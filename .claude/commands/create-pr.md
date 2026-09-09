---
description: Open a pull request for the current branch, writing the description from the work actually done.
argument-hint: '[target-branch] [review]'
---

Create a GitHub pull request for the current branch, writing the title and description from the actual work done in this session — not reconstructed from the diff.

## Why this command exists

A description written from the diff alone can't know _why_ a change was made, so it tends to describe things that aren't true — and those descriptions then feed `@claude` reviews, compounding the bad information. **You author it here, where the full context is available.**

This is `xbrlkit` — a **published Python package** exposing the `holon` CLI (`build`, `fetch`, `query`) and a library that converts SEC XBRL filings into `holon.jsonld` documents. **This repository is public**, and publishing is triggered by a push to `release/**` rather than by a merge, so the PR text is often public before the version that carries it.

**Everything runs through `uv run`** — the `just` recipes already do. Never invoke bare `python`, `pytest`, or `ruff`.

## Instructions

### 1. Preflight

```bash
CURRENT=$(git branch --show-current)
TARGET=${1:-main}
```

- **Never PR from `main`.** Branches come from `just create-feature <type> <name>`, not `git switch -c`.
- **Never target `release/**`** — that's the publish trigger, not a review branch.
- **Uncommitted changes**: surface them and ask whether to commit (never on `main`, stage by name, no `git add -A`).
- **Existing PR**: `gh pr list --head "$CURRENT" --base "$TARGET" --json url,number` — offer `gh pr edit` rather than duplicating.
- **Push**: `git push -u origin "$CURRENT"`.

### 2. Gather the real change context

- **Primary source: this session.** What changed and why.
- Corroborate with `git log --oneline "$TARGET".."$CURRENT"`, `git diff --stat`, and reading the full `git diff`.
- **No confabulation.** Every claim must be supported by the diff; when session context and the diff disagree, the diff wins.

### 3. Compose the PR

- **Title** — conventional-commit style with a scope, matching `git log` (e.g. `fix(parse): keep dimension members on nested contexts`).
- **Body** — **match the headings in `.github/PULL_REQUEST_TEMPLATE.md`**, since `--body-file` bypasses template prefill and a hand-written body silently drops omitted sections:
  - **Summary** — 1–3 sentences.
  - **Changes** — grouped by layer: `edgar/` (fetch), `parse/` + `model.py` (convert), `query.py` / `cli.py` (surface).
  - **Output Impact** — see below. "None" if the emitted `holon.jsonld` is byte-identical for existing filings.
  - **Testing** — the gate is `just test-all` (`test` → `format` → `lint` → `typecheck`). Name the filings you tested against — a conversion change verified on one filing is barely verified. If nothing was run, say "Not run".

  The template has no Related Issues section — put `Closes #123` as the last line of the Summary.

- **Output Impact is the judgment that matters.** Downstream, `xbrlkit-viewer` and `robosystems-report-components` render these documents:
  - **Changed output** — a different value, a renamed key, a restructured document. Say what changes and for which filings; a consumer's rendering may depend on the old shape.
  - **Broader coverage** — filings that previously failed now convert. Free, but name the class.
  - **CLI contract** — a renamed flag or command is breaking for anyone scripting against it.
  - **Internal** — refactors and tests leaving output identical.

- **Never bump `version` in `pyproject.toml`** — the release workflow owns it.
- **Attribution** — attribute to the user only; no Claude footer or trailer unless explicitly asked.

### 4. Create the PR

```bash
gh pr create --base "$TARGET" --head "$CURRENT" --title "<title>" --body-file /tmp/pr-body.md
```

### 5. Optional Claude review

Only if the user asks (`review` / `--review`): `gh pr comment <number> --body "@claude please review this PR"`. Nothing happens automatically.

## Output

1. PR URL. 2. Title. 3. Target ← source. 4. The Output Impact classification. 5. Whether a review was requested.

$ARGUMENTS
