"""The deploy-time policy context after the Global parity ruling (2026-10-27): no tool
narrowing, no runtime guard — every declared Gateway tool reaches ``LAUNCHPAD_GATEWAY_TOOLS``,
the execution role always keeps ``AgentCoreGatewayInvoke``, and the Gateway interceptor
decides each call. What the hub still does at deploy time: ``policy_enforcement.stage_policy``
(the principal carrier decision), ``mark_applied`` (the snapshot the ``policy_stale`` badge is
derived from), ``is_stale`` (carrier fingerprint only — a publish / retire / mode change never
makes an agent stale), ``apply()`` as one ``deploy_agent`` update job per carrier-stale agent
(+ resume); ``AgentSpec.policy_ids``; the zip deploy stages end to end with stubs.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from app.core import config
from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.core.errors import AppError, NotFoundError
from app.deployer import pipeline
from app.deployer import zip_runtime as zr
from app.deployer.pipeline import StageContext
from app.models.ledger import Agent, Job
from app.models.policy import PolicyChange
from app.routers import agents as agents_router
from app.schemas import agent as agent_schema
from app.schemas.agent import AgentSpec
from app.services import (
    agent_iam,
    aws_clients,
    policy_backend,
    policy_cedar,
    policy_enforcement,
    policy_store,
)
from app.services.agent_iam import RoleContext
from app.services.policy_cedar import CedarEngine
from app.templates.gateway_support import ENV_TOOLS, ENV_URL
from tests.conftest import ws_ctx
from tests.policy_fakes import FakeS3
from tests.test_agent_iam_lifecycle import StubIam
from tests.test_inbound_auth import StubRuntimeControl

WS = DEFAULT_WORKSPACE_ID
GATEWAY_ID = "launchpad-gw-abcdefghij"
RESOURCES = {
    "artifacts_bucket": "launchpad-artifacts-test",
    "execution_role_arn": "arn:aws-cn:iam::111122223333:role/shared",
    "gateway_url": "https://gw.example.test/mcp",
}
CTX = RoleContext(account_id="111122223333", region="cn-northwest-1",
                  artifacts_bucket="launchpad-artifacts-test",
                  ecr_repo_arn="arn:aws-cn:ecr:cn-northwest-1:111122223333:repository/lp")

PERMIT_HR = (
    'permit(principal, action == Launchpad::Action::"call_tool", resource) '
    'when { resource.name like "hr-database___*" };'
)
FORBID_PAYOUT = (
    'forbid(principal, action == Launchpad::Action::"call_tool", '
    'resource == Launchpad::Tool::"hr-database___create_payout");'
)
FORBID_ALL_HR = (
    'forbid(principal, action == Launchpad::Action::"call_tool", resource) '
    'when { resource.target == "hr-database" };'
)
GET_EMPLOYEE, CREATE_PAYOUT = "hr-database___get_employee", "hr-database___create_payout"


# ── fixtures / helpers ─────────────────────────────────────────────────────


@pytest.fixture
def engine():
    eng = CedarEngine()
    policy_backend.set_backend(eng)
    return eng


@pytest.fixture
def s3(monkeypatch):
    fake = FakeS3()
    monkeypatch.setattr(aws_clients, "client", lambda service, ctx, **kw: fake)
    monkeypatch.setattr(aws_clients, "get_session", lambda *a, **k: pytest.fail("session"))
    return fake


@pytest.fixture
def workspace():
    return ws_ctx(RESOURCES, id=WS)


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def no_threads(monkeypatch) -> list[str]:
    """``start_deploy_async`` records instead of starting a worker thread."""
    started: list[str] = []
    monkeypatch.setattr(pipeline, "start_deploy_async", lambda job_id: started.append(job_id))
    monkeypatch.setattr(agents_router, "start_deploy_async",
                        lambda job_id: started.append(job_id))
    return started


def _gateway_spec(name="hr-agent", tools=None, extra_tools=(), **over) -> dict:
    gateway = {"name": "hr-database", "type": "gateway", "config": {"gateway_id": GATEWAY_ID}}
    if tools:
        gateway["config"]["tools"] = list(tools)
    spec = {"name": name, "method": "zip_runtime", "system_prompt": "p",
            "memory": {"short_term": False, "long_term": False},
            "tools": [gateway, *extra_tools], **over}
    return AgentSpec.model_validate(spec).model_dump()


def _agent(spec: dict | None = None, agent_id="ag1", name="hr-agent", status="active",
           resource_id="rt-1", **fields) -> str:
    spec = spec if spec is not None else _gateway_spec(name=name)
    arn = (
        f"arn:aws-cn:bedrock-agentcore:cn-northwest-1:111122223333:runtime/{resource_id}"
        if resource_id else None
    )
    db = SessionLocal()
    try:
        row = Agent(id=agent_id, workspace_id=WS, name=name,
                    method=spec.get("method", "zip_runtime"), status=status, spec=spec,
                    resource_id=resource_id, arn=arn, **fields)
        db.add(row)
        db.commit()
        return row.id
    finally:
        db.close()


def _get(agent_id: str) -> Agent:
    db = SessionLocal()
    try:
        row = db.get(Agent, agent_id)
        db.refresh(row)
        return row
    finally:
        db.close()


def _publish(workspace, name, statement, **extra) -> str:
    db = SessionLocal()
    try:
        policy = policy_store.create_policy(db, WS, name=name, statement=statement,
                                            operator="river", **extra)
        policy_store.publish_policy(db, workspace, policy.id, operator="river")
        return policy.id
    finally:
        db.close()


def _set_mode(mode: str) -> None:
    """Global model: the workspace gateway's attachment to the default engine carries the mode
    (``off`` = no attachment)."""
    from app.models.ledger import Workspace
    from app.models.policy import PolicyEngineAttachment

    db = SessionLocal()
    try:
        ws_row = db.get(Workspace, WS)
        ws_row.resources = {**(ws_row.resources or {}), "gateway_id": GATEWAY_ID}
        db.query(PolicyEngineAttachment).filter_by(gateway_id=GATEWAY_ID).delete()
        if mode != "off":
            engine_row = policy_store.settings_row(db, WS)
            db.add(PolicyEngineAttachment(
                gateway_id=GATEWAY_ID, engine_id=engine_row.id,
                mode="ENFORCE" if mode == "enforce" else "LOG_ONLY"))
        db.commit()
    finally:
        db.close()
    policy_cedar.invalidate()


def _deploy_policy(agent_id: str) -> policy_enforcement.DeployPolicy:
    db = SessionLocal()
    try:
        row = db.get(Agent, agent_id)
        return policy_enforcement.deploy_policy_for(db, row, AgentSpec(**row.spec))
    finally:
        db.close()


def _interceptor(monkeypatch, on: bool) -> None:
    """The platform switch — what makes a governed agent carrier-stale (Global model: no
    workspace half)."""
    monkeypatch.setattr(config.get_settings(), "policy_gateway_interceptor", on)


def _sids(spec: AgentSpec) -> set[str]:
    return {s["Sid"] for s in agent_iam.policy_document(spec, CTX)["Statement"]}


def _journal(db, operation: str) -> list[PolicyChange]:
    return db.query(PolicyChange).filter(PolicyChange.operation == operation).all()


def _stale(agent_id: str) -> bool:
    return policy_enforcement.stale_flag(_get(agent_id))


# ── stale (derived, carrier-only) + mark_applied ──────────────────────────


def test_publish_retire_and_mode_never_make_agents_stale(engine, s3, workspace, db):
    """Global parity: policies apply at call time — the ACTIVE set and the mode have no
    deploy-time consequence, so nothing of them flips `policy_stale`."""
    gateway_agent = _agent()
    plain = _agent({"name": "plain", "method": "zip_runtime", "system_prompt": "p"},
                   agent_id="ag-plain", name="plain")
    preset = _agent(agent_id="ag-sys", name="sys-agent", system_key="eval-worker")
    assert not _stale(gateway_agent)
    pid = _publish(workspace, "permit_hr", PERMIT_HR)
    assert not _stale(gateway_agent) and not _stale(plain) and not _stale(preset)
    assert policy_enforcement.stale_agent_ids(db, WS) == []
    _publish(workspace, "forbid_all", FORBID_ALL_HR)
    policy_store.retire_policy(db, WS, pid, operator="river")
    _set_mode("enforce")
    _set_mode("off")
    assert not _stale(gateway_agent)


def test_the_interceptor_switch_makes_governed_agents_stale_and_mark_applied_clears_them(
    engine, s3, workspace, db, monkeypatch,
):
    """The ONE staleness rule: the carrier a deploy would render now differs from the live
    version's. Only agents with Gateway tools qualify — never a plain agent or a preset."""
    gateway_agent = _agent()
    plain = _agent({"name": "plain", "method": "zip_runtime", "system_prompt": "p"},
                   agent_id="ag-plain", name="plain")
    preset = _agent(agent_id="ag-sys", name="sys-agent", system_key="eval-worker")
    _publish(workspace, "permit_hr", PERMIT_HR)
    _set_mode("enforce")
    row = db.get(Agent, gateway_agent)
    policy = _deploy_policy(gateway_agent)
    assert (policy.active, policy.mode, policy.carrier, policy.declared) == (
        True, "enforce", False, ["hr-database___*"])
    policy_enforcement.mark_applied(db, row, policy, job_id="job-1")
    applied = _get(gateway_agent).policy_applied
    assert set(applied) == {"mode", "applied_at", "job_id"}  # no carrier, nothing narrowed
    assert applied["mode"] == "enforce" and applied["job_id"] == "job-1"
    [journal] = _journal(db, "bundle.apply")
    assert journal.requested == {"agent_id": gateway_agent, "job_id": "job-1", "mode": "enforce"}
    assert journal.after == {"carrier": False, "interceptor_decides": False}
    assert journal.operator == "deployer"
    assert not _stale(gateway_agent)

    _interceptor(monkeypatch, True)  # the switch flips: the next deploy renders the carrier
    assert _stale(gateway_agent) is True
    assert _stale(plain) is False and _stale(preset) is False
    assert policy_enforcement.stale_agent_ids(db, WS) == [gateway_agent]
    db.refresh(row)
    policy = _deploy_policy(gateway_agent)
    assert policy.carrier is True and policy.interceptor_decides is False  # no proven attach
    policy_enforcement.mark_applied(db, row, policy, job_id="job-2")
    assert _get(gateway_agent).policy_applied["carrier"] is True
    assert "interceptor_decides" not in _get(gateway_agent).policy_applied
    assert not _stale(gateway_agent)
    _interceptor(monkeypatch, False)  # and back: stale again until re-deployed without it
    assert _stale(gateway_agent) is True
    db.refresh(row)
    policy_enforcement.mark_applied(db, row, _deploy_policy(gateway_agent), job_id="job-3")
    assert not _stale(gateway_agent) and "carrier" not in _get(gateway_agent).policy_applied


def test_a_caller_without_a_job_decision_gets_the_module_rule(engine, s3, workspace, db,
                                                             monkeypatch):
    """`mark_applied(carrier=None)` (the Runtime-canary promote / rollback) stamps what a
    deploy would render NOW, so such a caller never leaves the agent falsely stale."""
    agent_id = _agent()
    _publish(workspace, "permit_hr", PERMIT_HR)
    _interceptor(monkeypatch, True)
    row = db.get(Agent, agent_id)
    policy_enforcement.mark_applied(db, row, job_id="canary-1", operator="canary")
    assert _get(agent_id).policy_applied["carrier"] is True and not _stale(agent_id)


def test_a_draft_never_makes_anyone_stale(engine, s3, workspace, db):
    agent_id = _agent()
    draft = policy_store.create_policy(db, WS, name="draft", statement=PERMIT_HR,
                                      operator="river")
    assert not _stale(agent_id)
    policy_store.delete_policy(db, WS, draft.id, operator="river")
    assert not _stale(agent_id)


def test_mark_applied_without_an_active_policy_leaves_no_journal_row(db):
    policy_backend.set_backend(None)
    agent_id = _agent()
    row = db.get(Agent, agent_id)
    policy_enforcement.mark_applied(db, row, _deploy_policy(agent_id))
    assert not _stale(agent_id)
    assert _get(agent_id).policy_applied["mode"] == "off"
    assert _journal(db, "bundle.apply") == []


# ── apply ──────────────────────────────────────────────────────────────────


def test_apply_starts_one_update_job_per_carrier_stale_agent(engine, s3, workspace, db,
                                                              no_threads, monkeypatch):
    stale = _agent(agent_id="ag-stale", name="stale")
    busy = _agent(agent_id="ag-busy", name="busy", status="deploying")
    preset = _agent(agent_id="ag-sys", name="sys-agent", system_key="eval-worker")
    _publish(workspace, "permit_hr", PERMIT_HR)
    _set_mode("enforce")
    assert not _stale(stale)  # the publish alone: nothing to re-deploy
    assert policy_enforcement.apply(db, workspace, operator="river") == []
    assert _journal(db, "bundle.apply") == []  # nothing touched, no journal row
    _interceptor(monkeypatch, True)
    assert _stale(stale) and _stale(busy) and not _stale(preset)  # presets are never stale

    out = policy_enforcement.apply(db, workspace, operator="river")
    by_id = {e["agent_id"]: e for e in out}
    assert set(by_id) == {stale, busy}
    assert by_id[busy]["job_id"] is None
    assert by_id[busy]["skipped"] == "a deployment is already in progress"
    entry = by_id[stale]
    assert set(entry) == {"agent_id", "job_id", "carrier"} and entry["carrier"] is True
    assert no_threads == [entry["job_id"]]
    job = db.get(Job, entry["job_id"])
    assert job.type == "deploy_agent" and job.status == "queued"
    assert job.payload["mode"] == "update" and job.payload["skip_register"] is True
    assert job.payload["policy_apply"] == {"mode": "enforce", "carrier": True,
                                           "operator": "river"}
    # the deploy stage clears the flag once the new version is READY, not the request
    assert _get(stale).status == "deploying" and _stale(stale) is True
    [journal] = _journal(db, "bundle.apply")
    assert journal.requested == {"agent_ids": None, "stale_only": True}
    assert {a["agent_id"] for a in journal.after["agents"]} == {stale, busy}
    assert policy_enforcement.apply_summary(out) == {
        "agents": [stale], "job_ids": [entry["job_id"]]}

    # explicit ids: a preset is listed as skipped, an unknown id is 404
    [sys_entry] = policy_enforcement.apply(db, workspace, agent_ids=[preset], operator="river")
    assert sys_entry["job_id"] is None and "exempt" in sys_entry["skipped"]
    with pytest.raises(NotFoundError):
        policy_enforcement.apply(db, workspace, agent_ids=["ghost"], operator="river")


def test_apply_jobs_are_resumed_like_any_deploy_job(engine, s3, workspace, db, no_threads):
    agent_id = _agent()
    _publish(workspace, "permit_hr", PERMIT_HR)
    [entry] = policy_enforcement.apply(db, workspace, agent_ids=[agent_id], operator="river")
    resumed = pipeline.resume_pending_jobs()
    assert entry["job_id"] in resumed and no_threads.count(entry["job_id"]) == 2


# ── AgentSpec.policy_ids ───────────────────────────────────────────────────


def test_policy_ids_is_additive_and_absent_means_clear():
    spec = AgentSpec(name="plain", method="zip_runtime", system_prompt="p")
    assert spec.policy_ids == [] and spec.model_dump()["policy_ids"] == []
    assert AgentSpec.model_fields["policy_ids"].metadata[0].max_length == 20
    source = Path(inspect.getfile(agent_schema)).read_text(encoding="utf-8")
    assert "an ABSENT field on write means CLEAR" in source
    # a stored spec written before the field reads back as []
    old = AgentSpec(**{"name": "old", "method": "zip_runtime", "system_prompt": "p"})
    assert old.policy_ids == []


def test_policy_ids_are_validated_against_the_workspace(engine, db, client, no_threads):
    with pytest.raises(AppError) as exc:
        policy_enforcement.validate_spec_policy_ids(
            db, WS, AgentSpec.model_validate(_gateway_spec(policy_ids=["pol_ghost"])),
        )
    assert exc.value.code == "policy.unknown_policy" and exc.value.status_code == 422
    assert exc.value.detail == {"policy_ids": ["pol_ghost"]}
    draft = policy_store.create_policy(db, WS, name="draft", statement=PERMIT_HR,
                                       operator="river")
    policy_enforcement.validate_spec_policy_ids(
        db, WS, AgentSpec.model_validate(_gateway_spec(policy_ids=[draft.id])),
    )  # a draft / retired id is accepted (dropped with a warning at deploy time)
    from tests.conftest import set_default_resources

    # round 16: a gateway spec needs the workspace Gateway + bucket before any later check;
    # N17-obs487: the spec's gateway tool must be BOUND to that Gateway (config.gateway_id ==
    # resources.gateway_id) before policy_ids are looked at, so the world names GATEWAY_ID
    set_default_resources({"artifacts_bucket": "launchpad-artifacts-test",
                           "gateway_id": GATEWAY_ID})
    res = client.post("/api/agents",
                      json=_gateway_spec(name="new-agent", policy_ids=["pol_ghost"]))
    assert res.status_code == 422 and res.json()["code"] == "policy.unknown_policy"
    assert no_threads == []  # refused before any job


# ── the zip stages end to end (stubs) ──────────────────────────────────────


def _stage_ctx(agent_id: str, logged: list[str]) -> StageContext:
    return StageContext(agent_id=agent_id, deployment_id="d-1", job_id="j-1",
                        workspace=ws_ctx(RESOURCES, id=WS), log=logged.append)


def test_provision_stage_keeps_the_gateway_grant_whatever_the_policy_set(engine, s3, workspace):
    """Global parity: the policy set never shapes IAM — a forbid-all set still leaves
    `AgentCoreGatewayInvoke` on the role (the interceptor refuses the calls)."""
    agent_id = _agent(_gateway_spec(tools=[GET_EMPLOYEE, CREATE_PAYOUT]))
    _publish(workspace, "forbid_all", FORBID_ALL_HR)
    _set_mode("enforce")
    iam = StubIam()
    logged: list[str] = []
    ctx = _stage_ctx(agent_id, logged)
    zr._stage_provision(ctx, _get(agent_id), iam_client=iam)
    [role] = iam.roles.values()
    [capability] = role["policies"].values()
    sids = {s["Sid"] for s in capability["Statement"]}
    assert "AgentCoreGatewayInvoke" in sids and "PolicyBundleRead" not in sids
    assert "policy_narrowing" not in ctx.scratch


def test_deploy_stage_writes_every_declared_tool_and_snapshots_the_carrier(
    engine, s3, workspace, monkeypatch, hermetic_log_retention,
):
    agent_id = _agent(_gateway_spec(tools=[GET_EMPLOYEE, CREATE_PAYOUT]), status="deploying")
    _publish(workspace, "permit_hr", PERMIT_HR)
    _publish(workspace, "forbid_payout", FORBID_PAYOUT)
    _set_mode("enforce")
    assert _stale(agent_id) is False  # nothing to re-deploy: policies decide at call time
    control = StubRuntimeControl()
    monkeypatch.setattr(zr, "control_client", lambda _ws=None: control)
    logged: list[str] = []
    ctx = _stage_ctx(agent_id, logged)
    ctx.scratch.update({"mode": "update", "execution_role_arn": "arn:agent-role",
                        "s3_bucket": "bkt", "s3_key": "agents/hr-agent/deployment_package.zip"})
    result = zr._stage_deploy(ctx, _get(agent_id))
    assert result.detail.startswith("READY")
    env = control.updated_with["environmentVariables"]
    assert env[ENV_TOOLS] == f"{GET_EMPLOYEE},{CREATE_PAYOUT}"  # the forbid narrows nothing
    assert env[ENV_URL] == RESOURCES["gateway_url"]
    assert not any(k.startswith("LAUNCHPAD_POLICY_") for k in env)
    row = _get(agent_id)
    assert _stale(agent_id) is False
    assert set(row.policy_applied) == {"mode", "applied_at", "job_id"}
    assert row.policy_applied["mode"] == "enforce" and row.policy_applied["job_id"] == "j-1"
    db = SessionLocal()
    try:
        [journal] = _journal(db, "bundle.apply")
        assert journal.after == {"carrier": False, "interceptor_decides": False}
    finally:
        db.close()
    assert any("2 Gateway tool pattern(s) rendered · principal carrier off" in line
               for line in logged), logged


def test_deploy_stage_without_a_policy_engine_keeps_the_env_as_before(
    monkeypatch, hermetic_log_retention,
):
    policy_backend.set_backend(None)
    agent_id = _agent(_gateway_spec(tools=[GET_EMPLOYEE]), status="deploying")
    control = StubRuntimeControl()
    monkeypatch.setattr(zr, "control_client", lambda _ws=None: control)
    ctx = _stage_ctx(agent_id, [])
    ctx.scratch.update({"mode": "update", "execution_role_arn": "arn:agent-role"})
    zr._stage_deploy(ctx, _get(agent_id))
    env = control.updated_with["environmentVariables"]
    assert env[ENV_TOOLS] == GET_EMPLOYEE
    assert not any(k.startswith("LAUNCHPAD_POLICY_") for k in env)


def test_deployer_stage_hooks_are_wired():
    from app.deployer import byoc

    for fn in (byoc._stage_generate, byoc._stage_deploy, zr._stage_generate, zr._stage_deploy):
        assert "policy_enforcement.stage_policy(ctx" in inspect.getsource(fn), fn.__name__
    assert "policy_enforcement.mark_applied(db, row, policy" in inspect.getsource(
        byoc._stage_deploy)
    assert "policy_enforcement.mark_applied(db, row, policy" in inspect.getsource(
        zr._stage_deploy)
    for fn in (byoc._stage_provision, zr._stage_provision):
        assert "policy" not in inspect.getsource(fn).replace("policy_enforcement", ""), fn.__name__


def test_knob_defaults(monkeypatch):
    monkeypatch.delenv("LAUNCHPAD_POLICY_EXEMPT_SYSTEM_PRESETS", raising=False)
    settings = config.Settings()
    assert not hasattr(settings, "policy_invoke_fail_mode")  # the invoke gate is gone
    assert not hasattr(settings, "policy_exempt_system_presets")  # amendment 10: no knob
    assert settings.policy_default_mode == "log_only"
