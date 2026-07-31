# Strands Studio zip entrance defaults to Mantle GPT-5.6

Child 2 of `07-31-mantle-default-models`. Read the parent `prd.md` first.
**Depends on child 1 (`07-31-harness-model-source`)**, which introduces
`AgentSpec.model_source`, the shared `frontend/src/lib/models.ts` catalog, and
the Model source selector this task reuses.

## Goal

Make the Strands Studio / zip creation path default to **Bedrock Mantle
`openai.gpt-5.6-sol`** and actually work there — which means the generated Python
must reach the Mantle endpoint. Today the zip template hardcodes
`Agent(model=MODEL_ID)` (a bare string, always resolved as a Bedrock Converse
model), and the only Mantle-capable path in the repo — the canvas codegen —
requires the operator to paste a `BEDROCK_API_KEY`.

The enabling fact: the installed Strands SDK (1.47.0) accepts
`OpenAIResponsesModel(bedrock_mantle_config={"region": ...}, model_id=...)`,
which mints a short-lived bearer token from the ambient AWS credential chain per
request (`strands/models/_openai_bedrock.py:96-144`). The AgentCore execution
role already has `bedrock:InvokeModel` on `*`
(`infra/stacks/base_stack.py:180-196`). So **no user-supplied API key is
needed**, and the SDK also computes the endpoint path itself — which fixes a
latent bug in the repo's own URL templating.

## Requirements

### R1 — Zip wizard defaults to Mantle

- The `zip_runtime` method's step-2 panel shows the Model source selector built
  in child 1, defaulting to **Bedrock Mantle / `openai.gpt-5.6-sol`**.
- Selecting `Bedrock` still produces today's behavior exactly.

### R2 — Zip template honors `model_source`

- `backend/app/templates/strands_agent/` renders a model **object** when
  `spec.model_source == "mantle"`:
  `OpenAIResponsesModel(bedrock_mantle_config={"region": <mantle region>}, model_id=MODEL_ID)`;
  and keeps passing the bare `MODEL_ID` string when `model_source == "bedrock"`.
- Rendered output must always compile (that is an existing invariant of this
  template renderer).
- The Mantle region is configurable and defaults to `us-east-1` — **not**
  `AWS_REGION`, because the runtime deploys to `us-west-2` while the repo's
  existing Mantle support documents these models as hosted in `us-east-1`.
- The zip's `requirements.txt` gains the `openai` Strands extra when the source
  is Mantle, so `aws-bedrock-token-generator` and `openai` are present in the
  package. Follow the precedent already used by the canvas publish path
  (`frontend/src/pages/CreateAgentStudio.tsx:135-144`).
- `harness_convert.py`'s harness→zip conversion (which child 1 makes carry
  `model_source`) produces working code for a Mantle-sourced harness.

### R3 — Canvas: no API key required, GPT-5.6 available, default is Mantle

- `MANTLE_MODELS` gains `openai.gpt-5.6-sol` / `-terra` / `-luna`, and
  `DEFAULT_MANTLE_MODEL_ID` becomes `openai.gpt-5.6-sol` (it is currently
  `xai.grok-4.3`).
- Mantle codegen emits `bedrock_mantle_config` instead of
  `client_args={"api_key": os.environ.get("BEDROCK_API_KEY"), "base_url": ...}`
  **when no explicit key is set on the node**. A node with an explicit `apiKey`
  keeps the current keyed/`base_url` behavior, so existing published flows are
  unaffected.
- A newly dropped agent node defaults to the Mantle provider with
  `openai.gpt-5.6-sol`.
- The `missingApiKey` publish warning no longer fires for Mantle nodes that rely
  on IAM.
- Fix `mantleBaseUrl()`: only `openai.gpt-5.*` model ids live under
  `/openai/v1`; everything else (e.g. `xai.grok-4.3`) is `/v1`
  (`strands/models/_openai_bedrock.py:31-39`). This only affects the
  explicit-key branch now, but it is wrong today for the current default model.
- Keep the two codegen guidance docs
  (`backend/app/codegen/guidance/flow_semantics.md:58`,
  `contract_spec.md:175`) in step with the emitted contract — the AI-fix/codegen
  path reads them as the spec.

### R4 — Local debug still works for the new default

`backend/app/services/local_exec.py` runs generated code with the backend's own
interpreter. Neither `openai` nor `aws-bedrock-token-generator` is installed in
`backend/.venv` today, so locally debugging a Mantle flow fails on import. Add
the `openai` Strands extra to the backend's dependencies so the **default**
creation path is locally debuggable.

## Acceptance criteria

- [ ] Zip wizard with untouched defaults → spec `{model_source: "mantle",
      model_id: "openai.gpt-5.6-sol"}`.
- [ ] Rendering that spec through the strands template produces code containing
      `OpenAIResponsesModel`, `bedrock_mantle_config`,
      `model_id`/`MODEL_ID = "openai.gpt-5.6-sol"`, and **no**
      `BEDROCK_API_KEY` — asserted in `backend/tests/test_strands_template.py`.
- [ ] The rendered module still `compile()`s (existing template invariant).
- [ ] The same spec's packaged `requirements.txt` includes the `openai` Strands
      extra; a `bedrock`-source spec does not.
- [ ] A `bedrock`-source zip spec renders byte-identical model wiring to before
      this change (`Agent(model=MODEL_ID)` with a bare string) — regression test.
- [ ] Canvas: a fresh agent node with no API key generates
      `OpenAIResponsesModel(bedrock_mantle_config={...}, model_id="openai.gpt-5.6-sol", ...)`
      and imports `from strands.models.openai_responses import OpenAIResponsesModel`.
- [ ] Canvas: a node **with** an explicit `apiKey` still generates the
      `client_args` form, and `mantleBaseUrl` returns `/v1` for `xai.grok-4.3`
      and `/openai/v1` for `openai.gpt-5.6-sol`.
- [ ] Publishing a Mantle canvas flow with no key raises no `missingApiKey`
      warning and sets no `BEDROCK_API_KEY` env on the runtime.
- [ ] `make verify` green, i18n parity holds.

## Non-goals

- Migrating the 11 curated sample flows in
  `frontend/src/studio/lib/sample-flows/*.ts` off Claude. They hard-code
  `global.anthropic.claude-sonnet-5` deliberately and several demonstrate
  Claude-specific features (cache points, effort tiers) that would break on a
  Responses-API model. They stay as they are.
- The vendored `apps/studio/` sub-app's own older catalog and Claude 3.7 default.
- Removing the `BEDROCK_API_KEY` support path entirely — it stays as an
  explicit override.
- The `container` / Claude Agent SDK path (its model stays Claude, per the
  parent PRD).
