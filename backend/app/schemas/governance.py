"""Typed HTTP contracts for Gateway and Policy governance."""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

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


class EngineRequest(MutationEnvelope):
    expected_gateway_updated_at: datetime
    name: str | None = Field(default=None, pattern=POLICY_NAME_PATTERN)
    authorization_model: Literal["allowlist", "preserve_traffic", "custom"] = "allowlist"
    high_risk_acknowledged: bool = False

    @model_validator(mode="after")
    def validate_preserve_traffic(self) -> "EngineRequest":
        if self.authorization_model == "preserve_traffic" and not self.high_risk_acknowledged:
            raise ValueError("preserve_traffic requires high-risk acknowledgement")
        return self


class PolicyCreateRequest(MutationEnvelope):
    expected_gateway_updated_at: datetime
    name: str = Field(pattern=POLICY_NAME_PATTERN)
    statement: str = Field(min_length=1, max_length=50_000)
    description: str | None = Field(default=None, max_length=500)
    authorization_model: Literal["allowlist", "preserve_traffic", "custom"] = "allowlist"
    high_risk_acknowledged: bool = False
    manual_actions: list[str] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def validate_authorization_model(self) -> "PolicyCreateRequest":
        if self.authorization_model == "preserve_traffic" and not self.high_risk_acknowledged:
            raise ValueError("preserve_traffic requires high-risk acknowledgement")
        if any(not action.strip() or len(action) > 256 for action in self.manual_actions):
            raise ValueError("manual action identifiers must be non-empty and at most 256 chars")
        return self


class PolicyUpdateRequest(MutationEnvelope):
    expected_gateway_updated_at: datetime
    expected_policy_updated_at: datetime
    statement: str = Field(min_length=1, max_length=50_000)
    description: str | None = Field(default=None, max_length=500)
    manual_actions: list[str] = Field(default_factory=list, max_length=100)


class PolicyDeleteRequest(MutationEnvelope):
    expected_gateway_updated_at: datetime
    expected_policy_updated_at: datetime


class PolicyTransitionRequest(MutationEnvelope):
    expected_gateway_updated_at: datetime
    expected_policy_updated_at: datetime
    evidence_range: Literal["1h", "6h", "24h", "7d"] = "24h"
    audit_id: str | None = Field(default=None, max_length=32)

    @model_validator(mode="after")
    def validate_override(self) -> "PolicyTransitionRequest":
        if self.override_reason is not None and not self.override_reason.strip():
            raise ValueError("override_reason cannot be empty")
        return self


class GatewayModeRequest(PolicyTransitionRequest):
    expected_policy_updated_at: datetime | None = None
    mode: Literal["LOG_ONLY", "ENFORCE"]


class GenerationRequest(MutationEnvelope):
    expected_gateway_updated_at: datetime
    text: str = Field(min_length=10, max_length=4000)
    name: str = Field(default="launchpad_generated", pattern=POLICY_NAME_PATTERN)


class RegistryImportRequest(MutationEnvelope):
    expected_gateway_updated_at: datetime
    record_name: str | None = Field(default=None, min_length=1, max_length=100)
    apply_update: bool = False


class RetireLegacyRequest(MutationEnvelope):
    expected_gateway_updated_at: datetime
    record_ids: list[str] = Field(min_length=1, max_length=100)


class GatewayAction(BaseModel):
    name: str
    target_id: str
    target_name: str
    description: str = ""
    input_schema: dict[str, Any] = Field(default_factory=dict)
    verified: bool
    source: Literal["control_schema", "live_tools_list", "manual"]


class OperationView(BaseModel):
    id: str
    gateway_id: str
    operation: str
    status: Literal["pending", "running", "succeeded", "failed", "partial", "interrupted"]
    error: str | None = None
    created_at: datetime | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
