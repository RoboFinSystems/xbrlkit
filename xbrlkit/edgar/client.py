"""EdgarClient — a small synchronous SEC EDGAR client.

The client layer the RoboSystems SEC adapter runs on: ticker→CIK resolution,
the submissions header and its pagination, the company-tickers map. Platform-
free: it reads all settings from :class:`xbrlkit.config.Config`, uses
``requests`` synchronously, and throttles every call through a
:class:`~xbrlkit.edgar.rate_limit.RateLimiter`.

EDGAR's throttle looks two ways — an HTTP 429 (or a 503 in an outage), and an
empty ``200`` body where JSON was expected. :meth:`EdgarClient._get` treats
both alike: wait (``Retry-After`` when given, else
:attr:`Config.throttle_backoff_s`) and retry a bounded number of times, then
raise :class:`EdgarThrottled`.
"""

from __future__ import annotations

import email.utils
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import requests

from xbrlkit.config import CONFIG, Config

from .rate_limit import RateLimiter

COMPANY_TICKERS_PATH = "/files/company_tickers.json"

logger = logging.getLogger(__name__)

RETRY_STATUSES = (429, 503)
MAX_RETRIES = 2
MAX_RETRY_AFTER = 300.0


class EdgarThrottled(requests.HTTPError):
  """EDGAR kept throttling after the bounded retries."""


@dataclass
class FilingRef:
  """One filing's identity, enough to build its Archives URL and load it."""

  cik: str
  accession: str
  form: str
  filing_date: str
  primary_document: str
  is_inline: bool
  report_date: str = ""
  acceptance_datetime: str = ""
  # Whether EDGAR holds XBRL for this filing at all. False for the forms that
  # are only a document — most 8-Ks, DEF 14A, S-1, everything before the 2009
  # mandate — which have no `-xbrl.zip` to download and load as document-only.
  is_xbrl: bool = True


@dataclass
class CompanyInfo:
  """Filer identity from the submissions header (not from the XBRL instance)."""

  cik: str
  name: str | None
  ein: str | None
  ticker: str | None
  exchange: str | None = None
  sic: str | None = None
  sic_description: str | None = None
  category: str | None = None
  state_of_incorporation: str | None = None
  fiscal_year_end: str | None = None
  entity_type: str | None = None
  website: str | None = None
  phone: str | None = None


class EdgarClient:
  """Synchronous EDGAR client. One HTTP session, one rate limiter."""

  def __init__(self, config: Config = CONFIG) -> None:
    self.config: Config = config
    self._session: requests.Session = requests.Session()
    self._session.headers.update(config.headers)
    self._limiter: RateLimiter = RateLimiter(config.rate_limit_per_sec)
    self._ticker_map: dict[str, str] | None = None

  def _get(self, url: str, *, expect_body: bool = True) -> requests.Response:
    """Spaced GET that rides out EDGAR's throttle and raises on any other
    HTTP error.

    A 429 / 503 waits for ``Retry-After`` (bounded) and retries; an empty
    ``200`` body, which is how EDGAR throttles a JSON endpoint, waits
    :attr:`Config.throttle_backoff_s` and retries. After :data:`MAX_RETRIES`
    of either, :class:`EdgarThrottled`.
    """
    attempt = 0
    while True:
      self._limiter.wait()
      resp = self._session.get(url, timeout=self.config.request_timeout)
      throttled = resp.status_code in RETRY_STATUSES or (
        expect_body and resp.status_code == 200 and not resp.content.strip()
      )
      if not throttled:
        resp.raise_for_status()
        return resp
      if attempt >= MAX_RETRIES:
        raise EdgarThrottled(
          f"EDGAR throttled {url} (HTTP {resp.status_code}, "
          f"{len(resp.content)} bytes) after {attempt} retries",
          response=resp,
        )
      delay = _retry_after(resp, self.config.throttle_backoff_s)
      logger.warning(
        "EDGAR throttled %s (HTTP %s); waiting %.0fs (retry %d/%d)",
        url,
        resp.status_code,
        delay,
        attempt + 1,
        MAX_RETRIES,
      )
      time.sleep(delay)
      attempt += 1

  def company_tickers(self) -> dict[str, dict[str, object]]:
    """The raw ``company_tickers.json`` map — index → ``{cik_str, ticker,
    title}``, in EDGAR's market-cap order."""
    url = f"{self.config.sec_base_url}{COMPANY_TICKERS_PATH}"
    data = self._get(url).json()
    return data if isinstance(data, dict) else {}

  def ticker_to_cik(self, ticker: str) -> str:
    """Resolve a ticker symbol to its zero-padded 10-digit CIK.

    Fetches (and caches) the SEC ``company_tickers.json`` map. Raises
    :class:`LookupError` if the ticker is unknown.
    """
    if self._ticker_map is None:
      self._ticker_map = self._load_ticker_map()
    key = ticker.upper()
    cik = self._ticker_map.get(key)
    if cik is None:
      raise LookupError(f"Unknown ticker: {ticker}")
    return cik

  def _load_ticker_map(self) -> dict[str, str]:
    ticker_map: dict[str, str] = {}
    for row in self.company_tickers().values():
      symbol = str(row["ticker"]).upper()
      ticker_map[symbol] = f"{int(row['cik_str']):0>10}"
    return ticker_map

  def _get_submissions(self, name: str) -> dict[str, object]:
    url = f"{self.config.sec_data_url}/submissions/{name}"
    return self._get(url).json()

  def list_filings(self, cik: str, forms: list[str] | None = None) -> list[FilingRef]:
    """List a company's filings, newest-first, optionally filtered by form.

    Reads ``filings.recent`` from the main submissions file and merges every
    ``filings.files[].name`` pagination file. ``forms`` (e.g. ``["10-K"]``)
    filters by exact form type when given.
    """
    padded_cik = f"{int(cik):0>10}"
    main = self._get_submissions(f"CIK{padded_cik}.json")
    filings = main.get("filings", {})
    if not isinstance(filings, dict):
      filings = {}

    recent = filings.get("recent", {})
    refs: list[FilingRef] = []
    if isinstance(recent, dict):
      refs.extend(self._refs_from_arrays(padded_cik, recent))

    for file_info in filings.get("files", []) or []:
      name = file_info.get("name")
      if not name:
        continue
      page = self._get_submissions(name)
      refs.extend(self._refs_from_arrays(padded_cik, page))

    if forms is not None:
      wanted = set(forms)
      refs = [ref for ref in refs if ref.form in wanted]

    refs.sort(key=lambda ref: ref.filing_date, reverse=True)
    return refs

  @staticmethod
  def _refs_from_arrays(padded_cik: str, arrays: dict[str, object]) -> list[FilingRef]:
    accessions = arrays.get("accessionNumber") or []
    if not isinstance(accessions, list):
      return []
    forms = arrays.get("form") or []
    dates = arrays.get("filingDate") or []
    primary = arrays.get("primaryDocument") or []
    inline = arrays.get("isInlineXBRL") or []
    xbrl = arrays.get("isXBRL") or []
    report_dates = arrays.get("reportDate") or []
    accepted = arrays.get("acceptanceDateTime") or []

    def at(seq: object, i: int) -> object:
      return seq[i] if isinstance(seq, list) and i < len(seq) else None

    refs: list[FilingRef] = []
    for i in range(len(accessions)):
      refs.append(
        FilingRef(
          cik=padded_cik,
          accession=str(accessions[i]),
          form=str(at(forms, i) or ""),
          filing_date=str(at(dates, i) or ""),
          primary_document=str(at(primary, i) or ""),
          is_inline=bool(at(inline, i)),
          report_date=str(at(report_dates, i) or ""),
          acceptance_datetime=str(at(accepted, i) or ""),
          is_xbrl=bool(at(xbrl, i)) or bool(at(inline, i)),
        )
      )
    return refs

  def company_info(self, cik: str) -> CompanyInfo:
    """Filer name / EIN / primary ticker from the submissions header.

    These identify the reporting entity and come from EDGAR metadata, not the
    XBRL instance (which only carries the CIK). Best-effort: unknown fields are
    ``None``.
    """
    padded_cik = f"{int(cik):0>10}"
    main = self._get_submissions(f"CIK{padded_cik}.json")
    return company_info_from_submissions(padded_cik, main)

  def submissions(self, cik: str) -> dict[str, object]:
    """The raw submissions header document for a CIK (``CIK##########.json``)."""
    padded_cik = f"{int(cik):0>10}"
    return self._get_submissions(f"CIK{padded_cik}.json")

  def complete_submissions(self, cik: str) -> dict[str, object]:
    """The submissions header with *every* filing merged into ``filings`` —
    the recent page plus each pagination file, in one flat set of arrays —
    and a ``_metadata`` block (``totalFilings``, ``lastUpdated``,
    ``paginationFilesMerged``). The shape a corpus snapshot stores.
    """
    padded_cik = f"{int(cik):0>10}"
    main = self._get_submissions(f"CIK{padded_cik}.json")
    result: dict[str, object] = {k: v for k, v in main.items() if k != "filings"}
    filings = main.get("filings")
    filings = filings if isinstance(filings, dict) else {}
    recent = filings.get("recent")
    recent = recent if isinstance(recent, dict) else {}
    fields = list(recent.keys())
    merged: dict[str, list[object]] = {
      name: list(recent.get(name) or []) for name in fields
    }
    pages = filings.get("files") or []
    pages = pages if isinstance(pages, list) else []
    merged_pages = 0
    for page_info in pages:
      name = page_info.get("name") if isinstance(page_info, dict) else None
      if not name:
        continue
      try:
        page = self._get_submissions(str(name))
      except requests.RequestException as exc:
        logger.warning("submissions page %s for CIK %s failed: %s", name, cik, exc)
        continue
      for field_name in fields:
        values = page.get(field_name)
        if isinstance(values, list):
          merged[field_name].extend(values)
      merged_pages += 1
    result["filings"] = merged
    result["_metadata"] = {
      "totalFilings": len(merged.get("accessionNumber", [])),
      "lastUpdated": datetime.now(timezone.utc).isoformat(),
      "paginationFilesMerged": merged_pages,
    }
    return result

  def get_filing_ref(self, cik: str, accession: str) -> FilingRef:
    """Return the :class:`FilingRef` for one accession.

    Falls back to a minimal ref (form/date unknown, ``is_inline=True``,
    ``is_xbrl=True``) when the accession is not found in the submissions
    history, so downloads can still proceed by URL construction alone — an
    unknown filing is tried as XBRL and falls back to document-only if it
    turns out to have none.
    """
    padded_cik = f"{int(cik):0>10}"
    for ref in self.list_filings(cik):
      if ref.accession == accession:
        return ref
    return FilingRef(
      cik=padded_cik,
      accession=accession,
      form="",
      filing_date="",
      primary_document="",
      is_inline=True,
    )


def _retry_after(resp: requests.Response, default: float) -> float:
  """The wait a throttled response asks for: ``Retry-After`` as seconds or an
  HTTP date, else ``default``; never more than :data:`MAX_RETRY_AFTER`."""
  value = resp.headers.get("Retry-After") if resp.headers is not None else None
  delay: float | None = None
  if value:
    text = str(value).strip()
    if text.isdigit():
      delay = float(text)
    else:
      parsed = email.utils.parsedate_to_datetime(text) if text else None
      if parsed is not None:
        delay = max(0.0, parsed.timestamp() - time.time())
  if delay is None:
    delay = default
  return min(delay, MAX_RETRY_AFTER)


def _first(value: object) -> str | None:
  if isinstance(value, list) and value:
    return str(value[0]) if value[0] is not None else None
  return None


def _text(value: object) -> str | None:
  """A header value as text. EDGAR writes an unknown value as ``""`` as often
  as ``null``; both are kept as they come, since the platform stores them as
  they come and a projection should read the same."""
  return None if value is None else str(value)


def company_info_from_submissions(cik: str, main: dict[str, object]) -> CompanyInfo:
  """Build :class:`CompanyInfo` from a submissions header document."""
  return CompanyInfo(
    cik=cik,
    name=_text(main.get("name")),
    ein=_text(main.get("ein")) or None,
    ticker=_first(main.get("tickers")),
    exchange=_first(main.get("exchanges")),
    sic=_text(main.get("sic")),
    sic_description=_text(main.get("sicDescription")),
    category=_text(main.get("category")),
    state_of_incorporation=_text(main.get("stateOfIncorporation")),
    fiscal_year_end=_text(main.get("fiscalYearEnd")),
    entity_type=_text(main.get("entityType")),
    # An empty website is unknown, not "": the platform's fallback chain ends in
    # None for it, where every other empty header value is kept as "".
    website=_text(main.get("website") or main.get("investorWebsite")) or None,
    phone=_text(main.get("phone")),
  )
