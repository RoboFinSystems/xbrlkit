"""Project a neutral ``XbrlModel`` into an xBRL-JSON document (OIM).

xBRL-JSON is the OIM report serialization (XBRL International, REC 2021-10-13).
Unlike the holon and Tavi projections, this one has a **reference
implementation**: Arelle's ``saveLoadableOIM`` writes the same document from the
same filing, which makes this the only projection whose output can be checked
against something other than our own reading of a spec.

That is the reason it exists. Arelle already emits xBRL-JSON, so a second writer
is redundant as a *feature* — its value is as a test of the layer underneath.
Every difference between this output and Arelle's is a fidelity bug in the parse
or in :class:`~xbrlkit.model.XbrlModel`, and those same bugs are present, silent
and unverifiable, in the holon and Tavi projections. Writing fact language is
how the first one was found.

The period, entity and language literals are shared with the Tavi projection
(see :mod:`._values`): a period is an interval of dateTimes on the exclusive
end — an instant at the close of 2024-12-31 is ``2025-01-01T00:00:00`` — the
entity is ``cik:0000066740``, and a language tag is lower case. One convention
is this projection's own: **values are canonical**, as ``xbrl:canonicalValues``
declares, so a decimal-typed integer keeps its ``.0`` where Tavi carries the
lexical value as reported.
"""

from __future__ import annotations

import json

from ..model import Concept, XbrlModel
from ._values import (
  entity_prefix,
  entity_sqname,
  language_tag,
  period_interval,
)

OIM_DOCUMENT_TYPE = "https://xbrl.org/2021/xbrl-json"

# Namespaces every document needs regardless of the filing's own taxonomies.
OIM_RESERVED_NAMESPACES: dict[str, str] = {
  "xbrl": "https://xbrl.org/2021",
  "xbrli": "http://www.xbrl.org/2003/instance",
  "iso4217": "http://www.xbrl.org/2003/iso4217",
  "utr": "http://www.xbrl.org/2009/utr",
}

# The entity SQName and its scheme binding, the period literal and the
# language form are shared with the Tavi projection — see ``_values``.

# Unit measures that mean "no unit" and are therefore left off the fact.
PURE_MEASURES = frozenset({"xbrli:pure", "pure"})


def to_oim(model: XbrlModel, *, report_id: str | None = None) -> str:
  """Project ``model`` into an xBRL-JSON document string."""
  return json.dumps(to_oim_document(model, report_id=report_id), indent=2, default=str)


def to_oim_document(
  model: XbrlModel, *, report_id: str | None = None
) -> dict[str, object]:
  """Project ``model`` into an xBRL-JSON document."""
  _ = report_id
  return {
    "documentInfo": {
      "documentType": OIM_DOCUMENT_TYPE,
      "features": {"xbrl:canonicalValues": True},
      "namespaces": _namespaces(model),
    },
    "facts": _facts(model, _namespaces(model)),
  }


def _namespaces(model: XbrlModel) -> dict[str, str]:
  """Prefix map: the reserved set, the entity scheme, and the filing's own."""
  namespaces = dict(OIM_RESERVED_NAMESPACES)
  prefix, scheme = entity_prefix(model.entity)
  namespaces[prefix] = scheme

  by_uri = {uri: prefix for prefix, uri in namespaces.items()}
  for concept in model.concepts.values():
    uri = concept.namespace
    if not uri or uri in by_uri:
      continue
    prefix = concept.qname.split(":", 1)[0] if ":" in concept.qname else None
    if not prefix or prefix in namespaces:
      prefix = f"ns{len(namespaces)}"
    namespaces[prefix] = uri
    by_uri[uri] = prefix
  return dict(sorted(namespaces.items()))


def _facts(model: XbrlModel, namespaces: dict[str, str]) -> dict[str, object]:
  """The facts object, keyed by a stable per-document fact id."""
  prefixes = {uri: prefix for prefix, uri in namespaces.items()}
  periods = {period.id: period for period in model.periods}
  units = {unit.id: unit for unit in model.units}
  entity = entity_sqname(model.entity)
  facts: dict[str, object] = {}
  concepts = model.concepts

  for index, fact in enumerate(model.facts):
    dimensions: dict[str, str] = {"concept": fact.concept_qname}
    if fact.language and _takes_language(concepts.get(fact.concept_qname)):
      # xBRL-JSON requires the lower-case form (xbrlje:invalidLanguageCodeCase).
      # Arelle's own writer emits the filing's mixed case, which its loader
      # then rejects; this is the one place this projection departs from it.
      dimensions["language"] = language_tag(fact.language)
    dimensions["entity"] = entity
    period = periods.get(fact.period_id)
    if period is not None:
      dimensions["period"] = period_interval(period)
    if fact.unit_id and fact.unit_id in units:
      measure = units[fact.unit_id].measure
      # A pure unit is equivalent to no unit, and OIM omits the dimension
      # rather than writing it (Tavi says the same in section 8.5.2.3).
      if measure not in PURE_MEASURES:
        dimensions["unit"] = measure
    for qualifier in fact.dims:
      value = qualifier.member_qname or qualifier.typed_value
      if value is not None:
        dimensions[qualifier.axis_qname] = value

    entry: dict[str, object] = {
      "value": _value(fact, concepts.get(fact.concept_qname), prefixes)
    }
    decimals = _decimals(fact.decimals)
    if decimals is not None:
      entry["decimals"] = decimals
    entry["dimensions"] = dimensions
    facts[f"f-{index}"] = entry
  return facts


def _takes_language(concept: Concept | None) -> bool:
  """Whether the language core dimension applies to facts of this concept.

  It applies to OIM text facts only. Neither the item type nor the base XML
  Schema type decides this: centralIndexKeyItemType and enumerationSetItemType
  are both token-derived, and only the first takes a language. The parse
  resolves it against the DTS type chain (see ``Concept.is_text_fact``); Tavi
  draws the same line for its own text-fact definition (section 8.3).
  """
  return concept is not None and concept.is_text_fact


def _value(
  fact: object, concept: Concept | None, prefixes: dict[str, str]
) -> str | None:
  """The fact value as a string — xBRL-JSON carries numerics as strings too."""
  if getattr(fact, "is_nil", False):
    return None
  value_str = getattr(fact, "value_str", None)
  if value_str is None:
    return None
  if getattr(fact, "value_kind", None) == "numeric":
    numeric = getattr(fact, "numeric_value", None)
    if numeric is not None:
      return _canonical_number(numeric, value_str, concept)
  return _canonical_enumeration(value_str, prefixes)


def _canonical_number(numeric: float, original: str, concept: Concept | None) -> str:
  """Canonical decimal form, as ``xbrl:canonicalValues`` declares.

  A decimal-valued fact keeps a fractional part (``56100000000.0``) while an
  integer-typed one does not (``9``), so the concept's datatype decides the
  form rather than the value's shape. Non-integral values are left exactly as
  reported: re-formatting a decimal risks losing precision the filing intended.
  """
  if concept is not None and concept.is_integer:
    return original
  if "e" in original.lower():
    return original
  if numeric.is_integer():
    return f"{numeric:.1f}" if "." not in original else original.split(".")[0] + ".0"
  # Canonical form carries no insignificant trailing zeros: 0.0530 is 0.053.
  return original.rstrip("0").rstrip(".") if "." in original else original


def _canonical_enumeration(value: str, prefixes: dict[str, str]) -> str:
  """Rewrite an extensible-enumeration value from URI form to a QName.

  Enumeration facts carry their members as ``{namespace}#{localName}`` in the
  instance; OIM writes them as QNames against the document's prefix map. A
  value whose namespace is not in the map is left alone rather than guessed at.
  """
  if "#" not in value:
    return value
  namespace, _, local = value.rpartition("#")
  prefix = prefixes.get(namespace)
  return f"{prefix}:{local}" if prefix and local else value


def _decimals(value: str | None) -> int | None:
  """``decimals`` is an integer; INF means infinitely precise, and is omitted."""
  if value is None or value.upper() in ("INF", "INFINITY"):
    return None
  try:
    return int(value)
  except ValueError:
    return None
