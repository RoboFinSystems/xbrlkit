"""server.json, the Official MCP Registry listing, tracks the package.

Clients that install from the registry run exactly the version server.json
pins, so it has to be the version pyproject.toml releases. create-release.yml
bumps both in one commit and publish.yml lists it after the PyPI upload; these
tests keep a hand edit from splitting them, and keep the description true.
"""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _listing() -> dict:
  return json.loads((ROOT / "server.json").read_text())


def _project() -> dict:
  return tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]


def test_the_listing_pins_the_released_version() -> None:
  listing = _listing()
  version = _project()["version"]
  assert listing["version"] == version
  assert [package["version"] for package in listing["packages"]] == [version]


def test_the_listing_names_this_package_and_its_readme_carries_the_token() -> None:
  listing = _listing()
  project = _project()
  assert [package["identifier"] for package in listing["packages"]] == [project["name"]]
  # The registry proves PyPI ownership by finding this line in the README.
  readme = (ROOT / project["readme"]).read_text()
  assert f"mcp-name: {listing['name']}" in readme


def test_the_description_fits_the_registry() -> None:
  assert len(_listing()["description"]) <= 100


@pytest.mark.asyncio
async def test_a_tool_count_in_the_description_is_the_servers(tmp_path: Path) -> None:
  match = re.search(r"\b(\d+) tools\b", _listing()["description"])
  if match is None:
    pytest.skip("the description names no tool count")
  from mcp.client import Client

  from xbrlkit.serve import FilingSession, build_server

  session = FilingSession()
  try:
    async with Client(build_server(session, tmp_path)) as client:
      tools = (await client.list_tools()).tools
  finally:
    session.close()
  assert int(match.group(1)) == len(tools)
