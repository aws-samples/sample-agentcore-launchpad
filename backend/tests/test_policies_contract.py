"""Response-shape contract of ``/api/policy-engines`` (Global model, contract
``docs/cn/interfaces/policy-engines.md``) — pinned exact key sets so the backend and the
console's typed client cannot drift. Additive keys are allowed only by editing this file in
the same change as ``docs/cn/interfaces/policy-backend.md``."""

import pytest

from app.models.policy import (
    ATTACHMENT_MODES,
    DECISIONS,
    ENFORCEMENT_MODES,
    ENGINE_STATUSES,
    POLICY_STATUSES,
)
from app.services import aws_clients, policy_engines, policy_store
from tests.policy_fakes import BASELINE, FakeS3

ENGINE_OUT = {
    "id", "name", "description", "status", "workspace_id", "policy_count", "active_policies",
    "attachments", "policy_set_version", "temporal_set_version", "temporal_count",
    "bundle_pending", "engine_decides", "last_install", "created_by", "created_by_name",
    "created_at", "updated_at", "has_user_permit", "warnings",
}
ATTACHMENT_SUMMARY = {"gateway_id", "gateway_name", "engine_id", "engine_name", "mode",
                      "attached_by", "attached_at", "updated_at"}
GATEWAY_ATTACHMENT_OUT = {"gateway_id", "attached", "engine_id", "engine_name", "mode",
                          "attached_by", "attached_at", "interceptor"}
INTERCEPTOR_OUT = {"attached", "interceptor_arn", "attached_at", "bundle_version",
                   "bundle_pending"}
POLICY_OUT = {
    "id", "engine_id", "name", "description", "status", "enforcement_mode", "kind",
    "active_version", "draft_version", "statement", "validation", "policy_ids", "published_at",
    "s3_key", "created_by", "created_at", "updated_at",
}
VERSION_OUT = {
    "id", "policy_id", "version", "statement", "schema_digest", "validation", "authored_by",
    "created_at", "published_at", "published_by", "s3_key", "kind",
}
VALIDATION_REPORT = {"ok", "errors", "warnings", "policy_ids", "scope"}
VALIDATION_ERROR = {"line", "col", "msg", "policy_id"}
SIMULATE_OUT = {
    "outcome", "determining_policy_ids", "policy_ids", "determining", "errors", "mode",
    "policy_versions", "source_set", "latency_ms", "request", "reason", "gateway_id",
}
DECISION_OUT = {
    "id", "request_id", "source", "channel", "gateway_id", "gateway_name", "engine_id",
    "principal_kind",
    "principal_id", "principal_label", "principal", "action", "resource_kind", "resource_id",
    "resource", "agent_id", "session_id", "decision", "mode", "policy_ids", "policy_versions",
    "bundle_version", "reason", "latency_ms", "created_at",
}
CHANGE_OUT = {
    "id", "engine_id", "policy_id", "version", "operation", "operator", "status", "before",
    "requested", "after", "override_reason", "error", "created_at",
}
ENTITIES_OUT = {"engine_id", "users", "api_keys", "agents", "tools", "roles", "system",
                "gateways", "templates"}
CAPABILITY_BLOCK = {"engines", "attached_gateways"}
RUNTIME_CHANNEL_STATUS = {"ready", "missing", "unknown"}


@pytest.fixture
def api(client, monkeypatch):
    fake = FakeS3()
    monkeypatch.setattr(aws_clients, "client", lambda service, ctx, **kw: fake)
    monkeypatch.setattr(policy_engines, "interceptor_hook",
                        lambda db, facts, attach: {"interceptor": "skipped"})
    return client


def _engine(api) -> str:
    res = api.post("/api/policy-engines", json={"name": "contract_engine"})
    assert res.status_code == 201, res.text
    return res.json()["id"]


def test_enums_are_lowercase_stored_states_and_globals_spelling_for_attachments():
    assert set(POLICY_STATUSES) == {"draft", "active", "retired"}
    assert set(ENGINE_STATUSES) == {"active", "deleting", "deleted"}
    assert set(ENFORCEMENT_MODES) == {"active", "log_only"}  # Global ACTIVE | LOG_ONLY
    assert set(ATTACHMENT_MODES) == {"LOG_ONLY", "ENFORCE"}  # Global's spelling, as is
    assert set(DECISIONS) == {"allow", "deny", "would_deny", "error"}


def test_engine_policy_version_and_change_shapes(api):
    eid = _engine(api)
    engine = api.get(f"/api/policy-engines/{eid}").json()
    assert set(engine) == ENGINE_OUT
    assert set(api.get("/api/policy-engines").json()) == {"engines"}
    created = api.post(f"/api/policy-engines/{eid}/policies",
                       json={"name": "b", "statement": BASELINE}).json()
    assert set(created) == POLICY_OUT
    assert set(created["validation"]) == VALIDATION_REPORT
    detail = api.get(f"/api/policy-engines/{eid}/policies/{created['id']}").json()
    assert set(detail) == POLICY_OUT | {"versions"}
    assert set(detail["versions"][0]) == VERSION_OUT
    assert detail["versions"][0]["published_at"] is None  # null ⇒ draft
    published = api.post(f"/api/policy-engines/{eid}/policies/{created['id']}/publish",
                         json={}).json()
    assert set(published) == POLICY_OUT and published["status"] == "active"
    listing = api.get(f"/api/policy-engines/{eid}/policies").json()
    assert set(listing) == {"policies", "engine"} and set(listing["engine"]) == ENGINE_OUT
    bad = api.post(f"/api/policy-engines/{eid}/validate", json={"statement": "permit("}).json()
    assert set(bad) == VALIDATION_REPORT and set(bad["errors"][0]) == VALIDATION_ERROR
    change = api.get(f"/api/policy-engines/{eid}/changes").json()["changes"][0]
    assert set(change) == CHANGE_OUT and change["status"] in {"succeeded", "failed"}


def test_attachment_simulate_decisions_entities_and_capability_shapes(api):
    from app.core.db import SessionLocal
    from app.models.ledger import Agent, ApiKey, Workspace

    db = SessionLocal()
    try:
        db.get(Workspace, "default").resources = {
            "artifacts_bucket": "b", "gateway_id": "gw-contract", "gateway_name": "gw"}
        db.add(ApiKey(id="key_1", workspace_id="default", name="ci", prefix="lp_live_ab12",
                      key_hash="h1", created_by_name="river"))
        db.add(Agent(id="ag1", workspace_id="default", name="hr", method="zip_runtime",
                     status="active", spec={"name": "hr", "method": "zip_runtime", "tools": [
                         {"type": "gateway", "name": "hr-database",
                          "config": {"tools": ["hr-database___get_employee"]}}]}))
        db.commit()
    finally:
        db.close()
    eid = _engine(api)
    created = api.post(f"/api/policy-engines/{eid}/policies",
                       json={"name": "b", "statement": BASELINE}).json()
    api.post(f"/api/policy-engines/{eid}/policies/{created['id']}/publish", json={})
    attachment = api.put("/api/governance/gateways/gw-contract/policy-engine",
                         json={"engine_id": eid, "mode": "ENFORCE"}).json()
    assert set(attachment) == GATEWAY_ATTACHMENT_OUT
    assert set(attachment["interceptor"]) == INTERCEPTOR_OUT
    assert set(api.get("/api/policy-engines").json()["engines"][0]["attachments"][0]) == \
        ATTACHMENT_SUMMARY
    out = api.post(f"/api/policy-engines/{eid}/simulate", json={
        "principal": {"kind": "user", "id": "river", "role": "member"}, "action": "call_tool",
        "resource": {"kind": "tool", "id": "hr-database___get_employee"},
        "context": {"agent_id": "ag1"}, "record": True}).json()
    assert set(out) == SIMULATE_OUT | {"decision_id"}
    assert out["outcome"] in {"ALLOW", "DENY", "NO_DECISION"} and out["mode"] == "enforce"
    assert out["determining_policy_ids"] == [f"{created['id']}:v1"]  # "<policy_id>:v<n>"
    audit = api.get(f"/api/policy-engines/{eid}/decisions").json()
    assert set(audit) == {"decisions", "runtime_channel"}
    assert set(audit["runtime_channel"]) == {"status", "reason", "log_group"}
    assert audit["runtime_channel"]["status"] in RUNTIME_CHANNEL_STATUS
    row = audit["decisions"][0]
    assert set(row) == DECISION_OUT
    assert row["decision"] in DECISIONS and row["source"] == "simulate"
    assert row["gateway_id"] == "gw-contract" and row["engine_id"] == eid
    entities = api.get(f"/api/policy-engines/{eid}/entities").json()
    assert set(entities) == ENTITIES_OUT
    assert set(entities["agents"][0]) == {"id", "name", "method", "status", "system", "owner"}
    assert set(entities["tools"][0]) == {"id", "name", "target", "tool", "pattern", "agent_ids",
                                         "source"}
    assert set(entities["api_keys"][0]) == {"id", "name", "label", "created_by", "enabled"}
    assert set(entities["gateways"][0]) == {"gateway_id", "gateway_arn", "attachment"}
    assert set(entities["templates"][0]) == {"name", "statement"}
    db = SessionLocal()
    try:
        assert set(policy_store.capability_summary(db, "default")) == CAPABILITY_BLOCK
        assert set(policy_store.overview_status(db, "default", engine_registered=True)) == {
            "enabled", "detail", "engines", "attached_gateways"}
    finally:
        db.close()


def test_users_entry_shape(api):
    from app.core.db import SessionLocal
    from app.models.ledger import User, UserWorkspace

    db = SessionLocal()
    try:
        user = User(username="ada", username_key="ada", email="ada@acme-corp.com",
                    password_hash="x", role="member", status="active")
        db.add(user)
        db.flush()
        db.add(UserWorkspace(user_id=user.id, workspace_id="default"))
        db.commit()
        uid = user.id
    finally:
        db.close()
    [entry] = api.get(f"/api/policy-engines/{_engine(api)}/entities").json()["users"]
    assert entry == {"id": uid, "user_id": uid, "username": "ada", "role": "member"}


def test_description_null_is_normalised(api):
    eid = _engine(api)
    res = api.post(f"/api/policy-engines/{eid}/policies",
                   json={"name": "nulldesc", "statement": BASELINE, "description": None})
    assert res.status_code == 201 and res.json()["description"] == ""
    engine = api.post("/api/policy-engines", json={"name": "nulldesc_engine", "description": None})
    assert engine.status_code == 201 and engine.json()["description"] == ""
