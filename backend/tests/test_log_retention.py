"""N11-obs56 — bounded retention on the per-runtime log group, set by the deploy stage.

The Runtime creates ``/aws/bedrock-agentcore/runtimes/<id>-DEFAULT`` itself with no
retention and nothing in the platform ever configured it. The stage now creates the group
if missing and puts ``Settings.log_group_retention_days`` (default 90) on it — idempotent,
fail-soft, through the funnel ``logs`` client. The real functions are imported HERE at
collection time: conftest's autouse ``hermetic_log_retention`` replaces the module
attribute the stages call, never these references.
"""

import inspect
from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError

from app.core.config import LOG_RETENTION_DAYS_ALLOWED, Settings
from app.deployer import log_retention
from app.deployer.log_retention import apply_runtime_log_retention as real_apply
from app.deployer.log_retention import ensure_log_group_retention, runtime_log_group_name

GROUP = "/aws/bedrock-agentcore/runtimes/rt-1-DEFAULT"


class _Logs:
    def __init__(self, *, exists=False, deny=None, transport=False):
        self.calls: list[tuple[str, dict]] = []
        self.exists, self.deny, self.transport = exists, deny, transport

    def _denied(self, op):
        raise ClientError({"Error": {"Code": "AccessDeniedException", "Message": "m"}}, op)

    def create_log_group(self, **kw):
        self.calls.append(("create", kw))
        if self.transport:
            raise EndpointConnectionError(endpoint_url="https://logs.example")
        if self.deny == "create":
            self._denied("CreateLogGroup")
        if self.exists:
            raise ClientError({"Error": {"Code": "ResourceAlreadyExistsException"}},
                              "CreateLogGroup")

    def put_retention_policy(self, **kw):
        self.calls.append(("retention", kw))
        if self.deny == "retention":
            self._denied("PutRetentionPolicy")


def _ctx(logs, lines):
    return SimpleNamespace(workspace=SimpleNamespace(client=lambda name: {"logs": logs}[name]),
                           log=lines.append)


def _settings(monkeypatch, days):
    monkeypatch.setattr(log_retention, "get_settings",
                        lambda: SimpleNamespace(log_group_retention_days=days))


def test_runtime_log_group_name_is_the_default_endpoint_group_the_teardown_sweeps():
    from app.deployer.teardown import runtime_log_group_prefix

    assert runtime_log_group_name("rt-1") == GROUP
    assert GROUP.startswith(runtime_log_group_prefix("rt-1"))


def test_ensure_creates_the_group_then_puts_the_retention():
    logs, lines = _Logs(), []
    detail = ensure_log_group_retention(logs, GROUP, days=90, log=lines.append)
    assert logs.calls == [("create", {"logGroupName": GROUP}),
                          ("retention", {"logGroupName": GROUP, "retentionInDays": 90})]
    assert detail == f"log group {GROUP} created · retention 90 d"
    assert lines == [f"log group {GROUP} created"]


def test_ensure_is_idempotent_on_an_existing_group():
    """A redeploy (or a runtime that already logged): the create answers
    ResourceAlreadyExistsException and the retention is still (re)applied."""
    logs = _Logs(exists=True)
    detail = ensure_log_group_retention(logs, GROUP, days=30)
    assert [op for op, _ in logs.calls] == ["create", "retention"]
    assert detail == f"log group {GROUP} exists · retention 30 d"


def test_ensure_raises_other_client_errors_for_the_caller():
    with pytest.raises(ClientError):
        ensure_log_group_retention(_Logs(deny="create"), GROUP, days=90)


def test_apply_sets_the_configured_retention_through_the_funnel_logs_client(monkeypatch):
    _settings(monkeypatch, 90)
    logs, lines = _Logs(), []
    detail = real_apply(_ctx(logs, lines), "rt-1")
    assert detail == f"log group {GROUP} created · retention 90 d"
    assert logs.calls[-1] == ("retention", {"logGroupName": GROUP, "retentionInDays": 90})


@pytest.mark.parametrize("logs, reason", [
    (_Logs(deny="create"), "AccessDeniedException"),
    (_Logs(deny="retention"), "AccessDeniedException"),
    (_Logs(transport=True), "EndpointConnectionError"),
])
def test_apply_is_fail_soft_and_names_the_grant(monkeypatch, logs, reason):
    """The deploy is READY by then: a denied or failed call is one WARN line, never a
    failed deploy, and the line says which grant the deploy role lacks."""
    _settings(monkeypatch, 90)
    detail = real_apply(_ctx(logs, []), "rt-1")
    assert detail.startswith(f"log-group retention WARN: {GROUP} keeps the account default")
    assert reason in detail
    assert "logs:CreateLogGroup + logs:PutRetentionPolicy" in detail and "N11-obs56" in detail


def test_apply_is_disabled_at_zero_without_touching_the_client(monkeypatch):
    _settings(monkeypatch, 0)
    boom = SimpleNamespace(workspace=SimpleNamespace(
        client=lambda name: pytest.fail("no client when disabled")), log=lambda _m: None)
    assert real_apply(boom, "rt-1") == (
        f"log-group retention: disabled (log_group_retention_days=0) — {GROUP} unbounded"
    )


def test_setting_defaults_to_ninety_days_and_accepts_only_api_values(monkeypatch):
    monkeypatch.delenv("LAUNCHPAD_LOG_GROUP_RETENTION_DAYS", raising=False)
    assert Settings().log_group_retention_days == 90
    assert 90 in LOG_RETENTION_DAYS_ALLOWED and 0 in LOG_RETENTION_DAYS_ALLOWED
    assert Settings(log_group_retention_days=0).log_group_retention_days == 0
    assert Settings(log_group_retention_days=365).log_group_retention_days == 365
    with pytest.raises(ValueError, match="CloudWatch Logs accepts"):
        Settings(log_group_retention_days=45)
    monkeypatch.setenv("LAUNCHPAD_LOG_GROUP_RETENTION_DAYS", "30")
    assert Settings().log_group_retention_days == 30


def test_zip_deploy_stage_ends_with_the_retention_step(monkeypatch, hermetic_log_retention):
    """The stage calls the module attribute once the runtime is READY (the conftest
    recorder stands in for the real function here) — zip_runtime, studio and the Harness
    method all run this stage."""
    from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
    from app.deployer import zip_runtime as zr
    from app.deployer.pipeline import StageContext
    from app.models.ledger import Agent
    from app.schemas.agent import AgentSpec
    from tests.conftest import ws_ctx
    from tests.test_inbound_auth import StubRuntimeControl

    spec = AgentSpec(name="retention-agent", method="zip_runtime", system_prompt="s",
                     memory={"short_term": False, "long_term": False}).model_dump()
    agent = Agent(workspace_id=DEFAULT_WORKSPACE_ID, name="retention-agent",
                  method="zip_runtime", status="deploying", spec=spec)
    db = SessionLocal()
    db.add(agent)
    db.commit()
    agent_id = agent.id
    db.close()
    monkeypatch.setattr(zr, "control_client", lambda _ws=None: StubRuntimeControl())
    resources = {"artifacts_bucket": "bkt", "execution_role_arn": "arn:role"}
    monkeypatch.setattr(zr, "get_settings", lambda: SimpleNamespace(
        account_id="111122223333", region="us-west-2", resources=resources))
    logged: list[str] = []
    ctx = StageContext(agent_id=agent_id, deployment_id="d-ret", job_id="j-ret",
                       workspace=ws_ctx(resources), log=logged.append)
    fresh = SessionLocal()
    row = fresh.get(Agent, agent_id)
    fresh.close()
    result = zr._stage_deploy(ctx, row)
    assert result.detail.startswith("READY · ")
    assert hermetic_log_retention == ["rt-1"]
    assert "log-group retention: hermetic stub (rt-1)" in logged


def test_byoc_deploy_stage_calls_the_same_step():
    from app.deployer import byoc

    source = inspect.getsource(byoc._stage_deploy)
    assert "log_retention.apply_runtime_log_retention(ctx, runtime_id)" in source
