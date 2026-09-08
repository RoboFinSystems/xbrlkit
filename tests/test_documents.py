"""Tests for the document layer — the filings that are not an XBRL instance.

Three things are checked here, all offline:

* the **XML lane**, which reads any of the SEC's XML forms (ownership, 13F,
  N-PORT) into fields and record tables without knowing which form it is;
* the **classic reconciliation**, which locates an instance's tagged text
  blocks inside a separate primary document that does not mark them up;
* **document-only loading**, where a filing with no XBRL at all is still a
  filing the server holds and the text tools read.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from xbrlkit.model import Concept, EntityIdentity, FilingMeta, XbrlFact, XbrlModel
from xbrlkit.serve import FilingSession, SourceError, build_text
from xbrlkit.serve import tools
from xbrlkit.text.xml import (
  is_rendered_path,
  parse_xml_document,
  raw_document_name,
  render,
)

FORM4 = """<?xml version="1.0"?>
<ownershipDocument>
  <schemaVersion>X0609</schemaVersion>
  <documentType>4</documentType>
  <periodOfReport>2026-09-01</periodOfReport>
  <issuer>
    <issuerCik>0001522767</issuerCik>
    <issuerName>MARIMED INC.</issuerName>
    <issuerTradingSymbol>MRMD</issuerTradingSymbol>
    <issuerForeignTradingSymbol/>
  </issuer>
  <reportingOwner>
    <reportingOwnerId>
      <rptOwnerCik>0001866577</rptOwnerCik>
      <rptOwnerName>Shaw Timothy</rptOwnerName>
    </reportingOwnerId>
  </reportingOwner>
  <nonDerivativeTable>
    <nonDerivativeTransaction>
      <securityTitle><value>Common stock</value></securityTitle>
      <transactionDate><value>2026-09-01</value></transactionDate>
      <transactionAmounts>
        <transactionShares><value>65000</value></transactionShares>
        <transactionPricePerShare>
          <value>0</value>
          <footnoteId id="F1"/>
        </transactionPricePerShare>
      </transactionAmounts>
    </nonDerivativeTransaction>
  </nonDerivativeTable>
  <footnotes>
    <footnote id="F1">RSUs convert on a one-for-one basis.</footnote>
  </footnotes>
</ownershipDocument>
"""

# The note as the document lays it out. A classic filing's instance carries
# this same markup, escaped, as the value of its text block — the two are one
# note rendered twice, which is the thing the reconciliation has to bridge.
NOTE_8 = """<p>NOTE 8 NOTES RECEIVABLE</p>
<p>At December 31, 2018 and 2017, notes receivable were comprised of the
following:</p>
<table><tr><td></td><td>2018</td><td>2017</td></tr>
<tr><td>First State Compassion Center</td><td>578,723</td><td>624,275</td></tr>
</table>"""

# A classic filing's narrative: no `ix:` markup anywhere, and the note the
# instance tagged appears here as ordinary prose followed by its table.
CLASSIC_10K = (
  """<html><body>
<p>FORM 10-K</p>
<p>Item 1. Business</p>
<p>The Company grows and sells widgets in several states, and licenses its
brands to others. It was incorporated in Delaware.</p>
<p>Item 7. Management's Discussion and Analysis</p>
<p>Revenue grew on widget volume during the year under review.</p>
"""
  + NOTE_8
  + """
</body></html>
"""
)


def _text_block_model(value: str) -> XbrlModel:
  """A model whose only content is one tagged text block."""
  qname = "us-gaap:LoansNotesTradeAndOtherReceivablesDisclosureTextBlock"
  return XbrlModel(
    filing=FilingMeta(
      accession="0000000000-19-000001",
      cik="0001522767",
      form="10-K",
      report_date=date(2018, 12, 31),
      is_inline_xbrl=False,
    ),
    entity=EntityIdentity(cik="0001522767", name="MariMed Inc."),
    concepts={
      qname: Concept(
        qname=qname,
        namespace="http://fasb.org/us-gaap/2018-01-31",
        name="LoansNotesTradeAndOtherReceivablesDisclosureTextBlock",
        period_type="duration",
        is_numeric=False,
        is_textblock=True,
        item_type="textBlockItemType",
        pref_label="Notes Receivable",
      )
    },
    facts=[
      XbrlFact(
        id="f1",
        concept_qname=qname,
        period_id="D-2018",
        entity_cik="0001522767",
        value_str=value,
        value_kind="text",
      )
    ],
  )


# -- the XML lane ----------------------------------------------------------------


def test_edgar_rendered_path_resolves_to_the_submitted_xml() -> None:
  rendered = "xslF345X06/wk-form4_1788293749.xml"
  assert is_rendered_path(rendered)
  assert raw_document_name(rendered) == "wk-form4_1788293749.xml"
  # A document that is already the submitted one is left alone.
  assert raw_document_name("form8-k.htm") == "form8-k.htm"
  assert not is_rendered_path("form8-k.htm")


def test_ownership_xml_reads_as_fields_and_records() -> None:
  doc = parse_xml_document(FORM4)
  assert doc.root == "ownershipDocument"
  assert doc.form_hint == "4"
  # `value` wrappers collapse; empty elements are left out.
  assert doc.fields["issuer.issuerName"] == "MARIMED INC."
  assert "issuer.issuerForeignTradingSymbol" not in doc.fields
  tables = {t.name: t for t in doc.tables}
  # One transaction under a `*Table` container is still a record table.
  assert tables["nonDerivativeTransaction"].rows == [
    {
      "securityTitle": "Common stock",
      "transactionDate": "2026-09-01",
      "transactionAmounts.transactionShares": "65000",
      "transactionAmounts.transactionPricePerShare": "0",
      "transactionAmounts.transactionPricePerShare.footnoteId@id": "F1",
    }
  ]
  # A footnote is text under an attribute — both are columns.
  assert tables["footnote"].rows == [
    {"@id": "F1", "text": "RSUs convert on a one-for-one basis."}
  ]


def test_xml_renders_every_value_so_it_can_be_searched() -> None:
  text = render(parse_xml_document(FORM4))
  for value in ("MARIMED INC.", "Shaw Timothy", "65000", "one-for-one"):
    assert value in text


# -- the classic reconciliation ---------------------------------------------------


def test_classic_blocks_locate_in_a_document_that_does_not_tag_them() -> None:
  """The instance's block and the document's prose are two renderings of the
  same note; the block has to be found in the document by its words alone."""
  model = _text_block_model(NOTE_8)
  text, sections = build_text(model, CLASSIC_10K)
  blocks = [s for s in sections if s.kind == "text_block"]
  assert len(blocks) == 1
  # Located, and located at the note — not at the Item 1 heading above it.
  assert blocks[0].offset is not None
  assert text[blocks[0].offset :].startswith("NOTE 8 NOTES RECEIVABLE")
  # The document is the text, not the block: the Items came with it.
  assert {s.id for s in sections if s.kind == "item"} == {"item_1", "item_7"}
  assert "licenses its" in text


def test_a_block_is_matched_on_its_prose_not_into_its_table() -> None:
  """A note's twelfth word is often already inside its table, where the two
  renderings disagree; matching stops at the table so the block still lands."""
  model = _text_block_model(
    "<p>At December 31, 2018 and 2017, notes receivable were comprised of the "
    "following:</p><table><tr><td>First</td><td>State</td><td>Compassion</td>"
    "<td>Center</td></tr></table>"
  )
  _text, sections = build_text(model, CLASSIC_10K)
  assert [s.offset for s in sections if s.kind == "text_block"] != [None]


def test_without_a_document_the_blocks_are_the_text() -> None:
  model = _text_block_model("<p>" + "word " * 40 + "</p>")
  text, sections = build_text(model, None)
  assert text.startswith("## us-gaap:LoansNotes")
  assert [s.kind for s in sections] == ["text_block"]


# -- document-only filings ---------------------------------------------------------


def test_html_document_loads_with_no_xbrl_at_all(tmp_path: Path) -> None:
  path = tmp_path / "form10-k.htm"
  path.write_text(CLASSIC_10K)
  session = FilingSession()
  try:
    lf = session.load(str(path))
    assert lf.has_xbrl is False
    assert lf.has_document is True
    # The form is read off the cover, so the Items are still found.
    assert lf.model.filing.form == "10-K"
    assert {s.id for s in lf.sections} == {"item_1", "item_7"}
    assert tools.describe_filing(lf)["profile"]["xbrl"] is False
  finally:
    session.close()


def test_xml_document_takes_its_identity_from_the_form(tmp_path: Path) -> None:
  path = tmp_path / "wk-form4.xml"
  path.write_text(FORM4)
  session = FilingSession()
  try:
    lf = session.load(str(path))
    assert lf.has_xbrl is False and lf.xml_document is not None
    assert lf.model.filing.form == "4"
    assert lf.model.filing.report_date == date(2026, 9, 1)
    assert lf.model.entity.cik == "0001522767"
    assert lf.model.entity.ticker == "MRMD"
    described = tools.describe_filing(lf)
    assert [r["name"] for r in described["sections"]["records"]] == [
      "nonDerivativeTransaction",
      "footnote",
    ]
    assert described["next"][0].startswith("records")
  finally:
    session.close()


def test_records_returns_the_rows_and_names_the_tables(tmp_path: Path) -> None:
  path = tmp_path / "wk-form4.xml"
  path.write_text(FORM4)
  session = FilingSession()
  try:
    lf = session.load(str(path))
    out = tools.records(lf, table="nonDerivativeTransaction")
    assert out["form"] == "4"
    assert out["tables"][0]["row_count"] == 1
    assert out["fields"]["issuer.issuerTradingSymbol"] == "MRMD"
    with pytest.raises(tools.ToolError, match="nonDerivativeTransaction"):
      tools.records(lf, table="holdings")
  finally:
    session.close()


def test_the_xbrl_tools_say_when_a_filing_has_no_xbrl(tmp_path: Path) -> None:
  path = tmp_path / "wk-form4.xml"
  path.write_text(FORM4)
  session = FilingSession()
  try:
    lf = session.load(str(path))
    for call in (
      lambda: tools.fact_grid(lf, ["Revenues"]),
      lambda: tools.statement(lf, "balance sheet"),
      lambda: tools.calculation(lf, "Assets"),
      lambda: tools.resolve_element(lf, "revenue"),
    ):
      with pytest.raises(tools.ToolError, match="carries no XBRL"):
        call()
    # And the text tools still work, which is the point.
    assert tools.search_text(lf, "Shaw")["total"] == 1
  finally:
    session.close()


def test_a_pdf_filing_is_named_rather_than_loaded_empty() -> None:
  from xbrlkit.edgar import FilingRef

  ref = FilingRef(
    cik="0001522767",
    accession="0001522767-20-000001",
    form="ARS",
    filing_date="2020-04-01",
    primary_document="annual-report.pdf",
    is_inline=False,
    is_xbrl=False,
  )
  session = FilingSession()
  try:
    # The refusal is decided before anything is fetched, so no client is used.
    with pytest.raises(SourceError, match=r"\.pdf document"):
      session._load_document_only(None, "1522767", ref.accession, ref, "src")
  finally:
    session.close()


def test_filing_refs_carry_whether_edgar_holds_xbrl() -> None:
  from xbrlkit.edgar.client import EdgarClient

  refs = EdgarClient._refs_from_arrays(
    "0001522767",
    {
      "accessionNumber": ["0001-19-000001", "0002-26-000002"],
      "form": ["10-K", "4"],
      "isInlineXBRL": [0, 0],
      "isXBRL": [1, 0],
      "primaryDocument": ["form10-k.htm", "xslF345X06/wk-form4.xml"],
    },
  )
  assert [r.is_xbrl for r in refs] == [True, False]
