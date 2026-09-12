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
  PLATFORM_NAMESPACE,
  GraphTables,
  build_lbug,
  copy_statement,
  graph_id,
  parse_structure_definition,
  to_graph_tables,
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
    path = build_lbug(tables, tmp_path / "filing.lbug")
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
    path = tmp_path / "filing.lbug"
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
