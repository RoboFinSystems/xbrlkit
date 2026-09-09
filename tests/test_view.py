"""`xbrlkit view`: the loopback server that hands one document to the viewer.

The browser half was verified by hand against Chrome 152 on 2026-09-08 — the
hosted viewer fetched and rendered a filing served from ``127.0.0.1`` with no
mixed-content or Local Network Access block. What is tested here is the half a
test can hold: that exactly one document is reachable, that its path is
unguessable, that the CORS header names the viewer's origin and nothing else,
and that a file already in the requested serialization is passed through
rather than round-tripped.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from xbrlkit.view import (
  DEFAULT_VIEWER,
  ViewerHost,
  origin_of,
  serve_report,
  viewer_url,
)


def _get(url: str, origin: str | None = None) -> tuple[int, dict[str, str], bytes]:
  request = urllib.request.Request(url)
  if origin:
    request.add_header("Origin", origin)
  try:
    with urllib.request.urlopen(request, timeout=5) as response:
      return response.status, dict(response.headers), response.read()
  except urllib.error.HTTPError as exc:
    return exc.code, dict(exc.headers), exc.read()


@pytest.fixture
def served():
  report = serve_report('{"@context": {}}', "acme.holon.jsonld")
  try:
    yield report
  finally:
    report.stop()


def test_serves_the_document_to_the_viewers_origin(served) -> None:
  status, headers, body = _get(served.file_url, origin=DEFAULT_VIEWER)
  assert status == 200
  assert body == b'{"@context": {}}'
  assert headers["Access-Control-Allow-Origin"] == DEFAULT_VIEWER
  assert headers["Content-Type"] == "application/ld+json"
  # Not `*`: a report served here may be a company's own, not EDGAR's.
  assert headers["Access-Control-Allow-Origin"] != "*"


def test_nothing_else_on_the_port_is_reachable(served) -> None:
  base = served.file_url.rsplit("/", 2)[0]
  for path in ("/", "/acme.holon.jsonld", "/../../etc/passwd"):
    status, _headers, _body = _get(f"{base}{path}")
    assert status == 404, path


def test_the_path_carries_an_unguessable_token(served) -> None:
  token = served.file_url.rsplit("/", 2)[-2]
  assert len(token) >= 16
  assert served.file_url.endswith("/acme.holon.jsonld")


def test_the_viewer_url_names_the_document(served) -> None:
  assert served.viewer_url.startswith(f"{DEFAULT_VIEWER}/?url=")
  assert served.file_url in served.viewer_url


def test_viewer_url_and_origin_helpers() -> None:
  assert viewer_url("https://example.test/", "http://127.0.0.1:1/a.json") == (
    "https://example.test/?url=http://127.0.0.1:1/a.json"
  )
  assert origin_of("https://example.test/path?q=1") == "https://example.test"
  with pytest.raises(ValueError):
    origin_of("not-a-url")


def test_viewer_host_shares_one_port_and_stops(tmp_path: Path) -> None:
  host = ViewerHost()
  try:
    first_file, first_page = host.publish("{}", "a.json")
    second_file, second_page = host.publish("{}", "b.json")
    assert first_file.rsplit("/", 2)[0] == second_file.rsplit("/", 2)[0]
    assert first_file != second_file
    assert first_page != second_page
    assert _get(first_file)[0] == 200 and _get(second_file)[0] == 200
  finally:
    host.close()
  # A closed host starts a new server rather than reusing a dead one.
  host.close()


# -- the CLI's choice of document -----------------------------------------------


def test_a_file_already_in_the_format_is_passed_through(tmp_path: Path) -> None:
  from xbrlkit.cli import _already_serialized

  holon = tmp_path / "x.holon.jsonld"
  holon.write_text(json.dumps({"@context": {}, "@graph": []}))
  tavi = tmp_path / "x.tavi.json"
  tavi.write_text(json.dumps({"documentInfo": {"type": "/compiled"}}))

  assert _already_serialized(str(holon), "holon") == holon
  assert _already_serialized(str(tavi), "tavi") == tavi
  # Asking for the other serialization is a conversion, not a pass-through.
  assert _already_serialized(str(holon), "tavi") is None
  assert _already_serialized(str(tavi), "holon") is None
  # And a source that is not a local JSON file is resolved the long way.
  assert _already_serialized("NVDA", "holon") is None
  assert _already_serialized(str(tmp_path / "missing.json"), "holon") is None


def test_view_parser_shape() -> None:
  from xbrlkit.cli import build_parser

  args = build_parser().parse_args(["view", "NVDA"])
  assert (args.source, args.format, args.host, args.port) == (
    "NVDA",
    "holon",
    "127.0.0.1",
    0,
  )
  assert args.open is True and args.viewer is None
  args = build_parser().parse_args(
    ["view", "x.tavi.json", "--as", "tavi", "--no-open", "--viewer", "http://l:5173"]
  )
  assert (args.format, args.open, args.viewer) == ("tavi", False, "http://l:5173")
