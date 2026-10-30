"""Console authorization: one declarative table for every `/api` route.

Why a table instead of per-route `Depends(require_admin)`:

* a reviewer can audit the whole authorization posture in one file rather than by
  grepping 19 routers;
* it is **default-deny** — a route with no entry raises instead of silently
  serving, so a newly added endpoint cannot ship unclassified. `tests/
  test_route_policy.py` enumerates the live routes and fails on drift in both
  directions.

Roles: `ADMIN` requires an administrator identity (a steward passes the structure
half, `ADMIN_SPLIT` at the end of this file, projects §11.4); `MEMBER` requires only a live
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
Amended by river 2026-08-10: starting evaluation/insights runs
(`POST /api/eval/runs`) is member-grantable via `perm:eval.run` on the same
default-granted terms — it invokes agents (member parity with Chat) and creates
billable AWS eval jobs, which revocation can still shut off per user.
Amended by river 2026-08-11: **the whole console is member-reachable except user
management** — every route that used to demand `ADMIN` (registry writes, knowledge
bases, governance, evaluation datasets/evaluators, experiments, canaries, API keys,
studio local exec, tools/demos, prices refresh) is now `MEMBER`; only `/api/users*`
still requires an administrator. The `perm:*` entries keep their revocation
semantics unchanged.
Amended 2026-10-04 (issue #55): creating, editing and deleting AgentCore Memory
resources (`/api/memory/resources` writes) is member-grantable via
`perm:memory.manage` on the same default-granted, revocable terms — DeleteMemory
is irreversible — and those routes reach only memories the workspace manages.
Adopting an existing account memory into management is `ADMIN`.
Amended 2026-10-08: the architect assistant's evaluation-assets routes (materialize,
retry, Lambda revision review, cleanup) are member-grantable via `perm:agents.deploy`
— the same permission that approves a proposal into a deployed Agent — instead of
`ADMIN`; clearing a conversation with assets needs the matching `perm:` keys.
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
* Invoking an agent is deliberately `MEMBER` (`/api/agents/{id}/invoke`,
  `/api/registry/a2a-demo`): it is the same capability the Chat console gives
  every member, so gating it while Chat stays open would protect nothing.

* The account-free `/share/*` routes (T13) sit outside `/api` — no console session,
  no `X-Workspace` — and are classified here as `PUBLIC` + hub-global so the drift
  test covers them; `enforce_route_policy` itself only runs for `/api`.

There is deliberately **no** setting that disables this table. A flag that turns
authorization off is the vulnerability; fixing a misclassification means editing
the entry.
"""

from typing import Any

from fastapi import Request
from sqlalchemy import text

from app.core.errors import AppError
from app.routers.auth import require_admin, require_identity, require_permission
from app.routers.workspaces import TIER_PROD, resolve_workspace
from app.services.audit import record_audit_event, record_refusal

ADMIN = "admin"
MEMBER = "member"
PUBLIC = "public"

# Member-grantable permissions (auth.AGENT_PERMISSIONS keys), default granted,
# revocable per user in the Users console.
PERM_AGENT_DEPLOY = "perm:agents.deploy"
PERM_AGENT_IMPORT = "perm:agents.import"
PERM_AGENT_DELETE = "perm:agents.delete"
PERM_AGENT_CONVERT = "perm:agents.convert"
PERM_EVAL_RUN = "perm:eval.run"
# T19/T21 — the release surface. `request` is default-granted to members and operators
# (asking for a release is not privileged); `approve` is granted to operators and admins
# only, so nobody can wave their own change into production.
PERM_PROMOTION_REQUEST = "perm:promotion.request"
PERM_PROMOTION_APPROVE = "perm:promotion.approve"
PERM_IDENTITY_MANAGE = "perm:identity.manage"
# Project keys (design §3.1, §5.3): inside a project, participation and ordinary writes
# are permissions of the caller's project role, not "any member".
PERM_PROJECT_USE = "perm:project.use"
PERM_PROJECT_WRITE = "perm:project.write"
# Workspace-shared infrastructure (governance, registry MCP/skill records, gateway
# targets…): project.write while the workspace holds one active project, admin-only
# once it holds several — one project must not reshape what another runs on.
INFRA = "infra"
INFRA_IDENTITY_MANAGE = "infra+perm:identity.manage"
PERM_IDENTITY_GRANT = "perm:identity.grant"
PERM_MEMORY_MANAGE = "perm:memory.manage"
# Agent-DLC: who may edit the ruler, sign it, admit samples, calibrate a judge, approve
# a waiver and sign a release (docs/agent-dlc-design.md §8).
PERM_CRITERIA_MANAGE = "perm:criteria.manage"
PERM_CRITERIA_SIGN = "perm:criteria.sign"
PERM_GOLDEN_ADMIT = "perm:golden.admit"
PERM_JUDGE_CALIBRATE = "perm:judge.calibrate"
PERM_WAIVER_APPROVE = "perm:waiver.approve"
PERM_RELEASE_SIGN = "perm:release.sign"
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
    # the signed-in account changes its OWN password (current one required)
    ("POST", "/api/auth/password"): MEMBER,
    # ---- agents: lifecycle changes are member-grantable permissions (default
    # granted, revocable per user); reads and invoke are plain member ----
    ("GET", "/api/agents"): MEMBER,
    ("POST", "/api/agents"): PERM_AGENT_DEPLOY,
    # the create flow's project picker: server-filtered to the caller's eligible projects
    ("GET", "/api/agents/eligible-projects"): MEMBER,
    ("GET", "/api/agents/discovery"): MEMBER,
    ("POST", "/api/agents/discovery/import"): PERM_AGENT_IMPORT,
    # BYOC artifact staging is one half of a deploy, so it carries deploy perms
    ("POST", "/api/agents/uploads"): PERM_AGENT_DEPLOY,
    ("GET", "/api/agents/uploads/{upload_id}"): MEMBER,
    ("GET", "/api/agents/{agent_id}"): MEMBER,
    ("GET", "/api/agents/{agent_id}/suggested-questions"): MEMBER,  # cached Converse call
    ("GET", "/api/agents/{agent_id}/identity"): MEMBER,
    ("GET", "/api/agents/{agent_id}/mcp"): MEMBER,  # live Gateway read-back
    ("GET", "/api/agents/{agent_id}/versions"): MEMBER,  # read-only AWS view
    ("GET", "/api/agents/{agent_id}/source"): MEMBER,  # read-only code / harness config
    ("GET", "/api/agents/{agent_id}/runtime-logs"): MEMBER,  # read-only AWS view
    ("GET", "/api/agents/{agent_id}/conversions"): MEMBER,  # ledger read: runtime twins
    ("DELETE", "/api/agents/{agent_id}"): PERM_AGENT_DELETE,
    ("POST", "/api/agents/{agent_id}/convert"): PERM_AGENT_CONVERT,
    ("POST", "/api/agents/{agent_id}/redeploy"): PERM_AGENT_DEPLOY,
    ("GET", "/api/agents/{agent_id}/snapshots"): MEMBER,  # ledger read (T18)
    ("GET", "/api/agents/{agent_id}/snapshots/diff"): MEMBER,
    ("GET", "/api/agents/{agent_id}/snapshots/{seq}"): MEMBER,
    ("POST", "/api/agents/{agent_id}/snapshots/{seq}/rollback"): PERM_AGENT_DEPLOY,
    ("POST", "/api/agents/{agent_id}/inbound-auth"): PERM_AGENT_DEPLOY,
    ("POST", "/api/agents/{agent_id}/invoke"): PERM_PROJECT_USE,  # parity with Chat
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
    ("DELETE", "/api/system-agents/{preset_key}"): ADMIN,
    # ---- architect assistant (SE-039): discussion with the preset is member
    # (parity with Chat); approving a proposal deploys a NEW agent and rides the
    # same permission as POST /api/agents. Conversations are owner-bound on top. ----
    ("GET", "/api/assistant/architect"): MEMBER,
    ("GET", "/api/assistant/architect/conversations"): MEMBER,
    ("POST", "/api/assistant/architect/conversations"): PERM_PROJECT_USE,
    ("GET", "/api/assistant/architect/conversations/{conversation_id}"): MEMBER,
    # sharing: the owner (amended 2026-10-08) or an administrator — the handler refuses
    # a non-owner member; nobody can reach a private conversation it does not own
    ("PUT", "/api/assistant/architect/conversations/{conversation_id}/sharing"): PERM_PROJECT_USE,
    ("POST", "/api/assistant/architect/conversations/{conversation_id}/catalog"): PERM_PROJECT_USE,
    ("PUT",
     "/api/assistant/architect/conversations/{conversation_id}/preparation"): PERM_PROJECT_USE,
    ("POST", "/api/assistant/architect/conversations/{conversation_id}/preparation/skills"):
        PERM_AGENT_DEPLOY,
    ("POST", "/api/assistant/architect/conversations/{conversation_id}/turns"): PERM_PROJECT_USE,
    ("PUT", "/api/assistant/architect/conversations/{conversation_id}/proposal"): PERM_PROJECT_USE,
    ("POST",
     "/api/assistant/architect/conversations/{conversation_id}/proposal/reject"): PERM_PROJECT_USE,
    ("POST", "/api/assistant/architect/conversations/{conversation_id}/proposal/approve"):
        PERM_AGENT_DEPLOY,
    # ---- SE-047 evaluation-assets plan: preparing/editing the private plan is
    # discussion (member, owner-bound); materializing creates AWS evaluators + a
    # Lambda + IAM → the deploy permission (amended 2026-10-08: was admin), owner-
    # bound, exact plan revision/hash; status is a ledger read; retry / review /
    # cleanup of an operation ride the same permission ----
    ("GET", "/api/assistant/architect/conversations/{conversation_id}/evaluation-plan"): MEMBER,
    ("POST", "/api/assistant/architect/conversations/{conversation_id}/evaluation-plan/prepare"):
        PERM_PROJECT_USE,
    ("PUT",
     "/api/assistant/architect/conversations/{conversation_id}/evaluation-plan"): PERM_PROJECT_USE,
    ("POST",
     "/api/assistant/architect/conversations/{conversation_id}/evaluation-plan/materialize"):
        PERM_AGENT_DEPLOY,
    ("GET", "/api/assistant/architect/conversations/{conversation_id}/evaluation-plan/operations/"
     "{operation_id}"): MEMBER,
    ("POST", "/api/assistant/architect/conversations/{conversation_id}/evaluation-plan/operations/"
     "{operation_id}/retry"): PERM_AGENT_DEPLOY,
    # SE-049: reviewed recovery of the Lambda first-initialization RevisionId conflict —
    # deploy permission + owner, reads CloudTrail / Lambda, writes only the ledger
    # review + requeue
    ("POST", "/api/assistant/architect/conversations/{conversation_id}/evaluation-plan/operations/"
     "{operation_id}/lambda-revision-review"): PERM_AGENT_DEPLOY,
    ("DELETE", "/api/assistant/architect/conversations/{conversation_id}/evaluation-plan/"
     "operations/{operation_id}/assets"): PERM_AGENT_DEPLOY,
    # ---- clearing a conversation (History panel): the footprint is a ledger read;
    # the purge is owner-bound and MEMBER at the policy layer — the handler adds
    # agents.deploy for cloud assets and agents.delete for an Agent (cleanup /
    # delete parity), so a plain transcript stays the member's to delete ----
    ("GET", "/api/assistant/architect/conversations/{conversation_id}/footprint"): MEMBER,
    ("DELETE", "/api/assistant/architect/conversations/{conversation_id}"): PERM_PROJECT_USE,
    # AC-3 (assistant-context C2, D-4): the owner rebuilds the conversation's
    # confirmed-items digest — one model call and a ledger write, no AWS resource;
    # owner-bound in the handler (a shared conversation's collaborators get 403)
    ("POST", "/api/assistant/architect/conversations/{conversation_id}/digest/regenerate"):
        PERM_PROJECT_USE,
    # ---- credential minting ----
    ("GET", "/api/apikeys"): MEMBER,
    ("POST", "/api/apikeys"): PERM_PROJECT_WRITE,
    ("POST", "/api/apikeys/{key_id}/disable"): PERM_PROJECT_WRITE,
    ("POST", "/api/apikeys/{key_id}/enable"): PERM_PROJECT_WRITE,
    ("PATCH", "/api/apikeys/{key_id}"): PERM_PROJECT_WRITE,  # scope / expiry / rate limit (T16)
    ("GET", "/api/apikeys/{key_id}/usage"): MEMBER,
    # ---- chat: the member-facing invoke surface ----
    ("POST", "/api/chat/{agent_id}"): PERM_PROJECT_USE,
    ("GET", "/api/chat/{agent_id}/history"): MEMBER,
    ("GET", "/api/chat/{agent_id}/memory"): MEMBER,
    ("GET", "/api/chat/{agent_id}/sessions"): MEMBER,
    ("POST", "/api/chat/{agent_id}/sessions/{session_id}/stop"): PERM_PROJECT_USE,
    # ---- studio local-debug scaffolding: prod refuses these in the handler
    # (local_exec guard) regardless of role ----
    ("GET", "/api/conversations"): MEMBER,
    ("POST", "/api/conversations"): PERM_PROJECT_USE,
    ("GET", "/api/conversations/{session_id}"): MEMBER,
    ("DELETE", "/api/conversations/{session_id}"): PERM_PROJECT_USE,
    ("PUT", "/api/conversations/{session_id}/code"): PERM_PROJECT_USE,
    ("GET", "/api/conversations/{session_id}/messages"): MEMBER,
    ("POST", "/api/conversations/{session_id}/messages"): PERM_PROJECT_USE,
    ("POST", "/api/conversations/{session_id}/messages/stream"): PERM_PROJECT_USE,
    # ---- local code execution (also refused outright in prod; see local_exec) ----
    ("POST", "/api/execute"): PERM_PROJECT_USE,
    ("POST", "/api/execute/stream"): PERM_PROJECT_USE,
    ("POST", "/api/fix-code/stream"): PERM_PROJECT_USE,
    ("GET", "/api/generate-code/status"): MEMBER,
    # ---- registry skill sources become deployable code; the two staging
    # helpers ride the deploy permission because the create wizard needs them ----
    ("POST", "/api/agent-skills/import"): PERM_AGENT_DEPLOY,
    ("GET", "/api/registry/records"): MEMBER,
    ("POST", "/api/registry/records"): INFRA,
    ("GET", "/api/registry/records/search"): MEMBER,
    # data-plane read (ListDiscoverableRegistryRecords) of the workspace registry
    ("GET", "/api/registry/records/discoverable"): MEMBER,
    ("GET", "/api/registry/records/{record_id}"): MEMBER,
    # a data-plane read (GetAgentCard) of the agent's own runtime; no ARN from the client
    ("GET", "/api/registry/records/{record_id}/live-agent-card"): MEMBER,
    ("PUT", "/api/registry/records/{record_id}"): INFRA,
    ("DELETE", "/api/registry/records/{record_id}"): INFRA,
    ("POST", "/api/registry/records/{record_id}/action"): INFRA,
    ("POST", "/api/registry/records/{record_id}/reimport"): INFRA,
    ("GET", "/api/registry/skills/capabilities"): MEMBER,
    # installs software on the server host
    ("POST", "/api/registry/skills/capabilities/git-install"): INFRA,
    ("POST", "/api/registry/skills/import"): INFRA,
    # fetches remote content (SSRF-guarded); staging-only, needed by deploy
    ("POST", "/api/registry/skills/inspect"): PERM_AGENT_DEPLOY,
    # queues the registry_sync_defaults job (202); its status reads at the same role
    ("POST", "/api/registry/sync-defaults"): INFRA,
    ("GET", "/api/registry/sync-defaults/{job_id}"): INFRA,
    ("GET", "/api/registry/attachables"): MEMBER,
    ("POST", "/api/registry/a2a-demo"): PERM_PROJECT_USE,  # an invoke; parity with Chat
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
    ("POST", "/api/identity/gateway-targets"): INFRA_IDENTITY_MANAGE,
    ("DELETE", "/api/identity/gateway-targets/{target_id}"): INFRA_IDENTITY_MANAGE,
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
    # inbound JWT auth (P3): reading the workspace default is member (the
    # wizard shows the effective value); changing it flips how every inheriting
    # agent authenticates its callers on next deploy
    ("GET", "/api/identity/inbound-auth/default"): MEMBER,
    ("PUT", "/api/identity/inbound-auth/default"): INFRA_IDENTITY_MANAGE,
    # ---- tools + demos: /tools/call can mutate external systems through a
    # gateway target, and the demos open billable cloud sessions ----
    ("GET", "/api/tools"): MEMBER,
    ("POST", "/api/tools/call"): INFRA,
    ("POST", "/api/demos/code-interpreter"): PERM_PROJECT_WRITE,
    ("GET", "/api/demos/browser/options"): MEMBER,
    ("POST", "/api/demos/browser"): PERM_PROJECT_WRITE,  # takes a caller-supplied URL
    ("DELETE", "/api/demos/browser/{session_id}"): PERM_PROJECT_WRITE,
    # ---- knowledge bases: reads and the retrieval playground stay open ----
    ("GET", "/api/knowledge-bases"): MEMBER,
    ("POST", "/api/knowledge-bases"): PERM_PROJECT_WRITE,
    ("POST", "/api/knowledge-bases/ensure-gateway"): INFRA,
    ("GET", "/api/knowledge-bases/source"): MEMBER,
    ("GET", "/api/knowledge-bases/{kb_id}"): MEMBER,
    ("PATCH", "/api/knowledge-bases/{kb_id}"): PERM_PROJECT_WRITE,
    ("DELETE", "/api/knowledge-bases/{kb_id}"): PERM_PROJECT_WRITE,
    ("POST", "/api/knowledge-bases/{kb_id}/files"): PERM_PROJECT_WRITE,
    ("POST", "/api/knowledge-bases/{kb_id}/data-sources"): PERM_PROJECT_WRITE,
    ("DELETE", "/api/knowledge-bases/{kb_id}/data-sources/{ds_id}"): PERM_PROJECT_WRITE,
    ("GET", "/api/knowledge-bases/{kb_id}/data-sources/{ds_id}/documents"): MEMBER,
    ("GET", "/api/knowledge-bases/{kb_id}/data-sources/{ds_id}/ingestion-jobs"): MEMBER,
    ("POST", "/api/knowledge-bases/{kb_id}/data-sources/{ds_id}/sync"): PERM_PROJECT_WRITE,
    ("POST", "/api/knowledge-bases/{kb_id}/query"): PERM_PROJECT_USE,  # retrieval playground
    # ---- COA context layer: connecting a deployment creates a Connection (a vaulted
    # client secret) and a workload identity, so it is ADMIN; a member's own
    # consent, revocation and COA reads (as that member) are participation in the
    # project (project.use — the playground spends its budget) ----
    ("GET", "/api/context-layer"): MEMBER,
    ("PUT", "/api/context-layer"): ADMIN,
    ("DELETE", "/api/context-layer"): ADMIN,
    ("POST", "/api/context-layer/authorize"): PERM_PROJECT_USE,
    ("POST", "/api/context-layer/revoke"): PERM_PROJECT_USE,
    # the shared demo account: any member may reveal it (each reveal is logged)
    ("GET", "/api/context-layer/demo-account"): MEMBER,
    ("POST", "/api/context-layer/demo-account/reveal"): PERM_PROJECT_USE,
    ("GET", "/api/context-layer/namespaces"): MEMBER,
    ("GET", "/api/context-layer/namespaces/{namespace_id}"): MEMBER,
    ("GET", "/api/context-layer/namespaces/{namespace_id}/sources"): MEMBER,
    ("GET", "/api/context-layer/namespaces/{namespace_id}/metrics"): MEMBER,
    ("GET", "/api/context-layer/namespaces/{namespace_id}/ontology"): MEMBER,
    ("GET", "/api/context-layer/health"): MEMBER,
    ("POST", "/api/context-layer/playground"): PERM_PROJECT_USE,
    # the managed COA gateway: an AgentCore Gateway + target + workload identity
    # change, so ADMIN; its live status is a read
    ("GET", "/api/context-layer/gateway"): MEMBER,
    ("PUT", "/api/context-layer/gateway"): ADMIN,
    ("POST", "/api/context-layer/gateway/refresh"): ADMIN,
    ("DELETE", "/api/context-layer/gateway"): ADMIN,
    # ---- governance ----
    # the workspace's PII guardrail preset (T12): reading it is a member's business
    # (the wizard shows whether it exists); creating the real Bedrock resource is not
    ("GET", "/api/governance/guardrail"): MEMBER,
    ("POST", "/api/governance/guardrail"): ADMIN,
    ("GET", "/api/governance/gateways"): MEMBER,
    ("GET", "/api/governance/gateways/{gateway_id}"): MEMBER,
    ("POST", "/api/governance/gateways/{gateway_id}/manage"): INFRA,
    ("DELETE", "/api/governance/gateways/{gateway_id}/manage"): INFRA,
    ("GET", "/api/governance/gateways/{gateway_id}/registry-preview"): MEMBER,
    ("POST", "/api/governance/gateways/{gateway_id}/registry-import"): INFRA,
    ("POST", "/api/governance/gateways/{gateway_id}/retire-legacy-records"): INFRA,
    ("GET", "/api/governance/gateways/{gateway_id}/policies"): MEMBER,
    ("POST", "/api/governance/gateways/{gateway_id}/policies"): INFRA,
    ("PUT", "/api/governance/gateways/{gateway_id}/policies/{policy_id}"): INFRA,
    ("DELETE", "/api/governance/gateways/{gateway_id}/policies/{policy_id}"): INFRA,
    ("POST", "/api/governance/gateways/{gateway_id}/policies/{policy_id}/promote"): INFRA,
    ("POST", "/api/governance/gateways/{gateway_id}/policies/{policy_id}/rollback"): INFRA,
    ("POST", "/api/governance/gateways/{gateway_id}/engine"): INFRA,
    ("GET", "/api/governance/gateways/{gateway_id}/rate-limits"): MEMBER,
    ("POST", "/api/governance/gateways/{gateway_id}/rate-limits"): INFRA,
    ("PUT", "/api/governance/gateways/{gateway_id}/rate-limits/{rate_limit_id}"): INFRA,
    ("DELETE", "/api/governance/gateways/{gateway_id}/rate-limits/{rate_limit_id}"): INFRA,
    ("POST", "/api/governance/gateways/{gateway_id}/targets/{target_id}/synchronize"): INFRA,
    ("POST", "/api/governance/gateways/{gateway_id}/mode"): INFRA,
    ("POST", "/api/governance/gateways/{gateway_id}/generations"): INFRA,
    ("GET", "/api/governance/gateways/{gateway_id}/generations/{generation_id}"): MEMBER,
    ("GET", "/api/governance/gateways/{gateway_id}/audit"): MEMBER,
    ("GET", "/api/governance/gateways/{gateway_id}/decisions"): MEMBER,
    ("GET", "/api/governance/operations/{operation_id}"): MEMBER,
    ("GET", "/api/governance/policies"): MEMBER,
    ("GET", "/api/governance/decisions"): MEMBER,
    # Not a dry run: it performs a real tools/call as the chosen principal and
    # journals a PolicyDecision row (routers/governance.py:406).
    ("POST", "/api/governance/policy-test"): INFRA,
    ("POST", "/api/governance/policy-generation"): INFRA,
    ("GET", "/api/governance/policy-generation/{generation_id}"): MEMBER,
    ("GET", "/api/traces/{session_id}"): MEMBER,
    # ---- evaluation: runs invoke agents and create AWS eval jobs ----
    ("GET", "/api/eval/datasets"): MEMBER,
    ("POST", "/api/eval/datasets"): PERM_PROJECT_WRITE,
    ("POST", "/api/eval/datasets/upload"): PERM_PROJECT_WRITE,
    ("GET", "/api/eval/datasets/cloud"): MEMBER,
    ("GET", "/api/eval/datasets/cloud/{cloud_id}"): MEMBER,
    ("DELETE", "/api/eval/datasets/cloud/{cloud_id}"): PERM_PROJECT_WRITE,
    ("POST", "/api/eval/datasets/cloud/{cloud_id}/publish-version"): PERM_PROJECT_WRITE,
    ("DELETE", "/api/eval/datasets/cloud/{cloud_id}/versions/{version}"): PERM_PROJECT_WRITE,
    ("PUT", "/api/eval/datasets/{dataset_id}"): PERM_PROJECT_WRITE,
    ("DELETE", "/api/eval/datasets/{dataset_id}"): PERM_PROJECT_WRITE,
    ("POST", "/api/eval/datasets/{dataset_id}/sync-to-aws"): PERM_PROJECT_WRITE,
    ("POST", "/api/eval/datasets/{dataset_id}/publish-version"): PERM_PROJECT_WRITE,
    # console V2 数据处理: observed sessions → local datasets (reads + ledger writes)
    ("POST", "/api/eval/datasets/from-sessions"): PERM_PROJECT_WRITE,
    ("GET", "/api/eval/pipelines"): MEMBER,
    ("POST", "/api/eval/pipelines"): PERM_PROJECT_WRITE,
    ("GET", "/api/eval/pipelines/{pipeline_id}"): MEMBER,
    ("PUT", "/api/eval/pipelines/{pipeline_id}"): PERM_PROJECT_WRITE,
    ("DELETE", "/api/eval/pipelines/{pipeline_id}"): PERM_PROJECT_WRITE,
    ("POST", "/api/eval/pipelines/{pipeline_id}/run"): PERM_PROJECT_WRITE,
    ("POST", "/api/eval/pipelines/preview-logs"): PERM_PROJECT_WRITE,
    ("GET", "/api/eval/agents/{agent_id}/log-streams"): MEMBER,
    ("GET", "/api/eval/log-sessions"): MEMBER,
    ("GET", "/api/eval/log-groups"): MEMBER,
    ("GET", "/api/eval/log-services"): MEMBER,
    ("GET", "/api/eval/evaluators"): MEMBER,
    ("POST", "/api/eval/evaluators"): PERM_PROJECT_WRITE,
    ("GET", "/api/eval/evaluators/{evaluator_id}"): MEMBER,
    ("PUT", "/api/eval/evaluators/{evaluator_id}"): PERM_PROJECT_WRITE,
    ("DELETE", "/api/eval/evaluators/{evaluator_id}"): PERM_PROJECT_WRITE,
    ("GET", "/api/eval/queue"): MEMBER,
    ("GET", "/api/eval/runs"): MEMBER,
    ("POST", "/api/eval/runs"): PERM_EVAL_RUN,
    # agent scorecards: ledger reads. The two-run evaluator compare lives at
    # `/runs/compare-pair`: `/runs/compare` is V3's fix ladder (`?runs=`, dlc router)
    ("GET", "/api/eval/scorecard"): MEMBER,
    ("GET", "/api/eval/runs/compare-pair"): MEMBER,
    ("GET", "/api/eval/runs/{run_id}"): MEMBER,
    ("GET", "/api/eval/runs/{run_id}/results"): MEMBER,
    ("POST", "/api/eval/runs/{run_id}/stop"): PERM_EVAL_RUN,
    ("POST", "/api/eval/runs/{run_id}/recheck"): PERM_EVAL_RUN,
    # starts a new run of the same scope — the same grant as POST /runs
    ("POST", "/api/eval/runs/{run_id}/rerun"): PERM_EVAL_RUN,
    ("GET", "/api/eval/runs/{run_id}/recommendation-inputs"): MEMBER,
    ("GET", "/api/eval/runs/{run_id}/recommendations"): MEMBER,
    # starts billable AWS recommendation jobs — same revocable grant as a run
    ("POST", "/api/eval/runs/{run_id}/recommendations"): PERM_EVAL_RUN,
    # re-publishes the Harness (a new version) — the same grant as a redeploy
    # saving a revision of a recommendation is ledger-only, on the generating permission
    ("PUT", "/api/eval/runs/{run_id}/recommendations/{rec_id}/edit"): PERM_EVAL_RUN,
    ("POST", "/api/eval/runs/{run_id}/recommendations/{rec_id}/accept"): PERM_AGENT_DEPLOY,
    ("DELETE", "/api/eval/runs/{run_id}"): PERM_EVAL_RUN,
    # online evaluation configs: create/resume start billed judge calls on live
    # traffic, the same cost class as starting a run; list/detail/results read AWS
    ("GET", "/api/eval/online"): MEMBER,
    ("POST", "/api/eval/online"): PERM_EVAL_RUN,
    ("GET", "/api/eval/online/{config_id}"): MEMBER,
    ("PATCH", "/api/eval/online/{config_id}"): PERM_PROJECT_WRITE,
    ("POST", "/api/eval/online/{config_id}/pause"): PERM_PROJECT_WRITE,
    ("POST", "/api/eval/online/{config_id}/resume"): PERM_EVAL_RUN,
    ("DELETE", "/api/eval/online/{config_id}"): PERM_PROJECT_WRITE,
    ("GET", "/api/eval/online/{config_id}/results"): MEMBER,
    ("GET", "/api/eval/online/{config_id}/reports"): MEMBER,
    ("POST", "/api/eval/online/{config_id}/reports"): PERM_EVAL_RUN,
    ("GET", "/api/eval/online/{config_id}/reports/{batch_id}"): MEMBER,
    # ---- coding agent: the platform-owned coding runtime. Its status is a member
    # read; install/repair and uninstall create or delete the workspace's runtime +
    # role on an operator's explicit request (like system presets), so ADMIN ----
    ("GET", "/api/coding/runtime"): MEMBER,
    ("POST", "/api/coding/runtime/install"): ADMIN,
    ("DELETE", "/api/coding/runtime"): ADMIN,
    # ---- coding agent: a member's own repos. Every by-id route resolves through
    # the private-owner choke point (app/coding/repos.repo_in: someone else's
    # repo is 404 in every visibility mode); admins reach other members' repos
    # only through `?all=1` and DELETE. Writes are participation in the caller's
    # project (project.use: a turn spends its budget); deploying hands the repo to
    # the byoc pipeline, so it carries the same permission as any deploy ----
    ("GET", "/api/coding/repos"): MEMBER,
    ("POST", "/api/coding/repos"): PERM_PROJECT_USE,
    ("GET", "/api/coding/repos/{repo_id}"): MEMBER,
    ("PATCH", "/api/coding/repos/{repo_id}"): PERM_PROJECT_USE,
    ("DELETE", "/api/coding/repos/{repo_id}"): PERM_PROJECT_USE,
    ("POST", "/api/coding/repos/{repo_id}/turns"): PERM_PROJECT_USE,
    ("POST", "/api/coding/repos/{repo_id}/interrupt"): PERM_PROJECT_USE,
    ("GET", "/api/coding/repos/{repo_id}/events"): MEMBER,
    ("GET", "/api/coding/repos/{repo_id}/tree"): MEMBER,
    ("GET", "/api/coding/repos/{repo_id}/file"): MEMBER,
    ("PUT", "/api/coding/repos/{repo_id}/file"): PERM_PROJECT_USE,
    ("GET", "/api/coding/repos/{repo_id}/changes"): MEMBER,
    ("GET", "/api/coding/repos/{repo_id}/diff"): MEMBER,
    ("POST", "/api/coding/repos/{repo_id}/download"): PERM_PROJECT_USE,  # a read; POST body
    ("POST", "/api/coding/repos/{repo_id}/deploy"): PERM_AGENT_DEPLOY,
    # ---- skill lab: local task assets/task sets + Runtime-backed jobs ----
    ("GET", "/api/skill-lab/status"): MEMBER,
    ("POST", "/api/skill-lab/task-assets"): PERM_PROJECT_WRITE,
    ("GET", "/api/skill-lab/tasksets"): MEMBER,
    ("POST", "/api/skill-lab/tasksets"): PERM_PROJECT_WRITE,
    ("GET", "/api/skill-lab/tasksets/{taskset_id}"): MEMBER,
    ("PUT", "/api/skill-lab/tasksets/{taskset_id}"): PERM_PROJECT_WRITE,
    ("DELETE", "/api/skill-lab/tasksets/{taskset_id}"): PERM_PROJECT_WRITE,
    ("GET", "/api/skill-lab/jobs"): MEMBER,
    ("POST", "/api/skill-lab/jobs"): PERM_PROJECT_WRITE,
    ("GET", "/api/skill-lab/jobs/{job_id}"): MEMBER,
    ("POST", "/api/skill-lab/jobs/{job_id}/cancel"): PERM_PROJECT_WRITE,
    ("DELETE", "/api/skill-lab/jobs/{job_id}"): PERM_PROJECT_WRITE,
    ("POST", "/api/skill-lab/jobs/{job_id}/resume"): PERM_PROJECT_WRITE,
    ("POST", "/api/skill-lab/jobs/{job_id}/publish"): INFRA,
    ("POST", "/api/skill-lab/jobs/{job_id}/import-taskset"): PERM_PROJECT_WRITE,
    ("POST", "/api/skill-lab/jobs/{job_id}/apply-expansion"): PERM_PROJECT_WRITE,
    ("GET", "/api/skill-lab/jobs/{job_id}/train-summary"): MEMBER,
    ("GET", "/api/skill-lab/jobs/{job_id}/diff"): MEMBER,
    ("GET", "/api/skill-lab/jobs/{job_id}/log"): MEMBER,
    ("GET", "/api/skill-lab/jobs/{job_id}/results"): MEMBER,
    ("GET", "/api/skill-lab/jobs/{job_id}/artifacts"): MEMBER,
    ("GET", "/api/skill-lab/jobs/{job_id}/artifacts/raw"): MEMBER,
    ("GET", "/api/experiments"): MEMBER,
    ("GET", "/api/experiments/readiness"): MEMBER,
    ("GET", "/api/experiments/providers"): MEMBER,
    ("POST", "/api/experiments"): PERM_PROJECT_WRITE,
    ("GET", "/api/experiments/{exp_id}"): MEMBER,
    ("POST", "/api/experiments/{exp_id}/action"): PERM_PROJECT_WRITE,
    # canaries provision real AgentCore runtimes
    ("GET", "/api/runtime-canaries"): MEMBER,
    ("POST", "/api/runtime-canaries"): PERM_PROJECT_WRITE,
    ("GET", "/api/runtime-canaries/{canary_id}"): MEMBER,
    ("POST", "/api/runtime-canaries/{canary_id}/action"): PERM_PROJECT_WRITE,
    # ---- read-only consoles ----
    # the wizard's scenario-template gallery (T10): static data, no workspace
    ("GET", "/api/agent-templates"): MEMBER,
    # the configured platform model defaults (model ids only) the console pre-selects
    ("GET", "/api/models/defaults"): MEMBER,
    # the admin Models page: platform-wide model choices (console > env > default)
    ("GET", "/api/models/settings"): ADMIN,
    ("PUT", "/api/models/settings/{field}"): ADMIN,
    ("DELETE", "/api/models/settings/{field}"): ADMIN,
    # what the active workspace's Region offers, and a real tiny invocation of one
    # (the judge probe creates and deletes a throwaway evaluator — admin only)
    ("GET", "/api/model-catalog"): MEMBER,
    ("POST", "/api/model-probe"): ADMIN,
    # ---- release bundles, promotions, inbox (T20-T22) ----
    # Bundling freezes a publish and mints no AWS resource, so it rides the same
    # default-granted permission as asking for the release itself.
    ("POST", "/api/agents/{agent_id}/release-bundles"): PERM_PROMOTION_REQUEST,
    ("GET", "/api/agents/{agent_id}/release-bundles"): MEMBER,
    ("GET", "/api/release-bundles/{bundle_id}"): MEMBER,
    ("POST", "/api/promotions"): PERM_PROMOTION_REQUEST,
    ("GET", "/api/promotions"): MEMBER,
    ("GET", "/api/promotions/{promotion_id}"): MEMBER,
    # the second pair of eyes: operators and admins, never the requester themselves
    ("POST", "/api/promotions/{promotion_id}/review"): PERM_PROMOTION_APPROVE,
    # T26/T27: the read-only plan preview and live progress are plain reads; running a
    # release and undoing one are the operator's (same key as approving); their prod
    # decision is in PROD_UNPROTECTED_AGENT_ROUTES, with the reason.
    ("GET", "/api/promotions/{promotion_id}/plan"): MEMBER,
    ("GET", "/api/promotions/{promotion_id}/execution"): MEMBER,
    ("POST", "/api/promotions/{promotion_id}/execute"): PERM_PROMOTION_APPROVE,
    ("POST", "/api/promotions/{promotion_id}/rollback"): PERM_PROMOTION_APPROVE,
    # the release policy of a target workspace: readable by anyone who can see the plan,
    # writable only by an administrator (it decides what may ship, and when)
    ("GET", "/api/release-policies/{workspace_id}"): MEMBER,
    ("PUT", "/api/release-policies/{workspace_id}"): ADMIN,
    # the inbox reads pending accounts and workspaces as well as this workspace's work
    ("GET", "/api/inbox"): ADMIN,
    # ---- spend attribution and alerts (T28/T29) ----
    # Spend is admin business: the per-person breakdown names who spent what, which is
    # the same reason the TTFA view is admin-only.
    ("GET", "/api/costs"): ADMIN,
    ("GET", "/api/costs/month-to-date"): ADMIN,
    # Rules are the workspace's operating posture — readable by any member (an operator
    # needs to see what is watched), writable by an administrator only.
    ("GET", "/api/alerts"): MEMBER,
    ("POST", "/api/alerts"): ADMIN,
    ("PATCH", "/api/alerts/{rule_id}"): ADMIN,
    ("DELETE", "/api/alerts/{rule_id}"): ADMIN,
    # Evaluation costs a billed read and can notify, so it is not a member action.
    ("POST", "/api/alerts/evaluate"): ADMIN,
    # ---- fleet, governance health, template marketplace (T37-T39) ----
    # The fleet spans environments a member may not be granted, so it is admin-only and
    # hub-global (it deliberately does not operate inside one workspace).
    ("GET", "/api/fleet"): ADMIN,
    ("GET", "/api/governance/health"): MEMBER,
    # Reads are cross-workspace by design (that is what publishing is for); writing is
    # scoped to the publishing workspace, which the handler enforces.
    ("GET", "/api/marketplace/templates"): MEMBER,
    ("POST", "/api/marketplace/templates"): INFRA,
    ("POST", "/api/marketplace/templates/{template_id}/use"): PERM_PROJECT_WRITE,
    ("DELETE", "/api/marketplace/templates/{template_id}"): INFRA,
    # ---- logical resource mapping (T23) ----
    # Reads are plain member (a builder previews what a bundle resolves to). Writes decide
    # which of the SELECTED (target) workspace's resources a promoted agent is wired to,
    # so they are the approver's permission, not the builder's. Not in PROD_PROTECTED:
    # mapping prod resources is exactly what an operator does there, and it mints no AWS
    # resource; the write is journaled in audit_events by the handler.
    ("GET", "/api/resource-mappings"): MEMBER,
    ("PUT", "/api/resource-mappings/{kind}/{name}"): PERM_PROMOTION_APPROVE,
    ("DELETE", "/api/resource-mappings/{kind}/{name}"): PERM_PROMOTION_APPROVE,
    ("GET", "/api/release-bundles/{bundle_id}/resolution"): MEMBER,
    # ---- GitOps export and environment comparison (T31/T32) ----
    # All read-only. `compare` reads sibling workspaces' ledger rows, filtered to the
    # caller's grants in the handler; `drift` reads AWS for the current workspace only.
    ("GET", "/api/release-bundles/{bundle_id}/export"): MEMBER,
    ("GET", "/api/environments/compare"): MEMBER,
    ("GET", "/api/environments/drift"): MEMBER,
    # ---- share links (T13/T14) and thumbs feedback (T15) ----
    # Creating a link is not an agent mutation, so it is deliberately NOT in
    # PROD_PROTECTED: a prod workspace may still hand out a chat link.
    ("GET", "/api/agents/{agent_id}/share-links"): MEMBER,
    ("POST", "/api/agents/{agent_id}/share-links"): PERM_PROJECT_WRITE,
    ("POST", "/api/share-links/{link_id}/revoke"): PERM_PROJECT_WRITE,
    ("POST", "/api/chat/{agent_id}/feedback"): PERM_PROJECT_USE,
    ("GET", "/api/feedback"): MEMBER,
    # The account-free surface (T13): outside `/api`, so no console session, and
    # PUBLIC + hub-global so no workspace header is read either — the link's own
    # row names the workspace. Each handler resolves the token first and answers
    # 404 for every unusable state.
    ("GET", "/share/{token}"): PUBLIC,
    ("POST", "/share/{token}/chat"): PUBLIC,
    ("POST", "/share/{token}/feedback"): PUBLIC,
    # SME review (T34): the same account-free posture under `/share/review`
    ("GET", "/share/review/{token}"): PUBLIC,
    ("POST", "/share/review/{token}/rate"): PUBLIC,
    # Agent-DLC annotation links (design §7.4): the same posture. The link row names
    # the workspace, the task and the annotator; the judge's verdict and the agreement
    # numbers are never served here.
    ("GET", "/share/annotate/{token}"): PUBLIC,
    ("POST", "/share/annotate/{token}/label"): PUBLIC,
    ("GET", "/api/agents/{agent_id}/review-links"): MEMBER,
    ("POST", "/api/agents/{agent_id}/review-links"): PERM_PROJECT_WRITE,
    # ---- business self-service: intent view, curated answers, issue box (T33/T35/T36) ----
    # Deliberately NOT in PROD_PROTECTED: the point of a curated answer is that a business
    # owner fixes production without a redeploy. Rule writes are journaled in audit_events.
    # ── Agent-DLC (docs/agent-dlc-design.md) ────────────────────────────────
    # Reads are MEMBER: the standard and its evidence are the team's shared picture.
    # Writes split by whose decision they are: editing the ruler is engineering
    # (`criteria.manage`), signing it / admitting samples / declaring a judge
    # calibrated / approving a waiver / signing a release are named people's.
    ("GET", "/api/criteria-sets"): MEMBER,
    ("POST", "/api/criteria-sets"): PERM_CRITERIA_MANAGE,
    ("GET", "/api/criteria-sets/{lineage_id}"): MEMBER,
    ("PUT", "/api/criteria-sets/{lineage_id}"): PERM_CRITERIA_MANAGE,
    ("DELETE", "/api/criteria-sets/{lineage_id}"): PERM_CRITERIA_MANAGE,
    ("POST", "/api/criteria-sets/{lineage_id}/versions"): PERM_CRITERIA_MANAGE,
    ("POST", "/api/criteria-sets/{lineage_id}/publish"): PERM_CRITERIA_MANAGE,
    ("POST", "/api/criteria-sets/{lineage_id}/sign"): PERM_CRITERIA_SIGN,
    ("POST", "/api/criteria-sets/{lineage_id}/adopt-template"): PERM_CRITERIA_MANAGE,
    ("GET", "/api/criteria-sets/{lineage_id}/diff"): MEMBER,
    ("GET", "/api/golden-sets"): MEMBER,
    ("POST", "/api/golden-sets"): PERM_CRITERIA_MANAGE,
    ("GET", "/api/golden-sets/{dataset_id}"): MEMBER,
    ("DELETE", "/api/golden-sets/{dataset_id}"): ADMIN,
    ("POST", "/api/golden-sets/{dataset_id}/items"): PERM_CRITERIA_MANAGE,
    # seeding writes the holdout once: the same right that admits golden samples
    ("POST", "/api/golden-sets/{dataset_id}/seed"): PERM_GOLDEN_ADMIT,
    ("POST", "/api/golden-sets/{dataset_id}/move"): PERM_CRITERIA_MANAGE,
    ("POST", "/api/golden-sets/{dataset_id}/retire"): PERM_CRITERIA_MANAGE,
    ("GET", "/api/annotation-tasks"): MEMBER,
    ("POST", "/api/annotation-tasks"): PERM_CRITERIA_MANAGE,
    ("GET", "/api/annotation-tasks/{task_id}"): MEMBER,
    ("DELETE", "/api/annotation-tasks/{task_id}"): PERM_CRITERIA_MANAGE,
    # labelling is the annotator's own act; the service refuses a non-annotator
    ("POST", "/api/annotation-tasks/{task_id}/labels"): PERM_PROJECT_USE,
    ("POST", "/api/annotation-tasks/{task_id}/adjudicate"): PERM_JUDGE_CALIBRATE,
    ("GET", "/api/annotation-tasks/{task_id}/agreement"): MEMBER,
    ("POST", "/api/annotation-tasks/{task_id}/decide"): PERM_JUDGE_CALIBRATE,
    ("GET", "/api/calibration/{criterion_key}"): MEMBER,
    # CR-2 (calibration-run-picker §4.3): the run picker of the new-task dialog — the
    # same key as creating the task it feeds; it counts verdicts, shows no sample
    ("GET", "/api/calibration/candidate-runs"): PERM_CRITERIA_MANAGE,
    ("GET", "/api/calibration/run-criteria/{run_id}"): PERM_CRITERIA_MANAGE,
    ("GET", "/api/annotation-tasks/{task_id}/links"): MEMBER,
    ("POST", "/api/annotation-tasks/{task_id}/links"): PERM_JUDGE_CALIBRATE,
    ("DELETE", "/api/annotation-tasks/{task_id}/links/{link_id}"): PERM_JUDGE_CALIBRATE,
    ("GET", "/api/agents/{agent_id}/release"): MEMBER,
    ("POST", "/api/agents/{agent_id}/release/migrate"): PERM_RELEASE_SIGN,
    ("POST", "/api/agents/{agent_id}/release/evaluate"): PERM_EVAL_RUN,
    ("GET", "/api/agents/{agent_id}/release/gate"): MEMBER,
    ("POST", "/api/agents/{agent_id}/release/sign"): PERM_RELEASE_SIGN,
    ("POST", "/api/agents/{agent_id}/release/block"): PERM_RELEASE_SIGN,
    ("POST", "/api/agents/{agent_id}/release/rollback"): PERM_RELEASE_SIGN,
    ("GET", "/api/release-records"): MEMBER,
    ("GET", "/api/release-records/{record_id}"): MEMBER,
    ("POST", "/api/agents/{agent_id}/waivers"): PERM_PROJECT_WRITE,  # asking is not deciding
    ("POST", "/api/waivers/{waiver_id}/approve"): PERM_WAIVER_APPROVE,
    ("POST", "/api/waivers/{waiver_id}/reject"): PERM_WAIVER_APPROVE,
    ("DELETE", "/api/waivers/{waiver_id}"): PERM_WAIVER_APPROVE,
    ("GET", "/api/admission"): MEMBER,
    ("GET", "/api/admission/{candidate_id}"): MEMBER,
    ("POST", "/api/admission/{candidate_id}/admit"): PERM_GOLDEN_ADMIT,
    ("POST", "/api/admission/{candidate_id}/reject"): PERM_GOLDEN_ADMIT,
    ("POST", "/api/admission/{candidate_id}/duplicate"): PERM_GOLDEN_ADMIT,
    ("GET", "/api/agents/{agent_id}/watch"): MEMBER,
    ("PUT", "/api/agents/{agent_id}/watch"): PERM_CRITERIA_MANAGE,
    ("POST", "/api/agents/{agent_id}/watch/run"): PERM_EVAL_RUN,
    ("GET", "/api/eval/runs/{run_id}/criteria"): MEMBER,
    ("POST", "/api/eval/runs/{run_id}/criteria/snapshot"): PERM_EVAL_RUN,
    ("GET", "/api/eval/runs/compare"): MEMBER,
    ("POST", "/api/eval/runs/estimate"): PERM_PROJECT_USE,
    ("GET", "/api/agents/{agent_id}/ladder"): MEMBER,
    ("GET", "/api/agents/{agent_id}/scorecard"): MEMBER,
    ("GET", "/api/audit"): ADMIN,
    ("GET", "/api/intents"): MEMBER,
    ("GET", "/api/agents/{agent_id}/rules"): MEMBER,
    ("POST", "/api/agents/{agent_id}/rules"): PERM_PROJECT_WRITE,
    ("PATCH", "/api/agents/{agent_id}/rules/{rule_id}"): PERM_PROJECT_WRITE,
    ("DELETE", "/api/agents/{agent_id}/rules/{rule_id}"): PERM_PROJECT_WRITE,
    ("PUT", "/api/agents/{agent_id}/rules-order"): PERM_PROJECT_WRITE,
    ("PUT", "/api/agents/{agent_id}/rules-enabled"): PERM_PROJECT_WRITE,
    ("POST", "/api/agents/{agent_id}/rules/test"): PERM_PROJECT_USE,
    ("GET", "/api/issues"): MEMBER,
    ("POST", "/api/issues"): PERM_PROJECT_WRITE,
    ("POST", "/api/issues/sync"): PERM_PROJECT_WRITE,
    ("GET", "/api/issues/{issue_id}"): MEMBER,
    ("POST", "/api/issues/{issue_id}/resolve"): PERM_PROJECT_WRITE,
    ("POST", "/api/issues/{issue_id}/reopen"): PERM_PROJECT_WRITE,
    ("POST", "/api/issues/{issue_id}/fixes"): PERM_PROJECT_WRITE,
    # ---- channel publishing (T30) ----
    # Creating a channel link is like creating a share link: MEMBER, not prod-protected.
    # The webhook is PUBLIC: Slack / Feishu call it, authenticated per request by the
    # adapter (signing secret / verification token) on top of the link token in the path.
    ("POST", "/api/agents/{agent_id}/channel-links"): PERM_PROJECT_WRITE,
    ("POST", "/share/channels/{platform}/{token}"): PUBLIC,
    ("GET", "/api/overview"): MEMBER,
    ("GET", "/api/overview/online-quality"): MEMBER,
    # per-user onboarding activity (TTFA) — admin-only like the Users console
    ("GET", "/api/overview/ttfa"): ADMIN,
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
    ("POST", "/api/memory/records/search"): PERM_PROJECT_USE,  # a search, not a mutation
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
    ("POST", "/api/observability/session-previews"): PERM_PROJECT_USE,  # a read; POST body
    # on-demand Evaluate: a judge inference, persists nothing
    ("POST", "/api/observability/sessions/{session_id}/evaluate"): PERM_PROJECT_WRITE,
    ("GET", "/api/observability/traces"): MEMBER,
    ("GET", "/api/observability/traces/{trace_id}"): MEMBER,
    ("GET", "/api/observability/prices"): MEMBER,
    ("POST", "/api/observability/prices/refresh"): INFRA,  # rewrites shared config
    # ---- console account management ----
    ("GET", "/api/users"): ADMIN,
    # an admin creates an account (active at once; only an admin can grant admin)
    ("POST", "/api/users"): ADMIN,
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
    ("POST", "/api/workspaces/{workspace_id}/observability/repair"): ADMIN,
    ("GET", "/api/workspaces/{workspace_id}/bootstrap"): ADMIN,
    # Reads the spoke role's own template-version tag (cached); writes nothing.
    ("GET", "/api/workspaces/{workspace_id}/role-template"): ADMIN,
    ("GET", "/api/workspaces/{workspace_id}/grants"): ADMIN,
    # Bulk grant/revoke from the workspace's side (per-user replacement stays on
    # PATCH /api/users/{id}); both write only `user_workspaces`.
    ("PUT", "/api/workspaces/{workspace_id}/grants"): ADMIN,
    # Shared vs isolated member resource visibility (services/visibility.py).
    ("GET", "/api/workspaces/{workspace_id}/settings/visibility"): ADMIN,
    ("PUT", "/api/workspaces/{workspace_id}/settings/visibility"): ADMIN,
    # ---- T18-tools: the agent detail's telemetry status (projects §17.3-4) — a cached,
    # read-only aws/spans probe of one agent in the caller's project ----
    ("GET", "/api/agents/{agent_id}/telemetry"): PERM_PROJECT_USE,
}

# Hub-global route prefixes: nothing under them operates inside a workspace.
HUB_GLOBAL_PREFIXES = (
    "/api/auth", "/api/users", "/api/workspaces", "/api/announcements", "/api/videos",
    # the wizard's scenario-template catalogue (T10) is static data — no account,
    # no region, nothing to scope
    "/api/agent-templates",
    # configured model ids (routers/model_defaults.py) — settings, not account data
    "/api/models",
    # the fleet view (T37) is the one read that deliberately spans every environment
    "/api/fleet",
    # the account-free share surface (T13): the token's row decides the workspace
    "/share",
    # project administration and the caller's access map (projects design §7.10):
    # projects span workspaces, so none of it runs inside one
    "/api/projects", "/api/role-templates", "/api/me",
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
        ("POST", "/api/auth/password"),
        ("GET", "/api/agent-templates"),
        ("GET", "/api/models/defaults"),
        ("GET", "/api/models/settings"),
        ("PUT", "/api/models/settings/{field}"),
        ("DELETE", "/api/models/settings/{field}"),
        ("GET", "/api/fleet"),
        ("GET", "/share/{token}"),
        ("POST", "/share/{token}/chat"),
        ("POST", "/share/{token}/feedback"),
        ("GET", "/share/review/{token}"),
        ("POST", "/share/review/{token}/rate"),
        ("GET", "/share/annotate/{token}"),
        ("POST", "/share/annotate/{token}/label"),
        ("POST", "/share/channels/{platform}/{token}"),
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
        ("POST", "/api/users"),
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
        ("POST", "/api/workspaces/{workspace_id}/observability/repair"),
        ("GET", "/api/workspaces/{workspace_id}/bootstrap"),
        ("GET", "/api/workspaces/{workspace_id}/role-template"),
        ("GET", "/api/workspaces/{workspace_id}/grants"),
        ("PUT", "/api/workspaces/{workspace_id}/grants"),
        # The target is the path parameter, like grants.
        ("GET", "/api/workspaces/{workspace_id}/settings/visibility"),
        ("PUT", "/api/workspaces/{workspace_id}/settings/visibility"),
    }
)

# ---- project administration (projects design §7.10, P1F-proj) ----
# Hub-global (HUB_GLOBAL_PREFIXES): a project spans workspaces. `pperm:<key>` is the
# design's path-project permission: <key> among the caller's effective keys in the
# project named by `{project_id}` in the PATH (never X-Project) — a non-member gets
# 404 `project.not_found`, a member without the key 403. Administrators pass.
# Membership-checked reads are MEMBER here; their handlers answer 404 to non-members.
PPERM_PREFIX = "pperm:"
PPERM_PROJECT_MEMBERS = "pperm:project.members"
PROJECT_ADMIN_ROUTES: dict[tuple[str, str], str] = {
    ("GET", "/api/me/access"): MEMBER,  # the caller's own projects and keys
    ("GET", "/api/role-templates"): MEMBER,  # static: built-in templates and keys
    ("GET", "/api/projects"): MEMBER,  # admins: all; anyone else: their own
    ("POST", "/api/projects"): ADMIN,
    ("GET", "/api/projects/{project_id}"): MEMBER,
    # name / department are admin-only inside the handler; an owner edits
    # the description and the owner-editable settings
    ("PATCH", "/api/projects/{project_id}"): PPERM_PROJECT_MEMBERS,
    ("POST", "/api/projects/{project_id}/archive"): ADMIN,
    ("POST", "/api/projects/{project_id}/unarchive"): ADMIN,
    ("GET", "/api/projects/{project_id}/workspaces"): MEMBER,
    ("PUT", "/api/projects/{project_id}/workspaces"): ADMIN,
    ("GET", "/api/projects/{project_id}/members"): MEMBER,
    ("PUT", "/api/projects/{project_id}/members"): PPERM_PROJECT_MEMBERS,
    ("DELETE", "/api/projects/{project_id}/members/{user_id}"): PPERM_PROJECT_MEMBERS,
    ("GET", "/api/projects/{project_id}/candidates"): PPERM_PROJECT_MEMBERS,
    ("GET", "/api/projects/{project_id}/roles"): MEMBER,
    ("PUT", "/api/projects/{project_id}/roles/{role_key}"): PPERM_PROJECT_MEMBERS,
    ("DELETE", "/api/projects/{project_id}/roles/{role_key}"): PPERM_PROJECT_MEMBERS,
    # moves an agent and its derived rows to another project of its workspace
    ("POST", "/api/projects/{project_id}/transfer"): ADMIN,
}
ROUTE_POLICY.update(PROJECT_ADMIN_ROUTES)
WORKSPACE_EXEMPT = WORKSPACE_EXEMPT | frozenset(PROJECT_ADMIN_ROUTES)


def require_path_project_permission(request: Request, key: str) -> None:
    """`pperm:<key>`: the caller holds <key> in the project named by the path."""
    from app.core.db import SessionLocal
    from app.core.errors import NotFoundError
    from app.core.project_scope import project_context
    from app.services import access

    identity = require_identity(request)
    project_id = str(request.path_params.get("project_id") or "")
    # administrators pass; a steward (§11.4) on the structure keys only
    passes = access.has_data_bypass(identity) or (
        key in STRUCTURE_PROJECT_KEYS and access.can_manage_structure(identity)
    )
    db = SessionLocal()
    try:
        with project_context(None):
            status = db.execute(
                text("SELECT status FROM projects WHERE id = :p"), {"p": project_id},
            ).scalar()
            membership = (
                None if passes
                else access.snapshot_for(db, identity).memberships.get(project_id)
            )
    finally:
        db.close()
    if not passes:
        if status is None or membership is None:
            if status is not None:  # a real project's: its audit view shows the attempt
                _journal_refusal(request, "project.not_found", project_id=project_id)
            raise NotFoundError("project.not_found", f"project '{project_id}' not found",
                                {"project_id": project_id})
        if key not in membership.permissions:
            _journal_refusal(request, "auth.permission_required", project_id=project_id)
            raise AppError(
                "auth.permission_required",
                f"This action requires the '{key}' permission in project '{project_id}'",
                {"permission": key, "project_id": project_id},
                status_code=403,
            )
    if status is not None and status != "active" and request.method not in access.READ_METHODS:
        raise AppError("project.archived", f"project '{project_id}' is archived",
                       {"project_id": project_id}, status_code=409)


# ---- third dimension (T05): prod protection ----
# The agent-mutating routes. On a workspace whose tier is `prod`, a member calling
# one of these gets 403 `workspace.prod_protected` (changes must arrive through
# promotion); an administrator is let through as break-glass and the call is
# journaled in `audit_events`. Everything else — reads, chat/invoke, eval,
# observability — stays open in prod. `tests/test_prod_protection.py` pins every
# `perm:agents.*` route to either this set or `PROD_UNPROTECTED_AGENT_ROUTES`, so
# a new lifecycle route cannot ship without a prod decision, and every non-read route
# under the agent, canary and experiment namespaces to this set or a documented exemption.
PROD_PROTECTED: frozenset[tuple[str, str]] = frozenset(
    {
        ("POST", "/api/agents"),  # create / deploy
        ("POST", "/api/agents/discovery/import"),
        ("POST", "/api/agents/uploads"),  # BYOC artifact staging
        # the Agent IDE's deploy handoff creates / re-publishes a byoc Agent through
        # start_create / republish_agent — POST /api/agents and redeploy by another door
        ("POST", "/api/coding/repos/{repo_id}/deploy"),
        ("DELETE", "/api/agents/{agent_id}"),
        ("POST", "/api/agents/{agent_id}/convert"),
        ("POST", "/api/agents/{agent_id}/redeploy"),  # edit = redeploy
        ("POST", "/api/agents/{agent_id}/inbound-auth"),  # re-publishes the runtime
        # accepting an AI recommendation republishes the agent (a new Harness version)
        # through the same republish path as redeploy, so prod treats it the same way
        ("POST", "/api/eval/runs/{run_id}/recommendations/{rec_id}/accept"),
        ("POST", "/api/agents/{agent_id}/snapshots/{seq}/rollback"),  # redeploy of an old spec
        # optimization flows that change what a live agent serves: a runtime canary mints
        # a candidate version from a member-supplied prompt / code and its setup already
        # rolls DEFAULT to it; an experiment's accept + promote redeploys the agent in place
        ("POST", "/api/runtime-canaries"),
        ("POST", "/api/runtime-canaries/{canary_id}/action"),
        ("POST", "/api/experiments/{exp_id}/action"),
        # Agent-DLC: the standard, its samples and what production serves
        ("POST", "/api/criteria-sets/{lineage_id}/publish"),
        ("POST", "/api/criteria-sets/{lineage_id}/sign"),
        ("POST", "/api/golden-sets/{dataset_id}/items"),
        ("POST", "/api/golden-sets/{dataset_id}/seed"),
        # taking a case OUT of the gate is as much a change to the standard as adding
        # one: a retired item leaves the gate's denominator, and a move shifts it out
        # of the replayed split
        ("POST", "/api/golden-sets/{dataset_id}/move"),
        ("POST", "/api/golden-sets/{dataset_id}/retire"),
        # a `not_aligned` verdict demotes a gating judge criterion to `observe`
        ("POST", "/api/annotation-tasks/{task_id}/decide"),
        ("POST", "/api/admission/{candidate_id}/admit"),
        ("POST", "/api/waivers/{waiver_id}/approve"),
        ("POST", "/api/agents/{agent_id}/release/migrate"),
        ("POST", "/api/agents/{agent_id}/release/sign"),
        ("POST", "/api/agents/{agent_id}/release/rollback"),
        ("PUT", "/api/agents/{agent_id}/watch"),
        ("POST", "/api/agents/{agent_id}/release/block"),
        ("POST", "/api/agent-skills/import"),  # deploy-flow skill upload to S3
        # the architect assistant's two deploy-side writes
        ("POST", "/api/assistant/architect/conversations/{conversation_id}/preparation/skills"),
        ("POST", "/api/assistant/architect/conversations/{conversation_id}/proposal/approve"),
        # system presets: already admin-only, listed so a prod install is journaled
        ("POST", "/api/system-agents/{preset_key}/install"),
        ("POST", "/api/system-agents/{preset_key}/skill-registration"),
        ("DELETE", "/api/system-agents/{preset_key}"),
    }
)
# `perm:agents.*` routes deliberately left open in prod, each with its reason.
PROD_UNPROTECTED_AGENT_ROUTES: dict[tuple[str, str], str] = {
    ("POST", "/api/registry/skills/inspect"): "preview only: parses a source, writes no S3/AWS",
    # T27: these publish agents, but into the promotion's TARGET workspace, while this guard
    # reads the tier of the REQUEST's workspace (the source). They are gated harder than the
    # guard could gate them: `promotion.approve` is held by operators/admins only, never by
    # a member, and a prod target is protected by the blocking gates (ENFORCE, window,
    # evaluation) instead. Listing them here would refuse an operator working from a prod
    # source and protect nothing about the prod target.
    ("POST", "/api/promotions/{promotion_id}/execute"): "targets another workspace; operator-only",
    ("POST", "/api/promotions/{promotion_id}/rollback"): "targets another workspace; operator-only",
    # Agent-DLC: these three change no agent. Evaluating a candidate and a scheduled
    # re-run only read the agent through its candidate endpoint (that is the point of
    # gating), and asking for a waiver is a request a risk owner still has to approve.
    ("POST", "/api/agents/{agent_id}/release/evaluate"): (
        "evaluates the candidate endpoint; it changes nothing the agent serves"
    ),
    ("POST", "/api/agents/{agent_id}/watch/run"): (
        "a read-only re-evaluation of the golden split"
    ),
    ("POST", "/api/agents/{agent_id}/waivers"): (
        "requesting a waiver is not granting one — approval is prod-protected"
    ),
    # The architect assistant's evaluation assets (upstream 2026-10-08 moved them from
    # ADMIN to `perm:agents.deploy`): they create / retry / review / clean up the eval
    # datasets, evaluators and test Lambda of a conversation's plan. Evaluation stays
    # open in prod; none of these changes what an agent serves.
    ("POST",
     "/api/assistant/architect/conversations/{conversation_id}/evaluation-plan/materialize"): (
        "creates evaluation assets for the plan; changes no agent"
    ),
    ("POST", "/api/assistant/architect/conversations/{conversation_id}/evaluation-plan/operations/"
     "{operation_id}/retry"): "retries an evaluation-asset operation; changes no agent",
    ("POST", "/api/assistant/architect/conversations/{conversation_id}/evaluation-plan/operations/"
     "{operation_id}/lambda-revision-review"): (
        "reviews an evaluation-asset Lambda conflict; ledger write + requeue only"
    ),
    ("DELETE", "/api/assistant/architect/conversations/{conversation_id}/evaluation-plan/"
     "operations/{operation_id}/assets"): "cleans up evaluation assets; changes no agent",
}


def is_prod_protected(method: str, path_format: str) -> bool:
    lookup = "GET" if method == "HEAD" else method
    return (lookup, path_format) in PROD_PROTECTED


def _guard_prod(request: Request, scope: Any, method: str, path_format: str) -> None:
    """Refuse a member's agent mutation on a `prod` workspace; journal an admin's."""
    if (getattr(scope.row, "tier", None) or "dev") != TIER_PROD:
        return
    if not is_prod_protected(method, path_format):
        return
    identity = require_identity(request)
    if not identity.is_admin:
        project = getattr(request.state, "project", None)
        _journal_refusal(request, "workspace.prod_protected", workspace_id=scope.id,
                         project_id=project.id if project is not None else None)
        raise AppError(
            "workspace.prod_protected",
            f"workspace '{scope.id}' is a prod workspace — agent changes must arrive "
            "through promotion, not be made here directly",
            {"workspace_id": scope.id, "tier": TIER_PROD},
            status_code=403,
        )
    record_audit_event(
        workspace_id=scope.id,
        actor=identity.username,
        action=f"{method} {path_format}",
        target=request.url.path,
    )


# Routers whose classification was extrapolated from the signed-off principle
# rather than reviewed route by route. Since the 2026-08-11 amendment they are
# all plain MEMBER anyway; the list survives as a pointer to what to re-examine
# if per-user data partitioning ever tightens the posture again.
UNREVIEWED_PREFIXES = (
    "/api/eval/",
    "/api/experiments",
    "/api/runtime-canaries",
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


# Workspace-scoped routes that need no project (design §5.3 PROJECT_EXEMPT): workspace
# configuration and catalogs, not project data. Extended as routers are reviewed.
PROJECT_EXEMPT: frozenset[tuple[str, str]] = frozenset(
    {
        ("DELETE", "/api/system-agents/{preset_key}"),
        # lists projects across the workspace for the caller (agent-project picker)
        ("GET", "/api/agents/eligible-projects"),
        ("GET", "/api/audit"),
        ("GET", "/api/costs"),
        ("GET", "/api/costs/month-to-date"),
        ("GET", "/api/demos/browser/options"),
        ("GET", "/api/experiments/providers"),
        ("GET", "/api/generate-code/status"),
        ("GET", "/api/governance/guardrail"),
        ("GET", "/api/governance/policy-generation/{generation_id}"),
        ("GET", "/api/identity/connections/templates"),
        ("GET", "/api/identity/consent-portal"),
        ("GET", "/api/identity/inbound-auth/default"),
        ("GET", "/api/inbox"),
        ("GET", "/api/marketplace/templates"),
        ("GET", "/api/model-catalog"),
        ("GET", "/api/observability/prices"),
        ("GET", "/api/registry/skills/capabilities"),
        ("GET", "/api/release-policies/{workspace_id}"),
        ("GET", "/api/skill-lab/status"),
        ("GET", "/api/system-agents"),
        ("GET", "/api/tools"),
        ("POST", "/api/model-probe"),
        ("POST", "/api/system-agents/{preset_key}/install"),
        ("POST", "/api/system-agents/{preset_key}/skill-registration"),
        ("PUT", "/api/release-policies/{workspace_id}"),
    }
)


def is_project_scoped(method: str, path_format: str) -> bool:
    """Whether a workspace-scoped route runs inside one project."""
    lookup = "GET" if method == "HEAD" else method
    return (lookup, path_format) not in PROJECT_EXEMPT


def _resolve_request_project(request: Request, workspace_id: str) -> None:
    """Resolve the request's project once and leave it on ``request.state.project``
    for ``require_permission`` / ``require_project`` and the ORM context
    (``bind_request_project``)."""
    from app.core.db import SessionLocal
    from app.core.project_scope import project_context
    from app.services import access

    identity = require_identity(request)
    requested = (request.headers.get(access.PROJECT_HEADER) or "").strip()
    db = SessionLocal()
    try:
        with project_context(None):  # access tables are not project-owned anyway
            request.state.project = access.resolve_project(
                db, identity, workspace_id, requested, request.method,
            )
    finally:
        db.close()


async def bind_request_project(request: Request) -> None:
    """App-level dependency, after ``enforce_route_policy``: publish the resolved project
    to the ORM context. Async on purpose — it runs in the request's own task, so the
    sync handler and a streaming response's generator (both run in the threadpool with
    a copy of this context) inherit it; the sync policy dependency could not set it."""
    from app.core.project_scope import set_current_project

    scope = getattr(request.state, "project", None)
    set_current_project(scope.id if scope is not None else None)


def enforce_route_policy(request: Request) -> None:
    """App-level dependency enforcing ROUTE_POLICY.

    A dependency rather than middleware because `scope["route"]` is only set once
    the router has matched, so this sees the exact `path_format` instead of
    re-implementing path matching.
    """
    path = request.url.path
    if path != API_PREFIX and not path.startswith(f"{API_PREFIX}/"):
        return  # /v1 carries its own X-Api-Key auth; static and redirects are open
    if request.method == "OPTIONS":
        return  # CORS preflight never reaches a handler
    route: Any = request.scope.get("route")
    path_format = getattr(route, "path_format", None) or path
    role = required_role(request.method, path_format)
    if role is None:
        # Default-deny: refuse rather than serve an unclassified route.
        raise AppError(
            "auth.route_unclassified",
            f"{request.method} {path_format} is missing from ROUTE_POLICY "
            "(app/core/route_policy.py) — classify it before serving it.",
            status_code=500,
        )
    if (role == ADMIN or role.startswith(PPERM_PREFIX)) and department_admin_passes(
            request, path_format):
        return  # §11.1: a department administrator, inside their department (hub-global)
    if role == ADMIN:
        require_admin_kind(request, request.method, path_format)  # §11.4 steward split
    if is_workspace_scoped(request.method, path_format):
        # Resolved here rather than per handler: enforcement must not depend on a
        # router remembering to declare the dependency. Handlers read the result
        # back through `require_workspace`.
        try:
            scope = resolve_workspace(request)
            if is_project_scoped(request.method, path_format):
                # before the permission check: inside a project the keys are the
                # project's (design §5.1), not the account's
                _resolve_request_project(request, scope.id)
        except AppError:
            # Keep the old precedence for a caller who lacks the key outright: they
            # learn "you may not do this", not which workspaces or projects exist.
            # (project.* keys exist only inside a project, so they cannot be checked here)
            key = (
                role.removeprefix(_PERM_PREFIX) if role.startswith(_PERM_PREFIX)
                else infra_key(role) if role.startswith(INFRA) else ""
            )
            if key and not key.startswith("project."):
                require_permission(request, key)
            raise
        _guard_prod(request, scope, request.method, path_format)
    if role.startswith(PPERM_PREFIX):  # journals its own refusals
        require_path_project_permission(request, role.removeprefix(PPERM_PREFIX))
        return
    try:
        if role.startswith(_PERM_PREFIX):
            require_permission(request, role.removeprefix(_PERM_PREFIX))
        elif role.startswith(INFRA):
            require_infra(request, role)
    except AppError as err:
        project = getattr(request.state, "project", None)
        if err.status_code == 403 and project is not None:
            workspace = getattr(request.state, "workspace", None)
            _journal_refusal(request, err.code, project_id=project.id,
                             workspace_id=getattr(workspace, "id", None))
        raise


def infra_key(role: str) -> str:
    """The project key an `infra` route needs while its workspace holds one project."""
    _, _, key = role.partition("+perm:")
    return key or "project.write"


def require_infra(request: Request, role: str) -> None:
    """`infra` / `infra+perm:<key>` (design §5.3): a write to workspace-shared
    infrastructure. With one active project in the workspace it is that project's
    `<key>` (project.write by default) — today's behaviour for every single-project
    console; with several, only an administrator may change what all of them share."""
    from app.core.db import SessionLocal
    from app.services import access

    workspace = getattr(request.state, "workspace", None)
    if workspace is None:  # an infra route must be workspace-scoped (drift test)
        require_admin(request)
        return
    db = SessionLocal()
    try:
        active = [b for b in access.workspace_bindings(db, workspace.id) if b.status == "active"]
        lowered = (len(active) > 1 and _lowerable(request)
                   and not require_identity(request).is_admin
                   and _infra_lowered(db, workspace.id))
    finally:
        db.close()
    if lowered:
        # §10.6-6: every project here is isolated with its Cedar rules in force, so a
        # project's own Connection-bound targets are reachable only by that project —
        # the project's key decides, and another project's target is not found
        require_permission(request, infra_key(role))
        _require_own_target(request, workspace.id)
        return
    if len(active) > 1 and not require_identity(request).is_admin and _project_skill_route(
        request, workspace.id
    ):
        # a skill record belongs to the project that registered it: that project's
        # members manage it with the project key; MCP records, unowned / system
        # skills and the bulk sources stay administrator-only
        require_permission(request, infra_key(role))
        return
    if len(active) > 1:
        identity = require_identity(request)
        if not identity.is_admin:
            raise AppError(
                "auth.infra_admin_required",
                "this workspace holds several projects, so its shared infrastructure "
                "(governance, registry catalog, gateway targets…) is changed by an "
                "administrator only",
                {"projects": len(active)},
                status_code=403,
            )
        return
    require_permission(request, infra_key(role))


# §10.6-6 (R10-gw): the `infra` routes that become the project's own once Cedar is per
# project — creating / deleting the project's Connection-bound gateway targets (and,
# appended below, its Lambda targets). The gateway's baseline policies and guardrails
# stay administrator-only in a multi-project workspace.
INFRA_LOWERED_WHEN_ISOLATED: frozenset[tuple[str, str]] = frozenset({
    ("POST", "/api/identity/gateway-targets"),
    ("DELETE", "/api/identity/gateway-targets/{target_id}"),
})


def _route_key(request: Request) -> tuple[str, str]:
    route: Any = request.scope.get("route")
    return request.method, getattr(route, "path_format", None) or request.url.path


# Registry Agent-skill records are project-owned (`resource_creators` kind
# `registry_skill`). Creating one is a project write (the router refuses any other
# record type on this path, `request.state.project_skill_only`); changing one is a
# project write only for a record the current project owns.
PROJECT_SKILL_CREATE: frozenset[tuple[str, str]] = frozenset({
    ("POST", "/api/registry/records"),
    ("POST", "/api/registry/skills/import"),
})
PROJECT_SKILL_RECORD: frozenset[tuple[str, str]] = frozenset({
    ("PUT", "/api/registry/records/{record_id}"),
    ("DELETE", "/api/registry/records/{record_id}"),
    ("POST", "/api/registry/records/{record_id}/action"),
    ("POST", "/api/registry/records/{record_id}/reimport"),
})


def _project_skill_route(request: Request, workspace_id: str) -> bool:
    key = _route_key(request)
    if key in PROJECT_SKILL_CREATE:
        request.state.project_skill_only = True
        return True
    if key not in PROJECT_SKILL_RECORD:
        return False
    record_id = (request.scope.get("path_params") or {}).get("record_id")
    project = getattr(request.state, "project", None)
    if not record_id or project is None:
        return False
    from app.core.db import SessionLocal
    from app.services import creators

    db = SessionLocal()
    try:
        return creators.owner_project(
            db, workspace_id, creators.REGISTRY_SKILL, str(record_id)) == project.id
    finally:
        db.close()


def _lowerable(request: Request) -> bool:
    return _route_key(request) in INFRA_LOWERED_WHEN_ISOLATED


def _infra_lowered(db: Any, workspace_id: str) -> bool:
    from app.services import project_gateway

    return project_gateway.infra_lowered(db, workspace_id)


def _require_own_target(request: Request, workspace_id: str) -> None:
    target_id = (request.scope.get("path_params") or {}).get("target_id")
    if not target_id:
        return
    from app.core.db import SessionLocal
    from app.core.project_scope import project_context
    from app.services import project_gateway

    # the policy runs before `bind_request_project` publishes the request's project
    project = getattr(request.state, "project", None)
    db = SessionLocal()
    try:
        with project_context(project.id if project is not None else None):
            project_gateway.require_own_target(db, workspace_id, str(target_id))
    finally:
        db.close()


# ---- P1F-admin: administrator management (design projects §7.3, §7.4, §7.10) ----
# Hub-global like the rest of account management: the forced logout writes the
# account row, the admin list reads accounts — neither operates inside a workspace.
ROUTE_POLICY.update({
    ("POST", "/api/users/{user_id}/sessions/revoke"): ADMIN,
    ("GET", "/api/admins"): ADMIN,
})
HUB_GLOBAL_PREFIXES = (*HUB_GLOBAL_PREFIXES, "/api/admins")
WORKSPACE_EXEMPT = WORKSPACE_EXEMPT | {
    ("POST", "/api/users/{user_id}/sessions/revoke"),
    ("GET", "/api/admins"),
}

# ---- network profiles (design projects §12.11, V12-model) ----
# A profile is workspace infrastructure an administrator keeps; reads run inside the
# request's project (a member sees the project's allowed set, an admin all of them).
# Writes and the per-project allowed set are ADMIN and project-exempt — the allowed
# set names its project in the path. Preflight / probe routes arrive with V12-check.
ROUTE_POLICY.update({
    ("GET", "/api/network-profiles"): MEMBER,
    ("GET", "/api/network-profiles/{profile_id}"): MEMBER,
    ("POST", "/api/network-profiles"): ADMIN,
    ("PATCH", "/api/network-profiles/{profile_id}"): ADMIN,
    ("DELETE", "/api/network-profiles/{profile_id}"): ADMIN,
    ("GET", "/api/network-profiles/projects/{project_id}"): ADMIN,
    ("PUT", "/api/network-profiles/projects/{project_id}"): ADMIN,
})
PROJECT_EXEMPT = PROJECT_EXEMPT | frozenset({
    ("POST", "/api/network-profiles"),
    ("PATCH", "/api/network-profiles/{profile_id}"),
    ("DELETE", "/api/network-profiles/{profile_id}"),
    ("GET", "/api/network-profiles/projects/{project_id}"),
    ("PUT", "/api/network-profiles/projects/{project_id}"),
})

# ---- departments (design projects §7.10) ----
# Hub-global like the rest of project structure (department → project → member; the
# former project groups are retired). Every write is structure, so ADMIN
# (`access.can_manage_structure`); the department list is MEMBER — its names label
# the project switcher's groups (§2.7) and it grants no access.
PROJECT_STRUCTURE_ROUTES: dict[tuple[str, str], str] = {
    ("GET", "/api/departments"): MEMBER,
    ("POST", "/api/departments"): ADMIN,
    ("PATCH", "/api/departments/{department_id}"): ADMIN,
    ("POST", "/api/departments/{department_id}/archive"): ADMIN,
    ("POST", "/api/departments/{department_id}/unarchive"): ADMIN,
    ("DELETE", "/api/departments/{department_id}"): ADMIN,  # only while unused
}
ROUTE_POLICY.update(PROJECT_STRUCTURE_ROUTES)
HUB_GLOBAL_PREFIXES = (*HUB_GLOBAL_PREFIXES, "/api/departments")
WORKSPACE_EXEMPT = WORKSPACE_EXEMPT | frozenset(PROJECT_STRUCTURE_ROUTES)

# ---- P1F-users: user management with projects (design projects §7.4–§7.6, §7.10) ----
# Account management stays ADMIN and hub-global (`/api/users` prefix): the account's
# detail and effective access, and the bulk "add to project" that writes the same
# `project_members` rows as `PUT /api/projects/{id}/members`.
ROUTE_POLICY.update({
    ("GET", "/api/users/{user_id}"): ADMIN,
    ("GET", "/api/users/{user_id}/access"): ADMIN,
    ("POST", "/api/users/bulk/projects"): ADMIN,
})
WORKSPACE_EXEMPT = WORKSPACE_EXEMPT | {
    ("GET", "/api/users/{user_id}"),
    ("GET", "/api/users/{user_id}/access"),
    ("POST", "/api/users/bulk/projects"),
}

# ---- CS-4: disconnect-safe console chat turns (chat-streaming §5.2) ----
# Following and stopping a turn are part of the member's own interaction with the agent,
# so they need what sending it needs; both are project-scoped, and a turn of another
# project answers 404 like a missing one.
ROUTE_POLICY.update({
    ("GET", "/api/chat/{agent_id}/turns/{turn_id}/events"): PERM_PROJECT_USE,
    ("POST", "/api/chat/{agent_id}/turns/{turn_id}/stop"): PERM_PROJECT_USE,
})

# ---- network preflight / probe / settings (design projects §12.4, §12.5, §12.7, V12-check) ----
# All administrator routes on workspace infrastructure, project-exempt like the other
# profile writes: the check and the probe read and write one profile of the request's
# workspace (another workspace's id answers 404); settings / enforcement are the
# workspace's network switches.
ROUTE_POLICY.update({
    ("POST", "/api/network-profiles/{profile_id}/check"): ADMIN,
    ("POST", "/api/network-profiles/{profile_id}/probe"): ADMIN,
    ("GET", "/api/network-profiles/settings"): ADMIN,
    ("PUT", "/api/network-profiles/settings"): ADMIN,
    ("GET", "/api/network-profiles/enforcement"): ADMIN,
})
PROJECT_EXEMPT = PROJECT_EXEMPT | frozenset({
    ("POST", "/api/network-profiles/{profile_id}/check"),
    ("POST", "/api/network-profiles/{profile_id}/probe"),
    ("GET", "/api/network-profiles/settings"),
    ("PUT", "/api/network-profiles/settings"),
    ("GET", "/api/network-profiles/enforcement"),
})

# ---- MCP server tool reviews (design projects §15.6, §15.13, M15-review) ----
# Inside the server's project: reading reviews is participation, deciding one is the
# project key `mcp.approve` (the deployer never decides their own — object-level SoD
# in services/mcp_reviews.py), and re-syncing the live target to look for drift is an
# ordinary project write.
PERM_MCP_APPROVE = "perm:mcp.approve"
ROUTE_POLICY.update({
    ("GET", "/api/mcp-servers/{agent_id}/reviews"): PERM_PROJECT_USE,
    ("GET", "/api/mcp-servers/{agent_id}/reviews/{review_id}"): PERM_PROJECT_USE,
    ("POST", "/api/mcp-servers/{agent_id}/reviews/{review_id}/approve"): PERM_MCP_APPROVE,
    ("POST", "/api/mcp-servers/{agent_id}/reviews/{review_id}/reject"): PERM_MCP_APPROVE,
    ("POST", "/api/mcp-servers/{agent_id}/reviews/check"): PERM_PROJECT_WRITE,
})

# ---- agent network (design projects §12.11, V12-deploy) ----
# A read of the agent like /identity and /mcp: plain member, inside the agent's project
# (another project's agent answers 404 through the project-scoped agent lookup).
ROUTE_POLICY.update({
    ("GET", "/api/agents/{agent_id}/network"): MEMBER,
})

# ---- MCP server operations, Lambda / adopted tool targets (projects §15.7, §15.8,
# §15.12, §15.13, §15.15, M15-rest) ----
# A health check re-syncs the server's target: an ordinary project write. Rollback is
# classified as participation and the handler demands the real key — release.sign when
# the server's live endpoint is a gated agent's, agents.deploy for DEFAULT (one route,
# two keys). The Cedar scaffold is mcp.approve; a mode switch in the same request asks
# `require_infra(infra+perm:mcp.approve)` in the handler. Lambda targets change the
# shared Gateway: infra (§10.6-6 lowers them to project level once Cedar is per
# project). Adopting a hand-made target is mcp.approve here and infra in the handler
# (`infra+perm:<project key>` is not a table role). Reviews follow the MCP-server ones.
INFRA_AGENT_DEPLOY = "infra+perm:agents.deploy"
ROUTE_POLICY.update({
    ("POST", "/api/mcp-servers/{agent_id}/health"): PERM_PROJECT_WRITE,
    ("POST", "/api/mcp-servers/{agent_id}/rollback"): PERM_PROJECT_USE,
    ("POST", "/api/mcp-servers/{agent_id}/policies/scaffold"): PERM_MCP_APPROVE,
    ("GET", "/api/gateway-tools/lambda"): PERM_PROJECT_USE,
    ("POST", "/api/gateway-tools/lambda"): INFRA_AGENT_DEPLOY,
    ("DELETE", "/api/gateway-tools/lambda/{target_id}"): INFRA_AGENT_DEPLOY,
    ("GET", "/api/gateway-tools/external"): PERM_PROJECT_USE,
    ("POST", "/api/gateway-tools/external/{target_id}/adopt"): PERM_MCP_APPROVE,
    ("GET", "/api/gateway-tools/{tool_id}/reviews"): PERM_PROJECT_USE,
    ("GET", "/api/gateway-tools/{tool_id}/reviews/{review_id}"): PERM_PROJECT_USE,
    ("POST", "/api/gateway-tools/{tool_id}/reviews/{review_id}/approve"): PERM_MCP_APPROVE,
    ("POST", "/api/gateway-tools/{tool_id}/reviews/{review_id}/reject"): PERM_MCP_APPROVE,
})
# what production serves: a member may not roll a prod server back or change the prod
# Gateway's Lambda tools (an admin's change is journaled), like a release rollback
PROD_PROTECTED = PROD_PROTECTED | frozenset({
    ("POST", "/api/mcp-servers/{agent_id}/rollback"),
    ("POST", "/api/gateway-tools/lambda"),
    ("DELETE", "/api/gateway-tools/lambda/{target_id}"),
})

# ---- project runtime isolation (design projects §10.2, §10.13, R10-model) ----
# Hub-global under /api/projects like the rest of project administration: the project
# is the path's, the workspace a query / body field the project must be bound to. Reads
# are MEMBER and answer 404 to non-members in the handler (a member sees only the
# workspaces they reach the project in); switching isolation and re-running a
# provisioning job change AWS state for the project: administrators.
PROJECT_ISOLATION_ROUTES: dict[tuple[str, str], str] = {
    ("GET", "/api/projects/{project_id}/isolation"): MEMBER,
    ("PUT", "/api/projects/{project_id}/isolation"): ADMIN,
    ("GET", "/api/projects/{project_id}/resources"): MEMBER,
    ("GET", "/api/projects/{project_id}/provisioning"): MEMBER,
    ("POST", "/api/projects/{project_id}/provision"): ADMIN,
}
ROUTE_POLICY.update(PROJECT_ISOLATION_ROUTES)
WORKSPACE_EXEMPT = WORKSPACE_EXEMPT | frozenset(PROJECT_ISOLATION_ROUTES)


# ---- project audit view and export (design projects §11.6, G11-audit) ----
# Hub-global under /api/projects like the rest of project administration: the path's
# project, the project key `project.audit` (a non-member 404, a member without it 403).
PPERM_PROJECT_AUDIT = "pperm:project.audit"
PROJECT_AUDIT_ROUTES: dict[tuple[str, str], str] = {
    ("GET", "/api/projects/{project_id}/audit"): PPERM_PROJECT_AUDIT,
    ("GET", "/api/projects/{project_id}/audit/export"): PPERM_PROJECT_AUDIT,
}
ROUTE_POLICY.update(PROJECT_AUDIT_ROUTES)
WORKSPACE_EXEMPT = WORKSPACE_EXEMPT | frozenset(PROJECT_AUDIT_ROUTES)


def _journal_refusal(
    request: Request, code: str, *, project_id: str | None, workspace_id: str | None = None,
) -> None:
    """`outcome = refused` in the refused project's journal (§11.6): the route's
    template as the action (like the prod break-glass rows), the concrete path and
    the refusal code as the target. Best-effort (`record_refusal`)."""
    try:
        identity = require_identity(request)
    except AppError:
        return  # no caller to name
    route: Any = request.scope.get("route")
    path_format = getattr(route, "path_format", None) or request.url.path
    record_refusal(
        actor=identity.username, actor_user_id=getattr(identity, "user_id", None),
        action=f"{request.method} {path_format}", target=f"{request.url.path} [{code}]",
        project_id=project_id, workspace_id=workspace_id,
    )

# ---- project lifecycle: export, deletion request, cooling-off, purge (projects design
# §11.5, G11-life) ----
# Structure, like archive: administrators only (the steward two-person rule arrives with
# §11.4). Hub-global — the project is the one in the path. Another project's export id
# answers 404 in the handler.
PROJECT_LIFECYCLE_ROUTES: dict[tuple[str, str], str] = {
    ("GET", "/api/projects/{project_id}/lifecycle"): ADMIN,
    ("POST", "/api/projects/{project_id}/export"): ADMIN,
    ("GET", "/api/projects/{project_id}/exports/{export_id}"): ADMIN,
    ("POST", "/api/projects/{project_id}/delete-request"): ADMIN,
    ("POST", "/api/projects/{project_id}/delete-request/cancel"): ADMIN,
    # `?dry_run=true` lists what would go; otherwise starts the `project_purge` job
    ("POST", "/api/projects/{project_id}/purge"): ADMIN,
}
ROUTE_POLICY.update(PROJECT_LIFECYCLE_ROUTES)
WORKSPACE_EXEMPT = WORKSPACE_EXEMPT | frozenset(PROJECT_LIFECYCLE_ROUTES)


# ---- department administrators (projects design §11.1, G11-dept) ----
# The design's `dadmin` policy, kept as an overlay so each route's entry above stays
# what everyone else needs (ADMIN, or `pperm:project.members` — "dadmin or the
# existing policy"): an administrator passes as before; a department administrator
# passes when the route's target belongs to a department they administer. The value
# says where the target's department is read:
#   project     — the path's `{project_id}` (projects.department_id)
#   department  — the path's `{department_id}`
#   query       — the `department_id` query parameter (a list filtered to it)
#   any         — the body names it: the caller administers some department, and the
#                 handler checks the body's (`services/projects.create_project`)
# Another department's project answers 404 (its existence is not theirs to
# learn); a caller who administers no department meets the route's own entry.
# Appointing department administrators and opening workspaces stay ADMIN.
DEPARTMENT_ADMIN_ROUTES: dict[tuple[str, str], str] = {
    ("POST", "/api/projects"): "any",
    ("PATCH", "/api/projects/{project_id}"): "project",
    ("POST", "/api/projects/{project_id}/archive"): "project",
    ("POST", "/api/projects/{project_id}/unarchive"): "project",
    ("PUT", "/api/projects/{project_id}/workspaces"): "project",  # open workspaces only
    ("PUT", "/api/projects/{project_id}/members"): "project",
    ("DELETE", "/api/projects/{project_id}/members/{user_id}"): "project",
    ("GET", "/api/projects/{project_id}/candidates"): "project",
    ("PUT", "/api/projects/{project_id}/roles/{role_key}"): "project",
    ("DELETE", "/api/projects/{project_id}/roles/{role_key}"): "project",
    ("POST", "/api/departments/{department_id}/invitations"): "department",
}
DEPARTMENT_ROUTES: dict[tuple[str, str], str] = {
    ("GET", "/api/departments/{department_id}/admins"): ADMIN,
    ("PUT", "/api/departments/{department_id}/admins"): ADMIN,
    ("GET", "/api/departments/{department_id}/workspaces"): ADMIN,
    ("PUT", "/api/departments/{department_id}/workspaces"): ADMIN,
    # + `settings.can_create_users` for a department administrator (in the handler)
    ("POST", "/api/departments/{department_id}/invitations"): ADMIN,
}
ROUTE_POLICY.update(DEPARTMENT_ROUTES)
WORKSPACE_EXEMPT = WORKSPACE_EXEMPT | frozenset(DEPARTMENT_ROUTES)


def department_admin_passes(request: Request, path_format: str) -> bool:
    """The `dadmin` overlay: True when a department administrator may call this route
    on this target; False to fall through to the route's own entry; 404 for another
    department's project."""
    lookup = "GET" if request.method == "HEAD" else request.method
    where = DEPARTMENT_ADMIN_ROUTES.get((lookup, path_format))
    if where is None:
        return False
    identity = require_identity(request)
    if identity.is_admin or not getattr(identity, "user_id", None):
        return False  # an administrator meets ADMIN / pperm as before
    from app.core.db import SessionLocal
    from app.core.errors import NotFoundError
    from app.core.project_scope import project_context
    from app.services import access

    params = request.path_params
    db = SessionLocal()
    try:
        with project_context(None):
            managed = access.managed_departments(db, identity)
            if not managed:
                return False
            status = None
            if where == "project":
                found = db.execute(
                    text("SELECT department_id, status FROM projects WHERE id = :p"),
                    {"p": str(params.get("project_id") or "")},
                ).first()
                target, status = (found[0], found[1]) if found else (None, None)
            elif where == "department":
                target = str(params.get("department_id") or "")
            elif where == "query":
                target = request.query_params.get("department_id") or None
            else:  # any
                return True
    finally:
        db.close()
    if target in managed:
        if (where == "project" and status not in (None, "active")
                and request.method not in access.READ_METHODS
                and not path_format.endswith(("/archive", "/unarchive"))):
            raise AppError("project.archived",
                           f"project '{params.get('project_id')}' is archived",
                           {"project_id": params.get("project_id")}, status_code=409)
        return True
    if where == "project":
        # a member with the key still passes `pperm:` for a project outside the
        # department; for anyone else another department's project does not exist
        if ROUTE_POLICY[(lookup, path_format)].startswith(PPERM_PREFIX):
            return False
        pid = str(params.get("project_id") or "")
        raise NotFoundError("project.not_found", f"project '{pid}' not found",
                            {"project_id": pid})
    return False

# ---- storage catalog (design storage-mounts §4, §5, §9, SM-2) ----
# An entry is workspace infrastructure an administrator keeps, like a network profile:
# reads run inside the request's project (a member sees the project's allowed set, an
# admin every entry with the shared view); registering, checking, editing, archiving
# and the per-project allowed set are ADMIN and project-exempt — the allowed set names
# its project in the path. The wizard's preflight reads AWS for the request's project
# and writes nothing: participation. The agent's storage card is a read of the agent.
ROUTE_POLICY.update({
    ("GET", "/api/storage-mounts"): MEMBER,
    ("GET", "/api/storage-mounts/{storage_id}"): MEMBER,
    ("GET", "/api/storage-mounts/{storage_id}/usage"): MEMBER,
    ("GET", "/api/storage-mounts/agents/{agent_id}"): MEMBER,
    ("POST", "/api/storage-mounts/preflight"): PERM_PROJECT_USE,
    ("POST", "/api/storage-mounts"): ADMIN,
    ("POST", "/api/storage-mounts/{storage_id}/check"): ADMIN,
    ("PATCH", "/api/storage-mounts/{storage_id}"): ADMIN,
    ("DELETE", "/api/storage-mounts/{storage_id}"): ADMIN,
    ("GET", "/api/storage-mounts/projects/{project_id}"): ADMIN,
    ("PUT", "/api/storage-mounts/projects/{project_id}"): ADMIN,
})
PROJECT_EXEMPT = PROJECT_EXEMPT | frozenset({
    ("POST", "/api/storage-mounts"),
    ("POST", "/api/storage-mounts/{storage_id}/check"),
    ("PATCH", "/api/storage-mounts/{storage_id}"),
    ("DELETE", "/api/storage-mounts/{storage_id}"),
    ("GET", "/api/storage-mounts/projects/{project_id}"),
    ("PUT", "/api/storage-mounts/projects/{project_id}"),
})

# ---- online console update (docs/design online-update §2.6, OU-1) ----
# Hub-global: the console's own code, not any workspace's or project's data. Everything
# is ADMIN (it builds and restarts the platform) except the version read the footer
# shows every member. The source repository / branch is configuration, never a body.
SYSTEM_UPDATE_ROUTES: dict[tuple[str, str], str] = {
    ("GET", "/api/system/version"): MEMBER,
    ("GET", "/api/system/update"): ADMIN,
    ("POST", "/api/system/update/check"): ADMIN,
    ("POST", "/api/system/update/prepare"): ADMIN,
    ("POST", "/api/system/update/rollback"): ADMIN,
    ("POST", "/api/system/update/{update_id}/schedule"): ADMIN,
    ("POST", "/api/system/update/{update_id}/cancel"): ADMIN,
    ("GET", "/api/system/update/{update_id}/log"): ADMIN,
}
ROUTE_POLICY.update(SYSTEM_UPDATE_ROUTES)
HUB_GLOBAL_PREFIXES = (*HUB_GLOBAL_PREFIXES, "/api/system")
WORKSPACE_EXEMPT = WORKSPACE_EXEMPT | frozenset(SYSTEM_UPDATE_ROUTES)


# ---- project quotas and the monthly budget (projects design §11.3, G11-quota) ----
# Hub-global under /api/projects like the rest of project administration. The read
# (usage against limits, the budget's month-to-date spend, remaining and reset date)
# is every member's — a non-member gets 404 in the handler; the project's department
# administrator sees it too. Setting quotas and the budget is a platform
# administrator's only: deliberately NO `dadmin` overlay (a department administrator
# meets plain ADMIN and is refused), and the handler checks again.
PROJECT_QUOTA_ROUTES: dict[tuple[str, str], str] = {
    ("GET", "/api/projects/{project_id}/quotas"): MEMBER,
    ("PUT", "/api/projects/{project_id}/quotas"): ADMIN,
}
ROUTE_POLICY.update(PROJECT_QUOTA_ROUTES)
WORKSPACE_EXEMPT = WORKSPACE_EXEMPT | frozenset(PROJECT_QUOTA_ROUTES)

# ---- department budgets and the cost dashboards (projects §11.3, DBUD) ----
# Budgets are project budgets; a department's budget only caps what its projects may
# be allocated (`services/department_budgets.py`). Hub-global, like the rest of
# project structure:
#   PUT  /api/projects/{id}/budget      the project's budget + overrun switch — the
#        `budget.manage` key at the organization level, or being the project's
#        department administrator (no key; `dadmin` overlay "project"); checked in the handler
#   PUT  /api/departments/{id}/budget   the department's ceiling + switch —
#        `budget.manage` at the organization level (admin / steward), no overlay
#   GET  /api/departments/{id}/budget   the department cost page — platform
#        administrators and that department's administrators (overlay
#        "department"); budget figures are structure like a project's quotas read
#   GET  /api/budgets/platform          the platform cost page (every project, the
#        platform / unattributed line) — administrators, data like `/api/costs`
#   GET  /api/budgets/me                which of those views the caller can open, and
#        its department alerts — any account (the handler answers only its own)
DEPARTMENT_BUDGET_ROUTES: dict[tuple[str, str], str] = {
    ("PUT", "/api/projects/{project_id}/budget"): ADMIN,
    ("GET", "/api/departments/{department_id}/budget"): ADMIN,
    ("PUT", "/api/departments/{department_id}/budget"): ADMIN,
    ("GET", "/api/budgets/platform"): ADMIN,
    ("GET", "/api/budgets/me"): MEMBER,
}
ROUTE_POLICY.update(DEPARTMENT_BUDGET_ROUTES)
HUB_GLOBAL_PREFIXES = (*HUB_GLOBAL_PREFIXES, "/api/budgets")
WORKSPACE_EXEMPT = WORKSPACE_EXEMPT | frozenset(DEPARTMENT_BUDGET_ROUTES)
DEPARTMENT_ADMIN_ROUTES.update({
    ("PUT", "/api/projects/{project_id}/budget"): "project",
    ("GET", "/api/departments/{department_id}/budget"): "department",
})


# ---- steward: the structure-only platform administrator (projects design §11.4,
# G11-steward) ----
# Every `ADMIN` route is one of two kinds, and a steward (`users.role = steward`:
# `access.can_manage_structure` True, `access.has_data_bypass` False) passes only the
# first:
#   ADMIN       structure — accounts, departments, projects, bindings,
#               workspaces (with bootstrap), network / storage catalogs, platform
#               settings and content;
#   ADMIN_DATA  data — anything that shows or moves a project's business data
#               (data audit events, cost detail, the fleet's agents, the inbox's
#               business items, exports, agent transfer) or acts inside a workspace's
#               runtime (installing presets, guardrails, consent portal, alerts) —
#               and the switches a steward must not flip for itself.
# The table keeps the value `ADMIN` for both (a member meets the same 403 either way);
# `ADMIN_SPLIT` is the classification, and `tests/test_route_policy.py` requires every
# `ADMIN` route in it — a new administrator route cannot ship without choosing. One
# missing at runtime is treated as `ADMIN_DATA` (closed for a steward).
ADMIN_DATA = "admin:data"

# the steward's own surface: owner notices and two-person confirmations are any
# account's (each handler answers only what is the caller's); the two-person switch
# is the administrators' — a steward does not get to loosen its own rule
ROUTE_POLICY.update({
    ("GET", "/api/steward/notices"): MEMBER,
    ("POST", "/api/steward/notices/{notice_id}/ack"): MEMBER,
    ("GET", "/api/steward/pending"): MEMBER,
    ("POST", "/api/steward/pending/{pending_id}/confirm"): MEMBER,
    ("POST", "/api/steward/pending/{pending_id}/reject"): MEMBER,
    ("GET", "/api/steward/settings"): ADMIN,
    ("PUT", "/api/steward/settings"): ADMIN,
})
HUB_GLOBAL_PREFIXES = (*HUB_GLOBAL_PREFIXES, "/api/steward")
WORKSPACE_EXEMPT = WORKSPACE_EXEMPT | {
    ("GET", "/api/steward/notices"),
    ("POST", "/api/steward/notices/{notice_id}/ack"),
    ("GET", "/api/steward/pending"),
    ("POST", "/api/steward/pending/{pending_id}/confirm"),
    ("POST", "/api/steward/pending/{pending_id}/reject"),
    ("GET", "/api/steward/settings"),
    ("PUT", "/api/steward/settings"),
}

_STRUCTURE_ROUTES = (
    # administrators and accounts (§7.3, §7.4); the handlers keep platform roles to
    # administrators (`routers/users.py`)
    ("GET", "/api/admins"),
    ("GET", "/api/users"),
    ("POST", "/api/users"),
    ("GET", "/api/users/stats"),
    ("POST", "/api/users/bulk/projects"),
    ("GET", "/api/users/{user_id}"),
    ("PATCH", "/api/users/{user_id}"),
    ("DELETE", "/api/users/{user_id}"),
    ("GET", "/api/users/{user_id}/access"),
    ("POST", "/api/users/{user_id}/sessions/revoke"),
    # departments (§7.10, §11.1)
    ("POST", "/api/departments"),
    ("PATCH", "/api/departments/{department_id}"),
    ("DELETE", "/api/departments/{department_id}"),
    ("POST", "/api/departments/{department_id}/archive"),
    ("POST", "/api/departments/{department_id}/unarchive"),
    ("GET", "/api/departments/{department_id}/admins"),
    ("PUT", "/api/departments/{department_id}/admins"),
    ("GET", "/api/departments/{department_id}/workspaces"),
    ("PUT", "/api/departments/{department_id}/workspaces"),
    ("POST", "/api/departments/{department_id}/invitations"),
    # projects: create, bind, archive, isolation and provisioning, lifecycle status
    ("POST", "/api/projects"),
    ("POST", "/api/projects/{project_id}/archive"),
    ("POST", "/api/projects/{project_id}/unarchive"),
    ("PUT", "/api/projects/{project_id}/workspaces"),
    ("PUT", "/api/projects/{project_id}/isolation"),
    ("POST", "/api/projects/{project_id}/provision"),
    ("GET", "/api/projects/{project_id}/lifecycle"),
    ("POST", "/api/projects/{project_id}/delete-request/cancel"),
    # workspaces, with bootstrap (§11.4 lists them)
    ("POST", "/api/workspaces"),
    ("GET", "/api/workspaces/hub-identity"),
    ("POST", "/api/workspaces/preflight"),
    ("PATCH", "/api/workspaces/{workspace_id}"),
    ("DELETE", "/api/workspaces/{workspace_id}"),
    ("GET", "/api/workspaces/{workspace_id}/bootstrap"),
    ("POST", "/api/workspaces/{workspace_id}/bootstrap"),
    ("GET", "/api/workspaces/{workspace_id}/grants"),
    ("PUT", "/api/workspaces/{workspace_id}/grants"),
    ("POST", "/api/workspaces/{workspace_id}/observability/repair"),
    ("POST", "/api/workspaces/{workspace_id}/purge"),
    ("GET", "/api/workspaces/{workspace_id}/role-template"),
    ("GET", "/api/workspaces/{workspace_id}/settings/visibility"),
    ("PUT", "/api/workspaces/{workspace_id}/settings/visibility"),
    # network and storage catalogs: what a project may use, not what it holds
    ("POST", "/api/network-profiles"),
    ("PATCH", "/api/network-profiles/{profile_id}"),
    ("DELETE", "/api/network-profiles/{profile_id}"),
    ("POST", "/api/network-profiles/{profile_id}/check"),
    ("POST", "/api/network-profiles/{profile_id}/probe"),
    ("GET", "/api/network-profiles/projects/{project_id}"),
    ("PUT", "/api/network-profiles/projects/{project_id}"),
    ("GET", "/api/network-profiles/settings"),
    ("PUT", "/api/network-profiles/settings"),
    ("GET", "/api/network-profiles/enforcement"),
    ("POST", "/api/storage-mounts"),
    ("POST", "/api/storage-mounts/{storage_id}/check"),
    ("PATCH", "/api/storage-mounts/{storage_id}"),
    ("DELETE", "/api/storage-mounts/{storage_id}"),
    ("GET", "/api/storage-mounts/projects/{project_id}"),
    ("PUT", "/api/storage-mounts/projects/{project_id}"),
    # platform settings and installation-wide content
    ("GET", "/api/models/settings"),
    ("PUT", "/api/models/settings/{field}"),
    ("DELETE", "/api/models/settings/{field}"),
    ("POST", "/api/model-probe"),
    ("POST", "/api/announcements"),
    ("GET", "/api/announcements/manage"),
    ("GET", "/api/announcements/{announcement_id}"),
    ("PUT", "/api/announcements/{announcement_id}"),
    ("DELETE", "/api/announcements/{announcement_id}"),
    ("POST", "/api/announcements/{announcement_id}/publish"),
    ("POST", "/api/announcements/{announcement_id}/unpublish"),
    ("GET", "/api/videos/manage"),
    ("POST", "/api/videos/manage"),
    ("GET", "/api/videos/manage/{video_id}"),
    ("PUT", "/api/videos/manage/{video_id}"),
    ("DELETE", "/api/videos/manage/{video_id}"),
    ("POST", "/api/videos/manage/{video_id}/publish"),
    ("POST", "/api/videos/manage/{video_id}/unpublish"),
    ("GET", "/api/steward/settings"),
    # read across the platform, each handler keeping a steward to structure: the
    # journal's structure events (`audit.steward_visible`), the fleet's counts (it
    # names no agent), the inbox's account and workspace items
    ("GET", "/api/audit"),
    ("GET", "/api/fleet"),
    ("GET", "/api/inbox"),
)
_DATA_ROUTES = (
    # a golden set (holdout included) is the project's evaluation data
    ("DELETE", "/api/golden-sets/{dataset_id}"),
    ("GET", "/api/costs"),
    ("GET", "/api/costs/month-to-date"),
    # cost detail (until its router keeps a steward to totals), time-to-first-agent
    ("GET", "/api/overview/ttfa"),
    # a project's data leaves or moves: the export bundle, an agent transfer; deletion
    # (a request purges on its own after the cooling-off) until §11.5's steward
    # two-person purge lands — cancelling one stays structure
    ("POST", "/api/projects/{project_id}/export"),
    ("GET", "/api/projects/{project_id}/exports/{export_id}"),
    ("POST", "/api/projects/{project_id}/transfer"),
    ("POST", "/api/projects/{project_id}/delete-request"),
    ("POST", "/api/projects/{project_id}/purge"),
    # inside a workspace's runtime: agents, memories, guardrails, identity, alerts
    ("POST", "/api/system-agents/{preset_key}/install"),
    ("POST", "/api/system-agents/{preset_key}/skill-registration"),
    ("DELETE", "/api/system-agents/{preset_key}"),
    # the Agent IDE's coding runtime + role, and the connected COA deployment (its
    # Connection and console workload identity): workspace runtime, like presets
    ("POST", "/api/coding/runtime/install"),
    ("DELETE", "/api/coding/runtime"),
    ("PUT", "/api/context-layer"),
    ("DELETE", "/api/context-layer"),
    # the managed COA gateway (an AgentCore Gateway + target + workload identity)
    ("PUT", "/api/context-layer/gateway"),
    ("POST", "/api/context-layer/gateway/refresh"),
    ("DELETE", "/api/context-layer/gateway"),
    ("POST", "/api/memory/resources/{memory_id}/adopt"),
    ("POST", "/api/governance/guardrail"),
    ("PUT", "/api/release-policies/{workspace_id}"),
    ("POST", "/api/identity/consent-portal"),
    ("DELETE", "/api/identity/consent-portal"),
    ("POST", "/api/alerts"),
    ("POST", "/api/alerts/evaluate"),
    ("PATCH", "/api/alerts/{rule_id}"),
    ("DELETE", "/api/alerts/{rule_id}"),
    # the steward's own two-person switch
    ("PUT", "/api/steward/settings"),
)
ADMIN_SPLIT: dict[tuple[str, str], str] = {
    **dict.fromkeys(_STRUCTURE_ROUTES, ADMIN),
    **dict.fromkeys(_DATA_ROUTES, ADMIN_DATA),
}
# `pperm:` keys that are structure: a steward manages any project's members and
# custom roles without being a member (§11.1 matrix); every other project key is data
STRUCTURE_PROJECT_KEYS = frozenset({"project.members"})


def admin_kind(method: str, path_format: str) -> str:
    """`ADMIN` (structure) or `ADMIN_DATA` for an `ADMIN` route; unknown is data."""
    lookup = "GET" if method == "HEAD" else method
    return ADMIN_SPLIT.get((lookup, path_format), ADMIN_DATA)


def require_admin_kind(request: Request, method: str, path_format: str) -> None:
    """An `ADMIN` route: administrators always; a steward only on structure."""
    from app.services import access

    if admin_kind(method, path_format) == ADMIN:
        access.require_structure(request)
    else:
        require_admin(request)


# ---- WIRE-B: routes and lowerings the B8 nodes asked for ----
# R10-kb: moving an isolated project's existing knowledge base onto the project's own
# role, S3 prefix and tag, then dropping the old data source. Project-scoped, not
# PROJECT_EXEMPT: the KB is resolved in the request's project (another project's is
# 404). Administrator data work — it copies the KB's documents.
KB_MIGRATION_ROUTES: dict[tuple[str, str], str] = {
    ("POST", "/api/knowledge-bases/{kb_id}/migrate-to-project"): ADMIN,
    ("POST", "/api/knowledge-bases/{kb_id}/migrate-to-project/finish"): ADMIN,
}
ROUTE_POLICY.update(KB_MIGRATION_ROUTES)
ADMIN_SPLIT.update(dict.fromkeys(KB_MIGRATION_ROUTES, ADMIN_DATA))

# the steward split of the B8 administrator routes (projects §11.4): a project's
# quotas are its structure (a steward sees quotas and counts); the online update
# restarts the console under every project's in-flight work and stays the
# administrators' (online-update §2.6)
ADMIN_SPLIT.update({
    ("PUT", "/api/projects/{project_id}/quotas"): ADMIN,
    # department budgets (DBUD): setting budgets and reading the department page are
    # structure; the platform page reads every workspace's spend: data
    ("PUT", "/api/projects/{project_id}/budget"): ADMIN,
    ("GET", "/api/departments/{department_id}/budget"): ADMIN,
    ("PUT", "/api/departments/{department_id}/budget"): ADMIN,
    ("GET", "/api/budgets/platform"): ADMIN_DATA,
    **{key: ADMIN_DATA for key, role in SYSTEM_UPDATE_ROUTES.items() if role == ADMIN},
})

# R10-gw: console-created Lambda targets are the project's own once Cedar is per
# project, like its Connection-bound targets (`gateway_tools` records their ownership,
# so another project's target is not found)
INFRA_LOWERED_WHEN_ISOLATED = INFRA_LOWERED_WHEN_ISOLATED | frozenset({
    ("POST", "/api/gateway-tools/lambda"),
    ("DELETE", "/api/gateway-tools/lambda/{target_id}"),
})

# ---- SG-1: the storyline (storyline-ux §2.3) ----
# One agent's storyline facts: a cached ledger read of an agent in the caller's
# project (another project's answers 404), participation-level like the telemetry
# status. "My next steps" sits under the hub-global `/api/me`: the handler resolves
# the X-Workspace / X-Project pair itself and reads inside that project only.
STORY_ROUTES: dict[tuple[str, str], str] = {
    ("GET", "/api/agents/{agent_id}/story"): PERM_PROJECT_USE,
    ("GET", "/api/me/next-steps"): MEMBER,
}
ROUTE_POLICY.update(STORY_ROUTES)
WORKSPACE_EXEMPT = WORKSPACE_EXEMPT | {("GET", "/api/me/next-steps")}


# ---- cross-project sharing grants (projects design §11.2, G11-share) ----
# Hub-global under /api/projects like the rest of project administration: the path's
# project is the owner (grant, revoke, the share form's options — `pperm:project.share`,
# a non-member 404) or, for approve / reject, the grantee of a cross-department request
# (its department administrator through the `dadmin` overlay, or an administrator;
# another department's project 404). The list is every member's: both directions, the
# handler answers 404 to a non-member. Approving lets one project's data into another,
# so a steward does not (§11.1 matrix): data.
PPERM_PROJECT_SHARE = "pperm:project.share"
RESOURCE_SHARE_ROUTES: dict[tuple[str, str], str] = {
    ("GET", "/api/projects/{project_id}/shares"): MEMBER,
    ("GET", "/api/projects/{project_id}/shares/options"): PPERM_PROJECT_SHARE,
    ("POST", "/api/projects/{project_id}/shares"): PPERM_PROJECT_SHARE,
    ("DELETE", "/api/projects/{project_id}/shares/{share_id}"): PPERM_PROJECT_SHARE,
    ("POST", "/api/projects/{project_id}/shares/{share_id}/approve"): ADMIN,
    ("POST", "/api/projects/{project_id}/shares/{share_id}/reject"): ADMIN,
}
ROUTE_POLICY.update(RESOURCE_SHARE_ROUTES)
WORKSPACE_EXEMPT = WORKSPACE_EXEMPT | frozenset(RESOURCE_SHARE_ROUTES)
DEPARTMENT_ADMIN_ROUTES.update({
    ("POST", "/api/projects/{project_id}/shares/{share_id}/approve"): "project",
    ("POST", "/api/projects/{project_id}/shares/{share_id}/reject"): "project",
})
ADMIN_SPLIT.update({
    ("POST", "/api/projects/{project_id}/shares/{share_id}/approve"): ADMIN_DATA,
    ("POST", "/api/projects/{project_id}/shares/{share_id}/reject"): ADMIN_DATA,
})

# ---- R10-migrate: moving existing agents into (and out of) an isolated project
# (projects design §10.11, §10.13) ----
# Hub-global like the rest of the isolation routes: the project is the path's, the
# workspace a body / query field it must be bound to. The status and an agent's
# boundary line are every member's (a non-member, another project's agent: 404 in the
# handler); starting, retrying and retiring republish agents and change their roles:
# administrators, structure like switching isolation itself (a steward may).
PROJECT_MIGRATION_ROUTES: dict[tuple[str, str], str] = {
    ("POST", "/api/projects/{project_id}/isolation/migrate"): ADMIN,
    ("GET", "/api/projects/{project_id}/isolation/migration"): MEMBER,
    ("POST", "/api/projects/{project_id}/isolation/migration/{run_id}/retry"): ADMIN,
    ("POST", "/api/projects/{project_id}/isolation/retire"): ADMIN,
    ("GET", "/api/projects/{project_id}/isolation/agents/{agent_id}"): MEMBER,
}
ROUTE_POLICY.update(PROJECT_MIGRATION_ROUTES)
WORKSPACE_EXEMPT = WORKSPACE_EXEMPT | frozenset(PROJECT_MIGRATION_ROUTES)
ADMIN_SPLIT.update({key: ADMIN for key, role in PROJECT_MIGRATION_ROUTES.items()
                    if role == ADMIN})

# ---- IA-1: calling an agent from your own backend (invoke-access §0.6) ----
# The agent's ARNs, least-privilege IAM policy and call samples: a ledger read of an
# agent in the caller's project (another project's answers 404), participation-level
# like invoking it. The project inventory + merged policy sits under the hub-global
# `/api/projects` prefix: `pperm:project.use` on the path project (a non-member 404);
# the handler reads every workspace the project is bound to inside that project.
PPERM_PROJECT_USE = "pperm:project.use"
INVOKE_ACCESS_ROUTES: dict[tuple[str, str], str] = {
    ("GET", "/api/agents/{agent_id}/invoke-access"): PERM_PROJECT_USE,
    ("GET", "/api/projects/{project_id}/invoke-access"): PPERM_PROJECT_USE,
}
ROUTE_POLICY.update(INVOKE_ACCESS_ROUTES)
WORKSPACE_EXEMPT = WORKSPACE_EXEMPT | {("GET", "/api/projects/{project_id}/invoke-access")}

# ---- SSO-1: console single sign-on over OIDC (sso design §2.6) ----
# The login surface is PUBLIC: the providers list feeds the login page, and the OIDC
# redirect leg arrives without a session by design (`auth._OPEN_API_PATTERN` lets it
# through the middleware). Configuring an identity provider is the administrators':
# it decides who may sign in and with which global role (a mapping can grant admin),
# so a steward — who must not raise its own reach — is refused: data.
SSO_ROUTES: dict[tuple[str, str], str] = {
    ("GET", "/api/auth/providers"): PUBLIC,
    ("GET", "/api/auth/oidc/{provider_id}/start"): PUBLIC,
    ("GET", "/api/auth/oidc/{provider_id}/callback"): PUBLIC,
    ("GET", "/api/admin/sso/providers"): ADMIN,
    ("POST", "/api/admin/sso/providers"): ADMIN,
    ("GET", "/api/admin/sso/providers/{provider_id}"): ADMIN,
    ("PATCH", "/api/admin/sso/providers/{provider_id}"): ADMIN,
    ("DELETE", "/api/admin/sso/providers/{provider_id}"): ADMIN,
    ("POST", "/api/admin/sso/providers/{provider_id}/test"): ADMIN,
    ("GET", "/api/admin/sso/providers/{provider_id}/mappings"): ADMIN,
    ("PUT", "/api/admin/sso/providers/{provider_id}/mappings"): ADMIN,
    ("GET", "/api/admin/sso/providers/{provider_id}/logins"): ADMIN,
    ("GET", "/api/admin/sso/users/{user_id}"): ADMIN,
}
ROUTE_POLICY.update(SSO_ROUTES)
HUB_GLOBAL_PREFIXES = (*HUB_GLOBAL_PREFIXES, "/api/admin/sso")
WORKSPACE_EXEMPT = WORKSPACE_EXEMPT | frozenset(SSO_ROUTES)
ADMIN_SPLIT.update({key: ADMIN_DATA for key, role in SSO_ROUTES.items() if role == ADMIN})

# ---- SSO-23: SAML 2.0 channel, SCIM 2.0 provisioning, hide local login (sso §2.2,
# §2.4, §2.6, D-2) ----
# The SAML leg is PUBLIC like the OIDC one: the browser arrives at start / ACS / SLS
# without a session by design, and the SP metadata is what the IdP imports.
# SCIM is PUBLIC to the console (no session, no workspace) because it carries its own
# credential — the provider's bearer token, checked by `sso_scim.authenticate` on
# every route (the `/v1` API-key pattern). Both sit in `auth._OPEN_API_PATTERN`.
# Loading metadata, rotating the SP key or the SCIM token and hiding local login are
# the administrators', data side like the rest of SSO configuration.
SSO_SAML_SCIM_ROUTES: dict[tuple[str, str], str] = {
    ("GET", "/api/auth/saml/{provider_id}/start"): PUBLIC,
    ("POST", "/api/auth/saml/{provider_id}/acs"): PUBLIC,
    ("GET", "/api/auth/saml/{provider_id}/metadata"): PUBLIC,
    ("GET", "/api/auth/saml/{provider_id}/sls"): PUBLIC,
    ("POST", "/api/admin/sso/providers/{provider_id}/saml/metadata"): ADMIN,
    ("POST", "/api/admin/sso/providers/{provider_id}/saml/sp-key"): ADMIN,
    ("POST", "/api/admin/sso/providers/{provider_id}/scim/token"): ADMIN,
    ("DELETE", "/api/admin/sso/providers/{provider_id}/scim/token"): ADMIN,
    ("GET", "/api/admin/sso/settings"): ADMIN,
    ("PUT", "/api/admin/sso/settings"): ADMIN,
    ("GET", "/api/scim/v2/ServiceProviderConfig"): PUBLIC,
    ("GET", "/api/scim/v2/Users"): PUBLIC,
    ("POST", "/api/scim/v2/Users"): PUBLIC,
    ("GET", "/api/scim/v2/Users/{user_id}"): PUBLIC,
    ("PUT", "/api/scim/v2/Users/{user_id}"): PUBLIC,
    ("PATCH", "/api/scim/v2/Users/{user_id}"): PUBLIC,
    ("DELETE", "/api/scim/v2/Users/{user_id}"): PUBLIC,
    ("GET", "/api/scim/v2/Groups"): PUBLIC,
    ("POST", "/api/scim/v2/Groups"): PUBLIC,
    ("GET", "/api/scim/v2/Groups/{group_id}"): PUBLIC,
    ("PUT", "/api/scim/v2/Groups/{group_id}"): PUBLIC,
    ("PATCH", "/api/scim/v2/Groups/{group_id}"): PUBLIC,
    ("DELETE", "/api/scim/v2/Groups/{group_id}"): PUBLIC,
}
ROUTE_POLICY.update(SSO_SAML_SCIM_ROUTES)
HUB_GLOBAL_PREFIXES = (*HUB_GLOBAL_PREFIXES, "/api/scim")
WORKSPACE_EXEMPT = WORKSPACE_EXEMPT | frozenset(SSO_SAML_SCIM_ROUTES)
ADMIN_SPLIT.update({key: ADMIN_DATA for key, role in SSO_SAML_SCIM_ROUTES.items()
                    if role == ADMIN})

# ---- SHARE-2: notices of ended cross-project shares (projects design §11.2) ----
# Hub-global under `/api/me` like the caller's next steps: each handler answers only
# the caller's own notices (anyone else's notice id answers 404), so any signed-in
# member may read and acknowledge.
SHARE_NOTICE_ROUTES: dict[tuple[str, str], str] = {
    ("GET", "/api/me/share-notices"): MEMBER,
    ("POST", "/api/me/share-notices/{notice_id}/ack"): MEMBER,
}
ROUTE_POLICY.update(SHARE_NOTICE_ROUTES)
WORKSPACE_EXEMPT = WORKSPACE_EXEMPT | frozenset(SHARE_NOTICE_ROUTES)

# ---- CT-2: cost allocation tags and the billing-basis cost view (cost-tags §1.4, §1.6) ----
# The tag set and its activation state are the workspace account's billing
# configuration: workspace-scoped, no project (PROJECT_EXEMPT), administrators' —
# reading it is structure (it names no project's spend), activating it changes the
# account's Billing settings: data, a steward does not. The billing view is cost
# detail like `/api/costs`: administrators, data. A project's custom cost tags are
# its owner's (`pperm:project.members`, like its other owner settings; the department
# administrator through `dadmin`); its budget's basis qualifies the monthly budget and
# is set by the same people as `PUT .../quotas` — platform administrators only, no
# `dadmin` overlay (structure). Hub-global under
# `/api/projects`: a non-member 404s.
COST_TAG_ROUTES: dict[tuple[str, str], str] = {
    ("GET", "/api/cost-tags"): ADMIN,
    ("POST", "/api/cost-tags/activate"): ADMIN,
    ("GET", "/api/costs/billing"): ADMIN,
    ("GET", "/api/projects/{project_id}/cost-tags"): MEMBER,
    ("PUT", "/api/projects/{project_id}/cost-tags"): PPERM_PROJECT_MEMBERS,
    ("PUT", "/api/projects/{project_id}/budget-basis"): ADMIN,
}
ROUTE_POLICY.update(COST_TAG_ROUTES)
PROJECT_EXEMPT = PROJECT_EXEMPT | frozenset({
    ("GET", "/api/cost-tags"),
    ("POST", "/api/cost-tags/activate"),
    ("GET", "/api/costs/billing"),
})
WORKSPACE_EXEMPT = WORKSPACE_EXEMPT | {
    ("GET", "/api/projects/{project_id}/cost-tags"),
    ("PUT", "/api/projects/{project_id}/cost-tags"),
    ("PUT", "/api/projects/{project_id}/budget-basis"),
}
DEPARTMENT_ADMIN_ROUTES.update({
    ("PUT", "/api/projects/{project_id}/cost-tags"): "project",
})
ADMIN_SPLIT.update({
    ("GET", "/api/cost-tags"): ADMIN,
    ("POST", "/api/cost-tags/activate"): ADMIN_DATA,
    ("GET", "/api/costs/billing"): ADMIN_DATA,
    ("PUT", "/api/projects/{project_id}/budget-basis"): ADMIN,
})

# ---- EC-1/EC-2: evaluation cost — estimate every entry, record what a run cost
# (eval-cost §1). Reads of the project's own runs and online configs: the run /
# config of another project answers 404 in the handler, like the run detail.
EVAL_COST_ROUTES: dict[tuple[str, str], str] = {
    ("GET", "/api/eval/run-costs"): PERM_PROJECT_USE,
    ("GET", "/api/eval/runs/{run_id}/cost"): PERM_PROJECT_USE,
    ("GET", "/api/eval/online/{config_id}/cost"): PERM_PROJECT_USE,
}
ROUTE_POLICY.update(EVAL_COST_ROUTES)

# ---- SB-3: custom Python tools and the sandboxed built-in tools (strands-builder §2) ----
# The code check and the approvals list are participation-level reads inside the
# request's project (another project's approval is not found). Approving — or revoking
# — a version lets code outside the allow-list run with an agent's role: an
# administrator's call, data (a steward does not). The sandbox setting is workspace
# configuration (project-exempt): every member reads it (the wizard greys out the
# closed tools), only an administrator opens it.
CUSTOM_TOOL_ROUTES: dict[tuple[str, str], str] = {
    ("POST", "/api/custom-tools/check"): PERM_PROJECT_USE,
    ("GET", "/api/custom-tools/approvals"): PERM_PROJECT_USE,
    ("POST", "/api/custom-tools/approvals"): ADMIN,
    ("DELETE", "/api/custom-tools/approvals/{code_hash}"): ADMIN,
    ("GET", "/api/custom-tools/sandbox"): MEMBER,
    ("PUT", "/api/custom-tools/sandbox"): ADMIN,
}
ROUTE_POLICY.update(CUSTOM_TOOL_ROUTES)
PROJECT_EXEMPT = PROJECT_EXEMPT | frozenset({
    ("GET", "/api/custom-tools/sandbox"),
    ("PUT", "/api/custom-tools/sandbox"),
})
ADMIN_SPLIT.update({key: ADMIN_DATA for key, role in CUSTOM_TOOL_ROUTES.items()
                    if role == ADMIN})

# ---- W12-BE: the cost-tag back-fill (cost-tags §1.3, CT-1's job) ----
# Workspace configuration like the activation beside it (project-exempt): one run
# tags every project's resources in the workspace and its view lists them, so it is
# the administrators' and data (a steward does not).
COST_TAG_BACKFILL_ROUTES: dict[tuple[str, str], str] = {
    ("POST", "/api/cost-tags/backfill"): ADMIN,
    ("GET", "/api/cost-tags/backfill"): ADMIN,
}
ROUTE_POLICY.update(COST_TAG_BACKFILL_ROUTES)
PROJECT_EXEMPT = PROJECT_EXEMPT | frozenset(COST_TAG_BACKFILL_ROUTES)
ADMIN_SPLIT.update(dict.fromkeys(COST_TAG_BACKFILL_ROUTES, ADMIN_DATA))

# ---- the rail modules an administrator hid (services/nav_settings) ----
# Hub-global console configuration, a view filter only (no route closes): every
# signed-in account reads the list (its rail drops those entries), only an
# administrator writes it, and a steward does not (it would shape every member's
# business console, which is data-side).
NAV_SETTINGS_ROUTES: dict[tuple[str, str], str] = {
    ("GET", "/api/nav/hidden-modules"): MEMBER,
    ("PUT", "/api/nav/hidden-modules"): ADMIN,
}
ROUTE_POLICY.update(NAV_SETTINGS_ROUTES)
HUB_GLOBAL_PREFIXES = (*HUB_GLOBAL_PREFIXES, "/api/nav")
WORKSPACE_EXEMPT = WORKSPACE_EXEMPT | frozenset(NAV_SETTINGS_ROUTES)
ADMIN_SPLIT.update({("PUT", "/api/nav/hidden-modules"): ADMIN_DATA})
