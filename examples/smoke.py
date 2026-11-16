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


with tempfile.TemporaryDirectory() as directory:
    root = Path(directory)
    db = root / "taxonomy.sqlite"
    records = project / "examples" / "records.json"
    definition = project / "examples" / "taxonomy.json"
    v1 = cli(db, "register", definition)["version"]
    original = cli(db, "classify", records)
    if [r["category"] for r in original["results"]] != ["hightech", "manufacturing", None]:
        raise RuntimeError("temporal classification failed")
    document = json.loads(definition.read_text())
    document["name"] = "industry-v2"
    document["rules"][0]["when"]["all"][1]["value"] = 10000000
    modified = root / "v2.json"
    modified.write_text(json.dumps(document))
    v2 = cli(db, "register", modified)["version"]
    impact = cli(db, "impact", records, v1, v2)
    if impact["changed"] != 1: raise RuntimeError("impact analysis incorrect")
    replayed = cli(db, "replay", original["run"])
    if replayed["results"][0]["category"] != "manufacturing": raise RuntimeError("replay failed")
    cli(db, "activate", v1)
    rolled_back = cli(db, "classify", records)
    if rolled_back["results"] != original["results"]: raise RuntimeError("version rollback failed")
    print(json.dumps({"ok": True, "classified": 3, "impact_changed": 1, "replay": True, "rollback": True, "cli_processes": 7}))
