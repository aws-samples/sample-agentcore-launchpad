"""Spend attribution in dollars: per agent, per person, month to date.

The Observability console already estimates cost per model and per session
(`observability.estimate_cost`, priced from the `model_prices` map). What it could not
answer is the question an administrator actually asks: **who spent it** — which agent,
which person, how much this month.

Two dimensions, each derived from data the platform already has rather than a new
telemetry path:

* **by agent** — spans carry the runtime's service name, so one Logs Insights query
  grouped by service gives per-agent tokens, which the shared price map turns into
  dollars. A service name that matches no ledger agent is still reported (`known:
  false`) and counted in the total: an imported or deleted agent — or, on an AWS
  account shared by two consoles, the other console's agent — spent real money.
* **by person** — spans do not carry a console identity, so attribution goes through the
  ledger: per-session tokens joined to `ChatSession.actor_id` (the bare username).
  Sessions the console did not open (direct `/v1` traffic, evaluation runs) land under a
  single `—` bucket rather than being silently dropped.

Every figure is an **estimate** and says so: prices are a config map matched by
substring, a model missing from it contributes tokens but no dollars, and the response
names those models so the number is never quietly wrong. A row is priced at the last
model its spans named (`latest(telemetry_model)`). A row with only Strands wrapper tokens
(a non-Bedrock provider such as Mantle emits no provider span) is priced on those.
"""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.ledger import Agent, ChatSession
from app.services.observability import (
    _INPUT_TOKENS_FIELD,
    _IS_LLM_FIELDS,
    _NATIVE_AGENT_FIELD,
    _OPERATION_FIELD,
    _OUTPUT_TOKENS_FIELD,
    SPANS_SOURCE,
    build_agent_resolver,
    cached,
    estimate_cost,
    logs_client,
    run_insights_queries,
)
from app.services.workspace import WorkspaceContext

# Its own range map: the observability views have no 30-day range, spend does.
RANGES: dict[str, int] = {"1h": 1, "6h": 6, "24h": 24, "7d": 168, "30d": 720}
DEFAULT_RANGE = "24h"
# Logs Insights ranges are relative; month-to-date is capped at the 30-day range.
MAX_MONTH_HOURS = 720


# `_IS_LLM_FIELDS` drops the Strands wrapper span (it duplicates the provider span), but
# a non-Bedrock provider (Mantle `openai.*`) emits the wrapper span ONLY. The wrapper sums
# ride alongside so a row whose provider sums are zero falls back to them — the same rule
# as the dashboard's tokens-by-model (`observability._tokens_by_model_rows`).
_WRAPPER_FIELDS = f"""fields ((strcontains({_OPERATION_FIELD}, "chat")
        + strcontains({_OPERATION_FIELD}, "text_completion")
        + strcontains({_OPERATION_FIELD}, "generate_content"))
        * strcontains(coalesce(attributes.gen_ai.system, ""), "strands-agents")
        * (1 - {_NATIVE_AGENT_FIELD})) as is_wrap
| fields {_INPUT_TOKENS_FIELD} * is_wrap as wrap_in,
         {_OUTPUT_TOKENS_FIELD} * is_wrap as wrap_out"""

# The tokens a row is priced on: provider sums, else the wrapper-only fallback.
_TOKEN_SUMS = """sum(llm_in) as tokens_in, sum(llm_out) as tokens_out,
        sum(llm_cache_read) as cache_read, sum(llm_cache_write) as cache_write,
        sum(wrap_in) as wrapper_in, sum(wrap_out) as wrapper_out"""


def q_cost_by_service() -> str:
    """Tokens per runtime service name — the per-agent dimension."""
    return f"""
{SPANS_SOURCE}
| filter ispresent(startTimeUnixNano)
| {_IS_LLM_FIELDS}
| {_WRAPPER_FIELDS}
| stats {_TOKEN_SUMS},
        sum(is_llm) as llm_calls, sum(is_wrap) as wrapper_calls,
        latest(telemetry_model) as model,
        count_distinct(attributes.session.id) as sessions
  by resource.attributes.service.name as service
| sort tokens_out desc
| limit 200
"""


def q_cost_by_session() -> str:
    """Tokens per session — joined to `ChatSession.actor_id` for the per-person view.

    Sorted before the cap so that, past 1000 sessions, the ones dropped are the smallest
    by provider output tokens (not an arbitrary 1000).
    """
    return f"""
{SPANS_SOURCE}
| filter ispresent(startTimeUnixNano) and ispresent(attributes.session.id)
| {_IS_LLM_FIELDS}
| {_WRAPPER_FIELDS}
| stats {_TOKEN_SUMS},
        latest(telemetry_model) as model
  by attributes.session.id as session_id
| sort tokens_out desc
| limit 1000
"""


def _num(row: dict[str, Any], key: str) -> float:
    try:
        return float(row.get(key) or 0)
    except (TypeError, ValueError):
        return 0.0


# What `unpriced_models` names when tokens were burned under no model id at all.
UNKNOWN_MODEL = "(unknown model)"


def _wrapper_only(row: dict[str, Any]) -> bool:
    """No provider-span tokens, only Strands wrapper ones (a Mantle provider)."""
    return not (_num(row, "tokens_in") or _num(row, "tokens_out")) and bool(
        _num(row, "wrapper_in") or _num(row, "wrapper_out")
    )


def _priced(row: dict[str, Any], prices: dict[str, Any]) -> tuple[float, float | None]:
    """(tokens, dollars-or-None) for one aggregate row."""
    if _wrapper_only(row):
        tokens_in, tokens_out = _num(row, "wrapper_in"), _num(row, "wrapper_out")
        cache_read = cache_write = 0.0
    else:
        tokens_in = _num(row, "tokens_in")
        tokens_out = _num(row, "tokens_out")
        cache_read = _num(row, "cache_read")
        cache_write = _num(row, "cache_write")
    usd = estimate_cost(
        row.get("model"), tokens_in, tokens_out, cache_read, cache_write, prices=prices
    )
    return tokens_in + tokens_out + cache_read + cache_write, usd


def _note_unpriced(
    unpriced: set[str], row: dict[str, Any], tokens: float, usd: float | None
) -> None:
    """Name the model of a row that burned tokens no price covers — a row with no
    model id at all included, so its tokens never count as silently free."""
    if usd is None and (tokens or row.get("model")):
        unpriced.add(str(row.get("model") or UNKNOWN_MODEL))


def _calls(row: dict[str, Any]) -> int:
    return int(_num(row, "wrapper_calls" if _wrapper_only(row) else "llm_calls"))


def cost_report(
    db: Session,
    workspace: WorkspaceContext,
    *,
    range_key: str = DEFAULT_RANGE,
    force: bool = False,
) -> dict[str, Any]:
    """Estimated spend for the window, broken down by agent and by person.

    Cached on the same terms as the observability views (Logs Insights is billed per
    scan), keyed by workspace and range.
    """
    hours = RANGES.get(range_key, RANGES[DEFAULT_RANGE])

    def build() -> dict[str, Any]:
        prices = get_settings().model_prices or {}
        results = run_insights_queries(
            {"by_service": q_cost_by_service(), "by_session": q_cost_by_session()},
            hours=hours,
            logs=logs_client(workspace),
        )

        agents_by_name = {
            row.name: row
            for row in db.scalars(
                select(Agent).where(Agent.workspace_id == workspace.id)
            ).all()
        }
        # service.name carries the endpoint suffix (`<runtime>.DEFAULT`) and the runtime
        # id, not the ledger name — resolve it the way the observability views do, and
        # keep the exact-name match for log sources registered under their own name.
        resolve = build_agent_resolver(db, workspace.id)
        by_agent: list[dict[str, Any]] = []
        unpriced: set[str] = set()
        for row in results.get("by_service") or []:
            service = row.get("service") or "—"
            tokens, usd = _priced(row, prices)
            _note_unpriced(unpriced, row, tokens, usd)
            agent = agents_by_name.get(service) or resolve(service)
            by_agent.append(
                {
                    "service": service,
                    "agent_id": agent.id if agent else None,
                    "agent_name": agent.name if agent else None,
                    "display_name": (agent.spec or {}).get("display_name") if agent else None,
                    "known": agent is not None,
                    "tokens": int(tokens),
                    "llm_calls": _calls(row),
                    "sessions": int(_num(row, "sessions")),
                    "est_cost_usd": usd,
                }
            )

        # session → actor, for the rows the console opened
        session_rows = results.get("by_session") or []
        ids = [str(row.get("session_id")) for row in session_rows if row.get("session_id")]
        actors: dict[str, str] = {}
        if ids:
            for chat in db.scalars(
                select(ChatSession).where(
                    ChatSession.workspace_id == workspace.id,
                    ChatSession.session_id.in_(ids),
                )
            ).all():
                actors[chat.session_id] = chat.actor_id
        per_actor: dict[str, dict[str, Any]] = {}
        for row in session_rows:
            tokens, usd = _priced(row, prices)
            _note_unpriced(unpriced, row, tokens, usd)
            # An unmatched session is real traffic with no console identity (/v1, an
            # evaluation replay). It gets its own bucket rather than vanishing.
            actor = actors.get(str(row.get("session_id")), "—")
            bucket = per_actor.setdefault(
                actor,
                {
                    "actor": actor,
                    "attributed": actor != "—",
                    "sessions": 0,
                    "tokens": 0,
                    "est_cost_usd": 0.0,
                },
            )
            bucket["sessions"] += 1
            bucket["tokens"] += int(tokens)
            if usd is not None:
                bucket["est_cost_usd"] = round(bucket["est_cost_usd"] + usd, 6)

        total_usd = round(sum(row["est_cost_usd"] or 0.0 for row in by_agent), 6)
        return {
            "range": range_key,
            "hours": hours,
            "generated_at": datetime.now(UTC).isoformat(),
            "total_est_cost_usd": total_usd,
            "total_tokens": sum(row["tokens"] for row in by_agent),
            "by_agent": sorted(
                by_agent, key=lambda row: row["est_cost_usd"] or 0.0, reverse=True
            ),
            "by_actor": sorted(
                per_actor.values(), key=lambda row: row["est_cost_usd"], reverse=True
            ),
            # Stated, never implied: these models burned tokens the price map cannot value.
            "unpriced_models": sorted(unpriced),
            "estimate": True,
        }

    return cached(f"costs:{workspace.id}:{range_key}", force, build)


def month_hours(now: datetime | None = None) -> int:
    """Whole hours since the UTC month start, clamped to 1..MAX_MONTH_HOURS.

    A check at 02:00 on the 1st looks at two hours, which is correct, not a bug; on
    the 31st the oldest hours past the 30-day cap are dropped.
    """
    now = now or datetime.now(UTC)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    elapsed = int((now - month_start).total_seconds() // 3600)
    return max(1, min(MAX_MONTH_HOURS, elapsed))


def month_to_date(
    db: Session, workspace: WorkspaceContext, *, force: bool = False
) -> dict[str, Any]:
    """Spend since the UTC month start: `{"usd", "hours", "unpriced_models"}`."""
    hours = month_hours()

    def build() -> dict[str, Any]:
        prices = get_settings().model_prices or {}
        results = run_insights_queries(
            {"by_service": q_cost_by_service()}, hours=hours, logs=logs_client(workspace)
        )
        total = 0.0
        unpriced: set[str] = set()
        for row in results.get("by_service") or []:
            tokens, usd = _priced(row, prices)
            _note_unpriced(unpriced, row, tokens, usd)
            total += usd or 0.0
        return {"usd": round(total, 6), "hours": hours, "unpriced_models": sorted(unpriced)}

    return cached(f"costs-mtd:{workspace.id}:{hours}", force, build)


def month_to_date_usd(db: Session, workspace: WorkspaceContext, *, force: bool = False) -> float:
    """The month-to-date figure a `cost_mtd_usd` alert compares against."""
    return float(month_to_date(db, workspace, force=force)["usd"])
