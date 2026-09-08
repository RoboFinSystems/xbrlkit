"""Project a neutral ``XbrlModel`` into the canonical ``holon.jsonld``.

The projection builds the flat holon RDF graph **directly** from the whole
``XbrlModel`` slice (:func:`~xbrlkit.serialize.graph.build_holon_graph`)
— every fact (numeric and text), concept, network, and dimensional coordinate —
then hands it to the vendored kernel's :func:`serialize_holon_jsonld_from_graph`,
which partitions it into the scene / boundary / projection named graphs and
emits dataset-form JSON-LD.

The earlier ``StatementBundle`` waist (numeric-only, four primary statements,
dimensions dropped) is retired from this path; the kernel encoder and its SHACL
shapes are unchanged apart from the v1.1 vocabulary superset.
"""

from __future__ import annotations

from ..model import XbrlModel
from ._kernel.holon import serialize_holon_jsonld_from_graph
from .graph import build_holon_graph, holon_root, namespace_bindings


def to_holon(model: XbrlModel, *, report_id: str | None = None) -> str:
  """Project ``model`` into the canonical dataset-form ``holon.jsonld`` string.

  The prefix bindings are resolved once and used twice — to mint the concept
  IRIs and to build the document's ``@context`` — because a concept compacts to
  its QName only if the two agree.
  """
  report_id = report_id or model.filing.accession
  namespaces = namespace_bindings(model)
  graph = build_holon_graph(model, report_id=report_id, namespaces=namespaces)
  return serialize_holon_jsonld_from_graph(
    graph, holon_root(report_id), namespaces=namespaces
  )
