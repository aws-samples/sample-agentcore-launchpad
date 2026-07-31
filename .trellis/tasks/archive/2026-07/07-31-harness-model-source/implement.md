# Implement — Harness model source selector

Ordered checklist. Backend first so the contract exists before the UI sends it.
Each numbered group is a safe stopping point.

## Step 1 — Backend schema + field plumbing

- [ ] `backend/app/schemas/agent.py`
  - [ ] Add `ModelSource = Literal["bedrock", "mantle"]` next to `Method` (`:13`).
  - [ ] Add `model_source: ModelSource = "bedrock"` immediately after
        `model_id` (`:134`). Docstring/comment must state *why* the default is
        `bedrock` and not `mantle` (backward compat for stored specs).
  - [ ] Do **not** add a cross-field validator (see `design.md` §2.2).
- [ ] `backend/app/services/harness_convert.py:316` — carry
      `model_source=source_spec.get("model_source") or AgentSpec.model_fields["model_source"].default`
      alongside the existing `model_id` line.

Validate: `cd backend && uv run ruff check . && uv run pytest -q`

## Step 2 — Harness payload

- [ ] `backend/app/deployer/harness.py`
  - [ ] Add module-level `_API_FORMAT = {"mantle": "responses", "bedrock": "converse_stream"}`
        and `_api_format(spec)`.
  - [ ] `build_create_params` (`:81`) — emit
        `{"bedrockModelConfig": {"modelId": spec.model_id, "apiFormat": _api_format(spec)}}`.
  - [ ] `_stage_generate` log line (`:229`) — include the source, e.g.
        `… · model {spec.model_id} ({spec.model_source})`.
  - [ ] Confirm no other site in the file touches the model.
- [ ] Do not touch `backend/app/services/agentcore/harness.py` —
      `wrap_params_for_update` already passes `model` through, and
      `UpdateHarnessRequest.model` is the same union shape.

## Step 3 — Backend tests

In `backend/tests/test_harness_deployer.py` (existing model assertion at `:27`):

- [ ] Mantle spec → `{"modelId": "openai.gpt-5.6-sol", "apiFormat": "responses"}`.
- [ ] Bedrock spec → `{"modelId": DEFAULT_MODEL_ID, "apiFormat": "converse_stream"}`.
- [ ] `AgentSpec(**spec_dict_without_model_source)` → `model_source == "bedrock"`
      and Converse payload.
- [ ] `wrap_params_for_update(...)["model"]` equals the create-time `model`
      (add near the existing `wrap_params_for_update` coverage around `:151`).

Validate: `cd backend && uv run pytest tests/test_harness_deployer.py -q`

## Step 4 — Shared frontend catalog

- [ ] New `frontend/src/lib/models.ts` with the exact exports in `design.md`
      §2.1: `ModelSource`, `ModelOption`, `MODEL_CATALOG`,
      `DEFAULT_MODEL_SOURCE`, `defaultModelFor`, `sourceOfModelId`,
      `CUSTOM_MODEL_OPTION`.
- [ ] Leave `frontend/src/studio/lib/models.ts` alone — child 2 reconciles it.
- [ ] Leave `frontend/src/pages/EvaluationEvaluators.tsx`'s `MODEL_OPTIONS`
      alone (out of scope).
- [ ] `frontend/src/lib/api.ts` — add `model_source?: ModelSource;` to
      `AgentSpecInput` (`:227-244`), importing the type from `./models`.

## Step 5 — Wizard UI

`frontend/src/pages/CreateAgent.tsx`, working through the touch-point table in
`design.md` §3.2:

- [ ] Delete `DEFAULT_MODEL` (`:27`); import from `../lib/models`.
- [ ] Add `modelSource` / `customModel` state (`near :424`).
- [ ] Method-change handler: `container` → force `bedrock` + Bedrock default
      model; other methods → `DEFAULT_MODEL_SOURCE` + its default model.
- [ ] New-agent reset (`:562`) and load-existing (`:700`) paths, including
        `StoredSpec.model_source?` (`:46-47`) and custom-id detection.
- [ ] `buildSpec()` (`:609`) — add `model_source: modelSource`.
- [ ] Replace the model `field` block (`:1052-1060`) with:
      Model source `selchips` (hidden when `method === "container"`,
      testids `model-source-mantle` / `model-source-bedrock`) → helper `note`
      → model `<select className="input">` over the source catalog +
      `Custom model ID…` → conditional free-text `input` (keep
      `id="agent-model"` and `className="input mono"`).

## Step 6 — i18n

- [ ] Reword `create.configure.model` in both locales (`common.json:235`) — it
      currently says `MODEL · BEDROCK` / `模型 · BEDROCK`, which becomes wrong.
- [ ] Add, in `en` and `zh-CN`, with identical key trees:
      `create.configure.modelSource`,
      `create.configure.modelSourceMantle` (= "Bedrock Mantle"),
      `create.configure.modelSourceMantleDesc`
      (= "Offer models that use the Responses API or Chat Completions API."),
      `create.configure.modelSourceBedrock` (= "Bedrock"),
      `create.configure.modelSourceBedrockDesc`
      (= "Offer models that use the Converse API."),
      `create.configure.modelCustom` (= "Custom model ID…").

Validate: `python3 scripts/i18n_check.py`

## Step 7 — Docs

- [ ] `docs/architecture.md` — in the Managed Harness (方式B) section, record
      that the model source maps to `bedrockModelConfig.apiFormat`
      (`responses` vs `converse_stream`) and that no API key is involved.
      One short paragraph; keep the bilingual convention of that file.

## Step 8 — Full gate

- [ ] `make verify` (backend ruff+pytest, infra ruff+pytest, frontend
      eslint+tsc+vite build, i18n parity).
- [ ] Re-read the acceptance criteria in `prd.md` and tick each one.

## Review gates

- After **Step 3**: the wire shape is decided and pinned by tests. If
  `apiFormat: converse_stream` on the Bedrock branch feels risky, resolve it
  here, before any UI exists.
- After **Step 6**: the whole feature is user-visible. Check the container
  carve-out by eye — the Claude Agent SDK path must look exactly as it did.

## Rollback points

- Steps 1–3 are additive and default to today's behavior; reverting Step 2's
  three-line change restores the previous payload exactly.
- Step 5 is the only user-visible change; reverting that one file restores the
  free-text model input while leaving the backend field harmlessly in place.

## Out of scope reminders

- Zip/Studio template honoring `model_source` → `07-31-zip-mantle-default`.
- Card order / `Other Agent SDK` rename → `07-31-other-agent-sdk`.
- `maxTokens` / `temperature` / `topP` on the harness model config.
