# Serialize — the projections

Four portable representations, all hanging off one parse. Fidelity loss is a
*projection* choice, never a limitation of [`XbrlModel`](../model.py): the
parse captures the full XBRL and each serializer decides what to shed.

| Target | Status | Notes |
| --- | --- | --- |
| **holon** (`.holon.jsonld`) | shipped | RDF/JSON-LD, renders in the [xbrlkit viewer](https://xbrlkit.com/). **Lossless against the model** — see below |
| **TAVI** (`.tavi.json`) | shipped | [Project TAVI](https://www.xbrl.org/Specification/tavi/PWD-2026-09-01/tavi-PWD-2026-09-01.html) compiled model, PWD-2026-09-01 |
| **OIM** (`.oim.json`) | shipped | xBRL-JSON, checked fact-for-fact against Arelle's own writer |
| **property graph** (`.lbug`, icebug-disk, parquet) | shipped | the [RoboSystems](https://robosystems.ai) `sec` graph's tables, ids and DDL, as one LadybugDB file per filing or an icebug-disk tree any LadybugDB queries in place |
| **ClawDog** (`.clawdog.jsonld`) | shipped | authored-report JSON-LD with fact provenance and calculation equations |

Three of them read back: see [`deserialize/`](../deserialize/README.md).

```python
from xbrlkit.serialize import (
  to_clawdog_report,
  to_holon,
  to_tavi_report,
  to_oim,
  to_graph_tables,
)

clawdog, gaps = to_clawdog_report(model)
holon = to_holon(model)
tavi, gaps = to_tavi_report(model)  # the document, and what it could not express
```

## The OIM projection is the one with a reference implementation

Arelle's `saveLoadableOIM` writes the same document from the same filing. A
second writer is redundant as a feature — its value is that **every difference
is a fidelity bug in the parse or the model**, and those same bugs are
otherwise silent in the holon output, which has nothing to check it. Current
parity is every fact on 3M FY2024 (3,150) and Boeing FY2024 (2,688), and all
but one on Microsoft FY2024 (1,855 of 1,856); footnotes are the one construct
the model does not carry.

## TAVI, and its gap report

TAVI is a **public working draft** whose name is explicitly a working title.
The emitter is written against the prose of PWD-2026-09-01, checked against the
eight example models published with the draft, and then diffed object class by
object class against the compiled model Arelle's `XbrlModel` plugin writes for
the same filing.

`to_tavi_report` returns a **`GapReport`** beside the document, split into what
the model cannot express and what this emitter has not mapped yet, so neither
is blamed for the other. That report is the substantive output of the exercise.
`SPEC_AMBIGUITIES` records where the draft is unclear or contradicts itself and
what this emitter chose.

The one genuine transformation is dimensionality: XBRL says it with arcroles
over `<xs:element>`s — a hypercube is an element, an axis is an element — while
TAVI gives each its own object type, so the definition networks are read back
into cube, dimension, domain class, domain network and member objects.

## The holon

The holon is the RDF/JSON-LD projection and the one standardisation candidate
in the set (see [`namespaces.py`](../namespaces.py)). It is a dataset of three
named graphs — **scene** (report, entity, facts, periods, units, dimensions),
**projection** (elements, structures, associations) and **boundary** (the
calculation roll-ups).

It is **lossless against `XbrlModel`**, and the round trip is the gate: model →
holon → model is identical on all 26 filings of the Filing Ladder corpus across
facts (values, decimals, dimensions, nil, language), labels (role and
language), all three network kinds (order, weight, preferred label, roots),
every concept field, and units and periods including their ids. The one thing
not carried is an element that no fact, network or dimension mentions — the
partitioner drops it, and a real parse does not produce one.

Two decisions inside it are load-bearing:

- **Each document binds its own prefixes**, from the namespaces the concepts,
  their declared types and the units carry. The alternative — a fixed table of
  year-normalized stems — addressed a us-gaap concept at an IRI FASB never
  minted and left a filer's own concepts spelling out a robosystems.ai URL.
  Year-independent identity is `rs-gaap`'s job, a real taxonomy with
  equivalence arcs onto each us-gaap version.
- **A label is a literal with a role, not a node.** Every label role is one
  predicate (`terseLabel`, `negatedLabel`, `periodStartLabel`, …); reifying
  them would add a node per label, half again as many nodes as a report has.

## The property graph

`xbrlkit build --format lpg` (with the `lpg` extra) writes the filing as a
single-file [LadybugDB](https://github.com/LadybugDB/ladybug) database with the
tables the RoboSystems `sec` graph is built from — the same node labels,
relationship types, columns and ids, declared once in
[`xbrlkit.schema`](../schema/__init__.py) — so Cypher written against the
shared graph runs on the file and a fact in either is the same row.

```python
from xbrlkit.serialize import to_graph_tables, write_parquet, build_lbug, write_icebug

tables = to_graph_tables(model)  # node and relationship rows, schema order
write_parquet(tables, Path("out/mmm"))  # nodes/*.parquet, relationships/*.parquet
build_lbug(tables, Path("out/mmm.lbug"))  # CREATE TABLE … + COPY FROM, one file
write_icebug(tables, Path("out/mmm.icebug"))  # icebug-disk: parquet CSR + schema.cypher
```

### Two containers, one set of rows

A `.lbug` is LadybugDB's own storage: one file, loaded by `COPY`, and readable
only by the engine version that wrote it or a later one that still reads that
storage version. An **icebug-disk** tree
([spec](https://github.com/Ladybug-Memory/icebug-format)) is the same tables as
plain parquet in CSR layout — `nodes_<Label>.parquet` (a row's position is the
node's offset), and per relationship an `indices_<TYPE>.parquet` (targets and
edge properties, sorted by source) and an `indptr_<TYPE>.parquet` (row
pointers) — plus a `schema.cypher` that declares every table over the tree. A
LadybugDB database runs that file and queries the parquet where it lies: no
load, no engine version tied to the files, and no LadybugDB needed to write
them. LadybugDB's own `EXPORT DATABASE` writes the same layout from 0.21 on.

```python
import ladybug

conn = ladybug.Connection(ladybug.Database(":memory:"))
for statement in Path("out/mmm.icebug/schema.cypher").read_text().split(";\n"):
  if statement.strip():
    conn.execute(statement)
conn.execute("MATCH (f:Fact)-[:FACT_HAS_ELEMENT]->(e:Element) RETURN e.qname, count(f)")
```

`schema.cypher` names where the tree lives — this directory's absolute path by
default, or `write_icebug(..., storage="https://…")` (or `s3://`, `hf://`) for a
tree you host; remote reads need LadybugDB's `httpfs` extension. Every table is
read back through Cypher identical to the `.lbug` built from the same rows, on
LadybugDB 0.18.1, 0.20.2 and 0.21.0. Two engine defects to know while they are
open: write relationship patterns with a type (`-[:FACT_HAS_ELEMENT]->`, never
`-[r]->`), since untyped patterns over icebug-disk tables return wrong rows
([#1066](https://github.com/LadybugDB/ladybug/issues/1066)); and on 0.21.0 a
pattern that changes direction and then filters a later node can fail
([#1068](https://github.com/LadybugDB/ladybug/issues/1068)).

### Stacking filings

`merge_graph_tables` puts several filings into one graph before either
container is written. The ids are content-addressed, so what two filings share —
the entity, periods, units, labels, references — arrives with the same id and is
kept once, while the report and its facts, dimensions and structures stay apart;
a node id that arrives twice with different properties is an error, never a
silent pick. A container cannot be appended to instead: a tree's rows are
positional, and `COPY` into an existing `.lbug` stops at the first shared id.

```python
from xbrlkit.serialize import merge_graph_tables

stack = merge_graph_tables(to_graph_tables(m) for m in (fy2024, fy2025))
write_icebug(stack, Path("out/mmm-stack.icebug"))
```

Two things to know when reading a stack. An element's id carries its taxonomy
year (`us-gaap/2024`, `us-gaap/2025`), so the same concept in two years is two
`Element` nodes: match across years on `Element.qname`. And two reports can hold
different values for one period — a restatement, or a recast comparative — so
scope a value to its report (`(r:Report)-[:REPORT_HAS_FACT]->(f)`) rather than
reading a period alone.

`xbrlkit.cypher.run_cypher` runs one read-only query over either container, in a
worker process with a time limit, and refuses untyped relationship patterns on
a tree (#1066 above).

What the platform adds *after* projection is not in the file: text blocks stay
inline in `Fact.value`, and the enrichment columns and tables
(`canonical_concept`, `canonical_type`, `FactSet`, `Classification`) are empty.
The projection is checked row for row against the platform's own processor on
the 26-filing corpus; the two explained differences are association ids (random
on the platform, derived from the arc here) and exact duplicate arcs inside
Arelle's aggregate `XBRL-dimensions` network, which the derived ids collapse.
