"""Holon serialization — the report as scene/boundary/projection named graphs.

A *holon* is a report expressed as an RDF **dataset**: three named graphs over
one report IRI —

* ``<report>#scene`` — the instance facts (values this report reports) plus the
  Information Block that groups them and the element/period/unit/entity/factSet
  those facts reference.
* ``<report>#boundary`` — the calculation network: the roll-up rules the facts
  must obey.
* ``<report>#projection`` — the presentation network: order, indentation,
  subtotals (structures + presentation arcs).

The fourth graph, ``#lineage`` (the ledger behind the facts), is intentionally
*absent* from the published holon — a report is an aggregation of the books, not
the books. The access boundary holds by graph omission, no filter code.

The holon is a **shape, not a file format**. JSON-LD's data model *is* an RDF
dataset, so dataset-form JSON-LD carries these named graphs natively — the
single canonical, API-native holon. (rdflib can serialize the same
:class:`~rdflib.Dataset` to TriG / N-Quads, but the platform does not emit
them: JSON-LD is the one holon format.)

This module is the **single source of truth** for the partition. Two entry
paths converge on :func:`build_holon_dataset`:

* production — :func:`serialize_to_holon_jsonld` builds the flat
  :class:`~rdflib.Graph` from a ``StatementBundle`` (via ``build_graph``), then
  partitions it;
* demo / offline — ``examples/_common/databook.py`` parses a downloaded flat
  ``.jsonld`` into a graph and partitions *that*.

Both call :func:`build_holon_dataset`, so the holon is derived one way.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping

from rdflib import RDF, Dataset, Graph, URIRef

from .bundle import StatementBundle
from .jsonld import (
  RS,
  XLINK,
  _build_context,
  _root_uri,
  build_graph,
)

# The named-graph suffixes on the report IRI — the scene / boundary / projection
# partition, keyed under the report IRI.
HOLON_GRAPHS: tuple[str, str, str] = ("scene", "boundary", "projection")


# ── Partition ──────────────────────────────────────────────────────────────


def partition_report_graph(g: Graph) -> dict[str, Graph]:
  """Split a flat report graph into the three *published* holon graphs.

  ``scene`` (facts + the InformationBlock that groups them + the
  element/period/unit/entity/factSet they reference), ``boundary``
  (calculation arcs) and ``projection`` (presentation arcs + structures).
  The ``lineage`` / event graph is intentionally absent: a report is an
  aggregation of the books, not the books. Everything here already lives in
  the report graph, so this is a pure repartition — no ledger access.

  The bulky ``rs:envelopeJson`` literal is dropped from the published graphs;
  it lives in the flat JSON-LD bundle for callers that need the opaque IB
  mechanics.
  """
  scene, boundary, projection = Graph(), Graph(), Graph()

  scene_subjects: set[URIRef] = set()
  for fact in g.subjects(RDF.type, RS.Fact):
    scene_subjects.add(fact)  # type: ignore[arg-type]
    for ref in (RS.element, RS.period, RS.unit, RS.entity, RS.factSet):
      v = g.value(fact, ref)
      if isinstance(v, URIRef):
        scene_subjects.add(v)
    # A fact may carry several dimensional coordinates; pull each rs:Dimension
    # node into scene, and the axis / member Elements it links so their labels
    # travel with the dimensional facts.
    for dim in g.objects(fact, RS.dimension):
      if not isinstance(dim, URIRef):
        continue
      scene_subjects.add(dim)
      for aref in (RS.axis, RS.member):
        av = g.value(dim, aref)
        if isinstance(av, URIRef):
          scene_subjects.add(av)
  # The InformationBlock molecule groups its facts via the shared factSet
  # (Fact --factSet--> FactSet <--factSet-- InformationBlock); include it so its
  # blockType / prefLabel and that grouping link land in scene.
  for ib in g.subjects(RDF.type, RS.InformationBlock):
    scene_subjects.add(ib)  # type: ignore[arg-type]
  # The Report node carries the filing's identity (accession / form / date /
  # fiscal focus); keep it in scene so the report is self-identifying.
  for report in g.subjects(RDF.type, RS.Report):
    scene_subjects.add(report)  # type: ignore[arg-type]
  for s in scene_subjects:
    for p, o in g.predicate_objects(s):
      if p == RS.envelopeJson:
        continue
      scene.add((s, p, o))

  # Associations: calculation → boundary, everything else (presentation and the
  # XBRL-dimensions *definition* wiring) → projection. The endpoint Elements are
  # copied alongside so the presentation tree and hypercube wiring carry labels.
  projection_elements: set[URIRef] = set()
  for assoc in g.subjects(RDF.type, RS.Association):
    at = str(g.value(assoc, RS.associationType) or "")
    target = boundary if at == "calculation" else projection
    for p, o in g.predicate_objects(assoc):
      target.add((assoc, p, o))
      if p in (XLINK["from"], XLINK.to) and isinstance(o, URIRef):
        projection_elements.add(o)
  for el in projection_elements:
    for p, o in g.predicate_objects(el):
      if p == RS.envelopeJson:
        continue
      projection.add((el, p, o))

  for struct in g.subjects(RDF.type, RS.Structure):
    for p, o in g.predicate_objects(struct):
      if p == RS.envelopeJson:
        continue
      if p == RS.hasAssociation:
        at = str(g.value(o, RS.associationType) or "")
        (boundary if at == "calculation" else projection).add((struct, p, o))
      else:
        projection.add((struct, p, o))

  return {"scene": scene, "boundary": boundary, "projection": projection}


def build_holon_dataset(g: Graph, root: URIRef) -> Dataset:
  """Assemble the holon :class:`~rdflib.Dataset` from a flat report graph.

  Each partition lands in a named graph keyed by the report IRI —
  ``<root>#scene`` / ``#boundary`` / ``#projection``. These IRIs are the ones
  the DataBook's ``graph:`` map declares and the holon viewer reads, so they
  must stay stable.
  """
  ds = Dataset()
  for name, sub in partition_report_graph(g).items():
    ctx = ds.graph(URIRef(f"{root}#{name}"))
    for triple in sub:
      ctx.add(triple)
  return ds


# ── Serializers ────────────────────────────────────────────────────────────


def serialize_holon_jsonld_from_graph(
  g: Graph, root: URIRef, *, namespaces: Mapping[str, str] | None = None
) -> str:
  """Serialize a flat report graph to the canonical **dataset-form JSON-LD**
  holon — ``{"@context", "@graph": [ {@id: …#scene, @graph: […]}, … ]}``.

  Passing the canonical ``@context`` compacts qnames and keeps the emitted
  vocabulary identical to the flat JSON-LD bundle; without it rdflib dumps its
  entire built-in prefix registry into ``@context``. ``namespaces`` adds the
  prefixes this report declares, so its concepts compact against the taxonomy
  the filing actually used.
  """
  ds = build_holon_dataset(g, root)
  text = ds.serialize(
    format="json-ld",
    auto_compact=True,
    context=_build_context(namespaces),
    indent=2,
    sort_keys=True,
  )
  return _sort_graph_arrays(text)


_DIGITS = re.compile(r"(\d+)")


def _natural(text: str) -> tuple[object, ...]:
  """A sort key that orders the numbers inside an IRI numerically.

  Association IRIs end in a per-structure index (``…/presentation/9``). Sorted
  as strings, ``…/10`` lands before ``…/2``; the reader keeps ``@graph`` order,
  and the next write re-indexes the arcs by that order — so a holon read back
  and written again moved most of its arcs on every hop and never settled.
  ``re.split`` on the capturing group alternates text and number chunks, so
  every key has a string at even positions and an int at odd ones and the
  tuples compare.
  """
  return tuple(int(part) if part.isdigit() else part for part in _DIGITS.split(text))


def _sort_graph_arrays(text: str) -> str:
  """Sort every ``@graph`` array by ``@id`` — and every string-valued property
  array — so serialization is byte-stable across runs and across round-trips.

  ``sort_keys`` orders dict keys but rdflib emits ``@graph`` node arrays and
  multi-valued property arrays (``@type``, ``hasAssociation``, ``factSet``) in
  hash order, so the same report shuffles between runs — noisy diffs for any
  holon checked into a repo. Neither order carries RDF meaning (the holon uses
  no ``@list``), so both are safe to sort. The sort is *natural* (numbers
  compare as numbers, :func:`_natural`), so the arcs come out in index order
  and a holon is a fixed point of read-then-write.
  """
  doc = json.loads(text)

  def sort_node(node: object) -> None:
    if isinstance(node, dict):
      for key, value in node.items():
        if not isinstance(value, list):
          continue
        if key == "@graph":
          value.sort(key=lambda n: _natural(n.get("@id", "") if isinstance(n, dict) else ""))
        elif all(isinstance(v, str) for v in value):
          value.sort(key=_natural)
        for child in value:
          sort_node(child)

  sort_node(doc)
  return json.dumps(doc, indent=2, sort_keys=True, ensure_ascii=False)


def serialize_to_holon_jsonld(bundle: StatementBundle) -> str:
  """Serialize a ``StatementBundle`` to the canonical dataset-form JSON-LD holon.

  Builds the flat report graph from the bundle (the same ``build_graph`` the
  flat JSON-LD serializer uses), then partitions it into the scene/boundary/
  projection named graphs and emits dataset-form JSON-LD.
  """
  g = build_graph(bundle)
  root = _root_uri(bundle)
  return serialize_holon_jsonld_from_graph(g, root)
