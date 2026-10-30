"""N12 E1-3b — the evaluation worker template, rendered and EXECUTED in-process.

The rendered ``main.py`` + ``launchpad_eval`` package are written to a temp dir and imported;
the module's test seams (``CLIENT_FACTORY`` / ``JUDGE_FACTORY``) plug a ``FakeS3``, a
``FakeLogs`` carrying the span fixture and a ``FakeJudge`` in. The entrypoint must ack within
a second, the thread must write ``status.json`` → ``invocations.jsonl`` → ``results.jsonl`` →
``done.json`` (last), honour the cancel marker and reject reserved kinds."""

import dataclasses
import importlib.util
import json
import sys
import time
from pathlib import Path

import pytest

from app.evaluation import jobs
from app.schemas.agent import AgentSpec, allowed_model_ids
from app.system_agents.presets import EVAL_WORKER, build_spec
from app.templates import eval_worker as ew
from tests.evaluation.fakes import FakeJudge, FakeLogs, FakeS3

FIXTURE = json.loads(
    (Path(__file__).resolve().parent / "fixtures" / "spans_session.json").read_text()
)
SID, GROUP = FIXTURE["session_id"], FIXTURE["log_group"]
BUCKET = "artifacts"


def _records_for(session_id):
    return [{**r, "attributes": {**r["attributes"], "session.id": session_id}}
            for r in FIXTURE["records"]]


def fake_logs(*session_ids):
    routes = {}
    for sid in session_ids:
        routes[f"= '{sid}' | sort"] = [{"@message": json.dumps(r)} for r in _records_for(sid)]
    return FakeLogs(routes)


class FakeDataPlane:
    """``invoke_agent_runtime`` answering the platform's SSE envelope."""

    def __init__(self, fail_times: int = 0, error: Exception | None = None,
                 fail_with: Exception | None = None) -> None:
        self.calls: list[dict] = []
        self.fail_times = fail_times
        self.error = error
        self.fail_with = fail_with  # the transient error raised while fail_times > 0
        self.error_for: dict[str, Exception] = {}  # session id → always raise this

    def invoke_agent_runtime(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        if kwargs.get("runtimeSessionId") in self.error_for:
            raise self.error_for[kwargs["runtimeSessionId"]]
        if self.fail_times > 0:
            self.fail_times -= 1
            raise self.fail_with or RuntimeError("internal server error: hiccup")
        body = ('data: {"event": "delta", "text": "Ada has "}\n'
                'data: {"event": "delta", "text": "12 days"}\n'
                'data: {"event": "complete", "result": "Ada has 12 days"}\n')

        class _B:
            def read(self):
                return body.encode()

            def close(self):
                pass

        return {"response": _B(), "contentType": "text/event-stream"}


@pytest.fixture
def worker(tmp_path, monkeypatch):
    """The rendered worker module, importable, with fakes plugged into its seams."""
    spec = build_spec(EVAL_WORKER, BUCKET, EVAL_WORKER.default_options())
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "main.py").write_text(ew.render_main_py(spec), encoding="utf-8")
    ew.write_library(pkg)
    monkeypatch.syspath_prepend(str(pkg))
    _forget_library()
    module_spec = importlib.util.spec_from_file_location("eval_worker_main", pkg / "main.py")
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    s3, logs, data = FakeS3(), fake_logs(SID), FakeDataPlane()
    module.CLIENT_FACTORY = lambda service: {"s3": s3, "logs": logs,
                                             "bedrock-agentcore": data}[service]
    judge = FakeJudge(overrides={"score": "Very helpful", "reasoning": "clear"})
    module.JUDGE_FACTORY = lambda model_id, **kw: judge
    module.fakes = {"s3": s3, "logs": logs, "data": data, "judge": judge}  # type: ignore[attr-defined]
    monkeypatch.setattr(module.jobs_batch, "wait_for_evaluation_telemetry",
                        lambda *a, **k: None)
    yield module
    _forget_library()


def _forget_library() -> None:
    for name in [m for m in sys.modules
                 if m == "launchpad_eval" or m.startswith("launchpad_eval.")]:
        sys.modules.pop(name)


def payload(job_id="job1", job_type="batch_eval", **spec_overrides):
    spec = {
        "run_id": "run1",
        "target": {"runtime_arn": "arn:aws-cn:bedrock-agentcore:cn-northwest-1:1:runtime/t",
                   "protocol": "http", "runtime_user_id": None},
        "sessions": [{"session_id": SID, "scenario_id": "item_1", "prompts": ["hi"],
                      "arm": None, "qualifier": None, "baggage": None, "launchpad_meta": None}],
        "wait": {"seconds": 0, "log_groups": [GROUP], "spans_source": "runtime_logs",
                 "content_log_group": GROUP},
        "evaluator_defs": [{"id": "Builtin.Helpfulness"}, {"id": "Builtin.TrajectoryInOrderMatch"}],
        "ground_truth": {SID: {"expected_trajectory": ["hr_lookup"], "input": "hi"}},
        "judge_model_id": None,
        "replay": True,
    }
    spec.update(spec_overrides)
    return jobs.JobSpec(job_id=job_id, job_type=job_type,
                        workspace={"account_id": "1", "region": "cn-northwest-1"},
                        sink={"bucket": BUCKET, "prefix": jobs.job_prefix(job_id, 1)},
                        spec=spec).to_payload()


def _wait(module, job_id, timeout=20.0):
    thread = module._THREADS.get(job_id)
    if thread is not None:
        thread.join(timeout)
    assert job_id not in module._THREADS or not module._THREADS[job_id].is_alive()


def _obj(s3, key):
    return s3.objects[(BUCKET, key)].decode()


def test_entrypoint_acks_fast_then_writes_every_artefact_in_order(worker):
    s3 = worker.fakes["s3"]
    t0 = time.monotonic()
    ack = worker._handle(payload())
    assert time.monotonic() - t0 < 1.0
    assert ack["state"] == "ACCEPTED" and ack["job_id"] == "job1" and ack["attempt"] == 1
    assert ack["release"] == ew.release_tag()
    # status.json is written BEFORE the ack returns (the thread may already have moved on)
    status_puts = [c[1]["Body"] for c in s3.calls
                   if c[0] == "put_object" and c[1]["Key"] == jobs.status_key("job1", 1)]
    first_status = json.loads(status_puts[0])
    assert first_status["state"] == "RUNNING" and first_status["phase"] == "invoking"
    assert first_status["progress"]["message"] == "worker accepted the job"
    _wait(worker, "job1")
    keys = sorted(k for (_b, k) in s3.objects)
    # N17-obs417: plus the attempt's judge-token sidecar
    assert keys == [jobs.done_key("job1", 1), jobs.invocations_key("job1", 1),
                    jobs.results_key("job1", 1), jobs.status_key("job1", 1),
                    jobs.artifact_key("job1", 1, "usage.json")]
    done = json.loads(_obj(s3, jobs.done_key("job1", 1)))
    assert done["state"] == "COMPLETED" and done["error"] is None
    assert done["result_count"] == 2
    assert done["summary"]["scores"] == [
        {"evaluatorId": "Builtin.Helpfulness", "score": 0.833, "count": 1, "n": 1, "unit": "turn"},
        {"evaluatorId": "Builtin.TrajectoryInOrderMatch", "score": 1.0,
            "count": 1, "n": 1, "unit": "session"},
    ]
    results = [json.loads(line) for line in _obj(s3, jobs.results_key("job1", 1)).splitlines()]
    assert {r["evaluator_id"] for r in results} == {"Builtin.Helpfulness",
                                                    "Builtin.TrajectoryInOrderMatch"}
    assert all(r["session_id"] == SID and r["id"] for r in results)
    assert results[0]["id"] == jobs.record_id("job1", SID, results[0]["evaluator_id"], 0)
    invocations = [json.loads(line)
                   for line in _obj(s3, jobs.invocations_key("job1", 1)).splitlines()]
    assert invocations == [{"session_id": SID, "scenario_id": "item_1", "arm": None,
                            "status": "ok", "code": None, "response_chars": 15,
                            # D3-c: the turns the replay sent
                            "runtime_version": None, "turns_replayed": 1, "retries": 0}]
    # done.json was written after results.jsonl (R-18)
    puts = [c[1]["Key"] for c in s3.calls if c[0] == "put_object"]
    assert puts.index(jobs.done_key("job1", 1)) > puts.index(jobs.results_key("job1", 1))
    # the final status reads evaluating with the progress message
    status = json.loads(_obj(s3, jobs.status_key("job1", 1)))
    assert status["phase"] == "evaluating" and status["progress"]["message"] == "evaluating 1/1"


def test_replay_uses_the_preassigned_session_qualifier_and_baggage(worker):
    data = worker.fakes["data"]
    worker._handle(payload(sessions=[{
        "session_id": SID, "scenario_id": "s", "prompts": ["a", "b"], "arm": "T1",
        "qualifier": "treat123456", "baggage": "launchpad.bundle=bndl_x:2",
        "launchpad_meta": {"bundle": "bndl_x:2", "variant": "T1"},
    }]))
    _wait(worker, "job1")
    assert len(data.calls) == 2  # two turns, one session
    call = data.calls[0]
    assert call["runtimeSessionId"] == SID and call["qualifier"] == "treat123456"
    assert call["baggage"] == "launchpad.bundle=bndl_x:2"
    body = json.loads(call["payload"])
    assert body["prompt"] == "a" and body["launchpad"] == {"bundle": "bndl_x:2", "variant": "T1"}
    assert body["actor_id"] == "launchpad-eval"
    assert data.calls[1]["runtimeSessionId"] == SID
    invocations = [json.loads(line) for line in
                   _obj(worker.fakes["s3"], jobs.invocations_key("job1", 1)).splitlines()]
    assert invocations[0]["arm"] == "T1" and invocations[0]["status"] == "ok"


def test_transient_invoke_errors_are_replayed_in_the_same_session(worker):
    worker.fakes["data"].fail_times = 1
    worker._handle(payload())
    _wait(worker, "job1")
    done = json.loads(_obj(worker.fakes["s3"], jobs.done_key("job1", 1)))
    assert done["state"] == "COMPLETED"
    invocations = [json.loads(line) for line in
                   _obj(worker.fakes["s3"], jobs.invocations_key("job1", 1)).splitlines()]
    assert invocations[0]["retries"] == 1 and invocations[0]["status"] == "ok"
    assert len(worker.fakes["data"].calls) == 2


def test_the_data_plane_client_carries_the_hub_invoke_read_timeout(worker, monkeypatch):
    """N-SCN-bug1: the worker used botocore's 60 s default read timeout while the hub's
    `data_client` uses `Settings.agentcore_read_timeout_s`; a cold first turn of a heavy
    agent (> 60 s) timed out on the worker only. The constant is rendered from the hub
    setting, and the SDK must not retry the timeout itself (the job runner's classified
    loop is the one retry, logged attempt k/N)."""
    from app.core.config import get_settings

    expected = get_settings().agentcore_read_timeout_s
    assert worker.INVOKE_READ_TIMEOUT_S == expected and expected > 900  # ≥ the 15 min limit
    built: list[tuple] = []

    def fake_boto3_client(service, **kwargs):
        built.append((service, kwargs))
        return object()

    worker.CLIENT_FACTORY = None  # the real construction path
    worker._CLIENTS.clear()
    monkeypatch.setattr(worker.boto3, "client", fake_boto3_client)
    worker._client("bedrock-agentcore")
    worker._client("bedrock-agentcore")  # cached
    worker._client("logs")
    assert [b[0] for b in built] == ["bedrock-agentcore", "logs"]
    data_cfg = built[0][1]["config"]
    assert built[0][1]["region_name"] == worker._REGION
    assert data_cfg.read_timeout == expected
    assert data_cfg.connect_timeout == worker.INVOKE_CONNECT_TIMEOUT_S == 10
    assert data_cfg.retries == {"max_attempts": 1, "mode": "standard"}
    assert built[1][1]["config"] is None  # other clients keep botocore defaults
    # the rendered constant FOLLOWS the setting (a hub override reaches the worker on repair)
    monkeypatch.setattr(get_settings(), "agentcore_read_timeout_s", 1234)
    spec = build_spec(EVAL_WORKER, BUCKET, EVAL_WORKER.default_options())
    rendered = ew.render_main_py(spec)
    assert "INVOKE_READ_TIMEOUT_S = 1234\n" in rendered and "__LAUNCHPAD_" not in rendered


def test_code_evaluator_lambda_clients_carry_the_evaluators_timeout(worker, monkeypatch):
    """N17-obs333: the worker's Lambda client was botocore's default (60 s read timeout,
    SDK retries); a code evaluator's ``lambda_timeout_s`` was stored but never applied."""
    built: list[tuple] = []

    def fake_boto3_client(service, **kwargs):
        built.append((service, kwargs))
        return object()

    worker.CLIENT_FACTORY = None  # the real construction path
    monkeypatch.setattr(worker.boto3, "client", fake_boto3_client)
    monkeypatch.setenv("LAUNCHPAD_EVAL_CODE_EVALUATORS_ENABLED", "1")
    monkeypatch.setattr(worker, "_client", lambda service: object())
    clients = worker._env().lambda_client
    first = clients.for_timeout(120)
    assert clients.for_timeout(120) is first  # one client per timeout
    clients.for_timeout(30)
    assert [(b[0], b[1]["region_name"]) for b in built] == [("lambda", worker._REGION)] * 2
    configs = [b[1]["config"] for b in built]
    assert [c.read_timeout for c in configs] == [125, 35]
    assert all(c.retries == {"total_max_attempts": 1, "mode": "standard"} for c in configs)
    monkeypatch.setenv("LAUNCHPAD_EVAL_CODE_EVALUATORS_ENABLED", "")
    assert worker._env().lambda_client is None


def test_a_read_timeout_is_transient_and_the_scenario_recovers_in_the_same_session(worker):
    """N-SCN-bug1: botocore's ReadTimeoutError was not transient → one attempt, message
    "failed after 2 replays". Now it is replayed like a 5xx, in the same session."""
    from botocore.exceptions import ReadTimeoutError

    data = worker.fakes["data"]
    data.fail_times, data.fail_with = 2, ReadTimeoutError(endpoint_url="https://dp.test")
    worker._handle(payload())
    _wait(worker, "job1")
    s3 = worker.fakes["s3"]
    done = json.loads(_obj(s3, jobs.done_key("job1", 1)))
    assert done["state"] == "COMPLETED" and done["result_count"] == 2
    assert len(data.calls) == 3 and {c["runtimeSessionId"] for c in data.calls} == {SID}
    [inv] = [json.loads(line) for line in _obj(s3, jobs.invocations_key("job1", 1)).splitlines()]
    assert inv["status"] == "ok" and inv["retries"] == 2 and "error_kind" not in inv


def test_a_scenario_that_never_answers_is_errored_and_the_other_is_scored(worker):
    """N-SCN-bug1 (b) through the real rendered worker: 2 sessions, one keeps timing out →
    COMPLETED with the answered session scored and the unanswered one as error records
    (`error_kind: infrastructure`, `error_type` = the replay's exception)."""
    from botocore.exceptions import ReadTimeoutError

    other = "sess-other" + "x" * 26
    data, s3 = worker.fakes["data"], worker.fakes["s3"]
    data.error_for[other] = ReadTimeoutError(endpoint_url="https://dp.test")
    base = payload()["spec"]["sessions"][0]
    truth = {"expected_trajectory": ["hr_lookup"], "input": "hi"}
    worker._handle(payload(
        sessions=[base, {**base, "session_id": other, "scenario_id": "item_2"}],
        ground_truth={SID: truth, other: truth},
    ))
    _wait(worker, "job1")
    done = json.loads(_obj(s3, jobs.done_key("job1", 1)))
    n = worker.jobs_batch.TRANSIENT_SCENARIO_ATTEMPTS
    assert done["state"] == "COMPLETED" and done["error"] is None
    assert done["summary"]["scores"] == [
        {"evaluatorId": "Builtin.Helpfulness", "score": 0.833, "count": 1, "n": 1, "unit": "turn"},
        {"evaluatorId": "Builtin.TrajectoryInOrderMatch", "score": 1.0,
            "count": 1, "n": 1, "unit": "session"},
    ]
    assert done["summary"]["replay_failures"] == [{
        "session_id": other, "scenario_id": "item_2", "code": "ReadTimeoutError",
        "attempts": n, "error": 'Read timeout on endpoint URL: "https://dp.test"',
        "arm": None,  # N12-obs62: a plain run has no arm
    }]
    invocations = [json.loads(line)
                   for line in _obj(s3, jobs.invocations_key("job1", 1)).splitlines()]
    assert [(i["session_id"], i["status"]) for i in invocations] == [(SID, "ok"), (other, "failed")]
    assert invocations[1]["error_kind"] == "infrastructure" and invocations[1]["attempts"] == n
    assert sum(1 for c in data.calls if c["runtimeSessionId"] == other) == n
    results = [json.loads(line) for line in _obj(s3, jobs.results_key("job1", 1)).splitlines()]
    assert done["result_count"] == len(results) == 4
    errored = [r for r in results if r["session_id"] == other]
    assert {r["evaluator_id"] for r in errored} == {"Builtin.Helpfulness",
                                                    "Builtin.TrajectoryInOrderMatch"}
    assert all(r["score"] is None and r["error_type"] == "ReadTimeoutError"
               and r["error_kind"] == "infrastructure"
               and f"scenario item_2 failed after {n}/{n} attempts" in r["error_message"]
               for r in errored)
    assert all(r["score"] is not None for r in results if r["session_id"] == SID)


def test_a_final_invoke_failure_fails_the_job_with_the_reason(worker):
    worker.fakes["data"].error = ValueError("the agent is gone")
    worker._handle(payload())
    _wait(worker, "job1")
    s3 = worker.fakes["s3"]
    done = json.loads(_obj(s3, jobs.done_key("job1", 1)))
    assert done["state"] == "FAILED" and "the agent is gone" in done["error"]
    # N-SCN-bug1: the ONLY scenario failed → the run fails; a caller error gets one attempt
    # and the message says so (it used to claim "failed after 2 replays")
    n = worker.jobs_batch.TRANSIENT_SCENARIO_ATTEMPTS
    assert done["error"].startswith(
        "RuntimeError: all 1 scenarios failed to replay — first error: ")
    assert (f"scenario item_1 failed after 1/{n} attempts: ValueError: the agent is gone"
            in done["error"])
    invocations = [json.loads(line)
                   for line in _obj(s3, jobs.invocations_key("job1", 1)).splitlines()]
    assert invocations[0]["status"] == "failed" and invocations[0]["code"] == "ValueError"
    assert invocations[0]["attempts"] == 1 and invocations[0]["error_kind"] == "infrastructure"
    assert len(worker.fakes["data"].calls) == 1
    assert (BUCKET, jobs.results_key("job1", 1)) not in s3.objects


def test_replay_false_skips_invoking_and_waiting(worker):
    worker._handle(payload(replay=False))
    _wait(worker, "job1")
    s3 = worker.fakes["s3"]
    assert worker.fakes["data"].calls == []
    assert (BUCKET, jobs.invocations_key("job1", 1)) not in s3.objects
    assert json.loads(_obj(s3, jobs.done_key("job1", 1)))["state"] == "COMPLETED"
    phases = [json.loads(c[1]["Body"])["phase"] for c in s3.calls
              if c[0] == "put_object" and c[1]["Key"] == jobs.status_key("job1", 1)]
    assert "waiting" not in phases and phases[-1] == "evaluating"


def test_cancel_marker_stops_the_job(worker):
    s3 = worker.fakes["s3"]
    s3.objects[(BUCKET, jobs.cancel_key("job1"))] = b"cancel"  # the backend asked first
    worker._handle(payload())
    _wait(worker, "job1")
    done = json.loads(_obj(s3, jobs.done_key("job1", 1)))
    assert done["state"] == "STOPPED" and done["error"] == "stopped by operator"
    assert worker.fakes["data"].calls == []  # stopped before the first invocation


def test_a_cancel_during_the_replay_backoff_stops_the_rendered_worker_within_a_second(
        worker, monkeypatch):
    """N13-obs8: the rendered worker's replay backoff (`TRANSIENT_RETRY_BACKOFF_S`, here 15 s)
    is slept in ≤ 1 s slices that poll the S3 cancel marker — the operator's stop written
    half a second into the wait ends the job within about a second, with no retry."""
    s3, data = worker.fakes["s3"], worker.fakes["data"]
    data.fail_times = 1
    data.fail_with = RuntimeError("runtime returned error: An error occurred "
                                  "(internalServerException) when calling the ConverseStream "
                                  "operation: Internal Server Error")
    monkeypatch.setattr(worker, "TRANSIENT_RETRY_BACKOFF_S", (15.0,))
    clock = {"now": 0.0}
    slept: list[float] = []

    def sleep(seconds: float) -> None:
        slept.append(seconds)
        clock["now"] += seconds
        if clock["now"] >= 0.5:  # the backend writes the marker during the wait
            s3.objects[(BUCKET, jobs.cancel_key("job1"))] = b"cancel"

    real_env = worker._env
    monkeypatch.setattr(worker, "_env", lambda: dataclasses.replace(real_env(), sleep=sleep))

    worker._handle(payload())
    _wait(worker, "job1")

    done = json.loads(_obj(s3, jobs.done_key("job1", 1)))
    assert done["state"] == "STOPPED" and done["error"] == "stopped by operator"
    assert len(data.calls) == 1  # the failed attempt only — no retry after the stop
    assert clock["now"] <= 1.0 + 1e-9 and all(s <= 1.0 for s in slept), slept
    # the library the worker ships is the hub's module: same slice width
    assert worker.jobs_batch.runtime_invoke.BACKOFF_SLICE_S == 1.0


def test_a_judge_exception_is_an_error_record_not_a_failed_job(worker):
    worker.JUDGE_FACTORY = lambda model_id, **kw: FakeJudge(fail_with=RuntimeError("judge down"))
    worker._handle(payload(evaluator_defs=[{"id": "Builtin.Helpfulness"}]))
    _wait(worker, "job1")
    s3 = worker.fakes["s3"]
    done = json.loads(_obj(s3, jobs.done_key("job1", 1)))
    assert done["state"] == "COMPLETED" and done["result_count"] == 1
    assert done["summary"]["scores"] == []
    record = json.loads(_obj(s3, jobs.results_key("job1", 1)).splitlines()[0])
    assert record["error_type"] == "RuntimeError" and "judge down" in record["error_message"]


def test_a_crash_before_any_phase_is_a_failed_job(worker):
    worker._handle(payload(wait=None, sessions=[{"no_session_id": True}]))
    _wait(worker, "job1")
    done = json.loads(_obj(worker.fakes["s3"], jobs.done_key("job1", 1)))
    assert done["state"] == "FAILED" and "KeyError" in done["error"]


def test_insights_job_writes_the_artifact(worker):
    worker.JUDGE_FACTORY = lambda model_id, **kw: FakeJudge(
        text=json.dumps({"errors": []}),
        by_tool={"SessionSummary": {"intent_label": "Leave", "intent": "x",
                                    "summary_label": "Lookup", "summary": "y"}})
    worker._handle(payload(job_type="insights", kinds=[
        "Builtin.Insight.FailureAnalysis", "Builtin.Insight.UserIntent",
        "Builtin.Insight.ExecutionSummary"]))
    _wait(worker, "job1")
    s3 = worker.fakes["s3"]
    blob = json.loads(_obj(s3, jobs.artifact_key("job1", 1, "insights.json")))
    assert set(blob) == {"failures", "userIntents", "executionSummaries",
                         "failureDetection"}  # N17-obs449
    assert blob["userIntents"][0]["name"] == "Leave"
    done = json.loads(_obj(s3, jobs.done_key("job1", 1)))
    assert done["state"] == "COMPLETED" and done["summary"]["insights"] == blob


@pytest.mark.parametrize("kind", ["skill_taskgen", "skill_train", "skill_eval", "nonsense"])
def test_reserved_and_unknown_kinds_are_rejected_at_the_ack(worker, kind):
    ack = worker._handle(payload(job_type=kind))
    assert ack["state"] == "REJECTED" and ack["code"] == "evaluation.kind_unsupported"
    assert ack["job_id"] == "job1"
    assert worker.fakes["s3"].objects == {} and worker._THREADS == {}


def test_a_malformed_payload_is_rejected_not_crashed(worker):
    ack = worker._handle({"job_type": "batch_eval"})  # no job_id
    assert ack["state"] == "REJECTED" and ack["code"] == "evaluation.bad_payload"
    ack = worker._handle({**payload(), "sink": {}})
    assert ack["state"] == "REJECTED" and "bucket" in ack["message"]


def test_the_worker_has_no_otel_instrumentation_and_a_bearer_resolver(worker):
    source = (Path(worker.__file__)).read_text()
    code = source.split('"""', 2)[2]  # everything after the module docstring
    assert "opentelemetry-instrument" not in code and "AGENT_OBSERVABILITY_ENABLED" not in code
    assert "_resolve_bedrock_api_key" in source and "GetResourceApiKey" not in source.split(
        "def _resolve_bedrock_api_key")[0]
    assert "signature_version" not in source  # the bearer rides through the env, as every runtime
    assert worker.WORKER_NAME == EVAL_WORKER.name


# ─── rendering / dispatch (no execution) ──────────────────────────────────────
def test_zip_runtime_generates_the_worker_from_the_preset_template():
    from app.deployer import zip_runtime

    spec = AgentSpec(**build_spec(EVAL_WORKER, BUCKET, EVAL_WORKER.default_options()).model_dump())
    code, label = zip_runtime._generate_code(spec, "eval_worker")
    assert label == "evaluation worker template" and "BedrockAgentCoreApp" in code
    assert "launchpad_eval" in code
    assert zip_runtime._method_requirements(spec, "eval_worker") == ew.base_requirements()
    plain, _ = zip_runtime._generate_code(spec)  # an ordinary zip_runtime spec renders the agent
    assert "launchpad_eval" not in plain


def test_build_spec_pins_the_template_release_in_the_env():
    spec = build_spec(EVAL_WORKER, BUCKET, EVAL_WORKER.default_options())
    assert spec.method == "zip_runtime" and spec.skills == [] and spec.tools == []
    assert spec.env == {ew.RELEASE_ENV: ew.release_tag()}
    assert spec.name == EVAL_WORKER.name and spec.name.startswith("launchpad-eval-worker")
    from app.system_agents import presets

    assert presets.skill_version_from_spec(spec.model_dump()) == ew.EVAL_WORKER_VERSION
    assert presets.template_release_from_spec(spec.model_dump()) == ew.release_tag()


def test_the_job_thread_inherits_the_request_workload_token(worker):
    """N10v3-bug2 cascade: ``judge_model`` exchanges the Runtime-injected workload token
    (a per-request contextvar) for the Global Bedrock key — the job thread must run in a
    copy of the request context or the judge fails with "no workload access token"."""
    import contextvars

    seen: dict[str, object] = {}

    def fake_run_job(job, sink, env):
        seen["token"] = worker.BedrockAgentCoreContext.get_workload_access_token()
        sink.finish("COMPLETED")

    worker.jobs_batch.run_job = fake_run_job

    def request():
        worker.BedrockAgentCoreContext.set_workload_access_token("tok-from-request")
        return worker._handle(payload())

    ack = contextvars.copy_context().run(request)
    assert ack["state"] == "ACCEPTED"
    _wait(worker, "job1")
    assert seen["token"] == "tok-from-request"
    # request-scoped only: the process-wide context never saw it
    assert worker.BedrockAgentCoreContext.get_workload_access_token() is None


# ─── N12-bug8: the judge's key is resolved on the request thread, once ────────
class _Identity:
    """``_identity_client()`` stand-in recording WHICH thread exchanged the token."""

    def __init__(self, fail: Exception | None = None):
        self.calls: list[dict] = []
        self.fail = fail
        self.dp_client = self

    def get_resource_api_key(self, **kw):
        import threading

        self.calls.append({**kw, "thread": threading.current_thread().name})
        if self.fail is not None:
            raise self.fail
        return {"apiKey": "global-bedrock-key"}

    def get_workload_access_token(self, name):
        self.calls.append({"self_mint": name})
        raise AssertionError("the worker must never self-mint when a token was injected")


def _real_judge(worker, monkeypatch, identity):
    """Drop the judge seam so ``judge_model`` runs the real resolver against ``identity``."""
    worker.JUDGE_FACTORY = None
    monkeypatch.setattr(worker, "_identity_client", lambda: identity)
    monkeypatch.setattr(worker, "_launchpad_bedrock_model",
                        lambda *, api_key, **config: {"api_key": api_key, **config})
    worker._BEDROCK_API_KEY.clear()
    worker._WORKLOAD_TOKEN.clear()


def test_judge_model_omits_temperature_for_kimi_k3_and_keeps_it_for_glm(worker, monkeypatch):
    """N12-bug11 (t1 run 917fef1fe10e): the Kimi K3 derived evaluator's records errored with
    ValidationException "ConverseStream: This model doesn't support the temperature field" —
    ``judge_model`` always forwarded ``temperature``. The worker now carries the hub's
    capability table inlined and ``_launchpad_bedrock_model`` drops the rejected field."""
    built: list[dict] = []

    class _Recorder:
        def __init__(self, **config):
            built.append(config)

    worker.JUDGE_FACTORY = None
    monkeypatch.setenv(worker.BEDROCK_BEARER_ENV, "restored-after-test")
    monkeypatch.setattr(worker, "_resolve_bedrock_api_key", lambda: "global-bedrock-key")
    monkeypatch.setattr(worker, "BedrockModel", _Recorder)
    assert worker.MODEL_INFERENCE_PARAMS["global.zai.glm-5.3"] == {"temperature": True}
    assert worker.MODEL_INFERENCE_PARAMS["global.moonshotai.kimi-k3"] == {"temperature": False}
    assert set(worker.MODEL_INFERENCE_PARAMS) == set(allowed_model_ids())

    worker.judge_model("global.moonshotai.kimi-k3", temperature=0.0)
    worker.judge_model("global.zai.glm-5.3", max_tokens=2048, temperature=0.0)
    worker.judge_model("global.moonshotai.kimi-k3", max_tokens=512, temperature=1.0)  # optimize

    kimi, glm, kimi_opt = built
    # judge-client bounds (judge_client): every Bedrock judge gets the judge client config
    for config in built:
        client = config.pop("boto_client_config")
        assert (client.read_timeout, client.connect_timeout) == (150, 10)
        assert client.retries == {"total_max_attempts": 2, "mode": "standard"}
    assert "temperature" not in kimi and "temperature" not in kimi_opt
    # N-SCN-bug2: the judge default is the model-aware budget, not the runtime's own ceiling
    assert kimi == {"model_id": "global.moonshotai.kimi-k3", "region_name": worker.MODEL_REGION,
                    "max_tokens": 2048}
    assert worker.judge_budget("global.moonshotai.kimi-k3") == 2048
    assert worker.judge_budget("global.zai.glm-5.3") == worker.judge_budget() == 4096
    assert kimi_opt["max_tokens"] == 512
    assert glm == {"model_id": "global.zai.glm-5.3", "region_name": worker.MODEL_REGION,
                   "max_tokens": 2048, "temperature": 0.0}
    # the key went to the environment for botocore, never into the model config
    assert "api_key" not in kimi and "api_key" not in glm


def test_the_key_is_resolved_once_on_the_request_thread_and_cached_for_pool_threads(
    worker, monkeypatch,
):
    """N12-bug8: strands-evals / asyncio evaluate on thread pools of their own, which do
    not inherit the request's contextvars — a lazy first exchange there found no injected
    token, fell to self-mint and Identity refused ("linked to a service") on every record.
    The request thread resolves + caches the key BEFORE the job leaves it."""
    import concurrent.futures
    import contextvars
    import threading

    identity = _Identity()
    _real_judge(worker, monkeypatch, identity)
    pool_models: dict[str, object] = {}

    def fake_run_job(job, sink, env):
        # a pool thread with an EMPTY context, exactly like the evaluators' executors
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            pool_models["model"] = pool.submit(env.judge_factory, None).result()
            pool_models["token_seen"] = pool.submit(
                worker.BedrockAgentCoreContext.get_workload_access_token).result()
        sink.finish("COMPLETED")

    worker.jobs_batch.run_job = fake_run_job

    def request():
        worker.BedrockAgentCoreContext.set_workload_access_token("tok-from-request")
        return worker._handle(payload())

    ack = contextvars.copy_context().run(request)
    assert ack["state"] == "ACCEPTED"
    _wait(worker, "job1")
    # exactly one exchange, on the request thread, with the injected token
    assert identity.calls == [{"workloadIdentityToken": "tok-from-request",
                               "resourceCredentialProviderName": worker.MODEL_CREDENTIAL,
                               "thread": threading.current_thread().name}]
    # the pool thread never saw the context … and still got the cached key, no Identity call
    assert pool_models["token_seen"] is None
    assert pool_models["model"]["api_key"] == "global-bedrock-key"
    assert worker._BEDROCK_API_KEY["key"] == "global-bedrock-key"
    assert worker._WORKLOAD_TOKEN["value"] == "tok-from-request"  # captured for later threads
    done = json.loads(_obj(worker.fakes["s3"], jobs.done_key("job1", 1)))
    assert done["state"] == "COMPLETED"


def test_an_unobtainable_key_rejects_the_job_at_the_ack_with_a_readable_error(
    worker, monkeypatch,
):
    """When the exchange fails on the request thread the job is REJECTED there — one
    readable infrastructure error in the ack and in status.json / done.json — instead of
    one identical evaluator error per record."""
    import contextvars

    class _Refused(Exception):
        response = {"Error": {"Code": "ValidationException", "Message": "nope"}}

    identity = _Identity(fail=_Refused("GetResourceApiKey refused for this identity"))
    _real_judge(worker, monkeypatch, identity)
    worker.jobs_batch.run_job = lambda *a, **k: pytest.fail("the job must not start")

    def request():
        worker.BedrockAgentCoreContext.set_workload_access_token("tok-from-request")
        return worker._handle(payload())

    ack = contextvars.copy_context().run(request)
    assert ack["state"] == "REJECTED" and ack["code"] == "evaluation.worker_credential"
    assert ack["message"] == "_Refused: GetResourceApiKey refused for this identity"
    assert worker._THREADS == {} and "key" not in worker._BEDROCK_API_KEY
    s3 = worker.fakes["s3"]
    status = json.loads(_obj(s3, jobs.status_key("job1", 1)))
    assert status["progress"]["message"].startswith("worker rejected the job: _Refused:")
    done = json.loads(_obj(s3, jobs.done_key("job1", 1)))
    assert done["state"] == "FAILED" and done["error"] == ack["message"]
    assert done["summary"] == {"error_kind": "infrastructure"}
    assert (BUCKET, jobs.results_key("job1", 1)) not in s3.objects


def test_a_request_without_an_injected_token_is_rejected_naming_runtime_user_id(
    worker, monkeypatch,
):
    identity = _Identity()
    _real_judge(worker, monkeypatch, identity)
    monkeypatch.delenv("LAUNCHPAD_WORKLOAD_NAME", raising=False)
    worker.jobs_batch.run_job = lambda *a, **k: pytest.fail("the job must not start")
    ack = worker._handle(payload())  # no request context at all
    assert ack["state"] == "REJECTED" and ack["code"] == "evaluation.worker_credential"
    assert "runtimeUserId" in ack["message"] and identity.calls == []


# ─── N-SCN-bug2: judge output budget on the worker ───────────────────────────
def test_the_worker_inlines_the_judge_budget_table_and_ships_the_budget_helper(worker):
    """The hub's capability table and floor / cap are rendered into main.py (like
    MODEL_INFERENCE_PARAMS, N12-bug11) and the shared helper travels in the library zip."""
    from app.core.config import get_settings

    assert worker.JUDGE_DEFAULT_MAX_TOKENS["global.zai.glm-5.3"] == 4096
    assert worker.JUDGE_DEFAULT_MAX_TOKENS["global.moonshotai.kimi-k3"] == 2048
    assert set(worker.JUDGE_DEFAULT_MAX_TOKENS) == set(allowed_model_ids())  # N-R6: every row
    assert worker.JUDGE_MAX_TOKENS_FLOOR == get_settings().eval_judge_max_tokens == 1024
    assert worker.JUDGE_MAX_TOKENS_CAP == get_settings().eval_judge_max_tokens_cap == 16384
    assert worker.judge_budget() == 4096 and worker.judge_budget("custom.model") == 1024
    assert "launchpad_eval/model_calls.py" in ew.library_files()
    assert worker.model_calls.call_with_budget is not None
    env = worker._env()
    assert env.judge_budget_cap == 16384 and env.judge_budget("global.moonshotai.kimi-k3") == 2048
    source = (ew.TEMPLATE_DIR / "main.py.tmpl").read_text(encoding="utf-8")
    for placeholder in ("__LAUNCHPAD_JUDGE_DEFAULT_MAX_TOKENS__",
                        "__LAUNCHPAD_JUDGE_MAX_TOKENS_FLOOR__",
                        "__LAUNCHPAD_JUDGE_MAX_TOKENS_CAP__"):
        assert placeholder in source
    assert "__LAUNCHPAD_" not in ew.render_main_py(
        build_spec(EVAL_WORKER, BUCKET, EVAL_WORKER.default_options()))


def test_a_judge_that_hits_its_output_limit_is_rejudged_with_a_doubled_budget(worker):
    """Counting run #2 (a) on the worker path: the first attempt runs at the model-aware default
    (GLM 5.3 → 4096), the retry on a fresh judge at 8192 — one scored record, no error."""
    from strands.types.exceptions import MaxTokensReachedException

    calls: list[tuple] = []

    def factory(model_id, max_tokens=None, **kw):
        calls.append((model_id, max_tokens))
        if (max_tokens or worker.judge_budget(model_id)) < 8192:
            return FakeJudge(fail_with=MaxTokensReachedException("maximum token limit"))
        return FakeJudge(overrides={"score": "Very helpful", "reasoning": "fits"})

    worker.JUDGE_FACTORY = factory
    worker._handle(payload(evaluator_defs=[{"id": "Builtin.Helpfulness"}]))
    _wait(worker, "job1")
    s3 = worker.fakes["s3"]
    done = json.loads(_obj(s3, jobs.done_key("job1", 1)))
    assert done["state"] == "COMPLETED" and done["result_count"] == 1
    record = json.loads(_obj(s3, jobs.results_key("job1", 1)).splitlines()[0])
    assert record["score"] == 0.833 and record["error_type"] is None
    assert calls == [(worker.MODEL_ID, None), (worker.MODEL_ID, 8192)]


def test_a_judge_that_never_fits_is_an_error_record_naming_the_cap(worker):
    from strands.types.exceptions import MaxTokensReachedException

    budgets: list = []

    def factory(model_id, max_tokens=None, **kw):
        budgets.append(max_tokens)
        return FakeJudge(fail_with=MaxTokensReachedException("maximum token limit"))

    worker.JUDGE_FACTORY = factory
    worker._handle(payload(evaluator_defs=[{"id": "Builtin.Helpfulness"}]))
    _wait(worker, "job1")
    record = json.loads(_obj(worker.fakes["s3"], jobs.results_key("job1", 1)).splitlines()[0])
    assert budgets == [None, 8192, 16384]
    assert record["error_type"] == "JudgeBudgetExceeded"
    assert record["error_message"] == (
        "judge Builtin.Helpfulness exceeded 16384 output tokens after 3 attempts"
        # N17-obs385: the rescue steps the error names
        "; the context had nothing to truncate; no judge_fallback model is configured")


# ─── N-R6: a SiliconFlow judge goes through the inlined provider dispatch ───────────


@pytest.mark.siliconflow
def test_judge_model_on_a_siliconflow_id_builds_an_openai_model_with_its_provider_key(
    worker, monkeypatch
):
    sf = "siliconflow.zai-org/GLM-5.3"
    assert worker.MODEL_PROVIDERS[sf] == {
        "kind": "openai_compatible", "base_url": "https://api.siliconflow.cn/v1",
        "api_model_name": "zai-org/GLM-5.3", "credential_provider": "launchpad-siliconflow",
        "streaming": True,
    }
    built: list[dict] = []

    class _OpenAI:
        def __init__(self, **config):
            built.append(config)

    exchanges: list[str] = []

    class _Identity:
        dp_client = None

        def __init__(self):
            self.dp_client = self

        def get_resource_api_key(self, **kw):
            exchanges.append(kw["resourceCredentialProviderName"])
            return {"apiKey": "sf-judge-key"}

    worker.JUDGE_FACTORY = None
    monkeypatch.setattr(worker, "OpenAIModel", _OpenAI)
    monkeypatch.setattr(worker, "_identity_client", lambda: _Identity())
    monkeypatch.setattr(worker, "_workload_access_token", lambda: "wl-token")
    monkeypatch.setattr(worker, "_launchpad_bedrock_model",
                        lambda **kw: pytest.fail("Bedrock path taken for a SiliconFlow judge"))
    worker._PROVIDER_API_KEYS.clear()

    worker.judge_model(sf, temperature=0.0)
    worker.judge_model(sf, max_tokens=2048, temperature=0.5)
    assert exchanges == ["launchpad-siliconflow"]  # once per process
    assert built == [
        {"client_args": {"base_url": "https://api.siliconflow.cn/v1", "api_key": "sf-judge-key"},
         "model_id": "zai-org/GLM-5.3", "params": {"max_tokens": 4096, "temperature": 0.0},
         "stream": True},
        {"client_args": {"base_url": "https://api.siliconflow.cn/v1", "api_key": "sf-judge-key"},
         "model_id": "zai-org/GLM-5.3", "params": {"max_tokens": 2048, "temperature": 0.5},
         "stream": True},
    ]
    assert worker.JUDGE_DEFAULT_MAX_TOKENS[sf] == 4096


# ── D21 (M1-d): job payload descriptors go in front of the inlined tables ──
def test_payload_descriptors_are_registered_before_the_inlined_tables(worker):
    module = worker
    from app.schemas.agent import model_descriptor

    kimi = "global.moonshotai.kimi-k3"
    rendered_budget = module.judge_budget(kimi)
    descriptor = {**model_descriptor(kimi), "judge_max_tokens": rendered_budget + 512,
                  "catalog_version": module.MODEL_CATALOG_VERSION + 1,
                  "inference_params": {"temperature": True, "top_p": True}}
    payload = {"job_id": "j-desc", "job_type": "batch_eval", "workspace": {}, "sink": {},
               "spec": {}, "models": {kimi: descriptor}}
    job = jobs.JobSpec.from_payload(payload)
    assert job.models == {kimi: descriptor} and job.to_payload()["models"] == {kimi: descriptor}
    assert module._launchpad_register_descriptors(job.models) == 1
    # descriptor-first: the provider row, the judge budget and the inference params
    assert module._model_provider(kimi) is module.MODEL_DESCRIPTORS[kimi]
    assert module.judge_budget(kimi) == min(rendered_budget + 512, module.JUDGE_MAX_TOKENS_CAP)
    assert module._supported_model_config({"model_id": kimi, "temperature": 0.1}) == {
        "model_id": kimi, "temperature": 0.1}
    # an old payload without the field → the inlined tables, exactly as before
    old = jobs.JobSpec.from_payload({k: v for k, v in payload.items() if k != "models"})
    assert old.models == {}
    module.MODEL_DESCRIPTORS.clear()
    module._launchpad_register_descriptors(old.models)
    assert module._model_provider(kimi) == module.MODEL_PROVIDERS[kimi]
    assert module.judge_budget(kimi) == rendered_budget


def test_worker_block_is_the_strands_block_with_descriptor_support(worker):
    module = worker
    from app.schemas.agent import catalog_version

    assert module.MODEL_CATALOG_VERSION == catalog_version()
    assert module.MODEL_DESCRIPTOR["model_id"] == module.MODEL_ID
    assert callable(module._launchpad_register_descriptors)


# ─── N12-obs53 / N12-obs55: transient upstream errors are retried with backoff ─────────
class FlakyJudge(FakeJudge):
    """A judge whose first ``len(failures)`` model calls die on the queued exceptions
    (the 09:44Z ``EventStreamError`` on ConverseStream) and then answer normally."""

    def __init__(self, failures, **kw):
        super().__init__(**kw)
        self.failures = list(failures)
        self.streams = 0

    async def stream(self, messages, tool_specs=None, system_prompt=None, **kwargs):
        self.streams += 1
        if self.failures:
            raise self.failures.pop(0)
        async for event in super().stream(messages, tool_specs=tool_specs,
                                          system_prompt=system_prompt, **kwargs):
            yield event


def _judge_5xx():
    from botocore.exceptions import EventStreamError

    return EventStreamError(
        {"Error": {"Code": "internalServerException",
                   "Message": "Internal Server Error. Try your request again."}},
        "ConverseStream",
    )


def test_the_worker_inlines_the_transient_retry_backoff_from_the_hub_setting(worker, monkeypatch):
    from app.core.config import get_settings

    # conftest pins the setting to "0": the hermetic worker retries at once …
    assert worker.TRANSIENT_RETRY_BACKOFF_S == get_settings().eval_transient_retry_backoff == (0.0,)
    assert worker._env().transient_backoff_s == (0.0,)
    # … and the rendered constant FOLLOWS the setting (a hub override reaches the worker on
    # repair, like the judge budget cap)
    monkeypatch.setattr(get_settings(), "eval_transient_retry_backoff_seconds", "3,4")
    spec = build_spec(EVAL_WORKER, BUCKET, EVAL_WORKER.default_options())
    rendered = ew.render_main_py(spec)
    assert "TRANSIENT_RETRY_BACKOFF_S: tuple[float, ...] = (3.0, 4.0)\n" in rendered
    assert "__LAUNCHPAD_" not in rendered
    # the shipped predicate is the hub's: one list, re-exported by the Runtime wrapper
    from app.services.agentcore import runtime as rt

    library = worker.jobs_batch.runtime_invoke
    assert library.UPSTREAM_TRANSIENT_MARKERS == rt.UPSTREAM_TRANSIENT_MARKERS
    assert library.is_upstream_transient(_judge_5xx()) is True


def test_a_judge_5xx_is_retried_and_the_session_is_still_scored(worker):
    """N12-obs53 (live 2026-10-16 09:44Z): the GLM judge call died on a Bedrock
    internalServerException surfaced as EventStreamError; the record errored and the run
    completed with errors. Now the call is retried (3 attempts) and the session scores."""
    judge = FlakyJudge([_judge_5xx(), _judge_5xx()],
                       overrides={"score": "Very helpful", "reasoning": "clear"})
    worker.JUDGE_FACTORY = lambda model_id, **kw: judge
    worker._handle(payload(evaluator_defs=[{"id": "Builtin.Helpfulness"}]))
    _wait(worker, "job1")
    s3 = worker.fakes["s3"]
    done = json.loads(_obj(s3, jobs.done_key("job1", 1)))
    assert done["state"] == "COMPLETED" and done["result_count"] == 1
    record = json.loads(_obj(s3, jobs.results_key("job1", 1)).splitlines()[0])
    assert record["error_type"] is None and record["score"] is not None
    assert judge.streams == 3  # two 5xx, then the answer


def test_a_judge_that_stays_5xx_is_an_error_record_typed_as_the_cause_naming_the_attempts(
        worker):
    judge = FlakyJudge([_judge_5xx()] * 10)
    worker.JUDGE_FACTORY = lambda model_id, **kw: judge
    worker._handle(payload(evaluator_defs=[{"id": "Builtin.Helpfulness"}]))
    _wait(worker, "job1")
    s3 = worker.fakes["s3"]
    done = json.loads(_obj(s3, jobs.done_key("job1", 1)))
    assert done["state"] == "COMPLETED" and done["summary"]["scores"] == []
    record = json.loads(_obj(s3, jobs.results_key("job1", 1)).splitlines()[0])
    assert record["score"] is None and record["error_type"] == "EventStreamError"
    assert record["error_message"].startswith(
        "judge Builtin.Helpfulness errored on every one of 3 attempts (transient upstream): "
        "EventStreamError: An error occurred (internalServerException)")
    assert "failed after" not in record["error_message"]  # not an unanswered scenario
    assert judge.streams == 3  # the whole budget, nothing more


def test_a_replay_5xx_error_payload_is_retried_in_the_same_session(worker):
    """N12-obs55 (live 2026-10-16 13:29Z): `failed after 1/3 attempts: RuntimeError: runtime
    returned error: An error occurred (internalServerException)…` — the runtime answered the
    SSE `{"error": …}` payload and the class was not retried. Now it is, in the same
    pre-assigned session, and the retry's cause is on the invocation line."""
    data = worker.fakes["data"]
    data.fail_times, data.fail_with = 1, RuntimeError(
        "runtime returned error: An error occurred (internalServerException) when calling the "
        "ConverseStream operation: Internal Server Error. Try your request again.")
    worker._handle(payload())
    _wait(worker, "job1")
    s3 = worker.fakes["s3"]
    done = json.loads(_obj(s3, jobs.done_key("job1", 1)))
    assert done["state"] == "COMPLETED" and done["result_count"] == 2
    assert len(data.calls) == 2 and {c["runtimeSessionId"] for c in data.calls} == {SID}
    [inv] = [json.loads(line) for line in _obj(s3, jobs.invocations_key("job1", 1)).splitlines()]
    assert inv["status"] == "ok" and inv["retries"] == 1
    [retry] = inv["retried"]
    assert retry["attempt"] == 1 and retry["backoff_s"] == 0.0  # conftest: "0"
    assert retry["reason"].startswith("RuntimeError: runtime returned error: An error occurred "
                                      "(internalServerException)")


def _online_payload(**spec_overrides):
    """An ``online_eval`` job for the rendered worker (the scheduler's spec shape)."""
    return payload(
        job_type="online_eval", config_id="oe_a-1", mode="scores",
        sessions=[{"session_id": SID}], log_groups=[GROUP], lookback_hours=24,
        evaluator_defs=[{"id": "Builtin.Helpfulness"}], judge_model_id="global.zai.glm-5.3",
        **spec_overrides,
    )


def test_the_rendered_worker_online_job_retries_a_judge_5xx_with_the_inlined_backoff(
        worker, monkeypatch):
    """N13-obs14: the ONLINE job's judge calls take the worker's rendered
    `TRANSIENT_RETRY_BACKOFF_S` through the dispatcher (`WorkerEnv.transient_backoff_s`) —
    two 5xx then the answer: the session scores, the waits are the inlined list slept in
    ≤ 1 s slices (N13-obs8)."""
    monkeypatch.setattr(worker, "TRANSIENT_RETRY_BACKOFF_S", (3.0, 4.0))
    slept: list[float] = []
    real_env = worker._env
    monkeypatch.setattr(worker, "_env",
                        lambda: dataclasses.replace(real_env(), sleep=slept.append))
    judge = FlakyJudge([_judge_5xx(), _judge_5xx()],
                       overrides={"score": "Very helpful", "reasoning": "clear"})
    worker.JUDGE_FACTORY = lambda model_id, **kw: judge

    worker._handle(_online_payload())
    _wait(worker, "job1")

    s3 = worker.fakes["s3"]
    done = json.loads(_obj(s3, jobs.done_key("job1", 1)))
    assert done["state"] == "COMPLETED" and done["result_count"] == 1
    record = json.loads(_obj(s3, jobs.results_key("job1", 1)).splitlines()[0])
    assert record["error_type"] is None and record["score"] is not None
    assert judge.streams == 3  # two 5xx, then the answer
    assert abs(sum(slept) - 7.0) < 1e-9 and all(0 < s <= 1.0 for s in slept), slept
    # the rendered constant's comment states the online path shares it
    source = Path(worker.__file__).read_text(encoding="utf-8")
    assert "N13-obs14" in source and "TRANSIENT_RETRY_BACKOFF_S: tuple[float, ...] = (" in source


def test_the_rendered_worker_online_job_records_an_exhausted_judge_typed_as_the_cause(worker):
    judge = FlakyJudge([_judge_5xx()] * 10)
    worker.JUDGE_FACTORY = lambda model_id, **kw: judge
    worker._handle(_online_payload())
    _wait(worker, "job1")
    s3 = worker.fakes["s3"]
    done = json.loads(_obj(s3, jobs.done_key("job1", 1)))
    assert done["state"] == "COMPLETED" and done["summary"]["scores"] == []
    assert done["result_count"] == 1  # single-counted
    record = json.loads(_obj(s3, jobs.results_key("job1", 1)).splitlines()[0])
    assert record["score"] is None and record["error_type"] == "EventStreamError"
    assert record["error_message"].startswith(
        "judge Builtin.Helpfulness errored on every one of 3 attempts (transient upstream): "
        "EventStreamError: An error occurred (internalServerException)")
    assert "failed after" not in record["error_message"]
    assert judge.streams == 3
