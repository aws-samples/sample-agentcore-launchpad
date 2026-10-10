"""Console V2 数据处理 · 运行日志 — CloudWatch log records → dataset items.

Hermetic: Logs Insights is replaced by in-memory rows (`run_insights_queries`
patched where `pipeline_logs` calls it), so nothing reads CloudWatch.
"""

import json

import pytest
from pydantic import ValidationError

from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.evaluation import pipeline_logs, pipeline_routers
from app.evaluation.models import EvalDataset, EvalPipeline
from app.evaluation.pipeline_logs import LogFormat


def _row(message, stream="app-stream", ts="2026-10-10 03:00:00.000", group="/my/app"):
    if not isinstance(message, str):
        message = json.dumps(message)
    return {"@message": message, "@logStream": stream, "@timestamp": ts,
            "@log": f"123456789012:{group}"}


# newest first — the order the Logs Insights query returns
MESSAGE_ROWS = [
    _row({"conv": "c1", "role": "assistant", "content": "5 days."},
         ts="2026-10-10 03:00:04.000"),
    _row({"conv": "c1", "role": "human", "content": "And sick leave?"},
         ts="2026-10-10 03:00:03.000"),
    _row({"conv": "c2", "role": "user", "content": "Reset my password"},
         ts="2026-10-10 03:00:02.500"),
    _row({"conv": "c1", "role": "AI", "content": [{"text": "You have 12 days."}]},
         ts="2026-10-10 03:00:02.000"),
    _row('INFO 2026-10-10 handler {"conv": "c1", "role": "user", "content": "Leave days?"}',
         ts="2026-10-10 03:00:01.000"),
    _row({"conv": "c1", "role": "system", "content": "you are helpful"}),
    _row({"conv": "c3", "role": "user"}),
    _row({"role": "user", "content": "no session"}),
    _row("plain text line"),
]
MESSAGE_FMT = LogFormat(preset="message", session_field="conv", role_field="role",
                        text_field="content")


# ─── pure conversion ────────────────────────────────────────────────────────
def test_parse_record_and_paths():
    assert pipeline_logs.parse_record('{"a": 1}') == {"a": 1}
    assert pipeline_logs.parse_record('WARN x {"a": {"b": 2}} trailing') == {"a": {"b": 2}}
    assert pipeline_logs.parse_record("[1, 2]") is None
    assert pipeline_logs.parse_record("no json") is None
    record = {"attributes": {"session.id": "s1"}, "payload": {"messages": [{"text": "hi"}]}}
    assert pipeline_logs.get_path(record, "attributes.session.id") == "s1"  # flat dotted key
    assert pipeline_logs.get_path(record, "payload.messages.0.text") == "hi"
    assert pipeline_logs.get_path(record, "payload.messages.3.text") is None
    assert pipeline_logs.record_paths([record]) == ["attributes.session.id",
                                                    "payload.messages.0.text"]


def test_message_preset_groups_orders_and_counts_failures():
    out = pipeline_logs.convert(MESSAGE_ROWS, MESSAGE_FMT, max_sessions=10)
    sessions = {s["session_id"]: s for s in out["sessions"]}
    assert list(sessions) == ["c1", "c2"]  # newest session first
    assert sessions["c1"]["turns"] == [
        {"role": "user", "text": "Leave days?"},
        {"role": "assistant", "text": "You have 12 days."},
        {"role": "user", "text": "And sick leave?"},
        {"role": "assistant", "text": "5 days."},
    ]
    assert sessions["c1"]["log_group"] == "/my/app" and sessions["c1"]["records"] == 4
    assert out["failed"] == {"other_role": 1, "no_text": 1, "no_session": 1, "not_json": 1}
    assert (out["events"], out["parsed"], out["sessions_found"]) == (9, 5, 2)
    assert {"conv", "role", "content", "content.0.text"} <= set(out["paths"])


def test_paths_cover_every_format_in_a_mixed_group():
    # the newest records are another service's format: the older chat fields
    # must still be suggested, the most common paths first
    rows = [_row({"request_id": f"r{i}", "payload": {"query": "q"}}) for i in range(8)]
    rows += MESSAGE_ROWS
    paths = pipeline_logs.convert(rows, MESSAGE_FMT, max_sessions=10)["paths"]
    assert {"conv", "role", "content", "request_id", "payload.query"} <= set(paths)
    assert paths[:2] == ["request_id", "payload.query"]  # 8 records each


def test_max_sessions_keeps_the_newest_and_visibility_hides():
    out = pipeline_logs.convert(MESSAGE_ROWS, MESSAGE_FMT, max_sessions=1)
    assert [s["session_id"] for s in out["sessions"]] == ["c1"]
    assert out["sessions_found"] == 2
    out = pipeline_logs.convert(MESSAGE_ROWS, MESSAGE_FMT, max_sessions=10,
                                visible=lambda sid: sid != "c1")
    assert [s["session_id"] for s in out["sessions"]] == ["c2"]


def test_exchange_preset_with_and_without_sessions():
    rows = [_row({"q": "What is 6*7?", "a": {"text": "42"}}, ts="2026-10-10 03:00:01.000"),
            _row({"q": "Hello"}, ts="2026-10-10 03:00:00.000"),
            _row({"a": "orphan"})]
    fmt = LogFormat(preset="exchange", input_field="q", output_field="a")
    out = pipeline_logs.convert(rows, fmt, max_sessions=10)
    assert [s["turns"] for s in out["sessions"]] == [
        [{"role": "user", "text": "What is 6*7?"}, {"role": "assistant", "text": "42"}],
        [{"role": "user", "text": "Hello"}],
    ]
    assert out["failed"] == {"no_text": 1}
    again = pipeline_logs.convert(rows, fmt, max_sessions=10)
    assert [s["session_id"] for s in again["sessions"]] == [
        s["session_id"] for s in out["sessions"]
    ]

    by_stream = LogFormat(preset="exchange", input_field="q", output_field="a",
                          session_field="@logStream")
    out = pipeline_logs.convert(rows[:2], by_stream, max_sessions=10)
    assert [s["session_id"] for s in out["sessions"]] == ["app-stream"]
    assert [t["text"] for t in out["sessions"][0]["turns"]] == ["Hello", "What is 6*7?", "42"]


def _genai(trace, ts, user, reply=None, finish="end_turn", sid="g-1"):
    body = {"input": {"messages": [{"role": "user", "content": user}]}}
    if reply:
        body["output"] = {"messages": [{"role": "assistant",
                                        "content": {"message": reply, "finish_reason": finish}}]}
    return _row({"traceId": trace, "timeUnixNano": ts, "attributes": {"session.id": sid},
                 "body": body}, stream="otel-rt-logs")


def test_genai_preset_reuses_the_content_record_reader():
    rows = [_genai("t2", 3, "And sick leave?", "5 days."),
            _genai("t1", 2, "Leave days?", "12 days."),
            _genai("t1", 1, "Leave days?"),
            _row({"resource": {}, "scope": "spans"}),
            _genai("t3", 1, "other", "x", sid="")]
    out = pipeline_logs.convert(rows, LogFormat(), max_sessions=10)
    [session] = out["sessions"]
    assert session["session_id"] == "g-1"
    assert [(t["role"], t["text"]) for t in session["turns"]] == [
        ("USER", "Leave days?"), ("ASSISTANT", "12 days."),
        ("USER", "And sick leave?"), ("ASSISTANT", "5 days."),
    ]
    assert out["failed"] == {"not_genai": 1, "no_session": 1}


def test_format_validation():
    with pytest.raises(ValidationError):
        LogFormat(preset="message", session_field="conv", role_field="role")
    with pytest.raises(ValidationError):
        LogFormat(preset="exchange")
    with pytest.raises(ValidationError):
        LogFormat(preset="exchange", input_field="q; drop")
    with pytest.raises(ValidationError):
        LogFormat(preset="message", session_field="c", role_field="r", text_field="t",
                  user_roles=[" "])
    assert "like /(?i)a\\/b\\.c/" in pipeline_logs.events_query("a/b.c")
    assert "filter" not in pipeline_logs.events_query("  ")
    assert pipeline_logs.GENAI_FILTER in pipeline_logs.events_query(None, "genai")


# ─── routes ─────────────────────────────────────────────────────────────────
@pytest.fixture
def fake_logs(monkeypatch):
    calls = []

    def run_insights_queries(queries, hours, logs=None, log_groups=None, workspace=None):
        calls.append({"queries": queries, "hours": hours, "log_groups": log_groups})
        return {"events": list(state["rows"])}

    state = {"rows": MESSAGE_ROWS, "calls": calls}
    monkeypatch.setattr(pipeline_logs.observability, "run_insights_queries",
                        run_insights_queries)
    monkeypatch.setattr(pipeline_routers, "_spawn", lambda fn: fn())  # run inline
    return state


LOGS_SOURCE = {
    "type": "logs", "range": "7d", "max_sessions": 10,
    "log_groups": ["/my/app", "/my/app"], "keyword": "conv",
    "format": {"preset": "message", "session_field": "conv", "role_field": "role",
               "text_field": "content"},
}


def test_preview_logs(client, fake_logs):
    res = client.post("/api/eval/pipelines/preview-logs", json={"source": LOGS_SOURCE})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["events"] == 9 and body["sessions_found"] == 2 and body["truncated"] is False
    assert len(body["samples"]) == 5 and body["samples"][0]["stream"] == "app-stream"
    assert body["sessions"][0]["turns"][0] == {"role": "user", "text": "Leave days?"}
    [call] = fake_logs["calls"]
    assert call["log_groups"] == ["/my/app"] and call["hours"] == 168
    assert "(?i)conv" in call["queries"]["events"]

    res = client.post("/api/eval/pipelines/preview-logs", json={"source": {"type": "traces"}})
    assert res.status_code == 422
    res = client.post("/api/eval/pipelines/preview-logs",
                      json={"source": {"type": "logs", "log_groups": []}})
    assert res.status_code == 422


def _run(client, pid):
    assert client.post(f"/api/eval/pipelines/{pid}/run").status_code == 202
    return client.get(f"/api/eval/pipelines/{pid}").json()


def test_logs_pipeline_runs_into_a_dataset_once(client, fake_logs):
    res = client.post("/api/eval/pipelines", json={
        "name": "app logs", "source": LOGS_SOURCE, "output": {"dataset_name": "from-logs"},
    })
    assert res.status_code == 201, res.text
    pid = res.json()["id"]
    assert res.json()["config"]["source"]["format"]["preset"] == "message"

    first = _run(client, pid)
    assert first["status"] == "succeeded", first["last_run"]
    run = first["last_run"]
    assert (run["scanned"], run["matched"], run["added"]) == (2, 2, 2)
    assert run["log"]["failed"]["not_json"] == 1 and run["log"]["truncated"] is False
    db = SessionLocal()
    try:
        items = db.get(EvalDataset, run["dataset_id"]).items
    finally:
        db.close()
    assert [i["scenario_id"] for i in items] == ["log-c1", "log-c2"]
    assert items[0]["metadata"] == {"source": "logs", "session_id": "c1", "log_group": "/my/app"}
    assert items[0]["turns"][1] == {"input": "And sick leave?", "expected_response": "5 days."}

    assert _run(client, pid)["last_run"]["added"] == 0  # stable ids: nothing twice


def test_logs_pipeline_inputs_only(client, fake_logs):
    pid = client.post("/api/eval/pipelines", json={
        "name": "inputs", "source": LOGS_SOURCE, "processing": {"keep_replies": False},
        "output": {"dataset_name": "inputs-only"},
    }).json()["id"]
    run = _run(client, pid)["last_run"]
    db = SessionLocal()
    try:
        items = db.get(EvalDataset, run["dataset_id"]).items
    finally:
        db.close()
    assert all("expected_response" not in t for i in items for t in i["turns"])


def test_pre_log_source_rows_still_read_as_traces(client, fake_logs, monkeypatch):
    monkeypatch.setattr(pipeline_routers.observability, "list_sessions",
                        lambda *a, **k: {"sessions": []})
    db = SessionLocal()
    try:
        row = EvalPipeline(workspace_id=DEFAULT_WORKSPACE_ID, name="old", config={
            "source": {"agent": None, "range": "24h", "status": "all", "max_sessions": 5},
            "processing": {"first_turn_only": False, "dedupe": True, "min_input_chars": 0},
            "output": {"dataset_name": "old-ds"},
        })
        db.add(row)
        db.commit()
        pid = row.id
    finally:
        db.close()
    out = _run(client, pid)
    assert out["status"] == "succeeded" and out["last_run"]["scanned"] == 0
    assert fake_logs["calls"] == []  # never read logs
