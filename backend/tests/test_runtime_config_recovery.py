"""Sequential publish/restore regressions with versioned, SDK-validated AWS fakes."""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError
from botocore.validate import validate_parameters
from pydantic import ValidationError

from app.core.db import SessionLocal
from app.deployer.runtime_config import (
    captured_runtime_config,
    carry_runtime_config,
    original_runtime_config,
)
from app.models.ledger import Agent, Job, Promotion, ReleaseBundle, SpecSnapshot
from app.optimization import canary_infra, canary_service
from app.schemas.agent import AgentSpec
from app.services import promotion_exec
from app.services.agentcore import runtime as rt
from tests.conftest import TEST_PROJECT, client_error, ws_ctx
from tests.test_runtime_config import BASELINE, CONSUMPTION, MODEL, seed_runtime, spec_input
from tests.test_runtime_config_deployers import (
    ARN,
    RESOURCES,
    LiveControl,
    stage_fixture,
)

CANDIDATE = {**BASELINE, "minimum_memory_mib": 8192}
V1 = {**CONSUMPTION, "platform_version": "V1"}


class VersionedControl(LiveControl):
    """Update really rolls current settings; Get(version) reads an immutable copy."""

    def __init__(self, original=BASELINE):
        super().__init__()
        self.live["agentRuntimeName"] = "billing_rt"
        self.live["platformVersion"] = original["platform_version"]
        self.live.pop("resourceConfigurations", None)
        if original["billing_model"] == "committed_baseline":
            self.live["resourceConfigurations"] = [
                {"minimumMemoryInMiB": original["minimum_memory_mib"]}
            ]
        self.versions = {"1": deepcopy(self.live)}
        self.gets = []
        self.updates = []

    def get_agent_runtime(self, **params):
        validate_parameters(params, MODEL.operation_model("GetAgentRuntime").input_shape)
        assert params["agentRuntimeId"] == "billing_rt-abcdefghij"
        self.gets.append(params)
        if self.failure:
            raise client_error("AccessDeniedException", operation="GetAgentRuntime")
        return deepcopy(self.versions[params["agentRuntimeVersion"]]
                        if "agentRuntimeVersion" in params else self.live)

    def update_agent_runtime(self, **params):
        super().update_agent_runtime(**params)
        self.updates.append(deepcopy(params))
        version = str(len(self.versions) + 1)
        self.live.update(agentRuntimeVersion=version)
        if "platformVersion" in params:
            self.live["platformVersion"] = params["platformVersion"]
        self.live.pop("resourceConfigurations", None)
        if "resourceConfigurations" in params:
            self.live["resourceConfigurations"] = deepcopy(params["resourceConfigurations"])
        self.versions[version] = deepcopy(self.live)
        return {"agentRuntimeArn": ARN, "agentRuntimeVersion": version}


def assert_settings(params, expected):
    assert params["platformVersion"] == expected["platform_version"]
    if expected["billing_model"] == "consumption":
        assert "resourceConfigurations" not in params
    else:
        assert params["resourceConfigurations"] == [
            {"minimumMemoryInMiB": expected["minimum_memory_mib"]}
        ]


@pytest.mark.parametrize("incoming", [None, CONSUMPTION])
def test_carry_keeps_target_choice_but_explicit_consumption_wins_without_mutation(incoming):
    before = {"runtime_config": deepcopy(BASELINE)}
    raw = {"system_prompt": "new", "runtime_config": incoming}
    carried = carry_runtime_config(raw, before)
    assert carried["runtime_config"] == (incoming or BASELINE)
    carried["runtime_config"]["platform_version"] = "V1"
    assert before["runtime_config"] == BASELINE
    assert raw["runtime_config"] == incoming


@pytest.mark.parametrize("detail", [
    {"resourceConfigurations": [{"minimumMemoryInMiB": 2048}]}, {"platformVersion": "V3"},
    {"platformVersion": "V2", "resourceConfigurations": {}},
    {"platformVersion": "V2", "resourceConfigurations": [{"unknown": 2048}]},
    {"platformVersion": "V2", "resourceConfigurations": [{"minimumMemoryInMiB": "2048"}]},
    {"platformVersion": "V1", "resourceConfigurations": [{"minimumMemoryInMiB": 2048}]},
])
def test_capture_refuses_missing_platform_or_unrepresentable_resources(detail):
    with pytest.raises((RuntimeError, ValidationError)):
        captured_runtime_config(detail)


@pytest.mark.parametrize("detail", [{}, {"resourceConfigurations": []}])
def test_capture_reads_pre_platform_runtime_as_v1_consumption(detail):
    # Live: most runtimes not updated since platform versions shipped report neither
    # field. Resources exist only on V2, so this is the documented V1 create default.
    assert captured_runtime_config(detail).model_dump() == V1


def pre_platform_control():
    """A runtime last updated before AWS reported platformVersion at all."""
    control = VersionedControl(V1)
    control.live.pop("platformVersion")
    control.versions["1"].pop("platformVersion")
    return control


@pytest.mark.parametrize("version", [None, "", "DEFAULT", "0", "01", 1, "123456", "１"])
def test_original_version_requires_proof_without_any_read(version):
    control = MagicMock()
    with pytest.raises(RuntimeError, match="not proven"):
        original_runtime_config(control, "billing_rt-abcdefghij", version)
    control.get_agent_runtime.assert_not_called()


def test_original_version_read_mismatch_or_missing_platform_is_not_guessed():
    control = VersionedControl()
    control.versions["1"]["agentRuntimeVersion"] = "2"
    with pytest.raises(RuntimeError, match="did not match"):
        original_runtime_config(control, "billing_rt-abcdefghij", "1")
    control.versions["1"]["agentRuntimeVersion"] = "1"
    control.versions["1"].pop("platformVersion")
    with pytest.raises(RuntimeError, match="platform metadata is missing"):
        original_runtime_config(control, "billing_rt-abcdefghij", "1")


def stub_mint_build(monkeypatch, tmp_path):
    monkeypatch.setattr(canary_infra, "_generate_code", lambda _s: ("print(1)", "test"))
    monkeypatch.setattr(canary_infra, "build_agent_zip", lambda *a, **k: (tmp_path / "x.zip", None))
    monkeypatch.setattr(canary_infra, "_uploader_for", lambda _ws: lambda *a: None)
    monkeypatch.setattr(rt, "wait_runtime_ready", lambda *a, **k: {"status": "READY"})


@pytest.mark.parametrize("original", [BASELINE, CONSUMPTION, V1])
@pytest.mark.parametrize("explicit", [False, True])
def test_candidate_config_change_then_service_rollback_restores_original(
    monkeypatch, tmp_path, original, explicit
):
    agent_id = seed_runtime(original if explicit else None)
    with SessionLocal() as db:
        agent = db.get(Agent, agent_id)
    control = VersionedControl(original)
    stub_mint_build(monkeypatch, tmp_path)
    monkeypatch.setattr(canary_service, "control_client", lambda _ws: control)
    monkeypatch.setattr(canary_service, "data_client", lambda _ws: MagicMock())
    monkeypatch.setattr(canary_service.experiment_service, "_stop_ab_test",
                        lambda *a, **k: {"executionStatus": "STOPPED"})
    candidate = AgentSpec(**spec_input(runtime_config=CANDIDATE, system_prompt="rejected"))
    canary = canary_service.start_canary(agent, candidate, ws_ctx(RESOURCES))
    current, minted, _ = canary_infra.mint_candidate_version(
        agent=agent, edited_spec=candidate, control_client=control,
        workspace=ws_ctx(RESOURCES), canary_id=canary.id,
    )
    assert (current, minted) == ("1", "2")
    assert_settings(control.updates[0], CANDIDATE)
    canary_service._update(canary.id, artifact={"setup": {
        "v_current": current, "v_candidate": minted, "ab_test_id": "ab1",
    }})

    result = canary_service.act_rollback(canary.id, lambda _m: None)

    assert result["restored_version"] == "3"
    assert_settings(control.updates[-1], original)
    immutable_reads = [p for p in control.gets if "agentRuntimeVersion" in p]
    assert immutable_reads == ([] if explicit else [
        {"agentRuntimeId": "billing_rt-abcdefghij", "agentRuntimeVersion": "1"},
    ])
    with SessionLocal() as db:
        restored = db.get(Agent, agent_id)
        assert restored.version == "3" and restored.spec["system_prompt"] != "rejected"
        if explicit:
            assert restored.spec["runtime_config"] == original
        else:
            assert "runtime_config" not in restored.spec  # no ordinary legacy migration


@pytest.mark.parametrize("proof", ["missing_version", "missing_metadata", "denied"])
def test_legacy_canary_recovery_fails_before_build_or_mutation_without_proof(monkeypatch, proof):
    control = VersionedControl()
    if proof == "missing_metadata":
        control.versions["1"].pop("platformVersion")
    if proof == "denied":
        control.failure = True
    monkeypatch.setattr(canary_infra, "_generate_code",
                        lambda *_a: pytest.fail("unproven recovery must not build"))
    with pytest.raises((RuntimeError, ClientError),
                       match="not proven|platform metadata is missing|AccessDenied"):
        canary_infra.mint_candidate_version(
            agent=SimpleNamespace(resource_id="billing_rt-abcdefghij"),
            edited_spec=AgentSpec(**spec_input()), control_client=control,
            workspace=ws_ctx(RESOURCES), canary_id="can1", role="restore",
            original_version=None if proof == "missing_version" else "1",
            uploader=lambda *a: pytest.fail("unproven recovery must not upload"),
        )
    assert control.updates == []



def staged_direct_run(monkeypatch, config, incoming):
    """Use real provision/snapshot + container update paths, stub only AWS/wait."""
    module, ctx, agent, _stub = stage_fixture(monkeypatch, "container", config)
    with SessionLocal() as db:
        bundle = ReleaseBundle(
            workspace_id="default", agent_id=agent.id, agent_name=agent.name,
            method="container", spec=spec_input("container"), artifact={}, digest="a" * 64,
        )
        db.add(bundle)
        db.flush()
        spec = spec_input("container", system_prompt="candidate", **(
            {"runtime_config": incoming} if incoming is not None else {}
        ))
        promotion = Promotion(
            workspace_id="default", target_workspace_id="default", bundle_id=bundle.id,
            status="executing", execution={"spec": spec, "target_agent_id": agent.id},
        )
        db.add(promotion)
        db.commit()
        run = SimpleNamespace(
            promotion_id=promotion.id, target_id="default", target=ws_ctx(RESOURCES),
            action="execute", project_id=TEST_PROJECT, log=lambda *a, **k: None,
            bundle=lambda session: session.get(ReleaseBundle, bundle.id),
        )

    def publish(_run, job_id):
        # _stage_deploy is the real container deployer; no build/IAM/network call.
        with SessionLocal() as db:
            row = db.get(Agent, agent.id)
        module._stage_deploy(ctx, row)
        with SessionLocal() as db:
            db.get(Agent, agent.id).status = "active"
            db.get(Job, job_id).status = "succeeded"
            db.commit()

    monkeypatch.setattr(promotion_exec, "_wait_for_job", publish)
    return run, agent.id, module, publish


@pytest.mark.parametrize("incoming", [None, CONSUMPTION])
def test_direct_promotion_legacy_bundle_keeps_target_explicit_config(monkeypatch, incoming):
    run, agent_id, module, publish = staged_direct_run(monkeypatch, BASELINE, incoming)
    control = VersionedControl()
    monkeypatch.setattr(module, "control_client", lambda _ws: control)
    monkeypatch.setattr(promotion_exec, "control_client",
                        lambda _ws: pytest.fail("explicit previous config needs no extra read"))
    promotion_exec._stage_provision(run)
    with SessionLocal() as db:
        expected = incoming or BASELINE
        assert db.get(Agent, agent_id).spec["runtime_config"] == expected
        assert db.query(SpecSnapshot).one().spec["runtime_config"] == expected
        assert db.get(Promotion, run.promotion_id).execution["previous_spec"][
            "runtime_config"
        ] == BASELINE
    execution = promotion_exec._execution(run.promotion_id)
    publish(run, execution["deploy_job_id"])
    assert_settings(control.updates[-1], expected)


@pytest.mark.parametrize("original", [BASELINE, CONSUMPTION, V1])
@pytest.mark.parametrize("explicit", [False, True])
def test_failed_direct_publish_restores_previous_runtime_settings(
    monkeypatch, original, explicit
):
    run, agent_id, module, publish = staged_direct_run(
        monkeypatch, original if explicit else None, CANDIDATE
    )
    control = VersionedControl(original)
    monkeypatch.setattr(module, "control_client", lambda _ws: control)
    monkeypatch.setattr(promotion_exec, "control_client", lambda _ws: control)
    promotion_exec._stage_provision(run)
    execution = promotion_exec._execution(run.promotion_id)
    assert execution["previous_runtime_config"] == (None if explicit else original)
    if not explicit:
        assert "runtime_config" not in execution["previous_spec"]
    reads_before_publish = len(control.gets)
    assert reads_before_publish == (0 if explicit else 1)
    publish(run, execution["deploy_job_id"])
    assert_settings(control.updates[0], CANDIDATE)

    promotion_exec._revert_direct(run, "smoke")

    assert_settings(control.updates[-1], original)
    assert len(control.updates) == 2
    assert len(control.gets) == reads_before_publish + 2  # mount reads only after capture
    assert not any("agentRuntimeVersion" in p for p in control.gets)
    with SessionLocal() as db:
        agent = db.get(Agent, agent_id)
        assert agent.spec["runtime_config"] == original and agent.status == "active"
        assert agent.spec["system_prompt"] != "candidate"
        assert db.query(SpecSnapshot).filter_by(seq=2).one().spec["runtime_config"] == original
    assert promotion_exec._execution(run.promotion_id)["reverted"] is True


def test_pre_platform_target_promotes_and_restores_v1(monkeypatch):
    run, agent_id, module, publish = staged_direct_run(monkeypatch, None, CANDIDATE)
    control = pre_platform_control()
    monkeypatch.setattr(module, "control_client", lambda _ws: control)
    monkeypatch.setattr(promotion_exec, "control_client", lambda _ws: control)
    promotion_exec._stage_provision(run)
    execution = promotion_exec._execution(run.promotion_id)
    assert execution["previous_runtime_config"] == V1
    publish(run, execution["deploy_job_id"])
    assert_settings(control.updates[0], CANDIDATE)

    promotion_exec._revert_direct(run, "smoke")

    assert_settings(control.updates[-1], V1)
    with SessionLocal() as db:
        assert db.get(Agent, agent_id).spec["runtime_config"] == V1
    assert promotion_exec._execution(run.promotion_id)["reverted"] is True


def test_pre_platform_canary_rollback_restores_v1(monkeypatch, tmp_path):
    agent_id = seed_runtime(None)
    with SessionLocal() as db:
        agent = db.get(Agent, agent_id)
    control = pre_platform_control()
    stub_mint_build(monkeypatch, tmp_path)
    monkeypatch.setattr(canary_service, "control_client", lambda _ws: control)
    monkeypatch.setattr(canary_service, "data_client", lambda _ws: MagicMock())
    monkeypatch.setattr(canary_service.experiment_service, "_stop_ab_test",
                        lambda *a, **k: {"executionStatus": "STOPPED"})
    candidate = AgentSpec(**spec_input(runtime_config=CANDIDATE, system_prompt="rejected"))
    canary = canary_service.start_canary(agent, candidate, ws_ctx(RESOURCES))
    current, minted, _ = canary_infra.mint_candidate_version(
        agent=agent, edited_spec=candidate, control_client=control,
        workspace=ws_ctx(RESOURCES), canary_id=canary.id,
    )
    canary_service._update(canary.id, artifact={"setup": {
        "v_current": current, "v_candidate": minted, "ab_test_id": "ab1",
    }})

    result = canary_service.act_rollback(canary.id, lambda _m: None)

    assert result["restored_version"] == "3"
    assert_settings(control.updates[-1], V1)


def test_existing_direct_execution_without_pre_publish_proof_refuses_recovery(monkeypatch):
    run, agent_id, _module, _publish = staged_direct_run(monkeypatch, CANDIDATE, CANDIDATE)
    previous = spec_input("container", system_prompt="original")
    promotion_exec._patch_execution(
        run.promotion_id, previous_spec=previous, deploy_mode="update", use_canary=False,
    )
    logs = []
    run.log = lambda msg, **kw: logs.append(msg)
    monkeypatch.setattr(promotion_exec, "control_client",
                        lambda _ws: pytest.fail("cannot read rejected current as original"))
    monkeypatch.setattr(promotion_exec.pipeline, "create_deployment",
                        lambda *a, **kw: pytest.fail("unproven recovery must not start"))

    promotion_exec._revert_direct(run, "smoke")

    assert any("not captured before publish" in msg and "recovery requires review" in msg
               for msg in logs)
    assert not promotion_exec._execution(run.promotion_id).get("reverted")
    with SessionLocal() as db:
        assert db.get(Agent, agent_id).spec["runtime_config"] == CANDIDATE
        assert db.query(Job).count() == 0


def test_direct_pre_publish_capture_failure_leaves_target_and_jobs_untouched(monkeypatch):
    run, agent_id, _module, _publish = staged_direct_run(monkeypatch, None, CANDIDATE)
    control = VersionedControl()
    control.failure = True
    monkeypatch.setattr(promotion_exec, "control_client", lambda _ws: control)
    with pytest.raises(ClientError, match="AccessDenied"):
        promotion_exec._stage_provision(run)
    with SessionLocal() as db:
        assert "runtime_config" not in db.get(Agent, agent_id).spec
        assert db.get(Agent, agent_id).status == "active"
        assert db.query(Job).count() == 0
    assert control.updates == []



def stub_canary_setup(monkeypatch):
    service = canary_service.experiment_service
    monkeypatch.setattr(canary_service, "data_client", lambda _ws: MagicMock())
    monkeypatch.setattr(canary_infra, "create_canary_gateway", lambda **kw: {
        "gateway_id": "gw1", "gateway_arn": "arn:gw", "gateway_url": "https://gw",
    })
    monkeypatch.setattr(canary_infra, "ensure_endpoint_ready", lambda *a, **k: {})
    monkeypatch.setattr(service, "create_runtime_target_idempotent", lambda *a, **kw: "target1")
    monkeypatch.setattr(service, "create_online_eval_idempotent", lambda *a, **kw: {
        "onlineEvaluationConfigId": "oe1", "onlineEvaluationConfigArn": "arn:oe1",
    })
    monkeypatch.setattr(canary_service, "_create_target_ab_test",
                        lambda *a, **kw: {"abTestId": "ab1"})
    monkeypatch.setattr(service, "_stop_ab_test", lambda *a, **kw: {"executionStatus": "STOPPED"})
    monkeypatch.setattr(canary_service, "assert_verdict_allows", lambda *a, **kw: None)


@pytest.mark.parametrize("incoming", [None, CONSUMPTION])
@pytest.mark.parametrize("legacy_row", [False, True])
def test_canary_legacy_bundle_carry_survives_setup_completion_and_snapshot(
    monkeypatch, tmp_path, incoming, legacy_row
):
    agent_id = seed_runtime(BASELINE)
    with SessionLocal() as db:
        agent = db.get(Agent, agent_id)
    control = VersionedControl()
    monkeypatch.setattr(canary_service, "control_client", lambda _ws: control)
    edited = AgentSpec(**spec_input(system_prompt="candidate", **(
        {"runtime_config": incoming} if incoming is not None else {}
    )))
    row = canary_service.start_canary(agent, edited, ws_ctx(RESOURCES))
    expected = incoming or BASELINE
    assert row.artifacts["edited_spec"]["runtime_config"] == expected
    if legacy_row and incoming is None:
        # Simulate an execution already queued by an older client/backend.
        canary_service._update(row.id, artifact={"edited_spec": edited.model_dump()})
    stub_mint_build(monkeypatch, tmp_path)
    stub_canary_setup(monkeypatch)

    canary_service.act_setup(row.id, lambda _m: None)
    assert_settings(control.updates[-1], expected)
    assert canary_service._get(row.id).artifacts["edited_spec"]["runtime_config"] == expected
    canary_service.act_complete(row.id, lambda _m: None, allow_non_significant=False)

    with SessionLocal() as db:
        promoted = db.get(Agent, agent_id)
        assert promoted.spec["runtime_config"] == expected and promoted.version == "2"
        snap = db.query(SpecSnapshot).one()
        assert snap.spec["runtime_config"] == expected and snap.aws_version == "2"


def test_setup_retry_keeps_pre_candidate_version_as_restore_proof(monkeypatch, tmp_path):
    agent_id = seed_runtime()
    with SessionLocal() as db:
        agent = db.get(Agent, agent_id)
    control = VersionedControl(V1)
    monkeypatch.setattr(canary_service, "control_client", lambda _ws: control)
    row = canary_service.start_canary(
        agent, AgentSpec(**spec_input(runtime_config=CANDIDATE)), ws_ctx(RESOURCES),
    )
    stub_mint_build(monkeypatch, tmp_path)
    stub_canary_setup(monkeypatch)
    first = canary_service.act_setup(row.id, lambda _m: None)
    assert first["v_current"] == "1" and control.live["agentRuntimeVersion"] == "2"
    monkeypatch.setattr(canary_infra, "current_version",
                        lambda *a: pytest.fail("retry must not adopt rejected version as original"))

    second = canary_service.act_setup(row.id, lambda _m: None)
    assert second["v_current"] == "1" and second["v_candidate"] == "3"
    canary_service.act_rollback(row.id, lambda _m: None)
    assert_settings(control.updates[-1], V1)



def test_historical_canary_completion_does_not_drop_target_explicit_choice(monkeypatch):
    agent_id = seed_runtime(BASELINE)
    with SessionLocal() as db:
        agent = db.get(Agent, agent_id)
    control = VersionedControl()
    monkeypatch.setattr(canary_service, "control_client", lambda _ws: control)
    edited = AgentSpec(**spec_input(system_prompt="candidate"))
    row = canary_service.start_canary(agent, edited, ws_ctx(RESOURCES))
    canary_service._update(row.id, artifact={
        "edited_spec": edited.model_dump(),  # older completed setup did not carry it
        "setup": {"ab_test_id": "ab1", "v_current": "1", "v_candidate": "2"},
    })
    stub_canary_setup(monkeypatch)

    canary_service.act_complete(row.id, lambda _m: None, allow_non_significant=False)

    with SessionLocal() as db:
        assert db.get(Agent, agent_id).spec["runtime_config"] == BASELINE
        assert db.query(SpecSnapshot).one().spec["runtime_config"] == BASELINE


def test_candidate_mint_itself_carries_explicit_target_choice(monkeypatch, tmp_path):
    edited = AgentSpec(**spec_input())
    agent = SimpleNamespace(
        id="a" * 32, resource_id="billing_rt-abcdefghij", name=edited.name, project_id=None,
        workspace_id="default", spec=spec_input(runtime_config=BASELINE),
    )
    control = VersionedControl()
    stub_mint_build(monkeypatch, tmp_path)

    canary_infra.mint_candidate_version(
        agent=agent, edited_spec=edited, control_client=control,
        workspace=ws_ctx(RESOURCES), canary_id="can1",
    )

    assert_settings(control.updates[-1], BASELINE)
    assert edited.runtime_config is None  # the helper must not mutate caller-owned spec
    assert len(control.gets) == 1
