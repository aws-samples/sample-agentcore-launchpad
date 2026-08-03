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
without one (health, docs, and the login surface itself).

The classification principle, signed off by river 2026-08-03: **admin for routes
that execute code, change deployed or cloud state, mint credentials, or change
governance posture; member for reads and for the member's own interaction with an
agent.** Two consequences worth knowing before editing this table:

* `member` is close to read-only. That is intended while T19 (no per-user data
  partitioning) is open — a member who could deploy could also see and mutate
  every other member's resources.
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
from app.routers.auth import require_admin

ADMIN = "admin"
MEMBER = "member"
PUBLIC = "public"

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
    # ---- agents: deploy-state changes are admin, reads and invoke are not ----
    ("GET", "/api/agents"): MEMBER,
    ("POST", "/api/agents"): ADMIN,
    ("GET", "/api/agents/discovery"): MEMBER,
    ("POST", "/api/agents/discovery/import"): ADMIN,
    ("GET", "/api/agents/{agent_id}"): MEMBER,
    ("DELETE", "/api/agents/{agent_id}"): ADMIN,
    ("POST", "/api/agents/{agent_id}/convert"): ADMIN,
    ("POST", "/api/agents/{agent_id}/redeploy"): ADMIN,
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
    # ---- registry skill sources become deployable code ----
    ("POST", "/api/agent-skills/import"): ADMIN,
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
    ("POST", "/api/registry/skills/inspect"): ADMIN,  # fetches remote content
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
    ("POST", "/api/eval/runs"): ADMIN,
    ("GET", "/api/eval/runs/{run_id}"): MEMBER,
    ("GET", "/api/experiments"): MEMBER,
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
# Carried as a known gap in the task's release notes; revisit alongside T19.
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
