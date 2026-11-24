"""Project a holon into a DataBook: the report as a document a person reads.

A holon is complete and nobody reads it raw. A DataBook is a Markdown file
that is at once a readable document, a container of typed data blocks and a
self-describing artefact (YAML frontmatter saying what it is and how it was
made). This module writes one *from* a holon, and the DataBook says so: its
process stamp names the holon as its primary input, by IRI and by digest.

It is a projection, not a copy. Each section of the report becomes a table of
its consolidated values, with the same facts beside it as a small Turtle block
a processor can load into the holon's ``#scene`` graph. The arrangement, the
roll-ups, the dimensional breakdowns and the ledger stay in the holon, and the
DataBook points at the named graph that holds each. Whoever wants everything
goes to the holon.

The frontmatter keeps to the DataBook core keys; what is particular to a
report sits under one key of its own, ``xbrl_holon``.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

from rdflib import RDF, XSD, Graph, Namespace, URIRef

from .. import __version__
from ..deserialize.holon import from_holon_report
from ..information_block import InformationBlock, fact_membership, plan_blocks
from ..model import Arc, Concept, Period, XbrlFact, XbrlModel
from ..namespaces import HOLON_VOCAB
from . import DATASET, GRAPHS, HolonValidation, read_graphs, validate_holon

RS = Namespace(HOLON_VOCAB)
SHAPES_BASE = f"{HOLON_VOCAB}shapes/"
REPOSITORY = "https://github.com/RoboFinSystems/xbrlkit"

# What a DataBook says about each of the holon's graphs: how much of it is here.
PROJECTED = "projected"  # shown as tables, and as facts in reference blocks
REFERENCED = "referenced"  # in the holon only; named here by IRI
ABSENT = "absent"

# A section's table stops being readable long before a holon stops being
# useful: a section past these bounds is summarised and left to the holon.
MAX_COLUMNS = 4
MAX_ROWS = 400
# Across the whole document. A filing has a hundred detail tables behind its
# statements and notes; past this many rows the rest are listed, not drawn.
MAX_DOCUMENT_ROWS = 1500
# A filer's "(Tables)" and "(Details)" roles are the workings behind a note.
# They are most of a filing's sections and the least read, so they are named
# in the contents and left to the holon unless asked for.
DETAIL_LEVELS = ("tables", "details")
# A column fewer than this share of the fullest column's rows use is a stray
# comparative (one prior-year balance in a note), and is left to the holon.
MIN_COLUMN_SHARE = 0.2
# Narrative shown under a section: how many passages, and how much of each.
MAX_PASSAGES = 3
PASSAGE_LENGTH = 900
MAX_INLINE_FACTS = 250
# A DataBook is for graph data too small to need a store; past this many
# triples in all, the remaining sections point at the holon instead.
MAX_INLINE_TRIPLES = 3000
TEXT_PREVIEW = 96

# The predicates of a fact a reference block carries. The nodes they point at
# (the element, the period, the unit) are described in the holon.
_FACT_PREDICATES = (
  RDF.type,
  RS.element,
  RS.period,
  RS.unit,
  RS.numericValue,
  RS.decimals,
  RS.factType,
)

_TAG = re.compile(r"<[^>]+>")
_SPACE = re.compile(r"\s+")
_SLUG = re.compile(r"[^a-z0-9]+")
_TOTAL_ROLES = ("totalLabel", "periodEndLabel", "netLabel")


@dataclass
class _Row:
  depth: int
  label: str
  qname: str
  abstract: bool = False
  total: bool = False
  values: dict[str, XbrlFact] = field(default_factory=dict)


@dataclass
class _Section:
  block: InformationBlock
  slug: str
  title: str
  rows: list[_Row]
  columns: list[Period]
  shown: list[XbrlFact]
  facts: int
  dimensional: int
  axes: list[str]
  truncated: bool
  # Left out of the document to keep it readable; named in its contents.
  listed_only: bool = False


def databook_from_holon(
  text: str,
  *,
  href: str | None = None,
  created: date | None = None,
  timestamp: datetime | None = None,
  validation: HolonValidation | None = None,
  validate: bool = True,
  inline_rdf: bool = True,
  details: bool = False,
  links: bool = True,
) -> str:
  """The DataBook for the holon in ``text``.

  ``href`` is where a reader can get the holon (a file name beside the
  DataBook, or a URL); the holon's IRI and its SHA-256 are written either way.
  The verdicts in the DataBook come from ``validation``, or from validating
  ``text`` here unless ``validate`` is false. ``inline_rdf`` controls the
  Turtle block under each section, and ``details`` draws a filing's "(Tables)"
  and "(Details)" sections too, which are otherwise only named. With ``links``
  every value in a table is a link to its fact in the holon. ``created`` and ``timestamp`` default to the
  report's own date and to now.
  """
  model, _ = from_holon_report(text)
  graphs, roots = read_graphs(text)
  root = sorted(roots)[0]
  if validation is None and validate:
    validation = validate_holon(text)
  digest = hashlib.sha256(text.encode("utf-8")).hexdigest()

  sections = _sections(model, details=details)
  scene = graphs.get("scene", Graph())
  fact_nodes = {
    str(scene.value(node, RS.internalId)): node
    for node in scene.subjects(RDF.type, RS.Fact)
    if isinstance(node, URIRef)
  }
  blocks: list[tuple[_Section, str | None, int]] = []
  inline_triples = 0
  inline_subjects: set[URIRef] = set()
  for section in sections:
    turtle, count, subjects = (
      _reference_block(scene, section, fact_nodes)
      if inline_rdf and not section.listed_only and inline_triples < MAX_INLINE_TRIPLES
      else (None, 0, set())
    )
    inline_triples += count
    inline_subjects |= subjects
    blocks.append((section, turtle, count))

  stamp = (timestamp or datetime.now(UTC)).astimezone(UTC).replace(microsecond=0)
  made = created or model.filing.filing_date or model.filing.report_date or stamp.date()
  title = _title(model)
  front = _frontmatter(
    model=model,
    root=root,
    title=title,
    created=made,
    stamp=stamp,
    href=href,
    digest=digest,
    graphs=graphs,
    validation=validation,
    inline_triples=inline_triples,
    inline_subjects=len(inline_subjects),
    sections=sections,
  )
  body = _body(
    model=model,
    root=root,
    title=title,
    href=href,
    digest=digest,
    graphs=graphs,
    validation=validation,
    blocks=blocks,
    fact_nodes=fact_nodes if links else {},
  )
  return f"---\n{front}---\n\n{body}"


def to_databook(model: XbrlModel, **kwargs: Any) -> str:
  """The DataBook for ``model``: its holon written, then projected."""
  from ..serialize import to_holon

  return databook_from_holon(to_holon(model), **kwargs)


# -- the report as sections ----------------------------------------------------


def _sections(model: XbrlModel, *, details: bool = False) -> list[_Section]:
  blocks = [b for b in plan_blocks(model) if b.renderable]
  membership = fact_membership(model, blocks)
  periods = {p.id: p for p in model.periods}

  def producer_order(block: InformationBlock) -> int | None:
    orders = [
      n.structure_order for n in block.presentation if n.structure_order is not None
    ]
    return min(orders) if orders else None

  # A producer that ordered its sections keeps its order; a filing keeps EDGAR's.
  if any(producer_order(b) is not None for b in blocks):
    blocks.sort(key=lambda b: (producer_order(b) is None, producer_order(b) or 0))

  sections: list[_Section] = []
  slugs: dict[str, int] = defaultdict(int)
  for block in blocks:
    facts = membership.get(block.role_uri, [])
    if not facts:
      continue
    consolidated = _most_precise([f for f in facts if not f.dims])
    by_concept: dict[str, list[XbrlFact]] = defaultdict(list)
    for fact in consolidated:
      by_concept[fact.concept_qname].append(fact)
    columns = _columns(consolidated, periods)
    keep = {p.id for p in columns}
    rows, truncated = _rows(model, block, by_concept, keep)
    # A concept presented twice (cash at the start and at the end of a
    # period) is one fact shown twice.
    shown = list({f.id: f for row in rows for f in row.values.values()}.values())
    base = _SLUG.sub("-", block.name.lower()).strip("-")[:48] or "section"
    slugs[base] += 1
    slug = base if slugs[base] == 1 else f"{base}-{slugs[base]}"
    dimensional = [f for f in facts if f.dims]
    axes = sorted({d.axis_qname for f in dimensional for d in f.dims})
    sections.append(
      _Section(
        block=block,
        slug=slug,
        title=block.name,
        rows=rows,
        columns=columns,
        shown=shown,
        facts=len(facts),
        dimensional=len(dimensional),
        axes=axes,
        truncated=truncated,
      )
    )
  drawn = 0
  for section in sections:
    detail = section.block.level in DETAIL_LEVELS and not details
    if detail or drawn >= MAX_DOCUMENT_ROWS:
      section.listed_only = True
      section.rows, section.shown = [], []
    drawn += len(section.rows)
  return sections


def _precision(decimals: str | None) -> float:
  if decimals is None:
    return float("-inf")
  if decimals.upper() == "INF":
    return float("inf")
  try:
    return float(decimals)
  except ValueError:
    return float("-inf")


def _most_precise(facts: list[XbrlFact]) -> list[XbrlFact]:
  """One fact per concept, period and unit: the one stated most precisely."""
  best: dict[tuple[Any, ...], XbrlFact] = {}
  for fact in facts:
    key = (fact.concept_qname, fact.period_id, fact.unit_id, fact.language)
    current = best.get(key)
    if current is None or _precision(fact.decimals) > _precision(current.decimals):
      best[key] = fact
  return list(best.values())


def _columns(facts: list[XbrlFact], periods: dict[str, Period]) -> list[Period]:
  """The section's periods, newest first, the longer span ahead at one end date."""
  used: dict[str, int] = defaultdict(int)
  for fact in facts:
    if fact.period_id in periods:
      used[fact.period_id] += 1

  def key(period_id: str) -> tuple[date, int, int]:
    period = periods[period_id]
    span = (
      (period.end - period.start).days
      if period.period_type == "duration" and period.start and period.end
      else 0
    )
    return (period.end or date.min, span, used[period_id])

  fullest = max(used.values(), default=0)
  wanted = [i for i in used if used[i] >= fullest * MIN_COLUMN_SHARE]
  return [periods[i] for i in sorted(wanted, key=key, reverse=True)[:MAX_COLUMNS]]


def _rows(
  model: XbrlModel,
  block: InformationBlock,
  by_concept: dict[str, list[XbrlFact]],
  keep: set[str],
) -> tuple[list[_Row], bool]:
  """The presentation trees walked in order, one row per concept shown."""
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

  rows: list[_Row] = []
  truncated = False

  def visit(qname: str, depth: int, role: str | None, trail: tuple[str, ...]) -> None:
    nonlocal truncated
    if len(rows) >= MAX_ROWS:
      truncated = True
      return
    concept = model.concepts.get(qname)
    # A table and its axes are the breakdown's scaffolding, not rows. A
    # member needs no test: it has no consolidated value and is not abstract.
    structural = concept is not None and (
      concept.is_hypercube_item or concept.is_dimension_item
    )
    if not structural:
      values = {
        f.period_id: f for f in by_concept.get(qname, []) if f.period_id in keep
      }
      abstract = concept is not None and concept.is_abstract
      if values or abstract:
        rows.append(
          _Row(
            depth=depth,
            label=_label(concept, qname, role),
            qname=qname,
            abstract=abstract,
            total=qname in totals or bool(role and role.endswith(_TOTAL_ROLES)),
            values=values,
          )
        )
    # An axis heads its domain and members: the breakdown, which is the holon's.
    if concept is not None and concept.is_dimension_item:
      return
    if qname in trail or depth > 14:
      return
    for arc in children.get(qname, []):
      visit(arc.to_qname, depth + 1, arc.preferred_label, (*trail, qname))

  for root in roots:
    if skip_root:
      for arc in children.get(root, []):
        visit(arc.to_qname, 0, arc.preferred_label, (root,))
    else:
      visit(root, 0, None, ())
  # An abstract heading with nothing beneath it says nothing. Dropping one can
  # leave its own heading bare, so repeat until none is.
  pruned = rows
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
  return pruned, truncated


def _label(concept: Concept | None, qname: str, role: str | None) -> str:
  if concept is None:
    return qname.split(":")[-1]
  if role:
    for label in concept.labels:
      if label.role == role and label.value:
        return label.value
  return concept.pref_label or concept.name


# -- values --------------------------------------------------------------------


def _period_label(period: Period) -> str:
  if period.period_type == "instant":
    return period.end.isoformat() if period.end else period.id
  if period.period_type == "forever":
    return "forever"
  start = period.start.isoformat() if period.start else "?"
  end = period.end.isoformat() if period.end else "?"
  return f"{start} to {end}"


def _cell(fact: XbrlFact | None) -> str:
  if fact is None:
    return ""
  if fact.is_nil:
    return "nil"
  if fact.value_kind == "numeric" and fact.numeric_value is not None:
    return _number(fact)
  text = _SPACE.sub(" ", _TAG.sub(" ", fact.value_str or "")).strip()
  if len(text) > TEXT_PREVIEW:
    return f"*text, {len(text):,} characters*"
  return _escape(text)


def _linked(
  fact: XbrlFact | None,
  fact_nodes: dict[str, URIRef],
  labels: dict[str, str],
  defined: list[str],
) -> str:
  """The cell, as a link to the fact it shows.

  A reference-style link: the row stays as short as its value, and the fact's
  IRI is written once beneath the table. This is the DataBook's form of
  inline tagging: the number a person reads *is* the fact in the holon.
  """
  text = _cell(fact)
  if fact is None or not text:
    return text
  node = fact_nodes.get(fact.id)
  if node is None:
    return text
  label = labels.get(fact.id)
  if label is None:
    label = f"f{len(labels) + 1}"
    labels[fact.id] = label
    defined.append(f"[{label}]: <{node}>")
  return f"[{text}][{label}]"


def _number(fact: XbrlFact) -> str:
  """The number as the report states it, with separators; a negative in parentheses."""
  lexical = fact.value_str if fact.value_str and _is_plain(fact.value_str) else None
  if lexical is None:
    value = fact.numeric_value or 0.0
    lexical = f"{value:.2f}" if value != int(value) else str(int(value))
  negative = lexical.startswith("-")
  whole, _, fraction = lexical.lstrip("+-").partition(".")
  # A value reads to the places it is stated to: 204,844.90 at two places,
  # 1,000 at none. Digits past the stated places are kept; they were reported.
  places = fact.decimals or ""
  significant = fraction.rstrip("0")
  if places.lstrip("-").isdigit():
    fraction = significant.ljust(min(max(int(places), 0), 6), "0")
  grouped = f"{int(whole or '0'):,}" + (f".{fraction}" if fraction else "")
  return f"({grouped})" if negative and grouped.strip("0.,") else grouped


def _count(number: int, noun: str) -> str:
  return f"{number:,} {noun}{'' if number == 1 else 's'}"


def _is_plain(lexical: str) -> bool:
  return bool(re.fullmatch(r"[+-]?\d+(\.\d+)?", lexical))


def _escape(text: str) -> str:
  return text.replace("\\", "\\\\").replace("|", "\\|")


# -- the Turtle beside a table -------------------------------------------------


def _reference_block(
  scene: Graph, section: _Section, fact_nodes: dict[str, URIRef]
) -> tuple[str | None, int, set[URIRef]]:
  """The facts a section's table shows, as Turtle for the ``#scene`` graph."""
  numeric = [
    f
    for f in section.shown
    if f.value_kind == "numeric" and f.numeric_value is not None
  ]
  if not numeric or len(numeric) > MAX_INLINE_FACTS:
    return None, 0, set()
  graph = Graph()
  for prefix, namespace in scene.namespaces():
    graph.bind(prefix, namespace, replace=True)
  graph.bind("rs", RS, replace=True)
  graph.bind("xsd", XSD, replace=True)
  names = graph.namespace_manager
  subjects: set[URIRef] = set()
  statements: list[str] = []
  triples = 0
  for fact in sorted(numeric, key=lambda f: f.id):
    node = fact_nodes.get(fact.id)
    if node is None or node in subjects:
      continue
    subjects.add(node)
    # Written by hand, not by a serializer: one would rewrite 124000 as
    # 124000.0, and a block has to say exactly what the holon says.
    pairs = [
      ("a" if predicate == RDF.type else predicate.n3(names), value.n3(names))
      for predicate in _FACT_PREDICATES
      for value in sorted(scene.objects(node, predicate), key=str)
    ]
    triples += len(pairs)
    body = " ;\n".join(f"    {predicate} {value}" for predicate, value in pairs)
    statements.append(f"{node.n3(names)}\n{body} .")
  if not statements:
    return None, 0, set()
  text = "\n\n".join(statements)
  prefixes = [
    f"@prefix {prefix}: <{namespace}> ."
    for prefix, namespace in sorted(graph.namespaces())
    if re.search(rf"(?<![\w<]){re.escape(prefix)}:", text)
  ]
  return "\n".join(prefixes) + "\n\n" + text, triples, subjects


# -- frontmatter ---------------------------------------------------------------


def _title(model: XbrlModel) -> str:
  name = model.entity.name or model.entity.cik
  parts = [p for p in (model.filing.form, _report_period(model)) if p]
  return f"{name}: {' '.join(parts)}" if parts else name


def _report_period(model: XbrlModel) -> str | None:
  if model.filing.report_date:
    return f"for the period ended {model.filing.report_date.isoformat()}"
  ends = [p.end for p in model.periods if p.end]
  return f"for the period ended {max(ends).isoformat()}" if ends else None


def _disposition(name: str, graphs: dict[str, Graph]) -> str:
  if name not in graphs:
    return ABSENT
  return PROJECTED if name == "scene" else REFERENCED


def _frontmatter(
  *,
  model: XbrlModel,
  root: str,
  title: str,
  created: date,
  stamp: datetime,
  href: str | None,
  digest: str,
  graphs: dict[str, Graph],
  validation: HolonValidation | None,
  inline_triples: int,
  inline_subjects: int,
  sections: list[_Section],
) -> str:
  holon_input: dict[str, Any] = {
    "iri": root,
    "role": "primary",
    "description": (
      f"The report's holon (sha256:{digest}). This DataBook is a projection "
      "of it; every value here is a fact in its #scene graph."
    ),
  }
  inputs = [holon_input]
  if validation is not None:
    inputs.append(
      {
        "iri": SHAPES_BASE,
        "role": "constraint",
        "description": "The holon format's SHACL shapes, one set per named graph.",
      }
    )
  front: dict[str, Any] = {
    "id": f"{root}/databook",
    "title": title,
    "type": "databook",
    "version": "1.0.0",
    "created": created.isoformat(),
    "description": (
      f"{title}, as a document to read: each section of the report as a "
      "table of its consolidated values, with the same facts beside it as "
      "RDF. A projection of the report's holon, which holds everything."
    ),
    "domain": HOLON_VOCAB,
    "subject": ["financial report", "XBRL", "holon"],
  }
  if inline_triples:
    front["graph"] = {
      "namespace": HOLON_VOCAB,
      "named_graph": f"{root}#scene",
      "triple_count": inline_triples,
      "subjects": inline_subjects,
      "rdf_version": "1.1",
      "turtle_version": "1.1",
      "reification": False,
    }
  if validation is not None:
    front["shapes"] = [
      f"{SHAPES_BASE}{name}"
      for name in (*GRAPHS, DATASET)
      if name in graphs or name == DATASET
    ]
  front["process"] = {
    "transformer": f"xbrlkit {__version__}",
    "transformer_type": "script",
    "transformer_iri": REPOSITORY,
    "inputs": inputs,
    "timestamp": stamp.strftime("%Y-%m-%dT%H:%M:%SZ"),
    "note": (
      "Written from the holon alone: nothing here was read from anywhere "
      "else, so the DataBook can be made again from the file it describes."
    ),
  }

  report: dict[str, Any] = {"iri": root, "id": model.filing.accession}
  report["entity"] = model.entity.name or model.entity.cik
  for key, value in (
    ("form", model.filing.form),
    ("period_end", model.filing.report_date),
    ("filed", model.filing.filing_date),
    ("reporting_style", model.filing.reporting_style),
  ):
    if value:
      report[key] = value.isoformat() if isinstance(value, date) else value
  holon: dict[str, Any] = {"iri": root, "sha256": digest}
  if href:
    holon["href"] = href
  own: dict[str, Any] = {
    "report": report,
    "holon": holon,
    "graphs": [
      {
        "name": name,
        "iri": f"{root}#{name}",
        "disposition": _disposition(name, graphs),
        **({"triples": len(graphs[name])} if name in graphs else {}),
      }
      for name in GRAPHS
    ],
    "sections": [
      {"id": s.slug, "name": s.title, "facts": s.facts, "drawn": not s.listed_only}
      for s in sections
    ],
  }
  if validation is not None:
    own["validation"] = _validation_summary(validation)
  front["xbrl_holon"] = own
  return _yaml(front)


def _validation_summary(validation: HolonValidation) -> dict[str, Any]:
  verdicts = [*validation.graphs.values()]
  if validation.dataset is not None:
    verdicts.append(validation.dataset)
  summary: dict[str, Any] = {
    "conforms": validation.conforms,
    "graphs": [
      {
        "name": v.name,
        "conforms": v.conforms,
        "violations": v.violations,
        "warnings": v.warnings,
      }
      for v in verdicts
      if v.present
    ],
  }
  if validation.problems:
    summary["problems"] = list(validation.problems)
  if validation.check is not None:
    check = validation.check
    summary["report"] = {
      "ok": check.ok,
      "facts": check.facts,
      "calculations_checked": check.calculations_checked,
      "calculations_footed": check.calculations_footed,
      "problems": len(check.problems),
    }
  return summary


def _yaml(value: Any, indent: int = 0) -> str:
  """A plain mapping as YAML. Strings are written as JSON strings, which YAML
  reads as double-quoted scalars, so nothing in a company's name can break it."""
  pad = "  " * indent
  lines: list[str] = []
  if isinstance(value, dict):
    for key, item in value.items():
      if isinstance(item, dict) and item:
        lines.append(f"{pad}{key}:\n{_yaml(item, indent + 1)}".rstrip("\n"))
      elif isinstance(item, list) and item:
        lines.append(f"{pad}{key}:\n{_yaml(item, indent + 1)}".rstrip("\n"))
      else:
        lines.append(f"{pad}{key}: {_scalar(item)}")
  elif isinstance(value, list):
    for item in value:
      if isinstance(item, dict) and all(
        not isinstance(v, dict | list) for v in item.values()
      ):
        flow = ", ".join(f"{key}: {_scalar(v)}" for key, v in item.items())
        if len(flow) <= 160:
          lines.append(f"{pad}- {{{flow}}}")
          continue
        inner = _yaml(item, indent + 1).split("\n")
        lines.append(f"{pad}- {inner[0].lstrip()}")
        lines.extend(line for line in inner[1:] if line)
      elif isinstance(item, dict):
        inner = _yaml(item, indent + 1).split("\n")
        first = inner[0].lstrip()
        lines.append(f"{pad}- {first}")
        lines.extend(line for line in inner[1:] if line)
      else:
        lines.append(f"{pad}- {_scalar(item)}")
  return "\n".join(lines) + "\n"


def _scalar(value: Any) -> str:
  if value is None:
    return "null"
  if isinstance(value, bool):
    return "true" if value else "false"
  if isinstance(value, int | float):
    return str(value)
  if isinstance(value, dict | list):
    return "{}" if isinstance(value, dict) else "[]"
  return json.dumps(str(value), ensure_ascii=False)


# -- body ----------------------------------------------------------------------


def _body(
  *,
  model: XbrlModel,
  root: str,
  title: str,
  href: str | None,
  digest: str,
  graphs: dict[str, Graph],
  validation: HolonValidation | None,
  blocks: list[tuple[_Section, str | None, int]],
  fact_nodes: dict[str, URIRef],
) -> str:
  where = f"[`{href}`]({href})" if href else f"`{root}`"
  out: list[str] = [
    f"# {title}",
    "",
    "## Overview",
    "",
    (
      "This is a report as a document to read. Each section below is one "
      "information block of the report: a table of its consolidated values, "
      "and beneath it the same facts as RDF. It is a **projection of the "
      f"report's holon** ({where}), and the holon holds everything this "
      "leaves out: how each section is arranged, the roll-ups the values must "
      "obey, the breakdowns by segment or other axis, and, where the producer "
      "attached it, the ledger behind each figure."
    ),
    "",
  ]
  if blocks:
    out += ["| Section | Facts | Shown here |", "|---|---:|---:|"]
    for section, _, _ in blocks:
      if section.listed_only:
        out.append(f"| {_escape(section.title)} | {section.facts:,} | in the holon |")
      else:
        out.append(
          f"| [{_escape(section.title)}](#{section.slug}) | {section.facts:,} | "
          f"{len(section.shown):,} |"
        )
    out.append("")
    left = sum(1 for section, _, _ in blocks if section.listed_only)
    if left:
      out += [
        (
          f"{_count(left, 'section')} {'is' if left == 1 else 'are'} named above "
          "and not drawn here: the tables "
          "and details behind the notes, and anything past about "
          f"{MAX_DOCUMENT_ROWS:,} rows, so that this stays a document to read. "
          "Every one of them is in the holon."
        ),
        "",
      ]
  else:
    out += ["The report has no section with facts to show.", ""]

  # One label per fact across the document, so a fact shown in two sections
  # is the same link in both.
  labels: dict[str, str] = {}
  for section, turtle, count in blocks:
    if not section.listed_only:
      out += _section_markdown(section, turtle, count, root, fact_nodes, labels)

  out += _validation_markdown(validation)
  out += _holon_markdown(root, href, digest, graphs)
  return "\n".join(out).rstrip() + "\n"


def _section_markdown(
  section: _Section,
  turtle: str | None,
  count: int,
  root: str,
  fact_nodes: dict[str, URIRef],
  labels: dict[str, str],
) -> list[str]:
  out = [f'<a id="{section.slug}"></a>', "", f"## {section.title}", ""]
  if section.rows and section.columns:
    header = " | ".join(_period_label(p) for p in section.columns)
    out += [
      f"| | {header} |",
      "|---|" + "---:|" * len(section.columns),
    ]
    # Where each value's link goes. Written once, under the first table that
    # shows the fact; a renderer hides these lines and resolves the links.
    defined: list[str] = []
    for row in section.rows:
      label = "&nbsp;&nbsp;" * row.depth + _escape(row.label)
      if row.abstract or row.total:
        label = "&nbsp;&nbsp;" * row.depth + f"**{_escape(row.label)}**"
      cells = " | ".join(
        _linked(row.values.get(p.id), fact_nodes, labels, defined)
        for p in section.columns
      )
      out.append(f"| {label} | {cells} |")
    out.append("")
    if defined:
      out += [*defined, ""]
  else:
    out += ["No consolidated value in this section; see the holon.", ""]

  out += _passages(section)

  notes: list[str] = []
  if section.truncated:
    notes.append(
      f"The table stops at {MAX_ROWS} rows; the section continues in the holon."
    )
  if section.dimensional:
    axes = ", ".join(f"`{a}`" for a in section.axes[:6])
    more = f" and {len(section.axes) - 6} more" if len(section.axes) > 6 else ""
    notes.append(
      f"{_count(section.dimensional, 'further fact')} "
      f"{'breaks' if section.dimensional == 1 else 'break'} these values out by "
      f"{axes}{more}. They are in the holon's `#scene` graph."
    )
  notes.append(f"Section role: `{section.block.role_uri}`")
  out += [f"- {note}" for note in notes] + [""]

  if turtle is None and any(f.value_kind == "numeric" for f in section.shown):
    out += ["*These facts are in the holon's `#scene` graph, not repeated here.*", ""]
  if turtle:
    out += [
      "<details>",
      f"<summary>RDF: {_count(len(section.shown), 'fact')} above ({count:,} triples)</summary>",
      "",
      f"<!-- databook:id: {section.slug}-facts -->",
      f"<!-- databook:label: {section.title}: the facts in the table above -->",
      f"<!-- databook:graph: {root}#scene -->",
      "<!-- mode=reference -->",
      "```turtle",
      turtle,
      "```",
      "",
      "The element, period and unit each fact points at are described in the holon.",
      "",
      "</details>",
      "",
    ]
  return out


def _passages(section: _Section) -> list[str]:
  """The section's narrative, where its facts are text too long for a cell."""
  newest = section.columns[0].id if section.columns else None
  out: list[str] = []
  shown = 0
  for row in section.rows:
    fact = row.values.get(newest or "")
    if fact is None or fact.value_kind == "numeric" or fact.is_nil:
      continue
    text = _SPACE.sub(" ", _TAG.sub(" ", fact.value_str or "")).strip()
    if len(text) <= TEXT_PREVIEW:
      continue
    if shown == MAX_PASSAGES:
      out += ["*Further passages are in the holon.*", ""]
      break
    shown += 1
    excerpt = (
      text if len(text) <= PASSAGE_LENGTH else text[:PASSAGE_LENGTH].rsplit(" ", 1)[0]
    )
    more = "" if len(text) <= PASSAGE_LENGTH else " *[continues in the holon]*"
    out += [f"**{_escape(row.label)}**", "", f"> {excerpt}{more}", ""]
  return out


def _validation_markdown(validation: HolonValidation | None) -> list[str]:
  if validation is None:
    return []
  verdict = (
    "conforms to the holon format" if validation.conforms else "does not conform"
  )
  out = [
    "## Validation",
    "",
    f"The holon this was written from **{verdict}**. Each named graph is "
    "checked against its own SHACL shapes, and the graphs together for "
    "references that cross them.",
    "",
    "| Graph | Verdict | Triples | Violations | Warnings |",
    "|---|---|---:|---:|---:|",
  ]
  verdicts = [*validation.graphs.values()]
  if validation.dataset is not None:
    verdicts.append(validation.dataset)
  for graph in verdicts:
    name = "the graphs together" if graph.name == DATASET else f"`#{graph.name}`"
    if not graph.present:
      out.append(f"| {name} | absent | | | |")
      continue
    out.append(
      f"| {name} | {'conforms' if graph.conforms else 'fails'} | "
      f"{graph.triples:,} | {graph.violations} | {graph.warnings} |"
    )
  out.append("")
  for problem in validation.problems:
    out.append(f"- {problem}")
  if validation.unreadable:
    out += [f"The report could not be read back: {validation.unreadable}", ""]
  elif validation.check is not None:
    check = validation.check
    state = (
      "holds together"
      if check.ok
      else f"has {len(check.problems)} inconsistencies of its own"
    )
    out += [
      (
        f"Read back, the report **{state}**: {check.facts:,} facts, "
        f"{check.calculations_footed:,} of {check.calculations_checked:,} "
        "calculations foot. This is a check that the report agrees with "
        "itself, not that it is complete or true."
      ),
      "",
    ]
  return out


def _holon_markdown(
  root: str, href: str | None, digest: str, graphs: dict[str, Graph]
) -> list[str]:
  about = {
    "scene": "what the report states: the facts and what each points at",
    "projection": "how it is arranged: structures, presentation and definition arcs",
    "boundary": "the roll-ups the facts must obey: calculation arcs",
    "lineage": "the ledger behind the facts: contributions, lines, entries, events",
  }
  here = {
    PROJECTED: "projected: the tables and Turtle blocks above",
    REFERENCED: "in the holon only",
    ABSENT: "not in this holon",
  }
  out = [
    "## The holon",
    "",
    "Everything in this document comes from one file, and the file has more.",
    "",
    f"- **Report**: `{root}`",
    *([f"- **Holon**: [`{href}`]({href})"] if href else []),
    f"- **SHA-256**: `{digest}`",
    "",
    "| Named graph | Holds | Triples | In this document |",
    "|---|---|---:|---|",
  ]
  for name in GRAPHS:
    triples = f"{len(graphs[name]):,}" if name in graphs else ""
    out.append(
      f"| `{root}#{name}` | {about[name]} | {triples} | "
      f"{here[_disposition(name, graphs)]} |"
    )
  out += [
    "",
    (
      "To read the whole report: `xbrlkit view <holon>` opens it rendered, "
      "`xbrlkit serve` puts it behind tools a model can call, and "
      "`xbrlkit validate <holon>` repeats the check above."
    ),
    "",
  ]
  return out


__all__ = ["databook_from_holon", "to_databook"]
