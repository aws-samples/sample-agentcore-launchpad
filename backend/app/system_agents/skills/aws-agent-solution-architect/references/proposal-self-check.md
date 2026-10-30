# Proposal self-check (run before every proposal block)

Launchpad validates a proposal with a strict contract (`extra="forbid"`, exact member
names, limits, cross-field rules). A failing block is stored as an **invalid** revision
and every failing rule — not only the first — is replayed to you on the next turn. Walk
every check below, in order, before the first block — each rejection costs the member time. The platform
validates shapes and references; you must also review whether each golden test's
`evaluator` and `pass_criteria` can actually judge the intended requirement.

## 1. Delivery

- [ ] Exactly ONE fenced block tagged `launchpad-proposal` (or ONE
      `launchpad-proposal-patch` — `{"base_revision": N, "operations": […]}`, RFC 6902
      against the stored JSON in the preamble — for a later revision) in the reply; one
      JSON object; nothing else inside the fence. Two blocks → the reply carries no
      proposal.
- [ ] Never mention patches or operations to the member; describe what changed.
- [ ] Whole block ≤ 64,000 bytes. Trim `system_prompt`, notes and scenario text first.
- [ ] Only the members the protocol lists. No `env`, `code`, `requirements`,
      `allowed_tools`, `protocol`, `filesystem`, `network`, comments or trailing commas.

## 2. Agent members

- [ ] `version`: 1.
- [ ] `name`: `^[a-z][a-z0-9-]{2,47}$`; not starting with `launchpad-`, `harness-`,
      `system-`; not the name of an Agent already in the catalog.
- [ ] `model_id` is one of the ids under "Model catalog" in SKILL.md / the protocol
      preamble — copied exactly, never a model the customer named that is not listed
      (ask them to choose first); `model_source` is `bedrock`.
- [ ] `system_prompt`: schema allows 1–20,000 chars, but v1 is **≤ 50 characters**
      (≤ 50 汉字 incl. punctuation; English ≤ 50 words): who the agent is and what it
      does for whom — nothing more. No red lines, escalation, tone, tool instructions,
      scenario scripts or golden tests; those live in golden tests, evaluators and
      `requirements_baseline`, and reach the prompt through later revisions driven by
      the golden tests. Longer only when the user explicitly asked for it.
- [ ] `tools`, `skills`, `knowledge_bases`: catalog keys/ids from the preamble ONLY,
      no repeats, ≤ 20 / 10 / 10. Anything missing from the catalog → `manual_tasks`.
- [ ] `memory`: `disabled` | `workspace`.
- [ ] `max_iterations` 1–100, default 100; `timeout_seconds` 10–3600, default 600 unless the user
      explicitly chooses a different execution budget. Do not turn a latency goal
      into an unreviewed hard cancellation limit.
- [ ] `summary` ≤ 4,000 chars; `requirements_baseline`, `assumptions`, `manual_tasks`:
      lists of non-empty strings ≤ 1,000 chars, ≤ 40 each.
- [ ] `evaluator_recommendations` (≤ 40): each entry is a non-empty string ≤ 1,000 chars
      OR the object `{"text", "evaluator_id"?, "blocking"?, "threshold"?, "metric"?}` —
      `text` required, no other members (never a registry row `{name, type, level,
      owner, …}`). Every entry that states a gate is the object (§5 "Recommendation
      gates").

## 3. Golden tests

- [ ] `golden_tests` ≤ 40; each has `id` (unique, ≤ 64), `input` (1–2,000),
      `expected_response` (≤ 2,000), `expected_tools` (≤ 10), `forbidden_behavior` and
      `pass_criteria` (≤ 1,000 each), `evaluator` (≤ 200), `source` ∈
      `customer_pain_point` | `industry_assumption`, optional `adversarial` (boolean).
      No other members.
- [ ] Every red-team test is marked `adversarial: true`: embedded instructions or role
      overrides, insider / nonpublic information, pressure for prohibited advice or
      actions (the flag keeps red-team transcripts out of any later prompt revision).

## 4. `fishbone`

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
      `unresolved` dimensions have none confirmed.
- [ ] No blank dimension: every dimension was asked about; `unresolved` carries ≥ 1
      suggested note with `confirmed: false, selected: false`; `explored_empty` only on
      the customer's explicit "nothing there" after the suggestions; ≥ 1 confirmed
      note in the whole fishbone.
- [ ] A blanket acceptance of the express close ("默认", "按你的建议", "都适用", the
      "these apply" option) made each of its suggestions `confirmed: true` — the
      fishbone is present, not omitted as "only assumed".
- [ ] `parking_lot` ≤ 40 of `{original (1–1,000), converted_to? (≤ 200)}`.

## 5. `evaluation_plan` seed — self-consistency (optional member)

The seed is inert; the member later creates evaluation assets from the reviewed plan.
Launchpad drafts that plan from the seed at proposal time and checks the rules below
on the draft — all of them, reported together. Omit the member rather than emit a
seed that fails one.

- [ ] **Count ≤ 10 evaluators**, counting what the platform ADDS to the seed: every
      `Builtin.*` / `ThirdParty.*` id named in `evaluator_recommendations` that the seed
      does not list, the assertions judge when every scenario has `assertions` (unless a
      seeded judge already reads `{assertions}`), and the expected-tools rule when every
      scenario has an `expected_trajectory` (unless a seeded code rule already uses
      `reference_trajectory`). A seed of ten with assertions is eleven.
- [ ] **Evaluators ↔ dataset agree.** Every `scenarios[].golden_test_id` and every
      `blocked_golden_tests[].golden_test_id` exists in `golden_tests`; a golden test is
      a scenario OR blocked — never both, never twice; one scenario per golden test.
      `golden_test_ids` is `[]` on every evaluator (a run scores every session with every
      evaluator; the platform cannot route one evaluator to some tests).
- [ ] **References agree.** A judge whose `instructions` read `{expected_response}`,
      `{assertions}` or `{expected_tool_trajectory}`, and a code rule of type
      `reference_response` / `reference_trajectory`, apply to EVERY scenario — so every
      scenario must carry that reference (`expected_response` on every turn for a TRACE
      judge / `reference_response`; `assertions`; `expected_trajectory`). `Builtin`
      trajectory evaluators need `expected_trajectory` on every scenario too.
- [ ] **Reference-driven trajectory evaluator ⇒ `expected_trajectory` on EVERY
      scenario.** Before emitting, count the scenarios that carry `expected_trajectory`
      and compare with the number of scenarios; if any scenario lacks it, either add the
      trajectory to every scenario or drop / replace the evaluator (a built-in trajectory
      evaluator, a `reference_trajectory` code rule, a judge reading
      `{expected_tool_trajectory}`). An evaluator cannot be scoped to some scenarios
      (`golden_test_ids` is always `[]`), so a partial set is rejected per missing
      scenario — one error line each (N-ARCH-obs10: 2 of 6 first proposals failed here).
      The reference-driven evaluators and the reference each one reads:
      `Builtin.TrajectoryExactOrderMatch` / `Builtin.TrajectoryInOrderMatch` /
      `Builtin.TrajectoryAnyOrderMatch` and a code check `reference_trajectory` →
      `expected_trajectory`; a code check `reference_response` (TRACE) → `expected_response`
      on every turn; a judge reading `{expected_tool_trajectory}` → `expected_trajectory`,
      `{expected_response}` → `expected_response`, `{assertions}` → `assertions`.
- [ ] **`[]` is not a trajectory (N17-obs295: a complete first proposal failed four times
      here).** An empty `expected_trajectory: []` counts as MISSING for every
      reference-driven evaluator — it does not mean "this scenario calls no tool". A
      refusal / red-line / injection scenario that should call no business tool therefore
      cannot sit under a `reference_trajectory` check. Never bundle a `reference_trajectory`
      check with a global prohibition in one code evaluator. Instead keep the deterministic
      prohibition as its own evaluator, put each scenario's required tools in that
      scenario's `assertions` ("Must: call `hr-database___get_employee` before computing"),
      and give scenarios that call tools a grounded `expected_trajectory` without seeding a
      `reference_trajectory` check (with no reference evaluator, `expected_trajectory` may
      be present on some scenarios only):

      ```json
      {"kind": "code", "key": "no_business_writes", "title": "No business writes",
       "name": "no_business_writes", "level": "SESSION",
       "description": "Deterministic prohibition of the payout write tool.",
       "rules": {"version": 1, "checks": [{"id": "forbid_payout", "type": "tool_set",
                                           "forbidden": ["hr-database___create_payout"]}]},
       "golden_test_ids": []}
      ```

      Add `{"id": "required_tools", "type": "reference_trajectory", "mode": "superset"}`
      only when EVERY scenario carries a non-empty `expected_trajectory`.
- [ ] **Recommendation keys agree.** `recommendation_keys` maps a `evaluator_recommend-
      ations` index (a digit string, 0-based, `< len(evaluator_recommendations)`) to seed
      evaluator `key`s that exist. An index with no prose entry, or an unknown key, fails.
- [ ] **Keys and names unique.** Evaluator `key`s unique; `name`s of judge / code
      evaluators unique; code check `id`s unique inside one rule set.
- [ ] **Each evaluator is complete and typed.** `kind: existing` → `evaluator_id` from the
      preamble's Evaluators list only. `kind: judge` → `name`, `level`
      (`TRACE|SESSION|TOOL_CALL`), `instructions` with ≥ 1 placeholder allowed at that
      level (`TRACE`/`TOOL_CALL`: `{context}` `{assistant_turn}` `{expected_response}`;
      `SESSION`: `{context}` `{available_tools}` `{actual_tool_trajectory}`
      `{expected_tool_trajectory}` `{assertions}`), `threshold` 0–1 if given. `kind: code` →
      `name`, `level` (`TRACE|SESSION`), `rules.checks[]` with EXACTLY the members its type
      takes (`tool_count` needs `min` and/or `max`, `min ≤ max`; `tool_sequence` needs
      `tools[]` unless `mode: exact`, mode `exact|subsequence`; `tool_set` needs `allowed`
      and/or `forbidden`; `output_*` needs `text`; `reference_response` at level TRACE;
      `reference_trajectory` at level SESSION, mode `superset|exact`). Every `description`
      ≤ 200 characters.
- [ ] **Entry shape (N-ARCH-obs11: 4 of 7 first proposals failed here).** The `name` of a
      `judge` / `derived` / `code` evaluator matches `^[a-zA-Z][a-zA-Z0-9_]{0,47}$` —
      letters, digits and underscores only, starting with a letter, ≤ 48 characters,
      **no hyphens** (write `helpdesk_guardrails_judge`, never `helpdesk-guardrails-judge`;
      `key`, `id` and `scenario_id` allow hyphens, `name` does not). A `kind: existing`
      entry carries no `description` — its members are `key`, `title`, `evaluator_id`,
      `golden_test_ids` and the optional `blocking`, `threshold`, `note`; put commentary
      in `note`. `description` exists only on `judge` / `derived` / `code` entries, and
      `judge.description` ≤ 200 characters — one sentence; put detail in the
      rubric/instructions (N-ARCH-obs14: a 201-character description cost a turn; Launchpad
      now trims an over-long `title` / `description` / `note` with an ellipsis and a
      `proposal_repaired` note instead of refusing, but never touches `name`,
      `instructions`, rating scales, code rules or scenario members — those stay errors).
- [ ] **Recommendation gates (WB-006: a stated gate used to draft as "informational").**
      Every recommendation that says *blocking* / *block publishing* / *gate* / 阻断 / 门禁,
      or names a threshold (*threshold 0.9*, 阈值 0.9, ≥ 0.9, 至少 0.9), is emitted as the
      object `{"text": …, "evaluator_id": …, "blocking": true, "threshold": <0–1>,
      "metric"?: …}` whose `evaluator_id` is a listed built-in id or the `key` / `name` of
      a seed evaluator in THIS proposal (or its index is in `recommendation_keys`).
      Launchpad carries the gate onto that entry and shows "blocking · threshold N"; a
      gate no entry can carry leaves the recommendation unresolved and the proposal
      invalid ("asks for a gate … that no evaluator carries"). A seed entry the
      recommendation gates should itself carry `blocking: true` and the `threshold`.
- [ ] **No capability conflict.** With any tool, KB, Skill or native tool mounted, no
      global `tool_count max: 0` without a `tool`, no exact empty `tool_sequence`; tool
      names in rules and `expected_trajectory` are runtime callable names of the
      selected attachments (never catalog keys).

## 6. Last look

- [ ] Only the members the protocol lists — the optional `evaluation_plan` seed and
      `fishbone` included, nothing else: evaluation assets are not created from this
      conversation.
- [ ] Every catalog reference (tool key, skill name, KB id) is copied from
      the preamble, not remembered or invented.
- [ ] The reply prose already shows the architecture, trade-offs, assumptions, manual
      tasks and the golden-test table — the proposal adds structure, not new facts.
- [ ] The JSON parses. Count the braces of the last golden test and the last array.
