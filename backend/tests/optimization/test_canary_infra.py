"""Target-based canary infra helpers (Phase 2a): naming, versions, candidate
minting and named endpoints (CN: no per-canary gateway) — all with stubbed clients and
injected build/upload seams (no real pip / S3 / AWS)."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import app.optimization.canary_infra as infra
from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.models.ledger import Agent
from app.schemas.agent import AgentSpec
from tests.conftest import ws_ctx

RUNTIME_ARN = (
    "arn:aws:bedrock-agentcore:us-west-2:111122223333:runtime/runtime_x-abcdefghij"
)


class ConflictException(Exception):
    """Name-matched to service._is_conflict / infra._is_conflict."""


class ResourceNotFoundException(Exception):
    pass


def _workspace(monkeypatch, **resources):
    res = {
        "artifacts_bucket": "bkt",
        "execution_role_arn": "arn:role/exec",
        "gateway_role_arn": "arn:role/gw",
    }
    res.update(resources)
    return ws_ctx(res)


def _fake_pip(_cmd, **_kwargs):
    """Stands in for both subprocesses build_zip drives — the uv resolve, which
    must leave a lockfile at `-o`, and the pip install."""
    if "compile" in _cmd:
        Path(_cmd[_cmd.index("-o") + 1]).write_text(
            "probe-pkg==1.0.0 \\\n    --hash=sha256:" + "e" * 64 + "\n",
            encoding="utf-8",
        )
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
    workspace = _workspace(monkeypatch)
    db = SessionLocal()
    try:
        agent = Agent(
            workspace_id=DEFAULT_WORKSPACE_ID, name="mintme",
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
            workspace=workspace,
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
    # UpdateAgentRuntime detaches an omitted list — the candidate echoes the mounts
    assert up_kwargs["filesystemConfigurations"] == [
        {"sessionStorage": {"mountPath": "/mnt/workspace"}}
    ]

    # the ledger Agent row is never mutated — production stays on v_current
    db3 = SessionLocal()
    try:
        row = db3.get(Agent, agent_id)
        assert row.version == "7"
        assert row.status == "active"
        assert row.spec["system_prompt"] == "orig"
    finally:
        db3.close()


# ─── stable / treatment named endpoints ──────────────────────────────────────
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
