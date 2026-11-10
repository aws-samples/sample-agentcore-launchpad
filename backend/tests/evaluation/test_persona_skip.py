"""Upstream #251 parity (02607ab4, N15-obs4) on the CN worker: a simulated persona that
fails is a SKIPPED persona, not a failed run.

CN never had upstream's SDK-executor adapter (``simulation.run_simulated_scenario``): the
worker's ``simulator.drive`` calls our invoke directly, so the agent call's own exception
reaches ``_replay_session`` as is and gets the r12-f transient replay (same pre-assigned
session, hub backoff) like a predefined scenario. A persona that still fails after its
attempts is an N-SCN-bug1 errored scenario — the run goes on with the others and fails only
when EVERY scenario failed — and its invocation line / ``done.json`` entry carries
``simulated: true``, which the hub folds into ``eval_runs.scenario_failures``.
"""

# ruff: noqa: F811  — the `harness` fixture is imported by name from test_jobs_replay_failures

from __future__ import annotations

import dataclasses
import types

import pytest

from app.services.strands_eval import jobs_batch
from tests.evaluation.test_jobs_replay_failures import (
    S1,
    S2,
    S3,
    N,
    Sink,
    _job,
    _spec,
    _timeout,
    harness,  # noqa: F401 — registered here
)

PROFILE = {"traits": {"tone": "frustrated but polite"}, "context": "childcare fell through",
           "goal": "Get a PTO request submitted"}


def _persona(session_id: str, scenario_id: str) -> dict:
    return {"session_id": session_id, "scenario_id": scenario_id, "replay": True,
            "actor_profile": dict(PROFILE), "input": "I need time off next week",
            "max_turns": 3, "arm": None, "qualifier": None, "baggage": None,
            "launchpad_meta": None}


def _one_turn_actor(monkeypatch):
    """The actor opens the conversation and has no next turn: ``simulator.drive`` makes
    exactly one agent call per attempt, so the invoke plan drives the outcome."""
    def build_actor(profile, initial_query, *, model, max_turns):
        return types.SimpleNamespace(initial_query=initial_query, has_next=lambda: False)

    monkeypatch.setattr(jobs_batch.simulator, "build_actor", build_actor)


def test_a_persona_whose_agent_call_hiccups_is_replayed_like_a_predefined_scenario(
    harness, monkeypatch
):
    _one_turn_actor(monkeypatch)
    h = harness({S2: [_timeout(), "ok"]})
    sleeps: list[float] = []
    sink = Sink()
    spec = _spec(sessions=[_spec()["sessions"][0], _persona(S2, "p2"), _spec()["sessions"][2]],
                 actor_model_id="actor-model")

    jobs_batch.run_job(_job(spec), sink, dataclasses.replace(
        h.env, sleep=sleeps.append, transient_backoff_s=(5.0, 15.0)))

    # the agent call's own exception reached the replay loop: one backoff, same session
    assert [c[0] for c in h.runtime.calls] == [S1, S2, S2, S3]
    assert abs(sum(sleeps) - 5.0) < 1e-9
    line = sink.invocations[1]
    assert line["status"] == "ok" and line["retries"] == 1 and line["simulated"] is True
    assert h.evaluated == [[S1, S2, S3]]
    assert sink.finished[0] == "COMPLETED" and "replay_failures" not in sink.finished[2]
    assert "simulated" not in sink.invocations[0]  # a predefined line is unchanged


def test_a_persona_that_keeps_failing_is_skipped_and_the_run_goes_on(harness, monkeypatch):
    _one_turn_actor(monkeypatch)
    h = harness({S2: [_timeout()]})
    sink = Sink()
    spec = _spec(sessions=[_spec()["sessions"][0], _persona(S2, "p2"), _spec()["sessions"][2]],
                 actor_model_id="actor-model")

    jobs_batch.run_job(_job(spec), sink, h.env)

    assert [c[0] for c in h.runtime.calls] == [S1] + [S2] * N + [S3]
    skipped = sink.invocations[1]
    assert skipped["status"] == "failed" and skipped["simulated"] is True
    assert skipped["attempts"] == N and skipped["retries"] == N - 1
    # the other scenarios are scored; the run is COMPLETED with the persona listed
    assert h.evaluated == [[S1, S3]]
    assert sink.finished[0] == "COMPLETED"
    [failure] = sink.finished[2]["replay_failures"]
    assert failure["scenario_id"] == "p2" and failure["simulated"] is True
    assert failure["attempts"] == N and failure["code"] == "ReadTimeoutError"
    # the hub folds exactly that entry into `scenario_failures` (upstream's wire shape)
    from app.evaluation.jobs import JobDone
    from app.evaluation.service import skipped_personas

    assert skipped_personas([JobDone(state="COMPLETED", summary=sink.finished[2])]) == [
        {"scenario_id": "p2", "retries": str(N - 1), "code": "ReadTimeoutError",
         "error": f"ReadTimeoutError: {failure['error']}"}]


def test_a_failed_predefined_scenario_is_not_a_skipped_persona(harness, monkeypatch):
    """CN keeps N-SCN-bug1 for predefined scenarios too (more tolerant than upstream, which
    fails the run on a replayed scenario): errored, run goes on — but not listed as a persona."""
    _one_turn_actor(monkeypatch)
    h = harness({S1: [ValueError("bad prompt")]})
    sink = Sink()
    spec = _spec(sessions=[_spec()["sessions"][0], _persona(S2, "p2")], actor_model_id="m")

    jobs_batch.run_job(_job(spec), sink, h.env)

    assert sink.finished[0] == "COMPLETED" and h.evaluated == [[S2]]
    [failure] = sink.finished[2]["replay_failures"]
    assert failure["scenario_id"] == "s1" and "simulated" not in failure  # entry unchanged
    from app.evaluation.jobs import JobDone
    from app.evaluation.service import skipped_personas

    assert skipped_personas([JobDone(state="COMPLETED", summary=sink.finished[2])]) == []


def test_the_run_fails_only_when_every_persona_failed(harness, monkeypatch):
    _one_turn_actor(monkeypatch)
    h = harness({S2: [RuntimeError("actor model refused")], S3: [RuntimeError("b")]})
    sink = Sink()
    spec = _spec(sessions=[_persona(S2, "p2"), _persona(S3, "p3")], actor_model_id="m")

    with pytest.raises(RuntimeError) as info:
        jobs_batch.run_job(_job(spec), sink, h.env)

    # the worker's FAILED reason names the first skipped persona (the executor writes it
    # to done.json; the hub settles the row `failed` with it — N-SCN-bug1 unchanged)
    assert str(info.value).startswith("all 2 scenarios failed to replay — first error: "
                                      "scenario p2 failed after 1/")
    assert [r["simulated"] for r in sink.invocations] == [True, True]
    assert h.evaluated == []
