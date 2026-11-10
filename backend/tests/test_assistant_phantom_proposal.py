# ruff: noqa: F811 — pytest fixtures are imported explicitly from test_assistant
"""N17-obs468 — a reply that CLAIMS a revision but carries no proposal block.

Live 2026-10-26 (conversation 785829de on default): after revision 1 the member asked
「更新提案」 and the architect answered 「修订已提交，控制台中的提案已更新为最新版」 — but its
reply carried, instead of a change block, the literal replay placeholder Launchpad puts
where it removes a past reply's block ("[proposal block of this reply omitted from the
replay — …]"). The model had imitated it. Nothing was extracted, no revision 2 was ever
stored, and nothing told anyone: the member was told to hard-refresh.

Now such a reply (the placeholder or a close variant, or a submission claim with no
block) gets a transcript notice and ONE corrective internal call in the same turn; the
placeholder is no longer written inside the model's own replayed messages, and the
preamble forbids writing it."""

import json

import pytest

from app.assistant import proposal as contract
from app.assistant import service
from app.core.config import get_settings

from .test_assistant import (  # noqa: F401 — shared fixtures, including autouse guards
    VALID_PROPOSAL,
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

# the live reply of turn 2, verbatim in shape: a submission claim + the imitated marker
PHANTOM = ("修订已提交，控制台中的提案已更新为最新版。如果页面没有变化，请强制刷新。\n\n"
           + service.REPLAY_PROPOSAL_MARKER)
# the live reply of 08:15:17Z: a model-invented bracket note, no block
NO_PATCH = ("我已根据你的要求调整了鱼骨图的「其他」屏障。\n\n"
            "[No patch emitted in this reply; the stored proposal already reflects it]")


def _patch(revision: int, value: str) -> str:
    body = json.dumps({"base_revision": revision, "operations": [
        {"op": "replace", "path": "/system_prompt", "value": value}]})
    return f"已更新系统提示词。\n\n```{contract.PATCH_FENCE}\n{body}\n```"


def _events(text: str) -> list[dict]:
    return [{"contentBlockDelta": {"delta": {"text": text[i:i + 40]}}}
            for i in range(0, len(text), 40)]


def _first_revision(client, harness) -> str:
    cid = _open(client)
    harness.reply("Here is the design.\n" + _block(VALID_PROPOSAL))
    events = _turn(client, cid, "propose")
    assert next(d for k, d in events if k == "proposal")["revision"] == 1
    return cid


def _rows(client, cid, turn):
    return [m for m in _latest(client, cid)["messages"] if m["turn"] == turn]


def test_the_live_shape_gets_a_notice_and_one_corrective_call_that_recovers(
    client, ready, harness
):
    cid = _first_revision(client, harness)
    calls_before = len(harness.calls)
    harness.queued = [_events(PHANTOM), _events(_patch(1, "You answer travel questions."))]
    events = _turn(client, cid, "更新提案")
    kinds = [k for k, _ in events]
    assert len(harness.calls) - calls_before == 2  # the reply + ONE corrective call
    assert "error" not in kinds and kinds[-1] == "done"
    assert kinds.index("reask") < kinds.index("proposal")
    assert dict(events)["reask"] == {"revision": 1, "reason": "replay_marker"}
    proposal = dict(events)["proposal"]
    assert (proposal["revision"], proposal["status"]) == (2, "draft")
    assert proposal["content"]["system_prompt"] == "You answer travel questions."

    rows = [(m["role"], m["name"]) for m in _rows(client, cid, 2)]
    # the internal follow-up is never stored as a member message
    assert rows == [("user", None), ("assistant", None),
                    ("error", service.PROPOSAL_MISSING_RECOVERED), ("assistant", None)]
    # the corrective call names the placeholder, the base revision, and is on the
    # member's side of the replay
    sent = harness.messages_of(-1)
    follow_up = sent[-1]["content"][0]["text"]
    assert sent[-1]["role"] == "user"
    assert "replay placeholder" in follow_up and "revision 1" in follow_up
    assert _latest(client, cid)["turn_revisions"]["2"] == [2]


def test_the_corrective_call_is_bounded_to_once_and_the_member_is_told(client, ready, harness):
    cid = _first_revision(client, harness)
    calls_before = len(harness.calls)
    harness.queued = [_events(PHANTOM), _events(PHANTOM)]  # the correction echoes it too
    events = _turn(client, cid, "更新提案")
    kinds = [k for k, _ in events]
    assert len(harness.calls) - calls_before == 2  # never a third call
    assert "proposal" not in kinds and kinds[-1] == "done"
    detail = _latest(client, cid)
    assert [p["revision"] for p in detail["proposals"]] == [1]  # nothing stored as one
    notice = [m for m in _rows(client, cid, 2) if m["role"] == "error"]
    assert [m["name"] for m in notice] == [service.PROPOSAL_MISSING]
    assert "still revision 1" in notice[0]["text"]
    assert detail["turn_revisions"].get("2", []) == []


@pytest.mark.parametrize("reply, reason", [
    (NO_PATCH, "replay_marker"),
    ("修订已提交，请在控制台查看。", "claimed_revision"),
    ("I have submitted the revised proposal; the console now shows it.", "claimed_revision"),
    ("[proposal block omitted from the replay]", "replay_marker"),
])
def test_close_variants_and_bare_claims_are_caught(client, ready, harness, reply, reason):
    cid = _first_revision(client, harness)
    harness.queued = [_events(reply), _events(_patch(1, "Fixed."))]
    events = _turn(client, cid, "更新提案")
    assert dict(events)["reask"]["reason"] == reason
    assert dict(events)["proposal"]["revision"] == 2


@pytest.mark.parametrize("reply", [
    "Which tools should the agent use?",
    "我会在你确认后更新提案。",  # a future intent, not a claim of a stored change
    "提案未修改：这个问题不需要改动。",
])
def test_an_ordinary_reply_without_a_block_is_left_alone(client, ready, harness, reply):
    cid = _first_revision(client, harness)
    calls_before = len(harness.calls)
    harness.reply(reply)
    events = _turn(client, cid, "question")
    assert len(harness.calls) - calls_before == 1
    assert "reask" not in [k for k, _ in events]
    assert not [m for m in _rows(client, cid, 2) if m["role"] == "error"]


def test_a_reply_with_a_real_block_and_a_claim_is_not_reasked(client, ready, harness):
    cid = _first_revision(client, harness)
    calls_before = len(harness.calls)
    harness.reply("修订已提交。\n\n" + _patch(1, "New prompt."))
    events = _turn(client, cid, "更新提案")
    assert len(harness.calls) - calls_before == 1
    assert dict(events)["proposal"]["revision"] == 2


def test_with_the_correction_switched_off_the_notice_still_tells_the_truth(
    client, ready, harness, monkeypatch
):
    monkeypatch.setattr(get_settings(), "assistant_proposal_reask", False)
    cid = _first_revision(client, harness)
    calls_before = len(harness.calls)
    harness.reply(PHANTOM)
    _turn(client, cid, "更新提案")
    assert len(harness.calls) - calls_before == 1
    assert [m["name"] for m in _rows(client, cid, 2) if m["role"] == "error"] == [
        service.PROPOSAL_MISSING]


def test_the_replay_never_puts_the_placeholder_in_the_models_own_messages(
    client, ready, harness
):
    cid = _first_revision(client, harness)
    harness.queued = [_events(PHANTOM), _events(PHANTOM)]
    _turn(client, cid, "更新提案")
    harness.reply("Which tools?")
    _turn(client, cid, "next")
    sent = harness.messages_of(-1)
    for m in sent:
        if m["role"] == "assistant":
            text = m["content"][0]["text"]
            assert service.REPLAY_PROPOSAL_MARKER not in text
            assert "omitted from the replay" not in text
            assert f"```{contract.PROPOSAL_FENCE}" not in text
    member_side = "\n".join(m["content"][0]["text"] for m in sent if m["role"] == "user")
    # what happened is said on Launchpad's side: the preamble says past blocks are removed
    # (turn 1's), and turn 2's replies carried none
    assert service.REPLAY_BLOCK_NOTE in member_side
    assert service.REPLAY_NO_BLOCK_NOTE in member_side
    assert service.REPLAY_PROPOSAL_MARKER not in "\n".join(
        m["content"][0]["text"] for m in sent)


def test_the_preamble_forbids_writing_the_placeholder():
    text = " ".join(service.PROTOCOL_PREAMBLE.split())
    assert "Never write the replay placeholder" in text
    assert "emit the complete proposal block" in text


def test_every_model_turn_reports_the_revisions_it_produced(client, ready, harness):
    cid = _first_revision(client, harness)
    harness.reply("Which tools?")
    _turn(client, cid, "question")
    detail = _latest(client, cid)
    assert detail["turn_revisions"] == {"1": [1], "2": []}


# ---------------------------------------------------------------------------
# a re-ask that ends without its outcome is settled, never left pending
# ---------------------------------------------------------------------------


def _drive_until_reask(harness, how: str) -> str:
    """Revision 1, then a phantom reply through ``run_turn`` stopped as soon as the
    re-ask starts: ``signal`` = the owner cancels, ``close`` = the consumer goes away."""
    from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
    from app.models.ledger import Workspace
    from app.routers.auth import Identity
    from app.services.workspace import workspace_context

    harness.queued = [_events("Design.\n" + _block(VALID_PROPOSAL)),
                      _events(PHANTOM), _events(_patch(1, "Fixed."))]
    db = SessionLocal()
    try:
        row = db.get(Workspace, DEFAULT_WORKSPACE_ID)
        identity = Identity(username="river", role="admin")
        conv = service.create_conversation(db, row, workspace_context(row), identity, "t")
        for _ in service.run_turn(db, conv, row, workspace_context(row), identity, "propose",
                                  run=service.TurnRun()):
            pass
        run = service.TurnRun()
        gen = service.run_turn(db, conv, row, workspace_context(row), identity, "更新提案",
                               run=run)
        run.generator = gen
        kinds = []
        for event in gen:
            kinds.append(event["event"])
            if event["event"] == "reask":
                if how == "close":
                    gen.close()
                    break
                run.signal()
        assert "reask" in kinds
        return conv.id
    finally:
        db.close()


@pytest.mark.parametrize("how", ["signal", "close"])
def test_a_cancel_during_the_reask_settles_the_notice(client, ready, harness, how):
    cid = _drive_until_reask(harness, how)
    names = [m["name"] for m in _rows(client, cid, 2) if m["role"] == "error"]
    assert service.PROPOSAL_MISSING_PENDING not in names
    assert names.count(service.PROPOSAL_MISSING) == 1
    notice = next(m for m in _rows(client, cid, 2) if m["name"] == service.PROPOSAL_MISSING)
    assert notice["missing"] == {"base": 1, "revision": None}
    assert _latest(client, cid)["turn_in_progress"] is None


def test_a_reask_left_by_a_dead_process_is_settled_at_startup(client, ready, harness):
    from app.core.db import SessionLocal
    from app.models.assistant import AssistantConversation, AssistantMessage, AssistantTurnEvent

    cid = _first_revision(client, harness)
    harness.queued = [_events(PHANTOM), _events(PHANTOM)]
    _turn(client, cid, "更新提案")
    db = SessionLocal()
    try:  # rewind turn 2 to "re-ask in flight in a process that then died"
        notice = db.query(AssistantMessage).filter_by(
            conversation_id=cid, name=service.PROPOSAL_MISSING).one()
        notice.name = service.PROPOSAL_MISSING_PENDING
        notice.text = service.missing_note(service.PROPOSAL_MISSING_PENDING, 1)
        db.query(AssistantTurnEvent).filter(
            AssistantTurnEvent.conversation_id == cid, AssistantTurnEvent.turn == 2,
            AssistantTurnEvent.event.in_(service.TURN_TERMINAL_EVENTS)).delete()
        row = db.get(AssistantConversation, cid)
        row.active_turn, row.active_turn_token = 2, "dead-token"
        db.commit()
    finally:
        db.close()
    assert service.clear_stale_turn_claims() == 1
    names = [m["name"] for m in _rows(client, cid, 2) if m["role"] == "error"]
    assert names == [service.PROPOSAL_MISSING]
    assert _latest(client, cid)["turn_in_progress"] is None
