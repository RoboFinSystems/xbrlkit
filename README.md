# xbrlkit

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

Work with XBRL filings above [Arelle](https://arelle.org): fetch a filing, parse
it **once** into a neutral typed model, and project that model into whichever
portable representation you need — or hand it one of those representations and
get the model back.

```
  EDGAR ───────────┐
  filings.xbrl.org ├──▶ Arelle ──▶ XbrlModel ──┬──▶ holon.jsonld    (RDF / JSON-LD)
  XBRL zip / iXBRL ┘                 ▲         ├──▶ TAVI            (compiled model)
                                     │         ├──▶ xBRL-JSON       (OIM)
                   holon, TAVI ──────┘         └──▶ property graph  (parquet, .lbug)

                   primary HTML ──▶ xbrlkit.text ──▶ sections (text blocks, Items, tables)

                   holon, TAVI ──▶ xbrlkit view ──▶ the report, rendered in a browser
```

Three ways in — the SEC, everyone else through
[filings.xbrl.org](https://filings.xbrl.org), and **the filing itself**: an
XBRL package or archive (`.zip`), an iXBRL document (`.htm`), a bare instance
(`.xml`), a filing directory, or an `http(s)` URL to any of them. Nothing about
the middle of this requires EDGAR, or a regulator at all — a report that was
never filed with anybody parses like one that was.

Four projections out, and two of those read back, so a report that was never an
SEC filing gets the same treatment. A fifth surface, the filing's text, reads
the primary HTML directly and needs neither Arelle nor the network. The model
itself can be served: `xbrlkit serve` holds a filing in memory and exposes it
to an MCP client through shaped tools. And `xbrlkit view` puts it on screen.

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
| [**`serialize`**](https://github.com/RoboFinSystems/xbrlkit/blob/main/xbrlkit/serialize/README.md) | the four projections | holon, TAVI (+ its gap report), xBRL-JSON, the property graph |
| [**`deserialize`**](https://github.com/RoboFinSystems/xbrlkit/blob/main/xbrlkit/deserialize/README.md) | the importers | a holon or a TAVI read back into the model, no Arelle |
| [**`edgar`**](https://github.com/RoboFinSystems/xbrlkit/blob/main/xbrlkit/edgar/README.md) | the SEC | discovery, download, full-text search, 1994 onward |
| [**`filings_org`**](https://github.com/RoboFinSystems/xbrlkit/blob/main/xbrlkit/filings_org/README.md) | everyone else | ESEF and the national regimes, by LEI |
| [**`text`**](https://github.com/RoboFinSystems/xbrlkit/blob/main/xbrlkit/text/README.md) | the filing as prose | inline text blocks, 10-K/10-Q Items, the XML forms |
| [**`serve`**](https://github.com/RoboFinSystems/xbrlkit/blob/main/xbrlkit/serve/README.md) | the local MCP server | sixteen shaped tools over a filing in memory |

`model.py` is the waist itself, `schema/` declares the property graph's tables,
`query.py` runs SPARQL over a built holon, and `view.py` is the loopback server
behind `xbrlkit view` and the `view_filing` tool.

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

SEC fair access asks for a `User-Agent` identifying you with contact info.
EDGAR works out of the box under a default that names the project, and the
first unattributed fetch says so once — SEC rate limits per IP, so the shared
default costs nobody else their budget. Identifying yourself is a courtesy,
and one worth extending. `just install` already created your `.env`:

```bash
# .env
SEC_GOV_USER_AGENT="Your Name your@email.com"
```

`.env` is loaded automatically by every command **run from a checkout of this
repo** — the lookup is relative to the installed code, not your working
directory, so a `uvx` or `pip` install never picks one up. There, use
`export SEC_GOV_USER_AGENT=…`, `--user-agent`, or an MCP `env` block (see
[Serve to an MCP client](#serve-to-an-mcp-client)). Nothing outside EDGAR
needs it — a local file, a JSON report and filings.xbrl.org all load without.

## Usage

```bash
# Build a holon.jsonld from a specific filing (-> ./output/)
xbrlkit build --cik 320193 --accno 0000320193-23-000106

# The other projections: TAVI (plus its .tavi.gaps.json sidecar), xBRL-JSON,
# the property graph (needs the lpg extra), or every one of them
xbrlkit build --cik 320193 --accno 0000320193-23-000106 --format tavi
xbrlkit build --cik 320193 --accno 0000320193-23-000106 --format all

# Fetch the latest filing for a ticker (-> ./output/); --form and --n filter
xbrlkit fetch --ticker NVDA

# Query consolidated facts in a built holon (in-memory SPARQL)
xbrlkit query --in output/0000320193-23-000106.holon.jsonld --element us-gaap:Assets

# Open a filing as a rendered report in the browser — no account, no download
xbrlkit view NVDA

# The filing itself needs no EDGAR and no network — an XBRL .zip, an iXBRL
# .htm, a bare instance .xml, a filing directory. `serve` and `view` take any
# source; `build` and `fetch` are the EDGAR path
xbrlkit view ./report.zip
xbrlkit serve ./mmm-20241231.htm
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

Two ways to run it. They differ in which process does the fetching, and so in
where your SEC identity goes.

**stdio — the client launches the server.** The identity belongs in the
server's own `env` block:

```json
{
  "mcpServers": {
    "xbrlkit": {
      "command": "uvx",
      "args": [
        "--from", "xbrlkit[mcp]@latest",
        "xbrlkit", "serve", "--transport", "stdio"
      ],
      "env": { "SEC_GOV_USER_AGENT": "Your Name you@example.com" }
    }
  }
}
```

**HTTP — you start the server, the client only points at a URL.** An `env`
block in the client config would reach nothing here; set it on the command:

```bash
pip install "xbrlkit[mcp]"
SEC_GOV_USER_AGENT="Your Name you@example.com" xbrlkit serve
# → MCP at http://127.0.0.1:8765/mcp

# or without installing anything
SEC_GOV_USER_AGENT="Your Name you@example.com" \
  uvx --from "xbrlkit[mcp]@latest" xbrlkit serve
```

```json
{
  "mcpServers": {
    "xbrlkit": { "type": "http", "url": "http://127.0.0.1:8765/mcp" }
  }
}
```

or, equivalently:

```bash
claude mcp add --transport http xbrlkit http://127.0.0.1:8765/mcp
```

A `.env` file is **not** a channel for either of these. The lookup is relative
to the installed code rather than your working directory, so it resolves only
inside a checkout of this repo — a `uvx` or `pip` install never sees one. Use
the environment, the `env` block, or `--user-agent`.

Both are optional: EDGAR works unattributed under the default, saying so once.
And filings.xbrl.org, local packages and TAVI/holon JSON need no identity at all.

Then load filings from the chat — a ticker, an EDGAR `cik:accession`, a
`lei:`, a local package, or a holon or TAVI by path or URL — and ask for
statements, facts by concept and period, calculations, exhibits and text.
No graph and no database sits behind any of it: every answer about a filing is
read from that filing. The one outward call is `search_filings`, which asks
EDGAR's own full-text index which filings to go and read. Full detail, including the tool table and the `--pure` profile, in
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
projection is a rung of the ladder, so its
[published results](https://github.com/HarbingerFinLab/filing-ladder/blob/main/results/README.md)
are also a measurement of what a model can do with each of these outputs. That
corpus is this package's test bench too: the text sections were checked against
the filing's own text-block facts on all 26 filings, the property graph row for
row against the platform's processor, and the two importers by round trip.

## View & explore

Built holons and TAVI models render in the **xbrlkit viewer** — the browser
side of the toolkit, a reader that renders the financial statements and lets
you ask questions of the report with AI:

- **Hosted:** <https://xbrlkit.com/> — open a `holon.jsonld` or a `tavi.json`
  and explore the statements, notes and dimensional facts, or chat with the
  report.
- **Source:** <https://github.com/RoboFinSystems/xbrlkit-viewer>

The viewer reads a holon entirely client-side, so a single `holon.jsonld` is a
complete, portable, self-describing report. Its chat asks the report raw
questions (jq over a TAVI model, SPARQL over a holon); `xbrlkit serve` is the
other side of that pair — the same filing behind shaped tools, on your own
machine.

**`xbrlkit view` joins the two.** It resolves a filing the way `serve` does,
serializes it, and hands that one document to the viewer:

```bash
xbrlkit view NVDA                       # the latest 10-K, rendered in a browser tab
xbrlkit view "NVDA 10-Q" --as tavi      # a different form, a different serialization
xbrlkit view 320193:0000320193-23-000106
xbrlkit view lei:549300E9PC51EN656011   # a filer outside EDGAR
xbrlkit view output/x.holon.jsonld      # a document you already have, verbatim
xbrlkit view NVDA --no-open             # print the link instead of opening it
```

Without installing anything:

```bash
uvx xbrlkit view NVDA
```

A browser cannot be handed a local path — `file://` is unreachable from an
https page, and a file input cannot be pre-populated — so this serves the
document instead, on an ephemeral loopback port with an unguessable path, and
opens `xbrlkit.com/?url=…` pointing at it. `http://127.0.0.1` is a
potentially trustworthy origin, so the https page may read it; the CORS header
names the viewer's origin and no other. The document is readable there, by that
origin, until you press Ctrl-C. `--viewer` points at a different build.

From an MCP client the same thing is the `view_filing` tool: *"load NVDA"*,
then *"show me it"*.

## License

MIT © 2026 RFS LLC — see [LICENSE](LICENSE).
