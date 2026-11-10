# ruff: noqa: F811
"""Unknown account telemetry must not become a member's workspace data."""

import json

import pytest

from app.services import observability as obs
from tests.test_agent_visibility import (  # noqa: F401
    ada,
    admin,
    app,
    bob,
    world,
)


@pytest.fixture(autouse=True)
def local_remote_source(monkeypatch):
    monkeypatch.setattr(obs, "run_insights_queries", lambda *a, **k: {"ownership": [{
        "service": "bob_agent.DEFAULT", "record_count": "1",
        "attributed_count": "1", "service_count": "1",
    }]})


@pytest.mark.parametrize("mode", ["shared", "isolated"])
def test_unknown_external_lists_are_admin_only(world, monkeypatch, mode):
    from tests.test_visibility import _isolate

    if mode == "isolated":
        _isolate()
    rows = [
        {"trace_id": "a" * 32, "service": "bob_agent.DEFAULT",
         "session_id": "bob-session-0001", "agent": "bob-agent", "status": "ok"},
        {"trace_id": "b" * 32, "service": "external.DEFAULT",
         "session_id": "external-session-0001", "agent": "external.DEFAULT", "status": "ok"},
    ]
    monkeypatch.setattr(obs, "list_traces", lambda *a, **k: {"traces": rows})
    monkeypatch.setattr(obs, "list_sessions", lambda *a, **k: {"sessions": rows})
    for path in ("traces", "sessions"):
        response = world.bob.get(f"/api/observability/{path}")
        assert response.status_code == 200
        assert [r["service"] for r in response.json()[path]] == ["bob_agent.DEFAULT"]
        assert len(world.admin.get(f"/api/observability/{path}").json()[path]) == 2


def test_external_detail_and_agent_hint_cannot_bypass_membership(world, monkeypatch):
    external = {"service": "external.DEFAULT", "session_id": "external-session-0001"}
    monkeypatch.setattr(obs, "get_trace", lambda *a, **k: {"meta": external, "spans": []})
    monkeypatch.setattr(obs, "get_session", lambda *a, **k: {
        "session_id": external["session_id"], "traces": [external]})
    monkeypatch.setattr(obs, "run_insights_queries", lambda *a, **k: {"ownership": [external]})
    calls = []
    monkeypatch.setattr(obs, "get_session_transcript", lambda *a, **k: calls.append("transcript"))
    monkeypatch.setattr(obs, "session_transcripts", lambda *a, **k: (
        calls.append(dict(a[0])) or {}))
    monkeypatch.setattr(obs, "evaluate_session", lambda *a, **k: calls.append("evaluate"))
    sid = external["session_id"]
    for path in (
        f"traces/{'b' * 32}", f"sessions/{sid}",
        f"sessions/{sid}/transcript?agent_id={world.bob_agent}",
    ):
        response = world.bob.get(f"/api/observability/{path}")
        assert response.status_code == 404, response.text
    response = world.bob.post(f"/api/observability/sessions/{sid}/evaluate",
                              json={"evaluator_ids": ["Builtin.Helpfulness"]})
    assert response.status_code == 404
    response = world.bob.post("/api/observability/session-previews", json={
        "sessions": [{"session_id": sid}], "agent_id": world.bob_agent,
    })
    assert response.status_code == 200
    assert response.json()["previews"][0]["reason"] == "not_found"
    assert calls == [{}]
    assert world.admin.get(f"/api/observability/traces/{'b' * 32}").status_code == 200
    assert world.admin.get(f"/api/observability/sessions/{sid}").status_code == 200


@pytest.mark.parametrize("service", ["external.DEFAULT", "unrelated_prefix_bob_agent.DEFAULT"])
def test_local_session_does_not_authorize_a_conflicting_service(world, monkeypatch, service):
    from app.models.ledger import ChatSession
    from tests.test_visibility import _add

    sid = "local-session-collision"
    _add(ChatSession(agent_id=world.bob_agent, session_id=sid, actor_id="bob"))
    row = {"trace_id": "c" * 32, "service": service, "session_id": sid}
    monkeypatch.setattr(obs, "list_traces", lambda *a, **k: {"traces": [row]})
    monkeypatch.setattr(obs, "get_trace", lambda *a, **k: {"meta": row, "spans": []})
    assert world.bob.get("/api/observability/traces").json()["traces"] == []
    assert world.bob.get(f"/api/observability/traces/{'c' * 32}").status_code == 404


@pytest.mark.parametrize("extra", [
    {"services": ["bob_agent.DEFAULT", "external.DEFAULT"]},
    {"session_ids": ["local-session-0001", "external-session-0001"]},
])
def test_mixed_trace_is_not_authorized_by_one_local_contributor(world, monkeypatch, extra):
    from app.models.ledger import ChatSession
    from tests.test_visibility import _add

    _add(ChatSession(agent_id=world.bob_agent, session_id="local-session-0001", actor_id="bob"))
    payload = {"meta": {"service": "bob_agent.DEFAULT", "session_id": "local-session-0001",
                        **extra}, "spans": []}
    monkeypatch.setattr(obs, "get_trace", lambda *a, **k: payload)
    assert world.admin.get(f"/api/observability/traces/{'c' * 32}").status_code == 200
    assert world.bob.get(f"/api/observability/traces/{'c' * 32}").status_code == 404


def test_mixed_service_aggregates_are_not_authorized_by_latest_service(world, monkeypatch):
    row = {"service": "bob_agent.DEFAULT", "service_count": 2, "session_id": "mixed-session-1"}
    monkeypatch.setattr(obs, "list_traces", lambda *a, **k: {"traces": [row]})
    monkeypatch.setattr(obs, "list_sessions", lambda *a, **k: {"sessions": [row]})
    for path in ("traces", "sessions"):
        assert world.bob.get(f"/api/observability/{path}").json()[path] == []
        assert world.admin.get(f"/api/observability/{path}").json()[path] == [row]


def test_known_ledger_and_derived_sessions_do_not_need_aws_probes(world, monkeypatch):
    from app.core.db import SessionLocal
    from app.models.ledger import ChatSession
    from app.models.policy import PolicyRuntimeSession
    from tests.test_visibility import _add

    sid, derived = "local-session-0001", "lpp-local-session-0001"
    _add(ChatSession(agent_id=world.bob_agent, session_id=sid, actor_id="bob"))
    with SessionLocal() as db:
        db.add(PolicyRuntimeSession(agent_id=world.bob_agent, session_id=sid,
                                   principal="user:bob", runtime_session_id=derived))
        db.commit()
    monkeypatch.setattr(obs, "run_insights_queries",
                        lambda *a, **k: pytest.fail("known ledger session needs no metadata probe"))
    monkeypatch.setattr(obs, "get_session_transcript", lambda *a, **k: {"available": False})
    monkeypatch.setattr(obs, "list_sessions", lambda *a, **k: {
        "sessions": [{"session_id": sid}, {"session_id": derived}]})
    assert len(world.bob.get("/api/observability/sessions").json()["sessions"]) == 2
    for identifier in (sid, derived):
        assert world.bob.get(
            f"/api/observability/sessions/{identifier}/transcript").status_code == 200


def test_native_runtime_sessions_remain_readable_without_a_chat_row(world, monkeypatch):
    sid = "external-caller-local-runtime"
    probes = []

    def metadata(queries, hours, **kwargs):
        probes.append((queries, hours))
        return {"ownership": [{"service": "harness_bob_agent.DEFAULT", "record_count": "3",
                               "attributed_count": "3", "service_count": "1"}]}

    monkeypatch.setattr(obs, "run_insights_queries", metadata)
    monkeypatch.setattr(obs, "get_session_transcript", lambda *a, **k: {"available": False})
    response = world.bob.get(f"/api/observability/sessions/{sid}/transcript")
    assert response.status_code == 200
    assert len(probes) == 1 and probes[0][1] == 168
    assert sid in probes[0][0]["ownership"]


def test_another_workspace_ledger_does_not_authorize_a_member(world, monkeypatch):
    from app.models.ledger import ChatSession, Workspace
    from tests.test_visibility import _add

    _add(Workspace(id="other", name="other", account_id="444455556666", region="cn-north-1"))
    sid = "other-workspace-session"
    _add(ChatSession(workspace_id="other", agent_id=world.bob_agent, session_id=sid,
                     actor_id="bob"))
    monkeypatch.setattr(obs, "list_sessions", lambda *a, **k: {"sessions": [{"session_id": sid}]})
    assert world.bob.get("/api/observability/sessions").json()["sessions"] == []
    assert len(world.admin.get("/api/observability/sessions").json()["sessions"]) == 1


def test_metadata_failure_does_not_read_transcript(world, monkeypatch):
    from app.core.errors import AppError

    def unavailable(*args, **kwargs):
        raise AppError("observability.query_failed", "metadata unavailable", status_code=503)

    monkeypatch.setattr(obs, "run_insights_queries", unavailable)
    monkeypatch.setattr(obs, "get_session_transcript",
                        lambda *a, **k: pytest.fail("unknown provenance must fail closed"))
    response = world.bob.get("/api/observability/sessions/unknown-session-0001/transcript")
    assert response.status_code == 503


def test_local_session_collision_cannot_authorize_account_level_scoring(world, monkeypatch):
    from app.models.ledger import ChatSession
    from tests.test_visibility import _add

    sid = "local-session-collision"
    _add(ChatSession(agent_id=world.bob_agent, session_id=sid, actor_id="bob"))
    probes, calls = [], []

    def metadata(*args, **kwargs):
        probes.append(1)
        return {"ownership": [{"service": "external.DEFAULT", "record_count": "2",
                               "attributed_count": "2", "service_count": "1"}]}

    monkeypatch.setattr(obs, "run_insights_queries", metadata)
    monkeypatch.setattr(obs, "evaluate_session", lambda *a, **k: calls.append(1) or {})
    response = world.bob.post(f"/api/observability/sessions/{sid}/evaluate",
                              json={"evaluator_ids": ["Builtin.Helpfulness"]})
    assert response.status_code == 404, response.text
    assert probes == [1] and calls == []


def test_hidden_replay_session_in_a_single_content_event_is_not_authorized(world, monkeypatch):
    from app.evaluation.models import EvalRun
    from tests.test_visibility import _add, _isolate

    sid = "hidden-replay-session"
    _add(EvalRun(agent_id=world.bob_agent, agent_name="bob-agent", dataset_id="dataset-1",
                 status="completed", scenario_map={sid: {"scenario_id": "1"}},
                 created_by=world.ada_id))
    _isolate()
    payload = {"meta": {"service": "bob_agent.DEFAULT", "session_id": None,
                        "session_ids": [sid]}, "spans": []}
    monkeypatch.setattr(obs, "get_trace", lambda *a, **k: payload)
    assert world.bob.get(f"/api/observability/traces/{'c' * 32}").status_code == 404
    assert world.admin.get(f"/api/observability/traces/{'c' * 32}").status_code == 200
    monkeypatch.setattr(obs, "run_insights_queries",
                        lambda *a, **k: pytest.fail("hidden replay must fail before AWS"))
    response = world.bob.post(f"/api/observability/sessions/{sid}/evaluate",
                              json={"evaluator_ids": ["Builtin.Helpfulness"]})
    assert response.status_code == 404


def test_scoring_provenance_covers_sources_beyond_the_latest_500_traces(world, monkeypatch):
    sid = "large-remote-session"
    calls = []

    def metadata(queries, *args, **kwargs):
        query = queries["ownership"]
        if "by traceId" in query:
            # The external contributor is older than the newest 500 local traces.
            return {"ownership": [{"service": "bob_agent.DEFAULT", "session_id": sid}] * 500}
        return {"ownership": [{"service": "bob_agent.DEFAULT", "record_count": "501",
                               "attributed_count": "501", "service_count": "2"}]}

    monkeypatch.setattr(obs, "run_insights_queries", metadata)
    monkeypatch.setattr(obs, "evaluate_session", lambda *a, **k: calls.append(1) or {})
    response = world.bob.post(f"/api/observability/sessions/{sid}/evaluate",
                              json={"evaluator_ids": ["Builtin.Helpfulness"]})
    assert response.status_code == 404
    assert calls == []


@pytest.mark.parametrize(("summary", "status"), [
    ({"record_count": "501", "attributed_count": "501", "service_count": "1"}, 200),
    ({"record_count": "501", "attributed_count": "500", "service_count": "1"}, 404),
    ({"record_count": "501", "attributed_count": "501", "service_count": "2"}, 404),
    ({"record_count": "0", "attributed_count": "0", "service_count": "0"}, 404),
    ({"record_count": "NaN", "attributed_count": "NaN", "service_count": "1"}, 404),
    ({}, 404),
])
def test_scoring_probes_the_complete_actual_reader_window(world, monkeypatch, summary, status):
    from app.core.config import get_settings
    from app.evaluation.strands_engine import StrandsEngine

    monkeypatch.setattr(get_settings(), "eval_batch_wait_s", 48 * 3600)
    probes, calls = [], []

    def metadata(queries, hours, **kwargs):
        probes.append(hours)
        query = queries["ownership"]
        assert "limit " not in query.lower() and "by traceId" not in query
        assert "ispresent(startTimeUnixNano)" not in query  # content records contribute too
        return {"ownership": [{"service": "bob_agent.DEFAULT", **summary}]}

    monkeypatch.setattr(obs, "run_insights_queries", metadata)
    monkeypatch.setattr(obs, "evaluate_session", lambda *a, **k: calls.append(1) or {})
    response = world.bob.post("/api/observability/sessions/remote-session-001/evaluate", json={
        "evaluator_ids": ["Builtin.Helpfulness"], "range": "1h",
    })
    assert response.status_code == status
    assert probes == [StrandsEngine._lookback_hours()] == [72]
    assert calls == ([1] if status == 200 else [])


@pytest.mark.parametrize("service", ["external.DEFAULT", None, ""])
def test_session_detail_checks_remote_sources_even_for_a_local_ledger_id(
    world, monkeypatch, service,
):
    from app.models.ledger import ChatSession
    from tests.test_visibility import _add

    sid = "local-session-collision"
    _add(ChatSession(agent_id=world.bob_agent, session_id=sid, actor_id="bob"))
    monkeypatch.setattr(obs, "run_insights_queries", lambda *a, **k: {"ownership": [{
        "service": service, "record_count": "1",
        "attributed_count": "1", "service_count": "1",
    }]})
    monkeypatch.setattr(obs, "get_session",
                        lambda *a, **k: pytest.fail("do not read a conflicting transcript"))
    assert world.bob.get(f"/api/observability/sessions/{sid}").status_code == 404


def test_local_session_cannot_authorize_a_trace_with_missing_remote_source(world, monkeypatch):
    from app.models.ledger import ChatSession
    from tests.test_visibility import _add

    sid = "local-session-collision"
    _add(ChatSession(agent_id=world.bob_agent, session_id=sid, actor_id="bob"))
    monkeypatch.setattr(obs, "run_insights_queries", lambda *a, **k: {"ownership": [{
        "record_count": "1", "attributed_count": "0", "service_count": "0",
    }]})
    calls = []
    monkeypatch.setattr(obs, "get_trace", lambda *a, **k: (
        calls.append(1) or {"meta": {"session_ids": [sid]}, "spans": []}))
    assert world.bob.get(f"/api/observability/traces/{'a' * 32}").status_code == 404
    assert calls == []


@pytest.mark.parametrize("service", ["external.DEFAULT", None])
def test_trace_actual_content_source_is_checked_after_admin_warms_cache(
    world, monkeypatch, service,
):
    tid, sid = "d" * 32, "local-runtime-session"
    span = {"traceId": tid, "spanId": "s1", "name": "invoke", "startTimeUnixNano": 1,
            "endTimeUnixNano": 2, "attributes": {"session.id": sid},
            "resource": {"attributes": {"service.name": "bob_agent.DEFAULT",
                                        "aws.log.group.names": "/custom/runtime-content"}}}
    event = {"traceId": tid, "spanId": "s1", "attributes": {"session.id": sid},
             "resource": {"attributes": {"service.name": service}},
             "body": {"input": {"messages": [{"role": "user", "content": "foreign-content"}]}}}

    def query(queries, *args, **kwargs):
        return {key: (
            [{"@message": json.dumps(span)}] if key == "spans" else
            [{"@message": json.dumps(event)}] if key == "events" else
            [{"service": "bob_agent.DEFAULT", "record_count": "2",
              "attributed_count": "2", "service_count": "1"}]
        ) for key in queries}

    monkeypatch.setattr(obs, "run_insights_queries", query)
    obs.reset_cache()
    assert world.admin.get(f"/api/observability/traces/{tid}").status_code == 200
    response = world.bob.get(f"/api/observability/traces/{tid}")
    assert response.status_code == 404
    assert "foreign-content" not in response.text


def test_external_transcript_validates_actual_older_content_records(world, monkeypatch):
    from app.services import memory_backend

    sid = "external-caller-local-runtime"
    record = {"traceId": "a" * 32, "spanId": "s1", "timeUnixNano": 1,
              "attributes": {"session.id": sid},
              "resource": {"attributes": {"service.name": "external.DEFAULT"}},
              "body": {"input": {"messages": [{"role": "user", "content": "foreign-content"}]}}}

    class Logs:
        def filter_log_events(self, **kwargs):
            return {"events": [{"message": json.dumps(record)}]}

    monkeypatch.setattr(memory_backend, "_backend", None)
    monkeypatch.setattr(obs, "logs_client", lambda *_: Logs())
    response = world.bob.get(f"/api/observability/sessions/{sid}/transcript", params={
        "agent_id": world.bob_agent, "since": "2026-01-01T00:00:00Z",
    })
    assert response.status_code == 404
    assert "foreign-content" not in response.text


def test_external_transcript_skips_runtime_application_log_lines(world, monkeypatch):
    """N17-obs520: the substring filter over the runtime log group also matches the
    runtime's plain application log lines (`bedrock_agentcore.app`, bare `sessionId`,
    no resource attributes). They are not telemetry: a member's own external session
    must still read, while the guard keeps judging the actual content records."""
    from app.services import memory_backend

    sid = "external-caller-own-runtime"
    app_line = {"timestamp": "2026-10-29T04:58:40.129Z", "level": "INFO",
                "logger": "bedrock_agentcore.app", "requestId": "8a538410",
                "message": "Returning streaming response (generator) (0.000s)",
                "sessionId": sid[:20]}
    record = {"traceId": "a" * 32, "spanId": "s1", "timeUnixNano": 1,
              "attributes": {"session.id": sid},
              "resource": {"attributes": {"service.name": "bob_agent.DEFAULT"}},
              "body": {"input": {"messages": [{"role": "user", "content": "member-content"}]}}}

    class Logs:
        def filter_log_events(self, **kwargs):
            return {"events": [{"message": json.dumps(app_line)},
                               {"message": json.dumps(record)}]}

    monkeypatch.setattr(memory_backend, "_backend", None)
    monkeypatch.setattr(obs, "logs_client", lambda *_: Logs())
    response = world.bob.get(f"/api/observability/sessions/{sid}/transcript", params={
        "agent_id": world.bob_agent, "since": "2026-01-01T00:00:00Z",
    })
    assert response.status_code == 200, response.text
    assert "member-content" in response.text

    # fail-closed is unchanged: the same app line next to a FOREIGN content record is 404
    record["resource"]["attributes"]["service.name"] = "external.DEFAULT"
    record["body"]["input"]["messages"][0]["content"] = "foreign-content"
    response = world.bob.get(f"/api/observability/sessions/{sid}/transcript", params={
        "agent_id": world.bob_agent, "since": "2026-01-01T00:00:00Z",
    })
    assert response.status_code == 404
    assert "foreign-content" not in response.text


def test_chat_inspector_cannot_render_substring_session_records(world, monkeypatch):
    from app.models.ledger import ChatSession
    from app.services import traces
    from tests.test_visibility import _add

    sid = "local-chat-session"
    _add(ChatSession(agent_id=world.bob_agent, session_id=sid, actor_id="bob"))
    record = {"traceId": "a" * 32, "spanId": "s1", "name": "foreign-call",
              "attributes": {"session.id": sid + "-foreign"},
              "resource": {"attributes": {"service.name": "bob_agent.DEFAULT"}}}
    monkeypatch.setattr(traces, "run_insights_queries", lambda *a, **k: {
        "spans": [{"@message": json.dumps(record), "@log": "aws/spans"}],
    })
    response = world.bob.get(f"/api/traces/{sid}")
    assert response.status_code == 404
    assert "foreign-call" not in response.text


def test_member_record_guard_is_immutable_and_rejects_the_whole_batch(world, monkeypatch):
    from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
    from app.core.errors import AppError
    from app.models.ledger import Workspace
    from app.services import visibility
    from app.services.observability_access import TelemetryAccess
    from app.services.visibility import Viewer
    from app.services.workspace import workspace_context

    with SessionLocal() as db:
        guard = TelemetryAccess(
            db, workspace_context(db.get(Workspace, DEFAULT_WORKSPACE_ID)),
            Viewer(world.bob_id, False), admin=False,
        ).records_guard()
    assert guard is not None
    monkeypatch.setattr(visibility, "sees", lambda *a: pytest.fail("guard must not access DB"))
    valid = {"attributes": {"session.id": "exact-session-001"},
             "resource": {"attributes": {"service.name": "bob_agent.DEFAULT"}}}
    guard("exact-session-001", [valid])
    for invalid in ({}, {"attributes": {"session.id": "exact-session-001-foreign"},
                        "resource": valid["resource"]}, None,
                    {"attributes": {"session.id": "exact-session-001",
                                    "session": {"id": "another-session"}},
                     "resource": valid["resource"]}):
        with pytest.raises(AppError, match="session not found"):
            guard("exact-session-001", [valid, invalid])


@pytest.mark.parametrize("service", ["bob_agent.DEFAULT", "external.DEFAULT", None])
def test_score_now_passes_member_authorization_to_actual_reader(world, monkeypatch, service):
    """A successful metadata probe cannot authorize records arriving afterwards."""
    from app.evaluation import engine as engine_slot

    sid = "late-arriving-session"
    consumed = []
    record = {"attributes": {"session.id": sid},
              "resource": {"attributes": {"service.name": service}}}

    class Reader:
        def evaluate_sessions(self, workspace, *, records_guard, **kwargs):
            assert kwargs["session_ids"] == [sid]
            records_guard(sid, [record])
            consumed.append(record)
            return []

    monkeypatch.setattr(obs, "session_log_groups", lambda *a, **k: (["/custom/runtime"], 1))
    monkeypatch.setattr(engine_slot, "get_engine", lambda: Reader())
    response = world.bob.post(f"/api/observability/sessions/{sid}/evaluate", json={
        "evaluator_ids": ["Builtin.Helpfulness"],
    })
    allowed = service == "bob_agent.DEFAULT"
    assert response.status_code == (200 if allowed else 404), response.text
    assert consumed == ([record] if allowed else [])


@pytest.mark.parametrize(("root_service", "root_sid", "root_visible"), [
    ("external.DEFAULT", "foreign-session-001", False),
    (None, "foreign-session-001", False),
    ("bob_agent.DEFAULT", "local-session-001", True),
    ("bob_agent.DEFAULT", None, True),
    ("bob_agent.DEFAULT", "hidden-replay-session", False),
])
def test_member_root_enrichment_keeps_its_own_authorization_after_cache(
    world, monkeypatch, root_service, root_sid, root_visible,
):
    from app.evaluation import online
    from app.evaluation.models import EvalRun
    from tests.test_visibility import _add, _isolate

    tid, sid = "e" * 32, "local-session-001"
    _add(EvalRun(agent_id=world.bob_agent, agent_name="bob-agent", dataset_id="dataset-1",
                 status="completed", scenario_map={"hidden-replay-session": {"scenario_id": "1"}},
                 created_by=world.ada_id))
    _isolate()
    aggregate = {"traceId": tid, "service": "bob_agent.DEFAULT", "service_count": "1",
                 "session_id": sid, "start_ns": "1000000", "end_ns": "5000000",
                 "span_count": "2", "tokens_in": "12"}
    root = {"traceId": tid, "service": root_service, "session_id": root_sid,
            "name": "ROOT_OPERATION", "durationNano": "987000000"}

    def query(queries, *args, **kwargs):
        for key, query in queries.items():
            if key.startswith("roots"):
                assert "attributes.session.id as session_id" in query
                assert "resource.attributes.session.id as resource_session_id" in query
        return {key: (
            [{"service": "bob_agent.DEFAULT", "record_count": "2",
              "attributed_count": "2", "service_count": "1"}] if key == "ownership"
            else [root] if key.startswith("roots") else [aggregate]
        ) for key in queries}

    monkeypatch.setattr(obs, "run_insights_queries", query)
    monkeypatch.setattr(obs, "session_transcript", lambda *a, **k: {"available": False})
    monkeypatch.setattr(online, "session_online_scores", lambda *a, **k: {})
    obs.reset_cache()
    for path in ("/api/observability/traces", f"/api/observability/sessions/{sid}"):
        # Root provenance must survive an admin's cache entry without leaking into the API.
        admin = world.admin.get(path)
        assert admin.status_code == 200, admin.text
        assert admin.json()["traces"][0]["root_operation"] == "ROOT_OPERATION"
        assert "_root_enrichment" not in admin.json()["traces"][0]
        response = world.bob.get(path)
        assert response.status_code == 200, response.text
        row = response.json()["traces"][0]
        assert row["root_operation"] == ("ROOT_OPERATION" if root_visible else "trace")
        assert row["duration_ms"] == (987.0 if root_visible else 4.0)
        assert row["tokens"]["input"] == 12 and row["span_count"] == 2
        assert row["service"] == "bob_agent.DEFAULT"
        assert "_root_enrichment" not in row
        if not root_visible:
            assert "ROOT_OPERATION" not in response.text
            if root_sid != sid:
                assert root_sid not in response.text
        # Member sanitization must not mutate the shared cache or admin behavior.
        assert world.admin.get(path).json()["traces"][0]["duration_ms"] == 987.0
