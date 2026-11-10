"""Overview endpoint: live tiles + service health from resources and ledger."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import app.routers.overview as overview_mod
from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.models.ledger import Agent, ChatSession
from app.services import registry_backend
from tests.conftest import set_default_resources


def _registry(list_records):
    """A ``RegistryBackend`` stand-in exposing only the read the Overview uses."""
    registry_backend.set_backend(SimpleNamespace(list_records=list_records))


def _seed():
    db = SessionLocal()
    agent = Agent(
        workspace_id=DEFAULT_WORKSPACE_ID,
        name="ov-agent", method="harness", status="active", spec={})
    db.add(agent)
    db.flush()
    db.add(ChatSession(
        workspace_id=DEFAULT_WORKSPACE_ID,
        agent_id=agent.id, session_id="s1", turns=2))
    stale = ChatSession(
        workspace_id=DEFAULT_WORKSPACE_ID,
        agent_id=agent.id, session_id="s2", turns=1)
    stale.last_at = datetime.now(UTC) - timedelta(days=3)
    db.add(stale)
    db.commit()
    db.close()


def test_overview_tiles_and_health(client, monkeypatch):
    _seed()
    records = [
        {"recordId": "r1", "descriptorType": "A2A", "status": "APPROVED"},
        {"recordId": "r2", "descriptorType": "MCP", "status": "DRAFT"},
        {"recordId": "r3", "descriptorType": "AGENT_SKILLS", "status": "APPROVED"},
        {"recordId": "r4", "descriptorType": "A2A", "status": "DEPRECATED"},
    ]
    _registry(lambda _ws, _type=None, _status=None: records)
    monkeypatch.setattr(overview_mod, "_traces_active", lambda _ws: True)
    overview_mod._cache.clear()

    res = client.get("/api/overview")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["registry_assets"] == {"agents": 1, "tools": 1, "skills": 1, "total": 3}
    assert body["active_sessions"] == 1  # 3-day-old session excluded
    assert body["services"]["observability"] is True
    assert set(body["services"]) == {
        "gateway", "memory", "registry", "policy", "evaluation", "observability",
    }
    # usage row (WB-001): nothing evaluated → off, detail null (r7-e: structured, not a string)
    assert body["services"]["evaluation"] is False
    assert body["service_detail"]["evaluation"] is None
    # Global model: the Cedar engine is registered (create_app) but no engine is attached →
    # off, and the detail already reads the posture ("<mode> · <n> active")
    assert body["services"]["policy"] is False
    assert body["service_detail"]["policy"] == "0 engine(s) · 0 gateway(s)"


def test_overview_registry_failure_falls_back_to_cache(client, monkeypatch):
    def boom(_ws, _type=None, _status=None):
        raise RuntimeError("registry down")

    _registry(boom)
    monkeypatch.setattr(overview_mod, "_traces_active", lambda _ws: False)
    overview_mod._cache["default"] = {
        "assets_at": 0.0, "assets": {"agents": 7, "tools": 0, "skills": 0},
        "traces_at": 0.0, "traces": None,
    }
    res = client.get("/api/overview")
    assert res.status_code == 200
    assert res.json()["registry_assets"]["agents"] == 7


def test_overview_counts_nothing_while_no_registry_backend_is_registered(client, monkeypatch):
    _seed()
    registry_backend.set_backend(None)
    monkeypatch.setattr(overview_mod, "_traces_active", lambda _ws: False)
    overview_mod._cache.clear()

    body = client.get("/api/overview").json()

    assert body["registry_assets"] == {"agents": 0, "tools": 0, "skills": 0, "total": 0}
    assert body["active_sessions"] == 1


def test_overview_exposes_registry_unavailable_reason(client, monkeypatch):
    set_default_resources({
        "registry_id": "",
        "registry_unavailable_reason": "blocked by account policy",
    })
    monkeypatch.setattr(overview_mod, "_traces_active", lambda _ws: False)
    overview_mod._cache.clear()

    response = client.get("/api/overview")

    assert response.status_code == 200
    body = response.json()
    assert body["services"]["registry"] is False
    assert body["service_detail"]["registry"] == "blocked by account policy"


def test_overview_policy_row_is_ledger_derived_not_a_resource_key(client, monkeypatch):
    """Global model: `services.policy` is true iff the Cedar engine is registered AND one of the
    workspace's policy engines is attached to a gateway with ≥ 1 ACTIVE policy;
    `service_detail.policy` = "<n> engine(s) · <m> gateway(s)". A stale
    `resources["policy_engine_id"]` (C5: nothing in the CN build writes it) changes nothing."""
    from app.models.policy import PolicyEngineAttachment
    from app.services import aws_clients, policy_backend, policy_engines, policy_store
    from tests.conftest import ws_ctx
    from tests.policy_fakes import BASELINE, FakeS3

    set_default_resources({"policy_engine_id": "pe-0123456789", "artifacts_bucket": "b"})
    monkeypatch.setattr(overview_mod, "_traces_active", lambda _ws: False)
    monkeypatch.setattr(aws_clients, "client", lambda service, ctx, **kw: FakeS3())
    body = client.get("/api/overview").json()
    assert body["services"]["policy"] is False
    assert body["service_detail"]["policy"] == "0 engine(s) · 0 gateway(s)"

    # one engine with one published policy, attached to a gateway (the S3 copy lands on a
    # FakeS3 through the funnel seam)
    db = SessionLocal()
    try:
        engine = policy_engines.create_engine(db, DEFAULT_WORKSPACE_ID, name="hr", operator="t")
        policy = policy_store.create_policy(db, engine, name="baseline", statement=BASELINE,
                                            operator="t")
        policy_store.publish_policy(db, ws_ctx({"artifacts_bucket": "b"}), engine, policy.id,
                                    operator="t")
        body = client.get("/api/overview").json()
        assert body["services"]["policy"] is False, "an unattached engine decides nothing"
        db.add(PolicyEngineAttachment(gateway_id="gw-one", engine_id=engine.id, mode="LOG_ONLY"))
        db.commit()
    finally:
        db.close()
    body = client.get("/api/overview").json()
    assert body["services"]["policy"] is True
    assert body["service_detail"]["policy"] == "1 engine(s) · 1 gateway(s)"

    policy_backend.set_backend(None)
    body = client.get("/api/overview").json()
    assert body["services"]["policy"] is False
    assert body["service_detail"]["policy"] == "1 engine(s) · 1 gateway(s)"


def test_overview_policy_row_is_off_without_an_attachment(client, monkeypatch):
    """An engine whose policies are ACTIVE but which no gateway is attached to guards nothing."""
    from app.services import aws_clients, policy_engines, policy_store
    from tests.conftest import ws_ctx
    from tests.policy_fakes import BASELINE, FakeS3

    monkeypatch.setattr(overview_mod, "_traces_active", lambda _ws: False)
    monkeypatch.setattr(aws_clients, "client", lambda service, ctx, **kw: FakeS3())
    set_default_resources({"artifacts_bucket": "b"})
    db = SessionLocal()
    try:
        engine = policy_engines.create_engine(db, DEFAULT_WORKSPACE_ID, name="hr", operator="t")
        policy = policy_store.create_policy(db, engine, name="baseline", statement=BASELINE,
                                            operator="t")
        policy_store.publish_policy(db, ws_ctx({"artifacts_bucket": "b"}), engine, policy.id,
                                    operator="t")
    finally:
        db.close()
    body = client.get("/api/overview").json()
    assert body["services"]["policy"] is False
    assert body["service_detail"]["policy"] == "1 engine(s) · 0 gateway(s)"
