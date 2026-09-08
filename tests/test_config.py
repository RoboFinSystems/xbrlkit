"""The SEC User-Agent / ``.env`` config contract.

The CLI calls ``load_dotenv()`` at startup and then builds a fresh ``Config``,
so a ``SEC_GOV_USER_AGENT`` set in a local ``.env`` reaches the EDGAR client.
These tests exercise that env -> ``Config`` path without any network access.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

import pytest

import xbrlkit.config as config_module
from xbrlkit.config import DEFAULT_USER_AGENT, Config


def test_env_user_agent_overrides_default(monkeypatch):
  monkeypatch.setenv("SEC_GOV_USER_AGENT", "Acme Corp ops@acme.com")
  cfg = Config()
  assert cfg.user_agent == "Acme Corp ops@acme.com"
  assert cfg.headers["User-Agent"] == "Acme Corp ops@acme.com"


def test_no_identity_when_unset(monkeypatch):
  """Unset means unset — the config never invents an identity."""
  monkeypatch.delenv("SEC_GOV_USER_AGENT", raising=False)
  assert Config().user_agent is None


def test_generic_headers_name_the_software_not_an_operator(monkeypatch):
  """Every host gets a default whose address is reserved, so it reaches nobody."""
  monkeypatch.delenv("SEC_GOV_USER_AGENT", raising=False)
  agent = Config().headers["User-Agent"]
  assert agent == DEFAULT_USER_AGENT
  assert agent.endswith("@example.com")


def test_sec_headers_fall_back_to_the_default(monkeypatch):
  """EDGAR still works undeclared — the default is a courtesy, not a gate."""
  monkeypatch.delenv("SEC_GOV_USER_AGENT", raising=False)
  monkeypatch.setattr(config_module, "_warned_undeclared", False)
  assert Config().sec_headers == {"User-Agent": DEFAULT_USER_AGENT}


@pytest.mark.parametrize("blank", ["", "   "])
def test_a_blank_identity_is_no_identity(monkeypatch, blank):
  monkeypatch.setenv("SEC_GOV_USER_AGENT", blank)
  monkeypatch.setattr(config_module, "_warned_undeclared", False)
  assert Config().sec_headers == {"User-Agent": DEFAULT_USER_AGENT}


def test_undeclared_identity_warns_exactly_once(monkeypatch, caplog):
  """Behind every EDGAR request, so a repeat per call would be pure noise."""
  monkeypatch.delenv("SEC_GOV_USER_AGENT", raising=False)
  monkeypatch.setattr(config_module, "_warned_undeclared", False)
  cfg = Config()
  with caplog.at_level("WARNING", logger="xbrlkit.config"):
    for _ in range(3):
      cfg.sec_headers
  warnings = [r for r in caplog.records if "SEC_GOV_USER_AGENT" in r.getMessage()]
  assert len(warnings) == 1


def test_a_declared_identity_never_warns(monkeypatch, caplog):
  monkeypatch.setenv("SEC_GOV_USER_AGENT", "Acme Corp ops@acme.com")
  monkeypatch.setattr(config_module, "_warned_undeclared", False)
  with caplog.at_level("WARNING", logger="xbrlkit.config"):
    Config().sec_headers
  assert not caplog.records


def test_sec_headers_pass_a_declared_identity_through(monkeypatch):
  monkeypatch.setenv("SEC_GOV_USER_AGENT", "Acme Corp ops@acme.com")
  assert Config().sec_headers == {"User-Agent": "Acme Corp ops@acme.com"}


def test_dotenv_file_populates_config(tmp_path):
  """A ``.env`` loaded via ``load_dotenv`` flows into a fresh ``Config``."""
  # load_dotenv mutates os.environ directly, so save/restore around it rather
  # than relying on monkeypatch (which can't unwind that external mutation).
  original = os.environ.pop("SEC_GOV_USER_AGENT", None)
  try:
    env = tmp_path / ".env"
    env.write_text('SEC_GOV_USER_AGENT="Dotenv User dev@example.com"\n')
    load_dotenv(dotenv_path=env, override=True)
    assert Config().user_agent == "Dotenv User dev@example.com"
  finally:
    if original is None:
      os.environ.pop("SEC_GOV_USER_AGENT", None)
    else:
      os.environ["SEC_GOV_USER_AGENT"] = original


def test_env_example_template_is_tracked_and_documents_user_agent():
  """The tracked template must survive the ``.env*`` gitignore un-ignore."""
  example = Path(__file__).resolve().parent.parent / ".env.example"
  assert example.exists()
  assert "SEC_GOV_USER_AGENT" in example.read_text()


def test_default_identity_carries_no_personal_contact():
  """The fallback names the project and a reserved address, nobody's inbox."""
  assert DEFAULT_USER_AGENT == "xbrlkit xbrlkit@example.com"
  assert "github.com" not in DEFAULT_USER_AGENT


def test_a_github_user_agent_warns(monkeypatch, caplog):
  """EDGAR 403s any User-Agent mentioning github.com, with no hint why."""
  monkeypatch.setenv("SEC_GOV_USER_AGENT", "xbrlkit (+https://github.com/acme/x)")
  monkeypatch.setattr(config_module, "_warned_github", False)
  with caplog.at_level("WARNING", logger="xbrlkit.config"):
    Config().sec_headers
  assert any("github.com" in r.getMessage() for r in caplog.records)
