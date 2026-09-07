"""A local MCP server over filings loaded in memory — ``xbrlkit serve``.

The filing is parsed once (:func:`~xbrlkit.parse.load_model` →
:func:`~xbrlkit.parse.to_xbrl_model`) and held; the tools read the model and
the primary document's text. Shapes follow the RoboSystems ``sec`` graph's
MCP tools — describe, resolve an element, the fact grid, a statement, a
calculation roll-up, text search and read — so a reader that knows one knows
the other, with nothing behind them but the filing. Needs the ``mcp`` extra::

    pip install "xbrlkit[mcp]"
    xbrlkit serve NVDA            # or a path, a URL, cik:accession
    # → MCP over Streamable HTTP at http://127.0.0.1:8765/mcp

:class:`FilingSession` holds the loaded filings; :func:`build_server`
registers the tools on an ``MCPServer``; :func:`serve` runs it. The tool
functions themselves live in :mod:`xbrlkit.serve.tools` and take a
:class:`LoadedFiling`, so they can be called without MCP at all.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .session import FilingSession, LoadedFiling, SourceError, TextSection, build_text

__all__ = (
  "FilingSession",
  "LoadedFiling",
  "SourceError",
  "TextSection",
  "build_server",
  "build_text",
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
