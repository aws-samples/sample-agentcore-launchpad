"""Bootstrap idempotency with mocked boto3 clients."""

from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError

from app.services import bootstrap as bs

REG_ARN = "arn:aws:bedrock-agentcore:us-west-2:111:registry/launchpad-registry-abc123"
MEM_ARN = "arn:aws:bedrock-agentcore:us-west-2:111:memory/launchpad_memory-xyz789"


def make_control(registries=(), memories=()):
    control = MagicMock()
    control.list_registries.return_value = {"registries": list(registries)}
    control.list_memories.return_value = {"memories": list(memories)}
    control.create_registry.return_value = {"registryArn": REG_ARN}
    control.create_memory.return_value = {
        "memory": {"id": "launchpad_memory-xyz789", "arn": MEM_ARN}
    }
    control.get_memory.return_value = {"memory": {"status": "ACTIVE"}}
    return control


def test_ensure_registry_creates_when_missing():
    control = make_control()
    result, created = bs.ensure_registry(control)
    assert created is True
    assert result == {"id": "launchpad-registry-abc123", "arn": REG_ARN}
    control.create_registry.assert_called_once()


def test_ensure_registry_reuses_existing():
    existing = {
        "name": bs.REGISTRY_NAME,
        "registryId": "launchpad-registry-abc123",
        "registryArn": REG_ARN,
    }
    control = make_control(registries=[existing])
    result, created = bs.ensure_registry(control)
    assert created is False
    assert result["id"] == "launchpad-registry-abc123"
    control.create_registry.assert_not_called()


def test_ensure_registry_degrades_on_access_denied():
    control = make_control()
    control.list_registries.side_effect = ClientError(
        {
            "Error": {
                "Code": "AccessDeniedException",
                "Message": "blocked by account policy",
            }
        },
        "ListRegistries",
    )

    result, created = bs.ensure_registry(control)

    assert result is None
    assert created is False
    control.create_registry.assert_not_called()


def test_ensure_registry_degrades_when_create_is_denied():
    control = make_control()
    control.create_registry.side_effect = ClientError(
        {
            "Error": {
                "Code": "AccessDenied",
                "Message": "create blocked by account policy",
            }
        },
        "CreateRegistry",
    )

    result, created = bs.ensure_registry(control)

    assert result is None
    assert created is False


def test_ensure_registry_does_not_hide_other_client_errors():
    control = make_control()
    control.list_registries.side_effect = ClientError(
        {"Error": {"Code": "ThrottlingException", "Message": "slow down"}},
        "ListRegistries",
    )

    with pytest.raises(ClientError):
        bs.ensure_registry(control)


def test_run_bootstrap_continues_and_clears_registry_ids(monkeypatch):
    outputs = {
        "ArtifactsBucketName": "bucket",
        "EcrRepoName": "repo",
        "EcrRepoUri": "account.dkr.ecr.us-west-2.amazonaws.com/repo",
        "CodeBuildProjectName": "build",
        "UserPoolId": "pool",
        "UserPoolClientId": "client",
        "AgentExecutionRoleArn": "arn:aws:iam::111:role/exec",
    }
    control = MagicMock()
    cognito = MagicMock()
    sts = MagicMock()
    sts.get_caller_identity.return_value = {"Account": "111"}
    clients = {
        "bedrock-agentcore-control": control,
        "cognito-idp": cognito,
        "sts": sts,
    }
    written: list[dict] = []

    monkeypatch.setattr(bs, "get_stack_outputs", lambda _region: outputs)
    monkeypatch.setattr(bs, "_client", lambda service, _region: clients[service])
    monkeypatch.setattr(bs, "ensure_registry", lambda _control: (None, False))
    monkeypatch.setattr(
        bs,
        "ensure_memory",
        lambda _control, execution_role_arn=None: (
            {"id": "memory-id", "arn": "arn:aws:bedrock-agentcore:memory"},
            False,
        ),
    )
    monkeypatch.setattr(bs, "load_config", lambda: {})
    monkeypatch.setattr(
        bs, "ensure_demo_user_passwords", lambda *_args: ({"demo": "known"}, False)
    )
    monkeypatch.setattr(bs, "write_config", lambda update: written.append(update) or update)

    summary = bs.run_bootstrap("us-west-2")

    assert summary["registry"] == {
        "available": False,
        "id": "",
        "arn": "",
        "created": False,
        "reason": bs.REGISTRY_ACCESS_DENIED_REASON,
    }
    resources = written[0]["resources"]
    assert resources["registry_id"] == ""
    assert resources["registry_arn"] == ""
    assert resources["registry_unavailable_reason"] == bs.REGISTRY_ACCESS_DENIED_REASON
    assert resources["memory_id"] == "memory-id"


def test_ensure_memory_creates_when_missing():
    control = make_control()
    result, created = bs.ensure_memory(control, execution_role_arn="arn:aws:iam::111:role/x")
    assert created is True
    assert result["id"] == "launchpad_memory-xyz789"
    kwargs = control.create_memory.call_args.kwargs
    strategy_kinds = {next(iter(s)) for s in kwargs["memoryStrategies"]}
    assert strategy_kinds == {"semanticMemoryStrategy", "userPreferenceMemoryStrategy"}
    assert kwargs["memoryExecutionRoleArn"] == "arn:aws:iam::111:role/x"


def test_ensure_memory_reuses_existing():
    existing = {"id": "launchpad_memory-xyz789", "arn": MEM_ARN}
    control = make_control(memories=[existing])
    result, created = bs.ensure_memory(control)
    assert created is False
    assert result["arn"] == MEM_ARN
    control.create_memory.assert_not_called()


def test_merge_config_deep_merges():
    base = {"region": "us-west-2", "resources": {"a": 1, "b": 2}, "keep": True}
    update = {"resources": {"b": 3, "c": 4}, "region": "us-west-2"}
    merged = bs.merge_config(base, update)
    assert merged == {
        "region": "us-west-2",
        "resources": {"a": 1, "b": 3, "c": 4},
        "keep": True,
    }
    # base untouched
    assert base["resources"] == {"a": 1, "b": 2}


def test_demo_passwords_only_set_when_needed():
    cognito = MagicMock()
    cognito.admin_get_user.side_effect = [
        {"UserStatus": "CONFIRMED"},
        {"UserStatus": "FORCE_CHANGE_PASSWORD"},
    ]
    passwords, changed = bs.ensure_demo_user_passwords(
        cognito, "pool-1", existing={"river": "Known1234567890"}
    )
    assert changed is True
    assert passwords["river"] == "Known1234567890"
    assert len(passwords["demo"]) >= 12
    cognito.admin_set_user_password.assert_called_once()
