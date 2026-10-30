"""The control-plane section of the shared ledger scan (``ledger_invariants.scan_control_plane``,
folded into ``scan_ledger``): clean on a fresh ledger, and each orphan class it names is
reported."""

from __future__ import annotations

from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.models.assistant import AssistantProposal
from app.optimization.models import ExperimentAssignment, RuntimeCanary
from tests.property.ledger_invariants import scan_control_plane, scan_ledger


def test_fresh_ledger_is_clean(client):
    assert scan_ledger(SessionLocal) == []


def test_orphans_are_reported(client):
    db = SessionLocal()
    try:
        db.add(AssistantProposal(workspace_id=DEFAULT_WORKSPACE_ID, conversation_id="gone",
                                 revision=1, source="model", content={}, content_hash="0" * 64,
                                 status="draft", created_by="river"))
        db.add(RuntimeCanary(workspace_id=DEFAULT_WORKSPACE_ID, name="c",
                             champion_agent_id="no-agent", challenger_agent_id="no-agent",
                             champion_agent_name="a", challenger_agent_name="a",
                             status="cleaned", running_action="teardown", artifacts={}))
        db.add(ExperimentAssignment(workspace_id=DEFAULT_WORKSPACE_ID, owner_kind="canary",
                                    owner_id="no-canary", session_id="s" * 33, arm="C"))
        db.commit()
        found = scan_control_plane(db)
    finally:
        db.close()
    assert any("orphan assistant proposal rows of conversation gone" == f for f in found)
    assert any("names missing agent no-agent" in f for f in found)
    assert any("unknown running_action 'teardown'" in f for f in found)
    assert any("missing owner no-canary" in f for f in found)
