"""Versioned taxonomy service with artifact review and gated activation.

Storage layout:
  * artifacts  — immutable execution artifacts (document + protocol + operator
                 descriptor), keyed by the artifact identity digest;
  * names      — version-name immutability (a name maps to one taxonomy document);
  * reviews    — append-only review events bound to an artifact digest;
  * active     — singleton pointer to the active artifact;
  * activations— activation history, written only on successful gated switches;
  * runs       — classification runs pinned to the artifact they executed;
  * versions   — legacy pre-artifact versions, kept read-only for compatibility.
"""
import json
import os
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from .artifact import Artifact, verify_stored_artifact
from .rules import PredicateEngine, timestamp
from .taxonomy import canonical

DEFAULT_APPROVAL_TTL = 7 * 24 * 3600
REQUIRED_APPROVALS = 2

_UNSET = object()


class ReviewError(ValueError):
    """A review action is not allowed in the artifact's current state."""


class ApprovalRequired(ReviewError):
    """The artifact does not have the required valid approvals."""


class DuplicateApproval(ReviewError):
    """The reviewer already holds a valid approval on this artifact."""


class ReviewForbidden(ReviewError):
    """The identity may not perform this review action."""


class ArtifactTerminal(ReviewError):
    """The artifact was rejected or retracted; no further review is possible."""


class ActivationConflict(ValueError):
    """The expected active identity did not match at commit time."""


SCHEMA = """
CREATE TABLE IF NOT EXISTS versions (digest TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE, document TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS active (singleton INTEGER PRIMARY KEY CHECK(singleton=1), digest TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS activations (id INTEGER PRIMARY KEY, digest TEXT NOT NULL, expected TEXT, at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, version TEXT NOT NULL, records TEXT NOT NULL, results TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS artifacts (
    digest TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    taxonomy TEXT NOT NULL,
    document TEXT NOT NULL,
    protocol TEXT NOT NULL,
    operators TEXT NOT NULL,
    author TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS names (name TEXT PRIMARY KEY, taxonomy TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS reviews (
    id INTEGER PRIMARY KEY,
    artifact TEXT NOT NULL REFERENCES artifacts(digest),
    reviewer TEXT NOT NULL,
    action TEXT NOT NULL CHECK(action IN ('approve','withdraw','reject','retract')),
    comment TEXT,
    at TEXT NOT NULL,
    expires_at TEXT
);
"""


class TaxonomyService:
    def __init__(self, path, engine=None, identity=None, clock=None):
        self.engine = engine or PredicateEngine()
        self.registry = self.engine.registry
        self.identity = identity or os.environ.get("TAXONFORGE_IDENTITY") or os.environ.get("USER") or "unknown"
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self.db = sqlite3.connect(path, timeout=10, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        self._migrate()

    def _migrate(self):
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(activations)")}
        if columns and "expected" not in columns:
            self.db.execute("ALTER TABLE activations ADD COLUMN expected TEXT")
        # Legacy version names stay immutable under the artifact regime.
        self.db.execute("INSERT OR IGNORE INTO names (name, taxonomy) SELECT name, digest FROM versions")

    def _now(self):
        return self._clock()

    @contextmanager
    def _txn(self):
        """Single commit boundary; BEGIN IMMEDIATE serializes writers so
        check-then-write sequences (review events, the activation gate) are
        atomic across processes."""
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield self.db
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        self.db.execute("COMMIT")

    # -- drafts ------------------------------------------------------------

    def register(self, document, activate=True, expected=_UNSET, author=None):
        """Normalize the draft into an immutable artifact and record it.

        With activate=True (the default) the artifact immediately passes
        through the same activation gate as every other entry point, which
        requires prior valid approvals for this exact identity.
        """
        author = author or self.identity
        artifact = Artifact(document, self.engine)
        with self._txn():
            row = self.db.execute("SELECT taxonomy FROM names WHERE name=?", (artifact.name,)).fetchone()
            if row and row["taxonomy"] != artifact.taxonomy_digest:
                raise ValueError("version name is immutable; choose a new name")
            self.db.execute("INSERT OR IGNORE INTO names VALUES (?,?)", (artifact.name, artifact.taxonomy_digest))
            self.db.execute(
                "INSERT OR IGNORE INTO artifacts (digest, name, taxonomy, document, protocol, operators, author, created_at)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (artifact.digest, artifact.name, artifact.taxonomy_digest, canonical(artifact.document),
                 artifact.protocol, canonical(artifact.operators), author, self._now().isoformat()))
        if activate:
            self.activate(artifact.digest, expected=expected)
        return artifact.digest

    # -- review ------------------------------------------------------------

    def _review_state(self, digest, now):
        row = self.db.execute("SELECT author, name, created_at FROM artifacts WHERE digest=?", (digest,)).fetchone()
        if row is None:
            raise KeyError(f"unknown artifact {digest}")
        events = [dict(e) for e in self.db.execute(
            "SELECT id, reviewer, action, comment, at, expires_at FROM reviews WHERE artifact=? ORDER BY id",
            (digest,))]
        approvers, rejected, retracted = {}, False, False
        for event in events:
            action = event["action"]
            if action == "approve":
                approvers[event["reviewer"]] = event["expires_at"]
            elif action == "withdraw":
                approvers.pop(event["reviewer"], None)
            elif action == "reject":
                rejected = True
            elif action == "retract":
                retracted = True
        author = row["author"]
        valid = sorted(
            reviewer for reviewer, expires_at in approvers.items()
            if reviewer != author and (expires_at is None or timestamp(expires_at) > now))
        if retracted:
            status = "retracted"
        elif rejected:
            status = "rejected"
        elif len(valid) >= REQUIRED_APPROVALS:
            status = "approved"
        else:
            status = "pending"
        return {"digest": digest, "name": row["name"], "author": author, "status": status,
                "approvers": valid, "required": REQUIRED_APPROVALS, "events": events}

    def review_state(self, digest):
        return self._review_state(digest, self._now())

    def _review(self, digest, reviewer, action, comment=None, expires_at=None):
        now = self._now()
        with self._txn():
            state = self._review_state(digest, now)
            if state["status"] in ("rejected", "retracted"):
                raise ArtifactTerminal(f"artifact is {state['status']}; review is closed, submit a new artifact")
            author = state["author"]
            if action == "approve":
                if reviewer == author:
                    raise ReviewForbidden("authors cannot approve their own draft")
                if reviewer in state["approvers"]:
                    raise DuplicateApproval(f"{reviewer} already holds a valid approval on {digest}")
            elif action == "withdraw":
                if reviewer not in state["approvers"]:
                    raise ReviewError(f"{reviewer} holds no valid approval on {digest} to withdraw")
            elif action == "reject":
                if reviewer == author:
                    raise ReviewForbidden("authors retract their own draft instead of rejecting it")
            elif action == "retract":
                if reviewer != author:
                    raise ReviewForbidden("only the author can retract a draft")
            self.db.execute(
                "INSERT INTO reviews (artifact, reviewer, action, comment, at, expires_at) VALUES (?,?,?,?,?,?)",
                (digest, reviewer, action, comment, now.isoformat(), expires_at))
        return self.review_state(digest)

    def approve(self, digest, reviewer, comment=None, ttl_seconds=DEFAULT_APPROVAL_TTL, expires_at=None):
        """Record an approval bound to the artifact identity. Approvals expire;
        re-approval after withdrawal or expiry is a new event."""
        if expires_at is not None:
            expires = timestamp(expires_at) if isinstance(expires_at, str) else expires_at
            if expires.tzinfo is None:
                raise ValueError("approval expiry requires an explicit timezone")
        elif ttl_seconds is not None:
            expires = self._now() + timedelta(seconds=ttl_seconds)
        else:
            expires = None
        return self._review(digest, reviewer, "approve", comment,
                            expires.isoformat() if expires is not None else None)

    def withdraw_approval(self, digest, reviewer, comment=None):
        return self._review(digest, reviewer, "withdraw", comment)

    def reject(self, digest, reviewer, comment=None):
        return self._review(digest, reviewer, "reject", comment)

    def retract(self, digest, author=None, comment=None):
        return self._review(digest, author or self.identity, "retract", comment)

    # -- activation gate ---------------------------------------------------

    def active_digest(self):
        row = self.db.execute("SELECT digest FROM active WHERE singleton=1").fetchone()
        return row["digest"] if row else None

    def _record_activation(self, con, digest, expected):
        con.execute("INSERT INTO activations (digest, expected) VALUES (?,?)", (digest, expected))

    def activate(self, digest, expected=_UNSET):
        """The single activation gate. In one commit boundary: verify the
        expected active identity, verify the artifact currently holds valid
        approvals, switch active and append history. Any failure rolls the
        whole transaction back, so no pseudo-activation history is written
        and a crash cannot leave a half-switched state."""
        if self.db.execute("SELECT 1 FROM artifacts WHERE digest=?", (digest,)).fetchone() is None:
            if self.db.execute("SELECT 1 FROM versions WHERE digest=?", (digest,)).fetchone() is not None:
                raise ApprovalRequired(
                    f"legacy version {digest} is readable but was never reviewed; re-register it as an artifact")
            raise KeyError(f"unknown artifact {digest}")
        now = self._now()
        with self._txn():
            current = self.active_digest()
            want = current if expected is _UNSET else expected
            if current != want:
                raise ActivationConflict(f"expected active {want}, found {current}")
            state = self._review_state(digest, now)
            if state["status"] != "approved":
                raise ApprovalRequired(
                    f"artifact {digest} is {state['status']} with {len(state['approvers'])} of "
                    f"{REQUIRED_APPROVALS} required valid approvals")
            if current is None:
                self.db.execute("INSERT INTO active VALUES (1,?)", (digest,))
            else:
                cursor = self.db.execute(
                    "UPDATE active SET digest=? WHERE singleton=1 AND digest=?", (digest, current))
                if cursor.rowcount != 1:
                    raise ActivationConflict("active version changed concurrently")
            self._record_activation(self.db, digest, current)

    # -- execution ---------------------------------------------------------

    def load(self, digest=None):
        if digest is None:
            digest = self.active_digest()
            if digest is None:
                raise ValueError("no active taxonomy")
        row = self.db.execute("SELECT * FROM artifacts WHERE digest=?", (digest,)).fetchone()
        if row is not None:
            limits = {"max_nodes": self.engine.max_nodes, "max_depth": self.engine.max_depth}
            return verify_stored_artifact(row, self.registry, limits)
        row = self.db.execute("SELECT digest, document FROM versions WHERE digest=?", (digest,)).fetchone()
        if row is not None:
            return Artifact(json.loads(row["document"]), self.engine, legacy_digest=row["digest"])
        raise KeyError("unknown taxonomy version")

    def classify(self, records, version=None):
        # Pin the artifact first: the whole run — rules, explanation protocol
        # and operators — comes from this frozen artifact, even if another
        # process switches the active version while the run is in flight.
        artifact = self.load(version)
        records = list(records)
        if any(not isinstance(r.get("id"), str) or not r["id"] for r in records):
            raise ValueError("records require nonempty string identifiers")
        if len({r["id"] for r in records}) != len(records):
            raise ValueError("duplicate record identifiers")
        results = [artifact.classify(record) for record in records]
        run_id = uuid.uuid4().hex
        self.db.execute("INSERT INTO runs VALUES (?,?,?,?)",
                        (run_id, artifact.digest, canonical(records), canonical(results)))
        return {"run": run_id, "version": artifact.digest, "results": results}

    def replay(self, run_id, version=None):
        row = self.db.execute("SELECT records FROM runs WHERE id=?", (run_id,)).fetchone()
        if not row:
            raise KeyError("unknown run")
        return self.classify(json.loads(row["records"]), version)

    def impact(self, records, before, after):
        left, right = self.load(before), self.load(after)
        changes = []
        for record in records:
            a, b = left.classify(record), right.classify(record)
            signature = lambda x: (x["status"], x["category"], x["path"], sorted(m["target"] for m in x["winners"]))
            if signature(a) != signature(b):
                changes.append({"record_id": record["id"], "before": a, "after": b})
        return {"before": left.digest, "after": right.digest, "changed": len(changes), "changes": changes}

    def close(self):
        self.db.close()
