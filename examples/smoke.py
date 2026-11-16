import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

project = Path(__file__).resolve().parents[1]
env = dict(os.environ, PYTHONPATH=str(project / "src"))


def cli(db, *args):
    completed = subprocess.run([sys.executable, "-m", "taxonforge.cli", "--db", str(db), *map(str, args)],
                               env=env, check=True, capture_output=True, text=True)
    return json.loads(completed.stdout)


def cli_error(db, *args):
    completed = subprocess.run([sys.executable, "-m", "taxonforge.cli", "--db", str(db), *map(str, args)],
                               env=env, capture_output=True, text=True)
    assert completed.returncode == 2, completed.stderr
    return completed.stderr.strip()


def publish(db, definition, expected):
    digest = cli(db, "register", definition, "--no-activate", "--author", "release-author")["version"]
    cli(db, "approve", digest, "--by", "reviewer-a")
    cli(db, "approve", digest, "--by", "reviewer-b")
    cli(db, "activate", digest, "--expected", expected)
    return digest


with tempfile.TemporaryDirectory() as directory:
    root = Path(directory)
    db = root / "taxonomy.sqlite"
    records = project / "examples" / "records.json"
    definition = project / "examples" / "taxonomy.json"

    # Activation is gated: a draft without two distinct non-author approvals
    # cannot be activated, whichever entry point is used.
    gated = cli_error(db, "register", definition, "--author", "release-author")
    assert "approvals" in gated, gated
    v1 = publish(db, definition, "none")
    assert cli(db, "status", v1)["status"] == "approved"

    original = cli(db, "classify", records)
    if [r["category"] for r in original["results"]] != ["hightech", "manufacturing", None]:
        raise RuntimeError("temporal classification failed")

    document = json.loads(definition.read_text())
    document["name"] = "industry-v2"
    document["rules"][0]["when"]["all"][1]["value"] = 10000000
    modified = root / "v2.json"
    modified.write_text(json.dumps(document))
    v2 = publish(db, modified, v1)
    assert v2 != v1

    impact = cli(db, "impact", records, v1, v2)
    if impact["changed"] != 1: raise RuntimeError("impact analysis incorrect")
    replayed = cli(db, "replay", original["run"])
    if replayed["results"][0]["category"] != "manufacturing": raise RuntimeError("replay failed")

    # A stale expected identity conflicts and appends no activation history.
    conflict = cli_error(db, "activate", v1, "--expected", v1)
    assert "expected active" in conflict, conflict

    cli(db, "activate", v1, "--expected", v2)
    rolled_back = cli(db, "classify", records)
    if rolled_back["results"] != original["results"]: raise RuntimeError("version rollback failed")
    print(json.dumps({"ok": True, "classified": 3, "impact_changed": 1, "replay": True,
                      "rollback": True, "gated": True, "cli_processes": 17}))
