# China IT service desk workshop

This package contains the eight-step Chinese learner manual, synthetic ticket service,
five knowledge documents, five skills, tool schemas, policies and two evaluation datasets.
It does not contain credentials or a destination environment's manifest or acceptance
record. Labelled reference screenshots show synthetic example conversations and scores.

Deploy the platform first using [the distribution guide](../../docs/cn/distribution.md).
The policy-engine installation must succeed before the `policy` stage.

## Configure

Copy `config.example.json` outside the repository, then edit every destination field.
Relative `state_dir` paths are relative to that configuration file, not the shell.
Use an existing SSM-managed tools host in the **destination** account. A newly deployed
Launchpad console host can also serve this small workshop; use `tools_user: ubuntu`.
The script never creates or deletes that EC2. The host needs the AWS CLI and permission
to read `launchpad-<scenario>-api-key` from SSM (included in the console host's IAM role).
Choose an unused `/24` inside that host's VPC for `lambda_subnet_cidr`.
Only the workshop Lambda security group gets ingress to `tools_port`.

The operator credentials need the resource creation permissions exercised by the scripts
(SSM, EC2 subnet/route/endpoint/SG, IAM, Lambda, Gateway and the source bucket).
The configured admin must be able to register/approve users, skills and knowledge sources.
Use a fresh console for the fixed learner names `lab01`–`lab10` and the service-desk
accounts; existing unowned usernames cause preparation to stop, never a password reset.

### Lab assets follow the package

The assets under `lab-assets/` (service code, Lambda bridge, tool schemas, prompts, knowledge
articles, Skill zips, datasets) and the seed `policies/` belong to the package; the copy in
`state_dir` is only a cache. Every script that reads that copy (`refresh_service.py`,
`refresh_targets.py`, `refresh_datasets.py`, `student_kit.py`, `agents.py`, `reset_student.py`,
`prepare_console.py`, `prepare_policy.py`, `deploy_tools.py`, `build_manual.py`) first calls
`lab.sync_assets()`, which copies new and changed `lab-assets/` files over, adds missing
`policies/` files (the rendered Cedar `prepare_policy.py` writes there is never overwritten),
keeps extra files and prints one `Assets synced from the package (…)` line only when something
changed — so a repo update reaches an existing environment on the next refresh, with no hand
copy. The tools host's `.run` marker is checked by the same scripts: a marker written before
`model_id` left the config digest is accepted, rewritten and reported as `run marker migrated`;
a real mismatch prints both values into `evidence/<label>.json` before the script stops.

### Service-desk accounts

`staff_accounts` lists the service-desk ("staff") usernames; the default is `["itdesk01"]`,
which reproduces the single-account behaviour exactly. With one personal staff account per
learner (manual 1.3: `lab03` ↔ `itdesk03`), list them all. Every listed account gets
`policy.manage`, may read every ticket, resolve tickets and publish knowledge; the three
staff rules are generated with one `principal == AgentCore::User::"…"` term per account.
`staff_publish_targets` optionally maps a staff username to the `s3://bucket/prefix` of
that account's own knowledge-base data source; accounts without an entry publish to the
shared knowledge-base source created by the `console` stage. Create the extra data sources
yourself; the scripts only grant the Lambda `s3:PutObject` on each listed prefix.

Both settings are excluded from the run identity digest, so they can be changed in an existing
run directory. Re-run `workshop.py console` (new accounts), `workshop.py tools` (service
environment, Lambda code and IAM) and `workshop.py policy` (regenerated Cedar: a policy whose
active text differs from the generated text gets a new draft via PUT and is published). The
service reads `ITSM_STAFF_ACCOUNTS` (comma-separated) and `ITSM_STAFF_PUBLISH_TARGETS`
(JSON object username → prefix) from the systemd drop-in `10-staff.conf` that
`scripts/refresh_service.py` writes; it restarts the unit only when the code or that file changed.
`workshop.py staff` deploys the reference Agent for the first staff account; add `--all-staff`
for one per account (`itdesk-console-cn` for the first, `<user>-console-cn` for the others).

TLS verification defaults to on. For the template's self-signed certificate, provide a
trusted CA file in `tls_verify`, or explicitly use `false` for your controlled lab.
No credential is sent to a configured plain HTTP URL.

```bash
export LAUNCHPAD_LAB_CONFIG=/absolute/path/itsm-config.json
uv run --project samples/itsm-cn samples/itsm-cn/scripts/workshop.py plan
uv run --project samples/itsm-cn samples/itsm-cn/scripts/workshop.py all
```

Stages run serially: `console` → `tools` → `policy` → `staff` → `manual`. The `staff`
stage deploys the service desk reference Agent for chapter 8. Each stage may also
be run separately. State and evidence go to `state_dir`. Keep the config and manifest:
they establish ownership for retries and reset. A changed configuration requires a new
run directory; do not reuse one run's ledger against another account.

After an API timeout, inspect the target resource before retrying if it was not yet
recorded in the manifest. The scripts stop on conflicts rather than adopting an
unknown resource. Do not run individual scripts concurrently.

After attaching a new Gateway, policy validation may briefly be unable to read its
tool schemas. The preparation retries that specific read error up to 13 attempts,
ten seconds apart. Cedar/compiler errors and publish failures are not retried.

### Changing the model

The model is an experiment-time choice, not part of the environment's identity: `model_id`
is a *mutable* config key (`lab.MUTABLE_KEYS`, excluded from the config digest), so changing
it never makes the scripts refuse the `state_dir`. The lab config is the single source of
truth — never edit the manifest's `model_id` by hand, and the student manual never names a
model (the resource card's **模型** row carries it). To switch:

1. **Console roles** — **模型提供方** → **模型角色** tab (administrator): `Agent 默认模型`
   (`agent_default`) is the model the Agent wizard pre-selects — set it to the model the
   Agents will run; `评估裁判` (`judge`) and `提示词优化器` (`prompt_opt`) are the evaluation /
   recommendation models — they may differ from the Agent model (e.g. the same OpenAI
   gpt-oss model for both, or Kimi for the Agents and gpt-oss for judging). Click **探测** on
   each row you changed; a LiteLLM / OpenAI-compatible provider must also have its credential
   registered (**注册凭据**) and its card's **探测** passing.
2. **Lab config** — set `model_id` to the new id exactly as the catalog lists it (for example
   `litellm.gpt-oss-120b`). Every script checks it against `GET /api/capabilities` (the list
   the console pickers read: tool-capable, provider configured) *before* deploying anything
   and refuses with the usable ids otherwise.
3. **Redeploy** — `reset_student.py --student labNN --apply` (dry-run first): it prints
   `model_id: <old> -> <new> (from config)`, updates the manifest copy, redeploys the three
   Agents (employee → stage-1 baseline, evaluation → kit spec, service-desk → kit spec — the
   last one only when its model actually changed, so its sessions survive an ordinary reset)
   and rewrites `cards/labNN.md`, whose **模型** row now shows the configured model.
   `student_kit.py --cards-only` and `agents.py` sync the manifest the same way; `--model <id>`
   on `reset_student.py` / `agents.py` deploys one run with another model without touching the
   config, the manifest or the card.
4. **Student manual** — nothing changes: learners read the model from their card.

### Employee Agent row of the card

`employee_agent_prebuilt` (boolean, default `true`, also in `lab.MUTABLE_KEYS`, so flipping it
never changes the config digest) selects the wording of the resource card's **员工 Agent** row —
the two strings organizer manual 1.12 asks for, verbatim:

| `employee_agent_prebuilt` | 员工 Agent row |
| --- | --- |
| `true` (default; `agents.py` pre-built `labNN-itdesk-cn`) | `labNN-itdesk-cn（已预建，直接打开）` |
| `false` (the learner creates it in scenario 1 step 3.1.1) | `labNN-itdesk-cn（未预建，场景一 3.1.1 由学员创建）` |

`student_kit.py` (live run, `--dry-run`, `--cards-only`) and the card `reset_student.py --apply`
rewrites all read it from the lab config, so the row is never edited by hand after a reset.

## Hand off to learners

`state_dir/IT服务台小助手-中国区实验手册.html` is a standalone offline handout.
The guide uses the configured model and the actual created Gateway name.
Bundled screenshots are explicitly labelled as reference-environment images. Put the
new run's screenshots under `state_dir/screenshots/` with the names in `handbook.md`
to replace them. `build_manual.py --final` requires actual run screenshots and an
evaluation summary; reference images never satisfy that acceptance check.
Learner Agents are deliberately created by learners in the GUI.
Optionally create one reference Agent for acceptance:

```bash
uv run --project samples/itsm-cn samples/itsm-cn/scripts/agents.py lab01 3
uv run --project samples/itsm-cn samples/itsm-cn/scripts/refresh_callers.py
uv run --project samples/itsm-cn samples/itsm-cn/scripts/credentials.py lab03
```

After changing the package's `service.py`, `lambda_bridge.py` or the staff settings, roll the
installed service and Lambda without touching IAM or the Gateway:

```bash
uv run --project samples/itsm-cn samples/itsm-cn/scripts/refresh_service.py
```

After changing a tool description or input schema in `lab-assets/tools.json`, push it to the
live Gateway targets (`deploy_tools.py` creates a target once and skips it afterwards;
`workshop.py tools` runs the `--apply` form after `deploy_tools.py`). The dry run prints
`UNCHANGED` / `WOULD UPDATE` per target with the tools that differ; `--apply` calls
`update_gateway_target` for those only and waits for `READY`. Tool names must stay the same
(the Cedar rules name `<target>___<tool>`):

```bash
uv run --project samples/itsm-cn samples/itsm-cn/scripts/refresh_targets.py
uv run --project samples/itsm-cn samples/itsm-cn/scripts/refresh_targets.py --apply
```

**Tool call names (N17-obs543).** The Harness exposes a lab Gateway tool to the model as
`launchpad_gw_<target>___<tool>` — `launchpad_gw_servicenow___get_ticket`,
`launchpad_gw_servicenow-knowledge___draft_article`. gpt-oss kept calling the knowledge tools
under the `servicenow` prefix (`Unknown tool`), so the staff prompt (`prompts.json` `staff`,
used by every service-desk Agent the kit or `agents.py <staff> 3` builds) lists every tool by
its exact full name per target, and every `tools.json` description starts with
`调用名 launchpad_gw_<target>___<tool>。`. A service-desk Agent deployed before this change
picks the prompt up on its next redeploy (`agents.py itdesk01 3` for the reference Agent; the
kit's `lab03-itdesk-console-cn` through `reset_student.py` or a console redeploy) and the
descriptions once `refresh_targets.py --apply` has run. `backend/tests/test_itsm_desk_tools.py`
pins both to `student_kit_lib.harness_tool_names`.

The last command explicitly reveals one password from SSM. Send account cards individually;
never redirect them into this repository or the shared handout.
No admin password is distributed with the lab.

## Per-student kit (manual 1.3: two logins, three Agents)

Manual 1.3 gives every learner a second, personal service-desk login and personal
copies of the knowledge base, the datasets and two pre-built Agents, so nobody shares
`itdesk01` and one learner's publish cannot change another learner's "not found before
publish" baseline. After `workshop.py all` (the shared accounts, Skills, Gateway and
engine must exist), run the kit once per learner:

```bash
uv run --project samples/itsm-cn samples/itsm-cn/scripts/student_kit.py lab03 --dry-run
uv run --project samples/itsm-cn samples/itsm-cn/scripts/student_kit.py lab03
uv run --project samples/itsm-cn samples/itsm-cn/scripts/student_kit.py lab04 --staff desk-four
```

`--dry-run` prints the complete call plan and the resource card without logging in or
calling anything. The live run creates, in this order, and records every item under
`students.<labNN>` of the manifest with ownership:

| Resource | Created as | Name for lab03 |
| --- | --- | --- |
| Service-desk account | admin (SSM SecureString `<PREFIX>users/itdesk03`, never printed) | `itdesk03`, member; itdesk01's grant minus `policy.manage` and `eval.run`. `staff_accounts` may list the pair's desk account up front: when `workshop.py console` / `staff` already created it (manifest `users.itdesk03`, ownership `created`), the kit adopts that account as recorded (`students.lab03.staff.ownership = adopted`, its workshop-stage grant and SSM key kept) instead of refusing it; a same-named account the manifest does not record is still refused |
| Personal knowledge base | admin, so both of the learner's logins see it in either visibility mode | `anypharma-it-helpdesk-cn-lab03`: the 5 shared documents uploaded to its own upload data source and synced |
| Personal datasets | the learner | `itsm-exam-8-cn-lab03` (8 items), `itsm-held-out-cn-lab03` (2 items) |
| Evaluation Agent | the learner | `lab03-itdesk-eval-cn`: chapter-3 prompt plus a read-only rule, personal KB, four learner Skills, `servicenow` attached with `config.tools = [servicenow___get_ticket, servicenow___search_tickets]` only, memory off |
| Service-desk Agent | the service-desk account | `lab03-itdesk-console-cn`: staff prompt naming `itdesk03` and every tool by its exact `launchpad_gw_<target>___<tool>` name (N17-obs543), personal KB, all five Skills, `servicenow` + `servicenow-knowledge`, memory off |
| Caller map | admin | re-publishes `itsm_agents_call_tools` (Cedar unchanged) so the Gateway knows the two new Agents; `--skip-refresh-callers` to omit |

The datasets are derived from the shared files by the pure function
`student_kit_lib.adapt_dataset(items, student)`: `我是lab01` becomes the learner,
`RITM0010001` / `INC0020001` become the learner's own tickets (manual table 2.2), and
H02, T01, T02, T03 get read-only expectations (a recommendation or a query, never an
expected 建单 / 转派 / 评论). T02 asks about the learner's CRM ticket by number and T02 /
T03 accept whatever state the tool returns: the manual's scenario two (4.2.5
`add_comment`) moves that ticket from `on_hold` to `in_progress` before scenario three
evaluates it, so a question about "那张挂起的单" failed for every learner who followed the
manual (practice round R3). `backend/tests/test_itsm_student_kit.py` and
`test_itsm_refresh_datasets.py` check the adaptation and validate the bodies against the
backend's request schemas.

When the shipped assets or `READ_ONLY_ITEMS` change after the kit has run,
`scripts/refresh_datasets.py` re-derives every recorded personal dataset and, with
`--apply`, replaces its items in place (`PUT /api/eval/datasets/{id}` as the learner, the
creator): scenario ids, splits and the count are checked to stay the same, so completed
runs remain comparable by case id; a dataset not recorded on the manifest row is skipped,
never adopted. `--dry-run` (default) prints which scenarios would change.

Re-running is idempotent: finished steps are skipped and an interrupted step resumes.
Like `prepare_console.py`, the kit refuses to adopt a same-named account, KB, dataset
or Agent that the manifest does not record. The run ends with the resource card of the
organizer checklist (§二) filled with real values (also written to
`evidence/student-labNN-resource-card.md`). Passwords are handed out with
`credentials.py` through a separate channel.

The card is also written to `<state_dir>/cards/labNN.md` (UTF-8 Markdown, overwritten on
every run) and its relative path is recorded as `students.labNN.card_path` in the
manifest, so the organizer has one file per learner to hand out and the learner → service
desk mapping (`lab01` → `itdesk01`, …) survives outside the manifest. Cards name the SSM
parameters of the credentials, never a password. To regenerate every card from the
manifest alone — no login, no API call — run
`uv run --project samples/itsm-cn samples/itsm-cn/scripts/student_kit.py --cards-only`
(add a learner name to rewrite one card).

What the kit does **not** do, and why:

- **Publish-target binding and the Cedar service-desk list.** The shipped policies and
  the tools backend still name `itdesk01` as the only service desk (requester bypass,
  `servicenow-knowledge`, `resolve_ticket`). The per-staff list is the `staff_accounts`
  field of the config (a list of service-desk usernames; the kit warns when the staff
  login is missing from it) consumed by `prepare_policy.py` / `deploy_tools.py`; the
  publish target of each staff login (its learner's personal KB data source, shown on
  the card as `kb / data source <ds_id>`) is bound there, not by the kit. Binding a new
  target needs two AWS grants besides the service environment, and `refresh_service.py`
  (run by `workshop.py tools`) applies both on every roll for the full target set — the
  shared source plus every `staff_publish_targets` value: the Lambda role's inline policy
  `PublishOnlyWorkshopKnowledge` (`s3:PutObject` on each `s3://bucket/prefix*`) and the
  policy of the S3 Gateway VPC endpoint the Lambda reaches S3 through (manifest
  `lambda_s3_endpoint`; modified only when it differs, printed as `unchanged` otherwise).
  A target missing from either grant publishes with `AccessDenied`. The granted set is
  recorded as `service.publish_targets` in the manifest.
- **The employee Agent** `lab03-itdesk-cn` is created by the learner in chapter 3 (or
  pre-built with `agents.py lab03 1`), as the manual intends.
- **Per-member grants do not exist.** Visibility is a workspace-wide mode (`shared` or
  `isolated`); there is no way to show one learner's KB, Agent or evaluation runs to one
  other account. In `shared` mode (the default) every learner sees every personal
  resource and must pick their own by name; in `isolated` mode the admin-created KB
  stays visible to everyone, the Agents and datasets only to their creator and admins,
  and the staff login cannot read the learner's runs or memory at all.
- **No evaluation fixture snapshot.** The tools host serves one shared synthetic ticket
  database; `get_ticket` / `search_tickets` read the live state the learner's chapter-4
  comments changed. The kit enforces "read only" through the Agent's `allowedTools`,
  which the Harness applies at runtime; the Gateway policy itself permits every
  workspace Agent every tool, so the restriction is per Agent, not per principal.
- **Dataset names are not unique on the platform**; the manifest is what makes the
  kit idempotent. Delete a half-created learner's rows from the manifest only after
  removing the resources in the console.

## Acceptance

First run the isolated business tests (no AWS or live database):

```bash
uv run --project samples/itsm-cn samples/itsm-cn/scripts/test_business.py
```

Then perform all eight sections of the generated manual in the new console. Record the
release commit, account/region, Agent version, actual tool results, policy decisions,
memory recall and evaluation IDs. Local business tests do not prove Gateway enforcement.

Every lab rule, the requester policy included, is published `active` (the manual presumes an enforced gateway from 2.1 on). The Agent baseline,
user baseline and staff-only restrictions are active. In chapter 4, switch requester
enforcement to active and test self/impersonation/staff cases in new sessions.
The final handoff must explicitly say whether chapter 4 enforcement has been enabled.

## Reset and ownership

```bash
uv run --project samples/itsm-cn samples/itsm-cn/scripts/reset_lab.py
uv run --project samples/itsm-cn samples/itsm-cn/scripts/reset_lab.py --apply
```

This resets only the synthetic service database. It does not delete published KB documents,
accounts, Agents, memories or the shared host. To repeat chapter 8, remove this run's
generated `KBxxxxxxx.md` documents through the knowledge-base GUI and synchronize the source.
There is no whole-account cleanup command. The manifest records the lab-owned Lambda,
Gateway, roles, subnet, route table, endpoint and security-group rule for scoped teardown;
the tools host is always referenced, never owned.

### Cleanup after a class (per student)

`cleanup_students.py` returns the console to its pre-class state one learner at a time,
keeping the pre-provisioned base. It reads the manifest row `students.labNN` and the live
console as the configured administrator; the default is a dry run that prints the exact
plan (every `DELETE` it would issue, with ids) and mutates nothing.

```bash
# everyone except lab01 — plan first, then apply
uv run --project samples/itsm-cn samples/itsm-cn/scripts/cleanup_students.py --all-but lab01
uv run --project samples/itsm-cn samples/itsm-cn/scripts/cleanup_students.py --all-but lab01 --apply
# named learners
uv run --project samples/itsm-cn samples/itsm-cn/scripts/cleanup_students.py --students lab02,lab03 --apply
# keep the service-desk accounts (and the Agents they created outside the kit)
uv run --project samples/itsm-cn samples/itsm-cn/scripts/cleanup_students.py --all-but lab01 --apply --keep-staff
# also drop the deleted staff accounts from the lab config (atomic rewrite, <config>.bak kept)
uv run --project samples/itsm-cn samples/itsm-cn/scripts/cleanup_students.py --all-but lab01 --apply --trim-config
```

Per learner, in dependency order: active evaluation runs on the learner's Agents or datasets
are stopped (the Agent delete refuses `409 agent.in_use` while one runs); every Agent the
kit recorded, the `agents.py` employee Agent, every live `labNN-itdesk-cn` /
`labNN-itdesk-console-cn` / `labNN-itdesk-eval-cn` / `labNN-sim-cn` and every Agent created
by the learner or (unless the staff account is kept) their staff account is deleted and
polled until `GET /api/agents/{id}` answers 404 — the Agent teardown takes the Runtime,
role, logs, artifacts, registry record and the Agent's long-term memory records with it,
and the learner's chat sessions become unreachable with their Agent (there is no session
delete route); `failed` / `stopped` runs are deleted while `completed` runs stay as
evaluation history (the API refuses `409 run.not_deletable`); the two personal datasets go
with `?force=true`; the personal knowledge base is deleted (ledger row, `kb/<kb_id>/` S3
prefix and index — never forced: a KB still mounted by somebody else's Agent is reported);
remaining long-term memory records of the deleted Agents' actors `<agent_id>__*` are swept
as administrator (skipped when the workspace has no memory feature); the learner account
and the staff account are deleted (`--keep-staff`, or a staff row the manifest marks
`shared` / `referenced`, keeps the staff account; the SSM credential parameters always
stay, so `prepare_console.py` / the kit re-register with the same passwords); finally
`students.labNN`, `users.labNN`, `agents.labNN` (and `users.<staff>` when deleted) leave
the manifest under the manifest lock, `cards/labNN.md` is deleted and
`evidence/cleanup-labNN.json` records every step.

Never deleted, whatever a manifest row says: the shared knowledge base
`anypharma-it-helpdesk-cn`, the Gateway, the policy engine and its policies, the Lambda and
the synthetic service, the Skills, the reference Agent `itdesk-console-cn` (and
`agents.<first staff account>`), system presets, the organizer accounts (`admin`,
`workshop-admin`, `itdesk01`, the configured `admin_username`, the first `staff_accounts`
entry) and anything not recorded for — or named after — the requested learner. A learner
named in `--students` but missing from the manifest is refused (exit 2); `--all-but` prints
what it keeps. Every 404 is "already gone", so a half-cleaned learner finishes cleanly; a
learner with a failure (a 409 that outlives the retries, an unexpected status) keeps their
manifest row and the run exits 1 — fix the cause and re-run.

The lab config is not edited unless `--trim-config` is given. Either way, once a staff
account listed in `staff_accounts` / `staff_publish_targets` has been deleted, remove it
from the config (or let `--trim-config` do it) and re-run, in this order,
`refresh_service.py` (service environment, Lambda publish grants) and `prepare_policy.py`
(regenerated Cedar staff rules); the script prints both commands.
`backend/tests/test_itsm_cleanup_students.py` runs the script against a stub console.

### 课后复位 / Post-class reset (the kept pair)

`cleanup_students.py` deletes a learner; `reset_student.py` keeps one and undoes only what
the manual's exercises produced, so the SAME pair (employee `labNN` + service desk
`itdeskNN`, one pair per environment) starts clean next time — instructor manual
「八、课后清理」 §8.2. It reads the manifest row `students.labNN` and the live console as the
configured administrator; the default is a dry run that prints every call it would make and
mutates nothing.

```bash
# plan first (default: every student in the manifest), then apply
uv run --project samples/itsm-cn samples/itsm-cn/scripts/reset_student.py --student lab01
uv run --project samples/itsm-cn samples/itsm-cn/scripts/reset_student.py --student lab01 --apply
# keep the synthetic tickets as they are; accounts valid for 14 days instead of 30
uv run --project samples/itsm-cn samples/itsm-cn/scripts/reset_student.py --student lab01 --apply --skip-tickets --extend-days 14
# the next class runs "not pre-built": delete the employee Agent so the learner re-creates it
uv run --project samples/itsm-cn samples/itsm-cn/scripts/reset_student.py --student lab01 --apply --employee-agent delete
```

Per learner, in this order: active evaluation runs on the kit Agents are stopped (a redeploy
refuses `409 agent.in_use` while one runs); the employee Agent `labNN-itdesk-cn`, when it
exists, is redeployed to the stage-1 baseline (`student_kit_lib.stage1_agent_spec`: the
lab config's `model_id`, `step1` of `prompts.json`, no tool / Skill / KB / policy, long-term
memory off — the rebuilt Runtime drops its old chat sessions and long-term records) and
polled until `active` (≤ 15 min), or deleted with `--employee-agent delete` / a manifest row
`agents.employee.prebuilt: false`; the evaluation Agent `labNN-itdesk-eval-cn` is redeployed
to the kit `spec` the manifest row recorded (model = the lab config's `model_id`), and an
ACCEPTED recommendation on one of its runs is reported (it cannot be un-accepted); the
service-desk Agent is redeployed to its kit `spec` only when the model it runs differs from
the configured one (see "Changing the model"); leftover Agents
named `labNN-*-cn` outside the kit set, or created by the pair, are deleted and polled until
404; long-term memory records of the actors `<agent_id>__labNN` of the employee and
evaluation Agents are deleted strategy by strategy (`GET /api/memory/namespaces` →
`GET /api/memory/records` → `DELETE /api/memory/records/{id}?actor_id=`; skipped when the
workspace has no memory feature); every published article `KB<7 digits>.md` directly under
the personal KB's data-source prefix `kb/<kb_id>/<ds_id>/` is deleted from S3 with boto3
(the lab's `profile` / region), a FULL rebuild sync (`{"full": true}`) is started and polled
to completion, and the remaining document count is printed (expect the 5 base files); the
synthetic tickets are reset exactly as `reset_lab.py --apply` does (ALL tickets of the
environment — `--skip-tickets` leaves them); every lab rule, the requester rule
`itsm_requester_is_self` included, is set back to `active` (what `prepare_policy.py`
installs — the manual presumes an enforced gateway from 2.1 on; the engine, the Gateway attachment and the Cedar text are not
touched); both accounts get `expires_at` = now + `--extend-days` (default 30) and the new
dates are printed; finally the completed evaluation runs that remain (permanent history: the
API refuses `409 run.not_deletable`, so name them to the class) and the chat sessions still
listed on the console Agent (not rebuilt) are reported, `evidence/reset-labNN-<ts>.json` is
written, `students.labNN.last_reset` is recorded and the resource card `cards/labNN.md` is
re-rendered (`student_kit.py --cards-only` semantics) so `card_path` stays valid.

Never touched: the shared knowledge base, the Gateway, the policy engine, the tools service
and Lambda code, the Skills, the reference Agent, system presets, the organizer accounts
(`admin`, `workshop-admin`, `itdesk01`, the configured administrator, the first
`staff_accounts` entry) and other learners. A `--student` missing from the manifest is
refused (exit 2); a failed step is reported in the evidence file and the run exits 1 — fix
the cause and re-run (every 404 is "already gone", a `409 agent.in_use` /
`agent.deploy_in_progress` is retried). `backend/tests/test_itsm_reset_student.py` runs the
script against a stub console, a stub S3 client and a stub SSM `remote`.
