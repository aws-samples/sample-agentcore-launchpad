"""N12-obs46 (upstream b5eb3dc8 parity) — a canary chooses the evaluators both arms are
scored with and a primary evaluator that alone decides the verdict.

CN twist: the arms are scored by the local per-arm online configs AND by the replay job
(``traffic.plan``), so the choice has to reach both; the verdict keeps the N-SCN-obs14
per-arm bar (``Settings.verdict_min_arm_n``), applied to the PRIMARY's own arm counts when
a primary is set. The managed-Harness canary half of the upstream change has no CN twin
(CA-1a)."""

from unittest.mock import MagicMock

import pytest

import app.optimization.canary_service as canary_svc
from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.core.errors import AppError
from app.evaluation.models import EvalEvaluator
from app.evaluation.online_evaluators import ONLINE_EVAL_DEFAULT, normalize_online_evaluators
from app.optimization import traffic
from app.optimization.verdict import compute_verdict, metric_evaluator_id
from app.schemas.agent import AgentSpec
from tests.optimization.test_runtime_canaries import (
    _agent,
    _FakeOnline,
    _live_artifacts,
    _mk_canary,
    _persist_agents,
    _reload,
    _round,
)

GSR, HELP, REFUSAL = "Builtin.GoalSuccessRate", "Builtin.Helpfulness", "Builtin.Refusal"


def _judge(evaluator_id: str, *, scale: list[dict] | None = None, instructions: str | None = None):
    db = SessionLocal()
    try:
        db.add(EvalEvaluator(
            id=evaluator_id, workspace_id=DEFAULT_WORKSPACE_ID, name=evaluator_id,
            definition="judge", instructions=instructions or "Rate {assistant_turn}.",
            rating_scale=scale if scale is not None else [
                {"value": 1.0, "label": "pass", "definition": "ok"},
                {"value": 0.0, "label": "fail", "definition": "bad"},
            ],
        ))
        db.commit()
    finally:
        db.close()


@pytest.fixture
def control(monkeypatch):
    client = MagicMock()
    client.get_agent_runtime.side_effect = lambda agentRuntimeId: {
        "agentRuntimeName": f"runtime-{agentRuntimeId}"
    }
    monkeypatch.setattr(canary_svc, "control_client", lambda _ws=None: client)
    return client


def _create(client, **extra):
    (agent_id,) = _persist_agents(_agent(f"subject-{len(extra)}-{id(extra) % 9973}"))
    return client.post("/api/runtime-canaries", json={
        "agent_id": agent_id, "candidate": {"system_prompt": "new prompt"}, **extra,
    })


# ─── create: validation + storage ─────────────────────────────────────────────
def test_runtime_create_stores_the_chosen_evaluators_and_primary(client, control):
    _judge("store_rules")
    res = _create(client, online_evaluators=[HELP, "store_rules", HELP],
                  primary_evaluator="store_rules")
    assert res.status_code == 201, res.text
    artifacts = res.json()["artifacts"]
    assert artifacts["online_evaluators"] == [HELP, "store_rules"]  # deduped, order kept
    assert artifacts["primary_evaluator"] == "store_rules"


def test_omitting_the_choice_stores_the_default_pair_and_no_primary(client, control):
    res = _create(client)
    assert res.status_code == 201, res.text
    artifacts = res.json()["artifacts"]
    assert artifacts["online_evaluators"] == list(ONLINE_EVAL_DEFAULT)
    assert "primary_evaluator" not in artifacts
    empty = _create(client, online_evaluators=[], primary_evaluator="")
    assert empty.status_code == 201
    assert empty.json()["artifacts"]["online_evaluators"] == list(ONLINE_EVAL_DEFAULT)
    assert "primary_evaluator" not in empty.json()["artifacts"]


@pytest.mark.parametrize(
    "evaluators, code",
    [
        (["Builtin.TrajectoryInOrderMatch"], "canary.evaluator_unsupported"),
        (["Builtin.NoSuchJudge"], "canary.evaluator_unsupported"),
        ([f"Builtin.Helpfulness{i}" if i else HELP for i in range(11)],
         "canary.evaluator_unsupported"),
        (["ghost_judge"], "canary.evaluator_unsupported"),
        (["gt_judge"], "canary.evaluator_unsupported"),
        (["categorical_judge"], "canary.evaluator_categorical"),
    ],
)
def test_unusable_evaluators_are_refused_at_create(client, control, evaluators, code):
    _judge("gt_judge", instructions="Compare {assistant_turn} with {expected_response}.")
    _judge("categorical_judge", scale=[{"label": "good", "definition": "g"},
                                       {"label": "bad", "definition": "b"}])
    res = _create(client, online_evaluators=evaluators)
    assert res.status_code == 400, res.text
    assert res.json()["code"] == code
    db = SessionLocal()
    try:
        from app.optimization.models import RuntimeCanary

        assert db.query(RuntimeCanary).count() == 0  # refused before any row was written
    finally:
        db.close()


def test_a_primary_outside_the_selection_is_refused(client, control):
    res = _create(client, online_evaluators=[HELP], primary_evaluator=GSR)
    assert res.status_code == 400
    body = res.json()
    assert body["code"] == "canary.primary_not_selected"
    assert body["detail"]["online_evaluators"] == [HELP]


def test_a_default_pair_member_may_be_primary_without_a_list(client, control):
    res = _create(client, primary_evaluator=HELP)
    assert res.status_code == 201, res.text
    assert res.json()["artifacts"]["online_evaluators"] == list(ONLINE_EVAL_DEFAULT)
    assert res.json()["artifacts"]["primary_evaluator"] == HELP


def test_require_numeric_refuses_only_categorical_custom_judges():
    _judge("numeric_judge")
    _judge("categorical_judge", scale=[{"label": "good", "definition": "g"}])
    db = SessionLocal()
    try:
        assert normalize_online_evaluators(["numeric_judge", REFUSAL], db, code_prefix="canary",
                                           require_numeric=True) == ["numeric_judge", REFUSAL]
        # without the flag the online-eval rules alone apply (a categorical judge is fine)
        assert normalize_online_evaluators(["categorical_judge"], db) == ["categorical_judge"]
        with pytest.raises(AppError) as exc:
            normalize_online_evaluators(["categorical_judge"], db, code_prefix="canary",
                                        require_numeric=True)
        assert exc.value.code == "canary.evaluator_categorical"
        assert exc.value.status_code == 400
    finally:
        db.close()


# ─── setup / traffic: the choice reaches both arms and the replay job ─────────
def _setup_stubs(monkeypatch):
    monkeypatch.setattr(
        canary_svc.canary_infra, "mint_candidate_version",
        lambda **kw: ("1", "2", f"agents/x/canary/{kw['canary_id']}-{kw['role']}.zip"))
    monkeypatch.setattr(canary_svc.canary_infra, "current_version", lambda control, runtime_id: "1")
    monkeypatch.setattr(
        canary_svc.canary_infra, "ensure_endpoint_ready",
        lambda control, *, runtime_id, endpoint_name, version, log=None: {"status": "READY"})
    online = _FakeOnline()
    monkeypatch.setattr(canary_svc, "_online", lambda: online)
    monkeypatch.setattr(canary_svc, "control_client", MagicMock)
    return online


def _spec_artifacts(agent_id: str, **extra) -> dict:
    return {
        "agent_meta": {"id": agent_id, "name": "subject", "arn": "arn:agent",
                       "resource_id": "subject-res", "runtime_name": "SubjectRuntime"},
        "edited_spec": AgentSpec(name="subject", method="zip_runtime",
                                 system_prompt="edited").model_dump(),
        "rounds": [],
        **extra,
    }


def test_setup_scores_both_arms_with_the_chosen_evaluators(monkeypatch):
    (agent_id,) = _persist_agents(_agent("subject"))
    row = _mk_canary(agent_id=agent_id, artifacts=_spec_artifacts(
        agent_id, online_evaluators=[HELP, "store_rules"], primary_evaluator="store_rules"))
    online = _setup_stubs(monkeypatch)

    result = canary_svc.act_setup(row.id, lambda message: None)

    assert [c["evaluators"] for c in online.created] == [[HELP, "store_rules"]] * 2
    assert result["online_evaluators"] == [HELP, "store_rules"]
    assert result["primary_evaluator"] == "store_rules"
    stored = _reload(row.id).artifacts["setup"]
    assert stored["online_evaluators"] == [HELP, "store_rules"]
    assert stored["primary_evaluator"] == "store_rules"


def test_a_legacy_row_keeps_the_default_pair_and_no_primary(monkeypatch):
    (agent_id,) = _persist_agents(_agent("subject"))
    row = _mk_canary(agent_id=agent_id, artifacts=_spec_artifacts(agent_id))
    online = _setup_stubs(monkeypatch)

    result = canary_svc.act_setup(row.id, lambda message: None)

    assert [c["evaluators"] for c in online.created] == [list(ONLINE_EVAL_DEFAULT)] * 2
    assert result["online_evaluators"] == list(ONLINE_EVAL_DEFAULT)
    assert "primary_evaluator" not in result
    assert canary_svc.configured_evaluators(_reload(row.id)) == list(ONLINE_EVAL_DEFAULT)
    assert canary_svc.primary_evaluator(_reload(row.id)) is None


def _two_metrics(help_c: int, help_t: int, rules_c: int, rules_t: int, *,
                 help_means=(0.5, 0.8), rules_means=(0.9, 0.4), rules_sig=False) -> list[dict]:
    return [
        {"evaluatorId": HELP, "label": HELP, "polarity": 1,
         "control": {"name": "C", "mean": help_means[0], "sampleSize": help_c},
         "variants": [{"name": "T1", "mean": help_means[1], "sampleSize": help_t,
                       "isSignificant": True}]},
        {"evaluatorId": "store_rules", "label": "store_rules", "polarity": 1,
         "control": {"name": "C", "mean": rules_means[0], "sampleSize": rules_c},
         "variants": [{"name": "T1", "mean": rules_means[1], "sampleSize": rules_t,
                       "isSignificant": rules_sig}]},
    ]


def _traffic_stubs(monkeypatch, metrics):
    monkeypatch.setattr(canary_svc, "ab_metrics",
                        lambda row: ({"executionStatus": "RUNNING"}, metrics))
    planned: dict = {}

    def fake_plan(ws, agent, items, owner, *, evaluators, **kw):
        planned.update(evaluators=list(evaluators))
        return "job", {"sid": {"arm": "C"}}

    monkeypatch.setattr(traffic, "plan", fake_plan)
    monkeypatch.setattr(
        traffic, "run",
        lambda ws, owner, job, assigned, *, executor, progress, **kw: {
            "sent": 1, "failed": 0, "job_id": "j", "job_state": "COMPLETED"})
    monkeypatch.setattr(canary_svc.eval_executor, "select_executor", lambda *a, **k: object())
    monkeypatch.setattr(canary_svc.eval_executor, "assert_executor_ready", lambda *a, **k: None)
    return planned


def test_traffic_replays_with_the_chosen_evaluators_and_records_the_primary_baseline(monkeypatch):
    row = _mk_canary(artifacts=_live_artifacts(
        online_evaluators=[HELP, "store_rules"], primary_evaluator="store_rules"))
    planned = _traffic_stubs(monkeypatch, _two_metrics(12, 11, 3, 2))

    attempt = canary_svc.act_traffic(row.id, ["hello"], {"dataset_id": "d", "dataset_name": "n"},
                                     lambda message: None)

    assert planned["evaluators"] == [HELP, "store_rules"]
    assert attempt["baseline_n"] == 28  # every evaluator's samples (the aggregate marker)
    assert attempt["primary_baseline_n"] == 5  # the primary's own samples


def test_traffic_without_a_primary_records_no_primary_baseline(monkeypatch):
    row = _mk_canary(artifacts=_live_artifacts(online_evaluators=[HELP, "store_rules"]))
    planned = _traffic_stubs(monkeypatch, _two_metrics(12, 11, 3, 2))

    attempt = canary_svc.act_traffic(row.id, ["hello"], {"dataset_id": "d", "dataset_name": "n"},
                                     lambda message: None)

    assert planned["evaluators"] == [HELP, "store_rules"]
    assert "primary_baseline_n" not in attempt


def test_legacy_traffic_replays_with_the_default_pair(monkeypatch):
    row = _mk_canary(artifacts=_live_artifacts())
    planned = _traffic_stubs(monkeypatch, [])
    canary_svc.act_traffic(row.id, ["hello"], {"dataset_id": "d", "dataset_name": "n"},
                           lambda message: None)
    assert planned["evaluators"] == list(ONLINE_EVAL_DEFAULT)


# ─── verdict: the primary decides, under the per-arm bar ──────────────────────
def _primary_round(**kw):
    attempt = {"baseline_n": kw.pop("baseline_n", 0), "sent": 12, "failed": 0}
    if "primary_baseline_n" in kw:
        attempt["primary_baseline_n"] = kw.pop("primary_baseline_n")
    return {"ramp_stage": 0, "weights": {}, "traffic_attempts": [attempt]}


def test_the_primary_decides_even_against_the_aggregate(monkeypatch):
    """Helpfulness says the candidate wins big (12 vs 11 records); the primary store_rules
    judge says it loses — the primary decides, the aggregate is display only."""
    row = _mk_canary(artifacts=_live_artifacts(
        online_evaluators=[HELP, "store_rules"], primary_evaluator="store_rules",
        rounds=[_primary_round(baseline_n=0, primary_baseline_n=0)]))
    metrics = _two_metrics(12, 11, 12, 11, rules_sig=True)
    monkeypatch.setattr(canary_svc, "ab_metrics",
                        lambda row: ({"executionStatus": "RUNNING"}, metrics))
    monkeypatch.setattr(canary_svc, "_sleep", lambda *_a, **_k: None)

    verdict = canary_svc.act_verdict(row.id, lambda message: None)

    assert verdict["verdict"] == "control-wins"
    assert verdict["primary"] == "store_rules"
    assert verdict["primary_delta"] == -0.5
    assert verdict["significant"] is True  # the primary's own test, not Helpfulness's
    assert verdict["n"] == 23 and verdict["arms_n"] == {"control": 12, "treatment": 11}
    assert verdict["avg_delta"] == -0.1  # the all-evaluator aggregate, kept for display only
    assert verdict["evaluators"] == [HELP, "store_rules"] and verdict["unscored"] == []
    stored = _reload(row.id).artifacts["rounds"][0]["verdict"]
    assert stored["primary"] == "store_rules" and stored["verdict"] == "control-wins"


def test_without_a_primary_the_aggregate_still_decides(monkeypatch):
    row = _mk_canary(artifacts=_live_artifacts(
        online_evaluators=[HELP, "store_rules"], rounds=[_primary_round(baseline_n=0)]))
    monkeypatch.setattr(canary_svc, "ab_metrics",
                        lambda row: ({"executionStatus": "RUNNING"}, _two_metrics(12, 11, 12, 11)))
    monkeypatch.setattr(canary_svc, "_sleep", lambda *_a, **_k: None)

    verdict = canary_svc.act_verdict(row.id, lambda message: None)

    # +0.3 (Helpfulness) and -0.5 (store_rules) at equal weight → control-wins on the mean
    assert verdict["verdict"] == "control-wins" and "primary" not in verdict
    assert verdict["evaluators"] == [HELP, "store_rules"] and verdict["unscored"] == []


def test_the_primarys_arm_bar_not_the_aggregates_gates_a_directional_verdict(monkeypatch):
    """Helpfulness has 12/11 records (clears the bar); the primary has 12/4 — the verdict
    is insufficient-n on the PRIMARY's counts, and names it."""
    row = _mk_canary(artifacts=_live_artifacts(
        online_evaluators=[HELP, "store_rules"], primary_evaluator="store_rules",
        rounds=[_primary_round(baseline_n=0, primary_baseline_n=0)]))
    monkeypatch.setattr(canary_svc, "ab_metrics",
                        lambda row: ({"executionStatus": "RUNNING"}, _two_metrics(12, 11, 12, 4)))
    monkeypatch.setattr(canary_svc, "_sleep", lambda *_a, **_k: None)

    verdict = canary_svc.act_verdict(row.id, lambda message: None)

    assert verdict["verdict"] == "insufficient-n"
    assert verdict["arms_n"] == {"control": 12, "treatment": 4} and verdict["min_arm_n"] == 10
    assert "store_rules" in verdict["reason"] and "treatment has 4" in verdict["reason"]
    stored_row = _reload(row.id)
    with pytest.raises(AppError) as exc:
        canary_svc.assert_verdict_allows(stored_row, allow_non_significant=False)
    assert exc.value.code == "canary.override_required"
    canary_svc.assert_verdict_allows(stored_row, allow_non_significant=True)
    with pytest.raises(AppError) as blocked:
        canary_svc.assert_verdict_allows(stored_row, allow_non_significant=True, terminal=True)
    # the complete 409 names both arms of the PRIMARY (the counts the bar was applied to)
    assert "control has 12" in blocked.value.message and "treatment has 4" in blocked.value.message


def test_an_unscored_primary_is_insufficient_data_and_reported(monkeypatch):
    """Only Helpfulness scored; the primary has no metric at all → insufficient-data, the
    other evaluator never stands in, and `unscored` names the primary."""
    row = _mk_canary(artifacts=_live_artifacts(
        online_evaluators=[HELP, "store_rules"], primary_evaluator="store_rules",
        rounds=[_primary_round(baseline_n=0)]))
    only_help = _two_metrics(12, 11, 0, 0)[:1]
    monkeypatch.setattr(canary_svc, "ab_metrics",
                        lambda row: ({"executionStatus": "RUNNING"}, only_help))
    monkeypatch.setattr(canary_svc, "_sleep", lambda *_a, **_k: None)

    verdict = canary_svc.act_verdict(row.id, lambda message: None)

    assert verdict["verdict"] == "insufficient-data"
    assert verdict["primary"] == "store_rules" and verdict["n"] == 0
    assert "store_rules" in verdict["reason"]
    assert verdict["unscored"] == ["store_rules"]
    with pytest.raises(AppError) as exc:
        canary_svc.assert_verdict_allows(_reload(row.id), allow_non_significant=True)
    assert exc.value.code == "canary.verdict_blocked"


def test_the_verdict_waits_for_the_primary_not_just_any_evaluator(monkeypatch):
    """Helpfulness already scored this stage's traffic; the primary has not — the first read
    is not enough, the second (primary grew past its marker) is."""
    row = _mk_canary(artifacts=_live_artifacts(
        online_evaluators=[HELP, "store_rules"], primary_evaluator="store_rules",
        rounds=[_primary_round(baseline_n=20, primary_baseline_n=4)]))
    reads = iter([_two_metrics(12, 11, 2, 2), _two_metrics(12, 11, 12, 11)])
    sleeps: list[int] = []
    monkeypatch.setattr(canary_svc, "ab_metrics",
                        lambda row: ({"executionStatus": "RUNNING"}, next(reads)))
    monkeypatch.setattr(canary_svc, "_sleep", lambda s, *_a, **_k: sleeps.append(s))

    verdict = canary_svc.act_verdict(row.id, lambda message: None)

    assert len(sleeps) == 1
    assert verdict["verdict"] == "control-wins" and verdict["primary"] == "store_rules"
    assert verdict["n"] == 23


def test_a_stale_primary_at_the_deadline_is_insufficient_data(monkeypatch):
    row = _mk_canary(artifacts=_live_artifacts(
        online_evaluators=[HELP, "store_rules"], primary_evaluator="store_rules",
        rounds=[_primary_round(baseline_n=20, primary_baseline_n=23)]))
    monkeypatch.setattr(canary_svc, "ab_metrics",
                        lambda row: ({"executionStatus": "RUNNING"}, _two_metrics(12, 11, 12, 11)))
    monkeypatch.setattr(canary_svc, "_sleep", lambda *_a, **_k: None)
    monkeypatch.setattr(canary_svc, "VERDICT_WAIT_S", 0)

    verdict = canary_svc.act_verdict(row.id, lambda message: None)

    assert verdict["verdict"] == "insufficient-data"
    assert verdict["reason"].startswith("no new primary evaluator samples")
    assert verdict["reason_code"] == "no_new_primary_samples"  # N17-obs443
    assert verdict["reason_params"]["primary"] == verdict["primary"]
    assert verdict["primary"] == "store_rules" and verdict["n"] == 23


def test_an_attempt_without_the_primary_marker_falls_back_to_the_aggregate_wait(monkeypatch):
    """A traffic attempt recorded before N12-obs46 carries no primary_baseline_n: the wait
    is the aggregate one, the verdict still comes from the primary."""
    row = _mk_canary(artifacts=_live_artifacts(
        online_evaluators=[HELP, "store_rules"], primary_evaluator="store_rules",
        rounds=[_primary_round(baseline_n=5)]))
    monkeypatch.setattr(canary_svc, "ab_metrics",
                        lambda row: ({"executionStatus": "RUNNING"}, _two_metrics(12, 11, 12, 11)))
    monkeypatch.setattr(canary_svc, "_sleep", lambda *_a, **_k: None)

    verdict = canary_svc.act_verdict(row.id, lambda message: None)

    assert verdict["verdict"] == "control-wins" and verdict["primary"] == "store_rules"


def test_a_legacy_row_verdict_carries_no_scoring_keys(monkeypatch):
    row = _mk_canary(artifacts=_live_artifacts(rounds=[_round(0, baseline_n=0)]))
    only_help = _two_metrics(12, 11, 12, 11)[:1]
    monkeypatch.setattr(canary_svc, "ab_metrics",
                        lambda row: ({"executionStatus": "RUNNING"}, only_help))
    monkeypatch.setattr(canary_svc, "_sleep", lambda *_a, **_k: None)

    verdict = canary_svc.act_verdict(row.id, lambda message: None)

    assert verdict["verdict"] == "treatment-wins"
    assert "evaluators" not in verdict and "unscored" not in verdict and "primary" not in verdict


# ─── compute_verdict with a primary ───────────────────────────────────────────
def _metric(evaluator, c_mean, t_mean, c_n=12, t_n=12, significant=False):
    return {
        "evaluatorId": evaluator, "label": evaluator.rsplit("/", 1)[-1],
        "control": {"mean": c_mean, "sampleSize": c_n},
        "variants": [{"mean": t_mean, "sampleSize": t_n, "isSignificant": significant}],
    }


def test_compute_verdict_without_a_primary_is_unchanged():
    metrics = [_metric(HELP, 0.4, 0.6), _metric("store_rules", 0.9, 0.4)]
    verdict = compute_verdict(metrics)
    assert verdict["verdict"] == "control-wins" and "primary" not in verdict
    assert verdict["arms_n"] == {"control": 12, "treatment": 12}


def test_compute_verdict_orients_a_penalty_primary():
    metrics = [_metric(HELP, 0.6, 0.4), _metric(REFUSAL, 0.3, 0.0)]
    verdict = compute_verdict(metrics, primary=REFUSAL)
    assert verdict["verdict"] == "treatment-wins"  # fewer refusals, whatever Helpfulness says
    assert verdict["primary_delta"] == 0.3 and verdict["primary"] == REFUSAL


def test_compute_verdict_uses_the_primary_sample_size_and_significance():
    metrics = [_metric(HELP, 0.4, 0.6, 40, 40, significant=True),
               _metric("store_rules", 0.5, 0.7, 12, 11, significant=False)]
    verdict = compute_verdict(metrics, primary="store_rules")
    assert verdict["verdict"] == "treatment-wins"
    assert verdict["n"] == 23 and verdict["arms_n"] == {"control": 12, "treatment": 11}
    assert verdict["significant"] is False
    assert verdict["min_arm_n"] == 10 and verdict["avg_delta"] is not None


def test_compute_verdict_primary_below_the_bar_is_insufficient_n():
    metrics = [_metric(HELP, 0.4, 0.6, 40, 40), _metric("store_rules", 0.5, 0.7, 12, 4)]
    verdict = compute_verdict(metrics, primary="store_rules")
    assert verdict["verdict"] == "insufficient-n"
    assert verdict["arms_n"] == {"control": 12, "treatment": 4}
    assert "store_rules" in verdict["reason"]
    # an explicit, lower bar is honoured — the bar is the caller's, as for the aggregate
    lowered = compute_verdict(metrics, min_arm_n=4, primary="store_rules")
    assert lowered["verdict"] == "treatment-wins"


def test_compute_verdict_tie_and_missing_primary():
    tie = compute_verdict([_metric("store_rules", 0.5, 0.5)], primary="store_rules")
    assert tie["verdict"] == "tie" and tie["primary_delta"] == 0.0
    missing = compute_verdict([_metric(HELP, 0.4, 0.6)], primary="store_rules")
    assert missing["verdict"] == "insufficient-data" and missing["n"] == 0
    assert missing["primary"] == "store_rules"
    assert compute_verdict([], primary="store_rules")["verdict"] == "insufficient-data"
    unscored = compute_verdict([_metric("store_rules", None, 0.6)], primary="store_rules")
    assert unscored["verdict"] == "insufficient-data"


def test_compute_verdict_keys_the_primary_by_the_evaluator_arn_tail():
    arn = "arn:aws-cn:bedrock-agentcore:cn-northwest-1:111122223333:evaluator/store_rules"
    verdict = compute_verdict([_metric(arn, 0.5, 0.7)], primary="store_rules")
    assert verdict["verdict"] == "treatment-wins"
    assert metric_evaluator_id({"evaluatorId": arn}) == "store_rules"
    assert metric_evaluator_id({"label": HELP}) == HELP


def test_a_trajectory_only_session_judge_may_be_the_canary_primary(client, control):
    """N17-obs311: ``{actual_tool_trajectory}`` / ``{context}`` / ``{available_tools}`` are
    rendered from the arm's own trace, so a judge reading only them needs no ground truth —
    it is accepted as the primary evaluator, and as a recommendation's target."""
    from app.evaluation.recommendations import _evaluator_problem

    _judge("traj_judge", instructions=(
        "Pass only when {actual_tool_trajectory} calls get_pto_balance before the answer; "
        "conversation: {context}; tools: {available_tools}."))
    res = _create(client, online_evaluators=[HELP, "traj_judge"], primary_evaluator="traj_judge")
    assert res.status_code == 201, res.text
    assert res.json()["artifacts"]["primary_evaluator"] == "traj_judge"
    db = SessionLocal()
    try:
        assert _evaluator_problem("traj_judge", db.get(EvalEvaluator, "traj_judge")) is None
    finally:
        db.close()
