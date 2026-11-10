"""AgentSpec — the one artifact every creation method converges into."""

import os
import re
from collections.abc import Iterable, Mapping
from pathlib import PurePosixPath
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas import model_catalog
from app.schemas.attachments import AttachmentRequest
from app.schemas.inbound_auth import InboundAuth
from app.schemas.model_catalog import (  # noqa: F401 — re-exported public names (D21)
    DEFAULT_MODEL_CREDENTIAL,
    DEFAULT_MODEL_ID,
    DEFAULT_MODEL_REGION,
    DEFAULT_OPENAI_COMPATIBLE_JUDGE_MAX_TOKENS,
    DEFAULT_OPENAI_COMPATIBLE_MAX_TOKENS,
    DEFAULT_REASONING_MAX_TOKENS,
    DEFAULT_RUNTIME_MAX_TOKENS,
    MIN_MODEL_MAX_TOKENS,
    PROVIDER_BEDROCK,
    PROVIDER_OPENAI_COMPATIBLE,
    ModelRow,
    ProviderRow,
    build_model_catalog,
    openai_compatible_entry,
    parse_provider_models,
)
from app.schemas.requirements import assert_all_pinned


def agent_default_model_id() -> str:
    """The model a NEW spec without one gets — the `agent_default` model role (r15-e):
    `Settings.agent_default_model_id` = the console override, else env / yaml, else
    `DEFAULT_MODEL_ID`. Read per call, never at import."""
    from app.services import model_roles

    # rev-r25 F3: a dead (disabled / deleted) role model falls back to the role's default
    return model_roles.resolve("agent_default") or DEFAULT_MODEL_ID


def judge_default_model_id() -> str:
    """The `judge` model role (`Settings.eval_judge_model_id`, the model-defaults page) — the
    model an LLM-judge evaluator gets when nothing names one — falling back to
    `DEFAULT_MODEL_ID`. Read per call, never at import (N17-obs202: the architect's
    evaluation plans follow the page the same way the system presets do)."""
    from app.services import model_roles

    return model_roles.resolve("judge") or DEFAULT_MODEL_ID

# Spec C3: the CN build may invoke exactly the models of the catalog. Since D21 the catalog
# is the admin-managed **model registry** (`services/model_registry.py`, Settings → Model
# Providers) published as an immutable snapshot (`schemas/model_catalog.py`). The pre-D21
# module constants (`PROVIDERS`, `MODEL_CATALOG`, `MODEL_CAPABILITIES`,
# `MODEL_DEFAULT_MAX_TOKENS`, `ALLOWED_MODEL_IDS`) were REMOVED in M1-e (C5); consumers call
# the function views below (`providers()`, `model_catalog_table()`, `model_capabilities()`,
# `model_default_max_tokens()`, `allowed_model_ids()`), which answer the same dicts / tuple
# from `model_catalog.current()`. Two provider kinds exist (N-R6):
#
# * ``bedrock`` — the Global Bedrock cross-region inference profiles, entered from
#   DEFAULT_MODEL_REGION with the workspace's Global Bedrock API key (held by the AgentCore
#   Identity API-key credential provider DEFAULT_MODEL_CREDENTIAL — bootstrap stage
#   `model-credential`), never with an aws-cn role, which is why no IAM statement anywhere
#   names a model.
# * ``openai_compatible`` — chat models of an OpenAI-compatible endpoint (e.g. SiliconFlow:
#   standard chat-completions + Bearer key), each provider's key held by its own Identity
#   API-key credential provider and reached through the same GetResourceApiKey exchange.
#   Since R13 (N13-obs7) no such provider is seeded: an administrator registers one.
#
# Provider specifics (endpoint, credential provider name, SSM parameter) live ONLY in the
# registry; every surface — hub builder, generated runtimes, eval worker, Skill Lab OpenCode
# config, IAM, bootstrap, the console pickers — derives from the snapshot
# (docs/cn/interfaces/model-registry.md). With an EMPTY registry (pre-seed, tests without a
# DB) the snapshot is `BUILTIN_CATALOG` = today's tables, so behaviour is unchanged.
MODEL_NOT_ALLOWED_CODE = "model.not_allowed"
# An OpenAI-compatible model without function calling cannot drive an agent (tools are the
# agent contract) but may judge — refused as `AgentSpec.model_id` with this code.
MODEL_TOOLS_UNSUPPORTED_CODE = "model.tools_unsupported"
# A catalog model whose provider has no API key in this workspace (N-R6-obs1): listed by
# `/api/capabilities` with `available: false` so the console can explain, refused with this
# code wherever a member picks a model (agent create/redeploy, BYOC allowed_models, judges,
# actor, recommendation/optimizer, architect proposal) — never only at invoke time.
MODEL_PROVIDER_UNCONFIGURED_CODE = "model.provider_unconfigured"
# N17-obs203 §6: a catalog model of ANOTHER member's personal provider — never offered to this
# account (pickers, `/api/capabilities`, the architect prose) and refused wherever it is named
# (`agent.model_not_visible` on agent create / redeploy, `run.model_not_visible` on an actor).
MODEL_NOT_VISIBLE_CODE = "model.not_visible"
# N17-obs227 (rev-r20): the HUB spends a provider's key on a member's behalf at these sites —
# a judge / derived evaluator's `model_id`, a plan approval's judges, a recommendation's
# reflection model, the hub judge builder — so they refuse another member's personal model
# with this code (the console translates it next to `agent.` / `run.model_not_visible`).
MODELS_NOT_VISIBLE_CODE = "models.model_not_visible"

# Converse `inferenceConfig` fields a strands BedrockModel forwards and that a catalog
# model may refuse. N12-bug11 (live 2026-10-10, t1 run 917fef1fe10e): Kimi K3 answers
# ConverseStream with ValidationException "This model doesn't support the temperature
# field"; GLM 5.3 accepts it. Every model builder — aws_clients.build_chat_model on the hub
# and the generated runtimes' `_launchpad_bedrock_model` / `_launchpad_openai_model` (which
# get the table below inlined by templates/strands_agent.rendered_model_inference_params) —
# drops a field the table flags False instead of sending it. `top_p` is not listed: nothing
# sends it and the model cards do not document it; the registry records it per row (D21)
# for the day a builder sends it.
INFERENCE_PARAMS: tuple[str, ...] = ("temperature",)

# ── providers ────────────────────────────────────────────────────────────────────
ProviderKind = Literal["bedrock", "openai_compatible"]


# ── views over the snapshot (D21) ─────────────────────────────────────────────────
# Every reader goes through these functions (no module-level dict / tuple proxies since
# M1-e, C5 — a test publishes a variant snapshot with `model_catalog.publish(
# model_catalog.extended(...))` instead of monkeypatching a module constant). The dicts
# are the snapshot's cached legacy views: identity (`is`) is stable while the snapshot is,
# and callers must not mutate them.
def allowed_model_ids() -> tuple[str, ...]:
    """The chat rows of the catalog in picker order (the former `ALLOWED_MODEL_IDS`;
    disabled chat rows included — stored specs on them keep parsing, plan G4)."""
    return model_catalog.current().allowed_ids()


def providers() -> dict[str, dict[str, Any]]:
    """``{name: legacy provider row}`` (the former `PROVIDERS` dict; keys
    `LEGACY_PROVIDER_KEYS`). The snapshot's cached view — read-only."""
    return model_catalog.current().legacy_providers()


def provider_row(name: str) -> dict[str, Any]:
    """The legacy provider row for ``name`` (`KeyError` outside the catalog, as the former
    ``PROVIDERS`` lookup was)."""
    return providers()[name]


def model_catalog_table() -> dict[str, dict[str, Any]]:
    """``{model_id: legacy catalog row}`` for every CHAT row (the former `MODEL_CATALOG`
    dict; keys `LEGACY_MODEL_KEYS`, insertion order = picker order). Read-only."""
    return model_catalog.current().legacy_models()


def model_capabilities() -> dict[str, dict[str, Any]]:
    """Per-model capability table (the former `MODEL_CAPABILITIES`): `tools`, `streaming`,
    `vision`, the `INFERENCE_PARAMS` flags, `default_judge_max_tokens`, `max_output_tokens`
    — consumed by services/attachments.py (native image-attachment support), the inference
    parameter filter below and the deployer templates. N-SCN-bug2: `default_judge_max_tokens`
    is the output budget a judge / optimizer / summariser call on this model STARTS from
    (the hub's `Settings.eval_judge_max_tokens` is a floor under it); `max_output_tokens`
    (the model's documented hard limit) is honoured by the budget resolver when present."""
    return model_catalog.current().legacy_capabilities()


def model_default_max_tokens() -> dict[str, int]:
    """Default per-call output ceiling per catalog model (N10-bug4; the former
    `MODEL_DEFAULT_MAX_TOKENS`), consumed through `default_runtime_max_tokens()` by the one
    renderer (`templates/strands_agent.rendered_max_tokens`) every generated runtime shares."""
    return model_catalog.current().legacy_default_max_tokens()


def catalog_version() -> int:
    """The published snapshot's version (journal id; 0 for the built-in table)."""
    return model_catalog.current().version


def model_row(model_id: str) -> ModelRow | None:
    """The snapshot's typed row for `model_id` (chat or embedding), or None."""
    return model_catalog.current().models.get(model_id)


def model_descriptor(model_id: str) -> dict[str, Any] | None:
    """The resolved descriptor a job payload carries to a worker (plan §5.5), or None for
    an id outside the catalog."""
    snapshot = model_catalog.current()
    row = snapshot.models.get(model_id)
    if row is None:
        return None
    provider = snapshot.providers.get(row.provider)
    return row.descriptor(snapshot.version,
                          model_region=provider.model_region if provider else None)


def active_embedding() -> ModelRow | None:
    """The catalog's active embedding row (plan §6) — Titan v2 for the built-in table."""
    return model_catalog.current().embedding_model()


def model_enabled(model_id: str, workspace_id: str | None = None) -> bool:
    """``model.enabled ∧ provider.enabled ∧ workspace flag`` from the snapshot (D21); an id
    the snapshot does not know (a test overlay, a custom BYOC id) answers True."""
    return model_catalog.current().model_enabled(model_id, workspace_id)


def model_entry(model_id: str) -> dict[str, Any] | None:
    """The catalog row for `model_id`, or None outside the catalog."""
    return model_catalog_table().get(model_id)


def model_provider(model_id: str) -> dict[str, Any]:
    """The provider row a model is served by — Bedrock for an id outside the catalog
    (custom BYOC ids, stale stored specs keep the historical path)."""
    entry = model_catalog_table().get(model_id)
    return provider_row(entry["provider"] if entry else "bedrock")


def model_provider_kind(model_id: str) -> str:
    return str(model_provider(model_id)["kind"])


def is_openai_compatible_model(model_id: str) -> bool:
    return model_provider_kind(model_id) == PROVIDER_OPENAI_COMPATIBLE


def api_model_name(model_id: str) -> str:
    """What the endpoint receives: the bare provider id for an OpenAI-compatible model,
    the id itself for Bedrock / unknown ids."""
    entry = model_catalog_table().get(model_id)
    return str(entry["api_model_name"]) if entry else model_id


def supports_tools(model_id: str) -> bool:
    """Function calling. An id outside the catalog is assumed to (the historical default)."""
    return bool(model_capabilities().get(model_id, {}).get("tools", True))


def provider_configured(provider: str, resources: Mapping[str, Any] | None = None) -> bool:
    """Whether ``provider``'s API key is configured (N-R6-obs1).

    ``resources`` = a WORKSPACE's resource map (callers that act for a workspace pass
    ``ctx.resources``): the bootstrap / credential-flow record
    ``resources.model_credentials[provider].configured`` on that map decides — the Identity
    credential provider the workspace's runtimes / workers read the key from exists only once
    it was registered in that workspace's account, so the record wins over a key that merely
    sits in the hub env; without a record, whether the hub environment carries the provider's
    ``api_key_env``. A required provider (Global Bedrock) is always configured — bootstrap
    refuses to run without its key.

    ``resources=None`` asks about the HUB ITSELF (the hub-side model builder, the preset
    snapshot): N17-obs280 (C3) — the registry row is the hub's single source,
    `ProviderRow.hub_holds_key()` (a registered key on the row, else the host env), the same
    predicate as the Settings card's `configured` and `aws_clients.provider_api_key`; before,
    this branch read the host env alone and refused a console-keyed provider the hub could
    read. Never a Settings read (workspace funnel)."""
    row = provider_row(provider)
    if row.get("required") or row["kind"] == PROVIDER_BEDROCK:
        return True
    if resources is None:
        return model_catalog.current().providers[provider].hub_holds_key()
    records = resources.get("model_credentials") if isinstance(resources, Mapping) else None
    record = records.get(provider) if isinstance(records, Mapping) else None
    if isinstance(record, Mapping) and "configured" in record:
        return bool(record["configured"])
    return bool(os.environ.get(str(row["api_key_env"]), "").strip())


def configured_providers(resources: Mapping[str, Any] | None = None) -> dict[str, bool]:
    """``{provider: configured}`` for every provider row."""
    return {name: provider_configured(name, resources) for name in providers()}


def model_available(model_id: str, resources: Mapping[str, Any] | None = None,
                    workspace_id: str | None = None) -> bool:
    """A catalog model is available when its provider is configured AND (D21) the row, its
    provider and the workspace flag are enabled; an id outside the catalog answers True here
    (the catalog check `assert_model_allowed` refuses it first)."""
    entry = model_catalog_table().get(model_id)
    if not entry:
        return True
    if not model_enabled(model_id, workspace_id):
        return False
    return provider_configured(str(entry["provider"]), resources)


def openai_compatible_providers() -> dict[str, dict[str, Any]]:
    """``{provider: legacy provider row}`` for every OpenAI-compatible provider."""
    return {name: row for name, row in providers().items()
            if row["kind"] == PROVIDER_OPENAI_COMPATIBLE}


def model_provider_table() -> dict[str, dict[str, Any]]:
    """``{model_id: {kind, base_url, api_model_name, credential_provider, streaming}}`` for
    every catalog model — the table the generated runtimes get inlined
    (`templates/strands_agent.rendered_model_providers`) and the Skill Lab OpenCode config
    is built from, so a runtime never imports the hub package or spells an endpoint."""
    return {
        model_id: {
            "kind": entry["kind"],
            "base_url": entry["base_url"],
            "api_model_name": entry["api_model_name"],
            "credential_provider": entry["credential_provider"],
            "streaming": entry["streaming"],
        }
        for model_id, entry in model_catalog_table().items()
    }


def default_runtime_max_tokens(model_id: str) -> int:
    """Per-call output ceiling for `model_id` when the spec sets no `max_tokens`.

    Catalog models get their `model_default_max_tokens()` entry; an id outside the catalog
    keeps the historical `DEFAULT_RUNTIME_MAX_TOKENS` — Bedrock rejects a value above a
    model's own limit, so an unknown model never gets the larger ceiling."""
    return model_default_max_tokens().get(model_id, DEFAULT_RUNTIME_MAX_TOKENS)


def default_judge_max_tokens(model_id: str) -> int | None:
    """Starting judge / optimizer output budget for a catalog model (N-SCN-bug2); ``None``
    for an id outside the catalog — the caller then keeps `Settings.eval_judge_max_tokens`."""
    value = model_capabilities().get(model_id, {}).get("default_judge_max_tokens")
    return int(value) if value else None


def judge_budget_defaults() -> dict[str, int]:
    """``{model_id: default_judge_max_tokens}`` for the catalog — the dict the budget
    resolver (`services/strands_eval/model_calls.resolve_budget`) and the worker template's
    inlined table are built from."""
    return {model_id: default for model_id in allowed_model_ids()
            if (default := default_judge_max_tokens(model_id)) is not None}


def model_max_output_tokens(model_id: str) -> int | None:
    """The model's documented output ceiling when the table carries `max_output_tokens`
    (none verified for the CN catalog today — the budget cap alone bounds the retries)."""
    value = model_capabilities().get(model_id, {}).get("max_output_tokens")
    return int(value) if value else None


def inference_param_support(model_id: str) -> dict[str, bool]:
    """Which `INFERENCE_PARAMS` `model_id` accepts (N12-bug11).

    An id outside the catalog (custom BYOC ids) has no entry and is assumed to accept
    everything — the field is sent as before, never silently dropped."""
    caps = model_capabilities().get(model_id, {})
    return {param: caps.get(param, True) for param in INFERENCE_PARAMS}


def supported_model_config(model_id: str, config: dict[str, Any]) -> dict[str, Any]:
    """`config` (strands BedrockModel kwargs) without the inference parameters
    `model_id` rejects; everything else — and every field of an unknown model — is kept."""
    accepts = inference_param_support(model_id)
    return {key: value for key, value in config.items() if accepts.get(key, True)}


def supports_native_images(model_id: str) -> bool:
    """Whether `model_id` accepts image content blocks natively (Converse vision).

    An id outside the catalog (custom BYOC ids, stale stored specs) has no entry and
    is treated as non-vision — the conservative default."""
    return model_capabilities().get(model_id, {}).get("vision", False)


def principal_of_identity(identity: Any) -> model_catalog.Principal | None:
    """The catalog `Principal` of a console identity (`routers/auth.Identity`): its
    `user_id` and whether it is an administrator. ``None`` identity → ``None`` (every row)."""
    if identity is None:
        return None
    return model_catalog.Principal(user_id=getattr(identity, "user_id", None),
                                   is_admin=bool(getattr(identity, "is_admin", False)))


def model_visible(model_id: str, principal: model_catalog.Principal | None) -> bool:
    """N17-obs203 §6 — see `Catalog.model_visible`."""
    return model_catalog.current().model_visible(model_id, principal)


def assert_model_visible(model_id: str, principal: model_catalog.Principal | None,
                         where: str = "model_id") -> str:
    """Refuse a model of ANOTHER member's personal provider (N17-obs203 §6) — the owner and
    administrators pass. Raises `ModelRefusal(model.not_visible)`; routes map it to their own
    code (`agent.model_not_visible`, `run.model_not_visible`)."""
    if not model_visible(model_id, principal):
        provider = model_catalog.current().provider_of(model_id)
        label = provider.label if provider is not None else "its provider"
        raise ModelRefusal(
            MODEL_NOT_VISIBLE_CODE,
            f"{where} {model_id!r} belongs to {label}, another member's personal provider — "
            "only its owner or an administrator may use it; pick a model of a shared provider "
            "or one of your own",
        )
    return model_id


def hub_model_refusal_code(exc: BaseException) -> str:
    """`model_refusal_code` for the hub-side USE sites (N17-obs227): a visibility refusal maps
    to `models.model_not_visible`, everything else keeps its code."""
    code = model_refusal_code(exc)
    return MODELS_NOT_VISIBLE_CODE if code == MODEL_NOT_VISIBLE_CODE else code


def model_catalog_entries(resources: Mapping[str, Any] | None = None,
                          workspace_id: str | None = None,
                          principal: model_catalog.Principal | None = None
                          ) -> list[dict[str, Any]]:
    """Every **enabled** chat model of the catalog with its table-derived traits, in
    catalog order (a row an administrator disabled — the model, its provider or, with
    ``workspace_id``, the workspace flag — is omitted: pickers and prose never offer it).

    The ONE source every prose rendering of the catalog is built from — the architect
    preset's skill bundle (rendered at install/repair time), the assistant protocol
    preamble (rendered per turn) and the proposal validation messages — so a model
    added to the registry reaches all of them without anyone editing prose. Never
    hard-code the ids in prose; call this.

    ``available`` (N-R6-obs1) is the one workspace-dependent trait: the provider's key is
    configured (`provider_configured`, ``resources`` = the workspace's resource map).
    D21 additive keys: ``kind`` (`chat`), ``source`` (`builtin|probed|manual`),
    ``enabled`` (always true in this list). ``principal`` (N17-obs203 §6) drops the rows of
    other members' personal providers; ``None`` keeps every row."""
    configured = configured_providers(resources)
    snapshot = model_catalog.current()
    capabilities = snapshot.legacy_capabilities()
    table = snapshot.legacy_models()
    entries: list[dict[str, Any]] = []
    for model_id in snapshot.allowed_ids():
        if not snapshot.model_enabled(model_id, workspace_id):
            continue
        if not snapshot.model_visible(model_id, principal):
            continue
        caps = capabilities.get(model_id, {})
        entry = table[model_id]
        row = snapshot.models[model_id]
        entries.append({
            "model_id": model_id,
            "default": row.default,
            "label": entry["label"],
            "available": configured[str(entry["provider"])],
            # D21 additive: the registry dimension (consumed by /api/capabilities)
            "kind": row.kind,
            "source": row.source,
            "enabled": True,
            # N-R6: provider dimension (CN additive — consumed by /api/capabilities and the
            # architect prose; a Bedrock-only reader may ignore every key below)
            "provider": entry["provider"],
            "provider_kind": entry["kind"],
            "provider_label": snapshot.legacy_providers()[entry["provider"]]["label"],
            "api_model_name": entry["api_model_name"],
            "tools": bool(entry["tools"]),
            "streaming": bool(entry["streaming"]),
            "vision": bool(caps.get("vision", False)),
            "temperature": bool(caps.get("temperature", True)),
            "default_max_tokens": default_runtime_max_tokens(model_id),
            "default_judge_max_tokens": default_judge_max_tokens(model_id),
            "max_output_tokens": model_max_output_tokens(model_id),
        })
    return entries


# What the architect prose says about a model whose provider has no key here (N-R6-obs1);
# the prompt rule next to the list tells the architect never to propose such a model.
MODEL_UNAVAILABLE_MARKER = "(not configured in this workspace — not selectable)"


def model_catalog_lines(resources: Mapping[str, Any] | None = None,
                        workspace_id: str | None = None,
                        principal: model_catalog.Principal | None = None,
                        *, tag_default: bool = True) -> list[str]:
    """One Markdown bullet per catalog model: the exact id plus its traits in plain
    words (image input, temperature support, default output ceiling, judge budget). A
    model of an unconfigured provider carries `MODEL_UNAVAILABLE_MARKER` after its id and,
    as its first trait, the provider's SSM parameter the operator must store — so the
    architect can name it when it refuses the model (N-ARCH-bug3); the parameter comes
    from the provider row, never from prose. Prose is unchanged for built-in rows (D21 parity).

    N17-obs298: " (default)" marks the effective `agent_default` role model — the same id
    `assistant.service.protocol_preamble()` names as the `model_id` default — never the
    registry's default-row flag (`entry["default"]`, the provider page's 默认 column). A
    role model the visible catalog does not list tags nothing. `tag_default=False` renders
    no tag (the install-time skill catalog, which would go stale when the role moves)."""
    lines: list[str] = []
    role_model = agent_default_model_id() if tag_default else None
    for entry in model_catalog_entries(resources, workspace_id, principal):
        traits: list[str] = []
        if not entry["available"]:
            provider = provider_row(str(entry["provider"]))
            traits.append(
                f"selectable once the operator stores the {entry['provider_label']} API key "
                f"as SSM SecureString parameter `{provider['ssm_parameter']}` (the host "
                f"exports it as `{provider['api_key_env']}`) and re-runs bootstrap"
            )
        traits += [
            "provider: " + entry["provider_label"] + (
                "" if entry["provider_kind"] == PROVIDER_BEDROCK
                else f" (OpenAI-compatible endpoint, model `{entry['api_model_name']}`)"),
            "tool calling: " + ("yes" if entry["tools"]
                                else "NO — usable as an LLM judge only, never as an agent model"),
            "image input: " + ("yes (native vision)" if entry["vision"] else "no (text only)"),
            "accepts the `temperature` parameter: " + ("yes" if entry["temperature"] else "no"),
            f"default per-call output ceiling {entry['default_max_tokens']} tokens",
        ]
        if entry["default_judge_max_tokens"]:
            traits.append("as an LLM judge / optimizer its output budget starts at "
                          f"{entry['default_judge_max_tokens']} tokens")
        if entry["max_output_tokens"]:
            traits.append(f"documented hard output limit {entry['max_output_tokens']} tokens")
        tag = " (default)" if entry["model_id"] == role_model else ""
        if not entry["available"]:
            tag += f" {MODEL_UNAVAILABLE_MARKER}"
        lines.append(f"- `{entry['model_id']}`{tag} — " + "; ".join(traits))
    return lines


# Strands Studio canvas flows carry their models on the nodes, not in `model_id`
# (the publish sends none). Node types that carry a model and the provider string
# mirror frontend/src/studio (`PropertyPanel.tsx`, `lib/code-generator.ts`).
STUDIO_MODEL_NODE_TYPES = frozenset({"agent", "orchestrator-agent", "swarm"})
STUDIO_BEDROCK_PROVIDER = "AWS Bedrock"


# AgentCore Runtime ``environmentVariables`` cap (CreateAgentRuntime / UpdateAgentRuntime:
# 50 entries). The platform's own keys share it (``deployer/environment.PLATFORM_ENV_KEYS``,
# every key the deployers may inject — that module imports this one, so the count is repeated
# here and ``tests/test_runtime_environment.py`` pins the two equal and the sum = the limit),
# so ``AgentSpec.env`` refuses more than the remainder up front (N16-obs72, lead ruling
# 2026-10-19) instead of running the pipeline into a ValidationException.
RUNTIME_ENV_LIMIT = 50
PLATFORM_ENV_RESERVED = 28  # N16-obs90 added LAUNCHPAD_A2A_ACTOR_KEY (was 27)
AGENT_ENV_MAX_VARS = RUNTIME_ENV_LIMIT - PLATFORM_ENV_RESERVED  # 22


class ModelRefusal(ValueError):
    """A model the catalog refuses. A plain ``ValueError`` for pydantic (the message starts
    with the code, as before); ``code`` lets a route answer 422 with the right envelope."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.reason = message


def model_refusal_code(exc: BaseException) -> str:
    """The envelope code for a refusal raised by the asserts below (`model.not_allowed`
    for any other ValueError, the historical mapping)."""
    return str(getattr(exc, "code", "") or MODEL_NOT_ALLOWED_CODE)


def assert_model_allowed(model_id: str, where: str = "model_id") -> str:
    """The one place the catalog is enforced; the message starts with the error code."""
    allowed = allowed_model_ids()
    if model_id not in allowed:
        raise ModelRefusal(
            MODEL_NOT_ALLOWED_CODE,
            f"{where} {model_id!r} is not available in this "
            f"build — allowed: {', '.join(allowed)}",
        )
    return model_id


def assert_agent_model_allowed(model_id: str, where: str = "model_id") -> str:
    """`assert_model_allowed` plus the agent contract: the model must support tool calling
    (N-R6). A judge-only model is still a catalog model — judges use `assert_model_allowed`."""
    assert_model_allowed(model_id, where)
    if not supports_tools(model_id):
        raise ModelRefusal(
            MODEL_TOOLS_UNSUPPORTED_CODE,
            f"{where} {model_id!r} does not support tool "
            "calling, so it cannot drive an agent — pick a model whose catalog entry lists "
            "tools (it stays available as an evaluation judge)",
        )
    return model_id


def assert_model_provider_configured(
    model_id: str, where: str = "model_id", resources: Mapping[str, Any] | None = None,
    workspace_id: str | None = None,
) -> str:
    """N-R6-obs1: refuse a catalog model whose provider has no API key in this workspace,
    naming the SSM parameter to create. A request-boundary guard (routes, judges,
    recommendations, the architect) — deliberately NOT part of `AgentSpec` validation, so
    a stored spec keeps parsing when its provider's key is rotated away.

    D21: a row an administrator disabled (the model, its provider, or the workspace flag)
    is refused here too, with the historical `model.not_allowed` code — stored specs on it
    still parse; only new picks are refused (plan G4)."""
    if model_id in model_catalog_table() and not model_enabled(model_id, workspace_id):
        raise ModelRefusal(
            MODEL_NOT_ALLOWED_CODE,
            f"{where} {model_id!r} is disabled in the model registry"
            + (" for this workspace" if workspace_id and model_catalog.current().model_enabled(
                model_id) else "")
            + " — an administrator can re-enable it under Settings → Model Providers; "
            "until then pick an enabled model",
        )
    if not model_available(model_id, resources, workspace_id):
        row = model_provider(model_id)
        raise ModelRefusal(
            MODEL_PROVIDER_UNCONFIGURED_CODE,
            f"{where} {model_id!r} runs on {row['label']}, which is not configured in this "
            f"workspace — store the {row['label']} API key as SSM SecureString parameter "
            f"{row['ssm_parameter']} (the host exports it as {row['api_key_env']}) and re-run "
            "`make bootstrap`; until then pick a model of a configured provider",
        )
    return model_id


def assert_model_available(
    model_id: str, where: str = "model_id", resources: Mapping[str, Any] | None = None,
    workspace_id: str | None = None,
) -> str:
    """Catalog membership AND a configured, enabled provider/row — what every member-facing
    model choice is held to (judge, actor, recommendation/optimizer model)."""
    assert_model_allowed(model_id, where)
    return assert_model_provider_configured(model_id, where, resources, workspace_id)


def assert_agent_model_available(
    model_id: str, where: str = "model_id", resources: Mapping[str, Any] | None = None,
    workspace_id: str | None = None,
) -> str:
    """`assert_agent_model_allowed` AND a configured provider (agent create / redeploy)."""
    assert_agent_model_allowed(model_id, where)
    return assert_model_provider_configured(model_id, where, resources, workspace_id)


def spec_model_ids(spec: "AgentSpec") -> list[tuple[str, str]]:
    """Every (model_id, where) an incoming spec could invoke: `model_id`, BYOC
    `allowed_models`, Studio node models."""
    out = [(spec.model_id, "model_id")]
    if spec.byoc and spec.byoc.allowed_models:
        out += [(m, "byoc.allowed_models entry") for m in spec.byoc.allowed_models]
    if spec.method == "studio":
        out += [(m, "studio node modelId") for m in studio_node_models(spec.studio_flow)]
    return out


def assert_spec_models_available(spec: "AgentSpec",
                                 resources: Mapping[str, Any] | None = None,
                                 workspace_id: str | None = None,
                                 principal: model_catalog.Principal | None = None) -> None:
    """Every model an incoming spec could invoke (`model_id`, BYOC `allowed_models`,
    Studio node models) has a configured, enabled provider — the request-boundary twin of
    `AgentSpec._model_catalog` (which already holds them to the catalog). ``workspace_id``
    (D21, optional) additionally applies the workspace's enable flags; ``principal``
    (N17-obs203 §6) refuses a model of another member's personal provider FIRST
    (`model.not_visible` — the caller may not even learn whether it is configured)."""
    for model_id, where in spec_model_ids(spec):
        assert_model_visible(model_id, principal, where)
    for model_id, where in spec_model_ids(spec):
        assert_model_provider_configured(model_id, where, resources, workspace_id)


def studio_node_models(flow: Any) -> list[str]:
    """Explicit catalog model ids on a canvas flow's model-bearing nodes, in order.

    The canvas's catalog branch keeps the historical provider value `AWS Bedrock`
    (STUDIO_BEDROCK_PROVIDER) for every catalog model — Bedrock or OpenAI-compatible
    (N-R6-obs2); the generated `BedrockModel(model_id=…)` is rebound by the Studio adapter
    to the shared provider dispatch. A node without `modelId` generates the canvas default
    (= DEFAULT_MODEL_ID) and a free-text provider's `modelName` is not a catalog id — neither
    is checked here.
    """
    if not isinstance(flow, dict):
        return []
    ids: list[str] = []
    for node in flow.get("nodes") or []:
        if not isinstance(node, dict) or node.get("type") not in STUDIO_MODEL_NODE_TYPES:
            continue
        data = node.get("data") if isinstance(node.get("data"), dict) else {}
        provider = data.get("modelProvider") or STUDIO_BEDROCK_PROVIDER
        model_id = data.get("modelId")
        if provider == STUDIO_BEDROCK_PROVIDER and isinstance(model_id, str) and model_id:
            ids.append(model_id)
    return ids


Method = Literal["harness", "zip_runtime", "studio", "byoc"]

# Which model-hosting surface the model_id belongs to. Only native Bedrock is
# supported in this build (CN has no third-party model-hosting surface).
ModelSource = Literal["bedrock"]

# Per-model-call reasoning effort for OpenAI GPT-5.x / GPT-6 models (same effort
# scale). Consumed by the harness method only and only for an OpenAI model reached
# through native Bedrock (``model_source="bedrock"``), where the platform knows the exact Converse
# pass-through shape (``additionalModelRequestFields.reasoning.effort``). Absent ⇒
# nothing is sent and the model's own default applies.
ReasoningEffort = Literal["low", "medium", "high"]

# Ceiling for ``AgentSpec.max_tokens`` (per model call). The service model bounds
# ``bedrockModelConfig.maxTokens`` from below only; a too-large value surfaces as a
# model-side validation error, so this cap is a sanity bound, not a model catalogue.
MAX_TOKENS_CEILING = 131072
# Per-call ceiling a Harness on an OpenAI GPT model gets when the spec sets none: the
# service default is too small for their hidden reasoning (live 2026-10-04: a GPT-6
# call reasoned for 95 s and stopped at max_tokens before writing any answer). Other
# families keep the service default — Bedrock rejects a value above a model's limit.
DEFAULT_GPT_MAX_TOKENS = 65536


def is_openai_model_id(model_id: str) -> bool:
    """``openai.<model>`` bare id or any inference-profile-prefixed form of it."""
    return "openai." in model_id


# The renderers' placeholder marker; refused in member-supplied text
# (AgentSpec._no_reserved_placeholder).
RESERVED_PLACEHOLDER = "__LAUNCHPAD_"

# Methods that can mount managed knowledge bases. studio is excluded: its code is
# generated by the vendored canvas, which has no place to inject a retrieval tool.
KB_METHODS = {"harness", "zip_runtime"}

# A named, platform-owned bundle of local @tool functions compiled into the
# generated runtime. Deliberately NOT a ToolRef.type member: every existing member
# denotes an *external* resource that drives IAM and deployer behaviour, while a
# toolkit drives neither — it only changes which functions the template emits.
# Adding it to ToolRef would put a member inside _uses_gateway()'s blast radius
# (app/services/agent_iam.py) that needs no workload token.
# See app/templates/toolkits/ and .trellis/spec/launchpad/agent-toolkits.md.
Toolkit = Literal["hr_assistant"]


_REST_TOOL_NAME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_-]{0,63}$")
# type=a2a (N11 r3-C): the ref name is the peer's registry record / agent name and becomes
# the ``ask_<name>`` tool; the ARN is the peer's AgentCore Runtime in any partition —
# matched structurally, never against a literal partition (docs/cn/interfaces/partition.md).
_A2A_TOOL_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,47}$")
_A2A_RUNTIME_ARN_RE = re.compile(
    r"^arn:aws[a-z-]*:bedrock-agentcore:[a-z0-9-]+:\d{12}:runtime/[A-Za-z0-9_.-]+$"
)
A2A_CONSUMER_METHODS = ("harness", "zip_runtime")
# the principal token's `aud` is the agent + its peers, at most 32 entries (contract I1)
A2A_MAX_PEERS = 31
CONNECTION_NAME_RE = r"^[a-zA-Z0-9\-_]{1,128}$"
# Tool-level auth is honoured by exactly these methods: zip_runtime renders the
# generated identity client, byoc gets the declaration (IAM + env) for its own code.
TOOL_AUTH_METHODS = ("zip_runtime", "byoc")
# Acting modes implemented today; the schema accepts the others so a stored spec
# stays loadable, and request-time validation refuses them (identity_providers).
SUPPORTED_ACTING_MODES = ("as_agent", "as_user")


class ApiKeyPlacement(BaseModel):
    """Where the runtime puts a fetched API key.

    The default renders ``Authorization: Bearer <key>``; any other header name
    sends the bare key (``x-api-key: <key>``), as does a query parameter.
    """

    in_: Literal["header", "query"] = Field(default="header", alias="in")
    name: str = Field(default="Authorization", min_length=1, max_length=128)

    # stored and echoed as {"in": ...} — the console's field name
    model_config = {"populate_by_name": True, "serialize_by_alias": True}


# legacy (earlier fork) flow → (kind, mode)
_LEGACY_FLOWS = {
    "M2M": ("oauth2", "as_agent"),
    "API_KEY": ("api_key", "as_agent"),
    "USER_FEDERATION": ("oauth2", "as_user"),
}


class ToolAuth(BaseModel):
    """Outbound auth for one tool, backed by a Connection (an AgentCore Identity
    credential provider in this workspace's token vault).

    ``connection`` names the credential provider (not its ARN). Whether it exists
    — and whether its kind matches ``kind`` — is validated at request time against
    the live vault (``services.identity_providers.validate_tool_auth``), not here:
    the valid name space lives in AWS, and a stored spec must keep validating
    after its Connection is deleted (the next save fails loudly instead).

    Declared on the schema on purpose: ``AgentSpec`` ignores unknown keys, so an
    undeclared ``auth`` would be dropped silently on every save.
    """

    connection: str = Field(pattern=CONNECTION_NAME_RE)
    kind: Literal["oauth2", "api_key"]
    mode: Literal["as_agent", "as_user", "obo"] = "as_agent"
    scopes: list[str] = Field(default_factory=list, max_length=32)
    audience: str | None = Field(default=None, max_length=512)
    # api_key only: key placement. None ⇒ Authorization: Bearer <key>.
    api_key: ApiKeyPlacement | None = None

    @model_validator(mode="before")
    @classmethod
    def _legacy_shape(cls, data: Any) -> Any:
        """Accept the earlier fork's ``{provider, flow}`` vocabulary."""
        if not isinstance(data, dict):
            return data
        data = dict(data)
        if "connection" not in data and "provider" in data:
            data["connection"] = data.pop("provider")
        flow = data.pop("flow", None)
        if flow in _LEGACY_FLOWS:
            kind, mode = _LEGACY_FLOWS[flow]
            data.setdefault("kind", kind)
            data.setdefault("mode", mode)
        data.pop("force_reauth", None)
        return data

    @model_validator(mode="after")
    def _kind_shape(self) -> "ToolAuth":
        if self.kind == "api_key":
            if self.scopes:
                raise ValueError("scopes apply to oauth2 Connections only")
            if self.audience:
                raise ValueError("audience applies to oauth2 Connections only")
            if self.mode != "as_agent":
                raise ValueError("an api_key Connection can only act as the agent")
        elif self.api_key is not None:
            raise ValueError("api_key placement applies to api_key Connections only")
        for scope in self.scopes:
            if not scope or len(scope) > 256:
                raise ValueError("each scope must be 1-256 characters")
        return self


class ToolRef(BaseModel):
    """Reference to a tool the agent may call.

    type=builtin → AgentCore builtin (code-interpreter / browser)
    type=gateway → MCP tool via the shared gateway (wired in phase 6)
    type=mcp     → remote MCP server URL
    type=rest    → plain HTTP API (config.url) called directly from the runtime;
                   carries ``auth`` when the API is protected
    type=a2a     → another Launchpad agent, by its APPROVED A2A registry record
                   (config.record_id, config.runtime_arn; optional url/agent_id/
                   description from the attachables catalog). Rendered as an
                   ``ask_<name>`` tool that calls the peer's Runtime through
                   InvokeAgentRuntime (N11 r3-C, N10v2-bug5); the consumer's role is
                   granted that ARN (agent_iam ``A2AInvokeMountedPeers``).
    """

    type: Literal["builtin", "gateway", "mcp", "rest", "a2a"]
    name: str
    config: dict[str, Any] = Field(default_factory=dict)
    # Tool-level outbound auth (rest/mcp only). type=gateway stays implicit: the
    # gateway authenticates its own targets (see routers/identity gateway targets).
    # Omitted from the dump when unset, so a stored spec without tool auth keeps
    # its exact pre-identity shape.
    auth: ToolAuth | None = Field(default=None, exclude_if=lambda v: v is None)

    @model_validator(mode="after")
    def _gateway_tool_list(self) -> "ToolRef":
        # type=gateway may carry ``config.tools``: the exact MCP tool names this
        # attachment may call (the record's declared tools, optionally narrowed).
        # Absent = legacy spec → the record's own ``<name>___*`` target scope.
        if self.type != "gateway" or "tools" not in self.config:
            return self
        tools = self.config["tools"]
        if (
            not isinstance(tools, list)
            or not 1 <= len(tools) <= MAX_GATEWAY_TOOLS
            or not all(isinstance(t, str) and _GATEWAY_TOOL_RE.match(t) for t in tools)
            or len(set(tools)) != len(tools)
        ):
            raise ValueError(
                f"gateway tool {self.name!r}: config.tools must be 1–{MAX_GATEWAY_TOOLS} "
                "distinct MCP tool names ([A-Za-z0-9_.-], ≤ 128 characters)"
            )
        return self

    @model_validator(mode="after")
    def _a2a_config(self) -> "ToolRef":
        """type=a2a must carry what the renderer and IAM need; the name becomes code."""
        if self.type != "a2a":
            return self
        if not _A2A_TOOL_NAME_RE.match(self.name):
            raise ValueError(
                f"a2a attachment name {self.name!r} must match {_A2A_TOOL_NAME_RE.pattern} "
                "(it is the peer's registry record name and becomes the ask_<name> tool)"
            )
        record_id = self.config.get("record_id")
        if not isinstance(record_id, str) or not record_id.strip():
            raise ValueError(f"a2a attachment {self.name!r}: config.record_id is required")
        arn = self.config.get("runtime_arn")
        if not isinstance(arn, str) or not _A2A_RUNTIME_ARN_RE.match(arn):
            raise ValueError(
                f"a2a attachment {self.name!r}: config.runtime_arn must be the peer's "
                "AgentCore Runtime ARN (arn:<partition>:bedrock-agentcore:<region>:"
                "<account>:runtime/<id>)"
            )
        return self


class KnowledgeBaseRef(BaseModel):
    """Managed Knowledge Base mounted on the agent via the shared KB gateway.

    name/description are denormalized at selection time — they feed the system
    prompt and detail views without a Bedrock round-trip.
    """

    kb_id: str = Field(min_length=1, max_length=32, pattern=r"^[A-Za-z0-9]+$")
    name: str = Field(default="", max_length=200)
    description: str = Field(default="", max_length=1000)


class MemoryConfig(BaseModel):
    short_term: bool = True
    long_term: bool = False
    # Per-agent AgentCore Memory selection. None ⇒ the workspace's shared
    # bootstrap memory (every spec written before this field existed reads back
    # that way). Ids follow the AgentCore `{name}-{suffix}` shape.
    memory_id: str | None = Field(
        default=None, max_length=128, pattern=r"^[a-zA-Z][a-zA-Z0-9_-]*$"
    )


class A2ASkill(BaseModel):
    """One AgentCard skill advertised by an A2A-protocol agent.

    Becomes both the A2A server's served card content and the Registry
    record's card `skills` entry — the routing surface other agents match on.
    """

    id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=1000)
    tags: list[str] = Field(default_factory=list, max_length=10)


# AgentCore filesystemConfigurations mount-path contract: exactly one level under
# /mnt, 6-200 chars (see task research: runtime-filesystem-configurations).
MOUNT_PATH_RE = r"^/mnt/[a-zA-Z0-9._-]+/?$"

_S3_FILES_AP_RE = re.compile(
    r"^arn:aws[\w-]*:s3files:[^:]+:\d{12}:file-system/[^/]+/access-point/.+$"
)
_EFS_AP_RE = re.compile(
    r"^arn:aws[\w-]*:elasticfilesystem:[^:]+:\d{12}:access-point/.+$"
)


class SessionStorageFs(BaseModel):
    """Managed session storage (Preview) — per-session, reset on version update."""

    mount_path: str = Field(
        default="/mnt/workspace", pattern=MOUNT_PATH_RE, min_length=6, max_length=200
    )


class ByoMount(BaseModel):
    """Bring-your-own mount: an S3 Files or EFS access point."""

    access_point_arn: str = Field(min_length=20, max_length=2048)
    mount_path: str = Field(pattern=MOUNT_PATH_RE, min_length=6, max_length=200)


class VpcNetwork(BaseModel):
    """networkModeConfig for networkMode=VPC — required by BYO file systems."""

    subnets: list[str] = Field(min_length=1, max_length=8)
    security_groups: list[str] = Field(min_length=1, max_length=5)


class FilesystemConfig(BaseModel):
    """AgentCore Runtime filesystemConfigurations (≤1 session, ≤2 s3, ≤2 efs).

    session_storage defaults ON; an explicit JSON null disables it.
    """

    session_storage: SessionStorageFs | None = Field(default_factory=SessionStorageFs)
    s3_files: list[ByoMount] = Field(default_factory=list, max_length=2)
    efs: list[ByoMount] = Field(default_factory=list, max_length=2)

    @property
    def byo(self) -> bool:
        return bool(self.s3_files or self.efs)

    @model_validator(mode="after")
    def _check(self) -> "FilesystemConfig":
        paths = [m.mount_path.rstrip("/") for m in (*self.s3_files, *self.efs)]
        if self.session_storage:
            paths.append(self.session_storage.mount_path.rstrip("/"))
        if len(paths) != len(set(paths)):
            raise ValueError("filesystem mount paths must be unique")
        for mount in self.s3_files:
            if not _S3_FILES_AP_RE.match(mount.access_point_arn):
                raise ValueError(
                    f"'{mount.access_point_arn}' is not an S3 Files access point ARN"
                )
        for mount in self.efs:
            if not _EFS_AP_RE.match(mount.access_point_arn):
                raise ValueError(
                    f"'{mount.access_point_arn}' is not an EFS access point ARN"
                )
        return self


# HarnessAllowedTool from the bedrock-agentcore-control service model (2023-06-05):
# pattern ``\*|@?[^/]+(/[^/]+)?``, length 1–64.
_ALLOWED_TOOL_RE = re.compile(r"^(\*|@?[^/]+(/[^/]+)?)$")
_GATEWAY_TOOL_RE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
MAX_GATEWAY_TOOLS = 250
ALLOWED_TOOL_MAX_LEN = 64

# ── BYOC (bring your own code) ──────────────────────────────────────────────

# What the uploaded/BYO artifact is. code_zip → direct-code Runtime
# (codeConfiguration); container_source → CodeBuild → ECR → Runtime;
# container_image → an existing private-ECR image, no build.
ByocArtifactKind = Literal["code_zip", "container_source", "container_image"]

# AgentManagedRuntimeType members the console offers (the service model also
# carries PYTHON_3_14/NODE_22; Python-only here because requirements resolution
# targets CPython wheels).
ByocPythonVersion = Literal["PYTHON_3_10", "PYTHON_3_11", "PYTHON_3_12", "PYTHON_3_13"]

# Ceiling for ``ByocConfig.allowed_models`` — each entry becomes one or two ARNs
# recorded against this spec, so the bound is a sanity cap, not a model catalogue.
BYOC_ALLOWED_MODELS_MAX = 20
INFERENCE_PROFILE_PREFIXES = ("global.", "us.", "eu.", "apac.")
_BYOC_MODEL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.:-]*$")
_BYOC_MODEL_ARN_RE = re.compile(
    r"^arn:aws(?:-cn|-us-gov)?:bedrock:[a-z0-9-]+:(?P<account>\d{12})?:"
    r"(?P<kind>foundation-model|inference-profile)/(?P<id>[A-Za-z0-9][A-Za-z0-9.:-]*)$"
)


def byoc_model_target(model_id: str) -> tuple[str, str]:
    """Validate a literal BYOC model selection and return (resource type, id).

    Custom identifiers remain usable without catalog lookup, but can never
    insert IAM wildcards. Profile targets must be derivable from a system
    inference-profile id; application profiles require a separate live lookup.
    """
    if is_openai_compatible_model(model_id):
        # N-R6: a catalog model on an OpenAI-compatible endpoint — no Bedrock resource at
        # all; the user code receives the Launchpad id and resolves the endpoint/key the
        # same way the generated runtimes do (docs/cn/interfaces/model-providers.md).
        return PROVIDER_OPENAI_COMPATIBLE, api_model_name(model_id)
    if model_id.startswith("arn:"):
        match = _BYOC_MODEL_ARN_RE.fullmatch(model_id)
        if match is None or (
            match["kind"] == "foundation-model" and match["account"] is not None
        ):
            raise ValueError(
                "BYOC models must be literal model IDs, foundation-model ARNs, "
                "or system inference-profile ARNs"
            )
        kind, target = match["kind"], match["id"]
    else:
        if _BYOC_MODEL_ID_RE.fullmatch(model_id) is None:
            raise ValueError("BYOC model IDs cannot contain wildcards, spaces, or IAM variables")
        target = model_id
        kind = (
            "inference-profile" if target.startswith(INFERENCE_PROFILE_PREFIXES)
            else "foundation-model"
        )
    if kind == "inference-profile":
        prefix = next((p for p in INFERENCE_PROFILE_PREFIXES if target.startswith(p)), "")
        if not prefix or len(target) == len(prefix):
            raise ValueError("BYOC inference profiles must use a global, us, eu, or apac model ID")
    return kind, target

# Private ECR in *some* account/region — the workspace match (this account, this
# region) is a resource check, done against the WorkspaceContext at request time,
# not here. Public registries (public.ecr.aws, docker.io) never match.
_PRIVATE_ECR_IMAGE_RE = re.compile(
    r"^(\d{12})\.dkr\.ecr\.([a-z0-9-]+)\.amazonaws\.com(?:\.cn)?/"  # cn-lint: allow
    r"[a-z0-9._/-]+(:[A-Za-z0-9._-]+|@sha256:[0-9a-f]{64})$"
)


def parse_ecr_image_uri(uri: str) -> tuple[str, str] | None:
    """(account_id, region) of a private-ECR image URI, or None if not one."""
    match = _PRIVATE_ECR_IMAGE_RE.match(uri)
    return (match.group(1), match.group(2)) if match else None


class ByocProvenance(BaseModel):
    """Who uploaded what — stamped by the SERVER from the stored upload manifest
    (or, for container_image, from the describe_images check), never taken from
    the client. Rendered on the agent detail page."""

    sha256: str = ""
    size_bytes: int = 0
    original_filename: str = Field(default="", max_length=255)
    uploaded_by: str = Field(default="", max_length=128)
    uploaded_at: str = Field(default="", max_length=64)


class ByocTelemetry(BaseModel):
    """N17-obs410: whether the packaged code_zip carries the ADOT launcher — stamped by the
    SERVER at package time from the built bundle (never taken from the client); the deploy
    starts the runtime through ``opentelemetry-instrument`` only when ``instrumented``."""

    instrumented: bool = False
    reason: str = Field(default="", max_length=300)
    # N17-obs465 (container kinds): the aws-cn trace exporter layer's decision —
    # applied | not_aws_cn | opted_out | not_node | unknown_runtime (empty: not decided yet)
    cn_exporter: str = Field(default="", max_length=32)
    runtime: str = Field(default="", max_length=16)  # node | python | "" (unknown)
    # the member's own image and the derived one that is deployed (`<repo>@sha256:…` URIs)
    member_image: str = Field(default="", max_length=512)
    layer_image: str = Field(default="", max_length=512)


class ByocConfig(BaseModel):
    """User-code deployment settings for method="byoc"."""

    artifact_kind: ByocArtifactKind
    # the staged zip (POST /api/agents/uploads) — code_zip / container_source
    upload_id: str | None = Field(default=None, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    # an existing image — container_image only
    image_uri: str | None = Field(default=None, max_length=512)
    # code_zip only: must exist at the zip root (or the single top-level dir)
    entrypoint: str = Field(default="main.py", max_length=255)
    python_version: ByocPythonVersion = "PYTHON_3_13"
    # code_zip: resolve requirements.txt in the zip for linux/aarch64 at package
    # time (skipped when the zip has none). Never executes user code.
    install_requirements: bool = True
    # Documentation-only in v1: both values send {"prompt", "actor_id"}; "raw"
    # merely acknowledges the contract warning in the console. No payload mapper.
    invoke_contract: Literal["launchpad_prompt", "raw"] = "launchpad_prompt"
    # Every Bedrock model this agent's code may invoke (foundation-model or
    # inference-profile ids, or their supported ARNs). Custom ids are checked
    # for literal IAM-safe shape, without requiring a catalog match. entry [0]
    # is the PRIMARY model (= spec.model_id, injected as env MODEL_ID). None ⇒
    # [spec.model_id] — every spec written before this field existed reads
    # back unchanged.
    allowed_models: list[str] | None = Field(default=None, min_length=1,
                                             max_length=BYOC_ALLOWED_MODELS_MAX)
    provenance: ByocProvenance | None = None
    telemetry: ByocTelemetry | None = None  # server-stamped (N17-obs410)
    # N17-obs465: in aws-cn the platform adds its trace exporter layer on top of a Node
    # container agent's image (container_source / container_image); false opts out
    cn_trace_exporter: bool = True

    @model_validator(mode="after")
    def _allowed_models_shape(self) -> "ByocConfig":
        if self.allowed_models is None:
            return self
        cleaned = [model_id.strip() for model_id in self.allowed_models]
        if any(not model_id for model_id in cleaned):
            raise ValueError("allowed_models entries cannot be empty")
        if len(cleaned) != len(set(cleaned)):
            raise ValueError("allowed_models entries must be unique")
        for model_id in cleaned:
            byoc_model_target(model_id)
        self.allowed_models = cleaned
        return self

    @model_validator(mode="after")
    def _artifact_inputs(self) -> "ByocConfig":
        if self.artifact_kind in ("code_zip", "container_source"):
            if not self.upload_id:
                raise ValueError(
                    f"byoc artifact_kind={self.artifact_kind} requires upload_id "
                    "(upload the zip via POST /api/agents/uploads first)"
                )
            if self.image_uri:
                raise ValueError("image_uri applies to artifact_kind=container_image only")
        else:  # container_image
            if not self.image_uri:
                raise ValueError("byoc artifact_kind=container_image requires image_uri")
            if self.upload_id:
                raise ValueError("upload_id applies to the zip artifact kinds only")
            if parse_ecr_image_uri(self.image_uri) is None:
                raise ValueError(
                    "image_uri must be a private ECR image in this account "
                    "(<account>.dkr.ecr.<region>.<dns-suffix>/<repo>:<tag>); "
                    "public registries are refused"
                )
        if self.artifact_kind == "code_zip":
            entry = self.entrypoint
            if not entry.endswith(".py") or entry.startswith("/") or ".." in entry:
                raise ValueError("entrypoint must be a relative .py path inside the zip")
        return self


class AgentSpec(BaseModel):
    @model_validator(mode="before")
    @classmethod
    def _creator_is_server_set(cls, data: Any) -> Any:
        # Family B step 1: `created_by` / `created_by_name` are recorded by the server from
        # the signed-in principal; a payload naming them is refused (422) with this
        # explanation, never honoured. `system_key` / `system` (N10v3 spoof rule) used to
        # be silently dropped; since N16-obs78 a request body (`AgentSpecRequest`) refuses
        # every unknown key the same way, while stored specs stay tolerant.
        if isinstance(data, dict):
            sent = [k for k in ("created_by", "created_by_name") if k in data]
            if sent:
                raise ValueError(
                    f"{', '.join(sent)}: the creator is recorded by the server and cannot "
                    "be set by the client"
                )
        return data

    name: str = Field(pattern=r"^[a-z][a-z0-9-]{2,47}$")
    method: Method
    # r15-e: the live platform default (`agent_default` role) — a stored spec always carries
    # its own id, so only a NEW spec submitted without one reads it.
    model_id: str = Field(default_factory=lambda: agent_default_model_id())
    # Only "bedrock" exists in this build; the model id itself is checked against
    # allowed_model_ids() by _model_catalog.
    model_source: ModelSource = "bedrock"
    # Global region the inference profile is entered from, and the AgentCore Identity
    # API-key credential provider (in THIS workspace's region) that holds the Global
    # Bedrock API key. Both reach the runtime as LAUNCHPAD_MODEL_REGION /
    # LAUNCHPAD_MODEL_CREDENTIAL (deployer/environment.py).
    model_region: str = Field(
        default=DEFAULT_MODEL_REGION, pattern=r"^[a-z]{2}(-gov)?-[a-z]+-\d$"
    )
    model_credential: str = Field(
        default=DEFAULT_MODEL_CREDENTIAL, pattern=r"^[A-Za-z0-9_-]{1,128}$"
    )
    # Per-model-call output ceiling (harness ``bedrockModelConfig.maxTokens``) and
    # reasoning effort (harness ``bedrockModelConfig.additionalParams``). Both are
    # harness-only and optional: every spec written before they existed reads back as
    # None and sends exactly the request it always did. Neither is a spend cap — the
    # harness loop is bounded separately by ``max_iterations`` / ``timeout_seconds``.
    max_tokens: int | None = Field(default=None, ge=1, le=MAX_TOKENS_CEILING)
    reasoning_effort: ReasoningEffort | None = None
    # Non-empty for every method except byoc (enforced in _byoc_constraints):
    # BYOC code carries its own prompt — the platform has no template to put one in.
    system_prompt: str = Field(default="", max_length=20000)
    # Durable production defaults (a recommendation's ACCEPT / a canary candidate writes
    # them). Config bundles may override these per request (optimizer rollouts).
    tool_description_overrides: dict[str, str] = Field(
        default_factory=dict, max_length=100
    )
    tools: list[ToolRef] = Field(default_factory=list)
    # Platform-owned local tool sets inlined into the generated agent (zip_runtime
    # only in v1). Rendered from this field at *generation* time — never
    # materialized into spec.code/code_bundle, so the artifact stays rebuildable from
    # the spec (a canary candidate mint re-renders it).
    toolkits: list[Toolkit] = Field(default_factory=list, max_length=2)
    skills: list[str] = Field(default_factory=list)
    # Native Harness capabilities are opt-in, independently of ToolConfig attachments.
    native_tools: list[Literal["shell", "file_operations"]] = Field(
        default_factory=list, max_length=2
    )
    # Harness ``allowedTools`` patterns (harness method only; the other methods ignore
    # it). None derives access from final attachments, Skills and native_tools.
    # An explicit list overrides derivation, including "*" and the empty list.
    # Pattern per the service model: ``*`` or ``@?[^/]+(/[^/]+)?``, ≤ 64 chars.
    allowed_tools: list[str] | None = Field(default=None, max_length=50)
    # extra pip requirements for zip_runtime/studio agents (on top of the template base set)
    requirements: list[str] = Field(default_factory=list)
    # pre-generated agent code (studio method) — bypasses the strands template
    code: str | None = Field(default=None, max_length=200000)
    # multi-file agent source (harness conversion) — relpath → content; must
    # contain main.py, which becomes the runtime entrypoint
    code_bundle: dict[str, str] | None = None
    # provenance of a harness→runtime conversion: {agent_id, agent_name, harness_arn}
    source_harness: dict[str, str] | None = None
    # per-capability wiring outcome of a conversion (memory/knowledge/…) — UI renders it
    conversion_notes: dict[str, str] | None = None
    # Strands Studio canvas graph {nodes, edges, graphMode} — persisted for later edit/re-publish
    studio_flow: dict[str, Any] | None = None
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    # N16-obs72: at most AGENT_ENV_MAX_VARS member variables (``_env_within_budget``) — a
    # larger map used to run the whole generate → package → provision pipeline (~130 s) and
    # die at CreateAgentRuntime with ValidationException (the Runtime's 50 includes the
    # platform's own keys). 422 here instead, naming the reserved budget.
    env: dict[str, str] = Field(default_factory=dict)
    max_iterations: int = Field(default=100, ge=1, le=100)
    timeout_seconds: int = Field(default=600, ge=10, le=3600)
    # AgentCore Runtime persistent storage. Session storage mounts on the harness,
    # zip_runtime and studio methods. BYO mounts (S3 Files / EFS) require explicit
    # VPC network configuration.
    filesystem: FilesystemConfig = Field(default_factory=FilesystemConfig)
    # VPC networkModeConfig; mandatory whenever a BYO file system is mounted
    network: VpcNetwork | None = None
    # Knowledge bases mounted on this agent (self-hosted on the workspace's OpenSearch
    # domain, N6): kb_search / kb_deep_search tools baked into the generated code run
    # k-NN queries against each KB's own index with the runtime execution role (no
    # gateway, no Bedrock KB). See app/templates/kb_support.py and
    # docs/cn/interfaces/kb-runtime.md.
    knowledge_bases: list[KnowledgeBaseRef] = Field(default_factory=list, max_length=10)
    # Runtime service protocol. "a2a" deploys a standard A2A JSON-RPC server
    # (port 9000, serverProtocol=A2A) — zip_runtime only in v1.
    protocol: Literal["http", "a2a"] = "http"
    # AgentCard skills served by the A2A server and published to the Registry
    a2a_skills: list[A2ASkill] = Field(default_factory=list, max_length=20)
    # Bring-your-own-code settings — required iff method="byoc"
    byoc: ByocConfig | None = None
    # Inbound authentication of this agent's Runtime. AWS_IAM only in this
    # build (GRAPH-LOOP-SPEC N3 / D9) — None and InboundAuth(mode="iam") are
    # equivalent; the field stays for the ledger snapshot shape
    # (``inbound_auth_mode`` / ``inbound_auth_config``) the deploy stage
    # writes onto the Agent row.
    inbound_auth: InboundAuth | None = None
    # D20 (P1-b, additive): policies this agent pins itself to, beyond the workspace-
    # scoped ones and the agent-scoped ones that list it (plan §4.2). Validated against
    # THIS workspace's `policies` on POST/PUT /api/agents (422 policy.unknown_policy);
    # a retired one is dropped with a warning in the deploy job log, never a failure.
    # The spec is a full-document replacement, so an ABSENT field on write means CLEAR,
    # never "keep the previous list".
    policy_ids: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("env")
    @classmethod
    def _env_within_budget(cls, value: dict[str, str]) -> dict[str, str]:
        if len(value) > AGENT_ENV_MAX_VARS:
            raise ValueError(
                f"env has {len(value)} variables; AgentCore Runtime allows "
                f"{RUNTIME_ENV_LIMIT} per runtime and the platform reserves "
                f"{PLATFORM_ENV_RESERVED} of them for its own keys — you may set up to "
                f"{AGENT_ENV_MAX_VARS}"
            )
        return value

    @field_validator("inbound_auth", mode="before")
    @classmethod
    def _degrade_legacy_inbound_auth(cls, value: Any) -> Any:
        """A pre-N3 stored spec may still carry ``{"mode": "jwt", "jwt": {...}}``
        (or any other shape this build no longer models) — reconstructing such a
        row (``AgentSpec(**agent.spec)`` on redeploy/resume) must degrade it to
        IAM rather than raise. Mirrors the lenient read path in
        ``schemas.inbound_auth.parse_inbound_auth``, which this field cannot
        call directly (it would bypass pydantic's own ``{"mode": "iam"}`` case).
        """
        if isinstance(value, dict) and value.get("mode") != "iam":
            return None
        return value

    @model_validator(mode="after")
    def _byoc_constraints(self) -> "AgentSpec":
        """BYOC scope for v1 — and the non-byoc system_prompt floor.

        The platform does not generate BYOC code, so it cannot wire toolkits,
        skills, tools or knowledge bases into it — those are refused rather than
        silently ignored. ``system_prompt`` stays mandatory for every other
        method (it used to be ``min_length=1`` on the field; the floor moved here
        so BYOC — whose code owns its own prompt — can omit it).
        """
        if self.method != "byoc":
            if not self.system_prompt:
                raise ValueError("system_prompt must not be empty")
            if self.byoc is not None:
                raise ValueError("byoc settings apply to method='byoc' only")
            return self
        if self.byoc is None:
            raise ValueError("method='byoc' requires the byoc settings block")
        if self.protocol != "http":
            raise ValueError("byoc agents speak the HTTP runtime contract only in v1")
        for field_name in ("toolkits", "skills", "knowledge_bases"):
            if getattr(self, field_name):
                raise ValueError(
                    f"{field_name} are not supported by the byoc method in v1 — "
                    "the platform does not generate this agent's code, so it "
                    "cannot wire them in; configure them inside your own code"
                )
        # byoc tools are outbound-auth declarations only: an entry carrying
        # ``auth`` drives IAM grants + LAUNCHPAD_OUTBOUND_AUTH, and the member's
        # own code performs the exchange. A tool without auth would ask the
        # platform to wire code it does not generate — refused.
        for tool in self.tools:
            if tool.auth is None:
                raise ValueError(
                    "tools are not supported by the byoc method in v1 unless they "
                    "carry an auth block — byoc tools are outbound-auth declarations "
                    "for your own code; wire the tool itself inside your code"
                )
        if self.code or self.code_bundle:
            raise ValueError(
                "byoc deploys the uploaded artifact — code/code_bundle are not used"
            )
        if self.requirements:
            raise ValueError(
                "byoc resolves requirements.txt from inside the uploaded zip — "
                "spec.requirements is not used"
            )
        models = self.byoc.allowed_models
        if models is not None:
            # model_id is the PRIMARY model and must equal allowed_models[0]:
            # a client that sends only the list gets the first entry as primary;
            # a client that sends both gets its model_id moved to the front so
            # the two fields can never disagree about which model is primary.
            if "model_id" not in self.model_fields_set:
                self.model_id = models[0]
            elif self.model_id not in models:
                raise ValueError(
                    f"model_id {self.model_id!r} must be one of byoc.allowed_models "
                    "— it is the primary model (env MODEL_ID)"
                )
            elif models[0] != self.model_id:
                models.remove(self.model_id)
                models.insert(0, self.model_id)
        # Omitting the list still authorizes model_id, so it needs the same
        # literal-resource validation as explicit allowlist entries.
        byoc_model_target(self.model_id)
        return self

    @model_validator(mode="after")
    def _model_catalog(self) -> "AgentSpec":
        """Spec C3 — every model this agent could invoke is in allowed_model_ids() and (N-R6)
        supports tool calling. A Studio node may carry any catalog model (N-R6-obs2): the
        adapter routes the canvas's `BedrockModel(model_id=…)` through the shared provider
        dispatch, so an OpenAI-compatible id is held to the same two checks as `model_id`
        (and to a configured provider at the request boundary — `assert_spec_models_available`)."""
        assert_agent_model_allowed(self.model_id)
        if self.byoc and self.byoc.allowed_models:
            for model_id in self.byoc.allowed_models:
                assert_agent_model_allowed(model_id, "byoc.allowed_models entry")
        if self.method == "studio":
            for model_id in studio_node_models(self.studio_flow):
                assert_agent_model_allowed(model_id, "studio node modelId")
        return self

    @property
    def allowed_model_ids(self) -> list[str]:
        """Every model this agent's execution role may invoke; entry [0] is the
        primary (= ``model_id``). Only byoc can carry more than one — every other
        method (and every byoc spec without the list) reads back as [model_id]."""
        if self.byoc and self.byoc.allowed_models:
            return list(self.byoc.allowed_models)
        return [self.model_id]

    @model_validator(mode="after")
    def _tool_auth_placement(self) -> "AgentSpec":
        """Where an ``auth`` block may sit, and what a ``rest`` tool must carry.

        ``rest`` tools and ``auth`` blocks are honoured by zip_runtime (the strands
        template renders the identity client) and byoc (declaration only), so every
        other method refuses them instead of deploying an agent whose console
        shows auth its runtime never performs. Harness/studio agents get
        outbound auth through a Gateway target bound to a Connection instead.
        """
        carriers = [t for t in self.tools if t.auth is not None or t.type == "rest"]
        if not carriers:
            return self
        if self.method not in TOOL_AUTH_METHODS:
            raise ValueError(
                "rest tools / tool auth are supported by the zip_runtime and byoc "
                "methods only — attach a Gateway target bound to a Connection for "
                "the other methods"
            )
        if self.protocol != "http":
            raise ValueError(
                "rest tools / tool auth are not supported by protocol=a2a runtimes"
            )
        if self.code or self.code_bundle:
            raise ValueError(
                "rest tools / tool auth cannot be combined with code/code_bundle — "
                "those specs deploy their own source and never render the identity "
                "client"
            )
        names = [t.name for t in carriers]
        if len(names) != len(set(names)):
            raise ValueError("rest / auth tool names must be unique")
        for tool in self.tools:
            if tool.type == "rest" and not _REST_TOOL_NAME_RE.match(tool.name):
                raise ValueError(
                    f"rest tool name {tool.name!r} must match {_REST_TOOL_NAME_RE.pattern} "
                    "(it becomes the model-facing function name)"
                )
            if tool.type == "rest" and self.method != "byoc":
                url = str((tool.config or {}).get("url") or "")
                if not url.startswith(("http://", "https://")):
                    raise ValueError(
                        f"rest tool '{tool.name}' requires config.url (http/https)"
                    )
            if tool.auth is not None and tool.type not in ("rest", "mcp"):
                raise ValueError(
                    f"tool '{tool.name}': auth applies to rest and mcp tools only — "
                    "gateway tools are authenticated by the gateway's own targets"
                )
        return self

    @model_validator(mode="after")
    def _no_reserved_placeholder(self) -> "AgentSpec":
        """``__LAUNCHPAD_`` is the renderers' placeholder marker (round 16 rev — moved from
        harness_local's post-render scan, where a prompt carrying it failed the job's first
        stage): refused in EVERY member-supplied string the templates may embed — the whole
        dumped spec walked recursively (prompt, tool names / configs incl. mcp URLs and gateway
        tool patterns, skills URIs, KB / A2A text, …) except ``env`` values, which are never
        rendered — naming the dotted path. Legitimate text (``LAUNCHPAD``, ``LAUNCHPAD_MODE``,
        ``__LAUNCHPAD`` without the trailing underscore, lower case) passes."""

        def walk(value: Any, path: str) -> None:
            if isinstance(value, str):
                if RESERVED_PLACEHOLDER in value:
                    raise ValueError(
                        f"{path}: the substring {RESERVED_PLACEHOLDER!r} is reserved for "
                        "template placeholders and refused in member-supplied text"
                    )
            elif isinstance(value, dict):
                for key, item in value.items():
                    walk(str(key), f"{path}.{key}" if path else str(key))
                    walk(item, f"{path}.{key}" if path else str(key))
            elif isinstance(value, list | tuple):
                for i, item in enumerate(value):
                    walk(item, f"{path}[{i}]")

        for field_name, value in self.model_dump().items():
            if field_name == "env":
                continue  # runtime environment values are passed through, never rendered
            walk(value, field_name)
        return self

    @model_validator(mode="after")
    def _mcp_tools_need_url(self) -> "AgentSpec":
        """A remote MCP server is its URL (round 16 — moved from the Harness renderer,
        harness_local._synthetic_mcp_tools, where it failed the job's first stage)."""
        for tool in self.tools:
            if tool.type != "mcp":
                continue
            url = str((tool.config or {}).get("url") or "")
            if not url.startswith(("http://", "https://")):
                raise ValueError(f"mcp tool {tool.name!r}: config.url (http/https) is required")
        return self

    @model_validator(mode="after")
    def _a2a_constraints(self) -> "AgentSpec":
        if self.protocol == "a2a" and self.method != "zip_runtime":
            raise ValueError(
                "the A2A protocol is only supported by the zip_runtime method in v1"
            )
        if self.protocol == "a2a" and (self.code or self.code_bundle):
            raise ValueError(
                "protocol=a2a always uses the platform A2A template — "
                "custom code/code_bundle is not supported in v1"
            )
        if self.a2a_skills and self.protocol != "a2a":
            raise ValueError("a2a_skills require protocol=a2a")
        if self.protocol == "a2a":
            ids = [s.id for s in self.a2a_skills]
            if len(ids) != len(set(ids)):
                raise ValueError("a2a_skills ids must be unique")
        return self

    @model_validator(mode="after")
    def _a2a_tool_constraints(self) -> "AgentSpec":
        """type=a2a attachments (N11 r3-C): only where the platform renders the client.

        The ``ask_<agent>`` tool is baked into generated source, so a method that deploys
        its own code (byoc — refused earlier by ``_byoc_constraints``; studio / converted
        ``code`` / ``code_bundle``) and the A2A *server* template (no tool graft channel)
        cannot carry one; refused rather than silently dropped.
        """
        peers = [t for t in self.tools if t.type == "a2a"]
        if not peers:
            return self
        if self.method not in A2A_CONSUMER_METHODS:
            raise ValueError(
                "a2a agent attachments are supported by the harness and zip_runtime methods "
                "only (the platform generates no code for the other methods)"
            )
        if self.protocol != "http":
            raise ValueError(
                "a2a agent attachments are not supported by protocol=a2a runtimes — the A2A "
                "server template carries no tool graft channel"
            )
        if self.code or self.code_bundle:
            raise ValueError(
                "a2a agent attachments cannot be combined with code/code_bundle — those "
                "specs deploy their own source and never render the A2A client"
            )
        names = [t.name for t in peers]
        if len(names) != len(set(names)):
            raise ValueError("a2a attachment names must be unique")
        if len(peers) > A2A_MAX_PEERS:
            raise ValueError(f"at most {A2A_MAX_PEERS} a2a agent attachments per agent")
        return self

    @model_validator(mode="after")
    def _inference_knobs_supported(self) -> "AgentSpec":
        """Refuse a knob the target cannot honour instead of dropping it silently.

        ``max_tokens`` maps only onto the harness request. ``reasoning_effort``
        is an OpenAI-specific request field: sent to a Claude, Nova or other model it
        is either rejected by the provider or ignored, so it is accepted only for an
        OpenAI model on native Bedrock (Converse), the one pass-through shape the
        platform has verified.
        """
        if self.max_tokens is not None and self.method != "harness":
            raise ValueError("max_tokens is supported by the harness method only")
        if self.reasoning_effort is None:
            return self
        if self.method != "harness":
            raise ValueError("reasoning_effort is supported by the harness method only")
        if not is_openai_model_id(self.model_id):
            raise ValueError(
                f"reasoning_effort applies to OpenAI GPT-5.x / GPT-6 models only; "
                f"{self.model_id!r} is not an OpenAI model id"
            )
        return self

    @model_validator(mode="after")
    def _native_tools_supported(self) -> "AgentSpec":
        if self.native_tools and self.method != "harness":
            raise ValueError("native_tools is supported by the harness method only")
        if len(self.native_tools) != len(set(self.native_tools)):
            raise ValueError("native_tools must be unique")
        return self

    @model_validator(mode="after")
    def _allowed_tools_shape(self) -> "AgentSpec":
        for pattern in self.allowed_tools or []:
            if not 1 <= len(pattern) <= ALLOWED_TOOL_MAX_LEN:
                raise ValueError(
                    f"allowed_tools entry {pattern!r} must be 1–{ALLOWED_TOOL_MAX_LEN} characters"
                )
            if not _ALLOWED_TOOL_RE.match(pattern):
                raise ValueError(
                    f"allowed_tools entry {pattern!r} must match '*' or '@?name(/tool)?'"
                )
        return self

    @model_validator(mode="after")
    def _requirements_are_pinned(self) -> "AgentSpec":
        # Refused here rather than mid-deploy so the console shows the problem
        # before a build starts. Only the caller-supplied list is checked; the
        # platform's own lists keep their ranges by design and the package
        # stage's hashed lockfile is what makes the resolved set reproducible.
        assert_all_pinned(self.requirements)
        return self

    @model_validator(mode="after")
    def _byo_needs_vpc(self) -> "AgentSpec":
        if self.filesystem.byo and self.network is None:
            raise ValueError(
                "BYO file systems (S3 Files / EFS) require VPC network configuration"
            )
        return self

    @model_validator(mode="after")
    def _kb_method_supported(self) -> "AgentSpec":
        if self.knowledge_bases and self.method not in KB_METHODS:
            raise ValueError(
                "knowledge_bases are supported by the harness and zip_runtime "
                "methods; the Strands Studio canvas (studio) has no retrieval "
                "tool contract yet"
            )
        if self.knowledge_bases and self.protocol == "a2a":
            raise ValueError(
                "knowledge_bases are not supported by protocol=a2a runtimes "
                "(the A2A template carries no retrieval tool)"
            )
        return self

    @model_validator(mode="after")
    def _toolkits_supported(self) -> "AgentSpec":
        """Toolkits only exist where the Strands template actually renders.

        Rejected rather than silently dropped: a toolkit that quietly vanished
        would leave an agent whose console shows five tools and whose runtime has
        none, and whose readiness panel then waits forever for calls that can
        never happen.
        """
        if not self.toolkits:
            return self
        if self.method != "zip_runtime":
            raise ValueError(
                "toolkits are supported by the zip_runtime method only — the self-built "
                "harness template has no toolkit injection point, and the Strands "
                "Studio canvas generates its own"
            )
        if self.protocol != "http":
            raise ValueError(
                "toolkits are not supported by protocol=a2a runtimes "
                "(the A2A server uses its own template)"
            )
        if self.code or self.code_bundle:
            raise ValueError(
                "toolkits cannot be combined with code/code_bundle — those specs "
                "deploy their own source and never render the Strands template"
            )
        if len(self.toolkits) != len(set(self.toolkits)):
            raise ValueError("toolkits must be unique (a toolkit inlined twice "
                             "would redefine its functions)")
        return self

    @model_validator(mode="after")
    def _code_bundle_valid(self) -> "AgentSpec":
        if self.code_bundle is None:
            return self
        if self.code:
            raise ValueError("code and code_bundle are mutually exclusive")
        if "main.py" not in self.code_bundle:
            raise ValueError("code_bundle must contain main.py (the entrypoint)")
        if len(self.code_bundle) > 64:
            raise ValueError("code_bundle exceeds 64 files")
        total = 0
        for path, content in self.code_bundle.items():
            pure = PurePosixPath(path)
            if pure.is_absolute() or ".." in pure.parts or "\\" in path:
                raise ValueError(f"code_bundle path '{path}' is not a safe relative path")
            total += len(content.encode("utf-8"))
        if total > 1_000_000:
            raise ValueError("code_bundle exceeds 1MB of source")
        return self

    @model_validator(mode="after")
    def _tool_description_overrides_valid(self) -> "AgentSpec":
        for name, description in self.tool_description_overrides.items():
            if not name or len(name) > 200:
                raise ValueError("tool description override names must be 1-200 characters")
            if len(description) > 4000:
                raise ValueError(
                    f"tool description override for '{name}' exceeds 4000 characters"
                )
        return self


# ── creation-time validation against the method's declared domain (round 16) ─────────
# plans/CAPABILITY-MATRIX-DESIGN.md §1.2: the pydantic validators above refuse what is
# wrong for EVERY method; what one method cannot render is a property of the method's
# generate stage, declared once next to its ``register_method`` call
# (``deployer.pipeline.MethodCapabilities``) and checked here before any agent row, job or
# AWS call. The renderers keep their own checks as the last line of defence for stored specs.

METHOD_UNSUPPORTED_CODE = "agent.method_unsupported"
RESOURCE_MISSING_CODE = "workspace.resource_missing"
GATEWAY_TOOL_UNBOUND_CODE = "agent.gateway_tool_unbound"
# AgentCore gateway ids: `<name>-<10 lowercase alphanumerics>` (the same shape agent_iam
# grants on; anything else is refused HERE instead of being silently dropped there)
GATEWAY_ID_FULL_RE = re.compile(r"[0-9a-z](?:[0-9a-z-]{0,98})-[0-9a-z]{10}")

HOW_TO_FIX = {
    "artifacts_bucket": "run this workspace's bootstrap (make bootstrap) — it creates the "
                        "artifacts bucket every deploy packages into",
    "gateway_id": "run this workspace's bootstrap gateway stage, or remove the Gateway tool "
                  "from the spec",
    "codebuild_project": "run this workspace's bootstrap (it creates the CodeBuild project a "
                         "container_source upload is built with), or publish a code_zip / "
                         "container_image artifact instead",
}


class MethodUnsupported(ValueError):
    """An option outside the method's declared creation-time domain (422)."""

    def __init__(self, method: str, field: str, value: str, supported: Iterable[str]):
        self.method, self.field, self.value = method, field, value
        self.supported = sorted(supported)
        offered = ", ".join(self.supported) or "nothing"
        super().__init__(
            f"{field} = {value!r} is not supported by the {method} method (supported: {offered})"
        )

    def detail(self) -> dict[str, Any]:
        return {"method": self.method, "field": self.field, "value": self.value,
                "supported": self.supported}


class GatewayToolUnbound(ValueError):
    """N17-obs487 (V1): a ``type: gateway`` ToolRef that does not resolve to THIS workspace's
    Gateway (422). The runtime always talks to the workspace Gateway (``resources.gateway_url``)
    while IAM grants ``InvokeGateway`` on ``config.gateway_id``: an absent id deployed an
    ``active`` agent whose role had no grant (every MCP initialize a 403), a foreign id granted
    the role invoke on ANY gateway of the account. ``reason``: absent | malformed | foreign."""

    def __init__(self, name: str, reason: str, value: str | None, workspace_gateway_id: str):
        self.name, self.reason, self.value = name, reason, value
        self.workspace_gateway_id = workspace_gateway_id
        self.how_to_fix = (
            "attach the Gateway tool through the Registry (GET /api/registry/attachables → "
            "mcp_servers[].record_id + gateway_id) so config carries this workspace's "
            f"gateway_id {workspace_gateway_id!r}"
        )
        shown = {"absent": "config.gateway_id (or a resolvable config.record_id) is required",
                 "malformed": f"config.gateway_id {value!r} is not a gateway id",
                 "foreign": f"config.gateway_id {value!r} is not this workspace's gateway "
                            f"({workspace_gateway_id})"}[reason]
        super().__init__(f"gateway tool {name!r}: {shown}")

    def detail(self) -> dict[str, Any]:
        return {"field": "tools.gateway", "name": self.name, "reason": self.reason,
                "value": self.value, "workspace_gateway_id": self.workspace_gateway_id,
                "how_to_fix": self.how_to_fix}


class WorkspaceResourceMissing(ValueError):
    """A workspace resource the deploy would need from its first stage is absent (409)."""

    def __init__(self, method: str, resource: str):
        self.method, self.resource = method, resource
        self.how_to_fix = HOW_TO_FIX.get(resource, "run this workspace's bootstrap")
        super().__init__(
            f"{resource} missing from this workspace's resource map — {self.how_to_fix}"
        )

    def detail(self) -> dict[str, Any]:
        return {"method": self.method, "resource": self.resource, "how_to_fix": self.how_to_fix}


def validate_against_method(spec: "AgentSpec", caps: Any, workspace: Any) -> None:
    """Refuse ``spec`` where ``caps`` (the method's ``MethodCapabilities``) says its
    generate stage could not render it, or ``workspace`` (a ``WorkspaceContext``:
    ``resources`` / ``account_id`` / ``region``) lacks what the deploy needs.

    Raises ``MethodUnsupported`` (→ 422 ``agent.method_unsupported``) or
    ``WorkspaceResourceMissing`` (→ 409 ``workspace.resource_missing``). Reads nothing but
    its arguments — no ledger, no AWS — so every creation entry point (``POST /api/agents``,
    re-publish, the architect approval) calls it before its first write.
    """
    method = spec.method

    def refuse(field: str, value: str, supported: Iterable[str]) -> None:
        raise MethodUnsupported(method, field, value, supported)

    for tool in spec.tools:
        if tool.auth is not None:
            # an authenticated rest / mcp tool rides the method's identity channel
            if not caps.tool_auth_modes:
                refuse("tools.auth", tool.type, ())
            if tool.auth.mode not in caps.tool_auth_modes:
                refuse("tools.auth.mode", tool.auth.mode, caps.tool_auth_modes)
            continue
        if tool.type == "builtin":
            if tool.name not in caps.builtin_tools:
                refuse("tools.builtin", tool.name, caps.builtin_tools)
            continue
        if tool.type not in caps.tool_types:
            refuse("tools.type", tool.type, caps.tool_types)
        if tool.type == "mcp":
            # the earlier fork's config-level auth marker — the Harness mounts
            # unauthenticated remote MCP servers only (harness_local._synthetic_mcp_tools)
            legacy_auth = (tool.config or {}).get("auth")
            if legacy_auth not in (None, "none"):
                refuse("tools.auth", str(legacy_auth), caps.tool_auth_modes)
    for native in spec.native_tools:
        if native not in caps.native_tools:
            refuse("native_tools", native, caps.native_tools)
    if spec.protocol not in caps.protocols:
        refuse("protocol", spec.protocol, caps.protocols)
    if (spec.memory.short_term or spec.memory.long_term) and not caps.memory:
        refuse("memory", "on", ("off",))
    if spec.knowledge_bases and not caps.knowledge_bases:
        refuse("knowledge_bases", spec.knowledge_bases[0].kb_id, ())
    if spec.byoc is not None:
        kind = spec.byoc.artifact_kind
        if kind not in caps.byoc_kinds:
            refuse("byoc.artifact_kind", kind, caps.byoc_kinds)
        if kind == "container_image":
            parsed = parse_ecr_image_uri(spec.byoc.image_uri or "")
            here = f"{workspace.account_id}/{workspace.region}"
            if parsed is not None and f"{parsed[0]}/{parsed[1]}" != here:
                refuse("byoc.image_uri", f"{parsed[0]}/{parsed[1]}", (here,))
    resources = getattr(workspace, "resources", None) or {}
    needed = sorted(caps.requires)
    if "gateway" in caps.tool_types and any(t.type == "gateway" for t in spec.tools):
        needed.append("gateway_id")
    if spec.byoc is not None and spec.byoc.artifact_kind == "container_source":
        needed.append("codebuild_project")  # image_build.build_and_push_image needs it
    for key in needed:
        if not resources.get(key):
            raise WorkspaceResourceMissing(method, key)
    # N17-obs487 (V1): every Gateway ToolRef must be bound to THIS workspace's Gateway — the
    # runtime is wired to it, IAM grants whatever ``config.gateway_id`` says. Absent with a
    # ``record_id`` is left to ``method_validation.require_known_references`` (a ledger lookup
    # that normalises the stored spec); absent without one, malformed or foreign is refused here.
    workspace_gateway_id = str(resources.get("gateway_id") or "")
    for tool in spec.tools:
        if tool.type != "gateway":
            continue
        config = tool.config or {}
        gateway_id = config.get("gateway_id")
        if gateway_id in (None, ""):
            if not isinstance(config.get("record_id"), str) or not config["record_id"].strip():
                raise GatewayToolUnbound(tool.name, "absent", None, workspace_gateway_id)
            continue
        if isinstance(gateway_id, str) and gateway_id == workspace_gateway_id:
            continue  # bound to this workspace's Gateway, whatever shape bootstrap stored
        # not this workspace's: "malformed" when it could never be a gateway id, else "foreign"
        if not isinstance(gateway_id, str) or not GATEWAY_ID_FULL_RE.fullmatch(gateway_id):
            raise GatewayToolUnbound(tool.name, "malformed", str(gateway_id), workspace_gateway_id)
        # Other account gateways are resolved against AWS by method_validation before writes.


class AgentSpecRequest(AgentSpec):
    """``AgentSpec`` as a REQUEST body (N16-obs78): an unknown top-level key is 422
    ``validation.invalid_request`` naming it (``extra_forbidden``) instead of being
    silently dropped — the console builds every key it sends (``AgentSpecInput`` /
    ``republishSpec``'s carried list), so a stray one is a bug, like ``OnlineConfigPatch``
    / ``UserPatch``. Specs READ BACK from the ledger keep parsing through the tolerant
    ``AgentSpec`` (an old row may carry a key a later build retired); a stored spec's
    ``model_dump()`` round-trips through this class (the architect proposal path)."""

    model_config = ConfigDict(extra="forbid")


class InvokeRequest(AttachmentRequest):
    session_id: str | None = None
    actor_id: str = "default"


class InvokeResponse(BaseModel):
    text: str
    session_id: str
    latency_ms: int


def _clean_resource_ids(ids: list[str], label: str) -> list[str]:
    cleaned = list(dict.fromkeys(resource_id.strip() for resource_id in ids))
    if any(not resource_id for resource_id in cleaned):
        raise ValueError(f"{label} IDs cannot be empty")
    if len(cleaned) > 100:
        raise ValueError(f"cannot import more than 100 unique {label} IDs")
    if any(len(resource_id) > 256 for resource_id in cleaned):
        raise ValueError(f"{label} IDs cannot exceed 256 characters")
    return cleaned


class RuntimeImportRequest(BaseModel):
    """One import call carries both discovered resource kinds; either may be empty."""

    runtime_ids: list[str] = []
    harness_ids: list[str] = []

    @model_validator(mode="after")
    def _unique_bounded_ids(self) -> "RuntimeImportRequest":
        self.runtime_ids = _clean_resource_ids(self.runtime_ids, "Runtime")
        self.harness_ids = _clean_resource_ids(self.harness_ids, "Harness")
        if not self.runtime_ids and not self.harness_ids:
            raise ValueError("provide at least one Runtime ID or Harness ID to import")
        return self
