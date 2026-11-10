"""CN extras of workspace isolation (Family B step 2, r12-d) — what upstream #262 / #263
could not cover because the surfaces are CN-only.

* Visibility: a hidden agent answers 404 on invoke / chat before any AWS call (the former
  Cedar invoke gate is gone — Global parity, 2026-10-27; policies govern tool calls only).
* A runtime canary needs BOTH arms visible (champion and challenger are agent rows).
* Policies stay shared (`test_agent_visibility.py` covers the picker); the two new
  workspace routes are admin-only and hub-global in the route policy.
"""

import io

import pytest

from app.services import invoke as invoke_service
from tests import test_agent_visibility as tav
from tests.test_agent_visibility import _add, _isolate, _refused
from tests.test_policy_deploy_narrowing import (  # noqa: F401 — fixtures by name
    PERMIT_HR,
    _publish,
    _set_mode,
    engine,
    s3,
    workspace,
)


class _Plane:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def invoke_agent_runtime(self, **kwargs):
        self.calls.append(kwargs)
        return {"response": io.BytesIO(b'{"result": "ok"}'), "contentType": "application/json"}


@pytest.fixture
def plane(monkeypatch) -> _Plane:
    stub = _Plane()
    monkeypatch.setattr(invoke_service, "data_client", lambda _ws=None: stub)
    return stub

# the auth-enabled console, its members and their agents
app, admin, ada, bob, world = tav.app, tav.admin, tav.ada, tav.bob, tav.world
no_real_deploy = tav.no_real_deploy


def test_hidden_agent_is_404_and_a_visible_one_invokes_ungated(
    engine, s3, workspace, plane, world,  # noqa: F811 — pytest fixtures imported by name
):
    """Global parity: invoking is not a policy decision. With an ACTIVE policy set in
    `enforce`, bob's own agent invokes (no gate), ada's hidden agent is 404 `agent.not_found`
    on both entrances before any AWS call."""
    _publish(workspace, "permit_hr", PERMIT_HR)
    _set_mode("enforce")
    _isolate()
    own = world.bob.post(f"/api/agents/{world.bob_agent}/invoke", json={"prompt": "hi"})
    assert own.status_code == 200, own.text
    assert len(plane.calls) == 1
    _refused(world.bob, "agent.not_found",
             ("POST", f"/api/agents/{world.ada_agent}/invoke", {"prompt": "hi"}),
             ("POST", f"/api/chat/{world.ada_agent}", {"prompt": "hi"}))
    assert len(plane.calls) == 1  # the hidden agent's paths never reached the data plane


def test_runtime_canary_needs_both_arms_visible(monkeypatch, world):
    from app.optimization import canary_routers
    from app.optimization.models import RuntimeCanary

    monkeypatch.setattr(canary_routers.canary_service, "stage_not_ready_reason",
                        lambda *a: "not now")
    can = _add(RuntimeCanary(name="c", champion_agent_id=world.bob_agent,
                             champion_agent_name="bob-agent",
                             challenger_agent_id=world.ada_agent,
                             challenger_agent_name="ada-agent", created_by=world.bob_id))
    _isolate()
    _refused(world.bob, "agent.not_found",
             ("POST", f"/api/runtime-canaries/{can}/action", {"action": "setup"}))
    # the verdict read and cleanup never touch either agent
    res = world.bob.post(f"/api/runtime-canaries/{can}/action", json={"action": "cleanup"})
    assert res.status_code == 409 and res.json()["code"] == "canary.stage_not_ready"
    res = world.admin.post(f"/api/runtime-canaries/{can}/action", json={"action": "setup"})
    assert res.json()["code"] == "canary.stage_not_ready"


def test_visibility_routes_are_admin_only_and_hub_global():
    from app.core.route_policy import ADMIN, ROUTE_POLICY, WORKSPACE_EXEMPT

    for method in ("GET", "PUT"):
        key = (method, "/api/workspaces/{workspace_id}/settings/visibility")
        assert ROUTE_POLICY[key] == ADMIN
        assert key in WORKSPACE_EXEMPT


def test_the_a2a_registry_record_of_a_hidden_agent_disappears_from_the_overview(world):
    """The hub-global registry count drops the hidden agents' A2A records only; MCP and
    skill records stay shared and counted."""
    from app.services import registry_backend
    from tests.test_agent_visibility import _StubRegistry

    records = [{"recordId": "rec-ada", "descriptorType": "A2A", "status": "APPROVED"},
               {"recordId": "rec-mcp", "descriptorType": "MCP", "status": "APPROVED"}]
    import app.routers.overview as overview_mod

    registry_backend.set_backend(_StubRegistry(records))
    overview_mod._cache.clear()
    try:
        _isolate()
        assets = world.bob.get("/api/overview").json()["registry_assets"]
        assert (assets["agents"], assets["tools"], assets["total"]) == (0, 1, 1)
        assets = world.ada.get("/api/overview").json()["registry_assets"]
        assert (assets["agents"], assets["tools"]) == (1, 1)
    finally:
        registry_backend.set_backend(None)
        overview_mod._cache.clear()


def test_by_id_chat_surfaces_are_own_only_in_both_modes_observability_only_under_isolation(
    monkeypatch, world
):
    """N16-obs70 / N16-obs71 (2026-10-19, supersedes the 2026-10-16 "by-id stays open under
    shared" ruling for CHAT): the chat stop / memory rail answer another member's session
    404 in BOTH modes — one `_session_for` choke point, the regression matrix is
    `test_chat_session_ownership.py`. The trace rail / observability details keep the
    accepted design (E04 §7): workspace-wide under `shared`, the same own-sessions
    predicate under `isolated`, where the lists also drop the rows."""
    from types import SimpleNamespace

    import app.routers.chat as chat_router
    import app.routers.observability as obs_router
    from app.models.ledger import ChatMessage, ChatSession
    from app.services import memory_backend as mb
    from app.services import observability as obs

    sid, other = "sess-ada-byid-0001", "sess-nobody-0001"
    _add(ChatSession(agent_id=world.legacy_agent, session_id=sid, actor_id="ada",
                     created_by=world.ada_id, created_by_name="ada", turns=1))
    _add(ChatMessage(agent_id=world.legacy_agent, session_id=sid, role="user", text="hi"))
    monkeypatch.setattr(chat_router, "stop_agent_session",
                        lambda *a, **k: {"ended": True, "already_ended": False})
    monkeypatch.setattr(mb, "get_backend", lambda: SimpleNamespace(
        session_memory_summary=lambda *a, **k: {"short_term": [], "long_term": []}))
    rows = [{"trace_id": "a" * 32, "service": "legacy_agent.DEFAULT", "agent": "legacy-agent",
             "session_id": s, "status": "ok"} for s in (sid, other)]
    monkeypatch.setattr(obs, "list_traces", lambda *a, **k: {"traces": list(rows)})
    monkeypatch.setattr(obs, "list_sessions", lambda *a, **k: {"sessions": list(rows)})
    monkeypatch.setattr(obs, "get_session", lambda s, *a, **k: {"session_id": s, "traces": []})
    probes = []

    def local_source(queries, *args, **kwargs):
        probes.append(queries)
        return {"ownership": [{"service": "legacy_agent.DEFAULT", "record_count": "1",
                               "attributed_count": "1", "service_count": "1"}]}

    monkeypatch.setattr(obs, "run_insights_queries", local_source)
    monkeypatch.setattr(obs_router.trace_service, "session_trace",
                        lambda s, ws, **k: {"session_id": s, "spans": [], "span_count": 0})

    base = f"/api/chat/{world.legacy_agent}"
    calls = (
        ("POST", f"{base}/sessions/{sid}/stop"),
        ("GET", f"{base}/memory?session_id={sid}"),
        ("GET", f"/api/traces/{sid}"),
        ("GET", f"/api/observability/sessions/{sid}"),
    )
    # shared: the chat routes are ada's alone (N16-obs70/71) …
    _refused(world.bob, "chat.session_not_found", *calls[:2])
    for method, url in calls[2:]:  # … the observability by-id surfaces stay open
        res = world.bob.request(method, url)
        assert res.status_code == 200, (url, res.text)
    sessions = world.bob.get("/api/observability/sessions").json()["sessions"]
    listed = {r["session_id"] for r in sessions}
    assert listed == {sid, other}

    _isolate()
    _refused(world.bob, "chat.session_not_found", *calls[:3])
    before = len(probes)
    _refused(world.bob, "observability.session_not_found", calls[3])
    assert len(probes) == before  # isolation rejects before any remote source probe
    for path, key in (("sessions", "sessions"), ("traces", "traces")):
        ids = {r["session_id"] for r in world.bob.get(f"/api/observability/{path}").json()[key]}
        assert ids == {other}, path  # the row with no ledger session stays
    for client in (world.ada, world.admin):
        for method, url in calls:
            assert client.request(method, url).status_code == 200, (url, client)
