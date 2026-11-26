"""Simulated persona scenarios — SDK executor adapter + dataset plumbing."""

import pytest

from app.evaluation import simulation
from app.evaluation.routers import _infer_kind, _validate_items
from app.evaluation.scenarios import (
    aws_examples,
    dataset_kind,
    ground_truth_metadata,
    normalize_scenarios,
    scenario_kind,
    simulates,
)
from tests.evaluation.test_datasets_v2 import PERSONA, SCENARIO


# ─── adapter: our invokers drive the SDK executor ────────────────────────────
class FakeResult:
    def __init__(self, status="COMPLETED", error=None):
        self.status = status
        self.error = error


def stub_executor(monkeypatch, *, turns=2, status="COMPLETED", captured=None):
    """Replace the SDK executor with one that calls the invoker ``turns`` times
    (framework session id stays stable across turns, like the real loop)."""
    from bedrock_agentcore.evaluation import AgentInvokerInput

    class FakeExecutor:
        def __init__(self, *, agent_invoker, simulation_config):
            if captured is not None:
                captured["config"] = simulation_config
            self.agent_invoker = agent_invoker

        def run_scenario(self, scenario):
            if captured is not None:
                captured["scenario"] = scenario
            try:
                for turn in range(turns):
                    self.agent_invoker(AgentInvokerInput(
                        payload=f"turn-{turn}", session_id="framework-key"))
            except Exception as exc:  # the real executor swallows into the result too
                return FakeResult(status="FAILED", error=str(exc))
            return FakeResult(status=status,
                              error="boom" if status == "FAILED" else None)

    monkeypatch.setattr(simulation, "SimulatedScenarioExecutor", FakeExecutor)


def test_adapter_threads_runtime_session_and_maps_fields(monkeypatch):
    captured: dict = {}
    stub_executor(monkeypatch, turns=3, captured=captured)
    calls: list[tuple[str | None, str]] = []

    def invoke(
        client, arn, prompt, session_id=None, actor_id="default", runtime_user_id=None
    ):
        calls.append((session_id, prompt))
        return {"text": "ok", "session_id": "runtime-sess-" + "x" * 30}

    monkeypatch.setattr(simulation.rt, "invoke_runtime_text", invoke)

    sid = simulation.run_simulated_scenario(
        object(), agent_arn="arn:x", method="zip_runtime", scenario=PERSONA,
        actor_model_id="global.anthropic.claude-haiku-4-5-20251001-v1:0",
    )
    assert sid == "runtime-sess-" + "x" * 30
    # first turn opens the session, later turns reuse the RUNTIME session id
    assert calls[0][0] is None
    assert calls[1][0] == sid and calls[2][0] == sid

    sim = captured["scenario"]
    assert sim.scenario_id == "frustrated-employee-leave"
    assert sim.actor_profile.goal == "Get a PTO request submitted and confirmed"
    assert sim.max_turns == 8
    assert sim.assertions == ["Agent submits a PTO request"]
    assert captured["config"].model_id == (
        "global.anthropic.claude-haiku-4-5-20251001-v1:0"
    )


def test_adapter_dispatches_harness(monkeypatch):
    stub_executor(monkeypatch, turns=1)
    actors: list[str] = []

    def invoke(client, arn, prompt, session_id=None, actor_id="default"):
        actors.append(actor_id)
        return {"text": "ok", "session_id": "harness-sess-" + "x" * 30}

    monkeypatch.setattr(simulation.hc, "invoke_harness_text", invoke)
    sid = simulation.run_simulated_scenario(
        object(), agent_arn="arn:h", method="harness", scenario=PERSONA,
        actor_model_id="m", actor_id="agent1__eval-run1-0",
    )
    assert sid.startswith("harness-sess-")
    # the persona's turns run under the scenario's own memory actor
    assert actors == ["agent1__eval-run1-0"]


def test_adapter_raises_on_failed_scenario(monkeypatch):
    stub_executor(monkeypatch, turns=1, status="FAILED")
    monkeypatch.setattr(
        simulation.rt, "invoke_runtime_text",
        lambda *a, **k: {"text": "ok", "session_id": "s" * 33},
    )
    with pytest.raises(RuntimeError, match="boom"):
        simulation.run_simulated_scenario(
            object(), agent_arn="arn:x", method="zip_runtime", scenario=PERSONA,
            actor_model_id="m",
        )


def test_adapter_reraises_the_agent_calls_own_error(monkeypatch):
    """The executor keeps only str(exc); the adapter hands the caller the agent
    call's real exception so its code drives the retry / skip decision."""
    from app.core.errors import AppError

    stub_executor(monkeypatch, turns=2)
    timeout = AppError("harness.execution_timeout", "Harness execution timed out")

    def invoke(client, arn, prompt, session_id=None, actor_id="default"):
        if session_id:  # the second turn stalls
            raise timeout
        return {"text": "ok", "session_id": "harness-sess-" + "x" * 30}

    monkeypatch.setattr(simulation.hc, "invoke_harness_text", invoke)
    with pytest.raises(AppError) as err:
        simulation.run_simulated_scenario(
            object(), agent_arn="arn:h", method="harness", scenario=PERSONA,
            actor_model_id="m",
        )
    assert err.value is timeout


def test_adapter_requires_actor_model():
    with pytest.raises(RuntimeError, match="actor_model_id"):
        simulation.run_simulated_scenario(
            object(), agent_arn="arn:x", method="zip_runtime", scenario=PERSONA,
            actor_model_id="",
        )


# ─── dataset plumbing for persona items ──────────────────────────────────────
def test_persona_items_validate_and_infer_kind():
    _validate_items([PERSONA])  # no raise
    assert _infer_kind([PERSONA]) == "simulated"
    assert _infer_kind([SCENARIO, PERSONA]) == "simulated"
    assert simulation.is_simulated(PERSONA) and not simulation.is_simulated(SCENARIO)


def test_persona_items_rejected_without_goal_or_input():
    from app.core.errors import AppError
    bad_cases = [
        {**PERSONA, "input": ""},
        {**PERSONA, "actor_profile": {"context": "x"}},  # no goal
        {**PERSONA, "scenario_id": ""},
    ]
    for bad in bad_cases:
        with pytest.raises(AppError):
            _validate_items([bad])


def test_scenario_kind_follows_the_aws_schema():
    """AWS datasets declare no schema type: `turns` makes a fixed scenario even
    beside an actor_profile; no `turns` with input/actor_profile is dynamic."""
    both = {**SCENARIO, "actor_profile": PERSONA["actor_profile"]}
    assert scenario_kind(SCENARIO) == "fixed" and not simulates(SCENARIO)
    assert scenario_kind(both) == "fixed" and not simulates(both)
    assert not simulation.is_simulated(both)
    assert scenario_kind(PERSONA) == "dynamic" and simulates(PERSONA)
    assert scenario_kind({"scenario_id": "q", "input": "hi"}) == "dynamic"
    assert not simulates({"scenario_id": "q", "input": "hi"})
    assert scenario_kind({"prompt": "hi"}) == "legacy"
    _validate_items([both])  # fixed + a valid profile passes both rule sets
    assert _infer_kind([both]) == "predefined"


def test_input_only_dynamic_scenario_replays_as_one_turn():
    item = {"scenario_id": "one-shot", "input": "What is PTO?", "max_turns": 3,
            "assertions": ["explains PTO"]}
    _validate_items([item])
    assert _infer_kind([item]) == "predefined"
    assert normalize_scenarios([item]) == [{
        "scenario_id": "one-shot", "assertions": ["explains PTO"],
        "turns": [{"input": "What is PTO?"}],
    }]


def test_legacy_prompt_item_with_a_stray_input_stays_legacy():
    item = {"prompt": "2+2?", "expected": "4", "input": "ignored"}
    assert scenario_kind(item) == "legacy"
    _validate_items([item])  # no scenario_id needed, as before
    assert normalize_scenarios([item])[0]["turns"] == [
        {"input": "2+2?", "expected_response": "4"}
    ]


def test_traffic_prompts_read_input_only_dynamic_items():
    from types import SimpleNamespace

    from app.optimization.service import resolve_traffic_items
    ds = SimpleNamespace(kind="predefined", items=[
        {"scenario_id": "one-shot", "input": "What is PTO?"}, SCENARIO,
    ])
    assert resolve_traffic_items(ds) == [
        ("one-shot", "What is PTO?"), ("refund_flow", "I want a refund"),
    ]


def test_aws_examples_send_scenarios_verbatim_and_convert_only_legacy():
    """sync-to-aws sends what AWS stores: an input-only dynamic item (dataset
    generation writes them for single-turn sessions) keeps its shape and metadata,
    so AWS's content dedupe still recognises it; only prompt items are converted."""
    generated = {"scenario_id": "g1", "input": "hi",
                 "metadata": {"generation_id": "gen-1", "source": {"session_id": "s1"}}}
    legacy = {"prompt": "2+2?", "expected": "4"}
    assert aws_examples([generated, PERSONA, SCENARIO, legacy]) == [
        generated, PERSONA, SCENARIO,
        {"scenario_id": "item_4", "turns": [{"input": "2+2?", "expected_response": "4"}]},
    ]


def test_dynamic_items_rejected_without_a_usable_shape():
    from app.core.errors import AppError
    bad_cases = [
        {"scenario_id": "x", "input": "   "},  # dynamic without an opening message
        # AWS accepts a profile without input; the local SDK actor needs one
        {"scenario_id": "x", "actor_profile": {"context": "c", "goal": "g"}},
        {**SCENARIO, "actor_profile": {"context": "c"}},  # fixed + invalid profile
        {"scenario_id": "x"},  # none of turns / input / actor_profile / prompt
    ]
    for bad in bad_cases:
        with pytest.raises(AppError):
            _validate_items([bad])


def test_dataset_kind_from_examples():
    assert dataset_kind([]) == "empty"
    assert dataset_kind([SCENARIO, {"prompt": "x"}]) == "predefined"
    assert dataset_kind([PERSONA]) == "simulated"
    assert dataset_kind([SCENARIO, PERSONA]) == "mixed"


def test_persona_normalize_passthrough_and_ground_truth():
    scenarios = normalize_scenarios([PERSONA])
    assert scenarios == [PERSONA]
    meta = ground_truth_metadata(scenarios, ["sess-1"])
    assert meta[0]["testScenarioId"] == "frustrated-employee-leave"
    assert meta[0]["groundTruth"]["inline"] == {
        "assertions": [{"text": "Agent submits a PTO request"}]
    }


# ─── execute_run: a failed persona is skipped, not fatal ─────────────────────
def _personas(*ids):
    return [{**PERSONA, "scenario_id": sid} for sid in ids]


def _execute(monkeypatch, outcomes, items):
    """Run ``items`` through execute_run with ``run_simulated_scenario`` replaced:
    ``outcomes[scenario_id]`` is a list consumed per attempt (a session id, or an
    exception to raise)."""
    from unittest.mock import MagicMock

    from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
    from app.evaluation import service as evaluation
    from app.evaluation.models import EvalRun
    from tests.conftest import ws_ctx

    attempts: list[str] = []

    def fake_run(_data, *, scenario, **_kw):
        attempts.append(scenario["scenario_id"])
        outcome = outcomes[scenario["scenario_id"]].pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    client = MagicMock()
    client.start_batch_evaluation.return_value = {"batchEvaluationId": "batch-1"}
    monkeypatch.setattr(evaluation, "data_client", lambda _ws: client)
    monkeypatch.setattr(evaluation.simulation, "run_simulated_scenario", fake_run)
    monkeypatch.setattr(evaluation, "_wait_for_fresh_telemetry", lambda **_kw: None)
    monkeypatch.setattr(evaluation, "_poll_batch", lambda *_a, **_kw: {})
    monkeypatch.setattr(evaluation, "_finish_from_result", lambda *_a, **_kw: None)
    with SessionLocal() as db:
        row = EvalRun(workspace_id=DEFAULT_WORKSPACE_ID, agent_id="agent-harness",
                      agent_name="test-harness", status="queued", evaluators=[])
        db.add(row)
        db.commit()
        run_id = row.id
    evaluation.execute_run(
        run_id, workspace=ws_ctx(), agent_arn="arn:h", method="harness",
        service_name="harness_test.DEFAULT", log_group="/test", items=items,
        evaluators=["Builtin.GoalSuccessRate"], mode="evaluators", wait_seconds=0,
        actor_model_id="m",
    )
    batch = client.start_batch_evaluation.call_args
    with SessionLocal() as db:
        return db.get(EvalRun, run_id), attempts, batch


def test_a_stalled_persona_is_retried_once_then_skipped_and_the_run_goes_on(monkeypatch):
    from app.core.errors import AppError

    def stall():
        return AppError("harness.execution_timeout", "Harness execution timed out")

    row, attempts, batch = _execute(
        monkeypatch, {"p1": [stall(), stall()], "p2": ["sess-p2-" + "x" * 30]},
        _personas("p1", "p2"),
    )
    assert attempts == ["p1", "p1", "p2"]  # one timeout retry, then skipped
    assert row.session_ids == ["sess-p2-" + "x" * 30] and row.batch_eval_id == "batch-1"
    assert row.error is None and batch is not None  # the surviving session is scored
    assert row.scenario_failures == [{
        "scenario_id": "p1", "retries": "1", "code": "harness.execution_timeout",
        "error": "harness.execution_timeout: Harness execution timed out",
    }]


def test_a_final_persona_error_is_skipped_without_retry(monkeypatch):
    row, attempts, _batch = _execute(
        monkeypatch,
        {"p1": ["sess-p1-" + "x" * 30], "p2": [RuntimeError("actor model refused")]},
        _personas("p1", "p2"),
    )
    assert attempts == ["p1", "p2"]
    assert row.session_ids == ["sess-p1-" + "x" * 30]
    assert row.scenario_failures == [{"scenario_id": "p2", "retries": "0", "code": "RuntimeError",
                                      "error": "RuntimeError: actor model refused"}]


def test_the_run_fails_only_when_every_persona_failed(monkeypatch):
    row, _attempts, batch = _execute(
        monkeypatch, {"p1": [RuntimeError("a")], "p2": [RuntimeError("b")]},
        _personas("p1", "p2"),
    )
    assert row.status == "failed" and batch is None
    assert "all 2 scenarios failed" in row.error and "p1: RuntimeError: a" in row.error
    assert [f["scenario_id"] for f in row.scenario_failures] == ["p1", "p2"]


def test_an_ordinary_run_records_no_scenario_failures(monkeypatch):
    row, _attempts, _batch = _execute(
        monkeypatch, {"p1": ["sess-p1-" + "x" * 30]}, _personas("p1"))
    assert row.scenario_failures is None and row.session_ids == ["sess-p1-" + "x" * 30]
