"""Tests for the holon projection (``serialize/``).

Covers the presentation-network classifier, the model → ``holon.jsonld``
projection (three named graphs, an InformationBlock, facts grouped by
factSet), and a SHACL drift-guard over the exact bundle the holon serializes
from.
"""

from __future__ import annotations

import json
from datetime import date
from typing import Any

from xbrlkit.model import (
  Arc,
  Concept,
  DimQualifier,
  EntityIdentity,
  FilingMeta,
  Label,
  Network,
  Period,
  Unit,
  XbrlFact,
  XbrlModel,
)
from rdflib import RDF

from xbrlkit.serialize._kernel import context
from xbrlkit.serialize import (
  build_holon_graph,
  classify_network,
  root_qname,
  to_holon,
)
from xbrlkit.serialize._kernel.jsonld import RS, shacl_report


def _model() -> XbrlModel:
  """A minimal, hand-authored single-filing model: a two-line balance sheet."""
  filing = FilingMeta(
    accession="0000000000-24-000001",
    cik="0001234567",
    form="10-K",
    taxonomy_namespaces=["http://fasb.org/us-gaap/2024-01-31"],
  )
  entity = EntityIdentity(
    cik="0001234567",
    name="Acme Corp",
    legal_name="Acme Corporation",
    ein="12-3456789",
  )
  concepts = {
    "us-gaap:Assets": Concept(
      qname="us-gaap:Assets",
      namespace="http://fasb.org/us-gaap/2024-01-31",
      name="Assets",
      period_type="instant",
      balance="debit",
      is_numeric=True,
      item_type="monetaryItemType",
      substitution_group="xbrli:item",
      pref_label="Assets",
    ),
    "us-gaap:Cash": Concept(
      qname="us-gaap:Cash",
      namespace="http://fasb.org/us-gaap/2024-01-31",
      name="CashAndCashEquivalentsAtCarryingValue",
      period_type="instant",
      balance="debit",
      is_numeric=True,
      item_type="monetaryItemType",
      substitution_group="xbrli:item",
      pref_label="Cash",
    ),
  }
  periods = [Period(id="I-2024", period_type="instant", end=date(2024, 12, 31))]
  units = [Unit(id="usd", measure="iso4217:USD")]
  facts = [
    XbrlFact(
      id="f1",
      concept_qname="us-gaap:Assets",
      period_id="I-2024",
      unit_id="usd",
      entity_cik="0001234567",
      numeric_value=1000.0,
      decimals="-3",
      value_kind="numeric",
    ),
    XbrlFact(
      id="f2",
      concept_qname="us-gaap:Cash",
      period_id="I-2024",
      unit_id="usd",
      entity_cik="0001234567",
      numeric_value=250.0,
      decimals="-3",
      value_kind="numeric",
    ),
    # A text fact — must be shed by the numeric-only projection.
    XbrlFact(
      id="f3",
      concept_qname="dei:EntityRegistrantName",
      period_id="I-2024",
      entity_cik="0001234567",
      value_str="Acme Corp",
      value_kind="text",
    ),
  ]
  networks = [
    Network(
      role_uri="http://acme.com/role/BalanceSheet",
      definition="Consolidated Balance Sheets",
      kind="presentation",
      arcs=[Arc(from_qname="us-gaap:Assets", to_qname="us-gaap:Cash", order=1.0)],
    ),
    Network(
      role_uri="http://acme.com/role/BalanceSheet",
      definition="Consolidated Balance Sheets",
      kind="calculation",
      arcs=[
        Arc(from_qname="us-gaap:Assets", to_qname="us-gaap:Cash", order=1.0, weight=1.0)
      ],
    ),
  ]
  return XbrlModel(
    filing=filing,
    entity=entity,
    concepts=concepts,
    periods=periods,
    units=units,
    facts=facts,
    networks=networks,
  )


def _types(node: dict[str, Any]) -> list[str]:
  t = node.get("@type", [])
  return t if isinstance(t, list) else [t]


def _all_nodes(doc: dict[str, Any]) -> list[dict[str, Any]]:
  nodes: list[dict[str, Any]] = []
  for entry in doc.get("@graph", []):
    if isinstance(entry, dict) and "@graph" in entry:
      nodes.extend(entry["@graph"])
    elif isinstance(entry, dict):
      nodes.append(entry)
  return nodes


def test_classify_network() -> None:
  assert classify_network("x", "Consolidated Balance Sheets") == "balance_sheet"
  assert (
    classify_network("x", "Consolidated Statements of Financial Position")
    == "balance_sheet"
  )
  assert classify_network("x", "Consolidated Statements of Operations") == (
    "income_statement"
  )
  assert (
    classify_network("x", "Statements of Comprehensive Income") == "income_statement"
  )
  assert classify_network("x", "Consolidated Statements of Cash Flows") == (
    "cash_flow_statement"
  )
  assert (
    classify_network("x", "Statement of Stockholders' Equity") == "equity_statement"
  )
  # Non-primary + parenthetical networks are skipped.
  assert classify_network("x", "Notes to the Financial Statements") is None
  assert classify_network("x", "Balance Sheet (Parenthetical)") is None
  # Role-URI fallback when the definition does not classify.
  assert classify_network("http://x/role/BalanceSheet", None) == "balance_sheet"


def test_to_holon_three_named_graphs() -> None:
  doc = json.loads(to_holon(_model()))
  assert "@graph" in doc

  ids = {
    entry.get("@id", "")
    for entry in doc["@graph"]
    if isinstance(entry, dict) and "@graph" in entry
  }
  assert any(i.endswith("#scene") for i in ids)
  assert any(i.endswith("#boundary") for i in ids)
  assert any(i.endswith("#projection") for i in ids)


def test_to_holon_information_block_and_facts() -> None:
  doc = json.loads(to_holon(_model()))
  nodes = _all_nodes(doc)

  ib_nodes = [n for n in nodes if "rs:InformationBlock" in _types(n)]
  assert ib_nodes, "expected at least one rs:InformationBlock"

  fact_nodes = [n for n in nodes if "rs:Fact" in _types(n)]
  # Full fidelity: all three facts survive — two numeric + one text (dei).
  assert len(fact_nodes) == 3
  numeric = [n for n in fact_nodes if "numericValue" in n]
  text = [n for n in fact_nodes if "stringValue" in n]
  assert len(numeric) == 2
  assert len(text) == 1
  # The two balance-sheet line items group into their section's factSet; the
  # dei text fact belongs to no presentation network, so it carries none.
  assert all("factSet" in n for n in numeric)


def test_holon_graph_shacl_conforms() -> None:
  # Drift-guard: the flat graph the holon serializes from must satisfy the rs:
  # structural topology (positive Fact/Association/Dimension shapes + banned
  # dialects), now including text facts and dimensional nodes.
  result = shacl_report(build_holon_graph(_model()))
  assert result.ran
  assert result.conforms, result.report


def _dim_model() -> XbrlModel:
  """A concept reported both consolidated and broken down by one segment axis."""
  filing = FilingMeta(accession="0000000000-24-000002", cik="0001234567", form="10-K")
  entity = EntityIdentity(cik="0001234567", name="Acme Corp")
  concepts = {
    "us-gaap:Revenues": Concept(
      qname="us-gaap:Revenues",
      namespace="http://fasb.org/us-gaap/2024-01-31",
      name="Revenues",
      period_type="duration",
      balance="credit",
      is_numeric=True,
      item_type="monetaryItemType",
      pref_label="Revenues",
    ),
  }
  periods = [
    Period(
      id="D-2024",
      period_type="duration",
      start=date(2024, 1, 1),
      end=date(2024, 12, 31),
      duration_type="annual",
    )
  ]
  units = [Unit(id="usd", measure="iso4217:USD")]
  segment = DimQualifier(
    axis_qname="us-gaap:StatementBusinessSegmentsAxis",
    member_qname="acme:WidgetsMember",
    is_explicit=True,
    axis_type="segment",
  )
  facts = [
    XbrlFact(
      id="fc",  # consolidated total (no dimensions)
      concept_qname="us-gaap:Revenues",
      period_id="D-2024",
      unit_id="usd",
      entity_cik="0001234567",
      numeric_value=1000.0,
      value_kind="numeric",
    ),
    XbrlFact(
      id="fd",  # one segment's breakdown (dimensional)
      concept_qname="us-gaap:Revenues",
      period_id="D-2024",
      unit_id="usd",
      entity_cik="0001234567",
      dims=[segment],
      numeric_value=600.0,
      value_kind="numeric",
    ),
  ]
  networks = [
    Network(
      role_uri="http://acme.com/role/Income",
      definition="Consolidated Statements of Operations",
      kind="presentation",
      arcs=[Arc(from_qname="us-gaap:Revenues", to_qname="us-gaap:Revenues", order=1.0)],
    )
  ]
  return XbrlModel(
    filing=filing,
    entity=entity,
    concepts=concepts,
    periods=periods,
    units=units,
    facts=facts,
    networks=networks,
  )


def test_structure_order_is_string_sorted() -> None:
  # A 7-digit filer statement vs a 6-digit ecd governance role: numerically
  # 9952153 > 995445, but as strings "9952153" < "995445", so the statement must
  # rank first — matching the SEC adapter's `ORDER BY number` (string) sort.
  concepts = {
    "us-gaap:Assets": Concept(
      qname="us-gaap:Assets", namespace="", name="Assets", is_numeric=True
    ),
    "ecd:Foo": Concept(qname="ecd:Foo", namespace="", name="Foo"),
  }
  networks = [
    Network(
      role_uri="http://x/role/Insider",
      definition="995445 - Disclosure - Insider Trading Arrangements",
      kind="presentation",
      arcs=[Arc(from_qname="ecd:Foo", to_qname="ecd:Foo", order=1.0)],
    ),
    Network(
      role_uri="http://x/role/BalanceSheet",
      definition="9952153 - Statement - Consolidated Balance Sheets",
      kind="presentation",
      arcs=[Arc(from_qname="us-gaap:Assets", to_qname="us-gaap:Assets", order=1.0)],
    ),
  ]
  model = XbrlModel(
    filing=FilingMeta(accession="0000000000-24-000009", cik="0000000001"),
    entity=EntityIdentity(cik="0000000001"),
    concepts=concepts,
    networks=networks,
  )
  graph = build_holon_graph(model)
  order = {
    str(graph.value(s, RS.structureName)): int(graph.value(s, RS.structureOrder))  # type: ignore[arg-type]
    for s in graph.subjects(RDF.type, RS.Structure)
  }
  bs = order["9952153 - Statement - Consolidated Balance Sheets"]
  insider = order["995445 - Disclosure - Insider Trading Arrangements"]
  assert bs < insider, f"statement (rank {bs}) must precede governance (rank {insider})"


def test_item_type_value_domain() -> None:
  # itemType is the value domain (orthogonal to elementType), so a consumer can
  # tell a rendered HTML disclosure (textBlock) from a number or a plain string.
  concepts = {
    "us-gaap:PolicyTextBlock": Concept(
      qname="us-gaap:PolicyTextBlock",
      namespace="",
      name="PolicyTextBlock",
      is_textblock=True,
    ),
    "us-gaap:Assets": Concept(
      qname="us-gaap:Assets",
      namespace="",
      name="Assets",
      is_numeric=True,
      item_type="monetaryItemType",
    ),
    "dei:EntityRegistrantName": Concept(
      qname="dei:EntityRegistrantName",
      namespace="",
      name="EntityRegistrantName",
      item_type="stringItemType",
    ),
    "us-gaap:EarningsPerShareBasic": Concept(
      qname="us-gaap:EarningsPerShareBasic",
      namespace="",
      name="EarningsPerShareBasic",
      is_numeric=True,
      item_type="perShareItemType",
    ),
    "us-gaap:SharesOutstanding": Concept(
      qname="us-gaap:SharesOutstanding",
      namespace="",
      name="SharesOutstanding",
      is_numeric=True,
      is_shares=True,
      item_type="sharesItemType",
    ),
  }
  model = XbrlModel(
    filing=FilingMeta(accession="0000000000-24-000010", cik="0000000001"),
    entity=EntityIdentity(cik="0000000001"),
    concepts=concepts,
  )
  graph = build_holon_graph(model)
  item = {
    str(graph.value(s, RS.internalId)): str(graph.value(s, RS.itemType))
    for s in graph.subjects(RDF.type, RS.Element)
  }
  assert item["us-gaap:PolicyTextBlock"] == "textBlock"
  assert item["us-gaap:Assets"] == "monetary"
  assert item["dei:EntityRegistrantName"] == "string"
  # Per-share and share counts must be distinguishable so a renderer opts them
  # out of statement rescaling (else EPS rounds to 0 in a scaled statement).
  assert item["us-gaap:EarningsPerShareBasic"] == "perShare"
  assert item["us-gaap:SharesOutstanding"] == "shares"


def test_report_node_carries_filing_metadata() -> None:
  # The holon must identify its filing (accession/form) via a Report node that
  # survives the partition into #scene — mirroring the SEC graph's Report node.
  model = _model()
  graph = build_holon_graph(model)
  reports = list(graph.subjects(RDF.type, RS.Report))
  assert len(reports) == 1
  assert str(graph.value(reports[0], RS.accessionNumber)) == "0000000000-24-000001"
  assert str(graph.value(reports[0], RS.form)) == "10-K"

  doc = json.loads(to_holon(model))
  scene = next(
    e["@graph"]
    for e in doc["@graph"]
    if isinstance(e, dict) and str(e.get("@id", "")).endswith("#scene")
  )
  assert any("rs:Report" in _types(n) for n in scene)


def test_dimensional_facts_emitted_and_partitioned() -> None:
  model = _dim_model()

  # SHACL: the dimensional node satisfies rs:DimensionShape.
  assert shacl_report(build_holon_graph(model)).conforms

  doc = json.loads(to_holon(model))
  nodes = _all_nodes(doc)

  dim_nodes = [n for n in nodes if "rs:Dimension" in _types(n)]
  assert len(dim_nodes) == 1, "one rs:Dimension for the single (axis, member)"
  assert "axis" in dim_nodes[0] and "member" in dim_nodes[0]
  assert dim_nodes[0].get("axisType") == "segment"

  fact_nodes = [n for n in nodes if "rs:Fact" in _types(n)]
  assert len(fact_nodes) == 2
  dimensional = [n for n in fact_nodes if "dimension" in n]
  assert len(dimensional) == 1, "only the breakdown fact carries rs:dimension"

  # The rs:Dimension node must land in the #scene graph (alongside its facts).
  scene = next(
    e["@graph"]
    for e in doc["@graph"]
    if isinstance(e, dict) and str(e.get("@id", "")).endswith("#scene")
  )
  assert any("rs:Dimension" in _types(n) for n in scene)


NEGATED_TERSE = "http://www.xbrl.org/2009/role/negatedTerseLabel"


def test_presentation_arc_preferred_label() -> None:
  """A presentation arc's preferred-label role and resolved string are emitted."""
  model = _model()
  model.concepts["us-gaap:Cash"].labels.append(
    Label(value="Less cash", role=NEGATED_TERSE, language="en-US")
  )
  model.networks[0].arcs[0].preferred_label = NEGATED_TERSE

  doc = json.loads(to_holon(model))
  pres = [
    n
    for n in _all_nodes(doc)
    if "rs:Association" in _types(n) and n.get("associationType") == "presentation"
  ]
  assert pres, "expected a presentation association"
  assert pres[0]["preferredLabelRole"] == NEGATED_TERSE
  assert pres[0]["preferredLabel"] == "Less cash"


def test_arc_without_preferred_label_emits_none() -> None:
  """Arcs with no preferred-label role carry neither property."""
  doc = json.loads(to_holon(_model()))
  for n in _all_nodes(doc):
    if "rs:Association" in _types(n):
      assert "preferredLabel" not in n
      assert "preferredLabelRole" not in n


def test_holon_serialization_is_deterministic() -> None:
  """Same model, same bytes — and every @graph array is @id-sorted."""
  model = _model()
  assert to_holon(model) == to_holon(model)
  doc = json.loads(to_holon(model))
  for entry in doc.get("@graph", []):
    if isinstance(entry, dict) and isinstance(entry.get("@graph"), list):
      ids = [n.get("@id", "") for n in entry["@graph"] if isinstance(n, dict)]
      assert ids == sorted(ids)


def test_the_canonical_context_declares_each_term_once() -> None:
  """A repeated key in the context silently rebinds a term.

  `serialize/_kernel/` is a vendored fork and is excluded from ruff, so F601 —
  which is enabled and does catch this — never runs on it. That is how a second
  `"label"` key shipped in 0.7.0 and shadowed `rdfs:label` with `rs:label`,
  leaving one term meaning two predicates across two documents that both call
  themselves holons. Lint cannot reach the file; this can.
  """
  import ast
  from pathlib import Path

  source = Path(context.__file__).read_text()
  tree = ast.parse(source)
  for node in ast.walk(tree):
    if not isinstance(node, ast.Dict):
      continue
    keys = [k.value for k in node.keys if isinstance(k, ast.Constant)]
    duplicates = {k for k in keys if keys.count(k) > 1}
    assert not duplicates, f"repeated context keys: {sorted(duplicates)}"


def test_the_standard_label_term_does_not_shadow_rdfs_label() -> None:
  """`label` is `rdfs:label` — the platform's holon context binds it that way
  too, and the two must not disagree. XBRL's standard-role label is written
  under its own name."""
  assert context.CANONICAL_CONTEXT["label"] == "rdfs:label"
  assert context.CANONICAL_CONTEXT["standardLabel"]["@id"].endswith("standardLabel")


class _Arc:
  """Minimal stand-in for a presentation arc."""

  def __init__(self, parent: str, child: str) -> None:
    self.from_qname = parent
    self.to_qname = child


class TestRootQname:
  def test_the_root_is_the_parent_that_is_never_a_child(self) -> None:
    arcs = [_Arc("a:Root", "a:Total"), _Arc("a:Total", "a:Line")]
    assert root_qname(arcs) == "a:Root"

  def test_several_roots_are_ambiguous(self) -> None:
    """Nothing says which root speaks for the tree, so none does."""
    assert root_qname([_Arc("a:One", "a:X"), _Arc("a:Two", "a:Y")]) is None

  def test_no_arcs_is_no_root(self) -> None:
    assert root_qname([]) is None


class TestClassifyByRoot:
  """The language-independent fallback — why an ESEF filing classifies at all."""

  def test_a_swedish_definition_classifies_on_its_root(self) -> None:
    assert (
      classify_network(
        "http://cloetta.com/roles/RapportOEverFinansiellStaellning",
        "03 - Rapport över finansiell ställning",
        "ifrs-full:StatementOfFinancialPositionAbstract",
      )
      == "balance_sheet"
    )

  def test_a_spanish_definition_classifies_on_its_root(self) -> None:
    assert (
      classify_network(
        "http://www.bbva.es/role/EstadodeflujosdeefectivoStatement",
        "[0000005] Estado de flujos de efectivo, método indirecto (Statement)",
        "ifrs-full:StatementOfCashFlowsAbstract",
      )
      == "cash_flow_statement"
    )

  def test_the_namespace_does_not_matter(self) -> None:
    """One table of local names serves IFRS and US GAAP alike."""
    for ns in ("ifrs-full", "us-gaap", "acme"):
      assert (
        classify_network("x", "något på svenska", f"{ns}:StatementOfCashFlowsAbstract")
        == "cash_flow_statement"
      )

  def test_a_parenthetical_is_still_excluded_by_its_root(self) -> None:
    """The safety property: a parenthetical shares the statement's root.

    Only the definition separates them, so the root must never override it.
    """
    assert (
      classify_network(
        "x",
        "Consolidated Balance Sheets (Parenthetical)",
        "us-gaap:StatementOfFinancialPositionAbstract",
      )
      is None
    )

  def test_the_definition_wins_when_it_classifies(self) -> None:
    """The root is a last resort, not a correction."""
    assert (
      classify_network(
        "x", "Consolidated Statements of Cash Flows", "us-gaap:IncomeStatementAbstract"
      )
      == "cash_flow_statement"
    )

  def test_an_unknown_root_classifies_nothing(self) -> None:
    assert classify_network("x", "opaque", "us-gaap:CreditLossAbstract") is None

  def test_no_root_is_the_old_behaviour(self) -> None:
    assert classify_network("x", "Rapport över finansiell ställning") is None
