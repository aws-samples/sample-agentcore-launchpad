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
- `container` → `bedrock`, and the Model source control is hidden. The Claude
  Agent SDK can only drive Claude models.
- `zip_runtime` → the selector is offered, and the default follows whether the
  Strands template can emit a Mantle model object. Passing a model id to
  `Agent(model=...)` as a bare string resolves to a Converse call, so a Mantle id
  on that path deploys cleanly and fails on first invoke — a break no test in
  `make verify` can catch.

## Related

- [Harness → Runtime Conversion](./harness-conversion.md) — the conversion
  carries `model_source` alongside `model_id`.
