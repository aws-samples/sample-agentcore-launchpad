# Remote Production Deployment

## 1. Scope / Trigger

Use this contract when deploying AgentCore Launchpad to the workshop EC2 host,
rebuilding the `us-east-1` environment, changing the CloudFront origin, or
debugging a deployment that reports resources from the wrong AWS Region.

The current production-like workshop deployment is:

```text
AWS account:       434444145045
AWS Region:        us-east-1
EC2 instance:      i-040893f6e82e60bc7
EC2 public IP:     54.221.233.74
SSH key:           ~/workspace/4344-us-east-1.pem
Remote repository: /home/ubuntu/workspace/agentcore_launchpad
CloudFront:        https://dh5fx2s7uotew.cloudfront.net
Distribution ID:  E3FSZ11DEKP19N
```

This is a fresh `us-east-1` control plane. Do not migrate or reuse
`us-west-2` AgentCore identifiers. Seed examples through the Launchpad APIs so
that AWS resources and the SQLite ledger are created together.

## 2. Signatures

Connect and prepare the repository:

```bash
ssh -i ~/workspace/4344-us-east-1.pem ubuntu@54.221.233.74
cd /home/ubuntu/workspace
git clone https://github.com/xiehust/agentcore_launchpad.git
cd agentcore_launchpad
export PATH="$HOME/.local/bin:$PATH"
export AWS_REGION=us-east-1
export AWS_DEFAULT_REGION=us-east-1
export LAUNCHPAD_REGION=us-east-1
```

Install/build/bootstrap using repository tooling:

```bash
cd backend && uv sync && cd ..
cd infra && uv sync && cd ..
cd frontend && npm ci && npm run build && cd ..
make bootstrap
bash scripts/setup_exec_env.sh
```

Run `make verify` in a shell **without** the `LAUNCHPAD_*` exports above:

```bash
env -u LAUNCHPAD_REGION make verify        # or a fresh shell with only PATH set
```

Config precedence is defaults < `config/launchpad.yaml` < `LAUNCHPAD_`-prefixed
env < kwargs, so an exported `LAUNCHPAD_REGION` beats the YAML and
`tests/test_config.py::test_yaml_source_feeds_settings` fails (it asserts the
YAML source wins). `tests/test_a2a_demo.py` fails for the same reason. Both pass
in a clean shell — do not chase them as real regressions (live-hit 2026-07-28).
The `LAUNCHPAD_REGION` the *service* gets from its systemd drop-in is correct and
unrelated; only the test shell must be clean.

The execution-environment script must finish with:

```text
studio_exec_python =
  /home/ubuntu/workspace/agentcore_launchpad/data/exec-venv/bin/python
```

Production processes:

```text
launchpad-backend.service
  WorkingDirectory=/home/ubuntu/workspace/agentcore_launchpad/backend
  ExecStart=uv run uvicorn app.main:app --host 127.0.0.1 --port 8000

launchpad-frontend.service
  WorkingDirectory=/home/ubuntu/workspace/agentcore_launchpad/frontend
  ExecStart=npm run preview -- --host 127.0.0.1 --port 5173 --strictPort

nginx
  listens on EC2 port 80
  /api/* and /v1/* -> 127.0.0.1:8000
  /assets/* and all other paths -> 127.0.0.1:5173
```

Manage services non-interactively:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now launchpad-backend launchpad-frontend nginx
sudo systemctl restart launchpad-backend launchpad-frontend nginx
systemctl is-active launchpad-backend launchpad-frontend nginx
```

## 3. Contracts

### Region contract

All three variables must resolve to `us-east-1` in the backend process:

```text
AWS_REGION=us-east-1
AWS_DEFAULT_REGION=us-east-1
LAUNCHPAD_REGION=us-east-1
```

The generated `config/launchpad.yaml` must also contain:

```yaml
region: us-east-1
```

The base backend unit currently contains legacy `us-west-2` values, but the
drop-in below has higher systemd precedence:

```text
/etc/systemd/system/launchpad-backend.service.d/region.conf
```

Never infer the effective Region from the first `Environment=` line in
`systemctl cat`. Verify the merged process environment and health response:

```bash
systemctl show launchpad-backend --property=Environment
curl -fsS http://127.0.0.1:8000/api/health
# {"status":"ok",...,"region":"us-east-1"}
```

### Network and CloudFront contract

- Backend and frontend bind only to loopback.
- EC2 security group `sg-04398d8676cd97ee3` permits:
  - TCP 22 from `0.0.0.0/0` for SSH.
  - TCP 80 only from CloudFront managed prefix list `pl-3b927c52`.
- Do not permit public `0.0.0.0/0` access to ports 80, 443, 5173, or 8000.
- CloudFront origin is
  `ec2-54-221-233-74.compute-1.amazonaws.com`, HTTP port 80.
- CloudFront redirects viewers to HTTPS and supports HTTP/2 and HTTP/3.
- CloudFront sends `X-Launchpad-Origin-Key`; nginx returns `403` when this
  header is missing or incorrect.
- The origin-key value is a secret. Store it outside Git and never put it in a
  spec, command transcript, issue, or commit.
- The default CloudFront behavior allows all API methods and uses the managed
  caching-disabled policy. `/assets/*` uses the managed optimized cache policy.

### Authentication contract

The backend service receives:

```text
LAUNCHPAD_AUTH_USERNAME
LAUNCHPAD_AUTH_PASSWORD
LAUNCHPAD_AUTH_COOKIE_SECURE=true
```

Do not persist the password in repository files. Prefer a root-readable
systemd `EnvironmentFile` or another operator-managed secret source. The
CloudFront origin header is defense in depth; it does not replace the
Launchpad application login.

### Data and AWS resource contract

- `data/launchpad.db` is the EC2-local ledger.
- `data/agentcore-cli/node_modules/.bin/agentcore` is a gitignored, host-local
  bootstrap prerequisite. `make bootstrap` installs and verifies exactly
  `@aws/agentcore@0.21.1`; Harness conversion never uses a global CLI from
  `PATH`.
- `config/launchpad.yaml` is generated, Region-specific, and gitignored.
- AWS remains the source of truth for Runtime, Harness, Memory, Gateway,
  Registry, Policy, Evaluation, and Knowledge Base state.
- `make bootstrap` is idempotent and targets the effective Region.
- Do not copy a west-region ledger into the east deployment. Old ARNs and
  resource IDs would make the UI point at the wrong control plane.
- Create seed agents, datasets, Registry records, and KB attachments through
  authenticated `/api/*` endpoints.

## 4. Validation & Error Matrix

| Condition | Expected result / fix |
|---|---|
| `/api/health` reports `us-west-2` | Fix the systemd Region drop-in, run `daemon-reload`, and restart backend |
| `/api/health` hangs and logs show `QueuePool limit of size 5 overflow 10` | File SQLite must use `NullPool`; verify the deployed `app/core/db.py`, restart the backend to release old checked-out connections, then repeat a concurrent authenticated `/api/agents` probe |
| `make verify` fails only `test_yaml_source_feeds_settings` + `test_a2a_demo` | The shell exports `LAUNCHPAD_REGION`, which outranks the YAML. Re-run in a clean shell; both pass |
| An IAM change from a new revision has no effect | `make bootstrap` runs `cdk deploy` only when the stack is MISSING. This host has no CDK CLI, so deploy the east stack from a box that does: `cd infra && AWS_REGION=us-east-1 CDK_DEFAULT_REGION=us-east-1 uv run cdk diff` then `deploy` (same account, region-parameterised stack) |
| UI still displays `us-west-2` after config edit | Backend process is stale; restart it and recheck health |
| Direct EC2 HTTP request lacks origin header | nginx returns `403` |
| CloudFront returns `403` from origin | Compare CloudFront header name/value with nginx without logging the value |
| Browser receives `401` after login | Use application login cookie; remove any obsolete HTTP Basic Auth layer |
| Port 80 is reachable from arbitrary Internet clients | Replace `0.0.0.0/0` with the CloudFront managed prefix list |
| Agent row contains a west-region ARN | Delete/recreate through east APIs; do not patch the ledger ARN |
| `uv` is not found over non-interactive SSH | Prefix PATH with `$HOME/.local/bin` or call `/home/ubuntu/.local/bin/uv` |
| Harness conversion reports `agent.convert_cli_missing` | Run `make bootstrap`, then verify `data/agentcore-cli/node_modules/.bin/agentcore --version` prints `0.21.1` |
| Studio/local execution cannot import Strands tools | Run `bash scripts/setup_exec_env.sh` and verify `data/exec-venv/bin/python` |
| Agent or dataset appears only in SQLite | Recreate through Launchpad API so AWS and ledger state converge |
| Service exits after SSH disconnect | Run it under systemd, not an interactive shell |

## 5. Good / Base / Bad Cases

- Good: bootstrap a fresh east stack, keep loopback application listeners,
  restrict EC2 port 80 to CloudFront, require the origin header, enable Secure
  application cookies, seed through APIs, and validate a real Agent invoke.
- Base: back up `data/launchpad.db`, `git merge --ff-only origin/main`, rerun
  `make bootstrap` to refresh host-local prerequisites, deploy the east CDK
  stack if the revision changed `infra/` (this host has no CDK CLI — deploy from
  one that does, the stack is region-parameterised), rebuild the frontend
  (`vite preview` serves `dist/`, so a pull alone changes nothing the browser
  sees), restart the two application services, then verify health plus
  CloudFront login.
- Bad: expose Vite/Uvicorn publicly, open nginx port 80 globally, copy west
  resource IDs, commit generated config or passwords, or treat a successful
  systemd start as proof that AWS calls work.

## 6. Tests Required

Run these checks after deployment or Region changes:

```bash
# Remote process and local-origin health
systemctl is-active launchpad-backend launchpad-frontend nginx
curl -fsS http://127.0.0.1:8000/api/health

# Public edge
curl -I https://dh5fx2s7uotew.cloudfront.net

# Repository gate
cd /home/ubuntu/workspace/agentcore_launchpad
make verify

# Execution environment
data/exec-venv/bin/python -c \
  'import strands, strands_tools, mcp; print("exec environment imports ok")'

# Harness conversion prerequisite
data/agentcore-cli/node_modules/.bin/agentcore --version
```

Assertion points:

- Health reports `us-east-1`.
- All three services are `active`.
- CloudFront redirects/serves HTTPS and the application login works.
- Direct origin access without `X-Launchpad-Origin-Key` is rejected.
- Agent ARNs and generated resource ARNs contain `us-east-1`.
- At least one deployed agent can be invoked through the Launchpad API.
- Registry auto-registration and any seeded Evaluation/KB rows are visible in
  the console.
- The repository-managed AgentCore CLI reports exactly `0.21.1`.
- Browser console and page error logs are empty after login and navigation.

## 7. Wrong vs Correct

### Wrong

```text
Internet -> EC2:80/5173/8000
copy data/launchpad.db from us-west-2
edit region in YAML only
run uvicorn and vite in an SSH shell
```

This bypasses the edge boundary, preserves stale regional identifiers, leaves
the effective backend Region ambiguous, and makes processes die with the shell.

### Correct

```text
Internet -> CloudFront HTTPS
CloudFront -> EC2:80 (managed prefix list + secret origin header)
nginx -> 127.0.0.1:8000 and 127.0.0.1:5173
fresh us-east-1 bootstrap -> API-created seed records
systemd -> durable backend/frontend processes
```

The edge, process, Region, and data contracts are then independently
verifiable and do not rely on copied west-region state.
