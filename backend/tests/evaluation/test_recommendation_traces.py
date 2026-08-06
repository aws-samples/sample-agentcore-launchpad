"""`agentTraces` selection for recommendation jobs (default window vs pinned batch)."""

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest

from app.evaluation import agentcore_eval as ac

LOG_GROUPS = ["arn:aws:logs:us-west-2:1:log-group:/aws/bedrock-agentcore/runtimes/x:*"]
SERVICES = ["x.DEFAULT"]
BATCH_ARN = "arn:aws:bedrock-agentcore:us-west-2:1:batch-evaluation/insights-abc"


def test_default_window_shape_is_unchanged():
    """Every existing experiment reads this payload — a change here silently
    alters what all of them analyse."""
    traces = ac.recommendation_traces(
        log_group_arns=LOG_GROUPS, service_names=SERVICES
    )
    assert set(traces) == {"cloudwatchLogs"}
    cw = traces["cloudwatchLogs"]
    assert set(cw) == {"logGroupArns", "serviceNames", "startTime", "endTime"}
    assert cw["logGroupArns"] == LOG_GROUPS
    assert cw["serviceNames"] == SERVICES
    assert ac.RECOMMEND_LOOKBACK_DAYS == 7
    span = cw["endTime"] - cw["startTime"]
    assert span == timedelta(days=7)
    assert cw["endTime"] <= datetime.now(UTC)


def test_a_batch_arn_replaces_the_window_entirely():
    """Not merged: the union has one branch, and leaving cloudwatchLogs in would
    widen the input right back past the job's own session set."""
    traces = ac.recommendation_traces(
        log_group_arns=LOG_GROUPS,
        service_names=SERVICES,
        batch_evaluation_arn=BATCH_ARN,
    )
    assert traces == {"batchEvaluation": {"batchEvaluationArn": BATCH_ARN}}


def test_neither_source_is_a_programming_error():
    """An empty window fails server-side with a far less obvious message."""
    with pytest.raises(ValueError, match="batch_evaluation_arn"):
        ac.recommendation_traces()
    with pytest.raises(ValueError):
        ac.recommendation_traces(log_group_arns=LOG_GROUPS)  # no service names
    with pytest.raises(ValueError):
        ac.recommendation_traces(service_names=SERVICES)  # no log groups


def _sent(client: MagicMock) -> dict:
    return client.start_recommendation.call_args.kwargs


def test_system_prompt_job_honours_a_pinned_batch():
    client = MagicMock()
    ac.start_system_prompt_recommendation(
        client, name="n", system_prompt="p", batch_evaluation_arn=BATCH_ARN
    )
    cfg = _sent(client)["recommendationConfig"]["systemPromptRecommendationConfig"]
    assert cfg["agentTraces"] == {"batchEvaluation": {"batchEvaluationArn": BATCH_ARN}}
    # the evaluator stays hardcoded — explicitly out of scope for this change
    assert cfg["evaluationConfig"]["evaluators"][0]["evaluatorArn"].endswith(
        "Builtin.GoalSuccessRate"
    )


def test_tool_description_job_honours_a_pinned_batch():
    """Both generators, or one RECOMMEND would read two different trace sets and
    "only that job's sessions" would be false."""
    client = MagicMock()
    ac.start_tool_description_recommendation(
        client,
        name="n",
        tools=[{"toolName": "get_pay_stub", "description": "d"}],
        batch_evaluation_arn=BATCH_ARN,
    )
    cfg = _sent(client)["recommendationConfig"]["toolDescriptionRecommendationConfig"]
    assert cfg["agentTraces"] == {"batchEvaluation": {"batchEvaluationArn": BATCH_ARN}}


@pytest.mark.parametrize(
    "start",
    [ac.start_system_prompt_recommendation, ac.start_tool_description_recommendation],
)
def test_both_jobs_default_to_the_window(start):
    client = MagicMock()
    kwargs = (
        {"system_prompt": "p"}
        if start is ac.start_system_prompt_recommendation
        else {"tools": [{"toolName": "t", "description": "d"}]}
    )
    start(client, name="n", log_group_arns=LOG_GROUPS, service_names=SERVICES, **kwargs)
    config = next(iter(_sent(client)["recommendationConfig"].values()))
    assert "cloudwatchLogs" in config["agentTraces"]
    assert "batchEvaluation" not in config["agentTraces"]
