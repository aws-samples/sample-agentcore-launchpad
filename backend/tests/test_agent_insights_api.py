from unittest.mock import Mock

import pytest
from botocore.loaders import Loader
from sqlalchemy.exc import IntegrityError

from app.core.db import SessionLocal
from app.core.project_scope import project_context
from app.evaluation import insight_routers as api
from app.evaluation.models import OnlineEvalConfig
from app.models.ledger import Agent, Workspace
from app.routers.workspaces import WorkspaceScope
from app.services.visibility import Viewer
from app.services.workspace import WorkspaceContext
from tests.conftest import TEST_PROJECT, client_error

LIST = "/api/eval/agents/visible/findings"
SERVICE = "harness_MyAgent.live"


def _agent(id_, resource, project=TEST_PROJECT, creator=None):
    return Agent(
        id=id_, name=id_, method="harness", status="active", resource_id=resource,
        workspace_id="default", project_id=project, created_by=creator, endpoint_mode="live",
    )


@pytest.fixture
def reader(client, monkeypatch):
    with SessionLocal() as db:
        db.add(_agent("visible", "MyAgent-aaaaaaaaaa"))
        db.add(_agent("second", "Second-bbbbbbbbbb"))
        db.commit()
    logs = Mock()
    logs.describe_log_groups.side_effect = lambda **kw: {
        "logGroups": [
            {"logGroupName": kw["logGroupNamePrefix"] + "runtime-DEFAULT"},
            {"logGroupName": kw["logGroupNamePrefix"] + "runtime-live"},
        ],
    }

    def workspace_client(_ctx, service):
        assert service == "logs"
        return logs

    monkeypatch.setattr(WorkspaceContext, "client", workspace_client)
    aws = Mock()
    monkeypatch.setattr(api, "api_data_client", lambda _: aws)
    return aws


@pytest.mark.parametrize(
    ("store", "expected"),
    [
        ({"status": "ACTIVE", "enablementStatus": "ENABLED"}, "ready"),
        ({"status": "INACTIVE", "enablementStatus": "DISABLED"}, "disabled"),
        ({"status": "UPDATING"}, "updating"),
        ({"status": "ERROR", "failureReason": "sensitive"}, "error"),
        (client_error("AccessDeniedException"), "denied"),
        (client_error("ThrottlingException"), "unavailable"),
        (client_error("ResourceNotFoundException"), "unsupported"),
        (None, "unsupported"),
    ],
)
def test_store_state(client, monkeypatch, store, expected):
    control = Mock() if store is not None else object()
    if isinstance(store, dict):
        control.get_agent_insight_store.return_value = store
    elif isinstance(store, Exception):
        control.get_agent_insight_store.side_effect = store
    monkeypatch.setattr(api, "control_client", lambda _: control)
    response = client.get("/api/eval/insight-store")
    assert response.status_code == 200
    assert response.json() == {"state": expected}
    if store is not None:
        control.get_agent_insight_store.assert_called_once_with(
            agentInsightStoreId="default-insight-store"
        )
        assert len(control.mock_calls) == 1


def test_list_filters_batch_and_fences_cursor(client, reader):
    reader.list_agent_insights.return_value = {
        "agentInsights": [
            {"agentInsightId": "one", "applicationName": SERVICE, "name": "Same name"},
            {"agentInsightId": "two", "applicationName": SERVICE, "name": "Same name"},
            {"agentInsightId": "wrong", "applicationName": "foreign.DEFAULT"},
            {"agentInsightId": "batch", "applicationName": SERVICE, "batchEvaluationId": "batch"},
            {"agentInsightId": "kms", "applicationName": SERVICE, "name": "unavailable",
             "customerMetadataStatus": "CUSTOMER_KEY_UNAVAILABLE"},
        ],
        "nextToken": "aws-page-two",
    }
    response = client.get(LIST)
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["application_name"] == SERVICE
    assert [item["agentInsightId"] for item in payload["items"]] == ["one", "two", "kms"]
    assert "name" not in payload["items"][-1]
    reader.list_agent_insights.assert_called_once_with(
        applicationName=SERVICE, archiveStatus="ACTIVE", maxResults=25,
    )
    cursor = payload["next_cursor"]
    assert client.get(
        LIST.replace("visible", "second"), params={"cursor": cursor},
    ).status_code == 400
    assert client.get(LIST, params={"cursor": "modified"}).status_code == 400
    assert reader.list_agent_insights.call_count == 1
    assert client.get(LIST, params={"cursor": cursor}).status_code == 200
    reader.list_agent_insights.assert_called_with(
        applicationName=SERVICE, archiveStatus="ACTIVE", maxResults=25, nextToken="aws-page-two",
    )


def test_detail_requires_exact_service_and_excludes_batches(client, reader):
    item = {
        "agentInsightId": "one", "applicationName": SERVICE,
        "customerMetadataStatus": "AVAILABLE",
        "rootCauses": [{"sampleSessions": [{"sessionId": "session1"}]}],
    }
    reader.get_agent_insight.return_value = item
    response = client.get(f"{LIST}/one")
    assert response.status_code == 200
    assert response.json()["rootCauses"] == item["rootCauses"]
    reader.get_agent_insight.assert_called_once_with(
        applicationName=SERVICE, agentInsightId="one",
    )
    for wrong in (
        {"agentInsightId": "another"},
        {"applicationName": "foreign.DEFAULT"},
        {"batchEvaluationId": "batch"},
    ):
        reader.get_agent_insight.return_value = {**item, **wrong}
        assert client.get(f"{LIST}/one").status_code == 404
    reader.get_agent_insight.return_value = {**item, "customerMetadataStatus": "UNAVAILABLE"}
    response = client.get(f"{LIST}/one")
    assert response.status_code == 200
    assert "rootCauses" not in response.json()
    assert response.json()["customerMetadataStatus"] == "UNAVAILABLE"


def test_other_project_or_predecessor_blocks_combined_scope(client, reader):
    with SessionLocal() as db:
        db.add(_agent("foreign", "Foreign-cccccccccc", project="another"))
        db.commit()
    assert client.get(LIST.replace("visible", "foreign")).status_code == 404
    with SessionLocal() as db:
        old = _agent("old", "MyAgent-cccccccccc", project="another")
        old.status = "deleted"
        db.add(old)
        db.commit()
    assert client.get(LIST).status_code == 404
    reader.list_agent_insights.assert_not_called()


def test_same_aws_insight_store_cannot_belong_to_two_workspaces():
    with SessionLocal() as db:
        default = db.get(Workspace, "default")
        db.add(Workspace(id="collision", name="Collision", account_id=default.account_id,
                         region=default.region))
        with pytest.raises(IntegrityError, match="workspaces.account_id, workspaces.region"):
            db.flush()


def test_foreign_project_config_blocks_list_and_detail(client, reader):
    with SessionLocal() as db:
        db.add(OnlineEvalConfig(
            workspace_id="default", project_id="another", agent_id="visible",
            agent_name="visible", config_id="foreign1", config_arn="arn:foreign1",
            name="former-config", service_name=SERVICE, log_group="ignored",
        ))
        db.commit()
    assert client.get(LIST).status_code == 404
    assert client.get(f"{LIST}/one").status_code == 404
    reader.list_agent_insights.assert_not_called()
    reader.get_agent_insight.assert_not_called()


def test_hidden_online_config_blocks_aggregate_for_member(reader, workspace):
    with SessionLocal() as db, project_context(TEST_PROJECT):
        agent = db.get(Agent, "visible")
        db.add(OnlineEvalConfig(
            workspace_id="default", project_id=TEST_PROJECT, agent_id=agent.id,
            agent_name=agent.name, config_id="conf1", config_arn="arn:conf1", name="test",
            service_name=SERVICE, log_group="ignored", created_by="another-user",
        ))
        db.commit()
        ws = WorkspaceScope("default", db.get(Workspace, "default"), workspace)
        with pytest.raises(api.NotFoundError):
            api.list_agent_findings("visible", None, db, ws,
                                    Viewer("caller", True, TEST_PROJECT))
        reader.list_agent_insights.assert_not_called()


def test_sdk_models_support_insight_reads():
    loader = Loader()
    data = loader.load_service_model("bedrock-agentcore", "service-2")
    control = loader.load_service_model("bedrock-agentcore-control", "service-2")
    assert {"ListAgentInsights", "GetAgentInsight"} <= data["operations"].keys()
    assert "GetAgentInsightStore" in control["operations"]


def test_several_visible_configs_share_same_runtime_application(client, reader, monkeypatch):
    with SessionLocal() as db:
        agent = db.get(Agent, "visible")
        agent.method = "zip_runtime"
        agent.endpoint_mode = "default"
        for suffix in ("1", "2"):
            db.add(OnlineEvalConfig(
                workspace_id="default", project_id=TEST_PROJECT, agent_id=agent.id,
                agent_name=agent.name, config_id=f"conf{suffix}", config_arn=f"arn:conf{suffix}",
                name=f"test{suffix}", service_name="MyAgent.DEFAULT", log_group="ignored",
            ))
        db.commit()
    control = Mock()
    control.get_agent_runtime.return_value = {"agentRuntimeName": "MyAgent"}
    monkeypatch.setattr("app.evaluation.service.control_client", lambda _: control)
    reader.list_agent_insights.return_value = {"agentInsights": []}
    response = client.get(LIST)
    assert response.status_code == 200, response.text
    assert response.json() == {
        "application_name": "MyAgent.DEFAULT", "items": [], "next_cursor": None,
    }
    control.get_agent_runtime.assert_called_once_with(agentRuntimeId="MyAgent-aaaaaaaaaa")
    reader.list_agent_insights.assert_called_once_with(
        applicationName="MyAgent.DEFAULT", archiveStatus="ACTIVE", maxResults=25,
    )


def test_shared_platform_agent_does_not_disclose_cross_project_aggregate(client, reader):
    with SessionLocal() as db:
        shared = _agent("shared", "Shared-cccccccccc", project=None)
        shared.system_key = "test-preset"
        db.add(shared)
        db.commit()
    assert client.get(LIST.replace("visible", "shared")).status_code == 404
    reader.list_agent_insights.assert_not_called()
