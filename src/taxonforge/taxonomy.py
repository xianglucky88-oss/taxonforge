import hashlib
import json
from .rules import active, timestamp


def canonical(document):
    return json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


class Taxonomy:
    def __init__(self, document, engine):
        self.document = json.loads(canonical(document))
        document = self.document
        self.engine = engine
        self.categories, self.aliases = {}, {}
        if not isinstance(document.get("name"), str) or not document["name"]:
            raise ValueError("taxonomy name is required")
        for category in document.get("categories", []):
            code = category.get("code")
            if not isinstance(code, str) or not code or code in self.categories:
                raise ValueError("category codes must be unique nonempty strings")
            self.categories[code] = category
            self._dates(category)
        if not self.categories:
            raise ValueError("at least one category is required")
        for code, category in self.categories.items():
            for alias in [code, *category.get("aliases", [])]:
                if not isinstance(alias, str) or not alias or alias in self.aliases:
                    raise ValueError("duplicate or invalid category alias")
                self.aliases[alias] = code
            seen, node = set(), code
            while node is not None:
                if node in seen:
                    raise ValueError("category tree contains a cycle")
                if node not in self.categories:
                    raise ValueError("unknown parent category")
                seen.add(node)
                node = self.categories[node].get("parent")
        seen_rules = set()
        self.rules = document.get("rules", [])
        for rule in self.rules:
            if not isinstance(rule.get("id"), str) or not rule["id"] or rule["id"] in seen_rules:
                raise ValueError("rule identifiers must be unique")
            seen_rules.add(rule["id"])
            if rule.get("target") not in self.aliases or type(rule.get("priority", 0)) is not int:
                raise ValueError("invalid rule target or priority")
            self._dates(rule)
            engine.validate(rule["when"])
        self.digest = hashlib.sha256(canonical(self.document).encode()).hexdigest()

    @staticmethod
    def _dates(spec):
        for key in ("from", "until"):
            if spec.get(key): timestamp(spec[key])
        if spec.get("from") and spec.get("until") and timestamp(spec["from"]) >= timestamp(spec["until"]):
            raise ValueError("empty or inverted effective period")

    def path(self, code):
        result = []
        while code is not None:
            result.append(code)
            code = self.categories[code].get("parent")
        return list(reversed(result))

    def classify(self, record):
        at = timestamp(record["occurred_at"])
        matches, explanations = [], []
        for rule in sorted(self.rules, key=lambda r: (-r.get("priority", 0), r["id"])):
            code = self.aliases[rule["target"]]
            eligible = active(rule, at) and all(active(self.categories[c], at) for c in self.path(code))
            matched, trace = self.engine.evaluate(rule["when"], record)
            explanations.append({"rule": rule["id"], "eligible": eligible, "trace": trace})
            if eligible and matched:
                matches.append({"rule": rule["id"], "target": code, "priority": rule.get("priority", 0)})
        winners = [m for m in matches if m["priority"] == matches[0]["priority"]] if matches else []
        targets = sorted({m["target"] for m in winners})
        status = "unmatched" if not targets else "matched" if len(targets) == 1 else "conflict"
        code = targets[0] if status == "matched" else None
        return {"record_id": record["id"], "status": status, "category": code,
                "path": self.path(code) if code else [], "winners": winners,
                "explanations": explanations, "version": self.digest}
