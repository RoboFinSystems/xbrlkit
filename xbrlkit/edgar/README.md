# EDGAR — the SEC fetch layer

Discovery and download for the SEC, exposed for hosts that do their own:
synchronous `requests`, local-filesystem output, and EDGAR's two throttle
signatures — a 429, and an empty 200 — ridden out with a bounded
wait-and-retry (`EdgarThrottled` when the budget is spent).

| | |
| --- | --- |
| `EdgarClient` | ticker → CIK, a company's filing list (`list_filings`, by form), `company_info`, one filing by accession (`get_filing_ref`) |
| `EftsClient` / `query_efts` | bulk discovery through EDGAR full-text search: by form, year or quarter, across every filer |
| `download_filing` / `fetch` | the XBRL zip for one accession, unpacked to a directory |
| `download_primary_document` | the readable primary document, which for a classic filing is a *sibling* of the XBRL package |
| `filing_index.py` | the filing's document table — types, descriptions, sizes, URLs |
| `submission.py` | the complete submission as SGML, for the years EDGAR wrote no separate files |

```python
from pathlib import Path
from xbrlkit.edgar import EdgarClient, download_filing

client = EdgarClient()
cik = client.ticker_to_cik("MMM")
latest = client.list_filings(cik, forms=["10-K"])[0]
package = download_filing(client, cik, latest.accession, Path("data"))  # the Arelle load target
```

The SEC **User-Agent** is required here as everywhere: set
`SEC_GOV_USER_AGENT="Your Name your@email.com"` or pass `--user-agent`.

## What EDGAR actually holds

By count EDGAR is not an XBRL corpus. Ownership forms alone are 49% of one
filer's history and 85% of another's; XBRL is 10–30%. Three kinds of filing
load, and `describe_filing`'s `profile` says which one you have — see
[`serve/`](../serve/README.md#three-kinds-of-filing).

Three things about EDGAR shape how this reads a filing, and each was a bug
before it was a feature:

**A filing is a *set* of documents, and the primary one is not always where the
content is.** An 8-K is boilerplate with the press release attached as
`EX-99.1`; a 13F-HR's primary document is a 2 KB cover page whose holdings are
every one of them in a second document. `other_documents()` lists what else was
filed — one small fetch of the index page, and only when asked.

Everything is listed, **including what this cannot read**, with the URL it
lives at. A PDF annual report and a chart filed as an image are content; that
they are not HTML or XML is a fact about this reader, not about the filing, and
the caller asking may well be able to open one.

**Before about 2000 EDGAR wrote no separate files at all.** A filing is one
SGML stream: its documents have types and sequence numbers but no names, and
the filing index lists them with an empty Document column because there is
nothing to link to. Those filings load by splitting the complete submission —
sequence 1 is the primary document, the rest become its other documents — so
1994 onward reads like anything else.

**A classic (pre-inline) filing's narrative lives outside its XBRL package.**
`form10-k.htm` is a *sibling* of the instance, never a member of the
`-xbrl.zip`, so the document is fetched alongside and the instance's tagged
blocks are located within it by matching their prose. Without that, every
filing before iXBRL reads as tagged blocks alone: no Items, no MD&A, no cover
page.

Filings from outside the SEC are [`filings_org/`](../filings_org/README.md).
