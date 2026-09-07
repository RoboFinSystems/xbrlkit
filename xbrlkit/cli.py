"""Command-line interface — a SEC filing to a portable report document.

    xbrlkit build --cik 320193 --accno 0000320193-23-000106  # -> output/<accno>.holon.jsonld
    xbrlkit build --cik 320193 --accno … --format tavi       # -> output/<accno>.tavi.json
    xbrlkit build --cik 320193 --accno … --format lpg        # -> output/<accno>.lbug
    xbrlkit fetch --ticker NVDA --form 10-K --n 1            # -> output/

Wires the three layers: ``edgar`` (fetch) -> ``parse`` (Arelle -> XbrlModel) ->
``serialize`` (XbrlModel -> a holon, a Tavi compiled model, an OIM report, or
a property-graph database).

``--format lpg`` needs the ``lpg`` extra (``pip install "xbrlkit[lpg]"``) and
writes the filing as a single-file LadybugDB database with the same tables as
the RoboSystems ``sec`` graph, text blocks inline.

``--format tavi`` writes a second sidecar, ``<accession>.tavi.gaps.json``: what
the filing carries that Project Tavi has nowhere to put. That file is the point
of the Tavi projection, not a by-product of it.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from datetime import date
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from .config import Config
from .edgar import EdgarClient, download_filing
from .model import EntityIdentity, FilingMeta, XbrlModel
from .parse import close, load_model, to_xbrl_model
from .serialize import (
  build_lbug,
  to_graph_tables,
  to_holon,
  to_oim_document,
  to_tavi_report,
)

# Generated documents land here by default — a git-tracked folder whose contents
# are git-ignored (see output/.gitignore). Relative to the working directory.
DEFAULT_OUTPUT_DIR = Path("output")

FORMATS = ("holon", "tavi", "oim", "lpg", "all", "both")
SUFFIXES = {
  "holon": ".holon.jsonld",
  "tavi": ".tavi.json",
  "oim": ".oim.json",
  "lpg": ".lbug",
}
# "both" predates the OIM projection and is kept as an alias for the two it
# originally meant, so an existing invocation keeps writing the same two files.
FORMAT_SETS = {
  "both": ("holon", "tavi"),
  "all": ("holon", "tavi", "oim"),
}


def _parse_date(value: str | None) -> date | None:
  if not value:
    return None
  try:
    return date.fromisoformat(value)
  except ValueError:
    return None


def _write_outputs(model: XbrlModel, out_path: Path, fmt: str, named: bool) -> None:
  """Write the requested projection(s) of ``model``.

  A single format with an explicit ``-o`` writes exactly that path, unchanged
  from before ``--format`` existed. A multi-format run has to derive a stem,
  because one parse then produces several documents that cannot share a name.
  """
  out_path.parent.mkdir(parents=True, exist_ok=True)
  stem = out_path.name
  for suffix in SUFFIXES.values():
    stem = stem.removesuffix(suffix)
  exact = out_path if named and fmt not in FORMAT_SETS else None

  wanted = FORMAT_SETS.get(fmt, (fmt,))
  for name in wanted:
    target = exact or out_path.parent / f"{stem}{SUFFIXES[name]}"
    if name == "holon":
      target.write_text(to_holon(model))
      print(f"wrote {target}")
    elif name == "oim":
      target.write_text(json.dumps(to_oim_document(model), indent=2, default=str))
      print(f"wrote {target}")
    elif name == "lpg":
      tables = to_graph_tables(model)
      build_lbug(tables, target)
      counts = tables.counts()
      print(
        f"wrote {target}  ({counts.get('Fact', 0)} facts, "
        f"{counts.get('Element', 0)} elements, {counts.get('Structure', 0)} structures)"
      )
    else:
      document, gaps = to_tavi_report(model)
      target.write_text(json.dumps(document, indent=2, default=str))
      gaps_path = out_path.parent / f"{stem}.tavi.gaps.json"
      gaps_path.write_text(json.dumps(gaps.to_dict(), indent=2, default=str))
      print(f"wrote {target}")
      against_model = len(gaps.item_types_without_builtin) + len(
        gaps.dropped_period_semantics
      )
      print(f"wrote {gaps_path}  ({against_model} findings against the model)")


def _build_one(
  client: EdgarClient,
  cik: str,
  accession: str,
  out_path: Path,
  cache_dir: Path,
  fmt: str = "holon",
  named: bool = False,
) -> XbrlModel:
  """Fetch one filing, parse it, and write the requested projection(s)."""
  ref = client.get_filing_ref(cik, accession)
  info = client.company_info(cik)
  with tempfile.TemporaryDirectory() as tmp:
    target = download_filing(client, cik, accession, Path(tmp))
    mx = load_model(target, cache_dir=cache_dir)
    try:
      filing = filing_meta(client.config.sec_base_url, cik, accession, ref, target.name)
      model = to_xbrl_model(mx, filing, entity=entity_identity(info))
    finally:
      close(mx.modelManager.cntlr)
  _write_outputs(model, out_path, fmt, named)
  print(f"  ({len(model.facts)} facts, {len(model.networks)} networks)")
  return model


def filing_meta(
  sec_base_url: str, cik: str, accession: str, ref: Any, primary_document: str
) -> FilingMeta:
  """The :class:`FilingMeta` for one filing from its EDGAR record and the
  file Arelle loaded. ``report_uri`` is the primary document's Archives URL
  — the stem the property-graph projection scopes its report-level ids on."""
  padded = str(int(cik)).zfill(10)
  report_uri = (
    f"{sec_base_url}/Archives/edgar/data/{int(cik)}/"
    f"{accession.replace('-', '')}/{primary_document}"
  )
  return FilingMeta(
    accession=accession,
    cik=padded,
    form=ref.form or None,
    filing_date=_parse_date(ref.filing_date),
    report_date=_parse_date(getattr(ref, "report_date", None)),
    acceptance_datetime=getattr(ref, "acceptance_datetime", None) or None,
    is_inline_xbrl=bool(getattr(ref, "is_inline", True)),
    primary_document=primary_document,
    report_uri=report_uri,
  )


def entity_identity(info: Any) -> EntityIdentity:
  """The :class:`EntityIdentity` carried by an EDGAR submissions header."""
  return EntityIdentity(
    cik=info.cik,
    name=info.name,
    legal_name=info.name,
    ein=info.ein,
    ticker=info.ticker,
    exchange=getattr(info, "exchange", None),
    sic=getattr(info, "sic", None),
    sic_description=getattr(info, "sic_description", None),
    category=getattr(info, "category", None),
    state_of_incorporation=getattr(info, "state_of_incorporation", None),
    fiscal_year_end=getattr(info, "fiscal_year_end", None),
    entity_type=getattr(info, "entity_type", None),
    website=getattr(info, "website", None),
    phone=getattr(info, "phone", None),
  )


def _config_from_args(args: argparse.Namespace) -> Config:
  if getattr(args, "user_agent", None):
    return Config(user_agent=args.user_agent)
  # Fresh Config() re-reads os.environ after main()'s load_dotenv(), so a
  # SEC_GOV_USER_AGENT set in .env is honored (the module-level CONFIG was
  # frozen at import, before the .env was loaded).
  return Config()


def _cmd_build(args: argparse.Namespace) -> int:
  config = _config_from_args(args)
  client = EdgarClient(config=config)
  out = Path(args.out) if args.out else DEFAULT_OUTPUT_DIR / args.accno
  _build_one(
    client,
    args.cik,
    args.accno,
    out,
    config.arelle_cache_dir,
    args.format,
    named=bool(args.out),
  )
  return 0


def _cmd_query(args: argparse.Namespace) -> int:
  from .query import fact_grid, load_holon

  graph = load_holon(args.infile)
  rows = fact_grid(
    graph,
    elements=args.element or None,
    periods=args.period or None,
    period_type=args.period_type,
  )
  for r in rows:
    print(f"{r.end_date or '':<12} {r.qname:<55} {r.value:>20,.4f}  {r.measure or ''}")
  print(f"({len(rows)} consolidated facts)", file=sys.stderr)
  return 0


def _cmd_fetch(args: argparse.Namespace) -> int:
  config = _config_from_args(args)
  client = EdgarClient(config=config)
  cik = client.ticker_to_cik(args.ticker)
  filings = client.list_filings(cik, forms=[args.form] if args.form else None)
  if not filings:
    print(f"no {args.form or 'matching'} filings for {args.ticker}", file=sys.stderr)
    return 1
  out_dir = Path(args.out)
  out_dir.mkdir(parents=True, exist_ok=True)
  for ref in filings[: args.n]:
    _build_one(
      client,
      cik,
      ref.accession,
      out_dir / ref.accession,
      config.arelle_cache_dir,
      args.format,
    )
  return 0


def _cmd_cache(args: argparse.Namespace) -> int:
  from .parse import cache as schema_cache

  config = _config_from_args(args)
  cache_dir = Path(args.cache_dir) if args.cache_dir else config.arelle_cache_dir

  if args.cache_command == "status":
    report = schema_cache.status(cache_dir, config=config)
    print(f"cache: {report.cache_dir}")
    print(f"files: {report.files} ({report.schemas} schemas)")
    for host, count in sorted(report.by_host.items(), key=lambda kv: -kv[1]):
      print(f"  {host}: {count}")
    if report.missing_essentials:
      print("missing essentials:")
      for url in report.missing_essentials:
        print(f"  {url}")
      return 1
    print("seeded: yes")
    return 0

  if args.cache_command == "extract":
    report = schema_cache.seed(Path(args.bundle), cache_dir, config=config)
    print(
      f"extracted {report.bundle} into {report.cache_dir}: "
      f"{report.written} written, {report.skipped} skipped"
    )
    return 0

  if args.cache_command == "bundle":
    hosts = tuple(args.host) if args.host else None
    count = schema_cache.bundle(Path(args.out), cache_dir, hosts=hosts, config=config)
    print(f"packed {count} files from {cache_dir} into {args.out}")
    return 0

  if args.cache_command == "download":
    if args.url:
      urls = list(args.url)
    else:
      years = _parse_years(args.years) if args.years else schema_cache.DEFAULT_YEARS
      urls = schema_cache.entry_points(years)
    print(f"loading {len(urls)} entry points into {cache_dir}", file=sys.stderr)
    report = schema_cache.download(urls, cache_dir, config=config)
    for url in report.loaded:
      print(f"ok      {url}")
    for url, reason in report.failed.items():
      print(f"failed  {url}: {reason}")
    print(
      f"{len(report.loaded)} loaded, {len(report.failed)} failed, "
      f"{report.files_added} files added ({report.files_after} in cache)"
    )
    return 1 if report.failed and not report.loaded else 0

  raise ValueError(f"unknown cache command: {args.cache_command}")


MCP_EXTRA_HINT = (
  "xbrlkit serve needs the mcp extra: pip install 'xbrlkit[mcp]'  "
  "(or, without installing: uvx --from 'xbrlkit[mcp]@latest' xbrlkit serve …)"
)


def _cmd_serve(args: argparse.Namespace) -> int:
  # The session imports without the SDK; the server does not. Check for the
  # SDK up front so the message names the extra rather than a module.
  try:
    import mcp  # noqa: F401  # pyright: ignore[reportUnusedImport]
  except ImportError:
    print(f"error: {MCP_EXTRA_HINT}", file=sys.stderr)
    return 1
  from .serve import FilingSession, serve
  from .serve.server import REPRESENTATIONS

  if args.representation != "model":
    known = ", ".join(r for r in REPRESENTATIONS if r != "model")
    print(
      f"error: --as {args.representation}: only `model` is served in this release; "
      f"{known} are the next backends (the tools of the Filing Ladder's rungs).",
      file=sys.stderr,
    )
    return 1
  with_document = args.with_document
  if with_document is None:
    with_document = not args.pure
  profile = "pure" if args.pure else "product"
  text_source = "the primary document" if with_document else "the tagged text blocks"
  print(
    f"xbrlkit serve: --as {args.representation}, {profile} profile, "
    f"text tools read {text_source}",
    file=sys.stderr,
  )

  session = FilingSession(config=_config_from_args(args))
  for source in args.sources:
    print(f"loading {source} …", file=sys.stderr)
    loaded = session.load(source)
    model = loaded.model
    print(
      f"  {loaded.id}: {model.entity.name or model.entity.cik} {model.filing.form or ''} "
      f"({len(model.facts)} facts, {len(model.networks)} networks, "
      f"{len(loaded.text):,} chars of text)",
      file=sys.stderr,
    )
  try:
    serve(
      session,
      host=args.host,
      port=args.port,
      transport=args.transport,
      out_dir=Path(args.out_dir),
      path=args.path,
      pure=args.pure,
      with_document=with_document,
    )
  except KeyboardInterrupt:
    pass
  finally:
    session.close()
  return 0


def _parse_years(text: str) -> tuple[int, ...]:
  """``2022-2026`` or ``2023,2024`` → a tuple of years."""
  if "-" in text:
    start, end = text.split("-", 1)
    return tuple(range(int(start), int(end) + 1))
  return tuple(int(part) for part in text.split(",") if part.strip())


def build_parser() -> argparse.ArgumentParser:
  parser = argparse.ArgumentParser(
    prog="xbrlkit",
    description="Work with XBRL filings above Arelle: one parse, portable models.",
  )
  parser.add_argument(
    "--user-agent",
    help="SEC User-Agent (else $SEC_GOV_USER_AGENT). Must identify you with contact info.",
  )
  sub = parser.add_subparsers(dest="command", required=True)

  b = sub.add_parser("build", help="Build one filing by CIK + accession number.")
  b.add_argument("--cik", required=True, help="CIK (zero-padded or bare).")
  b.add_argument(
    "--accno", required=True, help="Accession number, e.g. 0000320193-23-000106."
  )
  b.add_argument(
    "-o",
    "--out",
    default=None,
    help="Output path (default: output/<accession>.holon.jsonld).",
  )
  b.add_argument(
    "--format",
    choices=FORMATS,
    default="holon",
    help="Projection: holon | tavi | oim | lpg | all (default holon). 'tavi' also writes a .tavi.gaps.json; 'lpg' writes a LadybugDB database and needs the lpg extra.",
  )
  b.set_defaults(func=_cmd_build)

  f = sub.add_parser("fetch", help="Fetch N filings for a ticker.")
  f.add_argument("--ticker", required=True, help="Ticker symbol, e.g. NVDA.")
  f.add_argument("--form", default="10-K", help="Form type filter (default 10-K).")
  f.add_argument(
    "--n", type=int, default=1, help="Number of most-recent filings (default 1)."
  )
  f.add_argument(
    "-o",
    "--out",
    default=str(DEFAULT_OUTPUT_DIR),
    help="Output directory (default: output/).",
  )
  f.add_argument(
    "--format",
    choices=FORMATS,
    default="holon",
    help="Projection: holon | tavi | oim | lpg | all (default holon). 'tavi' also writes a .tavi.gaps.json; 'lpg' writes a LadybugDB database and needs the lpg extra.",
  )
  f.set_defaults(func=_cmd_fetch)

  q = sub.add_parser(
    "query", help="Query consolidated facts in a holon.jsonld (in-memory SPARQL)."
  )
  q.add_argument("--in", dest="infile", required=True, help="Path to a holon.jsonld.")
  q.add_argument(
    "--element", action="append", help="Element qname filter, e.g. us-gaap:Assets."
  )
  q.add_argument(
    "--period", action="append", help="Period end date YYYY-MM-DD (repeatable)."
  )
  q.add_argument(
    "--period-type",
    choices=["instant", "annual", "quarterly"],
    dest="period_type",
    help="Restrict to instant / annual-duration / quarterly-duration facts.",
  )
  q.set_defaults(func=_cmd_query)

  c = sub.add_parser(
    "cache",
    help="The Arelle schema cache: status | download | extract | bundle.",
  )
  cache_common = argparse.ArgumentParser(add_help=False)
  cache_common.add_argument(
    "--cache-dir",
    default=None,
    help="Cache directory (default: $XBRLKIT_ARELLE_CACHE_DIR or ~/.cache/xbrlkit/arelle).",
  )
  cache_sub = c.add_subparsers(dest="cache_command", required=True)
  cache_sub.add_parser(
    "status", parents=[cache_common], help="Count the cache and check it is seeded."
  )
  d = cache_sub.add_parser(
    "download",
    parents=[cache_common],
    help="Fill the cache by loading the standard entry points (or the given URLs) through Arelle.",
  )
  d.add_argument("url", nargs="*", help="Entry-point URLs (default: the standard set).")
  d.add_argument(
    "--years", default=None, help="Taxonomy years for the standard set, e.g. 2022-2026."
  )
  e = cache_sub.add_parser(
    "extract", parents=[cache_common], help="Seed the cache from a bundle (tar.gz)."
  )
  e.add_argument("--bundle", required=True, help="Bundle path.")
  bd = cache_sub.add_parser(
    "bundle", parents=[cache_common], help="Pack the cache as a tar.gz."
  )
  bd.add_argument("--out", required=True, help="Output tar.gz path.")
  bd.add_argument(
    "--host", action="append", help="Only these hosts (repeatable; default: all)."
  )
  c.set_defaults(func=_cmd_cache)

  s = sub.add_parser(
    "serve",
    help="Serve loaded filings to an MCP client over Streamable HTTP (needs the mcp extra).",
    description=(
      "Load filings into memory and serve them over MCP with no authentication, "
      "bound to localhost. Each SOURCE is a local path (an inline .htm, an "
      "instance .xml, a filing directory or .zip), an http(s) URL, an EDGAR "
      "cik:accession, or a ticker with an optional form ('NVDA', 'NVDA 10-Q'). "
      "Filings can also be loaded later through the load_filing tool."
    ),
  )
  s.add_argument("sources", nargs="*", help="Filings to load at start (see above).")
  s.add_argument(
    "--host", default="127.0.0.1", help="Interface to bind (default: 127.0.0.1)."
  )
  s.add_argument("--port", type=int, default=8765, help="Port (default: 8765).")
  s.add_argument(
    "--path", default="/mcp", help="URL path of the MCP endpoint (default: /mcp)."
  )
  s.add_argument(
    "--transport",
    choices=["http", "stdio"],
    default="http",
    help="Streamable HTTP on --host:--port (default), or stdio for clients that speak nothing else.",
  )
  s.add_argument(
    "--out-dir",
    default=str(DEFAULT_OUTPUT_DIR),
    help="Where export_filing writes (default: output/).",
  )
  s.add_argument(
    "--as",
    dest="representation",
    default="model",
    metavar="REPRESENTATION",
    help=(
      "The representation to serve: `model` (the parsed filing behind shaped "
      "tools; the only one in this release). Coming: tavi, holon, lpg, files."
    ),
  )
  s.add_argument(
    "--pure",
    action="store_true",
    help=(
      "A faithful reading of the filing and nothing more: no statement kinds, "
      "no detected Items, no period buckets, the ladder's read cap. The "
      "profile a benchmark rung runs under; the default is the product profile."
    ),
  )
  s.add_argument(
    "--with-document",
    dest="with_document",
    action="store_true",
    default=None,
    help=(
      "search_text / read_text read the whole primary document (default under "
      "the product profile)."
    ),
  )
  s.add_argument(
    "--without-document",
    dest="with_document",
    action="store_false",
    help=(
      "search_text / read_text read only the tagged text blocks — the form's "
      "own text (default under --pure)."
    ),
  )
  s.set_defaults(func=_cmd_serve)
  return parser


def main(argv: list[str] | None = None) -> int:
  load_dotenv()  # load SEC_GOV_USER_AGENT (and other overrides) from a local .env
  parser = build_parser()
  args = parser.parse_args(argv)
  try:
    return args.func(args)
  except Exception as exc:  # surface a clean message, not a traceback, to the CLI user
    print(f"error: {exc}", file=sys.stderr)
    return 1


if __name__ == "__main__":
  raise SystemExit(main())
