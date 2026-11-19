"""A report authored from rows: built, checked, and carried through both
serializations without losing a fact or where it came from."""

from __future__ import annotations

import json
from datetime import date

import pytest

from xbrlkit.author import (
  AuthoringError,
  ConceptRow,
  EntityRow,
  FactRow,
  LineRow,
  ReportRows,
  SectionRow,
  build_report,
)
from xbrlkit.check import check_report
from xbrlkit.deserialize import from_holon_report, from_tavi_report
from xbrlkit.serialize import to_holon, to_tavi

OCT = date(2026, 10, 31)
OCT_1 = date(2026, 10, 1)
KPI = "https://example.com/taxonomy/kpi/v1/"


def _rows() -> ReportRows:
  """A monthly package: a balance sheet that balances, an income statement
  that foots, and one KPI from the firm's own vocabulary."""

  def bs(concept: str, value: float, cell: str) -> FactRow:
    return FactRow(
      concept=concept,
      value=value,
      end=OCT,
      unit="USD",
      decimals=0,
      source=f"package.xlsx#sheet=BS&cell={cell}",
    )

  def pl(concept: str, value: float, cell: str) -> FactRow:
    return FactRow(
      concept=concept,
      value=value,
      start=OCT_1,
      end=OCT,
      unit="USD",
      decimals=0,
      source=f"package.xlsx#sheet=PL&cell={cell}",
    )

  def money(qname: str, label: str, period: str, balance: str) -> ConceptRow:
    return ConceptRow.model_validate(
      {"qname": qname, "label": label, "period_type": period, "balance": balance}
    )

  return ReportRows(
    report_id="pkg-acme-2026-10",
    entity=EntityRow(identifier="acme-co", name="Acme Co"),
    reporting_style="authored-package",
    period_end=OCT,
    attributed_to="package-reader",
    namespaces={"kpi": KPI},
    concepts=[
      ConceptRow(
        qname="rs-gaap:BalanceSheetAbstract", label="Balance Sheet", abstract=True
      ),
      money("rs-gaap:Assets", "Total assets", "instant", "debit"),
      money("rs-gaap:Cash", "Cash", "instant", "debit"),
      money("rs-gaap:AccountsReceivable", "Receivables", "instant", "debit"),
      money(
        "rs-gaap:LiabilitiesAndStockholdersEquity",
        "Total liabilities and equity",
        "instant",
        "credit",
      ),
      money("rs-gaap:Liabilities", "Liabilities", "instant", "credit"),
      money("rs-gaap:StockholdersEquity", "Equity", "instant", "credit"),
      money("rs-gaap:NetIncomeLoss", "Net income", "duration", "credit"),
      money("rs-gaap:Revenues", "Revenue", "duration", "credit"),
      money("rs-gaap:CostsAndExpenses", "Expenses", "duration", "debit"),
      ConceptRow(qname="kpi:KpiAbstract", label="KPIs", abstract=True),
      ConceptRow(
        qname="kpi:Headcount", label="Headcount", kind="integer", period_type="instant"
      ),
    ],
    sections=[
      SectionRow(
        name="Balance Sheet",
        block_type="balance_sheet",
        head="rs-gaap:BalanceSheetAbstract",
        lines=[
          LineRow(concept="rs-gaap:Assets"),
          LineRow(concept="rs-gaap:Cash", parent="rs-gaap:Assets", weight=1),
          LineRow(
            concept="rs-gaap:AccountsReceivable", parent="rs-gaap:Assets", weight=1
          ),
          LineRow(concept="rs-gaap:LiabilitiesAndStockholdersEquity"),
          LineRow(
            concept="rs-gaap:Liabilities",
            parent="rs-gaap:LiabilitiesAndStockholdersEquity",
            weight=1,
          ),
          LineRow(
            concept="rs-gaap:StockholdersEquity",
            parent="rs-gaap:LiabilitiesAndStockholdersEquity",
            weight=1,
          ),
        ],
      ),
      SectionRow(
        name="Income Statement",
        block_type="income_statement",
        lines=[
          LineRow(concept="rs-gaap:NetIncomeLoss"),
          LineRow(concept="rs-gaap:Revenues", parent="rs-gaap:NetIncomeLoss", weight=1),
          LineRow(
            concept="rs-gaap:CostsAndExpenses",
            parent="rs-gaap:NetIncomeLoss",
            weight=-1,
          ),
        ],
      ),
      SectionRow(
        name="KPIs",
        head="kpi:KpiAbstract",
        lines=[LineRow(concept="kpi:Headcount")],
      ),
    ],
    facts=[
      bs("rs-gaap:Assets", 1000, "B10"),
      bs("rs-gaap:Cash", 600, "B4"),
      bs("rs-gaap:AccountsReceivable", 400, "B5"),
      bs("rs-gaap:LiabilitiesAndStockholdersEquity", 1000, "B20"),
      bs("rs-gaap:Liabilities", 300, "B14"),
      bs("rs-gaap:StockholdersEquity", 700, "B18"),
      pl("rs-gaap:NetIncomeLoss", 50, "B9"),
      pl("rs-gaap:Revenues", 250, "B3"),
      pl("rs-gaap:CostsAndExpenses", 200, "B7"),
      FactRow(
        concept="kpi:Headcount",
        value=42,
        end=OCT,
        unit="pure",
        decimals=0,
        source="package.pdf#page=3",
      ),
    ],
  )


def _replace(rows: ReportRows, concept: str, value: float) -> ReportRows:
  for row in rows.facts:
    if row.concept == concept:
      row.value = value
  return rows


def test_a_package_builds_into_a_report_that_checks() -> None:
  rows = _rows()
  model = build_report(rows)
  assert model.filing.report_id == "pkg-acme-2026-10"
  assert model.filing.reporting_style == "authored-package"
  assert model.entity.identifier == "acme-co"
  assert len(model.facts) == len(rows.facts)

  result = check_report(model, expected_facts=len(rows.facts), require_source=True)
  assert result.to_dict()["problems"] == []
  assert result.ok
  # Assets, liabilities-and-equity and net income, each against its terms.
  assert (result.calculations_checked, result.calculations_footed) == (3, 3)
  assert result.balance_checked == 1


def test_the_same_rows_build_the_same_report() -> None:
  assert to_holon(build_report(_rows())) == to_holon(build_report(_rows()))


@pytest.mark.parametrize("fmt", ["holon", "tavi"])
def test_an_authored_report_survives_each_serialization(fmt: str) -> None:
  """Every fact comes back with its value and its source, the report keeps
  its identity and style, and the reread report still checks."""
  model = build_report(_rows())
  if fmt == "holon":
    got, _ = from_holon_report(to_holon(model))
  else:
    got, gaps = from_tavi_report(to_tavi(model))
    assert gaps.skipped_facts == 0

  def signature(m: object) -> list[tuple[object, ...]]:
    facts = getattr(m, "facts")
    return sorted(
      (
        f.concept_qname,
        f.period_id,
        f.numeric_value,
        f.provenance.source if f.provenance else None,
        f.provenance.attributed_to if f.provenance else None,
      )
      for f in facts
    )

  assert signature(got) == signature(model)
  assert got.filing.report_id == model.filing.report_id
  assert got.filing.reporting_style == "authored-package"
  assert (got.entity.identifier, got.entity.scheme, got.entity.name) == (
    model.entity.identifier,
    model.entity.scheme,
    model.entity.name,
  )
  result = check_report(got, expected_facts=len(model.facts), require_source=True)
  assert result.to_dict()["problems"] == []
  assert (result.calculations_checked, result.balance_checked) == (3, 1)


def test_an_authored_holon_is_stable_from_its_first_write() -> None:
  first = to_holon(build_report(_rows()))
  assert to_holon(from_holon_report(first)[0]) == first


def test_a_total_that_does_not_foot_fails_with_the_numbers() -> None:
  model = build_report(_replace(_rows(), "rs-gaap:Cash", 500))
  result = check_report(model)
  assert not result.ok
  [problem] = [p for p in result.problems if p.code == "does_not_foot"]
  assert problem.detail["concept"] == "rs-gaap:Assets"
  assert (problem.detail["reported"], problem.detail["computed"]) == (1000, 900)


def test_a_balance_sheet_that_does_not_balance_fails() -> None:
  rows = _replace(_rows(), "rs-gaap:LiabilitiesAndStockholdersEquity", 1100)
  result = check_report(build_report(_replace(rows, "rs-gaap:Liabilities", 400)))
  assert [p.code for p in result.problems] == ["does_not_balance"]
  assert result.problems[0].detail["difference"] == -100


def test_two_values_for_one_line_fail() -> None:
  rows = _rows()
  rows.facts.append(
    FactRow(concept="rs-gaap:Cash", value=601, end=OCT, unit="USD", decimals=0)
  )
  result = check_report(build_report(rows))
  assert "duplicate_conflict" in [p.code for p in result.problems]


def test_a_fact_with_no_source_fails_only_when_a_source_is_required() -> None:
  rows = _rows()
  rows.facts[0].source = None
  model = build_report(rows)
  assert check_report(model).ok
  assert [p.code for p in check_report(model, require_source=True).problems] == [
    "no_source"
  ]


def test_a_dropped_fact_is_caught_by_the_count() -> None:
  """A TAVI fact whose period cannot be read is left out by the reader; the
  gaps now count it and the check fails on the count."""
  model = build_report(_rows())
  document = json.loads(to_tavi(model))
  document["xbrlModel"]["facts"][0]["factDimensions"]["xbrl:period"] = "October"
  got, gaps = from_tavi_report(json.dumps(document))
  assert gaps.skipped_facts == 1
  result = check_report(got, expected_facts=len(model.facts))
  assert "fact_count" in [p.code for p in result.problems]


def test_a_report_with_no_identifier_fails() -> None:
  model = build_report(_rows())
  model.filing.accession = "unknown"
  assert "no_report_id" in [p.code for p in check_report(model).problems]


def test_rows_that_cannot_be_placed_are_all_named() -> None:
  rows = _rows()
  rows.facts.append(FactRow(concept="rs-gaap:Goodwill", value=1, end=OCT, unit="USD"))
  rows.facts.append(FactRow(concept="rs-gaap:Cash", value="n/a", end=OCT, unit="USD"))
  rows.facts.append(FactRow(concept="rs-gaap:Cash", value=1, end=OCT))
  rows.facts.append(
    FactRow(concept="rs-gaap:Cash", value=1, start=OCT_1, end=OCT, unit="USD")
  )
  rows.concepts.append(ConceptRow(qname="acme:Widgets", label="Widgets"))
  with pytest.raises(AuthoringError) as raised:
    build_report(rows)
  text = " | ".join(raised.value.problems)
  assert len(raised.value.problems) == 5
  assert "undeclared concept" in text
  assert "needs a number" in text
  assert "needs a unit" in text
  assert "instant concept reported for a duration period" in text
  assert "no namespace for its prefix" in text


def test_a_flat_table_needs_a_head() -> None:
  rows = _rows()
  rows.sections[-1].head = None
  with pytest.raises(AuthoringError, match="give the section a head concept"):
    build_report(rows)
