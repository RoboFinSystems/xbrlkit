"""Tests for the pivot (``holon/pivot.py``) and the workbook drawn from it
(``holon/workbook.py``).

The pivot is checked on models built here: where a balance lands, what a
breakdown row holds, which periods get a column. The workbook is checked by
reading the file back: that it names its holon, that a number is a number and
a label is never a formula, and that what it leaves out it says.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime
from io import BytesIO
from pathlib import Path
from typing import Any

import openpyxl
import pytest

from tests.test_holon_databook import _book_model
from tests.test_holon_validate import ROLE, _concept
from xbrlkit.cli import main
from xbrlkit.holon.pivot import PivotSection, pivot_sections
from xbrlkit.holon.workbook import (
  CONTENTS_SHEET,
  OMITTED_CONTENT,
  to_workbook,
  workbook_from_holon,
)
from xbrlkit.model import (
  Arc,
  EntityIdentity,
  FilingMeta,
  Label,
  Network,
  Period,
  Unit,
  XbrlFact,
  XbrlModel,
)
from xbrlkit.serialize import to_holon

WHEN = datetime(2025, 2, 1, 12, 0, tzinfo=UTC)
START_LABEL = "http://www.xbrl.org/2003/role/periodStartLabel"
END_LABEL = "http://www.xbrl.org/2003/role/periodEndLabel"
NEGATED_LABEL = "http://www.xbrl.org/2009/role/negatedLabel"
FY = "D-2024"


def _fact(
  fact_id: str, name: str, value: float, period: str, **fields: Any
) -> XbrlFact:
  return XbrlFact(
    id=fact_id,
    concept_qname=f"us-gaap:{name}",
    period_id=period,
    unit_id="usd",
    entity_cik="0001234567",
    numeric_value=value,
    decimals="0",
    value_kind="numeric",
    **fields,
  )


def _report(
  concepts: list[Any],
  facts: list[XbrlFact],
  arcs: list[Arc],
  periods: list[Period],
  definition: str = "Statement of Equity",
) -> XbrlModel:
  return XbrlModel(
    filing=FilingMeta(accession="0000000000-24-000001", cik="0001234567", form="10-K"),
    entity=EntityIdentity(cik="0001234567", name="Acme Corp"),
    concepts={c.qname: c for c in concepts},
    periods=periods,
    units=[Unit(id="usd", measure="iso4217:USD")],
    facts=facts,
    networks=[
      Network(role_uri=ROLE, definition=definition, kind="presentation", arcs=arcs)
    ],
  )


def _roll_forward() -> XbrlModel:
  """Equity for one year: the opening balance, two movements, the closing one."""
  money = {"is_numeric": True, "item_type": "monetaryItemType"}
  equity = _concept("StockholdersEquity", **money)
  equity.labels = [
    Label(value="Equity, beginning", role=START_LABEL),
    Label(value="Equity, ending", role=END_LABEL),
  ]
  root = "us-gaap:StatementOfStockholdersEquityAbstract"
  return _report(
    concepts=[
      _concept("StatementOfStockholdersEquityAbstract", is_abstract=True),
      equity,
      _concept("NetIncomeLoss", **money),
      _concept("Dividends", **money),
    ],
    facts=[
      _fact("e0", "StockholdersEquity", 100.0, "I-2023"),
      _fact("e1", "StockholdersEquity", 130.0, "I-2024"),
      _fact("ni", "NetIncomeLoss", 50.0, FY),
      _fact("dv", "Dividends", 20.0, FY),
    ],
    arcs=[
      Arc(
        from_qname=root,
        to_qname="us-gaap:StockholdersEquity",
        order=1.0,
        preferred_label=START_LABEL,
      ),
      Arc(from_qname=root, to_qname="us-gaap:NetIncomeLoss", order=2.0),
      Arc(
        from_qname=root,
        to_qname="us-gaap:Dividends",
        order=3.0,
        preferred_label=NEGATED_LABEL,
      ),
      Arc(
        from_qname=root,
        to_qname="us-gaap:StockholdersEquity",
        order=4.0,
        preferred_label=END_LABEL,
      ),
    ],
    periods=[
      Period(id="I-2023", period_type="instant", end=date(2023, 12, 31)),
      Period(id="I-2024", period_type="instant", end=date(2024, 12, 31)),
      Period(
        id=FY,
        period_type="duration",
        start=date(2024, 1, 1),
        end=date(2024, 12, 31),
      ),
    ],
  )


def _only(model: XbrlModel, **kwargs: Any) -> PivotSection:
  sections = pivot_sections(model, **kwargs)
  assert len(sections) == 1
  return sections[0]


def _amounts(section: PivotSection) -> list[tuple[str, list[float | None]]]:
  return [
    (
      row.label,
      [
        row.values[c.key].numeric_value if c.key in row.values else None
        for c in section.columns
      ],
    )
    for row in section.rows
  ]


# -- the pivot -------------------------------------------------------------------


def test_a_balance_is_read_into_the_period_it_opens_or_closes() -> None:
  section = _only(_roll_forward())
  # One column, the year: neither balance date gets a column of its own.
  assert [c.label for c in section.columns] == ["2024-01-01 to 2024-12-31"]
  assert _amounts(section) == [
    ("Equity, beginning", [100.0]),
    ("NetIncomeLoss", [50.0]),
    ("Dividends", [20.0]),
    ("Equity, ending", [130.0]),
  ]
  assert section.shown == section.facts == 4


def test_a_negated_label_is_flagged_and_the_fact_left_as_stated() -> None:
  section = _only(_roll_forward())
  dividends = next(r for r in section.rows if r.qname == "us-gaap:Dividends")
  assert dividends.negated
  assert dividends.values[section.columns[0].key].numeric_value == 20.0
  assert not any(r.negated for r in section.rows if r is not dividends)


def test_an_opening_balance_dated_on_the_first_day_is_still_the_opening() -> None:
  model = _roll_forward()
  model.periods[0] = Period(id="I-2023", period_type="instant", end=date(2024, 1, 1))
  assert _amounts(_only(model))[0] == ("Equity, beginning", [100.0])


def test_a_date_no_period_absorbs_keeps_its_own_column() -> None:
  model = _roll_forward()
  model.periods.append(
    Period(id="I-2022", period_type="instant", end=date(2022, 12, 31))
  )
  model.facts.append(_fact("e9", "StockholdersEquity", 90.0, "I-2022"))
  section = _only(model)
  assert [c.label for c in section.columns] == [
    "2024-01-01 to 2024-12-31",
    "2022-12-31",
  ]
  # The stray balance shows on the closing row, never as an opening one.
  assert _amounts(section)[0] == ("Equity, beginning", [100.0, None])
  assert _amounts(section)[-1] == ("Equity, ending", [130.0, 90.0])
  assert section.shown == section.facts == 5


def test_a_breakdown_is_a_row_under_its_line() -> None:
  section = _only(_book_model())
  cash = next(r for r in section.rows if r.qname == "us-gaap:Cash")
  assert [m.label for m in cash.members] == ["WidgetsMember"]
  assert cash.members[0].dims == (
    ("us-gaap:StatementBusinessSegmentsAxis", "us-gaap:WidgetsMember"),
  )
  column = section.columns[0].key
  assert cash.values[column].numeric_value == 250.0
  assert cash.members[0].values[column].numeric_value == 100.0
  # The axis is scaffolding, not a line.
  assert all("Axis" not in r.qname for r in section.rows)
  assert section.axes == ["us-gaap:StatementBusinessSegmentsAxis"]
  assert section.shown == section.facts == 4


def test_a_line_reported_only_by_member_is_still_a_line() -> None:
  model = _book_model()
  model.facts = [f for f in model.facts if f.id != "f2"]
  section = _only(model)
  cash = next(r for r in section.rows if r.qname == "us-gaap:Cash")
  assert not cash.values
  assert [m.values[section.columns[0].key].numeric_value for m in cash.members] == [
    100.0
  ]


def _statement_with_a_stray_date() -> XbrlModel:
  money = {"is_numeric": True, "item_type": "monetaryItemType"}
  names = [f"Line{i}" for i in range(6)]
  root = "us-gaap:BalanceSheetAbstract"
  return _report(
    concepts=[
      _concept("BalanceSheetAbstract", is_abstract=True),
      *(_concept(name, **money) for name in names),
    ],
    facts=[
      *(_fact(f"a{i}", name, 10.0 + i, "I-2024") for i, name in enumerate(names)),
      *(_fact(f"b{i}", name, 5.0 + i, "I-2023") for i, name in enumerate(names)),
      _fact("stray", "Line0", 1.0, "I-2021"),
    ],
    arcs=[
      Arc(from_qname=root, to_qname=f"us-gaap:{name}", order=float(i))
      for i, name in enumerate(names)
    ],
    periods=[
      Period(id="I-2024", period_type="instant", end=date(2024, 12, 31)),
      Period(id="I-2023", period_type="instant", end=date(2023, 12, 31)),
      Period(id="I-2021", period_type="instant", end=date(2021, 12, 31)),
    ],
    definition="Balance Sheet",
  )


def test_a_stray_comparative_gets_no_column_and_is_counted() -> None:
  section = _only(_statement_with_a_stray_date())
  assert [c.label for c in section.columns] == ["2024-12-31", "2023-12-31"]
  assert (section.facts, section.shown) == (13, 12)

  kept = _only(_statement_with_a_stray_date(), drop_sparse_periods=False)
  assert [c.label for c in kept.columns] == ["2024-12-31", "2023-12-31", "2021-12-31"]
  assert kept.shown == kept.facts == 13


# -- the workbook ----------------------------------------------------------------


@pytest.fixture(scope="module")
def holon_text() -> str:
  return to_holon(_book_model())


def _open(data: bytes) -> Any:
  return openpyxl.load_workbook(BytesIO(data))


def _rows(sheet: Any) -> dict[str, list[Any]]:
  """A section sheet's lines by label: the cells after the label."""
  return {
    row[0].value: list(row[1:])
    for row in sheet.iter_rows(min_row=6)
    if row[0].value is not None
  }


def test_the_workbook_names_the_holon_it_was_written_from(holon_text: str) -> None:
  book = _open(
    workbook_from_holon(holon_text, href="acme.holon.jsonld", timestamp=WHEN)
  )
  assert book.sheetnames == [CONTENTS_SHEET, "Balance Sheet"]
  contents = book[CONTENTS_SHEET]
  facts = {
    row[0].value: row[1].value for row in contents.iter_rows(min_row=3, max_row=12)
  }
  assert facts["Holon"] == "https://robosystems.ai/report/0000000000-24-000001"
  assert facts["Holon file"] == "acme.holon.jsonld"
  assert facts["Holon SHA-256"] == hashlib.sha256(holon_text.encode()).hexdigest()
  assert facts["Written"] == "2025-02-01T12:00:00Z"
  column_a = [row[0].value for row in contents.iter_rows()]
  for line in OMITTED_CONTENT:
    assert line in column_a
  # The contents row of a section links to its sheet and counts its facts.
  listed = next(row for row in contents.iter_rows() if row[0].value == "Balance Sheet")
  assert listed[0].hyperlink.location == "'Balance Sheet'!A1"
  assert [c.value for c in listed[3:7]] == [3, 1, 4, 4]


def test_a_section_is_lines_by_periods_with_numbers_as_numbers(holon_text: str) -> None:
  sheet = _open(workbook_from_holon(holon_text, timestamp=WHEN))["Balance Sheet"]
  assert [c.value for c in sheet[5]] == ["Line item", "2024-12-31", "Unit", "Concept"]
  assert sheet.freeze_panes == "B6"
  rows = _rows(sheet)
  assert [(label, cells[0].value) for label, cells in rows.items()] == [
    ("Assets", 1000),
    ("Cash", 250),
    ("WidgetsMember", 100),
    ("Inventory", 750),
  ]
  assets, cash, member = rows["Assets"], rows["Cash"], rows["WidgetsMember"]
  assert assets[0].data_type == "n"
  assert assets[0].number_format == "#,##0;(#,##0)"
  assert (assets[1].value, assets[2].value) == ("USD", "us-gaap:Assets")
  # A total is bold; a breakdown sits a level under its line and says its member.
  assert assets[0].font.bold and not cash[0].font.bold
  assert member[0].font.italic
  assert (
    member[2].value == "us-gaap:StatementBusinessSegmentsAxis=us-gaap:WidgetsMember"
  )
  indent = {row[0].value: row[0].alignment.indent for row in sheet.iter_rows(min_row=6)}
  assert indent["WidgetsMember"] == indent["Cash"] + 1


def test_no_cell_is_a_formula_whatever_a_label_or_a_value_says() -> None:
  model = _book_model()
  model.concepts["us-gaap:Cash"].pref_label = '=HYPERLINK("http://x","Cash")'
  note = _concept("PolicyNote", is_textblock=True)
  note.pref_label = "Policy"
  model.concepts[note.qname] = note
  model.networks[0].arcs.append(
    Arc(from_qname="us-gaap:Assets", to_qname=note.qname, order=5.0)
  )
  model.facts.append(
    XbrlFact(
      id="t1",
      concept_qname=note.qname,
      period_id="I-2024",
      entity_cik="0001234567",
      value_str="<p>=1+1 &amp; more\x07</p>",
      value_kind="text",
    )
  )
  book = _open(to_workbook(model, timestamp=WHEN))
  for sheet in book:
    for row in sheet.iter_rows():
      for cell in row:
        assert cell.data_type != "f", (sheet.title, cell.coordinate, cell.value)
  rows = _rows(book["Balance Sheet"])
  assert '=HYPERLINK("http://x","Cash")' in rows
  # Markup is stripped, entities read, and what XML cannot carry is dropped.
  assert rows["Policy"][0].value == "=1+1 & more"


def test_a_negated_line_prints_reversed_and_says_so() -> None:
  sheet = _open(to_workbook(_roll_forward(), timestamp=WHEN))["Statement of Equity"]
  assert [c.value for c in sheet[5]] == [
    "Line item",
    "2024-01-01 to 2024-12-31",
    "Unit",
    "Concept",
    "Sign",
  ]
  rows = _rows(sheet)
  assert [(label, cells[0].value) for label, cells in rows.items()] == [
    ("Equity, beginning", 100),
    ("NetIncomeLoss", 50),
    ("Dividends", -20),
    ("Equity, ending", 130),
  ]
  assert rows["Dividends"][3].value == "reversed"
  assert rows["NetIncomeLoss"][3].value is None


def test_a_fact_that_states_no_places_reads_evenly_down_its_column() -> None:
  model = _book_model()
  for fact in model.facts:
    fact.decimals = "INF"
  model.facts[1].numeric_value = 250.5
  model.facts[1].value_str = "250.5"
  rows = _rows(_open(to_workbook(model, timestamp=WHEN))["Balance Sheet"])
  assert {cells[0].number_format for cells in rows.values()} == {"#,##0.00;(#,##0.00)"}


def test_sheet_names_fit_the_format_and_stay_distinct() -> None:
  model = _book_model()
  long = "Summary of Significant Accounting Policies: Revenue [Details]"
  twin = Network(
    role_uri=f"{ROLE}/2",
    definition=long.replace("Revenue", "Leases"),
    kind="presentation",
    arcs=list(model.networks[0].arcs),
  )
  model.networks[0].definition = long
  model.networks[1].definition = long
  model.networks.append(twin)
  names = _open(to_workbook(model, timestamp=WHEN)).sheetnames[1:]
  assert len(names) == len(set(names)) == 2
  assert all(len(name) <= 31 and not set(name) & set("[]:*?/\\") for name in names)
  assert names[1].endswith("(2)")


def test_the_cli_writes_the_workbook_beside_the_holon(
  tmp_path: Path, holon_text: str, capsys: pytest.CaptureFixture[str]
) -> None:
  source = tmp_path / "acme.holon.jsonld"
  source.write_text(holon_text, encoding="utf-8")
  assert main(["workbook", str(source)]) == 0
  out = tmp_path / "acme.xlsx"
  assert "wrote" in capsys.readouterr().out
  book = openpyxl.load_workbook(out)
  assert book.sheetnames == [CONTENTS_SHEET, "Balance Sheet"]
  assert book[CONTENTS_SHEET]["B4"].value is not None

  assert main(["workbook", str(tmp_path / "absent.holon.jsonld")]) == 2
