"""Worker-side handler for ``optimize`` jobs — Strands harness-optimizer (N12 E2-5; plan §B.5).

Contrastive reflection on **real rollouts**: every epoch the candidate system prompt is
written as a temporary config-bundle version (``bundles/<agent>/opt_<job>/v<n>.json`` —
the runtime's bundle reader picks it up through the pointer, CA-2), the dataset prompts are
replayed against the target runtime with that pointer, each rollout is scored with the
chosen evaluator, and ``ContrastiveReflectionOptimizer`` rewrites the prompt from the
contrast between high- and low-reward traces. The loop mirrors ``Trainer.fit`` step by step
(generate → reward → add → step → zero) so the cancel marker and ``status.json`` progress
can be honoured between epochs.

Pure library code: the model (``judge_factory`` → strands ``Model``; lead ruling — never
the optimizer's own default ``BedrockModel``), the target invocation (``invoke``), the
scorer (``score``) and the S3 client are injected by the dispatcher. Risk R-4: the
reflection agent carries ``strands_tools.shell`` to read the trace files it writes — the
provider is therefore an operator opt-in (``prompt_opt_harness_optimizer_enabled``).
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Callable, Iterable, Iterator
from typing import Any

from strands_harness_optimizer.datamodels import Reward, Rollout
from strands_harness_optimizer.formulas import SystemPromptFormula
from strands_harness_optimizer.optimizers.system_prompt import ContrastiveReflectionOptimizer
from strands_harness_optimizer.rewards.reward_function import RewardFunction
from strands_harness_optimizer.rollout_engines import AgentRolloutEngine
from strands_harness_optimizer.utils.templates import load_builtin_template

from app.services.strands_eval import model_calls

PROVIDER_ID = "harness_optimizer"
JOB_TYPE = "optimize"
TEMPLATE_SYSTEM = "contrastive_reflection/system_prompt.jinja"
TEMPLATE_TASK = "contrastive_reflection/task_message_system_prompt.jinja"
BAGGAGE_KEY = "launchpad.bundle"

# invoke(prompt, *, session_id, bundle_ref, baggage, launchpad_meta) -> agent reply text
InvokeFn = Callable[..., str]
# score(session_id, prompt, output) -> float | None  (the evaluator's score after the wait)
ScoreFn = Callable[..., float | None]


def bundle_components(system_prompt: str) -> dict[str, Any]:
    """The bundle body the runtime reader consumes (``components.configuration``)."""
    return {"configuration": {"system_prompt": str(system_prompt), "tools": {}}}


class BundleWriter:
    """Immutable candidate versions ``bundles/<agent>/<bundle_id>/v<n>.json`` on S3."""

    def __init__(self, s3_client: Any, *, bucket: str, agent_id: str, bundle_id: str) -> None:
        self._s3 = s3_client
        self.bucket = bucket
        self.agent_id = agent_id
        self.bundle_id = bundle_id
        self.version = 0
        self.refs: list[str] = []
        self.bundles: dict[str, dict[str, Any]] = {}

    def write(self, system_prompt: str) -> str:
        self.version += 1
        ref = f"{self.bundle_id}:{self.version}"
        key = f"bundles/{self.agent_id}/{self.bundle_id}/v{self.version}.json"
        components = bundle_components(system_prompt)
        body = json.dumps({
            "bundle_id": self.bundle_id, "version": self.version, "agent_id": self.agent_id,
            "name": self.bundle_id, "components": components,
        }, ensure_ascii=False).encode("utf-8")
        self._s3.put_object(Bucket=self.bucket, Key=key, Body=body,
                            ContentType="application/json")
        self.refs.append(ref)
        self.bundles[ref] = components
        return ref


class LaunchpadRolloutEngine(AgentRolloutEngine):
    """Replays the dataset prompts against the target runtime with the candidate prompt as
    a bundle pointer — rollouts hit the REAL agent, not a local stand-in."""

    def __init__(self, formula: SystemPromptFormula, *, invoke: InvokeFn, writer: BundleWriter,
                 session_prefix: str) -> None:
        super().__init__(formula, num_rollouts=1)
        self._invoke = invoke
        self.writer = writer
        self._session_prefix = session_prefix

    def generate(self, data_sample: dict[str, Any]) -> Rollout:
        return next(iter(self.generate_batch([data_sample])))

    def generate_batch(self, data_samples: Iterable[dict[str, Any]]) -> Iterator[Rollout]:
        candidate = str(self.formula.get_tunable_params().get("system_prompt") or "")
        ref = self.writer.write(candidate)
        for sample in data_samples:
            prompt = str(sample.get("prompt") or sample.get("input") or "")
            session_id = f"{self._session_prefix}-{uuid.uuid4().hex}"
            output = self._invoke(
                prompt, session_id=session_id, bundle_ref=ref,
                baggage=f"{BAGGAGE_KEY}={ref}", launchpad_meta={"bundle": ref},
            )
            yield Rollout(
                data_sample=dict(sample),
                messages=[{"role": "user", "content": prompt},
                          {"role": "assistant", "content": str(output or "")}],
                metadata={"session_id": session_id, "bundle_ref": ref,
                          "system_prompt": candidate},
            )


class LaunchpadReward(RewardFunction):
    """The chosen evaluator's score for the rollout's session (None → 0.0, recorded)."""

    def __init__(self, score: ScoreFn) -> None:
        self._score = score

    def __call__(self, rollout: Rollout) -> Reward:
        session_id = str(rollout.metadata.get("session_id") or "")
        prompt = str(rollout.messages[0].get("content") or "") if rollout.messages else ""
        output = str(rollout.messages[-1].get("content") or "") if rollout.messages else ""
        try:
            value = self._score(session_id, prompt, output)
        except Exception as exc:  # noqa: BLE001 — one unscorable rollout is a 0, not a crash
            return Reward(reward=0.0, metadata={"session_id": session_id,
                                                "error": f"{type(exc).__name__}: {exc}"[:300]})
        return Reward(reward=float(value) if value is not None else 0.0,
                      metadata={"session_id": session_id, "scored": value is not None})


class LaunchpadReflectionOptimizer(ContrastiveReflectionOptimizer):
    """The library optimizer with the reflection model injected (lead ruling: the model is
    the one ``global_bedrock_model`` / the worker's bearer factory built — the parent's
    default ``BedrockModel`` is constructed but never invoked)."""

    def __init__(self, formula: SystemPromptFormula, model: Any, **kwargs: Any) -> None:
        super().__init__(formula, load_builtin_template(TEMPLATE_SYSTEM),
                         load_builtin_template(TEMPLATE_TASK), **kwargs)
        self._launchpad_model = model

    def _create_agent(self, system_prompt: str):  # type: ignore[override]
        agent = super()._create_agent(system_prompt)
        agent.model = self._launchpad_model
        return agent


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def run(
    job: Any,  # evaluation.jobs.JobSpec — duck-typed: the library imports nothing from app.*
    sink: Any,
    *,
    judge_factory: Callable[..., Any],
    invoke: InvokeFn,
    score: ScoreFn,
    s3_client: Any,
    optimizer_factory: Callable[..., Any] | None = None,
) -> None:
    """``optimize`` job: N epochs of rollout → reward → contrastive reflection; the final
    prompt and every candidate bundle version travel in ``done.json.summary``."""
    spec = job.spec
    model_id = str(spec.get("model_id") or "")
    current = str(spec.get("current_prompt") or "")
    samples = [dict(s) for s in spec.get("dataset_prompts") or []]
    epochs = max(1, int(spec.get("epochs") or 1))
    bundle = dict(spec.get("bundle") or {})
    target = dict(spec.get("target") or {})
    if not samples:
        sink.finish("FAILED", error="the run's dataset has no prompts to replay — the "
                                    "optimizer needs scenarios to roll out")
        return
    writer = BundleWriter(
        s3_client, bucket=str(bundle.get("bucket") or job.sink.get("bucket") or ""),
        agent_id=str(bundle.get("agent_id") or target.get("agent_id") or ""),
        bundle_id=str(bundle.get("bundle_id") or f"opt_{job.job_id[-12:]}"),
    )
    formula = SystemPromptFormula(system_prompt=current)
    engine = LaunchpadRolloutEngine(formula, invoke=invoke, writer=writer,
                                    session_prefix=f"opt-{job.job_id[-8:]}")
    reward_fn = LaunchpadReward(score)
    max_tokens = int(spec.get("max_tokens") or 4096)
    # CN additive (N-SCN-bug2): the reflection's budget doubles on the output limit up to here
    max_tokens_cap = max(int(spec.get("max_tokens_cap") or model_calls.DEFAULT_BUDGET_CAP),
                         max_tokens)
    model = judge_factory(model_id, max_tokens=max_tokens, temperature=1.0)
    optimizer = (optimizer_factory(formula, model) if optimizer_factory else
                 LaunchpadReflectionOptimizer(formula, model, model_config={
                     "model_id": model_id, "max_tokens": max_tokens, "temperature": 1.0}))
    history: list[dict[str, Any]] = []
    started = time.monotonic()
    for epoch in range(1, epochs + 1):
        if sink.cancelled():
            sink.finish("STOPPED", summary={"provider": PROVIDER_ID, "epochs": history,
                                            "bundle_refs": writer.refs})
            return
        sink.status("invoking", done=epoch - 1, total=epochs,
                    message=f"epoch {epoch}/{epochs}: replaying {len(samples)} prompts")
        rollouts = list(engine.generate_batch(samples))
        sink.status("evaluating", done=epoch - 1, total=epochs,
                    message=f"epoch {epoch}/{epochs}: scoring {len(rollouts)} rollouts")
        rewards = [reward_fn(r) for r in rollouts]
        for rollout, reward in zip(rollouts, rewards, strict=True):
            sink.invocation({
                "session_id": rollout.metadata["session_id"],
                "scenario_id": rollout.data_sample.get("scenario_id"),
                "arm": "T1", "status": "ok", "bundle_ref": rollout.metadata["bundle_ref"],
                "reward": reward.reward,
            })
        history.append({
            "epoch": epoch, "avg_reward": round(_mean([r.reward for r in rewards]), 4),
            "bundle_ref": rollouts[0].metadata["bundle_ref"],
            "system_prompt": str(formula.get_tunable_params().get("system_prompt") or ""),
        })
        optimizer.add_rollouts(rollouts)
        optimizer.add_rewards(rewards)

        def reflect(limit: int) -> None:
            with model_calls.model_budget(model, limit):
                optimizer.step()

        def on_retry(attempt: int, _before: int, after: int, _epoch: int = epoch) -> None:
            sink.status("evaluating", done=_epoch - 1, total=epochs,
                        message=(f"epoch {_epoch}/{epochs}: reflection hit the token limit — "
                                 f"retrying with {after} tokens (attempt {attempt}/"
                                 f"{model_calls.MAX_BUDGET_RETRIES + 1})"))

        # N-SCN-bug2: the contrastive reflection is one model call over the same rollouts —
        # re-running `step()` with a bigger budget is safe (zero() only comes after)
        model_calls.call_with_budget(reflect, budget=max_tokens, cap=max_tokens_cap,
                                     label="optimizer reflection", model_id=model_id,
                                     on_retry=on_retry)
        optimizer.zero()
    final_prompt = str(formula.get_tunable_params().get("system_prompt") or "").strip()
    final_ref = (history[-1]["bundle_ref"] if final_prompt == history[-1]["system_prompt"]
                 else writer.write(final_prompt))
    best = max(history, key=lambda h: h["avg_reward"])
    result: dict[str, Any] = {
        "status": "COMPLETED" if final_prompt and final_prompt != current else "FAILED",
        "recommended_prompt": final_prompt if final_prompt != current else None,
        "explanation": (f"harness-optimizer contrastive reflection over {epochs} epoch(s) × "
                        f"{len(samples)} rollouts; best average reward {best['avg_reward']} "
                        f"(epoch {best['epoch']})"),
        "error": None if final_prompt and final_prompt != current else
                 "the optimizer left the prompt unchanged",
        "meta": {"epochs": history, "bundle_refs": list(writer.refs),
                 "final_bundle_ref": final_ref, "best_epoch": best["epoch"],
                 "latency_ms": int((time.monotonic() - started) * 1000),
                 "rollouts": epochs * len(samples)},
        "tool_descriptions": None, "tool_status": None, "tool_error": None,
        "tool_explanation": "",
    }
    sink.artifact("optimize.json", result)
    sink.finish("COMPLETED", summary={"provider": PROVIDER_ID, "model_id": model_id,
                                      "optimize": result, "bundle_refs": list(writer.refs),
                                      "bundles": dict(writer.bundles)})


def make_runner(**dependencies: Any) -> Callable[[Any, Any], None]:
    def runner(job: Any, sink: Any) -> None:
        run(job, sink, **dependencies)

    return runner
