# Surface failed system-prompt recommendation instead of silent fallback

Source: manual test finding ISSUE-007 (severity high, functional/ux),
`/evaluation?view=experiment`, chapter 09 `RECOMMEND` action.

## Problem

A system-prompt recommendation that AWS finishes as `FAILED` is presented to the
operator as a successful optimization result.

Observed: recommendation
`arn:…:recommendation/exp_d2b32b44_sp_682f8c-F4CFE9D25B` ended `FAILED` with a
`ValidationException` (prompt/session traces flagged by safety filters as a
possible prompt attack). Launchpad stored `system_prompt_status: FAILED` but the
UI showed no failure. It filled the editable recommendation with the generic
fallback text produced by `_fallback_treatment_prompt()` and enabled
`ACCEPT & CONTINUE`, so an operator can build (and potentially promote)
treatment resources from text no optimizer ever produced.

Root cause:

- `backend/app/optimization/service.py:380-392` — the system-prompt branch
  writes `recommended_prompt = <aws prompt> or _fallback_treatment_prompt(cur)`
  regardless of job status, and never reads
  `systemPromptRecommendationResult.errorCode` / `errorMessage` (both exist in
  the boto3 shape and are already used by the tool-description branch).
- `frontend/src/pages/EvaluationExperiment.tsx:587` — `spDone` is
  `rec?.recommended_prompt != null`, so the fallback text makes the stage look
  complete: no error note, no regenerate button, accept enabled. The
  tool-description path has `tool_status` / `tool_error` surfacing
  (lines 697-707); the system-prompt path has no equivalent.

## Requirements

1. Backend must record the real outcome of the system-prompt recommendation
   job: keep `system_prompt_status` (AWS status) and add
   `system_prompt_error` carrying `errorCode: errorMessage` (or
   `recommendation job ended <status>` when AWS gives no detail).
2. On a non-successful job (status not `COMPLETED`, or `COMPLETED` with an
   empty `recommendedSystemPrompt`), the backend must NOT emit a fabricated
   `recommended_prompt`. The generic fallback text is removed as a treatment
   source.
3. Regenerating the `system_prompt` type must clear the failure keys as well as
   the success keys (`_REC_KEYS`).
4. The UI must show the failure: a critical-styled note with the AWS status and
   error text, plus the "generate system-prompt recommendation" button so the
   operator can retry.
5. `ACCEPT & CONTINUE` must be disabled while the system-prompt recommendation
   is in a failed state — the operator may only proceed by regenerating
   successfully, or by authoring a treatment prompt themselves (edited text
   differing from the control prompt). The disabled state must state why.
6. The backend accept endpoint must enforce the same rule (409), not just the
   UI: after a failed system-prompt job, accept is rejected unless the request
   carries an operator-authored prompt that differs from the control prompt.
7. Tool-description-only recommendation runs keep working exactly as today
   (treatment inherits the current production prompt).
8. All new user-facing strings are i18n keys with en + zh-CN parity.

## Non-goals

- Retrying/altering the AWS recommendation request to avoid the safety filter.
- Changing tool-description recommendation behavior.
- Any change to bundles / A-B / promote stages beyond being gated by accept.

## Acceptance Criteria

- [ ] `stage_recommend` on a FAILED system-prompt job returns
      `system_prompt_status="FAILED"`, a non-empty `system_prompt_error`, and no
      `recommended_prompt`; a COMPLETED-but-empty result is treated the same.
- [ ] `stage_recommend` on a COMPLETED job with a prompt is unchanged
      (`recommended_prompt`, `explanation`, no `system_prompt_error`).
- [ ] Re-running only `system_prompt` clears a stale `system_prompt_error`.
- [ ] `POST /api/experiments/{id}/action {action:"accept"}` returns 409 when the
      stored recommendation has a failed system-prompt status and the submitted
      prompt is absent or equal to the control prompt; it succeeds when a
      different operator-authored prompt is submitted.
- [ ] Experiment page renders a failure note (AWS status + error) and the
      regenerate button when the system-prompt job failed, and the accept
      button is disabled with a stated reason until the prompt is edited.
- [ ] `_fallback_treatment_prompt` no longer supplies accepted/treatment text.
- [ ] `make verify` passes (backend ruff+pytest, infra, frontend
      eslint+tsc+build, i18n parity).
