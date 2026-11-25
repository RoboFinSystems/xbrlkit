"""Project a holon into a workbook: the report as an Excel file.

One sheet per section of the report, in the report's order, behind a contents
sheet that names the holon the workbook was written from, by IRI and by
digest. A row is a line the section presents, a column a period, and under a
line sit its breakdowns by the section's own axes (``pivot.py``).

Every number is the fact the holon states, written as a number. Nothing is
recomputed and no cell holds a formula, so a total is the reported total. The
roll-ups, the arrangement and the ledger stay in the holon; the contents sheet
says so.

Needs the ``xlsx`` extra (``pip install "xbrlkit[xlsx]"``).
"""

from __future__ import annotations

import hashlib
import html
import re
from datetime import UTC, datetime
from io import BytesIO
from typing import Any

from .. import __version__
from ..deserialize.holon import from_holon_report
from ..model import Unit, XbrlFact, XbrlModel
from . import read_graphs
from .pivot import PivotSection, pivot_sections

CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
EXTENSION = ".xlsx"
CONTENTS_SHEET = "Contents"

# What the format holds.
MAX_SHEET_NAME = 31
MAX_CELL_TEXT = 32_767
MAX_SHEET_ROWS = 1_048_576
MAX_SHEET_COLUMNS = 16_384
# A text fact is cut here, short of the cell limit, and the cell says so.
TEXT_LIMIT = 32_000
MAX_DECIMAL_PLACES = 6

# What a reader will not find in the workbook, said on its contents sheet.
OMITTED_CONTENT = (
  "calculation arcs and roll-up rules (the holon's #boundary graph); totals "
  "are the reported values, not formulas",
  "a line the report presents with a negated label shows its value with the "
  "sign reversed, as the statement prints it; the Sign column marks each",
  "a period only a stray fact or two is dated on has no column; Facts and "
  "Shown on this sheet differ by what such periods held",
  "the presentation and definition arcs themselves (the holon's #projection graph)",
  "fact provenance and the ledger behind the figures (the holon's #lineage graph)",
  "text facts longer than a cell holds are cut, and the cell says so",
)

_TAG = re.compile(r"<[^>]+>")
_SPACE = re.compile(r"\s+")
_SHEET_FORBIDDEN = re.compile(r"[\[\]:*?/\\]")
# Characters XML 1.0 cannot carry, which the format therefore cannot either.
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
# ``iso4217:USD`` reads as USD, ``xbrli:shares`` as shares.
_UNIT_PREFIX = re.compile(r"\b(?:iso4217|xbrli):")

_LABEL_WIDTH = 64
_VALUE_WIDTH = 18
_UNIT_WIDTH = 14
_CONCEPT_WIDTH = 52
_HEADER_ROW = 5


def workbook_from_holon(
  text: str,
  *,
  href: str | None = None,
  timestamp: datetime | None = None,
) -> bytes:
  """The workbook for the holon in ``text``, as the bytes of an ``.xlsx``.

  ``href`` is where a reader can get the holon (a file name beside the
  workbook, or a URL); the holon's IRI and its SHA-256 are written either way.
  ``timestamp`` defaults to now.
  """
  openpyxl = _openpyxl()
  model, _ = from_holon_report(text)
  _, roots = read_graphs(text)
  digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
  stamp = (timestamp or datetime.now(UTC)).astimezone(UTC).replace(microsecond=0)
  sections = pivot_sections(model)

  book = openpyxl.Workbook()
  contents = book.active
  contents.title = CONTENTS_SHEET
  names = _sheet_names(sections)
  truncated: dict[str, str] = {}
  for section, name in zip(sections, names, strict=True):
    note = _write_section(book.create_sheet(name), model, section)
    if note:
      truncated[name] = note
  _write_contents(
    contents,
    model=model,
    root=sorted(roots)[0] if roots else None,
    href=href,
    digest=digest,
    stamp=stamp,
    sections=sections,
    names=names,
    truncated=truncated,
  )

  title = _title(model)
  book.properties.title = title
  book.properties.creator = f"xbrlkit {__version__}"
  # The format wants naive datetimes, read as UTC.
  book.properties.created = stamp.replace(tzinfo=None)
  book.properties.modified = stamp.replace(tzinfo=None)
  out = BytesIO()
  book.save(out)
  return out.getvalue()


def to_workbook(model: XbrlModel, **kwargs: Any) -> bytes:
  """The workbook for ``model``: its holon written, then projected."""
  from ..serialize import to_holon

  return workbook_from_holon(to_holon(model), **kwargs)


def _openpyxl() -> Any:
  try:
    import openpyxl
  except ImportError as exc:  # pragma: no cover - exercised without the extra
    raise RuntimeError(
      'Writing a workbook needs openpyxl: pip install "xbrlkit[xlsx]"'
    ) from exc
  return openpyxl


# -- the contents sheet --------------------------------------------------------


def _write_contents(
  sheet: Any,
  *,
  model: XbrlModel,
  root: str | None,
  href: str | None,
  digest: str,
  stamp: datetime,
  sections: list[PivotSection],
  names: list[str],
  truncated: dict[str, str],
) -> None:
  from openpyxl.styles import Font
  from openpyxl.worksheet.hyperlink import Hyperlink

  bold = Font(bold=True)
  _text(sheet.cell(row=1, column=1), _title(model)).font = Font(bold=True, size=14)
  facts = [
    ("Entity", model.entity.name or model.entity.cik),
    ("Form", model.filing.form),
    (
      "Period ended",
      model.filing.report_date.isoformat() if model.filing.report_date else None,
    ),
    ("Holon", root),
    ("Holon file", href),
    ("Holon SHA-256", digest),
    ("Written by", f"xbrlkit {__version__}"),
    ("Written", stamp.strftime("%Y-%m-%dT%H:%M:%SZ")),
  ]
  row = 3
  for name, value in facts:
    if not value:
      continue
    _text(sheet.cell(row=row, column=1), name).font = bold
    _text(sheet.cell(row=row, column=2), str(value))
    row += 1

  row += 1
  for column, heading in enumerate(
    (
      "Sheet",
      "Section",
      "Level",
      "Lines",
      "Periods",
      "Facts",
      "Shown",
      "Broken out by",
    ),
    start=1,
  ):
    _text(sheet.cell(row=row, column=column), heading).font = bold
  link = Font(color="0563C1", underline="single")
  for section, name in zip(sections, names, strict=True):
    row += 1
    cell = _text(sheet.cell(row=row, column=1), name)
    # A jump inside the file. A bare "#…" string would be saved as an external link.
    cell.hyperlink = Hyperlink(
      ref=cell.coordinate, location=f"'{name.replace(chr(39), chr(39) * 2)}'!A1"
    )
    cell.font = link
    _text(sheet.cell(row=row, column=2), section.title)
    _text(sheet.cell(row=row, column=3), section.block.level)
    sheet.cell(row=row, column=4, value=len(section.rows))
    sheet.cell(row=row, column=5, value=len(section.columns))
    sheet.cell(row=row, column=6, value=section.facts)
    sheet.cell(row=row, column=7, value=section.shown)
    _text(sheet.cell(row=row, column=8), ", ".join(section.axes))

  row += 2
  _text(sheet.cell(row=row, column=1), "Not in this workbook").font = bold
  for line in (*OMITTED_CONTENT, *(f"{n}: {note}" for n, note in truncated.items())):
    row += 1
    _text(sheet.cell(row=row, column=1), line)
  row += 2
  _text(
    sheet.cell(row=row, column=1),
    "Facts counts every fact the section holds; Shown counts those a cell "
    "holds. They differ where a filing states one value more than once, "
    "states a fact the section's presentation does not list, or dates a "
    "stray fact on a period the section otherwise does not report.",
  )

  sheet.column_dimensions["A"].width = 34
  sheet.column_dimensions["B"].width = 72
  for letter in "CDEFG":
    sheet.column_dimensions[letter].width = 12
  sheet.column_dimensions["H"].width = 60


# -- a section's sheet ---------------------------------------------------------


def _write_section(sheet: Any, model: XbrlModel, section: PivotSection) -> str | None:
  """Draw one section. Returns what was left out when it outgrew a sheet."""
  from openpyxl.styles import Alignment, Border, Font, Side
  from openpyxl.utils import get_column_letter

  units = {u.id: _unit_label(u) for u in model.units}
  places = _unstated_places(section)
  columns = section.columns[: MAX_SHEET_COLUMNS - 3]
  value_columns = {column.key: index for index, column in enumerate(columns, start=2)}
  unit_column = len(columns) + 2
  concept_column = unit_column + 1
  # Only a section that reverses a sign has the column that says where.
  sign_column = concept_column + 1 if any(r.negated for r in section.rows) else None
  last_column = sign_column or concept_column

  _text(sheet.cell(row=1, column=1), section.title).font = Font(bold=True, size=13)
  grey = Font(color="666666")
  _text(sheet.cell(row=2, column=1), section.block.role_uri).font = grey
  if section.axes:
    axes = ", ".join(_axis_label(model, axis) for axis in section.axes)
    _text(sheet.cell(row=3, column=1), f"Broken out by: {axes}").font = grey

  thin = Side(style="thin")
  header = Font(bold=True)
  _text(sheet.cell(row=_HEADER_ROW, column=1), "Line item")
  for column in columns:
    _text(sheet.cell(row=_HEADER_ROW, column=value_columns[column.key]), column.label)
  _text(sheet.cell(row=_HEADER_ROW, column=unit_column), "Unit")
  _text(sheet.cell(row=_HEADER_ROW, column=concept_column), "Concept")
  if sign_column:
    _text(sheet.cell(row=_HEADER_ROW, column=sign_column), "Sign")
  for index in range(1, last_column + 1):
    cell = sheet.cell(row=_HEADER_ROW, column=index)
    cell.font = header
    cell.border = Border(bottom=thin)
    cell.alignment = Alignment(
      horizontal="right" if 1 < index < unit_column else "left", wrap_text=True
    )

  bold = Font(bold=True)
  italic = Font(italic=True, color="444444")
  total_border = Border(top=thin)
  at = _HEADER_ROW
  lines = 0

  def draw(
    label: str,
    depth: int,
    values: dict[str, XbrlFact],
    font: Any,
    concept: str,
    *,
    total: bool = False,
    negated: bool = False,
  ) -> None:
    nonlocal at
    at += 1
    cell = _text(sheet.cell(row=at, column=1), label)
    cell.alignment = Alignment(indent=min(depth, 15))
    if font is not None:
      cell.font = font
    shown_units: set[str] = set()
    for key, fact in values.items():
      index = value_columns.get(key)
      if index is None:
        continue
      target = sheet.cell(row=at, column=index)
      _value(target, fact, places.get(fact.unit_id, 0), negated=negated)
      if font is not None:
        target.font = font
      if total:
        target.border = total_border
      if fact.unit_id and fact.unit_id in units:
        shown_units.add(units[fact.unit_id])
    _text(sheet.cell(row=at, column=unit_column), ", ".join(sorted(shown_units)))
    _text(sheet.cell(row=at, column=concept_column), concept).font = grey
    if sign_column and negated:
      _text(sheet.cell(row=at, column=sign_column), "reversed").font = grey

  for row in section.rows:
    if at + 1 + len(row.members) > MAX_SHEET_ROWS:
      break
    lines += 1
    font = bold if row.abstract or row.total else None
    draw(
      row.label,
      row.depth,
      row.values,
      font,
      row.qname,
      total=row.total,
      negated=row.negated,
    )
    for member in row.members:
      breakdown = " | ".join(f"{axis}={value}" for axis, value in member.dims)
      draw(
        member.label,
        row.depth + 1,
        member.values,
        italic,
        breakdown,
        negated=row.negated,
      )

  sheet.freeze_panes = sheet.cell(row=_HEADER_ROW + 1, column=2)
  sheet.column_dimensions["A"].width = _LABEL_WIDTH
  for index in value_columns.values():
    sheet.column_dimensions[get_column_letter(index)].width = _VALUE_WIDTH
  sheet.column_dimensions[get_column_letter(unit_column)].width = _UNIT_WIDTH
  sheet.column_dimensions[get_column_letter(concept_column)].width = _CONCEPT_WIDTH
  if sign_column:
    sheet.column_dimensions[get_column_letter(sign_column)].width = _UNIT_WIDTH

  left: list[str] = []
  if lines < len(section.rows):
    left.append(f"{len(section.rows) - lines:,} lines past the sheet's last row")
  if len(columns) < len(section.columns):
    left.append(f"{len(section.columns) - len(columns):,} periods past its last column")
  return "; ".join(left) or None


def _unit_label(unit: Unit) -> str:
  """The unit's local name, read from its URI: a filer that binds its own
  prefix to the xbrli namespace (``i:shares``) still prints ``shares``."""
  if unit.numerator_uri and unit.denominator_uri:
    return f"{_local_name(unit.numerator_uri)}/{_local_name(unit.denominator_uri)}"
  if unit.uri:
    return _local_name(unit.uri)
  return _UNIT_PREFIX.sub("", unit.measure)


def _local_name(uri: str) -> str:
  return uri.rsplit("#", 1)[-1].rsplit("/", 1)[-1]


def _value(
  cell: Any, fact: XbrlFact, unstated_places: int = 0, *, negated: bool = False
) -> None:
  if fact.is_nil:
    _text(cell, "nil")
    return
  if fact.value_kind == "numeric" and fact.numeric_value is not None:
    # A line presented with a negated label prints the value reversed.
    cell.value = (
      -fact.numeric_value if negated and fact.numeric_value else fact.numeric_value
    )
    cell.number_format = _number_format(fact, unstated_places)
    return
  text = _SPACE.sub(" ", html.unescape(_TAG.sub(" ", fact.value_str or ""))).strip()
  if len(text) > TEXT_LIMIT:
    text = f"{text[:TEXT_LIMIT]} … [cut; {len(text):,} characters in the holon]"
  _text(cell, text)


def _stated_places(fact: XbrlFact) -> int | None:
  places = fact.decimals or ""
  if places.lstrip("-").isdigit():
    return min(max(int(places), 0), MAX_DECIMAL_PLACES)
  return None


def _unstated_places(section: PivotSection) -> dict[str | None, int]:
  """Per unit, the places to show a fact that states none (``INF``, or no
  ``decimals``): two when any such value in the section has a fraction, so a
  column of amounts reads evenly."""
  places: dict[str | None, int] = {}
  for row in section.rows:
    for values in (row.values, *(m.values for m in row.members)):
      for fact in values.values():
        value = fact.numeric_value
        if value is None or _stated_places(fact) is not None:
          continue
        places[fact.unit_id] = max(
          places.get(fact.unit_id, 0), 0 if value == int(value) else 2
        )
  return places


def _number_format(fact: XbrlFact, unstated_places: int = 0) -> str:
  """Separators, the places the fact is stated to, a negative in parentheses."""
  stated = _stated_places(fact)
  shown = unstated_places if stated is None else stated
  body = "#,##0" + (f".{'0' * shown}" if shown else "")
  return f"{body};({body})"


def _text(cell: Any, text: str | None) -> Any:
  """Write ``text`` as text. A label or a value that begins with ``=`` is not
  a formula: nothing in a holon is, and a workbook must not run one."""
  cell.value = _CONTROL.sub("", text or "")[:MAX_CELL_TEXT] or None
  if cell.value is not None:
    cell.data_type = "s"
  return cell


# -- names ---------------------------------------------------------------------


def _sheet_names(sections: list[PivotSection]) -> list[str]:
  """A sheet per section: its title, as far as a sheet's name allows."""
  used = {CONTENTS_SHEET.lower()}
  names: list[str] = []
  for section in sections:
    base = _SPACE.sub(" ", _SHEET_FORBIDDEN.sub(" ", section.title)).strip(" '")
    base = base or "Section"
    name = base[:MAX_SHEET_NAME].rstrip(" '")
    count = 1
    while name.lower() in used:
      count += 1
      suffix = f" ({count})"
      name = base[: MAX_SHEET_NAME - len(suffix)].rstrip(" '") + suffix
    used.add(name.lower())
    names.append(name)
  return names


def _axis_label(model: XbrlModel, axis: str) -> str:
  concept = model.concepts.get(axis)
  return (concept.pref_label or concept.name) if concept is not None else axis


def _title(model: XbrlModel) -> str:
  name = model.entity.name or model.entity.cik
  ends = [p.end for p in model.periods if p.end]
  ended = model.filing.report_date or (max(ends) if ends else None)
  parts = [
    part
    for part in (
      model.filing.form,
      f"for the period ended {ended.isoformat()}" if ended else None,
    )
    if part
  ]
  return f"{name}: {' '.join(parts)}" if parts else name
