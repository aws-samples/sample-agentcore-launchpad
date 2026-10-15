"""One admin-editable price source: model price overrides and AgentCore fee rates.

**Model prices.** ``Settings.model_prices`` (built-ins < launchpad.yaml < the
24 h refresher) is what the Spend page prices spans with. An administrator can
add a price for any model id — one that has none, or an override of an existing
entry — on the V2 Models page. Overrides live in ``platform_settings`` key
``model_price_overrides`` and are merged **over** the map by a settings hook (the
``model_settings`` #275 pattern), so they win over yaml, the refresher and the
built-ins and survive every ``get_settings.cache_clear()``. The Spend page and
budgets read the one merged map.

The refresher persists the map it refreshed to launchpad.yaml, so it must refresh
the map *without* the overrides (``base_model_prices``) — otherwise a console
override would be written into yaml and outlive its own reset.

**AgentCore rates** (``platform_settings`` key ``agentcore_rates``): the public
per-unit fees (read 2026-10-15, research/agentcore-fees.md §1) plus the
calibration factors the estimates use. Seeded with defaults, editable from day
one; the on-demand Evaluate meter uses the evaluator rates, reconciliation (child
3) the rest.
"""

from __future__ import annotations

import json
import math
import re
import threading
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings, register_settings_hook
from app.core.errors import AppError
from app.models.platform_settings import PlatformSetting
from app.services.observability import CACHE_READ_FACTOR, CACHE_WRITE_FACTOR, match_price

PRICE_OVERRIDES_KEY = "model_price_overrides"
AGENTCORE_RATES_KEY = "agentcore_rates"
PRICE_FIELDS = ("input", "output", "cache_read", "cache_write")  # USD per 1M tokens
# A price-map key is substring-matched against model ids, so any model-id-shaped
# string (a full id, an ARN, or a shorter family key) is a valid key.
PRICE_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{1,199}$")
MAX_PRICE_PER_MTOK = 10_000.0

# ── model price overrides: the snapshot + the get_settings hook ──────────────

_LOCK = threading.RLock()
_overrides: dict[str, dict[str, float]] = {}
# (the Settings object, its model_prices BEFORE any override was merged in)
_base: tuple[Settings | None, dict[str, Any]] = (None, {})


def _apply_snapshot(settings: Settings) -> None:
    """The ``get_settings`` hook: a freshly built Settings gets the overrides."""
    global _base
    with _LOCK:
        _base = (settings, dict(settings.model_prices or {}))
        if _overrides:
            settings.model_prices = {**_base[1], **_overrides}


register_settings_hook(_apply_snapshot)


def _base_of(settings: Settings) -> dict[str, Any]:
    """``settings``' price map before any override was merged into it."""
    global _base
    owner, values = _base
    if owner is settings:
        return values
    if owner is None:
        # cached before this module registered its hook: nothing merged into it yet
        _base = (settings, dict(settings.model_prices or {}))
        return _base[1]
    # A sibling: two threads missed a cleared cache at once, both builds went
    # through the hook (the last one recorded the base) and lru_cache kept the
    # other. It already carries the overrides, so an overridden key's base is the
    # sibling's — built from the same yaml — not its current (overridden) value.
    current = {k: v for k, v in (settings.model_prices or {}).items() if k not in _overrides}
    for key in _overrides:
        if key in values:
            current[key] = values[key]
    _base = (settings, current)
    return current


def base_model_prices(settings: Settings | None = None) -> dict[str, Any]:
    """The price map without console overrides: what the refresher refreshes and
    persists to launchpad.yaml, and what a reset returns a key to."""
    current = settings or get_settings()
    with _LOCK:
        return dict(_base_of(current))


def _sync(new: dict[str, dict[str, float]]) -> None:
    """Replace the snapshot and bring the cached Settings in line, in place."""
    global _overrides
    with _LOCK:
        if get_settings.cache_info().currsize == 0:
            # Cold cache: the next build applies the snapshot through the hook.
            # Building one here would bake in whatever env happens to be set now.
            _overrides = dict(new)
            return
        settings = get_settings()
        base = _base_of(settings)
        _overrides = dict(new)
        settings.model_prices = {**base, **_overrides}


def _load_json(db: Session, key: str) -> tuple[Any, PlatformSetting | None]:
    row = db.get(PlatformSetting, key)
    if row is None or not row.value:
        return None, row
    try:
        return json.loads(row.value), row
    except ValueError:
        return None, row


def _write_json(db: Session, key: str, value: Any, actor: str) -> PlatformSetting:
    row = db.get(PlatformSetting, key)
    if row is None:
        row = PlatformSetting(key=key)
        db.add(row)
    row.value = json.dumps(value, sort_keys=True)
    row.updated_by = (actor or "")[:64]
    row.updated_at = datetime.now(UTC)
    db.commit()
    return row


def _stored_overrides(db: Session) -> dict[str, Any]:
    value, _ = _load_json(db, PRICE_OVERRIDES_KEY)
    if not isinstance(value, dict):
        return {"prices": {}, "meta": {}}
    prices = value.get("prices") if isinstance(value.get("prices"), dict) else {}
    meta = value.get("meta") if isinstance(value.get("meta"), dict) else {}
    return {"prices": prices, "meta": meta}


def load_overrides(db: Session) -> None:
    """(Re)load the snapshot from the ledger — at startup, after ``init_db``."""
    stored = _stored_overrides(db)
    _sync({
        key: {f: float(entry[f]) for f in PRICE_FIELDS if f in entry}
        for key, entry in stored["prices"].items()
        if isinstance(entry, dict)
    })


def reset_overrides() -> None:
    """Drop the in-process snapshot (tests; the ledger is not touched)."""
    _sync({})


def overrides() -> dict[str, dict[str, float]]:
    with _LOCK:
        return {k: dict(v) for k, v in _overrides.items()}


def _number(value: Any, field: str, *, maximum: float = MAX_PRICE_PER_MTOK) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise AppError("rates.invalid_value", f"{field} must be a number",
                       {"field": field}) from None
    if not math.isfinite(number) or number < 0 or number > maximum:
        raise AppError("rates.invalid_value",
                       f"{field} must be a finite number between 0 and {maximum:g}",
                       {"field": field})
    return number


def validate_price_key(model_id: str) -> str:
    value = (model_id or "").strip()
    if not PRICE_KEY_RE.match(value):
        raise AppError("rates.invalid_model_id", f"{value!r} is not a model id",
                       {"model_id": value})
    return value


def set_price_override(
    db: Session, model_id: str, rates: dict[str, Any], actor: str
) -> dict[str, Any]:
    key = validate_price_key(model_id)
    entry: dict[str, float] = {}
    for field in PRICE_FIELDS:
        if rates.get(field) is not None:
            entry[field] = _number(rates[field], field)
    if "input" not in entry or "output" not in entry:
        raise AppError("rates.price_incomplete",
                       "a price needs at least an input and an output rate")
    stored = _stored_overrides(db)
    stored["prices"][key] = entry
    stored["meta"][key] = {"updated_by": (actor or "")[:64],
                           "updated_at": datetime.now(UTC).isoformat(timespec="seconds")}
    _write_json(db, PRICE_OVERRIDES_KEY, stored, actor)
    load_overrides(db)
    return price_entry(db, key)


def clear_price_override(db: Session, model_id: str, actor: str) -> dict[str, Any]:
    key = (model_id or "").strip()
    stored = _stored_overrides(db)
    if key not in stored["prices"]:
        raise AppError("rates.override_not_found", f"no price override for {key!r}",
                       {"model_id": key}, status_code=404)
    stored["prices"].pop(key, None)
    stored["meta"].pop(key, None)
    _write_json(db, PRICE_OVERRIDES_KEY, stored, actor)
    load_overrides(db)
    return price_entry(db, key)


def _rates_of(entry: dict[str, Any] | None) -> dict[str, float | None] | None:
    if not isinstance(entry, dict):
        return None
    out: dict[str, float | None] = {}
    for field in PRICE_FIELDS:
        try:
            out[field] = float(entry[field]) if entry.get(field) is not None else None
        except (TypeError, ValueError):
            out[field] = None
    return out


def price_entry(db: Session, key: str) -> dict[str, Any]:
    """One price-map key: its effective rates, the base (pre-override) rates and
    who overrode it."""
    stored = _stored_overrides(db)
    effective = get_settings().model_prices.get(key)
    base = base_model_prices().get(key)
    meta = stored["meta"].get(key) or {}
    return {
        "model_id": key,
        "rates": _rates_of(effective),
        "base": _rates_of(base),
        "overridden": key in stored["prices"],
        "updated_by": meta.get("updated_by"),
        "updated_at": meta.get("updated_at"),
    }


def price_table(db: Session) -> list[dict[str, Any]]:
    """Every key of the merged price map, overrides first, then by key."""
    stored = _stored_overrides(db)
    keys = set(get_settings().model_prices) | set(stored["prices"])
    entries = [price_entry(db, key) for key in keys]
    entries.sort(key=lambda e: (not e["overridden"], e["model_id"]))
    return entries


# ── pricing ─────────────────────────────────────────────────────────────────


def price_of(model_id: str | None) -> dict[str, float] | None:
    """The price entry the merged map gives this model id (longest substring key)."""
    return match_price(model_id, get_settings().model_prices)


def is_unpriced(model_id: str | None) -> bool:
    """True when the model id has no price in the merged map (nor an override).
    An absent model id is not "unpriced" here — the caller decides what an
    unknown model means."""
    return bool(model_id) and price_of(model_id) is None


def price_tokens(
    model_id: str | None,
    tokens_in: float = 0,
    tokens_out: float = 0,
    cache_read: float = 0,
    cache_write: float = 0,
) -> tuple[float | None, bool]:
    """``(usd, priced)`` for one usage at the merged map's rate. Same semantics as
    ``observability.estimate_cost``: cache rates default to input × 0.1 (read)
    and × 1.25 (write). An unpriced model is ``(None, False)``, never $0."""
    entry = price_of(model_id)
    if not entry:
        return None, False
    try:
        rate_in = float(entry.get("input", 0.0))
        rate_out = float(entry.get("output", 0.0))
        rate_read = float(entry.get("cache_read", rate_in * CACHE_READ_FACTOR))
        rate_write = float(entry.get("cache_write", rate_in * CACHE_WRITE_FACTOR))
    except (TypeError, ValueError):
        return None, False
    usd = (
        tokens_in * rate_in + tokens_out * rate_out
        + cache_read * rate_read + cache_write * rate_write
    ) / 1e6
    return round(usd, 8), True


# ── AgentCore fee rates + calibration ───────────────────────────────────────

# Public rates read 2026-10-15 (research/agentcore-fees.md §1; us-east-1 = us-west-2).
DEFAULT_AGENTCORE_RATES: dict[str, float] = {
    # Runtime microVM, per vCPU-hour / GB-hour, by platform version
    "runtime_v1_vcpu_hour": 0.0895,
    "runtime_v1_gb_hour": 0.00945,
    "runtime_v2_vcpu_hour": 0.1276,
    "runtime_v2_gb_hour": 0.0169,
    # Memory: short-term event ingestion at the 12 KB per-event minimum and
    # $1.00/GB (from 2026-10-06); long-term retrieve request $0.50 / 1K
    "memory_event": 0.000012,
    "memory_retrieval": 0.0005,
    # Gateway: $0.005 / 1K invocations, $0.025 / 1K search calls; Policy authorization
    "gateway_call": 0.000005,
    "gateway_search": 0.000025,
    "policy_authorization": 0.000025,
    # Web Search on AgentCore $7 / 1K queries; Bedrock KB retrieval / agentic retrieval
    "web_search_query": 0.007,
    "kb_query": 0.001,
    "kb_agentic_query": 0.004,
    # Evaluations: built-in per 1K tokens (on-demand / online = Tier1, batch = Tier2),
    # custom per evaluation ($1.50 / 1K; the judge's own model tokens billed apart)
    "evaluation_builtin_input_per_1k": 0.0024,
    "evaluation_builtin_output_per_1k": 0.012,
    "evaluation_batch_input_per_1k": 0.0018,
    "evaluation_batch_output_per_1k": 0.009,
    "evaluation_custom": 0.0015,
    # Code Interpreter / Browser, per vCPU-hour / GB-hour
    "code_interpreter_vcpu_hour": 0.0895,
    "code_interpreter_gb_hour": 0.00945,
    "browser_vcpu_hour": 0.0895,
    "browser_gb_hour": 0.00945,
}
RATE_UNITS: dict[str, str] = {
    "runtime_v1_vcpu_hour": "vCPU-hour", "runtime_v1_gb_hour": "GB-hour",
    "runtime_v2_vcpu_hour": "vCPU-hour", "runtime_v2_gb_hour": "GB-hour",
    "memory_event": "event", "memory_retrieval": "request",
    "gateway_call": "call", "gateway_search": "call", "policy_authorization": "request",
    "web_search_query": "query", "kb_query": "query", "kb_agentic_query": "query",
    "evaluation_builtin_input_per_1k": "1K input tokens",
    "evaluation_builtin_output_per_1k": "1K output tokens",
    "evaluation_batch_input_per_1k": "1K input tokens",
    "evaluation_batch_output_per_1k": "1K output tokens",
    "evaluation_custom": "evaluation",
    "code_interpreter_vcpu_hour": "vCPU-hour", "code_interpreter_gb_hour": "GB-hour",
    "browser_vcpu_hour": "vCPU-hour", "browser_gb_hour": "GB-hour",
}
# Seeds for the estimates (research/agentcore-fees.md §3.1 measured averages): a
# Harness session holds ~8 GB through the 900 s idle tail, a Strands/zip runtime
# ~1.5 GB; CPU is 1-10 % of Runtime cost. The built-in evaluator token seeds are
# uncalibrated placeholders until reconciliation recalibrates them.
DEFAULT_CALIBRATION: dict[str, float] = {
    "runtime_idle_timeout_seconds": 900,
    "runtime_harness_gb": 8.0,
    "runtime_default_gb": 1.5,
    "runtime_harness_vcpu": 0.08,
    "runtime_default_vcpu": 0.02,
    "builtin_eval_input_tokens": 10000,
    "builtin_eval_output_tokens": 250,
    # One agent-started Code Interpreter session (dev, 30 d: 3.74 GB-h and 0.048
    # vCPU-h over 10 sessions; the tool session runs to its own timeout)
    "code_interpreter_gb_hours_per_session": 0.37,
    "code_interpreter_vcpu_hours_per_session": 0.005,
    # A runtime's measured factors (hourly calibration, services/budget_calibration.py)
    # replace the seeds above once this many sessions back them
    "calibration_min_sessions": 3,
}
CALIBRATION_UNITS: dict[str, str] = {
    "runtime_idle_timeout_seconds": "seconds",
    "runtime_harness_gb": "GB resident", "runtime_default_gb": "GB resident",
    "runtime_harness_vcpu": "vCPU average", "runtime_default_vcpu": "vCPU average",
    "builtin_eval_input_tokens": "tokens per evaluation",
    "builtin_eval_output_tokens": "tokens per evaluation",
    "code_interpreter_gb_hours_per_session": "GB-hours per session",
    "code_interpreter_vcpu_hours_per_session": "vCPU-hours per session",
    "calibration_min_sessions": "sessions",
}
# Per-runtime measured factors live beside the flat calibration keys, under
# ``agentcore_rates.calibration.runtimes`` — written by the hourly calibration only,
# never by the admin PUT (a "runtimes" key there is refused as unknown), and kept by
# it (``set_agentcore_rates`` merges into the stored calibration dict).
RUNTIME_FACTORS_KEY = "runtimes"
_MAX_RATE = 1_000_000.0


def _stored_agentcore(db: Session) -> tuple[dict[str, float], dict[str, float],
                                            PlatformSetting | None]:
    value, row = _load_json(db, AGENTCORE_RATES_KEY)
    value = value if isinstance(value, dict) else {}
    rates = value.get("rates") if isinstance(value.get("rates"), dict) else {}
    calibration = (value.get("calibration")
                   if isinstance(value.get("calibration"), dict) else {})
    return rates, calibration, row


def agentcore_rates(db: Session) -> dict[str, float]:
    """The effective fee rates: stored values over the seeded defaults."""
    stored, _, _ = _stored_agentcore(db)
    out = dict(DEFAULT_AGENTCORE_RATES)
    for key, value in stored.items():
        if key in out:
            try:
                out[key] = float(value)
            except (TypeError, ValueError):
                continue
    return out


def calibration(db: Session) -> dict[str, float]:
    _, stored, _ = _stored_agentcore(db)
    out = dict(DEFAULT_CALIBRATION)
    for key, value in stored.items():
        if key in out:
            try:
                out[key] = float(value)
            except (TypeError, ValueError):
                continue
    return out


def runtime_factors(db: Session) -> dict[str, dict[str, Any]]:
    """Measured Runtime factors per runtime id (``{gb, vcpu, sessions, hours,
    session_hours, last_hour, updated_at, runtime_name}``), as the hourly calibration
    stored them. Malformed entries are skipped."""
    _, stored, _ = _stored_agentcore(db)
    raw = stored.get(RUNTIME_FACTORS_KEY)
    if not isinstance(raw, dict):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for runtime_id, entry in raw.items():
        if not isinstance(entry, dict):
            continue
        try:
            gb, vcpu = float(entry["gb"]), float(entry["vcpu"])
            sessions = int(entry.get("sessions") or 0)
        except (KeyError, TypeError, ValueError):
            continue
        if not (math.isfinite(gb) and math.isfinite(vcpu)):
            continue
        out[str(runtime_id)] = {**entry, "gb": gb, "vcpu": vcpu, "sessions": sessions}
    return out


def store_runtime_factors(db: Session, factors: dict[str, dict[str, Any]]) -> None:
    """Replace the measured per-runtime factors, leaving every other stored rate and
    calibration key as it is (a re-read just before the write, so an admin edit made
    since the caller last read the row survives). Commits."""
    value, row = _load_json(db, AGENTCORE_RATES_KEY)
    value = value if isinstance(value, dict) else {}
    stored_rates = value.get("rates") if isinstance(value.get("rates"), dict) else {}
    stored_cal = (dict(value["calibration"]) if isinstance(value.get("calibration"), dict)
                  else {})
    stored_cal[RUNTIME_FACTORS_KEY] = factors
    if row is None:
        row = PlatformSetting(key=AGENTCORE_RATES_KEY)
        db.add(row)
    # updated_by / updated_at name the last *admin* edit on the Models page; the
    # calibration's own timestamp is on each runtime entry
    row.value = json.dumps({**value, "rates": stored_rates, "calibration": stored_cal},
                           sort_keys=True)
    db.commit()


def agentcore_rates_view(db: Session) -> dict[str, Any]:
    _, _, row = _stored_agentcore(db)
    return {
        "rates": agentcore_rates(db),
        "defaults": dict(DEFAULT_AGENTCORE_RATES),
        "units": dict(RATE_UNITS),
        "calibration": calibration(db),
        "calibration_defaults": dict(DEFAULT_CALIBRATION),
        "calibration_units": dict(CALIBRATION_UNITS),
        "updated_by": row.updated_by if row is not None else None,
        "updated_at": row.updated_at.isoformat() if row is not None else None,
    }


def set_agentcore_rates(
    db: Session,
    *,
    rates: dict[str, Any] | None,
    calibration_values: dict[str, Any] | None,
    actor: str,
) -> dict[str, Any]:
    """Merge the given keys over what is stored. A ``null`` value resets that key
    to its default; an unknown key is refused (a typo would be silently unused)."""
    stored_rates, stored_cal, _ = _stored_agentcore(db)
    for given, known, target in (
        (rates or {}, DEFAULT_AGENTCORE_RATES, stored_rates),
        (calibration_values or {}, DEFAULT_CALIBRATION, stored_cal),
    ):
        for key, value in given.items():
            if key not in known:
                raise AppError("rates.unknown_key", f"unknown rate {key!r}", {"key": key})
            if value is None:
                target.pop(key, None)
            else:
                target[key] = _number(value, key, maximum=_MAX_RATE)
    _write_json(db, AGENTCORE_RATES_KEY,
                {"rates": stored_rates, "calibration": stored_cal}, actor)
    return agentcore_rates_view(db)
