"""Overview evaluation tiles (N12 E2-3): ``eval_pass_rate`` / ``eval_runs`` from the run
ledger, restored next to the N1 tiles; WB-001 adds the ``evaluation`` service row the
home card reads (completed runs > 0, detail ``{"runs": n, "pass_rate": p}`` — structured
since r7-e so the console formats it in the viewer's language)."""

from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.evaluation.models import EvalRun
from app.models.ledger import Agent
from app.routers import overview as overview_mod


def _seed_runs():
    db = SessionLocal()
    agent = Agent(workspace_id=DEFAULT_WORKSPACE_ID, name="ov-agent", method="zip_runtime",
                  status="active", spec={})
    db.add(agent)
    db.flush()
    db.add(EvalRun(
        workspace_id=DEFAULT_WORKSPACE_ID, agent_id=agent.id, agent_name=agent.name,
        status="completed",
        scores=[{"evaluatorId": "Builtin.Helpfulness", "score": 1.0, "count": 3},
                {"evaluatorId": "Builtin.Correctness", "score": 0.5, "count": 3}],
    ))
    db.add(EvalRun(  # not completed — never counted
        workspace_id=DEFAULT_WORKSPACE_ID, agent_id=agent.id, agent_name=agent.name,
        status="failed", scores=[{"evaluatorId": "Builtin.Helpfulness", "score": 0.0}],
    ))
    db.add(EvalRun(  # another workspace — never counted
        workspace_id="other-ws", agent_id=agent.id, agent_name=agent.name,
        status="completed", scores=[{"evaluatorId": "Builtin.Helpfulness", "score": 0.0}],
    ))
    db.commit()
    db.close()


def test_overview_carries_eval_pass_rate_and_run_count(client, monkeypatch):
    _seed_runs()
    monkeypatch.setattr(overview_mod, "_traces_active", lambda _ws: False)
    overview_mod._cache.clear()
    body = client.get("/api/overview").json()
    assert body["eval_pass_rate"] == 0.75
    assert body["eval_runs"] == 1
    assert set(body["services"]) == {
        "gateway", "memory", "registry", "policy", "evaluation", "observability",
    }
    # WB-001: the evaluation row lights up with the first completed run and carries the
    # same numbers as the tile — the card can no longer say "run your first evaluation"
    # next to a 15-run / 85 % tile
    assert body["services"]["evaluation"] is True
    assert body["service_detail"]["evaluation"] == {"runs": 1, "pass_rate": 0.75}


def test_overview_without_runs_reads_null_pass_rate(client, monkeypatch):
    monkeypatch.setattr(overview_mod, "_traces_active", lambda _ws: False)
    overview_mod._cache.clear()
    body = client.get("/api/overview").json()
    assert body["eval_pass_rate"] is None and body["eval_runs"] == 0
    assert body["services"]["evaluation"] is False
    assert body["service_detail"]["evaluation"] is None


def test_eval_detail_is_structured_not_english():
    # r7-e: the console pluralises/translates ("15 runs · 85 % pass" / "15 次运行 · 通过率 85 %")
    assert overview_mod._eval_detail(None, 0) is None
    assert overview_mod._eval_detail(None, 3) == {"runs": 3, "pass_rate": None}  # no scores yet
    assert overview_mod._eval_detail(0.85, 15) == {"runs": 15, "pass_rate": 0.85}
    assert overview_mod._eval_detail(1.0, 1) == {"runs": 1, "pass_rate": 1.0}


def test_online_quality_route_exists_with_the_contract_keys(client):
    body = client.get("/api/overview/online-quality").json()
    assert set(body) == {"range", "mean", "scores", "sessions", "agents", "configs",
                         "evaluators", "cached"}
    assert body["configs"] == 0 and body["cached"] is False
