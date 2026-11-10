"""``harness_optimizer`` (N12 E2-5): disabled by default, submits an ``optimize`` job whose
spec carries the run's dataset prompts / target / bundle location; the worker-side handler
rolls the candidate prompt out as temporary bundle versions, scores the rollouts and lets
the (here scripted) contrastive optimizer rewrite the prompt epoch by epoch."""

from __future__ import annotations

import json

import pytest

from app.core.config import get_settings
from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.evaluation import jobs
from app.evaluation.executor import JobSink, MemoryStore
from app.evaluation.jobs import JobDone, JobSpec
from app.evaluation.models import EvalDataset, EvalRun
from app.optimization.models import ConfigBundle
from app.services.strands_eval import jobs_optimize
from tests.evaluation.fakes import FakeModel, FakeS3
from tests.evaluation.test_run_recommendations import SESSIONS, _agent, _results, _rows, _stub

JOB_ID = "provider-harness_optimizer-0123456789ab"


def _job(prompts, epochs=2, **spec) -> JobSpec:
    return JobSpec(
        job_id=JOB_ID, job_type="optimize",
        workspace={"account_id": "1", "region": "cn-northwest-1"},
        sink={"bucket": "launchpad-artifacts", "prefix": jobs.job_prefix(JOB_ID, 1)},
        spec={"provider": "harness_optimizer", "model_id": "global.zai.glm-5.3",
              "current_prompt": "v0 prompt", "dataset_prompts": prompts, "epochs": epochs,
              "max_tokens": 4096,
              "bundle": {"bucket": "launchpad-artifacts", "agent_id": "agent-1",
                         "bundle_id": "opt_0123456789ab"},
              "target": {"runtime_arn": "arn:aws-cn:bedrock-agentcore:cn-northwest-1:1:runtime/r",
                         "agent_id": "agent-1"}, **spec},
    )


class ScriptedOptimizer:
    """Stands in for ContrastiveReflectionOptimizer: each step applies the next prompt."""

    def __init__(self, formula, model, prompts):
        self.formula, self.model, self.prompts = formula, model, list(prompts)
        self.seen: list[tuple[int, list[float]]] = []
        self._rollouts, self._rewards = [], []

    def add_rollouts(self, rollouts):
        self._rollouts.extend(rollouts)

    def add_rewards(self, rewards):
        self._rewards.extend(rewards)

    def step(self):
        self.seen.append((len(self._rollouts), [r.reward for r in self._rewards]))
        if self.prompts:
            self.formula.update_params({"system_prompt": self.prompts.pop(0)})

    def zero(self):
        self._rollouts, self._rewards = [], []


def test_optimize_job_writes_a_bundle_version_per_epoch_and_reports_the_final_prompt():
    store, s3 = MemoryStore(), FakeS3()
    job = _job([{"scenario_id": "S1", "prompt": "q1"}, {"scenario_id": "S2", "prompt": "q2"}])
    sink = JobSink(store, job)
    invoked: list[dict] = []
    scored: list[str] = []
    factory_calls: list[tuple] = []
    optimizers: list[ScriptedOptimizer] = []

    def invoke(prompt, *, session_id, bundle_ref, baggage, launchpad_meta):
        invoked.append({"prompt": prompt, "session_id": session_id, "ref": bundle_ref,
                        "baggage": baggage, "meta": launchpad_meta})
        return f"answer to {prompt} under {bundle_ref}"

    def score(session_id, prompt, output):
        scored.append(session_id)
        return 0.25 if ":1" in output else 0.75

    def judge_factory(model_id, **kw):
        factory_calls.append((model_id, kw))
        return FakeModel()

    def optimizer_factory(formula, model):
        optimizers.append(ScriptedOptimizer(formula, model, ["v1 prompt", "v2 prompt"]))
        return optimizers[-1]

    jobs_optimize.run(job, sink, judge_factory=judge_factory, invoke=invoke, score=score,
                      s3_client=s3, optimizer_factory=optimizer_factory)

    assert factory_calls == [("global.zai.glm-5.3", {"max_tokens": 4096, "temperature": 1.0})]
    assert isinstance(optimizers[0].model, FakeModel)  # the injected model, never a default
    # epoch 1 rolls out v0 under version 1, epoch 2 rolls out v1 under version 2, final = v2
    keys = sorted(k for (_, k) in s3.objects)
    assert keys == [f"bundles/agent-1/opt_0123456789ab/v{n}.json" for n in (1, 2, 3)]
    assert [json.loads(s3.objects[("launchpad-artifacts", k)])["components"]["configuration"][
        "system_prompt"] for k in keys] == ["v0 prompt", "v1 prompt", "v2 prompt"]
    assert [i["ref"] for i in invoked] == ["opt_0123456789ab:1"] * 2 + ["opt_0123456789ab:2"] * 2
    assert invoked[0]["baggage"] == "launchpad.bundle=opt_0123456789ab:1"
    assert invoked[0]["meta"] == {"bundle": "opt_0123456789ab:1"}
    assert len({i["session_id"] for i in invoked}) == 4 and len(scored) == 4
    assert optimizers[0].seen == [(2, [0.25, 0.25]), (2, [0.75, 0.75])]
    done = JobDone.from_dict(json.loads(store.get(jobs.done_key(JOB_ID, 1)).decode()))
    assert done.state == "COMPLETED" and done.summary["provider"] == "harness_optimizer"
    result = done.summary["optimize"]
    assert result["status"] == "COMPLETED" and result["recommended_prompt"] == "v2 prompt"
    assert [e["avg_reward"] for e in result["meta"]["epochs"]] == [0.25, 0.75]
    assert result["meta"]["best_epoch"] == 2 and result["meta"]["rollouts"] == 4
    assert result["meta"]["final_bundle_ref"] == "opt_0123456789ab:3"
    assert done.summary["bundle_refs"] == [f"opt_0123456789ab:{n}" for n in (1, 2, 3)]
    assert done.summary["bundles"]["opt_0123456789ab:3"]["configuration"]["system_prompt"] == (
        "v2 prompt")
    lines = store.get(jobs.invocations_key(JOB_ID, 1)).decode().splitlines()
    assert len(lines) == 4 and json.loads(lines[0])["arm"] == "T1"
    assert json.loads(store.get(jobs.artifact_key(JOB_ID, 1, "optimize.json")).decode()) == result


def test_optimize_job_honours_cancel_and_refuses_an_empty_dataset():
    store, s3 = MemoryStore(), FakeS3()
    job = _job([{"scenario_id": "S1", "prompt": "q1"}])
    store.put(jobs.cancel_key(JOB_ID), b"cancel")
    jobs_optimize.run(job, JobSink(store, job), judge_factory=lambda *a, **k: FakeModel(),
                      invoke=lambda *a, **k: "x", score=lambda *a: 1.0, s3_client=s3,
                      optimizer_factory=lambda f, m: ScriptedOptimizer(f, m, []))
    assert JobDone.from_dict(json.loads(store.get(jobs.done_key(JOB_ID, 1)).decode())).state == (
        "STOPPED")
    empty_store = MemoryStore()
    empty = _job([])
    jobs_optimize.run(empty, JobSink(empty_store, empty), judge_factory=lambda *a, **k: FakeModel(),
                      invoke=lambda *a, **k: "x", score=lambda *a: 1.0, s3_client=s3)
    done = JobDone.from_dict(json.loads(empty_store.get(jobs.done_key(JOB_ID, 1)).decode()))
    assert done.state == "FAILED" and "dataset" in done.error


def test_unchanged_prompt_reads_as_failed_and_unscorable_rollouts_count_zero():
    store, s3 = MemoryStore(), FakeS3()
    job = _job([{"scenario_id": "S1", "prompt": "q1"}], epochs=1)

    def score(*_a):
        raise RuntimeError("spans not found")

    jobs_optimize.run(job, JobSink(store, job), judge_factory=lambda *a, **k: FakeModel(),
                      invoke=lambda *a, **k: "x", score=score, s3_client=s3,
                      optimizer_factory=lambda f, m: ScriptedOptimizer(f, m, []))
    done = JobDone.from_dict(json.loads(store.get(jobs.done_key(JOB_ID, 1)).decode()))
    result = done.summary["optimize"]
    assert result["status"] == "FAILED" and result["recommended_prompt"] is None
    assert "unchanged" in result["error"] and result["meta"]["epochs"][0]["avg_reward"] == 0.0


def test_reflection_optimizer_uses_the_injected_model():
    from strands_harness_optimizer.formulas import SystemPromptFormula

    model = FakeModel()
    optimizer = jobs_optimize.LaunchpadReflectionOptimizer(
        SystemPromptFormula(system_prompt="p"), model,
        model_config={"model_id": "global.zai.glm-5.3", "max_tokens": 512, "temperature": 1.0})
    agent = optimizer._create_agent("you optimise prompts")
    assert agent.model is model
    assert any("submit_optimized_params" in str(t) for t in agent.tool_names)


def test_provider_submits_an_optimize_job_with_dataset_target_and_bundle(client, monkeypatch):
    executor = _stub(monkeypatch)
    monkeypatch.setattr(get_settings(), "prompt_opt_harness_optimizer_enabled", True)
    db = SessionLocal()
    dataset = EvalDataset(workspace_id=DEFAULT_WORKSPACE_ID, name="ds", kind="predefined", items=[
        {"scenario_id": "S1", "turns": [{"input": "How much PTO do I have?"}]},
        {"scenario_id": "S2", "turns": [{"input": "Book a day off"}]},
        {"scenario_id": "S3", "turns": [{"input": "   "}]},
    ])
    db.add(dataset)
    db.commit()
    agent_id = _agent(arn="arn:aws-cn:bedrock-agentcore:cn-northwest-1:1:runtime/rt-1")
    run = EvalRun(workspace_id=DEFAULT_WORKSPACE_ID, agent_id=agent_id, agent_name="a",
                  status="completed", evaluators=["Builtin.Helpfulness"],
                  session_ids=list(SESSIONS), dataset_id=dataset.id)
    db.add(run)
    db.commit()
    run_id = run.id
    db.close()
    _results(run_id)
    from tests.conftest import set_default_resources
    set_default_resources({"artifacts_bucket": "launchpad-artifacts"})

    res = client.post(f"/api/eval/runs/{run_id}/recommendations", json={
        "kinds": ["system_prompt"], "system_prompt": "Be helpful.",
        "provider": "harness_optimizer", "evaluator": "Builtin.Helpfulness"})
    assert res.status_code == 201, res.text
    job = executor.submitted[0]
    assert job.job_type == "optimize" and job.job_id.startswith("provider-harness_optimizer-")
    spec = job.spec
    assert spec["dataset_prompts"] == [{"scenario_id": "S1", "prompt": "How much PTO do I have?"},
                                       {"scenario_id": "S2", "prompt": "Book a day off"}]
    assert spec["target"]["agent_id"] == agent_id and spec["target"]["runtime_arn"].endswith("rt-1")
    assert spec["bundle"] == {"bucket": "launchpad-artifacts", "agent_id": agent_id,
                              "bundle_id": f"opt_{job.job_id.rsplit('-', 1)[-1]}"}
    assert spec["evaluator_def"] == {"id": "Builtin.Helpfulness"}
    assert spec["log_groups"] == ["/aws/bedrock-agentcore/runtimes/rt-1-DEFAULT", "aws/spans"]
    assert spec["epochs"] == get_settings().prompt_opt_epochs and spec["evidence"]

    # the worker's done.json → the row AND the candidate bundles become ledger rows
    bundle_id = spec["bundle"]["bundle_id"]
    executor.complete(job.job_id, [], summary={
        "optimize": {"status": "COMPLETED", "recommended_prompt": "Be very helpful.",
                     "explanation": "2 epochs", "meta": {"best_epoch": 2}},
        "bundle_refs": [f"{bundle_id}:1", f"{bundle_id}:2"],
        "bundles": {f"{bundle_id}:1": {"configuration": {"system_prompt": "Be helpful.",
                                                           "tools": {}}},
                    f"{bundle_id}:2": {"configuration": {"system_prompt": "Be very helpful.",
                                                           "tools": {}}}},
    })
    row = _rows(client, run_id)[0]
    assert row["status"] == "COMPLETED"
    assert row["result"]["recommended_prompt"] == "Be very helpful."
    assert row["result"]["provider"] == "harness_optimizer"
    db = SessionLocal()
    rows = db.query(ConfigBundle).filter_by(id=bundle_id).order_by(ConfigBundle.version).all()
    assert [r.version for r in rows] == [1, 2] and rows[1].agent_id == agent_id
    assert rows[1].s3_key == f"bundles/{agent_id}/{bundle_id}/v2.json"
    assert rows[1].components["configuration"]["system_prompt"] == "Be very helpful."
    db.close()


# ─── N-SCN-bug2: the contrastive reflection under the output budget ──────────
class _TightOptimizer(ScriptedOptimizer):
    """`step()` hits the model's output limit while its max_tokens is under ``fits_at``."""

    def __init__(self, formula, model, prompts, fits_at):
        super().__init__(formula, model, prompts)
        self.fits_at = fits_at
        self.budgets: list[int] = []

    def step(self):
        from strands.types.exceptions import MaxTokensReachedException

        budget = int(self.model.get_config()["max_tokens"])
        self.budgets.append(budget)
        if budget < self.fits_at:
            raise MaxTokensReachedException("Model stopped generating due to maximum token limit.")
        super().step()


def _run_optimize(fits_at: int, **spec):
    store, s3 = MemoryStore(), FakeS3()
    job = _job([{"scenario_id": "S1", "prompt": "q1"}], epochs=1, **spec)
    sink = JobSink(store, job)
    models: list[FakeModel] = []
    optimizers: list[_TightOptimizer] = []

    def judge_factory(model_id, **kw):
        models.append(FakeModel())
        models[-1].update_config(max_tokens=kw["max_tokens"])
        return models[-1]

    def optimizer_factory(formula, model):
        optimizers.append(_TightOptimizer(formula, model, ["v1 prompt"], fits_at))
        return optimizers[-1]

    jobs_optimize.run(job, sink, judge_factory=judge_factory,
                      invoke=lambda prompt, **kw: "answer", score=lambda *a: 0.5, s3_client=s3,
                      optimizer_factory=optimizer_factory)
    return store, models[0], optimizers[0]


def test_optimize_reflection_retries_with_a_doubled_budget_and_restores_the_model():
    store, model, optimizer = _run_optimize(fits_at=16384, max_tokens=4096)
    assert optimizer.budgets == [4096, 8192, 16384]
    assert model.get_config()["max_tokens"] == 4096  # raised for the call only
    done = JobDone.from_dict(json.loads(store.get(jobs.done_key(JOB_ID, 1)).decode()))
    assert done.state == "COMPLETED"
    assert done.summary["optimize"]["recommended_prompt"] == "v1 prompt"


def test_optimize_reflection_that_never_fits_fails_naming_the_budget():
    from app.services.strands_eval import model_calls

    with pytest.raises(model_calls.JudgeBudgetExceeded) as exc:
        _run_optimize(fits_at=10**9, max_tokens=4096, max_tokens_cap=8192)
    assert str(exc.value) == "optimizer reflection exceeded 8192 output tokens after 2 attempts"
