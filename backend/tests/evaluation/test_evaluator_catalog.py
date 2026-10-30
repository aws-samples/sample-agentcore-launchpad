from app.core.config import get_settings

"""GET /api/eval/evaluators — catalog classification (N12 E1-1).

Builtins (incl. the skill evaluators) come from the local catalog; the custom rows of the
workspace are classified by their definition and project ``evaluator_type``/``provider``.
There is no account listing in CN: third-party managed evaluators do not exist.
"""

GENERAL_PROMPT_TEMPLATE_EVALUATORS = {
    "Builtin.GoalSuccessRate": "SESSION",
    "Builtin.Helpfulness": "TRACE",
    "Builtin.Correctness": "TRACE",
    "Builtin.Faithfulness": "TRACE",
    "Builtin.ResponseRelevance": "TRACE",
    "Builtin.ContextRelevance": "TRACE",
    "Builtin.Conciseness": "TRACE",
    "Builtin.Coherence": "TRACE",
    "Builtin.InstructionFollowing": "TRACE",
    "Builtin.Refusal": "TRACE",
    "Builtin.Harmfulness": "TRACE",
    "Builtin.Stereotyping": "TRACE",
    "Builtin.ToolSelectionAccuracy": "TOOL_CALL",
    "Builtin.ToolParameterAccuracy": "TOOL_CALL",
}
SKILL_EVALUATORS = {
    "Builtin.SkillSelectionAccuracy": "TOOL_CALL",
    "Builtin.SkillInstructionFollowing": "TOOL_CALL",
}
TRAJECTORY_EVALUATORS = {
    "Builtin.TrajectoryExactOrderMatch": "SESSION",
    "Builtin.TrajectoryInOrderMatch": "SESSION",
    "Builtin.TrajectoryAnyOrderMatch": "SESSION",
}
EXPECTED_BUILTIN_EVALUATORS = {
    **GENERAL_PROMPT_TEMPLATE_EVALUATORS,
    **SKILL_EVALUATORS,
    **TRAJECTORY_EVALUATORS,
}


def test_catalog_builtins_only(client):
    res = client.get("/api/eval/evaluators")
    assert res.status_code == 200
    body = res.json()
    rows = body["evaluators"]
    assert all(r["source"] == "builtin" for r in rows)
    assert body["builtin_count"] == len(rows) == 19
    assert {r["id"]: r["level"] for r in rows} == EXPECTED_BUILTIN_EVALUATORS
    assert all(r["evaluator_type"] == "Builtin" for r in rows)
    # N17-obs403: Global has no ContextRelevance evaluator — the one Launchpad-only builtin
    assert {r["id"] for r in rows if r["provider"] != "AWS"} == {"Builtin.ContextRelevance"}
    assert next(r for r in rows if r["id"] == "Builtin.ContextRelevance")["provider"] == "Launchpad"
    # Ground-truth is required only by the three session-level trajectory matchers.
    assert {r["id"] for r in rows if r.get("requires_ground_truth")} == set(TRAJECTORY_EVALUATORS)
    assert all(
        next(r for r in rows if r["id"] == e)["level"] == "TOOL_CALL" for e in SKILL_EVALUATORS
    )
    assert len(GENERAL_PROMPT_TEMPLATE_EVALUATORS) == 14
    assert len(SKILL_EVALUATORS) == 2
    assert len(TRAJECTORY_EVALUATORS) == 3


def test_catalog_classifies_the_workspace_rows(client):
    judge = client.post("/api/eval/evaluators", json={
        "name": "my_judge", "instructions": "Rate the tone of {context}",
    }).json()["evaluator_id"]
    derived = client.post("/api/eval/evaluators", json={
        "name": "my_derived", "base_evaluator_id": "Builtin.GoalSuccessRate",
    }).json()["evaluator_id"]
    res = client.get("/api/eval/evaluators")
    body = res.json()
    rows = body["evaluators"]
    by_id = {r["id"]: r for r in rows}
    builtin_rows = [r for r in rows if r["source"] == "builtin"]
    assert len(builtin_rows) == body["builtin_count"] == 19
    assert rows[:19] == builtin_rows  # builtins first, then the workspace's rows
    assert by_id[judge] == {
        "id": judge, "name": "my_judge", "level": "TRACE", "status": "ACTIVE",
        "source": "custom", "evaluator_type": "Custom", "provider": "Custom",
        "definition": "judge",
        "pass_threshold": 0.7,  # N17-obs425: a custom evaluator keeps the default bar
        # N17-obs428 (additive): the zh label display's inputs — no base, the row's own scale
        "base_evaluator_id": None, "scale_labels": by_id[judge]["scale_labels"],
        "version": 1,  # N17-obs434 (additive): the definition's version, listed
        # N17-obs437 (additive): a rubric reading no ground truth
        "requires_ground_truth": False, "ground_truth_fields": [],
        # Family B step 1: the console principal that created the row (open console →
        # the operator's name, no users.id)
        "created_by": None, "created_by_name": get_settings().auth_username,
    }
    assert by_id[derived]["source"] == "custom"
    assert by_id[derived]["evaluator_type"] == "CustomDerived"
    assert by_id[derived]["provider"] == "AWS"
    assert by_id[derived]["level"] == "SESSION"
    assert by_id[derived]["definition"] == "derived"
    # N17-obs428: a derived row names its base and has no scale of its own
    assert by_id[derived]["base_evaluator_id"] == "Builtin.GoalSuccessRate"
    assert by_id[derived]["scale_labels"] == []
    assert by_id[derived]["version"] == 1
    # a built-in row lists no version (it is the library release, on the detail)
    assert "version" not in by_id["Builtin.Helpfulness"]


def test_catalog_hides_other_workspaces_rows(client):
    from app.core.db import SessionLocal
    from app.evaluation.models import EvalEvaluator

    db = SessionLocal()
    try:
        db.add(EvalEvaluator(workspace_id="other", name="foreign", definition="judge",
                             instructions="x {session}", rating_scale=[]))
        db.commit()
    finally:
        db.close()
    rows = client.get("/api/eval/evaluators").json()["evaluators"]
    assert all(r["source"] == "builtin" for r in rows)
