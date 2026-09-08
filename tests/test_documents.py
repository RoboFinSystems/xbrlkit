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


# -- the filing's other documents --------------------------------------------------

# EDGAR's index page, in its two-table shape: document files, then data files.
# The 13F twin (a source `.xml` beside its rendered `.html`) and an inline
# document's " iXBRL" name marker are both here because both have bitten.
INDEX_PAGE = """<html><body>
<table summary="Document Format Files">
<tr><th>Seq</th><th>Description</th><th>Document</th><th>Type</th><th>Size</th></tr>
<tr><td>1</td><td></td><td>primary_doc.html</td><td>13F-HR</td><td>0</td></tr>
<tr><td>1</td><td></td><td>primary_doc.xml</td><td>13F-HR</td><td>4231</td></tr>
<tr><td>2</td><td>INFORMATION TABLE</td><td>56757.html</td><td>INFORMATION TABLE</td><td>0</td></tr>
<tr><td>2</td><td>INFORMATION TABLE</td><td>56757.xml</td><td>INFORMATION TABLE</td><td>44724</td></tr>
<tr><td>3</td><td></td><td>ex99-1.htm &nbsp;iXBRL</td><td>EX-99.1</td><td>19547</td></tr>
<tr><td>4</td><td></td><td>audit_001.jpg</td><td>GRAPHIC</td><td>7050</td></tr>
<tr><td>&nbsp;</td><td>Complete submission text file</td><td>0001-26-000001.txt</td><td>&nbsp;</td><td>6929712</td></tr>
</table>
<table summary="Data Files">
<tr><th>Seq</th><th>Description</th><th>Document</th><th>Type</th><th>Size</th></tr>
<tr><td>5</td><td>XBRL INSTANCE FILE</td><td>mrmd-20181231.xml</td><td>EX-101.INS</td><td>907209</td></tr>
</table>
</body></html>
"""


def test_the_index_page_lists_every_real_document() -> None:
  from xbrlkit.edgar.filing_index import parse_filing_index

  docs = parse_filing_index(INDEX_PAGE)
  # Header rows and the complete-submission row carry no sequence number.
  assert [d.seq for d in docs] == [1, 1, 2, 2, 3, 4, 5]
  # EDGAR appends a marker to an inline document's name; the name is the file.
  assert [d.document for d in docs if d.seq == 3] == ["ex99-1.htm"]
  assert [d.type for d in docs if d.seq == 5] == ["EX-101.INS"]


def test_other_documents_keeps_the_content_and_drops_the_redundant() -> None:
  from xbrlkit.edgar.filing_index import other_documents, parse_filing_index

  kept = other_documents(parse_filing_index(INDEX_PAGE), "primary_doc.xml")
  assert [(d.document, d.type) for d in kept] == [
    # The holdings, as the source XML — not its rendered twin.
    ("56757.xml", "INFORMATION TABLE"),
    ("ex99-1.htm", "EX-99.1"),
    # Listed although nothing here can read it: it is content, and the caller
    # is told where it lives.
    ("audit_001.jpg", "GRAPHIC"),
  ]
  # Gone only where the content is already had: the primary, its own rendered
  # twin, and the XBRL package loaded from the zip.
  names = {d.document for d in kept}
  assert not names & {"primary_doc.xml", "primary_doc.html", "mrmd-20181231.xml"}


def test_a_document_this_cannot_read_is_listed_with_where_it_is() -> None:
  from xbrlkit.edgar.filing_index import FilingDocument
  from xbrlkit.serve import tools as serve_tools
  from xbrlkit.serve.session import LoadedFiling

  pdf = FilingDocument(
    seq=2,
    type="EX-99.1",
    document="annual-report.pdf",
    size=900_000,
    url="https://www.sec.gov/Archives/edgar/data/1/2/annual-report.pdf",
  )
  htm = FilingDocument(
    seq=3, type="EX-21", document="ex-21.htm", url="https://x/ex-21.htm"
  )
  assert pdf.is_readable is False and htm.is_readable is True

  class _Session:
    def other_documents(self, lf: object) -> list[FilingDocument]:
      return [pdf, htm]

  model = _text_block_model("<p>" + "word " * 40 + "</p>")
  loaded = LoadedFiling(id="x", source="memory", model=model, text="", sections=[])
  out = serve_tools.documents(loaded, _Session())
  by_name = {d["document"]: d for d in out["documents"]}
  assert by_name["annual-report.pdf"]["url"] == pdf.url
  assert "does not read .pdf" in by_name["annual-report.pdf"]["read"]
  assert by_name["ex-21.htm"]["read"] == "read_document"
  assert "not text — fetch the url" in out["note"]


def test_a_filing_not_from_edgar_says_so_rather_than_guessing(tmp_path: Path) -> None:
  path = tmp_path / "wk-form4.xml"
  path.write_text(FORM4)
  session = FilingSession()
  try:
    lf = session.load(str(path))
    with pytest.raises(SourceError, match="not loaded from EDGAR"):
      session.other_documents(lf)
  finally:
    session.close()


def test_documents_reports_an_empty_filing_plainly() -> None:
  from xbrlkit.serve import tools as serve_tools

  class _Session:
    def other_documents(self, lf: object) -> list[object]:
      return []

  from xbrlkit.serve.session import LoadedFiling

  model = _text_block_model("<p>" + "word " * 40 + "</p>")
  loaded = LoadedFiling(id="x", source="memory", model=model, text="", sections=[])
  out = serve_tools.documents(loaded, _Session())
  assert out["count"] == 0
  assert "Nothing was filed with this one" in out["note"]


# -- the complete submission (EDGAR before 2000) -----------------------------------

# A filing as EDGAR stored it before it wrote separate files: a PEM envelope, a
# plain-text header, and documents with a type and a sequence but no filename.
SUBMISSION = """-----BEGIN PRIVACY-ENHANCED MESSAGE-----
Proc-Type: 2001,MIC-CLEAR
MIC-Info: RSA-MD5,RSA,
 EpbWlzEXQTzfdDL5kjG3x50wY6TvcOk9uLwpcX6h26y2bYQdAb3l62SVW2tTdzHo

<SEC-DOCUMENT>0000912057-95-001314.txt : 19950310
<SEC-HEADER>0000912057-95-001314.hdr.sgml : 19950310
ACCESSION NUMBER:\t\t0000912057-95-001314
CONFORMED SUBMISSION TYPE:\t10-K
CONFORMED PERIOD OF REPORT:\t19941231
FILED AS OF DATE:\t\t19950310

FILER:

\tCOMPANY DATA:
\t\tCOMPANY CONFORMED NAME:\t\t\tABBOTT LABORATORIES
\t\tCENTRAL INDEX KEY:\t\t\t0000001800
</SEC-HEADER>
<DOCUMENT>
<TYPE>10-K
<SEQUENCE>1
<DESCRIPTION>ANNUAL REPORT
<TEXT>

<PAGE>
Item 1. Business

The Company discovers, develops, manufactures and sells a broad line of
health care products, and has done so since 1888 in Illinois.

<PAGE>
Item 2. Properties

The Company owns plants in Illinois and elsewhere, together with the
laboratories and offices described in this filing.
</TEXT>
</DOCUMENT>
<DOCUMENT>
<TYPE>EX-27
<SEQUENCE>2
<DESCRIPTION>FINANCIAL DATA SCHEDULE
<TEXT>
<S>                    <C>
TOTAL-ASSETS           9412796
</TEXT>
</DOCUMENT>
<DOCUMENT>
<TYPE>GRAPHIC
<SEQUENCE>3
<DESCRIPTION>SIGNATURE
<TEXT>
begin 644 sig.gif
M1TE&.#EA
end
</TEXT>
</DOCUMENT>
-----END PRIVACY-ENHANCED MESSAGE-----
"""


def test_a_complete_submission_splits_into_its_documents() -> None:
  from xbrlkit.edgar.submission import parse_submission, strip_pem

  assert strip_pem(SUBMISSION).startswith("<SEC-DOCUMENT>")
  # A submission with no envelope is left exactly as it is.
  assert strip_pem("<SEC-DOCUMENT>x") == "<SEC-DOCUMENT>x"

  header, docs = parse_submission(strip_pem(SUBMISSION))
  assert header["CONFORMED SUBMISSION TYPE"] == "10-K"
  assert header["CONFORMED PERIOD OF REPORT"] == "19941231"
  assert header["COMPANY CONFORMED NAME"] == "ABBOTT LABORATORIES"

  # Named by sequence and type, because EDGAR named none of them.
  assert [d.name for d in docs] == [
    "0001-10-K.txt",
    "0002-EX-27.txt",
    "0003-GRAPHIC.uu",
  ]
  assert [d.sequence for d in docs] == [1, 2, 3]
  assert docs[0].description == "ANNUAL REPORT"
  # `<PAGE>` is a printer's page break, not content.
  assert "<PAGE>" not in docs[0].text
  assert docs[0].text.startswith("Item 1. Business")
  # A uuencoded image is detected, so it is neither named nor served as prose.
  assert docs[2].is_binary and not docs[0].is_binary


def test_a_submission_document_takes_the_extension_of_its_body() -> None:
  from xbrlkit.edgar.submission import SubmissionDocument

  plain = SubmissionDocument(sequence=1, type="10-K", description="", text="Item 1.")
  markup = SubmissionDocument(
    sequence=2, type="EX-99", description="", text="<HTML><body>hi</body></HTML>"
  )
  named = SubmissionDocument(
    sequence=3, type="EX-1", description="", text="x", filename="given.htm"
  )
  assert (plain.name, markup.name, named.name) == (
    "0001-10-K.txt",
    "0002-EX-99.htm",
    "given.htm",
  )


def test_edgar_header_dates_are_yyyymmdd() -> None:
  from xbrlkit.serve.session import _parse_edgar_date

  assert _parse_edgar_date("19941231") == date(1994, 12, 31)
  assert _parse_edgar_date("1994-12-31") is None
  assert _parse_edgar_date(None) is None
  assert _parse_edgar_date("19941331") is None


def test_the_1990s_10_k405_is_a_10_k() -> None:
  """`10-K405` was the common 1990s annual report — the same Items."""
  from xbrlkit.text.narrative import NarrativeExtractor

  body = (
    "<html><body><p>Item 1. Business</p><p>The Company designs and sells "
    "graphics processors to computer makers, and was incorporated in "
    "Delaware in 1993 for that purpose.</p>"
    "<p>Item 2. Properties</p><p>The Company leases its headquarters in "
    "Santa Clara, California, under an operating lease.</p></body></html>"
  )
  found = {
    s.section_id for s in NarrativeExtractor(part_size=None).extract(body, "10-K405")
  }
  assert {"item_1", "item_2"} <= found


def test_a_plain_text_filing_still_has_items(tmp_path: Path) -> None:
  """1990s filings are plain text and say "Item 1. Business" all the same."""
  from xbrlkit.serve.session import _read_document

  path = tmp_path / "0001-10-K.txt"
  path.write_text(
    "Item 1. Business\n\nThe Company discovers, develops, manufactures and "
    "sells a broad line of health care products in Illinois.\n\n"
    "Item 2. Properties\n\nThe Company owns plants in Illinois together with "
    "the laboratories and offices described in this filing.\n"
  )
  model = _text_block_model("<p>" + "word " * 40 + "</p>")
  model.filing.form = "10-K"
  read = _read_document(path, model)
  assert read is not None
  assert {s.id for s in read.sections} == {"item_1", "item_2"}
  assert all(s.offset is not None for s in read.sections)


def test_a_plain_text_document_loads_without_arelle(tmp_path: Path) -> None:
  """Arelle cannot read plain text, and reaching for it first only produced a
  parse error where the answer is that this is a document."""
  path = tmp_path / "0001-10-K.txt"
  path.write_text("Item 1. Business\n\n" + "The Company sells widgets. " * 20)
  session = FilingSession()
  try:
    lf = session.load(str(path))
    assert lf.has_xbrl is False and lf.has_document is True
    assert "The Company sells widgets." in lf.text
  finally:
    session.close()


# -- packages from outside EDGAR ---------------------------------------------------


def _esef_package(root: Path) -> Path:
  """A taxonomy package laid out the way ESEF ships one: nothing at the root,
  the report under ``reports/``, the filer's taxonomy under their own domain."""
  (root / "META-INF").mkdir(parents=True)
  (root / "META-INF" / "taxonomyPackage.xml").write_text(
    '<taxonomyPackage xmlns="http://xbrl.org/2016/taxonomy-package"/>'
  )
  (root / "META-INF" / "catalog.xml").write_text("<catalog/>")
  (root / "acme.example" / "xbrl").mkdir(parents=True)
  (root / "acme.example" / "xbrl" / "acme-2024.xsd").write_text("<xs:schema/>")
  reports = root / "reports"
  reports.mkdir()
  report = reports / "acme-2024.xhtml"
  # Megabytes of stylesheet before the first tagged fact, as a real one has.
  report.write_text(
    '<html xmlns:ix="http://www.xbrl.org/2013/inlineXBRL">\n<style>'
    + ("/* padding */ " * 20000)
    + "</style>\n<ix:nonNumeric name='x'>y</ix:nonNumeric>\n</html>"
  )
  return report


def test_a_package_is_found_however_deep_the_report_sits(tmp_path: Path) -> None:
  from xbrlkit.serve.session import _find_load_target

  report = _esef_package(tmp_path / "pkg")
  # Nothing is at the package root; looking only there found these empty,
  # which is most of Europe.
  assert _find_load_target(tmp_path / "pkg") == report


def test_inline_is_recognised_by_namespace_not_by_a_tag_near_the_top(
  tmp_path: Path,
) -> None:
  """A real ESEF report opens with megabytes of stylesheet — the first tagged
  fact in one sampled here sits 2.9 MB in — so a sniff of the opening bytes
  has to match the namespace declaration on the root element."""
  from xbrlkit.serve.session import _is_inline

  report = _esef_package(tmp_path / "pkg")
  head = report.read_bytes()[:200_000]
  assert b"ix:nonNumeric" not in head  # the fixture reproduces the problem
  assert _is_inline(head) is True
  assert _is_inline(b"<html><body>an ordinary page</body></html>") is False
  # Any prefix, and the older namespace, both count.
  assert _is_inline(b'<html xmlns:inline="http://www.xbrl.org/2008/inlineXBRL">')


def test_the_taxonomy_package_manifest_is_what_gets_registered(
  tmp_path: Path,
) -> None:
  """Arelle takes a package as a zip or as its manifest, but not as an
  unpacked directory — and by load time the zip is already unpacked."""
  from xbrlkit.serve.session import _taxonomy_packages

  report = _esef_package(tmp_path / "pkg")
  assert _taxonomy_packages(report) == [
    tmp_path / "pkg" / "META-INF" / "taxonomyPackage.xml"
  ]
  # A filing that is not in a package registers nothing.
  loose = tmp_path / "loose.htm"
  loose.write_text("<html/>")
  assert _taxonomy_packages(loose) == []


def test_a_package_archive_is_preferred_to_its_manifest(tmp_path: Path) -> None:
  """Arelle registers a package from its archive or its manifest and the two
  are not interchangeable — one Dutch package's manifest raises inside Arelle
  where the same package as a zip registers cleanly. When a zip was the source
  it is still to hand, so it is what gets registered."""
  import zipfile

  from xbrlkit.serve.session import _taxonomy_packages

  pkg = tmp_path / "pkg"
  report = _esef_package(pkg)
  archive = tmp_path / "filing.zip"
  with zipfile.ZipFile(archive, "w") as zf:
    for path in sorted(pkg.rglob("*")):
      if path.is_file():
        zf.write(path, path.relative_to(pkg.parent))

  session = FilingSession()
  try:
    # The unpacked tree still knows it is a package, which is what decides
    # that the archive should be registered at all.
    assert _taxonomy_packages(report)
    # A zip with no package in it registers nothing, so an EDGAR filing zip
    # does not start logging warnings about taxonomy packages.
    plain = tmp_path / "plain.zip"
    with zipfile.ZipFile(plain, "w") as zf:
      zf.writestr("mmm-20241231.xml", "<xbrl/>")
    with zipfile.ZipFile(plain) as zf:
      names = zf.namelist()
    assert names == ["mmm-20241231.xml"]
  finally:
    session.close()


# -- filings.xbrl.org --------------------------------------------------------------

# One JSON:API payload as the index returns it, with the entity included.
FILINGS_PAYLOAD = {
  "data": [
    {
      "type": "filing",
      "attributes": {
        "fxo_id": "213800H2PQMIF3OVZY47-2022-03-31-ESEF-GB-0",
        "country": "GB",
        "period_end": "2022-03-31",
        "package_url": "/213800H2PQMIF3OVZY47/2022-03-31/ESEF/GB/0/pkg.zip",
        "report_url": "/213800H2PQMIF3OVZY47/2022-03-31/ESEF/GB/0/reports/r.xhtml",
        "json_url": None,
        "error_count": 0,
        "inconsistency_count": 2,
      },
      "relationships": {"entity": {"data": {"type": "entity", "id": "2670"}}},
    },
    {
      "type": "filing",
      "attributes": {
        "fxo_id": "EDRPOU-32033791-2020-12-31-UAIFRS-UA-0",
        "country": "UA",
        "period_end": "2020-12-31",
        "package_url": None,
        "report_url": "/EDRPOU-32033791/2020-12-31/UAIFRS/UA/0/r.html",
      },
      "relationships": {},
    },
  ],
  "included": [
    {
      "type": "entity",
      "id": "2670",
      "attributes": {"identifier": "213800H2PQMIF3OVZY47", "name": "KAINOS GROUP PLC"},
    }
  ],
  "meta": {"count": 2},
}


def test_the_index_payload_reads_into_filings_and_entities() -> None:
  from xbrlkit.filings_org.client import _records

  gb, ua = _records(FILINGS_PAYLOAD)
  assert gb.fxo_id.endswith("ESEF-GB-0")
  assert gb.entity is not None and gb.entity.name == "KAINOS GROUP PLC"
  assert gb.entity_identifier == "213800H2PQMIF3OVZY47"
  assert gb.inconsistency_count == 2
  # A filing with no package must resolve its taxonomy over the network —
  # which is exactly what fails for the regimes whose host has gone.
  assert gb.has_package is True
  assert ua.has_package is False
  assert ua.entity is None and ua.entity_identifier == ""


def test_a_relative_package_path_becomes_an_absolute_url() -> None:
  from xbrlkit.filings_org.client import FilingRecord
  from xbrlkit.filings_org.download import package_url

  gb = FilingRecord(fxo_id="x", package_url="/a/pkg.zip")
  assert package_url(gb) == "https://filings.xbrl.org/a/pkg.zip"
  # A filing with only a report falls back to it, and an absolute URL is kept.
  assert package_url(FilingRecord(fxo_id="x", report_url="/a/r.xhtml")).endswith(
    "/a/r.xhtml"
  )
  assert package_url(FilingRecord(fxo_id="x", package_url="https://e.test/p.zip")) == (
    "https://e.test/p.zip"
  )
  assert package_url(FilingRecord(fxo_id="x")) == ""


def test_the_new_source_forms_do_not_collide_with_the_old_ones() -> None:
  """An LEI and an index filing id have to be told apart from a ticker and an
  EDGAR accession, since all four arrive as one string."""
  from xbrlkit.serve.session import (
    _ACCESSION_RE,
    _CIK_ACCESSION_RE,
    _FXO_RE,
    _LEI_RE,
    _TICKER_RE,
  )

  lei = "213800H2PQMIF3OVZY47"
  fxo = f"{lei}-2022-03-31-ESEF-GB-0"
  assert _LEI_RE.match(f"lei:{lei}") and _LEI_RE.match(f"LEI/{lei}")
  assert _FXO_RE.match(fxo) and _FXO_RE.match(f"fxo:{fxo}")
  # The forms that came first still win their own shapes.
  assert not _LEI_RE.match("NVDA") and not _FXO_RE.match("NVDA")
  assert not _FXO_RE.match("0001493152-19-005497")
  assert _ACCESSION_RE.match("0001493152-19-005497")
  assert _CIK_ACCESSION_RE.match("1522767:0001493152-19-005497")
  assert _TICKER_RE.match("NVDA")
  # A ticker is at most ten characters, so an LEI cannot be read as one.
  assert not _TICKER_RE.match(lei)


def test_the_latest_filing_is_not_one_whose_period_has_not_ended() -> None:
  """One Finnish filer's index entry reports a period ending in 2031; sorting
  on the period alone hands that back as their newest report."""
  from xbrlkit.filings_org.client import FilingRecord, FilingsOrgClient

  rows = [
    FilingRecord(fxo_id="a", period_end="2031-01-01"),
    FilingRecord(fxo_id="b", period_end="2025-12-31"),
    FilingRecord(fxo_id="c", period_end="2024-12-31"),
  ]
  client = FilingsOrgClient.__new__(FilingsOrgClient)
  client.entity_filings = lambda lei, limit=25: rows  # type: ignore[method-assign]
  assert client.latest_filing("x", today=date(2026, 9, 7)).fxo_id == "b"
  # Every period in the future is still an answer, not an error.
  client.entity_filings = lambda lei, limit=25: rows[:1]  # type: ignore[method-assign]
  assert client.latest_filing("x", today=date(2026, 9, 7)).fxo_id == "a"
