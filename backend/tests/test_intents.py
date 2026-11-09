"""T33 -- intent view: model grouping, the documented fallback, unanswered detection."""

import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.core.db import SessionLocal
from app.main import create_app
from app.models.ledger import ChatFeedback
from app.services import intents
from tests.selfservice_util import seed_turn
from tests.test_guardrail import _ctx
from tests.test_share_links import make_agent


@pytest.fixture(autouse=True)
def fresh_cache():
    intents.reset_cache()
    yield
    intents.reset_cache()


@pytest.fixture
def client():
    return TestClient(create_app())


class FakeConverse:
    def __init__(self, reply=None, error=None):
        self.reply, self.error, self.calls = reply, error, []

    def converse(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        text = self.reply if isinstance(self.reply, str) else json.dumps(self.reply)
        return {"output": {"message": {"content": [{"text": text}]}}}


def use_model(monkeypatch, fake):
    ctx = _ctx(SimpleNamespace(), SimpleNamespace())
    object.__setattr__(ctx, "client", lambda service, *a, **k: fake)
    monkeypatch.setattr(type(SessionLocal().get_bind()), "__module__", "x", raising=False)
    return ctx


def seed(agent_id):
    leave1 = seed_turn(agent_id, "How many leave days do I have?", "You have 12 days.")
    leave2 = seed_turn(agent_id, "how many leave days do I have", "12 days left.")
    payroll = seed_turn(agent_id, "When is payday?", "I couldn't find that in my documents.")
    err = seed_turn(agent_id, "Reset my badge", "boom", role="error")
    return leave1, leave2, payroll, err


def view(client, monkeypatch, fake, **params):
    from app.routers import issues as issues_router

    monkeypatch.setattr(issues_router.intents, "_model_groups",
                        lambda ws, turns, lang: intents._parse(json.dumps(fake), len(turns))
                        if fake is not None else (_ for _ in ()).throw(RuntimeError("boom")))
    res = client.get("/api/intents", params={"days": 7, **params})
    assert res.status_code == 200, res.text
    return res.json()


def test_model_grouping_with_volume_thumbs_and_unanswered(client, monkeypatch):
    agent_id = make_agent()
    (s1, m1), (s2, m2), (s3, m3), (s4, _) = seed(agent_id)
    # sessions come newest first: err(0) payroll(1) leave2(2) leave1(3)
    reply = {"clusters": [{"label": "Leave balance", "members": [2, 3]},
                          {"label": "Payday", "members": [1]}], "unanswered": [1]}
    db = SessionLocal()
    try:
        db.add(ChatFeedback(workspace_id="default", agent_id=agent_id, session_id=s1,
                            message_id=m1, verdict="down", actor="u", source="console"))
        db.add(ChatFeedback(workspace_id="default", agent_id=agent_id, session_id=s2,
                            message_id=m2, verdict="up", actor="u", source="console"))
        db.commit()
    finally:
        db.close()
    body = view(client, monkeypatch, reply)
    assert body["source"] == "model" and body["sessions_considered"] == 4
    rows = {r["label"]: r for r in body["clusters"]}
    leave = rows["Leave balance"]
    assert (leave["volume"], leave["thumbs_down"], leave["rated"], leave["down_rate"]) == (
        2, 1, 2, 0.5)
    assert rows["Payday"]["down_rate"] is None  # unrated is not "0% bad"
    # the model left the error turn out of every cluster: it lands in "Other questions"
    assert rows["Other questions"]["volume"] == 1
    # thumbs-down sessions sort first inside a cluster
    assert leave["sessions"][0]["status"] == "down"
    unanswered = {u["session_id"]: u for u in body["unanswered"]}
    assert set(unanswered) == {s3, s4}  # refusal phrase + error turn
    assert unanswered[s3]["cluster"] == "Payday" and unanswered[s3]["message_id"] == m3
    assert unanswered[s4]["message_id"] is None


def test_the_fallback_groups_recurring_questions_when_the_model_fails(client, monkeypatch):
    agent_id = make_agent()
    seed(agent_id)
    body = view(client, monkeypatch, None)
    assert body["source"] == "fallback"
    labels = [r["label"] for r in body["clusters"]]
    assert labels[0].lower().startswith("how many leave days")  # the recurring question
    assert body["clusters"][0]["volume"] == 2
    assert "Other questions" in labels
    assert len(body["unanswered"]) == 2  # the deterministic check still finds them


def test_unusable_model_output_falls_back_too():
    for bad in ("not json", "{}", '{"clusters": [{"label": "x", "members": [99]}]}',
                '{"clusters": "nope"}'):
        assert intents._parse(bad, 3) is None


def test_the_grouping_is_cached_and_refresh_bypasses_it(client, monkeypatch):
    agent_id = make_agent()
    seed(agent_id)
    fake = FakeConverse({"clusters": [{"label": "All", "members": [0, 1, 2, 3]}]})
    from app.routers import issues as issues_router

    monkeypatch.setattr(issues_router.intents, "_model_groups",
                        lambda ws, turns, lang: fake.converse() and intents._parse(
                            json.dumps(fake.reply), len(turns)))
    for _ in range(2):
        assert client.get("/api/intents").status_code == 200
    assert len(fake.calls) == 1
    client.get("/api/intents", params={"refresh": "true"})
    assert len(fake.calls) == 2


def test_the_real_converse_call_goes_through_the_client_funnel(monkeypatch):
    fake = FakeConverse({"clusters": [{"label": "Leave", "members": [0]}], "unanswered": []})
    ctx = _ctx(SimpleNamespace(), SimpleNamespace())
    seen = {}

    def client_for(service, *a, **k):
        seen["service"] = service
        return fake

    object.__setattr__(ctx, "client", client_for)
    turn = intents.Turn("s", "a", 1, "How many days?", "12", False, False)
    groups, flagged = intents._model_groups(ctx, [turn], "zh-CN")
    assert seen["service"] == "bedrock-runtime" and groups[0]["label"] == "Leave"
    assert "Simplified Chinese" in fake.calls[0]["system"][0]["text"]
    # Claude 5 rejects `temperature` with a ValidationException
    assert "temperature" not in fake.calls[0]["inferenceConfig"]
    assert fake.calls[0]["modelId"] == "global.anthropic.claude-sonnet-5-5"  # the default


def test_the_platform_model_follows_settings(monkeypatch):
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "platform_model_id", "global.zai.glm-5.3")
    fake = FakeConverse({"clusters": [{"label": "Leave", "members": [0]}], "unanswered": []})
    ctx = _ctx(SimpleNamespace(), SimpleNamespace())
    object.__setattr__(ctx, "client", lambda service, *a, **k: fake)
    turn = intents.Turn("s", "a", 1, "How many days?", "12", False, False)
    intents._model_groups(ctx, [turn], "en")
    assert fake.calls[0]["modelId"] == "global.zai.glm-5.3"
    # GLM (Converse) gets the same family-neutral request: maxTokens only
    assert set(fake.calls[0]["inferenceConfig"]) == {"maxTokens"}


def test_rule_answers_and_presets_are_not_unanswered(client, monkeypatch):
    agent_id = make_agent()
    seed_turn(agent_id, "q", "I don't know", answered_by="rule:1")
    body = view(client, monkeypatch, {"clusters": [{"label": "Q", "members": [0]}]})
    assert body["unanswered"] == []


def test_an_empty_window_answers_without_calling_the_model(client):
    make_agent()
    body = client.get("/api/intents").json()
    assert body["clusters"] == [] and body["unanswered"] == [] and body["sessions_considered"] == 0


def test_the_output_budget_leaves_room_for_claude_5_reasoning():
    # Reasoning tokens count against maxTokens; at 1800 a 70-session prompt
    # returned only reasoning and no JSON, so the view always fell back.
    assert intents.MAX_OUTPUT_TOKENS >= 8000
