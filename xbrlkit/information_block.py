"""Information blocks — a filing's networks grouped by extended-link role, and
the disclosures those roles form.

XBRL scatters one section of a report across up to three linkbases that
share nothing but a role URI: the presentation tree that orders it, the
calculation arcs that foot it, and the definition arcs that declare its
hypercube. An :class:`InformationBlock` is the role read whole — Charlie
Hoffman's *Block*, the RoboSystems platform's primary construct, where a
``structures`` row and a Block run 1:1 — and it is the unit a reader asks
for: "the maturities table of the leases note", not "the presentation
network whose role ends in ``LeasesMaturitiesDetails``".

A :class:`Disclosure` is the family a filer's role definitions spell out.
EDGAR filers title every role ``NNNN - Category - Title``, and split a note
over several: the note itself (one text block), its ``(Policies)``, its
``(Tables)`` (the tables as text blocks) and one ``(Details)`` role per
table of facts, each ``Title - Subtitle (Details)``. Reading the family
back off those titles is what lets a reader see a note as one thing and
pick the table it needs. It is a reading of the filer's own words: nothing
here classifies, and a filing whose definitions follow no such convention
(ESEF, a ledger's own report) yields one disclosure per structure.

The hypercube reconstruction follows XBRL Dimensions 1.0 per base set — the
same walk :mod:`xbrlkit.serialize.tavi` makes for its cubes — scoped to one
role, so a fact can be tested for membership in *this* section's cube.

One more reading of the filer's words: a filer's tooling sometimes splits a
section's arcs across ``Role`` and ``Role_1`` — a second calculation tree
that would contradict the first if both sat in one base set — and gives the
two the same definition. That second role is not a section; it is the same
section's arcs in a second drawer, and it folds into the block whose
definition it repeats.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Callable, Literal

from xbrlkit.model import Arc, Network, XbrlFact, XbrlModel

Level = Literal[
  "statement",
  "parenthetical",
  "note",
  "policies",
  "tables",
  "details",
  "document",
  "other",
]

# XBRL Dimensions 1.0 arcroles — what ``Arc.arcrole`` preserves on a
# definition network, and the whole input to the cube reconstruction.
DIM_ALL = "http://xbrl.org/int/dim/arcrole/all"
DIM_NOT_ALL = "http://xbrl.org/int/dim/arcrole/notAll"
DIM_HYPERCUBE_DIMENSION = "http://xbrl.org/int/dim/arcrole/hypercube-dimension"
DIM_DIMENSION_DOMAIN = "http://xbrl.org/int/dim/arcrole/dimension-domain"
DIM_DOMAIN_MEMBER = "http://xbrl.org/int/dim/arcrole/domain-member"
DIM_DIMENSION_DEFAULT = "http://xbrl.org/int/dim/arcrole/dimension-default"

_DEFINITION = re.compile(
  r"^\s*(\d+)\s*-\s*(Statement|Disclosure|Document|Schedule)\s*-\s*(.*?)\s*$",
  re.IGNORECASE,
)
# The level a filer writes into the title: "(Details)", "(Tables)",
# "(Policies)", "(Parenthetical)" — with a filer's own qualifiers tolerated
# ("(Details Textual)", "(Details 2)", "(Parentheticals)").
_SUFFIX = re.compile(
  r"\s*\((Details?|Tables?|Policies|Parentheticals?)\b[^)]*\)", re.IGNORECASE
)
# A family key that is only a note number ("Note 5", "5.") names nothing;
# the whole title is the family then.
_NOTE_NUMBER = re.compile(r"^(?:note\s*)?\d+[a-z]?\.?$", re.IGNORECASE)
# ``Role_1``: a filer's second drawer for one section's arcs.
_ROLE_SUFFIX = re.compile(r"^(.*?)_(\d+)$")


def parse_definition(definition: str | None) -> tuple[str | None, str | None, str]:
  """``"9955528 - Disclosure - Leases (Tables)"`` → ``("9955528", "Disclosure",
  "Leases (Tables)")``. A definition without the EDGAR shape comes back whole
  as the name, with no number and no category."""
  text = (definition or "").strip()
  m = _DEFINITION.match(text)
  if not m:
    return None, None, text
  return m.group(1), m.group(2).capitalize(), m.group(3)


def parse_level(category: str | None, name: str) -> tuple[Level, str, str | None]:
  """The level, the family title and the subtitle a role title spells out.

  ``"Leases - Maturities of lease liabilities (Details)"`` under
  ``Disclosure`` → ``("details", "Leases", "Maturities of lease
  liabilities")``. A statement keeps its whole title as the family; its
  parenthetical joins it. A title that says nothing of its level is the
  note itself under ``Disclosure``, the statement under ``Statement``, and
  ``other`` when the filer used no convention at all.
  """
  suffix = _SUFFIX.search(name)
  stripped = _SUFFIX.sub("", name).strip() if suffix else name.strip()
  word = suffix.group(1).lower() if suffix else ""
  cat = (category or "").lower()
  level: Level
  if word.startswith("detail"):
    level = "details"
  elif word.startswith("table"):
    level = "tables"
  elif word == "policies":
    level = "policies"
  elif word.startswith("parenthetical"):
    level = "parenthetical"
  elif cat == "statement":
    level = "statement"
  elif cat == "disclosure":
    level = "note"
  elif cat == "document":
    level = "document"
  elif cat == "schedule":
    level = "details"
  else:
    level = "other"

  subtitle: str | None = None
  title = stripped
  if level in ("details", "tables", "policies", "note", "parenthetical"):
    head, sep, tail = stripped.partition(" - ")
    if sep and not _NOTE_NUMBER.match(head.strip()):
      title, subtitle = head.strip(), tail.strip() or None
  return level, title or name, subtitle


@dataclass
class Axis:
  """One dimension of a reconstructed hypercube."""

  qname: str
  domain: str | None = None
  # Every member the domain network reaches, in walk order.
  members: list[str] = field(default_factory=list)
  default: str | None = None
  typed: bool = False


@dataclass
class Hypercube:
  """One ``all`` hypercube declared in a role's definition linkbase."""

  qname: str
  axes: list[Axis] = field(default_factory=list)
  # The primary items the ``all`` arcs hang the cube on (the LineItems
  # abstracts, usually).
  primary_items: list[str] = field(default_factory=list)

  @property
  def axis_qnames(self) -> frozenset[str]:
    return frozenset(a.qname for a in self.axes)


@dataclass
class InformationBlock:
  """One extended-link role read whole: its networks, its concepts, its cube.

  The same object the platform's ``InformationBlockEnvelope`` describes,
  read from a filing: what a filing cannot know — the concept-arrangement
  pattern, rules, verification, provenance — stays with the producer, and
  ``block_type`` is carried through only when a producer supplied it.
  """

  role_uri: str
  role_id: str | None
  definition: str | None
  number: str | None
  category: str | None
  # The title as the filer wrote it, less the EDGAR number and category.
  name: str
  # The family title, the level and the subtitle read from the name.
  disclosure: str
  level: Level
  subtitle: str | None = None
  # A producer's own id for the structure, when its networks carried one
  # (serialization-waist phase 3); a filing leaves it None.
  structure_id: str | None = None
  # The producer's block type (``balance_sheet``, ``rollforward`` …), when
  # its networks carried one. A filing never sets it: classifying a role is
  # enrichment, and xbrlkit does not guess.
  block_type: str | None = None
  # Roles folded into this block because they repeat its definition under a
  # numeric suffix (``…Details_1``); their arcs are read as this section's.
  merged_roles: list[str] = field(default_factory=list)
  presentation: list[Network] = field(default_factory=list)
  calculation: list[Network] = field(default_factory=list)
  definition_networks: list[Network] = field(default_factory=list)
  # Every concept the presentation trees cite.
  concepts: set[str] = field(default_factory=set)
  hypercubes: list[Hypercube] = field(default_factory=list)

  @property
  def id(self) -> str:
    """The short name a reader passes back: the role id, else the role's
    last path segment — the same id ``describe_filing`` lists."""
    return self.role_id or self.role_uri.rstrip("/").rsplit("/", 1)[-1]

  @property
  def has_calc(self) -> bool:
    return any(n.arcs for n in self.calculation)

  @property
  def renderable(self) -> bool:
    return bool(self.presentation)

  @property
  def axes(self) -> list[Axis]:
    """The cube axes, in declaration order, one entry per axis."""
    seen: dict[str, Axis] = {}
    for cube in self.hypercubes:
      for axis in cube.axes:
        seen.setdefault(axis.qname, axis)
    return list(seen.values())

  def admits(self, fact: XbrlFact) -> bool:
    """Whether the fact's dimensional signature falls inside this section.

    A fact with no qualifier is the consolidated value and belongs wherever
    its concept is presented. A qualified fact belongs only where a cube
    declares every axis it uses; a section that declares no cube shows
    consolidated values alone, which is what its presentation tree
    promises.
    """
    if not fact.dims:
      return True
    used = frozenset(d.axis_qname for d in fact.dims)
    return any(used <= cube.axis_qnames for cube in self.hypercubes)


@dataclass
class Disclosure:
  """A family of blocks under one title: the note and its tables."""

  name: str
  category: str | None
  number: str | None
  blocks: list[InformationBlock] = field(default_factory=list)

  @property
  def levels(self) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    for block in self.blocks:
      counts[block.level] += 1
    return dict(counts)


_ORDER_PREFIX = re.compile(r"^\s*(\d+)")


def order_key(block: InformationBlock) -> tuple[str, str]:
  """EDGAR's own order: the definition number sorted as a *string* (so the
  six-digit governance roles do not overtake the seven-digit filer ones),
  unnumbered roles last."""
  return (block.number or "~", block.role_uri)


ArcsFrom = Callable[[str, str, str], list[Arc]]


def _walk_domain(root: str, role: str, arcs_from: ArcsFrom) -> list[str]:
  """Every member under a domain, breadth-first, cycle-safe, following each
  arc's ``targetRole`` where one is set."""
  members: list[str] = []
  seen: set[str] = {root}
  queue: list[tuple[str, str]] = [(root, role)]
  while queue:
    parent, current_role = queue.pop(0)
    for arc in arcs_from(current_role, DIM_DOMAIN_MEMBER, parent):
      if arc.to_qname in seen:
        continue
      seen.add(arc.to_qname)
      members.append(arc.to_qname)
      queue.append((arc.to_qname, arc.target_role or current_role))
  return members


def role_folds(model: XbrlModel) -> dict[str, str]:
  """Each role URI → the role it is read as.

  ``Role_1`` folds into ``Role`` when ``Role`` is itself a role of the
  filing and the two share a definition (or the suffixed one has none).
  A suffixed URI whose base is absent, or whose definition differs, is a
  section of its own and stays put.
  """
  roles: set[str] = set()
  definitions: dict[str, str | None] = {}
  for network in model.networks:
    roles.add(network.role_uri)
    if definitions.get(network.role_uri) is None:
      definitions[network.role_uri] = network.definition
  folds: dict[str, str] = {}
  for role in roles:
    folds[role] = role
    m = _ROLE_SUFFIX.match(role)
    if not m or m.group(1) not in roles:
      continue
    base = m.group(1)
    definition = definitions.get(role)
    if definition is None or definition == definitions.get(base):
      folds[role] = base
  return folds


def _definition_arcs(
  model: XbrlModel, folds: dict[str, str] | None = None
) -> dict[str, dict[str, list[Arc]]]:
  """Definition arcs by role and arcrole. A folded role's arcs are filed
  under the role it folds into *and* under its own URI, so a cube walk that
  starts from the block finds them and a ``targetRole`` hop that names the
  suffixed role still resolves."""
  folds = folds or {}
  by_role: dict[str, dict[str, list[Arc]]] = {}
  for network in model.networks:
    if network.kind != "definition":
      continue
    keys = {network.role_uri, folds.get(network.role_uri, network.role_uri)}
    for key in keys:
      bucket = by_role.setdefault(key, {})
      for arc in network.arcs:
        bucket.setdefault(arc.arcrole or "", []).append(arc)
  return by_role


def build_hypercubes(
  by_role: dict[str, dict[str, list[Arc]]], role: str
) -> list[Hypercube]:
  """The ``all`` hypercubes a role declares, each with its axes walked out.

  The traversal is per base set: a cube's axes are the ``hypercube-dimension``
  arcs in the role its ``all`` arc names, an axis's domain the
  ``dimension-domain`` arc in *that* role, and the members the
  ``domain-member`` walk from there — each hop continuing in the previous
  arc's ``targetRole`` where one is set. Defaults are declared once,
  globally, and looked up across every role.
  """

  def arcs_from(r: str, arcrole: str, source: str) -> list[Arc]:
    return [a for a in by_role.get(r, {}).get(arcrole, []) if a.from_qname == source]

  defaults: dict[str, str] = {}
  for bucket in by_role.values():
    for arc in bucket.get(DIM_DIMENSION_DEFAULT, []):
      defaults.setdefault(arc.from_qname, arc.to_qname)

  cubes: dict[str, Hypercube] = {}
  for all_arc in by_role.get(role, {}).get(DIM_ALL, []):
    hypercube = all_arc.to_qname
    cube = cubes.get(hypercube)
    if cube is not None:
      cube.primary_items.append(all_arc.from_qname)
      continue
    cube = Hypercube(qname=hypercube, primary_items=[all_arc.from_qname])
    cubes[hypercube] = cube
    hypercube_role = all_arc.target_role or role
    for hd in arcs_from(hypercube_role, DIM_HYPERCUBE_DIMENSION, hypercube):
      axis_qname = hd.to_qname
      if any(a.qname == axis_qname for a in cube.axes):
        continue
      axis_role = hd.target_role or hypercube_role
      domain_arcs = arcs_from(axis_role, DIM_DIMENSION_DOMAIN, axis_qname)
      if not domain_arcs:
        cube.axes.append(
          Axis(qname=axis_qname, default=defaults.get(axis_qname), typed=True)
        )
        continue
      domain = domain_arcs[0].to_qname
      members = _walk_domain(domain, domain_arcs[0].target_role or axis_role, arcs_from)
      cube.axes.append(
        Axis(
          qname=axis_qname,
          domain=domain,
          members=members,
          default=defaults.get(axis_qname),
        )
      )
  return list(cubes.values())


def plan_blocks(model: XbrlModel) -> list[InformationBlock]:
  """Every role in the filing as one :class:`InformationBlock`, in EDGAR order."""
  by_role: dict[str, InformationBlock] = {}
  folds = role_folds(model)
  for network in model.networks:
    role = folds.get(network.role_uri, network.role_uri)
    st = by_role.get(role)
    if st is None:
      number, category, name = parse_definition(network.definition)
      level, title, subtitle = parse_level(category, name or role)
      st = InformationBlock(
        role_uri=role,
        role_id=network.role_id,
        definition=network.definition,
        number=number,
        category=category,
        name=name or role,
        disclosure=title,
        level=level,
        subtitle=subtitle,
      )
      by_role[role] = st
    if network.role_uri != role and network.role_uri not in st.merged_roles:
      st.merged_roles.append(network.role_uri)
    elif st.definition is None and network.definition:
      # The first network seen had no definition; a later one names the role.
      number, category, name = parse_definition(network.definition)
      level, title, subtitle = parse_level(category, name)
      st.definition, st.number, st.category, st.name = (
        network.definition,
        number,
        category,
        name,
      )
      st.disclosure, st.level, st.subtitle = title, level, subtitle
    if st.role_id is None and network.role_id:
      st.role_id = network.role_id
    if st.structure_id is None and network.structure_id:
      st.structure_id = network.structure_id
    if network.kind == "presentation":
      st.presentation.append(network)
      if st.block_type is None and network.block_type:
        st.block_type = network.block_type
      for arc in network.arcs:
        st.concepts.add(arc.from_qname)
        st.concepts.add(arc.to_qname)
    elif network.kind == "calculation":
      st.calculation.append(network)
    else:
      st.definition_networks.append(network)

  definition_arcs = _definition_arcs(model, folds)
  for st in by_role.values():
    if st.definition_networks:
      st.hypercubes = build_hypercubes(definition_arcs, st.role_uri)
  return sorted(by_role.values(), key=order_key)


def group_disclosures(blocks: list[InformationBlock]) -> list[Disclosure]:
  """Blocks under one title, in the order the first of each appears.

  Only blocks with a presentation tree join a family — a role that
  carries calculation or definition arcs alone is scaffolding for a
  section named elsewhere, not a section of its own.
  """
  families: dict[tuple[str, str], Disclosure] = {}
  for st in blocks:
    if not st.renderable:
      continue
    key = (st.disclosure.lower(), (st.category or "").lower())
    fam = families.get(key)
    if fam is None:
      fam = Disclosure(name=st.disclosure, category=st.category, number=st.number)
      families[key] = fam
    fam.blocks.append(st)
  return list(families.values())


def fact_membership(
  model: XbrlModel, blocks: list[InformationBlock]
) -> dict[str, list[XbrlFact]]:
  """Role URI → the facts that belong to that block.

  A fact belongs to every section whose presentation cites its concept and
  whose cube admits its dimensions (:meth:`InformationBlock.admits`). An authored
  report that pinned a fact to one structure (``XbrlFact.structure_id``)
  keeps the pin.
  """
  by_concept: dict[str, list[InformationBlock]] = defaultdict(list)
  for st in blocks:
    if not st.renderable:
      continue
    for concept in st.concepts:
      by_concept[concept].append(st)
  by_structure_id = {st.structure_id: st for st in blocks if st.structure_id}
  out: dict[str, list[XbrlFact]] = defaultdict(list)
  for fact in model.facts:
    pinned = by_structure_id.get(fact.structure_id or "")
    if pinned is not None:
      out[pinned.role_uri].append(fact)
      continue
    for st in by_concept.get(fact.concept_qname, ()):
      if st.admits(fact):
        out[st.role_uri].append(fact)
  return dict(out)
