# The holon — the format, and how a file is checked against it

A **holon** is one report written as an RDF dataset: a handful of named graphs
that hang off a single report IRI, each holding one kind of thing. It is
serialized as dataset-form JSON-LD (`.holon.jsonld`). xbrlkit writes one from
any filing it can parse ([`serialize/`](../serialize/README.md)), reads one
back into the model ([`deserialize/`](../deserialize/README.md)), and this
package says whether a given file *is* one.

This page is the definition. It describes what the writers produce today,
version `1.0`; the shapes in [`shapes/`](shapes/) are the same definition in a
form a machine can run.

```bash
xbrlkit validate report.holon.jsonld
```

```
conforms  https://robosystems.ai/report/0000320193-24-000123
  #scene       ok  (90,561 triples, 12 shapes)
  #projection  ok  (86,983 triples, 8 shapes)
  #boundary    ok  (3,480 triples, 5 shapes)
  #lineage     absent
  dataset      ok  (162,319 triples, 11 shapes)
  report       7 problems  (1,283 facts, 41/48 calculations foot)
```

```python
from xbrlkit.holon import validate_holon

result = validate_holon(text)
result.conforms   # the file meets the format
result.ok         # ... and the report inside it holds together
result.graphs["scene"].findings
```

## Why named graphs

A report is several different things at once, and they change for different
reasons and are wanted by different readers:

| Graph | Holds | The question it answers |
| --- | --- | --- |
| `<report>#scene` | the report and its entity; the facts; the element, period, unit and dimensions each fact points at; the information blocks that group facts into sections | *What does the report state?* |
| `<report>#projection` | the structures (one per section), their presentation and definition arcs, and the elements those arcs join | *How is it arranged?* |
| `<report>#boundary` | the calculation arcs: which facts must sum to which | *What must hold for it to be right?* |
| `<report>#lineage` | the ledger behind the facts: contributions, ledger lines, entries, transactions, events | *Where did each number come from?* |

A single flat graph can carry all of it, and nobody can then say which triples
are the statement, which are its layout and which are its rules. Kept apart, a
reader takes the graph it needs, a writer can leave one out, and each can be
checked against its own definition.

Only `#scene` is always present. A report with no networks has no
`#projection` and no `#boundary`; an empty graph is not written. `#lineage` is
optional by design: a report is an aggregation of the books, not the books,
and whoever holds the ledger decides who receives it.

### How the names line up with other writing on holons

The words *scene*, *boundary* and *projection* are used in more than one sense
in the literature this draws on, so, for the avoidance of doubt:

- `#scene` is the graph of **current reported values**. It is not the
  human-readable prose layer of a document.
- `#boundary` is the set of **rules the contents must satisfy**. It is not
  access control.
- `#projection` is the report's **arrangement** for a reader.
- `#lineage` contains what is elsewhere called the **event graph**, together
  with the ledger derived from those events and the links from each fact back
  to it. The events in it are typed `rs:Event` and `prov:Activity`.

## The document

```json
{
  "@context": { "rs": "https://robosystems.ai/vocab/", "us-gaap": "http://fasb.org/us-gaap/2024#", "…": "…" },
  "@graph": [
    { "@id": "https://robosystems.ai/report/<id>#boundary",   "@graph": [ … ] },
    { "@id": "https://robosystems.ai/report/<id>#projection", "@graph": [ … ] },
    { "@id": "https://robosystems.ai/report/<id>#scene",      "@graph": [ … ] }
  ]
}
```

- **One report per file.** Every graph is named `<report IRI>#<name>`, and the
  `rs:Report` node in `#scene` *is* that report IRI.
- **The vocabulary** is `https://robosystems.ai/vocab/` (`rs:`), with XBRL's
  own terms where XBRL has them (`xbrli:`, `xlink:`, `link:`), SKOS for labels
  and PROV-O for provenance.
- **Each document binds its own prefixes**, from the namespaces its concepts,
  their declared types and its units actually use. A `us-gaap` concept is
  addressed in the taxonomy year the filing used.
- **The version** is on the report: `rs:serializationVersion`, currently
  `"1.0"`.
- **Order carries no meaning.** `@graph` arrays and multi-valued properties are
  sorted so the same report is the same bytes on every write.

## What each graph contains

Cardinalities below are what the shapes enforce. *Described in this graph*
means the node being pointed at has its own description in the same graph, so
the graph can be read alone.

### `#scene`

| Node | Requires | May carry |
| --- | --- | --- |
| `rs:Report` (exactly one) | `rs:entity` → an `rs:Entity` here; `rs:serializationVersion`; `rs:accessionNumber` (the report's identifier); `rs:reportingStyle` | `rs:form`, `rs:filingDate`, `rs:periodEndDate`, the fiscal focus, `rs:mode` |
| `rs:Entity` | `rs:internalId`; `rs:scheme` (an IRI); `skos:prefLabel` | `rs:legalName`, `rs:ein` |
| `rs:Fact` | `rs:element`, `rs:period`, `rs:entity`, each exactly one and described here; `rs:factType` (`numeric` or `nonnumeric`); `rs:internalId` | `rs:unit` (at most one, described here); `rs:numericValue` (an `xsd:decimal`) or `rs:stringValue`; `rs:decimals`; `rs:dimension`…; `rs:factSet`…; `rs:structure`; `rs:isNil`; `rs:language`; provenance (below) |
| `rs:Element` | `rs:internalId` (its QName); `rs:elementType` (`concept`, `abstract`, `axis`, `member`, `hypercube`); `rs:itemType`; `rs:abstract` | `xbrli:periodType`, `xbrli:balance`, labels, `rs:dataType`, `rs:baseType`, `rs:substitutionGroup` |
| `rs:Period` | `xbrli:periodType` (`instant`, `duration`, `forever`) | `xbrli:instant`, or `xbrli:startDate` and `xbrli:endDate`; the calendar keys |
| `rs:Unit` | `xbrli:measure` (an IRI) | |
| `rs:Dimension` | `rs:axis` → an `rs:Element` here | `rs:member` → an `rs:Element` here, or `rs:typedValue`; `rs:isExplicit`, `rs:isTyped`, `rs:axisType` |
| `rs:InformationBlock` | `rs:structure` (an IRI); `rs:factSet` (an IRI); `rs:internalId`; `skos:prefLabel` | `rs:blockType` |

A numeric fact carries its number and a nonnumeric fact carries none. An
instant period that states no instant, or a duration with no end date, is a
warning: unusual, and still readable.

A fact joins a section through `rs:factSet`: it shares a fact-set IRI with the
information block for that section, and may sit in several. The fact set has
no node of its own.

**Provenance on a fact** is PROV-O where PROV-O has the word:
`prov:hadPrimarySource`, `prov:wasAttributedTo`, and, when the ledger is
attached, `prov:wasDerivedFrom` → the contributions in `#lineage`. Two terms
are the vocabulary's own: `rs:sourceKind`, and `rs:contentHash`, a digest of
what the fact was computed from that travels with the fact whether or not the
ledger does.

**Labels are literals, one predicate per role**: `skos:prefLabel`, then
`rs:standardLabel`, `rs:terseLabel`, `rs:totalLabel`, `rs:negatedLabel`,
`rs:periodStartLabel` and the rest, with `rdfs:comment` for documentation. A
label is not a node.

No `rs:Association` and no `rs:Structure` belongs in `#scene`.

### `#projection`

| Node | Requires | May carry |
| --- | --- | --- |
| `rs:Structure` (also typed `rs:RollUp` or `rs:Hierarchy`) | `rs:internalId`; `rs:roleUri`; `rs:structureName` | `rs:hasAssociation`… → `rs:Association` nodes here; `rs:factSet`; `rs:structureOrder`; `rs:blockType` |
| `rs:Association` | `xlink:from` and `xlink:to`, each exactly one `rs:Element` described here; `rs:associationType` (anything but `calculation`) | `xlink:arcrole`, `xlink:role`, `link:order`, `rs:preferredLabelRole`, `rs:preferredLabel`, `rs:targetRole` |
| `rs:Element` | as in `#scene` | |

An element is described in every graph that cites it, so an element used by a
fact and by an arc appears in both `#scene` and `#projection`, the same
description each time.

No fact, report or information block belongs here, and no calculation arc.

### `#boundary`

Calculation arcs and nothing else.

| Node | Requires |
| --- | --- |
| `rs:Association`, also typed `rs:RollUpRelationship` | `xlink:from` (the total) and `xlink:to` (the component); `rs:associationType` = `calculation`; `link:weight` |
| a structure, by IRI only | nothing but `rs:hasAssociation` → the arcs here. The structure is described in `#projection` |

### `#lineage` (optional)

Written by a producer that holds the books. The chain runs from a fact down
to the events behind it:

```
rs:Fact ─prov:wasDerivedFrom─▶ rs:Contribution      one per entity and account
                                 └─prov:hadMember─▶ rs:LineItem …
rs:LineItem ─rs:entry─▶ rs:Entry ─rs:transaction─▶ rs:Transaction
     └─prov:wasGeneratedBy─▶ rs:Event ─prov:wasAssociatedWith─▶ rs:Agent
```

| Node | Requires | May carry |
| --- | --- | --- |
| `rs:Contribution` (also `prov:Collection`) | `rs:substantiates` → one fact, by IRI; `rs:lineCount` | `prov:hadMember`… → ledger lines, by IRI; `rs:account`, `rs:entity`, `rs:amount`, the window |
| `rs:LineItem` | `rs:internalId`; `rs:entry` → an `rs:Entry` here; `rs:account` → an `rs:Account` here | `rs:debit` or `rs:credit`; `prov:wasGeneratedBy` → an `rs:Event` here |
| `rs:Entry` | `rs:internalId`; `rs:transaction` → an `rs:Transaction` here; `rs:postingDate` | `prov:wasGeneratedBy` |
| `rs:Transaction` | `rs:internalId`; `rs:transactionDate` | `prov:wasGeneratedBy` |
| `rs:Event` (also `prov:Activity`) | `rs:internalId`; `rs:eventType`; `rs:occurredAt` | `prov:wasAssociatedWith` → an `rs:Agent` here |
| `rs:Account` | `rs:internalId`; `skos:prefLabel` | |
| `rs:Agent`, `rs:LedgerEntity` | `rs:internalId` | |

**A contribution may list no members.** That is the form the graph takes when
the ledger lines are withheld: the contribution, its count and its amount say
what stands behind the fact without disclosing it.

The report's own classes stay out of this graph. A member entity is an
`rs:LedgerEntity`, not an `rs:Entity`, and a chart account an `rs:Account`,
not an `rs:Element`, because a reader takes an `rs:Entity` or an `rs:Element`
to be the report's.

### The graphs together

What no single graph can show, checked over their union:

- a fact's or an information block's `rs:structure` is an `rs:Structure` that
  `#projection` describes;
- every arc under a structure is an `rs:Association` some graph describes, and
  both its ends are elements `#projection` describes (this is where a
  `#boundary` arc to an undescribed element is caught);
- a fact's `prov:wasDerivedFrom` is a contribution `#lineage` describes, and a
  contribution substantiates a fact `#scene` describes;
- none of the retired ways of writing a report appears anywhere:
  `xbrli:contextRef` (a fact carries its period, entity and unit directly),
  `rs:arcFrom` / `rs:arcTo`, or a direct predicate between two elements
  (`rs:summationOf`, `rs:parent`, `rs:generalOf`, `rs:dimensionOf`).

## Two checks, two layers

`validate_holon` answers two different questions and keeps the answers apart.

**Does the file meet the format?** (`conforms`) The shapes above, graph by
graph, then together. This looks at the file as written, so it sees what a
tolerant reader would quietly skip or repair.

**Does the report hold together?** (`check`) The file is read back into the
model and [`check_report`](../check.py) asks whether it is whole (an
identity, every fact pointing at a concept, period and unit that exist) and
agrees with itself (duplicate facts match, every calculation foots, the
balance sheet balances).

A filing as filed regularly conforms and does not foot everywhere, which is a
fact about the filing. So `xbrlkit validate` exits `1` only when the file does
not meet the format, and `--strict` adds the report check for a report that is
about to be loaded somewhere and must be right. `--no-check` skips the second
question; `--json` prints the whole verdict.

Neither check proves the report is true or complete. They say the file is
what it claims to be and that the report is consistent with itself.

## The DataBook: a holon as a document to read

A holon is complete, and nobody reads one raw. `xbrlkit databook` writes the
report as a [DataBook](https://github.com/kurtcagle/databook): a Markdown
file that is at once a document a person reads, a container of typed data
blocks, and a self-describing artefact whose YAML frontmatter says what it is
and how it was made.

```bash
xbrlkit databook report.holon.jsonld        # -> report.databook.md
```

```python
from xbrlkit.holon.databook import databook_from_holon, to_databook

markdown = databook_from_holon(text, href="report.holon.jsonld")
```

The relationship is the one iXBRL has to XBRL. An inline XBRL document is a
rendering with the facts embedded in it; the linkbases are elsewhere, and a
reader who wants them goes to the XBRL. A DataBook is the rendering with the
facts; the holon is the complete document.

**It is a projection of the holon, not a copy of it**, and it says which part
of each graph it carries:

| Graph | In the DataBook |
| --- | --- |
| `#scene` | *projected*: each section as a table of its consolidated values, and the same facts beneath it as a Turtle block |
| `#projection`, `#boundary`, `#lineage` | *referenced*: named by IRI, with their size; the content is in the holon |

So the two go together and neither contains the other. The DataBook's process
stamp names the holon as its primary input, by IRI and by SHA-256, and the
document ends by pointing at it. Whoever wants the breakdown by segment, the
roll-ups or the ledger goes to the holon.

What a DataBook holds:

- **Frontmatter** that keeps to the DataBook core keys (`id`, `title`,
  `type`, `version`, `created`, `graph`, `shapes`, `process`). What is
  particular to a report sits under one key, `xbrl_holon`: the report, the
  holon and its digest, the four graphs with their disposition, the sections,
  and the validation verdict.
- **A section per information block**, in the report's order: the table, any
  narrative too long for a cell as a short passage, and a note of how many
  facts break the values out by which axes.
- **Every value is a link to its fact.** A number in a table is a
  reference-style link whose target, written once beneath the table, is the
  fact's IRI in the holon. It is the DataBook's form of inline tagging: what a
  person reads *is* the fact. `--no-links` writes plain values.
- **A reference block per section** (`mode=reference`, folded): the facts in
  the table as Turtle, addressed by `databook:id` and bound for the holon's
  `#scene` graph. Every triple in a block is a triple of the holon.
- **The verdict**: each graph against its shapes, and the report read back.
- **The holon**: where it is, its digest, and what each graph holds.

It is meant to stay a document. A filing's "(Tables)" and "(Details)" roles,
the workings behind its notes, are named in the contents and left to the
holon unless `--details` asks for them; a section stops at 400 rows and the
document at about 1,500; and the Turtle stops at about 3,000 triples, after
which sections point at the holon instead. `--no-rdf` writes the tables alone.

## How the format changes

Additively. A property may be added and a node type may be added; a term is
never renamed and a graph is never repurposed, because holons already written
are read by tools that will not be rewritten. A change that could make an
existing conforming file stop conforming needs a new
`rs:serializationVersion`.

The shapes are deliberately a floor, not a census: they require what
xbrlkit's writer has always written and what a reader cannot do without. A
property a writer adds is not a violation. A holon written before the format
had a writer of its own (no `rs:Report` node, elements without an
`rs:itemType`) does not conform, and says so.

## The workbook: a holon as an Excel file

`xbrlkit workbook` writes the report as an `.xlsx`, for the reader who opens a
spreadsheet. It needs the `xlsx` extra (`pip install "xbrlkit[xlsx]"`).

```bash
xbrlkit workbook report.holon.jsonld        # -> report.xlsx
```

```python
from xbrlkit.holon.workbook import workbook_from_holon, to_workbook

data = workbook_from_holon(text, href="report.holon.jsonld")  # bytes
```

One sheet per section of the report, in the report's order, behind a contents
sheet that names the holon by IRI and by SHA-256 and lists every sheet with
its counts. On a section's sheet a row is a line the section presents and a
column is a period; under a line sit its breakdowns, one row per member of the
section's own axes. Unlike the DataBook it draws every section, the detail
tables and their breakdowns included.

What it does to the facts, all of it said on the contents sheet:

- **Every number is the reported fact, written as a number.** No cell holds a
  formula, so a total is the reported total and nothing is recomputed. A label
  or a text value that begins with `=` is written as text.
- **A balance is read into the period it opens or closes.** A duration column
  shows the balance dated at its end, and a line presented with a
  `periodStart` label shows the balance dated the day before its start, so a
  roll-forward reads as it prints. A date gets a column of its own only when a
  fact on it is shown nowhere else.
- **A line presented with a negated label shows its value reversed,** as the
  statement prints it, and the sheet's `Sign` column marks the line.
- **A period only a stray fact or two is dated on has no column.** The
  contents sheet counts the facts each section holds beside those it shows.

The rows and columns come from [`pivot.py`](pivot.py) (`pivot_sections`), which
caps nothing and writes no file. It follows the rules
[report-components](https://github.com/RoboFinSystems/robosystems-report-components)
uses on screen for balances, signs and sparse periods.

## Files

| | |
| --- | --- |
| [`__init__.py`](__init__.py) | `validate_holon`, `validate_holon_file`, `render`, the verdict types |
| [`databook.py`](databook.py) | `databook_from_holon`, `to_databook` |
| [`pivot.py`](pivot.py) | `pivot_sections`: a section as lines by periods, with its breakdowns |
| [`workbook.py`](workbook.py) | `workbook_from_holon`, `to_workbook` |
| [`shapes/scene.ttl`](shapes/scene.ttl), [`projection.ttl`](shapes/projection.ttl), [`boundary.ttl`](shapes/boundary.ttl), [`lineage.ttl`](shapes/lineage.ttl) | one shapes graph per named graph |
| [`shapes/common.ttl`](shapes/common.ttl) | `rs:Element`, read into both `#scene` and `#projection` |
| [`shapes/dataset.ttl`](shapes/dataset.ttl) | the graphs together |
