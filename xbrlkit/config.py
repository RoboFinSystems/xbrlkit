"""Standalone configuration — replaces the robosystems ``config.env`` coupling.

The SEC adapter reads user-agent / base URLs / cache dirs from the platform's
central env config. This package is platform-free, so those settings live here
(env-overridable, or constructed explicitly by the CLI) instead.

SEC fair-access asks for a ``User-Agent`` that identifies you with contact
info. Set ``SEC_GOV_USER_AGENT`` (e.g. ``"Acme Corp ops@acme.com"``) and your
traffic is attributed to you. Leave it unset and EDGAR still works, under a
default that names the project: SEC rate limits per IP, so the shared string
costs no one else their budget. It is a courtesy worth extending anyway, so
the first unattributed EDGAR fetch of a process says so once, on stderr.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

# The fallback identity: the project name and a placeholder address, carrying
# nobody's real contact details — a default holding a person's address would
# put it on every install's traffic. ``example.com`` is reserved (RFC 2606),
# so it can never reach a stranger's inbox. Reaching a real one is what
# SEC_GOV_USER_AGENT is for, and the warning below asks for it.
#
# Deliberately no repository URL: EDGAR answers 403 to any User-Agent
# containing "github.com" (see GITHUB_UA_HELP), whatever else it says.
DEFAULT_USER_AGENT = "xbrlkit xbrlkit@example.com"

GITHUB_UA_HELP = (
  "SEC_GOV_USER_AGENT contains 'github.com', which EDGAR refuses with HTTP "
  "403 no matter what else the header says. Use a name and email instead, "
  "e.g. 'Acme Corp ops@acme.com'."
)

SEC_IDENTITY_HELP = (
  "xbrlkit is fetching from sec.gov under its default User-Agent. SEC fair "
  "access asks you to identify yourself with contact info; set "
  "SEC_GOV_USER_AGENT to a name and email you control:\n"
  "  export SEC_GOV_USER_AGENT='Acme Corp ops@acme.com'\n"
  "or pass --user-agent. For an MCP client the server's own env block carries "
  "it; see the README. Nothing outside EDGAR needs it."
)


def _default_cache_dir() -> Path:
  override = os.environ.get("XBRL_HOLON_CACHE_DIR")
  if override:
    return Path(override)
  return Path.home() / ".cache" / "xbrlkit"


@dataclass(frozen=True)
class Config:
  """Runtime settings. Immutable; the CLI builds one per invocation."""

  # The operator's own identity, or ``None`` until they declare one. Only the
  # SEC clients care; everything else is happy with the default.
  user_agent: str | None = field(
    default_factory=lambda: os.environ.get("SEC_GOV_USER_AGENT") or None
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

  def identity(self) -> str | None:
    """The declared identity, or ``None``.

    Falls back to the environment so an identity set after this config was
    built still counts — the module-level ``CONFIG`` is created at import,
    which is often before a host has loaded its env.
    """
    declared = self.user_agent or os.environ.get("SEC_GOV_USER_AGENT")
    if not declared or not declared.strip():
      return None
    declared = declared.strip()
    if "github.com" in declared.lower():
      warn_github_user_agent()
    return declared

  @property
  def headers(self) -> dict[str, str]:
    """Headers for hosts that require no declared identity."""
    return {"User-Agent": self.identity() or DEFAULT_USER_AGENT}

  @property
  def sec_headers(self) -> dict[str, str]:
    """Headers for sec.gov, warning once when no identity has been declared."""
    identity = self.identity()
    if identity is None:
      warn_undeclared_sec_identity()
      return {"User-Agent": DEFAULT_USER_AGENT}
    return {"User-Agent": identity}


CONFIG = Config()


logger = logging.getLogger(__name__)
_warned_undeclared = False


def warn_undeclared_sec_identity() -> None:
  """Say once per process that EDGAR is being fetched unattributed.

  Once, because this sits behind every EDGAR request and a warning repeated a
  few hundred times through a corpus run is noise the reader learns to skip.
  """
  global _warned_undeclared
  if _warned_undeclared:
    return
  _warned_undeclared = True
  logger.warning("%s", SEC_IDENTITY_HELP)


_warned_github = False


def warn_github_user_agent() -> None:
  """Say once that a github.com User-Agent is refused by EDGAR.

  The failure is otherwise a bare 403 with nothing pointing at the header.
  """
  global _warned_github
  if _warned_github:
    return
  _warned_github = True
  logger.warning("%s", GITHUB_UA_HELP)
