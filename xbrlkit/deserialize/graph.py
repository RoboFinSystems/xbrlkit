"""Read the XBRL property graph back into the neutral ``XbrlModel``.

The inverse of :mod:`xbrlkit.serialize.lpg`. That projection writes a filing
as the node and relationship tables of :mod:`xbrlkit.schema` — the tables the
RoboSystems ``sec`` graph is built from, and the same tables a ledger's own
report materializes into — and this walks the rows back into one model. The
input is the rows, not a database: :func:`from_graph` takes the
:class:`~xbrlkit.serialize.lpg.GraphTables` the projection emits, and a
caller that holds the graph elsewhere runs :data:`SLICE_QUERIES` against it
and hands the rows over. :func:`read_lbug` does exactly that for a
single-filing ``.lbug`` file.

**Two producers fill these tables, and the reader keys on the columns, not
on either producer's habits.** A filing projected by ``to_graph_tables`` has
a role URI on every structure, an EDGAR definition, labels by role, and one
fact per distinct source hash. A ledger's materialization has none of those:
its structures are the producer's own (no role, a block type in ``type``, a
name where the definition would be), its associations carry the platform's
own kinds beside the XBRL ones, its facts sit in fact sets under those
structures, and it writes no labels at all. The rule both importers keep
holds here too — read what the rows carry and say what they did not — so a
structure without a role is read as the producer's (``structure_id``,
``block_type`` and ``fact_set_id`` set, the way an authored holon reads), and
one with a role is read as the filing's (those three left unset).

**What the graph carries, and what it does not.** Facts with their values,
decimals, units, periods, dimensions and entity; elements with every flag,
their labels by role and language, and their references; presentation,
calculation and definition associations with order, weight, preferred label
and root; structures by role with the EDGAR definition; the filing's
identity, inline flag included. Absent, and left empty:

- **an arc's ``targetRole``.** The ``Association`` table has no column for
  it, so a hypercube whose axes or members a filing declared in another
  role reads as that role's own arcs only.
- **fact language**, and the nil-versus-empty distinction where a writer
  stored an empty string for nil.
- **the processed ``value_str``.** The graph keeps one value — the raw
  lexical one when the parse had it — and it is read as both.
- **``is_text_fact``** for a concept that is not a text block, the
  acceptance *time* (the graph keeps the date), ``primary_document``, and
  the QName prefix of a type or substitution group whose namespace no
  concept of the filing binds.

Networks come back grouped one per role and linkbase kind, as the holon
reader groups them — a parse writes one per *arcrole* and role, so a
definition linkbase read from the graph is one network whose arcs each keep
their own arcrole, not five. Nothing downstream distinguishes the two
shapes; the block planner buckets definition arcs by arcrole itself.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from ..model import (
  Arc,
  Concept,
  DimQualifier,
  EntityIdentity,
  FilingMeta,
  Label,
  Network,
  NetworkKind,
  Period,
  Reference,
  Unit,
  XbrlFact,
  XbrlModel,
)
from ..namespaces import ENTITY_SCHEME
from ..parse.ids import unit_id
from ..periods import duration_period, forever_period, instant_period
from ..schema import NODE_TABLES, REL_TABLES
from ..serialize._values import CIK_SCHEME
from ..serialize.lpg import PARENT_CHILD, SUMMATION_ITEM, GraphTables

Row = Mapping[str, Any]
Tables = GraphTables | Mapping[str, Sequence[Row]]

DIMENSION_ARCROLE_BASE = "http://xbrl.org/int/dim/arcrole/"
# Arelle's aggregate base set over a role's whole dimensional linkbase. A
# parse that walked it wrote every dimensional arc a second time under this
# name, and the graph holds both copies; they are read as definition arcs
# with this arcrole, which the block planner's arcrole buckets ignore.
XBRL_DIMENSIONS = "XBRL-dimensions"
STANDARD_LABEL_ROLE = "http://www.xbrl.org/2003/role/label"

# The two linkbase arcroles the model documents as URIs, in the short form
# a producer may have written them.
SHORT_ARCROLES: dict[str, str] = {
  "parent-child": PARENT_CHILD,
  "summation-item": SUMMATION_ITEM,
}

# Association kinds the platform's ledger writes that are not linkbase
# networks — a chart's mapping onto a taxonomy, a trait's general-special
# tree, a derivation. The model has no network for them; they are counted.
NON_NETWORK_TYPES = frozenset(
  {"mapping", "general-special", "equivalence", "derivation", "has-part"}
)

# Namespaces a type or substitution group sits in that the filing's own
# concepts never bind a prefix for, under the prefixes the specifications
# themselves use. Anything else keeps its namespace and local name and no
# QName, rather than a guessed prefix.
STANDARD_PREFIXES: dict[str, str] = {
  "http://www.xbrl.org/2003/instance": "xbrli",
  "http://xbrl.org/2005/xbrldt": "xbrldt",
}


class GraphError(ValueError):
  """The rows are not one report's slice of the property graph."""


@dataclass
class ImportGaps:
  """What the graph could not supply, per field of the model."""

  missing: list[str] = field(default_factory=list)
  unresolved_references: int = 0
  # Facts whose value is a pointer to the text — the platform's
  # ``value_type`` ``external``, a CDN URL — rather than the text itself.
  external_values: int = 0
  # Associations of kinds the model has no network for, by kind.
  associations_skipped: dict[str, int] = field(default_factory=dict)

  def to_dict(self) -> dict[str, object]:
    return {
      "missing": sorted(self.missing),
      "unresolved_references": self.unresolved_references,
      "external_values": self.external_values,
      "associations_skipped": dict(sorted(self.associations_skipped.items())),
    }


def from_graph(tables: Tables) -> XbrlModel:
  """Read one report's rows of the property graph."""
  model, _ = _read(tables)
  return model


def from_graph_report(tables: Tables) -> tuple[XbrlModel, ImportGaps]:
  """Read one report's rows, returning the model and what they could not supply."""
  return _read(tables)


# -- the slice ------------------------------------------------------------------

_REPORT = (
  "MATCH (r:Report) WHERE r.identifier = $report OR r.uri = $report "
  "OR r.accession_number = $report"
)
_BY_TAXONOMY = "MATCH (r)-[:REPORT_USES_TAXONOMY]->(t:Taxonomy)<-[:STRUCTURE_HAS_TAXONOMY]-(s:Structure)"
_BY_FACT_SET = "MATCH (r)-[:REPORT_HAS_FACT_SET]->(fs:FactSet)<-[:STRUCTURE_HAS_FACT_SET]-(s:Structure)"
_ASSOCIATION = (
  "-[:STRUCTURE_HAS_ASSOCIATION]->(a:Association) "
  "OPTIONAL MATCH (a)-[:ASSOCIATION_HAS_FROM_ELEMENT]->(fe:Element) "
  "OPTIONAL MATCH (a)-[:ASSOCIATION_HAS_TO_ELEMENT]->(te:Element) "
  "RETURN a.*, s.identifier AS from__STRUCTURE_HAS_ASSOCIATION, "
  "fe.identifier AS to__ASSOCIATION_HAS_FROM_ELEMENT, "
  "te.identifier AS to__ASSOCIATION_HAS_TO_ELEMENT"
)
_ARC_ELEMENTS = (
  "-[:STRUCTURE_HAS_ASSOCIATION]->(:Association)"
  "-[:ASSOCIATION_HAS_FROM_ELEMENT|ASSOCIATION_HAS_TO_ELEMENT]->(e:Element) "
  "RETURN DISTINCT e.*"
)


@dataclass(frozen=True)
class SliceQuery:
  """One Cypher statement of a report's slice.

  A node query returns ``n.*`` for the rows of ``table``, plus edge columns:
  ``to__<REL>`` holds the id at the far end of a relationship the row is the
  ``from`` of, ``from__<REL>`` the id at the near end of one it is the ``to``
  of. A relationship query (``nodes`` false) returns ``src`` and ``dst``.
  """

  table: str
  cypher: str
  nodes: bool = True


# The report's rows, by ``$report`` — its identifier, its URI or its
# accession. Structures reach a report two ways and both are asked: through
# the taxonomy the report uses (a filing's own schema, so exact on ``sec``)
# and through the fact sets the report holds (a ledger's report, and the
# platform's enrichment on a filing). A row reached twice is kept once.
SLICE_QUERIES: tuple[SliceQuery, ...] = (
  SliceQuery("Report", f"{_REPORT} RETURN r.*"),
  SliceQuery(
    "Entity",
    f"{_REPORT} MATCH (e:Entity)-[:ENTITY_HAS_REPORT]->(r) "
    "RETURN e.*, r.identifier AS to__ENTITY_HAS_REPORT",
  ),
  SliceQuery(
    "Entity",
    f"{_REPORT} MATCH (r)-[:REPORT_HAS_FACT]->(:Fact)-[:FACT_HAS_ENTITY]->(e:Entity) "
    "RETURN DISTINCT e.*",
  ),
  SliceQuery(
    "Taxonomy",
    f"{_REPORT} MATCH (r)-[:REPORT_USES_TAXONOMY]->(t:Taxonomy) "
    "RETURN t.*, r.identifier AS from__REPORT_USES_TAXONOMY",
  ),
  SliceQuery(
    "Structure",
    f"{_REPORT} {_BY_TAXONOMY} RETURN DISTINCT s.*, "
    "t.identifier AS to__STRUCTURE_HAS_TAXONOMY",
  ),
  SliceQuery(
    "Structure",
    f"{_REPORT} {_BY_FACT_SET} RETURN DISTINCT s.*, "
    "fs.identifier AS to__STRUCTURE_HAS_FACT_SET",
  ),
  SliceQuery(
    "FactSet",
    f"{_REPORT} MATCH (r)-[:REPORT_HAS_FACT_SET]->(fs:FactSet) "
    "RETURN fs.*, r.identifier AS from__REPORT_HAS_FACT_SET",
  ),
  SliceQuery(
    "FACT_SET_CONTAINS_FACT",
    f"{_REPORT} MATCH (r)-[:REPORT_HAS_FACT_SET]->(fs:FactSet)"
    "-[:FACT_SET_CONTAINS_FACT]->(f:Fact) "
    "RETURN fs.identifier AS src, f.identifier AS dst",
    nodes=False,
  ),
  SliceQuery("Association", f"{_REPORT} {_BY_TAXONOMY}{_ASSOCIATION}"),
  SliceQuery("Association", f"{_REPORT} {_BY_FACT_SET}{_ASSOCIATION}"),
  SliceQuery(
    "Element",
    f"{_REPORT} MATCH (r)-[:REPORT_HAS_FACT]->(:Fact)-[:FACT_HAS_ELEMENT]->(e:Element) "
    "RETURN DISTINCT e.*",
  ),
  SliceQuery("Element", f"{_REPORT} {_BY_TAXONOMY}{_ARC_ELEMENTS}"),
  SliceQuery("Element", f"{_REPORT} {_BY_FACT_SET}{_ARC_ELEMENTS}"),
  SliceQuery(
    "Element",
    f"{_REPORT} MATCH (r)-[:REPORT_HAS_FACT]->(:Fact)-[:FACT_HAS_DIMENSION]->(:Dimension)"
    "-[:DIMENSION_HAS_AXIS_ELEMENT|DIMENSION_HAS_MEMBER_ELEMENT]->(e:Element) "
    "RETURN DISTINCT e.*",
  ),
  SliceQuery(
    "Fact",
    f"{_REPORT} MATCH (r)-[:REPORT_HAS_FACT]->(f:Fact) "
    "OPTIONAL MATCH (f)-[:FACT_HAS_ELEMENT]->(e:Element) "
    "OPTIONAL MATCH (f)-[:FACT_HAS_PERIOD]->(p:Period) "
    "OPTIONAL MATCH (f)-[:FACT_HAS_UNIT]->(u:Unit) "
    "OPTIONAL MATCH (f)-[:FACT_HAS_ENTITY]->(en:Entity) "
    "OPTIONAL MATCH (f)-[:FACT_HAS_DIMENSION]->(d:Dimension) "
    "RETURN f.*, r.identifier AS from__REPORT_HAS_FACT, "
    "e.identifier AS to__FACT_HAS_ELEMENT, p.identifier AS to__FACT_HAS_PERIOD, "
    "u.identifier AS to__FACT_HAS_UNIT, en.identifier AS to__FACT_HAS_ENTITY, "
    "d.identifier AS to__FACT_HAS_DIMENSION",
  ),
  SliceQuery(
    "Period",
    f"{_REPORT} MATCH (r)-[:REPORT_HAS_FACT]->(:Fact)-[:FACT_HAS_PERIOD]->(p:Period) "
    "RETURN DISTINCT p.*",
  ),
  SliceQuery(
    "Unit",
    f"{_REPORT} MATCH (r)-[:REPORT_HAS_FACT]->(:Fact)-[:FACT_HAS_UNIT]->(u:Unit) "
    "RETURN DISTINCT u.*",
  ),
  SliceQuery(
    "Dimension",
    f"{_REPORT} MATCH (r)-[:REPORT_HAS_FACT]->(:Fact)-[:FACT_HAS_DIMENSION]->(d:Dimension) "
    "OPTIONAL MATCH (d)-[:DIMENSION_HAS_AXIS_ELEMENT]->(ax:Element) "
    "OPTIONAL MATCH (d)-[:DIMENSION_HAS_MEMBER_ELEMENT]->(me:Element) "
    "RETURN DISTINCT d.*, ax.identifier AS to__DIMENSION_HAS_AXIS_ELEMENT, "
    "me.identifier AS to__DIMENSION_HAS_MEMBER_ELEMENT",
  ),
)

# The second pass, by ``$elements`` — the identifiers the first pass found —
# because labels and references hang off elements, not off the report.
ELEMENT_QUERIES: tuple[SliceQuery, ...] = (
  SliceQuery(
    "Label",
    "MATCH (e:Element)-[:ELEMENT_HAS_LABEL]->(l:Label) WHERE e.identifier IN $elements "
    "RETURN l.*, e.identifier AS from__ELEMENT_HAS_LABEL",
  ),
  SliceQuery(
    "Reference",
    "MATCH (e:Element)-[:ELEMENT_HAS_REFERENCE]->(x:Reference) "
    "WHERE e.identifier IN $elements "
    "RETURN x.*, e.identifier AS from__ELEMENT_HAS_REFERENCE",
  ),
)

Runner = Callable[[str, Mapping[str, Any]], Sequence[Row]]


def fetch_slice(run: Runner, report: str) -> GraphTables:
  """One report's slice, through ``run(cypher, parameters) -> rows``.

  ``run`` is whatever executes Cypher against the graph — a LadybugDB
  connection here, an HTTP client elsewhere — returning each row as a
  mapping from column name to value.
  """
  results: list[tuple[SliceQuery, Sequence[Row]]] = [
    (query, run(query.cypher, {"report": report})) for query in SLICE_QUERIES
  ]
  elements = sorted(
    {
      _row_identifier(row)
      for query, rows in results
      if query.table == "Element"
      for row in rows
      if _row_identifier(row)
    }
  )
  if elements:
    results.extend(
      (query, run(query.cypher, {"elements": elements})) for query in ELEMENT_QUERIES
    )
  return tables_from_slice(results)


def tables_from_slice(
  results: Iterable[tuple[SliceQuery, Sequence[Row]]],
) -> GraphTables:
  """The rows a slice returned, unfolded into the projection's own tables.

  A node row's ``n.<column>`` columns become the node; each ``to__`` /
  ``from__`` column becomes one relationship row. A node reached by two
  queries is kept once, as is a relationship.
  """
  tables = GraphTables()
  seen_nodes: dict[str, set[str]] = {}
  seen_rels: dict[str, set[tuple[str, str]]] = {}

  def relate(name: str, frm: Any, to: Any) -> None:
    if frm is None or to is None:
      return
    key = (str(frm), str(to))
    if key in seen_rels.setdefault(name, set()):
      return
    seen_rels[name].add(key)
    tables.relationships.setdefault(name, []).append({"from": str(frm), "to": str(to)})

  for query, rows in results:
    if not query.nodes:
      for row in rows:
        relate(query.table, row.get("src"), row.get("dst"))
      continue
    for row in rows:
      node: dict[str, Any] = {}
      edges: list[tuple[str, Any, Any]] = []
      for column, value in row.items():
        if column.startswith("to__"):
          if value is not None:
            edges.append((column[4:], None, value))
        elif column.startswith("from__"):
          if value is not None:
            edges.append((column[6:], value, None))
        else:
          node[column.split(".", 1)[-1]] = value
      identifier = node.get("identifier")
      if identifier is None:
        continue
      if identifier not in seen_nodes.setdefault(query.table, set()):
        seen_nodes[query.table].add(identifier)
        tables.nodes.setdefault(query.table, []).append(node)
      for name, frm, to in edges:
        relate(
          name, identifier if frm is None else frm, identifier if to is None else to
        )
  return tables


def read_lbug(path: str | Path, report: str | None = None) -> GraphTables:
  """One report's slice of a LadybugDB file.

  Requires the ``lpg`` extra. A file holding one report needs no ``report``;
  one holding several needs the report named — by identifier, URI or
  accession.
  """
  try:
    import ladybug as lbug
  except ImportError as exc:  # pragma: no cover - depends on the extra
    raise ImportError(
      "reading a .lbug needs the ladybug package: pip install 'xbrlkit[lpg]'"
    ) from exc

  db = lbug.Database(str(path), read_only=True)
  conn = lbug.Connection(db)
  try:

    def run(cypher: str, parameters: Mapping[str, Any]) -> list[Row]:
      result = conn.execute(cypher, parameters=dict(parameters))
      names = result.get_column_names()
      return [dict(zip(names, values)) for values in result.get_all()]

    if report is None:
      reports = [
        row["r.identifier"] for row in run("MATCH (r:Report) RETURN r.identifier", {})
      ]
      if len(reports) != 1:
        raise GraphError(
          f"{path} holds {len(reports)} reports; name one with `report` "
          "(its identifier, URI or accession)"
        )
      report = str(reports[0])
    return fetch_slice(run, report)
  finally:
    conn.close()
    db.close()


def _row_identifier(row: Row) -> str | None:
  for column, value in row.items():
    if column == "identifier" or column.endswith(".identifier"):
      return None if value is None else str(value)
  return None


# -- the read ---------------------------------------------------------------------


def _read(tables: Tables) -> tuple[XbrlModel, ImportGaps]:
  nodes, rels = _normalize(tables)
  reports = nodes.get("Report", [])
  if not reports:
    raise GraphError("no Report row; the rows are one report's slice of the graph")
  if len(reports) > 1:
    raise GraphError(
      f"{len(reports)} Report rows; the rows are one report's slice of the graph"
    )
  report = reports[0]
  report_id = _text(report.get("identifier")) or ""
  gaps = ImportGaps(
    missing=[
      "arc targetRole (a cube declared across roles reads as its own role's arcs)",
      "fact language",
      "the nil-versus-empty distinction, where the writer stored an empty string",
      "the processed value_str (the graph keeps the raw value; it is read as both)",
      "is_text_fact for a concept that is not a text block",
      "the acceptance time of day (the graph keeps the date)",
      "primary_document",
      "type and substitution group QName prefixes outside the filing's namespaces",
      "network documentation",
    ]
  )

  entities = _by_id(nodes.get("Entity", []))
  elements = _by_id(nodes.get("Element", []))
  structures = _by_id(nodes.get("Structure", []))
  associations = _by_id(nodes.get("Association", []))
  labels = _by_id(nodes.get("Label", []))
  references = _by_id(nodes.get("Reference", []))
  fact_sets = _by_id(nodes.get("FactSet", []))
  if elements and not labels:
    gaps.missing.append("labels (this graph holds none)")
  if elements and not references:
    gaps.missing.append("concept references (this graph holds none)")

  filer_row = _filer(report_id, entities, rels)
  entity = _entity(filer_row)
  taxonomy_uri = _taxonomy_uri(report_id, nodes.get("Taxonomy", []), rels)
  concepts, element_qnames = _concepts(elements, labels, references, rels)
  periods = _periods(nodes.get("Period", []))
  units = _units(nodes.get("Unit", []))
  dimensions = _dimensions(
    nodes.get("Dimension", []), element_qnames, elements, rels, gaps
  )
  producers = {sid for sid, row in structures.items() if _is_producers(row)}
  pins = _pins(rels, fact_sets, producers)
  facts = _facts(
    nodes.get("Fact", []),
    report_id,
    rels,
    element_qnames,
    periods,
    units,
    dimensions,
    entities,
    filer_row,
    entity,
    pins,
    gaps,
  )
  networks = _networks(structures, associations, rels, element_qnames, producers, gaps)
  filing = _filing(report, entity, taxonomy_uri, concepts)

  seen_periods: dict[str, Period] = {}
  for period in periods.values():
    seen_periods.setdefault(period.id, period)
  seen_units: dict[str, Unit] = {}
  for unit in units.values():
    seen_units.setdefault(unit.id, unit)

  return (
    XbrlModel(
      filing=filing,
      entity=entity,
      concepts=concepts,
      periods=list(seen_periods.values()),
      units=list(seen_units.values()),
      facts=facts,
      networks=networks,
    ),
    gaps,
  )


def _normalize(
  tables: Tables,
) -> tuple[dict[str, list[Row]], dict[str, list[dict[str, Any]]]]:
  """The rows as node tables and relationship tables, relationship rows
  keyed ``from`` / ``to`` however the source spelled them."""
  if isinstance(tables, GraphTables):
    node_rows: Mapping[str, Sequence[Row]] = tables.nodes
    rel_rows: Mapping[str, Sequence[Row]] = tables.relationships
  else:
    node_names = {table.name for table in NODE_TABLES}
    rel_names = {table.name for table in REL_TABLES}
    node_rows = {name: rows for name, rows in tables.items() if name in node_names}
    rel_rows = {name: rows for name, rows in tables.items() if name in rel_names}
  nodes = {name: [dict(row) for row in rows] for name, rows in node_rows.items()}
  rels: dict[str, list[dict[str, Any]]] = {}
  for name, rows in rel_rows.items():
    normalized: list[dict[str, Any]] = []
    for row in rows:
      frm = row.get("from", row.get("src"))
      to = row.get("to", row.get("dst"))
      if frm is None or to is None:
        continue
      normalized.append({"from": str(frm), "to": str(to)})
    rels[name] = normalized
  return nodes, rels


def _by_id(rows: Sequence[Row]) -> dict[str, Row]:
  out: dict[str, Row] = {}
  for row in rows:
    identifier = _text(row.get("identifier"))
    if identifier and identifier not in out:
      out[identifier] = row
  return out


def _forward(rels: Mapping[str, Sequence[Row]], name: str) -> dict[str, list[str]]:
  """``from`` id -> ``to`` ids, in row order."""
  out: dict[str, list[str]] = {}
  for row in rels.get(name, []):
    out.setdefault(row["from"], []).append(row["to"])
  return out


def _first(rels: Mapping[str, Sequence[Row]], name: str) -> dict[str, str]:
  """``from`` id -> the first ``to`` id."""
  out: dict[str, str] = {}
  for row in rels.get(name, []):
    out.setdefault(row["from"], row["to"])
  return out


# -- identity --------------------------------------------------------------------


def _filer(
  report_id: str, entities: Mapping[str, Row], rels: Mapping[str, Sequence[Row]]
) -> Row:
  """The entity the report belongs to: the one that has it, else the one
  marked parent, else the first."""
  for row in rels.get("ENTITY_HAS_REPORT", []):
    if row["to"] == report_id and row["from"] in entities:
      return entities[row["from"]]
  for row in entities.values():
    if _bool(row.get("is_parent")):
      return row
  return next(iter(entities.values()), {})


def _entity(row: Row) -> EntityIdentity:
  uri = _text(row.get("uri")) or ""
  cik = (
    _text(row.get("cik"))
    or uri.rsplit("#", 1)[-1]
    or _text(row.get("identifier"))
    or ""
  )
  return EntityIdentity(
    cik=cik,
    scheme=_text(row.get("scheme")) or _scheme_for(cik),
    name=_text(row.get("name")),
    legal_name=_text(row.get("legal_name")),
    ein=_text(row.get("tax_id")),
    ticker=_text(row.get("ticker")),
    exchange=_text(row.get("exchange")),
    sic=_text(row.get("sic")),
    sic_description=_text(row.get("sic_description")),
    category=_text(row.get("category")),
    state_of_incorporation=_text(row.get("state_of_incorporation")),
    fiscal_year_end=_text(row.get("fiscal_year_end")),
    entity_type=_text(row.get("entity_type")),
    website=_text(row.get("website")),
    phone=_text(row.get("phone")),
  )


def _scheme_for(identifier: str) -> str:
  """A ten-digit identifier is a CIK; anything else is read under the
  neutral entity scheme rather than called an SEC one."""
  digits = identifier.isdigit() and len(identifier) == 10
  return CIK_SCHEME if digits else ENTITY_SCHEME


def _taxonomy_uri(
  report_id: str, taxonomies: Sequence[Row], rels: Mapping[str, Sequence[Row]]
) -> str | None:
  """The filer's own namespace: the taxonomy the report uses."""
  by_id = _by_id(taxonomies)
  for row in rels.get("REPORT_USES_TAXONOMY", []):
    if row["from"] == report_id and row["to"] in by_id:
      return _text(by_id[row["to"]].get("uri"))
  return _text(taxonomies[0].get("uri")) if len(taxonomies) == 1 else None


def _filing(
  report: Row,
  entity: EntityIdentity,
  taxonomy_uri: str | None,
  concepts: Mapping[str, Concept],
) -> FilingMeta:
  year = _int(report.get("fiscal_year_focus"))
  month = _int(report.get("fiscal_year_end_month"))
  return FilingMeta(
    accession=_text(report.get("accession_number"))
    or _text(report.get("identifier"))
    or "unknown",
    cik=entity.cik,
    form=_text(report.get("form")),
    filing_date=_date(report.get("filing_date")),
    report_date=_date(report.get("report_date")),
    acceptance_datetime=_text(report.get("acceptance_date")),
    is_inline_xbrl=_bool_or_none(report.get("is_inline_xbrl")),
    fiscal_year_focus=str(year) if year else None,
    fiscal_period_focus=_text(report.get("fiscal_period_focus")),
    fiscal_year_end_month=str(month) if month else None,
    report_uri=_text(report.get("uri")),
    extension_namespace=taxonomy_uri,
    taxonomy_namespaces=sorted(
      {concept.namespace for concept in concepts.values() if concept.namespace}
    ),
  )


# -- elements ---------------------------------------------------------------------


def _concepts(
  elements: Mapping[str, Row],
  labels: Mapping[str, Row],
  references: Mapping[str, Row],
  rels: Mapping[str, Sequence[Row]],
) -> tuple[dict[str, Concept], dict[str, str]]:
  """Every element as a concept, and the element id -> qname map the
  relationship tables resolve through."""
  labels_of = _forward(rels, "ELEMENT_HAS_LABEL")
  references_of = _forward(rels, "ELEMENT_HAS_REFERENCE")
  prefixes = dict(STANDARD_PREFIXES)
  for row in elements.values():
    qname = _text(row.get("qname")) or ""
    uri = _text(row.get("uri")) or ""
    if ":" in qname and "#" in uri:
      prefixes.setdefault(uri.rsplit("#", 1)[0], qname.split(":", 1)[0])

  concepts: dict[str, Concept] = {}
  qnames: dict[str, str] = {}
  for identifier, row in elements.items():
    uri = _text(row.get("uri")) or ""
    namespace, local = uri.rsplit("#", 1) if "#" in uri else ("", "")
    qname = _text(row.get("qname")) or (_qname(namespace, local, prefixes) or "")
    if not qname:
      continue
    local = local or qname.split(":", 1)[-1]
    qnames[identifier] = qname
    if qname in concepts:
      continue
    concept_labels = [
      Label(
        value=None if labels[lid].get("value") is None else str(labels[lid]["value"]),
        role=_text(labels[lid].get("type")),
        language=_text(labels[lid].get("language")),
      )
      for lid in labels_of.get(identifier, [])
      if lid in labels
    ]
    # A producer that writes no labels names the element in ``name``; when
    # that is not the local name, it is the element's label.
    display = _text(row.get("name"))
    if (
      display
      and display != local
      and not any(_is_standard(label) for label in concept_labels)
    ):
      concept_labels.insert(0, Label(value=display, role=STANDARD_LABEL_ROLE))
    sub_namespace, sub_local = _split_uri(_text(row.get("substitution_group")))
    type_namespace, type_local = _split_uri(_text(row.get("item_type")))
    is_textblock = _bool(row.get("is_textblock"))
    concepts[qname] = Concept(
      qname=qname,
      namespace=namespace,
      name=local,
      period_type=_period_type(row.get("period_type")),
      balance=_balance(row.get("balance")),
      is_abstract=_bool(row.get("is_abstract")),
      is_numeric=_bool(row.get("is_numeric")),
      is_textblock=is_textblock,
      is_hypercube_item=_bool(row.get("is_hypercube_item")),
      is_dimension_item=_bool(row.get("is_dimension_item")),
      is_domain_member=_bool(row.get("is_domain_member")),
      is_shares=_bool(row.get("is_shares")),
      is_integer=_bool(row.get("is_integer")),
      is_fraction=_bool(row.get("is_fraction")),
      substitution_group=_qname(sub_namespace, sub_local, prefixes),
      substitution_group_namespace=sub_namespace or None,
      item_type=type_local or None,
      nice_type=_text(row.get("type")),
      item_type_qname=_qname(type_namespace, type_local, prefixes)
      if type_namespace
      else None,
      item_type_namespace=type_namespace or None,
      is_text_fact=is_textblock,
      pref_label=next(
        (label.value for label in concept_labels if _is_standard(label)), None
      ),
      labels=concept_labels,
      references=[
        Reference(
          value=str(references[rid].get("value") or ""),
          role=_text(references[rid].get("type")),
        )
        for rid in references_of.get(identifier, [])
        if rid in references
      ],
    )
  return concepts, qnames


def _is_standard(label: Label) -> bool:
  return label.role in (STANDARD_LABEL_ROLE, "label")


def _split_uri(value: str | None) -> tuple[str, str]:
  """``namespace#local`` as its two parts; a bare local name has no namespace."""
  if not value:
    return "", ""
  if "#" in value:
    namespace, local = value.rsplit("#", 1)
    return namespace, local
  return "", value


def _qname(namespace: str, local: str, prefixes: Mapping[str, str]) -> str | None:
  if not local:
    return None
  if not namespace:
    return local
  prefix = prefixes.get(namespace)
  return f"{prefix}:{local}" if prefix else None


# -- periods, units, dimensions ------------------------------------------------


def _periods(rows: Sequence[Row]) -> dict[str, Period]:
  """Period rows by graph id. The calendar fields are recomputed from the
  dates, the same way every source of a model computes them."""
  periods: dict[str, Period] = {}
  for row in rows:
    identifier = _text(row.get("identifier"))
    if not identifier or identifier in periods:
      continue
    kind = _text(row.get("period_type"))
    start = _date(row.get("start_date"))
    end = _date(row.get("end_date"))
    if kind == "instant" and end:
      periods[identifier] = instant_period(end)
    elif start and end:
      periods[identifier] = duration_period(start, end)
    elif kind == "forever" or (start is None and end is None):
      periods[identifier] = forever_period()
  return periods


def _units(rows: Sequence[Row]) -> dict[str, Unit]:
  units: dict[str, Unit] = {}
  for row in rows:
    identifier = _text(row.get("identifier"))
    uri = _text(row.get("uri"))
    measure = _text(row.get("measure")) or (uri.rsplit("#", 1)[-1] if uri else None)
    if not identifier or identifier in units or not (uri or measure):
      continue
    units[identifier] = Unit(
      id=unit_id(uri or measure or ""),
      measure=measure or "",
      uri=uri,
      numerator_uri=_text(row.get("numerator_uri")),
      denominator_uri=_text(row.get("denominator_uri")),
    )
  return units


def _dimensions(
  rows: Sequence[Row],
  qnames: Mapping[str, str],
  elements: Mapping[str, Row],
  rels: Mapping[str, Sequence[Row]],
  gaps: ImportGaps,
) -> dict[str, DimQualifier]:
  """Dimension rows by graph id. The axis and member resolve through their
  element edges, else through the element URIs the row carries."""
  axis_of = _first(rels, "DIMENSION_HAS_AXIS_ELEMENT")
  member_of = _first(rels, "DIMENSION_HAS_MEMBER_ELEMENT")
  by_uri = {
    _text(row.get("uri")): qnames[identifier]
    for identifier, row in elements.items()
    if identifier in qnames and _text(row.get("uri"))
  }
  dimensions: dict[str, DimQualifier] = {}
  for row in rows:
    identifier = _text(row.get("identifier"))
    if not identifier or identifier in dimensions:
      continue
    axis = qnames.get(axis_of.get(identifier, "")) or by_uri.get(
      _text(row.get("axis_uri"))
    )
    if not axis:
      gaps.unresolved_references += 1
      continue
    typed = _bool(row.get("is_typed"))
    member: str | None = None
    if not typed:
      member = qnames.get(member_of.get(identifier, "")) or by_uri.get(
        _text(row.get("member_uri"))
      )
      if not member:
        gaps.unresolved_references += 1
        continue
    axis_type = _text(row.get("type"))
    dimensions[identifier] = DimQualifier(
      axis_qname=axis,
      member_qname=member,
      typed_value=_text(row.get("member")) if typed else None,
      is_explicit=not typed,
      axis_type=axis_type if axis_type in ("segment", "scenario") else None,
    )
  return dimensions


# -- facts -------------------------------------------------------------------------


def _is_producers(structure: Row) -> bool:
  """A structure with no role URI is the producer's own, not a filing's."""
  return _text(structure.get("network_uri")) is None


def _pins(
  rels: Mapping[str, Sequence[Row]], fact_sets: Mapping[str, Row], producers: set[str]
) -> dict[str, str]:
  """Fact id -> the producer's structure it is pinned to, through its fact
  set. A filing's facts carry no pin; the presentation networks say where
  they belong."""
  structure_of_set: dict[str, str] = {}
  for row in rels.get("STRUCTURE_HAS_FACT_SET", []):
    if row["from"] in producers and row["to"] in fact_sets:
      structure_of_set.setdefault(row["to"], row["from"])
  pins: dict[str, str] = {}
  for row in rels.get("FACT_SET_CONTAINS_FACT", []):
    structure = structure_of_set.get(row["from"])
    if structure:
      pins.setdefault(row["to"], structure)
  return pins


def _facts(
  rows: Sequence[Row],
  report_id: str,
  rels: Mapping[str, Sequence[Row]],
  qnames: Mapping[str, str],
  periods: Mapping[str, Period],
  units: Mapping[str, Unit],
  dimensions: Mapping[str, DimQualifier],
  entities: Mapping[str, Row],
  filer_row: Row,
  entity: EntityIdentity,
  pins: Mapping[str, str],
  gaps: ImportGaps,
) -> list[XbrlFact]:
  element_of = _first(rels, "FACT_HAS_ELEMENT")
  period_of = _first(rels, "FACT_HAS_PERIOD")
  unit_of = _first(rels, "FACT_HAS_UNIT")
  entity_of = _first(rels, "FACT_HAS_ENTITY")
  dims_of = _forward(rels, "FACT_HAS_DIMENSION")
  of_report = {
    row["to"] for row in rels.get("REPORT_HAS_FACT", []) if row["from"] == report_id
  }

  facts: list[XbrlFact] = []
  seen: set[str] = set()
  for row in rows:
    identifier = _text(row.get("identifier"))
    if not identifier or identifier in seen:
      continue
    if of_report and identifier not in of_report:
      continue
    seen.add(identifier)
    qname = qnames.get(element_of.get(identifier, ""))
    period = periods.get(period_of.get(identifier, ""))
    if not qname or period is None:
      gaps.unresolved_references += 1
      continue
    unit = units.get(unit_of.get(identifier, ""))
    numeric = unit is not None or _text(row.get("fact_type")) == "Numeric"
    value = row.get("value")
    value_str = None if value is None else str(value)
    if _text(row.get("value_type")) == "external":
      gaps.external_values += 1
    context = entities.get(entity_of.get(identifier, ""), filer_row)
    context_cik = _text(context.get("cik"))
    context_name = _text(context.get("name"))
    is_filer = context is filer_row or _bool(context.get("is_parent"))
    stem = _fact_stem(_text(row.get("uri")))
    facts.append(
      XbrlFact(
        id=stem or identifier,
        concept_qname=qname,
        period_id=period.id,
        unit_id=unit.id if unit is not None else None,
        entity_cik=(context_cik or context_name or entity.cik)
        if not is_filer
        else entity.cik,
        entity_scheme=_text(context.get("scheme")) or entity.scheme,
        entity_identifier=entity.cik if is_filer else (context_name or context_cik),
        dims=[dimensions[d] for d in dims_of.get(identifier, []) if d in dimensions],
        value_str=value_str,
        raw_value=value_str,
        source_hash=stem,
        numeric_value=_float(row.get("numeric_value")) if numeric else None,
        decimals=_text(row.get("decimals")) if numeric else None,
        value_kind="numeric" if numeric else "text",
        is_nil=value_str is None,
        content_type=_text(row.get("content_type")),
        structure_id=pins.get(identifier),
      )
    )
  return facts


def _fact_stem(uri: str | None) -> str | None:
  """The hash the projection scoped the fact's URI on — ``…#fact-<hash>``."""
  if not uri or "#fact-" not in uri:
    return None
  return uri.rsplit("#fact-", 1)[-1] or None


# -- networks ----------------------------------------------------------------------


def _kind(arcrole: str, association_type: str) -> NetworkKind | None:
  """Which linkbase an association belongs to, or ``None`` for a kind the
  model has no network for.

  The producer's own kinds are its own vocabulary and are honoured first —
  a ledger's mapping arc borrows the parent-child arcrole without being
  presentation. Then the arcrole says which XBRL linkbase, an association
  typed but without an XBRL arcrole takes its type, and the projection's
  ``Other`` is the definition linkbase by construction.
  """
  if association_type in NON_NETWORK_TYPES:
    return None
  if arcrole == PARENT_CHILD:
    return "presentation"
  if arcrole == SUMMATION_ITEM:
    return "calculation"
  if arcrole.startswith(DIMENSION_ARCROLE_BASE) or arcrole == XBRL_DIMENSIONS:
    return "definition"
  if association_type in ("presentation", "calculation", "definition"):
    return association_type  # type: ignore[return-value]
  if association_type == "other":
    # The projection's word for every linkbase arcrole that is neither
    # parent-child nor summation-item — which is what the parse files as a
    # definition network (the 2023 summation-item of Calculations 1.1
    # included, as the parse does today).
    return "definition"
  return None


def _networks(
  structures: Mapping[str, Row],
  associations: Mapping[str, Row],
  rels: Mapping[str, Sequence[Row]],
  qnames: Mapping[str, str],
  producers: set[str],
  gaps: ImportGaps,
) -> list[Network]:
  associations_of = _forward(rels, "STRUCTURE_HAS_ASSOCIATION")
  from_of = _first(rels, "ASSOCIATION_HAS_FROM_ELEMENT")
  to_of = _first(rels, "ASSOCIATION_HAS_TO_ELEMENT")
  sets_of = _forward(rels, "STRUCTURE_HAS_FACT_SET")

  networks: list[Network] = []
  for identifier, structure in structures.items():
    producers_own = identifier in producers
    uri = _text(structure.get("uri")) or ""
    role_uri = _text(structure.get("network_uri")) or uri or identifier
    role_id = uri.rsplit("#", 1)[1] if "#" in uri else None
    definition = _text(structure.get("definition")) or (
      _text(structure.get("name")) if producers_own else None
    )
    grouped: dict[NetworkKind, list[Arc]] = {}
    for association_id in associations_of.get(identifier, []):
      row = associations.get(association_id)
      if row is None:
        continue
      raw_arcrole = _text(row.get("arcrole")) or ""
      arcrole = SHORT_ARCROLES.get(raw_arcrole, raw_arcrole)
      association_type = (_text(row.get("association_type")) or "").lower()
      kind = _kind(arcrole, association_type)
      if kind is None:
        key = association_type or raw_arcrole or "untyped"
        gaps.associations_skipped[key] = gaps.associations_skipped.get(key, 0) + 1
        continue
      source = qnames.get(from_of.get(association_id, ""))
      target = qnames.get(to_of.get(association_id, ""))
      if not source or not target:
        gaps.unresolved_references += 1
        continue
      grouped.setdefault(kind, []).append(
        Arc(
          from_qname=source,
          to_qname=target,
          arcrole=arcrole or None,
          order=_float(row.get("order_value")),
          weight=_float(row.get("weight")) if kind == "calculation" else None,
          preferred_label=_text(row.get("preferred_label")),
          is_root=_bool(row.get("root")),
        )
      )
    for kind in ("presentation", "calculation", "definition"):
      arcs = grouped.get(kind)  # type: ignore[call-overload]
      if not arcs:
        continue
      networks.append(
        Network(
          role_uri=role_uri,
          definition=definition,
          kind=kind,  # type: ignore[arg-type]
          arcs=arcs,
          role_id=role_id,
          block_type=_text(structure.get("type")) if producers_own else None,
          structure_id=identifier if producers_own else None,
          fact_set_id=(sets_of.get(identifier) or [None])[0] if producers_own else None,
        )
      )
  return networks


# -- reading rows defensively ------------------------------------------------------


def _text(value: Any) -> str | None:
  if value is None:
    return None
  text = str(value)
  return text if text != "" else None


def _bool(value: Any, *, default: bool = False) -> bool:
  parsed = _bool_or_none(value)
  return default if parsed is None else parsed


def _bool_or_none(value: Any) -> bool | None:
  """A boolean however the store returned it — LadybugDB stringifies the
  ``root`` column's booleans on load."""
  if isinstance(value, bool):
    return value
  text = _text(value)
  if text is None:
    return None
  return text.strip().lower() == "true"


def _int(value: Any) -> int | None:
  try:
    return int(str(_text(value)))
  except (TypeError, ValueError):
    return None


def _float(value: Any) -> float | None:
  try:
    return float(_text(value))  # type: ignore[arg-type]
  except (TypeError, ValueError):
    return None


def _date(value: Any) -> date | None:
  try:
    return date.fromisoformat(str(_text(value))[:10])
  except (TypeError, ValueError):
    return None


def _period_type(value: Any) -> Any:
  text = _text(value)
  return text if text in ("instant", "duration", "forever") else None


def _balance(value: Any) -> Any:
  text = _text(value)
  return text if text in ("debit", "credit") else None


__all__ = (
  "ELEMENT_QUERIES",
  "SLICE_QUERIES",
  "GraphError",
  "ImportGaps",
  "SliceQuery",
  "fetch_slice",
  "from_graph",
  "from_graph_report",
  "read_lbug",
  "tables_from_slice",
)
