"""The 8-K item numbers — what a current report is *about*, said structurally.

An 8-K is a wrapper. Its primary document is a page of boilerplate carrying a
handful of cover-page facts, and the substance is an exhibit hanging off it:
the press release, the agreement, the presentation. So "what is in this 8-K?"
is not answerable from the tagged content, and the form type does not narrow it
either — every 8-K looks the same from the outside.

**The item numbers are the answer, and EDGAR publishes them.** The submissions
record carries an ``items`` field per filing (``"2.02,9.01"``), and Item 2.02 —
*Results of Operations and Financial Condition* — is the SEC's own marker for
an earnings release. A filer who reports quarterly results by press release
codes it 2.02, always, because the rule requires it.

That makes an earnings 8-K identifiable **before** anything is fetched or read.
It matters because the numbers a company leads with — adjusted EBITDA,
non-GAAP margin, segment colour, guidance — appear in that exhibit and nowhere
in any XBRL, and they arrive weeks before the 10-Q that eventually restates
part of them in GAAP.
"""

from __future__ import annotations

# Items as the SEC numbers them (Form 8-K, General Instruction B). Names are
# shortened from the official captions to what a reader needs at a glance.
EIGHT_K_ITEMS: dict[str, str] = {
  "1.01": "Entry into a Material Definitive Agreement",
  "1.02": "Termination of a Material Definitive Agreement",
  "1.03": "Bankruptcy or Receivership",
  "1.04": "Mine Safety — Reporting of Shutdowns and Patterns of Violations",
  "1.05": "Material Cybersecurity Incidents",
  "2.01": "Completion of Acquisition or Disposition of Assets",
  "2.02": "Results of Operations and Financial Condition",
  "2.03": "Creation of a Direct Financial Obligation",
  "2.04": "Triggering Events That Accelerate a Financial Obligation",
  "2.05": "Costs Associated with Exit or Disposal Activities",
  "2.06": "Material Impairments",
  "3.01": "Notice of Delisting or Failure to Satisfy a Listing Rule",
  "3.02": "Unregistered Sales of Equity Securities",
  "3.03": "Material Modification to Rights of Security Holders",
  "4.01": "Changes in Registrant's Certifying Accountant",
  "4.02": "Non-Reliance on Previously Issued Financial Statements",
  "5.01": "Changes in Control of Registrant",
  "5.02": "Departure or Election of Directors and Officers",
  "5.03": "Amendments to Articles or Bylaws; Change in Fiscal Year",
  "5.04": "Temporary Suspension of Trading Under Employee Benefit Plans",
  "5.05": "Amendment to Code of Ethics, or Waiver of a Provision",
  "5.06": "Change in Shell Company Status",
  "5.07": "Submission of Matters to a Vote of Security Holders",
  "5.08": "Shareholder Director Nominations",
  "6.01": "ABS Informational and Computational Material",
  "6.02": "Change of Servicer or Trustee",
  "6.03": "Change in Credit Enhancement or External Support",
  "6.04": "Failure to Make a Required Distribution",
  "6.05": "Securities Act Updating Disclosure",
  "7.01": "Regulation FD Disclosure",
  "8.01": "Other Events",
  "9.01": "Financial Statements and Exhibits",
}

# The item that says "this is an earnings release".
EARNINGS_ITEM = "2.02"
# Regulation FD — the other one whose substance is almost always an exhibit
# (an investor presentation, a script, a slide deck).
FD_ITEM = "7.01"
# Says an exhibit exists, without saying what it is.
EXHIBITS_ITEM = "9.01"


def parse_items(raw: str | None) -> list[str]:
  """EDGAR's ``items`` field (``"2.02,9.01"``) as a list of codes.

  Tolerant of the shapes EDGAR has used: extra whitespace, a trailing comma,
  and the older captioned form (``"Item 2.02"``), which appears in some
  historical records.
  """
  if not raw:
    return []
  codes: list[str] = []
  for part in str(raw).replace(";", ",").split(","):
    code = part.strip().removeprefix("Item").strip()
    # A captioned entry carries the name after the number; keep the number.
    code = code.split(" ", 1)[0].strip().rstrip(".:")
    if code and code not in codes:
      codes.append(code)
  return codes


def describe_items(codes: list[str] | None) -> list[dict[str, str]]:
  """Each item code with its caption, unknown codes included as themselves."""
  return [{"item": code, "name": EIGHT_K_ITEMS.get(code, "")} for code in (codes or [])]


def is_earnings_release(codes: list[str] | None) -> bool:
  """Whether this filing is coded as reporting results — Item 2.02."""
  return EARNINGS_ITEM in (codes or [])


def items_note(codes: list[str] | None) -> str | None:
  """What a reader should do next, given what the filing is coded as.

  An 8-K's tagged content is its cover page; the substance is the exhibit. The
  note says so, and says it most strongly where the exhibit is the earnings
  release — the numbers a company leads with, which no XBRL anywhere carries.
  """
  codes = codes or []
  if not codes:
    return None
  if is_earnings_release(codes):
    return (
      "Item 2.02 — this is an earnings release. The results are in the "
      "attached exhibit (usually EX-99.1), not in this filing's XBRL, which "
      "is the cover page only. Call documents, then read_document on the "
      "EX-99.1. The exhibit carries what the company leads with — non-GAAP "
      "measures, adjusted EBITDA, segment detail and guidance — which no XBRL "
      "holds, and it arrives weeks before the 10-Q that restates part of it."
    )
  if FD_ITEM in codes:
    return (
      "Item 7.01 — Regulation FD disclosure. The substance is normally an "
      "exhibit (a presentation, a script, a release): call documents, then "
      "read_document."
    )
  if EXHIBITS_ITEM in codes:
    return (
      "This 8-K has exhibits (Item 9.01) and an 8-K's tagged content is its "
      "cover page, so the substance is in them: call documents, then "
      "read_document."
    )
  return (
    "An 8-K's tagged content is its cover page; anything more is in the "
    "documents filed with it. Call documents to see them."
  )
