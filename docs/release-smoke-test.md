# Release smoke test

A fixed procedure for proving a deployed Launchpad release actually works against real
Amazon Bedrock AgentCore. Run it **on every significant version change** — after the deploy,
before you tell anyone the release is good.

It is a *smoke* test: it proves each surface is wired end to end on real AWS. It is not a
regression suite (`make verify` is), and passing it does not mean nothing broke.

## What this is not

- **Not hermetic.** Every step creates real AgentCore resources and is **billable**:
  runtimes, memory events, gateways, knowledge bases, evaluation batches, CodeBuild runs.
- **Not run in an empty account.** The prod deployment carries demo/lab resources
  (≈20 agents, several datasets, past evaluation runs). Read the inventory before and after
  so you can tell *your* rows from the furniture.
- **Not idempotent by magic.** Scripts clean up after themselves *when they finish*. A script
  killed midway leaves its agent behind — see [Teardown](#teardown).
- **Not a UI test.** It drives the HTTP API and the service layer. The console rendering is
  only spot-checked (step 1).

## Environments

| | dev box | **prod box** |
|---|---|---|
| Instance | `i-0785d8d0b8b950448` | `i-040893f6e82e60bc7` (`agentops_launchpad`) |
| Region | us-west-2 | **us-east-1** |
| Mode | `make dev`, login gate off | systemd, `LAUNCHPAD_RUN_MODE=prod` |
| Entry | `localhost:5173` / `:8000` | https://dh5fx2s7uotew.cloudfront.net |
| Repo | `/home/ubuntu/workspace/agentcore_launchpad` | same path |

Both live in AWS account `434444145045`. They have separate bootstraps (own gateway, memory,
registry, ledger), so same-named resources do **not** collide — but **account-level quotas
are shared**, which matters for evaluation (see [Collisions](#collisions-check-before-you-start)).

This runbook targets the **prod box**. Everything below assumes you are `ssh`'d into it.

## Reaching the box

```bash
ssh -i ~/workspace/4344-us-east-1.pem ubuntu@54.221.233.74
```

Four traps, all previously paid for:

1. **Non-interactive SSH starts in `$HOME`, not the repo.** Every remote command needs
   `cd /home/ubuntu/workspace/agentcore_launchpad` first. For anything multi-line, don't
   fight nested quoting — write a local script and pipe it:
   `ssh … 'bash -s' < local_script.sh` with the `cd` as its first line.
2. **`uv` is not on the non-interactive PATH.** Use `/home/ubuntu/.local/bin/uv`.
3. **There is no `aws` CLI on this box at all.** Use boto3 via `uv run`, or run read-only AWS
   checks from the dev box (same account).
4. **`sqlite3` is not installed either.** The inventory probe below uses Python's `sqlite3`
   module instead.

## ⚠️ These scripts mutate real AWS the moment they start

Every `e2e_*.py` runs against a live deployment as soon as it is invoked. There is no
dry-run mode and no confirmation prompt. **A script with no arguments runs against its
default target.**

Since 2026-08-04 every script parses its arguments through `argparse`, so `--help` is a safe
way to read its flags. That was **not** true before, and the fix exists because of a real
incident worth keeping in mind here:

> `uv run python scripts/e2e_experiment.py --help` was run on the dev box to check its flags
> (`e2e_experiment.py` no longer exists — it tested the optimization/experiment feature
> removed for the CN build; the lesson below still applies to every remaining script).
> The script had no `argparse`, so `--help` was ignored and it **resumed a live experiment and
> drove it through to `promote`** — stopping the A/B test, applying the treatment system prompt
> plus two tool-description overrides to the production agent, and deploying a new runtime
> version. Piping to `head` did not stop it either: Python block-buffers stdout to a pipe, so
> the script never hit `EPIPE` and ran to completion long after `head` exited.

Two habits that follow from it:

- To learn what a script does, **read its docstring** (`head -20 scripts/e2e_x.py`). `--help`
  is safe now, but the docstring is where the flow, the resources, and the cleanup behaviour
  are actually described.
- **Never assume a pipe or a short timeout will abort one of these.** If you need to stop a
  run, kill the process — and then check the ledger for what it left behind.

## Preflight

### 1. Version gate

```bash
cd /home/ubuntu/workspace/agentcore_launchpad
git describe --tags          # must be the release you think you are testing
systemctl is-active launchpad-backend launchpad-frontend    # active / active
sudo journalctl -u launchpad-backend --since "-5m" --no-pager | tail -20
```

A ledger-schema drift shows up as a startup `RuntimeError` in that journal — read it after
every restart. Also confirm the console itself answers:

```bash
curl -s -o /dev/null -w '%{http_code}\n' https://dh5fx2s7uotew.cloudfront.net/   # 200
curl -s https://dh5fx2s7uotew.cloudfront.net/api/overview                        # auth.required
```

`auth.required` on a bare call is **correct** in prod mode — it is the auth gate working.

### 2. Credentials for the e2e scripts

The scripts authenticate through `backend/scripts/_e2e_client.py`, which needs the console
admin credentials in the environment. They live in the systemd unit, not in any shell
profile:

```bash
systemctl show launchpad-backend -p Environment | tr ' ' '\n' | grep LAUNCHPAD_AUTH
export LAUNCHPAD_E2E_USERNAME=admin
read -rs LAUNCHPAD_E2E_PASSWORD && export LAUNCHPAD_E2E_PASSWORD   # paste, no echo
```

If the deployment needs auth and these are unset, every script exits immediately with an
actionable message **before creating anything** — that is by design.

> **Use `http://127.0.0.1:8000` as the base, not the CloudFront URL.** The session cookie is
> `Secure` in prod, and an RFC 6265 client will not send it over plain HTTP — the helper works
> around that by pinning the token as a header, so the loopback base is fine and it avoids
> CloudFront's 30 s origin-response timeout on slow invokes. Every HTTP script accepts
> `--base`; five of them default to this URL rather than requiring it.

### 3. Inventory snapshot (the "before" state)

Read-only, no auth, straight off the ledger. Save the output — you diff against it at the end.

```bash
cd /home/ubuntu/workspace/agentcore_launchpad/backend
/home/ubuntu/.local/bin/uv run python - <<'PY'
import sqlite3
c = sqlite3.connect("/home/ubuntu/workspace/agentcore_launchpad/data/launchpad.db")
for label, sql in [
    ("agents",        "select name, method, status from agents order by name"),
    ("chat_sessions", "select count(*) from chat_sessions"),
]:
    print(f"{label}: {c.execute(sql).fetchall()}")
PY
```

**The ledger is not the whole picture.** Registry records and knowledge bases live in AWS, not
in SQLite — there is no `registry_records` table. A ledger-only diff therefore cannot see the
records step 4 creates or the KB step 7 leaves behind, which is exactly how a leak hides behind
a clean-looking diff. Snapshot those two over the API as well (authenticated):

```bash
curl -s $BASE/api/registry/records | python3 -c "import json,sys; r=json.load(sys.stdin)['records']; print('registry_records:', len(r)); print(sorted(x['name'] for x in r))"
curl -s $BASE/api/knowledge-bases   | python3 -c "import json,sys; print('kbs:', [(k['kb_id'], k['name'], k['status']) for k in json.load(sys.stdin)['items']])"
```

### 4. Collisions: check before you start

These are invisible from the code and each one can waste a whole run.

| Check | Why | How |
|---|---|---|
| `hr-assistant` exists and is `active` | `e2e_chat_memory.py`, `e2e_observability.py`, `e2e_traces.py` all chat with this persistent agent | `agents` line of the inventory |

**Removed for the CN build:** the evaluation-batch-capacity, running-experiment and
A/B-gateway-busy checks that used to live here applied to the Evaluation and
Optimization/Experiment features deleted in Tasks 3/5 of the CN foundation node; there
is nothing to check for them anymore.

## Core tier (~30–40 min)

Run in this order — later steps consume what earlier ones produce. Stop at the first
failure; a smoke test that keeps going after a break produces a misleadingly green tail.

```bash
cd /home/ubuntu/workspace/agentcore_launchpad/backend
BASE=http://127.0.0.1:8000
```

| # | Step | Command | Expected signal | Time |
|---|---|---|---|---|
| 1 | Service map | `curl -s $BASE/api/overview` (authenticated) + open the console in a browser | every AgentCore service green; console renders past login | 2 min |
| 3 | Zip runtime deploy | `uv run python scripts/e2e_zip_runtime.py --base $BASE` | pip/zip/S3/create/READY, calculator-tool answer, agent deleted | 5–8 min |
| 4 | Registry | `uv run python scripts/e2e_registry.py --base $BASE` | MCP ×2 + AGENT_SKILLS defaults sync, A2A record auto-created, `DRAFT→PENDING_APPROVAL→APPROVED`, search endpoint answers with well-formed records (`fresh record indexed: False` is normal — see below), one record disabled | 4–6 min |
| 5 | Chat + memory | `uv run python scripts/e2e_chat_memory.py --base $BASE` | session A turn 2 shows turn-1 continuity (short-term); a stated preference appears as an extracted long-term record; session B is influenced by it | 4–6 min |
| 6 | Observability | `uv run python scripts/e2e_observability.py` | all five `/api/observability` endpoints answer; one real trace tree with model + tool spans; 2nd identical call <300 ms (cache) | 3–5 min |
| 6b | Traces | `uv run python scripts/e2e_traces.py` | the just-chatted session's span tree appears in `aws/spans` | 2–4 min |

Steps 6 and 6b default to `http://localhost:8000` when `--base` is omitted — correct on this
box, and part of why the whole tier is run *on* the box.

**Removed for the CN build:** step 2 (Harness deploy, `e2e_harness.py`) posted
`method="harness"`, which the agents router now rejects with 503
`feature.unavailable` until N7 registers the harness method — the script exercised
nothing and has been deleted. Steps 7 (Managed KB, `e2e_knowledge_base.py`) and 8
(Evaluation, `e2e_eval_run.py`) tested the AWS-managed Knowledge Bases and Evaluation
implementations removed in Tasks 3/5 of the CN foundation node; both scripts are gone.

### Step 4's search step reports rather than asserts — and why

`e2e_registry.py` used to end with
`assert any(r["name"] == "expense-report-writer" for r in found)` over
`SearchRegistryRecords` (now `SearchDiscoverableRegistryRecords` in GA), which
**failed on every clean run**. That was a defect in the test,
not in the product. Measured on 2026-08-04 (us-east-1):

- the record exists (`AGENT_SKILLS`, `PENDING_APPROVAL`), and everything before the assertion
  passes — defaults sync, A2A auto-registration, the approval transitions;
- `q=expense` returns `[]` immediately, after 30 s, after ~20 min — and **still `[]` the next
  day, >12 h after the record was created**;
- `q=office` likewise never finds `office-facts`;
- under the preview API, `q=hr` returned a **DRAFT** record; the GA discoverable
  API now returns approved records only;
- every record search *does* return predates the run.

`SearchDiscoverableRegistryRecords` is AWS-side semantic search
(`console_search` → `reg.search_records` adds no platform filtering), and
approval does not make its index immediately consistent. The assertion encoded
an immediate-consistency guarantee the API does not offer.

**Fixed 2026-08-05.** The step now asserts what the API actually guarantees — HTTP 200, a
list, well-formed records — polls three times over ~20 s for the fresh record, and prints
`fresh record indexed: True|False` either way without failing on `False`. So step 4 should be
green; if it is not, the failure is real. The script no longer aborts there, so it reaches its
own cleanup and does not leave `e2e-registry-agent` behind.

**Why this order:** 2 before 3 (cheapest real deploy first — if the pipeline is broken, learn
it in 3 minutes, not 8). 5 before 6/6b (they read back the spans the chat just produced;
running them first tests nothing).

## Extended tier

Only when the release touched these paths, or before a milestone release. Each is
independently runnable.

| Step | Command | Notes | Time |
|---|---|---|---|
| Gateway tool | `uv run python scripts/e2e_gateway_tool.py --base $BASE` | HR question answered through the gateway | 4–6 min |

**Removed for the CN build:** the container-path/native-evaluation, full-evaluation,
configuration A/B experiment, runtime canary, and policy/governance rows tested the
方式A container path, Evaluation/Optimization, and Cedar Policy features deleted in
Tasks 2/3 of the CN foundation node; their scripts (`e2e_eval_extended.py`,
`e2e_experiment.py`, `e2e_runtime_canary.py`, `e2e_policy.py`,
`e2e_gateway_policy_management.py`) no longer exist.

`e2e_golden_path.py --base $BASE` is a compressed alternative to the Core tier
(bootstrap → harness agent → chat ×2 → gateway tool → registry → cleanup, ~10 min).
Use it for a quick confidence check, not as a replacement — it skips zip, KB, observability,
and traces.

## Teardown

Most scripts delete what they created unless you pass `--keep`. **One exception bites:**

- **Any script that aborts mid-run skips its own cleanup.** Step 4 used to do this every time
  before its search assertion was fixed; any step that dies for a *new* reason will do the
  same, so always finish with the inventory diff below rather than trusting exit codes.

After the run:

1. Re-run the [inventory probe](#3-inventory-snapshot-the-before-state) — **including the two
   API snapshots**, since KBs and registry records are invisible to the ledger diff — and
   compare against the before state. Anything your run added and did not remove is a
   **finding**, not a footnote. Expect one legitimate delta: `chat_sessions` grows, because
   it is append-only history rather than a live resource.
2. For a script that died midway, delete its agent by name (they are prefixed `e2e-`):
   ```bash
   curl -s $BASE/api/agents | python3 -c "import json,sys;[print(a['id'],a['name']) for a in json.load(sys.stdin)['agents'] if a['name'].startswith('e2e-')]"
   curl -s -X DELETE $BASE/api/agents/<id>      # with the session cookie
   ```
3. `data/launchpad.db` was backed up during the deploy; the smoke test only adds rows, so
   that backup stays a valid floor if the ledger needs restoring.

## Last recorded run

**2026-08-04, prod box, v0.0.3 — 7 of 8 Core steps passed in ~27 min.** The one failure was
step 4's search assertion (diagnosed above as a test defect, not a regression). No leaked
agents, datasets, experiments or canaries; the step-7 KB and the aborted step-4 agent were
deleted by hand. `defaults sync` left three `PENDING_APPROVAL` records behind — kept
deliberately, since they are legitimate default-catalog entries rather than test fixtures, so
prod now carries two generations of default records (the older `hr-db` / `pirate-speak` set and
the current `hr-database` / `office-facts` / `expense-report-writer` set).

Notably **not** covered by that run: the Extended tier in full, so the bounded-concurrency
gateway send that v0.0.3 is named for was never verified against real AWS before the
feature it exercised (`e2e_experiment.py` / Configuration A/B experiments) was removed
for the CN build (Task 3 of the CN foundation node) — this record is kept as a reminder
that this run predates the CN build and some of what it covered no longer exists.

## Known gaps

State these honestly rather than implying full coverage:

- **方式C (Strands Studio canvas) and the harness method (方式B) have no e2e
  script.** Creation-method coverage is zip_runtime + A2A only. 方式B 503s until N7
  registers the harness method, so `e2e_harness.py` (which only exercised that
  method) has been removed — see "Removed for the CN build" above. Studio must be
  clicked through by hand if the release touched it. (方式A — the
  container/Claude Agent SDK path — was removed for the CN build along with
  `e2e_claude_sdk.py`, which no longer exercised a live creation method.)
- **Two scripts default `--base` to `http://localhost:8000`** (`e2e_observability`,
  `e2e_traces`). They accept the flag, but omitting it targets the local box — pass it
  explicitly if you mean somewhere else.
- **The console UI is only spot-checked.** No automated browser coverage in this procedure.
- **Removed for the CN build:** the Managed KB (`e2e_knowledge_base.py`) and KB gateway
  (`e2e_kb_gateway.py`) bypass-HTTP scripts tested the AWS-managed Knowledge Bases
  implementation and the AgentCore KB connector gateway, both deleted in Tasks 1/5 of the
  CN foundation node; neither script exists anymore.
