"""The filings a running server holds, and how each one gets there.

A :class:`FilingSession` turns a *source* — a local file, directory or zip,
an ``http(s)`` URL, an EDGAR ``cik:accession`` pair, or a ticker — into a
:class:`LoadedFiling`: the neutral :class:`~xbrlkit.model.XbrlModel`, the
primary document as plain text (the search and read target), and a section
map over that text (the 10-K Items and the tagged text blocks, with their
character offsets). Arelle is entered under a process-wide lock because it
keeps global state; the parsed model is what the tools read.
"""

from __future__ import annotations

import re
import shutil
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal
from zipfile import ZipFile

from xbrlkit.config import CONFIG, Config
from xbrlkit.model import EntityIdentity, FilingMeta, XbrlFact, XbrlModel
from xbrlkit.text.ixbrl import _strip_html, iXBRLParser
from xbrlkit.text.narrative import NarrativeExtractor, _html_to_text

SectionKind = Literal["item", "text_block"]

_INLINE_SUFFIXES = {".htm", ".html", ".xhtml"}
_ACCESSION_RE = re.compile(r"^\d{10}-\d{2}-\d{6}$")
_CIK_ACCESSION_RE = re.compile(r"^(\d{1,10})[:/](\d{10}-\d{2}-\d{6})$")
_TICKER_RE = re.compile(r"^([A-Za-z][A-Za-z0-9.\-]{0,9})(?:\s+([0-9A-Za-z\-/]+))?$")

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
      "or a ticker (optionally followed by a form, e.g. `NVDA 10-Q`)."
    )

  def _load_local(self, path: Path, source: str) -> LoadedFiling:
    package_dir: Path | None = None
    if path.is_file() and path.suffix.lower() == ".json":
      return self._load_model_json(path, source)
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
    model = self._parse(
      target, accession=_local_accession(path, target), filing=None, entity=None
    )
    return self._finish(_local_id(path, model), source, model, target, package_dir)

  def _load_model_json(self, path: Path, source: str) -> LoadedFiling:
    """A filing saved by ``export_filing model`` (the parse itself): no
    Arelle, no network. The primary document is read from beside it when
    the file named in the model's metadata is there."""
    try:
      model = XbrlModel.model_validate_json(path.read_text(encoding="utf-8"))
    except ValueError as exc:
      raise SourceError(f"{path} is not an XbrlModel JSON file: {exc}") from exc
    target: Path | None = None
    if model.filing.primary_document:
      candidate = path.parent / model.filing.primary_document
      if candidate.is_file():
        target = candidate
    model = _enrich_from_dei(model)
    return self._finish(_local_id(path, model), source, model, target, path.parent)

  def _load_url(self, url: str) -> LoadedFiling:
    accession = Path(url.split("?", 1)[0]).stem or url
    model = self._parse(url, accession=accession, filing=None, entity=None)
    target: Path | None = None
    if Path(url.split("?", 1)[0]).suffix.lower() in _INLINE_SUFFIXES:
      # Arelle keeps its own copy; fetch the document once more for the text.
      import requests

      resp = requests.get(url, headers=self.config.headers, timeout=60)
      resp.raise_for_status()
      target = self._tmp / Path(url.split("?", 1)[0]).name
      target.write_bytes(resp.content)
    return self._finish(accession, url, model, target, None)

  def _load_edgar(self, cik: str, accession: str, source: str) -> LoadedFiling:
    from xbrlkit.cli import entity_identity, filing_meta
    from xbrlkit.edgar import EdgarClient, download_filing

    client = EdgarClient(config=self.config)
    ref = client.get_filing_ref(cik, accession)
    info = client.company_info(cik)
    package_dir = self._tmp / accession
    target = download_filing(client, cik, accession, package_dir)
    filing = filing_meta(self.config.sec_base_url, cik, accession, ref, target.name)
    model = self._parse(target, accession, filing=filing, entity=entity_identity(info))
    return self._finish(accession, source, model, target, package_dir)

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
  ) -> XbrlModel:
    from xbrlkit.parse import close, load_model, to_xbrl_model

    with _ARELLE_LOCK:
      mx = load_model(
        target,
        cache_dir=self.config.arelle_cache_dir,
        offline=self.config.arelle_offline,
        timeout=self.config.arelle_timeout,
        config=self.config,
      )
      try:
        if filing is None:
          filing = _filing_meta_from_instance(mx, target, accession)
        model = to_xbrl_model(mx, filing, entity=entity)
      finally:
        close(mx.modelManager.cntlr)
    return _enrich_from_dei(model)

  def _finish(
    self,
    filing_id: str,
    source: str,
    model: XbrlModel,
    target: Path | None,
    package_dir: Path | None,
  ) -> LoadedFiling:
    html = None
    if target is not None and target.suffix.lower() in _INLINE_SUFFIXES:
      html = target.read_text(encoding="utf-8", errors="replace")
    block_text, block_sections = _text_from_text_blocks(model)
    if html is None:
      text, sections = block_text, block_sections
    else:
      text, sections = build_text(model, html)
    return LoadedFiling(
      id=filing_id,
      source=source,
      model=model,
      text=text,
      sections=sections,
      load_target=target,
      package_dir=package_dir,
      block_text=block_text,
      block_sections=block_sections,
      has_document=html is not None,
    )


# -- the readable document ------------------------------------------------------


def build_text(model: XbrlModel, html: str | None) -> tuple[str, list[TextSection]]:
  """The filing as plain text plus its section map.

  With the inline document in hand the text is the whole document and the
  sections are its Items (10-K / 10-Q) and tagged text blocks located in it.
  Without it (a classic instance, or a URL Arelle loaded that is not an
  inline document) the text is the tagged text blocks themselves, one after
  another under their concept names — every block a section with a known
  offset.
  """
  if html is None:
    return _text_from_text_blocks(model)

  text = _normalize_text(_html_to_text(html))
  sections: list[TextSection] = []
  form = model.filing.form or ""
  if form:
    for item in NarrativeExtractor(part_size=None).extract(html, form):
      sections.append(
        TextSection(
          id=item.section_id,
          label=item.section_label,
          kind="item",
          chars=len(item.content),
          offset=_locate(text, item.content),
        )
      )
  for block in iXBRLParser(part_size=None).parse(html):
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
  sections.sort(key=lambda s: (s.offset is None, s.offset or 0))
  return text, sections


def _text_from_text_blocks(model: XbrlModel) -> tuple[str, list[TextSection]]:
  parts: list[str] = []
  sections: list[TextSection] = []
  offset = 0
  for fact in model.facts:
    if fact.value_kind != "text" or not fact.value_str:
      continue
    concept = model.concepts.get(fact.concept_qname)
    if concept is None or not concept.is_textblock:
      continue
    body = _normalize_text(_strip_html(fact.value_str))
    if len(body.split()) < 20:
      continue
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


# -- filing identity without EDGAR ----------------------------------------------


_INSTANCE_ROOT_RE = re.compile(rb"<(?:[A-Za-z0-9_]+:)?xbrl[\s>]")


def _find_load_target(package_dir: Path) -> Path:
  """The file Arelle should load from a filing directory: the inline
  document (the largest ``.htm`` carrying ``ix:`` markup), else the XBRL
  instance — recognised by its root element, since a package need not
  follow EDGAR's naming (an instance called ``instance.xml`` beside
  ``report.xsd`` and hyphenated linkbases is a valid package too). A
  package that wraps itself in one directory is looked into."""
  entries = sorted(p for p in package_dir.iterdir() if not p.name.startswith("."))
  if len(entries) == 1 and entries[0].is_dir():
    return _find_load_target(entries[0])
  inline: list[tuple[int, Path]] = []
  instances: list[Path] = []
  for candidate in entries:
    if not candidate.is_file():
      continue
    suffix = candidate.suffix.lower()
    if suffix not in _INLINE_SUFFIXES and suffix not in (".xml", ".xbrl"):
      continue
    with candidate.open("rb") as fh:
      head = fh.read(200_000)
    if suffix in _INLINE_SUFFIXES:
      if b"ix:nonNumeric" in head or b"ix:nonFraction" in head or b"ix:header" in head:
        inline.append((candidate.stat().st_size, candidate))
    elif _INSTANCE_ROOT_RE.search(head[:4000]):
      instances.append(candidate)
  if inline:
    return max(inline)[1]
  if len(instances) == 1:
    return instances[0]
  if len(instances) > 1:
    # Several instances: prefer the one named after a schema, EDGAR-style.
    for schema in sorted(package_dir.glob("*.xsd")):
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
