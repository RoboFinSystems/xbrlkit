"""A report's sections as rows and period columns: the pivot a file is drawn from.

Each section (an information block) is walked in presentation order. A row is
a concept the section presents; a column is a period. Under a row sit its
breakdowns, one row per member of the section's own axes.

Balances are read into the period they open or close, as a statement prints
them. A duration column shows a balance dated at its end, and a row presented
with a ``periodStart`` label shows the balance dated the day before its start.
A date gets a column of its own only when a fact on it is shown nowhere else.

Nothing is capped here. A writer that has to bound a table does so itself and
says what it left out.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Literal

from ..information_block import InformationBlock, fact_membership, plan_blocks
from ..model import Arc, Concept, Period, XbrlFact, XbrlModel

TOTAL_ROLES = ("totalLabel", "periodEndLabel", "netLabel")
# A period with fewer than this share of the fullest period's facts is a stray
# comparative (one prior-year balance in a note), not a column of the section.
# The same rule, and the same numbers, as report-components' ``buildPivot``.
SPARSE_PERIOD_SHARE = 0.2
SPARSE_PERIOD_FLOOR = 2
SPARSE_PERIOD_MIN_FULLEST = 5
MAX_DEPTH = 14

Binding = Literal["start", "end"]
DimsKey = tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class PivotColumn:
  """One period column. Periods that state the same dates share a column."""

  key: str
  kind: Literal["duration", "instant", "forever"]
  start: date | None
  end: date | None
  period_ids: tuple[str, ...]

  @property
  def label(self) -> str:
    if self.kind == "forever":
      return "forever"
    end = self.end.isoformat() if self.end else "?"
    if self.kind == "instant":
      return end
    return f"{self.start.isoformat() if self.start else '?'} to {end}"


@dataclass
class PivotMember:
  """One breakdown of a row: the same concept for one member of each axis."""

  dims: DimsKey
  label: str
  values: dict[str, XbrlFact] = field(default_factory=dict)


@dataclass
class PivotRow:
  depth: int
  label: str
  qname: str
  abstract: bool = False
  total: bool = False
  binding: Binding | None = None
  # Presented with a negated label: a statement prints the value with its
  # sign reversed. ``values`` holds the facts as stated; the writer reverses.
  negated: bool = False
  values: dict[str, XbrlFact] = field(default_factory=dict)
  members: list[PivotMember] = field(default_factory=list)


@dataclass
class PivotSection:
  block: InformationBlock
  title: str
  columns: list[PivotColumn]
  rows: list[PivotRow]
  # Every fact that belongs to the section, and how many of them a cell shows.
  facts: int
  shown: int
  axes: list[str]


def precision(decimals: str | None) -> float:
  if decimals is None:
    return float("-inf")
  if decimals.upper() == "INF":
    return float("inf")
  try:
    return float(decimals)
  except ValueError:
    return float("-inf")


def label_for(concept: Concept | None, qname: str, role: str | None = None) -> str:
  """The concept's label in ``role`` when it has one, else its own label."""
  if concept is None:
    return qname.split(":")[-1]
  if role:
    for label in concept.labels:
      if label.role == role and label.value:
        return label.value
  return concept.pref_label or concept.name


def period_label(period: Period) -> str:
  if period.period_type == "instant":
    return period.end.isoformat() if period.end else period.id
  if period.period_type == "forever":
    return "forever"
  start = period.start.isoformat() if period.start else "?"
  end = period.end.isoformat() if period.end else "?"
  return f"{start} to {end}"


def pivot_sections(
  model: XbrlModel, *, drop_sparse_periods: bool = True
) -> list[PivotSection]:
  """Every section of the report that holds a fact, in the report's order."""
  blocks = [b for b in plan_blocks(model) if b.renderable]
  membership = fact_membership(model, blocks)

  def producer_order(block: InformationBlock) -> int | None:
    orders = [
      n.structure_order for n in block.presentation if n.structure_order is not None
    ]
    return min(orders) if orders else None

  # A producer that ordered its sections keeps its order; a filing keeps EDGAR's.
  if any(producer_order(b) is not None for b in blocks):
    blocks.sort(key=lambda b: (producer_order(b) is None, producer_order(b) or 0))

  periods = {p.id: p for p in model.periods}
  sections: list[PivotSection] = []
  for block in blocks:
    facts = membership.get(block.role_uri, [])
    if facts:
      sections.append(
        pivot_section(
          model, block, facts, periods, drop_sparse_periods=drop_sparse_periods
        )
      )
  return sections


def pivot_section(
  model: XbrlModel,
  block: InformationBlock,
  facts: list[XbrlFact],
  periods: dict[str, Period] | None = None,
  *,
  drop_sparse_periods: bool = True,
) -> PivotSection:
  """One section's facts laid out on its presentation tree.

  With ``drop_sparse_periods`` a period that only a stray fact or two is
  dated on gets no column; ``PivotSection.shown`` then falls short of
  ``facts`` by what those columns held.
  """
  periods = periods if periods is not None else {p.id: p for p in model.periods}
  axis_order = {axis.qname: i for i, axis in enumerate(block.axes)}
  member_order = {
    (axis.qname, member): i
    for axis in block.axes
    for i, member in enumerate(axis.members)
  }

  # concept → breakdown → period id → the fact.
  series: dict[str, dict[DimsKey, dict[str, XbrlFact]]] = defaultdict(
    lambda: defaultdict(dict)
  )
  for fact in _dedup(facts):
    if fact.period_id in periods:
      series[fact.concept_qname][_dims_key(fact, axis_order)][fact.period_id] = fact

  used = {pid for by_dims in series.values() for s in by_dims.values() for pid in s}
  durations, instants = _candidate_columns([periods[pid] for pid in used])
  if drop_sparse_periods:
    density: dict[str, int] = defaultdict(int)
    for by_dims in series.values():
      for by_period in by_dims.values():
        for pid, fact in by_period.items():
          if not fact.is_nil:
            density[pid] += 1
    fullest = max(
      (sum(density[i] for i in c.period_ids) for c in (*durations, *instants)),
      default=0,
    )
    if fullest >= SPARSE_PERIOD_MIN_FULLEST:
      floor = max(SPARSE_PERIOD_FLOOR, fullest * SPARSE_PERIOD_SHARE)
      dense = lambda c: sum(density[i] for i in c.period_ids) >= floor  # noqa: E731
      # A balance that closes a dropped period goes with it.
      dropped_ends = {c.end for c in durations if not dense(c)}
      durations = [c for c in durations if dense(c)]
      instants = [c for c in instants if dense(c) and c.end not in dropped_ends]
  instant_ids: dict[date, tuple[str, ...]] = {
    c.end: c.period_ids for c in instants if c.end is not None
  }

  skeleton = _walk(model, block, series)
  placed: set[str] = set()

  def place(
    row_series: dict[str, XbrlFact],
    binding: Binding | None,
    columns: list[PivotColumn],
  ) -> dict[str, XbrlFact]:
    out: dict[str, XbrlFact] = {}
    for column in columns:
      fact = _first(row_series, column.period_ids)
      if fact is None and column.kind == "duration":
        for day in _balance_dates(column, binding):
          fact = _first(row_series, instant_ids.get(day, ()))
          if fact is not None:
            break
      elif column.kind == "instant" and (binding == "start" or fact is None):
        continue
      if fact is not None:
        out[column.key] = fact
    return out

  def fill(columns: list[PivotColumn]) -> None:
    for row in skeleton:
      by_dims = series.get(row.qname, {}) if not row.abstract else {}
      row.values = place(by_dims.get((), {}), row.binding, columns)
      row.members = []
      for dims in sorted(
        (d for d in by_dims if d), key=lambda d: _member_rank(d, member_order)
      ):
        values = place(by_dims[dims], row.binding, columns)
        if values:
          row.members.append(
            PivotMember(dims=dims, label=_member_label(model, dims), values=values)
          )
      for values in (row.values, *(m.values for m in row.members)):
        placed.update(f.id for f in values.values())

  # First on the durations alone, to learn which balances they absorb; a date
  # whose facts all landed in a duration column needs no column of its own.
  fill(durations)
  shown_in_tree = {
    fact.id
    for row in skeleton
    if not row.abstract
    for by_period in series.get(row.qname, {}).values()
    for fact in by_period.values()
  }
  standalone = [
    c
    for c in instants
    if any(
      fact.id in shown_in_tree and fact.id not in placed
      for by_dims in series.values()
      for by_period in by_dims.values()
      for pid, fact in by_period.items()
      if pid in c.period_ids
    )
  ]
  columns = sorted([*durations, *standalone], key=_column_rank, reverse=True)
  placed.clear()
  fill(columns)

  rows = _prune(skeleton)
  axes = sorted({axis for by_dims in series.values() for d in by_dims for axis, _ in d})
  return PivotSection(
    block=block,
    title=block.name,
    columns=[c for c in columns if _column_used(c, rows)],
    rows=rows,
    facts=len(facts),
    shown=len(placed),
    axes=axes,
  )


# -- columns -------------------------------------------------------------------


def _candidate_columns(
  periods: list[Period],
) -> tuple[list[PivotColumn], list[PivotColumn]]:
  grouped: dict[tuple[Any, ...], list[Period]] = defaultdict(list)
  for period in periods:
    if period.period_type == "duration":
      grouped[("duration", period.start, period.end)].append(period)
    elif period.period_type == "instant":
      grouped[("instant", None, period.end)].append(period)
    else:
      grouped[("forever", None, None)].append(period)
  durations: list[PivotColumn] = []
  instants: list[PivotColumn] = []
  for (kind, start, end), members in grouped.items():
    ids = tuple(sorted(p.id for p in members))
    if kind == "duration":
      key = f"{start.isoformat() if start else '?'}..{end.isoformat() if end else '?'}"
    elif kind == "instant":
      key = end.isoformat() if end else ids[0]
    else:
      key = "forever"
    column = PivotColumn(key=key, kind=kind, start=start, end=end, period_ids=ids)
    (instants if kind == "instant" else durations).append(column)
  return durations, instants


def _column_rank(column: PivotColumn) -> tuple[date, int]:
  """Newest first; at one end date the longer span ahead of the balance."""
  span = (
    (column.end - column.start).days + 1
    if column.kind == "duration" and column.start and column.end
    else 0
  )
  return (column.end or date.min, span)


def _balance_dates(column: PivotColumn, binding: Binding | None) -> list[date]:
  """The dates of the balance a row shows in a duration column.

  XBRL dates an opening balance at the end of the day before the period
  starts; some producers date it on the first day instead.
  """
  if binding == "start":
    if column.start is None:
      return []
    return [column.start - timedelta(days=1), column.start]
  return [column.end] if column.end else []


def _column_used(column: PivotColumn, rows: list[PivotRow]) -> bool:
  return any(
    column.key in values
    for row in rows
    for values in (row.values, *(m.values for m in row.members))
  )


# -- rows ----------------------------------------------------------------------


def _walk(
  model: XbrlModel,
  block: InformationBlock,
  series: dict[str, dict[DimsKey, dict[str, XbrlFact]]],
) -> list[PivotRow]:
  """The presentation trees in order: a row per heading and per concept with a fact."""
  children: dict[str, list[Arc]] = defaultdict(list)
  targets: set[str] = set()
  for network in block.presentation:
    for arc in network.arcs:
      children[arc.from_qname].append(arc)
      targets.add(arc.to_qname)
  for arcs in children.values():
    arcs.sort(key=lambda a: a.order if a.order is not None else 0.0)
  roots = [q for q in children if q not in targets] or list(children)[:1]
  # A lone abstract root only repeats the section's name: start beneath it.
  top = model.concepts.get(roots[0]) if len(roots) == 1 else None
  skip_root = top is not None and top.is_abstract
  totals = {arc.from_qname for n in block.calculation for arc in n.arcs}
  # The cube's own scaffolding: a filer lists its domains and members in the
  # presentation tree, and none of them is a line.
  scaffolding: set[str] = set()
  for cube in block.hypercubes:
    scaffolding.add(cube.qname)
    for axis in cube.axes:
      scaffolding.add(axis.qname)
      if axis.domain:
        scaffolding.add(axis.domain)
      scaffolding.update(axis.members)

  rows: list[PivotRow] = []

  def visit(qname: str, depth: int, role: str | None, trail: tuple[str, ...]) -> None:
    concept = model.concepts.get(qname)
    reported = qname in series
    structural = not reported and (
      qname in scaffolding
      or (
        concept is not None and (concept.is_hypercube_item or concept.is_dimension_item)
      )
    )
    abstract = not reported and concept is not None and concept.is_abstract
    if not structural and (reported or abstract):
      rows.append(
        PivotRow(
          depth=depth,
          label=label_for(concept, qname, role),
          qname=qname,
          abstract=abstract,
          total=qname in totals or bool(role and role.endswith(TOTAL_ROLES)),
          binding=_binding(role),
          negated=bool(role and "negated" in role.rsplit("/", 1)[-1]),
        )
      )
    # An axis heads its domain and members, which are the breakdown rows.
    if concept is not None and concept.is_dimension_item:
      return
    if qname in trail or depth > MAX_DEPTH:
      return
    for arc in children.get(qname, []):
      visit(arc.to_qname, depth + 1, arc.preferred_label, (*trail, qname))

  for root in roots:
    if skip_root:
      for arc in children.get(root, []):
        visit(arc.to_qname, 0, arc.preferred_label, (root,))
    else:
      visit(root, 0, None, ())
  return rows


def _binding(role: str | None) -> Binding | None:
  if not role:
    return None
  name = role.rsplit("/", 1)[-1]
  if "periodStart" in name:
    return "start"
  if "periodEnd" in name:
    return "end"
  return None


def _prune(rows: list[PivotRow]) -> list[PivotRow]:
  """Drop a line with nothing to show, then a heading with nothing beneath it."""
  pruned = [row for row in rows if row.abstract or row.values or row.members]
  while True:
    kept = [
      row
      for index, row in enumerate(pruned)
      if not row.abstract
      or (index + 1 < len(pruned) and pruned[index + 1].depth > row.depth)
    ]
    if len(kept) == len(pruned):
      break
    pruned = kept
  # A tree whose headings were scaffolding starts at the margin all the same.
  shallowest = min((row.depth for row in kept), default=0)
  for row in kept:
    row.depth -= shallowest
  return kept


# -- facts ---------------------------------------------------------------------


def _dims_key(fact: XbrlFact, axis_order: dict[str, int]) -> DimsKey:
  return tuple(
    sorted(
      ((d.axis_qname, d.member_qname or d.typed_value or "") for d in fact.dims),
      key=lambda pair: (axis_order.get(pair[0], len(axis_order)), pair),
    )
  )


def _dedup(facts: list[XbrlFact]) -> list[XbrlFact]:
  """One fact per concept, period, unit and breakdown: the most precise."""
  best: dict[tuple[Any, ...], XbrlFact] = {}
  for fact in facts:
    dims = tuple(
      sorted((d.axis_qname, d.member_qname or d.typed_value or "") for d in fact.dims)
    )
    key = (fact.concept_qname, fact.period_id, fact.unit_id, dims, fact.language)
    current = best.get(key)
    if current is None or precision(fact.decimals) > precision(current.decimals):
      best[key] = fact
  return list(best.values())


def _first(
  row_series: dict[str, XbrlFact], period_ids: tuple[str, ...]
) -> XbrlFact | None:
  for period_id in period_ids:
    fact = row_series.get(period_id)
    if fact is not None:
      return fact
  return None


def _member_rank(
  dims: DimsKey, member_order: dict[tuple[str, str], int]
) -> tuple[tuple[int, str], ...]:
  return tuple((member_order.get(pair, len(member_order)), pair[1]) for pair in dims)


def _member_label(model: XbrlModel, dims: DimsKey) -> str:
  parts: list[str] = []
  for axis, member in dims:
    concept = model.concepts.get(member)
    if concept is not None:
      parts.append(label_for(concept, member))
    else:
      # A typed axis has a value where an explicit one has a member.
      parts.append(f"{label_for(model.concepts.get(axis), axis)}: {member}")
  return " | ".join(parts)
