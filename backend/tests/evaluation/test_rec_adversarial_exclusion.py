"""AgentCore prompt recommendations leave reviewed adversarial-test sessions out.

AgentCore Recommendations refuses traces that contain prompt-injection content (live
2026-10-03: one injection scenario failed a whole run's recommendation, the other 15
sessions completed), so a scenario whose golden test is marked ``adversarial`` stays
evaluated but its session is not sent; the other sessions' spans go inline.
"""

from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.evaluation.models import EvalDataset
from tests.evaluation.test_run_recommendations import BATCH_ARN, _run, _span, _stub


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


SESSIONS = ["sess-0001", "sess-0002", "sess-0003"]


def _start(client, run_id):
    return client.post(f"/api/eval/runs/{run_id}/recommendations",
                       json={"kinds": ["system_prompt"], "system_prompt": "Help."})


def test_adversarial_sessions_are_left_out_and_recorded(client, monkeypatch):
    spans = {sid: [_span(sid, "search")] for sid in SESSIONS}
    _, data = _stub(monkeypatch, spans=spans)
    data.start_recommendation.return_value = {"recommendationId": "rec-1"}
    ds = _dataset([_item("S1"), _item("S2", adversarial=True), _item("S3")])
    run_id = _run("", session_ids=SESSIONS, dataset_id=ds)

    res = _start(client, run_id)

    assert res.status_code == 201, res.text
    config = data.start_recommendation.call_args.kwargs["recommendationConfig"]
    traces = config["systemPromptRecommendationConfig"]["agentTraces"]
    assert traces == {"sessionSpans": spans["sess-0001"] + spans["sess-0003"]}
    assert [q["sessions"] for q in data.queries] == [["sess-0001", "sess-0003"]]
    excluded = [{"session_id": "sess-0002", "scenario_id": "S2"}]
    assert res.json()["recommendations"][0]["result"] == {"excluded_sessions": excluded}

    # the AWS result replaces the payload, never the record of what was left out
    data.get_recommendation.return_value = {"status": "COMPLETED", "recommendationResult": {
        "systemPromptRecommendationResult": {"recommendedSystemPrompt": "New.",
                                             "explanation": "Why."}}}
    row = client.get(f"/api/eval/runs/{run_id}/recommendations").json()["recommendations"][0]
    assert row["result"] == {"excluded_sessions": excluded,
                             "recommended_prompt": "New.", "explanation": "Why."}


def test_without_marked_sessions_the_batch_reference_is_kept(client, monkeypatch):
    _, data = _stub(monkeypatch)
    data.start_recommendation.return_value = {"recommendationId": "rec-1"}
    ds = _dataset([_item("S1"), _item("S2"), _item("S3")])

    res = _start(client, _run("", session_ids=SESSIONS, dataset_id=ds))

    assert res.status_code == 201, res.text
    config = data.start_recommendation.call_args.kwargs["recommendationConfig"]
    assert config["systemPromptRecommendationConfig"]["agentTraces"] == {
        "batchEvaluation": {"batchEvaluationArn": BATCH_ARN}}
    assert data.queries == []


def test_a_dataset_that_no_longer_pairs_with_the_sessions_excludes_nothing(client, monkeypatch):
    _, data = _stub(monkeypatch)
    data.start_recommendation.return_value = {"recommendationId": "rec-1"}
    ds = _dataset([_item("S1", adversarial=True), _item("S2")])  # edited: 2 items, 3 sessions

    res = _start(client, _run("", session_ids=SESSIONS, dataset_id=ds))

    assert res.status_code == 201, res.text
    config = data.start_recommendation.call_args.kwargs["recommendationConfig"]
    assert "batchEvaluation" in config["systemPromptRecommendationConfig"]["agentTraces"]


def test_a_run_of_only_adversarial_sessions_is_refused_before_aws(client, monkeypatch):
    _, data = _stub(monkeypatch)
    ds = _dataset([_item(s, adversarial=True) for s in ("S1", "S2", "S3")])

    res = _start(client, _run("", session_ids=SESSIONS, dataset_id=ds))

    assert res.status_code == 409
    assert res.json()["code"] == "recommendation.only_adversarial_sessions"
    data.start_recommendation.assert_not_called()


def test_the_golden_test_snapshot_carries_the_flag_only_when_set():
    from app.assistant.evaluation_plan import golden_test_snapshot

    assert golden_test_snapshot({"id": "G1", "input": "x", "adversarial": True})["adversarial"]
    assert "adversarial" not in golden_test_snapshot({"id": "G1", "input": "x",
                                                      "adversarial": False})
