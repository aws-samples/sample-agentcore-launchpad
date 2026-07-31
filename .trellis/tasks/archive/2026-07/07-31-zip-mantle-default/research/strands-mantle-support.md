# Research — Bedrock Mantle auth for zip-packaged Strands agents

Question: can a zip-packaged Strands agent on AgentCore Runtime call a Mantle
model (`openai.gpt-5.6-sol`) **without** a user-supplied API key?

**Answer: yes.** The installed Strands SDK has first-class IAM-only Mantle
support that this repo does not currently use. Verified 2026-07-31 against
`backend/.venv/lib/python3.12/site-packages/strands/` (**strands-agents 1.47.0**).

## The SDK feature

There is no separate Mantle model class. A shared private helper,
`strands/models/_openai_bedrock.py`, is wired into both `OpenAIModel` and
`OpenAIResponsesModel`.

`strands/models/openai_responses.py:150-153`:
```python
def __init__(
    self,
    client_args: dict[str, Any] | None = None,
    bedrock_mantle_config: BedrockMantleConfig | None = None,
    **model_config: Unpack[OpenAIResponsesConfig],
) -> None:
```

- `_resolve_client_args()` (`openai_responses.py:184-193`) delegates to
  `resolve_bedrock_client_args()` (`_openai_bedrock.py:96-144`).
- `_openai_bedrock.py:119,134` — `from aws_bedrock_token_generator import provide_token`
  then `provide_token(region=…, [aws_credentials_provider=…], [expiry=…])`. This
  uses the **standard AWS credential chain** (SigV4 presign, no network call), so
  the AgentCore Runtime execution role is enough.
- `_openai_bedrock.py:142-143` sets `base_url` and `api_key` (the minted token)
  itself.
- Re-resolved at each `openai.AsyncOpenAI(**self._resolve_client_args())` site —
  `openai_responses.py:260, 312, 492` — so the short-lived token is **re-minted
  per request**, surviving its max lifetime.
- Region resolution order (`_openai_bedrock.py:113`): config → `boto_session` →
  `boto3.Session().region_name` / `AWS_REGION`; raises `ValueError` at `:89` if
  none resolves. Hence the design's explicit region default.

`BedrockMantleConfig` (`_openai_bedrock.py:42-64`) — TypedDict, all optional:
`region`, `boto_session`, `credentials_provider`, `expiry`. **`api_key` is not an
accepted key**, and `openai_responses.py:174-179` raises `ValueError` if
`client_args` contains `api_key` or `base_url` alongside
`bedrock_mantle_config`. The two forms are mutually exclusive.

### Base URL path split — the repo gets this wrong today

`_openai_bedrock.py:23,28-39` — `https://bedrock-mantle.{region}.api.aws{path}`:

- `path = "/openai/v1"` **only** when `model_id` starts with `openai.gpt-5.`
- `path = "/v1"` otherwise

The repo's own `mantleBaseUrl()` (`frontend/src/studio/lib/models.ts:98-100`)
returns `/openai/v1` unconditionally — wrong for its *current default* Mantle
model, `xai.grok-4.3` (`models.ts:103,109`). Letting the SDK build the URL
removes the class of bug; the fix is still needed for the explicit-key branch.

## Packaging

`strands_agents-1.47.0.dist-info/METADATA:128`:
```
Requires-Dist: aws-bedrock-token-generator<2.0.0,>=1.1.0; extra == 'openai'
```

Neither `aws_bedrock_token_generator` nor `openai` is present in
`backend/.venv/lib/python3.12/site-packages` today (only `bedrock_agentcore`
matches `*bedrock*`). `strands` itself is only a **transitive** backend
dependency — `backend/pyproject.toml:6-17` lists
`bedrock-agentcore[simulation]==1.17.*` but no `strands-agents`.

Consequence: `_openai_bedrock.py:120-124` would `ImportError` in the backend's
own interpreter, which is what `local_exec.py` uses (`sys.executable`), and its
`:192-194` handler would misreport it as "strands not installed".

Deployed zips are fine **if** the requirements carry the extra — the canvas
publish path already adds `strands-agents[openai]` for Mantle/OpenAI nodes
(`frontend/src/pages/CreateAgentStudio.tsx:135-144`), which is the precedent for
emitting it as a separate line alongside the template's
`strands-agents[otel]>=1.0,<2` (`backend/app/templates/strands_agent/requirements.txt`).

## No `bedrock-mantle` botocore service; auth signing name is `bedrock`

- `botocore/data/` has `bedrock`, `bedrock-agent`, `bedrock-agent-runtime`,
  `bedrock-agentcore`, `bedrock-agentcore-control`, `bedrock-data-automation{,-runtime}`,
  `bedrock-runtime` — **no `bedrock-mantle`**. `endpoints.json` contains no
  `"mantle"` substring. Mantle is a plain HTTPS/OpenAI-protocol endpoint.
- botocore does support Bedrock bearer tokens: `botocore/utils.py:3626-3638`
  (`AWS_BEARER_TOKEN_{SIGNING_NAME}` → `AWS_BEARER_TOKEN_BEDROCK`),
  `botocore/auth.py:1130,1202,1223`. Both `bedrock` and `bedrock-runtime` declare
  `auth: ['aws.auth#sigv4', 'smithy.api#httpBearerAuth']`, `signingName: bedrock`.
- **No AWS API mints one.** Searching `bedrock/2023-04-20` and
  `bedrock-runtime/2023-09-30` for operations containing `ApiKey`/`Token` yields
  `[]` and `['CountTokens']`. The token is minted client-side — exactly what
  `provide_token` does. (The `bedrock-agentcore` Identity APIs —
  `GetResourceApiKey`, `GetResourceOauth2Token`, … — vault *third-party* keys,
  not Bedrock's own.)

## IAM is already sufficient

`infra/stacks/base_stack.py:180-196` — `AgentExecutionRole`
(`assumed_by=ServicePrincipal("bedrock-agentcore.amazonaws.com")`), first
statement:

```python
actions=["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
resources=["*"],
```

Account-wide, and `bedrock` is the signing name a Mantle bearer token authorizes.
There is no `bedrock-mantle:*` action namespace to grant. **No infra change.**

## The repo's current Mantle path, end to end (key-based)

1. Operator types the key — `frontend/src/studio/PropertyPanel.tsx:288` (comment),
   field at `:466-474` bound to `data.apiKey`.
2. Provider docs assume a key — `frontend/src/studio/lib/models.ts:91`.
3. Codegen hardcodes it — `studio/lib/code-generator.ts:1446` (agent scope) and
   `:1559` (tool scope), `studio/lib/graph-code-generator.ts:60`:
   `client_args={ "api_key": os.environ.get("BEDROCK_API_KEY"), "base_url": "<baseUrl>" }`.
   Same contract in the LLM-codegen guidance:
   `backend/app/codegen/guidance/flow_semantics.md:58`,
   `contract_spec.md:175`.
4. Env derivation — `CreateAgentStudio.tsx:150-172`; `env.BEDROCK_API_KEY` set at
   `:168` only when non-empty.
5. Requirements — `CreateAgentStudio.tsx:135-144` adds `strands-agents[openai]`.
6. Deploy — `backend/app/deployer/environment.py:9-17` (`dict(spec.env)` +
   `LAUNCHPAD_MEMORY_ID`) → `zip_runtime.py:377,385` `environmentVariables`.
   Asserted in `backend/tests/test_zip_runtime_deployer.py:380,403`.
7. Local debug — `backend/app/services/local_exec.py:67-68`.

**The key is optional at publish time.** `CreateAgentStudio.tsx:171` computes
`missingApiKey`, rendered as a non-blocking warning at `:667-672`;
`frontend/src/locales/en/common.json:1769` says publish is allowed but "the agent
will fail at runtime without it."

Two sharp edges in that state today:
- The generated `OpenAIResponsesModel(...)` is at **module scope**, so a missing
  key is an **import-time** crash of the zip's `main.py`, not a per-request error.
- `openai/_client.py:133` falls back to `OPENAI_API_KEY`. A flow with both an
  OpenAI node and a Mantle node silently sends the OpenAI key to
  `bedrock-mantle…api.aws` → a confusing 401 instead of a config error.

Both disappear on the IAM path.

## Zip vs canvas: where the model lives

| | `zip_runtime` (wizard card) | `studio` (canvas) |
|---|---|---|
| Model source of truth | `spec.model_id` → `__LAUNCHPAD_MODEL_ID__` (`templates/strands_agent/__init__.py:41` → `main.py.tmpl:69`) | baked as a literal in canvas-generated Python; `spec.model_id` is **inert** |
| Model wiring | `main.py.tmpl:337` `"model": MODEL_ID` — bare string ⇒ Bedrock Converse | `OpenAIResponsesModel(...)` / `BedrockModel(...)` |
| Deployer | `zip_runtime.py:439-440` registers **both** `zip_runtime` and `studio` on the same stages | same |
| Artifact handling | template render | `adapt_studio_code` keeps the module verbatim — `backend/app/templates/studio_agent/__init__.py:80-94`, docstring `:11-14` states model/prompt rewriting is deliberately not attempted |
| Misleading log | — | `zip_runtime.py:322` logs `spec.model_id` for studio agents, i.e. the schema default, not the model in the code |

`spec.requirements` extras and env come from the canvas
(`CreateAgentStudio.tsx:135-173`); `_method_requirements`
(`zip_runtime.py:301-313`) adds `STUDIO_EXTRA_REQUIREMENTS`.

## Canvas quirks the implementer must respect

- For non-Bedrock providers the generators select
  `modelIdentifier = modelName`, **not** `modelId` — which is why
  `PropertyPanel.applyProviderChange` sets both to the same id
  (`PropertyPanel.tsx:274-275`). Any new default must do the same.
- Agent-node drop defaults (`FlowEditor.tsx:230-242`) currently pair a Sonnet
  **5** id with a stale `modelName: 'Claude Sonnet 4.6'` label.
- `orchestrator-agent` and `swarm` nodes get **no** model on drop; they fall back
  to the generators' destructuring defaults
  (`code-generator.ts:270,305,359,1083,1184,1302,1335`,
  `graph-code-generator.ts:432`). Changing those fallbacks would retroactively
  change existing flows.
- `MANTLE_MODELS` today is `xai.grok-4.3`, `openai.gpt-5.5`, `openai.gpt-5.4`
  (`models.ts:103-107`); reaching `openai.gpt-5.6-sol` currently needs the
  `Custom model` escape hatch (`isCustomMantleModel`, `:130-137`).
- The vendored `apps/studio/` sub-app is a **third**, older lineage
  (`us.anthropic.claude-3-7-sonnet-20250219-v1:0`, no Mantle provider) — out of
  scope per `CLAUDE.md`.
