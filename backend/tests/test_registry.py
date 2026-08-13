"""Registry payload builders, upsert/update wrappers, status transitions, search."""

import json
from unittest.mock import MagicMock

from app.services.agentcore import registry as reg

RID = "launchpad-registry-x"


def test_a2a_card_and_descriptors():
    card = reg.build_a2a_card(
        name="hr-assistant", description="HR agent", arn="arn:x", version="2", method="harness"
    )
    assert card["name"] == "hr-assistant" and card["version"] == "2"
    desc = reg.build_a2a_descriptors(card)
    inline = json.loads(desc["a2a"]["agentCard"]["inlineContent"])
    assert inline["url"] == "arn:x"
    assert desc["a2a"]["agentCard"]["schemaVersion"] == reg.A2A_SCHEMA_VERSION


def test_mcp_descriptors_server_json():
    desc = reg.build_mcp_descriptors(
        target="hr-database",
        description="d",
        gateway_url="https://gw/mcp",
        tools=[{"name": "get_employee", "description": "x", "inputSchema": {}}],
    )
    server = json.loads(desc["mcp"]["server"]["inlineContent"])
    assert server["name"] == "io.launchpad/hr-database"
    assert server["remotes"] == [{"type": "streamable-http", "url": "https://gw/mcp"}]
    assert desc["mcp"]["server"]["schemaVersion"] == "2025-07-09"
    tools = json.loads(desc["mcp"]["tools"]["inlineContent"])
    assert tools["tools"][0]["name"] == "get_employee"


def test_skills_descriptors():
    desc = reg.build_skills_descriptors(
        skill_md="---\nname: x\n---\n# X", definition={"name": "x", "path": "s3://b/skills/x/"}
    )
    assert desc["agentSkills"]["skillDefinition"]["schemaVersion"] == "0.1.0"
    assert "# X" in desc["agentSkills"]["skillMd"]["inlineContent"]


def test_wrap_descriptors_for_update_nesting():
    create_style = reg.build_mcp_descriptors(
        target="t", description="d", gateway_url="u", tools=[]
    )
    wrapped = reg.wrap_descriptors_for_update(create_style)
    mcp = wrapped["optionalValue"]["mcpServer"]["optionalValue"]
    assert mcp["data"]["optionalValue"]
    assert "tools" in mcp["additionalData"]["optionalValue"]
    a2a_wrapped = reg.wrap_descriptors_for_update(
        {"a2a": {"agentCard": {"inlineContent": "{}", "schemaVersion": "0.3.0"}}}
    )
    a2a = a2a_wrapped["optionalValue"]["a2aAgentCard"]["optionalValue"]
    assert a2a["data"] == {"optionalValue": "{}"}


def test_ga_descriptor_round_trip():
    cases = [
        (
            "A2A",
            reg.build_a2a_descriptors(
                reg.build_a2a_card(
                    name="a", description="d", arn="arn:x", version="1", method="harness"
                )
            ),
        ),
        (
            "MCP",
            reg.build_mcp_descriptors(
                target="m", description="d", gateway_url="https://mcp", tools=[]
            ),
        ),
        (
            "AGENT_SKILLS",
            reg.build_skills_descriptors(
                skill_md="---\nname: abc\n---\n# A",
                definition={"name": "abc", "path": "s3://bucket/abc"},
            ),
        ),
    ]
    for descriptor_type, descriptors in cases:
        aws_type = {"A2A": "AGENT", "MCP": "MCP", "AGENT_SKILLS": "SKILL"}[
            descriptor_type
        ]
        assert reg.from_ga_descriptors(
            aws_type, reg.to_ga_descriptors(descriptor_type, descriptors)
        ) == descriptors


def test_upsert_creates_and_derives_record_id():
    client = MagicMock()
    client.list_registry_records.return_value = {"registryRecords": []}
    client.create_registry_record.return_value = {
        "recordArn": f"arn:aws:agent-registry:us-west-2:1:registry/{RID}/record/abc123",
        "status": "CREATING",
    }
    record, created = reg.upsert_record(
        client,
        RID,
        name="x",
        description="d",
        descriptor_type="MCP",
        descriptors=reg.build_mcp_descriptors(
            target="x", description="d", gateway_url="https://example.test/mcp", tools=None
        ),
    )
    assert created is True and record["recordId"] == "abc123"
    kwargs = client.create_registry_record.call_args.kwargs
    assert kwargs["recordType"] == "MCP"
    assert kwargs["displayName"] == "x"
    assert kwargs["recordVersion"] == "1.0.0-mcp"
    assert "mcpServer" in kwargs["descriptors"]


def test_upsert_updates_with_wrappers():
    client = MagicMock()
    client.list_registry_records.return_value = {
        "registryRecords": [
            {"name": "x", "recordId": "abc123", "recordType": "MCP"}
        ]
    }
    client.update_registry_record.return_value = {
        "recordId": "abc123",
        "recordType": "MCP",
        "status": "DRAFT",
    }
    _, created = reg.upsert_record(
        client, RID, name="x", description="d", descriptor_type="MCP",
        descriptors={"mcp": {"server": {"schemaVersion": "v", "inlineContent": "{}"}}},
    )
    assert created is False
    kwargs = client.update_registry_record.call_args.kwargs
    assert kwargs["description"] == {"optionalValue": "d"}
    assert kwargs["recordType"] == "MCP"
    assert "optionalValue" in kwargs["descriptors"]


def test_status_transitions():
    client = MagicMock()
    reg.submit_record(client, RID, "r1")
    client.submit_registry_record_for_approval.assert_called_once_with(
        registryId=RID, recordId="r1"
    )
    reg.approve_record(client, RID, "r1")
    assert client.update_registry_record_status.call_args.kwargs["status"] == "APPROVED"
    reg.disable_record(client, RID, "r1")
    assert client.update_registry_record_status.call_args.kwargs["status"] == "DEPRECATED"


def test_wait_record_settled():
    client = MagicMock()
    client.get_registry_record.side_effect = [
        {"status": "CREATING"},
        {"status": "DRAFT"},
    ]
    record = reg.wait_record_settled(client, RID, "r1", sleeper=lambda _: None)
    assert record["status"] == "DRAFT"


def test_search_caps_max_results():
    client = MagicMock()
    client.search_discoverable_registry_records.return_value = {
        "registryRecords": [{"name": "a", "recordType": "AGENT"}]
    }
    out = reg.search_records(client, [RID], "expense")
    assert out[0]["descriptorType"] == "A2A"
    assert (
        client.search_discoverable_registry_records.call_args.kwargs["maxResults"]
        <= 20
    )


def test_harness_skills_round_trip():
    """Registry skill prefixes land as skills[{s3:{uri}}] in CreateHarness
    params — the `path` member is a filesystem path and never loads from S3."""
    from app.deployer.harness import build_create_params
    from app.schemas.agent import AgentSpec

    spec = AgentSpec(
        name="skill-agent", method="harness", system_prompt="x",
        skills=["s3://bkt/skills/expense-report-writer/"],
    )
    params = build_create_params(spec, "arn:role", None)
    assert params["skills"] == [{"s3": {"uri": "s3://bkt/skills/expense-report-writer/"}}]


def test_register_stage_log_does_not_claim_a_submit_it_never_made(monkeypatch):
    """Only NEW records are auto-submitted. UpdateRegistryRecord resets an
    existing record to DRAFT and re-approval is a human step, so the refresh
    path must not log "auto-submitted" — that mismatch (log says submitted, AWS
    says DRAFT) reads as a broken status machine to whoever debugs next.
    """
    from types import SimpleNamespace

    import app.deployer.registration as registration

    logs: list[str] = []
    row = SimpleNamespace(id="a1", registry_record_id=None)
    session = MagicMock()
    session.get.return_value = row
    ctx = SimpleNamespace(session=lambda: session, log=logs.append, workspace=object())

    for created, expected in ((True, "auto-submitted"), (False, "DRAFT")):
        logs.clear()
        # Stub arity mirrors the real (agent, workspace) signature — the live
        # us-east-2 deploy failed on exactly this drift going unnoticed.
        monkeypatch.setattr(
            registration, "register_agent_record",
            lambda _row, _ws, created=created: {"record_id": "rec-1", "created": created},
        )
        result = registration.register_stage(ctx, row)
        assert expected in logs[0], logs
        assert ("created" if created else "refreshed") in result.detail
    # the misleading combination must be impossible
    assert "auto-submitted" not in logs[0]


def test_register_stage_skips_only_explicit_registry_unavailability(monkeypatch):
    from types import SimpleNamespace

    import app.deployer.registration as registration
    from app.services.registry_console import RegistryUnavailableError

    logs: list[str] = []
    row = SimpleNamespace(id="a1", registry_record_id=None)
    session = MagicMock()
    session.get.return_value = row
    ctx = SimpleNamespace(session=lambda: session, log=logs.append, workspace=object())
    monkeypatch.setattr(
        registration,
        "register_agent_record",
        lambda _row, _ws: (_ for _ in ()).throw(
            RegistryUnavailableError("blocked by account policy")
        ),
    )

    result = registration.register_stage(ctx, row)

    assert result.skipped is True
    assert "register skipped" in result.detail
    session.commit.assert_not_called()


def test_registry_endpoint_returns_unavailable_envelope(client, monkeypatch):
    import app.services.registry_console as console

    monkeypatch.setattr(
        console,
        "console_list",
        lambda *_args: (_ for _ in ()).throw(
            console.RegistryUnavailableError("blocked by account policy")
        ),
    )

    response = client.get("/api/registry/records")

    assert response.status_code == 503
    assert response.json() == {
        "code": "registry.unavailable",
        "message": "blocked by account policy",
        "detail": {"reason": "blocked by account policy"},
    }
