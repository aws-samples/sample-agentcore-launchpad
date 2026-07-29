"""Target-based canary infra helpers (Phase 2a): naming, versions, candidate
minting, dedicated gateway, and named endpoints — all with stubbed clients and
injected build/upload seams (no real pip / S3 / AWS)."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import app.optimization.canary_infra as infra
import app.optimization.service as exp_svc
from app.core.db import SessionLocal
from app.models.ledger import Agent
from app.schemas.agent import AgentSpec

RUNTIME_ARN = (
    "arn:aws:bedrock-agentcore:us-west-2:111122223333:runtime/runtime_x-abcdefghij"
)


class ConflictException(Exception):
    """Name-matched to service._is_conflict / infra._is_conflict."""


class ResourceNotFoundException(Exception):
    pass


def _patch_settings(monkeypatch, **resources):
    res = {
        "artifacts_bucket": "bkt",
        "execution_role_arn": "arn:role/exec",
        "gateway_role_arn": "arn:role/gw",
    }
    res.update(resources)
    monkeypatch.setattr(
        infra, "get_settings",
        lambda: SimpleNamespace(resources=res, region="us-west-2"),
    )


def _fake_pip(_cmd, **_kwargs):
    return SimpleNamespace(returncode=0, stderr="")


# ─── naming helpers ──────────────────────────────────────────────────────────
def test_endpoint_naming_mirrors_default_forms():
    assert (
        infra.endpoint_log_group("res-1", "stable")
        == "/aws/bedrock-agentcore/runtimes/res-1-stable"
    )
    assert infra.endpoint_service_name("runtime_x", "treatment") == "runtime_x.treatment"


def test_current_version_stringifies():
    control = MagicMock()
    control.get_agent_runtime.return_value = {"agentRuntimeVersion": 5}
    assert infra.current_version(control, "rt-1") == "5"
    control.get_agent_runtime.assert_called_once_with(agentRuntimeId="rt-1")


# ─── candidate minting ───────────────────────────────────────────────────────
def test_mint_candidate_reads_version_from_update_and_leaves_ledger_untouched(
    monkeypatch, tmp_path
):
    _patch_settings(monkeypatch)
    db = SessionLocal()
    try:
        agent = Agent(
            name="mintme",
            method="zip_runtime",
            status="active",
            arn=RUNTIME_ARN,
            resource_id="mintme-abcdefghij",
            version="7",
            spec={"system_prompt": "orig", "method": "zip_runtime", "name": "mintme"},
        )
        db.add(agent)
        db.commit()
        agent_id = agent.id
    finally:
        db.close()

    control = MagicMock()
    control.get_agent_runtime.return_value = {
        "agentRuntimeVersion": "7",
        "status": "READY",
        "agentRuntimeArn": RUNTIME_ARN,
        "agentRuntimeName": "runtime_x",
    }
    control.update_agent_runtime.return_value = {
        "agentRuntimeVersion": "8",
        "status": "READY",
        "agentRuntimeArn": RUNTIME_ARN,
    }
    spec = AgentSpec(name="mintme", method="zip_runtime", system_prompt="edited prompt")
    uploads: list[tuple[str, str]] = []

    db2 = SessionLocal()
    try:
        v_current, v_candidate, s3_key = infra.mint_candidate_version(
            agent=db2.get(Agent, agent_id),
            edited_spec=spec,
            control_client=control,
            canary_id="can123",
            pip_runner=_fake_pip,
            uploader=lambda local, bucket, key: uploads.append((bucket, key)),
            build_root=tmp_path,
        )
    finally:
        db2.close()

    # v_candidate comes from the UpdateAgentRuntime response, not the GET
    assert (v_current, v_candidate) == ("7", "8")
    # the zip landed under a deterministic per-canary/role key the caller can
    # record — a retried mint overwrites it instead of orphaning a new object
    assert uploads == [("bkt", "agents/mintme/canary/can123-candidate.zip")]
    assert s3_key == "agents/mintme/canary/can123-candidate.zip"
    up_kwargs = control.update_agent_runtime.call_args.kwargs
    s3 = up_kwargs["agentRuntimeArtifact"]["codeConfiguration"]["code"]["s3"]
    assert s3 == {"bucket": "bkt", "prefix": uploads[0][1]}
    assert up_kwargs["roleArn"] == "arn:role/exec"

    # the ledger Agent row is never mutated — production stays on v_current
    db3 = SessionLocal()
    try:
        row = db3.get(Agent, agent_id)
        assert row.version == "7"
        assert row.status == "active"
        assert row.spec["system_prompt"] == "orig"
    finally:
        db3.close()


def test_mint_candidate_container_is_follow_up(monkeypatch, tmp_path):
    _patch_settings(monkeypatch)
    spec = AgentSpec(name="containeragent", method="container", system_prompt="p")
    with pytest.raises(NotImplementedError, match="container"):
        infra.mint_candidate_version(
            agent=SimpleNamespace(name="c", resource_id="c-x"),
            edited_spec=spec,
            control_client=MagicMock(),
            canary_id="can123",
            build_root=tmp_path,
        )


# ─── dedicated per-canary gateway ────────────────────────────────────────────
def test_create_canary_gateway_is_unique_and_iam(monkeypatch):
    _patch_settings(monkeypatch)
    control = MagicMock()
    control.create_gateway.return_value = {"gatewayId": "gw-can"}
    control.get_gateway.return_value = {
        "status": "READY",
        "gatewayArn": "arn:gw",
        "gatewayUrl": "https://gw",
    }
    out = infra.create_canary_gateway(control_client=control, canary_id="abc123def456")
    assert out == {
        "gateway_id": "gw-can",
        "gateway_arn": "arn:gw",
        "gateway_url": "https://gw",
    }
    kwargs = control.create_gateway.call_args.kwargs
    assert kwargs["name"] == "lp-canary-abc123def456"
    assert kwargs["authorizerType"] == "AWS_IAM"
    assert kwargs["roleArn"] == "arn:role/gw"


def test_create_canary_gateway_adopts_on_conflict(monkeypatch):
    _patch_settings(monkeypatch)
    control = MagicMock()
    control.create_gateway.side_effect = ConflictException("already exists")
    control.list_gateways.return_value = {
        "items": [
            {"name": "other", "gatewayId": "gw-other"},
            {"name": "lp-canary-dupe123456", "gatewayId": "gw-existing"},
        ]
    }
    control.get_gateway.return_value = {
        "status": "READY",
        "gatewayArn": "arn:gw",
        "gatewayUrl": "https://gw",
    }
    out = infra.create_canary_gateway(control_client=control, canary_id="dupe123456")
    # a prior attempt's gateway is adopted by name rather than re-created
    assert out == {
        "gateway_id": "gw-existing",
        "gateway_arn": "arn:gw",
        "gateway_url": "https://gw",
    }
    control.get_gateway.assert_called_with(gatewayIdentifier="gw-existing")


def test_delete_canary_gateway():
    control = MagicMock()
    infra.delete_canary_gateway(control, "gw-1")
    control.delete_gateway.assert_called_once_with(gatewayIdentifier="gw-1")


def test_delete_canary_gateway_retries_until_ab_test_drains(monkeypatch):
    """DeleteGateway is rejected for minutes while the async-deleting A/B test
    drains (real-AWS e2e finding) — retry until it takes, don't leak."""
    monkeypatch.setattr(infra, "_sleep", lambda *_a, **_k: None)
    control = MagicMock()
    control.delete_gateway.side_effect = [
        RuntimeError("gateway has an associated A/B test"),
        RuntimeError("gateway has an associated A/B test"),
        None,
    ]
    infra.delete_canary_gateway(control, "gw-1", interval_s=0)
    assert control.delete_gateway.call_count == 3


def test_delete_canary_gateway_treats_not_found_as_deleted(monkeypatch):
    monkeypatch.setattr(infra, "_sleep", lambda *_a, **_k: None)
    control = MagicMock()

    class ResourceNotFoundException(Exception):
        pass

    control.delete_gateway.side_effect = ResourceNotFoundException("gone")
    infra.delete_canary_gateway(control, "gw-1")
    assert control.delete_gateway.call_count == 1  # already gone == success


def test_delete_canary_gateway_raises_after_budget(monkeypatch):
    """If the delete never takes within the budget, raise so the caller records
    the skip rather than hanging forever."""
    monkeypatch.setattr(infra, "_sleep", lambda *_a, **_k: None)
    control = MagicMock()
    control.delete_gateway.side_effect = RuntimeError("still busy")
    with pytest.raises(RuntimeError):
        infra.delete_canary_gateway(control, "gw-1", timeout_s=0.0, interval_s=0)


# ─── stable / treatment endpoints ────────────────────────────────────────────
def test_ensure_canary_endpoints_pins_and_waits_both():
    control = MagicMock()
    control.get_agent_runtime_endpoint.return_value = {"status": "READY"}
    out = infra.ensure_canary_endpoints(
        control_client=control,
        runtime_id="rt-1",
        v_current="7",
        v_candidate="8",
        stable_name="stable",
        treatment_name="treatment",
    )
    assert out == {"stable": "stable", "treatment": "treatment"}
    created = {
        c.kwargs["name"]: c.kwargs["agentRuntimeVersion"]
        for c in control.create_agent_runtime_endpoint.call_args_list
    }
    assert created == {"stable": "7", "treatment": "8"}
    waited = {
        c.kwargs["endpointName"]
        for c in control.get_agent_runtime_endpoint.call_args_list
    }
    assert waited == {"stable", "treatment"}


def test_ensure_canary_endpoints_adopts_on_conflict():
    control = MagicMock()
    control.create_agent_runtime_endpoint.side_effect = ConflictException("exists")
    control.get_agent_runtime_endpoint.return_value = {"status": "READY"}
    infra.ensure_canary_endpoints(
        control_client=control,
        runtime_id="rt-1",
        v_current="7",
        v_candidate="8",
        stable_name="stable",
        treatment_name="treatment",
    )
    updated = {
        c.kwargs["endpointName"]: c.kwargs["agentRuntimeVersion"]
        for c in control.update_agent_runtime_endpoint.call_args_list
    }
    assert updated == {"stable": "7", "treatment": "8"}


def test_promote_stable_endpoint_repoints_to_candidate():
    control = MagicMock()
    control.get_agent_runtime_endpoint.return_value = {"status": "READY", "liveVersion": "8"}
    infra.promote_stable_endpoint(
        control_client=control, runtime_id="rt-1", stable_name="stable", version="8"
    )
    kwargs = control.update_agent_runtime_endpoint.call_args.kwargs
    assert kwargs["endpointName"] == "stable"
    assert kwargs["agentRuntimeVersion"] == "8"


def test_delete_endpoint_quiet_swallows_not_found_but_reraises_real_errors():
    control = MagicMock()
    control.delete_agent_runtime_endpoint.side_effect = ResourceNotFoundException("gone")
    infra.delete_endpoint_quiet(control, runtime_id="rt-1", endpoint_name="treatment")

    control.delete_agent_runtime_endpoint.side_effect = RuntimeError("boom")
    with pytest.raises(RuntimeError):
        infra.delete_endpoint_quiet(control, runtime_id="rt-1", endpoint_name="treatment")


# ─── qualifier passthrough on the shared target helper ───────────────────────
def test_create_runtime_target_passes_qualifier_through():
    control = MagicMock()
    control.create_gateway_target.return_value = {"targetId": "t1"}
    control.get_gateway_target.return_value = {"status": "READY"}

    tid = exp_svc.create_runtime_target_idempotent(
        control, "gw-1", "tname", "arn:agent", qualifier="treatment"
    )
    assert tid == "t1"
    cfg = control.create_gateway_target.call_args.kwargs["targetConfiguration"]
    assert cfg["http"]["agentcoreRuntime"]["qualifier"] == "treatment"

    exp_svc.create_runtime_target_idempotent(control, "gw-1", "tname2", "arn:agent")
    cfg2 = control.create_gateway_target.call_args.kwargs["targetConfiguration"]
    assert cfg2["http"]["agentcoreRuntime"]["qualifier"] == "DEFAULT"
