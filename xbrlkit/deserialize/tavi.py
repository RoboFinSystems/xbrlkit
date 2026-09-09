"""Read a Project TAVI compiled model back into the neutral ``XbrlModel``.

The inverse of :mod:`xbrlkit.serialize.tavi`, and the reason it exists: a TAVI
document is a *representation of a report*, not a rendering of one, so a
consumer holding one should be able to ask it the same questions it asks a
filing — which in this package means turning it into an ``XbrlModel`` once and
letting every existing tool work unchanged. Nothing here touches Arelle: Arelle
cannot read TAVI, and a caller who has the JSON does not need it to.

**What TAVI carries, and what it does not.** The document is close to lossless
against this model — facts with their values, decimals, language and dimensions;
concepts with their datatype, period type, balance and nillable flag; every
label role; presentation and calculation networks with order, weight and
preferred label; the extended link roles as groups. Four things have no home in
it and are therefore *not* reconstructed here, because inventing them would make
the importer's output disagree with the parse it claims to reproduce:

- the derived period semantics (duration bucket, calendar placement) — the one
  exception, recomputed by :mod:`xbrlkit.periods` from the dates themselves,
  which is where the parse gets them too;
- ``is_hypercube_item`` and the abstractness of axes, domains and members: the
  emitter turns those elements into dimensional objects, and TAVI has no flag
  for either;
- reference linkbase entries, ``Network.role_id``, a fact's source hash and
  raw lexical value, and a dimension's segment/scenario axis;
- a fact's own entity when it differs from the report's — the emitter writes
  the report's entity on every fact.

:func:`gaps` reports what a given document could not supply, so a round trip
can be diffed without re-deriving the list by hand.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from ..model import (
  Arc,
  Concept,
  DimQualifier,
  EntityIdentity,
  FilingMeta,
  Label,
  Network,
  NetworkKind,
  Unit,
  XbrlFact,
  XbrlModel,
)
from ..namespaces import TAVI_REPORT_BASE
from ..parse.ids import unit_id
from ..periods import period_from_interval
from ..serialize._values import CIK_SCHEME
from ..serialize.tavi import (
  CALCULATION_RELATIONSHIP,
  ITEM_TYPE_DATATYPES,
  LABEL_ROLE_TYPES,
  PRESENTATION_RELATIONSHIP,
  ROOT_SOURCE,
)

# The arcroles the two relationship types stand for, so an imported arc reads
# the same as a parsed one.
PARENT_CHILD_ARCROLE = "http://www.xbrl.org/2003/arcrole/parent-child"
SUMMATION_ITEM_ARCROLE = "http://www.xbrl.org/2003/arcrole/summation-item"

# Datatype -> item type. Built by inverting the emitter's map so the two cannot
# drift; where two item types share a datatype the shorter name wins, which is
# the one a filer's concept almost always declares.
DATATYPE_ITEM_TYPES: dict[str, str] = {}
for _item_type, _datatype in ITEM_TYPE_DATATYPES.items():
  if _datatype not in DATATYPE_ITEM_TYPES or len(_item_type) < len(
    DATATYPE_ITEM_TYPES[_datatype]
  ):
    DATATYPE_ITEM_TYPES[_datatype] = _item_type

# Label type -> role URI, likewise inverted from the emitter's map.
LABEL_TYPE_ROLES: dict[str, str] = {
  label_type: role for role, label_type in LABEL_ROLE_TYPES.items()
}

# The item types whose facts are numbers. `Concept.is_numeric` is Arelle's flag
# on the concept, and this is the same line drawn from the declared type.
NUMERIC_ITEM_TYPES: frozenset[str] = frozenset(
  {
    "monetaryItemType", "pureItemType", "sharesItemType", "percentItemType",
    "perShareItemType", "decimalItemType", "integerItemType", "areaItemType",
    "energyItemType", "flowItemType", "forceItemType", "frequencyItemType",
    "lengthItemType", "massItemType", "memoryItemType", "planeAngleItemType",
    "powerItemType", "pressureItemType", "speedItemType", "temperatureItemType",
    "voltageItemType", "volumeItemType", "fractionItemType",
  }
)  # fmt: skip

# XML Schema simple types that make a custom datatype numeric.
NUMERIC_BASE_TYPES: frozenset[str] = frozenset(
  {
    "decimal", "float", "double", "integer", "long", "int", "short", "byte",
    "nonPositiveInteger", "negativeInteger", "nonNegativeInteger",
    "positiveInteger", "unsignedLong", "unsignedInt", "unsignedShort",
    "unsignedByte",
  }
)  # fmt: skip

# Measures the emitter rewrites, and what they were before it did.
MEASURE_SOURCES: dict[str, tuple[str, str]] = {
  "xbrla:shares": ("xbrli:shares", "http://www.xbrl.org/2003/instance#shares"),
  "xbrlr:pure": ("xbrli:pure", "http://www.xbrl.org/2003/instance#pure"),
}
PURE_MEASURE = "xbrli:pure"
PURE_URI = "http://www.xbrl.org/2003/instance#pure"
UTR_NAMESPACE = "http://www.xbrl.org/2009/utr"

_DEI_DOCUMENT_TYPE = "dei:DocumentType"
_DEI_PERIOD_END = "dei:DocumentPeriodEndDate"
_DEI_REGISTRANT_NAME = "dei:EntityRegistrantName"
_DEI_FISCAL_YEAR = "dei:DocumentFiscalYearFocus"
_DEI_FISCAL_PERIOD = "dei:DocumentFiscalPeriodFocus"
_DEI_FISCAL_YEAR_END = "dei:CurrentFiscalYearEndDate"

# The fact dimensions that are the core OIM axes rather than taxonomy ones.
CORE_DIMENSIONS = frozenset(
  {"xbrl:concept", "xbrl:period", "xbrl:entity", "xbrl:unit", "xbrl:language"}
)


class TaviError(ValueError):
  """The document is not a TAVI compiled model this importer can read."""


@dataclass
class ImportGaps:
  """What the document could not supply, per field of the model."""

  missing: list[str] = field(default_factory=list)
  unmapped_datatypes: dict[str, int] = field(default_factory=dict)
  unmapped_label_types: dict[str, int] = field(default_factory=dict)
  typed_dimensions: int = 0

  def to_dict(self) -> dict[str, object]:
    return {
      "missing": sorted(self.missing),
      "unmapped_datatypes": dict(sorted(self.unmapped_datatypes.items())),
      "unmapped_label_types": dict(sorted(self.unmapped_label_types.items())),
      "typed_dimensions": self.typed_dimensions,
    }


def from_tavi_json(text: str) -> XbrlModel:
  """Read a TAVI compiled model from its JSON text."""
  model, _ = from_tavi_report(text)
  return model


def from_tavi(document: Mapping[str, Any]) -> XbrlModel:
  """Read a TAVI compiled model that is already parsed JSON."""
  model, _ = _read(document)
  return model


def from_tavi_report(text: str) -> tuple[XbrlModel, ImportGaps]:
  """Read a TAVI document, returning the model and what it could not supply."""
  try:
    document = json.loads(text)
  except ValueError as exc:
    raise TaviError(f"not JSON: {exc}") from exc
  if not isinstance(document, Mapping):
    raise TaviError("not a JSON object")
  return _read(document)


def _read(document: Mapping[str, Any]) -> tuple[XbrlModel, ImportGaps]:
  info = _mapping(document.get("documentInfo"))
  xbrl_model = _mapping(document.get("xbrlModel"))
  if not xbrl_model:
    raise TaviError("no `xbrlModel` object")
  namespaces = {
    str(prefix): str(uri)
    for prefix, uri in _mapping(info.get("namespaces")).items()
    if isinstance(uri, str)
  }
  gaps = ImportGaps(
    missing=[
      "is_hypercube_item",
      "abstract flag on axes, domains and members",
      "concept references",
      "network role_id",
      "fact source_hash and raw_value",
      "dimension segment/scenario axis",
      "per-fact entity",
    ]
  )

  entity = _entity(xbrl_model, namespaces)
  concepts = _concepts(xbrl_model, namespaces, gaps)
  _apply_labels(xbrl_model, concepts, entity, _entity_sqname(xbrl_model), gaps)
  networks = _networks(xbrl_model)
  facts, periods, units = _facts(xbrl_model, concepts, entity, namespaces, gaps)
  _mark_text_facts(concepts, facts)
  filing = _filing(document, xbrl_model, namespaces, entity, facts, concepts)

  return (
    XbrlModel(
      filing=filing,
      entity=entity,
      concepts=concepts,
      periods=periods,
      units=units,
      facts=facts,
      networks=networks,
    ),
    gaps,
  )


# -- identity -------------------------------------------------------------------


def _entity_sqname(xbrl_model: Mapping[str, Any]) -> str:
  """The entity object's name, as the document wrote it."""
  entities = _sequence(xbrl_model.get("entities"))
  return str(_mapping(entities[0]).get("name", "")) if entities else ""


def _entity(
  xbrl_model: Mapping[str, Any], namespaces: Mapping[str, str]
) -> EntityIdentity:
  """The reporting entity from its SQName — scheme first, identifier second."""
  name = _entity_sqname(xbrl_model)
  prefix, _, identifier = name.partition(":")
  if not identifier:
    prefix, identifier = "", name
  scheme = namespaces.get(prefix) or (CIK_SCHEME if prefix == "cik" else prefix)
  return EntityIdentity(cik=identifier or "", scheme=scheme or CIK_SCHEME)


def _filing(
  document: Mapping[str, Any],
  xbrl_model: Mapping[str, Any],
  namespaces: Mapping[str, str],
  entity: EntityIdentity,
  facts: Sequence[XbrlFact],
  concepts: Mapping[str, Concept],
) -> FilingMeta:
  """Filing identity: the report namespace, the model properties, the cover page.

  TAVI records the report's own dates as model properties and nothing else about
  the filing, so the accession comes from the namespace the emitter minted for
  the report and the fiscal context from the ``dei`` facts the report itself
  carries. Neither is invention: both are read out of the document.
  """
  accession = ""
  report_namespace = namespaces.get("rpt", "")
  if report_namespace.startswith(f"{TAVI_REPORT_BASE}/"):
    accession = report_namespace[len(TAVI_REPORT_BASE) + 1 :]
  filing_date: date | None = None
  for entry in _sequence(xbrl_model.get("properties")):
    prop = _mapping(entry)
    if prop.get("property") == "xbrl:reportFilingDate":
      filing_date = _date(prop.get("value"))
  cover = _cover_facts(facts)
  fiscal_end = cover.get(_DEI_FISCAL_YEAR_END, "")
  if entity.name is None:
    entity.name = cover.get(_DEI_REGISTRANT_NAME)
  return FilingMeta(
    accession=accession or "unknown",
    cik=entity.cik,
    form=cover.get(_DEI_DOCUMENT_TYPE),
    is_inline_xbrl=None,
    filing_date=filing_date,
    report_date=_date(cover.get(_DEI_PERIOD_END)),
    fiscal_year_focus=cover.get(_DEI_FISCAL_YEAR),
    fiscal_period_focus=cover.get(_DEI_FISCAL_PERIOD),
    fiscal_year_end_month=(
      fiscal_end[2:4]
      if fiscal_end.startswith("--") and fiscal_end[2:4].isdigit()
      else None
    ),
    taxonomy_namespaces=sorted(
      {concept.namespace for concept in concepts.values() if concept.namespace}
    ),
  )


def _cover_facts(facts: Sequence[XbrlFact]) -> dict[str, str]:
  """The undimensioned ``dei`` cover-page values, by concept."""
  cover: dict[str, str] = {}
  for fact in facts:
    if fact.dims or not fact.concept_qname.startswith("dei:"):
      continue
    value = (fact.value_str or "").strip()
    if value:
      cover.setdefault(fact.concept_qname, value)
  return cover


# -- concepts and labels ---------------------------------------------------------


def _concepts(
  xbrl_model: Mapping[str, Any], namespaces: Mapping[str, str], gaps: ImportGaps
) -> dict[str, Concept]:
  """Every named object that was an ``<xs:element>`` before TAVI split them.

  TAVI gives concepts, headings, dimensions, domain classes and members their
  own object types; XBRL called all five an element, and so does this model.
  """
  datatypes = {
    str(_mapping(entry).get("name")): _mapping(entry)
    for entry in _sequence(xbrl_model.get("dataTypes"))
  }
  concepts: dict[str, Concept] = {}

  for entry in _sequence(xbrl_model.get("concepts")):
    obj = _mapping(entry)
    qname = str(obj.get("name", ""))
    if not qname:
      continue
    concepts[qname] = _concept(qname, obj, namespaces, datatypes, gaps)

  for entry in _sequence(xbrl_model.get("headings")):
    qname = str(_mapping(entry).get("name", ""))
    if qname and qname not in concepts:
      concepts[qname] = _bare(qname, namespaces, is_abstract=True)

  for entry in _sequence(xbrl_model.get("dimensions")):
    obj = _mapping(entry)
    qname = str(obj.get("name", ""))
    if not qname:
      continue
    concepts[qname] = _bare(qname, namespaces, is_abstract=True, is_dimension_item=True)
    domain = obj.get("domainClass")
    if isinstance(domain, str) and domain and domain not in concepts:
      concepts[domain] = _bare(domain, namespaces, is_domain_member=True)

  for key in ("domainClasses", "members"):
    for entry in _sequence(xbrl_model.get(key)):
      qname = str(_mapping(entry).get("name", ""))
      if qname and qname not in concepts:
        concepts[qname] = _bare(qname, namespaces, is_domain_member=True)

  return concepts


def _concept(
  qname: str,
  obj: Mapping[str, Any],
  namespaces: Mapping[str, str],
  datatypes: Mapping[str, Mapping[str, Any]],
  gaps: ImportGaps,
) -> Concept:
  """One concept object, with the type facts its datatype implies."""
  datatype = obj.get("dataType")
  item_type: str | None = None
  item_type_qname: str | None = None
  item_type_namespace: str | None = None
  base_type: str | None = None
  if isinstance(datatype, str) and datatype:
    item_type = DATATYPE_ITEM_TYPES.get(datatype)
    if item_type is None:
      # A datatype object: the concept's own taxonomy-defined type, kept by the
      # emitter with the XML Schema type it derives from.
      custom = datatypes.get(datatype)
      if custom is not None:
        item_type = datatype.split(":", 1)[-1]
        item_type_qname = datatype
        item_type_namespace = namespaces.get(datatype.split(":", 1)[0])
        base = str(custom.get("baseType", ""))
        base_type = base.split(":", 1)[-1] or None
      else:
        gaps.unmapped_datatypes[datatype] = gaps.unmapped_datatypes.get(datatype, 0) + 1
    else:
      base_type = _base_type_of(datatype)

  balance = None
  for entry in _sequence(obj.get("properties")):
    prop = _mapping(entry)
    if prop.get("property") == "xbrla:balance":
      value = str(prop.get("value", ""))
      balance = value if value in ("debit", "credit") else None

  numeric = item_type in NUMERIC_ITEM_TYPES or (base_type in NUMERIC_BASE_TYPES)
  prefix, _, local = qname.partition(":")
  return Concept(
    qname=qname,
    namespace=namespaces.get(prefix, "") if local else "",
    name=local or qname,
    period_type=_period_type(obj.get("periodType")),
    balance=balance,
    is_numeric=numeric,
    is_textblock=item_type == "textBlockItemType",
    is_shares=item_type == "sharesItemType",
    is_integer=item_type == "integerItemType",
    is_fraction=item_type == "fractionItemType",
    item_type=item_type,
    item_type_qname=item_type_qname,
    item_type_namespace=item_type_namespace,
    base_xsd_type=base_type,
    nillable=bool(obj.get("nillable", False)),
    # A text fact is settled by the emitter's own signal — the language
    # dimension it writes for text facts and nothing else, applied in
    # `_mark_text_facts`. A text block is one whatever its facts carry.
    is_text_fact=item_type == "textBlockItemType",
  )


def _base_type_of(datatype: str) -> str | None:
  """The XML Schema type a built-in TAVI datatype derives from."""
  if datatype.startswith("xs:"):
    return datatype[3:]
  if datatype in ("xbrlr:monetary", "xbrlr:pureType", "xbrla:sharesType"):
    return "decimal"
  if datatype == "xbrlr:textBlock":
    return "string"
  return None


def _bare(
  qname: str,
  namespaces: Mapping[str, str],
  *,
  is_abstract: bool = False,
  is_dimension_item: bool = False,
  is_domain_member: bool = False,
) -> Concept:
  """An element TAVI records by name alone — a heading, an axis, a member."""
  prefix, _, local = qname.partition(":")
  return Concept(
    qname=qname,
    namespace=namespaces.get(prefix, "") if local else "",
    name=local or qname,
    is_abstract=is_abstract,
    is_dimension_item=is_dimension_item,
    is_domain_member=is_domain_member,
  )


def _apply_labels(
  xbrl_model: Mapping[str, Any],
  concepts: dict[str, Concept],
  entity: EntityIdentity,
  entity_sqname: str,
  gaps: ImportGaps,
) -> None:
  """Hang each label object on the element it points at.

  A label on a group is the extended link role's definition and is handled with
  the networks; a label on the entity is the registrant's name, which is the
  only place a compiled model carries it.
  """
  for entry in _sequence(xbrl_model.get("labels")):
    label = _mapping(entry)
    target = str(label.get("forObject", ""))
    value = label.get("value")
    label_type = str(label.get("labelType", ""))
    role = LABEL_TYPE_ROLES.get(label_type)
    if role is None and label_type:
      gaps.unmapped_label_types[label_type] = (
        gaps.unmapped_label_types.get(label_type, 0) + 1
      )
    if target and target == entity_sqname:
      if entity.name is None and isinstance(value, str):
        entity.name = value
      continue
    concept = concepts.get(target)
    if concept is None:
      continue
    language = label.get("language")
    concept.labels.append(
      Label(
        value=str(value) if isinstance(value, str) else None,
        role=role,
        language=str(language) if isinstance(language, str) else None,
      )
    )
    if role == LABEL_TYPE_ROLES.get("xbrl:label") and concept.pref_label is None:
      concept.pref_label = str(value) if isinstance(value, str) else ""
  for concept in concepts.values():
    if concept.pref_label is None and concept.labels:
      concept.pref_label = concept.labels[0].value or ""


# -- networks -------------------------------------------------------------------


def _networks(xbrl_model: Mapping[str, Any]) -> list[Network]:
  """Networks, rejoined to the extended link roles their groups stand for."""
  group_roles: dict[str, str] = {}
  for entry in _sequence(xbrl_model.get("groups")):
    group = _mapping(entry)
    name, uri = group.get("name"), group.get("groupURI")
    if isinstance(name, str) and isinstance(uri, str):
      group_roles[name] = uri

  definitions: dict[str, str] = {}
  documentations: dict[str, str] = {}
  for entry in _sequence(xbrl_model.get("labels")):
    label = _mapping(entry)
    role_uri = group_roles.get(str(label.get("forObject", "")))
    value = label.get("value")
    if role_uri is None or not isinstance(value, str):
      continue
    if label.get("labelType") == "xbrl:documentation":
      documentations.setdefault(role_uri, value)
    else:
      definitions.setdefault(role_uri, value)

  network_roles: dict[str, str] = {}
  for entry in _sequence(xbrl_model.get("groupContents")):
    content = _mapping(entry)
    role_uri = group_roles.get(str(content.get("groupName", "")))
    target = content.get("forObject")
    if role_uri and isinstance(target, str):
      network_roles[target] = role_uri

  networks: list[Network] = []
  for entry in _sequence(xbrl_model.get("networks")):
    obj = _mapping(entry)
    name = str(obj.get("name", ""))
    role_uri = network_roles.get(name)
    if role_uri is None:
      continue
    kind: NetworkKind = (
      "calculation"
      if obj.get("relationshipTypeName") == CALCULATION_RELATIONSHIP
      else "presentation"
    )
    if (
      obj.get("relationshipTypeName")
      not in (PRESENTATION_RELATIONSHIP, CALCULATION_RELATIONSHIP)
      and obj.get("relationshipTypeName") is not None
    ):
      continue
    networks.append(
      Network(
        role_uri=role_uri,
        definition=definitions.get(role_uri),
        documentation=documentations.get(role_uri),
        kind=kind,
        arcs=_arcs(_sequence(obj.get("relationships")), kind),
      )
    )
  return networks


def _arcs(relationships: Sequence[Any], kind: NetworkKind) -> list[Arc]:
  """Relationships as arcs, with the roots the virtual root source declares."""
  roots: set[str] = set()
  entries: list[Mapping[str, Any]] = []
  for entry in relationships:
    relationship = _mapping(entry)
    if relationship.get("source") == ROOT_SOURCE:
      target = relationship.get("target")
      if isinstance(target, str):
        roots.add(target)
      continue
    entries.append(relationship)

  arcrole = PARENT_CHILD_ARCROLE if kind == "presentation" else SUMMATION_ITEM_ARCROLE
  arcs: list[Arc] = []
  for relationship in entries:
    source = relationship.get("source")
    target = relationship.get("target")
    if not isinstance(source, str) or not isinstance(target, str):
      continue
    weight: float | None = None
    preferred_label: str | None = None
    for entry in _sequence(relationship.get("properties")):
      prop = _mapping(entry)
      if prop.get("property") == "xbrl:weight":
        weight = _float(prop.get("value"))
      elif prop.get("property") == "xbrl:preferredLabel":
        preferred_label = LABEL_TYPE_ROLES.get(str(prop.get("value", "")))
    arcs.append(
      Arc(
        from_qname=source,
        to_qname=target,
        arcrole=arcrole,
        order=_float(relationship.get("order")),
        weight=weight,
        preferred_label=preferred_label,
        is_root=source in roots,
      )
    )
  return arcs


# -- facts, periods and units ----------------------------------------------------


def _facts(
  xbrl_model: Mapping[str, Any],
  concepts: Mapping[str, Concept],
  entity: EntityIdentity,
  namespaces: Mapping[str, str],
  gaps: ImportGaps,
) -> tuple[list[XbrlFact], list[Any], list[Unit]]:
  """Facts, and the periods and units they use.

  Periods and units are not objects a fact points at in TAVI — a fact carries
  the literal — so both lists are built from the facts themselves, exactly the
  set the filing used.
  """
  members = {qname for qname, concept in concepts.items() if concept.is_domain_member}
  periods: dict[str, Any] = {}
  units: dict[str, Unit] = {}
  facts: list[XbrlFact] = []

  for entry in _sequence(xbrl_model.get("facts")):
    obj = _mapping(entry)
    dimensions = _mapping(obj.get("factDimensions"))
    concept_qname = str(dimensions.get("xbrl:concept", ""))
    if not concept_qname:
      continue
    concept = concepts.get(concept_qname)

    period_literal = str(dimensions.get("xbrl:period", ""))
    period = periods.get(period_literal)
    if period is None and period_literal:
      period = period_from_interval(period_literal)
      if period is not None:
        periods[period_literal] = period
    if period is None:
      continue

    values = _sequence(obj.get("factValues"))
    value_obj = _mapping(values[0]) if values else {}
    raw = value_obj.get("value")
    value_str = None if raw is None else str(raw)

    measure = dimensions.get("xbrl:unit")
    unit = None
    if isinstance(measure, str) and measure:
      unit = _unit(measure, units, namespaces)
    elif concept is not None and concept.is_numeric:
      # A pure unit is written as no unit at all (section 8.5.2.3); a numeric
      # fact that arrives without one had one before the emitter dropped it.
      unit = _unit(PURE_MEASURE, units, namespaces)

    dims: list[DimQualifier] = []
    for axis, member in dimensions.items():
      if axis in CORE_DIMENSIONS or not isinstance(member, str):
        continue
      explicit = member in members or member in concepts
      if not explicit:
        gaps.typed_dimensions += 1
      dims.append(
        DimQualifier(
          axis_qname=axis,
          member_qname=member if explicit else None,
          typed_value=None if explicit else member,
          is_explicit=explicit,
        )
      )

    numeric_value = _float(value_str) if unit is not None else None
    decimals = value_obj.get("decimals")
    language = dimensions.get("xbrl:language")
    facts.append(
      XbrlFact(
        id=str(obj.get("name", f"f-{len(facts)}")),
        concept_qname=concept_qname,
        period_id=period.id,
        unit_id=unit.id if unit is not None else None,
        entity_cik=entity.cik,
        entity_scheme=entity.scheme,
        entity_identifier=entity.cik,
        dims=dims,
        value_str=value_str,
        numeric_value=numeric_value,
        decimals=None if decimals is None else str(decimals),
        value_kind="numeric" if unit is not None else "text",
        is_nil=not values,
        language=str(language) if isinstance(language, str) else None,
      )
    )

  return facts, list(periods.values()), list(units.values())


def _unit(measure: str, units: dict[str, Unit], namespaces: Mapping[str, str]) -> Unit:
  """The unit a fact's measure names, minted once per measure.

  The emitter rewrites two measures (a share count to the accounting module's
  unit, a pure to none at all) and leaves the rest as the filing wrote them;
  this puts the two back and resolves the remainder against the namespace the
  prefix is bound to, so the unit's id matches the parse's.
  """
  existing = units.get(measure)
  if existing is not None:
    return existing
  token, uri = _measure_source(measure, namespaces)
  if "/" in token:
    numerator, _, denominator = token.partition("/")
    num_uri = _measure_source(numerator, namespaces)[1]
    den_uri = _measure_source(denominator, namespaces)[1]
    unit = Unit(
      id=unit_id(f"{num_uri}/{den_uri}"),
      measure=token,
      uri=f"{num_uri}/{den_uri}",
      numerator_uri=num_uri,
      denominator_uri=den_uri,
    )
  else:
    unit = Unit(id=unit_id(uri), measure=token, uri=uri)
  units[measure] = unit
  return unit


def _measure_source(measure: str, namespaces: Mapping[str, str]) -> tuple[str, str]:
  """A TAVI measure as ``(token, uri)`` in the form the parse produced.

  A filer's own unit (``ba:aircraft``) is a QName like any other, so the
  document's namespace map answers it; only where it does not is the prefix
  itself the best available stem.
  """
  if "/" in measure:
    numerator, _, denominator = measure.partition("/")
    num_token, num_uri = _measure_source(numerator, namespaces)
    den_token, den_uri = _measure_source(denominator, namespaces)
    return f"{num_token}/{den_token}", f"{num_uri}/{den_uri}"
  known = MEASURE_SOURCES.get(measure)
  if known is not None:
    return known
  prefix, _, local = measure.partition(":")
  if not local:
    return measure, measure
  if prefix == "iso4217":
    return measure, f"http://www.xbrl.org/2003/iso4217#{local}"
  if prefix == "utr":
    # `utr` is one of TAVI's *reserved* prefixes and binds to the draft's own
    # namespace, which is not where the unit registry lives; the emitter put a
    # bare registry token under it, so it comes back to the registry.
    return measure, f"{UTR_NAMESPACE}#{local}"
  declared = namespaces.get(prefix)
  if declared:
    return measure, f"{declared.rstrip('#')}#{local}"
  return measure, f"{prefix}#{local}"


def _mark_text_facts(concepts: dict[str, Concept], facts: Sequence[XbrlFact]) -> None:
  """A fact carrying a language settles its concept's text-fact flag.

  The emitter writes ``xbrl:language`` only for OIM text facts, so a fact that
  has one is direct evidence of the flag the datatype can only imply.
  """
  for fact in facts:
    if not fact.language:
      continue
    concept = concepts.get(fact.concept_qname)
    if concept is not None:
      concept.is_text_fact = True


# -- reading JSON defensively ----------------------------------------------------


def _mapping(value: Any) -> Mapping[str, Any]:
  return value if isinstance(value, Mapping) else {}


def _sequence(value: Any) -> Sequence[Any]:
  return value if isinstance(value, Sequence) and not isinstance(value, str) else []


def _float(value: Any) -> float | None:
  try:
    return float(value)  # type: ignore[arg-type]
  except (TypeError, ValueError):
    return None


def _date(value: Any) -> date | None:
  try:
    return date.fromisoformat(str(value)[:10])
  except (TypeError, ValueError):
    return None


def _period_type(value: Any) -> Any:
  return value if value in ("instant", "duration", "forever") else None
