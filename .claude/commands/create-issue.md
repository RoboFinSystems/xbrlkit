---
description: Create a GitHub issue for the XBRL→holon CLI, routed to the right layer.
argument-hint: '[what the issue is about]'
---

Create a GitHub issue for the current repository based on the user's input.

## Instructions

1. **Work out which layer owns it** - This package converts SEC XBRL filings into `holon.jsonld` documents, and ships both a `holon` CLI (`build`, `fetch`, `query`) and a library. Most bug reports resolve to one of four places — say which:
   - **The filing itself.** SEC XBRL is frequently malformed or unusual. If the source document really says what the output reflects, this is not a bug — it may be a request for tolerance, which is a different and much bigger change.
   - **Fetching** (`xbrlkit/edgar/`) — retrieval, the SEC User-Agent requirement, rate limits, caching.
   - **Parsing / modelling** (`parse/`, `model.py`) — the filing was fetched correctly but the translation into the holon model dropped or mangled something.
   - **Query / CLI surface** (`query.py`, `cli.py`, `config.py`) — the model was right but the command or query returns the wrong thing.

   Rendering bugs are usually **not** this repo: the viewer is `xbrlkit-viewer` and the rendering components are `robosystems-report-components`. If the `holon.jsonld` is correct and it still looks wrong, file it there.

2. **Determine Issue Type** - Pick one: **Bug**, **Task**, **Feature**, **RFC**, **Spec**. This repo has **no `.github/ISSUE_TEMPLATE/` directory**, so confirm with `ls .github/ISSUE_TEMPLATE/` and `gh issue create --help` before assuming; structure the body yourself if there's nothing to mirror.

3. **Draft the Issue** - For a conversion bug, include:
   - **The filing**: accession number, CIK, form type, and period. Without these it isn't reproducible — every XBRL bug is filing-specific.
   - The **exact command** (`xbrlkit build …`) and the installed version.
   - **Actual vs expected** in the output document — name the concept/element and the value.
   - Whether it reproduces with a **freshly fetched** filing versus a cached one.
   - A minimal excerpt of the source XBRL, if you can isolate it. SEC filings are public, so pasting the relevant fragment is fine and makes the issue actionable.

4. **Sanitize** - This repo is public and the issue is world-readable immediately. SEC filings are public data, so the usual financial-data caution doesn't apply to filing content — but still keep out API keys, internal infrastructure detail, internal cost/pricing information, and anything about non-public customers. Keep security-adjacent text terse; use a private Security Advisory for coordinated disclosure.

5. **Create the Issue**:

   ```bash
   gh issue create \
     --type <Bug|Task|Feature|RFC|Spec> \
     --title "<clear, concise title>" \
     --body-file /tmp/issue-body.md
   ```

   Write the body to a file to avoid shell-escaping problems.

## Labels

```bash
gh label list --limit 100
```

**This repo carries only GitHub's stock labels** — it does **not** have the `area:*` / `priority:*` / `size:*` families the apps and SDK clients use. Don't apply those from memory; `gh issue create` fails on a label that doesn't exist.

## Output Format

1. The issue URL
2. Brief summary of what was created
3. Issue type and any labels applied
4. Which layer you concluded owns it, and whether it should be filed against the viewer or report-components instead

$ARGUMENTS
