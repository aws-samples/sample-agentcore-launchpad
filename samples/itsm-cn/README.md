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
| Service-desk account | admin (SSM SecureString `<PREFIX>users/itdesk03`, never printed) | `itdesk03`, member; itdesk01's grant minus `policy.manage` and `eval.run` |
| Personal knowledge base | admin, so both of the learner's logins see it in either visibility mode | `anypharma-it-helpdesk-cn-lab03`: the 5 shared documents uploaded to its own upload data source and synced |
| Personal datasets | the learner | `itsm-exam-8-cn-lab03` (8 items), `itsm-held-out-cn-lab03` (2 items) |
| Evaluation Agent | the learner | `lab03-itdesk-eval-cn`: chapter-3 prompt plus a read-only rule, personal KB, four learner Skills, `servicenow` attached with `config.tools = [servicenow___get_ticket, servicenow___search_tickets]` only, memory off |
| Service-desk Agent | the service-desk account | `lab03-itdesk-console-cn`: staff prompt naming `itdesk03`, personal KB, all five Skills, `servicenow` + `servicenow-knowledge`, memory off |
| Caller map | admin | re-publishes `itsm_agents_call_tools` (Cedar unchanged) so the Gateway knows the two new Agents; `--skip-refresh-callers` to omit |

The datasets are derived from the shared files by the pure function
`student_kit_lib.adapt_dataset(items, student)`: `我是lab01` becomes the learner,
`RITM0010001` / `INC0020001` become the learner's own tickets (manual table 2.2), and
H02, T01, T02, T03 get read-only expectations (a recommendation or a query, never an
expected 建单 / 转派 / 评论). `backend/tests/test_itsm_student_kit.py` checks the
adaptation and validates the bodies against the backend's request schemas.

Re-running is idempotent: finished steps are skipped and an interrupted step resumes.
Like `prepare_console.py`, the kit refuses to adopt a same-named account, KB, dataset
or Agent that the manifest does not record. The run ends with the resource card of the
organizer checklist (§二) filled with real values (also written to
`evidence/student-labNN-resource-card.md`). Passwords are handed out with
`credentials.py` through a separate channel.

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

The requester policy starts in `log_only` for the chapter 3 contrast. The Agent baseline,
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
