"""Project a neutral ``XbrlModel`` into the XBRL property graph — node and
relationship tables, parquet files, and a single-filing LadybugDB database.

This is the projection the RoboSystems platform builds its shared ``sec``
graph from, expressed as a function of the model instead of a walk over
Arelle: the same tables (:mod:`xbrlkit.schema`), the same columns in the same
order, and the same ids. Every id is a UUID5 of ``kind:content`` against the
platform's namespace, with report-scoped ids (the report, its facts, its
dimensions, its structures) folded on the filing's EDGAR URL, so a filing
projected here and a filing ingested by the platform are the same rows —
which is what lets a single-filing ``.lbug`` stand in for the shared graph.

Three things differ from the platform's pipeline by design, all of them
enrichment rather than projection: text-block values stay inline (the
platform externalizes them to a CDN and stores the URL), ``Element`` and
``Structure`` carry no canonical concept or type (the platform's embedding
pass assigns them), and ``FactSet`` / ``Classification`` are empty (the
platform's association classifier fills them).

Association ids are the one place the platform is not deterministic (it
mints a random UUID per arc); here they hash the arc's content so a rebuild
is byte-identical.
"""

from __future__ import annotations

import shutil
import tempfile
import uuid
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path, PurePath
from typing import Any

from ..model import Concept, Network, Period, XbrlFact, XbrlModel
from ..schema import (
  BOOLEAN,
  DOUBLE,
  INT32,
  INT64,
  NODE_TABLES,
  REL_TABLES,
  STRING,
  NodeTable,
  RelTable,
  ddl,
  node_table,
  rel_table,
  type_default,
)

# The RoboSystems platform's UUID5 namespace (``robosystems.utils.uuid``).
# Ids minted against it are the platform's ids; this projection shares them
# on purpose, where the holon / TAVI / OIM projections keep xbrlkit's own.
PLATFORM_NAMESPACE = uuid.UUID("a1b2c3d4-e5f6-7890-abcd-ef1234567890")

# What the platform stamps on ``Report.xbrl_processor_version``.
XBRL_GRAPH_PROCESSOR_VERSION = "1.0.0"

ISO_8601_URI = "http://www.w3.org/2001/XMLSchema#dateTime"
CIK_SCHEME = "http://www.sec.gov/CIK"
PARENT_CHILD = "http://www.xbrl.org/2003/arcrole/parent-child"
SUMMATION_ITEM = "http://www.xbrl.org/2003/arcrole/summation-item"

Row = dict[str, Any]


def graph_id(kind: str, content: str) -> str:
  """The platform's deterministic id: ``uuid5(namespace, "kind:content")``."""
  return str(uuid.uuid5(PLATFORM_NAMESPACE, f"{kind}:{content}"))


def parse_structure_definition(
  definition: str | None,
) -> tuple[str | None, str | None, str | None]:
  """Split a role definition into ``(number, type, name)``.

  ``"0001001 - Statement - CONSOLIDATED BALANCE SHEETS"`` → ``("0001001",
  "Statement", "CONSOLIDATED BALANCE SHEETS")``. A doubled type
  (``"… - Disclosure - Disclosure - …"``) is collapsed; a definition without
  the two separators is returned whole as the name.
  """
  if not definition or not definition.strip():
    return (None, None, None)
  parts = definition.split(" - ")
  if len(parts) < 3:
    return (None, None, definition.strip() or None)
  number = parts[0].strip() or None
  type_part = parts[1].strip() or None
  remaining = parts[2:]
  if remaining and type_part and remaining[0].strip() == type_part:
    remaining = remaining[1:]
  name = " - ".join(remaining).strip() or None
  return (number, type_part, name)


@dataclass
class GraphTables:
  """The projected rows, one list per table, keyed by table name in schema
  order. Every table is present; a table the filing does not fill is empty."""

  nodes: dict[str, list[Row]] = field(default_factory=dict)
  relationships: dict[str, list[Row]] = field(default_factory=dict)

  def __post_init__(self) -> None:
    for table in NODE_TABLES:
      self.nodes.setdefault(table.name, [])
    for table in REL_TABLES:
      self.relationships.setdefault(table.name, [])

  def counts(self) -> dict[str, int]:
    return {
      **{name: len(rows) for name, rows in self.nodes.items() if rows},
      **{name: len(rows) for name, rows in self.relationships.items() if rows},
    }


class _Projection:
  """One pass over the model, accumulating rows with the platform's rules."""

  def __init__(self, model: XbrlModel) -> None:
    self.model = model
    self.tables = GraphTables()
    filing = model.filing
    self.accession = filing.accession
    self.report_uri = filing.report_uri or filing.accession
    self.cik = _normalize_cik(model.entity.cik)
    self.taxonomy_id: str | None = None
    self.report_id: str | None = None
    self.entity_id: str | None = None
    self._elements: dict[str, str] = {}  # qname → element id
    self._entities: set[str] = set()  # uris
    self._periods: set[str] = set()
    self._units: set[str] = set()
    self._dimensions: dict[tuple[str, str, str], str] = {}
    self._facts: set[str] = set()
    self._labels: set[str] = set()
    self._references: set[str] = set()
    self._associations: set[str] = set()

  # ---- rows ---------------------------------------------------------------

  def _node(self, table: str, **values: Any) -> Row:
    spec = node_table(table)
    row = {p.name: values.get(p.name, type_default(p.type)) for p in spec.properties}
    self.tables.nodes[table].append(row)
    return row

  def _rel(self, table: str, from_id: str, to_id: str, **values: Any) -> Row:
    spec = rel_table(table)
    row: Row = {"from": from_id, "to": to_id}
    for p in spec.properties:
      row[p.name] = values.get(p.name, type_default(p.type))
    self.tables.relationships[table].append(row)
    return row

  # ---- the walk -------------------------------------------------------------

  def run(self) -> GraphTables:
    self._entity()
    self._report()
    self._taxonomy()
    self._structures()
    for fact in self.model.facts:
      self._fact(fact)
    return self.tables

  def _entity(self) -> None:
    entity = self.model.entity
    uri = f"{CIK_SCHEME}#{self.cik}"
    self.entity_id = graph_id("entity", uri)
    values: Row = {
      "identifier": self.entity_id,
      "uri": uri,
      "scheme": CIK_SCHEME,
      "cik": self.cik,
      "ticker": entity.ticker,
      "name": entity.name,
      "legal_name": entity.legal_name or entity.name,
      "industry": entity.sic_description if entity.sic else None,
      "entity_type": entity.entity_type,
      "sic": entity.sic,
      "sic_description": entity.sic_description,
      "category": entity.category,
      "state_of_incorporation": entity.state_of_incorporation,
      "fiscal_year_end": entity.fiscal_year_end,
      "tax_id": entity.ein.zfill(9) if entity.ein else None,
      "website": entity.website,
      "status": "active",
      "is_parent": True,
      "parent_entity_id": None,
      "created_at": None,
      "updated_at": None,
    }
    # The platform sets these three only when EDGAR reports them, so an
    # absent one lands as the STRING default rather than null.
    if entity.exchange:
      values["exchange"] = entity.exchange
    if entity.phone:
      values["phone"] = entity.phone
    self._node("Entity", **values)
    self._entities.add(uri)

  def _report(self) -> None:
    filing = self.model.filing
    self.report_id = graph_id("report", self.report_uri)
    self._node(
      "Report",
      identifier=self.report_id,
      uri=self.report_uri,
      name=filing.form,
      accession_number=filing.accession,
      form=filing.form,
      filing_date=_iso(filing.filing_date),
      report_date=_iso(filing.report_date),
      acceptance_date=(filing.acceptance_datetime or None)
      and filing.acceptance_datetime[:10],
      is_inline_xbrl=filing.is_inline_xbrl,
      xbrl_processor_version=XBRL_GRAPH_PROCESSOR_VERSION,
      processed=False,
      failed=False,
      fiscal_year_focus=_int(filing.fiscal_year_focus),
      fiscal_period_focus=filing.fiscal_period_focus,
      fiscal_year_end_month=_int(filing.fiscal_year_end_month),
    )
    assert self.entity_id is not None
    self._rel("ENTITY_HAS_REPORT", self.entity_id, self.report_id)

  def _taxonomy(self) -> None:
    namespace = self.model.filing.extension_namespace
    if not namespace or self.report_id is None:
      return
    self.taxonomy_id = graph_id("taxonomy", namespace)
    self._node("Taxonomy", identifier=self.taxonomy_id, uri=namespace)
    self._rel("REPORT_USES_TAXONOMY", self.report_id, self.taxonomy_id)

  # ---- elements -----------------------------------------------------------

  def _element(self, qname: str) -> str | None:
    """The element id for a concept, emitting its row, labels and references
    the first time it is seen."""
    if qname in self._elements:
      return self._elements[qname]
    concept = self.model.concepts.get(qname)
    if concept is None:
      return None
    uri = f"{concept.namespace}#{concept.name}"
    element_id = graph_id("element", uri)
    self._elements[qname] = element_id
    self._node(
      "Element",
      identifier=element_id,
      uri=uri,
      qname=concept.qname,
      name=concept.name,
      period_type=concept.period_type,
      type=concept.nice_type,
      balance=concept.balance,
      is_abstract=concept.is_abstract,
      is_dimension_item=concept.is_dimension_item,
      is_domain_member=concept.is_domain_member,
      is_hypercube_item=concept.is_hypercube_item,
      is_integer=concept.is_integer,
      is_numeric=concept.is_numeric,
      is_shares=concept.is_shares,
      is_fraction=concept.is_fraction,
      is_textblock=concept.is_textblock,
      substitution_group=_qname_uri(
        concept.substitution_group, concept.substitution_group_namespace
      ),
      item_type=_qname_uri(concept.item_type_qname, concept.item_type_namespace),
    )
    self._labels_and_references(concept, element_id, uri)
    return element_id

  def _labels_and_references(self, concept: Concept, element_id: str, uri: str) -> None:
    for label in concept.labels:
      label_id = graph_id("label", f"{label.value}#{label.role}#{label.language}")
      if label_id not in self._labels:
        self._labels.add(label_id)
        self._node(
          "Label",
          identifier=label_id,
          value=label.value,
          type=label.role,
          language=label.language,
        )
      self._rel("ELEMENT_HAS_LABEL", element_id, label_id)
      if self.taxonomy_id is not None:
        self._rel("TAXONOMY_HAS_LABEL", self.taxonomy_id, label_id, element_uri=uri)
    for reference in concept.references:
      reference_id = graph_id("reference", f"{reference.value}#{reference.role}")
      if reference_id not in self._references:
        self._references.add(reference_id)
        self._node(
          "Reference",
          identifier=reference_id,
          value=reference.value,
          type=reference.role,
        )
      self._rel("ELEMENT_HAS_REFERENCE", element_id, reference_id)
      if self.taxonomy_id is not None:
        self._rel("TAXONOMY_HAS_REFERENCE", self.taxonomy_id, reference_id)

  # ---- structures and associations ------------------------------------------

  def _structures(self) -> None:
    namespace = self.model.filing.extension_namespace
    if not namespace:
      return
    by_role: dict[str, list[Network]] = {}
    for network in self.model.networks:
      if not network.role_id or not network.arcs:
        continue
      by_role.setdefault(network.role_uri, []).append(network)

    for role_uri, networks in by_role.items():
      role_id = networks[0].role_id
      structure_uri = f"{namespace}#{role_id}"
      structure_id = graph_id(
        "structure", f"structure:{self.accession}#{structure_uri}"
      )
      definition = networks[0].definition or ""
      number, network_type, name = parse_structure_definition(definition)
      self._node(
        "Structure",
        identifier=structure_id,
        uri=structure_uri,
        network_uri=role_uri,
        definition=definition,
        number=number,
        type=network_type,
        name=name,
      )
      if self.taxonomy_id is not None:
        self._rel("STRUCTURE_HAS_TAXONOMY", structure_id, self.taxonomy_id)
      for network in networks:
        for arc in network.arcs:
          self._association(structure_id, structure_uri, network, arc)

  def _association(
    self, structure_id: str, structure_uri: str, network: Network, arc: Any
  ) -> None:
    parent_id = self._element(arc.from_qname)
    child_id = self._element(arc.to_qname)
    arcrole = arc.arcrole or ""
    order_value = float(arc.order) if arc.order is not None else None
    association_id = graph_id(
      "association",
      f"{structure_uri}#{arcrole}#{arc.from_qname}#{arc.to_qname}"
      f"#{order_value}#{arc.preferred_label}",
    )
    if association_id in self._associations:
      return
    self._associations.add(association_id)
    if arcrole == PARENT_CHILD:
      association_type = "Presentation"
    elif arcrole == SUMMATION_ITEM:
      association_type = "Calculation"
    else:
      association_type = "Other"
    self._node(
      "Association",
      identifier=association_id,
      arcrole=arcrole,
      order_value=order_value,
      association_type=association_type,
      weight=float(arc.weight)
      if arcrole == SUMMATION_ITEM and arc.weight is not None
      else None,
      root=arc.is_root,
      preferred_label=arc.preferred_label,
    )
    if parent_id and child_id:
      self._rel("ASSOCIATION_HAS_FROM_ELEMENT", association_id, parent_id)
      self._rel("ASSOCIATION_HAS_TO_ELEMENT", association_id, child_id)
    self._rel("STRUCTURE_HAS_ASSOCIATION", structure_id, association_id)

  # ---- facts ----------------------------------------------------------------

  def _fact(self, fact: XbrlFact) -> None:
    fact_uri = f"{self.report_uri}#fact-{fact.source_hash or fact.id}"
    fact_id = graph_id("fact", fact_uri)
    if fact_id in self._facts:
      return
    self._facts.add(fact_id)
    is_numeric = fact.unit_id is not None
    self._node(
      "Fact",
      identifier=fact_id,
      uri=fact_uri,
      value=fact.raw_value if fact.raw_value is not None else fact.value_str,
      numeric_value=fact.numeric_value if is_numeric else None,
      fact_type="Numeric" if is_numeric else "Nonnumeric",
      decimals=fact.decimals if is_numeric else None,
      value_type="inline",
      content_type=None,
      has_dimensions=bool(fact.dims),
      dimension_count=len(fact.dims),
    )
    assert self.report_id is not None
    self._rel("REPORT_HAS_FACT", self.report_id, fact_id)

    if is_numeric:
      unit_id = self._unit(fact.unit_id)
      if unit_id:
        self._rel("FACT_HAS_UNIT", fact_id, unit_id)

    for dim in fact.dims:
      dimension_id = self._dimension(dim)
      if dimension_id:
        self._rel("FACT_HAS_DIMENSION", fact_id, dimension_id)

    self._rel("FACT_HAS_ENTITY", fact_id, self._context_entity(fact))

    element_id = self._element(fact.concept_qname)
    if element_id:
      self._rel("FACT_HAS_ELEMENT", fact_id, element_id)

    period_id = self._period(fact.period_id)
    if period_id:
      self._rel("FACT_HAS_PERIOD", fact_id, period_id)

  def _unit(self, model_unit_id: str | None) -> str | None:
    unit = next((u for u in self.model.units if u.id == model_unit_id), None)
    if unit is None or not unit.uri:
      return None
    unit_id = graph_id("unit", unit.uri)
    if unit_id not in self._units:
      self._units.add(unit_id)
      if unit.numerator_uri and unit.denominator_uri:
        value = f"{_fragment(unit.numerator_uri)}/{_fragment(unit.denominator_uri)}"
      else:
        value = _fragment(unit.uri)
      self._node(
        "Unit",
        identifier=unit_id,
        uri=unit.uri,
        measure=unit.measure,
        value=value,
        numerator_uri=unit.numerator_uri,
        denominator_uri=unit.denominator_uri,
      )
    return unit_id

  def _dimension(self, dim: Any) -> str | None:
    axis = self.model.concepts.get(dim.axis_qname)
    if axis is None:
      return None
    axis_uri = f"{axis.namespace}#{axis.name}"
    axis_type = dim.axis_type or "unknown"
    if dim.is_explicit:
      member = self.model.concepts.get(dim.member_qname or "")
      if member is None:
        return None
      member_uri = f"{member.namespace}#{member.name}"
      key = (axis_uri, member_uri, axis_type)
      if key in self._dimensions:
        return self._dimensions[key]
      dimension_id = graph_id(
        "dimension", f"{self.report_uri}#dimension-{axis_uri}-{member_uri}"
      )
      self._dimensions[key] = dimension_id
      self._node(
        "Dimension",
        identifier=dimension_id,
        axis=axis.name,
        member=member.name,
        dimension_type="xbrl_explicit",
        axis_uri=axis_uri,
        member_uri=member_uri,
        type=axis_type,
        is_explicit=True,
        is_typed=False,
      )
      axis_element = self._element(dim.axis_qname)
      member_element = self._element(dim.member_qname or "")
      if axis_element:
        self._rel("DIMENSION_HAS_AXIS_ELEMENT", dimension_id, axis_element)
      if member_element:
        self._rel("DIMENSION_HAS_MEMBER_ELEMENT", dimension_id, member_element)
      return dimension_id

    typed = dim.typed_value if dim.typed_value is not None else ""
    key = (axis_uri, typed, axis_type)
    if key in self._dimensions:
      return self._dimensions[key]
    dimension_id = graph_id(
      "dimension", f"{self.report_uri}#dimension-{axis_uri}-typed-{typed}"
    )
    self._dimensions[key] = dimension_id
    self._node(
      "Dimension",
      identifier=dimension_id,
      axis=axis.name,
      member=typed,
      dimension_type="xbrl_typed",
      axis_uri=axis_uri,
      member_uri=typed,
      type=axis_type,
      is_explicit=False,
      is_typed=True,
    )
    axis_element = self._element(dim.axis_qname)
    if axis_element:
      self._rel("DIMENSION_HAS_AXIS_ELEMENT", dimension_id, axis_element)
    return dimension_id

  def _context_entity(self, fact: XbrlFact) -> str:
    """The filer for its own contexts; a subsidiary row for any other."""
    assert self.entity_id is not None
    normalized = _normalize_cik(fact.entity_identifier or fact.entity_cik)
    if normalized == self.cik:
      return self.entity_id
    scheme = fact.entity_scheme or self.model.entity.scheme
    uri = f"{scheme}#{normalized}"
    entity_id = graph_id("entity", uri)
    if uri not in self._entities:
      self._entities.add(uri)
      self._node(
        "Entity",
        identifier=entity_id,
        uri=uri,
        scheme=scheme,
        cik=normalized if normalized.isdigit() else None,
        name=fact.entity_identifier or normalized,
        is_parent=False,
        parent_entity_id=self.entity_id,
        entity_type="subsidiary",
      )
    return entity_id

  def _period(self, model_period_id: str) -> str | None:
    period = next((p for p in self.model.periods if p.id == model_period_id), None)
    if period is None:
      return None
    uri = _period_uri(period)
    period_id = graph_id("period", uri)
    if period_id not in self._periods:
      self._periods.add(period_id)
      if period.period_type == "instant":
        days: int | None = 0
        key = period.calendar_period_key or _iso(period.end)
      elif period.period_type == "duration" and period.start and period.end:
        days = (period.end - period.start).days + 1
        key = period.calendar_period_key
      else:
        days = None
        key = "forever"
      self._node(
        "Period",
        identifier=period_id,
        uri=uri,
        start_date=_iso(period.start),
        end_date=_iso(period.end),
        calendar_year=period.calendar_year,
        calendar_quarter=period.calendar_quarter,
        days_in_period=days,
        period_type=period.period_type,
        duration_type=period.duration_type,
        calendar_period_key=key,
      )
    return period_id


def to_graph_tables(model: XbrlModel) -> GraphTables:
  """Project ``model`` into the XBRL property graph's rows."""
  return _Projection(model).run()


# ---- parquet ----------------------------------------------------------------


def _arrow_type(type_: str) -> Any:
  import pyarrow as pa

  return {
    STRING: pa.string(),
    INT32: pa.int32(),
    INT64: pa.int64(),
    DOUBLE: pa.float64(),
    BOOLEAN: pa.bool_(),
  }[type_]


def _column(values: list[Any], type_: str) -> Any:
  """An arrow array for one column. A STRING column that carries booleans
  (``Association.root``) is written as booleans, as the platform writes it;
  LadybugDB stringifies them on load."""
  import pyarrow as pa

  present = [v for v in values if v is not None]
  if type_ == STRING and present and all(isinstance(v, bool) for v in present):
    return pa.array(values, pa.bool_())
  if type_ == STRING:
    return pa.array([None if v is None else str(v) for v in values], pa.string())
  if type_ == DOUBLE:
    return pa.array([None if v is None else float(v) for v in values], pa.float64())
  if type_ in (INT32, INT64):
    return pa.array([None if v is None else int(v) for v in values], _arrow_type(type_))
  return pa.array([None if v is None else bool(v) for v in values], pa.bool_())


def _table(spec: NodeTable | RelTable, rows: list[Row]) -> Any:
  import pyarrow as pa

  columns: dict[str, Any] = {}
  if isinstance(spec, RelTable):
    columns["from"] = pa.array([r["from"] for r in rows], pa.string())
    columns["to"] = pa.array([r["to"] for r in rows], pa.string())
  for prop in spec.properties:
    columns[prop.name] = _column([r.get(prop.name) for r in rows], prop.type)
  return pa.table(columns)


def write_parquet(tables: GraphTables, out_dir: Path) -> list[Path]:
  """Write the non-empty tables as ``nodes/<Name>.parquet`` and
  ``relationships/<NAME>.parquet``, columns in schema order."""
  import pyarrow.parquet as pq

  written: list[Path] = []
  for subdir, specs, rows_by_name in (
    ("nodes", NODE_TABLES, tables.nodes),
    ("relationships", REL_TABLES, tables.relationships),
  ):
    for spec in specs:
      rows = rows_by_name.get(spec.name) or []
      if not rows:
        continue
      path = out_dir / subdir / f"{spec.name}.parquet"
      path.parent.mkdir(parents=True, exist_ok=True)
      with open(path, "wb") as handle:
        pq.write_table(_table(spec, rows), handle)
      written.append(path)
  return written


# ---- LadybugDB ----------------------------------------------------------------


def build_lbug(tables: GraphTables, path: Path) -> Path:
  """Create a LadybugDB database at ``path`` holding ``tables``.

  Requires the ``lpg`` extra (``pip install "xbrlkit[lpg]"``). An existing
  database at ``path`` is replaced. The schema is created in full, nodes are
  loaded before relationships, and every load is a positional ``COPY FROM`` a
  parquet file written in schema column order.
  """
  try:
    import ladybug as lbug
  except ImportError as exc:  # pragma: no cover - depends on the extra
    raise ImportError(
      "building a .lbug needs the ladybug package: pip install 'xbrlkit[lpg]'"
    ) from exc

  path = Path(path)
  if path.exists():
    if path.is_dir():
      shutil.rmtree(path)
    else:
      path.unlink()
  for sidecar in (
    path.with_name(path.name + ".wal"),
    path.with_name(path.name + ".lock"),
  ):
    if sidecar.exists():
      sidecar.unlink()
  path.parent.mkdir(parents=True, exist_ok=True)

  with tempfile.TemporaryDirectory(prefix="xbrlkit-lpg-") as tmp:
    parquet_files = {p.stem: p for p in write_parquet(tables, Path(tmp))}
    db = lbug.Database(str(path))
    conn = lbug.Connection(db)
    try:
      for statement in ddl():
        conn.execute(statement)
      for spec in (*NODE_TABLES, *REL_TABLES):
        parquet = parquet_files.get(spec.name)
        if parquet is None:
          continue
        conn.execute(copy_statement(spec.name, parquet))
    finally:
      conn.close()
      db.close()
  return path


def copy_statement(table: str, parquet: PurePath) -> str:
  """The ``COPY FROM`` that loads one parquet file into ``table``.

  The path goes in as posix: LadybugDB reads a string literal's backslashes as
  escape sequences, so a Windows path passed verbatim is a parser error.
  """
  return f'COPY {table} FROM "{parquet.as_posix()}"'


# ---- helpers ------------------------------------------------------------------


def _normalize_cik(raw: str | None) -> str:
  text = str(raw or "")
  if text.isdigit():
    return text.lstrip("0").zfill(10)
  return text


def _iso(value: date | None) -> str | None:
  return value.isoformat() if value is not None else None


def _int(value: str | int | None) -> int | None:
  if value is None or value == "":
    return None
  try:
    return int(value)
  except (TypeError, ValueError):
    return None


def _qname_uri(qname: str | None, namespace: str | None) -> str | None:
  """``namespace#localName`` for a prefixed qname, as the platform stores
  substitution groups and item types."""
  if not qname:
    return None
  local = qname.split(":")[-1]
  return f"{namespace}#{local}" if namespace else local


def _fragment(uri: str) -> str:
  return uri.rsplit("#", 1)[-1]


def _period_uri(period: Period) -> str:
  if period.period_type == "instant":
    return f"{ISO_8601_URI}#{_iso(period.end)}"
  if period.period_type == "duration":
    return f"{ISO_8601_URI}#{_iso(period.start)}/{_iso(period.end)}"
  return f"{ISO_8601_URI}#Forever"


__all__ = (
  "GraphTables",
  "PLATFORM_NAMESPACE",
  "XBRL_GRAPH_PROCESSOR_VERSION",
  "build_lbug",
  "graph_id",
  "parse_structure_definition",
  "to_graph_tables",
  "write_parquet",
)
