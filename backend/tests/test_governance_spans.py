"""Policy decision rows parsed from aws/spans.

The span fixtures below are copied **verbatim** from the captured corpus in
`.trellis/tasks/07-29-policy-span-detail/research/policy-span-corpus.md`, including
the two undocumented attributes. They are not hand-simplified: the real attribute
set is what the parser has to survive.
"""

import ast
import json
from typing import Any

import pytest

from app.core.errors import AppError
from app.services import governance_spans as gs

GW_ID = "launchpad-gw-em0yuqmmdp"
GW_ARN = f"arn:aws:bedrock-agentcore:us-west-2:434444145045:gateway/{GW_ID}"
ENGINE_ARN = (
    "arn:aws:bedrock-agentcore:us-west-2:434444145045:policy-engine/launchpad_pe-rwtcceczvs"
)
TRACE = "6a6a005492b3acc3e318e0f22ae9909e"
SESSION = "f3f6cd2ec7f44768849c00e1847c5f481c3b552a3d174327a2da4748de9e1da5"
BASELINE_POLICY = "launchpad_baseline_allow-obafj1o9hj"
LOG_ONLY_POLICY = "lab_readonly_tools-be45dja2_p"

INVOKE_TOOL_SPAN = {
    "name": "AgentCore.Gateway.InvokeTool",
    "kind": "SERVER",
    "traceId": TRACE,
    "spanId": "08b21d1592a9b9aa",
    "parentSpanId": "b2c1f80f6719f359",
    "startTimeUnixNano": 1785331800758000000,
    "attributes": {
        "PlatformType": "AWS::BedrockAgentCore",
        "aws.agentcore.gateway.policy.mode": "ENFORCE",
        "aws.agentcore.policy.authorization_decision": "ALLOW",
        "aws.resource.arn": GW_ARN,
        "aws.resource.type": "AWS::BedrockAgentCore::Gateway",
        "execute_tool_latency_ms": 289,
        "gateway.id": GW_ID,
        "gateway.name": "launchpad-gw",
        "http.response.status_code": 200,
        "tool.name": "hr-database___list_departments",
    },
}

AUTHORIZE_SPAN = {
    "name": "AgentCore.Policy.AuthorizeAction",
    "kind": "CLIENT",
    "traceId": TRACE,
    "spanId": "e94f9481b9a43495",
    "parentSpanId": "08b21d1592a9b9aa",
    "startTimeUnixNano": 1785331800758000000,
    "attributes": {
        "aws.agentcore.gateway.policy.arn": ENGINE_ARN,
        "aws.agentcore.gateway.policy.mode": "ENFORCE",
        "aws.agentcore.policy.authorization_decision": "ALLOW",
        "aws.agentcore.policy.determining_policies": [BASELINE_POLICY],
        "aws.agentcore.policy.log_only_matched_policies": [LOG_ONLY_POLICY],
        "aws.agentcore.policy.mismatched_policies": [],
        "aws.agentcore.policy.target_resource.id": GW_ID,
        "aws.agentcore.policy.types": [[BASELINE_POLICY, "Cedar"]],
        "aws.remote.operation": "AuthorizeAction",
        "aws.resource.arn": GW_ARN,
    },
}

PARTIAL_SPAN = {
    "name": "AgentCore.Policy.PartiallyAuthorizeActions",
    "kind": "CLIENT",
    "traceId": TRACE,
    "spanId": "9ca47dd6a8e67249",
    "parentSpanId": "857a1f35baff7a19",
    "startTimeUnixNano": 1785331797355000000,
    "attributes": {
        "aws.agentcore.gateway.policy.arn": ENGINE_ARN,
        "aws.agentcore.gateway.policy.mode": "ENFORCE",
        "aws.agentcore.policy.allowed_tools": [
            "hr-database___check_calendar",
            "hr-database___get_employee",
            "hr-database___list_departments",
            "office-facts___get_office_fact",
            "office-facts___list_office_topics",
        ],
        "aws.agentcore.policy.denied_tools": ["hr-database___create_payout"],
        "aws.agentcore.policy.target_resource.id": GW_ID,
        "aws.remote.operation": "PartiallyAuthorizeActions",
        "aws.resource.arn": GW_ARN,
    },
}

ALL_SPANS = [INVOKE_TOOL_SPAN, AUTHORIZE_SPAN, PARTIAL_SPAN]


class FakeInsights:
    """Stands in for observability.run_insights_queries."""

    def __init__(
        self,
        spans: list[dict[str, Any]] | None = None,
        sessions: list[dict[str, str]] | None = None,
        *,
        fail_first: bool = False,
        fail_second: bool = False,
    ) -> None:
        self.spans = spans if spans is not None else ALL_SPANS
        self.sessions = sessions if sessions is not None else [
            {"traceId": TRACE, "attributes.session.id": SESSION}
        ]
        self.fail_first = fail_first
        self.fail_second = fail_second
        self.queries: list[str] = []

    def __call__(self, queries, hours, logs=None, log_groups=None):
        name, query = next(iter(queries.items()))
        self.queries.append(query)
        if name == "decisions":
            if self.fail_first:
                raise AppError("observability.query_failed", "boom", status_code=502)
            return {"decisions": [{"@message": json.dumps(s)} for s in self.spans]}
        if self.fail_second:
            raise AppError("observability.query_failed", "boom", status_code=502)
        return {"sessions": self.sessions}


@pytest.fixture
def insights(monkeypatch):
    fake = FakeInsights()
    monkeypatch.setattr(gs, "run_insights_queries", fake)
    return fake


def _rows(range_key: str = "7d", policy_id: str | None = None) -> dict[str, Any]:
    return gs.gateway_decision_rows(object(), GW_ARN, range_key, policy_id)


def test_invocation_row_from_gateway_span_joined_with_policy_span(insights):
    result = _rows()
    invocation = [r for r in result["decisions"] if r["evaluation"] == "invocation"]
    assert len(invocation) == 1
    row = invocation[0]

    # action + outcome come from the Gateway span alone
    assert row["action"] == "hr-database___list_departments"
    assert row["outcome"] == "ALLOW"
    assert row["engine_mode"] == "ENFORCE"
    # policy ids come from the child Policy span, joined on parentSpanId
    assert row["policy_id"] == BASELINE_POLICY
    assert row["determining_policies"] == [BASELINE_POLICY]
    assert row["mismatched_policies"] == []
    assert row["log_only_matched_policies"] == [LOG_ONLY_POLICY]
    assert row["engine_id"] == "launchpad_pe-rwtcceczvs"
    assert row["gateway_id"] == GW_ID
    assert row["trace_id"] == TRACE
    assert row["span_id"] == "08b21d1592a9b9aa"
    assert row["at"] is not None


def test_denied_tools_become_listing_rows_and_allowed_tools_do_not(insights):
    result = _rows()
    listing = [r for r in result["decisions"] if r["evaluation"] == "tool_listing"]
    assert [r["action"] for r in listing] == ["hr-database___create_payout"]
    assert all(r["outcome"] == "DENY" for r in listing)

    # the five allowed tools must NOT produce rows
    actions = {r["action"] for r in result["decisions"]}
    assert "hr-database___check_calendar" not in actions
    assert "office-facts___get_office_fact" not in actions
    assert len(result["decisions"]) == 2  # one invocation + one denied tool


def test_session_id_joined_by_trace_id(insights):
    result = _rows()
    assert {r["session_id"] for r in result["decisions"]} == {SESSION}
    assert len(insights.queries) == 2
    assert TRACE in insights.queries[1]


def test_principal_and_policy_mode_are_always_absent(insights):
    """Structural, not a gap to fill later: M2M auth carries no human principal,
    and the span only has the Gateway attachment mode."""
    for row in _rows()["decisions"]:
        assert row["principal"] is None
        assert row["policy_mode"] is None


def test_parser_never_references_the_unverified_reason_attribute(insights):
    """`aws.agentcore.policy.authorization_reason` is documented by AWS but was
    absent from the captured span. Referencing it would be the documented-but-
    unverified mistake the research gate exists to prevent. The module docstring may
    explain that; the code may not mention it."""
    source = open(gs.__file__).read()
    tree = ast.parse(source)
    # clean=False so the value matches the source text byte for byte; the cleaned
    # form is dedented/stripped and would fail to excise.
    docstring = ast.get_docstring(tree, clean=False) or ""
    code_only = source.replace(docstring, "", 1)
    assert docstring and docstring not in code_only, "docstring was not excised"
    assert "authorization_reason" not in code_only

    for row in _rows()["decisions"]:
        assert "reason" not in row


def test_no_spans_means_no_session_query(monkeypatch):
    fake = FakeInsights(spans=[])
    monkeypatch.setattr(gs, "run_insights_queries", fake)
    result = gs.gateway_decision_rows(object(), GW_ARN, "24h")
    assert result["decisions"] == []
    assert result["unavailable_reason"] is None
    assert len(fake.queries) == 1  # second pass skipped entirely


def test_query_failure_reports_reason_without_raising(monkeypatch):
    fake = FakeInsights(fail_first=True)
    monkeypatch.setattr(gs, "run_insights_queries", fake)
    result = gs.gateway_decision_rows(object(), GW_ARN, "24h")
    assert result["decisions"] == []
    assert result["unavailable_reason"] == "observability.query_failed"


def test_session_failure_still_returns_rows(monkeypatch):
    fake = FakeInsights(fail_second=True)
    monkeypatch.setattr(gs, "run_insights_queries", fake)
    result = gs.gateway_decision_rows(object(), GW_ARN, "24h")
    assert len(result["decisions"]) == 2
    assert all(r["session_id"] is None for r in result["decisions"])
    assert result["unavailable_reason"] is None


def test_policy_filter_narrows_by_determining_policies(insights):
    kept = _rows(policy_id=BASELINE_POLICY)["decisions"]
    assert [r["evaluation"] for r in kept] == ["invocation"]

    assert _rows(policy_id="not-a-policy")["decisions"] == []


def test_unknown_range_falls_back_to_24h(insights):
    _rows(range_key="nonsense")
    assert insights.queries  # did not raise; hours defaulted


def test_malformed_span_message_is_skipped(monkeypatch):
    class Broken(FakeInsights):
        def __call__(self, queries, hours, logs=None, log_groups=None):
            name = next(iter(queries))
            if name == "decisions":
                return {"decisions": [{"@message": "not json"}, {"nope": "1"}]}
            return {"sessions": []}

    monkeypatch.setattr(gs, "run_insights_queries", Broken())
    assert gs.gateway_decision_rows(object(), GW_ARN, "24h")["decisions"] == []
