"""Platform model settings: roles, console overrides, the account catalog, probes.

Every model the platform calls or pre-selects is a ``Settings`` field (env
``LAUNCHPAD_<FIELD>``). An administrator can override one on the console's Models
page; the override is a ``platform_settings`` ledger row and is applied to the
process's cached ``Settings`` object, so every consumer — which reads
``get_settings().<field>`` per call — follows it without a restart. Precedence:
console > env > launchpad.yaml > built-in default. The backend runs as one uvicorn
process; overrides are loaded into the console overlay (``core/config``) at startup
and updated on every write, and every Settings object ``get_settings`` builds carries
the overlay — so a runtime ``get_settings.cache_clear()`` (the price refresher, a
bootstrap) never drops a console value.
"""

from __future__ import annotations

import os
import re
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from botocore.config import Config
from sqlalchemy.orm import Session

from app.core.config import (
    Settings,
    clear_console_override,
    console_overrides,
    get_settings,
    load_yaml_config,
    replace_console_overrides,
    set_console_override,
)
from app.core.errors import AppError
from app.models.platform_settings import PlatformSetting
from app.services.workspace import WorkspaceContext

Probe = Literal["converse", "judge", "actor"]
MODEL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{2,120}$")


@dataclass(frozen=True)
class Role:
    field: str
    probe: Probe = "converse"
    # model families a feature can drive (None = any): the Claude Agent SDK / Claude
    # Code CLI paths take Claude only, codex takes OpenAI only
    families: tuple[str, ...] | None = None
    allow_empty: bool = False  # "" is a valid value (judge fallback = none)
    # the role runs an agent / tool loop, so its model must accept Converse toolConfig
    needs_tools: bool = False

    @property
    def env(self) -> str:
        return f"LAUNCHPAD_{self.field.upper()}"


ROLES: tuple[Role, ...] = (
    Role("platform_model_id"),
    Role("judge_model_id", probe="judge"),
    Role("judge_fallback_model_id", probe="judge", allow_empty=True),
    Role("assistant_model_id", needs_tools=True),
    Role("architect_model_id", needs_tools=True),
    Role("assistant_digest_model_id", allow_empty=True),
    Role("agent_default_model_id", needs_tools=True),
    Role("actor_model_id", probe="actor", needs_tools=True),
    Role("codegen_model", families=("anthropic",)),
    Role("prompt_opt_default_model_id"),
    Role("studio_fallback_model_id", needs_tools=True),
    Role("claude_sdk_model_id", families=("anthropic",)),
    # openai.* runs on the host codex CLI, everything else on the claude CLI
    Role("skill_lab_judge_model_id", families=("openai", "anthropic")),
    Role("skill_lab_target_model_id", families=("anthropic",)),
    # codex's amazon-bedrock-runtime provider drives any Responses-API model on
    # bedrock-runtime; Kimi K3 was live-verified on the worker (2026-11-02)
    Role("skill_lab_codex_target_model_id", families=("openai", "moonshotai")),
)
ROLE_BY_FIELD = {r.field: r for r in ROLES}
# Bedrock models measured to refuse toolConfig ("This model doesn't support tool
# use", us-east-1 + us-west-2, 2026-10-12): fine for plain Converse roles (platform,
# judge, prompt optimisation), never for a role that runs tools.
NO_TOOL_USE = re.compile(r"(^|\.)deepseek\.r1")


def supports_tools(model_id: str) -> bool | None:
    """False when the model is known to refuse tool use; None = unknown (probe it)."""
    return False if NO_TOOL_USE.search(model_id) else None


def role(field: str) -> Role:
    found = ROLE_BY_FIELD.get(field)
    if found is None:
        raise AppError("models.unknown_role", f"unknown model setting {field!r}",
                       status_code=404)
    return found


def model_family(model_id: str) -> str:
    """``anthropic`` for ``global.anthropic.x`` / ``anthropic.x`` and so on."""
    parts = model_id.split(".")
    if len(parts) >= 3 and parts[0] in {"global", "us", "eu", "apac", "jp", "au", "ca"}:
        return parts[1]
    return parts[0] if len(parts) >= 2 else ""


def validate(r: Role, model_id: str) -> str:
    value = (model_id or "").strip()
    if not value:
        if r.allow_empty:
            return ""
        raise AppError("models.empty", f"{r.field} needs a model id")
    if not MODEL_ID_RE.match(value):
        raise AppError("models.invalid_id", f"{value!r} is not a Bedrock model id")
    if r.needs_tools and supports_tools(value) is False:
        raise AppError("models.no_tool_use",
                       f"{r.field} runs tools, and {value!r} does not support tool use")
    if r.families and model_family(value) not in r.families:
        allowed = " / ".join(r.families)
        raise AppError(
            "models.wrong_family",
            f"{r.field} drives a feature that runs {allowed} models only; {value!r} is not one",
        )
    return value


# ── overrides ───────────────────────────────────────────────────────────────


def _rows(db: Session) -> dict[str, PlatformSetting]:
    return {
        row.key: row
        for row in db.query(PlatformSetting).filter(
            PlatformSetting.key.in_([r.field for r in ROLES])
        )
    }


def apply_overrides(db: Session) -> None:
    """Load the ledger's console overrides into the console overlay (and so onto the
    cached Settings). A role without a row has no overlay entry: env /
    launchpad.yaml / the default hold for it."""
    replace_console_overrides(
        frozenset(r.field for r in ROLES),
        {key: row.value for key, row in _rows(db).items()},
    )


def _source(r: Role, overlay: dict[str, Any]) -> str:
    if r.field in overlay:
        return "console"
    if r.env in {k.upper() for k in os.environ}:
        return "env"
    if r.field in load_yaml_config_live():
        return "config"
    return "default"


def load_yaml_config_live() -> dict[str, Any]:
    from app.core import config

    return load_yaml_config(config.CONFIG_FILE)


def _env_value(r: Role) -> str | None:
    for key, value in os.environ.items():
        if key.upper() == r.env:
            return value
    return None


def role_rows(db: Session) -> list[dict[str, Any]]:
    settings, rows, overlay = get_settings(), _rows(db), console_overrides()
    out = []
    for r in ROLES:
        row = rows.get(r.field)
        out.append({
            "field": r.field,
            "env": r.env,
            "value": str(getattr(settings, r.field) or ""),
            "default": str(Settings.model_fields[r.field].default or ""),
            "env_value": _env_value(r),
            "console_value": row.value if row is not None else None,
            "source": _source(r, overlay),
            "probe": r.probe,
            "families": list(r.families) if r.families else None,
            "allow_empty": r.allow_empty,
            "needs_tools": r.needs_tools,
            "updated_by": row.updated_by if row is not None else None,
            "updated_at": row.updated_at.isoformat() if row is not None else None,
        })
    return out


def role_row(db: Session, field: str) -> dict[str, Any]:
    return next(row for row in role_rows(db) if row["field"] == field)


def set_override(db: Session, field: str, model_id: str, actor: str) -> dict[str, Any]:
    r = role(field)
    value = validate(r, model_id)
    row = db.get(PlatformSetting, r.field)
    if row is None:
        row = PlatformSetting(key=r.field)
        db.add(row)
    row.value, row.updated_by, row.updated_at = value, actor[:64], datetime.now(UTC)
    db.commit()
    set_console_override(r.field, value)
    return role_row(db, r.field)


def clear_override(db: Session, field: str) -> dict[str, Any]:
    r = role(field)
    row = db.get(PlatformSetting, r.field)
    if row is not None:
        db.delete(row)
        db.commit()
    # back to what env / launchpad.yaml / the default say for this one field
    clear_console_override(r.field)
    return role_row(db, r.field)


def effective_defaults() -> dict[str, str]:
    """The effective value of every role (what /api/models/defaults serves)."""
    settings = get_settings()
    return {r.field: str(getattr(settings, r.field) or "").strip() for r in ROLES}


# ── the account's catalog ───────────────────────────────────────────────────

_CATALOG_CACHE: dict[tuple[str, str], tuple[float, dict[str, Any]]] = {}
CATALOG_TTL_S = 600.0
# non-chat models listing TEXT output or carrying a profile we never offer
_NOT_CHAT = re.compile(r"embed|rerank|stable-|stability\.|pegasus|marengo|sonic|"
                       r"voxtral|palmyra-vision|upscale", re.I)


def _short_cfg() -> Config:
    return Config(connect_timeout=5, read_timeout=20, retries={"max_attempts": 2})


def catalog(ctx: WorkspaceContext, *, force: bool = False) -> dict[str, Any]:
    """Text models offered in the workspace Region: inference profiles (system
    defined) plus on-demand foundation models. Offered is not entitled — a model
    can still be refused at invoke; the probe tells."""
    key = (ctx.account_id or "", ctx.region)
    hit = _CATALOG_CACHE.get(key)
    if hit and not force and time.monotonic() - hit[0] < CATALOG_TTL_S:
        return hit[1]
    models: dict[str, dict[str, Any]] = {}
    try:
        client = ctx.client("bedrock", cache_token="model-catalog", config=_short_cfg())
        token: str | None = None
        while True:
            kwargs: dict[str, Any] = {"maxResults": 1000, "typeEquals": "SYSTEM_DEFINED"}
            if token:
                kwargs["nextToken"] = token
            resp = client.list_inference_profiles(**kwargs)
            for p in resp.get("inferenceProfileSummaries") or []:
                pid = p.get("inferenceProfileId") or ""
                if pid and not _NOT_CHAT.search(pid) and p.get("status", "ACTIVE") == "ACTIVE":
                    models[pid] = {"model_id": pid, "provider": model_family(pid),
                                   "name": p.get("inferenceProfileName") or pid,
                                   "kind": "inference_profile",
                                   "tool_use": supports_tools(pid)}
            token = resp.get("nextToken")
            if not token:
                break
        resp = client.list_foundation_models(byOutputModality="TEXT")
        for m in resp.get("modelSummaries") or []:
            mid = m.get("modelId") or ""
            if (not mid or _NOT_CHAT.search(mid)
                    or "ON_DEMAND" not in (m.get("inferenceTypesSupported") or [])
                    or (m.get("modelLifecycle") or {}).get("status") == "LEGACY"):
                continue
            models.setdefault(mid, {"model_id": mid, "provider": model_family(mid),
                                    "name": m.get("modelName") or mid,
                                    "kind": "foundation_model",
                                    "tool_use": supports_tools(mid)})
    except Exception as exc:  # the UI falls back to its static lists
        return {"region": ctx.region, "models": [], "error": _short_error(exc)}
    out = {"region": ctx.region,
           "models": sorted(models.values(), key=lambda m: (m["provider"], m["model_id"]))}
    _CATALOG_CACHE[key] = (time.monotonic(), out)
    return out


# ── probes ──────────────────────────────────────────────────────────────────

_PROBE_TOOL = {"toolSpec": {
    "name": "reply", "description": "Send the reply.",
    "inputSchema": {"json": {"type": "object", "properties": {"message": {"type": "string"}},
                             "required": ["message"]}}}}
PROBE_EVALUATOR_PREFIX = "zz_probe_"


def _short_error(exc: BaseException) -> str:
    error = (getattr(exc, "response", None) or {}).get("Error") or {}
    code, message = error.get("Code"), error.get("Message")
    text = f"{code}: {message}" if code else f"{type(exc).__name__}: {exc}"
    return text[:300]


def _check(name: str, fn: Any) -> dict[str, Any]:
    started = time.monotonic()
    try:
        fn()
        error = None
    except Exception as exc:
        error = _short_error(exc)
    return {"name": name, "ok": error is None, "error": error,
            "latency_ms": int((time.monotonic() - started) * 1000)}


def _evaluator_probe(control: Any, model_id: str) -> None:
    """CreateEvaluator test-calls the judge model; a throwaway zz_probe_ evaluator
    is created and deleted at once."""
    created = control.create_evaluator(
        evaluatorName=f"{PROBE_EVALUATOR_PREFIX}{uuid.uuid4().hex[:10]}",
        description="Launchpad model probe (deleted immediately)",
        level="TRACE",
        evaluatorConfig={"llmAsAJudge": {
            "instructions": "Rate the response {assistant_turn} given {context}.",
            "ratingScale": {"numerical": [
                {"value": 0.0, "label": "Bad", "definition": "Unhelpful."},
                {"value": 1.0, "label": "Good", "definition": "Helpful."}]},
            "modelConfig": {"bedrockEvaluatorModelConfig": {"modelId": model_id}},
        }},
        clientToken=str(uuid.uuid4()),
    )
    evaluator_id = created["evaluatorId"]
    try:
        control.delete_evaluator(evaluatorId=evaluator_id)
    except Exception as exc:
        raise RuntimeError(f"the model was accepted, but deleting probe evaluator "
                           f"{evaluator_id} failed ({_short_error(exc)}) — delete it by hand"
                           ) from exc


def probe(ctx: WorkspaceContext, field: str, model_id: str) -> dict[str, Any]:
    """A real tiny Converse call (plus forced tool choice for the actor, plus the
    AgentCore evaluator acceptance for a judge) in the workspace Region."""
    r = role(field)
    value = validate(r, model_id)
    if not value:
        return {"ok": True, "model_id": "", "region": ctx.region, "checks": []}
    runtime = ctx.client("bedrock-runtime", cache_token="model-probe", config=_short_cfg())
    checks = [_check("converse", lambda: runtime.converse(
        modelId=value, messages=[{"role": "user", "content": [{"text": "Reply with OK."}]}],
        inferenceConfig={"maxTokens": 16}))]
    if r.needs_tools and r.probe != "actor":
        checks.append(_check("tool_use", lambda: runtime.converse(
            modelId=value,
            messages=[{"role": "user", "content": [{"text": "Say hi through the tool."}]}],
            inferenceConfig={"maxTokens": 256}, toolConfig={"tools": [_PROBE_TOOL]})))
    if r.probe == "actor":
        checks.append(_check("tool_choice", lambda: runtime.converse(
            modelId=value,
            messages=[{"role": "user", "content": [{"text": "Say hi through the tool."}]}],
            inferenceConfig={"maxTokens": 256},
            toolConfig={"tools": [_PROBE_TOOL], "toolChoice": {"any": {}}})))
    if r.probe == "judge":
        from app.services.agentcore.client import control_client

        control = control_client(ctx)
        checks.append(_check("evaluator", lambda: _evaluator_probe(control, value)))
    return {"ok": all(c["ok"] for c in checks), "model_id": value, "region": ctx.region,
            "checks": checks}
