"""Audit 7.6: editing a SCORING field of a custom evaluator that an ENABLED online config or a
running canary scores with is 409 ``evaluator.in_use`` with the referrers; a description edit
stays allowed; an administrator may force it (the version then moves, P7.1)."""

from __future__ import annotations

from tests import test_visibility as tv
from tests.test_visibility import _canary, _online_config

app, admin, ada = tv.app, tv.admin, tv.ada

INSTRUCTIONS = "Rate the tone of {context}"
BODY = {"instructions": INSTRUCTIONS, "level": "SESSION", "description": "tone"}


def _judge(client) -> str:
    res = client.post("/api/eval/evaluators", json={"name": "locked_judge", **BODY})
    assert res.status_code == 201, res.text
    return res.json()["evaluator_id"]


def _put(client, evaluator_id: str, force: bool = False, **over):
    url = f"/api/eval/evaluators/{evaluator_id}" + ("?force=true" if force else "")
    return client.put(url, json={**BODY, **over})


def test_a_scoring_edit_under_an_enabled_online_config_is_refused(admin, ada):
    ada_id, ada_client = ada
    judge = _judge(ada_client)
    agent = tv._active_agent("lock-agent", ada_id)
    _online_config("cfg-on", "on", ada_id, agent_id=agent, evaluators=[judge])
    res = _put(ada_client, judge, instructions="Rate the clarity of {context}")
    assert res.status_code == 409, res.text
    body = res.json()
    assert body["code"] == "evaluator.in_use"
    assert [(r["kind"], r["id"]) for r in body["detail"]["referrers"]] == [
        ("online_config", "cfg-on")]
    assert "edit with ?force=true" in body["message"]
    # every scoring field locks: level, model, rating scale
    assert _put(ada_client, judge, level="TRACE").status_code == 409
    assert _put(ada_client, judge, rating_scale=[
        {"value": 1.0, "label": "y", "definition": "y"},
        {"value": 0.0, "label": "n", "definition": "n"}]).status_code == 409
    # a description edit is not a scoring edit
    ok = _put(ada_client, judge, description="tone, reworded")
    assert ok.status_code == 200, ok.text
    assert ok.json()["description"] == "tone, reworded"


def test_a_disabled_config_or_a_finished_canary_does_not_lock(ada):
    ada_id, ada_client = ada
    judge = _judge(ada_client)
    agent = tv._active_agent("lock-agent-2", ada_id)
    _online_config("cfg-off", "off", ada_id, agent_id=agent, evaluators=[judge],
                   execution_status="DISABLED")
    _canary(ada_id, champion_agent_id=agent, challenger_agent_id=agent, status="cleaned",
            artifacts={"online_evaluators": [judge]})
    res = _put(ada_client, judge, instructions="Rate the clarity of {context}")
    assert res.status_code == 200, res.text


def test_a_running_canary_locks_and_an_administrator_may_force(admin, ada):
    ada_id, ada_client = ada
    judge = _judge(ada_client)
    agent = tv._active_agent("lock-agent-3", ada_id)
    canary = _canary(ada_id, champion_agent_id=agent, challenger_agent_id=agent,
                     artifacts={"online_evaluators": [judge]})
    edit = {"instructions": "Rate the clarity of {context}"}
    res = _put(ada_client, judge, **edit)
    assert res.status_code == 409
    assert [(r["kind"], r["id"]) for r in res.json()["detail"]["referrers"]] == [
        ("canary", canary)]
    # a member's force is refused; an administrator's goes through and bumps the version
    member = _put(ada_client, judge, force=True, **edit)
    assert member.status_code == 403 and member.json()["code"] == "auth.forbidden"
    before = admin.get(f"/api/eval/evaluators/{judge}").json()["version"]
    forced = _put(admin, judge, force=True, **edit)
    assert forced.status_code == 200, forced.text
    assert forced.json()["instructions"] == edit["instructions"]
    assert admin.get(f"/api/eval/evaluators/{judge}").json()["version"] == before + 1
