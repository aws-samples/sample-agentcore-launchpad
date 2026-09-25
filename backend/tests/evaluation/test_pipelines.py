"""Console V2 数据处理 — observed sessions → local evaluation datasets.

Hermetic: the observability service is replaced by in-memory session fixtures
(synthetic transcripts), so nothing reads CloudWatch or Memory.
"""

import pytest

from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.evaluation import pipeline_routers, pipelines
from app.evaluation.models import EvalDataset

SID_A = "sess-aaaaaaaa-0001"
SID_B = "sess-bbbbbbbb-0002"
SID_EMPTY = "sess-cccccccc-0003"

SESSIONS = {
    SID_A: {
        "summary": {"agent": "hr-bot"},
        "transcript": {"available": True, "turns": [
            {"role": "user", "text": "How many leave days do I have?", "at": "t1"},
            {"role": "assistant", "text": "You have 12 days left.", "at": "t2"},
            {"role": "user", "text": "And sick leave?", "at": "t3"},
            {"role": "assistant", "text": "5 days.", "at": "t4"},
        ]},
    },
    SID_B: {
        "summary": {"agent": "hr-bot"},
        "transcript": {"available": True, "turns": [
            {"role": "user", "text": "Reset my password", "at": "t1"},
        ]},
    },
    SID_EMPTY: {"summary": {"agent": "hr-bot"},
                "transcript": {"available": False, "reason": "no_ledger_row"}},
}

ROWS = [
    {"session_id": SID_A, "agent": "hr-bot", "errors": 0, "last": "2026-09-25T10:00:00Z"},
    {"session_id": SID_B, "agent": "hr-bot", "errors": 2, "last": "2026-09-25T11:00:00Z"},
    {"session_id": SID_EMPTY, "agent": "hr-bot", "errors": 0, "last": "2026-09-25T09:00:00Z"},
    {"session_id": "sess-dddddddd-0004", "agent": "other", "errors": 0, "last": "x"},
]


@pytest.fixture
def fake_sessions(monkeypatch):
    calls = {"get": []}

    def get_session(session_id, range_key, db, workspace, force=False, logs=None):
        calls["get"].append(session_id)
        if session_id not in SESSIONS:
            from app.core.errors import NotFoundError

            raise NotFoundError("observability.session_not_found", "session not found")
        return {"session_id": session_id, **SESSIONS[session_id]}

    def list_sessions(range_key, db, workspace, force=False, logs=None):
        return {"range": range_key, "sessions": ROWS, "count": len(ROWS)}

    monkeypatch.setattr(pipeline_routers.observability, "get_session", get_session)
    monkeypatch.setattr(pipeline_routers.observability, "list_sessions", list_sessions)
    return calls


def _dataset(items, kind):
    db = SessionLocal()
    try:
        row = EvalDataset(workspace_id=DEFAULT_WORKSPACE_ID, name="seed", kind=kind, items=items)
        db.add(row)
        db.commit()
        return row.id
    finally:
        db.close()


# ─── pure extraction ────────────────────────────────────────────────────────
def test_exchanges_pairs_user_with_following_reply():
    turns = [
        {"role": "user", "text": "a"}, {"role": "user", "text": "b"},
        {"role": "assistant", "text": "reply"}, {"role": "assistant", "text": "orphan"},
        {"role": "user", "text": "unanswered"},
    ]
    assert pipelines.exchanges(turns) == [("a\nb", "reply"), ("unanswered", "")]


def test_items_predefined_and_legacy_shapes():
    turns = SESSIONS[SID_A]["transcript"]["turns"]
    [scenario] = pipelines.items_from_transcript(SID_A, turns, kind="predefined", agent="hr-bot")
    assert scenario["scenario_id"] == "trace-sess-aaaaaaaa-0001"
    assert scenario["turns"][1] == {"input": "And sick leave?", "expected_response": "5 days."}
    assert scenario["metadata"] == {"source": "trace", "session_id": SID_A, "agent": "hr-bot"}
    [single] = pipelines.items_from_transcript(SID_A, turns, kind="predefined",
                                               first_turn_only=True)
    assert len(single["turns"]) == 1
    assert pipelines.items_from_transcript(SID_A, turns, kind="legacy") == [
        {"prompt": "How many leave days do I have?", "expected": "You have 12 days left."}
    ]
    assert pipelines.items_from_transcript(SID_A, turns, kind="legacy", min_input_chars=500) == []


def test_merge_dedupes_and_caps():
    existing = [{"prompt": "same"}]
    merged, added, reasons = pipelines.merge_items(
        existing, [{"prompt": "same"}, {"prompt": "new"}], dedupe=True
    )
    assert added == 1 and reasons == ["duplicate"] and merged[-1] == {"prompt": "new"}
    full = [{"prompt": f"p{i}"} for i in range(pipelines.MAX_DATASET_ITEMS)]
    _, added, reasons = pipelines.merge_items(full, [{"prompt": "x"}])
    assert added == 0 and reasons == ["dataset_full"]


def test_select_sessions_filters_agent_status_and_limit():
    assert [r["session_id"] for r in pipelines.select_sessions(
        ROWS, agent="hr-bot", status="error", limit=5)] == [SID_B]
    ok = pipelines.select_sessions(ROWS, agent="hr-bot", status="ok", limit=1)
    assert [r["session_id"] for r in ok] == [SID_A]  # newest first, then capped


# ─── trajectories → dataset ─────────────────────────────────────────────────
def test_from_sessions_creates_predefined_dataset(client, fake_sessions):
    res = client.post("/api/eval/datasets/from-sessions", json={
        "session_ids": [SID_A, SID_B, SID_EMPTY, "sess-missing-0009"], "name": "badcases",
    })
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["added"] == 2
    assert body["dataset"]["kind"] == "predefined"
    assert body["dataset"]["item_count"] == 2
    reasons = {s["session_id"]: s["reason"] for s in body["skipped"]}
    assert reasons == {SID_EMPTY: "no_transcript", "sess-missing-0009": "not_found"}


def test_from_sessions_appends_to_legacy_and_dedupes(client, fake_sessions):
    ds = _dataset([{"prompt": "Reset my password"}], "legacy")
    res = client.post("/api/eval/datasets/from-sessions", json={
        "session_ids": [SID_A, SID_B], "dataset_id": ds,
    })
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["added"] == 1  # SID_B's question is already in the dataset
    assert body["dataset"]["items"][-1]["prompt"] == "How many leave days do I have?"


def test_from_sessions_rejects_simulated_and_empty(client, fake_sessions):
    ds = _dataset([{"scenario_id": "s1", "input": "hi",
                    "actor_profile": {"context": "c", "goal": "g"}}], "simulated")
    res = client.post("/api/eval/datasets/from-sessions",
                      json={"session_ids": [SID_A], "dataset_id": ds})
    assert res.status_code == 400
    assert res.json()["code"] == "dataset.kind_unsupported"

    res = client.post("/api/eval/datasets/from-sessions",
                      json={"session_ids": [SID_EMPTY], "name": "empty"})
    assert res.status_code == 422
    assert res.json()["code"] == "dataset.nothing_extracted"

    res = client.post("/api/eval/datasets/from-sessions",
                      json={"session_ids": [SID_A], "name": "x", "dataset_id": ds})
    assert res.status_code == 422  # both targets


# ─── saved pipelines ────────────────────────────────────────────────────────
def test_pipeline_crud_and_runs_append_once(client, fake_sessions):
    res = client.post("/api/eval/pipelines", json={
        "name": "hr trajectories",
        "source": {"agent": "hr-bot", "range": "24h", "status": "all", "max_sessions": 10},
        "processing": {"first_turn_only": True},
        "output": {"dataset_name": "hr-regression"},
    })
    assert res.status_code == 201, res.text
    pid = res.json()["id"]
    assert res.json()["status"] == "idle"
    assert [p["id"] for p in client.get("/api/eval/pipelines").json()["pipelines"]] == [pid]

    first = client.post(f"/api/eval/pipelines/{pid}/run").json()
    assert first["status"] == "succeeded", first["last_run"]
    run = first["last_run"]
    assert (run["scanned"], run["matched"], run["added"]) == (4, 3, 2)
    dataset_id = run["dataset_id"]
    # the first run created the dataset; the pipeline now targets it
    assert first["config"]["output"] == {"dataset_id": dataset_id}

    second = client.post(f"/api/eval/pipelines/{pid}/run").json()
    assert second["status"] == "succeeded"
    assert second["last_run"]["added"] == 0 and second["last_run"]["dataset_id"] == dataset_id
    db = SessionLocal()
    try:
        assert len(db.get(EvalDataset, dataset_id).items) == 2
    finally:
        db.close()

    upd = client.put(f"/api/eval/pipelines/{pid}", json={
        "name": "renamed", "output": {"dataset_id": dataset_id},
    })
    assert upd.status_code == 200 and upd.json()["name"] == "renamed"
    assert client.delete(f"/api/eval/pipelines/{pid}").json() == {"deleted": True}
    assert client.get(f"/api/eval/pipelines/{pid}").status_code == 404


def test_pipeline_output_must_exist_in_workspace(client, fake_sessions):
    res = client.post("/api/eval/pipelines", json={
        "name": "p", "output": {"dataset_id": "nope"},
    })
    assert res.status_code == 404
    res = client.post("/api/eval/pipelines", json={"name": "p", "output": {}})
    assert res.status_code == 422


def test_pipeline_run_failure_is_recorded(client, fake_sessions, monkeypatch):
    res = client.post("/api/eval/pipelines", json={"name": "p",
                                                    "output": {"dataset_name": "d"}})
    pid = res.json()["id"]

    def boom(*args, **kwargs):
        raise RuntimeError("logs unavailable")

    monkeypatch.setattr(pipeline_routers.observability, "list_sessions", boom)
    out = client.post(f"/api/eval/pipelines/{pid}/run").json()
    assert out["status"] == "failed"
    assert "logs unavailable" in out["last_run"]["error"]
