"""Immutable execution artifacts: the unit that review approves and activation runs.

An artifact identity pins, all at once:
  * the canonicalized taxonomy document,
  * the rule interpretation protocol,
  * each operator's controlled version and config,
  * a content digest of the operator implementations.

The identity is a SHA-256 over the canonical serialization of those parts.
It is never derived from Python object addresses or version names alone.
"""
import hashlib
import json

from .rules import PredicateEngine
from .taxonomy import Taxonomy, canonical


class ArtifactError(ValueError):
    """An artifact cannot be built, verified or executed."""


class OperatorMismatch(ArtifactError):
    """The environment provides different operator content than reviewed."""


class ProtocolMismatch(ArtifactError):
    """The artifact was reviewed under an unsupported rule protocol."""


class ArtifactIntegrityError(ArtifactError):
    """A stored artifact row does not reproduce its recorded identity."""


class Artifact:
    """A frozen executor plus its auditable identity.

    The artifact snapshots the engine's operators at build time, so a caller
    mutating the registry afterwards cannot change what this artifact runs.
    """

    def __init__(self, document, engine, legacy_digest=None):
        self.engine = engine.snapshot()
        self.taxonomy = Taxonomy(document, self.engine)
        self.document = self.taxonomy.document
        self.taxonomy_digest = self.taxonomy.digest
        self.protocol = self.engine.PROTOCOL
        self.operators = self.engine.operator_descriptor()
        self.legacy = legacy_digest is not None
        if legacy_digest is not None:
            # Pre-artifact versions keep their historical document digest so
            # existing runs and the active pointer stay resolvable.
            self.digest = legacy_digest
        else:
            payload = {
                "document": self.document,
                "protocol": self.protocol,
                "operators": self.operators,
            }
            self.digest = hashlib.sha256(canonical(payload).encode()).hexdigest()

    @property
    def name(self):
        return self.document["name"]

    def classify(self, record):
        result = self.taxonomy.classify(record)
        result["version"] = self.digest
        result["taxonomy"] = self.taxonomy_digest
        return result


def verify_stored_artifact(row, registry, engine_limits):
    """Rebuild an artifact from its stored row, resolving operators from the
    trusted registry and verifying every pinned digest.

    Refuses (rather than silently running mutated content) when the caller's
    registry no longer provides the reviewed operator versions.
    """
    if row["protocol"] != PredicateEngine.PROTOCOL:
        raise ProtocolMismatch(
            f"artifact protocol {row['protocol']} is not supported by {PredicateEngine.PROTOCOL}")
    descriptor = json.loads(row["operators"])
    functions = {}
    for name, meta in descriptor.items():
        spec = registry.resolve(name, meta["version"])
        if spec is None:
            raise OperatorMismatch(
                f"operator {name}@{meta['version']} is not provided by this environment")
        if spec.digest != meta["digest"]:
            raise OperatorMismatch(
                f"operator {name}@{meta['version']} content changed since review")
        functions[name] = spec.function
    engine = PredicateEngine(registry=registry, **engine_limits)
    engine.operators = functions
    artifact = Artifact(json.loads(row["document"]), engine)
    if artifact.digest != row["digest"] or artifact.taxonomy_digest != row["taxonomy"]:
        raise ArtifactIntegrityError(f"stored artifact {row['digest']} does not reproduce its identity")
    return artifact
