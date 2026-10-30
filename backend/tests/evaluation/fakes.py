"""Hermetic doubles for the N12 evaluation stack (no AWS, no network, no judge calls).

``FakeEngine`` implements ``evaluation.engine.EvaluationEngine``; ``FakeExecutor`` the
``evaluation.executor.EvalExecutor`` seam; ``FakeLogs`` a CloudWatch Logs client whose
Insights results are keyed by a substring of the query text (same style as
``tests/test_observability.py``); ``FakeModel`` a scripted ``strands.models.Model``;
``FakeS3`` an in-memory S3 client (put/get/list/delete/copy/head).
"""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator, AsyncIterable
from datetime import UTC, datetime
from typing import Any

from strands.models import Model

from app.evaluation.jobs import JobDone, JobSpec, JobStatus


class FakeEngine:
    """Scripted scores per evaluator id; records every call."""

    def __init__(self, scores: dict[str, float] | None = None,
                 insights: dict[str, Any] | None = None) -> None:
        self.scores = dict(scores or {})
        self.insights = insights or {"failures": [], "userIntents": [], "executionSummaries": []}
        self.calls: list[dict[str, Any]] = []
        self.sessions: dict[str, Any] = {}

    def evaluate_sessions(self, ws, *, source, owner_id, session_ids, log_groups, evaluator_ids,
                          ground_truth, judge_model_id, stop, progress, persist=True,
                          references=None, on_check=None):
        self.calls.append({"source": source, "owner_id": owner_id, "session_ids": list(session_ids),
                           "log_groups": list(log_groups), "evaluator_ids": list(evaluator_ids),
                           "ground_truth": ground_truth, "judge_model_id": judge_model_id,
                           "persist": persist})
        records = []
        for sid in session_ids:
            if stop and stop():
                break
            for evaluator in evaluator_ids:
                records.append({
                    "session_id": sid, "trace_id": None, "evaluator_id": evaluator,
                    "level": "TRACE", "score": self.scores.get(evaluator, 1.0),
                    "label": "pass", "explanation": "scripted", "error_type": None,
                    "error_message": None,
                })
            if progress:
                progress(f"scored {sid}")
        return records

    def diagnose_sessions(self, ws, *, session_ids, log_groups, kinds, judge_model_id):
        self.calls.append({"diagnose": list(session_ids), "kinds": list(kinds)})
        return dict(self.insights)

    def session(self, ws, *, session_id, log_groups):
        return self.sessions.get(session_id)


class FakeExecutor:
    """Records submits; status / done / results are scripted by the test."""

    def __init__(self) -> None:
        self.submitted: list[JobSpec] = []
        self.cancelled: list[str] = []
        self.statuses: dict[str, JobStatus] = {}
        self.dones: dict[str, JobDone] = {}
        self.results: dict[str, list[dict[str, Any]]] = {}

    def submit(self, ws, job: JobSpec) -> str:
        self.submitted.append(job)
        self.statuses.setdefault(job.job_id, JobStatus(phase="invoking"))
        return job.job_id

    def status(self, ws, job_id: str) -> JobStatus | None:
        return self.statuses.get(job_id)

    def done(self, ws, job_id: str) -> JobDone | None:
        return self.dones.get(job_id)

    def ingest(self, ws, job_id: str, since: int = 0) -> list[dict[str, Any]]:
        return list(self.results.get(job_id, []))[since:]

    def cancel(self, ws, job_id: str) -> None:
        self.cancelled.append(job_id)

    # test helpers
    def complete(self, job_id: str, records: list[dict[str, Any]],
                 summary: dict[str, Any] | None = None) -> None:
        self.results[job_id] = list(records)
        self.statuses[job_id] = JobStatus(phase="evaluating", state="COMPLETED")
        self.dones[job_id] = JobDone(state="COMPLETED", result_count=len(records),
                                     summary=summary or {})


class FakeLogs:
    """Logs Insights double: ``routes`` maps a query-text substring to result rows
    (``{field: value}`` dicts); the first matching key (longest first) answers."""

    def __init__(self, routes: dict[str, list[dict[str, Any]]]) -> None:
        self.routes = dict(routes)
        self.started: list[dict[str, Any]] = []
        self._results: dict[str, list[dict[str, Any]]] = {}
        self.filtered: list[dict[str, Any]] = []

    def start_query(self, **kwargs: Any) -> dict[str, str]:
        self.started.append(kwargs)
        query = kwargs.get("queryString", "")
        rows: list[dict[str, Any]] = []
        for key in sorted(self.routes, key=len, reverse=True):
            if key in query:
                rows = self.routes[key]
                break
        qid = f"q{len(self.started)}"
        self._results[qid] = rows
        return {"queryId": qid}

    def get_query_results(self, queryId: str) -> dict[str, Any]:  # noqa: N803 — boto3 kwarg
        rows = self._results.get(queryId, [])
        return {"status": "Complete", "results": [
            [{"field": k, "value": v if isinstance(v, str) else json.dumps(v)}
             for k, v in row.items()] for row in rows]}

    def describe_log_groups(self, **kwargs: Any) -> dict[str, Any]:
        return {"logGroups": []}

    def filter_log_events(self, **kwargs: Any) -> dict[str, Any]:
        self.filtered.append(kwargs)
        return {"events": []}


class FakeModel(Model):
    """A ``strands.models.Model`` that replays scripted outputs — never calls Bedrock.

    ``structured`` are dicts (or pydantic instances) returned, in order, for structured
    output requests; ``texts`` are plain assistant replies for free-form turns. The agent
    loop asks for structured output through a dynamically registered tool, so ``stream``
    answers such a request with a ``toolUse`` of that tool carrying the scripted dict.
    """

    def __init__(self, structured: list[Any] | None = None, texts: list[str] | None = None,
                 model_id: str = "fake-model") -> None:
        self.structured = list(structured or [])
        self.texts = list(texts or [])
        self.model_id = model_id
        self.calls: list[dict[str, Any]] = []
        self._config: dict[str, Any] = {"model_id": model_id}

    def update_config(self, **model_config: Any) -> None:
        self._config.update(model_config)

    def get_config(self) -> dict[str, Any]:
        return dict(self._config)

    def _next_structured(self) -> dict[str, Any]:
        item = self.structured.pop(0) if self.structured else {"score": 1.0, "test_pass": True,
                                                                "reason": "scripted"}
        return item if isinstance(item, dict) else item.model_dump()

    async def structured_output(self, output_model, prompt, system_prompt=None, **kwargs
                                ) -> AsyncGenerator[dict[str, Any], None]:
        self.calls.append({"kind": "structured_output", "prompt": prompt})
        yield {"output": output_model.model_validate(self._next_structured())}

    async def stream(self, messages, tool_specs=None, system_prompt=None, **kwargs
                     ) -> AsyncIterable[dict[str, Any]]:
        self.calls.append({"kind": "stream", "messages": messages,
                           "tools": [t["name"] for t in tool_specs or []]})
        yield {"messageStart": {"role": "assistant"}}
        if tool_specs and self.structured:
            tool = tool_specs[-1]  # the structured-output tool is registered last
            yield {"contentBlockStart": {"start": {"toolUse": {
                "toolUseId": f"tu-{len(self.calls)}", "name": tool["name"]}}}}
            yield {"contentBlockDelta": {"delta": {"toolUse": {
                "input": json.dumps(self._next_structured())}}}}
            yield {"contentBlockStop": {}}
            yield {"messageStop": {"stopReason": "tool_use"}}
        else:
            text = self.texts.pop(0) if self.texts else "ok"
            yield {"contentBlockDelta": {"delta": {"text": text}}}
            yield {"contentBlockStop": {}}
            yield {"messageStop": {"stopReason": "end_turn"}}
        yield {"metadata": {"usage": {"inputTokens": 1, "outputTokens": 1, "totalTokens": 2},
                            "metrics": {"latencyMs": 1}}}


def _synth(schema: dict[str, Any], defs: dict[str, Any], overrides: dict[str, Any],
           arrays: int = 0) -> Any:
    """A valid instance of a JSON schema: enum → first value (or the override), object →
    required members, array → ``arrays`` items (or 1 when the schema needs one), number
    0.8, integer 1, boolean True, string "scripted"."""
    if "$ref" in schema:
        return _synth(defs[schema["$ref"].rsplit("/", 1)[-1]], defs, overrides, arrays)
    if "enum" in schema:
        return schema["enum"][0]
    kind = schema.get("type")
    if "anyOf" in schema:
        options = [o for o in schema["anyOf"] if o.get("type") != "null"] or schema["anyOf"]
        return _synth(options[0], defs, overrides, arrays)
    if kind == "object":
        props = schema.get("properties", {})
        required = schema.get("required", list(props))
        out = {}
        for k, v in props.items():
            if k not in required and k not in overrides:
                continue
            resolved = _resolve(v, defs)
            # an override applies only where it is a VALID value (every builtin judge has its
            # own rating enum; an alien label would make strands retry the output forever)
            if k in overrides and ("enum" not in resolved or overrides[k] in resolved["enum"]):
                out[k] = overrides[k]
            else:
                out[k] = _synth(v, defs, overrides, arrays)
        return out
    if kind == "array":
        items = schema.get("items", {"type": "string"})
        count = max(int(arrays), 1 if schema.get("minItems") else 0)
        return [_synth(items, defs, overrides, arrays) for _ in range(count)]
    if kind == "number":
        return 0.8
    if kind == "integer":
        return 1
    if kind == "boolean":
        return True
    return "scripted"


def _resolve(schema: dict[str, Any], defs: dict[str, Any]) -> dict[str, Any]:
    while "$ref" in schema:
        schema = defs[schema["$ref"].rsplit("/", 1)[-1]]
    if "anyOf" in schema:
        options = [o for o in schema["anyOf"] if o.get("type") != "null"] or schema["anyOf"]
        return _resolve(options[0], defs)
    return schema


class FakeJudge(FakeModel):
    """A judge that always answers the forced structured-output tool with a VALID instance
    of its JSON schema (the builtin evaluators each have their own rating model). Field
    values are synthesised (enum → first value) unless ``overrides`` names them; ``by_tool``
    maps a substring of the structured-output tool name to an explicit dict (or callable
    returning one) for that request; ``fail_with`` raises before answering."""

    def __init__(self, overrides: dict[str, Any] | None = None,
                 by_tool: dict[str, Any] | None = None, fail_with: Exception | None = None,
                 model_id: str = "fake-judge", arrays: int = 0,
                 text: Any = None) -> None:
        super().__init__(model_id=model_id)
        self.overrides = dict(overrides or {})
        self.by_tool = dict(by_tool or {})
        self.fail_with = fail_with
        self.arrays = arrays
        # free-form turns (the detectors parse JSON out of plain text): a string, or a
        # callable over the request messages returning one; "ok" by default
        self.text = text
        self.schemas: list[dict[str, Any]] = []
        self.tool_names: list[str] = []

    def text_for(self, messages: Any) -> str:
        if callable(self.text):
            return str(self.text(messages))
        return str(self.text) if self.text is not None else "ok"

    def answer_for(self, tool: dict[str, Any]) -> dict[str, Any]:
        schema = tool["inputSchema"]["json"]
        self.schemas.append(schema)
        self.tool_names.append(tool["name"])
        for needle, scripted in self.by_tool.items():
            if needle in tool["name"]:
                return scripted() if callable(scripted) else dict(scripted)
        return _synth(schema, schema.get("$defs", {}), self.overrides, self.arrays)

    async def stream(self, messages, tool_specs=None, system_prompt=None, **kwargs
                     ) -> AsyncIterable[dict[str, Any]]:
        self.calls.append({"kind": "stream", "messages": messages,
                           "tools": [t["name"] for t in tool_specs or []]})
        if self.fail_with is not None:
            raise self.fail_with
        yield {"messageStart": {"role": "assistant"}}
        if tool_specs:
            tool = tool_specs[-1]
            yield {"contentBlockStart": {"start": {"toolUse": {
                "toolUseId": f"tu-{len(self.calls)}", "name": tool["name"]}}}}
            yield {"contentBlockDelta": {"delta": {"toolUse": {
                "input": json.dumps(self.answer_for(tool))}}}}
            yield {"contentBlockStop": {}}
            yield {"messageStop": {"stopReason": "tool_use"}}
        else:
            yield {"contentBlockDelta": {"delta": {"text": self.text_for(messages)}}}
            yield {"contentBlockStop": {}}
            yield {"messageStop": {"stopReason": "end_turn"}}
        yield {"metadata": {"usage": {"inputTokens": 1, "outputTokens": 1, "totalTokens": 2},
                            "metrics": {"latencyMs": 1}}}


class _Body:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def read(self) -> bytes:
        return self._data


class FakeS3:
    """In-memory S3 client (the subset the evaluation stack uses)."""

    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def put_object(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("put_object", kwargs))
        body = kwargs["Body"]
        self.objects[(kwargs["Bucket"], kwargs["Key"])] = (
            body.encode("utf-8") if isinstance(body, str) else bytes(body))
        return {}

    def get_object(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("get_object", kwargs))
        key = (kwargs["Bucket"], kwargs["Key"])
        if key not in self.objects:
            raise KeyError(kwargs["Key"])
        return {"Body": _Body(self.objects[key]),
                "LastModified": datetime.now(UTC)}

    def head_object(self, **kwargs: Any) -> dict[str, Any]:
        if (kwargs["Bucket"], kwargs["Key"]) not in self.objects:
            raise KeyError(kwargs["Key"])
        return {"ContentLength": len(self.objects[(kwargs["Bucket"], kwargs["Key"])])}

    def list_objects_v2(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("list_objects_v2", kwargs))
        prefix = kwargs.get("Prefix", "")
        keys = sorted(k for (b, k) in self.objects
                      if b == kwargs["Bucket"] and k.startswith(prefix))
        return {"Contents": [{"Key": k, "Size": len(self.objects[(kwargs["Bucket"], k)])}
                             for k in keys], "KeyCount": len(keys)}

    def delete_object(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("delete_object", kwargs))
        self.objects.pop((kwargs["Bucket"], kwargs["Key"]), None)
        return {}

    def copy_object(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("copy_object", kwargs))
        src = kwargs["CopySource"]
        src_key = (src["Bucket"], src["Key"]) if isinstance(src, dict) else tuple(src.split("/", 1))
        self.objects[(kwargs["Bucket"], kwargs["Key"])] = self.objects[src_key]
        return {}


CI_KEYS = ("sessions_n", "session_mean", "sd", "ci95", "ci_method", "scale")


def strip_ci(scores):
    """A run's ``scores`` without the EVAL-TRUST 3.2 interval keys (pinned in
    test_run_confidence) — for tests that pin the unit-level headline only."""
    return [{k: v for k, v in s.items() if k not in CI_KEYS} for s in scores or []]
