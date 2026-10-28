"""Criteria sets: validation, versions, templates, publish and sign."""

import pytest

from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.core.errors import AppError
from app.dlc import criteria as svc
from app.models.dlc import CriteriaSet, Criterion

WS = DEFAULT_WORKSPACE_ID


def kind_of(evaluator_id: str) -> str:
    return "code" if evaluator_id.startswith("code-") else "judge"


def row(key, **overrides):
    base = {
        "key": key,
        "text": f"agent does {key}",
        "dimension": "quality",
        "tier": "gate",
        "threshold": 0.9,
        "level": "session",
        "executor": {"kind": "evaluator", "evaluator_id": "code-check"},
        "denominator": "sessions",
        "expected_type": "deterministic",
    }
    base.update(overrides)
    return base


def full_table():
    return [
        row("C-001", dimension="cognition", executor={"kind": "evaluator",
            "evaluator_id": "Builtin.TrajectoryInOrderMatch"}, expected_type="trajectory"),
        row("C-002"),
        row("C-003", dimension="responsibility", tier="redline", expected_type="redline"),
        row("C-004", dimension="cost", executor={"kind": "metric"},
            metric_rule={"metric": "cost_per_success_usd", "op": "<=", "value": 0.05}),
        row("C-005", dimension="performance", executor={"kind": "metric"},
            metric_rule={"metric": "latency_p95_ms", "op": "<=", "value": 3000}),
    ]


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        for model in (Criterion, CriteriaSet):
            session.query(model).delete()
        session.commit()
        yield session
    finally:
        session.rollback()
        session.close()


def normalized(rows):
    return [svc.normalize_criterion(r, kind_of) for r in rows]


def codes(findings, level="error"):
    return {f["code"] for f in findings if f["level"] == level}


def test_a_full_table_publishes_cleanly():
    assert codes(svc.validate(normalized(full_table()), for_publish=True)) == set()


def test_a_red_line_cannot_be_judged_by_an_llm():
    rows = normalized([row("C-1", tier="redline",
                           executor={"kind": "evaluator", "evaluator_id": "Builtin.Helpfulness"})])
    assert "criteria.redline_judge" in codes(svc.validate(rows))


def test_a_human_judged_criterion_is_valid_and_manual_is_its_old_name():
    human = row("C-006", dimension="quality", executor={"kind": "human"})
    legacy = row("C-007", dimension="quality", executor={"kind": "manual"})
    rows = normalized(full_table() + [human, legacy])
    assert [r["executor"]["kind"] for r in rows[-2:]] == ["human", "human"]
    assert codes(svc.validate(rows, for_publish=True)) == set()


def test_cost_and_performance_must_be_metrics():
    rows = normalized([row("C-1", dimension="cost")])
    assert "criteria.metric_dimension" in codes(svc.validate(rows))


def test_publishing_needs_every_dimension_or_a_written_not_applicable():
    rows = normalized(full_table()[:3])  # no cost / performance
    errors = codes(svc.validate(rows, for_publish=True))
    assert "criteria.dimension_missing" in errors
    rows[0]["notes"] = "n/a:cost batch job, n/a:performance offline"
    assert "criteria.dimension_missing" not in codes(svc.validate(rows, for_publish=True))


def test_a_set_needs_a_red_line():
    rows = normalized([r for r in full_table() if r["tier"] != "redline"])
    assert "criteria.no_redline" in codes(svc.validate(rows, for_publish=True))


def test_subject_must_be_the_agent():
    rows = normalized([row("C-1", text="让客户觉得被重视")])
    assert "criteria.subject_not_agent" in codes(svc.validate(rows), level="warning")


def test_an_uncalibrated_judge_is_observed():
    judge = svc.normalize_criterion(
        row("C-9", executor={"kind": "evaluator", "evaluator_id": "Builtin.Helpfulness"}),
        kind_of,
    )
    assert svc.is_judge(judge)
    assert svc.effective_tier(judge, calibrated=False) == "observe"
    assert svc.effective_tier(judge, calibrated=True) == "gate"
    code = svc.normalize_criterion(row("C-8"), kind_of)
    assert svc.effective_tier(code, calibrated=False) == "gate"


def test_summary_reports_effective_gates_and_compound_rate():
    rows = normalized(full_table() + [
        row("C-006", executor={"kind": "evaluator", "evaluator_id": "Builtin.Helpfulness"}),
    ])
    out = svc.summary(rows, calibrated_keys=set())
    # C-001, C-002, C-004, C-005 (metrics), C-006 (judge) are declared gates
    assert out["declared_gates"] == 5 and out["effective_gates"] == 4  # judge demoted
    # only pass-rate gates compound: C-001 × C-002
    assert out["compound_gate_rate"] == pytest.approx(0.81)
    calibrated = svc.summary(rows, calibrated_keys={"C-006"})
    assert calibrated["effective_gates"] == 5
    assert calibrated["compound_gate_rate"] == pytest.approx(0.729)


def test_draft_publish_sign_and_new_version(db):
    s = svc.create_set(db, WS, kind="agent", agent_id="a1", name="客服", actor="alice")
    svc.save_draft(db, s, full_table(), actor="alice", kind_of=kind_of)
    svc.publish(db, s, actor="alice")
    assert s.status == "published"
    with pytest.raises(AppError) as exc:
        svc.save_draft(db, s, full_table(), actor="alice", kind_of=kind_of)
    assert exc.value.code == "criteria.not_draft"
    with pytest.raises(AppError) as exc:
        svc.sign(db, s, actor="alice", note="", is_admin=False)
    assert exc.value.code == "criteria.self_sign"
    svc.sign(db, s, actor="bob", note="ok", is_admin=False)
    assert s.signed_by == "bob"
    v2 = svc.new_version(db, WS, s.lineage_id, actor="alice")
    assert v2.version == 2 and v2.status == "draft" and v2.parent_version == 1
    assert len(svc.criteria_of(db, v2.id)) == 5
    with pytest.raises(AppError):
        svc.new_version(db, WS, s.lineage_id, actor="alice")  # a draft already exists
    rows = full_table()
    rows[1]["threshold"] = 0.95
    svc.save_draft(db, v2, rows, actor="alice", kind_of=kind_of)
    svc.publish(db, v2, actor="alice")
    assert svc.get_version(db, WS, s.lineage_id, 1).status == "superseded"
    changes = svc.diff(svc.criteria_of(db, s.id), svc.criteria_of(db, v2.id))
    assert changes == [{"key": "C-002", "change": "changed", "fields": ["threshold"],
                        "before": {"threshold": 0.9}, "after": {"threshold": 0.95}}]


def test_an_agent_has_one_lineage(db):
    svc.create_set(db, WS, kind="agent", agent_id="a1", name="x", actor="alice")
    with pytest.raises(AppError) as exc:
        svc.create_set(db, WS, kind="agent", agent_id="a1", name="y", actor="alice")
    assert exc.value.code == "criteria.agent_has_set"


def _published_template(db, rows=None):
    t = svc.create_set(db, WS, kind="template", name="工业品客服", scenario="industrial",
                       actor="tom")
    svc.save_draft(db, t, rows or full_table(), actor="tom", kind_of=kind_of)
    svc.publish(db, t, actor="tom")
    return t


def test_an_agent_set_inherits_a_template_and_tracks_origins(db):
    t = _published_template(db)
    a = svc.create_set(db, WS, kind="agent", agent_id="a1", name="a", actor="alice",
                       template_id=t.lineage_id, template_version=1)
    assert {c.origin for c in svc.criteria_of(db, a.id)} == {"template"}
    rows = full_table()
    rows[1]["threshold"] = 0.95  # override
    rows.append(row("C-100", text="agent cites the policy"))  # addition
    svc.save_draft(db, a, rows, actor="alice", kind_of=kind_of)
    origins = {c.key: c.origin for c in svc.criteria_of(db, a.id)}
    assert origins["C-002"] == "override" and origins["C-100"] == "added"
    assert origins["C-001"] == "template"


def test_a_template_red_line_cannot_be_demoted_and_removal_needs_a_reason(db):
    t = _published_template(db)
    a = svc.create_set(db, WS, kind="agent", agent_id="a1", name="a", actor="alice",
                       template_id=t.lineage_id, template_version=1)
    rows = full_table()
    rows[2]["tier"] = "gate"
    rows[2]["threshold"] = 0.9
    with pytest.raises(AppError) as exc:
        svc.save_draft(db, a, rows, actor="alice", kind_of=kind_of)
    assert "criteria.redline_demoted" in codes(exc.value.detail["findings"])
    rows = [r for r in full_table() if r["key"] != "C-002"]
    with pytest.raises(AppError) as exc:
        svc.save_draft(db, a, rows, actor="alice", kind_of=kind_of,
                       removals=[{"key": "C-002", "reason": ""}])
    assert "criteria.removal_needs_reason" in codes(exc.value.detail["findings"])
    svc.save_draft(db, a, rows, actor="alice", kind_of=kind_of,
                   removals=[{"key": "C-002", "reason": "no recommendations in this agent"}])


def test_adopting_a_newer_template_keeps_overrides_and_needs_a_new_signature(db):
    t = _published_template(db)
    a = svc.create_set(db, WS, kind="agent", agent_id="a1", name="a", actor="alice",
                       template_id=t.lineage_id, template_version=1)
    rows = full_table()
    rows[1]["threshold"] = 0.97
    svc.save_draft(db, a, rows, actor="alice", kind_of=kind_of)
    svc.publish(db, a, actor="alice")
    svc.sign(db, a, actor="bob", note="", is_admin=False)

    t2 = svc.new_version(db, WS, t.lineage_id, actor="tom")
    template_rows = full_table() + [row("C-006", text="agent states stock status")]
    svc.save_draft(db, t2, template_rows, actor="tom", kind_of=kind_of)
    svc.publish(db, t2, actor="tom")
    assert svc.newer_template_version(db, a) == 2

    adopted = svc.adopt_template(db, WS, a.lineage_id, template_version=2, actor="alice")
    assert adopted.template_version == 2 and adopted.signed_by is None
    by_key = {c.key: c for c in svc.criteria_of(db, adopted.id)}
    assert by_key["C-002"].threshold == 0.97 and by_key["C-002"].origin == "override"
    assert by_key["C-006"].origin == "template"


def test_rows_from_an_evaluation_plan():
    plan = {"evaluators": [
        {"key": "e1", "kind": "code", "title": "never quotes another account's price",
         "blocking": True, "rules": {"checks": [{"type": "output_not_contains"}]},
         "level": "SESSION"},
        {"key": "e2", "kind": "existing", "evaluator_id": "Builtin.TrajectoryInOrderMatch",
         "title": "verify tier before price", "blocking": True, "threshold": 1.0},
        {"key": "e3", "kind": "judge", "title": "polite refusal", "blocking": False},
    ]}
    rows = svc.rows_from_evaluation_plan(plan)
    assert [r["tier"] for r in rows] == ["redline", "gate", "observe"]
    assert rows[0]["dimension"] == "responsibility" and rows[0]["threshold"] is None
    assert rows[1]["dimension"] == "cognition" and rows[1]["threshold"] == 1.0
