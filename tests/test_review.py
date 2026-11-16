import copy
import json
import multiprocessing
import sqlite3
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from taxonforge import (ActivationConflict, ApprovalRequired, ArtifactTerminal,
                        DuplicateApproval, OperatorMismatch, OperatorRegistry,
                        PredicateEngine, ReviewError, ReviewForbidden, TaxonomyService)

FIXTURES = Path(__file__).resolve().parents[1] / "examples"


def op_gte(a, b):
    return a >= b


def op_gt(a, b):
    return a > b


def load_document():
    return json.loads((FIXTURES / "taxonomy.json").read_text())


def load_records():
    return json.loads((FIXTURES / "records.json").read_text())


def _activation_worker(db_path, digest, expected, barrier, queue):
    service = TaxonomyService(db_path)
    try:
        barrier.wait()
        service.activate(digest, expected=expected)
        queue.put(("ok", None))
    except Exception as exc:  # outcome asserted by type name
        queue.put(("error", type(exc).__name__))
    finally:
        service.close()


class Clock:
    def __init__(self):
        self.now = datetime(2026, 11, 16, 12, 0, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.now

    def advance(self, **kwargs):
        self.now += timedelta(**kwargs)


class ReviewTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = str(Path(self.tmp.name) / "taxonomy.sqlite")
        self.clock = Clock()
        self.service = TaxonomyService(self.db_path, identity="author", clock=self.clock)
        self.addCleanup(self.service.close)
        self.document = load_document()
        self.records = load_records()

    def submit(self, document=None, author="author"):
        return self.service.register(document or self.document, activate=False, author=author)

    def varied_document(self, name="industry-v2"):
        document = copy.deepcopy(self.document)
        document["name"] = name
        document["rules"][0]["when"]["all"][1]["value"] = 10000000
        return document

    def approve_pair(self, digest, reviewers=("reviewer-1", "reviewer-2")):
        for reviewer in reviewers:
            self.service.approve(digest, reviewer)

    def publish(self, document=None):
        digest = self.submit(document)
        self.approve_pair(digest)
        self.service.activate(digest)
        return digest

    def activations(self, digest=None):
        sql = "SELECT digest, expected FROM activations"
        rows = self.service.db.execute(sql).fetchall()
        return [(r["digest"], r["expected"]) for r in rows if digest is None or r["digest"] == digest]


class ReviewFlowTests(ReviewTestCase):
    def test_activation_requires_two_distinct_non_author_approvals(self):
        digest = self.submit()
        with self.assertRaises(ApprovalRequired):
            self.service.activate(digest)
        self.service.approve(digest, "reviewer-1")
        with self.assertRaises(ApprovalRequired):
            self.service.activate(digest)
        self.service.approve(digest, "reviewer-2")
        self.assertEqual(self.service.review_state(digest)["status"], "approved")
        self.service.activate(digest)
        self.assertEqual(self.service.active_digest(), digest)
        self.assertEqual(self.activations(digest), [(digest, None)])

    def test_register_default_activation_passes_the_same_gate(self):
        with self.assertRaises(ApprovalRequired):
            self.service.register(self.document)  # activate=True by default
        self.assertIsNone(self.service.active_digest())
        self.assertEqual(self.activations(), [])
        # The draft was still recorded; once approved, re-registering the same
        # content activates through the gate.
        digest = self.service.register(self.document, activate=False)
        self.approve_pair(digest)
        again = self.service.register(self.document)
        self.assertEqual(again, digest)
        self.assertEqual(self.service.active_digest(), digest)

    def test_author_cannot_approve_and_reviewer_must_differ(self):
        digest = self.submit()
        with self.assertRaises(ReviewForbidden):
            self.service.approve(digest, "author")
        self.service.approve(digest, "reviewer-1")
        with self.assertRaises(ApprovalRequired):
            self.service.activate(digest)

    def test_duplicate_approval_rejected_until_withdrawn_or_expired(self):
        digest = self.submit()
        self.service.approve(digest, "reviewer-1")
        with self.assertRaises(DuplicateApproval):
            self.service.approve(digest, "reviewer-1")
        self.service.withdraw_approval(digest, "reviewer-1")
        self.service.approve(digest, "reviewer-1")  # re-approval after withdrawal is a new event
        with self.assertRaises(DuplicateApproval):
            self.service.approve(digest, "reviewer-1")

    def test_approval_expiry_has_a_clear_result(self):
        digest = self.submit()
        self.service.approve(digest, "reviewer-1", ttl_seconds=60)
        self.service.approve(digest, "reviewer-2", ttl_seconds=3600)
        self.assertEqual(self.service.review_state(digest)["status"], "approved")
        self.clock.advance(seconds=61)
        state = self.service.review_state(digest)
        self.assertEqual(state["status"], "pending")
        self.assertEqual(state["approvers"], ["reviewer-2"])
        with self.assertRaises(ApprovalRequired):
            self.service.activate(digest)
        self.service.approve(digest, "reviewer-1")  # expired approval may be refreshed
        self.assertEqual(self.service.review_state(digest)["status"], "approved")

    def test_withdraw_requires_an_existing_approval(self):
        digest = self.submit()
        with self.assertRaises(ReviewError):
            self.service.withdraw_approval(digest, "reviewer-1")

    def test_rejection_is_terminal(self):
        digest = self.submit()
        self.service.approve(digest, "reviewer-1")
        self.service.reject(digest, "reviewer-2", comment="rule 3 is unsound")
        self.assertEqual(self.service.review_state(digest)["status"], "rejected")
        with self.assertRaises(ApprovalRequired):
            self.service.activate(digest)
        with self.assertRaises(ArtifactTerminal):
            self.service.approve(digest, "reviewer-2")

    def test_retract_only_by_author_and_terminal(self):
        digest = self.submit()
        with self.assertRaises(ReviewForbidden):
            self.service.retract(digest, author="reviewer-1")
        with self.assertRaises(ReviewForbidden):
            self.service.reject(digest, "author")
        self.service.retract(digest)
        self.assertEqual(self.service.review_state(digest)["status"], "retracted")
        with self.assertRaises(ArtifactTerminal):
            self.service.approve(digest, "reviewer-1")
        with self.assertRaises(ApprovalRequired):
            self.service.activate(digest)

    def test_review_events_bind_identity_not_content_echo(self):
        digest = self.submit()
        self.approve_pair(digest)
        changed = copy.deepcopy(self.document)
        changed["name"] = "industry-v2"
        changed["rules"][0]["when"]["all"][1]["value"] = 10000000
        other = self.submit(changed)
        self.assertNotEqual(digest, other)
        state = self.service.review_state(other)
        self.assertEqual(state["status"], "pending")
        self.assertEqual(state["approvers"], [])
        with self.assertRaises(ApprovalRequired):
            self.service.activate(other)

    def test_expected_active_mismatch_conflicts_without_history(self):
        v1 = self.publish()
        candidate = self.submit(self.varied_document())
        self.approve_pair(candidate)
        with self.assertRaises(ActivationConflict):
            self.service.activate(candidate, expected="not-the-active-digest")
        self.assertEqual(self.service.active_digest(), v1)
        self.assertEqual(self.activations(candidate), [])
        self.service.activate(candidate, expected=v1)
        self.assertEqual(self.activations(candidate), [(candidate, v1)])

    def test_unknown_and_legacy_digests_cannot_be_activated(self):
        with self.assertRaises(KeyError):
            self.service.activate("0" * 64)


class OperatorIdentityTests(ReviewTestCase):
    def setUp(self):
        super().setUp()
        self.registry = OperatorRegistry.builtins()
        self.service.close()
        self.service = TaxonomyService(self.db_path, engine=PredicateEngine(registry=self.registry),
                                       identity="author", clock=self.clock)

    def test_operator_change_after_review_creates_new_identity(self):
        v1 = self.submit()
        self.approve_pair(v1)
        # The caller publishes a new controlled operator version AFTER review.
        self.registry.register("gte", op_gt, version="2", config={"inclusive": False})
        v2 = self.service.register(self.document, activate=False)
        self.assertNotEqual(v1, v2)
        state = self.service.review_state(v2)
        self.assertEqual(state["status"], "pending")  # old approvals bind v1 only
        with self.assertRaises(ApprovalRequired):
            self.service.activate(v2)
        # v1 stays approved and still executes with the operators it pinned.
        self.service.activate(v1)
        record = {"id": "boundary", "occurred_at": "2026-06-01T00:00:00Z",
                  "industry": "electronics", "revenue": 1000000}
        result = self.service.classify([record], version=v1)["results"][0]
        self.assertEqual(result["category"], "hightech")  # inclusive gte, not the new strict one

    def test_released_operator_version_content_is_immutable(self):
        self.registry.register("gte", op_gt, version="2")
        with self.assertRaises(ValueError):
            self.registry.register("gte", op_gte, version="2")  # same label, different content
        self.registry.register("gte", op_gt, version="2")  # identical content is idempotent

    def test_mutating_live_engine_changes_only_future_drafts(self):
        v1 = self.submit()
        self.approve_pair(v1)
        self.service.registry.register("gte", op_gt, version="2")
        v2 = self.service.register(self.document, activate=False)
        self.assertNotEqual(v1, v2)
        # The already stored v1 still verifies and runs with its pinned operators.
        artifact = self.service.load(v1)
        self.assertEqual(artifact.operators["gte"]["version"], "builtin/1")
        record = {"id": "boundary", "occurred_at": "2026-06-01T00:00:00Z",
                  "industry": "electronics", "revenue": 1000000}
        self.assertEqual(artifact.classify(record)["category"], "hightech")

    def test_environment_without_reviewed_operators_is_refused(self):
        self.registry.register("gte", op_gt, version="2")
        digest = self.submit()
        self.approve_pair(digest)
        foreign = TaxonomyService(self.db_path)  # default registry lacks gte@2
        self.addCleanup(foreign.close)
        with self.assertRaises(OperatorMismatch):
            foreign.load(digest)
        with self.assertRaises(OperatorMismatch):
            foreign.classify(self.records, version=digest)


class ConcurrencyTests(ReviewTestCase):
    def test_last_approval_races_withdrawal(self):
        digest = self.submit()
        self.service.approve(digest, "reviewer-1")
        barrier = threading.Barrier(3)
        errors = []

        def final_approval():
            service = TaxonomyService(self.db_path, clock=self.clock)
            try:
                barrier.wait()
                service.approve(digest, "reviewer-2")
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
            finally:
                service.close()

        def withdrawal():
            service = TaxonomyService(self.db_path, clock=self.clock)
            try:
                barrier.wait()
                service.withdraw_approval(digest, "reviewer-1")
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
            finally:
                service.close()

        threads = [threading.Thread(target=final_approval), threading.Thread(target=withdrawal)]
        for thread in threads: thread.start()
        barrier.wait()
        for thread in threads: thread.join(30)
        self.assertEqual(errors, [])
        # Whatever the order, exactly one valid approval survives and the
        # artifact is not activatable.
        state = self.service.review_state(digest)
        self.assertEqual(state["approvers"], ["reviewer-2"])
        self.assertEqual(state["status"], "pending")
        with self.assertRaises(ApprovalRequired):
            self.service.activate(digest)

    def test_approval_then_withdrawal_orders_are_consistent(self):
        digest = self.submit()
        self.service.approve(digest, "reviewer-1")
        self.service.approve(digest, "reviewer-2")
        self.service.withdraw_approval(digest, "reviewer-1")
        self.assertEqual(self.service.review_state(digest)["status"], "pending")
        with self.assertRaises(ApprovalRequired):
            self.service.activate(digest)

    def test_retract_races_final_approval(self):
        digest = self.submit()
        self.service.approve(digest, "reviewer-1")
        self.service.retract(digest)
        with self.assertRaises(ArtifactTerminal):
            self.service.approve(digest, "reviewer-2")
        with self.assertRaises(ApprovalRequired):
            self.service.activate(digest)

    def test_two_processes_racing_activation_produce_a_single_winner(self):
        v1 = self.publish()
        candidate = self.submit(self.varied_document())
        self.approve_pair(candidate)
        ctx = multiprocessing.get_context("fork")
        barrier, queue = ctx.Barrier(2), ctx.Queue()
        processes = [ctx.Process(target=_activation_worker,
                                 args=(self.db_path, candidate, v1, barrier, queue))
                     for _ in range(2)]
        for process in processes: process.start()
        for process in processes: process.join(30)
        for process in processes:
            if process.is_alive():
                process.terminate()
                self.fail("activation worker did not finish")
        outcomes = [queue.get(timeout=10) for _ in range(2)]
        self.assertEqual(sorted(kind for kind, _ in outcomes), ["error", "ok"])
        loser = [detail for kind, detail in outcomes if kind == "error"][0]
        self.assertEqual(loser, "ActivationConflict")
        self.assertEqual(self.service.active_digest(), candidate)
        self.assertEqual(self.activations(candidate), [(candidate, v1)])  # no pseudo history from the loser

    def test_crash_before_commit_leaves_no_half_state(self):
        v1 = self.publish()
        candidate = self.submit(self.varied_document())
        self.approve_pair(candidate)

        class CrashingService(TaxonomyService):
            def _record_activation(self, con, digest, expected):
                raise RuntimeError("simulated crash before commit")

        crashing = CrashingService(self.db_path, clock=self.clock)
        with self.assertRaises(RuntimeError):
            crashing.activate(candidate, expected=v1)
        crashing.close()
        self.assertEqual(self.service.active_digest(), v1)
        self.assertEqual(self.activations(candidate), [])
        # The approvals are durable; only the activation was rolled back.
        self.assertEqual(self.service.review_state(candidate)["status"], "approved")

    def test_uncommitted_active_switch_is_invisible_after_process_exit(self):
        v1 = self.publish()
        candidate = self.submit(self.varied_document())
        self.approve_pair(candidate)
        connection = sqlite3.connect(self.db_path, isolation_level=None)
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("UPDATE active SET digest=? WHERE singleton=1", (candidate,))
        connection.close()  # process-exit analogue: the open transaction is discarded
        self.assertEqual(self.service.active_digest(), v1)
        self.assertEqual(self.activations(candidate), [])

    def test_run_pins_artifact_while_active_switches_mid_run(self):
        v1 = self.publish()
        self.service.registry.register("gte", op_gt, version="2")
        v2 = self.service.register(self.document, activate=False)
        self.approve_pair(v2)
        record = {"id": "boundary", "occurred_at": "2026-06-01T00:00:00Z",
                  "industry": "electronics", "revenue": 1000000}

        def records():
            # A second process commits the activation after the run pinned v1.
            other = TaxonomyService(self.db_path)
            try:
                other.activate(v2, expected=v1)
            finally:
                other.close()
            yield record

        run = self.service.classify(records())
        self.assertEqual(self.service.active_digest(), v2)  # the switch really happened mid-run
        self.assertEqual(run["version"], v1)
        self.assertEqual(run["results"][0]["version"], v1)
        # Inclusive gte from the pinned artifact, not the new strict operator.
        self.assertEqual(run["results"][0]["category"], "hightech")
        trace = run["results"][0]["explanations"][0]["trace"]["children"][1]
        self.assertTrue(trace["matched"])
        row = self.service.db.execute("SELECT version FROM runs WHERE id=?", (run["run"],)).fetchone()
        self.assertEqual(row["version"], v1)


class RunArtifactConsistencyTests(ReviewTestCase):
    def setUp(self):
        super().setUp()
        self.document = {
            "name": "boundary-v1",
            "categories": [
                {"code": "root"},
                {"code": "manufacturing", "parent": "root", "aliases": ["factory"]},
                {"code": "hightech", "parent": "manufacturing",
                 "from": "2026-01-01T00:00:00Z", "until": "2027-01-01T00:00:00Z"},
            ],
            "rules": [
                {"id": "advanced", "target": "hightech", "priority": 20, "when": {"all": [
                    {"field": "industry", "op": "eq", "value": "electronics"},
                    {"field": "revenue", "op": "gte", "value": 1000000}]}},
                {"id": "basic", "target": "factory", "priority": 10,
                 "when": {"field": "industry", "op": "in", "value": ["electronics", "machinery"]}},
                {"id": "tie-a", "target": "manufacturing", "priority": 5,
                 "when": {"field": "industry", "op": "eq", "value": "conflicting"}},
                {"id": "tie-b", "target": "root", "priority": 5,
                 "when": {"field": "industry", "op": "eq", "value": "conflicting"}},
            ],
        }
        self.records = [
            {"id": "from-boundary", "occurred_at": "2026-01-01T00:00:00Z",
             "industry": "electronics", "revenue": 1000000},
            {"id": "until-boundary", "occurred_at": "2027-01-01T00:00:00Z",
             "industry": "electronics", "revenue": 1000000},
            {"id": "missing-field", "occurred_at": "2026-06-01T00:00:00Z", "industry": "electronics"},
            {"id": "tie", "occurred_at": "2026-06-01T00:00:00Z", "industry": "conflicting", "revenue": 5},
        ]
        self.digest = self.publish(self.document)

    @staticmethod
    def collect_operators(trace, operators):
        if "children" in trace:
            for child in trace["children"]:
                RunArtifactConsistencyTests.collect_operators(child, operators)
        elif "child" in trace:
            RunArtifactConsistencyTests.collect_operators(trace["child"], operators)
        else:
            operators.add(trace["operator"])

    def test_run_interpretation_matches_recorded_artifact(self):
        run = self.service.classify(self.records)
        self.assertEqual(run["version"], self.digest)
        statuses = [(r["status"], r["category"]) for r in run["results"]]
        self.assertEqual(statuses, [("matched", "hightech"), ("matched", "manufacturing"),
                                    ("matched", "manufacturing"), ("conflict", None)])
        missing = run["results"][2]["explanations"][0]["trace"]["children"][1]
        self.assertEqual(missing["reason"], "missing_field")
        row = self.service.db.execute("SELECT * FROM runs WHERE id=?", (run["run"],)).fetchone()
        self.assertEqual(row["version"], self.digest)
        stored_records = json.loads(row["records"])
        stored_results = json.loads(row["results"])
        self.assertEqual(stored_results, run["results"])
        # Re-executing the stored records against the recorded artifact
        # reproduces the stored interpretation exactly.
        again = self.service.classify(stored_records, version=row["version"])
        self.assertEqual(again["results"], stored_results)
        for result in stored_results:
            self.assertEqual(result["version"], self.digest)
        # Every operator named in the explanations belongs to the artifact.
        artifact = self.service.load(self.digest)
        operators = set()
        for result in stored_results:
            for explanation in result["explanations"]:
                self.collect_operators(explanation["trace"], operators)
        self.assertTrue(operators)
        self.assertLessEqual(operators, set(artifact.operators) | {"all", "any", "not"})


class LegacyCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = str(Path(self.tmp.name) / "taxonomy.sqlite")
        self.document = load_document()
        self.records = load_records()
        # Build a pre-artifact database with the historical schema.
        from taxonforge.rules import PredicateEngine
        from taxonforge.taxonomy import Taxonomy, canonical
        taxonomy = Taxonomy(self.document, PredicateEngine())
        self.legacy_digest = taxonomy.digest
        connection = sqlite3.connect(self.db_path)
        connection.executescript("""
            CREATE TABLE versions (digest TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE, document TEXT NOT NULL);
            CREATE TABLE active (singleton INTEGER PRIMARY KEY CHECK(singleton=1), digest TEXT NOT NULL);
            CREATE TABLE activations (id INTEGER PRIMARY KEY, digest TEXT NOT NULL, at TEXT DEFAULT CURRENT_TIMESTAMP);
            CREATE TABLE runs (id TEXT PRIMARY KEY, version TEXT NOT NULL, records TEXT NOT NULL, results TEXT NOT NULL);
        """)
        connection.execute("INSERT INTO versions VALUES (?,?,?)",
                           (self.legacy_digest, self.document["name"], canonical(taxonomy.document)))
        connection.execute("INSERT INTO active VALUES (1,?)", (self.legacy_digest,))
        connection.commit()
        connection.close()
        self.service = TaxonomyService(self.db_path, identity="author")
        self.addCleanup(self.service.close)

    def test_legacy_versions_stay_readable(self):
        self.assertEqual(self.service.active_digest(), self.legacy_digest)
        artifact = self.service.load(self.legacy_digest)
        self.assertTrue(artifact.legacy)
        result = self.service.classify(self.records)
        self.assertEqual(result["version"], self.legacy_digest)
        self.assertEqual([r["category"] for r in result["results"]],
                         ["hightech", "manufacturing", None])

    def test_legacy_versions_are_not_activatable_and_drafts_not_auto_approved(self):
        with self.assertRaises(ApprovalRequired):
            self.service.activate(self.legacy_digest)
        # Compatibility mode must not treat new drafts as approved either.
        changed = copy.deepcopy(self.document)
        changed["name"] = "industry-v2"
        draft = self.service.register(changed, activate=False)
        self.assertEqual(self.service.review_state(draft)["status"], "pending")
        with self.assertRaises(ApprovalRequired):
            self.service.activate(draft)

    def test_legacy_names_stay_immutable(self):
        changed = copy.deepcopy(self.document)
        changed["rules"][0]["when"]["all"][1]["value"] = 10000000
        with self.assertRaises(ValueError):
            self.service.register(changed, activate=False)


if __name__ == "__main__":
    unittest.main()
