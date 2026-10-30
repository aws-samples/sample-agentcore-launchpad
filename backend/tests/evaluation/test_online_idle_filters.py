"""N17-obs353 (audit P4.3 / P4.8) — online idle detection.

P4.3: the config's filters fed the same ``filter`` as ``stats max(@timestamp) as last``, so
``last`` was the last MATCHING record: a session whose later records lack the filtered
attribute read as idle while still active, was scored on a partial conversation, and never
again. Now ``last`` is taken over every record of the session (the ``scan`` query, no
filters) and the filters are a separate predicate (the ``matched`` query): a session is
scored when it is idle over ALL its records and at least one record matches.

P4.8: the scan read only the runtime log group, so an agent with unified telemetry (content
only on the spans in ``aws/spans``) was never discovered. The scan now reads the job's
groups — the runtime group(s) plus ``aws/spans`` unless ``spans_source == runtime_logs``.
"""

from __future__ import annotations

from datetime import timedelta

from app.core.config import get_settings
from app.core.db import SessionLocal
from app.evaluation import online_scheduler as sched
from app.evaluation.models import OnlineSample
from tests.evaluation.test_online_configs import LOG_GROUP, SERVICE, ledger_row, make_agent
from tests.evaluation.test_online_scheduler import NOW, harness, scan_row

FILTERS = [{"key": "gen_ai.operation.name", "operator": "Equals",
            "value": {"stringValue": "invoke_agent"}}]


def per_query(answers: dict[str, list[dict]]):
    """A ``run_insights_queries`` double answering each named query on its own."""
    seen: list[dict] = []

    def run(queries, hours, logs=None, log_groups=None, workspace=None):
        seen.append({"queries": dict(queries), "log_groups": list(log_groups or [])})
        return {name: list(answers.get(name, [])) for name in queries}

    run.seen = seen  # type: ignore[attr-defined]
    return run


def _tick(db, answers, settings=None):
    executor, _q, kwargs = harness(settings=settings)
    queries = per_query(answers)
    sched.tick(db, NOW, **{**kwargs, "run_queries": queries})
    return queries, {s.session_id for s in db.query(OnlineSample).all()}


def test_an_active_session_whose_last_records_do_not_match_is_not_scored_yet():
    db = SessionLocal()
    ledger_row(db, make_agent(db), "oe_f-1", sampling_percentage=100.0,
               session_timeout_minutes=15, filters=FILTERS)
    answers = {
        # every record: s-active wrote 1 min ago (a later turn's chat / tool spans)
        "scan": [scan_row("session-active", NOW - timedelta(minutes=1)),
                 scan_row("session-idle", NOW - timedelta(minutes=40)),
                 scan_row("session-nomatch", NOW - timedelta(minutes=40))],
        # matching records only: s-active's last invoke_agent span is 30 min old
        "matched": [scan_row("session-active", NOW - timedelta(minutes=30)),
                    scan_row("session-idle", NOW - timedelta(minutes=41))],
    }
    queries, sampled = _tick(db, answers)
    assert sampled == {"session-idle"}
    call = queries.seen[0]["queries"]
    assert set(call) == {"scan", "matched"}
    assert "gen_ai.operation.name" not in call["scan"]  # last activity over ALL records
    assert 'attributes.gen_ai.operation.name = "invoke_agent"' in call["matched"]
    db.close()


def test_without_filters_one_scan_query_decides():
    db = SessionLocal()
    ledger_row(db, make_agent(db), "oe_f-2", sampling_percentage=100.0,
               session_timeout_minutes=15)
    queries, sampled = _tick(db, {"scan": [
        scan_row("session-one", NOW - timedelta(minutes=20)),
        scan_row("session-two", NOW - timedelta(minutes=2))]})
    assert sampled == {"session-one"}
    assert set(queries.seen[0]["queries"]) == {"scan"}
    db.close()


def test_the_scan_reads_aws_spans_so_unified_telemetry_agents_are_discovered():
    db = SessionLocal()
    ledger_row(db, make_agent(db), "oe_f-3", sampling_percentage=100.0,
               session_timeout_minutes=15)
    idle = scan_row("session-unified", NOW - timedelta(minutes=20))
    queries, sampled = _tick(db, {"scan": [idle]})
    assert sampled == {"session-unified"}
    assert queries.seen[0]["log_groups"] == [LOG_GROUP, "aws/spans"]
    assert f'"{SERVICE}"' in queries.seen[0]["queries"]["scan"]
    db.close()


def test_runtime_logs_mode_scans_the_runtime_group_only():
    db = SessionLocal()
    ledger_row(db, make_agent(db), "oe_f-4", sampling_percentage=100.0,
               session_timeout_minutes=15)
    settings = get_settings().model_copy(update={"observability_spans_source": "runtime_logs"})
    queries, _sampled = _tick(db, {"scan": []}, settings=settings)
    assert queries.seen[0]["log_groups"] == [LOG_GROUP]
    db.close()
