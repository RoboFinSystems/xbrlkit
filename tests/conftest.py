"""Shared pytest fixtures for the xbrlkit test suite."""

from pathlib import Path

import pytest


@pytest.fixture
def sample_output_dir(tmp_path: Path) -> Path:
  """Return a temporary directory for tests that write output artifacts."""
  out = tmp_path / "output"
  out.mkdir()
  return out


@pytest.fixture(autouse=True)
def declared_sec_identity(monkeypatch):
  """Give every test a SEC identity of its own.

  The EDGAR and EFTS clients refuse to start without one. Setting a fixed
  test value here keeps the suite hermetic: it neither depends on the
  developer's ``SEC_GOV_USER_AGENT`` nor sends their address anywhere. Tests
  that exercise the absent case delete it themselves.
  """
  monkeypatch.setenv("SEC_GOV_USER_AGENT", "xbrlkit tests tests@example.com")
