# Implementation Plan

## 1. Establish the audit ledger

- [x] Preserve a 94-record disposition matrix grouped by rule, file, and line.
- [x] Confirm current resolved versions and identify the active owning
      manifest/lock for every dependency advisory.
- [x] Record live-audit findings that are newer than or unrelated to the CSV as
      separate residual context.

## 2. Remediate dependencies

- [x] Upgrade Studio npm dependencies/resolutions past the vulnerable
      `minimatch`, `tar`, and `rollup` ranges using npm.
- [x] Upgrade Studio Python requirements past vulnerable
      `python-multipart` and `aiohttp` versions, align `pyproject.toml`, and
      regenerate `uv.lock`.
- [x] Use structured parsing plus package-native audits to prove the six
      report versions/advisory families are absent.
- [x] Run Studio install/build validation before continuing.

## 3. Fix actionable source findings

- [x] Add and verify a non-root final user in the Studio ECS Dockerfile.
- [x] Replace WebSocket string replacement with URL/scheme construction.
- [x] Replace mockup `innerHTML` with text/node DOM APIs.
- [x] Simplify the redundant invoke-button ternary.
- [x] Add focused regression checks where a durable automated assertion is
      practical.

## 4. Close audited false positives

- [x] Annotate all 21 synchronous subprocess and 5 async exec findings only
      after confirming argv execution and provenance.
- [x] Annotate all 41 intentional sleeps without changing polling semantics.
- [x] Annotate the 7 framework/thread callback closures.
- [x] Annotate the fixture-only `shell=True` call with `nosec B602`.
- [x] Annotate the public OAuth provider name for `generic-api-key`.
- [x] Recount annotations/findings to ensure no CSV row was skipped and inspect
      every suppression for rule-level scope.

## 5. Validate

- [x] Run focused backend tests for subprocess, conversion, bootstrap, and
      lifecycle behavior.
- [x] Run Studio build plus lint baseline comparison. Full lint retains 85
      pre-existing vendored errors; the new helper is clean.
- [x] Run relevant Studio backend lock/import checks.
- [x] Attempt the ECS image build and inspect the Dockerfile runtime user.
      Public ECR returned 403; the mirror build passed the user-creation layer
      and then stopped at the intentionally runtime-generated source file.
- [x] Run `make verify`.
- [x] Start the local stack and validate the Studio target flow with
      Playwright CLI because the Browser plugin is unavailable.
- [x] Rerun ProbeScan if its CLI/config is available; otherwise execute and
      retain targeted static/package-audit evidence and state the limitation.
- [x] Review `git diff` and `git status` so the CSV remains untracked and no
      unrelated files are included.

## 6. Finish

- [x] Update the relevant spec only if implementation establishes a reusable
      security contract not already documented.
- [x] Commit only the reviewed remediation/task files (`b4c92f8`).
- [x] Archive the Trellis task after all required checks pass.
