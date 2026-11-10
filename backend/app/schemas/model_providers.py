"""Wire contract of `/api/settings/model-providers` (D21, plan §3) — the frozen shapes the
console (M1-c, `frontend/src/lib/api.ts`) codes against from day 0.

Every response is **secret-free by construction**: a provider carries the *names* of its
Identity credential provider and SSM parameter (the flat `credential_*` keys of `ProviderOut`),
never a value;
there is no field a key could travel in. Request bodies forbid unknown keys so a drifting
client fails loudly. `tests/test_model_providers_api.py` pins the exact key set of every
response model; `tests/test_model_providers_contract.py` pins them against the console's
`frontend/src/lib/api.ts` (pairing: cn/m1-c a143c8cf).
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

ProviderKind = Literal["bedrock", "openai_compatible"]
ModelKind = Literal["chat", "embedding"]
RowSource = Literal["builtin", "probed", "manual"]
CredentialSource = Literal["ssm", "host_env"]
ProbeState = Literal["never", "ok", "failed"]
# N17-obs203: owner-bound providers — `personal` (owner + administrators) | `workspace` (all)
ProviderScope = Literal["personal", "workspace"]

PROVIDER_NAME_PATTERN = r"^[a-z][a-z0-9-]{1,31}$"
SSM_PARAMETER_PATTERN = r"^launchpad-[a-z0-9-]+-api-key$"
MODEL_REGION_PATTERN = r"^[a-z]{2}(-gov)?-[a-z]+-\d$"


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ── providers ────────────────────────────────────────────────────────────────────────
class ProviderProbeOut(BaseModel):
    state: ProbeState
    checked_at: str | None = None
    latency_ms: int | None = None
    models_seen: list[str] = Field(default_factory=list)
    error: str | None = None


class ProviderOut(BaseModel):
    id: str
    name: str
    kind: ProviderKind
    label: str
    base_url: str | None
    model_region: str | None
    # credential REFERENCES only (spec C4): Identity provider name, SSM parameter NAME,
    # where the key enters, the host-env fallback variable — never a value
    credential_provider: str
    credential_ssm_parameter: str | None
    credential_source: CredentialSource
    credential_env: str | None
    # N17-obs283 (design simplification 10.3): ONE shape — the flat keys above. The nested
    # `credential {provider, ssm_parameter, source, env, configured, last4}` mirror (rev-r8 M1)
    # was dropped; its only extra fact is `key_last4` below.
    required: bool
    builtin: bool
    enabled: bool
    id_prefix: str
    probe: ProviderProbeOut
    configured: bool  # the hub holds the key — `ProviderRow.hub_holds_key()` (N17-obs280)
    key_version: int | None  # SSM `Version` of the registered key (M1-b), never its value
    key_modified_at: str | None  # SSM `LastModifiedDate`
    # N17-obs203 §3: the last four characters of a key typed in the console (the "configured ·
    # ****abcd" badge), null when the key came from SSM / the host environment — never the value
    key_last4: str | None = None
    model_count: int  # additive to the m1-c contract (ignored by the console)
    created_by: str | None  # N17-obs203: the creating account's users.id (the ownership key)
    created_at: str | None
    updated_by: str | None
    updated_at: str | None
    # N17-obs203: owner-bound resource — display name of the creator, visibility scope and
    # whether the CALLER's account created the row
    created_by_name: str | None = None
    scope: ProviderScope = "workspace"
    owned: bool = False


class ProviderCreate(_In):
    name: str = Field(pattern=PROVIDER_NAME_PATTERN)
    kind: ProviderKind
    label: str = Field(min_length=1, max_length=64)
    base_url: str | None = Field(default=None, max_length=256)
    model_region: str | None = Field(default=None, pattern=MODEL_REGION_PATTERN)
    credential_ssm_parameter: str | None = Field(default=None, pattern=SSM_PARAMETER_PATTERN)
    enabled: bool = True
    # N17-obs203: a member may only create `personal` (the default for members; omitted =
    # the caller's default); `workspace` needs an administrator (403 otherwise)
    scope: ProviderScope | None = None
    # N17-obs203 §3: the provider's API key, typed in the console — WRITE-ONLY. It exists in
    # the request body and in memory until the console host has probed the endpoint with it
    # and written it to the row's SSM SecureString; no response, log, journal or ledger column
    # ever carries it (`core/errors.is_sensitive_field` keeps it out of validation errors too).
    api_key: str | None = Field(default=None, min_length=1, max_length=65536)


class ProviderUpdate(_In):
    """`name` and `kind` are immutable (a kind is a client implementation)."""

    label: str | None = Field(default=None, min_length=1, max_length=64)
    base_url: str | None = Field(default=None, max_length=256)
    model_region: str | None = Field(default=None, pattern=MODEL_REGION_PATTERN)
    credential_ssm_parameter: str | None = Field(default=None, pattern=SSM_PARAMETER_PATTERN)
    enabled: bool | None = None
    # N17-obs203: flipping the scope (sharing a personal provider with the workspace, or the
    # reverse) is an administrator's action
    scope: ProviderScope | None = None
    # N17-obs203 §3: replace the key (write-only, see `ProviderCreate.api_key`)
    api_key: str | None = Field(default=None, min_length=1, max_length=65536)


class CredentialRegisterIn(_In):
    """`POST /{id}/credential`: without a body the hub reads the SSM parameter named on the
    provider row (plan §4). N17-obs203 §3: with `api_key` (write-only) the console host probes
    the endpoint with the typed key, writes it to that parameter itself and then registers it —
    the "replace key" path."""

    api_key: str | None = Field(default=None, min_length=1, max_length=65536)


class CredentialOut(BaseModel):
    configured: bool
    created: bool
    rotated: bool
    workspaces_updated: int
    key_version: int | None = None  # SSM parameter version (never its value)
    last4: str | None = None  # N17-obs203 §3: the typed key's last four characters, never more


# ── models ───────────────────────────────────────────────────────────────────────────
class InferenceParamsOut(BaseModel):
    temperature: bool
    top_p: bool


class InferenceParamsIn(_In):
    temperature: bool | None = None
    top_p: bool | None = None


class ModelProbeRecordOut(BaseModel):
    """`models.probe` (plan §7.4)."""

    checked_at: str | None = None
    latency_ms: int | None = None
    findings: dict[str, Any] = Field(default_factory=dict)
    errors: dict[str, str] = Field(default_factory=dict)
    raw_hint: dict[str, Any] = Field(default_factory=dict)


class ModelOut(BaseModel):
    id: str
    provider_id: str
    provider: str
    model_id: str
    api_model_name: str
    label: str
    kind: ModelKind
    enabled: bool
    is_default: bool
    tools: bool
    streaming: bool
    vision: bool
    inference_params: InferenceParamsOut
    default_max_tokens: int
    max_output_tokens: int | None
    default_judge_max_tokens: int | None
    embedding_dimension: int | None
    embedding_max_chars: int | None
    source: RowSource
    capability_sources: dict[str, str]
    probe: ModelProbeRecordOut | None
    sort_order: int
    created_by: str | None
    created_at: str | None
    updated_by: str | None
    updated_at: str | None


class ModelCapabilityOverrides(_In):
    tools: bool | None = None
    streaming: bool | None = None
    vision: bool | None = None
    inference_params: InferenceParamsIn | None = None
    default_max_tokens: int | None = Field(default=None, ge=0, le=131072)
    max_output_tokens: int | None = Field(default=None, ge=256, le=1_000_000)
    default_judge_max_tokens: int | None = Field(default=None, ge=256, le=131072)
    embedding_dimension: int | None = Field(default=None, ge=1, le=65536)
    embedding_max_chars: int | None = Field(default=None, ge=1, le=10_000_000)


class ModelCreate(_In):
    api_model_name: str = Field(min_length=1, max_length=200, pattern=r"^\S+$")
    kind: ModelKind = "chat"
    label: str | None = Field(default=None, max_length=128)
    probe: bool = True  # probe-on-add (adopted default); false ⇒ kind defaults, `manual`
    overrides: ModelCapabilityOverrides | None = None
    enabled: bool = True


class ModelUpdate(ModelCapabilityOverrides):
    label: str | None = Field(default=None, min_length=1, max_length=128)
    enabled: bool | None = None
    is_default: bool | None = None
    sort_order: int | None = Field(default=None, ge=0, le=100_000)


class ModelProbeIn(_In):
    apply: bool = False


class ModelProbeDiffEntry(BaseModel):
    stored: Any
    probed: Any
    source: str


class ModelProbeOut(BaseModel):
    findings: dict[str, Any]
    diff: dict[str, ModelProbeDiffEntry]
    errors: dict[str, str] = Field(default_factory=dict)
    raw_hint: dict[str, Any] = Field(default_factory=dict)
    latency_ms: int | None = None
    applied: bool = False  # api.ts `ModelProbeOut.applied?: boolean` — followed literally


# ── workspaces / journal / page ──────────────────────────────────────────────────────
class WorkspaceFlagsIn(_In):
    disabled_models: list[str] = Field(default_factory=list, max_length=500)


class WorkspaceOut(BaseModel):
    """One `workspaces[]` element; `PUT /workspaces/{id}/models` returns the same shape."""

    id: str
    name: str
    disabled_models: list[str]


class WorkspaceFlagsOut(WorkspaceOut):
    """`PUT /workspaces/{id}/models` echo: the element plus `workspace_id` (rev-r8 M8)."""

    workspace_id: str


class WarningOut(BaseModel):
    code: str
    message: str
    detail: dict[str, Any] | None = None


class RegistryOut(BaseModel):
    catalog_version: int
    providers: list[ProviderOut]
    models: list[ModelOut]
    workspaces: list[WorkspaceOut]
    warnings: list[WarningOut]


class ChangeOut(BaseModel):
    id: int
    operation: str
    actor: str
    target_id: str | None
    before: dict[str, Any] | None
    after: dict[str, Any] | None
    created_at: str | None


class ChangesOut(BaseModel):
    items: list[ChangeOut]


# ── model roles (r15-e, upstream #275 subset) ────────────────────────────────────────
class ModelRoleOut(BaseModel):
    """One row of ``GET /api/model-defaults`` and the echo of the admin PUT / DELETE."""

    role: str  # `agent_default`, `judge`, … (services/model_roles.ROLES)
    field: str  # the `Settings` field the role reads (= the `platform_settings` key)
    model_id: str  # effective: the override, else env / yaml / built-in default
    default: str  # what a reset returns to
    overridden_by: str | None  # the administrator who set the override, null when none
    updated_at: str | None
    needs_tools: bool  # the role runs tools: only tool-capable catalog rows are accepted
    allow_empty: bool  # "" is a valid value (no model)
    # N16-obs22: the env / yaml / built-in default is disabled or gone — a reset would be
    # refused (422 models.model_not_allowed, suggest: repoint); the console flags the row
    default_disabled: bool = False
    # N17-obs252: `chat` (a Settings-backed role) or `embedding` (the registry's ACTIVE
    # embedding row — `field` is "" and a save goes through the guarded default flip)
    kind: str = "chat"
    # N17-obs252: the console feature the role belongs to (`skill_lab`, `studio`) — hidden on
    # the roles view while that feature is hidden; null = always shown
    feature: str | None = None


class ModelRolesOut(BaseModel):
    roles: list[ModelRoleOut]


class ModelRoleSetIn(_In):
    model_id: str = Field(max_length=200)
