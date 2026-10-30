"""Persona scenarios on ``strands_evals.simulation.ActorSimulator`` (plan §B.2, W-E1-6).

``from_case_for_user_simulator`` is deliberately not used: it generates the actor profile
with a default ``Agent()`` (Claude, no bearer). The profile comes from the dataset item and
the model is the injected judge/actor model object.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from strands.models import Model
from strands_evals.simulation import ActorSimulator
from strands_evals.types.simulation import ActorProfile

# (prompt, session_id) -> {text, session_id}
InvokeText = Callable[[str, str | None], dict[str, Any]]


def build_actor(
    profile: dict[str, Any], initial_query: str, *, model: Any, max_turns: int = 10
) -> ActorSimulator:
    if not isinstance(model, Model):
        raise ValueError(
            "the actor simulator needs a model object (aws_clients.global_bedrock_model)"
        )
    actor_profile = ActorProfile(
        traits=dict(profile.get("traits") or {}),
        context=str(profile.get("context") or ""),
        actor_goal=str(profile.get("actor_goal") or profile.get("goal") or ""),
    )
    return ActorSimulator(actor_profile=actor_profile, initial_query=initial_query,
                          model=model, max_turns=max_turns)


def drive(actor: ActorSimulator, invoke: InvokeText) -> str | None:
    """Run the actor ↔ agent loop; returns the agent's runtime session id."""
    session_id: str | None = None
    message = actor.initial_query
    while True:
        reply = invoke(message, session_id)
        session_id = reply.get("session_id") or session_id
        if not actor.has_next():
            return session_id
        result = actor.act(str(reply.get("text") or ""))
        response = getattr(result, "structured_output", None)
        stop = bool(getattr(response, "stop", False))
        message = str(getattr(response, "message", "") or "")
        if stop or not message:
            return session_id
