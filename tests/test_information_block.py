"""Tests for ``information_block`` and the two tools built on it.

A hand-authored filing carries what the rules need: an income statement with
a calculation network, a balance sheet with its parenthetical, and a leases
note split the way EDGAR filers split one — the note (a text block), its
tables, a details table with a hypercube (one axis, two members, a default),
a calculation arc and a fact on an axis the cube does not declare, and a
second details table with no cube at all. Every rule the tools carry — the
family read off the titles, the level, cube admission, the member pivot, the
footing check, the text pointer — is checked without Arelle or the network.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

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
from xbrlkit.serve import FilingSession, LoadedFiling, build_text
from xbrlkit.serve import tools
from xbrlkit.information_block import (
  DIM_ALL,
  DIM_DIMENSION_DEFAULT,
  DIM_DIMENSION_DOMAIN,
  DIM_DOMAIN_MEMBER,
  DIM_HYPERCUBE_DIMENSION,
  fact_membership,
  group_disclosures,
  parse_definition,
  parse_level,
  plan_blocks,
)

US_GAAP = "http://fasb.org/us-gaap/2024-01-31"
ACME = "http://acme.example/20241231"
IS_ROLE = "http://acme.example/role/StatementOfIncome"
BS_ROLE = "http://acme.example/role/BalanceSheet"
BS_PAREN_ROLE = "http://acme.example/role/BalanceSheetParenthetical"
NOTE_ROLE = "http://acme.example/role/Leases"
TABLES_ROLE = "http://acme.example/role/LeasesTables"
COST_ROLE = "http://acme.example/role/LeasesLeaseCostDetails"
MATURITY_ROLE = "http://acme.example/role/LeasesMaturitiesDetails"
IFRS_ROLE = "http://acme.example/role/Unnumbered"
TOTAL = "http://www.xbrl.org/2003/role/totalLabel"

SEGMENT_AXIS = "us-gaap:StatementBusinessSegmentsAxis"
GEO_AXIS = "srt:StatementGeographicalAxis"


def _concept(qname: str, **kw) -> Concept:
  prefix, name = qname.split(":")
  base = dict(
    qname=qname,
    namespace=US_GAAP if prefix == "us-gaap" else ACME,
    name=name,
    period_type="duration",
    is_numeric=True,
    item_type="monetaryItemType",
    pref_label=name,
  )
  base.update(kw)
  return Concept(**base)


def _model() -> XbrlModel:
  filing = FilingMeta(
    accession="0000000000-25-000002",
    cik="0001234567",
    form="10-K",
    report_date=date(2024, 12, 31),
    fiscal_year_focus="2024",
  )
  entity = EntityIdentity(cik="0001234567", name="Acme Corp", ticker="ACME")
  abstract = dict(is_abstract=True, is_numeric=False, item_type=None)
  concepts = {
    q: _concept(q, **kw)
    for q, kw in {
      "us-gaap:IncomeStatementAbstract": abstract,
      "us-gaap:Revenues": dict(
        labels=[Label(value="Total revenues", role=TOTAL)], pref_label="Revenues"
      ),
      "us-gaap:CostOfRevenue": {},
      "us-gaap:GrossProfit": {},
      "us-gaap:Assets": dict(period_type="instant"),
      "us-gaap:Cash": dict(period_type="instant"),
      "us-gaap:PreferredStockParOrStatedValuePerShare": dict(
        period_type="instant", item_type="perShareItemType"
      ),
      "us-gaap:LeasesAbstract": abstract,
      "us-gaap:LesseeOperatingLeasesTextBlock": dict(
        is_numeric=False, is_textblock=True, item_type="textBlockItemType"
      ),
      "us-gaap:LeaseCostTableTextBlock": dict(
        is_numeric=False, is_textblock=True, item_type="textBlockItemType"
      ),
      "us-gaap:LeaseCostTable": dict(is_hypercube_item=True, is_numeric=False),
      "us-gaap:LeasesLineItems": abstract,
      "us-gaap:OperatingLeaseCost": {},
      "us-gaap:VariableLeaseCost": {},
      "us-gaap:ShortTermLeaseCost": {},
      "us-gaap:LeaseCost": {},
      "us-gaap:LesseeOperatingLeaseLiabilityPaymentsDueNextTwelveMonths": dict(
        period_type="instant"
      ),
      "us-gaap:LesseeOperatingLeaseLiabilityPaymentsDueYearTwo": dict(
        period_type="instant"
      ),
      "us-gaap:LesseeOperatingLeaseLiabilityPaymentsDue": dict(period_type="instant"),
      SEGMENT_AXIS: dict(is_dimension_item=True, is_numeric=False),
      "us-gaap:SegmentDomain": dict(is_domain_member=True, is_numeric=False),
      "acme:WidgetsMember": dict(is_domain_member=True, is_numeric=False),
      "acme:GadgetsMember": dict(is_domain_member=True, is_numeric=False),
      "acme:GizmosMember": dict(is_domain_member=True, is_numeric=False),
      GEO_AXIS: dict(is_dimension_item=True, is_numeric=False),
      "country:US": dict(is_domain_member=True, is_numeric=False),
      "ifrs-full:Revenue": {},
    }.items()
  }
  periods = [
    Period(
      id="D-2024",
      period_type="duration",
      start=date(2024, 1, 1),
      end=date(2024, 12, 31),
      duration_type="annual",
      calendar_period_key="2024",
    ),
    Period(
      id="D-2023",
      period_type="duration",
      start=date(2023, 1, 1),
      end=date(2023, 12, 31),
      duration_type="annual",
      calendar_period_key="2023",
    ),
    Period(id="I-2024", period_type="instant", end=date(2024, 12, 31)),
  ]
  units = [Unit(id="usd", measure="iso4217:USD")]
  widgets = [DimQualifier(axis_qname=SEGMENT_AXIS, member_qname="acme:WidgetsMember")]
  gadgets = [DimQualifier(axis_qname=SEGMENT_AXIS, member_qname="acme:GadgetsMember")]
  gizmos = [DimQualifier(axis_qname=SEGMENT_AXIS, member_qname="acme:GizmosMember")]
  us = [DimQualifier(axis_qname=GEO_AXIS, member_qname="country:US")]

  def fact(fid, qname, pid, value, dims=(), decimals="-3"):
    return XbrlFact(
      id=fid,
      concept_qname=qname,
      period_id=pid,
      unit_id="usd",
      entity_cik="0001234567",
      dims=list(dims),
      value_str=str(value),
      numeric_value=float(value),
      decimals=decimals,
      value_kind="numeric",
    )

  def text(fid, qname, body):
    return XbrlFact(
      id=fid,
      concept_qname=qname,
      period_id="D-2024",
      entity_cik="0001234567",
      value_str=body,
      value_kind="text",
    )

  facts = [
    fact("r24", "us-gaap:Revenues", "D-2024", 1000),
    fact("r23", "us-gaap:Revenues", "D-2023", 900),
    fact("c24", "us-gaap:CostOfRevenue", "D-2024", 600),
    fact("c23", "us-gaap:CostOfRevenue", "D-2023", 500),
    fact("g24", "us-gaap:GrossProfit", "D-2024", 400),
    # 2023 gross profit does not foot: the filer reported 1,401 against 400,
    # a gap no rounding at the stated thousands explains.
    fact("g23", "us-gaap:GrossProfit", "D-2023", 1401),
    fact("a24", "us-gaap:Assets", "I-2024", 5000),
    fact("k24", "us-gaap:Cash", "I-2024", 1250),
    fact("p24", "us-gaap:PreferredStockParOrStatedValuePerShare", "I-2024", 0.01),
    fact("oc24", "us-gaap:OperatingLeaseCost", "D-2024", 100),
    fact("oc23", "us-gaap:OperatingLeaseCost", "D-2023", 90),
    fact("vc24", "us-gaap:VariableLeaseCost", "D-2024", 20),
    fact("lc24", "us-gaap:LeaseCost", "D-2024", 120),
    fact("ocw", "us-gaap:OperatingLeaseCost", "D-2024", 60, dims=widgets),
    fact("ocg", "us-gaap:OperatingLeaseCost", "D-2024", 40, dims=gadgets),
    fact("lcw", "us-gaap:LeaseCost", "D-2024", 75, dims=widgets),
    # Reported for one segment only — no consolidated value at all.
    fact("stg", "us-gaap:ShortTermLeaseCost", "D-2024", 15, dims=gizmos),
    # A geography breakdown of the same concept: the lease-cost cube does not
    # declare that axis, so this fact belongs to no section here.
    fact("ocus", "us-gaap:OperatingLeaseCost", "D-2024", 55, dims=us),
    fact(
      "m1",
      "us-gaap:LesseeOperatingLeaseLiabilityPaymentsDueNextTwelveMonths",
      "I-2024",
      30,
    ),
    fact(
      "m2", "us-gaap:LesseeOperatingLeaseLiabilityPaymentsDueYearTwo", "I-2024", 100
    ),
    fact("m", "us-gaap:LesseeOperatingLeaseLiabilityPaymentsDue", "I-2024", 130),
    # Dimensional on a concept of the cube-less maturities table: not admitted.
    fact(
      "mw",
      "us-gaap:LesseeOperatingLeaseLiabilityPaymentsDue",
      "I-2024",
      80,
      dims=widgets,
    ),
    fact("ifrs", "ifrs-full:Revenue", "D-2024", 5),
    text(
      "t-note",
      "us-gaap:LesseeOperatingLeasesTextBlock",
      "<p>The company leases offices and warehouses under operating leases "
      "expiring through 2031. Lease cost is recognized on a straight-line basis "
      "over the lease term, and variable payments are expensed as incurred.</p>",
    ),
    text(
      "t-table",
      "us-gaap:LeaseCostTableTextBlock",
      "<table><tr><td>Operating lease cost</td><td>100</td></tr>"
      "<tr><td>Variable lease cost</td><td>20</td></tr>"
      "<tr><td>Total lease cost</td><td>120</td></tr></table>"
      "<p>The components of lease cost for the years presented were as shown "
      "in the table above, with all amounts in thousands of dollars.</p>",
    ),
  ]

  def pres(role, definition, arcs, **kw):
    return Network(
      role_uri=role, definition=definition, kind="presentation", arcs=arcs, **kw
    )

  networks = [
    pres(
      IS_ROLE,
      "0000001 - Statement - Consolidated Statements of Income",
      [
        Arc(
          from_qname="us-gaap:IncomeStatementAbstract",
          to_qname="us-gaap:Revenues",
          order=1,
          preferred_label=TOTAL,
        ),
        Arc(
          from_qname="us-gaap:IncomeStatementAbstract",
          to_qname="us-gaap:CostOfRevenue",
          order=2,
        ),
        Arc(
          from_qname="us-gaap:IncomeStatementAbstract",
          to_qname="us-gaap:GrossProfit",
          order=3,
        ),
      ],
      role_id="IncomeStatement",
      block_type="income_statement",
    ),
    Network(
      role_uri=IS_ROLE,
      definition="0000001 - Statement - Consolidated Statements of Income",
      kind="calculation",
      arcs=[
        Arc(from_qname="us-gaap:GrossProfit", to_qname="us-gaap:Revenues", weight=1.0),
        Arc(
          from_qname="us-gaap:GrossProfit",
          to_qname="us-gaap:CostOfRevenue",
          weight=-1.0,
        ),
      ],
    ),
    pres(
      BS_ROLE,
      "0000002 - Statement - Consolidated Balance Sheets",
      [Arc(from_qname="us-gaap:Assets", to_qname="us-gaap:Cash", order=1)],
    ),
    pres(
      BS_PAREN_ROLE,
      "0000003 - Statement - Consolidated Balance Sheets (Parenthetical)",
      [
        Arc(
          from_qname="us-gaap:Assets",
          to_qname="us-gaap:PreferredStockParOrStatedValuePerShare",
        )
      ],
    ),
    pres(
      NOTE_ROLE,
      "0000010 - Disclosure - Leases",
      [
        Arc(
          from_qname="us-gaap:LeasesAbstract",
          to_qname="us-gaap:LesseeOperatingLeasesTextBlock",
        )
      ],
    ),
    pres(
      TABLES_ROLE,
      "0000020 - Disclosure - Leases (Tables)",
      [
        Arc(
          from_qname="us-gaap:LeasesAbstract",
          to_qname="us-gaap:LeaseCostTableTextBlock",
        )
      ],
    ),
    pres(
      COST_ROLE,
      "0000030 - Disclosure - Leases - Lease Cost (Details)",
      [
        Arc(from_qname="us-gaap:LeaseCostTable", to_qname="us-gaap:LeasesLineItems"),
        Arc(
          from_qname="us-gaap:LeasesLineItems",
          to_qname="us-gaap:OperatingLeaseCost",
          order=1,
        ),
        Arc(
          from_qname="us-gaap:LeasesLineItems",
          to_qname="us-gaap:VariableLeaseCost",
          order=2,
        ),
        Arc(
          from_qname="us-gaap:LeasesLineItems",
          to_qname="us-gaap:LeaseCost",
          order=3,
          preferred_label=TOTAL,
        ),
        Arc(
          from_qname="us-gaap:LeasesLineItems",
          to_qname="us-gaap:ShortTermLeaseCost",
          order=4,
        ),
      ],
    ),
    Network(
      role_uri=COST_ROLE,
      definition="0000030 - Disclosure - Leases - Lease Cost (Details)",
      kind="calculation",
      arcs=[
        Arc(
          from_qname="us-gaap:LeaseCost",
          to_qname="us-gaap:OperatingLeaseCost",
          weight=1.0,
        ),
        Arc(
          from_qname="us-gaap:LeaseCost",
          to_qname="us-gaap:VariableLeaseCost",
          weight=1.0,
        ),
      ],
    ),
    Network(
      role_uri=COST_ROLE,
      definition="0000030 - Disclosure - Leases - Lease Cost (Details)",
      kind="definition",
      arcs=[
        Arc(
          from_qname="us-gaap:LeasesLineItems",
          to_qname="us-gaap:LeaseCostTable",
          arcrole=DIM_ALL,
        ),
        Arc(
          from_qname="us-gaap:LeaseCostTable",
          to_qname=SEGMENT_AXIS,
          arcrole=DIM_HYPERCUBE_DIMENSION,
        ),
        Arc(
          from_qname=SEGMENT_AXIS,
          to_qname="us-gaap:SegmentDomain",
          arcrole=DIM_DIMENSION_DOMAIN,
        ),
        Arc(
          from_qname="us-gaap:SegmentDomain",
          to_qname="acme:WidgetsMember",
          arcrole=DIM_DOMAIN_MEMBER,
        ),
        Arc(
          from_qname="us-gaap:SegmentDomain",
          to_qname="acme:GadgetsMember",
          arcrole=DIM_DOMAIN_MEMBER,
        ),
        Arc(
          from_qname="us-gaap:SegmentDomain",
          to_qname="acme:GizmosMember",
          arcrole=DIM_DOMAIN_MEMBER,
        ),
        Arc(
          from_qname=SEGMENT_AXIS,
          to_qname="us-gaap:SegmentDomain",
          arcrole=DIM_DIMENSION_DEFAULT,
        ),
      ],
    ),
    pres(
      MATURITY_ROLE,
      "0000031 - Disclosure - Leases - Maturities of lease liabilities (Details)",
      [
        Arc(
          from_qname="us-gaap:LesseeOperatingLeaseLiabilityPaymentsDue",
          to_qname="us-gaap:LesseeOperatingLeaseLiabilityPaymentsDueNextTwelveMonths",
          order=1,
        ),
        Arc(
          from_qname="us-gaap:LesseeOperatingLeaseLiabilityPaymentsDue",
          to_qname="us-gaap:LesseeOperatingLeaseLiabilityPaymentsDueYearTwo",
          order=2,
        ),
      ],
    ),
    # The filer's tooling put the maturities roll-up in a second drawer:
    # the same definition under a suffixed role, with no presentation.
    Network(
      role_uri=MATURITY_ROLE + "_1",
      definition="0000031 - Disclosure - Leases - Maturities of lease liabilities (Details)",
      kind="calculation",
      arcs=[
        Arc(
          from_qname="us-gaap:LesseeOperatingLeaseLiabilityPaymentsDue",
          to_qname="us-gaap:LesseeOperatingLeaseLiabilityPaymentsDueNextTwelveMonths",
          weight=1.0,
        ),
        Arc(
          from_qname="us-gaap:LesseeOperatingLeaseLiabilityPaymentsDue",
          to_qname="us-gaap:LesseeOperatingLeaseLiabilityPaymentsDueYearTwo",
          weight=1.0,
        ),
      ],
    ),
    pres(
      IFRS_ROLE,
      "[310000] Statement of comprehensive income",
      [Arc(from_qname="ifrs-full:Revenue", to_qname="ifrs-full:Revenue")],
    ),
  ]
  return XbrlModel(
    filing=filing,
    entity=entity,
    concepts=concepts,
    periods=periods,
    units=units,
    facts=facts,
    networks=networks,
  )


@pytest.fixture
def model() -> XbrlModel:
  return _model()


@pytest.fixture
def loaded(model: XbrlModel) -> LoadedFiling:
  text, sections = build_text(model, html=None)
  return LoadedFiling(
    id="acme", source="memory", model=model, text=text, sections=sections
  )


# -- parsing the filer's titles ---------------------------------------------------


@pytest.mark.parametrize(
  "definition, expected",
  [
    (
      "9955528 - Disclosure - Leases (Tables)",
      ("9955528", "Disclosure", "Leases (Tables)"),
    ),
    (
      "0001001 - Statement - CONSOLIDATED BALANCE SHEETS",
      ("0001001", "Statement", "CONSOLIDATED BALANCE SHEETS"),
    ),
    (
      "[310000] Statement of comprehensive income",
      (None, None, "[310000] Statement of comprehensive income"),
    ),
    (None, (None, None, "")),
  ],
)
def test_parse_definition(definition, expected):
  assert parse_definition(definition) == expected


@pytest.mark.parametrize(
  "category, name, expected",
  [
    ("Disclosure", "Leases", ("note", "Leases", None)),
    ("Disclosure", "Leases (Policies)", ("policies", "Leases", None)),
    ("Disclosure", "Leases (Tables)", ("tables", "Leases", None)),
    (
      "Disclosure",
      "Leases - Maturities of lease liabilities (Details)",
      ("details", "Leases", "Maturities of lease liabilities"),
    ),
    (
      "Disclosure",
      "Derivatives - Cash Flow Hedges - Gain (Loss) in OCI (Details)",
      ("details", "Derivatives", "Cash Flow Hedges - Gain (Loss) in OCI"),
    ),
    ("Disclosure", "Income Taxes (Details Textual)", ("details", "Income Taxes", None)),
    ("Disclosure", "Note 5 - Revenue", ("note", "Note 5 - Revenue", None)),
    (
      "Statement",
      "Consolidated Balance Sheet",
      ("statement", "Consolidated Balance Sheet", None),
    ),
    (
      "Statement",
      "Consolidated Balance Sheet (Parenthetical)",
      ("parenthetical", "Consolidated Balance Sheet", None),
    ),
    ("Document", "Cover", ("document", "Cover", None)),
    (
      None,
      "[310000] Statement of comprehensive income",
      ("other", "[310000] Statement of comprehensive income", None),
    ),
  ],
)
def test_parse_level(category, name, expected):
  assert parse_level(category, name) == expected


# -- blocks and families -----------------------------------------------------------


def test_plan_blocks_groups_networks_by_role_in_edgar_order(model):
  blocks = plan_blocks(model)
  by_role = {s.role_uri: s for s in blocks}
  assert [s.number for s in blocks][:3] == ["0000001", "0000002", "0000003"]
  assert blocks[-1].role_uri == IFRS_ROLE  # unnumbered sorts last
  cost = by_role[COST_ROLE]
  assert len(cost.presentation) == 1 and cost.has_calc and len(cost.hypercubes) == 1
  assert cost.level == "details" and cost.disclosure == "Leases"
  assert cost.subtitle == "Lease Cost"
  income = by_role[IS_ROLE]
  assert income.id == "IncomeStatement"  # the role id wins over the path segment
  assert by_role[MATURITY_ROLE].id == "LeasesMaturitiesDetails"


def test_hypercube_is_walked_from_the_roles_definition_arcs(model):
  cost = next(s for s in plan_blocks(model) if s.role_uri == COST_ROLE)
  (cube,) = cost.hypercubes
  assert cube.qname == "us-gaap:LeaseCostTable"
  assert cube.primary_items == ["us-gaap:LeasesLineItems"]
  (axis,) = cube.axes
  assert axis.qname == SEGMENT_AXIS
  assert axis.domain == "us-gaap:SegmentDomain"
  assert axis.members == [
    "acme:WidgetsMember",
    "acme:GadgetsMember",
    "acme:GizmosMember",
  ]
  assert axis.default == "us-gaap:SegmentDomain"
  assert not axis.typed


def test_group_disclosures_reads_families_off_the_titles(model):
  families = group_disclosures(plan_blocks(model))
  by_name = {f.name: f for f in families}
  assert by_name["Leases"].levels == {"note": 1, "tables": 1, "details": 2}
  assert by_name["Consolidated Balance Sheets"].levels == {
    "statement": 1,
    "parenthetical": 1,
  }
  assert by_name["Consolidated Statements of Income"].levels == {"statement": 1}
  assert by_name["[310000] Statement of comprehensive income"].levels == {"other": 1}


def test_fact_membership_admits_by_the_blocks_own_cube(model):
  blocks = plan_blocks(model)
  membership = fact_membership(model, blocks)
  cost_ids = {f.id for f in membership[COST_ROLE]}
  assert {"oc24", "vc24", "lc24", "ocw", "ocg", "lcw", "stg"} <= cost_ids
  assert "ocus" not in cost_ids  # geography is not an axis of this cube
  maturity_ids = {f.id for f in membership[MATURITY_ROLE]}
  assert maturity_ids == {"m1", "m2", "m"}  # no cube: consolidated only
  assert "ocus" not in {f.id for facts in membership.values() for f in facts}


# -- the two tools ------------------------------------------------------------------


def test_disclosures_lists_families_with_counts(loaded):
  out = tools.disclosures(loaded)
  rows = {r["disclosure"]: r for r in out["disclosures"]}
  leases = rows["Leases"]
  assert leases["blocks"] == 4
  assert leases["levels"] == {"note": 1, "tables": 1, "details": 2}
  assert leases["facts"] == 11  # oc24 oc23 vc24 lc24 ocw ocg lcw stg m1 m2 m
  assert leases["text_blocks"] == 2
  assert "category" not in leases
  assert rows["Consolidated Balance Sheets"]["category"] == "Statement"
  assert out["count"] == len(rows)


def test_disclosures_topic_indexes_one_family(loaded):
  fam = tools.disclosures(loaded, "leases")
  assert fam["disclosure"] == "Leases" and fam["block_count"] == 4
  levels = [b["level"] for b in fam["blocks"]]
  assert levels == ["note", "tables", "details", "details"]
  note, tables, cost, maturity = fam["blocks"]
  assert note["text_blocks"][0]["concept"] == "us-gaap:LesseeOperatingLeasesTextBlock"
  assert tables["text_blocks"][0]["chars"] > 0
  assert cost["name"] == "Lease Cost"
  assert cost["axes"] == [SEGMENT_AXIS] and cost["calc"] is True
  assert cost["facts"] == 8 and cost["dimensional_facts"] == 4
  assert maturity["name"] == "Maturities of lease liabilities"
  assert "axes" not in maturity and maturity["facts"] == 3


def test_disclosures_topic_errors_are_correctable(loaded):
  with pytest.raises(tools.ToolError, match="No disclosure matches"):
    tools.disclosures(loaded, "goodwill")
  with pytest.raises(tools.ToolError, match="2 disclosures match"):
    tools.disclosures(loaded, "consolidated")


def test_information_block_pivots_a_details_table_by_its_own_axis(loaded):
  out = tools.information_block(loaded, "Lease Cost", whole=False)
  head = out["block"]
  assert head["disclosure"] == "Leases" and head["level"] == "details"
  assert "block_type" not in head  # a filing never classifies its own roles
  assert head["id"] == "LeasesLeaseCostDetails"
  assert [s["level"] for s in head["siblings"]] == ["note", "tables", "details"]
  assert [c["key"] for c in out["columns"]] == [
    "2024-01-01..2024-12-31",
    "2023-01-01..2023-12-31",
  ]

  (axis,) = out["axes"]
  assert axis["axis"] == SEGMENT_AXIS and axis["default"] == "us-gaap:SegmentDomain"
  assert [(m["member"], m["facts"]) for m in axis["members"]] == [
    ("acme:WidgetsMember", 2),
    ("acme:GadgetsMember", 1),
    ("acme:GizmosMember", 1),
  ]

  rows = {r["concept"]: r for r in out["rows"]}
  assert rows["us-gaap:LeaseCostTable"]["depth"] == 0
  operating = rows["us-gaap:OperatingLeaseCost"]
  assert operating["values"] == {
    "2024-01-01..2024-12-31": 100.0,
    "2023-01-01..2023-12-31": 90.0,
  }
  assert operating["members"] == {
    "acme:WidgetsMember": {"2024-01-01..2024-12-31": 60.0},
    "acme:GadgetsMember": {"2024-01-01..2024-12-31": 40.0},
  }
  assert rows["us-gaap:LeaseCost"]["label"] == "LeaseCost"
  # The geography fact is not in this section, so no member row carries it.
  assert "country:US" not in json.dumps(out)

  (calc,) = out["calculation"]
  assert calc["total"] == "us-gaap:LeaseCost"
  assert [c["concept"] for c in calc["children"]] == [
    "us-gaap:OperatingLeaseCost",
    "us-gaap:VariableLeaseCost",
  ]
  assert calc["checked"] == 1 and calc["foots"] == 1 and "differences" not in calc
  assert "text" not in out and out["truncated"] is False


def test_information_block_reports_a_total_that_does_not_foot(loaded):
  out = tools.information_block(loaded, "income statement", whole=False)
  assert out["block"]["kind"] == "income_statement"
  assert out["block"]["block_type"] == "income_statement"  # the producer's word
  assert out["block"]["level"] == "statement"
  assert "siblings" not in out["block"]
  (calc,) = out["calculation"]
  assert calc["checked"] == 2 and calc["foots"] == 1
  diff = calc["differences"]["2023-01-01..2023-12-31"]
  assert diff == {"reported": 1401.0, "computed": 400.0, "difference": 1001.0}
  # A reported label under the arc's preferred role.
  revenue = next(r for r in out["rows"] if r["concept"] == "us-gaap:Revenues")
  assert revenue["label"] == "Total revenues"


def test_information_block_points_at_the_text_blocks(loaded):
  out = tools.information_block(loaded, "Leases (Tables)", whole=False)
  assert out["block"]["level"] == "tables"
  (entry,) = out["text"]
  assert entry["concept"] == "us-gaap:LeaseCostTableTextBlock"
  assert (
    entry["preview"].startswith("Operating lease cost")
    or "Operating lease cost" in entry["preview"]
  )
  assert entry["chars"] > 50
  # The offset indexes the text read_text pages from.
  assert (
    loaded.block_text[entry["offset"] :]
    .lstrip()
    .startswith(entry["preview"][:20].split("\n")[0].strip()[:10])
  )
  note = tools.information_block(loaded, "0000010 - Disclosure - Leases", whole=False)
  assert note["block"]["level"] == "note"
  assert note["text"][0]["concept"] == "us-gaap:LesseeOperatingLeasesTextBlock"


def test_information_block_member_and_period_filters(loaded):
  widgets = tools.information_block(loaded, "Lease Cost", member="widgets", whole=False)
  operating = next(
    r for r in widgets["rows"] if r["concept"] == "us-gaap:OperatingLeaseCost"
  )
  assert list(operating["members"]) == ["acme:WidgetsMember"]
  assert [m["member"] for m in widgets["axes"][0]["members"]] == ["acme:WidgetsMember"]

  one_year = tools.information_block(
    loaded, "Lease Cost", periods=["2024"], whole=False
  )
  assert [c["key"] for c in one_year["columns"]] == ["2024-01-01..2024-12-31"]
  operating = next(
    r for r in one_year["rows"] if r["concept"] == "us-gaap:OperatingLeaseCost"
  )
  assert operating["values"] == {"2024-01-01..2024-12-31": 100.0}

  capped = tools.information_block(loaded, "Lease Cost", max_members=1, whole=False)
  assert capped["members_omitted"] == 2
  operating = next(
    r for r in capped["rows"] if r["concept"] == "us-gaap:OperatingLeaseCost"
  )
  assert list(operating["members"]) == ["acme:WidgetsMember"]
  assert operating["members_omitted"] == 1
  # A row reported only on a dropped member keeps its most-reported one.
  short = next(
    r for r in capped["rows"] if r["concept"] == "us-gaap:ShortTermLeaseCost"
  )
  assert short["members"] == {"acme:GizmosMember": {"2024-01-01..2024-12-31": 15.0}}
  assert "members_omitted" not in short
  # Left to the budget, a small table is never cut at all.
  whole = tools.information_block(loaded, "Lease Cost", whole=False)
  assert "members_omitted" not in whole
  assert all("members_omitted" not in r for r in whole["rows"])


def test_information_block_folds_a_suffixed_role_into_the_block(loaded):
  out = tools.information_block(loaded, "Maturities", whole=False)
  assert out["block"]["merged_roles"] == [MATURITY_ROLE + "_1"]
  (calc,) = out["calculation"]
  assert calc["total"] == "us-gaap:LesseeOperatingLeaseLiabilityPaymentsDue"
  assert calc["checked"] == 1 and calc["foots"] == 1
  # The suffixed role is not a section of its own.
  assert not any(s["id"].endswith("_1") for s in out["block"]["siblings"])


def test_a_suffixed_role_folds_only_when_it_repeats_the_definition(model):
  from xbrlkit.information_block import role_folds

  # The cube's definition arcs move into a suffixed drawer: still this block's cube.
  moved = model.model_copy(
    update={
      "networks": [
        n.model_copy(update={"role_uri": COST_ROLE + "_1"})
        if n.kind == "definition" and n.role_uri == COST_ROLE
        else n
        for n in model.networks
      ]
    }
  )
  cost = next(b for b in plan_blocks(moved) if b.role_uri == COST_ROLE)
  assert cost.merged_roles == [COST_ROLE + "_1"] and len(cost.hypercubes) == 1
  assert role_folds(moved)[COST_ROLE + "_1"] == COST_ROLE

  # A different definition under the suffix is a section of its own.
  other = model.model_copy(
    update={
      "networks": [
        n.model_copy(
          update={
            "role_uri": COST_ROLE + "_1",
            "definition": "0000099 - Disclosure - Other",
          }
        )
        if n.kind == "definition" and n.role_uri == COST_ROLE
        else n
        for n in model.networks
      ]
    }
  )
  cost = next(b for b in plan_blocks(other) if b.role_uri == COST_ROLE)
  assert cost.merged_roles == [] and cost.hypercubes == []
  assert role_folds(other)[COST_ROLE + "_1"] == COST_ROLE + "_1"


def test_member_budget_bounds_a_large_cube(model):
  many = model.model_copy(deep=True)
  arcs = next(
    n for n in many.networks if n.kind == "definition" and n.role_uri == COST_ROLE
  ).arcs
  for i in range(700):
    q = f"acme:M{i}Member"
    many.concepts[q] = Concept(
      qname=q, namespace=ACME, name=f"M{i}Member", is_domain_member=True
    )
    arcs.append(
      Arc(from_qname="us-gaap:SegmentDomain", to_qname=q, arcrole=DIM_DOMAIN_MEMBER)
    )
    many.facts.append(
      XbrlFact(
        id=f"x{i}",
        concept_qname="us-gaap:OperatingLeaseCost",
        period_id="D-2024",
        unit_id="usd",
        entity_cik="0001234567",
        dims=[DimQualifier(axis_qname=SEGMENT_AXIS, member_qname=q)],
        value_str="1",
        numeric_value=1.0,
        decimals="0",
        value_kind="numeric",
      )
    )
  text, sections = build_text(many, html=None)
  lf = LoadedFiling(
    id="many", source="memory", model=many, text=text, sections=sections
  )

  out = tools.information_block(lf, "Lease Cost", whole=False)
  operating = next(
    r for r in out["rows"] if r["concept"] == "us-gaap:OperatingLeaseCost"
  )
  shown = len(operating["members"])
  # 703 member keys in the block, 702 of them on this row; the budget keeps
  # the most-reported first and the row says how many it lost.
  assert 0 < shown < 700
  assert out["members_omitted"] == 702 - shown
  assert operating["members_omitted"] == 702 - shown

  # An explicit ceiling still applies, and stops at the hard maximum.
  capped = tools.information_block(lf, "Lease Cost", max_members=1000, whole=False)
  operating = next(
    r for r in capped["rows"] if r["concept"] == "us-gaap:OperatingLeaseCost"
  )
  assert len(operating["members"]) == 199  # 200 kept, one of them on another row


def test_information_block_without_a_cube_shows_consolidated_only(loaded):
  out = tools.information_block(loaded, "Maturities", whole=False)
  assert "axes" not in out
  total = next(
    r
    for r in out["rows"]
    if r["concept"] == "us-gaap:LesseeOperatingLeaseLiabilityPaymentsDue"
  )
  assert total["values"] == {"2024-12-31": 130.0}
  assert "members" not in total


def test_pure_profile_reads_the_filers_words_only(loaded):
  out = tools.information_block(
    loaded, "Consolidated Statements of Income", pure=True, whole=False
  )
  assert "kind" not in out["block"]
  assert out["block"]["level"] == "statement"  # the filer's own category
  assert "duration" not in out["columns"][0]
  fam = tools.disclosures(loaded, "leases", pure=True)
  assert all("kind" not in b for b in fam["blocks"])


@pytest.mark.asyncio
async def test_server_registers_the_section_tools(loaded: LoadedFiling, tmp_path: Path):
  from mcp.client import Client

  from xbrlkit.serve import build_server

  session = FilingSession()
  session._filings[loaded.id] = loaded
  server = build_server(session, tmp_path)
  try:
    async with Client(server) as client:
      names = {t.name for t in (await client.list_tools()).tools}
      assert {"disclosures", "information_block"} <= names
      listed = json.loads((await client.call_tool("disclosures", {})).content[0].text)
      assert listed["count"] >= 4
      block = json.loads(
        (
          await client.call_tool(
            "information_block", {"block": "Lease Cost", "member": "gadgets"}
          )
        )
        .content[0]
        .text
      )
      assert block["block"]["disclosure"] == "Leases"
      assert [m["member"] for m in block["axes"][0]["members"]] == [
        "acme:GadgetsMember"
      ]
      missing = json.loads(
        (await client.call_tool("information_block", {"block": "goodwill"}))
        .content[0]
        .text
      )
      assert "error" in missing
  finally:
    session.close()
