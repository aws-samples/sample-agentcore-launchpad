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

Both call Bedrock Converse with the model id from env `MODEL_ID`
(default `us.anthropic.claude-3-5-haiku-20241022-v1:0`) and fall back to an
echo when the account has no model access, so they deploy and chat regardless.

Launchpad sets `MODEL_ID` for you: the agent's execution role may invoke only
the models in the wizard's **Allowed models** list (`spec.byoc.allowed_models`
via the API; without the list, just `spec.model_id`). The deployer injects the
primary (first) model as env `MODEL_ID` and the full list as env
`ALLOWED_MODEL_IDS` (comma-separated) unless you set them yourself — so these
samples always call a permitted model with no extra configuration. An agent
that switches models at runtime should pick from `ALLOWED_MODEL_IDS`.
Selections accept literal IDs, foundation-model ARNs and system inference-profile
ARNs. Wildcards, IAM variables and application inference-profile ARNs are refused;
an unknown ID never grants access to all foundation models.

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
zip -r ../hello-http.zip .        # zip the directory CONTENTS
# — or zip the directory itself; Launchpad normalizes a single top-level dir:
cd samples/byoc && zip -r hello-http.zip hello-http/
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
(`<account>.dkr.ecr.<region>.amazonaws.com/<repo>:<tag>`).
