"""Author a report from rows: the model without a filing behind it.

Every other way into an :class:`XbrlModel` starts from a document somebody
already serialized. A report read out of a spreadsheet or a PDF package starts
from rows: this concept, this period, this value, from this cell. ``build_report``
turns those rows into the model, minting the period, unit and fact ids and the
presentation and calculation networks, so the author never writes a holon or a
TAVI by hand and the writers then emit either.

The builder is strict where the readers are tolerant: a row it cannot place is
an error naming the row, never a fact that quietly goes missing.
"""

from __future__ import annotations

import hashlib
import re
from datetime import date
from typing import Literal

from pydantic import BaseModel, Field

from .model import (
  Arc,
  BalanceType,
  Concept,
  DimQualifier,
  EntityIdentity,
  FactProvenance,
  FilingMeta,
  Label,
  Network,
  Period,
  PeriodType,
  Unit,
  XbrlFact,
  XbrlModel,
)
from .namespaces import ENTITY_SCHEME, REPORT_BASE
from .parse.ids import unit_id
from .periods import duration_period, instant_period
from .serialize._kernel.context import CANONICAL_CONTEXT

STANDARD_LABEL_ROLE = "http://www.xbrl.org/2003/role/label"
PARENT_CHILD_ARCROLE = "http://www.xbrl.org/2003/arcrole/parent-child"
SUMMATION_ITEM_ARCROLE = "http://www.xbrl.org/2003/arcrole/summation-item"
ROLE_BASE = "https://robosystems.ai/role"
TEXT_LANGUAGE = "en"
# What a source is called when the row does not say: a place in a document.
DOCUMENT_SPAN = "document_span"

ConceptKind = Literal[
  "monetary",
  "shares",
  "percent",
  "pure",
  "decimal",
  "integer",
  "string",
  "text_block",
  "date",
  "boolean",
]

_ITEM_TYPES: dict[str, str] = {
  "monetary": "monetaryItemType",
  "shares": "sharesItemType",
  "percent": "percentItemType",
  "pure": "pureItemType",
  "decimal": "decimalItemType",
  "integer": "integerItemType",
  "string": "stringItemType",
  "text_block": "textBlockItemType",
  "date": "dateItemType",
  "boolean": "booleanItemType",
}
_NON_NUMERIC = frozenset({"string", "text_block", "date", "boolean"})
_TEXT = frozenset({"string", "text_block"})

# The statements, in the order a report presents them. A section with one of
# these block types takes the SEC role-definition shape readers parse for a
# title, a kind and an order.
STATEMENT_TITLES: dict[str, str] = {
  "balance_sheet": "Balance Sheet",
  "income_statement": "Income Statement",
  "cash_flow_statement": "Cash Flow Statement",
  "equity_statement": "Statement of Changes in Equity",
}

_UNIT_NAMESPACES = {
  "iso4217": "http://www.xbrl.org/2003/iso4217",
  "xbrli": "http://www.xbrl.org/2003/instance",
}
_CURRENCY = re.compile(r"^[A-Z]{3}$")


class AuthoringError(ValueError):
  """The rows do not make a report. ``problems`` names every row at fault."""

  def __init__(self, problems: list[str]) -> None:
    self.problems = problems
    shown = "; ".join(problems[:5])
    more = f" (and {len(problems) - 5} more)" if len(problems) > 5 else ""
    super().__init__(f"{len(problems)} problem(s) in the rows: {shown}{more}")


class EntityRow(BaseModel):
  """Who the report is about: an identifier under the scheme that names it."""

  identifier: str
  scheme: str = ENTITY_SCHEME
  name: str
  legal_name: str | None = None


class ConceptRow(BaseModel):
  """A concept the report uses, declared once."""

  qname: str
  label: str | None = None
  kind: ConceptKind = "monetary"
  period_type: PeriodType | None = None
  balance: BalanceType | None = None
  abstract: bool = False


class FactRow(BaseModel):
  """One value: a concept, a period, and where it was read from.

  ``end`` alone is an instant; ``start`` and ``end`` are a duration, both
  inclusive. ``unit`` is a currency code (``USD``), ``shares`` or ``pure``,
  and is required on a numeric concept. ``source`` is the place in the
  document the value came from.
  """

  concept: str
  value: float | str | None
  end: date
  start: date | None = None
  unit: str | None = None
  decimals: int | str | None = None
  source: str | None = None
  source_kind: str | None = None
  # Axis qname to member qname.
  dims: dict[str, str] = Field(default_factory=dict)


class LineRow(BaseModel):
  """One line of a section, in order. ``weight`` makes it a term of its
  parent's total (``1`` adds, ``-1`` subtracts)."""

  concept: str
  parent: str | None = None
  weight: float | None = None


class SectionRow(BaseModel):
  """A statement or a table: its lines, top to bottom.

  A line with no parent hangs off ``head`` when one is given; with no head it
  is the root of the tree and must have lines beneath it.
  """

  name: str
  block_type: str | None = None
  head: str | None = None
  role_uri: str | None = None
  lines: list[LineRow] = Field(default_factory=list)


class ReportRows(BaseModel):
  """A whole report as rows."""

  report_id: str
  entity: EntityRow
  # How the report was composed. Required: left unset, a holon says
  # ``sec-as-filed``, which an authored report is not.
  reporting_style: str
  period_end: date | None = None
  form: str | None = None
  # Who or what read the values: stamped on every fact that names a source.
  attributed_to: str | None = None
  # Prefix to namespace, for any prefix xbrlkit does not already know.
  namespaces: dict[str, str] = Field(default_factory=dict)
  concepts: list[ConceptRow] = Field(default_factory=list)
  sections: list[SectionRow] = Field(default_factory=list)
  facts: list[FactRow] = Field(default_factory=list)


def build_report(rows: ReportRows) -> XbrlModel:
  """The model for ``rows``. Raises :class:`AuthoringError` naming every row
  that cannot be placed; a report that builds has lost nothing."""
  problems: list[str] = []
  if not rows.report_id.strip():
    problems.append("report_id is empty")
  if not rows.entity.identifier.strip():
    problems.append("entity.identifier is empty")
  if not rows.reporting_style.strip():
    problems.append("reporting_style is empty")

  concepts = _concepts(rows, problems)
  networks = _networks(rows, concepts, problems)
  periods: dict[str, Period] = {}
  units: dict[str, Unit] = {}
  facts: dict[str, XbrlFact] = {}
  for position, row in enumerate(rows.facts):
    fact = _fact(rows, position, row, concepts, periods, units, problems)
    if fact is not None:
      facts.setdefault(fact.id, fact)
  if problems:
    raise AuthoringError(problems)

  return XbrlModel(
    filing=FilingMeta(
      accession=rows.report_id,
      cik=rows.entity.identifier,
      reporting_style=rows.reporting_style,
      form=rows.form,
      report_date=rows.period_end,
      is_inline_xbrl=False,
      report_uri=f"{REPORT_BASE}/{rows.report_id}",
      taxonomy_namespaces=sorted({c.namespace for c in concepts.values()}),
    ),
    entity=EntityIdentity(
      cik=rows.entity.identifier,
      scheme=rows.entity.scheme,
      name=rows.entity.name,
      legal_name=rows.entity.legal_name,
    ),
    concepts=concepts,
    periods=list(periods.values()),
    units=list(units.values()),
    facts=list(facts.values()),
    networks=networks,
  )


def _namespace(qname: str, rows: ReportRows) -> str | None:
  prefix, _, local = qname.partition(":")
  if not local:
    return None
  if prefix in rows.namespaces:
    return rows.namespaces[prefix]
  known = CANONICAL_CONTEXT.get(prefix)
  return known if isinstance(known, str) else None


def _concepts(rows: ReportRows, problems: list[str]) -> dict[str, Concept]:
  out: dict[str, Concept] = {}
  for row in rows.concepts:
    namespace = _namespace(row.qname, rows)
    if namespace is None:
      problems.append(
        f"concept {row.qname!r}: no namespace for its prefix (add it to namespaces)"
      )
      continue
    if row.qname in out:
      problems.append(f"concept {row.qname!r}: declared twice")
      continue
    labels = (
      [Label(value=row.label, role=STANDARD_LABEL_ROLE, language=TEXT_LANGUAGE)]
      if row.label
      else []
    )
    out[row.qname] = Concept(
      qname=row.qname,
      namespace=namespace,
      name=row.qname.partition(":")[2],
      period_type=row.period_type,
      balance=row.balance,
      is_abstract=row.abstract,
      is_numeric=not row.abstract and row.kind not in _NON_NUMERIC,
      is_textblock=row.kind == "text_block",
      is_shares=row.kind == "shares",
      is_integer=row.kind == "integer",
      is_text_fact=row.kind in _TEXT,
      # An abstract concept carries no value, so it has no value type to state.
      item_type=_ITEM_TYPES["string" if row.abstract else row.kind],
      nillable=True,
      pref_label=row.label,
      labels=labels,
    )
  return out


def _slug(value: str) -> str:
  return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-") or "section"


def _definition(section: SectionRow, order: int) -> str:
  title = STATEMENT_TITLES.get(section.block_type or "")
  if title is not None:
    code = list(STATEMENT_TITLES).index(section.block_type or "") + 1
    return f"{code:04d} - Statement - {title}"
  return f"{order:04d} - Disclosure - {section.name}"


def _networks(
  rows: ReportRows, concepts: dict[str, Concept], problems: list[str]
) -> list[Network]:
  out: list[Network] = []
  roles: set[str] = set()
  for index, section in enumerate(rows.sections):
    where = f"section {section.name!r}"
    role = section.role_uri or f"{ROLE_BASE}/{rows.report_id}/{_slug(section.name)}"
    if role in roles:
      problems.append(f"{where}: its role is already used by another section")
      continue
    roles.add(role)
    cited = [line.concept for line in section.lines]
    cited += [line.parent for line in section.lines if line.parent]
    if section.head:
      cited.append(section.head)
    missing = sorted({q for q in cited if q not in concepts})
    if missing:
      problems.append(f"{where}: undeclared concept(s) {', '.join(missing)}")
      continue
    in_section = {line.concept for line in section.lines}
    parents = {line.parent for line in section.lines if line.parent}
    presentation: list[Arc] = []
    calculation: list[Arc] = []
    for order, line in enumerate(section.lines, start=1):
      parent = line.parent or section.head
      if line.parent and line.parent not in in_section and line.parent != section.head:
        problems.append(
          f"{where}: {line.concept!r} names a parent that is not a line of the section"
        )
        continue
      if parent is None:
        if line.concept not in parents:
          problems.append(
            f"{where}: {line.concept!r} has no parent and nothing beneath it "
            "(give the section a head concept)"
          )
        continue
      presentation.append(
        Arc(
          from_qname=parent,
          to_qname=line.concept,
          arcrole=PARENT_CHILD_ARCROLE,
          order=float(order),
        )
      )
      if line.weight is not None and line.parent:
        calculation.append(
          Arc(
            from_qname=line.parent,
            to_qname=line.concept,
            arcrole=SUMMATION_ITEM_ARCROLE,
            order=float(order),
            weight=line.weight,
          )
        )
    # After the SEC's own numbering, so statements sort ahead of the tables.
    definition = _definition(section, len(STATEMENT_TITLES) + index + 1)
    for kind, arcs in (("presentation", presentation), ("calculation", calculation)):
      if not arcs:
        continue
      targets = {arc.to_qname for arc in arcs}
      for arc in arcs:
        arc.is_root = arc.from_qname not in targets
      out.append(
        Network(
          role_uri=role,
          definition=definition,
          kind=kind,
          arcs=arcs,
          block_type=section.block_type,
        )
      )
  return out


def _unit(token: str) -> Unit | None:
  if _CURRENCY.match(token):
    prefix, local = "iso4217", token
  elif token.lower() in ("shares", "pure"):
    prefix, local = "xbrli", token.lower()
  else:
    return None
  uri = f"{_UNIT_NAMESPACES[prefix]}#{local}"
  return Unit(id=unit_id(uri), measure=f"{prefix}:{local}", uri=uri)


def _lexical(value: float) -> str:
  """A number the way XBRL writes it: a whole amount without ``.0``."""
  return str(int(value)) if value == int(value) else repr(value)


def _fact(
  rows: ReportRows,
  position: int,
  row: FactRow,
  concepts: dict[str, Concept],
  periods: dict[str, Period],
  units: dict[str, Unit],
  problems: list[str],
) -> XbrlFact | None:
  where = f"fact {position} ({row.concept})"
  concept = concepts.get(row.concept)
  if concept is None:
    problems.append(f"{where}: undeclared concept")
    return None
  if concept.is_abstract:
    problems.append(f"{where}: an abstract concept carries no value")
    return None
  if row.start is not None and row.start > row.end:
    problems.append(f"{where}: the period starts after it ends")
    return None
  stated: PeriodType = "instant" if row.start is None else "duration"
  if concept.period_type in ("instant", "duration") and concept.period_type != stated:
    problems.append(
      f"{where}: a {concept.period_type} concept reported for a {stated} period"
    )
    return None
  for qname in (*row.dims, *row.dims.values()):
    if qname not in concepts:
      problems.append(f"{where}: undeclared dimension concept {qname!r}")
      return None
  period = (
    instant_period(row.end)
    if row.start is None
    else duration_period(row.start, row.end)
  )

  unit: Unit | None = None
  numeric: float | None = None
  text: str | None
  if concept.is_numeric:
    if isinstance(row.value, bool) or not isinstance(row.value, (int, float)):
      problems.append(f"{where}: a numeric concept needs a number, got {row.value!r}")
      return None
    if row.unit is None:
      problems.append(f"{where}: a numeric fact needs a unit")
      return None
    unit = _unit(row.unit)
    if unit is None:
      problems.append(
        f"{where}: unit {row.unit!r} is not a currency code, 'shares' or 'pure'"
      )
      return None
    numeric = float(row.value)
    text = _lexical(numeric)
  else:
    text = None if row.value is None else str(row.value)

  periods.setdefault(period.id, period)
  if unit is not None:
    units.setdefault(unit.id, unit)
  dims = [
    DimQualifier(axis_qname=axis, member_qname=member)
    for axis, member in sorted(row.dims.items())
  ]
  decimals = None if row.decimals is None else str(row.decimals)
  # Content-derived, so the same row is the same fact on every build and two
  # rows that disagree stay two facts for the check to find.
  stem = "|".join(
    [
      rows.report_id,
      row.concept,
      period.id,
      unit.id if unit else "",
      ";".join(f"{d.axis_qname}={d.member_qname}" for d in dims),
      text or "",
    ]
  )
  provenance = None
  if row.source:
    provenance = FactProvenance(
      source=row.source,
      kind=row.source_kind or DOCUMENT_SPAN,
      attributed_to=rows.attributed_to,
    )
  return XbrlFact(
    id="f-" + hashlib.sha256(stem.encode()).hexdigest()[:24],
    concept_qname=row.concept,
    period_id=period.id,
    unit_id=unit.id if unit else None,
    entity_cik=rows.entity.identifier,
    dims=dims,
    value_str=text,
    numeric_value=numeric,
    decimals=decimals,
    value_kind="numeric" if concept.is_numeric else "text",
    is_nil=text is None,
    language=TEXT_LANGUAGE if concept.is_text_fact else None,
    provenance=provenance,
  )


__all__ = (
  "AuthoringError",
  "ConceptRow",
  "EntityRow",
  "FactRow",
  "LineRow",
  "ReportRows",
  "SectionRow",
  "build_report",
)
