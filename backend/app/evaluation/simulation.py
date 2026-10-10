"""Simulated-persona scenario execution (devguide "Simulated scenarios").

Wraps the bedrock-agentcore SDK's ``SimulatedScenarioExecutor`` (preview): an
LLM actor — configured per run via ``actor_model_id`` — plays the user against
the agent until its goal is met or ``max_turns`` is hit. The executor's
framework session id is only a conversation key; the agent is invoked through
the platform's own invokers so the RUNTIME session id (what telemetry carries,
and what StartBatchEvaluation must be scoped to) is the one recorded.
"""

from __future__ import annotations

from typing import Any

from bedrock_agentcore.evaluation import (
    ActorProfile,
    AgentInvokerInput,
    AgentInvokerOutput,
    SimulatedScenario,
    SimulatedScenarioExecutor,
    SimulationConfig,
)

from app.services.agentcore import harness as hc
from app.services.agentcore import runtime as rt


def is_simulated(scenario: dict[str, Any]) -> bool:
    return "actor_profile" in scenario


def run_simulated_scenario(
    data_client: Any,
    *,
    agent_arn: str,
    method: str,
    scenario: dict[str, Any],
    actor_model_id: str,
    protocol: str = "http",
    runtime_user_id: str | None = None,
    invoke_text: Any = None,
) -> str:
    """Drive one persona scenario to completion; returns the runtime session id.

    The executor swallows every exception into its result, so when it reports
    FAILED the agent call's own exception (e.g. AppError
    ``harness.execution_timeout``) is re-raised as is — the caller's transient
    retry and its error codes see the real failure. A failure that never came
    from the agent call (the actor model, the executor) raises RuntimeError.
    ``invoke_text(prompt, session_id) -> {text, session_id}`` overrides the
    default SigV4 runtime call for HTTP runtimes — the JWT-inbound bearer path
    rides through it (harness/A2A keep their own dispatch).
    """
    if not actor_model_id:
        raise RuntimeError(
            "simulated persona scenarios need an actor_model_id (the Bedrock "
            "model that plays the user)"
        )
    state: dict[str, Any] = {"session_id": None, "error": None}

    def invoker(inp: AgentInvokerInput) -> AgentInvokerOutput:
        try:
            return _invoke(inp)
        except Exception as exc:
            state["error"] = exc  # the executor keeps only str(exc)
            raise

    def _invoke(inp: AgentInvokerInput) -> AgentInvokerOutput:
        prompt = inp.payload if isinstance(inp.payload, str) else str(inp.payload)
        if method == "harness":
            result = hc.invoke_harness_text(
                data_client, agent_arn, prompt, session_id=state["session_id"]
            )
        elif protocol == "a2a":  # JSON-RPC runtimes reject the {prompt} payload
            result = rt.invoke_a2a_text(
                data_client, agent_arn, prompt, session_id=state["session_id"]
            )
        elif invoke_text is not None:
            result = invoke_text(prompt, state["session_id"])
        else:
            result = rt.invoke_runtime_text(
                data_client,
                agent_arn,
                prompt,
                session_id=state["session_id"],
                runtime_user_id=runtime_user_id,
            )
        state["session_id"] = result["session_id"]
        return AgentInvokerOutput(agent_output=result["text"])

    profile = scenario.get("actor_profile") or {}
    executor = SimulatedScenarioExecutor(
        agent_invoker=invoker,
        simulation_config=SimulationConfig(model_id=actor_model_id),
    )
    result = executor.run_scenario(
        SimulatedScenario(
            scenario_id=scenario["scenario_id"],
            scenario_description=scenario.get("scenario_description", ""),
            actor_profile=ActorProfile(
                traits=profile.get("traits") or {},
                context=profile.get("context", ""),
                goal=profile.get("goal", ""),
            ),
            input=scenario.get("input", ""),
            max_turns=int(scenario.get("max_turns") or 10),
            assertions=scenario.get("assertions"),
        )
    )
    if result.status != "COMPLETED" or not state["session_id"]:
        if state["error"] is not None:
            raise state["error"]
        raise RuntimeError(
            f"simulated scenario '{scenario['scenario_id']}' failed: "
            f"{result.error or 'agent was never invoked'}"
        )
    return state["session_id"]
