"""Billing classification: one declarative table for every non-GET route.

Every route that **starts billable work** — model tokens, or an AgentCore fee
charged per use (Evaluations, Gateway, Code Interpreter, Browser, Policy
authoring, KB retrieval) — is ``Billable(source)``; every other non-GET route is
``FREE``. A billable console route passes the budget gate
(``services/budgets.gate_request``) inside ``enforce_route_policy``, after the role
and workspace checks and before the handler runs: a payer at or over their
monthly limit is refused with ``402 budget.exceeded`` (and a budgeted payer about
to use an unpriced model with ``402 budget.model_unpriced``). ``/v1`` invoke and
stream are billable too; ``routers/public_api.py`` gates them with the key's
creator as the payer.

Like ``ROUTE_POLICY`` the table is complete by construction:
``tests/test_billing_policy.py`` enumerates the live routes and fails on a
non-GET route with no row and on a row with no route. ``GET`` routes are reads
and never billable; a new billable ``GET`` would be a design smell.

What is FREE, and why: CRUD and control-plane writes (create / update / delete a
resource, deploy, publish, grants), and the console's own reads that AWS bills as
workspace overhead (Memory record search, Logs Insights scans of alert checks and
pipelines). Deploy and build costs are not usage. Long-running work a billable
route starts (eval runs, Skill Lab jobs, experiment traffic) is metered and
stopped at its next boundary by the jobs child; this table only gates the entry.

``Billable(..., actions=…)`` marks an action dispatcher whose billability depends
on the body (an experiment or canary ``action``): the dependency cannot read the
body, so the handler gates exactly those actions through ``gate_action`` — a
``cleanup`` that stops spend must never be refused.
"""

from dataclasses import dataclass
from typing import Any

# usage_events.source values (services/budgets re-exports them)
SOURCE_CHAT = "chat"
SOURCE_V1 = "v1"
SOURCE_INVOKE = "invoke"
SOURCE_ARCHITECT = "architect"
SOURCE_EVAL = "eval"
SOURCE_EXPERIMENT = "experiment"
SOURCE_CANARY = "canary"
SOURCE_SKILL_LAB = "skill_lab"
SOURCE_OBSERVABILITY = "observability"
SOURCE_CODEGEN = "codegen"
SOURCE_KNOWLEDGE = "knowledge"
SOURCE_TOOLS = "tools"
SOURCE_STUDIO = "studio"
SOURCE_GOVERNANCE = "governance"
SOURCE_MODELS_PROBE = "models_probe"
SOURCE_CODING = "coding"  # coding-agent turns (app/coding)
SOURCES = (
    SOURCE_CHAT, SOURCE_V1, SOURCE_INVOKE, SOURCE_ARCHITECT, SOURCE_EVAL,
    SOURCE_EXPERIMENT, SOURCE_CANARY, SOURCE_SKILL_LAB, SOURCE_OBSERVABILITY,
    SOURCE_CODEGEN, SOURCE_KNOWLEDGE, SOURCE_TOOLS, SOURCE_STUDIO, SOURCE_GOVERNANCE,
    SOURCE_MODELS_PROBE, SOURCE_CODING,
)


@dataclass(frozen=True)
class Billable:
    source: str
    # set → only these body ``action`` values are billable, gated by the handler
    actions: frozenset[str] | None = None


@dataclass(frozen=True)
class Free:
    pass


FREE = Free()

# Experiment actions that invoke the agent, run a recommender or stand up judges;
# accept / bundles / promote / verdict / cleanup only edit or tear down state.
EXPERIMENT_BILLABLE_ACTIONS = frozenset({"recommend", "traffic", "gateway", "abtest"})
# Canary setup creates the online judges, traffic invokes both arms, and advance
# moves the ramp to its next stage — the canary's online judges keep scoring live
# traffic for another stage (the jobs child's "check before each stage advance").
# verdict / complete / rollback read or END the rollout, so a payer over budget
# can always wind a canary down.
CANARY_BILLABLE_ACTIONS = frozenset({"setup", "traffic", "advance"})

_ARCH = "/api/assistant/architect/conversations/{conversation_id}"

# (HTTP method, route.path_format) -> Billable(source) | FREE
BILLING: dict[tuple[str, str], Billable | Free] = {
    # ---- the login surface ----
    ("POST", "/api/auth/login"): FREE,
    ("POST", "/api/auth/register"): FREE,
    ("POST", "/api/auth/logout"): FREE,
    # ---- agents: lifecycle is control plane; invoke is a turn (parity with Chat) ----
    ("POST", "/api/agents"): FREE,
    ("POST", "/api/agents/discovery/import"): FREE,
    ("POST", "/api/agents/uploads"): FREE,
    ("DELETE", "/api/agents/{agent_id}"): FREE,
    ("POST", "/api/agents/{agent_id}/convert"): FREE,
    ("POST", "/api/agents/{agent_id}/redeploy"): FREE,
    ("POST", "/api/agents/{agent_id}/inbound-auth"): FREE,
    ("POST", "/api/agents/{agent_id}/invoke"): Billable(SOURCE_INVOKE),
    ("POST", "/api/agents/{agent_id}/snapshots/{seq}/rollback"): FREE,
    # ---- system presets: install / uninstall create or delete resources ----
    ("POST", "/api/system-agents/{preset_key}/install"): FREE,
    ("POST", "/api/system-agents/{preset_key}/skill-registration"): FREE,
    ("DELETE", "/api/system-agents/{preset_key}"): FREE,
    # ---- architect assistant: a turn invokes the preset Harness; approving deploys and
    # materializing creates evaluation assets (research §4 entry points) ----
    ("POST", "/api/assistant/architect/conversations"): FREE,
    ("PUT", f"{_ARCH}/sharing"): FREE,
    ("POST", f"{_ARCH}/catalog"): FREE,
    ("PUT", f"{_ARCH}/preparation"): FREE,
    ("POST", f"{_ARCH}/preparation/skills"): FREE,
    ("POST", f"{_ARCH}/turns"): Billable(SOURCE_ARCHITECT),
    ("PUT", f"{_ARCH}/proposal"): FREE,
    ("POST", f"{_ARCH}/proposal/reject"): FREE,
    ("POST", f"{_ARCH}/proposal/approve"): Billable(SOURCE_ARCHITECT),
    ("POST", f"{_ARCH}/evaluation-plan/prepare"): FREE,
    ("PUT", f"{_ARCH}/evaluation-plan"): FREE,
    ("POST", f"{_ARCH}/evaluation-plan/materialize"): Billable(SOURCE_ARCHITECT),
    ("POST", f"{_ARCH}/evaluation-plan/operations/{{operation_id}}/retry"): FREE,
    ("POST", f"{_ARCH}/evaluation-plan/operations/{{operation_id}}/lambda-revision-review"): FREE,
    ("DELETE", f"{_ARCH}/evaluation-plan/operations/{{operation_id}}/assets"): FREE,
    ("DELETE", _ARCH): FREE,
    # ---- API keys ----
    ("POST", "/api/apikeys"): FREE,
    ("POST", "/api/apikeys/{key_id}/disable"): FREE,
    ("POST", "/api/apikeys/{key_id}/enable"): FREE,
    # ---- Chat / TryChat: one turn per POST ----
    ("POST", "/api/chat/{agent_id}"): Billable(SOURCE_CHAT),
    ("POST", "/api/chat/{agent_id}/sessions/{session_id}/stop"): FREE,
    # ---- thumbs feedback ----
    ("POST", "/api/feedback"): FREE,
    # ---- Studio local debug: runs agent code that calls Bedrock (refused in prod) ----
    ("POST", "/api/conversations"): FREE,
    ("DELETE", "/api/conversations/{session_id}"): FREE,
    ("PUT", "/api/conversations/{session_id}/code"): FREE,
    ("POST", "/api/conversations/{session_id}/messages"): Billable(SOURCE_STUDIO),
    ("POST", "/api/conversations/{session_id}/messages/stream"): Billable(SOURCE_STUDIO),
    ("POST", "/api/execute"): Billable(SOURCE_STUDIO),
    ("POST", "/api/execute/stream"): Billable(SOURCE_STUDIO),
    # ---- Studio AI fix: a Claude Agent SDK session on the host ----
    ("POST", "/api/fix-code/stream"): Billable(SOURCE_CODEGEN),
    # ---- skills + registry: control plane; the A2A demo is an invoke ----
    ("POST", "/api/agent-skills/import"): FREE,
    ("POST", "/api/registry/records"): FREE,
    ("PUT", "/api/registry/records/{record_id}"): FREE,
    ("DELETE", "/api/registry/records/{record_id}"): FREE,
    ("POST", "/api/registry/records/{record_id}/action"): FREE,
    ("POST", "/api/registry/records/{record_id}/reimport"): FREE,
    ("POST", "/api/registry/skills/capabilities/git-install"): FREE,
    ("POST", "/api/registry/skills/import"): FREE,
    ("POST", "/api/registry/skills/inspect"): FREE,
    ("POST", "/api/registry/sync-defaults"): FREE,
    ("POST", "/api/registry/a2a-demo"): Billable(SOURCE_INVOKE),
    # ---- identity: connections, grants, consent portal ----
    ("POST", "/api/identity/connections/oauth2"): FREE,
    ("POST", "/api/identity/connections/api-key"): FREE,
    ("DELETE", "/api/identity/connections/{kind}/{name}"): FREE,
    ("POST", "/api/identity/gateway-targets"): FREE,
    ("DELETE", "/api/identity/gateway-targets/{target_id}"): FREE,
    ("POST", "/api/identity/oauth/complete"): FREE,
    ("DELETE", "/api/identity/grants/{connection}"): FREE,
    ("POST", "/api/identity/consent-portal"): FREE,
    ("DELETE", "/api/identity/consent-portal"): FREE,
    ("PUT", "/api/identity/inbound-auth/default"): FREE,
    # ---- tools: a Gateway tools/call and AgentCore Code Interpreter / Browser sessions ----
    ("POST", "/api/tools/call"): Billable(SOURCE_TOOLS),
    ("POST", "/api/demos/code-interpreter"): Billable(SOURCE_TOOLS),
    ("POST", "/api/demos/browser"): Billable(SOURCE_TOOLS),
    ("DELETE", "/api/demos/browser/{session_id}"): FREE,
    # ---- knowledge bases: ingestion (embedding tokens) and retrieval are billed per use ----
    ("POST", "/api/knowledge-bases"): FREE,
    ("POST", "/api/knowledge-bases/ensure-gateway"): FREE,
    ("PATCH", "/api/knowledge-bases/{kb_id}"): FREE,
    ("DELETE", "/api/knowledge-bases/{kb_id}"): FREE,
    ("POST", "/api/knowledge-bases/{kb_id}/files"): FREE,
    ("POST", "/api/knowledge-bases/{kb_id}/data-sources"): FREE,
    ("DELETE", "/api/knowledge-bases/{kb_id}/data-sources/{ds_id}"): FREE,
    ("POST", "/api/knowledge-bases/{kb_id}/data-sources/{ds_id}/sync"): Billable(SOURCE_KNOWLEDGE),
    ("POST", "/api/knowledge-bases/{kb_id}/query"): Billable(SOURCE_KNOWLEDGE),
    # ---- COA context layer: connect / consent / revoke are control-plane writes; COA's
    # own spend is not metered by Launchpad ----
    ("PUT", "/api/context-layer"): FREE,
    ("DELETE", "/api/context-layer"): FREE,
    ("POST", "/api/context-layer/authorize"): FREE,
    ("POST", "/api/context-layer/revoke"): FREE,
    # the playground asks COA's Serve runtime: COA spend is in COA's account, and
    # Launchpad does not meter it
    ("POST", "/api/context-layer/playground"): FREE,
    # ---- governance: policy test calls a Gateway tool; NL policy authoring bills tokens ----
    ("POST", "/api/governance/gateways/{gateway_id}/manage"): FREE,
    ("DELETE", "/api/governance/gateways/{gateway_id}/manage"): FREE,
    ("POST", "/api/governance/gateways/{gateway_id}/registry-import"): FREE,
    ("POST", "/api/governance/gateways/{gateway_id}/retire-legacy-records"): FREE,
    ("POST", "/api/governance/gateways/{gateway_id}/policies"): FREE,
    ("PUT", "/api/governance/gateways/{gateway_id}/policies/{policy_id}"): FREE,
    ("DELETE", "/api/governance/gateways/{gateway_id}/policies/{policy_id}"): FREE,
    ("POST", "/api/governance/gateways/{gateway_id}/policies/{policy_id}/promote"): FREE,
    ("POST", "/api/governance/gateways/{gateway_id}/policies/{policy_id}/rollback"): FREE,
    ("POST", "/api/governance/gateways/{gateway_id}/engine"): FREE,
    ("POST", "/api/governance/gateways/{gateway_id}/rate-limits"): FREE,
    ("PUT", "/api/governance/gateways/{gateway_id}/rate-limits/{rate_limit_id}"): FREE,
    ("DELETE", "/api/governance/gateways/{gateway_id}/rate-limits/{rate_limit_id}"): FREE,
    ("POST", "/api/governance/gateways/{gateway_id}/targets/{target_id}/synchronize"): FREE,
    ("POST", "/api/governance/gateways/{gateway_id}/mode"): FREE,
    ("POST", "/api/governance/gateways/{gateway_id}/generations"): Billable(SOURCE_GOVERNANCE),
    ("POST", "/api/governance/policy-test"): Billable(SOURCE_GOVERNANCE),
    ("POST", "/api/governance/policy-generation"): Billable(SOURCE_GOVERNANCE),
    # ---- evaluation: runs, recommendations and online judging bill evaluations; the
    # datasets / evaluators / pipelines CRUD and a run's stop / recheck do not ----
    ("POST", "/api/eval/datasets"): FREE,
    ("POST", "/api/eval/datasets/upload"): FREE,
    ("DELETE", "/api/eval/datasets/cloud/{cloud_id}"): FREE,
    ("POST", "/api/eval/datasets/cloud/{cloud_id}/publish-version"): FREE,
    ("DELETE", "/api/eval/datasets/cloud/{cloud_id}/versions/{version}"): FREE,
    ("PUT", "/api/eval/datasets/{dataset_id}"): FREE,
    ("DELETE", "/api/eval/datasets/{dataset_id}"): FREE,
    ("POST", "/api/eval/datasets/{dataset_id}/sync-to-aws"): FREE,
    ("POST", "/api/eval/datasets/{dataset_id}/publish-version"): FREE,
    ("POST", "/api/eval/datasets/from-sessions"): FREE,
    ("POST", "/api/eval/pipelines"): FREE,
    ("PUT", "/api/eval/pipelines/{pipeline_id}"): FREE,
    ("DELETE", "/api/eval/pipelines/{pipeline_id}"): FREE,
    ("POST", "/api/eval/pipelines/{pipeline_id}/run"): FREE,
    ("POST", "/api/eval/pipelines/preview-logs"): FREE,
    ("POST", "/api/eval/evaluators"): FREE,
    ("PUT", "/api/eval/evaluators/{evaluator_id}"): FREE,
    ("DELETE", "/api/eval/evaluators/{evaluator_id}"): FREE,
    ("POST", "/api/eval/runs"): Billable(SOURCE_EVAL),
    ("POST", "/api/eval/runs/{run_id}/stop"): FREE,
    ("POST", "/api/eval/runs/{run_id}/recheck"): FREE,
    ("POST", "/api/eval/runs/{run_id}/rerun"): Billable(SOURCE_EVAL),
    ("POST", "/api/eval/runs/{run_id}/recommendations"): Billable(SOURCE_EVAL),
    ("PUT", "/api/eval/runs/{run_id}/recommendations/{rec_id}/edit"): FREE,
    ("POST", "/api/eval/runs/{run_id}/recommendations/{rec_id}/accept"): FREE,
    ("DELETE", "/api/eval/runs/{run_id}"): FREE,
    ("POST", "/api/eval/online"): Billable(SOURCE_EVAL),
    ("PATCH", "/api/eval/online/{config_id}"): FREE,
    ("POST", "/api/eval/online/{config_id}/pause"): FREE,
    ("POST", "/api/eval/online/{config_id}/resume"): Billable(SOURCE_EVAL),
    ("DELETE", "/api/eval/online/{config_id}"): FREE,
    ("POST", "/api/eval/online/{config_id}/reports"): Billable(SOURCE_EVAL),
    # ---- coding agent: installing the runtime is control plane (build + deploy) ----
    ("POST", "/api/coding/runtime/install"): FREE,
    ("DELETE", "/api/coding/runtime"): FREE,
    # a turn calls the model (tokens settle when it ends); the rest is the member's
    # own project bookkeeping, file saves and the deploy handoff (control plane)
    ("POST", "/api/coding/projects"): FREE,
    ("PATCH", "/api/coding/projects/{project_id}"): FREE,
    ("DELETE", "/api/coding/projects/{project_id}"): FREE,
    ("POST", "/api/coding/projects/{project_id}/turns"): Billable(SOURCE_CODING),
    ("POST", "/api/coding/projects/{project_id}/interrupt"): FREE,
    ("PUT", "/api/coding/projects/{project_id}/file"): FREE,
    ("POST", "/api/coding/projects/{project_id}/deploy"): FREE,
    # ---- Skill Lab: a job (and its resume) runs rollouts and judges ----
    ("POST", "/api/skill-lab/task-assets"): FREE,
    ("POST", "/api/skill-lab/tasksets"): FREE,
    ("PUT", "/api/skill-lab/tasksets/{taskset_id}"): FREE,
    ("DELETE", "/api/skill-lab/tasksets/{taskset_id}"): FREE,
    ("POST", "/api/skill-lab/jobs"): Billable(SOURCE_SKILL_LAB),
    ("POST", "/api/skill-lab/jobs/{job_id}/cancel"): FREE,
    ("DELETE", "/api/skill-lab/jobs/{job_id}"): FREE,
    ("POST", "/api/skill-lab/jobs/{job_id}/resume"): Billable(SOURCE_SKILL_LAB),
    ("POST", "/api/skill-lab/jobs/{job_id}/publish"): FREE,
    ("POST", "/api/skill-lab/jobs/{job_id}/import-taskset"): FREE,
    ("POST", "/api/skill-lab/jobs/{job_id}/apply-expansion"): FREE,
    # ---- experiments and canaries: create + the billable actions ----
    ("POST", "/api/experiments"): Billable(SOURCE_EXPERIMENT),
    ("POST", "/api/experiments/{exp_id}/action"):
        Billable(SOURCE_EXPERIMENT, actions=EXPERIMENT_BILLABLE_ACTIONS),
    ("POST", "/api/runtime-canaries"): Billable(SOURCE_CANARY),
    ("POST", "/api/runtime-canaries/{canary_id}/action"):
        Billable(SOURCE_CANARY, actions=CANARY_BILLABLE_ACTIONS),
    # ---- announcements and videos (admin content) ----
    ("POST", "/api/announcements"): FREE,
    ("PUT", "/api/announcements/{announcement_id}"): FREE,
    ("POST", "/api/announcements/{announcement_id}/publish"): FREE,
    ("POST", "/api/announcements/{announcement_id}/unpublish"): FREE,
    ("DELETE", "/api/announcements/{announcement_id}"): FREE,
    ("POST", "/api/videos/manage"): FREE,
    ("PUT", "/api/videos/manage/{video_id}"): FREE,
    ("POST", "/api/videos/manage/{video_id}/publish"): FREE,
    ("POST", "/api/videos/manage/{video_id}/unpublish"): FREE,
    ("DELETE", "/api/videos/manage/{video_id}"): FREE,
    # ---- memory console: a record search is a console read (workspace overhead) ----
    ("POST", "/api/memory/records/search"): FREE,
    ("POST", "/api/memory/resources"): FREE,
    ("PUT", "/api/memory/resources/{memory_id}"): FREE,
    ("DELETE", "/api/memory/resources/{memory_id}"): FREE,
    ("POST", "/api/memory/resources/{memory_id}/adopt"): FREE,
    # ---- observability: SCORE NOW calls Evaluate; the price refresh is config ----
    ("POST", "/api/observability/sessions/{session_id}/evaluate"): Billable(SOURCE_OBSERVABILITY),
    ("POST", "/api/observability/prices/refresh"): FREE,
    ("POST", "/api/observability/session-previews"): FREE,  # ledger / Memory / log reads
    # ---- user and workspace management ----
    ("PATCH", "/api/users/{user_id}"): FREE,
    ("DELETE", "/api/users/{user_id}"): FREE,
    ("POST", "/api/workspaces"): FREE,
    ("POST", "/api/workspaces/preflight"): FREE,
    ("PATCH", "/api/workspaces/{workspace_id}"): FREE,
    ("DELETE", "/api/workspaces/{workspace_id}"): FREE,
    ("POST", "/api/workspaces/{workspace_id}/purge"): FREE,
    ("POST", "/api/workspaces/{workspace_id}/bootstrap"): FREE,
    ("PUT", "/api/workspaces/{workspace_id}/grants"): FREE,
    ("PUT", "/api/workspaces/{workspace_id}/settings/visibility"): FREE,
    # ---- platform model settings: the probe is one real Converse call ----
    ("PUT", "/api/admin/models/{role}"): FREE,
    ("DELETE", "/api/admin/models/{role}"): FREE,
    ("POST", "/api/admin/models/probe"): Billable(SOURCE_MODELS_PROBE),
    # ---- alert rules: an evaluate pass is a CloudWatch scan (workspace overhead) ----
    ("POST", "/api/alerts"): FREE,
    ("PATCH", "/api/alerts/{rule_id}"): FREE,
    ("DELETE", "/api/alerts/{rule_id}"): FREE,
    ("POST", "/api/alerts/evaluate"): FREE,
    # ---- spend budgets, model prices and AgentCore rates (routers/budgets.py) ----
    ("POST", "/api/budgets/levels"): FREE,
    ("PATCH", "/api/budgets/levels/{level_id}"): FREE,
    ("DELETE", "/api/budgets/levels/{level_id}"): FREE,
    ("PUT", "/api/budgets/users/{user_id}"): FREE,
    ("PUT", "/api/budgets/settings"): FREE,
    ("PUT", "/api/admin/model-prices/{model_id}"): FREE,
    ("DELETE", "/api/admin/model-prices/{model_id}"): FREE,
    ("PUT", "/api/admin/agentcore-rates"): FREE,
    # ---- public /v1 (API-key auth; gated in routers/public_api.py, payer = the
    # key's creator) ----
    ("POST", "/v1/agents/{agent_id}/invoke"): Billable(SOURCE_V1),
    ("POST", "/v1/agents/{agent_id}/invoke-stream"): Billable(SOURCE_V1),
}

V1_PREFIX = "/v1"


def classification(method: str, path_format: str) -> Billable | Free | None:
    """The route's billing class, or None when it is not in the table."""
    return BILLING.get((method, path_format))


def enforce(request: Any, path_format: str) -> None:
    """Run the budget gate for a billable console route (``enforce_route_policy``).

    A GET is never billable. An unclassified non-GET route is not refused here —
    ``ROUTE_POLICY`` is the default-deny boundary, and the drift test keeps this
    table complete — so a classification gap can never take the console down.
    """
    if request.method in {"GET", "HEAD", "OPTIONS"}:
        return
    row = classification(request.method, path_format)
    if not isinstance(row, Billable) or row.actions is not None:
        return
    from app.services import budgets

    budgets.gate_request(request, row.source)


def gate_action(request: Any, action: str) -> None:
    """The handler-side gate of an action dispatcher (``Billable(actions=…)``): only
    the billable actions pass the budget check, so a ``cleanup`` that stops spend is
    never refused."""
    route = request.scope.get("route")
    path_format = getattr(route, "path_format", None) or request.url.path
    row = classification(request.method, path_format)
    if isinstance(row, Billable) and row.actions is not None and action in row.actions:
        from app.services import budgets

        budgets.gate_request(request, row.source)
