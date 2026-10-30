"""Proposal validation reports EVERY failing stage at once, in operator-readable
sentences (N-ARCH-obs2 / obs3), on the exact shapes the 2026-10-12 architect e2e
observed: a foreign model behind an 11-evaluator plan, a `recommendation_keys` index
with no prose entry, and the pydantic dump the member used to see."""

import pytest
from pydantic import ValidationError

from app.assistant import proposal as contract
from app.schemas import agent as schema
from tests.test_assistant import VALID_PROPOSAL, _catalog

FOREIGN = "anthropic.claude-sonnet-4-5"


def _existing(n: int) -> list[dict]:
    return [{"kind": "existing", "key": f"e{i}", "title": f"E{i}",
             "evaluator_id": "Builtin.Helpfulness", "golden_test_ids": []} for i in range(n)]


def _no_dump(errors: list[str]) -> None:
    joined = "\n".join(errors)
    for leak in ("input_value", "input_type", "errors.pydantic.dev", "Value error,",
                 "validation error for"):
        assert leak not in joined, leak


def _selectable() -> list[str]:
    """The ids the member can pick here (N-ARCH-obs9): catalog minus unconfigured providers."""
    return [m for m in schema.allowed_model_ids() if schema.model_available(m)]


def test_run_c_shape_reports_the_model_and_the_plan_together():
    """4b run C: model outside the catalog AND 11 evaluators — before, only the plan
    error was stored and the model error cost a second round-trip."""
    bad = {**VALID_PROPOSAL, "model_id": FOREIGN,
           "evaluation_plan": {"evaluators": _existing(11), "scenarios": [],
                               "recommendation_keys": {"8": ["e1"]},
                               "blocked_golden_tests": []}}
    content, display, errors = contract.validate(bad, _catalog())
    assert content is None and display["model_id"] == FOREIGN  # still shown / editable
    assert any("11 evaluators" in e and "at most 10" in e for e in errors)
    assert any(e.startswith("evaluation_plan.recommendation_keys: index 8 has no "
                            "evaluator_recommendations entry") for e in errors)
    model = [e for e in errors if e.startswith("model_id:")]
    assert model == [f"model_id: '{FOREIGN}' is not available in this build — choose one of: "
                     + ", ".join(f"`{m}`" for m in _selectable())]
    assert len(errors) == len(set(errors)) and errors[0].startswith("evaluation_plan")
    _no_dump(errors)
    # the member edit (PUT) path validates through the same function: same list
    assert contract.parse_content(bad)[1] == errors


def test_minimal_foreign_model_proposal_is_refused_with_the_catalog_listed():
    content, _display, errors = contract.validate({**VALID_PROPOSAL, "model_id": FOREIGN},
                                                  _catalog())
    assert content is None
    assert errors == [f"model_id: '{FOREIGN}' is not available in this build — choose one of: "
                      + ", ".join(f"`{m}`" for m in _selectable())]
    _no_dump(errors)


@pytest.mark.siliconflow
def test_foreign_model_hint_lists_only_selectable_ids(monkeypatch):
    """N-ARCH-obs9: the "choose one of" hint never names a catalog model whose provider has
    no API key here (following it would earn a second error); configured → listed."""
    sf = [m for m in schema.allowed_model_ids() if m.startswith("siliconflow.")]
    assert sf, "the CN catalog carries SiliconFlow rows"
    monkeypatch.delenv("LAUNCHPAD_SILICONFLOW_API_KEY", raising=False)
    hint = contract.model_catalog_errors(FOREIGN)[0].split("choose one of:")[1]
    assert all(m not in hint for m in sf)
    assert all(f"`{m}`" in hint for m in schema.allowed_model_ids() if m not in sf)
    # the workspace's own record wins over the hub env (same source as the catalog prose)
    record = {"model_credentials": {"siliconflow": {"configured": True}}}
    hint = contract.model_catalog_errors(FOREIGN, record)[0].split("choose one of:")[1]
    assert all(f"`{m}`" in hint for m in schema.allowed_model_ids())
    monkeypatch.setenv("LAUNCHPAD_SILICONFLOW_API_KEY", "k")
    hint = contract.model_catalog_errors(FOREIGN)[0].split("choose one of:")[1]
    assert all(f"`{m}`" in hint for m in schema.allowed_model_ids())
    # both branches agree on the list
    monkeypatch.delenv("LAUNCHPAD_SILICONFLOW_API_KEY", raising=False)
    foreign_hint = contract.model_catalog_errors(FOREIGN)[0].split("choose one of:")[1]
    unconfigured_hint = contract.model_catalog_errors(sf[0])[0].split("choose one of:")[1]
    assert foreign_hint == unconfigured_hint


def test_the_model_message_follows_the_table():
    """C5 (M1-e): the proposal validator reads `allowed_model_ids()` live — a row published
    into the snapshot is accepted and listed; no module constant to overlay."""
    import dataclasses

    from app.schemas import model_catalog

    fake = "global.example.new-model-9"
    glm = model_catalog.current().models["global.zai.glm-5.3"]
    row = dataclasses.replace(glm, model_id=fake, label=fake, api_model_name=fake, default=False)
    model_catalog.publish(model_catalog.extended(model_catalog.current(), models={fake: row}))
    try:
        assert contract.model_catalog_errors(fake) == []
        assert contract.model_catalog_errors(FOREIGN)[0].endswith(f"`{fake}`")
        assert contract.model_catalog_errors(None) == []  # a missing member is a shape error
    finally:
        model_catalog.reset()


def test_shape_errors_are_readable_and_reported_together():
    bad = {**VALID_PROPOSAL, "env": {"A": "1"}, "memory": "short", "name": "Bad Name",
           "native_tools": ["browser"], "timeout_seconds": 5}
    del bad["system_prompt"]
    _c, _d, errors = contract.validate(bad, _catalog())
    assert "name: must be a lowercase slug of 3–48 characters (letters, digits, hyphens; " \
           "starts with a letter)" in errors
    assert "system_prompt: is required" in errors
    assert "memory: must be `disabled` or `workspace`" in errors
    assert "native_tools.0: must be `shell` and/or `file_operations`" in errors
    assert "env: is not a proposal member — remove it" in errors
    assert any(e.startswith("timeout_seconds:") and "greater than or equal to 10" in e
               for e in errors)
    _no_dump(errors)


def test_references_and_the_drafted_plan_are_reported_with_the_model():
    bad = {**VALID_PROPOSAL, "model_id": FOREIGN, "tools": ["gateway:nope"],
           "knowledge_bases": ["KBZZZ"]}
    _c, _d, errors = contract.validate(bad, _catalog())
    assert errors[0].startswith("model_id:")  # stage order: shape/model, then references
    assert "tools: 'gateway:nope' is not an available tool in this workspace" in errors
    assert "knowledge_bases: 'KBZZZ' is not an active managed knowledge base here" in errors
    assert len(errors) == 3


def test_bindings_refusal_is_worded_for_the_member(monkeypatch):
    """The defensive last stage: AgentSpec itself refusing. Simulated, since every rule
    it mirrors is now caught earlier."""
    def boom(_content, _catalog):
        schema.AgentSpec(name="plain-harness", method="harness", system_prompt="hi",
                         model_id="global.nowhere.model")

    monkeypatch.setattr(contract, "resource_bindings", boom)
    _c, _d, errors = contract.validate(VALID_PROPOSAL, _catalog())
    assert len(errors) == 1 and errors[0].startswith("proposal cannot be turned into an agent "
                                                      "spec — ")
    assert "model.not_allowed" in errors[0]
    _no_dump(errors)


def test_format_validation_errors_strips_pydantic_prefixes():
    with pytest.raises(ValidationError) as info:
        contract.ProposalContent.model_validate({**VALID_PROPOSAL, "summary": "x" * 4001,
                                                 "golden_tests": "nope"})
    errors = contract.format_validation_errors(info.value)
    assert "summary: too long (at most 4000)" in errors
    assert any(e.startswith("golden_tests: Input should be a valid list") for e in errors)
    _no_dump(errors)


def test_a_valid_proposal_still_validates_cleanly():
    content, _display, errors = contract.validate(VALID_PROPOSAL, _catalog())
    assert content is not None and errors == []


@pytest.mark.siliconflow
def test_a_catalog_model_of_an_unconfigured_provider_is_refused_naming_the_parameter(
    monkeypatch,
):
    """N-R6-obs1: the architect may only propose a model whose provider has a key in this
    workspace; the message names the SSM parameter and lists the selectable models."""
    sf = "siliconflow.zai-org/GLM-5.3"
    monkeypatch.delenv("LAUNCHPAD_SILICONFLOW_API_KEY", raising=False)
    errors = contract.model_catalog_errors(sf)
    assert len(errors) == 1 and errors[0].startswith(f"model_id: '{sf}' runs on SiliconFlow")
    assert "not configured in this workspace" in errors[0]
    assert "launchpad-siliconflow-api-key" in errors[0]
    assert errors[0].endswith("choose one of: `global.zai.glm-5.3`, `global.moonshotai.kimi-k3`")
    assert sf not in errors[0].split("choose one of:")[1]
    _no_dump(errors)
    # the whole proposal path reports it (member edit too), nothing else about the model
    content, display, got = contract.validate({**VALID_PROPOSAL, "model_id": sf}, _catalog())
    assert content is None and display["model_id"] == sf and got == errors
    assert contract.parse_content({**VALID_PROPOSAL, "model_id": sf})[1] == errors
    # configured → a valid proposal
    monkeypatch.setenv("LAUNCHPAD_SILICONFLOW_API_KEY", "k")
    assert contract.model_catalog_errors(sf) == []
    content, _display, got = contract.validate({**VALID_PROPOSAL, "model_id": sf}, _catalog())
    assert got == [] and content is not None and content.model_id == sf


def test_evaluator_entry_shape_errors_quote_the_rule_verbatim():
    """N-ARCH-obs11: 4 of 7 first proposals (2026-10-14) failed on two evaluation-plan
    shape details — a hyphen in a custom judge `name` (3×) and a `description` on a
    built-in entry (1×) — and the raw pydantic text ("String should match pattern …",
    "Extra inputs are not permitted") cost a 20–40 s patch turn each. Both messages now
    state the rule verbatim (the same sentence the skill's self-check carries) and the
    name error proposes the rename, so the fix is one turn."""
    from app.assistant import evaluation_plan as plan

    judge = {"kind": "judge", "key": "guard", "title": "Guardrails",
             "name": "helpdesk-guardrails-judge", "level": "TRACE",
             "instructions": "Given {context}, judge whether {assistant_turn} stays in policy.",
             "golden_test_ids": []}
    seed = {
        "evaluators": [
            {"kind": "existing", "key": "help", "title": "Helpfulness",
             "evaluator_id": "Builtin.Helpfulness", "golden_test_ids": [],
             "description": "a note on the built-in"},
            judge,
        ],
        "scenarios": [{"scenario_id": "GT-1", "golden_test_id": "GT-1",
                       "turns": [{"input": "How many vacation days?"}]}],
        "recommendation_keys": {}, "blocked_golden_tests": [],
    }
    _c, _d, errors = contract.validate({**VALID_PROPOSAL, "evaluation_plan": seed}, _catalog())
    name = [e for e in errors if e.startswith("evaluation_plan.evaluators.1.judge.name:")]
    assert name == [f"evaluation_plan.evaluators.1.judge.name: {plan.EVALUATOR_NAME_RULE} — "
                    "rename `helpdesk-guardrails-judge` to `helpdesk_guardrails_judge`"]
    assert f"`{plan._EVALUATOR_NAME_RE}`" in name[0] and "hyphens are not allowed" in name[0]
    desc = [e for e in errors
            if e.startswith("evaluation_plan.evaluators.0.existing.description:")]
    assert desc == [f"evaluation_plan.evaluators.0.existing.description: "
                    f"{plan.EXISTING_NO_DESCRIPTION_RULE}"]
    assert "`note`" in desc[0] and "no `description`" in desc[0]
    joined = "\n".join(errors)
    assert "String should match pattern" not in joined
    assert "Extra inputs are not permitted" not in joined
    _no_dump(errors)
    # the member edit (PUT) path reports the same two lines
    edit_errors = contract.parse_content({**VALID_PROPOSAL, "evaluation_plan": seed})[1]
    assert all(e in edit_errors for e in name + desc)
    # the reviewed-plan validator (prepare / PUT plan) speaks the same sentence
    raw = {"version": 1, "source_revision": 1, "source_content_hash": "0" * 64,
           "dataset": {"name": "d"}, "evaluators": [judge], "scenarios": [],
           "blocked_golden_tests": []}
    _p, plan_errors = plan.validate_plan(raw, VALID_PROPOSAL, revision=1, content_hash="0" * 64)
    assert any(e.startswith(f"evaluators.0.judge.name: {plan.EVALUATOR_NAME_RULE}")
               for e in plan_errors)
    # an unknown member of a custom entry names the entry kind, never the pydantic phrase
    extra = {**judge, "name": "guardrails_judge", "rubric": "x"}
    _p, plan_errors = plan.validate_plan({**raw, "evaluators": [extra]}, VALID_PROPOSAL,
                                         revision=1, content_hash="0" * 64)
    assert "evaluators.0.judge.rubric: is not a member of a `kind: judge` evaluator entry — " \
           "remove it" in plan_errors
    # a name that cannot be mechanically renamed states the rule without a suggestion
    assert plan.suggest_evaluator_name("---") is None
    assert plan.suggest_evaluator_name("9-lives judge") == "e_9_lives_judge"
    assert plan.suggest_evaluator_name("already_fine") is None
