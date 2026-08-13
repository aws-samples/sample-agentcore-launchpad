"""All three deployers provision the agent's own execution role (T3).

The deploy stages already read `ctx.scratch["execution_role_arn"]` first, so wiring
the provision stage is the whole change — these tests pin that each method actually
does it, and that the documented escape hatch still yields the shared role.
"""

import pytest

from app.core.db import DEFAULT_WORKSPACE_ID
from app.deployer import container as container_method
from app.deployer import harness as harness_method
from app.deployer import zip_runtime as zip_method
from app.deployer.pipeline import StageContext
from app.models.ledger import Agent
from app.services import agent_iam
from tests.test_agent_iam_lifecycle import StubIam

from .conftest import ws_ctx

SHARED = "arn:aws:iam::123456789012:role/launchpad-agent-execution-role"


class Settings:
    region = "us-west-2"
    account_id = "123456789012"
    per_agent_execution_roles = True
    resources = {
        "execution_role_arn": SHARED,
        "artifacts_bucket": "bkt",
        "ecr_repo": "launchpad-agents",
        "memory_id": "launchpad_memory-abc",
    }


class SharedRoleSettings(Settings):
    per_agent_execution_roles = False


def _agent(method="zip_runtime", name="probe") -> Agent:
    return Agent(
        workspace_id=DEFAULT_WORKSPACE_ID, id="abcdef1234",
        name=name,
        method=method,
        spec={"name": name, "method": method, "system_prompt": "p"},
        version="1",
    )


def _ctx(resources: dict | None = None) -> StageContext:
    return StageContext(
        agent_id="abcdef1234", deployment_id="d", job_id="j",
        workspace=ws_ctx(Settings.resources if resources is None else resources),
    )


@pytest.mark.parametrize(
    ("module", "method"),
    [
        (zip_method, "zip_runtime"),
        (container_method, "container"),
        (harness_method, "harness"),
    ],
)
class TestEachDeployerProvisionsItsOwnRole:
    def test_creates_a_per_agent_role_and_records_the_arn(
        self, module, method, monkeypatch
    ):
        monkeypatch.setattr(module, "get_settings", lambda: Settings())
        if module is harness_method:
            # the harness provision stage also touches the KB gateway; no KBs here
            monkeypatch.setattr(module, "control_client", lambda _ws=None: object())
        iam = StubIam()
        agent = _agent(method)
        ctx = _ctx()
        result = module._stage_provision(ctx, agent, iam_client=iam)

        expected = agent_iam.role_name_for(agent.name, agent.id)
        assert ctx.scratch["execution_role_arn"].endswith(f"role/{expected}")
        assert expected in iam.roles
        assert expected in result.detail

    def test_is_idempotent_for_a_resumed_job(self, module, method, monkeypatch):
        monkeypatch.setattr(module, "get_settings", lambda: Settings())
        if module is harness_method:
            monkeypatch.setattr(module, "control_client", lambda _ws=None: object())
        iam = StubIam()
        agent = _agent(method)
        first = module._stage_provision(_ctx(), agent, iam_client=iam)
        second = module._stage_provision(_ctx(), agent, iam_client=iam)
        assert first.detail == second.detail
        assert len(iam.roles) == 1

    def test_the_escape_hatch_yields_the_shared_role(self, module, method, monkeypatch):
        """An over-tight policy fails at invoke time, so an operator needs a way back
        without a code change."""
        monkeypatch.setattr(module, "get_settings", lambda: SharedRoleSettings())
        if module is harness_method:
            monkeypatch.setattr(module, "control_client", lambda _ws=None: object())
        iam = StubIam()
        ctx = _ctx()
        module._stage_provision(ctx, _agent(method), iam_client=iam)
        assert ctx.scratch["execution_role_arn"] == SHARED
        assert iam.roles == {}  # nothing created


class TestSharedRoleFallbackStillValidatesConfig:
    def test_a_missing_shared_role_is_an_actionable_error(self, monkeypatch):
        class NoRole(SharedRoleSettings):
            resources = {}

        monkeypatch.setattr(zip_method, "get_settings", lambda: NoRole())
        with pytest.raises(RuntimeError, match="bootstrap"):
            zip_method._stage_provision(_ctx({}), _agent(), iam_client=StubIam())


class TestTwoAgentsAreIsolated:
    def test_they_get_different_roles(self, monkeypatch):
        """The point of the exercise: one agent's role is not the other's."""
        monkeypatch.setattr(zip_method, "get_settings", lambda: Settings())
        iam = StubIam()
        first = Agent(
            workspace_id=DEFAULT_WORKSPACE_ID, id="1111111111", name="alpha", method="zip_runtime",
            spec={"name": "alpha", "method": "zip_runtime", "system_prompt": "p"},
        )
        second = Agent(
            workspace_id=DEFAULT_WORKSPACE_ID, id="2222222222", name="beta", method="zip_runtime",
            spec={"name": "beta", "method": "zip_runtime", "system_prompt": "p"},
        )
        ctx_a, ctx_b = _ctx(), _ctx()
        zip_method._stage_provision(ctx_a, first, iam_client=iam)
        zip_method._stage_provision(ctx_b, second, iam_client=iam)
        assert ctx_a.scratch["execution_role_arn"] != ctx_b.scratch["execution_role_arn"]
        assert SHARED not in (
            ctx_a.scratch["execution_role_arn"], ctx_b.scratch["execution_role_arn"]
        )
        assert len(iam.roles) == 2


class TestDeleteRemovesTheRole:
    def test_deleting_an_agent_deletes_its_role(self, monkeypatch):
        iam = StubIam()
        agent = _agent()
        agent_iam.ensure_role(
            iam, agent,
            __import__("app.schemas.agent", fromlist=["AgentSpec"]).AgentSpec(**agent.spec),
            agent_iam.role_context(ws_ctx(Settings.resources)),
        )
        assert (
            agent_iam.delete_execution_role(
                agent, Settings(), ws_ctx(Settings.resources), iam=iam
            )
            is True
        )
        assert iam.roles == {}

    def test_the_shared_role_is_never_deleted(self, monkeypatch):
        iam = StubIam()
        assert (
            agent_iam.delete_execution_role(
                _agent(), SharedRoleSettings(), ws_ctx(Settings.resources), iam=iam
            )
            is True
        )
        assert "delete_role" not in " ".join(iam.calls)
