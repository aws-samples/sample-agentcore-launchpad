"""Runtime-canary subject eligibility (N12-obs34; canary-owned since N13-obs2).

The backend owns the verdict: ``GET /api/agents`` projects it as ``canary_capability`` and
``canary_routers._eligible_agent`` refuses with the same function, so the console's
disabled dropdown rows and the router always agree. The configuration-experiment twin
(``experiment_capability`` / ``config_bundle_support``) retired with its routes (N13-obs2).
"""

from typing import Any

from app.deployer.pipeline import method_capabilities


def canary_capability(agent_row: Any) -> dict[str, Any]:
    """Backend-owned Runtime-canary subject capability projection.

    A canary mints a candidate version of ONE agent from an edited spec and splits
    traffic between two named endpoints (``optimization.routing``), so the subject must
    be deployed as an AgentCore Runtime with immutable versions
    (``MethodCapabilities.runtime_versions``) whose artifact the platform can rebuild
    from a spec (``candidate_mint``) — ``zip_runtime``, ``studio`` and the self-built
    ``harness`` (N12-obs34: it IS a Runtime in CN; the Global managed-Harness canary is
    the thing that does not exist here). BYOC is versioned but member-owned, so no
    candidate can be minted; A2A servers do not take HTTP canary traffic.
    """
    base = {"eligible": False, "reason": None, "reason_code": None}
    if getattr(agent_row, "system_key", None):
        return {
            **base,
            "reason_code": "system-managed",
            "reason": "System-managed presets cannot be the subject of a canary.",
        }
    if agent_row.status != "active":
        return {
            **base,
            "reason_code": "not-active",
            "reason": "Canary agent must be active.",
        }
    caps = method_capabilities(getattr(agent_row, "method", None))
    if not caps.runtime_versions:
        return {
            **base,
            "reason_code": "not-runtime",
            "reason": "Target canaries require an AgentCore Runtime agent.",
        }
    if not caps.candidate_mint:
        # candidate minting rebuilds the artifact from an edited spec, which the
        # platform cannot do for user-owned source (BYOC)
        return {
            **base,
            "reason_code": "custom-source-unverified",
            "reason": "BYOC candidates cannot be minted from an edited spec.",
        }
    if (agent_row.spec or {}).get("protocol", "http") != "http":
        return {
            **base,
            "reason_code": "a2a",
            "reason": "A2A agents are not compatible with HTTP target-canary traffic.",
        }
    if ":runtime/" not in str(agent_row.arn or ""):
        return {
            **base,
            "reason_code": "no-runtime-arn",
            "reason": "The agent has no deployed AgentCore Runtime ARN.",
        }
    return {**base, "eligible": True}
