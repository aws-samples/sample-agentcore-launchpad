"""Optional agent-owned Runtime platform and billing intent (not worker defaults)."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, StrictInt, model_validator

RUNTIME_CONFIG_METHODS = frozenset({"zip_runtime", "studio", "container", "byoc"})
BASELINE_MEMORY_MIB = frozenset({1024, 2048, 4096, 8192})


class RuntimeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    platform_version: Literal["V1", "V2"]
    billing_model: Literal["consumption", "committed_baseline"]
    minimum_memory_mib: StrictInt | None = None

    @model_validator(mode="after")
    def _billing_configuration(self) -> "RuntimeConfig":
        if self.billing_model == "consumption":
            if self.minimum_memory_mib is not None:
                raise ValueError("consumption billing cannot set minimum_memory_mib")
        elif self.platform_version != "V2" or self.minimum_memory_mib not in BASELINE_MEMORY_MIB:
            raise ValueError(
                "committed_baseline requires V2 and minimum_memory_mib of "
                "1024, 2048, 4096 or 8192"
            )
        return self
