# xbrlkit

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

<!-- mcp-name: ai.robosystems/xbrlkit -->

**The XBRL toolkit from [RoboSystems](https://github.com/RoboFinSystems/robosystems), the open-source knowledge graph for finance.**

Read any XBRL financial report on your own machine: an SEC 10-K, a European
ESEF filing, or a report that was never filed with anyone. No account, no
database, nothing indexed.

| You want to | Run |
| --- | --- |
| See a filing as a rendered report | `uvx xbrlkit view NVDA` |
| Ask questions of a filing from an MCP client | `uvx --from "xbrlkit[mcp]@latest" xbrlkit serve` |
| Save a filing as one portable file | `xbrlkit fetch --ticker NVDA` |
| Turn that file into Markdown or Excel | `xbrlkit databook <file>` · `xbrlkit workbook <file>` |
| Build a report from your own numbers, and check it | `build_report` · `check_report` |

```bash
pip install xbrlkit
```

## See a filing

`xbrlkit view` opens a filing as a rendered report in your browser.

```bash
xbrlkit view NVDA                       # the latest 10-K
xbrlkit view "NVDA 10-Q" --as tavi      # a different form, a different serialization
xbrlkit view 320193:0000320193-23-000106
xbrlkit view lei:549300E9PC51EN656011   # a filer outside EDGAR
xbrlkit view ./report.zip               # a file you already have, no network
xbrlkit view NVDA --no-open             # print the link instead of opening it
```

The report renders in the [xbrlkit viewer](https://xbrlkit.com/)
([source](https://github.com/RoboFinSystems/xbrlkit-viewer)), which reads the
document entirely in the browser. Your machine serves it on a loopback port
that only the viewer's origin can read, until you press Ctrl-C.

## Ask a filing questions

`xbrlkit serve` is a local MCP server: it holds filings in memory and answers
from them.

```bash
uvx --from "xbrlkit[mcp]@latest" xbrlkit serve
# → MCP at http://127.0.0.1:8765/mcp
```

Point a client at that URL:

```json
{
  "mcpServers": {
    "xbrlkit": { "type": "http", "url": "http://127.0.0.1:8765/mcp" }
  }
}
```

Or let the client launch the server itself, over stdio:

```json
{
  "mcpServers": {
    "xbrlkit": {
      "command": "uvx",
      "args": ["--from", "xbrlkit[mcp]@latest", "xbrlkit", "serve", "--transport", "stdio"],
      "env": { "SEC_GOV_USER_AGENT": "Your Name you@example.com" }
    }
  }
}
```

Then load a filing from the chat (*"load NVDA's latest 10-K"*) and ask:

- **A statement as a table**: the income statement, balance sheet, cash flow or any disclosure, in the filer's own order and labels
- **The concept behind a phrase**: "revenue" → the element names this filer reports, its own extensions included
- **Values by concept and period**: consolidated totals, or broken out by segment, product or any other axis
- **Whether a subtotal foots**: the parts, their weights, and the computed sum against the reported total
- **One disclosure whole**: a note's rows, its breakdowns, its roll-up and its text together
- **A search of the prose**: regex over the whole document, tagged or not
- **The other documents**: exhibits, an 8-K's earnings release, a 13F's holdings
- **Forms with no XBRL**: a Form 4's transactions, a 13F's positions, as rows
- **Filings worth reading**: EDGAR full-text search, where every hit carries the id that loads it
- **An export**: the filing as any format below, or opened in the browser
- **Several filings at once** (the `lpg` extra): a company's years or its peers as one graph, with read-only Cypher over it

Every answer is read from the filing in memory. The one graph it can query is
one you asked it to write.

A ticker loads RoboSystems' published parse of the filing when one exists, and
the answer's `read_from` says which copy you have; `--pure` parses from EDGAR
instead. The tool table, the switches and where exports go are in
[`serve/`](https://github.com/RoboFinSystems/xbrlkit/blob/main/xbrlkit/serve/README.md).

## Convert a filing

`build` and `fetch` write a filing as a file; `--format` picks which.

```bash
xbrlkit fetch --ticker NVDA                                        # latest 10-K -> ./output/
xbrlkit fetch --ticker NVDA --form 10-Q --n 4 --format tavi
xbrlkit build --cik 320193 --accno 0000320193-23-000106 --format all
```

| Format | What it is | Reads back |
| --- | --- | --- |
| `holon` (default) | The whole report as one RDF / JSON-LD file, lossless | yes |
| `tavi` | A compiled model, with a sidecar listing what it could not carry | yes |
| `clawdog` | Authored-report JSON-LD, with the same kind of sidecar | yes |
| `oim` | xBRL-JSON, the standard's own JSON | no |
| `lpg` | A property graph as a LadybugDB database you can query (the `lpg` extra) | no |
| `icebug` | The same graph as a directory any LadybugDB reads in place (experimental) | no |

A holon is the file the other commands take:

```bash
xbrlkit validate output/0000320193-23-000106.holon.jsonld   # checked against the format, graph by graph
xbrlkit databook output/0000320193-23-000106.holon.jsonld   # Markdown tables, with the facts as RDF
xbrlkit workbook output/0000320193-23-000106.holon.jsonld   # Excel, a sheet per section (the xlsx extra)
xbrlkit query --in output/0000320193-23-000106.holon.jsonld --element us-gaap:Assets
```

Each format is described in
[`serialize/`](https://github.com/RoboFinSystems/xbrlkit/blob/main/xbrlkit/serialize/README.md),
and the holon itself in
[`holon/`](https://github.com/RoboFinSystems/xbrlkit/blob/main/xbrlkit/holon/README.md).

## Use it as a library

A `FilingSession` loads a filing the way `view` and `serve` do, with no extra
installed.

```python
from xbrlkit.deserialize import from_holon_json
from xbrlkit.serialize import to_holon, to_tavi_report
from xbrlkit.serve import FilingSession

session = FilingSession()
model = session.load("NVDA").model  # or "NVDA 10-Q", "cik:accession", "lei:…", a path
holon = to_holon(model)             # holon.jsonld text
tavi, gaps = to_tavi_report(model)  # the TAVI model, and what it could not carry
model = from_holon_json(holon)      # and back again
session.close()
```

A host running its own Arelle goes a level down: `xbrlkit.parse.load_model`
returns the `ModelXbrl`, and `to_xbrl_model(mx, filing)` walks it into the same
model.

## Author a report

`build_report` turns rows into a report, and `check_report` gives one verdict
on it.

```python
from datetime import date
from xbrlkit.author import ConceptRow, EntityRow, FactRow, LineRow, ReportRows, SectionRow, build_report
from xbrlkit.check import check_report

model = build_report(
  ReportRows(
    report_id="acme-2026-10",
    entity=EntityRow(identifier="acme-co", name="Acme Co"),
    reporting_style="authored-package",
    concepts=[
      ConceptRow(qname="us-gaap:Assets", label="Total assets", period_type="instant"),
      ConceptRow(qname="us-gaap:Cash", label="Cash", period_type="instant"),
    ],
    sections=[
      SectionRow(
        name="Balance Sheet",
        block_type="balance_sheet",
        lines=[
          LineRow(concept="us-gaap:Assets"),
          LineRow(concept="us-gaap:Cash", parent="us-gaap:Assets", weight=1),
        ],
      )
    ],
    facts=[
      FactRow(concept="us-gaap:Assets", value=600, end=date(2026, 10, 31), unit="USD",
              decimals=0, source="package.xlsx#sheet=BS&cell=B10"),
      FactRow(concept="us-gaap:Cash", value=600, end=date(2026, 10, 31), unit="USD",
              decimals=0, source="package.xlsx#sheet=BS&cell=B4"),
    ],
  )
)
result = check_report(model, require_source=True)
result.ok, result.to_dict()["problems"]  # (True, [])
```

- **What the builder does**: mints the periods, units, fact ids and the presentation and calculation networks, so nobody writes a holon or a TAVI by hand
- **What the check covers**: every fact resolves, duplicate facts agree, every calculation foots, the balance sheet balances
- **When a row is wrong**: `AuthoringError` names every row at fault
- **A fact's `source`**: the document's name or URL, then `key=value` pairs saying where the value was read (`page=3` for a PDF, `sheet=BS&cell=B10` for a workbook, `label=` for the line's own wording)

The model that comes back is the same one a filing parses into, so it
serializes, serves and renders like one.

## Install

```bash
pip install xbrlkit
```

| Extra | Adds |
| --- | --- |
| `xbrlkit[mcp]` | The MCP server, `xbrlkit serve` |
| `xbrlkit[lpg]` | The property graph as a LadybugDB database, and Cypher over it |
| `xbrlkit[icebug]` | The same graph as an icebug-disk tree (experimental) |
| `xbrlkit[xlsx]` | A holon as an Excel workbook |

Combine them for the server: `xbrlkit[mcp,lpg,xlsx]` adds the graph tools and
the workbook export.

From a source checkout:

```bash
brew install uv just
just install     # dependencies, and .env from the template
```

### SEC User-Agent

Optional. EDGAR works under a default that names the project, saying so once;
identifying yourself is a courtesy the SEC asks for.

```bash
export SEC_GOV_USER_AGENT="Your Name your@email.com"
```

- **Where to set it**: the environment, `--user-agent` before the command, or an MCP client's `env` block
- **A `.env` file**: read only inside a checkout of this repo, never by a `pip` or `uvx` install
- **When it matters**: only for EDGAR; a local file, a JSON report and filings.xbrl.org need none

## How it's built

A filing is parsed once into a neutral model, and everything else reads that
model.

```
  EDGAR ───────────┐
  filings.xbrl.org ├──▶ Arelle ──▶ XbrlModel ──┬──▶ holon.jsonld ──▶ DataBook, workbook
  XBRL zip / iXBRL ┘                 ▲         ├──▶ TAVI
                                     │         ├──▶ xBRL-JSON
     holon, TAVI, ClawDog ───────────┤         ├──▶ ClawDog
     your own rows (author) ─────────┘         └──▶ property graph

                   primary HTML ──▶ xbrlkit.text ──▶ sections (text blocks, Items, tables)
```

[Arelle](https://arelle.org) stays the parser; nobody should reimplement DTS
resolution. What it does not give you is anything ergonomic to hold:
`ModelXbrl` is a large mutable object graph tied to a controller you have to
close. `XbrlModel` is stateless, single-filing and lossless.

**The one architectural rule:** everything goes through `XbrlModel`. A feature
that reaches into Arelle's `ModelXbrl` directly is bypassing it.

| Module | What it does |
| --- | --- |
| [`parse`](https://github.com/RoboFinSystems/xbrlkit/blob/main/xbrlkit/parse/README.md) | Arelle in, `XbrlModel` out: the load, the DTS cache policy, taxonomy packages |
| [`serialize`](https://github.com/RoboFinSystems/xbrlkit/blob/main/xbrlkit/serialize/README.md) | The formats out, and the gap reports |
| [`deserialize`](https://github.com/RoboFinSystems/xbrlkit/blob/main/xbrlkit/deserialize/README.md) | A holon, a TAVI or a ClawDog report read back into the model, no Arelle |
| [`holon`](https://github.com/RoboFinSystems/xbrlkit/blob/main/xbrlkit/holon/README.md) | The holon format, `validate`, `databook` and `workbook` |
| [`edgar`](https://github.com/RoboFinSystems/xbrlkit/blob/main/xbrlkit/edgar/README.md) | The SEC: discovery, download, full-text search, 1994 onward |
| [`filings_org`](https://github.com/RoboFinSystems/xbrlkit/blob/main/xbrlkit/filings_org/README.md) | ESEF and the national regimes, by LEI |
| [`text`](https://github.com/RoboFinSystems/xbrlkit/blob/main/xbrlkit/text/README.md) | The filing as prose: text blocks, 10-K/10-Q Items, the XML forms; needs neither Arelle nor the network |
| [`serve`](https://github.com/RoboFinSystems/xbrlkit/blob/main/xbrlkit/serve/README.md) | The local MCP server |

`author.py` and `check.py` build and check a report from rows, `query.py` runs
SPARQL over a holon, `cypher.py` runs read-only Cypher over a built graph, and
`view.py` is the loopback server behind `xbrlkit view`.

## Where it runs

- **RoboSystems**: the [platform's](https://github.com/RoboFinSystems/robosystems) SEC pipeline parses, projects and indexes every filing with this package, and the shared `sec` graph is declared from `xbrlkit.schema`
- **Filing Ladder**: the [benchmark](https://github.com/HarbingerFinLab/filing-ladder) built its 26-filing corpus with this package, each format is a rung, and since protocol v0.2 `xbrlkit serve` is a rung of its own in the [published table](https://github.com/HarbingerFinLab/filing-ladder/blob/main/results/README.md)

## Documentation

- **Modules**: the table above links each module's own README
- **Technical docs**: [robosystems.ai/docs/technical](https://robosystems.ai/docs/technical)
- **Related**: [RoboSystems](https://github.com/RoboFinSystems/robosystems) · [xbrlkit viewer](https://github.com/RoboFinSystems/xbrlkit-viewer) · [Filing Ladder](https://github.com/HarbingerFinLab/filing-ladder)

## Support

- [Issues](https://github.com/RoboFinSystems/xbrlkit/issues) · [Discussions](https://github.com/orgs/RoboFinSystems/discussions) · [Projects](https://github.com/orgs/RoboFinSystems/projects)

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

MIT © 2026 RFS LLC
