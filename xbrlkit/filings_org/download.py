"""Fetching one filing from filings.xbrl.org.

A filing arrives one of two ways. Most are a **taxonomy package** — a zip
carrying the report and the filer's own extension taxonomy, which is what makes
them loadable without reaching the filer's domain. The rest are a bare report,
and those resolve their taxonomy over the network or not at all.
"""

from __future__ import annotations

from pathlib import Path

from xbrlkit.config import CONFIG, Config

from .client import FilingRecord, FilingsOrgClient


def package_url(record: FilingRecord, config: Config = CONFIG) -> str:
  """The absolute URL of a filing's package, or its report when it has none."""
  path = record.package_url or record.report_url
  if not path:
    return ""
  return path if path.startswith("http") else f"{config.filings_base_url}{path}"


def download_filing(
  client: FilingsOrgClient, record: FilingRecord, dest_dir: Path
) -> Path:
  """Download a filing into ``dest_dir`` and return the file to load.

  The package when there is one — it carries the filer's extension taxonomy,
  so the report can resolve without reaching their domain — else the report.
  """
  url = package_url(record, client.config)
  if not url:
    raise FileNotFoundError(
      f"{record.fxo_id} has neither a package nor a report in the index"
    )
  resp = client._session.get(
    url, headers=client.config.headers, timeout=max(client.config.request_timeout, 300)
  )
  resp.raise_for_status()
  dest_dir.mkdir(parents=True, exist_ok=True)
  target = dest_dir / Path(url.split("?", 1)[0]).name
  target.write_bytes(resp.content)
  return target


def fetch(fxo_id: str, dest_dir: Path, config: Config = CONFIG) -> Path:
  """Convenience wrapper: resolve one filing by id and download it."""
  client = FilingsOrgClient(config)
  return download_filing(client, client.filing(fxo_id), dest_dir)


__all__ = ["download_filing", "fetch", "package_url"]
