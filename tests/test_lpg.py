"""Tests for the property-graph projection (``xbrlkit.serialize.lpg``) and the
schema it writes into (``xbrlkit.schema``)."""

from __future__ import annotations

import uuid
from datetime import date
from pathlib import Path, PureWindowsPath

import pyarrow.parquet as pq
import pytest

from xbrlkit import schema
from xbrlkit.model import (
  Arc,
  Concept,
  DimQualifier,
  EntityIdentity,
  FilingMeta,
  Label,
  Network,
  Period,
  Reference,
  Unit,
  XbrlFact,
  XbrlModel,
)
from xbrlkit.serialize.lpg import (
  ICEBUG_DISK_VERSION,
  PLATFORM_NAMESPACE,
  GraphTables,
  build_lbug,
  copy_statement,
  graph_id,
  merge_graph_tables,
  parse_structure_definition,
  to_graph_tables,
  write_icebug,
  write_parquet,
)

REPORT_URI = (
  "https://www.sec.gov/Archives/edgar/data/66740/000006674025000006/mmm-20241231.htm"
)
US_GAAP = "http://fasb.org/us-gaap/2024"
MMM = "http://www.mmm.com/20241231"
PARENT_CHILD = "http://www.xbrl.org/2003/arcrole/parent-child"
SUMMATION = "http://www.xbrl.org/2003/arcrole/summation-item"


def _concept(qname: str, namespace: str, **kw) -> Concept:
  return Concept(
    qname=qname,
    namespace=namespace,
    name=qname.split(":")[1],
    labels=[
      Label(
        value=f"{qname} label",
        role="http://www.xbrl.org/2003/role/label",
        language="en-US",
      )
    ],
    **kw,
  )


@pytest.fixture
def model() -> XbrlModel:
  concepts = {
    "us-gaap:Revenues": _concept(
      "us-gaap:Revenues",
      US_GAAP,
      period_type="duration",
      balance="credit",
      is_numeric=True,
      nice_type="Monetary",
      item_type="monetaryItemType",
      item_type_qname="xbrli:monetaryItemType",
      item_type_namespace="http://www.xbrl.org/2003/instance",
      substitution_group="xbrli:item",
      substitution_group_namespace="http://www.xbrl.org/2003/instance",
      references=[
        Reference(value="Topic 606", role="http://www.xbrl.org/2003/role/reference")
      ],
    ),
    "us-gaap:Assets": _concept(
      "us-gaap:Assets",
      US_GAAP,
      period_type="instant",
      is_numeric=True,
      nice_type="Monetary",
    ),
    "us-gaap:GoodwillDisclosureTextBlock": _concept(
      "us-gaap:GoodwillDisclosureTextBlock",
      US_GAAP,
      period_type="duration",
      is_textblock=True,
      nice_type="TextBlock",
    ),
    "us-gaap:StatementBusinessSegmentsAxis": _concept(
      "us-gaap:StatementBusinessSegmentsAxis",
      US_GAAP,
      is_dimension_item=True,
      nice_type="Axis",
    ),
    "mmm:SafetyAndIndustrialMember": _concept(
      "mmm:SafetyAndIndustrialMember", MMM, is_domain_member=True, nice_type="Domain"
    ),
    "us-gaap:IncomeStatementAbstract": _concept(
      "us-gaap:IncomeStatementAbstract", US_GAAP, is_abstract=True
    ),
  }
  return XbrlModel(
    filing=FilingMeta(
      accession="0000066740-25-000006",
      cik="0000066740",
      form="10-K",
      filing_date=date(2025, 2, 5),
      fiscal_year_focus="2024",
      fiscal_period_focus="FY",
      fiscal_year_end_month="12",
      report_date=date(2024, 12, 31),
      acceptance_datetime="2025-02-05T16:03:20.000Z",
      is_inline_xbrl=True,
      primary_document="mmm-20241231.htm",
      report_uri=REPORT_URI,
      extension_namespace=MMM,
    ),
    entity=EntityIdentity(
      cik="0000066740",
      name="3M CO",
      legal_name="3M CO",
      ein="410417775",
      ticker="MMM",
      exchange="NYSE",
      sic="3841",
      sic_description="Surgical & Medical Instruments & Apparatus",
      category="Large accelerated filer",
      state_of_incorporation="DE",
      fiscal_year_end="1231",
      entity_type="operating",
      website="https://www.3m.com",
    ),
    concepts=concepts,
    periods=[
      Period(
        id="p-fy2024",
        period_type="duration",
        start=date(2024, 1, 1),
        end=date(2024, 12, 31),
        duration_type="annual",
        calendar_year=2024,
        calendar_quarter="FY",
        calendar_period_key="2024",
      ),
      Period(
        id="p-2024-12-31",
        period_type="instant",
        end=date(2024, 12, 31),
        calendar_year=2024,
        calendar_quarter="Q4",
        calendar_period_key="2024-12-31",
      ),
      Period(id="p-forever", period_type="forever"),
    ],
    units=[
      Unit(
        id="u-usd", measure="iso4217:USD", uri="http://www.xbrl.org/2003/iso4217#USD"
      ),
      Unit(
        id="u-usd-shares",
        measure="iso4217:USD/xbrli:shares",
        uri="http://www.xbrl.org/2003/iso4217#USD/http://www.xbrl.org/2003/instance#shares",
        numerator_uri="http://www.xbrl.org/2003/iso4217#USD",
        denominator_uri="http://www.xbrl.org/2003/instance#shares",
      ),
    ],
    facts=[
      XbrlFact(
        id="f1",
        concept_qname="us-gaap:Revenues",
        period_id="p-fy2024",
        unit_id="u-usd",
        entity_cik="0000066740",
        entity_scheme="http://www.sec.gov/CIK",
        entity_identifier="0000066740",
        value_str="24575000000",
        raw_value="24575000000",
        numeric_value=24575000000.0,
        decimals="-6",
        source_hash="aaa111",
      ),
      XbrlFact(
        id="f2",
        concept_qname="us-gaap:Revenues",
        period_id="p-fy2024",
        unit_id="u-usd",
        entity_cik="0000066740",
        entity_scheme="http://www.sec.gov/CIK",
        entity_identifier="0000066740",
        value_str="11000000000",
        raw_value="11000000000",
        numeric_value=11000000000.0,
        decimals="-6",
        source_hash="bbb222",
        dims=[
          DimQualifier(
            axis_qname="us-gaap:StatementBusinessSegmentsAxis",
            member_qname="mmm:SafetyAndIndustrialMember",
            axis_type="segment",
          )
        ],
      ),
      XbrlFact(
        id="f3",
        concept_qname="us-gaap:GoodwillDisclosureTextBlock",
        period_id="p-fy2024",
        entity_cik="0000066740",
        entity_scheme="http://www.sec.gov/CIK",
        entity_identifier="0000066740",
        value_kind="text",
        value_str="<p>Goodwill note</p>",
        raw_value="<p>Goodwill  note</p>",
        source_hash="ccc333",
      ),
      # a subsidiary's context, a duplicate fact (same hash), and a typed dimension
      XbrlFact(
        id="f4",
        concept_qname="us-gaap:Assets",
        period_id="p-2024-12-31",
        unit_id="u-usd",
        entity_cik="0000000042",
        entity_scheme="http://www.sec.gov/CIK",
        entity_identifier="42",
        value_str="7",
        raw_value="7",
        numeric_value=7.0,
        decimals="0",
        source_hash="ddd444",
      ),
      XbrlFact(
        id="f5",
        concept_qname="us-gaap:Assets",
        period_id="p-2024-12-31",
        unit_id="u-usd",
        entity_cik="0000000042",
        entity_scheme="http://www.sec.gov/CIK",
        entity_identifier="42",
        value_str="7",
        raw_value="7",
        numeric_value=7.0,
        decimals="0",
        source_hash="ddd444",
      ),
      XbrlFact(
        id="f6",
        concept_qname="us-gaap:Assets",
        period_id="p-forever",
        unit_id="u-usd",
        entity_cik="0000066740",
        entity_scheme="http://www.sec.gov/CIK",
        entity_identifier="0000066740",
        value_str="1",
        raw_value="1",
        numeric_value=1.0,
        decimals="0",
        source_hash="eee555",
        dims=[
          DimQualifier(
            axis_qname="us-gaap:StatementBusinessSegmentsAxis",
            typed_value="2024-Q4",
            is_explicit=False,
            axis_type="scenario",
          )
        ],
      ),
    ],
    networks=[
      Network(
        role_uri="http://www.mmm.com/role/IncomeStatement",
        definition="0000003 - Statement - Consolidated Statement of Income",
        kind="presentation",
        role_id="IncomeStatement",
        arcs=[
          Arc(
            from_qname="us-gaap:IncomeStatementAbstract",
            to_qname="us-gaap:Revenues",
            arcrole=PARENT_CHILD,
            order=1.0,
            is_root=True,
            preferred_label="http://www.xbrl.org/2003/role/totalLabel",
          )
        ],
      ),
      Network(
        role_uri="http://www.mmm.com/role/IncomeStatement",
        definition="0000003 - Statement - Consolidated Statement of Income",
        kind="calculation",
        role_id="IncomeStatement",
        arcs=[
          Arc(
            from_qname="us-gaap:Assets",
            to_qname="us-gaap:Revenues",
            arcrole=SUMMATION,
            order=1.0,
            weight=1.0,
            is_root=True,
          )
        ],
      ),
      Network(
        role_uri="http://www.mmm.com/role/NoRoleType",
        kind="presentation",
        arcs=[
          Arc(
            from_qname="us-gaap:Assets",
            to_qname="us-gaap:Revenues",
            arcrole=PARENT_CHILD,
          )
        ],
      ),
    ],
  )


@pytest.mark.unit
class TestSchema:
  def test_every_table_has_an_identifier_first_and_ddl_names_it(self):
    for table in schema.NODE_TABLES:
      assert table.columns[0] == "identifier"
      assert table.ddl().startswith(f"CREATE NODE TABLE IF NOT EXISTS {table.name}(")
      assert "PRIMARY KEY(identifier)" in table.ddl()
    for table in schema.REL_TABLES:
      assert table.columns[:2] == ("from", "to")
      assert table.ddl() == table.ddl().strip()
      assert schema.node_table(table.from_node) and schema.node_table(table.to_node)

  def test_ddl_order_is_nodes_then_relationships(self):
    statements = schema.ddl()
    kinds = ["NODE" if "NODE TABLE" in s else "REL" for s in statements]
    assert kinds == ["NODE"] * len(schema.NODE_TABLES) + ["REL"] * len(
      schema.REL_TABLES
    )
    assert (
      "CREATE REL TABLE IF NOT EXISTS TAXONOMY_HAS_LABEL(FROM Taxonomy TO Label,\n        element_uri STRING)"
      in statements
    )

  def test_type_defaults_mirror_the_platform(self):
    assert schema.type_default(schema.STRING) == ""
    assert schema.type_default(schema.INT32) == 0
    assert schema.type_default(schema.DOUBLE) == 0.0
    assert schema.type_default(schema.BOOLEAN) is False


@pytest.mark.unit
class TestIds:
  def test_platform_namespace_and_scheme(self):
    assert graph_id("element", f"{US_GAAP}#Revenues") == str(
      uuid.uuid5(PLATFORM_NAMESPACE, f"element:{US_GAAP}#Revenues")
    )

  def test_parse_structure_definition(self):
    assert parse_structure_definition(
      "0001001 - Statement - CONSOLIDATED BALANCE SHEETS"
    ) == ("0001001", "Statement", "CONSOLIDATED BALANCE SHEETS")
    assert parse_structure_definition(
      "995410 - Disclosure - Disclosure - Supplemental - Details"
    ) == ("995410", "Disclosure", "Supplemental - Details")
    assert parse_structure_definition("Just a name") == (None, None, "Just a name")
    assert parse_structure_definition("") == (None, None, None)


@pytest.mark.unit
class TestProjection:
  def test_every_table_is_present_and_rows_have_schema_columns(self, model):
    tables = to_graph_tables(model)
    assert set(tables.nodes) == {t.name for t in schema.NODE_TABLES}
    assert set(tables.relationships) == {t.name for t in schema.REL_TABLES}
    for name, rows in tables.nodes.items():
      for row in rows:
        assert tuple(row) == schema.node_table(name).columns
    for name, rows in tables.relationships.items():
      for row in rows:
        assert tuple(row) == schema.rel_table(name).columns
    assert not tables.nodes["FactSet"] and not tables.nodes["Classification"]

  def test_entity_and_report(self, model):
    tables = to_graph_tables(model)
    entities = {e["cik"]: e for e in tables.nodes["Entity"]}
    filer = entities["0000066740"]
    assert filer["identifier"] == graph_id(
      "entity", "http://www.sec.gov/CIK#0000066740"
    )
    assert (
      filer["tax_id"] == "410417775" and filer["industry"] == filer["sic_description"]
    )
    assert (
      filer["exchange"] == "NYSE" and filer["phone"] == ""
    )  # absent → the platform's STRING default
    assert filer["is_parent"] is True and filer["status"] == "active"
    subsidiary = entities["0000000042"]
    assert subsidiary["name"] == "42" and subsidiary["entity_type"] == "subsidiary"
    assert (
      subsidiary["parent_entity_id"] == filer["identifier"]
      and subsidiary["ticker"] == ""
    )

    (report,) = tables.nodes["Report"]
    assert report["identifier"] == graph_id("report", REPORT_URI)
    assert report["uri"] == REPORT_URI and report["name"] == "10-K"
    assert (
      report["filing_date"] == "2025-02-05"
      and report["acceptance_date"] == "2025-02-05"
    )
    assert (
      report["fiscal_year_focus"],
      report["fiscal_period_focus"],
      report["fiscal_year_end_month"],
    ) == (2024, "FY", 12)
    assert report["xbrl_processor_version"] == "1.0.0" and report["processed"] is False
    assert report["updated_at"] == ""
    assert tables.relationships["ENTITY_HAS_REPORT"] == [
      {"from": filer["identifier"], "to": report["identifier"]}
    ]

  def test_facts_dedupe_on_the_platform_id_and_keep_raw_values(self, model):
    tables = to_graph_tables(model)
    facts = {f["uri"]: f for f in tables.nodes["Fact"]}
    assert len(facts) == 5  # f4 and f5 share the hash
    revenue = facts[f"{REPORT_URI}#fact-aaa111"]
    assert revenue["identifier"] == graph_id("fact", f"{REPORT_URI}#fact-aaa111")
    assert revenue["fact_type"] == "Numeric" and revenue["decimals"] == "-6"
    assert (
      revenue["numeric_value"] == 24575000000.0 and revenue["value"] == "24575000000"
    )
    assert revenue["has_dimensions"] is False and revenue["dimension_count"] == 0
    text = facts[f"{REPORT_URI}#fact-ccc333"]
    assert text["value"] == "<p>Goodwill  note</p>"  # raw, not whitespace-processed
    assert text["fact_type"] == "Nonnumeric" and text["decimals"] is None
    assert text["numeric_value"] is None and text["value_type"] == "inline"
    segment = facts[f"{REPORT_URI}#fact-bbb222"]
    assert segment["has_dimensions"] is True and segment["dimension_count"] == 1

  def test_units_periods_and_their_edges(self, model):
    tables = to_graph_tables(model)
    units = {u["measure"]: u for u in tables.nodes["Unit"]}
    assert units["iso4217:USD"]["value"] == "USD"
    assert units["iso4217:USD"]["identifier"] == graph_id(
      "unit", "http://www.xbrl.org/2003/iso4217#USD"
    )
    assert units["iso4217:USD"]["numerator_uri"] is None
    periods = {p["period_type"]: p for p in tables.nodes["Period"]}
    annual = periods["duration"]
    assert (
      annual["uri"] == "http://www.w3.org/2001/XMLSchema#dateTime#2024-01-01/2024-12-31"
    )
    assert annual["days_in_period"] == 366 and annual["calendar_period_key"] == "2024"
    instant = periods["instant"]
    assert (
      instant["start_date"] is None
      and instant["end_date"] == "2024-12-31"
      and instant["days_in_period"] == 0
    )
    forever = periods["forever"]
    assert (
      forever["calendar_period_key"] == "forever" and forever["days_in_period"] is None
    )
    assert len(tables.relationships["FACT_HAS_UNIT"]) == 4
    assert len(tables.relationships["FACT_HAS_PERIOD"]) == 5

  def test_dimensions(self, model):
    tables = to_graph_tables(model)
    dims = {d["dimension_type"]: d for d in tables.nodes["Dimension"]}
    explicit = dims["xbrl_explicit"]
    axis_uri = f"{US_GAAP}#StatementBusinessSegmentsAxis"
    member_uri = f"{MMM}#SafetyAndIndustrialMember"
    assert explicit["identifier"] == graph_id(
      "dimension", f"{REPORT_URI}#dimension-{axis_uri}-{member_uri}"
    )
    assert (explicit["axis"], explicit["member"], explicit["type"]) == (
      "StatementBusinessSegmentsAxis",
      "SafetyAndIndustrialMember",
      "segment",
    )
    assert explicit["is_explicit"] is True and explicit["is_typed"] is False
    typed = dims["xbrl_typed"]
    assert typed["identifier"] == graph_id(
      "dimension", f"{REPORT_URI}#dimension-{axis_uri}-typed-2024-Q4"
    )
    assert (
      typed["member"] == "2024-Q4"
      and typed["member_uri"] == "2024-Q4"
      and typed["type"] == "scenario"
    )
    assert len(tables.relationships["DIMENSION_HAS_AXIS_ELEMENT"]) == 2
    assert len(tables.relationships["DIMENSION_HAS_MEMBER_ELEMENT"]) == 1
    assert len(tables.relationships["FACT_HAS_DIMENSION"]) == 2

  def test_elements_labels_references_and_taxonomy_edges(self, model):
    tables = to_graph_tables(model)
    elements = {e["qname"]: e for e in tables.nodes["Element"]}
    revenue = elements["us-gaap:Revenues"]
    assert revenue["identifier"] == graph_id("element", f"{US_GAAP}#Revenues")
    assert revenue["type"] == "Monetary" and revenue["balance"] == "credit"
    assert revenue["substitution_group"] == "http://www.xbrl.org/2003/instance#item"
    assert revenue["item_type"] == "http://www.xbrl.org/2003/instance#monetaryItemType"
    assert revenue["canonical_concept"] == "" and revenue["canonical_confidence"] == 0.0
    # only concepts the graph touches: facts, dimensions, and structures with a role type
    assert set(elements) == {
      "us-gaap:Revenues",
      "us-gaap:Assets",
      "us-gaap:GoodwillDisclosureTextBlock",
      "us-gaap:StatementBusinessSegmentsAxis",
      "mmm:SafetyAndIndustrialMember",
      "us-gaap:IncomeStatementAbstract",
    }
    (taxonomy,) = tables.nodes["Taxonomy"]
    assert taxonomy["uri"] == MMM and taxonomy["name"] == ""
    labels = tables.relationships["TAXONOMY_HAS_LABEL"]
    assert all(row["from"] == taxonomy["identifier"] for row in labels)
    assert {row["element_uri"] for row in labels} == {
      e["uri"] for e in tables.nodes["Element"]
    }
    (reference,) = tables.nodes["Reference"]
    assert reference["value"] == "Topic 606"
    assert tables.relationships["ELEMENT_HAS_REFERENCE"] == [
      {"from": revenue["identifier"], "to": reference["identifier"]}
    ]
    assert tables.relationships["TAXONOMY_HAS_REFERENCE"] == [
      {"from": taxonomy["identifier"], "to": reference["identifier"]}
    ]

  def test_structures_and_associations(self, model):
    tables = to_graph_tables(model)
    (structure,) = tables.nodes[
      "Structure"
    ]  # the network without a role type is skipped
    assert structure["uri"] == f"{MMM}#IncomeStatement"
    assert structure["identifier"] == graph_id(
      "structure", f"structure:0000066740-25-000006#{MMM}#IncomeStatement"
    )
    assert (structure["number"], structure["type"], structure["name"]) == (
      "0000003",
      "Statement",
      "Consolidated Statement of Income",
    )
    assert (
      structure["canonical_type"] == "" and structure["canonical_confidence"] == 0.0
    )
    associations = {a["association_type"]: a for a in tables.nodes["Association"]}
    presentation = associations["Presentation"]
    assert (
      presentation["weight"] is None
      and presentation["root"] is True
      and presentation["order_value"] == 1.0
    )
    assert presentation["preferred_label"] == "http://www.xbrl.org/2003/role/totalLabel"
    calculation = associations["Calculation"]
    assert calculation["weight"] == 1.0 and calculation["arcrole"] == SUMMATION
    elements = {e["qname"]: e["identifier"] for e in tables.nodes["Element"]}
    froms = {
      r["from"]: r["to"] for r in tables.relationships["ASSOCIATION_HAS_FROM_ELEMENT"]
    }
    tos = {
      r["from"]: r["to"] for r in tables.relationships["ASSOCIATION_HAS_TO_ELEMENT"]
    }
    assert (
      froms[presentation["identifier"]] == elements["us-gaap:IncomeStatementAbstract"]
    )
    assert tos[presentation["identifier"]] == elements["us-gaap:Revenues"]
    assert {r["to"] for r in tables.relationships["STRUCTURE_HAS_ASSOCIATION"]} == set(
      associations[a]["identifier"] for a in associations
    )
    assert tables.relationships["STRUCTURE_HAS_TAXONOMY"] == [
      {"from": structure["identifier"], "to": tables.nodes["Taxonomy"][0]["identifier"]}
    ]

  def test_calculations_1_1_arcs_are_calculation_associations(self, model):
    """The 2023 summation-item arcrole is the calculation linkbase too, and
    keeps its weight."""
    model.networks[1].arcs[0].arcrole = "https://xbrl.org/2023/arcrole/summation-item"
    tables = to_graph_tables(model)
    calculation = next(
      a for a in tables.nodes["Association"] if a["association_type"] == "Calculation"
    )
    assert calculation["arcrole"] == "https://xbrl.org/2023/arcrole/summation-item"
    assert calculation["weight"] == 1.0

  def test_a_type_without_a_qname_keeps_its_namespace(self, model):
    """A concept read back from a serialization may carry a type's namespace
    and local name but no QName for a prefix the filing never bound; the
    projection writes the type from what it has rather than dropping it."""
    concept = model.concepts["us-gaap:Assets"]
    concept.item_type = "monetaryItemType"
    concept.item_type_qname = None
    concept.item_type_namespace = "http://www.xbrl.org/2003/instance"
    tables = to_graph_tables(model)
    assets = next(e for e in tables.nodes["Element"] if e["qname"] == "us-gaap:Assets")
    assert assets["item_type"] == "http://www.xbrl.org/2003/instance#monetaryItemType"

  def test_projection_is_deterministic(self, model):
    first = to_graph_tables(model)
    second = to_graph_tables(model)
    assert first.nodes == second.nodes and first.relationships == second.relationships


@pytest.mark.unit
class TestParquetAndDatabase:
  def test_parquet_columns_follow_the_schema(self, model, tmp_path: Path):
    tables = to_graph_tables(model)
    written = write_parquet(tables, tmp_path)
    names = {p.relative_to(tmp_path).as_posix() for p in written}
    assert (
      "nodes/Fact.parquet" in names
      and "relationships/FACT_HAS_ELEMENT.parquet" in names
    )
    assert "nodes/FactSet.parquet" not in names  # empty tables are not written
    fact = pq.read_table(tmp_path / "nodes" / "Fact.parquet")
    assert fact.column_names == list(schema.node_table("Fact").columns)
    assert str(fact.schema.field("dimension_count").type) == "int64"
    report = pq.read_table(tmp_path / "nodes" / "Report.parquet")
    assert str(report.schema.field("fiscal_year_focus").type) == "int32"
    association = pq.read_table(tmp_path / "nodes" / "Association.parquet")
    assert (
      str(association.schema.field("root").type) == "bool"
    )  # as the platform writes it
    label = pq.read_table(tmp_path / "relationships" / "TAXONOMY_HAS_LABEL.parquet")
    assert label.column_names == ["from", "to", "element_uri"]

  def test_build_and_query_a_database(self, model, tmp_path: Path):
    lbug = pytest.importorskip("ladybug")
    tables = to_graph_tables(model)
    path = build_lbug(tables, tmp_path / "filing.lbdb")
    assert path.exists()
    db = lbug.Database(str(path), read_only=True)
    conn = lbug.Connection(db)
    try:
      rows = conn.execute(
        "MATCH (r:Report)-[:REPORT_HAS_FACT]->(f:Fact {has_dimensions: false})-[:FACT_HAS_ELEMENT]->"
        "(e:Element {qname: 'us-gaap:Revenues'}), (f)-[:FACT_HAS_PERIOD]->(p:Period) "
        "RETURN r.form, e.qname, p.end_date, f.numeric_value"
      ).get_all()
      assert rows == [["10-K", "us-gaap:Revenues", "2024-12-31", 24575000000.0]]
      assert conn.execute(
        "MATCH (a:Association) RETURN a.root ORDER BY a.root"
      ).get_all() == [["True"], ["True"]]
      assert conn.execute(
        "MATCH (t:Taxonomy)-[l:TAXONOMY_HAS_LABEL]->(:Label) RETURN count(l)"
      ).get_all() == [[6]]
      tables_present = {
        row[0] for row in conn.execute("CALL show_tables() RETURN name").get_all()
      }
      assert {t.name for t in schema.NODE_TABLES} <= tables_present
    finally:
      conn.close()
      db.close()

  def test_rebuild_replaces_an_existing_database(self, model, tmp_path: Path):
    pytest.importorskip("ladybug")
    tables = to_graph_tables(model)
    path = tmp_path / "filing.lbdb"
    build_lbug(tables, path)
    build_lbug(GraphTables(), path)
    lbug = __import__("ladybug")
    db = lbug.Database(str(path), read_only=True)
    conn = lbug.Connection(db)
    try:
      assert conn.execute("MATCH (f:Fact) RETURN count(f)").get_all() == [[0]]
    finally:
      conn.close()
      db.close()

  def test_copy_statement_takes_the_posix_path_on_windows(self):
    """LadybugDB reads a string literal's backslashes as escapes, so a Windows
    path handed to COPY verbatim is a parser error: ``\\n`` in ``\\nodes`` is
    a newline. The statement carries the posix form on every platform."""
    parquet = PureWindowsPath(
      r"C:\Users\user\AppData\Local\Temp\xbrlkit-lpg-1\nodes\Fact.parquet"
    )
    statement = copy_statement("Fact", parquet)
    assert statement == (
      'COPY Fact FROM "C:/Users/user/AppData/Local/Temp/xbrlkit-lpg-1/nodes/Fact.parquet"'
    )
    assert "\\" not in statement


def _read(path: Path):
  with open(path, "rb") as handle:
    return pq.read_table(handle)


def _mount(lbug, schema_cypher: Path):
  db = lbug.Database(":memory:")
  conn = lbug.Connection(db)
  for statement in schema_cypher.read_text().split(";\n"):
    if statement.strip():
      conn.execute(statement)
  return db, conn


@pytest.mark.unit
class TestIcebug:
  def test_every_table_is_written_in_the_icebug_disk_layout(
    self, model, tmp_path: Path
  ):
    write_icebug(to_graph_tables(model), tmp_path / "tree")
    names = {p.name for p in (tmp_path / "tree").iterdir()}
    for table in schema.NODE_TABLES:
      assert f"nodes_{table.name}.parquet" in names  # empty tables too
    for table in schema.REL_TABLES:
      assert {f"indices_{table.name}.parquet", f"indptr_{table.name}.parquet"} <= names
    assert "schema.cypher" in names
    metadata = pq.read_metadata(tmp_path / "tree" / "nodes_Fact.parquet").metadata
    assert metadata[b"icebug_disk_version"] == ICEBUG_DISK_VERSION.encode()
    fact = _read(tmp_path / "tree" / "nodes_Fact.parquet")
    assert fact.column_names == list(schema.node_table("Fact").columns)

  def test_csr_arrays_reproduce_every_edge(self, model, tmp_path: Path):
    tables = to_graph_tables(model)
    write_icebug(tables, tmp_path / "tree")
    for spec in schema.REL_TABLES:
      sources = [r["identifier"] for r in tables.nodes[spec.from_node]]
      targets = [r["identifier"] for r in tables.nodes[spec.to_node]]
      pointers = (
        _read(tmp_path / "tree" / f"indptr_{spec.name}.parquet")
        .column("ptr")
        .to_pylist()
      )
      indices = _read(tmp_path / "tree" / f"indices_{spec.name}.parquet")
      assert len(pointers) == len(sources) + 1 and pointers[-1] == indices.num_rows
      assert pointers == sorted(pointers)
      target_offsets = indices.column("target").to_pylist()
      rebuilt = sorted(
        (sources[i], targets[target_offsets[k]])
        for i in range(len(sources))
        for k in range(pointers[i], pointers[i + 1])
      )
      assert rebuilt == sorted(
        (r["from"], r["to"]) for r in tables.relationships[spec.name]
      )
    label = _read(tmp_path / "tree" / "indices_TAXONOMY_HAS_LABEL.parquet")
    assert label.column_names == ["target", "element_uri"]

  def test_columns_are_written_in_their_declared_type(self, model, tmp_path: Path):
    """No COPY casts a tree on its way in, so a STRING column holds strings —
    ``Association.root`` included, which the parquet projection writes as
    booleans and a ``.lbdb`` stores as ``"True"`` / ``"False"``."""
    write_icebug(to_graph_tables(model), tmp_path / "tree")
    association = _read(tmp_path / "tree" / "nodes_Association.parquet")
    assert str(association.schema.field("root").type) == "string"
    assert set(association.column("root").to_pylist()) == {"True"}
    report = _read(tmp_path / "tree" / "nodes_Report.parquet")
    assert str(report.schema.field("fiscal_year_focus").type) == "int32"

  def test_schema_cypher_declares_every_table_over_the_storage(
    self, model, tmp_path: Path
  ):
    tables = to_graph_tables(model)
    write_icebug(tables, tmp_path / "tree")
    statements = [
      s
      for s in (tmp_path / "tree" / "schema.cypher").read_text().split(";\n")
      if s.strip()
    ]
    assert len(statements) == len(schema.NODE_TABLES) + len(schema.REL_TABLES)
    local = (tmp_path / "tree").resolve().as_posix()
    assert all(
      s.endswith(f"WITH (storage = '{local}', format = 'icebug-disk')")
      for s in statements
    )
    write_icebug(
      tables, tmp_path / "hosted", storage="hf://datasets/acme/filings/main/mmm"
    )
    hosted = (tmp_path / "hosted" / "schema.cypher").read_text()
    assert (
      "storage = 'hf://datasets/acme/filings/main/mmm'" in hosted
      and local not in hosted
    )

  def test_a_rewrite_replaces_the_tree(self, model, tmp_path: Path):
    write_icebug(to_graph_tables(model), tmp_path / "tree")
    (tmp_path / "tree" / "stray.parquet").write_bytes(b"")
    write_icebug(GraphTables(), tmp_path / "tree")
    assert not (tmp_path / "tree" / "stray.parquet").exists()
    assert _read(tmp_path / "tree" / "nodes_Fact.parquet").num_rows == 0

  def test_an_edge_to_a_missing_node_is_refused(self, model, tmp_path: Path):
    tables = to_graph_tables(model)
    tables.relationships["FACT_HAS_ELEMENT"].append(
      {"from": "no-such-fact", "to": "no-such-element"}
    )
    with pytest.raises(ValueError, match="FACT_HAS_ELEMENT"):
      write_icebug(tables, tmp_path / "tree")

  def test_a_mounted_tree_reads_as_the_database_does(
    self, model, tmp_path: Path, monkeypatch
  ):
    """Every table, read through Cypher from a tree mounted in place, matches
    the same table in a ``.lbdb`` built from the same rows — from another
    working directory, since the storage path is absolute. Typed patterns only:
    untyped ones are wrong on icebug-disk tables (LadybugDB/ladybug#1066)."""
    lbug = pytest.importorskip("ladybug")
    tables = to_graph_tables(model)
    built = build_lbug(tables, tmp_path / "filing.lbdb")
    write_icebug(tables, tmp_path / "tree")
    (tmp_path / "elsewhere").mkdir()
    monkeypatch.chdir(tmp_path / "elsewhere")

    def rows(conn) -> dict[str, list]:
      out = {}
      for spec in schema.NODE_TABLES:
        columns = ", ".join(f"n.{p.name}" for p in spec.properties)
        out[spec.name] = conn.execute(
          f"MATCH (n:{spec.name}) RETURN {columns} ORDER BY n.identifier"
        ).get_all()
      for spec in schema.REL_TABLES:
        props = "".join(f", r.{p.name}" for p in spec.properties)
        out[spec.name] = sorted(
          conn.execute(
            f"MATCH (a:{spec.from_node})-[r:{spec.name}]->(b:{spec.to_node}) RETURN a.identifier, b.identifier{props}"
          ).get_all()
        )
      return out

    db = lbug.Database(str(built), read_only=True)
    conn = lbug.Connection(db)
    try:
      expected = rows(conn)
    finally:
      conn.close()
      db.close()
    db, conn = _mount(lbug, tmp_path / "tree" / "schema.cypher")
    try:
      assert rows(conn) == expected
      assert sum(len(v) for v in expected.values()) > 0
      assert conn.execute(
        "MATCH (r:Report)-[:REPORT_HAS_FACT]->(f:Fact {has_dimensions: false})-[:FACT_HAS_ELEMENT]->"
        "(e:Element {qname: 'us-gaap:Revenues'}), (f)-[:FACT_HAS_PERIOD]->(p:Period) "
        "RETURN r.form, e.qname, p.end_date, f.numeric_value"
      ).get_all() == [["10-K", "us-gaap:Revenues", "2024-12-31", 24575000000.0]]
    finally:
      conn.close()
      db.close()


NEXT_REPORT_URI = (
  "https://www.sec.gov/Archives/edgar/data/66740/000006674026000009/mmm-20251231.htm"
)


def _next_year(model: XbrlModel) -> XbrlModel:
  """The same filer's next report: a new accession, one fact restated."""
  facts = [
    f.model_copy(
      update={
        "numeric_value": 24000000000.0,
        "raw_value": "24000000000",
        "value_str": "24000000000",
      }
    )
    if f.concept_qname == "us-gaap:Revenues"
    else f
    for f in model.facts
  ]
  filing = model.filing.model_copy(
    update={"accession": "0000066740-26-000009", "report_uri": NEXT_REPORT_URI}
  )
  return model.model_copy(update={"filing": filing, "facts": facts})


REVENUE_BY_REPORT = (
  "MATCH (r:Report)-[:REPORT_HAS_FACT]->(f:Fact {has_dimensions: false})-[:FACT_HAS_ELEMENT]->"
  "(e:Element {qname: 'us-gaap:Revenues'}) RETURN r.accession_number, f.numeric_value "
  "ORDER BY r.accession_number"
)


@pytest.mark.unit
class TestMerge:
  def test_shared_rows_are_kept_once_and_reports_stay_apart(self, model):
    one, two = to_graph_tables(model), to_graph_tables(_next_year(model))
    merged = merge_graph_tables([one, two])
    counts = merged.counts()
    assert counts["Entity"] == one.counts()["Entity"]
    assert counts["Report"] == 2
    assert counts["Fact"] == one.counts()["Fact"] + two.counts()["Fact"]
    assert counts["Period"] == one.counts()["Period"]
    assert counts["Element"] == one.counts()["Element"]
    assert counts["ENTITY_HAS_REPORT"] == 2
    assert counts["ELEMENT_HAS_LABEL"] == one.counts()["ELEMENT_HAS_LABEL"]

  def test_stacking_a_filing_on_itself_changes_nothing(self, model):
    tables = to_graph_tables(model)
    merged = merge_graph_tables([tables, to_graph_tables(model)])
    assert merged.nodes == tables.nodes
    assert merged.relationships == tables.relationships

  def test_an_id_with_two_meanings_is_refused(self, model):
    one, two = to_graph_tables(model), to_graph_tables(_next_year(model))
    two.nodes["Unit"][0] = {**two.nodes["Unit"][0], "measure": "iso4217:EUR"}
    with pytest.raises(ValueError, match="Unit .* different measure"):
      merge_graph_tables([one, two])

  def test_an_entity_is_described_as_of_the_newest_filing(self, model):
    """A filer's category, name or exchange can change between filings; the
    stack keeps the newest filing's description whatever the stacking order,
    and each filing's own value stays in its dei facts."""
    older = to_graph_tables(model)
    later = model.model_copy(
      update={
        "filing": model.filing.model_copy(
          update={
            "accession": "0000066740-26-000009",
            "report_uri": NEXT_REPORT_URI,
            "filing_date": date(2026, 2, 4),
          }
        ),
        "entity": model.entity.model_copy(update={"category": "Non-accelerated Filer"}),
      }
    )
    newer = to_graph_tables(later)
    filer = older.nodes["Entity"][0]["identifier"]
    assert newer.nodes["Entity"][0]["identifier"] == filer
    for parts in ([older, newer], [newer, older]):
      merged = merge_graph_tables(parts)
      kept = [r for r in merged.nodes["Entity"] if r["identifier"] == filer]
      assert len(kept) == 1 and kept[0]["category"] == "Non-accelerated Filer"
      assert merged.counts()["ENTITY_HAS_REPORT"] == 2
    assert older.nodes["Entity"][0]["category"] == "Large accelerated filer"

  def test_an_element_role_one_filing_shows_is_kept(self, model):
    """A read-back model marks a table a hypercube only where its own filing
    presents it as one, so two filings can disagree on the same element."""
    one, two = to_graph_tables(model), to_graph_tables(_next_year(model))
    qname = "us-gaap:IncomeStatementAbstract"
    for tables, flag in ((one, True), (two, False)):
      row = next(r for r in tables.nodes["Element"] if r["qname"] == qname)
      row["is_hypercube_item"] = flag
    for parts in ([one, two], [two, one]):
      merged = merge_graph_tables(parts)
      kept = [r for r in merged.nodes["Element"] if r["qname"] == qname]
      assert len(kept) == 1 and kept[0]["is_hypercube_item"] is True
    untouched = next(r for r in two.nodes["Element"] if r["qname"] == qname)
    assert untouched["is_hypercube_item"] is False  # the inputs are not rewritten

  def test_any_other_element_difference_is_still_refused(self, model):
    one, two = to_graph_tables(model), to_graph_tables(_next_year(model))
    row = next(r for r in two.nodes["Element"] if r["qname"] == "us-gaap:Revenues")
    row["is_numeric"] = not row["is_numeric"]
    with pytest.raises(ValueError, match="Element .* different is_numeric"):
      merge_graph_tables([one, two])

  def test_a_stack_reads_both_reports_from_either_container(
    self, model, tmp_path: Path
  ):
    lbug = pytest.importorskip("ladybug")
    merged = merge_graph_tables(
      [to_graph_tables(model), to_graph_tables(_next_year(model))]
    )
    expected = [
      ["0000066740-25-000006", 24575000000.0],
      ["0000066740-26-000009", 24000000000.0],
    ]
    db = lbug.Database(str(build_lbug(merged, tmp_path / "stack.lbdb")), read_only=True)
    conn = lbug.Connection(db)
    try:
      assert conn.execute(REVENUE_BY_REPORT).get_all() == expected
    finally:
      conn.close()
      db.close()
    write_icebug(merged, tmp_path / "stack.icebug")
    db, conn = _mount(lbug, tmp_path / "stack.icebug" / "schema.cypher")
    try:
      assert conn.execute(REVENUE_BY_REPORT).get_all() == expected
    finally:
      conn.close()
      db.close()


def _read_back(model: XbrlModel) -> XbrlModel:
  """The model as it comes back from its TAVI, with the two identity fields the
  serve session restores for a published filing put back."""
  from xbrlkit.deserialize import from_tavi_json
  from xbrlkit.serialize import to_tavi

  back = from_tavi_json(to_tavi(model))
  back.filing.report_uri = model.filing.report_uri
  back.filing.extension_namespace = model.filing.extension_namespace
  return back


@pytest.mark.unit
class TestReadBackModels:
  def test_duplicates_collapse_without_a_parser_hash(self, model):
    unhashed = [f.model_copy(update={"source_hash": None}) for f in model.facts]
    revenue = next(f for f in unhashed if f.concept_qname == "us-gaap:Revenues")
    twice = revenue.model_copy(update={"id": "f-again", "decimals": "-3"})
    tables = to_graph_tables(model.model_copy(update={"facts": [*unhashed, twice]}))
    assert len(tables.nodes["Fact"]) == len(to_graph_tables(model).nodes["Fact"])

  def test_role_ids_are_derived_only_when_the_model_has_none(self, model):
    parsed = {r["uri"] for r in to_graph_tables(model).nodes["Structure"]}
    assert f"{MMM}#NoRoleType" not in parsed  # an Arelle network with no id
    stripped = model.model_copy(
      update={
        "networks": [n.model_copy(update={"role_id": None}) for n in model.networks]
      }
    )
    derived = {r["uri"] for r in to_graph_tables(stripped).nodes["Structure"]}
    assert derived == parsed | {f"{MMM}#NoRoleType"}

  def test_a_tavi_round_trip_keeps_the_graph_s_identity(self, model):
    parsed, back = to_graph_tables(model), to_graph_tables(_read_back(model))
    for table in ("Report", "Period", "Unit", "Dimension", "Structure"):
      assert {r["identifier"] for r in parsed.nodes[table]} <= {
        r["identifier"] for r in back.nodes[table]
      }, table
    for kind in ("Presentation", "Calculation"):
      ids = [
        {
          r["identifier"]
          for r in t.nodes["Association"]
          if r["association_type"] == kind
        }
        for t in (parsed, back)
      ]
      assert ids[0] and ids[0] <= ids[1], kind
    assert len(back.nodes["Fact"]) == len(parsed.nodes["Fact"])
