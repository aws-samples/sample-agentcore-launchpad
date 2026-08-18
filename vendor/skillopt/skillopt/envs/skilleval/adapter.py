"""SkillEval environment adapter — trains skills against rubric-judged tasks.

Registers the skilleval task format with the ReflACT trainer so a task set
built for evaluation (``scripts/evaluate_skill.py``) can drive skill
optimization unchanged: rollout runs the tasks through the exec target
(Claude Code / Codex), the LLM judge converts each task's rubric into
``hard``/``soft`` scores, and reflection sees the rubric as hidden reference
material alongside the agent's answer and the judge's verdict.
"""
from __future__ import annotations

import json
import os
import shlex

from skillopt.datasets.base import BatchSpec
from skillopt.envs.base import EnvAdapter
from skillopt.envs.skilleval.agentic_judge import preflight_agentic_judge
from skillopt.envs.skilleval.bundle import SKILL_MD, normalize_rel_path, split_bundle
from skillopt.envs.skilleval.contracts import JUDGE_MODES
from skillopt.envs.skilleval.dataloader import SkillEvalDataLoader
from skillopt.envs.skilleval.evaluator import AgenticJudgeConfig, evaluate_rollouts, judge
from skillopt.envs.skilleval.plugin import PluginState, plugin_hash
from skillopt.envs.skilleval.rollout import collect_support_files, run_batch
from skillopt.model import azure_openai as _llm
from skillopt.utils.scoring import skill_hash


class SkillEvalAdapter(EnvAdapter):
    """ReflACT adapter for user task sets with per-task rubrics."""

    def __init__(
        self,
        split_dir: str = "",
        data_path: str = "",
        split_mode: str = "split_dir",
        split_ratio: str = "4:3:3",
        split_seed: int = 42,
        split_output_dir: str = "",
        workers: int = 3,
        timeout: int = 900,
        analyst_workers: int = 4,
        failure_only: bool = False,
        minibatch_size: int = 4,
        edit_budget: int = 4,
        seed: int = 42,
        limit: int = 0,
        skill_dir: str = "",
        trainable_files: list[str] | str | None = None,
        judge_mode: str = "auto",
        judge_backend: str = "claude_code_exec",
        judge_model: str = "",
        judge_timeout: int = 300,
        judge_effort: str = "low",
        judge_cache: bool = True,
        judge_sandbox_command: tuple[str, ...] | list[str] | str = "bwrap",
        judge_max_evidence_bytes: int = 536_870_912,
        judge_max_scratch_bytes: int = 1_073_741_824,
        judge_max_render_pixels: int = 500_000_000,
    ) -> None:
        # For a multi-file skill only the trainable state evolves; supporting
        # files (scripts/, references/, ...) are frozen and copied into every
        # rollout workspace. By default the state is SKILL.md alone; with
        # *trainable_files* it is a bundle (see bundle.py) of SKILL.md plus
        # those files, and the frozen set excludes them. Everything is
        # collected/validated eagerly so a bad path fails before model calls.
        self.trainable_files = self._normalize_trainable(trainable_files)
        if self.trainable_files and not skill_dir:
            raise ValueError("trainable_files requires skill_dir")
        support = collect_support_files(skill_dir) if skill_dir else []
        self._seed_docs: dict[str, str] = {}
        self._seed_skill_md = ""
        if self.trainable_files:
            trainable = set(self.trainable_files)
            support = [(src, rel) for src, rel in support
                       if normalize_rel_path(rel) not in trainable]
            for rel in self.trainable_files:
                self._seed_docs[rel] = self._read_seed(skill_dir, rel)
            self._seed_skill_md = self._read_seed(skill_dir, SKILL_MD)
        self.skill_files = support or None
        self.workers = workers
        self.timeout = int(timeout)
        self.analyst_workers = analyst_workers
        self.failure_only = failure_only
        self.minibatch_size = minibatch_size
        self.edit_budget = edit_budget
        # The agentic judge's mode is validated here -- AgenticJudgeConfig
        # itself deliberately leaves it unvalidated/unused; evaluator's
        # should_use_agentic and this adapter are its intended consumers.
        if judge_mode not in JUDGE_MODES:
            raise ValueError(f"judge_mode must be one of {sorted(JUDGE_MODES)}: {judge_mode!r}")
        # A bare string (e.g. from YAML ``judge_sandbox_command: bwrap`` or
        # ``sudo -n bwrap``) is shlex-split once into a trusted argv vector;
        # AgenticJudgeConfig then rejects an empty vector and never passes it
        # through a shell. An already-structured list/tuple is kept as-is.
        sandbox_command = (
            shlex.split(judge_sandbox_command)
            if isinstance(judge_sandbox_command, str)
            else judge_sandbox_command
        )
        self.judge_config = AgenticJudgeConfig(
            mode=judge_mode,
            backend=judge_backend,
            model=judge_model,
            timeout=judge_timeout,
            effort=judge_effort,
            cache=judge_cache,
            sandbox_command=sandbox_command,
            max_evidence_bytes=judge_max_evidence_bytes,
            max_scratch_bytes=judge_max_scratch_bytes,
            max_render_pixels=judge_max_render_pixels,
        )
        self.dataloader = SkillEvalDataLoader(
            split_dir=split_dir,
            data_path=data_path,
            split_mode=split_mode,
            split_ratio=split_ratio,
            split_seed=split_seed,
            split_output_dir=split_output_dir,
            seed=seed,
            limit=limit,
        )

    # ── Lifecycle ─────────────────────────────────────────────────────────

    def setup(self, cfg: dict) -> None:
        super().setup(cfg)
        self.dataloader.setup(cfg)
        # Explicit agentic mode validates the whole judge environment eagerly
        # (backend executable, sandbox usability, MCP init, format tools) so a
        # misconfiguration fails here, before any rollout/model spend. Auto
        # mode stays lazy: it validates when the first supported binary task
        # is encountered (run_agentic_judge's per-task probe).
        if self.judge_config.mode == "agentic":
            preflight_agentic_judge(
                self.judge_config,
                self.dataloader.train_items
                + self.dataloader.val_items
                + self.dataloader.test_items,
            )

    def get_dataloader(self):
        return self.dataloader

    # ── Env construction ──────────────────────────────────────────────────

    def build_env_from_batch(self, batch: BatchSpec, **kwargs):
        return list(batch.payload or [])

    def build_train_env(self, batch_size: int, seed: int, **kwargs):
        batch = self.dataloader.build_train_batch(batch_size=batch_size, seed=seed, **kwargs)
        return self.build_env_from_batch(batch, **kwargs)

    def build_eval_env(self, env_num: int, split: str, seed: int, **kwargs):
        batch = self.dataloader.build_eval_batch(env_num=env_num, split=split, seed=seed, **kwargs)
        return self.build_env_from_batch(batch, **kwargs)

    # ── Rollout (scoring lives here; judge provides hard/soft) ───────────

    def rollout(self, env_manager, skill_content: str, out_dir: str, **kwargs) -> list[dict]:
        items: list[dict] = env_manager
        skill_md, skill_docs = self._split_state(skill_content)
        rollout_results = run_batch(
            items,
            skill_md,
            out_dir,
            workers=self.workers,
            timeout=self.timeout,
            model=_llm.TARGET_DEPLOYMENT,
            skill_files=self.skill_files,
            skill_docs=skill_docs,
        )
        results = evaluate_rollouts(
            items,
            rollout_results,
            state_hash=skill_hash(skill_content),
            out_root=out_dir,
            judge_config=self.judge_config,
            chat_judge=judge,
        )
        self._persist_trajectories(items, results, out_dir)
        return results

    def rollout_plugin(
        self,
        env_manager,
        plugin_state: PluginState,
        out_dir: str,
        **kwargs,
    ) -> list[dict]:
        """Run and judge tasks with every named Plugin Skill installed."""
        del kwargs
        items: list[dict] = env_manager
        runtime_skills = plugin_state.runtime_skills()
        rollout_results = run_batch(
            items,
            runtime_skills[0]["content"],
            out_dir,
            workers=self.workers,
            timeout=self.timeout,
            model=_llm.TARGET_DEPLOYMENT,
            runtime_skills=runtime_skills,
        )
        results = evaluate_rollouts(
            items,
            rollout_results,
            state_hash=plugin_hash(plugin_state),
            out_root=out_dir,
            judge_config=self.judge_config,
            chat_judge=judge,
        )
        for item, result in zip(items, results):
            result["target_skills"] = list(item.get("target_skills") or [])
            result["task_type"] = item.get("task_type", "default")
        self._persist_trajectories(items, results, out_dir)
        return results

    # ── Multi-doc bundle state ────────────────────────────────────────────

    @staticmethod
    def _normalize_trainable(trainable_files: list[str] | str | None) -> list[str]:
        if not trainable_files:
            return []
        raw = (trainable_files.split(",") if isinstance(trainable_files, str)
               else list(trainable_files))
        rels = []
        for entry in raw:
            rel = normalize_rel_path(entry)
            if rel == SKILL_MD:
                raise ValueError("SKILL.md is always trainable; do not list it in trainable_files")
            if rel not in rels:
                rels.append(rel)
        return rels

    @staticmethod
    def _read_seed(skill_dir: str, rel: str) -> str:
        path = os.path.join(skill_dir, rel)
        if not os.path.isfile(path):
            raise ValueError(f"trainable file not found in skill_dir: {path}")
        with open(path, encoding="utf-8") as f:
            return f.read()

    def _split_state(self, skill_content: str) -> tuple[str, dict[str, str] | None]:
        """Resolve the trainer's state string into (SKILL.md, trainable docs).

        Without trainable_files the state IS SKILL.md. With them it is a
        bundle; a section the optimizer mangled or dropped falls back to the
        seed copy — the rollout always sees a complete file set, and the gate
        judges whatever the candidate actually produced.
        """
        if not self.trainable_files:
            return skill_content, None
        docs = split_bundle(skill_content, allowed=self.trainable_files)
        skill_md = docs.get(SKILL_MD) or self._seed_skill_md
        skill_docs = {rel: docs.get(rel, self._seed_docs[rel])
                      for rel in self.trainable_files}
        return skill_md, skill_docs

    def _persist_trajectories(self, items: list[dict], results: list[dict], out_dir: str) -> None:
        """Write predictions/<id>/conversation.json + enrich results for reflection.

        ``fmt_minibatch_trajectories`` silently skips any result without a
        conversation.json, so this is what makes reflection see skilleval
        trajectories at all.
        """
        for item, result in zip(items, results):
            task_id = str(result["id"])
            criteria = result.get("judge_criteria") or []
            coverage = result.get("judge_coverage") or {}
            verdict_note = (
                f"Judge status: {result.get('judge_status', 'unknown')}\n"
                f"Judge verdict: hard={result.get('hard')} soft={result.get('soft')}\n"
                f"Judge reason: {result.get('judge_reason', '')}\n"
                f"Criteria: {json.dumps(criteria, ensure_ascii=False)}\n"
                f"Coverage: {json.dumps(coverage, ensure_ascii=False)}"
            )
            if result.get("error"):
                verdict_note += f"\nRollout error: {result['error']}"
            conversation = [
                {"role": "user", "content": item["question"]},
                {"role": "assistant", "content": result.get("response", "")},
                {"role": "system", "content": verdict_note},
            ]
            pred_dir = os.path.join(out_dir, "predictions", task_id)
            os.makedirs(pred_dir, exist_ok=True)
            with open(os.path.join(pred_dir, "conversation.json"), "w", encoding="utf-8") as f:
                json.dump(conversation, f, ensure_ascii=False, indent=2)

            result["task_description"] = item["question"]
            if not result.get("hard"):
                result["fail_reason"] = result.get("judge_reason") or result.get("error", "")
            result["n_turns"] = 1

    # ── Reflection support ────────────────────────────────────────────────

    def build_reference_text(self, item: dict) -> str:
        """Expose the rubric to the optimizer as hidden reference material."""
        rubric = str(item.get("rubric") or "").strip()
        if not rubric:
            return ""
        return f"Acceptance rubric (not shown to the agent):\n{rubric}"

    def get_task_types(self) -> list[str]:
        seen: list[str] = []
        for item in (
            self.dataloader.train_items
            + self.dataloader.val_items
            + self.dataloader.test_items
        ):
            task_type = str(item.get("task_type") or "default")
            if task_type not in seen:
                seen.append(task_type)
        return seen or ["default"]
