# Interface: model credential (N2 → N6, N7, N8/N9 operators)

Frozen 2026-10-08. Spec C3/C4. Every Bedrock model call in the CN build is a Global Bedrock
cross-region profile entered from `us-west-2` with a **Global Bedrock API key**; no IAM role in
`aws-cn` is ever granted `bedrock:InvokeModel`.

> **N-R6 (2026-10-12):** the catalog now carries a provider dimension — OpenAI-compatible
> endpoints (SiliconFlow first) beside Global Bedrock, each with its own Identity API-key
> provider created by the same `model-credential` stage. This file stays the Bedrock
> reference; the provider tables, key flow and every consuming surface are in
> [model-providers.md](model-providers.md). `ALLOWED_MODEL_IDS` below is now the catalog
> order (the two Bedrock rows first, then the `siliconflow.*` rows of the override).

## Catalog (`backend/app/schemas/agent.py`)
| constant | value |
|---|---|
| `ALLOWED_MODEL_IDS` | `("global.zai.glm-5.3", "global.moonshotai.kimi-k3")` |
| `DEFAULT_MODEL_ID` | `"global.zai.glm-5.3"` |
| `DEFAULT_MODEL_REGION` / `AgentSpec.model_region` | `"us-west-2"` |
| `DEFAULT_MODEL_CREDENTIAL` / `AgentSpec.model_credential` | `"launchpad-global-bedrock"` (Identity API-key provider name) |
| `MIN_MODEL_MAX_TOKENS` | `256` — GLM 5.3 always reasons; below ~200 the text block is empty |
| `DEFAULT_REASONING_MAX_TOKENS` / `MODEL_DEFAULT_MAX_TOKENS` / `default_runtime_max_tokens(model_id)` | `16384` for both catalog models (N10-bug4: a Harness turn with a skill mounted hit `MaxTokensReachedException` at the old flat `4096`); an id outside the catalog keeps `DEFAULT_RUNTIME_MAX_TOKENS = 4096`. Only applies when `AgentSpec.max_tokens` is unset — an explicit value always wins. Rendered into every generated runtime by `templates/strands_agent.rendered_max_tokens(spec)` (zip runtime, A2A, Harness `model/load.py`) |
| refusal | `ValueError` whose message starts `model.not_allowed:` (422 on the API) |

Embeddings (N6: `amazon.titan-embed-text-v2:0`) are not agent models and are **not** in the
catalog; they use the same key and region through the same two mechanisms below.

## Environment variables
| name | who sets it | who reads it |
|---|---|---|
| `LAUNCHPAD_GLOBAL_BEDROCK_API_KEY` | the operator's shell running `make bootstrap` (hub bootstrap); for the **workspace** `model-credential` stage, the hub process's own environment — `workspace_bootstrap._stage_model_credential` runs inside the hub (background thread, not a separate process), so `model_credential.ensure_model_credential`'s `os.environ` read is the hub's | `app/services/model_credential.py` only — never yaml, ledger or logs; missing ⇒ `MissingModelCredentialError` / `BootstrapError` naming it |
| `AWS_BEARER_TOKEN_BEDROCK` | hub process environment (N5 runbook: systemd `EnvironmentFile`), `local_exec.build_execution_env`, and every template right before it builds a `bedrock-runtime` client | botocore ≥ 1.43 — bearer auth for signing name `bedrock`, **read when the client is created** |
| `LAUNCHPAD_MODEL_REGION` | `deployer/environment.py` (= `spec.model_region`), `local_exec` (= `Settings.model_region`) | templates, canvas-generated code |
| `LAUNCHPAD_MODEL_CREDENTIAL` | `deployer/environment.py` (= `spec.model_credential`) | templates (`GetResourceApiKey` provider name); BYOC code |
| `LAUNCHPAD_WORKLOAD_NAME` | `deployer/environment.py` from the second deploy on (= runtime id = workload identity name) | templates' self-mint fallback `GetWorkloadAccessToken` |

**Operational consequence:** since both the hub bootstrap *and every workspace's*
`model-credential` stage read `LAUNCHPAD_GLOBAL_BEDROCK_API_KEY` from the hub process's
own environment, the hub's systemd `EnvironmentFile` (`/etc/launchpad/env`, written by
`infra/ec2/launchpad-cn-ec2.yaml` on the N5 branch) must carry **both**
`LAUNCHPAD_GLOBAL_BEDROCK_API_KEY` (hub bootstrap + every workspace's model-credential
stage) **and** `AWS_BEARER_TOKEN_BEDROCK` (the hub's own Kimi codegen calls) — omitting
either makes every workspace bootstrap fail at the `model-credential` stage (the
former) or the Kimi codegen-fix backend fail every request (the latter), not just the
one `make bootstrap` invocation that happened to export it into a throwaway shell.

## Bootstrap (`model-credential`)
Hub: `run_bootstrap` → `ensure_model_credential(control)` after the stack outputs, before the gateway.
Workspace: `STAGE_ORDER = [validate-access, iam, model-credential, storage, codebuild, gateway, observability, finalize]`.
`GetApiKeyCredentialProvider(name)` → absent: `CreateApiKeyCredentialProvider(name, apiKey)`; present:
`UpdateApiKeyCredentialProvider(name, apiKey)` (rotation in place, no redeploy). Written to
`config/launchpad.yaml` / `Workspace.resources`:
```yaml
resources:
  model_credential_name: launchpad-global-bedrock
  model_credential_arn: arn:aws-cn:bedrock-agentcore:<region>:<account>:token-vault/default/apikeycredentialprovider/launchpad-global-bedrock
  model_credential_secret_arn: arn:aws-cn:secretsmanager:<region>:<account>:secret:bedrock-agentcore-identity!default/apikey/launchpad-global-bedrock-XXXXXX
```

## Runtime contract (`_resolve_bedrock_api_key()`)
Rendered into `strands_agent` and `strands_a2a_agent` (same code), and as
`launchpad_resolve_bedrock_api_key()` by the studio wrapper:
```python
def _resolve_bedrock_api_key() -> str:
    # 1. token = BedrockAgentCoreContext.get_workload_access_token()  (Runtime-injected; the
    #    platform sends runtimeUserId on EVERY invoke — gateway_support.runtime_user_id)
    #    or IdentityClient(AWS_REGION).get_workload_access_token(LAUNCHPAD_WORKLOAD_NAME)
    # 2. IdentityClient(AWS_REGION).dp_client.get_resource_api_key(
    #        workloadIdentityToken=token, resourceCredentialProviderName=MODEL_CREDENTIAL)["apiKey"]
    # 3. cached in-process (dict), RuntimeError (never "") when no token / no apiKey
def _launchpad_bedrock_model(*, api_key: str, **config):
    os.environ["AWS_BEARER_TOKEN_BEDROCK"] = api_key      # before the client exists
    return BedrockModel(**config)                           # strands 1.47 has no api_key kwarg
build_model() == _launchpad_bedrock_model(api_key=_resolve_bedrock_api_key(),
    model_id=MODEL_ID, region_name=MODEL_REGION, max_tokens=MODEL_MAX_TOKENS)  # ≥ 256
```
Streams: events carrying `reasoning` / `reasoningText` are dropped; only `data` becomes `delta`.
Studio code (models built at import) gets a lazy `bedrock-runtime` client instead
(`templates/studio_agent.MODEL_CREDENTIAL_PRELUDE`): the key is exchanged on the first real
client attribute access.

## Tool schemas (N17-obs302)
Every model a generated runtime builds normalizes the tool specs it is handed before they
reach the provider. Kimi K3 rejected every turn of a Harness agent with the code-interpreter
builtin ("Mantle streaming error … The task request was rejected by the target"): the
strands_tools schema carries pydantic's discriminated `oneOf` on `action` and
`format: binary` on `FileContent.blob`, and Kimi refuses both. GLM 5.3 accepts them.
- **Rules**, applied recursively to `inputSchema.json`: `oneOf` → `anyOf` (beside an existing
  `anyOf` it becomes an `allOf` conjunct); `discriminator` is removed; `format` is removed
  only when it is `binary` or `byte` (`date-time`, `uri` … stay). Property names and the data
  keywords (`enum`, `const`, `default`, `examples`, `required`) are never rewritten. The
  rewrite is pure and idempotent, and it never raises: an unexpected shape passes through
  unchanged.
- **Model-agnostic by design** (no per-model flag, no catalog field): the rewrite only
  loosens what the model is told. The tools still validate their own input.
- **One source:** `backend/app/templates/tool_schema.py` (stdlib only). The hub imports it.
  `__LAUNCHPAD_TOOL_SCHEMA_SOURCE__` inlines its text into the model-helpers block of
  `strands_agent/main.py.tmpl`, and `_launchpad_chat_model` returns
  `launchpad_normalize_model_tool_schemas(model)`. That wrapper replaces the instance's
  `stream`, the call every Agent turn and Bedrock's `structured_output` make with
  `tool_specs`. Every runtime's model code is sliced from that block, so all of them are
  covered: zip runtime `build_model()`, the Harness `model/load.py` (`load_model()`, every
  `Agent(model=…)` in the bundle), the Studio adapter (`_LaunchpadStudioModel`), the A2A
  runtime and the evaluation worker's `judge_model`. On the hub, `aws_clients.build_chat_model`
  wraps its BedrockModel / OpenAIModel the same way. The worker's release digest lists
  `tool_schema`. System presets deploy as `harness` / `zip_runtime` and get it through those
  paths. The BYOC samples build no Strands model with tools and stay untouched.
- **Rollout:** the normalizer lives in the rendered bundle, so an agent picks it up on its
  next (re)deploy, which re-renders the bundle. A running runtime keeps the code it was
  packaged with.
- Tests: `backend/tests/test_tool_schema_normalizer.py` (on the captured code_interpreter
  schema, `tests/fixtures/obs302_code_interpreter_input_schema.json`) and
  `backend/tests/test_tool_schema_runtime_wiring.py`. The second renders a Harness agent with
  code-interpreter, KB, gateway and skills, builds its Agent offline on real strands, and
  asserts on the ConverseStream `toolConfig`.

## IAM (per-agent role, `agent_iam._model_credential_statements`, unconditional)
```json
{"Sid": "ModelCredentialApiKey", "Effect": "Allow",
 "Action": ["bedrock-agentcore:GetResourceApiKey", "bedrock-agentcore:GetWorkloadAccessToken"],
 "Resource": ["arn:<partition>:bedrock-agentcore:<region>:<account>:token-vault/default",
              "arn:<partition>:bedrock-agentcore:<region>:<account>:workload-identity-directory/default",
              "arn:<partition>:bedrock-agentcore:<region>:<account>:workload-identity-directory/default/workload-identity/<runtime-name-base>_??????-*",
              "arn:<partition>:bedrock-agentcore:<region>:<account>:token-vault/default/apikeycredentialprovider/<model_credential>"]},
{"Sid": "ModelCredentialVaultSecret", "Effect": "Allow", "Action": ["secretsmanager:GetSecretValue"],
 "Resource": ["arn:<partition>:secretsmanager:<region>:<account>:secret:bedrock-agentcore-identity!default/apikey/<model_credential>-*"]}
```
Build ARNs with `ctx.arn(...)` (partition.md); never literals.

## Hub-side calls
`aws_clients.global_bedrock_runtime_client(settings.model_region)` — bearer-only
(`Config(signature_version="bearer")`, cached per region, `RuntimeError` naming
`AWS_BEARER_TOKEN_BEDROCK` when unset). OpenAI-compatible root:
`aws_clients.global_bedrock_openai_base_url(region)` →
`https://bedrock-runtime.<region>.<dns suffix>/openai/v1` (the Kimi codegen backend posts to
`…/chat/completions` with `Authorization: Bearer <key>`).

Whether the HUB holds a registry provider's key is one predicate on the registry row,
`ProviderRow.hub_holds_key()` (N17-obs280): the built-in Bedrock row is always configured from
the host's Global Bedrock key (`credential_source: host_env`, `key_version` NULL by design); an
OpenAI-compatible row needs its key facts (a key registered from its SSM parameter or typed in
the console — the only licence for the hub's `GetParameter`) or its `credential_env` exported in
the hub process. The per-workspace `resources.model_credentials[<name>]` record written by this
stage answers a different question — whether THAT workspace's Identity vault holds the key its
runtimes read — and is never a hub decision (`model-registry.md` §8.2).

## How N6 reuses it
- **Extractor Lambda / ingest** (no workload identity, so it cannot call
  `GetResourceApiKey` the way a runtime does): `backend/lambdas/memory_extractor/handler.py::
  resolve_bearer(control, secrets, provider_name)` does the **same exchange a workload
  identity hides, by hand, in two calls**: (1) `control.get_api_key_credential_provider(name=
  LAUNCHPAD_MODEL_CREDENTIAL)` on a `bedrock-agentcore-control` client → `apiKeySecretArn`;
  (2) `secrets.get_secret_value(SecretId=apiKeySecretArn)` → `SecretString`, parsed by
  `_secret_to_bearer()` (accepts `{"api_key"|"apiKey"|"key"|"token": "..."}`, any other
  single-string-valued dict, or a bare string — the Identity-managed secret's exact JSON
  shape is unverified pre-N9, hence accepting all of them). The bearer is cached in-process
  by provider name so a warm container never re-fetches, set onto `AWS_BEARER_TOKEN_BEDROCK`,
  then `boto3.Session().client("bedrock-runtime", region_name=MODEL_REGION,
  config=Config(signature_version="bearer"))` is used for both Titan embeddings and the
  GLM/Kimi extraction prompt (`maxTokens ≥ 256`, ignore `reasoningContent` blocks). The
  Lambda execution role (`infra/stacks/memory_stack.py`) grants exactly the two calls above —
  Sid `ModelCredentialProvider` (`bedrock-agentcore:GetApiKeyCredentialProvider` on
  **`token-vault/default`** *and* `token-vault/default/apikeycredentialprovider/<MODEL_CREDENTIAL>`)
  and Sid `ModelCredentialVaultSecret` (`secretsmanager:GetSecretValue` on
  `secret:bedrock-agentcore-identity!default/apikey/<MODEL_CREDENTIAL>-*`) — never a
  bootstrap-injected env var carrying the secret ARN, and never the key material itself.
  **N10-bug2 (live 2026-10-10):** Identity authorises `GetApiKeyCredentialProvider` against the
  token *vault* ARN, not the provider ARN the call names — with only the provider ARN granted
  every extractor invocation was denied on `…:token-vault/default` and KB ingestion / long-term
  memory never ran. The vault-level resource is therefore mandatory; the provider ARN is kept
  alongside (the same dual shape the per-agent role uses, which works live). The vault is one per
  account+region and is not `EnvName`-suffixed. Guarded by
  `infra/tests/test_memory_stack.py::test_extractor_may_read_the_credential_provider_at_the_token_vault_level`.
  Rotation needs no Lambda redeploy (the secret ARN is stable; only its value changes); a
  401/403 from Bedrock drops the cached entry and re-resolves on the next invocation.
- **Runtime-side memory library** (`templates/launchpad_memory/`): call the template's
  `_resolve_bedrock_api_key()` (already defined in `main.py`) — do not duplicate the exchange.

## How N7 reuses it
- The self-built Harness template grafts the same `MODEL_*` block + helper functions (copy from
  `strands_agent/main.py.tmpl`, placeholders `__LAUNCHPAD_MODEL_REGION__`, `__LAUNCHPAD_MODEL_CREDENTIAL__`,
  `__LAUNCHPAD_MODEL_MAX_TOKENS__` via `strands_agent.rendered_max_tokens(spec)`).
- `method="harness"` specs already receive the IAM statements and the env vars (both are
  method-agnostic); `runtime_user_id` is already sent on every invoke.
- `system_agents/presets.py` default model is `DEFAULT_MODEL_ID`; `reasoning_effort=None`.

## How the BYOC samples reuse it (N11 r2, N10-bug3)
- `samples/byoc/hello-http/main.py` and `hello-container/main.py` (identical code) carry a
  verbatim mirror of the template's `<launchpad-model-constants>` / `<launchpad-model-helpers>`
  blocks (minus the A2A header-capture slot) and build a raw `boto3` `bedrock-runtime` client
  in `LAUNCHPAD_MODEL_REGION` with `Config(signature_version="bearer")` after exporting the
  key — the pattern of the extractor Lambda above, fed by the runtime's workload-token
  exchange. `samples/byoc/check.sh` runs the hermetic proof (`samples/byoc/tests/`); see
  `samples/byoc/README.md` § *Model access*. Edit the template block and the samples together.

## Deletion guard (N13-obs22, lead ruling 2026-10-18)
`DELETE /api/identity/connections/{kind}/{name}` refuses a provider that is in use as a model
credential with `409 identity.in_use`: `services/identity_providers.model_credential_references`
collects the workspace's non-deleted agents whose stored `spec.model_credential` is the name and the
model registry providers (`model_providers.credential_provider`, hub-wide) that name it — at most
20 ids in `detail.referenced_by` (`{type: agent | model_provider, id, name}`), `detail.total` and
`detail.truncated` — for an isolation-limited member both follow the masked list (`routers/identity.
_mask_refs`), so the response never counts agents the caller may not see. Online evaluation configs carry no credential reference. The
check runs after the tool-auth / gateway-target guard and **before** the external rule, so
`launchpad-global-bedrock` (external to Launchpad, pinned by every default agent and the `bedrock`
registry provider) answers `identity.in_use`; a vault-only provider nobody pins stays
`identity.external_connection`. Repointing the agents / providers lifts the guard.
