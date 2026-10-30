"""N17-obs449 — insight failure analysis never reads a broken detection as "no failures".

strands-evals' failure detector calls the judge's raw ``stream`` in text mode and parses the
text: a reply cut at max_tokens (nothing raises on that path) or one that does not parse
became an EMPTY failure list with only a log warning, and a low-confidence finding is dropped
by the threshold. Insights runs 9913db33ebf2 / 2a6a1e2bb9dd (hr-eval-demo, GLM-5.3 judge at
a 4096-token budget) reported 0 failure clusters over 40 sessions with no error. Now:
a max_tokens stop retries at a higher budget, a still-truncated or unparseable reply is the
session's error row (``kind``), and ``failureDetection`` counts sessions / incomplete / low
findings dropped — so 0 clusters can only mean 0 detected. These are the investigation's
scripted-model probes as tests.
"""

from __future__ import annotations

import json

import pytest

from app.core.db import DEFAULT_WORKSPACE_ID
from app.services.strands_eval import insights
from tests.conftest import ws_ctx
from tests.evaluation.fakes import FakeJudge
from tests.test_strands_engine import FAILURE_JSON, RCA_JSON, SID, engine_with

FA = [insights.FAILURE_ANALYSIS]
LOW_JSON = FAILURE_JSON.replace('"high"', '"low"')


@pytest.fixture
def ws():
    return ws_ctx(id=DEFAULT_WORKSPACE_ID, region="cn-northwest-1")


class _Judge(FakeJudge):
    """FakeJudge whose free-form (detector) replies stop at max_tokens while its budget is
    below ``full_at`` — the reply is then the first ``cut`` characters only."""

    def __init__(self, text, *, full_at=None, cut=40, budget=4096):
        super().__init__(text=text, by_tool={"RCAStructuredOutput": json.loads(RCA_JSON)},
                         model_id="global.zai.glm-5.3")
        self.full_at, self.cut = full_at, cut
        self.update_config(max_tokens=budget)
        self.budgets: list[int] = []

    async def stream(self, messages, tool_specs=None, system_prompt=None, **kwargs):
        budget = int(self.get_config().get("max_tokens") or 0)
        truncated = not tool_specs and self.full_at is not None and budget < self.full_at
        if not tool_specs:
            self.budgets.append(budget)
        async for event in super().stream(messages, tool_specs, system_prompt, **kwargs):
            if truncated and "contentBlockDelta" in event:
                text = event["contentBlockDelta"]["delta"].get("text", "")
                event = {"contentBlockDelta": {"delta": {"text": text[:self.cut]}}}
            if truncated and "messageStop" in event:
                event = {"messageStop": {"stopReason": "max_tokens"}}
            yield event


def _diagnose(ws, judge):
    return engine_with(judge=judge).diagnose_sessions(
        ws, session_ids=[SID], log_groups=["/aws/bedrock-agentcore/runtimes/x-DEFAULT"],
        kinds=FA, judge_model_id=None)


def test_a_valid_reply_is_clustered_and_counted(ws):
    out = _diagnose(ws, _Judge(FAILURE_JSON))
    assert [c["name"] for c in out["failures"]] == ["Tool misuse"]
    assert "errors" not in out
    assert out["failureDetection"] == [{"sessions": 1, "incomplete": 0, "lowConfidenceDropped": 0,
                                        "threshold": "medium",
                                          "judge": "global.zai.glm-5.3"}]


def test_the_detector_starts_at_its_own_budget_so_a_long_reasoning_reply_needs_one_call(ws):
    """N17-obs449: GLM-5.3 spent the 4096 judge default inside its reasoning block on 40/40
    live sessions; the detector starts at DETECTION_START_BUDGET (16384, bounded by the cap)
    instead of paying for the 4096 / 8192 attempts first."""
    judge = _Judge(FAILURE_JSON, full_at=8192)
    out = _diagnose(ws, judge)
    assert judge.budgets == [insights.DETECTION_START_BUDGET] == [16384]
    assert [c["name"] for c in out["failures"]] == ["Tool misuse"]
    assert out["failureDetection"][0]["incomplete"] == 0
    assert judge.get_config()["max_tokens"] == 4096  # the judge's own budget is restored


def test_a_still_truncated_reply_is_an_error_row_not_no_failures(ws):
    judge = _Judge(FAILURE_JSON, full_at=10**9)
    out = _diagnose(ws, judge)
    assert out["failures"] == []
    (row,) = out["errors"]
    assert row["session_id"] == SID and row["kind"] == insights.DETECTION_TRUNCATED
    assert "JudgeBudgetExceeded" in row["error"]
    assert out["failureDetection"][0] == {"sessions": 1, "incomplete": 1,
                                          "lowConfidenceDropped": 0, "threshold": "medium",
                                          "judge": "global.zai.glm-5.3"}


def test_an_empty_reply_spent_on_reasoning_is_an_error_row(ws):
    judge = _Judge(FAILURE_JSON, full_at=10**9, cut=0)
    out = _diagnose(ws, judge)
    assert out["failures"] == [] and out["errors"][0]["kind"] == insights.DETECTION_TRUNCATED


def test_an_empty_end_turn_is_an_empty_error_row(ws):
    out = _diagnose(ws, _Judge(""))
    assert out["failures"] == []
    (row,) = out["errors"]
    assert row["kind"] == insights.DETECTION_EMPTY and "FailureDetectionStopped" in row["error"]


@pytest.mark.parametrize("reply", ["I found no JSON-worthy problems.", '{"failures": []}'])
def test_an_unparseable_reply_is_an_error_row(ws, reply):
    out = _diagnose(ws, _Judge(reply))
    assert out["failures"] == []
    (row,) = out["errors"]
    assert row["kind"] == insights.DETECTION_UNPARSEABLE
    assert "FailureDetectionUnparseable" in row["error"]
    assert out["failureDetection"][0]["incomplete"] == 1


def test_no_failures_is_still_no_failures(ws):
    out = _diagnose(ws, _Judge(json.dumps({"errors": []})))
    assert out["failures"] == [] and "errors" not in out
    assert out["failureDetection"][0] == {"sessions": 1, "incomplete": 0,
                                          "lowConfidenceDropped": 0, "threshold": "medium",
                                          "judge": "global.zai.glm-5.3"}


def test_low_confidence_findings_are_dropped_as_before_and_counted(ws):
    out = _diagnose(ws, _Judge(LOW_JSON))
    assert out["failures"] == [] and "errors" not in out  # the threshold is unchanged
    assert out["failureDetection"][0]["lowConfidenceDropped"] == 1


def test_the_judge_stream_is_restored_after_each_diagnosis(ws):
    judge = _Judge(FAILURE_JSON)
    before = judge.stream
    _diagnose(ws, judge)
    assert judge.stream == before
