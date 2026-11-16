"""Term-mention annotation, independent of rule classification.

This module only *mentions* taxonomy aliases inside free text. It never
selects a category, never evaluates predicates and never writes a
classification run. The matching pipeline is fixed to Unicode 14.0.0:

1. split the *original* text into extended grapheme clusters (UAX #29),
2. apply NFKC then :meth:`str.casefold` to each complete cluster,
3. concatenate the per-cluster retrieval forms,
4. match alias retrieval keys against that view,
5. map every match back to whole original-cluster boundaries.

All positions are Python string codepoint offsets (half-open) into the
original text -- never UTF-8 byte offsets and never offsets into the
normalized view. The exact alias resolution on :class:`Taxonomy` is
untouched; the normalization pipeline below exists solely in this module.
"""
import hashlib
import json
import sys
import unicodedata
from collections import deque

from ._grapheme import (
    SEGMENTATION_VERSION,
    StreamingGraphemeSplitter,
    grapheme_clusters,
)
from .rules import PredicateEngine
from .taxonomy import Taxonomy, canonical

#: Protocol version for the on-the-wire annotation envelope.
MATCH_PROTOCOL_VERSION = "tf-mentions/1.0"
#: Pinned Unicode data version. The tables live in _ucd14_data; the
#: interpreter's unicodedata must expose the same NFKC/casefold data.
PINNED_UNICODE_VERSION = "14.0.0"

# Declared resource ceilings. All counts are explicit in the API and an
# over-budget condition raises MentionBudgetError instead of truncating.
DEFAULT_MAX_CLUSTER_CODEPOINTS = 64
DEFAULT_MAX_CANDIDATES_PER_MENTION = 1024
DEFAULT_MAX_MENTIONS = 1_000_000
DEFAULT_MAX_RESIDENT_TEXT_CODEPOINTS = 1_000_000
DEFAULT_MAX_STAGING_BYTES = 64 * 1024 * 1024


class MentionError(ValueError):
    """Base error for mention annotation failures."""


class MentionBudgetError(MentionError):
    """A declared resource ceiling would be exceeded; nothing is truncated."""

    def __init__(self, reason, stats):
        super().__init__(f"mention budget exceeded: {reason}")
        self.reason = reason
        self.stats = dict(stats)


def _require_pinned_unicode():
    if unicodedata.unidata_version != PINNED_UNICODE_VERSION:
        raise MentionError(
            f"mention annotation requires unicodedata {PINNED_UNICODE_VERSION} "
            f"(pinned protocol), interpreter provides {unicodedata.unidata_version}")


_require_pinned_unicode()


def cluster_retrieval_form(cluster):
    """NFKC then casefold applied to one complete grapheme cluster."""
    return unicodedata.normalize("NFKC", cluster).casefold()


def retrieval_key(alias):
    """Retrieval key for an alias: per-cluster NFKC+casefold, concatenated."""
    return "".join(cluster_retrieval_form(c) for c in grapheme_clusters(alias))


def _deep_size(obj, seen=None):
    """Conservative resident-byte estimate for index structures."""
    if seen is None:
        seen = set()
    marker = id(obj)
    if marker in seen:
        return 0
    seen.add(marker)
    size = sys.getsizeof(obj)
    if isinstance(obj, dict):
        size += sum(_deep_size(k, seen) + _deep_size(v, seen) for k, v in obj.items())
    elif isinstance(obj, (list, tuple, set, frozenset, deque)):
        size += sum(_deep_size(v, seen) for v in obj)
    return size


class AliasIndex:
    """Version-pinned, read-only dictionary of alias retrieval keys.

    Several literal aliases may normalize to the same key and several nodes
    may share a key. Candidates are stored as an ordered sequence and marked
    ambiguous; dictionary insertion order never removes or reorders a node.
    """

    def __init__(self, document):
        # Reuse Taxonomy's structural validation (unique codes/aliases,
        # parent existence, cycle detection, rule well-formedness) so an
        # annotation index can only be built from a document the service
        # itself would accept. The engine is never used to evaluate rules.
        taxonomy = Taxonomy(document, PredicateEngine())
        self.document_digest = taxonomy.digest
        # Read the literal alias -> node mapping straight from the validated
        # taxonomy. The mention pipeline is the only place folded keys merge.
        self._build(taxonomy.aliases)

    @classmethod
    def from_taxonomy(cls, taxonomy):
        """Build an index from an already-validated, version-bound taxonomy."""
        index = cls.__new__(cls)
        index.document_digest = taxonomy.digest
        index._build(taxonomy.aliases)
        return index

    def _build(self, exact_aliases):
        # key -> {"aliases": sorted distinct literals, "nodes": sorted codes}
        self.keys = {}
        self.alias_count = 0
        for literal, code in exact_aliases.items():
            self.alias_count += 1
            key = retrieval_key(literal)
            if not key:
                raise MentionError(
                    f"alias {literal!r} of node {code!r} normalizes to an empty key")
            entry = self.keys.setdefault(key, {"aliases": set(), "nodes": []})
            entry["aliases"].add(literal)
            if code not in entry["nodes"]:
                entry["nodes"].append(code)
        if not self.keys:
            raise MentionError("annotator requires at least one alias")
        for entry in self.keys.values():
            entry["aliases"] = tuple(sorted(entry["aliases"]))
            entry["nodes"] = tuple(sorted(entry["nodes"]))
        self.key_count = len(self.keys)
        # Keys live in the normalized view, so their lengths are view
        # codepoint lengths; never compare them with original cluster counts.
        self.max_key_view_len = max(len(key) for key in self.keys)
        payload = [
            self.document_digest,
            sorted((key, entry["aliases"], entry["nodes"])
                   for key, entry in self.keys.items()),
        ]
        self.digest = hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":"),
                       allow_nan=False).encode()).hexdigest()
        self.index_bytes = _deep_size(self.keys) + _deep_size(
            {k: v for k, v in vars(self).items() if k not in ("keys",)})

    def entry(self, key):
        return self.keys.get(key)

    def entries(self):
        return self.keys


class AhoCorasick:
    """Multi-pattern automaton over retrieval keys.

    The automaton walks the *view* codepoint stream (one view string per
    finalized original cluster). Outputs carry view offsets which the
    annotator maps back to original grapheme boundaries. Counters record
    work performed so multi-pattern matching can be compared with the
    per-word reference scanner.
    """

    def __init__(self, keys):
        self.goto = [{}]
        self.failure = [0]
        self.output = [()]
        self.transitions_taken = 0
        for key in sorted(keys):
            self._insert(key)
        self._build_failure_links()
        self.states = len(self.goto)
        self.memory_bytes = _deep_size(
            [self.goto, self.failure, self.output])

    def _insert(self, key):
        state = 0
        for ch in key:
            nxt = self.goto[state].get(ch)
            if nxt is None:
                nxt = len(self.goto)
                self.goto[state][ch] = nxt
                self.goto.append({})
                self.failure.append(0)
                self.output.append(())
            state = nxt
        self.output[state] = (*self.output[state], key)

    def _build_failure_links(self):
        queue = deque()
        for ch, nxt in self.goto[0].items():
            queue.append(nxt)
        while queue:
            state = queue.popleft()
            for ch, nxt in self.goto[state].items():
                queue.append(nxt)
                fallback = self.failure[state]
                while fallback and ch not in self.goto[fallback]:
                    fallback = self.failure[fallback]
                self.failure[nxt] = self.goto[fallback].get(ch, 0)
                self.output[nxt] = (
                    *self.output[nxt], *self.output[self.failure[nxt]])

    def step(self, state, ch):
        """One automaton transition with memoized failure resolution."""
        self.transitions_taken += 1
        while state and ch not in self.goto[state]:
            state = self.failure[state]
        return self.goto[state].get(ch, 0)


def reference_scan(window_view, index, probes):
    """Per-word reference matcher: test every key against a suffix window.

    Used as the comparison strategy against the Aho-Corasick automaton. It
    performs identical cluster-aligned matching, just with explicit probes.
    """
    hits = []
    keys = index.keys
    for key in keys:
        probes[0] += 1
        if len(key) <= len(window_view) and window_view.endswith(key):
            hits.append(key)
    return hits


def solve_non_overlapping(mentions):
    """Deterministic non-overlapping view of all mentions.

    Optimization order, applied globally:
      1. maximize total covered original codepoints,
      2. among ties, minimize the number of mentions,
      3. among remaining ties, stable adjudication by (start, end, ordered
         candidate node sequences, ordered alias literals).
    Leftmost-longest is deliberately not used: on ``abcdef`` with aliases
    ``abcd``, ``abc`` and ``def`` this chooses ``abc``+``def`` (6 points)
    rather than ``abcd`` alone (4 points).

    Implementation is O(n log n): mentions are sorted by end, each mention
    finds its feasible predecessor range with binary search, and a prefix
    best array carries the winning score and reconstruction back-pointers.
    """
    from bisect import bisect_right

    def signature(mention):
        return (mention["start"], mention["end"],
                tuple(mention["candidates"]),
                tuple(a for c in mention["aliases"] for a in c))

    # Identical mentions are redundant; dedupe deterministically first.
    unique = {signature(m): m for m in mentions}
    if not unique:
        return []
    # A global rank gives tie-break #3 an exact, cheap encoding: the
    # lexicographic order of two candidate-signature sequences equals the
    # lexicographic order of their rank sequences.
    ordered = sorted(unique.values(), key=signature)
    n = len(ordered)
    end_order = sorted(range(n), key=lambda i: (ordered[i]["end"], i))
    sorted_ends = [ordered[i]["end"] for i in end_order]
    # prefix_best[pos] = (score, mention rank, predecessor prefix position)
    # for the best solution using only mentions end_order[0 .. pos]. The
    # score's third element is the rank sequence (bounded by max_mentions).
    prefix_best = [None] * n
    root = (0, 0, ())
    for pos, i in enumerate(end_order):
        mention = ordered[i]
        p = bisect_right(sorted_ends, mention["start"]) - 1
        base = prefix_best[p][0] if p >= 0 else root
        score = (base[0] + mention["end"] - mention["start"],
                 base[1] - 1, base[2] + (i,))
        if pos == 0 or score > prefix_best[pos - 1][0]:
            prefix_best[pos] = (score, i, p)
        else:
            prefix_best[pos] = prefix_best[pos - 1]
    chain, pos = [], n - 1
    while pos >= 0:
        _, i, p = prefix_best[pos]
        chain.append(ordered[i])
        if p < 0:
            break
        pos = p
    return list(reversed(chain))


class MentionAnnotator:
    """Streaming term-mention annotator bound to one frozen taxonomy version.

    The annotator is read-only with respect to the taxonomy service: it
    holds an :class:`AliasIndex` snapshot and never consults rule
    predicates, effective dates or the active-version pointer. Text may be
    streamed in chunks cut at any codepoint; chunks are never normalized
    independently and no final span is emitted while a later codepoint
    could still change the trailing grapheme cluster.
    """

    def __init__(self, document, *, strategy="aho",
                 max_cluster_codepoints=DEFAULT_MAX_CLUSTER_CODEPOINTS,
                 max_candidates_per_mention=DEFAULT_MAX_CANDIDATES_PER_MENTION,
                 max_mentions=DEFAULT_MAX_MENTIONS,
                 max_resident_text_codepoints=DEFAULT_MAX_RESIDENT_TEXT_CODEPOINTS,
                 max_staging_bytes=DEFAULT_MAX_STAGING_BYTES):
        index = AliasIndex(document)
        self._configure(index, strategy, {
            "max_cluster_codepoints": max_cluster_codepoints,
            "max_candidates_per_mention": max_candidates_per_mention,
            "max_mentions": max_mentions,
            "max_resident_text_codepoints": max_resident_text_codepoints,
            "max_staging_bytes": max_staging_bytes,
        })

    @classmethod
    def from_index(cls, index, *, strategy="aho",
                   max_cluster_codepoints=DEFAULT_MAX_CLUSTER_CODEPOINTS,
                   max_candidates_per_mention=DEFAULT_MAX_CANDIDATES_PER_MENTION,
                   max_mentions=DEFAULT_MAX_MENTIONS,
                   max_resident_text_codepoints=DEFAULT_MAX_RESIDENT_TEXT_CODEPOINTS,
                   max_staging_bytes=DEFAULT_MAX_STAGING_BYTES):
        """Bind an annotator to a pre-built, version-pinned AliasIndex."""
        annotator = cls.__new__(cls)
        annotator._configure(index, strategy, {
            "max_cluster_codepoints": max_cluster_codepoints,
            "max_candidates_per_mention": max_candidates_per_mention,
            "max_mentions": max_mentions,
            "max_resident_text_codepoints": max_resident_text_codepoints,
            "max_staging_bytes": max_staging_bytes,
        })
        return annotator

    def _configure(self, index, strategy, limits):
        if strategy not in ("aho", "scan"):
            raise ValueError("strategy must be 'aho' or 'scan'")
        self.index = index
        self.strategy = strategy
        self.automaton = AhoCorasick(self.index.keys) if strategy == "aho" else None
        self.limits = dict(limits)
        self._reset()

    def _reset(self):
        self._splitter = StreamingGraphemeSplitter()
        # Sliding window over finalized clusters; never the whole text.
        self._window = deque()          # (view form, original codepoint length)
        self._window_view_len = 0
        self._window_orig_len = 0
        self._view_pos = 0              # codepoints emitted into the view
        self._orig_pos = 0              # original codepoints finalized
        self._state = 0 if self.strategy == "aho" else None
        self._mentions = []
        self._probes = 0
        self._staging_bytes = 0
        self._failed = False
        self._total_codepoints = 0
        self._text_hasher = hashlib.sha256()

    # -- resource handling -------------------------------------------------

    def _stats(self):
        return {
            "resident_text_codepoints": self._splitter.pending_codepoints
            + self._window_orig_len,
            "pending_grapheme_codepoints": self._splitter.pending_codepoints,
            "window_graphemes": len(self._window),
            "mentions": len(self._mentions),
            "candidate_nodes_total": sum(
                len(m["candidates"]) for m in self._mentions),
            "staging_bytes": self._staging_bytes,
            "index_bytes": self.index.index_bytes,
            "automaton_bytes": self.automaton.memory_bytes if self.automaton else 0,
            "total_codepoints_seen": self._total_codepoints,
        }

    def _fail(self, reason):
        self._failed = True
        raise MentionBudgetError(reason, self._stats())

    def _check_text_residency(self):
        resident = self._splitter.pending_codepoints + self._window_orig_len
        if resident > self.limits["max_resident_text_codepoints"]:
            self._fail("resident text codepoint ceiling exceeded")

    # -- matching ----------------------------------------------------------

    def _record_hits(self, hit_keys):
        if not hit_keys:
            return
        end_orig = self._orig_pos
        end_view = self._view_pos
        for key in sorted(hit_keys):
            entry = self.index.entry(key)
            # Walk original clusters backwards until their concatenated
            # view spans exactly the key. A key that only matches a part of
            # an expanded cluster cannot be mapped to whole clusters and is
            # discarded (e.g. bare "f" cannot carve a span out of "ﬃ").
            acc_view, acc_orig = 0, 0
            for view, size in reversed(self._window):
                acc_view += len(view)
                acc_orig += size
                if acc_view >= len(key):
                    break
            # The automaton guarantees a suffix match; if the whole-cluster
            # alignment overshoots, the key lies inside one expanded cluster
            # and has no valid original span (bare "f" vs the "ﬃ" ligature).
            if acc_view != len(key):
                continue
            start_view = end_view - acc_view
            start_orig = end_orig - acc_orig
            candidates = entry["nodes"]
            if len(candidates) > self.limits["max_candidates_per_mention"]:
                self._fail("candidate explosion for one mention")
            mention = {
                "start": start_orig,
                "end": end_orig,
                "view_start": start_view,
                "view_end": end_view,
                "key": key,
                "aliases": entry["aliases"],
                "candidates": list(candidates),
                "ambiguous": len(set(candidates)) > 1,
            }
            if len(self._mentions) >= self.limits["max_mentions"]:
                self._fail("mention count ceiling exceeded")
            self._mentions.append(mention)
            self._staging_bytes += _deep_size(mention)
            if self._staging_bytes > self.limits["max_staging_bytes"]:
                self._fail("staging byte ceiling exceeded")

    def _push_cluster(self, cluster):
        if len(cluster) > self.limits["max_cluster_codepoints"]:
            self._fail("single grapheme cluster exceeds codepoint ceiling")
        view = cluster_retrieval_form(cluster)
        # Retain only clusters that the longest key could still reach.
        self._window.append((view, len(cluster)))
        self._window_view_len += len(view)
        self._window_orig_len += len(cluster)
        # Retain enough clusters that the longest key stays reconstructable;
        # keep the oldest cluster while dropping it would undercut that.
        while (len(self._window) > 1
               and self._window_view_len - len(self._window[0][0])
               >= self.index.max_key_view_len):
            dropped_view, dropped_orig = self._window.popleft()
            self._window_view_len -= len(dropped_view)
            self._window_orig_len -= dropped_orig
        self._view_pos += len(view)
        self._orig_pos += len(cluster)
        # The window now reflects the real retained residency: enforce the
        # declared ceiling here as well as between chunks.
        self._check_text_residency()
        window_view = "".join(v for v, _ in self._window)
        if self.strategy == "aho":
            hits = []
            for ch in view:
                self._state = self.automaton.step(self._state, ch)
            for key in self.automaton.output[self._state]:
                if len(key) <= self._window_view_len:
                    hits.append(key)
        else:
            hits = [key for key in reference_scan(
                window_view, self.index, [0])
                if len(key) <= self._window_view_len]
            self._probes += 1
        self._record_hits(hits)

    # -- public API --------------------------------------------------------

    def add(self, chunk):
        """Feed one text chunk; return mentions finalized by this chunk.

        Returned mentions are already immutable with respect to future
        chunks because their rightmost cluster is complete. Chunks may cut
        at any codepoint (base/combining mark, ligature, surrogate pair).
        """
        if self._failed:
            raise MentionError("annotator is unusable after a budget failure")
        if not isinstance(chunk, str):
            raise TypeError("text chunks must be strings")
        self._total_codepoints += len(chunk)
        self._text_hasher.update(chunk.encode("utf-8"))
        emitted = []
        for cluster in self._splitter.add(chunk):
            before = len(self._mentions)
            self._push_cluster(cluster)
            emitted.extend(self._public_mention(m) for m in self._mentions[before:])
            self._check_text_residency()
        self._check_text_residency()
        return emitted

    def finish(self):
        """Flush the trailing cluster and return the envelope."""
        if self._failed:
            raise MentionError("annotator is unusable after a budget failure")
        for cluster in self._splitter.finish():
            self._push_cluster(cluster)
        mentions = self._mentions
        non_overlapping = solve_non_overlapping(mentions)
        stats = self._stats()
        cost = {
            "strategy": self.strategy,
            "automaton_transitions": (
                self.automaton.transitions_taken if self.automaton else 0),
            "scan_probes": self._probes,
            "automaton_states": self.automaton.states if self.automaton else 0,
            "candidate_nodes_total": stats["candidate_nodes_total"],
            "mention_count": len(mentions),
        }
        text_summary = self._text_hasher.hexdigest()
        envelope = {
            "protocol": MATCH_PROTOCOL_VERSION,
            "unicode_version": PINNED_UNICODE_VERSION,
            "segmentation": SEGMENTATION_VERSION,
            "normalization": "per-extended-grapheme-cluster NFKC then casefold",
            "offset_semantics": "python-codepoint half-open offsets into the original text",
            "taxonomy_digest": self.index.document_digest,
            "dictionary_digest": self.index.digest,
            "text_summary": text_summary,
            "text_codepoints": self._total_codepoints,
            "aliases_indexed": self.index.alias_count,
            "keys_indexed": self.index.key_count,
            "mentions": [self._public_mention(m) for m in mentions],
            "non_overlapping_view": [
                self._public_mention(m) for m in non_overlapping],
            "limits": dict(self.limits),
            "stats": stats,
            "cost": cost,
        }
        self._reset()
        return envelope

    @staticmethod
    def _public_mention(mention):
        return {
            "start": mention["start"],
            "end": mention["end"],
            "aliases": list(mention["aliases"]),
            "candidates": list(mention["candidates"]),
            "ambiguous": mention["ambiguous"],
            "key": mention["key"],
        }


def annotate_mentions(document, chunks, **kwargs):
    """Convenience wrapper: annotate an iterable of string chunks at once."""
    annotator = MentionAnnotator(document, **kwargs)
    for chunk in chunks:
        annotator.add(chunk)
    return annotator.finish()
