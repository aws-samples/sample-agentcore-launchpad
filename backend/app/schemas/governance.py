"""Typed HTTP contracts for Gateway governance."""

from datetime import datetime
from typing import Any, Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator

IDENTIFIER_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$"
POLICY_NAME_PATTERN = r"^[A-Za-z][A-Za-z0-9_]{0,47}$"


class MutationEnvelope(BaseModel):
    expected_gateway_updated_at: datetime | None = None
    expected_policy_updated_at: datetime | None = None
    acknowledged_gateway_ids: list[str] = Field(default_factory=list, max_length=100)
    confirmation_name: str | None = Field(default=None, max_length=100)
    override_reason: str | None = Field(default=None, max_length=1000)

    @field_validator("acknowledged_gateway_ids")
    @classmethod
    def validate_acknowledged_ids(cls, value: list[str]) -> list[str]:
        for gateway_id in value:
            if not gateway_id or len(gateway_id) > 128:
                raise ValueError("invalid acknowledged Gateway ID")
        return sorted(set(value))


class GatewayAction(BaseModel):
    name: str
    target_id: str
    target_name: str
    description: str = ""
    input_schema: dict[str, Any] = Field(default_factory=dict)
    verified: bool
    source: Literal["control_schema", "live_tools_list", "manual"]


class RateConfigRequest(BaseModel):
    """One ``RateConfig`` — the semantic bounds are checked by the service so the
    console gets a ``detail.reason`` instead of a pydantic location list."""

    model_config = ConfigDict(extra="forbid")

    rate: float
    period: str


class RateLimitEntryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dimensions: dict[str, str]
    requests: list[RateConfigRequest] | None = None
    tokens: list[RateConfigRequest] | None = None
    connections: list[RateConfigRequest] | None = None


class RateLimitCreateRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    dimension_keys: list[str] = Field(
        validation_alias=AliasChoices("dimension_keys", "dimensionKeys"),
    )
    entries: list[RateLimitEntryRequest]
    description: str | None = None


class RateLimitUpdateRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    entries: list[RateLimitEntryRequest]
    description: str | None = None
    # Immutable on AWS. Accepted here only so the service can answer 422 with a
    # reason instead of letting an ignored field pass silently.
    dimension_keys: list[str] | None = Field(
        default=None,
        validation_alias=AliasChoices("dimension_keys", "dimensionKeys"),
    )
