"""N17-obs464 — edit an AI recommendation by hand and publish it from the card.

The user (2026-10-26) wants, right on the recommendation card, to start from the
AI-recommended prompt, adjust it by hand and publish — also after the recommendation was
accepted (e6ca9126 / travel-expense-assistant-v2, accepted to v3), where the card was
read-only. ``POST /api/eval/runs/{run_id}/recommendations/{rec_id}/revise``
``{system_prompt, expected_version}`` publishes a NEW Harness version on top of the live one
through the accept path (same eligibility, ``agents.deploy``, owner-or-admin, preset /
converted refusals), pinned to the version the editor was opened on — a concurrent publish
answers 409 ``recommendation.version_moved`` and nothing is clobbered. It is recorded on
the recommendation (``edits[]``: by, at, from which version, the job) and on the version
history (snapshot note ``recommendation edited``). The row payload names the text the
editor starts from (``final_prompt``: what the acceptance published, edits included —
recovered from that publish's spec snapshot for an acceptance recorded before it was kept).
"""

from __future__ import annotations

import threading

from app.core.db import SessionLocal
from app.models.ledger import Agent, Job, SpecSnapshot
from tests.evaluation.test_run_recommendations import (
    _harness_agent,
    _no_deploy,
    _rec,
    _rows,
    _run,
    _stub,
)


def _live(agent_id: str, version: str) -> None:
    """The publish's deploy finished: the agent is ACTIVE on ``version``."""
    db = SessionLocal()
    try:
        agent = db.get(Agent, agent_id)
        agent.status, agent.version = "active", version
        db.commit()
    finally:
        db.close()


def _agent(agent_id: str) -> Agent:
    db = SessionLocal()
    try:
        return db.get(Agent, agent_id)
    finally:
        db.close()


def _url(run_id: str, rec_id: str, action: str) -> str:
    return f"/api/eval/runs/{run_id}/recommendations/{rec_id}/{action}"


def _accepted(client, monkeypatch, *, edited: str | None = None):
    _stub(monkeypatch)
    started = _no_deploy(monkeypatch)
    agent_id = _harness_agent()
    run_id = _run(agent_id)
    rec_id = _rec(run_id)
    body = {"system_prompt": edited} if edited is not None else None
    res = client.post(_url(run_id, rec_id, "accept"), json=body)
    assert res.status_code == 202, res.text
    _live(agent_id, "2")
    return agent_id, run_id, rec_id, started


def test_an_acceptance_records_the_text_it_published(client, monkeypatch):
    _agent_id, run_id, _rec_id, _ = _accepted(client, monkeypatch, edited="reviewed prompt")
    row = _rows(client, run_id)[0]
    assert row["accepted"]["system_prompt"] == "reviewed prompt"
    assert row["final_prompt"] == "reviewed prompt"
    assert row["edits"] == []


def test_publishing_an_edit_of_an_accepted_recommendation_makes_a_new_version(client, monkeypatch):
    agent_id, run_id, rec_id, started = _accepted(client, monkeypatch)
    res = client.post(_url(run_id, rec_id, "revise"), json={
        "system_prompt": "new prompt, adjusted by hand", "expected_version": "2"})
    assert res.status_code == 202, res.text
    body = res.json()
    assert started[-1] == body["job_id"] and len(started) == 2
    assert _agent(agent_id).spec["system_prompt"] == "new prompt, adjusted by hand"
    edit = body["recommendation"]["edits"][-1]
    assert (edit["version_before"], edit["job_id"]) == ("2", body["job_id"])
    assert edit["by"] and edit["at"]
    assert body["recommendation"]["final_prompt"] == "new prompt, adjusted by hand"
    db = SessionLocal()
    try:
        notes = [s.note for s in db.query(SpecSnapshot).filter_by(agent_id=agent_id)
                 .order_by(SpecSnapshot.seq)]
    finally:
        db.close()
    assert notes[-2:] == ["recommendation accepted", "recommendation edited"]


def test_a_stale_pin_is_refused_and_nothing_is_published(client, monkeypatch):
    agent_id, run_id, rec_id, started = _accepted(client, monkeypatch)
    _live(agent_id, "3")  # something else published v3 since the editor opened on v2
    res = client.post(_url(run_id, rec_id, "revise"),
                      json={"system_prompt": "edited", "expected_version": "2"})
    assert res.status_code == 409
    assert res.json()["code"] == "recommendation.version_moved"
    assert res.json()["detail"] == {"expected_version": "2", "current_version": "3"}
    assert len(started) == 1 and _agent(agent_id).spec["system_prompt"] != "edited"


def test_two_concurrent_publishes_of_one_pin_make_one_version(client, monkeypatch):
    agent_id, run_id, rec_id, started = _accepted(client, monkeypatch)
    barrier = threading.Barrier(2)
    codes: list[int] = []

    def go(text: str) -> None:
        barrier.wait()
        res = client.post(_url(run_id, rec_id, "revise"),
                          json={"system_prompt": text, "expected_version": "2"})
        codes.append(res.status_code)

    threads = [threading.Thread(target=go, args=(f"edit {i}",)) for i in range(2)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert sorted(codes) == [202, 409], codes
    assert len(started) == 2  # the acceptance + exactly one edit
    db = SessionLocal()
    try:
        assert db.query(Job).count() == 2
    finally:
        db.close()


def test_an_unaccepted_recommendation_is_edited_through_accept(client, monkeypatch):
    _stub(monkeypatch)
    _no_deploy(monkeypatch)
    run_id = _run(_harness_agent())
    rec_id = _rec(run_id)
    res = client.post(_url(run_id, rec_id, "revise"),
                      json={"system_prompt": "x", "expected_version": "1"})
    assert res.status_code == 409 and res.json()["code"] == "recommendation.not_accepted"
    row = _rows(client, run_id)[0]
    assert row["final_prompt"] == "new prompt"  # the editor starts from the AI text


def test_an_empty_edit_and_an_ineligible_agent_are_refused(client, monkeypatch):
    agent_id, run_id, rec_id, _ = _accepted(client, monkeypatch)
    res = client.post(_url(run_id, rec_id, "revise"),
                      json={"system_prompt": "   ", "expected_version": "2"})
    assert res.status_code == 422 and res.json()["code"] == "recommendation.accept_prompt_empty"
    db = SessionLocal()
    try:
        agent = db.get(Agent, agent_id)
        agent.method = "byoc"  # its prompt is baked into the member's own code
        db.commit()
    finally:
        db.close()
    res = client.post(_url(run_id, rec_id, "revise"),
                      json={"system_prompt": "edited", "expected_version": "2"})
    assert res.status_code == 400 and res.json()["code"] == "recommendation.accept_not_harness"


def test_a_legacy_acceptance_starts_from_the_text_its_publish_recorded(client, monkeypatch):
    # an acceptance recorded before the published text was kept: the spec snapshot of its
    # deployment holds what went live (edits included)
    agent_id, run_id, rec_id, _ = _accepted(client, monkeypatch, edited="published with edits")
    from app.evaluation.models import EvalRecommendation

    db = SessionLocal()
    try:
        row = db.get(EvalRecommendation, rec_id)
        accepted = dict(row.accepted)
        accepted.pop("system_prompt")
        row.accepted = accepted
        db.commit()
    finally:
        db.close()
    assert _rows(client, run_id)[0]["final_prompt"] == "published with edits"
    assert agent_id
