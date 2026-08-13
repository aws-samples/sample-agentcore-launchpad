# Agent Runbook — Local Dev Startup

Audience: an AI agent (or operator) that must bring up, verify, and tear down
the Launchpad stack in **dev mode** on a workstation/EC2 dev box. Every step is
a runnable command; run them from the repo root unless noted. Paired doc:
[agent-runbook-prod.md](agent-runbook-prod.md).

## 0. What dev mode means

- Backend `uvicorn --reload` on `:8000`, frontend `vite dev` on `:5173`,
  both bound `0.0.0.0` (via `make dev`) — code edits hot-reload.
- `LAUNCHPAD_RUN_MODE` is unset/`dev` → login gate is **off** locally
  (`/api/auth/status` returns `auth_required: false`); Studio local debug is
  served without opt-ins.
- AWS side is REAL (`us-west-2` by default): anything that deploys/invokes hits
  the account in `config/launchpad.yaml`. There is no mock plane.

## 1. Preconditions — check before starting

```bash
# toolchain
uv --version && node --version && docker --version   # docker only if using the sandbox backend

# bootstrap artifacts (gitignored — absent on a fresh clone/worktree)
test -f config/launchpad.yaml && echo config-ok || echo "MISSING: run 'make bootstrap' (one-time, creates real AWS resources)"
test -x data/exec-venv/bin/python && echo exec-venv-ok || echo "MISSING: bash scripts/setup_exec_env.sh (needed for Studio local debug on the subprocess backend)"
test -d data/agentcore-cli && echo cli-ok || echo "MISSING: npm install --prefix data/agentcore-cli --no-save --package-lock=false @aws/agentcore@0.21.1 (needed for harness→zip conversion)"

# ports free? (something else may already hold them — see §5)
ss -tlnp | grep -E ':8000|:5173' || echo ports-free
```

Do NOT run `make bootstrap` casually: it is idempotent but talks to real AWS
(CDK, Cognito, gateways). Run it only when `config/launchpad.yaml` is missing.

## 2. Start

```bash
make dev          # foreground; Ctrl-C stops both services
```

What it does (`scripts/dev.sh`): backend `uv run uvicorn app.main:app --reload
--host 0.0.0.0 --port $PLATFORM_API_PORT` + frontend `npm run dev -- --host
0.0.0.0 --port $PLATFORM_UI_PORT`. Override ports with `PLATFORM_API_PORT` /
`PLATFORM_UI_PORT` env vars.

Agent-friendly variants:

```bash
# background, single service each (when you only need one side)
make backend      # backend only, 127.0.0.1 semantics differ: binds default host, --reload
make frontend     # frontend only

# throwaway parallel stack that does NOT disturb a user-owned running stack:
cd backend && uv run uvicorn app.main:app --port 8011 &          # no --reload
cd frontend && LAUNCHPAD_API=http://localhost:8011 npx vite --port 5199 &
# kill both afterwards: pkill -f "port 8011"; pkill -f "port 5199"
```

`vite.config.ts` honors `LAUNCHPAD_API`, so a second frontend can point at a
second backend. Backend restarts re-run `resume_pending_jobs()` (real AWS side
effects if interrupted deploy jobs exist in the ledger) — prefer the throwaway
stack over restarting a stack you did not start.

## 3. Verify it is up

```bash
curl -s localhost:8000/api/health                          # {"status":"ok"} shape
curl -s localhost:8000/api/auth/status                     # expect "auth_required":false in dev
curl -s -o /dev/null -w '%{http_code}\n' localhost:5173/   # 200
```

Port-drift trap: vite auto-shifts to **5174** if 5173 is held by an unrelated
server. Before citing browser evidence, confirm the app behind the port:
`curl -s localhost:5173/ | grep -o '<title>[^<]*'` — the wrong app may answer.

## 4. Studio local debug backends (optional)

Default backend is `subprocess` (needs `data/exec-venv`, §1). To use the Docker
sandbox instead:

```bash
bash scripts/setup_exec_docker.sh              # builds launchpad-studio-exec:latest (~34s, 387MB)
export LAUNCHPAD_STUDIO_EXEC_BACKEND=docker    # set before starting the backend
```

- If the image build 403s on `public.ecr.aws`: `docker logout public.ecr.aws`
  (stale ECR-public token) and retry.
- Credential-less posture additionally needs
  `sudo bash scripts/setup_exec_docker.sh --harden-net` + the yaml settings it
  prints. On these EC2 boxes the IMDS hop limit is 2, so without the hardened
  network a container CAN use the instance role — acceptable on a private dev
  box, and it is what makes keyless Bedrock Mantle debugging work.
- Smoke test: `cd backend && uv run python scripts/e2e_docker_exec.py`
  (add `--no-aws` to skip the real Bedrock call).

## 5. Common failures

| Symptom | Cause → fix |
|---|---|
| Backend import errors / AWS calls fail before bootstrap | Defaults keep the app importable, but real AWS calls need `config/launchpad.yaml` → `make bootstrap` once |
| `:8000` already in use | A stack is already running (`ps -o lstart=,cmd= -p $(pgrep -f 'uvicorn app.main:app')`). Do not kill a user-owned stack; use the throwaway-stack recipe in §2 |
| Studio debug 503 `interpreter_unavailable` | `bash scripts/setup_exec_env.sh` |
| Studio debug 503 `docker_unavailable` | daemon/permission/image — the message names the fix; image build via §4 |
| harness→zip conversion 502 "managed AgentCore CLI missing" | reinstall `data/agentcore-cli` (§1 command); version must stay 0.21.1 |
| Tests unexpectedly hit real AWS | `backend/tests/conftest.py` redirects SQLite but does **not** stub boto3 on a credentialed box — monkeypatch the service wrapper in any test touching an AWS branch |

## 6. Quality gate & teardown

```bash
make verify       # canonical gate: backend ruff+pytest, infra ruff+pytest, frontend eslint+tsc+build, i18n parity
```

Foreground `make dev`: Ctrl-C (its trap kills both children). Background
one-offs: kill the specific pids/ports you started — never `pkill -f uvicorn`
broadly on a shared box.

## 7. Workspaces — what an operating agent must know

Since 2026-08-13 the console manages multiple `(account, region)` environments
("workspaces"); the original us-west-2 environment is the reserved `default`
workspace. Full feature docs: [architecture.md](architecture.md#workspaces--multi-accountmulti-region-environments),
[cross-account-workspaces.md](cross-account-workspaces.md).

**Every `/api` probe is workspace-scoped.** curl without a header resolves to
`default` (admin / open console), so §3's smoke commands keep working — but a
resource in another workspace answers **404, not 403**, from `default`'s view.
When probing a non-default workspace, name it:

```bash
curl -s localhost:8000/api/agents -H 'X-Workspace: <id>'
# jobs are scoped too — polling a bootstrap job REQUIRES the target workspace:
curl -s localhost:8000/api/jobs/<job_id> -H 'X-Workspace: <id>'
```

**Restart side effects grew.** `resume_pending_jobs()` now also resumes
interrupted `bootstrap_workspace` jobs (real AWS provisioning continues in the
target account/region). And startup **refuses to boot** if any scoped ledger
row has a NULL `workspace_id` — the error names the table; that is a write-path
bug to fix, not a row to hand-patch into `default`.

**The `default` row mirrors `config/launchpad.yaml` on every startup**; all
other workspaces are ledger-authoritative and provisioned by the ten-stage
bootstrap job (resumable: a failed run re-POSTs and skips succeeded stages).
Mutating calls against a non-`ready` workspace 409 by design; reads work.

**Never register `(434444145045, us-east-1)`** — that region hosts the
independent prod deployment; the bootstrap's validate-access stage refuses
regions with foreign Launchpad resources, so it fails fast, but don't try.

**Demo workspaces kept on this box** (beyond `default`): `lab-use2`
(same-account us-east-2) and `spoke-use1` (cross-account 936038267572 through
`LaunchpadWorkspaceRole`; deleting that account's `launchpad-workspace-role`
CFN stack revokes hub access entirely). A workspace with ANY rows (even one
failed job) cannot be detached — known limitation, purge decision pending.
