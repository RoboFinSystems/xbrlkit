"""The public index of filings that are not on EDGAR.

`filings.xbrl.org <https://filings.xbrl.org>`_ is XBRL International's index of
filings outside the SEC — ESEF annual financial reports and the national
regimes that publish through it. It is open, it asks for no key, and it speaks
JSON:API, so one adapter reaches every country in it at once rather than one
per national filing authority.

What it is *not* is a complete picture of Europe. Germany has nothing in it —
the Bundesanzeiger does not share — and two of the largest slices cannot be
loaded even though they are indexed, because they are national-GAAP filings
whose national taxonomy host has moved or gone: Ukraine's does not resolve at
all and Denmark's entry point answers 404. The self-contained ESEF packages,
which is most of the rest, load.

The identifier here is the **LEI**, not a ticker or a CIK, and a filing's own
id is the index's ``fxo_id`` — entity, period, taxonomy, country and a
sequence, e.g. ``213800H2PQMIF3OVZY47-2022-03-31-ESEF-GB-0``.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import date
from typing import Any
from urllib.parse import urlencode

import requests

from xbrlkit.config import CONFIG, Config

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EntityRecord:
  """A filer as the index knows it: an LEI and a name."""

  identifier: str
  name: str = ""


@dataclass(frozen=True)
class FilingRecord:
  """One filing in the index, and where its files are."""

  fxo_id: str
  country: str = ""
  period_end: str = ""
  package_url: str = ""
  report_url: str = ""
  json_url: str = ""
  entity: EntityRecord | None = None
  error_count: int = 0
  inconsistency_count: int = 0

  @property
  def entity_identifier(self) -> str:
    return self.entity.identifier if self.entity else ""

  @property
  def has_package(self) -> bool:
    """Whether the filing ships as a taxonomy package.

    Without one the report must resolve its taxonomy over the network, which
    is exactly what fails for the national-GAAP regimes whose host is gone.
    """
    return bool(self.package_url)


class FilingsOrgClient:
  """Read-only client for the filings.xbrl.org JSON:API."""

  def __init__(self, config: Config = CONFIG, per_sec: float | None = None) -> None:
    self.config = config
    self._min_interval = 1.0 / (per_sec or config.rate_limit_per_sec)
    self._last_request = 0.0
    self._session = requests.Session()

  # -- transport --------------------------------------------------------------

  def _get(self, path: str, **query: str) -> dict[str, Any]:
    """One GET against the index, paced and with JSON:API's own media type."""
    url = f"{self.config.filings_base_url}{path}"
    if query:
      url = f"{url}?{urlencode(query)}"
    wait = self._min_interval - (time.monotonic() - self._last_request)
    if wait > 0:
      time.sleep(wait)
    resp = self._session.get(
      url,
      headers={**self.config.headers, "Accept": "application/vnd.api+json"},
      timeout=self.config.request_timeout,
    )
    self._last_request = time.monotonic()
    resp.raise_for_status()
    return resp.json()

  # -- reads ------------------------------------------------------------------

  def filings(
    self,
    country: str | None = None,
    period_end: str | None = None,
    limit: int = 25,
    newest_first: bool = True,
  ) -> list[FilingRecord]:
    """Filings in the index, most recent period first by default."""
    query: dict[str, str] = {"page[size]": str(max(1, limit)), "include": "entity"}
    if country:
      query["filter[country]"] = country.upper()
    if period_end:
      query["filter[period_end]"] = period_end
    if newest_first:
      query["sort"] = "-period_end"
    return _records(self._get("/api/filings", **query))

  def filing(self, fxo_id: str) -> FilingRecord:
    """One filing by its index id."""
    found = _records(
      self._get("/api/filings", **{"filter[fxo_id]": fxo_id, "include": "entity"})
    )
    if not found:
      raise LookupError(f"No filing {fxo_id!r} in the index at filings.xbrl.org")
    return found[0]

  def entity(self, lei: str) -> EntityRecord:
    """A filer by LEI."""
    payload = self._get(f"/api/entities/{lei}")
    data = payload.get("data")
    if not isinstance(data, dict):
      raise LookupError(f"No entity {lei!r} in the index at filings.xbrl.org")
    return _entity(data)

  def entity_filings(self, lei: str, limit: int = 25) -> list[FilingRecord]:
    """Everything one filer has filed, most recent period first."""
    payload = self._get(f"/api/entities/{lei}/filings", **{"page[size]": str(limit)})
    found = _records(payload)
    known = self.entity(lei)
    dated = sorted(found, key=lambda f: f.period_end or "", reverse=True)
    return [
      FilingRecord(**{**vars(record), "entity": record.entity or known})
      for record in dated
    ]

  def latest_filing(self, lei: str, today: date | None = None) -> FilingRecord:
    """The filer's most recent filing.

    A period that has not ended yet is not the latest filing, it is a mistake:
    one Finnish filer's index entry reports a period ending in 2031, and
    sorting on the period alone hands that back as their newest report. Future
    periods are passed over unless every filing has one.
    """
    found = self.entity_filings(lei, limit=100)
    if not found:
      raise LookupError(f"{lei} has no filings in the index at filings.xbrl.org")
    now = (today or date.today()).isoformat()
    ended = [f for f in found if f.period_end and f.period_end <= now]
    return ended[0] if ended else found[0]


# -- JSON:API shapes -------------------------------------------------------------


def _records(payload: dict[str, Any]) -> list[FilingRecord]:
  """The filings in one JSON:API payload, with their entities attached."""
  entities = {
    item.get("id"): _entity(item)
    for item in payload.get("included") or []
    if item.get("type") == "entity"
  }
  data = payload.get("data")
  rows = data if isinstance(data, list) else [data] if isinstance(data, dict) else []
  found: list[FilingRecord] = []
  for row in rows:
    if not isinstance(row, dict):
      continue
    attributes = row.get("attributes") or {}
    link = ((row.get("relationships") or {}).get("entity") or {}).get("data") or {}
    found.append(
      FilingRecord(
        fxo_id=str(attributes.get("fxo_id") or ""),
        country=str(attributes.get("country") or ""),
        period_end=str(attributes.get("period_end") or ""),
        package_url=str(attributes.get("package_url") or ""),
        report_url=str(attributes.get("report_url") or ""),
        json_url=str(attributes.get("json_url") or ""),
        entity=entities.get(link.get("id")),
        error_count=int(attributes.get("error_count") or 0),
        inconsistency_count=int(attributes.get("inconsistency_count") or 0),
      )
    )
  return found


def _entity(item: dict[str, Any]) -> EntityRecord:
  attributes = item.get("attributes") or {}
  return EntityRecord(
    identifier=str(attributes.get("identifier") or item.get("id") or ""),
    name=str(attributes.get("name") or ""),
  )


__all__ = ["EntityRecord", "FilingRecord", "FilingsOrgClient"]
