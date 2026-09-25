"""Parse: load a SEC XBRL filing with Arelle into the neutral ``XbrlModel``.

Two steps, one contract:

- :func:`load_model` builds a headless Arelle controller (inline-XBRL enabled,
  the DTS served cache-first with per-host spacing and ``Retry-After``
  backoff) and returns the loaded ``ModelXbrl`` — or raises
  :class:`DtsResolutionError` when part of the DTS could not be resolved.
- :func:`to_xbrl_model` walks that ``ModelXbrl`` into a single-filing
  :class:`xbrlkit.model.XbrlModel`.

:func:`close` releases the controller when done. A host with its own Arelle
controller calls :func:`configure_webcache` to put the same cache policy on
it and :func:`register_sec_transforms` to get the SEC inline-XBRL transforms
this package vendors. The cache itself — seeding from a bundle, filling from
the standard entry points, packing for another machine — is
:mod:`xbrlkit.parse.cache`.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
  from xbrlkit.parse.arelle_load import (
    DtsResolutionError,
    LoadState,
    close,
    configure_webcache,
    load_model,
    load_state,
    register_sec_transforms,
  )
  from xbrlkit.parse.to_model import to_xbrl_model

# Loaded on first use (PEP 562), not on import. `xbrlkit.parse.ids` is imported
# by `xbrlkit.periods`, and importing any submodule runs this file first: loading
# Arelle and `to_model` here made `import xbrlkit.periods` circular (to_model
# imports periods) and pulled Arelle into every caller that wanted a period id.
_LAZY: dict[str, str] = {
  "DtsResolutionError": "xbrlkit.parse.arelle_load",
  "LoadState": "xbrlkit.parse.arelle_load",
  "close": "xbrlkit.parse.arelle_load",
  "configure_webcache": "xbrlkit.parse.arelle_load",
  "load_model": "xbrlkit.parse.arelle_load",
  "load_state": "xbrlkit.parse.arelle_load",
  "register_sec_transforms": "xbrlkit.parse.arelle_load",
  "to_xbrl_model": "xbrlkit.parse.to_model",
}


def __getattr__(name: str) -> Any:
  module = _LAZY.get(name)
  if module is None:
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
  value = getattr(importlib.import_module(module), name)
  globals()[name] = value
  return value


__all__ = [
  "DtsResolutionError",
  "LoadState",
  "close",
  "configure_webcache",
  "load_model",
  "load_state",
  "register_sec_transforms",
  "to_xbrl_model",
]
