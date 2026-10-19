# Project instructions (Launchpad coding workspace)

You are building an agent that **AgentCore Launchpad** will deploy to Amazon Bedrock
AgentCore Runtime. You write, run and test the code here. **Launchpad deploys it, you
never do**: do not run `agentcore launch`, `docker push`, `aws ... create-*`, CDK,
CloudFormation or any other deploy command. When the project is ready, tell the user
to press **Deploy** in the console. Launchpad then builds it, gives it its own IAM role,
and registers it like any other agent (observability, evaluation, budgets).

## The deploy contract (Launchpad "Bring Your Own Code")

Launchpad deploys this directory in one of two shapes. It picks `container_source` when
a `Dockerfile` sits at the project root, and `code_zip` otherwise.

Both shapes must follow these rules:

- **Serve HTTP on port 8080**, with `POST /invocations` and `GET /ping`. `/ping` answers
  `200` with `{"status": "Healthy"}`.
- **Request body:** `POST /invocations` receives JSON:
  `{"prompt": "<user message>", "actor_id": "<user id>"}`. Ignore any field you do not
  use; more may appear.
- **Response:** reply with JSON, and put the text in `{"result": "..."}`. Launchpad also
  reads `response`, `answer`, `output`, `text`, `message` or `content`. A reply with
  none of them is shown as raw JSON. A reply of `{"error": "..."}` shows as a failed
  turn. A streamed reply (`text/event-stream`) also works. Each event is one
  `data: <json>` line followed by a blank line. Send `{"event": "delta", "text": "..."}`
  chunks, then `{"event": "complete", "result": "..."}`.
- **Simplest way:** use the `bedrock-agentcore` Python SDK, which does all of the above:

  ```python
  from bedrock_agentcore.runtime import BedrockAgentCoreApp

  app = BedrockAgentCoreApp()

  @app.entrypoint
  def invoke(payload):
      return {"result": f"you said: {payload.get('prompt', '')}"}

  if __name__ == "__main__":
      app.run()
  ```
- **Models:** call Amazon Bedrock with the model id in the env var `MODEL_ID`. The
  agent's role may call only the models in `ALLOWED_MODEL_IDS` (a comma-separated
  list), so never hard-code another model id. Credentials come from the runtime role:
  use the default boto3 credential chain, and never put access keys in code.
- **Architecture:** the deployed runtime is **linux/arm64 (aarch64)**.

### Shape 1: `code_zip` (no root `Dockerfile`)

- Python. The entrypoint is `main.py` at the project root, unless the user picks
  another file when deploying.
- Dependencies go in a root `requirements.txt`, with direct dependencies from the
  package index only. Launchpad locks it for linux/aarch64 at deploy time. These are
  refused: `-r`/`-c` includes, `-e` editable installs, local paths, direct URL or VCS
  entries, and `--index-url`/`--extra-index-url`/`--find-links`. Every dependency
  needs a published aarch64 wheel, because source builds never run.
- Python 3.13 is the default runtime (3.10 to 3.12 can be chosen at deploy).
- At deploy, the code runs as `python main.py` on the managed runtime, with no
  OpenTelemetry launcher, so the entrypoint must start the server itself
  (`app.run()`).

### Shape 2: `container_source` (root `Dockerfile`)

- Launchpad builds the root `Dockerfile` for linux/arm64 (CodeBuild) and deploys the
  image. Its own build recipe replaces any `buildspec.yml`.
- The image must listen on `0.0.0.0:8080` and serve the same two routes.
- Use a public base image that supports arm64 (for example
  `public.ecr.aws/docker/library/python:3.13-slim`).

### Limits for both shapes

- The export is the project's tracked files plus untracked files that are not ignored.
  `.git`, `node_modules`, `.venv` and symlinks are never included.
- The export must stay under 50 MB.
- Do not commit generated artifacts.

## This workspace

- **Storage:** the project lives on a 1 GB session store (`/mnt/workspace/project`),
  with roughly 100 000 files at most. Keep it small.
- **Dependencies:** `node_modules` and `.venv` are symlinks to local disk, and so are
  the uv, npm and pip caches. They are not saved. **They come back empty after the
  workspace restarts** (an idle timeout, an update, a restore), so reinstall them
  before you run anything. For example:
  - `uv venv .venv && uv pip install -r requirements.txt`, or `uv sync`
    (`UV_PROJECT_ENVIRONMENT` already points at the local-disk venv);
  - `npm install`.
- **Never put secrets in files:** no keys, passwords or tokens. The project is saved
  to S3 and deployed as-is.
- **Commands run without approval** and the user can interrupt you. Prefer short,
  bounded commands. Do not start servers that run forever. Start them in the
  background and stop them when you are done (for example
  `python main.py & sleep 3; curl -s localhost:8080/ping; kill %1`).
- **Test locally before you say you are done:**

  ```bash
  curl -s -X POST localhost:8080/invocations \
    -H 'content-type: application/json' \
    -d '{"prompt": "hello", "actor_id": "test"}'
  ```
- **Git:** this directory is a git repository. Commit meaningful steps with clear
  messages. The console shows the diff since the last commit and the diff of each
  turn.
