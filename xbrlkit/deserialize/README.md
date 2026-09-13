# Deserialize — the importers

The arrows run both ways. XBRL is the source in every other path through this
package; these read a **TAVI compiled model**, a **holon** or the **property
graph's rows** back into [`XbrlModel`](../model.py), after which every tool in
the package works over it unchanged.

```python
from xbrlkit.deserialize import from_tavi_json, from_holon_json, from_graph, read_lbug

model = from_tavi_json(Path("boeing.tavi.json").read_text())
model = from_holon_json(Path("boeing.holon.jsonld").read_text())
model = from_graph(read_lbug(Path("boeing.lbug")))     # or rows from any graph
```

Arelle is not involved and cannot be: it does not load any of them. That is
the point — a report that never was an SEC filing (a ledger's own output, a
converted filing someone handed you, a filing sitting in a graph) becomes
queryable with the same tools, and [`xbrlkit serve`](../serve/README.md) loads
a TAVI or a holon by path or by URL and a `.lbug` by path.

`from_tavi_report` / `from_holon_report` / `from_graph_report` return the model
**and** a gap report, so a round trip can be diffed without re-deriving the
list by hand.

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

| | TAVI | holon | property graph |
| --- | --- | --- | --- |
| loses | the definition networks (they become cube objects), `is_hypercube_item`, the abstractness of axes and members, `decimals="INF"`, and the case of a language tag (the emitter lower-cases it, as xBRL-JSON requires) | an element no fact, network or dimension mentions; the reference linkbase; a fact's source hash | an arc's `targetRole` (no column for it — a cube declared across roles reads as its own role's arcs); fact language; the processed `value_str` (the raw value is read as both); `is_text_fact` off a text block; the acceptance *time*; the QName prefix of a type outside the filing's own namespaces |
| keeps | every label role, the datatype detail, the filing's namespaces | everything else — see [`serialize/`](../serialize/README.md#the-holon) | everything else, the inline-XBRL flag included — the one thing the other two cannot say |
| facts | one per reported fact | one per **distinct** fact when the parse gave duplicates the same content-derived id | one per distinct source hash, as the projection wrote them |

TAVI's losses are the standard's; the holon's were ours, and closing them is
what the importers were good for. The graph's one real loss, `targetRole`, is a
column the schema does not have yet.

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

## Reading the graph

The graph is the projection [`serialize/lpg.py`](../serialize/lpg.py) writes —
the node and relationship tables of [`schema/`](../schema/__init__.py), which
the RoboSystems `sec` graph is built from and a ledger's own report
materializes into. The reader takes **rows, not a database**: `from_graph`
consumes the `GraphTables` the projection emits, and a caller that holds the
graph elsewhere runs `SLICE_QUERIES` (one Cypher statement per table, by
`$report`) against it and hands the rows to `tables_from_slice`. `read_lbug`
does exactly that for a single-filing `.lbug`; the RoboSystems platform does
it over its query API, so its hosted `disclosures` / `information-block` tools
run xbrlkit's own block rules on the graph rather than a second copy of them.

Two producers fill these tables and the reader keys on the columns, not on
either one's habits. A filing's structure has a role URI and reads as the
filing's; a ledger's has none, and reads as the producer's — `structure_id`,
`block_type` and `fact_set_id` set, the way an authored holon reads — with its
name where the definition would be, an element's name as its label when the
producer wrote no labels, and the platform's own association kinds (a chart's
mapping, a trait's general-special tree) counted in the gap report rather than
read as networks.

## The gate

Three round trips on the 27-filing corpus of 2024–2025 10-Ks and 10-Qs the
[Filing Ladder](https://github.com/HarbingerFinLab/filing-ladder) is built on:

1. **model → holon → model** is identical on all 26, across facts, labels,
   networks, concept fields, units and periods.
2. Reading each filing from its **TAVI** and from its **holon** renders all
   **2,915 presentation networks identically** — same rows, same order, same
   labels — with the same calculation networks and the same `fact_grid`
   answers.
3. Reading each filing from its **`.lbug`** and from its **holon** gives the
   same concepts, the same facts, the same presentation, calculation and
   definition arcs, and the same information blocks — same axes, cubes and
   footing on every role — and reading the graph's own re-projection is a
   fixed point (`tests/test_deserialize_graph.py`, with `XBRLKIT_CORPUS` set
   to the ladder's data directory).

## Still refused

**xBRL-JSON.** That one is Arelle's own serialization and wants its OIM loader,
not an importer of ours — hours of work, not a lane. `xbrlkit serve` names it
rather than half-reading it.
