"""Build the flat holon RDF graph **directly** from a neutral ``XbrlModel``.

This is the full-fidelity path: unlike the ``StatementBundle`` route (numeric-
only, four primary statements, dimensions dropped), this walks the whole slice —
every fact (numeric *and* text), every concept, every network (presentation,
calculation, and the XBRL-dimensions *definition* wiring), and dimensional
qualifiers as first-class ``rs:Dimension`` nodes — into one flat
:class:`rdflib.Graph`. The kernel's :func:`serialize_holon_jsonld_from_graph`
then partitions it into the scene / boundary / projection named graphs.

It reuses the kernel's URI minting, namespaces, and ``@context`` so the emitted
vocabulary stays identical to the framework seeds; the only additions are the
v1.1 superset terms (``rs:Dimension`` fidelity layer, ``rs:stringValue`` /
``rs:factType``, ``rs:durationType``).

Structures are emitted as the raw slice produces them — a role URI, the role
*definition* as the section name, and the reified associations — with **no**
semantic ``blockType``/``canonical_type`` (that classification is enrichment,
which is out of scope; the renderer keys on the structure + its factSet + the
presentation arcs, not a type).

Section membership (which facts render under which statement/disclosure) is a
deterministic ``rs:FactSet`` grouping: a fact joins the factSet of every
structure whose *presentation* network cites its concept. The parity queries and
the renderer both read that linkage; no semantic enrichment is involved.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from decimal import Decimal

from collections.abc import Mapping

from rdflib import RDF, RDFS, XSD, Graph, Literal, URIRef

from ..model import Concept, Network, Unit, XbrlModel
from .tavi import LABEL_ROLE_TYPES
from ..namespaces import FACTSET_BASE, REPORT_BASE
from ._kernel.jsonld import (
  LINK,
  RS,
  SERIALIZATION_VERSION,
  SKOS,
  XBRLI,
  XLINK,
  _arcrole_uri,
  _concept_uri,
  _measure_uri,
  _scoped,
  _structure_arrangement,
)

_FACTSET_BASE = FACTSET_BASE

# Label role URI -> the vocabulary term its labels are written under. Derived
# from the Tavi emitter's role map so the two projections name a role the same
# way and neither can drift: Tavi's `xbrl:terseLabel` is this `rs:terseLabel`.
LABEL_ROLE_TERMS: dict[str, str] = {
  role: label_type.split(":", 1)[-1] for role, label_type in LABEL_ROLE_TYPES.items()
}
# The two roles that already have a predicate: the standard label is what
# `skos:prefLabel` means, and a documentation label is `rdfs:comment`.
STANDARD_LABEL_ROLE = "http://www.xbrl.org/2003/role/label"
DOCUMENTATION_LABEL_ROLE = "http://www.xbrl.org/2003/role/documentation"


def _slug(value: str) -> str:
  """A short, stable, path-safe id for a role/dimension URI."""
  return hashlib.md5(value.encode()).hexdigest()[:16]


def namespace_bindings(model: XbrlModel) -> dict[str, str]:
  """Prefix -> namespace for the taxonomies *this filing* declares.

  The concepts carry their real namespace, so the document can bind its own
  prefixes rather than lean on a fixed table of year-normalized stems. Two
  things were wrong with that table: it addressed a us-gaap concept inside
  FASB's namespace at an IRI FASB never minted (`…/us-gaap/Revenues`, no year),
  and it had no entry at all for a filer's own taxonomy, so every extension
  concept fell back to a robosystems.ai URL and never compacted to a QName.
  Year-independent identity is `rs-gaap`'s job — a real taxonomy with
  equivalence arcs onto each us-gaap version — not something to fake by
  dropping the year out of somebody else's namespace.

  A prefix a filing binds twice is left out: it cannot compact unambiguously,
  and the QName-carrying fallback is the honest answer for it.
  """
  seen: dict[str, set[str]] = {}
  for concept in model.concepts.values():
    # A concept's own namespace, and the one its declared type lives in: a
    # filing can name a type from a taxonomy it declares no concept from (the
    # DTR types), and that prefix needs binding too or the type comes back
    # without its namespace.
    for qname, namespace in (
      (concept.qname, concept.namespace),
      (concept.item_type_qname, concept.item_type_namespace),
    ):
      if not qname or not namespace:
        continue
      prefix, _, local = qname.partition(":")
      if local:
        seen.setdefault(prefix, set()).add(namespace)
  # A unit is a QName too, and its namespace may be one no concept declares —
  # the unit registry, a filer's own unit of measure.
  for unit in model.units:
    for token, uri in _measure_parts(unit):
      prefix, _, local = token.partition(":")
      namespace = uri[: -len(local) - 1] if local and uri.endswith(f"#{local}") else ""
      if local and namespace:
        seen.setdefault(prefix, set()).add(namespace)
  return {
    prefix: _namespace_stem(next(iter(namespaces)))
    for prefix, namespaces in sorted(seen.items())
    if len(namespaces) == 1
  }


def _measure_parts(unit: Unit) -> list[tuple[str, str]]:
  """A unit's measure token(s) paired with the URI each resolved to."""
  if unit.numerator_uri and unit.denominator_uri and "/" in unit.measure:
    numerator, _, denominator = unit.measure.partition("/")
    return [(numerator, unit.numerator_uri), (denominator, unit.denominator_uri)]
  return [(unit.measure, unit.uri)] if unit.uri else []


def _namespace_stem(namespace: str) -> str:
  """A namespace as the stem a local name appends to.

  An XBRL target namespace names the schema, and a concept in it is a fragment
  of that schema, so the separator is `#` unless the namespace already ends in
  one or is written as a path.
  """
  return namespace if namespace.endswith(("#", "/")) else f"{namespace}#"


def _factset_uri(role_uri: str) -> URIRef:
  return URIRef(f"{_FACTSET_BASE}{_slug(role_uri)}")


def holon_root(report_id: str) -> URIRef:
  """The report IRI a holon's named graphs (#scene/#boundary/#projection) hang off."""
  return URIRef(f"{REPORT_BASE}{report_id}")


_ORDER_PREFIX = re.compile(r"^\s*(\d+)")


def _order_key(name: str | None) -> str:
  """Lexicographic sort key from a SEC role definition's leading number.

  SEC role definitions read ``"{number} - {Type} - {Name}"``. That number is a
  *string* sort key, not an integer: a filer's own sections are 7-digit
  (``9952153``) while the standard ecd/cyd governance roles are 6-digit
  (``995445``), so a numeric sort drops the 6-digit governance codes ahead of the
  statements. Sorting the digit string lexicographically — exactly what the SEC
  adapter does (``ORDER BY number``) — keeps statements first. Unnumbered roles
  sort last (``~`` follows every digit in ASCII).
  """
  m = _ORDER_PREFIX.match(name or "")
  return m.group(1) if m else "~"


@dataclass
class _Structure:
  """One extended-link-role network group (presentation + calc + definition)."""

  role_uri: str
  slug: str
  name: str
  order: int | None = None
  presentation: list[Network] = field(default_factory=list)
  calculation: list[Network] = field(default_factory=list)
  definition: list[Network] = field(default_factory=list)
  pres_concepts: set[str] = field(default_factory=set)

  @property
  def has_calc(self) -> bool:
    return any(n.arcs for n in self.calculation)

  @property
  def renderable(self) -> bool:
    """A structure is a section only if it has a presentation tree."""
    return bool(self.presentation)


def build_holon_graph(
  model: XbrlModel,
  *,
  report_id: str | None = None,
  namespaces: Mapping[str, str] | None = None,
) -> Graph:
  """Assemble the flat holon graph from the whole ``XbrlModel`` slice.

  ``namespaces`` is the prefix map the concepts resolve against; it defaults to
  the filing's own (:func:`namespace_bindings`) and must be the same map the
  document's ``@context`` is built from, or the emitted IRIs will not compact.
  """
  report_id = report_id or model.filing.accession
  root = holon_root(report_id)
  entity_node = _scoped(root, "entity", model.entity.cik)
  ns = namespace_bindings(model) if namespaces is None else dict(namespaces)

  g = Graph()
  _add_root(g, model, root, entity_node)
  _add_elements(g, model, ns)
  _add_periods(g, model, root)
  _add_units(g, model, root, ns)

  structures = _plan_structures(model)
  _add_structures(g, structures, root, model, ns)

  dim_uris = _add_dimensions(g, model, root, ns)
  membership = _fact_membership(model, structures)
  _add_facts(g, model, root, entity_node, dim_uris, membership, ns)
  _add_information_blocks(g, structures, membership, root)
  return g


# ── Root + entity ──────────────────────────────────────────────────────────


def _add_root(
  g: Graph,
  model: XbrlModel,
  root: URIRef,
  entity_node: URIRef,
) -> None:
  g.add((root, RDF.type, RS.Report))
  g.add((root, RS.serializationVersion, Literal(SERIALIZATION_VERSION)))
  g.add((root, RS.mode, Literal("report")))
  g.add((root, RS.reportingStyle, Literal("sec-as-filed")))
  g.add((root, RS.entity, entity_node))

  # Filing metadata on the Report node — identifies the filing (accession/form/
  # date/fiscal focus), mirroring the SEC graph's Report node.
  filing = model.filing
  g.add((root, RS.accessionNumber, Literal(filing.accession)))
  if filing.form:
    g.add((root, RS.form, Literal(filing.form)))
  if filing.filing_date:
    g.add(
      (root, RS.filingDate, Literal(filing.filing_date.isoformat(), datatype=XSD.date))
    )
  if filing.fiscal_year_focus:
    g.add((root, RS.fiscalYearFocus, Literal(filing.fiscal_year_focus)))
  if filing.fiscal_period_focus:
    g.add((root, RS.fiscalPeriodFocus, Literal(filing.fiscal_period_focus)))
  if filing.fiscal_year_end_month:
    g.add((root, RS.fiscalYearEndMonth, Literal(filing.fiscal_year_end_month)))
  # The period the report covers, as EDGAR records it — distinct from the date
  # it was filed, and the field every consumer sorts a filer's reports by.
  if filing.report_date:
    g.add(
      (
        root,
        RS.periodEndDate,
        Literal(filing.report_date.isoformat(), datatype=XSD.date),
      )
    )

  g.add((entity_node, RDF.type, RS.Entity))
  name = model.entity.name or model.entity.cik
  g.add((entity_node, SKOS.prefLabel, Literal(name)))
  g.add((entity_node, RS.internalId, Literal(model.entity.cik)))
  g.add((entity_node, RS.scheme, URIRef(model.entity.scheme)))
  if model.entity.legal_name:
    g.add((entity_node, RS.legalName, Literal(model.entity.legal_name)))
  if model.entity.ein:
    g.add((entity_node, RS.ein, Literal(model.entity.ein)))


# ── Elements (rs:Element per concept — full DTS coverage) ───────────────────


def _add_elements(g: Graph, model: XbrlModel, ns: Mapping[str, str]) -> None:
  for qname, concept in sorted(model.concepts.items()):
    uri = _concept_uri(qname, ns)
    g.add((uri, RDF.type, RS.Element))
    if concept.balance:
      g.add((uri, XBRLI.balance, Literal(concept.balance)))
    if concept.period_type:
      g.add((uri, XBRLI.periodType, Literal(concept.period_type)))
    is_monetary = (concept.item_type or "").startswith("monetary")
    g.add((uri, RS.monetary, Literal(is_monetary, datatype=XSD.boolean)))
    g.add((uri, RS.abstract, Literal(concept.is_abstract, datatype=XSD.boolean)))
    g.add((uri, RS.elementType, Literal(_element_type(concept))))
    g.add((uri, RS.itemType, Literal(_item_type(concept))))
    # `itemType` is the value *domain* a renderer formats by, and several XBRL
    # types share one (`dei:yesNoItemType` is a `string`). The declared type is
    # what the filing actually says, so it is carried beside it rather than
    # instead of it.
    declared = concept.item_type_qname or concept.item_type
    if declared:
      g.add((uri, RS.dataType, Literal(declared)))
    if concept.base_xsd_type:
      g.add((uri, RS.baseType, Literal(concept.base_xsd_type)))
    if concept.nillable:
      g.add((uri, RS.nillable, Literal(True, datatype=XSD.boolean)))
    if concept.pref_label:
      g.add((uri, SKOS.prefLabel, Literal(concept.pref_label)))
    _add_labels(g, uri, concept)
    if concept.substitution_group:
      g.add((uri, RS.substitutionGroup, _concept_uri(concept.substitution_group, ns)))
    g.add((uri, RS.internalId, Literal(qname)))
    g.add((uri, RS.source, Literal(_source_of(qname))))


def _add_labels(g: Graph, uri: URIRef, concept: Concept) -> None:
  """Every label the concept carries, one predicate per label role.

  ``skos:prefLabel`` alone says what to call an element and nothing about how a
  filer chose to call it in one place — the negated, total and period-start
  forms a statement renders under, and the documentation the taxonomy defines
  it with. Those are one triple each rather than a node each: a label is a
  literal with a role, and reifying it would add a node per label (a third
  again as many nodes as the whole report) to say the same thing.

  A role the vocabulary does not name falls back to ``skos:altLabel``, which
  keeps the text and loses only which kind of alternative it was.
  """
  for label in concept.labels:
    if label.value is None:
      continue
    # `skos:prefLabel` stays a plain string — it is what every consumer of a
    # holon reads an element's name from — and the standard-role label is
    # written beside it as well, because that one carries a language and the
    # preferred label has nowhere to put it.
    term = LABEL_ROLE_TERMS.get(label.role or "")
    if label.role == DOCUMENTATION_LABEL_ROLE:
      predicate = RDFS.comment
    elif term:
      predicate = RS[term]
    else:
      predicate = SKOS.altLabel
    literal = (
      Literal(label.value, lang=label.language)
      if label.language
      else Literal(label.value)
    )
    g.add((uri, predicate, literal))


def _element_type(concept: Concept) -> str:
  if concept.is_hypercube_item:
    return "hypercube"
  if concept.is_dimension_item:
    return "axis"
  if concept.is_domain_member:
    return "member"
  if concept.is_abstract:
    return "abstract"
  return "concept"


def _item_type(concept: Concept) -> str:
  """The element's value domain (``rs:itemType``) — orthogonal to elementType.

  elementType is the *structural* role (concept/abstract/axis/member/hypercube);
  itemType is what kind of *value* the element's facts carry, so a consumer knows
  a fact is a rendered HTML disclosure (``textBlock``) vs a number vs a date/flag.
  Derived from Arelle's derivation-aware flags (robust to custom subtypes), it
  matches the value-domain vocabulary the platform's planned ``Element.itemType``
  will use (see specs/parking lot/nonnumeric-facts.md §5).
  """
  if concept.is_textblock:
    return "textBlock"
  raw = (concept.item_type or "").lower()
  if concept.is_numeric:
    # Distinguish the numeric domains a renderer must format/scale differently —
    # per-share and share counts never rescale by the statement factor, percent
    # scales to %, etc. Mirrors the SEC adapter's deriveNumericKind.
    if raw.startswith("monetary"):
      return "monetary"
    if "pershare" in raw:
      return "perShare"
    if concept.is_shares or "shares" in raw:
      return "shares"
    if "percent" in raw:
      return "percent"
    if "pure" in raw:
      return "pure"
    if concept.is_integer or "integer" in raw:
      return "integer"
    return "decimal"
  if "date" in raw:
    return "date"
  if "boolean" in raw:
    return "boolean"
  return "string"


def _source_of(qname: str) -> str:
  return qname.split(":", 1)[0] if ":" in qname else "unknown"


# ── Periods + units ─────────────────────────────────────────────────────────


def _add_periods(g: Graph, model: XbrlModel, root: URIRef) -> None:
  for period in model.periods:
    uri = _scoped(root, "period", period.id)
    g.add((uri, RDF.type, RS.Period))
    g.add((uri, XBRLI.periodType, Literal(period.period_type)))
    if period.period_type == "instant" and period.end is not None:
      g.add((uri, XBRLI.instant, Literal(period.end.isoformat(), datatype=XSD.date)))
    elif period.period_type == "duration":
      start = period.start or period.end
      if start is not None:
        g.add((uri, XBRLI.startDate, Literal(start.isoformat(), datatype=XSD.date)))
      if period.end is not None:
        g.add((uri, XBRLI.endDate, Literal(period.end.isoformat(), datatype=XSD.date)))
    if period.duration_type:
      g.add((uri, RS.durationType, Literal(period.duration_type)))
    if period.calendar_year is not None:
      g.add((uri, RS.calendarYear, Literal(period.calendar_year, datatype=XSD.integer)))
    if period.calendar_quarter:
      g.add((uri, RS.calendarQuarter, Literal(period.calendar_quarter)))
    if period.calendar_period_key:
      g.add((uri, RS.calendarPeriodKey, Literal(period.calendar_period_key)))


def _add_units(g: Graph, model: XbrlModel, root: URIRef, ns: Mapping[str, str]) -> None:
  for unit in model.units:
    uri = _scoped(root, "unit", unit.id)
    g.add((uri, RDF.type, RS.Unit))
    g.add((uri, XBRLI.measure, _measure_uri(unit.measure, ns)))


# ── Structures (rs:Structure + reified rs:Association for every network) ─────


def _plan_structures(model: XbrlModel) -> dict[str, _Structure]:
  """Group networks by extended-link role into one Structure each."""
  structs: dict[str, _Structure] = {}
  for net in model.networks:
    role = net.role_uri
    st = structs.get(role)
    if st is None:
      st = _Structure(role_uri=role, slug=_slug(role), name=net.definition or role)
      structs[role] = st
    # A presentation network's role definition is the section's display name.
    if net.kind == "presentation":
      st.presentation.append(net)
      if net.definition:
        st.name = net.definition
      for arc in net.arcs:
        st.pres_concepts.add(arc.from_qname)
        st.pres_concepts.add(arc.to_qname)
    elif net.kind == "calculation":
      st.calculation.append(net)
    else:
      st.definition.append(net)

  # Section order: rank structures by their role-definition number sorted as a
  # *string* (matching the SEC adapter's `ORDER BY number`), so 6-digit ecd
  # governance roles don't sort ahead of the 7-digit filer statements.
  ranked = sorted(structs.values(), key=lambda s: _order_key(s.name))
  for rank, st in enumerate(ranked):
    st.order = rank
  return structs


def _preferred_label_text(model: XbrlModel, qname: str, role: str) -> str | None:
  """The concept's label under the arc's preferred-label role (English first).

  The filer chooses per presentation arc which label variant renders — terse,
  total, negated, … — so the string must come from the label linkbase entry for
  that exact role, not the concept's standard label.
  """
  concept = model.concepts.get(qname)
  if concept is None:
    return None
  fallback: str | None = None
  for label in concept.labels:
    if label.role != role or not label.value:
      continue
    if label.language is None or label.language.startswith("en"):
      return label.value
    fallback = fallback or label.value
  return fallback


def _add_structures(
  g: Graph,
  structures: dict[str, _Structure],
  root: URIRef,
  model: XbrlModel,
  ns: Mapping[str, str],
) -> None:
  for st in structures.values():
    s_uri = _scoped(root, "structure", st.slug)
    g.add((s_uri, RDF.type, RS.Structure))
    # Structural arrangement only (RollUp when calc arcs, else Hierarchy) — no
    # equity RollForward special-case, since that needs semantic typing.
    g.add((s_uri, RDF.type, _structure_arrangement(st.has_calc, None)))
    g.add((s_uri, RS.internalId, Literal(st.role_uri)))
    g.add((s_uri, RS.roleUri, Literal(st.role_uri)))
    g.add((s_uri, RS.structureName, Literal(st.name)))
    g.add((s_uri, SKOS.prefLabel, Literal(st.name)))
    if st.order is not None:
      g.add((s_uri, RS.structureOrder, Literal(st.order, datatype=XSD.integer)))
    if st.renderable:
      g.add((s_uri, RS.factSet, _factset_uri(st.role_uri)))

    groups = (
      ("presentation", st.presentation),
      ("calculation", st.calculation),
      ("definition", st.definition),
    )
    for kind, nets in groups:
      idx = 0
      for net in nets:
        for arc in net.arcs:
          a_uri = _scoped(root, f"association/{st.slug}/{kind}", str(idx))
          idx += 1
          g.add((s_uri, RS.hasAssociation, a_uri))
          g.add((a_uri, RDF.type, RS.Association))
          if kind == "calculation":
            g.add((a_uri, RDF.type, RS.RollUpRelationship))
          g.add((a_uri, XLINK["from"], _concept_uri(arc.from_qname, ns)))
          g.add((a_uri, XLINK.to, _concept_uri(arc.to_qname, ns)))
          g.add((a_uri, RS.associationType, Literal(kind)))
          # The filer's per-arc label choice: the role URI (negated* roles carry
          # the display-sign semantic) plus the resolved label string, so a
          # consumer needs no label-linkbase lookup of its own.
          if kind == "presentation" and arc.preferred_label:
            g.add((a_uri, RS.preferredLabelRole, Literal(arc.preferred_label)))
            text = _preferred_label_text(model, arc.to_qname, arc.preferred_label)
            if text:
              g.add((a_uri, RS.preferredLabel, Literal(text)))
          if arc.arcrole:
            g.add((a_uri, XLINK.arcrole, _arcrole_uri(arc.arcrole)))
          g.add((a_uri, XLINK.role, URIRef(st.role_uri)))
          if arc.order is not None:
            g.add(
              (
                a_uri,
                LINK.order,
                Literal(Decimal(str(arc.order)), datatype=XSD.decimal),
              )
            )
          if arc.weight is not None:
            g.add(
              (
                a_uri,
                LINK.weight,
                Literal(Decimal(str(arc.weight)), datatype=XSD.decimal),
              )
            )


# ── Dimensions (rs:Dimension nodes, deduped by axis + member/typed value) ────


def _dim_key(axis: str, member: str | None, typed: str | None) -> str:
  return f"{axis}|{member or ''}|{typed or ''}"


def _add_dimensions(
  g: Graph, model: XbrlModel, root: URIRef, ns: Mapping[str, str]
) -> dict[str, URIRef]:
  """Emit one rs:Dimension per unique (axis, member/typed) across all facts."""
  uris: dict[str, URIRef] = {}
  for fact in model.facts:
    for dim in fact.dims:
      key = _dim_key(dim.axis_qname, dim.member_qname, dim.typed_value)
      if key in uris:
        continue
      d_uri = _scoped(root, "dimension", _slug(key))
      uris[key] = d_uri
      g.add((d_uri, RDF.type, RS.Dimension))
      g.add((d_uri, RS.axis, _concept_uri(dim.axis_qname, ns)))
      g.add((d_uri, RS.isExplicit, Literal(dim.is_explicit, datatype=XSD.boolean)))
      g.add((d_uri, RS.isTyped, Literal(not dim.is_explicit, datatype=XSD.boolean)))
      if dim.member_qname:
        g.add((d_uri, RS.member, _concept_uri(dim.member_qname, ns)))
      if dim.typed_value is not None:
        g.add((d_uri, RS.typedValue, Literal(dim.typed_value)))
      if dim.axis_type:
        g.add((d_uri, RS.axisType, Literal(dim.axis_type)))
  return uris


# ── Facts (rs:Fact — numeric or non-numeric, with dimensions + factSets) ─────


def _fact_membership(
  model: XbrlModel, structures: dict[str, _Structure]
) -> dict[str, set[str]]:
  """Map each fact id → the role_uris of structures whose presentation cites it."""
  by_concept: dict[str, list[str]] = {}
  for st in structures.values():
    if not st.renderable:
      continue
    for concept in st.pres_concepts:
      by_concept.setdefault(concept, []).append(st.role_uri)
  membership: dict[str, set[str]] = {}
  for fact in model.facts:
    roles = by_concept.get(fact.concept_qname)
    if roles:
      membership[fact.id] = set(roles)
  return membership


def _add_facts(
  g: Graph,
  model: XbrlModel,
  root: URIRef,
  entity_node: URIRef,
  dim_uris: dict[str, URIRef],
  membership: dict[str, set[str]],
  ns: Mapping[str, str],
) -> None:
  for fact in model.facts:
    uri = _scoped(root, "fact", fact.id)
    g.add((uri, RDF.type, RS.Fact))
    g.add((uri, RS.element, _concept_uri(fact.concept_qname, ns)))
    g.add((uri, RS.entity, entity_node))
    g.add((uri, RS.period, _scoped(root, "period", fact.period_id)))
    if fact.unit_id is not None:
      g.add((uri, RS.unit, _scoped(root, "unit", fact.unit_id)))

    if fact.value_kind == "numeric" and fact.numeric_value is not None:
      # The value as the filing wrote it, not the float's repr: `0.0500` is a
      # rate stated to four places and `Decimal(str(0.05))` is not the same
      # statement. Both are valid xsd:decimal, so nothing downstream has to
      # change to read it, and the lexical form is what every other projection
      # of this model carries.
      lexical = fact.value_str or str(Decimal(str(fact.numeric_value)))
      g.add((uri, RS.numericValue, Literal(lexical, datatype=XSD.decimal)))
      g.add((uri, RS.factType, Literal("numeric")))
      if fact.decimals is not None:
        g.add((uri, RS.decimals, Literal(fact.decimals)))
    else:
      if fact.value_str is not None:
        g.add((uri, RS.stringValue, Literal(fact.value_str)))
      g.add((uri, RS.factType, Literal("nonnumeric")))

    # xsi:nil — reported as *not disclosed*, which an empty value alone cannot
    # say. Written only when true, so its absence is the ordinary case.
    if fact.is_nil:
      g.add((uri, RS.isNil, Literal(True, datatype=XSD.boolean)))
    # XBRL carries a language on non-numeric facts and every other projection
    # of this model writes it; the holon was dropping it.
    if fact.language:
      g.add((uri, RS.language, Literal(fact.language)))

    g.add((uri, RS.internalId, Literal(fact.id)))

    for dim in fact.dims:
      key = _dim_key(dim.axis_qname, dim.member_qname, dim.typed_value)
      d_uri = dim_uris.get(key)
      if d_uri is not None:
        g.add((uri, RS.dimension, d_uri))

    for role in membership.get(fact.id, ()):  # a fact may sit in several sections
      g.add((uri, RS.factSet, _factset_uri(role)))


# ── Information Blocks (one per renderable structure with member facts) ──────


def _add_information_blocks(
  g: Graph,
  structures: dict[str, _Structure],
  membership: dict[str, set[str]],
  root: URIRef,
) -> None:
  used_roles: set[str] = set()
  for roles in membership.values():
    used_roles |= roles
  for st in structures.values():
    if not st.renderable or st.role_uri not in used_roles:
      continue
    ib_uri = _scoped(root, "ib", st.slug)
    g.add((ib_uri, RDF.type, RS.InformationBlock))
    g.add((ib_uri, RS.internalId, Literal(st.slug)))
    g.add((ib_uri, SKOS.prefLabel, Literal(st.name)))
    # Link the block to its structure so the renderer orders/matches by identity
    # (structure), not a semantic type; the shared factSet groups the facts.
    g.add((ib_uri, RS.structure, _scoped(root, "structure", st.slug)))
    g.add((ib_uri, RS.factSet, _factset_uri(st.role_uri)))


__all__ = ["build_holon_graph", "holon_root"]
