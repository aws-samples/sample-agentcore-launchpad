# Launchpad deviations from upstream SkillOpt

Vendored subset of https://github.com/xiehust/SkillEvalOpt_Studio
(a fork of microsoft/SkillOpt, MIT — see `LICENSE`).

- **Upstream pin:** commit `ae82de4df9176d7eef726a8a9e1991520bcc7ba3` (2026-08-18).
- **Consumed by:** `backend/app/skill_lab/` — always as a *subprocess* running in the
  dedicated venv `data/skill-lab-venv/` (built from `requirements-launchpad.txt`).
  Launchpad backend code must never import modules from this tree in-process
  (`backend/app/` import rule; the boto3 funnel guard does not scan this tree).
- Re-vendoring: re-copy the subset from a newer upstream commit, re-apply the
  patches below, update the pin.

## Subset (what was copied)

`scripts/{evaluate_skill,train,generate_tasks}.py`, `configs/{_base_,skilleval}/`,
`deploy/agentcore/Dockerfile`, and the `skillopt/` package minus: all benchmark
envs (`envs/*` except `base.py`, `__init__.py`, `skilleval/`), `scheduler/`
(unused placeholder), `engine/plugin_trainer.py`, `evaluation/plugin_gate.py`,
`model/{codex_backend,router}.py` (unreferenced by the vendored entry points), and —
since patch 7 (D16) — `model/claude_backend.py` plus upstream's `deploy/agentcore/
codex-config.toml` (the Claude Code / Codex executors are gone from this tree).
Also copied (2026-08-18): `data/skilleval_demo/` — the self-authored demo
skills (`logtriage_skill`, `logtriage_skill_v2`, `report_skill/initial.md`) and
demo task sets (`logtriage_tasks/`, `report_tasks/`, `xlsx_tasks.json`), seeded
by `backend/app/skill_lab/samples.py` as registry records + read-only sample
task sets. Deliberately NOT copied from upstream's sample surface: `ckpt/`
paper-checkpoint skills (research payloads stay out of this public repo) and
the benchmark subsets (searchqa/livemath/officeqa — external dataset content;
upstream itself only commits ID-only manifests for licensing reasons).

Not copied: `skillopt_studio/`, `skillopt_webui/`, `skillopt_sleep*/`, `plugins/`,
`tests/`, `ckpt/`, `data/` (except `data/skilleval_demo/` above), docs,
`scripts/agentcore/` (replaced by Launchpad bootstrap).

## Patches

### 1. `skillopt/model/bedrock_chat.py` (new) + dispatch wiring

Bedrock-native chat backend on boto3 `bedrock-runtime` Converse for the
optimizer role (reflection/patch generation and the skilleval chat judge) —
zero-key: default credential chain (instance role), no endpoint/bearer/CLI.
Wired in `model/__init__.py` (imports, four chat dispatch branches, token-summary
merge, reset/effort/deployment fan-outs), `model/backend_config.py` (optimizer +
target allow-lists), `model/common.py` (aliases `bedrock`/`bedrock_chat`, default
model `global.zai.glm-5.3` since D10/D16 — the CN judge/optimizer default).
Since D16 the client pins `signature_version="bearer"` whenever
`AWS_BEARER_TOKEN_BEDROCK` is present: the CN host and worker reach Global Bedrock
with an API key, never a SigV4 identity (docs/cn/interfaces/model-credential.md). All edits carry a `# LAUNCHPAD PATCH`
marker. `tools`/`tool_choice`/`return_message` raise `NotImplementedError`
(no optimizer-role caller in this subset passes them).

### 2. `deploy/agentcore/Dockerfile` rework (D16: OpenCode worker image)

Upstream copied host `claude`/`codex` binaries into the image; the first Launchpad
variant installed pinned Claude Code / Codex CLIs. Since D16 the image carries ONE
exec agent: the OpenCode CLI, installed as the single arm64 binary inside the npm
platform package `opencode-linux-arm64` (`ARG OPENCODE_VERSION=1.18.35`, the version
the N13 spike validated in an arm64 container; the tarball's sha256 is pinned beside it
and checked at build time) fetched from the China npm mirror — no Node runtime, no
npm. Base image, apt mirror, pip index and npm registry are build args rendered by
`backend/app/skill_lab/worker_build.py` from Settings (`base_image_mirror_*`,
`apt_mirror`, `pip_index_url`, `npm_registry`), so a China build never leaves the
mirrors; nothing is pulled from `claude.ai`, GitHub, Docker Hub or `public.ecr.aws`. No model
region, credential or key is baked in: the runtime env carries `AWS_REGION`,
`SKILLOPT_OPENCODE_BASE_URL` (Bedrock's OpenAI-compatible root, built with the
partition helper) and `LAUNCHPAD_MODEL_CREDENTIAL`; the worker exchanges its
Runtime-injected workload token for the Global Bedrock API key per session (patch 9).
Built by Launchpad's CodeBuild ARM64 pipeline from the assembled context (upstream's
`build_and_push.sh` / `scripts/agentcore/setup_infra.py` are not vendored); the
codex-home staging of the first variant is gone with the executor.

### 3. `requirements-launchpad.txt` (authored, replaces upstream `requirements.txt`)

The import closure of *this subset* only, so the venv stays small: upstream's
`numpy`/`httpx` are dropped (nothing in the vendored tree imports either — they
serve the excluded benchmark envs), and `json_repair` is promoted from an
upstream extra to a hard dependency, because its absence makes
`skillopt.utils.json_utils` silently drop a malformed judge/analyst edit and the
malformed-JSON case is exactly the non-OpenAI backend this integration uses.
Floors otherwise match upstream's.

### 4. `scripts/validate_tasks.py` (new, launchpad addition)

Thin CLI over `skillopt.envs.skilleval.dataloader.load_tasks` so the Launchpad
backend's task-set validation is by construction identical to what
`evaluate_skill.py` / `train.py` accept. JSON-on-stdout protocol; exit 0 when
the validator ran (failures are data). The only third-party import the loader's
chain reaches is `openai` (via `skillopt.model.azure_openai`), which the script
replaces with a placeholder when absent — `load_tasks` never calls a model, so
verdicts are identical either way (checked against the real venv interpreter).
That is what lets hermetic backend tests run this validator with a bare
interpreter instead of requiring the provisioned venv.

### 5. `skillopt/model/codex_harness.py` — reduced to the backend-agnostic harness (D16)

The first Launchpad variant patched this module's Codex judge transport (seeded
`CODEX_HOME`, pre-approved MCP tools). Since D16 the Claude Code / Codex executors —
CLI and SDK paths, both judge transports, structured-output plumbing, trace parsers
— are deleted, not flagged. What remains is everything that is not about a
particular CLI: `prepare_workspace` / `render_skill_md`, the prompt wrappers,
`_persist_artifacts`, `extract_exec_usage` (now fed by the OpenCode usage line) and
`extract_exec_failure`; `run_target_exec` dispatches to the OpenCode backend
(patch 7). The module keeps its historical name so the import graph stays diffable.

### 6. `skillopt/envs/skilleval/evaluator.py` — text-judge output evidence

Both chat-judge entry points include modified seeded files when the rollout
manifest identifies them as outputs. Previously, a task that rewrote a supplied
report lost that report's body because every input path was excluded. Unchanged
inputs remain excluded. Text evidence permits 16,000 characters per file and
64,000 total (still at most eight files), with explicit truncation markers.
This lets ordinary reports include their findings and next actions beyond the
old 2,000-character preview. Scoring instructions and verdict parsing are unchanged.
Markdown task outputs should use chat/auto judging; the binary artifact inspector
does not support reading Markdown bodies.

### 7. `skillopt/model/opencode_backend.py` (new) — the one exec backend (D16 / N13)

The single module that knows OpenCode: `ExecutorBackend` protocol + `BACKEND`,
`run_opencode_exec` (same keyword surface as the executor it replaces, so
`envs/skilleval/rollout.py`, `judge_worker.py`, `scripts/generate_tasks.py` and
`model/agentcore_worker.py` call it unchanged), the headless command
(`opencode run --format json --model bedrock-openai/<id> [--agent skillopt-judge]`),
the per-run `opencode.json` (custom `@ai-sdk/openai-compatible` provider at
`$SKILLOPT_OPENCODE_BASE_URL`, API key referenced as
`{env:AWS_BEARER_TOKEN_BEDROCK}` — never written to disk; permission block
`edit allow|deny / bash allow / webfetch deny`), the restricted judge agent (every
built-in tool off, exactly one required stdio MCP server, only its allowlisted tools
on, no project instructions, trusted system prompt as a primary agent), and the
tolerant `--format json` event-stream parser (`text`, `tool_use`/`tool`,
`step_start`/`step_finish` tokens, `error`) producing the transcript the pipelines
read (final message, tool calls, usage). The persisted raw carries one
claude-key-shaped usage line so `extract_exec_usage` is unchanged. Wiring:
`backend_config.py` (exec set `{opencode_exec}`, re-exports), `common.py` (aliases
`opencode`/`opencode_exec`, default `global.moonshotai.kimi-k3`), `model/__init__.py`
(legacy `set_backend("opencode")`), `agentic_judge.py` (`_SUPPORTED_BACKENDS`,
`JUDGE_ALLOWED_TOOLS` as `<server>_<tool>`, the preflight probes the opencode
binary), `adapter.py` / `configs/*.yaml` / `scripts/*.py` / `config.py` /
`engine/trainer.py` (`opencode_exec_path|effort` replace the codex/claude knobs),
`gradient/reflect.py` (the codex-only trace blocks are gone). Exact CLI flags, the
event schema and the version pin are what the N13 spike confirms
(`docs/cn/reports/n13-opencode-spike.md`); the hermetic fixtures under
`backend/tests/fixtures/opencode/` are replaced by its captures.
`run_opencode_exec(home=…)` → `write_opencode_config` / `subprocess_env` / `job_home`
accept an explicit per-job home (N10v4-bug1): the AgentCore worker puts the OpenCode XDG
dirs on local container disk instead of beside a work_dir on the session-storage mount,
where sqlite locking fails; host-side callers pass nothing and keep `.opencode-home`.

### 8. `skillopt/model/agentcore_runner.py` — runtimeUserId + cancel marker (D16)

Every `InvokeAgentRuntime` carries `runtimeUserId` (`SKILLOPT_AGENTCORE_RUNTIME_USER_ID`,
default `skill-lab`) so the Runtime injects the workload access token the worker
needs for the model credential; the remote-exec config no longer strips
`use_sdk`/`profile` (gone); an interrupted wait writes `<job_prefix>/cancel` and
stops the session (cheap alignment with the N12 evaluation-worker protocol).

### 9. `skillopt/model/agentcore_worker.py` — Identity-resolved key + job markers (D16)

`resolve_model_api_key()` mirrors the runtime template's `_resolve_bedrock_api_key()`
(workload token → `GetResourceApiKey` on `LAUNCHPAD_MODEL_CREDENTIAL`) and the key
is exported as `AWS_BEARER_TOKEN_BEDROCK` for the CLI child only. The worker writes
`status.json` on start and `done.json` beside `result.json` at the end, and honours a
`cancel` marker before launching the CLI. Ping reports the OpenCode version and
whether the base URL / credential env are set.

### 10. Infrastructure vs exec failure classification (N10v3-bug3)

`agentcore_worker` tags `result.json` / `done.json` with `error_kind: infrastructure|exec`
(set-up / model-key failures vs the CLI raising); `agentcore_runner._raise_for_remote_error`
turns the former into `AgentCoreRunnerError` and `envs/skilleval/rollout.py::_rollout_one`
catches it as an **invalid row** (`score_valid=False`, `error_type="infrastructure"`) instead
of the scored `artifact_failure` zero; exec failures keep upstream semantics. The worker's
background job also runs under `contextvars.copy_context()` (N10v3-bug2). Upstream has no
remote worker, so there is nothing to converge with.

## Known v1 limitations (deliberate)

- ~~Chat judge only~~ (lifted 2026-08-18): the host provisions `bwrap` and the
  runner passes the judge quartet, so `auto`/`agentic` judge modes work — see
  the launchpad spec (skill-lab-foundation §13). Binary-artifact tasks still
  fail closed when the host lacks the sandbox launcher, upstream's documented
  behavior. The inspector deps (mcp/openpyxl/Pillow/python-docx/
  python-pptx) ARE installed in the venv — `evaluate_skill.py` imports them at
  module load regardless of judge mode.
- Single-account credentials: the exec runner uses the process's default AWS
  credential chain; no assume-role support (`SKILLOPT_AGENTCORE_ROLE_ARN` patch
  reserved as a follow-up).
- Worker image has no ECR scan gate (unlike the agent-container deploy path):
  it is platform-owned bootstrap infrastructure, and a base-image CVE finding
  would otherwise block `make bootstrap`. Revisit if the posture changes.
- The agentic judge on the HOST shells out to `opencode` as well (routed by
  `backend/app/skill_lab/runner.py`); the host must have the CLI on PATH (the
  status endpoint reports `judge_opencode_ready`). OpenCode has no structured-output
  flag: the verdict schema is requested in the prompt and parsed by `verdict.py`.
- `opencode.json` is written into each per-task work_dir (it is the CLI's project
  config) and is therefore part of the rollout manifest diff baseline like task.md.

## Binary task assets

Launchpad task files may use stable binary descriptors in `files` alongside legacy inline
strings. `load_tasks(..., assets_dir=...)` verifies each content-addressed blob and records
private `_asset_files` copy pairs; split serialization removes private fields and reload
reconstructs them. `evaluate_skill.py` and `validate_tasks.py` accept `--assets-dir`, the
training adapter accepts `assets_dir`, and rollout copies each blob to its declared relative
path before Runtime transport. The worker image pins openpyxl 3.1.5, Pillow 11.3.0, and pypdf
6.0.0 so supported XLSX/image/PDF inputs can be inspected in the microVM.

## Taskgen input attachments

`generate_tasks.py` gained `--attachments <manifest.json>` + `--attachment-assets <dir>`
(the manifest names documents; the directory holds their bytes keyed by sha256, both inside
the platform's immutable job snapshot). The manifest is decoded strictly before any model
call — JSON shape, non-empty array, name safety, case-insensitive duplicate names,
digest shape, size shape, byte presence and byte size are all checked — then each
document is copied to `data/<name>` in the generation work dir, which is the only channel
to a worker: `agentcore_runner.run_remote_exec` explicitly refuses `images`/`data_dirs`
because they reference host paths outside `work_dir`.

The prompt gains an "Attached documents" section stating that the same relative path is
what the evaluated agent will see, so a question the model writes ("open `data/rows.csv`")
stays true after import. Task items may carry `attachments: [<name>, …]`; the agent names
documents and never handles digests, because only the platform can mint a verified
descriptor. `validate_generated_tasks` rejects a declaration that is not an array, names a
document that was not attached, differs from the attached name in case, repeats a name, or
re-inlines an attached path through `files` — all as feedback for the existing one-retry
loop rather than a hard failure on the first attempt. `_bounded_task_summary` echoes
declared names so expansion context shows which documents existing tasks already use.

Upstream has no equivalent: it handles binary evaluation through separate envs with their
own `data_root` (`spreadsheetbench`, `officeqa`, `docvqa`) rather than through the skilleval
task schema, so there is nothing to converge with here.
