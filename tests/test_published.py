"""Tests for loading a filing from its published representations.

The RoboSystems public data CDN writes every processed SEC filing as a holon
beside the document as filed, under ``{year}/{cik}/{accession}/``, with a
per-filer catalog under ``companies/``. A ticker or ``cik:accession`` loads
that holon when there is one — in a fraction of the time Arelle takes and
with no taxonomy fetch — and falls back to EDGAR when there is not. These
tests stand a local server in for the CDN and stand a sentinel in for EDGAR,
so each path is seen to be taken, or not.
"""

from __future__ import annotations

import functools
import http.server
import json
import socketserver
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.test_deserialize import _model
from xbrlkit.config import Config
from xbrlkit.model import Concept, Label, XbrlFact, XbrlModel
from xbrlkit.serialize import to_holon, to_tavi
from xbrlkit.serve import tools
from xbrlkit.serve.session import FilingSession, PublishedFiling

US_GAAP = "http://fasb.org/us-gaap/2024"
# What the public catalog records about the filer. None of it is in the filing:
# a cover page tags no SIC, and a holon built from the filing carries none.
FILER = {
  "name": "Acme Corporation",
  "exchange": "Nasdaq",
  "sic": "3559",
  "sic_description": "Special Industry Machinery",
}
STANDARD = "http://www.xbrl.org/2003/role/label"
ACCESSION = "0000000000-24-000001"
CIK = "0001234567"
FRAGMENT = (
  "<p>The Company leases office space under operating leases that expire at "
  "various dates through 2031, with renewal options at the Company's discretion "
  "and no residual value guarantees on any of them.</p>"
)


def _model_with_text() -> XbrlModel:
  """The deserialize fixture plus one text block whose value, as a published
  holon carries it, is the URL of its fragment."""
  model = _model()
  model.concepts["us-gaap:LesseeOperatingLeasesTextBlock"] = Concept(
    qname="us-gaap:LesseeOperatingLeasesTextBlock",
    namespace=US_GAAP,
    name="LesseeOperatingLeasesTextBlock",
    period_type="duration",
    is_textblock=True,
    is_text_fact=True,
    pref_label="Leases",
    labels=[Label(value="Leases", role=STANDARD, language="en-US")],
  )
  model.facts.append(
    XbrlFact(
      id="t1",
      concept_qname="us-gaap:LesseeOperatingLeasesTextBlock",
      period_id=model.periods[1].id,
      entity_cik=CIK,
      value_str="__FRAGMENT_URL__",
      value_kind="text",
    )
  )
  return model


@pytest.fixture
def cdn(tmp_path: Path) -> Iterator[str]:
  """A local stand-in for the public data CDN: the filer's catalog, the
  filing's folder with its manifest, holon, document and one fragment."""
  handler = functools.partial(
    http.server.SimpleHTTPRequestHandler, directory=str(tmp_path)
  )
  with socketserver.TCPServer(("127.0.0.1", 0), handler) as httpd:
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    folder = f"{base}/2024/{CIK}/{ACCESSION}"
    root = tmp_path / "2024" / CIK / ACCESSION
    root.mkdir(parents=True)
    (root / "fact_abc.html").write_text(FRAGMENT)
    model = _model_with_text()
    holon = to_holon(model).replace("__FRAGMENT_URL__", f"{folder}/fact_abc.html")
    (root / "holon.jsonld").write_text(holon)
    (root / "acme-20241231.htm").write_text(
      "<html><body><h1>Item 1. Business</h1><p>Acme makes widgets and sells "
      "them everywhere widgets are wanted, which is most places.</p>"
      f"<h2>Note 5. Leases</h2>{FRAGMENT}</body></html>"
    )
    representations = [
      {
        "kind": "holon",
        "name": "holon.jsonld",
        "media_type": "application/ld+json",
        "url": f"{folder}/holon.jsonld",
      },
      {
        "kind": "document",
        "name": "acme-20241231.htm",
        "media_type": "text/html",
        "url": f"{folder}/acme-20241231.htm",
      },
    ]
    (root / "manifest.json").write_text(
      json.dumps(
        {
          "representations": representations,
          # The real manifest names the filer, which is how a `cik:accession`
          # load reaches the catalog that holds that filer's identity.
          "entity": {"cik": CIK, "name": "Acme Corp", "ticker": "ACME"},
        }
      )
    )
    companies = tmp_path / "companies"
    companies.mkdir()
    (companies / "acme.json").write_text(
      json.dumps(
        {
          "ticker": "ACME",
          "cik": CIK,
          **FILER,
          "filings": [
            {
              "accession": ACCESSION,
              "form": "10-K",
              "filing_date": "2025-02-14",
              "folder": folder,
              "representations": representations,
            },
            {
              "accession": "0000000000-23-000001",
              "form": "10-K",
              "filing_date": "2024-02-14",
              "folder": None,
              "representations": [],
            },
          ],
        }
      )
    )
    # A filer whose newest 10-K predates the artifacts: no folder, nothing to load.
    # A catalog with no SIC in it: the lookup has to fall through to EDGAR,
    # and what the catalog does carry still wins over the header.
    (companies / "olde.json").write_text(
      json.dumps(
        {
          "ticker": "OLDE",
          "exchange": "AMEX",
          "filings": [
            {
              "accession": "0000000000-23-000009",
              "form": "10-K",
              "folder": None,
              "representations": [],
            },
            {
              "accession": "0000000000-22-000009",
              "form": "10-K",
              "folder": folder,
              "representations": representations,
            },
          ],
        }
      )
    )
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
      yield base
    finally:
      httpd.shutdown()


class _NoEdgar:
  """EDGAR must not be touched when the published filing serves."""

  def __init__(self, *args, **kwargs) -> None:
    raise AssertionError("EDGAR was consulted for a published filing")


@pytest.fixture
def no_edgar(monkeypatch: pytest.MonkeyPatch) -> None:
  import xbrlkit.edgar

  monkeypatch.setattr(xbrlkit.edgar, "EdgarClient", _NoEdgar)


def _session(cdn: str, **overrides) -> FilingSession:
  return FilingSession(Config(artifacts_base_url=cdn, **overrides))


@pytest.mark.unit
def test_a_ticker_loads_the_published_holon_and_its_document(
  cdn: str, no_edgar
) -> None:
  session = _session(cdn)
  try:
    loaded = session.load("ACME")
    assert loaded.id == ACCESSION
    assert loaded.source_kind == "holon"
    assert loaded.has_xbrl is True
    # The document as filed came with it: the text tools read the whole
    # filing, not only the tagged blocks.
    assert loaded.has_document is True
    assert loaded.model.filing.document_name == "acme-20241231.htm"
    receipt = tools.load_receipt(loaded)
    assert receipt["filing"]["primary_document"] == "acme-20241231.htm"
    assert tools.fact_grid(loaded, ["us-gaap:Assets"])["rows"][0]["value"] == 1000.0
    hits = tools.search_text(loaded, "widgets")
    assert hits["hits"], hits
    # The text block's fragment was fetched and inlined.
    block = next(
      f
      for f in loaded.model.facts
      if f.concept_qname == "us-gaap:LesseeOperatingLeasesTextBlock"
    )
    assert block.value_str == FRAGMENT
  finally:
    session.close()


@pytest.mark.unit
def test_a_form_that_is_not_the_newest_still_resolves_by_form(
  cdn: str, no_edgar
) -> None:
  """``ACME 10-K`` names the form; the newest filing of that form decides."""
  session = _session(cdn)
  try:
    assert session.load("ACME 10-K").id == ACCESSION
  finally:
    session.close()


@pytest.mark.unit
def test_cik_and_accession_probe_the_published_folder(cdn: str, no_edgar) -> None:
  session = _session(cdn)
  try:
    loaded = session.load(f"1234567:{ACCESSION}")
    assert loaded.id == ACCESSION
    assert loaded.source_kind == "holon"
  finally:
    session.close()


@pytest.mark.unit
def test_the_newest_filing_decides_never_an_older_one_with_a_holon(
  cdn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
  """OLDE's newest 10-K predates the artifacts. The answer is EDGAR for that
  filing, not the older filing that happens to be published."""
  from types import SimpleNamespace

  sentinel = SimpleNamespace(id="0000000000-23-000009")
  calls: list[tuple[str, str]] = []

  def fake_edgar(self, cik, accession, source):
    calls.append((cik, accession))
    return sentinel

  monkeypatch.setattr(FilingSession, "_load_edgar", fake_edgar)

  class Client:
    def __init__(self, config=None) -> None:
      pass

    def ticker_to_cik(self, ticker):
      return "0000000042"

    def list_filings(self, cik, forms=None):
      return [SimpleNamespace(accession="0000000000-23-000009")]

  import xbrlkit.edgar

  monkeypatch.setattr(xbrlkit.edgar, "EdgarClient", Client)
  session = _session(cdn)
  try:
    assert session._published_by_ticker("OLDE", "10-K") is None
    assert session.load("OLDE") is sentinel
    assert calls == [("0000000042", "0000000000-23-000009")]
  finally:
    session.close()


@pytest.mark.unit
def test_an_unknown_filer_or_accession_falls_back_to_edgar(cdn: str) -> None:
  session = _session(cdn)
  try:
    assert session._published_by_ticker("NOPE", "10-K") is None
    assert session._published_by_accession("0000000042", "0000000042-25-000001") is None
    assert session._published_by_accession("0000000042", "not-an-accession") is None
  finally:
    session.close()


@pytest.mark.unit
def test_no_base_url_means_no_lookup(cdn: str) -> None:
  session = _session("")
  try:
    assert session._published_by_ticker("ACME", "10-K") is None
    assert session._published_by_accession("1234567", ACCESSION) is None
  finally:
    session.close()


@pytest.mark.unit
def test_fragments_stay_urls_when_fetching_is_off(cdn: str, no_edgar) -> None:
  session = _session(cdn, fetch_external_text=False)
  try:
    loaded = session.load("ACME")
    block = next(
      f
      for f in loaded.model.facts
      if f.concept_qname == "us-gaap:LesseeOperatingLeasesTextBlock"
    )
    assert block.value_str.startswith("http://127.0.0.1:")
  finally:
    session.close()


@pytest.mark.unit
def test_a_missing_fragment_stays_a_url_and_the_load_succeeds(
  cdn: str, no_edgar, tmp_path: Path
) -> None:
  (tmp_path / "2024" / CIK / ACCESSION / "fact_abc.html").unlink()
  session = _session(cdn)
  try:
    loaded = session.load("ACME")
    block = next(
      f
      for f in loaded.model.facts
      if f.concept_qname == "us-gaap:LesseeOperatingLeasesTextBlock"
    )
    assert block.value_str.endswith("/fact_abc.html")
  finally:
    session.close()


@pytest.mark.unit
def test_a_manifest_without_a_model_is_not_a_published_filing() -> None:
  from xbrlkit.serve.session import _published_from

  assert (
    _published_from("acc", [{"kind": "document", "url": "http://x/doc.htm"}], None)
    is None
  )
  published = _published_from(
    "acc", [{"kind": "holon", "name": "holon.jsonld"}], "http://x/f/"
  )
  assert published == PublishedFiling(
    accession="acc", holon_url="http://x/f/holon.jsonld"
  )
  # A filing published as a TAVI model alone is still a published filing.
  only_tavi = _published_from(
    "acc", [{"kind": "tavi", "name": "tavi.json"}], "http://x/f/"
  )
  assert only_tavi == PublishedFiling(accession="acc", tavi_url="http://x/f/tavi.json")
  assert only_tavi.model_urls == ["http://x/f/tavi.json"]


def _publish_tavi(tmp_path: Path, base: str, *, reachable: bool = True) -> None:
  """List a TAVI model in the ACME catalog ahead of the holon; write the file
  only when it should be reachable. The TAVI carries its text block inline."""
  folder = f"{base}/2024/{CIK}/{ACCESSION}"
  root = tmp_path / "2024" / CIK / ACCESSION
  if reachable:
    model = _model_with_text()
    for fact in model.facts:
      if fact.value_str == "__FRAGMENT_URL__":
        fact.value_str = FRAGMENT
    (root / "tavi.json").write_text(to_tavi(model))
  catalog_path = tmp_path / "companies" / "acme.json"
  catalog = json.loads(catalog_path.read_text())
  newest = catalog["filings"][0]
  newest["representations"] = [
    {"kind": "tavi", "name": "tavi.json", "url": f"{folder}/tavi.json"},
    *newest["representations"],
  ]
  catalog_path.write_text(json.dumps(catalog))


@pytest.mark.unit
def test_a_ticker_loads_the_published_tavi_first(
  cdn: str, tmp_path: Path, no_edgar
) -> None:
  _publish_tavi(tmp_path, cdn)
  session = _session(cdn)
  try:
    loaded = session.load("ACME")
    assert loaded.source_kind == "tavi"
    assert loaded.has_document is True
    assert tools.fact_grid(loaded, ["us-gaap:Assets"])["rows"][0]["value"] == 1000.0
    block = next(
      f
      for f in loaded.model.facts
      if f.concept_qname == "us-gaap:LesseeOperatingLeasesTextBlock"
    )
    assert block.value_str == FRAGMENT
  finally:
    session.close()


@pytest.mark.unit
def test_an_unreachable_tavi_falls_back_to_the_holon(
  cdn: str, tmp_path: Path, no_edgar
) -> None:
  _publish_tavi(tmp_path, cdn, reachable=False)
  session = _session(cdn)
  try:
    loaded = session.load("ACME")
    assert loaded.source_kind == "holon"
    assert tools.fact_grid(loaded, ["us-gaap:Assets"])["rows"][0]["value"] == 1000.0
  finally:
    session.close()


# ── The filer's identity ───────────────────────────────────────────────────
#
# A holon carries what the filing carries, and a filing establishes very
# little about its filer: no SIC, and on a holon not even the ticker unless
# the cover page tagged one. The catalog published beside it does, so a load
# from the CDN answers for the filer without going to EDGAR for it — and
# falls through to the submissions header when the catalog cannot.


class _Header:
  """EDGAR's submissions header, as ``company_info`` returns it."""

  def __init__(self, config=None, calls: list[str] | None = None) -> None:
    self._calls = calls

  def company_info(self, cik: str):
    from types import SimpleNamespace

    if self._calls is not None:
      self._calls.append(cik)
    return SimpleNamespace(
      cik=cik,
      name="Olde Industries",
      ein="12-3456789",
      ticker="OLDE",
      exchange="NYSE",
      sic="2911",
      sic_description="Petroleum Refining",
      category="Non-accelerated filer",
      state_of_incorporation="DE",
      fiscal_year_end="1231",
      entity_type="operating",
      website=None,
      phone="212-555-0100",
    )


@pytest.mark.unit
def test_a_published_load_takes_the_filer_from_the_catalog(cdn: str, no_edgar) -> None:
  session = _session(cdn)
  try:
    entity = session.load("ACME").model.entity
    assert entity.sic == "3559"
    assert entity.sic_description == "Special Industry Machinery"
    assert entity.exchange == "Nasdaq"
    # Filled only where the filing said nothing: the holon's own name stands.
    assert entity.name == "Acme Corp"
    assert entity.legal_name == "Acme Corp"
  finally:
    session.close()


@pytest.mark.unit
def test_the_accession_route_reaches_the_catalog_through_the_manifest(
  cdn: str, no_edgar
) -> None:
  """A `cik:accession` load has no ticker to start from; the manifest names
  the filer, and the filer's catalog holds the identity."""
  session = _session(cdn)
  try:
    assert session.load(f"1234567:{ACCESSION}").model.entity.sic == "3559"
  finally:
    session.close()


@pytest.mark.unit
def test_the_header_backfills_a_catalog_with_no_sic(
  cdn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
  import xbrlkit.edgar

  monkeypatch.setattr(xbrlkit.edgar, "EdgarClient", _Header)
  session = _session(cdn)
  try:
    found = session._filer_metadata("0000000042", ticker="OLDE")
    assert found["sic"] == "2911"
    # The catalog wins where both carry the field.
    assert found["exchange"] == "AMEX"
  finally:
    session.close()


@pytest.mark.unit
def test_a_filer_lookup_that_fails_leaves_the_filing_loaded(
  cdn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
  """EDGAR refusing is not a load failure: the fields stay empty."""
  import requests

  import xbrlkit.edgar

  class _Down:
    def __init__(self, config=None) -> None:
      pass

    def company_info(self, cik: str):
      raise requests.RequestException("429 Too Many Requests")

  monkeypatch.setattr(xbrlkit.edgar, "EdgarClient", _Down)
  session = _session(cdn)
  try:
    assert session._filer_metadata("0000000042", ticker="OLDE") == {
      "ticker": "OLDE",
      "exchange": "AMEX",
    }
  finally:
    session.close()


@pytest.mark.unit
def test_the_filer_is_looked_up_once_per_cik(
  cdn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
  """A sweep of one filer's filings costs one lookup, not one per filing."""
  import functools

  import xbrlkit.edgar

  calls: list[str] = []
  monkeypatch.setattr(
    xbrlkit.edgar, "EdgarClient", functools.partial(_Header, calls=calls)
  )
  session = _session(cdn)
  try:
    session._filer_metadata("0000000042", ticker="OLDE")
    session._filer_metadata("42", ticker="OLDE")
    assert calls == ["0000000042"]
  finally:
    session.close()
