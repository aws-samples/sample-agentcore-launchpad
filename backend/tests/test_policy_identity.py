"""Console identity -> Cognito Gateway policy principal."""

import base64
import json
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError
from pydantic import SecretStr

from app.core.errors import AppError
from app.services import aws_clients, policy_identity


def _jwt(username: str, groups: list[str], exp: int | None = None) -> str:
    header = base64.urlsafe_b64encode(b'{"alg":"none"}').decode().rstrip("=")
    payload = base64.urlsafe_b64encode(
        json.dumps(
            {
                "username": username,
                "cognito:groups": groups,
                "exp": exp or int(time.time()) + 3600,
            }
        ).encode()
    ).decode().rstrip("=")
    return f"{header}.{payload}.sig"


def _settings(password: str | None = "console-secret"):
    return SimpleNamespace(
        auth_password=SecretStr(password) if password else None,
        region="us-west-2",
        resources={"user_pool_id": "pool", "user_pool_client_id": "client"},
    )


@pytest.fixture(autouse=True)
def _clear_cache():
    policy_identity._token_cache.clear()
    yield
    policy_identity._token_cache.clear()


def test_open_console_keeps_m2m_without_cognito(monkeypatch):
    monkeypatch.setattr(policy_identity, "get_settings", lambda: _settings(None))
    monkeypatch.setattr(
        aws_clients,
        "client",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no AWS call")),
    )
    assert policy_identity.gateway_user_token("river", "admin") is None


def test_demo_user_uses_bootstrap_password_and_reconciles_group(monkeypatch):
    token = _jwt("demo", ["hr-analyst"])
    cognito = MagicMock()
    cognito.admin_get_user.return_value = {
        "Username": "demo",
        "UserAttributes": [{"Name": "email", "Value": "demo@launchpad.local"}],
    }
    cognito.admin_list_groups_for_user.return_value = {
        "Groups": [{"GroupName": "platform-admin"}]
    }
    cognito.initiate_auth.return_value = {
        "AuthenticationResult": {"AccessToken": token, "ExpiresIn": 3600}
    }
    monkeypatch.setattr(policy_identity, "get_settings", _settings)
    monkeypatch.setattr(
        policy_identity,
        "load_yaml_config",
        lambda: {"demo_users": {"passwords": {"demo": "bootstrap-password"}}},
    )
    monkeypatch.setattr(aws_clients, "client", lambda *a, **k: cognito)

    assert policy_identity.gateway_user_token("demo", "member") == token
    assert cognito.initiate_auth.call_args.kwargs["AuthParameters"] == {
        "USERNAME": "demo",
        "PASSWORD": "bootstrap-password",
    }
    cognito.admin_remove_user_from_group.assert_called_once_with(
        UserPoolId="pool", Username="demo", GroupName="platform-admin"
    )
    cognito.admin_add_user_to_group.assert_called_once_with(
        UserPoolId="pool", Username="demo", GroupName="hr-analyst"
    )


def test_new_console_user_creates_marked_shadow_identity(monkeypatch):
    token = _jwt("clare", ["platform-admin"])
    cognito = MagicMock()
    cognito.admin_get_user.side_effect = ClientError(
        {"Error": {"Code": "UserNotFoundException", "Message": "missing"}},
        "AdminGetUser",
    )
    cognito.admin_create_user.return_value = {
        "User": {
            "Attributes": [
                {"Name": "preferred_username", "Value": policy_identity.SHADOW_MARKER}
            ]
        }
    }
    cognito.admin_list_groups_for_user.return_value = {"Groups": []}
    cognito.initiate_auth.return_value = {
        "AuthenticationResult": {"AccessToken": token, "ExpiresIn": 3600}
    }
    monkeypatch.setattr(policy_identity, "get_settings", _settings)
    monkeypatch.setattr(policy_identity, "load_yaml_config", lambda: {})
    monkeypatch.setattr(aws_clients, "client", lambda *a, **k: cognito)

    assert (
        policy_identity.gateway_user_token("clare", "admin", "clare@example.com")
        == token
    )
    attributes = cognito.admin_create_user.call_args.kwargs["UserAttributes"]
    assert {"Name": "preferred_username", "Value": policy_identity.SHADOW_MARKER} in attributes
    password = cognito.admin_set_user_password.call_args.kwargs["Password"]
    assert password != "console-secret" and len(password) >= 12


def test_unmarked_existing_cognito_user_is_not_adopted(monkeypatch):
    cognito = MagicMock()
    cognito.admin_get_user.return_value = {
        "Username": "clare",
        "UserAttributes": [{"Name": "email", "Value": "clare@example.com"}],
    }
    monkeypatch.setattr(policy_identity, "get_settings", _settings)
    monkeypatch.setattr(policy_identity, "load_yaml_config", lambda: {})
    monkeypatch.setattr(aws_clients, "client", lambda *a, **k: cognito)

    with pytest.raises(AppError) as error:
        policy_identity.gateway_user_token("clare", "member")
    assert error.value.code == "policy.identity_conflict"
    cognito.admin_set_user_password.assert_not_called()


def test_mismatched_jwt_claims_are_rejected(monkeypatch):
    cognito = MagicMock()
    cognito.admin_get_user.return_value = {
        "Username": "demo",
        "UserAttributes": [],
    }
    cognito.admin_list_groups_for_user.return_value = {
        "Groups": [{"GroupName": "hr-analyst"}]
    }
    cognito.initiate_auth.return_value = {
        "AuthenticationResult": {
            "AccessToken": _jwt("river", ["platform-admin"]),
            "ExpiresIn": 3600,
        }
    }
    monkeypatch.setattr(policy_identity, "get_settings", _settings)
    monkeypatch.setattr(
        policy_identity,
        "load_yaml_config",
        lambda: {"demo_users": {"passwords": {"demo": "pw"}}},
    )
    monkeypatch.setattr(aws_clients, "client", lambda *a, **k: cognito)

    with pytest.raises(AppError) as error:
        policy_identity.gateway_user_token("demo", "member")
    assert error.value.code == "policy.identity_invalid"
