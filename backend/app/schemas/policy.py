"""Request / response shapes of policy engines and gateway attachments
(Global model; contract ``docs/cn/interfaces/policy-engines.md``). Response shapes are
documented on the store's ``*_view`` helpers and in ``docs/cn/interfaces/policy-backend.md``."""

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, BeforeValidator, Field, field_validator

# Global's POLICY_NAME_PATTERN (schemas/governance.py) — kept so restored console code
# validates the same way.
POLICY_NAME_PATTERN = r"^[A-Za-z][A-Za-z0-9_]{0,47}$"
MAX_STATEMENT_CHARS = 50_000

EnforcementMode = Literal["active", "log_only"]  # a policy's (Global ACTIVE | LOG_ONLY)
AttachmentMode = Literal["LOG_ONLY", "ENFORCE"]  # a gateway attachment's (Global spelling)
# Global parity (contract amendment 9): policies decide Gateway tool calls only — there is
# no `invoke` action and no agent resource in the vocabulary
Action = Literal["call_tool", "list_tool"]
PrincipalKind = Literal["user", "api_key", "agent", "system"]
ResourceKind = Literal["tool"]


class PolicyEngineCreate(BaseModel):
    """``POST /api/policy-engines`` — Global's CreatePolicyEngine (name, description)."""

    name: str = Field(pattern=POLICY_NAME_PATTERN)
    description: str | None = Field(default=None, max_length=4000)

    @field_validator("description", mode="before")
    @classmethod
    def _none_description(cls, value: str | None) -> str:
        return value or ""


class PolicyEngineUpdate(BaseModel):
    description: str | None = Field(default=None, max_length=4000)


class GatewayPolicyEngineAttach(BaseModel):
    """Gateway.policyEngineConfiguration at the governance gateway attachment route."""

    engine_id: str = Field(min_length=1, max_length=32)
    mode: AttachmentMode = "LOG_ONLY"

    @field_validator("mode", mode="before")
    @classmethod
    def valid_mode(cls, value: Any) -> Any:
        if value not in ("LOG_ONLY", "ENFORCE"):
            from app.core.errors import AppError

            raise AppError("gateway.policy_engine_mode_invalid",
                           "mode must be LOG_ONLY or ENFORCE", status_code=422)
        return value


class PolicyCreate(BaseModel):
    name: str = Field(pattern=POLICY_NAME_PATTERN)
    statement: str = Field(min_length=1, max_length=MAX_STATEMENT_CHARS)
    # The editors send `null` for an empty description — normalised to "".
    description: str | None = Field(default=None, max_length=4000)
    # Global `enforcementMode`; absent = `log_only` (every new policy starts as a shadow)
    enforcement_mode: EnforcementMode | None = None
    # Name of the editor template the statement started from (journal only).
    prefill: str | None = Field(default=None, max_length=64)

    @field_validator("description", mode="before")
    @classmethod
    def _none_description(cls, value: str | None) -> str:
        return value or ""


class PolicyUpdate(BaseModel):
    statement: str | None = Field(default=None, min_length=1, max_length=MAX_STATEMENT_CHARS)
    description: str | None = Field(default=None, max_length=4000)


class PolicyPublish(BaseModel):
    """No confirmation / apply fields (Global parity: a publish is a publish)."""

    version: int | None = Field(default=None, ge=1)
    reason: str | None = Field(default=None, max_length=2000)


class PolicyRetire(BaseModel):
    reason: str | None = Field(default=None, max_length=2000)


class StatementBody(BaseModel):
    statement: str = Field(min_length=1, max_length=MAX_STATEMENT_CHARS)
    # (`/validate`): `policy_id` = the policy being edited, left out of the ACTIVE set a
    # Global-vocabulary (temporal) statement compiles with
    policy_id: str | None = Field(default=None, max_length=32)


class SimulatePrincipal(BaseModel):
    kind: PrincipalKind
    id: str = Field(min_length=1, max_length=120)
    # users only — picks the Role parent when the id is not a ledger user
    role: Literal["admin", "member"] | None = None


class SimulateResource(BaseModel):
    kind: ResourceKind
    id: str = Field(min_length=1, max_length=160)


class PinnedVersion(BaseModel):
    policy_id: str = Field(min_length=1, max_length=32)
    version: int = Field(ge=1)


# A6 review nit: the engine simulation's inputs are bounded (a replay runs the real engine)
SIMULATE_MAX_JSON_BYTES = 16 * 1024
SIMULATE_MAX_HISTORY = 50


def _bounded_json(value: dict[str, Any]) -> dict[str, Any]:
    import json

    size = len(json.dumps(value, separators=(",", ":"), default=str).encode())
    if size > SIMULATE_MAX_JSON_BYTES:
        raise ValueError(f"at most {SIMULATE_MAX_JSON_BYTES} bytes of JSON")
    return value


class SimulateHistoryEvent(BaseModel):
    """One earlier event of the simulated session (A6 engine simulation)."""

    tool: str = Field(min_length=1, max_length=160)
    kind: Literal["request", "response", "error"] = "request"
    input: dict[str, Any] = Field(default_factory=dict)
    output: dict[str, Any] = Field(default_factory=dict)
    at: datetime

    _bounded = field_validator("input", "output")(_bounded_json)


class SimulateEngine(BaseModel):
    """A6: decide on the Dogwood engine (both stages, temporal rules over ``history``). The
    principal is ``SimulateRequest.principal``; the calling agent is ``agent_id`` (else the
    agent principal); ``tool`` absent = the request's tool resource id."""

    tool: str | None = Field(default=None, min_length=1, max_length=160)
    agent_id: str | None = Field(default=None, min_length=1, max_length=64)
    input: dict[str, Any] = Field(default_factory=dict)
    at: datetime | None = None
    history: list[SimulateHistoryEvent] = Field(default_factory=list,
                                                max_length=SIMULATE_MAX_HISTORY)

    _bounded = field_validator("input")(_bounded_json)


class SimulateRequest(BaseModel):
    principal: SimulatePrincipal
    action: Action
    resource: SimulateResource
    # the gateway whose attachment mode the answer reports (the engine evaluates against that
    # gateway's resource arn); absent = the engine's workspace gateway
    gateway_id: str | None = Field(default=None, max_length=128)
    context: dict[str, Any] = Field(default_factory=dict)
    # Evaluate each policy's draft version (when it has one) instead of its ACTIVE one.
    include_drafts: bool = False
    # Evaluate exactly this version of one policy (the editor's "test this draft").
    policy_version: PinnedVersion | None = None
    # Also journal the outcome as a ``policy_decisions`` row with ``source=simulate``.
    record: bool = False
    # A6 (additive): decide on the engine; absent → the cedarpy simulation as before.
    engine: SimulateEngine | None = None


class GatewaySimulateRequest(SimulateEngine):
    """The console's flat, gateway-specific tool simulation contract."""

    gateway_id: str = Field(min_length=1, max_length=128)
    principal: SimulatePrincipal
    tool: str = Field(min_length=1, max_length=160)
    record: bool = False

    def as_simulate_request(self) -> SimulateRequest:
        """Share the existing engine replay, permission checks and rate limit."""
        return SimulateRequest(
            principal=self.principal, action="call_tool",
            resource=SimulateResource(kind="tool", id=self.tool),
            gateway_id=self.gateway_id, record=self.record,
            engine=SimulateEngine(
                tool=self.tool, agent_id=self.agent_id, input=self.input,
                at=self.at, history=self.history,
            ),
        )


def _unmixed_simulation(value: Any) -> Any:
    """Do not let invalid flat requests fall back to a different legacy evaluation."""
    if isinstance(value, dict):
        flat = {"tool", "agent_id", "input", "at", "history"}
        legacy = {"action", "resource", "context", "include_drafts", "policy_version", "engine"}
        if flat.intersection(value) and legacy.intersection(value):
            raise ValueError("flat and legacy simulation fields cannot be mixed")
    return value


PolicySimulationRequest = Annotated[
    GatewaySimulateRequest | SimulateRequest, BeforeValidator(_unmixed_simulation),
]


class EnforcementModeUpdate(BaseModel):
    """Per-policy enforcement (Global ``enforcementMode``): ``log_only`` records what the
    policy would deny without denying."""

    enforcement_mode: EnforcementMode
