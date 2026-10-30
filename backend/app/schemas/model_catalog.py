"""The model catalog snapshot (D21) — pure data, no database import.

The registry (`services/model_registry.py`, tables `model_providers` / `models` /
`model_catalog_changes` / `workspace_model_settings`) publishes an immutable ``Catalog``
into the process-global holder below; `schemas/agent.py` keeps every historical public
name (``PROVIDERS``, ``MODEL_CATALOG``, ``ALLOWED_MODEL_IDS``, ``MODEL_CAPABILITIES``, …) as
a **view** over `current()`, so the ~30 consumer call sites keep reading plain dicts and
tuples while the data comes from the ledger.

Resolution order (plan §5.1): the registry's rows once seeded; **before the seed** (or in a
test that never builds the app) the holder carries ``BUILTIN_CATALOG`` — literally today's
``build_model_catalog()`` output (the Global Bedrock rows) plus the Titan v2 embedding row,
version 0. The seed inserts exactly these rows (``source: builtin``), so an empty registry
behaves byte-identically to the pre-D21 build's Bedrock half by construction (gate G2;
``tests/test_model_registry_parity.py``). Since R13 (N13-obs7) the seed carries **no
OpenAI-compatible provider**: SiliconFlow (or any chat-completions endpoint) is what an
administrator registers through `/api/settings/model-providers` with an SSM parameter name —
`tests/model_fixtures.py` does exactly that for the tests of the kind.

This module may be imported at `Settings` validation time (the validators import the catalog)
and from the deployer / evaluation background threads: it must stay free of DB and AWS.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger("launchpad.model_catalog")

# ── constants shared with schemas/agent.py (re-exported there) ───────────────────────
DEFAULT_MODEL_ID = "global.zai.glm-5.3"
DEFAULT_MODEL_REGION = "us-west-2"  # region-boundary: global-bedrock
DEFAULT_MODEL_CREDENTIAL = "launchpad-global-bedrock"
# GLM 5.3 reasons before it answers (always on): a ceiling under ~200 tokens is spent
# entirely on `reasoningContent` and the text block comes back empty (live 2026-10-08,
# CN-MIGRATION-ANALYSIS §7). Generated runtimes never request fewer than this.
MIN_MODEL_MAX_TOKENS = 256
# Per-call output ceiling a generated runtime gets when the spec sets none and the model
# is outside the catalog (custom BYOC ids, stale stored specs) — the historical default.
DEFAULT_RUNTIME_MAX_TOKENS = 4096
# N10-bug4 (live 2026-10-10): GLM 5.3 / Kimi K3 spend their always-on reasoning inside the
# same ceiling; a catalog model defaults to this instead — an explicit `AgentSpec.max_tokens`
# always wins (see `schemas.agent.default_runtime_max_tokens`).
DEFAULT_REASONING_MAX_TOKENS = 16384
# OpenAI-compatible chat models: most such endpoints (e.g. SiliconFlow) cap `max_tokens` at
# 8k–16k per call (model cards, 2026-10); the catalog defaults to the conservative 8192 and a
# `max=<n>` trait raises it per model. Judges start from 4096 like GLM 5.3.
DEFAULT_OPENAI_COMPATIBLE_MAX_TOKENS = 8192
DEFAULT_OPENAI_COMPATIBLE_JUDGE_MAX_TOKENS = 4096

PROVIDER_BEDROCK = "bedrock"
PROVIDER_OPENAI_COMPATIBLE = "openai_compatible"
PROVIDER_KINDS: tuple[str, ...] = (PROVIDER_BEDROCK, PROVIDER_OPENAI_COMPATIBLE)
MODEL_KIND_CHAT = "chat"
MODEL_KIND_EMBEDDING = "embedding"
MODEL_KINDS: tuple[str, ...] = (MODEL_KIND_CHAT, MODEL_KIND_EMBEDDING)
SOURCE_BUILTIN = "builtin"
SOURCE_PROBED = "probed"
SOURCE_MANUAL = "manual"
SOURCE_UNKNOWN = "unknown"  # per-field only: a probe that could not settle the field
ROW_SOURCES: tuple[str, ...] = (SOURCE_BUILTIN, SOURCE_PROBED, SOURCE_MANUAL)
FIELD_SOURCES: tuple[str, ...] = (*ROW_SOURCES, SOURCE_UNKNOWN)
CREDENTIAL_SOURCE_SSM = "ssm"
CREDENTIAL_SOURCE_HOST_ENV = "host_env"
CREDENTIAL_SOURCES: tuple[str, ...] = (CREDENTIAL_SOURCE_SSM, CREDENTIAL_SOURCE_HOST_ENV)
# The capability fields a probe may settle (plan §7.2/7.3); everything else is `manual`.
CAPABILITY_FIELDS: tuple[str, ...] = (
    "tools", "streaming", "vision", "temperature", "top_p", "default_max_tokens",
    "max_output_tokens", "default_judge_max_tokens", "embedding_dimension",
    "embedding_max_chars",
)
# The fields whose provenance `models.capability_sources` records (m1-c contract):
# `embedding_max_chars` is an editable column without a source.
CAPABILITY_SOURCE_FIELDS: tuple[str, ...] = tuple(
    f for f in CAPABILITY_FIELDS if f != "embedding_max_chars")
PROBED_CAPABILITY_FIELDS: tuple[str, ...] = (
    "tools", "streaming", "temperature", "top_p", "embedding_dimension",
)

# Built-in embedding row (= `services/opensearch_http.EMBED_*` today; plan §6).
BUILTIN_EMBEDDING_MODEL_ID = "amazon.titan-embed-text-v2:0"
BUILTIN_EMBEDDING_DIMENSION = 1024
BUILTIN_EMBEDDING_MAX_CHARS = 8000

# ── the built-in provider table (seed source; the pre-D21 PROVIDERS minus SiliconFlow) ──
# `credential_provider` is the AgentCore Identity API-key provider holding the key
# (`launchpad-<name>`); `ssm_parameter` the SecureString the host / the hub reads it from;
# `api_key_env` the host-env fallback. `required=False` providers are skipped with a "not
# configured" line when the key is absent. `id_prefix` makes a Launchpad model id unique and
# stable across providers (`<name>.<api_model_name>` for every non-Bedrock provider).
# R13 (N13-obs7, user decision 2026-10-17): the disabled-by-default SiliconFlow row and its
# four chat models left the seed; an administrator adds an OpenAI-compatible provider with
# `POST /api/settings/model-providers` (`credential_ssm_parameter` = the SSM NAME of its key).
# Ledgers seeded before R13 keep their rows — the seed runs once.
BUILTIN_PROVIDERS: dict[str, dict[str, Any]] = {
    "bedrock": {
        "kind": PROVIDER_BEDROCK,
        "label": "Global Bedrock",
        "base_url": None,
        "credential_provider": DEFAULT_MODEL_CREDENTIAL,
        "ssm_parameter": "launchpad-global-bedrock-api-key",
        "api_key_env": "LAUNCHPAD_GLOBAL_BEDROCK_API_KEY",
        "required": True,
        "id_prefix": "",
    },
}
# Model-spec grammar (admin tooling and tests): comma-separated `api_model_name[:trait[+trait…]]`;
# traits `tools` (default), `notools`, `vision`, `nostream`, `max=<n>`, `judge=<n>`.
_MODEL_TRAITS = frozenset({"tools", "notools", "vision", "nostream"})
_MODEL_TRAIT_VALUES = frozenset({"max", "judge"})

# The keys of a legacy catalog row (the pre-D21 ``MODEL_CATALOG`` dict), in today's order —
# the shape every pre-D21 consumer (hub builder, templates, Skill Lab, tests) indexes; never
# extended.
LEGACY_MODEL_KEYS: tuple[str, ...] = (
    "model_id", "provider", "kind", "label", "api_model_name", "base_url",
    "credential_provider", "vision", "temperature", "tools", "streaming",
    "default_max_tokens", "default_judge_max_tokens", "max_output_tokens",
)
LEGACY_PROVIDER_KEYS: tuple[str, ...] = (
    "kind", "label", "base_url", "credential_provider", "ssm_parameter", "api_key_env",
    "required", "id_prefix",
)


def parse_provider_models(spec: str) -> list[tuple[str, dict[str, Any]]]:
    """``api_model_name[:trait+trait…]`` entries → ``[(api_model_name, traits)]``.

    Raises ``ValueError`` on an unknown trait or an empty name so a typo in the override is a
    start-up failure, not a silently missing model."""
    entries: list[tuple[str, dict[str, Any]]] = []
    for raw in str(spec or "").split(","):
        item = raw.strip()
        if not item:
            continue
        name, _, trait_text = item.partition(":")
        name = name.strip()
        if not name or any(ch.isspace() for ch in name):
            raise ValueError(f"model catalog override: bad model name in {item!r}")
        traits: dict[str, Any] = {"tools": True, "vision": False, "streaming": True}
        for trait in filter(None, (t.strip() for t in trait_text.split("+"))):
            key, has_value, value = trait.partition("=")
            if has_value and key in _MODEL_TRAIT_VALUES:
                if not value.isdigit() or int(value) < MIN_MODEL_MAX_TOKENS:
                    raise ValueError(
                        f"model catalog override: {key}= needs an integer ≥ "
                        f"{MIN_MODEL_MAX_TOKENS} in {item!r}"
                    )
                traits[key] = int(value)
            elif not has_value and key in _MODEL_TRAITS:
                if key == "notools":
                    traits["tools"] = False
                elif key == "nostream":
                    traits["streaming"] = False
                else:
                    traits[key] = True
            else:
                raise ValueError(
                    f"model catalog override: unknown trait {trait!r} in {item!r} "
                    f"(allowed: {', '.join(sorted(_MODEL_TRAITS | _MODEL_TRAIT_VALUES))})"
                )
        entries.append((name, traits))
    return entries


def _bedrock_entry(model_id: str, *, vision: bool, temperature: bool,
                   default_judge_max_tokens: int) -> dict[str, Any]:
    return {
        "model_id": model_id,
        "provider": "bedrock",
        "kind": PROVIDER_BEDROCK,
        "label": {"global.zai.glm-5.3": "GLM 5.3 (Z.AI · global)",
                  "global.moonshotai.kimi-k3": "Kimi K3 (Moonshot AI · global)"}.get(
                      model_id, model_id),
        "api_model_name": model_id,
        "base_url": None,
        "credential_provider": DEFAULT_MODEL_CREDENTIAL,
        "vision": vision,
        "temperature": temperature,
        "tools": True,
        "streaming": True,
        "default_max_tokens": DEFAULT_REASONING_MAX_TOKENS,
        "default_judge_max_tokens": default_judge_max_tokens,
        "max_output_tokens": None,
    }


def openai_compatible_entry(provider: str, api_model_name: str,
                            traits: dict[str, Any] | None = None,
                            provider_row: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """One legacy catalog row for ``api_model_name`` on the OpenAI-compatible ``provider``
    (a `BUILTIN_PROVIDERS` key, or any provider when ``provider_row`` is given — the
    registry passes its own row for an admin-registered provider; since R13 every
    OpenAI-compatible provider is admin-registered, so ``provider_row`` is the normal path)."""
    if provider_row is None and provider not in BUILTIN_PROVIDERS:
        raise ValueError(
            f"provider {provider!r} is not built in — pass its registry row as provider_row"
        )
    table = provider_row if provider_row is not None else BUILTIN_PROVIDERS[provider]
    if table["kind"] != PROVIDER_OPENAI_COMPATIBLE:
        raise ValueError(f"provider {provider!r} is not OpenAI-compatible")
    traits = dict(traits or {})
    return {
        "model_id": f"{table['id_prefix']}{api_model_name}",
        "provider": provider,
        "kind": PROVIDER_OPENAI_COMPATIBLE,
        "label": f"{api_model_name.rsplit('/', 1)[-1]} ({table['label']})",
        "api_model_name": api_model_name,
        "base_url": table["base_url"],
        "credential_provider": table["credential_provider"],
        "vision": bool(traits.get("vision", False)),
        "temperature": True,
        "tools": bool(traits.get("tools", True)),
        "streaming": bool(traits.get("streaming", True)),
        "default_max_tokens": int(traits.get("max", DEFAULT_OPENAI_COMPATIBLE_MAX_TOKENS)),
        "default_judge_max_tokens": int(
            traits.get("judge", DEFAULT_OPENAI_COMPATIBLE_JUDGE_MAX_TOKENS)),
        "max_output_tokens": None,
    }


def build_model_catalog(seed_spec: str = "",
                        provider_row: Mapping[str, Any] | None = None
                        ) -> dict[str, dict[str, Any]]:
    """The catalog in its legacy dict shape, Bedrock rows first (catalog order = picker
    order), then — only when ``seed_spec`` names any — the OpenAI-compatible rows of
    ``seed_spec`` (the grammar above) on ``provider_row`` (a legacy provider row carrying its
    ``name``). The seed calls it bare (Bedrock only, R13); tests build catalogs for any
    spec. Pure."""
    catalog: dict[str, dict[str, Any]] = {}
    # Bedrock model cards, verified 2026-10-09: Kimi K3 has native vision input; GLM 5.3 is
    # text-only. N12-bug11: Kimi K3 rejects `temperature`. N-SCN-bug2: judge budgets.
    for entry in (
        _bedrock_entry("global.zai.glm-5.3", vision=False, temperature=True,
                       default_judge_max_tokens=4096),
        _bedrock_entry("global.moonshotai.kimi-k3", vision=True, temperature=False,
                       default_judge_max_tokens=2048),
    ):
        catalog[entry["model_id"]] = entry
    for api_model_name, traits in parse_provider_models(seed_spec):
        if provider_row is None:
            raise ValueError("OpenAI-compatible rows need provider_row: the seed carries no "
                             "such provider since R13 (N13-obs7)")
        entry = openai_compatible_entry(str(provider_row["name"]), api_model_name, traits,
                                        provider_row=provider_row)
        catalog[entry["model_id"]] = entry
    return catalog


# ── who is asking (N17-obs203 §6) ────────────────────────────────────────────────────
@dataclass(frozen=True)
class Principal:
    """The console account a catalog read or a model pick is made for: the catalog visible
    to it = every WORKSPACE-scope provider's models + the account's OWN personal providers'
    models; administrators see everything. ``WORKSPACE_ONLY`` (no account, not an admin) is
    the shared catalog alone — what a preset snapshot or an internal render uses; ``None``
    (no principal) keeps every row (workers, the deploy-job re-validation)."""

    user_id: str | None
    is_admin: bool = False

    def sees_provider(self, row: ProviderRow) -> bool:
        if self.is_admin or row.scope != "personal":
            return True
        return self.user_id is not None and row.owner_id == self.user_id


WORKSPACE_ONLY = Principal(user_id=None, is_admin=False)


# ── snapshot rows ────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class ProviderRow:
    """One model source. ``id`` is the registry row (`mp_…`), ``None`` for the built-in
    table before the seed. No key material anywhere — only names."""

    name: str
    kind: str
    label: str
    base_url: str | None
    model_region: str | None
    credential_provider: str
    ssm_parameter: str | None
    api_key_env: str | None
    required: bool
    enabled: bool
    id_prefix: str
    id: str | None = None
    builtin: bool = True
    credential_source: str = CREDENTIAL_SOURCE_HOST_ENV
    probe_state: str = "never"
    # N17-obs203: owner-bound providers — the creating account (`users.id`, None = the
    # platform's / an administrator's) and the visibility scope (`personal` | `workspace`);
    # not part of the legacy row (pickers filter on them through `Catalog.visible_model_ids`)
    owner_id: str | None = None
    scope: str = "workspace"
    # N17-obs254 / N17-obs280 (C3): whether the REGISTRY ROW holds a key the hub registered
    # from its SSM parameter (`key_version` / `key_last4` / `key_entered_at` — reset by
    # `update_provider` in the same commit as a host change, gone with a deleted row). The row
    # is the hub's ONE source for "a key is registered / the hub may read it": see
    # `hub_holds_key`; no workspace `model_credentials` record is consulted on the hub.
    key_registered: bool = False

    def hub_holds_key(self, *, host_env: bool = True) -> bool:
        """THE hub predicate (N17-obs280, C3) — whether the hub itself holds this provider's
        key. ``host_env=False`` asks only "does the row hold a registered key" — what licenses
        the hub's SSM read (`aws_clients.provider_api_key`) and what a host change must clear;
        ``host_env=True`` (default) adds the host-environment sources: the required / Bedrock
        row (the Global Bedrock key of the host — `credential_source: host_env`, never a
        `key_version`) and the row's `credential_env` variable when the hub process exports
        it. Every hub-side "configured" decision reads this; per-WORKSPACE availability
        (another account's Identity vault) stays `schemas.agent.provider_configured` with that
        workspace's resource map."""
        if self.key_registered:
            return True
        if not host_env:
            return False
        if self.required or self.kind == PROVIDER_BEDROCK:
            return True
        return bool(os.environ.get(str(self.api_key_env or ""), "").strip())

    def legacy(self) -> dict[str, Any]:
        """The pre-D21 ``PROVIDERS`` row for this provider — exactly today's keys."""
        return {
            "kind": self.kind,
            "label": self.label,
            "base_url": self.base_url,
            "credential_provider": self.credential_provider,
            "ssm_parameter": self.ssm_parameter,
            "api_key_env": self.api_key_env,
            "required": self.required,
            "id_prefix": self.id_prefix,
        }


@dataclass(frozen=True)
class ModelRow:
    """One catalog model. ``kind`` is ``chat`` | ``embedding``; ``provider_kind`` the
    serving provider's kind (what the legacy row calls ``kind``)."""

    model_id: str
    provider: str
    provider_kind: str
    kind: str
    label: str
    api_model_name: str
    base_url: str | None
    credential_provider: str
    tools: bool
    streaming: bool
    vision: bool
    temperature: bool
    top_p: bool
    default_max_tokens: int
    max_output_tokens: int | None
    default_judge_max_tokens: int | None
    embedding_dimension: int | None
    embedding_max_chars: int | None
    enabled: bool
    default: bool
    source: str
    id: str | None = None
    sort_order: int = 0

    def legacy(self) -> dict[str, Any]:
        """The pre-D21 ``MODEL_CATALOG`` row for this model — exactly today's keys."""
        return {
            "model_id": self.model_id,
            "provider": self.provider,
            "kind": self.provider_kind,
            "label": self.label,
            "api_model_name": self.api_model_name,
            "base_url": self.base_url,
            "credential_provider": self.credential_provider,
            "vision": self.vision,
            "temperature": self.temperature,
            "tools": self.tools,
            "streaming": self.streaming,
            "default_max_tokens": self.default_max_tokens,
            "default_judge_max_tokens": self.default_judge_max_tokens,
            "max_output_tokens": self.max_output_tokens,
        }

    def legacy_capabilities(self) -> dict[str, Any]:
        """The pre-D21 ``MODEL_CAPABILITIES[model_id]`` dict."""
        return {
            "vision": self.vision,
            "temperature": self.temperature,
            "tools": self.tools,
            "streaming": self.streaming,
            "default_judge_max_tokens": self.default_judge_max_tokens,
            **({"max_output_tokens": self.max_output_tokens} if self.max_output_tokens else {}),
        }

    def descriptor(self, catalog_version: int, *, model_region: str | None = None
                   ) -> dict[str, Any]:
        """The resolved model descriptor a job payload carries to a worker (plan §5.5).

        M1-d additive fields: ``provider`` (the registry name the hub resolves a key for),
        ``model_region`` (the provider row's; a Bedrock profile is entered from it) and, for
        an embedding row, ``embedding: {dimension, max_chars}`` (what an index mapping and
        the extractor Lambda need)."""
        out = {
            "model_id": self.model_id,
            "kind": self.provider_kind,
            "model_kind": self.kind,
            "base_url": self.base_url,
            "api_model_name": self.api_model_name,
            "credential_provider": self.credential_provider,
            "streaming": self.streaming,
            "inference_params": {"temperature": self.temperature, "top_p": self.top_p},
            "judge_max_tokens": self.default_judge_max_tokens,
            "default_max_tokens": self.default_max_tokens,
            "max_output_tokens": self.max_output_tokens,
            "catalog_version": catalog_version,
            "provider": self.provider,
            "model_region": model_region,
        }
        if self.kind == MODEL_KIND_EMBEDDING:
            out["embedding"] = {
                "dimension": self.embedding_dimension,
                "max_chars": self.embedding_max_chars,
            }
        return out


def model_row_from_legacy(entry: Mapping[str, Any], *, kind: str = MODEL_KIND_CHAT,
                          enabled: bool = True, default: bool | None = None,
                          source: str = SOURCE_BUILTIN, top_p: bool = True,
                          embedding_dimension: int | None = None,
                          embedding_max_chars: int | None = None,
                          id: str | None = None, sort_order: int = 0) -> ModelRow:
    """A ``ModelRow`` from a legacy catalog dict (the seed path and the tests')."""
    return ModelRow(
        model_id=str(entry["model_id"]),
        provider=str(entry["provider"]),
        provider_kind=str(entry["kind"]),
        kind=kind,
        label=str(entry["label"]),
        api_model_name=str(entry["api_model_name"]),
        base_url=entry.get("base_url"),
        credential_provider=str(entry["credential_provider"]),
        tools=bool(entry.get("tools", True)),
        streaming=bool(entry.get("streaming", True)),
        vision=bool(entry.get("vision", False)),
        temperature=bool(entry.get("temperature", True)),
        top_p=top_p,
        default_max_tokens=int(entry.get("default_max_tokens", DEFAULT_RUNTIME_MAX_TOKENS)),
        max_output_tokens=entry.get("max_output_tokens"),
        default_judge_max_tokens=entry.get("default_judge_max_tokens"),
        embedding_dimension=embedding_dimension,
        embedding_max_chars=embedding_max_chars,
        enabled=enabled,
        default=(entry["model_id"] == DEFAULT_MODEL_ID) if default is None else default,
        source=source,
        id=id,
        sort_order=sort_order,
    )


def provider_row_from_legacy(name: str, row: Mapping[str, Any], *, enabled: bool = True,
                             id: str | None = None, builtin: bool = True,
                             model_region: str | None = None,
                             credential_source: str | None = None,
                             probe_state: str = "never") -> ProviderRow:
    kind = str(row["kind"])
    return ProviderRow(
        name=name,
        kind=kind,
        label=str(row["label"]),
        base_url=row.get("base_url"),
        model_region=model_region if model_region is not None else (
            DEFAULT_MODEL_REGION if kind == PROVIDER_BEDROCK else None),
        credential_provider=str(row["credential_provider"]),
        ssm_parameter=row.get("ssm_parameter"),
        api_key_env=row.get("api_key_env"),
        required=bool(row.get("required", False)),
        enabled=enabled,
        id_prefix=str(row.get("id_prefix", "")),
        id=id,
        builtin=builtin,
        credential_source=credential_source or (
            CREDENTIAL_SOURCE_HOST_ENV if kind == PROVIDER_BEDROCK else CREDENTIAL_SOURCE_SSM),
        probe_state=probe_state,
    )


@dataclass(frozen=True)
class Catalog:
    """An immutable snapshot of the registry. ``models`` insertion order = picker order
    (Bedrock rows first by seed order, then `sort_order`). ``version`` is the journal id
    (`max(model_catalog_changes.id)`; 0 for the built-in table). ``workspace_disabled``
    carries the per-workspace enable flags (plan §2.4; absent = enabled).

    The legacy views are computed once per snapshot and cached, so `providers()["bedrock"]`
    keeps returning the same dict object across calls (call sites compare with ``is``)."""

    version: int
    providers: dict[str, ProviderRow]
    models: dict[str, ModelRow]
    workspace_disabled: dict[str, frozenset[str]] = field(default_factory=dict)
    # N17-obs203 §5: the ledger CONTENT stamp (`model_registry.content_stamp` — every
    # catalog-shaping row, personal-scope ones included) the snapshot was built from; what the
    # 30 s refresher compares, so a member's personal provider reaches a second process even
    # though `version` (the catalog version presets / deployments compare against) did not move
    revision: int = 0
    _cache: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)

    # ── derived views (cached) ──
    def _cached(self, key: str, build: Callable[[], Any]) -> Any:
        try:
            return self._cache[key]
        except KeyError:
            value = self._cache[key] = build()
            return value

    def chat_models(self) -> dict[str, ModelRow]:
        """Every chat row (enabled or not — stored specs on a disabled row keep parsing)."""
        return self._cached("chat_models", lambda: {
            mid: row for mid, row in self.models.items() if row.kind == MODEL_KIND_CHAT})

    def embedding_models(self) -> dict[str, ModelRow]:
        return self._cached("embedding_models", lambda: {
            mid: row for mid, row in self.models.items() if row.kind == MODEL_KIND_EMBEDDING})

    def allowed_ids(self) -> tuple[str, ...]:
        """The legacy ``ALLOWED_MODEL_IDS``: chat rows in catalog order."""
        return self._cached("allowed_ids", lambda: tuple(self.chat_models()))

    def legacy_providers(self) -> dict[str, dict[str, Any]]:
        return self._cached("legacy_providers", lambda: {
            name: row.legacy() for name, row in self.providers.items()})

    def legacy_models(self) -> dict[str, dict[str, Any]]:
        return self._cached("legacy_models", lambda: {
            mid: row.legacy() for mid, row in self.chat_models().items()})

    def legacy_capabilities(self) -> dict[str, dict[str, Any]]:
        return self._cached("legacy_capabilities", lambda: {
            mid: row.legacy_capabilities() for mid, row in self.chat_models().items()})

    def legacy_default_max_tokens(self) -> dict[str, int]:
        return self._cached("legacy_default_max_tokens", lambda: {
            mid: row.default_max_tokens for mid, row in self.chat_models().items()})

    # ── lookups ──
    def provider_of(self, model_id: str) -> ProviderRow | None:
        row = self.models.get(model_id)
        return self.providers.get(row.provider) if row else None

    def default_chat_model(self) -> ModelRow | None:
        for row in self.chat_models().values():
            if row.default:
                return row
        return None

    def embedding_model(self) -> ModelRow | None:
        """The active embedding row: the enabled default, else the first enabled one."""
        rows = [r for r in self.embedding_models().values() if r.enabled]
        for row in rows:
            if row.default:
                return row
        return rows[0] if rows else None

    def model_enabled(self, model_id: str, workspace_id: str | None = None) -> bool:
        """``model.enabled ∧ provider.enabled ∧ workspace flag ≠ false``. An id the
        snapshot does not know answers True — the catalog check refuses it first."""
        row = self.models.get(model_id)
        if row is None:
            return True
        if not row.enabled:
            return False
        provider = self.providers.get(row.provider)
        if provider is not None and not provider.enabled:
            return False
        if workspace_id is not None and model_id in self.workspace_disabled.get(
                workspace_id, frozenset()):
            return False
        return True

    def model_visible(self, model_id: str, principal: Principal | None) -> bool:
        """N17-obs203 §6: whether ``principal`` may pick / render ``model_id`` — its provider
        is workspace-scope, or the principal's own personal provider, or the principal is an
        administrator. ``None`` (no principal) and an unknown id answer True (the catalog check
        refuses the latter first)."""
        if principal is None:
            return True
        provider = self.provider_of(model_id)
        return provider is None or principal.sees_provider(provider)

    def visible_model_ids(self, principal: Principal | None) -> tuple[str, ...]:
        return tuple(mid for mid in self.allowed_ids() if self.model_visible(mid, principal))

    def summary(self) -> str:
        chat = sum(1 for r in self.models.values() if r.kind == MODEL_KIND_CHAT)
        emb = sum(1 for r in self.models.values() if r.kind == MODEL_KIND_EMBEDDING)
        return (f"{len(self.providers)} provider(s) · {chat} chat · {emb} embedding "
                f"(catalog v{self.version})")


def extended(catalog: Catalog, *, providers: Mapping[str, ProviderRow] | None = None,
             models: Mapping[str, ModelRow] | None = None, version: int | None = None,
             revision: int | None = None) -> Catalog:
    """A new snapshot = ``catalog`` with the given provider / model rows added or replaced
    (a fresh derived-view cache; ``catalog`` itself is untouched). The way a test or a tool
    publishes a variant of the catalog since the module-level overlay proxies were removed
    (M1-e, C5): ``publish(extended(current(), models={fake: row}))``."""
    return Catalog(
        version=catalog.version if version is None else version,
        providers={**catalog.providers, **(providers or {})},
        models={**catalog.models, **(models or {})},
        workspace_disabled=dict(catalog.workspace_disabled),
        revision=catalog.revision if revision is None else revision,
    )


def builtin_embedding_row() -> ModelRow:
    return ModelRow(
        model_id=BUILTIN_EMBEDDING_MODEL_ID,
        provider="bedrock",
        provider_kind=PROVIDER_BEDROCK,
        kind=MODEL_KIND_EMBEDDING,
        label="Titan Text Embeddings v2 (Amazon · global)",
        api_model_name=BUILTIN_EMBEDDING_MODEL_ID,
        base_url=None,
        credential_provider=DEFAULT_MODEL_CREDENTIAL,
        tools=False,
        streaming=False,
        vision=False,
        temperature=False,
        top_p=False,
        default_max_tokens=0,
        max_output_tokens=None,
        default_judge_max_tokens=None,
        embedding_dimension=BUILTIN_EMBEDDING_DIMENSION,
        embedding_max_chars=BUILTIN_EMBEDDING_MAX_CHARS,
        enabled=True,
        default=True,
        source=SOURCE_BUILTIN,
    )


def builtin_catalog() -> Catalog:
    """Today's tables as a ``Catalog`` (version 0): `build_model_catalog()` (the Global
    Bedrock rows) + the Titan v2 embedding row. No Settings / env read (C5); no
    OpenAI-compatible provider (R13, N13-obs7)."""
    providers = {name: provider_row_from_legacy(name, row)
                 for name, row in BUILTIN_PROVIDERS.items()}
    models: dict[str, ModelRow] = {}
    for index, (model_id, entry) in enumerate(build_model_catalog().items()):
        models[model_id] = model_row_from_legacy(entry, sort_order=index)
    embedding = builtin_embedding_row()
    models[embedding.model_id] = embedding
    return Catalog(version=0, providers=providers, models=models)


BUILTIN_CATALOG: Catalog = builtin_catalog()

# ── the holder ───────────────────────────────────────────────────────────────────────
# `current()` is read on every `assert_model_*` / template render; it is a dict read plus,
# at most every REFRESH_TTL_S seconds, one call into the installed refresher (the registry's
# `SELECT max(id) FROM model_catalog_changes` stamp check — plan §5.4). Writes publish
# in-process immediately; the stamp covers a second process (`make dev` reload).
REFRESH_TTL_S = 30.0
_lock = threading.RLock()
_current: Catalog = BUILTIN_CATALOG
_refresher: Callable[[], Catalog | None] | None = None
_last_check: float = 0.0
_refreshing = False


def current() -> Catalog:
    """The published snapshot (refreshed through the installed refresher when its stamp
    is older than ``REFRESH_TTL_S``; fail-soft — a failing refresher keeps the snapshot)."""
    global _last_check, _refreshing
    if _refresher is not None and not _refreshing:
        now = time.monotonic()
        if now - _last_check >= REFRESH_TTL_S:
            with _lock:
                if not _refreshing and now - _last_check >= REFRESH_TTL_S:
                    _refreshing = True
                    _last_check = now
                    try:
                        fresh = _refresher()
                        if fresh is not None:
                            publish(fresh)
                    except Exception:  # noqa: BLE001 — never fail a request on a stamp check
                        log.warning("model catalog refresh failed; keeping v%d",
                                    _current.version, exc_info=True)
                    finally:
                        _refreshing = False
    return _current


def publish(catalog: Catalog) -> Catalog:
    """Install ``catalog`` as the current snapshot (registry writes, startup seed)."""
    global _current, _last_check
    with _lock:
        _current = catalog
        _last_check = time.monotonic()
    return catalog


def set_refresher(fn: Callable[[], Catalog | None] | None) -> None:
    """Install the stamp-check refresher (`services/model_registry.install_refresher`);
    ``None`` uninstalls it. The next `current()` after ``REFRESH_TTL_S`` calls it."""
    global _refresher, _last_check
    with _lock:
        _refresher = fn
        _last_check = time.monotonic()


def force_refresh() -> Catalog:
    """Run the refresher now (tests, admin writes from another process)."""
    global _last_check
    with _lock:
        _last_check = -float("inf")
    return current()


def reset() -> None:
    """Back to the built-in table with no refresher — the hermetic suite's teardown."""
    set_refresher(None)
    publish(BUILTIN_CATALOG)
