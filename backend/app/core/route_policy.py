"""Console authorization: one declarative table for every `/api` route.

Why a table instead of per-route `Depends(require_admin)`:

* a reviewer can audit the whole authorization posture in one file rather than by
  grepping 19 routers;
* it is **default-deny** — a route with no entry raises instead of silently
  serving, so a newly added endpoint cannot ship unclassified. `tests/
  test_route_policy.py` enumerates the live routes and fails on drift in both
  directions.

Roles: `ADMIN` requires an administrator identity; `MEMBER` requires only a live
session (the `auth_middleware` 401 already covers that); `PUBLIC` is reachable
without one (health, docs, and the login surface itself). A `perm:<key>` value
requires that member permission (admins implicitly hold all; a member holds every
key unless an admin stored an explicit denial on their account — see
`auth.AGENT_PERMISSIONS` and the Users console).

The classification principle, signed off by river 2026-08-03: **admin for routes
that execute code, change deployed or cloud state, mint credentials, or change
governance posture; member for reads and for the member's own interaction with an
agent.** Amended by river 2026-08-07: the agent-lifecycle routes (deploy,
discovery import, delete, convert and the deploy-flow skill helpers) are
member-grantable via `perm:agents.*`, **default granted**, revocable per user.
Amended by river 2026-08-11: **the whole console is member-reachable except user
management** — every route that used to demand `ADMIN` (registry writes, knowledge
bases, governance, API keys, studio local exec, tools/demos) is now
`MEMBER`; only `/api/users*` still requires an administrator. The `perm:*` entries
keep their revocation semantics unchanged.
Amended 2026-10-04 (issue #55): creating, editing and deleting AgentCore Memory
resources (`/api/memory/resources` writes) is member-grantable via
`perm:memory.manage` on the same default-granted, revocable terms — DeleteMemory
is irreversible — and those routes reach only memories the workspace manages.
Adopting an existing account memory into management is `ADMIN`.
Amended 2026-10-22 (N17-obs248, user ruling): `POST /api/registry/records/{record_id}/action`
stays `MEMBER` because `submit` is a member operation, but its handler gates per action —
`approve`, `publish`, `reject` and `disable` require an administrator (403
`registry.approval_admin_only`); platform-owned records are still approved by the `system`
actor inside the platform paths, never through this route.
Since 2026-08-12 the table carries a **second dimension**: whether a route
operates inside a workspace (one account/region environment). `WORKSPACE_EXEMPT`
names the hub-global routes; every other entry is workspace-scoped, and
`enforce_route_policy` resolves + authorizes the caller's workspace for it
before the handler runs (`routers/workspaces.resolve_workspace`). Absence from
the exempt set *is* the classification, so a new route cannot ship without one.

Consequences worth knowing before editing this table:

* Data is partitioned per workspace, not per user: within a workspace every
  member still sees and can mutate the same shared agents, records, datasets and
  gateways, but a member only reaches the workspaces an admin granted them
  (`user_workspaces`), and a resource id belonging to another workspace answers
  404. `ADMIN` marks user/workspace management, announcement publication and
  the gateway's consent portal (it passes an operator-named IAM role);
  it does not generically mark "state changes".
* The studio local-exec surface (`/api/execute*`, conversations writes) stays safe
  in production through its own handler guard (`local_exec`, refused outright in
  prod unless explicitly opted in) — that guard, not this table, is the real
  boundary there.
* Invoking an agent is deliberately `MEMBER` (`/api/agents/{id}/invoke`): it is the
  same capability the Chat console gives every member, so gating it while Chat stays
  open would protect nothing.

The table is the only route-level admin check (N17-obs284, r17-s2): a handler of an
`ADMIN` route does not repeat it with `Depends(require_admin)` or `require_admin(request)` —
`tests/test_route_policy.py::TestAdminRoutesAreTableOnly` fails if one comes back and proves,
per `ADMIN` route, that the table refuses a member before any route dependency or handler body
runs. What handlers still check is what a `(method, path)` row cannot express: per-action
gates inside a `MEMBER` route (registry `ADMIN_ONLY_ACTIONS`), per-field gates
(`model_registry.require_admin_for_credential_fields`, `routers/model_providers.
_require_admin_for_scope`), per-row ownership (`services/ownership.require_creator_or_admin`)
and protected-resource refusals (`refuse_protected_mutation`).

There is deliberately **no** setting that disables this table. A flag that turns
authorization off is the vulnerability; fixing a misclassification means editing
the entry.
"""

from typing import Any

from fastapi import Request

from app.core.errors import AppError
from app.routers.auth import app_path, require_admin, require_permission
from app.routers.workspaces import resolve_workspace

ADMIN = "admin"
MEMBER = "member"
PUBLIC = "public"

# Member-grantable permissions (auth.AGENT_PERMISSIONS keys), default granted,
# revocable per user in the Users console.
PERM_AGENT_DEPLOY = "perm:agents.deploy"
PERM_AGENT_IMPORT = "perm:agents.import"
PERM_AGENT_DELETE = "perm:agents.delete"
PERM_IDENTITY_MANAGE = "perm:identity.manage"
PERM_IDENTITY_GRANT = "perm:identity.grant"
PERM_MEMORY_MANAGE = "perm:memory.manage"
# starting evaluation / insights runs (billable judge calls) is member-grantable
PERM_EVAL_RUN = "perm:eval.run"
# D20 / Global model: authoring / publishing Cedar policies and managing policy engines is
# member-grantable; attaching an engine to a gateway (its governance posture) is ADMIN.
PERM_POLICY_MANAGE = "perm:policy.manage"
_PERM_PREFIX = "perm:"

API_PREFIX = "/api"

# (HTTP method, route.path_format) -> required role
ROUTE_POLICY: dict[tuple[str, str], str] = {
    # ---- health, docs, and the login surface (reachable without a session) ----
    ("GET", "/api/health"): PUBLIC,
    ("GET", "/api/docs"): PUBLIC,
    ("GET", "/api/openapi.json"): PUBLIC,
    ("GET", "/api/auth/status"): PUBLIC,
    ("POST", "/api/auth/login"): PUBLIC,
    ("POST", "/api/auth/register"): PUBLIC,
    ("POST", "/api/auth/logout"): MEMBER,
    # ---- agents: lifecycle changes are member-grantable permissions (default
    # granted, revocable per user); reads and invoke are plain member ----
    ("GET", "/api/agents"): MEMBER,
    ("POST", "/api/agents"): PERM_AGENT_DEPLOY,
    ("GET", "/api/agents/discovery"): MEMBER,
    ("POST", "/api/agents/discovery/import"): PERM_AGENT_IMPORT,
    # BYOC artifact staging is one half of a deploy, so it carries deploy perms
    ("POST", "/api/agents/uploads"): PERM_AGENT_DEPLOY,
    ("GET", "/api/agents/uploads/{upload_id}"): MEMBER,
    ("GET", "/api/agents/{agent_id}"): MEMBER,
    ("GET", "/api/agents/{agent_id}/identity"): MEMBER,
    ("GET", "/api/agents/{agent_id}/versions"): MEMBER,  # read-only AWS view
    ("GET", "/api/agents/{agent_id}/trace-status"): MEMBER,  # N17-obs465, read-only logs
    ("DELETE", "/api/agents/{agent_id}"): PERM_AGENT_DELETE,
    ("POST", "/api/agents/{agent_id}/redeploy"): PERM_AGENT_DEPLOY,
    ("POST", "/api/agents/{agent_id}/invoke"): MEMBER,  # parity with Chat
    # spec snapshots (upstream #277, reads only in CN): ledger reads
    ("GET", "/api/agents/{agent_id}/snapshots"): MEMBER,
    ("GET", "/api/agents/{agent_id}/snapshots/diff"): MEMBER,
    ("GET", "/api/agents/{agent_id}/snapshots/{seq}"): MEMBER,
    ("GET", "/api/jobs/{job_id}"): MEMBER,
    # ---- system-managed presets: status is a ledger read; install/repair and
    # uninstall create or delete billable AWS resources on an operator's explicit
    # request and are the ONLY mutation paths for a preset (the agent routes above
    # refuse rows that carry Agent.system_key, whatever perm:* the caller holds) ----
    ("GET", "/api/system-agents"): MEMBER,
    ("POST", "/api/system-agents/{preset_key}/install"): ADMIN,
    # registers / verifies the preset's published Skill release in the Registry
    # (one Registry write at most, no S3 write) — administrator, like install
    ("POST", "/api/system-agents/{preset_key}/skill-registration"): ADMIN,
    # the preset's own job log (install/repair/uninstall) — names AWS resources
    ("GET", "/api/system-agents/{preset_key}/jobs/{job_id}"): ADMIN,
    ("DELETE", "/api/system-agents/{preset_key}"): ADMIN,
    # ---- architect assistant (SE-039): discussion with the preset is member
    # (parity with Chat); approving a proposal deploys a NEW agent and rides the
    # same permission as POST /api/agents. Conversations are owner-bound on top. ----
    ("GET", "/api/assistant/architect"): MEMBER,
    ("GET", "/api/assistant/architect/conversations"): MEMBER,
    ("POST", "/api/assistant/architect/conversations"): MEMBER,
    ("GET", "/api/assistant/architect/conversations/{conversation_id}"): MEMBER,
    # sharing: the owner (upstream #226, ported 2026-10-16) or an administrator — the
    # handler refuses a non-owner collaborator (403 assistant.share_forbidden); nobody
    # but an admin can reach a private conversation it does not own
    ("PUT", "/api/assistant/architect/conversations/{conversation_id}/sharing"): MEMBER,
    ("POST", "/api/assistant/architect/conversations/{conversation_id}/catalog"): MEMBER,
    ("PUT", "/api/assistant/architect/conversations/{conversation_id}/preparation"): MEMBER,
    ("POST", "/api/assistant/architect/conversations/{conversation_id}/preparation/skills"):
        PERM_AGENT_DEPLOY,
    ("POST", "/api/assistant/architect/conversations/{conversation_id}/turns"): MEMBER,
    # N-ARCH-obs15: reattach to a turn (same member parity as the turn itself)
    ("GET", "/api/assistant/architect/conversations/{conversation_id}/turns/{turn}/stream"):
        MEMBER,
    ("PUT", "/api/assistant/architect/conversations/{conversation_id}/proposal"): MEMBER,
    ("POST", "/api/assistant/architect/conversations/{conversation_id}/proposal/reject"): MEMBER,
    ("POST", "/api/assistant/architect/conversations/{conversation_id}/proposal/approve"):
        PERM_AGENT_DEPLOY,
    # ---- clearing a conversation (History panel): the footprint is a ledger read;
    # the purge is owner-bound and MEMBER at the policy layer — the handler adds
    # agents.deploy for cloud assets and agents.delete for an Agent (cleanup /
    # delete parity), so a plain transcript stays the member's to delete ----
    # ---- SE-047 evaluation-assets plan (N12 W-E5): every member prepares / edits /
    # reads plans; materialization is owner-bound, exact plan revision/hash and needs
    # the deploy permission; status is a ledger read; retry / cleanup of an operation
    # ride the same permission ----
    ("GET", "/api/assistant/architect/conversations/{conversation_id}/evaluation-plan"): MEMBER,
    ("POST", "/api/assistant/architect/conversations/{conversation_id}/evaluation-plan/prepare"):
        MEMBER,
    ("PUT", "/api/assistant/architect/conversations/{conversation_id}/evaluation-plan"): MEMBER,
    ("POST",
     "/api/assistant/architect/conversations/{conversation_id}/evaluation-plan/materialize"):
        PERM_AGENT_DEPLOY,
    ("GET", "/api/assistant/architect/conversations/{conversation_id}/evaluation-plan/operations/"
     "{operation_id}"): MEMBER,
    ("POST", "/api/assistant/architect/conversations/{conversation_id}/evaluation-plan/operations/"
     "{operation_id}/retry"): PERM_AGENT_DEPLOY,
    ("DELETE", "/api/assistant/architect/conversations/{conversation_id}/evaluation-plan/"
     "operations/{operation_id}/assets"): PERM_AGENT_DEPLOY,
    ("GET", "/api/assistant/architect/conversations/{conversation_id}/footprint"): MEMBER,
    ("DELETE", "/api/assistant/architect/conversations/{conversation_id}"): MEMBER,
    # ---- credential minting ----
    ("GET", "/api/apikeys"): MEMBER,
    ("POST", "/api/apikeys"): MEMBER,
    ("POST", "/api/apikeys/{key_id}/disable"): MEMBER,
    ("POST", "/api/apikeys/{key_id}/enable"): MEMBER,
    # the handler adds owner-or-admin on top (apikeys.require_key_owner)
    ("DELETE", "/api/apikeys/{key_id}"): MEMBER,
    # ---- chat: the member-facing invoke surface ----
    ("POST", "/api/chat/{agent_id}"): MEMBER,
    ("GET", "/api/chat/{agent_id}/history"): MEMBER,
    ("GET", "/api/chat/{agent_id}/memory"): MEMBER,
    ("GET", "/api/chat/{agent_id}/sessions"): MEMBER,
    ("POST", "/api/chat/{agent_id}/sessions/{session_id}/stop"): MEMBER,
    # N-ARCH-obs15 v2: reattach to a console turn (same member parity as the turn)
    ("GET", "/api/chat/{agent_id}/sessions/{session_id}/turns/{turn}/stream"): MEMBER,
    # ---- studio local-debug scaffolding: prod refuses these in the handler
    # (local_exec guard) regardless of role ----
    ("GET", "/api/conversations"): MEMBER,
    ("POST", "/api/conversations"): MEMBER,
    ("GET", "/api/conversations/{session_id}"): MEMBER,
    ("DELETE", "/api/conversations/{session_id}"): MEMBER,
    ("PUT", "/api/conversations/{session_id}/code"): MEMBER,
    ("GET", "/api/conversations/{session_id}/messages"): MEMBER,
    ("POST", "/api/conversations/{session_id}/messages"): MEMBER,
    ("POST", "/api/conversations/{session_id}/messages/stream"): MEMBER,
    # ---- local code execution (also refused outright in prod; see local_exec) ----
    ("POST", "/api/execute"): MEMBER,
    ("POST", "/api/execute/stream"): MEMBER,
    ("POST", "/api/fix-code/stream"): MEMBER,
    ("GET", "/api/generate-code/status"): MEMBER,
    # ---- registry skill sources become deployable code; the two staging
    # helpers ride the deploy permission because the create wizard needs them ----
    ("POST", "/api/agent-skills/import"): PERM_AGENT_DEPLOY,
    ("GET", "/api/registry/records"): MEMBER,
    ("POST", "/api/registry/records"): MEMBER,
    ("GET", "/api/registry/records/search"): MEMBER,
    # data-plane read (ListDiscoverableRegistryRecords) of the workspace registry
    ("GET", "/api/registry/records/discoverable"): MEMBER,
    # per-status counts (N17-obs267): the sidebar pending badge + the list's status tabs
    ("GET", "/api/registry/records/counts"): MEMBER,
    ("GET", "/api/registry/records/{record_id}"): MEMBER,
    # a data-plane read (GetAgentCard) of the agent's own runtime; no ARN from the client
    ("GET", "/api/registry/records/{record_id}/live-agent-card"): MEMBER,
    ("PUT", "/api/registry/records/{record_id}"): MEMBER,
    ("DELETE", "/api/registry/records/{record_id}"): MEMBER,
    # MEMBER for submit; approve/publish/reject/disable are admin-only inside the handler
    # (403 registry.approval_admin_only — N17-obs248, 2026-10-22)
    ("POST", "/api/registry/records/{record_id}/action"): MEMBER,
    ("POST", "/api/registry/records/{record_id}/reimport"): MEMBER,
    ("GET", "/api/registry/skills/capabilities"): MEMBER,
    # installs software on the server host
    ("POST", "/api/registry/skills/capabilities/git-install"): MEMBER,
    ("POST", "/api/registry/skills/import"): MEMBER,
    # fetches remote content (SSRF-guarded); staging-only, needed by deploy
    ("POST", "/api/registry/skills/inspect"): PERM_AGENT_DEPLOY,
    ("POST", "/api/registry/sync-defaults"): MEMBER,
    # WB-005: the selective gateway → MCP record import; same capability as
    # sync-defaults / registering an MCP server (parity with POST /api/registry/records)
    ("GET", "/api/registry/import-gateway/preview"): MEMBER,
    ("POST", "/api/registry/import-gateway"): MEMBER,
    ("GET", "/api/registry/attachables"): MEMBER,
    # ---- identity: Connections (credential providers) and gateway targets
    # bound to one. Reads are plain member; creating/deleting a vault entry or a
    # target is the member-grantable identity.manage permission ----
    ("GET", "/api/identity/connections"): MEMBER,
    ("GET", "/api/identity/connections/templates"): MEMBER,
    ("GET", "/api/identity/connections/oidc-sources"): MEMBER,
    ("GET", "/api/identity/connections/{kind}/{name}"): MEMBER,
    ("POST", "/api/identity/connections/oauth2"): PERM_IDENTITY_MANAGE,
    ("POST", "/api/identity/connections/api-key"): PERM_IDENTITY_MANAGE,
    ("DELETE", "/api/identity/connections/{kind}/{name}"): PERM_IDENTITY_MANAGE,
    ("GET", "/api/identity/gateway-targets"): MEMBER,
    ("POST", "/api/identity/gateway-targets"): PERM_IDENTITY_MANAGE,
    ("DELETE", "/api/identity/gateway-targets/{target_id}"): PERM_IDENTITY_MANAGE,
    # as_user (3LO): binding a consent into the vault or revoking one is the
    # member-grantable identity.grant permission; reading one's OWN grants
    # (the router scopes every query to the caller) is plain member
    ("POST", "/api/identity/oauth/complete"): PERM_IDENTITY_GRANT,
    ("GET", "/api/identity/grants"): MEMBER,
    ("GET", "/api/identity/grants/{connection}/status"): MEMBER,
    ("DELETE", "/api/identity/grants/{connection}"): PERM_IDENTITY_GRANT,
    # the gateway's Consent Portal is workspace infrastructure
    ("GET", "/api/identity/consent-portal"): MEMBER,
    # gateway-wide singleton whose create passes an operator-named IAM role to
    # AgentCore: an administrator decision, not member-grantable
    ("POST", "/api/identity/consent-portal"): ADMIN,
    ("DELETE", "/api/identity/consent-portal"): ADMIN,
    # ---- tools + demos: /tools/call can mutate external systems through a
    # gateway target, and the demos open billable cloud sessions ----
    ("GET", "/api/tools"): MEMBER,
    ("POST", "/api/tools/call"): MEMBER,
    ("POST", "/api/demos/code-interpreter"): MEMBER,
    ("GET", "/api/demos/browser/options"): MEMBER,
    ("POST", "/api/demos/browser"): MEMBER,  # takes a caller-supplied URL
    ("DELETE", "/api/demos/browser/{session_id}"): MEMBER,
    # ---- knowledge bases: reads and the retrieval playground stay open ----
    ("GET", "/api/knowledge-bases"): MEMBER,
    ("POST", "/api/knowledge-bases"): MEMBER,
    ("GET", "/api/knowledge-bases/{kb_id}"): MEMBER,
    ("PATCH", "/api/knowledge-bases/{kb_id}"): MEMBER,
    ("DELETE", "/api/knowledge-bases/{kb_id}"): MEMBER,
    ("POST", "/api/knowledge-bases/{kb_id}/files"): MEMBER,
    ("POST", "/api/knowledge-bases/{kb_id}/data-sources"): MEMBER,
    ("DELETE", "/api/knowledge-bases/{kb_id}/data-sources/{ds_id}"): MEMBER,
    ("GET", "/api/knowledge-bases/{kb_id}/data-sources/{ds_id}/documents"): MEMBER,
    ("GET", "/api/knowledge-bases/{kb_id}/data-sources/{ds_id}/ingestion-jobs"): MEMBER,
    ("POST", "/api/knowledge-bases/{kb_id}/data-sources/{ds_id}/sync"): MEMBER,
    ("POST", "/api/knowledge-bases/{kb_id}/query"): MEMBER,  # retrieval playground
    # ---- skill lab: local task assets/task sets + Runtime-backed jobs ----
    ("GET", "/api/skill-lab/status"): MEMBER,
    ("POST", "/api/skill-lab/task-assets"): MEMBER,
    ("GET", "/api/skill-lab/tasksets"): MEMBER,
    ("POST", "/api/skill-lab/tasksets"): MEMBER,
    ("GET", "/api/skill-lab/tasksets/{taskset_id}"): MEMBER,
    ("PUT", "/api/skill-lab/tasksets/{taskset_id}"): MEMBER,
    ("DELETE", "/api/skill-lab/tasksets/{taskset_id}"): MEMBER,
    ("GET", "/api/skill-lab/jobs"): MEMBER,
    ("POST", "/api/skill-lab/jobs"): MEMBER,
    ("GET", "/api/skill-lab/jobs/{job_id}"): MEMBER,
    ("POST", "/api/skill-lab/jobs/{job_id}/cancel"): MEMBER,
    ("DELETE", "/api/skill-lab/jobs/{job_id}"): MEMBER,
    ("POST", "/api/skill-lab/jobs/{job_id}/resume"): MEMBER,
    ("POST", "/api/skill-lab/jobs/{job_id}/publish"): MEMBER,
    ("POST", "/api/skill-lab/jobs/{job_id}/import-taskset"): MEMBER,
    ("POST", "/api/skill-lab/jobs/{job_id}/apply-expansion"): MEMBER,
    ("GET", "/api/skill-lab/jobs/{job_id}/train-summary"): MEMBER,
    ("GET", "/api/skill-lab/jobs/{job_id}/diff"): MEMBER,
    ("GET", "/api/skill-lab/jobs/{job_id}/log"): MEMBER,
    ("GET", "/api/skill-lab/jobs/{job_id}/results"): MEMBER,
    ("GET", "/api/skill-lab/jobs/{job_id}/artifacts"): MEMBER,
    ("GET", "/api/skill-lab/jobs/{job_id}/artifacts/raw"): MEMBER,
    # ---- governance (Gateway lifecycle, rate limits, target sync; Cedar Policy
    # does not exist in the CN build) ----
    ("GET", "/api/governance/gateways"): MEMBER,
    ("GET", "/api/governance/gateways/{gateway_id}"): MEMBER,
    ("POST", "/api/governance/gateways/{gateway_id}/manage"): MEMBER,
    ("DELETE", "/api/governance/gateways/{gateway_id}/manage"): MEMBER,
    ("GET", "/api/governance/gateways/{gateway_id}/rate-limits"): MEMBER,
    ("POST", "/api/governance/gateways/{gateway_id}/rate-limits"): MEMBER,
    ("PUT", "/api/governance/gateways/{gateway_id}/rate-limits/{rate_limit_id}"): MEMBER,
    ("DELETE", "/api/governance/gateways/{gateway_id}/rate-limits/{rate_limit_id}"): MEMBER,
    ("POST", "/api/governance/gateways/{gateway_id}/targets/{target_id}/synchronize"): MEMBER,
    # ---- CN evaluation / optimization (N12): runs invoke agents and start judge jobs ----
    ("GET", "/api/eval/datasets"): MEMBER,
    ("POST", "/api/eval/datasets"): MEMBER,
    ("POST", "/api/eval/datasets/upload"): MEMBER,
    ("GET", "/api/eval/datasets/cloud"): MEMBER,
    ("GET", "/api/eval/datasets/cloud/{cloud_id}"): MEMBER,
    ("DELETE", "/api/eval/datasets/cloud/{cloud_id}"): MEMBER,
    ("POST", "/api/eval/datasets/cloud/{cloud_id}/publish-version"): MEMBER,
    ("DELETE", "/api/eval/datasets/cloud/{cloud_id}/versions/{version}"): MEMBER,
    ("PUT", "/api/eval/datasets/{dataset_id}"): MEMBER,
    ("DELETE", "/api/eval/datasets/{dataset_id}"): MEMBER,
    ("POST", "/api/eval/datasets/{dataset_id}/sync-to-aws"): MEMBER,
    ("POST", "/api/eval/datasets/{dataset_id}/publish-version"): MEMBER,
    # console V2 data processing: observed sessions → local datasets (reads + ledger writes)
    ("POST", "/api/eval/datasets/from-sessions"): MEMBER,
    ("GET", "/api/eval/pipelines"): MEMBER,
    ("POST", "/api/eval/pipelines"): MEMBER,
    ("GET", "/api/eval/pipelines/{pipeline_id}"): MEMBER,
    ("PUT", "/api/eval/pipelines/{pipeline_id}"): MEMBER,
    ("DELETE", "/api/eval/pipelines/{pipeline_id}"): MEMBER,
    ("POST", "/api/eval/pipelines/{pipeline_id}/run"): MEMBER,
    ("GET", "/api/eval/agents/{agent_id}/log-streams"): MEMBER,
    ("GET", "/api/eval/log-sessions"): MEMBER,
    ("GET", "/api/eval/log-groups"): MEMBER,
    ("GET", "/api/eval/log-services"): MEMBER,
    ("GET", "/api/eval/evaluators"): MEMBER,
    ("POST", "/api/eval/evaluators"): MEMBER,
    ("GET", "/api/eval/evaluators/{evaluator_id}"): MEMBER,
    ("PUT", "/api/eval/evaluators/{evaluator_id}"): MEMBER,
    ("DELETE", "/api/eval/evaluators/{evaluator_id}"): MEMBER,
    ("GET", "/api/eval/queue"): MEMBER,
    ("GET", "/api/eval/runs"): MEMBER,
    ("POST", "/api/eval/runs"): PERM_EVAL_RUN,
    # agent scorecards + two-run compare: ledger reads (compare adds the two runs'
    # results reads, as `/results` does) — upstream #289
    ("GET", "/api/eval/scorecard"): MEMBER,
    ("GET", "/api/eval/runs/compare"): MEMBER,
    ("GET", "/api/eval/runs/{run_id}"): MEMBER,
    ("GET", "/api/eval/runs/{run_id}/results"): MEMBER,
    ("POST", "/api/eval/runs/{run_id}/stop"): PERM_EVAL_RUN,
    ("POST", "/api/eval/runs/{run_id}/recheck"): PERM_EVAL_RUN,
    # starts a new run of the same scope — the same grant as POST /runs (upstream #289)
    ("POST", "/api/eval/runs/{run_id}/rerun"): PERM_EVAL_RUN,
    ("GET", "/api/eval/runs/{run_id}/recommendation-inputs"): MEMBER,
    ("GET", "/api/eval/runs/{run_id}/recommendations"): MEMBER,
    # starts a recommendation job (judge / reflection calls) — same revocable grant as a run
    ("POST", "/api/eval/runs/{run_id}/recommendations"): PERM_EVAL_RUN,
    # re-publishes the agent (a new version) — the same grant as a redeploy
    ("POST", "/api/eval/runs/{run_id}/recommendations/{rec_id}/accept"): PERM_AGENT_DEPLOY,
    ("POST", "/api/eval/runs/{run_id}/recommendations/{rec_id}/revise"): PERM_AGENT_DEPLOY,
    ("DELETE", "/api/eval/runs/{run_id}"): PERM_EVAL_RUN,
    # online evaluation configs: create/resume start judge calls on live traffic, the
    # same cost class as starting a run; list/detail/results are reads
    ("GET", "/api/eval/online"): MEMBER,
    ("POST", "/api/eval/online"): PERM_EVAL_RUN,
    ("GET", "/api/eval/online/{config_id}"): MEMBER,
    ("PATCH", "/api/eval/online/{config_id}"): MEMBER,
    ("POST", "/api/eval/online/{config_id}/pause"): MEMBER,
    ("POST", "/api/eval/online/{config_id}/resume"): PERM_EVAL_RUN,
    ("DELETE", "/api/eval/online/{config_id}"): MEMBER,
    ("GET", "/api/eval/online/{config_id}/results"): MEMBER,
    ("GET", "/api/eval/online/{config_id}/reports"): MEMBER,
    ("POST", "/api/eval/online/{config_id}/reports"): PERM_EVAL_RUN,
    ("GET", "/api/eval/online/{config_id}/reports/{batch_id}"): MEMBER,
    # the recommendation providers a picker may offer (no AWS call)
    ("GET", "/api/eval/recommendation-providers"): MEMBER,
    # canaries provision real AgentCore runtime versions + endpoints
    ("GET", "/api/runtime-canaries"): MEMBER,
    ("POST", "/api/runtime-canaries"): MEMBER,
    ("GET", "/api/runtime-canaries/{canary_id}"): MEMBER,
    ("POST", "/api/runtime-canaries/{canary_id}/action"): MEMBER,
    # ---- end CN evaluation / optimization (N12) ----
    # ---- self-hosted Policy, Global model (/api/policy-engines, mounted only with the
    # `policy` feature; contract docs/cn/interfaces/policy-engines.md). Engine / policy writes
    # are member-grantable (perm:policy.manage); a gateway attachment changes the gateway's
    # governance posture and is an administrator's. ----
    ("GET", "/api/policy-engines"): MEMBER,
    ("POST", "/api/policy-engines"): PERM_POLICY_MANAGE,
    ("GET", "/api/policy-engines/{engine_id}"): MEMBER,
    ("PUT", "/api/policy-engines/{engine_id}"): PERM_POLICY_MANAGE,
    ("DELETE", "/api/policy-engines/{engine_id}"): PERM_POLICY_MANAGE,
    ("GET", "/api/policy-engines/{engine_id}/status"): MEMBER,
    ("GET", "/api/policy-engines/{engine_id}/entities"): MEMBER,
    ("GET", "/api/policy-engines/{engine_id}/changes"): MEMBER,
    ("GET", "/api/policy-engines/{engine_id}/decisions"): MEMBER,
    ("POST", "/api/policy-engines/{engine_id}/validate"): MEMBER,  # pure evaluation
    ("POST", "/api/policy-engines/{engine_id}/format"): MEMBER,
    # dry-run; `record: true` additionally requires perm:policy.manage in the handler
    ("POST", "/api/policy-engines/{engine_id}/simulate"): MEMBER,
    ("GET", "/api/policy-engines/{engine_id}/policies"): MEMBER,
    ("POST", "/api/policy-engines/{engine_id}/policies"): PERM_POLICY_MANAGE,
    ("GET", "/api/policy-engines/{engine_id}/policies/{policy_id}"): MEMBER,
    ("GET", "/api/policy-engines/{engine_id}/policies/{policy_id}/versions"): MEMBER,
    ("PUT", "/api/policy-engines/{engine_id}/policies/{policy_id}"): PERM_POLICY_MANAGE,
    ("POST", "/api/policy-engines/{engine_id}/policies/{policy_id}/publish"): PERM_POLICY_MANAGE,
    ("PUT", "/api/policy-engines/{engine_id}/policies/{policy_id}/enforcement-mode"):
        PERM_POLICY_MANAGE,
    ("POST", "/api/policy-engines/{engine_id}/policies/{policy_id}/retire"): PERM_POLICY_MANAGE,
    ("DELETE", "/api/policy-engines/{engine_id}/policies/{policy_id}"): PERM_POLICY_MANAGE,
    # Gateway.policyEngineConfiguration: which engine decides a gateway's calls, in which mode
    ("GET", "/api/governance/gateways/{gateway_id}/policy-engine"): MEMBER,
    ("PUT", "/api/governance/gateways/{gateway_id}/policy-engine"): ADMIN,
    ("DELETE", "/api/governance/gateways/{gateway_id}/policy-engine"): ADMIN,
    # the retired /api/policies surface answers 410 policy.moved for one release
    ("GET", "/api/policies"): MEMBER,
    ("POST", "/api/policies"): MEMBER,
    ("GET", "/api/policies/{rest}"): MEMBER,
    ("POST", "/api/policies/{rest}"): MEMBER,
    ("PUT", "/api/policies/{rest}"): MEMBER,
    ("DELETE", "/api/policies/{rest}"): MEMBER,
    # ---- end D20 self-hosted Policy ----
    # ---- read-only consoles ----
    ("GET", "/api/overview"): MEMBER,
    ("GET", "/api/overview/online-quality"): MEMBER,  # N12 E2-3 (baseline row)
    ("GET", "/api/capabilities"): MEMBER,
    # ---- hub-global notices: members read published snapshots only ----
    ("GET", "/api/announcements"): MEMBER,
    ("GET", "/api/announcements/manage"): ADMIN,
    ("GET", "/api/announcements/{announcement_id}"): ADMIN,
    ("POST", "/api/announcements"): ADMIN,
    ("PUT", "/api/announcements/{announcement_id}"): ADMIN,
    ("POST", "/api/announcements/{announcement_id}/publish"): ADMIN,
    ("POST", "/api/announcements/{announcement_id}/unpublish"): ADMIN,
    ("DELETE", "/api/announcements/{announcement_id}"): ADMIN,
    # ---- hub-global videos: only published snapshots reach members ----
    ("GET", "/api/videos"): MEMBER,
    ("GET", "/api/videos/manage"): ADMIN,
    ("GET", "/api/videos/manage/{video_id}"): ADMIN,
    ("POST", "/api/videos/manage"): ADMIN,
    ("PUT", "/api/videos/manage/{video_id}"): ADMIN,
    ("POST", "/api/videos/manage/{video_id}/publish"): ADMIN,
    ("POST", "/api/videos/manage/{video_id}/unpublish"): ADMIN,
    ("DELETE", "/api/videos/manage/{video_id}"): ADMIN,
    ("GET", "/api/memory/overview"): MEMBER,
    ("GET", "/api/memory/actors"): MEMBER,
    ("GET", "/api/memory/events"): MEMBER,
    ("GET", "/api/memory/extraction-jobs"): MEMBER,
    ("GET", "/api/memory/namespaces"): MEMBER,
    ("GET", "/api/memory/records"): MEMBER,
    ("POST", "/api/memory/records/search"): MEMBER,  # a search, not a mutation
    # N10v2-bug6: irreversible; the handler further requires admin or the owner of the
    # agent the record's actor is scoped to
    ("DELETE", "/api/memory/records/{record_id}"): PERM_MEMORY_MANAGE,
    ("GET", "/api/memory/sessions"): MEMBER,
    ("GET", "/api/memory/resources"): MEMBER,
    # creates a billable AgentCore Memory resource, registered as managed
    ("POST", "/api/memory/resources"): PERM_MEMORY_MANAGE,
    ("GET", "/api/memory/resources/{memory_id}"): MEMBER,  # managed memories only
    # description / event expiry; managed memories only
    ("PUT", "/api/memory/resources/{memory_id}"): PERM_MEMORY_MANAGE,
    # irreversible (every event and record goes); managed memories only
    ("DELETE", "/api/memory/resources/{memory_id}"): PERM_MEMORY_MANAGE,
    # brings a memory the platform did not create under workspace management
    ("POST", "/api/memory/resources/{memory_id}/adopt"): ADMIN,
    ("GET", "/api/observability/dashboard"): MEMBER,
    ("GET", "/api/observability/sessions"): MEMBER,
    ("GET", "/api/observability/sessions/{session_id}"): MEMBER,
    ("GET", "/api/observability/sessions/{session_id}/transcript"): MEMBER,
    # a read; POST for the per-session start times (upstream #287) — the handler adds
    # the chat-session ownership rule like the transcript route
    ("POST", "/api/observability/session-previews"): MEMBER,
    ("POST", "/api/observability/sessions/{session_id}/evaluate"): MEMBER,  # SCORE NOW (N12)
    ("GET", "/api/observability/traces"): MEMBER,
    ("GET", "/api/observability/traces/{trace_id}"): MEMBER,
    # Chat Inspector trace rail — the handler adds the chat-session ownership rule
    ("GET", "/api/traces/{session_id}"): MEMBER,
    # ---- console account management ----
    ("GET", "/api/users"): ADMIN,
    ("GET", "/api/users/stats"): ADMIN,
    ("PATCH", "/api/users/{user_id}"): ADMIN,
    ("DELETE", "/api/users/{user_id}"): ADMIN,
    # ---- workspace administration (hub-global, see WORKSPACE_EXEMPT) ----
    ("GET", "/api/workspaces"): MEMBER,  # returns only the caller's workspaces
    ("POST", "/api/workspaces"): ADMIN,
    # The hub's own account/role, for a spoke stack's parameters.
    ("GET", "/api/workspaces/hub-identity"): ADMIN,
    # Probes an AssumeRole before anything is recorded; writes nothing.
    ("POST", "/api/workspaces/preflight"): ADMIN,
    ("PATCH", "/api/workspaces/{workspace_id}"): ADMIN,
    ("DELETE", "/api/workspaces/{workspace_id}"): ADMIN,
    ("POST", "/api/workspaces/{workspace_id}/purge"): ADMIN,
    ("POST", "/api/workspaces/{workspace_id}/bootstrap"): ADMIN,
    ("GET", "/api/workspaces/{workspace_id}/bootstrap"): ADMIN,
    ("GET", "/api/workspaces/{workspace_id}/grants"): ADMIN,
    # Bulk grant/revoke from the workspace's side (per-user replacement stays on
    # PATCH /api/users/{id}); both write only `user_workspaces`.
    ("PUT", "/api/workspaces/{workspace_id}/grants"): ADMIN,
    # Shared vs isolated member resource visibility (services/visibility.py).
    ("GET", "/api/workspaces/{workspace_id}/settings/visibility"): ADMIN,
    ("PUT", "/api/workspaces/{workspace_id}/settings/visibility"): ADMIN,
    # ---- model registry (D21, plan §3/§9.1): hub-global, administrators only — no
    # member-grantable `models.manage` permission in the pilot (user ruling). Probe /
    # credential routes answer 501 until M1-b registers its hooks. ----
    # N17-obs203 (user decision 2026-10-21): model providers are OWNER-BOUND resources —
    # every signed-in member may register their own (`scope: personal`) and read / change /
    # delete it, its models, key and probes; the handler adds the visibility 404 and the
    # creator-or-admin 403 (`model_registry.require_provider_mutable`), and only an
    # administrator may set `scope: workspace` (share) or touch another member's row. The
    # journal and the per-workspace flag matrix stay an administrator's.
    ("GET", "/api/settings/model-providers"): MEMBER,
    ("POST", "/api/settings/model-providers"): MEMBER,
    ("GET", "/api/settings/model-providers/changes"): ADMIN,
    ("PUT", "/api/settings/model-providers/workspaces/{workspace_id}/models"): ADMIN,
    ("GET", "/api/settings/model-providers/{provider_id}"): MEMBER,
    ("PUT", "/api/settings/model-providers/{provider_id}"): MEMBER,
    ("DELETE", "/api/settings/model-providers/{provider_id}"): MEMBER,
    ("POST", "/api/settings/model-providers/{provider_id}/credential"): MEMBER,
    ("POST", "/api/settings/model-providers/{provider_id}/probe"): MEMBER,
    ("POST", "/api/settings/model-providers/{provider_id}/models"): MEMBER,
    ("PUT", "/api/settings/model-providers/{provider_id}/models/{model_ref}"): MEMBER,
    ("DELETE", "/api/settings/model-providers/{provider_id}/models/{model_ref}"): MEMBER,
    ("POST", "/api/settings/model-providers/{provider_id}/models/{model_ref}/probe"): MEMBER,
    # ---- model roles (r15-e, upstream #275 subset; services/model_roles.py): hub-global.
    # The effective model per role is every picker's pre-selection (any signed-in role);
    # an override changes a platform default for every workspace and member at once. ----
    ("GET", "/api/model-defaults"): MEMBER,
    ("PUT", "/api/settings/model-roles/{role}"): ADMIN,
    ("DELETE", "/api/settings/model-roles/{role}"): ADMIN,
}

# Hub-global route prefixes: nothing under them operates inside a workspace.
HUB_GLOBAL_PREFIXES = (
    "/api/auth", "/api/users", "/api/workspaces", "/api/announcements", "/api/videos",
    "/api/settings",  # D21: the model registry is hub-global (per-workspace flags are rows)
    "/api/model-defaults",  # r15-e: the model roles are one hub-global ledger table
)


def is_hub_global(path_format: str) -> bool:
    """Whether a path sits under a hub-global prefix.

    Matched on path segments, not raw string prefixes: a bare `startswith` would
    also swallow a future `/api/userspace` and silently exempt it from the
    workspace boundary, which is the one direction that fails open.
    """
    return any(
        path_format == prefix or path_format.startswith(f"{prefix}/")
        for prefix in HUB_GLOBAL_PREFIXES
    )

# The routes that are NOT workspace-scoped. Listed rather than derived so the
# posture is auditable entry by entry; `tests/test_route_policy.py` asserts the
# set against the rule "PUBLIC (no identity to resolve a workspace for) or
# hub-global prefix" in both directions, so it can neither rot nor grow quietly.
WORKSPACE_EXEMPT: frozenset[tuple[str, str]] = frozenset(
    {
        ("GET", "/api/health"),
        ("GET", "/api/docs"),
        ("GET", "/api/openapi.json"),
        ("GET", "/api/auth/status"),
        ("POST", "/api/auth/login"),
        ("POST", "/api/auth/register"),
        ("POST", "/api/auth/logout"),
        ("GET", "/api/announcements"),
        ("GET", "/api/announcements/manage"),
        ("GET", "/api/announcements/{announcement_id}"),
        ("POST", "/api/announcements"),
        ("PUT", "/api/announcements/{announcement_id}"),
        ("POST", "/api/announcements/{announcement_id}/publish"),
        ("POST", "/api/announcements/{announcement_id}/unpublish"),
        ("DELETE", "/api/announcements/{announcement_id}"),
        ("GET", "/api/videos"),
        ("GET", "/api/videos/manage"),
        ("GET", "/api/videos/manage/{video_id}"),
        ("POST", "/api/videos/manage"),
        ("PUT", "/api/videos/manage/{video_id}"),
        ("POST", "/api/videos/manage/{video_id}/publish"),
        ("POST", "/api/videos/manage/{video_id}/unpublish"),
        ("DELETE", "/api/videos/manage/{video_id}"),
        ("GET", "/api/users"),
        ("GET", "/api/users/stats"),
        ("PATCH", "/api/users/{user_id}"),
        ("DELETE", "/api/users/{user_id}"),
        ("GET", "/api/workspaces"),
        ("POST", "/api/workspaces"),
        ("GET", "/api/workspaces/hub-identity"),
        # Tests credentials for a workspace that does not exist yet, so there is
        # no workspace to resolve — the candidate comes from the body.
        ("POST", "/api/workspaces/preflight"),
        ("PATCH", "/api/workspaces/{workspace_id}"),
        ("DELETE", "/api/workspaces/{workspace_id}"),
        # Deletes the target's own scoped rows; the target is the path parameter,
        # and the caller's own selection is irrelevant to it.
        ("POST", "/api/workspaces/{workspace_id}/purge"),
        # Operates ON a workspace that is not usable yet; the target is the path
        # parameter, not the caller's X-Workspace header.
        ("POST", "/api/workspaces/{workspace_id}/bootstrap"),
        ("GET", "/api/workspaces/{workspace_id}/bootstrap"),
        ("GET", "/api/workspaces/{workspace_id}/grants"),
        ("PUT", "/api/workspaces/{workspace_id}/grants"),
        # The target is the path parameter, like grants.
        ("GET", "/api/workspaces/{workspace_id}/settings/visibility"),
        ("PUT", "/api/workspaces/{workspace_id}/settings/visibility"),
        # D21: the model registry is one hub-global table; the per-workspace enable flags
        # name their workspace in the path, never through the caller's selection.
        ("GET", "/api/settings/model-providers"),
        ("POST", "/api/settings/model-providers"),
        ("GET", "/api/settings/model-providers/changes"),
        ("PUT", "/api/settings/model-providers/workspaces/{workspace_id}/models"),
        ("GET", "/api/settings/model-providers/{provider_id}"),
        ("PUT", "/api/settings/model-providers/{provider_id}"),
        ("DELETE", "/api/settings/model-providers/{provider_id}"),
        ("POST", "/api/settings/model-providers/{provider_id}/credential"),
        ("POST", "/api/settings/model-providers/{provider_id}/probe"),
        ("POST", "/api/settings/model-providers/{provider_id}/models"),
        ("PUT", "/api/settings/model-providers/{provider_id}/models/{model_ref}"),
        ("DELETE", "/api/settings/model-providers/{provider_id}/models/{model_ref}"),
        ("POST", "/api/settings/model-providers/{provider_id}/models/{model_ref}/probe"),
        # r15-e: model roles — one hub-global ledger table, no workspace to resolve
        ("GET", "/api/model-defaults"),
        ("PUT", "/api/settings/model-roles/{role}"),
        ("DELETE", "/api/settings/model-roles/{role}"),
    }
)

# Routers whose classification was extrapolated from the signed-off principle
# rather than reviewed route by route. Since the 2026-08-11 amendment they are
# all plain MEMBER anyway; the list survives as a pointer to what to re-examine
# if per-user data partitioning ever tightens the posture again.
UNREVIEWED_PREFIXES = (
    "/api/conversations",
    "/api/observability/",
)


def required_role(method: str, path_format: str) -> str | None:
    """The role a route demands, or None when it is not in the table."""
    # Starlette answers HEAD from the GET handler; authorize it the same way.
    lookup = "GET" if method == "HEAD" else method
    return ROUTE_POLICY.get((lookup, path_format))


def is_workspace_scoped(method: str, path_format: str) -> bool:
    """Whether this route operates inside one workspace environment."""
    lookup = "GET" if method == "HEAD" else method
    return (lookup, path_format) not in WORKSPACE_EXEMPT


def enforce_route_policy(request: Request) -> None:
    """App-level dependency enforcing ROUTE_POLICY.

    A dependency rather than middleware because `scope["route"]` is only set once
    the router has matched, so this sees the exact `path_format` instead of
    re-implementing path matching.
    """
    # N17-obs284 rev-r26 N1: this is the only route-level guard, so the /api test keys on
    # the MATCHED route's path format (what the table is keyed on). The raw `url.path`
    # carries any ASGI `root_path` that routing strips, which would skip enforcement for
    # every route. `app_path` (url.path minus root_path) is the fallback when no route is set.
    route: Any = request.scope.get("route")
    path_format = getattr(route, "path_format", None) or app_path(request)
    if path_format != API_PREFIX and not path_format.startswith(f"{API_PREFIX}/"):
        return  # /v1 carries its own X-Api-Key auth; static and redirects are open
    if request.method == "OPTIONS":
        return  # CORS preflight never reaches a handler
    role = required_role(request.method, path_format)
    if role is None:
        # Default-deny: refuse rather than serve an unclassified route.
        raise AppError(
            "auth.route_unclassified",
            f"{request.method} {path_format} is missing from ROUTE_POLICY "
            "(app/core/route_policy.py) — classify it before serving it.",
            status_code=500,
        )
    if role == ADMIN:
        require_admin(request)
    elif role.startswith(_PERM_PREFIX):
        require_permission(request, role.removeprefix(_PERM_PREFIX))
    if is_workspace_scoped(request.method, path_format):
        # Resolved here rather than per handler: enforcement must not depend on a
        # router remembering to declare the dependency. Handlers read the result
        # back through `require_workspace`.
        resolve_workspace(request)
