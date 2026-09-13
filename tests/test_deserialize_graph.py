"""Tests for the property-graph importer (``deserialize/graph.py``).

The property that matters is the round trip: a model projected into the
graph's tables by ``serialize/lpg.py`` and read back is the same model,
except where the graph has no column — and each of those is declared in the
gap report rather than filled in. The second property is that the reader
keys on the schema's columns, not on one producer's habits: the rows a
ledger's materialization writes read as the producer's report.
"""

from __future__ import annotations

import os
from collections import Counter
from datetime import date
from pathlib import Path

import pytest

from xbrlkit.deserialize import (
  GraphError,
  from_graph,
  from_graph_report,
  from_holon_json,
  read_lbug,
)
from xbrlkit.deserialize.graph import (
  ELEMENT_QUERIES,
  SLICE_QUERIES,
  tables_from_slice,
)
from xbrlkit.information_block import plan_blocks
from xbrlkit.model import (
  Arc,
  Concept,
  DimQualifier,
  EntityIdentity,
  FilingMeta,
  Label,
  Network,
  Reference,
  Unit,
  XbrlFact,
  XbrlModel,
)
from xbrlkit.parse.ids import unit_id
from xbrlkit.periods import duration_period, forever_period, instant_period
from xbrlkit.serialize.lpg import GraphTables, build_lbug, to_graph_tables
from xbrlkit.serve import tools
from xbrlkit.serve.session import FilingSession

US_GAAP = "http://fasb.org/us-gaap/2024"
DEI = "http://xbrl.sec.gov/dei/2024"
ACME = "http://www.acme.example/20241231"
XBRLI = "http://www.xbrl.org/2003/instance"
XBRLDT = "http://xbrl.org/2005/xbrldt"
DTR_NUMERIC = "http://www.xbrl.org/dtr/type/numeric"
DTR_2022 = "http://www.xbrl.org/dtr/type/2022-03-31"
STANDARD = "http://www.xbrl.org/2003/role/label"
TERSE = "http://www.xbrl.org/2003/role/terseLabel"
DOCUMENTATION = "http://www.xbrl.org/2003/role/documentation"
PARENT_CHILD = "http://www.xbrl.org/2003/arcrole/parent-child"
SUMMATION = "http://www.xbrl.org/2003/arcrole/summation-item"
DIM = "http://xbrl.org/int/dim/arcrole"
BALANCE_SHEET = "http://www.acme.example/role/BalanceSheet"
SEGMENTS = "http://www.acme.example/role/SegmentsDetails"
REPORT_URI = (
  "https://www.sec.gov/Archives/edgar/data/12345/000001234525000001/acme-20241231.htm"
)
USD_URI = "http://www.xbrl.org/2003/iso4217#USD"
SHARES_URI = "http://www.xbrl.org/2003/instance#shares"
CIK_SCHEME = "http://www.sec.gov/CIK"


def _monetary(
  qname: str, namespace: str, period_type: str, balance: str, **kw
) -> Concept:
  local = qname.split(":", 1)[1]
  return Concept(
    qname=qname,
    namespace=namespace,
    name=local,
    period_type=period_type,  # type: ignore[arg-type]
    balance=balance,  # type: ignore[arg-type]
    is_numeric=True,
    item_type="monetaryItemType",
    item_type_qname="xbrli:monetaryItemType",
    item_type_namespace=XBRLI,
    substitution_group="xbrli:item",
    substitution_group_namespace=XBRLI,
    nice_type="Monetary",
    pref_label=kw.pop("pref_label", local),
    labels=kw.pop("labels", [Label(value=local, role=STANDARD, language="en-US")]),
    **kw,
  )


def _model() -> XbrlModel:
  """A filing shaped like a parse, for a fictional filer: real period ids,
  labels by role, a segment, a typed axis, a subsidiary's context, a
  duplicate fact, a nil fact, a text block, and all three linkbases."""
  instant = instant_period(date(2024, 12, 31))
  year = duration_period(date(2024, 1, 1), date(2024, 12, 31))
  forever = forever_period()
  usd = Unit(id=unit_id(USD_URI), measure="iso4217:USD", uri=USD_URI)
  per_share = Unit(
    id=unit_id(f"{USD_URI}/{SHARES_URI}"),
    measure="iso4217:USD/xbrli:shares",
    uri=f"{USD_URI}/{SHARES_URI}",
    numerator_uri=USD_URI,
    denominator_uri=SHARES_URI,
  )
  concepts = {
    "us-gaap:AssetsAbstract": Concept(
      qname="us-gaap:AssetsAbstract",
      namespace=US_GAAP,
      name="AssetsAbstract",
      is_abstract=True,
      nice_type="String",
      pref_label="Assets [Abstract]",
      labels=[Label(value="Assets [Abstract]", role=STANDARD, language="en-US")],
    ),
    "us-gaap:Assets": _monetary(
      "us-gaap:Assets",
      US_GAAP,
      "instant",
      "debit",
      labels=[
        Label(value="Assets", role=STANDARD, language="en-US"),
        Label(value="Total assets", role=TERSE, language="en-US"),
        Label(
          value="Sum of the carrying amounts…", role=DOCUMENTATION, language="en-US"
        ),
      ],
      references=[
        Reference(value="Topic 210", role="http://www.xbrl.org/2003/role/reference")
      ],
    ),
    "us-gaap:Cash": _monetary("us-gaap:Cash", US_GAAP, "instant", "debit"),
    "us-gaap:Revenues": _monetary("us-gaap:Revenues", US_GAAP, "duration", "credit"),
    "us-gaap:EarningsPerShareBasic": Concept(
      qname="us-gaap:EarningsPerShareBasic",
      namespace=US_GAAP,
      name="EarningsPerShareBasic",
      period_type="duration",
      is_numeric=True,
      item_type="perShareItemType",
      # A prefix the filing's own concepts never bind: the namespace
      # survives the graph, the QName does not (declared).
      item_type_qname="num:perShareItemType",
      item_type_namespace=DTR_NUMERIC,
      substitution_group="xbrli:item",
      substitution_group_namespace=XBRLI,
      nice_type="PerShare",
      pref_label="Earnings per share, basic",
      labels=[
        Label(value="Earnings per share, basic", role=STANDARD, language="en-US")
      ],
    ),
    "us-gaap:GoodwillDisclosureTextBlock": Concept(
      qname="us-gaap:GoodwillDisclosureTextBlock",
      namespace=US_GAAP,
      name="GoodwillDisclosureTextBlock",
      period_type="duration",
      is_textblock=True,
      is_text_fact=True,
      item_type="textBlockItemType",
      item_type_namespace=DTR_2022,
      nice_type="TextBlock",
      pref_label="Goodwill",
      labels=[Label(value="Goodwill", role=STANDARD, language="en-US")],
    ),
    "dei:DocumentType": Concept(
      qname="dei:DocumentType",
      namespace=DEI,
      name="DocumentType",
      period_type="duration",
      item_type="submissionTypeItemType",
      item_type_qname="dei:submissionTypeItemType",
      item_type_namespace=DEI,
      nice_type="String",
      # Nothing in the graph says a string concept is a text fact (declared).
      is_text_fact=True,
      pref_label="Document Type",
      labels=[Label(value="Document Type", role=STANDARD, language="en-US")],
    ),
    "us-gaap:SegmentTable": Concept(
      qname="us-gaap:SegmentTable",
      namespace=US_GAAP,
      name="SegmentTable",
      is_abstract=True,
      is_hypercube_item=True,
      substitution_group="xbrldt:hypercubeItem",
      substitution_group_namespace=XBRLDT,
      nice_type="Table",
    ),
    "us-gaap:StatementBusinessSegmentsAxis": Concept(
      qname="us-gaap:StatementBusinessSegmentsAxis",
      namespace=US_GAAP,
      name="StatementBusinessSegmentsAxis",
      is_abstract=True,
      is_dimension_item=True,
      substitution_group="xbrldt:dimensionItem",
      substitution_group_namespace=XBRLDT,
      nice_type="Axis",
    ),
    "us-gaap:SegmentDomain": Concept(
      qname="us-gaap:SegmentDomain",
      namespace=US_GAAP,
      name="SegmentDomain",
      is_abstract=True,
      is_domain_member=True,
      nice_type="Domain",
    ),
    "acme:WidgetsMember": Concept(
      qname="acme:WidgetsMember",
      namespace=ACME,
      name="WidgetsMember",
      is_domain_member=True,
      nice_type="Domain",
      pref_label="Widgets",
      labels=[Label(value="Widgets", role=STANDARD, language="en-US")],
    ),
    "acme:ContractAxis": Concept(
      qname="acme:ContractAxis",
      namespace=ACME,
      name="ContractAxis",
      is_abstract=True,
      is_dimension_item=True,
      nice_type="Axis",
    ),
  }
  filer = {
    "entity_cik": "0000012345",
    "entity_scheme": CIK_SCHEME,
    "entity_identifier": "0000012345",
  }
  facts = [
    XbrlFact(
      id="f1",
      concept_qname="us-gaap:Assets",
      period_id=instant.id,
      unit_id=usd.id,
      value_str="1000",
      numeric_value=1000.0,
      decimals="-3",
      source_hash="a1",
      **filer,
    ),
    XbrlFact(
      id="f2",
      concept_qname="us-gaap:Cash",
      period_id=instant.id,
      unit_id=usd.id,
      value_str="400",
      numeric_value=400.0,
      decimals="-3",
      source_hash="a2",
      dims=[
        DimQualifier(
          axis_qname="us-gaap:StatementBusinessSegmentsAxis",
          member_qname="acme:WidgetsMember",
          axis_type="segment",
        )
      ],
      **filer,
    ),
    XbrlFact(
      id="f3",
      concept_qname="dei:DocumentType",
      period_id=year.id,
      value_str="10-K",
      value_kind="text",
      language="en-us",
      source_hash="a3",
      **filer,
    ),
    XbrlFact(
      id="f4",
      concept_qname="us-gaap:GoodwillDisclosureTextBlock",
      period_id=year.id,
      value_str="<p>Goodwill note</p>",
      value_kind="text",
      source_hash="a4",
      **filer,
    ),
    # A subsidiary's context, and the same fact tagged twice (one hash).
    XbrlFact(
      id="f5",
      concept_qname="us-gaap:Assets",
      period_id=instant.id,
      unit_id=usd.id,
      entity_cik="0000000042",
      entity_scheme=CIK_SCHEME,
      entity_identifier="42",
      value_str="7",
      numeric_value=7.0,
      decimals="0",
      source_hash="a5",
    ),
    XbrlFact(
      id="f6",
      concept_qname="us-gaap:Assets",
      period_id=instant.id,
      unit_id=usd.id,
      entity_cik="0000000042",
      entity_scheme=CIK_SCHEME,
      entity_identifier="42",
      value_str="7",
      numeric_value=7.0,
      decimals="0",
      source_hash="a5",
    ),
    XbrlFact(
      id="f7",
      concept_qname="us-gaap:Revenues",
      period_id=forever.id,
      unit_id=usd.id,
      value_str="1",
      numeric_value=1.0,
      decimals="0",
      source_hash="a7",
      dims=[
        DimQualifier(
          axis_qname="acme:ContractAxis",
          typed_value="C-1",
          is_explicit=False,
          axis_type="scenario",
        )
      ],
      **filer,
    ),
    XbrlFact(
      id="f8",
      concept_qname="us-gaap:Revenues",
      period_id=year.id,
      unit_id=usd.id,
      value_str=None,
      is_nil=True,
      source_hash="a8",
      **filer,
    ),
    XbrlFact(
      id="f9",
      concept_qname="us-gaap:EarningsPerShareBasic",
      period_id=year.id,
      unit_id=per_share.id,
      value_str="2.5",
      numeric_value=2.5,
      decimals="2",
      source_hash="a9",
      **filer,
    ),
  ]
  networks = [
    Network(
      role_uri=BALANCE_SHEET,
      definition="0000002 - Statement - Balance Sheet",
      kind="presentation",
      role_id="BalanceSheet",
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
      role_uri=BALANCE_SHEET,
      definition="0000002 - Statement - Balance Sheet",
      kind="calculation",
      role_id="BalanceSheet",
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
    Network(
      role_uri=SEGMENTS,
      definition="0000003 - Disclosure - Segments (Details)",
      kind="presentation",
      role_id="SegmentsDetails",
      arcs=[
        Arc(
          from_qname="us-gaap:AssetsAbstract",
          to_qname="us-gaap:Cash",
          arcrole=PARENT_CHILD,
          order=1.0,
          is_root=True,
        )
      ],
    ),
    Network(
      role_uri=SEGMENTS,
      definition="0000003 - Disclosure - Segments (Details)",
      kind="definition",
      role_id="SegmentsDetails",
      arcs=[
        Arc(
          from_qname="us-gaap:AssetsAbstract",
          to_qname="us-gaap:SegmentTable",
          arcrole=f"{DIM}/all",
          order=1.0,
          is_root=True,
        ),
        Arc(
          from_qname="us-gaap:SegmentTable",
          to_qname="us-gaap:StatementBusinessSegmentsAxis",
          arcrole=f"{DIM}/hypercube-dimension",
          order=1.0,
        ),
        Arc(
          from_qname="us-gaap:StatementBusinessSegmentsAxis",
          to_qname="us-gaap:SegmentDomain",
          arcrole=f"{DIM}/dimension-domain",
          order=1.0,
        ),
        Arc(
          from_qname="us-gaap:SegmentDomain",
          to_qname="acme:WidgetsMember",
          arcrole=f"{DIM}/domain-member",
          order=1.0,
          # The graph has no column for this (declared).
          target_role="http://www.acme.example/role/Members",
        ),
        Arc(
          from_qname="us-gaap:StatementBusinessSegmentsAxis",
          to_qname="us-gaap:SegmentDomain",
          arcrole=f"{DIM}/dimension-default",
          order=1.0,
        ),
      ],
    ),
  ]
  return XbrlModel(
    filing=FilingMeta(
      accession="0000012345-25-000001",
      cik="0000012345",
      form="10-K",
      filing_date=date(2025, 2, 5),
      fiscal_year_focus="2024",
      fiscal_period_focus="FY",
      fiscal_year_end_month="12",
      report_date=date(2024, 12, 31),
      acceptance_datetime="2025-02-05T16:03:20.000Z",
      is_inline_xbrl=True,
      primary_document="acme-20241231.htm",
      report_uri=REPORT_URI,
      extension_namespace=ACME,
      taxonomy_namespaces=sorted({US_GAAP, DEI, ACME}),
    ),
    entity=EntityIdentity(
      cik="0000012345",
      name="Acme Industrial Corp",
      legal_name="Acme Industrial Corp",
      ein="123456789",
      ticker="ACME",
      exchange="NYSE",
      sic="3841",
      sic_description="Surgical & Medical Instruments & Apparatus",
      category="Large accelerated filer",
      state_of_incorporation="DE",
      fiscal_year_end="1231",
      entity_type="operating",
      website="https://www.acme.example",
      phone="5555550100",
    ),
    concepts=concepts,
    periods=[instant, year, forever],
    units=[usd, per_share],
    facts=facts,
    networks=networks,
  )


@pytest.fixture
def model() -> XbrlModel:
  return _model()


@pytest.fixture
def through(model: XbrlModel) -> XbrlModel:
  return from_graph(to_graph_tables(model))


def _network_shapes(model: XbrlModel) -> list[tuple]:
  """Networks as comparable tuples, arcs sorted, ``target_role`` aside."""
  return sorted(
    (
      n.role_uri,
      n.definition,
      n.kind,
      n.role_id,
      n.block_type,
      n.structure_id,
      n.fact_set_id,
      tuple(
        sorted(
          (
            a.from_qname,
            a.to_qname,
            a.arcrole,
            a.order,
            a.weight,
            a.preferred_label,
            a.is_root,
          )
          for a in n.arcs
        )
      ),
    )
    for n in model.networks
  )


# What a fact loses through the graph: its id becomes the hash the projection
# scoped its URI on, and the graph has no column for language or a second
# lexical form of the value.
LOST_ON_FACTS = frozenset({"id", "raw_value", "language"})


def _fact_shapes(model: XbrlModel, skip: frozenset[str] = LOST_ON_FACTS) -> list[str]:
  return sorted(str(f.model_dump(exclude=set(skip))) for f in model.facts)


# -- what survives ---------------------------------------------------------------


@pytest.mark.unit
class TestRoundTrip:
  def test_keeps_the_filing(self, model: XbrlModel, through: XbrlModel) -> None:
    got, want = through.filing, model.filing
    assert got.accession == want.accession
    assert got.cik == want.cik
    assert got.form == want.form
    assert got.filing_date == want.filing_date
    assert got.report_date == want.report_date
    assert (
      got.fiscal_year_focus,
      got.fiscal_period_focus,
      got.fiscal_year_end_month,
    ) == (
      "2024",
      "FY",
      "12",
    )
    assert got.report_uri == REPORT_URI
    assert got.extension_namespace == ACME
    assert got.taxonomy_namespaces == sorted({US_GAAP, DEI, ACME})
    # The graph says whether the filing was inline XBRL — the one thing the
    # TAVI and holon readers cannot.
    assert got.is_inline_xbrl is True
    # The graph keeps the acceptance date, not the time.
    assert got.acceptance_datetime == "2025-02-05"
    assert got.primary_document is None

  def test_keeps_the_entity(self, model: XbrlModel, through: XbrlModel) -> None:
    assert through.entity.model_dump() == model.entity.model_dump()

  def test_keeps_periods_and_their_ids(
    self, model: XbrlModel, through: XbrlModel
  ) -> None:
    assert [p.model_dump() for p in through.periods] == [
      p.model_dump() for p in model.periods
    ]

  def test_keeps_units_and_their_ids(
    self, model: XbrlModel, through: XbrlModel
  ) -> None:
    assert sorted(u.model_dump().items() for u in through.units) == sorted(
      u.model_dump().items() for u in model.units
    )

  def test_keeps_the_concepts(self, model: XbrlModel, through: XbrlModel) -> None:
    assert set(through.concepts) == set(model.concepts)
    for qname, want in model.concepts.items():
      got = through.concepts[qname]
      skip = {"is_text_fact"}
      if qname == "us-gaap:EarningsPerShareBasic":
        # `num:` is bound by no concept of the filing: the namespace and the
        # local name come back, the QName does not.
        skip.add("item_type_qname")
        assert got.item_type_qname is None
        assert got.item_type == "perShareItemType"
        assert got.item_type_namespace == DTR_NUMERIC
      assert got.model_dump(exclude=skip) == want.model_dump(exclude=skip), qname
    assert through.concepts["us-gaap:GoodwillDisclosureTextBlock"].is_text_fact is True
    assert through.concepts["dei:DocumentType"].is_text_fact is False

  def test_keeps_the_facts(self, model: XbrlModel, through: XbrlModel) -> None:
    # The duplicate collapsed on its hash, as the projection collapses it.
    assert len(through.facts) == len(model.facts) - 1
    want = _fact_shapes(model)
    want.remove(str(model.facts[5].model_dump(exclude=set(LOST_ON_FACTS))))
    assert _fact_shapes(through) == want
    by_id = {f.id: f for f in through.facts}
    # A fact's id is the hash the projection scoped its URI on.
    assert set(by_id) == {"a1", "a2", "a3", "a4", "a5", "a7", "a8", "a9"}
    assert by_id["a8"].is_nil is True and by_id["a8"].value_str is None
    assert (by_id["a5"].entity_cik, by_id["a5"].entity_identifier) == (
      "0000000042",
      "42",
    )
    typed = by_id["a7"].dims[0]
    assert (
      typed.axis_qname,
      typed.typed_value,
      typed.is_explicit,
      typed.axis_type,
    ) == (
      "acme:ContractAxis",
      "C-1",
      False,
      "scenario",
    )
    assert by_id["a4"].value_str == "<p>Goodwill note</p>"
    # The graph keeps one value; it comes back as both forms.
    assert by_id["a1"].raw_value == by_id["a1"].value_str == "1000"

  def test_keeps_the_networks(self, model: XbrlModel, through: XbrlModel) -> None:
    assert _network_shapes(through) == _network_shapes(model)
    read = next(n for n in through.networks if n.kind == "definition")
    assert all(a.target_role is None for a in read.arcs)
    assert any(a.target_role for n in model.networks for a in n.arcs)

  def test_is_a_fixed_point(self, through: XbrlModel) -> None:
    """Reading, projecting and reading again changes nothing."""
    again = from_graph(to_graph_tables(through))
    assert again.filing.model_dump() == through.filing.model_dump()
    assert again.entity.model_dump() == through.entity.model_dump()
    assert {q: c.model_dump() for q, c in again.concepts.items()} == {
      q: c.model_dump() for q, c in through.concepts.items()
    }
    assert _fact_shapes(again, frozenset()) == _fact_shapes(through, frozenset())
    assert _network_shapes(again) == _network_shapes(through)

  def test_gaps_are_declared(self, model: XbrlModel) -> None:
    _, gaps = from_graph_report(to_graph_tables(model))
    report = gaps.to_dict()
    assert any("targetRole" in item for item in report["missing"])
    assert any("language" in item for item in report["missing"])
    assert report["unresolved_references"] == 0
    assert report["external_values"] == 0
    assert report["associations_skipped"] == {}


# -- the rows, however they arrive --------------------------------------------------


@pytest.mark.unit
class TestRows:
  def test_refuses_rows_that_are_not_one_report(self, model: XbrlModel) -> None:
    with pytest.raises(GraphError, match="no Report row"):
      from_graph(GraphTables())
    tables = to_graph_tables(model)
    tables.nodes["Report"].append({**tables.nodes["Report"][0], "identifier": "other"})
    with pytest.raises(GraphError, match="2 Report rows"):
      from_graph(tables)

  def test_relationship_rows_read_src_and_dst(
    self, model: XbrlModel, through: XbrlModel
  ) -> None:
    """The platform's parquet names the ends ``src`` / ``dst``."""
    tables = to_graph_tables(model)
    flat: dict[str, list[dict]] = dict(tables.nodes)
    for name, rows in tables.relationships.items():
      flat[name] = [{"src": r["from"], "dst": r["to"]} for r in rows]
    got = from_graph(flat)
    assert _fact_shapes(got) == _fact_shapes(through)
    assert _network_shapes(got) == _network_shapes(through)

  def test_root_reads_however_the_store_spelled_it(self, model: XbrlModel) -> None:
    """LadybugDB stringifies the ``root`` column's booleans on load."""
    tables = to_graph_tables(model)
    for row in tables.nodes["Association"]:
      row["root"] = "True" if row["root"] else "False"
    got = from_graph(tables)
    balance = next(
      n
      for n in got.networks
      if n.role_uri == BALANCE_SHEET and n.kind == "presentation"
    )
    assert [a.is_root for a in balance.arcs] == [True, False]

  def test_an_external_text_block_is_counted_not_read(self, model: XbrlModel) -> None:
    """The platform stores a text block's value as a CDN URL."""
    tables = to_graph_tables(model)
    row = next(r for r in tables.nodes["Fact"] if r["uri"].endswith("#fact-a4"))
    row["value"] = "https://cdn.example/text/a4.html"
    row["value_type"] = "external"
    row["content_type"] = "text/html"
    got, gaps = from_graph_report(tables)
    fact = next(f for f in got.facts if f.id == "a4")
    assert fact.value_str == "https://cdn.example/text/a4.html"
    assert fact.content_type == "text/html"
    assert gaps.external_values == 1

  def test_a_ledgers_rows_read_as_the_producers_report(self) -> None:
    """A tenant's materialization: no role URIs, no labels, a block type in
    ``type``, the platform's own association kinds beside the XBRL ones,
    facts in a fact set under the structure, short arcroles."""
    rows: dict[str, list[dict]] = {
      "Report": [
        {
          "identifier": "rep1",
          "uri": None,
          "name": "FY2025 Financials",
          "accession_number": None,
          "form": None,
          "filing_date": "2026-01-15",
          "report_date": "2025-12-31",
          "is_inline_xbrl": False,
          "fiscal_year_focus": 0,
          "fiscal_year_end_month": 0,
        }
      ],
      "Entity": [
        {
          "identifier": "ent1",
          "uri": None,
          "scheme": None,
          "cik": None,
          "name": "Acme LLC",
        }
      ],
      "Taxonomy": [
        {"identifier": "tax1", "uri": "https://robosystems.ai/taxonomy/rs-gaap/v1/"}
      ],
      "Structure": [
        {
          "identifier": "st1",
          "uri": None,
          "network_uri": None,
          "definition": None,
          "type": "balance_sheet",
          "name": "Balance Sheet",
        },
        {
          "identifier": "coa",
          "uri": None,
          "network_uri": None,
          "definition": "Chart of Accounts",
          "type": "ChartOfAccounts",
          "name": "Chart of Accounts",
        },
      ],
      "Element": [
        {
          "identifier": "e1",
          "uri": None,
          "qname": "rs-gaap:Assets",
          "name": "Assets",
          "period_type": "instant",
          "balance": "debit",
          "is_numeric": True,
        },
        {
          "identifier": "e2",
          "uri": None,
          "qname": "rl:1000",
          "name": "Cash and equivalents",
          "period_type": "instant",
          "balance": "debit",
          "is_numeric": True,
        },
      ],
      "Association": [
        {
          "identifier": "a1",
          "arcrole": "parent-child",
          "association_type": "presentation",
          "order_value": 1.0,
          "weight": 0.0,
          "root": None,
          "preferred_label": "",
        },
        {
          "identifier": "a2",
          "arcrole": "http://www.xbrl.org/2003/arcrole/summation-item",
          "association_type": "calculation",
          "order_value": 1.0,
          "weight": 1.0,
          "root": None,
          "preferred_label": "",
        },
        {
          "identifier": "a3",
          "arcrole": "parent-child",
          "association_type": "mapping",
          "order_value": 1.0,
          "weight": 0.0,
          "root": None,
          "preferred_label": "",
        },
      ],
      "FactSet": [{"identifier": "fs1", "factset_type": "", "provenance": ""}],
      "Fact": [
        {
          "identifier": "fact1",
          "uri": None,
          "value": "1500",
          "numeric_value": 1500.0,
          "fact_type": "Numeric",
          "decimals": "-2",
          "value_type": "inline",
          "content_type": None,
          "has_dimensions": False,
          "dimension_count": 0,
        }
      ],
      "Period": [
        {
          "identifier": "per1",
          "uri": None,
          "start_date": None,
          "end_date": "2025-12-31",
          "period_type": "instant",
          "duration_type": None,
        }
      ],
      "Unit": [
        {
          "identifier": "unit_usd",
          "uri": "iso4217:USD",
          "measure": "iso4217:USD",
          "value": "USD",
          "numerator_uri": None,
          "denominator_uri": None,
        }
      ],
      "ENTITY_HAS_REPORT": [{"src": "ent1", "dst": "rep1"}],
      "REPORT_USES_TAXONOMY": [{"src": "rep1", "dst": "tax1"}],
      "STRUCTURE_HAS_TAXONOMY": [{"src": "st1", "dst": "tax1"}],
      "STRUCTURE_HAS_ASSOCIATION": [
        {"src": "st1", "dst": "a1"},
        {"src": "st1", "dst": "a2"},
        {"src": "coa", "dst": "a3"},
      ],
      "ASSOCIATION_HAS_FROM_ELEMENT": [
        {"src": "a1", "dst": "e1"},
        {"src": "a2", "dst": "e1"},
        {"src": "a3", "dst": "e1"},
      ],
      "ASSOCIATION_HAS_TO_ELEMENT": [
        {"src": "a1", "dst": "e2"},
        {"src": "a2", "dst": "e2"},
        {"src": "a3", "dst": "e2"},
      ],
      "REPORT_HAS_FACT_SET": [{"src": "rep1", "dst": "fs1"}],
      "STRUCTURE_HAS_FACT_SET": [{"src": "st1", "dst": "fs1"}],
      "FACT_SET_CONTAINS_FACT": [{"src": "fs1", "dst": "fact1"}],
      "REPORT_HAS_FACT": [{"src": "rep1", "dst": "fact1"}],
      "FACT_HAS_ELEMENT": [{"src": "fact1", "dst": "e2"}],
      "FACT_HAS_PERIOD": [{"src": "fact1", "dst": "per1"}],
      "FACT_HAS_UNIT": [{"src": "fact1", "dst": "unit_usd"}],
      "FACT_HAS_ENTITY": [{"src": "fact1", "dst": "ent1"}],
    }
    got, gaps = from_graph_report(rows)

    # The report's identity is its own id; nothing calls it an SEC filing.
    assert got.filing.accession == "rep1"
    assert got.filing.form is None
    assert got.filing.is_inline_xbrl is False
    assert got.filing.fiscal_year_focus is None
    assert (
      got.filing.extension_namespace == "https://robosystems.ai/taxonomy/rs-gaap/v1/"
    )
    assert got.entity.cik == "ent1"
    assert got.entity.scheme == "http://robosystems.ai/entity"
    assert got.entity.name == "Acme LLC"

    # An element named beyond its local name carries that name as its label.
    cash = got.concepts["rl:1000"]
    assert cash.namespace == ""
    assert cash.name == "1000"
    assert [(label.value, label.role) for label in cash.labels] == [
      ("Cash and equivalents", STANDARD)
    ]
    assert cash.pref_label == "Cash and equivalents"
    assert got.concepts["rs-gaap:Assets"].labels == []

    # The producer's structure: its own id, block type and fact set; the
    # short arcrole read as the URI the model documents.
    kinds = {(n.kind, n.role_uri): n for n in got.networks}
    balance = kinds[("presentation", "st1")]
    assert (
      balance.definition,
      balance.block_type,
      balance.structure_id,
      balance.fact_set_id,
    ) == (
      "Balance Sheet",
      "balance_sheet",
      "st1",
      "fs1",
    )
    assert [(a.from_qname, a.to_qname, a.arcrole) for a in balance.arcs] == [
      ("rs-gaap:Assets", "rl:1000", PARENT_CHILD)
    ]
    assert [(a.weight, a.is_root) for a in kinds[("calculation", "st1")].arcs] == [
      (1.0, False)
    ]
    # A mapping arc is the producer's own kind, not a presentation network.
    assert ("presentation", "coa") not in kinds
    assert gaps.associations_skipped == {"mapping": 1}
    assert "labels (this graph holds none)" in gaps.missing

    # The fact is pinned to the structure through its fact set.
    [fact] = got.facts
    assert (fact.concept_qname, fact.value_str, fact.numeric_value, fact.decimals) == (
      "rl:1000",
      "1500",
      1500.0,
      "-2",
    )
    assert fact.structure_id == "st1"
    assert fact.entity_cik == "ent1"
    [period] = got.periods
    assert (period.period_type, period.end) == ("instant", date(2025, 12, 31))
    [unit] = got.units
    assert (unit.measure, unit.uri) == ("iso4217:USD", "iso4217:USD")


# -- the slice, from a database ------------------------------------------------------


@pytest.mark.unit
class TestSlice:
  def test_every_table_the_projection_writes_has_a_query(self) -> None:
    filled = {name for name, rows in to_graph_tables(_model()).nodes.items() if rows}
    asked = {q.table for q in SLICE_QUERIES} | {q.table for q in ELEMENT_QUERIES}
    assert filled <= asked

  def test_unfolds_edge_columns_into_relationship_rows(self) -> None:
    fact_query = next(q for q in SLICE_QUERIES if q.table == "Fact")
    rows = [
      {
        "f.identifier": "f1",
        "f.value": "1",
        "to__FACT_HAS_ELEMENT": "e1",
        "to__FACT_HAS_DIMENSION": "d1",
        "from__REPORT_HAS_FACT": "r1",
      },
      {
        "f.identifier": "f1",
        "f.value": "1",
        "to__FACT_HAS_ELEMENT": "e1",
        "to__FACT_HAS_DIMENSION": "d2",
        "from__REPORT_HAS_FACT": "r1",
      },
      {
        "f.identifier": "f2",
        "f.value": "2",
        "to__FACT_HAS_ELEMENT": None,
        "to__FACT_HAS_DIMENSION": None,
        "from__REPORT_HAS_FACT": "r1",
      },
    ]
    membership = next(q for q in SLICE_QUERIES if q.table == "FACT_SET_CONTAINS_FACT")
    tables = tables_from_slice(
      [(fact_query, rows), (membership, [{"src": "fs1", "dst": "f1"}])]
    )
    assert tables.nodes["Fact"] == [
      {"identifier": "f1", "value": "1"},
      {"identifier": "f2", "value": "2"},
    ]
    assert tables.relationships["FACT_HAS_ELEMENT"] == [{"from": "f1", "to": "e1"}]
    assert tables.relationships["FACT_HAS_DIMENSION"] == [
      {"from": "f1", "to": "d1"},
      {"from": "f1", "to": "d2"},
    ]
    assert tables.relationships["REPORT_HAS_FACT"] == [
      {"from": "r1", "to": "f1"},
      {"from": "r1", "to": "f2"},
    ]
    assert tables.relationships["FACT_SET_CONTAINS_FACT"] == [
      {"from": "fs1", "to": "f1"}
    ]

  def test_read_lbug_gives_the_same_model(
    self, model: XbrlModel, through: XbrlModel, tmp_path: Path
  ) -> None:
    pytest.importorskip("ladybug")
    path = build_lbug(to_graph_tables(model), tmp_path / "acme.lbug")
    got = from_graph(read_lbug(path))
    assert got.filing.model_dump() == through.filing.model_dump()
    assert got.entity.model_dump() == through.entity.model_dump()
    assert {q: c.model_dump() for q, c in got.concepts.items()} == {
      q: c.model_dump() for q, c in through.concepts.items()
    }
    assert _fact_shapes(got, frozenset()) == _fact_shapes(through, frozenset())
    assert _network_shapes(got) == _network_shapes(through)
    # By accession, URI or identifier; a report the file does not hold is refused.
    assert from_graph(read_lbug(path, report=model.filing.accession)).facts
    assert from_graph(read_lbug(path, report=REPORT_URI)).facts
    with pytest.raises(GraphError, match="no Report row"):
      read_lbug(path, report="0000000000-00-000000")
      from_graph(read_lbug(path, report="0000000000-00-000000"))

  def test_the_session_loads_a_lbug(self, model: XbrlModel, tmp_path: Path) -> None:
    pytest.importorskip("ladybug")
    path = build_lbug(to_graph_tables(model), tmp_path / "acme.lbug")
    session = FilingSession()
    try:
      loaded = session.load(str(path))
      assert loaded.id == model.filing.accession
      assert loaded.has_xbrl is True
      assert loaded.has_document is False
      described = tools.describe_filing(loaded)
      assert described["filing"]["accession"] == model.filing.accession
      assert described["filing"]["form"] == "10-K"
      rows = tools.fact_grid(loaded, ["us-gaap:EarningsPerShareBasic"])["rows"]
      assert [row["value"] for row in rows] == [2.5]
      rendered = tools.statement(loaded, BALANCE_SHEET)["rows"]
      assert [row["label"] for row in rendered] == [
        "Assets [Abstract]",
        "Total assets",
        "Cash",
      ]
      families = tools.disclosures(loaded)["disclosures"]
      assert {family["disclosure"] for family in families} >= {
        "Balance Sheet",
        "Segments",
      }
      block = tools.information_block(loaded, "SegmentsDetails")
      assert block["axes"][0]["axis"] == "us-gaap:StatementBusinessSegmentsAxis"
      assert any(row.get("members") for row in block["rows"])
    finally:
      session.close()


# -- the gate: the corpus ----------------------------------------------------------------

CORPUS = os.environ.get("XBRLKIT_CORPUS")
_FILINGS = (
  sorted(
    p.parent
    for p in Path(CORPUS).glob("*/rung7b.filing.lbug")
    if (p.parent / "rung7c.holon.jsonld").is_file()
  )
  if CORPUS
  else []
)


def _arc_shapes(model: XbrlModel, kind: str) -> Counter:
  return Counter(
    (
      n.role_uri,
      a.arcrole,
      a.from_qname,
      a.to_qname,
      a.order,
      a.weight,
      a.preferred_label,
    )
    for n in model.networks
    if n.kind == kind
    for a in n.arcs
    if a.arcrole != "XBRL-dimensions"
  )


def _fact_keys(model: XbrlModel) -> Counter:
  units = {u.id: u for u in model.units}
  periods = {p.id: p for p in model.periods}
  return Counter(
    (
      f.concept_qname,
      (
        periods[f.period_id].period_type,
        periods[f.period_id].start,
        periods[f.period_id].end,
      ),
      units[f.unit_id].measure if f.unit_id else None,
      # A nil text fact reads as an empty string from the graph, as null
      # from the holon: the declared nil-versus-empty gap.
      f.numeric_value if f.value_kind == "numeric" else (f.value_str or None),
      tuple(sorted((d.axis_qname, d.member_qname or d.typed_value) for d in f.dims)),
      f.entity_cik,
    )
    for f in model.facts
  )


@pytest.mark.integration
@pytest.mark.skipif(
  not _FILINGS, reason="set XBRLKIT_CORPUS to the Filing Ladder data directory"
)
@pytest.mark.parametrize("filing", _FILINGS, ids=[p.name for p in _FILINGS])
def test_the_graph_and_the_holon_read_the_same_filing(filing: Path) -> None:
  """The corpus gate: a filing read from its ``.lbug`` and from its holon
  agree on every fact, concept and arc the block planner uses, and reading
  the graph's own re-projection is a fixed point.

  Two differences are the other side's, and are excluded: a holon read back
  mints an extension unit's URI under the prefix rather than the namespace
  (units compare by measure), and the projection folds a second copy of a
  dimensional arc into the ``XBRL-dimensions`` aggregate (those arcs are
  excluded; the planner ignores them).
  """
  pytest.importorskip("ladybug")
  graph, gaps = from_graph_report(read_lbug(filing / "rung7b.filing.lbug"))
  holon = from_holon_json((filing / "rung7c.holon.jsonld").read_text())

  assert gaps.unresolved_references == 0
  assert set(graph.concepts) == set(holon.concepts)
  assert _fact_keys(graph) == _fact_keys(holon)
  for kind in ("presentation", "calculation", "definition"):
    assert _arc_shapes(graph, kind) == _arc_shapes(holon, kind), kind

  by_role_graph = {b.role_uri: b for b in plan_blocks(graph)}
  by_role_holon = {b.role_uri: b for b in plan_blocks(holon)}
  assert set(by_role_graph) == set(by_role_holon)
  for role, block in by_role_graph.items():
    other = by_role_holon[role]
    assert sorted(a.qname for a in block.axes) == sorted(a.qname for a in other.axes), (
      role
    )
    assert (block.has_calc, len(block.hypercubes)) == (
      other.has_calc,
      len(other.hypercubes),
    ), role

  again = from_graph(to_graph_tables(graph))
  assert again.filing.model_dump() == graph.filing.model_dump()
  assert again.entity.model_dump() == graph.entity.model_dump()
  assert {q: c.model_dump() for q, c in again.concepts.items()} == {
    q: c.model_dump() for q, c in graph.concepts.items()
  }
  assert _fact_shapes(again, frozenset()) == _fact_shapes(graph, frozenset())
  assert _network_shapes(again) == _network_shapes(graph)
