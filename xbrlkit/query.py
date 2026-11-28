"""In-memory SPARQL over a ``holon.jsonld`` — the query side of the round trip.

The holon *is* the queryable graph: parse the dataset-form JSON-LD back into an
:class:`rdflib.Graph` (all named graphs unioned, exactly as the viewer reads
them) and run SPARQL over it. No triplestore, no server — the serialization
carries the semantics and answers questions directly in memory.

:func:`fact_grid` is a faithful transliteration of the SEC ``build-fact-grid`` /
``financial-statement-analysis`` semantics against one filing: match facts by
element qname, keep only **consolidated** totals (``NOT EXISTS { ?f
rs:dimension }`` — the RDF equivalent of ``Fact.has_dimensions = false``),
optionally filter by period end date and period type, then dedup ``(qname,
end)`` keeping the newest and order by end date descending. Because dimensions
are first-class here, the same graph can *also* answer dimensional questions the
MCP tools never expose — but parity is defined on the consolidated slice.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from rdflib import Dataset, Graph

from .namespaces import HOLON_VOCAB

_CONSOLIDATED_FACTS = (
  f"PREFIX rs: <{HOLON_VOCAB}>"
  + """
PREFIX xbrli: <http://www.xbrl.org/2003/instance#>
PREFIX skos: <http://www.w3.org/2004/02/skos/core#>
SELECT ?qname ?name ?value ?end ?ptype ?dtype ?measure WHERE {
  ?f a rs:Fact ;
     rs:element ?el ;
     rs:period ?p ;
     rs:numericValue ?value .
  ?el rs:internalId ?qname .
  OPTIONAL { ?el skos:prefLabel ?name }
  ?p xbrli:periodType ?ptype .
  OPTIONAL { ?p xbrli:instant ?inst }
  OPTIONAL { ?p xbrli:endDate ?ed }
  BIND(COALESCE(?ed, ?inst) AS ?end)
  OPTIONAL { ?p rs:durationType ?dtype }
  OPTIONAL { ?f rs:unit ?u . ?u xbrli:measure ?measure }
  FILTER NOT EXISTS { ?f rs:dimension ?dim }
}
"""
)


@dataclass(frozen=True)
class FactRow:
  """One consolidated numeric fact as returned by the query layer."""

  qname: str
  name: str | None
  value: float
  end_date: str | None
  period_type: str
  duration_type: str | None
  measure: str | None


def parse_holon(text: str) -> Graph:
  """Parse dataset-form holon JSON-LD into one queryable graph (graphs unioned)."""
  ds = Dataset()
  ds.parse(data=text, format="json-ld")
  g = Graph()
  for s, p, o, _ctx in ds.quads((None, None, None, None)):
    g.add((s, p, o))
  return g


def load_holon(path: str | Path) -> Graph:
  """Load a ``holon.jsonld`` file into one queryable graph."""
  return parse_holon(Path(path).read_text(encoding="utf-8"))


def consolidated_facts(graph: Graph) -> list[FactRow]:
  """Every consolidated (undimensioned) numeric fact in the holon."""
  rows: list[FactRow] = []
  for r in graph.query(_CONSOLIDATED_FACTS):
    rows.append(
      FactRow(
        qname=str(r.qname),  # type: ignore[attr-defined]
        name=str(r.name) if r.name is not None else None,  # type: ignore[attr-defined]
        value=float(r.value),  # type: ignore[attr-defined]
        end_date=_iso(r.end),  # type: ignore[attr-defined]
        period_type=str(r.ptype),  # type: ignore[attr-defined]
        duration_type=str(r.dtype) if r.dtype is not None else None,  # type: ignore[attr-defined]
        measure=str(r.measure) if r.measure is not None else None,  # type: ignore[attr-defined]
      )
    )
  return rows


def fact_grid(
  graph: Graph,
  *,
  elements: list[str] | None = None,
  periods: list[str] | None = None,
  period_type: str | None = None,
) -> list[FactRow]:
  """Consolidated facts filtered by element / period / period type.

  Mirrors the MCP fact-grid contract: ``period_type`` ``"instant"`` keeps
  instant facts; ``"annual"`` / ``"quarterly"`` keep durations of that bucket.
  Rows are deduped on ``(qname, end)`` keeping the newest, ordered end DESC.
  """
  rows = consolidated_facts(graph)
  if elements is not None:
    wanted = set(elements)
    rows = [r for r in rows if r.qname in wanted]
  if periods is not None:
    pset = set(periods)
    rows = [r for r in rows if r.end_date in pset]
  if period_type == "instant":
    rows = [r for r in rows if r.period_type == "instant"]
  elif period_type in ("annual", "quarterly"):
    rows = [r for r in rows if r.duration_type == period_type]

  rows.sort(key=lambda r: r.end_date or "", reverse=True)
  seen: set[tuple[str, str | None]] = set()
  out: list[FactRow] = []
  for r in rows:
    key = (r.qname, r.end_date)
    if key in seen:
      continue
    seen.add(key)
    out.append(r)
  return out


def _iso(node: object) -> str | None:
  """Render a date/dateTime literal (or any node) as an ISO string."""
  if node is None:
    return None
  py = getattr(node, "toPython", lambda: node)()
  return py.isoformat() if hasattr(py, "isoformat") else str(py)
