"""Tests for the xBRL-JSON (OIM) projection (``serialize/oim.py``).

The projection's real test is the diff against Arelle's ``saveLoadableOIM`` on
whole filings, which needs a network fetch and a full parse and so cannot live
here. These cover the conventions that diff established — the ones that are
easy to get backwards and that no type-checker would catch.
"""

from __future__ import annotations

import json
from datetime import date
from typing import Any

from xbrlkit.model import (
  Concept,
  DimQualifier,
  EntityIdentity,
  FilingMeta,
  Period,
  Unit,
  XbrlFact,
  XbrlModel,
)
from xbrlkit.serialize import to_oim, to_oim_document
from xbrlkit.serialize.oim import OIM_DOCUMENT_TYPE

US_GAAP = "http://fasb.org/us-gaap/2024"


def _concept(name: str, **kw: Any) -> Concept:
  return Concept(qname=f"us-gaap:{name}", namespace=US_GAAP, name=name, **kw)


def _model() -> XbrlModel:
  concepts = {
    "us-gaap:Assets": _concept(
      "Assets", period_type="instant", is_numeric=True, item_type="monetaryItemType"
    ),
    "us-gaap:Revenues": _concept(
      "Revenues", period_type="duration", is_numeric=True, item_type="monetaryItemType"
    ),
    "us-gaap:Ratio": _concept(
      "Ratio", period_type="duration", is_numeric=True, item_type="pureItemType"
    ),
    "us-gaap:Count": _concept(
      "Count",
      period_type="duration",
      is_numeric=True,
      is_integer=True,
      item_type="integerItemType",
    ),
    "us-gaap:Policy": _concept(
      "Policy", period_type="duration", item_type="textBlockItemType", is_text_fact=True
    ),
    "us-gaap:Flag": _concept(
      "Flag", period_type="duration", item_type="booleanItemType", is_text_fact=False
    ),
  }
  periods = [
    Period(id="p-i", period_type="instant", end=date(2024, 12, 31)),
    Period(
      id="p-d", period_type="duration", start=date(2024, 1, 1), end=date(2024, 12, 31)
    ),
  ]
  units = [
    Unit(id="u-usd", measure="iso4217:USD"),
    Unit(id="u-pure", measure="xbrli:pure"),
  ]

  def fact(fid: str, qname: str, pid: str, **kw: Any) -> XbrlFact:
    return XbrlFact(
      id=fid, concept_qname=qname, period_id=pid, entity_cik="0001234567", **kw
    )

  facts = [
    fact(
      "1",
      "us-gaap:Assets",
      "p-i",
      unit_id="u-usd",
      value_str="1000",
      numeric_value=1000.0,
      decimals="-3",
    ),
    fact(
      "2",
      "us-gaap:Ratio",
      "p-d",
      unit_id="u-pure",
      value_str="0.0530",
      numeric_value=0.053,
      decimals="4",
    ),
    fact("3", "us-gaap:Count", "p-d", value_str="9", numeric_value=9.0, decimals="INF"),
    fact(
      "4",
      "us-gaap:Policy",
      "p-d",
      value_str="Some policy text",
      value_kind="text",
      language="en-US",
    ),
    fact(
      "5", "us-gaap:Flag", "p-d", value_str="false", value_kind="text", language="en-US"
    ),
    fact(
      "6",
      "us-gaap:Assets",
      "p-i",
      unit_id="u-usd",
      value_str="",
      value_kind="text",
      is_nil=True,
    ),
    fact(
      "7",
      "us-gaap:Revenues",
      "p-d",
      unit_id="u-usd",
      value_str="500",
      numeric_value=500.0,
      decimals="-3",
      dims=[
        DimQualifier(
          axis_qname="us-gaap:SegmentAxis", member_qname="us-gaap:NorthAmerica"
        )
      ],
    ),
  ]
  return XbrlModel(
    filing=FilingMeta(accession="0000000000-24-000001", cik="0001234567", form="10-K"),
    entity=EntityIdentity(cik="0001234567", name="Acme Corp"),
    concepts=concepts,
    periods=periods,
    units=units,
    facts=facts,
  )


def _facts() -> list[dict[str, Any]]:
  return list(to_oim_document(_model())["facts"].values())


def _by_concept(qname: str) -> dict[str, Any]:
  return next(f for f in _facts() if f["dimensions"]["concept"] == qname)


def test_document_envelope() -> None:
  info = to_oim_document(_model())["documentInfo"]
  assert info["documentType"] == OIM_DOCUMENT_TYPE
  assert info["features"] == {"xbrl:canonicalValues": True}
  assert info["namespaces"]["cik"] == "http://www.sec.gov/CIK"


def test_non_sec_entity_binds_its_own_scheme() -> None:
  """An entity under any scheme but the SEC's is ``entity:<id>``, scheme bound."""
  model = _model().model_copy(
    update={
      "entity": EntityIdentity(cik="ent_01K3ZQ", scheme="http://robosystems.ai/entity")
    }
  )
  document = to_oim_document(model)
  namespaces = document["documentInfo"]["namespaces"]
  assert namespaces["entity"] == "http://robosystems.ai/entity"
  assert "cik" not in namespaces
  assert all(
    fact["dimensions"]["entity"] == "entity:ent_01K3ZQ"
    for fact in document["facts"].values()
  )


def test_instant_period_is_the_exclusive_next_midnight() -> None:
  """The close of 2024-12-31 is written as the instant after it."""
  assert _by_concept("us-gaap:Assets")["dimensions"]["period"] == "2025-01-01T00:00:00"


def test_duration_period_end_is_exclusive() -> None:
  assert _by_concept("us-gaap:Policy")["dimensions"]["period"] == (
    "2024-01-01T00:00:00/2025-01-01T00:00:00"
  )


def test_values_are_strings_and_decimals_are_integers() -> None:
  assets = _by_concept("us-gaap:Assets")
  assert assets["value"] == "1000.0"
  assert assets["decimals"] == -3


def test_integer_typed_facts_keep_no_fractional_part() -> None:
  """A decimal fact reads 1000.0; an integer-typed one reads 9, not 9.0."""
  count = _by_concept("us-gaap:Count")
  assert count["value"] == "9"
  assert "decimals" not in count  # INF is omitted


def test_canonical_values_drop_insignificant_trailing_zeros() -> None:
  assert _by_concept("us-gaap:Ratio")["value"] == "0.053"


def test_pure_units_are_omitted() -> None:
  """A pure unit means no unit, and OIM leaves the dimension off."""
  assert "unit" not in _by_concept("us-gaap:Ratio")["dimensions"]
  assert _by_concept("us-gaap:Assets")["dimensions"]["unit"] == "iso4217:USD"


def test_language_applies_to_text_facts_only() -> None:
  """Both facts carry an xml:lang; only the string-derived one takes it."""
  # xBRL-JSON requires the lower-case form; the filing said en-US.
  assert _by_concept("us-gaap:Policy")["dimensions"]["language"] == "en-us"
  assert "language" not in _by_concept("us-gaap:Flag")["dimensions"]


def test_nil_facts_are_null_not_empty() -> None:
  nil = next(f for f in _facts() if f["value"] is None)
  assert nil["dimensions"]["concept"] == "us-gaap:Assets"


def test_taxonomy_dimensions_sit_beside_the_core_ones() -> None:
  revenues = _by_concept("us-gaap:Revenues")["dimensions"]
  assert revenues["us-gaap:SegmentAxis"] == "us-gaap:NorthAmerica"
  assert revenues["entity"] == "cik:0001234567"


def test_serialization_is_deterministic_and_valid_json() -> None:
  first, second = to_oim(_model()), to_oim(_model())
  assert first == second
  assert json.loads(first)["documentInfo"]["documentType"] == OIM_DOCUMENT_TYPE
