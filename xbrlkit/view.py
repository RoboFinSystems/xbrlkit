"""Hand one report document to a browser-based viewer, from the command line.

A browser cannot be handed a local path: ``file://`` is unreachable from an
https page, and a file input cannot be pre-populated programmatically — that
would be a file-exfiltration hole. So this does not drive the file picker. It
**serves the file**, on the loopback interface, and opens the viewer at a URL
that names it.

The fact that makes that work: ``http://127.0.0.1`` and ``http://localhost``
are *potentially trustworthy origins* in the browser's security model, so
mixed-content blocking does not apply to them — an https page may fetch from a
local http server. Verified against Chrome 152 on 2026-09-08: the hosted
viewer (then at ``https://holon.robosystems.ai``, now ``https://xbrlkit.com``)
fetched and rendered a filing served here, with no Local Network Access prompt
in the way.

The only machinery this needs, then, is a CORS header, because the viewer's
origin is not this one.

**Posture.** Loopback only, one document per path, and each path carries an
unguessable token — so the document is readable by the viewer origin, for as
long as the process runs, and not by every other page the browser has open.
``Access-Control-Allow-Origin`` names that one origin rather than ``*`` for
the same reason: a report served here may be a company's own, not EDGAR's.
"""

from __future__ import annotations

import secrets
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, urlsplit

# The hosted xbrlkit viewer: reads a holon.jsonld or a tavi.json, entirely in
# the browser. Overridable for a local build or a fork. Releases before 0.10
# named the viewer's earlier home, https://holon.robosystems.ai, which keeps
# serving the same app as an alias — the CORS header below names exactly one
# origin, so the old name must keep working for those installs.
DEFAULT_VIEWER = "https://xbrlkit.com"

_CONTENT_TYPES = {
  ".jsonld": "application/ld+json",
  ".json": "application/json",
}


def origin_of(url: str) -> str:
  """The scheme-and-authority of ``url`` — what a CORS header names."""
  parts = urlsplit(url)
  if not parts.scheme or not parts.netloc:
    raise ValueError(f"not an absolute URL: {url!r}")
  return f"{parts.scheme}://{parts.netloc}"


def viewer_url(viewer: str, file_url: str) -> str:
  """The viewer page that opens ``file_url`` — the ``?url=`` link."""
  return f"{viewer.rstrip('/')}/?url={quote(file_url, safe=':/')}"


class _Handler(BaseHTTPRequestHandler):
  """Serves exactly the documents registered on the server, and nothing else.

  No filesystem is reachable from here: the routing table is a dict the
  server owns, so a path this does not recognise is a 404 and there is
  nothing for a traversal to traverse to.
  """

  protocol_version = "HTTP/1.1"
  server: "_Server"  # pyright: ignore[reportIncompatibleVariableOverride]

  def _cors(self) -> None:
    self.send_header("Access-Control-Allow-Origin", self.server.origin)
    self.send_header("Vary", "Origin")

  def _empty(self, code: int) -> None:
    self.send_response(code)
    if code == 204:
      self._cors()
      self.send_header("Access-Control-Allow-Methods", "GET, HEAD, OPTIONS")
      self.send_header("Access-Control-Max-Age", "600")
    self.send_header("Content-Length", "0")
    self.end_headers()

  def do_OPTIONS(self) -> None:  # noqa: N802  # the stdlib's naming
    self._empty(204 if self.path in self.server.documents else 404)

  def do_HEAD(self) -> None:  # noqa: N802
    self._send(head_only=True)

  def do_GET(self) -> None:  # noqa: N802
    self._send(head_only=False)

  def _send(self, head_only: bool) -> None:
    document = self.server.documents.get(self.path)
    if document is None:
      self._empty(404)
      return
    body, content_type = document
    self.send_response(200)
    self._cors()
    self.send_header("Content-Type", content_type)
    self.send_header("Content-Length", str(len(body)))
    self.send_header("Cache-Control", "no-store")
    self.end_headers()
    if not head_only:
      self.wfile.write(body)

  def log_message(self, format: str, *args: object) -> None:
    """Silence the stdlib's stderr access log; the CLI prints its own notice."""


class _Server(ThreadingHTTPServer):
  daemon_threads = True

  def __init__(self, address: tuple[str, int], origin: str) -> None:
    super().__init__(address, _Handler)
    self.origin = origin
    self.documents: dict[str, tuple[bytes, str]] = {}


@dataclass
class ReportServer:
  """A loopback HTTP server holding report documents for one viewer origin.

  Runs on a daemon thread from construction until :meth:`stop`. Built by
  :func:`serve_report`, or directly when more than one document is wanted.
  """

  viewer: str
  _server: _Server
  _thread: threading.Thread

  @property
  def base_url(self) -> str:
    host, port = self._server.server_address[0], self._server.server_address[1]
    return f"http://{host}:{port}"

  def add(self, body: bytes | str, filename: str) -> str:
    """Register one document and return the URL that serves it."""
    if isinstance(body, str):
      body = body.encode("utf-8")
    suffix = "".join(Path(filename).suffixes[-1:])
    content_type = _CONTENT_TYPES.get(suffix, "application/octet-stream")
    path = f"/{secrets.token_urlsafe(12)}/{quote(Path(filename).name)}"
    self._server.documents[path] = (body, content_type)
    return f"{self.base_url}{path}"

  def open_url(self, file_url: str) -> str:
    """The viewer page for a URL this server returned."""
    return viewer_url(self.viewer, file_url)

  def stop(self) -> None:
    self._server.shutdown()
    self._server.server_close()
    self._thread.join(timeout=5)


def start_server(
  *,
  viewer: str = DEFAULT_VIEWER,
  host: str = "127.0.0.1",
  port: int = 0,
) -> ReportServer:
  """Start a :class:`ReportServer`. ``port=0`` takes an ephemeral one."""
  server = _Server((host, port), origin_of(viewer))
  thread = threading.Thread(
    target=server.serve_forever, name="xbrlkit-view", daemon=True
  )
  thread.start()
  return ReportServer(viewer=viewer, _server=server, _thread=thread)


@dataclass
class ServedReport:
  """One document being served, and the viewer link that opens it."""

  server: ReportServer
  file_url: str
  viewer_url: str

  def stop(self) -> None:
    self.server.stop()


def serve_report(
  body: bytes | str,
  filename: str,
  *,
  viewer: str = DEFAULT_VIEWER,
  host: str = "127.0.0.1",
  port: int = 0,
) -> ServedReport:
  """Serve one report document and return its viewer link."""
  server = start_server(viewer=viewer, host=host, port=port)
  file_url = server.add(body, filename)
  return ServedReport(
    server=server, file_url=file_url, viewer_url=server.open_url(file_url)
  )


class ViewerHost:
  """One :class:`ReportServer` per process, started when something first
  wants it and kept until the process ends.

  A long-running host — the MCP server — publishes a document and returns
  before the browser has fetched it, so the server has to outlive the call
  that created it. Everything published goes to the same port; a document
  is only reachable by the token in its own path.
  """

  def __init__(
    self,
    viewer: str = DEFAULT_VIEWER,
    *,
    host: str = "127.0.0.1",
    port: int = 0,
  ) -> None:
    self.viewer = viewer
    self._host = host
    self._port = port
    self._server: ReportServer | None = None
    self._lock = threading.Lock()

  def publish(self, body: bytes | str, filename: str) -> tuple[str, str]:
    """Serve one document; return ``(file_url, viewer_url)``."""
    with self._lock:
      if self._server is None:
        self._server = start_server(
          viewer=self.viewer, host=self._host, port=self._port
        )
      server = self._server
    file_url = server.add(body, filename)
    return file_url, server.open_url(file_url)

  def close(self) -> None:
    with self._lock:
      server, self._server = self._server, None
    if server is not None:
      server.stop()
