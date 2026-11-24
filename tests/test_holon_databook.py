"""Tests for the DataBook projection (``holon/databook.py``).

A DataBook is written from a holon and says so. These check that it keeps to
the DataBook core frontmatter, that what it shows is what the holon states,
that its RDF blocks are a true slice of ``#scene``, and that what it leaves
out it names.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest
from rdflib import RDF, Dataset, Graph, Namespace, URIRef

from tests.test_holon_validate import ROLE, _graph, _model, _typed
from xbrlkit.cli import main
from xbrlkit.holon import validate_holon
from xbrlkit.holon.databook import databook_from_holon, to_databook
from xbrlkit.model import Arc, Concept, Network, XbrlFact, XbrlModel
from xbrlkit.serialize import to_holon

RS = Namespace("https://robosystems.ai/vocab/")
REPORT = "https://robosystems.ai/report/0000000000-24-000001"
WHEN = {
  "created": date(2025, 1, 31),
  "timestamp": datetime(2025, 2, 1, 12, 0, tzinfo=UTC),
}
BLOCK = re.compile(
  r"<!-- databook:id: (?P<id>\S+) -->\n"
  r"<!-- databook:label: (?P<label>.*?) -->\n"
  r"<!-- databook:graph: (?P<graph>\S+) -->\n"
  r"<!-- mode=reference -->\n"
  r"```turtle\n(?P<turtle>.*?)\n```",
  re.S,
)


def _book_model() -> XbrlModel:
  """The validator's model, with the segment axis shown on the balance sheet
  so the fact broken out by segment belongs to that section."""
  model = _model()
  # Lexical forms a serializer would rewrite (1000 as 1000.0): a reference
  # block has to carry them as the holon does.
  for fact in model.facts:
    fact.value_str = str(int(fact.numeric_value or 0))
  model.networks[0].arcs.append(
    Arc(
      from_qname="us-gaap:Assets",
      to_qname="us-gaap:StatementBusinessSegmentsAxis",
      order=9.0,
    )
  )
  return model


@pytest.fixture(scope="module")
def holon_text() -> str:
  return to_holon(_book_model())


@pytest.fixture(scope="module")
def book(holon_text: str) -> str:
  return databook_from_holon(holon_text, href="acme.holon.jsonld", **WHEN)


def _front(book: str) -> str:
  assert book.startswith("---\n")
  return book[4 : book.index("\n---\n", 4)]


def _top_keys(front: str) -> list[str]:
  return [
    line.split(":", 1)[0] for line in front.splitlines() if re.match(r"^\w", line)
  ]


def _value(front: str, key: str) -> Any:
  """One scalar from the frontmatter; a string is written as JSON."""
  match = re.search(rf"^\s*(?:- )?{re.escape(key)}: (.+)$", front, re.M)
  assert match, key
  return json.loads(match.group(1))


# -- the frontmatter keeps to the DataBook core ---------------------------------


def test_the_identity_keys_are_there_and_are_what_core_asks(book: str) -> None:
  front = _front(book)
  keys = _top_keys(front)
  assert keys[:5] == ["id", "title", "type", "version", "created"]
  # A key written twice is read once, and the other is lost without a word.
  assert len(keys) == len(set(keys))
  assert _value(front, "id") == f"{REPORT}/databook"
  assert _value(front, "type") == "databook"
  assert re.fullmatch(r"\d+\.\d+\.\d+", _value(front, "version"))
  # Quoted, so YAML reads a string and not a date.
  assert _value(front, "created") == "2025-01-31"
  assert _value(front, "title") == "Acme Corp: 10-K for the period ended 2024-12-31"


def test_what_is_ours_sits_under_one_key(book: str) -> None:
  core = {
    "id", "title", "type", "version", "created", "description", "domain",
    "subject", "graph", "shapes", "process",
  }  # fmt: skip
  assert set(_top_keys(_front(book))) - core == {"xbrl_holon"}


def test_the_process_stamp_names_the_holon_as_its_primary_input(
  book: str, holon_text: str
) -> None:
  front = _front(book)
  digest = hashlib.sha256(holon_text.encode()).hexdigest()
  stamp = front[front.index("process:") : front.index("xbrl_holon:")]
  assert '  transformer_type: "script"' in stamp
  assert f'    - iri: "{REPORT}"\n      role: "primary"' in stamp
  assert f"sha256:{digest}" in stamp
  assert 'role: "constraint"' in stamp
  assert '  timestamp: "2025-02-01T12:00:00Z"' in stamp
  assert f'sha256: "{digest}"' in front
  assert 'href: "acme.holon.jsonld"' in front


def test_every_graph_is_named_with_how_much_of_it_is_here(book: str) -> None:
  front = _front(book)
  for name, disposition in (
    ("scene", "projected"),
    ("projection", "referenced"),
    ("boundary", "referenced"),
    ("lineage", "absent"),
  ):
    line = next(line for line in front.splitlines() if f'name: "{name}"' in line)
    assert f'iri: "{REPORT}#{name}"' in line
    assert f'disposition: "{disposition}"' in line
    assert ("triples:" in line) is (disposition != "absent")


def test_the_same_holon_and_the_same_dates_give_the_same_bytes(holon_text: str) -> None:
  first = databook_from_holon(holon_text, **WHEN)
  assert first == databook_from_holon(holon_text, **WHEN)


# -- what it shows is what the holon states -------------------------------------


def test_a_section_is_a_table_of_its_consolidated_values(book: str) -> None:
  assert "## Balance Sheet" in book
  assert "| | 2024-12-31 |" in book
  # The total is the row its calculation hangs from, and reads as one.
  assert "| **Assets** | [1,000][f1] |" in book
  assert "| &nbsp;&nbsp;Cash | [250][f2] |" in book
  assert "| &nbsp;&nbsp;Inventory | [750][f3] |" in book
  # The segment's 100 is a breakdown, and a breakdown is the holon's.
  assert "[100]" not in book
  assert (
    "1 further fact breaks these values out by `us-gaap:StatementBusinessSegmentsAxis`"
    in book
  )
  assert f"Section role: `{ROLE}`" in book


def test_a_value_reads_to_the_places_it_is_stated_to() -> None:
  model = _model()
  model.facts[0].numeric_value, model.facts[0].value_str = 1000.5, "1000.5"
  model.facts[0].decimals = "2"
  model.facts[1].numeric_value, model.facts[1].value_str = -250.0, "-250"
  book = to_databook(model, validate=False, links=False, **WHEN)
  assert "| **Assets** | 1,000.50 |" in book
  assert "| &nbsp;&nbsp;Cash | (250) |" in book
  # Past the stated places, the digits reported are kept.
  model.facts[2].numeric_value, model.facts[2].value_str = 750.125, "750.125"
  plain = to_databook(model, validate=False, links=False, **WHEN)
  assert "| &nbsp;&nbsp;Inventory | 750.125 |" in plain


def test_narrative_too_long_for_a_cell_is_shown_beneath_the_table() -> None:
  model = _model()
  model.concepts["us-gaap:Policy"] = Concept(
    qname="us-gaap:Policy",
    namespace="http://fasb.org/us-gaap/2024-01-31",
    name="Policy",
    period_type="instant",
    pref_label="Inventory policy",
  )
  passage = "<p>Inventory is carried at cost, first in first out. </p>" * 30
  model.facts.append(
    XbrlFact(
      id="t1",
      concept_qname="us-gaap:Policy",
      period_id="I-2024",
      entity_cik="0001234567",
      value_str=passage,
      value_kind="text",
    )
  )
  model.networks[0].arcs.append(
    Arc(from_qname="us-gaap:Assets", to_qname="us-gaap:Policy", order=3.0)
  )
  book = to_databook(model, validate=False, **WHEN)
  assert "| &nbsp;&nbsp;Inventory policy | [*text, " in book
  assert "**Inventory policy**\n\n> Inventory is carried at cost" in book
  assert "*[continues in the holon]*" in book
  assert "<p>" not in book


# -- a value is a link to its fact ----------------------------------------------


def test_each_value_links_to_the_fact_it_shows(book: str, holon_text: str) -> None:
  definitions = dict(re.findall(r"^\[(f\d+)\]: <(\S+)>$", book, re.M))
  used = set(re.findall(r"\]\[(f\d+)\]", book))
  assert used and used == set(definitions)
  doc = json.loads(holon_text)
  facts = {n["@id"]: n for n in _typed(_graph(doc, "scene"), "rs:Fact")}
  # The link beside "1,000" is the fact whose value is 1000.
  label = re.search(r"\| \*\*Assets\*\* \| \[1,000\]\[(f\d+)\]", book)
  assert label is not None
  assert facts[definitions[label.group(1)]]["numericValue"] == "1000"
  assert set(definitions.values()) <= set(facts)


def test_a_fact_shown_in_two_sections_is_one_link() -> None:
  model = _book_model()
  # A second section that presents the same total.
  model.networks.append(
    Network(
      role_uri="http://acme.com/role/Summary",
      definition="Summary",
      kind="presentation",
      arcs=[Arc(from_qname="us-gaap:Assets", to_qname="us-gaap:Cash", order=1.0)],
    )
  )
  book = to_databook(model, validate=False, **WHEN)
  assert "## Summary" in book
  assert book.count("[1,000][f1]") == 2
  assert len(re.findall(r"^\[f1\]: ", book, re.M)) == 1


def test_links_can_be_left_out(holon_text: str) -> None:
  book = databook_from_holon(holon_text, links=False, **WHEN)
  assert "| **Assets** | 1,000 |" in book
  assert not re.search(r"^\[f\d+\]: ", book, re.M)


# -- the RDF beside a table is a true slice of #scene ---------------------------


def test_each_reference_block_is_addressed_and_loads_into_the_scene(book: str) -> None:
  blocks = list(BLOCK.finditer(book))
  assert [b["id"] for b in blocks] == ["balance-sheet-facts"]
  assert blocks[0]["graph"] == f"{REPORT}#scene"
  # Folded, so a plain Markdown reader shows the table first.
  assert "<details>\n<summary>RDF: 3 facts above" in book


def test_the_blocks_say_only_what_the_holon_says(book: str, holon_text: str) -> None:
  dataset = Dataset()
  dataset.parse(data=holon_text, format="json-ld")
  scene = dataset.graph(URIRef(f"{REPORT}#scene"))
  inline = Graph()
  for block in BLOCK.finditer(book):
    inline.parse(data=block["turtle"], format="turtle")
  assert len(inline) > 0
  assert all(triple in scene for triple in inline)
  facts = set(inline.subjects(RDF.type, RS.Fact))
  assert len(facts) == 3 and set(inline.subjects()) == facts
  front = _front(book)
  assert _value(front, "triple_count") == len(inline)
  assert _value(front, "subjects") == len(facts)
  assert _value(front, "named_graph") == f"{REPORT}#scene"


def test_without_rdf_there_are_tables_and_no_graph_metadata(holon_text: str) -> None:
  book = databook_from_holon(holon_text, inline_rdf=False, **WHEN)
  assert "```turtle" not in book and "<details>" not in book
  assert "graph" not in _top_keys(_front(book))
  assert "| **Assets** | [1,000][f1] |" in book
  assert "*These facts are in the holon's `#scene` graph, not repeated here.*" in book


# -- the verdict travels with it ------------------------------------------------


def test_the_validation_verdict_is_in_the_frontmatter_and_the_body(book: str) -> None:
  front = _front(book)
  verdict = front[front.index("  validation:") :]
  assert "    conforms: true" in verdict
  assert (
    '      - {name: "scene", conforms: true, violations: 0, warnings: 0}' in verdict
  )
  assert "      calculations_footed: 1" in verdict
  assert "## Validation" in book
  assert "**conforms to the holon format**" in book
  assert "| `#lineage` | absent |" in book
  assert (
    "Read back, the report **holds together**: 4 facts, 1 of 1 calculations foot"
    in book
  )
  shapes = front[front.index("shapes:") : front.index("process:")]
  assert "/shapes/scene" in shapes and "/shapes/lineage" not in shapes


def test_a_holon_that_does_not_conform_says_so(holon_text: str) -> None:
  doc = json.loads(holon_text)
  del _typed(_graph(doc, "projection"), "rs:Structure")[0]["roleUri"]
  broken = json.dumps(doc)
  book = databook_from_holon(broken, **WHEN)
  assert "**does not conform**" in book
  assert "| `#projection` | fails |" in book
  assert "    conforms: false" in _front(book)


def test_a_verdict_can_be_passed_in_or_left_out(holon_text: str) -> None:
  verdict = validate_holon(holon_text, check=False)
  book = databook_from_holon(holon_text, validation=verdict, **WHEN)
  assert "## Validation" in book and "Read back" not in book
  bare = databook_from_holon(holon_text, validate=False, **WHEN)
  assert "## Validation" not in bare
  assert "shapes" not in _top_keys(_front(bare))
  assert "validation:" not in _front(bare)


# -- what it leaves out, it names -----------------------------------------------


def _with_details() -> XbrlModel:
  model = _model()
  for network in model.networks:
    network.definition = "0000002 - Statement - Balance Sheet"
  model.concepts["us-gaap:Debt"] = Concept(
    qname="us-gaap:Debt",
    namespace="http://fasb.org/us-gaap/2024-01-31",
    name="Debt",
    period_type="instant",
    is_numeric=True,
    item_type="monetaryItemType",
    pref_label="Debt",
  )
  model.concepts["us-gaap:DebtAbstract"] = Concept(
    qname="us-gaap:DebtAbstract",
    namespace="http://fasb.org/us-gaap/2024-01-31",
    name="DebtAbstract",
    is_abstract=True,
    pref_label="Debt [Abstract]",
  )
  model.facts.append(
    XbrlFact(
      id="d1",
      concept_qname="us-gaap:Debt",
      period_id="I-2024",
      unit_id="usd",
      entity_cik="0001234567",
      numeric_value=400.0,
      decimals="0",
      value_kind="numeric",
    )
  )
  model.networks.append(
    Network(
      role_uri="http://acme.com/role/DebtDetails",
      definition="0000009 - Disclosure - Debt (Details)",
      kind="presentation",
      arcs=[Arc(from_qname="us-gaap:DebtAbstract", to_qname="us-gaap:Debt", order=1.0)],
    )
  )
  return model


def test_a_filings_detail_sections_are_named_and_left_to_the_holon() -> None:
  book = to_databook(_with_details(), validate=False, **WHEN)
  assert "## Balance Sheet" in book
  assert "## Debt (Details)" not in book
  assert "| Debt (Details) | 1 | in the holon |" in book
  assert "1 section is named above and not drawn here" in book
  assert '{id: "debt-details", name: "Debt (Details)", facts: 1, drawn: false}' in book
  assert "[400]" not in book


def test_details_can_be_asked_for() -> None:
  book = to_databook(_with_details(), validate=False, details=True, **WHEN)
  assert "## Debt (Details)" in book
  assert "| Debt | [400][" in book
  assert "not drawn here" not in book


def test_the_document_ends_by_pointing_at_the_holon(book: str, holon_text: str) -> None:
  tail = book[book.index("## The holon") :]
  assert "[`acme.holon.jsonld`](acme.holon.jsonld)" in tail
  assert hashlib.sha256(holon_text.encode()).hexdigest() in tail
  assert f"| `{REPORT}#boundary` |" in tail and "in the holon only" in tail
  assert f"| `{REPORT}#lineage` |" in tail and "not in this holon" in tail


def test_a_lineage_graph_is_referenced_never_copied(holon_text: str) -> None:
  from tests.test_holon_validate import _with_lineage

  doc = _with_lineage(json.loads(holon_text), members=True)
  book = databook_from_holon(json.dumps(doc), **WHEN)
  line = next(line for line in _front(book).splitlines() if 'name: "lineage"' in line)
  assert 'disposition: "referenced"' in line
  assert "rs:LineItem" not in book and "/line/l1" not in book


def test_a_name_cannot_break_the_frontmatter() -> None:
  model = copy.deepcopy(_model())
  model.entity.name = 'Acme: "Widgets" & Sons #1'
  front = _front(to_databook(model, validate=False, **WHEN))
  assert _value(front, "title").startswith('Acme: "Widgets" & Sons #1: 10-K')


# -- the command ----------------------------------------------------------------


def test_the_command_writes_the_databook_beside_the_holon(
  holon_text: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
  source = tmp_path / "acme.holon.jsonld"
  source.write_text(holon_text)
  assert main(["databook", str(source)]) == 0
  out = tmp_path / "acme.databook.md"
  assert out.exists() and str(out) in capsys.readouterr().out
  text = out.read_text()
  assert 'href: "acme.holon.jsonld"' in text and "```turtle" in text

  named = tmp_path / "deep" / "report.md"
  assert (
    main(
      [
        "databook",
        str(source),
        "-o",
        str(named),
        "--no-rdf",
        "--no-validate",
        "--no-links",
      ]
    )
    == 0
  )
  text = named.read_text()
  assert "```turtle" not in text and "## Validation" not in text
  assert main(["databook", str(tmp_path / "absent.holon.jsonld")]) == 2
