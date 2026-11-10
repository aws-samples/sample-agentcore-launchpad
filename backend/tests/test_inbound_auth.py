"""Inbound auth is AWS_IAM-only in this build (spec D9 / GRAPH-LOOP-SPEC N3)."""

import pytest
from pydantic import ValidationError

from app.schemas.inbound_auth import IAM_INBOUND, InboundAuth, parse_inbound_auth


def test_inbound_auth_only_accepts_iam():
    assert InboundAuth().mode == "iam"
    assert InboundAuth(mode="iam") == IAM_INBOUND


def test_inbound_auth_rejects_jwt():
    with pytest.raises(ValidationError):
        InboundAuth(mode="jwt")


def test_inbound_auth_has_no_jwt_field():
    assert not hasattr(InboundAuth(), "jwt")


def test_parse_inbound_auth_tolerates_a_legacy_jwt_blob():
    """A pre-N3 stored spec may still carry {"mode": "jwt", "jwt": {...}} —
    the lenient read path (parse_inbound_auth) must not 500 the agent list."""
    assert parse_inbound_auth({"mode": "jwt", "jwt": {"discovery_url": "https://x"}}) is None
    assert parse_inbound_auth({"mode": "iam"}) == IAM_INBOUND


def test_no_jwt_symbols_survive_in_the_schema_module():
    import app.schemas.inbound_auth as m

    for name in (
        "JwtInboundConfig", "CustomClaim", "JWT_CAPABLE_METHODS",
        "DISCOVERY_URL_RE", "CLAIM_NAME_RE", "CLAIM_VALUE_RE", "authorizer_configuration",
    ):
        assert not hasattr(m, name), f"{name} should have been deleted"


def test_oidc_issuer_helpers_survive_for_outbound_connections():
    import app.schemas.inbound_auth as m

    assert m.normalize_issuer("https://idp.example.com/") == "https://idp.example.com"
    assert m.issuer_from_discovery_url(
        "https://idp.example.com/.well-known/openid-configuration"
    ) == "https://idp.example.com"


# ---------------------------------------------------------------------------
# AgentSpec placement
# ---------------------------------------------------------------------------


class TestSpecPlacement:
    def test_none_means_inherit_iam(self):
        from app.schemas.agent import AgentSpec

        spec = AgentSpec(name="plain-agent", method="zip_runtime", system_prompt="hi")
        assert spec.inbound_auth is None

    def test_explicit_iam_is_fine_everywhere(self):
        from app.schemas.agent import AgentSpec

        spec = AgentSpec(name="h-agent", method="harness", system_prompt="hi",
                         inbound_auth=InboundAuth(mode="iam"))
        assert spec.inbound_auth.mode == "iam"

    def test_jwt_is_rejected_at_the_model_before_it_ever_reaches_agentspec(self):
        with pytest.raises(ValidationError):
            InboundAuth(mode="jwt")

    def test_a_legacy_jwt_dict_degrades_to_none_instead_of_raising(self):
        """A pre-N3 stored spec's `inbound_auth` may still be a JWT blob —
        reconstructing it (`AgentSpec(**agent.spec)` on redeploy/resume) must
        degrade to IAM rather than raise ValidationError (fix round 1, finding 2)."""
        from app.schemas.agent import AgentSpec

        spec = AgentSpec(
            name="legacy-jwt-agent", method="zip_runtime", system_prompt="hi",
            inbound_auth={"mode": "jwt", "jwt": {"discovery_url": "https://x",
                                                  "allowed_clients": ["c"]}},
        )
        assert spec.inbound_auth is None

    def test_an_unrecognized_inbound_auth_shape_also_degrades_to_none(self):
        from app.schemas.agent import AgentSpec

        spec = AgentSpec(name="legacy-garbage-agent", method="zip_runtime",
                         system_prompt="hi", inbound_auth={"nonsense": True})
        assert spec.inbound_auth is None

    def test_an_iam_dict_still_parses_normally(self):
        from app.schemas.agent import AgentSpec

        spec = AgentSpec(name="iam-dict-agent", method="zip_runtime",
                         system_prompt="hi", inbound_auth={"mode": "iam"})
        assert spec.inbound_auth == InboundAuth(mode="iam")

    def test_a_legacy_jwt_spec_deploys_without_raising(self, monkeypatch, db):
        """A row stored before this build removed the JWT model must still
        redeploy — the zip deploy stage's own `AgentSpec(**agent.spec)`
        reconstruction (zip_runtime.py's lighter fixture, reused from
        TestDeployerOmitsAuthorizer) must not raise ValidationError."""
        from types import SimpleNamespace

        from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
        from app.deployer import zip_runtime as zr
        from app.deployer.pipeline import StageContext
        from app.models.ledger import Agent
        from app.schemas.agent import AgentSpec
        from tests.conftest import ws_ctx

        # A valid spec, as the current model would have written it, with the
        # stored blob then carrying a now-unmodelable inbound_auth — exactly
        # what a pre-N3 row looks like today.
        legacy_spec = AgentSpec(
            name="legacy-jwt-deploy-agent", method="studio", system_prompt="s",
            code="print('x')", memory={"short_term": False, "long_term": False},
        ).model_dump()
        legacy_spec["inbound_auth"] = {
            "mode": "jwt", "jwt": {"discovery_url": "https://x", "allowed_clients": ["c"]},
        }
        agent = Agent(
            workspace_id=DEFAULT_WORKSPACE_ID, name="legacy-jwt-deploy-agent",
            method="studio", status="deploying", spec=legacy_spec,
        )
        db.add(agent)
        db.commit()
        agent_id = agent.id

        monkeypatch.setattr(zr, "control_client", lambda _ws=None: StubRuntimeControl())
        monkeypatch.setattr(zr, "get_settings", lambda: SimpleNamespace(
            account_id="111122223333", region="us-west-2",
            resources={"artifacts_bucket": "bkt", "execution_role_arn": "arn:role"},
        ))
        ctx = StageContext(agent_id=agent_id, deployment_id="d-legacy", job_id="j-legacy",
                           workspace=ws_ctx({"artifacts_bucket": "bkt",
                                             "execution_role_arn": "arn:role"}))
        fresh = SessionLocal()
        agent = fresh.get(Agent, agent_id)
        fresh.close()
        zr._stage_deploy(ctx, agent)  # must not raise ValidationError
        reloaded = SessionLocal()
        row = reloaded.get(Agent, agent_id)
        reloaded.close()
        assert row.status != "failed"
        assert row.inbound_auth_mode == "iam"


# ---------------------------------------------------------------------------
# services.inbound_auth: always IAM
# ---------------------------------------------------------------------------


class TestService:
    def test_display_mode_is_always_iam(self):
        from app.core.db import DEFAULT_WORKSPACE_ID
        from app.models.ledger import Agent
        from app.services import inbound_auth as svc

        agent = Agent(workspace_id=DEFAULT_WORKSPACE_ID, name="x", method="zip_runtime",
                      status="active", spec={})
        assert svc.display_mode(agent) == "iam"

    def test_deployed_inbound_auth_is_always_iam(self):
        from app.core.db import DEFAULT_WORKSPACE_ID
        from app.models.ledger import Agent
        from app.services import inbound_auth as svc

        agent = Agent(workspace_id=DEFAULT_WORKSPACE_ID, name="x", method="zip_runtime",
                      status="active", spec={})
        assert svc.deployed_inbound_auth(agent) == IAM_INBOUND

    def test_resolve_for_agent_is_always_iam(self):
        from app.core.db import DEFAULT_WORKSPACE_ID
        from app.models.ledger import Agent
        from app.services import inbound_auth as svc

        agent = Agent(workspace_id=DEFAULT_WORKSPACE_ID, name="x", method="zip_runtime",
                      status="active", spec={})
        assert svc.resolve_for_agent(agent, db=None) == IAM_INBOUND

    def test_record_deployed_auth_always_writes_iam_none(self):
        from app.core.db import DEFAULT_WORKSPACE_ID
        from app.models.ledger import Agent
        from app.services import inbound_auth as svc

        agent = Agent(workspace_id=DEFAULT_WORKSPACE_ID, name="x", method="zip_runtime",
                      status="active", spec={},
                      inbound_auth_mode="jwt", inbound_auth_config={"mode": "jwt"})
        svc.record_deployed_auth(agent, IAM_INBOUND)
        assert agent.inbound_auth_mode == "iam"
        assert agent.inbound_auth_config is None

    def test_authorizer_configuration_is_always_none(self):
        from app.services import inbound_auth as svc

        assert svc.authorizer_configuration(IAM_INBOUND) is None

    def test_legacy_jwt_ledger_row_reads_back_as_iam_display_mode(self):
        """A pre-N3 row whose deploy-time snapshot still says "jwt" must not
        crash the agent list — this build's display is unconditionally IAM."""
        from app.core.db import DEFAULT_WORKSPACE_ID
        from app.models.ledger import Agent
        from app.services import inbound_auth as svc

        agent = Agent(workspace_id=DEFAULT_WORKSPACE_ID, name="x", method="zip_runtime",
                      status="active", spec={}, inbound_auth_mode="jwt",
                      inbound_auth_config={"mode": "jwt", "jwt": {"discovery_url": "https://x"}})
        assert svc.display_mode(agent) == "iam"
        assert svc.deployed_inbound_auth(agent) == IAM_INBOUND


# ---------------------------------------------------------------------------
# IAM: the (now-dormant) JWT workload-token grant stays a pure function
# ---------------------------------------------------------------------------


def test_policy_document_never_grants_the_retired_inbound_jwt_statement():
    """`agent_iam.policy_document` used to carry an inbound-JWT kwarg (fed from
    the resolved inbound mode, always IAM now) that added a workload-token
    grant statement for a JWT authorizer. Both the kwarg and the statement are
    unreachable once `InboundAuth.mode` is `Literal["iam"]`, and were deleted
    in fix round 1 — pinned here so the retired Sid never comes back by accident."""
    from app.schemas.agent import AgentSpec
    from app.services.agent_iam import RoleContext, policy_document

    ctx = RoleContext(
        account_id="111122223333", region="us-west-2", artifacts_bucket="bkt",
        ecr_repo_arn="arn:aws:ecr:us-west-2:111122223333:repository/r")
    spec = AgentSpec(name="ia-role-a", method="zip_runtime", system_prompt="s")
    statements = policy_document(spec, ctx)["Statement"]
    assert not any(s["Sid"] == "InboundJwtWorkloadToken" for s in statements)
    import inspect

    assert "inbound_jwt" not in inspect.signature(policy_document).parameters


# ---------------------------------------------------------------------------
# deployer: authorizerConfiguration is always omitted (IAM)
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


class TestDeployerOmitsAuthorizer:
    """UpdateAgentRuntime resets an omitted authorizerConfiguration to IAM — the
    zip deploy stage must never send the key, create or update."""

    def _deploy(self, monkeypatch, db, *, existing=None, mode="create"):
        from types import SimpleNamespace

        from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
        from app.deployer import zip_runtime as zr
        from app.deployer.pipeline import StageContext
        from app.models.ledger import Agent
        from app.schemas.agent import AgentSpec
        from tests.conftest import ws_ctx

        spec = AgentSpec(
            name="ia-deploy-agent", method="studio", system_prompt="s",
            code="print('x')", memory={"short_term": False, "long_term": False},
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

    def test_create_omits_the_field(self, monkeypatch, db):
        stub, row = self._deploy(monkeypatch, db)
        assert "authorizerConfiguration" not in stub.created_with
        assert row.inbound_auth_mode == "iam"
        assert row.inbound_auth_config is None

    def test_update_omits_the_field(self, monkeypatch, db):
        stub, row = self._deploy(
            monkeypatch, db, mode="update",
            existing={"resource_id": "rt-1", "arn": "arn:rt-1", "version": "1",
                      "inbound_auth_mode": "iam"},
        )
        assert "authorizerConfiguration" not in stub.updated_with
        assert row.inbound_auth_mode == "iam"
        assert row.inbound_auth_config is None


@pytest.mark.parametrize("wrapper", ["update_code_runtime", "update_container_runtime"])
def test_update_wrappers_forward_an_authorizer_configuration_when_given_one(wrapper):
    """The generic wrapper-level forwarding (`authorizer_configuration` kwarg →
    `params["authorizerConfiguration"]`) is transport infra independent of the
    inbound-auth model — pinned here with a plain dict, not a JWT model."""
    from app.services.agentcore import runtime as rt

    stub = StubRuntimeControl()
    authorizer = {"customJWTAuthorizer": {"discoveryUrl": "https://idp.example/x"}}
    kwargs = {"runtime_id": "rt-1", "role_arn": "arn:role",
              "authorizer_configuration": authorizer}
    if wrapper == "update_code_runtime":
        kwargs.update(s3_bucket="b", s3_key="k")
    else:
        kwargs.update(container_uri="1.dkr.ecr.us-west-2.amazonaws.com/r:t")
    getattr(rt, wrapper)(stub, **kwargs)
    assert stub.updated_with["authorizerConfiguration"] == authorizer


@pytest.fixture
def db():
    from app.core.db import SessionLocal

    session = SessionLocal()
    yield session
    session.close()
