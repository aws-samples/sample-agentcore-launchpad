"""Spend attribution in dollars (roadmap T28).

The Observability console already estimates cost per model and per session
(`observability.estimate_cost`, priced from the `model_prices` map). What it could not
answer is the question an administrator actually asks: **who spent it** — which agent,
which person, how much of this month's budget is left.

Two dimensions, each derived from data the platform already has rather than a new
telemetry path:

* **by agent** — spans carry the runtime's service name, so one Logs Insights query
  grouped by service gives per-agent tokens, which the shared price map turns into
  dollars. A service name that matches no ledger agent is still reported (an imported or
  deleted agent spent real money).
* **by person** — spans do not carry a console identity, so attribution goes through the
  ledger: per-session tokens joined to `ChatSession.actor_id`. Sessions the console did
  not open (direct `/v1` traffic, evaluation runs) land under a single `—` bucket rather
  than being silently dropped, because an unattributed total that looks like zero is
  worse than one that says "unattributed".

* **by project** (design §10.8) — each agent row's project comes from, in order: the
  `launchpad.project` span resource attribute an isolated project's agents carry; the
  project's application inference profile when the span's model id is one (its
  `project_resources` row); the ledger agent's `project_id`. Cost Explorer's view of
  the `launchpad:project` tag (runtimes, harnesses, inference profiles) is the
  independent check of the same split.

Every figure is an **estimate** and says so: prices are a config map matched by
substring, a model missing from it contributes tokens but no dollars, and the response
names those models so the number is never quietly wrong.
"""

import re
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.project_scope import project_context
from app.models.ledger import Agent, ChatSession
from app.models.projects import PROJECT_ID_PATTERN
from app.services import inference_profiles
from app.services.observability import (
    _IS_LLM_FIELDS,
    SPAN_PROJECT_ATTR,
    SPANS_SOURCE,
    cached,
    estimate_cost,
    logs_client,
    run_insights_queries,
)
from app.services.workspace import WorkspaceContext

# The same whitelist the observability console uses, plus the month-to-date window the
# budget is expressed against.
RANGES: dict[str, int] = {"1h": 1, "6h": 6, "24h": 24, "7d": 168, "30d": 720}
DEFAULT_RANGE = "24h"
# calendar periods, like the AWS billing console: in UTC (the day AWS bills by), a
# week starting on Monday; `custom` takes `start` / `end` dates (inclusive)
PERIODS = ("today", "this_week", "last_week", "this_month", "last_month", "custom")
CUSTOM_MAX_DAYS = 92


def resolve_window(range_key: str, *, start: str | None = None, end: str | None = None,
                   now: datetime | None = None) -> tuple[datetime, datetime]:
    """(start, end) UTC datetimes for a trailing range or a calendar period. A current
    period (today, this week, this month) ends now; `custom` covers whole days."""
    from datetime import date, timedelta

    from app.core.errors import AppError

    now = now or datetime.now(UTC)
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if range_key in RANGES:
        return now - timedelta(hours=RANGES[range_key]), now
    if range_key == "today":
        return day, now
    if range_key == "this_week":
        return day - timedelta(days=day.weekday()), now
    if range_key == "last_week":
        this_week = day - timedelta(days=day.weekday())
        return this_week - timedelta(days=7), this_week
    month = day.replace(day=1)
    if range_key == "this_month":
        return month, now
    if range_key == "last_month":
        previous = (month - timedelta(days=1)).replace(day=1)
        return previous, month
    if range_key == "custom":
        try:
            first = date.fromisoformat(str(start))
            last = date.fromisoformat(str(end))
        except ValueError:
            raise AppError("costs.bad_period", "custom needs start and end dates "
                           "(YYYY-MM-DD)", status_code=422) from None
        days = (last - first).days + 1
        if days < 1 or days > CUSTOM_MAX_DAYS or first > now.date():
            raise AppError("costs.bad_period",
                           f"a custom period is 1–{CUSTOM_MAX_DAYS} days, not in the future",
                           {"max_days": CUSTOM_MAX_DAYS}, status_code=422)
        begin = datetime(first.year, first.month, first.day, tzinfo=UTC)
        return begin, min(begin + timedelta(days=days), now)
    raise AppError("costs.bad_range",
                   f"range must be one of {', '.join((*RANGES, *PERIODS))}", status_code=422)
_PROJECT_RE = re.compile(PROJECT_ID_PATTERN)


def q_cost_by_service() -> str:
    """Tokens per runtime service name and span project — the per-agent dimension
    (`span_project` is "" for the spans that name no project)."""
    return f"""
{SPANS_SOURCE}
| filter ispresent(startTimeUnixNano)
| {_IS_LLM_FIELDS}
| fields coalesce(resource.attributes.{SPAN_PROJECT_ATTR}, "") as span_project
| stats sum(llm_in) as tokens_in, sum(llm_out) as tokens_out,
        sum(llm_cache_read) as cache_read, sum(llm_cache_write) as cache_write,
        sum(is_llm) as llm_calls, latest(telemetry_model) as model,
        count_distinct(attributes.session.id) as sessions
  by resource.attributes.service.name as service, span_project
| sort tokens_out desc
| limit 200
"""


def q_cost_by_model() -> str:
    """Tokens per (runtime service, span project, model): each row is priced at its own
    model's rate, which gives the per-model view and the per-agent dollars (an agent
    that called several models is no longer priced at its latest model's rate)."""
    return f"""
{SPANS_SOURCE}
| filter ispresent(startTimeUnixNano)
| {_IS_LLM_FIELDS}
| filter is_llm > 0
| fields coalesce(resource.attributes.{SPAN_PROJECT_ATTR}, "") as span_project
| stats sum(llm_in) as tokens_in, sum(llm_out) as tokens_out,
        sum(llm_cache_read) as cache_read, sum(llm_cache_write) as cache_write,
        sum(is_llm) as llm_calls
  by resource.attributes.service.name as service, span_project, telemetry_model as model
| sort tokens_out desc
| limit 1000
"""


def q_cost_by_session() -> str:
    """Tokens per session — joined to `ChatSession.actor_id` for the per-person view."""
    return f"""
{SPANS_SOURCE}
| filter ispresent(startTimeUnixNano) and ispresent(attributes.session.id)
| {_IS_LLM_FIELDS}
| stats sum(llm_in) as tokens_in, sum(llm_out) as tokens_out,
        sum(llm_cache_read) as cache_read, sum(llm_cache_write) as cache_write,
        latest(telemetry_model) as model,
        latest(resource.attributes.{SPAN_PROJECT_ATTR}) as span_project
  by attributes.session.id as session_id
| limit 1000
"""


def _num(row: dict[str, Any], key: str) -> float:
    try:
        return float(row.get(key) or 0)
    except (TypeError, ValueError):
        return 0.0


def _row_project(row: dict[str, Any], agent: Agent | None) -> tuple[str | None, str | None]:
    """(project id, how it was known) for one aggregate row: the span attribute, else
    the project's inference profile the model id names, else the ledger agent."""
    span = row.get("span_project")
    if isinstance(span, str) and _PROJECT_RE.fullmatch(span):
        return span, "span"
    profile = inference_profiles.profile_for_model(row.get("model"))
    if profile is not None:
        return profile.project_id, "inference_profile"
    if agent is not None and agent.project_id:
        return agent.project_id, "agent"
    return None, None


def _priced(row: dict[str, Any], prices: dict[str, Any]) -> tuple[float, float | None]:
    """(tokens, dollars-or-None) for one aggregate row."""
    tokens_in = _num(row, "tokens_in")
    tokens_out = _num(row, "tokens_out")
    cache_read = _num(row, "cache_read")
    cache_write = _num(row, "cache_write")
    usd = estimate_cost(
        row.get("model"), tokens_in, tokens_out, cache_read, cache_write, prices=prices
    )
    return tokens_in + tokens_out + cache_read + cache_write, usd


def cost_report(
    db: Session,
    workspace: WorkspaceContext,
    *,
    range_key: str = DEFAULT_RANGE,
    force: bool = False,
    project: str | None = None,
    start: str | None = None,
    end: str | None = None,
) -> dict[str, Any]:
    """Estimated spend for the window, broken down by agent, by person and by model.

    The window is a trailing range (`RANGES`) or a calendar period (`PERIODS`, UTC,
    `resolve_window`); the response names it as `period{start, end}`.

    Cached on the same terms as the observability views (Logs Insights is billed per
    scan), keyed by workspace and range. The cached entry is workspace-wide — built
    across projects, every per-agent and per-session row carrying its project — and
    `project` (design §5.9) narrows it after the cache: that project's agents, and
    the people of that project's sessions. The unattributed rows (a service no agent
    claims, the `—` bucket) belong to no project and appear only without one.
    """
    calendar = range_key in PERIODS
    window_start, window_end = resolve_window(range_key, start=start, end=end)
    hours = max(1, round((window_end - window_start).total_seconds() / 3600))
    # a calendar period's cache key is its start day (a current one moves its end, which
    # the cache TTL refreshes); a closed period never changes
    cache_key = (f"costs:{workspace.id}:{range_key}:{window_start.date().isoformat()}"
                 f":{(window_end.date().isoformat()) if range_key == 'custom' else ''}"
                 if calendar else f"costs:{workspace.id}:{range_key}")

    def build() -> dict[str, Any]:
        prices = get_settings().model_prices or {}
        logs = logs_client(workspace)
        results = run_insights_queries(
            {"by_service": q_cost_by_service(), "by_session": q_cost_by_session(),
             "by_model": q_cost_by_model()},
            hours=hours,
            logs=logs,
            **({"window": (int(window_start.timestamp()), int(window_end.timestamp()))}
               if calendar else {}),
        )

        from app.services.observability import build_agent_resolver

        with project_context(None):  # attribution sees every project's rows
            agents_by_name = {
                row.name: row
                for row in db.scalars(
                    select(Agent).where(Agent.workspace_id == workspace.id)
                ).all()
            }
        # service.name carries the endpoint suffix (`<runtime>.DEFAULT`) and the runtime
        # id, not the ledger name — resolve it the way the observability views do, and
        # keep the exact-name match for log sources registered under their own name.
        resolve = build_agent_resolver(db, workspace.id, all_projects=True)
        # one row per (service, project): a runtime's stamped and unstamped spans (a
        # publish before and after its project was isolated) group apart in the query
        merged: dict[tuple[str, str | None], dict[str, Any]] = {}
        for row in results.get("by_service") or []:
            service = row.get("service") or "—"
            tokens, usd = _priced(row, prices)
            agent = agents_by_name.get(service) or resolve(service)
            project_id, source = _row_project(row, agent)
            entry = merged.get((service, project_id))
            if entry is None:
                merged[(service, project_id)] = {
                    "service": service,
                    "agent_id": agent.id if agent else None,
                    "display_name": (agent.spec or {}).get("display_name") if agent else None,
                    "known": agent is not None,
                    "project_id": project_id,
                    "project_source": source,
                    "tokens": int(tokens),
                    "llm_calls": int(_num(row, "llm_calls")),
                    "sessions": int(_num(row, "sessions")),
                    "est_cost_usd": usd,
                    "_unpriced": str(row["model"]) if usd is None and row.get("model") else None,
                }
                continue
            entry["tokens"] += int(tokens)
            entry["llm_calls"] += int(_num(row, "llm_calls"))
            entry["sessions"] += int(_num(row, "sessions"))
            if usd is not None:
                entry["est_cost_usd"] = round((entry["est_cost_usd"] or 0.0) + usd, 6)
            elif row.get("model") and not entry["_unpriced"]:
                entry["_unpriced"] = str(row["model"])
        # per (service, project, model), each priced at its own model's rate
        model_rows: list[dict[str, Any]] = []
        priced_by_key: dict[tuple[str, str | None], list[dict[str, Any]]] = {}
        for row in results.get("by_model") or []:
            service = row.get("service") or "—"
            tokens, usd = _priced(row, prices)
            if not tokens:
                continue
            agent = agents_by_name.get(service) or resolve(service)
            project_id, _ = _row_project(row, agent)
            model = str(row.get("model") or "") or "unknown"
            item = {
                "model": model, "service": service, "project_id": project_id,
                "tokens_in": int(_num(row, "tokens_in")),
                "tokens_out": int(_num(row, "tokens_out")),
                "cache_tokens": int(_num(row, "cache_read") + _num(row, "cache_write")),
                "tokens": int(tokens), "llm_calls": int(_num(row, "llm_calls")), "usd": usd,
                "_unpriced": model if usd is None and model != "unknown" else None,
            }
            model_rows.append(item)
            priced_by_key.setdefault((service, project_id), []).append(item)
        # an agent's dollars are the sum of its per-model rows when the query has them
        for key, entry in merged.items():
            rows = priced_by_key.get(key)
            if not rows:
                continue
            priced = [r["usd"] for r in rows if r["usd"] is not None]
            entry["est_cost_usd"] = round(sum(priced), 6) if priced else None
            entry["_unpriced"] = next((r["_unpriced"] for r in rows if r["_unpriced"]), None)
        by_agent = list(merged.values())

        # session → (actor, project), for the rows the console opened
        session_rows = results.get("by_session") or []
        ids = [str(row.get("session_id")) for row in session_rows if row.get("session_id")]
        chats: dict[str, tuple[str, str | None]] = {}
        if ids:
            with project_context(None):
                for chat in db.scalars(
                    select(ChatSession).where(
                        ChatSession.workspace_id == workspace.id,
                        ChatSession.session_id.in_(ids),
                    )
                ).all():
                    chats[chat.session_id] = (chat.actor_id, chat.project_id)
        sessions: list[dict[str, Any]] = []
        for row in session_rows:
            tokens, usd = _priced(row, prices)
            # An unmatched session is real traffic with no console identity (/v1, an
            # evaluation replay). It gets its own bucket rather than vanishing.
            actor, pid = chats.get(str(row.get("session_id")), ("—", None))
            if pid is None:  # no console identity, but the spans may name the project
                pid, _ = _row_project(row, None)
            sessions.append({
                "actor": actor, "project_id": pid, "tokens": int(tokens), "usd": usd,
                "_unpriced": str(row["model"]) if usd is None and row.get("model") else None,
            })
        return {
            "range": range_key,
            "hours": hours,
            "period": {"start": window_start.isoformat(), "end": window_end.isoformat()},
            "generated_at": datetime.now(UTC).isoformat(),
            "by_agent": by_agent,
            "sessions": sessions,
            "model_rows": model_rows,
        }

    return _report(cached(cache_key, force, build), project)


def _report(raw: dict[str, Any], project: str | None) -> dict[str, Any]:
    """The response for one project (None = the whole workspace) from the cached rows."""
    agents = [
        row for row in raw["by_agent"] if project is None or row["project_id"] == project
    ]
    sessions = [
        row for row in raw["sessions"] if project is None or row["project_id"] == project
    ]
    # a cache entry built before the per-model query has no `model_rows`
    model_rows = [
        row for row in raw.get("model_rows") or []
        if project is None or row["project_id"] == project
    ]
    unpriced = {row["_unpriced"] for row in (*agents, *sessions, *model_rows)
                if row["_unpriced"]}
    per_model: dict[str, dict[str, Any]] = {}
    for row in model_rows:
        bucket = per_model.setdefault(row["model"], {
            "model": row["model"], "agents": set(), "llm_calls": 0, "tokens_in": 0,
            "tokens_out": 0, "cache_tokens": 0, "tokens": 0, "est_cost_usd": None,
        })
        bucket["agents"].add(row["service"])
        for k in ("llm_calls", "tokens_in", "tokens_out", "cache_tokens", "tokens"):
            bucket[k] += row[k]
        if row["usd"] is not None:
            bucket["est_cost_usd"] = round((bucket["est_cost_usd"] or 0.0) + row["usd"], 6)
    by_model = [{**b, "agents": len(b["agents"])} for b in per_model.values()]
    per_actor: dict[str, dict[str, Any]] = {}
    for row in sessions:
        bucket = per_actor.setdefault(
            row["actor"], {"actor": row["actor"], "sessions": 0, "tokens": 0, "est_cost_usd": 0.0}
        )
        bucket["sessions"] += 1
        bucket["tokens"] += row["tokens"]
        if row["usd"] is not None:
            bucket["est_cost_usd"] = round(bucket["est_cost_usd"] + row["usd"], 6)
    by_agent = [{k: v for k, v in row.items() if k != "_unpriced"} for row in agents]
    per_project: dict[str | None, dict[str, Any]] = {}
    for row in by_agent:
        bucket = per_project.setdefault(row["project_id"], {
            "project_id": row["project_id"], "agents": 0, "tokens": 0, "est_cost_usd": 0.0,
        })
        bucket["agents"] += 1
        bucket["tokens"] += row["tokens"]
        bucket["est_cost_usd"] = round(bucket["est_cost_usd"] + (row["est_cost_usd"] or 0.0), 6)
    return {
        "range": raw["range"],
        "hours": raw["hours"],
        **({"period": raw["period"]} if "period" in raw else {}),
        "generated_at": raw["generated_at"],
        **({"project": project} if project is not None else {}),
        "total_est_cost_usd": round(sum(row["est_cost_usd"] or 0.0 for row in by_agent), 6),
        "total_tokens": sum(row["tokens"] for row in by_agent),
        "by_agent": sorted(by_agent, key=lambda row: row["est_cost_usd"] or 0.0, reverse=True),
        "by_actor": sorted(
            per_actor.values(), key=lambda row: row["est_cost_usd"], reverse=True
        ),
        # the project dimension (§10.8); `project_id` None = attributed to no project
        "by_project": sorted(
            per_project.values(), key=lambda row: row["est_cost_usd"], reverse=True
        ),
        # per model, each call priced at its own model's rate; `est_cost_usd` None =
        # the price map has no rate for it (also listed in `unpriced_models`)
        "by_model": sorted(
            by_model, key=lambda row: (row["est_cost_usd"] or 0.0, row["tokens"]), reverse=True
        ),
        # Stated, never implied: these models burned tokens the price map cannot value.
        "unpriced_models": sorted(unpriced),
        "estimate": True,
        **({"cache": raw["cache"]} if "cache" in raw else {}),
    }


def month_to_date_usd(
    db: Session, workspace: WorkspaceContext, *, force: bool = False,
    project: str | None = None,
) -> float:
    """Spend since the first of the month, for the budget comparison.

    Logs Insights ranges are relative, so this uses the hours elapsed this month (at
    most 744, a 31-day month) — a budget check at 02:00 on the 1st looks at two hours,
    which is correct, not a bug.

    `project` counts only the spend attributed to that project (span attribute,
    inference profile, else the resolved agent — `_row_project`); the cached entry is
    workspace-wide.
    """
    raw = _month_to_date_raw(db, workspace, force=force)
    if project is None:
        return float(raw["usd"])
    return float((raw.get("by_project") or {}).get(project, 0.0))


def month_to_date_breakdown(
    db: Session, workspace: WorkspaceContext, *, force: bool = False,
) -> dict[str, Any]:
    """The workspace's month-to-date estimate: `usd` (every priced span), `by_project`
    (the part `_row_project` attributes to a project) and `unattributed_usd` (the
    rest — services no ledger agent claims: platform presets, the shared COA / Skill
    Lab runtimes, deleted agents). One cached read shared with `month_to_date_usd`."""
    raw = _month_to_date_raw(db, workspace, force=force)
    by_project = {pid: float(usd) for pid, usd in (raw.get("by_project") or {}).items()}
    total = float(raw["usd"])
    return {"usd": total, "by_project": by_project,
            "unattributed_usd": round(max(0.0, total - sum(by_project.values())), 6),
            "hours": raw.get("hours")}


def _month_to_date_raw(
    db: Session, workspace: WorkspaceContext, *, force: bool = False,
) -> dict[str, Any]:
    now = datetime.now(UTC)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    hours = max(1, min(744, int((now - month_start).total_seconds() // 3600) or 1))

    def build() -> dict[str, Any]:
        from app.services.observability import build_agent_resolver

        prices = get_settings().model_prices or {}
        # per (service, project, model): every call priced at its own model's rate,
        # the same basis as `cost_report`'s totals
        results = run_insights_queries(
            {"by_model": q_cost_by_model()}, hours=hours, logs=logs_client(workspace)
        )
        resolve = build_agent_resolver(db, workspace.id, all_projects=True)
        total = 0.0
        by_project: dict[str, float] = {}
        for row in results.get("by_model") or []:
            _, usd = _priced(row, prices)
            total += usd or 0.0
            project_id, _ = _row_project(row, resolve(row.get("service")))
            if project_id:
                by_project[project_id] = by_project.get(project_id, 0.0) + (usd or 0.0)
        return {
            "usd": round(total, 6),
            "hours": hours,
            "by_project": {pid: round(usd, 6) for pid, usd in by_project.items()},
        }

    return cached(f"costs-mtd:{workspace.id}:{hours}", force, build)


def invocations_by_project(
    db: Session, workspace: WorkspaceContext, *, range_key: str = "30d",
) -> dict[str, int]:
    """Sessions per project over the window, from the (cached) estimate rows — the
    invocation volume the billing view (`billing_costs`, cost-tags design D-4)
    allocates the shared infrastructure row by. Unattributed rows weigh nothing."""
    raw = cost_report(db, workspace, range_key=range_key)
    weights: dict[str, int] = {}
    for row in raw["by_agent"]:
        if row["project_id"]:
            weights[row["project_id"]] = weights.get(row["project_id"], 0) + int(
                row["sessions"] or row["llm_calls"] or 0)
    return weights
