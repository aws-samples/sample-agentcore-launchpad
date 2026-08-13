"""Named-endpoint wrappers, invoke qualifier, and the shared sigv4_post helper.

Phase 1 of the production target-based canary — pure wrappers, no real AWS.
"""

import json
from types import SimpleNamespace

import pytest

from app.services import aws_clients
from app.services.agentcore import gateway as gw
from app.services.agentcore import runtime as rt
from tests.conftest import ws_ctx


# ─── named endpoint wrappers ─────────────────────────────────────────────────
class StubEndpointControl:
    """Records endpoint calls; get() replays a scripted status sequence."""

    def __init__(self, statuses=("READY",)):
        self.statuses = list(statuses)
        self.calls: list[tuple[str, dict]] = []

    def create_agent_runtime_endpoint(self, **kw):
        self.calls.append(("create", kw))
        return {"status": "CREATING", "agentRuntimeEndpointArn": "arn:ep"}

    def update_agent_runtime_endpoint(self, **kw):
        self.calls.append(("update", kw))
        return {"status": "UPDATING"}

    def get_agent_runtime_endpoint(self, **kw):
        self.calls.append(("get", kw))
        status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
        return {"status": status, "failureReason": "boom" if "FAILED" in status else None}

    def delete_agent_runtime_endpoint(self, **kw):
        self.calls.append(("delete", kw))
        return {}


def test_endpoint_wrappers_call_expected_methods_with_kwargs():
    stub = StubEndpointControl()
    rt.create_runtime_endpoint(stub, runtime_id="rt-1", endpoint_name="stable", version=3)
    rt.update_runtime_endpoint(stub, runtime_id="rt-1", endpoint_name="stable", version=4)
    rt.get_runtime_endpoint(stub, runtime_id="rt-1", endpoint_name="stable")
    rt.delete_runtime_endpoint(stub, runtime_id="rt-1", endpoint_name="treatment")

    assert [c[0] for c in stub.calls] == ["create", "update", "get", "delete"]
    assert stub.calls[0][1] == {
        "agentRuntimeId": "rt-1", "name": "stable", "agentRuntimeVersion": "3",
    }
    assert stub.calls[1][1] == {
        "agentRuntimeId": "rt-1", "endpointName": "stable", "agentRuntimeVersion": "4",
    }
    assert stub.calls[2][1] == {"agentRuntimeId": "rt-1", "endpointName": "stable"}
    assert stub.calls[3][1] == {"agentRuntimeId": "rt-1", "endpointName": "treatment"}


def test_wait_endpoint_ready_returns_on_ready():
    stub = StubEndpointControl(["CREATING", "CREATING", "READY"])
    seen: list[str | None] = []
    detail = rt.wait_endpoint_ready(
        stub, runtime_id="rt-1", endpoint_name="stable",
        sleeper=lambda _: None, on_status=seen.append,
    )
    assert detail["status"] == "READY"
    assert seen == ["CREATING", "READY"]


def test_wait_endpoint_ready_raises_on_failure():
    stub = StubEndpointControl(["UPDATE_FAILED"])
    with pytest.raises(RuntimeError, match="boom"):
        rt.wait_endpoint_ready(
            stub, runtime_id="rt-1", endpoint_name="stable", sleeper=lambda _: None,
        )


def test_wait_endpoint_ready_times_out():
    stub = StubEndpointControl(["CREATING"])  # never reaches READY
    with pytest.raises(TimeoutError):
        rt.wait_endpoint_ready(
            stub, runtime_id="rt-1", endpoint_name="stable",
            timeout_s=0, sleeper=lambda _: None,
        )


# ─── invoke qualifier ────────────────────────────────────────────────────────
class StubDataPlane:
    def __init__(self, body: bytes):
        self.body = body
        self.invoked_with: dict | None = None

    def invoke_agent_runtime(self, **kwargs):
        self.invoked_with = kwargs
        return {"response": SimpleNamespace(read=lambda: self.body)}


def test_invoke_runtime_text_omits_qualifier_by_default():
    stub = StubDataPlane(b'{"result": "ok"}')
    out = rt.invoke_runtime_text(stub, "arn:rt-1", "hi")
    assert out["text"] == "ok"
    assert "qualifier" not in stub.invoked_with


def test_invoke_runtime_text_passes_qualifier():
    stub = StubDataPlane(b'{"result": "ok"}')
    rt.invoke_runtime_text(stub, "arn:rt-1", "hi", qualifier="stable")
    assert stub.invoked_with["qualifier"] == "stable"


def test_stream_runtime_events_yields_native_sse_incrementally():
    class Body:
        def __init__(self):
            self.lines_seen = 0
            self.chunk_size = None

        def iter_lines(self, *, chunk_size):
            self.chunk_size = chunk_size
            for line in [
                b'data: {"event":"delta","text":"hello "}',
                b"",
                b'data: {"event":"heartbeat","timestamp":1784210000.0}',
                b"",
                b'data: {"event":"tool","name":"search","id":"tool-1"}',
                b"",
                b'data: {"event":"delta","text":"world"}',
                b"",
                b'data: {"event":"complete","result":"hello world"}',
                b"",
            ]:
                self.lines_seen += 1
                yield line

    body = Body()

    class StreamingDataPlane:
        def invoke_agent_runtime(self, **_kwargs):
            return {"response": body, "contentType": "text/event-stream"}

    stream = rt.stream_runtime_events(StreamingDataPlane(), "arn:rt-1", "hi")

    assert next(stream) == {"event": "delta", "data": {"text": "hello "}}
    assert body.lines_seen == 2
    assert body.chunk_size == 32
    assert list(stream) == [
        {"event": "heartbeat", "data": {}},
        {"event": "tool", "data": {"name": "search", "id": "tool-1"}},
        {"event": "delta", "data": {"text": "world"}},
    ]


def test_invoke_runtime_text_joins_native_sse_without_final_duplication():
    class Body:
        def iter_lines(self, *, chunk_size):
            assert chunk_size == 32
            yield from [
                b'data: {"event":"delta","text":"hello "}',
                b"",
                b'data: {"event":"delta","text":"world"}',
                b"",
                b'data: {"event":"complete","result":"hello world"}',
                b"",
            ]

    class StreamingDataPlane:
        def invoke_agent_runtime(self, **_kwargs):
            return {"response": Body(), "contentType": "text/event-stream"}

    result = rt.invoke_runtime_text(StreamingDataPlane(), "arn:rt-1", "hi")

    assert result["text"] == "hello world"


# ─── sigv4_post ──────────────────────────────────────────────────────────────
def _stub_creds(monkeypatch):
    monkeypatch.setattr(
        aws_clients, "get_session",
        lambda *a, **k: SimpleNamespace(
            get_credentials=lambda: SimpleNamespace(
                get_frozen_credentials=lambda: "frozen"
            )
        ),
    )


class _FakeResp:
    status_code = 200


def test_sigv4_post_sets_session_header_and_signs(monkeypatch):
    _stub_creds(monkeypatch)
    captured: dict = {}
    signed: list = []

    def poster(url, content, headers):
        captured.update(url=url, content=content, headers=headers)
        return _FakeResp()

    resp = gw.sigv4_post(
        "https://gw/x/invocations",
        {"prompt": "hi"},
        ws_ctx(),
        session_id="sess-123",
        poster=poster,
        signer=lambda creds, region, req: signed.append(creds),
    )
    assert resp.status_code == 200
    assert captured["url"] == "https://gw/x/invocations"
    assert captured["headers"][gw.SESSION_HEADER] == "sess-123"
    assert json.loads(captured["content"]) == {"prompt": "hi"}
    assert signed == ["frozen"]


def test_sigv4_post_omits_session_header_when_absent(monkeypatch):
    _stub_creds(monkeypatch)
    captured: dict = {}

    def poster(url, content, headers):
        captured["headers"] = headers
        return _FakeResp()

    gw.sigv4_post(
        "https://gw/x/invocations", {"prompt": "hi"}, ws_ctx(),
        poster=poster, signer=lambda *a: None,
    )
    assert gw.SESSION_HEADER not in captured["headers"]
