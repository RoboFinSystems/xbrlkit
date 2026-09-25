"""Every public module imports on its own, in a fresh interpreter.

A module that only imports when something else was imported first is a cycle
waiting for a caller: `import xbrlkit.periods` failed in 0.18.1 because
`xbrlkit.parse` loaded Arelle and `to_model` on import, and `to_model` imports
`periods`. Each module is imported in its own subprocess, since one process
would mask the order dependence the test exists to find.
"""

from __future__ import annotations

import pkgutil
import subprocess
import sys

import pytest

import xbrlkit


def _public_modules() -> list[str]:
  names = [xbrlkit.__name__]
  for info in pkgutil.walk_packages(xbrlkit.__path__, prefix=f"{xbrlkit.__name__}."):
    if any(part.startswith("_") for part in info.name.split(".")):
      continue
    names.append(info.name)
  return sorted(names)


@pytest.mark.parametrize("module", _public_modules())
def test_a_public_module_imports_on_its_own(module: str) -> None:
  result = subprocess.run(
    [sys.executable, "-c", f"import {module}"],
    capture_output=True,
    text=True,
    timeout=120,
  )
  assert result.returncode == 0, result.stderr.strip().splitlines()[-1:]


def test_periods_does_not_load_arelle() -> None:
  """A caller that wants a period id should not pay for Arelle."""
  result = subprocess.run(
    [
      sys.executable,
      "-c",
      "import sys, xbrlkit.periods; print('arelle' in sys.modules)",
    ],
    capture_output=True,
    text=True,
    timeout=120,
  )
  assert result.stdout.strip() == "False", result.stderr
