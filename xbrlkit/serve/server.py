"""The MCP server: the tools in :mod:`xbrlkit.serve.tools` registered on an
``MCPServer`` and run over Streamable HTTP on localhost (or stdio).

No authentication, by design: the server runs on the user's own machine over
their own filings, and the transport binds to the loopback interface with
the SDK's host- and origin-header validation on, so a page in a browser
cannot drive it. Point an MCP client at ``http://127.0.0.1:8765/mcp``.
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

INSTRUCTIONS = """\
xbrlkit: XBRL filings loaded in memory on this machine — any XBRL or inline \
XBRL report (SEC 10-K / 10-Q / 20-F, IFRS / ESEF, tagged ACFRs), parsed once \
with Arelle into one neutral model and queried through these tools. There is \
no graph and no index behind them: every answer is read from the filing.

START
- list_filings says what is loaded. Nothing? load_filing takes a local path \
(an inline .htm, an instance .xml, a filing directory or zip), a URL, an EDGAR \
`cik:accession`, or a ticker (`NVDA`, `NVDA 10-Q`).
- describe_filing FIRST for a filing you have not looked at: the entity, the \
periods with the `key` the other tools use, the statements by role, the axes \
present, and the text sections with their offsets. Never guess a concept name \
or a period key.

NUMBERS
- resolve_element turns a phrase ("revenue", "operating lease liability") into \
the concept qnames this filing reports — the filer's own extension concepts \
included — with fact counts and the statements each sits in.
- fact_grid returns the consolidated total per concept and period: facts with \
no dimensional qualifier, the most precise of duplicate tags. Segment and \
member breakdowns need include_dimensions, axis or member.
- statement renders one presentation network — the balance sheet, income \
statement, cash flows, equity, or any disclosure table — as rows in filing \
order with values per period column.
- calculation answers "what sums to X": the calculation-linkbase children with \
their weights, the computed sum against the reported total, per period.

TEXT
- search_text is a regular-expression search over the whole primary document \
as plain text — cover, Items, notes, signatures, tagged or not — returning \
windows with offsets; read_text pages from an offset. The sections in \
describe_filing give the offsets of the Items and the tagged notes.

RULES
- Instants (balances, one date) and durations (flows, start..end) are \
different period keys; an annual figure is a 12-month duration, a quarter a \
3-month one. Read the `duration` field before comparing.
- Values are as reported, scale applied; `unit` says the measure, `decimals` \
the precision (-6 = millions). Text-block values come back as a preview and a \
character count; read the block through search_text / read_text.
- Cite the concept qname and the period key for every number you report.
"""


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


def build_server(session: FilingSession, out_dir: Path | None = None) -> Any:
  """The ``MCPServer`` with every tool registered against ``session``.

  ``out_dir`` is the only place ``export_filing`` writes.
  """
  from mcp.server import MCPServer

  export_dir = Path(out_dir) if out_dir is not None else Path("output")
  server = MCPServer(
    name="xbrlkit",
    title="xbrlkit",
    instructions=INSTRUCTIONS,
    version=__version__,
  )

  def run(fn: Any, *args: Any, **kwargs: Any) -> str:
    try:
      return _dumps(fn(*args, **kwargs))
    except (tools.ToolError, SourceError) as exc:
      return _error(str(exc))

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
      "directory, or a .zip package), an http(s) URL Arelle can load, an EDGAR "
      "`cik:accession` (e.g. `1045810:0001045810-26-000021`), or a ticker "
      "with an optional form (`NVDA`, `NVDA 10-Q`) for the latest filing of "
      "that form. Any XBRL taxonomy loads: US GAAP, IFRS, ESEF, ACFR. Takes "
      "seconds to a minute; the taxonomy cache makes repeat loads fast."
    ),
    structured_output=False,
  )
  async def load_filing(
    source: Annotated[str, Field(description="Path, URL, cik:accession, or ticker.")],
    filing_id: Annotated[
      str | None,
      Field(
        description="An id to refer to the filing by; defaults to its accession or file name."
      ),
    ] = None,
  ) -> str:
    try:
      loaded = await anyio.to_thread.run_sync(session.load, source, filing_id)
    except (SourceError, FileNotFoundError, ValueError) as exc:
      return _error(str(exc))
    return run(tools.describe_filing, loaded)

  @server.tool(
    name="describe_filing",
    description=(
      "How a loaded filing is laid out: entity, form and fiscal context; fact "
      "counts; the reporting periods with the `key` fact_grid and statement "
      "use; units; the presentation networks (statements and disclosures) by "
      "role, the primary statements flagged; the dimensional axes present; and "
      "the text sections (10-K Items, tagged notes) with their character "
      "offsets. Call this first — never guess names or keys."
    ),
    structured_output=False,
  )
  def describe_filing(filing: Filing = None) -> str:
    return run(lambda: tools.describe_filing(session.get(filing)))

  @server.tool(
    name="resolve_element",
    description=(
      "Find the XBRL concepts a filing reports for a phrase: 'revenue', "
      "'lease liability', 'us-gaap:Assets'. Matches qnames, names and labels, "
      "ranked; each match carries its label, type, period type, balance, how "
      "many facts the filing reports for it, and the statements it appears "
      "in. Use the returned qnames in fact_grid and calculation."
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
      "first. `period_end` (YYYY-MM-DD or YYYY) and `period_type` (instant, "
      "duration, annual, quarterly) narrow the periods. Set include_dimensions, "
      "or name an axis / member (substring match), for segment and member "
      "breakdowns. Concepts may be qnames or bare names (`Revenues`)."
    ),
    structured_output=False,
  )
  def fact_grid(
    elements: Annotated[
      list[str],
      Field(
        description="Concept qnames or names, e.g. ['us-gaap:Revenues', 'NetIncomeLoss']."
      ),
    ],
    filing: Filing = None,
    period_end: Annotated[
      str | None, Field(description="A period end date (YYYY-MM-DD) or a year (YYYY).")
    ] = None,
    period_type: Annotated[
      str | None,
      Field(
        description="instant | duration | annual | quarterly | semi_annual | nine_months"
      ),
    ] = None,
    include_dimensions: Annotated[
      bool,
      Field(description="Return member breakdowns instead of consolidated totals."),
    ] = False,
    axis: Annotated[
      str | None,
      Field(
        description="Keep facts on an axis whose qname contains this (implies dimensions)."
      ),
    ] = None,
    member: Annotated[
      str | None,
      Field(
        description="Keep facts whose member qname contains this (implies dimensions)."
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
      )
    )

  @server.tool(
    name="statement",
    description=(
      "Render one presentation network as a table: rows in the filing's own "
      "order with depth, label and the consolidated value per period column. "
      "`statement` is a role URI or name from describe_filing, or a primary "
      "statement by kind: balance_sheet, income_statement, cash_flow_statement, "
      "equity_statement (plain phrases like 'balance sheet' work). `periods` "
      "limits the columns to those keys, end dates, or years; otherwise the "
      "most recent eight."
    ),
    structured_output=False,
  )
  def statement(
    statement: Annotated[
      str,
      Field(
        description="Role URI, network name (or part of one), or a statement kind."
      ),
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
        session.get(filing), statement, periods=periods, max_rows=max_rows
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
      str | None, Field(description="A period end date (YYYY-MM-DD) or a year (YYYY).")
    ] = None,
  ) -> str:
    return run(
      lambda: tools.calculation(
        session.get(filing), concept, role=role, period_end=period_end
      )
    )

  @server.tool(
    name="search_text",
    description=(
      "Case-insensitive regular-expression search over the filing's primary "
      "document as plain text — every Item, note, table, the cover and the "
      "signatures, tagged or not. Returns up to max_hits matches (max 25), "
      "each with its character offset, the section it falls in, and a window "
      "of text centred on it (default 300 characters, max 1500), plus the "
      "total match count. Follow up with read_text at an offset."
    ),
    structured_output=False,
  )
  def search_text(
    pattern: Annotated[str, Field(description="A regular expression (Python syntax).")],
    filing: Filing = None,
    window: Annotated[
      int, Field(description="Characters of context around each match.", ge=40, le=1500)
    ] = 300,
    max_hits: Annotated[int, Field(description="Matches to return.", ge=1, le=25)] = 10,
  ) -> str:
    return run(
      lambda: tools.search_text(
        session.get(filing), pattern, window=window, max_hits=max_hits
      )
    )

  @server.tool(
    name="read_text",
    description=(
      "Read up to 8000 characters of the filing's primary document as plain "
      "text from a character offset (from search_text, or a section offset in "
      "describe_filing). Returns the text, the section it starts in, and the "
      "next offset when more follows."
    ),
    structured_output=False,
  )
  def read_text(
    filing: Filing = None,
    offset: Annotated[
      int, Field(description="Character offset to start at.", ge=0)
    ] = 0,
    length: Annotated[
      int, Field(description="Characters to read (max 8000).", ge=1, le=8000)
    ] = 4000,
  ) -> str:
    return run(
      lambda: tools.read_text(session.get(filing), offset=offset, length=length)
    )

  @server.tool(
    name="export_filing",
    description=(
      "Write the loaded filing as one of xbrlkit's projections into the "
      "server's output directory and return the path: `holon` (RDF / JSON-LD, "
      "opens in the RoboSystems holon viewer), `tavi` (the Project Tavi "
      "compiled model, JSON), `oim` (xBRL-JSON), or `lpg` (a single-filing "
      "LadybugDB graph; needs the lpg extra)."
    ),
    structured_output=False,
  )
  def export_filing(
    format: Annotated[
      Literal["holon", "tavi", "oim", "lpg"],
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
) -> None:
  """Run the server until interrupted."""
  server = build_server(session, out_dir)
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
