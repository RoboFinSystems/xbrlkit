# Serve — the local MCP server

`xbrlkit serve` loads filings into memory and serves them to any MCP client
over Streamable HTTP — Claude Code, Claude Desktop, Cursor, VS Code, or a
script with the MCP SDK. There is no graph and no index behind the tools:
**every answer is read from the parsed filing.**

```bash
pip install "xbrlkit[mcp]"
xbrlkit serve
# → MCP at http://127.0.0.1:8765/mcp

claude mcp add --transport http xbrlkit http://127.0.0.1:8765/mcp
```

Point the client at the URL — no key, no sign-in — and load filings from the
chat: *"load NVIDIA's latest 10-K"*, *"load `1045810:0001045810-26-000021`"*,
*"load `~/Downloads/mmm-20241231.htm`"*.

## What `load_filing` takes

| Source | Example |
| --- | --- |
| a ticker, with an optional form | `NVDA`, `NVDA 10-Q` |
| an EDGAR `cik:accession` | `1045810:0001045810-26-000021` |
| a **LEI**, or a filings.xbrl.org filing id | `lei:213800H2PQMIF3OVZY47` |
| a local filing | an inline `.htm`, an instance `.xml`, a directory, a `.zip` or `.xbri` package |
| a **taxonomy** published on its own, by path or URL | `us-gaap-2025.zip`, `https://xbrl.fasb.org/us-gaap/2026/us-gaap-2026.zip`, an unpacked taxonomy directory |
| a **JSON report**, by path or URL | `.clawdog.jsonld`, `.tavi.json`, `.holon.jsonld`, or a `model.json` from `export_filing` |
| a URL Arelle can load | any of the above on the web |

The two indexes behind the first three are [`edgar/`](../edgar/README.md) and
[`filings_org/`](../filings_org/README.md); the JSON reports are read by
[`deserialize/`](../deserialize/README.md), with no Arelle and no taxonomy
fetch. Several filings load at once, each under an id; `unload_filing` drops
one.

### A ticker or `cik:accession` reads RoboSystems' parse first

For an SEC filing named by ticker or `cik:accession`, the server first looks
for it on the RoboSystems public data CDN (`https://public.robosystems.ai`),
where the RoboSystems SEC pipeline publishes each filing it has processed as a
TAVI model and a holon. That loads in about a second; parsing the filing from
EDGAR with Arelle takes 30 to 60. When the CDN has no copy, it parses from
EDGAR.

The published copy is **RoboSystems' parse of the filing, not the filing as
filed**. The server reads the holon, the complete report, and falls back to the
TAVI model. Either lacks Arelle's fact hashes (so fact ids differ from an EDGAR
parse) and concept references; the TAVI fallback also drops the definition arcs
a hypercube cannot express. It is as current as the xbrlkit that processed it. So the answer says which one you have: `load_filing` and `describe_filing`
return `read_from`, `{"kind": "published", "url": …}` for a CDN copy, or
`edgar`, `file`, `url` or `filings.xbrl.org` otherwise.

A filing loaded without a taxonomy whose host has gone — every Danish ESEF
report cites one — says that too: `missing_taxonomy` names the documents and
counts the facts that are in the report and not in the model.

To read the filing itself, parse it from EDGAR:

- `--pure` does so by default (see below).
- `XBRLKIT_ARTIFACTS_URL=""` turns the CDN off in either profile; set it to
  another URL to read published copies from there.

A zip or directory with no report in it is a taxonomy, and loads from an
entry point: the first one its `META-INF/taxonomyPackage.xml` lists
(FASB's US GAAP package lists `entire/us-gaap-entryPoint-all` first), or —
with no manifest, as GASB's exposure drafts ship — the one schema nothing else
in the package imports. The receipt's `taxonomy` names the entry point loaded
and the others on offer; `entry_point` on `load_filing` picks another, by its
path, file name or name. A taxonomy answers with its concepts and networks —
`resolve_element`, `disclosures`, `statement`, `information_block` — and no
facts. An elements-only entry point (a schema with no linkbases) has no
networks to read, and is refused as such.

## The tools

The tools are the shapes a reader needs, not a query language.

| tool | what it answers |
| --- | --- |
| `describe_filing` | how the filing is laid out: entity, periods (with the keys the other tools use), statements and disclosures by role, dimensional axes, text sections with offsets — **call it first** |
| `resolve_element` | which concepts the filing reports for a phrase ("revenue", "lease liability"), ranked, with fact counts and where they appear |
| `fact_grid` | values by concept and period — the consolidated total by default (no dimensional qualifier, the most precise of duplicate tags), member breakdowns on request |
| `statement` | one presentation network as a table: rows in filing order with preferred labels, values per period column |
| `calculation` | what sums to a total: the calculation children with weights, computed against reported, per period |
| `disclosures` | the filing's blocks as families read from the filer's own role titles — each note with its policies, tables and details, each statement with its parenthetical, the cover page — as a list with counts, or one family's index by topic; the cheap call |
| `information_block` | one block read whole: rows in filing order with consolidated values, the same rows by the block's own axes, its calculation arcs footed per period, and its text blocks with offsets — the expensive call, after `disclosures` |
| `documents`, `read_document` | what else was filed — exhibits, an 8-K's press release, a 13F's holdings table — each with its URL and whether it reads natively; and reading one |
| `records` | an XML filing's own tables — a Form 4's transactions and holdings, a 13F's positions — as rows, with the header fields beside them |
| `search_text`, `read_text` | regex search over the readable text — the whole primary document, or the tagged text blocks alone — and paging from an offset. A pattern that matches more than came back also says where the matches fall, by section, busiest first, and how many further sections hold the rest; one that matches nothing counts its own words separately, so a phrase the filer words differently is a step rather than a dead end |
| `export_filing` | the filing as ClawDog, holon, TAVI, xBRL-JSON, a LadybugDB database (the graph to query), an experimental icebug-disk tree, or `model` (the parse itself, reloadable without Arelle), written under `--out-dir` (`~/xbrlkit/output` by default). With `filings`, the two graph formats stack several loaded filings into one graph — a company's years, or peers — keeping what they share once and every fact on its own report |
| `run_cypher` | one read-only Cypher query over a graph `export_filing` wrote, single or stacked, with a 60-second limit and at most 200 rows. **Listed only when LadybugDB is installed** (`xbrlkit[lpg]`): without it there is no graph to query |
| `view_filing` | the filing rendered as a report in the browser: it is serialized, served from an unguessable path on loopback (readable only by the viewer's origin, for as long as this server runs), and the link comes back to hand to the user |
| `search_filings` | which filings across EDGAR match a phrase, form, date range or filer — the discovery step before `load_filing`, since every hit carries the `cik:accession` that loads it. Returns a page and the total matched; EDGAR's full-text index begins in 2001 |
| `list_filings`, `load_filing`, `unload_filing` | the session |

The same functions are importable without MCP (`xbrlkit.serve.tools`) for tests
and notebooks, and the Cypher runner on its own as `xbrlkit.cypher`.

## Three kinds of filing

`describe_filing`'s `profile` says which one you have:

| kind | what it is | how it reads |
| --- | --- | --- |
| **XBRL** | 10-K, 10-Q, 20-F, IFRS / ESEF, ACFRs — inline or classic | facts, networks and text; the whole toolset |
| **XML** | the forms with no XBRL: ownership (3, 4, 5), 13F, N-PORT, SC 13D/G | `records` returns the form's own tables; searchable as text |
| **document** | an 8-K, a proxy, a registration statement, anything pre-2000 | text only — `search_text` and `read_text` |

A JSON report reads as **XBRL** with no document behind it, so the text tools
answer over its tagged text blocks.

## The 8-K is the exception worth knowing

An 8-K is a wrapper, and the wrapper is what carries the XBRL. A typical
earnings 8-K: **about 20 facts, all cover-page, no numeric facts, no
statements, a few thousand characters of text** — while the earnings release
attached to it as `EX-99.1` runs to hundreds of kilobytes. `fact_grid` and `statement` have nothing
to say about a filing like that, and the answer is never in the tagged content.

**EDGAR says which 8-K is which, and `describe_filing` now returns it.** The
submissions record carries item numbers per filing, and they are the SEC's own
classification of what the report is about:

| item | what it means | where the substance is |
| --- | --- | --- |
| **2.02** | Results of Operations and Financial Condition | **the earnings release**, normally `EX-99.1` |
| 7.01 | Regulation FD Disclosure | an exhibit — a deck, a script, a release |
| 9.01 | Financial Statements and Exhibits | says exhibits exist, not what they are |

So an earnings 8-K is identifiable **before** anything is fetched:

```json
"items": [{"item": "2.02", "name": "Results of Operations and Financial Condition"}, …],
"items_note": "Item 2.02 — this is an earnings release. The results are in the
               attached exhibit (usually EX-99.1), not in this filing's XBRL …"
```

and `next` leads with `documents` rather than the fact tools.

**Why this matters more than a routing convenience.** The exhibit carries what
the company leads with — adjusted EBITDA, non-GAAP margin, segment detail,
guidance. Those are not untagged by oversight; **no XBRL taxonomy holds them**,
so the structured filing cannot contain the answer. And the release lands weeks
before the 10-Q that restates part of it in GAAP. For a reader who wants to know
what a company just reported, an Item 2.02 exhibit is a primary source, not a
supplement to the periodic report — and it is the natural companion to the MD&A,
which is untagged in the 10-Q too.

## Two switches shape the answers

`--pure` is a faithful reading of the filing and nothing more: no statement
kinds (networks are listed by the filer's own names), no detected Items, no
period buckets, the Filing Ladder's read cap — the profile a benchmark rung
runs under. It parses a ticker or `cik:accession` from EDGAR rather than
reading RoboSystems' published parse, unless `XBRLKIT_ARTIFACTS_URL` is set.

`--with-document` / `--without-document` choose whether the text tools read the
whole primary document or only the tagged text blocks; the product profile
defaults to the document, `--pure` to the blocks so the document can be held
out as a control.

```bash
xbrlkit serve --pure ./0000066740-25-000006/                   # the form alone
xbrlkit serve --pure --with-document ./0000066740-25-000006/   # + the document
```

`--as model` names the representation served; it is the only one in this
release, and `tavi`, `holon`, `lpg` and `files` are the next backends behind
the same flag.

## Filings on the command line

```bash
xbrlkit serve NVDA                     # latest 10-K for a ticker
xbrlkit serve "NVDA 10-Q" MMM          # two filings, by id afterwards
xbrlkit serve ./0000066740-25-000006/  # a filing directory, or a .zip
```

They load *before* the server answers its first request — a cold taxonomy
cache can take a minute — which is why **the launch commands above name no
filing**: an empty server answers immediately and the first `load_filing` call
decides what to open. Name one on the command line when you want it warm and
the client will wait.

## Without installing — `uvx`

```bash
uvx --from "xbrlkit[mcp]@latest" xbrlkit serve   # pin instead: --from "xbrlkit[mcp]==0.6.0"
```

Two things in the `--from` matter: the **`[mcp]` extra** — `uvx xbrlkit` alone
resolves the package without it, and `serve` stops with a message naming it —
and **`@latest`**, without which `uvx` keeps reusing the environment it built
the first time and never sees a new release. The `mcp` extra alone serves every
tool but `run_cypher`; add `lpg` for the graph tools — `--from
"xbrlkit[mcp,lpg]@latest"` — which also lets `export_filing` write `.lbdb` and
icebug-disk graphs.

Clients that launch a server themselves run the same command over stdio:

```json
{
  "mcpServers": {
    "xbrlkit": {
      "command": "uvx",
      "args": ["--from", "xbrlkit[mcp]@latest", "xbrlkit", "serve", "--transport", "stdio"],
      "env": { "SEC_GOV_USER_AGENT": "Your Name your@email.example" }
    }
  }
}
```

```bash
claude mcp add xbrlkit -e SEC_GOV_USER_AGENT="Your Name your@email.example" \
  -- uvx --from "xbrlkit[mcp]@latest" xbrlkit serve --transport stdio
```

`SEC_GOV_USER_AGENT` is needed for anything EDGAR has to fetch (a ticker, a
`cik:accession`); a local file needs none.

**Where exports go.** A client that launches the server also picks its working
directory: Claude Desktop starts it in `/`, which is not writable, and Claude
Code starts it in whatever project is open. So `export_filing` writes to
`~/xbrlkit/output` unless `--out-dir` names another folder:

```json
"args": ["--from", "xbrlkit[mcp,lpg]@latest", "xbrlkit", "serve", "--transport", "stdio",
         "--out-dir", "/Users/you/xbrlkit-exports"]
```

The server prints the folder when it starts, every export returns its absolute
path, and a folder it cannot write comes back as an error that says to set
`--out-dir`.

## Security

The server binds to the loopback interface with the SDK's host- and
origin-header validation on, so a page in a browser cannot drive it. `--host`
opens it to a network you trust, and `--transport stdio` serves clients that
speak nothing else. There is no authentication: it is a local tool over local
files.
