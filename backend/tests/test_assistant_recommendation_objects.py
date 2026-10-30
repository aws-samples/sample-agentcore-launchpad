"""WB-009 (P2, 2026-10-14): ``evaluator_recommendations`` entries may be objects.

The architect kept emitting the field as an array of objects while the schema wanted
strings — ~10 shape errors and one extra repair turn per proposal. A canonical object
``{"text", "evaluator_id"?, "blocking"?, "threshold"?, "metric"?}`` is now valid as-is;
the common non-canonical forms (``{name|title|recommendation|evaluator: …}``, the skill's
registry rows) are normalised into it with a ``proposal_repaired`` note; an entry nothing
can be read from is still refused — in ONE readable line, not one per union branch.
Strings are untouched, so every stored revision hashes exactly as before. Hermetic.
"""

import json

from app.assistant import proposal as contract
from app.assistant.recommendation_intent import RECOMMENDATION_SHAPE_RULE, REPAIR_NOTE_PREFIX

BASE = {"version": 1, "name": "hr-helpdesk", "model_id": "global.zai.glm-5.3",
        "system_prompt": "You answer HR policy questions."}


def _parse(recommendations):
    return contract.parse_content({**BASE, "evaluator_recommendations": recommendations})


def _stored(content):
    return contract.content_dump(content)["evaluator_recommendations"]


def test_string_entries_are_stored_verbatim_and_hash_as_before():
    recs = ["Builtin.Helpfulness", "hr_guardrails_judge 阻断发布，阈值 1.00"]
    content, errors = _parse(recs)
    assert content is not None, errors
    dump = contract.content_dump(content)
    assert dump["evaluator_recommendations"] == recs
    # the stored form of a string-only proposal is byte-identical to the pre-WB-009 one
    legacy = {**dump, "evaluator_recommendations": list(recs)}
    assert contract.canonical_hash(dump) == contract.canonical_hash(legacy)


def test_canonical_objects_are_valid_as_emitted():
    recs = [{"text": "Refusal must hold on every red-team case", "evaluator_id": "Builtin.Refusal",
             "blocking": True, "threshold": 1.0, "metric": "pass rate"},
            {"text": "track helpfulness", "blocking": False}]
    content, errors = _parse(recs)
    assert content is not None, errors
    assert _stored(content) == recs  # no repair note, no None members


def test_registry_shaped_objects_are_normalised_with_a_repair_note():
    """The skill's §4 registry row: id/name, type, level, input signal, scoring method,
    suggested threshold, owner, failure response — read into the canonical entry, the
    rest folded into the text so nothing the architect wrote is lost."""
    recs = [{"name": "hr-guardrails-judge", "type": "LLM-as-a-judge", "level": "trace",
             "rationale": "catches policy violations", "suggested_threshold": "1.00",
             "gate": True, "owner": "HR ops"},
            {"evaluator": "Builtin.Helpfulness", "recommendation": "track helpfulness",
             "blocking": False}]
    content, errors = _parse(recs)
    assert content is not None, errors
    first, second = _stored(content)
    assert first["evaluator_id"] == "hr-guardrails-judge"
    assert first["blocking"] is True and first["threshold"] == 1.0
    assert first["text"].startswith("hr-guardrails-judge: catches policy violations (")
    for folded in ("type: LLM-as-a-judge", "level: trace", "owner: HR ops"):
        assert folded in first["text"]
    assert first["note"].startswith(f"{REPAIR_NOTE_PREFIX}: object entry normalised")
    for read in ("text from `rationale`", "evaluator_id from `name`", "blocking from `gate`",
                 "threshold from `suggested_threshold`",
                 "members folded into the text: type, level, owner"):
        assert read in first["note"], read
    assert second == {"text": "Builtin.Helpfulness: track helpfulness",
                      "evaluator_id": "Builtin.Helpfulness", "blocking": False,
                      "note": f"{REPAIR_NOTE_PREFIX}: object entry normalised — text from "
                              "`recommendation`; evaluator_id from `evaluator`"}


def test_title_and_description_objects_and_percent_thresholds():
    content, errors = _parse([{"title": "Latency judge", "description": "p95 under 3 s",
                               "metric": "p95 latency", "threshold": "90%"}])
    assert content is not None, errors
    (entry,) = _stored(content)
    assert entry["text"] == "Latency judge — p95 under 3 s"
    assert entry["metric"] == "p95 latency" and entry["threshold"] == 0.9
    assert "evaluator_id" not in entry  # "Latency judge" is a title, not an evaluator reference


def test_mixed_strings_and_objects_are_accepted_together():
    recs = ["Builtin.Refusal 作为门禁，阈值 0.9",
            {"text": "helpfulness trend", "evaluator_id": "Builtin.Helpfulness"},
            {"name": "hr-guardrails-judge", "recommendation": "block publishing"}]
    content, errors = _parse(recs)
    assert content is not None, errors
    stored = _stored(content)
    assert stored[0] == recs[0] and stored[1] == recs[1]
    assert stored[2]["evaluator_id"] == "hr-guardrails-judge" and "note" in stored[2]


def test_nonsense_entries_are_refused_in_one_readable_line_each():
    content, errors = _parse([{"foo": 1}, 42, "", ["a", "b"], None])
    assert content is None
    for idx in range(5):
        lines = [e for e in errors if e.startswith(f"evaluator_recommendations[{idx}]")]
        assert lines == [f"evaluator_recommendations[{idx}]: {RECOMMENDATION_SHAPE_RULE}"], lines
    assert not any("Input should be" in e or "union" in e.lower() for e in errors), errors


def test_a_canonical_object_with_a_bad_member_names_the_member():
    content, errors = _parse([{"text": "ok", "threshold": 1.5},
                              {"text": "ok", "evaluator_id": "has space"}])
    assert content is None
    assert any(e.startswith("evaluator_recommendations[0].threshold: ") for e in errors), errors
    assert any(e.startswith("evaluator_recommendations[1].evaluator_id: ") for e in errors)
    # the string branch of the union adds no second line for the same entry
    assert not any(e == f"evaluator_recommendations[0]: {RECOMMENDATION_SHAPE_RULE}"
                   for e in errors)


def test_a_repaired_entry_round_trips_through_a_member_edit():
    content, errors = _parse([{"name": "hr-guardrails-judge", "rationale": "block publishing",
                               "suggested_threshold": 1.0}])
    assert content is not None, errors
    stored = _stored(content)
    again, errors = _parse(json.loads(json.dumps(stored)))
    assert again is not None, errors
    assert _stored(again) == stored  # canonical already: no second repair note


def test_an_existing_note_is_kept_in_front_of_the_repair_note():
    content, errors = _parse([{"recommendation": "track helpfulness", "note": "member note"}])
    assert content is not None, errors
    (entry,) = _stored(content)
    assert entry["note"].startswith("member note | proposal_repaired: ")


def test_the_proposal_schema_states_the_shape_in_its_own_error_rule():
    assert '"text": "<the recommendation>"' in RECOMMENDATION_SHAPE_RULE
    assert '"blocking"?: true|false, "threshold"?: 0–1, "metric"?' in RECOMMENDATION_SHAPE_RULE
