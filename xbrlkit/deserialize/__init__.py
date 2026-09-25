"""Read a serialization back into the neutral ``XbrlModel`` — the inverse of
:mod:`xbrlkit.serialize`.

XBRL is the source in every other path through this package: Arelle loads a
filing and :mod:`xbrlkit.parse` walks it into the model, which the serializers
then project. These importers go the other way. A caller who already holds a
TAVI compiled model or a holon — a report that was never an SEC filing, a
ledger's own output, a filing someone else converted — turns it into the same
``XbrlModel`` and gets the whole toolset over it, with no Arelle and no XBRL.

The rule the importers keep: **read what the serialization carries and nothing
else.** Where a projection dropped something, the importer leaves it empty and
says so in its gap report rather than reconstructing a plausible value, because
a filled-in field is indistinguishable from a read one and would make the round
trip look lossless when it is not. The single exception is the derived period
enrichment, which :mod:`xbrlkit.periods` recomputes from the dates for every
source of a model — including the parse.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
  from .clawdog import (
    ClawDogError,
    from_clawdog,
    from_clawdog_json,
    from_clawdog_report,
  )
  from .holon import HolonError, from_holon, from_holon_json, from_holon_report
  from .tavi import TaviError, from_tavi, from_tavi_json, from_tavi_report

# Loaded on first use (PEP 562), not on import. Importing any submodule runs
# this file first, so eager imports here made
# `xbrlkit.deserialize.tavi` load rdflib through the holon importer.
_LAZY: dict[str, tuple[str, str]] = {
  "ClawDogError": (".clawdog", "ClawDogError"),
  "from_clawdog": (".clawdog", "from_clawdog"),
  "from_clawdog_json": (".clawdog", "from_clawdog_json"),
  "from_clawdog_report": (".clawdog", "from_clawdog_report"),
  "HolonError": (".holon", "HolonError"),
  "from_holon": (".holon", "from_holon"),
  "from_holon_json": (".holon", "from_holon_json"),
  "from_holon_report": (".holon", "from_holon_report"),
  "TaviError": (".tavi", "TaviError"),
  "from_tavi": (".tavi", "from_tavi"),
  "from_tavi_json": (".tavi", "from_tavi_json"),
  "from_tavi_report": (".tavi", "from_tavi_report"),
}


def __getattr__(name: str) -> Any:
  entry = _LAZY.get(name)
  if entry is None:
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
  module, attribute = entry
  value = getattr(importlib.import_module(module, __name__), attribute)
  globals()[name] = value
  return value


__all__ = (
  "ClawDogError",
  "HolonError",
  "TaviError",
  "from_clawdog",
  "from_clawdog_json",
  "from_clawdog_report",
  "from_holon",
  "from_holon_json",
  "from_holon_report",
  "from_tavi",
  "from_tavi_json",
  "from_tavi_report",
)
