"""Bootstrap idempotency with mocked boto3 clients."""

from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError

from app.services import bootstrap as bs

REG_ARN = "arn:aws:agent-registry:us-west-2:111:registry/launchpad-registry-abc123"
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
        "agent-registry-control": control,
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
    monkeypatch.setattr(
        bs,
        "write_config",
        lambda update, replace=None: written.append(update | (replace or {})) or update,
    )

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


def _user_not_found() -> ClientError:
    return ClientError(
        {"Error": {"Code": "UserNotFoundException", "Message": "missing"}},
        "AdminGetUser",
    )


def make_cognito(users: dict[str, dict]) -> MagicMock:
    """Cognito stub backed by a {username: AdminGetUser response} map."""
    cognito = MagicMock()

    def get_user(UserPoolId: str, Username: str) -> dict:
        if Username not in users:
            raise _user_not_found()
        return users[Username]

    cognito.admin_get_user.side_effect = get_user
    return cognito


def test_write_config_replace_drops_stale_keys(monkeypatch, tmp_path):
    """A deep merge would resurrect a deleted demo user's password from the file."""
    config_file = tmp_path / "launchpad.yaml"
    config_file.write_text(
        "demo_users:\n  passwords:\n    river: stale\n    demo: kept\nregion: us-west-2\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(bs, "CONFIG_FILE", config_file)

    merged = bs.write_config(
        {"resources": {"a": "1"}},
        replace={"demo_users": {"passwords": {"admin": "new", "demo": "kept"}}},
    )

    assert merged["demo_users"]["passwords"] == {"admin": "new", "demo": "kept"}
    assert merged["region"] == "us-west-2"
    assert "river" not in config_file.read_text(encoding="utf-8")


def test_demo_passwords_only_set_when_needed():
    cognito = make_cognito(
        {
            "admin": {"UserStatus": "CONFIRMED"},
            "demo": {"UserStatus": "FORCE_CHANGE_PASSWORD"},
        }
    )
    passwords, changed = bs.ensure_demo_user_passwords(
        cognito, "pool-1", existing={"admin": "Known1234567890"}
    )
    assert changed is True
    assert passwords["admin"] == "Known1234567890"
    assert len(passwords["demo"]) >= 12
    cognito.admin_set_user_password.assert_called_once()
    cognito.admin_create_user.assert_not_called()
    cognito.admin_delete_user.assert_not_called()


def test_missing_demo_user_is_created_with_group():
    """Prewarmed-account path: CDK is skipped, so bootstrap must create users."""
    cognito = make_cognito({"demo": {"UserStatus": "CONFIRMED"}})

    passwords, changed = bs.ensure_demo_user_passwords(
        cognito, "pool-1", existing={"demo": "KnownDemo1234567"}
    )

    assert changed is True
    create = cognito.admin_create_user.call_args.kwargs
    assert create["Username"] == "admin"
    assert create["MessageAction"] == "SUPPRESS"
    assert {"Name": "email", "Value": "admin@launchpad.local"} in create["UserAttributes"]
    assert {"Name": "email_verified", "Value": "true"} in create["UserAttributes"]
    cognito.admin_add_user_to_group.assert_called_once_with(
        UserPoolId="pool-1", Username="admin", GroupName="platform-admin"
    )
    set_pw = cognito.admin_set_user_password.call_args.kwargs
    assert set_pw["Username"] == "admin" and set_pw["Permanent"] is True
    assert len(passwords["admin"]) >= 12
    assert passwords["demo"] == "KnownDemo1234567"


def test_legacy_river_demo_user_is_deleted_and_dropped_from_passwords():
    cognito = make_cognito(
        {
            "admin": {"UserStatus": "CONFIRMED"},
            "demo": {"UserStatus": "CONFIRMED"},
            "river": {
                "UserStatus": "CONFIRMED",
                "UserAttributes": [{"Name": "email", "Value": "river@launchpad.local"}],
            },
        }
    )

    passwords, changed = bs.ensure_demo_user_passwords(
        cognito,
        "pool-1",
        existing={
            "admin": "KnownAdmin123456",
            "demo": "KnownDemo1234567",
            "river": "StaleRiver123456",
        },
    )

    cognito.admin_delete_user.assert_called_once_with(UserPoolId="pool-1", Username="river")
    assert "river" not in passwords
    assert passwords == {"admin": "KnownAdmin123456", "demo": "KnownDemo1234567"}
    assert changed is True  # the stale key must drop out of the config


def test_shadow_bridge_user_named_river_is_never_deleted():
    """A console-identity shadow user owning a legacy name must survive cleanup."""
    from app.services.policy_identity import SHADOW_MARKER

    cognito = make_cognito(
        {
            "admin": {"UserStatus": "CONFIRMED"},
            "demo": {"UserStatus": "CONFIRMED"},
            "river": {
                "UserStatus": "CONFIRMED",
                "UserAttributes": [{"Name": "preferred_username", "Value": SHADOW_MARKER}],
            },
        }
    )

    passwords, changed = bs.ensure_demo_user_passwords(
        cognito,
        "pool-1",
        existing={"admin": "KnownAdmin123456", "demo": "KnownDemo1234567"},
    )

    cognito.admin_delete_user.assert_not_called()
    assert changed is False
    assert set(passwords) == {"admin", "demo"}


def test_legacy_cleanup_skips_absent_user():
    cognito = make_cognito(
        {"admin": {"UserStatus": "CONFIRMED"}, "demo": {"UserStatus": "CONFIRMED"}}
    )

    _passwords, changed = bs.ensure_demo_user_passwords(
        cognito,
        "pool-1",
        existing={"admin": "KnownAdmin123456", "demo": "KnownDemo1234567"},
    )

    cognito.admin_delete_user.assert_not_called()
    assert changed is False
