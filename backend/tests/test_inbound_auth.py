"""Inbound JWT auth (P1): model validation, resolution precedence, the
discovery probe, authorizerConfiguration projection, the workspace-default API
and the ledger migration of the new columns."""

import json

import pytest
import sqlalchemy as sa

from app.core import db as db_module
from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.core.errors import AppError
from app.models.ledger import Agent, Workspace
from app.schemas.agent import AgentSpec
from app.schemas.inbound_auth import (
    IAM_INBOUND,
    CustomClaim,
    InboundAuth,
    JwtInboundConfig,
    authorizer_configuration,
    parse_inbound_auth,
    resolve_inbound_auth,
)
from app.services import inbound_auth as svc

DISCOVERY = (
    "https://cognito-idp.us-west-2.amazonaws.com/us-west-2_ABC123"
    "/.well-known/openid-configuration"
)


def jwt_auth(**overrides) -> InboundAuth:
    fields = {"discovery_url": DISCOVERY, "allowed_clients": ["client-a"], **overrides}
    return InboundAuth(mode="jwt", jwt=JwtInboundConfig(**fields))


# ---------------------------------------------------------------------------
# model validation
# ---------------------------------------------------------------------------

class TestModel:
    def test_iam_default(self):
        assert InboundAuth().mode == "iam"

    def test_jwt_requires_config(self):
        with pytest.raises(ValueError, match="requires the jwt config"):
            InboundAuth(mode="jwt")

    def test_iam_refuses_config(self):
        with pytest.raises(ValueError, match="mode='jwt' only"):
            InboundAuth(mode="iam", jwt=JwtInboundConfig(
                discovery_url=DISCOVERY, allowed_clients=["c"]))

    @pytest.mark.parametrize("url", [
        "https://idp.example.com/x/.well-known/openid-configuration",
        "http://localhost:8080/realms/r/.well-known/openid-configuration",
    ])
    def test_discovery_url_accepts(self, url):
        JwtInboundConfig(discovery_url=url, allowed_clients=["c"])

    @pytest.mark.parametrize("url", [
        "https://idp.example.com/.well-known/jwks.json",
        "idp.example.com/.well-known/openid-configuration",  # no scheme
        "https://idp.example.com/openid-configuration",
    ])
    def test_discovery_url_rejects(self, url):
        with pytest.raises(ValueError, match="discovery_url"):
            JwtInboundConfig(discovery_url=url, allowed_clients=["c"])

    def test_at_least_one_restriction(self):
        with pytest.raises(ValueError, match="restrict callers"):
            JwtInboundConfig(discovery_url=DISCOVERY)

    def test_custom_claims_alone_suffice(self):
        config = JwtInboundConfig(
            discovery_url=DISCOVERY,
            custom_claims=[CustomClaim(
                name="cognito:groups", value_type="STRING_ARRAY",
                match_operator="CONTAINS_ANY", match_values=["platform-admin"])],
        )
        assert config.custom_claims[0].match_operator == "CONTAINS_ANY"

    def test_equals_takes_exactly_one_value(self):
        with pytest.raises(ValueError, match="exactly one"):
            CustomClaim(name="scope", match_operator="EQUALS",
                        match_values=["a", "b"])

    def test_parse_lenient_on_garbage(self):
        assert parse_inbound_auth(None) is None
        assert parse_inbound_auth("jwt") is None
        assert parse_inbound_auth({"mode": "jwt"}) is None  # invalid shape
        parsed = parse_inbound_auth({"mode": "iam"})
        assert parsed is not None and parsed.mode == "iam"


# ---------------------------------------------------------------------------
# AgentSpec placement
# ---------------------------------------------------------------------------

class TestSpecPlacement:
    def test_zip_runtime_accepts_jwt(self):
        spec = AgentSpec(name="jwt-agent", method="zip_runtime",
                         system_prompt="hi", inbound_auth=jwt_auth())
        assert spec.inbound_auth.mode == "jwt"

    def test_none_means_inherit(self):
        spec = AgentSpec(name="plain-agent", method="zip_runtime", system_prompt="hi")
        assert spec.inbound_auth is None

    def test_harness_refuses_explicit_jwt(self):
        with pytest.raises(ValueError, match="not supported by the harness"):
            AgentSpec(name="h-agent", method="harness", system_prompt="hi",
                      inbound_auth=jwt_auth())

    def test_a2a_refuses_explicit_jwt(self):
        with pytest.raises(ValueError, match="protocol=a2a"):
            AgentSpec(name="a2a-agent", method="zip_runtime", system_prompt="hi",
                      protocol="a2a", inbound_auth=jwt_auth())

    def test_explicit_iam_is_fine_everywhere(self):
        spec = AgentSpec(name="h-agent", method="harness", system_prompt="hi",
                         inbound_auth=InboundAuth(mode="iam"))
        assert spec.inbound_auth.mode == "iam"


# ---------------------------------------------------------------------------
# resolution precedence
# ---------------------------------------------------------------------------

class TestResolution:
    def test_spec_wins_over_workspace(self):
        resolved = resolve_inbound_auth(
            IAM_INBOUND, jwt_auth(), method="zip_runtime")
        assert resolved.mode == "iam"

    def test_workspace_default_inherited(self):
        resolved = resolve_inbound_auth(None, jwt_auth(), method="zip_runtime")
        assert resolved.mode == "jwt"

    def test_defaults_to_iam(self):
        assert resolve_inbound_auth(None, None, method="zip_runtime").mode == "iam"

    @pytest.mark.parametrize("method", ["harness", "discovered"])
    def test_non_runtime_methods_resolve_iam_under_jwt_default(self, method):
        assert resolve_inbound_auth(None, jwt_auth(), method=method).mode == "iam"

    def test_a2a_resolves_iam_under_jwt_default(self):
        resolved = resolve_inbound_auth(
            None, jwt_auth(), method="zip_runtime", protocol="a2a")
        assert resolved.mode == "iam"

    @pytest.mark.parametrize("method", ["zip_runtime", "studio", "container", "byoc"])
    def test_runtime_methods_inherit_jwt(self, method):
        assert resolve_inbound_auth(None, jwt_auth(), method=method).mode == "jwt"


# ---------------------------------------------------------------------------
# authorizerConfiguration projection
# ---------------------------------------------------------------------------

class TestAuthorizerConfiguration:
    def test_iam_is_none(self):
        assert authorizer_configuration(IAM_INBOUND) is None

    def test_jwt_shape(self):
        auth = InboundAuth(mode="jwt", jwt=JwtInboundConfig(
            discovery_url=DISCOVERY,
            allowed_clients=["client-a", "client-b"],
            allowed_audience=["aud-1"],
            allowed_scopes=["api/read"],
            custom_claims=[CustomClaim(
                name="cognito:groups", value_type="STRING_ARRAY",
                match_operator="CONTAINS_ANY",
                match_values=["platform-admin", "hr-analyst"])],
        ))
        config = authorizer_configuration(auth)["customJWTAuthorizer"]
        assert config["discoveryUrl"] == DISCOVERY
        assert config["allowedClients"] == ["client-a", "client-b"]
        assert config["allowedAudience"] == ["aud-1"]
        assert config["allowedScopes"] == ["api/read"]
        claim = config["customClaims"][0]
        assert claim["inboundTokenClaimName"] == "cognito:groups"
        assert claim["inboundTokenClaimValueType"] == "STRING_ARRAY"
        assert claim["authorizingClaimMatchValue"] == {
            "claimMatchValue": {"matchValueString"
                                "List": ["platform-admin", "hr-analyst"]},
            "claimMatchOperator": "CONTAINS_ANY",
        }

    def test_equals_claim_uses_single_string(self):
        auth = InboundAuth(mode="jwt", jwt=JwtInboundConfig(
            discovery_url=DISCOVERY,
            custom_claims=[CustomClaim(
                name="token_use", match_operator="EQUALS", match_values=["access"])],
        ))
        claim = authorizer_configuration(auth)["customJWTAuthorizer"]["customClaims"][0]
        assert claim["authorizingClaimMatchValue"]["claimMatchValue"] == {
            "matchValueString": "access"
        }

    def test_empty_lists_are_omitted(self):
        config = authorizer_configuration(jwt_auth())["customJWTAuthorizer"]
        assert "allowedAudience" not in config
        assert "allowedScopes" not in config
        assert "customClaims" not in config


# ---------------------------------------------------------------------------
# discovery probe
# ---------------------------------------------------------------------------

class TestProbe:
    def test_valid_document_passes(self):
        result = svc.probe_discovery(
            DISCOVERY,
            probe=lambda url: {"issuer": "https://i", "jwks_uri": "https://i/jwks",
                               "token_endpoint": "https://i/token"},
        )
        assert result["jwks_uri"] == "https://i/jwks"

    def test_unreachable_names_the_url(self):
        def failing(url):
            raise OSError("connection refused")
        with pytest.raises(AppError) as excinfo:
            svc.probe_discovery(DISCOVERY, probe=failing)
        assert excinfo.value.code == "identity.discovery_unreachable"
        assert DISCOVERY in excinfo.value.message

    def test_missing_jwks_uri_refused(self):
        with pytest.raises(AppError) as excinfo:
            svc.probe_discovery(DISCOVERY, probe=lambda url: {"issuer": "https://i"})
        assert excinfo.value.code == "identity.discovery_invalid"

    def test_iam_mode_skips_probe(self):
        # would raise if the probe ran
        svc.validate_inbound_auth(
            IAM_INBOUND, probe=lambda url: (_ for _ in ()).throw(AssertionError))


# ---------------------------------------------------------------------------
# workspace default persistence + snapshot readback
# ---------------------------------------------------------------------------

@pytest.fixture
def db():
    session = SessionLocal()
    yield session
    session.close()


class TestWorkspaceDefault:
    def test_roundtrip(self, db):
        row = db.get(Workspace, DEFAULT_WORKSPACE_ID)
        assert svc.workspace_default(row) is None  # fresh DB: implicit IAM
        svc.set_workspace_default(db, row, jwt_auth())
        db.refresh(row)
        stored = svc.workspace_default(row)
        assert stored is not None and stored.mode == "jwt"
        assert stored.jwt.allowed_clients == ["client-a"]

    def test_set_preserves_other_settings_keys(self, db):
        row = db.get(Workspace, DEFAULT_WORKSPACE_ID)
        row.settings = {"unrelated": 1}
        db.commit()
        svc.set_workspace_default(db, row, InboundAuth(mode="iam"))
        db.refresh(row)
        assert row.settings["unrelated"] == 1
        assert svc.workspace_default(row).mode == "iam"

    def test_unreadable_blob_reads_as_no_default(self, db):
        row = db.get(Workspace, DEFAULT_WORKSPACE_ID)
        row.settings = {svc.SETTINGS_KEY: {"mode": "jwt"}}  # invalid: no config
        db.commit()
        assert svc.workspace_default(row) is None


class TestDeployedSnapshot:
    def _agent(self, db, **fields) -> Agent:
        agent = Agent(workspace_id=DEFAULT_WORKSPACE_ID, name="snap-agent",
                      method="zip_runtime", status="active", spec={}, **fields)
        db.add(agent)
        db.commit()
        return agent

    def test_null_mode_reads_iam(self, db):
        agent = self._agent(db)
        assert svc.deployed_inbound_auth(agent).mode == "iam"
        assert svc.is_jwt_mode(agent) is False
        assert svc.display_mode(agent) == "iam"

    def test_jwt_snapshot_reads_back(self, db):
        agent = self._agent(
            db, inbound_auth_mode="jwt", inbound_auth_config=jwt_auth().model_dump())
        deployed = svc.deployed_inbound_auth(agent)
        assert deployed.mode == "jwt"
        assert deployed.jwt.discovery_url == DISCOVERY
        assert svc.is_jwt_mode(agent) is True

    def test_corrupt_jwt_snapshot_still_reads_jwt(self, db):
        # keying the invoke path off a bad snapshot as IAM would 403 every call
        agent = self._agent(db, inbound_auth_mode="jwt",
                            inbound_auth_config={"mode": "nonsense"})
        assert svc.deployed_inbound_auth(agent).mode == "jwt"

    def test_discovered_agent_projects_scanned_authorizer(self, db):
        agent = self._agent(db)
        agent.method = "discovered"
        agent.spec = {"discovery": {"authorizer_type": "custom_jwt"}}
        db.commit()
        assert svc.display_mode(agent) == "jwt"


# ---------------------------------------------------------------------------
# router: GET/PUT /api/identity/inbound-auth/default
# ---------------------------------------------------------------------------

class TestDefaultApi:
    def test_get_reports_implicit_iam(self, client):
        body = client.get("/api/identity/inbound-auth/default").json()
        assert body["default"]["mode"] == "iam"
        assert body["configured"] is False

    def test_put_probes_and_persists(self, client, monkeypatch):
        probed: list[str] = []

        def fake_probe(url, probe=None):
            probed.append(url)
            return {"issuer": "https://i", "jwks_uri": "https://i/jwks",
                    "token_endpoint": ""}

        monkeypatch.setattr(svc, "probe_discovery", fake_probe)
        response = client.put(
            "/api/identity/inbound-auth/default",
            json={"mode": "jwt", "jwt": {"discovery_url": DISCOVERY,
                                         "allowed_clients": ["client-a"]}},
        )
        assert response.status_code == 200, response.text
        assert probed == [DISCOVERY]
        body = response.json()
        assert body["default"]["mode"] == "jwt"
        assert body["configured"] is True
        # persisted: a fresh GET sees it
        again = client.get("/api/identity/inbound-auth/default").json()
        assert again["default"]["mode"] == "jwt"

    def test_put_rejects_unreachable_discovery(self, client, monkeypatch):
        def failing(url, probe=None):
            raise AppError("identity.discovery_unreachable", "nope", status_code=422)

        monkeypatch.setattr(svc, "probe_discovery", failing)
        response = client.put(
            "/api/identity/inbound-auth/default",
            json={"mode": "jwt", "jwt": {"discovery_url": DISCOVERY,
                                         "allowed_clients": ["client-a"]}},
        )
        assert response.status_code == 422
        # nothing persisted
        assert client.get("/api/identity/inbound-auth/default").json()["configured"] is False

    def test_put_back_to_iam(self, client, monkeypatch):
        monkeypatch.setattr(
            svc, "probe_discovery",
            lambda url, probe=None: {"issuer": "", "jwks_uri": "x", "token_endpoint": ""})
        client.put("/api/identity/inbound-auth/default",
                   json={"mode": "jwt", "jwt": {"discovery_url": DISCOVERY,
                                                "allowed_clients": ["c"]}})
        response = client.put("/api/identity/inbound-auth/default", json={"mode": "iam"})
        assert response.status_code == 200
        assert response.json()["default"]["mode"] == "iam"


# ---------------------------------------------------------------------------
# deployer: authorizerConfiguration on create + every update transition
# ---------------------------------------------------------------------------


class StubRuntimeControl:
    def __init__(self):
        self.created_with = None
        self.updated_with = None

    def create_agent_runtime(self, **kwargs):
        self.created_with = kwargs
        return {"agentRuntimeId": "rt-1", "agentRuntimeArn": "arn:rt-1",
                "agentRuntimeVersion": "1", "status": "CREATING"}

    def update_agent_runtime(self, **kwargs):
        self.updated_with = kwargs
        return {"agentRuntimeId": kwargs["agentRuntimeId"], "agentRuntimeArn": "arn:rt-1",
                "agentRuntimeVersion": "2", "status": "UPDATING"}

    def get_agent_runtime(self, agentRuntimeId):
        return {"agentRuntimeId": agentRuntimeId, "agentRuntimeArn": "arn:rt-1",
                "agentRuntimeVersion": "1", "status": "READY"}


class TestDeployerAuthorizer:
    """The zip deploy stage resolves, passes and snapshots the authorizer.

    All three Runtime deployers share the resolve→_kwargs→record pattern; the
    zip stage is exercised end-to-end and the others are covered by the shared
    wrapper tests above plus their own env tests.
    """

    def _deploy(self, monkeypatch, db, *, spec_auth=None, workspace_auth=None,
                existing=None, mode="create"):
        from types import SimpleNamespace

        from app.deployer import zip_runtime as zr
        from app.deployer.pipeline import StageContext
        from tests.conftest import ws_ctx

        if workspace_auth is not None:
            row = db.get(Workspace, DEFAULT_WORKSPACE_ID)
            svc.set_workspace_default(db, row, workspace_auth)

        spec = AgentSpec(
            name="ia-deploy-agent", method="studio", system_prompt="s",
            code="print('x')", memory={"short_term": False, "long_term": False},
            inbound_auth=spec_auth,
        )
        agent = Agent(
            workspace_id=DEFAULT_WORKSPACE_ID, name="ia-deploy-agent",
            method="studio", status="deploying", spec=spec.model_dump(),
            **(existing or {}),
        )
        db.add(agent)
        db.commit()
        agent_id = agent.id

        stub = StubRuntimeControl()
        monkeypatch.setattr(zr, "control_client", lambda _ws=None: stub)
        monkeypatch.setattr(zr, "get_settings", lambda: SimpleNamespace(
            account_id="111122223333", region="us-west-2",
            resources={"artifacts_bucket": "bkt", "execution_role_arn": "arn:role"},
        ))
        ctx = StageContext(agent_id=agent_id, deployment_id="d-ia", job_id="j-ia",
                           workspace=ws_ctx({"artifacts_bucket": "bkt",
                                             "execution_role_arn": "arn:role"}))
        if mode == "update":
            ctx.scratch["mode"] = "update"
        fresh = SessionLocal()
        agent = fresh.get(Agent, agent_id)
        fresh.close()
        zr._stage_deploy(ctx, agent)
        reloaded = SessionLocal()
        row = reloaded.get(Agent, agent_id)
        reloaded.close()
        return stub, row

    def test_create_with_explicit_jwt(self, monkeypatch, db):
        stub, row = self._deploy(monkeypatch, db, spec_auth=jwt_auth())
        config = stub.created_with["authorizerConfiguration"]["customJWTAuthorizer"]
        assert config["discoveryUrl"] == DISCOVERY
        assert row.inbound_auth_mode == "jwt"
        assert row.inbound_auth_config["jwt"]["allowed_clients"] == ["client-a"]

    def test_create_inherits_workspace_jwt_default(self, monkeypatch, db):
        stub, row = self._deploy(monkeypatch, db, workspace_auth=jwt_auth())
        assert "authorizerConfiguration" in stub.created_with
        assert row.inbound_auth_mode == "jwt"

    def test_create_iam_omits_the_field(self, monkeypatch, db):
        stub, row = self._deploy(monkeypatch, db)
        assert "authorizerConfiguration" not in stub.created_with
        assert row.inbound_auth_mode == "iam"
        assert row.inbound_auth_config is None

    def test_update_iam_to_jwt(self, monkeypatch, db):
        stub, row = self._deploy(
            monkeypatch, db, spec_auth=jwt_auth(), mode="update",
            existing={"resource_id": "rt-1", "arn": "arn:rt-1", "version": "1",
                      "inbound_auth_mode": "iam"},
        )
        assert "authorizerConfiguration" in stub.updated_with
        assert row.inbound_auth_mode == "jwt"

    def test_update_jwt_to_iam_omits_the_field(self, monkeypatch, db):
        # omitting authorizerConfiguration on update is the JWT→IAM switch
        stub, row = self._deploy(
            monkeypatch, db, spec_auth=InboundAuth(mode="iam"), mode="update",
            existing={"resource_id": "rt-1", "arn": "arn:rt-1", "version": "1",
                      "inbound_auth_mode": "jwt",
                      "inbound_auth_config": jwt_auth().model_dump()},
        )
        assert "authorizerConfiguration" not in stub.updated_with
        assert row.inbound_auth_mode == "iam"
        assert row.inbound_auth_config is None

    def test_update_jwt_config_change(self, monkeypatch, db):
        changed = jwt_auth(allowed_clients=["client-b"])
        stub, row = self._deploy(
            monkeypatch, db, spec_auth=changed, mode="update",
            existing={"resource_id": "rt-1", "arn": "arn:rt-1", "version": "1",
                      "inbound_auth_mode": "jwt",
                      "inbound_auth_config": jwt_auth().model_dump()},
        )
        sent = stub.updated_with["authorizerConfiguration"]["customJWTAuthorizer"]
        assert sent["allowedClients"] == ["client-b"]
        assert row.inbound_auth_config["jwt"]["allowed_clients"] == ["client-b"]

    def test_spec_iam_pins_against_workspace_jwt_default(self, monkeypatch, db):
        stub, row = self._deploy(
            monkeypatch, db,
            spec_auth=InboundAuth(mode="iam"), workspace_auth=jwt_auth())
        assert "authorizerConfiguration" not in stub.created_with
        assert row.inbound_auth_mode == "iam"


# ---------------------------------------------------------------------------
# IAM: the JWT workload-token grant
# ---------------------------------------------------------------------------

class TestJwtIamGrant:
    def _statements(self, spec: AgentSpec, *, inbound_jwt: bool):
        from app.services.agent_iam import RoleContext, policy_document

        ctx = RoleContext(
            account_id="111122223333", region="us-west-2", artifacts_bucket="bkt",
            ecr_repo_arn="arn:aws:ecr:us-west-2:111122223333:repository/r",
            memory_id="mem-1")
        return policy_document(spec, ctx, inbound_jwt=inbound_jwt)["Statement"]

    def test_jwt_mode_gets_the_workload_token_grant(self):
        spec = AgentSpec(name="ia-role-a", method="zip_runtime", system_prompt="s")
        statements = self._statements(spec, inbound_jwt=True)
        grant = next(s for s in statements if s["Sid"] == "InboundJwtWorkloadToken")
        assert "bedrock-agentcore:GetWorkloadAccessTokenForJWT" in grant["Action"]
        assert all(":workload-identity-directory/" in r for r in grant["Resource"])

    def test_iam_mode_gets_nothing_extra(self):
        spec = AgentSpec(name="ia-role-b", method="zip_runtime", system_prompt="s")
        statements = self._statements(spec, inbound_jwt=False)
        assert not any(s["Sid"] == "InboundJwtWorkloadToken" for s in statements)

    def test_gateway_agents_already_carry_the_action(self):
        # the family-wide grant subsumes it — no duplicate statement
        spec = AgentSpec(
            name="ia-role-c", method="zip_runtime", system_prompt="s",
            tools=[{"type": "gateway", "name": "gw"}])
        statements = self._statements(spec, inbound_jwt=True)
        assert not any(s["Sid"] == "InboundJwtWorkloadToken" for s in statements)
        family = next(s for s in statements if s["Sid"] == "AgentCoreWorkloadIdentity")
        assert "bedrock-agentcore:GetWorkloadAccessTokenForJWT" in family["Action"]


# ---------------------------------------------------------------------------
# bearer invoke path
# ---------------------------------------------------------------------------


class FakeBearerResponse:
    """Stub of the httpx streaming response the bearer transport reads."""

    def __init__(self, status_code=200, body=b"", content_type="application/json",
                 sse_lines=None):
        self.status_code = status_code
        self.headers = {"content-type": content_type}
        self._body = body
        self._sse_lines = sse_lines

    def read(self):
        return self._body

    def iter_lines(self):
        yield from (self._sse_lines or [])


def _opener(response, captured):
    from contextlib import contextmanager

    @contextmanager
    def open_response(url, headers, body):
        captured.update({"url": url, "headers": headers, "body": body})
        yield response

    return open_response


class TestBearerTransport:
    ARN = "arn:aws:bedrock-agentcore:us-west-2:111122223333:runtime/demo-AbCdEf1234"

    def test_url_headers_and_body(self):
        from app.services.agentcore import runtime as rt

        captured: dict = {}
        response = FakeBearerResponse(body=b'{"result": "hello"}')
        result = rt.invoke_runtime_text_bearer(
            "us-west-2", self.ARN, "tok-123", "hi",
            session_id="s" * 40, http_response=_opener(response, captured),
        )
        assert result == {"text": "hello", "session_id": "s" * 40}
        assert captured["url"] == (
            "https://bedrock-agentcore.us-west-2.amazonaws.com/runtimes/"
            + "arn%3Aaws%3Abedrock-agentcore%3Aus-west-2%3A111122223333%3A"
            "runtime%2Fdemo-AbCdEf1234/invocations?qualifier=DEFAULT"
        )
        assert captured["headers"]["Authorization"] == "Bearer tok-123"
        assert captured["headers"][rt.BEARER_SESSION_HEADER] == "s" * 40
        body = json.loads(captured["body"])
        assert body == {"prompt": "hi", "actor_id": "default"}

    def test_sse_stream_parses_deltas(self):
        from app.services.agentcore import runtime as rt

        response = FakeBearerResponse(
            content_type="text/event-stream",
            sse_lines=[
                'data: {"event": "tool", "name": "search", "id": "t1"}', "",
                'data: {"event": "delta", "text": "par"}', "",
                'data: {"event": "delta", "text": "tial"}', "",
            ],
        )
        events = list(rt.stream_runtime_events_bearer(
            "us-west-2", self.ARN, "tok", "hi", http_response=_opener(response, {}),
        ))
        assert [e["event"] for e in events] == ["tool", "delta", "delta"]
        assert "".join(
            e["data"]["text"] for e in events if e["event"] == "delta") == "partial"

    @pytest.mark.parametrize("status", [401, 403])
    def test_auth_rejection_is_typed(self, status):
        from app.services.agentcore import runtime as rt

        response = FakeBearerResponse(status_code=status, body=b'{"message":"nope"}')
        with pytest.raises(rt.RuntimeBearerAuthError) as excinfo:
            rt.invoke_runtime_text_bearer(
                "us-west-2", self.ARN, "bad", "hi", http_response=_opener(response, {}))
        assert excinfo.value.status_code == status

    def test_other_http_error_is_generic(self):
        from app.services.agentcore import runtime as rt

        response = FakeBearerResponse(status_code=500, body=b"boom")
        with pytest.raises(RuntimeError, match="HTTP 500"):
            rt.invoke_runtime_text_bearer(
                "us-west-2", self.ARN, "tok", "hi", http_response=_opener(response, {}))


class TestInvokeDispatch:
    """invoke_agent_text / invoke_agent_events pick Bearer for JWT-mode agents."""

    def _jwt_agent(self, db) -> Agent:
        agent = Agent(
            workspace_id=DEFAULT_WORKSPACE_ID, name="jwt-dispatch-agent",
            method="zip_runtime", status="active",
            resource_id="rt-1",
            arn="arn:aws:bedrock-agentcore:us-west-2:111122223333:runtime/x-1",
            spec={"name": "jwt-dispatch-agent", "protocol": "http"},
            inbound_auth_mode="jwt",
            inbound_auth_config=jwt_auth().model_dump(),
        )
        db.add(agent)
        db.commit()
        return agent

    def test_jwt_agent_uses_bearer_with_caller_token(self, db, monkeypatch):
        from app.services import invoke as invoke_service

        calls: dict = {}

        def fake_bearer(region, arn, token, prompt, **kwargs):
            calls.update({"region": region, "arn": arn, "token": token})
            return {"text": "ok", "session_id": "sid"}

        monkeypatch.setattr(
            invoke_service.rt, "invoke_runtime_text_bearer", fake_bearer)
        agent = self._jwt_agent(db)
        result = invoke_service.invoke_agent_text(
            agent, "hi", bearer_token="user-jwt")
        assert result["text"] == "ok"
        assert calls["token"] == "user-jwt"

    def test_jwt_agent_falls_back_to_m2m(self, db, monkeypatch):
        from app.services import invoke as invoke_service

        calls: dict = {}
        monkeypatch.setattr(
            invoke_service.inbound_auth_service, "m2m_bearer_token",
            lambda workspace: "m2m-token")
        monkeypatch.setattr(
            invoke_service.rt, "invoke_runtime_text_bearer",
            lambda region, arn, token, prompt, **kwargs: (
                calls.update({"token": token}) or {"text": "ok", "session_id": "s"}))
        agent = self._jwt_agent(db)
        invoke_service.invoke_agent_text(agent, "hi")
        assert calls["token"] == "m2m-token"

    def test_jwt_agent_without_m2m_fails_named_never_sigv4(self, db, monkeypatch):
        from app.services import invoke as invoke_service

        def sigv4_forbidden(*args, **kwargs):
            raise AssertionError("SigV4 invoke must not run for a JWT-mode agent")

        monkeypatch.setattr(
            invoke_service.rt, "invoke_runtime_text", sigv4_forbidden)
        # the default workspace resources carry no m2m_client_id in tests
        agent = self._jwt_agent(db)
        with pytest.raises(AppError) as excinfo:
            invoke_service.invoke_agent_text(agent, "hi")
        assert excinfo.value.code == "identity.m2m_unavailable"

    def test_auth_rejection_maps_to_hinted_403(self, db, monkeypatch):
        from app.services import invoke as invoke_service
        from app.services.agentcore import runtime as rt_mod

        def rejecting(region, arn, token, prompt, **kwargs):
            raise rt_mod.RuntimeBearerAuthError(403, "denied")

        monkeypatch.setattr(
            invoke_service.rt, "invoke_runtime_text_bearer", rejecting)
        agent = self._jwt_agent(db)
        with pytest.raises(AppError) as excinfo:
            invoke_service.invoke_agent_text(agent, "hi", bearer_token="tok")
        assert excinfo.value.code == "agent.inbound_auth_rejected"
        assert excinfo.value.detail["allowed_clients"] == ["client-a"]

    def test_events_use_bearer_stream(self, db, monkeypatch):
        from app.services import invoke as invoke_service

        def fake_stream(region, arn, token, prompt, **kwargs):
            yield {"event": "delta", "data": {"text": "streamed"}}

        monkeypatch.setattr(
            invoke_service.rt, "stream_runtime_events_bearer", fake_stream)
        agent = self._jwt_agent(db)
        events = list(invoke_service.invoke_agent_events(
            agent, "hi", bearer_token="tok"))
        assert events == [{"event": "delta", "data": {"text": "streamed"}}]

    def test_iam_agent_is_untouched(self, db, monkeypatch):
        from app.services import invoke as invoke_service

        def bearer_forbidden(*args, **kwargs):
            raise AssertionError("bearer path must not run for an IAM agent")

        monkeypatch.setattr(
            invoke_service.rt, "invoke_runtime_text_bearer", bearer_forbidden)
        monkeypatch.setattr(
            invoke_service.rt, "invoke_runtime_text",
            lambda client, arn, prompt, **kwargs: {"text": "sig", "session_id": "s"})
        monkeypatch.setattr(invoke_service, "data_client", lambda ws: object())
        agent = Agent(
            workspace_id=DEFAULT_WORKSPACE_ID, name="iam-dispatch-agent",
            method="zip_runtime", status="active", resource_id="rt-2",
            arn="arn:rt-2", spec={"name": "iam-dispatch-agent"},
        )
        db.add(agent)
        db.commit()
        assert invoke_service.invoke_agent_text(agent, "hi")["text"] == "sig"


# ---------------------------------------------------------------------------
# migration: the new columns reach an upgraded ledger
# ---------------------------------------------------------------------------

def test_inbound_auth_columns_are_migrated(tmp_path):
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'old.db'}")
    db_module.Base.metadata.create_all(bind=engine)
    with engine.begin() as conn:
        conn.execute(sa.text("ALTER TABLE workspaces DROP COLUMN settings"))
        conn.execute(sa.text("ALTER TABLE agents DROP COLUMN inbound_auth_mode"))
        conn.execute(sa.text("ALTER TABLE agents DROP COLUMN inbound_auth_config"))
    assert db_module.schema_drift(engine)  # proves the drop took
    db_module._migrate(engine)
    assert db_module.schema_drift(engine) == {}
