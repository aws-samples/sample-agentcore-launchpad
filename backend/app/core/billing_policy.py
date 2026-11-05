"""Budget classification: which console routes spend a project's money.

Every mutating route under ``BILLABLE_PREFIXES`` has a row in ``BILLING``: either
``Billable(source)`` — it starts work AWS bills per use (model tokens, an AgentCore
Runtime / Evaluations / Gateway / Code Interpreter / Browser fee, a CodeBuild deploy,
KB embedding) — or ``FREE`` (CRUD, control-plane bookkeeping, a stop or teardown,
reads). ``tests/test_billing_policy.py`` enumerates the live routes and fails on an
unclassified mutating route under those prefixes, and on a row with no route — so a
new billable endpoint cannot ship ungated.

A billable route passes ``enforce_budget`` (an app-level dependency after the route
policy): the request's project at or over its ``monthly_budget_usd`` is refused with
409 ``project.budget_exhausted`` (``services/project_quotas.assert_budget``) before
the handler runs. A request with no project (hub-global, platform and system work) is
exempt, and so is every ``GET``: a read never spends the budget.

Two refinements the dependency cannot see, gated by the handler instead:

* ``Billable(..., actions=…)`` — an action dispatcher whose billability depends on
  the body's ``action`` (an experiment, a canary): the handler calls ``gate_action``
  so a ``cleanup`` / ``verdict`` that ends spend is never refused;
* ``Billable(..., in_handler=True)`` — the project the work is filed under is named
  in the body (agent creation and conversion, `services/agent_project`): the handler
  calls ``assert_budget`` with it.

Outside the console routes the invoke chain gates itself, so ``/v1``, ``/share``
pages and channel adapters are covered with console chat by the one entry point
(``services/invoke.invoke_agent_text`` and ``services/chat.chat_stream``). Long jobs
(deploys, evaluation runs, Skill Lab, experiment traffic) are checked when they
start, never mid-way: spend estimates lag, so a project may overshoot its budget by
what such work and the lag add up to.

Budget administration itself is FREE: the budget / quota / cost-view routes live under
``/api/projects``, ``/api/departments`` and ``/api/budgets``, outside
``BILLABLE_PREFIXES`` (the guard test refuses a row outside them), so the dependency
never gates them — a used-up project can always be given more budget. A turn's COA
tool calls (direct and gateway mode) run inside the agent's invoke, so they meet the
agent's project's gate there, before the gateway preflight or any COA call.
"""

from dataclasses import dataclass
from typing import Any

from fastapi import Request

SOURCE_INVOKE = "invoke"
SOURCE_CHAT = "chat"
SOURCE_DEPLOY = "deploy"
SOURCE_ARCHITECT = "architect"
SOURCE_EVAL = "eval"
SOURCE_EXPERIMENT = "experiment"
SOURCE_CANARY = "canary"
SOURCE_SKILL_LAB = "skill_lab"
SOURCE_KNOWLEDGE = "knowledge"
SOURCE_STUDIO = "studio"
SOURCE_CODEGEN = "codegen"
SOURCE_TOOLS = "tools"
SOURCE_GOVERNANCE = "governance"
SOURCE_MODELS_PROBE = "models_probe"
SOURCE_CODING = "coding"
SOURCE_CONTEXT_LAYER = "context_layer"


@dataclass(frozen=True)
class Billable:
    source: str
    # set → only these body `action` values are billable, gated by the handler
    actions: frozenset[str] | None = None
    # the handler gates the body-named project itself (the dependency skips it)
    in_handler: bool = False


@dataclass(frozen=True)
class Free:
    pass


FREE = Free()

# The prefixes whose every mutating route is classified (the guard test's scope).
BILLABLE_PREFIXES = (
    "/api/agents", "/api/assistant", "/api/chat", "/api/coding", "/api/context-layer",
    "/api/conversations", "/api/demos",
    "/api/eval", "/api/execute", "/api/experiments", "/api/fix-code", "/api/golden-sets",
    "/api/governance", "/api/knowledge-bases", "/api/mcp-servers", "/api/memory",
    "/api/model-probe", "/api/observability", "/api/promotions", "/api/registry",
    "/api/runtime-canaries", "/api/skill-lab", "/api/tools",
)

# experiment actions that invoke the agent, run a recommender or stand up the A/B
# gateway; accept / bundles / promote / verdict / cleanup edit or tear down state
EXPERIMENT_BILLABLE_ACTIONS = frozenset({"recommend", "traffic", "gateway", "abtest"})
# canary setup mints the candidate and its judges, traffic invokes both arms, advance
# keeps the judges scoring another stage; verdict / complete / rollback / cleanup end it
CANARY_BILLABLE_ACTIONS = frozenset({"setup", "traffic", "advance"})

_AGENT = "/api/agents/{agent_id}"
_ARCH = "/api/assistant/architect/conversations/{conversation_id}"
_PLAN_OP = f"{_ARCH}/evaluation-plan/operations/{{operation_id}}"
_GW = "/api/governance/gateways/{gateway_id}"

# (HTTP method, route.path_format) -> Billable | FREE
BILLING: dict[tuple[str, str], Billable | Free] = {
    # ---- agents: a deploy builds and provisions; invoke is a turn ----
    ("POST", "/api/agents"): Billable(SOURCE_DEPLOY, in_handler=True),
    ("POST", "/api/agents/discovery/import"): FREE,
    ("POST", "/api/agents/uploads"): FREE,
    ("DELETE", _AGENT): FREE,
    ("POST", f"{_AGENT}/channel-links"): FREE,
    ("POST", f"{_AGENT}/convert"): Billable(SOURCE_DEPLOY, in_handler=True),
    ("POST", f"{_AGENT}/inbound-auth"): FREE,
    ("POST", f"{_AGENT}/invoke"): Billable(SOURCE_INVOKE),
    ("POST", f"{_AGENT}/redeploy"): Billable(SOURCE_DEPLOY),
    # adopting the AWS console's prompt is a redeploy
    ("POST", f"{_AGENT}/prompt-drift/adopt"): Billable(SOURCE_DEPLOY),
    ("POST", f"{_AGENT}/release-bundles"): FREE,
    ("POST", f"{_AGENT}/release/block"): FREE,
    ("POST", f"{_AGENT}/release/evaluate"): Billable(SOURCE_EVAL),
    ("POST", f"{_AGENT}/release/migrate"): FREE,
    ("POST", f"{_AGENT}/release/rollback"): FREE,
    ("POST", f"{_AGENT}/release/sign"): FREE,
    ("POST", f"{_AGENT}/review-links"): FREE,
    ("POST", f"{_AGENT}/rules"): FREE,
    ("PUT", f"{_AGENT}/rules-enabled"): FREE,
    ("PUT", f"{_AGENT}/rules-order"): FREE,
    ("POST", f"{_AGENT}/rules/test"): FREE,  # a text match, no model call
    ("DELETE", f"{_AGENT}/rules/{{rule_id}}"): FREE,
    ("PATCH", f"{_AGENT}/rules/{{rule_id}}"): FREE,
    ("POST", f"{_AGENT}/share-links"): FREE,
    ("POST", f"{_AGENT}/snapshots/{{seq}}/rollback"): FREE,
    ("POST", f"{_AGENT}/waivers"): FREE,
    ("PUT", f"{_AGENT}/watch"): FREE,
    ("POST", f"{_AGENT}/watch/run"): Billable(SOURCE_EVAL),
    # ---- the architect assistant: a turn invokes the preset; approving deploys ----
    ("POST", "/api/assistant/architect/conversations"): FREE,
    ("DELETE", _ARCH): FREE,
    ("POST", f"{_ARCH}/catalog"): FREE,
    ("POST", f"{_ARCH}/digest/regenerate"): Billable(SOURCE_ARCHITECT),  # one Converse
    ("PUT", f"{_ARCH}/evaluation-plan"): FREE,
    ("POST", f"{_ARCH}/evaluation-plan/materialize"): Billable(SOURCE_ARCHITECT),
    ("DELETE", f"{_PLAN_OP}/assets"): FREE,
    ("POST", f"{_PLAN_OP}/lambda-revision-review"): FREE,
    ("POST", f"{_PLAN_OP}/retry"): FREE,
    ("POST", f"{_ARCH}/evaluation-plan/prepare"): FREE,
    ("PUT", f"{_ARCH}/preparation"): FREE,
    ("POST", f"{_ARCH}/preparation/skills"): FREE,
    ("PUT", f"{_ARCH}/proposal"): FREE,
    ("POST", f"{_ARCH}/proposal/approve"): Billable(SOURCE_ARCHITECT),
    ("POST", f"{_ARCH}/proposal/reject"): FREE,
    ("PUT", f"{_ARCH}/sharing"): FREE,
    ("POST", f"{_ARCH}/turns"): Billable(SOURCE_ARCHITECT),
    # ---- Chat: one turn per POST ----
    ("POST", "/api/chat/{agent_id}"): Billable(SOURCE_CHAT),
    ("POST", "/api/chat/{agent_id}/feedback"): FREE,
    ("POST", "/api/chat/{agent_id}/sessions/{session_id}/stop"): FREE,
    ("POST", "/api/chat/{agent_id}/turns/{turn_id}/stop"): FREE,
    # ---- the Agent IDE (app/coding): a turn runs codex on Bedrock in the coding
    # runtime; install builds that runtime (CodeBuild); deploy hands the repo to the
    # byoc pipeline (start_create / republish_agent). The rest is repo CRUD ----
    ("POST", "/api/coding/runtime/install"): Billable(SOURCE_DEPLOY),
    ("DELETE", "/api/coding/runtime"): FREE,
    ("POST", "/api/coding/repos"): FREE,
    ("DELETE", "/api/coding/repos/{repo_id}"): FREE,
    ("PATCH", "/api/coding/repos/{repo_id}"): FREE,
    ("POST", "/api/coding/repos/{repo_id}/deploy"): Billable(SOURCE_DEPLOY),
    ("POST", "/api/coding/repos/{repo_id}/download"): FREE,
    ("PUT", "/api/coding/repos/{repo_id}/file"): FREE,
    ("POST", "/api/coding/repos/{repo_id}/interrupt"): FREE,
    ("POST", "/api/coding/repos/{repo_id}/turns"): Billable(SOURCE_CODING),
    # ---- the COA context layer: connecting, consent and the demo account are
    # bookkeeping; the playground runs a COA agent turn (model tokens) ----
    ("PUT", "/api/context-layer"): FREE,
    ("DELETE", "/api/context-layer"): FREE,
    ("POST", "/api/context-layer/authorize"): FREE,
    ("POST", "/api/context-layer/demo-account/reveal"): FREE,
    ("POST", "/api/context-layer/playground"): Billable(SOURCE_CONTEXT_LAYER),
    ("POST", "/api/context-layer/revoke"): FREE,
    # the managed COA gateway: control-plane writes (+ one COA tools/list read)
    ("PUT", "/api/context-layer/gateway"): FREE,
    ("POST", "/api/context-layer/gateway/refresh"): FREE,
    ("DELETE", "/api/context-layer/gateway"): FREE,
    # ---- Studio local debug and the AI fix: model calls on the host ----
    ("POST", "/api/conversations"): FREE,
    ("DELETE", "/api/conversations/{session_id}"): FREE,
    ("PUT", "/api/conversations/{session_id}/code"): FREE,
    ("POST", "/api/conversations/{session_id}/messages"): Billable(SOURCE_STUDIO),
    ("POST", "/api/conversations/{session_id}/messages/stream"): Billable(SOURCE_STUDIO),
    ("POST", "/api/execute"): Billable(SOURCE_STUDIO),
    ("POST", "/api/execute/stream"): Billable(SOURCE_STUDIO),
    ("POST", "/api/fix-code/stream"): Billable(SOURCE_CODEGEN),
    # ---- tools: a Gateway tools/call, Code Interpreter / Browser sessions ----
    ("POST", "/api/demos/browser"): Billable(SOURCE_TOOLS),
    ("DELETE", "/api/demos/browser/{session_id}"): FREE,
    ("POST", "/api/demos/code-interpreter"): Billable(SOURCE_TOOLS),
    ("POST", "/api/tools/call"): Billable(SOURCE_TOOLS),
    # ---- evaluation: runs, recommendations and online judging bill evaluations ----
    ("POST", "/api/eval/datasets"): FREE,
    ("DELETE", "/api/eval/datasets/cloud/{cloud_id}"): FREE,
    ("POST", "/api/eval/datasets/cloud/{cloud_id}/publish-version"): FREE,
    ("DELETE", "/api/eval/datasets/cloud/{cloud_id}/versions/{version}"): FREE,
    ("POST", "/api/eval/datasets/from-sessions"): FREE,
    ("POST", "/api/eval/datasets/upload"): FREE,
    ("DELETE", "/api/eval/datasets/{dataset_id}"): FREE,
    ("PUT", "/api/eval/datasets/{dataset_id}"): FREE,
    ("POST", "/api/eval/datasets/{dataset_id}/publish-version"): FREE,
    ("POST", "/api/eval/datasets/{dataset_id}/sync-to-aws"): FREE,
    ("POST", "/api/eval/evaluators"): FREE,
    ("DELETE", "/api/eval/evaluators/{evaluator_id}"): FREE,
    ("PUT", "/api/eval/evaluators/{evaluator_id}"): FREE,
    ("POST", "/api/eval/online"): Billable(SOURCE_EVAL),
    ("DELETE", "/api/eval/online/{config_id}"): FREE,
    ("PATCH", "/api/eval/online/{config_id}"): FREE,
    ("POST", "/api/eval/online/{config_id}/pause"): FREE,
    ("POST", "/api/eval/online/{config_id}/reports"): Billable(SOURCE_EVAL),
    ("POST", "/api/eval/online/{config_id}/resume"): Billable(SOURCE_EVAL),
    ("POST", "/api/eval/pipelines"): FREE,
    ("POST", "/api/eval/pipelines/preview-logs"): FREE,
    ("DELETE", "/api/eval/pipelines/{pipeline_id}"): FREE,
    ("PUT", "/api/eval/pipelines/{pipeline_id}"): FREE,
    ("POST", "/api/eval/pipelines/{pipeline_id}/run"): FREE,  # a dataset from logs
    ("POST", "/api/eval/runs"): Billable(SOURCE_EVAL),
    ("POST", "/api/eval/runs/estimate"): FREE,
    ("DELETE", "/api/eval/runs/{run_id}"): FREE,
    ("POST", "/api/eval/runs/{run_id}/criteria/snapshot"): FREE,
    ("POST", "/api/eval/runs/{run_id}/recheck"): FREE,
    ("POST", "/api/eval/runs/{run_id}/recommendations"): Billable(SOURCE_EVAL),
    ("POST", "/api/eval/runs/{run_id}/recommendations/{rec_id}/accept"): FREE,
    ("PUT", "/api/eval/runs/{run_id}/recommendations/{rec_id}/edit"): FREE,
    # a new run of the same scope: bills exactly as POST /api/eval/runs
    ("POST", "/api/eval/runs/{run_id}/rerun"): Billable(SOURCE_EVAL),
    ("POST", "/api/eval/runs/{run_id}/stop"): FREE,
    ("POST", "/api/observability/prices/refresh"): FREE,
    # transcript previews for the task wizard: a read, POST only for the body
    ("POST", "/api/observability/session-previews"): FREE,
    ("POST", "/api/observability/sessions/{session_id}/evaluate"): Billable(SOURCE_EVAL),
    # ---- golden sets: curation only ----
    ("POST", "/api/golden-sets"): FREE,
    ("DELETE", "/api/golden-sets/{dataset_id}"): FREE,
    ("POST", "/api/golden-sets/{dataset_id}/items"): FREE,
    ("POST", "/api/golden-sets/{dataset_id}/move"): FREE,
    ("POST", "/api/golden-sets/{dataset_id}/retire"): FREE,
    ("POST", "/api/golden-sets/{dataset_id}/seed"): FREE,
    # ---- experiments and canaries: billable actions only (gated in the handler) ----
    ("POST", "/api/experiments"): FREE,
    ("POST", "/api/experiments/{exp_id}/action"): Billable(
        SOURCE_EXPERIMENT, actions=EXPERIMENT_BILLABLE_ACTIONS),
    ("POST", "/api/runtime-canaries"): FREE,  # the ledger row; setup mints the candidate
    ("POST", "/api/runtime-canaries/{canary_id}/action"): Billable(
        SOURCE_CANARY, actions=CANARY_BILLABLE_ACTIONS),
    # ---- Skill Lab: a job runs the engine (model calls) ----
    ("POST", "/api/skill-lab/jobs"): Billable(SOURCE_SKILL_LAB),
    ("DELETE", "/api/skill-lab/jobs/{job_id}"): FREE,
    ("POST", "/api/skill-lab/jobs/{job_id}/apply-expansion"): FREE,
    ("POST", "/api/skill-lab/jobs/{job_id}/cancel"): FREE,
    ("POST", "/api/skill-lab/jobs/{job_id}/import-taskset"): FREE,
    ("POST", "/api/skill-lab/jobs/{job_id}/publish"): FREE,
    ("POST", "/api/skill-lab/jobs/{job_id}/resume"): Billable(SOURCE_SKILL_LAB),
    ("POST", "/api/skill-lab/task-assets"): FREE,
    ("POST", "/api/skill-lab/tasksets"): FREE,
    ("DELETE", "/api/skill-lab/tasksets/{taskset_id}"): FREE,
    ("PUT", "/api/skill-lab/tasksets/{taskset_id}"): FREE,
    # ---- knowledge bases: ingestion (embedding tokens) and retrieval ----
    ("POST", "/api/knowledge-bases"): FREE,
    ("POST", "/api/knowledge-bases/ensure-gateway"): FREE,
    ("DELETE", "/api/knowledge-bases/{kb_id}"): FREE,
    ("PATCH", "/api/knowledge-bases/{kb_id}"): FREE,
    ("POST", "/api/knowledge-bases/{kb_id}/data-sources"): FREE,
    ("DELETE", "/api/knowledge-bases/{kb_id}/data-sources/{ds_id}"): FREE,
    ("POST", "/api/knowledge-bases/{kb_id}/data-sources/{ds_id}/sync"): Billable(
        SOURCE_KNOWLEDGE),
    ("POST", "/api/knowledge-bases/{kb_id}/files"): FREE,  # the upload; sync ingests
    ("POST", "/api/knowledge-bases/{kb_id}/migrate-to-project"): FREE,
    ("POST", "/api/knowledge-bases/{kb_id}/migrate-to-project/finish"): FREE,
    ("POST", "/api/knowledge-bases/{kb_id}/query"): Billable(SOURCE_KNOWLEDGE),
    # ---- governance: NL policy authoring and the policy test bill tokens / a call ----
    ("POST", f"{_GW}/engine"): FREE,
    ("POST", f"{_GW}/generations"): Billable(SOURCE_GOVERNANCE),
    ("DELETE", f"{_GW}/manage"): FREE,
    ("POST", f"{_GW}/manage"): FREE,
    ("POST", f"{_GW}/mode"): FREE,
    ("POST", f"{_GW}/policies"): FREE,
    ("DELETE", f"{_GW}/policies/{{policy_id}}"): FREE,
    ("PUT", f"{_GW}/policies/{{policy_id}}"): FREE,
    ("POST", f"{_GW}/policies/{{policy_id}}/promote"): FREE,
    ("POST", f"{_GW}/policies/{{policy_id}}/rollback"): FREE,
    ("POST", f"{_GW}/rate-limits"): FREE,
    ("DELETE", f"{_GW}/rate-limits/{{rate_limit_id}}"): FREE,
    ("PUT", f"{_GW}/rate-limits/{{rate_limit_id}}"): FREE,
    ("POST", f"{_GW}/registry-import"): FREE,
    ("POST", f"{_GW}/retire-legacy-records"): FREE,
    ("POST", f"{_GW}/targets/{{target_id}}/synchronize"): FREE,
    ("POST", "/api/governance/guardrail"): FREE,
    ("POST", "/api/governance/policy-generation"): Billable(SOURCE_GOVERNANCE),
    ("POST", "/api/governance/policy-test"): Billable(SOURCE_GOVERNANCE),
    # ---- MCP server operations: re-syncs and reviews, no model call ----
    ("POST", "/api/mcp-servers/{agent_id}/health"): FREE,
    ("POST", "/api/mcp-servers/{agent_id}/policies/scaffold"): FREE,
    ("POST", "/api/mcp-servers/{agent_id}/reviews/check"): FREE,
    ("POST", "/api/mcp-servers/{agent_id}/reviews/{review_id}/approve"): FREE,
    ("POST", "/api/mcp-servers/{agent_id}/reviews/{review_id}/reject"): FREE,
    ("POST", "/api/mcp-servers/{agent_id}/rollback"): FREE,
    # ---- memory: workspace overhead, never a project's turn ----
    ("POST", "/api/memory/records/search"): FREE,
    ("POST", "/api/memory/resources"): FREE,
    ("DELETE", "/api/memory/resources/{memory_id}"): FREE,
    ("PUT", "/api/memory/resources/{memory_id}"): FREE,
    ("POST", "/api/memory/resources/{memory_id}/adopt"): FREE,
    # ---- a model probe: one call to the chosen model ----
    ("POST", "/api/model-probe"): Billable(SOURCE_MODELS_PROBE),
    # ---- promotions: executing deploys into the target and smoke-invokes it ----
    ("POST", "/api/promotions"): FREE,
    ("POST", "/api/promotions/{promotion_id}/execute"): Billable(SOURCE_DEPLOY),
    ("POST", "/api/promotions/{promotion_id}/review"): FREE,
    ("POST", "/api/promotions/{promotion_id}/rollback"): FREE,
    # ---- registry: control plane; the A2A demo is an invoke ----
    ("POST", "/api/registry/a2a-demo"): Billable(SOURCE_INVOKE),
    ("POST", "/api/registry/records"): FREE,
    ("DELETE", "/api/registry/records/{record_id}"): FREE,
    ("PUT", "/api/registry/records/{record_id}"): FREE,
    ("POST", "/api/registry/records/{record_id}/action"): FREE,
    ("POST", "/api/registry/records/{record_id}/reimport"): FREE,
    ("POST", "/api/registry/skills/capabilities/git-install"): FREE,
    ("POST", "/api/registry/skills/import"): FREE,
    ("POST", "/api/registry/skills/inspect"): FREE,
    ("POST", "/api/registry/sync-defaults"): FREE,
}


def is_billable_prefix(path_format: str) -> bool:
    return any(path_format == p or path_format.startswith(f"{p}/")
               for p in BILLABLE_PREFIXES)


def billing_of(method: str, path_format: str) -> Billable | Free | None:
    return BILLING.get((method, path_format))


def billable_routes() -> list[tuple[str, str]]:
    """Every billable (method, path) — the documented list."""
    return sorted(key for key, value in BILLING.items() if isinstance(value, Billable))


def _request_project(request: Request) -> str | None:
    scope: Any = getattr(request.state, "project", None)
    return getattr(scope, "id", None) if scope is not None else None


def enforce_budget(request: Request) -> None:
    """App-level dependency after the route policy: a billable route of a project at
    or over its monthly budget is refused (409 `project.budget_exhausted`)."""
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return
    route: Any = request.scope.get("route")
    path_format = getattr(route, "path_format", None) or request.url.path
    rule = billing_of(request.method, path_format)
    if not isinstance(rule, Billable) or rule.actions is not None or rule.in_handler:
        return
    project_id = _request_project(request)
    if not project_id:
        return  # no project: platform / system work is not a project's spend
    from app.services.project_quotas import assert_project_budget

    assert_project_budget(project_id)


def gate_action(project_id: str | None, action: str, actions: frozenset[str]) -> None:
    """An action dispatcher's gate: only the billable actions meet the budget."""
    if action not in actions:
        return
    from app.services.project_quotas import assert_project_budget

    assert_project_budget(project_id)
