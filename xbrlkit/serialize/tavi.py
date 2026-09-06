"""Project a neutral ``XbrlModel`` into a Project Tavi compiled model.

Tavi (XBRL International, PWD 2026-09-01 — previously "OIM Taxonomy") replaces
the XML taxonomy + instance pair with one JSON object model: taxonomy objects
and report objects live in a single document, and every object is a named,
QName-addressed, referenceable thing.

This is the third projection off the one parse (see :mod:`..model`): the holon
emits RDF, ``graph`` emits the LPG/parquet shape, and this emits Tavi. Nothing
upstream changes — the ``XbrlModel`` already carries what Tavi needs, because
Tavi's fact model (``factDimensions``: concept/period/unit/entity plus taxonomy
dimensions as peers) is the same shape the parse has always produced.

We emit a **compiled model** (``documentType`` ``…/compiled``): fully resolved,
no imports, self-contained — one file per filing, so a consumer needs nothing
else to read it.

The one genuine transformation is dimensionality. XBRL says it with arcroles
over ``<xs:element>``s — a hypercube is an element, an axis is an element, a
domain and its members are elements — while Tavi gives each its own object
type. :func:`_dimensional` reads the definition networks back into cube,
dimension, domain class, domain network and member objects, which is also why
those elements must then be kept *out* of ``concepts``: in Tavi they are no
longer concepts, and emitting them twice would collide on the name.

**Whatever Tavi has nowhere to put** is not hidden: :func:`to_tavi_report`
returns a :class:`GapReport` alongside the document, split into what the model
cannot express and what this emitter has not mapped yet, so neither is blamed
for the other. That report is the substantive output of the exercise.

Written against the prose of PWD-2026-09-01 and checked against the eight
example models published with the draft's demo. There is still no
``tavi-schema.json`` to validate against, so :data:`SPEC_AMBIGUITIES` records
where the draft is unclear or contradicts itself and what this emitter chose.

It was then diffed, object class by object class, against the compiled model
Arelle's ``XbrlModel`` plugin (Arelle PR #2418, unreleased) writes for the same
filing — the same exercise the OIM projection ran against ``saveLoadableOIM``.
Where the two disagreed and the draft decided it, this emitter was fixed
(period literals, the entity SQName, fact language, the pure and shares
datatypes, root sources, nillable, hypercube headings); where the draft did not
decide it, the disagreement is recorded below as an ambiguity.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field

from ..model import Arc, Concept, Network, Period, XbrlFact, XbrlModel
from ..namespaces import TAVI_REPORT_BASE
from ._values import (
  entity_prefix,
  entity_sqname,
  language_tag,
  period_interval,
)

TAVI_VERSION = "PWD-2026-09-01"
TAVI_BASE = "https://xbrl.org/PWD/2026-09-01"
DOCTYPE_COMPILED = f"{TAVI_BASE}/compiled"

# Reserved prefixes, per section 2.4. `xs` is bound to the http form used by
# every namespaces-map example in the draft, not the https form in the section
# 2.4 table — see SPEC_AMBIGUITIES.
RESERVED_NAMESPACES: dict[str, str] = {
  "xbrl": TAVI_BASE,
  "xbrlr": f"{TAVI_BASE}/report",
  "xbrla": f"{TAVI_BASE}/accounting",
  "xs": "http://www.w3.org/2001/XMLSchema",
  "iso4217": "http://www.xbrl.org/2003/iso4217",
  "utr": f"{TAVI_BASE}/utr",
}

# Namespace for the objects this emitter mints (facts, groups, the model
# object). Report-scoped so two filings never collide. See namespaces.py.
REPORT_NS_BASE = TAVI_REPORT_BASE
REPORT_PREFIX = "rpt"

# XBRL 2.1 item type -> Tavi datatype QName. Every target here was verified
# against the built-in model in Appendix E; an unverified item type is recorded
# as a gap rather than guessed, because a wrong datatype is a silent
# correctness bug that a validator would not catch.
ITEM_TYPE_DATATYPES: dict[str, str] = {
  "monetaryItemType": "xbrlr:monetary",
  # xbrlr:pure is the *unit* (Appendix E); the datatype it measures is pureType.
  "pureItemType": "xbrlr:pureType",
  # No core datatype types a share count. The accounting module the draft
  # reserves the xbrla prefix for defines one, per the copy Arelle ships — see
  # SPEC_AMBIGUITIES: shares-datatype-in-unpublished-module.
  "sharesItemType": "xbrla:sharesType",
  "percentItemType": "xbrlr:percent",
  "perShareItemType": "xbrlr:perShare",
  "textBlockItemType": "xbrlr:textBlock",
  "areaItemType": "xbrlr:area",
  "energyItemType": "xbrlr:energy",
  "flowItemType": "xbrlr:flow",
  "forceItemType": "xbrlr:force",
  "frequencyItemType": "xbrlr:frequency",
  "lengthItemType": "xbrlr:length",
  "massItemType": "xbrlr:mass",
  "memoryItemType": "xbrlr:memory",
  "planeAngleItemType": "xbrlr:planeAngle",
  "powerItemType": "xbrlr:power",
  "pressureItemType": "xbrlr:pressure",
  "speedItemType": "xbrlr:speed",
  "temperatureItemType": "xbrlr:temperature",
  "voltageItemType": "xbrlr:voltage",
  "volumeItemType": "xbrlr:volume",
  "durationItemType": "xbrlr:duration",
  "dateTimeItemType": "xbrlr:dateTime",
  "stringItemType": "xs:string",
  "normalizedStringItemType": "xs:normalizedString",
  "booleanItemType": "xs:boolean",
  "dateItemType": "xs:date",
  "gYearItemType": "xs:gYear",
  "anyURIItemType": "xs:anyURI",
  "integerItemType": "xs:integer",
  "decimalItemType": "xs:decimal",
  "QNameItemType": "xs:QName",
  # Extensible enumerations have a built-in datatype; the domain a concept
  # restricts its values to (enum2:domain) is not in the model yet.
  "enumerationItemType": "xbrlr:enumeration",
  "enumerationSetItemType": "xbrlr:enumeration",
}

# Item types with no built-in Tavi datatype at all — a gap in the model, not in
# this emitter. Kept separate so the gap report does not blame the spec for our
# own unmapped types. Empty since the share count moved to the accounting module.
ITEM_TYPES_WITHOUT_BUILTIN: frozenset[str] = frozenset()

# XML Schema simple types a custom datatype object may name as its baseType.
# Anything Arelle reports outside this set (anyType, the xbrli unions) falls
# back to xs:string, the base every text-derived item type shares.
XSD_SIMPLE_TYPES: frozenset[str] = frozenset(
  {
    "string", "boolean", "decimal", "float", "double", "duration", "dateTime",
    "time", "date", "gYearMonth", "gYear", "gMonthDay", "gDay", "gMonth",
    "hexBinary", "base64Binary", "anyURI", "QName", "NOTATION",
    "normalizedString", "token", "language", "NMTOKEN", "NMTOKENS", "Name",
    "NCName", "ID", "IDREF", "IDREFS", "ENTITY", "ENTITIES", "integer",
    "nonPositiveInteger", "negativeInteger", "long", "int", "short", "byte",
    "nonNegativeInteger", "unsignedLong", "unsignedInt", "unsignedShort",
    "unsignedByte", "positiveInteger", "yearMonthDuration", "dayTimeDuration",
    "dateTimeStamp",
  }
)  # fmt: skip

# Label role URI -> Tavi label type QName. Section 14.6 keeps the XBRL 2.1 and
# Link Role Registry roles and addresses them by QName instead of URI; this map
# is transcribed from the core model's own labelTypes, using the *prose* URIs
# for the two entries where the core model contradicts itself (see
# SPEC_AMBIGUITIES: duplicate-label-uris).
LABEL_ROLE_TYPES: dict[str, str] = {
  "http://www.xbrl.org/2003/role/label": "xbrl:label",
  "http://www.xbrl.org/2003/role/terseLabel": "xbrl:terseLabel",
  "http://www.xbrl.org/2003/role/verboseLabel": "xbrl:verboseLabel",
  "http://www.xbrl.org/2003/role/totalLabel": "xbrl:totalLabel",
  "http://www.xbrl.org/2003/role/periodStartLabel": "xbrl:periodStartLabel",
  "http://www.xbrl.org/2003/role/periodEndLabel": "xbrl:periodEndLabel",
  "http://www.xbrl.org/2003/role/documentation": "xbrl:documentation",
  "http://www.xbrl.org/2003/role/negativeLabel": "xbrl:negativeLabel",
  "http://www.xbrl.org/2003/role/negativeTerseLabel": "xbrl:negativeTerseLabel",
  "http://www.xbrl.org/2003/role/negativeVerboseLabel": "xbrl:negativeVerboseLabel",
  "http://www.xbrl.org/2003/role/positiveLabel": "xbrl:positiveLabel",
  "http://www.xbrl.org/2003/role/positiveTerseLabel": "xbrl:positiveTerseLabel",
  "http://www.xbrl.org/2003/role/positiveVerboseLabel": "xbrl:positiveVerboseLabel",
  "http://www.xbrl.org/2003/role/zeroLabel": "xbrl:zeroLabel",
  "http://www.xbrl.org/2003/role/zeroTerseLabel": "xbrl:zeroTerseLabel",
  "http://www.xbrl.org/2003/role/zeroVerboseLabel": "xbrl:zeroVerboseLabel",
  "http://www.xbrl.org/2006/role/restatedLabel": "xbrl:restatedLabel",
  "http://www.xbrl.org/2009/role/negatedLabel": "xbrl:negatedLabel",
  "http://www.xbrl.org/2009/role/negatedTerseLabel": "xbrl:negatedTerseLabel",
  "http://www.xbrl.org/2009/role/negatedTotalLabel": "xbrl:negatedTotalLabel",
  "http://www.xbrl.org/2009/role/negatedNetLabel": "xbrl:negatedNetLabel",
  "http://www.xbrl.org/2009/role/negatedPeriodEndLabel": "xbrl:negatedPeriodEndLabel",
  "http://www.xbrl.org/2009/role/negatedPeriodStartLabel": "xbrl:negatedPeriodStartLabel",
  "http://www.xbrl.org/2009/role/netLabel": "xbrl:netLabel",
  "http://www.xbrl.org/2009/role/deprecatedLabel": "xbrl:deprecatedLabel",
  "http://www.xbrl.org/2009/role/deprecatedDateLabel": "xbrl:deprecatedDateLabel",
  "http://www.xbrl.org/2009/role/negativePeriodEndLabel": "xbrl:negativePeriodEndLabel",
  "http://www.xbrl.org/2009/role/negativePeriodEndTotalLabel": (
    "xbrl:negativePeriodEndTotalLabel"
  ),
  "http://www.xbrl.org/2009/role/negativePeriodStartLabel": (
    "xbrl:negativePeriodStartLabel"
  ),
  "http://www.xbrl.org/2009/role/negativePeriodStartTotalLabel": (
    "xbrl:negativePeriodStartTotalLabel"
  ),
  "http://www.xbrl.org/2009/role/positivePeriodEndLabel": "xbrl:positivePeriodEndLabel",
  "http://www.xbrl.org/2009/role/positivePeriodEndTotalLabel": (
    "xbrl:positivePeriodEndTotalLabel"
  ),
  "http://www.xbrl.org/2009/role/positivePeriodStartLabel": (
    "xbrl:positivePeriodStartLabel"
  ),
  "http://www.xbrl.org/2009/role/positivePeriodStartTotalLabel": (
    "xbrl:positivePeriodStartTotalLabel"
  ),
  "http://xbrl.us/us-gaap/role/label/negated": "xbrl:negated",
  "http://xbrl.us/us-gaap/role/label/negatedPeriodEnd": "xbrl:negatedPeriodEnd",
  "http://xbrl.us/us-gaap/role/label/negatedPeriodStart": "xbrl:negatedPeriodStart",
  "http://xbrl.us/us-gaap/role/label/negatedTotal": "xbrl:negatedTotal",
}
DEFAULT_LABEL_TYPE = "xbrl:label"

PRESENTATION_RELATIONSHIP = "xbrl:parent-child"
CALCULATION_RELATIONSHIP = "xbrl:summation-item"
# Section 10.4: the virtual origin a network root is anchored to. Not rendered;
# it exists so several roots can be declared and ordered.
ROOT_SOURCE = "xbrl:rootSource"

# Points where PWD-2026-09-01 is unclear or contradicts itself, and the reading
# this emitter took. Carried in the output so a reviewer sees the assumptions
# rather than having to infer them from the bytes.
SPEC_AMBIGUITIES: tuple[dict[str, str], ...] = (
  {
    "id": "xs-namespace-scheme",
    "where": "section 2.4 reserved prefixes vs. every namespaces-map example",
    "issue": (
      "The reserved-prefix table binds xs to https://www.w3.org/2001/XMLSchema; "
      "all six namespaces-map examples and the built-in model bind it to the "
      "http form. Section 4.2.2 requires a reserved alias to carry exactly its "
      "prescribed URI (oimce:invalidURIForReservedAlias), so the examples are "
      "invalid against the table. Arelle's XbrlModel plugin has the same split: "
      "https in its reserved-prefix constants, http in every resource it ships "
      "and in the documents it writes."
    ),
    "choice": "http form — it is XML Schema's real namespace and the examples agree.",
  },
  {
    "id": "shares-datatype-in-unpublished-module",
    "where": "built-in datatypes (Appendix E) vs. section 2.4's xbrla prefix",
    "issue": (
      "No core datatype types a share count, which every equity filing reports; "
      "xbrlr:perShare exists but nothing it divides by. Section 2.4 reserves "
      "xbrla for an accounting module and an example imports "
      "../spec-taxonomies/xbrla.json, but the draft does not publish that "
      "module. The copy Arelle ships defines xbrla:sharesType, a unit "
      "xbrla:shares, and xbrla:MonetaryPerShare."
    ),
    "choice": (
      "sharesItemType maps to xbrla:sharesType and the shares unit to "
      "xbrla:shares, on the strength of the reference implementation's copy; "
      "perShareItemType keeps the built-in xbrlr:perShare."
    ),
  },
  {
    "id": "xbrlr-decimal-undefined",
    "where": "section 8.2.1 unit object examples",
    "issue": (
      "The unit examples use dataType xbrlr:decimal, which is not defined in "
      "the built-in model and is not an XML Schema built-in, so those examples "
      "raise oimte:invalidQNameReference against the unit object's own "
      "constraint. Arelle's types module does not define it either."
    ),
    "choice": (
      "a unit's dataType is the datatype of what it measures — xbrlr:monetary "
      "for a currency, xbrla:sharesType for shares — and xs:decimal otherwise."
    ),
  },
  {
    "id": "duplicate-label-uris",
    "where": "core model label types (Appendix E)",
    "issue": (
      "Two URIs are each bound to two label types, which the model's own "
      "oimte:duplicateLabelURI forbids: "
      ".../2009/role/negativePeriodEndTotalLabel carries both "
      "xbrl:negativePeriodEndLabel and xbrl:negativePeriodEndTotalLabel, and "
      ".../2009/role/positivePeriodStartTotalLabel carries both "
      "xbrl:positivePeriodEndTotalLabel and xbrl:positivePeriodStartTotalLabel. "
      "The consequence is that .../negativePeriodEndLabel and "
      ".../positivePeriodEndTotalLabel have no binding at all, so a filing "
      "using either Link Role Registry role has nothing to map to. The prose "
      "table in section 14.6 gives each type its matching URI, so the core "
      "model is the copy that is wrong. The core.json Arelle ships carries the "
      "same two duplicates."
    ),
    "choice": "the prose URIs are used, which restores a 1:1 role/type mapping.",
  },
  {
    "id": "domain-vs-domainNetwork",
    "where": "Appendix B vs. section 5.10.1",
    "issue": (
      "Appendix B names the cube dimension's domain property `domain`; section "
      "5.10.1, all twenty uses across the eight example models, and Arelle's "
      "converter name it `domainNetwork`."
    ),
    "choice": "domainNetwork.",
  },
  {
    "id": "reconciliation-required-but-unused",
    "where": "section 14.3.2 vs. section 14.3.4 and the published example models",
    "issue": (
      "Section 14.3.2 states that xbrl:reconciliation is required on "
      "xbrl:summation-item relationships, while section 14.3.4 defines it as a "
      "boolean whose true value marks a relationship as a reconciliation exempt "
      "from the weight/balance rule (and calls it xbrla:reconciliation there). "
      "A required boolean of that meaning forces every converter to assert "
      "something false on every ordinary relationship. None of the eight "
      "example models carries it, though they use xbrl:weight on those same "
      "relationships 42 times, and Arelle's core model lists it as allowed, "
      "not required."
    ),
    "choice": (
      "not emitted: an XBRL 2.1 calculation arc carries no reconciliation flag, "
      "so there is nothing true to write."
    ),
  },
  {
    "id": "period-literal-form",
    "where": "section 8.5.2.2 vs. xbrlr:periodString (Appendix E) vs. the examples",
    "issue": (
      "Section 8.5.2.2 calls xbrl:period an ISO 8601 interval and says no more. "
      "The built-in xbrlr:periodString requires xs:dateTime values in canonical "
      "form and states that the time component cannot be omitted "
      "(oimce:invalidPeriodRepresentation). The examples write twelve instants "
      "as bare dates and durations in both forms."
    ),
    "choice": (
      "the datatype's form — dateTime with an exclusive end, identical to "
      "xBRL-JSON and to Arelle's converter, so a fact's period literal is the "
      "same in both projections of one filing."
    ),
  },
  {
    "id": "language-case",
    "where": "section 5.7 (xbrl:languageDomain) vs. xBRL-JSON's language rule",
    "issue": (
      "The language domain's example value is fr-CA. xBRL-JSON requires the "
      "lower-case form (xbrlje:invalidLanguageCodeCase), and Arelle applies "
      "that rule to a Tavi fact — so its own converter flags its own en-US "
      "output. BCP 47 tags are case-insensitive, so either form names the "
      "same language."
    ),
    "choice": (
      "lower case on facts, as xBRL-JSON; label languages keep the filing's "
      "literal, as the examples' labels do."
    ),
  },
  {
    "id": "fact-value-literal-type",
    "where": "section 8.4.1 vs. the published example models",
    "issue": (
      "The fact value's `value` is typed xs:anyType. The examples write 244 "
      "numeric values as JSON strings and 42 as JSON numbers; Arelle's "
      "converter writes strings."
    ),
    "choice": (
      "strings — the reported lexical value, which is also what xBRL-JSON "
      "mandates and what keeps a large or precise decimal exact."
    ),
  },
)


@dataclass
class GapReport:
  """What the filing carries that the Tavi model has nowhere to put.

  The substantive output of the exercise: each entry is a concrete thing a real
  SEC filing expresses and PWD-2026-09-01 cannot, discovered by emitting rather
  than by reading.
  """

  # Item types Tavi has no built-in datatype for. A finding against the model.
  item_types_without_builtin: dict[str, int] = field(default_factory=dict)
  # Item types this emitter has not mapped yet. A finding against us — most are
  # taxonomy-defined (dei:*) and belong in a taxonomy, not the built-in model.
  item_types_unmapped_here: dict[str, int] = field(default_factory=dict)
  # Item types with no built-in mapping that were carried as datatype objects
  # (name + baseType) so the concept keeps its real type. Not a gap: a record.
  custom_datatypes: dict[str, int] = field(default_factory=dict)
  unmapped_label_roles: dict[str, int] = field(default_factory=dict)
  dropped_period_semantics: list[dict[str, object]] = field(default_factory=list)
  facts_without_cube: int = 0
  dimensional_facts: int = 0
  notes: list[str] = field(default_factory=list)

  def to_dict(self) -> dict[str, object]:
    return {
      "spec_version": TAVI_VERSION,
      "against_the_model": {
        "item_types_without_builtin": dict(
          sorted(self.item_types_without_builtin.items())
        ),
        "dropped_period_semantics": self.dropped_period_semantics,
        "spec_ambiguities": [dict(a) for a in SPEC_AMBIGUITIES],
      },
      "against_this_emitter": {
        "item_types_unmapped_here": dict(sorted(self.item_types_unmapped_here.items())),
        "custom_datatypes": dict(sorted(self.custom_datatypes.items())),
        "unmapped_label_roles": dict(sorted(self.unmapped_label_roles.items())),
        "facts_without_cube": self.facts_without_cube,
        "dimensional_facts": self.dimensional_facts,
      },
      "notes": self.notes,
    }


def to_tavi(
  model: XbrlModel, *, report_id: str | None = None, description: str | None = None
) -> str:
  """Project ``model`` into a Tavi compiled-model JSON string."""
  document, _ = to_tavi_report(model, report_id=report_id, description=description)
  return json.dumps(document, indent=2, sort_keys=False, default=str)


def to_tavi_report(
  model: XbrlModel, *, report_id: str | None = None, description: str | None = None
) -> tuple[dict[str, object], GapReport]:
  """Project ``model``, returning the document and what it could not express.

  ``report_id`` scopes the report's own namespace (the accession by default);
  ``description`` replaces the ``documentInfo`` sentence, which otherwise
  reads the model as an EDGAR filing.
  """
  report_id = report_id or model.filing.accession
  gaps = GapReport()
  namespaces = _namespaces(model, report_id)
  prefix_for_uri = {uri: prefix for prefix, uri in namespaces.items()}
  default_language = _default_language(model)

  dimensional = _dimensional(model)
  concepts, headings, datatypes = _concepts_and_headings(
    model, gaps, dimensional, prefix_for_uri
  )
  networks, groups, group_contents, group_labels = _networks_and_groups(
    model, dimensional, default_language
  )

  xbrl_model: dict[str, object] = {
    "name": f"{REPORT_PREFIX}:Report",
    "modelType": "xbrl:report",
    "properties": _model_properties(model),
    "entities": _entities(model),
    "units": _units(model),
    "dataTypes": datatypes,
    "concepts": concepts,
    "headings": headings,
    "dimensions": dimensional.dimensions,
    "domainClasses": dimensional.domain_classes,
    "domainNetworks": dimensional.domain_networks,
    "members": dimensional.members,
    "cubes": dimensional.cubes,
    "labels": _labels(model, gaps, default_language)
    + _entity_labels(model, default_language)
    + group_labels,
    "networks": networks,
    "groups": groups,
    "groupContents": group_contents,
    "facts": _facts(model, gaps, dimensional),
  }

  document: dict[str, object] = {
    "documentInfo": {
      "documentType": DOCTYPE_COMPILED,
      "namespaces": namespaces,
      "description": description
      or (
        f"{model.filing.form or 'filing'} {model.filing.accession} "
        f"(CIK {model.filing.cik}) projected from XBRL by xbrlkit"
      ),
    },
    "xbrlModel": {k: v for k, v in xbrl_model.items() if v},
  }
  return document, gaps


@dataclass
class Dimensional:
  """The cube half of the model, rebuilt from the definition networks.

  XBRL expresses dimensionality as arcroles over ``<xs:element>``s: a hypercube
  is an element, an axis is an element, a domain and its members are elements.
  Tavi makes each a distinct object type, so this pass reads the arcroles and
  hands back the objects — plus ``claimed``, the element qnames that are now
  dimensional objects and must not also be emitted as concepts.
  """

  cubes: list[dict[str, object]] = field(default_factory=list)
  dimensions: list[dict[str, object]] = field(default_factory=list)
  domain_classes: list[dict[str, object]] = field(default_factory=list)
  domain_networks: list[dict[str, object]] = field(default_factory=list)
  members: list[dict[str, object]] = field(default_factory=list)
  claimed: set[str] = field(default_factory=set)
  # (all axes, required axes) per positive cube, for the section 8.5.2.5 check.
  cube_spaces: list[tuple[frozenset[str], frozenset[str]]] = field(default_factory=list)
  # cube name -> the extended link role whose definition linkbase declared its
  # hypercube, so the cube can join that role's group (section 10.2).
  cube_roles: dict[str, str] = field(default_factory=dict)

  def covers(self, signature: frozenset[str]) -> bool:
    """Whether some positive cube admits a fact carrying exactly ``signature``.

    A fact falls inside a cube when it uses no axis the cube lacks and supplies
    every axis the cube requires. Optional axes are the ones a fact may omit —
    see :func:`_cube_dimensions` for why a defaulted axis is optional.
    """
    return any(
      signature <= every and required <= signature
      for every, required in self.cube_spaces
    )


# XBRL Dimensions 1.0 arcroles. These are what ``Arc.arcrole`` preserves on a
# definition network, and they are the whole input to the cube reconstruction.
DIM_ALL = "http://xbrl.org/int/dim/arcrole/all"
DIM_NOT_ALL = "http://xbrl.org/int/dim/arcrole/notAll"
DIM_HYPERCUBE_DIMENSION = "http://xbrl.org/int/dim/arcrole/hypercube-dimension"
DIM_DIMENSION_DOMAIN = "http://xbrl.org/int/dim/arcrole/dimension-domain"
DIM_DOMAIN_MEMBER = "http://xbrl.org/int/dim/arcrole/domain-member"
DIM_DIMENSION_DEFAULT = "http://xbrl.org/int/dim/arcrole/dimension-default"

# Core dimensions are declared optional on every reconstructed cube: an XBRL
# hypercube constrains taxonomy dimensions only, and section 5.10.1's `optional`
# is what lets facts that omit a core dimension still fall inside the cube.
OPTIONAL_CORE_DIMENSIONS = ("xbrl:period", "xbrl:entity", "xbrl:unit", "xbrl:language")


def _report_namespace(report_id: str) -> str:
  return f"{REPORT_NS_BASE}/{report_id}"


def _dimensional(model: XbrlModel) -> Dimensional:
  """Rebuild cubes, dimensions, domains and members from definition networks.

  The traversal is XBRL Dimensions 1.0's, per base set: a hypercube's axes are
  the ``hypercube-dimension`` arcs in the role its ``all`` arc names, an
  axis's domain is the ``dimension-domain`` arc in *that* role, and so on down
  the members — each hop continuing in the previous arc's ``targetRole`` where
  one is set. A filing reuses one hypercube element across many sections with
  different axes and members, so cubes are one per (role, hypercube) and
  domain networks one per (cube, axis). Keying either on the element's QName
  alone, as this pass once did, unions every section into every cube — which
  the diff against Arelle's converter showed as cubes with axes and members
  their sections never declared.
  """
  out = Dimensional()
  # arcs by (role, arcrole): the unit of XDT traversal is the base set.
  by_role: dict[str, dict[str, list[Arc]]] = {}
  roles_in_order: list[str] = []
  for network in model.networks:
    if network.kind != "definition":
      continue
    if network.role_uri not in by_role:
      roles_in_order.append(network.role_uri)
    bucket = by_role.setdefault(network.role_uri, {})
    for arc in network.arcs:
      bucket.setdefault(arc.arcrole or "", []).append(arc)

  def arcs_from(role: str, arcrole: str, source: str) -> list[Arc]:
    return [a for a in by_role.get(role, {}).get(arcrole, []) if a.from_qname == source]

  # An axis with a default member is one a fact may omit: XBRL fills the gap
  # with the default, which is exactly what Tavi's `optional` cube dimension
  # means (section 5.10.1). Defaults are declared once, globally.
  defaulted_axes: frozenset[str] = frozenset(
    arc.from_qname
    for bucket in by_role.values()
    for arc in bucket.get(DIM_DIMENSION_DEFAULT, [])
  )

  dimensions: dict[str, dict[str, object]] = {}
  domain_classes: dict[str, dict[str, object]] = {}
  member_domain_classes: dict[str, set[str]] = {}
  # Identical domain networks (same root, same edges) across sections share one
  # object; there is nothing to distinguish them by.
  domain_network_names: dict[tuple[str, tuple[tuple[str, str], ...]], str] = {}
  cube_count = 0

  for role in roles_in_order:
    seen_hypercubes: set[str] = set()
    for all_arc in by_role[role].get(DIM_ALL, []):
      hypercube = all_arc.to_qname
      if hypercube in seen_hypercubes:
        continue  # several primary items sharing one hypercube: one cube
      seen_hypercubes.add(hypercube)
      out.claimed.add(hypercube)
      hypercube_role = all_arc.target_role or role

      axes: list[str] = []
      cube_networks: dict[str, str] = {}
      for hd in arcs_from(hypercube_role, DIM_HYPERCUBE_DIMENSION, hypercube):
        axis = hd.to_qname
        if axis in axes:
          continue
        axes.append(axis)
        out.claimed.add(axis)
        axis_role = hd.target_role or hypercube_role
        domain_arcs = arcs_from(axis_role, DIM_DIMENSION_DOMAIN, axis)
        if not domain_arcs:
          # A typed dimension has no domain element; its values conform to a
          # datatype instead (section 5.7).
          dimensions.setdefault(axis, {"name": axis})
          continue
        domain = domain_arcs[0].to_qname
        out.claimed.add(domain)
        dimensions.setdefault(axis, {"name": axis, "domainClass": domain})
        domain_classes.setdefault(domain, {"name": domain})

        edges = _walk_domain(domain, domain_arcs[0].target_role or axis_role, arcs_from)
        for _, target in edges:
          out.claimed.add(target)
          member_domain_classes.setdefault(target, set()).add(domain)
        key = (domain, tuple(edges))
        network_name = domain_network_names.get(key)
        if network_name is None:
          network_name = f"{REPORT_PREFIX}:domain-{len(domain_network_names)}"
          domain_network_names[key] = network_name
          out.domain_networks.append(
            {
              "name": network_name,
              "root": domain,
              "relationships": [
                {"source": source, "target": target} for source, target in edges
              ],
            }
          )
        cube_networks[axis] = network_name

      cube_name = f"{REPORT_PREFIX}:cube-{cube_count}"
      cube_count += 1
      out.cube_roles[cube_name] = role
      out.cubes.append(
        {
          "name": cube_name,
          "cubeDimensions": _cube_dimensions(axes, cube_networks, defaulted_axes),
        }
      )
      out.cube_spaces.append((frozenset(axes), frozenset(axes) - defaulted_axes))

  out.dimensions = [dimensions[axis] for axis in sorted(dimensions)]
  out.domain_classes = [domain_classes[d] for d in sorted(domain_classes)]
  for member in sorted(member_domain_classes):
    out.members.append(
      {"name": member, "domainClasses": sorted(member_domain_classes[member])}
    )

  # Section 8.5.2.5 requires every fact to fall inside some positive cube, and
  # undimensioned facts fall inside none of the hypercubes above. An open cube
  # (section 14.5.3 — a cube with no cubeType) is the model's own idiom for
  # "any concept, no taxonomy dimensions".
  out.cubes.append(
    {
      "name": f"{REPORT_PREFIX}:cube-undimensioned",
      "cubeDimensions": _cube_dimensions([], {}, defaulted_axes),
    }
  )
  out.cube_spaces.append((frozenset(), frozenset()))
  return out


def _cube_dimensions(
  axes: list[str],
  domain_network_names: dict[str, str],
  defaulted_axes: frozenset[str],
) -> list[dict[str, object]]:
  """Cube dimensions: the concept dimension, the core three, then the axes.

  The concept dimension is left open (no ``domainNetwork``), which section
  14.5.3 defines as admitting every concept. Reconstructing a concept domain
  per hypercube from the primary-item side of the ``all`` arcs would narrow the
  cube without adding information a consumer of one filing can use.
  """
  dimensions: list[dict[str, object]] = [{"dimension": "xbrl:concept"}]
  dimensions.extend(
    {"dimension": core, "optional": True} for core in OPTIONAL_CORE_DIMENSIONS
  )
  for axis in axes:
    entry: dict[str, object] = {"dimension": axis}
    network = domain_network_names.get(axis)
    if network:
      entry["domainNetwork"] = network
    if axis in defaulted_axes:
      entry["optional"] = True
    dimensions.append(entry)
  return dimensions


def _walk_domain(
  root: str,
  role: str,
  arcs_from: Callable[[str, str, str], list[Arc]],
) -> list[tuple[str, str]]:
  """Flatten a domain-member tree into source/target pairs, cycle-safe.

  Each hop is looked up in the previous arc's ``targetRole`` where one is
  set, else in the role the walk is already in.
  """
  pairs: list[tuple[str, str]] = []
  seen: set[str] = {root}
  queue: list[tuple[str, str]] = [(root, role)]
  while queue:
    parent, current_role = queue.pop(0)
    for arc in arcs_from(current_role, DIM_DOMAIN_MEMBER, parent):
      pairs.append((parent, arc.to_qname))
      if arc.to_qname not in seen:
        seen.add(arc.to_qname)
        queue.append((arc.to_qname, arc.target_role or current_role))
  return pairs


def _namespaces(model: XbrlModel, report_id: str) -> dict[str, str]:
  """Prefix -> URI map: reserved prefixes, the filing's taxonomies, ours.

  Section 2.4: the URI is the authoritative identity and the prefix is a
  serialisation convenience, so a generated prefix is sufficient wherever the
  source namespace does not carry one.
  """
  namespaces = dict(RESERVED_NAMESPACES)
  namespaces[REPORT_PREFIX] = _report_namespace(report_id)
  # The entity scheme: the SQName ``cik:0000066740`` (or ``entity:<id>`` under
  # any other scheme) needs it bound.
  prefix, scheme = entity_prefix(model.entity)
  namespaces[prefix] = scheme

  by_uri = {uri: prefix for prefix, uri in namespaces.items()}
  for concept in model.concepts.values():
    _bind(namespaces, by_uri, concept.qname, concept.namespace)
    # An item type with no built-in equivalent becomes a datatype object named
    # by its own QName, so its namespace needs a prefix too.
    if concept.item_type_qname and concept.item_type_namespace:
      local = concept.item_type_qname.split(":", 1)[-1]
      if local not in ITEM_TYPE_DATATYPES:
        _bind(namespaces, by_uri, concept.item_type_qname, concept.item_type_namespace)
  return namespaces


def _bind(
  namespaces: dict[str, str], by_uri: dict[str, str], qname: str, uri: str
) -> None:
  """Bind ``uri`` under the prefix ``qname`` carries, or a generated one."""
  if not uri or uri in by_uri:
    return
  prefix = qname.split(":", 1)[0] if ":" in qname else None
  if not prefix or prefix in namespaces:
    prefix = f"ns{len(namespaces)}"
  namespaces[prefix] = uri
  by_uri[uri] = prefix


def _model_properties(model: XbrlModel) -> list[dict[str, object]]:
  """Model-level properties: the report's own dates (sections 14.3.5/14.3.6)."""
  properties: list[dict[str, object]] = []
  if model.filing.filing_date:
    properties.append(
      {
        "property": "xbrl:reportFilingDate",
        "value": model.filing.filing_date.isoformat(),
      }
    )
  return properties


def _default_language(model: XbrlModel) -> str:
  """The language the filing reports its text in, for labels that have none.

  A label object requires a language (section 5.14.1) and an extended link
  role's definition carries none, so the group labels made from those
  definitions take the tag the filing's own text facts carry — ``en`` when
  the filing has no text facts at all.
  """
  tags = [fact.language for fact in model.facts if fact.language]
  if not tags:
    return "en"
  return max(set(tags), key=tags.count)


def _entities(model: XbrlModel) -> list[dict[str, object]]:
  """The reporting entity (section 8.1).

  An entity's SQName "includes the scheme and the identifier", so the scheme
  is the namespace and the identifier the local name — ``cik:0000066740`` for
  an SEC filer, the same name the OIM projection writes and Arelle's converter
  emits, and ``entity:<id>`` under any other scheme. It was previously minted
  under this report's own namespace, which dropped the scheme and gave two
  converters of one filing two different entities.
  """
  return [{"name": entity_sqname(model.entity)}]


def _entity_labels(model: XbrlModel, default_language: str) -> list[dict[str, object]]:
  """The entity's name as a label object pointing at the entity (section 5.14).

  ``forObject`` is "any", and a reader holding the compiled model and nothing
  else — no ``dei`` facts, no EDGAR header — still needs a name to put on the
  report. The entity object itself carries only its SQName.
  """
  if not model.entity.name:
    return []
  return [
    {
      "forObject": entity_sqname(model.entity),
      "labelType": DEFAULT_LABEL_TYPE,
      "value": model.entity.name,
      "language": default_language,
    }
  ]


def _units(model: XbrlModel) -> list[dict[str, object]]:
  """Unit objects (section 8.2), one per distinct measure the facts use.

  A unit's ``dataType`` is the datatype of what it measures. A pure unit is
  not an object at all — it is no unit (section 8.5.2.3). A composite measure
  cannot be a QName, so it becomes a unit object of this report whose
  ``compositeUnitRepresentation`` is the unit string the facts carry, which is
  how section 8.2 says a reported unit string resolves to a defined unit.
  """
  units: list[dict[str, object]] = []
  seen: set[str] = set()
  for unit in model.units:
    qname = _unit_qname(unit.measure)
    if qname is None or qname in seen:
      continue
    seen.add(qname)
    if "/" in qname or "*" in qname:
      units.append(
        {
          "name": f"{REPORT_PREFIX}:unit-{len(seen)}",
          "dataType": _unit_datatype(qname),
          "compositeUnitRepresentation": [qname],
        }
      )
    else:
      units.append({"name": qname, "dataType": _unit_datatype(qname)})
  return units


def _unit_datatype(unit: str) -> str:
  """The datatype of a value measured in ``unit``; xs:decimal when unknown."""
  numerator, _, denominator = unit.partition("/")
  if numerator.startswith("iso4217:"):
    if denominator == "xbrla:shares":
      return "xbrlr:perShare"
    return "xbrlr:monetary" if not denominator else "xs:decimal"
  if unit == "xbrla:shares":
    return "xbrla:sharesType"
  return "xs:decimal"


def _unit_qname(measure: str) -> str | None:
  """A measure token as the unit a Tavi fact carries, or ``None`` for pure.

  A pure unit is no unit (section 8.5.2.3). A share count is measured in the
  accounting module's unit. Anything else keeps the filing's own token: an
  ISO 4217 code is already a QName under the reserved prefix, a bare UTR token
  is bound to ``utr``, and a composite measure (``iso4217:USD/xbrli:shares``)
  becomes the unit string representation section 8.2 says a fact may carry.
  """
  measure = measure.strip()
  if "/" in measure or "*" in measure:
    return "/".join(
      "*".join(_measure_qname(part) or "xbrlr:pure" for part in side.split("*"))
      for side in measure.split("/")
    )
  return _measure_qname(measure)


def _measure_qname(measure: str) -> str | None:
  local = measure.rsplit(":", 1)[-1]
  if local == "pure":
    return None
  if local == "shares":
    return "xbrla:shares"
  if ":" in measure:
    return measure
  return f"utr:{measure}"


def _concepts_and_headings(
  model: XbrlModel,
  gaps: GapReport,
  dimensional: Dimensional,
  prefix_for_uri: dict[str, str],
) -> tuple[list[dict[str, object]], list[dict[str, object]], list[dict[str, object]]]:
  """Split concepts into concept objects and heading objects.

  Section 5.3: a heading is an object with no reportable value that is still a
  component of the concept dimension — exactly an abstract XBRL element. A
  hypercube is one too: it is abstract, and the presentation tree hangs the
  axes and line items under it, so it must exist as an object for those
  relationships to resolve. Its dimensional meaning lives in the cube object
  the ``all`` arc became, under a different name, so there is no collision.

  Elements the dimensional pass claimed (axes, domains, members) are excluded:
  in Tavi they are dimension, domain class and member objects, and emitting
  them here as well would collide on the name (``oimte:duplicateObjects``). In
  XBRL they are all ``<xs:element>``, which is precisely the flattening Tavi
  undoes.

  Returns the concepts, the headings, and the datatype objects (section 11.1)
  the concepts reference — one per taxonomy-defined item type.
  """
  concepts: list[dict[str, object]] = []
  headings: list[dict[str, object]] = []
  datatypes: dict[str, dict[str, object]] = {}

  for qname in sorted(model.concepts):
    concept = model.concepts[qname]
    if qname in dimensional.claimed and not concept.is_hypercube_item:
      continue
    if concept.is_abstract:
      headings.append({"name": qname})
      continue

    obj: dict[str, object] = {"name": qname}
    datatype = _datatype_for(concept, gaps, prefix_for_uri, datatypes)
    if datatype:
      obj["dataType"] = datatype
    if concept.period_type:
      obj["periodType"] = concept.period_type
    if concept.nillable:
      # Section 5.2: nillable defaults to false, and a nil fact on a concept
      # that does not declare it is an error. Every us-gaap concept declares it.
      obj["nillable"] = True
    properties = _concept_properties(concept)
    if properties:
      obj["properties"] = properties
    concepts.append(obj)

  return concepts, headings, list(datatypes.values())


def _datatype_for(
  concept: Concept,
  gaps: GapReport,
  prefix_for_uri: dict[str, str],
  datatypes: dict[str, dict[str, object]],
) -> str | None:
  """The concept's datatype: a built-in where one exists, else its own.

  An item type with no built-in equivalent — dei:yesNoItemType, the DTR's
  percentItemType, a filer's own enumeration — is neither dropped nor folded to
  its base. It becomes a datatype object (section 11.1) named by its own QName
  with the XML Schema type it derives from, which is what Arelle's converter
  does and what keeps the concept's real type in the model. A concept requires
  a datatype, so an unmapped type used to leave the object invalid.
  """
  item_type = concept.item_type
  if not item_type:
    return None
  local = item_type.split(":", 1)[-1]
  datatype = ITEM_TYPE_DATATYPES.get(local)
  if datatype is not None:
    return datatype
  prefix = prefix_for_uri.get(concept.item_type_namespace or "")
  if prefix:
    name = f"{prefix}:{local}"
    if name not in datatypes:
      base = concept.base_xsd_type or ""
      datatypes[name] = {
        "name": name,
        "baseType": f"xs:{base}" if base in XSD_SIMPLE_TYPES else "xs:string",
      }
    gaps.custom_datatypes[name] = gaps.custom_datatypes.get(name, 0) + 1
    return name
  bucket = (
    gaps.item_types_without_builtin
    if local in ITEM_TYPES_WITHOUT_BUILTIN
    else gaps.item_types_unmapped_here
  )
  bucket[local] = bucket.get(local, 0) + 1
  return None


def _concept_properties(concept: Concept) -> list[dict[str, object]]:
  """Concept-level properties. Balance is the accounting module's (section 15.3.1)."""
  properties: list[dict[str, object]] = []
  if concept.balance:
    properties.append({"property": "xbrla:balance", "value": concept.balance})
  return properties


def _labels(
  model: XbrlModel, gaps: GapReport, default_language: str
) -> list[dict[str, object]]:
  """Label objects (section 5.14): free-standing, pointing at ``forObject``.

  ``forObject`` is "any", so a label on an axis, domain or member is emitted
  unchanged — those objects still exist in the model, just under a different
  object type than they had in XBRL. A label's language is required; a label
  that arrived without one takes the filing's.
  """
  labels: list[dict[str, object]] = []
  for qname in sorted(model.concepts):
    for label in model.concepts[qname].labels:
      label_type = LABEL_ROLE_TYPES.get(label.role or "", None)
      if label_type is None:
        role = label.role or "(none)"
        gaps.unmapped_label_roles[role] = gaps.unmapped_label_roles.get(role, 0) + 1
        label_type = DEFAULT_LABEL_TYPE
      labels.append(
        {
          "forObject": qname,
          "labelType": label_type,
          "value": label.value or "",
          "language": label.language or default_language,
        }
      )
  return labels


def _networks_and_groups(
  model: XbrlModel, dimensional: Dimensional, default_language: str
) -> tuple[
  list[dict[str, object]],
  list[dict[str, object]],
  list[dict[str, object]],
  list[dict[str, object]],
]:
  """Networks (section 10.3) plus the groups (section 10.1) that carry them.

  An extended link role becomes a group: Tavi's group is the report section an
  ELR has always stood for, and it carries that role's presentation and
  calculation networks and the cube its definition linkbase declared.
  Definition networks themselves are held back for the cube pass — their
  arcroles are the raw material for ``cubeObject``.
  """
  networks: list[dict[str, object]] = []
  groups: list[dict[str, object]] = []
  group_contents: list[dict[str, object]] = []
  group_labels: list[dict[str, object]] = []
  group_names: dict[str, str] = {}
  definitions: dict[str, str] = {}
  for network in model.networks:
    if network.definition and network.role_uri not in definitions:
      definitions[network.role_uri] = network.definition

  def group_for(role_uri: str) -> str:
    group_name = group_names.get(role_uri)
    if group_name is None:
      group_name = f"{REPORT_PREFIX}:group-{len(group_names)}"
      group_names[role_uri] = group_name
      groups.append({"name": group_name, "groupURI": role_uri})
      definition = definitions.get(role_uri)
      if definition:
        # An extended link role's human-readable definition becomes a label on
        # the group, which is how the specification's own examples name a
        # group. A label requires a language (section 5.14.1); the definition
        # has none, so it takes the filing's.
        group_labels.append(
          {
            "forObject": group_name,
            "labelType": DEFAULT_LABEL_TYPE,
            "value": definition,
            "language": default_language,
          }
        )
    return group_name

  for index, network in enumerate(model.networks):
    if network.kind == "definition":
      continue
    relationship_type = (
      PRESENTATION_RELATIONSHIP
      if network.kind == "presentation"
      else CALCULATION_RELATIONSHIP
    )
    name = f"{REPORT_PREFIX}:network-{network.kind}-{index}"
    # Section 10.4: a network root is the target of a relationship whose source
    # is xbrl:rootSource. Those come first, so the roots read before the tree.
    roots = sorted({arc.from_qname for arc in network.arcs if arc.is_root})
    relationships: list[dict[str, object]] = [
      {"source": ROOT_SOURCE, "target": root, "order": float(position)}
      for position, root in enumerate(roots, start=1)
    ]
    relationships.extend(_relationship(arc, network) for arc in network.arcs)
    networks.append(
      {
        "name": name,
        "relationshipTypeName": relationship_type,
        "relationships": relationships,
      }
    )
    group_contents.append({"groupName": group_for(network.role_uri), "forObject": name})

  # A cube joins the section whose definition linkbase declared its hypercube
  # (section 10.2: group contents name cubes as well as networks).
  for cube in dimensional.cubes:
    role_uri = dimensional.cube_roles.get(str(cube["name"]))
    if role_uri:
      group_contents.append(
        {"groupName": group_for(role_uri), "forObject": cube["name"]}
      )

  return networks, groups, group_contents, group_labels


def _relationship(arc: Arc, network: Network) -> dict[str, object]:
  """One relationship object (section 10.4), with its link properties."""
  entry: dict[str, object] = {"source": arc.from_qname, "target": arc.to_qname}
  if arc.order is not None:
    entry["order"] = arc.order
  properties: list[dict[str, object]] = []
  if network.kind == "calculation" and arc.weight is not None:
    # Section 14.3.1: weight is required on summation-item. Reconciliation is
    # deliberately absent — SPEC_AMBIGUITIES: reconciliation-required-but-unused.
    properties.append({"property": "xbrl:weight", "value": arc.weight})
  if arc.preferred_label:
    label_type = LABEL_ROLE_TYPES.get(arc.preferred_label)
    if label_type:
      properties.append({"property": "xbrl:preferredLabel", "value": label_type})
  if properties:
    entry["properties"] = properties
  return entry


def _facts(
  model: XbrlModel, gaps: GapReport, dimensional: Dimensional
) -> list[dict[str, object]]:
  """Fact objects (section 8.3) — the near-identity mapping.

  ``factDimensions`` is a flat name/value map over concept/period/unit/entity
  plus taxonomy-defined dimensions as peers, which is the shape the parse
  already produces.
  """
  periods = {period.id: period for period in model.periods}
  units = {unit.id: unit for unit in model.units}
  entity = entity_sqname(model.entity)
  facts: list[dict[str, object]] = []

  for index, fact in enumerate(model.facts):
    concept = model.concepts.get(fact.concept_qname)
    period = periods.get(fact.period_id)
    dimensions: dict[str, object] = {"xbrl:concept": fact.concept_qname}
    if period is not None:
      dimensions["xbrl:period"] = _period_value(period)
      _record_period_semantics(period, gaps)
    dimensions["xbrl:entity"] = entity
    if fact.unit_id and fact.unit_id in units:
      unit = _unit_qname(units[fact.unit_id].measure)
      # A pure unit is equivalent to no unit, and a numeric fact without one is
      # read as xbrlr:pure (section 8.5.2.3), so the dimension is omitted — the
      # same convention as xBRL-JSON.
      if unit is not None:
        dimensions["xbrl:unit"] = unit
    # The language dimension applies to text facts (section 8.3), and the fact
    # value carries the same tag (section 8.4.1); both are written, as Arelle's
    # converter does. The parse captures the tag; it was previously dropped
    # here — the same fidelity bug the OIM diff found in the parse, one layer up.
    language = (
      language_tag(fact.language)
      if concept is not None and concept.is_text_fact
      else None
    )
    if language:
      dimensions["xbrl:language"] = language

    for qualifier in fact.dims:
      value = qualifier.member_qname or qualifier.typed_value
      if value is not None:
        dimensions[qualifier.axis_qname] = value

    signature = frozenset(qualifier.axis_qname for qualifier in fact.dims)
    if fact.dims:
      gaps.dimensional_facts += 1
    if not dimensional.covers(signature):
      gaps.facts_without_cube += 1

    entry: dict[str, object] = {
      "name": f"{REPORT_PREFIX}:f-{index}",
      "factDimensions": dimensions,
    }
    if not fact.is_nil:
      # A nil fact has no fact value; a reported one has exactly one, carrying
      # the lexical value as a string (SPEC_AMBIGUITIES: fact-value-literal-type).
      fact_value: dict[str, object] = {"value": _fact_value(fact)}
      if language:
        fact_value["language"] = language
      if fact.decimals is not None:
        decimals = _decimals(fact.decimals)
        if decimals is not None:
          fact_value["decimals"] = decimals
      entry["factValues"] = [fact_value]
    facts.append(entry)
  return facts


def _fact_value(fact: XbrlFact) -> str | None:
  """The reported lexical value, as a string.

  The parse keeps the value as reported (``value_str``) for every fact; the
  numeric form is a convenience derived from it. A number is written from the
  lexical value, not the float, so ``24575000000`` does not become
  ``24575000000.0`` and a precise decimal is not rounded through binary.
  """
  if fact.value_str is not None:
    return fact.value_str
  if fact.numeric_value is None:
    return None
  number = fact.numeric_value
  return str(int(number)) if number.is_integer() else repr(number)


def _period_value(period: Period) -> str:
  """A period as an ISO 8601 interval of dateTimes (section 8.5.2.2).

  The built-in ``xbrlr:periodString`` requires ``xs:dateTime`` values with the
  time component present, so the parse's human-facing dates are rolled forward
  onto the exclusive end — the same literal the OIM projection writes for the
  same fact. See SPEC_AMBIGUITIES: period-literal-form.
  """
  return period_interval(period)


def _record_period_semantics(period: Period, gaps: GapReport) -> None:
  """Record period meaning that Tavi's bare interval cannot carry.

  ``xbrl:period`` is an ISO 8601 interval and nothing else. The four fields the
  parse derives — the duration bucket and the calendar placement — have no home
  in the model, and neither does the YTD-vs-standalone distinction that every
  consumer has to reconstruct before it can compute a quarter from cumulative
  figures. This is the concrete form of the gap.
  """
  if period.duration_type is None and period.calendar_period_key is None:
    return
  entry = {
    "period": _period_value(period),
    "duration_type": period.duration_type,
    "calendar_year": period.calendar_year,
    "calendar_quarter": period.calendar_quarter,
    "calendar_period_key": period.calendar_period_key,
  }
  if entry not in gaps.dropped_period_semantics:
    gaps.dropped_period_semantics.append(entry)


def _decimals(value: str) -> int | None:
  """``decimals`` is an integer in Tavi; INF means infinitely precise (absent)."""
  if value.upper() in ("INF", "INFINITY"):
    return None
  try:
    return int(value)
  except ValueError:
    return None
