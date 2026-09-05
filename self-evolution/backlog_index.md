# Self-evolution backlog — index

Source-of-truth router for the direction pages under `backlog/`. Mutable direction
state (Status, Notes, Branch, PR) lives **only** in the routed pages; this index never
summarises it. Managed by `.claude/skills/self-evolution/scripts/backlog.py`.

## Contract

- **Statuses**: `not-started` · `in-progress` · `in-review` · `done` · `abandoned`.
  `in-review` = PR open, hermetic acceptance (`make verify` + diff review) passed,
  awaiting merge and/or a declared live check. It does not block the batch.
- **Exactly one** direction is `in-progress` at a time.
- **Priority**: lower number wins; earlier ID breaks ties. A page owns 20 priorities.
- **Batch**: the unfinished records sharing the newest `Origin report`. A run works
  the batch first, then leftovers from older batches.
- **Ranking dimensions**, each 1–5: Importance (higher = more valuable) ·
  Architecture fit (higher = fits the current design better) · Evidence confidence
  (higher = stronger support) · Implementation difficulty (higher = harder) ·
  Implementation risk (higher = riskier).
- **Score** = 2·Importance + Architecture fit + Evidence − Difficulty − Risk.
- **`MINIMUM_IMPLEMENTATION_SCORE = 7`.** Applied when proposing (below-gate
  directions are recorded only in the research report) and when selecting (a
  below-gate record is set `abandoned` with `below score gate (Score = n < 7)`).
  Two constraints keep the gate honest: never restate a rating to cross it, and a
  below-gate direction survives only with a recorded safety, correctness or
  dependency reason.

## Pages
- [directions-001-020.md](backlog/directions-001-020.md) — priorities 1–20
