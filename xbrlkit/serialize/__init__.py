"""Projections of the neutral ``XbrlModel`` into portable serializations.

:func:`to_holon` is the RDF/JSON-LD projection, :func:`to_tavi` the Project TAVI
compiled model, and :func:`to_oim` the xBRL-JSON (OIM) report — the only one
with a reference implementation to check against. :func:`to_graph_tables` is
the property-graph projection (the RoboSystems ``sec`` graph's tables), with
:func:`write_parquet` and :func:`build_lbug` to land it as parquet or as a
single-filing LadybugDB database. :func:`build_holon_graph` exposes the flat RDF
graph the holon partitions (for SPARQL / SHACL). :func:`classify_network` is the
legacy four-primary heuristic, retained for callers that want it — the holon
itself emits no semantic block type.
"""

from __future__ import annotations

from .classify import classify_network, root_qname
from .graph import build_holon_graph
from .holon import to_holon
from .lpg import GraphTables, build_lbug, to_graph_tables, write_parquet
from .oim import to_oim, to_oim_document
from .tavi import GapReport, to_tavi, to_tavi_report

__all__ = (
  "GapReport",
  "GraphTables",
  "build_holon_graph",
  "build_lbug",
  "classify_network",
  "root_qname",
  "to_graph_tables",
  "to_holon",
  "to_oim",
  "to_oim_document",
  "to_tavi",
  "to_tavi_report",
  "write_parquet",
)
