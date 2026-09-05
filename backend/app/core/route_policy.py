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
  404. `ADMIN` marks user + workspace management; it no longer marks "state
  changes".
* The studio local-exec surface (`/api/execute*`, conversations writes) stays safe
  in production through its own handler guard (`local_exec`, refused outright in
  prod unless explicitly opted in) — that guard, not this table, is the real
  boundary there.
* Invoking an agent is deliberately `MEMBER` (`/api/agents/{id}/invoke`,
  `/api/registry/a2a-demo`): it is the same capability the Chat console gives
  every member, so gating it while Chat stays open would protect nothing.

There is deliberately **no** setting that disables this table. A flag that turns
authorization off is the vulnerability; fixing a misclassification means editing
the entry.
"""

from typing import Any

from fastapi import Request

from app.core.errors import AppError
from app.routers.auth import require_admin, require_permission
from app.routers.workspaces import resolve_workspace

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
    ("GET", "/api/agents/{agent_id}"): MEMBER,
    ("DELETE", "/api/agents/{agent_id}"): PERM_AGENT_DELETE,
    ("POST", "/api/agents/{agent_id}/convert"): PERM_AGENT_CONVERT,
    ("POST", "/api/agents/{agent_id}/redeploy"): PERM_AGENT_DEPLOY,
    ("POST", "/api/agents/{agent_id}/invoke"): MEMBER,  # parity with Chat
    ("GET", "/api/jobs/{job_id}"): MEMBER,
    # ---- credential minting ----
    ("GET", "/api/apikeys"): MEMBER,
    ("POST", "/api/apikeys"): MEMBER,
    ("POST", "/api/apikeys/{key_id}/disable"): MEMBER,
    ("POST", "/api/apikeys/{key_id}/enable"): MEMBER,
    # ---- chat: the member-facing invoke surface ----
    ("POST", "/api/chat/{agent_id}"): MEMBER,
    ("GET", "/api/chat/{agent_id}/history"): MEMBER,
    ("GET", "/api/chat/{agent_id}/memory"): MEMBER,
    ("GET", "/api/chat/{agent_id}/sessions"): MEMBER,
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
    ("GET", "/api/registry/records/{record_id}"): MEMBER,
    ("PUT", "/api/registry/records/{record_id}"): MEMBER,
    ("DELETE", "/api/registry/records/{record_id}"): MEMBER,
    ("POST", "/api/registry/records/{record_id}/action"): MEMBER,
    ("POST", "/api/registry/records/{record_id}/reimport"): MEMBER,
    ("GET", "/api/registry/skills/capabilities"): MEMBER,
    # installs software on the server host
    ("POST", "/api/registry/skills/capabilities/git-install"): MEMBER,
    ("POST", "/api/registry/skills/import"): MEMBER,
    # fetches remote content (SSRF-guarded); staging-only, needed by deploy
    ("POST", "/api/registry/skills/inspect"): PERM_AGENT_DEPLOY,
    ("POST", "/api/registry/sync-defaults"): MEMBER,
    ("GET", "/api/registry/attachables"): MEMBER,
    ("POST", "/api/registry/a2a-demo"): MEMBER,  # an invoke; parity with Chat
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
    ("POST", "/api/knowledge-bases/ensure-gateway"): MEMBER,
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
    # ---- governance ----
    ("GET", "/api/governance/gateways"): MEMBER,
    ("GET", "/api/governance/gateways/{gateway_id}"): MEMBER,
    ("POST", "/api/governance/gateways/{gateway_id}/manage"): MEMBER,
    ("DELETE", "/api/governance/gateways/{gateway_id}/manage"): MEMBER,
    ("GET", "/api/governance/gateways/{gateway_id}/registry-preview"): MEMBER,
    ("POST", "/api/governance/gateways/{gateway_id}/registry-import"): MEMBER,
    ("POST", "/api/governance/gateways/{gateway_id}/retire-legacy-records"): MEMBER,
    ("GET", "/api/governance/gateways/{gateway_id}/policies"): MEMBER,
    ("POST", "/api/governance/gateways/{gateway_id}/policies"): MEMBER,
    ("PUT", "/api/governance/gateways/{gateway_id}/policies/{policy_id}"): MEMBER,
    ("DELETE", "/api/governance/gateways/{gateway_id}/policies/{policy_id}"): MEMBER,
    ("POST", "/api/governance/gateways/{gateway_id}/policies/{policy_id}/promote"): MEMBER,
    ("POST", "/api/governance/gateways/{gateway_id}/policies/{policy_id}/rollback"): MEMBER,
    ("POST", "/api/governance/gateways/{gateway_id}/engine"): MEMBER,
    ("POST", "/api/governance/gateways/{gateway_id}/mode"): MEMBER,
    ("POST", "/api/governance/gateways/{gateway_id}/generations"): MEMBER,
    ("GET", "/api/governance/gateways/{gateway_id}/generations/{generation_id}"): MEMBER,
    ("GET", "/api/governance/gateways/{gateway_id}/audit"): MEMBER,
    ("GET", "/api/governance/gateways/{gateway_id}/decisions"): MEMBER,
    ("GET", "/api/governance/operations/{operation_id}"): MEMBER,
    ("GET", "/api/governance/policies"): MEMBER,
    ("GET", "/api/governance/decisions"): MEMBER,
    # Not a dry run: it performs a real tools/call as the chosen principal and
    # journals a PolicyDecision row (routers/governance.py:406).
    ("POST", "/api/governance/policy-test"): MEMBER,
    ("POST", "/api/governance/policy-generation"): MEMBER,
    ("GET", "/api/governance/policy-generation/{generation_id}"): MEMBER,
    ("GET", "/api/traces/{session_id}"): MEMBER,
    # ---- evaluation: runs invoke agents and create AWS eval jobs ----
    ("GET", "/api/eval/datasets"): MEMBER,
    ("POST", "/api/eval/datasets"): MEMBER,
    ("POST", "/api/eval/datasets/upload"): MEMBER,
    ("GET", "/api/eval/datasets/cloud"): MEMBER,
    ("GET", "/api/eval/datasets/cloud/{cloud_id}"): MEMBER,
    ("DELETE", "/api/eval/datasets/cloud/{cloud_id}"): MEMBER,
    ("PUT", "/api/eval/datasets/{dataset_id}"): MEMBER,
    ("DELETE", "/api/eval/datasets/{dataset_id}"): MEMBER,
    ("POST", "/api/eval/datasets/{dataset_id}/sync-to-aws"): MEMBER,
    ("GET", "/api/eval/evaluators"): MEMBER,
    ("POST", "/api/eval/evaluators"): MEMBER,
    ("GET", "/api/eval/evaluators/{evaluator_id}"): MEMBER,
    ("PUT", "/api/eval/evaluators/{evaluator_id}"): MEMBER,
    ("DELETE", "/api/eval/evaluators/{evaluator_id}"): MEMBER,
    ("GET", "/api/eval/queue"): MEMBER,
    ("GET", "/api/eval/runs"): MEMBER,
    ("POST", "/api/eval/runs"): PERM_EVAL_RUN,
    ("GET", "/api/eval/runs/{run_id}"): MEMBER,
    # online evaluation configs: create/resume start billed judge calls on live
    # traffic, the same cost class as starting a run; list/detail/results read AWS
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
    ("GET", "/api/experiments"): MEMBER,
    ("GET", "/api/experiments/readiness"): MEMBER,
    ("GET", "/api/experiments/providers"): MEMBER,
    ("POST", "/api/experiments"): MEMBER,
    ("GET", "/api/experiments/{exp_id}"): MEMBER,
    ("POST", "/api/experiments/{exp_id}/action"): MEMBER,
    # canaries provision real AgentCore runtimes
    ("GET", "/api/runtime-canaries"): MEMBER,
    ("POST", "/api/runtime-canaries"): MEMBER,
    ("GET", "/api/runtime-canaries/{canary_id}"): MEMBER,
    ("POST", "/api/runtime-canaries/{canary_id}/action"): MEMBER,
    # ---- read-only consoles ----
    ("GET", "/api/overview"): MEMBER,
    ("GET", "/api/overview/online-quality"): MEMBER,
    ("GET", "/api/memory/overview"): MEMBER,
    ("GET", "/api/memory/actors"): MEMBER,
    ("GET", "/api/memory/events"): MEMBER,
    ("GET", "/api/memory/extraction-jobs"): MEMBER,
    ("GET", "/api/memory/namespaces"): MEMBER,
    ("GET", "/api/memory/records"): MEMBER,
    ("POST", "/api/memory/records/search"): MEMBER,  # a search, not a mutation
    ("GET", "/api/memory/sessions"): MEMBER,
    ("GET", "/api/memory/resources"): MEMBER,
    ("POST", "/api/memory/resources"): MEMBER,  # creates an AgentCore Memory resource
    ("GET", "/api/memory/resources/{memory_id}"): MEMBER,
    ("PUT", "/api/memory/resources/{memory_id}"): MEMBER,  # description / event expiry
    ("DELETE", "/api/memory/resources/{memory_id}"): MEMBER,
    ("GET", "/api/observability/dashboard"): MEMBER,
    ("GET", "/api/observability/sessions"): MEMBER,
    ("GET", "/api/observability/sessions/{session_id}"): MEMBER,
    ("GET", "/api/observability/traces"): MEMBER,
    ("GET", "/api/observability/traces/{trace_id}"): MEMBER,
    ("GET", "/api/observability/prices"): MEMBER,
    ("POST", "/api/observability/prices/refresh"): MEMBER,  # rewrites shared config
    # ---- console account management: the one surface that stays admin ----
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
}

# Hub-global route prefixes: nothing under them operates inside a workspace.
HUB_GLOBAL_PREFIXES = ("/api/auth", "/api/users", "/api/workspaces")


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
    }
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
    if role == ADMIN:
        require_admin(request)
    elif role.startswith(_PERM_PREFIX):
        require_permission(request, role.removeprefix(_PERM_PREFIX))
    if is_workspace_scoped(request.method, path_format):
        # Resolved here rather than per handler: enforcement must not depend on a
        # router remembering to declare the dependency. Handlers read the result
        # back through `require_workspace`.
        resolve_workspace(request)
