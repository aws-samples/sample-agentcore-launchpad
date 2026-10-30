"""The model registry (D21): seed, snapshot, CRUD, journal, per-workspace flags.

Owns the four ledger tables of `app/models/model_registry.py` and publishes an immutable
`schemas.model_catalog.Catalog` into the process-global holder after every write
(`refresh`). Reads never hit the ledger per request: `schemas/agent.py` answers from the
snapshot; the 30 s stamp check (`install_refresher`) covers a second process.

Resolution order (plan §5.1): registry rows once seeded → `BUILTIN_CATALOG` while the
``models`` table is empty. `ensure_seeded()` (startup) inserts today's built-in tables
verbatim (`source: builtin`, one `seed` journal row), so a fresh ledger behaves exactly like
the pre-D21 build (gate G2, `tests/test_model_registry_parity.py`).

Nothing here touches AWS. Probes and the credential flow are M1-b's
(`services/model_probe.py`, `model_credential.py`) and plug in through `set_probe_hooks`;
the embedding reindex guard is M1-d's and plugs in through `set_embedding_guard`. No key
material ever enters these tables or the dicts this module returns (spec C4, plan §9.2).
"""

from __future__ import annotations

import ipaddress
import logging
import re
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol
from urllib.parse import urlsplit

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.db import SessionLocal
from app.core.errors import AppError, NotFoundError
from app.models.ledger import Agent, Workspace
from app.models.model_registry import (
    Model,
    ModelCatalogChange,
    ModelProvider,
    WorkspaceModelSetting,
)
from app.schemas import model_catalog as mc
from app.schemas.model_catalog import Catalog, ModelRow, ProviderRow
from app.services import creators

if TYPE_CHECKING:
    from app.routers.auth import Identity

log = logging.getLogger("launchpad.model_registry")

PROVIDER_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{1,31}$")
SSM_PARAMETER_RE = re.compile(r"^launchpad-[a-z0-9-]+-api-key$")
MODEL_REGION_RE = re.compile(r"^[a-z]{2}(-gov)?-[a-z]+-\d$")
API_MODEL_NAME_RE = re.compile(r"^\S{1,200}$")
PROBE_NEVER: dict[str, Any] = {"state": "never", "checked_at": None, "latency_ms": None,
                               "models_seen": [], "error": None}

# Error codes (plan §3; `model.*` coexist with the historical `model.not_allowed` family).
E_PROVIDER_NOT_FOUND = "model_provider.not_found"
E_PROVIDER_EXISTS = "model_provider.exists"
E_PROVIDER_BUILTIN = "model_provider.builtin"
E_PROVIDER_IN_USE = "model_provider.in_use"
# D21-obs8: `PUT /{id}` carried a key outside `ProviderUpdate` (`name` / `kind` are immutable)
E_PROVIDER_INVALID_UPDATE = "model_provider.invalid_update"
E_PROVIDER_REQUIRED = "model_provider.required"
E_PROVIDER_INVALID_BASE_URL = "model_provider.invalid_base_url"
E_PROVIDER_HOST_NOT_ALLOWED = "model_provider.host_not_allowed"
E_PROVIDER_INVALID_SSM = "model_provider.invalid_ssm_parameter"
E_PROVIDER_INVALID_REGION = "model_provider.invalid_model_region"
E_MODEL_NOT_FOUND = "model.not_found"
E_MODEL_EXISTS = "model.exists"
E_MODEL_BUILTIN = "model.builtin"
E_MODEL_IN_USE = "model.in_use"
E_MODEL_IS_DEFAULT = "model.is_default"
E_MODEL_DEFAULT_INVALID = "model.default_invalid"
E_MODEL_INVALID = "model.invalid"
E_MODEL_UNKNOWN = "model.unknown"
E_PROBE_UNAVAILABLE = "model_providers.probe_unavailable"
E_MODEL_PROBE_FAILED = "model.probe_failed"  # raised by M1-b with detail {findings, errors}
E_CREDENTIAL_UNAVAILABLE = "model_providers.credential_unavailable"  # FE: isProbeUnavailable
# N17-obs203: owner-bound providers — a member touching a row that is not theirs (or sharing one)
E_PROVIDER_FORBIDDEN = "model_provider.forbidden"
E_PROVIDER_INVALID_SCOPE = "model_provider.invalid_scope"
# N17-obs203 §3: a key typed in the console — the probe with it failed (nothing stored), the
# console host could not write the SSM parameter (IAM: `SsmWriteLaunchpadSecretParameters`),
# or the provider kind takes no key (Bedrock rides the Global bearer)
E_PROVIDER_PROBE_FAILED = "model_provider.probe_failed"
E_SSM_WRITE_FAILED = "model_provider.ssm_write_failed"
E_API_KEY_UNSUPPORTED = "model_provider.api_key_unsupported"
SCOPE_PERSONAL = "personal"
SCOPE_WORKSPACE = "workspace"
SCOPES: tuple[str, ...] = (SCOPE_PERSONAL, SCOPE_WORKSPACE)
W_PROBE_FAILED = "probe_failed"
W_PRESET_SNAPSHOT_BEHIND = "preset_snapshot_behind"  # produced by M1-d (system_agents)
# Journal operations that are AUDIT ONLY: they are listed by `/changes` but never advance the
# catalog version, because they change nothing a rendered runtime, a preset snapshot or a
# `/api/capabilities` reader could observe (D21-obs5 — a key rotation is not a catalog change;
# D21-obs6 — a probe records what the endpoint answered, it does not change the catalog. When
# an applied re-probe DOES change capability fields, `model_probe._reprobe` journals those
# changes as a separate, catalog-shaping `model.update` row — see §5.4 of the contract).
# r15-e: a model-role override (`model_role.set` / `model_role.reset`) is audit-only too — it
# moves a platform default, not the catalog a runtime or a picker's option list is rendered from.
# N17-obs203 §3: `provider.key` (a console-typed key reached SSM) is audit-only like
# `provider.credential` — a key changes nothing a runtime or a picker renders.
AUDIT_ONLY_OPERATIONS: frozenset[str] = frozenset({
    "provider.credential", "provider.probe", "model.probe", "model_role.set", "model_role.reset",
    "provider.key"})


# ── naming invariants (plan §1.1) ────────────────────────────────────────────────────
def credential_provider_name(provider_name: str) -> str:
    return f"launchpad-{provider_name}"


def default_ssm_parameter(provider_name: str) -> str:
    return f"launchpad-{provider_name}-api-key"


def credential_env_name(provider_name: str) -> str:
    return f"LAUNCHPAD_{provider_name.upper().replace('-', '_')}_API_KEY"


def owner_scoped_ssm_parameter(user_id: str | None, provider_name: str) -> str:
    """N17-obs203: the SSM SecureString a PERSONAL provider's key lives in —
    `launchpad-user-<users.id>-<name>-api-key` (the owner's id keeps two members' same-named
    providers apart; a creator without a `users` row — the config admin — gets the plain
    name). Always within `SSM_PARAMETER_RE` and the ops role's `launchpad-*-api-key` grant."""
    if not user_id:
        return default_ssm_parameter(provider_name)
    return f"launchpad-user-{user_id}-{provider_name}-api-key"


def owner_scoped_credential_provider(user_id: str | None, provider_name: str) -> str:
    """The Identity API-key provider of a PERSONAL provider: `launchpad-user-<users.id>-<name>`
    (≤ 80 chars of `[a-z0-9-]`, within Identity's 128-char `[a-zA-Z0-9_-]+` rule)."""
    if not user_id:
        return credential_provider_name(provider_name)
    return f"launchpad-user-{user_id}-{provider_name}"


def validate_scope(value: Any) -> str:
    scope = str(value or SCOPE_WORKSPACE)
    if scope not in SCOPES:
        raise AppError(E_PROVIDER_INVALID_SCOPE, f"scope must be one of {', '.join(SCOPES)}",
                       {"scope": scope}, status_code=422)
    return scope


# ── ownership + visibility (N17-obs203; users-and-ownership.md "Model providers") ────────
def provider_visible(row: ModelProvider, identity: Identity | None) -> bool:
    """Who SEES a provider: administrators every row; a member every `workspace`-scope row
    and their OWN `personal` rows — never another member's personal provider. No identity
    (internal callers) sees everything."""
    if identity is None or identity.is_admin:
        return True
    if (row.scope or SCOPE_WORKSPACE) == SCOPE_WORKSPACE:
        return True
    return creators.Creator.of(identity).owns(row)


def provider_owned(row: ModelProvider, identity: Identity | None) -> bool:
    """`owned` flag of `ProviderOut`: the caller's account created the row (an administrator
    may mutate every row but owns only what they created)."""
    if identity is None:
        return False
    return creators.Creator.of(identity).owns(row)


def require_provider_visible(row: ModelProvider, identity: Identity | None) -> ModelProvider:
    """A hidden provider answers the kind's not-found — indistinguishable from a missing one
    (the 404-before-403 precedence of users-and-ownership.md §8)."""
    if not provider_visible(row, identity):
        raise NotFoundError(E_PROVIDER_NOT_FOUND, f"model provider {row.id!r} not found")
    return row


def require_provider_mutable(row: ModelProvider, identity: Identity | None, action: str
                             ) -> ModelProvider:
    """Creator-or-admin (`services/ownership.require_creator_or_admin`) for every write on a
    provider or on its models / key / probe: 403 `model_provider.forbidden` naming the row,
    its creator and the action; a NULL creator (seed, legacy, config admin) is an
    administrator's. Runs AFTER `require_provider_visible`."""
    from app.services import ownership

    require_provider_visible(row, identity)
    ownership.require_creator_or_admin(
        row, identity, E_PROVIDER_FORBIDDEN,
        message="only the provider's creator or an administrator may change or delete it",
        detail={"provider_id": row.id, "action": action})
    return row


def visible_providers(db: Session, identity: Identity | None) -> list[ModelProvider]:
    return [p for p in _providers(db) if provider_visible(p, identity)]


# N17-obs225 (rev-r20 B1): on a WORKSPACE-scope row the fields that decide WHERE the hub sends
# a key and WHICH key it sends — `base_url`, `credential_ssm_parameter` (the Advanced path) and
# the key itself (typed on create / update / `POST /credential`, or read from the operator's
# parameter by a body-less `POST /credential`) — are an administrator's, whoever created the
# row. The creator keeps `label` / `enabled` / `model_region` and the models. Without this, a
# member whose personal row an administrator shared (or a legacy re-pointed creator who is no
# longer an administrator) could repoint a shared row at the operator's parameter and their own
# host: `POST /credential` would register the operator's key for the member's Identity provider,
# `POST /probe` would send it to the member's host and `POST /credential {api_key}` would
# overwrite the operator's parameter. The gate runs AFTER `require_provider_mutable` (so the
# 404 / 403 order of users-and-ownership.md §8 holds) and BEFORE any write.
CREDENTIAL_FIELDS: tuple[str, ...] = ("base_url", "credential_ssm_parameter")


def _changes_field(row: ModelProvider, field: str, data: Mapping[str, Any]) -> bool:
    """Whether ``data`` carries a DIFFERENT value for ``field`` (the console's edit drawer
    sends every `ProviderUpdate` key on every save — an unchanged echo is not a change)."""
    if field not in data or data[field] is None:
        return False
    current = str(getattr(row, field) or "").strip()
    wanted = str(data[field] or "").strip()
    if field == "base_url":
        current, wanted = current.rstrip("/"), wanted.rstrip("/")
    return wanted != current


def require_admin_for_credential_fields(row: ModelProvider, identity: Identity | None,
                                        data: Mapping[str, Any] | None = None, *,
                                        api_key: bool = False,
                                        action: str | None = None) -> None:
    """403 `model_provider.forbidden {provider_id, action, scope}` when a non-administrator
    changes `base_url` / `credential_ssm_parameter` of, or enters / replaces / registers the
    key of, a `workspace`-scope provider. ``action`` names a non-field action (`credential`).
    A personal row is its owner's end to end (the owner-derived parameter already pins which
    key it can reach — N17-obs221)."""
    if identity is None or identity.is_admin:
        return
    if (row.scope or SCOPE_WORKSPACE) != SCOPE_WORKSPACE:
        return
    what = action
    if what is None:
        changed = [f for f in CREDENTIAL_FIELDS if _changes_field(row, f, data or {})]
        if api_key:
            changed.append("api_key")
        what = changed[0] if changed else None
    if what is None:
        return
    raise AppError(E_PROVIDER_FORBIDDEN,
                   "on a shared (workspace-scope) provider only an administrator may change the "
                   "endpoint, the SSM parameter or the key — the creator keeps the label, the "
                   "enabled flag and the models",
                   {"provider_id": row.id, "action": what, "scope": SCOPE_WORKSPACE,
                    **creators.fields(row)}, status_code=403)


# ── plug-in slots for the sibling nodes ──────────────────────────────────────────────
class ProbeHooks(Protocol):
    """M1-b's probe + credential implementation (`services/model_probe.py`). Every method
    runs in the request thread, returns a secret-free dict and may raise `AppError`."""

    def probe_provider(self, db: Session, provider: ModelProvider, actor: str
                       ) -> dict[str, Any]: ...

    def register_credential(self, db: Session, provider: ModelProvider, actor: str
                            ) -> dict[str, Any]: ...

    def probe_model(self, db: Session, provider: ModelProvider, api_model_name: str,
                    kind: str, model: Model | None, *, apply: bool, actor: str
                    ) -> dict[str, Any]: ...

    # N17-obs203 §3 — a key typed in the console (the value lives only in these arguments)
    def probe_api_key(self, provider: ModelProvider, api_key: str) -> None: ...

    def store_api_key(self, db: Session, provider: ModelProvider, api_key: str, actor: str
                      ) -> dict[str, Any]: ...

    def forget_api_key(self, db: Session, provider: ModelProvider, actor: str
                       ) -> dict[str, Any]: ...

    # N17-obs229: the hub / spoke `resources.model_credentials[<name>]` record of a deleted
    # provider — `configured: false` so a same-named successor row starts unconfigured
    def forget_credential_record(self, db: Session, provider: ModelProvider) -> int: ...

    # N17-obs228: the row's `base_url` changes without a re-entered key — the stored key is
    # cleared everywhere the hub can reach (SSM if console-written, Identity best-effort, the
    # hub record, the cache); returns the residue report the journal note carries
    def clear_api_key(self, db: Session, provider: ModelProvider, actor: str
                      ) -> dict[str, Any]: ...


_probe_hooks: ProbeHooks | None = None
_embedding_guard: Any = None  # Callable[[Session, Model], None] raising AppError (M1-d)


def set_probe_hooks(hooks: ProbeHooks | None) -> None:
    global _probe_hooks
    _probe_hooks = hooks


def probe_hooks() -> ProbeHooks | None:
    return _probe_hooks


def require_probe_hooks(what: str = "probe") -> ProbeHooks:
    if _probe_hooks is None:
        code = E_CREDENTIAL_UNAVAILABLE if what == "credential" else E_PROBE_UNAVAILABLE
        raise AppError(code, f"model {what}s are not available in this build yet",
                       {"what": what}, status_code=501)
    return _probe_hooks


def set_embedding_guard(guard: Any) -> None:
    """M1-d: ``guard(db, model_row)`` raises 409 `embedding.reindex_required` when the
    active embedding row may not change (plan §6)."""
    global _embedding_guard
    _embedding_guard = guard


# M1-d: extra `warnings(db)` producers — ``fn(db) -> list[{code, message, detail}]`` — e.g.
# the preset-snapshot banner (`system_agents.service.catalog_snapshot_warnings`). Fail-soft:
# a producer that raises is logged and skipped, a Settings read never 500s on it.
_warning_producers: list[Any] = []


def add_warning_producer(fn: Any) -> None:
    if fn not in _warning_producers:
        _warning_producers.append(fn)


def clear_warning_producers() -> None:
    _warning_producers.clear()


# N17-obs206: `fn(row)` listeners told about every journal row that MOVES THE CATALOG
# VERSION (`stamp()` — not `AUDIT_ONLY_OPERATIONS`, not a personal-scope row) and — a
# separate predicate, `stamp()` untouched (rev-r24 #3) — about every model-ROLE write
# (`model_role.set` / `.reset`: an installed preset renders the role's default, N17-obs202),
# from inside `_journal` (before the writer's commit; a listener must therefore only
# *schedule*, never read the catalog). The installed-preset auto-repair sweep debounces on it
# (`system_agents.auto_repair.on_catalog_change`, registered in `app/main.py`). Fail-soft: a
# listener that raises is logged and skipped, the write itself never fails on it.
_catalog_change_listeners: list[Any] = []
MODEL_ROLE_OPERATIONS: frozenset[str] = frozenset({"model_role.set", "model_role.reset"})


def add_catalog_change_listener(fn: Any) -> None:
    if fn not in _catalog_change_listeners:
        _catalog_change_listeners.append(fn)


def clear_catalog_change_listeners() -> None:
    _catalog_change_listeners.clear()


def moves_catalog_version(operation: str, scope: str | None) -> bool:
    """Whether a journal row of ``operation`` / ``scope`` advances `stamp()` — the one
    predicate `stamp()` and the change listeners share."""
    return operation not in AUDIT_ONLY_OPERATIONS and scope != SCOPE_PERSONAL


def arms_auto_repair(operation: str, scope: str | None) -> bool:
    """Whether a journal row should wake the installed-preset auto-repair sweep: it moves
    the catalog version, or it is a model-role write (audit-only for `stamp()`, yet a preset
    renders the role's default). Personal-scope rows never do."""
    return moves_catalog_version(operation, scope) or (
        operation in MODEL_ROLE_OPERATIONS and scope != SCOPE_PERSONAL)


def _notify_catalog_change(row: ModelCatalogChange) -> None:
    if not arms_auto_repair(row.operation, row.scope):
        return
    for fn in list(_catalog_change_listeners):
        try:
            fn(row)
        except Exception:  # noqa: BLE001 — a listener never fails a registry write
            log.exception("catalog change listener %r failed", fn)


# ── snapshot ─────────────────────────────────────────────────────────────────────────
def _provider_row(p: ModelProvider) -> ProviderRow:
    probe = p.probe if isinstance(p.probe, dict) else {}
    return ProviderRow(
        name=p.name, kind=p.kind, label=p.label, base_url=p.base_url,
        model_region=p.model_region, credential_provider=p.credential_provider,
        ssm_parameter=p.credential_ssm_parameter, api_key_env=p.credential_env,
        required=bool(p.required), enabled=bool(p.enabled), id_prefix=p.id_prefix or "",
        id=p.id, builtin=bool(p.builtin), credential_source=p.credential_source or "ssm",
        probe_state=str(probe.get("state") or "never"),
        owner_id=p.created_by, scope=p.scope or SCOPE_WORKSPACE,
        key_registered=_stored_key(p),  # N17-obs254 / obs280
    )


def _model_row(m: Model, p: ModelProvider) -> ModelRow:
    params = m.inference_params if isinstance(m.inference_params, dict) else {}
    return ModelRow(
        model_id=m.model_id, provider=p.name, provider_kind=p.kind, kind=m.kind,
        label=m.label, api_model_name=m.api_model_name, base_url=p.base_url,
        credential_provider=p.credential_provider, tools=bool(m.tools),
        streaming=bool(m.streaming), vision=bool(m.vision),
        temperature=bool(params.get("temperature", True)),
        top_p=bool(params.get("top_p", True)),
        default_max_tokens=int(m.default_max_tokens or 0),
        max_output_tokens=m.max_output_tokens,
        default_judge_max_tokens=m.default_judge_max_tokens,
        embedding_dimension=m.embedding_dimension, embedding_max_chars=m.embedding_max_chars,
        enabled=bool(m.enabled), default=bool(m.is_default), source=m.source or "manual",
        id=m.id, sort_order=int(m.sort_order or 0),
    )


def _providers(db: Session) -> list[ModelProvider]:
    return list(db.scalars(select(ModelProvider).order_by(
        ModelProvider.builtin.desc(), ModelProvider.created_at, ModelProvider.id)))


def _models(db: Session) -> list[Model]:
    return list(db.scalars(select(Model).order_by(Model.sort_order, Model.created_at, Model.id)))


def stamp(db: Session) -> int:
    """The catalog version in the ledger: ``max(model_catalog_changes.id)`` over the
    catalog-shaping operations (seed, provider/model create·update·delete, workspace flags);
    0 when empty. `AUDIT_ONLY_OPERATIONS` rows (credential registrations, provider / model
    probes) keep their journal id but never become a version — so `preset_snapshot_behind`
    and the `Deployment.catalog_version` comparison only ever see real catalog changes
    (D21-obs5, D21-obs6). N17-obs203 §5: rows about a PERSONAL-scope provider (`scope ==
    personal`) do not either — a member's own provider changes nothing the shared catalog, a
    preset snapshot or another member's picker renders; only workspace-scope changes (and a
    scope flip, journaled `workspace`) advance the version."""
    return int(db.scalar(select(func.max(ModelCatalogChange.id)).where(
        ModelCatalogChange.operation.notin_(AUDIT_ONLY_OPERATIONS),
        or_(ModelCatalogChange.scope.is_(None), ModelCatalogChange.scope != SCOPE_PERSONAL),
    )) or 0)


def content_stamp(db: Session) -> int:
    """Every catalog-shaping row, personal-scope ones included — the snapshot `revision` the
    30 s refresher compares (N17-obs203 §5), so a second process reloads a member's new
    personal provider even though `stamp()` did not move."""
    return int(db.scalar(select(func.max(ModelCatalogChange.id)).where(
        ModelCatalogChange.operation.notin_(AUDIT_ONLY_OPERATIONS))) or 0)


def workspace_flags(db: Session) -> dict[str, list[str]]:
    """``{workspace_id: [disabled model ids]}`` — only false rows exist."""
    out: dict[str, list[str]] = {}
    for row in db.scalars(select(WorkspaceModelSetting).where(
            WorkspaceModelSetting.enabled.is_(False)).order_by(WorkspaceModelSetting.model_id)):
        out.setdefault(row.workspace_id, []).append(row.model_id)
    return out


def load_catalog(db: Session) -> Catalog:
    """The ledger as a `Catalog`; `BUILTIN_CATALOG` while the ``models`` table is empty."""
    providers = _providers(db)
    models = _models(db)
    if not providers or not models:
        return mc.BUILTIN_CATALOG
    by_id = {p.id: p for p in providers}
    provider_rows = {p.name: _provider_row(p) for p in providers}
    model_rows: dict[str, ModelRow] = {}
    # Seed order already puts the Bedrock rows first; later rows are appended by
    # `sort_order` (= picker order).
    for m in models:
        p = by_id.get(m.provider_id)
        if p is None:  # orphan (should not happen: deletes cascade in `delete_provider`)
            continue
        model_rows[m.model_id] = _model_row(m, p)
    flags = {ws: frozenset(ids) for ws, ids in workspace_flags(db).items()}
    return Catalog(version=stamp(db), providers=provider_rows, models=model_rows,
                   workspace_disabled=flags, revision=content_stamp(db))


def refresh(db: Session) -> Catalog:
    """Rebuild the snapshot from the ledger and publish it (after every write)."""
    return mc.publish(load_catalog(db))


def _refresher() -> Catalog | None:
    """The 30 s stamp check installed into the holder: reload only when the ledger's content
    revision differs from the published one (a second process wrote, or the ledger was
    reset). Returns None when nothing changed."""
    db = SessionLocal()
    try:
        # N17-obs203 §5: compare the CONTENT revision, not the catalog version — a personal
        # provider's rows move the former only, and a second process must still see them.
        # N17-obs281 (C1): the revision alone — `stamp` is the max over a SUBSET of the rows
        # `content_stamp` covers and the journal is append-only, so an equal revision implies
        # an equal version; the second `SELECT max(id)` never changed the outcome.
        if content_stamp(db) == mc.current().revision:
            return None
        return load_catalog(db)
    finally:
        db.close()


def install_refresher() -> None:
    mc.set_refresher(_refresher)


# ── journal ──────────────────────────────────────────────────────────────────────────
def _journal(db: Session, operation: str, actor: str, target_id: str | None,
             before: dict[str, Any] | None, after: dict[str, Any] | None,
             note: str | None = None, scope: str | None = None) -> ModelCatalogChange:
    """``scope`` (N17-obs203 §5) = the provider's visibility scope the row is about; a row
    about a personal provider never becomes the catalog version (`stamp`)."""
    row = ModelCatalogChange(operation=operation, actor=actor or "system",
                             target_id=target_id, before=before, after=after, note=note,
                             scope=scope)
    db.add(row)
    db.flush()
    _notify_catalog_change(row)  # N17-obs206: the auto-repair sweep debounces on this
    return row


def _journal_scope(*rows: ModelProvider | None) -> str:
    """The journal scope of a change touching ``rows``: `workspace` as soon as one side of the
    change is shared (a scope flip either way changes the shared catalog), else `personal`."""
    scopes = {str(getattr(r, "scope", None) or SCOPE_WORKSPACE) for r in rows if r is not None}
    return SCOPE_WORKSPACE if SCOPE_WORKSPACE in scopes or not scopes else SCOPE_PERSONAL


def list_changes(db: Session, *, limit: int = 50, before: int | None = None,
                 target_id: str | None = None) -> list[ModelCatalogChange]:
    """The journal, newest first. ``target_id`` (N17-obs203 §4) narrows it to one provider's
    rows — the audit trail of a provider (`provider.create / update / credential / key /
    probe / delete`, actor = the console user, host inside `before` / `after.base_url`,
    `created_at`) an administrator reads under the provider's card."""
    query = select(ModelCatalogChange).order_by(ModelCatalogChange.id.desc()).limit(limit)
    if before is not None:
        query = query.where(ModelCatalogChange.id < before)
    if target_id:
        query = query.where(ModelCatalogChange.target_id == target_id)
    return list(db.scalars(query))


def change_dict(row: ModelCatalogChange) -> dict[str, Any]:
    return {
        "id": row.id, "operation": row.operation, "actor": row.actor,
        "target_id": row.target_id, "before": row.before, "after": row.after,
        "created_at": _iso(row.created_at),
    }


# ── seed ─────────────────────────────────────────────────────────────────────────────
def _capability_sources(source: str) -> dict[str, str]:
    return dict.fromkeys(mc.CAPABILITY_SOURCE_FIELDS, source)


def ensure_seeded(db: Session | None = None, actor: str = "system") -> Catalog:
    """Insert today's built-in tables when the ``models`` table is empty (idempotent), then
    publish the snapshot. Called at startup (`app/main.py`) and by the bootstrap's
    `model-credential` stage; cheap when already seeded (one COUNT)."""
    own = db is None
    db = db or SessionLocal()
    try:
        if int(db.scalar(select(func.count(Model.id))) or 0) > 0:
            return refresh(db)
        # C5 (M1-e): the seed rows come from the module tables alone — no Settings / env
        # override exists any more; administrators edit the rows on the registry page.
        # R13 (N13-obs7): Bedrock + Titan only — an OpenAI-compatible provider is added by
        # an administrator, never seeded; a ledger seeded earlier keeps whatever it has.
        builtin = mc.builtin_catalog()
        existing = {p.name: p for p in _providers(db)}
        providers: dict[str, ModelProvider] = {}
        for name, prow in builtin.providers.items():
            row = existing.get(name)
            if row is None:
                row = ModelProvider(
                    name=name, kind=prow.kind, label=prow.label, base_url=prow.base_url,
                    model_region=prow.model_region,
                    credential_provider=prow.credential_provider,
                    credential_ssm_parameter=prow.ssm_parameter,
                    credential_source=prow.credential_source, credential_env=prow.api_key_env,
                    required=prow.required, builtin=True, enabled=True,
                    id_prefix=prow.id_prefix, probe=dict(PROBE_NEVER),
                    # N17-obs203: a built-in row is the platform's — no creator (an
                    # administrator's under the NULL-creator rule), workspace scope
                    created_by=None, created_by_name=None, scope=SCOPE_WORKSPACE,
                    updated_by=actor,
                )
                db.add(row)
            providers[name] = row
        db.flush()
        for index, mrow in enumerate(builtin.models.values()):
            db.add(Model(
                provider_id=providers[mrow.provider].id, model_id=mrow.model_id,
                api_model_name=mrow.api_model_name, label=mrow.label, kind=mrow.kind,
                enabled=True, is_default=mrow.default, tools=mrow.tools,
                streaming=mrow.streaming, vision=mrow.vision,
                inference_params={"temperature": mrow.temperature, "top_p": mrow.top_p},
                default_max_tokens=mrow.default_max_tokens,
                max_output_tokens=mrow.max_output_tokens,
                default_judge_max_tokens=mrow.default_judge_max_tokens,
                embedding_dimension=mrow.embedding_dimension,
                embedding_max_chars=mrow.embedding_max_chars,
                source=mc.SOURCE_BUILTIN, capability_sources=_capability_sources(
                    mc.SOURCE_BUILTIN),
                probe=None, sort_order=index, created_by=actor, updated_by=actor,
            ))
        db.flush()
        _journal(db, "seed", actor, None, None, {
            "providers": list(builtin.providers),
            "models": list(builtin.models),
        }, note="built-in catalog seeded on first start")
        db.commit()
        catalog = refresh(db)
        log.info("model-registry: seeded %s", catalog.summary())
        return catalog
    finally:
        if own:
            db.close()


def warnings(db: Session, identity: Identity | None = None) -> list[dict[str, Any]]:
    """The page banners. A member (N17-obs203) sees the probe warnings of the providers they
    can see only; the producer warnings (preset snapshots) are an administrator's."""
    out: list[dict[str, Any]] = []
    member = identity is not None and not identity.is_admin
    for p in _providers(db):
        if member and not provider_visible(p, identity):
            continue
        probe = p.probe if isinstance(p.probe, dict) else {}
        # N17-obs205: a DISABLED provider without a configured credential (the legacy
        # SiliconFlow preset after R13) is noise here — its stale probe stays on its own card
        if probe.get("state") == "failed" and (p.enabled or hub_holds_key(p)):
            out.append({
                "code": W_PROBE_FAILED,
                "message": f"{p.label}: the last connectivity probe failed",
                "detail": {"provider_id": p.id, "name": p.name,
                           "error": probe.get("error"), "checked_at": probe.get("checked_at")},
            })
    for producer in list(_warning_producers):
        if member:
            break
        try:
            out.extend(producer(db))
        except Exception as exc:  # noqa: BLE001 — a banner must never break the page
            log.warning("model-registry warning producer %r failed: %s", producer, exc)
    return out


# ── validation ───────────────────────────────────────────────────────────────────────
def _host_allowed(host: str, allowed: Iterable[str]) -> bool:
    host = host.lower()
    for pattern in allowed:
        pattern = str(pattern).lower().strip()
        if not pattern:
            continue
        if pattern.startswith("*."):
            if host == pattern[2:] or host.endswith(pattern[1:]):
                return True
        elif host == pattern:
            return True
    return False


def validate_base_url(base_url: str | None, kind: str,
                      allowed_hosts: Iterable[str] | None = None,
                      denied_hosts: Iterable[str] | None = None) -> str | None:
    """https, a hostname (no IP literal, no userinfo, no query / fragment) — the write-time
    half of the egress guard (plan §9.3). N17-obs203 (user decision 2026-10-21): the
    operator allowlist (`Settings.model_provider_allowed_hosts`) is no longer a
    precondition — EMPTY (the default) lets any public https host through; NON-EMPTY is the
    operator's **lock mode** and still restricts to the listed hosts. The optional denylist
    (`Settings.model_provider_denied_hosts`, exact or `*.suffix`) is checked ALWAYS. Both
    refuse with 422 `model_provider.host_not_allowed` naming the setting. M1-b adds the
    DNS / SSRF resolution check before each probe request (`model_probe.guard_base_url`)."""
    if kind == mc.PROVIDER_BEDROCK:
        if base_url:
            raise AppError(E_PROVIDER_INVALID_BASE_URL,
                           "a bedrock provider has no base_url (the bearer endpoint follows "
                           "model_region)", {"base_url": base_url}, status_code=422)
        return None
    url = (base_url or "").strip()
    if not url:
        raise AppError(E_PROVIDER_INVALID_BASE_URL,
                       "base_url is required for an OpenAI-compatible provider",
                       None, status_code=422)
    parts = urlsplit(url)
    host = (parts.hostname or "").strip().lower()
    if parts.scheme != "https" or not host or parts.username or parts.password or parts.query \
            or parts.fragment:
        raise AppError(E_PROVIDER_INVALID_BASE_URL,
                       "base_url must be an https URL with a hostname and no credentials, "
                       "query or fragment", {"base_url": url}, status_code=422)
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise AppError(E_PROVIDER_INVALID_BASE_URL, "base_url must name a host, not an IP "
                       "address", {"base_url": url}, status_code=422)
    settings = get_settings() if allowed_hosts is None or denied_hosts is None else None
    denied = list(denied_hosts if denied_hosts is not None
                  else getattr(settings, "model_provider_denied_hosts", None) or [])
    if _host_allowed(host, denied):
        raise AppError(
            E_PROVIDER_HOST_NOT_ALLOWED,
            f"host {host!r} is on the operator denylist "
            "(`model_provider_denied_hosts` in config/launchpad.yaml) — the console cannot "
            "override it",
            {"host": host, "setting": "model_provider_denied_hosts", "mode": "deny",
             "denied": denied},
            status_code=422,
        )
    allowed = list(allowed_hosts if allowed_hosts is not None
                   else getattr(settings, "model_provider_allowed_hosts", None) or [])
    if allowed and not _host_allowed(host, allowed):
        raise AppError(
            E_PROVIDER_HOST_NOT_ALLOWED,
            f"host {host!r} is not on the operator allowlist — this deployment runs in lock "
            "mode: add the host to `model_provider_allowed_hosts` in config/launchpad.yaml "
            "or empty the list (the console cannot widen it)",
            {"host": host, "setting": "model_provider_allowed_hosts", "mode": "lock",
             "allowed": allowed},
            status_code=422,
        )
    return url.rstrip("/")


def validate_ssm_parameter(name: str | None, provider_name: str) -> str:
    value = (name or "").strip() or default_ssm_parameter(provider_name)
    if not SSM_PARAMETER_RE.fullmatch(value):
        raise AppError(E_PROVIDER_INVALID_SSM,
                       "credential_ssm_parameter must match launchpad-*-api-key (the ops "
                       "role's SSM grant is this pattern)", {"ssm_parameter": value},
                       status_code=422)
    return value


def _validate_model_region(region: str | None, kind: str) -> str | None:
    if kind != mc.PROVIDER_BEDROCK:
        return None
    value = (region or "").strip() or mc.DEFAULT_MODEL_REGION
    if not MODEL_REGION_RE.fullmatch(value):
        raise AppError(E_PROVIDER_INVALID_REGION, "model_region must be an AWS region id",
                       {"model_region": value}, status_code=422)
    return value


# ── providers ────────────────────────────────────────────────────────────────────────
def get_provider(db: Session, provider_id: str) -> ModelProvider:
    row = db.get(ModelProvider, provider_id)
    if row is None:
        row = db.scalar(select(ModelProvider).where(ModelProvider.name == provider_id))
    if row is None:
        raise NotFoundError(E_PROVIDER_NOT_FOUND, f"model provider {provider_id!r} not found")
    return row


def _stored_key(row: ModelProvider) -> bool:
    """The row's key facts (N17-obs228 / obs254): a key registered from SSM (`key_version`) or
    typed in the console (`key_last4` / `key_entered_at`). Read only through `_provider_row`
    into `ProviderRow.key_registered` — the one input of the hub predicate."""
    return row.key_version is not None or bool(row.key_last4) or row.key_entered_at is not None


def hub_holds_key(row: ModelProvider, *, host_env: bool = True) -> bool:
    """N17-obs280 (C3): `ProviderRow.hub_holds_key` for a ledger row — THE hub predicate
    (replaces the former `_hub_configured` and `holds_registered_key`, which answered the same
    question from different inputs). ``host_env=False``: the row holds a registered key (what a
    host change clears); default: + the host-environment sources (Bedrock / required row, the
    exported `credential_env`) — the Settings card's `configured`, the default-model and the
    disable guards. Deliberately not a workspace read: per-workspace availability lives on
    each workspace's resource map (`schemas.agent.provider_configured`)."""
    return _provider_row(row).hub_holds_key(host_env=host_env)


def provider_dict(row: ModelProvider, model_count: int | None = None, *,
                  identity: Identity | None = None) -> dict[str, Any]:
    """`ProviderOut` — names only, never a key (plan §9.2). ``identity`` (the caller) decides
    the additive `owned` flag (N17-obs203); journal rows are written without one."""
    probe = {**PROBE_NEVER, **(row.probe if isinstance(row.probe, dict) else {})}
    return {
        "id": row.id, "name": row.name, "kind": row.kind, "label": row.label,
        "base_url": row.base_url, "model_region": row.model_region,
        "credential_provider": row.credential_provider,
        "credential_ssm_parameter": row.credential_ssm_parameter,
        "credential_source": row.credential_source,
        "credential_env": row.credential_env,
        "required": bool(row.required), "builtin": bool(row.builtin),
        "enabled": bool(row.enabled), "id_prefix": row.id_prefix or "",
        "probe": {
            "state": str(probe.get("state") or "never"),
            "checked_at": probe.get("checked_at"),
            "latency_ms": probe.get("latency_ms"),
            "models_seen": list(probe.get("models_seen") or [])[:200],
            "error": probe.get("error"),
        },
        "configured": hub_holds_key(row),
        "key_version": row.key_version,
        "key_modified_at": _iso(row.key_modified_at),
        # N17-obs203 §3: the console-typed key's last four characters (never more); N17-obs283:
        # flat like every other credential fact (the nested `credential` mirror is gone)
        "key_last4": row.key_last4,
        "model_count": model_count if model_count is not None else 0,
        "created_by": row.created_by, "created_at": _iso(row.created_at),
        "updated_by": row.updated_by, "updated_at": _iso(row.updated_at),
        # N17-obs203: owner-bound resource — creator display name, scope, "mine" flag
        "created_by_name": row.created_by_name,
        "scope": row.scope or SCOPE_WORKSPACE,
        "owned": provider_owned(row, identity),
    }


def model_counts(db: Session) -> dict[str, int]:
    rows = db.execute(select(Model.provider_id, func.count(Model.id)).group_by(Model.provider_id))
    return {pid: int(n) for pid, n in rows}


def create_provider(db: Session, data: Mapping[str, Any], actor: str, *,
                    creator: creators.Creator | None = None) -> ModelProvider:
    """`build_provider` + `insert_provider` in one step (every caller without a typed key)."""
    return insert_provider(db, build_provider(db, data, actor, creator=creator), actor)


def build_provider(db: Session, data: Mapping[str, Any], actor: str, *,
                   creator: creators.Creator | None = None) -> ModelProvider:
    """Validate ``data`` and return the row WITHOUT adding it to the session — the router
    probes a typed key against it first (N17-obs203 §3: a failed probe stores nothing).
    ``creator`` stamps `created_by` / `created_by_name`;
    ``data["scope"]`` is `workspace` (default) or `personal`. A PERSONAL provider's credential
    references are owner-scoped and NOT client-settable: SSM parameter
    `launchpad-user-<uid>-<name>-api-key`, Identity provider `launchpad-user-<uid>-<name>`, and
    **no host-env fallback** (`credential_env` NULL) — so a member can never point their own
    endpoint at a parameter or variable that holds someone else's key."""
    name = str(data.get("name") or "").strip()
    if not PROVIDER_NAME_RE.fullmatch(name):
        raise AppError("model_provider.invalid_name",
                       "name must match ^[a-z][a-z0-9-]{1,31}$", {"name": name},
                       status_code=422)
    kind = str(data.get("kind") or "")
    if kind not in mc.PROVIDER_KINDS:
        raise AppError("model_provider.invalid_kind",
                       f"kind must be one of {', '.join(mc.PROVIDER_KINDS)}", {"kind": kind},
                       status_code=422)
    if db.scalar(select(ModelProvider).where(ModelProvider.name == name)) is not None:
        raise AppError(E_PROVIDER_EXISTS, f"a provider named {name!r} already exists",
                       {"name": name}, status_code=409)
    scope = validate_scope(data.get("scope"))
    user_id = creator.user_id if creator is not None else None
    if scope == SCOPE_PERSONAL:
        ssm_parameter = owner_scoped_ssm_parameter(user_id, name)
        wanted = str(data.get("credential_ssm_parameter") or "").strip()
        if wanted and wanted != ssm_parameter:
            raise AppError(E_PROVIDER_INVALID_SSM,
                           "a personal provider's SSM parameter name is derived from its owner "
                           f"and name ({ssm_parameter}) and cannot be chosen",
                           {"ssm_parameter": wanted, "expected": ssm_parameter}, status_code=422)
        credential_provider = owner_scoped_credential_provider(user_id, name)
        credential_env = None
    else:
        ssm_parameter = validate_ssm_parameter(data.get("credential_ssm_parameter"), name)
        credential_provider = credential_provider_name(name)
        credential_env = credential_env_name(name)
    row = ModelProvider(
        name=name, kind=kind, label=str(data.get("label") or name).strip()[:64],
        base_url=validate_base_url(data.get("base_url"), kind),
        model_region=_validate_model_region(data.get("model_region"), kind),
        credential_provider=credential_provider,
        credential_ssm_parameter=ssm_parameter,
        credential_source=mc.CREDENTIAL_SOURCE_SSM, credential_env=credential_env,
        required=False, builtin=False, enabled=bool(data.get("enabled", True)),
        # the built-in Bedrock row alone owns the empty prefix (its ids are profile ids)
        id_prefix=f"{name}.", probe=dict(PROBE_NEVER), scope=scope, updated_by=actor,
    )
    creators.stamp(row, creator)
    return row


def _forget_cached_key(name: str) -> None:
    """N17-obs226: the hub's in-memory key cache (`aws_clients._PROVIDER_API_KEYS`) must never
    outlive the row it was read for — dropped on insert (defensive), delete and host change.
    A lazy import: this module stays AWS-free and the cache is process memory only."""
    from app.services import aws_clients

    aws_clients.invalidate_provider_api_key(name)


E_CREDENTIAL_CLEAR_FAILED = "model_provider.credential_clear_failed"


class CredentialClearError(AppError):
    """N17-obs231: `ProbeHooks.clear_api_key` ran every step it could, but at least one raised
    something other than an AWS refusal (a bug, a ledger write that failed). The row and the
    hub record already say unconfigured (that write comes first); ``report`` is the secret-free
    residue the router journals — `{ssm_parameter, ssm_deleted, identity_deleted,
    record_unconfigured, error, failed_steps}` — before re-raising."""

    def __init__(self, report: Mapping[str, Any]) -> None:
        self.report: dict[str, Any] = dict(report)
        failed = ", ".join(str(step) for step in self.report.get("failed_steps") or [])
        super().__init__(
            E_CREDENTIAL_CLEAR_FAILED,
            "the stored key could not be fully cleared on the host change — the row and the hub "
            f"record are unconfigured; failed: {failed or 'unknown'}; check the SSM parameter "
            f"{self.report.get('ssm_parameter')} and the Identity provider by hand",
            self.report, status_code=502)


def unconfigure_credential_record(db: Session, provider_name: str, **extra: Any) -> int:
    """Flip the hub / spoke `resources.model_credentials[<name>]` record to `configured: false`
    (+ ``extra`` facts such as `cleared_on_host_change` / `deleted_provider_id`) and drop the
    process key cache — a LOCAL ledger write, no AWS call. N17-obs231: this is the first step of
    every clearing (`ProbeHooks.clear_api_key`, `forget_credential_record`) and the whole of it
    when no probe module is registered: the record is each workspace's availability
    (`schemas.agent.provider_configured(name, ctx.resources)`), so a half-finished clearing
    must not leave a workspace offering the provider. It no longer licenses the hub's SSM read
    (N17-obs280: `aws_clients.provider_api_key` reads the row's key facts only). Returns the
    number of workspace rows written."""
    from app.services.workspace import merge_workspace_resources, workspace_context

    rows = db.scalars(select(Workspace).order_by(Workspace.created_at, Workspace.id)).all()
    count = 0
    for row in rows:
        existing = (row.resources or {}).get("model_credentials")
        existing = dict(existing) if isinstance(existing, Mapping) else {}
        current = existing.get(provider_name)
        if not isinstance(current, Mapping) or not current.get("configured"):
            continue
        entry = {**current, "configured": False, **extra}
        merge_workspace_resources(workspace_context(row),
                                  {"model_credentials": {**existing, provider_name: entry}})
        count += 1
    db.expire_all()
    _forget_cached_key(provider_name)
    return count


def insert_provider(db: Session, row: ModelProvider, actor: str) -> ModelProvider:
    """Persist a `build_provider` row: journal `provider.create`, commit, republish."""
    db.add(row)
    db.flush()
    _journal(db, "provider.create", actor, row.id, None, provider_dict(row),
             scope=_journal_scope(row))
    db.commit()
    refresh(db)
    _forget_cached_key(row.name)
    return row


def _assert_may_disable_provider(db: Session, row: ModelProvider) -> None:
    """Plan §9.5: the required (built-in Bedrock) provider cannot be disabled while it hosts
    the default chat / embedding model or is the only configured provider. r15-e: no provider
    can be disabled while one of its models is a platform role's model (409
    `model_provider.in_use` with the setting referrers — repoint the role first)."""
    if row.required:
        _assert_required_provider_may_be_disabled(db, row)
    hosted = {m.model_id for m in db.scalars(select(Model).where(Model.provider_id == row.id))}
    role_refs = [r for r in setting_referrers(hosted) if r.get("role")] if hosted else []
    if role_refs:
        roles = ", ".join(str(r["role"]) for r in role_refs)
        raise AppError(E_PROVIDER_IN_USE, f"{row.label} hosts the model of the platform "
                       f"role(s) {roles} — repoint them on the Model roles tab first",
                       {"referrers": role_refs, "suggest": "repoint"}, status_code=409)


def _assert_required_provider_may_be_disabled(db: Session, row: ModelProvider) -> None:
    defaults = db.scalars(select(Model).where(Model.provider_id == row.id,
                                              Model.is_default.is_(True))).all()
    others = [p for p in _providers(db) if p.id != row.id and p.enabled and hub_holds_key(p)]
    if defaults or not others:
        raise AppError(
            E_PROVIDER_REQUIRED,
            f"{row.label} cannot be disabled: it hosts the default "
            f"{'/'.join(sorted({m.kind for m in defaults})) or 'models'} "
            "or is the only configured provider — pick another default / configure another "
            "provider first",
            {"defaults": [m.model_id for m in defaults]}, status_code=409,
        )


def host_of(url: str | None) -> str | None:
    return urlsplit(str(url or "")).hostname


def update_provider(db: Session, row: ModelProvider, data: Mapping[str, Any], actor: str,
                    *, note: str | None = None, clear_credential: bool = False
                    ) -> ModelProvider:
    """``note`` lands on the `provider.update` journal row; ``clear_credential`` (N17-obs228:
    `base_url` changed on a row holding a registered key, no key re-entered) resets the row's
    key facts — `key_version` / `key_modified_at` / `key_last4` / `key_entered_at` — so the
    card shows 「未配置」 until a key is entered again (the hooks already removed the stored
    key best-effort; the residue is the ``note``)."""
    before = provider_dict(row)
    scope_before = row.scope
    if "label" in data and data["label"] is not None:
        row.label = str(data["label"]).strip()[:64] or row.label
    if "base_url" in data:
        host_changed = _changes_field(row, "base_url", data)
        row.base_url = validate_base_url(data["base_url"], row.kind)
        if host_changed:
            _forget_cached_key(row.name)  # N17-obs226: never carry a key to a new host
    if "model_region" in data and data["model_region"] is not None:
        row.model_region = _validate_model_region(data["model_region"], row.kind)
    if "scope" in data and data["scope"] is not None:
        row.scope = validate_scope(data["scope"])
    if "credential_ssm_parameter" in data and data["credential_ssm_parameter"] is not None:
        wanted = validate_ssm_parameter(data["credential_ssm_parameter"], row.name)
        if (row.scope or SCOPE_WORKSPACE) == SCOPE_PERSONAL and wanted != (
                row.credential_ssm_parameter or ""):
            # N17-obs203: a personal provider's parameter is owner-derived — repointing it at
            # another parameter would let a member use (or exfiltrate) someone else's key
            raise AppError(E_PROVIDER_INVALID_SSM,
                           "a personal provider's SSM parameter name is derived from its owner "
                           "and name and cannot be changed",
                           {"ssm_parameter": wanted, "expected": row.credential_ssm_parameter},
                           status_code=422)
        row.credential_ssm_parameter = wanted
    if "enabled" in data and data["enabled"] is not None:
        enabled = bool(data["enabled"])
        if row.enabled and not enabled:
            _assert_may_disable_provider(db, row)
        row.enabled = enabled
    row.updated_by = actor
    notes: list[str] = []
    if clear_credential:
        row.key_version = None
        row.key_modified_at = None
        row.key_last4 = None
        row.key_entered_at = None
        _forget_cached_key(row.name)
    if (scope_before or SCOPE_WORKSPACE) == SCOPE_PERSONAL and row.scope == SCOPE_WORKSPACE \
            and row.created_by is not None:
        # N17-obs225 (rev-r20 B1): sharing TRANSFERS ownership — a workspace-scope row is the
        # workspace's (NULL creator = an administrator's). The former creator keeps reading it
        # like every member and loses every mutation right; the journal names them.
        notes.append(f"ownership transferred to the workspace on sharing (former creator: "
                     f"{row.created_by_name or '?'} / {row.created_by})")
        row.created_by = None
        row.created_by_name = None
    if note:
        notes.append(note)
    db.flush()
    # a scope flip is `workspace` either way: the shared catalog gained or lost the rows
    flip = SCOPE_WORKSPACE if SCOPE_WORKSPACE in (scope_before, row.scope) else SCOPE_PERSONAL
    _journal(db, "provider.update", actor, row.id, before, provider_dict(row),
             note="; ".join(notes) or None,
             scope=flip if scope_before else _journal_scope(row))
    db.commit()
    refresh(db)
    if before.get("enabled") and not row.enabled:  # rev-r25 F3: hidden roles follow suit
        from app.services import model_roles

        hosted = {m.model_id for m in db.scalars(select(Model).where(Model.provider_id == row.id))}
        model_roles.on_model_unavailable(db, hosted, actor)
    return row


def record_entered_key(db: Session, row: ModelProvider, *, last4: str, actor: str,
                       entered_at: datetime | None = None, console_written: bool = True) -> None:
    """N17-obs203 §3: the console wrote the row's SSM parameter — keep the last four characters
    and the time (never the value) and journal an audit-only `provider.key` row
    `{ssm_parameter, last4, key_version}`. Commits and republishes. ``console_written=False``
    (rev-r22, obs232): the typed key overwrote a parameter the OPERATOR had created — `last4`
    is kept, `key_entered_at` is not stamped, so the parameter is never deleted with the row."""
    before = {"ssm_parameter": row.credential_ssm_parameter, "last4": row.key_last4,
              "key_version": row.key_version}
    row.key_last4 = str(last4 or "")[-4:] or None
    if console_written:
        row.key_entered_at = entered_at or datetime.now(UTC)
    row.updated_by = actor
    db.flush()
    after = {"ssm_parameter": row.credential_ssm_parameter, "last4": row.key_last4,
             "key_version": row.key_version}
    _journal(db, "provider.key", actor, row.id, before, after,
             note=("key typed in the console, written to the SSM parameter by the console host"
                   if console_written else
                   "key typed in the console, written over the operator-created SSM parameter "
                   "(kept when the provider is deleted)"),
             scope=_journal_scope(row))
    db.commit()
    refresh(db)


def record_cleared_key(db: Session, row: ModelProvider, actor: str, *, note: str) -> None:
    """N17-obs228 / obs230: the audit-only `provider.credential` row of a key cleared on a host
    change — `{configured: false, cleared: true}` plus the residue report in ``note`` (which
    parameter / Identity provider was deleted or kept). Written AFTER the row update committed;
    never a catalog version."""
    _journal(db, "provider.credential", actor, row.id,
             {"configured": True}, {"configured": False, "cleared": True},
             note=note, scope=_journal_scope(row))
    db.commit()


def assert_provider_deletable(db: Session, row: ModelProvider) -> list[Model]:
    """The refusals of `delete_provider` (409 `model_provider.builtin` / `in_use`) without the
    delete — the router runs them BEFORE it forgets a console-written key (N17-obs203 §3), so a
    refused delete leaves the parameter in place. Returns the provider's model rows."""
    if row.builtin:
        raise AppError(E_PROVIDER_BUILTIN, f"{row.label} is built in and cannot be deleted "
                       "(disable its models instead)", {"name": row.name}, status_code=409)
    models = db.scalars(select(Model).where(Model.provider_id == row.id)).all()
    refs = referrers(db, {m.model_id for m in models})
    if refs:
        # N17-obs207: the console's confirm dialog lists WHO holds the provider — the agents
        # (system presets flagged) and the platform roles — next to the generic referrer list
        agents = [{"id": r["id"], "name": r["name"], "workspace_id": r.get("workspace_id"),
                   "system_key": r.get("system_key")} for r in refs if r["kind"] == "agent"]
        roles = [{"role": r["role"], "field": r["id"], "model_ids": r["model_ids"]}
                 for r in refs if r["kind"] == "setting" and r.get("role")]
        raise AppError(E_PROVIDER_IN_USE,
                       f"{row.label} still serves models that agents, evaluators or settings "
                       "reference — repoint them first",
                       {"referrers": refs, "agents": agents, "roles": roles}, status_code=409)
    return models


def delete_provider(db: Session, row: ModelProvider, actor: str, *,
                    note: str | None = None) -> None:
    """``note`` (N17-obs203 §3) appends the SSM residue report of the console-entered key."""
    models = assert_provider_deletable(db, row)
    before = provider_dict(row, len(models))
    model_ids = [m.model_id for m in models]
    for m in models:
        db.delete(m)
    if model_ids:
        for flag in db.scalars(select(WorkspaceModelSetting).where(
                WorkspaceModelSetting.model_id.in_(model_ids))):
            db.delete(flag)
    db.delete(row)
    db.flush()
    base_note = "the Identity credential provider is not deleted (rotation history)"
    _journal(db, "provider.delete", actor, before["id"], before, None,
             note=f"{base_note}; {note}" if note else base_note, scope=before.get("scope"))
    db.commit()
    refresh(db)
    from app.services import model_roles

    model_roles.on_model_unavailable(db, set(model_ids), actor)  # rev-r25 F3
    _forget_cached_key(str(before["name"]))  # N17-obs226


# ── models ───────────────────────────────────────────────────────────────────────────
def get_model(db: Session, provider: ModelProvider, ref: str) -> Model:
    """``ref`` is the row id (`md_…`) or the Launchpad model id (may contain `/`)."""
    row = db.get(Model, ref)
    if row is None:
        row = db.scalar(select(Model).where(Model.model_id == ref))
    if row is None or row.provider_id != provider.id:
        raise NotFoundError(E_MODEL_NOT_FOUND, f"model {ref!r} not found on {provider.name}")
    return row


def model_dict(row: Model, provider: ModelProvider) -> dict[str, Any]:
    params = row.inference_params if isinstance(row.inference_params, dict) else {}
    return {
        "id": row.id, "provider_id": provider.id, "provider": provider.name,
        "model_id": row.model_id, "api_model_name": row.api_model_name, "label": row.label,
        "kind": row.kind, "enabled": bool(row.enabled), "is_default": bool(row.is_default),
        "tools": bool(row.tools), "streaming": bool(row.streaming), "vision": bool(row.vision),
        "inference_params": {"temperature": bool(params.get("temperature", True)),
                             "top_p": bool(params.get("top_p", True))},
        "default_max_tokens": int(row.default_max_tokens or 0),
        "max_output_tokens": row.max_output_tokens,
        "default_judge_max_tokens": row.default_judge_max_tokens,
        "embedding_dimension": row.embedding_dimension,
        "embedding_max_chars": row.embedding_max_chars,
        "source": row.source,
        "capability_sources": dict(row.capability_sources or {}),
        "probe": row.probe,
        "sort_order": int(row.sort_order or 0),
        "created_by": row.created_by, "created_at": _iso(row.created_at),
        "updated_by": row.updated_by, "updated_at": _iso(row.updated_at),
    }


def _kind_defaults(kind: str, provider: ModelProvider) -> dict[str, Any]:
    bedrock = provider.kind == mc.PROVIDER_BEDROCK
    if kind == mc.MODEL_KIND_EMBEDDING:
        return {"tools": False, "streaming": False, "vision": False, "temperature": False,
                "top_p": False, "default_max_tokens": 0, "max_output_tokens": None,
                "default_judge_max_tokens": None, "embedding_dimension": None,
                "embedding_max_chars": mc.BUILTIN_EMBEDDING_MAX_CHARS}
    return {"tools": True, "streaming": True, "vision": False, "temperature": True,
            "top_p": True,
            "default_max_tokens": (mc.DEFAULT_REASONING_MAX_TOKENS if bedrock
                                   else mc.DEFAULT_OPENAI_COMPATIBLE_MAX_TOKENS),
            "max_output_tokens": None,
            "default_judge_max_tokens": mc.DEFAULT_OPENAI_COMPATIBLE_JUDGE_MAX_TOKENS,
            "embedding_dimension": None, "embedding_max_chars": None}


_BOOL_FIELDS = ("tools", "streaming", "vision", "temperature", "top_p")


def _coerce_capability(field: str, value: Any) -> Any:
    if field in _BOOL_FIELDS:
        if not isinstance(value, bool):
            raise AppError(E_MODEL_INVALID, f"{field} must be a boolean", {"field": field},
                           status_code=422)
        return value
    if value is None:
        if field == "default_max_tokens":
            raise AppError(E_MODEL_INVALID, "default_max_tokens is required",
                           {"field": field}, status_code=422)
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise AppError(E_MODEL_INVALID, f"{field} must be an integer", {"field": field},
                       status_code=422)
    floor = 1 if field in ("embedding_dimension", "embedding_max_chars") else (
        0 if field == "default_max_tokens" else mc.MIN_MODEL_MAX_TOKENS)
    if value < floor:
        raise AppError(E_MODEL_INVALID, f"{field} must be ≥ {floor}", {"field": field},
                       status_code=422)
    return value


def _flatten_overrides(data: Mapping[str, Any] | None) -> dict[str, Any]:
    """`{tools, inference_params: {temperature, top_p}, …}` → `{field: value}` for every
    capability field that was actually sent (``None`` = not sent)."""
    out: dict[str, Any] = {}
    if not data:
        return out
    for field in mc.CAPABILITY_FIELDS:
        if field in ("temperature", "top_p"):
            continue
        if field in data and data[field] is not None:
            out[field] = data[field]
    params = data.get("inference_params")
    if isinstance(params, Mapping):
        for field in ("temperature", "top_p"):
            if field in params and params[field] is not None:
                out[field] = params[field]
    return out


def _apply_capabilities(row: Model, values: Mapping[str, Any], source: str) -> None:
    params = dict(row.inference_params or {})
    sources = dict(row.capability_sources or {})
    for field, raw in values.items():
        value = _coerce_capability(field, raw)
        if field in ("temperature", "top_p"):
            params[field] = value
        else:
            setattr(row, field, value)
        if field in mc.CAPABILITY_SOURCE_FIELDS:
            sources[field] = source
    row.inference_params = params
    row.capability_sources = sources


def create_model(db: Session, provider: ModelProvider, data: Mapping[str, Any], actor: str,
                 probed: Mapping[str, Any] | None = None) -> Model:
    """Insert one model row. ``probed`` = `{field: value}` findings M1-b settled before the
    insert (`source: probed` per field); ``data["overrides"]`` applies after (`manual`)."""
    api_model_name = str(data.get("api_model_name") or "").strip()
    if not API_MODEL_NAME_RE.fullmatch(api_model_name):
        raise AppError(E_MODEL_INVALID, "api_model_name must be 1–200 non-blank characters",
                       {"field": "api_model_name"}, status_code=422)
    kind = str(data.get("kind") or mc.MODEL_KIND_CHAT)
    if kind not in mc.MODEL_KINDS:
        raise AppError(E_MODEL_INVALID, f"kind must be one of {', '.join(mc.MODEL_KINDS)}",
                       {"field": "kind"}, status_code=422)
    model_id = f"{provider.id_prefix or ''}{api_model_name}"
    if db.scalar(select(Model).where(Model.model_id == model_id)) is not None:
        raise AppError(E_MODEL_EXISTS, f"model {model_id!r} already exists",
                       {"model_id": model_id}, status_code=409)
    label = str(data.get("label") or "").strip()[:128] or (
        api_model_name if provider.kind == mc.PROVIDER_BEDROCK
        else f"{api_model_name.rsplit('/', 1)[-1]} ({provider.label})")
    defaults = _kind_defaults(kind, provider)
    next_order = int(db.scalar(select(func.max(Model.sort_order))) or 0) + 1
    row = Model(
        provider_id=provider.id, model_id=model_id, api_model_name=api_model_name,
        label=label, kind=kind, enabled=bool(data.get("enabled", True)), is_default=False,
        inference_params={}, capability_sources={}, source=mc.SOURCE_MANUAL,
        sort_order=next_order, created_by=actor, updated_by=actor,
    )
    _apply_capabilities(row, defaults, mc.SOURCE_MANUAL)
    probed_values = {k: v for k, v in (probed or {}).items() if k in mc.CAPABILITY_FIELDS}
    if probed_values:
        _apply_capabilities(row, probed_values, mc.SOURCE_PROBED)
        row.source = mc.SOURCE_PROBED
        if isinstance(data.get("probe_record"), Mapping):
            row.probe = dict(data["probe_record"])
    overrides = _flatten_overrides(data.get("overrides"))
    if overrides:
        _apply_capabilities(row, overrides, mc.SOURCE_MANUAL)
        row.source = mc.SOURCE_MANUAL
    if kind == mc.MODEL_KIND_EMBEDDING and not row.embedding_dimension:
        raise AppError(E_MODEL_INVALID, "an embedding model needs embedding_dimension "
                       "(probe it or pass it under overrides)",
                       {"field": "embedding_dimension"}, status_code=422)
    db.add(row)
    db.flush()
    _journal(db, "model.create", actor, row.id, None, model_dict(row, provider),
             scope=_journal_scope(provider))
    db.commit()
    refresh(db)
    return row


def _assert_default_allowed(db: Session, provider: ModelProvider, row: Model) -> None:
    if not row.enabled or not provider.enabled:
        raise AppError(E_MODEL_DEFAULT_INVALID, "the default model must be enabled",
                       {"model_id": row.model_id}, status_code=422)
    if row.kind == mc.MODEL_KIND_CHAT:
        if not row.tools:
            raise AppError(E_MODEL_DEFAULT_INVALID, "the default chat model must support tool "
                           "calling (it drives agents)", {"model_id": row.model_id},
                           status_code=422)
        if not hub_holds_key(provider):
            raise AppError(E_MODEL_DEFAULT_INVALID, "the default chat model's provider must be "
                           "configured on the hub", {"model_id": row.model_id},
                           status_code=422)
    elif _embedding_guard is not None:
        _embedding_guard(db, row)  # 409 embedding.reindex_required (M1-d)


def update_model(db: Session, provider: ModelProvider, row: Model, data: Mapping[str, Any],
                 actor: str) -> Model:
    before = model_dict(row, provider)
    if data.get("label") is not None:
        row.label = str(data["label"]).strip()[:128] or row.label
    if data.get("sort_order") is not None:
        row.sort_order = int(data["sort_order"])
    capabilities = _flatten_overrides(data)
    if capabilities:
        _apply_capabilities(row, capabilities, mc.SOURCE_MANUAL)
        row.source = mc.SOURCE_MANUAL
    if data.get("enabled") is not None:
        enabled = bool(data["enabled"])
        if row.enabled and not enabled:
            if row.is_default:
                raise AppError(E_MODEL_IS_DEFAULT, f"{row.model_id} is the default "
                               f"{row.kind} model — pick another default before disabling it",
                               {"model_id": row.model_id}, status_code=409)
            # r15-e: a model a platform ROLE points at (judge, actor, Skill Lab target, …)
            # cannot be disabled either — the role would silently break; repoint it first.
            # Agents / evaluators naming the model do NOT block a disable (D21 §5.5: stored
            # specs keep parsing, only new picks are refused).
            _assert_no_role_points_at(row.model_id)
            # rev-r8 on m1-d (D21-obs1): a non-default embedding row is never the active
            # embedder, so disabling it needs no reindex guard; the guard runs on the flip
            # (`_assert_default_allowed`) only.
        row.enabled = enabled
    if data.get("is_default") is not None:
        wanted = bool(data["is_default"])
        if wanted and not row.is_default:
            _assert_default_allowed(db, provider, row)
            for other in db.scalars(select(Model).where(Model.kind == row.kind,
                                                        Model.is_default.is_(True))):
                other.is_default = False
            row.is_default = True
        elif not wanted and row.is_default:
            raise AppError(E_MODEL_IS_DEFAULT, f"{row.model_id} is the default {row.kind} "
                           "model — set another model as default instead of unsetting it",
                           {"model_id": row.model_id}, status_code=409)
    row.updated_by = actor
    db.flush()
    _journal(db, "model.update", actor, row.id, before, model_dict(row, provider),
             scope=_journal_scope(provider))
    db.commit()
    refresh(db)
    if before.get("enabled") and not row.enabled:
        from app.services import model_roles

        model_roles.on_model_unavailable(db, {row.model_id}, actor)  # rev-r25 F3
    return row


def delete_model(db: Session, provider: ModelProvider, row: Model, actor: str) -> None:
    if row.source == mc.SOURCE_BUILTIN and provider.builtin:
        raise AppError(E_MODEL_BUILTIN, f"{row.model_id} is a built-in row — disable it "
                       "instead of deleting it", {"model_id": row.model_id}, status_code=409)
    if row.is_default:
        raise AppError(E_MODEL_IS_DEFAULT, f"{row.model_id} is the default {row.kind} model",
                       {"model_id": row.model_id}, status_code=409)
    refs = referrers(db, {row.model_id})
    if refs:
        raise AppError(E_MODEL_IN_USE, f"{row.model_id} is referenced by agents, evaluators "
                       "or settings — disable it instead, or repoint them first",
                       {"referrers": refs, "suggest": "disable"}, status_code=409)
    before = model_dict(row, provider)
    for flag in db.scalars(select(WorkspaceModelSetting).where(
            WorkspaceModelSetting.model_id == row.model_id)):
        db.delete(flag)
    db.delete(row)
    db.flush()
    _journal(db, "model.delete", actor, before["id"], before, None,
             scope=_journal_scope(provider))
    db.commit()
    refresh(db)
    from app.services import model_roles

    model_roles.on_model_unavailable(db, {row.model_id}, actor)  # rev-r25 F3


# ── referrers (plan §3 / §9.5) ───────────────────────────────────────────────────────
def _assert_no_role_points_at(model_id: str) -> None:
    refs = [r for r in setting_referrers({model_id}) if r.get("role")]
    if refs:
        roles = ", ".join(str(r["role"]) for r in refs)
        raise AppError(E_MODEL_IN_USE, f"{model_id} is the model of the platform role(s) "
                       f"{roles} — repoint them on the Model roles tab first",
                       {"referrers": refs, "suggest": "repoint"}, status_code=409)


def referrers(db: Session, model_ids: set[str]) -> list[dict[str, Any]]:
    """Everything that names one of ``model_ids``: non-deleted agents (`spec.model_id`,
    BYOC `allowed_models`, Studio node models), evaluators (`eval_evaluators.model_id`) and
    the hub's model Settings. Secret-free; rendered by the console on a 409."""
    if not model_ids:
        return []
    from app.schemas.agent import studio_node_models

    out: list[dict[str, Any]] = []
    for agent in db.scalars(select(Agent).where(Agent.status != "deleted")):
        spec = agent.spec if isinstance(agent.spec, dict) else {}
        ids = {spec.get("model_id")}
        byoc = spec.get("byoc") if isinstance(spec.get("byoc"), dict) else {}
        ids.update(byoc.get("allowed_models") or [])
        ids.update(studio_node_models(spec.get("studio_flow")))
        hit = sorted(str(m) for m in ids & model_ids if m)
        if hit:
            out.append({"kind": "agent", "type": "agent", "id": agent.id, "name": agent.name,
                        "workspace_id": agent.workspace_id, "model_ids": hit,
                        "system_key": getattr(agent, "system_key", None)})  # N17-obs207
    from app.evaluation.models import EvalEvaluator

    for ev in db.scalars(select(EvalEvaluator).where(EvalEvaluator.model_id.in_(model_ids))):
        out.append({"kind": "evaluator", "type": "evaluator", "id": ev.id,
                    "name": getattr(ev, "name", ev.id),
                    "workspace_id": getattr(ev, "workspace_id", None),
                    "model_ids": [ev.model_id]})
    out.extend(setting_referrers(model_ids))
    return out


def setting_referrers(model_ids: set[str]) -> list[dict[str, Any]]:
    """The hub Settings naming one of ``model_ids``: every model role's EFFECTIVE value
    (r15-e — the console override when one is set, else env / yaml / default; `id` = the
    Settings field, `role` = the role name) and the optimizer's model list."""
    from app.services import model_roles

    out: list[dict[str, Any]] = []
    settings = get_settings()
    for r in model_roles.ROLES:
        # rev-r25 F3 (N17-obs252): a role of a hidden console feature never blocks a
        # disable / delete — the administrator cannot see it to repoint it; its override
        # is dropped afterwards instead (`model_roles.on_model_unavailable`)
        if model_roles.role_hidden(r, settings):
            continue
        value = str(getattr(settings, r.field, "") or "").strip()
        if value in model_ids:
            out.append({"kind": "setting", "type": "setting", "id": r.field, "name": r.field,
                        "role": r.name, "workspace_id": None, "model_ids": [value]})
    hit = sorted(m for m in (getattr(settings, "prompt_opt_models", None) or [])
                 if m in model_ids)
    if hit:
        out.append({"kind": "setting", "type": "setting", "id": "prompt_opt_models",
                    "name": "prompt_opt_models",
                    "workspace_id": None, "model_ids": hit})
    return out


# ── per-workspace flags (plan §2.4) ──────────────────────────────────────────────────
def set_workspace_flags(db: Session, workspace_id: str, disabled_models: Iterable[str],
                        actor: str) -> dict[str, Any]:
    """Replace the workspace's disabled list; returns the `workspaces[]` element."""
    ws = db.get(Workspace, workspace_id)
    if ws is None:
        raise NotFoundError("workspace.not_found", f"workspace {workspace_id!r} not found")
    wanted = sorted({str(m) for m in disabled_models if str(m).strip()})
    known = set(db.scalars(select(Model.model_id)))
    unknown = [m for m in wanted if m not in known]
    if unknown:
        raise AppError(E_MODEL_UNKNOWN, "unknown model id(s): " + ", ".join(unknown),
                       {"model_ids": unknown}, status_code=422)
    before = workspace_flags(db).get(workspace_id, [])
    for row in db.scalars(select(WorkspaceModelSetting).where(
            WorkspaceModelSetting.workspace_id == workspace_id)):
        db.delete(row)
    db.flush()
    for model_id in wanted:
        db.add(WorkspaceModelSetting(workspace_id=workspace_id, model_id=model_id,
                                     enabled=False, updated_by=actor))
    db.flush()
    _journal(db, "workspace.flags", actor, workspace_id,
             {"disabled_models": before}, {"disabled_models": wanted})
    db.commit()
    refresh(db)
    return {"id": ws.id, "name": ws.name, "disabled_models": wanted, "workspace_id": ws.id}


def workspaces_dict(db: Session) -> list[dict[str, Any]]:
    flags = workspace_flags(db)
    return [{"id": ws.id, "name": ws.name, "disabled_models": flags.get(ws.id, [])}
            for ws in db.scalars(select(Workspace).order_by(Workspace.created_at, Workspace.id))]


# ── the page payload ─────────────────────────────────────────────────────────────────
def registry_dict(db: Session, identity: Identity | None = None) -> dict[str, Any]:
    """The page payload. ``identity`` (N17-obs203) narrows it to what the caller may see: a
    member gets the workspace-scope providers plus their own personal ones (and only those
    providers' models and probe warnings); the per-workspace flag matrix is an
    administrator's view and reads `[]` for a member."""
    providers = visible_providers(db, identity)
    counts = model_counts(db)
    by_id = {p.id: p for p in providers}
    member = identity is not None and not identity.is_admin
    return {
        "catalog_version": stamp(db),
        "providers": [provider_dict(p, counts.get(p.id, 0), identity=identity)
                      for p in providers],
        "models": [model_dict(m, by_id[m.provider_id]) for m in _models(db)
                   if m.provider_id in by_id],
        "workspaces": [] if member else workspaces_dict(db),
        "warnings": warnings(db, identity),
    }


def _iso(value: datetime | None) -> str | None:
    """ISO-8601 in UTC with an explicit offset. SQLite hands back naive datetimes while a row
    still in the session carries `tzinfo=UTC`; both must render the same string so a POST
    response equals the following GET byte-for-byte."""
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()
