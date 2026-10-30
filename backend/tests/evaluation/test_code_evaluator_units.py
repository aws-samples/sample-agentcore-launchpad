"""N17-obs333 — TRACE and TOOL_CALL code evaluators run per unit; ``lambda_timeout_s`` is
applied.

Global: "The service handles Lambda invocation, parallel fan-out, and result mapping"; a
TOOL_CALL evaluator is invoked once per tool span with ``evaluationTarget {traceIds,
spanIds}`` and gets one result per call. ``lambdaTimeoutInSeconds`` (1–300, default 60) bounds
the invocation. Before this fix a TOOL_CALL code evaluator was invoked once per session (the
SDK handler then judged ``spanIds[0]`` only) and the Lambda client was the default one
(read timeout 60 s, botocore retries, then the judge's transient retries on top).
"""

from __future__ import annotations

from typing import Any

from botocore.exceptions import ReadTimeoutError

from app.services.strands_eval import code_evaluators
from tests.evaluation import global_code_evaluator
from tests.evaluation.code_eval_support import (
    TOOL,
    TURN_TRACES,
    InProcessLambda,
    run_corpus,
)

TOOL_SPANS = ["6c55a9b38a311a42", "12fd4ae146e9f882", "5edcf556823b2be6"]


def test_a_tool_call_evaluator_is_invoked_once_per_tool_span():
    client = InProcessLambda(global_code_evaluator.handler)
    records = run_corpus("TOOL_CALL", client)
    assert [e["evaluationTarget"] for e in client.events] == [
        {"traceIds": [t], "spanIds": [s]} for t, s in zip(TURN_TRACES, TOOL_SPANS, strict=True)]
    assert {e["evaluationLevel"] for e in client.events} == {"TOOL_CALL"}
    assert [(r["trace_id"], r["span_id"], r["tool_name"], r["label"], r["explanation"])
            for r in records] == [(t, s, TOOL, "Called", TOOL)
                                  for t, s in zip(TURN_TRACES, TOOL_SPANS, strict=True)]
    assert len({r["id"] for r in records}) == 3


def test_one_failing_tool_call_is_an_error_row_in_its_own_slot():
    def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
        if event["evaluationTarget"]["spanIds"] == [TOOL_SPANS[1]]:
            return {"errorCode": "BAD_ARGS", "errorMessage": "employee id malformed"}
        return {"label": "Pass", "value": 1.0}

    records = run_corpus("TOOL_CALL", InProcessLambda(handler))
    assert [(r["span_id"], r["score"], r["error_type"]) for r in records] == [
        (TOOL_SPANS[0], 1.0, None), (TOOL_SPANS[1], None, "BAD_ARGS"),
        (TOOL_SPANS[2], 1.0, None)]


def test_tool_call_references_are_the_session_entry_only():
    client = InProcessLambda(global_code_evaluator.handler)
    truth = {"assertions": ["a"], "expected_trajectory": [TOOL],
             "turns": [{"input": "x", "expected_response": "r0"}]}
    run_corpus("TOOL_CALL", client, truth)
    assert all(len(e["evaluationReferenceInputs"]) == 1
               and "traceId" not in e["evaluationReferenceInputs"][0]["context"]["spanContext"]
               for e in client.events)


class Clients:
    """``code_evaluators.LambdaClients`` factory double: one client per timeout."""

    def __init__(self, inner: Any) -> None:
        self.inner, self.made = inner, []

    def __call__(self, timeout_s: int) -> Any:
        self.made.append(timeout_s)
        return self.inner


def test_the_lambda_client_is_built_for_the_evaluators_timeout():
    clients = Clients(InProcessLambda(global_code_evaluator.handler))
    run_corpus("TRACE", code_evaluators.LambdaClients(clients), timeout_s=120)
    assert clients.made == [120]  # one client per timeout, reused for every turn
    config = code_evaluators.lambda_config(120)
    assert config.read_timeout == 120 + code_evaluators.CLIENT_TIMEOUT_MARGIN_S
    assert config.retries == {"total_max_attempts": 1, "mode": "standard"}
    assert code_evaluators.lambda_config(999).read_timeout == (
        300 + code_evaluators.CLIENT_TIMEOUT_MARGIN_S)


class SilentLambda:
    def __init__(self) -> None:
        self.calls = 0

    def invoke(self, **_kwargs: Any) -> dict[str, Any]:
        self.calls += 1
        raise ReadTimeoutError(endpoint_url="https://lambda.cn-northwest-1.amazonaws.com.cn")


def test_a_client_read_timeout_is_a_timeout_row_never_a_retried_transient():
    silent = SilentLambda()
    records = run_corpus("SESSION", code_evaluators.LambdaClients(lambda _t: silent),
                         timeout_s=5)
    assert [(r["score"], r["error_type"]) for r in records] == [(None, "lambda_timeout")]
    assert "5 s" in records[0]["error_message"]
    assert silent.calls == 1


def test_the_hub_executor_builds_per_timeout_clients_through_the_funnel(monkeypatch):
    from app.core.config import get_settings
    from app.evaluation import runtime_executor

    monkeypatch.setenv("LAUNCHPAD_EVAL_CODE_EVALUATORS_ENABLED", "1")
    get_settings.cache_clear()
    built: list[dict[str, Any]] = []

    class Ws:
        id = "default"

        def client(self, service: str, cache_token: str | None = None, **cfg: Any) -> Any:
            built.append({"service": service, "cache_token": cache_token, **cfg})
            return object()

    monkeypatch.setattr(runtime_executor, "data_client", lambda ws: None)
    try:
        env = runtime_executor.worker_env_for(Ws())  # type: ignore[arg-type]
    finally:
        get_settings.cache_clear()
    assert isinstance(env.lambda_client, code_evaluators.LambdaClients)
    env.lambda_client.for_timeout(45)
    lam = [b for b in built if b["service"] == "lambda"]
    assert len(lam) == 1 and lam[0]["cache_token"] == "code-evaluator-45"
    assert lam[0]["config"].read_timeout == 45 + code_evaluators.CLIENT_TIMEOUT_MARGIN_S
