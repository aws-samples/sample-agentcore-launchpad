"""Runtime discovery, import ownership, and invocation capability contracts."""

from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError

import app.routers.agents as agents_router
import app.services.chat as chat_service
import app.services.invoke as invoke_service
from app.core.config import get_settings
from app.core.db import SessionLocal
from app.models.ledger import Agent, Deployment
from app.services.agentcore.runtime import list_runtimes
from app.services.runtime_discovery import invoke_capability


def _detail(
    runtime_id: str,
    *,
    name: str | None = None,
    protocol: str = "HTTP",
    status: str = "READY",
    version: str = "3",
    artifact: str = "code",
    custom_jwt: bool = False,
) -> dict:
    runtime_name = name or runtime_id.rsplit("-", 1)[0]
    detail = {
        "agentRuntimeId": runtime_id,
        "agentRuntimeArn": (
            f"arn:aws:bedrock-agentcore:us-west-2:111122223333:runtime/{runtime_id}"
        ),
        "agentRuntimeName": runtime_name,
        "agentRuntimeVersion": version,
        "description": f"{runtime_name} description",
        "lastUpdatedAt": datetime(2026, 7, 31, 12, 30, tzinfo=UTC),
        "status": status,
        "protocolConfiguration": {"serverProtocol": protocol},
        "agentRuntimeArtifact": {
            "codeConfiguration" if artifact == "code" else "containerConfiguration": {
                "code": {"s3": {"bucket": "secret-bucket", "prefix": "secret-key"}},
                "containerUri": "111122223333.dkr.ecr.us-west-2.amazonaws.com/private",
            }
        },
        "environmentVariables": {"PRIVATE_TOKEN": "do-not-leak"},
        "roleArn": "arn:aws:iam::111122223333:role/private-role",
    }
    if custom_jwt:
        detail["authorizerConfiguration"] = {
            "customJWTAuthorizer": {
                "discoveryUrl": "https://issuer.example/.well-known/openid-configuration",
                "allowedClients": ["secret-client"],
            }
        }
    return detail


def _summary(detail: dict) -> dict:
    return {
        key: detail[key]
        for key in (
            "agentRuntimeId",
            "agentRuntimeArn",
            "agentRuntimeName",
            "agentRuntimeVersion",
            "description",
            "lastUpdatedAt",
            "status",
        )
    }


def _harness(name: str, *, status: str = "READY", version: str = "2") -> dict:
    return {
        "harnessId": f"{name}-1234567890",
        "harnessName": name,
        "arn": f"arn:aws:bedrock-agentcore:us-west-2:111122223333:harness/{name}-1234567890",
        "status": status,
        "harnessVersion": version,
        "createdAt": datetime(2026, 6, 26, 9, 0, tzinfo=UTC),
        "updatedAt": datetime(2026, 7, 31, 12, 30, tzinfo=UTC),
    }


def _harness_detail(name: str, *, custom_jwt: bool = False, **kwargs) -> dict:
    """GetHarness adds the configuration the projection must never expose."""
    detail = _harness(name, **kwargs) | {
        "executionRoleArn": "arn:aws:iam::111122223333:role/private-harness-role",
        "systemPrompt": "do-not-leak-prompt",
        "environmentVariables": {"PRIVATE_TOKEN": "do-not-leak"},
        "model": {"bedrock": {"modelId": "anthropic.claude"}},
    }
    if custom_jwt:
        detail["authorizerConfiguration"] = {
            "customJWTAuthorizer": {
                "discoveryUrl": "https://issuer.example/.well-known/openid-configuration",
                "allowedClients": ["secret-client"],
            }
        }
    return detail


def _mock_control(
    monkeypatch,
    details: list[dict],
    harnesses: list[dict] | None = None,
    harness_details: list[dict] | None = None,
) -> MagicMock:
    control = MagicMock()
    control.list_agent_runtimes.return_value = {
        "agentRuntimes": [_summary(detail) for detail in details]
    }
    control.list_harnesses.return_value = {"harnesses": harnesses or []}
    by_id = {detail["agentRuntimeId"]: detail for detail in details}
    control.get_agent_runtime.side_effect = lambda agentRuntimeId: by_id[agentRuntimeId]
    by_harness_id = {
        detail["harnessId"]: detail
        for detail in (harness_details if harness_details is not None else harnesses or [])
    }
    control.get_harness.side_effect = lambda harnessId: {
        "harness": by_harness_id[harnessId]
    }
    monkeypatch.setattr(agents_router, "control_client", lambda: control)
    return control


def test_list_runtimes_follows_all_pages():
    control = MagicMock()
    control.list_agent_runtimes.side_effect = [
        {"agentRuntimes": [{"agentRuntimeId": "one"}], "nextToken": "page-2"},
        {"agentRuntimes": [{"agentRuntimeId": "two"}]},
    ]

    assert [row["agentRuntimeId"] for row in list_runtimes(control)] == ["one", "two"]
    assert control.list_agent_runtimes.call_args_list[0].kwargs == {"maxResults": 100}
    assert control.list_agent_runtimes.call_args_list[1].kwargs == {
        "maxResults": 100,
        "nextToken": "page-2",
    }


def test_scan_sanitizes_details_and_projects_protocol_eligibility(client, monkeypatch):
    unknown_auth = _detail("unknown-auth-abcdefghij")
    unknown_auth["authorizerConfiguration"] = {"futureAuthorizer": {"secret": "hidden"}}
    details = [
        _detail("http-abcdefghij"),
        _detail("a2a-abcdefghij", protocol="A2A", artifact="container"),
        _detail("mcp-abcdefghij", protocol="MCP"),
        _detail("jwt-abcdefghij", custom_jwt=True),
        unknown_auth,
    ]
    _mock_control(monkeypatch, details)

    response = client.get("/api/agents/discovery")

    assert response.status_code == 200
    body = response.json()
    assert body["region"] == get_settings().region
    rows = {row["runtime_id"]: row for row in body["runtimes"]}
    assert rows["http-abcdefghij"]["importable"] is True
    assert rows["a2a-abcdefghij"]["protocol"] == "A2A"
    assert rows["a2a-abcdefghij"]["artifact_type"] == "container"
    assert rows["mcp-abcdefghij"]["importable"] is False
    assert rows["mcp-abcdefghij"]["reason_code"] == "not-agent-protocol"
    assert rows["jwt-abcdefghij"]["importable"] is True
    assert rows["jwt-abcdefghij"]["invoke_capability"]["reason_code"] == (
        "external-authorizer"
    )
    assert rows["unknown-auth-abcdefghij"]["authorizer_type"] == "unknown"
    assert rows["unknown-auth-abcdefghij"]["invoke_capability"]["reason_code"] == (
        "external-authorizer"
    )
    serialized = response.text
    for secret in (
        "do-not-leak",
        "secret-bucket",
        "private-role",
        "secret-client",
        "hidden",
    ):
        assert secret not in serialized


def test_scan_keeps_candidate_when_detail_inspection_fails(client, monkeypatch):
    detail = _detail("broken-abcdefghij")
    control = _mock_control(monkeypatch, [detail])
    control.get_agent_runtime.side_effect = ClientError(
        {"Error": {"Code": "AccessDeniedException", "Message": "denied"}},
        "GetAgentRuntime",
    )

    row = client.get("/api/agents/discovery").json()["runtimes"][0]

    assert row["runtime_id"] == "broken-abcdefghij"
    assert row["importable"] is False
    assert row["reason_code"] == "inspection-failed"
    assert "AccessDeniedException" in row["reason"]


def test_list_failure_is_typed(client, monkeypatch):
    control = MagicMock()
    control.list_agent_runtimes.side_effect = ClientError(
        {"Error": {"Code": "ThrottlingException", "Message": "slow down"}},
        "ListAgentRuntimes",
    )
    monkeypatch.setattr(agents_router, "control_client", lambda: control)

    response = client.get("/api/agents/discovery")

    assert response.status_code == 502
    assert response.json()["code"] == "runtime.discovery_failed"


def test_import_is_idempotent_and_refreshes_sanitized_metadata(client, monkeypatch):
    detail = _detail("owned-abcdefghij", name="owned-runtime")
    control = _mock_control(monkeypatch, [detail])

    first = client.post(
        "/api/agents/discovery/import",
        json={"runtime_ids": [detail["agentRuntimeId"], detail["agentRuntimeId"]]},
    )

    assert first.status_code == 200
    assert len(first.json()["imported"]) == 1
    agent_id = first.json()["imported"][0]["agent_id"]
    db = SessionLocal()
    row = db.get(Agent, agent_id)
    assert row.method == "discovered_runtime"
    assert row.owner == "aws-discovery"
    assert row.status == "active"
    assert row.version == "3"
    assert row.spec == {
        "protocol": "http",
        "discovery": {
            "runtime_name": "owned-runtime",
            "description": "owned-runtime description",
            "artifact_type": "code",
            "authorizer_type": "none",
            "aws_status": "READY",
            "last_updated_at": "2026-07-31T12:30:00+00:00",
        },
    }
    assert db.query(Deployment).filter(Deployment.agent_id == agent_id).count() == 0
    db.close()

    scanned = client.get("/api/agents/discovery").json()["runtimes"][0]
    assert scanned["managed_agent_id"] == agent_id
    assert scanned["managed_agent_method"] == "discovered_runtime"

    refreshed = _detail(
        "owned-abcdefghij",
        name="owned-runtime",
        status="UPDATE_FAILED",
        version="4",
        artifact="container",
    )
    control.get_agent_runtime.side_effect = lambda agentRuntimeId: refreshed
    second = client.post(
        "/api/agents/discovery/import", json={"runtime_ids": ["owned-abcdefghij"]}
    )

    assert second.status_code == 200
    assert second.json()["imported"] == []
    assert second.json()["updated"][0]["agent_id"] == agent_id
    db = SessionLocal()
    row = db.get(Agent, agent_id)
    assert row.status == "failed"
    assert row.version == "4"
    assert row.spec["discovery"]["artifact_type"] == "container"
    assert db.query(Agent).filter(Agent.resource_id == "owned-abcdefghij").count() == 1
    db.close()


def test_harness_backing_runtime_is_flagged_and_linked_to_its_harness_agent(
    client, monkeypatch
):
    harness = _harness("support")
    detail = _detail(
        "harness_support-abcdefghij", name="harness_support", artifact="container"
    )
    db = SessionLocal()
    owner = Agent(
        name="support-agent",
        method="harness",
        status="active",
        resource_id=harness["harnessId"],
        arn=harness["arn"],
        spec={"name": "support-agent", "method": "harness"},
    )
    db.add(owner)
    db.commit()
    owner_id = owner.id
    db.close()
    _mock_control(monkeypatch, [detail], harnesses=[harness])

    row = client.get("/api/agents/discovery").json()["runtimes"][0]

    assert row["artifact_type"] == "harness"
    assert row["importable"] is False
    assert row["reason_code"] == "harness-managed"
    assert row["invoke_capability"]["reason_code"] == "harness-managed"
    assert row["managed_agent_id"] == owner_id
    assert row["managed_agent_method"] == "harness"

    body = client.post(
        "/api/agents/discovery/import", json={"runtime_ids": [detail["agentRuntimeId"]]}
    ).json()

    assert body["imported"] == []
    assert body["already_managed"] == [
        {
            "runtime_id": detail["agentRuntimeId"],
            "agent_id": owner_id,
            "agent_name": "support-agent",
        }
    ]
    db = SessionLocal()
    assert (
        db.query(Agent)
        .filter(Agent.arn == detail["agentRuntimeArn"], Agent.status != "deleted")
        .count()
        == 0
    )
    db.close()


def test_external_harness_backing_runtime_import_is_rejected(client, monkeypatch):
    detail = _detail(
        "harness_alien-abcdefghij", name="harness_alien", artifact="container"
    )
    _mock_control(monkeypatch, [detail], harnesses=[_harness("alien")])

    body = client.post(
        "/api/agents/discovery/import", json={"runtime_ids": [detail["agentRuntimeId"]]}
    ).json()

    assert body["imported"] == []
    assert body["failed"][0]["reason_code"] == "harness-managed"


def test_harness_image_heuristic_flags_backing_runtime_when_list_harnesses_fails(
    client, monkeypatch
):
    detail = _detail("harness_orphan-abcdefghij", name="harness_orphan")
    detail["agentRuntimeArtifact"] = {
        "containerConfiguration": {
            "containerUri": "public.ecr.aws/y5s8y8h8/harness-us-west-2:latest"
        }
    }
    control = _mock_control(monkeypatch, [detail])
    control.list_harnesses.side_effect = ClientError(
        {"Error": {"Code": "AccessDeniedException", "Message": "denied"}},
        "ListHarnesses",
    )

    row = client.get("/api/agents/discovery").json()["runtimes"][0]

    assert row["artifact_type"] == "harness"
    assert row["importable"] is False
    assert row["reason_code"] == "harness-managed"
    assert row["managed_agent_id"] is None


def test_stale_imported_harness_backing_runtime_is_not_invokable():
    agent = Agent(
        name="stale-harness-import",
        method="discovered_runtime",
        status="active",
        arn="arn:aws:bedrock-agentcore:us-west-2:111:runtime/harness_stale-abcdefghij",
        spec={
            "protocol": "http",
            "discovery": {
                "aws_status": "READY",
                "authorizer_type": "none",
                "artifact_type": "harness",
            },
        },
    )

    capability = invoke_capability(agent)

    assert capability["eligible"] is False
    assert capability["reason_code"] == "harness-managed"


def test_import_never_rewrites_launchpad_managed_agent(client, monkeypatch):
    detail = _detail("managed-abcdefghij", name="managed-runtime")
    db = SessionLocal()
    managed = Agent(
        name="launchpad-agent",
        method="zip_runtime",
        status="active",
        resource_id=detail["agentRuntimeId"],
        arn=detail["agentRuntimeArn"],
        spec={"name": "launchpad-agent", "method": "zip_runtime"},
    )
    db.add(managed)
    db.commit()
    managed_id = managed.id
    db.close()
    _mock_control(monkeypatch, [detail])

    response = client.post(
        "/api/agents/discovery/import", json={"runtime_ids": [detail["agentRuntimeId"]]}
    )

    assert response.status_code == 200
    assert response.json()["already_managed"] == [
        {
            "runtime_id": detail["agentRuntimeId"],
            "agent_id": managed_id,
            "agent_name": "launchpad-agent",
        }
    ]
    db = SessionLocal()
    row = db.get(Agent, managed_id)
    assert row.method == "zip_runtime"
    assert row.name == "launchpad-agent"
    db.close()


def test_mcp_import_rejected_without_hiding_valid_import(client, monkeypatch):
    http = _detail("valid-abcdefghij")
    mcp = _detail("tools-abcdefghij", protocol="MCP")
    _mock_control(monkeypatch, [http, mcp])

    body = client.post(
        "/api/agents/discovery/import",
        json={"runtime_ids": [http["agentRuntimeId"], mcp["agentRuntimeId"]]},
    ).json()

    assert len(body["imported"]) == 1
    assert body["failed"] == [
        {
            "runtime_id": "tools-abcdefghij",
            "reason_code": "not-agent-protocol",
            "reason": "MCP runtimes are tool servers, not agents.",
        }
    ]


def test_name_conflict_gets_runtime_id_suffix(client, monkeypatch):
    detail = _detail("shared-abcdefghij", name="shared")
    db = SessionLocal()
    db.add(Agent(name="shared", method="harness", status="active", spec={}))
    db.commit()
    db.close()
    _mock_control(monkeypatch, [detail])

    body = client.post(
        "/api/agents/discovery/import", json={"runtime_ids": [detail["agentRuntimeId"]]}
    ).json()

    assert body["imported"][0]["agent_name"] == "shared-abcdefghij"


def test_discovered_delete_is_detach_only_and_republish_is_rejected(client, monkeypatch):
    db = SessionLocal()
    row = Agent(
        name="external",
        method="discovered_runtime",
        status="active",
        resource_id="external-abcdefghij",
        arn="arn:aws:bedrock-agentcore:us-west-2:111:runtime/external-abcdefghij",
        owner="aws-discovery",
        spec={
            "protocol": "http",
            "discovery": {
                "aws_status": "READY",
                "authorizer_type": "none",
            },
        },
    )
    db.add(row)
    db.commit()
    agent_id = row.id
    db.close()
    runtime_delete = MagicMock()
    monkeypatch.setattr(agents_router.zip_method, "delete_agent_resources", runtime_delete)

    redeploy = client.post(
        f"/api/agents/{agent_id}/redeploy",
        json={"name": "external", "method": "harness", "system_prompt": "x"},
    )
    assert redeploy.status_code == 400
    assert redeploy.json()["code"] == "agent.redeploy_external"

    deleted = client.delete(f"/api/agents/{agent_id}")
    assert deleted.status_code == 200
    assert deleted.json()["aws_resource_deleted"] is False
    runtime_delete.assert_not_called()

    db = SessionLocal()
    detached = db.get(Agent, agent_id)
    assert detached.status == "deleted"
    assert invoke_capability(detached) == {
        "eligible": False,
        "reason_code": "not-active",
        "reason": "The agent is not active.",
    }
    db.close()

    key = client.post("/api/apikeys", json={"name": "detached-key"}).json()["key"]
    headers = {"X-Api-Key": key}
    assert agent_id not in {
        agent["id"] for agent in client.get("/v1/agents", headers=headers).json()["agents"]
    }
    for response in (
        client.post(f"/api/agents/{agent_id}/invoke", json={"prompt": "hello"}),
        client.post(f"/api/chat/{agent_id}", json={"prompt": "hello"}),
    ):
        assert response.status_code == 409
        assert response.json()["code"] == "agent.invoke_not_supported"
        assert response.json()["detail"]["reason_code"] == "not-active"
    v1_invoke = client.post(
        f"/v1/agents/{agent_id}/invoke",
        headers=headers,
        json={"prompt": "hello"},
    )
    assert v1_invoke.status_code == 404
    assert v1_invoke.json()["code"] == "agent.not_found"


def test_ineligible_discovered_runtime_is_hidden_and_rejected_by_all_invoke_surfaces(
    client,
):
    db = SessionLocal()
    eligible = Agent(
        name="external-http",
        method="discovered_runtime",
        status="active",
        arn="arn:aws:bedrock-agentcore:us-west-2:111:runtime/external-http",
        spec={
            "protocol": "http",
            "discovery": {"aws_status": "READY", "authorizer_type": "none"},
        },
    )
    protected = Agent(
        name="external-jwt",
        method="discovered_runtime",
        status="active",
        arn="arn:aws:bedrock-agentcore:us-west-2:111:runtime/external-jwt",
        spec={
            "protocol": "http",
            "discovery": {"aws_status": "READY", "authorizer_type": "custom_jwt"},
        },
    )
    db.add_all([eligible, protected])
    db.commit()
    eligible_id, protected_id = eligible.id, protected.id
    db.close()
    key = client.post("/api/apikeys", json={"name": "discovery-key"}).json()["key"]
    headers = {"X-Api-Key": key}

    listed = client.get("/v1/agents", headers=headers).json()["agents"]
    assert [agent["id"] for agent in listed] == [eligible_id]
    for response in (
        client.post(f"/api/agents/{protected_id}/invoke", json={"prompt": "hello"}),
        client.post(f"/api/chat/{protected_id}", json={"prompt": "hello"}),
        client.post(
            f"/v1/agents/{protected_id}/invoke",
            headers=headers,
            json={"prompt": "hello"},
        ),
    ):
        assert response.status_code == 409
        assert response.json()["code"] == "agent.invoke_not_supported"
        assert response.json()["detail"]["reason_code"] == "external-authorizer"


@pytest.mark.parametrize(
    ("protocol", "custom_jwt", "status", "eligible", "reason_code"),
    [
        ("HTTP", False, "READY", True, None),
        ("A2A", False, "READY", True, None),
        ("MCP", False, "READY", False, "not-agent-protocol"),
        ("HTTP", True, "READY", False, "external-authorizer"),
        ("HTTP", False, "UPDATING", False, "runtime-not-ready"),
    ],
)
def test_discovered_invoke_capability(
    protocol, custom_jwt, status, eligible, reason_code
):
    detail = _detail(
        "capability-abcdefghij",
        protocol=protocol,
        custom_jwt=custom_jwt,
        status=status,
    )
    agent = Agent(
        name="capability",
        method="discovered_runtime",
        status="active",
        arn=detail["agentRuntimeArn"],
        spec={
            "protocol": protocol.lower(),
            "discovery": {
                "aws_status": status,
                "authorizer_type": "custom_jwt" if custom_jwt else "none",
            },
        },
    )

    capability = invoke_capability(agent)

    assert capability["eligible"] is eligible
    assert capability["reason_code"] == reason_code


@pytest.mark.parametrize(("protocol", "invoke_name"), [("http", "runtime"), ("a2a", "a2a")])
def test_discovered_http_and_a2a_use_shared_runtime_dispatch(
    monkeypatch, protocol, invoke_name
):
    agent = Agent(
        name=f"external-{protocol}",
        method="discovered_runtime",
        status="active",
        arn=f"arn:aws:bedrock-agentcore:us-west-2:111:runtime/{protocol}",
        spec={
            "protocol": protocol,
            "discovery": {"aws_status": "READY", "authorizer_type": "none"},
        },
    )
    runtime_invoke = MagicMock(return_value={"text": "ok", "session_id": "s" * 40})
    a2a_invoke = MagicMock(return_value={"text": "ok", "session_id": "s" * 40})
    monkeypatch.setattr(invoke_service, "data_client", lambda: object())
    monkeypatch.setattr(invoke_service.rt, "invoke_runtime_text", runtime_invoke)
    monkeypatch.setattr(invoke_service.rt, "invoke_a2a_text", a2a_invoke)

    result = invoke_service.invoke_agent_text(agent, "hello")

    assert result["text"] == "ok"
    assert runtime_invoke.called is (invoke_name == "runtime")
    assert a2a_invoke.called is (invoke_name == "a2a")


# --- managed Harness discovery / import -----------------------------------


def _imported_harness_agent(name: str = "external-harness", **discovery) -> Agent:
    return Agent(
        name=name,
        method="discovered_runtime",
        status="active",
        resource_id=f"{name}-1234567890",
        arn=f"arn:aws:bedrock-agentcore:us-west-2:111122223333:harness/{name}-1234567890",
        owner="aws-discovery",
        spec={
            "protocol": "harness",
            "discovery": {
                "resource_type": "harness",
                "harness_name": name,
                "aws_status": "READY",
                "authorizer_type": "none",
                **discovery,
            },
        },
    )


def test_scan_projects_harnesses_with_status_eligibility_and_owner_linkage(
    client, monkeypatch
):
    alien = _harness("alien")
    owned = _harness("support")
    failed = _harness("halfbuilt", status="CREATE_FAILED")
    deleting = _harness("goingaway", status="DELETING")
    db = SessionLocal()
    launchpad = Agent(
        name="support-agent",
        method="harness",
        status="active",
        resource_id=owned["harnessId"],
        arn=owned["arn"],
        spec={"name": "support-agent", "method": "harness"},
    )
    db.add(launchpad)
    db.commit()
    launchpad_id = launchpad.id
    db.close()
    _mock_control(monkeypatch, [], harnesses=[alien, owned, failed, deleting])

    body = client.get("/api/agents/discovery").json()

    assert body["harness_scan_error"] is None
    rows = {row["harness_id"]: row for row in body["harnesses"]}
    assert rows[alien["harnessId"]] == {
        "harness_id": alien["harnessId"],
        "harness_arn": alien["arn"],
        "name": "alien",
        "description": "",
        "version": "2",
        "aws_status": "READY",
        "last_updated_at": "2026-07-31T12:30:00+00:00",
        "managed_agent_id": None,
        "managed_agent_name": None,
        "managed_agent_method": None,
        "importable": True,
        "reason_code": None,
        "reason": None,
    }
    assert rows[owned["harnessId"]]["managed_agent_id"] == launchpad_id
    assert rows[owned["harnessId"]]["managed_agent_method"] == "harness"
    for unready in (failed, deleting):
        assert rows[unready["harnessId"]]["importable"] is False
        assert rows[unready["harnessId"]]["reason_code"] == "harness-not-ready"


def test_harness_scan_failure_is_soft_and_keeps_runtimes(client, monkeypatch):
    control = _mock_control(monkeypatch, [_detail("http-abcdefghij")])
    control.list_harnesses.side_effect = ClientError(
        {"Error": {"Code": "AccessDeniedException", "Message": "denied"}},
        "ListHarnesses",
    )

    body = client.get("/api/agents/discovery").json()

    assert body["harnesses"] == []
    assert "AccessDeniedException" in body["harness_scan_error"]
    assert body["runtimes"][0]["runtime_id"] == "http-abcdefghij"


def test_harness_import_is_idempotent_and_hides_configuration(client, monkeypatch):
    detail = _harness_detail("myresearchagent_myresearchagent")
    control = _mock_control(
        monkeypatch, [], harnesses=[_harness("myresearchagent_myresearchagent")],
        harness_details=[detail],
    )

    first = client.post(
        "/api/agents/discovery/import",
        json={"harness_ids": [detail["harnessId"], detail["harnessId"]]},
    )

    assert first.status_code == 200
    assert first.json()["imported"] == [
        {
            "harness_id": detail["harnessId"],
            "agent_id": first.json()["imported"][0]["agent_id"],
            "agent_name": "myresearchagent_myresearchagent",
        }
    ]
    for secret in ("do-not-leak", "private-harness-role", "secret-client"):
        assert secret not in first.text
    agent_id = first.json()["imported"][0]["agent_id"]
    db = SessionLocal()
    row = db.get(Agent, agent_id)
    assert row.method == "discovered_runtime"
    assert row.owner == "aws-discovery"
    assert row.status == "active"
    assert row.arn == detail["arn"]
    assert row.resource_id == detail["harnessId"]
    assert row.version == "2"
    assert row.spec == {
        "protocol": "harness",
        "discovery": {
            "resource_type": "harness",
            "harness_name": "myresearchagent_myresearchagent",
            "description": "",
            "authorizer_type": "none",
            "aws_status": "READY",
            "last_updated_at": "2026-07-31T12:30:00+00:00",
        },
    }
    assert db.query(Deployment).filter(Deployment.agent_id == agent_id).count() == 0
    db.close()

    scanned = client.get("/api/agents/discovery").json()["harnesses"][0]
    assert scanned["managed_agent_id"] == agent_id
    assert scanned["managed_agent_method"] == "discovered_runtime"

    refreshed = _harness_detail(
        "myresearchagent_myresearchagent", status="UPDATE_FAILED", version="3"
    )
    control.get_harness.side_effect = lambda harnessId: {"harness": refreshed}
    second = client.post(
        "/api/agents/discovery/import", json={"harness_ids": [detail["harnessId"]]}
    ).json()

    assert second["imported"] == []
    assert second["updated"][0]["agent_id"] == agent_id
    db = SessionLocal()
    row = db.get(Agent, agent_id)
    assert row.status == "failed"
    assert row.version == "3"
    assert row.spec["discovery"]["aws_status"] == "UPDATE_FAILED"
    assert db.query(Agent).filter(Agent.resource_id == detail["harnessId"]).count() == 1
    db.close()


def test_harness_import_never_duplicates_a_launchpad_harness_agent(client, monkeypatch):
    detail = _harness_detail("support")
    db = SessionLocal()
    launchpad = Agent(
        name="support-agent",
        method="harness",
        status="active",
        resource_id=detail["harnessId"],
        arn=detail["arn"],
        spec={"name": "support-agent", "method": "harness"},
    )
    db.add(launchpad)
    db.commit()
    launchpad_id = launchpad.id
    db.close()
    _mock_control(monkeypatch, [], harness_details=[detail])

    body = client.post(
        "/api/agents/discovery/import", json={"harness_ids": [detail["harnessId"]]}
    ).json()

    assert body["imported"] == []
    assert body["already_managed"] == [
        {
            "harness_id": detail["harnessId"],
            "agent_id": launchpad_id,
            "agent_name": "support-agent",
        }
    ]
    db = SessionLocal()
    assert db.query(Agent).filter(Agent.status != "deleted").count() == 1
    assert db.get(Agent, launchpad_id).method == "harness"
    db.close()


def test_harness_import_reports_detail_and_status_failures(client, monkeypatch):
    ready = _harness_detail("healthy")
    broken = _harness_detail("halfbuilt", status="CREATE_FAILED")
    control = _mock_control(monkeypatch, [], harness_details=[ready, broken])
    gone = "vanished-1234567890"

    def get_harness(harnessId):
        if harnessId == gone:
            raise ClientError(
                {"Error": {"Code": "ResourceNotFoundException", "Message": "gone"}},
                "GetHarness",
            )
        return {"harness": ready if harnessId == ready["harnessId"] else broken}

    control.get_harness.side_effect = get_harness

    body = client.post(
        "/api/agents/discovery/import",
        json={"harness_ids": [ready["harnessId"], broken["harnessId"], gone]},
    ).json()

    assert [row["agent_name"] for row in body["imported"]] == ["healthy"]
    failures = {row["harness_id"]: row["reason_code"] for row in body["failed"]}
    assert failures == {
        broken["harnessId"]: "harness-not-ready",
        gone: "inspection-failed",
    }
    assert "ResourceNotFoundException" in next(
        row["reason"] for row in body["failed"] if row["harness_id"] == gone
    )


def test_import_request_rejects_an_empty_selection(client):
    response = client.post("/api/agents/discovery/import", json={})

    assert response.status_code == 422


@pytest.mark.parametrize(
    ("status", "authorizer_type", "eligible", "reason_code"),
    [
        ("READY", "none", True, None),
        ("CREATING", "none", False, "harness-not-ready"),
        ("CREATE_FAILED", "none", False, "harness-not-ready"),
        ("READY", "custom_jwt", False, "external-authorizer"),
    ],
)
def test_discovered_harness_invoke_capability(
    status, authorizer_type, eligible, reason_code
):
    agent = _imported_harness_agent(
        aws_status=status, authorizer_type=authorizer_type
    )

    capability = invoke_capability(agent)

    assert capability["eligible"] is eligible
    assert capability["reason_code"] == reason_code


def test_discovered_harness_invokes_through_invoke_harness(monkeypatch):
    agent = _imported_harness_agent()
    harness_invoke = MagicMock(return_value={"text": "ok", "session_id": "s" * 40})
    runtime_invoke = MagicMock()
    monkeypatch.setattr(invoke_service, "data_client", lambda: object())
    monkeypatch.setattr(invoke_service.hc, "invoke_harness_text", harness_invoke)
    monkeypatch.setattr(invoke_service.rt, "invoke_runtime_text", runtime_invoke)

    result = invoke_service.invoke_agent_text(agent, "hello")

    assert result["text"] == "ok"
    assert runtime_invoke.called is False
    assert harness_invoke.call_args.args[1] == agent.arn


def test_imported_harness_invoke_endpoint_reaches_the_harness_data_plane(
    client, monkeypatch
):
    db = SessionLocal()
    row = _imported_harness_agent(name="api_harness")
    db.add(row)
    db.commit()
    agent_id, harness_arn = row.id, row.arn
    db.close()
    data = MagicMock()
    data.invoke_harness.return_value = {
        "stream": [{"contentBlockDelta": {"delta": {"text": "4"}}}]
    }
    monkeypatch.setattr(invoke_service, "data_client", lambda: data)

    body = client.post(f"/api/agents/{agent_id}/invoke", json={"prompt": "2+2?"}).json()

    assert body["text"] == "4"
    assert data.invoke_harness.call_args.kwargs["harnessArn"] == harness_arn
    data.invoke_agent_runtime.assert_not_called()


def test_discovered_harness_chat_streams_harness_events(monkeypatch):
    agent = _imported_harness_agent()
    data = MagicMock()
    data.invoke_harness.return_value = {
        "stream": [
            {"contentBlockStart": {"start": {"toolUse": {"name": "search", "toolUseId": "t1"}}}},
            {"contentBlockDelta": {"delta": {"text": "hi"}}},
        ]
    }
    monkeypatch.setattr(chat_service, "data_client", lambda: data)

    events = list(chat_service.chat_stream(agent, "hello", session_id="s" * 40))

    assert events[0]["data"]["mode"] == "stream"
    assert [event["event"] for event in events] == ["meta", "tool", "delta", "done"]
    assert data.invoke_harness.call_args.kwargs["harnessArn"] == agent.arn


def test_imported_harness_delete_is_detach_only(client, monkeypatch):
    db = SessionLocal()
    row = _imported_harness_agent(name="alien_harness")
    db.add(row)
    db.commit()
    agent_id = row.id
    db.close()
    control = MagicMock()
    monkeypatch.setattr(agents_router, "control_client", lambda: control)
    harness_teardown = MagicMock()
    monkeypatch.setattr(
        agents_router.harness_method, "delete_agent_resources", harness_teardown
    )
    role_teardown = MagicMock()
    monkeypatch.setattr(agents_router.agent_iam, "delete_execution_role", role_teardown)

    response = client.delete(f"/api/agents/{agent_id}")

    assert response.status_code == 200
    assert response.json()["aws_resource_deleted"] is False
    harness_teardown.assert_not_called()
    role_teardown.assert_not_called()
    control.delete_harness.assert_not_called()
    db = SessionLocal()
    assert db.get(Agent, agent_id).status == "deleted"
    db.close()


def test_imported_harness_owns_its_backing_runtime_in_the_runtime_scan(
    client, monkeypatch
):
    summary = _harness("myresearch")
    detail = _harness_detail("myresearch")
    backing = _detail(
        "harness_myresearch-abcdefghij", name="harness_myresearch", artifact="container"
    )
    _mock_control(
        monkeypatch, [backing], harnesses=[summary], harness_details=[detail]
    )

    imported = client.post(
        "/api/agents/discovery/import", json={"harness_ids": [summary["harnessId"]]}
    ).json()["imported"][0]
    row = client.get("/api/agents/discovery").json()["runtimes"][0]

    assert row["artifact_type"] == "harness"
    assert row["importable"] is False
    assert row["reason_code"] == "harness-managed"
    assert row["managed_agent_id"] == imported["agent_id"]
    assert row["managed_agent_method"] == "discovered_runtime"


def test_harness_import_name_collision_never_takes_over_another_agent(client, monkeypatch):
    """A same-named launchpad agent for an UNRELATED resource keeps its row+name."""
    detail = _harness_detail("frontdesk")
    db = SessionLocal()
    namesake = Agent(
        name="frontdesk",
        method="zip_runtime",
        status="active",
        resource_id="frontdesk-abcdefghij",
        arn="arn:aws:bedrock-agentcore:us-west-2:111122223333:runtime/frontdesk-abcdefghij",
        spec={"name": "frontdesk", "method": "zip_runtime"},
    )
    db.add(namesake)
    db.commit()
    namesake_id = namesake.id
    db.close()
    _mock_control(monkeypatch, [], harness_details=[detail])

    imported = client.post(
        "/api/agents/discovery/import", json={"harness_ids": [detail["harnessId"]]}
    ).json()["imported"][0]

    assert imported["agent_id"] != namesake_id
    assert imported["agent_name"] == f"frontdesk-{detail['harnessId'][-10:]}"
    db = SessionLocal()
    assert db.get(Agent, namesake_id).name == "frontdesk"
    assert db.get(Agent, namesake_id).method == "zip_runtime"
    assert db.get(Agent, imported["agent_id"]).arn == detail["arn"]
    db.close()


def test_discovered_runtime_chat_keeps_the_buffered_runtime_path(monkeypatch):
    """The harness gate must not capture a discovered RUNTIME (no resource_type)."""
    agent = Agent(
        name="external-http",
        method="discovered_runtime",
        status="active",
        arn="arn:aws:bedrock-agentcore:us-west-2:111122223333:runtime/http-abcdefghij",
        spec={
            "protocol": "http",
            "discovery": {"aws_status": "READY", "authorizer_type": "none"},
        },
    )
    data = MagicMock()
    monkeypatch.setattr(chat_service, "data_client", lambda: data)
    monkeypatch.setattr(invoke_service, "data_client", lambda: data)
    monkeypatch.setattr(
        invoke_service.rt,
        "invoke_runtime_text",
        MagicMock(return_value={"text": "runtime answer", "session_id": "s" * 40}),
    )

    events = list(chat_service.chat_stream(agent, "hello", session_id="s" * 40))

    assert events[0]["data"]["mode"] == "buffered"
    assert "".join(
        event["data"]["text"] for event in events if event["event"] == "delta"
    ) == "runtime answer"
    data.invoke_harness.assert_not_called()


def test_imported_harness_is_offered_by_the_public_api(client):
    db = SessionLocal()
    db.add(_imported_harness_agent(name="public_harness"))
    db.commit()
    agent_id = db.query(Agent).filter(Agent.name == "public_harness").first().id
    db.close()
    key = client.post("/api/apikeys", json={"name": "harness-key"}).json()["key"]

    listed = client.get("/v1/agents", headers={"X-Api-Key": key}).json()["agents"]

    assert agent_id in {agent["id"] for agent in listed}
