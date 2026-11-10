# ruff: noqa: F811 — pytest fixtures are imported explicitly from test_assistant
"""N17-obs469 — a proposal whose agent name is already taken says so before approval.

Live 2026-10-26 (conversation 785829de, proposal r2 "travel-expense-assistant"): 批准并部署
answered 409 ``agent.name_exists`` three times — the name was held by the active E22B agent
a74f3624 — and the only feedback was a transient toast, so the member thought the button did
not work. A draft / invalid revision now carries ``name_conflict`` (the same live-holder rule
``claim_agent_name`` applies), the console disables approval with that reason, and the
architect is told which names are in use. The server-side 409 stays the backstop."""

import pytest

from app.assistant import service
from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.models.ledger import Agent

from .test_assistant import (  # noqa: F401 — shared fixtures, including autouse guards
    VALID_PROPOSAL,
    _approve,
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

NAME = VALID_PROPOSAL["name"]


def _agent(name: str = NAME, status: str = "active") -> str:
    db = SessionLocal()
    try:
        row = Agent(workspace_id=DEFAULT_WORKSPACE_ID, name=name, method="harness",
                    status=status, spec={})
        db.add(row)
        db.commit()
        return row.id
    finally:
        db.close()


def _set_status(agent_id: str, status: str) -> None:
    db = SessionLocal()
    try:
        db.get(Agent, agent_id).status = status
        db.commit()
    finally:
        db.close()


def _proposal(client, harness) -> tuple[str, dict]:
    cid = _open(client)
    harness.reply("Here is the design.\n" + _block(VALID_PROPOSAL))
    events = _turn(client, cid, "propose")
    return cid, next(d for k, d in events if k == "proposal")


def test_a_draft_whose_name_is_held_by_a_live_agent_says_so(client, ready, harness):
    holder = _agent()
    cid, proposal = _proposal(client, harness)
    assert proposal["name_conflict"] == {"name": NAME}  # on the live proposal event too
    shown = _latest(client, cid)["proposals"][-1]
    assert shown["status"] == "draft" and shown["name_conflict"] == {"name": NAME}
    # the 409 stays the backstop
    res = _approve(client, cid, shown)
    assert res.status_code == 409 and res.json()["code"] == "agent.name_exists"
    # a deleted holder frees the name — the warning goes with it
    _set_status(holder, "deleted")
    assert _latest(client, cid)["proposals"][-1]["name_conflict"] is None


def test_a_free_name_carries_no_conflict(client, ready, harness):
    _agent(name="someone-else")
    cid, proposal = _proposal(client, harness)
    assert proposal["name_conflict"] is None


def test_an_approved_revision_never_reports_its_own_agent_as_a_conflict(
    client, ready, harness, no_real_deploy
):
    cid, proposal = _proposal(client, harness)
    res = _approve(client, cid, proposal)
    assert res.status_code == 202, res.text
    shown = _latest(client, cid)["proposals"][-1]
    assert shown["status"] == "approved" and shown["name_conflict"] is None


def test_the_architect_is_told_which_names_are_in_use(client, ready, harness):
    _agent()
    _agent(name="travel-expense-assistant")
    deleted = _agent(name="retired-bot")
    _set_status(deleted, "deleted")
    cid = _open(client)
    harness.reply("Which tools?")
    _turn(client, cid, "hello")
    preamble = harness.messages_of(-1)[0]["content"][0]["text"]
    section = preamble[preamble.index(service.TAKEN_NAMES_HEADING):]
    assert f"`{NAME}`" in section and "`travel-expense-assistant`" in section
    assert "`retired-bot`" not in section  # a deleted agent's name is reusable


def test_the_architect_is_told_when_the_stored_proposal_name_is_taken(client, ready, harness):
    cid, _ = _proposal(client, harness)
    _agent()
    harness.reply("Which tools?")
    _turn(client, cid, "next")
    preamble = harness.messages_of(-1)[0]["content"][0]["text"]
    assert f"The stored proposal's name `{NAME}` is already taken" in preamble


@pytest.mark.parametrize("count", [0, service.TAKEN_NAMES_MAX + 5])
def test_the_names_list_is_bounded(client, ready, harness, count):
    for i in range(count):
        _agent(name=f"agent-{i:03d}")
    cid = _open(client)
    harness.reply("ok")
    _turn(client, cid, "hello")
    preamble = harness.messages_of(-1)[0]["content"][0]["text"]
    section = preamble[preamble.index(service.TAKEN_NAMES_HEADING):]
    listed = section.split("\n\n## ", 1)[0]
    assert listed.count("`agent-") <= service.TAKEN_NAMES_MAX
    # the preset agent `ready` installs is always listed, so the list is never empty
    assert ("more not listed" in listed) == bool(count)
