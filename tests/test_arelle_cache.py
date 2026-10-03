"""The Arelle cache layer: the layout, seeding and bundling, and the fetch
policy a corpus run depends on (per-host spacing, ``Retry-After`` backoff,
loud failure on an unresolved DTS). No network."""

from __future__ import annotations

import io
import os
import tarfile
import urllib.error
from email.message import Message
from pathlib import Path
from types import SimpleNamespace

import pytest

from xbrlkit.parse import (
  DtsResolutionError,
  arelle_load,
  configure_webcache,
  load_model,
)
from xbrlkit.parse import cache as schema_cache
from xbrlkit.parse.arelle_load import (
  MAX_RETRIES,
  LoadState,
  _wrap_download,
  _wrap_getfilename,
  _wrap_normalize_url,
  _wrap_opener,
  retry_after_seconds,
)

XSD = "https://www.xbrl.org/2003/xbrl-instance-2003-12-31.xsd"


def _tarball(entries: dict[str, bytes]) -> io.BytesIO:
  buffer = io.BytesIO()
  with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
    for name, content in entries.items():
      info = tarfile.TarInfo(name)
      info.size = len(content)
      archive.addfile(info, io.BytesIO(content))
  buffer.seek(0)
  return buffer


def _write_tarball(path: Path, entries: dict[str, bytes]) -> Path:
  path.write_bytes(_tarball(entries).getvalue())
  return path


# ---- layout ---------------------------------------------------------------------


class TestCachePath:
  def test_matches_arelles_layout(self, tmp_path: Path) -> None:
    from arelle import Cntlr

    cntlr = Cntlr.Cntlr(
      hasGui=False,
      logFileName="logToBuffer",
      logFileMode="w",
      uiLang=None,
      disable_persistent_config=True,
    )
    try:
      for url in (
        XSD,
        "http://www.w3.org/2001/xml.xsd",
        "https://xbrl.fasb.org/us-gaap/2024/elts/us-gaap-2024.xsd",
        "https://example.com:8443/a/b.xsd",
        "https://example.com/dir/",
      ):
        expected = cntlr.webCache.urlToCacheFilepath(
          url, cacheDir=str(tmp_path), useRedirectFallback=False
        )
        assert schema_cache.cache_path(tmp_path, url) == Path(expected)
    finally:
      cntlr.close()


# ---- seed / status / bundle -------------------------------------------------------


class TestSeed:
  def test_reads_the_legacy_and_the_native_layout(self, tmp_path: Path) -> None:
    bundle = _write_tarball(
      tmp_path / "bundle.tar.gz",
      {
        "cache/www.xbrl.org/2003/xbrl-instance-2003-12-31.xsd": b"<xs:schema/>",
        "cache/cache_metadata.json": b"{}",
        "https/xbrl.sec.gov/dei/2024/dei-2024.xsd": b"<xs:schema/>",
        "http/www.w3.org/2001/xml.xsd": b"<xs:schema/>",
      },
    )
    cache_dir = tmp_path / "cache"
    report = schema_cache.seed(bundle, cache_dir)

    assert report.written == 3 and report.skipped == 1
    assert (cache_dir / "https/www.xbrl.org/2003/xbrl-instance-2003-12-31.xsd").exists()
    assert (cache_dir / "https/xbrl.sec.gov/dei/2024/dei-2024.xsd").exists()
    assert (cache_dir / "http/www.w3.org/2001/xml.xsd").exists()
    assert not (cache_dir / "cache_metadata.json").exists()
    assert schema_cache.cache_path(cache_dir, XSD).exists()

  def test_leaves_present_files_alone(self, tmp_path: Path) -> None:
    bundle = _write_tarball(
      tmp_path / "bundle.tar.gz", {"https/www.w3.org/2001/xml.xsd": b"new"}
    )
    cache_dir = tmp_path / "cache"
    existing = cache_dir / "https/www.w3.org/2001/xml.xsd"
    existing.parent.mkdir(parents=True)
    existing.write_bytes(b"old")

    report = schema_cache.seed(bundle, cache_dir)
    assert report.written == 0 and report.skipped == 1
    assert existing.read_bytes() == b"old"

  def test_refuses_entries_that_escape_the_cache(self, tmp_path: Path) -> None:
    bundle = _write_tarball(
      tmp_path / "bundle.tar.gz", {"https/../../escape.xsd": b"x"}
    )
    cache_dir = tmp_path / "cache"
    report = schema_cache.seed(bundle, cache_dir)
    assert report.written == 0 and report.skipped == 1
    assert not (tmp_path / "escape.xsd").exists()


class TestStatus:
  def test_empty_cache_is_not_seeded(self, tmp_path: Path) -> None:
    report = schema_cache.status(tmp_path / "missing")
    assert report.files == 0
    assert not report.seeded
    assert report.missing_essentials == list(schema_cache.ESSENTIAL_URLS)

  def test_counts_by_host_and_finds_essentials_under_either_scheme(
    self, tmp_path: Path
  ) -> None:
    for url in schema_cache.ESSENTIAL_URLS:
      path = schema_cache.cache_path(tmp_path, url.replace("https://", "http://", 1))
      path.parent.mkdir(parents=True, exist_ok=True)
      path.write_bytes(b"<xs:schema/>")
    extra = schema_cache.cache_path(
      tmp_path, "https://xbrl.sec.gov/dei/2024/dei-2024_lab.xml"
    )
    extra.parent.mkdir(parents=True)
    extra.write_bytes(b"<link/>")
    (tmp_path / "stray.txt").write_text("not in the layout")

    report = schema_cache.status(tmp_path)
    assert report.seeded
    assert report.files == 4 and report.schemas == 3
    assert report.by_host == {"www.w3.org": 1, "www.xbrl.org": 2, "xbrl.sec.gov": 1}


class TestBundle:
  def test_round_trips_and_filters_by_host(self, tmp_path: Path) -> None:
    cache_dir = tmp_path / "cache"
    for url in (
      XSD,
      "https://www.w3.org/2001/xml.xsd",
      "https://xbrl.fasb.org/us-gaap/2024/elts/us-gaap-2024.xsd",
    ):
      path = schema_cache.cache_path(cache_dir, url)
      path.parent.mkdir(parents=True, exist_ok=True)
      path.write_bytes(url.encode())

    out = tmp_path / "throttled.tar.gz"
    count = schema_cache.bundle(out, cache_dir, hosts=schema_cache.THROTTLED_HOSTS)
    assert count == 2

    other = tmp_path / "other"
    report = schema_cache.seed(out, other)
    assert report.written == 2
    assert schema_cache.cache_path(other, XSD).read_bytes() == XSD.encode()
    assert not schema_cache.cache_path(
      other, "https://xbrl.fasb.org/us-gaap/2024/elts/us-gaap-2024.xsd"
    ).exists()


def test_entry_points_cover_the_core_and_each_year() -> None:
  urls = schema_cache.entry_points((2024, 2025))
  assert XSD in urls
  assert "https://xbrl.sec.gov/dei/2024/dei-2024.xsd" in urls
  assert "https://xbrl.fasb.org/us-gaap/2025/elts/us-gaap-2025.xsd" in urls
  assert "https://xbrl.fasb.org/srt/2024/elts/srt-2024.xsd" in urls
  assert len(urls) == len(set(urls))


# ---- the fetch policy --------------------------------------------------------------


def _headers(**values: str) -> Message:
  message = Message()
  for key, value in values.items():
    message[key.replace("_", "-")] = value
  return message


class TestRetryAfter:
  def test_seconds_header(self) -> None:
    assert retry_after_seconds(_headers(Retry_After="7"), 0) == 7.0

  def test_http_date_header_is_relative_to_now(self) -> None:
    from email.utils import formatdate

    soon = formatdate(usegmt=True)  # now → ~0s
    assert retry_after_seconds(_headers(Retry_After=soon), 0) <= 1.0

  def test_missing_header_doubles(self) -> None:
    assert retry_after_seconds(None, 0) == 5.0
    assert retry_after_seconds(_headers(), 1) == 10.0
    assert retry_after_seconds(_headers(), 2) == 20.0

  def test_capped(self) -> None:
    assert retry_after_seconds(_headers(Retry_After="3600"), 0) == 60.0


class _FakeOpener:
  def __init__(self, failures: int, code: int = 429) -> None:
    self.failures = failures
    self.code = code
    self.calls: list[str] = []

  def open(self, fullurl, data=None, timeout=None):  # noqa: ANN001
    self.calls.append(fullurl)
    if len(self.calls) <= self.failures:
      raise urllib.error.HTTPError(
        fullurl, self.code, "throttled", _headers(Retry_After="1"), None
      )
    return "ok"


def _fake_webcache(opener: _FakeOpener | None = None) -> SimpleNamespace:
  return SimpleNamespace(
    opener=opener or _FakeOpener(0),
    workOffline=False,
    normalizeUrl=lambda url, base=None: url,
    getfilename=lambda url, base=None, **kw: url,
    _downloadFile=lambda url, filepath, r=False, retryCount=5: True,
  )


class TestOpenerBackoff:
  def test_waits_and_retries_on_429(self, monkeypatch: pytest.MonkeyPatch) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr(arelle_load.time, "sleep", sleeps.append)
    monkeypatch.setattr(arelle_load, "_limiters", {})
    monkeypatch.setattr(arelle_load.RateLimiter, "wait", lambda self: None)
    webcache = _fake_webcache(_FakeOpener(failures=2))
    state = LoadState()
    _wrap_opener(webcache, state)

    assert webcache.opener.open("https://www.xbrl.org/2003/x.xsd") == "ok"
    assert sleeps == [1.0, 1.0]
    assert state.backoffs == 2

  def test_gives_up_after_the_bounded_retries(
    self, monkeypatch: pytest.MonkeyPatch
  ) -> None:
    monkeypatch.setattr(arelle_load.time, "sleep", lambda s: None)
    monkeypatch.setattr(arelle_load, "_limiters", {})
    opener = _FakeOpener(failures=MAX_RETRIES + 5, code=503)
    webcache = _fake_webcache(opener)
    _wrap_opener(webcache, LoadState())

    with pytest.raises(urllib.error.HTTPError):
      webcache.opener.open("https://www.w3.org/2001/xml.xsd")
    assert len(opener.calls) == MAX_RETRIES + 1

  def test_other_errors_pass_through(self, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(arelle_load, "_limiters", {})
    opener = _FakeOpener(failures=1, code=404)
    webcache = _fake_webcache(opener)
    _wrap_opener(webcache, LoadState())
    with pytest.raises(urllib.error.HTTPError):
      webcache.opener.open("https://www.xbrl.org/missing.xsd")
    assert len(opener.calls) == 1

  def test_spaces_requests_per_host(self, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(arelle_load, "_limiters", {})
    webcache = _fake_webcache()
    _wrap_opener(webcache, LoadState())
    webcache.opener.open("https://www.xbrl.org/a.xsd")
    webcache.opener.open("https://xbrl.sec.gov/b.xsd")
    assert set(arelle_load._limiters) == {"www.xbrl.org", "xbrl.sec.gov"}


class TestUnresolvedRecording:
  def test_failed_download_is_recorded_with_one_arelle_attempt(self) -> None:
    seen: list[tuple[str, int]] = []

    def download(url, filepath, retrievingDueToRecheckInterval=False, retryCount=5):  # noqa: N803
      seen.append((url, retryCount))
      return url.endswith("ok.xsd")

    webcache = _fake_webcache()
    webcache._downloadFile = download
    state = LoadState()
    _wrap_download(webcache, state)

    assert webcache._downloadFile("https://www.xbrl.org/ok.xsd", "/tmp/ok") is True
    assert webcache._downloadFile("https://www.xbrl.org/gone.xsd", "/tmp/gone") is False
    assert webcache._downloadFile("https://www.xbrl.org/gone.xsd", "/tmp/gone") is False
    assert state.unresolved == ["https://www.xbrl.org/gone.xsd"]
    assert {retry for _, retry in seen} == {1}

  def test_offline_miss_is_recorded(self, tmp_path: Path) -> None:
    present = tmp_path / "present.xsd"
    present.write_bytes(b"<xs:schema/>")
    webcache = _fake_webcache()
    webcache.workOffline = True
    webcache.getfilename = lambda url, base=None, **kw: (
      str(present) if "present" in url else str(tmp_path / "missing.xsd")
    )
    state = LoadState()
    _wrap_getfilename(webcache, state)

    webcache.getfilename("https://www.xbrl.org/present.xsd")
    webcache.getfilename("https://www.xbrl.org/missing.xsd")
    webcache.getfilename("https://www.xbrl.org/missing.xsd", filenameOnly=True)
    webcache.getfilename("/local/file.xsd")
    assert state.unresolved == ["https://www.xbrl.org/missing.xsd"]

  def test_online_miss_is_not_recorded_by_the_lookup(self, tmp_path: Path) -> None:
    webcache = _fake_webcache()
    webcache.getfilename = lambda url, base=None, **kw: str(tmp_path / "missing.xsd")
    state = LoadState()
    _wrap_getfilename(webcache, state)
    webcache.getfilename("https://www.xbrl.org/missing.xsd")
    assert state.unresolved == []


def test_schema_hosts_are_fetched_over_https() -> None:
  webcache = _fake_webcache()
  _wrap_normalize_url(webcache)
  assert (
    webcache.normalizeUrl("http://www.xbrl.org/2003/xbrl-instance-2003-12-31.xsd")
    == XSD
  )
  assert webcache.normalizeUrl("http://example.com/x.xsd") == "http://example.com/x.xsd"
  assert webcache.normalizeUrl(XSD) == XSD


# ---- through a real controller --------------------------------------------------------


INSTANCE = """<?xml version="1.0" encoding="utf-8"?>
<xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance"
            xmlns:link="http://www.xbrl.org/2003/linkbase"
            xmlns:xlink="http://www.w3.org/1999/xlink">
  <link:schemaRef xlink:type="simple" xlink:href="https://example.invalid/missing.xsd"/>
</xbrli:xbrl>
"""


class TestLoadModel:
  def test_offline_with_a_cold_cache_refuses_the_partial_dts(
    self, tmp_path: Path
  ) -> None:
    instance = tmp_path / "instance.xml"
    instance.write_text(INSTANCE)

    with pytest.raises(DtsResolutionError) as excinfo:
      load_model(instance, tmp_path / "cache", offline=True)

    assert "https://example.invalid/missing.xsd" in excinfo.value.unresolved
    assert "missing.xsd" in str(excinfo.value)

  def test_a_taxonomy_on_a_host_that_has_gone_does_not_refuse_the_load(
    self, tmp_path: Path
  ) -> None:
    """Every Danish ESEF report cites the Danish Business Authority's taxonomy
    beside its own, and that host answers 404. The load comes back without it,
    and says what it went without."""
    gone = "http://archprod.service.eogs.dk/taxonomy/20241001/entry.xsd"
    instance = tmp_path / "instance.xml"
    instance.write_text(INSTANCE.replace("https://example.invalid/missing.xsd", gone))

    mx = load_model(instance, tmp_path / "cache", offline=True)
    try:
      assert arelle_load.load_state(mx.modelManager.cntlr).unresolved == [gone]
    finally:
      arelle_load.close(mx.modelManager.cntlr)

  def test_a_gone_host_does_not_excuse_another_unresolved_document(
    self, tmp_path: Path
  ) -> None:
    gone = "http://archprod.service.eogs.dk/taxonomy/20241001/entry.xsd"
    ref = '<link:schemaRef xlink:type="simple" xlink:href="{}"/>'
    instance = tmp_path / "instance.xml"
    instance.write_text(
      INSTANCE.replace(
        ref.format("https://example.invalid/missing.xsd"),
        ref.format(gone) + ref.format("https://example.invalid/missing.xsd"),
      )
    )

    with pytest.raises(DtsResolutionError) as excinfo:
      load_model(instance, tmp_path / "cache", offline=True)

    assert "https://example.invalid/missing.xsd" in excinfo.value.unresolved

  def test_configure_webcache_on_a_host_controller(self, tmp_path: Path) -> None:
    from arelle import Cntlr

    cntlr = Cntlr.Cntlr(
      hasGui=False,
      logFileName="logToBuffer",
      logFileMode="w",
      uiLang=None,
      disable_persistent_config=True,
    )
    try:
      state = configure_webcache(cntlr, tmp_path, offline=True, timeout=7)
      assert cntlr.webCache.cacheDir == str(tmp_path)
      assert cntlr.webCache.workOffline is True
      assert cntlr.webCache.timeout == 7
      assert cntlr.webCache.maxAgeSeconds == float("inf")  # never re-validate
      assert arelle_load.load_state(cntlr) is state
      assert state.unresolved == []
    finally:
      cntlr.close()


def test_offline_env_var_reaches_the_config(monkeypatch: pytest.MonkeyPatch) -> None:
  from xbrlkit.config import Config

  monkeypatch.setenv("XBRLKIT_ARELLE_OFFLINE", "true")
  monkeypatch.setenv("XBRLKIT_ARELLE_TIMEOUT", "12")
  monkeypatch.setenv("XBRLKIT_ARELLE_CACHE_DIR", os.path.join("/tmp", "xk-cache"))
  config = Config()
  assert config.arelle_offline is True
  assert config.arelle_timeout == 12
  assert config.arelle_cache_dir == Path("/tmp/xk-cache")
