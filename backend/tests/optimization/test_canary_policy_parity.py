"""N12-obs45 — a canary CANDIDATE version is minted with the deploy path's building blocks
(``canary_infra.mint_candidate_version`` reuses ``stage_policy``, ``_generate_code`` /
``render_harness_bundle``, ``runtime_environment``, ``inbound_auth.resolve_for_agent`` →
``authorizer_configuration``). Global parity (2026-10-27): no deploy-time narrowing and no
runtime guard exist any more, so the candidate's environment and code are exactly what a deploy
of the same spec renders whatever the ACTIVE set says; the mint never writes ``policy_applied``
(``policy_stale`` cannot flip); ``complete`` / ``rollback`` snapshot the promoted version's deploy
policy context (the principal carrier). Hermetic: FakeS3 through the funnel, fake pip, MagicMock
control.
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import app.optimization.canary_infra as infra
import app.optimization.canary_service as canary_svc
from app.core import config
from app.core.config import get_settings
from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.deployer import harness_local
from app.deployer.environment import runtime_environment
from app.deployer.pipeline import StageContext
from app.deployer.zip_runtime import _generate_code
from app.models.ledger import Agent
from app.models.policy import PolicyChange
from app.schemas.agent import AgentSpec
from app.services import (
    aws_clients,
    inbound_auth,
    policy_backend,
    policy_cedar,
    policy_enforcement,
)
from app.services.policy_cedar import CedarEngine
from app.services.policy_enforcement import DeployPolicy
from tests.conftest import ws_ctx
from tests.optimization.test_runtime_canaries import (
    _abtest_artifact,
    _mk_canary,
    _reload,
    _reload_agent,
    _round,
    _setup_artifact,
)
from tests.policy_fakes import FakeS3
from tests.test_policy_deploy_narrowing import (
    CREATE_PAYOUT,
    FORBID_PAYOUT,
    GET_EMPLOYEE,
    PERMIT_HR,
    RESOURCES,
    _agent,
    _gateway_spec,
    _get,
    _publish,
    _set_mode,
)

WS = DEFAULT_WORKSPACE_ID
# the deploy fixtures' resource map + the Gateway id the Harness renderer needs (R9)
RES = {**RESOURCES, "gateway_id": "launchpad-gw-abcdefghij"}
BUCKET = RES["artifacts_bucket"]
RUNTIME_ARN = "arn:aws-cn:bedrock-agentcore:cn-northwest-1:111122223333:runtime/rt-1"
LIVE_ROLE = "arn:aws-cn:iam::111122223333:role/launchpad-agent-hr-agent"
METHODS = ("zip_runtime", "harness", "studio")


# ── fixtures ───────────────────────────────────────────────────────────────


@pytest.fixture
def engine():
    eng = CedarEngine()
    policy_backend.set_backend(eng)
    return eng


@pytest.fixture
def s3(monkeypatch) -> FakeS3:
    fake = FakeS3()
    monkeypatch.setattr(aws_clients, "client", lambda service, ctx, **kw: fake)
    monkeypatch.setattr(aws_clients, "get_session", lambda *a, **k: pytest.fail("session"))
    return fake


@pytest.fixture
def workspace():
    return ws_ctx(RES, id=WS)


@pytest.fixture
def knob(monkeypatch):
    def _set(name: str, value: str) -> None:
        monkeypatch.setenv(f"LAUNCHPAD_{name.upper()}", value)
        config.get_settings.cache_clear()

    yield _set
    config.get_settings.cache_clear()


def _interceptor(monkeypatch, on: bool) -> None:
    """Both halves of the interceptor switch: what decides the principal carrier."""
    monkeypatch.setattr(get_settings(), "policy_gateway_interceptor", on)
    policy_cedar.invalidate()


# ── helpers ────────────────────────────────────────────────────────────────


def _fake_pip(_cmd, **_kwargs):
    """Both subprocesses build_zip drives: the uv resolve leaves a lockfile at `-o`, the
    pip install is a no-op."""
    if "compile" in _cmd:
        Path(_cmd[_cmd.index("-o") + 1]).write_text(
            "probe-pkg==1.0.0 \\\n    --hash=sha256:" + "e" * 64 + "\n", encoding="utf-8",
        )
    return SimpleNamespace(returncode=0, stderr="")


def _control(v_current: str = "3", v_candidate: str = "4") -> MagicMock:
    control = MagicMock()
    control.get_agent_runtime.return_value = {
        "agentRuntimeVersion": v_current, "status": "READY",
        "agentRuntimeArn": RUNTIME_ARN, "agentRuntimeName": "hr_agent", "roleArn": LIVE_ROLE,
    }
    control.update_agent_runtime.return_value = {
        "agentRuntimeVersion": v_candidate, "status": "READY", "agentRuntimeArn": RUNTIME_ARN,
    }
    return control


def _spec_for(method: str, **over) -> dict:
    """A gateway-tooled spec of ``method`` (the studio canvas supplies its own code)."""
    extra = {"code": "print('canvas')\n"} if method == "studio" else {}
    return _gateway_spec(tools=[GET_EMPLOYEE, CREATE_PAYOUT], method=method, **extra, **over)


def _deployed_agent(method: str, *, carrier: bool = False) -> str:
    """An ACTIVE agent whose last deploy stamped ``policy_applied`` (the deploy stage's
    snapshot: the mode and whether the live version renders the carrier)."""
    applied = {"mode": "enforce", "applied_at": "2026-10-27T00:00:00+00:00", "job_id": "j-0"}
    if carrier:
        applied["carrier"] = True
    return _agent(_spec_for(method), agent_id=f"ag-{method}", name="hr-agent", version="3",
                  policy_applied=applied)


def _mint(agent_id: str, spec: AgentSpec, workspace, tmp_path, *, control=None, role="candidate",
          logged: list[str] | None = None, stage_ctx: StageContext | None = None):
    """Run the mint with every AWS seam stubbed; returns (control, uploads, result)."""
    control = control or _control()
    uploads: list[tuple[str, str, str]] = []
    db = SessionLocal()
    try:
        agent = db.get(Agent, agent_id)
        result = infra.mint_candidate_version(
            agent=agent, edited_spec=spec, control_client=control, workspace=workspace,
            canary_id="can45", role=role, pip_runner=_fake_pip,
            uploader=lambda local, bucket, key: uploads.append((local, bucket, key)),
            build_root=tmp_path, log=(logged.append if logged is not None else lambda _m: None),
            stage_ctx=stage_ctx,
        )
    finally:
        db.close()
    return control, uploads, result


def _publish_hr_policies(workspace) -> str:
    _publish(workspace, "permit_hr", PERMIT_HR)
    forbid = _publish(workspace, "forbid_payout", FORBID_PAYOUT)
    _set_mode("enforce")
    return forbid


def _bundle_keys(s3: FakeS3) -> list[str]:
    return sorted(k for _b, k in s3.objects if k.startswith("policy/bundles/"))


# ── the candidate is exactly the deploy path's rendering — every method ─────


@pytest.mark.parametrize("method", METHODS)
def test_candidate_env_and_code_equal_the_deploy_path_whatever_the_policy_set(
    method, engine, s3, workspace, tmp_path,
):
    """Global parity: an ACTIVE set with a forbid changes NOTHING a candidate renders — every
    declared tool stays in the environment, no policy key, no guard module, no bundle write."""
    agent_id = _deployed_agent(method)
    _publish_hr_policies(workspace)
    spec = AgentSpec(**_spec_for(method, system_prompt="candidate"))
    logged: list[str] = []
    control, uploads, _ = _mint(agent_id, spec, workspace, tmp_path, logged=logged)
    up = control.update_agent_runtime.call_args.kwargs
    assert up["environmentVariables"] == runtime_environment(
        spec, RES, workload_name="rt-1", agent_id=agent_id,
    )
    env = up["environmentVariables"]
    assert env["LAUNCHPAD_GATEWAY_TOOLS"] == f"{GET_EMPLOYEE},{CREATE_PAYOUT}"
    assert not any(k.startswith("LAUNCHPAD_POLICY_") for k in env)
    assert _bundle_keys(s3) == []
    with zipfile.ZipFile(uploads[0][0]) as zf:
        main_py = zf.read("main.py").decode("utf-8")
        assert "launchpad_policy_guard.py" not in zf.namelist()
    if method == "harness":
        assert main_py == harness_local.render_harness_bundle(spec, workspace).files["main.py"]
    else:
        assert main_py == _generate_code(spec)[0]
    assert "policy_guard_hooks" not in main_py
    assert _get(agent_id).policy_applied["mode"] == "enforce"  # the mint never writes the row
    assert any("2 Gateway tool pattern(s) rendered" in line for line in logged), logged


@pytest.mark.parametrize("method", METHODS)
def test_flag_off_candidate_is_byte_identical_to_the_plain_mint(method, s3, workspace, tmp_path):
    """No engine registered (the policy module inactive): the mint sends exactly what it
    sent before N12-obs45 — plain env, plain code, no authorizer."""
    agent_id = _agent(_spec_for(method), agent_id=f"ag-{method}", name="hr-agent", version="3")
    spec = AgentSpec(**_spec_for(method, system_prompt="new"))
    control, uploads, _ = _mint(agent_id, spec, workspace, tmp_path)
    up = control.update_agent_runtime.call_args.kwargs
    assert up["environmentVariables"] == runtime_environment(
        spec, RES, workload_name="rt-1", agent_id=agent_id,
    )
    assert not any(k.startswith("LAUNCHPAD_POLICY_") for k in up["environmentVariables"])
    assert "authorizerConfiguration" not in up
    assert _bundle_keys(s3) == []
    with zipfile.ZipFile(uploads[0][0]) as zf:
        main_py = zf.read("main.py").decode("utf-8")
    if method == "harness":
        assert main_py == harness_local.render_harness_bundle(spec, workspace).files["main.py"]
    else:
        assert main_py == _generate_code(spec)[0]
    assert _get(agent_id).policy_applied is None


# ── inbound authorizer ─────────────────────────────────────────────────────


def test_candidate_carries_the_agents_inbound_authorizer(monkeypatch, s3, workspace, tmp_path):
    """The candidate echoes the authorizer the deploy stage resolves for THIS agent through
    the inbound-auth service (not hard-coded): a JWT configuration rides the update, the
    IAM-only default omits the field (an omitted authorizer is how JWT→IAM resets)."""
    agent_id = _agent(_spec_for("zip_runtime"), agent_id="ag-jwt", name="hr-agent", version="3")
    spec = AgentSpec(**_spec_for("zip_runtime"))
    control, _, _ = _mint(agent_id, spec, workspace, tmp_path)
    assert "authorizerConfiguration" not in control.update_agent_runtime.call_args.kwargs

    seen: dict = {}
    jwt = {"customJWTAuthorizer": {"discoveryUrl": "https://idp.example.test/.well-known/"
                                   "openid-configuration", "allowedClients": ["c1"]}}

    def resolve(agent, db=None):
        seen["agent_id"], seen["db"] = agent.id, db
        return SimpleNamespace(mode="jwt")

    monkeypatch.setattr(inbound_auth, "resolve_for_agent", resolve)
    monkeypatch.setattr(
        inbound_auth, "authorizer_configuration",
        lambda resolved: jwt if resolved.mode == "jwt" else None,
    )
    control, _, _ = _mint(agent_id, spec, workspace, tmp_path)
    assert control.update_agent_runtime.call_args.kwargs["authorizerConfiguration"] == jwt
    # resolved for THIS agent, with a ledger session — the deploy stage's call shape
    assert seen["agent_id"] == agent_id and seen["db"] is not None


# ── policy_stale: the mint never touches it; complete / rollback snapshot the carrier ────


def _stale(agent_id: str) -> bool:
    db = SessionLocal()
    try:
        return policy_enforcement.is_stale(db, db.get(Agent, agent_id))
    finally:
        db.close()


def _journal(operation: str) -> list[PolicyChange]:
    db = SessionLocal()
    try:
        return db.query(PolicyChange).filter(PolicyChange.operation == operation).all()
    finally:
        db.close()


def test_mint_leaves_policy_stale_alone_and_complete_snapshots_the_candidate(
    monkeypatch, engine, s3, workspace, tmp_path,
):
    """The interceptor switch turned on after the agent's last deploy makes it `policy_stale`
    (the live version renders no carrier); minting a candidate (which renders it) must not
    clear that — production still serves the old version. `complete` (production = candidate)
    snapshots the candidate's deploy policy context onto the row like the deploy stage does,
    which is when the badge clears."""
    agent_id = _deployed_agent("zip_runtime")
    _publish_hr_policies(workspace)
    _interceptor(monkeypatch, True)
    assert _stale(agent_id) is True
    spec = AgentSpec(**_spec_for("zip_runtime", system_prompt="promoted prompt"))
    stage_ctx = StageContext(agent_id=agent_id, deployment_id="", job_id="can45",
                             workspace=workspace, log=lambda _m: None)
    _mint(agent_id, spec, workspace, tmp_path, stage_ctx=stage_ctx)
    assert _stale(agent_id) is True  # untouched by the mint
    assert "carrier" not in _get(agent_id).policy_applied
    policy = stage_ctx.scratch["deploy_policy"]
    assert isinstance(policy, DeployPolicy) and policy.carrier is True
    snapshot = canary_svc._minted_narrowing(stage_ctx)
    assert snapshot["carrier"] is True and snapshot["mode"] == "enforce"

    # the setup artifact the canary records (what act_setup stores) + a promotable verdict
    setup = {**_setup_artifact(ramp_stage=2), "v_candidate": "4", "deploy_policy": snapshot}
    json.dumps(setup)  # JSON-safe, as the artifacts column requires
    row = _mk_canary(
        agent_id=agent_id,
        artifacts={
            "agent_meta": {"id": agent_id}, "edited_spec": spec.model_dump(), "setup": setup,
            "abtest": _abtest_artifact(ramp_stage=2),
            "rounds": [_round(2, verdict={"verdict": "treatment-wins", "significant": True})],
        },
    )
    monkeypatch.setattr(canary_svc, "control_client", MagicMock)
    result = canary_svc.act_complete(row.id, lambda _m: None, allow_non_significant=False)
    assert result["promoted_version"] == "4"

    agent = _reload_agent(agent_id)
    assert agent.version == "4" and agent.spec["system_prompt"] == "promoted prompt"
    applied = dict(agent.policy_applied)
    assert applied["carrier"] is True and applied["mode"] == "enforce"
    assert applied["job_id"] == row.id
    assert _stale(agent_id) is False  # the live version now renders the carrier
    [entry] = [c for c in _journal("bundle.apply") if c.operator == "canary"]
    assert entry.after == {"carrier": True, "interceptor_decides": False}
    assert _reload(row.id).status == "completed"


def test_complete_without_a_snapshot_keeps_the_row_as_before(monkeypatch, engine, s3, workspace):
    """A canary set up before N12-obs45 (no deploy policy snapshot on its setup) promotes as
    it always did: version + spec flip, `policy_applied` untouched."""
    agent_id = _deployed_agent("zip_runtime")
    row = _mk_canary(
        agent_id=agent_id,
        artifacts={
            "agent_meta": {"id": agent_id}, "edited_spec": _spec_for("zip_runtime"),
            "setup": _setup_artifact(ramp_stage=2), "abtest": _abtest_artifact(ramp_stage=2),
            "rounds": [_round(2, verdict={"verdict": "treatment-wins", "significant": True})],
        },
    )
    monkeypatch.setattr(canary_svc, "control_client", MagicMock)
    canary_svc.act_complete(row.id, lambda _m: None, allow_non_significant=False)
    agent = _reload_agent(agent_id)
    assert agent.version == "2" and agent.policy_applied["job_id"] == "j-0"
    assert _journal("bundle.apply") == []


def test_a_pre_parity_snapshot_key_is_still_read(monkeypatch, engine, s3, workspace):
    """A canary row set up before the parity ruling stored the snapshot under
    `setup.policy_narrowing` (with the retired narrowing fields): `complete` still reads it and
    stamps the carrier it carries, dropping the unknown keys."""
    agent_id = _deployed_agent("zip_runtime")
    legacy = {"mode": "enforce", "active": True, "applied": True, "patterns": [GET_EMPLOYEE],
              "removed": [CREATE_PAYOUT], "guard": True, "bundle_version": 3, "carrier": True}
    row = _mk_canary(
        agent_id=agent_id,
        artifacts={
            "agent_meta": {"id": agent_id}, "edited_spec": _spec_for("zip_runtime"),
            "setup": {**_setup_artifact(ramp_stage=2), "v_candidate": "4",
                      "policy_narrowing": legacy},
            "abtest": _abtest_artifact(ramp_stage=2),
            "rounds": [_round(2, verdict={"verdict": "treatment-wins", "significant": True})],
        },
    )
    monkeypatch.setattr(canary_svc, "control_client", MagicMock)
    canary_svc.act_complete(row.id, lambda _m: None, allow_non_significant=False)
    applied = _reload_agent(agent_id).policy_applied
    assert applied["carrier"] is True and "patterns" not in applied and "guard" not in applied


def test_rollback_snapshots_the_restored_versions_policy_context(monkeypatch, engine, s3,
                                                                 workspace):
    """act_rollback hands the mint a stage context and snapshots the deploy policy context the
    restore was minted with (the roll-forward version is production now)."""
    agent_id = _deployed_agent("zip_runtime", carrier=True)
    row = _mk_canary(
        agent_id=agent_id,
        artifacts={"agent_meta": {"id": agent_id}, "edited_spec": _spec_for("zip_runtime"),
                   "setup": _setup_artifact(), "abtest": _abtest_artifact(), "rounds": []},
    )
    monkeypatch.setattr(canary_svc, "control_client", MagicMock)
    seen: dict = {}

    def fake_mint(*, agent, edited_spec, control_client, workspace, log, canary_id, role,
                  stage_ctx):
        seen["role"], seen["ctx"] = role, stage_ctx
        stage_ctx.scratch["deploy_policy"] = DeployPolicy(
            active=True, mode="enforce", declared=[GET_EMPLOYEE], carrier=False)
        stage_ctx.scratch["principal_carrier_decision"] = (False, False)
        return ("1", "7", f"agents/subject/canary/{canary_id}-{role}.zip")

    monkeypatch.setattr(canary_svc.canary_infra, "mint_candidate_version", fake_mint)
    result = canary_svc.act_rollback(row.id, lambda _m: None)
    assert result["restored_version"] == "7" and seen["role"] == "restore"
    assert isinstance(seen["ctx"], StageContext) and seen["ctx"].agent_id == agent_id
    agent = _reload_agent(agent_id)
    assert agent.version == "7"
    assert "carrier" not in agent.policy_applied  # the restored version renders no carrier
    assert agent.policy_applied["job_id"] == row.id


def test_act_setup_records_the_minted_policy_context_on_the_setup_artifact(monkeypatch,
                                                                            workspace):
    """act_setup passes the mint a stage context and persists `scratch["deploy_policy"]`
    as `setup.deploy_policy` (None when the mint computed none)."""
    agent_id = _agent(_spec_for("zip_runtime"), agent_id="ag-setup", name="subject", version="1",
                      resource_id="subject-abcdefghij")
    row = _mk_canary(
        agent_id=agent_id, status="running", stage="create",
        artifacts={"agent_meta": {"id": agent_id, "arn": RUNTIME_ARN,
                                  "resource_id": "subject-abcdefghij", "runtime_name": "subject"},
                   "edited_spec": _spec_for("zip_runtime", system_prompt="edited"), "rounds": []},
    )
    monkeypatch.setattr(canary_svc, "control_client", MagicMock)
    monkeypatch.setattr(canary_svc.canary_infra, "current_version", lambda c, r: "1")
    monkeypatch.setattr(canary_svc.canary_infra, "ensure_endpoint_ready",
                        lambda *a, **kw: {"status": "READY"})
    monkeypatch.setattr(canary_svc, "_create_variant_eval", lambda **kw: {})

    def fake_mint(**kw):
        kw["stage_ctx"].scratch["deploy_policy"] = DeployPolicy(
            active=True, mode="log_only", declared=[GET_EMPLOYEE], carrier=True)
        kw["stage_ctx"].scratch["principal_carrier_decision"] = (True, False)
        return ("1", "2", f"agents/subject/canary/{kw['canary_id']}-{kw['role']}.zip")

    monkeypatch.setattr(canary_svc.canary_infra, "mint_candidate_version", fake_mint)
    result = canary_svc.act_setup(row.id, lambda _m: None)
    snapshot = result["deploy_policy"]
    assert snapshot["mode"] == "log_only" and snapshot["carrier"] is True
    assert DeployPolicy.from_snapshot(snapshot).declared == [GET_EMPLOYEE]
    assert _reload(row.id).artifacts["setup"]["deploy_policy"] == snapshot
