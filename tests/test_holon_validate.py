"""Tests for the holon validator (``holon/``): one verdict per named graph.

A holon the writer produces must conform, and each rule has a document that
breaks it: something missing, something in the wrong graph, a reference
nothing answers, a retired way of writing a relationship.
"""

from __future__ import annotations

import copy
import json
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from rdflib import RDF, Namespace

from xbrlkit.cli import main
from xbrlkit.deserialize.holon import HolonError
from xbrlkit.holon import (
  DATASET,
  GRAPHS,
  HolonValidation,
  render,
  shapes_graph,
  validate_holon,
  validate_holon_file,
)
from xbrlkit.model import (
  Arc,
  Concept,
  DimQualifier,
  EntityIdentity,
  FilingMeta,
  Network,
  Period,
  Unit,
  XbrlFact,
  XbrlModel,
)
from xbrlkit.serialize import to_holon

SH = Namespace("http://www.w3.org/ns/shacl#")
ROLE = "http://acme.com/role/BalanceSheet"
NS = "http://fasb.org/us-gaap/2024-01-31"


def _concept(name: str, **fields: Any) -> Concept:
  defaults: dict[str, Any] = {
    "qname": f"us-gaap:{name}",
    "namespace": NS,
    "name": name,
    "period_type": "instant",
    "pref_label": name,
  }
  return Concept(**{**defaults, **fields})


def _model() -> XbrlModel:
  """A balance sheet that foots, with one fact broken out by a segment."""
  money = {"balance": "debit", "is_numeric": True, "item_type": "monetaryItemType"}
  concepts = {
    c.qname: c
    for c in (
      _concept("Assets", **money),
      _concept("Cash", **money),
      _concept("Inventory", **money),
      _concept("StatementBusinessSegmentsAxis", is_dimension_item=True),
      _concept("WidgetsMember", is_domain_member=True),
    )
  }
  segment = DimQualifier(
    axis_qname="us-gaap:StatementBusinessSegmentsAxis",
    member_qname="us-gaap:WidgetsMember",
    is_explicit=True,
    axis_type="segment",
  )

  def fact(fact_id: str, name: str, value: float, **fields: Any) -> XbrlFact:
    return XbrlFact(
      id=fact_id,
      concept_qname=f"us-gaap:{name}",
      period_id="I-2024",
      unit_id="usd",
      entity_cik="0001234567",
      numeric_value=value,
      decimals="0",
      value_kind="numeric",
      **fields,
    )

  arcs = [
    Arc(from_qname="us-gaap:Assets", to_qname="us-gaap:Cash", order=1.0, weight=1.0),
    Arc(
      from_qname="us-gaap:Assets", to_qname="us-gaap:Inventory", order=2.0, weight=1.0
    ),
  ]
  return XbrlModel(
    filing=FilingMeta(accession="0000000000-24-000001", cik="0001234567", form="10-K"),
    entity=EntityIdentity(cik="0001234567", name="Acme Corp"),
    concepts=concepts,
    periods=[Period(id="I-2024", period_type="instant", end=date(2024, 12, 31))],
    units=[Unit(id="usd", measure="iso4217:USD")],
    facts=[
      fact("f1", "Assets", 1000.0),
      fact("f2", "Cash", 250.0),
      fact("f3", "Inventory", 750.0),
      fact("f4", "Cash", 100.0, dims=[segment]),
    ],
    networks=[
      Network(
        role_uri=ROLE, definition="Balance Sheet", kind="presentation", arcs=arcs
      ),
      Network(role_uri=ROLE, definition="Balance Sheet", kind="calculation", arcs=arcs),
    ],
  )


@pytest.fixture(scope="module")
def holon() -> dict[str, Any]:
  return json.loads(to_holon(_model()))


def _graph(doc: dict[str, Any], name: str) -> list[dict[str, Any]]:
  return next(g["@graph"] for g in doc["@graph"] if g["@id"].endswith(f"#{name}"))


def _typed(nodes: list[dict[str, Any]], term: str) -> list[dict[str, Any]]:
  def types(node: dict[str, Any]) -> list[str]:
    value = node.get("@type", [])
    return value if isinstance(value, list) else [value]

  return [n for n in nodes if term in types(n)]


def _validate(doc: dict[str, Any], **kwargs: Any) -> HolonValidation:
  return validate_holon(json.dumps(doc), **kwargs)


def _messages(result: HolonValidation, name: str) -> str:
  verdict = result.dataset if name == DATASET else result.graphs[name]
  assert verdict is not None
  return " | ".join(f.message for f in verdict.findings)


# -- what the writer produces conforms ------------------------------------------


def test_a_written_holon_conforms_graph_by_graph(holon: dict[str, Any]) -> None:
  result = _validate(holon)
  assert result.conforms, render(result)
  assert result.report == "https://robosystems.ai/report/0000000000-24-000001"
  for name in ("scene", "projection", "boundary"):
    verdict = result.graphs[name]
    assert verdict.present and verdict.conforms and verdict.triples > 0
    assert verdict.shapes > 0
  assert not result.graphs["lineage"].present
  assert result.dataset is not None and result.dataset.conforms


def test_the_report_is_checked_alongside_the_file(holon: dict[str, Any]) -> None:
  result = _validate(holon)
  assert result.check is not None and result.check.ok
  assert result.check.calculations_checked == result.check.calculations_footed == 1
  assert result.ok
  assert _validate(holon, check=False).check is None


def test_a_report_that_does_not_foot_conforms_but_is_not_ok(
  holon: dict[str, Any],
) -> None:
  doc = copy.deepcopy(holon)
  total = next(
    f for f in _typed(_graph(doc, "scene"), "rs:Fact") if f["@id"].endswith("/f1")
  )
  total["numericValue"] = "999"
  result = _validate(doc)
  # The file is well formed; it is the report inside it that is wrong.
  assert result.conforms
  assert result.check is not None and not result.check.ok
  assert not result.ok


def test_a_report_with_no_networks_needs_only_its_scene() -> None:
  model = _model()
  model.networks = []
  result = validate_holon(to_holon(model), check=False)
  assert result.conforms, render(result)
  assert not result.graphs["projection"].present
  assert not result.graphs["boundary"].present


# -- #scene ---------------------------------------------------------------------


def test_a_fact_without_a_period_fails_the_scene(holon: dict[str, Any]) -> None:
  doc = copy.deepcopy(holon)
  del _typed(_graph(doc, "scene"), "rs:Fact")[0]["period"]
  result = _validate(doc, check=False)
  assert not result.graphs["scene"].conforms
  assert "exactly one rs:Period" in _messages(result, "scene")
  assert result.graphs["projection"].conforms and result.graphs["boundary"].conforms


def test_a_fact_citing_an_element_the_scene_does_not_describe_fails(
  holon: dict[str, Any],
) -> None:
  doc = copy.deepcopy(holon)
  scene = _graph(doc, "scene")
  scene[:] = [n for n in scene if n["@id"] != "us-gaap:Inventory"]
  result = _validate(doc, check=False)
  assert "exactly one rs:Element" in _messages(result, "scene")


def test_a_numeric_fact_without_its_number_fails(holon: dict[str, Any]) -> None:
  doc = copy.deepcopy(holon)
  del _typed(_graph(doc, "scene"), "rs:Fact")[0]["numericValue"]
  result = _validate(doc, check=False)
  assert "A numeric fact carries rs:numericValue" in _messages(result, "scene")


def test_a_period_with_no_date_is_a_warning_not_a_violation(
  holon: dict[str, Any],
) -> None:
  doc = copy.deepcopy(holon)
  period = _typed(_graph(doc, "scene"), "rs:Period")[0]
  del period["xbrli:instant" if "xbrli:instant" in period else "instant"]
  result = _validate(doc, check=False)
  scene = result.graphs["scene"]
  assert scene.conforms and scene.warnings == 1
  assert scene.findings[0].severity == "warning"


def test_two_reports_in_one_scene_is_a_problem(holon: dict[str, Any]) -> None:
  doc = copy.deepcopy(holon)
  scene = _graph(doc, "scene")
  second = copy.deepcopy(_typed(scene, "rs:Report")[0])
  second["@id"] += "-again"
  scene.append(second)
  result = _validate(doc, check=False)
  assert not result.conforms
  assert any("2 rs:Report nodes" in p for p in result.problems)


# -- a node in the wrong graph --------------------------------------------------


def test_a_fact_in_the_boundary_fails_the_boundary(holon: dict[str, Any]) -> None:
  doc = copy.deepcopy(holon)
  _graph(doc, "boundary").append(
    copy.deepcopy(_typed(_graph(doc, "scene"), "rs:Fact")[0])
  )
  result = _validate(doc, check=False)
  assert result.graphs["scene"].conforms
  assert "belongs in #scene, not #boundary" in _messages(result, "boundary")


def test_a_calculation_arc_in_the_projection_fails_it(holon: dict[str, Any]) -> None:
  doc = copy.deepcopy(holon)
  arc = _typed(_graph(doc, "boundary"), "rs:Association")[0]
  _graph(doc, "projection").append(copy.deepcopy(arc))
  result = _validate(doc, check=False)
  messages = _messages(result, "projection")
  assert "A calculation arc belongs in #boundary" in messages
  assert "rs:RollUpRelationship belongs in #boundary" in messages


def test_a_presentation_arc_in_the_boundary_fails_it(holon: dict[str, Any]) -> None:
  doc = copy.deepcopy(holon)
  arc = _typed(_graph(doc, "projection"), "rs:Association")[0]
  _graph(doc, "boundary").append(copy.deepcopy(arc))
  result = _validate(doc, check=False)
  assert "Every arc in #boundary is a calculation arc" in _messages(result, "boundary")


# -- references across graphs ---------------------------------------------------


def test_an_arc_to_an_element_no_graph_describes_fails_the_dataset(
  holon: dict[str, Any],
) -> None:
  doc = copy.deepcopy(holon)
  for name in ("scene", "projection"):
    nodes = _graph(doc, name)
    nodes[:] = [n for n in nodes if n["@id"] != "us-gaap:Inventory"]
  result = _validate(doc, check=False)
  # The roll-up to Inventory sits in #boundary, where it is well formed on its
  # own: only the graphs together show that nothing describes its component.
  assert result.graphs["boundary"].conforms
  assert "described in #projection" in _messages(result, DATASET)


def test_a_block_naming_a_structure_nothing_describes_fails_the_dataset(
  holon: dict[str, Any],
) -> None:
  doc = copy.deepcopy(holon)
  _typed(_graph(doc, "scene"), "rs:InformationBlock")[0]["structure"] += "-missing"
  result = _validate(doc, check=False)
  assert result.graphs["scene"].conforms
  assert "information block's structure" in _messages(result, DATASET)


def test_a_retired_dialect_fails_the_dataset(holon: dict[str, Any]) -> None:
  doc = copy.deepcopy(holon)
  fact = _typed(_graph(doc, "scene"), "rs:Fact")[0]
  fact["http://www.xbrl.org/2003/instance#contextRef"] = "c1"
  result = _validate(doc, check=False)
  assert "xbrli:contextRef is XBRL syntax" in _messages(result, DATASET)


# -- the dataset's own make-up --------------------------------------------------


def test_a_graph_the_format_does_not_name_is_a_problem(holon: dict[str, Any]) -> None:
  doc = copy.deepcopy(holon)
  root = doc["@graph"][0]["@id"].rpartition("#")[0]
  doc["@graph"].append(
    {"@id": f"{root}#notes", "@graph": [{"@id": f"{root}/n1", "rs:mode": "x"}]}
  )
  result = _validate(doc, check=False)
  assert not result.conforms
  assert any(
    "#notes" in p and "not a graph the format names" in p for p in result.problems
  )


def test_a_holon_without_its_scene_is_a_problem(holon: dict[str, Any]) -> None:
  doc = copy.deepcopy(holon)
  doc["@graph"] = [g for g in doc["@graph"] if not g["@id"].endswith("#scene")]
  result = _validate(doc, check=False)
  assert "The #scene graph is missing." in result.problems


def test_something_that_is_not_a_holon_is_refused() -> None:
  with pytest.raises(HolonError):
    validate_holon("not json")
  with pytest.raises(HolonError):
    validate_holon(json.dumps({"@graph": [{"@id": "http://x/a", "http://x/p": "v"}]}))


# -- #lineage -------------------------------------------------------------------

LINEAGE_CONTEXT = {
  "prov": "http://www.w3.org/ns/prov#",
  "substantiates": {"@id": "rs:substantiates", "@type": "@id"},
  "hadMember": {"@id": "prov:hadMember", "@type": "@id"},
  "derivedFrom": {"@id": "prov:wasDerivedFrom", "@type": "@id"},
  "account": {"@id": "rs:account", "@type": "@id"},
  "entry": {"@id": "rs:entry", "@type": "@id"},
  "transaction": {"@id": "rs:transaction", "@type": "@id"},
}


def _with_lineage(holon: dict[str, Any], *, members: bool) -> dict[str, Any]:
  """The holon with a ledger behind its first fact: one contribution of one line."""
  doc = copy.deepcopy(holon)
  context = doc["@context"]
  doc["@context"] = [
    *(context if isinstance(context, list) else [context]),
    LINEAGE_CONTEXT,
  ]
  root = doc["@graph"][0]["@id"].rpartition("#")[0]
  fact = next(
    f for f in _typed(_graph(doc, "scene"), "rs:Fact") if f["@id"].endswith("/f2")
  )
  contribution: dict[str, Any] = {
    "@id": f"{root}/contribution/f2/cash",
    "@type": ["rs:Contribution", "prov:Collection"],
    "substantiates": fact["@id"],
    "rs:lineCount": 1,
    "account": f"{root}/account/cash",
  }
  fact["derivedFrom"] = [contribution["@id"]]
  nodes: list[dict[str, Any]] = [
    contribution,
    {
      "@id": f"{root}/account/cash",
      "@type": "rs:Account",
      "rs:internalId": "cash",
      "skos:prefLabel": "Cash",
    },
  ]
  if members:
    contribution["hadMember"] = [f"{root}/line/l1"]
    nodes += [
      {
        "@id": f"{root}/line/l1",
        "@type": "rs:LineItem",
        "rs:internalId": "l1",
        "entry": f"{root}/entry/e1",
        "account": f"{root}/account/cash",
        "rs:debit": "250.00",
      },
      {
        "@id": f"{root}/entry/e1",
        "@type": "rs:Entry",
        "rs:internalId": "e1",
        "transaction": f"{root}/transaction/t1",
        "rs:postingDate": "2024-12-01",
      },
      {
        "@id": f"{root}/transaction/t1",
        "@type": "rs:Transaction",
        "rs:internalId": "t1",
        "rs:transactionDate": "2024-12-01",
      },
    ]
  doc["@graph"].append({"@id": f"{root}#lineage", "@graph": nodes})
  return doc


def test_a_lineage_graph_is_validated_when_it_is_there(holon: dict[str, Any]) -> None:
  result = _validate(_with_lineage(holon, members=True), check=False)
  assert result.conforms, render(result)
  lineage = result.graphs["lineage"]
  assert lineage.present and lineage.triples > 0


def test_a_contribution_may_withhold_its_lines(holon: dict[str, Any]) -> None:
  # The form a holon carries when the ledger lines are not disclosed: the
  # contribution and its count, and no members.
  result = _validate(_with_lineage(holon, members=False), check=False)
  assert result.conforms, render(result)


def test_a_line_without_its_entry_fails_the_lineage(holon: dict[str, Any]) -> None:
  doc = _with_lineage(holon, members=True)
  del _typed(_graph(doc, "lineage"), "rs:LineItem")[0]["entry"]
  result = _validate(doc, check=False)
  assert "belongs to exactly one rs:Entry" in _messages(result, "lineage")
  assert result.graphs["scene"].conforms


def test_a_report_element_in_the_lineage_fails_it(holon: dict[str, Any]) -> None:
  doc = _with_lineage(holon, members=True)
  _graph(doc, "lineage").append(
    copy.deepcopy(_typed(_graph(doc, "scene"), "rs:Element")[0])
  )
  result = _validate(doc, check=False)
  assert "is an rs:Account, not an rs:Element" in _messages(result, "lineage")


def test_a_fact_derived_from_a_contribution_nothing_describes_fails_the_dataset(
  holon: dict[str, Any],
) -> None:
  doc = _with_lineage(holon, members=False)
  lineage = _graph(doc, "lineage")
  lineage[:] = [n for n in lineage if "rs:Contribution" not in n.get("@type", [])]
  result = _validate(doc, check=False)
  assert "described in #lineage" in _messages(result, DATASET)


def test_a_contribution_for_a_fact_the_scene_lacks_fails_the_dataset(
  holon: dict[str, Any],
) -> None:
  doc = _with_lineage(holon, members=False)
  _typed(_graph(doc, "lineage"), "rs:Contribution")[0]["substantiates"] += "-gone"
  result = _validate(doc, check=False)
  assert "substantiates an rs:Fact described in #scene" in _messages(result, DATASET)


# -- the shapes and the command -------------------------------------------------


@pytest.mark.parametrize("name", [*GRAPHS, DATASET])
def test_every_graph_has_shapes_with_a_message_each(name: str) -> None:
  shapes = shapes_graph(name)
  node_shapes = set(shapes.subjects(RDF.type, SH.NodeShape))
  assert node_shapes
  # A finding with no message tells its reader nothing, so every shape says
  # something and so does every property that requires a value.
  for shape in node_shapes:
    messages = list(shapes.objects(shape, SH.message))
    for prop in shapes.objects(shape, SH.property):
      own = list(shapes.objects(prop, SH.message))
      if (prop, SH.minCount, None) in shapes:
        assert own, (shape, shapes.value(prop, SH.path))
      messages += own
    assert messages, shape


def test_findings_are_bounded_and_counts_are_not(holon: dict[str, Any]) -> None:
  doc = copy.deepcopy(holon)
  for fact in _typed(_graph(doc, "scene"), "rs:Fact"):
    del fact["period"]
  scene = _validate(doc, check=False, max_findings=2).graphs["scene"]
  assert scene.violations == 4 and len(scene.findings) == 2


def test_the_command_says_yes_or_no_by_exit_code(
  holon: dict[str, Any], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
  good = tmp_path / "good.holon.jsonld"
  good.write_text(json.dumps(holon))
  assert main(["validate", str(good)]) == 0
  assert capsys.readouterr().out.startswith("conforms")

  doc = copy.deepcopy(holon)
  del _typed(_graph(doc, "scene"), "rs:Fact")[0]["period"]
  bad = tmp_path / "bad.holon.jsonld"
  bad.write_text(json.dumps(doc))
  assert main(["validate", str(bad), "--json"]) == 1
  verdict = json.loads(capsys.readouterr().out)
  assert verdict["conforms"] is False
  assert verdict["graphs"]["scene"]["violations"] == 1

  assert main(["validate", str(tmp_path / "absent.holon.jsonld")]) == 2
  assert validate_holon_file(good).conforms


def test_strict_also_fails_a_report_that_does_not_foot(
  holon: dict[str, Any], tmp_path: Path
) -> None:
  doc = copy.deepcopy(holon)
  total = next(
    f for f in _typed(_graph(doc, "scene"), "rs:Fact") if f["@id"].endswith("/f1")
  )
  total["numericValue"] = "999"
  path = tmp_path / "unfooted.holon.jsonld"
  path.write_text(json.dumps(doc))
  assert main(["validate", str(path)]) == 0
  assert main(["validate", str(path), "--strict"]) == 1
