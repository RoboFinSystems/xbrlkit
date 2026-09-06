"""Value literals the OIM-family projections share.

xBRL-JSON (REC 2021) and Tavi (PWD 2026-09-01) are two serialisations of the
same Open Information Model, and the literal forms below are where they agree:
a period is an ISO 8601 interval of ``xs:dateTime`` values with an exclusive
end, a language tag is lower case, and an entity is written scheme-first: the
SEC scheme under the ``cik`` prefix, any other under ``entity``. Keeping them
in one place means a fact's period or entity reads identically in both
projections of one filing — which is also what lets one be checked against
the other.
"""

from __future__ import annotations

from datetime import date, timedelta

from ..model import EntityIdentity, Period

# The SEC's entity scheme, bound to the prefix Arelle uses for it. An entity is
# identified by scheme + identifier, so the scheme is the namespace and the
# CIK the local name: ``cik:0000066740``.
CIK_PREFIX = "cik"
CIK_SCHEME = "http://www.sec.gov/CIK"
# Any other scheme binds under a neutral prefix. A model that did not come from
# an EDGAR filing — a ledger's own report, say — still identifies its entity by
# scheme + identifier, and writes it ``entity:<identifier>``.
ENTITY_PREFIX = "entity"


def entity_prefix(entity: EntityIdentity) -> tuple[str, str]:
  """The ``(prefix, scheme)`` pair an entity's SQName is bound under."""
  if entity.scheme == CIK_SCHEME:
    return CIK_PREFIX, CIK_SCHEME
  return ENTITY_PREFIX, entity.scheme


def entity_sqname(entity: EntityIdentity) -> str:
  """``cik:0000066740`` for an SEC filer; ``entity:<id>`` under any other scheme."""
  prefix, _ = entity_prefix(entity)
  return f"{prefix}:{entity.cik}"


def period_interval(period: Period) -> str:
  """A period as an interval of ``xs:dateTime`` values, end exclusive.

  An instant at the close of 2024-12-31 is ``2025-01-01T00:00:00``; the 2024
  calendar year is ``2024-01-01T00:00:00/2025-01-01T00:00:00``. The parse rolls
  Arelle's next-midnight back by a day into a human-facing date, so this rolls
  it forward again. Tavi's ``xbrlr:periodString`` says the time component
  cannot be omitted, which is why the projections do not write bare dates.
  """
  if period.period_type == "instant":
    return exclusive_end(period.end or period.start)
  if period.period_type == "forever":
    return "0001-01-01T00:00:00/9999-12-31T00:00:00"
  return f"{midnight(period.start)}/{exclusive_end(period.end)}"


def midnight(value: date | None) -> str:
  return f"{value.isoformat()}T00:00:00" if value else ""


def exclusive_end(value: date | None) -> str:
  return midnight(value + timedelta(days=1)) if value else ""


def language_tag(value: str | None) -> str | None:
  """A BCP 47 tag in the lower-case form xBRL-JSON requires.

  Tags are case-insensitive, so nothing is lost; ``en-US`` and ``en-us`` name
  the same language. xBRL-JSON makes the lower-case form mandatory
  (``xbrlje:invalidLanguageCodeCase``), and Arelle applies the same check to a
  Tavi fact, so both projections write it that way.
  """
  return value.lower() if value else None
