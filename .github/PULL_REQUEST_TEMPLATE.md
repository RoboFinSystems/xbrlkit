## Summary

<!-- What this PR does and why. Ground it in the actual change, not the diff mechanics. -->

## Changes

<!-- Grouped by layer: edgar/ (fetch), parse/ + model.py (convert), serialize/ (project),
     query.py / cli.py (surface). Note that _vendor/ is vendored third-party code and should
     not be edited here. -->

-

## Output Impact

<!-- Required judgment. The emitted holon.jsonld is consumed by robosystems-holon-viewer and
     robosystems-report-components; the TAVI projection targets a published spec.
     - CHANGED OUTPUT: a different value, a renamed key, a restructured document. Say which
       filings are affected — a consumer's rendering may depend on the old shape.
     - BROADER COVERAGE: filings that previously failed now convert. Name the class of filing.
     - CLI CONTRACT: a renamed command or flag, breaking for anyone scripting `xbrlkit build ...`.
     - INTERNAL: refactors and tests leaving output identical. -->

INTERNAL

## Testing

<!-- Run `just test-all` (test -> format -> lint -> typecheck) before opening. Note `just format`
     auto-writes, so stage what it rewrote, and only the pytest stage emits a summary — a green
     test count is not a green gate.

     NAME THE FILINGS you tested against (accession numbers). SEC XBRL varies enormously between
     filers and years, so a conversion change verified on one filing is barely verified.
     "Not run" is a valid answer. -->
