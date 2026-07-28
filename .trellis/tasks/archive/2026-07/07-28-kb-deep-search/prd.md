# PRD — `kb_deep_search`: agentic retrieval for the container + ZIP methods

## Problem

Task `07-28-kb-attach-container-zip` gave `zip_runtime` and `container` agents a
generated `kb_search` tool over `bedrock-agent-runtime:Retrieve`, and deliberately left
multi-step agentic retrieval as a harness-only advantage. That was a scope decision, not
a technical limit — `agentic_retrieve_stream` is a plain data-plane API on the same
`bedrock-agent-runtime` client, live-verified against `lab-fund-kb` on 2026-07-28.

Single-shot `Retrieve` is the wrong shape for a whole class of questions: cross-document
or cross-KB comparison, "list everything that…", summarisation, and anything whose
evidence is scattered across a document. The harness channel answers those with
`…___AgenticRetrieveStream`; the two code methods currently cannot.

## Goal

Give the generated agents a **second** retrieval tool, `kb_deep_search`, backed by
`AgenticRetrieveStream`, alongside the existing fast `kb_search` — and steer the model
between them from the generated system prompt.

## Scope

In scope:
1. `kb_deep_search(query, kb_id="")` in both templates
   (`strands_agent` native `@tool`, `claude_sdk_agent` in the same in-process
   `launchpad_kb` SDK-MCP server), sharing derivations from
   `app/templates/kb_support.py`.
2. Both tools coexist (product decision, river, 2026-07-28): `kb_search` stays the cheap
   default (~0.9s, no FM call), `kb_deep_search` is the expensive planner. The generated
   `## Knowledge bases` prompt section tells the model when to pick which. Two separate
   tool names (not one `deep=` flag) so the config-bundle A/B contract can tune each
   description independently.
3. Tool output carries **both** halves of the `result` event — the service-synthesized
   citation-backed answer *and* the deduplicated source chunks (one call returns both, so
   there is nothing to trade off).
4. `maxAgentIteration` derived from how many retrievers the call actually uses (AWS
   guidance: 3 for a single KB, 4–5 for multi).
5. IAM: `launchpad-agent-execution-role` += `bedrock:AgenticRetrieveStream`. **This
   action cannot be resource-scoped** — it must be granted on `*`, mirroring what
   `launchpad-gateway-role` already carries for the harness channel.
6. Tests: hermetic unit tests over a stubbed event stream (both templates) + infra synth
   assertion + a real-AWS deploy/invoke round.
7. Docs: lab 02 capability table + lab 04 §4.7, `.trellis/spec/launchpad/managed-kb.md`,
   and the frontend `kbNoteDirect` copy (en + zh-CN) which currently claims
   「仅单次检索（无 agentic 多步）」.

Out of scope:
- `studio` canvas and `protocol=a2a` — still rejected upstream by
  `AgentSpec._kb_method_supported`; unchanged.
- Custom planning/reranking models, guardrail `policyConfiguration`, `userContext`,
  `retrievalOverrides` filters, `nextToken` paging — all left at MANAGED/defaults.
- Streaming the planner's progress out to the console. Trace events are consumed for a
  one-line step summary only; the SSE/console contract does not change.
- Any change to `kb_search` behaviour or to the harness gateway channel.

## Acceptance criteria

- [x] A rendered Strands `main.py` with KBs mounted registers **both** `kb_search` and
      `kb_deep_search`; with no KBs mounted it registers neither. Compiles either way.
- [x] A rendered container `main.py` exposes both tools through the single
      `launchpad_kb` SDK-MCP server (so `ALLOWED_TOOLS` needs no new entry).
- [x] The generated `## Knowledge bases` prompt section names both tools and says which
      to prefer for open/comparative questions vs single-fact lookups.
- [x] `DEFAULT_TOOL_DESCRIPTIONS` (Strands) carries an entry per tool, so an A/B bundle
      can retune either one; spec `tool_description_overrides` still win.
- [x] `kb_deep_search` output contains the synthesized answer, the citation count, and
      the deduped chunks with their `sourceRetriever` id + `_source_uri`.
- [x] `maxAgentIteration` is 3 when the call targets one KB and 5 when it targets more.
- [x] An error member arriving **inside** the event stream (e.g.
      `accessDeniedException`) and an exception raised by the call itself both come back
      as readable text — never an exception that aborts the turn.
- [x] An unknown `kb_id` is refused without issuing a request, listing the mounted ids
      (same contract as `kb_search`).
- [x] CDK synth carries `bedrock:AgenticRetrieveStream` on the agent execution role.
- [x] `make verify` green.
- [x] Real AWS: one zip and one container agent deployed with a managed KB mounted; a
      comparison-style question routes to `kb_deep_search`, the answer is grounded, and
      the trace shows the tool call. Evidence in the task `research/` dir.
- [x] lab 02 / lab 04 / `managed-kb.md` / `kbNoteDirect` (en + zh-CN) no longer say the
      code methods have no agentic retrieval.

## Risks / non-goals

- **`bedrock:AgenticRetrieveStream` on `*` widens the shared execution role.** Every
  Launchpad runtime gains the ability to run agentic retrieval against any KB in the
  account. This already holds for the gateway role; accept it and record it in the spec
  so it is a known, deliberate grant rather than a surprise in an audit.
- Latency and cost are materially higher than `kb_search` (one FM call per planning
  round). Mitigated by keeping `kb_search` as the default and saying so in the prompt.
- The IAM change again needs an explicit `cd infra && uv run cdk deploy` —
  `make bootstrap` only runs CDK when the stack is missing.
