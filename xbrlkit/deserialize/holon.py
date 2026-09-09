"""Read a holon (the RDF/JSON-LD projection) back into the neutral ``XbrlModel``.

The inverse of :mod:`xbrlkit.serialize.holon`. The holon is a dataset of three
named graphs — the scene (report, entity, facts, periods, units, dimensions),
the projection (elements, structures, associations) and the boundary (the
calculation roll-ups) — and this walks the nodes back into one model.

It reads the document structurally rather than through rdflib, for one reason
that matters: the holon binds each taxonomy prefix to a **year-less stem**
(``dei`` to ``http://xbrl.sec.gov/dei/``), so expanding an element IRI and
contracting it again is what would lose the QName, which is the model's key
for everything. Reading the compact form keeps ``dei:AmendmentFlag`` intact.
An absolute IRI is still accepted and reversed through the document's own
``@context``, so a holon written expanded reads the same.

**What the holon carries, and what it does not.** Facts with their values,
decimals and dimensions — text facts included, with the full tagged HTML —
elements with abstractness, balance, period type, item type and their kind
(axis, hypercube, member), presentation and calculation associations with
order, weight and preferred label role, structures by role URI, periods and
units as first-class nodes, and the whole of the filing's identity on the
report node. Four things are genuinely absent and are left empty:

- **label roles.** An element carries one ``prefLabel`` and no palette, so the
  negated / total / period-start labels a renderer would use are gone.
- **taxonomy namespaces.** Prefixes are bound to year-less stems, so a
  concept's ``namespace`` is that stem rather than the filing's own URI.
- **duplicate facts**, collapsed at emit by content — the OIM consistent-
  duplicates rule, so a filing's fact count comes back as its distinct count.
- **fact language, the nil-versus-empty distinction** (an empty string fact and
  a nil fact are both written with an empty value), the datatype detail
  (``nillable``, the item type's own QName) and the hypercube *declarations*,
  though every dimension a fact actually carries survives.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
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
  Unit,
  XbrlFact,
  XbrlModel,
)
from ..namespaces import CONCEPT_BASE, ENTITY_SCHEME, HOLON_VOCAB
from ..serialize._values import CIK_SCHEME
from ..serialize.tavi import LABEL_ROLE_TYPES
from ..parse.ids import unit_id
from ..periods import duration_period, forever_period, instant_period

# The vocabulary terms, compact and expanded, so a node reads either way.
VOCAB_PREFIX = "rs:"

REPORT = "Report"
ENTITY = "Entity"
ELEMENT = "Element"
PERIOD = "Period"
UNIT = "Unit"
FACT = "Fact"
DIMENSION = "Dimension"
STRUCTURE = "Structure"
ASSOCIATION = "Association"

# Vocabulary term -> label role URI, inverted from the emitter's own map so a
# role a holon writes and a role this reads cannot drift apart. `prefLabel` and
# `documentation` are the two roles with a predicate of their own.
LABEL_TERM_ROLES: dict[str, str] = {
  label_type.split(":", 1)[-1]: role for role, label_type in LABEL_ROLE_TYPES.items()
}
STANDARD_LABEL_ROLE_URI = "http://www.xbrl.org/2003/role/label"
# The emitter names the standard-role label `standardLabel`; `label` is read as
# well, because holons written by 0.7.0-0.7.2 used it.
LABEL_TERM_ROLES["standardLabel"] = STANDARD_LABEL_ROLE_URI
LABEL_TERM_ROLES["label"] = STANDARD_LABEL_ROLE_URI
DOCUMENTATION_LABEL_ROLE = "http://www.xbrl.org/2003/role/documentation"

PARENT_CHILD_ARCROLE = "http://www.xbrl.org/2003/arcrole/parent-child"
SUMMATION_ITEM_ARCROLE = "http://www.xbrl.org/2003/arcrole/summation-item"
DIMENSION_ARCROLE_BASE = "http://xbrl.org/int/dim/arcrole/"
# The arcrole an arc takes when the association did not carry its own. A
# definition network has no single arcrole — its arcs are the dimensional
# wiring — so one that arrives without one keeps none.
DEFAULT_ARCROLES: dict[str, str | None] = {
  "presentation": PARENT_CHILD_ARCROLE,
  "calculation": SUMMATION_ITEM_ARCROLE,
  "definition": None,
}
STANDARD_LABEL_ROLE = "http://www.xbrl.org/2003/role/label"

# `elementType` says which kind of element the holon is describing.
ELEMENT_KINDS = {"axis": "axis", "hypercube": "hypercube", "member": "member"}

# `itemType` is the holon's own value-domain vocabulary, not XBRL's item type:
# it says what kind of value a fact carries so a renderer can format it, and
# several XBRL types collapse into one domain (`dei:yesNoItemType` arrives as
# `string`). Reading it back as the canonical item type of that domain keeps
# the field meaning one thing across importers; what it cannot do is recover
# the type the filing actually declared.
HOLON_ITEM_TYPES: dict[str, str] = {
  "monetary": "monetaryItemType",
  "perShare": "perShareItemType",
  "shares": "sharesItemType",
  "percent": "percentItemType",
  "pure": "pureItemType",
  "integer": "integerItemType",
  "decimal": "decimalItemType",
  "textBlock": "textBlockItemType",
  "date": "dateItemType",
  "boolean": "booleanItemType",
  "string": "stringItemType",
}
NUMERIC_DOMAINS = frozenset(
  {"monetary", "perShare", "shares", "percent", "pure", "integer", "decimal"}
)


class HolonError(ValueError):
  """The document is not a holon this importer can read."""


@dataclass
class ImportGaps:
  """What the holon could not supply, per field of the model."""

  missing: list[str] = field(default_factory=list)
  unresolved_references: int = 0

  def to_dict(self) -> dict[str, object]:
    return {
      "missing": sorted(self.missing),
      "unresolved_references": self.unresolved_references,
    }


def from_holon_json(text: str) -> XbrlModel:
  """Read a holon from its JSON-LD text."""
  model, _ = from_holon_report(text)
  return model


def from_holon(document: Mapping[str, Any]) -> XbrlModel:
  """Read a holon that is already parsed JSON."""
  model, _ = _read(document)
  return model


def from_holon_report(text: str) -> tuple[XbrlModel, ImportGaps]:
  """Read a holon, returning the model and what it could not supply."""
  try:
    document = json.loads(text)
  except ValueError as exc:
    raise HolonError(f"not JSON: {exc}") from exc
  if not isinstance(document, Mapping):
    raise HolonError("not a JSON object")
  return _read(document)


def _read(document: Mapping[str, Any]) -> tuple[XbrlModel, ImportGaps]:
  prefixes = _prefixes(document.get("@context"))
  nodes = _nodes(document)
  if not nodes:
    raise HolonError("no nodes in `@graph`")
  by_type = _by_type(nodes)
  if FACT not in by_type and ELEMENT not in by_type:
    raise HolonError("no rs:Fact or rs:Element nodes")
  gaps = ImportGaps(
    missing=[
      "an element no fact, network or dimension mentions",
      "concept references (the reference linkbase)",
      "the fraction flag and Arelle's display type",
      "network role_id",
      "fact source_hash and raw_value",
      "whether the filing behind the report was inline XBRL",
      "is_text_fact for a concept that reports nothing",
    ]
  )

  entity = _entity(by_type.get(ENTITY, []))
  concepts = _concepts(by_type.get(ELEMENT, []), prefixes)
  periods = _periods(by_type.get(PERIOD, []))
  units = _units(by_type.get(UNIT, []), prefixes)
  dimensions = _dimensions(by_type.get(DIMENSION, []), prefixes)
  facts = _facts(
    by_type.get(FACT, []), concepts, periods, units, dimensions, entity, prefixes, gaps
  )
  _mark_text_facts(concepts, facts)
  networks = _networks(
    by_type.get(ASSOCIATION, []), by_type.get(STRUCTURE, []), prefixes, concepts
  )
  filing = _filing(by_type.get(REPORT, []), entity, concepts)

  return (
    XbrlModel(
      filing=filing,
      entity=entity,
      concepts=concepts,
      periods=[period for period, _ in periods.values()],
      units=[unit for unit, _ in units.values()],
      facts=facts,
      networks=networks,
    ),
    gaps,
  )


# -- the document as nodes -------------------------------------------------------


def _prefixes(context: Any) -> dict[str, str]:
  """Prefix -> stem, from the document's own ``@context``."""
  prefixes: dict[str, str] = {}
  for entry in context if isinstance(context, Sequence) else [context]:
    for key, value in _mapping(entry).items():
      if isinstance(value, str) and not key.startswith("@"):
        prefixes[key] = value
  return prefixes


def _nodes(document: Mapping[str, Any]) -> list[Mapping[str, Any]]:
  """Every node object in the dataset, named graphs flattened into one list."""
  collected: list[Mapping[str, Any]] = []

  def walk(value: Any) -> None:
    if isinstance(value, Sequence) and not isinstance(value, str):
      for item in value:
        walk(item)
      return
    node = _mapping(value)
    if not node:
      return
    inner = node.get("@graph")
    if inner is not None:
      walk(inner)
      return
    if "@id" in node or "@type" in node:
      collected.append(node)

  walk(document.get("@graph"))
  return collected


def _by_type(nodes: Sequence[Mapping[str, Any]]) -> dict[str, list[Mapping[str, Any]]]:
  """Nodes bucketed by vocabulary term, a node landing under each of its types."""
  buckets: dict[str, list[Mapping[str, Any]]] = {}
  for node in nodes:
    types = node.get("@type")
    for entry in types if isinstance(types, list) else [types]:
      term = _term(entry)
      if term:
        buckets.setdefault(term, []).append(node)
  return buckets


def _term(value: Any) -> str | None:
  """The local name of a vocabulary term, compact (``rs:Fact``) or expanded."""
  if not isinstance(value, str):
    return None
  if value.startswith(VOCAB_PREFIX):
    return value[len(VOCAB_PREFIX) :]
  if value.startswith(HOLON_VOCAB):
    return value[len(HOLON_VOCAB) :]
  return None


def _qname(value: Any, prefixes: Mapping[str, str]) -> str:
  """An element reference as its QName.

  The holon writes a standard concept compact (``us-gaap:Revenues``) and an
  extension concept under the concept base with the QName already inside it.
  An absolute IRI is reversed through the context's prefix stems.
  """
  ref = _reference(value)
  if not ref:
    return ""
  if ref.startswith(CONCEPT_BASE):
    return ref[len(CONCEPT_BASE) :]
  if "://" not in ref:
    return ref
  for prefix, stem in prefixes.items():
    if stem and "://" in stem and ref.startswith(stem):
      local = ref[len(stem) :]
      if local and ":" not in local and "/" not in local:
        return f"{prefix}:{local}"
  return ref


def _reference(value: Any) -> str:
  """A property value that points at another node, as a plain string."""
  if isinstance(value, str):
    return value
  node = _mapping(value)
  target = node.get("@id")
  return target if isinstance(target, str) else ""


# -- identity --------------------------------------------------------------------


def _entity(nodes: Sequence[Mapping[str, Any]]) -> EntityIdentity:
  node = _mapping(nodes[0]) if nodes else {}
  identifier = _text(node.get("internalId")) or ""
  return EntityIdentity(
    cik=identifier,
    scheme=_text(node.get("scheme")) or _scheme_for(identifier),
    name=_text(node.get("prefLabel")),
    legal_name=_text(node.get("legalName")),
    ein=_text(node.get("ein")),
    ticker=_text(node.get("ticker")),
    sic=_text(node.get("sic")),
  )


def _scheme_for(identifier: str) -> str:
  """The scheme an entity is identified under when the holon does not say.

  A holon written from a `StatementBundle` carries no scheme — the bundle has
  no field for one — and calling a ledger's entity an SEC CIK because that is
  the model's default is a false statement about the report. A ten-digit
  identifier is a CIK; anything else is read under the neutral entity scheme,
  which is the same convention the OIM-family projections write it under.
  """
  digits = identifier.isdigit() and len(identifier) == 10
  return CIK_SCHEME if digits else ENTITY_SCHEME


def _filing(
  nodes: Sequence[Mapping[str, Any]],
  entity: EntityIdentity,
  concepts: Mapping[str, Concept],
) -> FilingMeta:
  """The report node is the whole of the filing's identity in a holon."""
  node = _mapping(nodes[0]) if nodes else {}
  return FilingMeta(
    accession=_text(node.get("accessionNumber")) or "unknown",
    cik=entity.cik,
    form=_text(node.get("form")),
    is_inline_xbrl=None,
    filing_date=_date(node.get("filingDate")),
    fiscal_year_focus=_text(node.get("fiscalYearFocus")),
    fiscal_period_focus=_text(node.get("fiscalPeriodFocus")),
    fiscal_year_end_month=_text(node.get("fiscalYearEndMonth")),
    report_date=_date(node.get("periodEndDate")),
    taxonomy_namespaces=sorted(
      {concept.namespace for concept in concepts.values() if concept.namespace}
    ),
  )


# -- elements, periods, units ----------------------------------------------------


def _concepts(
  nodes: Sequence[Mapping[str, Any]], prefixes: Mapping[str, str]
) -> dict[str, Concept]:
  concepts: dict[str, Concept] = {}
  for node in nodes:
    qname = _qname(node.get("@id"), prefixes)
    if not qname or qname in concepts:
      continue
    kind = _text(node.get("elementType")) or ""
    prefix, _, local = qname.partition(":")
    domain = _text(node.get("itemType")) or ""
    abstract = _bool(node.get("abstract"))
    declared = _text(node.get("dataType"))
    # `string` is the holon's fallback domain, written for every element it
    # cannot place — an abstract heading included. On an element that reports
    # nothing it carries no information, so it is not read back as a type.
    # `dataType` is the type the taxonomy declared; `itemType` is only the
    # domain it was bucketed into, so the declared one wins where it is there.
    # A holon written before `dataType` existed has the domain alone — and its
    # `string` is the catch-all written for everything the emitter could not
    # place, so it is read as no type rather than as a string type.
    item_type_qname = declared if declared and ":" in declared else None
    if declared:
      item_type = declared.split(":", 1)[-1]
    elif domain and domain != "string":
      item_type = HOLON_ITEM_TYPES.get(domain, domain)
    else:
      item_type = None
    pref_label = _text(node.get("prefLabel"))
    concepts[qname] = Concept(
      qname=qname,
      namespace=_namespace(prefix, prefixes) if local else "",
      name=local or qname,
      period_type=_period_type(node.get("periodType")),
      balance=_balance(node.get("balance")),
      is_abstract=abstract,
      is_numeric=domain in NUMERIC_DOMAINS or _bool(node.get("monetary")),
      is_textblock=domain == "textBlock",
      is_shares=domain == "shares",
      is_integer=domain == "integer",
      # Whether a fact of this concept is an OIM text fact is settled by the
      # facts themselves further down — a language is written for one and not
      # for anything else — because the declared type cannot answer it: both
      # `dei:centralIndexKeyItemType` and `dei:yesNoItemType` derive from
      # token and only one of them takes a language. A text block is one
      # whatever its facts carry.
      is_text_fact=domain == "textBlock",
      is_hypercube_item=kind == "hypercube",
      is_dimension_item=kind == "axis",
      is_domain_member=kind == "member",
      substitution_group=_text(node.get("substitutionGroup")),
      item_type=item_type,
      item_type_qname=item_type_qname,
      item_type_namespace=(
        _namespace(item_type_qname.split(":", 1)[0], prefixes)
        if item_type_qname
        else None
      ),
      nillable=_bool(node.get("nillable")),
      base_xsd_type=_text(node.get("baseType")),
      pref_label=pref_label,
      labels=_concept_labels(node, pref_label),
    )
  return concepts


def _concept_labels(node: Mapping[str, Any], pref_label: str | None) -> list[Label]:
  """Every label on an element: the preferred one plus a term per label role.

  Each role has a term of its own, so they come back with their language
  intact. ``skos:prefLabel`` is the same text as the standard-role label and is
  only read when no term carried it — which is how a holon written before the
  terms existed still yields a label.
  """
  labels: list[Label] = []
  for key, value in node.items():
    role = (
      DOCUMENTATION_LABEL_ROLE if key == "documentation" else LABEL_TERM_ROLES.get(key)
    )
    if role is None:
      continue
    for entry in _list(value):
      # An empty label is not a missing one — the model keeps the difference —
      # so this reads the value rather than asking whether it is truthy.
      text = entry.get("@value") if isinstance(entry, Mapping) else entry
      if not isinstance(text, str):
        continue
      language = _text(entry.get("@language")) if isinstance(entry, Mapping) else None
      labels.append(Label(value=text, role=role, language=language))
  if pref_label and not any(label.role == STANDARD_LABEL_ROLE_URI for label in labels):
    # A holon written before the label terms existed has the preferred label
    # and nothing else, and the preferred label *is* the standard-role one.
    labels.insert(0, Label(value=pref_label, role=STANDARD_LABEL_ROLE_URI))
  return labels


def _namespace(prefix: str, prefixes: Mapping[str, str]) -> str:
  """The namespace a prefix names, as the model writes it — no separator.

  The document binds a prefix to the stem a local name appends to, so the
  fragment separator comes off again here: an XBRL target namespace is
  ``http://fasb.org/us-gaap/2024``, and the ``#`` belongs to the concept IRI
  rather than to the namespace.
  """
  stem = prefixes.get(prefix, "")
  return stem[:-1] if stem.endswith("#") else stem


def _periods(
  nodes: Sequence[Mapping[str, Any]],
) -> dict[str, tuple[Period, str]]:
  """Period nodes by their holon id, each with the period it describes.

  The dates are already the human-facing inclusive ones, so the calendar fields
  are recomputed rather than read: they are derived either way, and deriving
  them keeps one implementation.
  """
  periods: dict[str, tuple[Period, str]] = {}
  for node in nodes:
    node_id = _text(node.get("@id")) or ""
    if not node_id or node_id in periods:
      continue
    kind = _text(node.get("periodType"))
    instant = _date(node.get("instant"))
    start = _date(node.get("startDate"))
    end = _date(node.get("endDate"))
    if kind == "instant" and instant:
      period = instant_period(instant)
    elif start and end:
      period = duration_period(start, end)
    elif kind == "forever":
      period = forever_period()
    else:
      continue
    periods[node_id] = (period, node_id)
  return periods


def _units(
  nodes: Sequence[Mapping[str, Any]], prefixes: Mapping[str, str]
) -> dict[str, tuple[Unit, str]]:
  """Unit nodes by their holon id. The holon keeps the filing's own measure."""
  units: dict[str, tuple[Unit, str]] = {}
  for node in nodes:
    node_id = _text(node.get("@id")) or ""
    measure = _qname(node.get("measure"), prefixes)
    if not node_id or not measure or node_id in units:
      continue
    if "/" in measure:
      numerator, _, denominator = measure.partition("/")
      num_uri = _measure_uri(numerator, prefixes)
      den_uri = _measure_uri(denominator, prefixes)
      unit = Unit(
        id=unit_id(f"{num_uri}/{den_uri}"),
        measure=measure,
        uri=f"{num_uri}/{den_uri}",
        numerator_uri=num_uri,
        denominator_uri=den_uri,
      )
    else:
      uri = _measure_uri(measure, prefixes)
      unit = Unit(id=unit_id(uri), measure=measure, uri=uri)
    units[node_id] = (unit, node_id)
  return units


def _measure_uri(measure: str, prefixes: Mapping[str, str]) -> str:
  """``namespace#localName`` for a measure token, the form the parse minted."""
  prefix, _, local = measure.partition(":")
  if not local:
    return measure
  stem = prefixes.get(prefix)
  if stem:
    return f"{stem.rstrip('#')}#{local}" if "#" in stem else f"{stem}#{local}"
  return f"{prefix}#{local}"


def _dimensions(
  nodes: Sequence[Mapping[str, Any]], prefixes: Mapping[str, str]
) -> dict[str, DimQualifier]:
  """Dimension nodes by their holon id — first-class, so facts point at them."""
  dimensions: dict[str, DimQualifier] = {}
  for node in nodes:
    node_id = _text(node.get("@id")) or ""
    axis = _qname(node.get("axis"), prefixes)
    if not node_id or not axis:
      continue
    explicit = _bool(node.get("isExplicit"), default=True)
    member = _qname(node.get("member"), prefixes)
    axis_type = _text(node.get("axisType"))
    dimensions[node_id] = DimQualifier(
      axis_qname=axis,
      member_qname=member or None if explicit else None,
      typed_value=_text(node.get("typedValue")) or (None if explicit else member),
      is_explicit=explicit,
      axis_type=axis_type if axis_type in ("segment", "scenario") else None,
    )
  return dimensions


# -- facts -----------------------------------------------------------------------


def _facts(
  nodes: Sequence[Mapping[str, Any]],
  concepts: Mapping[str, Concept],
  periods: Mapping[str, tuple[Period, str]],
  units: Mapping[str, tuple[Unit, str]],
  dimensions: Mapping[str, DimQualifier],
  entity: EntityIdentity,
  prefixes: Mapping[str, str],
  gaps: ImportGaps,
) -> list[XbrlFact]:
  facts: list[XbrlFact] = []
  for node in nodes:
    concept_qname = _qname(node.get("element"), prefixes)
    period_ref = _reference(node.get("period"))
    period = periods.get(period_ref)
    if not concept_qname or period is None:
      gaps.unresolved_references += 1
      continue
    unit = units.get(_reference(node.get("unit")))
    numeric_text = _text(node.get("numericValue"))
    string_value = node.get("stringValue")
    value_str = numeric_text if numeric_text is not None else _text(string_value)
    dims = [
      dimensions[ref]
      for ref in (_reference(entry) for entry in _list(node.get("dimension")))
      if ref in dimensions
    ]
    # `isNil` says it outright; a holon written before it existed leaves an
    # empty value behind, which is the same shape as a fact reported empty.
    declared_nil = node.get("isNil")
    facts.append(
      XbrlFact(
        id=_text(node.get("internalId")) or _text(node.get("@id")) or "",
        concept_qname=concept_qname,
        period_id=period[0].id,
        unit_id=unit[0].id if unit is not None else None,
        entity_cik=entity.cik,
        entity_scheme=entity.scheme,
        entity_identifier=entity.cik,
        dims=dims,
        value_str=value_str,
        numeric_value=_float(numeric_text),
        decimals=_text(node.get("decimals")),
        value_kind="numeric" if unit is not None else "text",
        is_nil=_bool(declared_nil) if declared_nil is not None else value_str is None,
        language=_text(node.get("language")),
      )
    )
  return facts


def _mark_text_facts(concepts: dict[str, Concept], facts: Sequence[XbrlFact]) -> None:
  """A fact carrying a language settles its concept's text-fact flag.

  The same rule the TAVI importer applies, over the same evidence: the emitters
  write a language for an OIM text fact and for nothing else, so a fact that
  has one says what its type could not.
  """
  for fact in facts:
    if not fact.language:
      continue
    concept = concepts.get(fact.concept_qname)
    if concept is not None:
      concept.is_text_fact = True


# -- networks --------------------------------------------------------------------


def _network_kind(node: Mapping[str, Any]) -> NetworkKind | None:
  """Which linkbase an association came from.

  ``associationType`` says it outright — and says ``definition`` for the
  dimensional wiring, which is why a holon can carry the definition networks a
  TAVI document turns into cubes and cannot give back. The arcrole is the
  fallback for a holon that omits the type.
  """
  declared = _text(node.get("associationType"))
  if declared in ("presentation", "calculation", "definition"):
    return declared
  arcrole = _text(node.get("arcrole")) or ""
  if arcrole == SUMMATION_ITEM_ARCROLE:
    return "calculation"
  if arcrole == PARENT_CHILD_ARCROLE:
    return "presentation"
  if arcrole.startswith(DIMENSION_ARCROLE_BASE):
    return "definition"
  return None


def _networks(
  associations: Sequence[Mapping[str, Any]],
  structures: Sequence[Mapping[str, Any]],
  prefixes: Mapping[str, str],
  concepts: dict[str, Concept],
) -> list[Network]:
  """Associations grouped back into one network per role and linkbase kind.

  A presentation association carries the preferred label it resolved to — the
  text *and* its role — which is the one place a holon keeps a label other than
  the standard one. Those are hung back on the concept as it goes past, because
  the renderer looks the preferred label up on the concept, and without them a
  statement renders under standard labels the filer did not choose.
  """
  definitions: dict[str, str] = {}
  for node in structures:
    role_uri = _text(node.get("roleUri"))
    name = _text(node.get("structureName")) or _text(node.get("prefLabel"))
    if role_uri and name:
      definitions.setdefault(role_uri, name)

  grouped: dict[tuple[str, NetworkKind], list[Mapping[str, Any]]] = {}
  for node in associations:
    role_uri = _text(node.get("role"))
    kind = _network_kind(node)
    if not role_uri or kind is None:
      continue
    grouped.setdefault((role_uri, kind), []).append(node)

  networks: list[Network] = []
  for (role_uri, kind), nodes in grouped.items():
    edges = [
      (_qname(node.get("from"), prefixes), _qname(node.get("to"), prefixes), node)
      for node in nodes
    ]
    edges = [edge for edge in edges if edge[0] and edge[1]]
    for _, target, node in edges:
      _recover_label(concepts.get(target), node)
    roots = {source for source, _, _ in edges} - {target for _, target, _ in edges}
    arcrole = DEFAULT_ARCROLES[kind]
    networks.append(
      Network(
        role_uri=role_uri,
        definition=definitions.get(role_uri),
        kind=kind,
        arcs=[
          Arc(
            from_qname=source,
            to_qname=target,
            arcrole=_text(node.get("arcrole")) or arcrole,
            order=_float(node.get("order")),
            weight=_float(node.get("weight")),
            preferred_label=_text(node.get("preferredLabelRole")),
            is_root=source in roots,
          )
          for source, target, node in edges
        ],
      )
    )
  return networks


def _recover_label(concept: Concept | None, node: Mapping[str, Any]) -> None:
  """Add an association's resolved preferred label to the concept it names."""
  role = _text(node.get("preferredLabelRole"))
  value = _text(node.get("preferredLabel"))
  if concept is None or not role or value is None:
    return
  if any(label.role == role and label.value == value for label in concept.labels):
    return
  concept.labels.append(Label(value=value, role=role))


# -- reading JSON defensively ----------------------------------------------------


def _mapping(value: Any) -> Mapping[str, Any]:
  return value if isinstance(value, Mapping) else {}


def _list(value: Any) -> Sequence[Any]:
  if value is None:
    return []
  if isinstance(value, Sequence) and not isinstance(value, str):
    return value
  return [value]


def _text(value: Any) -> str | None:
  if isinstance(value, Mapping):
    value = value.get("@value")
  if value is None:
    return None
  text = str(value)
  return text if text != "" else None


def _bool(value: Any, *, default: bool = False) -> bool:
  if isinstance(value, bool):
    return value
  text = _text(value)
  if text is None:
    return default
  return text.lower() == "true"


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
