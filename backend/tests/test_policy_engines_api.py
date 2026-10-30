"""``/api/policy-engines`` + ``/api/governance/gateways/{gid}/policy-engine`` over HTTP — the Global
AgentCore Policy model (contract ``docs/cn/interfaces/policy-engines.md``): engine CRUD, policy
ownership (a policy is addressed under its engine), attach / detach, two gateways on one engine
with different modes producing two bundles, the startup migration's idempotence, the 410 of the
retired ``/api/policies`` surface and the capabilities block. Hermetic: FakeS3 through the
client funnel, the interceptor attach hook replaced by a recorder, the Dogwood install inert
(platform switch off) unless a test turns it on with the fake compiler + shards."""

from __future__ import annotations

import json

import pytest

from app.core import config
from app.core.config import get_settings
from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.models.ledger import Agent, Workspace
from app.models.policy import (
    Policy,
    PolicyChange,
    PolicyDecision,
    PolicyEngine,
    PolicyEngineAttachment,
    PolicyGatewayAttachment,
    PolicySettings,
    PolicyVersion,
)
from app.policy.interceptor import bundle as interceptor_bundle
from app.services import (
    aws_clients,
    policy_backend,
    policy_engines,
    policy_gateway_state,
    policy_store,
)
from app.services.policy_cedar import CedarEngine
from tests.policy_fakes import BASELINE, BROKEN, FakeS3
from tests.policy_principal.test_dogwood_compiler import needs_engine

WS = DEFAULT_WORKSPACE_ID
GW = "launchpad-gw-exampleid0"
GW2 = "launchpad-gw-second0001"
BUCKET = "launchpad-artifacts-test"
RES = {"artifacts_bucket": BUCKET, "gateway_id": GW, "gateway_name": "launchpad-gw",
       "gateway_arn": f"arn:aws-cn:bedrock-agentcore:cn-northwest-1:111122223333:gateway/{GW}",
       "gateway_url": "https://gw.example.test/mcp",
       "policy_interceptor_function_arn":
           "arn:aws-cn:lambda:cn-northwest-1:111122223333:function:launchpad-policy-interceptor"}


@pytest.fixture
def s3(monkeypatch):
    fake = FakeS3()
    monkeypatch.setattr(aws_clients, "client", lambda service, ctx, **kw: fake)
    monkeypatch.setattr(aws_clients, "get_session", lambda *a, **k: pytest.fail("session"))
    return fake


@pytest.fixture
def hook(monkeypatch):
    """The interceptor attach / detach seam: records ``(gateway_id, attach)`` and writes the
    verified state exactly as the real hook does."""
    calls: list[tuple[str, bool]] = []

    def fake(db, facts, attach):
        from app.services import policy_gateway_state

        calls.append((facts["gateway_id"], attach))
        if attach:
            policy_gateway_state.record_attach(db, facts["gateway_id"], "arn:fn")
        else:
            policy_gateway_state.record_detach(db, facts["gateway_id"])
        return {"interceptor": "updated"}

    monkeypatch.setattr(policy_engines, "interceptor_hook", fake)
    return calls


@pytest.fixture
def api(client, s3, hook):
    assert isinstance(policy_backend.backend_or_none(), CedarEngine)
    db = SessionLocal()
    try:
        db.get(Workspace, WS).resources = dict(RES)
        db.commit()
    finally:
        db.close()
    return client


def _engine(api, name="hr_engine", **body) -> dict:
    res = api.post("/api/policy-engines", json={"name": name, **body})
    assert res.status_code == 201, res.text
    return res.json()


def _policy(api, eid, name="baseline", statement=BASELINE, **extra) -> dict:
    res = api.post(f"/api/policy-engines/{eid}/policies",
                   json={"name": name, "statement": statement, **extra})
    assert res.status_code == 201, res.text
    return res.json()


def _publish(api, eid, pid, **body) -> dict:
    res = api.post(f"/api/policy-engines/{eid}/policies/{pid}/publish", json=body)
    assert res.status_code == 200, res.text
    return res.json()


@needs_engine
def test_legacy_editor_preflight_uses_install_rewrite_and_gateway_schema(api, monkeypatch):
    from app.policy import dogwood_compiler as compiler
    from app.services import policy_engine_sync as sync
    from tests.policy_principal.test_d20_boolean_rewrite import ORIGINAL, TOOLS

    eid = _engine(api)["id"]
    pid = _policy(api, eid, statement=ORIGINAL)["id"]
    _publish(api, eid, pid)
    monkeypatch.setattr(get_settings(), "policy_gateway_interceptor", True)
    monkeypatch.setattr(sync, "engine_tools_list", lambda *_: TOOLS)
    calls = []
    real_compile = compiler.compile

    def capture(tools, policies):
        calls.append((tools, policies))
        return real_compile(tools, policies)

    monkeypatch.setattr(compiler, "compile", capture)
    route = f"/api/policy-engines/{eid}/validate"
    response = api.post(route, json={"statement": ORIGINAL, "policy_id": pid})
    assert response.status_code == 200, response.text
    assert response.json()["ok"] and response.json()["scope"] == "gateway_active_set"
    assert calls[-1][0] == TOOLS
    assert len(calls[-1][1]) == 1  # replacement, never old active + duplicate candidate
    assert calls[-1][1][0]["id"] == pid
    assert calls[-1][1][0]["statement"] == sync.engine_statement(ORIGINAL, "cedar")[0]
    broken = ORIGINAL.replace("office-facts___*", "office-*")
    failure = api.post(route, json={"statement": broken, "policy_id": pid})
    assert failure.status_code == 200, failure.text
    assert not failure.json()["ok"]
    assert any("name" in error["msg"] for error in failure.json()["errors"])
    with SessionLocal() as db:
        row = db.get(Policy, pid)
        assert row.active_version == 1 and row.draft_version is None


def test_failed_attachment_returns_error_and_identical_retry_finishes(api, monkeypatch):
    eid = _engine(api)["id"]
    attempts = []

    def attach(db, facts, enabled):
        attempts.append(enabled)
        if len(attempts) == 1:
            raise RuntimeError("temporary gateway failure")
        from app.services import policy_gateway_state

        policy_gateway_state.record_attach(db, GW, RES["policy_interceptor_function_arn"])
        return {"interceptor": "updated"}

    monkeypatch.setattr(policy_engines, "interceptor_hook", attach)
    url = f"/api/governance/gateways/{GW}/policy-engine"
    body = {"engine_id": eid, "mode": "ENFORCE"}
    assert api.put(url, json=body).status_code == 409
    retried = api.put(url, json=body)
    assert retried.status_code == 200, retried.text
    assert retried.json()["interceptor"]["attached"] is True
    assert attempts == [True, True]


def test_failed_detachment_is_retryable(api, monkeypatch):
    eid = _engine(api)["id"]
    url = f"/api/governance/gateways/{GW}/policy-engine"
    assert api.put(url, json={"engine_id": eid, "mode": "ENFORCE"}).status_code == 200
    attempts = []

    def detach(db, facts, enabled):
        attempts.append(enabled)
        if len(attempts) == 1:
            raise RuntimeError("temporary gateway failure")
        from app.services import policy_gateway_state

        policy_gateway_state.record_detach(db, GW)
        return {"interceptor": "updated"}

    monkeypatch.setattr(policy_engines, "interceptor_hook", detach)
    assert api.delete(url).status_code == 409
    retried = api.delete(url)
    assert retried.status_code == 200, retried.text
    assert retried.json()["interceptor"]["attached"] is False
    assert attempts == [False, False]


def test_migration_bakes_agent_scope_once_and_keeps_original_version(api, monkeypatch, s3):
    from app.services import policy_engine_sync
    from tests.policy_principal.test_engine_sync import FakeCompiler

    monkeypatch.setattr(policy_engine_sync, "compiler", FakeCompiler)
    monkeypatch.setattr(policy_engine_sync, "engine_tools_list", lambda *args: {"tools": []})
    eid = _engine(api)["id"]
    pid = _policy(api, eid, statement=(
        'forbid(principal, action == Launchpad::Action::"call_tool", '
        'resource == Launchpad::Tool::"hr-database___create_payout");'))["id"]
    _publish(api, eid, pid)
    monkeypatch.setattr(policy_engine_sync, "enabled", lambda: True)
    db = SessionLocal()
    try:
        policy = db.get(Policy, pid)
        policy.engine_id, policy.scope, policy.agent_ids = None, "agents", ["hr-agent"]
        db.commit()
        policy_engines.migrate(db)
        db.refresh(policy)
        assert policy.scope == "workspace" and policy.agent_ids == []
        assert policy.active_version == 2
        versions = db.query(PolicyVersion).filter_by(policy_id=pid).order_by(
            PolicyVersion.version).all()
        assert len(versions) == 2 and "hr-agent" not in versions[0].statement
        assert '["hr-agent"].contains(context.launchpad.agent_id)' in versions[1].statement
        assert versions[1].dialect == "dogwood"
        assert versions[1].validation["ok"] and versions[1].s3_key
        policy_engines.migrate(db)
        assert db.query(PolicyVersion).filter_by(policy_id=pid).count() == 2
        assert db.query(PolicyChange).filter_by(
            policy_id=pid, operation="policy.scope_baked").count() == 1
    finally:
        db.close()


def test_force_delete_engine_preserves_retired_policies_and_versions(api):
    eid = _engine(api)["id"]
    pid = _policy(api, eid)["id"]
    _publish(api, eid, pid)
    assert api.delete(f"/api/policy-engines/{eid}?force=true").status_code == 200
    db = SessionLocal()
    try:
        assert db.get(Policy, pid).status == "retired"
        assert db.query(PolicyVersion).filter_by(policy_id=pid).count() == 1
    finally:
        db.close()
    history = api.get(f"/api/policy-engines/{eid}/policies/{pid}/versions")
    assert history.status_code == 200 and len(history.json()["versions"]) == 1
    assert api.get("/api/policy-engines").json()["engines"] == []
    replacement = _engine(api)["id"]
    assert _policy(api, replacement)["id"] != pid
    assert api.post(f"/api/policy-engines/{eid}/policies/{pid}/publish", json={}).status_code == 404


def test_foreign_interceptor_is_visible_and_cannot_be_replaced_or_detached(api, monkeypatch, s3):
    eid = _engine(api)["id"]
    foreign = "arn:aws-cn:lambda:cn-northwest-1:111122223333:function:other-environment"
    monkeypatch.setattr(get_settings(), "policy_gateway_interceptor", True)
    monkeypatch.setattr(policy_engines, "gateway_snapshot", lambda facts: {
        "gatewayArn": RES["gateway_arn"], "name": "shared",
        "interceptorConfigurations": [{"interceptor": {"lambda": {"arn": foreign}}}]})
    url = f"/api/governance/gateways/{GW}/policy-engine"
    view = api.get(url)
    assert view.status_code == 200 and view.json()["foreign"] is True
    assert view.json()["foreign_lambda"] == foreign
    before = len(s3.calls)
    for result in (api.put(url, json={"engine_id": eid, "mode": "ENFORCE"}), api.delete(url)):
        assert result.status_code == 409
        assert result.json()["code"] == "gateway.policy_engine_foreign"
    assert len(s3.calls) == before
    with SessionLocal() as db:
        assert policy_engines.attachment_of(db, GW) is None


def test_cross_region_gateway_is_refused_before_attachment(api, monkeypatch):
    eid = _engine(api)["id"]
    monkeypatch.setattr(get_settings(), "policy_gateway_interceptor", True)
    monkeypatch.setattr(policy_engines, "gateway_snapshot", lambda facts: {
        "gatewayArn": RES["gateway_arn"].replace("cn-northwest-1", "cn-north-1")})
    response = api.put(f"/api/governance/gateways/{GW}/policy-engine",
                       json={"engine_id": eid, "mode": "ENFORCE"})
    assert response.status_code == 422 and response.json()["code"] == "gateway.policy_engine_region"


def test_retry_repairs_a_missing_verified_attachment_from_live_aws(api, monkeypatch):
    eid = _engine(api)["id"]
    url = f"/api/governance/gateways/{GW}/policy-engine"
    assert api.put(url, json={"engine_id": eid, "mode": "ENFORCE"}).status_code == 200
    with SessionLocal() as db:
        policy_gateway_state.record_detach(db, GW)
    monkeypatch.setattr(get_settings(), "policy_gateway_interceptor", True)
    monkeypatch.setattr(policy_engines, "gateway_snapshot", lambda facts: {
        "gatewayArn": RES["gateway_arn"], "interceptorConfigurations": [
            {"interceptor": {"lambda": {"arn": RES["policy_interceptor_function_arn"]}}}]})
    response = api.put(url, json={"engine_id": eid, "mode": "ENFORCE"})
    assert response.status_code == 200 and response.json()["interceptor"]["attached"]
    with SessionLocal() as db:
        assert policy_gateway_state.gateway_interceptor_attached(db, GW)


# ── engines ───────────────────────────────────────────────────────────────────


def test_engine_crud_over_http(api):
    assert api.get("/api/policy-engines").json() == {"engines": []}
    created = _engine(api, description="HR gateway rules")
    assert created["id"].startswith("pe_") and created["status"] == "active"
    assert created["policy_count"] == 0 and created["attachments"] == []
    assert created["policy_set_version"] is None and created["engine_decides"] is False
    eid = created["id"]

    dup = api.post("/api/policy-engines", json={"name": "hr_engine"})
    assert dup.status_code == 409 and dup.json()["code"] == "policy_engine.name_taken"
    bad = api.post("/api/policy-engines", json={"name": "1-bad"})
    assert bad.status_code == 422  # pydantic: Global's name pattern
    assert api.get("/api/policy-engines/pe_missing000").status_code == 404

    renamed = api.put(f"/api/policy-engines/{eid}", json={"description": "payroll"}).json()
    assert renamed["description"] == "payroll"
    listed = api.get("/api/policy-engines").json()["engines"]
    assert [e["id"] for e in listed] == [eid]

    pid = _policy(api, eid)["id"]
    busy = api.delete(f"/api/policy-engines/{eid}")
    assert busy.status_code == 409 and busy.json()["code"] == "policy_engine.has_policies"
    assert busy.json()["detail"] == {"policy_ids": [pid]}
    gone = api.delete(f"/api/policy-engines/{eid}", params={"force": "true"}).json()
    assert gone == {"deleted": True, "id": eid}
    assert api.get(f"/api/policy-engines/{eid}").status_code == 404
    db = SessionLocal()
    try:
        assert db.get(Policy, pid).status == "retired", "force preserves retired policy history"
        ops = [c.operation for c in db.query(PolicyChange).filter_by(engine_id=eid)
               .order_by(PolicyChange.created_at).all()]
    finally:
        db.close()
    assert ops == ["policy_engine.create", "policy_engine.update", "policy.create", "policy.retire",
                   "policy_engine.delete"]


def test_a_policy_belongs_to_one_engine(api, s3):
    one, two = _engine(api, "one")["id"], _engine(api, "two")["id"]
    created = _policy(api, one, description="who may call tools")
    pid = created["id"]
    assert created["engine_id"] == one and created["status"] == "draft"
    assert created["enforcement_mode"] == "log_only"  # Global: a new policy starts LOG_ONLY
    assert set(created) >= {"id", "engine_id", "name", "description", "status",
                            "enforcement_mode", "kind", "active_version",
                            "draft_version", "statement", "validation", "policy_ids",
                            "published_at", "s3_key", "created_by", "created_at", "updated_at"}
    assert "scope" not in created and "agent_ids" not in created  # the agent scope is gone

    # addressed under ITS engine only
    assert api.get(f"/api/policy-engines/{two}/policies/{pid}").status_code == 404
    assert api.put(f"/api/policy-engines/{two}/policies/{pid}",
                   json={"description": "x"}).status_code == 404
    assert [p["id"] for p in api.get(f"/api/policy-engines/{one}/policies").json()["policies"]] \
        == [pid]
    assert api.get(f"/api/policy-engines/{two}/policies").json()["policies"] == []
    # Names are unique within an engine; independent engines may use the same name.
    dup = api.post(f"/api/policy-engines/{two}/policies",
                   json={"name": "baseline", "statement": BASELINE})
    assert dup.status_code == 201
    dup = api.post(f"/api/policy-engines/{one}/policies",
                   json={"name": "baseline", "statement": BASELINE})
    assert dup.status_code == 409 and dup.json()["code"] == "policy.name_taken"
    bad = api.post(f"/api/policy-engines/{one}/policies", json={"name": "bad", "statement": BROKEN})
    assert bad.status_code == 422 and bad.json()["code"] == "policy.invalid_statement"

    published = _publish(api, one, pid, reason="go")
    assert published["status"] == "active" and published["active_version"] == 1
    assert published["s3_key"] == f"policy/policies/{pid}/v1.cedar"
    assert "apply" not in published and s3.calls[0]["IfNoneMatch"] == "*"
    assert api.get(f"/api/policy-engines/{one}").json()["active_policies"] == 1
    promoted = api.put(f"/api/policy-engines/{one}/policies/{pid}/enforcement-mode",
                       json={"enforcement_mode": "active"}).json()
    assert promoted["enforcement_mode"] == "active"
    nothing = api.post(f"/api/policy-engines/{one}/policies/{pid}/publish", json={})
    assert nothing.status_code == 409 and nothing.json()["code"] == "policy.no_draft"
    versions = api.get(f"/api/policy-engines/{one}/policies/{pid}/versions").json()["versions"]
    assert [v["version"] for v in versions] == [1] and versions[0]["published_at"]

    retired = api.post(f"/api/policy-engines/{one}/policies/{pid}/retire", json={}).json()
    assert retired["status"] == "retired"
    assert api.delete(f"/api/policy-engines/{one}/policies/{pid}").json() == {
        "deleted": True, "id": pid}
    ops = [c["operation"] for c in api.get(f"/api/policy-engines/{one}/changes").json()["changes"]]
    assert ops == ["policy.delete", "policy.retire", "policy.enforcement_mode", "policy.publish",
                   "policy.create", "policy_engine.create"]


def test_validate_format_and_simulate_under_the_engine(api, s3):
    eid = _engine(api)["id"]
    ok = api.post(f"/api/policy-engines/{eid}/validate", json={"statement": BASELINE}).json()
    assert ok["ok"] is True and ok["policy_ids"] == ["policy0", "policy1", "policy2", "policy3"]
    fmt = api.post(f"/api/policy-engines/{eid}/format",
                   json={"statement": "permit(principal,action,resource);"})
    assert fmt.json()["statement"].strip() == "permit (principal, action, resource);"
    db = SessionLocal()
    try:
        db.add(Agent(id="ag1", workspace_id=WS, name="hr-agent", method="zip_runtime",
                     status="active", spec={}, created_by_name="river"))
        db.commit()
    finally:
        db.close()
    member = {"kind": "user", "id": "river", "role": "member"}
    employee = {"kind": "tool", "id": "hr-database___get_employee"}
    body = {"principal": member, "action": "call_tool", "resource": employee,
            "context": {"agent_id": "ag1"}}
    before = api.post(f"/api/policy-engines/{eid}/simulate", json=body).json()
    # an empty set is Cedar's default-deny; the mode is the (unattached) gateway's = log_only
    assert before["outcome"] == "DENY" and before["mode"] == "log_only"
    assert before["gateway_id"] == GW
    pid = _policy(api, eid)["id"]
    _publish(api, eid, pid)
    after = api.post(f"/api/policy-engines/{eid}/simulate", json={**body, "record": True}).json()
    assert after["outcome"] == "ALLOW" and after["determining_policy_ids"] == [f"{pid}:v1"]
    rows = api.get(f"/api/policy-engines/{eid}/decisions").json()["decisions"]
    assert len(rows) == 1 and rows[0]["engine_id"] == eid and rows[0]["gateway_id"] == GW
    assert rows[0]["source"] == "simulate" and rows[0]["id"] == after["decision_id"]
    # the decisions of another engine are not this engine's
    other = _engine(api, "other")["id"]
    assert api.get(f"/api/policy-engines/{other}/decisions").json()["decisions"] == []


# ── gateway attachments ────────────────────────────────────────────────────────


def test_attach_change_mode_and_detach(api, s3, hook):
    eid = _engine(api)["id"]
    _publish(api, eid, _policy(api, eid)["id"])
    unattached = api.get(f"/api/governance/gateways/{GW}/policy-engine").json()
    assert unattached["attached"] is False and unattached["engine_id"] is None
    assert unattached["interceptor"]["attached"] is False

    bad_mode = api.put(f"/api/governance/gateways/{GW}/policy-engine",
        json={"engine_id": eid, "mode": "on"})
    assert bad_mode.status_code == 422
    missing = api.put(f"/api/governance/gateways/{GW}/policy-engine",
                      json={"engine_id": "pe_missing000", "mode": "ENFORCE"})
    assert missing.status_code == 404

    attached = api.put(f"/api/governance/gateways/{GW}/policy-engine",
                       json={"engine_id": eid, "mode": "LOG_ONLY"}).json()
    assert attached["attached"] is True and attached["engine_id"] == eid
    assert attached["mode"] == "LOG_ONLY" and attached["interceptor"]["attached"] is True
    assert hook == [(GW, True)], "the FIRST attach attaches the interceptor to that gateway"
    engine = api.get(f"/api/policy-engines/{eid}").json()
    assert engine["attachments"][0]["gateway_id"] == GW and engine["attachments"][0]["mode"] == \
        "LOG_ONLY"

    # idempotent; a mode change rewrites nothing on the AWS side but the bundle
    same = api.put(f"/api/governance/gateways/{GW}/policy-engine",
        json={"engine_id": eid, "mode": "LOG_ONLY"})
    assert same.status_code == 200 and hook == [(GW, True)]
    enforce = api.put(f"/api/governance/gateways/{GW}/policy-engine",
                      json={"engine_id": eid, "mode": "ENFORCE"}).json()
    assert enforce["mode"] == "ENFORCE" and hook == [(GW, True)]

    # the engine cannot go while attached
    busy = api.delete(f"/api/policy-engines/{eid}", params={"force": "true"})
    assert busy.status_code == 409 and busy.json()["code"] == "policy_engine.attached"
    assert busy.json()["detail"] == {"gateway_ids": [GW]}

    detached = api.delete(f"/api/governance/gateways/{GW}/policy-engine").json()
    assert detached["detached"] is True and detached["attached"] is False
    assert hook == [(GW, True), (GW, False)]
    assert api.delete(f"/api/governance/gateways/{GW}/policy-engine").json()["detached"] is False
    db = SessionLocal()
    try:
        ops = [(c.operation, c.after) for c in db.query(PolicyChange)
               .filter(PolicyChange.engine_id == eid,
                       PolicyChange.operation.in_(("gateway.attach", "gateway.detach")))
               .order_by(PolicyChange.created_at).all()]
    finally:
        db.close()
    assert [op for op, _ in ops] == ["gateway.attach", "gateway.attach", "gateway.detach"]
    assert ops[0][1]["first_attach"] is True and ops[1][1]["mode"] == "ENFORCE"
    assert policy_engines.capability_summary(SessionLocal(), WS) == {"engines": 1,
                                                                      "attached_gateways": 0}


def test_an_engine_of_another_workspace_is_not_attachable_here(api, hook):
    db = SessionLocal()
    try:
        db.add(Workspace(id="ws-two", name="two", account_id="444455556666",
                         region="cn-northwest-1", resources={}))
        foreign = PolicyEngine(workspace_id="ws-two", name="theirs")
        db.add(foreign)
        db.commit()
        fid = foreign.id
    finally:
        db.close()
    res = api.put(f"/api/governance/gateways/{GW}/policy-engine",
        json={"engine_id": fid, "mode": "ENFORCE"})
    assert res.status_code == 404 and res.json()["code"] == "policy_engine.not_found"
    assert hook == []


def test_two_gateways_on_one_engine_write_two_bundles_with_their_own_mode(
        api, s3, hook, monkeypatch):
    """One engine, two gateways, different modes (Global: policyEngineConfiguration is per
    gateway) — two bundles, same set, each with ITS attachment block; a detach rewrites that
    gateway's bundle with ``attachment: null``."""
    from app.services import principal_mint, principal_token
    from tests.policy_principal.fakes import EphemeralSigner
    from tests.policy_principal.test_gateway_publish import CasS3
    from tests.policy_principal.test_mint_entrances import StubKms  # the KMS signer stub

    cas = CasS3()  # the pointer swap is a conditional write: the plain FakeS3 has no ETags
    monkeypatch.setattr(aws_clients, "client", lambda service, ctx, **kw: cas)

    monkeypatch.setattr(get_settings(), "policy_engine_endpoints",
                        ["10.44.0.10:7443", "10.44.0.10:7444"])
    monkeypatch.setattr(get_settings(), "policy_hub_role_arn",
                        "arn:aws-cn:iam::111122223333:role/launchpad-cn-ops")
    stub = StubKms(EphemeralSigner())
    principal_mint.reset_signer()
    monkeypatch.setattr(principal_token, "hub_signer",
                        lambda: principal_token.KmsSigner(stub, "alias/launchpad-principal"))
    eid = _engine(api)["id"]
    _publish(api, eid, _policy(api, eid)["id"])
    api.put(f"/api/governance/gateways/{GW}/policy-engine",
        json={"engine_id": eid, "mode": "ENFORCE"})
    api.put(f"/api/governance/gateways/{GW2}/policy-engine",
        json={"engine_id": eid, "mode": "LOG_ONLY"})
    assert hook == [(GW, True), (GW2, True)]

    def bundle(gateway_id):
        pointer = json.loads(cas.objects[interceptor_bundle.pointer_key(gateway_id)])
        raw = json.loads(cas.objects[pointer["key"]])
        return interceptor_bundle.parse_bundle(raw, expect_gateway_id=gateway_id), raw

    first, raw1 = bundle(GW)
    second, raw2 = bundle(GW2)
    assert raw1["schema"] == raw2["schema"] == "launchpad.policy.gateway/3"
    assert "workspaces" not in raw1
    assert first.attachment.engine_id == second.attachment.engine_id == eid
    assert (first.attachment.mode, second.attachment.mode) == ("enforce", "log_only")
    # the platform switch is off in this test: the Dogwood set is not installed, so both
    # bundles carry an empty set, so the Lambda fails closed without a /3 published flag.
    assert first.attachment.active_count == second.attachment.active_count == 0
    assert "published" not in raw1["attachment"] and "published" not in raw2["attachment"]
    assert first.attachment.policy_set_version == second.attachment.policy_set_version
    assert raw2["gateway_arn"].endswith(f"gateway/{GW2}")  # derived: no owning workspace
    engine = api.get(f"/api/policy-engines/{eid}").json()
    assert sorted((a["gateway_id"], a["mode"]) for a in engine["attachments"]) == [
        (GW, "ENFORCE"), (GW2, "LOG_ONLY")]
    assert api.get("/api/capabilities").json()["policy"] == {"engines": 1,
                                                             "attached_gateways": 2}

    api.delete(f"/api/governance/gateways/{GW2}/policy-engine")
    detached, raw = bundle(GW2)
    assert detached.attachment is None and raw["attachment"] is None
    still, _ = bundle(GW)
    assert still.attachment is not None and still.attachment.mode == "enforce"
    principal_mint.reset_signer()


# ── migration ─────────────────────────────────────────────────────────────────


def _old_ledger(mode="enforce", *, live=True, policies=2) -> list[str]:
    """A pre-Global ledger: policies without an engine, the retired settings row with the
    workspace mode + both switches, the interceptor verified attached, gateway rows."""
    db = SessionLocal()
    try:
        db.get(Workspace, WS).resources = dict(RES)
        ids = []
        for i in range(policies):
            policy = policy_store.create_policy(db, WS, name=f"old_{i}", statement=BASELINE,
                                                operator="river")
            policy.engine_id = None  # as a pre-engine writer left it
            ids.append(policy.id)
        db.query(PolicyEngine).delete()
        db.add(PolicySettings(workspace_id=WS, mode=mode, gateway_interceptor=live,
                              engine_publish=live, policy_set_seq=3, temporal_set_version=1,
                              engine_state={"policy_set_version": "ps-3", "active_count": 2,
                                            "governed_actions": [], "bundle_pending": False}))
        if live:
            db.add(PolicyGatewayAttachment(gateway_id=GW, attached=True, interceptor_arn="arn"))
        db.add(PolicyDecision(workspace_id=WS, source="gateway", channel="gateway",
                              principal_kind="user", principal_id="u", action="call_tool",
                              resource_kind="tool", resource_id="t", decision="deny",
                              mode="enforce"))
        db.query(PolicyChange).delete()
        db.commit()
        return ids
    finally:
        db.close()


def test_migration_makes_one_default_engine_and_attaches_the_live_gateway(client, s3):
    ids = _old_ledger("enforce")
    db = SessionLocal()
    try:
        assert policy_engines.migrate(db) == [WS]
        [engine] = db.query(PolicyEngine).all()
        assert engine.name == "default_policy_engine" and engine.workspace_id == WS
        assert {p.engine_id for p in db.query(Policy).all()} == {engine.id}
        attachment = db.get(PolicyEngineAttachment, GW)
        assert attachment is not None and attachment.engine_id == engine.id
        assert attachment.mode == "ENFORCE" and attachment.attached_by == "migration"
        # the engine-set facts carry over as a MIGRATED state: the reconciler re-installs under
        # the engine key before anything trusts it
        assert engine.policy_set_seq == 3 and engine.temporal_set_version == 1
        assert engine.engine_state["migrated_from_workspace"] == WS
        assert engine.engine_state["bundle_pending"] is True
        from app.services import policy_engine_sync

        assert policy_engine_sync.engine_state_is_legacy(engine.engine_state)
        row = db.query(PolicyDecision).filter_by(source="gateway").one()
        assert (row.gateway_id, row.engine_id) == (GW, engine.id)
        journal = db.query(PolicyChange).filter_by(operation="policy_engine.migrated").all()
        assert len(journal) == 1 and journal[0].after["attachment_mode"] == "ENFORCE"
        assert journal[0].before["policies"] == len(ids)
        # idempotent: a second pass changes nothing and journals nothing more
        assert policy_engines.migrate(db) == []
        assert db.query(PolicyEngine).count() == 1
        assert db.query(PolicyChange).filter_by(operation="policy_engine.migrated").count() == 1
    finally:
        db.close()


@pytest.mark.parametrize("mode,live", [("off", True), ("log_only", False)])
def test_migration_leaves_an_off_or_unpublished_workspace_unattached(client, s3, mode, live):
    _old_ledger(mode, live=live)
    db = SessionLocal()
    try:
        assert policy_engines.migrate(db) == [WS]
        assert db.get(PolicyEngineAttachment, GW) is None
        [engine] = db.query(PolicyEngine).all()
        assert {p.engine_id for p in db.query(Policy).all()} == {engine.id}
        journal = db.query(PolicyChange).filter_by(operation="policy_engine.migrated").one()
        assert journal.after["attachment_mode"] is None
    finally:
        db.close()


def test_migration_on_a_fresh_ledger_creates_no_engine(client):
    db = SessionLocal()
    try:
        db.query(PolicyChange).delete()
        db.commit()
        assert policy_engines.migrate(db) == []  # zero-policy workspaces are untouched
        assert db.query(PolicyEngine).count() == 0
        assert policy_engines.migrate(db) == []
    finally:
        db.close()


# ── the retired surface, capabilities, feature gate ─────────────────────────────


@pytest.mark.parametrize("method,path", [
    ("GET", "/api/policies"), ("POST", "/api/policies"), ("GET", "/api/policies/settings"),
    ("PUT", "/api/policies/settings"), ("POST", "/api/policies/apply"),
    ("GET", "/api/policies/pol_x"), ("POST", "/api/policies/pol_x/publish"),
    ("DELETE", "/api/policies/pol_x"), ("GET", "/api/policies/audit"),
])
def test_the_retired_policies_surface_answers_410_moved(api, method, path):
    res = api.request(method, path, json={} if method in ("POST", "PUT") else None)
    assert res.status_code == 410, res.text
    body = res.json()
    assert body["code"] == "policy.moved"
    assert body["detail"]["engines_url"] == "/api/policy-engines"


@pytest.mark.parametrize("method,path,body", [
    ("GET", "/api/policy-engines", None),
    ("POST", "/api/policy-engines", {"name": "x"}),
    ("GET", "/api/policy-engines/pe_x/entities", None),
    ("GET", "/api/policy-engines/pe_x/policies", None),
    ("GET", f"/api/governance/gateways/{GW}/policy-engine", None),
])
def test_routes_answer_503_before_an_engine_is_registered(client, method, path, body):
    policy_backend.set_backend(None)
    res = client.request(method, path, json=body)
    assert res.status_code == 503, res.text
    assert res.json()["code"] == "feature.unavailable"


def test_routes_exist_only_with_the_policy_feature(monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import create_app

    config.get_settings.cache_clear()
    try:
        monkeypatch.setenv("LAUNCHPAD_FEATURES", json.dumps(["agents", "chat", "policy"]))
        config.get_settings.cache_clear()
        on = TestClient(create_app())
        assert on.get("/api/policy-engines").status_code == 200
        assert on.get("/api/policies").status_code == 410
        assert on.get("/api/capabilities").json()["policy"] == {"engines": 0,
                                                                "attached_gateways": 0}
        monkeypatch.setenv("LAUNCHPAD_FEATURES", json.dumps(["agents", "chat"]))
        config.get_settings.cache_clear()
        policy_backend.set_backend(None)
        off = TestClient(create_app())
        assert off.get("/api/policy-engines").status_code == 404
        assert off.get("/api/policies").status_code == 404
        assert "policy" not in off.get("/api/capabilities").json()["features"]
    finally:
        config.get_settings.cache_clear()


def test_entities_carry_the_workspace_gateway_and_its_attachment(api, hook):
    eid = _engine(api)["id"]
    body = api.get(f"/api/policy-engines/{eid}/entities").json()
    assert set(body) == {"engine_id", "users", "api_keys", "agents", "tools", "roles", "system",
                         "gateways", "templates"}
    assert body["engine_id"] == eid
    [gateway] = body["gateways"]
    assert gateway["gateway_id"] == GW and gateway["attachment"]["attached"] is False
    assert {t["name"] for t in body["templates"]} >= {"permit_workspace_agents_call_tools",
                                                     "permit_users_call_tools"}
