"""Walk an Arelle ``ModelXbrl`` into the neutral :class:`XbrlModel`.

This mirrors the ``make_*`` methods of the robosystems SEC adapter
(``adapters/sec/processors/xbrl_graph.py``) — same Arelle touch-points, same
date normalization, same numeric-vs-text convention — but writes Pydantic
objects into one in-memory model instead of a fan of parquet DataFrames.

Key conventions carried over from the adapter:

- **Numeric ⇔ the fact carries a unit** (``f.unit is not None``), *not* the
  concept's declared type.
- Arelle stores instant/end dates as the *exclusive* next midnight, so every
  instant/end date is rolled back one day (``- timedelta(1)``).
- Measures resolve to prefixed tokens (``iso4217:USD``) from the QName.

A fact the walk cannot place is never dropped silently: it is counted on the
model's ``parse_report`` by reason, and a fact under a context the instance
references but never declares is kept when the filing states that context's
period itself (see :func:`_recoverable_contexts`).
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any

from arelle import XbrlConst

from xbrlkit.model import (
  RECOVERED_CONTEXT,
  Arc,
  Concept,
  DimQualifier,
  DroppedFacts,
  DropReason,
  EntityIdentity,
  FactProvenance,
  FilingMeta,
  Label,
  Network,
  NetworkKind,
  ParseReport,
  Period,
  RecoveredContext,
  Reference,
  Unit,
  XbrlFact,
  XbrlModel,
)
from xbrlkit.parse.ids import fact_id, unit_id
from xbrlkit.periods import duration_period, forever_period, instant_period

if TYPE_CHECKING:
  from arelle.ModelXbrl import ModelXbrl

logger = logging.getLogger(__name__)

# Extended-link roles that carry no statement/disclosure network — the label
# and reference linkbases (standard link role) plus the enumeration-list roles.
ROLES_FILTERED = {
  "http://www.xbrl.org/2003/role/link",
  "http://fasb.org/srt/role/srt-eedm/ExtensibleEnumerationLists",
  "http://fasb.org/us-gaap/role/eedm/ExtensibleEnumerationLists",
}

_DEI_FISCAL_YEAR = "dei:DocumentFiscalYearFocus"
_DEI_FISCAL_PERIOD = "dei:DocumentFiscalPeriodFocus"
_DEI_FISCAL_YEAR_END = "dei:CurrentFiscalYearEndDate"

# Concept local names (start, end) under which a filing states the period of
# the context they sit in. The Indian exchanges' results taxonomies report
# these under each column's context, including the ones some filings never
# declare.
_REPORTING_PERIOD_FACTS = (
  ("DateOfStartOfReportingPeriod", "DateOfEndOfReportingPeriod"),
)

# Concepts sampled per dropped group on the parse report.
_DROPPED_CONCEPT_SAMPLE = 5


def to_xbrl_model(
  mx: ModelXbrl,
  filing: FilingMeta,
  *,
  entity_name: str | None = None,
  entity_ein: str | None = None,
  entity_ticker: str | None = None,
  entity: EntityIdentity | None = None,
) -> XbrlModel:
  """Convert a loaded ``ModelXbrl`` into the neutral single-filing model.

  ``entity_name`` / ``entity_ein`` / ``entity_ticker`` come from the EDGAR
  submissions header (the XBRL instance carries only the CIK); pass them when
  available so the reporting entity is fully identified. ``entity`` carries
  the whole header at once (its ``cik`` and ``scheme`` are replaced by what
  the instance says) and wins over the three keyword fields.
  """
  report_uri = filing.accession
  main_cik = _normalize_cik(filing.cik)

  concepts: dict[str, Concept] = {}
  periods: dict[str, Period] = {}
  units: dict[str, Unit] = {}
  facts: list[XbrlFact] = []
  namespaces: set[str] = set()

  entity_scheme: str | None = None
  fiscal_year_focus: str | None = None
  fiscal_period_focus: str | None = None
  fiscal_year_end_month: str | None = None

  tally = _ParseTally()
  rebuilt_contexts = _recoverable_contexts(mx, main_cik)

  for f in mx.facts:
    context = f.context
    context_id = getattr(f, "contextID", None)
    concept = f.concept
    rebuilt: _RebuiltContext | None = None
    if context is None:
      # A rebuilt context is a duration; an instant concept cannot sit in it.
      if getattr(concept, "periodType", None) == "duration":
        rebuilt = rebuilt_contexts.get(context_id or "")
      if rebuilt is None:
        tally.drop("undeclared_context", context_id, f)
        continue
    if concept is None or concept.qname is None:
      tally.drop("undefined_concept", context_id, f)
      continue

    qname_str = str(concept.qname)

    # DEI cover-page fiscal context.
    if qname_str == _DEI_FISCAL_YEAR:
      fiscal_year_focus = _text(f.value) or fiscal_year_focus
    elif qname_str == _DEI_FISCAL_PERIOD:
      fiscal_period_focus = _text(f.value) or fiscal_period_focus
    elif qname_str == _DEI_FISCAL_YEAR_END:
      month = _fiscal_year_end_month(f.value)
      if month is not None:
        fiscal_year_end_month = month

    # Concept (deduped by qname). Full DTS coverage is completed below from
    # dimension members (in _make_dims) and network endpoints (in
    # _make_networks), so abstract headers and axis/member/domain/hypercube
    # concepts also get a Concept with labels + flags.
    _ensure_concept(mx, concepts, namespaces, concept)

    # Period (deduped by content-derived id). Skip facts with invalid dates.
    period = rebuilt.period if rebuilt else _make_period(context)
    if period is None:
      tally.drop("invalid_period", context_id, f)
      continue
    if period.id not in periods:
      periods[period.id] = period

    # Unit (numeric facts only, deduped by resolved measure id).
    unit_ref: str | None = None
    if f.unit is not None:
      unit = _make_unit(f.unit)
      if unit is not None:
        if unit.id not in units:
          units[unit.id] = unit
        unit_ref = unit.id

    # Entity identity (prefer the context whose CIK is the filer's).
    if rebuilt:
      scheme, raw_cik = rebuilt.scheme, rebuilt.identifier
      rebuilt.facts += 1
    else:
      scheme, raw_cik = context.entityIdentifier
    norm_cik = _normalize_cik(raw_cik)
    if entity_scheme is None or norm_cik == main_cik:
      entity_scheme = scheme

    is_numeric = f.unit is not None
    numeric_value: float | None = None
    if is_numeric and f.value is not None:
      try:
        numeric_value = float(str(f.value))
      except (ValueError, TypeError):
        numeric_value = None

    raw_value = getattr(f, "value", None)
    source_hash = _md5(f)
    if rebuilt:
      source_hash = _orphan_hash(f, source_hash, context_id, unit_ref)
    facts.append(
      XbrlFact(
        id=fact_id(report_uri, source_hash if rebuilt else f.md5sum.value),
        concept_qname=qname_str,
        period_id=period.id,
        unit_id=unit_ref,
        entity_cik=norm_cik,
        entity_scheme=scheme,
        entity_identifier=str(raw_cik),
        dims=[] if rebuilt else _make_dims(context, mx, concepts, namespaces),
        value_str=_value_str(f),
        raw_value=str(raw_value) if raw_value is not None else None,
        source_hash=source_hash,
        numeric_value=numeric_value,
        decimals=(str(f.decimals) if (is_numeric and f.decimals is not None) else None),
        value_kind="numeric" if is_numeric else "text",
        language=getattr(f, "xmlLang", None),
        is_nil=bool(getattr(f, "isNil", False)),
        provenance=(
          FactProvenance(kind=RECOVERED_CONTEXT, attributed_to="xbrlkit")
          if rebuilt
          else None
        ),
      )
    )

  parse_report = tally.report(rebuilt_contexts)
  if parse_report is not None:
    logger.warning("%s: %s", report_uri, _describe_report(parse_report))

  # Networks last: their arc endpoints (abstract headers, subtotals, hypercube
  # wiring) complete the concept coverage and add their namespaces.
  networks = _make_networks(mx, concepts, namespaces)

  updated_filing = filing.model_copy(
    update={
      "fiscal_year_focus": fiscal_year_focus or filing.fiscal_year_focus,
      "fiscal_period_focus": fiscal_period_focus or filing.fiscal_period_focus,
      "fiscal_year_end_month": (fiscal_year_end_month or filing.fiscal_year_end_month),
      "taxonomy_namespaces": sorted(set(filing.taxonomy_namespaces) | namespaces),
      "extension_namespace": filing.extension_namespace or _extension_namespace(mx),
    }
  )

  scheme_resolved = entity_scheme or "http://www.sec.gov/CIK"
  if entity is not None:
    resolved_entity = entity.model_copy(
      update={
        "cik": main_cik,
        "scheme": scheme_resolved,
        "legal_name": entity.legal_name or entity.name,
      }
    )
  else:
    resolved_entity = EntityIdentity(
      cik=main_cik,
      scheme=scheme_resolved,
      name=entity_name,
      legal_name=entity_name,
      ein=entity_ein,
      ticker=entity_ticker,
    )

  return XbrlModel(
    filing=updated_filing,
    entity=resolved_entity,
    concepts=concepts,
    periods=list(periods.values()),
    units=list(units.values()),
    facts=facts,
    networks=networks,
    parse_report=parse_report,
  )


@dataclass
class _RebuiltContext:
  """An undeclared context's period and entity, as the parse rebuilt them."""

  period: Period
  start: date
  end: date
  scheme: str | None
  identifier: str
  period_from: list[str]
  entity_from: str
  facts: int = 0


@dataclass
class _ParseTally:
  """Counts the facts the walk leaves out, by reason and context id."""

  groups: dict[tuple[DropReason, str | None], DroppedFacts] = field(
    default_factory=dict
  )

  def drop(self, reason: DropReason, context_id: str | None, fact: Any) -> None:
    group = self.groups.setdefault(
      (reason, context_id), DroppedFacts(reason=reason, context_id=context_id)
    )
    group.facts += 1
    qname = str(getattr(fact, "qname", None) or "")
    if (
      qname
      and qname not in group.concepts
      and len(group.concepts) < _DROPPED_CONCEPT_SAMPLE
    ):
      group.concepts.append(qname)

  def report(self, rebuilt: dict[str, _RebuiltContext]) -> ParseReport | None:
    recovered = [
      RecoveredContext(
        context_id=context_id,
        start=ctx.start,
        end=ctx.end,
        period_id=ctx.period.id,
        facts=ctx.facts,
        period_from=ctx.period_from,
        entity_from=ctx.entity_from,  # pyright: ignore[reportArgumentType]
      )
      for context_id, ctx in rebuilt.items()
      if ctx.facts
    ]
    if not self.groups and not recovered:
      return None
    return ParseReport(dropped=list(self.groups.values()), recovered_contexts=recovered)


def _describe_report(report: ParseReport) -> str:
  """One line for the log: what was dropped and what was rebuilt."""
  parts: list[str] = []
  if report.dropped:
    by_reason: dict[str, int] = {}
    for group in report.dropped:
      by_reason[group.reason] = by_reason.get(group.reason, 0) + group.facts
    parts.append(
      f"{report.dropped_facts} fact(s) dropped ("
      + ", ".join(f"{reason}: {count}" for reason, count in by_reason.items())
      + ")"
    )
  if report.recovered_contexts:
    parts.append(
      f"{report.recovered_facts} fact(s) placed in rebuilt context(s) "
      + ", ".join(r.context_id for r in report.recovered_contexts)
    )
  return "; ".join(parts)


def _recoverable_contexts(mx: ModelXbrl, filing_id: str) -> dict[str, _RebuiltContext]:
  """Rebuild the contexts the instance references but never declares.

  Some filings name a context id on most of their facts and declare no
  ``<xbrli:context>`` for it; Arelle loads those facts with no context. A
  context is rebuilt only when the filing states its period itself: exactly
  one start date and one end date reported under that same id (see
  ``_REPORTING_PERIOD_FACTS``). The period is never taken from a declared
  sibling context — the same filings date some of those wrongly.

  The entity is the one every declared context names. When the declared
  contexts disagree nothing is rebuilt; when the instance declares none,
  ``filing_id`` (the caller's identifier for the filer) stands in, with no
  scheme. A rebuilt context has no dimensions.
  """
  orphans = [f for f in mx.facts if f.context is None and getattr(f, "contextID", None)]
  if not orphans:
    return {}

  entities = {c.entityIdentifier for c in (mx.contexts or {}).values()}
  if len(entities) > 1:
    return {}
  if entities:
    scheme, identifier = next(iter(entities))
    entity_from = "declared_contexts"
  else:
    scheme, identifier, entity_from = None, filing_id, "filing"

  stated: dict[str, dict[str, dict[date, str]]] = {}
  for f in orphans:
    qname = getattr(f, "qname", None)
    local = getattr(qname, "localName", None)
    if not any(local in pair for pair in _REPORTING_PERIOD_FACTS):
      continue
    value = _iso_date(getattr(f, "value", None))
    if value is not None:
      stated.setdefault(f.contextID, {}).setdefault(local, {})[value] = str(qname)

  rebuilt: dict[str, _RebuiltContext] = {}
  for context_id, by_name in stated.items():
    for start_name, end_name in _REPORTING_PERIOD_FACTS:
      starts, ends = by_name.get(start_name, {}), by_name.get(end_name, {})
      if len(starts) != 1 or len(ends) != 1:
        continue
      (start, start_qname), (end, end_qname) = (
        next(iter(starts.items())),
        next(iter(ends.items())),
      )
      if start > end:
        continue
      rebuilt[context_id] = _RebuiltContext(
        period=duration_period(start, end),
        start=start,
        end=end,
        scheme=scheme,
        identifier=str(identifier),
        period_from=[start_qname, end_qname],
        entity_from=entity_from,
      )
      break
  return rebuilt


def _orphan_hash(
  fact: Any, arelle_hash: str | None, context_id: str | None, unit_ref: str | None
) -> str:
  """A hash for a fact Arelle loaded with no context.

  Arelle hashes such a fact by its qname alone — the value, language, nil and
  unit only enter beside a context — so every orphan of one concept shares a
  hash. This folds in what Arelle would have, with the context id standing
  where the context's own hash goes.
  """
  parts = [
    arelle_hash or "",
    context_id or "",
    getattr(fact, "xmlLang", None) or "",
    "nil" if getattr(fact, "isNil", False) else str(getattr(fact, "value", "") or ""),
    unit_ref or "",
  ]
  return hashlib.md5("|".join(parts).encode(), usedforsecurity=False).hexdigest()


def _iso_date(value: Any) -> date | None:
  """An ISO date from a fact's lexical value, or ``None``."""
  try:
    return _to_date(date.fromisoformat(str(value).strip()[:10]))
  except ValueError:
    return None


def _ensure_concept(
  mx: ModelXbrl,
  concepts: dict[str, Concept],
  namespaces: set[str],
  concept: Any,
) -> None:
  """Register a concept (deduped by qname) with its namespace, if valid.

  The single collection point for every concept the slice touches — reported
  facts, dimension axes/members, and network-arc endpoints — so DTS coverage is
  complete rather than fact-driven.
  """
  if concept is None:
    return
  qname = getattr(concept, "qname", None)
  if qname is None:
    return
  qname_str = str(qname)
  if qname_str in concepts:
    return
  concepts[qname_str] = _make_concept(mx, concept)
  concept_ns = getattr(qname, "namespaceURI", None)
  if concept_ns:
    namespaces.add(concept_ns)


def _make_concept(mx: ModelXbrl, concept: Any) -> Concept:
  """Build a :class:`Concept` from an Arelle ``ModelConcept``."""
  qname = concept.qname
  document = getattr(concept, "document", None)
  namespace = getattr(qname, "namespaceURI", None) or (
    getattr(document, "targetNamespace", None) if document else None
  )

  subgroup = getattr(concept, "substitutionGroupQname", None)
  type_qname = getattr(concept, "typeQname", None)
  labels, pref_label = _make_labels(mx, concept)
  references = _make_references(mx, concept)

  return Concept(
    qname=str(qname),
    namespace=namespace or "",
    name=qname.localName,
    period_type=_period_type(getattr(concept, "periodType", None)),
    balance=_balance(getattr(concept, "balance", None)),
    is_abstract=bool(getattr(concept, "isAbstract", False)),
    is_numeric=bool(getattr(concept, "isNumeric", False)),
    is_textblock=bool(getattr(concept, "isTextBlock", False)),
    is_hypercube_item=bool(getattr(concept, "isHypercubeItem", False)),
    is_dimension_item=bool(getattr(concept, "isDimensionItem", False)),
    is_domain_member=bool(getattr(concept, "isDomainMember", False)),
    is_shares=bool(getattr(concept, "isShares", False)),
    is_integer=bool(getattr(concept, "isInteger", False)),
    is_fraction=bool(getattr(concept, "isFraction", False)),
    substitution_group=str(subgroup) if subgroup is not None else None,
    substitution_group_namespace=(
      getattr(subgroup, "namespaceURI", None) if subgroup is not None else None
    ),
    item_type=type_qname.localName if type_qname is not None else None,
    nice_type=getattr(concept, "niceType", None) or None,
    item_type_qname=str(type_qname) if type_qname is not None else None,
    item_type_namespace=(
      getattr(type_qname, "namespaceURI", None) if type_qname is not None else None
    ),
    base_xsd_type=getattr(concept, "baseXsdType", None) or None,
    nillable=str(getattr(concept, "nillable", "false")).lower() == "true",
    is_text_fact=bool(
      getattr(getattr(concept, "type", None), "isOimTextFactType", False)
    ),
    pref_label=pref_label,
    labels=labels,
    references=references,
  )


def _make_references(mx: ModelXbrl, concept: Any) -> list[Reference]:
  """Collect a concept's reference-linkbase entries, one per reference part."""
  references: list[Reference] = []
  rel_set = mx.relationshipSet(XbrlConst.conceptReference)
  for rel in rel_set.fromModelObject(concept):
    ref_obj = rel.toModelObject
    if ref_obj is None:
      continue
    role = getattr(ref_obj, "role", None)
    for part in ref_obj.iterchildren():
      value = getattr(part, "stringValue", None)
      if value is None:
        continue
      references.append(Reference(value=str(value), role=role))
  return references


def _md5(fact: Any) -> str | None:
  """Arelle's MD5 of a fact, as a hex string, when it carries one."""
  digest = getattr(fact, "md5sum", None)
  value = getattr(digest, "value", None) if digest is not None else None
  return str(value) if value else None


def _extension_namespace(mx: ModelXbrl) -> str | None:
  """The filer's own taxonomy namespace: the schema that sits in the filing's
  directory, as opposed to the standard taxonomies fetched by URL."""
  model_document = getattr(mx, "modelDocument", None)
  filing_dir = getattr(model_document, "filepathdir", None)
  if not filing_dir:
    return None
  for namespace, docs in (getattr(mx, "namespaceDocs", None) or {}).items():
    if not docs:
      continue
    if getattr(docs[0], "filepathdir", None) == filing_dir:
      return str(namespace)
  return None


def _make_labels(mx: ModelXbrl, concept: Any) -> tuple[list[Label], str | None]:
  """Collect a concept's label-linkbase entries + its standard label."""
  labels: list[Label] = []
  pref_label: str | None = None
  rel_set = mx.relationshipSet(XbrlConst.conceptLabel)
  for rel in rel_set.fromModelObject(concept):
    label_obj = rel.toModelObject
    if label_obj is None:
      continue
    role = getattr(label_obj, "role", None)
    value = getattr(label_obj, "text", None)
    labels.append(
      Label(
        value=value,
        role=role,
        language=getattr(label_obj, "xmlLang", None),
      )
    )
    if role == XbrlConst.standardLabel and pref_label is None:
      pref_label = value or ""
  if pref_label is None and labels:
    pref_label = labels[0].value or ""
  return labels, pref_label


def _make_period(context: Any) -> Period | None:
  """Build a :class:`Period` from a fact context, or ``None`` if invalid.

  Arelle stores instant/end datetimes as the exclusive next midnight, so both
  are rolled back one day to recover the reported date.
  """
  if context.isInstantPeriod:
    end = _to_date(context.instantDatetime - timedelta(1))
    return instant_period(end) if end else None
  if context.isStartEndPeriod:
    start = _to_date(context.startDatetime)
    end = _to_date(context.endDatetime - timedelta(1))
    return duration_period(start, end) if start and end else None
  if context.isForeverPeriod:
    return forever_period()
  return None


def _make_unit(unit: Any) -> Unit | None:
  """Build a :class:`Unit` from an Arelle ``ModelUnit``."""
  if unit.isSingleMeasure:
    token, uri = _measure_token(unit.measures[0][0])
    return Unit(id=unit_id(uri), measure=token, uri=uri)
  if unit.isDivide:
    num_token, num_uri = _measure_token(unit.measures[0][0])
    den_token, den_uri = _measure_token(unit.measures[1][0])
    return Unit(
      id=unit_id(f"{num_uri}/{den_uri}"),
      measure=f"{num_token}/{den_token}",
      uri=f"{num_uri}/{den_uri}",
      numerator_uri=num_uri,
      denominator_uri=den_uri,
    )
  return None


def _measure_token(qname: Any) -> tuple[str, str]:
  """Resolve a measure QName to ``(prefix:localName, namespace#localName)``."""
  local = qname.localName
  namespace = getattr(qname, "namespaceURI", None) or ""
  prefix = getattr(qname, "prefix", None)
  token = f"{prefix}:{local}" if prefix else local
  uri = f"{namespace}#{local}" if namespace else local
  return token, uri


def _make_dims(
  context: Any,
  mx: ModelXbrl,
  concepts: dict[str, Concept],
  namespaces: set[str],
) -> list[DimQualifier]:
  """Extract explicit + typed dimensional qualifiers from a fact context.

  Registers each axis (and explicit member) as a full :class:`Concept` so the
  serializer can label them, and records segment-vs-scenario per dimension.
  """
  # segDimValues / scenDimValues are keyed by the *axis ModelConcept*, not the
  # QName, so segment/scenario is tested against mem.dimension (the axis concept).
  seg = set(getattr(context, "segDimValues", {}) or {})
  scen = set(getattr(context, "scenDimValues", {}) or {})
  dims: list[DimQualifier] = []
  for dim, mem in context.qnameDims.items():
    axis_concept = getattr(mem, "dimension", None)
    _ensure_concept(mx, concepts, namespaces, axis_concept)
    member_qname: str | None = None
    if mem.isExplicit:
      member = getattr(mem, "member", None)
      _ensure_concept(mx, concepts, namespaces, member)
      if member is not None and member.qname is not None:
        member_qname = str(member.qname)
    dims.append(
      DimQualifier(
        axis_qname=str(dim),
        member_qname=member_qname,
        typed_value=mem.stringValue if mem.isTyped else None,
        is_explicit=bool(mem.isExplicit),
        axis_type=(
          "segment"
          if axis_concept in seg
          else "scenario"
          if axis_concept in scen
          else None
        ),
      )
    )
  return dims


def _make_networks(
  mx: ModelXbrl,
  concepts: dict[str, Concept],
  namespaces: set[str],
) -> list[Network]:
  """Enumerate presentation/calculation/definition networks from base sets.

  Registers every arc endpoint as a :class:`Concept` (completing DTS coverage
  for abstract headers, subtotals, and dimensional wiring) and stamps each arc
  with its specific ``arcrole`` so definition networks stay distinguishable.
  """
  networks: list[Network] = []
  seen: set[tuple[str, str]] = set()

  for base_set_key in mx.baseSets.keys():
    arcrole = base_set_key[0]
    role_uri = base_set_key[1]
    if not isinstance(arcrole, str) or not isinstance(role_uri, str):
      continue
    if role_uri in ROLES_FILTERED:
      continue
    kind = _classify_arcrole(arcrole)
    if kind is None:
      continue
    key = (arcrole, role_uri)
    if key in seen:
      continue
    seen.add(key)

    rels = mx.relationshipSet(arcrole, role_uri)
    if rels is None:
      continue

    roots = set(rels.rootConcepts or [])
    is_calc = kind == "calculation"
    arcs: list[Arc] = []
    for r in rels.modelRelationships:
      frm = r.fromModelObject
      to = r.toModelObject
      if frm is None or to is None or frm.qname is None or to.qname is None:
        continue
      _ensure_concept(mx, concepts, namespaces, frm)
      _ensure_concept(mx, concepts, namespaces, to)
      weight = r.weight if is_calc else None
      arcs.append(
        Arc(
          from_qname=str(frm.qname),
          to_qname=str(to.qname),
          arcrole=arcrole,
          order=float(r.order) if r.order is not None else None,
          weight=float(weight) if weight is not None else None,
          preferred_label=getattr(r, "preferredLabel", None),
          is_root=frm in roots,
          target_role=getattr(r, "targetRole", None) or None,
        )
      )
    if not arcs:
      continue

    networks.append(
      Network(
        role_uri=role_uri,
        definition=_role_definition(mx, role_uri),
        kind=kind,
        arcs=arcs,
        role_id=_role_id(mx, role_uri),
      )
    )
  return networks


def _value_str(fact: object) -> str | None:
  """The fact's value as text, preferring Arelle's typed value.

  ``xValue`` has XML Schema whitespace processing applied, so a token-typed
  fact tagged as ``"2024 "`` reads back as ``"2024"``. Raw ``value`` does not,
  and Arelle's own OIM writer uses ``xValue`` for exactly this reason. Numeric
  facts keep the raw text: their canonical formatting is decided downstream
  from ``numeric_value``, and ``xValue`` is a ``Decimal`` whose ``str`` would
  not match what the filing reported.
  """
  value = getattr(fact, "value", None)
  if value is None:
    return None
  if fact.unit is None:  # non-numeric
    typed = getattr(fact, "xValue", None)
    if isinstance(typed, str):
      return typed
  return str(value)


def _classify_arcrole(arcrole: str) -> NetworkKind | None:
  """Map an arcrole to a linkbase kind, or ``None`` to skip it.

  Calculations 1.1 (the 2023 ``summation-item`` arcrole) is the calculation
  linkbase as much as the 2003 one is: a filer that adopted it declares the
  same roll-ups under the newer arcrole, and reading those as definition
  arcs left every such filing with no calculation network and no footing.
  """
  if arcrole == XbrlConst.parentChild:
    return "presentation"
  if arcrole in (XbrlConst.summationItem, XbrlConst.summationItem11):
    return "calculation"
  if arcrole in (XbrlConst.conceptLabel, XbrlConst.conceptReference):
    return None
  return "definition"


def _role_definition(mx: ModelXbrl, role_uri: str) -> str | None:
  """Human-readable definition for an extended link role, if declared."""
  role_types = mx.roleTypes.get(role_uri)
  if not role_types:
    return None
  return getattr(role_types[0], "definition", None)


def _role_id(mx: ModelXbrl, role_uri: str) -> str | None:
  """The ``id`` of the role's ``<link:roleType>`` declaration, if any."""
  role_types = mx.roleTypes.get(role_uri)
  if not role_types:
    return None
  role_id = getattr(role_types[0], "id", None)
  return str(role_id) if role_id else None


def _to_date(value: Any) -> date | None:
  """Coerce an Arelle datetime to a validated ``date`` (or ``None``)."""
  try:
    resolved = value.date() if isinstance(value, datetime) else value
  except Exception:
    return None
  if not isinstance(resolved, date):
    return None
  if resolved.year < 1900 or resolved.year > 2100:
    return None
  return resolved


def _normalize_cik(raw: Any) -> str:
  """Zero-pad a numeric CIK to 10 digits; pass non-numeric ids through."""
  text = str(raw)
  if text.isdigit():
    return text.lstrip("0").zfill(10)
  return text


def _period_type(value: Any) -> str | None:
  """Guard an Arelle period type to the model's allowed literals."""
  return value if value in ("instant", "duration", "forever") else None


def _balance(value: Any) -> str | None:
  """Guard an Arelle balance to the model's allowed literals."""
  return value if value in ("debit", "credit") else None


def _text(value: Any) -> str | None:
  """Return a stripped string form of a fact value, or ``None``."""
  if value is None:
    return None
  text = str(value).strip()
  return text or None


def _fiscal_year_end_month(value: Any) -> str | None:
  """Parse the two-digit month from a ``--MM-DD`` fiscal-year-end value."""
  text = str(value) if value is not None else ""
  if text.startswith("--") and len(text) >= 5:
    month = text[2:4]
    if month.isdigit():
      return month
  return None
