"""N-SCN-bug2 — the shared budget helper every judge / optimizer / summariser call runs
through (``services/strands_eval/model_calls``) and the hub's resolver
(``evaluation/judge_budget``)."""

import logging

import pytest
from strands.types.exceptions import MaxTokensReachedException

from app.core.config import Settings
from app.evaluation import judge_budget
from app.schemas.agent import (
    allowed_model_ids,
    default_judge_max_tokens,
    judge_budget_defaults,
    model_capabilities,
)
from app.services.strands_eval import model_calls

ALLOWED_MODEL_IDS = allowed_model_ids()
MODEL_CAPABILITIES = model_capabilities()

MAX_TOKENS = MaxTokensReachedException("Model stopped generating due to maximum token limit.")


def _flaky(fail_until: int):
    """``call(max_tokens)`` that hits the output limit until the budget reaches ``fail_until``."""
    seen: list[int] = []

    def call(max_tokens: int) -> str:
        seen.append(max_tokens)
        if max_tokens < fail_until:
            raise MAX_TOKENS
        return f"ok@{max_tokens}"

    call.seen = seen  # type: ignore[attr-defined]
    return call


# ─── call_with_budget ────────────────────────────────────────────────────────
def test_retries_with_the_budget_doubled_and_logs_budgets_only(caplog):
    call = _flaky(4096)
    with caplog.at_level(logging.WARNING, logger=model_calls.__name__):
        out = model_calls.call_with_budget(call, budget=1024, cap=16384, label="judge X",
                                           model_id="global.zai.glm-5.3")
    assert out == "ok@4096" and call.seen == [1024, 2048, 4096]
    messages = [r.getMessage() for r in caplog.records]
    assert "judge X (global.zai.glm-5.3) hit the output limit: budget 1024 → 2048 (attempt 2/3)" \
        in messages
    assert "budget 2048 → 4096 (attempt 3/3)" in messages[1]
    assert not any("ok@" in m for m in messages)  # never the output, never the prompt


def test_stops_after_two_retries_and_the_error_names_the_budget_tried():
    call = _flaky(10**9)
    with pytest.raises(model_calls.JudgeBudgetExceeded) as exc:
        model_calls.call_with_budget(call, budget=1024, cap=16384, label="judge")
    assert call.seen == [1024, 2048, 4096]
    assert str(exc.value) == "judge exceeded 4096 output tokens after 3 attempts"
    assert exc.value.budget == 4096 and exc.value.attempts == 3
    assert isinstance(exc.value.__cause__, MaxTokensReachedException)


def test_stops_at_the_cap_before_the_retry_count_is_used():
    call = _flaky(10**9)
    with pytest.raises(model_calls.JudgeBudgetExceeded, match="exceeded 4096 output tokens after "
                                                            "2 attempts"):
        model_calls.call_with_budget(call, budget=3000, cap=4096, label="judge")
    assert call.seen == [3000, 4096]  # 3000 → 4096 (capped), then no room to grow


def test_a_budget_already_at_the_cap_fails_on_the_first_attempt_naming_it():
    call = _flaky(10**9)
    with pytest.raises(model_calls.JudgeBudgetExceeded,
                       match="reflection exceeded 16384 output tokens after 1 attempt$"):
        model_calls.call_with_budget(call, budget=16384, cap=16384, label="reflection")
    assert call.seen == [16384]


def test_a_parseable_partial_is_accepted_instead_of_retried():
    call = _flaky(10**9)
    salvaged: list = []

    def salvage(exc):
        salvaged.append(type(exc).__name__)
        return {"score": 0.5}

    out = model_calls.call_with_budget(call, budget=1024, salvage=salvage)
    assert out == {"score": 0.5} and call.seen == [1024]
    assert salvaged == ["MaxTokensReachedException"]


def test_an_unusable_partial_falls_through_to_the_retry():
    call = _flaky(2048)
    out = model_calls.call_with_budget(call, budget=1024, salvage=lambda exc: None)
    assert out == "ok@2048" and call.seen == [1024, 2048]


def test_other_errors_propagate_untouched_without_a_retry():
    seen: list[int] = []

    def call(max_tokens):
        seen.append(max_tokens)
        raise ValueError("ThrottlingException")

    with pytest.raises(ValueError):
        model_calls.call_with_budget(call, budget=1024)
    assert seen == [1024]


def test_on_retry_reports_attempt_and_both_budgets():
    notes: list[tuple] = []
    model_calls.call_with_budget(_flaky(4096), budget=1024,
                                 on_retry=lambda *a: notes.append(a))
    assert notes == [(2, 1024, 2048), (3, 2048, 4096)]


def test_output_truncated_and_wrapped_messages_count_as_the_output_limit():
    assert model_calls.is_max_tokens_error(MAX_TOKENS)
    assert model_calls.is_max_tokens_error(model_calls.OutputTruncated(1024, "no JSON"))
    assert model_calls.is_max_tokens_error(
        RuntimeError("MaxTokensReachedException: Model stopped generating due to maximum "
                     "token limit."))
    assert not model_calls.is_max_tokens_error(RuntimeError("ThrottlingException"))


# ─── model_budget / model_max_tokens ─────────────────────────────────────────
class _Model:
    def __init__(self, **config):
        self.config = config
        self.updates: list[dict] = []

    def get_config(self):
        return dict(self.config)

    def update_config(self, **kw):
        self.updates.append(kw)
        self.config.update(kw)


def test_model_budget_raises_the_models_max_tokens_for_the_call_and_restores_it():
    model = _Model(model_id="m", max_tokens=1024)
    assert model_calls.model_max_tokens(model) == 1024
    with model_calls.model_budget(model, 2048):
        assert model.config["max_tokens"] == 2048
    assert model.config["max_tokens"] == 1024
    with model_calls.model_budget(model, 1024):  # already there: no update at all
        pass
    assert model.updates == [{"max_tokens": 2048}, {"max_tokens": 1024}]
    assert model_calls.model_max_tokens(object()) is None


# ─── resolve_budget / the catalog table / the hub resolver ───────────────────
def test_every_catalog_model_declares_its_default_judge_budget():
    for model_id in ALLOWED_MODEL_IDS:
        assert default_judge_max_tokens(model_id), model_id
        assert 256 <= MODEL_CAPABILITIES[model_id]["default_judge_max_tokens"] <= 16384
    defaults = judge_budget_defaults()
    assert defaults["global.zai.glm-5.3"] == 4096 and defaults["global.moonshotai.kimi-k3"] == 2048
    assert set(defaults) == set(ALLOWED_MODEL_IDS)
    # N-R6: an OpenAI-compatible row starts at the GLM-like 4096
    assert all(defaults[m] == 4096 for m in ALLOWED_MODEL_IDS if m.startswith("siliconflow."))
    assert default_judge_max_tokens("custom.byoc-model") is None


@pytest.mark.parametrize("model_id, expected", [
    ("global.zai.glm-5.3", 4096),       # the thinking model: 4× the historical default
    ("global.moonshotai.kimi-k3", 2048),
    ("custom.byoc-model", 1024),        # unknown → the setting, unchanged
    (None, 4096),                       # the default judge is GLM
])
def test_resolve_budget_is_model_aware_with_the_setting_as_the_floor(model_id, expected):
    defaults = judge_budget_defaults()
    assert model_calls.resolve_budget(model_id if model_id else "global.zai.glm-5.3",
                                      floor=1024, defaults=defaults) == expected
    # the setting is a floor: raising it above the catalog default wins …
    assert model_calls.resolve_budget("global.moonshotai.kimi-k3", floor=3000,
                                      defaults=defaults) == 3000
    # … but never above the cap, and a documented hard limit lowers the cap
    assert model_calls.resolve_budget("global.zai.glm-5.3", floor=1024, defaults=defaults,
                                      cap=2048) == 2048
    assert model_calls.resolve_budget("global.zai.glm-5.3", floor=1024, defaults=defaults,
                                      hard_limits={"global.zai.glm-5.3": 3000}) == 3000
    assert model_calls.next_budget(16384, 16384) is None
    assert model_calls.next_budget(3000, 4096) == 4096


def test_the_hub_resolver_reads_the_settings_and_the_table():
    s = Settings(eval_judge_max_tokens=1024, eval_judge_max_tokens_cap=16384)
    assert s.eval_judge_max_tokens_cap == 16384  # the default
    assert judge_budget.judge_budget(None, s) == 4096  # default judge = GLM 5.3
    assert judge_budget.judge_budget("global.moonshotai.kimi-k3", s) == 2048
    assert judge_budget.judge_budget("custom.byoc-model", s) == 1024
    assert judge_budget.judge_budget_cap("global.zai.glm-5.3", s) == 16384
    lifted = Settings(eval_judge_max_tokens=8192, eval_judge_max_tokens_cap=8192)
    assert judge_budget.judge_budget("global.zai.glm-5.3", lifted) == 8192
    with pytest.raises(ValueError):
        Settings(eval_judge_max_tokens_cap=100)  # under the GLM reasoning floor


# ─── N-R6: an OpenAIModel keeps max_tokens under `params` ───────────────────────────


class _ParamsModel:
    def __init__(self, max_tokens):
        self.config = {"model_id": "Pro/x", "params": {"max_tokens": max_tokens,
                                                      "temperature": 0.0}}

    def get_config(self):
        return self.config

    def update_config(self, **kw):
        self.config.update(kw)


def test_model_budget_understands_params_style_configs():
    from app.services.strands_eval.model_calls import model_budget, model_max_tokens

    model = _ParamsModel(1024)
    assert model_max_tokens(model) == 1024
    with model_budget(model, 4096):
        assert model.config["params"] == {"max_tokens": 4096, "temperature": 0.0}
        assert "max_tokens" not in model.config  # never a stray top-level key
    assert model.config["params"]["max_tokens"] == 1024
