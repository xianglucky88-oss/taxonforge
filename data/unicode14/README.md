# Unicode 14.0.0 source data (untrusted external input)

These three text files are downloaded verbatim from the official Unicode
UCD 14.0.0 and are read **only** by `tools/generate_ucd14.py`, never at
runtime. The generator freezes their property tables into
`src/taxonforge/_ucd14_data.py`; application code imports that module.

Sources:

- https://www.unicode.org/Public/14.0.0/ucd/auxiliary/GraphemeBreakProperty.txt
- https://www.unicode.org/Public/14.0.0/ucd/auxiliary/GraphemeBreakTest.txt
- https://www.unicode.org/Public/14.0.0/ucd/emoji/emoji-data.txt

Regenerate with: `python3 -I tools/generate_ucd14.py`

The matching protocol additionally depends on the interpreter's
`unicodedata` (NFKC + casefold) being Unicode 14.0.0; `MentionAnnotator`
refuses to start otherwise, so segmentation and normalization versions
cannot silently drift apart.
