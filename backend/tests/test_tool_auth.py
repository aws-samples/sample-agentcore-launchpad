"""Tool-level outbound auth (``ToolRef.auth``): schema, request-time validation,
republish carry-through, IAM, generated code, env, the agent identity view, and
the upgrade of a ledger the earlier identity fork created."""

import ast
import json
from unittest.mock import MagicMock

import pytest
import sqlalchemy as sa
from botocore.exceptions import ClientError
from pydantic import ValidationError

import app.routers.agents as agents_router
from app.core import db as db_module
from app.core.db import SessionLocal, schema_drift
from app.core.errors import AppError
from app.deployer.environment import ENV_OUTBOUND_AUTH, runtime_environment
from app.models.ledger import Agent
from app.schemas.agent import AgentSpec, ToolAuth
from app.services import agent_iam
from app.services import agent_identity as identity_view
from app.services import identity_providers as ip
from app.services.agent_iam import RoleContext
from app.templates import identity_support
from app.templates.gateway_support import runtime_user_id
from app.templates.strands_agent import render_main_py

CTX = RoleContext(
    account_id="123456789012",
    region="us-west-2",
    artifacts_bucket="launchpad-artifacts-123456789012-us-west-2",
    ecr_repo_arn="arn:aws:ecr:us-west-2:123456789012:repository/launchpad-agents",
    memory_id="launchpad_memory-abc123",
)

REST_OAUTH = {
    "type": "rest",
    "name": "crm",
    "config": {"url": "https://crm.example/api", "description": "CRM"},
    "auth": {"connection": "team-idp", "kind": "oauth2", "scopes": ["crm/read"]},
}
REST_KEY = {
    "type": "rest",
    "name": "facts",
    "config": {"url": "https://facts.example"},
    "auth": {"connection": "team-key", "kind": "api_key",
             "api_key": {"in": "header", "name": "x-api-key"}},
}
SPEC = {
    "name": "auth-agent",
    "method": "zip_runtime",
    "system_prompt": "p",
    "tools": [REST_OAUTH, REST_KEY],
}


def _client_error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": code}}, "Op")


def make_control(oauth=("team-idp",), api=("team-key",)):
    control = MagicMock()
    control.list_oauth2_credential_providers.return_value = {
        "credentialProviders": [{"name": n} for n in oauth]
    }
    control.list_api_key_credential_providers.return_value = {
        "credentialProviders": [{"name": n} for n in api]
    }
    control.list_gateway_targets.return_value = {"items": []}
    return control


# ── schema ───────────────────────────────────────────────────────────────────


def test_auth_round_trips_and_absent_auth_keeps_the_old_shape():
    spec = AgentSpec(**SPEC)
    dumped = spec.model_dump()
    assert dumped["tools"][0]["auth"] == {
        "connection": "team-idp", "kind": "oauth2", "mode": "as_agent",
        "scopes": ["crm/read"], "audience": None, "api_key": None,
    }
    assert dumped["tools"][1]["auth"]["api_key"] == {"in": "header", "name": "x-api-key"}
    assert AgentSpec(**dumped).model_dump() == dumped

    plain = AgentSpec(name="plain-agent", method="zip_runtime", system_prompt="p",
                      tools=[{"type": "builtin", "name": "browser"}]).model_dump()
    assert "auth" not in plain["tools"][0]


def test_legacy_provider_flow_shape_maps_onto_connection_kind_mode():
    auth = ToolAuth(provider="team-idp", flow="M2M", scopes=["s"], force_reauth=True)
    assert (auth.connection, auth.kind, auth.mode) == ("team-idp", "oauth2", "as_agent")
    assert ToolAuth(provider="k", flow="API_KEY").kind == "api_key"
    assert ToolAuth(provider="u", flow="USER_FEDERATION").mode == "as_user"


@pytest.mark.parametrize(
    "auth",
    [
        {"connection": "k", "kind": "api_key", "scopes": ["s"]},
        {"connection": "k", "kind": "api_key", "mode": "as_user"},
        {"connection": "k", "kind": "oauth2", "api_key": {"in": "query", "name": "k"}},
        {"connection": "bad name", "kind": "oauth2"},
        {"connection": "k", "kind": "oauth2", "scopes": [""]},
    ],
)
def test_toolauth_kind_shape_errors(auth):
    with pytest.raises(ValidationError):
        ToolAuth(**auth)


@pytest.mark.parametrize(
    ("over", "fragment"),
    [
        ({"method": "harness"}, "zip_runtime and byoc"),
        ({"tools": [{**REST_OAUTH, "type": "gateway"}]}, "rest and mcp tools only"),
        ({"tools": [REST_OAUTH, REST_OAUTH]}, "unique"),
        ({"tools": [{**REST_OAUTH, "config": {}}]}, "config.url"),
        ({"tools": [{**REST_OAUTH, "name": "1bad"}]}, "must match"),
    ],
)
def test_auth_placement_is_refused_where_nothing_would_perform_it(over, fragment):
    with pytest.raises(ValidationError) as err:
        AgentSpec(**{**SPEC, **over})
    assert fragment in str(err.value)


def test_byoc_accepts_auth_declarations_only():
    byoc = {"name": "byoc-agent", "method": "byoc",
            "byoc": {"artifact_kind": "code_zip", "upload_id": "u1"}}
    spec = AgentSpec(**byoc, tools=[{**REST_KEY, "config": {}}])
    assert spec.tools[0].auth.connection == "team-key"
    with pytest.raises(ValidationError):
        AgentSpec(**byoc, tools=[{"type": "rest", "name": "open", "config": {}}])


# ── request-time validation ──────────────────────────────────────────────────


def test_validate_tool_auth_accepts_live_connections():
    ip.validate_tool_auth(make_control(), AgentSpec(**SPEC))


@pytest.mark.parametrize(
    ("control", "code"),
    [
        (make_control(oauth=()), "identity.connection_unknown"),
        (make_control(oauth=(), api=("team-key", "team-idp")), "identity.kind_mismatch"),
    ],
)
def test_validate_tool_auth_refuses_missing_or_mismatched(control, code):
    with pytest.raises(AppError) as err:
        ip.validate_tool_auth(control, AgentSpec(**SPEC))
    assert (err.value.code, err.value.status_code) == (code, 422)
    assert err.value.detail["connection"] == "team-idp"


def test_validate_tool_auth_refuses_unimplemented_modes():
    spec = AgentSpec(**{**SPEC, "tools": [
        {**REST_OAUTH, "auth": {**REST_OAUTH["auth"], "mode": "obo"}},
    ]})
    control = make_control()
    with pytest.raises(AppError) as err:
        ip.validate_tool_auth(control, spec)
    assert err.value.code == "identity.mode_unsupported"
    control.list_oauth2_credential_providers.assert_not_called()


# ── router: create / redeploy / republish ────────────────────────────────────


@pytest.fixture
def no_real_deploy(monkeypatch):
    launched: list[str] = []
    monkeypatch.setattr(agents_router, "start_deploy_async", lambda jid: launched.append(jid))
    return launched


@pytest.fixture
def vault(monkeypatch):
    control = make_control()
    monkeypatch.setattr(agents_router, "control_client", lambda _ctx: control)
    return control


def _activate(agent_id: str) -> None:
    db = SessionLocal()
    agent = db.get(Agent, agent_id)
    agent.status = "active"
    agent.resource_id = "rt-1"
    agent.arn = "arn:aws:bedrock-agentcore:us-west-2:111:runtime/rt-1"
    db.commit()
    db.close()


def test_create_with_missing_connection_is_422_and_writes_nothing(
    client, no_real_deploy, vault
):
    vault.list_api_key_credential_providers.return_value = {"credentialProviders": []}
    res = client.post("/api/agents", json=SPEC)
    assert res.status_code == 422
    assert res.json()["code"] == "identity.connection_unknown"
    assert no_real_deploy == []
    assert client.get("/api/agents").json()["agents"] == []


def test_republish_after_an_unrelated_edit_keeps_the_auth_block(
    client, no_real_deploy, vault
):
    created = client.post("/api/agents", json=SPEC)
    assert created.status_code == 202, created.text
    agent_id = created.json()["agent"]["id"]
    _activate(agent_id)
    stored = client.get(f"/api/agents/{agent_id}").json()["spec"]
    before = [t["auth"] for t in stored["tools"]]

    # the console republishes the stored spec with one unrelated field edited
    edited = {**stored, "system_prompt": "Now answer in French."}
    res = client.post(f"/api/agents/{agent_id}/redeploy", json=edited)
    assert res.status_code == 202, res.text

    after = client.get(f"/api/agents/{agent_id}").json()["spec"]
    assert after["system_prompt"] == "Now answer in French."
    assert [t["auth"] for t in after["tools"]] == before
    assert before[0]["connection"] == "team-idp"
    assert before[1]["api_key"] == {"in": "header", "name": "x-api-key"}


def test_redeploy_after_the_connection_vanished_fails_loudly(client, no_real_deploy, vault):
    agent_id = client.post("/api/agents", json=SPEC).json()["agent"]["id"]
    _activate(agent_id)
    vault.list_oauth2_credential_providers.return_value = {"credentialProviders": []}
    res = client.post(f"/api/agents/{agent_id}/redeploy", json=SPEC)
    assert res.status_code == 422
    assert res.json()["code"] == "identity.connection_unknown"
    assert client.get(f"/api/agents/{agent_id}").json()["status"] == "active"


def test_create_without_auth_never_reaches_the_vault(client, no_real_deploy, monkeypatch):
    def boom(_ctx):
        raise AssertionError("control_client must not be built for a spec without auth")

    monkeypatch.setattr(agents_router, "control_client", boom)
    res = client.post("/api/agents", json={
        "name": "plain", "method": "harness", "system_prompt": "p",
    })
    assert res.status_code == 202


# ── IAM ──────────────────────────────────────────────────────────────────────


def _statements(spec: AgentSpec) -> dict[str, dict]:
    return {s["Sid"]: s for s in agent_iam.policy_document(spec, CTX)["Statement"]}


def test_iam_grants_exactly_the_referenced_connections():
    statements = _statements(AgentSpec(**SPEC))
    base = "arn:aws:bedrock-agentcore:us-west-2:123456789012:token-vault/default"
    assert f"{base}/oauth2credentialprovider/team-idp" in (
        statements["ToolAuthOauth2Token"]["Resource"]
    )
    assert f"{base}/apikeycredentialprovider/team-key" in (
        statements["ToolAuthApiKey"]["Resource"]
    )
    secrets = statements["ToolAuthVaultSecrets"]["Resource"]
    assert all("bedrock-agentcore-identity!default/" in r for r in secrets)
    assert not any(r.endswith("!*") for r in secrets)
    assert {r.rsplit("/", 2)[-2] + "/" + r.rsplit("/", 1)[-1] for r in secrets} == {
        "oauth2/team-idp-*", "apikey/team-key-*",
    }


def test_iam_scopes_the_token_exchange_to_the_agents_own_workload_identity():
    statements = _statements(AgentSpec(**SPEC))
    own = (
        "arn:aws:bedrock-agentcore:us-west-2:123456789012:workload-identity-directory/"
        "default/workload-identity/auth_agent_??????-*"
    )
    for sid in ("ToolAuthOauth2Token", "ToolAuthApiKey"):
        resources = statements[sid]["Resource"]
        assert own in resources
        assert not any(r.endswith("/workload-identity/*") for r in resources)


def test_own_workload_identity_pattern_matches_the_runtime_ids_it_must():
    from fnmatch import fnmatchcase

    from app.deployer.zip_runtime import sanitize_runtime_name

    def matches(arn_pattern: str, identity: str) -> bool:
        # IAM ARN matching: * = any run, ? = exactly one character
        suffix = arn_pattern.rsplit("/workload-identity/", 1)[1]
        return fnmatchcase(identity, suffix)

    pattern = agent_iam.own_workload_identity_arn(AgentSpec(**SPEC), CTX)
    runtime_name = sanitize_runtime_name("auth-agent")
    # the Runtime names its workload identity after the runtime id
    assert matches(pattern, f"{runtime_name}-0RevAO6bjl")
    assert not matches(pattern, "auth_agent_extra_1a2b3c-0RevAO6bjl")  # another agent
    assert not matches(pattern, "other_agent_1a2b3c-0RevAO6bjl")
    long_name = "a-" * 23 + "zz"  # 48 chars: the base truncates to 40, ending in "_"
    long_pattern = agent_iam.own_workload_identity_arn(
        AgentSpec(name=long_name, method="zip_runtime", system_prompt="p"), CTX)
    assert matches(long_pattern, f"{sanitize_runtime_name(long_name)}-AbCdEf1234")


def test_iam_adds_nothing_without_auth_tools():
    spec = AgentSpec(name="plain-agent", method="zip_runtime", system_prompt="p",
                     tools=[{"type": "rest", "name": "open",
                             "config": {"url": "https://open.example"}}])
    assert not {sid for sid in _statements(spec) if sid.startswith("ToolAuth")}


# ── generated code / env / invoke ────────────────────────────────────────────


def test_rendered_main_compiles_and_bakes_identifiers_only():
    source = render_main_py(AgentSpec(**SPEC))
    ast.parse(source)
    assert "__LAUNCHPAD_IDENTITY" not in source
    assert "<launchpad-identity-outbound:p2>" in source
    assert "IDENTITY_TOOLS = identity_tools" in source
    assert "'connection': 'team-idp'" in source and "'key_name': 'x-api-key'" in source

    plain = render_main_py(AgentSpec(name="plain-agent", method="zip_runtime", system_prompt="p"))
    ast.parse(plain)
    assert "launchpad-identity-outbound" not in plain
    assert 'IDENTITY_TOOLS = lambda _stack, _session_id="", _force_reauth=(): []' in plain
    assert "IDENTITY_AUTH_NOTICES = lambda: []" in plain


def test_outbound_auth_env_and_workload_name():
    env = runtime_environment(AgentSpec(**SPEC), {}, workload_name="wl-1")
    declared = json.loads(env[ENV_OUTBOUND_AUTH])
    assert [(d["tool"], d["connection"], d["kind"], d["mode"]) for d in declared] == [
        ("crm", "team-idp", "oauth2", "as_agent"),
        ("facts", "team-key", "api_key", "as_agent"),
    ]
    assert declared[0]["url"] == "https://crm.example/api"
    assert env["LAUNCHPAD_WORKLOAD_NAME"] == "wl-1"
    plain_spec = AgentSpec(name="plain-agent", method="zip_runtime", system_prompt="p")
    plain = runtime_environment(plain_spec, {})
    assert ENV_OUTBOUND_AUTH not in plain


def test_runtime_user_id_is_sent_when_a_tool_carries_auth():
    assert runtime_user_id(AgentSpec(**SPEC).model_dump(), "alice")
    assert runtime_user_id({"tools": [{"type": "rest", "name": "x"}]}, "alice") is None
    assert identity_support.uses_workload_identity(AgentSpec(**SPEC).model_dump())


# ── agent identity view ──────────────────────────────────────────────────────


def _agent(**over) -> Agent:
    return Agent(**{
        "id": "a1", "name": "auth-agent", "method": "zip_runtime", "status": "active",
        "resource_id": "rt-1", "spec": AgentSpec(**SPEC).model_dump(),
    } | over)


def test_identity_view_reads_workload_identity_and_downstreams():
    control = make_control(oauth=("team-idp", "launchpad-gw-m2m"), api=())
    control.get_agent_runtime.return_value = {
        "workloadIdentityDetails": {
            "workloadIdentityArn": "arn:aws:bedrock-agentcore:us-west-2:111:"
            "workload-identity-directory/default/workload-identity/auth_agent-xyz"
        },
    }
    control.get_workload_identity.return_value = {
        "allowedResourceOauth2ReturnUrls": ["https://console.example/cb"]
    }
    view = identity_view.agent_identity(control, _agent(), {})
    assert view["workload_identity"]["status"] == "ready"
    assert view["workload_identity"]["name"] == "auth_agent-xyz"
    assert view["workload_identity"]["allowed_return_urls"] == ["https://console.example/cb"]
    control.get_workload_identity.assert_called_once_with(name="auth_agent-xyz")
    assert view["inbound"] == {
        "mode": "iam", "source": "aws", "jwt": None,
        "capable": True, "pinned": None, "ledger_mode": "iam", "invoke_url": None,
    }
    by_name = {d["name"]: d for d in view["downstreams"]}
    assert by_name["crm"]["connection_status"] == "ready"
    assert by_name["facts"]["connection_status"] == "missing"  # vault lacks team-key


def test_identity_view_lists_gateway_targets_behind_an_attached_gateway():
    control = make_control(oauth=("launchpad-gw-m2m", "team-idp"))
    control.get_agent_runtime.return_value = {
        "authorizerConfiguration": {"customJWTAuthorizer": {"discoveryUrl": "x"}},
    }
    control.list_gateway_targets.return_value = {"items": [{"targetId": "T1"}]}
    control.get_gateway_target.return_value = {
        "targetId": "T1", "name": "crm-api",
        "targetConfiguration": {"mcp": {"openApiSchema": {}}},
        "credentialProviderConfigurations": [{
            "credentialProviderType": "OAUTH",
            "credentialProvider": {"oauthCredentialProvider": {
                "providerArn": "arn:x:token-vault/default/oauth2credentialprovider/team-idp",
                "grantType": "CLIENT_CREDENTIALS", "scopes": ["crm/read"],
            }},
        }],
    }
    agent = _agent(spec={"name": "g", "tools": [{"type": "gateway", "name": "gw"}]})
    view = identity_view.agent_identity(control, agent, {"gateway_id": "gw-1"})
    assert view["workload_identity"]["status"] == "none"
    assert view["inbound"]["mode"] == "jwt"
    assert [(d["type"], d["connection"], d["via"]) for d in view["downstreams"]] == [
        ("gateway", "launchpad-gw-m2m", "agent"),
        ("gateway_target", "team-idp", "gateway"),
    ]


def test_identity_view_statuses_for_harness_undeployed_and_deleted_runtime():
    control = make_control()
    assert identity_view.agent_identity(
        control, _agent(method="harness"), {}
    )["workload_identity"]["status"] == "managed"
    assert identity_view.agent_identity(
        control, _agent(resource_id=None), {}
    )["workload_identity"]["status"] == "not_deployed"
    control.get_agent_runtime.side_effect = _client_error("ResourceNotFoundException")
    assert identity_view.agent_identity(
        control, _agent(), {}
    )["workload_identity"]["status"] == "missing"


def test_identity_endpoint(client, no_real_deploy, vault):
    agent_id = client.post("/api/agents", json=SPEC).json()["agent"]["id"]
    res = client.get(f"/api/agents/{agent_id}/identity")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["agent_id"] == agent_id
    assert body["workload_identity"]["status"] == "not_deployed"
    assert {d["name"] for d in body["downstreams"]} == {"crm", "facts"}
    assert client.get("/api/agents/nope/identity").status_code == 404


# ── ledger upgrade from the earlier identity fork ────────────────────────────


def test_fork_created_identity_tables_upgrade_in_place(tmp_path):
    """A ledger the earlier fork wrote: identity_providers without the P1
    columns, rows present. Startup must add the columns and keep the rows."""
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'fork.db'}")
    db_module.Base.metadata.create_all(bind=engine)
    with engine.begin() as conn:
        conn.execute(sa.text("DROP INDEX uq_identity_providers_ws_kind_name"))
        for column in ("client_id", "scopes", "template", "description"):
            conn.execute(sa.text(f"ALTER TABLE identity_providers DROP COLUMN {column}"))
        conn.execute(sa.text(
            "INSERT INTO identity_providers (id, workspace_id, name, kind, vendor, arn, "
            "created_by, created_at) VALUES ('p1', 'default', 'team-idp', 'oauth2', "
            "'CustomOauth2', 'arn:x', 'admin', '2026-09-01 00:00:00')"
        ))
    assert "identity_providers" in schema_drift(engine)

    db_module._migrate(engine)

    assert schema_drift(engine) == {}
    inspector = sa.inspect(engine)
    assert "uq_identity_providers_ws_kind_name" in {
        i["name"] for i in inspector.get_indexes("identity_providers")
    }
    with engine.begin() as conn:
        row = conn.execute(sa.text(
            "SELECT name, client_id, scopes FROM identity_providers"
        )).one()
    assert tuple(row) == ("team-idp", None, None)
    db_module._migrate(engine)  # idempotent
