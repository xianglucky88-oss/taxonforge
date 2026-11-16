"""Bounded declarative predicates; no expression evaluation or user SQL."""
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


class PredicateEngine:
    def __init__(self, operators=None, max_nodes=100, max_depth=12):
        self.operators = {
            "eq": lambda a, b: a == b,
            "ne": lambda a, b: a != b,
            "in": lambda a, b: a in b,
            "contains": lambda a, b: b in a,
            "gte": lambda a, b: a >= b,
            "lt": lambda a, b: a < b,
            "prefix": lambda a, b: isinstance(a, str) and a.startswith(b),
            **(operators or {}),
        }
        self.max_nodes, self.max_depth = max_nodes, max_depth

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
