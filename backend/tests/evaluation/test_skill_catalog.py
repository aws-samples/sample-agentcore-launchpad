"""LangGraph Deep Agents skill catalogues reach the skill judges (lead add-on, Round 3).

Deep Agents writes the offered skills as a ``**Available Skills:**`` list with bold names in
the model's system prompt; ``parse_available_skills`` reads only the ``<available_skills>``
block and ``### Available skills``, so a Deep Agents session had no offered skills.
``skill_catalog.available_skills`` is the hook the engine and the skill evaluators share.
Fixture: r17-bf7's ``tests/fixtures/langgraph/deepagents_{split,unified}.json``.
"""

from __future__ import annotations

import json

import pytest
from strands_evals.extractors.skills import parse_available_skills

from app.services.strands_eval import engine_core, skill_catalog
from tests.evaluation.fakes import FakeJudge
from tests.evaluation.test_langgraph_extraction import DEEP, _provider

OFFERED = [("haiku-writer", "Write a haiku (5-7-5 syllables) on a topic the user names."),
           ("trip-plan", "Plan a short business trip itinerary.")]
DEEP_TEXT = """You have access to a skills library.

**Available Skills:**

- **haiku-writer**: Write a haiku (5-7-5 syllables) on a topic the user names.
  -> Read `/skills/haiku-writer/SKILL.md` for full instructions
- **trip-plan**: Plan a short business trip itinerary.
  -> Read `/skills/trip-plan/SKILL.md` for full instructions

**How to Use Skills (Progressive Disclosure):**

- **not-a-skill**: a bullet of the next section
"""


def _session(variant):
    fixture = DEEP[variant]
    provider = _provider(fixture["records"], "both")
    return provider, fixture["session_id"], provider.get_evaluation_data(
        fixture["session_id"])["trajectory"]


def test_the_deep_agents_list_parses_and_stops_at_the_next_section():
    assert [tuple(s) for s in skill_catalog.parse_deepagents(DEEP_TEXT)] == OFFERED
    assert skill_catalog.available_skills(DEEP_TEXT) == skill_catalog.parse_deepagents(DEEP_TEXT)
    assert skill_catalog.parse_deepagents("no catalogue here") == []
    empty = "**Available Skills:**\n\n(No skills available yet.)\n"
    assert skill_catalog.available_skills(empty) == [] and skill_catalog.advertised(empty)
    assert not skill_catalog.advertised("no catalogue here")


def test_the_library_formats_still_win():
    xml = ("<available_skills><skill><name>pdf</name><description>Read PDFs</description>"
           "</skill></available_skills>\n" + DEEP_TEXT)
    assert [s.name for s in skill_catalog.available_skills(xml)] == ["pdf"]


@pytest.mark.parametrize("variant", ["split", "unified"])
def test_a_deep_agents_session_offers_its_skills(variant):
    _provider_, _sid, session = _session(variant)
    assert parse_available_skills(session) == []  # the gap this hook closes
    assert [tuple(s) for s in skill_catalog.available_skills(session)] == OFFERED


@pytest.mark.parametrize("variant", ["split", "unified"])
def test_skill_selection_and_the_placeholder_see_the_offered_skills(variant):
    provider, session_id, _session_ = _session(variant)
    judge = FakeJudge()
    evaluators = engine_core.EvaluatorSet(
        [{"id": "Builtin.SkillSelectionAccuracy"},
         {"id": "cust_offered", "definition": "judge", "level": "TOOL_CALL",
          "instructions": "Offered skills:\n{available_skills}\nCall: {tool_turn}",
          "rating_scale": [{"label": "pass", "value": 1.0}, {"label": "fail", "value": 0.0}]}],
        lambda *_a, **_k: judge)
    engine_core.evaluate_session(provider, session_id, evaluators, None, job_id="j",
                                 sleep=lambda _s: None)
    prompts = [json.dumps(c["messages"], ensure_ascii=False) for c in judge.calls]
    skill = [p for p in prompts if "Offered skills:" not in p]
    custom = [p for p in prompts if "Offered skills:" in p]
    assert len(skill) == 2 and custom
    for prompt in skill + custom:
        assert "- haiku-writer: Write a haiku (5-7-5 syllables)" in prompt
        assert "- trip-plan: Plan a short business trip itinerary." in prompt
        assert "not recorded by this harness" not in prompt
