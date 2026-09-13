"""Read a ClawDog JSON-LD document into the neutral ``XbrlModel``.

The importer is deliberately structural, like the holon and TAVI importers. A
ClawDog document is an authored report, not an SEC filing package, so Arelle is
not involved. The importer reads the fields the document carries and reports
unknown or unsupported material in an ``ImportGaps`` object.
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
  FactProvenance,
  FilingMeta,
  Label,
  Network,
  Period,
  Unit,
  XbrlFact,
  XbrlModel,
)
from ..namespaces import ENTITY_SCHEME
from ..parse.ids import unit_id
from ..periods import duration_period, forever_period, instant_period
from ..serialize.clawdog import DOCTYPE_CLAWDOG, STANDARD_LABEL


class ClawDogError(ValueError):
  """The document is not a ClawDog report this importer can read."""


@dataclass
class ImportGaps:
  """What the ClawDog document could not supply or this reader did not map."""

  missing: list[str] = field(default_factory=list)
  unknown_node_types: dict[str, int] = field(default_factory=dict)
  unsupported_equations: int = 0

  def to_dict(self) -> dict[str, object]:
    return {
      "missing": sorted(set(self.missing)),
      "unknown_node_types": dict(sorted(self.unknown_node_types.items())),
      "unsupported_equations": self.unsupported_equations,
    }


def from_clawdog_json(text: str) -> XbrlModel:
  """Read a ClawDog report from JSON text."""
  model, _ = from_clawdog_report(text)
  return model


def from_clawdog(document: Mapping[str, Any]) -> XbrlModel:
  """Read a parsed ClawDog report document."""
  model, _ = _read(document)
  return model


def from_clawdog_report(text: str) -> tuple[XbrlModel, ImportGaps]:
  """Read a ClawDog document and return the model plus importer gaps."""
  try:
    document = json.loads(text)
  except ValueError as exc:
    raise ClawDogError(f"not JSON: {exc}") from exc
  if not isinstance(document, Mapping):
    raise ClawDogError("not a JSON object")
  return _read(document)


def _read(document: Mapping[str, Any]) -> tuple[XbrlModel, ImportGaps]:
  if not _is_clawdog(document):
    raise ClawDogError("not a ClawDog report")
  graph = [_mapping(node) for node in _sequence(document.get("@graph"))]
  gaps = ImportGaps()
  _record_unknown_types(graph, gaps)

  report = _first(graph, "Report")
  entity_node = _first(graph, "Entity")
  if report is None:
    raise ClawDogError("no lg:Report node")
  if entity_node is None:
    raise ClawDogError("no lg:Entity node")

  entity = _entity(entity_node)
  periods = _periods(graph)
  units = _units(graph)
  concepts = _concepts(graph)
  facts = _facts(graph, entity, periods, units, gaps)
  networks = _networks(graph)
  _record_equation_gaps(graph, gaps)
  filing = _filing(report, entity, concepts)

  return (
    XbrlModel(
      filing=filing,
      entity=entity,
      concepts=concepts,
      periods=_unique_by_id(periods.values()),
      units=_unique_by_id(units.values()),
      facts=facts,
      networks=networks,
    ),
    gaps,
  )


def _is_clawdog(document: Mapping[str, Any]) -> bool:
  info = _mapping(document.get("documentInfo"))
  if info.get("documentType") == DOCTYPE_CLAWDOG:
    return True
  graph = _sequence(document.get("@graph"))
  return any(_has_type(_mapping(node), "Report") for node in graph) and "lg" in str(
    document.get("@context", "")
  )


def _entity(node: Mapping[str, Any]) -> EntityIdentity:
  identifier = _text(_prop(node, "identifier")) or _text(_prop(node, "cik")) or ""
  scheme = _text(_prop(node, "scheme")) or ENTITY_SCHEME
  return EntityIdentity(
    cik=identifier,
    scheme=scheme,
    name=_text(node.get("label")),
    legal_name=_text(_prop(node, "legalName")),
    ein=_text(_prop(node, "ein")),
    ticker=_text(_prop(node, "ticker")),
    exchange=_text(_prop(node, "exchange")),
    sic=_text(_prop(node, "sic")),
    sic_description=_text(_prop(node, "sicDescription")),
    category=_text(_prop(node, "category")),
    state_of_incorporation=_text(_prop(node, "stateOfIncorporation")),
    fiscal_year_end=_text(_prop(node, "fiscalYearEnd")),
    entity_type=_text(_prop(node, "entityType")),
    website=_text(_prop(node, "website")),
    phone=_text(_prop(node, "phone")),
  )


def _filing(
  report: Mapping[str, Any],
  entity: EntityIdentity,
  concepts: Mapping[str, Concept],
) -> FilingMeta:
  report_id = _text(_prop(report, "reportId")) or _text(report.get("@id")) or "unknown"
  return FilingMeta(
    accession=report_id.rsplit(":", 1)[-1] or report_id,
    cik=entity.cik,
    reporting_style=_text(_prop(report, "reportingStyle")) or "clawdog",
    report_meta=_dict_or_none(_prop(report, "reportMeta")),
    form=_text(_prop(report, "form")) or _text(report.get("label")),
    filing_date=_date(_prop(report, "filingDate")),
    report_date=_date(_prop(report, "reportDate")),
    fiscal_year_focus=_text(_prop(report, "fiscalYearFocus")),
    fiscal_period_focus=_text(_prop(report, "fiscalPeriodFocus")),
    fiscal_year_end_month=_text(_prop(report, "fiscalYearEndMonth")),
    taxonomy_namespaces=sorted(
      {
        str(ns)
        for ns in _list(_prop(report, "taxonomyNamespaces"))
        if isinstance(ns, str)
      }
      or {concept.namespace for concept in concepts.values() if concept.namespace}
    ),
    is_inline_xbrl=None,
    report_uri=_text(_prop(report, "reportURI")),
    extension_namespace=_text(_prop(report, "extensionNamespace")),
    items=[str(item) for item in _list(_prop(report, "items"))],
  )


def _periods(nodes: Sequence[Mapping[str, Any]]) -> dict[str, Period]:
  periods: dict[str, Period] = {}
  for node in nodes:
    if not any(
      _has_type(node, kind)
      for kind in ("InstantPeriod", "DurationPeriod", "ForeverPeriod")
    ):
      continue
    internal = _text(_prop(node, "internalId"))
    start = _date(_prop(node, "startDate"))
    end = _date(_prop(node, "endDate"))
    if _has_type(node, "DurationPeriod") and start and end:
      period = duration_period(start, end)
    elif _has_type(node, "ForeverPeriod"):
      period = forever_period()
    elif end or start:
      period = instant_period(end or start)  # type: ignore[arg-type]
    else:
      continue
    if internal and internal != period.id:
      period.id = internal
    periods[_node_key(node, internal or period.id)] = period
    periods[period.id] = period
  return periods


def _units(nodes: Sequence[Mapping[str, Any]]) -> dict[str, Unit]:
  units: dict[str, Unit] = {}
  for node in nodes:
    if not _has_type(node, "Unit"):
      continue
    internal = _text(_prop(node, "internalId"))
    measure = _text(_prop(node, "measure"))
    uri = _text(_prop(node, "uri")) or _measure_uri(measure)
    if not measure:
      continue
    unit = Unit(
      id=internal or unit_id(uri or measure),
      measure=measure,
      uri=uri,
      numerator_uri=_text(_prop(node, "numeratorURI")),
      denominator_uri=_text(_prop(node, "denominatorURI")),
    )
    units[_node_key(node, unit.id)] = unit
    units[unit.id] = unit
  return units


def _concepts(nodes: Sequence[Mapping[str, Any]]) -> dict[str, Concept]:
  concepts: dict[str, Concept] = {}
  for node in nodes:
    if not _has_type(node, "Concept"):
      continue
    qname = _text(_prop(node, "qname")) or _qname_from_node(node)
    if not qname:
      continue
    labels = [
      Label(
        value=_text(_mapping(label).get("value")),
        role=_text(_mapping(label).get("role")),
        language=_text(_mapping(label).get("language")),
      )
      for label in _list(_prop(node, "labels"))
      if isinstance(label, Mapping)
    ]
    if not labels and _text(node.get("label")):
      labels = [
        Label(value=_text(node.get("label")), role=STANDARD_LABEL, language="en")
      ]
    prefix, _, local = qname.partition(":")
    concepts[qname] = Concept(
      qname=qname,
      namespace=_text(_prop(node, "namespace")) or (prefix if local else ""),
      name=_text(_prop(node, "name")) or local or qname,
      period_type=_literal(
        _prop(node, "periodType"), ("instant", "duration", "forever")
      ),
      balance=_literal(_prop(node, "balance"), ("debit", "credit")),
      is_abstract=_bool(_prop(node, "isAbstract")),
      is_numeric=_bool(_prop(node, "isNumeric")),
      is_textblock=_bool(_prop(node, "isTextBlock")),
      is_hypercube_item=_bool(_prop(node, "isHypercubeItem")),
      is_dimension_item=_bool(_prop(node, "isDimensionItem")),
      is_domain_member=_bool(_prop(node, "isDomainMember")),
      is_shares=_bool(_prop(node, "isShares")),
      is_integer=_bool(_prop(node, "isInteger")),
      is_fraction=_bool(_prop(node, "isFraction")),
      substitution_group=_text(_prop(node, "substitutionGroup")),
      substitution_group_namespace=_text(_prop(node, "substitutionGroupNamespace")),
      item_type=_text(_prop(node, "itemType")),
      nice_type=_text(_prop(node, "niceType")),
      item_type_qname=_text(_prop(node, "itemTypeQName")),
      item_type_namespace=_text(_prop(node, "itemTypeNamespace")),
      base_xsd_type=_text(_prop(node, "baseXsdType")),
      nillable=_bool(_prop(node, "nillable")),
      is_text_fact=_bool(_prop(node, "isTextFact")),
      pref_label=_text(node.get("label")),
      labels=labels,
    )
  return concepts


def _facts(
  nodes: Sequence[Mapping[str, Any]],
  entity: EntityIdentity,
  periods: Mapping[str, Period],
  units: Mapping[str, Unit],
  gaps: ImportGaps,
) -> list[XbrlFact]:
  facts: list[XbrlFact] = []
  for node in nodes:
    if not _has_type(node, "Fact"):
      continue
    period_id = _text(_prop(node, "periodId")) or _reference(node.get("period"))
    period = periods.get(period_id)
    if period is None:
      gaps.missing.append("fact period")
      continue
    unit_id = _text(_prop(node, "unitId")) or _reference(node.get("unit"))
    unit = units.get(unit_id) if unit_id else None
    concept_qname = _text(_prop(node, "conceptQName")) or _qname_reference(
      node.get("concept")
    )
    facts.append(
      XbrlFact(
        id=_text(_prop(node, "internalId"))
        or _text(node.get("@id"))
        or f"f-{len(facts)}",
        concept_qname=concept_qname,
        period_id=period.id,
        unit_id=unit.id if unit is not None else None,
        entity_cik=entity.cik,
        entity_scheme=_text(_prop(node, "entityScheme")) or entity.scheme,
        entity_identifier=_text(_prop(node, "entityIdentifier")) or entity.cik,
        dims=[_dimension(entry) for entry in _list(_prop(node, "dimensions"))],
        value_str=_text(_prop(node, "value")),
        numeric_value=_float(_prop(node, "numericValue")),
        decimals=_text(_prop(node, "decimals")),
        value_kind=_literal(_prop(node, "valueKind"), ("numeric", "text"))
        or ("numeric" if unit else "text"),
        is_nil=_bool(_prop(node, "isNil")),
        language=_text(_prop(node, "language")),
        content_type=_text(_prop(node, "contentType")),
        structure_id=_text(_prop(node, "structureId")),
        provenance=_provenance(node),
      )
    )
  return facts


def _dimension(entry: Any) -> DimQualifier:
  node = _mapping(entry)
  explicit = _bool(_prop(node, "isExplicit"), default=True)
  member = _text(_prop(node, "memberQName")) or _qname_reference(node.get("member"))
  return DimQualifier(
    axis_qname=_text(_prop(node, "axisQName")) or _qname_reference(node.get("axis")),
    member_qname=member or None if explicit else None,
    typed_value=_text(_prop(node, "typedValue")) or (None if explicit else member),
    is_explicit=explicit,
    axis_type=_literal(_prop(node, "axisType"), ("segment", "scenario")),
  )


def _provenance(node: Mapping[str, Any]) -> FactProvenance | None:
  data = _mapping(_prop(node, "provenance"))
  source = (
    _text(data.get("source"))
    or _text(node.get("source"))
    or _text(node.get("sourceAnchor"))
  )
  kind = _text(data.get("kind")) or _text(_prop(node, "sourceKind"))
  content_hash = (
    _text(data.get("content_hash"))
    or _text(data.get("contentHash"))
    or _text(_prop(node, "contentHash"))
  )
  attributed = (
    _text(data.get("attributed_to"))
    or _text(data.get("attributedTo"))
    or _text(node.get("attributedTo"))
  )
  if not any((source, kind, content_hash, attributed)):
    return None
  return FactProvenance(
    source=source,
    kind=kind,
    content_hash=content_hash,
    attributed_to=attributed,
  )


def _networks(nodes: Sequence[Mapping[str, Any]]) -> list[Network]:
  networks: list[Network] = []
  for node in nodes:
    if not _has_type(node, "Network"):
      continue
    kind = _literal(_prop(node, "kind"), ("presentation", "calculation", "definition"))
    role_uri = _text(_prop(node, "roleURI"))
    if kind is None or not role_uri:
      continue
    networks.append(
      Network(
        role_uri=role_uri,
        definition=_text(_prop(node, "definition")) or _text(node.get("label")),
        documentation=_text(_prop(node, "documentation")),
        kind=kind,
        arcs=[
          Arc(
            from_qname=_text(_prop(_mapping(arc), "fromQName")) or "",
            to_qname=_text(_prop(_mapping(arc), "toQName")) or "",
            arcrole=_text(_prop(_mapping(arc), "arcrole")),
            order=_float(_prop(_mapping(arc), "order")),
            weight=_float(_prop(_mapping(arc), "weight")),
            preferred_label=_text(_prop(_mapping(arc), "preferredLabel")),
            is_root=_bool(_prop(_mapping(arc), "isRoot")),
            target_role=_text(_prop(_mapping(arc), "targetRole")),
          )
          for arc in _list(_prop(node, "arcs"))
          if isinstance(arc, Mapping)
        ],
        block_type=_text(_prop(node, "blockType")),
        structure_order=_int(_prop(node, "structureOrder")),
        structure_id=_text(_prop(node, "structureId")),
        fact_set_id=_text(_prop(node, "factSetId")),
      )
    )
  return networks


def _record_equation_gaps(nodes: Sequence[Mapping[str, Any]], gaps: ImportGaps) -> None:
  for node in nodes:
    if not _has_type(node, "AccountingEquation"):
      continue
    if _text(_prop(node, "operator")) not in ("sum", None):
      gaps.unsupported_equations += 1


def _record_unknown_types(nodes: Sequence[Mapping[str, Any]], gaps: ImportGaps) -> None:
  known = {
    "Report",
    "Entity",
    "InstantPeriod",
    "DurationPeriod",
    "ForeverPeriod",
    "Unit",
    "Concept",
    "Fact",
    "Network",
    "AccountingEquation",
  }
  for node in nodes:
    for item in _types(node):
      local = item.rsplit(":", 1)[-1]
      if local not in known:
        gaps.unknown_node_types[local] = gaps.unknown_node_types.get(local, 0) + 1


def _first(
  nodes: Sequence[Mapping[str, Any]], type_name: str
) -> Mapping[str, Any] | None:
  return next((node for node in nodes if _has_type(node, type_name)), None)


def _has_type(node: Mapping[str, Any], type_name: str) -> bool:
  return any(
    item == type_name or item.endswith(f":{type_name}") for item in _types(node)
  )


def _types(node: Mapping[str, Any]) -> list[str]:
  return [str(item) for item in _list(node.get("@type", node.get("type")))]


def _mapping(value: Any) -> Mapping[str, Any]:
  return value if isinstance(value, Mapping) else {}


def _sequence(value: Any) -> Sequence[Any]:
  return value if isinstance(value, Sequence) and not isinstance(value, str) else []


def _list(value: Any) -> list[Any]:
  if value is None:
    return []
  return (
    list(value)
    if isinstance(value, Sequence) and not isinstance(value, str)
    else [value]
  )


def _prop(node: Mapping[str, Any], local: str) -> Any:
  return node.get(f"lg:{local}", node.get(local))


def _node_key(node: Mapping[str, Any], fallback: str) -> str:
  return _text(node.get("@id", node.get("id"))) or fallback


def _reference(value: Any) -> str:
  if isinstance(value, Mapping):
    return _text(value.get("@id", value.get("id"))) or ""
  return _text(value) or ""


def _text(value: Any) -> str | None:
  if value is None:
    return None
  if isinstance(value, (str, int, float, bool)):
    return str(value)
  return None


def _date(value: Any) -> date | None:
  text = _text(value)
  if not text:
    return None
  try:
    return date.fromisoformat(text[:10])
  except ValueError:
    return None


def _bool(value: Any, *, default: bool = False) -> bool:
  if value is None:
    return default
  if isinstance(value, bool):
    return value
  if isinstance(value, str):
    return value.lower() in ("1", "true", "yes")
  return bool(value)


def _float(value: Any) -> float | None:
  if value is None:
    return None
  try:
    return float(value)
  except (TypeError, ValueError):
    return None


def _int(value: Any) -> int | None:
  if value is None:
    return None
  try:
    return int(value)
  except (TypeError, ValueError):
    return None


def _literal(value: Any, allowed: tuple[str, ...]) -> Any:
  text = _text(value)
  return text if text in allowed else None


def _dict_or_none(value: Any) -> dict[str, Any] | None:
  return dict(value) if isinstance(value, Mapping) else None


def _qname_from_node(node: Mapping[str, Any]) -> str:
  node_id = _text(node.get("@id", node.get("id"))) or ""
  return node_id.rsplit("concept:", 1)[-1] if "concept:" in node_id else node_id


def _qname_reference(value: Any) -> str:
  reference = _reference(value)
  return reference.rsplit("concept:", 1)[-1] if "concept:" in reference else reference


def _measure_uri(measure: str | None) -> str | None:
  if not measure:
    return None
  prefix, _, local = measure.partition(":")
  if prefix == "iso4217" and local:
    return f"http://www.xbrl.org/2003/iso4217#{local}"
  return measure


def _unique_by_id(items: Sequence[Any]) -> list[Any]:
  out: dict[str, Any] = {}
  for item in items:
    out.setdefault(str(item.id), item)
  return list(out.values())
