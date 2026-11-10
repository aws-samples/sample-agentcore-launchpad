# Interface: model providers (N-R6)

> **Superseded in part by D21 (2026-10-14) — see [model-registry.md](model-registry.md).**
> The provider dimension described here is now the admin-managed **model registry**: §1 (the
> two module tables) and §4 (adding a provider or a model by code / Settings override) no longer
> describe how the catalog is defined — the `PROVIDERS` / `MODEL_CATALOG` module dicts and the
> `LAUNCHPAD_SILICONFLOW_MODELS` override were removed (M1-e, C5); readers call the function
> views of `schemas/agent.py` over the registry snapshot. §2 (key flow) and §3
> (refusal codes) remain valid; the "Registry (D21)" section at the end maps old to new.

Frozen 2026-10-12. Extends [model-credential.md](model-credential.md) (spec C3/C4): the model
catalog has a **provider dimension**. Two kinds exist today — `bedrock` (the Global Bedrock
cross-region profiles) and `openai_compatible` (standard chat-completions endpoints with a
Bearer key; **SiliconFlow / 硅基流动** is the first instance). Nothing outside the catalog
tables spells a provider: every surface derives from `PROVIDERS` and `MODEL_CATALOG` in
`backend/app/schemas/agent.py`.

## 1. The two tables

| table | what a row carries |
|---|---|
| `PROVIDERS[name]` | `kind` (`bedrock` \| `openai_compatible`), `label`, `base_url` (None for Bedrock), `credential_provider` (AgentCore Identity API-key provider holding the key), `ssm_parameter` (SecureString the EC2 host reads), `api_key_env` (the env var the key enters the hub through), `required`, `id_prefix` (Launchpad-id prefix) |
| `MODEL_CATALOG[model_id]` | `provider`, `kind`, `label`, `api_model_name` (what the endpoint receives), `base_url`, `credential_provider`, `vision`, `temperature`, `tools` (function calling), `streaming`, `default_max_tokens`, `default_judge_max_tokens`, `max_output_tokens` |

Derived constants keep every pre-N-R6 consumer working unchanged: `ALLOWED_MODEL_IDS`
(= catalog order, Bedrock rows first), `MODEL_CAPABILITIES`, `MODEL_DEFAULT_MAX_TOKENS`,
`model_catalog_entries()` / `model_catalog_lines()` (architect prose, now with provider and
tool-calling traits), plus the new helpers `model_entry`, `model_provider`,
`is_openai_compatible_model`, `api_model_name`, `supports_tools`, `model_provider_table`,
`openai_compatible_providers`.

| provider | kind | endpoint | credential provider | SSM parameter | env var |
|---|---|---|---|---|---|
| `bedrock` | `bedrock` | Global Bedrock, `Settings.model_region` (us-west-2), bearer | `launchpad-global-bedrock` | `launchpad-global-bedrock-api-key` (required) | `LAUNCHPAD_GLOBAL_BEDROCK_API_KEY` |
| `siliconflow` | `openai_compatible` | `https://api.siliconflow.cn/v1` (chat completions, `Authorization: Bearer`) | `launchpad-siliconflow` | `launchpad-siliconflow-api-key` (**optional**) | `LAUNCHPAD_SILICONFLOW_API_KEY` |

### Launchpad model ids
Bedrock rows keep their profile id (`global.zai.glm-5.3`). An OpenAI-compatible row is
`<id_prefix><api_model_name>` — `siliconflow.zai-org/GLM-5.3` — unique and stable across
providers; only the catalog knows that the endpoint receives `zai-org/GLM-5.3`.

### SiliconFlow rows are a Settings override — **removed (D21 M1-e, C5)**
*Historical (N-R6).* `Settings.siliconflow_models` / env `LAUNCHPAD_SILICONFLOW_MODELS` (yaml key
`siliconflow_models`) was: comma-separated `api_model_name[:trait+trait…]`, traits `tools`
(default) · `notools` · `vision` · `nostream` · `max=<n>` (per-call output ceiling, default
8192) · `judge=<n>` (judge budget, default 4096). Empty ⇒ Bedrock only. The catalog reads the
value at import through `app.core.config.raw_setting` (same precedence as `Settings`, without
constructing it — `Settings`' own validators import the catalog). A typo in a trait is a
start-up error naming the entry.

Defaults (`schemas/model_catalog.DEFAULT_SILICONFLOW_MODELS` — since M1-e the only source of the
seed rows; the `Settings` field and its `_CN_SILICONFLOW_MODELS` mirror are gone): the
newest generation per vendor as of 2026-10-12 per siliconflow.cn/pricing — `zai-org/GLM-5.3`,
`deepseek-ai/DeepSeek-V4-Pro`, `Qwen/Qwen3.8-27B`, `Pro/moonshotai/Kimi-K2.6` (Launchpad ids
`siliconflow.<api_model_name>`). All four carry the `tools` trait; **tool-calling is to be
confirmed live on the marketplace Tools tag (cloud.siliconflow.cn/models?tags=Tools)** —
edit the row under Settings → Model Providers if an id is retired or lacks tools (the env override
no longer exists). The OpenAI-compatible defaults `max` 8192 / `judge` 4096 apply to all four (reasoning models).

## 2. Key flow (spec C4, unchanged shape, now per provider)

```
SSM SecureString <ssm_parameter>  ──(EC2 UserData / bootstrap-host.sh, instance role)──►  /etc/launchpad/env  <api_key_env>=…
        │                                                                                       │ (0600, root; systemd EnvironmentFile)
        │                                                                                       ▼
        │                                           make bootstrap: model_credential.ensure_provider_credentials(control)
        │                                             for every PROVIDERS row: key present → Create/UpdateApiKeyCredentialProvider(<credential_provider>)
        │                                                                      key absent  → required (bedrock): MissingModelCredentialError
        │                                                                                    optional: log "model-credential <name> not configured — <env> is unset
        │                                                                                                 (store … <ssm_parameter> …); <label> models stay unavailable"
        ▼
AgentCore Identity API-key credential provider  ◄──GetResourceApiKey(<credential_provider>)──  runtimes / eval worker / Skill Lab worker
                                                                                                  (Runtime-injected workload token; key stays in process memory)
```

* **Hub** (SCORE NOW, online, insights, recommend, optimize, architect): `aws_clients.
  build_chat_model(model_id, …)` dispatches on the catalog — Bedrock → `BedrockModel`
  (bearer, unchanged); OpenAI-compatible → `strands.models.openai.OpenAIModel(client_args=
  {"base_url", "api_key", "timeout", "max_retries"}, model_id=<api_model_name>, params=
  {max_tokens, temperature…}, stream=…)` with the key from `aws_clients.require_provider_
  api_key(provider)` (= `api_key_env`, hub process env only). `global_bedrock_model` stays as
  a thin alias. The `[openai]` extra of `strands-agents==1.47.0` is pinned in
  `backend/pyproject.toml`.
* **Generated runtimes** (zip runtime, A2A, Harness `model/load.py`, eval worker — one shared
  block in `templates/strands_agent/main.py.tmpl`): the constants block gains
  `MODEL_PROVIDERS` (rendered by `templates/strands_agent.rendered_model_providers()` from
  `model_provider_table()`, like `MODEL_INFERENCE_PARAMS`); `_launchpad_chat_model(model_id,
  **config)` dispatches — Bedrock → `_launchpad_bedrock_model(api_key=_resolve_bedrock_api_
  key(), region_name=MODEL_REGION, …)`; OpenAI-compatible → `_launchpad_openai_model(api_key=
  _resolve_api_key(<credential_provider>), base_url, api_model_name, …)`. The key of an
  OpenAI-compatible provider is a client argument only — never exported to the environment.
  `build_model()` and the worker's `judge_model()` both go through the dispatch; the request-
  thread prefetch (`_prefetch_bedrock_api_key`, name kept) resolves whichever key `MODEL_ID`
  needs. Every platform requirements set carries `strands-agents[…,openai]==1.47.0`
  (`strands_agent`, `strands_a2a_agent`, `eval_worker` + its `requirements.lock`,
  `harness_runtime/requirements.txt.tmpl`).
* **Studio** canvases (方式C) run the same block since N-R6-obs2 (r8-D): the adapter
  (`templates/studio_agent.adapt_studio_code(code, spec)`) renders the sliced constants +
  helpers (`model_support.render_model_block`) under the canvas's anchored
  `from strands.models import BedrockModel` import and rebinds the `BedrockModel` name to
  `_LaunchpadStudioModel` — a Strands `Model` that calls `_launchpad_chat_model(model_id,
  **config)` on first `stream()` / `structured_output()`, inside a request (canvas code builds
  its models at import time, before any workload token exists; `config` / `get_config` /
  `update_config` answer from the pending kwargs until then). A node therefore takes any
  catalog model: a Bedrock id → the stock `BedrockModel` (aliased `_LaunchpadStockBedrockModel`,
  bearer from `MODEL_CREDENTIAL`, `MODEL_REGION` — the canvas `region_name` line is ignored);
  an OpenAI-compatible id → `OpenAIModel` on the provider row's `base_url` with the key from
  its own credential provider; `MODEL_INFERENCE_PARAMS` drops what the model rejects on both
  paths, and Bedrock-only kwargs the canvas may emit regardless of the node's model
  (`cache_config`, `additional_request_fields`, …) never reach a chat-completions body. The
  canvas picker (`studio/lib/models.studioModelOptions`) is `lib/models.modelOptionsFor` — the
  provider option keeps the data-contract value `AWS Bedrock` under the label
  `studio.prop.providerCatalog`; unconfigured rows render disabled with `models.unavailable`,
  like every other picker. `AgentSpec` holds a node's id to the catalog + tool-calling
  (`studio_node_models`); the publish is held to a configured provider at the route.
* **BYOC**: `allowed_models` may list an OpenAI-compatible id — `byoc_model_target` returns
  `("openai_compatible", <api_model_name>)`, no Bedrock resource; the user code gets the
  Launchpad id in `MODEL_ID` / `ALLOWED_MODEL_IDS` and performs the same
  `GetResourceApiKey(<credential_provider>)` exchange.
* **IAM**: the per-agent / worker `ModelCredentialApiKey` + `ModelCredentialVaultSecret`
  grants cover the `launchpad-*` provider family (every platform provider is named so),
  plus the spec's own `model_credential` when it is a custom name (`agent_iam.
  model_credential_providers`). No statement names a model.
* **Skill Lab / OpenCode**: `executors.opencode.opencode_providers()` renders the
  OpenAI-compatible providers (base_url, `api_key_env`, `credential_provider`, `id_prefix`,
  models with `tool_call`) as JSON into `SKILLOPT_OPENCODE_PROVIDERS` for the host job env and
  the worker runtime env; the vendored `opencode_backend.build_opencode_config` adds a second
  provider block (`{env:<api_key_env>}`) and addresses a `siliconflow.*` target/judge model as
  `siliconflow/<api_model_name>`; `subprocess_env` inherits the key variable and opens the
  provider host in `NO_PROXY`; the worker (`agentcore_worker`) prefetches the extra provider's
  key on the request thread through Identity (`credential_provider`) and exports it to the CLI
  child only. The host passes its own copy of the key only when `/etc/launchpad/env` has it.
  Default target/judge stay the Bedrock models.
* **Console**: `GET /api/capabilities` → `models[]` (`model_id, label, provider, provider_kind,
  provider_label, api_model_name, tools, streaming, vision, default, available`) and
  `model_providers` (`kind, label, credential_provider, configured, ssm_parameter,
  api_key_env`); `lib/capabilities.parseCapabilities` merges the rows into `lib/models`
  (`applyModelCatalog(rows, providers)` → `MODEL_CATALOG` + `MODEL_PROVIDERS`), so the
  pickers offer the environment's SiliconFlow rows (the static file only knows the two
  Bedrock rows) and show the provider badge (`models.provider.<kind>`, en + zh-CN).
  `modelOptionsFor` (agents) hides `tools: false` rows; judges see the whole catalog. A row
  with `available: false` stays listed but every picker renders it **disabled** with the
  tooltip `models.unavailable` ("<provider label> not configured — store the key as SSM
  parameter <ssm_parameter>"; the label gets the `models.unavailableShort` suffix), and
  `defaultModelFor` never lands on one (N-R6-obs1, below).

### Configured providers and available models (N-R6-obs1)
A provider is **configured** in a workspace when its API key is held there; a catalog model
is **available** when its provider is configured. The answer comes from
`schemas.agent.provider_configured(provider, resources)` / `model_available(model_id,
resources)` / `configured_providers(resources)`:

1. the bootstrap's record `resources.model_credentials[<provider>].configured` on the
   **workspace's** resource map (`provider_credential_resources`, written by `make
   bootstrap`) — it says whether the Identity credential provider the runtimes and workers
   read the key from exists, so it wins over a key that merely sits in the hub env (both
   ways: `configured: false` with the variable set stays unconfigured until bootstrap is
   re-run);
2. without a record (a yaml written before N-R6), whether the hub environment carries the
   provider's `api_key_env`;
3. a `required` provider (Global Bedrock) is always configured — bootstrap refuses to run
   without its key.

`resources=None` means "the hub's own environment" — what the hub-side `build_chat_model`
and the preset skill snapshot (content-hashed and pinned across queue/job, so it must not
depend on a per-request map) use; nothing reads `Settings.resources` for this (workspace
funnel). Every request-boundary guard passes `ws.context.resources` (the proposal validator
reads the catalog summary's `resources`; plan approval the workspace row's).

The check is deliberately **not** part of `AgentSpec` validation: a stored spec must keep
parsing (pipeline resume, canary, optimization re-read `AgentSpec(**row.spec)`) when a
provider's key is rotated away. It is a request-boundary guard, 422
`model.provider_unconfigured` with a message naming the SSM parameter and the variable
(`assert_model_provider_configured` / `assert_model_available` /
`assert_agent_model_available` / `assert_spec_models_available`; the refusal is a
`ModelRefusal(ValueError)` whose `.code` the routes copy into the envelope,
`model_refusal_code`):

| where | guard |
|---|---|
| `POST /api/agents`, `POST /api/agents/{id}/redeploy` | `assert_spec_models_available(spec, ws.context.resources)` — `model_id`, BYOC `allowed_models`, Studio node models |
| judge create / update (`evaluators_local._require_model`) | `assert_model_available(…, resources=ws.context.resources)` |
| architect plan approval (`evaluation_assets._require_judge_models`) | same, with the workspace row's map |
| run start with an actor (`_check_actor_model`) | same |
| recommendation / optimizer model (`recommendations._model_for`) | `assert_model_provider_configured` after the catalog check; `GET /api/experiments/providers` → `models[].available` |
| hub `aws_clients.build_chat_model` (SCORE NOW, insights, optimizers) | `assert_model_available` on the hub env — a 422 code instead of the former `RuntimeError` |
| architect proposal (`proposal.model_catalog_errors`) | the member-facing error names the parameter and lists only selectable ids |

Prose: `model_catalog_entries(resources)` carries `available`; `model_catalog_lines(resources)`
appends `MODEL_UNAVAILABLE_MARKER` = `(not configured in this workspace — not selectable)`
after the id and renders, as the row's first trait, the provider's `ssm_parameter` /
`api_key_env` from `PROVIDERS` ("selectable once the operator stores the SiliconFlow API key
as SSM SecureString parameter `launchpad-siliconflow-api-key` (the host exports it as
`LAUNCHPAD_SILICONFLOW_API_KEY`) and re-runs bootstrap" — N-ARCH-bug3, skill 1.6.1). Both
the per-turn preamble rule and the preset SKILL.md rule tell the architect never to propose
such a model and, when the customer asks for it, to refuse by quoting the parameter named on
that row (there is no console setting for it). The `model_id` validation hint lists only
selectable ids in every branch (N-ARCH-obs9).

## 3. Refusals
| code | when |
|---|---|
| `model.not_allowed` (422) | id outside the catalog — unchanged (a Studio node's OpenAI-compatible catalog id is accepted since N-R6-obs2) |
| `model.tools_unsupported` (422) | an OpenAI-compatible row with `notools` used as `AgentSpec.model_id`, a BYOC `allowed_models` entry or a Studio node — still a valid judge / actor model |
| `model.provider_unconfigured` (422) | a catalog model whose provider has no API key in this workspace (N-R6-obs1) — agent create/redeploy (`model_id`, BYOC `allowed_models`, Studio nodes), judge create/update, plan approval judges, actor model, recommendation/optimizer model, the hub model builder; the message names the SSM parameter (`ssm_parameter`) and the variable (`api_key_env`) to configure and says to re-run bootstrap. The console disables such rows beforehand (`models[].available: false`) |

## 4. Adding a provider or a model
1. **Model on an existing provider** *(historical — since D21 use `POST /api/settings/model-providers/{id}/models`;
   the `LAUNCHPAD_SILICONFLOW_MODELS` / yaml override was removed in M1-e)*: formerly set the
   override for that environment and restart the backend; REPAIR the eval worker and system
   presets so their inlined tables / prose carry the row.
2. **New provider**: one row in `PROVIDERS` (kind `openai_compatible`, endpoint with a
   `# region-boundary: allow …` marker, `launchpad-<name>` credential provider,
   `launchpad-<name>-api-key` SSM parameter, `LAUNCHPAD_<NAME>_API_KEY`), a Settings override
   + `build_model_catalog` branch mirroring SiliconFlow's, the EC2 template parameter +
   `bootstrap-host.sh` lines mirroring `SiliconFlowApiKeyParameter`, and a row in this file.
   Tests in `tests/test_model_catalog.py` enforce that every catalog row answers every trait
   and that every credential provider is `launchpad-*`.

## Registry (D21)

Since D21 (node M1-a, 2026-10-14) the catalog is **data**, not code — the full contract is
[model-registry.md](model-registry.md). What changes for a reader of this file:

| N-R6 (this file) | D21 |
|---|---|
| `PROVIDERS` / `MODEL_CATALOG` module dicts | ledger tables `model_providers` / `models`, published as an immutable `schemas/model_catalog.Catalog`; M1-a kept the names as proxies, **M1-e removed them (C5)** — readers call `providers()` / `provider_row(name)` / `model_catalog_table()` |
| `ALLOWED_MODEL_IDS`, `MODEL_CAPABILITIES`, `MODEL_DEFAULT_MAX_TOKENS` | **removed (M1-e)**; function views `allowed_model_ids()`, `model_capabilities()`, `model_default_max_tokens()` |
| `Settings.siliconflow_models` / `LAUNCHPAD_SILICONFLOW_MODELS` (§"SiliconFlow rows are a Settings override") | **removed (M1-e, C5)** — the first start seeds the rows from the `DEFAULT_SILICONFLOW_MODELS` literal (`source: builtin`, journal `after.siliconflow_seed`); the variable is inert and no warning exists; administrators edit the rows on the Settings page |
| §4 "Adding a provider or a model" (code + Settings + template parameter) | `POST /api/settings/model-providers` / `POST …/{id}/models` (administrators only); the credential is an SSM parameter **name** (`launchpad-<name>-api-key`) registered once through `POST …/{id}/credential` (M1-b) — no code, no restart, no host env variable |
| `provider_configured(provider, resources)` / `model_available(model_id, resources)` | unchanged credential semantics **plus** `model.enabled ∧ provider.enabled ∧ workspace flag` (`model_available(…, workspace_id=None)`); a disabled row is refused at the request boundary with the historical `model.not_allowed` code while stored specs keep parsing |
| `/api/capabilities` `models[]` / `model_providers` | every key identical; additive `catalog_version`, `embedding`, `models[].{kind, source, enabled}` (chat rows only; disabled rows omitted), `model_providers.*.{id, enabled, probe_state}` |
| embedding model (`opensearch_http.EMBED_*`) | a `kind: embedding` row — built-in Titan v2 (dimension 1024), `is_default`; exposed as `schemas.agent.active_embedding()` and `/api/capabilities.embedding` (M1-d wires the OpenSearch / extractor consumers) |
