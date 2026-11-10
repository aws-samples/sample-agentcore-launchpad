"""Shared harness of the code-evaluator suites (N17-obs327 / obs328 / obs333): a REAL
redacted corpus session (3 turns, one gateway tool call each) replayed through the
production spans provider and engine, with an in-process Lambda behind ``invoke``."""

from __future__ import annotations

import io
import json
from typing import Any

from app.services.strands_eval import code_evaluators, engine_core
from app.services.strands_eval.spans_provider import SPANS_LOG_GROUP, LaunchpadSpansProvider
from tests.evaluation.corpus_support import GroupLogs, load_session

CORPUS = load_session("06d7a610023c")
SID = CORPUS["session_id"]
GROUPS = [CORPUS["log_group"], SPANS_LOG_GROUP]
TURN_TRACES = [t["trace_id"] for t in CORPUS["expected"]["turns"]]
TOOL = "launchpad_gw_hr-database___get_employee"


class InProcessLambda:
    """``lambda_client.invoke`` → the handler, through the JSON wire both ways;
    ``function_error`` stamps ``FunctionError`` on the response (an unhandled exception
    or a Lambda timeout — the body is then the runtime's ``{errorMessage, errorType}``)."""

    def __init__(self, handler: Any, function_error: str | None = None) -> None:
        self.handler, self.function_error, self.events = handler, function_error, []
        self.calls: list[dict[str, Any]] = []

    def invoke(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        event = json.loads(kwargs["Payload"])
        self.events.append(event)
        body = self.handler(event, None)
        response = {"StatusCode": 200, "Payload": io.BytesIO(json.dumps(body).encode())}
        if self.function_error:
            response["FunctionError"] = self.function_error
        return response


def definition(level: str, name: str = "tool_counter", timeout_s: int = 30) -> dict[str, Any]:
    return {"id": "ev_code01", "name": name, "definition": "code", "level": level,
            "lambda_arn": "arn:aws-cn:lambda:cn-northwest-1:1:function:launchpad-evalfn-x:1",
            "lambda_timeout_s": timeout_s}


def run_corpus(level: str, client: Any, truth: dict[str, Any] | None = None,
               name: str = "tool_counter", timeout_s: int = 30) -> list[dict[str, Any]]:
    logs = GroupLogs(CORPUS["records"])
    provider = LaunchpadSpansProvider(logs, GROUPS)
    runtime = code_evaluators.CodeEvaluatorRuntime(
        client, session_spans=code_evaluators.SessionSpans(logs, GROUPS),
        ground_truth={SID: truth} if truth else {})
    evaluators = engine_core.EvaluatorSet([definition(level, name, timeout_s)],
                                          lambda _m: None, lambda_client=runtime)
    return engine_core.evaluate_session(provider, SID, evaluators, truth, job_id="run1",
                                        sleep=lambda _s: None)
