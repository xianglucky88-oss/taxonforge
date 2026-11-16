import copy
import json
import tempfile
import unittest
from pathlib import Path
from taxonforge import TaxonomyService


class TaxonomyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = TaxonomyService(Path(self.tmp.name) / "taxonomy.sqlite")
        fixtures = Path(__file__).resolve().parents[1] / "examples"
        self.document = json.loads((fixtures / "taxonomy.json").read_text())
        self.records = json.loads((fixtures / "records.json").read_text())
        self.v1 = self.service.register(self.document)
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.service.close)

    def test_historical_effective_dates_and_priority(self):
        results = self.service.classify(self.records)["results"]
        self.assertEqual([r["category"] for r in results], ["hightech", "manufacturing", None])
        self.assertEqual(results[0]["path"], ["root", "manufacturing", "hightech"])
        self.assertEqual(results[2]["status"], "unmatched")

    def test_ties_produce_explicit_conflict(self):
        rule = copy.deepcopy(self.document["rules"][0])
        rule.update(id="tie", target="root")
        self.document["name"] = "v2"
        self.document["rules"].append(rule)
        self.service.register(self.document)
        result = self.service.classify(self.records)["results"][0]
        self.assertEqual(result["status"], "conflict")
        self.assertIsNone(result["category"])
        self.assertEqual(len(result["winners"]), 2)

    def test_impact_replay_and_rollback(self):
        original = self.service.classify(self.records)
        self.document["name"] = "v2"
        self.document["rules"][0]["when"]["all"][1]["value"] = 10000000
        v2 = self.service.register(self.document)
        self.assertEqual(self.service.impact(self.records, self.v1, v2)["changed"], 1)
        replay = self.service.replay(original["run"])
        self.assertEqual(replay["results"][0]["category"], "manufacturing")
        self.service.activate(self.v1)
        self.assertEqual(self.service.replay(original["run"])["results"], original["results"])

    def test_version_name_is_immutable(self):
        self.document["rules"].clear()
        with self.assertRaises(ValueError): self.service.register(self.document)
        self.assertEqual(self.service.load().digest, self.v1)

    def test_cycles_unknown_parents_and_alias_collisions(self):
        for mutation in ("cycle", "parent", "alias"):
            with self.subTest(mutation=mutation):
                doc = copy.deepcopy(self.document)
                if mutation == "cycle": doc["categories"][0]["parent"] = "hightech"
                if mutation == "parent": doc["categories"][0]["parent"] = "missing"
                if mutation == "alias": doc["categories"][0]["aliases"] = ["factory"]
                with self.assertRaises(ValueError): self.service.register(doc)

    def test_inverted_dates_and_timezone_required(self):
        self.records[0]["occurred_at"] = "2026-01-01T00:00:00"
        with self.assertRaises(ValueError): self.service.classify(self.records)
        self.document["categories"][2]["until"] = "2025-01-01T00:00:00Z"
        with self.assertRaises(ValueError): self.service.register(self.document)

    def test_invalid_batch_leaves_no_run(self):
        self.records.append(self.records[0])
        with self.assertRaises(ValueError): self.service.classify(self.records)
        self.assertEqual(self.service.db.execute("SELECT count(*) FROM runs").fetchone()[0], 0)

    def test_missing_and_incompatible_fields_are_explained(self):
        self.records[0]["revenue"] = "unknown"
        result = self.service.classify(self.records)["results"][0]
        trace = result["explanations"][0]["trace"]["children"][1]
        self.assertEqual(trace["reason"], "incompatible_type")

    def test_exclusive_until_boundary(self):
        self.document["name"] = "v2"
        self.document["categories"][2]["until"] = "2026-10-01T00:00:00Z"
        self.service.register(self.document)
        result = self.service.classify(self.records)["results"][0]
        self.assertEqual(result["category"], "manufacturing")

    def test_predicate_depth_limit(self):
        expression = {"field": "industry", "op": "eq", "value": "electronics"}
        for _ in range(15): expression = {"not": expression}
        self.document["rules"][0]["when"] = expression
        with self.assertRaises(ValueError): self.service.register(self.document)


if __name__ == "__main__": unittest.main()
