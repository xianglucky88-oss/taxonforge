import json
import sqlite3
import uuid
from .rules import PredicateEngine
from .taxonomy import Taxonomy, canonical


class TaxonomyService:
    def __init__(self, path, engine=None):
        self.engine = engine or PredicateEngine()
        self.db = sqlite3.connect(path, timeout=10)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS versions (digest TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE, document TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS active (singleton INTEGER PRIMARY KEY CHECK(singleton=1), digest TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS activations (id INTEGER PRIMARY KEY, digest TEXT NOT NULL, at TEXT DEFAULT CURRENT_TIMESTAMP);
            CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, version TEXT NOT NULL, records TEXT NOT NULL, results TEXT NOT NULL);
        """)

    def register(self, document, activate=True):
        taxonomy = Taxonomy(document, self.engine)
        with self.db:
            existing = self.db.execute("SELECT digest FROM versions WHERE name=?", (document["name"],)).fetchone()
            if existing and existing[0] != taxonomy.digest:
                raise ValueError("version name is immutable; choose a new name")
            self.db.execute("INSERT OR IGNORE INTO versions VALUES (?,?,?)",
                            (taxonomy.digest, document["name"], canonical(taxonomy.document)))
            if activate:
                self._activate(taxonomy.digest)
        return taxonomy.digest

    def _activate(self, digest):
        self.db.execute("INSERT INTO active VALUES (1,?) ON CONFLICT(singleton) DO UPDATE SET digest=excluded.digest", (digest,))
        self.db.execute("INSERT INTO activations(digest) VALUES (?)", (digest,))

    def activate(self, digest):
        self.load(digest)
        with self.db: self._activate(digest)

    def load(self, digest=None):
        if digest is None:
            row = self.db.execute("SELECT digest FROM active").fetchone()
            if not row: raise ValueError("no active taxonomy")
            digest = row[0]
        row = self.db.execute("SELECT document FROM versions WHERE digest=?", (digest,)).fetchone()
        if not row: raise KeyError("unknown taxonomy version")
        return Taxonomy(json.loads(row[0]), self.engine)

    def classify(self, records, version=None):
        taxonomy = self.load(version)
        records = list(records)
        if any(not isinstance(r.get("id"), str) or not r["id"] for r in records):
            raise ValueError("records require nonempty string identifiers")
        if len({r["id"] for r in records}) != len(records):
            raise ValueError("duplicate record identifiers")
        results = [taxonomy.classify(record) for record in records]
        run_id = uuid.uuid4().hex
        with self.db:
            self.db.execute("INSERT INTO runs VALUES (?,?,?,?)",
                            (run_id, taxonomy.digest, canonical(records), canonical(results)))
        return {"run": run_id, "version": taxonomy.digest, "results": results}

    def replay(self, run_id, version=None):
        row = self.db.execute("SELECT records FROM runs WHERE id=?", (run_id,)).fetchone()
        if not row: raise KeyError("unknown run")
        return self.classify(json.loads(row[0]), version)

    def impact(self, records, before, after):
        left, right = self.load(before), self.load(after)
        changes = []
        for record in records:
            a, b = left.classify(record), right.classify(record)
            signature = lambda x: (x["status"], x["category"], x["path"], sorted(m["target"] for m in x["winners"]))
            if signature(a) != signature(b):
                changes.append({"record_id": record["id"], "before": a, "after": b})
        return {"before": before, "after": after, "changed": len(changes), "changes": changes}

    def close(self):
        self.db.close()
