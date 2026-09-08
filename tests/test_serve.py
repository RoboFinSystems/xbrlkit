"""Tests for the local MCP server (``serve/``).

The tool functions are exercised against a hand-authored model — an income
statement with a calculation network, a balance sheet, a segment breakdown,
a duplicate-precision fact and a text block — so every rule the tools carry
(consolidated by default, most precise duplicate, presentation order,
preferred labels, period keys, section offsets) is checked without Arelle
or the network. The MCP layer is checked in-process through the SDK's own
client. A real filing is loaded only when ``XBRLKIT_TEST_FILING`` names one.
"""

from __future__ import annotations

import json
import os
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
from xbrlkit.serve import FilingSession, LoadedFiling, SourceError, build_text
from xbrlkit.serve import tools
from xbrlkit.serve.session import _find_load_target, _locate

US_GAAP = "http://fasb.org/us-gaap/2024-01-31"
IS_ROLE = "http://acme.example/role/StatementOfIncome"
BS_ROLE = "http://acme.example/role/BalanceSheet"
NOTE_ROLE = "http://acme.example/role/RevenueDisclosure"
NEGATED = "http://www.xbrl.org/2009/role/negatedLabel"
TOTAL = "http://www.xbrl.org/2003/role/totalLabel"


def _concept(name: str, **kw) -> Concept:
  base = dict(
    qname=f"us-gaap:{name}",
    namespace=US_GAAP,
    name=name,
    period_type="duration",
    is_numeric=True,
    item_type="monetaryItemType",
    nice_type="Monetary",
    pref_label=" ".join(_split_camel(name)),
  )
  base.update(kw)
  return Concept(**base)


def _split_camel(name: str) -> list[str]:
  out, cur = [], ""
  for ch in name:
    if ch.isupper() and cur:
      out.append(cur)
      cur = ch
    else:
      cur += ch
  out.append(cur)
  return out


def _model() -> XbrlModel:
  filing = FilingMeta(
    accession="0000000000-25-000001",
    cik="0001234567",
    form="10-K",
    filing_date=date(2025, 2, 1),
    report_date=date(2024, 12, 31),
    fiscal_year_focus="2024",
    fiscal_period_focus="FY",
    taxonomy_namespaces=[US_GAAP],
  )
  entity = EntityIdentity(cik="0001234567", name="Acme Corp", ticker="ACME")
  concepts = {
    "us-gaap:IncomeStatementAbstract": _concept(
      "IncomeStatementAbstract", is_abstract=True, is_numeric=False, item_type=None
    ),
    "us-gaap:Revenues": _concept(
      "Revenues",
      balance="credit",
      labels=[
        Label(value="Revenues", role="http://www.xbrl.org/2003/role/label"),
        Label(value="Total revenues", role=TOTAL),
        Label(
          value="Amount of revenue recognized from goods sold and services rendered.",
          role="http://www.xbrl.org/2003/role/documentation",
        ),
      ],
    ),
    "us-gaap:CostOfRevenue": _concept(
      "CostOfRevenue",
      balance="debit",
      labels=[
        Label(value="Cost of revenue", role="http://www.xbrl.org/2003/role/label"),
        Label(value="Less: cost of revenue", role=NEGATED),
      ],
    ),
    "us-gaap:GrossProfit": _concept("GrossProfit", balance="credit"),
    "us-gaap:Assets": _concept("Assets", period_type="instant", balance="debit"),
    "us-gaap:Cash": _concept(
      "CashAndCashEquivalentsAtCarryingValue",
      qname="us-gaap:Cash",
      period_type="instant",
      balance="debit",
      pref_label="Cash",
    ),
    "us-gaap:RevenueRecognitionPolicyTextBlock": _concept(
      "RevenueRecognitionPolicyTextBlock",
      is_numeric=False,
      is_textblock=True,
      item_type="textBlockItemType",
      nice_type="Text Block",
      pref_label="Revenue Recognition Policy",
    ),
    "us-gaap:StatementBusinessSegmentsAxis": _concept(
      "StatementBusinessSegmentsAxis", is_dimension_item=True, is_numeric=False
    ),
    "acme:WidgetsMember": Concept(
      qname="acme:WidgetsMember",
      namespace="http://acme.example/20241231",
      name="WidgetsMember",
      is_domain_member=True,
      pref_label="Widgets",
    ),
  }
  periods = [
    Period(
      id="D-2024",
      period_type="duration",
      start=date(2024, 1, 1),
      end=date(2024, 12, 31),
      duration_type="annual",
      calendar_year=2024,
      calendar_period_key="2024",
    ),
    Period(
      id="D-2023",
      period_type="duration",
      start=date(2023, 1, 1),
      end=date(2023, 12, 31),
      duration_type="annual",
      calendar_year=2023,
      calendar_period_key="2023",
    ),
    Period(id="I-2024", period_type="instant", end=date(2024, 12, 31)),
    Period(id="I-2023", period_type="instant", end=date(2023, 12, 31)),
  ]
  units = [Unit(id="usd", measure="iso4217:USD")]
  segment = [
    DimQualifier(
      axis_qname="us-gaap:StatementBusinessSegmentsAxis",
      member_qname="acme:WidgetsMember",
    )
  ]

  def fact(fid, qname, pid, value, decimals="-3", dims=(), unit="usd"):
    return XbrlFact(
      id=fid,
      concept_qname=qname,
      period_id=pid,
      unit_id=unit,
      entity_cik="0001234567",
      dims=list(dims),
      value_str=str(value),
      numeric_value=float(value),
      decimals=decimals,
      value_kind="numeric",
    )

  facts = [
    fact("f1", "us-gaap:Revenues", "D-2024", 1_000_000, decimals="-6"),
    # The same total tagged again in a note at a finer precision — this
    # one is the value to report.
    fact("f1b", "us-gaap:Revenues", "D-2024", 1_000_450, decimals="-3"),
    fact("f2", "us-gaap:Revenues", "D-2023", 900_000),
    fact("f3", "us-gaap:CostOfRevenue", "D-2024", 600_000),
    fact("f4", "us-gaap:CostOfRevenue", "D-2023", 500_000),
    fact("f5", "us-gaap:GrossProfit", "D-2024", 400_450),
    fact("f6", "us-gaap:GrossProfit", "D-2023", 400_000),
    fact("f7", "us-gaap:Assets", "I-2024", 5_000_000),
    fact("f8", "us-gaap:Assets", "I-2023", 4_500_000),
    fact("f9", "us-gaap:Cash", "I-2024", 1_250_000),
    fact("f10", "us-gaap:Revenues", "D-2024", 700_000, dims=segment),
    XbrlFact(
      id="t1",
      concept_qname="us-gaap:RevenueRecognitionPolicyTextBlock",
      period_id="D-2024",
      entity_cik="0001234567",
      value_str=(
        "<div><p>Revenue is recognized when control of the promised goods "
        "transfers to the customer, in an amount that reflects the "
        "consideration the company expects to receive.</p><p>Widget sales are "
        "recognized at a point in time; service revenue over the contract "
        "term. Contract liabilities were $12,000 at year end.</p></div>"
      ),
      value_kind="text",
    ),
  ]
  networks = [
    Network(
      role_uri=IS_ROLE,
      definition="1001 - Statement - Consolidated Statements of Income",
      kind="presentation",
      arcs=[
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
          preferred_label=NEGATED,
        ),
        Arc(
          from_qname="us-gaap:IncomeStatementAbstract",
          to_qname="us-gaap:GrossProfit",
          order=3,
        ),
      ],
    ),
    Network(
      role_uri=IS_ROLE,
      definition="1001 - Statement - Consolidated Statements of Income",
      kind="calculation",
      arcs=[
        Arc(
          from_qname="us-gaap:GrossProfit",
          to_qname="us-gaap:Revenues",
          order=1,
          weight=1.0,
        ),
        Arc(
          from_qname="us-gaap:GrossProfit",
          to_qname="us-gaap:CostOfRevenue",
          order=2,
          weight=-1.0,
        ),
      ],
    ),
    Network(
      role_uri=BS_ROLE,
      definition="1002 - Statement - Consolidated Balance Sheets",
      kind="presentation",
      arcs=[
        Arc(from_qname="us-gaap:Assets", to_qname="us-gaap:Cash", order=1),
      ],
    ),
    Network(
      role_uri=NOTE_ROLE,
      definition="2001 - Disclosure - Revenue (Policies)",
      kind="presentation",
      arcs=[
        Arc(
          from_qname="us-gaap:RevenueRecognitionPolicyTextBlock",
          to_qname="us-gaap:Revenues",
          order=1,
        ),
      ],
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
def loaded() -> LoadedFiling:
  model = _model()
  text, sections = build_text(model, html=None)
  return LoadedFiling(
    id="acme", source="memory", model=model, text=text, sections=sections
  )


@pytest.fixture
def session(loaded: LoadedFiling) -> FilingSession:
  s = FilingSession()
  s._filings[loaded.id] = loaded
  yield s
  s.close()


# -- describe -------------------------------------------------------------------


def test_describe_filing_orients(loaded: LoadedFiling) -> None:
  out = tools.describe_filing(loaded)
  assert out["entity"]["name"] == "Acme Corp"
  assert out["filing"]["form"] == "10-K"
  assert out["counts"]["facts"] == 12
  assert out["counts"]["text_blocks"] == 1
  assert out["counts"]["dimensional_facts"] == 1
  assert out["counts"]["networks"] == {
    "presentation": 3,
    "calculation": 1,
    "definition": 0,
  }
  keys = [p["key"] for p in out["periods"]]
  assert "2024-01-01..2024-12-31" in keys and "2024-12-31" in keys
  kinds = {s["role"]: s["kind"] for s in out["statements"]}
  assert kinds == {IS_ROLE: "income_statement", BS_ROLE: "balance_sheet"}
  assert [s["id"] for s in out["statements"]] == ["StatementOfIncome", "BalanceSheet"]
  assert out["statements"][0]["name"] == "Consolidated Statements of Income"
  assert out["disclosures"] == [
    {"id": "RevenueDisclosure", "name": "Revenue (Policies)", "concepts": 2}
  ]
  assert out["periods"][0] == {"key": "2024-12-31", "instant": True, "facts": 2}
  assert out["axes"][0]["axis"] == "us-gaap:StatementBusinessSegmentsAxis"
  assert out["axes"][0]["members"] == 1
  blocks = out["sections"]["text_blocks"]
  assert blocks[0]["id"] == "us-gaap:RevenueRecognitionPolicyTextBlock"
  assert blocks[0]["offset"] is not None


# -- resolve --------------------------------------------------------------------


def test_resolve_element_ranks_exact_then_prefix(loaded: LoadedFiling) -> None:
  out = tools.resolve_element(loaded, "revenue")
  qnames = [m["qname"] for m in out["matches"]]
  assert qnames[0] == "us-gaap:Revenues"
  assert "us-gaap:CostOfRevenue" in qnames
  top = out["matches"][0]
  assert top["facts"] == 4
  assert top["balance"] == "credit"
  assert any("Income" in name for name in top["in"])
  assert top["documentation"].startswith("Amount of revenue")


def test_resolve_element_by_qname_and_label(loaded: LoadedFiling) -> None:
  assert (
    tools.resolve_element(loaded, "us-gaap:Assets")["matches"][0]["qname"]
    == "us-gaap:Assets"
  )
  out = tools.resolve_element(loaded, "cash equivalents")
  assert out["matches"][0]["qname"] == "us-gaap:Cash"
  with pytest.raises(tools.ToolError):
    tools.resolve_element(loaded, "  ")


# -- fact grid ------------------------------------------------------------------


def test_fact_grid_is_consolidated_and_most_precise(loaded: LoadedFiling) -> None:
  out = tools.fact_grid(loaded, ["us-gaap:Revenues"])
  rows = out["rows"]
  # Two annual periods, the segment fact excluded, the -3 duplicate kept.
  assert [r["period"] for r in rows] == [
    "2024-01-01..2024-12-31",
    "2023-01-01..2023-12-31",
  ]
  assert rows[0]["value"] == 1_000_450
  assert rows[0]["decimals"] == "-3"
  assert rows[0]["unit"] == "iso4217:USD"
  assert rows[0]["duration"] == "annual"
  assert "dims" not in rows[0]
  assert out["resolved"] == ["us-gaap:Revenues"]


def test_fact_grid_dimensions_and_filters(loaded: LoadedFiling) -> None:
  dims = tools.fact_grid(loaded, ["Revenues"], include_dimensions=True)
  # The consolidated totals and the breakdown side by side; the breakdown
  # sorts after the total of its period.
  assert dims["row_count"] == 3
  assert [r.get("dims") is not None for r in dims["rows"]] == [False, True, False]
  assert dims["rows"][1]["value"] == 700_000
  assert dims["rows"][1]["dims"][0]["member"] == "acme:WidgetsMember"
  by_member = tools.fact_grid(loaded, ["Revenues"], member="widgets")
  assert by_member["row_count"] == 1
  year = tools.fact_grid(loaded, ["Revenues", "GrossProfit"], period_end="2023")
  assert {r["concept"] for r in year["rows"]} == {
    "us-gaap:Revenues",
    "us-gaap:GrossProfit",
  }
  assert all(r["period"].endswith("2023-12-31") for r in year["rows"])
  instants = tools.fact_grid(loaded, ["Assets", "Revenues"], period_type="instant")
  assert {r["concept"] for r in instants["rows"]} == {"us-gaap:Assets"}
  exact = tools.fact_grid(loaded, ["Assets"], period_end="2024-12-31")
  assert exact["row_count"] == 1 and exact["rows"][0]["value"] == 5_000_000


def test_fact_grid_reports_unresolved(loaded: LoadedFiling) -> None:
  out = tools.fact_grid(loaded, ["us-gaap:Revenues", "Nonesuch"])
  assert out["unresolved"] == ["Nonesuch"]
  assert out["row_count"] == 2
  with pytest.raises(tools.ToolError):
    tools.fact_grid(loaded, ["Revenues"], period_end="last year")
  with pytest.raises(tools.ToolError):
    tools.fact_grid(loaded, [])


# -- statement ------------------------------------------------------------------


def test_statement_by_kind_renders_rows_in_order(loaded: LoadedFiling) -> None:
  out = tools.statement(loaded, "income statement")
  assert out["statement"]["role"] == IS_ROLE
  assert out["statement"]["kind"] == "income_statement"
  rows = out["rows"]
  assert [r["concept"] for r in rows] == [
    "us-gaap:IncomeStatementAbstract",
    "us-gaap:Revenues",
    "us-gaap:CostOfRevenue",
    "us-gaap:GrossProfit",
  ]
  assert rows[0]["abstract"] is True and "values" not in rows[0]
  assert rows[1]["label"] == "Total revenues"  # the arc's preferred label
  assert rows[2]["label"] == "Less: cost of revenue"
  assert rows[1]["depth"] == 1
  assert rows[1]["values"]["2024-01-01..2024-12-31"] == 1_000_450
  assert [c["key"] for c in out["columns"]] == [
    "2024-01-01..2024-12-31",
    "2023-01-01..2023-12-31",
  ]


def test_statement_columns_put_the_year_before_its_fourth_quarter() -> None:
  model = _model()
  model.periods.append(
    Period(
      id="Q4-2024",
      period_type="duration",
      start=date(2024, 10, 1),
      end=date(2024, 12, 31),
      duration_type="quarterly",
    )
  )
  model.facts.append(
    XbrlFact(
      id="q4",
      concept_qname="us-gaap:Revenues",
      period_id="Q4-2024",
      unit_id="usd",
      entity_cik="0001234567",
      value_str="250000",
      numeric_value=250_000.0,
      decimals="-3",
    )
  )
  text, sections = build_text(model, html=None)
  lf = LoadedFiling(
    id="acme", source="memory", model=model, text=text, sections=sections
  )
  keys = [c["key"] for c in tools.statement(lf, "income statement")["columns"]]
  assert keys[:2] == ["2024-01-01..2024-12-31", "2024-10-01..2024-12-31"]


def test_income_taxes_note_is_not_an_income_statement() -> None:
  model = _model()
  model.networks.append(
    Network(
      role_uri="http://acme.example/role/IncomeTaxes",
      definition="2002 - Disclosure - Income Taxes",
      kind="presentation",
      arcs=[
        Arc(from_qname="us-gaap:IncomeStatementAbstract", to_qname="us-gaap:Revenues")
      ],
    )
  )
  model.networks.append(
    Network(
      role_uri="http://acme.example/role/ComprehensiveIncome",
      definition="1003 - Statement - Consolidated Statements of Comprehensive Income",
      kind="presentation",
      arcs=[
        Arc(from_qname="us-gaap:IncomeStatementAbstract", to_qname="us-gaap:Revenues")
      ],
    )
  )
  text, sections = build_text(model, html=None)
  lf = LoadedFiling(
    id="acme", source="memory", model=model, text=text, sections=sections
  )
  described = tools.describe_filing(lf)
  assert {s["id"] for s in described["disclosures"]} >= {"IncomeTaxes"}
  assert "IncomeTaxes" not in {s["id"] for s in described["statements"]}
  # The plain income statement beats the comprehensive one and the note.
  assert tools.statement(lf, "income statement")["statement"]["role"] == IS_ROLE
  assert tools.statement(lf, "IncomeTaxes")["statement"]["name"].endswith(
    "Income Taxes"
  )
  assert tools.statement(lf, "comprehensive income")["statement"]["role"].endswith(
    "ComprehensiveIncome"
  )


def test_statement_by_role_name_and_period_filter(loaded: LoadedFiling) -> None:
  out = tools.statement(loaded, "Balance Sheets", periods=["2024"])
  assert out["statement"]["role"] == BS_ROLE
  assert [c["key"] for c in out["columns"]] == ["2024-12-31"]
  assert out["rows"][1]["concept"] == "us-gaap:Cash"
  assert out["rows"][1]["values"] == {"2024-12-31": 1_250_000}
  note = tools.statement(loaded, NOTE_ROLE)
  assert note["rows"][0]["values"]["2024-01-01..2024-12-31"].startswith("[text ")
  with pytest.raises(tools.ToolError):
    tools.statement(loaded, "no such network")


# -- calculation ----------------------------------------------------------------


def test_calculation_rolls_children_up(loaded: LoadedFiling) -> None:
  out = tools.calculation(loaded, "GrossProfit")
  assert out["concept"] == "us-gaap:GrossProfit"
  net = out["networks"][0]
  assert [c["weight"] for c in net["children"]] == [1.0, -1.0]
  latest = net["periods"][0]
  assert latest["period"] == "2024-01-01..2024-12-31"
  assert latest["reported"] == 400_450
  assert latest["computed"] == 1_000_450 - 600_000
  assert latest["difference"] == 0
  assert "missing" not in latest


def test_calculation_child_reports_parents(loaded: LoadedFiling) -> None:
  out = tools.calculation(loaded, "us-gaap:Revenues")
  assert out["networks"] == []
  assert "us-gaap:GrossProfit" in out["note"]
  with pytest.raises(tools.ToolError):
    tools.calculation(loaded, "Nonesuch")


# -- text -----------------------------------------------------------------------


def test_text_from_text_blocks_when_no_document(loaded: LoadedFiling) -> None:
  assert loaded.text.startswith("## us-gaap:RevenueRecognitionPolicyTextBlock")
  assert "<p>" not in loaded.text
  section = loaded.sections[0]
  assert section.kind == "text_block"
  assert loaded.text[section.offset : section.offset + 7] == "Revenue"


def test_search_and_read_text(loaded: LoadedFiling) -> None:
  out = tools.search_text(loaded, r"contract liabilit\w+", window=80)
  assert out["total"] == 1
  hit = out["hits"][0]
  assert hit["match"].lower() == "contract liabilities"
  assert "$12,000" in hit["text"]
  assert hit["section"] == "Revenue Recognition Policy"
  page = tools.read_text(loaded, offset=hit["offset"], length=30)
  assert page["text"].startswith("Contract liabilities")
  assert page["next_offset"] == hit["offset"] + 30
  with pytest.raises(tools.ToolError):
    tools.search_text(loaded, "(")
  with pytest.raises(tools.ToolError):
    tools.read_text(loaded, offset=10**6)


def test_build_text_from_inline_document() -> None:
  model = _model()
  html = (
    "<html><body><div>Cover page. UNITED STATES SECURITIES AND EXCHANGE COMMISSION"
    "</div><div>Item 1. Business</div><p>Acme makes widgets for the world and "
    "sells them through distributors in forty countries.</p>"
    '<div><ix:nonNumeric name="us-gaap:RevenueRecognitionPolicyTextBlock" '
    'contextRef="c1"><p>Revenue is recognized when control of the promised goods '
    "transfers to the customer, in an amount that reflects the consideration the "
    "company expects to receive, and the policy runs to more than twenty words "
    "so the parser keeps it.</p></ix:nonNumeric></div>"
    "<div>Item 7. Management's Discussion and Analysis</div><p>Revenue grew "
    "eleven percent on widget volume; gross margin held at forty percent. "
    "Nothing else changed in the year that management would call material."
    "</p><div>Item 8. Financial Statements</div><p>See the notes.</p>"
    "</body></html>"
  )
  text, sections = build_text(model, html)
  assert "Acme makes widgets" in text
  blocks = [s for s in sections if s.kind == "text_block"]
  assert blocks and blocks[0].id == "us-gaap:RevenueRecognitionPolicyTextBlock"
  assert blocks[0].offset is not None
  assert text[blocks[0].offset :].startswith("Revenue is recognized")


def test_locate_matches_across_renderings() -> None:
  text = (
    "| NOTE 19. Commitments and Contingencies | 85 |\n| NOTE 20. Stock | 90 |\n"
    "\nNOTE 19. Commitments and Contingencies\nWarranties/Guarantees: 3M\u2019s accrued "
    "product warranty liabilities, recorded on the balance sheet"
  )
  block = (
    "NOTE 19. Commitments and Contingencies \n\n Warranties/Guarantees : 3M s accrued "
    "product warranty liabilities, recorded on the balance sheet"
  )
  # The table-of-contents row does not match; the section itself does.
  assert _locate(text, block) == text.index("\nNOTE 19.") + 1
  assert _locate(text, "nothing here at all whatsoever") is None
  assert _locate(text, "NOTE 19. Commitments") is None  # too short to trust


def test_locate_skips_a_contents_row_that_carries_the_whole_heading() -> None:
  heading = (
    "Item 7. Management's Discussion and Analysis of Financial Condition and "
    "Results of Operations"
  )
  text = (
    f"| {heading} | 33 |\n| Item 7A. Quantitative and Qualitative Disclosures "
    f"About Market Risk | 60 |\n\nItem 1. Business\nAcme makes widgets.\n\n{heading}\n"
    "Overview\nThe following discussion should be read together with the "
    "consolidated financial statements."
  )
  section = f"{heading}\n\nOverview\n\nThe following discussion should be read together"
  assert _locate(text, section) == text.index(f"\n{heading}\nOverview") + 1


# -- export ---------------------------------------------------------------------


@pytest.mark.parametrize("fmt", ["holon", "tavi", "oim"])
def test_export_filing_writes_projection(
  loaded: LoadedFiling, tmp_path: Path, fmt: str
) -> None:
  out = tools.export_filing(loaded, fmt, tmp_path)
  path = Path(out["path"])
  assert path.parent == tmp_path and path.is_file() and out["bytes"] > 0
  json.loads(path.read_text())
  if fmt == "tavi":
    assert len(out["files"]) == 2
  with pytest.raises(tools.ToolError):
    tools.export_filing(loaded, "pdf", tmp_path)


# -- session --------------------------------------------------------------------


def test_session_get_by_id_ticker_and_default(
  session: FilingSession, loaded: LoadedFiling
) -> None:
  assert session.get() is loaded
  assert session.get("acme") is loaded
  assert session.get("ACME") is loaded
  assert session.get(loaded.accession) is loaded
  with pytest.raises(SourceError):
    session.get("other")
  session._filings["second"] = loaded
  with pytest.raises(SourceError):
    session.get()
  assert tools.list_filings(session)["count"] == 2


def test_session_rejects_unresolvable_source(session: FilingSession) -> None:
  with pytest.raises(SourceError):
    session.load("")
  with pytest.raises(SourceError):
    session.load("not a source at all !!")


def test_find_load_target_recognises_an_instance_by_its_root(tmp_path: Path) -> None:
  # A RoboLedger-style package: nothing named after the schema, the
  # linkbases hyphenated, the instance called instance.xml.
  (tmp_path / "report.xsd").write_text("<xs:schema/>")
  for name in ("report-pre.xml", "report-cal.xml", "report-lab.xml"):
    (tmp_path / name).write_text(
      '<?xml version="1.0"?><link:linkbase xmlns:link="http://www.xbrl.org/2003/linkbase"/>'
    )
  (tmp_path / "instance.xml").write_text(
    '<?xml version="1.0"?>\n<xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance">'
    "</xbrli:xbrl>"
  )
  assert _find_load_target(tmp_path).name == "instance.xml"
  # Wrapped in one directory, the same answer.
  inner = tmp_path / "wrapped"
  inner.mkdir()
  for f in list(tmp_path.iterdir()):
    if f.is_file():
      f.rename(inner / f.name)
  assert _find_load_target(tmp_path).name == "instance.xml"


def test_find_load_target_prefers_inline_document(tmp_path: Path) -> None:
  (tmp_path / "acme-20241231.xsd").write_text("<schema/>")
  (tmp_path / "acme-20241231_cal.xml").write_text("<linkbase/>")
  (tmp_path / "exhibit.htm").write_text("<html>not inline</html>")
  (tmp_path / "acme-20241231.htm").write_text(
    '<html><ix:header/><ix:nonNumeric name="x"/></html>'
  )
  assert _find_load_target(tmp_path).name == "acme-20241231.htm"
  (tmp_path / "acme-20241231.htm").unlink()
  (tmp_path / "exhibit.htm").unlink()
  (tmp_path / "acme-20241231.xml").write_text(
    "<xbrl xmlns='http://www.xbrl.org/2003/instance'/>"
  )
  assert _find_load_target(tmp_path).name == "acme-20241231.xml"


# -- the pure profile and the document toggle -------------------------------------


def _loaded_with_document() -> LoadedFiling:
  """A filing that holds both renderings: a primary document and its blocks."""
  model = _model()
  from xbrlkit.serve.session import _text_from_text_blocks

  block_text, block_sections = _text_from_text_blocks(model)
  document = (
    "Cover page. UNITED STATES SECURITIES AND EXCHANGE COMMISSION\n\n"
    "Item 7. Management's Discussion\nRevenue grew eleven percent on widget volume; "
    "the untagged body says contract liabilities were $12,000 at year end too.\n\n"
    + block_text
  )
  from xbrlkit.serve.session import TextSection

  sections = [TextSection(id="item_7", label="MD&A", kind="item", chars=120, offset=63)]
  sections += [
    TextSection(
      id=b.id,
      label=b.label,
      kind=b.kind,
      chars=b.chars,
      offset=(b.offset or 0) + document.index(block_text),
    )
    for b in block_sections
  ]
  return LoadedFiling(
    id="acme",
    source="memory",
    model=model,
    text=document,
    sections=sections,
    block_text=block_text,
    block_sections=block_sections,
    has_document=True,
  )


def test_pure_describe_carries_nothing_the_filing_does_not() -> None:
  lf = _loaded_with_document()
  product = tools.describe_filing(lf)
  pure = tools.describe_filing(lf, pure=True)
  assert product["profile"] == {
    "pure": False,
    "text": "primary document",
    "xbrl": True,
  }
  assert pure["profile"]["pure"] is True and pure["profile"]["xbrl"] is True
  # No kinds: every network is listed by the filer's own name, none flagged.
  assert "statements" not in pure and "kind" not in json.dumps(pure["networks"])
  assert {n["id"] for n in pure["networks"]} == {
    "StatementOfIncome",
    "BalanceSheet",
    "RevenueDisclosure",
  }
  # No duration buckets, no Items map.
  assert all("duration" not in p for p in pure["periods"])
  assert "items" not in pure["sections"]
  assert product["sections"]["items"][0]["id"] == "item_7"
  assert any("duration" in p for p in product["periods"])


def test_pure_statement_knows_only_the_filers_names(loaded: LoadedFiling) -> None:
  assert (
    tools.statement(loaded, "income statement")["statement"]["kind"]
    == "income_statement"
  )
  with pytest.raises(tools.ToolError):
    tools.statement(loaded, "income statement", pure=True)
  out = tools.statement(loaded, "Statements of Income", pure=True)
  assert out["statement"] == {
    "role": IS_ROLE,
    "name": "1001 - Statement - Consolidated Statements of Income",
  }
  assert all("duration" not in c and "calendar" not in c for c in out["columns"])
  assert (
    tools.statement(loaded, "StatementOfIncome", pure=True)["statement"]["role"]
    == IS_ROLE
  )


def test_pure_fact_grid_refuses_buckets_and_drops_them(loaded: LoadedFiling) -> None:
  with pytest.raises(tools.ToolError):
    tools.fact_grid(loaded, ["Revenues"], period_type="annual", pure=True)
  rows = tools.fact_grid(loaded, ["Revenues"], period_type="duration", pure=True)[
    "rows"
  ]
  assert rows and all("duration" not in r for r in rows)
  assert "duration" in tools.fact_grid(loaded, ["Revenues"])["rows"][0]
  exact = tools.fact_grid(loaded, ["Revenues"], period_end="2024-12-31", pure=True)
  assert exact["row_count"] == 1


def test_document_toggle_selects_the_text(loaded: LoadedFiling) -> None:
  lf = _loaded_with_document()
  whole = tools.search_text(lf, "contract liabilities")
  blocks = tools.search_text(lf, "contract liabilities", whole=False)
  assert whole["total"] == 2 and blocks["total"] == 1
  assert whole["text_chars"] > blocks["text_chars"]
  assert whole["hits"][0]["section"] == "MD&A"
  # The pure profile drops the section label; a filing without a document
  # reads its blocks whichever way it is asked.
  assert "section" not in tools.search_text(lf, "contract", pure=True)["hits"][0]
  assert loaded.has_document is False
  assert tools.search_text(loaded, "contract", whole=True)["text_chars"] == len(
    loaded.block_text
  )


def test_pure_read_text_uses_the_ladders_cap(loaded: LoadedFiling) -> None:
  start = loaded.sections[0].offset or 0
  product = tools.read_text(loaded, offset=start, length=8000)
  pure = tools.read_text(loaded, offset=start, length=8000, pure=True)
  assert product["length"] == len(loaded.text) - start  # the fixture is short
  assert "section" in product and "section" not in pure
  long_lf = LoadedFiling(
    id="long",
    source="memory",
    model=loaded.model,
    text="x" * 10_000,
    sections=[],
    block_text="x" * 10_000,
  )
  assert tools.read_text(long_lf, length=8000)["length"] == 8000
  assert tools.read_text(long_lf, length=8000, pure=True)["length"] == 4000


def test_model_export_reloads_without_arelle(
  loaded: LoadedFiling, tmp_path: Path
) -> None:
  out = tools.export_filing(loaded, "model", tmp_path)
  path = Path(out["path"])
  assert path.name == "acme.model.json"
  session = FilingSession()
  try:
    again = session.load(str(path))
    assert again.model.model_dump() == loaded.model.model_dump()
    assert again.has_document is False
    assert again.id == loaded.accession  # accession-shaped, so it is the id
    assert tools.fact_grid(again, ["Revenues"])["rows"][0]["value"] == 1_000_450
    bad = tmp_path / "not-a-model.json"
    bad.write_text('{"hello": "world"}')
    with pytest.raises(SourceError, match="not a JSON file xbrlkit recognises"):
      session.load(str(bad))
    tavi = tmp_path / "x.tavi.json"
    tavi.write_text(
      '{"documentInfo": {"documentType": "https://xbrl.org/PWD/2026-09-01/compiled"}}'
    )
    with pytest.raises(SourceError, match="Tavi compiled model"):
      session.load(str(tavi))
    holon = tmp_path / "x.holon.jsonld"
    holon.write_text('{"@context": {}, "@graph": []}')
    with pytest.raises(SourceError, match="holon"):
      session.load(str(holon))
    # An HTML document with no XBRL is not an error: most of EDGAR is one.
    notxbrl = tmp_path / "page.htm"
    notxbrl.write_text("<html><body>not a filing</body></html>")
    document = session.load(str(notxbrl))
    assert document.has_xbrl is False and document.has_document is True
  finally:
    session.close()


# -- the CLI --------------------------------------------------------------------


def test_serve_rejects_a_representation_not_in_this_release(capsys) -> None:
  from xbrlkit.cli import main

  assert main(["serve", "--as", "tavi"]) == 1
  err = capsys.readouterr().err
  assert "only `model`" in err and "holon" in err


def test_serve_parser_profile_flags() -> None:
  from xbrlkit.cli import build_parser

  args = build_parser().parse_args(["serve", "--pure"])
  assert (args.representation, args.pure, args.with_document) == ("model", True, None)
  args = build_parser().parse_args(["serve", "--pure", "--with-document"])
  assert args.with_document is True
  args = build_parser().parse_args(["serve", "--without-document", "--as", "model"])
  assert (args.pure, args.with_document) == (False, False)


def test_serve_without_the_extra_names_it(monkeypatch, capsys) -> None:
  import sys

  from xbrlkit.cli import main

  monkeypatch.setitem(sys.modules, "mcp", None)  # `import mcp` now raises
  assert main(["serve"]) == 1
  err = capsys.readouterr().err
  assert "xbrlkit[mcp]" in err and "uvx --from" in err


def test_serve_parser_shape() -> None:
  from xbrlkit.cli import build_parser

  args = build_parser().parse_args(
    ["serve", "NVDA", "./x.htm", "--port", "9000", "--transport", "stdio"]
  )
  assert args.sources == ["NVDA", "./x.htm"]
  assert (args.host, args.port, args.transport, args.path) == (
    "127.0.0.1",
    9000,
    "stdio",
    "/mcp",
  )


# -- the MCP layer --------------------------------------------------------------


@pytest.mark.asyncio
async def test_server_lists_and_calls_tools(
  session: FilingSession, tmp_path: Path
) -> None:
  from mcp.client import Client

  from xbrlkit.serve import build_server

  server = build_server(session, tmp_path)
  async with Client(server) as client:
    listed = await client.list_tools()
    names = {t.name for t in listed.tools}
    assert names == {
      "list_filings",
      "load_filing",
      "unload_filing",
      "describe_filing",
      "resolve_element",
      "fact_grid",
      "statement",
      "calculation",
      "search_text",
      "read_text",
      "records",
      "export_filing",
    }
    described = await client.call_tool("describe_filing", {})
    payload = json.loads(described.content[0].text)
    assert payload["entity"]["ticker"] == "ACME"
    grid = await client.call_tool(
      "fact_grid", {"elements": ["Revenues"], "period_end": "2024-12-31"}
    )
    rows = json.loads(grid.content[0].text)["rows"]
    assert rows[0]["value"] == 1_000_450
    bad = await client.call_tool("statement", {"statement": "no such"})
    assert "error" in json.loads(bad.content[0].text)
    exported = await client.call_tool("export_filing", {"format": "tavi"})
    assert Path(json.loads(exported.content[0].text)["path"]).parent == tmp_path
    assert "unload_filing" in names
    dropped = await client.call_tool("unload_filing", {"filing": "acme"})
    assert json.loads(dropped.content[0].text) == {"unloaded": "acme", "loaded": []}
    empty = await client.call_tool("list_filings", {})
    assert json.loads(empty.content[0].text)["count"] == 0


@pytest.mark.asyncio
async def test_pure_server_profile(loaded: LoadedFiling, tmp_path: Path) -> None:
  from mcp.client import Client

  from xbrlkit.serve import build_server

  session = FilingSession()
  session._filings[loaded.id] = loaded
  try:
    server = build_server(session, tmp_path, pure=True)
    async with Client(server) as client:
      listed = await client.list_tools()
      read = next(t for t in listed.tools if t.name == "read_text")
      assert read.input_schema["properties"]["length"]["maximum"] == 4000
      search = next(t for t in listed.tools if t.name == "search_text")
      assert "tagged text blocks" in search.description
      described = json.loads(
        (await client.call_tool("describe_filing", {})).content[0].text
      )
      assert described["profile"]["pure"] is True and "networks" in described
      grid = await client.call_tool(
        "fact_grid", {"elements": ["Revenues"], "period_type": "annual"}
      )
      assert "error" in json.loads(grid.content[0].text)
  finally:
    session.close()


# -- a real filing, when one is named --------------------------------------------


@pytest.mark.integration
@pytest.mark.skipif(
  not os.environ.get("XBRLKIT_TEST_FILING"), reason="XBRLKIT_TEST_FILING not set"
)
def test_real_filing_round_trip() -> None:
  session = FilingSession()
  try:
    lf = session.load(os.environ["XBRLKIT_TEST_FILING"])
    described = tools.describe_filing(lf)
    assert described["counts"]["facts"] > 100
    assert any(s.get("statement") == "balance_sheet" for s in described["statements"])
    revenue = tools.resolve_element(lf, "revenue")["matches"]
    assert revenue
    grid = tools.fact_grid(lf, [revenue[0]["qname"]])
    assert grid["row_count"] > 0
    assert tools.search_text(lf, "item 7")["total"] > 0
  finally:
    session.close()
