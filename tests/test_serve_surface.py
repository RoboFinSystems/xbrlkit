"""The declared surface of :mod:`xbrlkit.serve`.

``serve`` is an MCP server, but its two shaped tools are also called directly
by a host that serves them itself: RoboSystems' ``information-block`` /
``disclosures`` endpoints build a ``LoadedFiling`` over a report they already
hold and call the tool functions, so the answers agree whether they are
reached through this server or through the platform. That makes the names in
``__all__`` an API, and a rename inside ``serve/`` an outage for the host on
its next deploy rather than a red build here.

This pins the names. Adding one is free; removing or renaming one should fail
loudly, next to a note saying who depends on it.
"""

from __future__ import annotations

import subprocess
import sys

import xbrlkit.serve as serve

# Every name a host is entitled to import from ``xbrlkit.serve``, and what it
# has to be. The six marked (RoboSystems) are the ones in use today —
# ``robosystems/operations/roboledger/views/information_blocks.py``.
DECLARED: dict[str, str] = {
  "FilingSession": "class",
  "LoadedFiling": "class",  # RoboSystems
  "SourceError": "class",
  "TextSection": "class",
  "ToolError": "class",  # RoboSystems
  "MAX_BLOCK_ROWS": "int",  # RoboSystems
  "MAX_BLOCK_MEMBERS_CAP": "int",  # RoboSystems
  "build_server": "callable",
  "build_text": "callable",
  "disclosures": "callable",  # RoboSystems
  "information_block": "callable",  # RoboSystems
  "serve": "callable",
}


def test_declared_surface_is_exactly_this() -> None:
  assert set(serve.__all__) == set(DECLARED), (
    "xbrlkit.serve.__all__ changed. Adding a name: add it here too. Removing "
    "or renaming one: it is an API — see this module's docstring."
  )


def test_every_declared_name_resolves() -> None:
  for name, kind in DECLARED.items():
    obj = getattr(serve, name, None)
    assert obj is not None, f"xbrlkit.serve.{name} is declared but missing"
    if kind == "class":
      assert isinstance(obj, type), f"{name} should be a class"
    elif kind == "int":
      assert isinstance(obj, int), f"{name} should be an int"
    else:
      assert callable(obj), f"{name} should be callable"


def test_importing_the_surface_does_not_need_the_mcp_extra() -> None:
  # ``disclosures`` and ``information_block`` are re-exported at module level,
  # so the base install has to keep importing: only build_server/serve may
  # reach for ``mcp``, and they do it lazily.
  #
  # In a subprocess, because another test in this session builds the server and
  # imports ``mcp`` for real — measured in-process this would pass or fail on
  # test order rather than on what the import actually pulls in.
  probe = (
    "import sys, xbrlkit.serve;"
    "print([m for m in sys.modules if m == 'mcp' or m.startswith('mcp.')])"
  )
  out = subprocess.run(
    [sys.executable, "-c", probe], capture_output=True, text=True, check=True
  )
  assert out.stdout.strip() == "[]", (
    f"importing xbrlkit.serve pulled in the mcp extra ({out.stdout.strip()}) — "
    f"keep the server imports lazy so a base install can call the tools"
  )


def test_the_two_shaped_tools_are_the_ones_the_module_documents() -> None:
  # A host calls these by name; the signature's first parameter is the loaded
  # filing in both, which is what lets a host substitute its own report.
  import inspect

  for fn in (serve.disclosures, serve.information_block):
    first = next(iter(inspect.signature(fn).parameters.values()))
    assert first.annotation in (serve.LoadedFiling, "LoadedFiling"), (
      f"{fn.__name__} should take a LoadedFiling first — a host builds one "
      f"over a report it already holds"
    )
