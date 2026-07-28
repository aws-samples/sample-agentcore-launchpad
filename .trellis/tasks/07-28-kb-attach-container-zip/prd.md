# PRD — Mount managed knowledge bases on the container (方式A) and Strands ZIP methods

## Problem

`docs/lab/02-deploy-runtime.md` (capability table, row 「挂载知识库（托管 RAG）」) states
that KB mounting is **harness-only** — `支持，仅此方式` for 方式B, `不支持` for the Claude
Agent SDK container (方式A) and Strands ZIP / Studio canvas (方式C). That is enforced in
code by `AgentSpec._kb_needs_harness` (`backend/app/schemas/agent.py`).

The restriction exists because the only implemented mount path is the managed-Harness
`agentcore_gateway` tool attachment to `launchpad-kb-gw` with OAuth CLIENT_CREDENTIALS —
a channel self-written runtimes (container/zip) do not have. It is not an AWS limitation:
the same managed KBs are retrievable through the IAM-authenticated data-plane API
`bedrock-agent-runtime:Retrieve`, which the console's KB Playground already uses
(`app/services/knowledge.query`).

Users therefore cannot combine RAG with either of the two code-generating methods, even
though those are the methods that support A/B config bundles, canary and custom tools.

## Goal

Let an agent created with **方式A (container)** or the **Strands ZIP fast path**
(`method=zip_runtime`, `protocol=http`) mount the same managed KBs the harness can mount,
selected in the same CreateAgent picker, and actually retrieve from them at invoke time.

## Scope

In scope:
1. `method in {harness, zip_runtime, container}` accepted by `AgentSpec` for
   `knowledge_bases`.
2. Retrieval channel for the two new methods: **direct `bedrock-agent-runtime:Retrieve`
   from inside the generated agent code, authenticated by the shared runtime execution
   role** (product decision, river, 2026-07-28 — chosen over reusing `launchpad-kb-gw`
   over MCP + AgentCore Identity M2M tokens because it needs no token lifecycle, no
   secret in the runtime, and is unit-testable).
   - Strands ZIP: a native `@tool kb_search` in `app/templates/strands_agent/main.py.tmpl`.
   - Container: the same retrieval helper exposed as an in-process SDK-MCP tool
     (`create_sdk_mcp_server`) in `app/templates/claude_sdk_agent/main.py.tmpl`.
3. A generated system-prompt section that tells the model which KBs are mounted, what
   each is for, and which tool to call — mirroring the harness `_kb_prompt` contract.
4. IAM: `launchpad-agent-execution-role` += `bedrock:Retrieve` + `bedrock:GetKnowledgeBase`
   on `knowledge-base/*`.
5. Frontend: the CreateAgent KB picker becomes available for all three wizard methods,
   with per-method explanatory copy (en + zh-CN parity).
6. Tests: hermetic backend/infra unit tests + a real-AWS end-to-end check (deploy one zip
   agent and one container agent with a KB mounted, invoke, assert grounded answer).
7. Docs: `docs/lab/02-deploy-runtime.md` capability table + `docs/lab/04-capabilities.md`
   mounting-path note, and `.trellis/spec/launchpad/managed-kb.md`.

Out of scope (explicitly):
- **Strands Studio canvas (`method=studio`)** — the user deferred it; the validator keeps
  rejecting it, and the reason is recorded in the spec.
- **`protocol=a2a` zip runtimes** — a separate template (`strands_a2a_agent`); rejected
  with its own message so the constraint is explicit rather than silently ignored.
- Agentic (multi-step) retrieval / `AgenticRetrieveStream` for the new methods — the
  harness keeps that advantage; the new methods get single-shot `Retrieve`.
- Any change to the harness mount path or to the `launchpad-kb-gw` topology.

## Acceptance criteria

- [x] `POST /api/agents` with `method=zip_runtime` or `container` + `knowledge_bases` is
      accepted (no 422); `method=studio` and `protocol=a2a` still 422 with a message that
      names the actual constraint.
- [x] The rendered Strands `main.py` for a spec with 2 KBs compiles, registers a
      `kb_search` tool, carries both kb ids/names/descriptions, and appends a
      「Knowledge bases」 system-prompt section naming `kb_search`. With no KBs,
      `MOUNTED_KBS == []`, no prompt section, and `kb_search` is **not** registered on the
      agent (the helper stays in the file — the template is one valid Python module with
      placeholders, not conditionally spliced text).
- [x] The rendered container `main.py` compiles, builds an SDK-MCP server named
      `launchpad_kb`, and `ALLOWED_TOOLS` contains `mcp__launchpad_kb`. With no KBs,
      unchanged from today.
- [x] `kb_search` returns formatted passages with score + source URI, and a readable
      message (not a traceback) when the KB is missing or access is denied.
- [x] The CDK synth of `launchpad-base` contains `bedrock:Retrieve` on the agent
      execution role.
- [x] Deleting a KB with `force` does not create/patch a `launchpad-kb-gw` agentic target
      for a non-harness agent.
- [x] CreateAgent lets a container/zip agent select KBs; i18n parity passes.
      (EDIT-restore verified by code path only, not in a browser: `loadForEdit` sets
      `selectedKbs` from `spec.knowledge_bases` unconditionally, and the
      `method !== "harness" → setSelectedKbs([])` effect that used to wipe it on the
      `setMethod(spec.method)` of an edit load is now gone.)
- [x] `make verify` green.
- [x] Real AWS: one `zip_runtime` and one `container` agent deployed with a managed KB
      mounted, each invoked with a question only answerable from the indexed document,
      both answers grounded in retrieved content; job logs + invoke output captured under
      the task `research/` dir.
- [x] `docs/lab/02-deploy-runtime.md` row 23 no longer says 不支持 for 方式A / ZIP, and
      explains that the three methods reach KBs by different channels.

## Non-goals / risks

- The IAM statement lands only after `make bootstrap` (CDK deploy) — an existing
  deployment retrieves nothing until the stack is updated. The lab doc and the
  troubleshooting table must say so.
- Two coexisting retrieval mechanisms (gateway for harness, direct API for the rest) is
  accepted complexity; it is documented in `managed-kb.md` so nobody "unifies" it by
  accident.
