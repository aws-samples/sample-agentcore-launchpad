"""N17-obs308: the notes ``draft_plan`` writes stay English in the plan content, and
``plan_notes`` splits them into coded parts the zh-CN console renders in its own language
(the ``validation_codes`` pattern of N17-obs296). Every note below is built by the real
``draft_plan`` so a drifting f-string fails here. Hermetic: no AWS."""

import json

from app.assistant import evaluation_plan as plans
from app.assistant import plan_notes

HASH = "0" * 64
GOLDEN = [{"id": "GT-1", "input": "How many vacation days do I get?",
           "pass_criteria": "cites the leave policy"}]
SEED = {
    "evaluators": [{
        "kind": "judge", "key": "refusal", "title": "Refusal judge", "name": "refusal_judge",
        "level": "TRACE", "instructions": "Judge {context} for refusals", "golden_test_ids": [],
        "note": "seed note; second clause",
    }],
    "scenarios": [{"scenario_id": "GT-1", "golden_test_id": "GT-1",
                   "turns": [{"input": "How many vacation days do I get?"}],
                   "assertions": ["cites the leave policy"]}],
}


def _draft(recommendations):
    content = {
        "version": 1, "name": "hr-helpdesk", "model_id": "global.zai.glm-5.3",
        "system_prompt": "You answer HR policy questions.", "golden_tests": GOLDEN,
        "evaluator_recommendations": recommendations,
        "evaluation_plan": json.loads(json.dumps(SEED)),
    }
    return plans.draft_plan(content, revision=1, content_hash=HASH, agent_name="hr-helpdesk")


def _entry(draft, key):
    return next(e for e in draft["evaluators"] if e["key"] == key)


def test_gate_tags_on_an_entry_note_are_coded_and_the_seed_text_kept():
    draft = _draft([
        {"text": "refusal must gate", "evaluator_id": "refusal", "blocking": True,
         "threshold": 1, "metric": "pass_rate"},
        {"text": "refusal again", "evaluator_id": "refusal", "threshold": 0.85},
    ])
    note = _entry(draft, "refusal")["note"]
    assert "gate from recommendation #2 (source: stated): threshold 0.85" in note
    parts = plan_notes.entry_note_parts(note)
    assert [p["code"] for p in parts] == ["text", "text", plan_notes.GATE_FROM,
                                          plan_notes.GATE_FROM]
    assert [p["text"] for p in parts[:2]] == ["seed note", "second clause"]
    assert parts[2]["params"] == {"index": 1, "source": "stated", "blocking": True,
                                  "threshold": 1.0, "metric": "pass_rate"}
    assert parts[3]["params"] == {"index": 2, "source": "stated", "blocking": False,
                                  "threshold": 0.85, "metric": None}
    # the parts re-join to the stored note
    assert "; ".join(p["text"] for p in parts) == note


def test_named_in_recommendation_on_a_catalog_row():
    draft = _draft(["Builtin.Helpfulness must block publishing at threshold 0.7"])
    row = next(e for e in draft["evaluators"] if e.get("evaluator_id") == "Builtin.Helpfulness")
    parts = plan_notes.entry_note_parts(row["note"])
    assert parts[0] == {"code": plan_notes.NAMED_IN, "params": {"index": 1},
                        "text": "named in recommendation #1"}
    assert parts[1]["code"] == plan_notes.GATE_FROM
    assert parts[1]["params"]["source"] == "parsed"
    assert parts[1]["params"]["blocking"] is True and parts[1]["params"]["threshold"] == 0.7


def test_recommendation_notes_carried_unresolved_and_unclassified():
    draft = _draft([
        {"text": "refusal gate", "evaluator_id": "refusal", "blocking": True,
         "threshold": 1, "metric": "adversarial_pass_rate"},
        {"text": "compliance gate", "evaluator_id": "no-such-judge", "blocking": True,
         "threshold": 0.8},
        {"text": "be nice", "evaluator_id": "Custom.unknown"},
        "a recommendation with no evaluator and no gate",
    ])
    recs = draft["recommendations"]
    carried = plan_notes.recommendation_note_parts(recs[0]["note"])
    assert carried[0]["code"] == plan_notes.GATE_CARRIED
    assert carried[0]["params"] == {"source": "stated", "targets": ["refusal"], "blocking": True,
                                    "threshold": 1.0, "metric": "adversarial_pass_rate"}
    unresolved = plan_notes.recommendation_note_parts(recs[1]["note"])
    assert unresolved[0]["code"] == plan_notes.UNRESOLVED_GATE
    assert unresolved[0]["params"]["why_code"] == "not_listed_or_seed"
    assert unresolved[0]["params"]["evaluator_id"] == "no-such-judge"
    assert unresolved[0]["params"]["threshold"] == 0.8
    unclassified = plan_notes.recommendation_note_parts(recs[2]["note"])
    assert unclassified[0]["code"] == plan_notes.UNCLASSIFIED
    assert unclassified[0]["params"]["why_code"] in ("not_listed", "not_listed_or_seed")
    assert unclassified[0]["params"]["evaluator_id"] == "Custom.unknown"
    bare = plan_notes.recommendation_note_parts(recs[3]["note"])
    assert bare[0]["code"] == plan_notes.UNCLASSIFIED and bare[0]["params"]["why_code"] is None
    for rec in recs:
        assert plan_notes.recommendation_note_parts(rec["note"])[0]["text"] == rec["note"]


def test_unknown_text_stays_verbatim_and_note_details_keys_by_entry():
    assert plan_notes.recommendation_note_parts("member wrote this") == [
        {"code": "text", "params": {}, "text": "member wrote this"}]
    assert plan_notes.entry_note_parts("gate from recommendation #1 (source: stated): ???")[0][
        "code"] == "text"
    draft = _draft([{"text": "refusal gate", "evaluator_id": "refusal", "blocking": True}])
    details = plan_notes.note_details(draft)
    assert details["evaluators"]["refusal"]["message"] == _entry(draft, "refusal")["note"]
    assert details["recommendations"]["0"]["parts"][0]["code"] == plan_notes.GATE_CARRIED
    assert plan_notes.note_details(None) == {"evaluators": {}, "recommendations": {}}


def test_plan_payload_carries_the_coded_notes_and_keeps_the_english_content():
    from types import SimpleNamespace

    from app.assistant import evaluation_assets

    draft = _draft([{"text": "refusal gate", "evaluator_id": "refusal", "blocking": True,
                     "threshold": 1}])
    stored = json.loads(json.dumps(draft))
    row = SimpleNamespace(
        id="plan", conversation_id="c", proposal_id="p", source_revision=1,
        source_content_hash=HASH, revision=1, source="platform", status="draft",
        content=stored, content_hash="h", validation_errors=[], created_by="u", created_at=None)
    out = evaluation_assets.plan_out(row, None)
    assert out["content"] == draft  # the stored notes are untouched
    rec = out["note_details"]["recommendations"]["0"]
    assert rec["message"] == draft["recommendations"][0]["note"]
    assert rec["parts"][0]["code"] == plan_notes.GATE_CARRIED
    assert out["note_details"]["evaluators"]["refusal"]["parts"][-1]["code"] == plan_notes.GATE_FROM


def test_a_rename_after_the_gate_tags_is_its_own_part():
    from app.assistant import evaluation_assets

    draft = _draft([{"text": "refusal gate", "evaluator_id": "refusal", "blocking": True,
                     "metric": "pass_rate"}])
    evaluation_assets.rename_taken_evaluators(draft, {"refusal_judge"}, revision=1)
    parts = plan_notes.entry_note_parts(_entry(draft, "refusal")["note"])
    assert [p["code"] for p in parts] == ["text", "text", plan_notes.GATE_FROM, plan_notes.RENAMED]
    assert parts[2]["params"]["metric"] == "pass_rate"
    assert parts[3]["params"] == {"name": "refusal_judge"}
    sentence = "renamed from 'x': that evaluator name is already taken in this workspace"
    assert plan_notes.entry_note_parts(sentence) == [
        {"code": plan_notes.RENAMED, "params": {"name": "x"}, "text": sentence}]
