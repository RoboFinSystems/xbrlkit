"""EDGAR fetch layer — resolve tickers, list filings, download XBRL zips and
the readable primary document, discover filings in bulk through EFTS.

Platform-free: synchronous ``requests``, local-filesystem output, all settings
from :class:`xbrlkit.config.Config`, and EDGAR's two throttle signatures (a 429,
an empty 200) ridden out with a bounded wait-and-retry.
"""

from __future__ import annotations

from .client import CompanyInfo, EdgarClient, EdgarThrottled, FilingRef
from .download import (
  download_filing,
  download_primary_document,
  fetch,
  primary_document_url,
)
from .efts import EftsClient, EftsHit, query_efts

__all__ = [
  "CompanyInfo",
  "EdgarClient",
  "EdgarThrottled",
  "EftsClient",
  "EftsHit",
  "FilingRef",
  "download_filing",
  "download_primary_document",
  "fetch",
  "primary_document_url",
  "query_efts",
]
