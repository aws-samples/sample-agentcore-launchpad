"""N12 E0-5 — the evaluation engine slot (same pattern as memory_backend)."""

import pytest

from app.core.errors import AppError
from app.evaluation import engine
from tests.evaluation.fakes import FakeEngine


@pytest.fixture(autouse=True)
def _reset():
    engine.set_engine(None)
    yield
    engine.set_engine(None)


def test_unregistered_engine_is_a_503_feature_unavailable():
    assert engine.engine_or_none() is None
    with pytest.raises(AppError) as exc:
        engine.get_engine()
    assert exc.value.code == "feature.unavailable" and exc.value.status_code == 503
    assert exc.value.detail == {"feature": "evaluation"}


def test_registered_engine_is_served_and_satisfies_the_protocol(workspace):
    fake = FakeEngine(scores={"Builtin.Helpfulness": 0.75})
    engine.set_engine(fake)
    assert engine.get_engine() is fake
    records = engine.get_engine().evaluate_sessions(
        workspace, source="run", owner_id="run-1", session_ids=["s1", "s2"],
        log_groups=["/aws/bedrock-agentcore/runtimes/x-DEFAULT"],
        evaluator_ids=["Builtin.Helpfulness"], ground_truth=None, judge_model_id=None,
        stop=None, progress=None,
    )
    assert len(records) == 2
    assert set(records[0]) == set(engine.EvalResultRecord.__annotations__)
    assert records[0]["score"] == 0.75 and records[0]["evaluator_id"] == "Builtin.Helpfulness"
    assert fake.calls[0]["source"] == "run"


def test_record_shape_is_the_contract_record():
    assert set(engine.EvalResultRecord.__annotations__) == {
        "session_id", "trace_id", "evaluator_id", "level", "score", "label", "explanation",
        "error_type", "error_message",
    }
