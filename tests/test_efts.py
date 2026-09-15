"""EFTS discovery: hit parsing, the query string, paging, the quarter
partitions, and the throttle policy. No network."""

from __future__ import annotations

from typing import Any

import pytest

from xbrlkit.edgar import EftsClient, EftsHit, efts


class FakeResponse:
  def __init__(self, payload: Any, status_code: int = 200, headers: dict | None = None):
    self._payload = payload
    self.status_code = status_code
    self.headers = headers or {}
    self.content = b"{}"

  def json(self) -> Any:
    return self._payload

  def raise_for_status(self) -> None:
    if self.status_code >= 400:
      raise AssertionError(f"HTTP {self.status_code}")


def _client(
  responses: list[FakeResponse], monkeypatch: pytest.MonkeyPatch
) -> tuple[EftsClient, list[str]]:
  client = EftsClient(per_sec=0)
  urls: list[str] = []
  queue = list(responses)

  def get(url: str, timeout: int | None = None) -> FakeResponse:
    urls.append(url)
    return queue.pop(0)

  monkeypatch.setattr(client._session, "get", get)
  return client, urls


def _page(total: int, hits: list[dict]) -> FakeResponse:
  return FakeResponse({"hits": {"total": {"value": total}, "hits": hits}})


def _hit(cik: int, accession: str, form: str = "10-K") -> dict:
  return {"_id": f"{accession}:doc.htm", "_source": {"ciks": [cik], "form": form}}


class TestEftsHit:
  def test_from_hit(self) -> None:
    hit = EftsHit.from_hit(
      {
        "_id": "0000320193-24-000123:aapl-20240928.htm",
        "_source": {
          "ciks": [320193],
          "form": "10-K",
          "file_num": "001-36743",
          "file_date": "2024-11-01",
          "display_names": ["Apple Inc.  (AAPL)"],
          "file_url": "https://www.sec.gov/Archives/edgar/data/320193/x.htm",
        },
      }
    )
    assert hit.cik == "0000320193"
    assert hit.accession == "0000320193-24-000123"
    assert hit.form == "10-K"
    assert hit.file_number == "001-36743"
    assert hit.filing_date == "2024-11-01"
    assert hit.primary_document == "Apple Inc.  (AAPL)"
    assert hit.file_url.endswith("/x.htm")

  def test_missing_fields(self) -> None:
    hit = EftsHit.from_hit({"_id": "0001-24-000001", "_source": {}})
    assert hit.cik == "" and hit.accession == "0001-24-000001" and hit.form == ""
    assert hit.file_number is None and hit.primary_document is None
    assert hit.parties == () and hit.party_ciks == ()

  def test_an_ownership_form_keeps_both_parties(self) -> None:
    """A Form 4 is associated with the reporting owner *and* the issuer.
    Keeping only the first entry loses the reason a CIK query matched: a
    search for the company returns a hit whose filer is an individual."""
    hit = EftsHit.from_hit(
      {
        "_id": "0001522767-26-000170:wk-form4.xml",
        "_source": {
          "ciks": [1866577, 1522767],
          "display_names": ["Shaw Timothy  (CIK 0001866577)", "MARIMED INC."],
          "form": "4",
          "file_date": "2026-09-01",
        },
      }
    )
    assert hit.party_ciks == ("0001866577", "0001522767")
    assert hit.parties == ("Shaw Timothy  (CIK 0001866577)", "MARIMED INC.")
    assert hit.cik == "0001866577"  # the one that addresses the filing


class TestBuildParams:
  def test_forms_exclude_amendments_by_default(self) -> None:
    params = EftsClient.build_params(forms=["10-K", "10-Q/A"])
    assert params["forms"] == "10-K,10-Q/A,-10-K/A"

  def test_amendments_kept_when_asked(self) -> None:
    assert (
      EftsClient.build_params(forms=["10-K"], include_amendments=True)["forms"]
      == "10-K"
    )

  def test_dates_default_to_the_full_range(self) -> None:
    params = EftsClient.build_params()
    assert params["startdt"] == "2001-01-01"
    assert len(params["enddt"]) == 10

  def test_ciks_are_padded_and_text_passes_through(self) -> None:
    params = EftsClient.build_params(
      ciks=["320193", "0000789019"], text_query='"material weakness"'
    )
    assert params["ciks"] == "0000320193,0000789019"
    assert params["q"] == '"material weakness"'


class TestQuery:
  def test_no_results(self, monkeypatch: pytest.MonkeyPatch) -> None:
    client, urls = _client([_page(0, [])], monkeypatch)
    assert client.query(forms=["10-K"], start_date="2024-01-01") == []
    assert len(urls) == 1 and "size=1" in urls[0]

  def test_pages_through_the_result_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
    hits = [_hit(320190 + i, f"000032019{i}-24-000001") for i in range(3)]
    client, urls = _client([_page(3, hits[:1]), _page(3, hits)], monkeypatch)
    results = client.query(forms=["10-K"])
    assert [h.cik for h in results] == ["0000320190", "0000320191", "0000320192"]
    assert "from=0&size=3" in urls[1]

  def test_max_results_caps_the_fetch(self, monkeypatch: pytest.MonkeyPatch) -> None:
    hits = [_hit(320190 + i, f"000032019{i}-24-000001") for i in range(10)]
    client, urls = _client([_page(10, hits[:1]), _page(10, hits[:5])], monkeypatch)
    assert len(client.query(forms=["10-K"], max_results=5)) == 5
    assert "size=5" in urls[1]

  def test_short_page_ends_the_walk(self, monkeypatch: pytest.MonkeyPatch) -> None:
    hits = [_hit(1, "0001-24-000001"), _hit(2, "0002-24-000001")]
    client, urls = _client([_page(5, hits[:1]), _page(5, hits)], monkeypatch)
    assert len(client.query()) == 2
    assert len(urls) == 2

  def test_429_waits_for_retry_after(self, monkeypatch: pytest.MonkeyPatch) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr(efts.time, "sleep", sleeps.append)
    throttled = FakeResponse({}, status_code=429, headers={"Retry-After": "3"})
    client, _ = _client([throttled, _page(0, [])], monkeypatch)
    assert client.query() == []
    assert sleeps == [3.0]

  def test_server_error_retries_then_gives_up(
    self, monkeypatch: pytest.MonkeyPatch
  ) -> None:
    monkeypatch.setattr(efts.time, "sleep", lambda s: None)
    client, urls = _client([FakeResponse({}, status_code=502)] * 10, monkeypatch)
    with pytest.raises(RuntimeError, match="server error 502"):
      client.query()
    assert len(urls) == efts.MAX_RETRIES + 1


class TestPartitions:
  def test_quarter_dates(self, monkeypatch: pytest.MonkeyPatch) -> None:
    client, urls = _client([_page(0, [])] * 4, monkeypatch)
    for quarter, (start, end) in {
      1: ("01-01", "03-31"),
      2: ("04-01", "06-30"),
      3: ("07-01", "09-30"),
      4: ("10-01", "12-31"),
    }.items():
      client.query_by_quarter(2024, quarter, ciks=["320193"])
      assert f"startdt=2024-{start}" in urls[-1] and f"enddt=2024-{end}" in urls[-1]
      assert "ciks=0000320193" in urls[-1]

  def test_invalid_quarter(self) -> None:
    with pytest.raises(ValueError, match="Quarter"):
      EftsClient(per_sec=0).query_by_quarter(2024, 5)

  def test_year_uses_the_default_forms(self, monkeypatch: pytest.MonkeyPatch) -> None:
    client, urls = _client([_page(0, [])], monkeypatch)
    client.query_by_year(2024)
    assert "startdt=2024-01-01" in urls[0] and "enddt=2024-12-31" in urls[0]
    assert "10-K" in urls[0] and "DEF+14A" in urls[0]


def test_constants() -> None:
  assert efts.EFTS_MAX_PAGE_SIZE == 100
  assert efts.EFTS_MAX_RESULTS == 10_000
