# Default creation entrances to Bedrock Mantle GPT-5.6

Parent task. It owns the source requirement set, the child task map, and the
cross-child acceptance criteria. It has no direct implementation work of its own.

## Goal

Move the console's default model away from Claude and onto **Bedrock Mantle
`openai.gpt-5.6-sol`** for the two entrances that can express an arbitrary model,
and restructure the three creation entrances so the Claude-only path is clearly
the third, extensible-by-SDK option.

## Source requirement set (verbatim intent from the request)

1. **Managed Harness create form** gains a **Model source** selector:
   - `Bedrock Mantle` — "Offer models that use the Responses API or Chat
     Completions API." Model dropdown: `openai.gpt-5.6-sol`,
     `openai.gpt-5.6-terra`, `openai.gpt-5.6-luna`.
   - `Bedrock` — "Offer models that use the Converse API." Model dropdown:
     `global.anthropic.claude-sonnet-5`, `global.anthropic.claude-opus-5`,
     `global.amazon.nova-2-lite-v1:0`.
2. **Strands Studio / zip entrance** becomes the **second** creation method and
   also defaults to Bedrock Mantle `openai.gpt-5.6-sol`.
3. **Claude Agent SDK entrance** becomes the **third** method and is renamed
   **Other Agent SDK**, with `Claude Agent SDK` demoted to a second-level
   sub-option so further SDKs can be added later. Because the Claude Agent SDK
   can only drive Claude models, **its default model stays Claude — do not
   change it.**

## Resolved ambiguities

| Question | Resolution |
|---|---|
| `gpt-5.6` vs `gpt-5.7` (the request said both) | **`openai.gpt-5.6-sol` / `-terra` / `-luna`**, default `openai.gpt-5.6-sol`. Confirmed by the requester on 2026-07-31. |
| Mantle default across entrances | `openai.gpt-5.6-sol` everywhere a Mantle default is needed. |
| Bedrock (Converse) default | `global.anthropic.claude-sonnet-5` — unchanged from today's default, so the Bedrock branch is a no-op for existing agents. |

## Constraints

- These model ids are **not verifiable from this account** — `aws bedrock
  list-inference-profiles` does not show `gpt-5.6-*`, `claude-opus-5`, or
  `nova-2-lite-v1:0`. They are taken as given from the requester. Therefore no
  child task may make a *live* successful invocation of these models an
  acceptance criterion; acceptance is on the **request payload / generated
  code** being correct. Treat the catalogs as data, easy to correct later.
- `make verify` must pass (backend ruff+pytest, infra ruff+pytest, frontend
  eslint+tsc+vite build, i18n en↔zh-CN parity).
- Existing agents already deployed with `global.anthropic.claude-sonnet-5` must
  keep working, and re-publishing one must not silently change its model source.
- No new AWS bootstrap resource may be required. Specifically: do **not** take
  the `openAiModelConfig` / `geminiModelConfig` / keyed `liteLlmModelConfig`
  branches of `HarnessModelConfiguration` — they need an AgentCore Identity
  API-key credential provider ARN that this repo never provisions.

## Load-bearing facts established during planning

- **Harness**: `HarnessModelConfiguration` (botocore 1.43.44,
  `bedrock-agentcore-control`) is a union; `bedrockModelConfig` takes an
  optional `apiFormat` enum of `converse_stream | responses | chat_completions`
  and requires **no** API key. That single field *is* the Mantle-vs-Converse
  split the request describes, so both branches of the new selector stay on
  `bedrockModelConfig`. The repo currently emits
  `{"bedrockModelConfig": {"modelId": ...}}` with no `apiFormat`
  (`backend/app/deployer/harness.py:81`).
- **Zip / Studio**: the installed Strands SDK (1.47.0) supports
  `OpenAIResponsesModel(bedrock_mantle_config={"region": ...}, model_id=...)`,
  which mints a short-lived bearer token from the ambient AWS credential chain
  per request (`strands/models/_openai_bedrock.py:96-144`). The AgentCore
  execution role already holds `bedrock:InvokeModel` on `*`
  (`infra/stacks/base_stack.py:180-196`). So the zip path can reach Mantle with
  **no user-supplied `BEDROCK_API_KEY`** — unlike today's canvas codegen, which
  hardcodes `os.environ.get("BEDROCK_API_KEY")`.
- **Entrance cards** are four hand-written sibling JSX blocks in
  `frontend/src/pages/CreateAgent.tsx:911-986`; DOM order *is* the display
  order, and each carries its own `style={{ "--i": N }}` animation index. The
  card titled "Strands Studio" is the `zip_runtime` method; the canvas at
  `/create/studio` is the separate `studio` method.

## Child task map

| # | Task | Deliverable | Depends on |
|---|---|---|---|
| 1 | `07-31-harness-model-source` | `Model source` selector + model dropdowns on the Managed Harness form; new `AgentSpec.model_source` field; `apiFormat` on the CreateHarness/UpdateHarness payload | — |
| 2 | `07-31-zip-mantle-default` | Zip/Studio entrance defaults to Mantle `openai.gpt-5.6-sol`; strands zip template and canvas codegen emit `bedrock_mantle_config`; Mantle catalog + base-url path fix | Task 1 (reuses `model_source` and the shared model catalog) |
| 3 | `07-31-other-agent-sdk` | Card order → Harness, Strands Studio, Other Agent SDK, Discovery; container card renamed with a second-level SDK sub-option | — (touches the same file as 1 and 2 — sequence it last to avoid conflicts) |

Ordering: **1 → 2 → 3**. Task 3 is independent in substance but edits the same
region of `CreateAgent.tsx`, so running it last avoids rebasing the card JSX.

## Cross-child acceptance criteria

- [ ] Every child's own acceptance criteria pass, and `make verify` is green
      after each child (not just at the end).
- [ ] The creation page shows exactly four entrances in the order **Managed
      Harness → Strands Studio → Other Agent SDK → Discover existing runtimes**.
- [ ] Creating a Managed Harness agent with the default form selections produces
      a CreateHarness payload of
      `{"bedrockModelConfig": {"modelId": "openai.gpt-5.6-sol", "apiFormat": "responses"}}`.
- [ ] Creating a Strands Studio zip agent with the default form selections
      produces generated Python containing `OpenAIResponsesModel` with
      `bedrock_mantle_config` and `model_id="openai.gpt-5.6-sol"`, and **no**
      `BEDROCK_API_KEY` reference.
- [ ] Switching the Harness Model source to `Bedrock` restores
      `{"bedrockModelConfig": {"modelId": "global.anthropic.claude-sonnet-5", "apiFormat": "converse_stream"}}`.
- [ ] The Other Agent SDK entrance still deploys a Claude Agent SDK container
      whose model is a Claude id — unchanged from before this work.
- [ ] Re-publishing (`redeploy`) an agent created **before** this change keeps
      its original model id and lands on the Converse branch, with a regression
      test proving it.
- [ ] `docs/architecture.md` describes the model-source concept once, and the
      three entrances by their new names/order.
- [ ] en ↔ zh-CN i18n parity holds (`python3 scripts/i18n_check.py`).

## Out of scope

- Payments, Gateway, Memory, Evaluation, Governance surfaces.
- The vendored `apps/studio/` sub-app's own older model catalog (its defaults
  stay as they are; prefer platform-side integration per `CLAUDE.md`).
- Changing the evaluation/optimization/codegen (`settings.codegen_model`)
  default models.
- Provisioning an AgentCore Identity API-key credential provider.
