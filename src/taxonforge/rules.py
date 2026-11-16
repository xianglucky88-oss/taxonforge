"""Bounded declarative predicates; no expression evaluation or user SQL."""
import hashlib
import json
from datetime import datetime, timezone


def timestamp(value):
    if not isinstance(value, str):
        raise ValueError("timestamp must be an ISO8601 string")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamps require an explicit timezone")
    return parsed.astimezone(timezone.utc)


def active(spec, at):
    return (not spec.get("from") or timestamp(spec["from"]) <= at) and (
        not spec.get("until") or at < timestamp(spec["until"]))


def _canonical(document):
    return json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _json_content(value, what):
    """Return a canonical JSON round-trip of value, rejecting non-content."""
    try:
        return json.loads(_canonical(value))
    except (TypeError, ValueError):
        raise ValueError(f"operator {what} must be JSON-serializable content") from None


def _code_content(code):
    """Content digest payload for a code object: bytecode, constants, names.

    Never includes object addresses; nested code objects are folded in
    recursively so behaviour-affecting content is fully covered.
    """
    return {
        "bytecode": code.co_code.hex(),
        "consts": [_const_content(const) for const in code.co_consts],
        "names": list(code.co_names),
    }


def _const_content(const):
    if const is None or isinstance(const, (bool, int, float, str)):
        return _json_content(const, "constant")
    if isinstance(const, bytes):
        return {"bytes": const.hex()}
    if isinstance(const, tuple):
        return {"tuple": [_const_content(c) for c in const]}
    if isinstance(const, frozenset):
        return {"frozenset": sorted((_const_content(c) for c in const), key=_canonical)}
    if hasattr(const, "co_code"):
        return {"code": _code_content(const)}
    raise ValueError(f"operator constant {const!r} is not digestible content")


def function_content(function):
    """Content payload of an operator function: bytecode, constants, default
    values and closure-captured state (which must be JSON content).

    Raises ValueError for callables whose content cannot be pinned; hashing
    object identities or version labels instead would not identify behaviour.
    """
    code = getattr(function, "__code__", None)
    if code is None:
        raise ValueError("operators must be introspectable Python functions")
    closure = function.__closure__ or ()
    return {
        "code": _code_content(code),
        "defaults": _json_content(function.__defaults__, "default values"),
        "kwdefaults": _json_content(function.__kwdefaults__, "keyword defaults"),
        "closure": [_json_content(cell.cell_contents, "captured state") for cell in closure],
    }


class OperatorSpec:
    """A controlled operator release: name, version, config and content digest."""

    def __init__(self, name, function, version, config=None):
        if not isinstance(name, str) or not name or not isinstance(version, str) or not version:
            raise ValueError("operator name and version must be nonempty strings")
        self.name, self.version = name, version
        self.function = function
        self.config = _json_content(config or {}, "config")
        self.digest = hashlib.sha256(_canonical({
            "version": self.version,
            "config": self.config,
            "function": function_content(function),
        }).encode()).hexdigest()


class OperatorRegistry:
    """Controlled operator versions. A released (name, version) is immutable:
    re-registering it with different content is rejected, so a caller cannot
    silently change what an already reviewed identity resolves to."""

    def __init__(self):
        self._library = {}
        self._current = {}

    def register(self, name, function, version, config=None):
        spec = OperatorSpec(name, function, version, config)
        existing = self._library.get((name, version))
        if existing is not None and existing.digest != spec.digest:
            raise ValueError(f"operator {name}@{version} is released with different content; choose a new version")
        self._library[(name, version)] = spec
        self._current[name] = spec
        return spec

    def resolve(self, name, version):
        return self._library.get((name, version))

    def spec_for_function(self, name, function):
        for spec in self._library.values():
            if spec.name == name and spec.function is function:
                return spec
        return None

    def current(self):
        return dict(self._current)

    @classmethod
    def builtins(cls):
        registry = cls()
        for name, function in _BUILTINS.items():
            registry.register(name, function, version="builtin/1")
        return registry


def _op_eq(a, b):
    return a == b


def _op_ne(a, b):
    return a != b


def _op_in(a, b):
    return a in b


def _op_contains(a, b):
    return b in a


def _op_gte(a, b):
    return a >= b


def _op_lt(a, b):
    return a < b


def _op_prefix(a, b):
    return isinstance(a, str) and a.startswith(b)


_BUILTINS = {
    "eq": _op_eq, "ne": _op_ne, "in": _op_in, "contains": _op_contains,
    "gte": _op_gte, "lt": _op_lt, "prefix": _op_prefix,
}


class PredicateEngine:
    # Rule interpretation protocol: node shapes, boolean semantics, leaf
    # resolution and reason codes. Bumped when interpretation changes; an
    # artifact pins the protocol it was reviewed under.
    PROTOCOL = "taxonforge.predicate/1"

    def __init__(self, operators=None, registry=None, max_nodes=100, max_depth=12):
        self.registry = registry if registry is not None else OperatorRegistry.builtins()
        for name, function in (operators or {}).items():
            self.registry.register(name, function, version="1")
        self._operators = None  # None: derive live from the registry
        self.max_nodes, self.max_depth = max_nodes, max_depth

    @property
    def operators(self):
        # Live engines follow the registry, so a caller publishing a new
        # controlled operator version changes what NEW drafts will run — and
        # therefore produces a new artifact identity. Artifacts never use this
        # live view: they execute on a frozen snapshot instead.
        if self._operators is not None:
            return self._operators
        return {name: spec.function for name, spec in self.registry.current().items()}

    @operators.setter
    def operators(self, functions):
        self._operators = dict(functions)

    def operator_descriptor(self):
        """Canonical {name: {version, config, digest}} for the operators this
        engine would execute right now, pinning content rather than identity."""
        if hasattr(self, "_descriptor"):
            return self._descriptor
        descriptor = {}
        for name in sorted(self.operators):
            spec = self.registry.spec_for_function(name, self.operators[name])
            if spec is not None:
                descriptor[name] = {"version": spec.version, "config": spec.config, "digest": spec.digest}
            else:
                descriptor[name] = {
                    "version": "uncontrolled",
                    "config": {},
                    "digest": OperatorSpec(name, self.operators[name], "uncontrolled").digest,
                }
        return descriptor

    def snapshot(self):
        """Frozen copy for an artifact: later mutation of this engine or its
        registry by the caller cannot leak into the snapshot."""
        clone = object.__new__(type(self))
        clone.registry = self.registry
        clone._operators = dict(self.operators)
        clone.max_nodes, clone.max_depth = self.max_nodes, self.max_depth
        clone._descriptor = self.operator_descriptor()
        return clone

    def validate(self, expression):
        nodes = 0
        def visit(node, depth):
            nonlocal nodes
            nodes += 1
            if nodes > self.max_nodes or depth > self.max_depth or not isinstance(node, dict):
                raise ValueError("predicate complexity limit exceeded or invalid node")
            branches = set(node) & {"all", "any", "not"}
            if branches:
                if len(branches) != 1 or len(node) != 1:
                    raise ValueError("ambiguous predicate")
                branch = next(iter(branches))
                children = [node[branch]] if branch == "not" else node[branch]
                if not isinstance(children, list) or not children:
                    raise ValueError("boolean predicate requires children")
                for child in children:
                    visit(child, depth + 1)
            elif set(node) != {"field", "op", "value"} or node["op"] not in self.operators or not isinstance(node["field"], str):
                raise ValueError("invalid leaf predicate")
            elif node["op"] == "in" and not isinstance(node["value"], list):
                raise ValueError("in operator requires an array")
        visit(expression, 0)

    def evaluate(self, expression, record):
        if "all" in expression or "any" in expression:
            branch = "all" if "all" in expression else "any"
            children = [self.evaluate(child, record) for child in expression[branch]]
            matched = all(c[0] for c in children) if branch == "all" else any(c[0] for c in children)
            return matched, {"operator": branch, "matched": matched, "children": [c[1] for c in children]}
        if "not" in expression:
            matched, detail = self.evaluate(expression["not"], record)
            return not matched, {"operator": "not", "matched": not matched, "child": detail}
        value = record
        found = True
        for part in expression["field"].split("."):
            if not isinstance(value, dict) or part not in value:
                found = False
                break
            value = value[part]
        matched, reason = False, None
        if found:
            try:
                matched = bool(self.operators[expression["op"]](value, expression["value"]))
            except (TypeError, ValueError):
                reason = "incompatible_type"
        else:
            reason = "missing_field"
        return matched, {"field": expression["field"], "operator": expression["op"],
                         "expected": expression["value"], "matched": matched, "reason": reason}
