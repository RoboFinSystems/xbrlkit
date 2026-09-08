# Text — the filing as prose

`xbrlkit.text` reads a filing's primary HTML document — **no Arelle, no
network** — and returns its text as sections. It is the surface that does not
hang off the model: the narrative of a filing is in the document, not in the
XBRL.

| Parser | Sections | Notes |
| --- | --- | --- |
| `iXBRLParser` | every inline-XBRL text block (notes, policies, tables), with the XBRL element names it contains | `ix:continuation` chains resolved; nested continuations and nested text blocks included; a concept tagged more than once is one section holding every occurrence; `ix:exclude` page furniture dropped |
| `NarrativeExtractor` | the 10-K / 10-Q Items — Business, Risk Factors, Cybersecurity, Properties, MD&A, Market Risk | table-of-contents rows and cross-references rejected; a 10-Q's Part I and Part II Items kept apart |
| `xml.py` | the SEC forms that are XML rather than HTML — ownership, 13F, N-PORT — as fields and record tables | generic: no per-form parsers, so a form nobody anticipated still reads |

```python
from xbrlkit.text import iXBRLParser, NarrativeExtractor

html = open("mmm-20241231.htm").read()
for s in iXBRLParser().parse(html):
  print(s.section_id, s.label, s.word_count, s.xbrl_elements[:3])
for s in NarrativeExtractor().extract(html, form_type="10-K"):
  print(s.section_id, s.label, s.word_count)
```

Both render HTML tables as markdown pipe tables and split a long section into
balanced parts at paragraph boundaries (`part`, `part_count`, and a `label`
like `"MD&A (2/6)"`) instead of truncating it.

## What was measured

On the 26-filing corpus of 2024–2025 10-Ks and 10-Qs: **every text block's full
text is carried**, where a map of outermost continuations alone lost 15–29% of
the note text on nine of the filings, and **every target Item starts at its
body heading**.

Two defects those checks found were fixed in 0.4.1 and are disclosed in the
Filing Ladder's protocol.

## Locating a block inside a document

A classic filing's tagged blocks live in the instance and its prose lives in a
sibling document, so the blocks are located in the text by matching their first
twelve letters-only words with bounded, tempered gaps. Two things break a naive
match and are handled: the two HTML strippers disagree about a curly
apostrophe, and a table-of-contents row carries the heading but not what
follows it. A block whose text begins with a table is cut at the first markdown
row before matching — which took one filing from 50 of 58 blocks located to all
58, without loosening the match.
