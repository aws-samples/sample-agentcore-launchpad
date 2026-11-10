"""Registry payload builders (pure), register-stage behaviour on the local backend."""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import app.deployer.registration as registration
from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.models.ledger import Agent
from app.services import registry_backend as reg
from app.services.registry_backend import RegistryUnavailableError
from app.services.registry_local import LocalRegistryBackend
from tests.conftest import ws_ctx


def test_a2a_card_and_descriptors():
    card = reg.build_a2a_card(
        name="hr-assistant", description="HR agent", arn="arn:x", version="2", method="zip_runtime"
    )
    assert card["name"] == "hr-assistant" and card["version"] == "2"
    desc = reg.build_a2a_descriptors(card)
    inline = json.loads(desc["a2a"]["agentCard"]["inlineContent"])
    assert inline["url"] == "arn:x"
    assert desc["a2a"]["agentCard"]["schemaVersion"] == reg.A2A_SCHEMA_VERSION


def test_a2a_card_data_plane_url_is_partition_aware_and_percent_encoded():
    ws = ws_ctx(region="cn-northwest-1")
    arn = ws.arn("bedrock-agentcore", "runtime/abc")
    card = reg.build_a2a_card(name="a", description="d", arn=arn, method="zip_runtime",
                              workspace=ws)
    assert card["url"].startswith("https://bedrock-agentcore.cn-northwest-1.amazonaws.com.cn/")
    assert "arn%3Aaws-cn%3A" in card["url"] and card["url"].endswith("?qualifier=DEFAULT")


def test_mcp_descriptors_server_json():
    desc = reg.build_mcp_descriptors(
        target="hr-database", description="d", gateway_url="https://gw/mcp",
        tools=[{"name": "get_employee", "description": "x", "inputSchema": {}}],
    )
    server = json.loads(desc["mcp"]["server"]["inlineContent"])
    assert server["name"] == "io.launchpad/hr-database"
    assert server["remotes"] == [{"type": "streamable-http", "url": "https://gw/mcp"}]
    assert desc["mcp"]["server"]["schemaVersion"] == "2025-07-09"
    assert json.loads(desc["mcp"]["tools"]["inlineContent"])["tools"][0]["name"] == "get_employee"


def test_skills_descriptors():
    desc = reg.build_skills_descriptors(
        skill_md="---\nname: x\n---\n# X", definition={"name": "x", "path": "s3://b/skills/x/"}
    )
    assert desc["agentSkills"]["skillDefinition"]["schemaVersion"] == "0.1.0"
    assert "# X" in desc["agentSkills"]["skillMd"]["inlineContent"]


@pytest.mark.parametrize(("value", "expected"), [
    ("A2A", "A2A"), ("agent", "A2A"), ("MCP", "MCP"), ("mcp", "MCP"),
    ("AGENT_SKILLS", "AGENT_SKILLS"), ("skill", "AGENT_SKILLS"), ("Custom", "CUSTOM"),
])
def test_platform_record_type_accepts_platform_and_ga_names(value, expected):
    assert reg.platform_record_type(value) == expected


def test_record_type_helpers_reject_unknown():
    with pytest.raises(ValueError):
        reg.ga_record_type("gadget")
    with pytest.raises(ValueError):
        reg.platform_record_type("gadget")


def test_bump_minor_version_preserves_type_suffix():
    assert reg.bump_minor_version("1.0.0-skill") == "1.1.0-skill"
    assert reg.bump_minor_version("1.0.0-mcp") == "1.1.0-mcp"
    assert reg.bump_minor_version("1.0.0") == "1.1.0"
    assert reg.bump_minor_version("garbage") == "1.1.0"


def test_descriptor_fingerprint_normalizes_embedded_json():
    a = {"mcp": {"server": {"inlineContent": json.dumps({"b": 1, "a": [1, 2]})}}}
    b = {"mcp": {"server": {"inlineContent": json.dumps({"a": [1, 2], "b": 1}, indent=2)}}}
    assert reg.descriptor_fingerprint(a) == reg.descriptor_fingerprint(b)
    assert reg.descriptor_fingerprint(a) != reg.descriptor_fingerprint({"mcp": {}})


# ---------- register stage on the local backend ----------


def _agent(**kw) -> Agent:
    db = SessionLocal()
    agent = Agent(**{
        "workspace_id": DEFAULT_WORKSPACE_ID, "name": "aurora-faq", "method": "zip_runtime",
        "status": "active", "arn": "arn:aws-cn:bedrock-agentcore:cn-northwest-1:1:runtime/r-1",
        "spec": {"system_prompt": "Answer FAQs", "protocol": "a2a"}, **kw,
    })
    db.add(agent)
    db.commit()
    db.refresh(agent)
    db.expunge(agent)
    db.close()
    return agent


def test_register_agent_record_creates_and_auto_submits(client):
    backend = LocalRegistryBackend()
    reg.set_backend(backend)
    ws = ws_ctx(region="cn-northwest-1")
    agent = _agent()
    out = registration.register_agent_record(agent, ws)
    assert out["created"] is True and out["changed"] is True
    record = backend.get_record(ws, out["record_id"])
    assert record["descriptorType"] == "A2A" and record["status"] == "PENDING_APPROVAL"
    card = reg.stored_a2a_card(record)
    assert card["metadata"]["launchpad.transport"] == "a2a-jsonrpc"
    assert card["url"].startswith("https://bedrock-agentcore.cn-northwest-1.amazonaws.com.cn/")


def test_identical_redeploy_leaves_an_approved_record_approved(client):
    backend = LocalRegistryBackend()
    reg.set_backend(backend)
    ws = ws_ctx(region="cn-northwest-1")
    agent = _agent()
    rid = registration.register_agent_record(agent, ws)["record_id"]
    backend.approve_record(ws, rid)
    again = registration.register_agent_record(agent, ws)
    assert again == {"record_id": rid, "created": False, "changed": False}
    assert backend.get_record(ws, rid)["status"] == "APPROVED"


def test_changed_card_refreshes_the_record_and_reenters_review(client):
    backend = LocalRegistryBackend()
    reg.set_backend(backend)
    ws = ws_ctx(region="cn-northwest-1")
    agent = _agent()
    rid = registration.register_agent_record(agent, ws)["record_id"]
    backend.approve_record(ws, rid)
    agent.arn = "arn:aws-cn:bedrock-agentcore:cn-northwest-1:1:runtime/r-2"  # re-created runtime
    out = registration.register_agent_record(agent, ws)
    assert out == {"record_id": rid, "created": False, "changed": True}
    record = backend.get_record(ws, rid)
    assert record["status"] == "DRAFT" and "runtime%2Fr-2" in reg.stored_a2a_card(record)["url"]


def test_redeploy_with_changed_card_skips_registration_when_record_is_deprecated(client):
    """A redeploy must not fail the whole job just because the registry record was
    disabled: the runtime already redeployed successfully by the time register_stage
    runs, so a DEPRECATED a2a record is a skip, not a stage failure (and the record
    itself is left untouched — only `delete` can move a DEPRECATED record)."""
    backend = LocalRegistryBackend()
    reg.set_backend(backend)
    ws = ws_ctx(region="cn-northwest-1")
    agent = _agent()
    rid = registration.register_agent_record(agent, ws)["record_id"]
    backend.approve_record(ws, rid)
    backend.disable_record(ws, rid)
    before = backend.get_record(ws, rid)

    db = SessionLocal()
    row = db.get(Agent, agent.id)
    row.arn = "arn:aws-cn:bedrock-agentcore:cn-northwest-1:1:runtime/r-2"  # changed card
    db.commit()
    ctx = SimpleNamespace(session=lambda: db, log=lambda _m: None, workspace=ws)
    try:
        result = registration.register_stage(ctx, row)
    finally:
        db.close()

    assert result.skipped is True
    assert "DEPRECATED" in result.detail
    after = backend.get_record(ws, rid)
    assert after["status"] == "DEPRECATED" and after == before


def test_register_stage_log_matches_what_happened(monkeypatch):
    logs: list[str] = []
    row = SimpleNamespace(id="a1", registry_record_id=None, system_key=None)
    session = MagicMock()
    session.get.return_value = row
    ctx = SimpleNamespace(session=lambda: session, log=logs.append, workspace=object())
    cases = (
        ({"created": True, "changed": True}, "auto-submitted", "created"),
        ({"created": False, "changed": True}, "DRAFT — needs re-approval", "refreshed"),
        ({"created": False, "changed": False}, "approval untouched", "unchanged"),
    )
    for result, expected_log, expected_detail in cases:
        logs.clear()
        monkeypatch.setattr(
            registration, "register_agent_record",
            lambda _row, _ws, _r=result: {"record_id": "rec-1", **_r},
        )
        out = registration.register_stage(ctx, row)
        assert expected_log in logs[0], logs
        assert expected_detail in out.detail
        assert row.registry_record_id == "rec-1"
    assert "auto-submitted" not in logs[0]  # the unchanged case never claims a submit


def test_register_stage_skips_when_no_backend_is_registered(monkeypatch):
    logs: list[str] = []
    row = SimpleNamespace(id="a1", registry_record_id=None, system_key=None)
    session = MagicMock()
    session.get.return_value = row
    ctx = SimpleNamespace(session=lambda: session, log=logs.append, workspace=object())
    monkeypatch.setattr(
        registration, "register_agent_record",
        lambda _row, _ws: (_ for _ in ()).throw(RegistryUnavailableError("no backend")),
    )
    result = registration.register_stage(ctx, row)
    assert result.skipped is True and "register skipped" in result.detail
    session.commit.assert_not_called()


def test_registry_route_returns_feature_unavailable_envelope_without_a_backend(client):
    reg.set_backend(None)
    response = client.get("/api/registry/records")
    assert response.status_code == 503
    assert response.json()["code"] == "feature.unavailable"
