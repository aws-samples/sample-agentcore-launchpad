"""Overview endpoint: live tiles + service health from resources and ledger."""

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import app.routers.overview as overview_mod
from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.evaluation.models import EvalRun
from app.models.ledger import Agent, ChatSession
from app.services import governance
from tests.conftest import set_default_resources, ws_ctx


def _seed():
    db = SessionLocal()
    agent = Agent(
        workspace_id=DEFAULT_WORKSPACE_ID,
        name="ov-agent", method="harness", status="active", spec={})
    db.add(agent)
    db.flush()
    db.add(ChatSession(
        workspace_id=DEFAULT_WORKSPACE_ID,
        agent_id=agent.id, session_id="s1", turns=2))
    stale = ChatSession(
        workspace_id=DEFAULT_WORKSPACE_ID,
        agent_id=agent.id, session_id="s2", turns=1)
    stale.last_at = datetime.now(UTC) - timedelta(days=3)
    db.add(stale)
    db.add(
        EvalRun(
            workspace_id=DEFAULT_WORKSPACE_ID, agent_id=agent.id,
            agent_name=agent.name,
            status="completed",
            scores=[{"evaluatorId": "Builtin.Helpfulness", "score": 1.0},
                    {"evaluatorId": "Builtin.Correctness", "score": 0.5}],
        )
    )
    db.commit()
    db.close()


def test_overview_tiles_and_health(client, monkeypatch):
    _seed()
    records = [
        {"recordId": "r1", "descriptorType": "A2A", "status": "APPROVED"},
        {"recordId": "r2", "descriptorType": "MCP", "status": "DRAFT"},
        {"recordId": "r3", "descriptorType": "AGENT_SKILLS", "status": "APPROVED"},
        {"recordId": "r4", "descriptorType": "A2A", "status": "DEPRECATED"},
    ]
    monkeypatch.setattr(overview_mod, "console_list", lambda _ws: records)
    monkeypatch.setattr(overview_mod, "_traces_active", lambda _ws: True)
    monkeypatch.setattr(
        overview_mod, "attached_policy_engine_id", lambda _c, _ws: "pe-live"
    )
    overview_mod._cache.clear()

    res = client.get("/api/overview")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["registry_assets"] == {"agents": 1, "tools": 1, "skills": 1, "total": 3}
    assert body["active_sessions"] == 1  # 3-day-old session excluded
    assert body["eval_pass_rate"] == 0.75
    assert body["eval_runs"] == 1
    assert body["services"]["observability"] is True
    assert body["services"]["policy"] is True
    assert body["service_detail"]["policy"] == "pe-live"
    assert set(body["services"]) == {
        "gateway", "memory", "registry", "policy", "evaluation", "observability",
    }


def test_overview_registry_failure_falls_back_to_cache(client, monkeypatch):
    def boom(_ws):
        raise RuntimeError("registry down")

    monkeypatch.setattr(overview_mod, "console_list", boom)
    monkeypatch.setattr(overview_mod, "_traces_active", lambda _ws: False)
    monkeypatch.setattr(
        overview_mod, "attached_policy_engine_id", lambda _c, _ws: ""
    )
    overview_mod._cache["default"] = {
        "assets_at": 0.0, "assets": {"agents": 7, "tools": 0, "skills": 0},
        "traces_at": 0.0, "traces": None,
    }
    res = client.get("/api/overview")
    assert res.status_code == 200
    assert res.json()["registry_assets"]["agents"] == 7


def test_overview_exposes_registry_unavailable_reason(client, monkeypatch):
    set_default_resources({
        "registry_id": "",
        "registry_unavailable_reason": "blocked by account policy",
    })
    monkeypatch.setattr(overview_mod, "_traces_active", lambda _ws: False)
    monkeypatch.setattr(
        overview_mod, "attached_policy_engine_id", lambda _c, _ws: ""
    )
    overview_mod._cache.clear()

    response = client.get("/api/overview")

    assert response.status_code == 200
    body = response.json()
    assert body["services"]["registry"] is False
    assert body["service_detail"]["registry"] == "blocked by account policy"


def test_policy_health_reads_live_gateway_attachment(monkeypatch):
    control = MagicMock()
    control.get_gateway.return_value = {
        "policyEngineConfiguration": {
            "arn": (
                "arn:aws:bedrock-agentcore:us-west-2:111:"
                "policy-engine/launchpad_pe-abc"
            )
        }
    }
    workspace = ws_ctx({"gateway_id": "launchpad-gw-1"})
    governance._engine_cache.clear()

    assert governance.attached_policy_engine_id(control, workspace) == "launchpad_pe-abc"
    control.get_gateway.assert_called_once_with(
        gatewayIdentifier="launchpad-gw-1"
    )
