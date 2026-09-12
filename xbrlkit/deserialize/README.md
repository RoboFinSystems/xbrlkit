# Deserialize — the importers

The arrows run both ways. XBRL is the source in every other path through this
package; these read a **ClawDog report**, a **TAVI compiled model** or a **holon** back into
[`XbrlModel`](../model.py), after which every tool in the package works over it
unchanged.

```python
from xbrlkit.deserialize import from_clawdog_json, from_tavi_json, from_holon_json

model = from_clawdog_json(Path("report.clawdog.jsonld").read_text())
model = from_tavi_json(Path("boeing.tavi.json").read_text())
model = from_holon_json(Path("boeing.holon.jsonld").read_text())
```

Arelle is not involved and cannot be: it does not load either format. That is
the point — a report that never was an SEC filing (a ledger's own output, a
converted filing someone handed you) becomes queryable with the same tools, and
[`xbrlkit serve`](../serve/README.md) loads either one by path or by URL.

`from_clawdog_report`, `from_tavi_report` and `from_holon_report` return the
model **and** a gap report, so a round trip can be diffed without re-deriving
the list by hand.

## The rule

**Read what the serialization carries and nothing else.** Where a projection
dropped something the field stays empty and the gap is named, rather than
filled with a plausible value — a filled-in field is indistinguishable from a
read one and would make the round trip look lossless when it is not.

The single exception is the derived period enrichment (the duration bucket, the
calendar placement), recomputed from the dates by [`xbrlkit.periods`](../periods.py),
the module the parse itself uses. That is what lets an imported period match a
parsed one **id for id** rather than merely resembling it.

## What each format loses

| | ClawDog | TAVI | holon |
| --- | --- | --- | --- |
| loses | filing-parser fidelity fields that do not belong to authored reports (`source_hash`, `raw_value`) and records them in gaps | the definition networks (they become cube objects), `is_hypercube_item`, the abstractness of axes and members, `decimals="INF"`, and the case of a language tag (the emitter lower-cases it, as xBRL-JSON requires) | an element no fact, network or dimension mentions; the reference linkbase; a fact's source hash |
| keeps | entity, periods, units, concepts, facts, dimensions, networks, fact provenance and calculation equations | every label role, the datatype detail, the filing's namespaces | everything else — see [`serialize/`](../serialize/README.md#the-holon) |
| facts | one per producer fact id | one per reported fact | one per **distinct** fact when the parse gave duplicates the same content-derived id |

TAVI's losses are the standard's; the holon's were ours, and closing them is
what the importers were good for.

## Reading the holon

It is read **structurally**, not through rdflib, for one reason that matters: a
holon binds each prefix to the namespace the filing declared, so expanding an
element IRI and contracting it again is what would lose the QName — the model's
key for everything. An absolute IRI is still accepted and reversed through the
document's own `@context`, so a holon written expanded reads the same.

Two things the importer recovers that are not obvious:

- **A presentation association carries the preferred label it resolved to** —
  text and role — so those are hung back on the concept. Without it a holon
  written before the label terms existed renders a statement under standard
  labels the filer did not choose (40 of 46 rows on Boeing's balance sheet).
- **A fact carrying a language is a text fact.** Neither the declared type nor
  the value domain can answer it — `dei:centralIndexKeyItemType` and
  `dei:yesNoItemType` both derive from token and only one takes a language — so
  the emitters' own signal settles it, on both formats.

## The gate

Two round trips on the 26-filing corpus of 2024–2025 10-Ks and 10-Qs the
[Filing Ladder](https://github.com/HarbingerFinLab/filing-ladder) is built on:

1. **model → holon → model** is identical on all 26, across facts, labels,
   networks, concept fields, units and periods.
2. Reading each filing from its **TAVI** and from its **holon** renders all
   **2,915 presentation networks identically** — same rows, same order, same
   labels — with the same calculation networks and the same `fact_grid`
   answers.

## Still refused

**xBRL-JSON.** That one is Arelle's own serialization and wants its OIM loader,
not an importer of ours — hours of work, not a lane. `xbrlkit serve` names it
rather than half-reading it.
