"""The filing's text: disclosure sections, narrative Items, tables, parts.

Everything here reads the filing's primary HTML document and needs nothing
else — no Arelle, no network. Two parsers produce sections:

- :class:`iXBRLParser` — the inline-XBRL text blocks (notes, policies,
  tables) with the XBRL element names each contains, continuation chains
  resolved.
- :class:`NarrativeExtractor` — the 10-K / 10-Q Item sections (Business,
  Risk Factors, MD&A, …) found by heading detection.

Both split a long section into balanced parts (:func:`split_text`) instead
of truncating it, and both render HTML tables as markdown pipe tables
(:func:`html_tables_to_markdown`).
"""

from .ixbrl import MIN_SECTION_WORDS, iXBRLParser, iXBRLSection
from .narrative import (
  SECTIONS_10K,
  SECTIONS_10Q,
  SECTIONS_20F,
  ExtractedSection,
  NarrativeExtractor,
)
from .parts import DEFAULT_PART_SIZE, part_label, split_text
from .tables import html_tables_to_markdown

__all__ = (
  "DEFAULT_PART_SIZE",
  "MIN_SECTION_WORDS",
  "SECTIONS_10K",
  "SECTIONS_10Q",
  "SECTIONS_20F",
  "ExtractedSection",
  "NarrativeExtractor",
  "html_tables_to_markdown",
  "iXBRLParser",
  "iXBRLSection",
  "part_label",
  "split_text",
)
