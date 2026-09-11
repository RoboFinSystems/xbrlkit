"""The filings a running server holds, and how each one gets there.

A :class:`FilingSession` turns a *source* — a local file, directory or zip,
an ``http(s)`` URL, an EDGAR ``cik:accession`` pair, a ticker, or — outside
the SEC — an ``lei:`` or a filings.xbrl.org filing id — into a
:class:`LoadedFiling`: the neutral :class:`~xbrlkit.model.XbrlModel`, the
primary document as plain text (the search and read target), and a section
map over that text (the 10-K Items and the tagged text blocks, with their
character offsets). Arelle is entered under a process-wide lock because it
keeps global state; the parsed model is what the tools read.
"""

from __future__ import annotations

import logging
import re
import shutil
import tempfile
import threading
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Literal
from zipfile import ZipFile

import requests
from xml.etree import ElementTree

from xbrlkit.config import CONFIG, Config
from xbrlkit.deserialize import HolonError, TaviError, from_holon_json, from_tavi_json
from xbrlkit.edgar.filing_index import FilingDocument
from xbrlkit.model import Concept, EntityIdentity, FilingMeta, XbrlFact, XbrlModel
from xbrlkit.text.ixbrl import _strip_html, iXBRLParser
from xbrlkit.text.narrative import NarrativeExtractor, _html_to_text
from xbrlkit.text.xml import XmlDocument, parse_xml_document, raw_document_name
from xbrlkit.text.xml import render as render_xml

SectionKind = Literal["item", "text_block", "records"]

_INLINE_SUFFIXES = {".htm", ".html", ".xhtml"}
_PLAIN_SUFFIXES = {".txt", ".md"}
# A report serialized as JSON — read into the model here, never by Arelle.
_JSON_SUFFIXES = {".json", ".jsonld"}
# What a document-only filing can be read from. A PDF (an ARS, an SEC comment
# letter) is a document EDGAR holds and this cannot read; it is named as such
# rather than loaded empty.
_DOCUMENT_SUFFIXES = _INLINE_SUFFIXES | _PLAIN_SUFFIXES | {".xml"}
_ACCESSION_RE = re.compile(r"^\d{10}-\d{2}-\d{6}$")
_CIK_ACCESSION_RE = re.compile(r"^(\d{1,10})[:/](\d{10}-\d{2}-\d{6})$")
# filings.xbrl.org: a filer by LEI (20 alphanumerics), or one filing by the
# index's own id. Neither can be mistaken for an EDGAR accession or a ticker.
_LEI_RE = re.compile(r"^lei[:/]([A-Za-z0-9]{20})$", re.IGNORECASE)
_FXO_RE = re.compile(
  r"^(?:fxo[:/])?([A-Za-z0-9]{20}-\d{4}-\d{2}-\d{2}-[A-Za-z0-9\-]+)$"
)
_TICKER_RE = re.compile(r"^([A-Za-z][A-Za-z0-9.\-]{0,9})(?:\s+([0-9A-Za-z\-/]+))?$")

logger = logging.getLogger(__name__)

# Arelle's controller is process-global; one load at a time.
_ARELLE_LOCK = threading.Lock()


@dataclass
class TextSection:
  """One region of the primary document a reader can name: a 10-K Item or a
  tagged text block. ``offset`` is its start in :attr:`LoadedFiling.text`
  when it could be located there, else ``None``."""

  id: str
  label: str
  kind: SectionKind
  chars: int
  offset: int | None = None
  elements: list[str] = field(default_factory=list)


@dataclass
class LoadedFiling:
  """One filing the server holds: the model plus its readable text.

  ``text`` / ``sections`` are the whole primary document when the filing
  came with one, else the tagged text blocks; ``block_text`` /
  ``block_sections`` are always the tagged text blocks alone — the form's
  own text, which is what a faithful reading of the serialization searches
  when the document is held out as a control.
  """

  id: str
  source: str
  model: XbrlModel
  text: str
  sections: list[TextSection]
  load_target: Path | None = None
  package_dir: Path | None = None
  block_text: str = ""
  block_sections: list[TextSection] = field(default_factory=list)
  has_document: bool = False
  # The parsed document of an XML filing (a Form 4, a 13F): its fields and
  # record tables. None for XBRL and HTML filings.
  xml_document: XmlDocument | None = None
  # The filing's other documents, listed from EDGAR's index page on first ask
  # and each one read on first read. Nothing is fetched until something asks.
  other_documents: list[FilingDocument] | None = None
  read_documents: dict[str, "ReadDocument"] = field(default_factory=dict)
  # The document the filing was loaded from, when it already is one of the
  # serializations this server hands out (a holon, a TAVI), and which one.
  # ``view_filing`` serves that document as it is rather than a re-projection
  # of the model: a producer's document may say things the model has no slot
  # for, and the reader who asked to see the report asked to see that one.
  source_document: str | None = None
  source_kind: str | None = None

  @property
  def has_xbrl(self) -> bool:
    """Whether the filing carries XBRL at all, or is document-only."""
    return bool(self.model.facts or self.model.concepts)

  def __post_init__(self) -> None:
    # A filing built without a document (a classic instance, a model.json, a
    # hand-authored model in a test) has one rendering: its text blocks.
    if not self.has_document and not self.block_text:
      self.block_text, self.block_sections = self.text, self.sections

  @property
  def accession(self) -> str:
    return self.model.filing.accession

  def readable(self, whole: bool = True) -> tuple[str, list[TextSection]]:
    """The text the text tools read: the whole document when ``whole`` and
    one is held, else the tagged text blocks."""
    if whole and self.has_document:
      return self.text, self.sections
    return self.block_text, self.block_sections


class SourceError(ValueError):
  """The source could not be resolved to a filing."""


class NoXbrlFound(SourceError):
  """Arelle read the source and found no XBRL in it.

  Distinct from a source it could not read at all, because a document with no
  XBRL is a filing this server still holds — most of EDGAR is one — and only
  the caller knows whether a document is on hand to read instead.
  """


class FilingSession:
  """The filings loaded into one server process, by id."""

  def __init__(self, config: Config = CONFIG) -> None:
    self.config = config
    self._filings: dict[str, LoadedFiling] = {}
    self._tmp = Path(tempfile.mkdtemp(prefix="xbrlkit-serve-"))
    self._lock = threading.Lock()

  # -- lookup ---------------------------------------------------------------

  def ids(self) -> list[str]:
    return list(self._filings)

  def all(self) -> list[LoadedFiling]:
    return list(self._filings.values())

  def get(self, filing: str | None = None) -> LoadedFiling:
    """The filing named by ``filing``; the only one when it is omitted."""
    if filing:
      hit = self._filings.get(filing)
      if hit is None:
        wanted = filing.lower()
        for candidate in self._filings.values():
          ticker = candidate.model.entity.ticker or ""
          if wanted in (ticker.lower(), candidate.accession.lower()):
            hit = candidate
            break
      if hit is None:
        raise SourceError(
          f"No loaded filing {filing!r}; loaded: {self.ids() or 'none'}. "
          "Call load_filing first."
        )
      return hit
    if len(self._filings) == 1:
      return next(iter(self._filings.values()))
    if not self._filings:
      raise SourceError("No filing is loaded. Call load_filing with a source first.")
    raise SourceError(
      f"Several filings are loaded ({self.ids()}); pass `filing` to choose one."
    )

  # -- loading ---------------------------------------------------------------

  def load(self, source: str, filing_id: str | None = None) -> LoadedFiling:
    """Resolve ``source`` and load it; returns the new :class:`LoadedFiling`."""
    source = source.strip()
    if not source:
      raise SourceError("An empty source.")
    with self._lock:
      loaded = self._load(source)
      wanted = filing_id or loaded.id
      loaded.id = self._unique_id(wanted)
      self._filings[loaded.id] = loaded
      return loaded

  def unload(self, filing: str) -> str:
    hit = self.get(filing)
    del self._filings[hit.id]
    if hit.package_dir is not None and self._tmp in hit.package_dir.parents:
      shutil.rmtree(hit.package_dir, ignore_errors=True)
    return hit.id

  def close(self) -> None:
    self._filings.clear()
    shutil.rmtree(self._tmp, ignore_errors=True)

  def _unique_id(self, wanted: str) -> str:
    if wanted not in self._filings:
      return wanted
    n = 2
    while f"{wanted}-{n}" in self._filings:
      n += 1
    return f"{wanted}-{n}"

  def _load(self, source: str) -> LoadedFiling:
    path = Path(source).expanduser()
    if path.exists():
      return self._load_local(path, source)
    if source.startswith(("http://", "https://")):
      return self._load_url(source)
    m = _CIK_ACCESSION_RE.match(source)
    if m:
      return self._load_edgar(m.group(1), m.group(2), source)
    m = _LEI_RE.match(source)
    if m:
      return self._load_filings_org(lei=m.group(1), source=source)
    m = _FXO_RE.match(source)
    if m:
      return self._load_filings_org(fxo_id=m.group(1), source=source)
    if _ACCESSION_RE.match(source):
      # The accession's prefix is the *filer agent's* CIK, which is the
      # company's own for self-filers; when it is not, EDGAR has no zip at
      # that path and the caller needs the cik:accession form.
      try:
        return self._load_edgar(source[:10], source, source)
      except FileNotFoundError as exc:
        raise SourceError(
          f"{source} was not filed under CIK {int(source[:10])}; "
          "pass it as `cik:accession`."
        ) from exc
    m = _TICKER_RE.match(source)
    if m:
      return self._load_ticker(m.group(1), m.group(2) or "10-K", source)
    raise SourceError(
      f"Cannot resolve {source!r}: give a local path, a URL, `cik:accession`, "
      "a ticker (optionally followed by a form, e.g. `NVDA 10-Q`), or — for a "
      "filing outside EDGAR — `lei:<LEI>` or a filings.xbrl.org filing id."
    )

  def _load_local(self, path: Path, source: str) -> LoadedFiling:
    package_dir: Path | None = None
    if path.is_file() and path.suffix.lower() in _JSON_SUFFIXES:
      return self._load_json(path, source)
    if path.is_dir():
      package_dir = path
      target = _find_load_target(path)
    elif path.suffix.lower() == ".zip":
      package_dir = Path(tempfile.mkdtemp(prefix="zip-", dir=self._tmp))
      with ZipFile(path) as archive:
        archive.extractall(package_dir)
      target = _find_load_target(package_dir)
    else:
      target = path
      package_dir = path.parent
    accession = _local_accession(path, target)
    # Arelle registers a taxonomy package from its archive or its manifest, and
    # the two are not interchangeable: one Dutch package's manifest raises
    # inside Arelle where the same package as a zip registers cleanly. The
    # archive is the better form whenever it is still to hand.
    packages: list[Path] | None = None
    if path.suffix.lower() == ".zip" and _taxonomy_packages(target):
      packages = [path]
    if target.suffix.lower() in _PLAIN_SUFFIXES:
      # Arelle cannot read plain text and never could: a filing from the 1990s
      # holds no markup at all. Going to it first only produces a parse error
      # where the answer is simply that this is a document.
      return self._document_only(target, source, accession, package_dir)
    try:
      model = self._parse(
        target, accession=accession, filing=None, entity=None, packages=packages
      )
    except NoXbrlFound:
      if target.suffix.lower() not in _DOCUMENT_SUFFIXES:
        raise
      return self._document_only(target, source, accession, package_dir)
    return self._finish(_local_id(path, model), source, model, target, package_dir)

  def _load_json(self, path: Path, source: str) -> LoadedFiling:
    """A JSON file, read into the model without Arelle.

    Three of the four shapes xbrlkit knows are read directly: the parse saved
    by ``export_filing model``, a TAVI compiled model, and a holon. Each is a
    representation of the report rather than a rendering of it, so the tools
    work over it unchanged — and none of them is something Arelle can load.
    The xBRL-JSON report is the one still refused: that one *is* Arelle's, and
    wants its OIM loader rather than an importer of our own.
    """
    text = path.read_text(encoding="utf-8", errors="replace")
    kind = json_kind(text[:4000])
    try:
      if kind == "tavi":
        model = from_tavi_json(text)
      elif kind == "holon":
        model = from_holon_json(text)
      elif kind == "model":
        model = XbrlModel.model_validate_json(text)
      else:
        raise SourceError(
          f"{path.name} is "
          f"{JSON_KIND_NAMES.get(kind, 'not a JSON file xbrlkit recognises')}"
          "; this server reads a saved parse, a TAVI compiled model, a holon, "
          "and XBRL packages."
        )
    except (TaviError, HolonError, ValueError) as exc:
      raise SourceError(f"{path} could not be read as {kind}: {exc}") from exc
    target: Path | None = None
    if model.filing.primary_document:
      candidate = path.parent / model.filing.primary_document
      if candidate.is_file():
        target = candidate
    model = _enrich_from_dei(model)
    served = kind in ("tavi", "holon")
    return self._finish(
      _local_id(path, model),
      source,
      model,
      target,
      path.parent,
      source_document=text if served else None,
      source_kind=kind if served else None,
    )

  def _load_url(self, url: str) -> LoadedFiling:
    clean = Path(url.split("?", 1)[0])
    accession = clean.stem or url
    suffix = clean.suffix.lower()
    if suffix in _JSON_SUFFIXES:
      # A JSON report is read here, not by Arelle, which cannot load one. This
      # is how a report published as an artifact — a holon or a TAVI on a CDN —
      # is opened by its URL rather than downloaded first.
      return self._load_json(self._fetch(url), url)
    try:
      model = self._parse(url, accession=accession, filing=None, entity=None)
    except NoXbrlFound:
      if suffix not in _DOCUMENT_SUFFIXES:
        raise
      return self._document_only(self._fetch(url), url, accession, None)
    target: Path | None = None
    if suffix in _INLINE_SUFFIXES:
      # Arelle keeps its own copy; fetch the document once more for the text.
      target = self._fetch(url)
    return self._finish(accession, url, model, target, None)

  def _fetch(self, url: str) -> Path:
    """The document at ``url``, saved beside this session's other work."""
    resp = requests.get(url, headers=self.config.headers, timeout=60)
    resp.raise_for_status()
    target = self._tmp / Path(url.split("?", 1)[0]).name
    target.write_bytes(resp.content)
    return target

  # -- the filing's other documents ------------------------------------------

  def other_documents(self, lf: LoadedFiling) -> list[FilingDocument]:
    """What else was filed with this one — listed once, then remembered.

    Costs one ~12 KB fetch of EDGAR's index page, and only when asked.
    """
    if lf.other_documents is not None:
      return lf.other_documents
    cik, accession = self._edgar_coordinates(lf)
    from xbrlkit.edgar import EdgarClient
    from xbrlkit.edgar.filing_index import fetch_filing_index
    from xbrlkit.edgar.filing_index import other_documents as select

    listed = fetch_filing_index(EdgarClient(config=self.config), cik, accession)
    lf.other_documents = select(listed, lf.model.filing.document_name or "")
    return lf.other_documents

  def read_other_document(self, lf: LoadedFiling, name: str) -> ReadDocument:
    """Fetch and read one of the filing's other documents, once."""
    wanted = (name or "").strip().lower()
    match = next(
      (d for d in self.other_documents(lf) if d.document.lower() == wanted), None
    )
    if match is None:
      names = [d.document for d in self.other_documents(lf)]
      raise SourceError(f"No document {name!r} in this filing; it has {names}")
    if not match.is_readable:
      raise SourceError(
        f"{match.document} is a {match.suffix or 'binary'} document, which this "
        f"server does not read. It is at {match.url} — fetch it there."
      )
    cached = lf.read_documents.get(match.document)
    if cached is not None:
      return cached
    dest = lf.package_dir or (self._tmp / lf.id)
    local = dest / match.document
    if local.is_file():
      # Split out of a complete submission at load; nothing to fetch.
      path = local
    else:
      cik, accession = self._edgar_coordinates(lf)
      from xbrlkit.edgar import EdgarClient, download_primary_document

      path = download_primary_document(
        EdgarClient(config=self.config), cik, accession, dest, match.document
      )
    # A bare model, deliberately: an exhibit is its own document, and carrying
    # the parent's form would have the narrative extractor hunt for a 10-K's
    # Items inside a certification.
    bare = XbrlModel(
      filing=FilingMeta(
        accession=lf.accession,
        cik=lf.model.filing.cik,
        document_name=match.document,
        form=match.type,
      ),
      entity=lf.model.entity,
    )
    read = _read_document(path, bare)
    if read is None:
      raise SourceError(
        f"{match.document} is a {match.suffix or 'binary'} document; "
        "this reads HTML, XML and plain text."
      )
    lf.read_documents[match.document] = read
    return read

  def _edgar_coordinates(self, lf: LoadedFiling) -> tuple[str, str]:
    """The CIK and accession needed to reach back to EDGAR for this filing."""
    cik, accession = lf.model.filing.cik, lf.accession
    if not cik or not _ACCESSION_RE.match(accession):
      raise SourceError(
        f"{lf.id} was not loaded from EDGAR, so its other documents cannot be "
        "listed. Load it as `cik:accession` or by ticker to reach them."
      )
    return cik, accession

  def _document_only(
    self, document: Path, source: str, accession: str, package_dir: Path | None
  ) -> LoadedFiling:
    """A document held on its own, with no EDGAR record to describe it.

    Identity comes from the document where it states it — an ownership form
    names its issuer, its form and its period — and is otherwise left unknown
    rather than guessed at.
    """
    filing = FilingMeta(accession=accession, cik="", document_name=document.name)
    entity = EntityIdentity(cik="")
    suffix = document.suffix.lower()
    if suffix == ".xml":
      try:
        _identify_from_xml(parse_xml_document(document.read_bytes()), filing, entity)
      except ElementTree.ParseError:
        pass
    elif suffix in _INLINE_SUFFIXES:
      filing.form = _form_on_the_cover(
        document.read_text(encoding="utf-8", errors="replace")
      )
    model = XbrlModel(filing=filing, entity=entity)
    return self._finish(accession, source, model, None, package_dir, document)

  def _load_edgar(self, cik: str, accession: str, source: str) -> LoadedFiling:
    """One EDGAR filing: its XBRL package, and its readable document.

    The ``-xbrl.zip`` holds XBRL and nothing else, so an inline filing arrives
    with its document (the instance *is* the document) while a classic one
    does not — its ``form10-k.htm`` is a sibling of the zip and is fetched
    separately. Without that second fetch every filing before iXBRL loads with
    no narrative at all: no Items, no MD&A, only the tagged blocks.
    """
    from xbrlkit.cli import entity_identity, filing_meta
    from xbrlkit.edgar import EdgarClient, download_filing, download_primary_document

    client = EdgarClient(config=self.config)
    ref = client.get_filing_ref(cik, accession)
    if not ref.is_xbrl:
      return self._load_document_only(client, cik, accession, ref, source)
    info = client.company_info(cik)
    package_dir = self._tmp / accession
    try:
      target = download_filing(client, cik, accession, package_dir)
    except FileNotFoundError:
      # EDGAR's record said XBRL and the package is not there: an unknown
      # accession assumed to have one, or a filing whose index disagrees.
      return self._load_document_only(client, cik, accession, ref, source)
    filing = filing_meta(self.config.sec_base_url, cik, accession, ref, target.name)
    document: Path | None = None
    if target.suffix.lower() in _INLINE_SUFFIXES:
      filing.document_name = target.name
    elif ref.primary_document:
      try:
        document = download_primary_document(
          client, cik, accession, package_dir, ref.primary_document
        )
        filing.document_name = document.name
      except (FileNotFoundError, requests.RequestException) as exc:
        # The filing still loads; its text tools fall back to the tagged
        # blocks, as they did before the document was fetched at all.
        logger.warning(
          "no primary document for %s (%s): %s", accession, ref.primary_document, exc
        )
    model = self._parse(target, accession, filing=filing, entity=entity_identity(info))
    return self._finish(accession, source, model, target, package_dir, document)

  def _load_document_only(
    self, client: Any, cik: str, accession: str, ref: Any, source: str
  ) -> LoadedFiling:
    """A filing EDGAR holds with no XBRL in it — most of EDGAR, by count.

    Every 8-K, proxy and registration statement, and every ownership form:
    there is no ``-xbrl.zip`` to fetch and nothing for Arelle to parse, so the
    document *is* the filing. The model is empty but real — it still carries
    who filed, which form, and when — and the text tools read the document.
    """
    from xbrlkit.cli import entity_identity, filing_meta
    from xbrlkit.edgar import download_primary_document

    name = raw_document_name(ref.primary_document)
    if not name:
      # Before about 2000 EDGAR wrote no separate files, so there is no
      # document to name: the filing is one SGML stream and its documents are
      # inside it. That is the whole of 1994-2000, and it is read by splitting.
      return self._load_from_submission(client, cik, accession, ref, source)
    if Path(name).suffix.lower() not in _DOCUMENT_SUFFIXES:
      from xbrlkit.edgar.download import primary_document_url

      where = primary_document_url(self.config.sec_base_url, cik, accession, name)
      raise SourceError(
        f"{accession} is a {Path(name).suffix or 'binary'} document ({name}), "
        f"which this server does not read. It is at {where} — fetch it there."
      )
    package_dir = self._tmp / accession
    document = download_primary_document(client, cik, accession, package_dir, name)
    filing = filing_meta(self.config.sec_base_url, cik, accession, ref, name)
    filing.document_name = name
    entity = entity_identity(client.company_info(cik))
    model = XbrlModel(filing=filing, entity=entity)
    return self._finish(accession, source, model, None, package_dir, document)

  def _load_from_submission(
    self, client: Any, cik: str, accession: str, ref: Any, source: str
  ) -> LoadedFiling:
    """A filing that exists only as its complete submission — EDGAR before 2000.

    The stream is fetched once, split, and written out as the files EDGAR never
    wrote. Sequence 1 becomes the filing's document; the rest are its other
    documents, already on disk, so listing them costs no further fetch.
    """
    from xbrlkit.cli import entity_identity, filing_meta
    from xbrlkit.edgar.submission import (
      complete_submission_url,
      parse_submission,
      strip_pem,
    )

    url = complete_submission_url(self.config.sec_base_url, cik, accession)
    raw = strip_pem(client._get(url).text)
    header, documents = parse_submission(raw)
    if not documents:
      raise SourceError(
        f"{accession} has no documents in its complete submission at {url}."
      )
    package_dir = self._tmp / accession
    package_dir.mkdir(parents=True, exist_ok=True)
    written: list[tuple[Any, Path]] = []
    for document in documents:
      target = package_dir / document.name
      target.write_text(document.text, encoding="utf-8")
      written.append((document, target))

    primary, primary_path = min(written, key=lambda pair: pair[0].sequence)
    filing = filing_meta(self.config.sec_base_url, cik, accession, ref, primary.name)
    filing.document_name = primary.name
    filing.form = filing.form or header.get("CONFORMED SUBMISSION TYPE") or None
    if filing.report_date is None:
      filing.report_date = _parse_edgar_date(header.get("CONFORMED PERIOD OF REPORT"))
    if filing.filing_date is None:
      filing.filing_date = _parse_edgar_date(header.get("FILED AS OF DATE"))
    model = XbrlModel(filing=filing, entity=entity_identity(client.company_info(cik)))
    loaded = self._finish(accession, source, model, None, package_dir, primary_path)
    # Every other document is already written; listing them needs no index page,
    # and they share one address because that is all EDGAR has for them.
    loaded.other_documents = [
      FilingDocument(
        seq=document.sequence,
        type=document.type,
        document=document.name,
        description=document.description,
        size=len(document.text),
        url=url,
      )
      for document, _ in written
      if document is not primary
    ]
    return loaded

  def _load_filings_org(
    self, source: str, lei: str = "", fxo_id: str = ""
  ) -> LoadedFiling:
    """A filing from XBRL International's index — ESEF and the national regimes.

    The package is downloaded and loaded exactly as a local one is: it carries
    the filer's own extension taxonomy, which is what lets a report that cites
    ``http://<the filer's domain>/...`` resolve at all. Identity comes from the
    index — the LEI and the filer's name — because these filings carry no CIK
    and no EDGAR record to ask.
    """
    from xbrlkit.filings_org import FilingsOrgClient, download_filing

    client = FilingsOrgClient(config=self.config)
    try:
      record = client.latest_filing(lei) if lei else client.filing(fxo_id)
    except LookupError as exc:
      raise SourceError(str(exc)) from exc
    if not record.has_package:
      logger.warning(
        "%s has no taxonomy package; its report must resolve %s's taxonomy "
        "over the network",
        record.fxo_id,
        record.country or "its regime",
      )
    package_dir = self._tmp / record.fxo_id
    archive = download_filing(client, record, package_dir)
    loaded = self._load_local(archive, source)
    loaded.id = record.fxo_id
    filing, entity = loaded.model.filing, loaded.model.entity
    filing.accession = filing.accession or record.fxo_id
    if record.period_end and filing.report_date is None:
      filing.report_date = _parse_iso_date(record.period_end)
    if record.entity:
      entity.name = entity.name or record.entity.name
      entity.legal_name = entity.legal_name or record.entity.name
    return loaded

  def _load_ticker(self, ticker: str, form: str, source: str) -> LoadedFiling:
    from xbrlkit.edgar import EdgarClient

    client = EdgarClient(config=self.config)
    cik = client.ticker_to_cik(ticker)
    refs = client.list_filings(cik, forms=[form.upper()])
    if not refs:
      raise SourceError(f"No {form.upper()} filings on EDGAR for {ticker.upper()}.")
    return self._load_edgar(cik, refs[0].accession, source)

  def _parse(
    self,
    target: Path | str,
    accession: str,
    filing: FilingMeta | None,
    entity: EntityIdentity | None,
    packages: list[Path] | None = None,
  ) -> XbrlModel:
    from xbrlkit.parse import close, load_model, to_xbrl_model

    with _ARELLE_LOCK:
      try:
        mx = load_model(
          target,
          cache_dir=self.config.arelle_cache_dir,
          offline=self.config.arelle_offline,
          timeout=self.config.arelle_timeout,
          packages=_taxonomy_packages(target) if packages is None else packages,
          config=self.config,
        )
      except RuntimeError as exc:
        raise SourceError(
          f"Arelle could not load {target}: not an XBRL or inline XBRL document "
          "it recognises (a TAVI, holon or OIM file needs its importer)."
        ) from exc
      try:
        if filing is None:
          filing = _filing_meta_from_instance(mx, target, accession)
        model = to_xbrl_model(mx, filing, entity=entity)
      finally:
        close(mx.modelManager.cntlr)
    if not model.facts and not model.concepts:
      # Arelle accepts any HTML as an empty document. That is not a failure —
      # an 8-K, a proxy, a Form 4 all read this way — but it is the caller's
      # call whether there is a document behind it worth holding.
      raise NoXbrlFound(
        f"{target} holds no XBRL facts or concepts: not an XBRL or inline XBRL "
        "document (a TAVI, holon or OIM file needs its importer)."
      )
    return _enrich_from_dei(model)

  def _finish(
    self,
    filing_id: str,
    source: str,
    model: XbrlModel,
    target: Path | None,
    package_dir: Path | None,
    document: Path | None = None,
    source_document: str | None = None,
    source_kind: str | None = None,
  ) -> LoadedFiling:
    """Assemble the :class:`LoadedFiling`.

    ``document`` is the filing's readable primary document when it is a
    *different* file from the Arelle load target — a classic filing's
    ``form10-k.htm`` beside its instance. When it is omitted the target is
    the document, as it is for inline XBRL.
    """
    block_text, block_sections = _text_from_text_blocks(model)
    doc = document if document is not None else target
    read = _read_document(doc, model)
    text, sections = (
      (read.text, read.sections) if read else (block_text, block_sections)
    )
    return LoadedFiling(
      id=filing_id,
      source=source,
      model=model,
      text=text,
      sections=sections,
      load_target=target,
      package_dir=package_dir,
      block_text=block_text,
      source_document=source_document,
      source_kind=source_kind,
      block_sections=block_sections,
      has_document=read is not None,
      xml_document=read.xml_document if read else None,
    )


# -- the readable document ------------------------------------------------------


@dataclass
class ReadDocument:
  """A filing's document, read into the text the tools search."""

  text: str
  sections: list[TextSection]
  xml_document: XmlDocument | None = None


def _read_document(doc: Path | None, model: XbrlModel) -> ReadDocument | None:
  """Read a filing's primary document, by what kind of document it is.

  Three lanes, because EDGAR is three kinds of document:

  * **HTML** — a 10-K, an 8-K, a proxy: prose, read as Items and (when the
    filing has XBRL) its tagged blocks located within them.
  * **XML** — the ownership forms, 13F, N-PORT: structure, read as fields and
    record tables and rendered to text so it can still be searched.
  * **plain text** — the oldest submissions, taken as they are.

  ``None`` when there is no document to read, or it is one this cannot read
  (a PDF), which leaves the filing on whatever text its XBRL carries.
  """
  if doc is None:
    return None
  suffix = doc.suffix.lower()
  if suffix in _INLINE_SUFFIXES:
    html = doc.read_text(encoding="utf-8", errors="replace")
    text, sections = build_text(model, html)
    return ReadDocument(text, sections)
  if suffix in _PLAIN_SUFFIXES:
    raw = doc.read_text(encoding="utf-8", errors="replace")
    text = _normalize_text(raw)
    return ReadDocument(text, _items_in(model, raw, text))
  if suffix == ".xml":
    # An XBRL instance is XML too; it is the load target, and its facts are
    # the reading. Only a document with no XBRL behind it is read this way.
    if model.facts or model.concepts:
      return None
    try:
      parsed = parse_xml_document(doc.read_bytes())
    except ElementTree.ParseError as exc:
      logger.warning("%s is not readable XML: %s", doc.name, exc)
      return None
    text = render_xml(parsed)
    sections = []
    for table in parsed.tables:
      header = f"## {table.name} ("
      at = text.find(header)
      sections.append(
        TextSection(
          id=table.name,
          label=f"{table.name} ({len(table.rows)} rows)",
          kind="records",
          chars=sum(len(v) for row in table.rows for v in row.values()),
          offset=at if at >= 0 else None,
          elements=table.columns,
        )
      )
    return ReadDocument(text, sections, parsed)
  return None


def build_text(model: XbrlModel, html: str | None) -> tuple[str, list[TextSection]]:
  """The filing as plain text plus its section map.

  With the document in hand the text is the whole document and the sections
  are its Items (10-K / 10-Q) and its tagged text blocks located in it.
  Without one the text is the tagged text blocks themselves, one after
  another under their concept names — every block a section with a known
  offset.

  Where the blocks come from depends on the era, because the document and
  the tagged blocks are one file only for inline XBRL:

  * **inline** — the blocks are ``ix:nonNumeric`` tags in this very document,
    so the iXBRL parser reads them out with the nested element qnames each
    one contains.
  * **classic** — the document carries no XBRL markup at all; the blocks live
    in a separate instance, as escaped HTML of these same paragraphs. They are
    taken from the model's facts and *located* in the document by matching
    their opening words, the way an Item is located. The two renderings agree
    on the prose and on nothing else, which is exactly what
    :func:`_locate` tolerates. Nested element qnames are not recoverable this
    way — the instance does not record which facts sat inside which block —
    so a classic block's ``elements`` is empty.
  """
  if html is None:
    return _text_from_text_blocks(model)

  text = _normalize_text(_html_to_text(html))
  sections: list[TextSection] = _items_in(model, html, text)
  inline_blocks = iXBRLParser(part_size=None).parse(html)
  for block in inline_blocks:
    sections.append(
      TextSection(
        id=block.section_id,
        label=block.section_label,
        kind="text_block",
        chars=len(block.content),
        offset=_locate(text, block.content),
        elements=block.xbrl_elements,
      )
    )
  if not inline_blocks:
    sections.extend(_blocks_located_in(model, text))
  sections.sort(key=lambda s: (s.offset is None, s.offset or 0))
  return text, sections


def _items_in(model: XbrlModel, source: str, text: str) -> list[TextSection]:
  """The form's Items, located in ``text``.

  ``source`` is what the extractor reads — the document's markup, or its plain
  text when that is all there is. A filing from the 1990s is plain text and
  still says "Item 1. Business"; the extractor's own HTML-to-text step passes
  such a document through, so one path serves both eras.
  """
  form = model.filing.form or ""
  if not form:
    return []
  return [
    TextSection(
      id=item.section_id,
      label=item.section_label,
      kind="item",
      chars=len(item.content),
      offset=_locate(text, item.content),
    )
    for item in NarrativeExtractor(part_size=None).extract(source, form)
  ]


def _blocks_located_in(model: XbrlModel, text: str) -> list[TextSection]:
  """The model's tagged text blocks, found in a document that does not mark
  them up — a classic filing's instance read against its own ``form10-k.htm``.

  Matching runs on the block's *prose* — its text before the first table row.
  A note's twelfth word is usually already inside its table, and the two
  renderings disagree most about tables: the instance's escaped HTML becomes
  markdown pipes while the document's becomes laid-out rows, putting a header
  row's worth of characters between two words that :func:`_locate` expects to
  find close together. Stopping at the table locates every block in the test
  filing where matching the whole body found 50 of 58, and it does so without
  widening the gap :func:`_locate` allows — the tolerance that keeps a table
  of contents from matching stays exactly as tight.
  """
  sections: list[TextSection] = []
  for fact, concept, body in _text_block_facts(model):
    head = _prose_head(body)
    offset = _locate(text, head)
    if offset is None and head is not body:
      offset = _locate(text, body)
    sections.append(
      TextSection(
        id=fact.concept_qname,
        label=concept.pref_label or concept.name,
        kind="text_block",
        chars=len(body),
        offset=offset,
      )
    )
  return sections


def _prose_head(body: str) -> str:
  """A block's text before its first markdown table row, when that leaves
  enough words to match on; the whole block when it is a table throughout."""
  cut = body.find("\n|")
  if cut <= 0:
    return body
  head = body[:cut]
  return head if len(_WORD_RE.findall(head)) >= 3 else body


def _text_block_facts(model: XbrlModel) -> list[tuple[XbrlFact, Concept, str]]:
  """Every tagged text block worth reading, as (fact, concept, plain text).

  A block under twenty words is a caption or an empty tag, not a disclosure.
  """
  found: list[tuple[XbrlFact, Concept, str]] = []
  for fact in model.facts:
    if fact.value_kind != "text" or not fact.value_str:
      continue
    concept = model.concepts.get(fact.concept_qname)
    if concept is None or not concept.is_textblock:
      continue
    body = _normalize_text(_strip_html(fact.value_str))
    if len(body.split()) < 20:
      continue
    found.append((fact, concept, body))
  return found


def _text_from_text_blocks(model: XbrlModel) -> tuple[str, list[TextSection]]:
  parts: list[str] = []
  sections: list[TextSection] = []
  offset = 0
  for fact, concept, body in _text_block_facts(model):
    header = f"## {fact.concept_qname}\n"
    chunk = header + body + "\n\n"
    sections.append(
      TextSection(
        id=fact.concept_qname,
        label=concept.pref_label or concept.name,
        kind="text_block",
        chars=len(body),
        offset=offset + len(header),
      )
    )
    parts.append(chunk)
    offset += len(chunk)
  return "".join(parts), sections


def _normalize_text(text: str) -> str:
  text = text.replace("\xa0", " ")
  text = re.sub(r"[ \t]+", " ", text)
  text = re.sub(r" *\n *", "\n", text)
  text = re.sub(r"\n{3,}", "\n\n", text)
  return text.strip()


_WORD_RE = re.compile(r"[A-Za-z]{4,}")


def _locate(text: str, content: str, words: int = 12, slack: int = 40) -> int | None:
  """Where ``content`` starts in ``text``.

  Matches the section's first few *words* (letters only, four or longer)
  in order, allowing a bounded run of anything between them, so the two
  renderings need not agree on line breaks, table pipes, numbers or how an
  apostrophe was stripped — while a table-of-contents row, which carries
  the heading but not what follows it, does not match — which is why twelve
  words, not the heading's eight, are required.
  """
  head = [re.escape(w) for w in _WORD_RE.findall(content)[:words]]
  if len(head) < 3:
    return None
  # The gap after a word may not contain that word again, so the match
  # starts at the last candidate before the second word — not at an earlier
  # heading that happens to share it.
  pattern = head[0] + "".join(
    rf"(?:(?!{prev}).){{0,{slack}}}?{nxt}" for prev, nxt in zip(head, head[1:])
  )
  m = re.search(pattern, text, re.DOTALL)
  return m.start() if m else None


# What each JSON xbrlkit recognises is called, for the one it cannot yet read.
JSON_KIND_NAMES = {
  "holon": "a holon (JSON-LD)",
  "tavi": "a TAVI compiled model",
  "oim": "an xBRL-JSON (OIM) report",
  "model": "a saved parse",
}


def json_kind(head: str) -> str:
  """Which JSON xbrlkit is looking at, from its first few kilobytes."""
  if '"@context"' in head or '"@graph"' in head:
    return "holon"
  if "/compiled" in head and '"documentInfo"' in head:
    return "tavi"
  if "xbrl-json" in head or "https://xbrl.org/2021" in head:
    return "oim"
  if '"filing"' in head and '"entity"' in head:
    return "model"
  return "unknown"


# -- filing identity without EDGAR ----------------------------------------------


_INSTANCE_ROOT_RE = re.compile(rb"<(?:[A-Za-z0-9_]+:)?xbrl[\s>]")
# An inline document declares the inline-XBRL namespace on its root element,
# whatever prefix it binds it to. Looking for `ix:` *elements* instead misses
# real reports: an ESEF filing opens with megabytes of stylesheet, and the
# first tagged fact in one sampled here sits 2.9 MB in.
_INLINE_NAMESPACES = (
  b"http://www.xbrl.org/2013/inlineXBRL",
  b"http://www.xbrl.org/2008/inlineXBRL",
)


def _taxonomy_packages(target: Path | str) -> list[Path]:
  """The taxonomy package a load target sits inside, if it sits in one.

  A conformant package marks itself with ``META-INF/taxonomyPackage.xml``, and
  that manifest is what gets registered: Arelle takes the package as a zip or
  as its manifest, but not as an unpacked directory, and by this point the zip
  has already been unpacked. Registering it lets the catalog remap the filer's
  own domain to the schema travelling beside the report.
  """
  path = Path(target)
  if not path.is_file():
    return []
  for parent in path.parents:
    manifest = parent / "META-INF" / "taxonomyPackage.xml"
    if manifest.is_file():
      return [manifest]
  return []


def _is_inline(head: bytes) -> bool:
  """Whether a document's opening bytes declare inline XBRL."""
  if any(namespace in head for namespace in _INLINE_NAMESPACES):
    return True
  return b"ix:nonNumeric" in head or b"ix:nonFraction" in head or b"ix:header" in head


def _find_load_target(package_dir: Path) -> Path:
  """The file Arelle should load from a filing directory: the inline
  document (the largest ``.htm`` carrying ``ix:`` markup), else the XBRL
  instance — recognised by its root element, since a package need not
  follow EDGAR's naming (an instance called ``instance.xml`` beside
  ``report.xsd`` and hyphenated linkbases is a valid package too).

  The whole tree is searched, not just the top level. A conformant XBRL
  taxonomy package puts nothing at its root: an ESEF report sits under
  ``reports/`` beside a ``META-INF/`` and the filer's own taxonomy in a
  directory named for their domain. Looking only at the top level found
  those packages empty, which is most of Europe.
  """
  inline: list[tuple[int, Path]] = []
  instances: list[Path] = []
  for candidate in sorted(package_dir.rglob("*")):
    if not candidate.is_file() or candidate.name.startswith("."):
      continue
    suffix = candidate.suffix.lower()
    if suffix not in _INLINE_SUFFIXES and suffix not in (".xml", ".xbrl"):
      continue
    with candidate.open("rb") as fh:
      head = fh.read(200_000)
    if suffix in _INLINE_SUFFIXES:
      if _is_inline(head):
        inline.append((candidate.stat().st_size, candidate))
    elif _INSTANCE_ROOT_RE.search(head[:4000]):
      instances.append(candidate)
  if inline:
    return max(inline)[1]
  if len(instances) == 1:
    return instances[0]
  if len(instances) > 1:
    # Several instances: prefer the one named after a schema, EDGAR-style.
    for schema in sorted(package_dir.rglob("*.xsd")):
      paired = schema.with_suffix(".xml")
      if paired in instances:
        return paired
    names = [p.name for p in instances]
    raise SourceError(
      f"{len(instances)} XBRL instances in {package_dir} ({names}); point at one."
    )
  raise SourceError(
    f"No inline document or XBRL instance found in {package_dir}; "
    "point at the file to load."
  )


_XML_IDENTITY = {
  "form": ("documentType", "submissionType"),
  "report_date": ("periodOfReport", "reportCalendarOrQuarter"),
}
_XML_ENTITY = {
  "cik": ("issuer.issuerCik", "issuerCik", "filer.filerCik"),
  "name": ("issuer.issuerName", "issuerName", "filer.filerName"),
  "ticker": ("issuer.issuerTradingSymbol", "issuerTradingSymbol"),
}


def _identify_from_xml(
  parsed: XmlDocument, filing: FilingMeta, entity: EntityIdentity
) -> None:
  """Fill in what an XML document says about itself: which form, whose, when."""

  def first(names: tuple[str, ...]) -> str | None:
    for name in names:
      value = parsed.fields.get(name)
      if value:
        return value
    return None

  filing.form = filing.form or first(_XML_IDENTITY["form"])
  reported = first(_XML_IDENTITY["report_date"])
  if reported and filing.report_date is None:
    filing.report_date = _parse_iso_date(reported)
  cik = first(_XML_ENTITY["cik"])
  if cik:
    entity.cik = f"{int(cik):0>10}" if cik.isdigit() else cik
    filing.cik = entity.cik
  entity.name = entity.name or first(_XML_ENTITY["name"])
  entity.ticker = entity.ticker or first(_XML_ENTITY["ticker"])


# A filing says which form it is on its cover page. Without EDGAR's record to
# ask — a document opened straight from disk — that is the only thing that
# tells the narrative extractor which Items to look for.
_COVER_FORM_RE = re.compile(
  r"\bFORM\s+(10-K|10-Q|20-F|40-F|8-K|S-1|S-3|DEF\s*14A)\b", re.IGNORECASE
)


def _form_on_the_cover(html: str) -> str | None:
  """The form a document names on its cover, from the top of the document."""
  m = _COVER_FORM_RE.search(_html_to_text(html[:400_000]))
  return re.sub(r"\s+", " ", m.group(1)).upper() if m else None


def _parse_edgar_date(value: str | None) -> date | None:
  """A header date, which EDGAR writes as ``YYYYMMDD``."""
  digits = (value or "").strip()
  if len(digits) != 8 or not digits.isdigit():
    return None
  try:
    return date(int(digits[:4]), int(digits[4:6]), int(digits[6:]))
  except ValueError:
    return None


def _parse_iso_date(value: str) -> date | None:
  try:
    return date.fromisoformat(value.strip()[:10])
  except ValueError:
    return None


def _local_accession(path: Path, target: Path) -> str:
  """An accession for a filing that did not come from EDGAR: the path's own
  name when it is one, else the loaded document's stem (``mmm-20241231``)."""
  stem = path.stem if path.is_file() else path.name
  return stem if _ACCESSION_RE.match(stem) else target.stem


def _local_id(path: Path, model: XbrlModel) -> str:
  """The id a local filing gets: its accession-shaped name when it has one,
  else the ticker, else the loaded document's stem."""
  stem = path.stem if path.is_file() else path.name
  if _ACCESSION_RE.match(stem):
    return stem
  if _ACCESSION_RE.match(model.filing.accession):
    return model.filing.accession
  if model.entity.ticker:
    return model.entity.ticker.lower()
  return Path(model.filing.primary_document or stem).stem


def _filing_meta_from_instance(
  mx: Any, target: Path | str, accession: str
) -> FilingMeta:
  """A :class:`FilingMeta` for a filing that did not come from EDGAR: the
  entity identifier from the instance's contexts, the form from ``dei``
  when the filing carries one, the document Arelle loaded as the report."""
  cik = ""
  for ctx in getattr(mx, "contexts", {}).values():
    try:
      _scheme, ident = ctx.entityIdentifier
    except Exception:  # pragma: no cover - defensive against odd contexts
      continue
    if ident:
      cik = str(ident)
      break
  form = None
  for fact in getattr(mx, "facts", ()):
    concept = getattr(fact, "concept", None)
    if concept is not None and str(concept.qname) == "dei:DocumentType":
      form = str(fact.value).strip() or None
      break
  target_path = Path(str(target).split("?", 1)[0])
  is_inline = target_path.suffix.lower() in _INLINE_SUFFIXES
  report_uri = (
    target
    if isinstance(target, str) and target.startswith("http")
    else target_path.resolve().as_uri()
  )
  return FilingMeta(
    accession=accession,
    cik=cik or accession,
    form=form,
    is_inline_xbrl=is_inline,
    primary_document=target_path.name,
    report_uri=str(report_uri),
  )


_DEI_ENTITY_FIELDS = {
  "dei:EntityRegistrantName": "name",
  "dei:TradingSymbol": "ticker",
  "dei:SecurityExchangeName": "exchange",
  "dei:EntityTaxIdentificationNumber": "ein",
  "dei:EntityIncorporationStateCountryCode": "state_of_incorporation",
}


def _enrich_from_dei(model: XbrlModel) -> XbrlModel:
  """Fill the entity's name, ticker and the filing's form / period end from
  the cover page ``dei`` facts when the EDGAR header did not supply them."""
  entity_updates: dict[str, Any] = {}
  filing_updates: dict[str, Any] = {}
  # A cover page that lists several securities tags the symbol per class
  # (with a dimension); the undimensioned fact wins, the first class stands
  # in when there is none.
  for fact in sorted(model.facts, key=lambda f: bool(f.dims)):
    if not fact.concept_qname.startswith("dei:"):
      continue
    value = (fact.value_str or "").strip()
    if not value:
      continue
    field_name = _DEI_ENTITY_FIELDS.get(fact.concept_qname)
    if field_name and getattr(model.entity, field_name) in (None, ""):
      entity_updates.setdefault(field_name, value)
    elif fact.dims:
      continue
    elif fact.concept_qname == "dei:DocumentType" and not model.filing.form:
      filing_updates.setdefault("form", value)
    elif (
      fact.concept_qname == "dei:DocumentPeriodEndDate" and not model.filing.report_date
    ):
      filing_updates.setdefault("report_date", _date_or_none(value))
  if not entity_updates and not filing_updates:
    return model
  return model.model_copy(
    update={
      "entity": model.entity.model_copy(update=entity_updates),
      "filing": model.filing.model_copy(update=filing_updates),
    }
  )


def _date_or_none(value: str) -> Any:
  from datetime import date

  try:
    return date.fromisoformat(value[:10])
  except ValueError:
    return None


def text_block_facts(model: XbrlModel) -> list[XbrlFact]:
  """The facts whose concept is a text block, in document order."""
  return [
    f
    for f in model.facts
    if f.value_kind == "text"
    and (c := model.concepts.get(f.concept_qname)) is not None
    and c.is_textblock
  ]
