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

import importlib
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
  from .classify import classify_network, root_qname
  from .clawdog import GapReport as ClawDogGapReport
  from .clawdog import to_clawdog, to_clawdog_report
  from .graph import build_holon_graph
  from .holon import to_holon
  from .lpg import GraphTables, build_lbug, to_graph_tables, write_parquet
  from .oim import to_oim, to_oim_document
  from .tavi import GapReport as TaviGapReport
  from .tavi import to_tavi, to_tavi_report

  GapReport = TaviGapReport

# Loaded on first use (PEP 562), not on import. Importing any submodule runs
# this file first, so eager imports here made
# `xbrlkit.serialize.tavi` load rdflib through the holon and graph projections.
_LAZY: dict[str, tuple[str, str]] = {
  "classify_network": (".classify", "classify_network"),
  "root_qname": (".classify", "root_qname"),
  "ClawDogGapReport": (".clawdog", "GapReport"),
  "to_clawdog": (".clawdog", "to_clawdog"),
  "to_clawdog_report": (".clawdog", "to_clawdog_report"),
  "build_holon_graph": (".graph", "build_holon_graph"),
  "to_holon": (".holon", "to_holon"),
  "GraphTables": (".lpg", "GraphTables"),
  "build_lbug": (".lpg", "build_lbug"),
  "to_graph_tables": (".lpg", "to_graph_tables"),
  "write_parquet": (".lpg", "write_parquet"),
  "to_oim": (".oim", "to_oim"),
  "to_oim_document": (".oim", "to_oim_document"),
  "GapReport": (".tavi", "GapReport"),
  "TaviGapReport": (".tavi", "GapReport"),
  "to_tavi": (".tavi", "to_tavi"),
  "to_tavi_report": (".tavi", "to_tavi_report"),
}


def __getattr__(name: str) -> Any:
  entry = _LAZY.get(name)
  if entry is None:
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
  module, attribute = entry
  value = getattr(importlib.import_module(module, __name__), attribute)
  globals()[name] = value
  return value


__all__ = (
  "GapReport",
  "ClawDogGapReport",
  "GraphTables",
  "TaviGapReport",
  "build_holon_graph",
  "build_lbug",
  "classify_network",
  "root_qname",
  "to_clawdog",
  "to_clawdog_report",
  "to_graph_tables",
  "to_holon",
  "to_oim",
  "to_oim_document",
  "to_tavi",
  "to_tavi_report",
  "write_parquet",
)
