"""Validate a holon against the format: one verdict per named graph.

A holon is a dataset of named graphs over one report IRI, and each graph has
its own job: ``#scene`` holds what the report states, ``#projection`` how it is
arranged, ``#boundary`` the roll-ups the facts must obey, and the optional
``#lineage`` the ledger behind them. So each is checked against its own SHACL
shapes, and a fifth set runs over their union for what no single graph can
show: a reference from one graph that nothing in another answers.

This is the check on the *file*. :func:`xbrlkit.check.check_report` is the
check on the *report* (it is whole, it foots, it balances) and runs on the
model a reader built, so it cannot see what the reader skipped. A holon about
to be trusted wants both, and :func:`validate_holon` gives both by default.

The format itself is written down beside this module, in ``README.md``.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from rdflib import RDF, Dataset, Graph, Namespace, URIRef

from ..check import CheckResult, check_report
from ..deserialize.holon import HolonError, from_holon_json
from ..namespaces import HOLON_VOCAB

# The graphs the format names. Only #scene is always there: a report with no
# networks has nothing to arrange and nothing to roll up, and an empty graph
# is not written.
GRAPHS: tuple[str, ...] = ("scene", "projection", "boundary", "lineage")
REQUIRED_GRAPHS: tuple[str, ...] = ("scene",)
# The shapes that run over the graphs taken together.
DATASET = "dataset"

_SHAPES_DIR = Path(__file__).resolve().parent / "shapes"
_SH = Namespace("http://www.w3.org/ns/shacl#")
_RS = Namespace(HOLON_VOCAB)

_WITH_COMMON = ("scene", "projection")
_shapes: dict[str, Graph] = {}
_shapes_lock = threading.Lock()


def shapes_graph(name: str) -> Graph:
  """The shapes for one named graph, or for ``dataset``."""
  if name not in (*GRAPHS, DATASET):
    raise ValueError(f"no shapes named {name!r}")
  with _shapes_lock:
    graph = _shapes.get(name)
    if graph is None:
      graph = Graph().parse(str(_SHAPES_DIR / f"{name}.ttl"), format="turtle")
      # An element is written in every graph that cites it, on one shape.
      if name in _WITH_COMMON:
        graph.parse(str(_SHAPES_DIR / "common.ttl"), format="turtle")
      _shapes[name] = graph
    return graph


@dataclass
class Finding:
  """One node that does not meet a shape."""

  severity: str  # "violation" | "warning"
  focus: str
  message: str
  shape: str | None = None
  path: str | None = None
  value: str | None = None

  def to_dict(self) -> dict[str, Any]:
    return {k: v for k, v in self.__dict__.items() if v is not None}


@dataclass
class GraphVerdict:
  """What the shapes said about one graph."""

  name: str
  present: bool = True
  triples: int = 0
  shapes: int = 0
  violations: int = 0
  warnings: int = 0
  # A bounded sample: a broken writer repeats one fault on every node.
  findings: list[Finding] = field(default_factory=list)

  @property
  def conforms(self) -> bool:
    return self.violations == 0

  def to_dict(self) -> dict[str, Any]:
    return {
      "name": self.name,
      "present": self.present,
      "conforms": self.conforms,
      "triples": self.triples,
      "shapes": self.shapes,
      "violations": self.violations,
      "warnings": self.warnings,
      "findings": [f.to_dict() for f in self.findings],
    }


@dataclass
class HolonValidation:
  """The verdict on a holon: each graph, the dataset, and the report."""

  report: str | None = None
  graphs: dict[str, GraphVerdict] = field(default_factory=dict)
  dataset: GraphVerdict | None = None
  # Faults in the dataset's own make-up: a graph missing, one the format does
  # not name, graphs hanging off different reports.
  problems: list[str] = field(default_factory=list)
  # ``None`` when the report check was not asked for or the file could not be read.
  check: CheckResult | None = None
  unreadable: str | None = None

  @property
  def conforms(self) -> bool:
    """The file meets the format."""
    return (
      not self.problems
      and all(v.conforms for v in self.graphs.values())
      and (self.dataset is None or self.dataset.conforms)
    )

  @property
  def ok(self) -> bool:
    """The file meets the format and, where it was checked, the report holds."""
    return (
      self.conforms
      and self.unreadable is None
      and (self.check is None or self.check.ok)
    )

  def to_dict(self) -> dict[str, Any]:
    return {
      "ok": self.ok,
      "conforms": self.conforms,
      "report": self.report,
      "problems": list(self.problems),
      "graphs": {name: v.to_dict() for name, v in self.graphs.items()},
      "dataset": self.dataset.to_dict() if self.dataset else None,
      "check": self.check.to_dict() if self.check else None,
      "unreadable": self.unreadable,
    }


def validate_holon(
  text: str, *, check: bool = True, max_findings: int = 25
) -> HolonValidation:
  """Validate holon JSON-LD ``text``.

  Raises :class:`~xbrlkit.deserialize.holon.HolonError` when the text is not a
  dataset of named graphs at all; anything short of that is a verdict. With
  ``check`` the report is also read back and checked
  (:func:`~xbrlkit.check.check_report`). ``max_findings`` bounds the findings
  kept per graph; the counts are always complete.
  """
  dataset = Dataset()
  try:
    dataset.parse(data=text, format="json-ld")
  except Exception as exc:  # rdflib raises several unrelated types here
    raise HolonError(f"not JSON-LD: {exc}") from exc

  result = HolonValidation()
  found: dict[str, Graph] = {}
  roots: set[str] = set()
  for graph in dataset.graphs():
    if not len(graph) or not isinstance(graph.identifier, URIRef):
      continue
    root, sep, name = str(graph.identifier).rpartition("#")
    if not sep or name not in GRAPHS:
      result.problems.append(
        f"<{graph.identifier}> is not a graph the format names "
        f"({', '.join('#' + g for g in GRAPHS)})."
      )
      continue
    roots.add(root)
    found[name] = graph
  if not found:
    raise HolonError("no named graph of a holon (#scene, #projection, #boundary)")
  result.problems.sort()

  if len(roots) > 1:
    result.problems.append(
      "The graphs hang off more than one report: " + ", ".join(sorted(roots)) + "."
    )
  result.report = sorted(roots)[0]
  for name in REQUIRED_GRAPHS:
    if name not in found:
      result.problems.append(f"The #{name} graph is missing.")

  union = Graph()
  for name in GRAPHS:
    graph = found.get(name)
    if graph is None:
      result.graphs[name] = GraphVerdict(name=name, present=False)
      continue
    result.graphs[name] = _run(name, graph, max_findings)
    for triple in graph:
      union.add(triple)
  result.dataset = _run(DATASET, union, max_findings)

  reports = set(found.get("scene", Graph()).subjects(RDF.type, _RS.Report))
  if "scene" in found and len(reports) != 1:
    result.problems.append(
      f"#scene describes {len(reports)} rs:Report nodes; a holon is one report."
    )
  elif len(roots) == 1 and reports and URIRef(result.report) not in reports:
    result.problems.append(
      "The rs:Report node is not the IRI the graphs are named from."
    )

  if check:
    try:
      result.check = check_report(from_holon_json(text))
    except HolonError as exc:
      result.unreadable = str(exc)
  return result


def validate_holon_file(path: str | Path, **kwargs: Any) -> HolonValidation:
  """Validate the holon at ``path``."""
  return validate_holon(Path(path).read_text(encoding="utf-8"), **kwargs)


def _run(name: str, graph: Graph, max_findings: int) -> GraphVerdict:
  from pyshacl import validate

  shapes = shapes_graph(name)
  _, results, _ = validate(
    graph, shacl_graph=shapes, inference="none", allow_warnings=True
  )
  verdict = GraphVerdict(
    name=name,
    triples=len(graph),
    shapes=len(set(shapes.subjects(RDF.type, _SH.NodeShape))),
  )
  for node in sorted(results.subjects(RDF.type, _SH.ValidationResult), key=str):
    warning = results.value(node, _SH.resultSeverity) == _SH.Warning
    if warning:
      verdict.warnings += 1
    else:
      verdict.violations += 1
    if len(verdict.findings) >= max_findings:
      continue
    shape = results.value(node, _SH.sourceShape)
    path = results.value(node, _SH.resultPath)
    value = results.value(node, _SH.value)
    message = results.value(node, _SH.resultMessage)
    verdict.findings.append(
      Finding(
        severity="warning" if warning else "violation",
        focus=str(results.value(node, _SH.focusNode) or ""),
        message=str(message or ""),
        shape=str(shape) if isinstance(shape, URIRef) else None,
        path=str(path) if isinstance(path, URIRef) else None,
        value=str(value) if value is not None else None,
      )
    )
  return verdict


def render(result: HolonValidation) -> str:
  """The verdict as the lines a person reads."""
  lines = [f"{'conforms' if result.conforms else 'DOES NOT CONFORM'}  {result.report}"]
  for problem in result.problems:
    lines.append(f"  ! {problem}")
  verdicts = [*result.graphs.values(), *([result.dataset] if result.dataset else [])]
  for verdict in verdicts:
    if not verdict.present:
      lines.append(f"  #{verdict.name:<11} absent")
      continue
    label = verdict.name if verdict.name == DATASET else f"#{verdict.name}"
    state = "ok" if verdict.conforms else f"{verdict.violations} violations"
    if verdict.warnings:
      state += f", {verdict.warnings} warnings"
    lines.append(
      f"  {label:<12} {state}  ({verdict.triples:,} triples, {verdict.shapes} shapes)"
    )
    for finding in verdict.findings:
      where = f" {finding.path}" if finding.path else ""
      lines.append(
        f"      {finding.severity}: {finding.focus}{where}: {finding.message}"
      )
  if result.unreadable:
    lines.append(f"  report       unreadable: {result.unreadable}")
  elif result.check is not None:
    check = result.check
    state = "ok" if check.ok else f"{len(check.problems)} problems"
    lines.append(
      f"  report       {state}  ({check.facts:,} facts, "
      f"{check.calculations_footed}/{check.calculations_checked} calculations foot)"
    )
    for problem in check.problems[:25]:
      lines.append(f"      {problem.code}: {problem.message}")
  return "\n".join(lines)


__all__ = [
  "DATASET",
  "GRAPHS",
  "REQUIRED_GRAPHS",
  "Finding",
  "GraphVerdict",
  "HolonValidation",
  "render",
  "shapes_graph",
  "validate_holon",
  "validate_holon_file",
]
