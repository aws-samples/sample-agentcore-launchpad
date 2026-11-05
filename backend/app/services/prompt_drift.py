"""Prompt drift: has the agent's system prompt been changed outside Launchpad?

The console shows ``Agent.spec.system_prompt`` — the prompt the last Launchpad deploy
published. An edit in the AWS console (``UpdateHarness``) publishes a new Harness
version the ledger does not know about: chat then answers with the new prompt while
the page shows the old one, and the next Launchpad re-publish silently puts the old
one back. This module reads the Harness's latest version back and compares.

* Only a Launchpad-built Harness (``method == "harness"``) is compared. A Runtime's
  prompt is compiled into its code (``SYSTEM_PROMPT`` in the generated ``main.py``),
  so it cannot be read back; an imported Harness has no Launchpad spec to compare to.
* The deployed ``systemPrompt`` carries the sections the deployer appends (knowledge
  bases, storage mounts, the context layer); ``strip_generated_prompt`` removes them
  so only the operator-owned part is compared — and adopted.
* Adopting is an ordinary re-publish (``republish_agent``) with the AWS prompt: the
  same guards, permission and Agent-DLC gate as a redeploy. Only the prompt is
  adopted; other members the console changed (``other_differences``) are put back to
  the ledger's values by that re-publish, and the read says so before it happens.
"""

from typing import Any

from app.core.errors import AppError
from app.deployer.harness import model_config, strip_generated_prompt
from app.models.ledger import Agent
from app.schemas.agent import AgentSpec
from app.services.agent_versions import require_resource
from app.services.agentcore import harness as harness_api

IN_SYNC, DRIFTED, UNSUPPORTED = "in_sync", "drifted", "unsupported"


def _prompt_text(harness: dict[str, Any]) -> str:
    blocks = harness.get("systemPrompt") or []
    if isinstance(blocks, str):
        return blocks
    return "".join(str(b.get("text") or "") for b in blocks if isinstance(b, dict))


def _other_differences(spec: AgentSpec, harness: dict[str, Any]) -> list[dict[str, Any]]:
    """Members besides the prompt the AWS console may have changed too, which an
    adopt (a re-publish of the ledger spec) would put back."""
    live_model = ((harness.get("model") or {}).get("bedrockModelConfig") or {})
    expected = model_config(spec)
    out = []
    for member, ledger, aws in (
        ("model_id", expected.get("modelId"), live_model.get("modelId")),
        ("max_tokens", expected.get("maxTokens"), live_model.get("maxTokens")),
        ("max_iterations", spec.max_iterations, harness.get("maxIterations")),
        ("timeout_seconds", spec.timeout_seconds, harness.get("timeoutSeconds")),
    ):
        if aws is not None and ledger != aws:
            out.append({"member": member, "ledger": ledger, "aws": aws})
    return out


def read(control: Any, agent: Agent) -> dict[str, Any]:
    """The ledger prompt beside the prompt of the Harness's latest AWS version."""
    kind, resource_id = require_resource(agent)
    base = {"agent_id": agent.id, "kind": kind, "ledger_version": agent.version,
            "ledger_prompt": (agent.spec or {}).get("system_prompt") or ""}
    if kind != "harness" or agent.method != "harness":
        reason = ("runtime" if kind == "runtime" else "imported")
        return {**base, "status": UNSUPPORTED, "reason": reason}
    spec = AgentSpec(**(agent.spec or {}))
    harness = harness_api.get_harness(control, resource_id)
    aws_prompt = strip_generated_prompt(spec, _prompt_text(harness))
    same = aws_prompt.rstrip() == spec.system_prompt.rstrip()
    return {
        **base,
        "ledger_prompt": spec.system_prompt,
        "status": IN_SYNC if same else DRIFTED,
        "aws_prompt": aws_prompt,
        "aws_version": str(harness.get("harnessVersion") or "") or None,
        "aws_updated_at": str(harness.get("updatedAt") or "") or None,
        # a gated agent's production traffic is pinned to `live`: an AWS console
        # edit lands on DEFAULT only, so production still answers with the old prompt
        "endpoint_mode": agent.endpoint_mode,
        "other_differences": [] if same else _other_differences(spec, harness),
    }


def adopt_spec(control: Any, agent: Agent, aws_version: str) -> tuple[AgentSpec, dict]:
    """The ledger spec with the AWS prompt, for a re-publish. Refused when the read
    the operator reviewed is stale or there is nothing to adopt."""
    current = read(control, agent)
    if current["status"] == UNSUPPORTED:
        raise AppError("agent.prompt_drift_unsupported",
                       "this agent's prompt cannot be read back from AWS",
                       {"reason": current["reason"]}, status_code=409)
    if current["aws_version"] != str(aws_version):
        raise AppError("agent.prompt_drift_stale",
                       f"AWS now serves version {current['aws_version']}, not "
                       f"{aws_version}: review the prompt again",
                       {"aws_version": current["aws_version"]}, status_code=409)
    if current["status"] == IN_SYNC:
        raise AppError("agent.prompt_in_sync",
                       "the ledger prompt already matches AWS", status_code=409)
    spec = AgentSpec(**(agent.spec or {}))
    adopted = spec.model_copy(update={"system_prompt": current["aws_prompt"]})
    return AgentSpec(**adopted.model_dump()), current
