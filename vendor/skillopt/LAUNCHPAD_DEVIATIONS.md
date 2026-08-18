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

`scripts/{evaluate_skill,train}.py`, `configs/{_base_,skilleval}/`,
`deploy/agentcore/{Dockerfile,codex-config.toml}`, and the `skillopt/` package minus: all benchmark
envs (`envs/*` except `base.py`, `__init__.py`, `skilleval/`), `scheduler/`
(unused placeholder), `engine/plugin_trainer.py`, `evaluation/plugin_gate.py`,
`model/{codex_backend,router}.py` (unreferenced by the vendored entry points).
Not copied: `skillopt_studio/`, `skillopt_webui/`, `skillopt_sleep*/`, `plugins/`,
`tests/`, `ckpt/`, `data/`, docs, `scripts/agentcore/` (replaced by Launchpad
bootstrap).

## Patches

### 1. `skillopt/model/bedrock_chat.py` (new) + dispatch wiring

Bedrock-native chat backend on boto3 `bedrock-runtime` Converse for the
optimizer role (reflection/patch generation and the skilleval chat judge) —
zero-key: default credential chain (instance role), no endpoint/bearer/CLI.
Wired in `model/__init__.py` (imports, four chat dispatch branches, token-summary
merge, reset/effort/deployment fan-outs), `model/backend_config.py` (optimizer +
target allow-lists), `model/common.py` (aliases `bedrock`/`bedrock_chat`, default
model `global.anthropic.claude-opus-5`). All edits carry a `# LAUNCHPAD PATCH`
marker. `tools`/`tool_choice`/`return_message` raise `NotImplementedError`
(no optimizer-role caller in this subset passes them).

### 2. `deploy/agentcore/Dockerfile` rework

Upstream copied host `claude`/`codex` binaries into the image. This variant
installs pinned CLIs inside the image (reproducible, host-independent): claude
via the official installer (`ARG CLAUDE_CLI_VERSION=2.1.234`) and codex via the
official standalone GitHub release binary (`ARG CODEX_CLI_VERSION=0.147.0`,
asset `codex-aarch64-unknown-linux-musl.tar.gz`). The image is built by
Launchpad's CodeBuild ARM64 pipeline from a context assembled in
`backend/app/skill_lab/worker_build.py` (upstream's `build_and_push.sh` and
`scripts/agentcore/setup_infra.py` are not vendored).

`codex-home/` is staged by the assembler, not committed wholesale:
`deploy/agentcore/codex-config.toml` (this tree, Launchpad variant — same
amazon-bedrock provider + `openai.gpt-5.6-sol` default as upstream's) plus
`model-catalogs/bedrock-models.json` read from the backend host's
`~/.codex/model-catalogs/` at context-assembly time, with upstream's `{}`
fallback when absent. The catalog is deliberately never committed: it embeds
OpenAI-proprietary `base_instructions` prompt text, which must not enter this
public repository. Both blobs are part of the content-addressed image tag, so a
host catalog change rolls the worker.

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

## Known v1 limitations (deliberate)

- Chat judge only: no `bwrap` sandbox is provisioned, so `judge_mode` must stay
  `chat`; binary-artifact tasks fail closed (`score_valid=False`), upstream's
  documented behavior. The inspector deps (mcp/openpyxl/Pillow/python-docx/
  python-pptx) ARE installed in the venv — `evaluate_skill.py` imports them at
  module load regardless of judge mode.
- Single-account credentials: the exec runner uses the process's default AWS
  credential chain; no assume-role support (`SKILLOPT_AGENTCORE_ROLE_ARN` patch
  reserved as a follow-up).
- Worker image has no ECR scan gate (unlike the agent-container deploy path):
  it is platform-owned bootstrap infrastructure, and a base-image CVE finding
  would otherwise block `make bootstrap`. Revisit if the posture changes.
- Runtime env deliberately omits upstream's `ANTHROPIC_SMALL_FAST_MODEL` pin:
  the claude CLI version is pinned in the image, so its default small-fast
  model is stable, and the live smoke passes without the extra pin.
