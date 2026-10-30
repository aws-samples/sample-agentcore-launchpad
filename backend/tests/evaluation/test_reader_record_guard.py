"""Authorize the records actually consumed, not an earlier metadata snapshot."""

import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core.errors import NotFoundError
from app.services.strands_eval.code_evaluators import (
    CodeEvaluatorRuntime,
    LambdaEvaluator,
    SessionSpans,
)
from app.services.strands_eval.spans_provider import LaunchpadSpansProvider
from tests.evaluation.fakes import FakeLogs

FIXTURE = json.loads((Path(__file__).parents[1] / "fixtures/spans_session.json").read_text())
SID, GROUP = FIXTURE["session_id"], FIXTURE["log_group"]


def records(sid=SID, service="local"):
    out = deepcopy(FIXTURE["records"])
    for row in out:
        row["attributes"]["session.id"] = sid
        row["resource"] = {"attributes": {"service.name": service}}
    return out


def guard(sid, batch):
    if not batch or any(
        row.get("attributes", {}).get("session.id") != sid
        or row.get("resource", {}).get("attributes", {}).get("service.name") != "local"
        for row in batch
    ):
        raise NotFoundError("observability.session_not_found", "session not found")


class ReadingLogs(FakeLogs):
    """Optionally emulate server equality so the query itself is tested."""

    def __init__(self, batch, *, server_filter=False):
        super().__init__({})
        self.batch, self.server_filter = batch, server_filter

    def start_query(self, **kwargs):
        batch = self.batch
        if self.server_filter and " like " not in kwargs["queryString"]:
            batch = [r for r in batch if r["attributes"]["session.id"] == SID]
        self.routes = {"": [{"@message": json.dumps(r)} for r in batch]}
        return super().start_query(**kwargs)


def test_reader_queries_exact_session_instead_of_matching_foreign_suffix():
    foreign = records(SID + "-foreign")
    for row in foreign:
        row["traceId"] = "foreign-trace"
    logs = ReadingLogs(records() + foreign, server_filter=True)
    provider = LaunchpadSpansProvider(logs, [GROUP], spans_source="runtime_logs")
    data = provider.get_evaluation_data(SID)
    assert " like " not in logs.started[0]["queryString"]
    assert all(t.trace_id != "foreign-trace" for t in data["trajectory"].traces)


@pytest.mark.parametrize("foreign", [records(SID + "-foreign"), records(service="foreign"),
                                     [{"attributes": {"session.id": SID}}]])
def test_every_raw_record_is_guarded_before_mapper_or_agent_filter(foreign):
    mapped = []
    provider = LaunchpadSpansProvider(
        ReadingLogs(records() + foreign), [GROUP, "custom-group"],
        spans_source="runtime_logs", records_guard=guard,
        mapper=SimpleNamespace(map_to_session=lambda *args: mapped.append(args)),
    )
    with pytest.raises(NotFoundError):
        provider.get_evaluation_data(SID)
    assert mapped == []
    assert provider.origin_reference(SID) is None


def test_hierarchy_read_is_also_guarded_before_mapping():
    class LateForeign(ReadingLogs):
        def start_query(self, **kwargs):
            if self.started:
                self.batch = records(service="foreign")
            return super().start_query(**kwargs)

    mapped = []
    logs = LateForeign(records())
    provider = LaunchpadSpansProvider(
        logs, [GROUP, "aws/spans"], records_guard=guard,
        mapper=SimpleNamespace(map_to_session=lambda *args: mapped.append(args)),
    )
    with pytest.raises(NotFoundError):
        provider.get_evaluation_data(SID)
    assert len(logs.started) == 2
    assert mapped == []


def test_lambda_raw_reader_denies_late_foreign_before_cache():
    reader = SessionSpans(ReadingLogs(records(service="foreign")), [GROUP], records_guard=guard)
    with pytest.raises(NotFoundError):
        reader(SID)
    assert reader._cache == {}


def test_lambda_envelope_guards_custom_runtime_documents_before_invoke():
    from strands_evals.types.evaluation import EvaluationData

    invoked = []
    runtime = CodeEvaluatorRuntime(
        lambda_client=SimpleNamespace(invoke=lambda **kwargs: invoked.append(kwargs)),
        session_spans=lambda sid: records(service="foreign"), records_guard=guard,
    )
    evaluator = LambdaEvaluator(lambda_arn="test-function", lambda_client=runtime,
                                timeout_s=1, name="guard-test", level="SESSION")
    with pytest.raises(NotFoundError):
        evaluator.evaluate(EvaluationData(name=SID, input="hello", actual_output="answer"))
    assert invoked == []


def test_engine_preserves_guard_denial_and_never_calls_judge():
    from tests.conftest import ws_ctx
    from tests.evaluation.fakes import FakeJudge
    from tests.test_strands_engine import engine_with, evaluate, rows

    ws = ws_ctx()

    judge = FakeJudge()
    engine = engine_with(logs=ReadingLogs(records() + records(service="foreign")), judge=judge)
    with pytest.raises(NotFoundError):
        evaluate(engine, ws, source="ondemand", persist=False, records_guard=guard)
    assert judge.calls == []
    assert rows() == []


@pytest.mark.parametrize("reader_kind", ["provider", "lambda"])
@pytest.mark.parametrize("message", ["not json", "[]", "null", '"scalar"'])
def test_malformed_row_cannot_be_silently_dropped_from_a_guarded_batch(reader_kind, message):
    valid = [{"@message": json.dumps(row)} for row in records()]
    logs = FakeLogs({"": [*valid, {"@message": message}]})
    if reader_kind == "provider":
        read = LaunchpadSpansProvider(logs, [GROUP], spans_source="runtime_logs",
                                      records_guard=guard).get_evaluation_data
    else:
        read = SessionSpans(logs, [GROUP], records_guard=guard)
    with pytest.raises(NotFoundError):
        read(SID)


def test_authorized_records_keep_identical_extraction():
    plain = LaunchpadSpansProvider(ReadingLogs(records()), [GROUP], spans_source="runtime_logs")
    guarded = LaunchpadSpansProvider(ReadingLogs(records()), [GROUP], spans_source="runtime_logs",
                                    records_guard=guard)
    assert guarded.get_evaluation_data(SID) == plain.get_evaluation_data(SID)


def test_engine_lambda_late_foreign_is_rejected_before_handler(monkeypatch):
    from app.core.config import get_settings
    from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
    from app.evaluation.models import EvalEvaluator
    from tests.conftest import ws_ctx
    from tests.test_strands_engine import engine_with, evaluate

    monkeypatch.setenv("LAUNCHPAD_EVAL_CODE_EVALUATORS_ENABLED", "1")
    monkeypatch.setenv("LAUNCHPAD_OBSERVABILITY_SPANS_SOURCE", "runtime_logs")
    get_settings.cache_clear()
    try:
        with SessionLocal() as db:
            db.add(EvalEvaluator(id="reader_guard_code", workspace_id=DEFAULT_WORKSPACE_ID,
                                 name="guard code", level="SESSION", definition="code",
                                 rating_scale=[],
                                 lambda_arn="arn:aws-cn:lambda:cn-northwest-1:1:function:guard"))
            db.commit()

        class LateForeign(ReadingLogs):
            def start_query(self, **kwargs):
                if self.started:
                    self.batch = records(service="foreign")
                return super().start_query(**kwargs)

        calls = []
        logs = LateForeign(records())
        engine = engine_with(logs=logs,
                             lambda_client=SimpleNamespace(invoke=lambda **kw: calls.append(kw)))
        with pytest.raises(NotFoundError):
            evaluate(engine, ws_ctx(id=DEFAULT_WORKSPACE_ID), source="ondemand", persist=False,
                     evaluator_ids=["reader_guard_code"], records_guard=guard)
        assert len(logs.started) == 2
        assert calls == []
    finally:
        get_settings.cache_clear()


def test_background_lambda_reader_retains_tolerant_null_field_handling():
    class NullFieldLogs(ReadingLogs):
        def get_query_results(self, queryId):  # noqa: N803 — boto3 contract
            result = super().get_query_results(queryId)
            result["results"].append([{"field": "@message", "value": None}])
            return result

    logs = NullFieldLogs(records())
    assert SessionSpans(logs, [GROUP])(SID)
    with pytest.raises(NotFoundError):
        SessionSpans(logs, [GROUP], records_guard=guard)(SID)
