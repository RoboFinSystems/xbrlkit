# filings.xbrl.org — the filings that are not the SEC's

[filings.xbrl.org](https://filings.xbrl.org) is XBRL International's open index
of ESEF annual financial reports and the national regimes that publish through
it. It asks for no key and speaks JSON:API, so **one adapter reaches every
country in it** rather than one per national filing authority.

Shaped like [`edgar/`](../edgar/README.md): a paced read-only client over the
index, and a download that fetches a filing's package.

```python
from xbrlkit.filings_org import FilingsOrgClient

client = FilingsOrgClient()
client.entity("213800H2PQMIF3OVZY47")            # KAINOS GROUP PLC
client.entity_filings("213800H2PQMIF3OVZY47")    # newest period first
client.filings(country="FI", limit=25)           # by country
```

```bash
xbrlkit serve lei:213800H2PQMIF3OVZY47           # a filer's latest, by LEI
```

Two source forms, neither of which can be mistaken for a ticker or an EDGAR
accession:

```
lei:213800H2PQMIF3OVZY47                      a filer's latest filing
213800H2PQMIF3OVZY47-2022-03-31-ESEF-GB-0     one filing, by the index's own id
```

## The identifier is the LEI

These filings carry no CIK and there is no EDGAR record to ask, so the entity's
name comes from the index and its scheme is **ISO 17442**. That is what comes
back from `describe_filing`, and it is the honest answer rather than an empty
`cik` field.

## Worth knowing before planning against it

**It is not all of Europe.** Germany files to the Bundesanzeiger, which does
not share, and has nothing in the index.

**Being indexed is not being loadable.** A self-contained ESEF package loads; a
filing that depends on a national taxonomy host loads only while that host
serves it. Ukraine's does not resolve at all, and its filings — over a third of
the index — cannot be loaded.

**Denmark loads without its national tags.** Every Danish report cites the
Danish Business Authority's taxonomy beside the ESEF one, and that host answers
404. The report loads with everything tagged against the taxonomies that
resolved — the IFRS statements — and the load receipt's `missing_taxonomy`
names the documents that did not and counts the facts left in the report.

**A period that has not ended is not the latest filing, it is a mistake.** One
Finnish filer's entry reports a period ending in 2031, so `lei:` passes over
future periods unless every filing has one.
