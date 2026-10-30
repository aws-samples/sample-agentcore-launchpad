# BYOC samples — Bring Your Own Code

Two minimal agents that satisfy the Launchpad BYOC runtime contract:

- ARM64 (aarch64) · port **8080** · `POST /invocations` + `GET /ping`
- invoke payload: `{"prompt": "...", "actor_id": "..."}`
- response: any JSON (or SSE). `{"result": "..."}` is the convention the
  samples follow; `response` / `answer` / `output` / `text` / `message` /
  `content` are read too, a body with none of them is shown verbatim as JSON,
  and `{"error": "..."}` renders as a failed turn.
- the `bedrock-agentcore` SDK (`BedrockAgentCoreApp` + `@app.entrypoint`)
  implements all of the above.

Both call Bedrock Converse with the model id from env `MODEL_ID` and fall back
to an echo that names the reason when the key or the model is unreachable, so
they deploy and chat regardless.

## Model access — the CN model-credential contract

There is no Bedrock endpoint in the runtime's own region (`cn-northwest-1`), so
a BYOC agent must not call `boto3.client("bedrock-runtime")` with defaults — that
is the `EndpointConnectionError` echo of N10-bug3. The samples follow the same
contract as every runtime Launchpad generates
([docs/cn/interfaces/model-credential.md](../../docs/cn/interfaces/model-credential.md)):

| env var (injected by the deployer) | sample reads it as | CN default |
|---|---|---|
| `MODEL_ID` | the Global cross-region profile to Converse with | `global.zai.glm-5.3` (also allowed: `global.moonshotai.kimi-k3`) |
| `LAUNCHPAD_MODEL_REGION` | region of the `bedrock-runtime` client | `us-west-2` |
| `LAUNCHPAD_MODEL_CREDENTIAL` | AgentCore Identity API-key provider holding the workspace's Global Bedrock API key | `launchpad-global-bedrock` |
| `LAUNCHPAD_WORKLOAD_NAME` | self-mint fallback when an invoke carries no workload token (set from the second deploy on) | — |

Per turn the sample (`main.py`, block `<launchpad-model-helpers>` — a verbatim
mirror of `backend/app/templates/strands_agent/main.py.tmpl`):

1. takes the Runtime-injected **workload access token**
   (`BedrockAgentCoreContext.get_workload_access_token()`; the platform sends
   `runtimeUserId` on every invoke), or mints one for `LAUNCHPAD_WORKLOAD_NAME`;
2. exchanges it once per process with **`GetResourceApiKey`** on the provider
   named by `LAUNCHPAD_MODEL_CREDENTIAL` (`IdentityClient(AWS_REGION).dp_client`)
   — the key is never baked into the zip or image;
3. exports the key as **`AWS_BEARER_TOKEN_BEDROCK` before** creating the
   `bedrock-runtime` client in `LAUNCHPAD_MODEL_REGION` with
   `Config(signature_version="bearer")` — botocore reads the bearer at client
   construction, and the runtime's IAM role holds no `bedrock:InvokeModel`;
4. calls Converse with `maxTokens ≥ 256` (GLM 5.3 reasons first; a smaller
   ceiling returns an empty text block) and joins only the `text` blocks —
   `reasoningContent` blocks are not the answer.

The per-agent execution role Launchpad creates already grants exactly the two
Identity calls (`GetWorkloadAccessToken`, `GetResourceApiKey`, scoped to the
one provider) — nothing to configure. A failure anywhere on that path echoes
`[echo — model unavailable (<model> via <provider> in <region>): <Reason>] <prompt>`
and drops the cached key so the next turn re-exchanges it (key rotation needs
no redeploy).

Launchpad sets `MODEL_ID` for you: the wizard's **Allowed models** list
(`spec.byoc.allowed_models` via the API; without the list, just `spec.model_id`)
is restricted to the CN catalog (`ALLOWED_MODEL_IDS` in
`backend/app/schemas/agent.py`; anything else is refused with
`model.not_allowed`). The deployer injects the primary (first) model as env
`MODEL_ID` and the full list as env `ALLOWED_MODEL_IDS` (comma-separated) unless
you set them yourself. An agent that switches models at runtime should pick from
`ALLOWED_MODEL_IDS`.

## Per-user policy — carrying the principal token to the Gateway

When a workspace governs Gateway tools per user (the policy interceptor is on), the hub
sends every invocation a short-lived principal token naming who the agent acts for, at
`payload["launchpad"]["principal"]` (5-minute lifetime for BYOC). Platform-rendered agents
forward it automatically; BYOC code does it with
[`launchpad_policy_principal.py`](launchpad_policy_principal.py), a dependency-free helper
(copy it next to your entry point):

```python
from launchpad_policy_principal import launchpad_principal_body, launchpad_principal_scope

@app.entrypoint
def invoke(payload, context):
    with launchpad_principal_scope(payload):  # this invocation's token, cleared when it ends
        ...
# before signing each Gateway MCP request body (tools/call, tools/list):
body = launchpad_principal_body(body)         # adds params._meta["launchpad/principal"]
```

An async-generator entry point wraps its events instead:
`async for event in launchpad_principal_turn(payload, my_events(payload)): yield event`.
The token belongs to one invocation only: it is set when the invocation starts and cleared
when it ends (also on an error), and an invocation without a token clears any earlier one.

With an httpx SigV4 auth flow, sign `_launchpad_with_principal(request)` instead (on
tools/call and tools/list it also adds the `x-amzn-bedrock-agentcore-policy-session-id`
header; any other request is signed unchanged). Without the token, calls whose
policies name a user are denied. The token is a bearer credential: never log it, never put it
into the model input or memory. Run tool calls one at a time (the policy engine records them
in order).

### Checking the samples without AWS

```bash
bash samples/byoc/check.sh        # py_compile both entry points + hermetic pytest
```

`samples/byoc/tests/` fakes `bedrock_agentcore` and `boto3` before loading each
`main.py` and asserts the whole path above (token → `GetResourceApiKey` on the
named provider → bearer exported before the client → client in the model region
→ Converse on `MODEL_ID`), the echo fallback and the once-per-process cache.

## hello-http — artifact kind `code_zip`

Python source + `requirements.txt`; Launchpad resolves the requirements for
linux/aarch64 at deploy time and runs the zip on the managed Python runtime.

**requirements.txt guidance.** List direct index dependencies only; version
pins are optional — Launchpad compiles the file into a hashed lock
(`requirements.lock`, shipped in the artifact), which is what makes the build
reproducible. The pip file format is honoured (continuations, comments,
environment markers), but `--hash=` options are dropped: the platform re-locks
against its own deploy target — linux/aarch64, `manylinux_2_28` by default (the
runtime is Amazon Linux 2023 / glibc 2.34, measured 2026-09-18; the docs'
`manylinux2014` remains the conservative fallback via
`LAUNCHPAD_RUNTIME_PYTHON_PLATFORM`) — and generates fresh hashes. Refused, so
the file cannot reach outside the platform's package index: `-r`/`-c` includes,
editable installs, local paths, direct URL/VCS entries, and
`--index-url`/`--extra-index-url`/`--find-links`. Source builds never run: a
dependency with no compatible aarch64 wheel fails with the package named — pin
a release that ships one, switch to the `container_source` path, or vendor the
packages in the zip and set `install_requirements=false`. The upload response
(`detected.requirements`) reports the resolve verdict before you deploy.

```bash
cd samples/byoc/hello-http
zip -r ../hello-http.zip . -x '__pycache__/*'   # zip the directory CONTENTS
# — or zip the directory itself; Launchpad normalizes a single top-level dir:
cd samples/byoc && zip -r hello-http.zip hello-http/ -x '*/__pycache__/*'
```

Upload the zip in **Create → Bring Your Own Code → Code zip** (entrypoint
`main.py`), or via the API — see `docs/api.md` (`POST /api/agents/uploads`).

## hello-container — artifact kind `container_source`

The same agent with a `Dockerfile`; Launchpad builds it ARM64 on CodeBuild,
pushes to ECR and deploys the image.

```bash
cd samples/byoc
zip -r hello-container.zip hello-container/
```

Upload in **Create → Bring Your Own Code → Dockerfile build**.

## artifact kind `container_image`

No sample needed — push any image satisfying the contract to a private ECR
repository in the workspace account/region and paste its URI
(`<account>.dkr.ecr.<region>.amazonaws.com.cn/<repo>:<tag>` in the `aws-cn`
partition). The image's code must follow the same model-credential contract as
above to reach a model.
