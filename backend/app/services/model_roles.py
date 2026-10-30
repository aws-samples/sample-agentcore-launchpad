"""Model roles (r15-e — the useful subset of upstream #275): which catalog model the
platform itself uses or pre-selects for each job, and the administrator's overrides.

Every role is a ``Settings`` field (env ``LAUNCHPAD_<FIELD>`` / ``launchpad.yaml`` / the
built-in default). An administrator overrides one on Settings → Model Providers → *Model
roles*; the override is a hub-global ``platform_settings`` ledger row (`key` = the Settings
field). Precedence: console override > env > launchpad.yaml > default.

How an override reaches the consumers — which all read ``get_settings().<field>`` per call:
this module keeps a process-wide snapshot of the ledger's overrides and registers a hook with
``app.core.config`` that applies it to **every** freshly built ``Settings``.
``get_settings.cache_clear()`` is called at runtime (default-workspace resource writes, the
test suite), and without the hook each clear would silently revert every override to env /
yaml / default. The snapshot is loaded from the ledger at startup (``load_overrides``) and
replaced on every set / clear, which also updates the cached ``Settings`` in place, so a
write takes effect without a restart. ``effective()`` re-syncs from the ledger when its rows
differ from the snapshot (a second process wrote), so the admin page is always current.

A role accepts only a model that is **in the catalog and enabled** (D21 disable semantics —
model row, provider, hub-wide) and, for a role that runs tools, whose capabilities include
tool use; anything else is 422 ``models.model_not_allowed {role, model_id, reason}``. The
effective role values are referrers of the registry (`model_registry.referrers`), so a model a
role points at can neither be deleted nor disabled (409 ``model.in_use``) until the role is
repointed. Every set / reset writes one ``model_catalog_changes`` row (``model_role.set`` /
``model_role.reset`` — audit-only, never a catalog version: a role changes nothing a rendered
runtime or a picker's option list could observe).

Not ported from upstream (user decision 2026-10-19): the Claude SDK / Studio fallback /
codex roles, the price table and the GPT-6 default. There is no ``assistant`` role either:
the system presets follow ``agent_default`` at install / repair time (N17-obs202).

N17-obs252 (user ruling 2026-10-22 "需要都读模型角色来确定模型"): every platform model
choice is a role — ``memory_extract`` (the extraction Lambda's summariser) and ``codegen``
(the Studio coding agent, `Settings.codegen_model`) join the Settings-backed roles, each
tagged with the console ``feature`` it belongs to so a hidden feature hides its roles; and
``embedding`` is the registry's ACTIVE embedding row (`schemas.agent.active_embedding`): no
Settings field, listed by ``effective()`` and changed through the registry's own guarded
default flip (`model_registry.update_model(..., {"is_default": True})` — 409
``embedding.reindex_required`` while any vector index holds documents, never weakened).
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings, register_settings_hook
from app.core.errors import AppError
from app.models.model_registry import PlatformSetting
from app.schemas import model_catalog as mc

E_UNKNOWN_ROLE = "models.unknown_role"
E_MODEL_NOT_ALLOWED = "models.model_not_allowed"

OP_SET = "model_role.set"
OP_RESET = "model_role.reset"


@dataclass(frozen=True)
class Role:
    name: str  # the API name (`agent_default`, `judge`, …)
    field: str  # the `Settings` field it reads (= the `platform_settings` key); "" = none
    # The role runs an agent / tool loop, so its model must support tool use.
    needs_tools: bool = False
    # "" is a valid value (judge fallback = none).
    allow_empty: bool = False
    # N17-obs252: `chat` (a Settings-backed role) or `embedding` (the registry's active row)
    kind: str = mc.MODEL_KIND_CHAT
    # N17-obs252: the console feature the role belongs to (`skill_lab`, `studio`); the roles
    # view hides the row while that feature is hidden. None = always shown.
    feature: str | None = None


ROLES: tuple[Role, ...] = (
    # the backend default of a spec without `model_id` + the V2 wizard pre-selection
    Role("agent_default", "agent_default_model_id", needs_tools=True),
    # strands_evals judge (`services/strands_eval`, `evaluation/judge_budget.py`)
    Role("judge", "eval_judge_model_id"),
    # reserved — stored, served, referenced; no consumer falls back to it in this build
    Role("judge_fallback", "eval_judge_fallback_model_id", allow_empty=True),
    # N17-obs449: the insights FailureAnalysis detector's judge — its root-cause step forces a
    # structured-output tool call, so it must support tools
    Role("failure_detection", "eval_failure_detection_model_id", needs_tools=True),
    # simulated-user actor of persona scenarios (Converse forced tool choice)
    Role("actor", "eval_actor_model_id", needs_tools=True),
    # the recommendation providers' default reflection model
    Role("prompt_opt", "prompt_opt_default_model_id"),
    # Skill Lab judge (`bedrock_chat`) and OpenCode target (runs the agent's tools)
    Role("skill_lab_judge", "skill_lab_judge_model_id", feature="skill_lab"),
    Role("skill_lab_target", "skill_lab_target_model_id", needs_tools=True,
         feature="skill_lab"),
    # N17-obs252: the memory extraction summariser (hub names it in every `memory_extract`
    # message; the Lambda uses it — its input limit bounds one extraction, N17-obs237)
    Role("memory_extract", "memory_extract_model_id"),
    # N17-obs252: the Studio coding agent (`codegen/config.py` reads it per call)
    Role("codegen", "codegen_model", feature="studio"),
)
# N17-obs252: the ACTIVE embedding row of the registry — not a Settings field. Listed with
# the roles, saved through the registry's guarded default flip (see `set_override`).
EMBEDDING_ROLE = Role("embedding", "", kind=mc.MODEL_KIND_EMBEDDING)
ROLE_BY_NAME = {r.name: r for r in (*ROLES, EMBEDDING_ROLE)}
ROLE_BY_FIELD = {r.field: r for r in ROLES}
FIELDS: tuple[str, ...] = tuple(r.field for r in ROLES)


def role(name: str) -> Role:
    found = ROLE_BY_NAME.get(name)
    if found is None:
        raise AppError(E_UNKNOWN_ROLE, f"unknown model role {name!r} (roles: "
                       + ", ".join(ROLE_BY_NAME) + ")", {"role": name}, status_code=404)
    return found


def _refuse(r: Role, model_id: str, reason: str, why: str) -> AppError:
    return AppError(E_MODEL_NOT_ALLOWED, f"{r.name}: {model_id!r} {why}",
                    {"role": r.name, "model_id": model_id, "reason": reason}, status_code=422)


def validate(r: Role, model_id: str, catalog: mc.Catalog | None = None) -> str:
    """The value a role may take: ``""`` only where allowed; otherwise an **enabled chat
    row** of the catalog (model + provider enabled, hub-wide — the per-workspace flags are a
    member-side picker rule, not a platform one) that supports tools when the role runs
    them. 422 ``models.model_not_allowed {role, model_id, reason}`` otherwise."""
    value = (model_id or "").strip()
    if not value:
        if r.allow_empty:
            return ""
        raise _refuse(r, value, "empty", "needs a model id")
    snapshot = catalog if catalog is not None else mc.current()
    row = snapshot.models.get(value)
    if row is None:
        raise _refuse(r, value, "not_in_catalog",
                      "is not in the model catalog (Settings → Model Providers)")
    if row.kind != r.kind:
        # N17-obs252: the embedding role takes embedding rows only; every other role a chat row
        reason = "not_chat" if r.kind == mc.MODEL_KIND_CHAT else f"not_{r.kind}"
        raise _refuse(r, value, reason, f"is a {row.kind} row, not a {r.kind} model")
    if not snapshot.model_enabled(value):
        raise _refuse(r, value, "disabled",
                      "is disabled in the model registry (model or provider) — enable it first")
    # N17-obs203 §6: a platform role runs for every member — a model of someone's PERSONAL
    # provider can never be the platform's; share the provider (scope: workspace) first
    if not snapshot.model_visible(value, mc.WORKSPACE_ONLY):
        raise _refuse(r, value, "personal_scope",
                      "belongs to a personal provider — the platform's roles accept "
                      "workspace-scope (shared) providers only; share it first")
    if r.needs_tools and not row.tools:
        raise _refuse(r, value, "no_tool_use",
                      "does not support tool use, and this role runs tools")
    return value


def _default_problem(r: Role, default: str) -> str | None:
    """N16-obs22 (lead ruling): the reason the env / yaml / built-in default is NOT a value
    the role may take today (`disabled`, `not_in_catalog`, …), or None when it is usable."""
    try:
        validate(r, default)
    except AppError as exc:
        return str((exc.detail or {}).get("reason") or "disabled")
    return None


# ── the override snapshot + the get_settings hook ───────────────────────────

logger = logging.getLogger("launchpad.model_roles")

_LOCK = threading.RLock()
_overrides: dict[str, str] = {}
# The role values of one Settings object BEFORE any override was put on it, i.e. what env /
# launchpad.yaml / the defaults resolve to: what a reset returns to.
_baseline: tuple[Settings | None, dict[str, str]] = (None, {})


def _values(settings: Settings) -> dict[str, str]:
    return {field: str(getattr(settings, field, "") or "") for field in FIELDS}


def _record_baseline(settings: Settings) -> dict[str, str]:
    global _baseline
    _baseline = (settings, _values(settings))
    return _baseline[1]


def _baseline_of(settings: Settings) -> dict[str, str]:
    # Every in-place application goes through here first, so an object without a recorded
    # baseline (cached before this module registered its hook) carries no override yet and
    # its current values ARE the baseline.
    global _baseline
    owner, values = _baseline
    if owner is settings:
        return values
    if owner is None:
        return _record_baseline(settings)
    # A sibling: two threads missed the cleared cache at once, both builds went through the
    # hook (the last one recorded the baseline), and lru_cache kept the other. Its overridden
    # fields already carry the override, so their baseline is the sibling's — built from the
    # same env / yaml — not their current value (which would make a reset a no-op and report
    # the override as the default).
    current = _values(settings)
    for field in _overrides:
        if field in values:
            current[field] = values[field]
    _baseline = (settings, current)
    return current


def _apply_snapshot(settings: Settings) -> None:
    """The ``get_settings`` hook: a freshly built Settings gets the overrides."""
    with _LOCK:
        _record_baseline(settings)
        for field, value in _overrides.items():
            setattr(settings, field, value)


register_settings_hook(_apply_snapshot)


def _sync(new: dict[str, str]) -> None:
    """Replace the snapshot and bring the cached Settings in line, in place."""
    global _overrides
    with _LOCK:
        if get_settings.cache_info().currsize == 0:
            # Cold cache: the next build applies the snapshot through the hook. Building one
            # here would bake in whatever env happens to be set now.
            _overrides = dict(new)
            return
        settings = get_settings()
        baseline = _baseline_of(settings)
        for field in _overrides.keys() - new.keys():
            setattr(settings, field, baseline[field])
        for field, value in new.items():
            setattr(settings, field, value)
        _overrides = dict(new)


def _rows(db: Session) -> dict[str, PlatformSetting]:
    return {row.key: row for row in db.query(PlatformSetting).filter(
        PlatformSetting.key.in_(list(FIELDS)))}


def load_overrides(db: Session) -> None:
    """(Re)load the snapshot from the ledger — at startup, after ``init_db``."""
    _sync({key: str(row.value or "") for key, row in _rows(db).items()})


def reset_overrides() -> None:
    """Drop the in-process snapshot (tests; the ledger is not touched)."""
    _sync({})


def ensure_loaded(db: Session) -> None:
    """Bring this process's snapshot in line with the ledger when they differ (rev-r25 F1 on
    N17-obs202): a process that never ran ``load_overrides`` — the bootstrap CLI installing
    the evaluation worker, a second API process — would otherwise resolve every role to env /
    yaml / built-in and, for a role-following preset, re-publish it on the wrong model. One
    small query; a no-op when nothing moved."""
    stored = {key: str(row.value or "") for key, row in _rows(db).items()}
    with _LOCK:
        if stored != _overrides:
            _sync(stored)


def role_hidden(r: Role, settings: Settings | None = None) -> bool:
    """rev-r25 F3 (N17-obs252): the role belongs to a console feature that is hidden right
    now — `Settings.hidden_role_features` (the server-side mirror of the console's
    `HIDDEN_SURFACES.features` / `.createEntries.studio`) or a feature the deployment does not
    expose (`Settings.features`). A hidden role is listed by ``effective()`` (the console
    filters it) but is never a registry referrer."""
    if not r.feature:
        return False
    current = settings or get_settings()
    if r.feature in (current.hidden_role_features or []):
        return True
    from app.core.features import FEATURES

    return r.feature in FEATURES and r.feature not in (current.features or [])


def resolve(name: str) -> str:
    """The model a consumer should use for ``name`` NOW: the effective value when the catalog
    accepts it, else the role's built-in / env default when THAT is accepted (one WARNING per
    change), else the effective value unchanged (nothing better exists — `default_disabled`
    flags it on the roles view). rev-r25 F3: a re-enabled feature never points at a dead
    model."""
    r = role(name)
    settings = get_settings()
    value = str(getattr(settings, r.field, "") or "").strip()
    if r.allow_empty and not value:
        return value
    try:
        validate(r, value)
        return value
    except AppError as exc:
        reason = str((exc.detail or {}).get("reason") or "disabled")
    with _LOCK:
        default = str(_baseline_of(settings).get(r.field, "") or "").strip()
    if default and default != value and _default_problem(r, default) is None:
        _warn_once(f"{r.name}:{value}->{default}",
                   "model role %s: %r is %s — using the default %r until the role is repointed",
                   r.name, value, reason, default)
        return default
    return value


_warned: set[str] = set()


def _warn_once(key: str, message: str, *args: Any) -> None:
    with _LOCK:
        if key in _warned:
            return
        _warned.add(key)
    logger.warning(message, *args)


def on_model_unavailable(db: Session, model_ids: set[str], actor: str = "registry") -> None:
    """rev-r25 F3: called by the registry after a model was disabled / deleted (or its
    provider was). A VISIBLE role pointing at it was refused before this point (409
    `model.in_use`); a HIDDEN role's console override is dropped here — the role returns to
    its env / yaml / built-in default, journaled `model_role.reset`, one WARNING — so the
    feature, once shown again, never points at a dead model. A role whose DEFAULT is the
    dead model keeps it (nothing better exists) and is flagged `default_disabled`."""
    for r in ROLES:
        value = str(getattr(get_settings(), r.field, "") or "").strip()
        if value not in model_ids:
            continue
        if r.field in overrides():
            try:
                clear_override(db, r.name, actor)
                logger.warning("model role %s: its model %r was disabled or deleted — the "
                               "override was dropped, the role is back at its default", r.name,
                               value)
            except AppError as exc:  # the default is unusable too: keep the override, warn
                # the role's row is hidden on the Model roles tab: name the layers that work
                logger.warning("model role %s: its model %r was disabled or deleted and the "
                               "default is not usable either (%s) — set it through "
                               "LAUNCHPAD_%s or config/launchpad.yaml", r.name, value,
                               exc.message, r.field.upper())
        else:
            logger.warning("model role %s: its default model %r was disabled or deleted — set "
                           "it through LAUNCHPAD_%s or config/launchpad.yaml", r.name, value,
                           r.field.upper())


def overrides() -> dict[str, str]:
    with _LOCK:
        return dict(_overrides)


# ── reads + writes ──────────────────────────────────────────────────────────


def _iso(value: datetime | None) -> str | None:
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()


def effective(db: Session) -> list[dict[str, Any]]:
    """Every role: its effective model, what a reset returns to, and who overrode it. Re-syncs
    the snapshot when the ledger rows differ from it (a second process wrote)."""
    rows = _rows(db)
    stored = {key: str(row.value or "") for key, row in rows.items()}
    with _LOCK:
        if stored != _overrides:
            _sync(stored)
        settings = get_settings()
        baseline = _baseline_of(settings)
        out = []
        for r in ROLES:
            row = rows.get(r.field)
            out.append({
                "role": r.name,
                "field": r.field,
                "model_id": str(getattr(settings, r.field, "") or "").strip(),
                "default": str(baseline.get(r.field, "") or "").strip(),
                "overridden_by": (row.updated_by or "") if row is not None else None,
                "updated_at": _iso(row.updated_at) if row is not None else None,
                "needs_tools": r.needs_tools,
                "allow_empty": r.allow_empty,
                # N16-obs22: the value a reset would return to is not usable right now
                "default_disabled": _default_problem(
                    r, str(baseline.get(r.field, "") or "").strip()) is not None,
                "kind": r.kind,
                "feature": r.feature,
            })
        out.append(_embedding_row(db))
    return out


def _embedding_row(db: Session) -> dict[str, Any]:
    """N17-obs252: the registry's ACTIVE embedding row as a role row. `model_id` = the active
    row (`schemas.agent.active_embedding`), `default` = the built-in Titan row, `overridden_by`
    = who flipped the default when it is not the built-in one (the Model row's `updated_by`)."""
    from sqlalchemy import select

    from app.models.model_registry import Model

    active = mc.current().embedding_model()
    model_id = active.model_id if active else ""
    overridden = bool(model_id) and model_id != mc.BUILTIN_EMBEDDING_MODEL_ID
    row = db.scalar(select(Model).where(Model.model_id == model_id)) if overridden else None
    return {
        "role": EMBEDDING_ROLE.name,
        "field": "",
        "model_id": model_id,
        "default": mc.BUILTIN_EMBEDDING_MODEL_ID,
        "overridden_by": ((row.updated_by if row is not None else None) or "") if overridden
        else None,
        "updated_at": _iso(row.updated_at) if row is not None else None,
        "needs_tools": False,
        "allow_empty": False,
        "default_disabled": _default_problem(EMBEDDING_ROLE, mc.BUILTIN_EMBEDDING_MODEL_ID)
        is not None,
        "kind": EMBEDDING_ROLE.kind,
        "feature": None,
    }


def _set_active_embedding(db: Session, model_id: str, actor: str) -> dict[str, Any]:
    """N17-obs252: the embedding "override" IS the registry's default flip — the same
    `model_registry.update_model(..., {"is_default": True})` the model table uses, so
    `embedding_guard` (409 `embedding.reindex_required` while a vector index holds
    documents) and the `model.update` journal / catalog version apply unchanged."""
    from sqlalchemy import select

    from app.models.model_registry import Model, ModelProvider
    from app.services import model_registry

    value = validate(EMBEDDING_ROLE, model_id)
    row = db.scalar(select(Model).where(Model.model_id == value))
    if row is None:  # pragma: no cover — validate() proved the catalog knows it
        raise _refuse(EMBEDDING_ROLE, value, "not_in_catalog", "is not a registry row")
    if not row.is_default:
        provider = db.get(ModelProvider, row.provider_id)
        model_registry.update_model(db, provider, row, {"is_default": True}, actor)
    return _embedding_row(db)


def effective_values() -> dict[str, str]:
    """``{role: model_id}`` from the cached Settings only (no ledger read) — the referrer
    rule of the registry reads this; empty values are dropped."""
    settings = get_settings()
    return {r.name: str(getattr(settings, r.field, "") or "").strip() for r in ROLES
            if str(getattr(settings, r.field, "") or "").strip()}


def _one(db: Session, name: str) -> dict[str, Any]:
    return next(item for item in effective(db) if item["role"] == name)


def set_override(db: Session, name: str, model_id: str, actor: str) -> dict[str, Any]:
    """Validate, store the row, re-apply the snapshot, journal ``model_role.set``."""
    from app.services import model_registry

    r = role(name)
    if r is EMBEDDING_ROLE:
        return _set_active_embedding(db, model_id, actor)
    value = validate(r, model_id)
    before = _one(db, r.name)
    row = db.get(PlatformSetting, r.field)
    if row is None:
        row = PlatformSetting(key=r.field)
        db.add(row)
    row.value, row.updated_by, row.updated_at = value, (actor or "")[:64], datetime.now(UTC)
    db.flush()
    model_registry._journal(db, OP_SET, actor, r.name,
                            {"field": r.field, "model_id": before["model_id"],
                             "overridden_by": before["overridden_by"]},
                            {"field": r.field, "model_id": value, "overridden_by": actor})
    db.commit()
    load_overrides(db)
    return _one(db, r.name)


def clear_override(db: Session, name: str, actor: str) -> dict[str, Any]:
    """Drop the override (a no-op without one — nothing journaled), back to env / yaml /
    default; journals ``model_role.reset``."""
    from app.services import model_registry

    r = role(name)
    if r is EMBEDDING_ROLE:  # back to the built-in Titan row, through the same guarded flip
        return _set_active_embedding(db, mc.BUILTIN_EMBEDDING_MODEL_ID, actor)
    row = db.get(PlatformSetting, r.field)
    if row is not None:
        before = _one(db, r.name)
        # N16-obs22 (lead ruling): never hand the role a default that is disabled / gone
        problem = _default_problem(r, before["default"])
        if problem is not None:
            raise AppError(E_MODEL_NOT_ALLOWED, f"{r.name}: the configured default "
                           f"{before['default']!r} is {problem} — repoint the role instead of "
                           "resetting it", {"role": r.name, "model_id": before["default"],
                                            "reason": problem, "suggest": "repoint"},
                           status_code=422)
        db.delete(row)
        db.flush()
        model_registry._journal(db, OP_RESET, actor, r.name,
                                {"field": r.field, "model_id": before["model_id"],
                                 "overridden_by": before["overridden_by"]},
                                {"field": r.field, "model_id": before["default"],
                                 "overridden_by": None})
        db.commit()
    load_overrides(db)
    return _one(db, r.name)
