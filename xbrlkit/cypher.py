"""Read-only Cypher over a property graph xbrlkit wrote: a ``.lbdb`` database or
an icebug-disk tree, holding one filing or several stacked.

Needs LadybugDB (the ``lpg`` extra). The query runs in a spawned worker with a
wall-clock limit: the engine evaluates in-process and cannot be interrupted, a
pattern with a cross product can run for a long time, and a forked child does
not come cleanly out of the MCP server's event loop. A database is opened
read-only; a tree is mounted into an in-memory database from its
``schema.cypher``, so nothing is ever written next to either.

A spawned worker imports the calling script again before it runs, so a script
that calls :func:`run_cypher` at module level needs the usual guard, or the
worker stops with multiprocessing's "bootstrapping phase" error::

    if __name__ == "__main__":
        print(run_cypher(Path("out/mmm-stack.icebug"), "MATCH (r:Report) RETURN count(r)"))

A notebook, a REPL or a function imported from a module needs nothing.

Two refusals before anything runs:

- **Writes.** A query must start as a read and name no write, DDL or load clause.
- **Untyped relationships on a tree.** On icebug-disk tables LadybugDB returns
  wrong rows for a relationship pattern with no type, silently
  (LadybugDB/ladybug#1066), so a tree only takes typed ones. A database has no
  such defect and takes both.
"""

from __future__ import annotations

import multiprocessing
import re
from pathlib import Path
from typing import Any

DEFAULT_MAX_ROWS = 200
TIMEOUT_S = 60.0

_READ_START = re.compile(
  r"^\s*(MATCH|OPTIONAL\s+MATCH|WITH|RETURN|UNWIND|CALL\s+(show_tables|table_info))\b",
  re.I,
)
_WRITE_CLAUSE = re.compile(
  r"\b(CREATE|MERGE|SET|DELETE|DETACH|REMOVE|DROP|ALTER|COPY|LOAD|INSTALL|ATTACH|"
  r"IMPORT|EXPORT|BEGIN|COMMIT|ROLLBACK|CHECKPOINT|USE)\b",
  re.I,
)
_STRING = re.compile(r"'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\"")
_PROPERTY_MAP = re.compile(r"\{[^{}]*\}")
_RELATIONSHIP = re.compile(r"-\s*\[([^\]]*)\]")
_BARE_ARROW = re.compile(r"\)\s*<?\s*-\s*-\s*>?\s*\(")


class CypherError(ValueError):
  """A query refused, failed or timed out; the message says which and why."""


def graph_kind(path: Path) -> str:
  """``"icebug"`` for a tree (a directory with a ``schema.cypher``), ``"lbug"``
  for a LadybugDB database file, whatever its extension."""
  path = Path(path)
  if path.is_dir() and (path / "schema.cypher").is_file():
    return "icebug"
  if path.is_file():
    return "lbug"
  raise CypherError(f"{path} is neither a LadybugDB database nor an icebug-disk tree")


def check_query(query: str, kind: str) -> None:
  """Raise :class:`CypherError` for a query ``run_cypher`` will not run."""
  code = _STRING.sub("''", query)
  if not _READ_START.match(code) or _WRITE_CLAUSE.search(code):
    raise CypherError(
      "Only read-only Cypher is allowed: MATCH … RETURN, with a LIMIT, or "
      "CALL show_tables() / table_info('<table>') for the schema."
    )
  if kind == "icebug":
    patterns = _PROPERTY_MAP.sub("", code)
    untyped = any(":" not in rel for rel in _RELATIONSHIP.findall(patterns))
    if untyped or _BARE_ARROW.search(patterns):
      raise CypherError(
        "Name the type of every relationship, e.g. -[:FACT_HAS_ELEMENT]->. On an "
        "icebug-disk tree LadybugDB returns wrong rows for an untyped pattern "
        "(LadybugDB/ladybug#1066); CALL show_tables() lists the types."
      )


def run_cypher(
  path: Path,
  query: str,
  *,
  max_rows: int = DEFAULT_MAX_ROWS,
  timeout_s: float = TIMEOUT_S,
) -> dict[str, Any]:
  """Run one read-only query over the graph at ``path`` and return its rows:
  ``columns``, ``row_count`` (every row the query produced) and ``rows`` (the
  first ``max_rows``, one dict per row)."""
  path = Path(path)
  kind = graph_kind(path)
  check_query(query, kind)
  ctx = multiprocessing.get_context("spawn")
  receiver, sender = ctx.Pipe(duplex=False)
  worker = ctx.Process(
    target=_worker, args=(str(path), kind, query, max_rows, sender), daemon=True
  )
  worker.start()
  sender.close()
  try:
    if receiver.poll(timeout_s):
      outcome = receiver.recv()
      worker.join(5)
      if "error" in outcome:
        raise CypherError(outcome["error"])
      return outcome
  except EOFError:
    worker.join(5)
    raise CypherError("the query worker exited without an answer") from None
  finally:
    receiver.close()
    if worker.is_alive():
      worker.kill()
      worker.join(5)
  raise CypherError(
    f"The query timed out after {timeout_s:g}s: the pattern is too broad (an "
    "unbounded join or a cross product). Anchor it on one concept, period or "
    "report, and add a LIMIT."
  )


def _worker(path: str, kind: str, query: str, max_rows: int, sender: Any) -> None:
  try:
    sender.send(_evaluate(Path(path), kind, query, max_rows))
  except Exception as exc:  # the worker's only reporting channel
    sender.send({"error": f"Cypher error: {exc}"})
  finally:
    sender.close()


def _evaluate(path: Path, kind: str, query: str, max_rows: int) -> dict[str, Any]:
  import ladybug as lbug

  if kind == "lbug":
    db = lbug.Database(str(path), read_only=True)
    conn = lbug.Connection(db)
  else:
    db = lbug.Database(":memory:")
    conn = lbug.Connection(db)
    for statement in (path / "schema.cypher").read_text().split(";\n"):
      if statement.strip():
        conn.execute(statement)
  try:
    try:
      result = conn.execute(query)
    except Exception as exc:
      return {"error": f"Cypher error: {exc}"}
    columns = list(result.get_column_names())
    rows: list[dict[str, Any]] = []
    total = 0
    while result.has_next():
      values = result.get_next()
      total += 1
      if len(rows) < max_rows:
        rows.append({c: _plain(v) for c, v in zip(columns, values, strict=False)})
  finally:
    conn.close()
    db.close()
  payload: dict[str, Any] = {"columns": columns, "row_count": total, "rows": rows}
  if total > max_rows:
    payload["note"] = (
      f"showing {max_rows} of {total} rows; narrow the pattern or add a LIMIT"
    )
  elif total == 0:
    payload["note"] = (
      "0 rows: the pattern matched nothing. Check the qname, the period shape and "
      "the property names (CALL table_info('<table>') RETURN *)."
    )
  return payload


def _plain(value: object) -> object:
  if isinstance(value, (int, float, bool, str)) or value is None:
    return value
  return str(value)


__all__ = (
  "DEFAULT_MAX_ROWS",
  "TIMEOUT_S",
  "CypherError",
  "check_query",
  "graph_kind",
  "run_cypher",
)
