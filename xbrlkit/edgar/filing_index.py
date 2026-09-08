"""A filing's other documents — the exhibits, and whatever else was filed with it.

The ``-xbrl.zip`` is XBRL and the primary document is one file, but a filing is
a *set* of documents and the rest of the set is often where the content is:

* an **8-K** is boilerplate with an ``EX-99.1`` press release attached — the
  form says a disclosure occurred, the exhibit is what was disclosed;
* a **13F-HR**'s primary document is a cover page. Every holding is in a second
  document, the ``INFORMATION TABLE``. Reading only the primary gets the
  manager's name and none of their positions;
* a **10-K** carries EX-21 subsidiaries, EX-31/32 certifications, and EX-10
  material contracts.

EDGAR's filing index page lists them with their types, and it lists *only* the
real ones: the FY2018 10-K's index has 15 documents where the complete
submission has 93 — the 76 the SEC's own renderer generated (``R1.htm`` …) are
not there. That makes the index page, at ~12 KB, both the cheapest and the
cleanest way to find out what a filing contains. Fetching the complete
submission instead would cost 3.7x the bytes and hand back the renderer's
output to filter out again.

Two things are dropped from what it lists. The **XBRL package** (``EX-101.*``)
is already loaded from the zip. And a document EDGAR serves twice — a source
``.xml`` beside its rendered ``.html`` twin, as 13F does — is kept once, as the
source, for the same reason the ownership forms are read from the submitted XML
rather than the page EDGAR renders from it.
"""

from __future__ import annotations

from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path

from .client import EdgarClient

# The XBRL package, already loaded from the `-xbrl.zip`.
_XBRL_PACKAGE_PREFIX = "EX-101"
# Types that are not text: images the filer embedded, the renderer's spreadsheet
# and zip copies of the filing itself.
_NOT_TEXT = {"GRAPHIC", "EXCEL", "ZIP", "JSON"}


@dataclass(frozen=True)
class FilingDocument:
  """One document in a filing, as EDGAR's index page lists it."""

  seq: int
  type: str
  document: str
  description: str = ""
  size: int = 0

  @property
  def suffix(self) -> str:
    return Path(self.document).suffix.lower()

  @property
  def is_xbrl_package(self) -> bool:
    return self.type.upper().startswith(_XBRL_PACKAGE_PREFIX)

  @property
  def is_text(self) -> bool:
    return self.type.upper() not in _NOT_TEXT


class _IndexTableParser(HTMLParser):
  """Rows of EDGAR's filing-index tables, as lists of cell text."""

  def __init__(self) -> None:
    super().__init__(convert_charrefs=True)
    self.rows: list[list[str]] = []
    self._row: list[str] | None = None
    self._cell: list[str] | None = None

  def handle_starttag(self, tag: str, attrs: object) -> None:
    if tag == "tr":
      self._row = []
    elif tag in ("td", "th") and self._row is not None:
      self._cell = []

  def handle_endtag(self, tag: str) -> None:
    if tag in ("td", "th") and self._row is not None and self._cell is not None:
      self._row.append("".join(self._cell).strip())
      self._cell = None
    elif tag == "tr" and self._row is not None:
      self.rows.append(self._row)
      self._row = None

  def handle_data(self, data: str) -> None:
    if self._cell is not None:
      self._cell.append(data)


def parse_filing_index(html: str) -> list[FilingDocument]:
  """The documents listed on a filing's index page, in sequence order.

  EDGAR lays them out as ``Seq | Description | Document | Type | Size`` across
  two tables (document files, then data files); both are read, and rows without
  a sequence number — headers, the complete-submission row — are skipped.
  """
  parser = _IndexTableParser()
  parser.feed(html)
  found: list[FilingDocument] = []
  for row in parser.rows:
    if len(row) < 5 or not row[0].strip().isdigit():
      continue
    name = row[2].strip()
    if not name:
      continue
    # EDGAR appends a marker to the name of an inline document ("x.htm iXBRL").
    name = name.split()[0]
    found.append(
      FilingDocument(
        seq=int(row[0].strip()),
        type=row[3].strip(),
        document=name,
        description=row[1].strip(),
        size=int(row[4].strip()) if row[4].strip().isdigit() else 0,
      )
    )
  return found


def other_documents(
  documents: list[FilingDocument], primary_document: str = ""
) -> list[FilingDocument]:
  """The documents worth reading besides the primary one and the XBRL package.

  A source document and its rendered twin — the same stem under the same
  sequence number, one ``.xml`` and one ``.html`` — collapse to the source.
  """
  primary = Path(primary_document).name.lower()
  # Twins are found across *every* listed document, not only the kept ones: a
  # 13F's `primary_doc.html` is the rendered twin of the primary itself, and
  # dropping the primary first would leave its rendering behind as an "other".
  sources = {
    (doc.seq, Path(doc.document).stem.lower())
    for doc in documents
    if doc.suffix == ".xml"
  }
  keep: list[FilingDocument] = []
  for doc in documents:
    if doc.is_xbrl_package or not doc.is_text:
      continue
    if doc.document.lower() == primary:
      continue
    keep.append(doc)
  return [
    doc
    for doc in keep
    if doc.suffix == ".xml" or (doc.seq, Path(doc.document).stem.lower()) not in sources
  ]


def fetch_filing_index(
  client: EdgarClient, cik: str, accession: str
) -> list[FilingDocument]:
  """Fetch and read one filing's index page."""
  url = (
    f"{client.config.sec_base_url}/Archives/edgar/data/"
    f"{int(cik)}/{accession.replace('-', '')}/{accession}-index.htm"
  )
  return parse_filing_index(client._get(url).text)


__all__ = [
  "FilingDocument",
  "fetch_filing_index",
  "other_documents",
  "parse_filing_index",
]
