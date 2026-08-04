"""Runtime Canary records, guards, actions, and resource ownership (Model 1:
one agent, two immutable versions, dedicated per-canary Gateway)."""

from unittest.mock import MagicMock

import pytest

import app.optimization.canary_service as canary_svc
import app.optimization.service as exp_svc
from app.core.db import SessionLocal
from app.core.errors import AppError
from app.models.ledger import Agent
from app.optimization.models import Experiment, RuntimeCanary
from app.schemas.agent import AgentSpec

RUNTIME_ARN = (
    "arn:aws:bedrock-agentcore:us-west-2:111122223333:"
    "runtime/runtime_agent-abcdefghij"
)


def _agent(
    name: str,
    *,
    method: str = "zip_runtime",
    status: str = "active",
    spec: dict | None = None,
    arn: str | None = RUNTIME_ARN,
) -> Agent:
    return Agent(
        name=name,
        method=method,
        status=status,
        arn=arn,
        resource_id=f"{name}-abcdefghij",
        spec=spec or {"protocol": "http", "system_prompt": "orig"},
    )


def _persist_agents(*agents: Agent) -> list[str]:
    db = SessionLocal()
    try:
        db.add_all(agents)
        db.commit()
        return [agent.id for agent in agents]
    finally:
        db.close()


def _mk_canary(
    *,
    status: str = "running",
    stage: str = "setup",
    agent_id: str = "a1",
    artifacts: dict | None = None,
) -> RuntimeCanary:
    db = SessionLocal()
    try:
        row = RuntimeCanary(
            name="CANARY-subject",
            champion_agent_id=agent_id,
            champion_agent_name="subject",
            challenger_agent_id=agent_id,
            challenger_agent_name="subject",
            status=status,
            stage=stage,
            artifacts=artifacts or {
                "agent_meta": {"id": agent_id},
                "edited_spec": {},
                "rounds": [],
            },
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        return row
    finally:
        db.close()


def _reload(canary_id: str) -> RuntimeCanary:
    db = SessionLocal()
    try:
        return db.get(RuntimeCanary, canary_id)
    finally:
        db.close()


def _reload_agent(agent_id: str) -> Agent:
    db = SessionLocal()
    try:
        return db.get(Agent, agent_id)
    finally:
        db.close()


def _setup_artifact(ramp_stage: int = 0) -> dict:
    control_weight, treatment_weight = canary_svc.RAMP_WEIGHTS[ramp_stage]
    return {
        "gateway_id": "gw-1",
        "gateway_arn": "arn:gateway",
        "gateway_url": "https://gateway.example",
        "test_name": "can_test_target",
        "ab_test_id": "ab-1",
        # champion/challenger here mean the control/treatment TARGETS.
        "champion": {
            "target_name": "cancontrol",
            "target_id": "target-c",
            "online_eval_id": "oe-c",
            "online_eval_arn": "arn:oe-c",
        },
        "challenger": {
            "target_name": "cantreatment",
            "target_id": "target-t",
            "online_eval_id": "oe-t",
            "online_eval_arn": "arn:oe-t",
        },
        "ramp_stage": ramp_stage,
        "weights": {"C": control_weight, "T1": treatment_weight},
        "v_current": "1",
        "v_candidate": "2",
        "stable_endpoint": "stablecan",
        "treatment_endpoint": "treatcan",
        "runtime_id": "subject-abcdefghij",
    }


def _round(
    ramp_stage: int,
    *,
    verdict: dict | None = None,
    baseline_n: int = 0,
) -> dict:
    result = {
        "ramp_stage": ramp_stage,
        "weights": {},
        "traffic_attempts": [{"baseline_n": baseline_n, "sent": 12, "failed": 0}],
    }
    if verdict is not None:
        result["verdict"] = verdict
    return result


# ─── create (single agent + candidate edit) ──────────────────────────────────
def test_create_runtime_canary_persists_single_agent_record(client, monkeypatch):
    (agent_id,) = _persist_agents(_agent("subject"))
    control = MagicMock()
    control.get_agent_runtime.side_effect = lambda agentRuntimeId: {
        "agentRuntimeName": f"runtime-{agentRuntimeId}"
    }
    monkeypatch.setattr(canary_svc, "control_client", lambda: control)

    res = client.post(
        "/api/runtime-canaries",
        json={"agent_id": agent_id, "candidate": {"system_prompt": "new prompt"}},
    )

    assert res.status_code == 201
    body = res.json()
    assert body["champion_agent_id"] == agent_id
    assert body["challenger_agent_id"] == agent_id
    assert body["stage"] == "setup"
    assert body["artifacts"]["rounds"] == []
    assert body["artifacts"]["agent_meta"]["id"] == agent_id
    assert body["artifacts"]["edited_spec"]["system_prompt"] == "new prompt"
    assert client.get("/api/runtime-canaries").json()["canaries"][0]["id"] == body["id"]
    db = SessionLocal()
    try:
        assert db.query(Experiment).count() == 0
        assert db.query(RuntimeCanary).count() == 1
    finally:
        db.close()


def test_create_requires_a_candidate_edit(client):
    (agent_id,) = _persist_agents(_agent("subject"))
    res = client.post(
        "/api/runtime-canaries",
        json={"agent_id": agent_id, "candidate": {}},
    )
    assert res.status_code == 400
    assert res.json()["code"] == "canary.candidate_empty"


@pytest.mark.parametrize(
    ("agent", "code"),
    [
        (_agent("inactive", status="failed"), "canary.agent_not_active"),
        (
            _agent("harness", method="harness", arn="arn:harness/x"),
            "canary.agent_unsupported",
        ),
        (
            _agent("a2aagent", spec={"protocol": "a2a"}),
            "canary.agent_unsupported",
        ),
        (_agent("cont", method="container"), "canary.agent_unsupported"),
    ],
)
def test_create_rejects_incompatible_agents(client, agent, code):
    (agent_id,) = _persist_agents(agent)
    res = client.post(
        "/api/runtime-canaries",
        json={"agent_id": agent_id, "candidate": {"system_prompt": "p"}},
    )
    assert res.status_code == 400
    assert res.json()["code"] == code


def test_studio_agent_is_canary_eligible(client, monkeypatch):
    (agent_id,) = _persist_agents(
        _agent(
            "studioagent",
            method="studio",
            spec={"protocol": "http", "system_prompt": "orig", "code": "x"},
        )
    )
    control = MagicMock()
    control.get_agent_runtime.side_effect = lambda agentRuntimeId: {
        "agentRuntimeName": f"runtime-{agentRuntimeId}"
    }
    monkeypatch.setattr(canary_svc, "control_client", lambda: control)

    res = client.post(
        "/api/runtime-canaries",
        json={"agent_id": agent_id, "candidate": {"code": "new studio code"}},
    )
    assert res.status_code == 201
    assert res.json()["artifacts"]["edited_spec"]["code"] == "new studio code"


def test_source_experiment_must_match_promoted_agent(client, monkeypatch):
    (agent_id,) = _persist_agents(_agent("subject"))
    db = SessionLocal()
    try:
        source = Experiment(
            name="EXP-source",
            agent_id="someone-else",
            agent_name="other",
            status="promoted",
            stage="promote",
            artifacts={"promote": {"deployment_id": "d1", "ab_test_status": "STOPPED"}},
        )
        db.add(source)
        db.commit()
        source_id = source.id
    finally:
        db.close()
    monkeypatch.setattr(canary_svc, "control_client", MagicMock)

    res = client.post(
        "/api/runtime-canaries",
        json={
            "agent_id": agent_id,
            "candidate": {"system_prompt": "p"},
            "source_experiment_id": source_id,
        },
    )
    assert res.status_code == 400
    assert res.json()["code"] == "canary.source_champion_mismatch"


def test_promoted_experiment_can_handoff_to_separate_canary(client, monkeypatch):
    (agent_id,) = _persist_agents(_agent("subject"))
    db = SessionLocal()
    try:
        source = Experiment(
            name="EXP-source",
            agent_id=agent_id,
            agent_name="subject",
            status="promoted",
            stage="promote",
            artifacts={"promote": {"deployment_id": "d1", "ab_test_status": "STOPPED"}},
        )
        db.add(source)
        db.commit()
        source_id = source.id
    finally:
        db.close()
    control = MagicMock()
    control.get_agent_runtime.side_effect = lambda agentRuntimeId: {
        "agentRuntimeName": f"runtime-{agentRuntimeId}"
    }
    monkeypatch.setattr(canary_svc, "control_client", lambda: control)

    res = client.post(
        "/api/runtime-canaries",
        json={
            "agent_id": agent_id,
            "candidate": {"system_prompt": "p"},
            "source_experiment_id": source_id,
        },
    )

    assert res.status_code == 201
    body = res.json()
    assert body["source_experiment_id"] == source_id
    assert body["champion_agent_id"] == agent_id
    assert body["artifacts"]["agent_meta"]["id"] == agent_id


# ─── setup dispatch + orchestration ──────────────────────────────────────────
def test_setup_action_dispatches(client, monkeypatch):
    row = _mk_canary()
    dispatched: list[str] = []
    monkeypatch.setattr(
        canary_svc,
        "run_action",
        lambda canary_id, action, fn: dispatched.append(action),
    )
    res = client.post(
        f"/api/runtime-canaries/{row.id}/action",
        json={"action": "setup"},
    )
    assert res.status_code == 202
    assert dispatched == ["setup"]


def test_setup_mints_candidate_targets_endpoints_and_own_ab_test(monkeypatch):
    (agent_id,) = _persist_agents(_agent("subject"))
    row = _mk_canary(
        agent_id=agent_id,
        artifacts={
            "agent_meta": {
                "id": agent_id,
                "name": "subject",
                "arn": "arn:agent",
                "resource_id": "subject-res",
                "runtime_name": "SubjectRuntime",
            },
            "edited_spec": AgentSpec(
                name="subject", method="zip_runtime", system_prompt="edited"
            ).model_dump(),
            "rounds": [],
        },
    )

    monkeypatch.setattr(
        canary_svc.canary_infra,
        "mint_candidate_version",
        lambda **kw: ("1", "2", f"agents/subject/canary/{kw['canary_id']}-{kw['role']}.zip"),
    )
    monkeypatch.setattr(
        canary_svc.canary_infra, "current_version", lambda control, runtime_id: "1"
    )
    monkeypatch.setattr(
        canary_svc.canary_infra,
        "create_canary_gateway",
        lambda **kw: {
            "gateway_id": "gw-can",
            "gateway_arn": "arn:gw",
            "gateway_url": "https://gw",
        },
    )
    endpoints_seen: dict = {}
    monkeypatch.setattr(
        canary_svc.canary_infra,
        "ensure_endpoint_ready",
        lambda control, *, runtime_id, endpoint_name, version, log=None: (
            endpoints_seen.update({endpoint_name: version}) or {"status": "READY"}
        ),
    )
    targets_seen: list[tuple[str, str]] = []
    monkeypatch.setattr(
        exp_svc,
        "create_runtime_target_idempotent",
        lambda control, gateway_id, name, arn, qualifier="DEFAULT": (
            targets_seen.append((name, qualifier)) or f"id-{name}"
        ),
    )
    monkeypatch.setattr(
        exp_svc,
        "create_online_eval_idempotent",
        lambda control, **kwargs: {
            "onlineEvaluationConfigId": f"id-{kwargs['name']}",
            "onlineEvaluationConfigArn": f"arn:{kwargs['name']}",
        },
    )
    data = MagicMock()
    data.create_ab_test.return_value = {"abTestId": "ab-canary"}
    monkeypatch.setattr(canary_svc, "data_client", lambda: data)
    monkeypatch.setattr(canary_svc, "control_client", MagicMock)

    result = canary_svc.act_setup(row.id, lambda message: None)

    assert result["ab_test_id"] == "ab-canary"
    assert result["weights"] == {"C": 90, "T1": 10}
    assert result["v_current"] == "1"
    assert result["v_candidate"] == "2"
    assert result["runtime_id"] == "subject-res"
    assert result["stable_endpoint"].startswith("stable")
    assert result["treatment_endpoint"].startswith("treat")
    assert result["champion"]["target_id"].startswith("id-can")
    assert result["challenger"]["target_id"].startswith("id-can")
    # the two targets are pinned to the stable / treatment named endpoints
    assert targets_seen[0][1] == result["stable_endpoint"]
    assert targets_seen[1][1] == result["treatment_endpoint"]
    # endpoints pinned stable→v_current, treatment→v_candidate
    assert endpoints_seen[result["stable_endpoint"]] == "1"
    assert endpoints_seen[result["treatment_endpoint"]] == "2"
    # the A/B test is created on the dedicated per-canary gateway at 90/10
    ab_kwargs = data.create_ab_test.call_args.kwargs
    assert ab_kwargs["gatewayArn"] == "arn:gw"
    assert [v["weight"] for v in ab_kwargs["variants"]] == [90, 10]
    stored = _reload(row.id)
    assert stored.artifacts["setup"]["ramp_stage"] == 0
    assert stored.artifacts["setup"]["gateway_id"] == "gw-can"


def test_setup_retry_adopts_its_own_ab_test_after_conflict(monkeypatch):
    (agent_id,) = _persist_agents(_agent("subject"))
    row = _mk_canary(
        agent_id=agent_id,
        artifacts={
            "agent_meta": {
                "id": agent_id,
                "name": "subject",
                "arn": "arn:agent",
                "resource_id": "subject-res",
                "runtime_name": "SubjectRuntime",
            },
            "edited_spec": AgentSpec(
                name="subject", method="zip_runtime", system_prompt="edited"
            ).model_dump(),
            "rounds": [],
        },
    )
    monkeypatch.setattr(
        canary_svc.canary_infra,
        "mint_candidate_version",
        lambda **kw: ("1", "2", f"agents/subject/canary/{kw['canary_id']}-{kw['role']}.zip"),
    )
    monkeypatch.setattr(
        canary_svc.canary_infra, "current_version", lambda control, runtime_id: "1"
    )
    monkeypatch.setattr(
        canary_svc.canary_infra,
        "create_canary_gateway",
        lambda **kw: {
            "gateway_id": "gw-can",
            "gateway_arn": "arn:gw",
            "gateway_url": "https://gw",
        },
    )
    monkeypatch.setattr(
        canary_svc.canary_infra,
        "ensure_endpoint_ready",
        lambda control, *, runtime_id, endpoint_name, version, log=None: {
            "status": "READY"
        },
    )
    monkeypatch.setattr(
        exp_svc,
        "create_runtime_target_idempotent",
        lambda control, gateway_id, name, arn, qualifier="DEFAULT": f"id-{name}",
    )
    monkeypatch.setattr(
        exp_svc,
        "create_online_eval_idempotent",
        lambda control, **kwargs: {
            "onlineEvaluationConfigId": f"id-{kwargs['name']}",
            "onlineEvaluationConfigArn": f"arn:{kwargs['name']}",
        },
    )

    class ConflictException(Exception):
        pass

    data = MagicMock()
    data.create_ab_test.side_effect = ConflictException("already exists")
    data.list_ab_tests.return_value = {
        "abTests": [
            {
                "abTestId": "ab-adopted",
                "name": f"can_{row.id[:8]}_target",
                "gatewayArn": "arn:gw",
                "executionStatus": "RUNNING",
            }
        ]
    }
    monkeypatch.setattr(canary_svc, "data_client", lambda: data)
    monkeypatch.setattr(canary_svc, "control_client", MagicMock)

    result = canary_svc.act_setup(row.id, lambda message: None)

    assert result["ab_test_id"] == "ab-adopted"
    assert _reload(row.id).artifacts["setup"]["test_name"] == (
        f"can_{row.id[:8]}_target"
    )


# ─── invoke-hot-path route (provisioning vs live gateway) ────────────────────
def test_active_route_is_provisioning_form_before_gateway_is_live():
    # Mirrors the PARTIAL setup act_setup persists before the mint: stable
    # endpoint stood up, gateway created, but no A/B test / control target yet.
    _mk_canary(
        agent_id="agentp",
        artifacts={
            "agent_meta": {"id": "agentp", "arn": "arn:agentp"},
            "edited_spec": {},
            "setup": {
                "runtime_id": "agentp-res",
                "stable_endpoint": "stablep",
                "v_current": "3",
                "gateway_id": "gw-p",
                "gateway_arn": "arn:gw-p",
                "gateway_url": "https://gw-p",
            },
        },
    )
    route = canary_svc.active_canary_route("agentp")
    # provisioning form: no gateway_url / control_target → invoke serves v_current
    # via the stable endpoint, NEVER DEFAULT (the untested candidate).
    assert route == {
        "runtime_id": "agentp-res",
        "arn": "arn:agentp",
        "stable_endpoint": "stablep",
        "v_current": "3",
    }


def test_active_route_is_live_gateway_form_once_ab_test_exists():
    _mk_canary(
        agent_id="agentl",
        artifacts={
            "agent_meta": {"id": "agentl", "arn": "arn:agentl"},
            "edited_spec": {},
            "setup": _setup_artifact(),
        },
    )
    route = canary_svc.active_canary_route("agentl")
    assert route["gateway_url"] == "https://gateway.example"
    assert route["control_target"] == "cancontrol"
    assert route["stable_endpoint"] == "stablecan"
    assert route["arn"] == "arn:agentl"
    assert route["runtime_id"] == "subject-abcdefghij"


def test_active_route_none_without_stable_endpoint():
    _mk_canary(
        agent_id="agentn",
        artifacts={
            "agent_meta": {"id": "agentn", "arn": "arn:agentn"},
            "edited_spec": {},
            "rounds": [],
        },
    )
    assert canary_svc.active_canary_route("agentn") is None


# ─── verdict policy ──────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("verdict", "allow_override", "error_code"),
    [
        ({"verdict": "treatment-wins", "significant": True}, False, None),
        (
            {"verdict": "treatment-wins", "significant": False},
            False,
            "canary.override_required",
        ),
        ({"verdict": "tie", "significant": False}, False, "canary.override_required"),
        (
            {"verdict": "control-wins", "significant": False},
            True,
            "canary.verdict_blocked",
        ),
        ({"verdict": "insufficient-n"}, True, "canary.verdict_blocked"),
    ],
)
def test_verdict_gate(verdict, allow_override, error_code):
    row = _mk_canary(
        artifacts={
            "setup": _setup_artifact(),
            "rounds": [_round(0, verdict=verdict)],
        }
    )
    if error_code is None:
        canary_svc.assert_verdict_allows(
            row, allow_non_significant=allow_override
        )
        return
    with pytest.raises(AppError) as exc:
        canary_svc.assert_verdict_allows(
            row, allow_non_significant=allow_override
        )
    assert exc.value.code == error_code


def test_non_significant_verdict_allows_explicit_override():
    row = _mk_canary(
        artifacts={
            "setup": _setup_artifact(),
            "rounds": [
                _round(
                    0,
                    verdict={
                        "verdict": "treatment-wins",
                        "significant": False,
                    },
                )
            ],
        }
    )
    canary_svc.assert_verdict_allows(row, allow_non_significant=True)


def test_advance_requires_current_stage_traffic_and_verdict():
    row = _mk_canary(artifacts={"setup": _setup_artifact(), "rounds": []})
    assert canary_svc.stage_not_ready_reason(row, "verdict") == (
        "send traffic at the current weights first"
    )
    assert canary_svc.stage_not_ready_reason(row, "advance") == (
        "record a verdict at the current weights first"
    )


def test_new_ramp_stage_cannot_reuse_prior_round_evidence():
    row = _mk_canary(
        artifacts={
            "setup": _setup_artifact(ramp_stage=1),
            "rounds": [
                _round(
                    0,
                    verdict={"verdict": "treatment-wins", "significant": True},
                )
            ],
        }
    )

    assert canary_svc.stage_not_ready_reason(row, "verdict") == (
        "send traffic at the current weights first"
    )
    assert canary_svc.stage_not_ready_reason(row, "advance") == (
        "record a verdict at the current weights first"
    )


def test_final_stage_must_complete_instead_of_advance(client):
    row = _mk_canary(
        artifacts={
            "setup": _setup_artifact(ramp_stage=2),
            "rounds": [
                _round(
                    2,
                    verdict={"verdict": "treatment-wins", "significant": True},
                )
            ],
        }
    )

    res = client.post(
        f"/api/runtime-canaries/{row.id}/action",
        json={"action": "advance"},
    )

    assert res.status_code == 409
    assert res.json()["code"] == "canary.stage_not_ready"


def test_complete_requires_final_stage(client):
    row = _mk_canary(
        artifacts={
            "setup": _setup_artifact(ramp_stage=1),
            "rounds": [
                _round(
                    1,
                    verdict={"verdict": "treatment-wins", "significant": True},
                )
            ],
        }
    )

    res = client.post(
        f"/api/runtime-canaries/{row.id}/action",
        json={"action": "complete"},
    )

    assert res.status_code == 409
    assert res.json()["code"] == "canary.stage_not_ready"


# ─── traffic / verdict / advance mechanics ───────────────────────────────────
def test_traffic_action_requires_dataset(client):
    row = _mk_canary(
        artifacts={
            "setup": _setup_artifact(),
            "rounds": [],
        }
    )

    res = client.post(
        f"/api/runtime-canaries/{row.id}/action",
        json={"action": "traffic"},
    )

    assert res.status_code == 422
    assert res.json()["code"] == "canary.dataset_required"
    assert _reload(row.id).artifacts["rounds"] == []


def test_traffic_records_metric_baseline_and_clears_prior_verdict(monkeypatch):
    row = _mk_canary(
        artifacts={
            "setup": _setup_artifact(),
            "rounds": [
                _round(
                    0,
                    verdict={"verdict": "treatment-wins", "significant": True},
                )
            ],
        }
    )
    data = MagicMock()
    data.get_ab_test.return_value = {
        "results": {
            "evaluatorMetrics": [
                {
                    "evaluatorArn": "arn:evaluator",
                    "controlStats": {"mean": 0.5, "sampleSize": 2},
                    "variantResults": [
                        {"name": "T1", "mean": 0.7, "sampleSize": 3}
                    ],
                }
            ]
        }
    }
    monkeypatch.setattr(canary_svc, "data_client", lambda: data)
    monkeypatch.setattr(
        exp_svc,
        "send_gateway_traffic",
        lambda *args, **kwargs: {
            "session_ids": ["s1"],
            "sent": 1,
            "failed": 0,
        },
    )

    result = canary_svc.act_traffic(
        row.id, ["prompt"], {"dataset_id": "d1"}, lambda message: None
    )

    assert result["baseline_n"] == 5
    stored_round = _reload(row.id).artifacts["rounds"][0]
    assert "verdict" not in stored_round
    assert stored_round["traffic_attempts"][-1]["dataset_id"] == "d1"


def test_verdict_waits_for_sample_growth_and_persists_current_round(monkeypatch):
    row = _mk_canary(
        artifacts={
            "setup": _setup_artifact(),
            "rounds": [_round(0, baseline_n=5)],
        }
    )
    data = MagicMock()
    data.get_ab_test.return_value = {
        "executionStatus": "RUNNING",
        "results": {
            "evaluatorMetrics": [
                {
                    "evaluatorArn": "arn:evaluator",
                    "controlStats": {"mean": 0.5, "sampleSize": 3},
                    "variantResults": [
                        {
                            "name": "T1",
                            "mean": 0.8,
                            "sampleSize": 4,
                            "isSignificant": True,
                        }
                    ],
                }
            ]
        },
    }
    monkeypatch.setattr(canary_svc, "data_client", lambda: data)

    verdict = canary_svc.act_verdict(row.id, lambda message: None)

    assert verdict["verdict"] == "treatment-wins"
    assert verdict["n"] == 7
    assert verdict["baseline_n"] == 5
    assert _reload(row.id).artifacts["rounds"][0]["verdict"]["n"] == 7


def test_advance_updates_only_canary_ab_weights(monkeypatch):
    row = _mk_canary(
        artifacts={
            "setup": _setup_artifact(),
            "rounds": [
                _round(
                    0,
                    verdict={"verdict": "treatment-wins", "significant": True},
                )
            ],
        }
    )
    captured: dict = {}
    monkeypatch.setattr(canary_svc, "data_client", MagicMock)
    monkeypatch.setattr(
        exp_svc,
        "update_weights_with_pause",
        lambda data, ab_test_id, variants: captured.update(
            ab_test_id=ab_test_id, variants=variants
        ),
    )

    result = canary_svc.act_advance(
        row.id, lambda message: None, allow_non_significant=False
    )

    assert result == {"ramp_stage": 1, "weights": {"C": 50, "T1": 50}}
    assert captured["ab_test_id"] == "ab-1"
    assert [variant["weight"] for variant in captured["variants"]] == [50, 50]


# ─── promote / rollback / cleanup (Option B: DEFAULT is production truth) ─────
def _boom_endpoint(*args, **kwargs):
    raise AssertionError("promote must not touch named endpoints under Option B")


def test_complete_records_candidate_as_production(monkeypatch):
    full_spec = AgentSpec(
        name="subject", method="zip_runtime", system_prompt="orig"
    ).model_dump()
    (agent_id,) = _persist_agents(_agent("subject", spec=full_spec))
    edited = AgentSpec(
        name="subject", method="zip_runtime", system_prompt="promoted prompt"
    ).model_dump()
    row = _mk_canary(
        agent_id=agent_id,
        artifacts={
            "agent_meta": {"id": agent_id},
            "edited_spec": edited,
            "setup": _setup_artifact(ramp_stage=2),
            "rounds": [
                _round(
                    2,
                    verdict={"verdict": "treatment-wins", "significant": True},
                )
            ],
        },
    )
    stopped: dict = {}
    monkeypatch.setattr(canary_svc, "control_client", MagicMock)
    monkeypatch.setattr(canary_svc, "data_client", MagicMock)
    monkeypatch.setattr(
        exp_svc,
        "_stop_ab_test",
        lambda data, ab_test_id, progress, **kwargs: (
            stopped.update(ab_test_id=ab_test_id) or {"executionStatus": "STOPPED"}
        ),
    )
    # Option B: promote does NOT repoint or delete named endpoints.
    monkeypatch.setattr(
        canary_svc.canary_infra, "promote_stable_endpoint", _boom_endpoint
    )
    monkeypatch.setattr(
        canary_svc.canary_infra, "delete_endpoint_quiet", _boom_endpoint
    )

    result = canary_svc.act_complete(
        row.id, lambda message: None, allow_non_significant=False
    )

    assert stopped["ab_test_id"] == "ab-1"
    assert result["winner"] == "challenger"
    assert result["promoted_version"] == "2"
    assert result["ab_test_status"] == "STOPPED"
    assert "experimental_only" not in result
    # the ledger now reflects production = candidate (DEFAULT already serves it)
    agent = _reload_agent(agent_id)
    assert agent.version == "2"
    assert agent.spec["system_prompt"] == "promoted prompt"
    stored = _reload(row.id)
    assert stored.status == "completed"
    assert stored.artifacts["complete"]["ab_test_status"] == "STOPPED"


def test_rollback_rolls_forward_current_spec(monkeypatch):
    full_spec = AgentSpec(
        name="subject", method="zip_runtime", system_prompt="orig"
    ).model_dump()
    (agent_id,) = _persist_agents(_agent("subject", spec=full_spec))
    row = _mk_canary(
        agent_id=agent_id,
        artifacts={
            "agent_meta": {"id": agent_id},
            "edited_spec": {},
            "setup": _setup_artifact(),
            "rounds": [],
        },
    )
    captured: dict = {}
    monkeypatch.setattr(canary_svc, "control_client", MagicMock)
    monkeypatch.setattr(canary_svc, "data_client", MagicMock)
    monkeypatch.setattr(
        exp_svc,
        "_stop_ab_test",
        lambda data, ab_test_id, progress, **kwargs: (
            captured.update(ab_test_id=ab_test_id) or {"executionStatus": "STOPPED"}
        ),
    )
    minted: dict = {}

    def fake_mint(*, agent, edited_spec, control_client, log, canary_id, role):
        minted["agent_id"] = agent.id
        minted["spec"] = edited_spec
        minted["role"] = role
        return ("1", "9", f"agents/subject/canary/{canary_id}-{role}.zip")

    monkeypatch.setattr(canary_svc.canary_infra, "mint_candidate_version", fake_mint)

    result = canary_svc.act_rollback(row.id, lambda message: None)

    assert captured["ab_test_id"] == "ab-1"
    assert result["winner"] == "champion"
    assert result["restored_version"] == "9"
    assert result["ab_test_status"] == "STOPPED"
    assert "experimental_only" not in result
    # roll-forward re-publishes the agent's CURRENT (unchanged) spec
    assert minted["agent_id"] == agent_id
    assert minted["spec"].system_prompt == "orig"
    assert _reload(row.id).status == "rolled_back"
    # DEFAULT (production truth) now serves the restored version
    assert _reload_agent(agent_id).version == "9"


def test_rollback_allowed_and_rolls_forward_on_partial_setup(monkeypatch):
    # A setup that failed AFTER standing up the stable endpoint but BEFORE the A/B
    # test: rollback must still be allowed (safety valve) and must roll production
    # forward off any minted candidate without trying to stop a non-existent test.
    full_spec = AgentSpec(
        name="subject", method="zip_runtime", system_prompt="orig"
    ).model_dump()
    (agent_id,) = _persist_agents(_agent("subject", spec=full_spec))
    row = _mk_canary(
        agent_id=agent_id,
        artifacts={
            "agent_meta": {"id": agent_id},
            "edited_spec": {},
            "setup": {
                "runtime_id": "subject-res",
                "stable_endpoint": "stablecan",
                "v_current": "1",
                "gateway_id": "gw-1",
            },
            "rounds": [],
        },
    )
    # rollback is the safety valve — allowed even with only a partial setup
    assert canary_svc.stage_not_ready_reason(row, "rollback") is None

    stop_calls: list = []
    monkeypatch.setattr(canary_svc, "control_client", MagicMock)
    monkeypatch.setattr(canary_svc, "data_client", MagicMock)
    monkeypatch.setattr(
        exp_svc,
        "_stop_ab_test",
        lambda *a, **k: stop_calls.append(True) or {"executionStatus": "STOPPED"},
    )
    minted: dict = {}

    def fake_mint(*, agent, edited_spec, control_client, log, canary_id, role):
        minted["spec"] = edited_spec
        return ("1", "5", f"agents/subject/canary/{canary_id}-{role}.zip")

    monkeypatch.setattr(canary_svc.canary_infra, "mint_candidate_version", fake_mint)

    result = canary_svc.act_rollback(row.id, lambda message: None)

    # no A/B test in the partial setup → _stop_ab_test is never called
    assert stop_calls == []
    assert result["ab_test_status"] is None
    # roll-forward re-publishes the agent's CURRENT (unchanged) spec → v_current
    assert minted["spec"].system_prompt == "orig"
    assert result["restored_version"] == "5"
    assert _reload(row.id).status == "rolled_back"
    assert _reload_agent(agent_id).version == "5"


def test_second_concurrent_canary_for_same_agent_is_rejected(client):
    (agent_id,) = _persist_agents(_agent("subject"))
    _mk_canary(agent_id=agent_id, status="running")

    res = client.post(
        "/api/runtime-canaries",
        json={"agent_id": agent_id, "candidate": {"system_prompt": "new"}},
    )
    assert res.status_code == 409
    assert res.json()["code"] == "canary.already_running"


def test_cleanup_deletes_dedicated_gateway_and_both_endpoints(monkeypatch):
    row = _mk_canary(artifacts={"setup": _setup_artifact(), "rounds": []})
    monkeypatch.setattr(
        canary_svc,
        "_owned_resources",
        lambda row, control, data: (
            "gw-1",
            ["target-c", "target-t"],
            ["oe-c", "oe-t"],
            ["ab-1"],
        ),
    )
    monkeypatch.setattr(exp_svc, "_stop_ab_test", lambda *args, **kwargs: {})
    captured: dict = {}

    def fake_cleanup(control, data, **kwargs):
        captured.update(kwargs)
        return [{"category": "abtest:ab-1", "status": "deleted", "detail": ""}]

    monkeypatch.setattr(canary_svc.ac, "cleanup_resources", fake_cleanup)
    deleted_gateways: list[str] = []
    monkeypatch.setattr(
        canary_svc.canary_infra,
        "delete_canary_gateway",
        lambda control, gateway_id, **kw: deleted_gateways.append(gateway_id),
    )
    deleted_endpoints: list[str] = []
    monkeypatch.setattr(
        canary_svc.canary_infra,
        "delete_endpoint_quiet",
        lambda control, **kw: deleted_endpoints.append(kw["endpoint_name"]),
    )
    monkeypatch.setattr(canary_svc, "control_client", MagicMock)
    monkeypatch.setattr(canary_svc, "data_client", MagicMock)

    canary_svc.act_cleanup(row.id, lambda message: None)

    # ac.cleanup_resources never deletes the gateway; the per-canary gateway is
    # deleted explicitly instead.
    assert captured["delete_gateway"] is False
    assert captured["gateway_id"] == "gw-1"
    assert deleted_gateways == ["gw-1"]
    # BOTH named endpoints are deleted (production uses DEFAULT post-canary).
    assert set(deleted_endpoints) == {"stablecan", "treatcan"}
    assert _reload(row.id).status == "cleaned"


def test_clear_stale_canary_actions_is_retryable():
    row = _mk_canary()
    db = SessionLocal()
    try:
        stored = db.get(RuntimeCanary, row.id)
        stored.running_action = "traffic"
        stored.progress = "sending"
        db.commit()
    finally:
        db.close()

    assert canary_svc.clear_stale_running_actions() == [row.id]
    stored = _reload(row.id)
    assert stored.running_action is None
    assert stored.progress is None
    assert stored.error.startswith("traffic: interrupted")


def _cleanup_stubs(monkeypatch, *, live_version: str):
    """Silence every non-S3 teardown so a test can assert on the S3 decisions."""
    monkeypatch.setattr(
        canary_svc, "_owned_resources", lambda row, control, data: (None, [], [], []))
    monkeypatch.setattr(canary_svc.ac, "cleanup_resources", lambda control, data, **kw: [])
    monkeypatch.setattr(
        canary_svc.canary_infra, "delete_endpoint_quiet", lambda control, **kw: None)
    monkeypatch.setattr(
        canary_svc.canary_infra, "current_version",
        lambda control, runtime_id: live_version)
    monkeypatch.setattr(canary_svc, "control_client", MagicMock)
    monkeypatch.setattr(canary_svc, "data_client", MagicMock)
    deleted: list[str] = []
    monkeypatch.setattr(
        canary_svc.canary_infra, "delete_object_quiet",
        lambda key, **kw: deleted.append(key))
    return deleted


def _s3_rows(results: list[dict[str, str]]) -> dict[str, dict[str, str]]:
    return {row["category"]: row for row in results if row["category"].startswith("s3:")}


def test_cleanup_deletes_the_superseded_candidate_zip_after_rollback(monkeypatch):
    """Both mints leave a ~37MB object behind (ISSUE-011). After a rollback the
    restored version is live, so only the rejected candidate's zip may go."""
    setup = {**_setup_artifact(), "candidate_s3_key": "agents/s/canary/c1-candidate.zip"}
    row = _mk_canary(artifacts={
        "setup": setup,
        "rollback": {"restored_version": "3",
                     "restored_s3_key": "agents/s/canary/c1-restore.zip"},
        "rounds": [],
    })
    deleted = _cleanup_stubs(monkeypatch, live_version="3")

    rows = _s3_rows(canary_svc.act_cleanup(row.id, lambda message: None))

    assert deleted == ["agents/s/canary/c1-candidate.zip"]
    assert rows["s3:agents/s/canary/c1-candidate.zip"]["status"] == "deleted"
    restored = rows["s3:agents/s/canary/c1-restore.zip"]
    assert restored["status"] == "skipped"
    assert "live version 3" in restored["detail"]


def test_cleanup_keeps_the_candidate_zip_when_it_is_still_live(monkeypatch):
    """Promoted canary: production runs the candidate, so its zip stays."""
    setup = {**_setup_artifact(), "candidate_s3_key": "agents/s/canary/c1-candidate.zip"}
    row = _mk_canary(artifacts={"setup": setup, "rounds": []})
    deleted = _cleanup_stubs(monkeypatch, live_version="2")  # v_candidate == "2"

    rows = _s3_rows(canary_svc.act_cleanup(row.id, lambda message: None))

    assert deleted == []
    assert rows["s3:agents/s/canary/c1-candidate.zip"]["status"] == "skipped"


def test_cleanup_of_a_row_without_recorded_keys_touches_no_s3(monkeypatch):
    """Rows written before the keys were recorded clean up exactly as before."""
    row = _mk_canary(artifacts={"setup": _setup_artifact(), "rounds": []})
    deleted = _cleanup_stubs(monkeypatch, live_version="2")

    results = canary_svc.act_cleanup(row.id, lambda message: None)

    assert deleted == []
    assert _s3_rows(results) == {}


def test_cleanup_keeps_objects_when_the_live_version_is_unknown(monkeypatch):
    setup = {**_setup_artifact(), "candidate_s3_key": "agents/s/canary/c1-candidate.zip"}
    row = _mk_canary(artifacts={"setup": setup, "rounds": []})
    deleted = _cleanup_stubs(monkeypatch, live_version="2")
    monkeypatch.setattr(
        canary_svc.canary_infra, "current_version",
        lambda control, runtime_id: (_ for _ in ()).throw(RuntimeError("throttled")))

    rows = _s3_rows(canary_svc.act_cleanup(row.id, lambda message: None))

    assert deleted == []
    assert rows["s3:agents/s/canary/c1-candidate.zip"]["status"] == "skipped"
    assert "live version unknown" in rows["s3:agents/s/canary/c1-candidate.zip"]["detail"]
