"""Production gaps P7.1 / P7.6 — which rubric (and which worker) produced a run's scores.

A custom evaluator was updated in place with no history, and a run kept only the evaluator
ids: after an edit nobody could tell which instructions / scale / judge produced a past
score; the evaluation worker's release was not on the run either. Now an evaluator row has a
``version`` that moves only when a scoring-relevant field changes (with its
``definition_hash``), the run snapshots the resolved definitions AT SUBMIT
(``EvalRun.evaluator_snapshot``) — the plan replays that snapshot even if the evaluator is
edited while the run waits in the queue — and records the worker release it dispatched to.
"""

from __future__ import annotations

import app.evaluation.service as svc
from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.evaluation import evaluators_local
from app.evaluation.models import EvalEvaluator
from app.evaluation.routers import _run_out
from tests.conftest import ws_ctx
from tests.evaluation.test_resume import get_run, make_run

MODEL = "global.zai.glm-5.3"


def _judge(db, name="tone_judge"):
    return evaluators_local.create_judge(
        db, DEFAULT_WORKSPACE_ID, name=name, instructions="Rate the tone. {assistant_turn}",
        rating_scale=[{"label": "good", "value": 1.0}, {"label": "bad", "value": 0.0}],
        model_id=MODEL, level="TRACE")


def test_a_new_evaluator_is_version_one_with_a_hash():
    with SessionLocal() as db:
        row = _judge(db)
        assert row.version == 1 and len(row.definition_hash) == 16
        out = evaluators_local.evaluator_out(row)
        assert (out["version"], out["definition_hash"]) == (1, row.definition_hash)


def test_only_a_scoring_change_bumps_the_version():
    with SessionLocal() as db:
        row = _judge(db)
        first = row.definition_hash
        evaluators_local.update_judge(db, row, instructions=row.instructions,
                                      rating_scale=row.rating_scale, model_id=MODEL,
                                      level="TRACE", description="just a new description")
        assert (row.version, row.definition_hash) == (1, first)
        stricter = "Rate the TONE strictly. {assistant_turn}"
        evaluators_local.update_judge(db, row, instructions=stricter,
                                      rating_scale=row.rating_scale, model_id=MODEL,
                                      level="TRACE", description="x")
        assert row.version == 2 and row.definition_hash != first


def test_the_run_snapshots_the_definitions_and_the_plan_replays_the_snapshot():
    with SessionLocal() as db:
        row = _judge(db)
        evaluator_id = row.id
        snapshot = svc.evaluator_snapshot(db, ws_ctx(id=DEFAULT_WORKSPACE_ID),
                                          ["Builtin.Helpfulness", evaluator_id])
    assert snapshot[0]["id"] == "Builtin.Helpfulness" and snapshot[0]["kind"] == "builtin"
    assert snapshot[0]["version"].startswith("strands-agents-evals ")
    assert (snapshot[1]["version"], snapshot[1]["kind"]) == (1, "judge")
    assert snapshot[1]["definition"]["instructions"] == "Rate the tone. {assistant_turn}"
    run_id = make_run(mode="evaluators", evaluators=["Builtin.Helpfulness", evaluator_id],
                      evaluator_snapshot=snapshot)
    # the evaluator is edited while the run waits in the queue
    with SessionLocal() as db:
        row = db.get(EvalEvaluator, evaluator_id)
        evaluators_local.update_judge(db, row, instructions="CHANGED {assistant_turn}",
                                      rating_scale=row.rating_scale, model_id=MODEL,
                                      level="TRACE", description="")
    defs = svc.snapshot_definitions(get_run(run_id))
    assert [d["id"] for d in defs] == ["Builtin.Helpfulness", evaluator_id]
    assert defs[1]["instructions"] == "Rate the tone. {assistant_turn}"  # the submitted rubric
    assert defs[0] == {"id": "Builtin.Helpfulness"}


def test_the_run_payload_names_the_versions_and_the_worker_release():
    snapshot = [{"id": "ev1", "name": "tone_judge", "kind": "judge", "version": 3,
                 "definition_hash": "abcdef0123456789", "definition": {"id": "ev1"}}]
    run_id = make_run(mode="evaluators", evaluators=["ev1"], evaluator_snapshot=snapshot,
                      worker_release="1.0.0-0123456789ab")
    out = _run_out(get_run(run_id), snapshot=None)
    assert out["evaluator_versions"] == [{"id": "ev1", "name": "tone_judge", "kind": "judge",
                                          "version": 3, "definition_hash": "abcdef0123456789"}]
    assert out["worker_release"] == "1.0.0-0123456789ab"
    legacy = _run_out(get_run(make_run(mode="evaluators", evaluators=["ev1"])), snapshot=None)
    assert (legacy["evaluator_versions"], legacy["worker_release"]) == ([], None)


def test_the_worker_release_is_the_template_release_tag():
    from app.templates import eval_worker

    assert svc.worker_release_tag() == eval_worker.release_tag(eval_worker.release())


def test_a_builtin_detail_names_the_library_release_the_run_snapshot_records():
    """N17-obs434: the evaluator detail page shows a built-in's version — the library
    release, the same string a run's ``evaluator_versions`` names (never null)."""
    out = evaluators_local.builtin_out("Builtin.Helpfulness")
    with SessionLocal() as db:
        snapshot = svc.evaluator_snapshot(db, ws_ctx(id=DEFAULT_WORKSPACE_ID),
                                          ["Builtin.Helpfulness"])
    assert out["version"] == snapshot[0]["version"]
    assert out["version"].startswith("strands-agents-evals")
    assert out["definition_hash"] is None
