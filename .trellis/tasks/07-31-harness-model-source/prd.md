# Harness model source selector (Bedrock Mantle / Bedrock)

Child 1 of `07-31-mantle-default-models`. Read the parent `prd.md` first — it
holds the source requirement set, the resolved model ids, and the cross-child
acceptance criteria.

## Goal

Let the operator choose a **Model source** when creating a Managed Harness agent,
and make **Bedrock Mantle `openai.gpt-5.6-sol`** the default. Today the model is
a single free-text input hard-wired to `global.anthropic.claude-sonnet-5`
(`frontend/src/pages/CreateAgent.tsx:27,1052-1060`) that always lands on the
Converse API.

This child also builds the two shared pieces the next child reuses: the
platform-side **model catalog** and the backend **`AgentSpec.model_source`**
field.

## Requirements

### R1 — Model source selector

On the step-2 configure panel, above the model field, add a **Model source**
control with exactly two options:

| Option | Helper text (as specified by the requester) | Models offered |
|---|---|---|
| `Bedrock Mantle` (default) | Offer models that use the Responses API or Chat Completions API. | `openai.gpt-5.6-sol` (default), `openai.gpt-5.6-terra`, `openai.gpt-5.6-luna` |
| `Bedrock` | Offer models that use the Converse API. | `global.anthropic.claude-sonnet-5` (default), `global.anthropic.claude-opus-5`, `global.amazon.nova-2-lite-v1:0` |

- The model field becomes a **dropdown** over the selected source's catalog, plus
  a `Custom model ID…` escape hatch that reveals the current free-text input.
- Switching source re-seeds the model to that source's default.
- The selector is visible for the **Managed Harness** and **Strands Studio /
  zip** methods. It is **hidden for the `container` (Claude Agent SDK) method**,
  which is pinned to `Bedrock` + its existing Claude default — the Claude Agent
  SDK can only drive Claude models.

### R2 — Round-trip an existing agent

- Loading an existing agent into the wizard (edit / re-publish) must show its
  stored source and model. A stored model id that is not in either catalog must
  render through the `Custom model ID…` branch with the id preserved verbatim.
- An agent stored **without** a `model_source` (i.e. every agent that exists
  today) must resolve to `Bedrock` / Converse, never to Mantle.

### R3 — Backend field

- `AgentSpec` gains `model_source: Literal["bedrock", "mantle"] = "bedrock"`.
  The default is `bedrock` **for backward compatibility** — it is the frontend
  form, not the schema, that defaults to Mantle.
- `frontend/src/lib/api.ts::AgentSpecInput` gains the matching optional field.
- `harness_convert.py` carries `model_source` through the harness→zip conversion
  alongside `model_id`.

### R4 — CreateHarness / UpdateHarness payload

- `backend/app/deployer/harness.py` emits
  `{"bedrockModelConfig": {"modelId": <id>, "apiFormat": <fmt>}}` where `fmt` is
  `responses` for Mantle and `converse_stream` for Bedrock.
- Stay on the `bedrockModelConfig` union branch for **both** sources. Do not use
  `openAiModelConfig` / `liteLlmModelConfig` — they require an AgentCore
  Identity API-key credential provider ARN this repo never provisions.
- `apiFormat` is carried per catalog entry (not hard-coded per source) so a
  future `chat_completions` model needs a catalog line and nothing else.
- The generate-stage log line (`harness.py:229`) mentions the source.

### R5 — i18n

New keys under `create.configure.*` in both `en` and `zh-CN`, and reword the
existing `create.configure.model` label, which currently reads
`MODEL · BEDROCK` / `模型 · BEDROCK` (`frontend/src/locales/*/common.json:235`)
and would now be wrong.

## Acceptance criteria

- [ ] Managed Harness form, untouched defaults → `POST /api/agents` body carries
      `model_source: "mantle"`, `model_id: "openai.gpt-5.6-sol"`.
- [ ] `build_create_params` for that spec returns
      `{"bedrockModelConfig": {"modelId": "openai.gpt-5.6-sol", "apiFormat": "responses"}}`
      — asserted by a new case in `backend/tests/test_harness_deployer.py`.
- [ ] Same, with source `Bedrock` → `{"modelId": "global.anthropic.claude-sonnet-5", "apiFormat": "converse_stream"}`.
- [ ] `AgentSpec(**{...no model_source...})` yields `model_source == "bedrock"`
      and the Converse payload — regression test for pre-existing agents.
- [ ] `wrap_params_for_update` passes `model` through unchanged, so re-publish
      keeps the chosen source — asserted.
- [ ] Selecting `Custom model ID…` sends the typed id verbatim, and loading an
      agent whose id is in neither catalog renders in the custom branch.
- [ ] The Claude Agent SDK (`container`) method shows **no** Model source
      control and still defaults to a Claude model id.
- [ ] `make verify` green, including `python3 scripts/i18n_check.py`.

## Non-goals

- The zip/Studio template and canvas codegen honoring `model_source` — that is
  child 2 (`07-31-zip-mantle-default`). This child only has to make the field
  exist, be selectable, and be persisted.
- Reordering or renaming the entrance cards — child 3.
- `maxTokens` / `temperature` / `topP` / `additionalParams` on the harness model
  config. They are available on the AWS shape but out of scope here.
