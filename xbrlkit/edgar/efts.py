"""SEC EFTS (full-text search) — discovery of filings by form, date, filer
or text, in bulk.

One query per date range returns every matching filing, instead of walking
company by company through the submissions API. EFTS caps a query at 10,000
hits, so a corpus is discovered by quarter (:meth:`EftsClient.query_by_quarter`);
a calendar quarter of the main forms sits at 5–7k. Synchronous ``requests``
under the package's rate limiter; a 429 waits for ``Retry-After``, a server
error waits and retries, both a bounded number of times.

API: https://efts.sec.gov/LATEST/
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlencode

import requests

from xbrlkit.config import CONFIG, Config

from .rate_limit import RateLimiter

logger = logging.getLogger(__name__)

EFTS_BASE_URL = "https://efts.sec.gov/LATEST/search-index"
EFTS_MAX_PAGE_SIZE = 100
EFTS_MAX_RESULTS = 10_000  # EFTS' hard limit per query
DEFAULT_FORMS: tuple[str, ...] = ("10-K", "10-Q", "20-F", "40-F", "DEF 14A", "S-1")

MAX_RETRIES = 3
MAX_RETRY_AFTER = 300.0  # cap a Retry-After at five minutes
SERVER_ERROR_RETRY_DELAY = 30.0

_QUARTERS = {
  1: ("01-01", "03-31"),
  2: ("04-01", "06-30"),
  3: ("07-01", "09-30"),
  4: ("10-01", "12-31"),
}


@dataclass
class EftsHit:
  """One filing from an EFTS result page."""

  cik: str
  accession: str
  form: str
  file_number: str | None
  filing_date: str | None
  primary_document: str | None
  file_url: str | None

  @classmethod
  def from_hit(cls, hit: dict) -> EftsHit:
    """Parse one ``hits.hits[]`` entry. The ``_id`` is ``accession:filename``;
    ``ciks`` is a list, whose first entry is the filer."""
    source = hit.get("_source", {}) or {}
    hit_id = str(hit.get("_id", ""))
    accession = hit_id.split(":")[0] if ":" in hit_id else hit_id
    ciks = source.get("ciks") or []
    display = source.get("display_names") or []
    return cls(
      cik=str(ciks[0]).zfill(10) if ciks else "",
      accession=accession,
      form=str(source.get("form", "") or ""),
      file_number=source.get("file_num"),
      filing_date=source.get("file_date"),
      primary_document=display[0] if display else None,
      file_url=source.get("file_url"),
    )


class EftsClient:
  """Synchronous EFTS client. One session, one rate limiter."""

  def __init__(self, config: Config = CONFIG, per_sec: float | None = None) -> None:
    self.config: Config = config
    self._session: requests.Session = requests.Session()
    self._session.headers.update(config.sec_headers)
    self._limiter: RateLimiter = RateLimiter(
      config.rate_limit_per_sec if per_sec is None else per_sec
    )

  # ---- one page -------------------------------------------------------------

  def _fetch_page(
    self, params: dict, offset: int = 0, size: int = EFTS_MAX_PAGE_SIZE
  ) -> dict:
    url = f"{EFTS_BASE_URL}?{urlencode(params)}&from={offset}&size={size}"
    attempt = 0
    while True:
      self._limiter.wait()
      resp = self._session.get(url, timeout=self.config.request_timeout)
      if resp.status_code == 429:
        if attempt >= MAX_RETRIES:
          raise RuntimeError(f"EFTS max retries ({MAX_RETRIES}) exceeded for {url}")
        delay = _retry_after(resp)
        logger.warning(
          "EFTS rate limited; waiting %.0fs (retry %d/%d)",
          delay,
          attempt + 1,
          MAX_RETRIES,
        )
        time.sleep(delay)
        attempt += 1
        continue
      if resp.status_code >= 500:
        if attempt >= MAX_RETRIES:
          raise RuntimeError(
            f"EFTS server error {resp.status_code} after {MAX_RETRIES} retries"
          )
        logger.warning(
          "EFTS server error %s; waiting %.0fs (retry %d/%d)",
          resp.status_code,
          SERVER_ERROR_RETRY_DELAY,
          attempt + 1,
          MAX_RETRIES,
        )
        time.sleep(SERVER_ERROR_RETRY_DELAY)
        attempt += 1
        continue
      resp.raise_for_status()
      return resp.json()

  # ---- queries --------------------------------------------------------------

  @staticmethod
  def build_params(
    forms: list[str] | tuple[str, ...] | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    ciks: list[str] | None = None,
    text_query: str | None = None,
    include_amendments: bool = False,
  ) -> dict[str, str]:
    """The EFTS query string. Dates are ``YYYY-MM-DD`` and EFTS needs both;
    CIKs are zero-padded; without ``include_amendments`` each form's ``/A``
    is excluded with EFTS' ``-form`` syntax."""
    params: dict[str, str] = {}
    if forms:
      wanted = list(forms)
      if not include_amendments:
        wanted.extend(f"-{form}/A" for form in forms if not form.endswith("/A"))
      params["forms"] = ",".join(wanted)
    params["startdt"] = start_date or "2001-01-01"
    params["enddt"] = end_date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if ciks:
      params["ciks"] = ",".join(str(c).zfill(10) for c in ciks)
    if text_query:
      params["q"] = text_query
    return params

  def query(
    self,
    forms: list[str] | tuple[str, ...] | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    ciks: list[str] | None = None,
    text_query: str | None = None,
    max_results: int | None = None,
    include_amendments: bool = False,
  ) -> list[EftsHit]:
    """Every filing matching the criteria, paged through EFTS.

    ``max_results`` caps the fetch; by default everything up to the EFTS
    ceiling is returned, with a warning when the query exceeds it.
    """
    return self.query_with_total(
      forms, start_date, end_date, ciks, text_query, max_results, include_amendments
    )[1]

  def query_with_total(
    self,
    forms: list[str] | tuple[str, ...] | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    ciks: list[str] | None = None,
    text_query: str | None = None,
    max_results: int | None = None,
    include_amendments: bool = False,
  ) -> tuple[int, list[EftsHit]]:
    """As :meth:`query`, and the total number of matches beside the page.

    A caller that shows a handful of hits still needs the size of what it is
    sampling — "8 of 3,412" is a different answer from "8".
    """
    params = self.build_params(
      forms, start_date, end_date, ciks, text_query, include_amendments
    )
    logger.info("EFTS query: forms=%s dates=%s..%s", forms, start_date, end_date)
    first = self._fetch_page(params, offset=0, size=1)
    total = int(first.get("hits", {}).get("total", {}).get("value", 0) or 0)
    if total == 0:
      return 0, []
    if total > EFTS_MAX_RESULTS:
      logger.warning(
        "EFTS query matches %d filings, over its %d limit; narrow the dates or forms",
        total,
        EFTS_MAX_RESULTS,
      )
    to_fetch = min(total, max_results or total, EFTS_MAX_RESULTS)
    hits: list[EftsHit] = []
    offset = 0
    while offset < to_fetch:
      size = min(EFTS_MAX_PAGE_SIZE, to_fetch - offset)
      page = self._fetch_page(params, offset=offset, size=size)
      page_hits = page.get("hits", {}).get("hits", []) or []
      hits.extend(EftsHit.from_hit(hit) for hit in page_hits)
      offset += len(page_hits)
      if len(page_hits) < size:
        break
    logger.info("EFTS query complete: %d filings", len(hits))
    return total, hits[:to_fetch]

  def query_by_year(
    self,
    year: int,
    forms: list[str] | tuple[str, ...] | None = None,
    ciks: list[str] | None = None,
  ) -> list[EftsHit]:
    """A calendar year in one query. Often over the 10k ceiling for the main
    forms; prefer :meth:`query_by_quarter` for a corpus."""
    return self.query(
      forms=forms or DEFAULT_FORMS,
      start_date=f"{year}-01-01",
      end_date=f"{year}-12-31",
      ciks=ciks,
    )

  def query_by_quarter(
    self,
    year: int,
    quarter: int,
    forms: list[str] | tuple[str, ...] | None = None,
    ciks: list[str] | None = None,
  ) -> list[EftsHit]:
    """A calendar quarter — the partition a corpus is discovered in."""
    if quarter not in _QUARTERS:
      raise ValueError(f"Quarter must be 1-4, got {quarter}")
    start, end = _QUARTERS[quarter]
    return self.query(
      forms=forms or DEFAULT_FORMS,
      start_date=f"{year}-{start}",
      end_date=f"{year}-{end}",
      ciks=ciks,
    )


def query_efts(
  forms: list[str] | tuple[str, ...] | None = None,
  start_date: str | None = None,
  end_date: str | None = None,
  ciks: list[str] | None = None,
  text_query: str | None = None,
  config: Config = CONFIG,
) -> list[EftsHit]:
  """Convenience: one query with a throwaway client."""
  return EftsClient(config).query(forms, start_date, end_date, ciks, text_query)


def _retry_after(resp: requests.Response) -> float:
  value = resp.headers.get("Retry-After", "60")
  try:
    return min(float(value), MAX_RETRY_AFTER)
  except (TypeError, ValueError):
    return 60.0


__all__ = (
  "DEFAULT_FORMS",
  "EFTS_MAX_PAGE_SIZE",
  "EFTS_MAX_RESULTS",
  "EftsClient",
  "EftsHit",
  "query_efts",
)
