"""Simulated persona scenarios (E1-6): the worker drives them on
``strands_evals.simulation.ActorSimulator`` through ``strands_eval/simulator`` — the SDK's
``SimulatedScenarioExecutor`` wrapper (``evaluation/simulation.py``) is gone — plus the
dataset plumbing for persona items and the actor-model catalog check."""

from pathlib import Path

import pytest

from app.core.errors import AppError
from app.evaluation.routers import _check_actor_model, _infer_kind, _validate_items
from app.evaluation.scenarios import ground_truth_metadata, normalize_scenarios
from app.services.strands_eval import simulator
from tests.evaluation.fakes import FakeModel
from tests.evaluation.test_datasets_v2 import PERSONA, SCENARIO

APP = Path(__file__).resolve().parents[2] / "app"


# ─── the actor: profile from the item, model injected ────────────────────────
def test_build_actor_maps_the_item_profile_onto_the_strands_profile():
    actor = simulator.build_actor(PERSONA["actor_profile"], PERSONA["input"],
                                  model=FakeModel(), max_turns=8)
    assert actor.initial_query == PERSONA["input"]
    assert actor._max_turns == 8  # the SDK keeps it private; the injected model is the actor's
    assert isinstance(actor.model_id, FakeModel)
    assert actor.actor_profile.actor_goal == PERSONA["actor_profile"]["goal"]
    assert actor.actor_profile.context == PERSONA["actor_profile"]["context"]
    assert actor.actor_profile.traits == PERSONA["actor_profile"]["traits"]


def test_build_actor_refuses_a_model_id_string():
    """A str would make ActorSimulator build a default Claude ``BedrockModel`` (R-3)."""
    with pytest.raises(ValueError, match="global_bedrock_model"):
        simulator.build_actor(PERSONA["actor_profile"], "hi", model="global.zai.glm-5.3")


def test_drive_threads_the_runtime_session_until_the_actor_stops():
    class Turn:
        def __init__(self, message, stop):
            self.structured_output = type("R", (), {"message": message, "stop": stop})()

    class Actor:
        initial_query = "turn-0"

        def __init__(self):
            self.seen = []
            self.turns = [Turn("turn-1", False), Turn("turn-2", False), Turn("", True)]

        def has_next(self):
            return bool(self.turns)

        def act(self, text):
            self.seen.append(text)
            return self.turns.pop(0)

    calls: list[tuple[str, str | None]] = []

    def invoke(prompt, session_id):
        calls.append((prompt, session_id))
        return {"text": f"reply to {prompt}", "session_id": "runtime-sess-" + "x" * 30}

    actor = Actor()
    sid = simulator.drive(actor, invoke)
    assert sid == "runtime-sess-" + "x" * 30
    # the first turn opens the session; later turns reuse the RUNTIME session id
    assert [c[1] for c in calls] == [None] + [sid] * 2
    assert [c[0] for c in calls] == ["turn-0", "turn-1", "turn-2"]
    assert actor.seen == ["reply to turn-0", "reply to turn-1", "reply to turn-2"]


def test_drive_stops_when_the_actor_has_no_next_turn():
    class Actor:
        initial_query = "only"

        def has_next(self):
            return False

    calls = []
    sid = simulator.drive(Actor(), lambda p, s: calls.append(p) or {"text": "ok",
                                                                    "session_id": "s" * 33})
    assert sid == "s" * 33 and calls == ["only"]


def test_the_sdk_executor_and_the_default_profile_generator_are_gone():
    """E1-6 acceptance: no ``SimulatedScenarioExecutor`` wrapper and no
    ``from_case_for_user_simulator`` (it builds a default Claude ``Agent()``)."""
    assert not (APP / "evaluation" / "simulation.py").exists()
    offenders = [
        str(path.relative_to(APP))
        for path in APP.rglob("*.py")
        if "from_case_for_user_simulator(" in path.read_text(encoding="utf-8")
        or "SimulatedScenarioExecutor" in path.read_text(encoding="utf-8")
    ]  # simulator.py's docstring names the banned factory without calling it
    assert offenders == []


# ─── the run request: actor model required and catalog-checked ───────────────
def test_persona_runs_need_an_actor_model_from_the_catalog():
    with pytest.raises(AppError) as exc:
        _check_actor_model([PERSONA], None)
    assert exc.value.code == "run.actor_model_required" and exc.value.status_code == 422
    with pytest.raises(AppError) as exc:
        _check_actor_model([PERSONA], "global.anthropic.claude-haiku-4-5-20251001-v1:0")
    assert exc.value.code == "model.not_allowed" and exc.value.status_code == 422
    _check_actor_model([PERSONA], "global.zai.glm-5.3")  # catalog id: no raise
    _check_actor_model([SCENARIO], None)  # no persona item: nothing required


# ─── dataset plumbing for persona items ──────────────────────────────────────
def test_persona_items_validate_and_infer_kind():
    _validate_items([PERSONA])  # no raise
    assert _infer_kind([PERSONA]) == "simulated"
    assert _infer_kind([SCENARIO, PERSONA]) == "simulated"


def test_persona_items_rejected_without_goal_or_input():
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
