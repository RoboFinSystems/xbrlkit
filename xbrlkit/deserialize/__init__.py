"""Read a serialization back into the neutral ``XbrlModel`` — the inverse of
:mod:`xbrlkit.serialize`.

XBRL is the source in every other path through this package: Arelle loads a
filing and :mod:`xbrlkit.parse` walks it into the model, which the serializers
then project. These importers go the other way. A caller who already holds a
TAVI compiled model or a holon — a report that was never an SEC filing, a
ledger's own output, a filing someone else converted — turns it into the same
``XbrlModel`` and gets the whole toolset over it, with no Arelle and no XBRL.

The rule both importers keep: **read what the serialization carries and nothing
else.** Where a projection dropped something, the importer leaves it empty and
says so in its gap report rather than reconstructing a plausible value, because
a filled-in field is indistinguishable from a read one and would make the round
trip look lossless when it is not. The single exception is the derived period
enrichment, which :mod:`xbrlkit.periods` recomputes from the dates for every
source of a model — including the parse.
"""

from __future__ import annotations

from .holon import HolonError, from_holon, from_holon_json, from_holon_report
from .tavi import TaviError, from_tavi, from_tavi_json, from_tavi_report

__all__ = (
  "HolonError",
  "TaviError",
  "from_holon",
  "from_holon_json",
  "from_holon_report",
  "from_tavi",
  "from_tavi_json",
  "from_tavi_report",
)
