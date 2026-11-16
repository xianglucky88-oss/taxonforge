"""Pinned UAX #29 (Unicode 14.0.0) grapheme segmentation tests."""
import unittest
from pathlib import Path

from taxonforge._grapheme import (
    SEGMENTATION_VERSION,
    StreamingGraphemeSplitter,
    UNIDATA_VERSION,
    gcb,
    grapheme_boundaries,
    grapheme_clusters,
    is_extended_pictographic,
)

DATA = Path(__file__).resolve().parents[1] / "data" / "unicode14" / "GraphemeBreakTest.txt"


def parse_official_cases():
    cases = []
    for line in DATA.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        tokens = line.replace("÷", " ÷ ").replace("×", " × ").split()
        expect, pos, chars = [], 0, []
        for tok in tokens:
            if tok in ("÷", "×"):
                if tok == "÷" and (not expect or expect[-1] != pos):
                    expect.append(pos)
            else:
                chars.append(chr(int(tok, 16)))
                pos += 1
        if expect[-1] != pos:
            expect.append(pos)
        cases.append(("".join(chars), expect))
    return cases


OFFICIAL_CASES = parse_official_cases()


class GraphemeSegmentationTests(unittest.TestCase):
    def test_pinned_version(self):
        self.assertEqual(UNIDATA_VERSION, "14.0.0")
        self.assertIn("UAX29", SEGMENTATION_VERSION)

    def test_official_break_test_oneshot(self):
        for text, expect in OFFICIAL_CASES:
            self.assertEqual(grapheme_boundaries(text), expect, text.encode("unicode_escape"))

    def test_official_break_test_streaming_schedules(self):
        schedules = [
            lambda i, n: 1,
            lambda i, n: (n - i) % 3 + 1,
            lambda i, n: (i * 7 + 2) % 5 + 1,
        ]
        for text, expect in OFFICIAL_CASES:
            for schedule in schedules:
                splitter = StreamingGraphemeSplitter()
                out, cut = [], 0
                while cut < len(text):
                    step = min(schedule(cut, len(text)), len(text) - cut)
                    out.extend(splitter.add(text[cut:cut + step]))
                    cut += step
                out.extend(splitter.finish())
                self.assertEqual("".join(out), text)
                bounds, total = [0], 0
                for cluster in out:
                    total += len(cluster)
                    bounds.append(total)
                self.assertEqual(bounds, expect, text.encode("unicode_escape"))

    def test_base_and_combining_mark_can_be_split_across_chunks(self):
        text = "e" + chr(0x0301)  # E + COMBINING ACUTE ACCENT, one cluster
        self.assertEqual(len(text), 2)
        self.assertEqual(grapheme_clusters(text), [text])
        splitter = StreamingGraphemeSplitter()
        self.assertEqual(splitter.add(text[:1]), [])
        self.assertEqual(splitter.add(text[1:]), [])
        self.assertEqual(splitter.finish(), [text])

    def test_hangul_jamo_composition(self):
        # L + V form an LV syllable (GB6/GB7); appending T makes LVT (GB8).
        lv = chr(0x1100) + chr(0x1161)
        lvt = lv + chr(0x11A8)
        self.assertEqual(grapheme_clusters(lv), [lv])
        self.assertEqual(grapheme_clusters(lvt), [lvt])
        splitter = StreamingGraphemeSplitter()
        self.assertEqual(splitter.add(lv[:1]), [])
        self.assertEqual(splitter.add(lv[1:]), [])
        self.assertEqual(splitter.add(chr(0x11A8)), [])
        self.assertEqual(splitter.finish(), [lvt])
        # GB8 keeps LVT x T together as well; a following vowel breaks.
        self.assertEqual(
            [len(c) for c in grapheme_clusters(lvt + chr(0x1161))], [3, 1])

    def test_regional_indicator_pairs_and_odd_tail(self):
        pair = chr(0x1F1E6) + chr(0x1F1E7)
        self.assertEqual(grapheme_clusters(pair), [pair])
        triple = pair + chr(0x1F1E8)
        self.assertEqual([len(c) for c in grapheme_clusters(triple)], [2, 1])

    def test_zwj_emoji_sequence(self):
        sequence = chr(0x1F468) + chr(0x200D) + chr(0x1F469) + chr(0x200D) + chr(0x1F467)
        self.assertEqual(grapheme_clusters(sequence), [sequence])
        # A ZWJ delivered in a later chunk cannot flush the cluster early.
        head, tail = sequence[:-1], sequence[-1]
        splitter = StreamingGraphemeSplitter()
        self.assertEqual(splitter.add(head), [])
        self.assertEqual(splitter.add(tail), [])
        self.assertEqual(splitter.finish(), [sequence])
        # And the full sequence stays one cluster even codepoint by codepoint.
        splitter = StreamingGraphemeSplitter()
        emitted = []
        for ch in sequence:
            emitted.extend(splitter.add(ch))
        self.assertEqual(emitted, [])
        self.assertEqual(splitter.finish(), [sequence])

    def test_cr_lf_pair(self):
        self.assertEqual(grapheme_clusters("\r\n"), ["\r\n"])
        splitter = StreamingGraphemeSplitter()
        self.assertEqual(splitter.add("\r"), [])
        self.assertEqual(splitter.add("\n"), [])
        self.assertEqual(splitter.finish(), ["\r\n"])

    def test_property_lookups(self):
        self.assertEqual(gcb(ord("a")), "Other")
        self.assertEqual(gcb(0x0301), "Extend")
        self.assertTrue(is_extended_pictographic(ord("😀")))
        self.assertFalse(is_extended_pictographic(ord("a")))

    def test_empty_and_type_guards(self):
        splitter = StreamingGraphemeSplitter()
        self.assertEqual(splitter.add(""), [])
        self.assertEqual(splitter.finish(), [])
        self.assertEqual(grapheme_clusters(""), [])
        with self.assertRaises(TypeError):
            grapheme_clusters(b"abc")
        with self.assertRaises(TypeError):
            splitter.add(123)


if __name__ == "__main__":
    unittest.main()
