"""Canary evaluator selection: the operator picks the evaluators both arms are scored
by (built-ins and numeric custom judges that need no ground truth) and optionally a
primary evaluator that alone decides the verdict."""

import itertools
from unittest.mock import MagicMock

import pytest

import app.optimization.canary_routers as canary_routers
import app.optimization.canary_service as canary_svc
import app.optimization.service as exp_svc
from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.evaluation.online_evaluators import ONLINE_EVAL_DEFAULT
from app.models.ledger import Agent
from app.optimization import canary_harness
from app.optimization.models import RuntimeCanary
from app.schemas.agent import AgentSpec
from tests.optimization.test_harness_canaries import (
    _agent as _harness_agent,
)
from tests.optimization.test_harness_canaries import (
    _canary as _harness_canary,
)
from tests.optimization.test_harness_canaries import (
    _setup_artifact as _harness_setup,
)
from tests.optimization.test_harness_canaries import (
    _versions_control,
)

GSR = "Builtin.GoalSuccessRate"
HELP = "Builtin.Helpfulness"
JUDGE = "StoreBusinessRules-abc123"
JUDGE_ARN = f"arn:aws:bedrock-agentcore:us-west-2:111122223333:evaluator/{JUDGE}"
RUNTIME_ARN = "arn:aws:bedrock-agentcore:us-west-2:111122223333:runtime/r-1"


class ResourceNotFoundException(Exception):
    pass


def _judge(instructions="Rate {assistant_turn} against the store rules in {context}.",
           scale=None):
    scale = scale or {"numerical": [
        {"value": 1.0, "label": "pass", "definition": "follows the rules"},
        {"value": 0.0, "label": "fail", "definition": "breaks a rule"},
    ]}
    return {"evaluatorConfig": {"llmAsAJudge": {"instructions": instructions,
                                                "ratingScale": scale}}}


_JUDGES = {
    JUDGE: _judge(),
    "GtJudge-1": _judge("Compare {assistant_turn} with {expected_response}."),
    "CatJudge-1": _judge(scale={"categorical": [
        {"label": "good", "definition": "fine"}, {"label": "bad", "definition": "not"}]}),
}


def _with_judges(control: MagicMock) -> MagicMock:
    def get_evaluator(evaluatorId):  # noqa: N803 — boto3 shape
        if evaluatorId not in _JUDGES:
            raise ResourceNotFoundException(evaluatorId)
        return _JUDGES[evaluatorId]

    control.get_evaluator.side_effect = get_evaluator
    return control


def _runtime_agent() -> str:
    db = SessionLocal()
    try:
        agent = Agent(
            workspace_id=DEFAULT_WORKSPACE_ID, name="subject-r", method="zip_runtime",
            status="active", arn=RUNTIME_ARN, resource_id="r-1",
            spec={"name": "subject-r", "method": "zip_runtime", "system_prompt": "orig"},
        )
        db.add(agent)
        db.commit()
        return agent.id
    finally:
        db.close()


@pytest.fixture
def control(monkeypatch):
    """One stub for both kinds: Harness versions, a runtime name, and the judges."""
    stub = _with_judges(_versions_control("1", "2", "3"))
    stub.get_agent_runtime.return_value = {"agentRuntimeName": "runtime-r"}
    monkeypatch.setattr(canary_routers, "control_client", lambda _ws=None: stub)
    monkeypatch.setattr(canary_svc, "control_client", lambda _ws=None: stub)
    return stub


def _harness_body(agent_id: str, **extra) -> dict:
    return {"agent_id": agent_id, "harness_versions": {"control": "1", "treatment": "3"},
            **extra}


def _runtime_body(agent_id: str, **extra) -> dict:
    return {"agent_id": agent_id, "candidate": {"system_prompt": "edited"}, **extra}


# ─── create ─────────────────────────────────────────────────────────────────
def test_harness_create_stores_the_chosen_evaluators_and_primary(client, control):
    res = client.post("/api/runtime-canaries", json=_harness_body(
        _harness_agent(), online_evaluators=[JUDGE, GSR, JUDGE], primary_evaluator=JUDGE,
    ))

    assert res.status_code == 201, res.text
    artifacts = res.json()["artifacts"]
    assert artifacts["online_evaluators"] == [JUDGE, GSR]  # deduped, order kept
    assert artifacts["primary_evaluator"] == JUDGE
    assert [c.kwargs["evaluatorId"] for c in control.get_evaluator.call_args_list] == [
        JUDGE]


def test_runtime_create_stores_the_chosen_evaluators_and_primary(client, control):
    res = client.post("/api/runtime-canaries", json=_runtime_body(
        _runtime_agent(), online_evaluators=[JUDGE, GSR], primary_evaluator=GSR,
    ))

    assert res.status_code == 201, res.text
    artifacts = res.json()["artifacts"]
    assert artifacts["online_evaluators"] == [JUDGE, GSR]
    assert artifacts["primary_evaluator"] == GSR


def test_omitting_the_choice_keeps_the_default_pair_and_no_primary(client, control):
    res = client.post("/api/runtime-canaries", json=_harness_body(_harness_agent()))

    assert res.status_code == 201, res.text
    artifacts = res.json()["artifacts"]
    assert artifacts["online_evaluators"] == list(ONLINE_EVAL_DEFAULT)
    assert "primary_evaluator" not in artifacts
    control.get_evaluator.assert_not_called()


@pytest.mark.parametrize(("evaluators", "code"), [
    (["GtJudge-1", GSR], "canary.evaluator_unsupported"),
    (["Builtin.TrajectoryExactOrderMatch"], "canary.evaluator_unsupported"),
    (["Builtin.NoSuchThing"], "canary.evaluator_unsupported"),
    (["Missing-judge"], "canary.evaluator_unsupported"),
    (["CatJudge-1", GSR], "canary.evaluator_categorical"),
    ([f"Custom-{i}" for i in range(11)], "canary.evaluator_unsupported"),
])
@pytest.mark.parametrize("kind", ["harness", "runtime"])
def test_unusable_evaluators_are_refused_at_create(client, control, kind, evaluators, code):
    body = (_harness_body(_harness_agent(), online_evaluators=evaluators) if kind == "harness"
            else _runtime_body(_runtime_agent(), online_evaluators=evaluators))

    res = client.post("/api/runtime-canaries", json=body)

    assert res.status_code == 400, res.text
    assert res.json()["code"] == code
    assert not SessionLocal().query(RuntimeCanary).count()


@pytest.mark.parametrize("kind", ["harness", "runtime"])
def test_a_primary_outside_the_selection_is_refused(
    client, control, kind,
):
    body = (_harness_body(_harness_agent(), online_evaluators=[GSR], primary_evaluator=HELP)
            if kind == "harness"
            else _runtime_body(_runtime_agent(), online_evaluators=[GSR], primary_evaluator=HELP))

    res = client.post("/api/runtime-canaries", json=body)

    assert res.status_code == 400, res.text
    assert res.json()["code"] == "canary.primary_not_selected"
    assert not SessionLocal().query(RuntimeCanary).count()


def test_a_default_pair_member_may_be_primary_without_a_list(client, control):
    res = client.post("/api/runtime-canaries", json=_harness_body(
        _harness_agent(), primary_evaluator=GSR))

    assert res.status_code == 201, res.text
    assert res.json()["artifacts"]["primary_evaluator"] == GSR


# ─── setup ──────────────────────────────────────────────────────────────────
def _stub_harness_setup(monkeypatch) -> list[dict]:
    from app.services.agentcore import harness as hc

    monkeypatch.setattr(hc, "ensure_harness_endpoint", lambda *a, **kw: None)
    monkeypatch.setattr(
        canary_svc.canary_infra, "create_canary_gateway",
        lambda **kw: {"gateway_id": "gw-h", "gateway_arn": "arn:gw-h",
                      "gateway_url": "https://gw-h"},
    )
    monkeypatch.setattr(canary_harness, "enable_gateway_tracing", lambda *a, **kw: {})
    monkeypatch.setattr(canary_harness, "create_passthrough_target",
                        lambda control, *, name, **kw: f"id-{name}")
    monkeypatch.setattr(canary_harness, "ensure_log_group", lambda *a: None)
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
    return evals


def test_harness_setup_scores_both_arms_with_the_chosen_evaluators(monkeypatch):
    canary_id = _harness_canary(
        _harness_agent(), online_evaluators=[JUDGE, GSR], primary_evaluator=JUDGE)
    evals = _stub_harness_setup(monkeypatch)

    result = canary_svc.act_setup(canary_id, lambda _m: None)

    assert [e["evaluators"] for e in evals] == [[JUDGE, GSR], [JUDGE, GSR]]
    assert result["online_evaluators"] == [JUDGE, GSR]
    assert result["primary_evaluator"] == JUDGE


def test_a_legacy_harness_row_keeps_the_default_pair(monkeypatch):
    canary_id = _harness_canary(_harness_agent())
    evals = _stub_harness_setup(monkeypatch)

    result = canary_svc.act_setup(canary_id, lambda _m: None)

    # None → create_online_eval_idempotent's own default (the pair)
    assert [e["evaluators"] for e in evals] == [None, None]
    assert result["online_evaluators"] == list(ONLINE_EVAL_DEFAULT)
    assert "primary_evaluator" not in result


def test_runtime_setup_scores_both_variants_with_the_chosen_evaluators(monkeypatch):
    agent_id = _runtime_agent()
    db = SessionLocal()
    row = RuntimeCanary(
        workspace_id=DEFAULT_WORKSPACE_ID, name="CANARY-subject-r",
        champion_agent_id=agent_id, champion_agent_name="subject-r",
        challenger_agent_id=agent_id, challenger_agent_name="subject-r",
        artifacts={
            "agent_meta": {"id": agent_id, "name": "subject-r", "arn": RUNTIME_ARN,
                           "resource_id": "r-1", "runtime_name": "runtime-r"},
            "edited_spec": AgentSpec(name="subject-r", method="zip_runtime",
                                     system_prompt="edited").model_dump(),
            "rounds": [],
            "online_evaluators": [JUDGE, GSR],
            "primary_evaluator": JUDGE,
        },
    )
    db.add(row)
    db.commit()
    canary_id = row.id
    db.close()
    monkeypatch.setattr(canary_svc.canary_infra, "mint_candidate_version",
                        lambda **kw: ("1", "2", "agents/r/canary/x.zip"))
    monkeypatch.setattr(canary_svc.canary_infra, "current_version", lambda c, r: "1")
    monkeypatch.setattr(
        canary_svc.canary_infra, "create_canary_gateway",
        lambda **kw: {"gateway_id": "gw", "gateway_arn": "arn:gw", "gateway_url": "https://gw"},
    )
    monkeypatch.setattr(canary_svc.canary_infra, "ensure_endpoint_ready",
                        lambda control, **kw: {"status": "READY"})
    monkeypatch.setattr(exp_svc, "create_runtime_target_idempotent",
                        lambda control, gateway_id, name, arn, qualifier="DEFAULT": f"id-{name}")
    evals: list[dict] = []
    monkeypatch.setattr(
        exp_svc, "create_online_eval_idempotent",
        lambda control, **kw: evals.append(kw) or {
            "onlineEvaluationConfigId": f"id-{kw['name']}",
            "onlineEvaluationConfigArn": f"arn:{kw['name']}",
        },
    )
    data = MagicMock()
    data.create_ab_test.return_value = {"abTestId": "ab-r"}
    monkeypatch.setattr(canary_svc, "data_client", lambda _ws=None: data)
    monkeypatch.setattr(canary_svc, "control_client", MagicMock)

    result = canary_svc.act_setup(canary_id, lambda _m: None)

    assert [e["evaluators"] for e in evals] == [[JUDGE, GSR], [JUDGE, GSR]]
    assert result["online_evaluators"] == [JUDGE, GSR]
    assert result["primary_evaluator"] == JUDGE


# ─── paired score keying ────────────────────────────────────────────────────
def test_paired_scores_are_keyed_by_the_evaluator_arn_tail():
    query = canary_harness.paired_scores_query(["oe-c"], ["c0"])
    assert "attributes.aws.bedrock_agentcore.evaluator.arn as arn" in query
    assert "by sid, evaluator, arn" in query
    # the judge's evaluation NAME differs from its id; the ARN tail is the id
    assert canary_harness.parse_paired_scores([
        {"sid": "c0", "evaluator": "Store business rules", "arn": JUDGE_ARN, "mean": "0.5"},
        {"sid": "c0", "evaluator": GSR, "mean": "1"},
    ]) == {"c0": {JUDGE: 0.5, GSR: 1.0}}


# ─── paired verdict ─────────────────────────────────────────────────────────
def _pairs(n):
    return [{"scenario_id": f"S{i:02d}", "prompt": f"q{i}", "control_session_id": f"c{i}",
             "treatment_session_id": f"t{i}"} for i in range(n)]


def _paired_round(canary_id: str, n: int) -> None:
    db = SessionLocal()
    row = db.get(RuntimeCanary, canary_id)
    row.artifacts = {**row.artifacts, "setup": {**_harness_setup(canary_id), "ramp_stage": 1},
                     "rounds": [{"ramp_stage": 1, "weights": {}, "traffic_attempts": [
                         {"mode": "paired", "pairs": _pairs(n), "sent": 2 * n, "failed": 0,
                          "baseline_n": 0, "completed_at": "2026-10-08T05:00:00+00:00"}]}]}
    db.commit()
    db.close()


def _score_rows(n: int, scores: dict[str, tuple[float, float]]) -> list[dict]:
    rows = []
    for evaluator, (c, t) in scores.items():
        extra = {"arn": JUDGE_ARN, "evaluator": "Store business rules"} if evaluator == JUDGE \
            else {"evaluator": evaluator}
        for i in range(n):
            rows += [{"sid": f"c{i}", "mean": str(c), **extra},
                     {"sid": f"t{i}", "mean": str(t), **extra}]
    return rows


def _paired_verdict(monkeypatch, canary_id: str, rows: list[dict]) -> dict:
    monkeypatch.setattr(canary_svc, "data_client", lambda _ws=None: MagicMock())
    monkeypatch.setattr("app.services.observability.run_insights_queries",
                        lambda queries, hours, **_kw: {"q0": rows})
    monkeypatch.setattr(canary_svc, "PAIRED_VERDICT_DEADLINE_S", 0)
    monkeypatch.setattr(exp_svc, "_sleep", lambda _s: pytest.fail("verdict waited"))
    return canary_svc.act_verdict(canary_id, lambda _m: None)


def test_the_primary_decides_even_against_the_aggregate(monkeypatch):
    canary_id = _harness_canary(
        _harness_agent(), online_evaluators=[JUDGE, HELP], primary_evaluator=JUDGE)
    _paired_round(canary_id, 6)
    # the judge prefers the treatment (+0.5); Helpfulness prefers control by more (-0.8)
    rows = _score_rows(6, {JUDGE: (0.5, 1.0), HELP: (0.9, 0.1)})

    verdict = _paired_verdict(monkeypatch, canary_id, rows)

    assert {m["evaluatorId"] for m in verdict["metrics"]} == {JUDGE, HELP}
    assert verdict["avg_delta"] < 0  # the aggregate alone would say control-wins
    assert verdict["verdict"] == "treatment-wins"
    assert verdict["primary"] == JUDGE and verdict["primary_delta"] == 0.5
    assert verdict["significant"] is True  # 6 identical wins: p = 2/64
    assert verdict["n"] == 12
    assert verdict["evaluators"] == [JUDGE, HELP] and verdict["unscored"] == []
    assert JUDGE in verdict["pairs"][0]["treatment"]


def test_without_a_primary_the_aggregate_still_decides(monkeypatch):
    canary_id = _harness_canary(_harness_agent(), online_evaluators=[JUDGE, HELP])
    _paired_round(canary_id, 6)
    rows = _score_rows(6, {JUDGE: (0.5, 1.0), HELP: (0.9, 0.1)})

    verdict = _paired_verdict(monkeypatch, canary_id, rows)

    assert verdict["verdict"] == "control-wins"
    assert "primary" not in verdict


def test_a_configured_evaluator_with_no_scores_is_reported_unscored(monkeypatch):
    canary_id = _harness_canary(
        _harness_agent(), online_evaluators=[GSR, JUDGE], primary_evaluator=GSR)
    _paired_round(canary_id, 4)
    rows = _score_rows(4, {GSR: (0.4, 0.9)})  # the judge never scored

    verdict = _paired_verdict(monkeypatch, canary_id, rows)

    assert verdict["unscored"] == [JUDGE]
    assert verdict["verdict"] == "treatment-wins"  # the scored primary still decides


def test_an_unscored_primary_is_insufficient_data(monkeypatch):
    canary_id = _harness_canary(
        _harness_agent(), online_evaluators=[GSR, JUDGE], primary_evaluator=JUDGE)
    _paired_round(canary_id, 4)
    rows = _score_rows(4, {GSR: (0.4, 0.9)})

    verdict = _paired_verdict(monkeypatch, canary_id, rows)

    assert verdict["verdict"] == "insufficient-data"
    assert verdict["reason"] == "primary evaluator unscored"
    assert verdict["primary"] == JUDGE and verdict["unscored"] == [JUDGE]
    with pytest.raises(Exception) as excinfo:
        canary_svc.assert_verdict_allows(
            SessionLocal().get(RuntimeCanary, canary_id), allow_non_significant=True)
    assert excinfo.value.code == "canary.verdict_blocked"


def test_the_wait_covers_configured_evaluators_not_just_the_observed(monkeypatch):
    """GSR is fully scored, the judge is not yet: a legacy (observed-set) wait would
    stop; the configured set keeps waiting until the judge catches up."""
    canary_id = _harness_canary(_harness_agent(), online_evaluators=[GSR, JUDGE])
    _paired_round(canary_id, 4)
    early = _score_rows(4, {GSR: (0.4, 0.9)})
    late = _score_rows(4, {GSR: (0.4, 0.9), JUDGE: (0.0, 1.0)})
    calls: list[int] = []

    def insights(queries, hours, **_kw):
        calls.append(1)
        return {"q0": early if len(calls) == 1 else late}

    monkeypatch.setattr(canary_svc, "data_client", lambda _ws=None: MagicMock())
    monkeypatch.setattr("app.services.observability.run_insights_queries", insights)
    monkeypatch.setattr(exp_svc, "_sleep", lambda _s: None)

    verdict = canary_svc.act_verdict(canary_id, lambda _m: None)

    assert len(calls) == 2
    assert verdict["unscored"] == []


def test_a_legacy_row_waits_on_the_observed_evaluators_and_reports_nothing(monkeypatch):
    canary_id = _harness_canary(_harness_agent())
    _paired_round(canary_id, 4)
    monkeypatch.setattr(canary_svc, "PAIRED_VERDICT_DEADLINE_S", 10_000)
    rows = _score_rows(4, {GSR: (0.4, 0.9)})
    monkeypatch.setattr(canary_svc, "data_client", lambda _ws=None: MagicMock())
    monkeypatch.setattr("app.services.observability.run_insights_queries",
                        lambda queries, hours, **_kw: {"q0": rows})
    monkeypatch.setattr(exp_svc, "_sleep", lambda _s: pytest.fail("legacy verdict waited"))

    verdict = canary_svc.act_verdict(canary_id, lambda _m: None)

    assert verdict["verdict"] == "treatment-wins"
    assert "unscored" not in verdict and "evaluators" not in verdict


# ─── runtime (AWS A/B results) verdict ──────────────────────────────────────
def test_the_runtime_verdict_follows_the_primary(monkeypatch):
    agent_id = _runtime_agent()
    db = SessionLocal()
    row = RuntimeCanary(
        workspace_id=DEFAULT_WORKSPACE_ID, name="CANARY-subject-r",
        champion_agent_id=agent_id, champion_agent_name="subject-r",
        challenger_agent_id=agent_id, challenger_agent_name="subject-r",
        artifacts={
            "agent_meta": {"id": agent_id}, "edited_spec": {},
            "online_evaluators": [JUDGE, HELP, GSR], "primary_evaluator": JUDGE,
            "setup": {"ab_test_id": "ab-1", "ramp_stage": 0},
            "rounds": [{"ramp_stage": 0, "weights": {},
                        "traffic_attempts": [{"baseline_n": 0, "sent": 10, "failed": 0}]}],
        },
    )
    db.add(row)
    db.commit()
    canary_id = row.id
    db.close()

    def metric(arn, c, t, significant=False):
        return {"evaluatorArn": arn, "controlStats": {"mean": c, "sampleSize": 5},
                "variantResults": [{"name": "T1", "mean": t, "sampleSize": 5,
                                    "isSignificant": significant}]}

    data = MagicMock()
    data.get_ab_test.return_value = {"executionStatus": "RUNNING", "results": {
        "evaluatorMetrics": [
            metric(JUDGE_ARN, 0.6, 0.7),
            metric(f"arn:aws:bedrock-agentcore:::evaluator/{HELP}", 0.9, 0.2, True),
        ]}}
    monkeypatch.setattr(canary_svc, "data_client", lambda _ws=None: data)

    verdict = canary_svc.act_verdict(canary_id, lambda _m: None)

    assert verdict["verdict"] == "treatment-wins" and verdict["primary"] == JUDGE
    assert verdict["significant"] is False  # the primary's own test, not Helpfulness'
    assert verdict["n"] == 10
    assert verdict["unscored"] == [GSR]


def _runtime_row(attempt: dict, *, primary: str | None = JUDGE) -> str:
    agent_id = _runtime_agent()
    db = SessionLocal()
    row = RuntimeCanary(
        workspace_id=DEFAULT_WORKSPACE_ID, name="CANARY-subject-r",
        champion_agent_id=agent_id, champion_agent_name="subject-r",
        challenger_agent_id=agent_id, challenger_agent_name="subject-r",
        artifacts={
            "agent_meta": {"id": agent_id}, "edited_spec": {},
            "online_evaluators": [JUDGE, HELP],
            **({"primary_evaluator": primary} if primary else {}),
            "setup": {"ab_test_id": "ab-1", "ramp_stage": 0, "gateway_url": "https://gw",
                      "champion": {"target_name": "champ"}},
            "rounds": [{"ramp_stage": 0, "weights": {}, "traffic_attempts": [attempt]}],
        },
    )
    db.add(row)
    db.commit()
    canary_id = row.id
    db.close()
    return canary_id


def _ab_result(judge_n: int, help_n: int) -> dict:
    def metric(arn, c, t, n):
        return {"evaluatorArn": arn, "controlStats": {"mean": c, "sampleSize": n},
                "variantResults": [{"name": "T1", "mean": t, "sampleSize": n}]}
    return {"executionStatus": "RUNNING", "results": {"evaluatorMetrics": [
        metric(JUDGE_ARN, 0.6, 0.7, judge_n),
        metric(f"arn:aws:bedrock-agentcore:::evaluator/{HELP}", 0.9, 0.2, help_n),
    ]}}


def test_runtime_traffic_records_the_primary_baseline(monkeypatch):
    canary_id = _runtime_row({"baseline_n": 0, "sent": 1, "failed": 0})
    data = MagicMock()
    data.get_ab_test.return_value = _ab_result(judge_n=2, help_n=5)
    monkeypatch.setattr(canary_svc, "data_client", lambda _ws=None: data)
    monkeypatch.setattr(exp_svc, "send_gateway_traffic",
                        lambda *a, **kw: {"session_ids": ["s1"], "sent": 1, "failed": 0})

    attempt = canary_svc.act_traffic(canary_id, ["q"], {"dataset_id": "d"}, lambda _m: None)

    assert attempt["baseline_n"] == 14
    assert attempt["primary_baseline_n"] == 4  # the judge's own two arms


def test_runtime_traffic_without_a_primary_records_no_primary_baseline(monkeypatch):
    canary_id = _runtime_row({"baseline_n": 0, "sent": 1, "failed": 0}, primary=None)
    data = MagicMock()
    data.get_ab_test.return_value = _ab_result(judge_n=2, help_n=5)
    monkeypatch.setattr(canary_svc, "data_client", lambda _ws=None: data)
    monkeypatch.setattr(exp_svc, "send_gateway_traffic",
                        lambda *a, **kw: {"session_ids": ["s1"], "sent": 1, "failed": 0})

    attempt = canary_svc.act_traffic(canary_id, ["q"], {"dataset_id": "d"}, lambda _m: None)

    assert "primary_baseline_n" not in attempt


def test_runtime_verdict_waits_for_the_primary_not_just_any_evaluator(monkeypatch):
    """Helpfulness already scored the new stage, the judge has not: the verdict waits
    for the judge instead of deciding on its previous-stage evidence."""
    canary_id = _runtime_row(
        {"baseline_n": 14, "primary_baseline_n": 4, "sent": 4, "failed": 0})
    data = MagicMock()
    data.get_ab_test.side_effect = [_ab_result(judge_n=2, help_n=9),
                                    _ab_result(judge_n=5, help_n=9)]
    monkeypatch.setattr(canary_svc, "data_client", lambda _ws=None: data)
    monkeypatch.setattr(exp_svc, "_sleep", lambda _s: None)

    verdict = canary_svc.act_verdict(canary_id, lambda _m: None)

    assert data.get_ab_test.call_count == 2
    assert verdict["verdict"] == "treatment-wins" and verdict["n"] == 10


def test_a_stale_runtime_primary_at_the_deadline_is_insufficient_data(monkeypatch):
    canary_id = _runtime_row(
        {"baseline_n": 14, "primary_baseline_n": 4, "sent": 4, "failed": 0})
    data = MagicMock()
    data.get_ab_test.return_value = _ab_result(judge_n=2, help_n=9)
    monkeypatch.setattr(canary_svc, "data_client", lambda _ws=None: data)
    clock = itertools.chain([0.0], itertools.repeat(10_000.0))  # deadline ref, then past it
    monkeypatch.setattr(canary_svc, "datetime", _FakeDatetime(clock))
    monkeypatch.setattr(exp_svc, "_sleep", lambda _s: pytest.fail("waited past the deadline"))

    verdict = canary_svc.act_verdict(canary_id, lambda _m: None)

    assert verdict["verdict"] == "insufficient-data"
    assert verdict["reason"] == (
        "no new primary evaluator samples arrived after current-stage traffic")
    assert verdict["primary"] == JUDGE


class _FakeDatetime:
    """``canary_service.datetime`` whose ``now().timestamp()`` walks ``clock``."""

    def __init__(self, clock):
        import datetime as _dt

        self._clock = clock
        self._real = _dt.datetime

    def now(self, tz=None):
        stamp = next(self._clock)
        return self._real.fromtimestamp(stamp, tz)

    def __getattr__(self, name):
        return getattr(self._real, name)


# ─── compute_verdict ────────────────────────────────────────────────────────
def _m(evaluator, c, t, n=5, significant=False):
    return {"evaluatorId": evaluator, "label": evaluator.rsplit("/", 1)[-1],
            "control": {"mean": c, "sampleSize": n},
            "variants": [{"mean": t, "sampleSize": n, "isSignificant": significant}]}


def test_compute_verdict_without_a_primary_is_unchanged():
    metrics = [_m(GSR, 0.5, 0.9), _m(HELP, 0.9, 0.2)]
    assert exp_svc.compute_verdict(metrics) == exp_svc.compute_verdict(metrics, primary=None)
    assert "primary" not in exp_svc.compute_verdict(metrics)


def test_compute_verdict_orients_a_penalty_primary():
    verdict = exp_svc.compute_verdict(
        [_m("Builtin.Refusal", 0.4, 0.1), _m(GSR, 0.9, 0.5)], primary="Builtin.Refusal")
    assert verdict["verdict"] == "treatment-wins"
    assert verdict["primary_delta"] == pytest.approx(0.3)


def test_compute_verdict_uses_the_primary_sample_size():
    verdict = exp_svc.compute_verdict(
        [_m(JUDGE_ARN, 0.1, 0.9, n=1), _m(GSR, 0.5, 0.6, n=40)], primary=JUDGE)
    assert verdict["verdict"] == "insufficient-n" and verdict["n"] == 2


def test_compute_verdict_tie_and_missing_primary():
    assert exp_svc.compute_verdict([_m(JUDGE, 0.5, 0.5)], primary=JUDGE)["verdict"] == "tie"
    missing = exp_svc.compute_verdict([_m(GSR, 0.5, 0.9)], primary=JUDGE)
    assert missing["verdict"] == "insufficient-data"
    assert missing["reason"] == "primary evaluator unscored"
    assert exp_svc.compute_verdict([], primary=JUDGE)["verdict"] == "insufficient-data"
