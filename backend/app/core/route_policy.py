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
Consequences worth knowing before editing this table:

* Outside `perm:agents.*`, `member` remains close to read-only. There is still no
  per-user data partitioning — a member who can deploy can also see and mutate
  every other member's agents; revoking the permissions restores the read-only
  posture per user.
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
    ("GET", "/api/apikeys"): ADMIN,
    ("POST", "/api/apikeys"): ADMIN,
    ("POST", "/api/apikeys/{key_id}/disable"): ADMIN,
    ("POST", "/api/apikeys/{key_id}/enable"): ADMIN,
    # ---- chat: the member-facing invoke surface ----
    ("POST", "/api/chat/{agent_id}"): MEMBER,
    ("GET", "/api/chat/{agent_id}/history"): MEMBER,
    ("GET", "/api/chat/{agent_id}/memory"): MEMBER,
    ("GET", "/api/chat/{agent_id}/sessions"): MEMBER,
    # ---- studio local-debug scaffolding: shares /api/execute's admin posture ----
    ("GET", "/api/conversations"): MEMBER,
    ("POST", "/api/conversations"): ADMIN,
    ("GET", "/api/conversations/{session_id}"): MEMBER,
    ("DELETE", "/api/conversations/{session_id}"): ADMIN,
    ("PUT", "/api/conversations/{session_id}/code"): ADMIN,
    ("GET", "/api/conversations/{session_id}/messages"): MEMBER,
    ("POST", "/api/conversations/{session_id}/messages"): ADMIN,
    ("POST", "/api/conversations/{session_id}/messages/stream"): ADMIN,
    # ---- local code execution (also refused outright in prod; see local_exec) ----
    ("POST", "/api/execute"): ADMIN,
    ("POST", "/api/execute/stream"): ADMIN,
    ("POST", "/api/fix-code/stream"): ADMIN,
    ("GET", "/api/generate-code/status"): MEMBER,
    # ---- registry skill sources become deployable code; the two staging
    # helpers ride the deploy permission because the create wizard needs them,
    # while record-creating imports stay admin ----
    ("POST", "/api/agent-skills/import"): PERM_AGENT_DEPLOY,
    ("GET", "/api/registry/records"): MEMBER,
    ("POST", "/api/registry/records"): ADMIN,
    ("GET", "/api/registry/records/search"): MEMBER,
    ("GET", "/api/registry/records/{record_id}"): MEMBER,
    ("PUT", "/api/registry/records/{record_id}"): ADMIN,
    ("DELETE", "/api/registry/records/{record_id}"): ADMIN,
    ("POST", "/api/registry/records/{record_id}/action"): ADMIN,
    ("POST", "/api/registry/records/{record_id}/reimport"): ADMIN,
    ("GET", "/api/registry/skills/capabilities"): MEMBER,
    # installs software on the server host
    ("POST", "/api/registry/skills/capabilities/git-install"): ADMIN,
    ("POST", "/api/registry/skills/import"): ADMIN,
    # fetches remote content (SSRF-guarded); staging-only, needed by deploy
    ("POST", "/api/registry/skills/inspect"): PERM_AGENT_DEPLOY,
    ("POST", "/api/registry/sync-defaults"): ADMIN,
    ("GET", "/api/registry/attachables"): MEMBER,
    ("POST", "/api/registry/a2a-demo"): MEMBER,  # an invoke; parity with Chat
    # ---- tools + demos: /tools/call can mutate external systems through a
    # gateway target, and the demos open billable cloud sessions ----
    ("GET", "/api/tools"): MEMBER,
    ("POST", "/api/tools/call"): ADMIN,
    ("POST", "/api/demos/code-interpreter"): ADMIN,
    ("GET", "/api/demos/browser/options"): MEMBER,
    ("POST", "/api/demos/browser"): ADMIN,  # takes a caller-supplied URL
    ("DELETE", "/api/demos/browser/{session_id}"): ADMIN,
    # ---- knowledge bases: reads and the retrieval playground stay open ----
    ("GET", "/api/knowledge-bases"): MEMBER,
    ("POST", "/api/knowledge-bases"): ADMIN,
    ("POST", "/api/knowledge-bases/ensure-gateway"): ADMIN,
    ("GET", "/api/knowledge-bases/{kb_id}"): MEMBER,
    ("PATCH", "/api/knowledge-bases/{kb_id}"): ADMIN,
    ("DELETE", "/api/knowledge-bases/{kb_id}"): ADMIN,
    ("POST", "/api/knowledge-bases/{kb_id}/files"): ADMIN,
    ("POST", "/api/knowledge-bases/{kb_id}/data-sources"): ADMIN,
    ("DELETE", "/api/knowledge-bases/{kb_id}/data-sources/{ds_id}"): ADMIN,
    ("GET", "/api/knowledge-bases/{kb_id}/data-sources/{ds_id}/documents"): MEMBER,
    ("GET", "/api/knowledge-bases/{kb_id}/data-sources/{ds_id}/ingestion-jobs"): MEMBER,
    ("POST", "/api/knowledge-bases/{kb_id}/data-sources/{ds_id}/sync"): ADMIN,
    ("POST", "/api/knowledge-bases/{kb_id}/query"): MEMBER,  # retrieval playground
    # ---- governance: every posture change is admin ----
    ("GET", "/api/governance/gateways"): MEMBER,
    ("GET", "/api/governance/gateways/{gateway_id}"): MEMBER,
    ("POST", "/api/governance/gateways/{gateway_id}/manage"): ADMIN,
    ("DELETE", "/api/governance/gateways/{gateway_id}/manage"): ADMIN,
    ("GET", "/api/governance/gateways/{gateway_id}/registry-preview"): MEMBER,
    ("POST", "/api/governance/gateways/{gateway_id}/registry-import"): ADMIN,
    ("POST", "/api/governance/gateways/{gateway_id}/retire-legacy-records"): ADMIN,
    ("GET", "/api/governance/gateways/{gateway_id}/policies"): MEMBER,
    ("POST", "/api/governance/gateways/{gateway_id}/policies"): ADMIN,
    ("PUT", "/api/governance/gateways/{gateway_id}/policies/{policy_id}"): ADMIN,
    ("POST", "/api/governance/gateways/{gateway_id}/policies/{policy_id}/promote"): ADMIN,
    ("POST", "/api/governance/gateways/{gateway_id}/policies/{policy_id}/rollback"): ADMIN,
    ("POST", "/api/governance/gateways/{gateway_id}/engine"): ADMIN,
    ("POST", "/api/governance/gateways/{gateway_id}/mode"): ADMIN,
    ("POST", "/api/governance/gateways/{gateway_id}/generations"): ADMIN,
    ("GET", "/api/governance/gateways/{gateway_id}/generations/{generation_id}"): MEMBER,
    ("GET", "/api/governance/gateways/{gateway_id}/audit"): MEMBER,
    ("GET", "/api/governance/gateways/{gateway_id}/decisions"): MEMBER,
    ("GET", "/api/governance/operations/{operation_id}"): MEMBER,
    ("GET", "/api/governance/policies"): MEMBER,
    ("GET", "/api/governance/decisions"): MEMBER,
    # Not a dry run: it performs a real tools/call as the chosen principal and
    # journals a PolicyDecision row (routers/governance.py:406).
    ("POST", "/api/governance/policy-test"): ADMIN,
    ("POST", "/api/governance/policy-generation"): ADMIN,
    ("GET", "/api/governance/policy-generation/{generation_id}"): MEMBER,
    ("GET", "/api/traces/{session_id}"): MEMBER,
    # ---- evaluation: runs invoke agents and create AWS eval jobs ----
    ("GET", "/api/eval/datasets"): MEMBER,
    ("POST", "/api/eval/datasets"): ADMIN,
    ("POST", "/api/eval/datasets/upload"): ADMIN,
    ("GET", "/api/eval/datasets/cloud"): MEMBER,
    ("GET", "/api/eval/datasets/cloud/{cloud_id}"): MEMBER,
    ("DELETE", "/api/eval/datasets/cloud/{cloud_id}"): ADMIN,
    ("PUT", "/api/eval/datasets/{dataset_id}"): ADMIN,
    ("DELETE", "/api/eval/datasets/{dataset_id}"): ADMIN,
    ("POST", "/api/eval/datasets/{dataset_id}/sync-to-aws"): ADMIN,
    ("GET", "/api/eval/evaluators"): MEMBER,
    ("POST", "/api/eval/evaluators"): ADMIN,
    ("GET", "/api/eval/evaluators/{evaluator_id}"): MEMBER,
    ("PUT", "/api/eval/evaluators/{evaluator_id}"): ADMIN,
    ("DELETE", "/api/eval/evaluators/{evaluator_id}"): ADMIN,
    ("GET", "/api/eval/queue"): MEMBER,
    ("GET", "/api/eval/runs"): MEMBER,
    ("POST", "/api/eval/runs"): PERM_EVAL_RUN,
    ("GET", "/api/eval/runs/{run_id}"): MEMBER,
    ("GET", "/api/experiments"): MEMBER,
    ("GET", "/api/experiments/readiness"): MEMBER,
    ("POST", "/api/experiments"): ADMIN,
    ("GET", "/api/experiments/{exp_id}"): MEMBER,
    ("POST", "/api/experiments/{exp_id}/action"): ADMIN,
    # canaries provision real AgentCore runtimes
    ("GET", "/api/runtime-canaries"): MEMBER,
    ("POST", "/api/runtime-canaries"): ADMIN,
    ("GET", "/api/runtime-canaries/{canary_id}"): MEMBER,
    ("POST", "/api/runtime-canaries/{canary_id}/action"): ADMIN,
    # ---- read-only consoles ----
    ("GET", "/api/overview"): MEMBER,
    ("GET", "/api/memory/overview"): MEMBER,
    ("GET", "/api/memory/actors"): MEMBER,
    ("GET", "/api/memory/events"): MEMBER,
    ("GET", "/api/memory/extraction-jobs"): MEMBER,
    ("GET", "/api/memory/namespaces"): MEMBER,
    ("GET", "/api/memory/records"): MEMBER,
    ("POST", "/api/memory/records/search"): MEMBER,  # a search, not a mutation
    ("GET", "/api/memory/sessions"): MEMBER,
    ("GET", "/api/observability/dashboard"): MEMBER,
    ("GET", "/api/observability/sessions"): MEMBER,
    ("GET", "/api/observability/sessions/{session_id}"): MEMBER,
    ("GET", "/api/observability/traces"): MEMBER,
    ("GET", "/api/observability/traces/{trace_id}"): MEMBER,
    ("GET", "/api/observability/prices"): MEMBER,
    ("POST", "/api/observability/prices/refresh"): ADMIN,  # rewrites shared config
    # ---- console account management ----
    ("GET", "/api/users"): ADMIN,
    ("GET", "/api/users/stats"): ADMIN,
    ("PATCH", "/api/users/{user_id}"): ADMIN,
    ("DELETE", "/api/users/{user_id}"): ADMIN,
}

# Routers whose classification was extrapolated from the signed-off principle
# rather than reviewed route by route (evaluation, experiments, canaries,
# conversations, observability writes). Reads are MEMBER, state changes ADMIN.
# Carried as a known gap in the release notes; revisit alongside per-user
# data partitioning.
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
