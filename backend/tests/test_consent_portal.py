"""The workspace gateway's Consent Portal and as_user (AUTHORIZATION_CODE)
Gateway targets: wrapper shapes, validation before any AWS write, routes."""

from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError

import app.routers.identity as identity_router
from app.core.errors import AppError, NotFoundError
from app.services import consent_portals as cp
from app.services import identity_gateway_targets as gt
from app.services.agentcore import consent_portal as portal_api

VAULT = "arn:aws:bedrock-agentcore:us-west-2:111:token-vault/default"
OAUTH_ARN = f"{VAULT}/oauth2credentialprovider/team-idp"
RESOURCES = {"gateway_id": "gw-1"}
ACCOUNT = "111122223333"
ROLE = f"arn:aws:iam::{ACCOUNT}:role/portal-exec"
PORTAL_URL = "https://gw.consent-portal.bedrock-agentcore.us-west-2.amazonaws.com"


def _portal(status="ACTIVE", gateway="gw-1", portal_id="cp-1"):
    return {
        "consentPortalId": portal_id,
        "consentPortalArn": f"arn:aws:bedrock-agentcore:us-west-2:111:consent-portal/{portal_id}",
        "name": "team-portal",
        "status": status,
        "portalUrl": PORTAL_URL,
        "executionRoleArn": ROLE,
        "idpConfig": {"credentialProviderArn": OAUTH_ARN, "scopes": ["openid"]},
        "sources": [{"type": "agentcore-gateway", "identifier": gateway}],
    }


def make_control(portals=()):
    control = MagicMock()
    by_id = {p["consentPortalId"]: p for p in portals}
    control.list_consent_portals.return_value = {
        "consentPortals": [
            {k: v for k, v in p.items() if k != "idpConfig"} for p in portals
        ]
    }
    control.get_consent_portal.side_effect = lambda **kw: by_id[kw["consentPortalIdentifier"]]
    control.get_oauth2_credential_provider.return_value = {"credentialProviderArn": OAUTH_ARN}
    control.create_consent_portal.return_value = _portal(status="CREATING")
    control.list_gateway_targets.return_value = {"items": []}
    control.create_gateway_target.return_value = {"targetId": "NEW1", "status": "CREATING"}
    return control


def _create_portal(control, **over):
    kwargs = {"name": "team-portal", "connection": "team-idp", "scopes": ["openid"],
              "execution_role_arn": ROLE, "account_id": ACCOUNT, "region": "us-west-2"} | over
    return cp.create(control, RESOURCES, **kwargs)


# ── wrappers ─────────────────────────────────────────────────────────────────


def test_portal_for_gateway_matches_the_source_and_reads_it_back_in_full():
    control = make_control([_portal(gateway="other", portal_id="cp-0"), _portal()])
    found = portal_api.portal_for_gateway(control, "gw-1")
    assert found["consentPortalId"] == "cp-1"
    assert found["idpConfig"]["credentialProviderArn"] == OAUTH_ARN
    control.get_consent_portal.assert_called_once_with(consentPortalIdentifier="cp-1")
    assert portal_api.portal_for_gateway(control, "gw-none") is None


def test_list_portals_pages():
    control = MagicMock()
    control.list_consent_portals.side_effect = [
        {"consentPortals": [_portal(portal_id="a")], "nextToken": "t"},
        {"consentPortals": [_portal(portal_id="b")]},
    ]
    assert [p["consentPortalId"] for p in portal_api.list_portals(control)] == ["a", "b"]
    assert control.list_consent_portals.call_args_list[1].kwargs["nextToken"] == "t"


def test_callback_urls():
    assert portal_api.callback_urls(PORTAL_URL + "/") == {
        "idp_callback": f"{PORTAL_URL}/callback",
        "target_return": f"{PORTAL_URL}/connect/callback",
    }


# ── service ──────────────────────────────────────────────────────────────────


def test_create_request_shape_and_view():
    control = make_control()
    out = _create_portal(control, audience="aud-1", description="d")
    control.create_consent_portal.assert_called_once_with(
        name="team-portal",
        executionRoleArn=ROLE,
        idpConfig={"credentialProviderArn": OAUTH_ARN, "scopes": ["openid"], "audience": "aud-1"},
        sources=[{"identifier": "gw-1", "type": "agentcore-gateway"}],
        description="d",
    )
    portal = out["portal"]
    assert portal["status"] == "CREATING" and portal["connection"] == "team-idp"
    # the URL is not meaningful until ACTIVE
    assert portal["portal_url"] is None and portal["callbacks"] is None


@pytest.mark.parametrize(
    ("over", "code"),
    [
        ({"name": "bad name"}, "identity.invalid_portal_name"),
        ({"name": "x" * 51}, "identity.invalid_portal_name"),
        ({"execution_role_arn": "arn:aws:iam::111122223333:user/u"}, "identity.invalid_role_arn"),
        ({"scopes": ["profile"]}, "identity.portal_scopes"),
    ],
)
def test_create_refuses_bad_input_without_calling_aws(over, code):
    control = make_control()
    with pytest.raises(AppError) as err:
        _create_portal(control, **over)
    assert err.value.code == code and err.value.status_code == 422
    control.create_consent_portal.assert_not_called()
    control.list_consent_portals.assert_not_called()


@pytest.mark.parametrize(
    "over",
    [
        # another account's role: the backend principal must not pass it
        {"execution_role_arn": "arn:aws:iam::444455556666:role/portal-exec"},
        # the right account id in the wrong partition
        {"execution_role_arn": f"arn:aws-cn:iam::{ACCOUNT}:role/portal-exec"},
        # a workspace whose account is not known yet cannot vouch for any role
        {"account_id": ""},
    ],
    ids=["cross-account", "wrong-partition", "unknown-account"],
)
def test_create_refuses_a_role_outside_the_workspace_account(over):
    control = make_control()
    with pytest.raises(AppError) as err:
        _create_portal(control, **over)
    assert (err.value.code, err.value.status_code) == ("identity.role_account_mismatch", 422)
    control.create_consent_portal.assert_not_called()
    control.list_consent_portals.assert_not_called()


def test_create_accepts_a_same_account_role_in_the_region_partition():
    control = make_control()
    _create_portal(control)
    assert control.create_consent_portal.call_args.kwargs["executionRoleArn"] == ROLE
    cn_role = f"arn:aws-cn:iam::{ACCOUNT}:role/portal-exec"
    _create_portal(make_control(), execution_role_arn=cn_role, region="cn-north-1")


@pytest.mark.parametrize(("region", "partition"), [
    ("us-west-2", "aws"), ("cn-northwest-1", "aws-cn"), ("us-gov-west-1", "aws-us-gov"),
])
def test_partition_for_region(region, partition):
    assert cp.partition_for_region(region) == partition


def test_create_refuses_a_second_portal_and_maps_conflict():
    with pytest.raises(AppError) as err:
        _create_portal(make_control([_portal()]))
    assert err.value.code == "identity.consent_portal_exists"
    control = make_control()
    control.create_consent_portal.side_effect = ClientError(
        {"Error": {"Code": "ConflictException", "Message": "x"}}, "CreateConsentPortal"
    )
    with pytest.raises(AppError) as err:
        _create_portal(control)
    assert err.value.code == "identity.consent_portal_exists" and err.value.status_code == 409


def test_no_gateway_is_409_and_status_is_empty():
    with pytest.raises(AppError) as err:
        cp.create(make_control(), {}, name="p", connection="c", scopes=["openid"],
                  execution_role_arn=ROLE, account_id=ACCOUNT, region="us-west-2")
    assert err.value.code == "identity.gateway_missing" and err.value.status_code == 409
    assert cp.status(make_control(), {}) == {"gateway_id": None, "portal": None}


def test_active_return_url_needs_an_active_portal():
    assert cp.active_return_url(make_control([_portal()]), RESOURCES) == (
        f"{PORTAL_URL}/connect/callback"
    )
    for control in (make_control(), make_control([_portal(status="CREATING")])):
        with pytest.raises(AppError) as err:
            cp.active_return_url(control, RESOURCES)
        assert err.value.code == "identity.consent_portal_required"


def test_delete():
    control = make_control([_portal()])
    cp.delete(control, RESOURCES)
    control.delete_consent_portal.assert_called_once_with(consentPortalIdentifier="cp-1")
    with pytest.raises(NotFoundError):
        cp.delete(make_control(), RESOURCES)


# ── as_user Gateway target ───────────────────────────────────────────────────

OPENAPI = """
openapi: 3.0.1
info: {title: crm, version: "1"}
servers: [{url: "https://crm.example"}]
paths:
  /me:
    get: {operationId: me, responses: {"200": {description: ok}}}
"""


def _create_target(control, **over):
    kwargs = {
        "name": "crm-me", "description": None, "source": "openapi",
        "openapi_schema": OPENAPI, "mcp_endpoint": None, "connection": "team-idp",
        "kind": "oauth2", "mode": "as_user", "scopes": ["crm/read"],
    } | over
    return gt.create_target(control, RESOURCES, **kwargs)


def test_as_user_target_uses_authorization_code_and_the_portal_return_url():
    control = make_control([_portal()])
    _create_target(control)
    oauth = control.create_gateway_target.call_args.kwargs[
        "credentialProviderConfigurations"
    ][0]["credentialProvider"]["oauthCredentialProvider"]
    assert oauth["grantType"] == "AUTHORIZATION_CODE"
    assert oauth["defaultReturnUrl"] == f"{PORTAL_URL}/connect/callback"
    assert oauth["providerArn"] == OAUTH_ARN


def test_as_user_target_without_an_active_portal_writes_nothing():
    control = make_control()
    with pytest.raises(AppError) as err:
        _create_target(control)
    assert err.value.code == "identity.consent_portal_required"
    control.create_gateway_target.assert_not_called()


def test_as_user_target_refuses_an_api_key_connection():
    control = make_control([_portal()])
    with pytest.raises(AppError) as err:
        _create_target(control, kind="api_key", scopes=[])
    assert err.value.code == "identity.target_auth_unsupported"
    control.create_gateway_target.assert_not_called()


# ── routes ───────────────────────────────────────────────────────────────────


@pytest.fixture
def control(monkeypatch):
    stub = make_control()
    monkeypatch.setattr(identity_router, "control_client", lambda _ctx: stub)
    monkeypatch.setattr(identity_router, "_resources", lambda _ws: RESOURCES)
    return stub


def test_routes_create_read_delete(client, control):
    response = client.post("/api/identity/consent-portal", json={
        "name": "team-portal", "connection": "team-idp", "execution_role_arn": ROLE,
    })
    assert response.status_code == 202, response.text
    assert response.json()["portal"]["status"] == "CREATING"
    assert control.create_consent_portal.call_args.kwargs["idpConfig"]["scopes"] == ["openid"]

    active = _portal()
    control.list_consent_portals.return_value = {"consentPortals": [active]}
    control.get_consent_portal.side_effect = None
    control.get_consent_portal.return_value = active
    read = client.get("/api/identity/consent-portal").json()
    assert read["portal"]["portal_url"] == PORTAL_URL
    assert read["portal"]["callbacks"]["idp_callback"] == f"{PORTAL_URL}/callback"

    assert client.delete("/api/identity/consent-portal").status_code == 200
    control.delete_consent_portal.assert_called_once()


def test_route_refuses_a_cross_account_role(client, control):
    response = client.post("/api/identity/consent-portal", json={
        "name": "team-portal", "connection": "team-idp",
        "execution_role_arn": "arn:aws:iam::444455556666:role/portal-exec",
    })
    assert response.status_code == 422
    assert response.json()["code"] == "identity.role_account_mismatch"
    control.create_consent_portal.assert_not_called()


def test_route_validation_error_envelope(client, control):
    response = client.post("/api/identity/consent-portal", json={
        "name": "p", "connection": "team-idp", "execution_role_arn": ROLE,
        "scopes": ["profile"],
    })
    assert response.status_code == 422
    assert response.json()["code"] == "identity.portal_scopes"
