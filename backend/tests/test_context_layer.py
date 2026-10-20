"""COA context layer: discovery, connect (create vs adopt), status, disconnect.

Hermetic: every AWS client is a fake from ``coa_fakes`` (keyed on service +
region), and nothing reaches COA.
"""

import json

import pytest

from app.core.db import SessionLocal
from app.core.errors import AppError
from app.models.ledger import Agent, IdentityProvider
from app.models.ledger import ContextLayer as ContextLayerRow
from app.services import context_layer, identity_providers
from app.services.workspace import context_for_workspace

from . import coa_fakes as fx

RETURN_URL = "http://localhost:5173/auth/return"


@pytest.fixture
def account(monkeypatch):
    return fx.install(monkeypatch)


def _ws():
    return context_for_workspace("default")


def _connect(connection=None, **overrides):
    db = SessionLocal()
    try:
        return context_layer.connect(
            db, _ws(), prefix=overrides.get("prefix", fx.PREFIX), env=fx.ENV,
            region=overrides.get("region", fx.COA_REGION), connection=connection,
            return_url=RETURN_URL,
        )
    finally:
        db.close()


def _agent(spec: dict, method: str = "harness", status: str = "active") -> str:
    db = SessionLocal()
    try:
        row = Agent(workspace_id="default", name=spec.get("name", "coa-agent"), method=method,
                    status=status, spec=spec)
        db.add(row)
        db.commit()
        return row.id
    finally:
        db.close()


# ── discovery ────────────────────────────────────────────────────────────────


def test_discovery_reads_coa_metadata_in_the_coa_region(account):
    resources = context_layer.discover(_ws(), prefix=fx.PREFIX, env=fx.ENV, region=fx.COA_REGION)
    assert resources == {
        "mcp_runtime_arn": fx.MCP_ARN,
        "serve_runtime_arn": fx.SERVE_ARN,
        "user_pool_id": fx.POOL,
        "issuer": fx.ISSUER,
        "agent_client_id": fx.AGENT_CLIENT,
        "web_client_id": fx.WEB_CLIENT,
        "api_endpoint": fx.API_ENDPOINT,
        # a CDK-prefixed output key still resolves
        "console_url": fx.CONSOLE_URL,
        "hosted_ui_domain": fx.HOSTED_UI,
    }
    # the region override: every metadata read targets the COA region
    for service in ("ssm", "cloudformation", "bedrock-agentcore-control"):
        assert account.regions_of(service) == {fx.COA_REGION}
    assert [kw["agentRuntimeId"] for kw in account.control.ops("get_agent_runtime")] == [
        "coa_w2_dev_mcp_server-1cRyvG5BcK", "coa_w2_dev_context_manager-Z8HDyj4suT",
    ]


def test_missing_parameters_are_refused_by_name(account):
    account.ssm.params.pop(f"/{fx.PREFIX}/issuer")
    with pytest.raises(AppError) as exc:
        context_layer.discover(_ws(), prefix=fx.PREFIX, env=fx.ENV, region=fx.COA_REGION)
    assert exc.value.code == "context_layer.not_found"
    assert exc.value.detail == {"missing": [f"/{fx.PREFIX}/issuer"]}


def test_agent_oauth_off_is_refused(monkeypatch):
    fx.install(monkeypatch, fx.Account(agent_oauth=False))
    with pytest.raises(AppError) as exc:
        context_layer.discover(_ws(), prefix=fx.PREFIX, env=fx.ENV, region=fx.COA_REGION)
    assert exc.value.code == "context_layer.agent_oauth_disabled"
    assert exc.value.status_code == 409


def test_a_deployment_in_another_account_is_refused(account):
    other = fx.MCP_ARN.replace(fx.ACCOUNT, "999988887777")
    account.ssm.params[f"/{fx.PREFIX}/mcp/runtime-arn"] = other
    with pytest.raises(AppError) as exc:
        context_layer.discover(_ws(), prefix=fx.PREFIX, env=fx.ENV, region=fx.COA_REGION)
    assert exc.value.code == "context_layer.cross_account"
    assert exc.value.detail["workspace_account"] == fx.ACCOUNT


def test_a_runtime_that_is_not_ready_is_refused(account):
    account.control.runtime_status["coa_w2_dev_context_manager-Z8HDyj4suT"] = "UPDATING"
    with pytest.raises(AppError) as exc:
        context_layer.discover(_ws(), prefix=fx.PREFIX, env=fx.ENV, region=fx.COA_REGION)
    assert exc.value.code == "context_layer.runtime_not_ready"
    assert exc.value.detail == {"runtime": "serve", "status": "UPDATING"}


def test_a_missing_stack_is_refused(account):
    account.cfn.outputs.pop(f"{fx.PREFIX}-{fx.ENV}-web")
    with pytest.raises(AppError) as exc:
        context_layer.discover(_ws(), prefix=fx.PREFIX, env=fx.ENV, region=fx.COA_REGION)
    assert exc.value.code == "context_layer.not_found"
    assert exc.value.detail == {"stack": f"{fx.PREFIX}-{fx.ENV}-web"}


def test_a_non_https_endpoint_is_refused(account):
    account.cfn.outputs[f"{fx.PREFIX}-{fx.ENV}-api"]["ApiEndpoint"] = "http://evil.example/"
    with pytest.raises(AppError) as exc:
        context_layer.discover(_ws(), prefix=fx.PREFIX, env=fx.ENV, region=fx.COA_REGION)
    assert exc.value.code == "context_layer.discovery_failed"


@pytest.mark.parametrize("field,value", [
    ("prefix", "Bad Prefix"), ("region", "mars-1"),
])
def test_invalid_inputs_are_refused_before_any_aws_call(account, field, value):
    kwargs = {"prefix": fx.PREFIX, "env": fx.ENV, "region": fx.COA_REGION, field: value}
    with pytest.raises(AppError) as exc:
        context_layer.discover(_ws(), **kwargs)
    assert exc.value.code == "context_layer.invalid_request"
    assert account.regions == []


# ── connect: Connection create vs adopt, console workload identity ───────────


def test_connect_creates_the_connection_and_the_console_workload(account):
    layer = _connect()
    assert layer.connection == "coa-coa-w2"
    assert layer.console_workload == "launchpad-console-default"
    created = account.control.ops("create_oauth2_credential_provider")
    assert len(created) == 1
    config = created[0]["oauth2ProviderConfigInput"]["customOauth2ProviderConfig"]
    assert config["clientId"] == fx.AGENT_CLIENT
    assert config["oauthDiscovery"] == {
        "discoveryUrl": f"{fx.ISSUER}/.well-known/openid-configuration"
    }
    # the vault calls stay in the workspace region
    assert account.control.workloads == {"launchpad-console-default": [RETURN_URL]}
    db = SessionLocal()
    try:
        row = db.query(ContextLayerRow).one()
        audit = db.query(IdentityProvider).one()
    finally:
        db.close()
    assert (row.prefix, row.env, row.coa_region) == (fx.PREFIX, fx.ENV, fx.COA_REGION)
    assert audit.client_id == fx.AGENT_CLIENT
    # identifiers only: the client secret is nowhere in the ledger
    assert fx.CLIENT_SECRET not in json.dumps(row.resources)
    assert fx.CLIENT_SECRET not in json.dumps({c.name: str(getattr(audit, c.name))
                                               for c in IdentityProvider.__table__.columns})


def test_connect_adopts_a_named_connection_holding_the_agent_client(account):
    account.control.providers["coa-w2"] = {
        "name": "coa-w2", "callbackUrl": fx.CALLBACK,
        "oauth2ProviderConfigOutput": {"customOauth2ProviderConfig": {"clientId": fx.AGENT_CLIENT}},
    }
    layer = _connect(connection="coa-w2")
    assert layer.connection == "coa-w2"
    assert account.control.ops("create_oauth2_credential_provider") == []


def test_connect_refuses_a_connection_with_another_client(account):
    account.control.providers["coa-w2"] = {
        "name": "coa-w2",
        "oauth2ProviderConfigOutput": {"customOauth2ProviderConfig": {"clientId": "other"}},
    }
    with pytest.raises(AppError) as exc:
        _connect(connection="coa-w2")
    assert exc.value.code == "context_layer.connection_mismatch"
    db = SessionLocal()
    try:
        assert db.query(ContextLayerRow).count() == 0
    finally:
        db.close()


def test_connect_refuses_a_named_connection_that_does_not_exist(account):
    with pytest.raises(AppError) as exc:
        _connect(connection="nope")
    assert exc.value.code == "context_layer.connection_not_found"


def test_reconnect_is_idempotent_and_merges_the_return_url(account):
    account.control.workloads["launchpad-console-default"] = ["https://other.example/auth/return"]
    _connect()
    _connect()  # re-adopts coa-coa-w2, no second provider, no second row
    assert len(account.control.ops("create_oauth2_credential_provider")) == 1
    assert account.control.ops("create_workload_identity") == []
    assert account.control.workloads["launchpad-console-default"] == sorted(
        ["https://other.example/auth/return", RETURN_URL]
    )
    assert len(account.control.ops("update_workload_identity")) == 1
    db = SessionLocal()
    try:
        assert db.query(ContextLayerRow).count() == 1
    finally:
        db.close()


def test_ensure_console_workload_is_a_noop_when_listed(account):
    account.control.workloads["w"] = [RETURN_URL]
    assert context_layer.ensure_console_workload(account.control, "w", RETURN_URL) is False
    assert account.control.calls == []


# ── status ───────────────────────────────────────────────────────────────────


def test_status_when_nothing_is_connected(account):
    db = SessionLocal()
    try:
        assert context_layer.status(db, _ws(), username="alice", is_admin=False) == {
            "connected": False, "can_manage": False,
        }
    finally:
        db.close()


def test_status_reports_live_health_and_hides_admin_fields_from_members(account):
    _connect()
    db = SessionLocal()
    try:
        member = context_layer.status(db, _ws(), username="alice", is_admin=False)
        admin = context_layer.status(db, _ws(), username="root", is_admin=True)
    finally:
        db.close()
    assert member["connected"] and member["ready"]
    assert member["runtimes"] == {"mcp": "READY", "serve": "READY"}
    # the callback check reads the client secret: administrators only
    assert member["callback_registered"] is None
    assert admin["callback_registered"] is True
    assert member["agent_oauth_enabled"] is True
    assert member["authorization"] == {"status": "none", "authorized_at": None}
    assert member["console_url"] == fx.CONSOLE_URL
    for key in ("callback_url", "agent_client_id", "callback_context_key"):
        assert key not in member
    assert admin["callback_url"] == fx.CALLBACK
    assert admin["callback_context_key"] == "agentOauthCallbackUrls"
    for payload in (member, admin):
        text = json.dumps(payload)
        assert fx.CLIENT_SECRET not in text and "NEVER" not in text


def test_status_flags_an_unregistered_callback(monkeypatch):
    account = fx.install(monkeypatch, fx.Account(callbacks=["https://other/callback"]))
    _connect()
    db = SessionLocal()
    try:
        out = context_layer.status(db, _ws(), username="alice", is_admin=True)
    finally:
        db.close()
    assert out["callback_registered"] is False
    assert account.cognito.ops("describe_user_pool_client")


def test_members_never_read_the_agent_client_and_admins_reuse_the_answer(account):
    _connect()  # connect itself reads the client once (to create the Connection)
    context_layer.forget_callback_check("default")
    reads = len(account.cognito.ops("describe_user_pool_client"))
    db = SessionLocal()
    try:
        context_layer.status(db, _ws(), username="alice", is_admin=False)
        assert len(account.cognito.ops("describe_user_pool_client")) == reads
        for _ in range(3):
            assert context_layer.status(db, _ws(), username="root", is_admin=True)[
                "callback_registered"
            ] is True
    finally:
        db.close()
    assert len(account.cognito.ops("describe_user_pool_client")) == reads + 1


def test_status_degrades_a_failed_probe_to_unknown(account):
    _connect()
    context_layer.forget_callback_check("default")

    def boom(**kwargs):
        raise fx.client_error("AccessDeniedException")

    account.cognito.describe_user_pool_client = boom
    db = SessionLocal()
    try:
        out = context_layer.status(db, _ws(), username="alice", is_admin=True)
    finally:
        db.close()
    assert out["callback_registered"] is None
    failed = {"probe": "agent_client", "code": "context_layer.discovery_failed"}
    assert failed in out["probe_errors"]


# ── disconnect ───────────────────────────────────────────────────────────────


def test_disconnect_is_refused_while_an_agent_attaches_the_layer(account):
    _connect()
    ref = {"namespace_id": fx.NAMESPACE, "namespace_name": "AnyCompany HR"}
    harness_id = _agent({"name": "hr-h", "method": "harness", "context_layer": ref})
    zip_id = _agent({"name": "hr-z", "method": "zip_runtime", "context_layer": ref},
                    method="zip_runtime")
    _agent({"name": "plain", "method": "harness"})
    _agent({"name": "gone", "method": "harness", "context_layer": ref}, status="deleted")
    db = SessionLocal()
    try:
        with pytest.raises(AppError) as exc:
            context_layer.disconnect(db, _ws())
        assert exc.value.code == "context_layer.referenced"
        assert {r["id"] for r in exc.value.detail["referenced_by"]} == {harness_id, zip_id}
        assert db.query(ContextLayerRow).count() == 1
    finally:
        db.close()


def test_disconnect_keeps_the_connection_and_the_workload(account):
    _connect()
    db = SessionLocal()
    try:
        out = context_layer.disconnect(db, _ws())
        assert db.query(ContextLayerRow).count() == 0
    finally:
        db.close()
    assert out == {"disconnected": True, "connection": "coa-coa-w2",
                   "console_workload": "launchpad-console-default"}
    assert "coa-coa-w2" in account.control.providers
    assert "launchpad-console-default" in account.control.workloads


def test_the_layers_connection_cannot_be_deleted_while_connected(account):
    _connect()
    db = SessionLocal()
    try:
        with pytest.raises(AppError) as exc:
            identity_providers.delete_connection(account.control, db, "default", "oauth2",
                                                 "coa-coa-w2")
    finally:
        db.close()
    assert exc.value.code == "identity.connection_referenced"
    assert exc.value.detail["referenced_by"][0]["type"] == "context_layer"


# ── HTTP ─────────────────────────────────────────────────────────────────────


def test_routes_connect_status_and_disconnect(client, account):
    body = {"prefix": fx.PREFIX, "env": fx.ENV, "region": fx.COA_REGION, "connection": None}
    connected = client.put("/api/context-layer", json=body)
    assert connected.status_code == 200, connected.text
    assert connected.json()["connected"] is True
    assert connected.json()["connection"] == "coa-coa-w2"
    status = client.get("/api/context-layer")
    assert status.status_code == 200 and status.json()["callback_registered"] is True
    assert fx.CLIENT_SECRET not in connected.text + status.text
    gone = client.delete("/api/context-layer")
    assert gone.status_code == 200 and gone.json()["disconnected"] is True
    assert client.get("/api/context-layer").json()["connected"] is False


def test_route_disconnect_answers_409_with_the_agents(client, account):
    _connect()
    _agent({"name": "hr-h", "method": "harness",
            "context_layer": {"namespace_id": fx.NAMESPACE}})
    response = client.delete("/api/context-layer")
    assert response.status_code == 409
    assert response.json()["code"] == "context_layer.referenced"
    assert response.json()["detail"]["referenced_by"][0]["name"] == "hr-h"


def test_route_connect_validates_its_body(client, account):
    response = client.put("/api/context-layer", json={"prefix": "x", "region": "nowhere"})
    assert response.status_code == 422
    assert account.regions == []
