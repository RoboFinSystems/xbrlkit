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
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from collections.abc import Mapping
from typing import Any, Literal
from zipfile import ZipFile

import requests
from xml.etree import ElementTree

from xbrlkit.config import CONFIG, Config
from xbrlkit.deserialize import (
  ClawDogError,
  HolonError,
  TaviError,
  from_clawdog_json,
  from_holon_json,
  from_tavi_json,
)
from xbrlkit.edgar.filing_index import FilingDocument
from xbrlkit.model import Concept, EntityIdentity, FilingMeta, XbrlFact, XbrlModel
from xbrlkit.text.ixbrl import _strip_html, iXBRLParser
from xbrlkit.text.narrative import (
  NarrativeExtractor,
  _html_to_text,
  _is_toc_row,
  _line_of,
)
from xbrlkit.text.xml import XmlDocument, parse_xml_document, raw_document_name
from xbrlkit.text.xml import render as render_xml

SectionKind = Literal["item", "text_block", "records"]

_INLINE_SUFFIXES = {".htm", ".html", ".xhtml"}
_PLAIN_SUFFIXES = {".txt", ".md"}
# A report serialized as JSON — read into the model here, never by Arelle.
_JSON_SUFFIXES = {".json", ".jsonld"}
# The published folder is keyed by filing year, ten-digit CIK and accession; the
# accession's middle segment is the year it was assigned.
_ACCESSION_YEAR_RE = re.compile(r"^\d{10}-(\d{2})-\d{6}$")
_EXTERNAL_TEXT_WORKERS = 8
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
  # Characters of heading standing immediately before ``offset``, when this
  # server assembled the text rather than parsing it from a document.
  # ``offset`` stays the body's own start — what a reader pages from — so
  # anything counting where matches fall reaches back over this instead, and
  # no character of an assembled reading belongs to no section.
  heading_chars: int = 0


@dataclass
class EntryPoint:
  """One way into a taxonomy: the schema its DTS is discovered from.

  ``document`` is the schema's path inside the package, as a caller names it
  back through ``entry_point``; ``path`` is where it sits on disk.
  """

  name: str
  document: str
  path: Path


@dataclass
class TaxonomyEntry:
  """The entry point a taxonomy package with no report was loaded from, and
  the ones it offers that were not."""

  entry_point: EntryPoint
  others: list[EntryPoint] = field(default_factory=list)


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
  # Set when the source was a taxonomy published on its own — a package with
  # schemas and linkbases and no report — and so holds concepts and networks
  # but no facts.
  taxonomy: TaxonomyEntry | None = None

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


# The identity fields a filing does not establish on its own. The cover page
# tags most of them (see ``_DEI_ENTITY_FIELDS``); ``sic`` it never tags, which
# is why a lookup outside the filing is worth one small fetch.
_FILER_FIELDS = (
  "name",
  "ein",
  "ticker",
  "exchange",
  "sic",
  "sic_description",
  "category",
  "state_of_incorporation",
  "fiscal_year_end",
  "entity_type",
  "website",
  "phone",
)


def _padded_cik(cik: str) -> str:
  """A CIK as EDGAR writes it, left-padded to ten digits."""
  return f"{int(cik):0>10}" if cik.strip().isdigit() else cik


def _text_or_none(value: Any) -> str | None:
  """A header or catalog value as a non-empty string, else ``None``."""
  if value is None:
    return None
  text = str(value).strip()
  return text or None


def _filer_fields(source: Mapping[str, Any]) -> dict[str, Any]:
  """The filer fields a CDN catalog carries, under the names the model uses.

  Keyed by field name rather than by an explicit mapping, so a catalog that
  grows a field the model already has is picked up without a code change.
  """
  return {
    name: value
    for name in _FILER_FIELDS
    if (value := _text_or_none(source.get(name))) is not None
  }


def _enrich_filer(loaded: LoadedFiling, fields: Mapping[str, Any]) -> None:
  """Fill the entity's empty identity fields from outside the filing.

  Fill-empty, never overwrite: what the filing said about itself on its cover
  page stands, and this supplies only what the filing does not carry.
  """
  entity = loaded.model.entity
  for name, value in fields.items():
    if value in (None, "") or getattr(entity, name, None) not in (None, ""):
      continue
    setattr(entity, name, value)
  if entity.name and not entity.legal_name:
    entity.legal_name = entity.name


@dataclass
class PublishedFiling:
  """A filing as the public data CDN lists it: the representations to load
  (the TAVI model first, the holon beside it), the document as filed when it
  was published too, and the filer's ticker — the key the catalog holds that
  filer's identity under."""

  accession: str
  holon_url: str | None = None
  document_url: str | None = None
  ticker: str | None = None
  tavi_url: str | None = None

  @property
  def model_urls(self) -> list[str]:
    """The representations to try, in order: the TAVI model, then the holon."""
    return [url for url in (self.tavi_url, self.holon_url) if url]


def _published_from(
  accession: str | None,
  representations: Any,
  folder: str | None,
  ticker: str | None = None,
) -> PublishedFiling | None:
  """The published filing a catalog entry or manifest describes, or ``None``
  when it lists neither a TAVI model nor a holon."""
  holon = document = tavi = None
  for rep in representations if isinstance(representations, list) else []:
    if not isinstance(rep, dict):
      continue
    url = rep.get("url") or (
      f"{folder.rstrip('/')}/{rep['name']}" if folder and rep.get("name") else None
    )
    if not url:
      continue
    if rep.get("kind") == "holon":
      holon = url
    elif rep.get("kind") == "tavi":
      tavi = url
    elif rep.get("kind") == "document":
      document = url
  if not (holon or tavi):
    return None
  return PublishedFiling(
    accession=accession or "",
    holon_url=holon,
    document_url=document,
    ticker=ticker,
    tavi_url=tavi,
  )


class FilingSession:
  """The filings loaded into one server process, by id."""

  def __init__(self, config: Config = CONFIG) -> None:
    self.config = config
    self._filings: dict[str, LoadedFiling] = {}
    self._filers: dict[str, dict[str, Any]] = {}
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

  def load(
    self, source: str, filing_id: str | None = None, entry_point: str | None = None
  ) -> LoadedFiling:
    """Resolve ``source`` and load it; returns the new :class:`LoadedFiling`.

    ``entry_point`` names the schema to load from a taxonomy package — a zip
    or directory, local or by URL — in place of the one chosen by default.
    """
    source = source.strip()
    if not source:
      raise SourceError("An empty source.")
    entry_point = (entry_point or "").strip() or None
    with self._lock:
      loaded = self._load(source, entry_point)
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

  def _load(self, source: str, entry_point: str | None = None) -> LoadedFiling:
    # A URL first: a presigned link runs past the file-name limit, and
    # probing it as a path raises.
    if source.startswith(("http://", "https://")):
      return self._load_url(source, entry_point)
    path = Path(source).expanduser()
    if _is_local(path):
      return self._load_local(path, source, entry_point)
    if entry_point:
      raise SourceError(_ENTRY_POINT_NEEDS_A_PACKAGE.format(source=source))
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

  def _load_local(
    self, path: Path, source: str, entry_point: str | None = None
  ) -> LoadedFiling:
    package_dir: Path | None = None
    taxonomy: TaxonomyEntry | None = None
    is_package = path.is_dir() or path.suffix.lower() == ".zip"
    if entry_point and not is_package:
      raise SourceError(_ENTRY_POINT_NEEDS_A_PACKAGE.format(source=source))
    if path.is_file() and path.suffix.lower() in _JSON_SUFFIXES:
      return self._load_json(path, source)
    if is_package:
      if path.is_dir():
        package_dir = path
      else:
        package_dir = Path(tempfile.mkdtemp(prefix="zip-", dir=self._tmp))
        with ZipFile(path) as archive:
          archive.extractall(package_dir)
      report = None if entry_point else _find_load_target(package_dir)
      if report is None:
        # No report in the package: a taxonomy published on its own, which
        # loads from one of its schemas and answers with concepts and networks.
        taxonomy = _choose_entry_point(package_dir, entry_point)
        target = taxonomy.entry_point.path
      else:
        target = report
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
    except NoXbrlFound as exc:
      if taxonomy is not None:
        # The model holds the concepts its networks and facts reach, so an
        # elements-only schema — no linkbases, nothing to reach them — reads
        # as empty. Say so, rather than that the package is not XBRL.
        others = ", ".join(e.document for e in taxonomy.others)
        raise SourceError(
          f"Entry point {taxonomy.entry_point.document} declares no networks "
          "for the tools to read (an elements-only schema); load an entry "
          f"point with linkbases instead{': ' + others if others else ''}."
        ) from exc
      if target.suffix.lower() not in _DOCUMENT_SUFFIXES:
        raise
      return self._document_only(target, source, accession, package_dir)
    loaded = self._finish(_local_id(path, model), source, model, target, package_dir)
    loaded.taxonomy = taxonomy
    return loaded

  def _load_json(
    self, path: Path, source: str, document: Path | None = None
  ) -> LoadedFiling:
    """A JSON file, read into the model without Arelle.

    Four of the JSON shapes xbrlkit knows are read directly: the parse saved
    by ``export_filing model``, a TAVI compiled model, a holon, and a ClawDog
    report. Each is a representation of the report rather than a rendering of
    it, so the tools work over it unchanged — and none of them is something
    Arelle can load.
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
      elif kind == "clawdog":
        model = from_clawdog_json(text)
      elif kind == "model":
        model = XbrlModel.model_validate_json(text)
      else:
        raise SourceError(
          f"{path.name} is "
          f"{JSON_KIND_NAMES.get(kind, 'not a JSON file xbrlkit recognises')}"
          "; this server reads a saved parse, a TAVI compiled model, a holon, "
          "a ClawDog report, and XBRL packages."
        )
    except (ClawDogError, TaviError, HolonError, ValueError) as exc:
      raise SourceError(f"{path} could not be read as {kind}: {exc}") from exc
    target: Path | None = None
    if document is not None:
      target = document
      model.filing.document_name = document.name
    elif model.filing.primary_document:
      candidate = path.parent / model.filing.primary_document
      if candidate.is_file():
        target = candidate
    model = _enrich_from_dei(model)
    inlined = self._inline_external_text(model)
    if inlined:
      logger.info("inlined %d text block fragments for %s", inlined, source)
    served = kind in ("tavi", "holon", "clawdog")
    return self._finish(
      _local_id(path, model),
      source,
      model,
      target,
      path.parent,
      source_document=text if served else None,
      source_kind=kind if served else None,
    )

  def _load_url(self, url: str, entry_point: str | None = None) -> LoadedFiling:
    clean = Path(url.split("?", 1)[0])
    accession = clean.stem or url
    suffix = clean.suffix.lower()
    if suffix == ".zip":
      # A package by URL is downloaded and loaded as a local one: the report
      # found inside it, or — for a taxonomy published on its own, which is
      # how FASB, XBRL US and the IFRS Foundation distribute theirs — an
      # entry point.
      archive = self._fetch(
        url, into=Path(tempfile.mkdtemp(prefix="url-", dir=self._tmp))
      )
      return self._load_local(archive, url, entry_point)
    if entry_point:
      raise SourceError(_ENTRY_POINT_NEEDS_A_PACKAGE.format(source=url))
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

  def _fetch(self, url: str, into: Path | None = None) -> Path:
    """The document at ``url``, saved beside this session's other work — under
    ``into`` when two filings would otherwise share a file name."""
    bare = url.split("?", 1)[0]
    try:
      resp = requests.get(url, headers=self.config.headers, timeout=60)
      resp.raise_for_status()
    except requests.HTTPError as exc:
      # The query is left out: on a presigned link it is the credential.
      raise SourceError(
        f"Fetching {bare} failed: {exc.response.status_code} "
        f"{exc.response.reason}. A signed link may have expired."
      ) from None
    except requests.RequestException as exc:
      raise SourceError(f"Fetching {bare} failed: {type(exc).__name__}.") from None
    folder = into or self._tmp
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / Path(bare).name
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
    from xbrlkit.cli import filing_meta
    from xbrlkit.edgar import EdgarClient, download_filing, download_primary_document

    published = self._published_by_accession(cik, accession)
    if published is not None:
      return self._load_published(published, source)

    client = EdgarClient(config=self.config)
    ref = client.get_filing_ref(cik, accession)
    if not ref.is_xbrl:
      return self._load_document_only(client, cik, accession, ref, source)
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
    # The cover page fills the entity first — ``_parse`` ends in the dei pass —
    # and the submissions header backfills only what the filing does not carry.
    model = self._parse(target, accession, filing=filing, entity=None)
    loaded = self._finish(accession, source, model, target, package_dir, document)
    _enrich_filer(loaded, self._filer_metadata(cik, client=client))
    return loaded

  def _load_document_only(
    self, client: Any, cik: str, accession: str, ref: Any, source: str
  ) -> LoadedFiling:
    """A filing EDGAR holds with no XBRL in it — most of EDGAR, by count.

    Every 8-K, proxy and registration statement, and every ownership form:
    there is no ``-xbrl.zip`` to fetch and nothing for Arelle to parse, so the
    document *is* the filing. The model is empty but real — it still carries
    who filed, which form, and when — and the text tools read the document.
    """
    from xbrlkit.cli import filing_meta
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
    model = XbrlModel(filing=filing, entity=EntityIdentity(cik=_padded_cik(cik)))
    loaded = self._finish(accession, source, model, None, package_dir, document)
    # No XBRL, so no cover page to read: the submissions header is the only
    # account of the filer this filing has.
    _enrich_filer(loaded, self._filer_metadata(cik, client=client))
    return loaded

  def _load_from_submission(
    self, client: Any, cik: str, accession: str, ref: Any, source: str
  ) -> LoadedFiling:
    """A filing that exists only as its complete submission — EDGAR before 2000.

    The stream is fetched once, split, and written out as the files EDGAR never
    wrote. Sequence 1 becomes the filing's document; the rest are its other
    documents, already on disk, so listing them costs no further fetch.
    """
    from xbrlkit.cli import filing_meta
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
    model = XbrlModel(filing=filing, entity=EntityIdentity(cik=_padded_cik(cik)))
    loaded = self._finish(accession, source, model, None, package_dir, primary_path)
    _enrich_filer(loaded, self._filer_metadata(cik, client=client))
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

    published = self._published_by_ticker(ticker, form)
    if published is not None:
      return self._load_published(published, source)

    client = EdgarClient(config=self.config)
    cik = client.ticker_to_cik(ticker)
    refs = client.list_filings(cik, forms=[form.upper()])
    if not refs:
      raise SourceError(f"No {form.upper()} filings on EDGAR for {ticker.upper()}.")
    return self._load_edgar(cik, refs[0].accession, source)

  # -- the published representations ------------------------------------------

  def _published_by_ticker(self, ticker: str, form: str) -> PublishedFiling | None:
    """The filer's newest filing of ``form`` on the public catalog, when that
    filing has a published holon.

    The newest filing decides: one that predates the artifacts has no holon,
    and the answer is then EDGAR, never an older filing that happens to have
    one.
    """
    base = self.config.artifacts_base_url
    if not base:
      return None
    catalog = self._get_json(f"{base}/companies/{ticker.lower()}.json")
    if not isinstance(catalog, dict):
      return None
    wanted = form.upper()
    for filing in catalog.get("filings") or []:
      if not isinstance(filing, dict) or (filing.get("form") or "").upper() != wanted:
        continue
      return _published_from(
        filing.get("accession"),
        filing.get("representations"),
        filing.get("folder"),
        ticker=ticker,
      )
    return None

  def _published_by_accession(self, cik: str, accession: str) -> PublishedFiling | None:
    """The filing's published folder, probed by its manifest."""
    base = self.config.artifacts_base_url
    match = _ACCESSION_YEAR_RE.match(accession)
    if not base or not match:
      return None
    folder = f"{base}/20{match.group(1)}/{cik.zfill(10)}/{accession}"
    manifest = self._get_json(f"{folder}/manifest.json")
    if not isinstance(manifest, dict):
      return None
    entity = manifest.get("entity")
    return _published_from(
      accession,
      manifest.get("representations"),
      folder,
      ticker=_text_or_none(entity.get("ticker")) if isinstance(entity, dict) else None,
    )

  def _get_json(self, url: str) -> Any:
    """A small JSON object from the CDN, or ``None`` for anything but a clean
    200 — a missing object answers 403 there, and either way the fallback is
    EDGAR."""
    try:
      resp = requests.get(url, headers=self.config.headers, timeout=10)
      if resp.status_code != 200:
        return None
      return resp.json()
    except (requests.RequestException, ValueError):
      return None

  def _filer_metadata(
    self, cik: str, ticker: str | None = None, client: Any = None
  ) -> dict[str, Any]:
    """The filer's identity as recorded outside the filing.

    ``sic`` above all: no cover page tags it, EDGAR assigns it, and without
    this lookup a filing read from its published holon reports none.

    Cached per CIK — one lookup per filer per session, not one per filing, so
    a sweep of a filer's twenty filings costs a single fetch. The public
    catalog answers first when the filing came from the CDN, which keeps a
    published load off sec.gov entirely; the submissions header answers for
    everything else, and backfills what a catalog does not carry.

    Best effort throughout: a filing loads without any of this, so a refusal,
    a timeout or a rate limit leaves the fields unfilled rather than failing
    the load. A failure is cached too — a filer EDGAR would not answer for is
    not asked about again for every filing in the session.
    """
    key = _padded_cik(cik)
    if key in self._filers:
      return self._filers[key]
    found = self._filer_from_catalog(ticker)
    if not found.get("sic"):
      # The catalog wins where both carry a field: it is what the CDN
      # published beside the holon, and the header is only current.
      found = {**self._filer_from_edgar(key, client), **found}
    self._filers[key] = found
    return found

  def _filer_from_catalog(self, ticker: str | None) -> dict[str, Any]:
    """The filer as the public catalog records it, or ``{}``."""
    base = self.config.artifacts_base_url
    if not ticker or not base:
      return {}
    catalog = self._get_json(f"{base}/companies/{ticker.lower()}.json")
    return _filer_fields(catalog) if isinstance(catalog, dict) else {}

  def _filer_from_edgar(self, cik: str, client: Any = None) -> dict[str, Any]:
    """The filer as EDGAR's submissions header records it, or ``{}``."""
    from xbrlkit.edgar import EdgarClient

    try:
      info = (client or EdgarClient(config=self.config)).company_info(cik)
    except (OSError, ValueError, LookupError) as exc:
      # A refusal, a timeout, a rate limit, a filer EDGAR has no header for:
      # the filing itself is already loaded, so the fields stay unfilled.
      # Deliberately not a blanket ``except``, which would hide a defect here
      # behind a filing that loads and quietly reports no SIC.
      logger.warning("filer metadata unavailable for CIK %s: %s", cik, exc)
      return {}
    return {
      name: value
      for name in _FILER_FIELDS
      if (value := _text_or_none(getattr(info, name, None))) is not None
    }

  def _load_published(self, published: PublishedFiling, source: str) -> LoadedFiling:
    """The filing from its published TAVI model (or its holon when there is no
    TAVI, or it cannot be read), with the document as filed beside it when the
    CDN has that too — the same shape an EDGAR load gives, in a fraction of the
    time and with no Arelle. The TAVI carries its text blocks inline, so there
    are no fragments to fetch after it."""
    urls = published.model_urls
    into = self._tmp / (published.accession or Path(urls[0].split("?", 1)[0]).stem)
    document: Path | None = None
    if published.document_url:
      try:
        document = self._fetch(published.document_url, into=into)
      except requests.RequestException as exc:
        logger.warning("published document unavailable for %s: %s", source, exc)
    loaded: LoadedFiling | None = None
    for position, url in enumerate(urls):
      try:
        path = self._fetch(url, into=into)
        logger.info("loading %s from %s", source, Path(url.split("?", 1)[0]).name)
        loaded = self._load_json(path, source, document=document)
        break
      except (requests.RequestException, SourceError) as exc:
        if position == len(urls) - 1:
          raise
        logger.warning("%s unavailable for %s (%s); trying the next", url, source, exc)
    assert loaded is not None
    _enrich_filer(
      loaded, self._filer_metadata(loaded.model.entity.cik, ticker=published.ticker)
    )
    return loaded

  def _inline_external_text(self, model: XbrlModel) -> int:
    """Replace a text block's fragment URL with the fragment.

    The published holon carries a large text block as the URL of the fragment
    the platform stored beside it. The text tools want the text, so the
    fragments are fetched on load, in parallel; one that cannot be fetched
    stays a URL rather than failing the load.
    """
    if not self.config.fetch_external_text:
      return 0
    pending: list[XbrlFact] = []
    for fact in model.facts:
      if fact.value_kind != "text" or not (fact.value_str or "").startswith(
        ("http://", "https://")
      ):
        continue
      concept = model.concepts.get(fact.concept_qname)
      if concept is not None and concept.is_textblock:
        pending.append(fact)
    if not pending:
      return 0

    def fetch(url: str) -> str | None:
      try:
        resp = requests.get(
          url, headers=self.config.headers, timeout=self.config.request_timeout
        )
        resp.raise_for_status()
        return resp.text
      except requests.RequestException as exc:
        logger.warning("text block fragment unavailable: %s (%s)", url, exc)
        return None

    with ThreadPoolExecutor(max_workers=_EXTERNAL_TEXT_WORKERS) as pool:
      bodies = list(pool.map(fetch, [fact.value_str or "" for fact in pending]))
    inlined = 0
    for fact, body in zip(pending, bodies, strict=True):
      if body is not None:
        fact.value_str = body
        fact.raw_value = body
        inlined += 1
    return inlined

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
        heading_chars=len(header),
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

# Words deep enough into a section that no index row reaches them: a table of
# contents renders the heading and then moves to the next entry, whatever
# shape it is drawn in — a pipe table, or Oracle's bare run of lines.
_DEEP_LOCATE_WORDS = 30

# "Item 7." or "Item 7A." at the head of a line — the shape a contents entry
# and a section heading share, which is why what follows it is what tells
# them apart.
_ITEM_HEADING_RE = re.compile(r"Item\s+\d+[A-Z]?[\.\s—–:]", re.IGNORECASE)


def _heads_a_contents_entry(text: str, pos: int) -> bool:
  """Whether the heading at ``pos`` is an entry in a table of contents.

  A contents entry is followed by the next entry; a section heading is
  followed by the section. That holds however the contents is drawn — a pipe
  table with page cells, or the bare run of lines Oracle and Procter & Gamble
  file — where the row test only sees the first.
  """
  if _is_toc_row(_line_of(text, pos)):
    return True
  line_end = text.find("\n", pos)
  if line_end == -1:
    return False
  following = [
    line for line in text[line_end : line_end + 400].split("\n") if line.strip()
  ]
  return any(_ITEM_HEADING_RE.match(line.lstrip("| ")) for line in following[:2])


def _locate(text: str, content: str, words: int = 12, slack: int = 40) -> int | None:
  """Where ``content`` starts in ``text``.

  Matches the section's first few *words* (letters only, four or longer)
  in order, allowing a bounded run of anything between them, so the two
  renderings need not agree on line breaks, table pipes, numbers or how an
  apostrophe was stripped — while a table-of-contents row, which carries
  the heading but not what follows it, does not match — which is why twelve
  words, not the heading's eight, are required.

  Twelve is not enough on its own. A heading that is twelve words by itself
  is carried whole by its index row, and Item 5's is: "Market for
  Registrant's Common Equity, Related Stockholder Matters and Issuer
  Purchases of Equity Securities". So the section is matched thirty words
  deep first, past anything an index row carries, and an index row is
  rejected outright in the shorter fallback — and a section found nowhere
  else reports no offset rather than the address of the table of contents.
  """
  found = _WORD_RE.findall(content)
  if len(found) < 3:
    return None
  # Deep first: an index row carries the heading and then the next entry, so
  # words this far into the section are the surest sign of the body itself.
  # The short head is the fallback, for the renderings that disagree too
  # much to carry a long match — with index rows rejected outright, since
  # those are exactly what the short head cannot tell apart.
  for probe in (_DEEP_LOCATE_WORDS, words):
    head = [re.escape(w) for w in found[:probe]]
    if len(head) < 3:
      continue
    deep = probe == _DEEP_LOCATE_WORDS
    # The gap after a word may not contain that word again, so the match
    # starts at the last candidate before the second word — not at an earlier
    # heading that happens to share it.
    pattern = head[0] + "".join(
      rf"(?:(?!{prev}).){{0,{slack}}}?{nxt}" for prev, nxt in zip(head, head[1:])
    )
    for m in re.finditer(pattern, text, re.DOTALL):
      if deep or not _heads_a_contents_entry(text, m.start()):
        return m.start()
  return None


# What each JSON xbrlkit recognises is called, for the one it cannot yet read.
JSON_KIND_NAMES = {
  "holon": "a holon (JSON-LD)",
  "tavi": "a TAVI compiled model",
  "clawdog": "a ClawDog report",
  "oim": "an xBRL-JSON (OIM) report",
  "model": "a saved parse",
}


def json_kind(head: str) -> str:
  """Which JSON xbrlkit is looking at, from its first few kilobytes."""
  if "ns/clawdog/report" in head or '"lg:Report"' in head:
    return "clawdog"
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


def _find_load_target(package_dir: Path) -> Path | None:
  """The report Arelle should load from a filing directory: the inline
  document (the largest ``.htm`` carrying ``ix:`` markup), else the XBRL
  instance — recognised by its root element, since a package need not
  follow EDGAR's naming (an instance called ``instance.xml`` beside
  ``report.xsd`` and hyphenated linkbases is a valid package too). ``None``
  when the package holds no report: a taxonomy published on its own, which
  :func:`_choose_entry_point` picks a schema from.

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
  return None


_ENTRY_POINT_NEEDS_A_PACKAGE = (
  "entry_point picks a schema inside a taxonomy package — a .zip or a "
  "directory, local or by URL; {source} is not one."
)
_SCHEMA_LOCATION_RE = re.compile(rb"""schemaLocation\s*=\s*(["'])(.*?)\1""", re.DOTALL)


def _local_name(tag: str) -> str:
  return tag.rsplit("}", 1)[-1]


def _choose_entry_point(package_dir: Path, wanted: str | None) -> TaxonomyEntry:
  """The schema to load a taxonomy package from, when it holds no report.

  A package that declares its entry points (``META-INF/taxonomyPackage.xml``)
  loads the one ``wanted`` names, else the first it lists: FASB lists the whole
  taxonomy first — its US GAAP and SRT packages open with
  ``entire/…-entryPoint-all``, ahead of the elements-only, DQC and meta-model
  entry points. A package without a manifest loads its one root
  schema, the one no other schema in it imports; with several, the caller
  names one. The others are returned beside the choice so it is never silent.
  """
  # Resolved once, so a package unpacked under a symlinked temp directory
  # (macOS's /var) still contains the resolved paths its entry points name.
  package_dir = package_dir.resolve()
  declared = _declared_entry_points(package_dir)
  entry_points = declared or _root_schemas(package_dir)
  if not entry_points:
    raise SourceError(
      f"No inline document, XBRL instance or taxonomy schema found in "
      f"{package_dir}; point at the file to load."
    )
  if wanted:
    chosen = _match_entry_point(entry_points, wanted)
  elif declared or len(entry_points) == 1:
    chosen = entry_points[0]
  else:
    raise SourceError(
      f"This taxonomy package has no manifest and {len(entry_points)} root "
      f"schemas; pass entry_point to choose one: "
      f"{', '.join(e.document for e in entry_points)}."
    )
  return TaxonomyEntry(
    entry_point=chosen, others=[e for e in entry_points if e is not chosen]
  )


def _match_entry_point(entry_points: list[EntryPoint], wanted: str) -> EntryPoint:
  """The entry point ``wanted`` names: exactly, by its name, its path in the
  package or its file name with or without ``.xsd``; else the one it is part
  of."""
  key = wanted.strip().lower()

  def names(e: EntryPoint) -> set[str]:
    document = Path(e.document)
    return {
      e.name.lower(),
      e.document.lower(),
      document.name.lower(),
      document.stem.lower(),
    }

  hits = [e for e in entry_points if key in names(e)]
  if not hits:
    hits = [e for e in entry_points if any(key in n for n in names(e))]
  if len(hits) == 1:
    return hits[0]
  listed = ", ".join(e.document for e in hits or entry_points)
  if hits:
    raise SourceError(f"entry_point {wanted!r} matches {len(hits)}: {listed}.")
  raise SourceError(f"No entry point matches {wanted!r}; this package has: {listed}.")


def _declared_entry_points(package_dir: Path) -> list[EntryPoint]:
  """The entry points the package's manifests declare, in their order, that
  resolve to a schema inside the package.

  An entry point names its schema by the URL it is published at
  (``https://xbrl.fasb.org/us-gaap/2025/entire/…``); the package catalog's
  ``rewriteURI`` maps that URL to the copy inside the package, and a relative
  ``href`` resolves against the manifest itself.
  """
  found: list[EntryPoint] = []
  for manifest in sorted(package_dir.rglob("META-INF/taxonomyPackage.xml")):
    meta_inf = manifest.parent
    try:
      root = ElementTree.parse(manifest).getroot()
    except ElementTree.ParseError as exc:
      logger.warning("unreadable taxonomy package manifest %s: %s", manifest, exc)
      continue
    rewrites = _catalog_rewrites(meta_inf / "catalog.xml")
    for element in root.iter():
      if _local_name(element.tag) != "entryPoint":
        continue
      name = ""
      href = ""
      for child in element:
        local = _local_name(child.tag)
        if local == "name" and not name:
          name = (child.text or "").strip()
        elif local == "entryPointDocument" and not href:
          href = (child.get("href") or "").strip()
      path = _resolve_package_href(href, meta_inf, rewrites) if href else None
      if path is None or not path.is_file() or not path.is_relative_to(package_dir):
        continue
      document = path.relative_to(package_dir).as_posix()
      found.append(EntryPoint(name=name or path.stem, document=document, path=path))
  return found


def _catalog_rewrites(catalog: Path) -> list[tuple[str, Path]]:
  """A package catalog's URL prefixes and the directories they map to,
  longest prefix first."""
  if not catalog.is_file():
    return []
  try:
    root = ElementTree.parse(catalog).getroot()
  except ElementTree.ParseError as exc:
    logger.warning("unreadable taxonomy package catalog %s: %s", catalog, exc)
    return []
  rewrites = [
    (start, catalog.parent / (element.get("rewritePrefix") or ""))
    for element in root.iter()
    if _local_name(element.tag) == "rewriteURI"
    and (start := element.get("uriStartString"))
  ]
  return sorted(rewrites, key=lambda r: -len(r[0]))


def _resolve_package_href(
  href: str, base: Path, rewrites: list[tuple[str, Path]]
) -> Path | None:
  """Where an ``href`` in a package manifest sits on disk, or ``None`` when
  it points outside the package."""
  if "://" not in href:
    return (base / href).resolve()
  for start, prefix in rewrites:
    if href.startswith(start):
      return (prefix / href[len(start) :]).resolve()
  return None


def _root_schemas(package_dir: Path) -> list[EntryPoint]:
  """The schemas in a package that no other schema in it imports or
  includes — where a DTS starts when no manifest says so. A published
  taxonomy with no manifest (GASB's exposure drafts) has one: the schema that
  imports its roles and types and links its linkbases."""
  schemas = sorted(
    p for p in package_dir.rglob("*.xsd") if p.is_file() and not p.name.startswith(".")
  )
  imported: set[Path] = set()
  for schema in schemas:
    for match in _SCHEMA_LOCATION_RE.finditer(schema.read_bytes()):
      for location in match.group(2).decode("utf-8", "replace").split():
        if "://" not in location:
          imported.add((schema.parent / location.split("#", 1)[0]).resolve())
  return [
    EntryPoint(
      name=schema.stem,
      document=schema.relative_to(package_dir).as_posix(),
      path=schema,
    )
    for schema in schemas
    if schema.resolve() not in imported
  ]


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
  if model.filing.primary_document:
    return Path(model.filing.primary_document).stem
  # A JSON report names itself; the file's stem may be an object-store key.
  return model.filing.accession or Path(stem).stem


def _is_local(path: Path) -> bool:
  """Whether ``path`` names something on disk — False, not an error, for a
  string too long to be a file name."""
  try:
    return path.exists()
  except OSError:
    return False


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
  "dei:EntityFilerCategory": "category",
  "dei:CurrentFiscalYearEndDate": "fiscal_year_end",
}
# The cover page tags the phone in two parts; the submissions header writes one
# string. Joined here so a filing enriched from itself and one enriched from
# EDGAR carry the same field.
_DEI_PHONE_FIELDS = ("dei:CityAreaCode", "dei:LocalPhoneNumber")


def _fiscal_year_end(value: str) -> str:
  """A fiscal year end as the submissions header writes it (``0131``).

  The cover page writes a gMonthDay (``--01-31``), EDGAR four digits. One
  shape, so the two sources can be compared and either can fill the field.
  """
  digits = "".join(c for c in value if c.isdigit())
  return digits[-4:] if len(digits) >= 4 else value


def _digits(value: str) -> str:
  """An EIN as the submissions header writes it: nine digits, no separator.

  The cover page writes ``94-3177549``, the header ``943177549``. Unnormalized,
  the same filer's EIN differs by which route read the filing.
  """
  digits = "".join(c for c in value if c.isdigit())
  return digits or value


_DEI_NORMALIZERS = {
  "dei:CurrentFiscalYearEndDate": _fiscal_year_end,
  "dei:EntityTaxIdentificationNumber": _digits,
}


def _enrich_from_dei(model: XbrlModel) -> XbrlModel:
  """Fill the entity's identity and the filing's form / period end from the
  cover page ``dei`` facts.

  The cover page is the filing's own account of who filed it, true as of the
  day it was filed. It is applied before the EDGAR submissions header, which
  is *current* rather than as-filed: a filer that has since changed exchange,
  name or filer category would otherwise have this year's answer attached to
  a filing from six years ago.
  """
  entity_updates: dict[str, Any] = {}
  filing_updates: dict[str, Any] = {}
  phone_parts: dict[str, str] = {}
  # A cover page that lists several securities tags the symbol per class
  # (with a dimension); the undimensioned fact wins, the first class stands
  # in when there is none.
  for fact in sorted(model.facts, key=lambda f: bool(f.dims)):
    if not fact.concept_qname.startswith("dei:"):
      continue
    value = (fact.value_str or "").strip()
    if not value:
      continue
    if fact.concept_qname in _DEI_PHONE_FIELDS and not fact.dims:
      phone_parts.setdefault(fact.concept_qname, value)
      continue
    field_name = _DEI_ENTITY_FIELDS.get(fact.concept_qname)
    if field_name and getattr(model.entity, field_name) in (None, ""):
      normalize = _DEI_NORMALIZERS.get(fact.concept_qname)
      entity_updates.setdefault(field_name, normalize(value) if normalize else value)
    elif fact.dims:
      continue
    elif fact.concept_qname == "dei:DocumentType" and not model.filing.form:
      filing_updates.setdefault("form", value)
    elif (
      fact.concept_qname == "dei:DocumentPeriodEndDate" and not model.filing.report_date
    ):
      filing_updates.setdefault("report_date", _date_or_none(value))
  area, local = (phone_parts.get(name) for name in _DEI_PHONE_FIELDS)
  if area and local and not model.entity.phone:
    entity_updates["phone"] = f"{area}-{local}"
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
