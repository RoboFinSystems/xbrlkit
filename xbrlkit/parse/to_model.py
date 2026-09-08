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
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any

from arelle import XbrlConst

from xbrlkit.model import (
  Arc,
  Concept,
  DimQualifier,
  EntityIdentity,
  FilingMeta,
  Label,
  Network,
  NetworkKind,
  Period,
  Reference,
  Unit,
  XbrlFact,
  XbrlModel,
)
from xbrlkit.parse.ids import fact_id, unit_id
from xbrlkit.periods import duration_period, forever_period, instant_period

if TYPE_CHECKING:
  from arelle.ModelXbrl import ModelXbrl

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

  for f in mx.facts:
    if f.context is None:
      continue
    concept = f.concept
    if concept is None or concept.qname is None:
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
    period = _make_period(f.context)
    if period is None:
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
    scheme, raw_cik = f.context.entityIdentifier
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
    facts.append(
      XbrlFact(
        id=fact_id(report_uri, f.md5sum.value),
        concept_qname=qname_str,
        period_id=period.id,
        unit_id=unit_ref,
        entity_cik=norm_cik,
        entity_scheme=scheme,
        entity_identifier=str(raw_cik),
        dims=_make_dims(f.context, mx, concepts, namespaces),
        value_str=_value_str(f),
        raw_value=str(raw_value) if raw_value is not None else None,
        source_hash=_md5(f),
        numeric_value=numeric_value,
        decimals=(str(f.decimals) if (is_numeric and f.decimals is not None) else None),
        value_kind="numeric" if is_numeric else "text",
        language=getattr(f, "xmlLang", None),
        is_nil=bool(getattr(f, "isNil", False)),
      )
    )

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
  )


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
  """Map an arcrole to a linkbase kind, or ``None`` to skip it."""
  if arcrole == XbrlConst.parentChild:
    return "presentation"
  if arcrole == XbrlConst.summationItem:
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
