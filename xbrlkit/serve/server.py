"""The MCP server: the tools in :mod:`xbrlkit.serve.tools` registered on an
``MCPServer`` and run over Streamable HTTP on localhost (or stdio).

No authentication, by design: the server runs on the user's own machine over
their own filings, and the transport binds to the loopback interface with
the SDK's host- and origin-header validation on, so a page in a browser
cannot drive it. Point an MCP client at ``http://127.0.0.1:8765/mcp``.

Two switches shape what the tools return:

- ``pure`` — a faithful reading of the filing and nothing more: no statement
  kinds, no detected Items, no period buckets, the Filing Ladder's read cap.
  The profile a benchmark rung runs under; the default is the product
  profile, which keeps those conveniences.
- ``with_document`` — whether the text tools read the whole primary document
  or only the tagged text blocks (the form's own text). On by default for
  the product profile, off by default under ``pure`` so the document can be
  held out as a control.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Annotated, Any, Literal

import anyio
from pydantic import Field

from xbrlkit import __version__
from xbrlkit.serve import tools
from xbrlkit.serve.session import FilingSession, SourceError

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
DEFAULT_PATH = "/mcp"
MAX_RESULT_CHARS = 60_000

# The representations `--as` will serve. Only `model` is in this release; the
# others are the tool sets of the Filing Ladder's rungs (tavi + jq, holon +
# SPARQL, lpg + Cypher, the package's files), specced but not yet moved.
REPRESENTATIONS = ("model", "tavi", "holon", "lpg", "files")

INSTRUCTIONS = """\
xbrlkit: SEC and XBRL filings loaded in memory on this machine, parsed once \
and queried through these tools. There is no graph and no index behind them: \
every answer is read from the filing.

Three kinds of filing load, and describe_filing's `profile` says which one you \
have:
- XBRL — any XBRL or inline XBRL report (SEC 10-K / 10-Q / 20-F, IFRS / ESEF, \
tagged ACFRs). Facts, networks and text; the whole toolset.
- XML — the forms with no XBRL in them: ownership (3, 4, 5), 13F, N-PORT, \
SC 13D/G. Read as fields and record tables through `records`, and as text.
- documents — an 8-K, a proxy, a registration statement: text only.

START
- list_filings says what is loaded. Nothing? load_filing takes a local path \
(an inline .htm, an instance .xml, a filing directory or zip), a URL, an \
EDGAR `cik:accession`, or a ticker (`NVDA`, `NVDA 10-Q`). Outside the SEC it \
takes `lei:<LEI>` for a filer's latest filing on filings.xbrl.org, or that \
index's own filing id — ESEF and the national regimes, identified by LEI \
rather than by ticker or CIK. XBRL is not the only source: a Tavi compiled \
model (`.tavi.json`), a holon (`.holon.jsonld`) or a model.json written by \
export_filing loads at once and answers everything below — so a report that \
was never an SEC filing, a ledger's own output included, uses the same tools. \
unload_filing drops one.
- describe_filing FIRST for a filing you have not looked at: the entity, the \
periods with the `key` the other tools use, the networks by role, the axes \
present, and the text sections with their offsets. Never guess a concept name \
or a period key.

NUMBERS
- resolve_element turns a phrase ("revenue", "operating lease liability") into \
the concept qnames this filing reports — the filer's own extension concepts \
included — with fact counts and the networks each sits in.
- fact_grid returns the consolidated total per concept and period: facts with \
no dimensional qualifier, the most precise of duplicate tags. Segment and \
member breakdowns need include_dimensions, axis or member.
- statement renders one presentation network — a primary statement or any \
disclosure table — as rows in filing order with values per period column.
- calculation answers "what sums to X": the calculation-linkbase children with \
their weights, the computed sum against the reported total, per period.

OTHER DOCUMENTS
- A filing is a set of documents and the primary one is not always where the \
content is: an 8-K is boilerplate with the press release attached as EX-99.1, \
and a 13F-HR's primary document is a cover page whose holdings are all in a \
second document. documents lists them; read_document reads one.

RECORDS (XML filings)
- records returns the form's own tables — a Form 4's transactions and \
holdings, a 13F's positions — as rows with the header fields beside them. \
describe_filing's `sections.records` lists the tables and their columns.

TEXT
- search_text is a regular-expression search over the readable text — the \
whole primary document, or the tagged text blocks alone, as the server was \
started — returning windows with offsets; read_text pages from an offset. \
describe_filing's `profile.text` says which, and its sections give offsets.

RULES
- Instants (balances, one date) and durations (flows, start..end) are \
different period keys; compare like with like — a 12-month duration with a \
12-month duration.
- Values are as reported, scale applied; `unit` says the measure, `decimals` \
the precision (-6 = millions). Text-block values come back as a preview and a \
character count; read the block through search_text / read_text.
- Cite the concept qname and the period key for every number you report.
"""

PURE_NOTE = """
PROFILE: pure. This server answers with the filing and nothing more: networks \
are listed by the filer's own role and definition (no statement kinds — find \
the balance sheet by its name), periods carry dates and no buckets, and there \
is no Item map. Everything you read is what the filer tagged or wrote.
"""

ReadLength = Annotated[
  int, Field(description="Characters to read (max 8000).", ge=1, le=8000)
]
PureReadLength = Annotated[
  int, Field(description="Characters to read (max 4000).", ge=1, le=4000)
]
Offset = Annotated[int, Field(description="Character offset to start at.", ge=0)]

Filing = Annotated[
  str | None,
  Field(
    description=(
      "The loaded filing's id (from list_filings; a ticker or accession also "
      "works). Omit when only one filing is loaded."
    )
  ),
]


def _dumps(payload: Any) -> str:
  text = json.dumps(payload, default=str, ensure_ascii=False)
  if len(text) > MAX_RESULT_CHARS:
    text = text[:MAX_RESULT_CHARS] + '…"clipped":true}'
  return text


def _error(message: str) -> str:
  return json.dumps({"error": message})


def build_server(
  session: FilingSession,
  out_dir: Path | None = None,
  *,
  pure: bool = False,
  with_document: bool | None = None,
) -> Any:
  """The ``MCPServer`` with every tool registered against ``session``.

  ``out_dir`` is the only place ``export_filing`` writes. ``with_document``
  defaults to the profile's own default: on for the product profile, off
  under ``pure``.
  """
  from mcp.server import MCPServer

  whole = (not pure) if with_document is None else bool(with_document)
  export_dir = Path(out_dir) if out_dir is not None else Path("output")
  server = MCPServer(
    name="xbrlkit",
    title="xbrlkit",
    instructions=INSTRUCTIONS + (PURE_NOTE if pure else ""),
    version=__version__,
  )

  def run(fn: Any, *args: Any, **kwargs: Any) -> str:
    try:
      return _dumps(fn(*args, **kwargs))
    except (tools.ToolError, SourceError) as exc:
      return _error(str(exc))

  def describe(loaded: Any) -> dict[str, Any]:
    return tools.describe_filing(loaded, pure=pure, whole=whole)

  @server.tool(
    name="list_filings",
    description="The filings this server has loaded, with their ids.",
    structured_output=False,
  )
  def list_filings() -> str:
    return run(tools.list_filings, session)

  @server.tool(
    name="load_filing",
    description=(
      "Load a filing into the server and return its description. `source` is "
      "a local path (an inline XBRL .htm, an XBRL instance .xml, a filing "
      "directory, a .zip package, or a JSON report: a Tavi compiled model, a "
      "holon, or a model.json written by export_filing), "
      "an http(s) URL Arelle can load, an EDGAR `cik:accession` (e.g. "
      "`1045810:0001045810-26-000021`), a ticker with an optional form "
      "(`NVDA`, `NVDA 10-Q`) for the latest filing of that form, or — outside "
      "the SEC — `lei:<LEI>` for that filer's latest filing on "
      "filings.xbrl.org, or one of that index's filing ids (e.g. "
      "`213800H2PQMIF3OVZY47-2022-03-31-ESEF-GB-0`). Any XBRL "
      "taxonomy loads: US GAAP, IFRS, ESEF, ACFR. Takes seconds to a minute; "
      "the taxonomy cache makes repeat loads fast, and a JSON report loads at "
      "once, with no Arelle and no taxonomy fetch."
    ),
    structured_output=False,
  )
  async def load_filing(
    source: Annotated[str, Field(description="Path, URL, cik:accession, or ticker.")],
    filing_id: Annotated[
      str | None,
      Field(
        description=(
          "An id to refer to the filing by; defaults to its accession or file name."
        )
      ),
    ] = None,
  ) -> str:
    try:
      loaded = await anyio.to_thread.run_sync(session.load, source, filing_id)
    except (SourceError, FileNotFoundError, ValueError) as exc:
      return _error(str(exc))
    return run(describe, loaded)

  @server.tool(
    name="unload_filing",
    description=(
      "Drop a loaded filing from the server (its id from list_filings). "
      "Load another with load_filing; nothing else changes."
    ),
    structured_output=False,
  )
  def unload_filing(
    filing: Annotated[str, Field(description="The loaded filing's id.")],
  ) -> str:
    try:
      dropped = session.unload(filing)
    except SourceError as exc:
      return _error(str(exc))
    return _dumps({"unloaded": dropped, "loaded": session.ids()})

  @server.tool(
    name="describe_filing",
    description=(
      "How a loaded filing is laid out: entity, form and fiscal context; fact "
      "counts; the reporting periods with the `key` fact_grid and statement "
      "use; units; the presentation networks by role (statements and "
      "disclosures — the primary statements flagged under the product "
      "profile); the dimensional axes present; and the text sections with "
      "their character offsets. Call this first — never guess names or keys."
    ),
    structured_output=False,
  )
  def describe_filing(filing: Filing = None) -> str:
    return run(lambda: describe(session.get(filing)))

  @server.tool(
    name="resolve_element",
    description=(
      "Find the XBRL concepts a filing reports for a phrase: 'revenue', "
      "'lease liability', 'us-gaap:Assets'. Matches qnames, names and labels, "
      "ranked; each match carries its label, type, period type, balance, how "
      "many facts the filing reports for it, and the networks it appears in. "
      "Use the returned qnames in fact_grid and calculation."
    ),
    structured_output=False,
  )
  def resolve_element(
    query: Annotated[str, Field(description="A concept name, qname, or label phrase.")],
    filing: Filing = None,
    limit: Annotated[
      int, Field(description="Matches to return (max 100).", ge=1, le=100)
    ] = 20,
  ) -> str:
    return run(lambda: tools.resolve_element(session.get(filing), query, limit))

  @server.tool(
    name="fact_grid",
    description=(
      "Values for one or more concepts across the filing's periods. By default "
      "the consolidated total per concept and period (facts with no dimensional "
      "qualifier), keeping the most precise of duplicate tags, newest period "
      "first. `period_end` (YYYY-MM-DD or YYYY) narrows the periods; "
      "`period_type` takes instant or duration (and, under the product "
      "profile, annual / quarterly buckets). Set include_dimensions, or name "
      "an axis / member (substring match), for segment and member breakdowns. "
      "Concepts may be qnames or bare names (`Revenues`)."
    ),
    structured_output=False,
  )
  def fact_grid(
    elements: Annotated[
      list[str],
      Field(
        description=(
          "Concept qnames or names, e.g. ['us-gaap:Revenues', 'NetIncomeLoss']."
        )
      ),
    ],
    filing: Filing = None,
    period_end: Annotated[
      str | None,
      Field(description="A period end date (YYYY-MM-DD) or a year (YYYY)."),
    ] = None,
    period_type: Annotated[
      str | None,
      Field(
        description=(
          "instant | duration; product profile also annual | quarterly | "
          "semi_annual | nine_months"
        )
      ),
    ] = None,
    include_dimensions: Annotated[
      bool,
      Field(description="Return member breakdowns beside the consolidated totals."),
    ] = False,
    axis: Annotated[
      str | None,
      Field(
        description=(
          "Keep facts on an axis whose qname contains this (implies dimensions)."
        )
      ),
    ] = None,
    member: Annotated[
      str | None,
      Field(
        description=(
          "Keep facts whose member qname contains this (implies dimensions)."
        )
      ),
    ] = None,
    limit: Annotated[
      int, Field(description="Rows to return (max 500).", ge=1, le=500)
    ] = 200,
  ) -> str:
    return run(
      lambda: tools.fact_grid(
        session.get(filing),
        elements,
        period_end=period_end,
        period_type=period_type,
        include_dimensions=include_dimensions,
        axis=axis,
        member=member,
        limit=limit,
        pure=pure,
      )
    )

  @server.tool(
    name="statement",
    description=(
      "Render one presentation network as a table: rows in the filing's own "
      "order with depth, label and the consolidated value per period column. "
      "`statement` is a network id, name (or part of one) or role URI from "
      "describe_filing; under the product profile a kind also works "
      "(balance_sheet, income_statement, cash_flow_statement, equity_statement, "
      "or a phrase like 'balance sheet'). `periods` limits the columns to those "
      "keys, end dates, or years; otherwise the most recent eight."
    ),
    structured_output=False,
  )
  def statement(
    statement: Annotated[
      str,
      Field(description="Network id, name (or part of one), role URI, or a kind."),
    ],
    filing: Filing = None,
    periods: Annotated[
      list[str] | None,
      Field(description="Period keys, end dates or years to keep as columns."),
    ] = None,
    max_rows: Annotated[
      int, Field(description="Rows to return (max 400).", ge=1, le=400)
    ] = 400,
  ) -> str:
    return run(
      lambda: tools.statement(
        session.get(filing), statement, periods=periods, max_rows=max_rows, pure=pure
      )
    )

  @server.tool(
    name="calculation",
    description=(
      "What sums to a total: the calculation-linkbase children of a concept "
      "with their weights, and per period the reported total, the sum "
      "computed from the children's consolidated facts, and the difference. "
      "Use for 'which line items make up operating expenses' or to check a "
      "subtotal. A concept that is a child rather than a parent reports what "
      "it contributes to."
    ),
    structured_output=False,
  )
  def calculation(
    concept: Annotated[
      str,
      Field(
        description="The total's concept qname or name, e.g. us-gaap:OperatingExpenses."
      ),
    ],
    filing: Filing = None,
    role: Annotated[
      str | None,
      Field(description="Restrict to calculation networks whose name contains this."),
    ] = None,
    period_end: Annotated[
      str | None,
      Field(description="A period end date (YYYY-MM-DD) or a year (YYYY)."),
    ] = None,
  ) -> str:
    return run(
      lambda: tools.calculation(
        session.get(filing), concept, role=role, period_end=period_end
      )
    )

  @server.tool(
    name="documents",
    description=(
      "What else was filed with this filing — the exhibits, and any second "
      "document the content actually lives in (an 8-K's EX-99.1 press "
      "release, a 13F's INFORMATION TABLE of holdings). Costs one small fetch "
      "the first time and nothing after. Every document carries its URL and "
      "says whether read_document can read it: a PDF or an image is listed "
      "with its address so a caller that can open one may fetch it directly. "
      "The XBRL package and the SEC's own rendered copies are not listed."
    ),
    structured_output=False,
  )
  def documents(filing: Filing = None) -> str:
    return run(lambda: tools.documents(session.get(filing), session))

  @server.tool(
    name="read_document",
    description=(
      f"Read up to {tools.MAX_READ} characters of one of the filing's other "
      "documents, from a character offset — the name comes from `documents`. "
      "An XML document (a 13F's holdings) comes back as its record tables "
      "rendered to text. Returns the next offset when more follows."
    ),
    structured_output=False,
  )
  def read_document(
    document: Annotated[
      str, Field(description="The document's name, from `documents`.")
    ],
    filing: Filing = None,
    offset: Annotated[int, Field(description="Character offset.", ge=0)] = 0,
    length: ReadLength = tools.MAX_READ,
  ) -> str:
    return run(
      lambda: tools.read_document(
        session.get(filing), session, document, offset=offset, length=length
      )
    )

  @server.tool(
    name="records",
    description=(
      "The record tables of an XML filing — a Form 4's transactions and "
      "holdings, a 13F's positions — as rows, with the document's header "
      "fields beside them. Omit `table` for every table; describe_filing's "
      "`sections.records` lists their names and columns."
    ),
    structured_output=False,
  )
  def records(
    filing: Filing = None,
    table: Annotated[
      str | None,
      Field(description="One table's name; omit for all of them."),
    ] = None,
    limit: Annotated[int, Field(description="Rows per table.", ge=1, le=1000)] = 100,
  ) -> str:
    return run(lambda: tools.records(session.get(filing), table=table, limit=limit))

  text_scope = (
    "the filing's primary document as plain text — every Item, note, table, "
    "the cover and the signatures, tagged or not"
    if whole
    else "the filing's tagged text blocks as plain text — the notes and "
    "policies the filer tagged, one after another under their concept names"
  )

  @server.tool(
    name="search_text",
    description=(
      f"Case-insensitive regular-expression search over {text_scope}. Returns "
      "up to max_hits matches (max 25), each with its character offset and a "
      "window of text centred on it (default 300 characters, max 1500), plus "
      "the total match count. Follow up with read_text at an offset."
    ),
    structured_output=False,
  )
  def search_text(
    pattern: Annotated[str, Field(description="A regular expression (Python syntax).")],
    filing: Filing = None,
    window: Annotated[
      int,
      Field(description="Characters of context around each match.", ge=40, le=1500),
    ] = 300,
    max_hits: Annotated[int, Field(description="Matches to return.", ge=1, le=25)] = 10,
  ) -> str:
    return run(
      lambda: tools.search_text(
        session.get(filing),
        pattern,
        window=window,
        max_hits=max_hits,
        whole=whole,
        pure=pure,
      )
    )

  read_cap = tools.PURE_MAX_READ if pure else tools.MAX_READ
  read_description = (
    f"Read up to {read_cap} characters of {text_scope}, from a character "
    "offset (from search_text, or a section offset in describe_filing). "
    "Returns the text and the next offset when more follows."
  )

  def _read(filing: str | None, offset: int, length: int) -> str:
    return run(
      lambda: tools.read_text(
        session.get(filing), offset=offset, length=length, whole=whole, pure=pure
      )
    )

  # Two registrations so the schema's maximum matches the profile's cap: the
  # SDK evaluates annotations against module globals, so the bound cannot be
  # computed here.
  if pure:

    @server.tool(
      name="read_text", description=read_description, structured_output=False
    )
    def read_text_pure(
      filing: Filing = None, offset: Offset = 0, length: PureReadLength = 4000
    ) -> str:
      return _read(filing, offset, length)

  else:

    @server.tool(
      name="read_text", description=read_description, structured_output=False
    )
    def read_text(
      filing: Filing = None, offset: Offset = 0, length: ReadLength = 4000
    ) -> str:
      return _read(filing, offset, length)

  @server.tool(
    name="export_filing",
    description=(
      "Write the loaded filing as one of xbrlkit's projections into the "
      "server's output directory and return the path: `holon` (RDF / JSON-LD, "
      "opens in the RoboSystems holon viewer), `tavi` (the Project Tavi "
      "compiled model, JSON), `oim` (xBRL-JSON), `lpg` (a single-filing "
      "LadybugDB graph; needs the lpg extra), or `model` (the parse itself as "
      "JSON — load_filing reloads it without Arelle)."
    ),
    structured_output=False,
  )
  def export_filing(
    format: Annotated[
      Literal["holon", "tavi", "oim", "lpg", "model"],
      Field(description="The projection to write."),
    ],
    filing: Filing = None,
  ) -> str:
    return run(lambda: tools.export_filing(session.get(filing), format, export_dir))

  return server


def serve(
  session: FilingSession,
  *,
  host: str = DEFAULT_HOST,
  port: int = DEFAULT_PORT,
  transport: Literal["http", "stdio"] = "http",
  out_dir: Path | None = None,
  path: str = DEFAULT_PATH,
  pure: bool = False,
  with_document: bool | None = None,
) -> None:
  """Run the server until interrupted."""
  server = build_server(session, out_dir, pure=pure, with_document=with_document)
  if transport == "stdio":
    server.run("stdio")
    return
  if host not in ("127.0.0.1", "localhost", "::1"):
    print(
      f"xbrlkit serve: binding {host} — this server has no authentication; "
      "only do this on a network you trust.",
      file=sys.stderr,
    )
  print(f"xbrlkit serve: MCP at http://{host}:{port}{path}", file=sys.stderr)
  server.run("streamable-http", host=host, port=port, streamable_http_path=path)
