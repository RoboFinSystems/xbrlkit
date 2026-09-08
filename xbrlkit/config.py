"""Standalone configuration — replaces the robosystems ``config.env`` coupling.

The SEC adapter reads user-agent / base URLs / cache dirs from the platform's
central env config. This package is platform-free, so those settings live here
(env-overridable, or constructed explicitly by the CLI) instead.

SEC fair-access requires a ``User-Agent`` that identifies you with contact
info. Set ``SEC_GOV_USER_AGENT`` (e.g. ``"Acme Corp ops@acme.com"``) or the
SEC will throttle you with empty/HTTP-429 responses.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_USER_AGENT = "xbrlkit (set SEC_GOV_USER_AGENT='Name email@example.com')"


def _default_cache_dir() -> Path:
  override = os.environ.get("XBRL_HOLON_CACHE_DIR")
  if override:
    return Path(override)
  return Path.home() / ".cache" / "xbrlkit"


@dataclass(frozen=True)
class Config:
  """Runtime settings. Immutable; the CLI builds one per invocation."""

  user_agent: str = field(
    default_factory=lambda: os.environ.get("SEC_GOV_USER_AGENT", DEFAULT_USER_AGENT)
  )
  sec_base_url: str = "https://www.sec.gov"
  sec_data_url: str = "https://data.sec.gov"
  # XBRL International's public index of filings outside EDGAR — ESEF and the
  # national regimes that publish through it. Open, and it asks for no key.
  filings_base_url: str = "https://filings.xbrl.org"
  request_timeout: int = 30
  rate_limit_per_sec: float = 5.0
  # EDGAR answers a throttled client with an empty 200 as often as a 429;
  # this is the wait before the one retry either gets.
  throttle_backoff_s: float = field(
    default_factory=lambda: float(os.environ.get("XBRLKIT_THROTTLE_BACKOFF", "45"))
  )
  cache_dir: Path = field(default_factory=_default_cache_dir)
  # Arelle's DTS fetches: per-fetch timeout, and whether to fetch at all.
  arelle_timeout: int = field(
    default_factory=lambda: int(os.environ.get("XBRLKIT_ARELLE_TIMEOUT", "30"))
  )
  arelle_offline: bool = field(
    default_factory=lambda: (
      os.environ.get("XBRLKIT_ARELLE_OFFLINE", "").lower() in ("1", "true", "yes")
    )
  )

  @property
  def arelle_cache_dir(self) -> Path:
    override = os.environ.get("XBRLKIT_ARELLE_CACHE_DIR")
    return Path(override) if override else self.cache_dir / "arelle"

  @property
  def headers(self) -> dict[str, str]:
    return {"User-Agent": self.user_agent}


CONFIG = Config()
