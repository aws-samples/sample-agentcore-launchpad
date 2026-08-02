# Rewrite lab docs as participant instructions

## Goal

Turn the Chinese workshop material under `docs/lab/` into participant-facing
instructions instead of a report of one historical workshop run.

## Requirements

- Keep commands, links, screenshots, resource names, product facts, step order, and
  interface interpretation guidance intact.
- Remove concrete run IDs and other identifiers copied from historical executions.
- Remove measured durations, timestamped timelines, historical scores, and wording such
  as "本次实测" or "本次实跑".
- Retain generic wait-time guidance only when participants need it to decide whether a
  state is normal or stalled.
- Rewrite affected Chinese prose with a direct, natural lab-guide voice using the
  `humanizer-zh` rules.
- Recheck the lab landing page and any facilitator guide in the current workspace for
  the same report-like wording.
- Do not change root product documentation or application behavior.

## Acceptance Criteria

- [x] `docs/lab/README.md` and all affected chapters address the participant through
      actions, expected states, and interpretation guidance.
- [x] No historical run ID, measured duration, score snapshot, or "本次实测/实跑"
      narration remains in `docs/lab/*.md`.
- [x] Necessary generic timing and UI-reading guidance remains available.
- [x] Markdown structure, code fences, and local links remain valid.
- [x] `git diff --check` and `make verify` pass.
- [x] The workspace is checked for a facilitator guide; absence is recorded rather than
      inventing one.

## Notes

- The corresponding Workshop Studio repository and `FACILITATOR_GUIDE.md` are not
  present in the current workspace snapshot.
- Final verification: backend 999 passed, infra 9 passed, frontend lint/typecheck/build
  passed, i18n parity passed, and `verify: PASS`.
- No product behavior or reusable implementation pattern changed, so no Trellis spec
  update was needed.
