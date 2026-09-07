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
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from xbrlkit.model import Arc, Concept, Network, Period, Unit, XbrlFact, XbrlModel
from xbrlkit.serialize import classify_network
from xbrlkit.serve.session import FilingSession, LoadedFiling, TextSection
from xbrlkit.text.ixbrl import _strip_html

MAX_HITS = 25
DEFAULT_HITS = 10
DEFAULT_WINDOW = 300
MAX_WINDOW = 1500
DEFAULT_READ = 4000
MAX_READ = 8000
MAX_GRID_ROWS = 500
MAX_STATEMENT_ROWS = 400
MAX_STATEMENT_COLUMNS = 8
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
  "holon": "holon.jsonld",
  "tavi": "tavi.json",
  "oim": "oim.json",
  "lpg": "lbug",
}


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
  """
  definition = (n.definition or "").lower()
  if any(marker in definition for marker in _NOT_A_STATEMENT):
    return None
  return classify_network(n.role_uri, n.definition)


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


def _section_at(sections: list[TextSection], offset: int) -> str | None:
  """The label of the innermost located section covering ``offset``."""
  best: TextSection | None = None
  best_offset = -1
  for s in sections:
    start = s.offset
    if start is None or start > offset or offset >= start + s.chars + 2:
      continue
    if start >= best_offset:
      best, best_offset = s, start
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


def describe_filing(lf: LoadedFiling) -> dict[str, Any]:
  model, idx = lf.model, index_for(lf)
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
  period_rows = [
    {"key": _period_key(p), "duration": p.duration_type, "facts": period_counts[p.id]}
    if p.period_type != "instant"
    else {"key": _period_key(p), "instant": True, "facts": period_counts[p.id]}
    for p in periods
  ]

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
    kind = idx.classification.get(n.role_uri)
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

  items = [s for s in lf.sections if s.kind == "item"]
  blocks = sorted(
    (s for s in lf.sections if s.kind == "text_block"), key=lambda s: -s.chars
  )[:DESCRIBE_TEXT_BLOCKS]

  filing = model.filing
  entity = model.entity
  return {
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
      "text_chars": len(lf.text),
    },
    "periods": period_rows,
    "periods_note": (
      f"{min(len(model.periods), DESCRIBE_PERIODS)} of {len(model.periods)} periods, "
      "the most reported; `key` is what fact_grid and statement return "
      "(start..end for a flow, one date for a balance)."
    ),
    "units": [u.measure for u in model.units],
    "statements": statements,
    "disclosures": disclosures,
    "networks_note": (
      "`statement` takes an id, a name (or part of one), a role, or a kind "
      "(balance_sheet, income_statement, cash_flow_statement, equity_statement)"
    ),
    "axes": axis_rows,
    "sections": {
      "items": [
        {"id": s.id, "label": s.label, "offset": s.offset, "chars": s.chars}
        for s in items
      ],
      "text_blocks": [
        {"id": s.id, "offset": s.offset, "chars": s.chars} for s in blocks
      ],
      "text_block_count": len([s for s in lf.sections if s.kind == "text_block"]),
      "note": "offsets index into the plain text that search_text and read_text read",
    },
    "next": [
      "resolve_element to turn a phrase into the concepts this filing reports",
      "fact_grid for consolidated values by concept and period",
      "statement with a `statement` value from `statements` above",
      "calculation for what sums to a total",
      "search_text for anything in the document text, read_text to page it",
    ],
  }


def resolve_element(lf: LoadedFiling, query: str, limit: int = 20) -> dict[str, Any]:
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
) -> dict[str, Any]:
  model, idx = lf.model, index_for(lf)
  if not elements:
    raise ToolError("elements is required: one or more concept names")
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
  if unresolved:
    out["unresolved"] = unresolved
    out["hint"] = "resolve_element finds the concept names this filing reports"
  return out


def _find_network(idx: Index, statement: str) -> Network:
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
  kind = _PRIMARY_ALIASES.get(sl)
  if kind is None:
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
) -> dict[str, Any]:
  model, idx = lf.model, index_for(lf)
  network = _find_network(idx, statement)
  max_rows = max(1, min(int(max_rows or MAX_STATEMENT_ROWS), MAX_STATEMENT_ROWS))

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

  def visit(
    qname: str, depth: int, label_role: str | None, trail: tuple[str, ...]
  ) -> None:
    nonlocal truncated
    if len(rows) >= max_rows:
      truncated = True
      return
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
    rows.append(row)
    if qname in trail or depth > 14:
      return
    for arc in children.get(qname, []):
      visit(arc.to_qname, depth + 1, arc.preferred_label, trail + (qname,))

  for root in roots:
    visit(root, 0, None, ())

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

  return {
    "statement": {
      "role": network.role_uri,
      "name": network.definition,
      "kind": idx.classification.get(network.role_uri),
    },
    "columns": [_period_view(period_by_key[k]) for k in keys],
    "rows": rows,
    "row_count": len(rows),
    "truncated": truncated,
    "note": (
      "consolidated values only, most precise duplicate kept; depth is the "
      "presentation nesting; a label like 'Total' or a negated label reflects "
      "the preferred label on the arc"
    ),
  }


def calculation(
  lf: LoadedFiling,
  concept: str,
  role: str | None = None,
  period_end: str | None = None,
) -> dict[str, Any]:
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


def search_text(
  lf: LoadedFiling,
  pattern: str,
  window: int = DEFAULT_WINDOW,
  max_hits: int = DEFAULT_HITS,
) -> dict[str, Any]:
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
  total = 0
  for m in rx.finditer(lf.text):
    total += 1
    if len(hits) >= max_hits:
      continue
    start = max(0, m.start() - half)
    end = min(len(lf.text), m.end() + half)
    hit: dict[str, Any] = {
      "offset": m.start(),
      "match": m.group(0)[:200],
      "text": lf.text[start:end],
    }
    section = _section_at(lf.sections, m.start())
    if section:
      hit["section"] = section
    hits.append(hit)
  return {
    "pattern": pattern,
    "total": total,
    "hits": hits,
    "text_chars": len(lf.text),
    "note": "offsets index the plain text; read_text pages from one",
  }


def read_text(
  lf: LoadedFiling, offset: int = 0, length: int = DEFAULT_READ
) -> dict[str, Any]:
  offset = max(0, int(offset or 0))
  length = max(1, min(int(length or DEFAULT_READ), MAX_READ))
  if offset >= len(lf.text):
    raise ToolError(
      f"offset {offset} is past the end of the text ({len(lf.text)} chars)"
    )
  end = min(len(lf.text), offset + length)
  out: dict[str, Any] = {
    "offset": offset,
    "length": end - offset,
    "text": lf.text[offset:end],
    "text_chars": len(lf.text),
  }
  if end < len(lf.text):
    out["next_offset"] = end
  section = _section_at(lf.sections, offset)
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
  if fmt == "holon":
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
