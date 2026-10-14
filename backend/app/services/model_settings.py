"""Platform model settings: the roles, console overrides, and a tiny probe.

Every model the platform itself calls or pre-selects is a ``Settings`` field (env
``LAUNCHPAD_<FIELD>``, or ``launchpad.yaml``), and a *role* here. An administrator
can override a role on the console's Models page; the override is a
``platform_settings`` ledger row. Precedence: console > env > launchpad.yaml >
built-in default.

How an override reaches the consumers — which all read ``get_settings().<field>``
per call: this module keeps a process-wide snapshot of the ledger's overrides and
registers a hook with ``app.core.config`` that applies it to **every** freshly
built ``Settings``. ``get_settings.cache_clear()`` is called at runtime (the price
refresher, default-workspace resource writes), and without the hook each clear
would silently revert every override to env / yaml / default. The snapshot is
loaded from the ledger at startup (``load_overrides``) and replaced on every set
or clear, which also updates the cached ``Settings`` in place, so a write takes
effect without a restart.

The backend runs as one uvicorn process per box, and each box has its own ledger,
so overrides are per deployment (prod and prod2 each keep their own).
"""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from botocore.config import Config
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings, register_settings_hook
from app.core.errors import AppError
from app.models.platform_settings import PlatformSetting
from app.services.workspace import WorkspaceContext

ProbeCheck = Literal["converse", "tool_use"]
# The model-id shape the platform accepts elsewhere (assistant proposals, eval plans).
MODEL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{2,120}$")
_GEO_PREFIXES = frozenset({"global", "us", "eu", "apac", "jp", "au", "ca"})


@dataclass(frozen=True)
class Role:
    field: str
    # Model families the feature can drive (None = any): the Claude Agent SDK /
    # Claude Code CLI paths take Claude only, codex takes OpenAI only.
    families: tuple[str, ...] | None = None
    allow_empty: bool = False  # "" is a valid value (judge fallback = none)
    # The role runs an agent / tool loop, so its model must accept Converse toolConfig.
    needs_tools: bool = False


ROLES: tuple[Role, ...] = (
    Role("agent_default_model_id", needs_tools=True),
    Role("claude_sdk_model_id", families=("anthropic",), needs_tools=True),
    Role("judge_model_id"),
    Role("judge_fallback_model_id", allow_empty=True),
    Role("assistant_model_id", needs_tools=True),
    Role("architect_model_id", needs_tools=True),
    Role("actor_model_id", needs_tools=True),
    Role("studio_fallback_model_id", needs_tools=True),
    Role("codegen_model", families=("anthropic",)),
    Role("prompt_opt_default_model_id"),
    # openai.* runs on the host codex CLI, everything else on the claude CLI
    Role("skill_lab_judge_model_id", families=("openai", "anthropic")),
    Role("skill_lab_target_model_id", families=("anthropic",)),
    Role("skill_lab_codex_target_model_id", families=("openai",)),
)
ROLE_BY_FIELD = {r.field: r for r in ROLES}
# Bedrock models measured to refuse toolConfig ("This model doesn't support tool
# use", us-east-1 + us-west-2, 2026-10-12): fine for plain Converse roles, never
# for a role that runs tools.
NO_TOOL_USE = re.compile(r"(^|\.)deepseek\.r1")


def role(field: str) -> Role:
    found = ROLE_BY_FIELD.get(field)
    if found is None:
        raise AppError("models.unknown_role", f"unknown model role {field!r}",
                       status_code=404)
    return found


def model_family(model_id: str) -> str:
    """``anthropic`` for ``global.anthropic.x`` / ``anthropic.x`` and so on."""
    parts = model_id.split(".")
    if len(parts) >= 3 and parts[0] in _GEO_PREFIXES:
        return parts[1]
    return parts[0] if len(parts) >= 2 else ""


def validate_model_id(model_id: str) -> str:
    value = (model_id or "").strip()
    if not MODEL_ID_RE.match(value):
        raise AppError("models.invalid_id", f"{value!r} is not a Bedrock model id")
    return value


def validate(r: Role, model_id: str) -> str:
    value = (model_id or "").strip()
    if not value:
        if r.allow_empty:
            return ""
        raise AppError("models.empty", f"{r.field} needs a model id")
    validate_model_id(value)
    if r.needs_tools and NO_TOOL_USE.search(value):
        raise AppError("models.no_tool_use",
                       f"{r.field} runs tools, and {value!r} does not support tool use")
    if r.families and model_family(value) not in r.families:
        allowed = " / ".join(r.families)
        raise AppError(
            "models.wrong_family",
            f"{r.field} drives a feature that runs {allowed} models only; {value!r} is not one",
        )
    return value


# ── the override snapshot + the get_settings hook ───────────────────────────

_LOCK = threading.RLock()
_overrides: dict[str, str] = {}
# The role values of one Settings object BEFORE any override was put on it, i.e.
# what env / launchpad.yaml / the defaults resolve to: what a reset returns to.
_baseline: tuple[Settings | None, dict[str, Any]] = (None, {})


def _record_baseline(settings: Settings) -> dict[str, Any]:
    global _baseline
    _baseline = (settings, {r.field: getattr(settings, r.field) for r in ROLES})
    return _baseline[1]


def _baseline_of(settings: Settings) -> dict[str, Any]:
    # Every in-place application goes through here first, so an object without a
    # recorded baseline (cached before this module registered its hook) carries no
    # override yet and its current values ARE the baseline.
    global _baseline
    owner, values = _baseline
    if owner is settings:
        return values
    if owner is None:
        return _record_baseline(settings)
    # A sibling: two threads missed the cleared cache at once, both builds went
    # through the hook (the last one recorded the baseline), and lru_cache kept the
    # other. Its overridden fields already carry the override, so their baseline is
    # the sibling's — built from the same env / yaml — not their current value
    # (which would make a reset a no-op and report the override as the default).
    current = {r.field: getattr(settings, r.field) for r in ROLES}
    for field in _overrides:
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
            # Cold cache: the next build applies the snapshot through the hook.
            # Building one here would bake in whatever env happens to be set now.
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
    return {
        row.key: row
        for row in db.query(PlatformSetting).filter(
            PlatformSetting.key.in_([r.field for r in ROLES])
        )
    }


def load_overrides(db: Session) -> None:
    """(Re)load the snapshot from the ledger — at startup, after ``init_db``."""
    _sync({key: row.value for key, row in _rows(db).items()})


def reset_overrides() -> None:
    """Drop the in-process snapshot (tests; the ledger is not touched)."""
    _sync({})


def overrides() -> dict[str, str]:
    with _LOCK:
        return dict(_overrides)


# ── reads + writes ──────────────────────────────────────────────────────────


def effective(db: Session) -> list[dict[str, Any]]:
    """Every role: its effective model, what a reset returns to, and who overrode it."""
    rows = _rows(db)
    with _LOCK:
        settings = get_settings()
        baseline = _baseline_of(settings)
        out = []
        for r in ROLES:
            row = rows.get(r.field)
            out.append({
                "role": r.field,
                "model_id": str(getattr(settings, r.field) or "").strip(),
                "default": str(baseline[r.field] or "").strip(),
                "overridden": row is not None,
                "constraint": list(r.families) if r.families else None,
                "allow_empty": r.allow_empty,
                "needs_tools": r.needs_tools,
                "updated_by": row.updated_by if row is not None else None,
                "updated_at": row.updated_at.isoformat() if row is not None else None,
            })
    return out


def _one(db: Session, field: str) -> dict[str, Any]:
    return next(item for item in effective(db) if item["role"] == field)


def set_override(db: Session, field: str, model_id: str, actor: str) -> dict[str, Any]:
    r = role(field)
    value = validate(r, model_id)
    row = db.get(PlatformSetting, r.field)
    if row is None:
        row = PlatformSetting(key=r.field)
        db.add(row)
    row.value, row.updated_by, row.updated_at = value, actor[:64], datetime.now(UTC)
    db.commit()
    load_overrides(db)
    return _one(db, r.field)


def clear_override(db: Session, field: str) -> dict[str, Any]:
    r = role(field)
    row = db.get(PlatformSetting, r.field)
    if row is not None:
        db.delete(row)
        db.commit()
    load_overrides(db)
    return _one(db, r.field)


# ── probe ───────────────────────────────────────────────────────────────────

_PROBE_TOOL = {"toolSpec": {
    "name": "reply", "description": "Send the reply.",
    "inputSchema": {"json": {"type": "object", "properties": {"message": {"type": "string"}},
                             "required": ["message"]}}}}


def _short_cfg() -> Config:
    return Config(connect_timeout=5, read_timeout=30, retries={"max_attempts": 1})


def probe(model_id: str, check: ProbeCheck, workspace: WorkspaceContext) -> dict[str, Any]:
    """One small real Converse call in ``workspace`` — plain, or carrying a tool
    definition (``tool_use``) — reporting OK or the AWS error. Read-only: nothing is
    created in the account."""
    value = validate_model_id(model_id)
    if check not in ("converse", "tool_use"):
        raise AppError("models.invalid_check", f"unknown probe check {check!r}")
    kwargs: dict[str, Any] = {
        "modelId": value,
        "messages": [{"role": "user", "content": [{"text": "Reply with OK."}]}],
        # 16, not less: GPT-6 refuses a max_output_tokens below 16
        "inferenceConfig": {"maxTokens": 16},
    }
    if check == "tool_use":
        kwargs["messages"] = [
            {"role": "user", "content": [{"text": "Say hi through the reply tool."}]}
        ]
        kwargs["inferenceConfig"] = {"maxTokens": 256}
        kwargs["toolConfig"] = {"tools": [_PROBE_TOOL], "toolChoice": {"auto": {}}}
    out: dict[str, Any] = {"model_id": value, "check": check, "region": workspace.region}
    runtime = workspace.client("bedrock-runtime", cache_token="model-probe",
                               config=_short_cfg())
    started = time.monotonic()
    try:
        response = runtime.converse(**kwargs)
    except Exception as exc:
        error = (getattr(exc, "response", None) or {}).get("Error") or {}
        out.update(ok=False, latency_ms=int((time.monotonic() - started) * 1000),
                   error_code=str(error.get("Code") or type(exc).__name__),
                   message=str(error.get("Message") or exc)[:300])
        return out
    out.update(ok=True, latency_ms=int((time.monotonic() - started) * 1000),
               stop_reason=(response or {}).get("stopReason"))
    return out
