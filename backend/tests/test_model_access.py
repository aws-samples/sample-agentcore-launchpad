"""A turn whose model call the agent's role may not make is named, and final.

The raw texts below are the ones measured live (2026-11-04, us-west-2) on a
zip_runtime and a harness agent whose execution role lost its ``BedrockModels``
grant: the code runtime reported a ``RuntimeError`` (the console answered a bare
500 on the sync route and offered a manual retry in chat); the harness streamed a
``runtimeClientError`` that was classed as transient (the console retried it by
itself, said "usually temporary; send the turn again", evaluations replayed it).
"""

from botocore.exceptions import ClientError, EventStreamError

from app.core.errors import AppError
from app.evaluation.service import transient_invoke_error
from app.routers.chat import retry_hint
from app.routers.public_api import public_stream_event
from app.services import chat as chat_service
from app.services.invoke import upstream_invoke_error
from app.services.model_access import MODEL_ACCESS_DENIED, model_access_denied

_DENIED = (
    "An error occurred (AccessDeniedException) when calling the ConverseStream "
    "operation: User: arn:aws:sts::123456789012:assumed-role/launchpad-agent-mt-c78147a5/"
    "BedrockAgentCore-5ac7a186 is not authorized to perform: "
    "bedrock:InvokeModelWithResponseStream on resource: "
    "arn:aws:bedrock:us-west-2:123456789012:inference-profile/global.zai.glm-5.3 "
    "because no identity-based policy allows the bedrock:InvokeModelWithResponseStream "
    "action"
)


def _runtime_error() -> RuntimeError:
    """A code runtime (zip_runtime / container / byoc): runtime.py's form."""
    return RuntimeError(f"runtime returned error: {_DENIED}")


def _harness_error() -> EventStreamError:
    """A Harness: the streamed runtimeClientError."""
    return EventStreamError(
        {"Error": {"Code": "runtimeClientError", "Message": _DENIED}}, "InvokeHarness",
    )


def test_both_measured_forms_are_named_with_the_model():
    streamed = RuntimeError(f"runtime client error: {_DENIED}")
    for exc in (_runtime_error(), _harness_error(), streamed):
        err = model_access_denied(exc)
        assert err is not None and err.code == MODEL_ACCESS_DENIED
        assert err.status_code == 502
        assert err.detail["model"] == "global.zai.glm-5.3"
        assert err.detail["action"] == "bedrock:InvokeModelWithResponseStream"
        assert err.detail["retryable"] is False
        assert "glm-5.3" in err.message


def test_model_access_not_enabled_and_mantle_are_named_too():
    no_access = ClientError(
        {"Error": {"Code": "AccessDeniedException",
                   "Message": "You don't have access to the model with the specified model ID."}},
        "ConverseStream",
    )
    assert model_access_denied(no_access).code == MODEL_ACCESS_DENIED
    mantle = RuntimeError(
        "runtime returned error: AccessDeniedException: User: arn:aws:sts::1:assumed-role/r/s "
        "is not authorized to perform: bedrock-mantle:CreateInference on resource: "
        "arn:aws:bedrock-mantle:us-east-1:1:project/default")
    assert model_access_denied(mantle).detail["action"] == "bedrock-mantle:CreateInference"


def test_other_denials_and_failures_are_left_alone():
    kb = RuntimeError(
        "runtime returned error: An error occurred (AccessDeniedException) when calling the "
        "Retrieve operation: User: arn:aws:sts::1:assumed-role/r/s is not authorized to "
        "perform: bedrock:Retrieve on resource: arn:aws:bedrock:us-west-2:1:knowledge-base/K")
    invoke = ClientError(
        {"Error": {"Code": "AccessDeniedException", "Message": "User: x is not authorized to "
                   "perform: bedrock-agentcore:InvokeAgentRuntime on resource: arn:aws:y"}},
        "InvokeAgentRuntime",
    )
    throttled = RuntimeError("runtime returned error: An error occurred (ThrottlingException) "
                             "when calling the ConverseStream operation: Too many tokens")
    for exc in (kb, invoke, throttled, ValueError("bug")):
        assert model_access_denied(exc) is None


def test_public_leaves_the_role_and_account_out():
    err = model_access_denied(_harness_error(), public=True)
    assert "upstream_message" not in err.detail
    assert "123456789012" not in str(err.detail) + err.message
    assert "123456789012" in model_access_denied(_harness_error()).detail["upstream_message"]


def test_sync_routes_answer_the_named_error_not_a_retry_or_a_500():
    for exc in (_runtime_error(), _harness_error()):
        err = upstream_invoke_error(exc)
        assert err is not None and err.code == MODEL_ACCESS_DENIED
        assert err.detail["retryable"] is False
    assert "upstream_message" not in upstream_invoke_error(_harness_error(), public=True).detail


def test_evaluations_and_canaries_do_not_replay_it():
    assert transient_invoke_error(_harness_error()) is False
    assert transient_invoke_error(RuntimeError(f"runtime client error: {_DENIED}")) is False
    assert transient_invoke_error(model_access_denied(_runtime_error())) is False
    # a harness hiccup without a model refusal is still transient
    assert transient_invoke_error(RuntimeError("runtime client error: boom")) is True


def _stream_error_event(monkeypatch, exc: BaseException) -> dict:
    agent = type("A", (), {"id": "a1", "name": "n", "method": "zip_runtime", "spec": {},
                           "resource_id": "r", "workspace_id": "default"})()

    def boom(*_a, **_kw):
        raise exc
        yield  # pragma: no cover

    monkeypatch.setattr(chat_service, "invoke_agent_events", boom)
    monkeypatch.setattr(chat_service, "refuse_assistant_session", lambda *_a, **_kw: None)
    monkeypatch.setattr(chat_service.guardrail, "guardrail_config", lambda _spec: None)
    monkeypatch.setattr(chat_service.answer_rules, "match", lambda *_a, **_kw: None,
                        raising=False)
    monkeypatch.setattr(chat_service.inbound_auth_service, "is_jwt_mode", lambda _a: False)
    events = list(chat_service.chat_stream(agent, "hi", session_id="s" * 40,
                                           workspace=None, status_events=True))
    return [e for e in events if e["event"] == "error"][0]["data"]


def test_the_chat_stream_names_it_and_offers_no_retry(monkeypatch):
    for exc in (_runtime_error(), _harness_error()):
        data = _stream_error_event(monkeypatch, exc)
        assert data["code"] == MODEL_ACCESS_DENIED
        assert data["transient"] is False
        assert retry_hint(data, produced=False) == {
            "category": "config", "manual": False, "auto": False}
        public = public_stream_event({"event": "error", "data": data})["data"]
        assert public["code"] == MODEL_ACCESS_DENIED
        assert "upstream_message" not in public.get("detail", {})
        assert "123456789012" not in str(public)


def test_an_app_error_passes_through_only_when_it_is_this_one():
    named = model_access_denied(_runtime_error())
    assert model_access_denied(named) is named
    assert model_access_denied(AppError("agent.upstream_error", "x")) is None
