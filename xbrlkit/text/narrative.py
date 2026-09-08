"""Extract the narrative Item sections of SEC 10-K and 10-Q filings.

Filers format the same document a hundred ways, so section detection is
multi-strategy: an Item-number regex over the document text, table-of-
contents heuristics to reject index rows and cross-references, own-block
scoring to pick the body heading, and a heading-name fallback for filers
that omit "Item N" entirely.

Sections extracted from a 10-K:
- Item 1: Business
- Item 1A: Risk Factors
- Item 1C: Cybersecurity
- Item 2: Properties
- Item 7: MD&A
- Item 7A: Market Risk

Sections extracted from a 10-Q (a 10-Q has two Item 2s and two Item 3s —
Part I's MD&A and Market Risk, Part II's Unregistered Sales and Defaults —
so the target names the Part):
- Part II, Item 1A: Risk Factors (quarterly updates)
- Part I, Item 2: MD&A
- Part I, Item 3: Market Risk
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from html.parser import HTMLParser

from .parts import DEFAULT_PART_SIZE, part_label, split_text
from .tables import html_tables_to_markdown


@dataclass
class ExtractedSection:
  """A single extracted narrative section, or one part of a long one."""

  section_id: str  # e.g. "item_1a"
  section_label: str  # e.g. "Risk Factors"
  content: str  # clean plain text, tables as markdown
  word_count: int  # of this part
  part: int = 1  # 1-based
  part_count: int = 1

  @property
  def label(self) -> str:
    """The label with the part suffix when the section was split: "MD&A (2/6)"."""
    return part_label(self.section_label, self.part, self.part_count)


# Target sections by form type: item number → (section_id, label, part).
# ``part`` is the Part the item must sit in, or None when the item number is
# unique across the form (every 10-K item is).
SECTIONS_10K: dict[str, tuple[str, str, str | None]] = {
  "1": ("item_1", "Business", None),
  "1A": ("item_1a", "Risk Factors", None),
  "1C": ("item_1c", "Cybersecurity", None),
  "2": ("item_2", "Properties", None),
  "7": ("item_7", "MD&A", None),
  "7A": ("item_7a", "Market Risk", None),
}

SECTIONS_10Q: dict[str, tuple[str, str, str | None]] = {
  "1A": ("item_1a", "Risk Factors", "II"),
  "2": ("item_2", "MD&A", "I"),
  "3": ("item_3", "Market Risk", "I"),
}

# Fallback: match sections by heading name when Item-number detection fails.
# Maps (section_id, label) to regex patterns that match common heading variations.
_NAME_BASED_SECTIONS_10K: dict[tuple[str, str], str] = {
  ("item_1a", "Risk Factors"): r"(?:^|\n)\s*Risk\s+Factors\s*\n",
  ("item_1c", "Cybersecurity"): r"(?:^|\n)\s*Cybersecurity\s*\n",
  ("item_2", "Properties"): r"(?:^|\n)\s*Properties\s*\n",
  ("item_7", "MD&A"): (r"(?:^|\n)\s*Management.s\s+Discussion\s+and\s+Analysis"),
  ("item_7a", "Market Risk"): (
    r"(?:^|\n)\s*Quantitative\s+and\s+Qualitative\s+Disclosures?\s+About\s+Market\s+Risk"
  ),
}

# A heading sits at the start of a line. In a table of contents rendered as a
# markdown pipe table it sits at the start of a cell instead, so a boundary
# may be preceded by pipes: "| Item 7. | Management's Discussion … | 25 |".
_ITEM_BOUNDARY_RE = re.compile(
  r"(?:^|\n)\s*(?:\|\s*)*(?:Item|ITEM)\s+(\d+[A-Z]?)[\.\s—–:]"
)
_PART_BOUNDARY_RE = re.compile(r"(?:^|\n)\s*(?:\|\s*)*(?:PART|Part)\s+([IV]+)\b")

# A cell that is a page reference: "25", "F-3", "ii".
_PAGE_CELL_RE = re.compile(r"(?:\d{1,3}|[A-Z]-\d{1,3}|[ivx]{1,5})", re.IGNORECASE)

# Minimum words for a section to be kept at all.
_MIN_SECTION_WORDS = 10

# A heading whose own block is shorter than this, amid a cluster of other
# Item headings, is an index entry rather than a section.
_MIN_OWN_BLOCK = 300


class _HTMLTextExtractor(HTMLParser):
  """Strip HTML tags, preserving meaningful whitespace."""

  def __init__(self):
    super().__init__()
    self.text: list[str] = []
    self._skip = False
    self._skip_tags = {"script", "style", "ix:header"}

  def handle_starttag(self, tag, attrs):
    tag_lower = tag.lower()
    if tag_lower in self._skip_tags:
      self._skip = True
    if tag_lower in ("p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6"):
      self.text.append("\n")
    if tag_lower == "td":
      self.text.append("\t")

  def handle_endtag(self, tag):
    if tag.lower() in self._skip_tags:
      self._skip = False
    if tag.lower() in (
      "p",
      "div",
      "tr",
      "li",
      "h1",
      "h2",
      "h3",
      "h4",
      "h5",
      "h6",
      "table",
    ):
      self.text.append("\n")

  def handle_data(self, data):
    if not self._skip:
      self.text.append(data)

  def get_text(self) -> str:
    return "".join(self.text)


def _html_to_text(html_content: str) -> str:
  """Convert HTML to clean text using fast parser.

  Tables are converted to markdown pipe tables before tag stripping,
  preserving column structure for financial data.
  """
  if "<table" in html_content or "<TABLE" in html_content:
    html_content = html_tables_to_markdown(html_content)
  extractor = _HTMLTextExtractor()
  extractor.feed(html_content)
  return extractor.get_text()


def _clean_text(text: str) -> str:
  """Clean extracted text — collapse whitespace, remove junk."""
  # Remove XBRL-style data blobs
  text = re.sub(r"[a-z]{2,10}:[A-Z][A-Za-z0-9]+Member", "", text)
  text = re.sub(r"\d{10,}", "", text)

  # Collapse multiple blank lines
  text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)

  # Collapse multiple spaces/tabs
  text = re.sub(r"[ \t]+", " ", text)

  # Remove empty lines, page numbers, TOC links
  lines = text.split("\n")
  cleaned: list[str] = []
  for line in lines:
    stripped = line.strip()
    if not stripped:
      if cleaned and cleaned[-1] != "":
        cleaned.append("")
      continue
    if re.match(r"^\d{1,3}$", stripped):
      continue
    if stripped.lower() == "table of contents":
      continue
    cleaned.append(stripped)

  return "\n".join(cleaned)


def _is_toc_row(line: str) -> bool:
  """A markdown table row with a page-reference cell is a table-of-contents
  row, whatever else it says: "| Item 7. | Management's Discussion … | 25 |"."""
  stripped = line.strip()
  if not stripped.startswith("|"):
    return False
  cells = [cell.strip() for cell in stripped.strip("|").split("|")]
  return any(cell and _PAGE_CELL_RE.fullmatch(cell) for cell in cells)


def _line_of(text: str, pos: int) -> str:
  line_start = text.rfind("\n", 0, pos) + 1
  line_end = text.find("\n", pos)
  return text[line_start : line_end if line_end != -1 else len(text)]


# A boundary is (position, item key, part): the key is the item number ("7A"),
# "PART" for a Part heading, or "END"; the part is the numeral of the last
# Part heading before it (None before the first).
Boundary = tuple[int, str, str | None]


def _build_boundary_list(text: str) -> list[Boundary]:
  """Sorted (position, key, part) for every Item and Part heading."""
  parts: list[tuple[int, str]] = [
    (m.start(), m.group(1).upper()) for m in _PART_BOUNDARY_RE.finditer(text)
  ]
  boundaries: list[Boundary] = [(pos, "PART", numeral) for pos, numeral in parts]
  for m in _ITEM_BOUNDARY_RE.finditer(text):
    boundaries.append((m.start(), m.group(1).upper(), _part_at(m.start(), parts)))
  boundaries.append((len(text), "END", None))
  boundaries.sort(key=lambda b: b[0])
  return boundaries


def _part_at(pos: int, parts: list[tuple[int, str]]) -> str | None:
  """The Part in force at ``pos``: the numeral of the last Part heading before it."""
  current: str | None = None
  for part_pos, numeral in parts:
    if part_pos >= pos:
      break
    current = numeral
  return current


def _same_item(boundary: Boundary, item: str, part: str | None) -> bool:
  """Whether a boundary is a heading of the target item (and Part, when one
  is required)."""
  _pos, key, boundary_part = boundary
  return key == item and (part is None or boundary_part == part)


def _find_section_end(
  start: int,
  item: str,
  part: str | None,
  boundaries: list[Boundary],
  text_len: int,
) -> int:
  """Find end position for a section, spanning repeated same-item blocks.

  Some filers (e.g. MSFT) repeat the same "Item N" marker for each sub-section,
  with "PART I" markers in between. This finds the last same-item boundary,
  then returns the position of the next different-item boundary after it.
  PART boundaries between same-item blocks are treated as internal structure.
  """
  last_same = start
  for boundary in boundaries:
    if boundary[0] > start and _same_item(boundary, item, part):
      last_same = boundary[0]

  for boundary in boundaries:
    pos, key, _ = boundary
    if pos <= last_same:
      continue
    if key == "PART":
      has_more_same = any(b[0] > pos and _same_item(b, item, part) for b in boundaries)
      if not has_more_same:
        return pos
      continue
    if not _same_item(boundary, item, part):
      return pos
  return text_len


def _own_block_length(start: int, item: str, part: str | None, boundaries) -> int:
  """Characters from ``start`` to the next heading that is not this item's.

  This scores a candidate on its own block only. Scoring it on the span to
  the *last* same-item heading let a table-of-contents row or a cross-
  reference near the front of the document outscore the body heading, since
  the span from the front covers the whole body.
  """
  for boundary in boundaries:
    if boundary[0] > start and not _same_item(boundary, item, part):
      return boundary[0] - start
  return boundaries[-1][0] - start


# What may precede a heading on its line: pipes, bullets, punctuation, or a
# Part prefix ("PART II — Item 7."). Anything else is a cross-reference.
_HEADING_PREFIX_RE = re.compile(r"(?:PART\s+[IV]+)?[\s\|•·\.,:;—–\-]*", re.IGNORECASE)
_OPENING_QUOTES = "\"'“‘"


def _is_toc_candidate(text: str, pos: int, own_block: int) -> bool:
  """Whether an "Item N" match sits in a table of contents or a cross-
  reference rather than at the head of its section. ``own_block`` is the
  candidate's own-block length (:func:`_own_block_length`)."""
  # Its own line is an index row: "| Item 7. | … | 25 |"
  if _is_toc_row(_line_of(text, pos)):
    return True

  # Cross-reference: a heading starts its line (or its cell). Text before it
  # on the line — "Refer to Risk Factors (Part II, Item 1A" — or an opening
  # quote — see “Item 7A. Quantitative…” — makes it a mention.
  line_start = text.rfind("\n", 0, pos)
  preceding = text[line_start + 1 : pos] if line_start >= 0 else text[:pos]
  if preceding.strip() and not _HEADING_PREFIX_RE.fullmatch(preceding):
    return True
  if preceding.strip() and preceding.rstrip()[-1] in _OPENING_QUOTES:
    return True

  after = text[pos : pos + 1000]
  # Many Item headings clustered right after a heading whose own block is a
  # line or two (skip its own first 50 chars): an index. Real sections may
  # reference 1–3 other items in their opening paragraph, and a short
  # section (Properties, then Legal Proceedings, then Mine Safety) has three
  # headings within a thousand characters but a block of its own.
  other_items = len(re.findall(r"ITEM\s+\d", after[50:], re.IGNORECASE))
  if other_items > 3 and own_block < _MIN_OWN_BLOCK:
    return True

  # Standalone page numbers, or index rows naming other Items, in the next
  # 500 chars: a section-specific table of contents.
  nearby_lines = after[:500].split("\n")
  page_number_lines = sum(
    1 for line in nearby_lines if re.match(r"^\s*\d{1,3}\s*$", line)
  )
  item_rows = sum(
    1
    for line in nearby_lines[1:]
    if _is_toc_row(line) and re.search(r"\bItem\s+\d", line, re.IGNORECASE)
  )
  return page_number_lines + item_rows >= 2


def _find_item_candidates(
  text: str, item: str, part: str | None, boundaries: list[Boundary]
) -> list[tuple[int, int]]:
  """(start, own-block length) for every plausible heading of ``item``."""
  # Match "ITEM 1." or "ITEM 1A." followed by a separator: period, whitespace,
  # em-dash (U+2014), en-dash (U+2013), colon. Em-dash format seen in COST
  # filings: "Item 2—Management's Discussion…"
  if item[-1].isalpha():
    pattern = rf"ITEM\s+{re.escape(item)}[\.\s—–:]"
  else:
    pattern = rf"ITEM\s+{re.escape(item)}(?![A-Z])[\.\s—–:]"

  parts = [(pos, numeral) for pos, key, numeral in boundaries if key == "PART"]
  candidates: list[tuple[int, int]] = []
  for m in re.finditer(pattern, text, re.IGNORECASE):
    if part is not None and _part_at(m.start(), parts) != part:
      continue
    own_block = _own_block_length(m.start(), item, part, boundaries)
    if _is_toc_candidate(text, m.start(), own_block):
      continue
    candidates.append((m.start(), own_block))
  return candidates


def _extend_start_backwards(
  best_start: int,
  candidates: list[tuple[int, int]],
  item: str,
  part: str | None,
  boundaries: list[Boundary],
) -> int:
  """From the best-scoring heading, walk back over earlier candidates while
  only same-item and Part headings lie between: a filer that repeats
  "Item 7" for each sub-section starts the section at the first one."""
  start = best_start
  for cand_start, _ in sorted(candidates, reverse=True):
    if cand_start >= start:
      continue
    between = [b for b in boundaries if cand_start < b[0] < start]
    if all(b[1] == "PART" or _same_item(b, item, part) for b in between):
      start = cand_start
    else:
      break
  return start


def _find_item_sections(
  text: str,
  target_items: dict[str, tuple[str, str, str | None]],
  boundaries: list[Boundary],
) -> dict[str, dict]:
  """Find the start of each target Item section (the body, not the TOC).

  Strategy: find every match for the item, drop table-of-contents rows and
  cross-references, score each survivor on its own block, take the best,
  then extend the start back over any repeated same-item headings. A target
  that names a Part is searched in that Part first; if the document's Part
  headings do not place any candidate there (a filer that puts them only in
  its table of contents), the Part is ignored for that item.
  """
  sections: dict[str, dict] = {}

  for item, (section_id, label, part) in target_items.items():
    candidates = _find_item_candidates(text, item, part, boundaries)
    if not candidates and part is not None:
      part = None
      candidates = _find_item_candidates(text, item, part, boundaries)
    if not candidates:
      continue

    best_start, _ = max(candidates, key=lambda c: (c[1], -c[0]))
    start = _extend_start_backwards(best_start, candidates, item, part, boundaries)
    sections[item] = {
      "section_id": section_id,
      "label": label,
      "start": start,
      "part": part,
    }

  return sections


def _find_sections_by_name(text: str, boundaries: list[Boundary]) -> dict:
  """Fallback: find sections by heading name when Item-number detection fails.

  Some filers (e.g. INTC) use section headings like "Risk Factors" and
  "Management's Discussion and Analysis" without "Item X" prefixes. This
  searches for those heading names directly, applying the same TOC/page-number
  filters and best-content-length scoring.
  """
  sections = {}

  for (section_id, label), pattern in _NAME_BASED_SECTIONS_10K.items():
    matches = list(re.finditer(pattern, text, re.IGNORECASE))

    candidates: list[tuple[int, int]] = []
    for m in matches:
      if _is_toc_row(_line_of(text, m.start() + 1)):
        continue
      after = text[m.start() : m.start() + 500]

      # Filter TOC entries: standalone page numbers in nearby text
      nearby_lines = after[:500].split("\n")
      page_number_lines = sum(
        1 for line in nearby_lines if re.match(r"^\s*\d{1,3}\s*$", line)
      )
      if page_number_lines >= 2:
        continue

      # Score by content length to next boundary (any boundary works here
      # since we don't have a specific item number to skip)
      end = len(text)
      for pos, _key, _part in boundaries:
        if pos > m.start() + 200:
          end = pos
          break
      content_len = end - m.start()
      candidates.append((m.start(), content_len))

    if candidates:
      best_start, _ = max(candidates, key=lambda c: c[1])
      sections[section_id] = {
        "section_id": section_id,
        "label": label,
        "start": best_start,
        "part": None,
      }

  return sections


class NarrativeExtractor:
  """Extract narrative sections from SEC 10-K/10-Q HTML filings.

  ``part_size`` is the target length of one part in characters; a section
  longer than that is split at paragraph boundaries into balanced parts
  (see :func:`xbrlkit.text.parts.split_text`). ``None`` keeps every section
  whole.
  """

  def __init__(self, part_size: int | None = DEFAULT_PART_SIZE) -> None:
    self.part_size = part_size

  def extract(self, html: str, form_type: str) -> list[ExtractedSection]:
    """Extract narrative sections from a filing HTML document.

    Args:
        html: Raw HTML content of the filing
        form_type: SEC form type ("10-K" or "10-Q"; amendments accepted)

    Returns:
        Sections in document order, each split into parts when long
    """
    form_upper = form_type.upper().replace("/A", "")
    # `10-K405` is a 10-K whose Rule 405 box is ticked — the common form
    # through the 1990s, and the same document with the same Items.
    if form_upper.endswith("405"):
      form_upper = form_upper[: -len("405")]
    if form_upper in ("10-K", "10-KSB", "20-F", "40-F"):
      target_items = SECTIONS_10K
    elif form_upper in ("10-Q", "10-QSB"):
      target_items = SECTIONS_10Q
    else:
      return []

    text = _html_to_text(html)
    boundaries = _build_boundary_list(text)

    sections = _find_item_sections(text, target_items, boundaries)

    # Fallback: if Item-number detection found nothing, try name-based detection.
    # Only for 10-K/10-KSB (INTC, Duke Energy, etc. omit "Item X" prefixes).
    # Excluded: 20-F/40-F use different item numbering and section names.
    use_name_fallback = False
    if not sections and form_upper in ("10-K", "10-KSB"):
      sections = _find_sections_by_name(text, boundaries)
      use_name_fallback = bool(sections)

    if not sections:
      return []

    sorted_items = sorted(sections.items(), key=lambda x: x[1]["start"])

    results: list[ExtractedSection] = []
    for item, info in sorted_items:
      start = info["start"]

      if use_name_fallback:
        # Name-based sections don't have item numbers in the boundary list,
        # so find end by looking for the next detected section or boundary.
        remaining_starts = [
          s["start"] for k, s in sections.items() if s["start"] > start + 200
        ]
        for pos, _key, _part in boundaries:
          if pos > start + 200:
            remaining_starts.append(pos)
        end = min(remaining_starts) if remaining_starts else len(text)
      else:
        end = _find_section_end(start, item, info["part"], boundaries, len(text))

      section_text = _clean_text(text[start:end])

      if len(section_text.split()) < _MIN_SECTION_WORDS:
        continue

      parts = split_text(section_text, self.part_size)
      for index, part_text in enumerate(parts, start=1):
        results.append(
          ExtractedSection(
            section_id=info["section_id"],
            section_label=info["label"],
            content=part_text,
            word_count=len(part_text.split()),
            part=index,
            part_count=len(parts),
          )
        )

    return results
