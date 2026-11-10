# ruff: noqa: F811
"""Conversation authorization must cover the same runtime IDs as its reader."""

import pytest

from app.core.db import SessionLocal
from app.models.ledger import ChatMessage, ChatSession
from app.models.policy import PolicyRuntimeSession
from app.services import observability as obs
from tests.test_agent_visibility import ada, admin, app, bob, world  # noqa: F401
from tests.test_visibility import _add, _isolate

SID = "conversation-session-0001"
DERIVED = "lpp-derived-session-0001"
SECOND = "lpp-derived-session-0002"


def _conversation(world, derived=(DERIVED,)):
    _isolate()
    _add(ChatSession(agent_id=world.bob_agent, session_id=SID, actor_id="bob",
                     created_by=world.bob_id))
    _add(ChatMessage(agent_id=world.bob_agent, session_id=SID, role="user",
                     text="My original conversation"))
    with SessionLocal() as db:
        for index, sid in enumerate(derived):
            db.add(PolicyRuntimeSession(agent_id=world.bob_agent, session_id=SID,
                                       principal=f"user:bob-{index}", runtime_session_id=sid))
        db.commit()


@pytest.mark.parametrize("force", [False, True])
def test_listed_governed_conversation_opens_with_original_transcript(world, monkeypatch, force):
    from app.services import runtime_sessions

    _conversation(world)
    seen = []
    resolutions = []
    resolve = runtime_sessions.source_session_ids

    def source_set(*args, **kwargs):
        resolutions.append(args[2])
        return resolve(*args, **kwargs)

    monkeypatch.setattr(runtime_sessions, "source_session_ids", source_set)

    def query(queries, hours, **kwargs):
        seen.extend(queries.values())
        results = {}
        for key, text in queries.items():
            if "as record_count" in text:
                results[key] = ([{
                    "record_count": "82", "attributed_count": "82", "service_count": "1",
                    "service": "bob_agent.DEFAULT",
                }] if DERIVED in text else [])
            elif key == "sessions":
                results[key] = [{"session_id": DERIVED, "service": "bob_agent.DEFAULT"}]
            elif key.startswith("aggregates") and DERIVED in text:
                results[key] = [{
                    "traceId": "a" * 32, "session_id": DERIVED,
                    "service": "bob_agent.DEFAULT", "service_count": "1",
                    "llm_count": "6", "tokens_in": "20899",
                }]
            else:
                results[key] = []
        return results

    monkeypatch.setattr(obs, "run_insights_queries", query)
    from app.evaluation import online
    monkeypatch.setattr(online, "session_online_scores", lambda *a, **k: {})
    listed = world.bob.get("/api/observability/sessions?force=true")
    assert listed.status_code == 200, listed.text
    assert listed.json()["sessions"][0]["session_id"] == SID
    response = world.bob.get(
        f"/api/observability/sessions/{SID}?force={str(force).lower()}")
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["session_id"] == SID
    assert payload["summary"]["traces"] == 1
    assert payload["summary"]["llm_calls"] == 6
    assert payload["traces"][0]["session_id"] == DERIVED
    assert payload["transcript"]["available"] is True
    assert "My original conversation" in str(payload["transcript"])
    assert any(DERIVED in text and "as record_count" in text for text in seen)
    assert resolutions == [SID]  # the reader uses the same frozen set as authorization


GOOD = {"record_count": "82", "attributed_count": "82", "service_count": "1",
        "service": "bob_agent.DEFAULT"}


@pytest.mark.parametrize(("sources", "status"), [
    ({SID: [], DERIVED: [GOOD], SECOND: []}, 200),
    ({SID: [GOOD], DERIVED: [GOOD], SECOND: [GOOD]}, 200),
    ({SID: [], DERIVED: [], SECOND: [GOOD]}, 200),
    ({SID: [], DERIVED: [], SECOND: []}, 404),
    ({SID: [{**GOOD, "service": "foreign.DEFAULT"}], DERIVED: [GOOD], SECOND: []}, 404),
    ({SID: [], DERIVED: [GOOD], SECOND: [{**GOOD, "service": "ada_agent.DEFAULT"}]}, 404),
    ({SID: [], DERIVED: [{**GOOD, "attributed_count": "81"}], SECOND: [GOOD]}, 404),
    ({SID: [], DERIVED: [GOOD], SECOND: [{**GOOD, "service_count": "2"}]}, 404),
    ({SID: [], DERIVED: [GOOD], SECOND: [{"record_count": "0"}]}, 404),
    ({SID: [], DERIVED: [GOOD], SECOND: [{**GOOD, "record_count": "NaN"}]}, 404),
])
def test_all_conversation_sources_are_checked_before_read(world, monkeypatch, sources, status):
    _conversation(world, (DERIVED, SECOND))
    probes, reads = [], []

    def query(queries, hours, **kwargs):
        assert hours == 168
        results = {}
        for key, text in queries.items():
            sid = next(sid for sid in sources if f'= "{sid}"' in text)
            probes.append(sid)
            results[key] = sources[sid]
        return results

    monkeypatch.setattr(obs, "run_insights_queries", query)
    monkeypatch.setattr(obs, "get_session", lambda sid, *a, **k: (
        reads.append(sid) or {"session_id": sid, "traces": []}))
    response = world.bob.get(f"/api/observability/sessions/{SID}?range=7d")
    assert response.status_code == status, response.text
    assert probes == [SID, DERIVED, SECOND]
    assert reads == ([SID] if status == 200 else [])


def test_hidden_owner_rejected_before_queries_and_admin_keeps_diagnostics(world, monkeypatch):
    _conversation(world)
    monkeypatch.setattr(obs, "run_insights_queries",
                        lambda *a, **k: pytest.fail("hidden owner/admin must not probe"))
    monkeypatch.setattr(obs, "get_session", lambda sid, *a, **k: {
        "session_id": sid, "traces": []})
    assert world.ada.get(f"/api/observability/sessions/{SID}").status_code == 404
    assert world.admin.get(f"/api/observability/sessions/{SID}").status_code == 200


def test_foreign_workspace_mapping_not_included(world, monkeypatch):
    from app.models.ledger import Agent, Workspace

    _conversation(world)
    _add(Workspace(id="foreign", name="foreign", account_id="444455556666",
                   region="cn-north-1"))
    with SessionLocal() as db:
        db.add(Agent(id="foreign-agent", name="foreign", method="harness", workspace_id="foreign",
                     created_by=world.bob_id, resource_id="foreign-abcdefghij"))
        db.commit()
        db.add(PolicyRuntimeSession(agent_id="foreign-agent", session_id=SID,
                                   principal="user:foreign", runtime_session_id=SECOND))
        db.commit()

    def query(queries, hours, **kwargs):
        assert all(SECOND not in text for text in queries.values())
        return {key: [GOOD] for key in queries}

    monkeypatch.setattr(obs, "run_insights_queries", query)
    monkeypatch.setattr(obs, "get_session", lambda sid, *a, **k: {
        "session_id": sid, "traces": []})
    assert world.bob.get(f"/api/observability/sessions/{SID}").status_code == 200


def test_exact_session_authorization_does_not_gain_alias_authorization(world, monkeypatch):
    from app.core.errors import NotFoundError
    from app.services.observability_access import TelemetryAccess
    from app.services.visibility import Viewer
    from tests.conftest import ws_ctx

    _conversation(world)

    def query(queries, hours, **kwargs):
        assert all(DERIVED not in text for text in queries.values())
        return {key: [] for key in queries}

    monkeypatch.setattr(obs, "run_insights_queries", query)
    with SessionLocal() as db:
        access = TelemetryAccess(db, ws_ctx(id="default"),
                                 Viewer(user_id=world.bob_id, filters=True), admin=False)
        with pytest.raises(NotFoundError):
            access.require_remote_session(SID, 24)


def test_governed_conversation_score_now_reads_real_runtime_sources(world, monkeypatch):
    from app.evaluation import engine
    from tests.evaluation.fakes import FakeEngine

    _conversation(world, (DERIVED, SECOND))
    class GuardedEngine(FakeEngine):
        def evaluate_sessions(self, *args, records_guard=None, **kwargs):
            assert records_guard is not None
            for source_id in kwargs["session_ids"]:
                records_guard(source_id, [{
                    "attributes": {"session.id": source_id},
                    "resource": {"attributes": {"service.name": "bob_agent.DEFAULT"}},
                }])
                kwargs["on_check"](source_id, {"status": "ok", "turns_checked": 1})
            return super().evaluate_sessions(*args, **kwargs)

    fake = GuardedEngine(scores={"Builtin.Helpfulness": 0.75})
    monkeypatch.setattr(engine, "_engine", fake)

    def query(queries, hours, **kwargs):
        results = {}
        for key, text in queries.items():
            has_data = any(f'= "{sid}"' in text for sid in (DERIVED, SECOND))
            if "as record_count" in text:
                results[key] = [GOOD] if has_data else []
            else:
                results[key] = [{"records": "7", "log_group": "aws/spans"}] if has_data else []
        return results

    monkeypatch.setattr(obs, "run_insights_queries", query)
    response = world.bob.post(f"/api/observability/sessions/{SID}/evaluate",
                              json={"evaluator_ids": ["Builtin.Helpfulness"]})
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["session_id"] == SID
    assert payload["source_session_ids"] == [DERIVED, SECOND]
    assert {r["span_context"]["sessionId"] for r in payload["results"]} == {DERIVED, SECOND}
    assert set(payload["extraction_checks"]) == {DERIVED, SECOND}
    assert fake.calls[0]["session_ids"] == [DERIVED, SECOND]
    assert fake.calls[0]["owner_id"] == SID


@pytest.mark.parametrize("blocked_kind", ["private", "replay"])
def test_blocked_derived_source_denied_before_any_query(world, monkeypatch, blocked_kind):
    from app.core.errors import NotFoundError
    from app.services.observability_access import TelemetryAccess
    from app.services.visibility import Viewer
    from tests.conftest import ws_ctx

    _conversation(world)
    with SessionLocal() as db:
        access = TelemetryAccess(db, ws_ctx(id="default"),
                                 Viewer(user_id=world.bob_id, filters=True), admin=False,
                                 denied_sessions=frozenset({DERIVED})
                                 if blocked_kind == "private" else frozenset())
        if blocked_kind == "replay":
            access.replayed_sessions = {DERIVED: False}
        monkeypatch.setattr(obs, "run_insights_queries",
                            lambda *a, **k: pytest.fail("blocked source must not query"))
        with pytest.raises(NotFoundError):
            access.require_remote_conversation(SID, 24)


def test_hidden_agent_mapping_not_in_shared_source_set(world):
    from app.services import runtime_sessions
    from app.services.visibility import Viewer

    _conversation(world)
    with SessionLocal() as db:
        db.add(PolicyRuntimeSession(agent_id=world.ada_agent, session_id=SID,
                                   principal="user:ada", runtime_session_id=SECOND))
        db.commit()
        assert runtime_sessions.source_session_ids(
            db, "default", SID, Viewer(user_id=world.bob_id, filters=True),
        ) == [SID, DERIVED]


def test_detail_cache_is_keyed_by_sources_not_only_their_count(world, monkeypatch):
    from app.evaluation import online
    from tests.conftest import ws_ctx

    _conversation(world, (DERIVED, SECOND))
    queried = []

    def query(queries, *args, **kwargs):
        queried.extend(queries.values())
        return {key: [] for key in queries}

    monkeypatch.setattr(obs, "run_insights_queries", query)
    monkeypatch.setattr(obs, "session_transcript", lambda *a, **k: {})
    monkeypatch.setattr(online, "session_online_scores", lambda *a, **k: {})
    obs.reset_cache()
    with SessionLocal() as db:
        for source in (DERIVED, SECOND):
            obs.get_session(SID, "24h", db, ws_ctx(id="default"),
                            source_session_ids=[SID, source])
    assert any(f'= "{DERIVED}"' in text for text in queried)
    assert any(f'= "{SECOND}"' in text for text in queried)


def test_scoring_guard_cannot_read_a_different_visible_session(world):
    from app.core.errors import NotFoundError
    from app.services.observability_access import TelemetryAccess
    from app.services.visibility import Viewer
    from tests.conftest import ws_ctx

    _conversation(world)
    with SessionLocal() as db:
        guard = TelemetryAccess(
            db, ws_ctx(id="default"), Viewer(user_id=world.bob_id, filters=True), admin=False,
        ).records_guard(session_ids=frozenset({SID, DERIVED}))
    record = {"attributes": {"session.id": SECOND},
              "resource": {"attributes": {"service.name": "bob_agent.DEFAULT"}}}
    with pytest.raises(NotFoundError):
        guard(SECOND, [record])


@pytest.mark.parametrize("bad_source", [SID, DERIVED, SECOND])
def test_scoring_checks_every_source_before_engine(world, monkeypatch, bad_source):
    _conversation(world, (DERIVED, SECOND))

    def query(queries, hours, **kwargs):
        assert hours >= 24
        return {key: [{**GOOD, "service": "foreign.DEFAULT"}]
                if f'= "{bad_source}"' in text else [GOOD]
                for key, text in queries.items()}

    monkeypatch.setattr(obs, "run_insights_queries", query)
    monkeypatch.setattr(obs, "evaluate_session",
                        lambda *a, **k: pytest.fail("polluted source must not evaluate"))
    response = world.bob.post(f"/api/observability/sessions/{SID}/evaluate",
                              json={"evaluator_ids": ["Builtin.Helpfulness"]})
    assert response.status_code == 404


def test_mapped_scoring_without_span_groups_does_not_invoke_engine(world, monkeypatch):
    from app.evaluation import engine

    _conversation(world)
    monkeypatch.setattr(obs, "run_insights_queries", lambda queries, *a, **k: {
        key: [GOOD] if DERIVED in text else [] for key, text in queries.items()
    })
    monkeypatch.setattr(obs, "session_log_groups", lambda *a, **k: ([], 0))
    monkeypatch.setattr(engine, "get_engine", lambda: pytest.fail("no spans to score"))
    response = world.bob.post(f"/api/observability/sessions/{SID}/evaluate",
                              json={"evaluator_ids": ["Builtin.Helpfulness"]})
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "observability.session_spans_missing"


@pytest.mark.parametrize("bad_stage", [None, "provider", "lambda"])
def test_mapped_score_uses_real_provider_and_lambda_with_exact_source_guards(
    world, monkeypatch, bad_stage,
):
    import json

    from app.core.config import get_settings
    from app.evaluation import engine as engine_slot
    from app.evaluation.models import EvalEvaluator
    from app.evaluation.strands_engine import StrandsEngine
    from tests.evaluation.code_eval_support import InProcessLambda
    from tests.evaluation.fakes import FakeLogs
    from tests.evaluation.test_reader_record_guard import records

    _conversation(world)
    monkeypatch.setattr(get_settings(), "observability_spans_source", "runtime_logs")
    monkeypatch.setattr(get_settings(), "eval_code_evaluators_enabled", True)
    _add(EvalEvaluator(id="source_guard_code", name="source guard", level="SESSION",
                      definition="code", rating_scale=[],
                      lambda_arn="arn:aws-cn:lambda:cn-northwest-1:1:function:guard"))

    class Logs(FakeLogs):
        def start_query(self, **kwargs):
            text = kwargs["queryString"]
            assert DERIVED in text
            assert " like " not in text
            foreign = bad_stage == ("lambda" if self.started else "provider")
            batch = records(DERIVED, "foreign.DEFAULT" if foreign else "bob_agent.DEFAULT")
            self.routes = {"": [{"@message": json.dumps(row)} for row in batch]}
            return super().start_query(**kwargs)

    logs = Logs({})
    client = InProcessLambda(lambda event, ctx: {"label": "Pass", "value": 1.0})
    real_engine = StrandsEngine(
        logs_client=lambda ws: logs, lambda_client=lambda ws: client,
        judge_factory=lambda *a, **k: pytest.fail("code evaluator must not call an LLM"),
        sleep=lambda _: None,
    )
    monkeypatch.setattr(engine_slot, "get_engine", lambda: real_engine)
    monkeypatch.setattr(obs, "run_insights_queries", lambda queries, *a, **k: {
        key: [GOOD] if DERIVED in text else [] for key, text in queries.items()
    })
    monkeypatch.setattr(obs, "session_log_groups", lambda sid, *a, **k: (
        (["/custom/runtime"], 3) if sid == DERIVED else ([], 0)))
    response = world.bob.post(f"/api/observability/sessions/{SID}/evaluate",
                              json={"evaluator_ids": ["source_guard_code"]})
    assert response.status_code == (200 if bad_stage is None else 404), response.text
    if bad_stage is None:
        assert response.json()["source_session_ids"] == [DERIVED]
        assert response.json()["results"][0]["span_context"]["sessionId"] == DERIVED
        assert response.json()["extraction_checks"][DERIVED] is not None
        assert len(client.events) == 1
        assert all(row["attributes"]["session.id"] == DERIVED
                   for row in client.events[0]["evaluationInput"]["sessionSpans"])
    else:
        assert client.events == []
