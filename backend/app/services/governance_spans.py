"""Per-decision Policy rows parsed from `aws/spans`.

The metric channel (`governance_evidence.py`) gives exact counts but cannot express
a principal, an action, or a trace link. Those live in spans — which AgentCore emits
only after trace delivery is enabled on the attached Gateway
(`policy_bootstrap.ensure_gateway_traces`).

Every attribute name below appears in a real captured span, recorded verbatim in
the task research note. Two consequences of that capture shape the module:

* **`AgentCore.Gateway.InvokeTool` is the row source, not the Policy span.** That
  one SERVER span carries `tool.name` *and*
  `aws.agentcore.policy.authorization_decision`, so action and outcome need no
  join; the child `AgentCore.Policy.*` span only adds the policy id lists.
* **There is no principal.** No span in the captured trace carries any
  principal/actor/subject attribute, because the Harness authenticates to the
  Gateway with an OAuth M2M client credential — the request has no human subject.
  The field stays in the row shape as `None` rather than being inferred.

`aws.agentcore.policy.authorization_reason` is deliberately absent from this module:
AWS documents it, but it did not appear on the captured span, and referencing a
documented-but-unobserved field is the exact mistake the research gate exists to
prevent. A test asserts this file does not mention it.
"""

import json
from datetime import UTC, datetime
from typing import Any

from app.core.errors import AppError
from app.services.observability import SPANS_LOG_GROUP, run_insights_queries

RANGE_HOURS = {"1h": 1, "6h": 6, "24h": 24, "7d": 168}

INVOKE_SPAN = "AgentCore.Gateway.InvokeTool"
AUTHORIZE_SPAN = "AgentCore.Policy.AuthorizeAction"
PARTIAL_SPAN = "AgentCore.Policy.PartiallyAuthorizeActions"
POLICY_SPANS = (AUTHORIZE_SPAN, PARTIAL_SPAN)

# Logs Insights caps and our own row cap. Truncation is reported, never silent.
SPAN_SCAN_LIMIT = 400
ROW_LIMIT = 200


def _attrs(span: dict[str, Any]) -> dict[str, Any]:
    return span.get("attributes") or {}


def _iso(nano: Any) -> str | None:
    try:
        seconds = int(nano) / 1_000_000_000
    except (TypeError, ValueError):
        return None
    return datetime.fromtimestamp(seconds, tz=UTC).isoformat()


def _as_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(v) for v in value]
    if value in (None, ""):
        return []
    return [str(value)]


def _engine_id(arn: Any) -> str | None:
    """`…:policy-engine/launchpad_pe-x` → `launchpad_pe-x`."""
    if not arn or not isinstance(arn, str) or "/" not in arn:
        return None
    return arn.rsplit("/", 1)[-1]


def _decision_query(gateway_arn: str) -> str:
    names = ", ".join(f'"{n}"' for n in (INVOKE_SPAN, *POLICY_SPANS))
    return (
        "fields @message"
        f" | filter name in [{names}]"
        f' and `attributes.aws.resource.arn` = "{gateway_arn}"'
        " | sort @timestamp desc"
        f" | limit {SPAN_SCAN_LIMIT}"
    )


def _session_query(trace_ids: list[str]) -> str:
    ids = ", ".join(f'"{t}"' for t in trace_ids)
    return (
        "fields traceId, `attributes.session.id`"
        f" | filter traceId in [{ids}] and ispresent(`attributes.session.id`)"
        f" | limit {SPAN_SCAN_LIMIT}"
    )


def _parse(rows: list[dict[str, str]]) -> list[dict[str, Any]]:
    """Raw `@message` JSON per row. Arrays like `determining_policies` cannot be
    selected as flat Logs Insights fields, which is why the whole message is
    fetched and decoded here."""
    spans = []
    for row in rows:
        raw = row.get("@message")
        if not raw:
            continue
        try:
            spans.append(json.loads(raw))
        except (ValueError, TypeError):
            continue
    return spans


def _row(
    span: dict[str, Any],
    policy: dict[str, Any] | None,
    *,
    action: str | None,
    outcome: str | None,
    evaluation: str,
) -> dict[str, Any]:
    attrs = _attrs(span)
    pattrs = _attrs(policy) if policy else {}
    determining = _as_list(pattrs.get("aws.agentcore.policy.determining_policies"))
    engine_arn = pattrs.get("aws.agentcore.gateway.policy.arn") or attrs.get(
        "aws.agentcore.gateway.policy.arn"
    )
    return {
        "at": _iso(span.get("startTimeUnixNano")),
        "gateway_id": (
            pattrs.get("aws.agentcore.policy.target_resource.id")
            or attrs.get("aws.agentcore.policy.target_resource.id")
            or attrs.get("gateway.id")
        ),
        "gateway_arn": attrs.get("aws.resource.arn"),
        "engine_id": _engine_id(engine_arn),
        "policy_id": determining[0] if determining else None,
        "determining_policies": determining,
        "mismatched_policies": _as_list(
            pattrs.get("aws.agentcore.policy.mismatched_policies")
        ),
        # Undocumented but captured: what a LOG_ONLY candidate policy would have
        # matched, visible even from an ENFORCE-mode span. The metric channel
        # cannot express this.
        "log_only_matched_policies": _as_list(
            pattrs.get("aws.agentcore.policy.log_only_matched_policies")
        ),
        # Structurally unavailable: the Harness authenticates with an M2M client
        # credential, so no span in the trace carries a human principal.
        "principal": None,
        "action": action,
        "outcome": outcome,
        # Only the Gateway attachment mode is in the span; the per-policy mode is
        # not, so it stays absent rather than being guessed from the engine mode.
        "engine_mode": (
            pattrs.get("aws.agentcore.gateway.policy.mode")
            or attrs.get("aws.agentcore.gateway.policy.mode")
        ),
        "policy_mode": None,
        "trace_id": span.get("traceId"),
        "span_id": span.get("spanId"),
        "session_id": None,
        "evaluation": evaluation,
        "source": "aws",
    }


def _assemble(spans: list[dict[str, Any]]) -> list[dict[str, Any]]:
    invokes = [s for s in spans if s.get("name") == INVOKE_SPAN]
    # The captured trace shows the Policy span as a *child* of the Gateway span.
    by_parent: dict[str, dict[str, Any]] = {}
    for span in spans:
        if span.get("name") in POLICY_SPANS and span.get("parentSpanId"):
            by_parent[span["parentSpanId"]] = span

    rows: list[dict[str, Any]] = []
    for span in invokes:
        attrs = _attrs(span)
        rows.append(
            _row(
                span,
                by_parent.get(span.get("spanId") or ""),
                action=attrs.get("tool.name"),
                outcome=attrs.get("aws.agentcore.policy.authorization_decision"),
                evaluation="invocation",
            )
        )

    for span in spans:
        if span.get("name") != PARTIAL_SPAN:
            continue
        # Only denials become rows. A single tools/list yields many allowed tools,
        # so expanding both sides would flood the table with one row per tool per
        # list call while adding nothing the aggregate panel does not show.
        for tool in _as_list(_attrs(span).get("aws.agentcore.policy.denied_tools")):
            rows.append(
                _row(
                    span,
                    span,
                    action=tool,
                    outcome="DENY",
                    # A list-time tool-availability decision, not a blocked call:
                    # under ENFORCE the tool is withheld from the model entirely.
                    evaluation="tool_listing",
                )
            )

    rows.sort(key=lambda r: (r["at"] or ""), reverse=True)
    return rows


def gateway_decision_rows(
    logs: Any,
    gateway_arn: str,
    range_key: str,
    policy_id: str | None = None,
) -> dict[str, Any]:
    """Per-decision rows for one gateway and window.

    Raises nothing on query failure — returns ``unavailable_reason`` so the caller
    can degrade to metrics-only.
    """
    hours = RANGE_HOURS.get(range_key, 24)
    try:
        first = run_insights_queries(
            {"decisions": _decision_query(gateway_arn)},
            hours,
            logs=logs,
            log_groups=[SPANS_LOG_GROUP],
        )
    except AppError as exc:
        return {"decisions": [], "unavailable_reason": exc.code, "truncated": False}

    raw = first.get("decisions") or []
    rows = _assemble(_parse(raw))
    if policy_id:
        rows = [r for r in rows if policy_id in r["determining_policies"]]
    truncated = len(raw) >= SPAN_SCAN_LIMIT or len(rows) > ROW_LIMIT
    rows = rows[:ROW_LIMIT]

    trace_ids = sorted({r["trace_id"] for r in rows if r["trace_id"]})
    if trace_ids:
        # session.id lives on the runtime/mcp spans, not the decision spans, so it
        # needs a second pass. Skipped entirely when there is nothing to join.
        try:
            second = run_insights_queries(
                {"sessions": _session_query(trace_ids)},
                hours,
                logs=logs,
                log_groups=[SPANS_LOG_GROUP],
            )
            sessions = {
                row["traceId"]: row.get("attributes.session.id")
                for row in second.get("sessions") or []
                if row.get("traceId")
            }
            for row in rows:
                row["session_id"] = sessions.get(row["trace_id"])
        except AppError:
            # Rows are still useful without the session link.
            pass

    return {"decisions": rows, "unavailable_reason": None, "truncated": truncated}
