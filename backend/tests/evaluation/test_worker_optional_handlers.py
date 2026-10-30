"""E2 hand-over: the worker dispatcher binds ``jobs_online`` / ``jobs_recommend`` /
``jobs_optimize`` by module name the moment the inliner ships them (no entrypoint edit),
hands each ``run()`` the dependency surface evaluation-engine.md §19 lists, and stays a clean
``kind_unsupported`` FAILED job while a module is absent."""

import sys
import types

import pytest

from app.evaluation import jobs
from app.services.strands_eval import engine_core, jobs_batch
from app.templates import eval_worker

PACKAGE = jobs_batch.__package__
MODULES = {"online_eval": "jobs_online", "recommend": "jobs_recommend",
           "optimize": "jobs_optimize"}


@pytest.fixture
def registry(monkeypatch):
    monkeypatch.setattr(jobs_batch, "HANDLERS",
                        {"batch_eval": jobs_batch.run_batch_eval,
                         "insights": jobs_batch.run_insights})
    monkeypatch.setattr(jobs_batch, "_OPTIONAL_PROBED", set())
    return jobs_batch.HANDLERS


class Sink:
    def __init__(self, cancelled=False):
        self.finished = False
        self.calls = []
        self._cancelled = cancelled
        self.job = types.SimpleNamespace(job_id="j3")  # _check_cancel names the job

    def cancelled(self):
        return self._cancelled

    def finish(self, state, error=None, **_):
        self.finished = True
        self.calls.append((state, error))


def _env(**extra):
    return jobs_batch.WorkerEnv(logs_client="LOGS", data_client="DATA",
                                judge_factory=lambda m, **kw: f"judge:{m}",
                                lambda_client="LAMBDA", spans_source="spans", clock=lambda: 100.0,
                                extra=extra)


def _job(job_type, spec=None, job_id="j1"):
    return jobs.JobSpec(job_id=job_id, job_type=job_type, attempt=1, workspace={},
                        sink={"bucket": "b", "prefix": f"evaluations/{job_id}/a1"},
                        spec=spec or {})


def _fake_module(name, calls):
    module = types.ModuleType(f"{PACKAGE}.{name}")

    def run(job, sink, **dependencies):
        calls.append((name, job.job_id, dependencies))

    module.run = run
    return module


def test_the_hook_is_keyed_on_the_modules_the_inliner_ships():
    assert {m for _, m, _ in jobs_batch.OPTIONAL_HANDLERS} == set(MODULES.values())
    optional = {name[: -len(".py")] for name in eval_worker._OPTIONAL_SOURCES}
    assert optional == set(MODULES.values())
    assert {t for t, _, _ in jobs_batch.OPTIONAL_HANDLERS} <= set(jobs.JOB_TYPES)
    # E2 has landed: the manifest ships all three next to the modules they import relatively
    shipped = set(eval_worker.library_manifest())
    for name in (*MODULES.values(), "spans_provider", "evaluators", "results", "sink"):
        assert f"{eval_worker.LIBRARY_PACKAGE}/{name}.py" in shipped


def test_the_landed_modules_register_through_the_hook(registry):
    assert jobs_batch.register_optional_handlers() == list(MODULES)
    assert set(registry) == {"batch_eval", "insights", *MODULES}
    assert jobs_batch.register_optional_handlers() == []  # idempotent


def test_absent_modules_register_nothing_and_the_kind_stays_unsupported(registry, monkeypatch):
    for name in MODULES.values():
        monkeypatch.setitem(sys.modules, f"{PACKAGE}.{name}", None)  # import → not found
    assert jobs_batch.register_optional_handlers() == []
    assert set(registry) == {"batch_eval", "insights"}
    assert jobs_batch._OPTIONAL_PROBED == set(MODULES.values())  # probed once, not per job
    sink = Sink()
    jobs_batch.run_job(_job("online_eval"), sink, _env())
    assert sink.calls[0][0] == "FAILED" and "kind_unsupported" in sink.calls[0][1]


def test_run_hands_each_module_the_documented_dependency_surface(registry, monkeypatch):
    calls: list = []
    for name in MODULES.values():
        monkeypatch.setitem(sys.modules, f"{PACKAGE}.{name}", _fake_module(name, calls))
    env = _env(s3_client="S3", converse="CONVERSE", optimizer_factory="OPT")
    jobs_batch.run_job(_job("online_eval"), Sink(), env)
    jobs_batch.run_job(_job("recommend", job_id="j2"), Sink(), env)
    jobs_batch.run_job(_job("optimize", job_id="j3", spec={
        "target": {"runtime_arn": "arn:aws-cn:x"}, "evaluator_def": {"id": "Builtin.Helpfulness"},
        "log_groups": ["/lg", "aws/spans"]}), Sink(), env)
    by_module = {name: (job_id, deps) for name, job_id, deps in calls}
    assert by_module["jobs_online"] == ("j1", {
        "logs_client": "LOGS", "judge_factory": env.judge_factory, "lambda_client": "LAMBDA",
        "spans_source": "spans", "sleep": env.sleep,
        "transient_backoff": env.transient_backoff_s})  # N13-obs14
    assert by_module["jobs_recommend"] == ("j2", {"judge_factory": env.judge_factory,
                                                 "converse": "CONVERSE"})
    job_id, deps = by_module["jobs_optimize"]
    assert job_id == "j3" and set(deps) == {"judge_factory", "invoke", "score", "s3_client",
                                            "optimizer_factory"}
    assert deps["s3_client"] == "S3" and deps["optimizer_factory"] == "OPT"
    assert callable(deps["invoke"]) and callable(deps["score"])


def test_optimize_invoke_replays_against_the_target_with_the_bundle_baggage():
    seen = []

    def invoke_seam(prompt, session_id, session):
        seen.append((prompt, session_id, session))
        return {"text": "hello", "session_id": session_id}

    env = jobs_batch.WorkerEnv(logs_client="LOGS", data_client="DATA",
                               judge_factory=lambda m, **kw: None, invoke=invoke_seam)
    spec = {"target": {"runtime_arn": "arn:aws-cn:x", "qualifier": "DEFAULT"},
            "evaluator_def": {"id": "Builtin.Helpfulness"}, "log_groups": ["/lg"]}
    invoke, _ = jobs_batch.optimize_bindings(env, Sink(), spec, job_id="j3")
    out = invoke("q", session_id="opt-1", bundle_ref="opt_ab:1",
                 baggage="launchpad.bundle=opt_ab:1", launchpad_meta={"bundle": "opt_ab:1"})
    assert out == "hello"
    assert seen == [("q", "opt-1", {"session_id": "opt-1", "bundle_ref": "opt_ab:1",
                                   "baggage": "launchpad.bundle=opt_ab:1",
                                   "launchpad_meta": {"bundle": "opt_ab:1"},
                                   "qualifier": "DEFAULT"})]


def test_optimize_score_flushes_then_evaluates_with_the_job_evaluator(monkeypatch):
    waits, evaluated = [], []
    monkeypatch.setattr(jobs_batch, "_wait_for_spans",
                        lambda env, sink, wait, sid, start_ms: waits.append((sid, start_ms,
                                                                            wait["seconds"])))

    def fake_evaluate_session(provider, session_id, evaluators, truth, *, job_id, sleep, stop):
        evaluated.append((session_id, [d["id"] for d in evaluators.definitions], job_id))
        return [{"score": 0.5}, {"score": 1.0}, {"score": None, "error": "n/a"}]

    monkeypatch.setattr(engine_core, "evaluate_session", fake_evaluate_session)
    env = _env()
    spec = {"target": {"runtime_arn": "arn:aws-cn:x"},
            "evaluator_def": {"id": "Builtin.Helpfulness"}, "log_groups": ["/lg", "aws/spans"]}
    invoke, score = jobs_batch.optimize_bindings(env, Sink(), spec, job_id="j3")
    env.invoke = lambda prompt, sid, session: {"text": "t", "session_id": sid}
    invoke("q", session_id="opt-1", bundle_ref="r:1", baggage=None, launchpad_meta=None)
    assert score("opt-1", "q", "t") == 0.75
    assert waits == [("opt-1", 100_000, jobs_batch.OPTIMIZE_FLUSH_SECONDS)]
    assert evaluated == [("opt-1", ["Builtin.Helpfulness"], "j3")]


def test_optimize_score_reports_judge_errors_and_honours_cancel(monkeypatch):
    monkeypatch.setattr(jobs_batch, "_wait_for_spans", lambda *a, **k: None)
    monkeypatch.setattr(engine_core, "evaluate_session",
                        lambda *a, **k: [{"score": None, "error": "judge down"}])
    env = _env()
    spec = {"target": {}, "evaluator_def": {"id": "Builtin.Helpfulness"}, "log_groups": ["/lg"]}
    _, score = jobs_batch.optimize_bindings(env, Sink(), spec, job_id="j3")
    with pytest.raises(RuntimeError, match="judge down"):  # LaunchpadReward → reward 0
        score("opt-1", "q", "t")
    monkeypatch.setattr(engine_core, "evaluate_session", lambda *a, **k: [{"score": None}])
    assert score("opt-1", "q", "t") is None
    _, score = jobs_batch.optimize_bindings(env, Sink(cancelled=True), spec, job_id="j3")
    with pytest.raises(jobs_batch.JobCancelled):
        score("opt-1", "q", "t")
    _, score = jobs_batch.optimize_bindings(env, Sink(), {"target": {}, "log_groups": ["/lg"]},
                                            job_id="j3")
    with pytest.raises(RuntimeError, match="evaluator_def"):
        score("opt-1", "q", "t")


def test_the_worker_entrypoint_hands_the_s3_client_and_the_keyword_judge_factory():
    source = (eval_worker.TEMPLATE_DIR / "main.py.tmpl").read_text(encoding="utf-8")
    assert 'extra={"s3_client": _client("s3")}' in source
    assert ("def judge_model(model_id: str | None = None, *, max_tokens: int | None = None,"
            in source)
