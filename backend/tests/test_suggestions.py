"""Suggested test questions (T09): grounding, fallback, cache — Bedrock stubbed."""

import json

import pytest

from app.services import suggestions

SPEC = {"name": "sq-agent", "method": "harness", "system_prompt": "You answer HR policy questions."}


class FakeBedrock:
    def __init__(self, text=None, error=None):
        self.text, self.error, self.calls = text, error, []

    def converse(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return {"output": {"message": {"content": [{"text": self.text}]}}}


@pytest.fixture(autouse=True)
def fresh_cache():
    suggestions.reset_cache()
    yield
    suggestions.reset_cache()


class Ctx:
    def __init__(self, bedrock):
        self.bedrock = bedrock

    def client(self, service, **_):
        assert service == "bedrock-runtime"
        return self.bedrock


def test_model_questions_are_used_and_capped():
    qs = [f"Question number {i}?" for i in range(8)]
    bedrock = FakeBedrock("Sure: " + json.dumps(qs))
    out = suggestions.suggested_questions(Ctx(bedrock), "a1", SPEC)
    assert out["source"] == "model"
    assert out["questions"] == qs[:5]
    call = bedrock.calls[0]
    # a cap with room for a reasoning model's thinking (GLM-5.3 used 294 for four)
    assert 300 < call["inferenceConfig"]["maxTokens"] <= 2000
    assert call["modelId"] == "global.anthropic.claude-sonnet-5-5"  # the default
    # Claude 5 rejects `temperature` with a ValidationException
    assert "temperature" not in call["inferenceConfig"]
    assert "HR policy" in call["messages"][0]["content"][0]["text"]


def test_platform_model_follows_settings(monkeypatch):
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "platform_model_id", "global.zai.glm-5.3")
    bedrock = FakeBedrock(json.dumps(["a one?", "b two?", "c three?"]))
    suggestions.suggested_questions(Ctx(bedrock), "a9", SPEC)
    assert bedrock.calls[0]["modelId"] == "global.zai.glm-5.3"
    assert set(bedrock.calls[0]["inferenceConfig"]) == {"maxTokens"}


def test_kb_documents_and_tools_ground_the_prompt(monkeypatch):
    monkeypatch.setattr(
        suggestions.knowledge, "sample_document_names", lambda ws, kb, limit=8: ["leave-policy.pdf"]
    )
    spec = {
        **SPEC,
        "knowledge_bases": [{"kb_id": "KB1", "name": "HR docs", "description": ""}],
        "tools": [{"type": "builtin", "name": "code-interpreter"}],
    }
    bedrock = FakeBedrock(json.dumps(["a one?", "b two?", "c three?"]))
    suggestions.suggested_questions(Ctx(bedrock), "a2", spec)
    text = bedrock.calls[0]["messages"][0]["content"][0]["text"]
    assert "leave-policy.pdf" in text and "code-interpreter" in text


def test_model_failure_falls_back():
    out = suggestions.suggested_questions(Ctx(FakeBedrock(error=RuntimeError("boom"))), "a3", SPEC)
    assert out["source"] == "fallback"
    assert 3 <= len(out["questions"]) <= 5


def test_garbage_output_falls_back_and_zh_language():
    out = suggestions.suggested_questions(Ctx(FakeBedrock("no json here")), "a4", SPEC, "zh-CN")
    assert out["source"] == "fallback"
    assert out["questions"] == suggestions.FALLBACKS["zh-CN"]


def test_nothing_to_go_on_skips_the_model():
    bedrock = FakeBedrock(json.dumps(["a?", "b?", "c?"]))
    out = suggestions.suggested_questions(Ctx(bedrock), "a5", {})
    assert out["source"] == "fallback" and bedrock.calls == []


def test_cache_avoids_second_call_and_spec_change_invalidates():
    bedrock = FakeBedrock(json.dumps(["a one?", "b two?", "c three?"]))
    ctx = Ctx(bedrock)
    suggestions.suggested_questions(ctx, "a6", SPEC)
    suggestions.suggested_questions(ctx, "a6", SPEC)
    assert len(bedrock.calls) == 1
    suggestions.suggested_questions(ctx, "a6", {**SPEC, "system_prompt": "changed"})
    assert len(bedrock.calls) == 2
    suggestions.suggested_questions(ctx, "a6", SPEC, force=True)
    assert len(bedrock.calls) == 3


def test_cache_expires(monkeypatch):
    bedrock = FakeBedrock(json.dumps(["a one?", "b two?", "c three?"]))
    now = [1000.0]
    monkeypatch.setattr(suggestions, "_now", lambda: now[0])
    suggestions.suggested_questions(Ctx(bedrock), "a7", SPEC)
    now[0] += suggestions.CACHE_TTL_SECONDS + 1
    suggestions.suggested_questions(Ctx(bedrock), "a7", SPEC)
    assert len(bedrock.calls) == 2


def test_endpoint_never_500s_and_404s_unknown(client, monkeypatch):
    import app.routers.agents as agents_router

    monkeypatch.setattr(agents_router, "start_deploy_async", lambda jid: None)

    def boom(*a, **k):
        raise RuntimeError("no aws")

    monkeypatch.setattr(suggestions, "_generate", boom)
    agent = client.post("/api/agents", json=SPEC).json()["agent"]
    res = client.get(f"/api/agents/{agent['id']}/suggested-questions?lang=zh")
    assert res.status_code == 200
    assert res.json()["source"] == "fallback"
    assert res.json()["questions"] == suggestions.FALLBACKS["zh-CN"]
    assert client.get("/api/agents/nope/suggested-questions").status_code == 404
