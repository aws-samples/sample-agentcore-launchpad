"""Recommendations leave reviewed adversarial-test sessions out of the evidence (N12 E2-4).

A prompt-injection session teaches the optimizer the wrong lesson (the Global job refused
such traces outright), so a scenario whose golden test is marked ``adversarial`` stays
evaluated but its ``eval_results`` rows are not shipped to the worker.
"""

from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.evaluation.models import EvalDataset
from tests.evaluation.test_run_recommendations import SESSIONS, _agent, _run, _stub


def _item(scenario_id: str, *, adversarial: bool = False) -> dict:
    golden = {"id": f"G-{scenario_id}", "input": "x"}
    if adversarial:
        golden["adversarial"] = True
    return {"scenario_id": scenario_id, "turns": [{"input": f"prompt {scenario_id}"}],
            "metadata": {"launchpad_assets": {"golden_test_id": golden["id"],
                                              "golden_test": golden}}}


def _dataset(items: list[dict]) -> str:
    db = SessionLocal()
    try:
        row = EvalDataset(workspace_id=DEFAULT_WORKSPACE_ID, name="adv-set", kind="predefined",
                          items=items)
        db.add(row)
        db.commit()
        return row.id
    finally:
        db.close()


def _start(client, run_id):
    return client.post(f"/api/eval/runs/{run_id}/recommendations",
                       json={"kinds": ["system_prompt"], "system_prompt": "Help."})


def test_adversarial_sessions_are_left_out_and_recorded(client, monkeypatch):
    executor = _stub(monkeypatch)
    ds = _dataset([_item("S1"), _item("S2", adversarial=True), _item("S3")])
    run_id = _run(_agent(), session_ids=SESSIONS, dataset_id=ds)

    res = _start(client, run_id)

    assert res.status_code == 201, res.text
    spec = executor.submitted[0].spec
    assert sorted(e["session_id"] for e in spec["evidence"]) == ["sess-0001", "sess-0003"]
    assert spec["stats"]["records"] == 2
    excluded = [{"session_id": "sess-0002", "scenario_id": "S2"}]
    assert res.json()["recommendations"][0]["result"]["excluded_sessions"] == excluded

    # the worker's result replaces the payload, never the record of what was left out
    executor.complete(executor.submitted[0].job_id, [], summary={"recommendation": {
        "status": "COMPLETED", "recommended_prompt": "New.", "explanation": "Why.", "meta": {}}})
    row = client.get(f"/api/eval/runs/{run_id}/recommendations").json()["recommendations"][0]
    assert row["result"] == {"provider": "gepa_lite", "provider_model_id": "global.zai.glm-5.3",
                             "excluded_sessions": excluded, "recommended_prompt": "New.",
                             "explanation": "Why.", "provider_meta": {}}


def test_without_marked_sessions_every_session_is_evidence(client, monkeypatch):
    executor = _stub(monkeypatch)
    ds = _dataset([_item("S1"), _item("S2"), _item("S3")])

    res = _start(client, _run(_agent(), session_ids=SESSIONS, dataset_id=ds))

    assert res.status_code == 201, res.text
    assert len(executor.submitted[0].spec["evidence"]) == 3
    assert "excluded_sessions" not in res.json()["recommendations"][0]["result"]


def test_a_dataset_that_no_longer_pairs_with_the_sessions_excludes_nothing(client, monkeypatch):
    executor = _stub(monkeypatch)
    ds = _dataset([_item("S1", adversarial=True), _item("S2")])  # edited: 2 items, 3 sessions

    res = _start(client, _run(_agent(), session_ids=SESSIONS, dataset_id=ds))

    assert res.status_code == 201, res.text
    assert len(executor.submitted[0].spec["evidence"]) == 3


def test_a_run_of_only_adversarial_sessions_is_refused_before_any_job(client, monkeypatch):
    executor = _stub(monkeypatch)
    ds = _dataset([_item(s, adversarial=True) for s in ("S1", "S2", "S3")])

    res = _start(client, _run(_agent(), session_ids=SESSIONS, dataset_id=ds))

    assert res.status_code == 409
    assert res.json()["code"] == "recommendation.only_adversarial_sessions"
    assert executor.submitted == []
