"""Light unit tests for the ``parse`` module.

A full parse needs a real filing (heavy — that's Wave 2 integration), so these
tests exercise only the pure helpers: deterministic ids, the instant/duration
date normalization (``- timedelta(1)``), and the measure→token resolver. No
network access, no Arelle model load.
"""

from __future__ import annotations

from datetime import date, datetime

from xbrlkit.parse import ids
from xbrlkit.parse.to_model import (
  _make_period,
  _make_unit,
  _measure_token,
  _normalize_cik,
)


def test_parse_package_imports():
  """The public surface imports without touching the network."""
  from xbrlkit.parse import (
    close,
    load_model,
    to_xbrl_model,
  )

  assert callable(load_model)
  assert callable(to_xbrl_model)
  assert callable(close)


# --- deterministic ids ------------------------------------------------------


def test_ids_are_stable():
  """Same input → same id, every call."""
  assert ids.period_id("iso#2024-12-31") == ids.period_id("iso#2024-12-31")
  assert ids.unit_id("iso4217#USD") == ids.unit_id("iso4217#USD")
  assert ids.element_id("ns", "Assets") == ids.element_id("ns", "Assets")


def test_ids_look_like_uuids():
  """Ids are 36-char UUID strings."""
  value = ids.period_id("iso#2024-12-31")
  assert len(value) == 36
  assert value.count("-") == 4


def test_ids_differ_by_kind():
  """Different node kinds never collide on identical content."""
  content = "same-content"
  assert ids.period_id(content) != ids.unit_id(content)
  assert ids.unit_id(content) != ids.entity_id(content)


def test_ids_differ_by_content():
  """Different content yields different ids within a kind."""
  assert ids.period_id("a") != ids.period_id("b")


def test_fact_id_is_report_scoped():
  """Facts with the same md5 in different reports do not collide."""
  a = ids.fact_id("accession-A", "deadbeef")
  b = ids.fact_id("accession-B", "deadbeef")
  assert a != b


# --- period normalization (Arelle exclusive next-midnight) ------------------


class _FakeInstantCtx:
  isInstantPeriod = True
  isStartEndPeriod = False
  isForeverPeriod = False
  instantDatetime = datetime(2025, 1, 1)


class _FakeDurationCtx:
  isInstantPeriod = False
  isStartEndPeriod = True
  isForeverPeriod = False
  startDatetime = datetime(2024, 1, 1)
  endDatetime = datetime(2025, 1, 1)


class _FakeForeverCtx:
  isInstantPeriod = False
  isStartEndPeriod = False
  isForeverPeriod = True


class _FakeUnknownCtx:
  isInstantPeriod = False
  isStartEndPeriod = False
  isForeverPeriod = False


def test_instant_period_rolls_back_one_day():
  """An instant's exclusive next-midnight becomes the reported date."""
  period = _make_period(_FakeInstantCtx())
  assert period is not None
  assert period.period_type == "instant"
  assert period.start is None
  assert period.end == date(2024, 12, 31)


def test_duration_period_normalizes_end():
  """A duration keeps its start and rolls its end back one day."""
  period = _make_period(_FakeDurationCtx())
  assert period is not None
  assert period.period_type == "duration"
  assert period.start == date(2024, 1, 1)
  assert period.end == date(2024, 12, 31)


def test_forever_period():
  period = _make_period(_FakeForeverCtx())
  assert period is not None
  assert period.period_type == "forever"
  assert period.start is None
  assert period.end is None


def test_unknown_period_is_none():
  assert _make_period(_FakeUnknownCtx()) is None


def test_instant_period_id_is_stable():
  """The instant period id derives from its normalized date."""
  period = _make_period(_FakeInstantCtx())
  assert period is not None
  assert period.id == ids.period_id(
    "http://www.w3.org/2001/XMLSchema#dateTime#2024-12-31"
  )


# --- measure → token resolver -----------------------------------------------


class _FakeQName:
  def __init__(self, prefix, local, namespace):
    self.prefix = prefix
    self.localName = local
    self.namespaceURI = namespace


def test_measure_token_with_prefix():
  token, uri = _measure_token(
    _FakeQName("iso4217", "USD", "http://www.xbrl.org/2003/iso4217")
  )
  assert token == "iso4217:USD"
  assert uri == "http://www.xbrl.org/2003/iso4217#USD"


def test_measure_token_without_prefix():
  token, uri = _measure_token(_FakeQName(None, "pure", ""))
  assert token == "pure"
  assert uri == "pure"


class _FakeSingleUnit:
  isSingleMeasure = True
  isDivide = False
  measures = ([_FakeQName("iso4217", "USD", "http://www.xbrl.org/2003/iso4217")], [])


class _FakeDivideUnit:
  isSingleMeasure = False
  isDivide = True
  measures = (
    [_FakeQName("iso4217", "USD", "http://www.xbrl.org/2003/iso4217")],
    [_FakeQName("xbrli", "shares", "http://www.xbrl.org/2003/instance")],
  )


def test_make_unit_single_measure():
  unit = _make_unit(_FakeSingleUnit())
  assert unit is not None
  assert unit.measure == "iso4217:USD"
  assert unit.numerator_uri is None
  assert unit.denominator_uri is None
  assert unit.id == ids.unit_id("http://www.xbrl.org/2003/iso4217#USD")


def test_make_unit_divide():
  unit = _make_unit(_FakeDivideUnit())
  assert unit is not None
  assert unit.measure == "iso4217:USD/xbrli:shares"
  assert unit.numerator_uri == "http://www.xbrl.org/2003/iso4217#USD"
  assert unit.denominator_uri == "http://www.xbrl.org/2003/instance#shares"


# --- cik normalization ------------------------------------------------------


def test_normalize_cik_pads_numeric():
  assert _normalize_cik("320193") == "0000320193"
  assert _normalize_cik("0000320193") == "0000320193"


def test_normalize_cik_passes_non_numeric():
  assert _normalize_cik("ABC-123") == "ABC-123"


def test_calendar_enrichment():
  from xbrlkit.periods import (
    duration_calendar as _duration_calendar,
  )
  from xbrlkit.periods import (
    instant_calendar as _instant_calendar,
  )

  assert _instant_calendar(date(2026, 1, 25)) == (2026, "Q1", "2026-01-25")
  assert _duration_calendar(date(2025, 1, 27), date(2026, 1, 25)) == (
    "annual",
    2026,
    "FY",
    "2026",
  )
  assert _duration_calendar(date(2025, 10, 27), date(2026, 1, 25)) == (
    "quarterly",
    2026,
    "Q1",
    "2026Q1",
  )
  dtype, _year, quarter, _key = _duration_calendar(
    date(2025, 12, 1), date(2025, 12, 31)
  )
  assert dtype == "other" and quarter is None


def test_sec_ixt_transforms_register():
  # The vendored EDGAR/transform registry must wire the SEC ixt namespace into
  # Arelle's lookup, or SEC-formatted cover-page/DEI facts (state/country codes,
  # word-numbers) parse to (ixTransformValueError). No network, no model load.
  from arelle import FunctionIxt

  from xbrlkit.parse import register_sec_transforms
  from xbrlkit.parse.arelle_load import SEC_IXT_NAMESPACE

  register_sec_transforms()
  registry = FunctionIxt.ixtNamespaceFunctions.get(SEC_IXT_NAMESPACE, {})
  assert "stateprovnameen" in registry
  assert "edgarprovcountryen" in registry
  assert callable(registry["stateprovnameen"])
  # Public for hosts with their own controller; a second call is a no-op.
  register_sec_transforms()
  assert FunctionIxt.ixtNamespaceFunctions[SEC_IXT_NAMESPACE] is not None
  assert len(FunctionIxt.ixtNamespaceFunctions[SEC_IXT_NAMESPACE]) == len(registry)
