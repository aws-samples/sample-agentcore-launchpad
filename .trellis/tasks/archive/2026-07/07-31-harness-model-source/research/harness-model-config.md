# Research — AgentCore Harness model configuration shape

Verified 2026-07-31 against the vendored service model, not from memory:
`backend/.venv/lib/python3.12/site-packages/botocore/data/bedrock-agentcore-control/2023-06-05/service-2.json.gz`
(botocore **1.43.44**). Re-verify with:

```bash
cd backend && uv run python -c "
import gzip,json,glob
p=glob.glob('.venv/lib/python3.12/site-packages/botocore/data/bedrock-agentcore-control/*/service-2.json.gz')[0]
d=json.loads(gzip.open(p).read())
print(json.dumps(d['shapes']['HarnessBedrockModelConfig'],indent=1))
print(json.dumps(d['shapes']['HarnessBedrockApiFormat'],indent=1))"
```

## `HarnessModelConfiguration` is a tagged union

Used verbatim by **both** `CreateHarness.model` and `UpdateHarness.model`.

| union key | member shape | required members |
|---|---|---|
| `bedrockModelConfig` | `HarnessBedrockModelConfig` | `modelId` |
| `openAiModelConfig` | `HarnessOpenAiModelConfig` | `modelId`, **`apiKeyArn`** |
| `geminiModelConfig` | `HarnessGeminiModelConfig` | `modelId`, **`apiKeyArn`** |
| `liteLlmModelConfig` | `HarnessLiteLlmModelConfig` | `modelId`, **`apiBase`** |

## `HarnessBedrockModelConfig` — the branch this task uses

```
required: [modelId]
members:
  modelId          ModelId          (plain string, no pattern, no length cap)
  maxTokens        MaxTokens
  temperature      Temperature
  topP             TopP
  apiFormat        HarnessBedrockApiFormat
  additionalParams Document
```

```
HarnessBedrockApiFormat = enum[ "converse_stream", "responses", "chat_completions" ]
```

**`apiFormat` is optional and there is no `apiKeyArn` on this branch.** That is
the whole basis of the design: Mantle-hosted models (Responses / Chat
Completions API) and native Bedrock models (Converse API) are the *same* union
branch, distinguished by this one enum, authenticated by the harness execution
role. It also matches the AWS console's own "Model source" wording that the
requester quoted.

`HarnessOpenAiApiFormat = enum["chat_completions", "responses"]` — note the
OpenAI branch has no `converse_stream`, consistent with it meaning a real OpenAI
account rather than a Bedrock-hosted model.

## Why the keyed branches are unusable here

`ApiKeyArn` pattern requires an **AgentCore Identity API-key credential provider**
ARN:

```
arn:aws:bedrock-agentcore:<region>:<acct>:token-vault/<name>/apikeycredentialprovider/<name>
```

This repo never creates one. `backend/app/services/gateway_bootstrap.py:123,136`
only calls `list_oauth2_credential_providers` /
`create_oauth2_credential_provider`, surfaced as
`settings.resources["oauth_provider_arn"]`. Nothing calls
`create_api_key_credential_provider`. So `openAiModelConfig`,
`geminiModelConfig`, and a keyed `liteLlmModelConfig` would each require new
bootstrap surface plus a secret to manage.

## Current repo code map (pre-change)

| What | Where |
|---|---|
| The **only** place the model reaches the wire | `backend/app/deployer/harness.py:81` — `"model": {"bedrockModelConfig": {"modelId": spec.model_id}}` (no `apiFormat`) |
| Params builder | `build_create_params` `harness.py:58-179`; rebuilt by `_stage_provision` `:266` (when KBs mount) and `_stage_deploy` `:288-294` (on resume) |
| Generate-stage log line | `harness.py:229` |
| Create/update wrappers | `backend/app/services/agentcore/harness.py:14-16, 19-23` |
| Create→update adapter | `wrap_params_for_update` `agentcore/harness.py:26-38` — drops `harnessName`, wraps `memory`, defaults `tools`/`skills`; **passes `model` through unchanged** |
| Failure surfacing | `wait_harness_ready` `agentcore/harness.py:49-68`; `TERMINAL_FAILURES = {CREATE_FAILED, UPDATE_FAILED, DELETE_FAILED}` at `:11` — a bad `modelId`/`apiFormat` pairing lands here with a `failureReason`, **not** as a boto validation error |
| Backend model default | `backend/app/schemas/agent.py:11` `DEFAULT_MODEL_ID = "global.anthropic.claude-sonnet-5"`; field at `:134`; **no** validator touches `model_id` |
| Existing model assertion | `backend/tests/test_harness_deployer.py:27` |

## Three independent copies of the same default on the frontend

There is no shared frontend model catalog today:

1. `frontend/src/pages/CreateAgent.tsx:27` — `DEFAULT_MODEL` (the wizard; a
   free-text input at `:1052-1060`, **not** a dropdown)
2. `frontend/src/studio/lib/models.ts:14` — `DEFAULT_MODEL_ID` (canvas)
3. `frontend/src/pages/EvaluationEvaluators.tsx` — its own `MODEL_OPTIONS`

No backend model-catalog endpoint exists. `/prices`
(`backend/app/routers/observability.py:33`) and
`backend/app/services/model_prices.py` are a **cost-estimation** price map, not a
selectable catalog.

## Unused-but-available CreateHarness inputs

For future reference, not this task: `maxTokens`, `truncation`, `allowedTools`,
`authorizerConfiguration`, `environment`, `environmentArtifact`, `tags`.
