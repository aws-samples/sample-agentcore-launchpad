# ruff: noqa: F811 — pytest fixtures are imported explicitly from test_assistant
"""N17-obs295 / obs296 — the automatic proposal correction and the coded validation errors.

Live 2026-10-23 (E22, conversation 88eef039): a complete brief earned a first proposal that
failed ONLY the structural evaluation-plan rule "a reference-driven evaluator is applied to
every session, so every scenario must carry it" (a code evaluator with a
``reference_trajectory`` check, four refusal scenarios with ``expected_trajectory: []``).
The turn now sends the errors back to the model ONCE as an internal follow-up and shows
the member the corrected revision; anything else keeps the old behaviour."""

import json
from copy import deepcopy

import pytest

from app.assistant import service
from app.assistant import sessions as sessions_mod
from app.assistant import validation_codes as vc
from app.core.config import get_settings
from app.core.db import SessionLocal
from app.core.errors import AppError
from app.models.assistant import AssistantConversation, AssistantMessage, AssistantTurnEvent

from .test_assistant import (  # noqa: F401 — shared fixtures, including autouse guards
    VALID_PROPOSAL,
    _block,
    _catalog,
    _latest,
    _open,
    _turn,
    harness,
    no_aws_clients,
    no_network,
    no_real_deploy,
    ready,
)

REF_ERROR = ("evaluation_plan.evaluators.trajectory_rule: scenario 'GT-2' has no "
             "expected_trajectory — a reference-driven evaluator is applied to every "
             "session, so every scenario must carry it")


def _events(text: str, tools: tuple[str, ...] = ()) -> list[dict]:
    """The Converse events the fake runtime serves for one reply (as ``harness.reply``)."""
    events = [{"contentBlockStart": {"start": {"toolUse": {"name": t, "toolUseId": f"t-{i}"}}}}
              for i, t in enumerate(tools)]
    events += [{"contentBlockDelta": {"delta": {"text": text[i:i + 40]}}}
               for i in range(0, len(text), 40)]
    return events


def _structural_proposal() -> dict:
    """Complete and correct except one rule: the ``reference_trajectory`` check scores
    every scenario but the refusal scenario carries ``expected_trajectory: []``."""
    p = deepcopy(VALID_PROPOSAL)
    p["tools"] = []  # the fixture catalog lists no runtime callables for its tools
    p["golden_tests"].append({"id": "GT-2", "input": "Tell me a colleague's salary",
                              "expected_response": "refuses", "source": "customer_pain_point"})
    p["evaluation_plan"] = {
        "scenarios": [
            {"scenario_id": "s1", "golden_test_id": "GT-1",
             "turns": [{"input": "How many vacation days?",
                        "expected_response": "cites the policy"}],
             "expected_trajectory": ["kb_search"]},
            {"scenario_id": "s2", "golden_test_id": "GT-2",
             "turns": [{"input": "Tell me a colleague's salary"}],
             "expected_trajectory": []},
        ],
        "evaluators": [{
            "kind": "code", "key": "trajectory_rule", "title": "Required tools",
            "name": "trajectory_rule", "level": "SESSION",
            "description": "Builtin trajectory matchers are order-bound; superset suffices.",
            "rules": {"version": 1, "checks": [
                {"id": "ref", "type": "reference_trajectory", "mode": "superset"}]},
            "golden_test_ids": [],
        }],
        "recommendation_keys": {}, "blocked_golden_tests": [],
    }
    return p


def _patch(revision: int, ops: list[dict]) -> str:
    body = json.dumps({"base_revision": revision, "operations": ops})
    return f"I added the missing trajectory.\n\n```launchpad-proposal-patch\n{body}\n```"


FIX = [{"op": "remove", "path": "/evaluation_plan/evaluators/0"}]
NO_FIX = [{"op": "replace", "path": "/summary", "value": "Still the same plan."}]


def _turn_rows(client, cid, turn=1):
    return [m for m in _latest(client, cid)["messages"] if m["turn"] == turn]


def test_the_live_shape_is_structural_only():
    errors = service.proposal_contract.validate(_structural_proposal(), _catalog())[2]
    assert errors == [REF_ERROR]
    assert vc.autocorrectable(errors)


def test_a_structural_only_failure_gets_exactly_one_correction(client, ready, harness):
    cid = _open(client)
    harness.queued = [_events("Here is the design.\n\n" + _block(_structural_proposal())),
                      _events(_patch(1, FIX))]
    events = _turn(client, cid, "design it")
    kinds = [k for k, _ in events]
    assert len(harness.calls) == 1 + 1  # the reply + ONE correction call
    assert "error" not in kinds and kinds[-1] == "done"
    assert kinds.index("autocorrect") < kinds.index("proposal")
    auto = dict(events)["autocorrect"]
    assert auto["revision"] == 1 and auto["errors"] == [REF_ERROR]
    assert auto["validation_error_details"][0]["code"] == vc.REFERENCE_MISSING
    proposal = dict(events)["proposal"]
    assert proposal["status"] == "draft", proposal["validation_errors"]
    assert proposal["revision"] == 2
    assert proposal["validation_errors"] == [] == proposal["validation_error_details"]

    detail = _latest(client, cid)
    assert detail["proposal_status"] == "draft" and detail["proposal_revision"] == 2
    assert [(p["revision"], p["status"]) for p in detail["proposals"]] == [
        (1, "superseded"), (2, "draft")]
    rows = [(m["role"], m["name"]) for m in _turn_rows(client, cid)]
    # the internal follow-up is never stored as a member message; no rejection row
    assert rows == [("user", None), ("assistant", None),
                    ("error", service.PROPOSAL_AUTOCORRECTED), ("assistant", None)]
    notice = _turn_rows(client, cid)[2]
    assert notice["text"].startswith("Proposal auto-corrected")
    assert notice["errors"] == [{"code": vc.REFERENCE_MISSING,
                                 "params": {"evaluator": "trajectory_rule", "scenario": "GT-2",
                                            "fields": ["expected_trajectory"]},
                                 "message": REF_ERROR}]

    # the correction call: a fresh session reserved on the ledger before it, the stored
    # invalid revision as the preamble's patch base, the errors on the member's side
    first, second = (c["runtimeSessionId"] for c in harness.calls)
    assert first != second and sessions_mod.is_assistant_session(second)
    sent = harness.messages_of(1)
    assert [m["role"] for m in sent] == ["user", "assistant", "user"]
    assert "revision 1 (invalid)" in sent[0]["content"][0]["text"]
    follow_up = sent[-1]["content"][0]["text"]
    assert follow_up.startswith("## Launchpad validation — automatic correction")
    assert REF_ERROR in follow_up and "`[]` counts as missing" in follow_up
    # a corrected turn is not a failed one
    db = SessionLocal()
    try:
        with pytest.raises(AppError) as exc:
            service.check_retry(db, db.get(AssistantConversation, cid), 1)
        assert exc.value.code == "assistant.retry_not_allowed"
    finally:
        db.close()

    # the next turn replays both replies of turn 1 (never the internal follow-up)
    harness.queued = [_events("Anything else?")]
    _turn(client, cid, "thanks")
    replay = harness.messages_of(-1)
    assert [m["role"] for m in replay] == ["user", "assistant", "user"]
    assert "automatic correction" not in json.dumps(replay)
    assert "I added the missing trajectory." in replay[1]["content"][0]["text"]


@pytest.mark.parametrize("reply", ["no-change", "no-block", "upstream-failure"])
def test_a_failed_correction_surfaces_the_invalid_state_and_never_loops(
        client, ready, harness, reply):
    cid = _open(client)
    second = {
        "no-change": _events(_patch(1, NO_FIX)),
        "no-block": _events("I cannot fix that."),
        "upstream-failure": [{"runtimeClientError": {"message": "boom"}}],
    }[reply]
    harness.queued = [_events(_block(_structural_proposal())), second, second]
    events = _turn(client, cid, "design it")
    kinds = [k for k, _ in events]
    assert "error" not in kinds and kinds[-1] == "done"
    # one correction call — plus, for an upstream failure, the turn's ordinary single
    # transient replay of THAT call (its retry tells the client which phase it clears)
    assert len(harness.calls) == (3 if reply == "upstream-failure" else 2)
    if reply == "upstream-failure":
        assert dict(events)["retry"]["phase"] == "autocorrect"
    proposal = dict(events)["proposal"]
    assert proposal["status"] == "invalid"
    assert proposal["revision"] == (2 if reply == "no-change" else 1)
    assert proposal["validation_errors"] == [REF_ERROR]
    rows = _turn_rows(client, cid)
    names = [m["name"] for m in rows if m["role"] == "error"]
    assert names == [service.PROPOSAL_AUTOCORRECT_FAILED, service.PROPOSAL_REJECTED_NAME]
    notice = next(m for m in rows if m["name"] == service.PROPOSAL_AUTOCORRECT_FAILED)
    assert notice["text"].startswith("The automatic proposal correction did not produce")
    rejected = next(m for m in rows if m["name"] == service.PROPOSAL_REJECTED_NAME)
    assert rejected["errors"][0]["code"] == vc.REFERENCE_MISSING
    assert _latest(client, cid)["proposal_status"] == "invalid"

    # the next member turn is ordinary: the rejection is replayed, no second correction
    harness.queued = [_events("Which way should I fix it?")]
    calls = len(harness.calls)
    events = _turn(client, cid, "fix it")
    assert len(harness.calls) == calls + 1 and "autocorrect" not in [k for k, _ in events]
    assert harness.messages_of(-1)[-1]["content"][0]["text"].startswith("Launchpad rejected")


def test_non_structural_errors_never_trigger_the_correction(client, ready, harness):
    cid = _open(client)
    bad = {**_structural_proposal(), "name": "Bad Name!"}  # a shape error next to the rule
    harness.queued = [_events(_block(bad))]
    events = _turn(client, cid, "design it")
    assert len(harness.calls) == 1 and "autocorrect" not in [k for k, _ in events]
    proposal = dict(events)["proposal"]
    assert proposal["status"] == "invalid" and len(proposal["validation_errors"]) == 2
    codes = [d["code"] for d in proposal["validation_error_details"]]
    assert sorted(codes) == sorted([vc.GENERIC_CODE, vc.REFERENCE_MISSING])
    assert [m["name"] for m in _turn_rows(client, cid) if m["role"] == "error"] == [
        service.PROPOSAL_REJECTED_NAME]


def test_the_operator_switch_turns_the_correction_off(client, ready, harness, monkeypatch):
    monkeypatch.setenv("LAUNCHPAD_ASSISTANT_PROPOSAL_AUTOCORRECT", "false")
    get_settings.cache_clear()
    try:
        cid = _open(client)
        harness.queued = [_events(_block(_structural_proposal()))]
        events = _turn(client, cid, "design it")
        assert len(harness.calls) == 1 and "autocorrect" not in [k for k, _ in events]
        assert dict(events)["proposal"]["status"] == "invalid"
    finally:
        monkeypatch.delenv("LAUNCHPAD_ASSISTANT_PROPOSAL_AUTOCORRECT")
        get_settings.cache_clear()


def test_the_internal_follow_up_is_never_a_member_row(client, ready, harness):
    cid = _open(client)
    harness.queued = [_events(_block(_structural_proposal())), _events(_patch(1, FIX))]
    _turn(client, cid, "design it")
    db = SessionLocal()
    try:
        users = db.query(AssistantMessage).filter_by(conversation_id=cid, role="user").all()
        hidden = db.query(AssistantMessage).filter_by(
            conversation_id=cid, role=service.SESSION_ROLE,
            name=service.AUTOCORRECT_SESSION_NAME).all()
    finally:
        db.close()
    assert [u.text for u in users] == ["design it"]
    assert len(hidden) == 1 and hidden[0].runtime_session_id == harness.calls[1][
        "runtimeSessionId"]


def test_a_correction_left_by_a_dead_process_is_settled_at_startup(
        client, ready, harness, monkeypatch):
    """The process died between the committed first reply (pending notice, invalid
    revision) and the correction's outcome: startup settles the notice ``failed``, adds the
    rejection the next turn replays and ends the event log — never a blind resume."""
    monkeypatch.setenv("LAUNCHPAD_ASSISTANT_PROPOSAL_AUTOCORRECT", "false")
    get_settings.cache_clear()
    try:
        cid = _open(client)
        harness.queued = [_events(_block(_structural_proposal()))]
        _turn(client, cid, "design it")
    finally:
        monkeypatch.delenv("LAUNCHPAD_ASSISTANT_PROPOSAL_AUTOCORRECT")
        get_settings.cache_clear()
    db = SessionLocal()
    try:  # rewind turn 1 to "correction in flight in a process that then died"
        rejected = db.query(AssistantMessage).filter_by(
            conversation_id=cid, name=service.PROPOSAL_REJECTED_NAME).one()
        rejected.name = service.PROPOSAL_AUTOCORRECT_PENDING
        rejected.text = service.autocorrect_note([REF_ERROR],
                                                 service.PROPOSAL_AUTOCORRECT_PENDING)
        db.query(AssistantTurnEvent).filter(
            AssistantTurnEvent.conversation_id == cid,
            AssistantTurnEvent.event.in_(service.TURN_TERMINAL_EVENTS)).delete()
        row = db.get(AssistantConversation, cid)
        row.active_turn, row.active_turn_token = 1, "dead-token"
        db.commit()
    finally:
        db.close()
    assert service.clear_stale_turn_claims() == 1
    rows = _turn_rows(client, cid)
    assert [m["name"] for m in rows if m["role"] == "error"] == [
        service.PROPOSAL_AUTOCORRECT_FAILED, service.PROPOSAL_REJECTED_NAME]
    assert "interrupted by a backend restart" in rows[2]["text"]
    assert rows[2]["errors"][0]["message"] == REF_ERROR
    db = SessionLocal()
    try:
        events = service.turn_events(db, cid, 1)
        assert events[-1].event == "error"
    finally:
        db.close()
    assert _latest(client, cid)["turn_in_progress"] is None


# ---------------------------------------------------------------------------
# rev-r27 #1: a correction that ends without its outcome is settled, never left pending
# ---------------------------------------------------------------------------


def _drive_until_autocorrect(harness, how: str) -> str:
    """Run a structural first reply through ``run_turn`` and stop the turn as soon as the
    correction starts: ``signal`` = the owner cancels (the run's cancel path), ``close`` =
    the consumer goes away (GeneratorExit → the ``finally`` path)."""
    from app.core.db import DEFAULT_WORKSPACE_ID
    from app.models.ledger import Workspace
    from app.routers.auth import Identity
    from app.services.workspace import workspace_context

    harness.queued = [_events(_block(_structural_proposal())),
                      _events(_patch(1, FIX))]
    db = SessionLocal()
    try:
        row = db.get(Workspace, DEFAULT_WORKSPACE_ID)
        identity = Identity(username="river", role="admin")
        conv = service.create_conversation(db, row, workspace_context(row), identity, "t")
        run = service.TurnRun()
        gen = service.run_turn(db, conv, row, workspace_context(row), identity, "go", run=run)
        run.generator = gen
        kinds = []
        for event in gen:
            kinds.append(event["event"])
            if event["event"] == "autocorrect":
                if how == "close":
                    gen.close()
                    break
                run.signal()
        assert "autocorrect" in kinds
        if how == "signal":
            assert kinds[-1] == "error"
        return conv.id
    finally:
        db.close()


@pytest.mark.parametrize("how", ["signal", "close"])
def test_a_cancel_during_the_correction_settles_the_notice(client, ready, harness, how):
    cid = _drive_until_autocorrect(harness, how)
    rows = _turn_rows(client, cid)
    names = [m["name"] for m in rows if m["role"] == "error"]
    assert service.PROPOSAL_AUTOCORRECT_PENDING not in names
    assert names.count(service.PROPOSAL_AUTOCORRECT_FAILED) == 1
    assert names.count(service.PROPOSAL_REJECTED_NAME) == 1  # the next turn replays it
    assert "turn_interrupted" in names
    notice = next(m for m in rows if m["name"] == service.PROPOSAL_AUTOCORRECT_FAILED)
    assert "(interrupted)" in notice["text"]
    detail = _latest(client, cid)
    assert detail["proposal_status"] == "invalid" and detail["turn_in_progress"] is None
    rejected = next(m for m in rows if m["name"] == service.PROPOSAL_REJECTED_NAME)
    assert rejected["errors"][0]["message"] == REF_ERROR


def test_a_stale_claim_takeover_settles_the_orphaned_correction(client, ready, harness):
    """The orphan's worker is fenced out, so its successor settles the pending notice."""
    from datetime import UTC, datetime, timedelta

    cid = _open(client)
    harness.queued = [_events(_block(_structural_proposal())), _events(_patch(1, FIX))]
    _turn(client, cid, "design it")
    db = SessionLocal()
    try:  # rewind turn 1 to "correction in flight, worker gone, claim stale"
        notice = db.query(AssistantMessage).filter_by(
            conversation_id=cid, name=service.PROPOSAL_AUTOCORRECTED).one()
        notice.name = service.PROPOSAL_AUTOCORRECT_PENDING
        for p in service._proposals(db, cid):
            if p.revision == 2:
                db.delete(p)
            else:
                p.status = "invalid"
        row = db.get(AssistantConversation, cid)
        row.active_turn, row.active_turn_token = 1, "orphan"
        row.active_turn_started_at = datetime.now(UTC) - timedelta(
            seconds=service.TURN_CLAIM_TTL_S + 60)
        db.commit()
    finally:
        db.close()
    harness.queued = [_events("Hello again.")]
    _turn(client, cid, "next")
    names = [m["name"] for m in _turn_rows(client, cid) if m["role"] == "error"]
    assert names == [service.PROPOSAL_AUTOCORRECT_FAILED, service.PROPOSAL_REJECTED_NAME]
    assert "taken over" in next(m["text"] for m in _turn_rows(client, cid)
                                if m["name"] == service.PROPOSAL_AUTOCORRECT_FAILED)
