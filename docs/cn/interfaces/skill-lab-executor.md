# Interface: Skill Lab executor seam (N13 → N9/N10 operators, future executors)

Frozen 2026-10-10 (D16). Spec `docs/cn/GRAPH-LOOP-SPEC.md` §N13, `REQUIREMENTS.md` §4.2;
spike `docs/cn/reports/n13-opencode-spike.md` (verdict GO-WITH-ADAPTERS, pin 1.18.35).

Skill Lab keeps the Global architecture — vendored SkillOpt CLIs run as **host subprocesses**
in `data/skill-lab-venv`, rollouts run one task per microVM on the **exec-worker Runtime**
(`launchpad_skill_lab_worker`, BYOC image), the judge/optimizer is `bedrock_chat` (Converse)
— and replaces exactly one thing: the exec agent CLI. Claude Code and Codex are deleted
(not flagged); OpenCode is the one executor. Everything OpenCode-specific lives in two
files, one per side of the subprocess boundary:

| side | module | owns |
|---|---|---|
| vendored (runs in the venv subprocess and inside the worker image) | `vendor/skillopt/skillopt/model/opencode_backend.py` | `ExecutorBackend` protocol + `BACKEND`, `run_opencode_exec`, the headless command, `opencode.json`, the child env, the event-stream parser, the restricted judge agent |
| platform (runs in the FastAPI process) | `backend/app/skill_lab/executors/opencode.py` | the backend id the API offers, host/worker env contracts, judge route, CLI probe, model-credential name |

The platform never imports the vendored tree (`tests/test_skill_lab_foundation.py::
test_backend_never_imports_vendored_skillopt`); the two modules agree on **environment
variable names** only.

## 1. `ExecutorBackend` (vendored protocol)

```python
@runtime_checkable
class ExecutorBackend(Protocol):
    name: str                                   # "opencode_exec"
    def run(self, *, work_dir: str, prompt: str, model: str, timeout: int,
            allow_file_edits: bool = False, policy: dict | None = None) -> tuple[str, str]: ...
    def exec_config(self) -> dict[str, Any]: ...   # {"path", "effort", "empty_response_retries"}
    def cli_binary(self) -> str: ...               # "opencode"
```

`run()` returns `(response, raw)`: the final answer text and the persisted transcript.
`run_opencode_exec(work_dir, prompt, model, timeout, images=None, data_dirs=None,
allowed_tools=None, permission_mode=None, allow_file_edits=False, policy=None)` keeps the
keyword surface of the executor it replaced, so `envs/skilleval/rollout.py`,
`envs/skilleval/judge_worker.py`, `scripts/generate_tasks.py`, `model/agentcore_worker.py`
and `codex_harness.run_target_exec` call it unchanged. Allow-lists: `backend_config.
EXEC_BACKENDS == {"opencode_exec"}`; `agentcore_runner` / `agentcore_worker` accept only it.

A future CLI = one new vendored module implementing the protocol + one new platform module
exporting the same names as `executors/opencode.py`; nothing else changes.

## 2. Command (spike §2/§5)

```
opencode run --pure --format json --title skillopt --dir <work_dir> \
  --model bedrock-openai/<model id> [--agent skillopt-judge] [-f <image>…] <prompt>
```
`cwd=work_dir`, **`stdin=DEVNULL`** (the CLI blocks on a non-TTY stdin otherwise),
`subprocess.run(timeout=…)` as the only time limit. Before the call the work_dir is
`git init -q`-ed (idempotent; `.git` is already a reserved root) so project-root discovery
stops there (spike finding 4). The prompt is the harness wrapper
(`codex_harness._exec_prompt` + `_retry_prompt`): read `task.md` and
`.agents/skills/skillopt-target/SKILL.md`, edits allowed/forbidden, answer in
`<answer>…</answer>`. Empty response → `EXEC_EMPTY_RESPONSE_RETRIES` (default 1) retries,
each attempt a `===== OPENCODE ATTEMPT n =====` segment of `raw`.

## 3. `opencode.json` (written per run to `<prediction dir>/.opencode-home/opencode.json`,
pointed at by `OPENCODE_CONFIG` — never inside the work_dir)

```jsonc
{
  "$schema": "https://opencode.ai/config.json",
  "provider": { "bedrock-openai": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "Amazon Bedrock (OpenAI-compatible)",
      "options": { "baseURL": "<SKILLOPT_OPENCODE_BASE_URL>",        // https://bedrock-runtime.<model region>.<partition suffix>/openai/v1
                   "apiKey": "{env:AWS_BEARER_TOKEN_BEDROCK}" },     // resolved by the CLI at runtime — never written
      "models": { "global.moonshotai.kimi-k3": {"tool_call": true, "reasoning": true, "limit": {"context": 262144, "output": 16384}},
                  "global.zai.glm-5.3":        {"tool_call": true, "reasoning": true, "limit": {"context": 200000, "output": 16384}} } } },
  "model": "bedrock-openai/<model id>",
  "snapshot": false,
  "permission": { "read": "allow", "glob": "allow", "grep": "allow", "list": "allow", "bash": "allow",
                  "edit": "allow|deny", "write": "allow|deny",               // = allow_file_edits
                  "webfetch": "deny", "websearch": "deny", "external_directory": "deny" }
}
```
Judge (`policy` dict from `agentic_judge.build_backend_policy`): same provider; every
permission `deny`; `tools` = every built-in `false` + `<server>_<tool>: true` for the
policy's allowlisted Artifact MCP tools (`JUDGE_ALLOWED_TOOLS`); `mcp.<name> = {"type":
"local", "command": [...], "enabled": true}` (exactly one required server, else
`RuntimeError`); `instructions: []`; `agent.skillopt-judge = {"mode": "primary", "prompt":
<trusted system prompt>, tools, permission}`; command adds `--agent skillopt-judge`. **N13 v1
ships `judge_mode=chat`** (spike §10a): the judge is `bedrock_chat`; the agentic agent is
wired but awaits its own live spike (OpenCode has no structured-output flag).

## 4. Environment contract

| variable | set by | read by | value |
|---|---|---|---|
| `SKILLOPT_OPENCODE_BASE_URL` | platform: `executors.opencode.provider_base_url()` = `aws_clients.global_bedrock_openai_base_url(settings.model_region)` → host job env (`runner.build_job_env`) and worker runtime env (`infra.worker_runtime_environment`) | vendored `opencode_backend.provider_base_url()` (fail-closed `RuntimeError` when unset) | `https://bedrock-runtime.us-west-2.<Global suffix>/openai/v1` — never a literal hostname in code |
| `AWS_BEARER_TOKEN_BEDROCK` | host: `aws_clients.require_global_bedrock_bearer()` (hub env; missing ⇒ 503 `skill_lab.model_credential_missing` **at submit** — `runner.require_model_credential` probes `host_env` in every submit path before any row, N13-obs20; `build_job_env` re-probes at job start) · worker: `agentcore_worker.resolve_model_api_key()` — Runtime-injected workload token (`runtimeUserId` on every invoke) → `GetResourceApiKey(LAUNCHPAD_MODEL_CREDENTIAL)`, exported for the CLI child only | OpenCode (`{env:…}`), `bedrock_chat` (pins `signature_version="bearer"` when present) | the Global Bedrock API key — env only, never disk/log/yaml |
| `SKILLOPT_OPENCODE_NO_PROXY` | platform: `executors.opencode.egress_allowlist()` | vendored `subprocess_env` → `NO_PROXY` | `<model partition suffix>,<workspace dns suffix>,169.254.169.254,localhost,127.0.0.1` |
| `SKILLOPT_OPENCODE_PROVIDERS` (N-R6) | platform: `executors.opencode.opencode_providers_env()` — the catalog's OpenAI-compatible providers as JSON (`name`, `base_url`, `api_key_env`, `credential_provider`, `id_prefix`, `models{tool_call}`) → host job env and worker runtime env | vendored `opencode_backend.extra_providers()` / `provider_for_model()`: a `siliconflow.*` target or judge gets its own provider block (`{env:LAUNCHPAD_SILICONFLOW_API_KEY}`) and is addressed as `siliconflow/<api model>`; `subprocess_env` inherits the key variable and opens the host in `NO_PROXY`; the worker prefetches the key via Identity (`credential_provider`) | `{"siliconflow": {…}}`; absent/empty ⇒ Bedrock-only behaviour (docs/cn/interfaces/model-providers.md) |
| `LAUNCHPAD_MODEL_CREDENTIAL`, `LAUNCHPAD_MODEL_REGION`, `BEDROCK_CHAT_REGION` | platform | worker (`resolve_model_api_key`), vendored `bedrock_chat` | `launchpad-global-bedrock` / `us-west-2` |
| `OPENCODE_EXEC_PATH`, `OPENCODE_EXEC_EFFORT`, `EXEC_EMPTY_RESPONSE_RETRIES` | vendored CLIs (`--opencode_exec_path/--opencode_exec_effort`, `opencode_exec_*` config keys) | `opencode_backend.configure_opencode_exec` | `opencode` / `low|medium|high` / `1` |

CLI child env (`opencode_backend.subprocess_env`, allow-listed — never `os.environ.copy()`):
inherited `PATH HOME LANG LC_ALL SSL_CERT_FILE SSL_CERT_DIR AWS_BEARER_TOKEN_BEDROCK`;
`OPENCODE_CONFIG=<job home>/opencode.json`; `OPENCODE_DISABLE_AUTOUPDATE/MODELS_FETCH/SHARE/
CLAUDE_CODE_SKILLS/LSP_DOWNLOAD/DEFAULT_PLUGINS=1`; `XDG_CONFIG_HOME XDG_DATA_HOME
XDG_CACHE_HOME XDG_STATE_HOME TMPDIR` under `<job home>/{config,data,cache,state,tmp}`;
`$XDG_CACHE_HOME/opencode/bin/rg` symlinked from the system ripgrep (spike finding 2);
`HTTPS_PROXY=HTTP_PROXY=http://127.0.0.1:9` + `NO_PROXY` (above) so only Bedrock (and the
worker's own partition) is reachable. The worker image pins the same `OPENCODE_DISABLE_*`
set in `ENV`.

## 5. Transcript schema (`--format json`, one JSON object per line; parser tolerant)

| event | fields read | → transcript |
|---|---|---|
| `step_start` / `step-start` | — | opens a turn (`turns[]`) |
| `text` | `part.text` | `text_parts[]`, the turn's texts |
| `tool_use` / `tool` | `part.tool`, `part.state.{status,input,output,error,title}` | `tool_calls[] = {tool,status,input,output,error,title}` |
| `step_finish` / `step-finish` | `part.tokens.{input,output,reasoning,cache.{read,write}}` (per model call) | summed into `usage = {input,output,reasoning,cache_read,cache_write}` |
| `error` | `part.error.{name,data.message}` (or top-level `error`) | `errors[]` → response `""` + `{"type":"exec_error","backend":"opencode_exec","error":…}` marker (`extract_exec_failure`) |

`final_message` = the text parts of the **last step** (after the final `step_start`),
else the last text part. `raw` = stdout verbatim (+ `[stderr]`) + one usage line
`{"type":"usage","input_tokens","cache_creation_input_tokens","cache_read_input_tokens",
"output_tokens" (= output + reasoning), "reasoning_tokens"}` — the key shape
`codex_harness.extract_exec_usage` already reads, so results keep their usage columns.
Artifacts: `<prediction dir>/opencode_raw.txt`, `opencode_trace_summary.txt`
(`build_trace_summary`: tool calls, tool/model errors, answer format).

Fixtures `backend/tests/fixtures/opencode/*.jsonl` carry this shape (from the spike's
verified schema); `tests/test_skill_lab_opencode_backend.py` (29 hermetic tests) is the
contract test.

## 6. Worker protocol (unchanged granularity, N12-aligned names)

Per exec call: `s3://<artifacts>/skill-lab/exec-jobs/<job>/{in.tar.gz, started.json,
status.json, progress/…, result.json, out.tar.gz, done.json}` (debris older than 7 days is
swept after every job by `runner.sweep_exec_jobs_prefix`, best-effort, through the shared
reporting deleter `services/s3_sweep.py` since N17-obs190 `cn/r17-u` — the log line counts
what S3 removed and names refused keys by count and code, `removed 1 stale object(s), 1 of 2
objects refused (AccessDenied)`, never a key or the S3 message); `cancel` marker (written by
the runner on an interrupted wait, honoured by the worker before launching the CLI);
`InvokeAgentRuntime(runtimeSessionId=skillopt-exec-<job>-…, runtimeUserId=skill-lab)`;
`HealthyBusy` via `add_async_task`; runtime `lifecycleConfiguration = {idle 300 s, max 8 h}`;
runtime name `resource_name("launchpad_skill_lab_worker")` with `-`→`_` (D13 EnvName);
role `launchpad-skill-lab-worker-<region>[-<env>]` with the ModelCredential statements of
every agent role and no `bedrock:InvokeModel`.

`result.json` is `{response, raw, error[, error_kind]}` and `done.json` carries
`{state: COMPLETED|FAILED, error, error_kind, …}`; when `error` is set, `error_kind` is
`infrastructure` (the worker never ran the CLI: download / exec config / cancel marker /
model-key resolution failed) or `exec` (the CLI ran and failed). The host's
`agentcore_runner._raise_for_remote_error` raises `AgentCoreRunnerError` for the former —
the rollout marks the row `score_valid=False`, `error_type="infrastructure"` (excluded
from scoring) — and the legacy `RuntimeError` for the latter (a scored `artifact_failure`).
The job itself runs inside `contextvars.copy_context()` so the Runtime-injected workload
token of the invoke reaches the job thread (N10v3-bug2/bug3).

### 6.1 Worker environment — local disk vs the session-storage mount (N10v4-bug1)

The worker runtime carries a `filesystemConfigurations` session-storage mount at
`/mnt/workspace`. It is a **network filesystem: sqlite locking does not work on it** — with
the job's OpenCode home placed there every exec exited 1 with `Unexpected error — database
is locked` (OpenCode keeps its db under `$XDG_DATA_HOME`). Rules, enforced by
`agentcore_worker` and `tests/test_skill_lab_worker_disk.py`:

* `_work_root()` is **local container disk** (`tempfile.gettempdir()`) by default. A job gets
  the persistent mount for its work_dir only when its payload says `persistent_workspace:
  true` *and* the mount is present and writable (`_session_storage_writable()`).
* The per-job OpenCode home — `XDG_CONFIG_HOME`, `XDG_DATA_HOME`, `XDG_CACHE_HOME`,
  `XDG_STATE_HOME`, `TMPDIR`, `opencode.json` — is **always** on local disk:
  `<tmp>/opencode-homes/<job>` (`_opencode_home`), handed to `run_opencode_exec(home=…)` →
  `write_opencode_config(…, home=)` / `subprocess_env(…, home=)`, created before the CLI
  starts and removed in the job's `finally` (it is never part of `out.tar.gz`). Without an
  explicit `home` the backend keeps the host-side layout `<prediction dir>/.opencode-home`.
* `ping` reports `work_root`, `session_storage` (whether the default root *is* the mount —
  now `false`), `session_storage_writable` and `opencode_home_root`.

## 7. Image contract (`vendor/skillopt/deploy/agentcore/Dockerfile`, spike §6/§9)

`ARG BASE_IMAGE` (default `docker.m.daocloud.io/library/python:3.13-slim`; the ECR mirror
`…/base/python:3.13-slim` when `Settings.base_image_mirror_enabled`), `APT_MIRROR`,
`PIP_INDEX_URL`, `NPM_CONFIG_REGISTRY`, `OPENCODE_VERSION=1.18.35`,
`OPENCODE_SHA256=57fd6ecf…84ba` — rendered as build args by `worker_build.render_buildspec`
from Settings (`apt_mirror`, `codebuild_pip_index_url` + `codebuild_pip_fallback_index_url` — the BUILD mirrors, not the host's `pip_index_url` (N11-r3-bug7) —, `npm_registry`,
`skill_lab_worker_opencode_version`) and hashed into the image tag. linux/arm64, no Node,
`opencode-linux-arm64-<ver>.tgz` from the npm mirror, `rg` symlink, `OPENCODE_DISABLE_*`
ENV. `tests/test_skill_lab_foundation.py::test_worker_cli_version_pin_matches_the_dockerfile`
pins version parity and forbids `claude.ai/install`, `public.ecr.aws`, `github.com`,
`npm install -g`.

## 8. Publish (hub side — `skill_lab.jobs.publish_job`)

`POST /api/skill-lab/jobs/{id}/publish` pushes a succeeded training job's `best_skill.md`
onto its source Registry record through `registry_console.update_record` (a bumped minor
version; the record settles into DRAFT, `reapprove` walks it back through the lifecycle — to
APPROVED only for an administrator; a member's publish re-submits it and stops at
PENDING_APPROVAL, `pending_approval: true` (N17-obs248)).
**Source-record ownership (N17-obs263):** two gates run — the job's creator-or-admin
(`403 skill_lab.forbidden`, router) and the **source record's** creator-or-admin
(`registry_console.require_record_mutable`, `403 registry.forbidden {created_by,
created_by_name, record_id, action: "publish"}`; a `null`-creator record — platform sample,
Skill Lab samples, administrator imports — is an administrator's). The record gate runs after
the job-state checks (`publish_unsupported` / `results_pending` / `publish_no_change`) and the
system-skill refusal, before the idempotence check, the multi-file split (`publish_skill/`
rebuild) and any S3 / record write, so a forbidden publish leaves the record's bytes, version
and status as they were. A member may still train on any record they can read; publishing
onto someone else's record is refused (download `best_skill.md` from the artifacts instead).
**Idempotent on content (N16-obs44):** when the record's latest version already carries this
exact rendering — its `skillMd.inlineContent` is byte-identical (single-file publish), or
this job's last publish (`params.published = {digest, version, at}`) pushed this digest and
the record is still at that version — the call answers `200` with the existing
`new_version`, `status_after == status_before`, `reapproved: false` and the additive
`unchanged: true`, and reaches neither `update_record` nor `reapprove_record`. Changed
content (or a record another writer moved on) mints a new version as before
(`unchanged: false`).

## Catalog version in the OpenCode provider table (D21 M1-d)

`opencode_providers()` renders from the registry snapshot (the `PROVIDERS` /
`MODEL_CATALOG` views) at job submit; each provider entry additionally carries
`catalog_version` (the registry journal id). It is placed **inside** every provider entry,
never at the top level of `SKILLOPT_OPENCODE_PROVIDERS` — the vendored
`opencode_backend.extra_providers()` treats every top-level key as a provider id.
