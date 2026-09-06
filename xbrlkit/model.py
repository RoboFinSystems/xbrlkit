"""XbrlModel — the neutral, lossless, single-filing in-memory model.

One parse produces this; each serializer consumes it. Today there is one
serializer (the holon / RDF projection); a second (the LPG / parquet
projection) is planned. The point of this model is that both hang off one
parse.

Fidelity loss is a *projection* choice, never a limitation of this model. The
parse captures the full XBRL — text facts, dimensions, every network — and each
serializer decides what to shed (the holon MVP drops text facts and
dimensions; the LPG projection keeps them).

Stateless and single-filing by design: an ``XbrlModel`` describes exactly one
filing and knows nothing about any other. All cross-filing / corpus concerns
(dedup, aggregation) live in the caller, not here.
"""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, Field

PeriodType = Literal["instant", "duration", "forever"]
BalanceType = Literal["debit", "credit"]
NetworkKind = Literal["presentation", "calculation", "definition"]
ValueKind = Literal["numeric", "text"]
DurationType = Literal["annual", "quarterly", "semi_annual", "nine_months", "other"]
AxisType = Literal["segment", "scenario"]


class FilingMeta(BaseModel):
  """Identity + fiscal context of the one filing this model describes."""

  accession: str
  cik: str  # zero-padded 10-digit
  form: str | None = None
  filing_date: date | None = None
  fiscal_year_focus: str | None = None
  fiscal_period_focus: str | None = None
  fiscal_year_end_month: str | None = None
  taxonomy_namespaces: list[str] = Field(default_factory=list)
  # EDGAR's record of the filing beyond form and date: the period the report
  # covers, when EDGAR accepted it, whether it is inline XBRL, and the file
  # Arelle loaded (the inline primary document, or the classic instance).
  report_date: date | None = None
  acceptance_datetime: str | None = None
  is_inline_xbrl: bool = True
  primary_document: str | None = None
  # The primary document's EDGAR URL. The property-graph projection scopes
  # report-level ids (the report, its facts, its dimensions) on it, exactly as
  # the RoboSystems platform does, so the same filing yields the same ids.
  report_uri: str | None = None
  # The filer's own taxonomy namespace — the schema shipped in the filing
  # package — as distinct from the standard taxonomies it imports.
  extension_namespace: str | None = None


class EntityIdentity(BaseModel):
  """The reporting entity (the XBRL context entity, resolved to the filer).

  Everything beyond ``cik`` and ``scheme`` comes from the EDGAR submissions
  header, not the XBRL instance; each is ``None`` when unknown.
  """

  cik: str
  scheme: str = "http://www.sec.gov/CIK"
  name: str | None = None
  legal_name: str | None = None
  ein: str | None = None
  ticker: str | None = None
  exchange: str | None = None
  sic: str | None = None
  sic_description: str | None = None
  category: str | None = None
  state_of_incorporation: str | None = None
  fiscal_year_end: str | None = None
  entity_type: str | None = None
  website: str | None = None
  phone: str | None = None


class Label(BaseModel):
  """One label-linkbase entry for a concept (role selects standard/terse/…).

  ``value`` is ``None`` for an empty label element (a documentation label
  with no text), which is not the same as an empty string.
  """

  value: str | None = None
  role: str | None = None
  language: str | None = None


class Reference(BaseModel):
  """One part of a reference-linkbase entry for a concept (a Topic, a
  Paragraph, a URI…), with the reference's role."""

  value: str
  role: str | None = None


class Concept(BaseModel):
  """An XBRL concept (``<xs:element>``) as walked from the DTS.

  Coverage is DTS-wide, not fact-driven: a ``Concept`` exists for every qname
  the slice touches — reported facts, presentation/calculation/definition arc
  endpoints (abstract headers, subtotals), and dimension axes/members/domains/
  hypercubes — so labels and structural flags are available for all of them.
  """

  qname: str
  namespace: str
  name: str
  period_type: PeriodType | None = None
  balance: BalanceType | None = None
  is_abstract: bool = False
  is_numeric: bool = False
  is_textblock: bool = False
  is_hypercube_item: bool = False
  is_dimension_item: bool = False
  is_domain_member: bool = False
  is_shares: bool = False
  is_integer: bool = False
  is_fraction: bool = False
  substitution_group: str | None = None
  substitution_group_namespace: str | None = None
  item_type: str | None = None
  # Arelle's user-facing type name: hypercubes "Table", dimensions "Axis",
  # otherwise the item type with "ItemType" removed ("Monetary", "String").
  nice_type: str | None = None
  # The item type's QName and namespace, so a projection can name it — a
  # taxonomy-defined type (dei:yesNoItemType) becomes a datatype object in Tavi
  # rather than being folded to its base — and the XML Schema simple type it
  # ultimately derives from (Arelle's baseXsdType: "string", "decimal", …).
  item_type_qname: str | None = None
  item_type_namespace: str | None = None
  base_xsd_type: str | None = None
  # xsi:nillable on the element. Tavi defaults it to false and a nil fact on a
  # concept that does not declare it is an error; every us-gaap concept does.
  nillable: bool = False
  # Whether facts of this concept are OIM "text facts" — string-derived, and
  # not one of the DTR no-language item types. This is what decides whether the
  # OIM language dimension applies, and it is the same line Tavi draws for its
  # text-fact definition. Neither `item_type` nor the base XSD type answers it:
  # centralIndexKeyItemType and enumerationSetItemType are both token-derived,
  # and only the first takes a language. Resolving it requires walking the type
  # derivation chain against the DTS, so it is captured at parse time rather
  # than inferred downstream.
  is_text_fact: bool = False
  pref_label: str | None = None
  labels: list[Label] = Field(default_factory=list)
  references: list[Reference] = Field(default_factory=list)


class Period(BaseModel):
  """A reporting period. ``end`` carries the instant date for instant periods.

  ``id`` is a content-derived, cross-filing-stable identifier so periods
  dedupe. Dates are already normalized (Arelle's exclusive next-midnight has
  been rolled back by one day at parse time). The calendar fields are a
  deterministic enrichment derived from the dates (not raw XBRL) — they place a
  period on a common calendar axis so "which quarter/year is this" is legible
  without re-deriving it: ``duration_type`` buckets the day span,
  ``calendar_year``/``calendar_quarter`` normalize by the end date, and
  ``calendar_period_key`` is a compact label (``2026Q1`` / ``2026`` / a date).
  """

  id: str
  period_type: PeriodType
  start: date | None = None
  end: date | None = None
  duration_type: DurationType | None = None
  calendar_year: int | None = None
  calendar_quarter: str | None = None
  calendar_period_key: str | None = None


class Unit(BaseModel):
  """A unit of measure. ``measure`` is the resolved token (e.g. ``iso4217:USD``).

  ``uri`` is the measure as ``namespace#localName`` (``num/den`` for a
  divided unit) — the content the id is derived from.
  """

  id: str
  measure: str
  uri: str | None = None
  numerator_uri: str | None = None
  denominator_uri: str | None = None


class DimQualifier(BaseModel):
  """One dimensional coordinate on a fact's context (segment/scenario member).

  ``axis_type`` records whether the coordinate came from the context's
  ``<segment>`` or ``<scenario>`` (resolved per-dimension, not per-context).
  """

  axis_qname: str
  member_qname: str | None = None
  typed_value: str | None = None
  is_explicit: bool = True
  axis_type: AxisType | None = None


class XbrlFact(BaseModel):
  """One reported fact. Numeric ⇔ the fact carries a unit (XBRL convention)."""

  id: str
  concept_qname: str
  period_id: str
  unit_id: str | None = None
  entity_cik: str
  # The context's entity as written — scheme and identifier — beside the
  # normalized ``entity_cik``. A subsidiary's context carries its own.
  entity_scheme: str | None = None
  entity_identifier: str | None = None
  dims: list[DimQualifier] = Field(default_factory=list)
  value_str: str | None = None
  # The fact's lexical value exactly as Arelle read it, before XML Schema
  # whitespace processing; ``value_str`` is the processed form OIM writes.
  raw_value: str | None = None
  # Arelle's MD5 of the fact — the platform's report-scoped fact id stem.
  source_hash: str | None = None
  numeric_value: float | None = None
  decimals: str | None = None
  value_kind: ValueKind = "numeric"
  # xsi:nil — the filing reported the fact as *not disclosed*, which is not the
  # same as an empty string, and which every serialization writes as null.
  is_nil: bool = False
  # The fact's xml:lang, which XBRL carries on non-numeric facts. Both OIM and
  # Tavi have a place for it; the parse previously kept language only on
  # labels, so every projection was silently dropping it.
  language: str | None = None


class Arc(BaseModel):
  """One linkbase relationship (parent → child), qname-addressed.

  ``arcrole`` is the full arcrole URI. For presentation/calculation it is the
  parent-child / summation-item role; for definition networks it distinguishes
  the XBRL-dimensions wiring (all / hypercube-dimension / dimension-domain /
  domain-member / dimension-default), which ``Network.kind`` alone collapses.
  """

  from_qname: str
  to_qname: str
  arcrole: str | None = None
  order: float | None = None
  weight: float | None = None
  preferred_label: str | None = None
  is_root: bool = False
  # xbrldt:targetRole on a dimensional arc: the extended link role in which the
  # *next* hop of the hypercube→dimension→domain→member traversal is found.
  # Absent, the traversal stays in the arc's own role. A cube rebuilt without
  # following it loses every axis or member a filing declared in another role.
  target_role: str | None = None


class Network(BaseModel):
  """One extended-link-role network (a statement or disclosure), one linkbase kind."""

  role_uri: str
  definition: str | None = None
  # A second, longer reading of the role — XBRL's documentation label role.
  # A producer whose definition is a composed sort key ("0001 - Statement -
  # Balance Sheet") keeps the role's own name here, so it is not lost.
  documentation: str | None = None
  kind: NetworkKind
  arcs: list[Arc] = Field(default_factory=list)
  # The ``id`` of the role's ``<link:roleType>`` in the filer's schema, when
  # declared; the property-graph projection names the structure by it.
  role_id: str | None = None


class XbrlModel(BaseModel):
  """The whole filing as neutral objects — the contract between parse and serialize."""

  filing: FilingMeta
  entity: EntityIdentity
  concepts: dict[str, Concept] = Field(default_factory=dict)
  periods: list[Period] = Field(default_factory=list)
  units: list[Unit] = Field(default_factory=list)
  facts: list[XbrlFact] = Field(default_factory=list)
  networks: list[Network] = Field(default_factory=list)
