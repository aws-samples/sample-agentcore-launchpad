"""N-ARCH-obs14 (2026-10-14, architect 1.6.3): over-long free text on a seed evaluator entry.

A live brief ("at least 10 golden tests covering refusals", HR helpdesk) produced a proposal
whose ``evaluation_plan.evaluators[7].judge.description`` was 201 characters; the whole
proposal became INVALID with pydantic's raw "String should have at most 200 characters" and
cost a turn. ``proposal._parse`` now trims the display-only members of an evaluator entry
(``title`` 200, ``description`` 200, ``note`` 2000) at a word boundary with an ellipsis and
records a ``proposal_repaired`` note on the entry (the WB-009 style); the proposal stays
VALID. Identifiers, judge ``instructions``, rating scales, code rules and scenario members
are never shortened — they stay errors, now in a readable line. Hermetic.
"""

from app.assistant import evaluation_plan as plan
from app.assistant import proposal as contract
from app.assistant.recommendation_intent import REPAIR_NOTE_PREFIX

BASE = {"version": 1, "name": "hr-helpdesk", "model_id": "global.zai.glm-5.3",
        "system_prompt": "You answer HR policy questions."}
JUDGE = {"kind": "judge", "key": "refusal_judge", "title": "Refusal judge",
         "name": "refusal_judge", "level": "TRACE",
         "instructions": "Given {context}, judge whether {assistant_turn} refuses correctly.",
         "golden_test_ids": []}
# 40 × "word " + "x" — 201 characters, the live length
DESC_201 = "word " * 40 + "x"


def _seed(*evaluators):
    return {"evaluators": list(evaluators), "scenarios": [], "recommendation_keys": {},
            "blocked_golden_tests": []}


def _parse(*evaluators):
    return contract.parse_content({**BASE, "evaluation_plan": _seed(*evaluators)})


def _entry(content, index=0):
    return contract.content_dump(content)["evaluation_plan"]["evaluators"][index]


def test_the_live_201_character_description_is_trimmed_and_the_proposal_stays_valid():
    assert len(DESC_201) == 201
    content, errors = _parse({**JUDGE, "description": DESC_201})
    assert errors == [] and content is not None
    entry = _entry(content)
    # cut before the last space inside the first 199 characters, ellipsis appended
    assert entry["description"] == "word " * 38 + "word…"
    assert len(entry["description"]) == 195 <= plan.MAX_DESCRIPTION
    assert entry["note"] == f"{REPAIR_NOTE_PREFIX}: judge description trimmed from 201 to 195 chars"
    assert "String should have at most" not in "\n".join(errors)


def test_a_400_character_run_without_spaces_is_cut_hard_to_exactly_200():
    content, errors = _parse({**JUDGE, "description": "x" * 400})
    assert errors == [] and content is not None
    entry = _entry(content)
    assert entry["description"] == "x" * 199 + "…" and len(entry["description"]) == 200
    assert entry["note"] == f"{REPAIR_NOTE_PREFIX}: judge description trimmed from 400 to 200 chars"


def test_exactly_200_characters_is_untouched_and_carries_no_repair_note():
    content, errors = _parse({**JUDGE, "description": "y" * 200})
    assert errors == [] and content is not None
    entry = _entry(content)
    assert entry["description"] == "y" * 200
    assert not entry.get("note")


def test_an_untouched_seed_is_returned_as_the_same_object():
    seed = _seed({**JUDGE, "description": "short"})
    out, changed = plan.trim_seed_free_text(seed)
    assert out is seed and changed is False
    for other in (None, "x", 3, {"evaluators": "not a list"}):
        assert plan.trim_seed_free_text(other) == (other, False)


def test_title_and_note_are_trimmed_too_and_an_existing_note_is_kept_in_front():
    content, errors = _parse({**JUDGE, "title": "T " * 150, "note": "member note",
                              "description": DESC_201})
    assert errors == [] and content is not None
    entry = _entry(content)
    assert len(entry["title"]) <= plan.MAX_TITLE and entry["title"].endswith("T…")
    assert entry["note"] == (
        f"member note | {REPAIR_NOTE_PREFIX}: judge title trimmed from 300 to "
        f"{len(entry['title'])} chars; judge description trimmed from 201 to 195 chars")
    content, errors = _parse({**JUDGE, "note": "n" * 2100})
    assert errors == [] and content is not None
    note = _entry(content)["note"]
    assert len(note) <= plan.MAX_TEXT and note.startswith("n" * 100)
    assert f"| {REPAIR_NOTE_PREFIX}: judge note trimmed from 2100 to " in note


def test_identifiers_instructions_and_scenario_members_are_never_trimmed():
    """A cut there would change what the evaluator does or what the test asks: these stay
    errors — in a readable line, not pydantic's "String should have at most N characters"."""
    content, errors = _parse({**JUDGE, "name": "n" * 60, "description": "fine",
                              "instructions": "Given {context} " + "z" * 4000})
    assert content is None
    assert any(e.startswith("evaluation_plan.evaluators.0.judge.name: ")
               and plan.EVALUATOR_NAME_RULE in e for e in errors)
    assert ("evaluation_plan.evaluators.0.judge.instructions: too long (4016 characters; "
            "at most 4000) — shorten it") in errors
    joined = "\n".join(errors)
    assert "String should have at most" not in joined and "input_value" not in joined
    # a scenario assertion over its limit is reported, never shortened
    seed = {**_seed(JUDGE), "scenarios": [{"scenario_id": "GT-1", "golden_test_id": "GT-1",
                                           "turns": [{"input": "Hi"}],
                                           "assertions": ["a" * 1001]}]}
    out, changed = plan.trim_seed_free_text(seed)
    assert out is seed and changed is False
    assert any("scenarios.0.assertions.0: too long (1001 characters; at most 1000)" in e
               for e in plan.seed_errors(seed))


def test_a_description_on_a_built_in_entry_still_reports_the_obs11_rule_untrimmed():
    existing = {"kind": "existing", "key": "help", "title": "Helpfulness",
                "evaluator_id": "Builtin.Helpfulness", "golden_test_ids": [],
                "description": DESC_201}
    out, changed = plan.trim_seed_free_text(_seed(existing))
    assert changed is False and out["evaluators"][0]["description"] == DESC_201
    _content, errors = _parse(existing)
    assert errors == [f"evaluation_plan.evaluators.0.existing.description: "
                      f"{plan.EXISTING_NO_DESCRIPTION_RULE}"]


def test_trim_text_rule():
    assert plan.trim_text("short", 200) == "short"
    assert plan.trim_text("a" * 200, 200) == "a" * 200
    assert plan.trim_text("a" * 100 + " " + "b" * 100, 200) == "a" * 100 + "…"
    # a space only in the first half is ignored: cut hard rather than lose half the text
    assert plan.trim_text("a b" + "c" * 300, 200) == ("a b" + "c" * 300)[:199] + "…"
    assert len(plan.trim_text("汉" * 400, 200)) == 200
    assert plan.trim_text("x" * 150 + ", " + "y" * 100, 200) == "x" * 150 + "…"
