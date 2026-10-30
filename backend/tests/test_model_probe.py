"""D21 (M1-b): the probe + credential hooks behind Settings → Model Providers, hermetic.

A scripted OpenAI-compatible endpoint (`httpx.MockTransport`), a stub Global Bedrock runtime
speaking the N12-bug11 `ValidationException` texts, fake SSM / Identity clients and a fake
resolver drive `services/model_probe.ModelProbeHooks` directly and through the admin routes
(`POST /{id}/models` probe-on-add, re-probe `apply`, `/credential`). The SSRF guard matrix,
the sanitiser and a no-leak scan (a planted key in every vendor message, in SSM and in the
hub cache must never appear in a response, journal row, ledger row, log line or resource
map) are here too. No AWS, no DNS, no network (C8)."""

from __future__ import annotations

import io
import json
import logging
from datetime import UTC, datetime

import httpx
import pytest
from botocore.exceptions import ClientError
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.db import SessionLocal
from app.core.errors import AppError
from app.main import create_app
from app.models.ledger import Workspace
from app.models.model_registry import Model, ModelProvider
from app.schemas import model_catalog
from app.services import aws_clients, model_registry
from app.services.model_probe import (
    HostBlockedError,
    ModelProbeHooks,
    address_blocked,
    guard_base_url,
)
from app.services.model_probe_openai import sanitize

PREFIX = "/api/settings/model-providers"
ACME_URL = "https://api.siliconflow.cn/acme/v1"
KEY = "sk-live-LEAKED-0123456789abcdef"
PUBLIC_IP = "93.184.216.34"
CHAT = "acme-org/Foo-7B"
EMBED = "acme-org/Embed-1k"


# ── fakes ────────────────────────────────────────────────────────────────────────────
class FakeEndpoint:
    """A scripted OpenAI-compatible server. Flags decide what the model 'supports'; every
    error body carries the planted KEY so the sanitiser is exercised on each path."""

    def __init__(self, *, tools=True, streaming=True, temperature=True, top_p=True,
                 auth_ok=True, models_endpoint=True, tool_choice_required=True,
                 redirect=False) -> None:
        self.tools = tools  # True | False (400) | None (200 without tool_calls)
        self.streaming = streaming
        self.temperature = temperature
        self.top_p = top_p
        self.auth_ok = auth_ok
        self.models_endpoint = models_endpoint
        self.tool_choice_required = tool_choice_required
        self.redirect = redirect
        self.requests: list[httpx.Request] = []

    def _error(self, status: int, message: str) -> httpx.Response:
        return httpx.Response(status, json={"error": {"message": f"{message} (key {KEY})",
                                                      "type": "invalid_request_error"}})

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.redirect:
            return httpx.Response(302, headers={"location": "https://evil.example/steal"})
        if request.headers.get("authorization") != f"Bearer {KEY}":
            return self._error(401, "missing bearer")
        if not self.auth_ok:
            return self._error(401, "invalid api key")
        path = request.url.path
        if path.endswith("/models"):
            if not self.models_endpoint:
                return self._error(404, "no such route")
            return httpx.Response(200, json={"data": [
                {"id": CHAT, "object": "model", "context_length": 128000,
                 "max_output_tokens": 8192},
                {"id": EMBED, "object": "model"},
            ]})
        body = json.loads(request.content or b"{}")
        if path.endswith("/embeddings"):
            if body.get("model") != EMBED:
                return self._error(404, f"model {body.get('model')} not found")
            return httpx.Response(200, json={"data": [{"embedding": [0.1] * 1024}]})
        assert path.endswith("/chat/completions"), path
        if body.get("model") != CHAT:
            return self._error(404, f"The model `{body.get('model')}` does not exist")
        if "temperature" in body and not self.temperature:
            return self._error(400, "This model doesn't support the temperature field")
        if "top_p" in body and not self.top_p:
            return self._error(400, "top_p is not supported by this model")
        if body.get("stream"):
            if not self.streaming:
                return self._error(400, "stream is not supported")
            return httpx.Response(
                200, headers={"content-type": "text/event-stream"},
                content=b'data: {"choices":[{"delta":{"content":"OK"}}]}\n\ndata: [DONE]\n\n')
        if body.get("tools"):
            if body.get("tool_choice") == "required" and not self.tool_choice_required:
                return self._error(400, "tool_choice 'required' is not supported")
            if self.tools is False:
                return self._error(400, "tools are not supported by this model")
            if self.tools is None:
                return httpx.Response(200, json={"choices": [{"message": {
                    "role": "assistant", "content": "OK"}}]})
            return httpx.Response(200, json={"choices": [{"message": {
                "role": "assistant", "content": None,
                "tool_calls": [{"id": "c1", "type": "function",
                                "function": {"name": "ping", "arguments": "{}"}}]}}]})
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant",
                                                                  "content": "OK"}}]})


class FakeSsm:
    def __init__(self, params=None, error=None) -> None:
        self.params = dict(params or {})
        self.error = error
        self.calls: list[str] = []

    def get_parameter(self, Name, WithDecryption=False):  # noqa: N803
        self.calls.append(Name)
        assert WithDecryption is True
        if self.error:
            raise ClientError({"Error": {"Code": self.error, "Message": f"denied {KEY}"}},
                              "GetParameter")
        if Name not in self.params:
            raise ClientError({"Error": {"Code": "ParameterNotFound", "Message": "nope"}},
                              "GetParameter")
        value, version = self.params[Name]
        return {"Parameter": {"Name": Name, "Value": value, "Version": version,
                              "LastModifiedDate": datetime(2026, 10, 14, 12, 0, tzinfo=UTC)}}


class FakeControl:
    def __init__(self, existing=(), refuse=False) -> None:
        self.providers = set(existing)
        self.refuse = refuse
        self.calls: list[tuple[str, str]] = []

    def _out(self, name):
        return {"name": name,
                "credentialProviderArn": f"arn:aws-cn:bedrock-agentcore:cn-northwest-1:111:"
                                         f"token-vault/default/apikeycredentialprovider/{name}",
                "apiKeySecretArn": f"arn:aws-cn:secretsmanager:cn-northwest-1:111:secret:{name}"}

    def get_api_key_credential_provider(self, name):
        self.calls.append(("get", name))
        if name not in self.providers:
            raise ClientError({"Error": {"Code": "ResourceNotFoundException", "Message": "no"}},
                              "GetApiKeyCredentialProvider")
        return self._out(name)

    def create_api_key_credential_provider(self, name, apiKey):  # noqa: N803
        self.calls.append(("create", name))
        if self.refuse:
            raise ClientError({"Error": {"Code": "ValidationException",
                                         "Message": f"bad key {apiKey}"}},
                              "CreateApiKeyCredentialProvider")
        self.providers.add(name)
        return self._out(name)

    def update_api_key_credential_provider(self, name, apiKey):  # noqa: N803
        self.calls.append(("update", name))
        return self._out(name)

    def delete_api_key_credential_provider(self, name):  # N17-obs228
        self.calls.append(("delete", name))
        if name not in self.providers:
            raise ClientError({"Error": {"Code": "ResourceNotFoundException", "Message": "no"}},
                              "DeleteApiKeyCredentialProvider")
        self.providers.discard(name)
        return {}


class FakeBedrockRuntime:
    """`bedrock-runtime` speaking the live N12-bug11 texts (Kimi K3 rejects temperature)."""

    def __init__(self, *, temperature_rejected=True, missing=False, tools=True) -> None:
        self.temperature_rejected = temperature_rejected
        self.missing = missing
        self.tools = tools
        self.calls: list[tuple] = []

    def converse(self, modelId, messages, inferenceConfig, toolConfig=None):  # noqa: N803
        self.calls.append(("converse", modelId, dict(inferenceConfig), toolConfig))
        if self.missing:
            raise ClientError({"Error": {"Code": "ValidationException",
                                         "Message": "The provided model identifier is invalid."}},
                              "Converse")
        if "temperature" in inferenceConfig and self.temperature_rejected:
            raise ClientError({"Error": {"Code": "ValidationException", "Message":
                                         "This model doesn't support the temperature field. "
                                         "Remove temperature and try again."}}, "Converse")
        if toolConfig:
            return {"stopReason": "tool_use" if self.tools else "end_turn", "output": {}}
        return {"stopReason": "end_turn", "output": {"message": {"content": [{"text": "OK"}]}}}

    def converse_stream(self, modelId, messages, inferenceConfig):  # noqa: N803
        self.calls.append(("stream", modelId))
        return {"stream": iter([{"messageStart": {"role": "assistant"}}])}

    def invoke_model(self, modelId, body, contentType, accept):  # noqa: N803
        self.calls.append(("invoke", modelId, json.loads(body)))
        return {"body": io.BytesIO(json.dumps({"embedding": [0.1] * 1024}).encode())}


# ── fixtures ─────────────────────────────────────────────────────────────────────────
@pytest.fixture(autouse=True)
def _hermetic(monkeypatch):
    monkeypatch.delenv("LAUNCHPAD_ACME_API_KEY", raising=False)
    monkeypatch.delenv("LAUNCHPAD_SILICONFLOW_API_KEY", raising=False)
    aws_clients.invalidate_provider_api_key()
    yield
    model_registry.set_probe_hooks(None)
    model_catalog.reset()
    aws_clients.invalidate_provider_api_key()


@pytest.fixture
def db():
    session = SessionLocal()
    model_registry.ensure_seeded(session)
    yield session
    session.close()


@pytest.fixture
def acme(db):
    return model_registry.create_provider(db, {"name": "acme", "kind": "openai_compatible",
                                               "label": "Acme", "base_url": ACME_URL}, "admin")


def _hooks(endpoint=None, *, key=KEY, resolver=None, **over) -> ModelProbeHooks:
    kwargs = dict(
        transport=httpx.MockTransport(endpoint) if endpoint is not None else None,
        resolver=resolver or (lambda host: [PUBLIC_IP]),
        key_resolver=lambda name: key,
        bearer_check=lambda: "bedrock-bearer",
        bedrock_runtime=lambda region: FakeBedrockRuntime(),
        ssm_factory=lambda ctx: FakeSsm(),
        control_factory=lambda ctx: FakeControl(),
    )
    kwargs.update(over)
    return ModelProbeHooks(**kwargs)


def _changes(db, operation):
    return [c for c in model_registry.list_changes(db, limit=200) if c.operation == operation]


def _bedrock(db) -> ModelProvider:
    return db.scalar(select(ModelProvider).where(ModelProvider.name == "bedrock"))


# ── provider probe ───────────────────────────────────────────────────────────────────
def test_probe_provider_lists_models_persists_and_journals(db, acme):
    endpoint = FakeEndpoint()
    result = _hooks(endpoint).probe_provider(db, acme, "admin")
    assert set(result) == {"state", "checked_at", "latency_ms", "models_seen", "error"}
    assert result["state"] == "ok" and result["models_seen"] == [CHAT, EMBED]
    assert result["error"] is None and result["checked_at"].endswith("+00:00")
    assert [r.url.path for r in endpoint.requests] == ["/acme/v1/models"]
    db.expire_all()
    assert db.get(ModelProvider, acme.id).probe == result
    [change] = _changes(db, "provider.probe")
    assert change.before["probe"]["state"] == "never" and change.after["probe"] == result
    assert model_catalog.current().providers["acme"].probe_state == "ok"


def test_probe_provider_journals_an_audit_row_without_a_catalog_version_bump(db, acme):
    """D21-obs6: live, two admin `POST …/probe` calls (bedrock ok, siliconflow failed) wrote
    `provider.probe` rows and advanced `catalog_version` 1 → 3 with no catalog change. The
    rows stay in `/changes`; the version, the published snapshot and a second process's 30 s
    stamp check do not move."""
    version = model_registry.stamp(db)
    assert version == model_catalog.current().version
    ok = _hooks(FakeEndpoint()).probe_provider(db, acme, "admin")
    failed = _hooks(FakeEndpoint(auth_ok=False)).probe_provider(db, acme, "admin")
    assert (ok["state"], failed["state"]) == ("ok", "failed")
    rows = _changes(db, "provider.probe")
    assert len(rows) == 2 and all(r.id > version for r in rows)
    assert rows[0].after["probe"]["state"] == "failed" and rows[1].after["probe"]["state"] == "ok"
    assert model_registry.list_changes(db, limit=1)[0].operation == "provider.probe"
    assert model_registry.stamp(db) == version == model_catalog.current().version
    assert model_registry._refresher() is None
    # the probing process republished its own snapshot (probe_state is read from it)
    assert model_catalog.current().providers["acme"].probe_state == "failed"


def test_probe_provider_auth_failure_is_failed_and_the_key_is_redacted(db, acme):
    result = _hooks(FakeEndpoint(auth_ok=False)).probe_provider(db, acme, "admin")
    assert result["state"] == "failed" and result["models_seen"] == []
    assert result["error"].startswith("auth_failed: HTTP 401")
    assert KEY not in result["error"] and "[redacted]" in result["error"]
    assert len(result["error"]) <= 300
    assert model_registry.warnings(db)[0]["code"] == "probe_failed"


def test_probe_provider_without_a_models_endpoint_is_ok_with_a_note(db, acme):
    result = _hooks(FakeEndpoint(models_endpoint=False)).probe_provider(db, acme, "admin")
    assert result["state"] == "ok" and result["models_seen"] == []
    assert result["error"].startswith("models_endpoint_unsupported (HTTP 404)")


def test_probe_provider_without_a_credential_fails_offline(db, acme):
    endpoint = FakeEndpoint()

    def no_key(name):
        raise RuntimeError(f"LAUNCHPAD_{name.upper()}_API_KEY is not set — store it as SSM "
                           f"parameter launchpad-{name}-api-key")

    result = _hooks(endpoint, key_resolver=no_key).probe_provider(db, acme, "admin")
    assert result["state"] == "failed" and endpoint.requests == []
    assert result["error"].startswith("credential_unavailable: LAUNCHPAD_ACME_API_KEY")
    assert "launchpad-acme-api-key" in result["error"]


def test_probe_provider_refuses_a_redirect(db, acme):
    result = _hooks(FakeEndpoint(redirect=True)).probe_provider(db, acme, "admin")
    assert result["state"] == "failed" and "redirect refused" in result["error"]


def test_bedrock_provider_probe_is_bearer_reachability_only(db):
    bedrock = _bedrock(db)
    runtime_calls: list = []
    hooks = _hooks(bedrock_runtime=lambda region: runtime_calls.append(region))
    result = hooks.probe_provider(db, bedrock, "admin")
    assert result["state"] == "ok" and runtime_calls == []  # no network at all
    assert result["models_seen"] == ["global.zai.glm-5.3", "global.moonshotai.kimi-k3"]

    def no_bearer():
        raise RuntimeError("AWS_BEARER_TOKEN_BEDROCK is not set — the hub reaches Global Bedrock")

    failed = _hooks(bearer_check=no_bearer).probe_provider(db, bedrock, "admin")
    assert failed["state"] == "failed"
    assert failed["error"].startswith("credential_unavailable: AWS_BEARER_TOKEN_BEDROCK")


# ── model probe (pre-insert) ─────────────────────────────────────────────────────────
def test_chat_probe_settles_every_field_within_seven_requests(db, acme):
    endpoint = FakeEndpoint()
    result = _hooks(endpoint).probe_model(db, acme, CHAT, "chat", None, apply=False,
                                          actor="admin")
    assert set(result) == {"findings", "errors", "record"}
    assert result["findings"] == {"exists": True, "tools": True, "streaming": True,
                                  "temperature": True, "top_p": True}
    assert result["errors"] == {}
    record = result["record"]
    assert set(record) == {"checked_at", "latency_ms", "findings", "errors", "raw_hint"}
    assert record["raw_hint"] == {"max_output_tokens": 8192, "context_length": 128000}
    assert record["findings"] == result["findings"]
    paths = [r.url.path for r in endpoint.requests]
    assert paths[0] == "/acme/v1/models" and len(paths) <= 7
    bodies = [json.loads(r.content) for r in endpoint.requests[1:]]
    assert all(b["max_tokens"] == 16 and b["model"] == CHAT for b in bodies)
    assert all(r.headers["authorization"] == f"Bearer {KEY}" for r in endpoint.requests)
    # the insert-less call wrote nothing
    assert _changes(db, "model.probe") == [] and _changes(db, "model.create") == []


def test_chat_probe_maps_the_n12_bug11_signature_to_temperature_false(db, acme):
    endpoint = FakeEndpoint(temperature=False, top_p=False, streaming=False, tools=False)
    result = _hooks(endpoint).probe_model(db, acme, CHAT, "chat", None, apply=False,
                                          actor="admin")
    assert result["findings"] == {"exists": True, "tools": False, "streaming": False,
                                  "temperature": False, "top_p": False}
    assert result["errors"] == {}


def test_chat_probe_retries_tool_choice_auto_and_marks_an_ambiguous_tool_result_unsettled(
    db, acme
):
    strict = FakeEndpoint(tool_choice_required=False)  # required → 400, auto → tool call
    result = _hooks(strict).probe_model(db, acme, CHAT, "chat", None, apply=False, actor="admin")
    assert result["findings"]["tools"] is True
    choices = [json.loads(r.content).get("tool_choice") for r in strict.requests
               if r.url.path.endswith("/chat/completions") and b"tools" in r.content]
    assert choices == ["required", "auto"]

    silent = FakeEndpoint(tools=None)  # 200 without tool_calls on both attempts
    result = _hooks(silent).probe_model(db, acme, CHAT, "chat", None, apply=False, actor="admin")
    assert "tools" not in result["findings"] and result["findings"]["exists"] is True
    assert "unverified" in result["errors"]["tools"]
    assert result["record"]["findings"]["tools"] is None  # stored as unsettled


def test_chat_probe_reports_a_missing_model_and_stops(db, acme):
    endpoint = FakeEndpoint()
    result = _hooks(endpoint).probe_model(db, acme, "acme-org/Gone", "chat", None, apply=False,
                                          actor="admin")
    assert result["findings"] == {"exists": False}
    assert result["errors"]["exists"].startswith("model_missing: HTTP 404")
    assert KEY not in json.dumps(result)
    assert all(e.startswith("skipped:") for f, e in result["errors"].items() if f != "exists")
    completions = [r for r in endpoint.requests if r.url.path.endswith("/chat/completions")]
    assert len(completions) == 1  # no tools / streaming / params requests after a miss


def test_embedding_probe_reads_the_dimension(db, acme):
    result = _hooks(FakeEndpoint()).probe_model(db, acme, EMBED, "embedding", None, apply=False,
                                                actor="admin")
    assert result["findings"] == {"exists": True, "embedding_dimension": 1024}
    missing = _hooks(FakeEndpoint()).probe_model(db, acme, "acme-org/NoEmbed", "embedding", None,
                                                 apply=False, actor="admin")
    assert missing["findings"] == {"exists": False}


def test_bedrock_chat_probe_with_the_stub_runtime(db):
    bedrock = _bedrock(db)
    runtime = FakeBedrockRuntime(temperature_rejected=True)
    hooks = _hooks(bedrock_runtime=lambda region: runtime)
    result = hooks.probe_model(db, bedrock, "global.moonshotai.kimi-k3", "chat", None,
                               apply=False, actor="admin")
    assert result["findings"] == {"exists": True, "tools": True, "streaming": True,
                                  "temperature": False, "top_p": True}
    assert result["errors"] == {}
    assert [c[0] for c in runtime.calls] == ["converse", "converse", "stream", "converse",
                                             "converse"]
    assert runtime.calls[0][2] == {"maxTokens": 256}
    assert runtime.calls[-1][2] == {"maxTokens": 256, "topP": 0.9}

    gone = _hooks(bedrock_runtime=lambda region: FakeBedrockRuntime(missing=True))
    result = gone.probe_model(db, bedrock, "global.nope", "chat", None, apply=False, actor="admin")
    assert result["findings"] == {"exists": False}
    assert result["errors"]["exists"].startswith("model_missing: ValidationException")

    embed = hooks.probe_model(db, bedrock, "amazon.titan-embed-text-v2:0", "embedding", None,
                              apply=False, actor="admin")
    assert embed["findings"] == {"exists": True, "embedding_dimension": 1024}

    def no_bearer(region):
        raise RuntimeError("AWS_BEARER_TOKEN_BEDROCK is not set")

    offline = _hooks(bedrock_runtime=no_bearer).probe_model(
        db, bedrock, "global.zai.glm-5.3", "chat", None, apply=False, actor="admin")
    assert offline["findings"] == {}
    assert offline["errors"]["exists"].startswith("credential_unavailable")


# ── the routes: probe-on-add and re-probe ────────────────────────────────────────────
@pytest.fixture
def api():
    client = TestClient(create_app())
    return client


def _acme_via_api(client):
    response = client.post(PREFIX, json={"name": "acme", "kind": "openai_compatible",
                                         "label": "Acme", "base_url": ACME_URL})
    assert response.status_code == 201, response.text
    return response.json()


def test_probe_on_add_fills_the_capabilities_and_a_miss_is_422(api):
    endpoint = FakeEndpoint(temperature=False)
    model_registry.set_probe_hooks(_hooks(endpoint))
    acme = _acme_via_api(api)
    created = api.post(f"{PREFIX}/{acme['id']}/models", json={"api_model_name": CHAT})
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["source"] == "probed"
    assert body["tools"] is True and body["streaming"] is True
    assert body["inference_params"] == {"temperature": False, "top_p": True}
    for field in ("tools", "streaming", "temperature", "top_p"):
        assert body["capability_sources"][field] == "probed"
    assert body["capability_sources"]["vision"] == "manual"  # not probed → kind default
    assert body["probe"]["raw_hint"] == {"max_output_tokens": 8192, "context_length": 128000}
    assert body["probe"]["findings"]["temperature"] is False and body["probe"]["errors"] == {}
    # the catalog follows: Kimi-style rejection reaches `supported_model_config`
    from app.schemas.agent import supported_model_config

    assert "temperature" not in supported_model_config(body["model_id"], {"temperature": 0.2,
                                                                          "max_tokens": 512})
    missing = api.post(f"{PREFIX}/{acme['id']}/models", json={"api_model_name": "acme-org/Gone"})
    assert missing.status_code == 422 and missing.json()["code"] == "model.probe_failed"
    assert missing.json()["detail"]["findings"] == {"exists": False}
    assert missing.json()["detail"]["errors"]["exists"].startswith("model_missing")
    assert KEY not in missing.text
    # a partial probe (tools unsettled) still inserts, with the record kept
    model_registry.set_probe_hooks(_hooks(FakeEndpoint(tools=None)))
    partial = api.post(f"{PREFIX}/{acme['id']}/models",
                       json={"api_model_name": CHAT, "label": "again"})
    assert partial.status_code == 409  # same Launchpad id — the first insert stands
    embed = api.post(f"{PREFIX}/{acme['id']}/models",
                     json={"api_model_name": EMBED, "kind": "embedding"})
    assert embed.status_code == 201, embed.text
    assert embed.json()["embedding_dimension"] == 1024
    assert embed.json()["capability_sources"]["embedding_dimension"] == "probed"


def test_reprobe_diffs_and_apply_writes_only_non_manual_fields(api, db):
    endpoint = FakeEndpoint()
    model_registry.set_probe_hooks(_hooks(endpoint))
    acme = _acme_via_api(api)
    created = api.post(f"{PREFIX}/{acme['id']}/models", json={"api_model_name": CHAT}).json()
    # the admin pins tools manually; the endpoint then stops supporting tools + temperature
    pinned = api.put(f"{PREFIX}/{acme['id']}/models/{created['id']}", json={"tools": True})
    assert pinned.status_code == 200 and pinned.json()["capability_sources"]["tools"] == "manual"
    endpoint.tools = False
    endpoint.temperature = False
    dry = api.post(f"{PREFIX}/{acme['id']}/models/{created['id']}/probe", json={"apply": False})
    assert dry.status_code == 200, dry.text
    assert set(dry.json()) == {"findings", "diff", "errors", "raw_hint", "latency_ms", "applied"}
    assert dry.json()["applied"] is False
    assert dry.json()["diff"]["tools"] == {"stored": True, "probed": False, "source": "manual"}
    assert dry.json()["diff"]["temperature"] == {"stored": True, "probed": False,
                                                 "source": "probed"}
    assert "exists" not in dry.json()["diff"]
    after_dry = api.get(f"{PREFIX}").json()
    row = next(m for m in after_dry["models"] if m["id"] == created["id"])
    assert row["tools"] is True and row["inference_params"]["temperature"] is True  # unchanged
    assert len(_changes(db, "model.probe")) == 0
    version = model_registry.stamp(db)
    assert version == model_catalog.current().version  # D21-obs6: a dry re-probe → no bump

    applied = api.post(f"{PREFIX}/{acme['id']}/models/{created['id']}/probe",
                       json={"apply": True})
    assert applied.status_code == 200 and applied.json()["applied"] is True
    # D21-obs6: exactly one bump — the catalog-shaping `model.update` row that carries only
    # the field the probe really changed; the `model.probe` row is audit-only
    [update] = [c for c in _changes(db, "model.update") if c.note == "applied from probe"]
    assert update.before == {"temperature": True} and update.after == {"temperature": False}
    assert update.actor == "admin" and update.target_id == created["id"]
    # the audit rows still consume journal ids, so the version jumps to the update row's id
    assert model_registry.stamp(db) == update.id > version
    assert model_registry.stamp(db) == model_catalog.current().version
    assert [c.id for c in model_registry.list_changes(db, limit=50)
            if c.id > version and c.operation not in model_registry.AUDIT_ONLY_OPERATIONS
            ] == [update.id]  # exactly one catalog-shaping row
    assert model_registry._refresher() is None
    assert api.get("/api/capabilities").json()["catalog_version"] == update.id
    assert update.id > _changes(db, "model.probe")[0].id  # listed above the audit row
    row = next(m for m in api.get(PREFIX).json()["models"] if m["id"] == created["id"])
    assert row["tools"] is True  # manual wins
    assert row["inference_params"]["temperature"] is False  # probed field followed the probe
    assert row["capability_sources"]["temperature"] == "probed"
    assert row["source"] == "manual"  # a manual field exists → the row stays manual
    assert row["probe"]["findings"]["temperature"] is False
    [change] = _changes(db, "model.probe")
    assert change.before["inference_params"]["temperature"] is True
    assert change.after["inference_params"]["temperature"] is False
    assert KEY not in json.dumps([change.before, change.after])


def test_reprobe_with_nothing_to_write_still_records_the_probe(api, db):
    model_registry.set_probe_hooks(_hooks(FakeEndpoint()))
    acme = _acme_via_api(api)
    manual = api.post(f"{PREFIX}/{acme['id']}/models",
                      json={"api_model_name": CHAT, "probe": False,
                            "overrides": {"tools": False}}).json()
    result = api.post(f"{PREFIX}/{acme['id']}/models/{manual['id']}/probe",
                      json={"apply": True}).json()
    assert result["applied"] is False  # every field is manual → nothing written
    row = next(m for m in api.get(PREFIX).json()["models"] if m["id"] == manual["id"])
    assert row["tools"] is False and row["probe"]["findings"]["tools"] is True
    assert len(_changes(db, "model.probe")) == 1 and _changes(db, "model.update") == []


def test_reprobe_apply_that_changes_nothing_does_not_advance_the_catalog_version(api, db):
    """D21-obs6: an applied re-probe whose probed values equal the stored ones writes the
    record + the audit `model.probe` row but no `model.update` row — no version bump. Only a
    later re-probe that really changes a field bumps, and exactly once."""
    endpoint = FakeEndpoint()
    model_registry.set_probe_hooks(_hooks(endpoint))
    acme = _acme_via_api(api)
    created = api.post(f"{PREFIX}/{acme['id']}/models", json={"api_model_name": CHAT}).json()
    assert created["capability_sources"]["tools"] == "probed"
    version = model_registry.stamp(db)
    assert version == model_catalog.current().version
    same = api.post(f"{PREFIX}/{acme['id']}/models/{created['id']}/probe", json={"apply": True})
    assert same.status_code == 200, same.text
    assert same.json()["applied"] is True  # probed fields were (re)written …
    assert len(_changes(db, "model.probe")) == 1
    assert _changes(db, "model.update") == []  # … but nothing changed → no catalog row
    assert model_registry.stamp(db) == version == model_catalog.current().version
    assert model_registry._refresher() is None
    assert api.get("/api/capabilities").json()["catalog_version"] == version
    # the endpoint drops streaming + tools: one `model.update` row with both fields, one bump
    endpoint.streaming = False
    endpoint.tools = False
    changed = api.post(f"{PREFIX}/{acme['id']}/models/{created['id']}/probe",
                       json={"apply": True}).json()
    assert changed["applied"] is True and changed["findings"]["streaming"] is False
    assert len(_changes(db, "model.probe")) == 2
    [update] = _changes(db, "model.update")
    assert update.note == "applied from probe"
    assert update.before == {"tools": True, "streaming": True}
    assert update.after == {"tools": False, "streaming": False}
    assert model_registry.stamp(db) == update.id > version
    assert model_registry.stamp(db) == model_catalog.current().version
    assert [c.id for c in model_registry.list_changes(db, limit=50)
            if c.id > version and c.operation not in model_registry.AUDIT_ONLY_OPERATIONS
            ] == [update.id]  # exactly one catalog-shaping row
    assert KEY not in json.dumps([update.before, update.after])


# ── credentials (plan §4) ────────────────────────────────────────────────────────────
def test_register_credential_reads_ssm_once_hands_it_to_identity_and_records_names(
    db, acme, caplog, monkeypatch
):
    caplog.set_level(logging.INFO)
    ssm = FakeSsm({"launchpad-acme-api-key": (KEY, 3)})
    control = FakeControl()
    hooks = _hooks(ssm_factory=lambda ctx: ssm, control_factory=lambda ctx: control)
    result = hooks.register_credential(db, acme, "admin")
    assert result == {"configured": True, "created": True, "rotated": False,
                      "workspaces_updated": 1, "key_version": 3}
    assert ssm.calls == ["launchpad-acme-api-key"]
    assert control.calls == [("get", "launchpad-acme"), ("create", "launchpad-acme")]
    db.expire_all()
    row = db.get(ModelProvider, acme.id)
    assert row.key_version == 3 and row.key_modified_at is not None
    [change] = _changes(db, "provider.credential")
    assert change.after == {"configured": True, "created": True, "rotated": False,
                            "key_version": 3, "key_modified_at": "2026-10-14T12:00:00+00:00"}
    record = db.get(Workspace, "default").resources["model_credentials"]["acme"]
    assert record == {"name": "launchpad-acme", "configured": True, "key_version": 3,
                      "source": "ssm", "arn": record["arn"], "secret_arn": record["secret_arn"]}
    assert record["arn"].endswith("apikeycredentialprovider/launchpad-acme")
    assert model_registry.provider_dict(row)["key_version"] == 3
    assert KEY not in caplog.text and KEY not in json.dumps(model_registry.registry_dict(db))
    assert "registered from SSM parameter launchpad-acme-api-key" in caplog.text

    # rotation: the parameter moved to v4 → Identity updated in place, journal says rotated
    ssm.params["launchpad-acme-api-key"] = (KEY + "-v4", 4)
    again = hooks.register_credential(db, acme, "admin")
    assert again == {"configured": True, "created": False, "rotated": True,
                     "workspaces_updated": 1, "key_version": 4}
    assert control.calls[-1] == ("update", "launchpad-acme")
    db.expire_all()
    assert db.get(ModelProvider, acme.id).key_version == 4
    assert _changes(db, "provider.credential")[0].after["rotated"] is True
    assert db.get(Workspace, "default").resources["model_credentials"]["acme"]["key_version"] == 4


def test_register_credential_with_an_unchanged_key_is_a_no_op_and_keeps_the_version(
    db, acme, caplog
):
    """D21-obs5: clicking Register again while the SSM parameter is at the version the row
    already records → `rotated: false`, Identity only read, no second journal row, and the
    catalog version is where it was (credential rows never bump it)."""
    caplog.set_level(logging.INFO)
    ssm = FakeSsm({"launchpad-acme-api-key": (KEY, 3)})
    control = FakeControl()
    hooks = _hooks(ssm_factory=lambda ctx: ssm, control_factory=lambda ctx: control)
    version_before = model_registry.stamp(db)
    first = hooks.register_credential(db, acme, "admin")
    assert first["created"] is True and first["rotated"] is False
    assert model_registry.stamp(db) == version_before == model_catalog.current().version
    db.expire_all()
    again = hooks.register_credential(db, db.get(ModelProvider, acme.id), "admin")
    assert again == {"configured": True, "created": False, "rotated": False,
                     "workspaces_updated": 1, "key_version": 3}
    assert control.calls == [("get", "launchpad-acme"), ("create", "launchpad-acme"),
                             ("get", "launchpad-acme")]
    assert len(_changes(db, "provider.credential")) == 1
    assert model_registry.stamp(db) == version_before
    assert "up to date" in caplog.text and KEY not in caplog.text
    # the workspace record is still (re)written with the same version — idempotent
    assert db.get(Workspace, "default").resources["model_credentials"]["acme"]["key_version"] == 3


def test_register_credential_stamps_the_hub_and_already_configured_spokes_only(db, acme):
    """D21-obs3: the Identity provider was created in the HUB's account. A spoke that never
    received it must not be told `configured: true` (`provider_configured` reads the workspace
    map → a deploy would succeed and the runtime's GetResourceApiKey fail); a spoke whose own
    bootstrap registered it keeps its own ARN and learns the version."""
    from app.schemas.agent import provider_configured

    spoke_arn = ("arn:aws-cn:bedrock-agentcore:cn-north-1:333344445555:token-vault/default/"
                 "apikeycredentialprovider/launchpad-acme")
    db.add(Workspace(id="ws-two", name="two", account_id="222233334444", region="cn-north-1",
                     resources={"model_credentials": {"bedrock": {"configured": True}}}))
    db.add(Workspace(id="ws-three", name="three", account_id="333344445555",
                     region="cn-northwest-1",
                     resources={"model_credentials": {"acme": {
                         "name": "launchpad-acme", "arn": spoke_arn, "secret_arn": "s",
                         "configured": True}}}))
    db.commit()
    hooks = _hooks(ssm_factory=lambda ctx: FakeSsm({"launchpad-acme-api-key": (KEY, 1)}),
                   control_factory=lambda ctx: FakeControl())
    assert hooks.register_credential(db, acme, "admin")["workspaces_updated"] == 2  # hub + three
    db.expire_all()
    two = db.get(Workspace, "ws-two").resources["model_credentials"]
    assert two == {"bedrock": {"configured": True}}  # the spoke without the credential: untouched
    assert provider_configured("acme", db.get(Workspace, "ws-two").resources) is False
    three = db.get(Workspace, "ws-three").resources["model_credentials"]["acme"]
    assert three["arn"] == spoke_arn and three["secret_arn"] == "s"  # its own provider kept
    assert three["configured"] is True and three["key_version"] == 1 and three["source"] == "ssm"
    hub = db.get(Workspace, "default").resources["model_credentials"]["acme"]
    assert hub["arn"].endswith(":111:token-vault/default/apikeycredentialprovider/launchpad-acme")
    assert provider_configured("acme", db.get(Workspace, "default").resources) is True


def test_register_credential_error_codes(db, acme, caplog):
    caplog.set_level(logging.INFO)
    with pytest.raises(AppError) as exc:
        _hooks(ssm_factory=lambda ctx: FakeSsm()).register_credential(db, acme, "admin")
    assert exc.value.code == "model_provider.ssm_parameter_missing"
    assert exc.value.status_code == 422
    assert exc.value.detail == {"ssm_parameter": "launchpad-acme-api-key",
                                "env": "LAUNCHPAD_ACME_API_KEY", "empty": False}
    with pytest.raises(AppError) as exc:
        _hooks(ssm_factory=lambda ctx: FakeSsm(error="AccessDeniedException")
               ).register_credential(db, acme, "admin")
    assert exc.value.code == "model_provider.ssm_read_failed" and exc.value.status_code == 502
    assert exc.value.detail["code"] == "AccessDeniedException" and KEY not in str(exc.value)
    with pytest.raises(AppError) as exc:
        _hooks(ssm_factory=lambda ctx: FakeSsm({"launchpad-acme-api-key": (KEY, 1)}),
               control_factory=lambda ctx: FakeControl(refuse=True)
               ).register_credential(db, acme, "admin")
    assert exc.value.code == "model_provider.identity_failed" and exc.value.status_code == 502
    assert exc.value.detail == {"credential_provider": "launchpad-acme",
                                "code": "ValidationException"}
    assert KEY not in str(exc.value) and KEY not in json.dumps(exc.value.detail)
    db.expire_all()
    assert db.get(ModelProvider, acme.id).key_version is None
    assert _changes(db, "provider.credential") == []
    assert KEY not in caplog.text


def test_register_credential_for_the_builtin_bedrock_row_uses_the_host_env(db, monkeypatch):
    bedrock = _bedrock(db)
    monkeypatch.setenv("LAUNCHPAD_GLOBAL_BEDROCK_API_KEY", KEY)
    ssm = FakeSsm()
    control = FakeControl(existing={"launchpad-global-bedrock"})
    result = _hooks(ssm_factory=lambda ctx: ssm, control_factory=lambda ctx: control
                    ).register_credential(db, bedrock, "admin")
    assert result == {"configured": True, "created": False, "rotated": True,
                      "workspaces_updated": 1, "key_version": None}
    assert ssm.calls == []  # host_env row: the variable wins, SSM untouched
    assert control.calls[-1] == ("update", "launchpad-global-bedrock")
    db.expire_all()
    assert db.get(Workspace, "default").resources["model_credentials"]["bedrock"]["source"] == (
        "host_env")


def test_register_credential_over_http_and_the_capabilities_follow(api, db):
    ssm = FakeSsm({"launchpad-acme-api-key": (KEY, 2)})
    model_registry.set_probe_hooks(_hooks(ssm_factory=lambda ctx: ssm,
                                          control_factory=lambda ctx: FakeControl()))
    acme = _acme_via_api(api)
    response = api.post(f"{PREFIX}/{acme['id']}/credential", json={})
    assert response.status_code == 200, response.text
    assert response.json() == {"configured": True, "created": True, "rotated": False,
                               "workspaces_updated": 1, "key_version": 2, "last4": None}
    page = api.get(PREFIX).json()
    out = next(p for p in page["providers"] if p["name"] == "acme")
    assert out["key_version"] == 2 and out["key_modified_at"] == "2026-10-14T20:00:00+08:00"
    assert api.get("/api/capabilities").json()["model_providers"]["acme"]["configured"] is True
    assert KEY not in api.get(PREFIX).text and KEY not in api.get(f"{PREFIX}/changes").text
    assert KEY not in api.get("/api/capabilities").text


# ── hub-side key (`aws_clients.provider_api_key`) ────────────────────────────────────
def test_provider_api_key_resolution_order_and_cache(db, acme, monkeypatch):
    from app.services import workspace as ws

    ssm = FakeSsm({"launchpad-acme-api-key": (KEY, 1)})
    monkeypatch.setattr(aws_clients, "ssm_client", lambda ctx: ssm)
    assert aws_clients.provider_api_key("bedrock") == aws_clients.require_global_bedrock_bearer() \
        if aws_clients.GLOBAL_BEDROCK_BEARER_ENV in __import__("os").environ else True
    # no hub record → environment only, SSM never consulted (offline by construction)
    with pytest.raises(RuntimeError) as exc:
        aws_clients.provider_api_key("acme")
    assert "LAUNCHPAD_ACME_API_KEY" in str(exc.value)
    assert "launchpad-acme-api-key" in str(exc.value) and ssm.calls == []
    monkeypatch.setenv("LAUNCHPAD_ACME_API_KEY", " env-key ")
    assert aws_clients.provider_api_key("acme") == "env-key"
    assert aws_clients.require_provider_api_key("acme") == "env-key"
    assert ssm.calls == []
    # the hub record says `source: ssm` → SSM first, cached, invalidated on registration
    ctx = ws.WorkspaceContext(
        id="default", account_id="111122223333", region="cn-northwest-1",
        resources={"model_credentials": {"acme": {"configured": True, "source": "ssm"}}})
    monkeypatch.setattr(ws, "default_workspace_context", lambda: ctx)
    # N17-obs254 (rev-r22): the record alone does not license the read — the ROW must hold a
    # registered key too (what the registration writes as key_version); env still wins here
    assert aws_clients.provider_api_key("acme") == "env-key" and ssm.calls == []
    monkeypatch.delenv("LAUNCHPAD_ACME_API_KEY")
    with pytest.raises(RuntimeError, match="holds no registered key"):
        aws_clients.provider_api_key("acme")
    assert ssm.calls == []
    monkeypatch.setenv("LAUNCHPAD_ACME_API_KEY", " env-key ")
    acme.key_version = 1
    db.commit()
    model_registry.refresh(db)
    assert aws_clients.provider_api_key("acme") == KEY
    assert aws_clients.provider_api_key("acme") == KEY and ssm.calls == ["launchpad-acme-api-key"]
    ssm.params["launchpad-acme-api-key"] = ("rotated", 2)
    assert aws_clients.provider_api_key("acme") == KEY  # cached within the TTL
    aws_clients.invalidate_provider_api_key("acme")
    assert aws_clients.provider_api_key("acme") == "rotated"
    # SSM failing → the environment is the fallback; neither → a RuntimeError naming both
    aws_clients.invalidate_provider_api_key()
    ssm.error = "AccessDeniedException"
    assert aws_clients.provider_api_key("acme") == "env-key"
    monkeypatch.delenv("LAUNCHPAD_ACME_API_KEY")
    with pytest.raises(RuntimeError) as exc:
        aws_clients.provider_api_key("acme")
    assert "AccessDeniedException" in str(exc.value) and KEY not in str(exc.value)
    with pytest.raises(RuntimeError, match="not in the model registry"):
        aws_clients.provider_api_key("nope")


# ── SSRF guard matrix (plan §9.3) ────────────────────────────────────────────────────
@pytest.mark.parametrize("address", [
    "127.0.0.1", "10.0.0.5", "172.16.3.4", "192.168.1.1", "169.254.169.254", "100.64.0.1",
    "0.0.0.0", "224.0.0.1", "::1", "fe80::1", "fd00::1", "::ffff:127.0.0.1", "::ffff:10.1.2.3",
    "not-an-ip",
])
def test_blocked_addresses(address):
    assert address_blocked(address)


@pytest.mark.parametrize("address", [PUBLIC_IP, "8.8.8.8", "2606:4700::1111"])
def test_public_addresses_pass(address):
    assert not address_blocked(address)


def test_guard_matrix():
    public = lambda host: [PUBLIC_IP]  # noqa: E731
    assert guard_base_url("https://api.siliconflow.cn/v1/", resolver=public) == (
        "https://api.siliconflow.cn/v1")
    assert guard_base_url("https://gw.siliconflow.cn/v1", resolver=public)  # wildcard
    # N17-obs203: no operator allowlist by default — any public https host resolves and passes
    assert guard_base_url("https://api.ppinfra.com/v3/openai", resolver=public)
    for url, code in (("http://api.siliconflow.cn/v1", "model_provider.invalid_base_url"),
                      ("https://10.0.0.5/v1", "model_provider.invalid_base_url"),
                      ("https://[::1]/v1", "model_provider.invalid_base_url"),
                      ("https://user:pw@api.siliconflow.cn/v1", "model_provider.invalid_base_url")):
        with pytest.raises(AppError) as exc:
            guard_base_url(url, resolver=public)
        assert exc.value.code == code, url
    # lock mode (a non-empty allowlist) and the always-on denylist still refuse by name
    with pytest.raises(AppError) as exc:
        guard_base_url("https://evil.example.com/v1", allowed_hosts=["*.siliconflow.cn"],
                       resolver=public)
    assert exc.value.code == "model_provider.host_not_allowed"
    assert exc.value.detail["mode"] == "lock"
    with pytest.raises(AppError) as exc:
        guard_base_url("https://evil.example.com/v1", denied_hosts=["evil.example.com"],
                       resolver=public)
    assert exc.value.code == "model_provider.host_not_allowed"
    assert exc.value.detail["mode"] == "deny"
    # DNS rebinding: an allowlisted name pointing at a private / metadata / loopback address
    for private in ("10.0.0.5", "169.254.169.254", "127.0.0.1", "::1", "::ffff:192.168.0.9"):
        with pytest.raises(HostBlockedError, match="SSRF guard"):
            guard_base_url("https://api.siliconflow.cn/v1", resolver=lambda h, p=private: [p])
    with pytest.raises(HostBlockedError):  # one private address among public ones is enough
        guard_base_url("https://api.siliconflow.cn/v1", resolver=lambda h: [PUBLIC_IP, "10.0.0.1"])
    with pytest.raises(HostBlockedError, match="did not resolve"):
        def boom(host):
            raise OSError("no such host")
        guard_base_url("https://api.siliconflow.cn/v1", resolver=boom)
    with pytest.raises(HostBlockedError, match="no address"):
        guard_base_url("https://api.siliconflow.cn/v1", resolver=lambda h: [])
    with pytest.raises(HostBlockedError, match="internal name"):
        guard_base_url("https://models.corp.internal/v1", allowed_hosts=["*.internal"],
                       resolver=public)


def test_the_guard_runs_before_every_request_so_a_mid_probe_rebind_is_refused(db, acme):
    endpoint = FakeEndpoint()
    answers = iter([[PUBLIC_IP], ["10.0.0.5"]])

    def flipping(host):
        return next(answers, ["10.0.0.5"])

    result = _hooks(endpoint, resolver=flipping).probe_model(db, acme, CHAT, "chat", None,
                                                             apply=False, actor="admin")
    assert [r.url.path for r in endpoint.requests] == ["/acme/v1/models"]  # second refused
    assert result["findings"] == {}
    assert "SSRF guard" in result["errors"]["exists"] and "10.0.0.5" in result["errors"]["exists"]


def test_probe_provider_refuses_a_private_resolution_without_sending(db, acme):
    endpoint = FakeEndpoint()
    result = _hooks(endpoint, resolver=lambda h: ["169.254.169.254"]).probe_provider(
        db, acme, "admin")
    assert result["state"] == "failed" and endpoint.requests == []
    assert "169.254.169.254" in result["error"] and "SSRF guard" in result["error"]


def test_the_transport_follows_no_redirects_and_trusts_no_proxy_env(acme):
    from app.services.model_probe_openai import OpenAICompatibleProbe

    probe = OpenAICompatibleProbe(ACME_URL, KEY, guard=lambda u: u, deadline=1e12,
                                  transport=httpx.MockTransport(FakeEndpoint()))
    assert probe._client.follow_redirects is False
    assert probe._client.trust_env is False
    probe.close()


# ── sanitiser + no-leak scan ─────────────────────────────────────────────────────────
def test_sanitize_redacts_keys_bearer_tokens_and_query_strings_and_truncates():
    text = (f"Bearer {KEY} rejected; api_key={KEY}; sk-live-ABCDEFGH12 seen at "
            f"https://api.example/v1/models?api_key={KEY}&x=1 " + "x" * 600)
    out = sanitize(text, secrets=(KEY,))
    assert KEY not in out and "sk-live" not in out and "?api_key" not in out
    assert "[redacted]" in out and len(out) <= 300
    assert sanitize("max_tokens: 16 is fine", secrets=("",)) == "max_tokens: 16 is fine"


def test_no_response_journal_ledger_or_log_line_carries_the_key(api, db, caplog, monkeypatch):
    """The planted KEY is the hub's key, sits in every vendor error body, in SSM and in the
    Identity refusal — none of it may reach anything that leaves the process."""
    caplog.set_level(logging.DEBUG)
    monkeypatch.setenv("LAUNCHPAD_ACME_API_KEY", KEY)
    endpoint = FakeEndpoint(auth_ok=False)
    ssm = FakeSsm({"launchpad-acme-api-key": (KEY, 9)})
    model_registry.set_probe_hooks(_hooks(endpoint, ssm_factory=lambda ctx: ssm,
                                          control_factory=lambda ctx: FakeControl()))
    acme = _acme_via_api(api)
    texts = [api.post(f"{PREFIX}/{acme['id']}/probe").text,
             api.post(f"{PREFIX}/{acme['id']}/models", json={"api_model_name": CHAT}).text,
             api.post(f"{PREFIX}/{acme['id']}/credential", json={}).text]
    endpoint.auth_ok = True
    endpoint.tools = False
    endpoint.temperature = False
    created = api.post(f"{PREFIX}/{acme['id']}/models", json={"api_model_name": CHAT})
    texts.append(created.text)
    texts.append(api.post(f"{PREFIX}/{acme['id']}/models/{created.json()['id']}/probe",
                          json={"apply": True}).text)
    texts += [api.get(PREFIX).text, api.get(f"{PREFIX}/changes").text,
              api.get("/api/capabilities").text, api.get(f"{PREFIX}/{acme['id']}").text]
    for text in texts:
        assert KEY not in text and "sk-live" not in text
    assert KEY not in caplog.text
    db.expire_all()
    for row in db.scalars(select(ModelProvider)):
        assert KEY not in json.dumps(model_registry.provider_dict(row))
    for row in db.scalars(select(Model)):
        assert KEY not in json.dumps(row.probe or {})
    for ws_row in db.scalars(select(Workspace)):
        assert KEY not in json.dumps(ws_row.resources)
    for change in model_registry.list_changes(db, limit=200):
        assert KEY not in json.dumps([change.before, change.after])
    # and the hub cache holds it only in memory: nothing in the config file either
    from app.services import bootstrap

    assert KEY not in (bootstrap.CONFIG_FILE.read_text() if bootstrap.CONFIG_FILE.exists() else "")
