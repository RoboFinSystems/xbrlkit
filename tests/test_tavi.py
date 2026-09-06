"""Tests for the Project Tavi projection (``serialize/tavi.py``).

Covers the compiled-model envelope, the core dimensions on a fact, the
abstract-concept split into heading objects, calculation link properties, and
the gap report — the record of what a real filing carries that Tavi has nowhere
to put, which is the substantive output of the projection.

Several expectations here were set by diffing this projection against the
compiled model Arelle's XbrlModel plugin writes for the same filing: the
period literal, the entity SQName, fact language, root sources, nillable, the
pure and shares datatypes, and hypercube headings.
"""

from __future__ import annotations

import json
from datetime import date
from typing import Any

from xbrlkit.model import (
  Arc,
  Concept,
  DimQualifier,
  EntityIdentity,
  FilingMeta,
  Label,
  Network,
  Period,
  Unit,
  XbrlFact,
  XbrlModel,
)
from xbrlkit.serialize import to_tavi, to_tavi_report
from xbrlkit.serialize.tavi import (
  DOCTYPE_COMPILED,
  ITEM_TYPE_DATATYPES,
  RESERVED_NAMESPACES,
  ROOT_SOURCE,
)

US_GAAP = "http://fasb.org/us-gaap/2024-01-31"
DEI = "http://xbrl.sec.gov/dei/2024"
STANDARD = "http://www.xbrl.org/2003/role/label"


def _model() -> XbrlModel:
  """A minimal filing: a header, a hypercube, three concepts, two dei facts."""
  concepts = {
    "us-gaap:AssetsAbstract": Concept(
      qname="us-gaap:AssetsAbstract",
      namespace=US_GAAP,
      name="AssetsAbstract",
      is_abstract=True,
      labels=[Label(value="Assets [Abstract]", role=STANDARD)],
    ),
    "us-gaap:SegmentTable": Concept(
      qname="us-gaap:SegmentTable",
      namespace=US_GAAP,
      name="SegmentTable",
      is_abstract=True,
      is_hypercube_item=True,
    ),
    "us-gaap:Assets": Concept(
      qname="us-gaap:Assets",
      namespace=US_GAAP,
      name="Assets",
      period_type="instant",
      balance="debit",
      is_numeric=True,
      item_type="monetaryItemType",
      nillable=True,
      labels=[Label(value="Assets", role=STANDARD)],
    ),
    "us-gaap:Cash": Concept(
      qname="us-gaap:Cash",
      namespace=US_GAAP,
      name="Cash",
      period_type="instant",
      balance="debit",
      is_numeric=True,
      item_type="monetaryItemType",
      labels=[Label(value="Cash", role="http://www.xbrl.org/2003/role/terseLabel")],
    ),
    "us-gaap:SharesOutstanding": Concept(
      qname="us-gaap:SharesOutstanding",
      namespace=US_GAAP,
      name="SharesOutstanding",
      period_type="instant",
      is_numeric=True,
      item_type="sharesItemType",
    ),
    "dei:EntityRegistrantName": Concept(
      qname="dei:EntityRegistrantName",
      namespace=DEI,
      name="EntityRegistrantName",
      period_type="duration",
      item_type="normalizedStringItemType",
      is_text_fact=True,
    ),
    "dei:EntityShellCompany": Concept(
      qname="dei:EntityShellCompany",
      namespace=DEI,
      name="EntityShellCompany",
      period_type="duration",
      item_type="yesNoItemType",
      item_type_qname="dei:yesNoItemType",
      item_type_namespace=DEI,
      base_xsd_type="string",
    ),
  }
  periods = [
    Period(
      id="p-instant",
      period_type="instant",
      end=date(2024, 12, 31),
      calendar_year=2024,
      calendar_quarter="FY",
      calendar_period_key="2024",
    ),
    Period(
      id="p-duration",
      period_type="duration",
      start=date(2024, 1, 1),
      end=date(2024, 12, 31),
      duration_type="annual",
      calendar_year=2024,
      calendar_quarter="FY",
      calendar_period_key="2024",
    ),
  ]
  facts = [
    XbrlFact(
      id="f1",
      concept_qname="us-gaap:Assets",
      period_id="p-instant",
      unit_id="u-usd",
      entity_cik="0001234567",
      value_str="1000",
      numeric_value=1000.0,
      decimals="-3",
    ),
    XbrlFact(
      id="f2",
      concept_qname="us-gaap:Cash",
      period_id="p-instant",
      unit_id="u-usd",
      entity_cik="0001234567",
      numeric_value=400.0,
      decimals="-3",
      dims=[
        DimQualifier(
          axis_qname="us-gaap:SegmentAxis", member_qname="us-gaap:NorthAmerica"
        )
      ],
    ),
    XbrlFact(
      id="f3",
      concept_qname="us-gaap:SharesOutstanding",
      period_id="p-duration",
      unit_id="u-shares",
      entity_cik="0001234567",
      numeric_value=50.0,
      decimals="INF",
    ),
    XbrlFact(
      id="f4",
      concept_qname="dei:EntityRegistrantName",
      period_id="p-duration",
      entity_cik="0001234567",
      value_str="Acme Corp",
      value_kind="text",
      language="en-US",
    ),
    XbrlFact(
      id="f5",
      concept_qname="dei:EntityShellCompany",
      period_id="p-duration",
      entity_cik="0001234567",
      value_str="false",
      value_kind="text",
      language="en-US",
    ),
    XbrlFact(
      id="f6",
      concept_qname="us-gaap:Assets",
      period_id="p-duration",
      unit_id="u-usd",
      entity_cik="0001234567",
      is_nil=True,
    ),
  ]
  dim = "http://xbrl.org/int/dim/arcrole"
  networks = [
    Network(
      role_uri="http://example.com/role/Segments",
      definition="Segments",
      kind="definition",
      arcs=[
        Arc(
          from_qname="us-gaap:CashAbstract",
          to_qname="us-gaap:SegmentTable",
          arcrole=f"{dim}/all",
          is_root=True,
        ),
        Arc(
          from_qname="us-gaap:SegmentTable",
          to_qname="us-gaap:SegmentAxis",
          arcrole=f"{dim}/hypercube-dimension",
        ),
        Arc(
          from_qname="us-gaap:SegmentAxis",
          to_qname="us-gaap:SegmentDomain",
          arcrole=f"{dim}/dimension-domain",
        ),
        Arc(
          from_qname="us-gaap:SegmentDomain",
          to_qname="us-gaap:NorthAmerica",
          arcrole=f"{dim}/domain-member",
        ),
      ],
    ),
    Network(
      role_uri="http://example.com/role/BalanceSheet",
      definition="Balance Sheet",
      kind="presentation",
      arcs=[
        Arc(
          from_qname="us-gaap:AssetsAbstract",
          to_qname="us-gaap:Assets",
          arcrole="http://www.xbrl.org/2003/arcrole/parent-child",
          order=1.0,
          is_root=True,
        ),
        Arc(
          from_qname="us-gaap:AssetsAbstract",
          to_qname="us-gaap:SegmentTable",
          arcrole="http://www.xbrl.org/2003/arcrole/parent-child",
          order=2.0,
          is_root=True,
        ),
      ],
    ),
    Network(
      role_uri="http://example.com/role/BalanceSheet",
      definition="Balance Sheet",
      kind="calculation",
      arcs=[
        Arc(
          from_qname="us-gaap:Assets",
          to_qname="us-gaap:Cash",
          arcrole="http://www.xbrl.org/2003/arcrole/summation-item",
          order=1.0,
          weight=1.0,
          is_root=True,
        )
      ],
    ),
  ]
  return XbrlModel(
    filing=FilingMeta(
      accession="0000000000-24-000001",
      cik="0001234567",
      form="10-K",
      filing_date=date(2025, 2, 14),
      taxonomy_namespaces=[US_GAAP],
    ),
    entity=EntityIdentity(cik="0001234567", name="Acme Corp"),
    concepts=concepts,
    periods=periods,
    units=[
      Unit(id="u-usd", measure="iso4217:USD"),
      Unit(id="u-shares", measure="xbrli:shares"),
    ],
    facts=facts,
    networks=networks,
  )


def _document() -> dict[str, Any]:
  document, _ = to_tavi_report(_model())
  return document


def _fact(document: dict[str, Any], concept: str) -> dict[str, Any]:
  return next(
    f
    for f in document["xbrlModel"]["facts"]
    if f["factDimensions"]["xbrl:concept"] == concept
  )


def test_compiled_model_envelope() -> None:
  """documentInfo declares a compiled model and binds the reserved prefixes."""
  info = _document()["documentInfo"]
  assert info["documentType"] == DOCTYPE_COMPILED
  namespaces = info["namespaces"]
  for prefix, uri in RESERVED_NAMESPACES.items():
    assert namespaces[prefix] == uri
  # A compiled model must not carry a documentNamespacePrefix (section 4.2.1).
  assert "documentNamespacePrefix" not in info
  assert US_GAAP in namespaces.values()
  # The entity scheme is bound, because the entity SQName uses it.
  assert namespaces["cik"] == "http://www.sec.gov/CIK"


def test_model_is_a_report() -> None:
  model = _document()["xbrlModel"]
  assert model["modelType"] == "xbrl:report"
  assert {"property": "xbrl:reportFilingDate", "value": "2025-02-14"} in model[
    "properties"
  ]


def test_abstract_concepts_become_headings() -> None:
  """Section 5.3: no reportable value, still on the concept dimension."""
  model = _document()["xbrlModel"]
  assert {"name": "us-gaap:AssetsAbstract"} in model["headings"]
  assert all(c["name"] != "us-gaap:AssetsAbstract" for c in model["concepts"])


def test_hypercube_is_a_heading_so_presentation_arcs_resolve() -> None:
  """The presentation tree hangs axes and line items under the Table element.

  The hypercube's dimensional meaning became a cube under another name; the
  element itself must still exist, or every arc touching it dangles. Every
  relationship endpoint in the model resolves to an object.
  """
  model = _document()["xbrlModel"]
  assert {"name": "us-gaap:SegmentTable"} in model["headings"]
  defined = {ROOT_SOURCE}
  for key in ("concepts", "headings", "members", "dimensions", "domainClasses"):
    defined |= {obj["name"] for obj in model.get(key, [])}
  for network in model["networks"]:
    for relationship in network["relationships"]:
      assert relationship["source"] in defined
      assert relationship["target"] in defined


def test_concept_carries_datatype_period_type_balance_and_nillable() -> None:
  model = _document()["xbrlModel"]
  assets = next(c for c in model["concepts"] if c["name"] == "us-gaap:Assets")
  assert assets["dataType"] == "xbrlr:monetary"
  assert assets["periodType"] == "instant"
  assert assets["nillable"] is True
  assert {"property": "xbrla:balance", "value": "debit"} in assets["properties"]
  # nillable defaults to false (section 5.2), so it is written only when true.
  cash = next(c for c in model["concepts"] if c["name"] == "us-gaap:Cash")
  assert "nillable" not in cash


def test_fact_carries_the_four_core_dimensions() -> None:
  """The near-identity mapping: factDimensions is a flat map (section 8.5).

  The period is an xs:dateTime interval with an exclusive end, the entity is
  scheme + identifier, and the value is the reported lexical string — the same
  three literals the OIM projection writes for this fact.
  """
  assets = _document()["xbrlModel"]["facts"][0]
  assert assets["factDimensions"] == {
    "xbrl:concept": "us-gaap:Assets",
    "xbrl:period": "2025-01-01T00:00:00",
    "xbrl:entity": "cik:0001234567",
    "xbrl:unit": "iso4217:USD",
  }
  assert assets["factValues"] == [{"value": "1000", "decimals": -3}]


def test_numeric_value_without_a_lexical_form_is_still_a_string() -> None:
  cash = _fact(_document(), "us-gaap:Cash")
  assert cash["factValues"][0]["value"] == "400"


def test_taxonomy_dimension_is_a_peer_of_the_core_dimensions() -> None:
  cash = _fact(_document(), "us-gaap:Cash")
  assert cash["factDimensions"]["us-gaap:SegmentAxis"] == "us-gaap:NorthAmerica"


def test_duration_period_is_an_iso_interval_of_datetimes() -> None:
  shares = _fact(_document(), "us-gaap:SharesOutstanding")
  assert (
    shares["factDimensions"]["xbrl:period"] == "2024-01-01T00:00:00/2025-01-01T00:00:00"
  )
  # decimals INF means infinitely precise, which Tavi expresses by omission.
  assert "decimals" not in shares["factValues"][0]


def test_text_fact_carries_its_language_in_lower_case() -> None:
  """Section 8.3: the language dimension applies to text facts, and only them."""
  document = _document()
  name = _fact(document, "dei:EntityRegistrantName")
  assert name["factDimensions"]["xbrl:language"] == "en-us"
  assert name["factValues"] == [{"value": "Acme Corp", "language": "en-us"}]
  # yesNoItemType is not a text fact, so the tag the filing carried is not one.
  shell = _fact(document, "dei:EntityShellCompany")
  assert "xbrl:language" not in shell["factDimensions"]
  assert "language" not in shell["factValues"][0]


def test_nil_fact_has_no_fact_value() -> None:
  nil = next(f for f in _document()["xbrlModel"]["facts"] if f["name"] == "rpt:f-5")
  assert nil["factDimensions"]["xbrl:concept"] == "us-gaap:Assets"
  assert "factValues" not in nil


def test_entity_sqname_keeps_the_scheme() -> None:
  """Section 8.1: the SQName includes the scheme and the identifier."""
  assert _document()["xbrlModel"]["entities"] == [{"name": "cik:0001234567"}]


def _ledger_model() -> XbrlModel:
  """The same model re-homed on an entity that is not an EDGAR filer."""
  return _model().model_copy(
    update={
      "entity": EntityIdentity(
        cik="ent_01K3ZQ", scheme="http://robosystems.ai/entity", name="Acme LLC"
      )
    }
  )


def test_non_sec_entity_binds_its_own_scheme_under_entity() -> None:
  """Section 8.1 again: the prefix follows the scheme, not the SEC.

  A model built from a ledger's own report has no CIK. Its entity is still
  scheme + identifier — written ``entity:<id>`` under the scheme it declares,
  with no ``cik`` binding at all.
  """
  document, _ = to_tavi_report(_ledger_model())
  namespaces = document["documentInfo"]["namespaces"]
  assert namespaces["entity"] == "http://robosystems.ai/entity"
  assert "cik" not in namespaces
  assert document["xbrlModel"]["entities"] == [{"name": "entity:ent_01K3ZQ"}]
  assert all(
    fact["factDimensions"]["xbrl:entity"] == "entity:ent_01K3ZQ"
    for fact in document["xbrlModel"]["facts"]
  )


def test_entity_name_is_a_label_on_the_entity() -> None:
  """The entity's name is a label object pointing at the entity (5.14).

  A reader holding the compiled model and nothing else — no ``dei`` facts, no
  EDGAR header — still needs a name to put on the report.
  """
  labels = [
    entry
    for entry in _document()["xbrlModel"]["labels"]
    if entry["forObject"] == "cik:0001234567"
  ]
  assert labels == [
    {
      "forObject": "cik:0001234567",
      "labelType": "xbrl:label",
      "value": "Acme Corp",
      "language": "en-US",
    }
  ]


def test_entity_without_a_name_has_no_label() -> None:
  model = _model().model_copy(update={"entity": EntityIdentity(cik="0001234567")})
  document, _ = to_tavi_report(model)
  assert not [
    entry
    for entry in document["xbrlModel"]["labels"]
    if entry["forObject"] == "cik:0001234567"
  ]


def test_description_reads_the_filing_unless_one_is_supplied() -> None:
  assert (
    _document()["documentInfo"]["description"]
    == "10-K 0000000000-24-000001 (CIK 0001234567) projected from XBRL by xbrlkit"
  )
  document, _ = to_tavi_report(
    _ledger_model(), description="RoboLedger report r1 g2 (Acme LLC)"
  )
  assert document["documentInfo"]["description"] == (
    "RoboLedger report r1 g2 (Acme LLC)"
  )
  assert (
    json.loads(to_tavi(_ledger_model(), description="custom"))["documentInfo"][
      "description"
    ]
    == "custom"
  )


def test_calculation_relationship_carries_weight_only() -> None:
  """Section 14.3.1: weight is required; reconciliation is a flag, not asserted."""
  networks = _document()["xbrlModel"]["networks"]
  calc = next(n for n in networks if n["relationshipTypeName"] == "xbrl:summation-item")
  body = [r for r in calc["relationships"] if r["source"] != ROOT_SOURCE]
  assert body[0]["properties"] == [{"property": "xbrl:weight", "value": 1.0}]


def test_network_roots_are_anchored_to_the_root_source() -> None:
  """Section 10.4: a root is the target of a relationship from xbrl:rootSource."""
  networks = _document()["xbrlModel"]["networks"]
  pres = next(n for n in networks if n["relationshipTypeName"] == "xbrl:parent-child")
  assert pres["relationships"][0] == {
    "source": ROOT_SOURCE,
    "target": "us-gaap:AssetsAbstract",
    "order": 1.0,
  }
  assert "roots" not in pres


def test_extended_link_roles_become_groups_that_carry_their_cubes() -> None:
  """A role is a group (section 10.1); it carries its networks and its cube."""
  model = _document()["xbrlModel"]
  groups = {g["groupURI"]: g["name"] for g in model["groups"]}
  assert set(groups) == {
    "http://example.com/role/BalanceSheet",
    "http://example.com/role/Segments",
  }
  by_group: dict[str, set[str]] = {}
  for content in model["groupContents"]:
    by_group.setdefault(content["groupName"], set()).add(content["forObject"])
  assert len(by_group[groups["http://example.com/role/BalanceSheet"]]) == 2
  assert by_group[groups["http://example.com/role/Segments"]] == {"rpt:cube-0"}
  # A group carries only name and groupURI; its readable name is a label.
  assert all(set(g) == {"name", "groupURI"} for g in model["groups"])


def test_group_definition_is_a_label_with_the_filing_language() -> None:
  """The ELR definition is a labelObject, as the specification's examples show.

  It was previously written as an `xbrl:groupDescription` property, which no
  property type in the model defines. A label requires a language, and the
  definition has none, so it takes the one the filing's text facts carry.
  """
  model = _document()["xbrlModel"]
  group_name = model["groups"][0]["name"]
  label = next(entry for entry in model["labels"] if entry["forObject"] == group_name)
  assert label == {
    "forObject": group_name,
    "labelType": "xbrl:label",
    "value": "Balance Sheet",
    "language": "en-US",
  }
  assert all("language" in entry for entry in model["labels"])


def test_shares_map_to_the_accounting_module() -> None:
  """No core datatype types a share count; the xbrla module does."""
  document, gaps = to_tavi_report(_model())
  assert gaps.item_types_without_builtin == {}
  shares = next(
    c
    for c in document["xbrlModel"]["concepts"]
    if c["name"].endswith("SharesOutstanding")
  )
  assert shares["dataType"] == "xbrla:sharesType"
  assert _fact(document, "us-gaap:SharesOutstanding")["factDimensions"][
    "xbrl:unit"
  ] == ("xbrla:shares")
  assert {"name": "xbrla:shares", "dataType": "xbrla:sharesType"} in document[
    "xbrlModel"
  ]["units"]


def test_pure_is_a_datatype_and_a_currency_unit_is_monetary() -> None:
  """xbrlr:pure is the unit; the datatype it measures is xbrlr:pureType."""
  assert ITEM_TYPE_DATATYPES["pureItemType"] == "xbrlr:pureType"
  units = _document()["xbrlModel"]["units"]
  assert {"name": "iso4217:USD", "dataType": "xbrlr:monetary"} in units


def test_taxonomy_defined_item_type_becomes_a_datatype_object() -> None:
  """Section 11.1: an item type with no built-in keeps its own name and base."""
  document, gaps = to_tavi_report(_model())
  model = document["xbrlModel"]
  assert {"name": "dei:yesNoItemType", "baseType": "xs:string"} in model["dataTypes"]
  shell = next(c for c in model["concepts"] if c["name"] == "dei:EntityShellCompany")
  assert shell["dataType"] == "dei:yesNoItemType"
  assert document["documentInfo"]["namespaces"]["dei"] == DEI
  assert gaps.custom_datatypes == {"dei:yesNoItemType": 1}
  assert gaps.item_types_unmapped_here == {}


def test_gap_report_records_dropped_period_semantics() -> None:
  """xbrl:period is a bare interval — the calendar placement has no home."""
  _, gaps = to_tavi_report(_model())
  dropped = gaps.dropped_period_semantics
  assert len(dropped) == 2
  annual = next(d for d in dropped if d["duration_type"] == "annual")
  assert annual["calendar_period_key"] == "2024"
  assert annual["period"] == "2024-01-01T00:00:00/2025-01-01T00:00:00"


def test_label_roles_map_to_the_core_model_label_types() -> None:
  """The standard role is xbrl:label, and the negated family exists in Tavi."""
  labels = _document()["xbrlModel"]["labels"]
  assets = next(entry for entry in labels if entry["forObject"] == "us-gaap:Assets")
  assert assets["labelType"] == "xbrl:label"
  cash = next(entry for entry in labels if entry["forObject"] == "us-gaap:Cash")
  assert cash["labelType"] == "xbrl:terseLabel"
  _, gaps = to_tavi_report(_model())
  assert gaps.unmapped_label_roles == {}


def test_gap_report_carries_the_spec_ambiguities() -> None:
  _, gaps = to_tavi_report(_model())
  ids = {a["id"] for a in gaps.to_dict()["against_the_model"]["spec_ambiguities"]}
  assert {
    "xs-namespace-scheme",
    "shares-datatype-in-unpublished-module",
    "duplicate-label-uris",
    "reconciliation-required-but-unused",
    "period-literal-form",
    "language-case",
  } <= ids


def test_serialization_is_deterministic_and_valid_json() -> None:
  first = to_tavi(_model())
  second = to_tavi(_model())
  assert first == second
  assert json.loads(first)["documentInfo"]["documentType"] == DOCTYPE_COMPILED


def test_hypercube_becomes_a_cube_with_its_axis() -> None:
  """The `all` and `hypercube-dimension` arcs rebuild into a cubeObject."""
  model = _document()["xbrlModel"]
  cube = next(c for c in model["cubes"] if c["name"] == "rpt:cube-0")
  by_dimension = {d["dimension"]: d for d in cube["cubeDimensions"]}
  # Section 5.10.2: the concept dimension must be present, and is left open.
  assert by_dimension["xbrl:concept"] == {"dimension": "xbrl:concept"}
  # Core dimensions are optional so facts that omit one still fall inside —
  # including language, which only text facts carry.
  assert by_dimension["xbrl:period"]["optional"] is True
  assert by_dimension["xbrl:language"]["optional"] is True
  assert by_dimension["us-gaap:SegmentAxis"]["domainNetwork"] == "rpt:domain-0"


def test_axis_domain_and_member_leave_the_concept_list() -> None:
  """In Tavi they are dimension, domain class and member objects, not concepts."""
  model = _document()["xbrlModel"]
  names = {c["name"] for c in model["concepts"]} | {
    h["name"] for h in model["headings"]
  }
  assert "us-gaap:SegmentAxis" not in names
  assert "us-gaap:SegmentDomain" not in names
  assert "us-gaap:NorthAmerica" not in names
  assert {"name": "us-gaap:SegmentAxis", "domainClass": "us-gaap:SegmentDomain"} in (
    model["dimensions"]
  )
  assert {"name": "us-gaap:SegmentDomain"} in model["domainClasses"]
  assert {
    "name": "us-gaap:NorthAmerica",
    "domainClasses": ["us-gaap:SegmentDomain"],
  } in model["members"]


def test_one_hypercube_in_two_roles_is_two_cubes() -> None:
  """A filing reuses one Table element across sections with different axes.

  Keyed on the element alone, every section's axes and members were unioned
  into one cube. Cubes are per (role, hypercube); domain networks per axis.
  """
  model = _model()
  dim = "http://xbrl.org/int/dim/arcrole"
  model.networks.append(
    Network(
      role_uri="http://example.com/role/Regions",
      definition="Regions",
      kind="definition",
      arcs=[
        Arc(
          from_qname="us-gaap:CashAbstract",
          to_qname="us-gaap:SegmentTable",
          arcrole=f"{dim}/all",
        ),
        Arc(
          from_qname="us-gaap:SegmentTable",
          to_qname="us-gaap:RegionAxis",
          arcrole=f"{dim}/hypercube-dimension",
        ),
        Arc(
          from_qname="us-gaap:RegionAxis",
          to_qname="us-gaap:RegionDomain",
          arcrole=f"{dim}/dimension-domain",
        ),
        Arc(
          from_qname="us-gaap:RegionDomain",
          to_qname="us-gaap:Europe",
          arcrole=f"{dim}/domain-member",
        ),
      ],
    )
  )
  document, _ = to_tavi_report(model)
  cubes = {
    c["name"]: {d["dimension"] for d in c["cubeDimensions"]}
    for c in document["xbrlModel"]["cubes"]
  }
  assert "us-gaap:SegmentAxis" in cubes["rpt:cube-0"]
  assert "us-gaap:RegionAxis" not in cubes["rpt:cube-0"]
  assert "us-gaap:RegionAxis" in cubes["rpt:cube-1"]
  assert "us-gaap:SegmentAxis" not in cubes["rpt:cube-1"]
  groups = {g["groupURI"]: g["name"] for g in document["xbrlModel"]["groups"]}
  assert {
    "groupName": groups["http://example.com/role/Regions"],
    "forObject": "rpt:cube-1",
  } in (document["xbrlModel"]["groupContents"])


def test_traversal_follows_target_role_into_another_section() -> None:
  """xbrldt:targetRole moves the next hop to another role; members live there."""
  model = _model()
  dim = "http://xbrl.org/int/dim/arcrole"
  definition = next(n for n in model.networks if n.kind == "definition")
  dimension_domain = next(
    a for a in definition.arcs if a.arcrole == f"{dim}/dimension-domain"
  )
  dimension_domain.target_role = "http://example.com/role/SegmentMembers"
  definition.arcs = [a for a in definition.arcs if a.arcrole != f"{dim}/domain-member"]
  model.networks.append(
    Network(
      role_uri="http://example.com/role/SegmentMembers",
      kind="definition",
      arcs=[
        Arc(
          from_qname="us-gaap:SegmentDomain",
          to_qname="us-gaap:NorthAmerica",
          arcrole=f"{dim}/domain-member",
        ),
        Arc(
          from_qname="us-gaap:SegmentDomain",
          to_qname="us-gaap:Asia",
          arcrole=f"{dim}/domain-member",
        ),
      ],
    )
  )
  document, _ = to_tavi_report(model)
  network = document["xbrlModel"]["domainNetworks"][0]
  assert {r["target"] for r in network["relationships"]} == {
    "us-gaap:NorthAmerica",
    "us-gaap:Asia",
  }


def test_domain_network_is_rooted_at_the_domain_class() -> None:
  """Section 5.10.1: the domain's root must match the dimension's domainClass."""
  network = _document()["xbrlModel"]["domainNetworks"][0]
  assert network["root"] == "us-gaap:SegmentDomain"
  assert network["relationships"] == [
    {"source": "us-gaap:SegmentDomain", "target": "us-gaap:NorthAmerica"}
  ]


def test_every_fact_now_falls_inside_a_cube() -> None:
  """Section 8.5.2.5 — the open cube catches the undimensioned facts."""
  _, gaps = to_tavi_report(_model())
  assert gaps.dimensional_facts == 1
  assert gaps.facts_without_cube == 0


def test_explicit_out_path_is_honoured_exactly(tmp_path: Any) -> None:
  """`-o` with one format writes that path, as it did before --format existed."""
  from xbrlkit.cli import _write_outputs

  target = tmp_path / "custom-name.json"
  _write_outputs(_model(), target, "holon", named=True)
  assert target.exists()
  assert not (tmp_path / "custom-name.json.holon.jsonld").exists()


def test_both_formats_derive_a_shared_stem(tmp_path: Any) -> None:
  """Two documents from one parse cannot share a name, so the stem is derived."""
  from xbrlkit.cli import _write_outputs

  _write_outputs(_model(), tmp_path / "acme.holon.jsonld", "both", named=True)
  assert (tmp_path / "acme.holon.jsonld").exists()
  assert (tmp_path / "acme.tavi.json").exists()
  assert (tmp_path / "acme.tavi.gaps.json").exists()
