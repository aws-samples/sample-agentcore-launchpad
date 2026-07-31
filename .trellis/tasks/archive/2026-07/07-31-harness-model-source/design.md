# Design — Harness model source selector

## 1. The one insight that shapes this design

`HarnessModelConfiguration` (botocore 1.43.44, `bedrock-agentcore-control`) is a
union of four branches, and `bedrockModelConfig` already carries the field that
*is* the Mantle-vs-Converse distinction:

```
HarnessBedrockModelConfig = {
  modelId (required), maxTokens, temperature, topP,
  apiFormat: enum[ converse_stream | responses | chat_completions ],
  additionalParams,
}
```

Verified directly against the vendored service model. Crucially `apiFormat` is
**optional and needs no API key**, unlike `openAiModelConfig` (requires
`apiKeyArn`, an AgentCore Identity credential-provider ARN) and
`liteLlmModelConfig` (requires `apiBase`).

So the requester's two options map onto one union branch:

| UI Model source | union branch | `apiFormat` |
|---|---|---|
| Bedrock Mantle | `bedrockModelConfig` | `responses` (or `chat_completions`) |
| Bedrock | `bedrockModelConfig` | `converse_stream` |

That is a **one-key change** at `backend/app/deployer/harness.py:81`, no bootstrap
resource, no secret, no IAM change. Everything else in this task is UI and field
plumbing.

## 2. Boundaries and contracts

### 2.1 New shared frontend module — `frontend/src/lib/models.ts`

The repo currently has **three** unrelated copies of
`"global.anthropic.claude-sonnet-5"` on the frontend
(`pages/CreateAgent.tsx:27`, `studio/lib/models.ts:14`, plus
`pages/EvaluationEvaluators.tsx`'s own `MODEL_OPTIONS`). This task introduces the
platform-side catalog that the wizard uses; child 2 makes
`studio/lib/models.ts` derive its Mantle list from it. `EvaluationEvaluators` is
left alone (out of scope).

```ts
export type ModelSource = "mantle" | "bedrock";

export interface ModelOption {
  model_id: string;
  label: string;
  /** Harness bedrockModelConfig.apiFormat for this model. */
  api_format: "converse_stream" | "responses" | "chat_completions";
}

export const MODEL_CATALOG: Record<ModelSource, ModelOption[]> = {
  mantle: [
    { model_id: "openai.gpt-5.6-sol",   label: "GPT-5.6 Sol",   api_format: "responses" },
    { model_id: "openai.gpt-5.6-terra", label: "GPT-5.6 Terra", api_format: "responses" },
    { model_id: "openai.gpt-5.6-luna",  label: "GPT-5.6 Luna",  api_format: "responses" },
  ],
  bedrock: [
    { model_id: "global.anthropic.claude-sonnet-5",  label: "Claude Sonnet 5 (global)",  api_format: "converse_stream" },
    { model_id: "global.anthropic.claude-opus-5",    label: "Claude Opus 5 (global)",    api_format: "converse_stream" },
    { model_id: "global.amazon.nova-2-lite-v1:0",    label: "Nova 2 Lite (global)",      api_format: "converse_stream" },
  ],
};

export const DEFAULT_MODEL_SOURCE: ModelSource = "mantle";
export const CLAUDE_SDK_MODEL_SOURCE: ModelSource = "bedrock";
export function defaultModelFor(source: ModelSource): string;
export function sourceOfModelId(id: string): ModelSource | null; // null ⇒ custom
export const CUSTOM_MODEL_OPTION = "__custom__";
```

`api_format` lives on the catalog entry rather than being derived from the source
so that adding a `chat_completions` Mantle model later is a data change only. The
**frontend does not send `api_format`** — the backend derives it (§2.3); the field
exists so the UI can label/group options and so one table stays the single
readable source of truth.

### 2.2 Backend schema — `backend/app/schemas/agent.py`

```python
ModelSource = Literal["bedrock", "mantle"]
...
model_id: str = DEFAULT_MODEL_ID          # unchanged
model_source: ModelSource = "bedrock"     # NEW
```

**Why the schema default is `bedrock`, not `mantle`:** the ledger stores agent
specs as JSON. Every row that exists today has no `model_source`, and every one
of them is a Converse-API Claude/Nova agent. A `mantle` schema default would
silently flip all of them to the Responses API on their next re-publish. The
Mantle default is a *form* default, applied in `CreateAgent.tsx`. This is the
same reasoning as the parent PRD's "existing agents keep working" constraint.

No cross-field validator. `model_source` and `model_id` are deliberately
independent: the id space is not closed (custom ids are a first-class feature),
and the account cannot enumerate valid ids, so any validator would be guesswork
that blocks legitimate use. A wrong pairing surfaces as an AWS
`CREATE_FAILED` + `failureReason`, which `wait_harness_ready`
(`backend/app/services/agentcore/harness.py:49-68`) already reports.

### 2.3 Harness payload — `backend/app/deployer/harness.py`

`build_create_params` (`:58-179`) is the only place the model reaches the wire.
Replace the literal at `:81`:

```python
"model": {"bedrockModelConfig": {
    "modelId": spec.model_id,
    "apiFormat": _api_format(spec),
}},
```

with a module-level helper:

```python
_API_FORMAT = {"mantle": "responses", "bedrock": "converse_stream"}

def _api_format(spec: AgentSpec) -> str:
    return _API_FORMAT[spec.model_source]
```

A dict keyed on the literal, not an `if`, so a third source is one line.

`apiFormat` is emitted **explicitly for both branches**, including
`converse_stream`. That is what the service already does implicitly today, so it
is semantically a no-op for existing agents, and it makes the payload
self-describing and directly assertable in tests. Rollback if the preview API
ever rejects it: omit the key when `model_source == "bedrock"` (one line).

`wrap_params_for_update` (`agentcore/harness.py:26-38`) copies every key except
`harnessName`, and `UpdateHarnessRequest.model` is the same union shape — so
re-publish needs **zero** changes. Verified against the service model; a test
pins it.

### 2.4 Data flow

```
CreateAgent.tsx  (modelSource state, default "mantle")
      │  buildSpec():  { model_id, model_source, ... }
      ▼
api.ts AgentSpecInput.model_source?: "bedrock" | "mantle"
      ▼
POST /api/agents → AgentSpec (model_source default "bedrock")
      ▼  ledger: Deployment.spec JSON  (round-trips for edit / resume / redeploy)
      ▼
deployer/harness.py build_create_params
      ▼
{"bedrockModelConfig": {"modelId": …, "apiFormat": "responses" | "converse_stream"}}
```

Nothing else in the pipeline reads the model. `_stage_provision` rebuilds params
when KBs are mounted (`harness.py:266`) and `_stage_deploy` rebuilds on resume
(`:288-294`) — both go through `build_create_params`, so the mapping applies
uniformly and idempotently, which the resumable-job contract requires.

## 3. Frontend design

### 3.1 UI shape

Follow the two existing in-page precedents rather than inventing anything:

- **Model source** → `selchips` two-chip control, exactly like the `zip_runtime`
  protocol selector at `CreateAgent.tsx:1179-1204`. `data-testid="model-source-mantle"`
  / `"model-source-bedrock"` to match that file's testid convention.
- **Model** → `<select className="input">` with
  `<option style={{ background: "#141816" }}>`, the platform convention from
  `pages/EvaluationEvaluators.tsx:357-372`. Last option is
  `Custom model ID…` (`CUSTOM_MODEL_OPTION`); choosing it reveals the existing
  `<input id="agent-model" className="input mono">` unchanged.

Helper text under the chips comes from the source's i18n description (the
"Responses API or Chat Completions API" / "Converse API" wording), rendered in
the existing `note` block style used at `:1205`.

### 3.2 State

```ts
const [modelSource, setModelSource] = useState<ModelSource>(DEFAULT_MODEL_SOURCE);
const [customModel, setCustomModel] = useState(false);
```

Touch points (all in `CreateAgent.tsx`):

| Site | Today | Change |
|---|---|---|
| `:27` | `const DEFAULT_MODEL = "global.anthropic.claude-sonnet-5"` | delete; import from `lib/models` |
| `:421` | `useState<Method>("harness")` | on method change, re-seed source+model (`container` → `bedrock` + Claude) |
| `:424` | `useState(DEFAULT_MODEL)` | `useState(defaultModelFor(DEFAULT_MODEL_SOURCE))` |
| `:562` | reset for new agent | also `setModelSource(DEFAULT_MODEL_SOURCE)`, `setCustomModel(false)` |
| `:700` | `setModelId(spec.model_id ?? DEFAULT_MODEL)` | derive source from `spec.model_source ?? "bedrock"`; `setCustomModel(sourceOfModelId(id) === null)` |
| `:46-47` | `interface StoredSpec` | add `model_source?: ModelSource` |
| `:609` | `model_id: modelId` in `buildSpec()` | add `model_source: modelSource` |
| `:1052-1060` | free-text model field | chips + select + conditional free-text |

`buildSpec()` is shared by all three methods, so `model_source` lands on
container specs too. That is fine and desirable — a container spec carries
`model_source: "bedrock"`, which is true and inert (the Claude SDK template only
substitutes `spec.model_id`).

### 3.2a Per-method seed table — decided during implementation

The design originally said "`container` → `bedrock`, other methods →
`DEFAULT_MODEL_SOURCE`". That over-reached: it would have made `zip_runtime`
default to a Mantle id while the zip template still hands the id to Strands as a
bare string (a Converse call), so the agent would deploy cleanly and fail on
first invoke — a break `make verify` cannot catch. Resolved with a module-scope
table so every commit stays shippable and child 2 flips one entry:

```ts
const MODEL_SOURCE_BY_METHOD: Record<Method, ModelSource> = {
  harness: DEFAULT_MODEL_SOURCE,     // mantle
  container: CLAUDE_SDK_MODEL_SOURCE, // bedrock — Claude-only SDK
  zip_runtime: "bedrock",            // → mantle in 07-31-zip-mantle-default
};
```

The selector is still *offered* for `zip_runtime`, satisfying R1; only its
default differs.

### 3.3 The `container` carve-out

Requirement 3 of the parent says the Claude Agent SDK keeps its Claude default.
Implementation: the Model source chips render only when `method !== "container"`;
selecting the `container` method forces `setModelSource("bedrock")` and re-seeds
the model to the Bedrock default. So the container path's observable behavior is
identical to today.

One consequence found during implementation: turning the free-text field into a
dropdown means the container path would now *advertise*
`global.amazon.nova-2-lite-v1:0`, which the Claude Agent SDK cannot drive — a
footgun the free-text field never presented. So the offered options are filtered
for that method (`modelOptionsFor(source, claudeOnly)` in
`frontend/src/lib/models.ts`), and the custom-branch test uses the *offered*
options rather than the whole catalog, so display and payload always agree.

## 4. Compatibility

| Scenario | Behavior |
|---|---|
| Agent created before this change, viewed | `model_source` absent → `bedrock` → Converse. Unchanged. |
| …re-published | `build_create_params` → `apiFormat: converse_stream`, which is the current implicit behavior. Same model, same semantics. |
| …loaded into the wizard | Bedrock chip selected; model id shown in the dropdown, or via `Custom model ID…` if its id is not one of the three Bedrock catalog entries (true for e.g. `claude-sonnet-4-6` agents). |
| Interrupted deploy job resumed after this change | `resume_pending_jobs()` re-runs from the first non-succeeded stage; params are rebuilt from the stored spec, so the source is whatever was stored. Idempotent. |
| Harness → zip conversion | `harness_convert.py:316` carries `model_source` alongside `model_id`; honoring it in the zip template is child 2. |

## 5. Risks

| Risk | Mitigation |
|---|---|
| `openai.gpt-5.6-*` / `claude-opus-5` / `nova-2-lite-v1:0` may not be enabled in the account | Acceptance is on the payload, not a live invocation (parent PRD constraint). The catalog is one table, trivially corrected. |
| Preview API rejects an explicit `apiFormat: converse_stream` | Rollback is one line: omit the key for `bedrock`. Surfaces as `CREATE_FAILED` + `failureReason`, already reported by `wait_harness_ready`. |
| Mantle model with `apiFormat: responses` may need different `maxTokens` handling | Out of scope; the repo does not set `maxTokens` on the harness today. |
| Turning a free-text field into a dropdown could strand existing custom ids | `Custom model ID…` + `sourceOfModelId() === null` detection, with an explicit acceptance criterion. |

## 6. Test plan

Backend (`backend/tests/test_harness_deployer.py`, which already asserts
`params["model"]["bedrockModelConfig"]["modelId"]` at `:27`):

1. Mantle spec → `{"modelId": "openai.gpt-5.6-sol", "apiFormat": "responses"}`.
2. Bedrock spec → `{"modelId": "global.anthropic.claude-sonnet-5", "apiFormat": "converse_stream"}`.
3. Spec built from a dict with **no** `model_source` → `model_source == "bedrock"`
   and the Converse payload (pre-existing-agent regression).
4. `wrap_params_for_update(build_create_params(mantle_spec))["model"]` is
   unchanged from create.

Frontend: `npx tsc --noEmit` + eslint; i18n parity via `scripts/i18n_check.py`.
No frontend unit-test harness exists for this page, so the UI criteria are
verified by reading the built page in `make dev` if a browser check is warranted.

## 7. Rejected alternatives

- **`openAiModelConfig` for Mantle.** Requires `apiKeyArn` pointing at an
  AgentCore Identity API-key credential provider. `gateway_bootstrap.py:123,136`
  only ever creates an *OAuth2* provider, so this would add bootstrap surface,
  a secret to manage, and a new failure mode — for a branch that is semantically
  about a third-party OpenAI account, not Bedrock-hosted models.
- **`liteLlmModelConfig` with `apiBase: https://bedrock-mantle.…/openai/v1`.**
  Matches the URL the canvas builds today, but still wants an API key and adds a
  LiteLLM hop for something `bedrockModelConfig` expresses natively.
- **Deriving `apiFormat` on the frontend and sending it.** Puts an AWS enum in
  the public request contract for no gain; the backend owns wire shapes
  everywhere else in this repo.
- **A cross-field validator pinning ids to sources.** The id space is open by
  design (custom ids) and unverifiable from this account; a validator would
  reject legitimate specs.
