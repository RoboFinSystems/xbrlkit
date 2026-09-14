"""A local MCP server over filings loaded in memory — ``xbrlkit serve``.

The filing is parsed once (:func:`~xbrlkit.parse.load_model` →
:func:`~xbrlkit.parse.to_xbrl_model`) and held; the tools read the model and
the primary document's text. Shapes follow the RoboSystems ``sec`` graph's
MCP tools — describe, resolve an element, the fact grid, a statement, a
calculation roll-up, text search and read — so a reader that knows one knows
the other, with nothing behind them but the filing. Needs the ``mcp`` extra::

    pip install "xbrlkit[mcp]"
    xbrlkit serve                 # then load_filing a ticker, a path, a URL,
                                  # cik:accession or lei: from the client
    # → MCP over Streamable HTTP at http://127.0.0.1:8765/mcp

:class:`FilingSession` holds the loaded filings; :func:`build_server`
registers the tools on an ``MCPServer``; :func:`serve` runs it. The tool
functions themselves live in :mod:`xbrlkit.serve.tools` and take a
:class:`LoadedFiling`, so they can be called without MCP at all.

**The names in ``__all__`` are this package's declared surface, and a host
that serves these tools itself depends on them.** RoboSystems does exactly
that: its ``information-block`` / ``disclosures`` endpoints build a
:class:`LoadedFiling` over a report it already holds and call
:func:`disclosures` and :func:`information_block` directly, so the two tools
answer identically whether they are reached through this server or through
the platform. Adding to this tuple is free; renaming or removing from it
breaks that host with no signal until its next deploy, so treat it as an API
and not as the inside of an MCP server. Everything else under
:mod:`xbrlkit.serve` is internal.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .session import FilingSession, LoadedFiling, SourceError, TextSection, build_text
from .tools import (
  MAX_BLOCK_MEMBERS_CAP,
  MAX_BLOCK_ROWS,
  ToolError,
  disclosures,
  information_block,
)

__all__ = (
  "MAX_BLOCK_MEMBERS_CAP",
  "MAX_BLOCK_ROWS",
  "FilingSession",
  "LoadedFiling",
  "SourceError",
  "TextSection",
  "ToolError",
  "build_server",
  "build_text",
  "disclosures",
  "information_block",
  "serve",
)


def build_server(
  session: FilingSession, out_dir: Path | None = None, **kwargs: Any
) -> Any:
  """The ``MCPServer`` with every tool registered (imports the mcp extra).

  ``pure`` and ``with_document`` pass through — see :mod:`xbrlkit.serve.server`.
  """
  from .server import build_server as _build

  return _build(session, out_dir, **kwargs)


def serve(session: FilingSession, **kwargs: Any) -> None:
  """Run the server until interrupted (imports the mcp extra)."""
  from .server import serve as _serve

  _serve(session, **kwargs)
