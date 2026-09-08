# xbrlkit

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

Work with XBRL filings above [Arelle](https://arelle.org): fetch a filing, parse
it **once** into a neutral typed model, and project that model into whichever
portable representation you need.

```
EDGAR ──▶ Arelle ──▶ XbrlModel ──┬──▶ holon.jsonld    (RDF / JSON-LD)
   │                             ├──▶ Tavi            (compiled model)
   │                             ├──▶ xBRL-JSON       (OIM)
   │                             └──▶ property graph  (parquet, .lbug)
   └──▶ primary HTML ──▶ xbrlkit.text ──▶ sections (text blocks, Items, tables)
```

Four projections hang off the model. A fifth surface, the filing's text, reads the
primary HTML document directly and needs neither Arelle nor the network. And the
model itself can be served: `xbrlkit serve` holds a filing in memory and exposes it
to an MCP client through shaped tools (see [Serve](#serve-to-an-mcp-client)).

Arelle stays the parser — nobody should reimplement DTS resolution. What it does
not give you is anything ergonomic to *hold*: `ModelXbrl` is a large mutable
object graph tied to a controller you have to close. `XbrlModel` is the answer to
that — stateless, single-filing, lossless, and the waist every projection hangs
off.

**The one architectural rule:** everything goes through `XbrlModel`. A feature
that reaches into Arelle's `ModelXbrl` directly is bypassing the waist, and that
is the change that turns a kit into a junk drawer.

## Projections

| Target | Status | Notes |
| --- | --- | --- |
| **holon** (`.holon.jsonld`) | shipped | RDF/JSON-LD, renders in the [Holon Viewer](https://holon.robosystems.ai/) |
| **Tavi** (`.tavi.json`) | shipped | [Project Tavi](https://www.xbrl.org/Specification/tavi/PWD-2026-09-01/tavi-PWD-2026-09-01.html) compiled model, PWD-2026-09-01 |
| **OIM** (`.oim.json`) | shipped | xBRL-JSON, checked fact-for-fact against Arelle's own writer |
| **property graph** (`.lbug`, parquet) | shipped | the [RoboSystems](https://robosystems.ai) `sec` graph's tables, ids and DDL, as one LadybugDB file per filing; row-identical to the platform's own processor on a 26-filing corpus |

The OIM projection is the one with a **released reference implementation** to
check against: Arelle's `saveLoadableOIM` writes the same document from the
same filing. A second writer is redundant as a feature — its value is that
every difference is a fidelity bug in the parse or the model, and those same
bugs are otherwise silent in the holon output, which has nothing to check it.
Current parity is every fact on 3M FY2024 (3,150) and Boeing FY2024 (2,688),
and all but one on Microsoft FY2024 (1,855 of 1,856); footnotes are the one
construct the model does not carry.

Tavi is a **public working draft** and its name is explicitly a working title,
so treat that projection as tracking a moving target. It has been diffed,
object class by object class, against the compiled model Arelle's unreleased
`XbrlModel` plugin ([Arelle PR #2418](https://github.com/Arelle/Arelle/pull/2418))
writes for 3M FY2024; the two agree on every fact outside that plugin's own
defects and on every cube. Where the draft left a choice open, the choice and
its reason are recorded in `SPEC_AMBIGUITIES` and carried in the
`.tavi.gaps.json` sidecar `--format tavi` writes alongside the document — the
sidecar also records what the filing carries that the model has nowhere to put,
and that file is the point of the projection, not a by-product of it.

## Property graph

`xbrlkit build --format lpg` (with the `lpg` extra: `pip install "xbrlkit[lpg]"`)
writes the filing as a single-file [LadybugDB](https://github.com/LadybugDB/ladybug)
database with the tables the RoboSystems `sec` graph is built from — the same
node labels, relationship types, columns and ids, declared once in
`xbrlkit.schema` — so Cypher written against the shared graph runs on the file
and a fact in either is the same row. What the platform adds after projection
is not in the file: text blocks stay inline in `Fact.value`, and the enrichment
columns and tables (`canonical_concept`, `canonical_type`, `FactSet`,
`Classification`) are empty. The projection is checked row for row against the
platform's own processor on the Filing Ladder's 26-filing corpus; the two
explained differences are association ids (random on the platform, derived
from the arc here) and exact duplicate arcs inside Arelle's aggregate
`XBRL-dimensions` network, which the derived ids collapse.

```python
from xbrlkit.serialize import to_graph_tables, write_parquet, build_lbug

tables = to_graph_tables(model)          # node and relationship rows, schema order
write_parquet(tables, Path("out/mmm"))   # nodes/*.parquet, relationships/*.parquet
build_lbug(tables, Path("out/mmm.lbug")) # CREATE TABLE … + COPY FROM, one file
```

A host that loads filings through its own Arelle controller — the platform's SEC
adapter does, for its cache policy — calls `xbrlkit.parse.register_sec_transforms()`
to get the SEC inline-XBRL transforms this package vendors, instead of carrying the
EDGAR plugin itself.

## Text

`xbrlkit.text` reads the filing's primary HTML document — no Arelle, no
network — and returns its text as sections:

| Parser | Sections | Notes |
| --- | --- | --- |
| `iXBRLParser` | every inline-XBRL text block (notes, policies, tables), with the XBRL element names it contains | `ix:continuation` chains resolved; nested continuations and nested text blocks included; a concept tagged more than once is one section holding every occurrence; `ix:exclude` page furniture dropped |
| `NarrativeExtractor` | the 10-K / 10-Q Items — Business, Risk Factors, Cybersecurity, Properties, MD&A, Market Risk | table-of-contents rows and cross-references rejected; a 10-Q's Part I and Part II Items kept apart |

Both render HTML tables as markdown pipe tables and split a long section into
balanced parts at paragraph boundaries (`part`, `part_count`, and a `label`
like `"MD&A (2/6)"`) instead of truncating it. Measured on a 26-filing corpus
of 2024–2025 10-Ks and 10-Qs: every text block's full text is carried, where a
map of outermost continuations alone lost 15–29% of the note text on nine of
the filings, and every target Item starts at its body heading.

```python
from xbrlkit.text import iXBRLParser, NarrativeExtractor

html = open("mmm-20241231.htm").read()
for s in iXBRLParser().parse(html):
  print(s.section_id, s.label, s.word_count, s.xbrl_elements[:3])
for s in NarrativeExtractor().extract(html, form_type="10-K"):
  print(s.section_id, s.label, s.word_count)
```

## Install

### As a package

```bash
pip install xbrlkit
```

Exposes the `xbrlkit` CLI (`xbrlkit build …`, `xbrlkit fetch …`, `xbrlkit query …`,
`xbrlkit cache …`, `xbrlkit serve …`)
and the library — use this to consume it from another project. Set your SEC
User-Agent via the environment (see [SEC User-Agent](#sec-user-agent)).

Two optional extras: `xbrlkit[lpg]` for the property-graph projection (pyarrow,
LadybugDB) and `xbrlkit[mcp]` for the local MCP server.

### From source (development)

```bash
# Install the toolchain
brew install uv just

# Install dependencies and provision .env from the template
just install
```

`just install` creates `.env` from `.env.example` on first run — then set your
SEC User-Agent in it.

## SEC User-Agent

SEC EDGAR requires a descriptive `User-Agent` on every request, or it throttles
you (empty responses / HTTP 429). `just install` already created your `.env` —
set your details there:

```bash
# .env
SEC_GOV_USER_AGENT="Your Name your@email.com"
```

`.env` is loaded automatically by every command. Outside the `just` workflow,
`export SEC_GOV_USER_AGENT="Your Name your@email.com"` or pass `--user-agent`.

## Usage

```bash
# Build a holon.jsonld from a specific filing (-> ./output/)
xbrlkit build --cik 320193 --accno 0000320193-23-000106

# The other projections: Tavi (plus its .tavi.gaps.json sidecar), xBRL-JSON,
# the property graph (needs the lpg extra), or every one of them
xbrlkit build --cik 320193 --accno 0000320193-23-000106 --format tavi
xbrlkit build --cik 320193 --accno 0000320193-23-000106 --format oim
xbrlkit build --cik 320193 --accno 0000320193-23-000106 --format lpg
xbrlkit build --cik 320193 --accno 0000320193-23-000106 --format all

# Fetch the latest filing for a ticker (-> ./output/); --form and --n filter
xbrlkit fetch --ticker NVDA

# Query consolidated facts in a built holon (in-memory SPARQL)
xbrlkit query --in output/0000320193-23-000106.holon.jsonld --element us-gaap:Assets
```

From a source checkout, `just` wraps the same CLI as a shorthand:
`just build 320193 0000320193-23-000106` and `just fetch NVDA`.

## Serve to an MCP client

`xbrlkit serve` loads filings into memory and serves them to any MCP client over
Streamable HTTP — Claude Code, Claude Desktop, Cursor, VS Code, or a script with
the MCP SDK. There is no graph and no index behind the tools: every answer is read
from the parsed filing.

```bash
pip install "xbrlkit[mcp]"
xbrlkit serve
# → MCP at http://127.0.0.1:8765/mcp
```

Point the client at the URL — no key, no sign-in — and load filings from the
chat: *"load NVIDIA's latest 10-K"*, *"load `1045810:0001045810-26-000021`"*,
*"load `~/Downloads/mmm-20241231.htm`"*. The `load_filing` tool takes a ticker
with an optional form (`NVDA`, `NVDA 10-Q`), an EDGAR `cik:accession`, a URL, an
inline `.htm`, an instance `.xml`, a filing directory or `.zip`, or a
`model.json` written by `export_filing`. Several filings can be loaded at once,
each under an id; `unload_filing` drops one.

```bash
claude mcp add --transport http xbrlkit http://127.0.0.1:8765/mcp
```

Filings named on the command line are loaded before the server starts:

```bash
xbrlkit serve NVDA                                   # latest 10-K for a ticker
xbrlkit serve "NVDA 10-Q" MMM                        # two filings, by id afterwards
xbrlkit serve ./0000066740-25-000006/                # a filing directory, or a .zip
```

Two switches shape what the tools answer. `--pure` is a faithful reading of the
filing and nothing more: no statement kinds (networks are listed by the filer's
own names), no detected Items, no period buckets, the Filing Ladder's read cap —
the profile a benchmark rung runs under. `--with-document` / `--without-document`
choose whether the text tools read the whole primary document or only the
tagged text blocks; the product profile defaults to the document, `--pure` to
the blocks so the document can be held out as a control. `--as model` names the
representation served; it is the only one in this release, and `tavi`, `holon`,
`lpg` and `files` — the tool sets of the ladder's other rungs — are the next
backends behind the same flag.

```bash
xbrlkit serve --pure ./0000066740-25-000006/         # the form alone
xbrlkit serve --pure --with-document ./0000066740-25-000006/   # + the document
```

### Without installing — `uvx`

`uvx` runs the server straight from PyPI in its own environment, nothing added to
yours. Two things in the `--from` matter: the `[mcp]` extra — `uvx xbrlkit` alone
resolves the package without it, and `serve` stops with a message naming it — and
`@latest`, without which `uvx` keeps reusing the environment it built the first
time and never sees a new release.

```bash
uvx --from "xbrlkit[mcp]@latest" xbrlkit serve NVDA   # pin instead: --from "xbrlkit[mcp]==0.5.0"
```

Clients that launch a server themselves — Claude Desktop, Claude Code's stdio
entries, Cursor — run the same command over stdio:

```json
{
  "mcpServers": {
    "xbrlkit": {
      "command": "uvx",
      "args": ["--from", "xbrlkit[mcp]@latest", "xbrlkit", "serve", "--transport", "stdio", "NVDA"],
      "env": { "SEC_GOV_USER_AGENT": "Your Name your@email.example" }
    }
  }
}
```

```bash
claude mcp add xbrlkit -e SEC_GOV_USER_AGENT="Your Name your@email.example" \
  -- uvx --from "xbrlkit[mcp]@latest" xbrlkit serve --transport stdio NVDA
```

The filings named on the command line load before the server answers its first
request — a cold taxonomy cache can take a minute — so a client with a short
startup timeout does better with no source on the command line and a
`load_filing` call once connected. `SEC_GOV_USER_AGENT` is needed for anything
EDGAR has to fetch (a ticker, a `cik:accession`); a local file needs none.

Any XBRL Arelle can load works — US GAAP, IFRS / ESEF, tagged ACFRs — and so
does the rest of EDGAR. Three kinds of filing load, and `describe_filing`'s
`profile` says which one you have:

| kind | what it is | how it reads |
|---|---|---|
| **XBRL** | 10-K, 10-Q, 20-F, IFRS / ESEF, ACFRs — inline or classic | facts, networks and text; the whole toolset |
| **XML** | the forms with no XBRL: ownership (3, 4, 5), 13F, N-PORT, SC 13D/G | `records` returns the form's own tables; searchable as text |
| **document** | an 8-K, a proxy, a registration statement | text only — `search_text` and `read_text` |

A **classic** (pre-inline) filing's narrative lives outside its XBRL package —
`form10-k.htm` is a sibling of the instance, not part of it — so the document is
fetched alongside and the instance's tagged blocks are located within it by
matching their prose. Without that, every filing before iXBRL reads as tagged
blocks alone: no Items, no MD&A, no cover page.

The tools are the shapes a reader needs, not a query language:

| tool | what it answers |
|---|---|
| `describe_filing` | how the filing is laid out: entity, periods (with the keys the other tools use), statements and disclosures by role, dimensional axes, text sections with offsets — call it first |
| `resolve_element` | which concepts the filing reports for a phrase ("revenue", "lease liability"), ranked, with fact counts and where they appear |
| `fact_grid` | values by concept and period — the consolidated total by default (no dimensional qualifier, the most precise of duplicate tags), member breakdowns on request |
| `statement` | one presentation network as a table: rows in filing order with preferred labels, values per period column |
| `calculation` | what sums to a total: the calculation children with weights, computed against reported, per period |
| `records` | an XML filing's own tables — a Form 4's transactions and holdings, a 13F's positions — as rows, with the header fields beside them |
| `search_text`, `read_text` | regex search over the readable text — the whole primary document, or the tagged text blocks alone — and paging from an offset |
| `export_filing` | the filing as holon, Tavi, xBRL-JSON, a LadybugDB file, or `model` (the parse itself, reloadable without Arelle), written under `--out-dir` |
| `list_filings`, `load_filing`, `unload_filing` | the session |

The server binds to the loopback interface with the SDK's host- and
origin-header validation on, so a page in a browser cannot drive it; `--host`
opens it to a network you trust, and `--transport stdio` serves clients that
speak nothing else. The same tool functions are importable without MCP
(`xbrlkit.serve.tools`) for tests and notebooks.

## EDGAR

`xbrlkit.edgar` is the fetch layer the CLI uses, exposed for hosts that discover
and download filings themselves: synchronous `requests`, local-filesystem output,
and EDGAR's two throttle signatures — a 429, and an empty 200 — ridden out with
a bounded wait-and-retry (`EdgarThrottled` when the budget is spent).

| | |
| --- | --- |
| `EdgarClient` | ticker → CIK, a company's filing list (`list_filings`, by form), `company_info`, one filing by accession (`get_filing_ref`) |
| `EftsClient` / `query_efts` | bulk discovery through EDGAR full-text search: by form, year or quarter, across every filer |
| `download_filing` / `fetch` | the XBRL zip for one accession, unpacked to a directory |

```python
from pathlib import Path
from xbrlkit.edgar import EdgarClient, download_filing

client = EdgarClient()
cik = client.ticker_to_cik("MMM")
latest = client.list_filings(cik, forms=["10-K"])[0]
package = download_filing(client, cik, latest.accession, Path("data"))  # the Arelle load target
```

The SEC User-Agent is required here as everywhere (see [SEC User-Agent](#sec-user-agent)).

## Arelle cache

Arelle resolves a filing's DTS by fetching every schema and linkbase it imports —
the XBRL core from xbrl.org, the W3C schemas from w3.org, `dei` / `srt` / `ecd` /
country / currency from xbrl.sec.gov, the us-gaap year from xbrl.fasb.org. A 10-K
resolves to a few hundred files, and the two smallest hosts throttle a cold cache
within a few dozen filings. So `load_model` serves the DTS from a persistent cache
(`~/.cache/xbrlkit/arelle`, or `$XBRLKIT_ARELLE_CACHE_DIR`) in Arelle's own layout,
spaces its fetches per host, waits out a `Retry-After` on a 429 or 503, and —
when a document still cannot be resolved — raises `DtsResolutionError` naming the
URLs rather than returning a filing that parses with holes.

Warm the cache once, or ship it:

```bash
xbrlkit cache status                          # what the cache holds; exit 1 if unseeded
xbrlkit cache download --years 2022-2026      # load the standard entry points through Arelle
xbrlkit cache bundle --out schemas.tar.gz --host www.xbrl.org --host www.w3.org
xbrlkit cache extract --bundle schemas.tar.gz # seed a container's cache at build time
```

`XBRLKIT_ARELLE_OFFLINE=1` (or `load_model(..., offline=True)`) never touches the
network; a miss is then an error, not a fetch. A host that builds its own Arelle
controller gets the same policy from `xbrlkit.parse.configure_webcache(cntlr, cache_dir)`.

## Where it runs

**RoboSystems.** The platform's SEC pipeline is built on this package: filings are
parsed with `xbrlkit.parse` (the platform's own Arelle controller, with
`register_sec_transforms` and the cache policy from `configure_webcache`), projected
with `to_holon`, `to_tavi_report` and the property-graph tables, the shared `sec`
graph is declared from `xbrlkit.schema`, and the full-text index behind its document
search is built from `xbrlkit.text`.

**Filing Ladder.** The [Filing Ladder](https://github.com/HarbingerFinLab/filing-ladder)
benchmark — one filing handed to the same language model in every representation —
built its 26-filing corpus of 2024–2025 10-Ks and 10-Qs with this package: the Tavi
compiled model, the holon, the per-filing property graph, and both text parsers. Each
projection is a rung of the ladder, so the
[v0.1 results](https://github.com/HarbingerFinLab/filing-ladder/blob/main/results/v0.1-sonnet-5/README.md)
are also a measurement of what a model can do with each of these outputs. Before that
run, every text-block section the parsers produce was checked against the filing's own
text-block facts as Arelle resolves them, on all 26 filings, and the property-graph
projection was checked row for row against the platform's processor. The two defects
those checks found in the text layer were fixed in 0.4.1 and are disclosed in the
benchmark's protocol.

## View & explore

Built holons render in the **RoboSystems Holon Viewer** — a browser-based reader
that renders the financial statements and lets you ask questions of the report
with AI:

- **Hosted:** <https://holon.robosystems.ai/> — open a `holon.jsonld` and explore
  the statements, notes, and dimensional facts, or chat with the report.
- **Source:** <https://github.com/RoboFinSystems/robosystems-holon-viewer> — run
  it locally or self-host.

The viewer reads a holon entirely client-side, so a single `holon.jsonld` is a
complete, portable, self-describing report. The viewer's chat asks the report raw
questions (jq over a Tavi model, SPARQL over a holon); `xbrlkit serve` is the other
side of that pair — the same filing behind shaped tools, on your own machine.

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

MIT © 2026 RFS LLC
