"""Single-filing XBRL download + extraction.

The per-filing artifact is a pure URL construction (no EFTS discovery): the
``{accession}-xbrl.zip`` under ``Archives/edgar/data``. Mind the two CIK
conventions —

* ``data.sec.gov/submissions`` uses the **zero-padded 10-digit** CIK, but
* ``www.sec.gov/Archives/edgar/data/...`` uses the **leading-zeros-stripped**
  CIK (``str(int(cik))``).

The accession keeps its dashes in the ``.zip`` filename but strips them for the
path segment. After extraction we return the primary Arelle load target,
mirroring ``SECClient.get_report_url``: the inline primary ``.htm`` for inline
filings, else the instance ``.xml`` (the sibling of the ``.xsd`` in the zip).

The ``-xbrl.zip`` carries XBRL and nothing else. A classic filing's readable
document — the ``form10-k.htm`` a person opens — is a *sibling* of the zip in
the filing directory, not a member of it, so reading the narrative of anything
filed before iXBRL means fetching it separately: that is
:func:`download_primary_document`.
"""

from __future__ import annotations

from io import BytesIO
from pathlib import Path
from zipfile import ZipFile

import requests

from xbrlkit.config import CONFIG, Config

from .client import EdgarClient


def _xbrl_zip_url(base_url: str, cik: str, accession: str) -> str:
  """Build the Archives ``{accession}-xbrl.zip`` URL for one filing."""
  cik_no_leading_zeros = str(int(cik))
  accession_no_dashes = accession.replace("-", "")
  filename = f"{accession}-xbrl.zip"
  return (
    f"{base_url}/Archives/edgar/data/"
    f"{cik_no_leading_zeros}/{accession_no_dashes}/{filename}"
  )


def _primary_load_target(
  extracted_names: list[str], dest_dir: Path, is_inline: bool, primary_document: str
) -> Path:
  """Resolve the Arelle load target inside an extracted filing.

  Inline filings load from the primary ``.htm``; classic filings load the
  instance ``.xml`` that sits beside the ``.xsd`` (mirrors ``get_report_url``).
  """
  if is_inline and primary_document:
    candidate = dest_dir / primary_document
    if candidate.exists():
      return candidate

  for name in extracted_names:
    if name.endswith(".xsd"):
      instance = name[: -len(".xsd")] + ".xml"
      candidate = dest_dir / instance
      if candidate.exists():
        return candidate

  if is_inline and primary_document:
    return dest_dir / primary_document

  raise FileNotFoundError(
    f"No Arelle load target found in extracted filing at {dest_dir}"
  )


def primary_document_url(base_url: str, cik: str, accession: str, name: str) -> str:
  """The Archives URL of one filing's primary document."""
  return (
    f"{base_url}/Archives/edgar/data/{int(cik)}/{accession.replace('-', '')}/{name}"
  )


def download_primary_document(
  client: EdgarClient,
  cik: str,
  accession: str,
  dest_dir: Path,
  primary_document: str = "",
) -> Path:
  """Download one filing's primary document (the readable form) into ``dest_dir``.

  ``primary_document`` defaults to the name on the filing's EDGAR record.
  Raises :class:`FileNotFoundError` when the filing has no primary document
  named, or the fetch 404s.
  """
  name = primary_document or client.get_filing_ref(cik, accession).primary_document
  if not name:
    raise FileNotFoundError(
      f"EDGAR names no primary document for accession {accession} (CIK {cik})"
    )
  url = primary_document_url(client.config.sec_base_url, cik, accession, name)
  try:
    resp = client._get(url)
  except requests.HTTPError as exc:
    if exc.response is not None and exc.response.status_code == 404:
      raise FileNotFoundError(f"No primary document for {accession} at {url}") from exc
    raise
  dest_dir.mkdir(parents=True, exist_ok=True)
  target = dest_dir / Path(name).name
  target.write_bytes(resp.content)
  return target


def download_filing(
  client: EdgarClient, cik: str, accession: str, dest_dir: Path
) -> Path:
  """Download + extract one filing's XBRL zip; return its Arelle load target.

  Raises a clear error if the ``-xbrl.zip`` is missing (404).
  """
  ref = client.get_filing_ref(cik, accession)
  url = _xbrl_zip_url(client.config.sec_base_url, cik, accession)

  try:
    resp = client._get(url)
  except requests.HTTPError as exc:
    if exc.response is not None and exc.response.status_code == 404:
      raise FileNotFoundError(
        f"No XBRL zip for accession {accession} (CIK {cik}) at {url}"
      ) from exc
    raise

  dest_dir.mkdir(parents=True, exist_ok=True)
  with ZipFile(BytesIO(resp.content)) as archive:
    names = archive.namelist()
    archive.extractall(dest_dir)

  return _primary_load_target(names, dest_dir, ref.is_inline, ref.primary_document)


def fetch(cik: str, accession: str, dest_dir: Path, config: Config = CONFIG) -> Path:
  """Convenience wrapper: build an :class:`EdgarClient` and download."""
  client = EdgarClient(config)
  return download_filing(client, cik, accession, dest_dir)


__all__ = [
  "download_filing",
  "download_primary_document",
  "fetch",
  "primary_document_url",
]
