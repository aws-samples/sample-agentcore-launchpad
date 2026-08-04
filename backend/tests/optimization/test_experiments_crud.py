"""Experiments CRUD + gateway traffic — adapted from agentcore_eva_opt
tests/test_experiments_crud.py and test_gateway_traffic.py."""

from unittest.mock import MagicMock

import app.optimization.routers as opt_router
import app.optimization.service as svc
from app.core.db import SessionLocal
from app.models.ledger import Agent
from app.optimization.models import Experiment


def test_experiment_create_and_persisted_stages(client, monkeypatch):
    db = SessionLocal()
    agent = Agent(name="exp-agent", method="zip_runtime", status="active",
                  arn="arn:rt", resource_id="rt-1", spec={"system_prompt": "x"})
    db.add(agent)
    db.commit()

    monkeypatch.setattr(
        opt_router.service, "start_experiment",
        lambda a: _make_exp(a),
    )
    monkeypatch.setattr(
        opt_router.readiness,
        "project_readiness",
        lambda *_args, **_kwargs: {"state": "ready"},
    )
    res = client.post("/api/experiments", json={"agent_id": agent.id})
    assert res.status_code == 201
    exp_id = res.json()["id"]

    detail = client.get(f"/api/experiments/{exp_id}").json()
    assert detail["stages"][:6] == [
        "recommend", "bundles", "gateway", "abtest", "traffic", "verdict",
    ]
    assert detail["artifacts"]["bundles"]["control"]["arn"] == "arn:c"
    db.close()


def _make_exp(agent_row):
    db = SessionLocal()
    exp = Experiment(
        name="EXP-t", agent_id=agent_row.id, agent_name=agent_row.name,
        status="ready", stage="verdict",
        artifacts={"bundles": {"control": {"arn": "arn:c"},
                               "treatment": {"arn": "arn:t"}}},
    )
    db.add(exp)
    db.commit()
    db.refresh(exp)
    db.close()
    return exp


def test_second_experiment_blocked_while_one_runs(client):
    db = SessionLocal()
    agent = Agent(name="exp-agent-2", method="zip_runtime", status="active",
                  arn="arn:rt", resource_id="rt-2", spec={})
    db.add(agent)
    db.add(Experiment(name="EXP-busy", agent_id="other", agent_name="other",
                      status="running", stage="traffic"))
    db.commit()
    res = client.post("/api/experiments", json={"agent_id": agent.id})
    assert res.status_code == 409
    assert res.json()["code"] == "experiment.already_running"
    db.close()


def test_harness_agents_rejected_for_experiments(client):
    db = SessionLocal()
    agent = Agent(name="h-agent", method="harness", status="active", arn="arn:h",
                  spec={})
    db.add(agent)
    db.commit()
    res = client.post("/api/experiments", json={"agent_id": agent.id})
    assert res.status_code == 400
    assert res.json()["code"] == "experiment.method_unsupported"
    db.close()


def test_gateway_traffic_signs_and_collects_sessions(monkeypatch):
    sent: list[dict] = []

    class FakeResponse:
        status_code = 200

    def poster(url, content, headers):
        sent.append({"url": url, "headers": headers})
        return FakeResponse()

    signed = []
    import app.services.agentcore.gateway as gw

    monkeypatch.setattr(
        gw.boto3, "Session",
        lambda region_name=None: MagicMock(
            get_credentials=lambda: MagicMock(
                get_frozen_credentials=lambda: "frozen-creds"
            )
        ),
    )
    result = svc.send_gateway_traffic(
        "https://gw.example", "expv1", ["p1", "p2", "p3"],
        poster=poster,
        signer=lambda creds, region, req: signed.append(creds),
    )
    assert result["sent"] == 3 and result["failed"] == 0
    assert len(result["session_ids"]) == 3
    assert all(s["url"] == "https://gw.example/expv1/invocations" for s in sent)
    assert all(
        "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id" in s["headers"] for s in sent
    )
    assert signed == ["frozen-creds"] * 3
