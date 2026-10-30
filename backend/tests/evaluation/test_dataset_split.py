"""D3-d / R3-c (eval Round 3): datasets carry a tuning / held-out split. Every scenario is in
one split (explicit ``split``, else a deterministic default); a run may replay one split;
tuning readers (recommendation evidence, the prompt optimizer's replay set) never read a
held-out scenario."""

import uuid

from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.evaluation import recommendations as recs
from app.evaluation import scenarios
from app.evaluation.models import EvalDataset, EvalRun
from tests.evaluation.test_run_recommendations import SESSIONS, _agent, _run, _stub
from tests.evaluation.test_runs_flow import make_agent, stub_environment, wait_terminal


def _item(sid: str, split: str | None = None) -> dict:
    item = {"scenario_id": sid, "turns": [{"input": f"prompt {sid}"}]}
    if split:
        item["split"] = split
    return item


def _dataset(items: list[dict]) -> str:
    db = SessionLocal()
    try:
        row = EvalDataset(workspace_id=DEFAULT_WORKSPACE_ID, name="split-set",
                          kind="predefined", items=items)
        db.add(row)
        db.commit()
        return row.id
    finally:
        db.close()


# ── the split itself ───────────────────────────────────────────────────────
def test_default_split_is_deterministic_and_holds_out_about_one_in_five():
    ids = [f"scenario-{n}" for n in range(2000)]
    first = [scenarios.default_split(i) for i in ids]
    assert first == [scenarios.default_split(i) for i in ids]
    share = first.count("held_out") / len(ids)
    assert 0.17 < share < 0.23, share


def test_a_scenario_keeps_its_split_when_others_are_added_or_reordered():
    items = [_item(f"S{n}") for n in range(1, 30)]
    before = dict(zip([i["scenario_id"] for i in items], scenarios.item_splits(items),
                      strict=True))
    grown = [_item("new-1"), *reversed(items), _item("new-2")]
    after = dict(zip([i["scenario_id"] for i in grown], scenarios.item_splits(grown),
                     strict=True))
    assert all(after[sid] == split for sid, split in before.items())


def test_explicit_split_wins_and_select_split_filters():
    # S12: default held_out; S3–S5 default tuning (5 scenarios: the default applies)
    items = [_item("S1", "held_out"), _item("S2", "tuning"), _item("S12"), _item("S3"),
             _item("S4")]
    assert scenarios.item_splits(items) == ["held_out", "tuning", "held_out", "tuning", "tuning"]
    held = scenarios.select_split(items, "held_out")
    assert [s["scenario_id"] for s in held] == ["S1", "S12"]
    assert all(s["split"] == "held_out" for s in held)  # the subset carries its resolved split
    assert [s["scenario_id"] for s in scenarios.select_split(items, "tuning")] == ["S2", "S3", "S4"]
    assert scenarios.select_split(items, "all") == items


def test_a_dataset_under_five_scenarios_defaults_to_all_tuning():
    small = [_item("S12"), _item("S14"), _item("S16"), _item("S17")]  # all default held_out
    assert scenarios.item_splits(small) == ["tuning"] * 4
    assert scenarios.split_counts(small) == {"tuning": 4, "held_out": 0}
    assert len(scenarios.select_split(small, "tuning")) == 4
    # an explicit split still holds out; at five scenarios the default applies
    assert scenarios.item_splits([*small[:3], _item("S1", "held_out")]) == [
        "tuning", "tuning", "tuning", "held_out"]
    assert scenarios.item_splits([*small, _item("S1")]) == ["held_out"] * 4 + ["tuning"]


def test_dataset_rejects_an_unknown_split_and_reports_counts(client):
    bad = client.post("/api/eval/datasets", json={
        "name": "bad-split", "items": [_item("S1", "validation")]})
    assert bad.status_code == 422 and bad.json()["code"] == "dataset.invalid_item"
    ok = client.post("/api/eval/datasets", json={
        "name": "ok-split", "items": [_item("S1", "held_out"), _item("S2"), _item("S3")]})
    assert ok.status_code == 201, ok.text
    assert ok.json()["split_counts"] == {"tuning": 2, "held_out": 1}
    assert ok.json()["item_splits"] == ["held_out", "tuning", "tuning"]


# ── runs ───────────────────────────────────────────────────────────────────
def test_a_held_out_run_replays_only_the_held_out_scenarios(client, monkeypatch):
    env, calls = stub_environment(monkeypatch)
    db = SessionLocal()
    agent = make_agent(db, name=f"split-{uuid.uuid4().hex[:6]}")
    db.close()
    ds = _dataset([_item("S1", "held_out"), _item("S2", "tuning"), _item("S3", "tuning")])
    res = client.post("/api/eval/runs", json={
        "agent_id": agent.id, "dataset_id": ds, "split": "held_out",
        "evaluators": ["Builtin.Helpfulness"], "wait_seconds": 0})
    assert res.status_code == 201, res.text
    run = wait_terminal(client, res.json()["id"])
    assert run["dataset_split"] == "held_out"
    assert calls["prompts"] == ["prompt S1"]
    assert [s["scenario_id"] for s in run["session_scenarios"]] == ["S1"]
    assert run["session_scenarios"][0]["split"] == "held_out"
    # a re-run keeps the split
    again = client.post(f"/api/eval/runs/{run['id']}/rerun", json={})
    assert again.status_code == 201, again.text
    assert again.json()["dataset_split"] == "held_out"


def test_an_all_run_records_each_sessions_split(client, monkeypatch):
    stub_environment(monkeypatch)
    db = SessionLocal()
    agent = make_agent(db, name=f"split-all-{uuid.uuid4().hex[:6]}")
    db.close()
    ds = _dataset([_item("S1", "held_out"), _item("S2", "tuning")])
    res = client.post("/api/eval/runs", json={
        "agent_id": agent.id, "dataset_id": ds,
        "evaluators": ["Builtin.Helpfulness"], "wait_seconds": 0})
    run = wait_terminal(client, res.json()["id"])
    assert run["dataset_split"] == "all"
    assert {s["scenario_id"]: s["split"] for s in run["session_scenarios"]} == {
        "S1": "held_out", "S2": "tuning"}


def test_an_empty_split_and_a_passive_split_are_refused(client, monkeypatch):
    stub_environment(monkeypatch)
    db = SessionLocal()
    agent = make_agent(db, name=f"split-empty-{uuid.uuid4().hex[:6]}")
    db.close()
    ds = _dataset([_item("S1", "tuning")])
    empty = client.post("/api/eval/runs", json={
        "agent_id": agent.id, "dataset_id": ds, "split": "held_out", "wait_seconds": 0})
    assert empty.status_code == 422 and empty.json()["code"] == "run.split_empty"
    passive = client.post("/api/eval/runs", json={
        "agent_id": agent.id, "lookback_hours": 24, "split": "tuning"})
    assert passive.status_code == 422 and passive.json()["code"] == "run.split_scope"
    db = SessionLocal()
    try:
        assert db.query(EvalRun).filter_by(agent_id=agent.id).count() == 0
    finally:
        db.close()


# ── tuning readers ─────────────────────────────────────────────────────────
def _split_run(ds: str, splits: list[str]) -> str:
    scenario_map = {sid: {"scenario_id": f"S{n}", "input": "x", "split": split}
                    for n, (sid, split) in enumerate(zip(SESSIONS, splits, strict=True), 1)}
    return _run(_agent(), session_ids=SESSIONS, dataset_id=ds, scenario_map=scenario_map)


def test_recommendation_evidence_leaves_held_out_sessions_out(client, monkeypatch):
    executor = _stub(monkeypatch)
    ds = _dataset([_item("S1"), _item("S2", "held_out"), _item("S3")])
    run_id = _split_run(ds, ["tuning", "held_out", "tuning"])
    res = client.post(f"/api/eval/runs/{run_id}/recommendations",
                      json={"kinds": ["system_prompt"], "system_prompt": "Help."})
    assert res.status_code == 201, res.text
    spec = executor.submitted[0].spec
    assert sorted(e["session_id"] for e in spec["evidence"]) == ["sess-0001", "sess-0003"]
    assert res.json()["recommendations"][0]["result"]["held_out_sessions"] == [
        {"session_id": "sess-0002", "scenario_id": "S2"}]
    # never silent (D3-d): the response says how much the held-out split kept out
    notice = res.json()["held_out_excluded"]
    assert notice["sessions"] == 1 and notice["scenarios"] == 1
    assert notice["message"].startswith("1 held-out scenario excluded")
    assert res.json()["recommendations"][0]["result"]["held_out_excluded"] == notice


def test_an_acceptance_run_never_seeds_a_recommendation(client, monkeypatch):
    executor = _stub(monkeypatch)
    ds = _dataset([_item(f"S{n}", "held_out") for n in (1, 2, 3)])
    run_id = _split_run(ds, ["held_out"] * 3)
    res = client.post(f"/api/eval/runs/{run_id}/recommendations",
                      json={"kinds": ["system_prompt"], "system_prompt": "Help."})
    assert res.status_code == 409
    assert res.json()["code"] == "recommendation.only_held_out_sessions"
    assert executor.submitted == []


def test_the_optimizer_replays_the_tuning_split_only():
    ds = _dataset([_item("S1", "held_out"), _item("S2", "tuning"), _item("S3")])
    db = SessionLocal()
    try:
        run = db.get(EvalRun, _split_run(ds, ["held_out", "tuning", "tuning"]))
        from tests.conftest import ws_ctx

        extras = recs.optimize_extras(db, run, None, ws_ctx(), "job-1", "Builtin.Helpfulness")
    finally:
        db.close()
    assert [p["scenario_id"] for p in extras["dataset_prompts"]] == ["S2", "S3"]
