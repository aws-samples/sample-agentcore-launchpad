"""Overview endpoint: live tiles + service health from resources and ledger."""

from datetime import UTC, datetime, timedelta

import app.routers.overview as overview_mod
from app.core.db import SessionLocal
from app.evaluation.models import EvalRun
from app.models.ledger import Agent, ChatSession


def _seed():
    db = SessionLocal()
    agent = Agent(name="ov-agent", method="harness", status="active", spec={})
    db.add(agent)
    db.flush()
    db.add(ChatSession(agent_id=agent.id, session_id="s1", turns=2))
    stale = ChatSession(agent_id=agent.id, session_id="s2", turns=1)
    stale.last_at = datetime.now(UTC) - timedelta(days=3)
    db.add(stale)
    db.add(
        EvalRun(
            agent_id=agent.id,
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
    monkeypatch.setattr(overview_mod, "console_list", lambda: records)
    monkeypatch.setattr(overview_mod, "_traces_active", lambda: True)
    overview_mod._cache.update(assets_at=0.0, assets=None)

    res = client.get("/api/overview")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["registry_assets"] == {"agents": 1, "tools": 1, "skills": 1, "total": 3}
    assert body["active_sessions"] == 1  # 3-day-old session excluded
    assert body["eval_pass_rate"] == 0.75
    assert body["eval_runs"] == 1
    assert body["services"]["observability"] is True
    # gateway/memory/registry/policy reflect config resource presence (bool)
    assert set(body["services"]) == {
        "gateway", "memory", "registry", "policy", "evaluation", "observability",
    }


def test_overview_registry_failure_falls_back_to_cache(client, monkeypatch):
    def boom():
        raise RuntimeError("registry down")

    monkeypatch.setattr(overview_mod, "console_list", boom)
    monkeypatch.setattr(overview_mod, "_traces_active", lambda: False)
    overview_mod._cache.update(
        assets_at=0.0, assets={"agents": 7, "tools": 0, "skills": 0}
    )
    res = client.get("/api/overview")
    assert res.status_code == 200
    assert res.json()["registry_assets"]["agents"] == 7


def test_overview_exposes_registry_unavailable_reason(client, monkeypatch):
    monkeypatch.setattr(
        overview_mod,
        "get_settings",
        lambda: type(
            "Settings",
            (),
            {
                "resources": {
                    "registry_id": "",
                    "registry_unavailable_reason": "blocked by account policy",
                }
            },
        )(),
    )
    monkeypatch.setattr(overview_mod, "_traces_active", lambda: False)
    overview_mod._cache.update(assets_at=0.0, assets=None)

    response = client.get("/api/overview")

    assert response.status_code == 200
    body = response.json()
    assert body["services"]["registry"] is False
    assert body["service_detail"]["registry"] == "blocked by account policy"
