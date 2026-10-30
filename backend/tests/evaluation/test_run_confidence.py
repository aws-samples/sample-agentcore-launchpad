"""EVAL-TRUST 3.2: a run's evaluator means carry their sample size and a 95 % t-interval on
session means (one observation per session); comparing two runs labels a difference
``not_significant`` when the intervals overlap or Welch's p ≥ 0.05."""

import random
import uuid

import pytest

from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.evaluation import results_store, run_stats
from app.evaluation.models import EvalRun
from app.optimization import stats
from tests.evaluation.test_runs_flow import make_agent, stub_environment, wait_terminal


@pytest.mark.parametrize(("df", "critical"), [(1, 12.7062), (4, 2.7764), (10, 2.2281),
                                              (30, 2.0423), (1000, 1.9623)])
def test_t_quantile_matches_the_table(df, critical):
    assert run_stats.t_quantile(df) == pytest.approx(critical, abs=1e-3)


def test_one_observation_per_session():
    records = [
        {"evaluator_id": "E", "session_id": "s1", "score": 1.0},
        {"evaluator_id": "E", "session_id": "s1", "score": 0.0},  # two turns → 0.5
        {"evaluator_id": "E", "session_id": "s2", "score": 1.0},
        {"evaluator_id": "E", "session_id": "s3", "score": None},  # errored: no score
    ]
    assert sorted(run_stats.session_scores(records)["E"]) == [0.5, 1.0]


def test_summary_interval_and_small_samples():
    out = run_stats.summarize([0.25, 0.75, 0.25, 0.75])
    assert out["sessions_n"] == 4 and out["session_mean"] == 0.5 and out["ci_method"] == "t"
    half = 3.1824 * 0.288675 / 2
    assert out["ci95"] == pytest.approx([0.5 - half, 0.5 + half], abs=1e-3)
    assert run_stats.summarize([0.7])["ci95"] is None
    assert run_stats.summarize([0.8, 0.8, 0.8])["ci95"] == [0.8, 0.8]


def test_summary_welch_equals_the_raw_welch():
    rng = random.Random(7)
    for _ in range(20):
        a = [rng.random() for _ in range(rng.randint(2, 30))]
        b = [rng.random() for _ in range(rng.randint(2, 30))]
        raw = stats.welch_two_sided(a, b)
        p = run_stats.welch_from_summary(run_stats.summarize(a), run_stats.summarize(b))
        assert p == pytest.approx(raw[2], abs=1e-3)


def test_labels():
    low = run_stats.summarize([0.1, 0.2, 0.15, 0.12, 0.18, 0.11])
    high = run_stats.summarize([0.9, 0.85, 0.95, 0.88, 0.92, 0.9])
    noisy = run_stats.summarize([0.0, 1.0, 0.2, 0.9, 0.5, 0.6])
    assert run_stats.compare(low, high)["label"] == "significant"
    assert run_stats.compare(low, noisy)["label"] == "not_significant"
    assert run_stats.compare(low, noisy)["ci_overlap"] is True
    assert run_stats.compare(low, run_stats.summarize([0.9]))["label"] == "insufficient"


def test_a_finished_run_carries_n_and_the_interval(client, monkeypatch):
    stub_environment(monkeypatch)
    db = SessionLocal()
    agent = make_agent(db, name=f"ci-{uuid.uuid4().hex[:6]}")
    db.close()
    ds = client.post("/api/eval/datasets", json={"name": "ci", "items": [
        {"prompt": "a"}, {"prompt": "b"}, {"prompt": "c"}]}).json()
    res = client.post("/api/eval/runs", json={
        "agent_id": agent.id, "dataset_id": ds["id"], "evaluators": ["Builtin.Helpfulness"],
        "wait_seconds": 0})
    run = wait_terminal(client, res.json()["id"])
    (score,) = run["scores"]
    assert score["sessions_n"] == 3
    assert "ci95" in score and "session_mean" in score and "sd" in score


def _stored_run(scores, records=None) -> str:
    db = SessionLocal()
    try:
        run = EvalRun(workspace_id=DEFAULT_WORKSPACE_ID, agent_id="", agent_name="svc",
                      mode="evaluators", status="completed", scores=scores,
                      session_ids=[r["session_id"] for r in records or []])
        db.add(run)
        db.commit()
        run_id = run.id
    finally:
        db.close()
    if records:
        db = SessionLocal()
        try:
            results_store.persist(db, DEFAULT_WORKSPACE_ID, source="run", owner_id=run_id,
                                  records=[{**r, "id": f"{run_id}-{i}", "level": "SESSION"}
                                           for i, r in enumerate(records)])
        finally:
            db.close()
    return run_id


def test_compare_labels_each_shared_evaluator(client):
    def entry(values):
        return {"evaluatorId": "Builtin.Helpfulness", "score": sum(values) / len(values),
                **run_stats.summarize(values)}

    a = _stored_run([entry([0.1, 0.2, 0.15, 0.12])])
    b = _stored_run([entry([0.9, 0.85, 0.95, 0.88])])
    c = _stored_run([entry([0.0, 1.0, 0.3, 0.8])])
    sig = client.get(f"/api/eval/runs/compare?a={a}&b={b}").json()["significance"]
    assert sig["Builtin.Helpfulness"]["label"] == "significant"
    flat = client.get(f"/api/eval/runs/compare?a={a}&b={c}").json()["significance"]
    assert flat["Builtin.Helpfulness"]["label"] == "not_significant"


def test_an_older_run_gets_its_interval_on_read(client):
    records = [{"session_id": f"s{i}", "evaluator_id": "Builtin.Helpfulness", "score": v}
               for i, v in enumerate([0.2, 0.4, 0.6])]
    run_id = _stored_run([{"evaluatorId": "Builtin.Helpfulness", "score": 0.4, "count": 3}],
                         records)
    (score,) = client.get(f"/api/eval/runs/{run_id}").json()["scores"]
    assert score["sessions_n"] == 3 and score["ci95"] is not None
    db = SessionLocal()
    try:
        assert db.get(EvalRun, run_id).scores[0]["sessions_n"] == 3  # persisted once
    finally:
        db.close()


# ── N17-obs423: the interval stays inside the scale ──────────────────────────
def test_the_reported_case_is_clamped_to_the_scale():
    # run 2d3fa04f0471: Correctness 0.714 at n = 7 read [0.263, 1.166]
    values = [1.0, 1.0, 1.0, 1.0, 1.0, 0.25, 0.0]
    out = run_stats.summarize(values)
    lo, hi = out["ci95"]
    assert 0.0 <= lo <= out["session_mean"] <= hi <= 1.0
    assert out["scale"] == [0.0, 1.0]


def test_a_binary_evaluator_gets_the_wilson_interval():
    values = [1.0] * 5 + [0.0] * 2  # 5 / 7
    out = run_stats.summarize(values)
    assert out["ci_method"] == "wilson"
    # Wilson 95 % for 5 / 7 (reference: 0.3589 – 0.9178)
    assert out["ci95"] == pytest.approx([0.3589, 0.9178], abs=1e-3)
    everyone = run_stats.summarize([1.0] * 6)
    assert everyone["ci95"][1] == 1.0 and everyone["ci95"][0] == pytest.approx(0.6097, abs=1e-3)
    # Welch's input is still the plain sample SD
    assert out["sd"] == pytest.approx(0.48795, abs=1e-4)


def test_a_custom_scale_bounds_the_interval(client):
    from app.core.db import SessionLocal as _S
    from app.evaluation.models import EvalEvaluator

    db = _S()
    try:
        row = EvalEvaluator(workspace_id=DEFAULT_WORKSPACE_ID, name="five_point", level="TRACE",
                            definition="judge", instructions="Rate {context}", model_id="m",
                            rating_scale=[{"value": v, "label": str(v), "definition": "x"}
                                          for v in (1.0, 2.0, 3.0, 4.0, 5.0)])
        db.add(row)
        db.commit()
        bounds = run_stats.scale_bounds(db, DEFAULT_WORKSPACE_ID,
                                        [row.id, "Builtin.Helpfulness"])
    finally:
        db.close()
    assert bounds[row.id] == (1.0, 5.0) and bounds["Builtin.Helpfulness"] == (0.0, 1.0)
    out = run_stats.summarize([5.0, 5.0, 4.0, 5.0], bounds[row.id])
    assert out["ci95"][1] == 5.0 and out["ci_method"] == "t"


def test_an_unclamped_stored_interval_is_summarised_again_on_read(client):
    records = [{"session_id": f"s{i}", "evaluator_id": "Builtin.Correctness", "score": v}
               for i, v in enumerate([1.0, 1.0, 0.5, 1.0])]
    run_id = _stored_run([{"evaluatorId": "Builtin.Correctness", "score": 0.875, "count": 4,
                           "sessions_n": 4, "session_mean": 0.875, "sd": 0.25,
                           "ci95": [0.477, 1.273]}], records)
    (score,) = client.get(f"/api/eval/runs/{run_id}").json()["scores"]
    assert score["ci95"][1] <= 1.0 and score["scale"] == [0.0, 1.0]
