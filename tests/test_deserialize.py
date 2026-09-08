"""Tests for the importers (``deserialize/``) — XBRL is not the only source.

The property that matters is the **round trip**: a model projected into a
serialization and read back has to be the same model, and where it is not, the
difference has to be the serialization's own gap rather than the importer's
invention. So these tests assert both halves — what comes back, and what does
not — and then assert the two importers agree with each other, because a Tavi
and a holon of one filing are two descriptions of the same report and should
answer a question the same way.
"""

from __future__ import annotations

import functools
import http.server
import json
import socketserver
import threading
from datetime import date
from pathlib import Path

import pytest

from xbrlkit.deserialize import (
  HolonError,
  TaviError,
  from_holon_json,
  from_holon_report,
  from_tavi_json,
  from_tavi_report,
)
from xbrlkit.model import (
  Arc,
  Concept,
  DimQualifier,
  EntityIdentity,
  FilingMeta,
  Label,
  Network,
  Unit,
  XbrlFact,
  XbrlModel,
)
from xbrlkit.parse.ids import unit_id
from xbrlkit.periods import duration_period, instant_period, period_from_interval
from xbrlkit.deserialize.holon import _scheme_for
from xbrlkit.namespaces import ENTITY_SCHEME
from xbrlkit.serialize import to_holon, to_tavi
from xbrlkit.serialize._values import CIK_SCHEME
from xbrlkit.serve import tools
from xbrlkit.serve.session import FilingSession, SourceError

US_GAAP = "http://fasb.org/us-gaap/2024"
DEI = "http://xbrl.sec.gov/dei/2024"
STANDARD = "http://www.xbrl.org/2003/role/label"
TERSE = "http://www.xbrl.org/2003/role/terseLabel"
PARENT_CHILD = "http://www.xbrl.org/2003/arcrole/parent-child"
SUMMATION = "http://www.xbrl.org/2003/arcrole/summation-item"
DIM = "http://xbrl.org/int/dim/arcrole"
BALANCE_SHEET = "http://example.com/role/BalanceSheet"
USD_URI = "http://www.xbrl.org/2003/iso4217#USD"


def _model() -> XbrlModel:
  """A filing shaped like a parse: real period ids, labels by role, a segment.

  Deliberately not the ``test_tavi`` fixture, which hand-writes period ids and
  omits derived fields — this one is what :mod:`xbrlkit.parse` would produce,
  so a difference after a round trip is the serialization's and not the
  fixture's.
  """
  instant = instant_period(date(2024, 12, 31))
  duration = duration_period(date(2024, 1, 1), date(2024, 12, 31))
  concepts = {
    "us-gaap:AssetsAbstract": Concept(
      qname="us-gaap:AssetsAbstract",
      namespace=US_GAAP,
      name="AssetsAbstract",
      is_abstract=True,
      pref_label="Assets [Abstract]",
      labels=[Label(value="Assets [Abstract]", role=STANDARD, language="en-US")],
    ),
    "us-gaap:Assets": Concept(
      qname="us-gaap:Assets",
      namespace=US_GAAP,
      name="Assets",
      period_type="instant",
      balance="debit",
      is_numeric=True,
      item_type="monetaryItemType",
      base_xsd_type="decimal",
      nillable=True,
      pref_label="Assets",
      labels=[
        Label(value="Assets", role=STANDARD, language="en-US"),
        Label(value="Total assets", role=TERSE, language="en-US"),
      ],
    ),
    "us-gaap:Cash": Concept(
      qname="us-gaap:Cash",
      namespace=US_GAAP,
      name="Cash",
      period_type="instant",
      balance="debit",
      is_numeric=True,
      item_type="monetaryItemType",
      base_xsd_type="decimal",
      pref_label="Cash",
      labels=[Label(value="Cash", role=STANDARD, language="en-US")],
    ),
    "us-gaap:SegmentTable": Concept(
      qname="us-gaap:SegmentTable",
      namespace=US_GAAP,
      name="SegmentTable",
      is_abstract=True,
      is_hypercube_item=True,
    ),
    "us-gaap:SegmentDomain": Concept(
      qname="us-gaap:SegmentDomain",
      namespace=US_GAAP,
      name="SegmentDomain",
      is_domain_member=True,
    ),
    "us-gaap:SegmentAxis": Concept(
      qname="us-gaap:SegmentAxis",
      namespace=US_GAAP,
      name="SegmentAxis",
      is_abstract=True,
      is_dimension_item=True,
    ),
    "us-gaap:NorthAmerica": Concept(
      qname="us-gaap:NorthAmerica",
      namespace=US_GAAP,
      name="NorthAmerica",
      is_domain_member=True,
    ),
    "dei:DocumentType": Concept(
      qname="dei:DocumentType",
      namespace=DEI,
      name="DocumentType",
      period_type="duration",
      item_type="stringItemType",
      base_xsd_type="string",
      is_text_fact=True,
    ),
    "dei:DocumentFiscalYearFocus": Concept(
      qname="dei:DocumentFiscalYearFocus",
      namespace=DEI,
      name="DocumentFiscalYearFocus",
      period_type="duration",
      item_type="stringItemType",
      base_xsd_type="string",
      is_text_fact=True,
    ),
    "dei:DocumentFiscalPeriodFocus": Concept(
      qname="dei:DocumentFiscalPeriodFocus",
      namespace=DEI,
      name="DocumentFiscalPeriodFocus",
      period_type="duration",
      item_type="stringItemType",
      base_xsd_type="string",
      is_text_fact=True,
    ),
  }
  facts = [
    XbrlFact(
      id="f1",
      concept_qname="us-gaap:Assets",
      period_id=instant.id,
      unit_id=unit_id(USD_URI),
      entity_cik="0001234567",
      value_str="1000",
      numeric_value=1000.0,
      decimals="-3",
    ),
    XbrlFact(
      id="f2",
      concept_qname="us-gaap:Cash",
      period_id=instant.id,
      unit_id=unit_id(USD_URI),
      entity_cik="0001234567",
      value_str="400",
      numeric_value=400.0,
      decimals="-3",
      dims=[
        DimQualifier(
          axis_qname="us-gaap:SegmentAxis", member_qname="us-gaap:NorthAmerica"
        )
      ],
    ),
    XbrlFact(
      id="f3",
      concept_qname="dei:DocumentType",
      period_id=duration.id,
      entity_cik="0001234567",
      value_str="10-K",
      value_kind="text",
      language="en-us",
    ),
    XbrlFact(
      id="f4",
      concept_qname="dei:DocumentFiscalYearFocus",
      period_id=duration.id,
      entity_cik="0001234567",
      value_str="2024",
      value_kind="text",
      language="en-us",
    ),
    XbrlFact(
      id="f5",
      concept_qname="dei:DocumentFiscalPeriodFocus",
      period_id=duration.id,
      entity_cik="0001234567",
      value_str="FY",
      value_kind="text",
      language="en-us",
    ),
  ]
  networks = [
    Network(
      role_uri=BALANCE_SHEET,
      definition="Balance Sheet",
      kind="presentation",
      arcs=[
        Arc(
          from_qname="us-gaap:AssetsAbstract",
          to_qname="us-gaap:Assets",
          arcrole=PARENT_CHILD,
          order=1.0,
          preferred_label=TERSE,
          is_root=True,
        ),
        Arc(
          from_qname="us-gaap:Assets",
          to_qname="us-gaap:Cash",
          arcrole=PARENT_CHILD,
          order=2.0,
        ),
      ],
    ),
    Network(
      role_uri="http://example.com/role/Segments",
      definition="Segments",
      kind="definition",
      arcs=[
        Arc(
          from_qname="us-gaap:AssetsAbstract",
          to_qname="us-gaap:SegmentTable",
          arcrole=f"{DIM}/all",
          is_root=True,
        ),
        Arc(
          from_qname="us-gaap:SegmentTable",
          to_qname="us-gaap:SegmentAxis",
          arcrole=f"{DIM}/hypercube-dimension",
        ),
        Arc(
          from_qname="us-gaap:SegmentAxis",
          to_qname="us-gaap:SegmentDomain",
          arcrole=f"{DIM}/dimension-domain",
        ),
        Arc(
          from_qname="us-gaap:SegmentDomain",
          to_qname="us-gaap:NorthAmerica",
          arcrole=f"{DIM}/domain-member",
        ),
      ],
    ),
    Network(
      role_uri=BALANCE_SHEET,
      definition="Balance Sheet",
      kind="calculation",
      arcs=[
        Arc(
          from_qname="us-gaap:Assets",
          to_qname="us-gaap:Cash",
          arcrole=SUMMATION,
          order=1.0,
          weight=1.0,
          is_root=True,
        )
      ],
    ),
  ]
  return XbrlModel(
    filing=FilingMeta(
      accession="0000000000-24-000001",
      cik="0001234567",
      form="10-K",
      filing_date=date(2025, 2, 14),
      fiscal_year_focus="2024",
      fiscal_period_focus="FY",
      fiscal_year_end_month="12",
      taxonomy_namespaces=[DEI, US_GAAP],
    ),
    entity=EntityIdentity(cik="0001234567", name="Acme Corp"),
    concepts=concepts,
    periods=[instant, duration],
    units=[
      Unit(
        id=unit_id(USD_URI),
        measure="iso4217:USD",
        uri=USD_URI,
      )
    ],
    facts=facts,
    networks=networks,
  )


@pytest.fixture
def model() -> XbrlModel:
  return _model()


def _through(model: XbrlModel, fmt: str) -> XbrlModel:
  """The model as it comes back through one serialization."""
  if fmt == "tavi":
    return from_tavi_json(to_tavi(model))
  return from_holon_json(to_holon(model))


@pytest.fixture
def through_tavi(model: XbrlModel) -> XbrlModel:
  return from_tavi_json(to_tavi(model))


@pytest.fixture
def through_holon(model: XbrlModel) -> XbrlModel:
  return from_holon_json(to_holon(model))


# -- what survives ---------------------------------------------------------------


@pytest.mark.parametrize("fmt", ["tavi", "holon"])
def test_round_trip_keeps_the_filing(model: XbrlModel, fmt: str) -> None:
  got = _through(model, fmt)
  assert got.filing.accession == model.filing.accession
  assert got.filing.cik == model.filing.cik
  assert got.filing.form == model.filing.form
  assert got.filing.filing_date == model.filing.filing_date
  assert got.filing.fiscal_year_focus == model.filing.fiscal_year_focus
  assert got.filing.fiscal_period_focus == model.filing.fiscal_period_focus
  assert got.entity.cik == model.entity.cik
  assert got.entity.scheme == model.entity.scheme
  # Neither serialization says whether the filing behind it was inline XBRL,
  # so neither importer claims it did.
  assert got.filing.is_inline_xbrl is None


@pytest.mark.parametrize("fmt", ["tavi", "holon"])
def test_round_trip_keeps_periods_and_their_ids(model: XbrlModel, fmt: str) -> None:
  got = _through(model, fmt)
  assert {p.id for p in got.periods} == {p.id for p in model.periods}
  by_id = {p.id: p for p in got.periods}
  for period in model.periods:
    other = by_id[period.id]
    assert (other.period_type, other.start, other.end) == (
      period.period_type,
      period.start,
      period.end,
    )
    # The calendar fields are derived, not carried: recomputing them is what
    # makes an id-for-id match possible in the first place.
    assert other.duration_type == period.duration_type
    assert other.calendar_period_key == period.calendar_period_key


@pytest.mark.parametrize("fmt", ["tavi", "holon"])
def test_round_trip_keeps_the_facts(model: XbrlModel, fmt: str) -> None:
  got = _through(model, fmt)
  assert len(got.facts) == len(model.facts)
  values = {(f.concept_qname, f.value_str) for f in got.facts}
  assert ("us-gaap:Assets", "1000") in values
  assert ("dei:DocumentType", "10-K") in values
  cash = next(f for f in got.facts if f.concept_qname == "us-gaap:Cash")
  assert cash.value_str == "400"
  assert cash.decimals == "-3"
  assert cash.value_kind == "numeric"
  assert [(d.axis_qname, d.member_qname) for d in cash.dims] == [
    ("us-gaap:SegmentAxis", "us-gaap:NorthAmerica")
  ]
  assert cash.unit_id == next(u.id for u in got.units if u.measure == "iso4217:USD")


@pytest.mark.parametrize("fmt", ["tavi", "holon"])
def test_round_trip_keeps_the_networks(model: XbrlModel, fmt: str) -> None:
  got = _through(model, fmt)
  by_kind = {(n.role_uri, n.kind): n for n in got.networks}
  presentation = by_kind[(BALANCE_SHEET, "presentation")]
  assert presentation.definition == "Balance Sheet"
  assert [(a.from_qname, a.to_qname, a.order) for a in presentation.arcs] == [
    ("us-gaap:AssetsAbstract", "us-gaap:Assets", 1.0),
    ("us-gaap:Assets", "us-gaap:Cash", 2.0),
  ]
  assert presentation.arcs[0].preferred_label == TERSE
  assert presentation.arcs[0].is_root is True
  assert presentation.arcs[1].is_root is False
  calculation = by_kind[(BALANCE_SHEET, "calculation")]
  assert [(a.to_qname, a.weight) for a in calculation.arcs] == [("us-gaap:Cash", 1.0)]


@pytest.mark.parametrize("fmt", ["tavi", "holon"])
def test_round_trip_keeps_the_concept_facts(model: XbrlModel, fmt: str) -> None:
  got = _through(model, fmt)
  assets = got.concepts["us-gaap:Assets"]
  assert assets.period_type == "instant"
  assert assets.balance == "debit"
  assert assets.is_numeric is True
  assert assets.item_type == "monetaryItemType"
  assert got.concepts["us-gaap:AssetsAbstract"].is_abstract is True
  assert got.concepts["us-gaap:SegmentAxis"].is_dimension_item is True
  assert got.concepts["us-gaap:NorthAmerica"].is_domain_member is True


# -- what does not, and is reported rather than invented -------------------------


def test_tavi_loses_the_definition_networks_the_holon_keeps(
  through_tavi: XbrlModel, through_holon: XbrlModel
) -> None:
  """Tavi turns the dimensional wiring into cube objects, which do not come
  back as arcs. The holon writes it as associations of its own kind, so the
  definition networks survive there — and the axes and members survive both."""
  assert not [n for n in through_tavi.networks if n.kind == "definition"]
  definition = [n for n in through_holon.networks if n.kind == "definition"]
  assert [(a.from_qname, a.to_qname) for a in definition[0].arcs] == [
    ("us-gaap:AssetsAbstract", "us-gaap:SegmentTable"),
    ("us-gaap:SegmentTable", "us-gaap:SegmentAxis"),
    ("us-gaap:SegmentAxis", "us-gaap:SegmentDomain"),
    ("us-gaap:SegmentDomain", "us-gaap:NorthAmerica"),
  ]
  for got in (through_tavi, through_holon):
    assert got.concepts["us-gaap:SegmentAxis"].is_dimension_item is True
    assert got.concepts["us-gaap:NorthAmerica"].is_domain_member is True
  # Only the holon says a hypercube is one; Tavi has no flag for it.
  assert through_holon.concepts["us-gaap:SegmentTable"].is_hypercube_item is True
  assert through_tavi.concepts["us-gaap:SegmentTable"].is_hypercube_item is False


def test_tavi_gaps_are_declared(model: XbrlModel) -> None:
  _, gaps = from_tavi_report(to_tavi(model))
  reported = " ".join(gaps.missing)
  assert "is_hypercube_item" in reported
  assert "source_hash" in reported
  assert gaps.unmapped_datatypes == {}
  assert gaps.unmapped_label_types == {}


def test_holon_gaps_are_declared(model: XbrlModel) -> None:
  got, gaps = from_holon_report(to_holon(model))
  reported = " ".join(gaps.missing)
  assert "no fact, network or dimension mentions" in reported
  assert "source_hash" in reported
  assert gaps.unresolved_references == 0


def test_holon_carries_what_the_filing_declared(
  model: XbrlModel, through_holon: XbrlModel
) -> None:
  """The four fields the holon used to drop, and the namespace it used to
  flatten — the round trip is what showed they were missing."""
  assets = through_holon.concepts["us-gaap:Assets"]
  assert assets.nillable is True
  assert assets.namespace == US_GAAP  # the year the filing used, not a stem
  assert assets.item_type == "monetaryItemType"
  assert assets.base_xsd_type == "decimal"
  assert (TERSE, "Total assets") in [(lab.role, lab.value) for lab in assets.labels]
  document_type = next(
    f for f in through_holon.facts if f.concept_qname == "dei:DocumentType"
  )
  assert document_type.language == "en-us"
  assert through_holon.concepts["dei:DocumentType"].is_text_fact is True


def test_a_holon_binds_the_filings_own_namespaces(model: XbrlModel) -> None:
  """A concept compacts to its QName against the taxonomy the filing declared.

  The document said ``http://fasb.org/us-gaap/Revenues`` before — an address
  inside FASB's namespace that FASB never minted, with the year dropped — and
  a filer's own concepts did not compact at all.
  """
  document = json.loads(to_holon(model))
  assert document["@context"]["us-gaap"] == f"{US_GAAP}#"
  assert document["@context"]["dei"] == f"{DEI}#"
  ids = [
    node["@id"]
    for graph in document["@graph"]
    for node in graph["@graph"]
    if "Element" in str(node.get("@type"))
  ]
  assert ids and all(":" in i and "://" not in i for i in ids)


def test_holon_keeps_the_preferred_label_a_network_resolved(
  model: XbrlModel, through_holon: XbrlModel
) -> None:
  """A presentation association carries the preferred label it resolved to, so
  the label reaches the concept even from a holon that never listed it.

  Every label is written on the element now, which is why this strips one back
  out: the association is where a holon written before that came from, and
  reading it is what took a real filing's balance sheet from 40 wrong labels
  out of 46 to none.
  """
  document = json.loads(to_holon(model))
  for graph in document["@graph"]:
    for node in graph["@graph"]:
      if node.get("internalId") == "us-gaap:Assets":
        node.pop("terseLabel", None)
  got = from_holon_json(json.dumps(document))
  assets = got.concepts["us-gaap:Assets"]
  assert (TERSE, "Total assets") in [(lab.role, lab.value) for lab in assets.labels]


def test_holon_collapses_duplicate_facts(model: XbrlModel) -> None:
  """A holon addresses a fact by its id, and the parse derives that id from the
  fact's content (Arelle's MD5), so a filing's duplicate facts are one node.

  Tavi names each fact positionally instead and keeps both. This is the one
  place the two serializations disagree about how many facts a filing has, and
  it is the emitters' difference rather than the importers'.
  """
  before = len(model.facts)
  model.facts.append(model.facts[0].model_copy())
  assert len(from_tavi_json(to_tavi(model)).facts) == before + 1
  assert len(from_holon_json(to_holon(model)).facts) == before


def test_holon_round_trips_the_whole_model(model: XbrlModel) -> None:
  """The gate: model -> holon -> model, compared field by field.

  Everything the model carries survives — facts with their values, decimals,
  dimensions, nil flag and language; every label with its role and language;
  all three network kinds with order, weight, preferred label and roots; the
  concept fields; and the units and periods with their content-derived ids.
  """
  got = from_holon_json(to_holon(model))
  assert got.entity.model_dump() == model.entity.model_dump()
  assert [p.model_dump() for p in got.periods] == [
    p.model_dump() for p in model.periods
  ]
  assert {(u.measure, u.uri) for u in got.units} == {
    (u.measure, u.uri) for u in model.units
  }
  assert {q: c.model_dump() for q, c in got.concepts.items()} == {
    q: c.model_dump() for q, c in model.concepts.items()
  }
  # A fact's own entity is not written per fact by either serialization — the
  # report has one entity and every fact is read as carrying it.
  skip = {"id", "entity_scheme", "entity_identifier"}
  assert sorted(str(f.model_dump(exclude=skip)) for f in got.facts) == sorted(
    str(f.model_dump(exclude=skip)) for f in model.facts
  )
  assert sorted(str(n.model_dump()) for n in got.networks) == sorted(
    str(n.model_dump()) for n in model.networks
  )
  # The filing identity, all but the one thing no serialization records.
  assert got.filing.model_dump(
    exclude={"is_inline_xbrl", "taxonomy_namespaces"}
  ) == model.filing.model_dump(exclude={"is_inline_xbrl", "taxonomy_namespaces"})


def test_a_holon_keeps_the_value_as_the_filing_wrote_it(model: XbrlModel) -> None:
  """A rate stated to four places is not the same statement as the float.

  The holon wrote `Decimal(str(0.05))` and lost the precision the filer chose;
  it writes the lexical value now, which is still a valid `xsd:decimal`.
  """
  model.facts[0].value_str = "1000.00"
  got = from_holon_json(to_holon(model))
  assets = next(f for f in got.facts if f.concept_qname == "us-gaap:Assets")
  assert assets.value_str == "1000.00"


def test_a_holon_tells_a_nil_fact_from_an_empty_one(model: XbrlModel) -> None:
  """Both were written with an empty value, so neither could be read back."""
  model.facts.append(
    XbrlFact(
      id="nil",
      concept_qname="us-gaap:Cash",
      period_id=model.periods[0].id,
      unit_id=unit_id(USD_URI),
      entity_cik="0001234567",
      is_nil=True,
    )
  )
  got = from_holon_json(to_holon(model))
  nil = [f for f in got.facts if f.is_nil]
  assert len(nil) == 1
  assert nil[0].concept_qname == "us-gaap:Cash"
  assert nil[0].value_str is None


def test_a_report_that_is_not_an_sec_filing_keeps_its_identity(
  model: XbrlModel,
) -> None:
  """A ledger's own report identifies its entity under its own scheme.

  Both importers used to lose half of that: Tavi looked for the entity's label
  under a `cik:` name it had reconstructed rather than the SQName the document
  wrote, and the holon called any entity an SEC CIK because that is the model's
  default. Found on a real RoboLedger report, whose two files disagreed about
  the same company.
  """
  model.entity = EntityIdentity(
    cik="entity_kg19ed34f81c37ba3f31fa",
    scheme=ENTITY_SCHEME,
    name="Harbinger Consultants LLC",
  )
  for got in (from_tavi_json(to_tavi(model)), from_holon_json(to_holon(model))):
    assert got.entity.name == "Harbinger Consultants LLC"
    assert got.entity.cik == "entity_kg19ed34f81c37ba3f31fa"
    assert got.entity.scheme == ENTITY_SCHEME


def test_a_holon_with_no_scheme_does_not_invent_an_sec_one(model: XbrlModel) -> None:
  """A holon written from a `StatementBundle` carries no scheme at all — the
  bundle has no field for one — so the identifier has to answer for it."""
  model.entity = EntityIdentity(cik="entity_kg1", scheme=ENTITY_SCHEME, name="Acme")
  document = json.loads(to_holon(model))
  for graph in document["@graph"]:
    for node in graph["@graph"]:
      if "Entity" in str(node.get("@type")):
        node.pop("scheme", None)
  got = from_holon_json(json.dumps(document))
  assert got.entity.scheme == ENTITY_SCHEME
  # A ten-digit identifier still reads as what it is.
  assert _scheme_for("0001234567") == CIK_SCHEME


# -- the two agree ---------------------------------------------------------------


def test_both_importers_answer_the_same(model: XbrlModel, tmp_path: Path) -> None:
  """A Tavi and a holon of one filing are two descriptions of one report, so
  every tool has to answer the same over either.

  This is the property the exercise turns on: the tool set is written against
  the model, so if the two importers agree, one tool set serves every
  serialization. Run over a real 10-K it holds across all 152 of its
  presentation networks, labels included.
  """
  tavi = tmp_path / "acme.tavi.json"
  tavi.write_text(to_tavi(model))
  holon = tmp_path / "acme.holon.jsonld"
  holon.write_text(to_holon(model))
  session = FilingSession()
  try:
    answers = []
    for path in (tavi, holon):
      loaded = session.load(str(path))
      answers.append(
        (
          tools.fact_grid(loaded, ["us-gaap:Assets", "us-gaap:Cash"])["rows"],
          tools.statement(loaded, BALANCE_SHEET)["rows"],
          tools.calculation(loaded, "us-gaap:Assets")["networks"],
          tools.resolve_element(loaded, "assets")["matches"],
        )
      )
    assert answers[0] == answers[1]
  finally:
    session.close()


# -- the server reads them -------------------------------------------------------


def test_the_session_loads_a_tavi_and_a_holon(model: XbrlModel, tmp_path: Path) -> None:
  tavi = tmp_path / "acme.tavi.json"
  tavi.write_text(to_tavi(model))
  holon = tmp_path / "acme.holon.jsonld"
  holon.write_text(to_holon(model))
  session = FilingSession()
  try:
    for path in (tavi, holon):
      loaded = session.load(str(path))
      assert loaded.has_xbrl is True
      assert loaded.has_document is False
      described = tools.describe_filing(loaded)
      assert described["filing"]["accession"] == model.filing.accession
      assert described["filing"]["form"] == "10-K"
      rows = tools.fact_grid(loaded, ["us-gaap:Assets"])["rows"]
      assert [row["value"] for row in rows] == [1000.0]
      rendered = tools.statement(loaded, BALANCE_SHEET)["rows"]
      assert [row["label"] for row in rendered] == [
        "Assets [Abstract]",
        "Total assets",
        "Cash",
      ]
  finally:
    session.close()


def test_the_session_loads_a_json_report_from_a_url(
  model: XbrlModel, tmp_path: Path
) -> None:
  """A report published as an artifact opens by its URL.

  Arelle fetches its own documents, which is why every other URL goes through
  it — but it cannot load either JSON report, so those are fetched here and
  read into the model instead.
  """
  (tmp_path / "acme.holon.jsonld").write_text(to_holon(model))
  handler = functools.partial(
    http.server.SimpleHTTPRequestHandler, directory=str(tmp_path)
  )
  with socketserver.TCPServer(("127.0.0.1", 0), handler) as httpd:
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    session = FilingSession()
    try:
      loaded = session.load(f"http://127.0.0.1:{port}/acme.holon.jsonld")
      assert loaded.has_xbrl is True
      assert tools.fact_grid(loaded, ["us-gaap:Assets"])["rows"][0]["value"] == 1000.0
    finally:
      session.close()
      httpd.shutdown()


def test_the_session_still_refuses_an_oim_report(tmp_path: Path) -> None:
  """xBRL-JSON is Arelle's own serialization and wants its loader, not an
  importer of ours — so it is named rather than half-read."""
  path = tmp_path / "x.oim.json"
  path.write_text(
    '{"documentInfo": {"documentType": "https://xbrl.org/2021/xbrl-json"}}'
  )
  session = FilingSession()
  try:
    with pytest.raises(SourceError, match="xBRL-JSON"):
      session.load(str(path))
  finally:
    session.close()


# -- reading badly-formed input ---------------------------------------------------


def test_a_tavi_that_is_not_one_is_refused() -> None:
  with pytest.raises(TaviError):
    from_tavi_json("not json")
  with pytest.raises(TaviError):
    from_tavi_json('{"documentInfo": {}}')


def test_a_holon_that_is_not_one_is_refused() -> None:
  with pytest.raises(HolonError):
    from_holon_json("[")
  with pytest.raises(HolonError):
    from_holon_json('{"@context": {}, "@graph": []}')


def test_period_literals_read_both_ways() -> None:
  """Tavi writes an exclusive-end dateTime; earlier emitters wrote inclusive
  dates. Both land on the same period."""
  exclusive = period_from_interval("2024-01-01T00:00:00/2025-01-01T00:00:00")
  inclusive = period_from_interval("2024-01-01/2024-12-31")
  assert exclusive is not None and inclusive is not None
  assert exclusive.id == inclusive.id
  assert exclusive.end == date(2024, 12, 31)
  instant = period_from_interval("2025-01-01T00:00:00")
  assert instant is not None and instant.end == date(2024, 12, 31)
  forever = period_from_interval("0001-01-01T00:00:00/9999-12-31T00:00:00")
  assert forever is not None and forever.period_type == "forever"
  assert period_from_interval("") is None


def test_a_tavi_without_a_report_namespace_still_reads() -> None:
  """A compiled model from another producer has no `rpt` namespace of ours;
  everything but the accession still reads."""
  document = json.loads(to_tavi(_model()))
  document["documentInfo"]["namespaces"].pop("rpt")
  got = from_tavi_json(json.dumps(document))
  assert got.filing.accession == "unknown"
  assert got.entity.cik == "0001234567"
  assert len(got.facts) == 5
