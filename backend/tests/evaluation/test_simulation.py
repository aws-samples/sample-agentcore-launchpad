"""Simulated persona scenarios — SDK executor adapter + dataset plumbing."""

import pytest

from app.evaluation import simulation
from app.evaluation.routers import _infer_kind, _validate_items
from app.evaluation.scenarios import ground_truth_metadata, normalize_scenarios
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
            for turn in range(turns):
                self.agent_invoker(AgentInvokerInput(
                    payload=f"turn-{turn}", session_id="framework-key"))
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
    monkeypatch.setattr(
        simulation.hc, "invoke_harness_text",
        lambda client, arn, prompt, session_id=None, actor_id="default": {
            "text": "ok", "session_id": "harness-sess-" + "x" * 30},
    )
    sid = simulation.run_simulated_scenario(
        object(), agent_arn="arn:h", method="harness", scenario=PERSONA,
        actor_model_id="m",
    )
    assert sid.startswith("harness-sess-")


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


def test_persona_normalize_passthrough_and_ground_truth():
    scenarios = normalize_scenarios([PERSONA])
    assert scenarios == [PERSONA]
    meta = ground_truth_metadata(scenarios, ["sess-1"])
    assert meta[0]["testScenarioId"] == "frustrated-employee-leave"
    assert meta[0]["groundTruth"]["inline"] == {
        "assertions": [{"text": "Agent submits a PTO request"}]
    }
