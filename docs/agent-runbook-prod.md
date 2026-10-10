# Agent Runbook — Production-Mode Startup

Audience: an AI agent (or operator) that must start, update, verify, or debug a
Launchpad deployment running in **prod mode**. Two supported shapes: the
built-in launcher (§2) and a systemd deployment (§3 — the reference layout used
by the real us-east-1 box). Paired doc: [agent-runbook-dev.md](agent-runbook-dev.md).

## 0. What prod mode means (`LAUNCHPAD_RUN_MODE=prod`)

- **Auth is mandatory**: `create_app()` refuses to boot with the login gate off
  unless `LAUNCHPAD_ALLOW_OPEN_CONSOLE=true`; every `/api` request from a
  non-loopback peer requires a session. Bare `curl /api/...` answering
  `{"code":"auth.required"}` is the *healthy* state, not an error.
- Frontend runs `vite preview` over the **built** `dist/` — frontend changes
  require `npm run build` + service restart; there is no hot reload.
- Backend runs without `--reload` — backend changes require a service restart.
- Studio local debug is refused (403 `studio.exec.disabled`) unless the
  operator opted in: either `LAUNCHPAD_STUDIO_LOCAL_EXEC_ENABLED=true`
  (subprocess on the host — sharp edge) or `studio_exec_backend: docker`
  (sandboxed; the opt-in built for prod — see §5).

## 1. Preconditions

```bash
test -f config/launchpad.yaml || echo "MISSING: run make bootstrap once (real AWS resources)"
# the login gate needs a credential source, or boot fails with a legible error:
grep -q auth_password config/launchpad.yaml || echo "set auth_password in launchpad.yaml or LAUNCHPAD_AUTH_PASSWORD in the environment"
cd frontend && npm ci --silent && cd ..           # preview needs node_modules + a build (start.py --prod builds for you)
```

## 2. Shape A — built-in launcher (single host, foreground supervisor)

```bash
LAUNCHPAD_AUTH_PASSWORD='<strong password>' python3 start.py --prod
# stop:
python3 start.py --stop
```

`--prod` builds the platform frontend, then runs backend
`uvicorn --host 0.0.0.0 --port 8000` (no reload) + frontend
`npm run preview -- --host 0.0.0.0 --port 5173 --strictPort`, injecting
`LAUNCHPAD_RUN_MODE=prod` into both. Hosts/ports overridable:
`LAUNCHPAD_HOST`, `LAUNCHPAD_API_HOST`, `PLATFORM_API_PORT`,
`PLATFORM_UI_PORT`. It waits on `/api/health` and `/` and prints log tails on
failure.

## 3. Shape B — systemd (reference: the us-east-1 box)

Two units, `launchpad-backend` + `launchpad-frontend` (frontend `Requires=` the
backend). Key facts an agent must know before touching them:

- Backend unit: `User=ubuntu`, `WorkingDirectory=.../backend`,
  `ExecStart=/home/ubuntu/.local/bin/uv run uvicorn app.main:app --host
  127.0.0.1 --port 8000`, env carries `LAUNCHPAD_RUN_MODE=prod`,
  `LAUNCHPAD_AUTH_USERNAME/PASSWORD`, `LAUNCHPAD_AUTH_COOKIE_SECURE=true`, and
  a **region drop-in** overriding the unit's default region (us-east-1 there —
  check with `systemctl show launchpad-backend -p Environment`). Hardening:
  `NoNewPrivileges`, **`PrivateTmp=true`** (consequences in §5),
  `ProtectSystem=full`.
- Frontend unit: `npm run preview -- --host 127.0.0.1 --port 5173 --strictPort`
  over `frontend/dist` — **a rebuild is required for any frontend change**.
- Both bind loopback; CloudFront (or any fronting proxy) terminates TLS.
- **`LAUNCHPAD_PUBLIC_BASE_URL` must name the public origin** the browser
  uses (`https://<public-host>`: scheme + host, no path, no trailing slash).
  It feeds the as_user (3LO) OAuth return URL `{public_base_url}/auth/return`,
  which AgentCore Identity redirects the user's browser to after IdP consent
  and which the deployer allow-lists on each as_user agent's workload
  identity. Unset, it falls back to the dev default `http://localhost:5173`
  and the backend logs a startup warning (`run_mode=prod but the as_user (3LO)
  OAuth return URL is http://localhost:5173/auth/return`). Behind a
  path-rewriting proxy set `LAUNCHPAD_OAUTH_RETURN_URL` (the full return-page
  URL) instead. See "Setting the public base URL" below.

```bash
# start/stop/status
sudo systemctl restart launchpad-backend launchpad-frontend
systemctl is-active launchpad-backend launchpad-frontend
sudo journalctl -u launchpad-backend --since "2 min ago" -q   # read after EVERY restart:
                                                              # ledger schema drift = startup RuntimeError here
```

### Setting the public base URL

Add one `Environment=` line to the backend unit through a drop-in (the unit
file itself stays untouched; this is the line to add):

```bash
sudo mkdir -p /etc/systemd/system/launchpad-backend.service.d
printf '[Service]\nEnvironment=LAUNCHPAD_PUBLIC_BASE_URL=https://<public-host>\n' \
  | sudo tee /etc/systemd/system/launchpad-backend.service.d/public-base-url.conf
sudo systemctl daemon-reload && sudo systemctl restart launchpad-backend
systemctl show launchpad-backend -p Environment | tr ' ' '\n' | grep PUBLIC_BASE_URL
sudo journalctl -u launchpad-backend --since "2 min ago" -q | grep -c "OAuth return URL"   # want 0
```

The return URL is baked into each as_user agent at deploy time (runtime env
`LAUNCHPAD_OAUTH_RETURN_URL` + the workload-identity allow-list), so after
setting or changing it, **re-publish every agent that has an as_user tool**.
The frontend unit needs nothing.

### Update recipe (verified sequence)

```bash
cd /home/ubuntu/workspace/agentcore_launchpad
(cd backend && /home/ubuntu/.local/bin/uv run python scripts/ledger_backup.py); echo "backup exit=$?"   # ALWAYS first; stop unless 0
git fetch origin main && git diff --name-only HEAD..origin/main        # scope the delta
git merge --ff-only origin/main
# only if the delta touched **/pyproject.toml or uv.lock:      cd backend && uv sync
# only if the delta touched package*.json:                     cd frontend && npm ci
# only if the delta touched frontend/:                         cd frontend && npm run build
(cd backend && /home/ubuntu/.local/bin/uv run python scripts/inflight.py); echo "inflight exit=$?"
sudo systemctl restart launchpad-backend   # + launchpad-frontend if rebuilt
```

**The ledger backup (always the first step).** `scripts/ledger_backup.py` copies the
live ledger with SQLite's online backup API, so a request committing during the copy
cannot tear it the way `cp` of the running service's file can (the engine runs SQLite's
default rollback journal). It writes `data/launchpad.db.bak-<UTC YYYYmmdd-HHMMSS>`, runs
`PRAGMA integrity_check` on the copy, and prints its path, size and the `agents` / `jobs`
/ `eval_runs` row counts — eyeball them against what the console shows. It opens the
ledger read-only, makes no AWS call and needs no `sqlite3` CLI; like the probe below it
reads `settings.database_url`, or name the file with `--db data/launchpad.db` (then it is
stdlib-only and plain `python3 backend/scripts/ledger_backup.py --db data/launchpad.db`
works without the venv). It prunes nothing by default. Retention is an optional, explicit
operator step: `--keep N` deletes the oldest `launchpad.db.bak-<stamp>` copies beyond the
newest N (the new one included) — preview it first with `--keep N --dry-run`, which takes
the backup and only lists what would be deleted.
Hand-named copies — `launchpad.db.bak-pre-pr191-*`, `launchpad.db.pre-registry-ga-*` —
never match and are never pruned. Exit `0` = written and verified; `1` = no backup (bad
path/URL, ledger locked past `--timeout`, disk error); `2` = the copy failed
`integrity_check` — it is kept as `….corrupt`, nothing is pruned, and the update must
stop until that is understood; `64` = bad usage. On a checkout that predates the script,
run the fetched copy: `git fetch origin main && git show
origin/main:backend/scripts/ledger_backup.py | python3 - --db data/launchpad.db`.

**The in-flight probe (run right before every restart).** `scripts/inflight.py`
lists every background item in the ledger and what the restart will do to it. It is
read-only — no `create_app`/`init_db`, no row written, no AWS call — needs no
`sqlite3` CLI, and tolerates a checkout that is ahead of the ledger schema (a table
or column the restart has not migrated yet is reported as `note: skipped …`, not an
error). Add `--json` for a machine-readable answer. It reads `settings.database_url`:
if the systemd unit sets `LAUNCHPAD_DATABASE_URL`, pass the same value to the probe
(an ad-hoc shell does not inherit the unit's environment). Branch on the exit code:

| Exit | Meaning | Do |
|---|---|---|
| `0` | nothing in flight, or only `restart_safe` rows | restart |
| `2` | only `resumes` / `reconciles` rows | restart is fine; expect the `resumed …` / `reconciling …` / `reconciled …` lines in the journal and watch the resumed jobs finish |
| `3` | at least one `fails_interrupted`, `fails_on_read`, `cleared_retry` or `unhandled` row | wait and re-run the probe until it drops to `0`/`2`; restart anyway only knowingly, and tell the owners which rows to re-submit or retry |
| `1` | the ledger could not be read (e.g. no file at the configured path) | fix the path; do not read "no output" as "nothing in flight" |
| `64` | bad command-line usage | fix the invocation |

Per outcome: `resumes` — a startup hook re-runs it from its checkpoint (deploy,
workspace bootstrap, preset uninstall, architect CLEAR jobs; evaluation-asset
operations). `reconciles` — settled from AWS, never replayed (eval runs whose batch
already started; Policy changes). `fails_interrupted` — failed at startup (eval runs
with no batch yet, Skill Lab jobs — a `train` job can be resumed on request — and
open architect turns, whose streams die). `fails_on_read` — failed the next time the
row is read, so the journal is silent about it (running data pipelines, provider
recommendation jobs). `cleared_retry` — an experiment/canary `running_action` or an
architect preparation claim is cleared and the user must retry. `restart_safe` — the
work lives on AWS (AgentCore recommendation jobs, refreshed from GetRecommendation).
`unhandled` — a queued/running `Job.type` with no resume starter: the restart strands
it; report it as a bug.

Run `env -u AWS_REGION -u AWS_DEFAULT_REGION -u LAUNCHPAD_REGION make verify`
before restarting. The backend test bootstrap isolates both SQLite and YAML reads
before importing the application; verification must not inherit this host's Region
or Docker execution settings, and never requires changing production configuration.

### Remote-box gotchas (they will bite a naive agent)

- Non-interactive SSH lands in `$HOME`, not the repo — start every remote
  script with `cd .../agentcore_launchpad`; prefer `ssh host 'bash -s' <
  local_script.sh` over inline quoting.
- `uv` is NOT on the non-interactive SSH PATH — use the absolute path
  `/home/ubuntu/.local/bin/uv` in scripts.
- There may be no `aws` CLI on the box — use `uv run python -c` with boto3, or
  run read-only checks from another credentialed machine.
- An ad-hoc `uv run` shell does **not** inherit the systemd env: it reads
  `run_mode=dev` while the service is prod. Confirm posture from
  `systemctl show launchpad-backend -p Environment` or the `auth.required`
  answer — never from an ad-hoc process.
- Never back the ledger up with `cp` while `launchpad-backend` runs — a commit
  landing mid-copy leaves a torn file that nothing checks. Use
  `scripts/ledger_backup.py` (update recipe); `cp` is only safe with the service
  stopped.
- Infra changes need an explicit `cdk deploy` with the region pinned
  (`CDK_DEFAULT_REGION=...`); `make bootstrap` skips CDK on an existing stack
  and `infra/app.py` defaults to us-west-2 when unset.
- A public endpoint takes constant internet scanning — 401 floods in the
  journal from unknown IPs are background noise, not an incident.

## 4. Verify after any start/update

```bash
# service + journal
systemctl is-active launchpad-backend launchpad-frontend
sudo journalctl -u launchpad-backend --since "2 min ago" -q | grep -icE "error|traceback"   # want 0

# auth boundary (both answers are REQUIRED-healthy)
curl -s localhost:8000/api/overview | grep -o auth.required     # unauthenticated → refused
curl -s -o /dev/null -w '%{http_code}\n' https://<public-host>/api/execute -X POST \
  -H 'Content-Type: application/json' -d '{"code":"print(1)"}'  # 401 through the proxy

# authenticated smoke (credentials from the unit env / operator, NOT from docs)
JAR=$(mktemp)
curl -s -c "$JAR" -X POST localhost:8000/api/auth/login -H 'Content-Type: application/json' \
  -d '{"username":"<user>","password":"<password>"}'
curl -s -b "$JAR" localhost:8000/api/overview | head -c 120; rm -f "$JAR"
```

## 5. Studio local debug in prod — the Docker sandbox

The supported way to offer local debug in prod. State on the reference box
(since 2026-08-11): image `launchpad-studio-exec:latest` built on-box, and in
`config/launchpad.yaml`:

```yaml
studio_exec_backend: docker
studio_exec_docker_network: launchpad-exec
studio_exec_forward_aws_credentials: false
```

Selecting the docker backend **is** the prod opt-in (no
`LAUNCHPAD_STUDIO_LOCAL_EXEC_ENABLED` needed; an explicit `false` still
disables). The hardened posture (`--harden-net` network + `DOCKER-USER` IMDS
rule) is mandatory wherever the instance role is powerful: with IMDS hop limit
2, an unhardened container can use that role. Consequence: local-debug / AI-Fix
requests need an explicit `bedrock_api_key` / `openai_api_key`.

Provisioning from scratch on a new prod box:

```bash
sudo apt-get install -y docker.io && sudo systemctl enable --now docker
sudo usermod -aG docker <service-user>          # takes effect on service restart
sudo bash scripts/setup_exec_docker.sh          # build image
sudo bash scripts/setup_exec_docker.sh --harden-net
# make the IMDS rule reboot-persistent (raw iptables rules are not):
# install a oneshot unit After=docker.service that re-adds the DOCKER-USER rule
# (reference: launchpad-exec-imds-block.service on the us-east-1 box), then:
sudo systemctl enable --now launchpad-exec-imds-block.service
```

Verification (authenticated, via §4's cookie jar):

```bash
curl -s -b "$JAR" -X POST localhost:8000/api/execute -H 'Content-Type: application/json' \
  -d '{"code":"print(\"prod-sandbox-ok\")"}'                    # success:true
# credential isolation — MUST print boto3-creds NONE + imds BLOCKED:
# (payload: boto3.Session().get_credentials() + urllib PUT to 169.254.169.254)
```

Known trap (fixed in `6b5f632`, keep in mind for regressions): the backend unit
has `PrivateTmp=true`, and docker resolves bind-mount sources in the **root**
mount namespace — a workdir under the service's private `/tmp` mounts empty
("can't open file '/work/generated_agent.py'"). Docker-backend workdirs
therefore live under `data/exec-runs/`; any "file not found in /work" symptom
means a workdir escaped that base.

## 6. Escape hatches (deliberate risk acceptance only)

| Variable | Effect |
|---|---|
| `LAUNCHPAD_ALLOW_OPEN_CONSOLE=true` | Unauthenticated console on a reachable interface |
| `LAUNCHPAD_STUDIO_LOCAL_EXEC_ENABLED=true` | Caller-supplied Python as a host subprocess in prod |
| `LAUNCHPAD_AUTH_COOKIE_SECURE=false` | Only when TLS is genuinely not terminated in front |

Never write real credentials into docs, commits, or logs; they live in the
systemd unit environment / operator secret storage.

## 7. Workspaces in prod — operational notes

The workspace model ([architecture.md](architecture.md#workspaces--multi-accountmulti-region-environments),
[cross-account-workspaces.md](cross-account-workspaces.md)) applies to a prod
box the same way as dev, with these prod-specific readings:

- **This box's `default` workspace is its own region** (us-east-1 on the
  reference box) — each deployment is an independent hub with its own ledger
  and workspaces. Do not register another Launchpad deployment's home region
  as a workspace here; the bootstrap's validate-access stage refuses regions
  that already host foreign Launchpad resources.
- **A service restart resumes interrupted `bootstrap_workspace` jobs**
  (real AWS provisioning in the target account/region) in addition to deploy
  jobs — the in-flight probe (`scripts/inflight.py`, §3 update recipe) lists
  them as `resumes`; expect the resumed job to continue after the restart.
- **Startup refuses to boot on any NULL `workspace_id` ledger row** (the
  journal names the table). That indicates a write-path bug introduced by an
  update; roll back the update rather than hand-patching rows.
- **Member sessions are workspace-gated**: a member with no grants sees an
  empty console by design; grants are edited on the Users page or at
  registration approval. Admin accounts bypass grants.
- **Cross-account access is revoked in the SPOKE account** — delete its
  `launchpad-workspace-role` CloudFormation stack; nothing on this box needs
  to change. The workspace row then fails with an actionable 502
  (`workspace.assume_role_failed`) until detached.
