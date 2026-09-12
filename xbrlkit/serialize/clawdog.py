"""Project a neutral ``XbrlModel`` into a ClawDog JSON-LD document.

The ClawDog shape is the authored-report lane beside holon and TAVI: a compact
JSON-LD graph of report, entity, periods, units, concepts, facts, networks, and
the calculation equations implied by calculation networks. It keeps producer
fact ids and fact provenance first-class, because those are the anchors a
ledger-authored report needs before it becomes a portable reporting object.

``to_clawdog_report`` returns the document plus a gap report. The document is
allowed to be small and native to ClawDog; the gap report is where fields that
belong to an XBRL filing rather than an authored report are named instead of
being silently forgotten.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from ..model import DimQualifier, Network, Period, Unit, XbrlFact, XbrlModel

CLAWDOG_VERSION = "clawdog-report-jsonld-v1"
CLAWDOG_VOCAB = "https://lodgeit.org/ns/report#"
DOCTYPE_CLAWDOG = "https://lodgeit.org/ns/clawdog/report/v1"
STANDARD_LABEL = "http://www.xbrl.org/2003/role/label"

CONTEXT: dict[str, Any] = {
  "id": "@id",
  "type": "@type",
  "lg": CLAWDOG_VOCAB,
  "xbrl": "https://xbrl.org/2021/",
  "prov": "http://www.w3.org/ns/prov#",
  "label": "lg:label",
  "entity": {"@id": "lg:entity", "@type": "@id"},
  "period": {"@id": "lg:period", "@type": "@id"},
  "unit": {"@id": "lg:unit", "@type": "@id"},
  "concept": {"@id": "lg:concept", "@type": "@id"},
  "axis": {"@id": "lg:axis", "@type": "@id"},
  "member": {"@id": "lg:member", "@type": "@id"},
  "source": {"@id": "prov:hadPrimarySource", "@type": "@id"},
  "attributedTo": {"@id": "prov:wasAttributedTo", "@type": "@id"},
}


@dataclass
class GapReport:
  """Fields the ClawDog document did not carry."""

  missing: list[str] = field(default_factory=list)
  notes: list[str] = field(default_factory=list)

  def to_dict(self) -> dict[str, object]:
    return {
      "spec_version": CLAWDOG_VERSION,
      "missing": sorted(set(self.missing)),
      "notes": list(self.notes),
    }


def to_clawdog(model: XbrlModel, *, report_id: str | None = None) -> str:
  """Project ``model`` into a ClawDog JSON-LD string."""
  document, _ = to_clawdog_report(model, report_id=report_id)
  return json.dumps(document, indent=2, sort_keys=False, default=str)


def to_clawdog_report(
  model: XbrlModel, *, report_id: str | None = None
) -> tuple[dict[str, object], GapReport]:
  """Project ``model`` and return the document plus what did not fit."""
  report_id = report_id or model.filing.report_id or "report"
  gaps = GapReport()
  graph: list[dict[str, object]] = []

  graph.append(_report_node(model, report_id))
  graph.append(_entity_node(model))
  graph.extend(_period_node(period) for period in model.periods)
  graph.extend(_unit_node(unit) for unit in model.units)
  graph.extend(_concept_node(model, qname, gaps) for qname in sorted(model.concepts))
  graph.extend(_fact_node(fact, gaps) for fact in model.facts)
  graph.extend(
    _network_node(network, index, gaps) for index, network in enumerate(model.networks)
  )
  graph.extend(_equation_nodes(model.networks))

  document: dict[str, object] = {
    "@context": CONTEXT,
    "documentInfo": {
      "documentType": DOCTYPE_CLAWDOG,
      "version": CLAWDOG_VERSION,
    },
    "@graph": graph,
  }
  return document, gaps


def _report_node(model: XbrlModel, report_id: str) -> dict[str, object]:
  filing = model.filing
  node: dict[str, object] = {
    "id": _node_id("report", report_id),
    "type": "lg:Report",
    "lg:reportId": filing.report_id,
    "entity": _node_id("entity", model.entity.identifier),
  }
  _put(node, "label", filing.form or filing.report_id)
  _put(node, "lg:reportingStyle", filing.reporting_style)
  _put(node, "lg:form", filing.form)
  _put(node, "lg:filingDate", filing.filing_date)
  _put(node, "lg:reportDate", filing.report_date)
  _put(node, "lg:fiscalYearFocus", filing.fiscal_year_focus)
  _put(node, "lg:fiscalPeriodFocus", filing.fiscal_period_focus)
  _put(node, "lg:fiscalYearEndMonth", filing.fiscal_year_end_month)
  _put(node, "lg:reportURI", filing.report_uri)
  _put(node, "lg:extensionNamespace", filing.extension_namespace)
  if filing.taxonomy_namespaces:
    node["lg:taxonomyNamespaces"] = list(filing.taxonomy_namespaces)
  if filing.items:
    node["lg:items"] = list(filing.items)
  if filing.report_meta:
    node["lg:reportMeta"] = filing.report_meta
  return node


def _entity_node(model: XbrlModel) -> dict[str, object]:
  entity = model.entity
  node: dict[str, object] = {
    "id": _node_id("entity", entity.identifier),
    "type": "lg:Entity",
    "lg:identifier": entity.identifier,
    "lg:scheme": entity.scheme,
  }
  _put(node, "label", entity.name)
  _put(node, "lg:legalName", entity.legal_name)
  _put(node, "lg:ein", entity.ein)
  _put(node, "lg:ticker", entity.ticker)
  _put(node, "lg:exchange", entity.exchange)
  _put(node, "lg:sic", entity.sic)
  _put(node, "lg:sicDescription", entity.sic_description)
  _put(node, "lg:category", entity.category)
  _put(node, "lg:stateOfIncorporation", entity.state_of_incorporation)
  _put(node, "lg:fiscalYearEnd", entity.fiscal_year_end)
  _put(node, "lg:entityType", entity.entity_type)
  _put(node, "lg:website", entity.website)
  _put(node, "lg:phone", entity.phone)
  return node


def _period_node(period: Period) -> dict[str, object]:
  if period.period_type == "duration":
    kind = "lg:DurationPeriod"
  elif period.period_type == "forever":
    kind = "lg:ForeverPeriod"
  else:
    kind = "lg:InstantPeriod"
  node: dict[str, object] = {
    "id": _node_id("period", period.id),
    "type": kind,
    "lg:internalId": period.id,
  }
  _put(node, "lg:startDate", period.start)
  _put(node, "lg:endDate", period.end)
  _put(node, "lg:durationType", period.duration_type)
  _put(node, "lg:calendarYear", period.calendar_year)
  _put(node, "lg:calendarQuarter", period.calendar_quarter)
  _put(node, "lg:calendarPeriodKey", period.calendar_period_key)
  return node


def _unit_node(unit: Unit) -> dict[str, object]:
  node: dict[str, object] = {
    "id": _node_id("unit", unit.id),
    "type": "lg:Unit",
    "lg:internalId": unit.id,
    "lg:measure": unit.measure,
  }
  _put(node, "lg:uri", unit.uri)
  _put(node, "lg:numeratorURI", unit.numerator_uri)
  _put(node, "lg:denominatorURI", unit.denominator_uri)
  return node


def _concept_node(model: XbrlModel, qname: str, gaps: GapReport) -> dict[str, object]:
  concept = model.concepts[qname]
  node: dict[str, object] = {
    "id": _node_id("concept", qname),
    "type": "lg:Concept",
    "lg:qname": concept.qname,
    "lg:namespace": concept.namespace,
    "lg:name": concept.name,
    "lg:isAbstract": concept.is_abstract,
    "lg:isNumeric": concept.is_numeric,
    "lg:isTextBlock": concept.is_textblock,
    "lg:isHypercubeItem": concept.is_hypercube_item,
    "lg:isDimensionItem": concept.is_dimension_item,
    "lg:isDomainMember": concept.is_domain_member,
    "lg:isShares": concept.is_shares,
    "lg:isInteger": concept.is_integer,
    "lg:isFraction": concept.is_fraction,
    "lg:nillable": concept.nillable,
    "lg:isTextFact": concept.is_text_fact,
  }
  _put(node, "label", concept.pref_label)
  _put(node, "lg:periodType", concept.period_type)
  _put(node, "lg:balance", concept.balance)
  _put(node, "lg:substitutionGroup", concept.substitution_group)
  _put(node, "lg:substitutionGroupNamespace", concept.substitution_group_namespace)
  _put(node, "lg:itemType", concept.item_type)
  _put(node, "lg:niceType", concept.nice_type)
  _put(node, "lg:itemTypeQName", concept.item_type_qname)
  _put(node, "lg:itemTypeNamespace", concept.item_type_namespace)
  _put(node, "lg:baseXsdType", concept.base_xsd_type)
  if concept.labels:
    node["lg:labels"] = [
      label.model_dump(exclude_none=True) for label in concept.labels
    ]
  if concept.references:
    gaps.missing.append("concept references")
  if qname not in _mentioned_concepts(model):
    gaps.missing.append("unmentioned concept")
  return node


def _fact_node(fact: XbrlFact, gaps: GapReport) -> dict[str, object]:
  node: dict[str, object] = {
    "id": _node_id("fact", fact.id),
    "type": "lg:Fact",
    "lg:internalId": fact.id,
    "concept": _node_id("concept", fact.concept_qname),
    "period": _node_id("period", fact.period_id),
    "lg:conceptQName": fact.concept_qname,
    "lg:periodId": fact.period_id,
    "lg:valueKind": fact.value_kind,
    "lg:isNil": fact.is_nil,
  }
  if fact.unit_id is not None:
    node["unit"] = _node_id("unit", fact.unit_id)
    node["lg:unitId"] = fact.unit_id
  _put(node, "lg:value", fact.value_str)
  _put(node, "lg:numericValue", fact.numeric_value)
  _put(node, "lg:decimals", fact.decimals)
  _put(node, "lg:language", fact.language)
  _put(node, "lg:contentType", fact.content_type)
  _put(node, "lg:structureId", fact.structure_id)
  _put(node, "lg:entityScheme", fact.entity_scheme)
  _put(node, "lg:entityIdentifier", fact.entity_identifier)
  if fact.dims:
    node["lg:dimensions"] = [_dimension(dim) for dim in fact.dims]
  if fact.provenance is not None:
    node["lg:provenance"] = fact.provenance.model_dump(exclude_none=True)
  if fact.source_hash is not None:
    gaps.missing.append("fact source_hash")
  if fact.raw_value is not None:
    gaps.missing.append("fact raw_value")
  return node


def _dimension(dim: DimQualifier) -> dict[str, object]:
  node: dict[str, object] = {"axis": _node_id("concept", dim.axis_qname)}
  node["lg:axisQName"] = dim.axis_qname
  if dim.member_qname is not None:
    node["member"] = _node_id("concept", dim.member_qname)
    node["lg:memberQName"] = dim.member_qname
  _put(node, "lg:typedValue", dim.typed_value)
  node["lg:isExplicit"] = dim.is_explicit
  _put(node, "lg:axisType", dim.axis_type)
  return node


def _network_node(network: Network, index: int, gaps: GapReport) -> dict[str, object]:
  node: dict[str, object] = {
    "id": _node_id("network", f"{index}-{network.kind}-{network.role_uri}"),
    "type": "lg:Network",
    "lg:roleURI": network.role_uri,
    "lg:kind": network.kind,
  }
  _put(node, "label", network.definition)
  _put(node, "lg:definition", network.definition)
  _put(node, "lg:documentation", network.documentation)
  _put(node, "lg:blockType", network.block_type)
  _put(node, "lg:structureOrder", network.structure_order)
  _put(node, "lg:structureId", network.structure_id)
  _put(node, "lg:factSetId", network.fact_set_id)
  if network.role_id is not None:
    gaps.missing.append("network role_id")
  node["lg:arcs"] = [
    {
      "lg:fromQName": arc.from_qname,
      "lg:toQName": arc.to_qname,
      **_maybe("lg:arcrole", arc.arcrole),
      **_maybe("lg:order", arc.order),
      **_maybe("lg:weight", arc.weight),
      **_maybe("lg:preferredLabel", arc.preferred_label),
      **_maybe("lg:isRoot", arc.is_root if arc.is_root else None),
      **_maybe("lg:targetRole", arc.target_role),
    }
    for arc in network.arcs
  ]
  return node


def _equation_nodes(networks: list[Network]) -> list[dict[str, object]]:
  nodes: list[dict[str, object]] = []
  for index, network in enumerate(n for n in networks if n.kind == "calculation"):
    by_parent: dict[str, list[dict[str, object]]] = {}
    for arc in network.arcs:
      by_parent.setdefault(arc.from_qname, []).append(
        {
          "concept": _node_id("concept", arc.to_qname),
          "lg:conceptQName": arc.to_qname,
          "lg:weight": 1.0 if arc.weight is None else arc.weight,
        }
      )
    for source, terms in by_parent.items():
      nodes.append(
        {
          "id": _node_id("equation", f"{index}-{source}"),
          "type": "lg:AccountingEquation",
          "lg:left": {
            "concept": _node_id("concept", source),
            "lg:conceptQName": source,
          },
          "lg:right": terms,
          "lg:operator": "sum",
          "lg:roleURI": network.role_uri,
        }
      )
  return nodes


def _mentioned_concepts(model: XbrlModel) -> set[str]:
  mentioned = {fact.concept_qname for fact in model.facts}
  for fact in model.facts:
    for dim in fact.dims:
      mentioned.add(dim.axis_qname)
      if dim.member_qname:
        mentioned.add(dim.member_qname)
  for network in model.networks:
    for arc in network.arcs:
      mentioned.add(arc.from_qname)
      mentioned.add(arc.to_qname)
  return mentioned


def _node_id(kind: str, value: str) -> str:
  return f"lg:{kind}:{_slug(value)}"


def _slug(value: str) -> str:
  return re.sub(r"[^A-Za-z0-9_.:-]+", "-", value).strip("-") or "unknown"


def _put(node: dict[str, object], key: str, value: Any) -> None:
  if value is not None and value != []:
    node[key] = value


def _maybe(key: str, value: Any) -> dict[str, object]:
  return {key: value} if value is not None else {}
