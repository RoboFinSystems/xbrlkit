"""The tools, as plain functions: a loaded filing in, a JSON-able dict out.

Each mirrors one of the shaped tools the RoboSystems ``sec`` graph serves
over MCP — describe, resolve an element, the fact grid, a statement, a
calculation roll-up, text search and read — but reads the in-memory
:class:`~xbrlkit.model.XbrlModel` instead of a graph. Nothing here knows
about MCP; :mod:`xbrlkit.serve.server` registers these and turns the
dicts into tool results, and a test or a notebook can call them directly.

Two conventions carried over from the graph tools, because they decide
whether a number is right:

- **Consolidated by default.** A fact with no dimensional qualifier is the
  entity-wide total; a fact with one is a member breakdown. ``fact_grid``
  and ``statement`` return the consolidated total unless a dimension is
  asked for.
- **The most precise duplicate wins.** A filer may tag the same value
  twice at different precisions (the statement line at ``decimals=-6``
  and a note at ``-3``); the higher precision is the one to report.
"""

from __future__ import annotations

import json
import math
import re
from bisect import bisect_right
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from xbrlkit.config import CONFIG, Config
from xbrlkit.model import Arc, Concept, Network, Period, Unit, XbrlFact, XbrlModel
from xbrlkit.serialize import classify_network, root_qname
from xbrlkit.serve.session import (
  FilingSession,
  LoadedFiling,
  TaxonomyEntry,
  TextSection,
)
from xbrlkit.edgar.items import describe_items, is_earnings_release, items_note
from xbrlkit.information_block import (
  Disclosure,
  InformationBlock,
  fact_membership,
  group_disclosures,
  plan_blocks,
)
from xbrlkit.text.ixbrl import _strip_html
from xbrlkit.view import ViewerHost

MAX_HITS = 25
DEFAULT_HITS = 10
# Rows of the hit distribution and of the term fallback a search returns.
SECTION_ROWS = 10
TERM_ROWS = 8
# Beyond this many matches a pattern says nothing about where its subject
# is, so the distribution is omitted rather than computed over a sample.
SECTION_SCAN = 20_000
# How far back a lookup walks to find the innermost section covering an
# offset; text blocks nest a level or two, never dozens.
NEST_SCAN = 32
DEFAULT_WINDOW = 300
MAX_WINDOW = 1500
DEFAULT_READ = 4000
MAX_READ = 8000
MAX_GRID_ROWS = 500
MAX_STATEMENT_ROWS = 400
MAX_STATEMENT_COLUMNS = 8
MAX_BLOCK_ROWS = 400
# Member breakdowns are kept up to a response budget, not a count: a large
# cube is bounded, a small table is never cut. `max_members` is the caller's
# explicit ceiling when given.
BLOCK_MEMBER_CHARS = 16_000
BLOCK_COLUMN_CHARS = 16_000
MEMBER_CELL_CHARS = 36
MAX_BLOCK_MEMBERS_CAP = 200
AXIS_MEMBERS_LISTED = 64
BLOCK_TEXT_PREVIEW = 240
TEXT_PREVIEW = 160
DESCRIBE_PERIODS = 30
DESCRIBE_AXES = 30
DESCRIBE_TEXT_BLOCKS = 25

_PRIMARY_ALIASES = {
  "balance sheet": "balance_sheet",
  "balance_sheet": "balance_sheet",
  "financial position": "balance_sheet",
  "income statement": "income_statement",
  "income_statement": "income_statement",
  "operations": "income_statement",
  "profit and loss": "income_statement",
  "cash flow": "cash_flow_statement",
  "cash flows": "cash_flow_statement",
  "cash_flow_statement": "cash_flow_statement",
  "equity": "equity_statement",
  "equity_statement": "equity_statement",
  "stockholders equity": "equity_statement",
  "shareholders equity": "equity_statement",
}

EXPORT_FORMATS = {
  "clawdog": "clawdog.jsonld",
  "holon": "holon.jsonld",
  "tavi": "tavi.json",
  "oim": "oim.json",
  "lpg": "lbug",
  "model": "model.json",
}
PURE_MAX_READ = 4000
BUCKET_PERIOD_TYPES = ("annual", "quarterly", "semi_annual", "nine_months")


class ToolError(ValueError):
  """A tool call the model can correct: a bad argument, an unknown name."""


# -- the per-filing index -------------------------------------------------------


@dataclass
class Index:
  """What every tool needs and no tool should recompute per call."""

  periods: dict[str, Period]
  units: dict[str, Unit]
  by_concept: dict[str, list[XbrlFact]]
  presentation: list[Network]
  calculation: list[Network]
  definition: list[Network]
  concept_roles: dict[str, list[str]] = field(default_factory=dict)
  classification: dict[str, str | None] = field(default_factory=dict)


def index_for(lf: LoadedFiling) -> Index:
  cached = getattr(lf, "_index", None)
  if cached is not None:
    return cached
  model = lf.model
  by_concept: dict[str, list[XbrlFact]] = defaultdict(list)
  for f in model.facts:
    by_concept[f.concept_qname].append(f)
  presentation = [n for n in model.networks if n.kind == "presentation"]
  concept_roles: dict[str, list[str]] = defaultdict(list)
  classification: dict[str, str | None] = {}
  for n in presentation:
    classification[n.role_uri] = _classify_statement(n)
    seen: set[str] = set()
    for arc in n.arcs:
      for q in (arc.from_qname, arc.to_qname):
        if q not in seen:
          seen.add(q)
          concept_roles[q].append(n.role_uri)
  idx = Index(
    periods={p.id: p for p in model.periods},
    units={u.id: u for u in model.units},
    by_concept=dict(by_concept),
    presentation=presentation,
    calculation=[n for n in model.networks if n.kind == "calculation"],
    definition=[n for n in model.networks if n.kind == "definition"],
    concept_roles=dict(concept_roles),
    classification=classification,
  )
  lf._index = idx  # pyright: ignore[reportAttributeAccessIssue]
  return idx


_NOT_A_STATEMENT = (
  "disclosure",
  "(details)",
  "(tables)",
  "(policies)",
  "parenthetical",
)
_DEFINITION_PREFIX = re.compile(
  r"^\s*\d+\s*-\s*(?:statement|disclosure|document)\s*-\s*", re.IGNORECASE
)


def _classify_statement(n: Network) -> str | None:
  """A primary-statement kind for a presentation network, or ``None``.

  The legacy classifier keys on words like "income", so left alone it calls
  an income-taxes note an income statement; a network whose definition says
  it is a disclosure, a detail or a parenthetical is never a primary
  statement, whatever else its name contains.

  The tree's root is passed as the last resort, so a filing whose definitions
  are not in English still classifies. It is deliberately reached only after
  the exclusions above: a parenthetical shares its root with the statement it
  qualifies, and only the definition tells them apart.
  """
  definition = (n.definition or "").lower()
  if any(marker in definition for marker in _NOT_A_STATEMENT):
    return None
  return classify_network(n.role_uri, n.definition, root_qname(n.arcs))


def _network_id(n: Network) -> str:
  """The short name a reader can pass back: the role's last path segment."""
  return n.role_id or n.role_uri.rstrip("/").rsplit("/", 1)[-1]


def _network_name(n: Network) -> str:
  """The definition without EDGAR's leading sort number and category."""
  definition = n.definition or _network_id(n)
  return _DEFINITION_PREFIX.sub("", definition).strip() or definition


# -- small shared helpers -------------------------------------------------------


def _pref_label(concept: Concept | None, qname: str | None = None) -> str | None:
  if concept is None:
    return qname.split(":")[-1] if qname else None
  return concept.pref_label or concept.name


def _label_for_role(concept: Concept | None, role: str | None) -> str | None:
  """The concept's label in ``role`` (a presentation arc's preferred label),
  falling back to its standard label."""
  if concept is None:
    return None
  if role:
    for label in concept.labels:
      if label.role == role and label.value:
        return label.value
    short = role.rsplit("/", 1)[-1].lower()
    for label in concept.labels:
      if label.role and label.role.lower().endswith(short) and label.value:
        return label.value
  return _pref_label(concept)


def _documentation(concept: Concept) -> str | None:
  for label in concept.labels:
    if label.role and label.role.endswith("documentation") and label.value:
      return label.value
  return None


def _period_key(p: Period | None) -> str | None:
  if p is None:
    return None
  if p.period_type == "instant":
    return p.end.isoformat() if p.end else p.id
  if p.period_type == "forever":
    return "forever"
  start = p.start.isoformat() if p.start else "?"
  end = p.end.isoformat() if p.end else "?"
  return f"{start}..{end}"


def _period_view(p: Period, facts: int | None = None) -> dict[str, Any]:
  out: dict[str, Any] = {
    "key": _period_key(p),
    "type": p.period_type,
    "start": p.start.isoformat() if p.start else None,
    "end": p.end.isoformat() if p.end else None,
    "duration": p.duration_type,
    "calendar": p.calendar_period_key,
  }
  if facts is not None:
    out["facts"] = facts
  return out


def _precision(decimals: str | None) -> float:
  if decimals is None:
    return -math.inf
  if decimals.strip().upper() == "INF":
    return math.inf
  try:
    return float(decimals)
  except ValueError:
    return -math.inf


def _dims_key(f: XbrlFact) -> tuple[tuple[str, str], ...]:
  return tuple(
    sorted((d.axis_qname, d.member_qname or d.typed_value or "") for d in f.dims)
  )


def _dedup(facts: list[XbrlFact]) -> list[XbrlFact]:
  """One fact per (concept, period, unit, dimensions): the most precise."""
  best: dict[tuple[Any, ...], XbrlFact] = {}
  for f in facts:
    key = (f.concept_qname, f.period_id, f.unit_id, _dims_key(f), f.language)
    current = best.get(key)
    if current is None or _precision(f.decimals) > _precision(current.decimals):
      best[key] = f
  return list(best.values())


def _consolidated(facts: list[XbrlFact]) -> list[XbrlFact]:
  return [f for f in facts if not f.dims]


def _span_days(p: Period | None) -> int:
  if p is None or p.period_type != "duration" or p.start is None or p.end is None:
    return 0
  return (p.end - p.start).days


def _end_of(p: Period | None) -> date:
  return p.end if p is not None and p.end else date.min


def _fact_view(f: XbrlFact, lf: LoadedFiling, idx: Index) -> dict[str, Any]:
  concept = lf.model.concepts.get(f.concept_qname)
  p = idx.periods.get(f.period_id)
  out: dict[str, Any] = {
    "concept": f.concept_qname,
    "label": _pref_label(concept, f.concept_qname),
    "period": _period_key(p),
    "period_type": p.period_type if p else None,
  }
  if p is not None and p.duration_type:
    out["duration"] = p.duration_type
  if f.is_nil:
    out["nil"] = True
  elif f.value_kind == "numeric":
    out["value"] = f.numeric_value
    unit = idx.units.get(f.unit_id or "")
    out["unit"] = unit.measure if unit else f.unit_id
    out["decimals"] = f.decimals
  else:
    text = _strip_html(f.value_str or "")
    if concept is not None and concept.is_textblock:
      out["text_chars"] = len(text)
      out["preview"] = text[:TEXT_PREVIEW]
    else:
      out["value"] = text[: TEXT_PREVIEW * 4]
  if f.dims:
    out["dims"] = [
      {"axis": d.axis_qname, "member": d.member_qname or d.typed_value} for d in f.dims
    ]
  return out


def _resolve_concepts(
  model: XbrlModel, elements: list[str]
) -> tuple[list[str], list[str]]:
  """Element names → concept qnames present in the filing.

  A prefixed name must match a qname (case-insensitively); a bare name
  matches the local name of any prefix. Unresolved names are returned
  beside the hits so the caller can say so.
  """
  lower_qnames = {q.lower(): q for q in model.concepts}
  by_local: dict[str, list[str]] = defaultdict(list)
  for q in model.concepts:
    by_local[q.split(":")[-1].lower()].append(q)
  hits: list[str] = []
  unresolved: list[str] = []
  for raw in elements:
    name = raw.strip()
    if not name:
      continue
    if ":" in name:
      q = lower_qnames.get(name.lower())
      if q:
        hits.append(q)
        continue
      name = name.split(":", 1)[1]
    matches = by_local.get(name.lower(), [])
    if matches:
      hits.extend(matches)
    else:
      unresolved.append(raw)
  seen: set[str] = set()
  ordered = [q for q in hits if not (q in seen or seen.add(q))]
  return ordered, unresolved


def _parse_period_end(value: str | None) -> tuple[date | None, int | None]:
  """``YYYY-MM-DD`` → a date; ``YYYY`` → a year; anything else is an error."""
  if not value:
    return None, None
  text = value.strip()
  if re.fullmatch(r"\d{4}", text):
    return None, int(text)
  try:
    return date.fromisoformat(text[:10]), None
  except ValueError as exc:
    raise ToolError(f"period_end must be YYYY-MM-DD or YYYY, not {value!r}") from exc


def _period_filter(idx: Index, period_end: str | None, period_type: str | None) -> Any:
  end_date, end_year = _parse_period_end(period_end)
  ptype = (period_type or "").strip().lower() or None
  if ptype and ptype not in {
    "instant",
    "duration",
    "annual",
    "quarterly",
    "semi_annual",
    "nine_months",
  }:
    raise ToolError(
      "period_type must be instant, duration, annual, quarterly, semi_annual "
      f"or nine_months, not {period_type!r}"
    )

  def keep(f: XbrlFact) -> bool:
    p = idx.periods.get(f.period_id)
    if p is None:
      return False
    if end_date is not None and p.end != end_date:
      return False
    if end_year is not None and (p.end is None or p.end.year != end_year):
      return False
    if ptype in ("instant", "duration") and p.period_type != ptype:
      return False
    if ptype in ("annual", "quarterly", "semi_annual", "nine_months"):
      if p.duration_type != ptype:
        return False
    return True

  return keep


def _period_wanted(p: Period | None, key: str, wanted: set[str]) -> bool:
  """Whether a statement column was asked for by key, end date, or year."""
  if key in wanted:
    return True
  if p is None or p.end is None:
    return False
  return p.end.isoformat() in wanted or str(p.end.year) in wanted


def _span(s: TextSection) -> tuple[int, int] | None:
  """The characters a located section covers, or ``None`` when it was not
  located: its body, any heading this server rendered for it, and the blank
  line that closes it. The one rule — both the label on a hit and the count
  of where matches fall read it here, so they cannot disagree about whether
  a character belongs to a section."""
  if s.offset is None:
    return None
  return s.offset - s.heading_chars, s.offset + s.chars + 2


def _section_at(sections: list[TextSection], offset: int) -> str | None:
  """The label of the innermost located section covering ``offset``."""
  best: TextSection | None = None
  best_offset = -1
  for s in sections:
    span = _span(s)
    if span is None or span[0] > offset or offset >= span[1]:
      continue
    if span[0] >= best_offset:
      best, best_offset = s, span[0]
  return best.label if best else None


# -- the tools -------------------------------------------------------------------


def list_filings(session: FilingSession) -> dict[str, Any]:
  rows = []
  for lf in session.all():
    m = lf.model
    rows.append(
      {
        "id": lf.id,
        "source": lf.source,
        "entity": m.entity.name,
        "ticker": m.entity.ticker,
        "form": m.filing.form,
        "period_end": m.filing.report_date.isoformat()
        if m.filing.report_date
        else None,
        "facts": len(m.facts),
      }
    )
  return {"filings": rows, "count": len(rows)}


LOAD_RECEIPT_KEYS = ("profile", "taxonomy", "filing", "entity", "counts")


def load_receipt(
  lf: LoadedFiling, *, pure: bool = False, whole: bool = True
) -> dict[str, Any]:
  """What ``load_filing`` returns: that the filing is here and what it is.

  Not the map. ``describe_filing`` costs thousands of tokens on a large
  filing — its network and period lists are most of it — and returning the
  same payload from ``load_filing`` meant a caller following the server's
  own instructions ("describe_filing FIRST") paid for it twice in a row.
  The receipt is projected from the description so the two never disagree.
  """
  full = describe_filing(lf, pure=pure, whole=whole)
  receipt: dict[str, Any] = {"loaded": lf.id}
  receipt.update({k: full[k] for k in LOAD_RECEIPT_KEYS if k in full})
  receipt["next"] = (
    "describe_filing for the map — the period keys, the networks by role, the "
    "axes and the text sections. Never guess a concept name or a period key."
  )
  return receipt


def describe_filing(
  lf: LoadedFiling, *, pure: bool = False, whole: bool = True
) -> dict[str, Any]:
  """How the filing is laid out.

  ``pure`` is the faithful-reading profile: nothing the filing does not
  carry — no statement kinds (the networks are listed by the filer's own
  role and definition), no detected Items, no duration buckets on periods.
  ``whole`` selects the primary document as the text the sections and the
  counts describe; ``False`` describes the tagged text blocks alone.
  """
  model, idx = lf.model, index_for(lf)
  text, sections = lf.readable(whole)
  facts = model.facts
  numeric = [f for f in facts if f.value_kind == "numeric"]
  text_blocks = [
    f
    for f in facts
    if f.value_kind == "text"
    and (c := model.concepts.get(f.concept_qname)) is not None
    and c.is_textblock
  ]
  dimensional = [f for f in facts if f.dims]

  period_counts: dict[str, int] = defaultdict(int)
  for f in facts:
    period_counts[f.period_id] += 1
  periods = sorted(
    model.periods,
    key=lambda p: (-period_counts[p.id], _end_of(p)),
  )[:DESCRIBE_PERIODS]
  periods.sort(key=lambda p: (_end_of(p), p.period_type == "instant"), reverse=True)
  period_rows: list[dict[str, Any]] = []
  for p in periods:
    row: dict[str, Any] = {"key": _period_key(p), "facts": period_counts[p.id]}
    if p.period_type == "instant":
      row["instant"] = True
    elif p.duration_type and not pure:
      row["duration"] = p.duration_type
    period_rows.append(row)

  axes: dict[str, dict[str, Any]] = {}
  for f in dimensional:
    for d in f.dims:
      entry = axes.setdefault(d.axis_qname, {"members": set(), "facts": 0})
      entry["members"].add(d.member_qname or d.typed_value or "")
      entry["facts"] += 1
  axis_rows = sorted(
    (
      {"axis": axis, "members": len(v["members"]), "facts": v["facts"]}
      for axis, v in axes.items()
    ),
    key=lambda r: -r["facts"],
  )[:DESCRIBE_AXES]

  statements: list[dict[str, Any]] = []
  disclosures: list[dict[str, Any]] = []
  for n in idx.presentation:
    concepts = {q for a in n.arcs for q in (a.from_qname, a.to_qname)}
    kind = None if pure else idx.classification.get(n.role_uri)
    if kind:
      statements.append(
        {
          "id": _network_id(n),
          "name": _network_name(n)[:100],
          "kind": kind,
          "role": n.role_uri,
          "concepts": len(concepts),
        }
      )
    else:
      disclosures.append(
        {
          "id": _network_id(n),
          "name": _network_name(n)[:100],
          "concepts": len(concepts),
        }
      )

  items = [] if pure else [s for s in sections if s.kind == "item"]
  blocks = sorted(
    (s for s in sections if s.kind == "text_block"), key=lambda s: -s.chars
  )[:DESCRIBE_TEXT_BLOCKS]

  if pure:
    networks_view: dict[str, Any] = {
      "networks": disclosures,
      "networks_note": (
        "presentation networks by the filer's own role and definition; "
        "`statement` takes an id, a name (or part of one), or a role"
      ),
    }
    section_items: dict[str, Any] = {}
  else:
    networks_view = {
      "statements": statements,
      "disclosures": disclosures,
      "networks_note": (
        "`statement` takes an id, a name (or part of one), a role, or a kind "
        "(balance_sheet, income_statement, cash_flow_statement, equity_statement)"
      ),
    }
    section_items = {
      "items": [
        {"id": s.id, "label": s.label, "offset": s.offset, "chars": s.chars}
        for s in items
      ]
    }

  filing = model.filing
  entity = model.entity
  return {
    "profile": {
      "pure": pure,
      "text": "primary document" if whole and lf.has_document else "tagged text blocks",
      "xbrl": lf.has_xbrl,
    },
    **({"taxonomy": _describe_taxonomy(lf.taxonomy)} if lf.taxonomy else {}),
    "filing": {
      "id": lf.id,
      "source": lf.source,
      "accession": filing.accession,
      "form": filing.form,
      "filing_date": filing.filing_date.isoformat() if filing.filing_date else None,
      "period_end": filing.report_date.isoformat() if filing.report_date else None,
      "fiscal_year": filing.fiscal_year_focus,
      "fiscal_period": filing.fiscal_period_focus,
      "fiscal_year_end_month": filing.fiscal_year_end_month,
      "inline_xbrl": filing.is_inline_xbrl,
      "primary_document": filing.primary_document,
      "extension_namespace": filing.extension_namespace,
      "taxonomies": len(filing.taxonomy_namespaces),
      **(
        {"items": describe_items(filing.items), "items_note": items_note(filing.items)}
        if filing.items
        else {}
      ),
    },
    "entity": {
      "name": entity.name,
      "ticker": entity.ticker,
      "identifier": entity.cik,
      "scheme": entity.scheme,
      "sic": entity.sic,
      "sic_description": entity.sic_description,
      "fiscal_year_end": entity.fiscal_year_end,
    },
    "counts": {
      "facts": len(facts),
      "numeric_facts": len(numeric),
      "consolidated_numeric_facts": len(_consolidated(numeric)),
      "dimensional_facts": len(dimensional),
      "text_blocks": len(text_blocks),
      "concepts_reported": len(idx.by_concept),
      "concepts_in_dts": len(model.concepts),
      "periods": len(model.periods),
      "units": len(model.units),
      "networks": {
        "presentation": len(idx.presentation),
        "calculation": len(idx.calculation),
        "definition": len(idx.definition),
      },
      "text_chars": len(text),
    },
    "periods": period_rows,
    "periods_note": (
      f"{min(len(model.periods), DESCRIBE_PERIODS)} of {len(model.periods)} periods, "
      "the most reported; `key` is what fact_grid and statement return "
      "(start..end for a flow, one date for a balance)."
    ),
    "units": [u.measure for u in model.units],
    **networks_view,
    "axes": axis_rows,
    "sections": {
      **section_items,
      "text_blocks": [
        {"id": s.id, "offset": s.offset, "chars": s.chars} for s in blocks
      ],
      "text_block_count": len([s for s in sections if s.kind == "text_block"]),
      "records": [
        {
          "name": t.name,
          "rows": len(t.rows),
          "columns": t.columns,
        }
        for t in (lf.xml_document.tables if lf.xml_document else [])
      ],
      "note": "offsets index into the plain text that search_text and read_text read",
    },
    "next": _next_steps(lf),
  }


def _describe_taxonomy(taxonomy: TaxonomyEntry) -> dict[str, Any]:
  """Which entry point a taxonomy package was loaded from, and the others it
  offers — the choice stated, never silent."""
  return {
    "entry_point": {
      "name": taxonomy.entry_point.name,
      "document": taxonomy.entry_point.document,
    },
    "other_entry_points": [
      {"name": e.name, "document": e.document} for e in taxonomy.others
    ],
    "note": (
      "a taxonomy with no report: concepts and networks, no facts, periods or "
      "units. load_filing with the same source and `entry_point` set to another "
      "entry point's document loads that one instead."
    ),
  }


def _next_steps(lf: LoadedFiling) -> list[str]:
  """What to call next, given what this filing actually is.

  An 8-K leads with its exhibits whatever else is true of it: its tagged
  content is a cover page, and the thing worth reading is attached. So when
  the item codes say the substance is elsewhere, that goes first — above the
  fact tools, which for an 8-K have almost nothing to work with.
  """
  if lf.taxonomy is not None:
    return [
      "resolve_element to find concepts by phrase — each with its label, type, "
      "balance, period type and the networks it sits in",
      "disclosures for the taxonomy's networks as families",
      "statement or information_block with a network from the list above to "
      "read its tree",
      "calculation for what sums to a total, where the taxonomy has calculation arcs",
    ]
  exhibit_first: list[str] = []
  if is_earnings_release(lf.model.filing.items):
    exhibit_first = [
      "documents, then read_document on the EX-99.1 — Item 2.02 means the "
      "results are in the attached release, not in this filing's XBRL",
    ]
  elif lf.model.filing.items:
    exhibit_first = [
      "documents to list what was filed with this 8-K — its tagged content is "
      "the cover page, so the substance is in the exhibits",
    ]
  if not lf.has_xbrl:
    document = [
      *exhibit_first,
      "search_text for anything in the document, read_text to page it",
    ]
    if lf.xml_document:
      return [
        "records for this form's tables — its transactions, holdings or rows",
        *document,
      ]
    return document
  return [
    *exhibit_first,
    "resolve_element to turn a phrase into the concepts this filing reports",
    "fact_grid for consolidated values by concept and period",
    "statement with a network from the list above",
    "calculation for what sums to a total",
    "search_text for anything in the document text, read_text to page it",
  ]


def _require_xbrl(lf: LoadedFiling, what: str) -> None:
  """Refuse an XBRL question about a filing that carries no XBRL.

  Most of EDGAR is document-only, and "no network matches" would send a
  caller hunting for a name that was never going to be there.
  """
  if lf.has_xbrl:
    return
  form = lf.model.filing.form or "This filing"
  raise ToolError(
    f"{form} carries no XBRL, so it has no {what}. It is a document: "
    "search_text and read_text read it"
    + (", and records returns its tables." if lf.xml_document else ".")
  )


def records(
  lf: LoadedFiling, table: str | None = None, limit: int = 100
) -> dict[str, Any]:
  """The record tables of an XML filing — a Form 4's transactions, a 13F's
  holdings — as rows, with the document's header fields alongside."""
  doc = lf.xml_document
  if doc is None:
    raise ToolError(
      "This filing is not an XML document; records reads the ownership forms, "
      "13F, N-PORT and the rest of EDGAR's XML. Use fact_grid or statement "
      "for an XBRL filing."
    )
  wanted = (table or "").strip().lower()
  tables = doc.tables
  if wanted:
    tables = [t for t in doc.tables if t.name.lower() == wanted]
    if not tables:
      names = [t.name for t in doc.tables]
      raise ToolError(f"No table {table!r} in this document; it has {names}")
  return {
    "document": doc.root,
    "form": lf.model.filing.form or doc.form_hint,
    "fields": doc.fields,
    "tables": [
      {
        "name": t.name,
        "columns": t.columns,
        "row_count": len(t.rows),
        "rows": t.rows[:limit],
        "truncated": len(t.rows) > limit,
      }
      for t in tables
    ],
  }


def documents(lf: LoadedFiling, session: Any) -> dict[str, Any]:
  """What else was filed with this filing: exhibits, and any second document
  the form's content actually lives in."""
  found = session.other_documents(lf)
  unreadable = [d for d in found if not d.is_readable]
  return {
    "filing": lf.id,
    "primary_document": lf.model.filing.document_name,
    "documents": [
      {
        "document": d.document,
        "type": d.type,
        "description": d.description or None,
        "size": d.size,
        "url": d.url or None,
        # A PDF or an image is content this reader cannot open. Saying where
        # it is beats leaving it out: whoever asked may well be able to.
        "read": "read_document"
        if d.is_readable
        else f"fetch the url — this server does not read {d.suffix or 'binary'}",
      }
      for d in found
    ],
    "count": len(found),
    "note": (
      "read_document reads the HTML, XML and text ones. "
      + (
        f"{len(unreadable)} of these are not text — fetch the url. "
        if unreadable
        else ""
      )
      + "The XBRL package and the SEC's own rendered copies are not listed."
    )
    if found
    else "Nothing was filed with this one but the primary document.",
  }


def read_document(
  lf: LoadedFiling,
  session: Any,
  document: str,
  offset: int = 0,
  length: int = MAX_READ,
) -> dict[str, Any]:
  """Read one of the filing's other documents — an exhibit, a 13F's holdings.

  Fetched on first read and kept, so paging through one costs one fetch.
  """
  read = session.read_other_document(lf, document)
  text = read.text
  offset = max(0, int(offset or 0))
  length = max(1, min(int(length or MAX_READ), MAX_READ))
  window = text[offset : offset + length]
  out: dict[str, Any] = {
    "document": document,
    "offset": offset,
    "length": len(window),
    "total_chars": len(text),
    "text": window,
  }
  if offset + len(window) < len(text):
    out["next_offset"] = offset + len(window)
  if read.xml_document is not None:
    out["records"] = [
      {"name": t.name, "columns": t.columns, "row_count": len(t.rows)}
      for t in read.xml_document.tables
    ]
    out["records_note"] = "the rows are in `text`, rendered as markdown tables"
  return out


def resolve_element(lf: LoadedFiling, query: str, limit: int = 20) -> dict[str, Any]:
  _require_xbrl(lf, "concepts")
  model, idx = lf.model, index_for(lf)
  q = (query or "").strip()
  if not q:
    raise ToolError("query is required")
  ql = q.lower()
  tokens = [t for t in re.split(r"[\s_\-]+", ql) if t]
  limit = max(1, min(int(limit or 20), 100))

  scored: list[tuple[float, int, str]] = []
  for qname, concept in model.concepts.items():
    if concept.is_hypercube_item or concept.is_dimension_item:
      continue
    local = concept.name.lower()
    labels = [lb.value.lower() for lb in concept.labels if lb.value]
    pref = (concept.pref_label or "").lower()
    score = 0.0
    if qname.lower() == ql:
      score = 100
    elif local == ql or pref == ql:
      score = 90
    elif local.startswith(ql) or pref.startswith(ql):
      score = 70
    elif tokens and all(t in local for t in tokens):
      score = 60
    elif tokens and any(all(t in lb for t in tokens) for lb in labels):
      score = 50
    elif ql in qname.lower():
      score = 40
    if score:
      scored.append((score, len(idx.by_concept.get(qname, ())), qname))
  scored.sort(key=lambda t: (-t[0], -t[1], t[2]))

  rows = []
  for score, count, qname in scored[:limit]:
    concept = model.concepts[qname]
    roles = idx.concept_roles.get(qname, [])
    names = []
    for role in roles[:3]:
      network = next((n for n in idx.presentation if n.role_uri == role), None)
      names.append(((network.definition if network else None) or role)[:80])
    row: dict[str, Any] = {
      "qname": qname,
      "label": _pref_label(concept),
      "type": concept.nice_type or concept.item_type,
      "period_type": concept.period_type,
      "balance": concept.balance,
      "abstract": concept.is_abstract,
      "facts": count,
      "in": names,
    }
    doc = _documentation(concept)
    if doc:
      row["documentation"] = doc[:240]
    rows.append(row)
  return {"query": q, "matches": rows, "match_count": len(scored)}


def fact_grid(
  lf: LoadedFiling,
  elements: list[str],
  period_end: str | None = None,
  period_type: str | None = None,
  include_dimensions: bool = False,
  axis: str | None = None,
  member: str | None = None,
  limit: int = 200,
  pure: bool = False,
) -> dict[str, Any]:
  _require_xbrl(lf, "facts")
  model, idx = lf.model, index_for(lf)
  if not elements:
    raise ToolError("elements is required: one or more concept names")
  if pure and (period_type or "").strip().lower() in BUCKET_PERIOD_TYPES:
    raise ToolError(
      "period_type buckets (annual, quarterly, …) are not part of the filing; "
      "under the pure profile filter by period_end or by instant / duration"
    )
  qnames, unresolved = _resolve_concepts(model, elements)
  keep = _period_filter(idx, period_end, period_type)
  limit = max(1, min(int(limit or 200), MAX_GRID_ROWS))
  axis_l = (axis or "").strip().lower()
  member_l = (member or "").strip().lower()
  dimensional = bool(include_dimensions or axis_l or member_l)

  facts: list[XbrlFact] = []
  for q in qnames:
    facts.extend(idx.by_concept.get(q, []))
  facts = [f for f in facts if keep(f)]
  if dimensional:
    if axis_l or member_l:
      facts = [
        f
        for f in facts
        if f.dims
        and (not axis_l or any(axis_l in d.axis_qname.lower() for d in f.dims))
        and (
          not member_l
          or any(
            member_l in (d.member_qname or d.typed_value or "").lower() for d in f.dims
          )
        )
      ]
  else:
    facts = _consolidated(facts)
  facts = _dedup(facts)
  order = {q: i for i, q in enumerate(qnames)}
  facts.sort(
    key=lambda f: (
      -_end_of(idx.periods.get(f.period_id)).toordinal(),
      order.get(f.concept_qname, 0),
      len(f.dims),
      _dims_key(f),
    )
  )
  rows = [_fact_view(f, lf, idx) for f in facts[:limit]]
  if pure:
    for row in rows:
      row.pop("duration", None)
  out: dict[str, Any] = {
    "rows": rows,
    "row_count": len(rows),
    "total": len(facts),
    "truncated": len(facts) > limit,
    "resolved": qnames,
    "note": (
      "rows with `dims` are member breakdowns; rows without are the consolidated totals"
      if dimensional
      else "consolidated totals only (facts with no dimensional qualifier); "
      "pass include_dimensions, axis or member for breakdowns"
    ),
  }
  excluded = _excluded_concepts(
    qnames,
    idx,
    model,
    shown={f.concept_qname for f in facts},
    keep=keep,
    period_type=period_type,
    dimensional=dimensional,
    filtered_members=bool(axis_l or member_l),
  )
  if excluded:
    out["excluded"] = excluded
    out["excluded_note"] = (
      "the filing reports these, but every fact was filtered out — a concept "
      "in `resolved` with no row is otherwise indistinguishable from one the "
      "filer never tagged"
    )
  if unresolved:
    out["unresolved"] = unresolved
    out["hint"] = "resolve_element finds the concept names this filing reports"
  return out


def _never_tagged(concept: Concept | None) -> dict[str, str]:
  """Why a concept the filing declares carries no fact anywhere in it.

  ``_resolve_concepts`` matches against ``model.concepts``, which the parse
  fills from facts, dimension axes and members, *and* network-arc endpoints —
  so a name resolves whenever the filing's own taxonomy uses it, tagged or
  not. Measured over three filers, everything that lands here is an abstract
  or a member: `resolve_element` ranks both among its matches, and its own
  advice is to pass what it returns to `fact_grid`. Neither is a reportable
  value, and saying which it is beats an empty answer.
  """
  if concept is None:
    return {"reason": "the filing declares it but reports no fact for it"}
  if concept.is_abstract:
    return {
      "reason": "abstract — a presentation header, not a reported value",
      "try": "statement or information_block renders the rows beneath it",
    }
  if concept.is_domain_member:
    return {
      "reason": "a domain member — it qualifies other facts, it does not carry one",
      "try": "pass it as `member` alongside the concept you want broken out",
    }
  if concept.is_dimension_item:
    return {
      "reason": "an axis — it qualifies other facts, it does not carry one",
      "try": "pass it as `axis` alongside the concept you want broken out",
    }
  return {
    "reason": "in this filing's taxonomy, but never tagged with a fact",
    "try": "resolve_element lists the concepts this filer actually reports",
  }


def _excluded_concepts(
  qnames: list[str],
  idx: Index,
  model: XbrlModel,
  *,
  shown: set[str],
  keep: Any,
  period_type: str | None,
  dimensional: bool,
  filtered_members: bool,
) -> list[dict[str, Any]]:
  """Resolved concepts that contributed no row, and what emptied each.

  ``unresolved`` means a name this filing's taxonomy does not contain at
  all. Everything else that produces no row used to come back silently,
  reading as "not reported" when it meant something narrower, in two
  shapes: a concept whose facts a filter removed — a balance-sheet concept
  under a duration bucket is the common one, since an instant has no
  duration — and a concept the filing declares but never tags, which is
  where an abstract or a member lands (see :func:`_never_tagged`).
  """
  ptype = (period_type or "").strip().lower() or None
  out: list[dict[str, Any]] = []
  for q in qnames:
    if q in shown:
      continue
    all_facts = idx.by_concept.get(q, [])
    if not all_facts:
      out.append({"concept": q, "facts": 0, **_never_tagged(model.concepts.get(q))})
      continue
    kept = [f for f in all_facts if keep(f)]
    row: dict[str, Any] = {"concept": q, "facts": len(all_facts)}
    if not kept:
      shapes = {
        p.period_type
        for f in all_facts
        if (p := idx.periods.get(f.period_id)) is not None
      }
      if ptype in ("duration",) + BUCKET_PERIOD_TYPES and shapes == {"instant"}:
        row["reason"] = (
          f"all {len(all_facts)} facts are instants; period_type={ptype!r} "
          "keeps durations only"
        )
        row["try"] = "period_end for the fiscal period with its closing balances"
      elif ptype == "instant" and shapes == {"duration"}:
        row["reason"] = (
          f"all {len(all_facts)} facts are durations; period_type='instant' "
          "keeps instants only"
        )
        row["try"] = "period_type='duration', or drop it"
      else:
        row["reason"] = "no fact falls in the periods asked for"
        row["try"] = "widen or drop period_end / period_type"
    elif not dimensional:
      row["reason"] = (
        f"all {len(kept)} facts in these periods carry a dimension; "
        "consolidated totals only"
      )
      row["try"] = "include_dimensions=true"
    elif filtered_members:
      row["reason"] = (
        f"no fact matches the axis / member asked for ({len(kept)} in period)"
      )
      row["try"] = "drop axis / member, or check describe_filing for the axes present"
    else:
      continue
    out.append(row)
  return out


def _find_network(idx: Index, statement: str, *, pure: bool = False) -> Network:
  s = (statement or "").strip()
  if not s:
    raise ToolError("statement is required: a role, a statement name, or part of one")
  sl = s.lower()
  for n in idx.presentation:
    if n.role_uri == s or (n.role_id and n.role_id == s):
      return n
  for n in idx.presentation:
    if _network_id(n).lower() == sl or _network_name(n).lower() == sl:
      return n
  # Kinds are our classification, not the filing's; the pure profile only
  # knows the filer's own names.
  kind = None if pure else _PRIMARY_ALIASES.get(sl)
  if kind is None and not pure:
    for alias, k in _PRIMARY_ALIASES.items():
      if alias in sl and ("parenthetical" not in sl):
        kind = k
        break
  if kind:
    primary = [
      n for n in idx.presentation if idx.classification.get(n.role_uri) == kind
    ]
    if len(primary) == 1:
      return primary[0]
    if primary:
      # Several classify alike (income and comprehensive income, or the
      # statement and a reprise of it in a note): the one that calls itself
      # a statement, is not the comprehensive one, and has the shortest name.
      return min(
        primary,
        key=lambda n: (
          "statement" not in (n.definition or "").lower(),
          "comprehensive" in (n.definition or "").lower(),
          len(n.definition or n.role_uri),
        ),
      )
  candidates = [
    n
    for n in idx.presentation
    if sl in (n.definition or "").lower() or sl in n.role_uri.lower()
  ]
  if len(candidates) == 1:
    return candidates[0]
  if not candidates:
    raise ToolError(
      f"No presentation network matches {statement!r}; describe_filing lists them"
    )
  names = [(n.definition or n.role_uri)[:90] for n in candidates[:10]]
  raise ToolError(
    f"{len(candidates)} networks match {statement!r}; choose one: {names}"
  )


def statement(
  lf: LoadedFiling,
  statement: str,
  periods: list[str] | None = None,
  max_rows: int = MAX_STATEMENT_ROWS,
  pure: bool = False,
  offset: int = 0,
) -> dict[str, Any]:
  _require_xbrl(lf, "presentation networks")
  model, idx = lf.model, index_for(lf)
  network = _find_network(idx, statement, pure=pure)
  max_rows = max(1, min(int(max_rows or MAX_STATEMENT_ROWS), MAX_STATEMENT_ROWS))
  offset = max(0, int(offset or 0))

  children: dict[str, list[Arc]] = defaultdict(list)
  parents: set[str] = set()
  for arc in network.arcs:
    children[arc.from_qname].append(arc)
    parents.add(arc.to_qname)
  for arcs in children.values():
    arcs.sort(key=lambda a: a.order if a.order is not None else 0.0)
  roots = [q for q in children if q not in parents]
  if not roots and network.arcs:
    roots = [network.arcs[0].from_qname]

  rows: list[dict[str, Any]] = []
  used_periods: dict[str, int] = defaultdict(int)
  truncated = False
  walked = 0
  page_trail: tuple[str, ...] = ()

  def row_at(qname: str, depth: int, label_role: str | None) -> dict[str, Any]:
    concept = model.concepts.get(qname)
    row: dict[str, Any] = {
      "depth": depth,
      "concept": qname,
      "label": _label_for_role(concept, label_role),
    }
    if concept is not None and concept.is_abstract:
      row["abstract"] = True
    else:
      values: dict[str, Any] = {}
      for f in _dedup(_consolidated(idx.by_concept.get(qname, []))):
        p = idx.periods.get(f.period_id)
        key = _period_key(p)
        if key is None:
          continue
        if f.is_nil:
          values[key] = None
        elif f.value_kind == "numeric":
          values[key] = f.numeric_value
        else:
          text = _strip_html(f.value_str or "")
          values[key] = (
            text if len(text) <= TEXT_PREVIEW else f"[text {len(text)} chars]"
          )
        used_periods[key] += 1
      if values:
        row["values"] = values
    return row

  def visit(
    qname: str, depth: int, label_role: str | None, trail: tuple[str, ...]
  ) -> None:
    nonlocal truncated, walked, page_trail
    if len(rows) >= max_rows:
      truncated = True
      return
    walked += 1
    if walked > offset:
      if not rows:
        page_trail = trail
      rows.append(row_at(qname, depth, label_role))
    if qname in trail or depth > 14:
      return
    for arc in children.get(qname, []):
      visit(arc.to_qname, depth + 1, arc.preferred_label, trail + (qname,))

  for root in roots:
    visit(root, 0, None, ())
  if offset and not rows:
    raise ToolError(f"offset {offset} is past the end of this network ({walked} rows)")

  period_by_key = {_period_key(p): p for p in model.periods}
  wanted = None
  if periods:
    wanted = {w.strip() for w in periods if w and w.strip()}
  keys = list(used_periods)
  if wanted:
    keys = [k for k in keys if _period_wanted(period_by_key.get(k), k, wanted)]
  # Newest first; at one end date the longer span first (the year ahead of
  # its fourth quarter), then the column more rows use.
  keys.sort(
    key=lambda k: (
      _end_of(period_by_key.get(k)),
      _span_days(period_by_key.get(k)),
      used_periods[k],
    ),
    reverse=True,
  )
  if not wanted:
    keys = keys[:MAX_STATEMENT_COLUMNS]
  keep = set(keys)
  for row in rows:
    if "values" in row:
      row["values"] = {k: v for k, v in row["values"].items() if k in keep}
      if not row["values"]:
        del row["values"]

  head: dict[str, Any] = {"role": network.role_uri, "name": network.definition}
  if not pure:
    head["kind"] = idx.classification.get(network.role_uri)
  columns = [_period_view(period_by_key[k]) for k in keys]
  if pure:
    for column in columns:
      column.pop("duration", None)
      column.pop("calendar", None)
  return {
    "statement": head,
    "columns": columns,
    "rows": rows,
    "row_count": len(rows),
    "truncated": truncated,
    **_page_fields(model, offset, len(rows), truncated, page_trail),
    "note": (
      "consolidated values only, most precise duplicate kept; depth is the "
      "presentation nesting; a label like 'Total' or a negated label reflects "
      "the preferred label on the arc; a truncated network continues from "
      "`next_offset` passed as `offset`"
    ),
  }


def _page_fields(
  model: XbrlModel,
  offset: int,
  returned: int,
  truncated: bool,
  trail: tuple[str, ...],
) -> dict[str, Any]:
  """Where a page of a presentation walk sits in the whole: the offset it
  starts at, the headers above its first row — a later page opens deep in
  the tree, and its depths alone do not say under what — and where the next
  page starts."""
  out: dict[str, Any] = {}
  if offset:
    out["offset"] = offset
    out["ancestors"] = [
      {"concept": q, "label": _pref_label(model.concepts.get(q), q)} for q in trail
    ]
  if truncated:
    out["next_offset"] = offset + returned
  return out


def calculation(
  lf: LoadedFiling,
  concept: str,
  role: str | None = None,
  period_end: str | None = None,
) -> dict[str, Any]:
  _require_xbrl(lf, "calculations")
  model, idx = lf.model, index_for(lf)
  qnames, _unresolved = _resolve_concepts(model, [concept])
  if not qnames:
    raise ToolError(f"No concept {concept!r} in this filing; try resolve_element")
  qname = qnames[0]
  keep = _period_filter(idx, period_end, None)
  role_l = (role or "").strip().lower()

  networks = [
    n
    for n in idx.calculation
    if any(a.from_qname == qname for a in n.arcs)
    and (
      not role_l
      or role_l in (n.definition or "").lower()
      or role_l in n.role_uri.lower()
    )
  ]
  out: dict[str, Any] = {
    "concept": qname,
    "label": _pref_label(model.concepts.get(qname), qname),
    "networks": [],
  }
  if not networks:
    parents = sorted(
      {
        a.from_qname
        for n in idx.calculation
        for a in n.arcs
        if a.to_qname == qname
        and (not role_l or role_l in (n.definition or "").lower())
      }
    )
    out["note"] = f"{qname} is not a calculation parent in this filing" + (
      f"; it contributes to {parents}" if parents else ""
    )
    return out

  parent_facts = [
    f for f in _dedup(_consolidated(idx.by_concept.get(qname, []))) if keep(f)
  ]
  parent_by_period: dict[tuple[str, str | None], XbrlFact] = {
    (f.period_id, f.unit_id): f for f in parent_facts
  }

  for n in networks:
    arcs = sorted(
      (a for a in n.arcs if a.from_qname == qname),
      key=lambda a: a.order if a.order is not None else 0.0,
    )
    child_facts: dict[str, dict[tuple[str, str | None], XbrlFact]] = {}
    for a in arcs:
      facts = _dedup(_consolidated(idx.by_concept.get(a.to_qname, [])))
      child_facts[a.to_qname] = {(f.period_id, f.unit_id): f for f in facts}
    period_keys = set(parent_by_period)
    for facts_by_period in child_facts.values():
      period_keys |= {k for k in facts_by_period if keep(facts_by_period[k])}
    periods_out = []
    for pid, uid in sorted(
      period_keys, key=lambda k: _end_of(idx.periods.get(k[0])), reverse=True
    ):
      p = idx.periods.get(pid)
      unit = idx.units.get(uid or "")
      parent = parent_by_period.get((pid, uid))
      contributions = []
      computed = 0.0
      missing = []
      for a in arcs:
        f = child_facts[a.to_qname].get((pid, uid))
        weight = a.weight if a.weight is not None else 1.0
        if f is None or f.numeric_value is None:
          missing.append(a.to_qname)
          continue
        computed += weight * f.numeric_value
        contributions.append(
          {
            "concept": a.to_qname,
            "label": _pref_label(model.concepts.get(a.to_qname), a.to_qname),
            "weight": weight,
            "value": f.numeric_value,
          }
        )
      reported = parent.numeric_value if parent is not None else None
      entry: dict[str, Any] = {
        "period": _period_key(p),
        "unit": unit.measure if unit else uid,
        "reported": reported,
        "computed": computed if contributions else None,
        "difference": (reported - computed)
        if reported is not None and contributions
        else None,
        "contributions": contributions,
      }
      if missing:
        entry["missing"] = missing
      periods_out.append(entry)
    out["networks"].append(
      {
        "role": n.role_uri,
        "name": n.definition,
        "children": [
          {
            "concept": a.to_qname,
            "label": _pref_label(model.concepts.get(a.to_qname), a.to_qname),
            "weight": a.weight if a.weight is not None else 1.0,
          }
          for a in arcs
        ],
        "periods": periods_out,
      }
    )
  out["note"] = (
    "computed = sum(weight × child) over the children with a consolidated fact "
    "in that period and unit; missing lists children without one"
  )
  return out


# -- disclosures and information blocks: the map and the block ------------------


Blocks = tuple[list[InformationBlock], dict[str, list[XbrlFact]], list[Disclosure]]


def blocks_for(lf: LoadedFiling) -> Blocks:
  """The filing's roles read whole, their facts, and the families they form —
  computed once per loaded filing."""
  cached = getattr(lf, "_blocks", None)
  if cached is not None:
    return cached
  blocks = plan_blocks(lf.model)
  membership = fact_membership(lf.model, blocks)
  families = group_disclosures(blocks)
  out: Blocks = (blocks, membership, families)
  lf._blocks = out  # pyright: ignore[reportAttributeAccessIssue]
  return out


def _text_block_sections(lf: LoadedFiling, whole: bool) -> dict[str, TextSection]:
  """Text-block sections by id — the block's concept qname, on every path."""
  _text, sections = lf.readable(whole)
  return {s.id: s for s in sections if s.kind == "text_block"}


def _is_text_block(model: XbrlModel, f: XbrlFact) -> bool:
  if f.value_kind != "text":
    return False
  concept = model.concepts.get(f.concept_qname)
  return concept is not None and concept.is_textblock


def _block_summary(
  st: InformationBlock,
  facts: list[XbrlFact],
  idx: Index,
  model: XbrlModel,
  *,
  pure: bool,
) -> dict[str, Any]:
  facts = _dedup(facts)
  counted = [f for f in facts if not _is_text_block(model, f)]
  row: dict[str, Any] = {
    "id": st.id,
    "level": st.level,
    "name": st.subtitle or st.name,
    "facts": len(counted),
    "concepts": len(st.concepts),
  }
  if st.block_type:
    row["block_type"] = st.block_type
  if not pure:
    kind = idx.classification.get(st.role_uri)
    if kind:
      row["kind"] = kind
  axes = [a.qname for a in st.axes]
  if axes:
    row["axes"] = axes
    row["dimensional_facts"] = sum(1 for f in counted if f.dims)
  if st.has_calc:
    row["calc"] = True
  blocks: dict[str, int] = {}
  for f in facts:
    if _is_text_block(model, f) and f.concept_qname not in blocks:
      blocks[f.concept_qname] = len(_strip_html(f.value_str or ""))
  if blocks:
    row["text_blocks"] = [{"concept": q, "chars": n} for q, n in blocks.items()]
  return row


def disclosures(
  lf: LoadedFiling, topic: str | None = None, *, pure: bool = False
) -> dict[str, Any]:
  """The filing's sections as families — a note with its policies, tables
  and details — or one family's index when ``topic`` names it."""
  _require_xbrl(lf, "presentation networks")
  model, idx = lf.model, index_for(lf)
  _blocks, membership, families = blocks_for(lf)

  if topic and topic.strip():
    t = topic.strip().lower()
    hits = [f for f in families if f.name.lower() == t] or [
      f for f in families if t in f.name.lower()
    ]
    if not hits:
      raise ToolError(
        f"No disclosure matches {topic!r}; call disclosures with no topic to list them"
      )
    if len(hits) > 1:
      names = [f.name for f in hits[:12]]
      raise ToolError(f"{len(hits)} disclosures match {topic!r}; choose one: {names}")
    fam = hits[0]
    return {
      "disclosure": fam.name,
      "category": fam.category,
      "blocks": [
        _block_summary(st, membership.get(st.role_uri, []), idx, model, pure=pure)
        for st in fam.blocks
      ],
      "block_count": len(fam.blocks),
      "note": (
        "one entry per role in this family, in filing order; `id` is what "
        "information_block and statement take; `facts` counts numeric facts "
        "this section admits, `dimensional_facts` those broken out by its axes"
      ),
    }

  rows: list[dict[str, Any]] = []
  for fam in families:
    fact_ids: set[str] = set()
    text_blocks: set[str] = set()
    for st in fam.blocks:
      for f in _dedup(membership.get(st.role_uri, [])):
        if _is_text_block(model, f):
          text_blocks.add(f.concept_qname)
        else:
          fact_ids.add(f.id)
    row: dict[str, Any] = {
      "disclosure": fam.name,
      "blocks": len(fam.blocks),
      "levels": fam.levels,
      "facts": len(fact_ids),
    }
    if fam.category and fam.category != "Disclosure":
      row["category"] = fam.category
    if text_blocks:
      row["text_blocks"] = len(text_blocks)
    rows.append(row)
  return {
    "disclosures": rows,
    "count": len(rows),
    "note": (
      "families read from the filer's own role titles, in filing order — "
      "statements, the cover page and the notes alike; call disclosures with a "
      "topic for one family's blocks, then information_block for the one you "
      "need"
    ),
  }


def _find_block(
  idx: Index, blocks: list[InformationBlock], block: str, *, pure: bool
) -> InformationBlock:
  network = _find_network(idx, block, pure=pure)
  for st in blocks:
    if st.role_uri == network.role_uri:
      return st
  raise ToolError(f"No information block for {block!r}")  # pragma: no cover


def _tolerance(decimals: str | None) -> float:
  """Half a unit at the fact's stated precision — a total reported to the
  million foots when it is within half a million of its children."""
  precision = _precision(decimals)
  if math.isinf(precision):
    return 0.5 if precision < 0 else 0.0
  return 0.5 * 10 ** (-precision)


def _member_key(f: XbrlFact, axis_order: dict[str, int]) -> str:
  parts = sorted(f.dims, key=lambda d: axis_order.get(d.axis_qname, len(axis_order)))
  keys = [d.member_qname or f"{d.axis_qname}={d.typed_value}" for d in parts]
  return " | ".join(keys)


def information_block(
  lf: LoadedFiling,
  block: str,
  periods: list[str] | None = None,
  member: str | None = None,
  max_rows: int = MAX_BLOCK_ROWS,
  max_members: int | None = None,
  offset: int = 0,
  *,
  pure: bool = False,
  whole: bool = True,
) -> dict[str, Any]:
  """One section read whole: rows in presentation order with consolidated
  values, the same rows by the section's own axes, its calculation arcs
  with a footing check, and its text blocks with offsets.

  Member breakdowns are kept most-reported first, up to the response budget
  (or ``max_members`` when the caller sets one); a row is never left blank
  by that cut, and every row says how many breakdowns it lost.

  A section longer than ``max_rows`` — a taxonomy's own networks run to
  hundreds of concepts — pages by ``offset``. The axes, calculation arcs and
  text blocks describe the section, not a page, and come with the first page
  alone; a later page carries its rows and their columns."""
  _require_xbrl(lf, "presentation networks")
  model, idx = lf.model, index_for(lf)
  blocks, membership, families = blocks_for(lf)
  st = _find_block(idx, blocks, block, pure=pure)
  max_rows = max(1, min(int(max_rows or MAX_BLOCK_ROWS), MAX_BLOCK_ROWS))
  offset = max(0, int(offset or 0))
  member_limit = (
    max(1, min(int(max_members), MAX_BLOCK_MEMBERS_CAP))
    if max_members is not None
    else None
  )
  member_l = (member or "").strip().lower()
  axis_order = {a.qname: i for i, a in enumerate(st.axes)}

  consolidated: dict[str, list[XbrlFact]] = defaultdict(list)
  dimensional: dict[str, list[tuple[str, XbrlFact]]] = defaultdict(list)
  text_facts: dict[str, XbrlFact] = {}
  member_counts: dict[str, int] = defaultdict(int)
  axis_member_counts: dict[tuple[str, str], int] = defaultdict(int)
  for f in _dedup(membership.get(st.role_uri, [])):
    if _is_text_block(model, f):
      text_facts.setdefault(f.concept_qname, f)
      continue
    if not f.dims:
      consolidated[f.concept_qname].append(f)
      continue
    key = _member_key(f, axis_order)
    if member_l and member_l not in key.lower():
      continue
    dimensional[f.concept_qname].append((key, f))
    member_counts[key] += 1
    for d in f.dims:
      axis_member_counts[(d.axis_qname, d.member_qname or d.typed_value or "")] += 1
  ranked = sorted(member_counts.items(), key=lambda kv: (-kv[1], kv[0]))
  kept_members: list[str] = []
  if member_limit is not None:
    kept_members = [k for k, _ in ranked[:member_limit]]
  else:
    spent = 0
    for key, n in ranked:
      cost = len(key) + MEMBER_CELL_CHARS * n
      if kept_members and (
        spent + cost > BLOCK_MEMBER_CHARS or len(kept_members) >= MAX_BLOCK_MEMBERS_CAP
      ):
        break
      kept_members.append(key)
      spent += cost
  keep_members = set(kept_members)
  members_omitted = len(member_counts) - len(kept_members)

  # The axes, domains and members of this block's own cube: a filer lists
  # them in the presentation tree, often without declaring the extension
  # members abstract, and none of them carries facts of its own.
  structural_names: set[str] = set()
  for cube in st.hypercubes:
    structural_names.add(cube.qname)
    for axis in cube.axes:
      structural_names.add(axis.qname)
      if axis.domain:
        structural_names.add(axis.domain)
      structural_names.update(axis.members)

  # The presentation walk, as `statement` makes it.
  network = st.presentation[0]
  children: dict[str, list[Arc]] = defaultdict(list)
  parents: set[str] = set()
  for n in st.presentation:
    for arc in n.arcs:
      children[arc.from_qname].append(arc)
      parents.add(arc.to_qname)
  for arcs in children.values():
    arcs.sort(key=lambda a: a.order if a.order is not None else 0.0)
  roots = [q for q in children if q not in parents]
  if not roots and network.arcs:
    roots = [network.arcs[0].from_qname]

  rows: list[dict[str, Any]] = []
  used_periods: dict[str, int] = defaultdict(int)
  truncated = False
  walked = 0
  page_trail: tuple[str, ...] = ()

  def cell(f: XbrlFact) -> Any:
    if f.is_nil:
      return None
    if f.value_kind == "numeric":
      return f.numeric_value
    text = _strip_html(f.value_str or "")
    return text if len(text) <= TEXT_PREVIEW else f"[text {len(text)} chars]"

  def visit(
    qname: str, depth: int, label_role: str | None, trail: tuple[str, ...]
  ) -> None:
    nonlocal truncated, walked, page_trail
    if len(rows) >= max_rows:
      truncated = True
      return
    walked += 1
    if walked > offset:
      if not rows:
        page_trail = trail
      rows.append(row_at(qname, depth, label_role))
    if qname in trail or depth > 14:
      return
    for arc in children.get(qname, []):
      visit(arc.to_qname, depth + 1, arc.preferred_label, trail + (qname,))

  def row_at(qname: str, depth: int, label_role: str | None) -> dict[str, Any]:
    concept = model.concepts.get(qname)
    row: dict[str, Any] = {
      "depth": depth,
      "concept": qname,
      "label": _label_for_role(concept, label_role),
    }
    # Headers, axes, hypercubes and the cube's members carry no facts of
    # their own. (``is_domain_member`` is not the test: in XDT every primary
    # item is a domain member, so that flag is true of a line item too.)
    structural = qname in structural_names or (
      concept is not None
      and (
        concept.is_abstract or concept.is_dimension_item or concept.is_hypercube_item
      )
    )
    if structural:
      row["abstract"] = True
    else:
      values: dict[str, Any] = {}
      for f in consolidated.get(qname, []):
        key = _period_key(idx.periods.get(f.period_id))
        if key is None:
          continue
        values[key] = cell(f)
        used_periods[key] += 1
      by_member: dict[str, dict[str, Any]] = {}
      breakdowns = dimensional.get(qname, [])
      if breakdowns:
        keys_here = {mkey for mkey, _ in breakdowns}
        shown = keys_here & keep_members
        if not values and not shown:
          # The cap never leaves a row blank: its most-reported breakdown
          # stands in for it, and the count below says what it lost.
          shown = {min(keys_here, key=lambda k: (-member_counts[k], k))}
        for mkey, f in breakdowns:
          if mkey not in shown:
            continue
          key = _period_key(idx.periods.get(f.period_id))
          if key is None:
            continue
          by_member.setdefault(mkey, {})[key] = cell(f)
          used_periods[key] += 1
        if len(keys_here) > len(shown):
          row["members_omitted"] = len(keys_here) - len(shown)
      if values:
        row["values"] = values
      if by_member:
        row["members"] = by_member
    return row

  for root in roots:
    visit(root, 0, None, ())
  if offset and not rows:
    raise ToolError(f"offset {offset} is past the end of this block ({walked} rows)")

  period_by_key = {_period_key(p): p for p in model.periods}
  wanted = {w.strip() for w in periods if w and w.strip()} if periods else None
  keys = list(used_periods)
  if wanted:
    keys = [k for k in keys if _period_wanted(period_by_key.get(k), k, wanted)]
  # An annual report's details tables carry the quarterly note figures too;
  # left to recency alone they take five of the eight columns and push the
  # third fiscal year out. Under the product profile the form's own span —
  # a year, and balances — comes first; the pure profile keeps recency.
  annual_form = not pure and (model.filing.form or "").upper() in (
    "10-K",
    "10-K/A",
    "20-F",
    "40-F",
  )

  def column_rank(k: str) -> tuple[Any, ...]:
    p = period_by_key.get(k)
    own_span = p is not None and (
      p.period_type == "instant" or p.duration_type == "annual"
    )
    return (
      own_span if annual_form else True,
      _end_of(p),
      _span_days(p),
      used_periods[k],
    )

  keys.sort(key=column_rank, reverse=True)
  if not wanted:
    # Columns are kept in that order up to a cell budget, never fewer than
    # the statement's eight: a sparse narrative table keeps its issuance
    # dates, a wide statement stays bounded.
    kept_keys: list[str] = []
    cells = 0
    for k in keys:
      cost = used_periods[k] * MEMBER_CELL_CHARS
      if len(kept_keys) >= MAX_STATEMENT_COLUMNS and cells + cost > BLOCK_COLUMN_CHARS:
        break
      kept_keys.append(k)
      cells += cost
    keys = kept_keys
  columns_omitted = len(used_periods) - len(keys)
  keys.sort(
    key=lambda k: (
      _end_of(period_by_key.get(k)),
      _span_days(period_by_key.get(k)),
      used_periods[k],
    ),
    reverse=True,
  )
  keep = set(keys)

  def newest(period_keys: set[str]) -> str:
    return max(
      period_keys,
      key=lambda k: (_end_of(period_by_key.get(k)), _span_days(period_by_key.get(k))),
    )

  for row in rows:
    row_periods: set[str] = set(row.get("values", {}))
    for vals in row.get("members", {}).values():
      row_periods |= set(vals)
    if not row_periods:
      continue
    shown = row_periods & keep
    if not shown and not wanted:
      # The column cut never leaves a row blank: its most recent period
      # stands in for it, outside the columns, and the count says the rest.
      shown = {newest(row_periods)}
    if "values" in row:
      row["values"] = {k: v for k, v in row["values"].items() if k in shown}
      if not row["values"]:
        del row["values"]
    if "members" in row:
      trimmed = {
        m: {k: v for k, v in vals.items() if k in shown}
        for m, vals in row["members"].items()
      }
      row["members"] = {m: vals for m, vals in trimmed.items() if vals}
      if not row["members"]:
        del row["members"]
    if len(row_periods) > len(shown):
      row["periods_omitted"] = len(row_periods) - len(shown)

  # The section's axes, with the members that carry facts here.
  axes_out: list[dict[str, Any]] = []
  for axis in st.axes:
    present = sorted(
      ((m, n) for (a, m), n in axis_member_counts.items() if a == axis.qname and m),
      key=lambda kv: (-kv[1], kv[0]),
    )
    entry: dict[str, Any] = {
      "axis": axis.qname,
      "label": _pref_label(model.concepts.get(axis.qname), axis.qname),
      "members": [
        {
          "member": m,
          "label": _pref_label(model.concepts.get(m), m),
          "facts": n,
        }
        for m, n in present[:AXIS_MEMBERS_LISTED]
      ],
    }
    if len(present) > AXIS_MEMBERS_LISTED:
      entry["members_omitted"] = len(present) - AXIS_MEMBERS_LISTED
    if axis.default:
      entry["default"] = axis.default
    if axis.typed:
      entry["typed"] = True
    axes_out.append(entry)

  # Calculation arcs in this role, footed on the consolidated values shown.
  # A total that foots says so in one number; only a difference is spelled
  # out, with the reported and computed values behind it.
  calc_out: list[dict[str, Any]] = []
  fact_at: dict[tuple[str, str, str | None], XbrlFact] = {}
  for q, facts in consolidated.items():
    for f in facts:
      fact_at[(q, f.period_id, f.unit_id)] = f
  for n in st.calculation:
    by_parent: dict[str, list[Arc]] = defaultdict(list)
    for arc in n.arcs:
      by_parent[arc.from_qname].append(arc)
    for parent, arcs in by_parent.items():
      arcs.sort(key=lambda a: a.order if a.order is not None else 0.0)
      checked = 0
      differences: dict[str, dict[str, Any]] = {}
      for f in consolidated.get(parent, []):
        key = _period_key(idx.periods.get(f.period_id))
        if key not in keep or f.numeric_value is None:
          continue
        computed = 0.0
        present_children = 0
        for arc in arcs:
          child = fact_at.get((arc.to_qname, f.period_id, f.unit_id))
          if child is None or child.numeric_value is None:
            continue
          present_children += 1
          computed += (arc.weight if arc.weight is not None else 1.0) * (
            child.numeric_value
          )
        if not present_children:
          continue
        checked += 1
        difference = f.numeric_value - computed
        if abs(difference) > _tolerance(f.decimals):
          differences[key] = {
            "reported": f.numeric_value,
            "computed": computed,
            "difference": difference,
          }
          if present_children < len(arcs):
            differences[key]["missing"] = len(arcs) - present_children
      roll: dict[str, Any] = {
        "total": parent,
        "children": [
          {"concept": a.to_qname, "weight": a.weight if a.weight is not None else 1.0}
          for a in arcs
        ],
      }
      if checked:
        roll["foots"] = checked - len(differences)
        roll["checked"] = checked
      if differences:
        roll["differences"] = differences
      calc_out.append(roll)

  # Tagged text blocks in this role, with where to read them.
  sections = _text_block_sections(lf, whole)
  text_out: list[dict[str, Any]] = []
  for q, f in text_facts.items():
    text = _strip_html(f.value_str or "")
    block_text: dict[str, Any] = {
      "concept": q,
      "label": _pref_label(model.concepts.get(q), q),
      "chars": len(text),
      "preview": text[:BLOCK_TEXT_PREVIEW],
    }
    section = sections.get(q)
    if section is not None and section.offset is not None:
      block_text["offset"] = section.offset
    text_out.append(block_text)

  family = next((f for f in families if st in f.blocks), None)
  head: dict[str, Any] = {
    "id": st.id,
    "role": st.role_uri,
    "name": st.name,
    "disclosure": st.disclosure,
    "level": st.level,
  }
  if st.block_type:
    head["block_type"] = st.block_type
  if st.merged_roles:
    head["merged_roles"] = list(st.merged_roles)
  if not pure:
    kind = idx.classification.get(st.role_uri)
    if kind:
      head["kind"] = kind
  if family is not None and len(family.blocks) > 1:
    head["siblings"] = [
      {"id": s.id, "level": s.level, "name": s.subtitle or s.name}
      for s in family.blocks
      if s is not st
    ]

  columns = [_period_view(period_by_key[k]) for k in keys]
  if pure:
    for column in columns:
      column.pop("duration", None)
      column.pop("calendar", None)
  out: dict[str, Any] = {"block": head, "columns": columns}
  # The axes, calculation and text describe the section; a later page of it
  # would only repeat them.
  first_page = not offset
  if axes_out and first_page:
    out["axes"] = axes_out
  out["rows"] = rows
  if calc_out and first_page:
    out["calculation"] = calc_out
  if text_out and first_page:
    out["text"] = text_out
  out["row_count"] = len(rows)
  out["truncated"] = truncated
  out.update(_page_fields(model, offset, len(rows), truncated, page_trail))
  if members_omitted:
    out["members_omitted"] = members_omitted
  if columns_omitted:
    out["periods_omitted"] = columns_omitted
    out["periods_tip"] = (
      "pass `periods` (keys, end dates or years) to choose the columns; a row "
      "whose only facts fall outside them keeps its most recent one"
    )
  out["note"] = (
    "rows follow the presentation tree, `abstract` marking headers, axes, "
    "domains and members; `values` are consolidated (no "
    "dimensional qualifier), `members` the same row broken out by this "
    "section's own axes — a member key joins one member per axis; "
    "`members_omitted` and `periods_omitted` on a row count the breakdowns "
    "and columns dropped from it, and a row is never left blank by either cut; "
    "`calculation` lists each total's children with weights, how many of the "
    "shown periods foot on consolidated values, and any difference; `text` "
    "entries are tagged text blocks — read one with read_text from its offset; "
    "a truncated block continues from `next_offset` passed as `offset`, and "
    "axes, calculation and text come with the first page only"
  )
  return out


_TERM_RE = re.compile(r"[A-Za-z][A-Za-z0-9]{2,}")


def _section_rows(
  sections: list[TextSection], offsets: list[int]
) -> tuple[list[dict[str, Any]], int]:
  """Where a pattern's matches fall, the busiest sections first.

  ``offsets`` are the match starts. A section is located by the same rule as
  :func:`_section_at` — the innermost one covering the offset — over a bisect
  index, so a broad pattern costs a lookup per match rather than a scan.

  A section covers the heading rendered for it, so a reading this server
  assembled from the tagged blocks attributes every match and the rows sum
  to the total: a concept name standing as a block's heading counts to the
  block it titles rather than falling in the gap between two sections, which
  is where the topical word of every block sits. In a parsed document matches
  outside every section — a table of contents, the signatures — are still
  left out; there the rows say where the mass is, not how it partitions.

  Returns the rows and how many sections the matches fall in. The rows are
  the busiest ``SECTION_ROWS`` of those, so the two differ whenever the
  distribution has a tail — which is the ordinary case in a text-block
  reading, where a word appearing in concept names puts one match in each of
  many blocks. A caller given only the rows would read their sum as the match
  count and conclude the rest of the matches were not there.
  """
  located: list[tuple[int, int, str]] = []
  for s in sections:
    span = _span(s)
    if span is not None:
      located.append((span[0], span[1], s.label))
  located.sort(key=lambda t: t[0])
  if not located:
    return [], 0
  starts = [t[0] for t in located]
  counts: Counter[str] = Counter()
  for offset in offsets:
    i = bisect_right(starts, offset) - 1
    for j in range(i, max(-1, i - NEST_SCAN), -1):
      if offset < located[j][1]:
        counts[located[j][2]] += 1
        break
  return [
    {"section": label, "hits": n} for label, n in counts.most_common(SECTION_ROWS)
  ], len(counts)


def _term_rows(text: str, pattern: str) -> list[dict[str, Any]]:
  """How often a missed pattern's own words occur on their own.

  A regular expression is all or nothing: ``customer concentration`` matches
  nothing in a filing that discusses it as *no single customer accounted for*.
  Counting the words separately says which of them the filing uses, and so
  which one to search for instead. Only worth saying when there are two —
  one word that matches nothing is what ``total`` already reported.

  A word is counted where one begins, not wherever its letters appear, so a
  term that only ever trails inside a concept name — ``block`` inside
  ``RevenueRecognitionPolicyTextBlock`` — reports the nothing it is rather
  than sending the caller after a machine token, while a stem written on
  purpose (``terminat``) still counts the words it starts.
  """
  terms = list(dict.fromkeys(m.group(0).lower() for m in _TERM_RE.finditer(pattern)))
  if len(terms) < 2:
    return []
  lowered = text.lower()
  rows = [
    {"term": t, "matches": len(re.findall(rf"\b{re.escape(t)}", lowered))}
    for t in terms[:TERM_ROWS]
  ]
  rows.sort(key=lambda r: -r["matches"])
  return rows


def search_text(
  lf: LoadedFiling,
  pattern: str,
  window: int = DEFAULT_WINDOW,
  max_hits: int = DEFAULT_HITS,
  *,
  whole: bool = True,
  pure: bool = False,
) -> dict[str, Any]:
  """Regex search over the readable text: the whole primary document when
  ``whole`` (and one is held), else the tagged text blocks.

  Hits are the first ``max_hits`` in document order. Two things are said
  about the matches that did not fit in them: ``sections`` counts where all
  of them fall, so a broad pattern routes the next call by weight rather
  than by guess, and on no match at all ``terms`` counts the pattern's own
  words, so a phrase the filer words differently is a step rather than a
  dead end. ``pure`` drops both, and the section label on hits — the
  ladder's exact hit shape.
  """
  text, sections = lf.readable(whole)
  if not (pattern or "").strip():
    raise ToolError("pattern is required")
  try:
    rx = re.compile(pattern, re.IGNORECASE)
  except re.error as exc:
    raise ToolError(f"invalid regular expression: {exc}") from exc
  window = max(40, min(int(window or DEFAULT_WINDOW), MAX_WINDOW))
  max_hits = max(1, min(int(max_hits or DEFAULT_HITS), MAX_HITS))
  half = window // 2
  hits: list[dict[str, Any]] = []
  offsets: list[int] = []
  total = 0
  for m in rx.finditer(text):
    total += 1
    if not pure and total <= SECTION_SCAN:
      offsets.append(m.start())
    if len(hits) >= max_hits:
      continue
    start = max(0, m.start() - half)
    end = min(len(text), m.end() + half)
    hit: dict[str, Any] = {
      "offset": m.start(),
      "match": m.group(0)[:200],
      "text": text[start:end],
    }
    section = None if pure else _section_at(sections, m.start())
    if section:
      hit["section"] = section
    hits.append(hit)

  out: dict[str, Any] = {
    "pattern": pattern,
    "total": total,
    "hits": hits,
    "text_chars": len(text),
  }
  note = "offsets index the plain text; read_text pages from one"
  if not pure:
    if len(hits) < total <= SECTION_SCAN:
      rows, found = _section_rows(sections, offsets)
      if rows:
        out["sections"] = rows
        if found > len(rows):
          out["sections_omitted"] = found - len(rows)
          where = (
            f"`sections` counts the {len(rows)} busiest of {found} sections "
            "they fall in"
          )
        else:
          where = "`sections` counts where all of them fall"
        note = f"{total} matches, {len(hits)} returned; {where}. " + note
    elif total == 0:
      rows = _term_rows(text, pattern)
      if rows:
        out["terms"] = rows
        note = (
          "nothing matched the pattern as written; `terms` counts its words "
          "on their own — search again for the wording the filing uses"
        )
  out["note"] = note
  return out


def read_text(
  lf: LoadedFiling,
  offset: int = 0,
  length: int = DEFAULT_READ,
  *,
  whole: bool = True,
  pure: bool = False,
) -> dict[str, Any]:
  """Page the readable text from an offset. ``pure`` caps a read at the
  ladder's 4,000 characters and omits the section label."""
  text, sections = lf.readable(whole)
  cap = PURE_MAX_READ if pure else MAX_READ
  offset = max(0, int(offset or 0))
  length = max(1, min(int(length or DEFAULT_READ), cap))
  if offset >= len(text):
    raise ToolError(f"offset {offset} is past the end of the text ({len(text)} chars)")
  end = min(len(text), offset + length)
  out: dict[str, Any] = {
    "offset": offset,
    "length": end - offset,
    "text": text[offset:end],
    "text_chars": len(text),
  }
  if end < len(text):
    out["next_offset"] = end
  section = None if pure else _section_at(sections, offset)
  if section:
    out["section"] = section
  return out


def export_filing(lf: LoadedFiling, format: str, out_dir: Path) -> dict[str, Any]:
  fmt = (format or "").strip().lower()
  if fmt not in EXPORT_FORMATS:
    raise ToolError(f"format must be one of {sorted(EXPORT_FORMATS)}, not {format!r}")
  out_dir = Path(out_dir)
  out_dir.mkdir(parents=True, exist_ok=True)
  stem = re.sub(r"[^A-Za-z0-9._-]+", "-", lf.id) or lf.accession
  target = out_dir / f"{stem}.{EXPORT_FORMATS[fmt]}"
  written: list[Path] = [target]
  model = lf.model
  if fmt == "clawdog":
    from xbrlkit.serialize import to_clawdog_report

    document, gaps = to_clawdog_report(model)
    target.write_text(json.dumps(document, indent=2, default=str))
    gaps_path = out_dir / f"{stem}.clawdog.gaps.json"
    gaps_path.write_text(json.dumps(gaps.to_dict(), indent=2, default=str))
    written.append(gaps_path)
  elif fmt == "holon":
    from xbrlkit.serialize import to_holon

    target.write_text(to_holon(model))
  elif fmt == "tavi":
    from xbrlkit.serialize import to_tavi_report

    document, gaps = to_tavi_report(model)
    target.write_text(json.dumps(document, indent=2, default=str))
    gaps_path = out_dir / f"{stem}.tavi.gaps.json"
    gaps_path.write_text(json.dumps(gaps.to_dict(), indent=2, default=str))
    written.append(gaps_path)
  elif fmt == "oim":
    from xbrlkit.serialize import to_oim

    target.write_text(to_oim(model))
  elif fmt == "model":
    # The parse itself: reloadable by load_filing without Arelle.
    target.write_text(model.model_dump_json(indent=2))
  else:
    try:
      from xbrlkit.serialize import build_lbug, to_graph_tables
    except ImportError as exc:  # pragma: no cover - depends on the extra
      raise ToolError("the lpg format needs `pip install 'xbrlkit[lpg]'`") from exc
    try:
      tables = to_graph_tables(model)
      build_lbug(tables, target)
    except ImportError as exc:
      raise ToolError("the lpg format needs `pip install 'xbrlkit[lpg]'`") from exc
  return {
    "format": fmt,
    "path": str(target),
    "bytes": target.stat().st_size if target.is_file() else None,
    "files": [str(p) for p in written],
  }


VIEW_FORMATS = ("holon", "tavi")


def view_filing(lf: LoadedFiling, format: str, viewers: ViewerHost) -> dict[str, Any]:
  """Serialize the filing, serve it on loopback, and return the viewer link.

  The document is not written to disk: the browser is the only reader, and
  ``export_filing`` is the tool for keeping a copy. A filing loaded from a
  document that already is the requested serialization is served as it was
  loaded, not re-projected through the model — the model has no slot for
  everything a producer's document may say.
  """
  fmt = (format or "").strip().lower()
  if fmt not in VIEW_FORMATS:
    raise ToolError(f"format must be one of {sorted(VIEW_FORMATS)}, not {format!r}")
  served = "projected from the model"
  if lf.source_kind == fmt and lf.source_document is not None:
    body = lf.source_document
    served = "as loaded"
  elif fmt == "holon":
    from xbrlkit.serialize import to_holon

    body = to_holon(lf.model)
  else:
    from xbrlkit.serialize import to_tavi_report

    document, _gaps = to_tavi_report(lf.model)
    body = json.dumps(document, indent=2, default=str)
  stem = re.sub(r"[^A-Za-z0-9._-]+", "-", lf.id) or lf.accession
  file_url, page_url = viewers.publish(body, f"{stem}.{EXPORT_FORMATS[fmt]}")
  return {
    "filing": lf.id,
    "format": fmt,
    "viewer_url": page_url,
    "document_url": file_url,
    "bytes": len(body.encode("utf-8")),
    "served": served,
    "note": (
      "Give viewer_url to the user. The document is served from this machine "
      "and readable only by the viewer's origin, while this server runs."
    ),
  }


# -- EDGAR-wide search ----------------------------------------------------------

# EFTS will page through 10,000 hits given the chance. A tool answers into a
# context window, so it returns a sample and says how big the thing sampled is.
SEARCH_MAX_LIMIT = 100
SEARCH_DEFAULT_LIMIT = 20
EFTS_FIRST_YEAR = 2001


def search_filings(
  text_query: str | None = None,
  forms: list[str] | None = None,
  start_date: str | None = None,
  end_date: str | None = None,
  ciks: list[str] | None = None,
  limit: int = SEARCH_DEFAULT_LIMIT,
  config: Config = CONFIG,
) -> dict[str, Any]:
  """Filings across EDGAR matching a phrase, form and date range.

  Returns a page of hits and the total that matched, each hit carrying the
  ``cik:accession`` that ``load_filing`` takes — the point of the tool is that
  a search result is directly loadable.
  """
  if not any((text_query, forms, start_date, end_date, ciks)):
    raise ToolError(
      "Give at least one of text_query, forms, start_date, end_date or ciks; "
      "an unfiltered search matches all of EDGAR."
    )
  limit = max(1, min(limit, SEARCH_MAX_LIMIT))

  from xbrlkit.edgar.efts import EftsClient

  try:
    total, hits = EftsClient(config=config).query_with_total(
      forms=forms,
      start_date=start_date,
      end_date=end_date,
      ciks=ciks,
      text_query=text_query,
      max_results=limit,
    )
  except Exception as exc:  # a network or EDGAR-side failure, not a bad query
    raise ToolError(f"EDGAR full-text search failed: {exc}") from exc

  asked = {str(c).zfill(10) for c in (ciks or [])}
  filings = [
    {
      "source": f"{hit.cik}:{hit.accession}",
      "form": hit.form,
      "filed": hit.filing_date,
      "filer": hit.primary_document,
      **({"parties": list(hit.parties)} if len(hit.parties) > 1 else {}),
      **(
        {"matched_cik": sorted(asked & set(hit.party_ciks))}
        if asked and len(hit.party_ciks) > 1
        else {}
      ),
    }
    for hit in hits
  ]
  result: dict[str, Any] = {
    "query": {
      "text_query": text_query,
      "forms": forms,
      "start_date": start_date,
      "end_date": end_date,
      "ciks": ciks,
    },
    "total_matching": total,
    "returned": len(filings),
    "filings": filings,
    "next": "load_filing with a hit's `source` to read one; search_text to search inside it",
  }
  if asked and any(f.get("parties") for f in filings):
    result["parties_note"] = (
      "a hit with `parties` names more than one filer — an ownership form (3, "
      "4, 5) is associated with both the reporting owner and the issuer, so a "
      "CIK query for a company returns the insider forms filed about it and "
      "`filer` is the individual. `matched_cik` says which of the CIKs asked "
      "for put the hit here; narrow with `forms` to take one side."
    )
  if total > len(filings):
    result["note"] = (
      f"{total} filings match; {len(filings)} shown. Narrow the dates, forms or "
      "phrase rather than raising the limit — the whole set is not the answer."
    )
  return result
