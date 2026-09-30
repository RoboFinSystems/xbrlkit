"""Tests for read-only Cypher over the graphs xbrlkit writes (``xbrlkit.cypher``)."""

from __future__ import annotations

from pathlib import Path

import pytest

from xbrlkit.cypher import CypherError, check_query, graph_kind, run_cypher
from xbrlkit.serialize import build_lbug, to_graph_tables, write_icebug

from .test_serve import _model

pytest.importorskip("ladybug")

REVENUE = (
  "MATCH (f:Fact {has_dimensions: false})-[:FACT_HAS_ELEMENT]->"
  "(e:Element {qname: 'us-gaap:Revenues'}), (f)-[:FACT_HAS_PERIOD]->(p:Period) "
  "RETURN p.end_date AS period_end, max(f.numeric_value) AS value ORDER BY period_end LIMIT 10"
)


@pytest.fixture(scope="module")
def graphs(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
  root = tmp_path_factory.mktemp("graphs")
  tables = to_graph_tables(_model())
  return {
    "lbug": build_lbug(tables, root / "acme.lbdb"),
    "icebug": write_icebug(tables, root / "acme.icebug"),
  }


@pytest.mark.unit
class TestCheckQuery:
  @pytest.mark.parametrize(
    "query",
    [
      "CREATE (n:Fact {identifier: 'x'})",
      "MATCH (f:Fact) SET f.value = 'x' RETURN f",
      "MATCH (f:Fact) DETACH DELETE f",
      "COPY Fact FROM 'elsewhere.parquet'",
      "LOAD FROM 'file.csv' RETURN *",
      "ATTACH 'other.lbug' AS other",
    ],
  )
  def test_writes_and_loads_are_refused(self, query: str) -> None:
    with pytest.raises(CypherError, match="read-only"):
      check_query(query, "lbug")

  def test_a_keyword_inside_a_string_is_not_a_clause(self) -> None:
    check_query(
      "MATCH (l:Label) WHERE contains(lower(l.value), 'assets held for sale; set aside') "
      "RETURN l.value LIMIT 5",
      "icebug",
    )

  @pytest.mark.parametrize(
    "query",
    [
      "MATCH ()-[r]->() RETURN count(r)",
      "MATCH (a:Fact)-[]->(b:Element) RETURN count(*)",
      "MATCH (a:Fact)-->(b:Element) RETURN count(*)",
      "MATCH (a:Element)<--(b:Fact) RETURN count(*)",
      "MATCH (a:Fact)-[r {weight: 1}]->(b) RETURN count(*)",
    ],
  )
  def test_untyped_relationships_are_refused_on_a_tree_only(self, query: str) -> None:
    with pytest.raises(CypherError, match="1066"):
      check_query(query, "icebug")
    check_query(query, "lbug")

  def test_typed_patterns_and_list_literals_pass_on_a_tree(self) -> None:
    check_query(
      "MATCH (f:Fact)-[:FACT_HAS_ELEMENT]->(e:Element), (f)-[r:FACT_HAS_PERIOD]->(p) "
      "WHERE e.qname IN ['us-gaap:Revenues', 'us-gaap:Assets'] RETURN count(*)",
      "icebug",
    )


@pytest.mark.unit
class TestRunCypher:
  def test_kinds(self, graphs: dict[str, Path], tmp_path: Path) -> None:
    assert graph_kind(graphs["lbug"]) == "lbug"
    assert graph_kind(graphs["icebug"]) == "icebug"
    with pytest.raises(CypherError, match="neither"):
      graph_kind(tmp_path)

  @pytest.mark.parametrize("kind", ["lbug", "icebug"])
  def test_both_containers_answer_alike(
    self, graphs: dict[str, Path], kind: str
  ) -> None:
    out = run_cypher(graphs[kind], REVENUE)
    assert out["columns"] == ["period_end", "value"]
    assert out["rows"] == [
      {"period_end": "2023-12-31", "value": 900_000.0},
      {"period_end": "2024-12-31", "value": 1_000_450.0},
    ]
    assert out["row_count"] == 2 and "note" not in out

  def test_rows_past_the_cap_are_counted_not_returned(
    self, graphs: dict[str, Path]
  ) -> None:
    out = run_cypher(
      graphs["lbug"], "MATCH (l:Label) RETURN l.value LIMIT 50", max_rows=2
    )
    assert len(out["rows"]) == 2 and out["row_count"] > 2
    assert "showing 2 of" in out["note"]

  def test_an_empty_answer_says_what_to_check(self, graphs: dict[str, Path]) -> None:
    out = run_cypher(
      graphs["icebug"],
      "MATCH (e:Element {qname: 'us-gaap:NoSuchThing'}) RETURN e.qname LIMIT 1",
    )
    assert out["rows"] == [] and "matched nothing" in out["note"]

  def test_an_engine_error_is_a_cypher_error(self, graphs: dict[str, Path]) -> None:
    with pytest.raises(CypherError, match="Cypher error"):
      run_cypher(graphs["lbug"], "MATCH (n:NoSuchTable) RETURN n LIMIT 1")

  def test_a_query_past_the_limit_is_stopped(self, graphs: dict[str, Path]) -> None:
    with pytest.raises(CypherError, match="timed out"):
      run_cypher(graphs["lbug"], REVENUE, timeout_s=0.01)

  def test_the_database_file_is_opened_read_only(self, graphs: dict[str, Path]) -> None:
    before = graphs["lbug"].stat().st_mtime_ns
    run_cypher(graphs["lbug"], REVENUE)
    assert graphs["lbug"].stat().st_mtime_ns == before
    assert not graphs["lbug"].with_name("acme.lbdb.wal").exists()
