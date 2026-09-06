"""xbrlkit — work with XBRL filings above Arelle: one parse, portable models.

Fetch a filing from EDGAR (:mod:`xbrlkit.edgar`), parse it once into the
neutral :class:`XbrlModel` (:mod:`xbrlkit.parse`), and project that model
into whichever representation you need (:mod:`xbrlkit.serialize`): the
``holon.jsonld`` RDF/JSON-LD document, the Project Tavi compiled model,
xBRL-JSON, or the property-graph tables and a single-filing LadybugDB file.
:mod:`xbrlkit.text` reads the filing's primary HTML into sections without
Arelle at all; :mod:`xbrlkit.query` runs SPARQL over a built holon.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

from .model import XbrlModel


def _get_version() -> str:
  try:
    return version("xbrlkit")
  except PackageNotFoundError:
    return "0.0.0+development"


__version__ = _get_version()

__all__ = ("XbrlModel", "__version__")
