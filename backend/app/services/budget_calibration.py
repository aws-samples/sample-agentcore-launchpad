"""Hourly Runtime calibration, and the report-only Cost Explorer comparison.

**Calibration.** Runtime compute is charged as billable session time × a GB and a
vCPU factor per runtime (``services/budget_reconcile.py``). The factors start at the
admin's seeds (``rates.DEFAULT_CALIBRATION``: a Harness microVM ~8 GB, other
runtimes ~1.5 GB) and are then measured, once an hour, per runtime this box's
sessions touched:

    GB factor   = MemoryUsed-GBHours (the hour)  ÷  billable session-hours (the hour)
    vCPU factor = CPUUsed-vCPUHours  (the hour)  ÷  billable session-hours (the hour)

- **Metrics:** ``AWS/Bedrock-AgentCore``, dimensions ``{Service=AgentCore.Runtime,
  Resource=<runtime ARN>}``, ``Sum`` over one hour, one batched ``GetMetricData``
  (live probe). They may be delayed up to 60 min, so the hour read is the one that
  ended at least 90 minutes ago.
- **Session-hours** come from **every** session's spans on that runtime — prod and
  prod2 share the AWS account, and the metric covers both boxes, so the divisor must
  too. Billable time is each session's trace intervals plus the runtime's idle tail
  (the reconciler's own rule), intersected with the hour.
- A sample with no metric datapoint, no span session-time, or an implausible factor
  (beyond the 8 GB / 2 vCPU session ceiling) is dropped. Samples merge into the
  stored factor weighted by session count (the history's weight capped, so the
  factor follows a changing agent). The reconciler uses a measured factor only once
  ``calibration_min_sessions`` sessions back it; below that the seeds apply.

The factors are stored with their sample sizes in ``agentcore_rates.calibration.runtimes``.

**Cost Explorer report.** ``cost_explorer_report`` compares this month's ledger
AgentCore fees with Cost Explorer's month-to-date AgentCore bill per account. Cost
Explorer is account-wide — every region and every box sharing the account (prod +
prod2) — so it never charges anyone; an admin opens it on demand ($0.01 per call,
cached for an hour).
"""

from __future__ import annotations

import logging
import threading
import time
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from app.core.db import SessionLocal
from app.models.budgets import UsageEvent
from app.models.ledger import Workspace
from app.services import budget_reconcile as br
from app.services import budgets, rates
from app.services import usage_meter as um
from app.services.observability import SPANS_SOURCE, logs_client
from app.services.workspace import WorkspaceContext, workspace_context

logger = logging.getLogger("launchpad.usage")

METRIC_NAMESPACE = "AWS/Bedrock-AgentCore"
GB_METRIC = "MemoryUsed-GBHours"
CPU_METRIC = "CPUUsed-vCPUHours"
RUNTIME_SERVICE = "AgentCore.Runtime"
# metrics may be delayed up to 60 min (devguide): read an hour that ended ≥ 90 min ago
CALIBRATION_DELAY = timedelta(minutes=90)
# GetMetricData takes 500 queries per call: two per runtime
RUNTIMES_PER_CALL = 250
# the per-session hardware ceiling is 2 vCPU / 8 GB (devguide quotas); a factor far
# above it means the metric covers sessions the spans do not (an uninstrumented
# client of the same runtime), so the sample is dropped
MIN_GB, MAX_GB = 0.128, 8.5
MAX_VCPU = 2.5
# the history's weight in a merge, in sessions: newer hours keep moving the factor
HISTORY_WEIGHT_CAP = 50
# how far back a runtime_compute row may have started and still cover the hour
_ROW_LOOKBACK = timedelta(hours=9)


def _floor_hour(value: datetime) -> datetime:
    return value.replace(minute=0, second=0, microsecond=0)


def calibration_hour(now: datetime | None = None) -> datetime:
    """The start of the latest hour whose metrics have surely landed."""
    return _floor_hour((now or datetime.now(UTC)) - CALIBRATION_DELAY) - timedelta(hours=1)


def q_runtime_sessions() -> str:
    """Per (session, trace): its span interval and the runtime it ran on — every
    session in the account, not only this box's."""
    return f"""
{SPANS_SOURCE}
| filter ispresent(startTimeUnixNano) and ispresent(attributes.session.id)
| stats min(startTimeUnixNano) as start_ns, max(endTimeUnixNano) as end_ns,
        latest(resource.attributes.cloud.resource_id) as resource
  by attributes.session.id as session_id, traceId
| limit {br.RESULT_LIMIT}
"""


def runtimes_touched(db: Session, workspace_id: str, start: datetime, end: datetime
                     ) -> dict[str, str]:
    """Runtime id → ARN of the runtimes this box's sessions ran on in ``[start, end)``
    (from the reconciler's ``runtime_compute`` rows)."""
    out: dict[str, str] = {}
    rows = (
        db.query(UsageEvent.detail)
        .filter(UsageEvent.kind == um.KIND_RUNTIME_COMPUTE,
                UsageEvent.origin_workspace_id == workspace_id,
                UsageEvent.created_at >= start - _ROW_LOOKBACK)
        .all()
    )
    for (detail,) in rows:
        if not isinstance(detail, dict):
            continue
        runtime_id, arn = detail.get("runtime_id"), detail.get("runtime_arn")
        try:
            first = float(detail.get("start") or 0)
            last = float(detail.get("end") or 0) + float(detail.get("tail_s") or 0)
        except (TypeError, ValueError):
            continue
        if runtime_id and arn and first < end.timestamp() and last > start.timestamp():
            out[str(runtime_id)] = str(arn)
    return out


def session_hours(
    rows: list[dict[str, Any]], runtimes: dict[str, dict[str, Any]],
    start: datetime, end: datetime,
) -> dict[str, tuple[float, int]]:
    """Runtime id → (billable session-hours inside ``[start, end)``, sessions) from
    span rows. ``runtimes`` maps each runtime id of interest to its lifecycle
    (``budget_reconcile.runtime_info``)."""
    per: dict[tuple[str, str], list[tuple[float, float]]] = defaultdict(list)
    for row in rows:
        runtime_id = br.runtime_id_of(row.get("resource"))
        if runtime_id not in runtimes:
            continue
        first, last = br._num(row, "start_ns") / 1e9, br._num(row, "end_ns") / 1e9
        if last <= 0:
            continue
        per[(runtime_id, str(row.get("session_id") or ""))].append((first or last, last))
    h0, h1 = start.timestamp(), end.timestamp()
    out: dict[str, tuple[float, int]] = {}
    for (runtime_id, _sid), intervals in per.items():
        info = runtimes[runtime_id]
        tail = br.idle_tail_seconds(info)
        max_life = float(info.get("max_lifetime_s") or br.DEFAULT_MAX_LIFETIME_SECONDS)
        merged: list[list[float]] = []
        for first, last in sorted(intervals):
            if (merged and first <= merged[-1][1] + tail
                    and first < merged[-1][0] + max_life):
                merged[-1][1] = max(merged[-1][1], last)
            else:
                merged.append([first, last])
        seconds = 0.0
        for first, last in merged:
            stop = min(last + tail, first + max_life)
            seconds += max(0.0, min(stop, h1) - max(first, h0))
        if seconds <= 0:
            continue
        hours, sessions = out.get(runtime_id, (0.0, 0))
        out[runtime_id] = (hours + seconds / 3600.0, sessions + 1)
    return out


def metric_sums(cw: Any, arns: dict[str, str], start: datetime, end: datetime
                ) -> dict[str, dict[str, float]]:
    """Runtime id → ``{gb_hours, vcpu_hours}`` for the hour (absent when the metric
    has no datapoint), in batched ``GetMetricData`` calls."""
    out: dict[str, dict[str, float]] = {}
    items = sorted(arns.items())
    for offset in range(0, len(items), RUNTIMES_PER_CALL):
        batch = items[offset:offset + RUNTIMES_PER_CALL]
        queries, ids = [], {}
        for index, (runtime_id, arn) in enumerate(batch):
            for prefix, metric in (("g", GB_METRIC), ("c", CPU_METRIC)):
                query_id = f"{prefix}{offset + index}"
                ids[query_id] = (runtime_id, "gb_hours" if prefix == "g" else "vcpu_hours")
                queries.append({
                    "Id": query_id, "ReturnData": True,
                    "MetricStat": {
                        "Metric": {"Namespace": METRIC_NAMESPACE, "MetricName": metric,
                                   "Dimensions": [{"Name": "Service", "Value": RUNTIME_SERVICE},
                                                  {"Name": "Resource", "Value": arn}]},
                        "Period": 3600, "Stat": "Sum",
                    },
                })
        response = cw.get_metric_data(MetricDataQueries=queries, StartTime=start, EndTime=end)
        for result in response.get("MetricDataResults") or []:
            target = ids.get(result.get("Id"))
            values = result.get("Values") or []
            if target is None or not values:
                continue
            out.setdefault(target[0], {})[target[1]] = float(sum(values))
    return out


def merge_factor(old: dict[str, Any] | None, *, gb: float, vcpu: float, sessions: int,
                 hours: float, hour: datetime, now: datetime, runtime_name: str,
                 sample: dict[str, Any]) -> dict[str, Any]:
    """Fold one hour's sample into a runtime's stored factor (weighted by sessions,
    the history's weight capped at ``HISTORY_WEIGHT_CAP``)."""
    if old:
        weight = min(int(old.get("sessions") or 0), HISTORY_WEIGHT_CAP)
        total = weight + sessions
        gb = (float(old["gb"]) * weight + gb * sessions) / total
        vcpu = (float(old["vcpu"]) * weight + vcpu * sessions) / total
    return {
        "gb": round(gb, 6), "vcpu": round(vcpu, 6),
        "sessions": int((old or {}).get("sessions") or 0) + sessions,
        "hours": int((old or {}).get("hours") or 0) + 1,
        "session_hours": round(float((old or {}).get("session_hours") or 0.0) + hours, 6),
        "last_hour": br._iso(hour), "updated_at": br._iso(now), "runtime_name": runtime_name,
        "last_sample": sample,
    }


def calibrate_workspace(
    db: Session, workspace_id: str, ctx: WorkspaceContext, hour: datetime, now: datetime,
    factors: dict[str, dict[str, Any]], *, logs: Any = None, cw: Any = None,
) -> dict[str, Any]:
    """One workspace's hour: the runtimes its sessions touched, their span
    session-hours and metrics, folded into ``factors`` (in place)."""
    end = hour + timedelta(hours=1)
    arns = runtimes_touched(db, workspace_id, hour, end)
    if not arns:
        return {"runtimes": 0, "samples": 0}
    default_idle = rates.calibration(db)["runtime_idle_timeout_seconds"]
    infos = {rid: br.runtime_info(ctx, workspace_id, rid, default_idle) for rid in arns}
    longest_tail = max((br.idle_tail_seconds(info) for info in infos.values()), default=0.0)
    scan_start = hour - timedelta(seconds=longest_tail + 60)
    rows = br._insights(logs if logs is not None else logs_client(ctx),
                        {"runtime_sessions": q_runtime_sessions()}, scan_start, end
                        )["runtime_sessions"]
    if len(rows) >= br.RESULT_LIMIT:
        # a truncated divisor would inflate every factor: skip the hour
        raise RuntimeError(f"calibration spans query hit the {br.RESULT_LIMIT}-row limit")
    hours = session_hours(rows, infos, hour, end)
    measured = metric_sums(cw if cw is not None else ctx.client("cloudwatch"),
                           {rid: arns[rid] for rid in hours}, hour, end) if hours else {}
    samples, dropped = 0, []
    for runtime_id, (session_h, sessions) in hours.items():
        metric = measured.get(runtime_id) or {}
        if "gb_hours" not in metric or session_h <= 0:
            dropped.append({"runtime_id": runtime_id, "reason": "no_metric"})
            continue
        gb = metric["gb_hours"] / session_h
        vcpu = metric.get("vcpu_hours", 0.0) / session_h
        if not (MIN_GB <= gb <= MAX_GB) or not (0 <= vcpu <= MAX_VCPU):
            dropped.append({"runtime_id": runtime_id, "reason": "implausible",
                            "gb": round(gb, 4), "vcpu": round(vcpu, 4)})
            continue
        sample = {"gb": round(gb, 6), "vcpu": round(vcpu, 6), "sessions": sessions,
                  "session_hours": round(session_h, 6),
                  "gb_hours": round(metric["gb_hours"], 6),
                  "vcpu_hours": round(metric.get("vcpu_hours", 0.0), 6),
                  "hour": br._iso(hour)}
        factors[runtime_id] = merge_factor(
            factors.get(runtime_id), gb=gb, vcpu=vcpu, sessions=sessions, hours=session_h,
            hour=hour, now=now, runtime_name=br.runtime_name_of(runtime_id), sample=sample,
        )
        samples += 1
    return {"runtimes": len(arns), "samples": samples, "dropped": dropped}


def calibrate(now: datetime | None = None, *, logs_for: Any = None, cw_for: Any = None,
              force: bool = False) -> dict[str, Any]:
    """The hourly pass (a no-op when the due hour is already calibrated). Never
    raises; one workspace failing does not stop the others."""
    now = now or datetime.now(UTC)
    hour = calibration_hour(now)
    outcome: dict[str, Any] = {"hour": br._iso(hour), "workspaces": {}, "failures": []}
    try:
        db = SessionLocal()
        try:
            state = br.load_state(db)
            done = br._parse_iso((state.get("calibration") or {}).get("last_hour"))
            if done is not None and done >= hour and not force:
                outcome["skipped"] = True
                return outcome
            workspaces = br._workspaces(db)
            factors = rates.runtime_factors(db)
            for ws in workspaces:
                try:
                    result = calibrate_workspace(
                        db, ws.id, workspace_context(ws), hour, now, factors,
                        logs=logs_for(ws.id) if logs_for else None,
                        cw=cw_for(ws.id) if cw_for else None,
                    )
                    outcome["workspaces"][ws.id] = result
                except Exception as exc:  # noqa: BLE001 — the next hour tries again
                    logger.warning("budget calibration failed for workspace %s: %s: %s",
                                   ws.id, type(exc).__name__, exc)
                    outcome["failures"].append({"workspace_id": ws.id, "at": br._iso(now),
                                                "error": f"{type(exc).__name__}: {exc}"[:300]})
            if any(r.get("samples") for r in outcome["workspaces"].values()):
                rates.store_runtime_factors(db, factors)
        finally:
            db.close()

        def record(current: dict[str, Any]) -> None:
            current["calibration"] = {
                "last_hour": br._iso(hour), "at": br._iso(now),
                "runtimes": sum(int(r.get("runtimes") or 0)
                                for r in outcome["workspaces"].values()),
                "samples": sum(int(r.get("samples") or 0)
                               for r in outcome["workspaces"].values()),
                "dropped": [d for r in outcome["workspaces"].values()
                            for d in (r.get("dropped") or [])][:br.MAX_FAILURES_KEPT],
                "failures": outcome["failures"][:br.MAX_FAILURES_KEPT],
            }

        br._update_state(record)
    except Exception as exc:  # noqa: BLE001 — the loop must never die
        logger.warning("budget calibration failed: %s", exc)
        outcome["failures"].append({"workspace_id": None, "error": str(exc)[:300]})
    return outcome


# ── report only: the ledger's AgentCore fees vs Cost Explorer ─────────────────

AGENTCORE_SERVICE = "Amazon Bedrock AgentCore"
REPORT_TTL_SECONDS = 3600.0
FEE_KINDS = {
    um.KIND_RUNTIME_COMPUTE: "runtime",
    um.KIND_MEMORY_EVENT: "memory",
    um.KIND_MEMORY_RETRIEVAL: "memory",
    um.KIND_GATEWAY_CALL: "gateway",
    um.KIND_WEB_SEARCH: "web_search",
    um.KIND_KB_QUERY: "knowledge_base",
    um.KIND_CODE_INTERPRETER: "code_interpreter",
    um.KIND_EVALUATION_BUILTIN: "evaluations_builtin",
    um.KIND_EVALUATION_CUSTOM: "evaluations_custom",
}
CATEGORIES = ("runtime", "evaluations_builtin", "evaluations_custom", "web_search", "memory",
              "gateway", "knowledge_base", "code_interpreter", "browser", "other", "overhead")

_REPORT_LOCK = threading.Lock()
_REPORT: dict[str, tuple[float, dict[str, Any]]] = {}


def reset_report_cache() -> None:
    with _REPORT_LOCK:
        _REPORT.clear()


def ce_category(usage_type: str) -> str:
    """A Cost Explorer AgentCore usage type (``USE1-Runtime:Consumption-based:Memory``)
    → the report's category. Storage, tool indexing and data transfer are workspace
    overhead, never charged to people."""
    text = usage_type.split("-", 1)[1] if usage_type[:4].isupper() and "-" in usage_type[:6] \
        else usage_type
    if "DataTransfer" in usage_type or "-Bytes" in usage_type:
        return "overhead"
    if text.startswith("Runtime:"):
        return "runtime"
    if text.startswith("Evaluations:"):
        return "evaluations_custom" if "CustomEvaluators" in text else "evaluations_builtin"
    if text.startswith("WebSearchTool:"):
        return "web_search"
    if text.startswith("Memory:"):
        return "overhead" if "Storage" in text else "memory"
    if text.startswith("Gateway:"):
        return "overhead" if "Tool-Indexing" in text else "gateway"
    if text.startswith("Policy:"):
        return "gateway"
    if text.startswith("Knowledge-Base:"):
        return "knowledge_base"
    if text.startswith("CodeInterpreter:"):
        return "code_interpreter"
    if text.startswith("BrowserTool:"):
        return "browser"
    return "other"


def _month_to_date(ce: Any, now: datetime) -> dict[str, Any]:
    start = now.replace(day=1).date().isoformat()
    end = (now.date() + timedelta(days=1)).isoformat()
    response = ce.get_cost_and_usage(
        TimePeriod={"Start": start, "End": end}, Granularity="MONTHLY",
        Metrics=["UnblendedCost"],
        Filter={"Dimensions": {"Key": "SERVICE", "Values": [AGENTCORE_SERVICE]}},
        GroupBy=[{"Type": "DIMENSION", "Key": "USAGE_TYPE"}],
    )
    by_category: dict[str, float] = defaultdict(float)
    by_region: dict[str, float] = defaultdict(float)
    estimated = False
    for period in response.get("ResultsByTime") or []:
        estimated = estimated or bool(period.get("Estimated"))
        for group in period.get("Groups") or []:
            usage_type = str((group.get("Keys") or [""])[0])
            try:
                usd = float(group["Metrics"]["UnblendedCost"]["Amount"])
            except (KeyError, TypeError, ValueError):
                continue
            by_category[ce_category(usage_type)] += usd
            prefix = usage_type.split("-", 1)[0] if "-" in usage_type[:6] else "global"
            by_region[prefix] += usd
    return {"by_category": dict(by_category), "by_region": dict(by_region),
            "estimated": estimated, "start": start, "end": end}


def _ledger_fees(db: Session, period: str, workspace_ids: list[str]) -> dict[str, float]:
    rows = (
        db.query(UsageEvent.kind, UsageEvent.usd)
        .filter(UsageEvent.period == period, UsageEvent.superseded_by.is_(None),
                UsageEvent.kind.in_(list(FEE_KINDS)),
                UsageEvent.origin_workspace_id.in_(workspace_ids))
        .all()
    )
    out: dict[str, float] = defaultdict(float)
    for kind, usd in rows:
        out[FEE_KINDS[kind]] += float(usd or 0.0)
    return dict(out)


def cost_explorer_report(db: Session, *, refresh: bool = False,
                         ce_for: Any = None, now: datetime | None = None) -> dict[str, Any]:
    """Per AWS account: this month's ledger AgentCore fees (this box's people) next to
    Cost Explorer's month-to-date AgentCore bill (the whole account: every region and
    every box). Report only. Cached for an hour; ``refresh`` re-reads ($0.01 per
    account)."""
    now = now or datetime.now(UTC)
    period = budgets.period_of(now)
    with _REPORT_LOCK:
        hit = _REPORT.get(period)
        if hit and not refresh and time.monotonic() - hit[0] < REPORT_TTL_SECONDS:
            return {**hit[1], "cached": True,
                    "age_seconds": round(time.monotonic() - hit[0], 1)}
    accounts: dict[str, list[Workspace]] = defaultdict(list)
    for ws in db.query(Workspace).order_by(Workspace.id):
        accounts[ws.account_id or "?"].append(ws)
    out_accounts = []
    for account_id, rows in sorted(accounts.items()):
        ledger = _ledger_fees(db, period, [ws.id for ws in rows])
        entry: dict[str, Any] = {
            "account_id": account_id,
            "workspaces": [{"id": ws.id, "name": ws.name, "region": ws.region} for ws in rows],
            "ledger_total_usd": round(sum(ledger.values()), 6),
        }
        try:
            ce = ce_for(account_id) if ce_for else workspace_context(rows[0]).client("ce")
            mtd = _month_to_date(ce, now)
            entry.update({
                "cost_explorer_total_usd": round(
                    sum(v for k, v in mtd["by_category"].items() if k != "overhead"), 6),
                "overhead_usd": round(mtd["by_category"].get("overhead", 0.0), 6),
                "by_region": {k: round(v, 6) for k, v in sorted(mtd["by_region"].items())},
                "estimated": mtd["estimated"], "error": None,
            })
            ce_cats = mtd["by_category"]
        except Exception as exc:  # noqa: BLE001 — e.g. a spoke role without ce:Get*
            entry.update({"cost_explorer_total_usd": None, "overhead_usd": None,
                          "by_region": {}, "estimated": None,
                          "error": f"{type(exc).__name__}: {exc}"[:300]})
            ce_cats = None
        entry["rows"] = [
            {"category": category, "ledger_usd": round(ledger.get(category, 0.0), 6),
             "cost_explorer_usd": (None if ce_cats is None
                                   else round(ce_cats.get(category, 0.0), 6))}
            for category in CATEGORIES
            if ledger.get(category) or (ce_cats or {}).get(category)
        ]
        out_accounts.append(entry)
    report = {"period": period, "generated_at": br._iso(now), "account_wide": True,
              "charges_anyone": False, "accounts": out_accounts}
    with _REPORT_LOCK:
        _REPORT[period] = (time.monotonic(), report)
    return {**report, "cached": False, "age_seconds": 0.0}
