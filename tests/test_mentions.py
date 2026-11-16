"""Term-mention annotation: pipeline, mapping, overlap and budgets."""
import copy
import itertools
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from taxonforge import (
    AliasIndex,
    MentionAnnotator,
    MentionBudgetError,
    annotate_mentions,
    retrieval_key,
    solve_non_overlapping,
)
from taxonforge._grapheme import grapheme_clusters
from taxonforge.mentions import PINNED_UNICODE_VERSION
from taxonforge.service import TaxonomyService


def doc_with(aliases_by_node):
    return {
        "name": "mention-test",
        "categories": [
            {"code": code, "aliases": list(aliases)}
            for code, aliases in aliases_by_node.items()
        ],
    }


# Independent brute-force reference: enumerate grapheme-aligned spans and
# compare their retrieval keys directly against the index.
def brute_force_mentions(text, index):
    clusters = grapheme_clusters(text)
    boundaries, pos = [0], 0
    for cluster in clusters:
        pos += len(cluster)
        boundaries.append(pos)
    forms = [retrieval_key(c) for c in clusters]
    results = []
    for i in range(len(clusters)):
        view = ""
        for j in range(i, len(clusters)):
            view += forms[j]
            entry = index.entry(view)
            if entry is not None:
                results.append({
                    "start": boundaries[i],
                    "end": boundaries[j + 1],
                    "key": view,
                    "aliases": list(entry["aliases"]),
                    "candidates": list(entry["nodes"]),
                    "ambiguous": len(set(entry["nodes"])) > 1,
                })
    return sorted(results, key=lambda m: (m["start"], m["end"], m["candidates"]))


def public_set(envelope):
    return sorted(
        ({k: m[k] for k in ("start", "end", "key", "aliases", "candidates", "ambiguous")}
         for m in envelope["mentions"]),
        key=lambda m: (m["start"], m["end"], m["candidates"]))


class RetrievalPipelineTests(unittest.TestCase):
    def setUp(self):
        self.doc = doc_with({
            "acute": ["é"],
            "lig": ["ffi"],
            "eff": ["f"],
            "overlap": ["abcd", "abc"],
            "defnode": ["def"],
        })
        self.index = AliasIndex(self.doc)

    def test_dictionary_e_matches_decomposed_e_acute_span(self):
        text = "e" + "́"  # LATIN SMALL LETTER E + COMBINING ACUTE ACCENT
        self.assertEqual(len(text), 2)
        envelope = annotate_mentions(self.doc, [text])
        spans = [(m["start"], m["end"]) for m in envelope["mentions"]]
        self.assertIn((0, 2), spans)
        hit = next(m for m in envelope["mentions"] if m["start"] == 0 and m["end"] == 2)
        self.assertEqual(hit["candidates"], ["acute"])

    def test_ffi_alias_matches_single_ligature_cluster(self):
        text = "ﬃ"
        envelope = annotate_mentions(self.doc, [text])
        spans = [(m["start"], m["end"], m["candidates"]) for m in envelope["mentions"]]
        self.assertIn((0, 1, ["lig"]), spans)
        # A bare "f" entry must never carve a pseudo-span out of the ligature.
        self.assertFalse(any(m["candidates"] == ["eff"] for m in envelope["mentions"]))

    def test_bare_f_still_matches_real_f_text(self):
        envelope = annotate_mentions(self.doc, ["ffi"])
        self.assertTrue(any(m["candidates"] == ["eff"] for m in envelope["mentions"]))
        lig = next(m for m in envelope["mentions"] if m["candidates"] == ["lig"])
        self.assertEqual((lig["start"], lig["end"]), (0, 3))

    def test_brute_force_agreement_on_mixed_text(self):
        samples = [
            "abcdef", "ﬃabc", "éﬃx", "CAFÉ café", "abﬃdef",
            "é ﬃ abcdef ﬃ", "É", "ﬀﬁﬂ", "",
        ]
        for text in samples:
            with self.subTest(text=text.encode("unicode_escape")):
                envelope = annotate_mentions(self.doc, [text])
                self.assertEqual(public_set(envelope), brute_force_mentions(text, self.index))

    def test_all_spans_land_on_whole_grapheme_boundaries(self):
        text = "aé ﬃ bc"
        boundaries = set()
        total = 0
        boundaries.add(0)
        for cluster in grapheme_clusters(text):
            total += len(cluster)
            boundaries.add(total)
        envelope = annotate_mentions(self.doc, [text])
        for mention in envelope["mentions"]:
            self.assertIn(mention["start"], boundaries)
            self.assertIn(mention["end"], boundaries)
            self.assertEqual(text[mention["start"]:mention["end"]],
                             text[mention["start"]:mention["end"]])

    def test_nfkc_casefold_pipeline_order(self):
        self.assertEqual(retrieval_key("CAFÉ"), "café")
        # Precomposed and decomposed originals share a retrieval key.
        self.assertEqual(retrieval_key("é"), retrieval_key("é"))


class OverlapSolverTests(unittest.TestCase):
    def test_abcdef_prefers_max_coverage_not_leftmost_longest(self):
        envelope = annotate_mentions(
            doc_with({"n": ["abcd", "abc", "def"]}), ["abcdef"])
        view = [(m["start"], m["end"], m["aliases"]) for m in envelope["non_overlapping_view"]]
        self.assertEqual(view, [(0, 3, ["abc"]), (3, 6, ["def"])])
        # The full candidate set still contains the overlapping abcd mention.
        all_spans = {(m["start"], m["end"]) for m in envelope["mentions"]}
        self.assertEqual(all_spans, {(0, 3), (0, 4), (3, 6)})

    def test_tie_break_min_mention_count_then_stable_order(self):
        # Two equal-coverage options: one long mention vs. two short ones.
        mentions = [
            {"start": 0, "end": 4, "candidates": ["z"], "aliases": [("z",)]},
            {"start": 0, "end": 2, "candidates": ["a"], "aliases": [("a",)]},
            {"start": 2, "end": 4, "candidates": ["b"], "aliases": [("b",)]},
        ]
        view = solve_non_overlapping(mentions)
        self.assertEqual([(m["start"], m["end"]) for m in view], [(0, 4)])
        # Stable adjudication when coverage and count both tie.
        left = {"start": 0, "end": 2, "candidates": ["a"], "aliases": [("a",)]}
        right = {"start": 2, "end": 4, "candidates": ["b"], "aliases": [("b",)]}
        view = solve_non_overlapping([right, left])
        self.assertEqual([m["candidates"] for m in view], [["a"], ["b"]])

    def test_non_overlapping_view_never_overlaps(self):
        envelope = annotate_mentions(
            doc_with({"n": ["a", "ab", "bc", "c", "abc"]}), ["abcabc"])
        view = envelope["non_overlapping_view"]
        for earlier, later in zip(view, view[1:]):
            self.assertLessEqual(earlier["end"], later["start"])


class AmbiguityTests(unittest.TestCase):
    def test_normalized_alias_collision_merges_ordered_candidates(self):
        doc = doc_with({"node-b": ["CAFÉ"], "node-a": ["café"]})
        envelope = annotate_mentions(doc, ["café"])
        self.assertEqual(len(envelope["mentions"]), 1)
        mention = envelope["mentions"][0]
        self.assertTrue(mention["ambiguous"])
        self.assertEqual(mention["candidates"], ["node-a", "node-b"])
        self.assertEqual(mention["aliases"], ["CAFÉ", "café"])

    def test_collision_is_independent_of_dictionary_order(self):
        docs = [
            doc_with({"node-b": ["CAFÉ"], "node-a": ["café"]}),
            doc_with({"node-a": ["café"], "node-b": ["CAFÉ"]}),
        ]
        envelopes = [annotate_mentions(doc, ["café"]) for doc in docs]
        self.assertEqual(envelopes[0]["mentions"], envelopes[1]["mentions"])

    def test_multiple_literals_same_node_deduplicate_candidate(self):
        doc = doc_with({"solo": ["CAFÉ", "café"]})
        envelope = annotate_mentions(doc, ["café"])
        mention = envelope["mentions"][0]
        self.assertEqual(mention["candidates"], ["solo"])
        self.assertFalse(mention["ambiguous"])


class ChunkInvarianceTests(unittest.TestCase):
    def setUp(self):
        self.doc = doc_with({
            "acute": ["é"], "lig": ["ffi"], "eff": ["f"],
            "overlap": ["abcd", "abc"], "defnode": ["def"], "cafe": ["café"],
        })

    def _reference(self, text):
        return annotate_mentions(self.doc, [text])

    def test_every_two_piece_split_matches_one_shot(self):
        text = "z é ﬃ abcdef café abcd"
        reference = self._reference(text)
        for k in range(1, len(text)):
            with self.subTest(cut=k):
                got = annotate_mentions(self.doc, [text[:k], text[k:]])
                self.assertEqual(got["mentions"], reference["mentions"])
                self.assertEqual(got["non_overlapping_view"], reference["non_overlapping_view"])
                self.assertEqual(got["text_summary"], reference["text_summary"])

    def test_single_codepoint_and_large_chunk_schedules(self):
        text = "ﬃ é abc".join(["q"] * 4)
        reference = self._reference(text)
        per_char = MentionAnnotator(self.doc)
        for ch in text:
            per_char.add(ch)
        self.assertEqual(per_char.finish()["mentions"], reference["mentions"])
        whole = MentionAnnotator(self.doc)
        whole.add(text)
        self.assertEqual(whole.finish()["mentions"], reference["mentions"])

    def test_combining_mark_reordering_and_hangul(self):
        # Multiple combining marks delivered one per chunk still form one
        # cluster; Hangul jamo in separate chunks stay one syllable cluster.
        text = "éᄀ가"
        reference = self._reference(text)
        for k in range(1, len(text)):
            got = annotate_mentions(self.doc, [text[:k], text[k:]])
            self.assertEqual(got["mentions"], reference["mentions"])

    def test_incremental_output_is_prefix_stable(self):
        text = "abcdefﬃ"
        annotator = MentionAnnotator(self.doc)
        collected = []
        for ch in text:
            collected.extend(annotator.add(ch))
        envelope = annotator.finish()
        # Public mention payloads streamed early must equal the final ones
        # (internal bookkeeping fields are not part of the contract).
        def public(mention):
            return {k: mention[k] for k in
                    ("start", "end", "aliases", "candidates", "ambiguous", "key")}
        streamed = [public(m) for m in collected]
        early_final = [public(m) for m in envelope["mentions"] if m["end"] < len(text)]
        self.assertEqual(streamed, early_final)
        # And the streamed payload schema matches the final public schema.
        self.assertEqual(set(collected[0]), set(envelope["mentions"][0]))
        for mention in collected:
            self.assertLessEqual(mention["end"], len(text))


class StrategyParityAndCostTests(unittest.TestCase):
    def test_scan_and_aho_return_identical_mentions(self):
        doc = doc_with({"n": ["abc", "abcd", "def", "f", "é", "ffi"]})
        text = "abcdef ﬃ é xyz abc"
        aho = annotate_mentions(doc, [text], strategy="aho")
        scan = annotate_mentions(doc, [text], strategy="scan")
        self.assertEqual(aho["mentions"], scan["mentions"])
        self.assertEqual(aho["non_overlapping_view"], scan["non_overlapping_view"])
        self.assertGreater(scan["cost"]["scan_probes"], 0)
        self.assertGreater(aho["cost"]["automaton_transitions"], 0)
        # The automaton performs one transition per view codepoint; the
        # scanner probes every key per cluster, so cost scales differently.
        self.assertLessEqual(aho["cost"]["automaton_transitions"],
                             scan["cost"]["scan_probes"] * max(len(k) for k in AliasIndex(doc).keys))

    def test_index_memory_reported_separately(self):
        doc = doc_with({"n": [f"term{i}" for i in range(20)]})
        index = AliasIndex(doc)
        envelope = annotate_mentions(doc, ["term0 term19"])
        self.assertGreater(index.index_bytes, 0)
        self.assertEqual(envelope["stats"]["index_bytes"], index.index_bytes)
        self.assertGreater(envelope["stats"]["automaton_bytes"], 0)


class BudgetTests(unittest.TestCase):
    def test_oversized_single_cluster_is_rejected(self):
        doc = doc_with({"n": ["x"]})
        text = "a" + "̀" * 10
        annotator = MentionAnnotator(doc, max_cluster_codepoints=5)
        with self.assertRaises(MentionBudgetError) as ctx:
            annotator.add(text)
            annotator.finish()
        self.assertIn("cluster", ctx.exception.reason)
        self.assertIn("resident_text_codepoints", ctx.exception.stats)

    def test_residency_ceiling_still_allows_long_text_within_other_budgets(self):
        doc = doc_with({"n": ["z"]})
        annotator = MentionAnnotator(
            doc, max_resident_text_codepoints=4, max_mentions=10_000,
            max_staging_bytes=10_000_000)
        # Spaces always break clusters, so the retained state stays tiny
        # even though total text far exceeds the residency ceiling.
        annotator.add("z z z z z z z z z z z z")
        envelope = annotator.finish()
        self.assertEqual(envelope["stats"]["total_codepoints_seen"], 23)
        self.assertLessEqual(envelope["stats"]["resident_text_codepoints"], 4)

    def test_residency_ceiling_rejects_unbreakable_run(self):
        # A long key forces the sliding window to retain many clusters; the
        # declared residency ceiling must then be enforced explicitly.
        doc = doc_with({"n": ["abcdefghij"]})
        annotator = MentionAnnotator(doc, max_resident_text_codepoints=3)
        with self.assertRaises(MentionBudgetError):
            annotator.add("abcdefghij")
            annotator.finish()

    def test_candidate_explosion_rejected_not_truncated(self):
        # Twenty distinct exact literals (satisfying Taxonomy's exact-alias
        # uniqueness) whose NFKC+casefold key is all "a", pointing at twenty
        # distinct nodes: one mention with an exploding candidate set.
        folding_to_a = [
            cp for cp in range(0x110000)
            if __import__("unicodedata").normalize("NFKC", chr(cp)).casefold() == "a"]
        self.assertGreaterEqual(len(folding_to_a), 20)
        doc = doc_with({
            f"node{i:02d}": [chr(folding_to_a[i])] for i in range(20)})
        annotator = MentionAnnotator(doc, max_candidates_per_mention=10)
        with self.assertRaises(MentionBudgetError) as ctx:
            annotator.add("a")
            annotator.finish()
        self.assertIn("candidate", ctx.exception.reason)
        # The annotator refuses further use after failure rather than
        # silently continuing with partial state.
        with self.assertRaises(Exception):
            annotator.add("more")

    def test_mention_count_and_staging_byte_ceilings(self):
        doc = doc_with({"n": ["a"]})
        annotator = MentionAnnotator(doc, max_mentions=3)
        with self.assertRaises(MentionBudgetError):
            annotator.add("a a a a a")
            annotator.finish()
        annotator = MentionAnnotator(doc, max_staging_bytes=200)
        with self.assertRaises(MentionBudgetError):
            annotator.add("a " * 200)
            annotator.finish()

    def test_budget_failure_does_not_mutate_taxonomy_store(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        service = TaxonomyService(Path(tmp.name) / "t.sqlite")
        self.addCleanup(service.close)
        document = {
            "name": "v1",
            "categories": [{"code": "root"}, {"code": "n", "parent": "root", "aliases": ["a"]}],
        }
        service.register(document)
        annotator = MentionAnnotator(document, max_mentions=1)
        with self.assertRaises(MentionBudgetError):
            annotator.add("a a a")
            annotator.finish()
        self.assertEqual(service.db.execute("SELECT count(*) FROM runs").fetchone()[0], 0)


class EnvelopeAndServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.service = TaxonomyService(Path(self.tmp.name) / "t.sqlite")
        self.addCleanup(self.service.close)
        self.document = {
            "name": "v1",
            "categories": [
                {"code": "root"},
                {"code": "n1", "parent": "root", "aliases": ["café"]},
                {"code": "n2", "parent": "root", "aliases": ["CAFÉ"]},
            ],
            "rules": [],
        }
        self.digest = self.service.register(self.document)

    def test_envelope_binds_versions_and_summaries(self):
        envelope = self.service.annotate_mentions(["café"], version=self.digest)
        self.assertEqual(envelope["protocol"], "tf-mentions/1.0")
        self.assertEqual(envelope["unicode_version"], PINNED_UNICODE_VERSION)
        self.assertIn("UAX29", envelope["segmentation"])
        self.assertEqual(envelope["taxonomy_digest"], self.digest)
        self.assertTrue(envelope["dictionary_digest"])
        self.assertEqual(len(envelope["text_summary"]), 64)
        self.assertIn("codepoint", envelope["offset_semantics"])

    def test_active_switch_does_not_change_bound_version_candidates(self):
        document2 = copy.deepcopy(self.document)
        document2["name"] = "v2"
        document2["categories"][1]["aliases"] = ["different"]
        digest2 = self.service.register(document2)
        self.service.activate(digest2)
        envelope = self.service.annotate_mentions(["café"], version=self.digest)
        self.assertEqual(envelope["taxonomy_digest"], self.digest)
        self.assertEqual(envelope["mentions"][0]["candidates"], ["n1", "n2"])
        # An explicit unknown version is rejected rather than silently falling back.
        with self.assertRaises(KeyError):
            self.service.annotate_mentions(["x"], version="deadbeef")

    def test_annotation_is_read_only_and_not_a_classification(self):
        before = self.service.db.execute("SELECT count(*) FROM runs").fetchone()[0]
        envelope = self.service.annotate_mentions(["café"])
        after = self.service.db.execute("SELECT count(*) FROM runs").fetchone()[0]
        self.assertEqual(before, after)
        for mention in envelope["mentions"]:
            self.assertNotIn("category", mention)
            self.assertNotIn("status", mention)
            self.assertIsInstance(mention["candidates"], list)

    def test_exact_taxonomy_alias_resolution_is_unchanged(self):
        taxonomy = self.service.load(self.digest)
        # Exact, normalization-free mapping still distinguishes the literals.
        self.assertEqual(taxonomy.aliases["café"], "n1")
        self.assertEqual(taxonomy.aliases["CAFÉ"], "n2")
        # The mention pipeline merges them under one folded retrieval key,
        # but Taxonomy.aliases keeps both exact spellings without folding.
        index = MentionAnnotator(self.document).index
        folded = retrieval_key("CAFÉ")
        # Node codes are indexed as aliases too, matching Taxonomy semantics;
        # the two text literals still collapse to one folded key.
        self.assertIn(folded, index.keys)
        self.assertEqual(index.entry(folded)["nodes"], ("n1", "n2"))
        self.assertEqual(index.entry("n1")["nodes"], ("n1",))
        # Taxonomy.aliases keeps both exact spellings plus node codes, with
        # no folded synthetic key inserted by the mention pipeline.
        self.assertEqual(
            sorted(taxonomy.aliases), ["CAFÉ", "café", "n1", "n2", "root"])

    def test_reusable_annotator_resets_between_documents(self):
        annotator = MentionAnnotator(self.document)
        first = annotator.finish()
        annotator.add("café")
        second = annotator.finish()
        self.assertEqual(first["mentions"], [])
        self.assertEqual(len(second["mentions"]), 1)
        self.assertEqual(second["text_codepoints"], 4)


class PinnedVersionTests(unittest.TestCase):
    def test_unicode_version_guard_rejects_other_runtime(self):
        import unicodedata
        from taxonforge.mentions import (
            MentionError, PINNED_UNICODE_VERSION, _require_pinned_unicode)
        self.assertEqual(PINNED_UNICODE_VERSION, "14.0.0")
        original = unicodedata.unidata_version
        unicodedata.unidata_version = "13.0.0"
        try:
            with self.assertRaises(MentionError):
                _require_pinned_unicode()
        finally:
            unicodedata.unidata_version = original
        # Restoring the pinned version leaves the guard satisfied.
        _require_pinned_unicode()


class CliAnnotationTests(unittest.TestCase):
    def test_annotate_subcommand_chunk_equivalence_and_read_only(self):
        project = Path(__file__).resolve().parents[1]
        env = dict(__import__("os").environ,
                   PYTHONPATH=str(project / "src"))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "t.sqlite"
            doc = root / "doc.json"
            doc.write_text(json.dumps({
                "name": "cli-v1",
                "categories": [
                    {"code": "root"},
                    {"code": "acute", "parent": "root", "aliases": ["é"]},
                    {"code": "lig", "parent": "root", "aliases": ["ffi", "f"]},
                ]}), encoding="utf-8")
            text = root / "text.txt"
            text.write_text("éﬃ f", encoding="utf-8")

            def run(*extra):
                out = subprocess.run(
                    [sys.executable, "-m", "taxonforge.cli", "--db", str(db),
                     "annotate", str(text), *extra],
                    env=env, check=True, capture_output=True, text=True)
                return json.loads(out.stdout)

            subprocess.run(
                [sys.executable, "-m", "taxonforge.cli", "--db", str(db),
                 "register", str(doc)], env=env, check=True, capture_output=True)
            whole = run()
            chunked = run("--chunk-codepoints", "1", "--strategy", "scan")
            self.assertEqual(whole["mentions"], chunked["mentions"])
            self.assertEqual(whole["non_overlapping_view"],
                             chunked["non_overlapping_view"])
            self.assertEqual(whole["text_summary"], chunked["text_summary"])
            # Annotation writes no classification run.
            import sqlite3
            with sqlite3.connect(db) as conn:
                self.assertEqual(
                    conn.execute("SELECT count(*) FROM runs").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
