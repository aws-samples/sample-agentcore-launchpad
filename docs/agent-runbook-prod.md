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

```bash
# start/stop/status
sudo systemctl restart launchpad-backend launchpad-frontend
systemctl is-active launchpad-backend launchpad-frontend
sudo journalctl -u launchpad-backend --since "2 min ago" -q   # read after EVERY restart:
                                                              # ledger schema drift = startup RuntimeError here
```

### Update recipe (verified sequence)

```bash
cd /home/ubuntu/workspace/agentcore_launchpad
cp -a data/launchpad.db data/launchpad.db.bak-$(date +%Y%m%d-%H%M%S)   # ALWAYS first
git fetch origin main && git diff --name-only HEAD..origin/main        # scope the delta
git merge --ff-only origin/main
# only if the delta touched **/pyproject.toml or uv.lock:      cd backend && uv sync
# only if the delta touched package*.json:                     cd frontend && npm ci
# only if the delta touched frontend/:                         cd frontend && npm run build
sudo systemctl restart launchpad-backend   # + launchpad-frontend if rebuilt
```

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
