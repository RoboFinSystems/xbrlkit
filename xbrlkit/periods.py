"""Reporting periods, built the same way by every source of an ``XbrlModel``.

Three sources mint periods: the Arelle parse (:mod:`xbrlkit.parse.to_model`)
and the two importers (:mod:`xbrlkit.deserialize`). A period's ``id`` is
content-derived so periods dedupe across filings, which means all three have
to agree exactly — one of them rounding a date or bucketing a span differently
would give the same filing, read two ways, two period lists.

The calendar fields are the deterministic enrichment :class:`~xbrlkit.model.Period`
documents: derived from the dates, never read from the filing. That is why a
serialization which drops them loses nothing — TAVI writes a bare ISO interval
and the TAVI importer recomputes them here, from the same function that wrote
them in the first place.
"""

from __future__ import annotations

from datetime import date, timedelta

from .model import Period
from .parse.ids import period_id

# Content-URI stem for period ids (matches the SEC adapter's ISO 8601 stem so
# the derived period ids are stable and portable).
ISO_8601_URI = "http://www.w3.org/2001/XMLSchema#dateTime"

# The interval the OIM-family projections write for an XBRL forever period.
FOREVER_INTERVAL = "0001-01-01T00:00:00/9999-12-31T00:00:00"


def quarter_of_month(month: int) -> str:
  """Calendar quarter (Q1-Q4) for a month — calendar, not fiscal."""
  if month <= 3:
    return "Q1"
  if month <= 6:
    return "Q2"
  if month <= 9:
    return "Q3"
  return "Q4"


def instant_calendar(end: date) -> tuple[int, str, str]:
  """Calendar enrichment for an instant period: (year, quarter, key)."""
  return end.year, quarter_of_month(end.month), end.isoformat()


def duration_calendar(start: date, end: date) -> tuple[str, int, str | None, str]:
  """Calendar enrichment for a duration: (duration_type, year, quarter, key).

  Mirrors the SEC adapter's ``make_period`` day-count buckets (``end`` is the
  inclusive reported end, so the span is ``(end - start).days + 1``) so the
  values match the graph: quarterly ~ 13 wk, semi_annual ~ 6 mo, nine_months ~
  9 mo, annual ~ 52/53 wk, else ``other``. ``calendar_period_key`` is a compact
  label — ``2026`` (annual), ``2026Q1`` (else a quarter), or ``start/end``.
  """
  days = (end - start).days + 1
  year = end.year
  if 80 <= days <= 100:
    dtype, quarter = "quarterly", quarter_of_month(end.month)
  elif 170 <= days <= 190:
    dtype, quarter = "semi_annual", ("H1" if end.month in (4, 5, 6, 7) else "H2")
  elif 260 <= days <= 280:
    dtype, quarter = "nine_months", "M9"
  elif 350 <= days <= 380:
    dtype, quarter = "annual", "FY"
  else:
    dtype, quarter = "other", None
  if dtype == "annual":
    key = str(year)
  elif quarter is not None:
    key = f"{year}{quarter}"
  else:
    key = f"{start.isoformat()}/{end.isoformat()}"
  return dtype, year, quarter, key


def instant_period(end: date) -> Period:
  """The instant period reported *as of* ``end`` (the inclusive date)."""
  year, quarter, key = instant_calendar(end)
  return Period(
    id=period_id(f"{ISO_8601_URI}#{end.isoformat()}"),
    period_type="instant",
    start=None,
    end=end,
    calendar_year=year,
    calendar_quarter=quarter,
    calendar_period_key=key,
  )


def duration_period(start: date, end: date) -> Period:
  """The duration period from ``start`` to ``end``, both inclusive dates."""
  dtype, year, quarter, key = duration_calendar(start, end)
  return Period(
    id=period_id(f"{ISO_8601_URI}#{start.isoformat()}/{end.isoformat()}"),
    period_type="duration",
    start=start,
    end=end,
    duration_type=dtype,
    calendar_year=year,
    calendar_quarter=quarter,
    calendar_period_key=key,
  )


def forever_period() -> Period:
  """XBRL's forever period, which carries no dates and no calendar placement."""
  return Period(
    id=period_id(f"{ISO_8601_URI}#Forever"),
    period_type="forever",
    start=None,
    end=None,
  )


def period_from_interval(literal: str) -> Period | None:
  """A period from an OIM-family period literal, or ``None`` if unreadable.

  The literal is an ISO 8601 interval of ``xs:dateTime`` values with an
  **exclusive** end — ``2025-01-01T00:00:00`` is the close of 2024-12-31 —
  which is what :func:`xbrlkit.serialize._values.period_interval` writes and
  what Arelle's own converter writes. A bare date with no time component is
  read as the inclusive date instead: earlier emitters wrote periods that way
  and the two forms are distinguishable, so both round-trip to the same period.
  """
  text = literal.strip()
  if not text:
    return None
  if text == FOREVER_INTERVAL:
    return forever_period()
  start_text, sep, end_text = text.partition("/")
  end = _endpoint(end_text if sep else start_text)
  if end is None:
    return None
  if not sep:
    return instant_period(end)
  start = _endpoint(start_text, exclusive=False)
  if start is None:
    return None
  return duration_period(start, end)


def _endpoint(text: str, *, exclusive: bool = True) -> date | None:
  """One end of an interval as an inclusive date.

  A value with a time component is exclusive (the next midnight) and is rolled
  back a day; a bare date is already inclusive. A start is never rolled back:
  midnight at the start of a day is that day.
  """
  text = text.strip()
  if not text:
    return None
  has_time = "T" in text
  try:
    value = date.fromisoformat(text[:10])
  except ValueError:
    return None
  return value - timedelta(days=1) if has_time and exclusive else value
