"""Billing classification table (core/billing_policy.py).

Like ROUTE_POLICY, the table is only trustworthy if it cannot drift: every live
non-GET route under ``/api`` and ``/v1`` has a row, every row names a live route,
and the entry points the research named as billable stay billable.
"""

import pytest
from starlette.requests import Request

from app.core import billing_policy
from app.core.billing_policy import BILLING, FREE, SOURCES, Billable
from app.core.errors import AppError
from app.core.route_policy import ROUTE_POLICY
from app.main import create_app
from app.services import budgets


def _live_non_get_routes(app) -> set[tuple[str, str]]:
    """Every (method, path_format) the app serves under /api or /v1, minus reads.
    Recurses through FastAPI 0.139's `_IncludedRouter` wrappers."""

    def walk(routes):
        found = []
        for route in routes:
            if type(route).__name__ == "_IncludedRouter":
                found += walk(route.original_router.routes)
            else:
                found.append(route)
        return found

    pairs = set()
    for route in walk(app.routes):
        path = getattr(route, "path_format", None) or getattr(route, "path", "")
        if not (path.startswith("/api/") or path.startswith("/v1/")):
            continue
        for method in getattr(route, "methods", None) or []:
            if method in {"GET", "HEAD", "OPTIONS"}:
                continue
            pairs.add((method, path))
    return pairs


def test_every_non_get_route_is_classified():
    live = _live_non_get_routes(create_app())
    missing = sorted(live - set(BILLING))
    assert not missing, (
        "non-GET routes missing from core/billing_policy.BILLING — classify each as "
        f"Billable(source) or FREE: {missing}"
    )


def test_every_row_names_a_live_route():
    live = _live_non_get_routes(create_app())
    stale = sorted(set(BILLING) - live)
    assert not stale, f"BILLING rows with no live route (remove them): {stale}"


def test_api_rows_agree_with_the_route_policy():
    """A console row the role table does not know would be classified for a route
    that cannot be served at all."""
    api_rows = {key for key in BILLING if key[1].startswith("/api/")}
    assert api_rows <= set(ROUTE_POLICY)
    assert all(method != "GET" for method, _ in BILLING)


def test_billable_rows_name_known_sources():
    for key, row in BILLING.items():
        if isinstance(row, Billable):
            assert row.source in SOURCES, key
        else:
            assert row is FREE, key


# The research's entry points (sources-and-metering.md §4) plus the routes the core
# child gates for the jobs child. A flip to FREE has to be a deliberate edit here.
REQUIRED_BILLABLE = {
    ("POST", "/api/chat/{agent_id}"): billing_policy.SOURCE_CHAT,
    ("POST", "/api/agents/{agent_id}/invoke"): billing_policy.SOURCE_INVOKE,
    ("POST", "/api/registry/a2a-demo"): billing_policy.SOURCE_INVOKE,
    ("POST", "/api/assistant/architect/conversations/{conversation_id}/turns"):
        billing_policy.SOURCE_ARCHITECT,
    ("POST", "/api/observability/sessions/{session_id}/evaluate"):
        billing_policy.SOURCE_OBSERVABILITY,
    ("POST", "/api/fix-code/stream"): billing_policy.SOURCE_CODEGEN,
    ("POST", "/api/eval/runs"): billing_policy.SOURCE_EVAL,
    ("POST", "/api/eval/runs/{run_id}/recommendations"): billing_policy.SOURCE_EVAL,
    ("POST", "/api/eval/online"): billing_policy.SOURCE_EVAL,
    ("POST", "/api/eval/online/{config_id}/resume"): billing_policy.SOURCE_EVAL,
    ("POST", "/api/eval/online/{config_id}/reports"): billing_policy.SOURCE_EVAL,
    ("POST", "/api/experiments"): billing_policy.SOURCE_EXPERIMENT,
    ("POST", "/api/experiments/{exp_id}/action"): billing_policy.SOURCE_EXPERIMENT,
    ("POST", "/api/runtime-canaries"): billing_policy.SOURCE_CANARY,
    ("POST", "/api/runtime-canaries/{canary_id}/action"): billing_policy.SOURCE_CANARY,
    ("POST", "/api/skill-lab/jobs"): billing_policy.SOURCE_SKILL_LAB,
    ("POST", "/api/skill-lab/jobs/{job_id}/resume"): billing_policy.SOURCE_SKILL_LAB,
    ("POST", "/api/knowledge-bases/{kb_id}/query"): billing_policy.SOURCE_KNOWLEDGE,
    ("POST", "/api/knowledge-bases/{kb_id}/data-sources/{ds_id}/sync"):
        billing_policy.SOURCE_KNOWLEDGE,
    ("POST", "/v1/agents/{agent_id}/invoke"): billing_policy.SOURCE_V1,
    ("POST", "/v1/agents/{agent_id}/invoke-stream"): billing_policy.SOURCE_V1,
}


def test_the_entry_points_are_billable():
    for key, source in REQUIRED_BILLABLE.items():
        row = BILLING[key]
        assert isinstance(row, Billable), key
        assert row.source == source, key


def test_teardown_and_crud_stay_free():
    """Stopping spend must never be refused for a payer over budget."""
    for key in (
        ("POST", "/api/eval/online/{config_id}/pause"),
        ("POST", "/api/eval/runs/{run_id}/stop"),
        ("POST", "/api/skill-lab/jobs/{job_id}/cancel"),
        ("POST", "/api/chat/{agent_id}/sessions/{session_id}/stop"),
        ("DELETE", "/api/agents/{agent_id}"),
        ("POST", "/api/agents"),
    ):
        assert BILLING[key] is FREE, key


def test_action_dispatchers_name_their_billable_actions():
    exp = BILLING[("POST", "/api/experiments/{exp_id}/action")]
    can = BILLING[("POST", "/api/runtime-canaries/{canary_id}/action")]
    assert exp.actions == {"recommend", "traffic", "gateway", "abtest"}
    # advance: the ramp checks the caller's budget before each stage (jobs child)
    assert can.actions == {"setup", "traffic", "advance"}
    assert "cleanup" not in exp.actions and "rollback" not in can.actions
    assert "complete" not in can.actions and "verdict" not in can.actions


class _Route:
    def __init__(self, path_format: str) -> None:
        self.path_format = path_format


def _request(path_format: str, payer: budgets.Payer, method: str = "POST") -> Request:
    return Request({
        "type": "http", "method": method, "path": path_format, "headers": [],
        "query_string": b"", "client": ("127.0.0.1", 1), "route": _Route(path_format),
        "state": {"budget_payer": payer}, "path_params": {},
    })


def _over_budget_payer() -> budgets.Payer:
    from app.core.db import SessionLocal
    from app.models.budgets import UsageEvent

    db = SessionLocal()
    try:
        db.add(UsageEvent(user_id="u-over", user_name="over", period=budgets.period_of(),
                          source="chat", usd=5.0))
        db.commit()
    finally:
        db.close()
    return budgets.Payer(user_id="u-over", name="over", limit_usd=5.0,
                         basis=budgets.BASIS_LEVEL)


def test_gate_action_refuses_only_the_billable_actions():
    payer = _over_budget_payer()
    path = "/api/experiments/{exp_id}/action"
    with pytest.raises(AppError) as refused:
        billing_policy.gate_action(_request(path, payer), "traffic")
    assert refused.value.status_code == 402 and refused.value.code == "budget.exceeded"
    for action in ("cleanup", "verdict", "accept", "promote"):
        billing_policy.gate_action(_request(path, payer), action)  # never refused
    canary = "/api/runtime-canaries/{canary_id}/action"
    with pytest.raises(AppError):
        billing_policy.gate_action(_request(canary, payer), "setup")
    billing_policy.gate_action(_request(canary, payer), "rollback")


def test_the_route_dependency_skips_action_dispatchers_and_reads():
    payer = _over_budget_payer()
    # the dependency never gates a dispatcher (the handler does, per action) …
    billing_policy.enforce(_request("/api/experiments/{exp_id}/action", payer), (
        "/api/experiments/{exp_id}/action"))
    # … nor a FREE route, nor a GET
    billing_policy.enforce(_request("/api/agents", payer), "/api/agents")
    billing_policy.enforce(_request("/api/chat/{agent_id}", payer, method="GET"),
                           "/api/chat/{agent_id}")
