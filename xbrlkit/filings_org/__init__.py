"""filings.xbrl.org — the public index of filings outside EDGAR.

XBRL International's index of ESEF and national-regime filings: open, no key,
and one adapter for every country in it. The identifier is the LEI.

Platform-free in the same way as :mod:`xbrlkit.edgar` — synchronous
``requests``, local-filesystem output, settings from
:class:`xbrlkit.config.Config`.
"""

from __future__ import annotations

from .client import EntityRecord, FilingRecord, FilingsOrgClient
from .download import download_filing, fetch, package_url

__all__ = [
  "EntityRecord",
  "FilingRecord",
  "FilingsOrgClient",
  "download_filing",
  "fetch",
  "package_url",
]
