# ruff: noqa: F811 — pytest fixtures are imported explicitly from test_assistant
"""``submit_proposal``: the model hands Launchpad its proposal mid-reply through an
inline function, gets the platform's own verdict back, and corrects it in the same
reply — the member receives a proposal that already validates."""

import json

import pytest

from app.assistant import proposal as contract
from app.assistant import service, submission
from app.core.errors import AppError
from app.services.agentcore import harness as hc

from .test_assistant import (  # noqa: F401 — shared fixtures, including autouse guards
    CATALOG,
    VALID_PROPOSAL,
    FakeStream,
    _block,
    _latest,
    _open,
    _turn,
    harness,
    no_aws_clients,
    no_network,
    no_real_deploy,
    ready,
)

_REAL_OVERRIDES = service.harness_tool_overrides  # before the fixture stubs it
PRESET_TOOLS = [{"type": "remote_mcp", "name": "aws_knowledge",
                 "config": {"remoteMcp": {"url": "https://knowledge-mcp.global.api.aws"}}}]
PRESET_ALLOWED = ["shell", "file_*", "@aws_knowledge"]


def _validator(raw):
    _content, display, errors = contract.validate(raw, CATALOG)
    return display, errors


def _subs(base_revision=None, base_content=None) -> submission.Submissions:
    return submission.Submissions(_validator, base_revision=base_revision,
                                  base_content=base_content, candidate_revision=7)


def _call(tool_id: str, payload) -> list[dict]:
    body = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    return [
        {"contentBlockStart": {"contentBlockIndex": 0, "start": {
            "toolUse": {"name": submission.TOOL_NAME, "toolUseId": tool_id}}}},
        {"contentBlockDelta": {"contentBlockIndex": 0,
                               "delta": {"toolUse": {"input": body[: len(body) // 2]}}}},
        {"contentBlockDelta": {"contentBlockIndex": 0,
                               "delta": {"toolUse": {"input": body[len(body) // 2:]}}}},
        {"contentBlockStop": {"contentBlockIndex": 0}},
        {"messageStop": {"stopReason": "tool_use"}},
    ]


def _text(text: str) -> list[dict]:
    return [{"contentBlockDelta": {"delta": {"text": text}}},
            {"messageStop": {"stopReason": "end_turn"}}]


@pytest.fixture
def offered(monkeypatch):
    """The preset's read-back tools, as a turn that offers the inline tool gets them."""
    seen = []

    def overrides(workspace, agent):
        seen.append(agent.name)
        return ([*PRESET_TOOLS, submission.TOOL], [*PRESET_ALLOWED, submission.ALLOWED_PATTERN])

    monkeypatch.setattr(service, "harness_tool_overrides", overrides)
    return seen


# ---------------------------------------------------------------------------
# the per-turn contract
# ---------------------------------------------------------------------------


def test_a_valid_proposal_is_accepted_and_becomes_the_candidate():
    subs = _subs()
    result = subs.handle(json.dumps({"proposal": VALID_PROPOSAL}))
    assert result["status"] == "accepted" and result["candidate_revision"] == 7
    assert subs.candidate is not None and subs.candidate["name"] == "hr-helpdesk"


def test_a_rejected_submission_is_corrected_against_the_candidate():
    subs = _subs()
    rejected = subs.handle(json.dumps({"proposal": {**VALID_PROPOSAL, "name": "Bad Name!"}}))
    assert rejected["status"] == "rejected" and rejected["errors"]
    assert rejected["candidate_revision"] == 7 and "base_revision is 7" in rejected["next"]
    fixed = subs.handle(json.dumps({"change": {"base_revision": 7, "operations": [
        {"op": "replace", "path": "/name", "value": "hr-helpdesk"}]}}))
    assert fixed["status"] == "accepted"
    assert subs.candidate is not None and subs.candidate["name"] == "hr-helpdesk"


def test_a_change_can_target_the_stored_base():
    subs = _subs(base_revision=3, base_content=dict(VALID_PROPOSAL))
    result = subs.handle(json.dumps({"change": {"base_revision": 3, "operations": [
        {"op": "replace", "path": "/summary", "value": "Revised."}]}}))
    assert result["status"] == "accepted"
    assert subs.candidate is not None and subs.candidate["summary"] == "Revised."
    wrong = _subs(base_revision=3, base_content=dict(VALID_PROPOSAL)).handle(json.dumps(
        {"change": {"base_revision": 2, "operations": [{"op": "remove", "path": "/summary"}]}}))
    assert wrong["status"] == "rejected" and "revision 3" in wrong["errors"][0]


@pytest.mark.parametrize("raw, needle", [
    ("{nope", "not valid JSON"),
    (json.dumps({"proposal": VALID_PROPOSAL, "change": {}}), "exactly one"),
    (json.dumps({"other": 1}), "exactly one"),
    (json.dumps({"change": {"base_revision": 1, "operations": []}}), "no stored proposal"),
])
def test_malformed_calls_are_rejected_without_a_candidate(raw, needle):
    subs = _subs()
    result = subs.handle(raw)
    assert result["status"] == "rejected" and needle in result["errors"][0]
    assert subs.candidate is None


def test_submissions_per_reply_are_bounded():
    subs = _subs()
    for _ in range(submission.MAX_SUBMISSIONS):
        subs.handle("{nope")
    assert "limit reached" in subs.handle(json.dumps({"proposal": VALID_PROPOSAL}))["errors"][0]


def test_the_transcript_summary_never_carries_the_json():
    assert submission.input_summary(json.dumps({"proposal": VALID_PROPOSAL})) == (
        "proposal hr-helpdesk")
    change = {"change": {"base_revision": 2, "operations": [{}, {}, {}]}}
    assert submission.input_summary(json.dumps(change)) == "proposal revision (3 edits)"
    assert "patch" not in submission.input_summary(json.dumps(change))


# ---------------------------------------------------------------------------
# the harness handoff
# ---------------------------------------------------------------------------


class _Client:
    def __init__(self, events):
        self.events = events

    def invoke_harness(self, **kwargs):
        return {"stream": FakeStream(self.events)}


def test_a_tool_use_stop_on_an_inline_tool_is_a_handoff_with_the_complete_input():
    events = list(hc.invoke_harness_events(
        _Client(_call("s-1", {"proposal": VALID_PROPOSAL})), "arn", [],
        session_id="s", actor_id="a", inline_tools=frozenset({submission.TOOL_NAME})))
    assert [e["event"] for e in events] == ["tool", "tool_input", "handoff"]
    (call,) = events[-1]["data"]["calls"]
    assert call["id"] == "s-1" and json.loads(call["input"])["proposal"] == VALID_PROPOSAL


def test_without_inline_tools_a_tool_use_stop_stays_an_incomplete_response():
    with pytest.raises(AppError) as err:
        list(hc.invoke_harness_events(
            _Client(_call("s-1", {"proposal": VALID_PROPOSAL})), "arn", [],
            session_id="s", actor_id="a"))
    assert err.value.code == "harness.incomplete_response"


# ---------------------------------------------------------------------------
# turns
# ---------------------------------------------------------------------------


def test_a_rejected_submission_is_fixed_within_the_same_reply(client, ready, harness, offered):
    cid = _open(client)
    harness.queued = [
        _call("s-1", {"proposal": {**VALID_PROPOSAL, "name": "Bad Name!"}}),
        _call("s-2", {"change": {"base_revision": 1, "operations": [
            {"op": "replace", "path": "/name", "value": "hr-helpdesk"}]}}),
        _text("Here is the design; review the proposal in the panel."),
    ]
    events = _turn(client, cid, "propose")
    assert [k for k, _ in events if k not in ("delta", "heartbeat")] == [
        "meta", "tool", "tool_input", "tool", "tool_input", "proposal", "preparation", "done"
    ], events
    proposal = next(d for k, d in events if k == "proposal")
    assert proposal["status"] == "draft" and proposal["content"]["name"] == "hr-helpdesk"
    assert proposal["revision"] == 1 and proposal["source"] == "model"

    calls = harness.calls
    assert len(calls) == 3
    assert len({c["runtimeSessionId"] for c in calls}) == 1  # one session per turn
    for c in calls:  # the inline tool and the preset's own tools on EVERY request
        assert c["tools"] == [*PRESET_TOOLS, submission.TOOL]
        assert c["allowedTools"] == [*PRESET_ALLOWED, "@submit_proposal"]
    first = calls[0]["messages"][0]["content"][0]["text"]
    assert f"## Submitting the proposal: the `{submission.TOOL_NAME}` tool" in first
    verdicts = []
    for c in calls[1:]:
        (block,) = c["messages"][0]["content"]
        verdicts.append(json.loads(block["toolResult"]["content"][0]["text"]))
    assert [v["status"] for v in verdicts] == ["rejected", "accepted"]
    assert verdicts[0]["candidate_revision"] == 1

    detail = _latest(client, cid)
    assert not [m for m in detail["messages"] if m["role"] == "error"]
    tool_rows = [m["text"] for m in detail["messages"] if m["role"] == "tool"]
    assert tool_rows == ["proposal Bad Name!", "proposal revision (1 edit)"]
    assert len(detail["proposals"]) == 1  # the rejected candidate was never stored


def test_a_submission_left_invalid_is_recorded_with_the_rejection(
    client, ready, harness, offered
):
    cid = _open(client)
    harness.queued = [
        _call("s-1", {"proposal": {**VALID_PROPOSAL, "name": "Bad Name!"}}),
        _text("I could not settle the name; please pick one."),
    ]
    proposal = next(d for k, d in _turn(client, cid, "propose") if k == "proposal")
    assert proposal["status"] == "invalid" and proposal["content"]["name"] == "Bad Name!"
    rows = [(m["role"], m["name"]) for m in _latest(client, cid)["messages"]]
    assert rows[-1] == ("error", service.PROPOSAL_REJECTED_NAME)


def test_the_submission_wins_over_a_block_printed_in_the_text(
    client, ready, harness, offered
):
    cid = _open(client)
    harness.queued = [
        _call("s-1", {"proposal": VALID_PROPOSAL}),
        _text("Done.\n" + _block({**VALID_PROPOSAL, "summary": "printed copy"})),
    ]
    proposal = next(d for k, d in _turn(client, cid, "propose") if k == "proposal")
    assert proposal["status"] == "draft"
    assert proposal["content"]["summary"] == VALID_PROPOSAL["summary"]


def test_a_turn_without_the_read_back_keeps_the_fenced_protocol(client, ready, harness):
    cid = _open(client)
    harness.reply(_block(VALID_PROPOSAL))
    proposal = next(d for k, d in _turn(client, cid, "propose") if k == "proposal")
    assert proposal["status"] == "draft"
    call = harness.calls[0]
    assert "tools" not in call and "allowedTools" not in call
    assert "## Submitting the proposal" not in call["messages"][0]["content"][0]["text"]


def test_the_read_back_fails_soft(monkeypatch, ready):
    class Boom:
        def get_harness(self, **kwargs):
            raise RuntimeError("throttled")

    monkeypatch.setattr(service, "control_client", lambda workspace: Boom())
    agent = type("A", (), {"resource_id": "h-1"})()
    assert _REAL_OVERRIDES(None, agent) is None

    class Detail:
        def get_harness(self, **kwargs):
            return {"harness": {"tools": PRESET_TOOLS, "allowedTools": PRESET_ALLOWED}}

    monkeypatch.setattr(service, "control_client", lambda workspace: Detail())
    got = _REAL_OVERRIDES(None, agent)
    assert got is not None
    tools, allowed = got
    assert tools == [*PRESET_TOOLS, submission.TOOL]
    assert allowed == [*PRESET_ALLOWED, "@submit_proposal"]
