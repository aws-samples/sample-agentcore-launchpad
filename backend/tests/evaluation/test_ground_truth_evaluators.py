"""N17-obs437 — custom evaluators that read ground truth on the surfaces that have none.

A custom judge whose rubric reads a ground-truth placeholder (``{assertions}``,
``{expected_response}``, ``{expected_tool_trajectory}`` or the renderer's aliases
``{expected_trajectory}`` / ``{expected_output}``), a derived evaluator on such a base, and a
managed code evaluator with reference rules cannot score live traffic. The evaluator API now
says so (``requires_ground_truth`` + ``ground_truth_fields``) so the online / canary / SCORE NOW
pickers disable it like the trajectory matchers, and every no-ground-truth write refuses it —
the ``{expected_trajectory}`` alias used to pass all of them. Existing configs keep running:
only a write that names the evaluators re-validates them.
"""

from __future__ import annotations

import pytest

from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.core.errors import AppError
from app.evaluation import evaluators_local, managed
from app.evaluation.models import EvalEvaluator, OnlineEvalConfig
from app.evaluation.online_evaluators import normalize_online_evaluators
from tests.conftest import ws_ctx
from tests.evaluation.test_online_configs import ledger_row, make_agent


def _row(db, evaluator_id, instructions="Rate {context}.", *, definition="judge",
         level="SESSION", **cols):
    row = EvalEvaluator(id=evaluator_id, workspace_id=DEFAULT_WORKSPACE_ID, name=evaluator_id,
                        definition=definition, instructions=instructions, level=level, **cols)
    db.add(row)
    db.commit()
    return row


@pytest.fixture
def rows():
    db = SessionLocal()
    _row(db, "cust_assertions", "Judge {context} against {assertions}.")
    _row(db, "cust_alias", "Compare the tools in {context} with {expected_trajectory}.")
    _row(db, "cust_plain", "Rate {context} given {actual_tool_trajectory}.")
    _row(db, "cust_derived", None, definition="derived", level="TRACE",
         base_evaluator_id="Builtin.Correctness", model_id="global.zai.glm-5.3")
    _row(db, "cust_code", None, definition="code", level="TRACE", lambda_arn="arn:x")
    yield db
    db.close()


def test_aliases_count_as_their_ground_truth_field():
    assert evaluators_local.ground_truth_placeholders("{expected_trajectory}") == [
        "expected_tool_trajectory"]
    assert evaluators_local.ground_truth_placeholders("{expected_output} {assertions}") == [
        "expected_response", "assertions"]
    assert evaluators_local.ground_truth_placeholders("{actual_tool_trajectory}") == []


def test_the_evaluator_api_says_which_evaluators_need_ground_truth(client, rows):
    listed = {e["id"]: e for e in client.get("/api/eval/evaluators").json()["evaluators"]}
    assert (listed["cust_assertions"]["requires_ground_truth"],
            listed["cust_assertions"]["ground_truth_fields"]) == (True, ["assertions"])
    assert listed["cust_alias"]["ground_truth_fields"] == ["expected_tool_trajectory"]
    for evaluator in ("cust_plain", "cust_derived", "cust_code"):
        assert listed[evaluator]["requires_ground_truth"] is False, evaluator
    detail = client.get("/api/eval/evaluators/cust_alias").json()
    assert (detail["requires_ground_truth"], detail["ground_truth_fields"]) == (
        True, ["expected_tool_trajectory"])
    assert client.get("/api/eval/evaluators/Builtin.TrajectoryInOrderMatch").json()[
        "requires_ground_truth"] is True
    assert client.get("/api/eval/evaluators/Builtin.Helpfulness").json()[
        "requires_ground_truth"] is False


@pytest.mark.parametrize("prefix", ["online_eval", "canary", "experiment"])
@pytest.mark.parametrize("evaluator, field", [("cust_assertions", "assertions"),
                                              ("cust_alias", "expected_tool_trajectory")])
def test_online_writes_refuse_them_naming_the_evaluator(rows, prefix, evaluator, field):
    with pytest.raises(AppError) as refused:
        normalize_online_evaluators(["Builtin.Helpfulness", evaluator], rows,
                                    code_prefix=prefix)
    assert refused.value.code == f"{prefix}.evaluator_unsupported"
    assert refused.value.detail == {"evaluator": evaluator, "placeholders": [field]}
    assert evaluator in str(refused.value)
    assert normalize_online_evaluators(["cust_plain", "cust_derived", "cust_code"], rows,
                                       code_prefix=prefix) == [
        "cust_plain", "cust_derived", "cust_code"]


def test_a_managed_code_evaluator_with_reference_rules_is_refused(rows, monkeypatch):
    owner = {"definition": "code", "operation_id": "op1", "plan_key": "k"}
    monkeypatch.setattr(managed, "managed_evaluator",
                        lambda db, ws, eid: owner if eid == "cust_code" else None)
    monkeypatch.setattr(managed, "managed_rules",
                        lambda db, o: [{"type": "reference_response"}] if o else None)
    with pytest.raises(AppError) as refused:
        normalize_online_evaluators(["cust_code"], rows, code_prefix="online_eval")
    assert refused.value.detail["placeholders"] == ["expected_response"]


def test_score_now_and_a_run_over_sessions_refuse_the_alias(client, rows):
    from app.services import observability as obs

    with pytest.raises(AppError) as score_now:
        obs.evaluate_session("s" * 64, "24h", ["cust_alias"], ws_ctx(id=DEFAULT_WORKSPACE_ID))
    assert score_now.value.code == "observability.evaluator_needs_ground_truth"
    assert score_now.value.detail == {"evaluators": {"cust_alias": ["expected_tool_trajectory"]}}

    from app.evaluation import routers
    from app.routers.workspaces import WorkspaceScope

    db = SessionLocal()
    scope = WorkspaceScope(id=DEFAULT_WORKSPACE_ID, context=ws_ctx(id=DEFAULT_WORKSPACE_ID),
                           row=None)
    with pytest.raises(AppError) as run:
        routers._assert_target_references(db, scope, ["cust_alias"], [], False)
    db.close()
    assert run.value.code == "run.judge_needs_ground_truth"
    assert "cust_alias" in run.value.detail["evaluators"]


def test_an_existing_config_holding_one_keeps_working(client, rows, monkeypatch):
    """Configs written before the refusal are the user's: a PATCH that does not name the
    evaluators leaves them alone; only re-sending the set re-validates it."""
    agent = make_agent(rows)
    ledger_row(rows, agent, "oe_a-1", evaluators=["Builtin.Helpfulness", "cust_alias"])
    res = client.patch("/api/eval/online/oe_a-1", json={"sampling_percentage": 20})
    assert res.status_code == 200, res.text
    rows.expire_all()
    config = rows.query(OnlineEvalConfig).one()
    assert config.evaluators == ["Builtin.Helpfulness", "cust_alias"]
    assert config.sampling_percentage == 20.0
    resent = client.patch("/api/eval/online/oe_a-1",
                          json={"evaluators": ["Builtin.Helpfulness", "cust_alias"]})
    assert resent.status_code == 400
    assert resent.json()["code"] == "online_eval.evaluator_unsupported"
