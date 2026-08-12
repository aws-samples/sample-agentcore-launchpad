"""Policy decision evidence read from CloudWatch metrics.

AgentCore publishes policy-decision *metrics* to the ``AWS/Bedrock-AgentCore``
namespace by default; per-decision *spans* require enabling trace delivery on the
attached Gateway and are handled elsewhere. This module owns the metric channel
only, which is why it is separate from ``governance.py``: the dimension shapes
below are preview-volatile and are kept out of the control-plane/ledger logic.

Two rules, both established empirically against a real account (see the task
research note ``policy-evidence-channels.md``) — get either wrong and the counts
are silently false rather than obviously broken:

1. **Never sum overlapping projections.** AWS publishes the same decision event
   under many dimension-name sets (``{OperationName}``,
   ``{TargetResource, OperationName, Mode}``,
   ``{Policy, TargetResource, OperationName, Mode}``, …). Every selection here
   matches an **exact** dimension-name set, never a subset.

2. **Projection availability differs per operation, so choose per operation.**
   ``AuthorizeAction`` publishes a gateway-level stream — one decision per call.
   ``PartiallyAuthorizeActions`` was observed publishing *only* ``ToolName``
   projections — one decision per (call, tool). A single fixed projection either
   misses every ``PartiallyAuthorizeActions`` decision (there is no gateway-level
   stream to find) or misreads tool-level counts as call counts. Each operation
   therefore resolves its own projection from a preference chain, and the
   resulting count carries a ``basis`` saying which unit it is in.
"""

import logging
from datetime import UTC, datetime
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError

from app.core.errors import AppError
from app.services.observability import cached, cw_client

log = logging.getLogger(__name__)

# The policy/service namespace. Note this is NOT `bedrock-agentcore`, which is
# the separate namespace observability.py reads for gen_ai token usage; both
# exist in the same account and they are not interchangeable.
NAMESPACE = "AWS/Bedrock-AgentCore"

OUTCOMES = {"allow": "AllowDecisions", "deny": "DenyDecisions"}
MISMATCH_METRICS = {
    "determining": "DeterminingPolicies",
    "no_determining": "NoDeterminingPolicies",
    "errors": "MismatchErrors",
}

WINDOW_SECONDS = {"1h": 3600, "6h": 21600, "24h": 86400, "7d": 604800}

# Per-operation projection preference chain. The first entry with at least one
# stream for the operation wins — exactly one projection is ever summed. Mode-
# bearing projections come first so the LOG_ONLY split stays available.
TOTAL_CHAIN: tuple[frozenset[str], ...] = (
    frozenset({"TargetResource", "OperationName", "Mode"}),
    frozenset({"TargetResource", "OperationName", "Mode", "ToolName"}),
    frozenset({"TargetResource", "OperationName"}),
    frozenset({"TargetResource", "OperationName", "ToolName"}),
)

DIMS_POLICY = frozenset({"Policy", "TargetResource", "OperationName", "Mode"})
DIMS_TOOL = frozenset({"TargetResource", "ToolName", "OperationName", "Mode"})
# The mismatch family was observed without Mode or ToolName dimensions.
DIMS_MISMATCH = frozenset({"TargetResource", "OperationName"})

MAX_QUERIES = 100


def _dim_map(metric: dict[str, Any]) -> dict[str, str]:
    return {d["Name"]: d["Value"] for d in metric.get("Dimensions") or []}


def _basis(exact_names: frozenset[str]) -> str:
    """Derived from the projection, not asserted: a ToolName-keyed projection
    counts one decision per (call, tool), anything else counts one per call."""
    return "per_tool" if "ToolName" in exact_names else "per_call"


def _select(
    metrics: list[dict[str, Any]],
    exact_names: frozenset[str],
    gateway_id: str,
    operation: str | None = None,
) -> list[dict[str, Any]]:
    """Streams whose dimension names are *exactly* ``exact_names``, scoped to this
    gateway (and optionally one operation). Exactness prevents double counting."""
    picked = []
    for metric in metrics:
        dims = _dim_map(metric)
        if frozenset(dims) != exact_names:
            continue
        if dims.get("TargetResource") != gateway_id:
            continue
        if operation is not None and dims.get("OperationName") != operation:
            continue
        picked.append(metric)
    return picked


def _list_metrics(cw: Any, metric_name: str) -> list[dict[str, Any]]:
    metrics: list[dict[str, Any]] = []
    for page in cw.get_paginator("list_metrics").paginate(
        Namespace=NAMESPACE, MetricName=metric_name
    ):
        metrics.extend(page.get("Metrics") or [])
    return metrics


def _sums(
    cw: Any,
    metrics: list[dict[str, Any]],
    period: int,
    start: datetime,
    end: datetime,
) -> tuple[list[float], bool]:
    """Window sum per stream, positionally aligned with ``metrics``."""
    truncated = len(metrics) > MAX_QUERIES
    batch = metrics[:MAX_QUERIES]
    if not batch:
        return [], False
    queries = [
        {
            "Id": f"m{index}",
            "MetricStat": {"Metric": metric, "Period": period, "Stat": "Sum"},
            "ReturnData": True,
        }
        for index, metric in enumerate(batch)
    ]
    response = cw.get_metric_data(MetricDataQueries=queries, StartTime=start, EndTime=end)
    totals = [0.0] * len(batch)
    for result in response.get("MetricDataResults") or []:
        index = int(str(result["Id"])[1:])
        if 0 <= index < len(totals):
            totals[index] = sum(result.get("Values") or [])
    return totals, truncated


def _operations(metrics: list[dict[str, Any]], gateway_id: str) -> list[str]:
    found = {
        _dim_map(m).get("OperationName")
        for m in metrics
        if _dim_map(m).get("TargetResource") == gateway_id
    }
    return sorted(o for o in found if o)


class _Acc:
    """Accumulates allow/deny per key, so a missing outcome reads as 0 not absent."""

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, float]] = {}

    def add(self, key: str, outcome: str, total: float) -> None:
        bucket = self.rows.setdefault(key, {"allow": 0.0, "deny": 0.0})
        bucket[outcome] += total

    def out(self, field: str) -> list[dict[str, Any]]:
        rows = [
            {field: key, "allow": round(v["allow"]), "deny": round(v["deny"])}
            for key, v in self.rows.items()
        ]
        rows.sort(key=lambda r: (-(r["allow"] + r["deny"]), str(r[field])))
        return rows


def _query(
    cw: Any,
    gateway_id: str,
    range_key: str,
    policy_id: str | None,
) -> dict[str, Any]:
    period = max(WINDOW_SECONDS[range_key], 60)
    end = datetime.now(tz=UTC)
    start = datetime.fromtimestamp(end.timestamp() - WINDOW_SECONDS[range_key], tz=UTC)

    by_mode, by_policy, by_tool = _Acc(), _Acc(), _Acc()
    operations: dict[str, dict[str, Any]] = {}
    totals = {"allow": 0.0, "deny": 0.0}
    log_only = {"allow": 0.0, "deny": 0.0}
    truncated = False
    policy_filter_partial = False

    for outcome, metric_name in OUTCOMES.items():
        metrics = _list_metrics(cw, metric_name)

        for operation in _operations(metrics, gateway_id):
            if policy_id is not None:
                # Only the Policy projection can answer a per-policy question.
                available = _select(metrics, DIMS_POLICY, gateway_id, operation)
                if not available:
                    # This operation publishes no Policy dimension at all, so its
                    # decisions cannot be attributed to a policy — say so rather
                    # than reporting a silently incomplete total.
                    policy_filter_partial = True
                streams = [m for m in available if _dim_map(m).get("Policy") == policy_id]
                basis = _basis(DIMS_POLICY)
            else:
                streams, basis = [], _basis(TOTAL_CHAIN[0])
                for exact in TOTAL_CHAIN:
                    streams = _select(metrics, exact, gateway_id, operation)
                    if streams:
                        basis = _basis(exact)
                        break
            if not streams:
                continue

            sums, cut = _sums(cw, streams, period, start, end)
            truncated = truncated or cut
            operation_total = sum(sums)
            totals[outcome] += operation_total
            for metric, total in zip(streams, sums, strict=False):
                mode = _dim_map(metric).get("Mode")
                if mode == "LOG_ONLY":
                    log_only[outcome] += total
                if mode:
                    by_mode.add(mode, outcome, total)

            row = operations.setdefault(
                operation, {"operation": operation, "allow": 0.0, "deny": 0.0, "basis": basis}
            )
            row[outcome] += operation_total
            # per_tool is the weaker unit; if any outcome had to fall back to it
            # the operation's number is tool-level, so report the weaker basis.
            if basis == "per_tool":
                row["basis"] = "per_tool"

        policy_streams = _select(metrics, DIMS_POLICY, gateway_id)
        for metric, total in _each(cw, policy_streams, period, start, end):
            by_policy.add(_dim_map(metric)["Policy"], outcome, total)
        tool_streams = _select(metrics, DIMS_TOOL, gateway_id)
        for metric, total in _each(cw, tool_streams, period, start, end):
            by_tool.add(_dim_map(metric)["ToolName"], outcome, total)

    mismatch = {}
    for label, metric_name in MISMATCH_METRICS.items():
        streams = _select(_list_metrics(cw, metric_name), DIMS_MISMATCH, gateway_id)
        sums, cut = _sums(cw, streams, period, start, end)
        truncated = truncated or cut
        mismatch[label] = round(sum(sums))

    operation_rows = [
        {**row, "allow": round(row["allow"]), "deny": round(row["deny"])}
        for row in operations.values()
    ]
    operation_rows.sort(key=lambda r: (-(r["allow"] + r["deny"]), r["operation"]))

    return {
        "range": range_key,
        "available": True,
        "unavailable_reason": None,
        "source": "metrics",
        "evidence_count": round(totals["allow"] + totals["deny"]),
        "log_only_count": round(log_only["allow"] + log_only["deny"]),
        "totals": {"allow": round(totals["allow"]), "deny": round(totals["deny"])},
        "by_operation": operation_rows,
        "by_mode": by_mode.out("mode"),
        "by_policy": by_policy.out("policy_id"),
        "by_tool": by_tool.out("tool"),
        "mismatch": mismatch,
        "truncated": truncated,
        "policy_filter_partial": policy_filter_partial,
        # Per-decision rows need spans (principal / reason / trace are not
        # expressible as metric dimensions). Empty here by design — never
        # synthesized from aggregates.
        "decisions": [],
        "count": 0,
    }


def _each(
    cw: Any,
    streams: list[dict[str, Any]],
    period: int,
    start: datetime,
    end: datetime,
) -> list[tuple[dict[str, Any], float]]:
    """``(stream, window sum)`` pairs — used for the breakdown accumulators."""
    sums, _ = _sums(cw, streams, period, start, end)
    return list(zip(streams, sums, strict=False))


def _unavailable(range_key: str, reason: str) -> dict[str, Any]:
    return {
        "range": range_key,
        "available": False,
        "unavailable_reason": reason,
        "source": "metrics",
        "evidence_count": 0,
        "log_only_count": 0,
        "totals": {"allow": 0, "deny": 0},
        "by_operation": [],
        "by_mode": [],
        "by_policy": [],
        "by_tool": [],
        "mismatch": dict.fromkeys(MISMATCH_METRICS, 0),
        "truncated": False,
        "policy_filter_partial": False,
        "decisions": [],
        "count": 0,
        "spans_unavailable_reason": None,
        "span_channel_status": "unknown",
        "span_channel_reason": "not_checked",
    }


def _aws_error_code(exc: Exception) -> str:
    if isinstance(exc, ClientError):
        return str(exc.response.get("Error", {}).get("Code") or "ClientError")
    return type(exc).__name__


def _with_spans(
    result: dict[str, Any],
    logs: Any,
    gateway_arn: str | None,
    gateway_id: str,
    range_key: str,
    policy_id: str | None,
) -> dict[str, Any]:
    """Attach per-decision rows from the span channel.

    Span failures degrade to metrics-only rather than failing the request: the
    metric aggregates are what the cutover gate reads and what already shipped, so
    a Logs Insights outage must not take the endpoint down. This is the one place
    where swallowing that error is correct.

    ``evidence_count`` is deliberately NOT recomputed from rows — spans are sampled
    while metrics are exact counts, so spans must never redefine the number the
    gate trusts.
    """
    result["decisions"] = []
    result["count"] = 0
    result["spans_unavailable_reason"] = None
    result["span_channel_status"] = "unknown"
    result["span_channel_reason"] = "not_checked"
    if not gateway_arn:
        result["span_channel_reason"] = "gateway_arn_missing"
        return result

    from app.services import governance_spans, policy_bootstrap

    if logs is not None:
        channel = policy_bootstrap.gateway_trace_delivery_status(
            logs, gateway_arn, gateway_id
        )
        result["span_channel_status"] = channel["status"]
        result["span_channel_reason"] = channel["reason"]

    try:
        spans = governance_spans.gateway_decision_rows(
            logs, gateway_arn, range_key, policy_id
        )
    except AppError as exc:
        log.warning("policy decision spans unavailable for %s: %s", gateway_arn, exc.code)
        result["spans_unavailable_reason"] = exc.code
        return result

    result["decisions"] = spans["decisions"]
    result["count"] = len(spans["decisions"])
    result["spans_unavailable_reason"] = spans["unavailable_reason"]
    result["truncated"] = bool(result.get("truncated")) or spans["truncated"]
    if spans["decisions"]:
        result["source"] = "metrics+spans"
    return result


def gateway_decisions(
    control: Any,
    cw: Any,
    gateway_id: str,
    range_key: str,
    workspace_id: str,
    policy_id: str | None = None,
    force: bool = False,
    logs: Any = None,
) -> dict[str, Any]:
    """Aggregate policy-decision evidence for one gateway and window, plus
    per-decision rows when the Policy span channel is open.

    A gateway that does not exist still 404s exactly as before. An unreadable
    metric channel yields ``available=False`` with the AWS error code — a
    different state from a readable channel that simply had no decisions in the
    window (``available=True`` with ``evidence_count=0``).
    """
    from app.services.governance import _require_gateway

    gateway = _require_gateway(control, gateway_id)
    if range_key not in WINDOW_SECONDS:
        range_key = "24h"

    def build() -> dict[str, Any]:
        try:
            result = _query(cw, gateway_id, range_key, policy_id)
        except (ClientError, BotoCoreError) as exc:
            code = _aws_error_code(exc)
            log.warning("policy decision metrics unavailable for %s: %s", gateway_id, code)
            result = _unavailable(range_key, code)
        return _with_spans(
            result,
            logs,
            gateway.get("gatewayArn"),
            gateway_id,
            range_key,
            policy_id,
        )

    key = f"gov-decisions:{workspace_id}:{gateway_id}:{range_key}:{policy_id or ''}"
    return cached(key, force, build)


def evidence_count(
    cw: Any,
    gateway_id: str,
    range_key: str = "24h",
    *,
    log_only: bool = True,
) -> int:
    """Best-effort count for the promotion/ENFORCE gate. Never raises.

    An unreadable channel returns 0, which falls back to the existing typed
    override path — a CloudWatch outage must not make a legitimate override
    impossible. ``log_only`` matches the documented gate rule, which asks for
    LOG_ONLY evidence specifically.
    """
    try:
        result = _query(cw, gateway_id, range_key, None)
    except (ClientError, BotoCoreError) as exc:
        log.warning("evidence count unavailable for %s: %s", gateway_id, _aws_error_code(exc))
        return 0
    except Exception:  # noqa: BLE001 - the gate must never be blocked by telemetry
        log.exception("unexpected error counting evidence for %s", gateway_id)
        return 0
    return int(result["log_only_count" if log_only else "evidence_count"])


__all__ = ["cw_client", "evidence_count", "gateway_decisions"]
