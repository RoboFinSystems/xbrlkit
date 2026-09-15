"""Tests for the 10-K / 10-Q narrative section extractor (``xbrlkit.text.narrative``)."""

import pytest

from xbrlkit.text import NarrativeExtractor
from xbrlkit.text.narrative import (
  _build_boundary_list,
  _clean_text,
  _html_to_text,
  _is_toc_row,
)

# Enough content between sections that a heading's own block outweighs any
# index row or cross-reference.
_FILLER = " ".join(["The company continues to operate in a competitive market."] * 20)


@pytest.mark.unit
class TestHtmlToText:
  def test_strips_tags(self):
    text = _html_to_text("<p>Hello <b>world</b></p>")
    assert "Hello" in text and "world" in text and "<" not in text

  def test_skips_script_style(self):
    html = (
      "<p>Visible</p><script>hidden</script><style>.hidden{}</style><p>Also visible</p>"
    )
    text = _html_to_text(html)
    assert "Visible" in text and "Also visible" in text
    assert "hidden" not in text

  def test_preserves_block_element_newlines(self):
    assert "\n" in _html_to_text("<p>First</p><p>Second</p>")


@pytest.mark.unit
class TestCleanText:
  def test_collapses_whitespace(self):
    assert "Hello world tabs" in _clean_text("Hello     world\t\ttabs")

  def test_removes_page_numbers(self):
    assert "42" not in _clean_text("Some text\n42\nMore text")

  def test_removes_toc_links(self):
    assert "Table of Contents" not in _clean_text("Some text\nTable of Contents\nMore")

  def test_removes_xbrl_member_blobs(self):
    assert "Member" not in _clean_text("us-gaap:EquitySecuritiesMember some text")


@pytest.mark.unit
class TestBoundaries:
  def test_toc_row_has_a_page_cell(self):
    assert _is_toc_row("| Item 7. | Management's Discussion and Analysis | 25 |")
    assert _is_toc_row("| Item 8. | Financial Statements | F-1 |")
    assert _is_toc_row("|  | Overview | 25 |")
    assert not _is_toc_row("| Item 7. | Management's Discussion and Analysis |")
    assert not _is_toc_row("Item 7. Management's Discussion and Analysis")

  def test_index_rows_are_boundaries_and_carry_their_part(self):
    text = (
      "| PART I | |\n| Item 1. | Business | 3 |\n| Item 7. | MD&A | 25 |\n"
      "\nPART I\n\nItem 1. Business\n\nPART II\n\nItem 7. MD&A\n"
    )
    keys = [(key, part) for _pos, key, part in _build_boundary_list(text)]
    assert keys == [
      ("PART", "I"),
      ("1", "I"),
      ("7", "I"),
      ("PART", "I"),
      ("1", "I"),
      ("PART", "II"),
      ("7", "II"),
      ("END", None),
    ]


SAMPLE_10K_HTML = f"""
<html><body>
<h2>PART I</h2>
<h3>ITEM 1. BUSINESS</h3>
<p>We are a technology company that develops semiconductor products.
{_FILLER}</p>
<h3>ITEM 1A. RISK FACTORS</h3>
<p>Our business faces several significant risks including supply chain and tariffs.
{_FILLER}</p>
<h3>ITEM 2. PROPERTIES</h3>
<p>Our principal offices are located in Santa Clara, California.
{_FILLER}</p>
<h2>PART II</h2>
<h3>ITEM 7. MANAGEMENT'S DISCUSSION AND ANALYSIS</h3>
<p>Revenue increased 25% year-over-year driven by strong data center demand.
{_FILLER}</p>
<h3>ITEM 8. FINANCIAL STATEMENTS</h3>
<p>See the consolidated financial statements beginning on page F-1.</p>
</body></html>
"""


def _sections(html: str, form: str, part_size: int | None = None) -> dict:
  extractor = NarrativeExtractor(part_size=part_size)
  return {s.section_id: s for s in extractor.extract(html, form) if s.part == 1}


@pytest.mark.unit
class TestNarrativeExtractor:
  def test_extracts_10k_sections(self):
    sections = _sections(SAMPLE_10K_HTML, "10-K")
    assert {"item_1", "item_1a", "item_2", "item_7"} <= set(sections)

  def test_section_content_is_clean(self):
    for section in NarrativeExtractor().extract(SAMPLE_10K_HTML, "10-K"):
      assert "<" not in section.content and ">" not in section.content
      assert section.word_count > 0

  def test_business_section_content(self):
    business = _sections(SAMPLE_10K_HTML, "10-K")["item_1"]
    assert "semiconductor" in business.content.lower()
    assert business.section_label == "Business"
    assert business.label == "Business"
    assert "RISK FACTORS" not in business.content

  def test_amendments_and_small_business_forms(self):
    assert "item_1" in _sections(SAMPLE_10K_HTML, "10-K/A")
    assert "item_1" in _sections(SAMPLE_10K_HTML, "10-KSB")

  def test_10q_extracts_different_sections(self):
    html = """
    <html><body>
    <h3>ITEM 2. MANAGEMENT'S DISCUSSION AND ANALYSIS</h3>
    <p>Revenue grew significantly in the quarter driven by cloud computing demand.
    We saw strong momentum across all product categories and geographies.
    Operating margins improved due to favorable product mix and cost controls.</p>
    <h3>ITEM 3. QUANTITATIVE AND QUALITATIVE DISCLOSURES ABOUT MARKET RISK</h3>
    <p>Interest rate risk, foreign currency risk, and equity price risk
    are the primary market risks we face in our ongoing operations.</p>
    </body></html>
    """
    sections = _sections(html, "10-Q")
    assert set(sections) == {"item_2", "item_3"}
    assert sections["item_2"].section_label == "MD&A"

  def test_unsupported_form_type_returns_empty(self):
    assert NarrativeExtractor().extract("<html></html>", "8-K") == []

  def test_skips_trivial_sections(self):
    html = """
    <html><body>
    <h3>ITEM 1. BUSINESS</h3>
    <p>Short.</p>
    <h3>ITEM 1A. RISK FACTORS</h3>
    <p>This section has enough words to pass the minimum threshold for inclusion
    in the extracted narrative sections output list and contains important details.</p>
    </body></html>
    """
    sections = _sections(html, "10-K")
    assert "item_1a" in sections
    assert "item_1" not in sections

  def test_name_based_fallback_when_items_are_unnumbered(self):
    html = f"""
    <html><body>
    <p>Risk Factors</p>
    <p>RISK BODY {_FILLER}</p>
    <p>Management's Discussion and Analysis of Financial Condition</p>
    <p>MDNA BODY {_FILLER}</p>
    </body></html>
    """
    sections = _sections(html, "10-K")
    assert "RISK BODY" in sections["item_1a"].content
    assert "MDNA BODY" in sections["item_7"].content


TOC_10K_HTML = f"""
<html><body>
<p>UNITED STATES SECURITIES AND EXCHANGE COMMISSION</p>
<p>FORM 10-K</p>
<p>TABLE OF CONTENTS</p>
<table>
<tr><td>Item 1.</td><td>Business</td><td>3</td></tr>
<tr><td></td><td>Overview</td><td>3</td></tr>
<tr><td>Item 1A.</td><td>Risk Factors</td><td>12</td></tr>
<tr><td>Item 1C.</td><td>Cybersecurity</td><td>20</td></tr>
<tr><td>Item 2.</td><td>Properties</td><td>21</td></tr>
<tr><td>Item 3.</td><td>Legal Proceedings</td><td>21</td></tr>
<tr><td>Item 4.</td><td>Mine Safety Disclosures</td><td>21</td></tr>
<tr><td>PART II</td><td></td><td></td></tr>
<tr><td>Item 7.</td><td>Management's Discussion and Analysis</td><td>25</td></tr>
<tr><td></td><td>Overview</td><td>25</td></tr>
<tr><td></td><td>Results of Operations</td><td>27</td></tr>
<tr><td>Item 7A.</td><td>Quantitative and Qualitative Disclosures About Market Risk</td><td>40</td></tr>
<tr><td>Item 8.</td><td>Financial Statements</td><td>42</td></tr>
</table>
<p>PART I</p>
<p>Item 1. Business</p><p>BUSINESS BODY {_FILLER}</p>
<p>Item 1A. Risk Factors</p><p>RISK BODY {_FILLER}</p>
<p>Item 1C. Cybersecurity</p><p>CYBER BODY {_FILLER}</p>
<p>Item 2. Properties</p>
<p>PROPERTIES BODY We own our headquarters in St. Paul and lease forty offices in
twelve countries, all suitable for their purposes. The company operates 51
manufacturing facilities in 26 states and 65 facilities in 25 countries, owns the
majority of its physical properties, and shares many of them across segments.</p>
<p>Item 3. Legal Proceedings</p><p>None material.</p>
<p>Item 4. Mine Safety Disclosures</p><p>Not applicable.</p>
<p>PART II</p>
<p>Item 7. Management's Discussion and Analysis</p><p>MDNA BODY {_FILLER}</p>
<p>Item 7A. Quantitative and Qualitative Disclosures About Market Risk</p>
<p>MARKET RISK BODY {_FILLER}</p>
<p>Item 8. Financial Statements</p><p>See F-1.</p>
</body></html>
"""


@pytest.mark.unit
class TestTableOfContents:
  def test_sections_start_at_the_body_heading_not_the_index_row(self):
    """A table of contents rendered as a pipe table defeats the standalone-
    page-number heuristic (the numbers sit in cells). On 3M's FY2024 10-K
    the indexed MD&A was the index row, then the cover, Item 1 and half of
    Item 1A."""
    sections = _sections(TOC_10K_HTML, "10-K")
    assert sections["item_7"].content.startswith("Item 7. Management's Discussion")
    assert "MDNA BODY" in sections["item_7"].content
    assert "| 25 |" not in sections["item_7"].content
    assert "BUSINESS BODY" not in sections["item_7"].content
    assert sections["item_7a"].content.startswith("Item 7A. Quantitative")
    assert "MARKET RISK BODY" in sections["item_7a"].content
    assert "Item 8" not in sections["item_7a"].content
    assert sections["item_1"].content.startswith("Item 1. Business")
    assert "TABLE OF CONTENTS" not in sections["item_1"].content

  def test_short_section_beside_short_sections_is_kept(self):
    """Properties, Legal Proceedings and Mine Safety are each a paragraph,
    so four Item headings fall within a thousand characters of the
    Properties heading — the shape of an index, except that the heading
    has a block of its own."""
    sections = _sections(TOC_10K_HTML, "10-K")
    assert "PROPERTIES BODY" in sections["item_2"].content
    assert "Legal Proceedings" not in sections["item_2"].content


@pytest.mark.unit
class TestCrossReferences:
  def test_quoted_cross_reference_at_line_start_is_not_a_heading(self):
    html = f"""
    <html><body>
    <p>Item 1. Business</p><p>{_FILLER}</p>
    <p>“Item 7A. Quantitative and Qualitative Disclosures About Market Risk” describes our HEDGING.</p>
    <p>{_FILLER}</p><p>{_FILLER}</p>
    <p>See “Item 7A. Quantitative and Qualitative Disclosures About Market Risk” for more HEDGING.</p>
    <p>{_FILLER}</p><p>{_FILLER}</p>
    <p>Item 1A. Risk Factors</p><p>{_FILLER}</p>
    <p>Item 7. Management's Discussion and Analysis</p><p>{_FILLER}</p>
    <p>Item 7A. Quantitative and Qualitative Disclosures About Market Risk</p>
    <p>MARKET RISK BODY is a short section of a few sentences about interest rates and currencies.</p>
    <p>Item 8. Financial Statements</p><p>See F-1.</p>
    </body></html>
    """
    sections = _sections(html, "10-K")
    assert sections["item_7a"].content.startswith("Item 7A. Quantitative")
    assert "MARKET RISK BODY" in sections["item_7a"].content
    assert "HEDGING" not in sections["item_7a"].content
    assert "HEDGING" in sections["item_1"].content

  def test_part_prefix_on_the_heading_line_is_allowed(self):
    html = f"""
    <html><body>
    <p>PART II — Item 7. Management's Discussion and Analysis</p><p>MDNA BODY {_FILLER}</p>
    <p>Item 8. Financial Statements</p><p>See F-1.</p>
    </body></html>
    """
    assert "MDNA BODY" in _sections(html, "10-K")["item_7"].content


def _10q_html(with_parts: bool) -> str:
  part_i = "<p>PART I. FINANCIAL INFORMATION</p>" if with_parts else ""
  part_ii = "<p>PART II. OTHER INFORMATION</p>" if with_parts else ""
  # Part II's sections are longer, so length alone would pick them.
  return f"""
  <html><body>
  {part_i}
  <p>Item 1. Financial Statements</p><p>{_FILLER}</p>
  <p>Item 2. Management's Discussion and Analysis of Financial Condition and Results of Operations</p>
  <p>MDNA BODY {_FILLER}</p>
  <p>Item 3. Quantitative and Qualitative Disclosures About Market Risk</p>
  <p>MARKET RISK BODY {_FILLER}</p>
  <p>Item 4. Controls and Procedures</p><p>{_FILLER}</p>
  {part_ii}
  <p>Item 1. Legal Proceedings</p><p>{_FILLER}</p>
  <p>Item 1A. Risk Factors</p><p>RISK BODY {_FILLER} {_FILLER}</p>
  <p>Item 2. Unregistered Sales of Equity Securities and Use of Proceeds</p>
  <p>UNREGISTERED BODY {_FILLER} {_FILLER}</p>
  <p>Item 3. Defaults Upon Senior Securities</p>
  <p>DEFAULTS BODY {_FILLER} {_FILLER}</p>
  <p>Item 4. Mine Safety Disclosures</p><p>Not applicable.</p>
  </body></html>
  """


@pytest.mark.unit
class Test10QParts:
  def test_part_i_items_are_not_confused_with_part_ii(self):
    """A 10-Q has two Item 2s and two Item 3s. Keyed on the number alone,
    KKR's and loanDepot's "Market Risk" documents held Part II's
    "Defaults Upon Senior Securities"."""
    sections = _sections(_10q_html(with_parts=True), "10-Q")
    assert "MDNA BODY" in sections["item_2"].content
    assert "UNREGISTERED" not in sections["item_2"].content
    assert "MARKET RISK BODY" in sections["item_3"].content
    assert "DEFAULTS" not in sections["item_3"].content
    assert "Item 4" not in sections["item_3"].content
    assert "RISK BODY" in sections["item_1a"].content
    assert "UNREGISTERED" not in sections["item_1a"].content

  def test_without_part_headings_the_part_is_ignored(self):
    sections = _sections(_10q_html(with_parts=False), "10-Q")
    assert {"item_1a", "item_2", "item_3"} <= set(sections)
    assert "RISK BODY" in sections["item_1a"].content


@pytest.mark.unit
class TestRepeatedHeadings:
  def test_repeated_same_item_blocks_form_one_section(self):
    """Some filers (MSFT) repeat "Item N" for each sub-section with a
    "PART I" marker between: the section runs from the first to the end
    of the last."""
    html = f"""
    <html><body>
    <p>PART I</p>
    <p>Item 1. Note About Forward-Looking Statements</p><p>FORWARD BODY {_FILLER}</p>
    <p>PART I</p>
    <p>Item 1. Business</p><p>BUSINESS BODY {_FILLER} {_FILLER}</p>
    <p>Item 1A. Risk Factors</p><p>RISK BODY {_FILLER}</p>
    </body></html>
    """
    sections = NarrativeExtractor().extract(html, "10-K")
    business = [s for s in sections if s.section_id == "item_1"]
    assert len(business) == 1
    assert business[0].content.startswith("Item 1. Note About")
    assert "BUSINESS BODY" in business[0].content
    assert "RISK BODY" not in business[0].content


@pytest.mark.unit
class TestParts:
  def test_long_section_is_split_into_parts(self):
    paragraphs = "".join(
      f"<p>Paragraph {i} " + " ".join(["word"] * 60) + "</p>" for i in range(40)
    )
    html = f"""
    <html><body>
    <h3>ITEM 1. BUSINESS</h3>
    {paragraphs}
    <h3>ITEM 1A. RISK FACTORS</h3>
    <p>RISK BODY {_FILLER}</p>
    </body></html>
    """
    whole = {
      s.section_id: s for s in NarrativeExtractor(part_size=None).extract(html, "10-K")
    }
    parts = [
      s
      for s in NarrativeExtractor(part_size=3_000).extract(html, "10-K")
      if s.section_id == "item_1"
    ]
    assert len(parts) > 1
    assert [p.part for p in parts] == list(range(1, len(parts) + 1))
    assert {p.part_count for p in parts} == {len(parts)}
    assert parts[1].label == f"Business (2/{len(parts)})"
    assert " ".join(p.content for p in parts).split() == whole["item_1"].content.split()
    assert sum(p.word_count for p in parts) == whole["item_1"].word_count
    for p in parts[1:]:
      assert p.content.startswith("Paragraph")
    assert "RISK BODY" not in parts[-1].content


# The items gap 1 adds, and the two rules that decide what they are worth.

SAMPLE_PART_III_HTML = f"""
<html><body>
<h2>PART I</h2>
<h3>ITEM 3. LEGAL PROCEEDINGS</h3>
<p>The Company is a defendant in respirator mask litigation in Hennepin County.
{_FILLER}</p>
<h2>PART II</h2>
<h3>ITEM 5. MARKET FOR REGISTRANT'S COMMON EQUITY</h3>
<p>The board authorised a repurchase of up to $7.5 billion of common stock.
{_FILLER}</p>
<h3>ITEM 9A. CONTROLS AND PROCEDURES</h3>
<p>Management identified a material weakness in controls over revenue recognition.
{_FILLER}</p>
<h2>PART III</h2>
<h3>ITEM 11. EXECUTIVE COMPENSATION</h3>
<p>The information required by this Item is incorporated by reference to the
Registrant's definitive Proxy Statement for the 2026 Annual Meeting.</p>
<h3>ITEM 13. CERTAIN RELATIONSHIPS AND RELATED TRANSACTIONS</h3>
<p>The Audit Committee reviewed each related-party transaction set out below.
{_FILLER}</p>
</body></html>
"""


@pytest.mark.unit
class TestAddedItems:
  def test_legal_proceedings_market_and_controls_are_captured(self):
    sections = _sections(SAMPLE_PART_III_HTML, "10-K")
    assert {"item_3", "item_5", "item_9a"} <= set(sections)
    assert "respirator" in sections["item_3"].content.lower()
    assert "repurchase" in sections["item_5"].content.lower()
    assert "material weakness" in sections["item_9a"].content.lower()

  def test_a_part_iii_item_that_points_at_the_proxy_is_not_a_section(self):
    """Answering a compensation question with a cross-reference is worse than
    not answering it."""
    assert "item_11" not in _sections(SAMPLE_PART_III_HTML, "10-K")

  def test_a_part_iii_item_set_out_inline_is_kept(self):
    sections = _sections(SAMPLE_PART_III_HTML, "10-K")
    assert "item_13" in sections
    assert "Audit Committee" in sections["item_13"].content

  def test_a_cross_reference_is_a_pointer_too(self):
    """Not every pointer says "incorporated by reference" — NVDA's Item 3 and
    Pfizer's Item 7A both just name the note the content is in."""
    html = """
    <html><body>
    <h3>ITEM 3. LEGAL PROCEEDINGS</h3>
    <p>Please see Note 12 of the Notes to the Consolidated Financial Statements
    in Part IV, Item 15 of this Annual Report on Form 10-K for a discussion of
    our legal proceedings.</p>
    </body></html>
    """
    assert "item_3" not in _sections(html, "10-K")

  def test_the_pointer_rule_is_not_only_for_the_new_items(self):
    """Pfizer's Item 7A is 42 words of "incorporated by reference to … MD&A".
    A pointer is not a section whichever item it is, and the MD&A it points at
    is indexed in its own right."""
    html = """
    <html><body>
    <h3>ITEM 7A. QUANTITATIVE AND QUALITATIVE DISCLOSURES ABOUT MARKET RISK</h3>
    <p>The information required by this Item is incorporated by reference to
    the discussion in the Analysis of Financial Condition, Liquidity, Capital
    Resources and Market Risk section within MD&amp;A.</p>
    </body></html>
    """
    assert "item_7a" not in _sections(html, "10-K")

  def test_an_item_that_names_the_pages_its_content_is_on_is_not_a_section(self):
    """JPMorgan files its MD&A, market risk and cybersecurity items as one
    sentence each naming a page range of the annual report. A page range is an
    address like a note number is, and the pointer rule had no word for it."""
    html = """
    <html><body>
    <h3>ITEM 7. MANAGEMENT'S DISCUSSION AND ANALYSIS</h3>
    <p>Management's discussion and analysis of financial condition and results
    of operations, entitled "Management's discussion and analysis," appears on
    pages 46-160. Such information should be read in conjunction with the
    Consolidated Financial Statements.</p>
    <h3>ITEM 7A. QUANTITATIVE AND QUALITATIVE DISCLOSURES ABOUT MARKET RISK</h3>
    <p>Refer to the Market Risk Management section of Management's discussion
    and analysis on pages 133-142 for a discussion of quantitative and
    qualitative disclosures about market risk.</p>
    </body></html>
    """
    sections = _sections(html, "10-K")
    assert "item_7" not in sections and "item_7a" not in sections

  def test_a_section_that_answers_first_and_cites_on_its_way_out_is_kept(self):
    """General Mills' Item 3 states the position on its legal actions in full,
    then points at Item 1 for environmental matters. The citation is where it
    ends, not what it is — which is why the pointer rule reads the opening
    sentence rather than hunting the whole section for a cross-reference."""
    html = """
    <html><body>
    <h3>ITEM 3 - LEGAL PROCEEDINGS</h3>
    <p>We are the subject of various pending or threatened legal actions in the
    ordinary course of our business. All such matters are subject to many
    uncertainties and outcomes that are not predictable with assurance. In our
    opinion, there were no claims or litigation pending as of May 31, 2026,
    that were reasonably likely to have a material adverse effect on our
    consolidated financial position. See the information contained under the
    section entitled "Environmental Matters" in Item 1 of this report for a
    discussion of environmental matters.</p>
    </body></html>
    """
    assert "item_3" in _sections(html, "10-K")

  def test_a_cross_reference_table_is_not_a_section(self):
    """BP and Unilever write their annual report to their own plan and satisfy
    the form with a table at the back mapping each Item to a page range. An
    Item heading found only there heads an address, and a 400-character table
    of page numbers indexed as "MD&A" answers a question with a lookup."""
    html = """
    <html><body>
    <table>
    <tr><td>Item 5.</td><td>Operating and Financial Review and Prospects</td><td></td></tr>
    <tr><td>A.</td><td>Operating results</td><td>6-9, 12-13, 18-27, 62-66</td></tr>
    <tr><td>B.</td><td>Liquidity and capital resources</td><td>159, 196, 211-219</td></tr>
    <tr><td>C.</td><td>Research and development, patents and licences</td><td>12, 189</td></tr>
    <tr><td>Item 6.</td><td>Directors, Senior Management and Employees</td><td></td></tr>
    </table>
    </body></html>
    """
    assert "item_5" not in _sections(html, "20-F")

  def test_a_long_section_that_cites_an_exhibit_is_not_a_pointer(self):
    html = f"""
    <html><body>
    <h3>ITEM 13. CERTAIN RELATIONSHIPS AND RELATED TRANSACTIONS</h3>
    <p>The related-party agreements are incorporated by reference as Exhibit
    10.1. {_FILLER} {_FILLER}</p>
    </body></html>
    """
    assert "item_13" in _sections(html, "10-K")

  def test_the_added_items_do_not_move_the_sections_that_were_there_before(self):
    """Boundaries come from every Item heading in the document, not from the
    target map, so widening the map cannot shift a section that already
    extracted."""
    from xbrlkit.text.narrative import SECTIONS_10K, _build_boundary_list
    from xbrlkit.text.narrative import _find_item_sections, _html_to_text

    text = _html_to_text(SAMPLE_10K_HTML)
    boundaries = _build_boundary_list(text)
    before = {"1": SECTIONS_10K["1"], "1A": SECTIONS_10K["1A"], "7": SECTIONS_10K["7"]}
    narrow = _find_item_sections(text, before, boundaries)
    wide = _find_item_sections(text, SECTIONS_10K, boundaries)
    for item, info in narrow.items():
      assert wide[item]["start"] == info["start"]


SAMPLE_20F_HTML = f"""
<html><body>
<h3>ITEM 3. KEY INFORMATION</h3>
<p>D. Risk Factors. Our operations in emerging markets expose us to currency risk.
{_FILLER}</p>
<h3>ITEM 4. INFORMATION ON THE COMPANY</h3>
<p>The Group refines and markets petroleum products across forty countries.
{_FILLER}</p>
<h3>ITEM 5. OPERATING AND FINANCIAL REVIEW AND PROSPECTS</h3>
<p>Revenue rose on higher realised prices and improved refining margins.
{_FILLER}</p>
<h3>ITEM 11. QUANTITATIVE AND QUALITATIVE DISCLOSURES ABOUT MARKET RISK</h3>
<p>We are exposed to commodity price risk, which we hedge with futures.
{_FILLER}</p>
<h3>ITEM 16K. CYBERSECURITY</h3>
<p>The Board reviews the Group's cybersecurity risk management programme.
{_FILLER}</p>
</body></html>
"""


@pytest.mark.unit
class Test20F:
  def test_a_20f_is_read_through_its_own_item_numbers(self):
    sections = _sections(SAMPLE_20F_HTML, "20-F")
    assert {"item_3", "item_4", "item_5", "item_11", "item_16k"} <= set(sections)

  def test_the_labels_cross_the_forms_where_the_numbers_do_not(self):
    """A 20-F files under Item 5 what a 10-K files under Item 7. The id is the
    form's own; the label is what a search can hold onto."""
    sections = _sections(SAMPLE_20F_HTML, "20-F")
    assert sections["item_5"].section_label == "MD&A"
    assert sections["item_3"].section_label == "Risk Factors"
    assert sections["item_11"].section_label == "Market Risk"

  def test_a_20f_is_not_read_through_the_10k_map(self):
    """Item 4 of a 20-F is the business; Item 4 of a 10-K is mine safety. Read
    through the 10-K map this filing reported no sections at all — the old
    branch did exactly that."""
    sections = _sections(SAMPLE_20F_HTML, "20-F")
    assert sections["item_4"].section_label == "Business"
    assert "item_7" not in sections

  def test_a_40f_reports_nothing_rather_than_something_wrong(self):
    """An MJDS wrapper carries its substance in the Canadian AIF and MD&A it
    files as exhibits, so it has no Item 1 or Item 7 to find."""
    assert NarrativeExtractor().extract(SAMPLE_10K_HTML, "40-F") == []
