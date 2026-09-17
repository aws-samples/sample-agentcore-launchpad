# Proposal self-check (run before every `launchpad-proposal` block)

Launchpad validates the block with a strict contract (`extra="forbid"`, exact member
names, limits, cross-field rules). A block that fails is stored as an **invalid**
revision: the member sees the errors, nothing can be approved, and the rejection is
replayed to you on the next turn. You have no shell in this Harness, so this file IS the
script: walk every check below, in order, against the JSON you are about to emit, and
fix the block before emitting it. Every rule here is a rule the platform enforces.

## 1. Block

- [ ] Exactly ONE fenced block tagged `launchpad-proposal` in the reply; one JSON object;
      nothing else inside the fence. Two blocks → the reply carries no proposal.
- [ ] Whole block ≤ 64,000 bytes. Trim `system_prompt`, notes and scenario text first.
- [ ] Only the members the protocol lists. No `env`, `code`, `requirements`,
      `allowed_tools`, `protocol`, `filesystem`, `network`, comments or trailing commas.

## 2. Agent members

- [ ] `version`: 1.
- [ ] `name`: `^[a-z][a-z0-9-]{2,47}$`; not starting with `launchpad-`, `harness-`,
      `system-`; not the name of an Agent already in the catalog.
- [ ] `model_id` / `model_source` (`bedrock` | `mantle`).
- [ ] `system_prompt`: 1–20,000 chars — and lean: identity, goal, hard boundaries,
      escalation, tone, language — nothing more. No scenario scripts, no
      golden tests pasted in; coverage comes from the Evaluation → Optimization loop.
- [ ] `tools`, `skills`, `knowledge_bases`: catalog keys/ids from the preamble ONLY,
      no repeats, ≤ 20 / 10 / 10. Anything missing from the catalog → `manual_tasks`.
- [ ] `memory`: `disabled` | `workspace`.
- [ ] `max_iterations` 1–100; `timeout_seconds` 10–3600, default 180 unless the user
      explicitly chooses a different execution budget. Do not turn a latency goal
      into an unreviewed hard cancellation limit.
- [ ] `summary` ≤ 4,000 chars; `requirements_baseline`, `assumptions`, `manual_tasks`,
      `evaluator_recommendations`: lists of non-empty strings ≤ 1,000 chars, ≤ 40 each.

## 3. Golden tests

- [ ] `golden_tests` ≤ 40; each has `id` (unique, ≤ 64), `input` (1–2,000),
      `expected_response` (≤ 2,000), `expected_tools` (≤ 10), `forbidden_behavior` and
      `pass_criteria` (≤ 1,000 each), `evaluator` (≤ 200), `source` ∈
      `customer_pain_point` | `industry_assumption`. No other members.
- [ ] Every id referenced anywhere in `evaluation_plan` exists here.

## 4. `evaluation_plan` — routing (the rule most often broken)

A dataset run scores **every session with every evaluator**. The platform cannot send
one evaluator to some golden tests and not to others, and a seed that tries is rejected
as a whole — after the member has already read and approved the design.

- [ ] **`golden_test_ids` is `[]` on every evaluator.** Not a subset, not "the tests it
      is meant for". (A list naming every scenario is tolerated but pointless.)
- [ ] Ready-made first: every `existing` entry uses an `evaluator_id` copied from the
      preamble's "Evaluators" list (built-in / third-party); every custom `judge` or
      `code` entry scores something no listed evaluator scores and says so in
      `description`. Harmfulness, toxicity, bias, PII, refusal, instruction following,
      helpfulness, relevance, conciseness, task completion → listed evaluators, never a
      custom judge.
- [ ] Before writing a tool constraint, inspect `tools`, `knowledge_bases` and `skills`
      together. `tools: []` does not mean the Harness is tool-free: KB retrieval and
      Skill loading/execution can still produce tool spans. Read-only means no
      business writes or false execution claims, not no reads or no support tools.
- [ ] A global `tool_count` with `max: 0` and no specific `tool`, or an exact empty
      `tool_sequence`, conflicts with any mounted tool, KB or Skill and is rejected.
      A verified named write-tool prohibition remains valid. Do not invent tool names.
      Empty expected trajectories do not authorize a global zero-call constraint.
- [ ] For every judge and code rule, ask: *is it true in EVERY scenario?*
  - Yes (never diagnoses; never claims to have contacted anyone;
    always the customer's language) → keep it as an evaluator.
  - No (only emergency scenarios must contain "call emergency services"; only
    medication scenarios must refer to a pharmacist) → it is NOT an evaluator. Move
    the requirement into the `assertions` of the scenarios it belongs to; the
    per-scenario SESSION judge that reads `{assertions}` scores it there. Delete the
    evaluator or generalise it into an invariant.
  - `output_contains` is almost never a global invariant. `output_not_contains`
    and named `tool_set forbidden` checks still need scenario and capability review.
    Global zero-call checks require a genuinely tool-free design and verified
    runtime behavior; even refusal scenarios may need retrieval or Skill loading.
- [ ] A reference-driven evaluator (a judge using `{expected_response}`,
      `{expected_tool_trajectory}` or `{assertions}`; a `reference_*` rule;
      `Builtin.ToolSelectionAccuracy`-style trajectory evaluators) is applied to every
      scenario, so EVERY scenario carries that reference — every turn's
      `expected_response` for a TRACE judge/rule, `expected_trajectory` for a
      trajectory evaluator, `assertions` for an assertions judge.
- [ ] A golden test no single-session AgentCore evaluator can score (multi-actor memory
      isolation, cross-session freshness, expert sign-off, metric baselines) is in
      `blocked_golden_tests` with its reason — not a scenario, not an evaluator — and
      the obligation is a `manual_tasks` line.

## 5. `evaluation_plan` — shapes

- [ ] **≤ 10 evaluators in total** (existing + judge + derived + code — one batch
      evaluation applies all of them and AWS accepts no more); ≤ 10 of kind
      `judge`/`derived`/`code`; ≤ 40 scenarios; ≤ 40 blocked golden tests. Fewer, chosen
      for this agent, beats the whole built-in list. Evaluator `key`s unique (`^[A-Za-z][A-Za-z0-9_-]{0,31}$`);
      cloud evaluator `name`s unique (`^[a-zA-Z][a-zA-Z0-9_]{0,47}$`).
- [ ] Every entry has ONLY the members of its kind:
  - `existing`: `kind, key, title, evaluator_id ("Builtin.<Name>" / "ThirdParty.<Name>"),
    golden_test_ids` (+ optional `blocking, threshold, note`).
  - `judge`: `+ name, instructions (10–4,000 chars), level (TRACE|SESSION|TOOL_CALL)`,
    optional `rating_scale, model_id, description`. Instructions contain ≥ 1
    placeholder and only the level's: SESSION → `{context} {available_tools}
    {actual_tool_trajectory} {expected_tool_trajectory} {assertions}`; TRACE/TOOL_CALL →
    `{context} {assistant_turn} {expected_response}`.
  - `code`: `+ name, level (TRACE|SESSION), rules: {version: 1, checks: [...]}`,
    optional `lambda_timeout_s, description`. 1–20 checks, unique `id`s.
- [ ] Every code check has `id`, `type` and EXACTLY its type's members — never `count`,
      `values`, `match`, `pattern`, `regex`, `any`, `all`:
  - `tool_count` → `min` and/or `max` (0–1000, min ≤ max); optional `tool` (omit = all
    tools; never `"*"`). "No tool calls" = `{"type": "tool_count", "max": 0}`.
  - `tool_sequence` → `tools` (1–20) + `mode` `exact` | `subsequence`.
  - `tool_set` → `allowed` and/or `forbidden`. Use exact names from the selected
    runtime catalog, never `mcp:` / `gateway:` attachment selectors. A positive
    allowlist includes mounted `skills` and KB retrieval support names. Missing
    catalogs remain unresolved; do not guess names or copy arbitrary observed calls.
    Native Harness `shell` / `file_operations` must first be selected in `native_tools`
    and disclosed for review; they are off by default. Evaluation rules cannot grant
    runtime access. Tool names cannot enforce read-only shell commands.
  - `output_contains` / `output_not_contains` / `output_exact` → `text` (ONE literal,
    non-empty) + optional `case_sensitive`. Several literals = several checks, and all
    must pass; "any of these phrases" cannot be a rule — make it a judge or an assertion.
  - `reference_trajectory` → level SESSION, `mode` `superset` | `exact`.
  - `reference_response` → level TRACE, no further members.
- [ ] Every scenario: `scenario_id` (`^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$`, unique),
      `golden_test_id` (exists; one scenario per golden test; not also blocked),
      `turns` 1–20 of `{input (1–8,000), expected_response? (≤ 2,000)}`,
      `expected_trajectory` ≤ 10, `assertions` ≤ 10 strings of 1–1,000 chars, optional
      `note`. Assertions are natural-language pass criteria, `"Must not: …"` for the
      forbidden ones.
- [ ] `recommendation_keys`: `{"<index of evaluator_recommendations>": ["<evaluator key>"]}`
      — indexes that exist, keys that exist.
- [ ] `blocked_golden_tests`: `{golden_test_id, reason}`, each id once.

## 6. `fishbone`

Present whenever at least ONE barrier was confirmed in THIS conversation — a partial
fishbone (other dimensions `unresolved`) is emitted, not dropped. Absent only when no
barrier was confirmed (never `{}` or assumed).

- [ ] `version` 1; `customer` (1–200); `date` `YYYY-MM-DD`; `use_case` (1–500);
      `service_target` ∈ `internal` | `b2b` | `b2c`.
- [ ] `coverage` names exactly the six dimensions `cognition quality responsibility cost
      performance other`, each `confirmed` | `explored_empty` | `unresolved`.
- [ ] `barriers` keys ⊆ those six; ≤ 20 notes per dimension; each note `{sticky_text
      (1–120), evidence? (≤ 1,000), customer_quote? (≤ 1,000, redacted), confirmed, selected}`.
- [ ] `selected` ⇒ `confirmed`; ≤ 3 selected per dimension.
- [ ] coverage `confirmed` ⇔ that dimension has ≥ 1 confirmed note; `explored_empty` /
      `unresolved` dimensions have none.
- [ ] `parking_lot` ≤ 40 of `{original (1–1,000), converted_to? (≤ 200)}`.

## 7. Last look

- [ ] Every scenario-specific requirement lives in a scenario's `assertions`, every
      evaluator is a global invariant, every `golden_test_ids` is `[]`.
- [ ] Every catalog reference (tool key, skill name, KB id, evaluator id) is copied from
      the preamble, not remembered or invented.
- [ ] The prose above the block already shows the architecture, trade-offs, assumptions,
      manual tasks and the golden-test table — the block adds structure, not new facts.
- [ ] The JSON parses. Count the braces of the last evaluator and the last scenario.
