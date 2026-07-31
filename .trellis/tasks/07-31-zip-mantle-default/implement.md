# Implement — Strands Studio zip entrance defaults to Mantle GPT-5.6

**Prerequisite:** child 1 (`07-31-harness-model-source`) is merged —
`AgentSpec.model_source`, `frontend/src/lib/models.ts`, and the Model source
selector must already exist.

Backend template first (it is the part that decides whether Mantle works at
all), then the canvas, then the wizard default.

## Step 1 — Verify the SDK feature before building on it

- [ ] `cd backend && uv run python -c "import inspect, strands.models.openai_responses as m; print(inspect.signature(m.OpenAIResponsesModel.__init__))"`
      → must show `bedrock_mantle_config`.
- [ ] Read `strands/models/_openai_bedrock.py:23-64,96-144` and confirm the
      base-URL path split and the `BedrockMantleConfig` keys still match
      `design.md` §1. If the pinned SDK has moved, stop and revise the design.

## Step 2 — Zip template Mantle branch

- [ ] `backend/app/templates/strands_agent/main.py.tmpl`
  - [ ] After `MODEL_ID` (`:69`) add `MODEL_SOURCE = "__LAUNCHPAD_MODEL_SOURCE__"`
        and `MANTLE_REGION = os.environ.get("LAUNCHPAD_MANTLE_REGION", "us-east-1")`.
  - [ ] Add `build_model()` exactly as in `design.md` §3.1, with the
        function-local `OpenAIResponsesModel` import and a comment explaining
        why it is function-local and why there is no API key.
  - [ ] `:337` — `"model": MODEL_ID` → `"model": build_model()`.
- [ ] `backend/app/templates/strands_agent/__init__.py` — substitute
      `__LAUNCHPAD_MODEL_SOURCE__` from `spec.model_source` alongside the
      existing `__LAUNCHPAD_MODEL_ID__` replacement (`:41`).
- [ ] `backend/app/deployer/zip_runtime.py` — `_method_requirements`
      (`:307-313`): add `strands-agents[openai]>=1.0,<2` when
      `spec.model_source == "mantle"`. Leave the `studio`-method path alone
      (it brings its own `spec.requirements`).
- [ ] Check `backend/app/templates/strands_a2a_agent/` — it also substitutes
      `__LAUNCHPAD_MODEL_ID__` (`:20`). Decide and record: A2A stays Bedrock-only
      for now (the parent PRD does not ask for it), but it must not break when a
      spec carries `model_source: "mantle"`.

Validate: `cd backend && uv run ruff check . && uv run pytest -q`

## Step 3 — Backend tests

- [ ] `backend/tests/test_strands_template.py`
  - [ ] Mantle spec render: contains `OpenAIResponsesModel`,
        `bedrock_mantle_config`, `MODEL_ID = "openai.gpt-5.6-sol"`; does **not**
        contain `BEDROCK_API_KEY`; `compile()`s.
  - [ ] Bedrock spec render: model wiring unchanged from before (bare
        `MODEL_ID` string reaches `Agent`), `compile()`s.
- [ ] `backend/tests/test_zip_runtime_deployer.py` — packaged requirements
      include the `openai` extra for a Mantle spec and not for a Bedrock spec.

Validate: `cd backend && uv run pytest tests/test_strands_template.py tests/test_zip_runtime_deployer.py -q`

## Step 4 — Backend dependency for local debug

- [ ] Add `strands-agents[openai]>=1.0,<2` to `backend/pyproject.toml`
      dependencies.
- [ ] `cd backend && uv sync` — confirm no resolution conflict with
      `bedrock-agentcore[simulation]` (currently resolves strands 1.47.0).
- [ ] `uv run python -c "import openai, aws_bedrock_token_generator"` must
      succeed.
- [ ] **If it conflicts:** drop this step, and instead note in
      `docs/troubleshooting.md` that local debug of a Mantle flow needs
      `uv add strands-agents[openai]`. Record the decision in the task notes —
      this is the one cuttable step.

## Step 5 — Canvas catalog + base-url fix

- [ ] `frontend/src/studio/lib/models.ts`
  - [ ] `MANTLE_MODELS` → the six-entry list in `design.md` §4.1, with
        `openai.gpt-5.6-sol` first; keep the existing three ids so published
        flows do not fall into `isCustomMantleModel`.
  - [ ] `DEFAULT_MANTLE_MODEL_ID` follows `[0]` (no change needed if it stays
        derived).
  - [ ] Re-export the Mantle ids from `../../lib/models` so there is one source
        of truth for the GPT-5.6 ids.
  - [ ] `mantleBaseUrl(region, modelId?)` — add the `/openai/v1` vs `/v1` split
        from `_openai_bedrock.py:31-39`; update both existing call sites in
        `PropertyPanel.tsx` (`:272`, `:305`).

## Step 6 — Canvas codegen

- [ ] Extract one exported helper (e.g. `mantleModelArgs(data)`) in
      `frontend/src/studio/lib/` implementing the key-present/key-absent branch
      from `design.md` §4.3.
- [ ] Call it from all three emitters, replacing the duplicated blocks:
      `lib/code-generator.ts:1441-1461` (agent scope), `:1556-1574` (tool
      scope), `lib/graph-code-generator.ts:57-75`.
- [ ] Confirm the `OpenAIResponsesModel` import injection still fires on both
      branches (`code-generator.ts:74-77`, `graph-code-generator.ts:383-386`).
- [ ] `frontend/src/studio/FlowEditor.tsx:230-242` — agent-node drop default →
      Mantle provider, `modelId` **and** `modelName` = `DEFAULT_MANTLE_MODEL_ID`,
      `region: DEFAULT_MANTLE_REGION`. Remove the stale
      `modelName: 'Claude Sonnet 4.6'`.
- [ ] Do **not** touch the generators' destructuring fallbacks
      (`code-generator.ts:270,305,359,1083,1184,1302,1335`,
      `graph-code-generator.ts:432`) — see `design.md` §4.4.
- [ ] Do **not** touch `frontend/src/studio/lib/sample-flows/*.ts`.

## Step 7 — Canvas publish path

- [ ] `frontend/src/pages/CreateAgentStudio.tsx:171` — `missingApiKey` must stop
      counting Mantle nodes; OpenAI-provider nodes still count.
- [ ] `:135-144` — confirm the `strands-agents[openai]` extra is added for any
      Mantle node, key or not (it carries the token generator).
- [ ] `:150-173` — no change; `env.BEDROCK_API_KEY` is already only set from a
      non-empty node key.
- [ ] Update / retire the now-stale warning copy in both locales
      (`common.json:1769` and its zh-CN twin) and the
      `studio.prop.bedrockApiKey*` help text (`common.json:1878-1880`) so the
      key reads as an optional override.

## Step 8 — Codegen guidance docs (the AI-fix contract)

- [ ] `backend/app/codegen/guidance/flow_semantics.md:33,45,58` and
      `backend/app/codegen/guidance/contract_spec.md:158,175` — describe the
      `bedrock_mantle_config` form as the default and the keyed form as the
      override. These files are read by the LLM codegen/AI-Fix path, so a stale
      contract here silently regenerates the old shape.

## Step 9 — Zip wizard default

- [ ] `frontend/src/pages/CreateAgent.tsx` — flip
      `MODEL_SOURCE_BY_METHOD.zip_runtime` from `"bedrock"` to
      `DEFAULT_MODEL_SOURCE`, and update the comment above the table.
      **Child 1 deliberately left zip on `bedrock`** so that no commit ships a
      default the template cannot execute: at that point the template still
      handed the model id to Strands as a bare string, so a Mantle id would
      deploy cleanly and fail on first invoke. This step is the other half of
      that decision — do it only **after** Step 2 makes the template emit a
      Mantle model object, and in the same commit.
- [ ] Update the `zip_runtime` card's hard-coded spec line
      (`CreateAgent.tsx:955`, `pip (arm64) → zip → S3 → Runtime`) only if it
      became misleading — otherwise leave it for child 3.

## Step 10 — Docs

- [ ] `docs/architecture.md` — the zip/Studio path's model source and the
      IAM-only Mantle auth (no `BEDROCK_API_KEY`).
- [ ] `docs/studio-integration.md:93-99,203` — it already notes the token
      generator "Mantle auth needs"; make it the documented default and remove
      the plaintext-key exposure caveat where it no longer applies.

## Step 11 — Full gate

- [ ] `make verify`.
- [ ] Manual canvas check in `make dev` — generate code for three nodes: fresh
      Mantle (IAM form), Mantle with a pasted key (keyed form), Bedrock
      (unchanged). Confirm the import line appears in the first two.
- [ ] Tick every acceptance criterion in `prd.md`.

## Review gates

- After **Step 3**: the generated Mantle code shape is pinned by tests. Any
  disagreement about `bedrock_mantle_config` vs a keyed form must be settled
  here, before the canvas work multiplies it across three emitters.
- After **Step 6**: three emitters now share one helper. Diff the Bedrock-node
  output against `main` to prove the non-Mantle path is untouched.

## Rollback points

- Steps 2–4 are additive and gated on `model_source == "mantle"`, which defaults
  to `bedrock` — so a `bedrock`-source deploy is bit-identical to before.
- Step 6's helper keeps the keyed branch intact, so reverting Step 6 alone
  restores the old canvas output.
- Step 9 is a one-line default change; reverting it leaves the Mantle capability
  in place but off by default.
