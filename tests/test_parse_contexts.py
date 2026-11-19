"""Facts the parse cannot place: counted, never dropped silently, and kept when
the filing states the period of an undeclared context itself.

The instances are synthetic and load offline: a three-concept schema and an
instance whose facts name context ids it never declares, the pattern some
Indian exchange results filings follow (``OneD`` for the quarter, ``FourD`` for
the year to date).
"""

from __future__ import annotations

import logging
from datetime import date
from pathlib import Path

import pytest

from xbrlkit.model import RECOVERED_CONTEXT, FilingMeta, XbrlModel
from xbrlkit.parse import close, load_model, to_xbrl_model

SCHEMA = """<?xml version="1.0" encoding="UTF-8"?>
<xs:schema xmlns:xs="http://www.w3.org/2001/XMLSchema"
    xmlns:xbrli="http://www.xbrl.org/2003/instance" xmlns:t="http://example.com/t"
    targetNamespace="http://example.com/t" elementFormDefault="qualified">
  <xs:import namespace="http://www.xbrl.org/2003/instance"
      schemaLocation="http://www.xbrl.org/2003/xbrl-instance-2003-12-31.xsd"/>
  <xs:element name="Revenue" id="t_Revenue" type="xbrli:monetaryItemType"
      substitutionGroup="xbrli:item" nillable="true" xbrli:periodType="duration"/>
  <xs:element name="Assets" id="t_Assets" type="xbrli:monetaryItemType"
      substitutionGroup="xbrli:item" nillable="true" xbrli:periodType="instant"/>
  <xs:element name="DateOfStartOfReportingPeriod" id="t_S" type="xbrli:dateItemType"
      substitutionGroup="xbrli:item" nillable="true" xbrli:periodType="duration"/>
  <xs:element name="DateOfEndOfReportingPeriod" id="t_E" type="xbrli:dateItemType"
      substitutionGroup="xbrli:item" nillable="true" xbrli:periodType="duration"/>
</xs:schema>
"""

HEAD = """<?xml version="1.0" encoding="UTF-8"?>
<xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance"
    xmlns:link="http://www.xbrl.org/2003/linkbase"
    xmlns:xlink="http://www.w3.org/1999/xlink" xmlns:t="http://example.com/t"
    xmlns:iso4217="http://www.xbrl.org/2003/iso4217">
<link:schemaRef xlink:type="simple" xlink:href="t.xsd"/>
<xbrli:unit id="INR"><xbrli:measure>iso4217:INR</xbrli:measure></xbrli:unit>
"""


def _context(context_id: str, entity: str, start: str, end: str) -> str:
  return (
    f'<xbrli:context id="{context_id}"><xbrli:entity>'
    f'<xbrli:identifier scheme="http://example.com/id">{entity}</xbrli:identifier>'
    f"</xbrli:entity><xbrli:period><xbrli:startDate>{start}</xbrli:startDate>"
    f"<xbrli:endDate>{end}</xbrli:endDate></xbrli:period></xbrli:context>\n"
  )


def _dates(context_id: str, start: str, end: str) -> str:
  return (
    f'<t:DateOfStartOfReportingPeriod contextRef="{context_id}">{start}'
    "</t:DateOfStartOfReportingPeriod>\n"
    f'<t:DateOfEndOfReportingPeriod contextRef="{context_id}">{end}'
    "</t:DateOfEndOfReportingPeriod>\n"
  )


def _fact(name: str, context_id: str, value: int) -> str:
  return (
    f'<t:{name} contextRef="{context_id}" unitRef="INR" decimals="0">{value}'
    f"</t:{name}>\n"
  )


# The quarter and the year to date, neither declared; one declared context
# that dates a year-to-date breakdown as the quarter, as the real filings do.
RESULTS = (
  _context("FourDetailD", "ACME", "2022-10-01", "2022-12-31")
  + _dates("OneD", "2022-10-01", "2022-12-31")
  + _dates("FourD", "2022-04-01", "2022-12-31")
  + _fact("Revenue", "OneD", 100)
  + _fact("Revenue", "FourD", 300)
  + _fact("Revenue", "FourDetailD", 300)
)


def _parse(tmp_path: Path, body: str) -> XbrlModel:
  (tmp_path / "t.xsd").write_text(SCHEMA)
  instance = tmp_path / "i.xml"
  instance.write_text(HEAD + body + "</xbrli:xbrl>\n")
  mx = load_model(str(instance), cache_dir=tmp_path / "cache", offline=True)
  try:
    return to_xbrl_model(mx, FilingMeta(accession="acc", cik="0000000042"))
  finally:
    close(mx.modelManager.cntlr)


def _by_context_value(model: XbrlModel) -> dict[str, tuple[date | None, date | None]]:
  periods = {p.id: p for p in model.periods}
  return {
    f.value_str or "": (periods[f.period_id].start, periods[f.period_id].end)
    for f in model.facts
    if f.concept_qname == "t:Revenue" and f.provenance is not None
  }


def test_undeclared_contexts_are_rebuilt_from_the_filings_own_dates(tmp_path: Path):
  model = _parse(tmp_path, RESULTS)

  assert len(model.facts) == 7
  assert _by_context_value(model) == {
    "100": (date(2022, 10, 1), date(2022, 12, 31)),
    "300": (date(2022, 4, 1), date(2022, 12, 31)),
  }
  report = model.parse_report
  assert report is not None and report.dropped == []
  assert [
    (r.context_id, r.start, r.end, r.facts) for r in report.recovered_contexts
  ] == [
    ("OneD", date(2022, 10, 1), date(2022, 12, 31), 3),
    ("FourD", date(2022, 4, 1), date(2022, 12, 31), 3),
  ]
  assert report.recovered_contexts[0].entity_from == "declared_contexts"
  assert report.recovered_contexts[0].period_from == [
    "t:DateOfStartOfReportingPeriod",
    "t:DateOfEndOfReportingPeriod",
  ]


def test_recovered_facts_are_marked_and_declared_ones_are_not(tmp_path: Path):
  model = _parse(tmp_path, RESULTS)

  marked = [f for f in model.facts if f.provenance is not None]
  declared = [f for f in model.facts if f.provenance is None]
  assert len(marked) == 6 and len(declared) == 1
  assert {f.provenance.kind for f in marked if f.provenance} == {RECOVERED_CONTEXT}
  assert all(f.entity_identifier == "ACME" and f.dims == [] for f in marked)
  # The declared context keeps the dates the filing gave it, right or wrong.
  period = next(p for p in model.periods if p.id == declared[0].period_id)
  assert (period.start, period.end) == (date(2022, 10, 1), date(2022, 12, 31))


def test_recovered_facts_of_one_concept_get_distinct_ids(tmp_path: Path):
  # Arelle hashes a fact with no context by its qname alone, so every orphan
  # of one concept shares a hash — across contexts and within one.
  model = _parse(tmp_path, RESULTS + _fact("Revenue", "OneD", 101))

  ids = [f.id for f in model.facts]
  assert len(set(ids)) == len(ids)
  hashes = [f.source_hash for f in model.facts if f.provenance is not None]
  assert len(set(hashes)) == len(hashes)


def test_a_context_with_no_stated_period_is_dropped_and_counted(
  tmp_path: Path, caplog: pytest.LogCaptureFixture
):
  body = (
    _context("Declared", "ACME", "2022-10-01", "2022-12-31")
    + _dates("OneD", "2022-10-01", "2022-12-31")
    + _fact("Revenue", "OneD", 100)
    + _fact("Revenue", "FourD", 300)
    + _fact("Revenue", "FourD", 301)
    + _fact("Revenue", "Declared", 5)
  )
  with caplog.at_level(logging.WARNING, logger="xbrlkit.parse.to_model"):
    model = _parse(tmp_path, body)

  report = model.parse_report
  assert report is not None
  assert [(d.reason, d.context_id, d.facts, d.concepts) for d in report.dropped] == [
    ("undeclared_context", "FourD", 2, ["t:Revenue"])
  ]
  assert report.dropped_facts == 2 and report.recovered_facts == 3
  assert len(model.facts) == 4
  assert "2 fact(s) dropped (undeclared_context: 2)" in caplog.text
  assert "rebuilt context(s) OneD" in caplog.text


def test_half_a_stated_period_is_not_enough(tmp_path: Path):
  body = (
    '<t:DateOfEndOfReportingPeriod contextRef="OneD">2022-12-31'
    "</t:DateOfEndOfReportingPeriod>\n" + _fact("Revenue", "OneD", 100)
  )
  model = _parse(tmp_path, body)

  assert model.facts == []
  assert model.parse_report is not None
  assert model.parse_report.recovered_contexts == []
  assert model.parse_report.dropped_facts == 2


def test_conflicting_stated_dates_are_not_enough(tmp_path: Path):
  body = (
    _dates("OneD", "2022-10-01", "2022-12-31")
    + _dates("OneD", "2022-07-01", "2022-12-31")
    + _fact("Revenue", "OneD", 100)
  )
  model = _parse(tmp_path, body)

  assert model.facts == []
  assert model.parse_report is not None and model.parse_report.dropped_facts == 5


def test_an_instant_concept_is_not_placed_in_a_rebuilt_duration(tmp_path: Path):
  body = (
    _dates("OneD", "2022-10-01", "2022-12-31")
    + _fact("Revenue", "OneD", 100)
    + _fact("Assets", "OneD", 900)
  )
  model = _parse(tmp_path, body)

  assert "t:Assets" not in {f.concept_qname for f in model.facts}
  report = model.parse_report
  assert report is not None
  assert [(d.context_id, d.facts, d.concepts) for d in report.dropped] == [
    ("OneD", 1, ["t:Assets"])
  ]


def test_declared_contexts_that_disagree_on_the_entity_rebuild_nothing(tmp_path: Path):
  body = (
    _context("A", "ACME", "2022-10-01", "2022-12-31")
    + _context("B", "OTHER", "2022-10-01", "2022-12-31")
    + _dates("OneD", "2022-10-01", "2022-12-31")
    + _fact("Revenue", "OneD", 100)
    + _fact("Revenue", "A", 1)
    + _fact("Revenue", "B", 2)
  )
  model = _parse(tmp_path, body)

  assert len(model.facts) == 2
  report = model.parse_report
  assert report is not None and report.recovered_contexts == []
  assert report.dropped_facts == 3


def test_with_no_declared_context_the_filings_identifier_stands_in(tmp_path: Path):
  model = _parse(
    tmp_path, _dates("OneD", "2022-10-01", "2022-12-31") + _fact("Revenue", "OneD", 100)
  )

  assert len(model.facts) == 3
  assert {f.entity_cik for f in model.facts} == {"0000000042"}
  assert {f.entity_scheme for f in model.facts} == {None}
  assert model.parse_report is not None
  assert model.parse_report.recovered_contexts[0].entity_from == "filing"


def test_a_filing_that_declares_its_contexts_has_no_parse_report(
  tmp_path: Path, caplog: pytest.LogCaptureFixture
):
  body = _context("OneD", "ACME", "2022-10-01", "2022-12-31") + _fact(
    "Revenue", "OneD", 100
  )
  with caplog.at_level(logging.WARNING, logger="xbrlkit.parse.to_model"):
    model = _parse(tmp_path, body)

  assert len(model.facts) == 1 and model.facts[0].provenance is None
  assert model.parse_report is None
  assert caplog.text == ""


def test_the_recovered_mark_travels_through_holon_and_tavi(tmp_path: Path):
  from xbrlkit.deserialize.holon import from_holon_json
  from xbrlkit.deserialize.tavi import from_tavi_json
  from xbrlkit.serialize import to_holon, to_tavi

  model = _parse(tmp_path, RESULTS)

  for back in (from_holon_json(to_holon(model)), from_tavi_json(to_tavi(model))):
    kinds = [f.provenance.kind if f.provenance else None for f in back.facts]
    assert kinds.count(RECOVERED_CONTEXT) == 6 and kinds.count(None) == 1


def test_the_load_receipt_and_describe_filing_say_what_the_parse_did(tmp_path: Path):
  from xbrlkit.serve import tools
  from xbrlkit.serve.session import FilingSession

  (tmp_path / "t.xsd").write_text(SCHEMA)
  instance = tmp_path / "i.xml"
  instance.write_text(
    HEAD
    + _context("Declared", "ACME", "2022-10-01", "2022-12-31")
    + _dates("OneD", "2022-10-01", "2022-12-31")
    + _fact("Revenue", "OneD", 100)
    + _fact("Revenue", "FourD", 300)
    + _fact("Revenue", "Declared", 5)
    + "</xbrli:xbrl>\n"
  )
  loaded = FilingSession().load(str(instance))

  for payload in (tools.load_receipt(loaded), tools.describe_filing(loaded)):
    report = payload["parse_report"]
    assert report["facts_dropped"] == 1
    assert report["dropped"] == [
      {
        "reason": "undeclared_context",
        "context": "FourD",
        "facts": 1,
        "concepts": ["t:Revenue"],
      }
    ]
    assert report["facts_in_recovered_contexts"] == 3
    assert report["recovered_contexts"][0]["context"] == "OneD"
    assert report["recovered_contexts"][0]["end"] == "2022-12-31"
