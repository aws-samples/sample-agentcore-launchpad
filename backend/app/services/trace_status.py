"""Whether a container agent's traces reach CloudWatch — the console badge of N17-obs465.

Two signals (lead ruling Q3, both):

1. PRIMARY — the hook's own start-up line in the runtime log group
   (``launchpad-adot-cn: attached|native|skipped|unavailable (<reason>)``, printed by the
   platform's aws-cn trace exporter layer each time a runtime instance starts). Read only when
   the layer was applied (``spec.byoc.telemetry.cn_exporter == "applied"``).
2. SECONDARY — the agent had sessions in the window but its newest one left no span record in
   ``aws/spans``: "traces not arriving", even when the hook said ``attached``.

Read-only, best effort: a missing group or a denied read degrades to ``None`` for that signal.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.deployer import adot_cn
from app.models.ledger import Agent, ChatSession
from app.services.observability import RUNTIME_LOG_GROUP_PREFIX, logs_client
from app.services.workspace import WorkspaceContext

SPANS_LOG_GROUP = "aws/spans"
WINDOW = timedelta(days=7)


def _ms(value: datetime) -> int:
    return int(value.timestamp() * 1000)


HOOK_PAGES = 20  # filter_log_events answers oldest first: follow pages to the newest line


def _hook_events(logs: Any, group: str, since: int) -> list[dict[str, Any]] | None:
    """Every hook line in the window. Pages are followed (bounded): the API answers OLDEST
    first, so the first page of a redeployed agent holds the previous image's lines."""
    events: list[dict[str, Any]] = []
    kwargs: dict[str, Any] = {"logGroupName": group,
                              "filterPattern": f'"{adot_cn.HOOK_LOG_PREFIX}"',
                              "startTime": since, "limit": 50}
    try:
        for _ in range(HOOK_PAGES):
            page = logs.filter_log_events(**kwargs)
            events += page.get("events") or []
            token = page.get("nextToken")
            if not token or token == kwargs.get("nextToken"):
                break
            kwargs["nextToken"] = token
    except Exception:  # noqa: BLE001 — best effort
        return events or None
    return events


def trace_status(db: Session, agent: Agent, workspace: WorkspaceContext, *,
                 logs: Any = None, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    byoc = (agent.spec or {}).get("byoc") or {}
    tele = byoc.get("telemetry") or {}
    out: dict[str, Any] = {
        "cn_exporter": tele.get("cn_exporter") or "",
        "member_image": tele.get("member_image") or "",
        "layer_image": tele.get("layer_image") or "",
        "hook": None, "traces": None, "badge": "unknown",
    }
    if not agent.resource_id:
        return out
    logs = logs or logs_client(workspace)
    group = f"{RUNTIME_LOG_GROUP_PREFIX}{agent.resource_id}-DEFAULT"
    since = _ms(now - WINDOW)
    if out["cn_exporter"] == "applied":
        events = _hook_events(logs, group, since)
        if events is not None:
            latest = max(events, key=lambda e: int(e.get("timestamp") or 0), default=None)
            parsed = adot_cn.parse_hook_line(str((latest or {}).get("message") or ""))
            out["hook"] = ({**parsed, "at": datetime.fromtimestamp(
                int(latest["timestamp"]) / 1000, UTC)} if parsed and latest else
                {"state": "not_started", "reason": "no runtime instance started yet", "at": None})
    newest = db.scalars(
        select(ChatSession).where(ChatSession.agent_id == agent.id,
                                  ChatSession.last_at >= (now - WINDOW).replace(tzinfo=None))
        .order_by(ChatSession.last_at.desc()).limit(1)).first()
    if newest is not None:
        try:
            hits = logs.filter_log_events(
                logGroupName=SPANS_LOG_GROUP, filterPattern=f'"{newest.session_id}"',
                startTime=_ms(newest.created_at.replace(tzinfo=UTC)
                              if newest.created_at.tzinfo is None else newest.created_at),
                limit=1).get("events") or []
            out["traces"] = {"session_id": newest.session_id, "arriving": bool(hits)}
        except Exception:  # noqa: BLE001
            out["traces"] = None
    hook_state = (out["hook"] or {}).get("state")
    if hook_state in ("unavailable", "skipped"):
        out["badge"] = "unavailable"
    elif out["traces"] is not None and not out["traces"]["arriving"]:
        out["badge"] = "not_arriving"
    elif out["traces"] is not None or hook_state in ("attached", "native"):
        out["badge"] = "ok"
    return out
