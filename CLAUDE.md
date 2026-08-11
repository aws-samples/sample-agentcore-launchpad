# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

AgentCore Launchpad is a production-grade sample asset: one React console over one
FastAPI backend that wires the real Amazon Bedrock AgentCore services (Runtime,
Harness, Memory, Gateway, Identity, Registry, Policy/Cedar, Evaluation, Observability)
into a unified **create → deploy → invoke → observe** experience. Everything targets a
real AWS account in `us-west-2`; there is no mock plane. Read
[docs/architecture.md](docs/architecture.md) first — it is the authoritative,
up-to-date map of how each console feature backs onto an AgentCore service and resource.

## Commands

All Python is managed by **uv** — run backend/infra commands from their own directory
with `uv run`, never bare `python`/`pip`.

**Before starting/stopping/updating the running stack**, read the agent runbooks:
[docs/agent-runbook-dev.md](docs/agent-runbook-dev.md) (local dev) /
[docs/agent-runbook-prod.md](docs/agent-runbook-prod.md) (prod, systemd) — they carry
the precondition probes and restart side-effect traps the table below does not.

| Task | Command |
|---|---|
| Full verify gate (**run before reporting done**) | `make verify` |
| Run local stack (backend :8000, frontend :5173, studio :8100/:5273) | `make dev` |
| One-time infra + AgentCore bootstrap (idempotent) | `make bootstrap` |
| Backend only / frontend only | `make backend` / `make frontend` |
| Backend lint + tests | `cd backend && uv run ruff check . && uv run pytest -q` |
| Single backend test | `cd backend && uv run pytest tests/test_agents_api.py::test_name -q` |
| Frontend lint / typecheck / build | `cd frontend && npm run lint && npx tsc --noEmit && npm run build` |
| i18n key parity (en ↔ zh-CN) | `python3 scripts/i18n_check.py` |

`scripts/verify.sh` (= `make verify`) is the canonical gate: backend ruff+pytest, infra
ruff+pytest, frontend eslint+tsc+vite-build, and i18n parity. It must pass before any
change is considered complete.

**`backend/tests/` vs `backend/scripts/e2e_*.py`:** `tests/` are hermetic unit tests
(SQLite is redirected to a temp DB in `conftest.py`; AWS is stubbed) and run in
`make verify`. The `e2e_*.py` scripts hit **real AWS** and require `make bootstrap` +
credentials — they are not part of the verify gate.

## Repo layout

`backend/` FastAPI control plane · `frontend/` React (Vite) console · `infra/` CDK app
(`launchpad-base` stack) · `apps/studio/` vendored Strands Studio sub-app (方式C) ·
`scripts/` bootstrap/teardown/dev/verify · `config/` generated `launchpad.yaml` ·
`docs/` architecture/api/setup/troubleshooting (all bilingual). See the table in
[README.md](README.md#repo-layout).

## Architecture — the load-bearing patterns

These are the abstractions that span many files; understanding them is what makes you
productive here. The per-feature detail lives in `docs/architecture.md` and the specs
under `.trellis/spec/launchpad/`.

- **Three creation methods, one pipeline.** 方式A (Claude Agent SDK → ARM64 container),
  方式B (managed Harness, no build), 方式C (Strands Studio canvas), plus `zip_runtime`,
  all converge into the ordered stages `generate → package → provision → deploy →
  register` in `backend/app/deployer/pipeline.py`. Each method registers one callable
  per stage (or omits it) via `register_method()`; the method modules
  (`deployer/harness.py`, `zip_runtime.py`, `container.py`) are imported **for their
  side effects** in `app/main.py`, so a new method must be imported there to exist.

- **Deploy is an async, resumable job.** `POST /api/agents` returns `202` with a
  `job_id`; the job runs on a background thread, persisting per-stage status onto the
  `Deployment` row and JSONL events onto `Job.log`. `resume_pending_jobs()` runs on
  startup and re-runs interrupted jobs from the first non-succeeded stage — so stages
  should be idempotent.

- **One invoke chain for both entrances.** The Chat console (`/api/chat/{id}`) and the
  public `/v1` API share the single entry point `app.services.invoke.invoke_agent_text`
  (+ `app.services.chat.chat_stream` for SSE). `/v1` only adds `X-Api-Key` auth
  (sha256-hashed); everything downstream of method dispatch is identical. Change invoke
  behavior in one place, not per-router.

- **AWS is the source of truth; the SQLite ledger holds only identifiers + derived
  progress.** The ledger (`data/launchpad.db`, models in `app/models/ledger.py` plus the
  evaluation/optimization models) stores agents, deployments, jobs, chat sessions, api
  keys, policy decisions, eval datasets/runs, and experiments. Authoritative resource
  state (runtime status, registry record status, traces, eval results) is always read
  back from AWS.

- **All boto3 clients are built in exactly one place.** `app/services/agentcore/client.py`
  is the only module that constructs AgentCore clients; preview-API drift is contained
  there and in the sibling wrapper modules (`runtime.py`, `harness.py`, `registry.py`,
  `codebuild.py`). Everything else receives clients explicitly so tests can inject stubs
  — follow this; do not call `boto3.client(...)` elsewhere.

- **Config precedence:** defaults < `config/launchpad.yaml` < `LAUNCHPAD_`-prefixed env
  < init kwargs (`app/core/config.py`). `launchpad.yaml` is written by `make bootstrap`
  and is gitignored; before bootstrap the defaults keep the app importable/testable, but
  real AWS calls will fail.

- **Per-agent memory scoping.** One shared `launchpad_memory` singleton; the AgentCore
  namespace is keyed only on `{actorId}`, so the platform folds the agent id into the
  actor (`scoped_actor(agent_id, human)` → `<agent>__<human>`) to partition both
  short-term events and long-term facts per agent. The ledger still stores the bare
  human actor for display.

## Frontend conventions

React + Vite + `react-router-dom`, TypeScript strict. Top-level routes are in
`src/App.tsx` (Overview, Create, Registry, Knowledge Bases, Chat, Observability,
Evaluation, Governance). Complex pages expose **sub-pages via a `?view=` query param**
(e.g. Evaluation's `?view=experiment|evaluators|datasets`, Registry's register/edit)
rather than nested routes — follow that pattern for new sub-surfaces. `src/lib/api.ts`
is the single typed client for the backend; keep its interfaces in sync with the FastAPI
schemas. All user-facing strings are i18n keys with **en + zh-CN parity enforced** by
`scripts/i18n_check.py`.

## Conventions & gotchas

- **All documentation is written in English** (per the launchpad spec index), even
  though the product UI and top-level docs are bilingual.
- Python: ruff (line length 100, target py312, rules `E,F,I,W,UP,B`); FastAPI routers
  live in `app/routers/` (console `/api`) and `app/routers/public_api.py` (public `/v1`);
  errors go through `app/core/errors.register_error_handlers`.
- `bedrock-agentcore` is a **preview SDK pinned to `1.17.*`** — treat API shapes as
  volatile and keep the volatility inside the `agentcore/` wrappers.
- `apps/studio/`, `vendor-src/*`, and `backend/samples/frontdesk_agent` are vendored /
  demo assets with their **own `CLAUDE.md` and conventions** — don't apply this file's
  rules to them blindly, and prefer editing the platform-side integration over the
  vendored code.
