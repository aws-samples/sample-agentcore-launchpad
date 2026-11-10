"""The policy simulator, the entity catalog and the handler-level permission checks over HTTP
under the Global model (``/api/policy-engines/{id}/…``). The engine / attachment lifecycle and
the migration are in ``test_policy_engines_api.py``; the exact response key sets in
``test_policies_contract.py``."""

import json
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.core import config
from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.main import create_app
from app.models.ledger import Agent, ApiKey
from app.services import aws_clients, policy_backend, policy_engines
from app.services import users as users_service
from app.services.policy_cedar import CedarEngine
from tests.policy_fakes import BASELINE, BROKEN, FORBID_KEY, TOOLS_ADMIN_ONLY, FakeEngine, FakeS3

WS = DEFAULT_WORKSPACE_ID


@pytest.fixture
def s3(monkeypatch):
    fake = FakeS3()
    monkeypatch.setattr(aws_clients, "client", lambda service, ctx, **kw: fake)
    monkeypatch.setattr(aws_clients, "get_session", lambda *a, **k: pytest.fail("session"))
    monkeypatch.setattr(policy_engines, "interceptor_hook",
                        lambda db, facts, attach: {"interceptor": "skipped"})
    return fake


@pytest.fixture
def api(client, s3):
    """The open-console client (built-in admin) with the real engine registered —
    `create_app` registers it, the autouse reset clears it after."""
    assert isinstance(policy_backend.backend_or_none(), CedarEngine)
    return client


@pytest.fixture
def eid(api) -> str:
    res = api.post("/api/policy-engines", json={"name": "hr_engine"})
    assert res.status_code == 201, res.text
    return res.json()["id"]


def _seed(agent_id="ag1", name="hr-agent", spec=None, key=None):
    db = SessionLocal()
    try:
        # `created_by_name` is what the Cedar `Agent.owner` attribute carries (Family B step 1)
        db.add(Agent(id=agent_id, workspace_id=WS, name=name, method="zip_runtime",
                     status="active", spec=spec or {}, created_by_name="river"))
        if key:
            db.add(ApiKey(id=key, workspace_id=WS, name="ci-key", prefix="lp_live_ab12",
                          key_hash=f"hash-{key}", created_by_name="river"))
        db.commit()
    finally:
        db.close()


def _create(api, eid, name="baseline", statement=BASELINE, **extra):
    res = api.post(f"/api/policy-engines/{eid}/policies",
                   json={"name": name, "statement": statement, **extra})
    assert res.status_code == 201, res.text
    return res.json()


def _publish(api, eid, policy_id, **body):
    res = api.post(f"/api/policy-engines/{eid}/policies/{policy_id}/publish", json=body)
    assert res.status_code == 200, res.text
    return res.json()


def test_create_app_registers_the_cedar_engine(client):
    assert isinstance(policy_backend.backend_or_none(), CedarEngine)


def test_default_capabilities_list_the_policy_feature(api):
    assert "policy" in api.get("/api/capabilities").json()["features"]


# ── validate / format ─────────────────────────────────────────────────────


def test_validate_and_format_endpoints(api, eid):
    base = f"/api/policy-engines/{eid}"
    ok = api.post(f"{base}/validate", json={"statement": BASELINE}).json()
    # the 4-statement call_tool BASELINE (workspace agents + member + admin + System::"hub")
    assert ok["ok"] is True and ok["policy_ids"] == ["policy0", "policy1", "policy2", "policy3"]
    bad = api.post(f"{base}/validate", json={"statement": BROKEN}).json()
    assert bad["ok"] is False and bad["errors"][0]["msg"]
    typo = api.post(f"{base}/validate", json={
        "statement": 'permit(principal == Launchpad::Member::"x", action, resource);'}).json()
    assert typo["ok"] is False and "Launchpad::Member" in typo["errors"][0]["msg"]
    fmt = api.post(f"{base}/format", json={"statement": "permit(principal,action,resource);"})
    assert fmt.json()["statement"].strip() == "permit (principal, action, resource);"
    broken = api.post(f"{base}/format", json={"statement": BROKEN})
    assert broken.status_code == 422 and broken.json()["code"] == "policy.invalid_statement"
    assert api.post(f"{base}/validate", json={"statement": ""}).status_code == 422
    invoke = api.post(f"{base}/policies", json={
        "name": "legacy", "statement": 'permit(principal, action == Launchpad::Action::"invoke", '
                                       'resource);'})
    assert invoke.status_code == 422 and invoke.json()["detail"]["reason"] == "invoke_retired"


# ── simulate ──────────────────────────────────────────────────────────────


def _sim(api, eid, principal, action, resource, **extra):
    return api.post(f"/api/policy-engines/{eid}/simulate", json={
        "principal": principal, "action": action, "resource": resource, **extra})


def test_simulate_matrix_drafts_pinned_and_record(api, eid, s3):
    """Global parity: the simulator decides Gateway tool calls only — a ``tool`` resource with
    ``call_tool`` / ``list_tool`` and the calling agent in ``context.agent_id``."""
    _seed(key="key_1")
    member = {"kind": "user", "id": "river", "role": "member"}
    key = {"kind": "api_key", "id": "key_1"}
    agent = {"kind": "agent", "id": "ag1"}
    employee = {"kind": "tool", "id": "hr-database___get_employee"}
    payout = {"kind": "tool", "id": "hr-database___create_payout"}
    on_ag1 = {"agent_id": "ag1"}

    before = _sim(api, eid, member, "call_tool", employee, context=on_ag1).json()
    # an EMPTY active set is Cedar's default-deny (`policy_empty_set_decision` defaults to
    # `deny` — exactly what the gateway interceptor answers an attached engine without a permit)
    assert before["outcome"] == "DENY" and before["determining_policy_ids"] == []
    assert before["reason"] is None and before["policy_ids"] == []
    assert before["mode"] == "log_only" and before["source_set"] == "active"
    assert before["request"]["principal"] == {"type": "Launchpad::User", "id": "river"}
    assert before["request"]["context"] == {"channel": "chat", "agent_id": "ag1"}

    created = _create(api, eid)
    pid = created["id"]
    draft = _sim(api, eid, member, "call_tool", employee, context=on_ag1,
                 include_drafts=True).json()
    assert draft["outcome"] == "ALLOW" and draft["source_set"] == "draft"
    assert draft["determining_policy_ids"] == [f"{pid}:v1"]
    assert draft["determining"] == [f"{pid}:v1#0"] and draft["policy_ids"] == [pid]
    _publish(api, eid, pid)
    assert _sim(api, eid, member, "call_tool", employee, context=on_ag1).json()["outcome"] == \
        "ALLOW"
    assert _sim(api, eid, {"kind": "system", "id": "hub"}, "call_tool", employee,
                context=on_ag1).json()["outcome"] == "ALLOW"
    denied_key = _sim(api, eid, key, "call_tool", employee, context=on_ag1).json()
    assert denied_key["outcome"] == "DENY" and denied_key["request"]["context"]["channel"] == "v1"
    # the agent principal (Stage A) decides its own calls under the workspace permit
    assert _sim(api, eid, agent, "call_tool", employee).json()["outcome"] == "ALLOW"

    # a pinned draft version: the editor's "test this draft"
    forbid = BASELINE + FORBID_KEY.replace("__KEY__", "key_1")
    api.put(f"/api/policy-engines/{eid}/policies/{pid}", json={"statement": forbid})
    pinned = _sim(api, eid, key, "call_tool", employee, context=on_ag1,
                  policy_version={"policy_id": pid, "version": 2}).json()
    assert pinned["source_set"] == "pinned" and pinned["outcome"] == "DENY"
    assert api.post(f"/api/policy-engines/{eid}/simulate", json={
        "principal": member, "action": "call_tool", "resource": employee,
        "policy_version": {"policy_id": pid, "version": 9}}).status_code == 404

    # the gateway attachment's mode is what the answer reports
    api.put("/api/governance/gateways/launchpad-gw-x/policy-engine", json={"engine_id": eid,
                                                              "mode": "ENFORCE"})
    enforced = _sim(api, eid, member, "call_tool", employee, context=on_ag1,
                    gateway_id="launchpad-gw-x").json()
    assert enforced["mode"] == "enforce" and enforced["gateway_id"] == "launchpad-gw-x"

    # record: one simulate row with the engine, the gateway and the session
    admin_only = _create(api, eid, "tools_admin_only", TOOLS_ADMIN_ONLY)
    _publish(api, eid, admin_only["id"])
    recorded = _sim(api, eid, member, "call_tool", payout, record=True,
                    context={**on_ag1, "session_id": "s-1"}).json()
    assert recorded["outcome"] == "DENY" and recorded["decision_id"]
    rows = api.get(f"/api/policy-engines/{eid}/decisions").json()["decisions"]
    [row] = [r for r in rows if r["id"] == recorded["decision_id"]]
    assert (row["source"], row["channel"], row["principal_kind"]) == ("simulate", "chat", "user")
    assert (row["resource_kind"], row["resource_id"]) == ("tool", payout["id"])
    assert row["session_id"] == "s-1" and admin_only["id"] in row["policy_ids"]
    assert row["engine_id"] == eid and row["reason"].startswith("simulate:active")
    assert api.get(f"/api/policy-engines/{eid}/decisions",
                   params={"since": "not-a-date"}).status_code == 422
    since = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
    assert api.get(f"/api/policy-engines/{eid}/decisions",
                   params={"since": since, "decision": "deny"}).json()["decisions"]


def test_simulate_on_an_empty_set_denies(api, eid, s3):
    _seed()
    member = {"kind": "user", "id": "river", "role": "member"}
    employee = {"kind": "tool", "id": "hr-database___get_employee"}
    on_ag1 = {"agent_id": "ag1"}
    out = _sim(api, eid, member, "call_tool", employee, context=on_ag1).json()
    assert out["outcome"] == "DENY" and out["reason"] is None


def test_simulate_surfaces_engine_failure_as_no_decision(api, eid, s3):
    _seed()
    policy_backend.set_backend(FakeEngine("no_decision"))
    out = _sim(api, eid, {"kind": "user", "id": "river", "role": "member"}, "call_tool",
               {"kind": "tool", "id": "hr-database___get_employee"},
               context={"agent_id": "ag1"}).json()
    assert out["outcome"] == "NO_DECISION" and out["errors"] == ["boom"]


# ── entities ──────────────────────────────────────────────────────────────


def test_entity_catalog(api, eid, s3):
    spec = {"name": "hr-agent", "method": "zip_runtime", "tools": [
        {"type": "gateway", "name": "hr-database",
         "config": {"tools": ["hr-database___get_employee"]}},
        {"type": "gateway", "name": "crm", "config": {}},
    ]}
    _seed("ag1", "hr-agent", spec=spec, key="key_1")
    _seed("ag2", "legacy", spec={"tools": [{"type": "gateway", "name": "legacy-db"}, "junk"]})
    body = api.get(f"/api/policy-engines/{eid}/entities").json()
    assert body["api_keys"] == [{"id": "key_1", "name": "ci-key", "label": None,
                                 "created_by": "river", "enabled": True}]
    assert [a["id"] for a in body["agents"]] == ["ag1", "ag2"]
    assert body["agents"][0] == {"id": "ag1", "name": "hr-agent", "method": "zip_runtime",
                                 "status": "active", "system": False, "owner": "river"}
    assert body["tools"] == [
        {"id": "crm___*", "name": "crm___*", "target": "crm", "tool": "*", "pattern": True,
         "agent_ids": ["ag1"], "source": "declared"},
        {"id": "hr-database___get_employee", "name": "hr-database___get_employee",
         "target": "hr-database", "tool": "get_employee", "pattern": False,
         "agent_ids": ["ag1"], "source": "declared"},
        {"id": "legacy-db___*", "name": "legacy-db___*", "target": "legacy-db", "tool": "*",
         "pattern": True, "agent_ids": ["ag2"], "source": "declared"},
    ]
    assert body["users"] == []  # the built-in admin has no users row
    assert body["roles"] == ["admin", "member"] and body["system"] == ["hub"]
    assert {t["name"] for t in body["templates"]} == {
        "permit_workspace_agents_call_tools", "permit_users_call_tools",
        "permit_agent_declared_tools", "forbid_tool_for_members"}
    # every shipped template parses and validates as-is
    for t in body["templates"]:
        assert api.post(f"/api/policy-engines/{eid}/validate",
                        json={"statement": t["statement"]}).json()["ok"] is True, t["name"]


# ── gated console: the handler-level role checks ──────────────────────────

ADMIN_CREDS = {"username": "operator", "password": "s3cret-pass"}
MEMBER_CREDS = {"username": "pol-user", "email": "pol-user@acme-corp.com",
                "password": "sufficient-pass"}


@pytest.fixture
def sessions(monkeypatch, s3):
    monkeypatch.setenv("LAUNCHPAD_AUTH_USERNAME", ADMIN_CREDS["username"])
    monkeypatch.setenv("LAUNCHPAD_AUTH_PASSWORD", ADMIN_CREDS["password"])
    config.get_settings.cache_clear()
    app = create_app()
    with (TestClient(app, client=("127.0.0.1", 4321)) as admin,
          TestClient(app, client=("127.0.0.1", 4321)) as member):
        assert admin.post("/api/auth/login", json=ADMIN_CREDS).status_code == 200
        assert member.post("/api/auth/register", json=MEMBER_CREDS).status_code == 201
        db = SessionLocal()
        try:
            user = users_service.find_by_username(db, MEMBER_CREDS["username"])
            user.status = users_service.STATUS_ACTIVE
            user.expires_at = datetime.now(UTC) + timedelta(days=7)
            users_service.set_workspace_grants(db, user, [WS])
            db.commit()
            user_id = user.id
        finally:
            db.close()
        login = member.post("/api/auth/login", json={"username": MEMBER_CREDS["username"],
                                                     "password": MEMBER_CREDS["password"]})
        assert login.status_code == 200, login.text
        yield admin, member, user_id
    config.get_settings.cache_clear()


def test_attach_is_admin_only_and_record_needs_the_permission(sessions):
    admin, member, user_id = sessions
    _seed()
    engine = member.post("/api/policy-engines", json={"name": "member_engine"})
    assert engine.status_code == 201  # perm:policy.manage is default-granted
    eid = engine.json()["id"]
    created = member.post(f"/api/policy-engines/{eid}/policies",
                          json={"name": "b", "statement": BASELINE})
    assert created.status_code == 201
    pid = created.json()["id"]
    assert member.post(f"/api/policy-engines/{eid}/policies/{pid}/publish",
                       json={}).status_code == 200
    # a member may change a policy's enforcement mode (perm:policy.manage) …
    assert member.put(f"/api/policy-engines/{eid}/policies/{pid}/enforcement-mode",
                      json={"enforcement_mode": "active"}).status_code == 200
    # … but attaching the engine to a gateway is the gateway's governance posture: ADMIN
    body = {"engine_id": eid, "mode": "ENFORCE"}
    forbidden = member.put("/api/governance/gateways/launchpad-gw-x/policy-engine", json=body)
    assert forbidden.status_code == 403
    assert member.get("/api/governance/gateways/launchpad-gw-x/policy-engine").status_code == 200
    attached = admin.put("/api/governance/gateways/launchpad-gw-x/policy-engine", json=body)
    assert attached.status_code == 200 and attached.json()["mode"] == "ENFORCE"
    assert member.delete("/api/governance/gateways/launchpad-gw-x/policy-engine").status_code == 403

    sim = {"principal": {"kind": "user", "id": MEMBER_CREDS["username"]}, "action": "call_tool",
           "resource": {"kind": "tool", "id": "hr-database___get_employee"},
           "context": {"agent_id": "ag1"}}
    dry = member.post(f"/api/policy-engines/{eid}/simulate", json=sim)
    assert dry.status_code == 200 and dry.json()["outcome"] == "ALLOW"
    # the member's own ledger row supplies the role → Role::"member" parent
    assert dry.json()["request"]["principal"]["id"] == MEMBER_CREDS["username"]
    assert member.post(f"/api/policy-engines/{eid}/simulate",
                       json={**sim, "record": True}).status_code == 200

    revoked = admin.patch(f"/api/users/{user_id}", json={"permissions": {"policy.manage": False}})
    assert revoked.status_code == 200
    denied = member.post(f"/api/policy-engines/{eid}/simulate", json={**sim, "record": True})
    assert denied.status_code == 403 and denied.json()["code"] == "auth.permission_required"
    assert denied.json()["detail"] == {"permission": "policy.manage"}
    assert member.post(f"/api/policy-engines/{eid}/simulate", json=sim).status_code == 200
    blocked = member.post(f"/api/policy-engines/{eid}/policies",
                          json={"name": "c", "statement": BASELINE})
    assert blocked.status_code == 403
    assert member.post("/api/policy-engines", json={"name": "another"}).status_code == 403
    assert member.get("/api/policy-engines").status_code == 200
    # the Users console lists the key
    assert "policy.manage" in admin.get("/api/auth/status").json()["permissions"]


def test_routes_exist_only_with_the_policy_feature(monkeypatch):
    config.get_settings.cache_clear()
    try:
        monkeypatch.setenv("LAUNCHPAD_FEATURES", json.dumps(["agents", "chat", "policy"]))
        config.get_settings.cache_clear()
        on = TestClient(create_app())
        assert on.get("/api/policy-engines").status_code == 200
        assert "policy" in on.get("/api/capabilities").json()["features"]
        monkeypatch.setenv("LAUNCHPAD_FEATURES", json.dumps(["agents", "chat"]))
        config.get_settings.cache_clear()
        policy_backend.set_backend(None)
        off = TestClient(create_app())
        assert off.get("/api/policy-engines").status_code == 404
        assert "policy" not in off.get("/api/capabilities").json()["features"]
        assert policy_backend.backend_or_none() is None  # not registered without the feature
    finally:
        config.get_settings.cache_clear()
