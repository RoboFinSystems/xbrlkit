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
import shutil
import textwrap
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


def test_load_receipt_confirms_without_the_map(loaded: LoadedFiling) -> None:
  """load_filing says what arrived; describe_filing draws the map. Returning
  the map from both made an agent following the instructions pay twice."""
  receipt = tools.load_receipt(loaded)
  full = tools.describe_filing(loaded)
  assert receipt["loaded"] == loaded.id
  assert receipt["entity"] == full["entity"]
  assert receipt["filing"] == full["filing"]
  assert receipt["counts"] == full["counts"]
  assert "describe_filing" in receipt["next"]
  for heavy in ("periods", "statements", "disclosures", "axes", "sections"):
    assert heavy not in receipt
  assert len(json.dumps(receipt)) < len(json.dumps(full))


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


def test_fact_grid_says_why_a_resolved_concept_has_no_row(
  loaded: LoadedFiling,
) -> None:
  """A balance-sheet concept under a duration bucket returns nothing: an
  instant has no duration. Silently, that reads as "the filer never tagged
  Assets" — the opposite of the truth."""
  out = tools.fact_grid(loaded, ["Assets", "Revenues"], period_type="annual")
  assert "us-gaap:Assets" in out["resolved"]
  assert not any(r["concept"] == "us-gaap:Assets" for r in out["rows"])
  (excluded,) = [e for e in out["excluded"] if e["concept"] == "us-gaap:Assets"]
  assert "instants" in excluded["reason"] and "annual" in excluded["reason"]
  assert "period_end" in excluded["try"]
  assert excluded["facts"] >= 1


def test_fact_grid_distinguishes_excluded_from_untagged(loaded: LoadedFiling) -> None:
  """`excluded` is what the filing reports and a filter removed; `unresolved`
  is what it never tagged. Conflating them is the whole defect."""
  out = tools.fact_grid(loaded, ["Assets", "Nonesuch"], period_type="annual")
  assert out["unresolved"] == ["Nonesuch"]
  assert [e["concept"] for e in out["excluded"]] == ["us-gaap:Assets"]


def test_fact_grid_is_quiet_when_every_concept_answers(loaded: LoadedFiling) -> None:
  assert "excluded" not in tools.fact_grid(loaded, ["Revenues"])


def test_fact_grid_explains_a_concept_that_carries_no_fact(
  loaded: LoadedFiling,
) -> None:
  """`resolve_element` resolves against the filing's taxonomy, so it ranks
  abstracts and members among its matches — and tells the caller to pass what
  it returns to `fact_grid`. Following that advice must not hit silence."""
  out = tools.fact_grid(
    loaded, ["us-gaap:IncomeStatementAbstract", "acme:WidgetsMember", "Revenues"]
  )
  by_concept = {e["concept"]: e for e in out["excluded"]}
  assert by_concept["us-gaap:IncomeStatementAbstract"]["facts"] == 0
  assert "abstract" in by_concept["us-gaap:IncomeStatementAbstract"]["reason"]
  assert "member" in by_concept["acme:WidgetsMember"]["reason"]
  assert "`member`" in by_concept["acme:WidgetsMember"]["try"]
  # neither is absent from the filing, so neither is unresolved
  assert "unresolved" not in out
  assert any(r["concept"] == "us-gaap:Revenues" for r in out["rows"])


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


def test_locate_skips_a_contents_row_whose_heading_fills_the_match() -> None:
  """Twelve words is the guard against a contents row, and Item 5's caption is
  twelve words by itself: "Market for Registrant's Common Equity, Related
  Stockholder Matters and Issuer Purchases of Equity Securities". The whole
  head fitting inside the row, the row matched first, and describe_filing
  published the table of contents as the offset of Item 5 on more than half
  the 10-Ks in the corpus."""
  heading = (
    "Item 5. Market for Registrant's Common Equity, Related Stockholder "
    "Matters and Issuer Purchases of Equity Securities"
  )
  body = (
    "The Company's common stock is traded on The Nasdaq Stock Market under "
    "the symbol AAPL. The Company repurchased shares under its share "
    "repurchase program during the fourth quarter."
  )
  text = (
    f"| {heading} | 19 |\n| Item 6. [Reserved] | 20 |\n"
    f"| Item 7. Management's Discussion and Analysis | 21 |\n"
    f"\nItem 1. Business\nAcme makes widgets.\n\n{heading}\n{body}"
  )
  assert _locate(text, f"{heading}\n\n{body}") == text.index(f"\n{heading}\n{body}") + 1


def test_locate_skips_a_contents_list_that_is_not_drawn_as_a_table() -> None:
  """Oracle's contents is a bare run of lines — no pipes, no page cells — so
  there is no row to recognise. What tells them apart is that a contents entry
  is followed by the next entry, never by the section's thirtieth word."""
  heading = (
    "Item 5. Market for Registrant's Common Equity, Related Stockholder "
    "Matters and Issuer Purchases of Equity Securities"
  )
  body = (
    "Our common stock is listed on the New York Stock Exchange. We repurchased "
    "shares of common stock under our publicly announced repurchase program "
    "during fiscal 2026, and we expect to continue repurchasing shares."
  )
  text = (
    "Item 4.\n\nMine Safety Disclosures\n\nPART II.\n\n"
    f"{heading}\n\nItem 6.\n\n[Reserved]\n\nItem 7.\n\n"
    "Management's Discussion and Analysis of Financial Condition\n\n"
    f"{heading}\n{body}"
  )
  assert _locate(text, f"{heading}\n\n{body}") == text.rindex(heading)


# -- export ---------------------------------------------------------------------


@pytest.mark.parametrize("fmt", ["clawdog", "holon", "tavi", "oim"])
def test_export_filing_writes_projection(
  loaded: LoadedFiling, tmp_path: Path, fmt: str
) -> None:
  out = tools.export_filing(loaded, fmt, tmp_path)
  path = Path(out["path"])
  assert path.parent == tmp_path and path.is_file() and out["bytes"] > 0
  json.loads(path.read_text())
  if fmt in ("clawdog", "tavi"):
    assert len(out["files"]) == 2
  with pytest.raises(tools.ToolError):
    tools.export_filing(loaded, "pdf", tmp_path)


def test_view_filing_serves_it_for_the_viewer(loaded: LoadedFiling) -> None:
  from xbrlkit.view import DEFAULT_VIEWER, ViewerHost

  viewers = ViewerHost()
  try:
    out = tools.view_filing(loaded, "holon", viewers)
    assert out["filing"] == loaded.id and out["bytes"] > 0
    assert out["viewer_url"].startswith(f"{DEFAULT_VIEWER}/view?url=")
    assert out["document_url"] in out["viewer_url"]
    assert out["document_url"].startswith("http://127.0.0.1:")
    with pytest.raises(tools.ToolError):
      tools.view_filing(loaded, "oim", viewers)
  finally:
    viewers.close()


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


# -- a taxonomy published on its own ------------------------------------------------


XS = "xmlns:xs='http://www.w3.org/2001/XMLSchema'"
GAAP_URL = "https://taxonomies.example/gaap/2025/"


def _bare_taxonomy(root: Path) -> Path:
  """A taxonomy shipped the way GASB's exposure draft is: no manifest, one
  schema importing its roles and types, its linkbases beside it pointing back
  at it."""
  root.mkdir(parents=True, exist_ok=True)
  (root / "gov-2026.xsd").write_text(
    f"<xs:schema {XS}>"
    "<xs:import namespace='http://gov.example/roles' schemaLocation='gov-roles.xsd'/>"
    "<xs:import namespace='http://gov.example/types' schemaLocation='./gov-types.xsd'/>"
    "<xs:import namespace='http://www.xbrl.org/2003/instance' "
    "schemaLocation='http://www.xbrl.org/2003/xbrl-instance-2003-12-31.xsd'/>"
    "</xs:schema>"
  )
  (root / "gov-roles.xsd").write_text(f"<xs:schema {XS}/>")
  (root / "gov-types.xsd").write_text(f"<xs:schema {XS}/>")
  (root / "gov-2026-pre.xml").write_text(
    "<link:linkbase xmlns:link='http://www.xbrl.org/2003/linkbase' "
    "xmlns:xlink='http://www.w3.org/1999/xlink'>"
    "<link:roleRef xlink:href='gov-roles.xsd#stmt'/>"
    "<link:loc xlink:href='gov-2026.xsd#gov_Cash'/></link:linkbase>"
  )
  return root


def _published_package(root: Path) -> Path:
  """A taxonomy package shipped the way FASB ships US GAAP: a manifest listing
  its entry points by their published URLs, a catalog mapping those URLs into
  the package, and no report."""
  pkg = root / "gaap-2025"
  (pkg / "META-INF").mkdir(parents=True)

  def entry(name: str, href: str) -> str:
    return (
      f"<tp:entryPoint><tp:name>{name}</tp:name>"
      f"<tp:entryPointDocument href='{href}'/></tp:entryPoint>"
    )

  (pkg / "META-INF" / "taxonomyPackage.xml").write_text(
    "<tp:taxonomyPackage xmlns:tp='http://xbrl.org/2016/taxonomy-package'>"
    "<tp:entryPoints>"
    + entry("Everything", f"{GAAP_URL}entire/gaap-entryPoint-all-2025.xsd")
    + entry("Published elsewhere", "https://elsewhere.example/other.xsd")
    + entry("Elements only", f"{GAAP_URL}elts/gaap-2025.xsd")
    + entry("Meta model", "../meta/gaap-meta-2025.xsd")
    + "</tp:entryPoints></tp:taxonomyPackage>"
  )
  (pkg / "META-INF" / "catalog.xml").write_text(
    "<catalog xmlns='urn:oasis:names:tc:entity:xmlns:xml:catalog'>"
    f"<rewriteURI uriStartString='{GAAP_URL}' rewritePrefix='../'/></catalog>"
  )
  for rel in (
    "entire/gaap-entryPoint-all-2025.xsd",
    "elts/gaap-2025.xsd",
    "meta/gaap-meta-2025.xsd",
  ):
    (pkg / rel).parent.mkdir(parents=True, exist_ok=True)
    (pkg / rel).write_text(f"<xs:schema {XS}/>")
  return root


def _zip(tree: Path, archive: Path) -> Path:
  import zipfile

  with zipfile.ZipFile(archive, "w") as zf:
    for path in sorted(tree.rglob("*")):
      if path.is_file():
        zf.write(path, path.relative_to(tree))
  return archive


def test_a_taxonomy_holds_no_report(tmp_path: Path) -> None:
  assert _find_load_target(_bare_taxonomy(tmp_path)) is None
  assert _find_load_target(_published_package(tmp_path / "pkg")) is None


def test_a_bare_taxonomy_loads_from_the_schema_nothing_imports(tmp_path: Path) -> None:
  from xbrlkit.serve.session import _choose_entry_point

  # The roles and types are imported, so they are not where the DTS starts;
  # the linkbase pointing back at the main schema does not make it an import.
  chosen = _choose_entry_point(_bare_taxonomy(tmp_path), None)
  assert chosen.entry_point.document == "gov-2026.xsd"
  assert chosen.others == []


def test_a_bare_taxonomy_with_several_roots_asks_for_one(tmp_path: Path) -> None:
  from xbrlkit.serve.session import _choose_entry_point

  root = _bare_taxonomy(tmp_path)
  (root / "gov-2026-alt.xsd").write_text(f"<xs:schema {XS}/>")
  with pytest.raises(SourceError, match="entry_point"):
    _choose_entry_point(root, None)
  chosen = _choose_entry_point(root, "gov-2026-alt")
  assert chosen.entry_point.document == "gov-2026-alt.xsd"
  assert [e.document for e in chosen.others] == ["gov-2026.xsd"]


def test_a_manifest_loads_the_first_entry_point_it_lists(tmp_path: Path) -> None:
  from xbrlkit.serve.session import _choose_entry_point

  chosen = _choose_entry_point(_published_package(tmp_path), None)
  assert chosen.entry_point.name == "Everything"
  assert chosen.entry_point.document == "gaap-2025/entire/gaap-entryPoint-all-2025.xsd"
  # The one published outside the package is not offered; a relative href
  # resolves against the manifest.
  assert [e.document for e in chosen.others] == [
    "gaap-2025/elts/gaap-2025.xsd",
    "gaap-2025/meta/gaap-meta-2025.xsd",
  ]


def test_an_entry_point_is_named_by_path_file_or_name(tmp_path: Path) -> None:
  from xbrlkit.serve.session import _choose_entry_point

  root = _published_package(tmp_path)

  def chosen(wanted: str) -> str:
    return _choose_entry_point(root, wanted).entry_point.document

  elements = "gaap-2025/elts/gaap-2025.xsd"
  assert chosen("gaap-2025/elts/gaap-2025.xsd") == elements
  assert chosen("gaap-2025.xsd") == elements
  assert chosen("Elements only") == elements
  assert chosen("gaap-entryPoint-all-2025").endswith("entryPoint-all-2025.xsd")
  assert chosen("meta") == "gaap-2025/meta/gaap-meta-2025.xsd"
  with pytest.raises(SourceError, match="matches 3"):
    chosen("gaap")
  with pytest.raises(SourceError, match="No entry point matches 'nope'"):
    chosen("nope")


def test_a_taxonomy_zip_loads_with_its_entry_point_stated(
  tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
  from xbrlkit.serve.session import NoXbrlFound

  parsed: list[Path] = []

  def fake_parse(self, target, accession, filing, entity, packages=None):
    parsed.append(Path(target))
    if Path(target).name == "gaap-2025.xsd":
      raise NoXbrlFound(f"{target} holds no XBRL facts or concepts")
    return _model()

  monkeypatch.setattr(FilingSession, "_parse", fake_parse)
  archive = _zip(_published_package(tmp_path / "tree"), tmp_path / "gaap-2025.zip")
  session = FilingSession()
  try:
    lf = session.load(str(archive))
    assert (
      parsed[-1].as_posix().endswith("gaap-2025/entire/gaap-entryPoint-all-2025.xsd")
    )
    receipt = tools.load_receipt(lf)
    taxonomy = receipt["taxonomy"]
    assert taxonomy["entry_point"]["name"] == "Everything"
    assert [e["document"] for e in taxonomy["other_entry_points"]] == [
      "gaap-2025/elts/gaap-2025.xsd",
      "gaap-2025/meta/gaap-meta-2025.xsd",
    ]
    assert tools.describe_filing(lf)["next"][0].startswith("resolve_element")

    other = session.load(str(archive), entry_point="meta")
    assert other.taxonomy is not None
    assert other.taxonomy.entry_point.document == "gaap-2025/meta/gaap-meta-2025.xsd"

    # An elements-only schema has no networks to read; the error says so and
    # names the entry points that do, rather than that this is not XBRL.
    with pytest.raises(SourceError, match="declares no networks"):
      session.load(str(archive), entry_point="Elements only")
  finally:
    session.close()


def test_a_taxonomy_zip_loads_by_url(
  tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
  monkeypatch.setattr(FilingSession, "_parse", lambda self, *a, **kw: _model())
  archive = _zip(_bare_taxonomy(tmp_path / "tree"), tmp_path / "gov-2026.zip")
  session = FilingSession()

  def fake_fetch(url: str, into: Path | None = None) -> Path:
    assert into is not None
    return Path(shutil.copy(archive, into / Path(url).name))

  monkeypatch.setattr(session, "_fetch", fake_fetch)
  try:
    lf = session.load("https://taxonomies.example/gov/gov-2026.zip")
    assert lf.taxonomy is not None
    assert lf.taxonomy.entry_point.document == "gov-2026.xsd"
  finally:
    session.close()


def test_entry_point_is_refused_where_there_is_no_package(tmp_path: Path) -> None:
  schema = _bare_taxonomy(tmp_path) / "gov-2026.xsd"
  session = FilingSession()
  try:
    for source in (str(schema), "ACME", "https://example.test/report.htm"):
      with pytest.raises(SourceError, match="taxonomy package"):
        session.load(source, entry_point="gov-2026")
  finally:
    session.close()


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


def test_search_text_says_where_the_unreturned_matches_fall() -> None:
  """A broad pattern routes the next call by weight, not by guess."""
  lf = _loaded_with_document()
  out = tools.search_text(lf, "contract liabilities", max_hits=1)
  assert out["total"] == 2 and len(out["hits"]) == 1
  assert out["sections"] == [
    {"section": "MD&A", "hits": 1},
    {"section": "Revenue Recognition Policy", "hits": 1},
  ]
  assert "2 matches, 1 returned" in out["note"]
  # Every match came back, so there is nothing left to say about where they are.
  assert "sections" not in tools.search_text(lf, "contract liabilities")
  # The pure profile keeps the ladder's hit shape.
  assert "sections" not in tools.search_text(
    lf, "contract liabilities", max_hits=1, pure=True
  )


def test_search_text_decomposes_a_phrase_that_matches_nothing() -> None:
  """A regex is all or nothing; the words it is made of are not."""
  lf = _loaded_with_document()
  out = tools.search_text(lf, "customer concentration")
  assert out["total"] == 0 and out["hits"] == []
  assert out["terms"] == [
    {"term": "customer", "matches": 1},
    {"term": "concentration", "matches": 0},
  ]
  assert "terms" in out["note"]
  # One word that matched nothing is what `total` already said.
  assert "terms" not in tools.search_text(lf, "concentration")
  assert "terms" not in tools.search_text(lf, "customer concentration", pure=True)


def _loaded_blocks_only() -> LoadedFiling:
  """A report that is only its tagged blocks, one of them unlabelled.

  The shape a produced report arrives in — a holon written by a ledger, a
  classic instance, a ``model.json`` — where there is no primary document to
  read and the text is assembled from the blocks under their concept names.
  The second block is an extension concept its producer gave no preferred
  label, which is the common case there and the one the fixture with a
  document does not cover.
  """
  from xbrlkit.serve.session import _text_from_text_blocks

  model = _model()
  model.concepts["acme:OperatingExpensePolicyTextBlock"] = Concept(
    qname="acme:OperatingExpensePolicyTextBlock",
    namespace="http://acme.example/20241231",
    name="OperatingExpensePolicyTextBlock",
    period_type="duration",
    is_numeric=False,
    is_textblock=True,
    item_type="textBlockItemType",
    nice_type="Text Block",
  )
  model.facts.append(
    XbrlFact(
      id="t2",
      concept_qname="acme:OperatingExpensePolicyTextBlock",
      period_id="D-2024",
      entity_cik="0001234567",
      value_str=(
        "<div><p>Operating expense is classified by function: cost of revenue, "
        "research and development, and general and administrative. Equipment is "
        "depreciated straight-line over thirty-six months.</p></div>"
      ),
      value_kind="text",
    )
  )
  block_text, block_sections = _text_from_text_blocks(model)
  return LoadedFiling(
    id="acme-blocks",
    source="memory",
    model=model,
    text=block_text,
    sections=block_sections,
    has_document=False,
  )


def test_assembled_reading_leaves_no_match_outside_a_section() -> None:
  """The concept name this server rendered as a heading is part of its block.

  Otherwise every heading sits in a gap between two sections, and since a
  qname carries the most topical word of the block it titles, the routing a
  broad pattern gets back is short by exactly the matches a reader is most
  likely to have been searching for.
  """
  lf = _loaded_blocks_only()
  out = tools.search_text(lf, "policy", max_hits=1)
  # "Policy" occurs only in the two concept names standing as headings.
  assert out["total"] == 2
  assert sum(row["hits"] for row in out["sections"]) == out["total"]
  assert out["sections"] == [
    {"section": "Revenue Recognition Policy", "hits": 1},
    {"section": "OperatingExpensePolicyTextBlock", "hits": 1},
  ]
  # And the hit itself is attributed, not returned as belonging to nothing.
  assert out["hits"][0]["section"] == "Revenue Recognition Policy"


def test_assembled_reading_names_an_unlabelled_block_by_its_concept() -> None:
  """A producer that wrote no preferred label still routes by a name."""
  lf = _loaded_blocks_only()
  out = tools.search_text(lf, "expense|depreciated", max_hits=1)
  # Three: the block's own body twice, and "Expense" in the heading over it.
  assert out["sections"] == [{"section": "OperatingExpensePolicyTextBlock", "hits": 3}]
  assert sum(row["hits"] for row in out["sections"]) == out["total"]


def _loaded_many_blocks(count: int = 12) -> LoadedFiling:
  """A report whose matches fall in more sections than the rows can hold."""
  from xbrlkit.serve.session import _text_from_text_blocks

  model = _model()
  for i in range(count):
    qname = f"acme:Note{i:02d}TextBlock"
    model.concepts[qname] = Concept(
      qname=qname,
      namespace="http://acme.example/20241231",
      name=f"Note{i:02d}TextBlock",
      period_type="duration",
      is_numeric=False,
      is_textblock=True,
      item_type="textBlockItemType",
      nice_type="Text Block",
      pref_label=f"Note {i:02d}",
    )
    model.facts.append(
      XbrlFact(
        id=f"n{i}",
        concept_qname=qname,
        period_id="D-2024",
        entity_cik="0001234567",
        value_str=(
          "<p>This note discusses the allocation of consideration among the "
          "separate obligations the company carries, and the allocation basis "
          f"the company applied in period {i} under its stated policy.</p>"
        ),
        value_kind="text",
      )
    )
  block_text, block_sections = _text_from_text_blocks(model)
  return LoadedFiling(
    id="acme-notes",
    source="memory",
    model=model,
    text=block_text,
    sections=block_sections,
    has_document=False,
  )


def test_a_capped_distribution_says_how_many_sections_it_left_out() -> None:
  """The rows are the busiest ten; a caller must not read their sum as total.

  Every one of the twelve notes carries the word twice, so the ten rows
  account for twenty of twenty-four matches. Saying the rows count where all
  of the matches fall would put the other four nowhere.
  """
  lf = _loaded_many_blocks()
  out = tools.search_text(lf, "allocation", max_hits=1)
  assert out["total"] == 24
  assert len(out["sections"]) == 10
  assert out["sections_omitted"] == 2
  assert "24 matches, 1 returned; `sections` counts the 10 busiest of 12" in out["note"]


def test_an_uncapped_distribution_still_accounts_for_every_match() -> None:
  """Nothing is omitted when the matches fall within the rows."""
  lf = _loaded_blocks_only()
  out = tools.search_text(lf, "revenue", max_hits=1)
  assert "sections_omitted" not in out
  assert "counts where all of them fall" in out["note"]
  assert sum(row["hits"] for row in out["sections"]) == out["total"]


def test_a_term_is_counted_where_a_word_starts() -> None:
  """A term that only trails inside a concept name is not a word the text uses."""
  lf = _loaded_blocks_only()
  out = tools.search_text(lf, "policy block")
  assert out["total"] == 0
  # "block" appears twice as the tail of a ...TextBlock concept name and never
  # as a word; "policy" likewise only inside the two names.
  assert out["terms"] == [
    {"term": "policy", "matches": 0},
    {"term": "block", "matches": 0},
  ]
  # A stem a caller wrote on purpose still counts the words it begins.
  assert tools.search_text(lf, "depreciat straightline")["terms"] == [
    {"term": "depreciat", "matches": 1},
    {"term": "straightline", "matches": 0},
  ]


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
    # A serialization xbrlkit recognises but cannot read is named as itself,
    # not as a broken model: an empty TAVI is a TAVI.
    tavi = tmp_path / "x.tavi.json"
    tavi.write_text(
      '{"documentInfo": {"documentType": "https://xbrl.org/PWD/2026-09-01/compiled"}}'
    )
    with pytest.raises(SourceError, match="as tavi"):
      session.load(str(tavi))
    holon = tmp_path / "x.holon.jsonld"
    holon.write_text('{"@context": {}, "@graph": []}')
    with pytest.raises(SourceError, match="as holon"):
      session.load(str(holon))
    oim = tmp_path / "x.oim.json"
    oim.write_text(
      '{"documentInfo": {"documentType": "https://xbrl.org/2021/xbrl-json"}}'
    )
    with pytest.raises(SourceError, match="xBRL-JSON"):
      session.load(str(oim))
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
      "search_filings",
      "load_filing",
      "unload_filing",
      "describe_filing",
      "resolve_element",
      "fact_grid",
      "statement",
      "calculation",
      "disclosures",
      "information_block",
      "search_text",
      "read_text",
      "records",
      "documents",
      "read_document",
      "export_filing",
      "view_filing",
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


class TestStartupBanner:
  """What ``xbrlkit serve`` tells the operator on the way up."""

  def test_the_json_is_valid_and_tracks_the_live_url(self):
    """Built from the running url, so a non-default port stays copy-pasteable."""
    from xbrlkit.serve.server import startup_banner

    url = "http://127.0.0.1:9000/mcp"
    banner = startup_banner(url, "Acme Corp ops@acme.com")
    block = banner[banner.index("{") : banner.rindex("}") + 1]
    parsed = json.loads(textwrap.dedent(block))
    assert parsed == {"mcpServers": {"xbrlkit": {"type": "http", "url": url}}}

  def test_it_offers_the_claude_code_command(self):
    from xbrlkit.serve.server import startup_banner

    banner = startup_banner("http://127.0.0.1:8765/mcp", None)
    assert "claude mcp add --transport http xbrlkit http://127.0.0.1:8765/mcp" in banner

  def test_an_undeclared_identity_names_the_default_and_the_fix(self):
    from xbrlkit.config import DEFAULT_USER_AGENT
    from xbrlkit.serve.server import identity_lines

    text = "\n".join(identity_lines(None))
    assert DEFAULT_USER_AGENT in text
    assert "SEC_GOV_USER_AGENT=" in text

  def test_a_declared_identity_is_echoed_without_advice(self):
    from xbrlkit.serve.server import identity_lines

    lines = identity_lines("Acme Corp ops@acme.com")
    assert lines == ["SEC identity: Acme Corp ops@acme.com"]


class TestSearchFilings:
  """EDGAR-wide discovery: the step before load_filing."""

  @staticmethod
  def _stub(monkeypatch, total, hits, rows=None):
    """Stand in for EftsClient without touching the network. `rows`, when
    given, replaces the generated hits with specific ones."""
    import xbrlkit.edgar.efts as efts

    class _Hit:
      def __init__(self, cik, accession, parties=(), party_ciks=()):
        self.cik = cik
        self.accession = accession
        self.form = "10-K"
        self.filing_date = "2026-03-24"
        self.primary_document = "ACME CORP  (ACME)"
        self.parties = parties or ("ACME CORP  (ACME)",)
        self.party_ciks = party_ciks or (cik,)

    class _Client:
      def __init__(self, config=None):
        self.config = config

      def query_with_total(self, **kwargs):
        _Client.seen = kwargs
        if rows is not None:
          return total, rows
        return total, [_Hit(f"000000000{i}", f"acc-{i}") for i in range(hits)]

    monkeypatch.setattr(efts, "EftsClient", _Client)
    return _Client

  def test_a_hit_is_directly_loadable(self, monkeypatch):
    """`source` is the cik:accession load_filing takes — that is the point."""
    self._stub(monkeypatch, total=3, hits=3)
    out = tools.search_filings(text_query="goodwill impairment")
    assert out["filings"][0]["source"] == "0000000000:acc-0"
    assert out["returned"] == 3

  def test_a_multi_party_hit_says_why_it_matched(self, monkeypatch):
    """A CIK query for a company also returns the Form 4s filed about it,
    whose `filer` is an individual. The hit has to say so, or it reads as a
    result for the wrong company."""
    from xbrlkit.edgar import EftsHit

    form4 = EftsHit(
      cik="0001866577",
      accession="0001522767-26-000170",
      form="4",
      file_number=None,
      filing_date="2026-09-01",
      primary_document="Shaw Timothy",
      file_url=None,
      party_ciks=("0001866577", "0001522767"),
      parties=("Shaw Timothy", "MARIMED INC."),
    )
    self._stub(monkeypatch, total=1, hits=0, rows=[form4])

    out = tools.search_filings(ciks=["1522767"], forms=["4"])
    row = out["filings"][0]
    assert row["parties"] == ["Shaw Timothy", "MARIMED INC."]
    assert row["matched_cik"] == ["0001522767"]
    assert "ownership form" in out["parties_note"]

  def test_a_single_party_hit_stays_quiet(self, monkeypatch):
    """A company's own filing has one party; no note, no extra keys."""
    self._stub(monkeypatch, total=1, hits=1)
    out = tools.search_filings(ciks=["0000000000"])
    assert "parties" not in out["filings"][0]
    assert "parties_note" not in out

  def test_an_unfiltered_search_is_refused(self):
    """Every filing on EDGAR is not an answer."""
    with pytest.raises(tools.ToolError, match="at least one"):
      tools.search_filings()

  def test_the_total_is_reported_beside_the_page(self, monkeypatch):
    """8 of 3,412 is a different answer from 8."""
    self._stub(monkeypatch, total=3412, hits=5)
    out = tools.search_filings(text_query="x", limit=5)
    assert out["total_matching"] == 3412
    assert out["returned"] == 5
    assert "3412 filings match" in out["note"]

  def test_no_note_when_the_page_is_the_whole_answer(self, monkeypatch):
    self._stub(monkeypatch, total=2, hits=2)
    out = tools.search_filings(text_query="x")
    assert "note" not in out

  @pytest.mark.parametrize("asked", [500, 10_000])
  def test_the_limit_is_clamped(self, monkeypatch, asked):
    """EFTS will page through 10,000 hits given the chance; a tool will not."""
    client = self._stub(monkeypatch, total=10_000, hits=1)
    tools.search_filings(text_query="x", limit=asked)
    assert client.seen["max_results"] == tools.SEARCH_MAX_LIMIT

  def test_a_network_failure_becomes_a_tool_error(self, monkeypatch):
    """So the caller gets a message it can act on, not a traceback."""
    import xbrlkit.edgar.efts as efts

    class _Broken:
      def __init__(self, config=None):
        pass

      def query_with_total(self, **kwargs):
        raise ConnectionError("EDGAR unreachable")

    monkeypatch.setattr(efts, "EftsClient", _Broken)
    with pytest.raises(tools.ToolError, match="EDGAR unreachable"):
      tools.search_filings(text_query="x")


# -- 8-K items ------------------------------------------------------------------


def test_parse_items_handles_edgars_shapes() -> None:
  from xbrlkit.edgar.items import parse_items

  assert parse_items("2.02,9.01") == ["2.02", "9.01"]
  assert parse_items(" 2.02 , 9.01 ,") == ["2.02", "9.01"]
  assert parse_items("Item 2.02: Results of Operations,Item 9.01") == ["2.02", "9.01"]
  assert parse_items("5.02;9.01") == ["5.02", "9.01"]
  assert parse_items("2.02,2.02") == ["2.02"]
  assert parse_items("") == [] and parse_items(None) == []


def test_an_earnings_8k_is_identified_and_points_at_the_exhibit() -> None:
  from xbrlkit.edgar.items import describe_items, is_earnings_release, items_note

  assert is_earnings_release(["2.02", "9.01"]) is True
  assert is_earnings_release(["8.01", "9.01"]) is False
  named = describe_items(["2.02", "9.01"])
  assert named[0] == {
    "item": "2.02",
    "name": "Results of Operations and Financial Condition",
  }
  # An unknown code is still returned, as itself.
  assert describe_items(["9.99"]) == [{"item": "9.99", "name": ""}]

  earnings = items_note(["2.02", "9.01"])
  assert earnings is not None and "EX-99.1" in earnings and "documents" in earnings
  # A release furnished under Reg FD alone is not coded 2.02, and must still
  # be routed to the exhibit — 2.02 is a strong positive signal, a weak
  # negative one.
  assert is_earnings_release(["2.01", "7.01", "9.01"]) is False
  fd = items_note(["2.01", "7.01", "9.01"])
  assert fd is not None and "documents" in fd and "read_document" in fd
  # Every coded 8-K says something; an uncoded filing says nothing.
  assert items_note(["7.01"]) is not None
  assert items_note(["9.01"]) is not None
  assert items_note(["5.07"]) is not None
  assert items_note([]) is None


def test_filing_meta_carries_the_items_off_an_edgar_ref() -> None:
  from types import SimpleNamespace

  from xbrlkit.cli import filing_meta

  ref = SimpleNamespace(
    form="8-K",
    filing_date="2025-05-07",
    report_date="2025-05-07",
    acceptance_datetime="",
    is_inline=True,
    items="2.02,9.01",
  )
  meta = filing_meta(
    "https://www.sec.gov", "1234567", "0001234567-25-000048", ref, "x.htm"
  )
  assert meta.items == ["2.02", "9.01"]
  # A ref with no items (every form but 8-K) leaves it empty, not None.
  bare = SimpleNamespace(
    form="10-K",
    filing_date="",
    report_date=None,
    acceptance_datetime="",
    is_inline=True,
  )
  assert (
    filing_meta("https://www.sec.gov", "1", "0000000000-00-000000", bare, "x.htm").items
    == []
  )


def test_an_8k_describes_its_items_and_leads_with_the_exhibit() -> None:
  from xbrlkit.model import EntityIdentity, FilingMeta, XbrlModel
  from xbrlkit.serve.session import LoadedFiling

  model = XbrlModel(
    filing=FilingMeta(
      accession="0001234567-25-000048",
      cik="0001234567",
      form="8-K",
      items=["2.02", "9.01"],
    ),
    entity=EntityIdentity(cik="0001234567", name="ACME CORP."),
  )
  lf = LoadedFiling(id="acme", source="x", model=model, text="cover", sections=[])
  out = tools.describe_filing(lf)
  assert out["filing"]["items"][0]["name"] == (
    "Results of Operations and Financial Condition"
  )
  assert "EX-99.1" in out["filing"]["items_note"]
  # The exhibit steer comes first, ahead of the text tools that would otherwise lead.
  assert "documents" in out["next"][0] and "Item 2.02" in out["next"][0]


def test_a_filing_with_no_items_says_nothing_about_them(loaded: LoadedFiling) -> None:
  out = tools.describe_filing(loaded)
  assert "items" not in out["filing"] and "items_note" not in out["filing"]
  assert "documents" not in out["next"][0]


def test_view_filing_serves_a_loaded_document_as_it_was_loaded(tmp_path: Path) -> None:
  """A holon loaded from disk is served for the viewer as it is. The
  re-projection through the model dropped what the model has no slot for,
  and the viewer rendered a different document from the one the user opened."""
  from urllib.request import urlopen

  from xbrlkit.serialize import to_holon
  from xbrlkit.view import ViewerHost

  document = json.loads(to_holon(_model()))
  for graph in document["@graph"]:
    for node in graph["@graph"]:
      if "Entity" in str(node.get("@type")):
        node["country"] = "US"  # a producer's term the model cannot hold
  text = json.dumps(document, indent=2)
  holon = tmp_path / "acme.holon.jsonld"
  holon.write_text(text)
  session = FilingSession()
  viewers = ViewerHost()
  try:
    loaded = session.load(str(holon))
    assert loaded.source_kind == "holon"
    out = tools.view_filing(loaded, "holon", viewers)
    assert out["served"] == "as loaded"
    assert out["bytes"] == len(text.encode("utf-8"))
    with urlopen(out["document_url"]) as response:
      assert response.read().decode("utf-8") == text
    assert tools.view_filing(loaded, "tavi", viewers)["served"] == (
      "projected from the model"
    )
  finally:
    viewers.close()
    session.close()


# ── The cover page as the filer's own account of itself ────────────────────
#
# Most of what identifies a filer is tagged on the cover page, and what is
# tagged there is true as of the day it was filed. It is read first for that
# reason: the submissions header is current, so on a filing from six years
# ago it answers with this year's exchange, name and filer category.


def _cover(**tagged: str) -> XbrlModel:
  from xbrlkit.model import XbrlFact

  return XbrlModel(
    filing=FilingMeta(accession="0000000000-24-000001", cik="0001234567"),
    entity=EntityIdentity(cik="0001234567"),
    facts=[
      XbrlFact(
        id=f"f{n}",
        concept_qname=qname,
        period_id="p1",
        entity_cik="0001234567",
        value_str=value,
        value_kind="text",
      )
      for n, (qname, value) in enumerate(tagged.items())
    ],
  )


@pytest.mark.unit
def test_the_cover_page_fills_the_filer_category_and_fiscal_year_end() -> None:
  from xbrlkit.serve.session import _enrich_from_dei

  model = _enrich_from_dei(
    _cover(
      **{
        "dei:EntityFilerCategory": "Large Accelerated Filer",
        "dei:CurrentFiscalYearEndDate": "--01-31",
        "dei:SecurityExchangeName": "NASDAQ",
      }
    )
  )
  assert model.entity.category == "Large Accelerated Filer"
  assert model.entity.exchange == "NASDAQ"
  # The cover page writes a gMonthDay and the submissions header four digits;
  # one shape, so either source can fill the field.
  assert model.entity.fiscal_year_end == "0131"


@pytest.mark.unit
def test_the_cover_pages_ein_is_written_the_way_the_header_writes_it() -> None:
  """``94-3177549`` on the cover page, ``943177549`` in the header — one
  filer's EIN must not depend on which route read the filing."""
  from xbrlkit.serve.session import _enrich_from_dei

  model = _enrich_from_dei(
    _cover(**{"dei:EntityTaxIdentificationNumber": "94-3177549"})
  )
  assert model.entity.ein == "943177549"


@pytest.mark.unit
def test_the_cover_pages_two_part_phone_becomes_one() -> None:
  from xbrlkit.serve.session import _enrich_from_dei

  model = _enrich_from_dei(
    _cover(**{"dei:CityAreaCode": "408", "dei:LocalPhoneNumber": "486-2000"})
  )
  assert model.entity.phone == "408-486-2000"


@pytest.mark.unit
def test_half_a_phone_number_is_not_a_phone_number() -> None:
  from xbrlkit.serve.session import _enrich_from_dei

  assert _enrich_from_dei(_cover(**{"dei:CityAreaCode": "408"})).entity.phone is None


@pytest.mark.unit
def test_what_the_filing_said_is_not_overwritten_from_outside_it() -> None:
  """The header is applied after the cover page and fills only the gaps."""
  from xbrlkit.serve.session import LoadedFiling, _enrich_filer, _enrich_from_dei

  model = _enrich_from_dei(_cover(**{"dei:SecurityExchangeName": "NASDAQ"}))
  loaded = LoadedFiling(id="x", source="x", model=model, text="", sections=[])
  _enrich_filer(loaded, {"exchange": "NYSE", "sic": "3674"})
  # As filed, not as EDGAR has it today.
  assert loaded.model.entity.exchange == "NASDAQ"
  # And the one field no cover page tags is filled.
  assert loaded.model.entity.sic == "3674"
