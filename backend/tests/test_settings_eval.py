"""N12 E0-4 — evaluation / optimization settings (plan §C.2 E0-4, CA-8 tightening).

Every judge / actor / optimizer model id must be in the CN catalog (spec C3); the
executor defaults to the AgentCore Runtime worker and `inprocess` is a hermetic-suite /
`make dev` / SCORE NOW affordance, never a silent production flag (`select_executor`
refuses it in prod — tests/evaluation/test_executor_selection.py).
"""

import pytest

from app.core.config import Settings
from app.schemas.agent import DEFAULT_MODEL_ID, MIN_MODEL_MAX_TOKENS, allowed_model_ids


def test_evaluation_defaults(monkeypatch):
    monkeypatch.delenv("LAUNCHPAD_ONLINE_EVAL_ENABLED", raising=False)  # conftest pins 0
    s = Settings()
    assert s.eval_executor == "runtime"
    assert s.eval_judge_model_id == DEFAULT_MODEL_ID
    assert s.eval_judge_max_tokens == 1024 >= MIN_MODEL_MAX_TOKENS
    assert s.eval_judge_concurrency == 4
    assert s.eval_actor_model_id == DEFAULT_MODEL_ID
    assert s.eval_max_concurrent_runs == 3
    assert s.eval_batch_wait_s == 7200
    assert s.eval_job_budget_s == 25200
    assert s.eval_job_max_sessions == 150
    assert s.eval_job_stale_s == 600
    assert s.eval_results_log_group_prefix == ""
    assert s.eval_code_evaluators_enabled is False
    assert s.online_eval_enabled is True
    assert s.online_eval_tick_s == 60
    assert s.online_eval_batch == 20
    assert s.prompt_opt_default_model_id == DEFAULT_MODEL_ID
    assert s.prompt_opt_models == list(allowed_model_ids()[:2])  # the Bedrock rows (N-R6)
    assert s.prompt_opt_harness_optimizer_enabled is False
    assert s.prompt_opt_epochs == 2


@pytest.mark.parametrize("field", [
    "eval_judge_model_id", "eval_actor_model_id", "prompt_opt_default_model_id",
])
def test_model_ids_must_be_in_the_cn_catalog(field):
    with pytest.raises(ValueError, match="model.not_allowed|catalog"):
        Settings(**{field: "global.anthropic.claude-sonnet-5"})
    assert getattr(Settings(**{field: "global.moonshotai.kimi-k3"}), field) == (
        "global.moonshotai.kimi-k3"
    )


def test_prompt_opt_models_list_is_catalog_only():
    with pytest.raises(ValueError):
        Settings(prompt_opt_models=["global.zai.glm-5.3", "us.openai.gpt-5.6-sol"])
    with pytest.raises(ValueError):
        Settings(prompt_opt_models=[])


def test_executor_accepts_only_the_two_known_values():
    assert Settings(eval_executor="inprocess").eval_executor == "inprocess"
    with pytest.raises(ValueError):
        Settings(eval_executor="lambda")


@pytest.mark.parametrize("field,value", [
    ("eval_judge_max_tokens", MIN_MODEL_MAX_TOKENS - 1),  # C3: maxTokens >= 256
    ("eval_judge_concurrency", 0),
    ("eval_max_concurrent_runs", 21),  # one Runtime session per job; keep polling bounded
    ("eval_batch_wait_s", 28801),  # the worker's 8 h async budget
    ("eval_job_budget_s", 28801),
    ("online_eval_tick_s", 9),
    ("online_eval_batch", 0),
    ("prompt_opt_epochs", 0),
])
def test_bounds(field, value):
    with pytest.raises(ValueError):
        Settings(**{field: value})


def test_env_overrides(monkeypatch):
    monkeypatch.setenv("LAUNCHPAD_EVAL_EXECUTOR", "inprocess")
    monkeypatch.setenv("LAUNCHPAD_ONLINE_EVAL_ENABLED", "0")
    monkeypatch.setenv("LAUNCHPAD_EVAL_JUDGE_MODEL_ID", "global.moonshotai.kimi-k3")
    s = Settings()
    assert s.eval_executor == "inprocess"
    assert s.online_eval_enabled is False
    assert s.eval_judge_model_id == "global.moonshotai.kimi-k3"


# ── r8-nits item 4: `eval_max_concurrent_runs` was declared twice (le=20, then le=5) ──


def test_eval_max_concurrent_runs_is_bounded_at_twenty():
    """config.py carried two definitions of the field; pydantic keeps the LAST, so the
    `le=5` quota line from the fork baseline shadowed the intended `le=20` (the CN worker
    model has no account batch quota — evaluation-engine.md). One definition, bound 20."""
    field = Settings.model_fields["eval_max_concurrent_runs"]
    bounds = {type(m).__name__: getattr(m, "le", None) for m in field.metadata}
    assert field.default == 3
    assert bounds.get("Le") == 20, field.metadata
    assert Settings(eval_max_concurrent_runs=20).eval_max_concurrent_runs == 20
    with pytest.raises(ValueError):
        Settings(eval_max_concurrent_runs=21)
    with pytest.raises(ValueError):
        Settings(eval_max_concurrent_runs=0)


def test_no_settings_field_is_declared_twice():
    """Generic guard: a field name assigned twice in the `Settings` class body is a
    silent override (the later wins), never what the author meant."""
    import ast
    import collections
    import inspect

    import app.core.config as config_mod

    tree = ast.parse(inspect.getsource(config_mod))
    settings_cls = next(
        node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "Settings"
    )
    names = collections.Counter()
    for node in settings_cls.body:
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names[node.target.id] += 1
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names[target.id] += 1
    duplicates = sorted(name for name, count in names.items() if count > 1)
    assert duplicates == [], f"Settings fields declared more than once: {duplicates}"
    assert names["eval_max_concurrent_runs"] == 1
