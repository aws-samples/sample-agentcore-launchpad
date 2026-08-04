# Implementation plan

## Step 1 - Backend characterization tests

- [x] Extend `backend/tests/test_zip_runtime_deployer.py` with the intended
      dispatch matrix:
      - normal HTTP/A2A zip + `spec.skills` uses explicit-path bundling;
      - no selected Skills is a no-op;
      - Studio remains code-reference-driven;
      - converted `code_bundle` + `spec.skills` remains a no-op.
- [x] Extend template tests for conditional `AgentSkills` initialization and
      HTTP/A2A compilation.
- Validate:
  `cd backend && uv run pytest tests/test_zip_runtime_deployer.py tests/test_strands_template.py tests/test_a2a_agent.py -q`

## Step 2 - Zip package dispatch

- [x] Update `backend/app/deployer/zip_runtime.py::bundle_skills` to route
      platform-generated `zip_runtime` specs to the existing
      `bundle_skill_paths_into()` helper.
- [x] Preserve the Studio code-reference branch and explicitly exclude
      `code_bundle`.
- [x] Update stale docstrings/comments that describe the package hook as
      Studio-only.
- [x] Confirm package stage detail lists successfully bundled Skill names.
- Validate the Step 1 test set.

## Step 3 - HTTP and A2A template wiring

- [x] Update `backend/app/templates/strands_agent/__init__.py` and
      `main.py.tmpl` with the rendered Skill-enabled flag, local Skill root,
      and conditional plugin wiring.
- [x] Apply the same contract to
      `backend/app/templates/strands_a2a_agent/`.
- [x] Do not change package requirements.
- [x] Verify no-Skill rendered output imports and runs as before.
- Validate:
  `cd backend && uv run pytest tests/test_strands_template.py tests/test_a2a_agent.py -q`

## Step 4 - Frontend create/edit flow

- [x] Update `frontend/src/pages/CreateAgent.tsx` so the shared Skill picker is
      visible for `zip_runtime`.
- [x] Include selected paths in zip create/redeploy specs.
- [x] Preserve the separate A2A AgentCard skills editor and payload.
- [x] Add i18n keys only if clarifying copy is required; keep en/zh-CN parity.
- Validate:
  `cd frontend && npm run lint && npx tsc --noEmit && npm run build`
  and `python3 scripts/i18n_check.py`.

## Step 5 - Focused backend regression

- [x] Run zip/template/A2A tests.
- [x] Run Harness conversion tests to prove `code_bundle` semantics.
- [x] Run container bundling tests to prove shared explicit-path behavior.
- [x] Run per-agent IAM policy tests.
- Validate:
  `cd backend && uv run pytest tests/test_zip_runtime_deployer.py tests/test_strands_template.py tests/test_a2a_agent.py tests/test_harness_convert.py tests/test_container_skill_bundle.py tests/test_agent_iam_policy.py -q`

## Step 6 - Browser verification

- [x] Start the local stack with `make dev`.
- [x] Use Playwright to verify the zip Skill picker, create payload, edit
      rehydration, and simultaneous A2A mounted/AgentCard Skill fields.
- [x] Check desktop and mobile layouts for overlap or clipped controls.
- [x] Record browser evidence under the task's `research/` directory; keep
      screenshots in `/tmp` per the frontend-testing skill.

## Step 7 - Documentation

- [x] Update `docs/architecture.md`.
- [x] Add `.trellis/spec/launchpad/zip-runtime-skills.md` and register it in
      `.trellis/spec/launchpad/index.md`.
- [x] Correct the conversion-only semantics in
      `.trellis/spec/launchpad/harness-conversion.md`.
- [x] Extend `.trellis/spec/launchpad/a2a-agents.md` with the two-Skills
      distinction.

## Step 8 - Canonical verification

- [x] Run `make verify`; do not report completion until it passes.
- [x] Review `git diff --check` and the full scoped diff.

## Step 9 - Real Portal/AWS validation

- [x] Confirm `config/launchpad.yaml`, AWS credentials, and an APPROVED
      deterministic Registry Skill in `us-west-2`.
- [x] Through Portal, create an HTTP zip runtime with that Skill.
- [x] Wait for ACTIVE, verify job logs name the bundled Skill, invoke through
      Chat, and record the distinctive Skill-controlled response.
- [x] Repeat for an A2A zip runtime, also verifying its AgentCard metadata
      remains independent from the mounted Registry Skill.
- [x] Capture runtime ids, job ids, invocation evidence, and cleanup outcome
      under `.trellis/tasks/08-04-zip-runtime-registry-skills/research/`.

## Step 10 - Review and finish

- [x] Run a final code review focused on packaging dispatch, template startup,
      A2A semantics, and converted-Harness compatibility.
- [x] Re-run affected tests after review fixes.
- [x] Update Trellis spec/journal, commit only task-scoped files, and archive
      the task after implementation and live validation are complete.

## Rollback

- No schema, database, Registry, or infrastructure migration is introduced.
- Reverting the frontend/template/deployer changes restores the old capability
  matrix.
- Existing stored `spec.skills` fields remain backward-compatible.
- Temporary AWS runtimes can be deleted independently through the existing
  agent lifecycle.
