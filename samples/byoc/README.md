# BYOC samples — Bring Your Own Code

Two minimal agents that satisfy the Launchpad BYOC runtime contract:

- ARM64 (aarch64) · port **8080** · `POST /invocations` + `GET /ping`
- invoke payload: `{"prompt": "...", "actor_id": "..."}`
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

## hello-http — artifact kind `code_zip`

Python source + `requirements.txt`; Launchpad resolves the requirements for
linux/aarch64 at deploy time and runs the zip on the managed Python runtime.

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
