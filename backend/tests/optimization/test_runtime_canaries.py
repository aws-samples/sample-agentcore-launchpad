"""Runtime Canary records, guards, actions, and resource ownership (Model 1:
one agent, two immutable versions behind two named endpoints; CN N12 W-E3: the split
is a local A/B row routed by the invoke chain, replay runs on the evaluation worker)."""

from unittest.mock import MagicMock

import pytest

import app.optimization.canary_service as canary_svc
from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.core.errors import AppError
from app.models.ledger import Agent, SpecSnapshot
from app.optimization import routing, traffic
from app.optimization.models import ExperimentAssignment, RuntimeCanary
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
        workspace_id=DEFAULT_WORKSPACE_ID, name=name,
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
            workspace_id=DEFAULT_WORKSPACE_ID, name="CANARY-subject",
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
        "gateway_id": "invoke-chain",
        "test_name": "can_test_target",
        "ab_test_id": "local_can",
        # champion/challenger here mean the control/treatment ENDPOINTS.
        "champion": {
            "target_name": "stablecan",
            "online_eval_id": "oe-c",
            "online_eval_arn": "arn:oe-c",
        },
        "challenger": {
            "target_name": "treatcan",
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


def _abtest_artifact(ramp_stage: int = 0, execution_status: str = "RUNNING") -> dict:
    control_weight, treatment_weight = canary_svc.RAMP_WEIGHTS[ramp_stage]
    return {
        "ab_test_id": "local_can",
        "variants": routing.target_variants("stablecan", "treatcan", control_weight,
                                            treatment_weight),
        "execution_status": execution_status,
    }


def _live_artifacts(ramp_stage: int = 0, **extra) -> dict:
    """A canary whose setup completed: setup + the RUNNING local A/B row."""
    return {"agent_meta": {"id": "a1", "arn": RUNTIME_ARN, "resource_id": "subject-abcdefghij",
                           "runtime_name": "subject"},
            "setup": _setup_artifact(ramp_stage), "abtest": _abtest_artifact(ramp_stage),
            "rounds": [], **extra}


class _FakeOnline:
    """W-E2's online module seam (create_for_owner / delete_for_owner / disable_for_owner)."""

    def __init__(self) -> None:
        self.created: list[dict] = []
        self.deleted: list[str] = []
        self.disabled: list[str] = []

    def create_for_owner(self, ws, agent, *, name, evaluators, log_group, service_name,
                         sampling, timeout):
        self.created.append({"name": name, "evaluators": evaluators, "log_group": log_group,
                             "service_name": service_name, "sampling": sampling,
                             "timeout": timeout})
        from types import SimpleNamespace

        return SimpleNamespace(config_id=f"id-{name}", config_arn=f"arn:{name}")

    def delete_for_owner(self, ws, owner_id):
        self.deleted.append(owner_id)

    def disable_for_owner(self, ws, owner_id, *, db=None):
        self.disabled.append(owner_id)  # N17-obs442
        return []

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
    monkeypatch.setattr(canary_svc, "control_client", lambda _ws=None: control)

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
            # a Harness is canary-eligible (version A/B) once it has a Harness ARN
            _agent("harness", method="harness", arn=None),
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
    monkeypatch.setattr(canary_svc, "control_client", lambda _ws=None: control)

    res = client.post(
        "/api/runtime-canaries",
        json={"agent_id": agent_id, "candidate": {"code": "new studio code"}},
    )
    assert res.status_code == 201
    assert res.json()["artifacts"]["edited_spec"]["code"] == "new studio code"


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


def test_setup_mints_candidate_pins_endpoints_and_opens_the_local_ab_row(monkeypatch):
    subject = _agent("subject")
    subject.version = "1"  # the ledger version production serves at setup
    (agent_id,) = _persist_agents(subject)
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
    routes_during_mint: dict = {}

    def fake_mint(**kw):
        # the PARTIAL setup is persisted BEFORE the mint: every live session is pinned
        # to the stable endpoint while DEFAULT auto-rolls to the untested candidate
        stored = _reload(row.id).artifacts
        assert stored["setup"]["stable_endpoint"] and "abtest" not in stored
        db = SessionLocal()
        try:
            route = routing.route_for(db, agent_id, "live-session-" + "x" * 30)
        finally:
            db.close()
        routes_during_mint["qualifier"] = route.qualifier
        return ("1", "2", f"agents/subject/canary/{kw['canary_id']}-{kw['role']}.zip")

    monkeypatch.setattr(canary_svc.canary_infra, "mint_candidate_version", fake_mint)
    monkeypatch.setattr(
        canary_svc.canary_infra, "current_version", lambda control, runtime_id: "1"
    )
    endpoints_seen: dict = {}
    monkeypatch.setattr(
        canary_svc.canary_infra,
        "ensure_endpoint_ready",
        lambda control, *, runtime_id, endpoint_name, version, log=None: (
            endpoints_seen.update({endpoint_name: version}) or {"status": "READY"}
        ),
    )
    online = _FakeOnline()
    monkeypatch.setattr(canary_svc, "_online", lambda: online)
    monkeypatch.setattr(canary_svc, "control_client", MagicMock)

    result = canary_svc.act_setup(row.id, lambda message: None)

    assert result["ab_test_id"] == f"local_{row.id}"
    assert result["weights"] == {"C": 90, "T1": 10}
    assert result["v_current"] == "1"
    assert result["v_candidate"] == "2"
    assert result["runtime_id"] == "subject-res"
    assert result["stable_endpoint"].startswith("stable")
    assert result["treatment_endpoint"].startswith("treat")
    assert result["gateway_id"] == "invoke-chain"
    # the two "targets" ARE the named endpoints
    assert result["champion"]["target_name"] == result["stable_endpoint"]
    assert result["challenger"]["target_name"] == result["treatment_endpoint"]
    assert result["champion"]["online_eval_id"] == f"id-can_{row.id[:8]}_oec"
    assert result["challenger"]["online_eval_id"] == f"id-can_{row.id[:8]}_oet"
    # endpoints pinned stable→v_current, treatment→v_candidate
    assert endpoints_seen[result["stable_endpoint"]] == "1"
    assert endpoints_seen[result["treatment_endpoint"]] == "2"
    # live traffic was pinned to the stable endpoint during the mint window
    assert routes_during_mint["qualifier"] == result["stable_endpoint"]
    # per-arm online evals read each endpoint's own log group
    assert [c["log_group"] for c in online.created] == [
        f"/aws/bedrock-agentcore/runtimes/subject-res-{result['stable_endpoint']}",
        f"/aws/bedrock-agentcore/runtimes/subject-res-{result['treatment_endpoint']}",
    ]
    assert [c["service_name"] for c in online.created] == [
        f"SubjectRuntime.{result['stable_endpoint']}",
        f"SubjectRuntime.{result['treatment_endpoint']}",
    ]
    assert all(c["sampling"] == 100.0 and c["timeout"] == 2 for c in online.created)
    stored = _reload(row.id)
    assert stored.artifacts["setup"]["ramp_stage"] == 0
    # N17-obs16: the ledger version the candidate was built on — what `complete` / `rollback`
    # compare against (`refuse_stale_base`); without this key the belt is silently off
    assert stored.artifacts["setup"]["agent_version"] == "1" == _reload_agent(agent_id).version
    abtest = stored.artifacts["abtest"]
    assert abtest["execution_status"] == "RUNNING"
    assert routing.weights_of(abtest["variants"]) == {"C": 90, "T1": 10}
    # …and the invoke chain now splits by endpoint qualifier
    db = SessionLocal()
    try:
        owner = routing.active_owner(db, agent_id)
    finally:
        db.close()
    assert owner.kind == "canary" and owner.id == row.id
    assert owner.arm("C").qualifier == result["stable_endpoint"]
    assert owner.arm("T1").qualifier == result["treatment_endpoint"]


def _real_setup(monkeypatch, row_id: str) -> None:
    """Drive the REAL `act_setup` over the AWS stubs (the mint, the endpoints, the online
    evaluators) — what records `setup.agent_version` for the stale-base belt."""
    monkeypatch.setattr(
        canary_svc.canary_infra, "mint_candidate_version",
        lambda **kw: ("1", "2", f"agents/subject/canary/{kw['canary_id']}-{kw['role']}.zip"),
    )
    monkeypatch.setattr(canary_svc.canary_infra, "current_version", lambda control, rid: "1")
    monkeypatch.setattr(
        canary_svc.canary_infra, "ensure_endpoint_ready",
        lambda control, *, runtime_id, endpoint_name, version, log=None: {"status": "READY"},
    )
    monkeypatch.setattr(canary_svc, "_online", lambda: _FakeOnline())
    monkeypatch.setattr(canary_svc, "control_client", MagicMock)
    canary_svc.act_setup(row_id, lambda message: None)


def test_complete_after_a_real_setup_refuses_a_stale_base(monkeypatch):
    """N17-obs16 belt, end to end on the real `act_setup`: the setup records the ledger
    version production serves (`1`); a re-publish that lands afterwards moves it (`5`); the
    canary's `complete` is then `409 canary.stale_base {setup_version: "1", current_version:
    "5"}` before the A/B stop. Deleting the `agent_version` line from `act_setup`'s partial
    makes THIS test fail (the setup assertion above too) — the belt is not a tautology."""
    subject = _agent("subject")
    subject.version = "1"
    (agent_id,) = _persist_agents(subject)
    row = _mk_canary(
        agent_id=agent_id,
        artifacts={
            "agent_meta": {"id": agent_id, "name": "subject", "arn": "arn:agent",
                           "resource_id": "subject-res", "runtime_name": "SubjectRuntime"},
            "edited_spec": AgentSpec(name="subject", method="zip_runtime",
                                     system_prompt="edited").model_dump(),
            "rounds": [],
        },
    )
    _real_setup(monkeypatch, row.id)
    assert _reload(row.id).artifacts["setup"]["agent_version"] == "1"
    db = SessionLocal()
    try:
        db.get(Agent, agent_id).version = "5"  # a redeploy finished after the setup
        db.commit()
    finally:
        db.close()
    touched: list[str] = []
    monkeypatch.setattr(canary_svc, "_stop_ab_test", lambda *a, **k: touched.append("stop") or {})
    monkeypatch.setattr(canary_svc, "assert_verdict_allows", lambda *a, **k: touched.append("v"))

    with pytest.raises(AppError) as exc:
        canary_svc.act_complete(row.id, lambda message: None, allow_non_significant=True)

    assert exc.value.code == "canary.stale_base" and exc.value.status_code == 409
    assert exc.value.detail == {"setup_version": "1", "current_version": "5",
                                "action": "complete"}
    assert touched == []
    stored = _reload(row.id)
    assert stored.status == "running" and _reload_agent(agent_id).version == "5"


def test_create_refuses_a_system_managed_preset(client):
    """agents.md §1.1 (rev-r17 on cn/r17-d): the create route refuses a preset subject up
    front — `canary_capability` answers `system-managed` for a row with a `system_key`, so no
    `running` canary row can ever name a preset (the action route's `assert_not_system_agent`
    is the belt for legacy rows)."""
    preset = _agent("preset-subject")
    preset.system_key = "hr-helpdesk"
    (agent_id,) = _persist_agents(preset)

    res = client.post("/api/runtime-canaries",
                      json={"agent_id": agent_id, "candidate": {"system_prompt": "p"}})

    assert res.status_code == 400, res.text
    body = res.json()
    assert body["code"] == "canary.agent_unsupported"
    assert body["detail"]["canary_capability"]["reason_code"] == "system-managed"
    db = SessionLocal()
    try:
        assert db.query(RuntimeCanary).filter_by(champion_agent_id=agent_id).count() == 0
    finally:
        db.close()


def test_setup_is_refused_while_another_canary_routes_the_agent():
    """One owner per agent (``routing.active_owner``): a second canary of the same agent
    whose stable endpoint is already provisioned refuses this one's setup. (The retired
    configuration experiment can no longer be that owner — N13-obs2.)"""
    (agent_id,) = _persist_agents(_agent("subject"))
    row = _mk_canary(agent_id=agent_id)
    _mk_canary(agent_id=agent_id, artifacts={
        "agent_meta": {"id": agent_id}, "edited_spec": {}, "rounds": [],
        "setup": {"stable_endpoint": "stableabc123", "runtime_id": "rid", "v_current": "3"},
    })
    with pytest.raises(AppError) as exc:
        canary_svc.assert_setup_available(row.id)
    assert exc.value.code == "canary.agent_busy"
    assert exc.value.detail["owner_kind"] == "canary"


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
        # N-SCN-obs15 (ruling 2026-10-14): a NON-significant control-wins is noise like a
        # tie — overridable for advance; a significant one (or an older payload without
        # `significant`) stays terminal even with the override
        (
            {"verdict": "control-wins", "significant": False},
            False,
            "canary.override_required",
        ),
        ({"verdict": "control-wins", "significant": False, "n": 72,
          "arms_n": {"control": 20, "treatment": 16}, "min_arm_n": 10}, True, None),
        ({"verdict": "control-wins", "significant": True}, False, "canary.verdict_blocked"),
        ({"verdict": "control-wins", "significant": True}, True, "canary.verdict_blocked"),
        ({"verdict": "control-wins"}, True, "canary.verdict_blocked"),
        # N-SCN-obs14: insufficient-n is weak evidence, not a loss — override advances
        ({"verdict": "insufficient-n"}, False, "canary.override_required"),
        ({"verdict": "insufficient-n", "arms_n": {"control": 32, "treatment": 4},
          "min_arm_n": 10}, True, None),
        ({"verdict": "insufficient-data"}, True, "canary.verdict_blocked"),
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


_INSUFFICIENT_N = {
    "verdict": "insufficient-n",
    "significant": False,
    "arms_n": {"control": 32, "treatment": 4},
    "min_arm_n": 10,
}


# N-SCN-obs14 follow-up (r7-m): `complete` is the terminal policy — insufficient-n is
# blocked there even with the override; everything else keeps the advance rules.
@pytest.mark.parametrize(
    ("verdict", "allow_override", "error_code"),
    [
        (_INSUFFICIENT_N, False, "canary.verdict_blocked"),
        (_INSUFFICIENT_N, True, "canary.verdict_blocked"),
        ({"verdict": "tie", "significant": False}, False, "canary.override_required"),
        ({"verdict": "tie", "significant": False}, True, None),
        (
            {"verdict": "treatment-wins", "significant": False},
            False,
            "canary.override_required",
        ),
        ({"verdict": "treatment-wins", "significant": False}, True, None),
        ({"verdict": "treatment-wins", "significant": True}, False, None),
        ({"verdict": "control-wins", "significant": True}, True, "canary.verdict_blocked"),
        # N-SCN-obs15: complete is never allowed on ANY control-wins — the current version
        # wins, there is nothing to promote (advance may override the non-significant one)
        ({"verdict": "control-wins", "significant": False}, False, "canary.verdict_blocked"),
        ({"verdict": "control-wins", "significant": False}, True, "canary.verdict_blocked"),
        ({"verdict": "insufficient-data"}, True, "canary.verdict_blocked"),
    ],
)
def test_complete_verdict_gate(verdict, allow_override, error_code):
    row = _mk_canary(
        artifacts={
            "setup": _setup_artifact(ramp_stage=2),
            "rounds": [_round(2, verdict=verdict)],
        }
    )
    if error_code is None:
        canary_svc.assert_verdict_allows(
            row, allow_non_significant=allow_override, terminal=True
        )
        return
    with pytest.raises(AppError) as exc:
        canary_svc.assert_verdict_allows(
            row, allow_non_significant=allow_override, terminal=True
        )
    assert exc.value.code == error_code
    assert exc.value.status_code == 409


def test_complete_blocked_on_insufficient_n_names_both_arms_and_the_bar():
    row = _mk_canary(
        artifacts={
            "setup": _setup_artifact(ramp_stage=2),
            "rounds": [_round(2, verdict=_INSUFFICIENT_N)],
        }
    )
    with pytest.raises(AppError) as exc:
        canary_svc.assert_verdict_allows(
            row, allow_non_significant=True, terminal=True
        )
    assert exc.value.code == "canary.verdict_blocked"
    assert exc.value.status_code == 409
    assert (
        "send a traffic round with sessions_per_arm ≥ 10 "
        "(control has 32, treatment has 4)"
    ) in exc.value.message
    assert exc.value.detail["terminal"] is True
    # the same verdict is still advance-able with the override (advance is not terminal)
    canary_svc.assert_verdict_allows(row, allow_non_significant=True)


def test_complete_blocked_on_insufficient_n_falls_back_to_the_settings_bar():
    """An older insufficient-n payload without arms_n / min_arm_n still blocks and
    names the configured bar."""
    row = _mk_canary(
        artifacts={
            "setup": _setup_artifact(ramp_stage=2),
            "rounds": [_round(2, verdict={"verdict": "insufficient-n"})],
        }
    )
    with pytest.raises(AppError) as exc:
        canary_svc.assert_verdict_allows(
            row, allow_non_significant=True, terminal=True
        )
    assert "sessions_per_arm ≥ 10 (control has ?, treatment has ?)" in exc.value.message


def test_complete_route_refuses_insufficient_n_with_and_without_override(client, monkeypatch):
    dispatched: list[str] = []
    monkeypatch.setattr(
        canary_svc, "run_action", lambda canary_id, action, fn: dispatched.append(action)
    )
    row = _mk_canary(
        artifacts={
            "setup": _setup_artifact(ramp_stage=2),
            "rounds": [_round(2, verdict=_INSUFFICIENT_N)],
        }
    )
    for body in ({"action": "complete"}, {"action": "complete", "allow_non_significant": True}):
        res = client.post(f"/api/runtime-canaries/{row.id}/action", json=body)
        assert res.status_code == 409, res.text
        assert res.json()["code"] == "canary.verdict_blocked"
        assert "sessions_per_arm ≥ 10" in res.json()["message"]
    assert dispatched == []


def test_advance_route_on_insufficient_n_needs_the_override_only(client, monkeypatch):
    dispatched: list[str] = []
    monkeypatch.setattr(
        canary_svc, "run_action", lambda canary_id, action, fn: dispatched.append(action)
    )
    row = _mk_canary(
        artifacts={
            "setup": _setup_artifact(ramp_stage=0),
            "rounds": [_round(0, verdict=_INSUFFICIENT_N)],
        }
    )
    res = client.post(f"/api/runtime-canaries/{row.id}/action", json={"action": "advance"})
    assert res.status_code == 409, res.text
    assert res.json()["code"] == "canary.override_required"
    res = client.post(
        f"/api/runtime-canaries/{row.id}/action",
        json={"action": "advance", "allow_non_significant": True},
    )
    assert res.status_code == 202, res.text
    assert dispatched == ["advance"]


_NON_SIG_CONTROL_WINS = {
    "verdict": "control-wins",
    "significant": False,
    "n": 72,
    "avg_delta": -0.09,
    "arms_n": {"control": 20, "treatment": 16},
    "min_arm_n": 10,
}


# N-SCN-obs15 (user ruling 2026-10-14): the scn-t01 confirmation run's 50/50 verdict —
# control-wins, n 72, p 0.27 / 0.16, both arms above the bar — is overridable `advance`
# evidence like a tie; a SIGNIFICANT control-wins stays terminal; `complete` refuses
# every control-wins.
def test_advance_route_on_non_significant_control_wins_needs_the_override_only(
    client, monkeypatch,
):
    dispatched: list[str] = []
    monkeypatch.setattr(
        canary_svc, "run_action", lambda canary_id, action, fn: dispatched.append(action)
    )
    row = _mk_canary(
        artifacts={
            "setup": _setup_artifact(ramp_stage=1),
            "rounds": [_round(1, verdict=_NON_SIG_CONTROL_WINS)],
        }
    )
    res = client.post(f"/api/runtime-canaries/{row.id}/action", json={"action": "advance"})
    assert res.status_code == 409, res.text
    assert res.json()["code"] == "canary.override_required"
    assert "control-wins" in res.json()["message"]
    assert dispatched == []
    res = client.post(
        f"/api/runtime-canaries/{row.id}/action",
        json={"action": "advance", "allow_non_significant": True},
    )
    assert res.status_code == 202, res.text
    assert dispatched == ["advance"]


def test_advance_route_keeps_a_significant_control_wins_terminal(client, monkeypatch):
    dispatched: list[str] = []
    monkeypatch.setattr(
        canary_svc, "run_action", lambda canary_id, action, fn: dispatched.append(action)
    )
    row = _mk_canary(
        artifacts={
            "setup": _setup_artifact(ramp_stage=1),
            "rounds": [_round(1, verdict={**_NON_SIG_CONTROL_WINS, "significant": True})],
        }
    )
    for body in ({"action": "advance"}, {"action": "advance", "allow_non_significant": True}):
        res = client.post(f"/api/runtime-canaries/{row.id}/action", json=body)
        assert res.status_code == 409, res.text
        assert res.json()["code"] == "canary.verdict_blocked"
    assert dispatched == []


@pytest.mark.parametrize("significant", [False, True])
def test_complete_route_refuses_any_control_wins(client, monkeypatch, significant):
    dispatched: list[str] = []
    monkeypatch.setattr(
        canary_svc, "run_action", lambda canary_id, action, fn: dispatched.append(action)
    )
    row = _mk_canary(
        artifacts={
            "setup": _setup_artifact(ramp_stage=2),
            "rounds": [_round(2, verdict={**_NON_SIG_CONTROL_WINS, "significant": significant})],
        }
    )
    for body in ({"action": "complete"}, {"action": "complete", "allow_non_significant": True}):
        res = client.post(f"/api/runtime-canaries/{row.id}/action", json=body)
        assert res.status_code == 409, res.text
        assert res.json()["code"] == "canary.verdict_blocked"
        assert "nothing to promote" in res.json()["message"]
    assert dispatched == []


def test_act_complete_refuses_insufficient_n_before_touching_aws(monkeypatch):
    row = _mk_canary(
        artifacts={
            "agent_meta": {"id": "a1"},
            "edited_spec": {},
            "setup": _setup_artifact(ramp_stage=2),
            "abtest": _abtest_artifact(ramp_stage=2),
            "rounds": [_round(2, verdict=_INSUFFICIENT_N)],
        }
    )

    def _boom(*args, **kwargs):
        raise AssertionError("complete must not reach AWS on insufficient-n")

    monkeypatch.setattr(canary_svc, "_stop_ab_test", _boom)
    with pytest.raises(AppError) as exc:
        canary_svc.act_complete(row.id, lambda message: None, allow_non_significant=True)
    assert exc.value.code == "canary.verdict_blocked"
    assert _reload(row.id).status == "running"


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


def _metrics(c_n: int, t_n: int, *, significant: bool = False) -> list[dict]:
    return [{
        "evaluatorId": "Builtin.Helpfulness", "label": "Builtin.Helpfulness", "polarity": 1,
        "control": {"name": "C", "mean": 0.5, "sampleSize": c_n},
        "variants": [{"name": "T1", "mean": 0.8, "sampleSize": t_n,
                      "isSignificant": significant}],
    }]


def test_traffic_records_metric_baseline_and_clears_prior_verdict(monkeypatch):
    row = _mk_canary(
        artifacts=_live_artifacts(rounds=[
            _round(0, verdict={"verdict": "treatment-wins", "significant": True})]),
    )
    monkeypatch.setattr(canary_svc, "ab_metrics",
                        lambda row: ({"executionStatus": "RUNNING"}, _metrics(2, 3)))
    planned: dict = {}

    def fake_plan(ws, agent, items, owner, *, evaluators, **kw):
        planned.update(items=list(items), owner=owner, evaluators=list(evaluators))
        return "job", {"sid": {"arm": "C"}}

    monkeypatch.setattr(traffic, "plan", fake_plan)
    monkeypatch.setattr(
        traffic, "run",
        lambda ws, owner, job, assigned, *, executor, progress, **kw: {
            "session_ids": ["s1"], "sent": 1, "failed": 0, "status_counts": {"ok": 1},
            "job_id": "tr1"},
    )
    monkeypatch.setattr(canary_svc.eval_executor, "select_executor",
                        lambda settings, *, job_type: f"executor:{job_type}")

    result = canary_svc.act_traffic(
        row.id, ["prompt"], {"dataset_id": "d1"}, lambda message: None,
        scenario_ids=["scn-1"],
    )

    assert result["baseline_n"] == 5
    assert result["job_id"] == "tr1"
    assert planned["items"] == [("scn-1", "prompt")]
    assert planned["owner"].kind == "canary" and planned["owner"].id == row.id
    assert planned["owner"].arm("T1").qualifier == "treatcan"
    assert planned["evaluators"] == list(canary_svc.CANARY_EVALUATORS)
    stored_round = _reload(row.id).artifacts["rounds"][0]
    assert "verdict" not in stored_round
    assert stored_round["traffic_attempts"][-1]["dataset_id"] == "d1"


def test_traffic_requires_a_running_ab_row():
    row = _mk_canary(artifacts=_live_artifacts())
    canary_svc.set_ab_execution_status(row.id, "STOPPED")
    with pytest.raises(RuntimeError, match="not running"):
        canary_svc.act_traffic(row.id, ["p"], {}, lambda message: None)

# ─── N-SCN-obs14: traffic sessions_per_arm ───────────────────────────────────
def _traffic_doubles(monkeypatch, arm_pattern: str, *, planned: dict):
    """Fake plan/run: sessions get arms by cycling ``arm_pattern`` ("CCCT" ≈ 75/25) so
    the trim logic is deterministic; ``planned`` records what the worker would run."""
    from types import SimpleNamespace

    def fake_plan(ws, agent, items, owner, *, evaluators, **kw):
        sessions, assigned = [], {}
        for index, (label, prompt) in enumerate(items):
            sid = f"s{index:02d}"
            # the live arm names: C / T1 (the pattern letter T stands for T1)
            arm = "T1" if arm_pattern[index % len(arm_pattern)] == "T" else "C"
            assigned[sid] = {"arm": arm, "qualifier": f"{arm}-ep"}
            sessions.append({"session_id": sid, "scenario_id": label, "prompts": [prompt],
                             **assigned[sid]})
        planned["items"] = list(items)
        return SimpleNamespace(spec={"sessions": sessions}), assigned

    def fake_run(ws, owner, job, assigned, *, executor, progress, **kw):
        planned["sessions"] = list(job.spec["sessions"])
        planned["assigned"] = dict(assigned)
        arms: dict[str, int] = {}
        for session in job.spec["sessions"]:
            arms[session["arm"]] = arms.get(session["arm"], 0) + 1
        return {"session_ids": [s["session_id"] for s in job.spec["sessions"]],
                "sent": len(job.spec["sessions"]), "failed": 0, "status_counts": {},
                "arms": arms, "job_id": "tr-cycle"}

    monkeypatch.setattr(traffic, "plan", fake_plan)
    monkeypatch.setattr(traffic, "run", fake_run)
    monkeypatch.setattr(canary_svc, "ab_metrics",
                        lambda row: ({"executionStatus": "RUNNING"}, []))
    monkeypatch.setattr(canary_svc.eval_executor, "select_executor",
                        lambda settings, *, job_type: "executor")
    dropped: list = []
    monkeypatch.setattr(canary_svc.routing, "reconcile",
                        lambda owner, invocations, *, planned=None, db=None:
                        dropped.extend(planned or []))
    return dropped


def test_traffic_without_sessions_per_arm_replays_the_dataset_once(monkeypatch):
    row = _mk_canary(artifacts=_live_artifacts())
    planned: dict = {}
    dropped = _traffic_doubles(monkeypatch, "CCCT", planned=planned)

    attempt = canary_svc.act_traffic(row.id, ["p1", "p2", "p3"], {"dataset_id": "d1"},
                                     lambda message: None, scenario_ids=["a", "b", "c"])

    assert [label for label, _p in planned["items"]] == ["a", "b", "c"]
    assert len(planned["sessions"]) == 3 and dropped == []
    assert "sessions_per_arm" not in attempt and "planned_sessions" not in attempt


def test_traffic_sessions_per_arm_cycles_the_dataset_until_every_arm_has_enough(monkeypatch):
    """3 prompts, ≈75/25 split, target 2 per arm: the first full pass (C C C) leaves the
    treatment at 0, so the round keeps cycling — s03 (T) → 1, s04 s05 s06 (C C C), s07 (T)
    → 2 — and stops there; the surplus pre-assigned sessions are reconciled away."""
    row = _mk_canary(artifacts=_live_artifacts())
    planned: dict = {}
    dropped = _traffic_doubles(monkeypatch, "CCCT", planned=planned)

    attempt = canary_svc.act_traffic(row.id, ["p1", "p2", "p3"], {"dataset_id": "d1"},
                                     lambda message: None, scenario_ids=["a", "b", "c"],
                                     sessions_per_arm=2)

    # the cap: 3 × dataset = 9 candidates, labels cycle with a pass suffix
    labels = [label for label, _p in planned["items"]]
    assert labels == ["a", "b", "c", "a#2", "b#2", "c#2", "a#3", "b#3", "c#3"]
    kept = [s["session_id"] for s in planned["sessions"]]
    assert kept == [f"s{i:02d}" for i in range(8)]
    assert sorted(dropped) == ["s08"]
    assert set(planned["assigned"]) == set(kept)
    assert attempt["arms"] == {"C": 6, "T1": 2}
    assert attempt["sessions_per_arm"] == 2 and attempt["planned_sessions"] == 8


def test_traffic_sessions_per_arm_stops_at_the_cap_when_an_arm_cannot_be_fed(monkeypatch):
    """A 90/10-like split cannot give the treatment 10 sessions from 3 prompts: the round
    sends the whole 3× cap and the verdict will say insufficient-n honestly."""
    row = _mk_canary(artifacts=_live_artifacts())
    planned: dict = {}
    dropped = _traffic_doubles(monkeypatch, "CCCCCCCCCT", planned=planned)

    attempt = canary_svc.act_traffic(row.id, ["p1", "p2", "p3"], {"dataset_id": "d1"},
                                     lambda message: None, sessions_per_arm=10)

    assert len(planned["sessions"]) == 9 and dropped == []
    assert attempt["planned_sessions"] == 9 and attempt["arms"] == {"C": 9}


def test_traffic_action_accepts_sessions_per_arm(client, monkeypatch):
    """The route carries the additive field through to the service (None when absent)."""
    from app.evaluation.models import EvalDataset

    row = _mk_canary(artifacts=_live_artifacts())
    captured: list = []
    monkeypatch.setattr(canary_svc, "run_action",
                        lambda canary_id, action, fn: captured.append(fn))
    monkeypatch.setattr(traffic, "resolve_traffic_items",
                        lambda dataset: [("scn-1", "hello")])
    db = SessionLocal()
    try:
        db.add(EvalDataset(id="ds-obs14", workspace_id=row.workspace_id, name="replay",
                           kind="predefined", items=[{"prompt": "hello"}]))
        db.commit()
    finally:
        db.close()

    res = client.post(f"/api/runtime-canaries/{row.id}/action",
                      json={"action": "traffic", "dataset_id": "ds-obs14",
                            "sessions_per_arm": 4})
    assert res.status_code == 202, res.text
    assert captured[-1].func is canary_svc.act_traffic
    assert captured[-1].keywords["sessions_per_arm"] == 4
    assert captured[-1].args[1:] == (["hello"], {"dataset_id": "ds-obs14",
                                                 "dataset_name": "replay"})

    res = client.post(f"/api/runtime-canaries/{row.id}/action",
                      json={"action": "traffic", "dataset_id": "ds-obs14"})
    assert res.status_code == 202, res.text
    assert captured[-1].keywords["sessions_per_arm"] is None

    bad = client.post(f"/api/runtime-canaries/{row.id}/action",
                      json={"action": "traffic", "dataset_id": "ds-obs14",
                            "sessions_per_arm": 0})
    assert bad.status_code == 422


def test_verdict_waits_for_sample_growth_and_persists_current_round(monkeypatch):
    row = _mk_canary(artifacts=_live_artifacts(rounds=[_round(0, baseline_n=5)]))
    reads = iter([_metrics(2, 3), _metrics(12, 11, significant=True)])
    monkeypatch.setattr(canary_svc, "ab_metrics",
                        lambda row: ({"executionStatus": "RUNNING"}, next(reads)))
    monkeypatch.setattr(canary_svc, "_sleep", lambda *_a, **_k: None)

    verdict = canary_svc.act_verdict(row.id, lambda message: None)

    assert verdict["verdict"] == "treatment-wins"
    assert verdict["n"] == 23
    assert verdict["baseline_n"] == 5
    # N-SCN-obs14: the per-arm counts and the bar ride on the stored verdict payload
    assert verdict["arms_n"] == {"control": 12, "treatment": 11}
    assert verdict["min_arm_n"] == 10
    stored = _reload(row.id).artifacts["rounds"][0]["verdict"]
    assert stored["n"] == 23 and stored["arms_n"] == {"control": 12, "treatment": 11}


def test_verdict_with_a_starved_treatment_arm_is_insufficient_n_with_counts(monkeypatch):
    """N-SCN-obs14 — the live 90/10 round (C 32 vs T1 4) must not end the ramp as a
    terminal control-wins; it is insufficient-n, carries both arms' n and the bar, and
    the operator may advance through it with the explicit override."""
    row = _mk_canary(artifacts=_live_artifacts(rounds=[_round(0, baseline_n=5)]))
    monkeypatch.setattr(canary_svc, "ab_metrics",
                        lambda row: ({"executionStatus": "RUNNING"}, _metrics(32, 4)))
    monkeypatch.setattr(canary_svc, "_sleep", lambda *_a, **_k: None)

    verdict = canary_svc.act_verdict(row.id, lambda message: None)

    assert verdict["verdict"] == "insufficient-n"
    assert verdict["arms_n"] == {"control": 32, "treatment": 4}
    assert verdict["min_arm_n"] == 10
    assert "treatment has 4" in verdict["reason"]
    stored_row = _reload(row.id)
    with pytest.raises(AppError) as exc:
        canary_svc.assert_verdict_allows(stored_row, allow_non_significant=False)
    assert exc.value.code == "canary.override_required"
    canary_svc.assert_verdict_allows(stored_row, allow_non_significant=True)


def test_verdict_reads_eval_results_of_assigned_sessions():
    """End to end on the local substrate: scored records of the canary's assigned
    sessions → GetABTest-shaped statistics → the baseline verdict maths."""
    from app.evaluation.models import EvalResult

    row = _mk_canary(artifacts=_live_artifacts(rounds=[_round(0, baseline_n=0)]))
    db = SessionLocal()
    try:
        for i in range(10):  # N-SCN-obs14: 10 scored sessions per arm clears the bar
            db.add(ExperimentAssignment(owner_kind="canary", owner_id=row.id,
                                        session_id=f"c{i}", arm="C"))
            db.add(ExperimentAssignment(owner_kind="canary", owner_id=row.id,
                                        session_id=f"t{i}", arm="T1"))
            db.add(EvalResult(id=f"c{i}", source="run", run_id="tr1", session_id=f"c{i}",
                              evaluator_id="Builtin.Helpfulness", score=0.4 + i * 0.01))
            db.add(EvalResult(id=f"t{i}", source="run", run_id="tr1", session_id=f"t{i}",
                              evaluator_id="Builtin.Helpfulness", score=0.9 + i * 0.01))
        db.commit()
    finally:
        db.close()
    verdict = canary_svc.act_verdict(row.id, lambda message: None)
    assert verdict["verdict"] == "treatment-wins"
    assert verdict["n"] == 20 and verdict["significant"] is True
    assert verdict["arms_n"] == {"control": 10, "treatment": 10}
    assert verdict["metrics"][0]["label"] == "Builtin.Helpfulness"

def test_advance_updates_only_the_local_ab_weights(monkeypatch):
    row = _mk_canary(artifacts=_live_artifacts(rounds=[
        _round(0, verdict={"verdict": "treatment-wins", "significant": True})]))

    result = canary_svc.act_advance(
        row.id, lambda message: None, allow_non_significant=False
    )

    assert result == {"ramp_stage": 1, "weights": {"C": 50, "T1": 50}}
    stored = _reload(row.id)
    assert stored.stage == "ramp"
    assert routing.weights_of(stored.artifacts["abtest"]["variants"]) == {"C": 50, "T1": 50}
    assert stored.artifacts["abtest"]["execution_status"] == "RUNNING"
    assert stored.artifacts["setup"]["ramp_stage"] == 1

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
            "abtest": _abtest_artifact(ramp_stage=2),
            "rounds": [
                _round(
                    2,
                    verdict={"verdict": "treatment-wins", "significant": True},
                )
            ],
        },
    )
    monkeypatch.setattr(canary_svc, "control_client", MagicMock)
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
    assert stored.artifacts["abtest"]["execution_status"] == "STOPPED"
    # …and the invoke chain stops pinning sessions: DEFAULT (= the candidate) serves
    db = SessionLocal()
    try:
        assert routing.active_owner(db, agent_id) is None
    finally:
        db.close()


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
            "abtest": _abtest_artifact(),
            "rounds": [],
        },
    )
    monkeypatch.setattr(canary_svc, "control_client", MagicMock)
    minted: dict = {}

    def fake_mint(*, agent, edited_spec, control_client, workspace, log, canary_id, role,
                  stage_ctx=None):
        minted["agent_id"] = agent.id
        minted["spec"] = edited_spec
        minted["role"] = role
        return ("1", "9", f"agents/subject/canary/{canary_id}-{role}.zip")

    monkeypatch.setattr(canary_svc.canary_infra, "mint_candidate_version", fake_mint)

    result = canary_svc.act_rollback(row.id, lambda message: None)

    assert result["winner"] == "champion"
    assert result["restored_version"] == "9"
    assert _reload(row.id).artifacts["abtest"]["execution_status"] == "STOPPED"
    assert result["ab_test_status"] == "STOPPED"
    assert "experimental_only" not in result
    # roll-forward re-publishes the agent's CURRENT (unchanged) spec
    assert minted["agent_id"] == agent_id
    assert minted["spec"].system_prompt == "orig"
    assert _reload(row.id).status == "rolled_back"
    # DEFAULT (production truth) now serves the restored version
    assert _reload_agent(agent_id).version == "9"
    # N17-obs2: the roll-forward is a publish — it records its own snapshot row, stamped
    # with the version it made live, holding the spec it re-published (the current one)
    db = SessionLocal()
    try:
        snaps = db.query(SpecSnapshot).filter_by(agent_id=agent_id).all()
    finally:
        db.close()
    assert [(s.note, s.aws_version, s.spec["system_prompt"]) for s in snaps] == [
        ("canary rolled back", "9", "orig")]


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

    monkeypatch.setattr(canary_svc, "control_client", MagicMock)
    minted: dict = {}

    def fake_mint(*, agent, edited_spec, control_client, workspace, log, canary_id, role,
                  stage_ctx=None):
        minted["spec"] = edited_spec
        return ("1", "5", f"agents/subject/canary/{canary_id}-{role}.zip")

    monkeypatch.setattr(canary_svc.canary_infra, "mint_candidate_version", fake_mint)

    result = canary_svc.act_rollback(row.id, lambda message: None)

    # no A/B row in the partial setup → nothing to stop
    assert result["ab_test_status"] is None
    assert "abtest" not in _reload(row.id).artifacts
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


def test_cleanup_stops_the_row_and_deletes_endpoints_configs_and_assignments(monkeypatch):
    row = _mk_canary(artifacts=_live_artifacts())
    db = SessionLocal()
    db.add(ExperimentAssignment(owner_kind="canary", owner_id=row.id, session_id="s1", arm="C"))
    db.add(ExperimentAssignment(owner_kind="canary", owner_id="other", session_id="s2", arm="C"))
    db.commit()
    db.close()
    online = _FakeOnline()
    monkeypatch.setattr(canary_svc, "_online", lambda: online)
    deleted_endpoints: list[str] = []
    monkeypatch.setattr(
        canary_svc.canary_infra,
        "delete_endpoint_quiet",
        lambda control, **kw: deleted_endpoints.append(kw["endpoint_name"]),
    )
    monkeypatch.setattr(canary_svc, "control_client", MagicMock)
    waited: list[dict] = []
    monkeypatch.setattr(
        canary_svc.canary_infra, "wait_endpoints_gone",
        lambda control, **kw: waited.append(kw) or [],
    )

    results = canary_svc.act_cleanup(row.id, lambda message: None)

    by_cat = {r["category"]: r["status"] for r in results}
    assert by_cat["abtest:local_can"] == "deleted"
    # N-SCN-obs3: cleanup waited for the two deleted names before flipping to `cleaned`
    assert len(waited) == 1
    assert set(waited[0]["endpoint_names"]) == {"stablecan", "treatcan"}
    assert waited[0]["runtime_id"] == _live_artifacts()["setup"]["runtime_id"]
    assert waited[0]["timeout_s"] == 180
    assert by_cat["endpoints-gone"] == "deleted"
    assert by_cat["online-eval:oe-c"] == "deleted" and by_cat["online-eval:oe-t"] == "deleted"
    assert online.deleted == [row.id]
    # BOTH named endpoints are deleted (production uses DEFAULT post-canary).
    assert set(deleted_endpoints) == {"stablecan", "treatcan"}
    assert by_cat["assignments"] == "deleted"
    stored = _reload(row.id)
    assert stored.status == "cleaned"
    assert "endpoints_pending" not in stored.artifacts
    assert stored.artifacts["abtest"]["execution_status"] == "STOPPED"
    db = SessionLocal()
    try:
        assert {a.session_id for a in db.query(ExperimentAssignment).all()} == {"s2"}
    finally:
        db.close()

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
    monkeypatch.setattr(canary_svc, "_online", lambda: _FakeOnline())
    monkeypatch.setattr(
        canary_svc.canary_infra, "delete_endpoint_quiet", lambda control, **kw: None)
    monkeypatch.setattr(
        canary_svc.canary_infra, "wait_endpoints_gone", lambda control, **kw: [])
    monkeypatch.setattr(
        canary_svc.canary_infra, "current_version",
        lambda control, runtime_id: live_version)
    monkeypatch.setattr(canary_svc, "control_client", MagicMock)
    deleted: list[str] = []
    monkeypatch.setattr(
        canary_svc.canary_infra, "delete_object_quiet",
        lambda key, _ws, **kw: deleted.append(key))
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


def _traffic_harness(monkeypatch):
    monkeypatch.setattr(canary_svc, "ab_metrics",
                        lambda row: ({"executionStatus": "RUNNING"}, _metrics(2, 3)))
    monkeypatch.setattr(traffic, "plan",
                        lambda ws, agent, items, owner, *, evaluators, **kw: ("job", {}))
    monkeypatch.setattr(canary_svc.eval_executor, "select_executor",
                        lambda settings, *, job_type: f"executor:{job_type}")


def test_traffic_records_the_completed_sessions_when_the_replay_job_fails(monkeypatch):
    """N-SCN-bug3: a FAILED job that still replayed sessions leaves an attempt (flagged with
    the worker's error) — the round's evidence is kept and the error is still surfaced."""
    row = _mk_canary(artifacts=_live_artifacts(rounds=[
        _round(0, verdict={"verdict": "treatment-wins", "significant": True})]))
    _traffic_harness(monkeypatch)
    partial = {"session_ids": ["s1", "s2"], "sent": 2, "failed": 1, "job_id": "tr9",
               "job_state": "FAILED", "status_counts": {"ok": 2, "failed": 1},
               "arms": {"C": 1, "T1": 2}}

    def failing_run(ws, owner, job, assigned, *, executor, progress, **kw):
        raise traffic.ReplayJobFailed("replay job tr9 failed: all 3 scenarios failed", partial)

    monkeypatch.setattr(traffic, "run", failing_run)

    with pytest.raises(traffic.ReplayJobFailed, match="replay job tr9 failed"):
        canary_svc.act_traffic(row.id, ["p1", "p2", "p3"], {"dataset_id": "d1"},
                               lambda message: None)

    stored = _reload(row.id)
    [current] = [r for r in stored.artifacts["rounds"] if r["ramp_stage"] == 0]
    assert len(current["traffic_attempts"]) == 2  # the seeded one + this partial attempt
    attempt = current["traffic_attempts"][-1]
    assert attempt["job_id"] == "tr9" and attempt["sent"] == 2
    assert attempt["session_ids"] == ["s1", "s2"] and attempt["arms"] == {"C": 1, "T1": 2}
    assert attempt["error"].startswith("replay job tr9 failed")
    assert attempt["dataset_id"] == "d1" and attempt["baseline_n"] == 5
    assert "verdict" not in current  # a new attempt always invalidates the prior verdict
    assert stored.stage == "traffic"


def test_traffic_with_nothing_completed_leaves_the_round_untouched(monkeypatch):
    row = _mk_canary(artifacts=_live_artifacts(rounds=[
        _round(0, verdict={"verdict": "treatment-wins", "significant": True})]))
    _traffic_harness(monkeypatch)
    empty = {"session_ids": [], "sent": 0, "failed": 3, "job_id": "tr0", "job_state": "FAILED",
             "status_counts": {"failed": 3}, "arms": {}}
    monkeypatch.setattr(traffic, "run", lambda *a, **k: (_ for _ in ()).throw(
        traffic.ReplayJobFailed("replay job tr0 failed: all 3 scenarios failed", empty)))

    with pytest.raises(traffic.ReplayJobFailed):
        canary_svc.act_traffic(row.id, ["p"], {"dataset_id": "d1"}, lambda message: None)

    current = _reload(row.id).artifacts["rounds"][0]
    assert len(current["traffic_attempts"]) == 1  # only the seeded one — nothing appended
    assert current["verdict"]["verdict"] == "treatment-wins"  # nothing new to judge


# ── N-SCN-obs3: cleanup waits for the named endpoints to disappear ───────────────


class _ListingControl:
    """ListAgentRuntimeEndpoints answering a scripted sequence of name sets (one page)."""

    def __init__(self, pages: list[list[str]], error: Exception | None = None):
        self.pages = list(pages)
        self.error = error
        self.calls = 0

    def list_agent_runtime_endpoints(self, **kwargs):
        self.calls += 1
        if self.error is not None:
            raise self.error
        names = self.pages.pop(0) if len(self.pages) > 1 else self.pages[0]
        return {"runtimeEndpoints": [{"name": n, "status": "DELETING"} for n in names]
                + [{"name": "DEFAULT", "status": "READY"}]}


def test_wait_endpoints_gone_polls_until_the_names_stop_being_listed(monkeypatch):
    slept: list[float] = []
    monkeypatch.setattr(canary_svc.canary_infra, "_sleep", slept.append)
    control = _ListingControl([["stablecan", "treatcan"], ["treatcan"], []])
    lines: list[str] = []
    pending = canary_svc.canary_infra.wait_endpoints_gone(
        control, runtime_id="rt", endpoint_names=["stablecan", "treatcan"],
        timeout_s=180, interval_s=5, log=lines.append,
    )
    assert pending == [] and control.calls == 3 and slept == [5, 5]
    assert lines[0].startswith("endpoint(s) still listed: stablecan, treatcan — poll 1")
    assert lines[1].startswith("endpoint(s) still listed: treatcan — poll 2")


def test_wait_endpoints_gone_reports_what_is_still_listed_at_the_deadline(monkeypatch):
    monkeypatch.setattr(canary_svc.canary_infra, "_sleep", lambda _s: None)
    control = _ListingControl([["treatcan"]])
    pending = canary_svc.canary_infra.wait_endpoints_gone(
        control, runtime_id="rt", endpoint_names=["stablecan", "treatcan"], timeout_s=0,
    )
    assert pending == ["treatcan"] and control.calls == 1   # timeout 0 = one listing


def test_wait_endpoints_gone_never_raises(monkeypatch):
    monkeypatch.setattr(canary_svc.canary_infra, "_sleep", lambda _s: None)

    class ResourceNotFoundException(Exception):
        pass

    gone_runtime = _ListingControl([], error=ResourceNotFoundException("rt gone"))
    assert canary_svc.canary_infra.wait_endpoints_gone(
        gone_runtime, runtime_id="rt", endpoint_names=["stablecan"], timeout_s=30) == []
    lines: list[str] = []
    broken = _ListingControl([], error=RuntimeError("throttled"))
    assert canary_svc.canary_infra.wait_endpoints_gone(
        broken, runtime_id="rt", endpoint_names=["stablecan", "treatcan"], timeout_s=30,
        log=lines.append,
    ) == ["stablecan", "treatcan"]
    assert lines == ["endpoint listing failed (RuntimeError) — stablecan, treatcan left as pending"]


def test_cleanup_records_endpoints_still_listed_at_the_deadline(monkeypatch):
    """The row still flips to `cleaned` (every delete was accepted), but the artifact
    says which names a consumer reading AWS right now will still see."""
    row = _mk_canary(artifacts=_live_artifacts())
    monkeypatch.setattr(canary_svc, "_online", lambda: _FakeOnline())
    monkeypatch.setattr(canary_svc.canary_infra, "delete_endpoint_quiet",
                        lambda control, **kw: None)
    monkeypatch.setattr(canary_svc, "control_client", MagicMock)
    monkeypatch.setattr(canary_svc.canary_infra, "wait_endpoints_gone",
                        lambda control, **kw: ["treatcan"])

    results = canary_svc.act_cleanup(row.id, lambda message: None)

    gone = next(r for r in results if r["category"] == "endpoints-gone")
    assert gone["status"] == "skipped"
    assert gone["detail"] == ("stablecan gone; still listed after 180s: treatcan "
                              "(DELETING is eventually consistent)")
    stored = _reload(row.id)
    assert stored.status == "cleaned"
    assert stored.artifacts["endpoints_pending"] == ["treatcan"]
    assert stored.artifacts["cleanup"] == results


def test_cleanup_does_not_wait_for_endpoints_it_could_not_delete(monkeypatch):
    row = _mk_canary(artifacts=_live_artifacts())
    monkeypatch.setattr(canary_svc, "_online", lambda: _FakeOnline())

    def refuse(control, **kw):
        raise RuntimeError("ValidationException")

    monkeypatch.setattr(canary_svc.canary_infra, "delete_endpoint_quiet", refuse)
    monkeypatch.setattr(canary_svc, "control_client", MagicMock)
    monkeypatch.setattr(canary_svc.canary_infra, "wait_endpoints_gone",
                        lambda control, **kw: pytest.fail("nothing was deleted"))
    results = canary_svc.act_cleanup(row.id, lambda message: None)
    assert "endpoints-gone" not in {r["category"] for r in results}
    assert _reload(row.id).status == "cleaned"


# ── N16-obs111a: presets are never canary arms; terminal canaries never block a new one ──


def test_a_system_preset_is_not_an_eligible_canary_agent(client):
    (agent_id,) = _persist_agents(_agent("preset", method="harness"))
    db = SessionLocal()
    try:
        db.get(Agent, agent_id).system_key = "hr-helpdesk"
        db.commit()
    finally:
        db.close()
    res = client.post(
        "/api/runtime-canaries",
        json={"agent_id": agent_id, "candidate": {"system_prompt": "new"}},
    )
    assert res.status_code == 400, res.text
    assert res.json()["code"] == "canary.agent_unsupported"
    assert res.json()["detail"]["canary_capability"]["reason_code"] == "system-managed"


@pytest.mark.parametrize("status", ["completed", "rolled_back", "cleaned"])
def test_a_settled_canary_does_not_block_a_new_one(client, monkeypatch, status):
    """`already_running` keys on CANARY_BLOCKING_STATUSES (= running), not a literal."""
    from app.optimization.models import CANARY_BLOCKING_STATUSES

    assert status not in CANARY_BLOCKING_STATUSES
    (agent_id,) = _persist_agents(_agent("subject"))
    _mk_canary(agent_id=agent_id, status=status)
    control = MagicMock()
    control.get_agent_runtime.side_effect = lambda agentRuntimeId: {
        "agentRuntimeName": f"runtime-{agentRuntimeId}"
    }
    monkeypatch.setattr(canary_svc, "control_client", lambda _ws=None: control)
    res = client.post(
        "/api/runtime-canaries",
        json={"agent_id": agent_id, "candidate": {"system_prompt": "new"}},
    )
    assert res.status_code == 201, res.text


# ── N17-obs386: a canary edit must reach the challenger's rendered code ──────────────

_UPLOADED_MAIN = (
    'DEFAULT_SYSTEM_PROMPT = "You are expense-helper."\n\n'
    "def resolve_system_prompt() -> str:\n    return DEFAULT_SYSTEM_PROMPT\n"
)


def test_a_prompt_edit_on_an_uploaded_code_bundle_is_refused_before_minting(client,
                                                                           monkeypatch):
    # eval-wb 4.2: a zip_runtime agent running its own code_bundle (not a harness export)
    # renders main.py verbatim — the edit was dropped and two identical versions compared
    spec = {"protocol": "http", "system_prompt": "orig",
            "code_bundle": {"main.py": _UPLOADED_MAIN}}
    (agent_id,) = _persist_agents(_agent("bundled", spec=spec))
    res = client.post("/api/runtime-canaries",
                      json={"agent_id": agent_id, "candidate": {"system_prompt": "refuse"}})
    assert res.status_code == 409
    assert res.json()["code"] == "canary.edit_not_applicable"
    db = SessionLocal()
    try:
        assert db.query(RuntimeCanary).count() == 0
    finally:
        db.close()


def test_a_candidate_that_renders_identically_is_refused(client, monkeypatch):
    # the same prompt with different whitespace: the challenger would be byte-identical
    (agent_id,) = _persist_agents(_agent("same"))
    res = client.post("/api/runtime-canaries",
                      json={"agent_id": agent_id, "candidate": {"system_prompt": "  orig  "}})
    assert res.status_code == 409
    assert res.json()["code"] == "canary.candidate_identical"
    db = SessionLocal()
    try:
        assert db.query(RuntimeCanary).count() == 0
    finally:
        db.close()


def test_a_real_edit_renders_a_different_challenger():
    from app.deployer.zip_runtime import _generate_code
    from app.optimization.canary_routers import CandidateEdit, _resolve_edited_spec

    agent = _agent("subject")
    edited = _resolve_edited_spec(agent, CandidateEdit(system_prompt="a brand new prompt"))
    champion = AgentSpec(**{**agent.spec, "name": agent.name, "method": agent.method})
    assert _generate_code(edited)[0] != _generate_code(champion)[0]
    assert "a brand new prompt" in _generate_code(edited)[0]
