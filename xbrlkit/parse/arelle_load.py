"""Load a SEC XBRL filing with Arelle into a ``ModelXbrl``, with a cache that
survives a corpus.

Arelle resolves a filing's DTS by fetching every schema and linkbase it
imports: the XBRL core from xbrl.org, the W3C schemas from w3.org, ``dei``,
``srt``, ``ecd``, country and currency from xbrl.sec.gov, the us-gaap year
from xbrl.fasb.org. A 10-K resolves to a few hundred files, and the two
smallest hosts (xbrl.org and w3.org) throttle a cold cache within a few dozen
filings. So the load here is cache-first, with the pieces that keep a corpus
run alive:

- a persistent cache directory in Arelle's own layout, so a seeded bundle
  (:mod:`xbrlkit.parse.cache`) is found by Arelle itself;
- ``https`` for hosts that redirect ``http``, one cache entry per file;
- no re-validation of a cached file (Arelle's weekly recheck, with check
  times that do not survive the process, is one conditional request per DTS
  file per process — the throttle generator on a warm cache);
- request spacing per host on every fetch, and on HTTP 429 / 503 a bounded
  wait for ``Retry-After`` and a retry, in place of Arelle's own tight loop;
- an offline mode that never touches the network;
- and a loud failure when a schema still cannot be resolved:
  :class:`DtsResolutionError` names the URLs, because a filing parsed against
  a partial DTS silently loses every concept the missing schema declared.

Usage::

    mx = load_model(source)          # cache under ~/.cache/xbrlkit/arelle
    model = to_xbrl_model(mx, filing)
    close(mx.modelManager.cntlr)

A host that builds its own Arelle controller (its own cache policy, its own
timeouts) still needs the SEC inline-XBRL transforms this package vendors;
:func:`register_sec_transforms` wires them into Arelle on their behalf.
"""

from __future__ import annotations

import email.utils
import logging
import os
import sys
import time
import urllib.error
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from arelle import Cntlr, PluginManager

from xbrlkit.config import CONFIG, Config
from xbrlkit.edgar.rate_limit import RateLimiter

if TYPE_CHECKING:
  from arelle.ModelXbrl import ModelXbrl

logger = logging.getLogger(__name__)

# SEC inline-XBRL transformation registry. Formatted numeric/date facts in
# modern 10-K/10-Q filings reference transforms in this namespace.
SEC_IXT_NAMESPACE = "http://www.sec.gov/inlineXBRL/transformation/2015-08-31"

# The vendored EDGAR plugin tree — its ``transform`` module carries the SEC ixt
# registry (name↔code tables) that standalone arelle-release lacks.
_VENDOR_PLUGINS = Path(__file__).resolve().parents[1] / "_vendor" / "arelle_plugins"

# Hosts a DTS resolves against. Every fetch is spaced per host; these are the
# ones a cold corpus run has been throttled by, and the ones whose ``http``
# URLs are fetched as ``https`` so a file has one cache entry.
SCHEMA_HOSTS: tuple[str, ...] = (
  "www.w3.org",
  "www.xbrl.org",
  "xbrl.org",
  "xbrl.sec.gov",
  "xbrl.fasb.org",
  "xbrl.ifrs.org",
)

# Fetch policy. ``FETCH_PER_SEC`` is per host and per process; a corpus run
# across worker processes multiplies it, which is still well under what the
# taxonomy hosts serve without complaint when the cache is warm.
FETCH_PER_SEC = 2.0
RETRY_STATUSES = (429, 503)
MAX_RETRIES = 3
MAX_RETRY_AFTER = 60.0
DEFAULT_RETRY_AFTER = 5.0


class DtsResolutionError(RuntimeError):
  """A filing loaded, but part of its DTS could not be resolved.

  ``unresolved`` lists the schema and linkbase URLs Arelle failed to obtain —
  throttled, offline with a cold cache, or simply gone. The model Arelle
  returned would be missing every concept those documents declare, so the
  load refuses it rather than hand back a filing that parses with holes.
  """

  def __init__(self, source: str, unresolved: list[str]) -> None:
    self.source = source
    self.unresolved = unresolved
    shown = ", ".join(unresolved[:5])
    more = f" (+{len(unresolved) - 5} more)" if len(unresolved) > 5 else ""
    super().__init__(
      f"{len(unresolved)} DTS document(s) could not be resolved while loading "
      f"{source}: {shown}{more}"
    )


@dataclass
class LoadState:
  """What one controller's cache layer saw: the URLs it could not resolve
  and how many times it backed off. Attached to the controller as
  ``_xbrlkit_load_state``."""

  unresolved: list[str] = field(default_factory=list)
  backoffs: int = 0

  def note_unresolved(self, url: str) -> None:
    if url not in self.unresolved:
      self.unresolved.append(url)


_limiters: dict[str, RateLimiter] = {}


def _limiter(host: str) -> RateLimiter:
  """One request spacer per host, shared by every controller in the process."""
  limiter = _limiters.get(host)
  if limiter is None:
    limiter = _limiters[host] = RateLimiter(FETCH_PER_SEC)
  return limiter


def load_model(
  source: str | Path,
  cache_dir: Path | None = None,
  *,
  offline: bool | None = None,
  timeout: int | None = None,
  packages: Sequence[str | Path] = (),
  config: Config = CONFIG,
) -> ModelXbrl:
  """Load an XBRL (inline or classic) document and return its ``ModelXbrl``.

  ``source`` may be a local path or an ``http(s)://`` URL. The DTS is served
  from ``cache_dir`` (default :attr:`Config.arelle_cache_dir`) and fetched on
  a miss unless ``offline`` (default :attr:`Config.arelle_offline`), each fetch
  bounded by ``timeout`` seconds (default :attr:`Config.arelle_timeout`).

  ``packages`` are XBRL taxonomy packages to register before loading. A
  filing outside EDGAR usually ships as one: the report references the filer's
  extension taxonomy at *their own domain*, which does not resolve over HTTP,
  and the package's ``META-INF/catalog.xml`` remaps that URL to the copy
  travelling beside it. Without registering it, every ESEF filing fails DTS
  resolution on a URL that was never meant to be fetched.

  Raises :class:`DtsResolutionError` if any DTS document could not be
  resolved, and ``RuntimeError`` if Arelle produced no document at all.

  The controller stays open — its C-extension model is live and the caller
  owns it. Pass ``mx.modelManager.cntlr`` to :func:`close` when done.
  """
  cntlr = _build_controller(
    cache_dir,
    offline=config.arelle_offline if offline is None else offline,
    timeout=config.arelle_timeout if timeout is None else timeout,
    config=config,
  )
  _register_packages(cntlr, packages)
  mx = cntlr.modelManager.load(str(source))
  if mx is None or getattr(mx, "modelDocument", None) is None:
    close(cntlr)
    raise RuntimeError(f"Arelle failed to load an XBRL document from: {source}")
  state = load_state(cntlr)
  if state.unresolved:
    unresolved = list(state.unresolved)
    close(cntlr)
    raise DtsResolutionError(str(source), unresolved)
  return mx


def _register_packages(cntlr: Any, packages: Sequence[str | Path]) -> None:
  """Register taxonomy packages so their catalogs remap the URLs they own."""
  if not packages:
    return
  from arelle import PackageManager

  PackageManager.init(cntlr, loadPackagesConfig=False)
  added = False
  for package in packages:
    try:
      if PackageManager.addPackage(cntlr, str(package)):
        added = True
      else:
        logger.warning("not a taxonomy package: %s", package)
    except Exception as exc:  # a malformed package must not sink the load
      logger.warning("could not register taxonomy package %s: %s", package, exc)
  if added:
    PackageManager.rebuildRemappings(cntlr)


def load_state(cntlr: Any) -> LoadState:
  """The :class:`LoadState` of a controller built by :func:`_build_controller`
  (or a fresh one for a controller that was not)."""
  state = getattr(cntlr, "_xbrlkit_load_state", None)
  if state is None:
    state = cntlr._xbrlkit_load_state = LoadState()
  return state


def close(cntlr: Any) -> None:
  """Close an Arelle controller, releasing its model and file handles."""
  if cntlr is None:
    return
  try:
    cntlr.close()
  except Exception:
    pass


def _build_controller(
  cache_dir: Path | None,
  *,
  offline: bool = False,
  timeout: int = 30,
  config: Config = CONFIG,
) -> Any:
  """Construct a headless Arelle controller wired for inline SEC XBRL, with
  the cache layer of this module on its WebCache."""
  resolved = Path(cache_dir) if cache_dir is not None else config.arelle_cache_dir
  resolved.mkdir(parents=True, exist_ok=True)

  cntlr = Cntlr.Cntlr(
    hasGui=False,
    logFileName="logToBuffer",
    logFileMode="w",
    uiLang=None,
    disable_persistent_config=True,
  )

  _enable_inline_xbrl(cntlr)
  configure_webcache(cntlr, resolved, offline=offline, timeout=timeout)
  return cntlr


def _enable_inline_xbrl(cntlr: Any) -> None:
  """Load the inline-XBRL document-set plugin and SEC ixt transforms.

  Without ``inlineXbrlDocumentSet`` Arelle treats an inline 10-K as plain HTML
  and every fact silently drops, so this wiring is load-bearing for modern
  filings.
  """
  PluginManager.init(cntlr, loadPluginConfig=False)
  try:
    PluginManager.addPluginModule("inlineXbrlDocumentSet")
  except Exception:
    pass
  register_sec_transforms()
  try:
    PluginManager.reset()
  except Exception:
    pass


def register_sec_transforms() -> None:
  """Register the SEC inline-XBRL transformation functions with Arelle.

  The SEC ``2015-08-31`` transforms (``stateprovnameen``, ``edgarprovcountryen``,
  ``numwordsen``, …) are **not** in standalone ``arelle-release`` — they live in
  the EDGAR plugin's ``transform`` module, which this package vendors at
  ``_vendor/arelle_plugins/EDGAR/transform`` (just the transform registry, not the
  matplotlib-backed renderer). Without them, SEC-formatted cover-page/DEI facts
  (state/country codes, some dates/booleans/word-numbers) parse to
  ``(ixTransformValueError)``.

  The plugin publishes its transforms through a ``ModelManager.LoadCustomTransforms``
  mount point that Arelle doesn't invoke in a headless load, so they are registered
  directly into the namespace map Arelle resolves against
  (``FunctionIxt.ixtNamespaceFunctions[ns][localName]``, FunctionIxt.py:34).

  :func:`load_model` calls this itself. It is public for hosts that build their
  own Arelle controller and load through it — the RoboSystems SEC adapter's
  client, for one — so they register the same registry rather than carrying a
  copy of the EDGAR plugin. Process-wide and idempotent: the registration lives
  on Arelle's module, not on a controller, and calling it again is a no-op.
  Safe to call before any controller exists.
  """
  try:
    from arelle import FunctionIxt
  except Exception:
    return
  if str(_VENDOR_PLUGINS) not in sys.path:
    sys.path.insert(0, str(_VENDOR_PLUGINS))
  try:
    from EDGAR.transform import loadSECtransforms  # type: ignore[import-not-found]

    custom: dict[Any, Any] = {}
    loadSECtransforms(custom)
    FunctionIxt.ixtNamespaceFunctions[SEC_IXT_NAMESPACE] = {
      qn.localName: fn for qn, fn in custom.items()
    }
  except Exception:
    pass


# ---- the cache layer ----------------------------------------------------------


def configure_webcache(
  cntlr: Any, cache_dir: Path, *, offline: bool = False, timeout: int = 30
) -> LoadState:
  """Put this module's cache policy on a controller's WebCache.

  Public for hosts with their own controller: after this call the controller
  serves the DTS from ``cache_dir`` (Arelle's layout), fetches on a miss with
  per-host spacing and ``Retry-After`` backoff unless ``offline``, and records
  every document it could not resolve on the returned :class:`LoadState` —
  which :func:`load_state` reads back and :func:`load_model` turns into a
  :class:`DtsResolutionError`.
  """
  webcache = getattr(cntlr, "webCache", None)
  state = load_state(cntlr)
  if webcache is None:
    return state
  webcache.cacheDir = str(cache_dir)
  webcache.workOffline = offline
  webcache.httpsRedirect = True
  webcache.timeout = timeout
  # A cached DTS file never goes stale: taxonomy URLs are versioned, and a
  # new year is a new URL. Arelle's default is to re-validate every cached
  # file older than a week with a conditional request — and since the check
  # times are not persisted between processes, that is one request per file
  # per process, hundreds per filing, on a warm cache. That recheck, not the
  # first fetch, is what throttles a corpus run.
  try:
    webcache.recheck = "never"
  except Exception:
    webcache.maxAgeSeconds = float("inf")
  _wrap_normalize_url(webcache)
  _wrap_download(webcache, state)
  _wrap_getfilename(webcache, state)
  _wrap_opener(webcache, state)
  return state


def _wrap_normalize_url(webcache: Any) -> None:
  """Fetch the schema hosts over ``https`` even when the DTS says ``http``:
  one round trip fewer per file, and one cache entry instead of two."""
  original = webcache.normalizeUrl

  def normalize(url: str | None, base: str | None = None) -> str:
    normalized = original(url, base)
    if isinstance(normalized, str) and normalized.startswith("http://"):
      host = urlparse(normalized).hostname or ""
      if host in SCHEMA_HOSTS:
        return "https://" + normalized[len("http://") :]
    return normalized

  webcache.normalizeUrl = normalize


def _wrap_download(webcache: Any, state: LoadState) -> None:
  """One attempt per Arelle download call — the retries live in the opener,
  where the response headers are — and a record of every URL that failed."""
  original = getattr(webcache, "_downloadFile", None)
  if original is None:
    return

  def download(
    url: str,
    filepath: str,
    retrievingDueToRecheckInterval: bool = False,  # noqa: N803 - Arelle's name
    retryCount: int = 5,  # noqa: N803 - Arelle's name
  ) -> bool:
    ok = original(url, filepath, retrievingDueToRecheckInterval, retryCount=1)
    if not ok:
      state.note_unresolved(url)
    return bool(ok)

  webcache._downloadFile = download


def _wrap_getfilename(webcache: Any, state: LoadState) -> None:
  """Offline, Arelle answers a miss with the path the file *would* have and
  lets the loader fail on it; record the miss so the load can refuse."""
  original = webcache.getfilename

  def getfilename(url: str | None, base: str | None = None, **kwargs: Any) -> Any:
    result = original(url, base, **kwargs)
    if (
      webcache.workOffline
      and not kwargs.get("filenameOnly")
      and isinstance(url, str)
      and url.startswith(("http://", "https://"))
      and isinstance(result, str)
      and not os.path.exists(result)
    ):
      state.note_unresolved(url)
    return result

  webcache.getfilename = getfilename


def _wrap_opener(webcache: Any, state: LoadState) -> None:
  """Space requests per host; on 429 / 503 wait for ``Retry-After`` (bounded)
  and retry a few times, then let the error through so the download is
  recorded as unresolved."""
  opener = getattr(webcache, "opener", None)
  original_open = getattr(opener, "open", None)
  if original_open is None:
    return

  def open_with_backoff(fullurl: Any, data: Any = None, timeout: Any = None) -> Any:
    url = fullurl if isinstance(fullurl, str) else getattr(fullurl, "full_url", "")
    host = urlparse(url).hostname if "://" in url else None
    attempt = 0
    while True:
      if host:
        _limiter(host).wait()
      try:
        return original_open(fullurl, data, timeout)
      except urllib.error.HTTPError as error:
        if error.code not in RETRY_STATUSES or attempt >= MAX_RETRIES:
          raise
        delay = retry_after_seconds(error.headers, attempt)
        state.backoffs += 1
        logger.warning(
          "HTTP %s from %s; waiting %.0fs (retry %d/%d)",
          error.code,
          host or url,
          delay,
          attempt + 1,
          MAX_RETRIES,
        )
        time.sleep(delay)
        attempt += 1

  opener.open = open_with_backoff


def retry_after_seconds(headers: Any, attempt: int) -> float:
  """The wait a 429 / 503 asks for: ``Retry-After`` as seconds or an HTTP
  date, else a doubling default; never more than ``MAX_RETRY_AFTER``."""
  value = None
  if headers is not None:
    try:
      value = headers.get("Retry-After")
    except Exception:
      value = None
  delay: float | None = None
  if value:
    text = str(value).strip()
    if text.isdigit():
      delay = float(text)
    else:
      parsed = email.utils.parsedate_to_datetime(text) if text else None
      if parsed is not None:
        delay = max(0.0, parsed.timestamp() - time.time())
  if delay is None:
    delay = DEFAULT_RETRY_AFTER * (2**attempt)
  return min(delay, MAX_RETRY_AFTER)


__all__ = (
  "DtsResolutionError",
  "LoadState",
  "SCHEMA_HOSTS",
  "SEC_IXT_NAMESPACE",
  "close",
  "configure_webcache",
  "load_model",
  "load_state",
  "register_sec_transforms",
  "retry_after_seconds",
)
