# xbrlkit

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

Work with XBRL filings above [Arelle](https://arelle.org): fetch a filing, parse
it **once** into a neutral typed model, and project that model into whichever
portable representation you need — or hand it one of those representations and
get the model back.

```
  EDGAR ───────────┐
                   ├──▶ Arelle ──▶ XbrlModel ──┬──▶ holon.jsonld    (RDF / JSON-LD)
  filings.xbrl.org ┘                 ▲         ├──▶ Tavi            (compiled model)
                                     │         ├──▶ xBRL-JSON       (OIM)
                   holon, Tavi ──────┘         └──▶ property graph  (parquet, .lbug)

                   primary HTML ──▶ xbrlkit.text ──▶ sections (text blocks, Items, tables)
```

Two sources in — the SEC, and everyone else through
[filings.xbrl.org](https://filings.xbrl.org) — four projections out, and two of
those read back, so a report that was never an SEC filing gets the same
treatment. A fifth surface, the filing's text, reads the primary HTML directly
and needs neither Arelle nor the network. And the model itself can be served:
`xbrlkit serve` holds a filing in memory and exposes it to an MCP client
through shaped tools.

Arelle stays the parser — nobody should reimplement DTS resolution. What it
does not give you is anything ergonomic to *hold*: `ModelXbrl` is a large
mutable object graph tied to a controller you have to close. `XbrlModel` is the
answer to that — stateless, single-filing, lossless, and the waist every
projection hangs off.

**The one architectural rule:** everything goes through `XbrlModel`. A feature
that reaches into Arelle's `ModelXbrl` directly is bypassing the waist, and
that is the change that turns a kit into a junk drawer.

## What's in the box

| | | |
| --- | --- | --- |
| [**`parse`**](https://github.com/RoboFinSystems/xbrlkit/blob/main/xbrlkit/parse/README.md) | Arelle in, `XbrlModel` out | the load, the DTS cache policy, taxonomy packages |
| [**`serialize`**](https://github.com/RoboFinSystems/xbrlkit/blob/main/xbrlkit/serialize/README.md) | the four projections | holon, Tavi (+ its gap report), xBRL-JSON, the property graph |
| [**`deserialize`**](https://github.com/RoboFinSystems/xbrlkit/blob/main/xbrlkit/deserialize/README.md) | the importers | a holon or a Tavi read back into the model, no Arelle |
| [**`edgar`**](https://github.com/RoboFinSystems/xbrlkit/blob/main/xbrlkit/edgar/README.md) | the SEC | discovery, download, full-text search, 1994 onward |
| [**`filings_org`**](https://github.com/RoboFinSystems/xbrlkit/blob/main/xbrlkit/filings_org/README.md) | everyone else | ESEF and the national regimes, by LEI |
| [**`text`**](https://github.com/RoboFinSystems/xbrlkit/blob/main/xbrlkit/text/README.md) | the filing as prose | inline text blocks, 10-K/10-Q Items, the XML forms |
| [**`serve`**](https://github.com/RoboFinSystems/xbrlkit/blob/main/xbrlkit/serve/README.md) | the local MCP server | fourteen shaped tools over a filing in memory |

`model.py` is the waist itself, `schema/` declares the property graph's tables,
and `query.py` runs SPARQL over a built holon.

## Install

```bash
pip install xbrlkit
```

Exposes the `xbrlkit` CLI (`build`, `fetch`, `query`, `cache`, `serve`) and the
library. Two optional extras: `xbrlkit[lpg]` for the property-graph projection
(pyarrow, LadybugDB) and `xbrlkit[mcp]` for the MCP server.

From a source checkout:

```bash
brew install uv just
just install     # dependencies, and .env from the template
```

### SEC User-Agent

SEC EDGAR requires a descriptive `User-Agent` on every request, or it throttles
you (empty responses / HTTP 429). `just install` already created your `.env` —
set your details there:

```bash
# .env
SEC_GOV_USER_AGENT="Your Name your@email.com"
```

`.env` is loaded automatically by every command. Outside the `just` workflow,
`export SEC_GOV_USER_AGENT=…` or pass `--user-agent`. Nothing outside EDGAR
needs it — a local file, a JSON report and filings.xbrl.org all load without.

## Usage

```bash
# Build a holon.jsonld from a specific filing (-> ./output/)
xbrlkit build --cik 320193 --accno 0000320193-23-000106

# The other projections: Tavi (plus its .tavi.gaps.json sidecar), xBRL-JSON,
# the property graph (needs the lpg extra), or every one of them
xbrlkit build --cik 320193 --accno 0000320193-23-000106 --format tavi
xbrlkit build --cik 320193 --accno 0000320193-23-000106 --format all

# Fetch the latest filing for a ticker (-> ./output/); --form and --n filter
xbrlkit fetch --ticker NVDA

# Query consolidated facts in a built holon (in-memory SPARQL)
xbrlkit query --in output/0000320193-23-000106.holon.jsonld --element us-gaap:Assets
```

From a source checkout, `just` wraps the same CLI: `just build 320193
0000320193-23-000106` and `just fetch NVDA`.

```python
from xbrlkit.parse import load_model, to_xbrl_model
from xbrlkit.serialize import to_holon, to_tavi_report
from xbrlkit.deserialize import from_holon_json

model = to_xbrl_model(load_model("mmm-20241231.htm"), filing_meta)
holon = to_holon(model)
tavi, gaps = to_tavi_report(model)
model = from_holon_json(holon)          # and back again
```

## Serve to an MCP client

```bash
pip install "xbrlkit[mcp]"
xbrlkit serve
# → MCP at http://127.0.0.1:8765/mcp

claude mcp add --transport http xbrlkit http://127.0.0.1:8765/mcp
```

Or without installing anything:

```bash
uvx --from "xbrlkit[mcp]@latest" xbrlkit serve
```

Then load filings from the chat — a ticker, an EDGAR `cik:accession`, a
`lei:`, a local package, or a holon or Tavi by path or URL — and ask for
statements, facts by concept and period, calculations, exhibits and text.
There is no graph and no index behind the tools: every answer is read from the
filing. Full detail, including the tool table and the `--pure` profile, in
[`serve/`](https://github.com/RoboFinSystems/xbrlkit/blob/main/xbrlkit/serve/README.md).

## Where it runs

**RoboSystems.** The platform's SEC pipeline is built on this package: filings
are parsed with `xbrlkit.parse` (its own Arelle controller, with
`register_sec_transforms` and the cache policy from `configure_webcache`),
projected with `to_holon`, `to_tavi_report` and the property-graph tables, the
shared `sec` graph is declared from `xbrlkit.schema`, and the full-text index
behind its document search is built from `xbrlkit.text`.

**Filing Ladder.** The
[Filing Ladder](https://github.com/HarbingerFinLab/filing-ladder) benchmark —
one filing handed to the same language model in every representation — built
its 26-filing corpus of 2024–2025 10-Ks and 10-Qs with this package. Each
projection is a rung of the ladder, so the
[v0.1 results](https://github.com/HarbingerFinLab/filing-ladder/blob/main/results/v0.1-sonnet-5/README.md)
are also a measurement of what a model can do with each of these outputs. That
corpus is this package's test bench too: the text sections were checked against
the filing's own text-block facts on all 26 filings, the property graph row for
row against the platform's processor, and the two importers by round trip.

## View & explore

Built holons render in the **RoboSystems Holon Viewer** — a browser-based
reader that renders the financial statements and lets you ask questions of the
report with AI:

- **Hosted:** <https://holon.robosystems.ai/> — open a `holon.jsonld` and
  explore the statements, notes and dimensional facts, or chat with the report.
- **Source:** <https://github.com/RoboFinSystems/robosystems-holon-viewer>

The viewer reads a holon entirely client-side, so a single `holon.jsonld` is a
complete, portable, self-describing report. Its chat asks the report raw
questions (jq over a Tavi model, SPARQL over a holon); `xbrlkit serve` is the
other side of that pair — the same filing behind shaped tools, on your own
machine.

## License

MIT © 2026 RFS LLC — see [LICENSE](LICENSE).
