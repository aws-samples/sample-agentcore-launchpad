"""Policy decision evidence from CloudWatch metrics.

The fixtures below reproduce the **real** dimension projections observed on
`launchpad-gw-em0yuqmmdp` (recorded in the task research note). The sparseness is
the thing under test: AWS publishes many overlapping projections of one event,
and `AuthorizeAction` / `PartiallyAuthorizeActions` do not publish the same ones.
"""

from typing import Any

import pytest
from botocore.exceptions import ClientError

from app.services import governance_evidence as ge
from app.services.observability import reset_cache

GW = "launchpad-gw-em0yuqmmdp"


def _metric(name: str, **dims: str) -> dict[str, Any]:
    return {
        "Namespace": ge.NAMESPACE,
        "MetricName": name,
        "Dimensions": [{"Name": k, "Value": v} for k, v in dims.items()],
    }


def _key(metric: dict[str, Any]) -> tuple:
    return (
        metric["MetricName"],
        frozenset((d["Name"], d["Value"]) for d in metric["Dimensions"]),
    )


class FakeCW:
    """Minimal CloudWatch stub: streams plus a per-stream window total."""

    def __init__(self, streams: list[tuple[dict[str, Any], float]]) -> None:
        self.streams = streams
        self.totals = {_key(m): v for m, v in streams}
        self.get_metric_data_calls = 0
        self.list_metrics_calls = 0

    def get_paginator(self, name: str):
        assert name == "list_metrics"
        outer = self

        class _P:
            def paginate(self, Namespace: str, MetricName: str):  # noqa: N803
                outer.list_metrics_calls += 1
                assert Namespace == ge.NAMESPACE
                return [
                    {"Metrics": [m for m, _ in outer.streams if m["MetricName"] == MetricName]}
                ]

        return _P()

    def get_metric_data(self, MetricDataQueries, StartTime, EndTime):  # noqa: N803
        self.get_metric_data_calls += 1
        results = []
        for query in MetricDataQueries:
            metric = query["MetricStat"]["Metric"]
            total = self.totals.get(_key(metric))
            results.append(
                {"Id": query["Id"], "Values": [] if total is None else [total]}
            )
        return {"MetricDataResults": results}


def _control() -> Any:
    class _C:
        pass

    return _C()


@pytest.fixture(autouse=True)
def _no_cache(monkeypatch):
    """Bypass the shared TTL cache and the gateway existence check by default."""
    reset_cache()
    monkeypatch.setattr(
        "app.services.governance._require_gateway",
        lambda control, gateway_id: {"gatewayId": gateway_id, "protocolType": "MCP"},
    )
    yield
    reset_cache()


# The real projection matrix: AuthorizeAction publishes a gateway-level stream,
# PartiallyAuthorizeActions publishes only ToolName-bearing streams, and several
# redundant projections of both are also published.
TOOLS = [
    "hr-database___get_employee",
    "hr-database___check_calendar",
    "hr-database___list_departments",
    "office-facts___get_office_fact",
    "office-facts___list_office_topics",
]
ENGINE = "launchpad_pe-rwtcceczvs"
POLICY = "launchpad_baseline_allow-obafj1o9hj"


def _realistic_streams(mode: str = "ENFORCE") -> list[tuple[dict[str, Any], float]]:
    allow, deny = "AllowDecisions", "DenyDecisions"
    streams: list[tuple[dict[str, Any], float]] = [
        # AuthorizeAction — one decision per call, 57 in window.
        (_metric(allow, TargetResource=GW, OperationName="AuthorizeAction", Mode=mode), 57.0),
        # …and its redundant projections, which must NOT be added on top.
        (_metric(allow, TargetResource=GW, OperationName="AuthorizeAction"), 57.0),
        (
            _metric(
                allow, TargetResource=GW, OperationName="AuthorizeAction",
                Mode=mode, PolicyEngine=ENGINE,
            ),
            57.0,
        ),
        (
            _metric(
                allow, TargetResource=GW, OperationName="AuthorizeAction",
                Mode=mode, Policy=POLICY,
            ),
            57.0,
        ),
        # DenyDecisions exists ONLY as a per-tool PartiallyAuthorizeActions stream:
        # no gateway-level and no Policy projection. A single fixed projection
        # would silently report zero denials.
        (
            _metric(
                deny, TargetResource=GW, OperationName="PartiallyAuthorizeActions",
                Mode=mode, ToolName="hr-database___create_payout",
            ),
            64.0,
        ),
        (
            _metric(
                deny, TargetResource=GW, OperationName="PartiallyAuthorizeActions",
                ToolName="hr-database___create_payout",
            ),
            64.0,
        ),
        # Another gateway's streams must never leak in.
        (_metric(allow, TargetResource="launchpad-kb-gw-pmyq7mchum",
                 OperationName="AuthorizeAction", Mode=mode), 999.0),
    ]
    for tool in TOOLS:
        streams.append(
            (
                _metric(
                    allow, TargetResource=GW, OperationName="PartiallyAuthorizeActions",
                    Mode=mode, ToolName=tool,
                ),
                116.0,
            )
        )
        streams.append(
            (
                _metric(
                    allow, TargetResource=GW, OperationName="PartiallyAuthorizeActions",
                    ToolName=tool,
                ),
                116.0,
            )
        )
    return streams


def test_overlapping_projections_are_not_double_counted():
    cw = FakeCW(_realistic_streams())
    result = ge.gateway_decisions(_control(), cw, GW, "7d")

    assert result["available"] is True
    # AuthorizeAction 57 (per call) + PartiallyAuthorizeActions 5x116 (per tool)
    assert result["totals"]["allow"] == 57 + 5 * 116
    assert result["totals"]["deny"] == 64
    assert result["evidence_count"] == 57 + 580 + 64

    by_operation = {r["operation"]: r for r in result["by_operation"]}
    assert by_operation["AuthorizeAction"] == {
        "operation": "AuthorizeAction", "allow": 57, "deny": 0, "basis": "per_call",
    }
    # Deny only exists per-tool, so the operation's number is tool-level.
    assert by_operation["PartiallyAuthorizeActions"]["basis"] == "per_tool"
    assert by_operation["PartiallyAuthorizeActions"]["deny"] == 64


def test_deny_only_published_per_tool_is_still_counted():
    """Regression: a fixed {TargetResource,OperationName,Mode} projection finds no
    DenyDecisions stream on this gateway and would report zero denials."""
    cw = FakeCW(_realistic_streams())
    result = ge.gateway_decisions(_control(), cw, GW, "7d")
    assert result["totals"]["deny"] == 64
    assert {r["tool"] for r in result["by_tool"]} >= {"hr-database___create_payout"}


def test_other_gateway_streams_are_excluded():
    cw = FakeCW(_realistic_streams())
    result = ge.gateway_decisions(_control(), cw, GW, "7d")
    assert 999 not in (result["totals"]["allow"], result["totals"]["deny"])
    assert result["evidence_count"] == 701


def test_breakdowns_are_not_required_to_partition_the_total():
    """by_policy covers only decisions that had a determining policy — it is a
    breakdown, not a decomposition, and must not be asserted to sum to the total."""
    cw = FakeCW(_realistic_streams())
    result = ge.gateway_decisions(_control(), cw, GW, "7d")
    by_policy_total = sum(r["allow"] + r["deny"] for r in result["by_policy"])
    assert by_policy_total == 57
    assert by_policy_total < result["evidence_count"]


def test_zero_evidence_in_window_is_available_not_unavailable():
    """Streams exist but reported no datapoints in the window — a quiet account,
    which is a different state from an unreadable channel."""
    streams = [(m, None) for m, _ in _realistic_streams()]
    cw = FakeCW([(m, 0.0) for m, _ in streams])
    cw.totals = {}  # every stream returns an empty Values list
    result = ge.gateway_decisions(_control(), cw, GW, "24h")

    assert result["available"] is True
    assert result["unavailable_reason"] is None
    assert result["evidence_count"] == 0


def test_unreadable_channel_reports_the_aws_error_code():
    class Denied(FakeCW):
        def get_metric_data(self, MetricDataQueries, StartTime, EndTime):  # noqa: N803
            raise ClientError(
                {"Error": {"Code": "AccessDeniedException", "Message": "no"}},
                "GetMetricData",
            )

    result = ge.gateway_decisions(_control(), Denied(_realistic_streams()), GW, "24h")
    assert result["available"] is False
    assert result["unavailable_reason"] == "AccessDeniedException"
    assert result["evidence_count"] == 0
    assert result["decisions"] == []


def test_policy_filter_narrows_and_flags_unattributable_operations():
    cw = FakeCW(_realistic_streams())
    result = ge.gateway_decisions(_control(), cw, GW, "7d", policy_id=POLICY)

    assert result["evidence_count"] == 57
    # PartiallyAuthorizeActions publishes no Policy dimension here, so its
    # decisions cannot be attributed — the response must say so.
    assert result["policy_filter_partial"] is True

    unknown = ge.gateway_decisions(_control(), cw, GW, "7d", policy_id="not-a-policy")
    assert unknown["evidence_count"] == 0


def test_decisions_stay_empty_and_are_never_synthesized():
    cw = FakeCW(_realistic_streams())
    result = ge.gateway_decisions(_control(), cw, GW, "7d")
    assert result["decisions"] == []
    assert result["count"] == 0
    assert result["source"] == "metrics"


def test_cache_hit_then_force_bypass():
    cw = FakeCW(_realistic_streams())
    first = ge.gateway_decisions(_control(), cw, GW, "7d")
    calls_after_first = cw.get_metric_data_calls
    assert first["cache"]["hit"] is False

    second = ge.gateway_decisions(_control(), cw, GW, "7d")
    assert second["cache"]["hit"] is True
    assert cw.get_metric_data_calls == calls_after_first

    third = ge.gateway_decisions(_control(), cw, GW, "7d", force=True)
    assert third["cache"]["hit"] is False
    assert cw.get_metric_data_calls > calls_after_first


def test_unknown_range_falls_back_to_24h():
    cw = FakeCW(_realistic_streams())
    result = ge.gateway_decisions(_control(), cw, GW, "nonsense")
    assert result["range"] == "24h"


def test_evidence_count_helper_counts_log_only_mode_only():
    enforce_only = FakeCW(_realistic_streams(mode="ENFORCE"))
    assert ge.evidence_count(enforce_only, GW) == 0
    assert ge.evidence_count(enforce_only, GW, log_only=False) == 701

    log_only = FakeCW(_realistic_streams(mode="LOG_ONLY"))
    assert ge.evidence_count(log_only, GW) == 701


def test_evidence_count_helper_never_raises():
    class Broken(FakeCW):
        def get_paginator(self, name: str):
            raise RuntimeError("boom")

    assert ge.evidence_count(Broken([]), GW) == 0


# ── merging the span channel ────────────────────────────────────────────────


def _with_span_rows(monkeypatch, rows=None, reason=None, raises=False):
    from app.services import governance_spans

    def fake(logs, gateway_arn, range_key, policy_id=None):
        if raises:
            from app.core.errors import AppError

            raise AppError("observability.query_failed", "boom", status_code=502)
        return {
            "decisions": rows if rows is not None else [],
            "unavailable_reason": reason,
            "truncated": False,
        }

    monkeypatch.setattr(governance_spans, "gateway_decision_rows", fake)


def test_span_rows_are_merged_without_redefining_evidence_count(monkeypatch):
    """Spans add detail; the gate's number stays metric-derived because spans are
    sampled while metrics are exact."""
    _with_span_rows(
        monkeypatch,
        rows=[{"action": "hr-database___list_departments", "outcome": "ALLOW"}],
    )
    monkeypatch.setattr(
        "app.services.governance._require_gateway",
        lambda control, gateway_id: {"gatewayId": gateway_id, "gatewayArn": "arn:gw"},
    )
    cw = FakeCW(_realistic_streams())
    result = ge.gateway_decisions(_control(), cw, GW, "7d", logs=object())

    assert result["source"] == "metrics+spans"
    assert result["count"] == 1 == len(result["decisions"])
    assert result["evidence_count"] == 701  # unchanged by the span merge
    assert result["spans_unavailable_reason"] is None


def test_span_failure_degrades_to_metrics_only(monkeypatch):
    """A Logs Insights outage must not take the endpoint down — the aggregates are
    what the cutover gate reads."""
    _with_span_rows(monkeypatch, raises=True)
    monkeypatch.setattr(
        "app.services.governance._require_gateway",
        lambda control, gateway_id: {"gatewayId": gateway_id, "gatewayArn": "arn:gw"},
    )
    cw = FakeCW(_realistic_streams())
    result = ge.gateway_decisions(_control(), cw, GW, "7d", logs=object())

    assert result["decisions"] == [] and result["count"] == 0
    assert result["spans_unavailable_reason"] == "observability.query_failed"
    assert result["source"] == "metrics"
    assert result["evidence_count"] == 701  # aggregates intact


def test_no_gateway_arn_skips_the_span_read(monkeypatch):
    called = {"n": 0}
    from app.services import governance_spans

    def fake(*args, **kwargs):
        called["n"] += 1
        return {"decisions": [], "unavailable_reason": None, "truncated": False}

    monkeypatch.setattr(governance_spans, "gateway_decision_rows", fake)
    monkeypatch.setattr(
        "app.services.governance._require_gateway",
        lambda control, gateway_id: {"gatewayId": gateway_id},  # no gatewayArn
    )
    result = ge.gateway_decisions(_control(), FakeCW(_realistic_streams()), GW, "7d")
    assert called["n"] == 0
    assert result["decisions"] == []


# ── the evidence / override gate ────────────────────────────────────────────


def test_gate_admits_when_evidence_exists_and_refuses_when_it_does_not():
    """`_assert_evidence_or_override` is unchanged; what changed is that it now
    receives a real count. Both directions must still hold."""
    from app.core.errors import AppError
    from app.services.governance import _assert_evidence_or_override

    gateway = {"name": "launchpad-gw"}

    _assert_evidence_or_override(gateway, {"evidence_count": 701})

    with pytest.raises(AppError) as caught:
        _assert_evidence_or_override(gateway, {"evidence_count": 0})
    assert caught.value.code == "governance.evidence_required"

    # zero evidence is still overridable with the typed confirmation
    _assert_evidence_or_override(
        gateway,
        {
            "evidence_count": 0,
            "confirmation_name": "launchpad-gw",
            "override_reason": "cutting over ahead of traffic",
        },
    )


@pytest.mark.parametrize(
    ("path", "queue_fn"),
    [
        ("policies/p-1/promote", "queue_policy_transition"),
        ("policies/p-1/rollback", "queue_policy_transition"),
        ("mode", "queue_gateway_mode"),
    ],
)
def test_mutation_routes_pass_a_real_evidence_count(client, monkeypatch, path, queue_fn):
    """Regression for the hardcoded `evidence_count=0` that forced every cutover
    through the zero-evidence override."""
    import app.routers.governance as gov

    seen: dict[str, Any] = {}

    def fake_queue(*args, **kwargs):
        seen.update(kwargs)
        return {"id": "0" * 32, "status": "queued"}

    monkeypatch.setattr(gov.governance_service, queue_fn, fake_queue)
    monkeypatch.setattr(gov.governance_service, "run_policy_change", lambda _id: None)
    monkeypatch.setattr(gov, "control_client", lambda: _control())
    monkeypatch.setattr(gov, "iam_client", lambda: _control())
    monkeypatch.setattr(gov, "cw_client", lambda: FakeCW(_realistic_streams(mode="LOG_ONLY")))

    body: dict[str, Any] = {
        "expected_gateway_updated_at": "2026-07-09T12:48:59Z",
        "expected_policy_updated_at": "2026-07-09T12:48:59Z",
        "evidence_range": "7d",
    }
    if path == "mode":
        body["mode"] = "ENFORCE"
    res = client.post(f"/api/governance/gateways/{GW}/{path}", json=body)

    assert res.status_code == 202, res.text
    assert seen["evidence_count"] == 701
