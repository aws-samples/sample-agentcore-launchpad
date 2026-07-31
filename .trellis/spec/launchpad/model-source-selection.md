# Model Source Selection

> One field, `AgentSpec.model_source`, decides which model-hosting surface an
> agent talks to: Bedrock Mantle (Responses / Chat Completions API) or native
> Bedrock (Converse API).

## Scope

Use this contract when changing the model dropdowns on the creation surfaces,
the harness model payload, the zip/Studio model wiring, or the shared frontend
model catalog.

## The Field

```python
ModelSource = Literal["bedrock", "mantle"]
model_source: ModelSource = "bedrock"
```

The schema default is **`bedrock`**, and that is load-bearing: the ledger stores
agent specs as JSON, and every row written before this field existed is a
Converse-API agent. A `mantle` schema default would silently move all of them to
the Responses API on their next re-publish. Mantle is a **form** default, chosen
per method in the console, never a schema default.

`model_source` and `model_id` are deliberately independent — there is no
cross-field validator. The id space is open (custom ids are a first-class
feature) and the target account cannot enumerate valid ids, so any validator
would reject legitimate specs. A wrong pairing surfaces as an AWS
`CREATE_FAILED` with a `failureReason`, which `wait_harness_ready` already
reports.

## Harness Payload — One Union Branch, Two `apiFormat` Values

`HarnessModelConfiguration` is a four-branch union, but Launchpad only ever
emits `bedrockModelConfig`:

```python
"model": {"bedrockModelConfig": {"modelId": spec.model_id,
                                 "apiFormat": _API_FORMAT[spec.model_source]}}
```

`HarnessBedrockApiFormat` is `converse_stream | responses | chat_completions`,
optional, and needs **no API key** — the harness execution role authenticates
both surfaces. `mantle` → `responses`; `bedrock` → `converse_stream`, emitted
explicitly because that is what the service already does implicitly, which keeps
the payload self-describing and assertable.

Never reach for `openAiModelConfig`, `geminiModelConfig`, or a keyed
`liteLlmModelConfig`. Each requires an `apiKeyArn` naming an **AgentCore
Identity API-key credential provider**, and bootstrap only ever creates an
*OAuth2* provider. Those branches mean "a third-party account", not
"a Bedrock-hosted model".

`build_create_params` is the single site that puts a model on the wire.
`wrap_params_for_update` passes `model` through unchanged, and
`UpdateHarnessRequest.model` is the same union shape, so re-publish preserves the
chosen source with no extra code.

## Frontend Catalog

`frontend/src/lib/models.ts` is the platform-side catalog for the creation
wizard. It carries `api_format` per entry rather than deriving it from the
source, so adding a `chat_completions` model later is a data change and nothing
else. The frontend sends `model_source` only — never `api_format`; the backend
owns wire shapes.

The wizard's model field is a dropdown plus a `Custom model ID…` escape hatch.
Two rules keep it honest:

- The custom-branch test is against the **offered** options, not the whole
  catalog (`isCustomModelId`). An id belonging to the other source, or one
  filtered out for a method, is custom here — otherwise the `<select>` would
  display one id and submit another.
- `modelOptionsFor(source, claudeOnly)` narrows the list. The Claude Agent SDK
  (`container`) is offered Claude ids only, since it cannot drive anything else.

## Per-Method Defaults

`MODEL_SOURCE_BY_METHOD` in `frontend/src/pages/CreateAgent.tsx` decides which
source each method starts on. The invariant to preserve: **a method only defaults
to `mantle` once its execution path can actually execute a Mantle model.**

- `harness` → `mantle`. The payload change alone is sufficient.
- `container` → `bedrock`, and the Model source control is hidden — the console
  renders the `AgentSpec.agent_sdk` selector in its place, because this entrance
  ("Other Agent SDK") chooses an SDK rather than a hosting surface. Its one SDK
  today, the Claude Agent SDK, can only drive Claude models, so the source is
  pinned and `modelOptionsFor(source, /* claudeOnly */ true)` narrows the ids.
  A second SDK member that can drive other models would have to re-open the
  control rather than widen this pin.
- `zip_runtime` → `mantle`, since the Strands template now emits a Mantle model
  object (see below). Passing a model id to `Agent(model=...)` as a bare string
  resolves to a Converse call, so a Mantle id on that path would deploy cleanly
  and fail on first invoke — a break no test in `make verify` can catch. That is
  why the default and the template branch had to ship together.
- `protocol=a2a` (a zip sub-mode) → pinned to `bedrock` with the control hidden
  (`A2A_MODEL_SOURCE`). `templates/strands_a2a_agent/` has no Mantle branch and
  ignores `model_source`; its wheel set is vendored into a ~46 MB zip, so adding
  the `openai` extra there is deferred until asked for.

## Zip / Studio — the Source Changes the Generated Code

On the zip path the model is an *argument to generated Python*, not a request
field, so `model_source` selects between two renderings
(`templates/strands_agent/main.py.tmpl::build_model`):

```python
# bedrock: a bare id ⇒ Bedrock Converse (unchanged from before model_source)
return MODEL_ID
# mantle:
from strands.models.openai_responses import OpenAIResponsesModel   # function-local!
return OpenAIResponsesModel(bedrock_mantle_config={"region": MANTLE_REGION},
                            model_id=MODEL_ID)
```

Four facts make this work, verified against strands-agents 1.47.0 and a live invoke:

- **`bedrock_mantle_config` is IAM-only.** `resolve_bedrock_client_args`
  (`strands/models/_openai_bedrock.py`) calls
  `aws_bedrock_token_generator.provide_token(region=…)` on the ambient credential
  chain — the agent's execution role. It is re-resolved per `AsyncOpenAI`
  construction, so the short-lived token outlives nothing. **Never require a
  `BEDROCK_API_KEY` here.**
- **Mantle needs its own IAM grants; `bedrock:InvokeModel` does NOT cover it.**
  `bedrock-mantle` is a separate IAM service, which is easy to miss because it has
  no boto3 client and no entry in botocore's `endpoints.json` — absence of a
  service model is not absence of an action namespace. Getting this wrong costs a
  full deploy to discover: the agent reaches ACTIVE and fails its *first invoke*
  with `401 access_denied … not authorized to perform:
  bedrock-mantle:CreateInference on arn:aws:bedrock-mantle:<region>:<acct>:project/default`.
  `infra/stacks/base_stack.py` therefore grants, mirroring the AWS managed policy
  `AmazonBedrockMantleInferenceAccess`:
  `bedrock-mantle:Get*`/`List*`/`CreateInference` on
  `arn:aws:bedrock-mantle:*:<acct>:project/*` (region wildcarded — Mantle models
  live outside the stack's region), `bedrock-mantle:CallWithBearerToken` on `*`
  (**not optional** — that is how the minted token is accepted), and
  `aws-marketplace:Subscribe`/`ViewSubscriptions` gated on
  `aws:CalledViaLast = bedrock-mantle.amazonaws.com` for the third-party
  (`openai.*` / `xai.*`) families. Pinned by
  `test_execution_role_can_run_bedrock_mantle_inference`. The **same role serves
  harness and zip** (`resources["execution_role_arn"]`), so this is one grant for
  both paths — and changing it requires a CDK deploy, not just a re-publish.
- **`bedrock_mantle_config` and `client_args={api_key,base_url}` are mutually
  exclusive** — `OpenAIResponsesModel.__init__` raises `ValueError`. Keep them as
  two branches; never merge them.
- **The import must stay function-local.** `strands/models/openai_responses.py`
  reads the `openai` package version at *module* import, and `openai` ships only
  in the `strands-agents[openai]` extra — which `_method_requirements`
  (`deployer/zip_runtime.py`) adds only for a Mantle spec. A module-scope import
  would break every Bedrock-source agent.
- **That extra carries a version floor, unlike the base pin.**
  `strands-agents[openai]>=1.47,<2`, not the base `>=1.0`: the
  `openai.gpt-5.*` → `/openai/v1` base-path split the default Mantle model needs
  landed in **1.46**, and `bedrock_mantle_config` is a keyword argument, so an
  older resolution would package cleanly and fail at first invoke. Keep this
  floor and `scripts/setup_exec_env.sh`'s in step. `openai>=2,<3` is pinned
  beside it for the same reason: the extra itself only asks for `>=1.68`, while
  `openai_responses` imports 2.x APIs at module scope, so the working
  resolution must not be left to whenever the zip happens to be built.

`MANTLE_REGION` = `LAUNCHPAD_MANTLE_REGION` or **`us-east-1`** — deliberately not
`AWS_REGION`, which is the `us-west-2` the runtime deploys into, where these
models are not offered. `provide_token` raises if no region resolves at all.

## Canvas (`/create/studio`)

The canvas bakes the model into generated Python as a literal, so `spec.model_id`
and `spec.model_source` are **inert** for `method="studio"`; the node's
`modelProvider` decides. `mantleModelArgs` in `studio/lib/models.ts` is the one
emitter for both forms and is called by all three generators (agent scope,
agent-as-tool scope, graph mode) — do not re-inline it:

- node `apiKey` empty ⇒ `bedrock_mantle_config` (the default);
- node `apiKey` set ⇒ the legacy `client_args` form, so flows published with a
  key generate byte-identical code.

Two related rules: only `openai.gpt-5.*` ids are served from `/openai/v1`, every
other Mantle id from `/v1` (`mantleBaseUrl`, mirroring `_openai_bedrock.py`); and
for non-Bedrock providers the generators read `modelIdentifier = modelName`, not
`modelId`, so any Mantle default must set **both** to the same id.

## Related

- [Harness → Runtime Conversion](./harness-conversion.md) — the conversion
  carries `model_source` alongside `model_id`, so a Mantle harness converts into
  a working Mantle zip.
