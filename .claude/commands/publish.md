---
description: Monitor a release/publish run — diagnose failures, verify the package landed on PyPI.
argument-hint: '[run-id]'
---

Monitor a release and publish run — pinpoint why it failed, and confirm the version landed on PyPI in a usable state.

## How a release happens here

One workflow, `create-release.yml` (dispatch), with three jobs in sequence:

1. **`create-release`** bumps the version in `pyproject.toml` and `server.json` on `main` and cuts `release/<version>`.
2. **`create-tag`** — `tag-release.yml` tags it and cuts the GitHub Release.
3. **`publish`** runs once the tag exists, under the `production` environment, so its required-reviewer prompt lands on this same run. It checks out the tag, verifies the version is the one just cut, validates `server.json`, builds, publishes to PyPI over OIDC trusted publishing, waits for PyPI to serve the release, and lists it on the Official MCP Registry. PyPI's trusted publisher is registered against `create-release.yml` and the `production` environment.

So **merging to `main` publishes nothing.** Dispatching the workflow is the publishing event, and nothing uploads until the deployment is approved on the run page. A failed publish is retried with **Re-run failed jobs** on that run; the version and tag are already cut, and a new release is warranted only when the code itself is at fault.

## Scope & guardrails

- **`gh` reads are free; dispatching a release is not.** A PyPI version can be yanked but **never re-uploaded**, so a bad publish burns that version number permanently. Confirm the bump type and ref with the user; default to watching a run they already started.
- **Never bump `version` in `pyproject.toml` by hand** — the workflow owns it.
- **A CLI contract change is a downstream event.** Anyone scripting `xbrlkit build …` breaks on a renamed command or flag. Say so and stop rather than dispatching.

## 1. Find the run

```bash
gh run list --workflow=create-release.yml --limit 5
gh run view <run-id>
gh run watch <run-id>
```

## 2. Pinpoint the failure

```bash
gh run view <run-id> --log-failed
```

- **Branch already exists** — a previous release run got partway; resolve the leftover `release/<version>` rather than re-dispatching blindly.
- **`publish` waiting** — paused on the `production` environment's required reviewer. Nothing is wrong; approve it from the run page (_Review deployments_).
- **Checked out version X, expected Y** — the tag the job checked out does not carry the version this run cut. Nothing was uploaded; look at what `create-tag` tagged.
- **server.json** — the listing must pin the version being released and the README must carry the `mcp-name` token, then `mcp-publisher validate` runs the registry's own checks. Both run before anything irreversible.
- **Build** — `just build-package` / `python -m build`. Packaging metadata problems surface here and nowhere in `just test-all`, which never builds a distribution.
- **Upload** — trusted publishing over OIDC; failures are usually PyPI-side publisher configuration (the publisher must name `create-release.yml` and the `production` environment), not code.
- **Version already on PyPI** — the upload is skipped and the job goes straight to the registry listing, which is what a re-run after a successful upload looks like. If you expected an upload, the version wasn't bumped.
- **MCP Registry listing** — runs after PyPI serves the release and skips itself when the version is already listed. A failure here leaves the PyPI release live but unlisted; fix the cause and re-run the failed job.

## 3. Verify it landed

```bash
curl -s https://pypi.org/pypi/xbrlkit/json | jq -r '.info.version'
```

Then confirm the artifact is usable, since packaging faults don't fail the upload:

```bash
pip download xbrlkit==<version> --no-deps -d /tmp/verify
unzip -l /tmp/verify/xbrlkit-<version>-*.whl | head -30
```

Check `py.typed` is present (its absence silently untypes the library for consumers) and that the vendored `_vendor/` files shipped — a missing vendored module fails only at import time on a user's machine.

Then the real check — the CLI actually runs:

```bash
uvx --from xbrlkit==<version> holon --help
```

## Output

Which workflow, what failed and where, the root cause, the re-run link, the verified published version, and whether the CLI runs from the published artifact. If nothing failed, say so.

$ARGUMENTS
