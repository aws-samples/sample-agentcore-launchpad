"""Agent-specific Runtime configuration resolution, shared by deployers and canary.

AWS preserves an omitted platformVersion but REPLACES resourceConfigurations on
update. Unconfigured legacy agents must echo live resources; only explicit
consumption may deliberately drop a committed baseline. No worker defaults live here.
"""

from copy import deepcopy
from typing import Any

from app.schemas.agent import AgentSpec
from app.schemas.runtime_config import RuntimeConfig
from app.services.agentcore import runtime as rt


def carry_runtime_config(spec: dict[str, Any], previous: dict[str, Any]) -> dict[str, Any]:
    """Full replacement by an old client/bundle keeps an explicit target choice.

    Absence/null is no intent; an explicit consumption object always wins. This
    carries ledger intent only, never inventing settings for a legacy agent.
    """
    result = deepcopy(spec)
    if result.get("runtime_config") is None and previous.get("runtime_config") is not None:
        result["runtime_config"] = deepcopy(previous["runtime_config"])
    return result


def captured_runtime_config(detail: dict[str, Any]) -> RuntimeConfig:
    """Capture proven AWS settings before a publish, or from an immutable version.

    Unlike ordinary legacy updates, a restore must SEND the original platform.
    Missing platform metadata cannot prove V1. Unknown resource shapes are refused
    rather than reduced to consumption or a guessed baseline.
    """
    platform = detail.get("platformVersion")
    if platform not in {"V1", "V2"}:
        raise RuntimeError(
            "original Runtime platform metadata is missing; recovery requires review"
        )
    resources = detail.get("resourceConfigurations")
    if resources is None or resources == []:
        return RuntimeConfig(platform_version=platform, billing_model="consumption")
    if (
        not isinstance(resources, list)
        or len(resources) != 1
        or not isinstance(resources[0], dict)
        or set(resources[0]) != {"minimumMemoryInMiB"}
    ):
        raise RuntimeError(
            "original Runtime resource configuration is unsupported; recovery requires review"
        )
    return RuntimeConfig(
        platform_version=platform, billing_model="committed_baseline",
        minimum_memory_mib=resources[0]["minimumMemoryInMiB"],
    )


def original_runtime_config(control: Any, runtime_id: str, version: str | None) -> RuntimeConfig:
    """Read only a proven pre-candidate version, never the rejected current version."""
    if not isinstance(version, str) or not version.isascii() or not version.isdecimal() or (
        not 1 <= len(version) <= 5 or version.startswith("0")
    ):
        raise RuntimeError("original Runtime version is not proven; recovery requires review")
    detail = rt.get_runtime(control, runtime_id, version=version)
    if detail.get("agentRuntimeVersion") != version:
        raise RuntimeError("original Runtime version read did not match; recovery requires review")
    return captured_runtime_config(detail)


def runtime_config_kwargs(
    spec: AgentSpec, *, updating: bool = False, live: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Optional kwargs for the Runtime wrappers, never a deployment-time default.

    Updates reuse the same successful GetAgentRuntime as mount preservation (or
    canary's role/version read). A missing live read cannot imply consumption.
    An old create job with no choice keeps its original AWS defaults.
    """
    config = spec.runtime_config
    if config is not None:
        kwargs: dict[str, Any] = {"platform_version": config.platform_version}
        if config.billing_model == "committed_baseline":
            kwargs["resource_configurations"] = [
                {"minimumMemoryInMiB": config.minimum_memory_mib}
            ]
        return kwargs
    if not updating:
        return {}
    if live is None:
        raise RuntimeError("live Runtime configuration is required to preserve legacy billing")
    # Keep platformVersion omitted: AWS preserves the current platform. Resources
    # are echoed verbatim (including future documented members), not reconstructed.
    resources = live.get("resourceConfigurations")
    return {"resource_configurations": deepcopy(resources)} if resources is not None else {}
