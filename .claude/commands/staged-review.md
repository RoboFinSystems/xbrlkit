---
description: Review the staged diff against this converter's output contract and filing-variance rules.
---

Review all staged changes (`git diff --cached`) with focus on the contexts below. Read the diff first — if nothing is staged, say so rather than reviewing the working tree.

This is `xbrlkit`: a **published Python package** converting SEC XBRL filings into `holon.jsonld`, exposing the `holon` CLI and a library. **Public repository.** Everything runs through `uv run`.

## Output contract (decides the verdict)

The emitted `holon.jsonld` is consumed downstream by `xbrlkit-viewer` and `robosystems-report-components`:

- Does the diff change a **value**, a **key name**, or the **document structure**? Say for which filings and why — a consumer's rendering can depend on the old shape.
- A CLI change (renamed command or flag) is breaking for anyone scripting `xbrlkit build …`.
- Broader coverage — filings that previously failed now convert — is additive, but name the class of filing so it can be spot-checked.

## Filing variance (the thing that makes this repo hard)

SEC XBRL is inconsistent across filers, years, and taxonomies. So:

- **A parse change verified on one filing is barely verified.** Does the diff add a fixture for the filing that motivated it? Does it plausibly hold for other filers?
- Is the change **tolerant or presumptive**? Code that assumes a context has exactly one dimension, or that an element appears once, breaks on the next filer. Tolerance beats correctness-on-one-example here.
- Does it silently swallow malformed input? Skipping a fact quietly produces a document that renders fine and is wrong — prefer surfacing.

## Layering

- `edgar/` fetches; `parse/` + `model.py` convert; `query.py` / `cli.py` expose. A fetch concern leaking into parsing (or vice versa) is worth flagging.
- `xbrlkit/_vendor/` is vendored third-party code — it should not be edited, reformatted, or refactored as part of an unrelated change.

## Network and SEC etiquette

- The SEC requires a User-Agent; changes that drop or hardcode it will get the caller blocked. It belongs in configuration.
- Are requests rate-limited and cached as before? A change that removes backoff or caching turns a working tool into one the SEC throttles.
- Do tests avoid hitting the network by default? A test suite that needs EDGAR is a test suite that fails in CI for environmental reasons.

## Testing

- Do new behaviors have fixtures? Fixtures are how conversion bugs stay fixed.
- Is the test asserting correct behavior, or just what the code currently does?

## Public-repo hygiene

SEC filings are public data, so filing content in fixtures is fine and useful. What still doesn't belong: API keys, internal infrastructure detail, internal cost/pricing information, and non-public customer information.

Never stage a `version` bump in `pyproject.toml` — the release workflow owns it.

## Output

1. **Output impact**: CHANGED OUTPUT / BROADER COVERAGE / CLI CONTRACT / INTERNAL
2. **Issues**: Problems that should be fixed before commit
3. **Suggestions**: Improvements that aren't blocking
4. **Questions**: Anything unclear

Anchor findings to `file:line`. If the staged diff is clean, say so plainly.
