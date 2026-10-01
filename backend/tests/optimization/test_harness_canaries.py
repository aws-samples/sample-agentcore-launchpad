"""Harness target canaries: two EXISTING versions of one managed Harness A/B'd behind
passthrough Gateway targets (a Harness ARN is not a valid ``agentcoreRuntime`` target)."""

import binascii
import json
import struct
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import app.optimization.canary_routers as canary_routers
import app.optimization.canary_service as canary_svc
import app.optimization.service as exp_svc
from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.models.ledger import Agent
from app.optimization import canary_harness
from app.optimization.models import RuntimeCanary
from app.services import invoke
from app.services.agentcore import harness as hc

HARNESS_ARN = "arn:aws:bedrock-agentcore:us-west-2:111122223333:harness/subject_h-abcdefghij"
HARNESS_ID = "subject_h-abcdefghij"
BACKING = "harness_subject_h-XyZ0123456"


def _harness_detail(version: str = "3", prompt: str = "current") -> dict:
    return {
        "harnessId": HARNESS_ID, "harnessName": "subject_h", "arn": HARNESS_ARN,
        "status": "READY", "harnessVersion": version,
        "systemPrompt": [{"text": prompt}],
        "environment": {"agentCoreRuntimeEnvironment": {"agentRuntimeId": BACKING}},
    }


def _agent(**fields) -> str:
    db = SessionLocal()
    try:
        agent = Agent(
            workspace_id=DEFAULT_WORKSPACE_ID, name="subject-h", method="harness",
            status="active", arn=HARNESS_ARN, resource_id=HARNESS_ID, version="3",
            spec={"name": "subject-h", "method": "harness", "system_prompt": "accepted"},
            **fields,
        )
        db.add(agent)
        db.commit()
        return agent.id
    finally:
        db.close()


def _canary(agent_id: str, **artifacts) -> str:
    db = SessionLocal()
    try:
        row = RuntimeCanary(
            workspace_id=DEFAULT_WORKSPACE_ID, name="CANARY-subject-h",
            champion_agent_id=agent_id, champion_agent_name="subject-h",
            challenger_agent_id=agent_id, challenger_agent_name="subject-h",
            artifacts={
                "kind": "harness",
                "agent_meta": {
                    "id": agent_id, "name": "subject-h", "arn": HARNESS_ARN,
                    "resource_id": HARNESS_ID, "harness_name": "subject_h",
                    "runtime_name": "harness_subject_h", "backing_runtime_id": BACKING,
                },
                "harness": {"control_version": "1", "treatment_version": "3"},
                "edited_spec": {"name": "subject-h", "method": "harness",
                                "system_prompt": "accepted"},
                "rounds": [],
                **artifacts,
            },
        )
        db.add(row)
        db.commit()
        return row.id
    finally:
        db.close()


def _reload(canary_id: str) -> RuntimeCanary:
    db = SessionLocal()
    try:
        return db.get(RuntimeCanary, canary_id)
    finally:
        db.close()


def _versions_control(*versions: str) -> MagicMock:
    control = MagicMock()
    control.list_harness_versions.return_value = {
        "harnessVersions": [{"harnessVersion": v, "status": "READY"} for v in versions]
    }
    control.get_harness.return_value = {"harness": _harness_detail(versions[-1])}
    return control


# ─── capability + create ────────────────────────────────────────────────────
def test_a_deployed_harness_is_canary_eligible():
    agent = SimpleNamespace(system_key=None, status="active", method="harness",
                            arn=HARNESS_ARN, spec={})
    assert exp_svc.canary_capability(agent)["eligible"] is True
    agent.arn = None
    assert exp_svc.canary_capability(agent)["reason_code"] == "no-harness-arn"


def test_create_harness_canary_pins_two_existing_versions(client, monkeypatch):
    control = _versions_control("1", "2", "3")
    monkeypatch.setattr(canary_routers, "control_client", lambda _ws=None: control)
    monkeypatch.setattr(canary_svc, "control_client", lambda _ws=None: control)
    agent_id = _agent()

    res = client.post("/api/runtime-canaries", json={
        "agent_id": agent_id, "harness_versions": {"control": "1", "treatment": "3"},
    })

    assert res.status_code == 201, res.text
    artifacts = res.json()["artifacts"]
    assert artifacts["kind"] == "harness"
    assert artifacts["harness"] == {"control_version": "1", "treatment_version": "3"}
    assert artifacts["agent_meta"]["backing_runtime_id"] == BACKING
    assert artifacts["agent_meta"]["harness_name"] == "subject_h"
    assert artifacts["edited_spec"]["system_prompt"] == "accepted"


@pytest.mark.parametrize(("versions", "code"), [
    (None, "canary.versions_required"),
    ({"control": "3", "treatment": "3"}, "canary.versions_identical"),
    ({"control": "1", "treatment": "2"}, "canary.treatment_not_latest"),
    ({"control": "9", "treatment": "3"}, "canary.version_not_found"),
])
def test_create_harness_canary_validates_versions(client, monkeypatch, versions, code):
    control = _versions_control("1", "2", "3")
    monkeypatch.setattr(canary_routers, "control_client", lambda _ws=None: control)
    monkeypatch.setattr(canary_svc, "control_client", lambda _ws=None: control)
    body: dict = {"agent_id": _agent()}
    if versions is not None:
        body["harness_versions"] = versions

    res = client.post("/api/runtime-canaries", json=body)

    assert res.status_code in {400, 422}
    assert res.json()["code"] == code
    assert not SessionLocal().query(RuntimeCanary).count()


# ─── setup ──────────────────────────────────────────────────────────────────
def test_setup_fronts_both_versions_with_passthrough_targets(monkeypatch):
    agent_id = _agent()
    canary_id = _canary(agent_id)
    endpoints: dict[str, str] = {}
    monkeypatch.setattr(
        hc, "ensure_harness_endpoint",
        lambda control, harness_id, name, version, **kw: endpoints.update({name: version}),
    )
    monkeypatch.setattr(
        canary_svc.canary_infra, "create_canary_gateway",
        lambda **kw: {"gateway_id": "gw-h", "gateway_arn": "arn:gw-h",
                      "gateway_url": "https://gw-h"},
    )
    tracing: list[str] = []
    monkeypatch.setattr(
        canary_harness, "enable_gateway_tracing",
        lambda logs, *, gateway_id, gateway_arn, log=None: (
            tracing.append(gateway_id) or {"source": "s", "destination": "d"}
        ),
    )
    targets: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        canary_harness, "create_passthrough_target",
        lambda control, *, gateway_id, name, harness_arn, qualifier, region, log=None: (
            targets.append((name, harness_arn, qualifier)) or f"id-{name}"
        ),
    )
    groups: list[str] = []
    monkeypatch.setattr(canary_harness, "ensure_log_group", lambda logs, name: groups.append(name))
    evals: list[dict] = []
    monkeypatch.setattr(
        exp_svc, "create_online_eval_idempotent",
        lambda control, **kw: evals.append(kw) or {
            "onlineEvaluationConfigId": f"id-{kw['name']}",
            "onlineEvaluationConfigArn": f"arn:{kw['name']}",
        },
    )
    data = MagicMock()
    data.create_ab_test.return_value = {"abTestId": "ab-h"}
    monkeypatch.setattr(canary_svc, "data_client", lambda _ws=None: data)
    monkeypatch.setattr(canary_svc, "control_client", lambda _ws=None: MagicMock())

    result = canary_svc.act_setup(canary_id, lambda _m: None)

    ctl = canary_harness.control_endpoint(canary_id)
    trt = canary_harness.treatment_endpoint(canary_id)
    assert endpoints == {ctl: "1", trt: "3"}
    assert tracing == ["gw-h"]
    assert [(t[1], t[2]) for t in targets] == [(HARNESS_ARN, ctl), (HARNESS_ARN, trt)]
    assert groups == [
        f"/aws/bedrock-agentcore/runtimes/{BACKING}-{ctl}",
        f"/aws/bedrock-agentcore/runtimes/{BACKING}-{trt}",
    ]
    assert [e["service_name"] for e in evals] == [
        f"harness_subject_h.{ctl}", f"harness_subject_h.{trt}",
    ]
    ab = data.create_ab_test.call_args.kwargs
    assert [v["weight"] for v in ab["variants"]] == [90, 10]
    assert ab["gatewayFilter"] == {"targetPaths": [f"/{targets[0][0]}/*"]}
    assert result["v_current"] == "1" and result["v_candidate"] == "3"
    assert result["stable_endpoint"] == ctl and result["treatment_endpoint"] == trt
    assert result["runtime_id"] == HARNESS_ID
    stored = _reload(canary_id).artifacts["setup"]
    assert stored["ab_test_id"] == "ab-h"


def test_a_failed_setup_keeps_invokes_on_the_control_endpoint(monkeypatch):
    agent_id = _agent()
    canary_id = _canary(agent_id)
    monkeypatch.setattr(hc, "ensure_harness_endpoint", lambda *a, **kw: None)

    def gateway_boom(**_kw):
        raise RuntimeError("gateway quota")

    monkeypatch.setattr(canary_svc.canary_infra, "create_canary_gateway", gateway_boom)
    monkeypatch.setattr(canary_svc, "control_client", lambda _ws=None: MagicMock())
    monkeypatch.setattr(canary_svc, "data_client", lambda _ws=None: MagicMock())

    with pytest.raises(RuntimeError):
        canary_svc.act_setup(canary_id, lambda _m: None)

    route = canary_svc.active_canary_route(agent_id)
    assert route["kind"] == "harness"
    assert route["stable_endpoint"] == canary_harness.control_endpoint(canary_id)
    assert "gateway_url" not in route


# ─── traffic / rollback / cleanup ───────────────────────────────────────────
def _setup_artifact(canary_id: str) -> dict:
    return {
        "runtime_id": HARNESS_ID, "gateway_id": "gw-h", "gateway_arn": "arn:gw-h",
        "gateway_url": "https://gw-h", "ab_test_id": "ab-h", "test_name": "t",
        "stable_endpoint": canary_harness.control_endpoint(canary_id),
        "treatment_endpoint": canary_harness.treatment_endpoint(canary_id),
        "v_current": "1", "v_candidate": "3", "ramp_stage": 0, "weights": {"C": 90, "T1": 10},
        "champion": {"target_name": "canctl", "target_id": "t-c", "online_eval_id": "oe-c"},
        "challenger": {"target_name": "cantrt", "target_id": "t-t", "online_eval_id": "oe-t"},
    }


# ─── early complete from 50/50 ──────────────────────────────────────────────
def _at_stage(canary_id: str, stage: int, verdict: dict | None, *, harness: bool = True) -> None:
    db = SessionLocal()
    try:
        row = db.get(RuntimeCanary, canary_id)
        setup = {**_setup_artifact(canary_id), "ramp_stage": stage}
        rounds = [{"ramp_stage": stage, "weights": {}, "traffic_attempts": [
            {"sent": 7, "failed": 0, "baseline_n": 0}]}]
        if verdict is not None:
            rounds[0]["verdict"] = verdict
        artifacts = {**row.artifacts, "setup": setup, "rounds": rounds}
        if not harness:
            artifacts.pop("kind")
        row.artifacts = artifacts
        db.commit()
    finally:
        db.close()


def _complete(client, canary_id: str, **extra):
    return client.post(f"/api/runtime-canaries/{canary_id}/action",
                       json={"action": "complete", **extra})


def test_a_harness_canary_completes_from_50_50_on_a_treatment_win(client, monkeypatch):
    canary_id = _canary(_agent())
    _at_stage(canary_id, 1, {"verdict": "treatment-wins", "significant": True})
    runs: list[str] = []
    monkeypatch.setattr(canary_svc, "run_action", lambda cid, action, fn: runs.append(action))

    res = _complete(client, canary_id)

    assert res.status_code == 202, res.text
    assert runs == ["complete"]


def test_an_early_complete_records_the_stage_it_left_from(monkeypatch):
    agent_id = _agent()
    canary_id = _canary(agent_id)
    _at_stage(canary_id, 1, {"verdict": "treatment-wins", "significant": True})
    monkeypatch.setattr(canary_svc, "data_client", lambda _ws=None: MagicMock())
    monkeypatch.setattr(exp_svc, "_stop_ab_test",
                        lambda *a, **kw: {"executionStatus": "STOPPED"})

    result = canary_svc.act_complete(canary_id, lambda _m: None, allow_non_significant=False)

    assert result["completed_at_stage"] == 1
    assert result["promoted_version"] == "3"
    assert _reload(canary_id).status == "completed"


def test_a_non_significant_early_win_still_needs_the_override(client, monkeypatch):
    canary_id = _canary(_agent())
    _at_stage(canary_id, 1, {"verdict": "treatment-wins", "significant": False})
    runs: list[str] = []
    monkeypatch.setattr(canary_svc, "run_action", lambda cid, action, fn: runs.append(action))

    refused = _complete(client, canary_id)
    overridden = _complete(client, canary_id, allow_non_significant=True)

    assert refused.status_code == 409
    assert refused.json()["code"] == "canary.override_required"
    assert overridden.status_code == 202
    assert runs == ["complete"]


@pytest.mark.parametrize(("stage", "verdict", "harness"), [
    (0, {"verdict": "treatment-wins", "significant": True}, True),   # 90/10 is too early
    (1, {"verdict": "tie", "significant": False}, True),             # a tie goes on to 1/99
    (1, {"verdict": "treatment-wins", "significant": True}, False),  # Runtime keeps the full ramp
])
def test_early_complete_is_only_a_harness_50_50_treatment_win(client, monkeypatch, stage,
                                                              verdict, harness):
    canary_id = _canary(_agent())
    _at_stage(canary_id, stage, verdict, harness=harness)
    runs: list[str] = []
    monkeypatch.setattr(canary_svc, "run_action", lambda cid, action, fn: runs.append(action))

    res = _complete(client, canary_id, allow_non_significant=True)

    assert res.status_code == 409
    assert res.json()["code"] == "canary.stage_not_ready"
    assert runs == []


def test_traffic_posts_the_invoke_harness_body_under_the_ab_filter(monkeypatch):
    agent_id = _agent()
    canary_id = _canary(agent_id)
    db = SessionLocal()
    row = db.get(RuntimeCanary, canary_id)
    row.artifacts = {**row.artifacts, "setup": _setup_artifact(canary_id)}
    db.commit()
    db.close()
    data = MagicMock()
    data.get_ab_test.return_value = {}
    monkeypatch.setattr(canary_svc, "data_client", lambda _ws=None: data)
    seen: dict = {}

    def send(url, target, prompts, workspace, **kw):
        seen.update(url=url, target=target, **kw)
        return {"session_ids": ["s"], "sent": 1, "failed": 0}

    monkeypatch.setattr(exp_svc, "send_gateway_traffic", send)

    canary_svc.act_traffic(canary_id, ["hello"], {"dataset_id": "d"}, lambda _m: None)

    assert seen["target"] == "canctl"
    # the trailing slash is what the A/B gatewayFilter /<target>/* intercepts
    assert seen["path_suffix"] == "/"
    body = seen["body_for"]("hello", "sid")
    assert body["messages"] == [{"role": "user", "content": [{"text": "hello"}]}]
    assert body["actorId"].startswith(agent_id)


def test_rollback_republishes_the_control_version(monkeypatch):
    agent_id = _agent()
    canary_id = _canary(agent_id)
    db = SessionLocal()
    row = db.get(RuntimeCanary, canary_id)
    row.artifacts = {**row.artifacts, "setup": _setup_artifact(canary_id)}
    db.commit()
    db.close()
    control = MagicMock()
    control.get_harness.side_effect = lambda **kw: {"harness": {
        **_harness_detail("1", "original prompt"),
        "memory": None, "tools": [{"type": "inline_function", "name": "x"}],
        "allowedTools": ["@x"], "maxIterations": 10,
    }} if kw.get("harnessVersion") == "1" else {"harness": _harness_detail("4")}
    control.update_harness.return_value = {"harness": _harness_detail("4")}
    monkeypatch.setattr(canary_svc, "control_client", lambda _ws=None: control)
    monkeypatch.setattr(canary_svc, "data_client", lambda _ws=None: MagicMock())
    monkeypatch.setattr(exp_svc, "_stop_ab_test",
                        lambda data, ab, progress, label="": {"executionStatus": "STOPPED"})

    result = canary_svc.act_rollback(canary_id, lambda _m: None)

    params = control.update_harness.call_args.kwargs
    assert params["harnessId"] == HARNESS_ID
    assert params["systemPrompt"] == [{"text": "original prompt"}]
    assert params["tools"] == [{"type": "inline_function", "name": "x"}]
    assert params["memory"] == {"optionalValue": {"disabled": {}}}
    assert "environment" not in params and "executionRoleArn" not in params
    assert result["restored_version"] == "4"
    assert result["restored_from_version"] == "1"
    db = SessionLocal()
    try:
        agent = db.get(Agent, agent_id)
        assert agent.version == "4"
        assert agent.spec["system_prompt"] == "original prompt"
    finally:
        db.close()
    assert _reload(canary_id).status == "rolled_back"


def test_cleanup_deletes_both_harness_endpoints_and_trace_delivery(monkeypatch):
    agent_id = _agent()
    canary_id = _canary(agent_id)
    db = SessionLocal()
    row = db.get(RuntimeCanary, canary_id)
    row.artifacts = {**row.artifacts, "setup": _setup_artifact(canary_id)}
    db.commit()
    db.close()
    control = MagicMock()
    control.list_gateway_targets.return_value = {"items": []}
    control.list_online_evaluation_configs.return_value = {"onlineEvaluationConfigs": []}
    monkeypatch.setattr(canary_svc, "control_client", lambda _ws=None: control)
    monkeypatch.setattr(canary_svc, "data_client", lambda _ws=None: MagicMock())
    monkeypatch.setattr(exp_svc, "list_ab_tests", lambda data: [])
    monkeypatch.setattr(exp_svc, "_stop_ab_test", lambda *a, **kw: {})
    monkeypatch.setattr(canary_svc.ac, "cleanup_resources", lambda *a, **kw: [])
    deleted: list[str] = []
    monkeypatch.setattr(hc, "delete_harness_endpoint",
                        lambda control, harness_id, name: deleted.append(name) or True)
    monkeypatch.setattr(canary_harness, "disable_gateway_tracing",
                        lambda logs, gateway_id: [{"category": "trace", "status": "deleted"}])
    monkeypatch.setattr(canary_svc.canary_infra, "delete_canary_gateway",
                        lambda control, gateway_id, log=None: None)

    results = canary_svc.act_cleanup(canary_id, lambda _m: None)

    assert deleted == [canary_harness.control_endpoint(canary_id),
                       canary_harness.treatment_endpoint(canary_id)]
    categories = [r["category"] for r in results]
    assert "trace" in categories and "gateway:gw-h" in categories
    assert not any(c.startswith("s3:") for c in categories)
    assert _reload(canary_id).status == "cleaned"


# ─── invoke routing ─────────────────────────────────────────────────────────
def _event(event_type: str, payload: dict) -> bytes:
    """One binary event-stream message (the wire format a passthrough target relays)."""
    body = json.dumps(payload).encode()
    headers = b""
    for name, value in ((":event-type", event_type), (":message-type", "event"),
                        (":content-type", "application/json")):
        headers += bytes([len(name)]) + name.encode() + b"\x07"
        headers += struct.pack(">H", len(value)) + value.encode()
    total = 12 + len(headers) + len(body) + 4
    prelude = struct.pack(">II", total, len(headers))
    prelude += struct.pack(">I", binascii.crc32(prelude) & 0xFFFFFFFF)
    message = prelude + headers + body
    return message + struct.pack(">I", binascii.crc32(message) & 0xFFFFFFFF)


STREAM = (
    _event("messageStart", {"role": "assistant"})
    + _event("contentBlockDelta", {"contentBlockIndex": 0, "delta": {"text": "Hello"}})
    + _event("contentBlockDelta", {"contentBlockIndex": 0, "delta": {"text": " there"}})
    + _event("messageStop", {"stopReason": "end_turn"})
)


def test_event_stream_bytes_decode_to_the_assistant_text():
    assert hc.event_stream_text(STREAM) == "Hello there"


def _harness_route(**extra) -> dict:
    return {"kind": "harness", "arn": HARNESS_ARN, "runtime_id": HARNESS_ID,
            "stable_endpoint": "ctlabc", "v_current": "1", **extra}


def test_live_harness_canary_invokes_through_the_gateway(monkeypatch):
    posts: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        invoke.gateway, "sigv4_post",
        lambda url, body, ws, **kw: posts.append((url, body)) or SimpleNamespace(
            status_code=200, content=STREAM),
    )
    direct = MagicMock()
    monkeypatch.setattr(invoke.hc, "invoke_harness_text", direct)

    out = invoke._invoke_harness_via_canary(
        _harness_route(gateway_url="https://gw/", control_target="canctl"),
        "hi", None, "actor-1", MagicMock(), {},
    )

    assert out["text"] == "Hello there"
    assert posts[0][0] == "https://gw/canctl/"
    assert posts[0][1]["messages"][0]["content"] == [{"text": "hi"}]
    assert posts[0][1]["actorId"] == "actor-1"
    direct.assert_not_called()


def test_harness_canary_falls_back_to_the_control_endpoint(monkeypatch):
    monkeypatch.setattr(invoke.gateway, "sigv4_post",
                        lambda *a, **kw: SimpleNamespace(status_code=502, content=b""))
    calls: list[dict] = []
    monkeypatch.setattr(invoke.hc, "invoke_harness_text",
                        lambda client, arn, prompt, **kw: calls.append(kw) or {"text": "ok"})
    monkeypatch.setattr(invoke, "data_client", lambda _ws: MagicMock())

    live = invoke._invoke_harness_via_canary(
        _harness_route(gateway_url="https://gw", control_target="canctl"),
        "hi", None, "actor-1", MagicMock(), {},
    )
    provisioning = invoke._invoke_harness_via_canary(
        _harness_route(), "hi", None, "actor-1", MagicMock(), {},
    )

    assert live == provisioning == {"text": "ok"}
    assert [c["qualifier"] for c in calls] == ["ctlabc", "ctlabc"]


def test_rollback_params_detach_what_the_rejected_version_added():
    params = canary_harness.rollback_params(
        {"systemPrompt": [{"text": "p"}], "memory": {"agentCoreMemoryConfiguration": {"arn": "m"}},
         "environment": {"x": 1}, "maxTokens": None},
        HARNESS_ID,
    )
    assert params["memory"] == {"optionalValue": {"agentCoreMemoryConfiguration": {"arn": "m"}}}
    assert params["tools"] == [] and params["skills"] == []
    assert "environment" not in params and "maxTokens" not in params
