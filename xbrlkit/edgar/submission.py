"""The complete submission — how to read a filing EDGAR never split into files.

Before about 2000, EDGAR stored a filing as one SGML stream and nothing else.
Its documents are inside that stream and were never written out separately:
the filing index lists them with types, descriptions and sizes but an **empty
Document column**, and ``index.json`` reports their sizes with no names, because
there are no names. ``primaryDocument`` in the submissions API is empty for the
same reason. Nothing can be fetched by name, so nothing before 2000 is readable
without splitting the stream — which is where 1994 to 2000 went.

The stream is simple and has been stable across every year sampled:

    -----BEGIN PRIVACY-ENHANCED MESSAGE-----      (optional — some filings
    ...                                            have no PEM wrapper)
    <SEC-DOCUMENT>0001012870-98-000618.txt : 19980309
    <SEC-HEADER>...
    ACCESSION NUMBER:       0001012870-98-000618
    CONFORMED SUBMISSION TYPE:  S-1
    ...
    </SEC-HEADER>
    <DOCUMENT>
    <TYPE>S-1
    <SEQUENCE>1
    <DESCRIPTION>FORM S-1
    <TEXT>
    ...the document...
    </TEXT>
    </DOCUMENT>

``<TYPE>`` and ``<SEQUENCE>`` are on every document; ``<FILENAME>`` is on none
of them, which is the whole problem, so each document is given a name built
from its sequence and type. Sequence 1 is the filing's primary document.

Content is plain text of the era — fixed-width tables, ``<PAGE>`` break markers,
no HTML. A later filing can carry HTML, and a document can be a uuencoded image;
both are detected so the name gets the right extension and an image is not
handed back as if it were prose.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_PEM_START = "-----BEGIN PRIVACY-ENHANCED MESSAGE-----"
_DOCUMENT_RE = re.compile(r"<DOCUMENT>(.*?)</DOCUMENT>", re.S)
_TEXT_RE = re.compile(r"<TEXT>(.*?)</TEXT>", re.S)
_TAG_RE = re.compile(r"^<(TYPE|SEQUENCE|DESCRIPTION|FILENAME)>(.*)$", re.M)
_HEADER_RE = re.compile(r"<SEC-HEADER>(.*?)</SEC-HEADER>", re.S)
_HEADER_FIELD_RE = re.compile(r"^\s*([A-Z][A-Z /&-]+):\s*(.+?)\s*$", re.M)
# A uuencoded body — how the era carried an image inside a text stream.
_UUENCODE_RE = re.compile(r"^begin \d{3} \S+", re.M)
_UNSAFE = re.compile(r"[^A-Za-z0-9.\-]+")
# `<PAGE>` is a printer's page break, not content.
_PAGE_MARKER_RE = re.compile(r"^<PAGE>[ \t]*\r?\n?", re.M)


@dataclass
class SubmissionDocument:
  """One document carved out of a complete submission."""

  sequence: int
  type: str
  description: str
  text: str
  filename: str = ""

  @property
  def is_binary(self) -> bool:
    """Whether the body is uuencoded rather than readable."""
    return bool(_UUENCODE_RE.search(self.text[:2000]))

  @property
  def name(self) -> str:
    """A name for a document EDGAR never gave one.

    ``0001-10-K.txt``: the sequence keeps filing order and uniqueness, the
    type says what it is, and the extension follows the body.
    """
    if self.filename:
      return self.filename
    stem = f"{self.sequence:04d}-{_UNSAFE.sub('-', self.type).strip('-') or 'document'}"
    return f"{stem}{self.suffix}"

  @property
  def suffix(self) -> str:
    if self.is_binary:
      return ".uu"
    head = self.text[:4000].upper()
    return ".htm" if "<HTML" in head or "<!DOCTYPE HTML" in head else ".txt"


def parse_submission(raw: str) -> tuple[dict[str, str], list[SubmissionDocument]]:
  """Split a complete submission into its header fields and its documents."""
  return submission_header(raw), submission_documents(raw)


def submission_header(raw: str) -> dict[str, str]:
  """The ``<SEC-HEADER>`` block's ``KEY: value`` lines.

  Keys are as EDGAR writes them (``CONFORMED SUBMISSION TYPE``), and the first
  occurrence wins — a submission with several filers repeats the address keys.
  """
  found = _HEADER_RE.search(raw)
  if not found:
    return {}
  fields: dict[str, str] = {}
  for key, value in _HEADER_FIELD_RE.findall(found.group(1)):
    fields.setdefault(key.strip(), value.strip())
  return fields


def submission_documents(raw: str) -> list[SubmissionDocument]:
  """Every ``<DOCUMENT>`` in the stream, in filing order."""
  documents: list[SubmissionDocument] = []
  for index, block in enumerate(_DOCUMENT_RE.findall(raw), start=1):
    tags = {key: value.strip() for key, value in _TAG_RE.findall(block)}
    body = _TEXT_RE.search(block)
    text = body.group(1) if body else ""
    sequence = tags.get("SEQUENCE", "")
    documents.append(
      SubmissionDocument(
        sequence=int(sequence) if sequence.isdigit() else index,
        type=tags.get("TYPE", "") or "document",
        description=tags.get("DESCRIPTION", ""),
        filename=tags.get("FILENAME", ""),
        text=_clean(text),
      )
    )
  return documents


def _clean(text: str) -> str:
  """The document body as filed, less the page-break markers."""
  return _PAGE_MARKER_RE.sub("", text).strip("\r\n")


def strip_pem(raw: str) -> str:
  """Drop the privacy-enhanced-message envelope some submissions carry."""
  start = raw.find(_PEM_START)
  if start < 0:
    return raw
  opening = raw.find("<SEC-DOCUMENT>", start)
  return raw[opening:] if opening > 0 else raw


def complete_submission_url(base_url: str, cik: str, accession: str) -> str:
  """The Archives URL of a filing's complete submission text file."""
  return f"{base_url}/Archives/edgar/data/{int(cik)}/{accession}.txt"


__all__ = [
  "SubmissionDocument",
  "complete_submission_url",
  "parse_submission",
  "strip_pem",
  "submission_documents",
  "submission_header",
]
