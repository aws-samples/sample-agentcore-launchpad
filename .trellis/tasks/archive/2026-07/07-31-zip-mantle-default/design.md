# Design — Strands Studio zip entrance defaults to Mantle GPT-5.6

## 1. The enabling SDK feature

`strands-agents` 1.47.0 ships `strands/models/_openai_bedrock.py`, wired into both
`OpenAIModel` and `OpenAIResponsesModel`:

```python
def __init__(self, client_args=None, bedrock_mantle_config: BedrockMantleConfig | None = None,
             **model_config): ...
```
(`strands/models/openai_responses.py:150-153`)

- `resolve_bedrock_client_args()` (`_openai_bedrock.py:96-144`) calls
  `aws_bedrock_token_generator.provide_token(region=…)` using the **standard AWS
  credential chain**, then sets `base_url` and `api_key` itself.
- It is re-resolved at each `openai.AsyncOpenAI(**self._resolve_client_args())`
  site (`openai_responses.py:260, 312, 492`), so the short-lived token is
  re-minted per request.
- Base URL is `https://bedrock-mantle.{region}.api.aws{path}` where `path` is
  `/openai/v1` **only** for `model_id` starting with `openai.gpt-5.`, else `/v1`
  (`_openai_bedrock.py:23,28-39`).
- `client_args` may not contain `api_key`/`base_url` when `bedrock_mantle_config`
  is given — it raises `ValueError` (`openai_responses.py:174-179`). The two
  forms are therefore **mutually exclusive**, which is exactly why this design
  keeps them as two branches rather than merging them.
- `BedrockMantleConfig` keys (all optional): `region`, `boto_session`,
  `credentials_provider`, `expiry` (`_openai_bedrock.py:42-64`).

Packaging: `aws-bedrock-token-generator<2.0.0,>=1.1.0` ships in the
`strands-agents[openai]` extra (`strands_agents-1.47.0.dist-info/METADATA:128`),
and is **not** currently installed in `backend/.venv`.

IAM: `infra/stacks/base_stack.py:180-196` already grants
`bedrock:InvokeModel` + `…WithResponseStream` on `*` to the AgentCore execution
role, and `bedrock` is the signing name `provide_token` signs for. There is no
`bedrock-mantle:*` action namespace (no such service in botocore, and no `mantle`
entry in `endpoints.json`). **No infra change is required.**

## 2. Two independent code paths, one requirement

The word "Strands Studio" covers two different methods in this repo. Both must
end up on Mantle, and they share nothing:

| | `zip_runtime` (the wizard card) | `studio` (the `/create/studio` canvas) |
|---|---|---|
| Where the model lives | `spec.model_id` → `__LAUNCHPAD_MODEL_ID__` in `templates/strands_agent/main.py.tmpl:69` | baked as a literal into canvas-generated Python; `spec.model_id` is inert |
| Model wiring today | `Agent(model=MODEL_ID)` — bare string ⇒ Bedrock Converse (`main.py.tmpl:337`) | `OpenAIResponsesModel(client_args={api_key: env BEDROCK_API_KEY, base_url: …})` (`studio/lib/code-generator.ts:1441-1461`) |
| Deployer | both register the same stages — `zip_runtime.py:439-440` | same |
| This task's change | teach the template a Mantle branch (§3) | switch to `bedrock_mantle_config`, refresh the catalog, change the drop default (§4) |

## 3. Zip template (`backend/app/templates/strands_agent/`)

### 3.1 Rendered shape

Add one placeholder and one function. The template is placeholder-substituted,
not `str.format`ed, and rendered output must always compile — so the branch is
resolved **at render time** for the import and **at run time** for the object, to
keep the module import-safe when the extra is somehow missing.

```python
AGENT_NAME = "__LAUNCHPAD_AGENT_NAME__"
MODEL_ID = "__LAUNCHPAD_MODEL_ID__"
MODEL_SOURCE = "__LAUNCHPAD_MODEL_SOURCE__"   # "bedrock" | "mantle"
MANTLE_REGION = os.environ.get("LAUNCHPAD_MANTLE_REGION", "us-east-1")

def build_model():
    """Bedrock Converse takes a bare model id; Mantle needs an explicit model
    object. bedrock_mantle_config mints a short-lived bearer token from the
    execution role on every request — there is no API key to provision."""
    if MODEL_SOURCE == "mantle":
        from strands.models.openai_responses import OpenAIResponsesModel
        return OpenAIResponsesModel(
            bedrock_mantle_config={"region": MANTLE_REGION},
            model_id=MODEL_ID,
        )
    return MODEL_ID
```

and at `main.py.tmpl:337`, `"model": MODEL_ID` becomes `"model": build_model()`.

The import is **function-local** so a `bedrock`-source zip never imports
`openai`, keeping that package out of the hot path for the majority of agents and
keeping the module importable if the extra is missing.

### 3.2 Why `LAUNCHPAD_MANTLE_REGION`, not `AWS_REGION`

The runtime deploys to `us-west-2` (`config/launchpad.yaml`), but this repo's
existing Mantle support states the GPT/Grok models are hosted in `us-east-1`
(`frontend/src/studio/lib/models.ts:95-96` `DEFAULT_MANTLE_REGION`). Using
`AWS_REGION` would silently point at a region where the model may not exist. An
env var with a `us-east-1` default keeps the region overridable per deployment
without a schema change, and cross-region is fine for IAM (the grant is on `*`).

If `provide_token` cannot resolve a region it raises `ValueError`
(`_openai_bedrock.py:89`) — the default prevents that.

### 3.3 Requirements

`zip_runtime.py:307-313` (`_method_requirements`) composes the packaged
requirements. Add the `openai` Strands extra when `spec.model_source == "mantle"`.
Emit it as a separate `strands-agents[openai]>=1.0,<2` line alongside the base
`strands-agents[otel]>=1.0,<2` — that is the shape the canvas publish path
already produces today (`CreateAgentStudio.tsx:135-144`) and pip resolves the
union of extras.

`method == "studio"` specs come with their own `spec.requirements`, so the
canvas keeps supplying its own extra; the new rule applies to `zip_runtime`.

## 4. Canvas (`frontend/src/studio/`)

### 4.1 Catalog (`studio/lib/models.ts`)

```ts
export const MANTLE_MODELS: BedrockModelOption[] = [
  { model_id: 'openai.gpt-5.6-sol',   model_name: 'GPT-5.6 Sol' },
  { model_id: 'openai.gpt-5.6-terra', model_name: 'GPT-5.6 Terra' },
  { model_id: 'openai.gpt-5.6-luna',  model_name: 'GPT-5.6 Luna' },
  { model_id: 'openai.gpt-5.5',       model_name: 'GPT-5.5 (OpenAI)' },
  { model_id: 'openai.gpt-5.4',       model_name: 'GPT-5.4 (OpenAI)' },
  { model_id: 'xai.grok-4.3',         model_name: 'Grok 4.3 (xAI)' },
];
export const DEFAULT_MANTLE_MODEL_ID = 'openai.gpt-5.6-sol';  // [0], was xai.grok-4.3
```

Existing ids are kept — removing them would strand published flows through
`isCustomMantleModel`. The list is reordered so `[0]` is the new default, which
is how `DEFAULT_MANTLE_MODEL_ID` is derived today (`:109`).

To avoid a fourth copy of the model tables, `studio/lib/models.ts` re-exports the
Mantle ids from the shared `frontend/src/lib/models.ts` catalog that child 1
creates; the studio module keeps its own `BedrockModelOption` display shape.

### 4.2 `mantleBaseUrl` path fix

```ts
export function mantleBaseUrl(region: string, modelId?: string): string {
  const path = modelId?.startsWith('openai.gpt-5.') ? '/openai/v1' : '/v1';
  return `https://bedrock-mantle.${region || DEFAULT_MANTLE_REGION}.api.aws${path}`;
}
```

Mirrors `_openai_bedrock.py:31-39`. It is currently unconditionally
`/openai/v1`, which is **wrong for the present default model** (`xai.grok-4.3`).
After this change the function is only used on the explicit-key branch, but it
must still be right.

### 4.3 Codegen branch

`code-generator.ts:1441-1461` (agent scope), `:1556-1574` (tool scope), and
`graph-code-generator.ts:57-75` all emit the same Mantle block. Each becomes:

```ts
if (modelProvider === MANTLE_PROVIDER) {
  const cfg = data.apiKey
    ? `client_args={"api_key": os.environ.get("BEDROCK_API_KEY"), "base_url": "${mantleBaseUrl(region, modelIdentifier)}"}`
    : `bedrock_mantle_config={"region": "${region || DEFAULT_MANTLE_REGION}"}`;
  ...
}
```

**Key present ⇒ keyed form; key absent ⇒ IAM form.** This is what makes the
change non-breaking: every flow published before today has a key and keeps its
exact generated code, while the new default needs no key. It also matches the
SDK's own mutual exclusion (§1).

`region` is already persisted on the node by `PropertyPanel.applyProviderChange`
(`:267-276`); `baseUrl` stays persisted for the keyed branch.

### 4.4 Node default (`studio/FlowEditor.tsx:230-242`)

```ts
modelProvider: MANTLE_PROVIDER,
modelId: DEFAULT_MANTLE_MODEL_ID,
modelName: DEFAULT_MANTLE_MODEL_ID,   // Mantle ids flow through the modelName codegen path
region: DEFAULT_MANTLE_REGION,
```

Note the existing quirk this must respect: for non-Bedrock providers the
generators select `modelIdentifier = modelName` (not `modelId`), which is why
`applyProviderChange` sets both to the same id (`PropertyPanel.tsx:274-275`). The
drop default must do the same. This also removes a stale label bug — the current
default sets `modelName: 'Claude Sonnet 4.6'` next to a Sonnet **5** id.

`orchestrator-agent` / `swarm` nodes get no model on drop and fall back inside
the generators; their destructuring defaults (`code-generator.ts:270,305,359,
1083,1184,1302,1335`, `graph-code-generator.ts:432`) stay on
`DEFAULT_MODEL_ID` (Bedrock Claude). Changing those fallbacks would also change
the model of every *existing* flow whose nodes lack an explicit id — so they are
deliberately left alone. The default a user actually sees comes from the drop.

### 4.5 Publish path

`CreateAgentStudio.tsx`:
- `:135-144` — the `strands-agents[openai]` extra must now be added for Mantle
  nodes **regardless of whether a key is set** (it is what carries the token
  generator). Today it is added for any Mantle/OpenAI node, so this already
  holds; verify it does not become key-conditional.
- `:150-173` — `env.BEDROCK_API_KEY` is only set from a non-empty node
  `apiKey`, so the IAM path already sets nothing. No change.
- `:171` `missingApiKey` — must stop counting Mantle nodes (they no longer need
  a key). OpenAI-provider nodes still do.

## 5. Local debug

`local_exec.py` runs generated code with `sys.executable` — the backend's own
interpreter. `openai` and `aws-bedrock-token-generator` are both absent from
`backend/.venv`, so locally debugging the **new default** flow would fail on
import (with the friendly "strands not installed" path at `local_exec.py:192-194`
misreporting the cause).

Fix: add `strands-agents[openai]>=1.0,<2` to `backend/pyproject.toml`
dependencies. `strands` is currently only a transitive dependency (via
`bedrock-agentcore[simulation]`, resolved to 1.47.0), so this also makes the
version explicit. Verify with `uv sync` that it does not conflict.

Credentials for local debug come from the developer's ambient AWS profile, which
`provide_token` picks up via the standard chain — no extra env plumbing needed.

## 6. Compatibility

| Scenario | Behavior |
|---|---|
| Existing `zip_runtime` agent (no `model_source` in stored spec) re-published | child 1's schema default → `bedrock` → `Agent(model=MODEL_ID)` with a bare string, byte-identical wiring to today. |
| Existing canvas flow with a `BEDROCK_API_KEY` | node has `apiKey` ⇒ keyed branch ⇒ identical generated code. |
| Existing canvas flow using Mantle **without** a key (published despite the warning, currently broken at import) | now generates the IAM form and actually works. Strict improvement. |
| Existing canvas flow using `xai.grok-4.3` with a key | now gets the correct `/v1` path instead of the wrong `/openai/v1`. Behavior change, but a bug fix. |
| Harness → zip conversion of a Mantle harness | `harness_convert.py` carries `model_source` (child 1), template honors it (this task). |

## 7. Risks

| Risk | Mitigation / note |
|---|---|
| `openai.gpt-5.6-*` may not be enabled, or not in `us-east-1` | Acceptance is on generated code, not a live call (parent PRD). Region is overridable via `LAUNCHPAD_MANTLE_REGION`. |
| `strands-agents[openai]` conflicts with the `bedrock-agentcore[simulation]`-pinned strands | Verify with `uv sync`; if it conflicts, drop the backend dependency step and accept that local debug of Mantle flows needs a manual install — record that in `docs/troubleshooting.md`. This is the one step that can be cut without losing the feature. |
| Three near-identical Mantle emitters (`code-generator.ts` ×2, `graph-code-generator.ts`) drift | Extract the branch into one exported helper in `studio/lib/` and call it from all three; the duplication is pre-existing but this task makes it worse if left. |
| Function-local import inside `build_model()` looks unusual | Comment it: keeps `openai` out of Bedrock-source zips and keeps the module import-safe. |

## 8. Test plan

Backend:
- `backend/tests/test_strands_template.py` — Mantle render (contains
  `OpenAIResponsesModel`, `bedrock_mantle_config`, no `BEDROCK_API_KEY`;
  `compile()`s), Bedrock render regression (bare-string wiring unchanged).
- `backend/tests/test_zip_runtime_deployer.py` — packaged `requirements.txt`
  contains the `openai` extra for a Mantle spec, and does not for a Bedrock spec.
  (This file already asserts env passthrough at `:380,403`.)
- `backend/tests/test_studio_artifact.py` — unchanged expectations; the canvas
  artifact is still adapted verbatim.

Frontend: `npm run lint`, `npx tsc --noEmit`, `npm run build`; i18n parity.
Canvas codegen has no unit-test harness, so verify the three emitted forms by
generating code in `make dev` for: fresh Mantle node (IAM), Mantle node with a
key (keyed), Bedrock node (unchanged).

## 9. Rejected alternatives

- **Keep requiring `BEDROCK_API_KEY`.** Would make the *default* creation path
  fail unless the operator pastes a secret, and the key currently lands in
  plaintext inside the stored `studio_flow` (flagged at
  `docs/studio-integration.md:99`).
- **Build the Mantle URL ourselves and pass `client_args`.** The SDK forbids
  combining that with `bedrock_mantle_config`, and our own URL builder has the
  `/openai/v1` vs `/v1` bug that the SDK gets right.
- **Reuse `AWS_REGION` for Mantle.** Points at `us-west-2`, where these models
  are not documented to exist.
- **Switch the codegen destructuring fallbacks to Mantle.** Would retroactively
  change the model of existing flows whose nodes carry no explicit id.
- **Migrate the sample flows.** Several depend on Claude-only features
  (cache points, effort tiers); see the parent PRD non-goals.
