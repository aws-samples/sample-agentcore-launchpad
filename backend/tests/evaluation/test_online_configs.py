"""Per-agent online evaluation configs — lifecycle, owner matrix, results (N12 E2-1/E2-3).

The ``OnlineEvalConfig`` row is the config (plan CA-3): every field AWS used to hold is a
column, so the assertions are row assertions. The owner matrix keeps ``agent`` (created
here for an agent) and ``experiment`` (``exp_*`` / ``can_*`` arms created by the experiment /
canary flows through ``create_for_owner``); ``external`` cannot occur any more (plan §B.9).
Results aggregate ``eval_results`` rows written by the online scheduler.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest

import app.evaluation.service as svc
from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.core.errors import AppError
from app.evaluation import online
from app.evaluation.models import EvalEvaluator, EvalResult, OnlineEvalConfig, OnlineSample
from app.models.ledger import Agent
from tests.conftest import ws_ctx

LOG_GROUP = "/aws/bedrock-agentcore/runtimes/rt-1-DEFAULT"
SERVICE = "eval_agent_abc123.DEFAULT"
T0 = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)
CONFIG_ID_RE = re.compile(r"^oe_[a-zA-Z0-9_]+-[0-9a-f]{10}$")


def make_agent(db, name="eval-agent", method="zip_runtime", resource_id="rt-1") -> Agent:
    agent = Agent(
        workspace_id=DEFAULT_WORKSPACE_ID, name=name, method=method, status="active",
        arn=f"arn:aws:bedrock-agentcore:us-west-2:1:runtime/{resource_id}",
        resource_id=resource_id, spec={"name": name},
    )
    db.add(agent)
    db.commit()
    return agent


class FakeDescribeLogs:
    """The only CloudWatch call create makes: does the agent's log group exist yet?"""

    def __init__(self, groups=(LOG_GROUP,)):
        self.groups = list(groups)
        self.calls: list[dict] = []

    def describe_log_groups(self, **kwargs):
        self.calls.append(kwargs)
        prefix = kwargs.get("logGroupNamePrefix", "")
        return {"logGroups": [{"logGroupName": g} for g in self.groups if g.startswith(prefix)]}


def stub_environment(monkeypatch, *, log_groups=(LOG_GROUP,)):
    """No AWS: the runtime name for ``resolve_telemetry`` and the log-group probe are stubbed."""
    logs = FakeDescribeLogs(log_groups)
    monkeypatch.setattr(online, "_logs_client", lambda _ctx: logs)
    monkeypatch.setattr(svc, "control_client", lambda _ws=None: MagicMock())
    monkeypatch.setattr(
        svc.rt, "get_runtime", lambda client, rid: {"agentRuntimeName": "eval_agent_abc123"}
    )
    return logs


CONFIG_CREATED = datetime(2026, 1, 1, tzinfo=UTC)


def ledger_row(db, agent, config_id, name="oe_eval_agent_aaaaaa", **cols):
    fields = {
        "workspace_id": DEFAULT_WORKSPACE_ID, "agent_id": agent.id, "agent_name": agent.name,
        "config_id": config_id, "config_arn": f"arn:{config_id}", "name": name,
        "service_name": SERVICE, "log_group": LOG_GROUP,
        "evaluators": ["Builtin.Helpfulness", "Builtin.Refusal"],
        "sampling_percentage": 5.0, "session_timeout_minutes": 3,
        # N17-obs444: a config samples only activity after it started; the suites scan
        # fixed-date sessions, so the row predates them unless a test says otherwise
        "created_at": CONFIG_CREATED,
    }
    fields.update(cols)
    row = OnlineEvalConfig(**fields)
    db.add(row)
    db.commit()
    return row


def judge_row(db, evaluator_id, instructions="Rate the answer.", **cols):
    row = EvalEvaluator(id=evaluator_id, workspace_id=DEFAULT_WORKSPACE_ID, name=evaluator_id,
                        definition="judge", instructions=instructions, **cols)
    db.add(row)
    db.commit()
    return row


def result_row(db, config_id, session_id, evaluator, score, *, label="Pass", at=None,
               explanation=None, error=None, trace_id=None, ordinal=0, source="online"):
    row = EvalResult(
        id=f"{config_id}:{session_id}:{evaluator}:{ordinal}", workspace_id=DEFAULT_WORKSPACE_ID,
        source=source, config_id=config_id, session_id=session_id, trace_id=trace_id,
        evaluator_id=evaluator, level="TRACE", score=score, label=label,
        explanation=explanation or f"because {evaluator}",
        error_type="RuntimeError" if error else None, error_message=error,
        created_at=at or datetime.now(UTC),
    )
    db.add(row)
    db.commit()
    return row


# ─── pure helpers ───────────────────────────────────────────────────────────


def test_generate_name_matches_the_name_regex():
    for raw in ("HR Assistant 財報", "a" * 80, "---", ""):
        name = online.generate_name(raw, suffix="abc123")
        assert online.NAME_RE.match(name), name
    assert online.generate_name("hr-assistant", suffix="abc123") == "oe_hr_assistant_abc123"


def test_owner_classification_is_agent_or_experiment():
    assert online.owner_of("exp_1234abcd_oe1") == "experiment"
    assert online.owner_of("can_1234abcd_oec") == "experiment"
    assert online.owner_of("oe_hr_assistant_abc123") == "agent"
    assert online.owner_of("quick_start") == "agent"  # no external owner in CN


def test_validate_filters_shape():
    ok = online.validate_filters([
        {"key": "session.id", "operator": "Contains", "value": {"stringValue": "vip"}},
        {"key": "latency", "operator": "GreaterThan",
         "value": {"doubleValue": 2.5, "stringValue": None}},
    ])
    assert ok[1]["value"] == {"doubleValue": 2.5}
    with pytest.raises(AppError) as bad_key:
        online.validate_filters(
            [{"key": "a b", "operator": "Equals", "value": {"stringValue": "x"}}]
        )
    assert bad_key.value.code == "online_eval.invalid_filter"
    with pytest.raises(AppError):
        online.validate_filters([{"key": "k", "operator": "Like", "value": {"stringValue": "x"}}])
    with pytest.raises(AppError):
        online.validate_filters([{"key": "k", "operator": "Equals",
                                  "value": {"stringValue": "x", "booleanValue": True}}])
    with pytest.raises(AppError):
        online.validate_filters(
            [{"key": f"k{i}", "operator": "Equals", "value": {"stringValue": "x"}}
             for i in range(6)]
        )


def test_normalize_covers_the_contract_keys_for_rows_and_dicts():
    db = SessionLocal()
    agent = make_agent(db)
    row = ledger_row(db, agent, "oe_a-1", filters=[{"key": "k", "operator": "Equals",
                                                     "value": {"stringValue": "v"}}])
    out = online._normalize(row)
    assert out["config_id"] == "oe_a-1" and out["owner"] == "agent"
    assert out["filter_count"] == 1 and out["detailed"] is True
    assert out["matched_agent"] is None and out["execution_role_arn"] is None
    assert out["data_source"] == {"log_groups": [LOG_GROUP], "service_name": SERVICE}
    assert out["results_log_group"] == online.results_log_group("oe_a-1")
    # the contract test feeds a plain dict — same key-set, nothing raised
    assert set(online._normalize({"config_id": "x"})) == set(out)
    db.close()


# ─── create ─────────────────────────────────────────────────────────────────


def test_create_writes_the_row_as_local_truth(client, monkeypatch):
    db = SessionLocal()
    agent = make_agent(db)
    logs = stub_environment(monkeypatch)

    res = client.post("/api/eval/online", json={
        "agent_id": agent.id,
        "evaluators": ["Builtin.Helpfulness", "Builtin.Refusal"],
        "sampling_percentage": 25,
        "session_timeout_minutes": 5,
        "filters": [{"key": "session.id", "operator": "Contains",
                     "value": {"stringValue": "vip"}}],
        "description": "vip sessions",
        "enable_on_create": False,
    })
    assert res.status_code == 201, res.text
    body = res.json()
    assert logs.calls and logs.calls[0]["logGroupNamePrefix"] == LOG_GROUP
    assert CONFIG_ID_RE.match(body["config_id"]), body["config_id"]
    assert body["owner"] == "agent" and body["agent_id"] == agent.id
    assert body["status"] == "ACTIVE" and body["execution_status"] == "DISABLED"
    assert body["mode"] == "scores" and body["insights"] == []
    assert body["evaluators"] == ["Builtin.Helpfulness", "Builtin.Refusal"]
    assert body["sampling_percentage"] == 25.0 and body["session_timeout_minutes"] == 5
    assert body["filter_count"] == 1 and body["filters"][0]["key"] == "session.id"
    assert body["description"] == "vip sessions"
    assert body["data_source"] == {"log_groups": [LOG_GROUP], "service_name": SERVICE}
    assert body["results_log_group"] == online.results_log_group(body["config_id"])
    assert body["arn"].startswith("arn:") and body["config_id"] in body["arn"]
    assert "amazonaws" not in body["arn"]
    row = db.query(OnlineEvalConfig).one()
    assert row.config_id == body["config_id"] and row.workspace_id == DEFAULT_WORKSPACE_ID
    assert row.log_group == LOG_GROUP and row.service_name == SERVICE
    assert row.evaluators == ["Builtin.Helpfulness", "Builtin.Refusal"]
    assert row.sampling_percentage == 25.0 and row.session_timeout_minutes == 5
    assert row.execution_status == "DISABLED" and row.status == "ACTIVE"
    assert row.filters == [{"key": "session.id", "operator": "Contains",
                            "value": {"stringValue": "vip"}}]
    db.close()


def test_create_defaults_enable_and_auto_description(client, monkeypatch):
    db = SessionLocal()
    agent = make_agent(db)
    stub_environment(monkeypatch)
    body = client.post("/api/eval/online", json={
        "agent_id": agent.id, "evaluators": ["Builtin.Helpfulness"]}).json()
    assert body["execution_status"] == "ENABLED"
    assert body["sampling_percentage"] == 10.0 and body["session_timeout_minutes"] == 15
    assert body["description"] == "Launchpad online evaluation · eval-agent"
    db.close()


@pytest.mark.parametrize("evaluators, code", [
    (["Builtin.TrajectoryInOrderMatch"], "online_eval.evaluator_unsupported"),
    (["Builtin.NotAThing"], "online_eval.evaluator_unsupported"),
    (["ghost_judge"], "online_eval.evaluator_unsupported"),
])
def test_create_rejects_unsupported_evaluators(client, monkeypatch, evaluators, code):
    db = SessionLocal()
    agent = make_agent(db)
    stub_environment(monkeypatch)
    res = client.post("/api/eval/online", json={"agent_id": agent.id, "evaluators": evaluators})
    assert res.status_code == 400
    assert res.json()["code"] == code
    assert db.query(OnlineEvalConfig).count() == 0
    db.close()


def test_create_rejects_ground_truth_judge_and_accepts_trace_only_one(client, monkeypatch):
    db = SessionLocal()
    agent = make_agent(db)
    stub_environment(monkeypatch)
    judge_row(db, "cust_gt", instructions="Compare to {expected_response}")
    judge_row(db, "cust_ok", instructions="Rate {assistant_turn} given {context}.")
    res = client.post("/api/eval/online", json={"agent_id": agent.id, "evaluators": ["cust_gt"]})
    assert res.status_code == 400
    assert res.json()["code"] == "online_eval.evaluator_unsupported"
    assert res.json()["detail"]["placeholders"] == ["expected_response"]
    res = client.post("/api/eval/online", json={
        "agent_id": agent.id, "evaluators": ["cust_ok", "Builtin.Helpfulness"]})
    assert res.status_code == 201, res.text
    assert res.json()["evaluators"] == ["cust_ok", "Builtin.Helpfulness"]
    db.close()


def test_create_bounds_are_422(client, monkeypatch):
    db = SessionLocal()
    agent = make_agent(db)
    stub_environment(monkeypatch)
    too_many = [f"Builtin.Helpfulness{i}" for i in range(11)]
    res = client.post("/api/eval/online", json={"agent_id": agent.id, "evaluators": too_many})
    assert res.status_code == 422
    assert client.post("/api/eval/online", json={
        "agent_id": agent.id, "evaluators": ["Builtin.Helpfulness"], "sampling_percentage": 0,
    }).status_code == 422
    assert client.post("/api/eval/online", json={
        "agent_id": agent.id, "evaluators": ["Builtin.Helpfulness"],
        "session_timeout_minutes": 2000,
    }).status_code == 422
    assert client.post("/api/eval/online", json={
        "agent_id": agent.id, "evaluators": ["Builtin.Helpfulness"],
        "filters": [{"key": "k", "operator": "Equals", "value": {}}],
    }).status_code == 422
    db.close()


def test_create_cold_agent_without_a_log_group_is_no_telemetry(client, monkeypatch):
    db = SessionLocal()
    agent = make_agent(db)
    stub_environment(monkeypatch, log_groups=())
    res = client.post(
        "/api/eval/online", json={"agent_id": agent.id, "evaluators": ["Builtin.Helpfulness"]}
    )
    assert res.status_code == 400
    assert res.json()["code"] == "online_eval.no_telemetry"
    assert res.json()["detail"]["log_group"] == LOG_GROUP
    assert db.query(OnlineEvalConfig).count() == 0
    db.close()


def test_create_requires_an_active_agent(client, monkeypatch):
    stub_environment(monkeypatch)
    res = client.post("/api/eval/online", json={
        "agent_id": "nope", "evaluators": ["Builtin.Helpfulness"]})
    assert res.status_code == 400 and res.json()["code"] == "agent.not_active"


# ─── list / detail ──────────────────────────────────────────────────────────


def test_list_classifies_and_flags_duplicates(client, monkeypatch):
    db = SessionLocal()
    agent = make_agent(db)
    other = make_agent(db, name="other", resource_id="rt-2")
    ledger_row(db, agent, "oe_a-1", "oe_eval_agent_aaaaaa")
    ledger_row(db, agent, "oe_a-2", "oe_eval_agent_bbbbbb")
    ledger_row(db, other, "oe_b-1", "oe_other_cccccc", execution_status="DISABLED")
    ledger_row(db, agent, "exp_1-1", "exp_12345678_oe1", sampling_percentage=100.0)
    ledger_row(db, agent, "can_1-1", "can_12345678_oec", execution_status="DISABLED")

    res = client.get("/api/eval/online")
    assert res.status_code == 200, res.text
    rows = {c["config_id"]: c for c in res.json()["configs"]}
    assert res.json()["total"] == 5
    assert rows["oe_a-1"]["owner"] == "agent" and rows["oe_a-1"]["agent_id"] == agent.id
    assert rows["exp_1-1"]["owner"] == "experiment"
    assert rows["can_1-1"]["owner"] == "experiment"
    assert rows["oe_b-1"]["owner"] == "agent" and rows["oe_b-1"]["agent_id"] == other.id
    # both agent configs ENABLED on the same agent → flagged; others not
    assert rows["oe_a-1"]["duplicate_enabled"] and rows["oe_a-2"]["duplicate_enabled"]
    assert not rows["oe_b-1"]["duplicate_enabled"] and not rows["exp_1-1"]["duplicate_enabled"]
    assert all(c["detailed"] is True and c["matched_agent"] is None for c in rows.values())
    assert rows["oe_a-1"]["evaluators"] == ["Builtin.Helpfulness", "Builtin.Refusal"]
    assert rows["oe_a-1"]["results_log_group"] == online.results_log_group("oe_a-1")
    db.close()


def test_list_is_workspace_scoped(client):
    db = SessionLocal()
    agent = make_agent(db)
    ledger_row(db, agent, "oe_a-1")
    ledger_row(db, agent, "oe_other-ws", workspace_id="other-ws")
    rows = client.get("/api/eval/online").json()["configs"]
    assert [r["config_id"] for r in rows] == ["oe_a-1"]
    db.close()


def test_get_detail_and_404(client):
    db = SessionLocal()
    agent = make_agent(db)
    ledger_row(db, agent, "oe_a-1", filters=[{"key": "k", "operator": "Equals",
                                              "value": {"stringValue": "v"}}])
    body = client.get("/api/eval/online/oe_a-1").json()
    assert body["owner"] == "agent" and body["filters"][0]["key"] == "k"
    assert body["data_source"] == {"log_groups": [LOG_GROUP], "service_name": SERVICE}
    res = client.get("/api/eval/online/missing")
    assert res.status_code == 404 and res.json()["code"] == "online_eval.not_found"
    db.close()


# ─── patch / pause / resume / delete ────────────────────────────────────────


def test_patch_updates_the_row_and_keeps_untouched_fields(client, monkeypatch):
    db = SessionLocal()
    agent = make_agent(db)
    filters = [{"key": "session.id", "operator": "Contains", "value": {"stringValue": "vip"}}]
    ledger_row(db, agent, "oe_a-1", filters=filters, description="d")

    res = client.patch("/api/eval/online/oe_a-1", json={"sampling_percentage": 50})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["sampling_percentage"] == 50.0 and body["session_timeout_minutes"] == 3
    assert body["filters"] == filters and body["description"] == "d"

    res = client.patch("/api/eval/online/oe_a-1", json={
        "description": "new", "evaluators": ["Builtin.Helpfulness"], "filters": []})
    body = res.json()
    assert body["description"] == "new" and body["evaluators"] == ["Builtin.Helpfulness"]
    assert body["filters"] == [] and body["sampling_percentage"] == 50.0

    # a cleared description falls back to the auto text (never an empty string)
    body = client.patch("/api/eval/online/oe_a-1", json={"description": "  "}).json()
    assert body["description"] == "Launchpad online evaluation · eval-agent"

    assert client.patch("/api/eval/online/oe_a-1", json={}).status_code == 200
    db.expire_all()
    row = db.query(OnlineEvalConfig).one()
    assert row.sampling_percentage == 50.0 and row.evaluators == ["Builtin.Helpfulness"]
    assert row.filters == []
    db.close()


def test_patch_validates_evaluators_like_create(client, monkeypatch):
    db = SessionLocal()
    agent = make_agent(db)
    ledger_row(db, agent, "oe_a-1")
    res = client.patch("/api/eval/online/oe_a-1",
                       json={"evaluators": ["Builtin.TrajectoryInOrderMatch"]})
    assert res.status_code == 400 and res.json()["code"] == "online_eval.evaluator_unsupported"
    db.expire_all()
    assert db.query(OnlineEvalConfig).one().evaluators == ["Builtin.Helpfulness", "Builtin.Refusal"]
    db.close()


@pytest.mark.parametrize("extra", [
    {"agent_id": "other-agent"},          # protected: the config's agent is immutable
    {"mode": "insights"},                 # protected: mode is immutable
    {"config_id": "oe_z-9"},              # identity
    {"sampling_percentag": 50},           # a typo must not be a silent no-op
])
def test_patch_refuses_unknown_or_protected_keys(client, extra):
    """N13-obs21 (E12 NC-05, lead ruling 2026-10-18): `OnlineConfigPatch` is `extra="forbid"`
    — an unknown or protected key is 422 `validation.invalid_request` naming the key, and the
    row is untouched (today `agent_id` was silently ignored with 200)."""
    db = SessionLocal()
    agent = make_agent(db)
    ledger_row(db, agent, "oe_a-1", description="d")
    res = client.patch("/api/eval/online/oe_a-1", json={"sampling_percentage": 50, **extra})
    assert res.status_code == 422, res.text
    body = res.json()
    assert body["code"] == "validation.invalid_request"
    (key,) = extra
    assert any(err["loc"][-1] == key and err["type"] == "extra_forbidden"
               for err in body["detail"])
    db.expire_all()
    row = db.query(OnlineEvalConfig).one()
    assert row.sampling_percentage == 5.0 and row.agent_id == agent.id
    assert row.description == "d"
    db.close()


def test_patch_rejects_experiment_owned_configs(client):
    db = SessionLocal()
    agent = make_agent(db)
    ledger_row(db, agent, "exp_1-1", "exp_12345678_oe1")
    res = client.patch("/api/eval/online/exp_1-1", json={"sampling_percentage": 1})
    assert res.status_code == 403 and res.json()["code"] == "online_eval.read_only"
    db.expire_all()
    assert db.query(OnlineEvalConfig).one().sampling_percentage == 5.0
    db.close()


def test_pause_resume_owner_matrix_and_status_reset(client):
    db = SessionLocal()
    agent = make_agent(db)
    ledger_row(db, agent, "oe_a-1", status="FAILED", failure_reason="worker missing")
    ledger_row(db, agent, "exp_1-1", "exp_12345678_oe1")

    body = client.post("/api/eval/online/oe_a-1/pause").json()
    assert body["execution_status"] == "DISABLED"
    body = client.post("/api/eval/online/oe_a-1/resume").json()
    # resume gives a FAILED config another chance (the scheduler re-validates)
    assert body["execution_status"] == "ENABLED" and body["status"] == "ACTIVE"
    assert body["failure_reason"] is None
    res = client.post("/api/eval/online/exp_1-1/pause")
    assert res.status_code == 403 and res.json()["code"] == "online_eval.read_only"
    db.close()


def test_delete_owner_matrix_removes_row_and_samples_keeps_results(client):
    db = SessionLocal()
    agent = make_agent(db)
    ledger_row(db, agent, "oe_a-1")
    ledger_row(db, agent, "exp_1-1", "exp_12345678_oe1")
    db.add(OnlineSample(workspace_id=DEFAULT_WORKSPACE_ID, config_id="oe_a-1",
                        session_id="s1", status="evaluated"))
    db.commit()
    result_row(db, "oe_a-1", "s1", "Builtin.Helpfulness", 1.0)

    res = client.delete("/api/eval/online/oe_a-1")
    assert res.status_code == 200, res.text
    assert res.json() == {"config_id": "oe_a-1", "deleted": True,
                          "results_log_group": online.results_log_group("oe_a-1")}
    assert db.query(OnlineEvalConfig).filter_by(config_id="oe_a-1").count() == 0
    assert db.query(OnlineSample).count() == 0
    assert db.query(EvalResult).count() == 1  # results stay, like AWS kept the log group

    res = client.delete("/api/eval/online/exp_1-1")
    assert res.status_code == 403
    assert client.delete("/api/eval/online/missing").status_code == 404
    db.close()


# ─── create_for_owner / delete_for_owner / results_for_owner (E3 contract) ──


def test_create_for_owner_writes_an_experiment_row(monkeypatch):
    db = SessionLocal()
    agent = make_agent(db)
    row = online.create_for_owner(
        ws_ctx(), agent, name="exp_12345678_oe", evaluators=["Builtin.Helpfulness"],
        log_group=LOG_GROUP, service_name=SERVICE, sampling=100.0, timeout=2, db=db,
    )
    assert row.config_id.startswith("exp_12345678_oe-")
    assert row.execution_status == "ENABLED" and row.mode == "scores"
    assert row.sampling_percentage == 100.0 and row.session_timeout_minutes == 2
    assert online._normalize(row)["owner"] == "experiment"
    # a second call for the same owner name is idempotent (returns the existing row)
    again = online.create_for_owner(
        ws_ctx(), agent, name="exp_12345678_oe", evaluators=["Builtin.Helpfulness"],
        log_group=LOG_GROUP, service_name=SERVICE, sampling=100.0, timeout=2, db=db,
    )
    assert again.config_id == row.config_id
    assert db.query(OnlineEvalConfig).count() == 1
    with pytest.raises(ValueError):
        online.create_for_owner(ws_ctx(), agent, name="oe_not_an_owner",
                                evaluators=None, log_group=LOG_GROUP, service_name=SERVICE,
                                sampling=100.0, timeout=2, db=db)
    db.close()


def test_delete_for_owner_removes_every_arm_config_and_its_samples():
    db = SessionLocal()
    agent = make_agent(db)
    ledger_row(db, agent, "can_1-1", "can_12345678_oec")
    ledger_row(db, agent, "can_1-2", "can_12345678_oet")
    ledger_row(db, agent, "oe_a-1")
    db.add(OnlineSample(workspace_id=DEFAULT_WORKSPACE_ID, config_id="can_1-1",
                        session_id="s1", status="evaluated"))
    db.commit()
    deleted = online.delete_for_owner(ws_ctx(), "12345678abcd", db=db)
    assert sorted(deleted) == ["can_1-1", "can_1-2"]
    assert [r.config_id for r in db.query(OnlineEvalConfig).all()] == ["oe_a-1"]
    assert db.query(OnlineSample).count() == 0
    db.close()


def test_results_for_owner_groups_scores_by_arm():
    db = SessionLocal()
    agent = make_agent(db)
    ledger_row(db, agent, "exp_1-1", "exp_12345678_oe")
    now = datetime.now(UTC)
    result_row(db, "exp_1-1", "s-c1", "Builtin.Helpfulness", 0.4, at=now)
    result_row(db, "exp_1-1", "s-c2", "Builtin.Helpfulness", 0.6, at=now)
    result_row(db, "exp_1-1", "s-t1", "Builtin.Helpfulness", 0.9, at=now)
    result_row(db, "exp_1-1", "s-t1", "Builtin.Refusal", 0.0, at=now)
    result_row(db, "exp_1-1", "s-old", "Builtin.Helpfulness", 0.1, at=now - timedelta(days=2))
    result_row(db, "exp_1-1", "s-err", "Builtin.Helpfulness", None, at=now, error="boom")
    arm_map = {"s-c1": "C", "s-c2": "C", "s-t1": "T1", "s-old": "C", "s-err": "T1"}
    out = online.results_for_owner(ws_ctx(), "12345678abcd", now - timedelta(hours=1), arm_map,
                                   db=db)
    assert out["configs"] == ["exp_1-1"]
    assert out["by_evaluator"]["Builtin.Helpfulness"] == {"C": [0.4, 0.6], "T1": [0.9]}
    assert out["by_evaluator"]["Builtin.Refusal"] == {"C": [], "T1": [0.0]}
    assert out["sessions"] == {"C": 2, "T1": 1}
    assert out["records"] == 4 and out["unassigned"] == 0
    db.close()


# ─── results (aggregated over eval_results) ─────────────────────────────────


def test_results_aggregate_eval_results_rows(client):
    db = SessionLocal()
    agent = make_agent(db)
    ledger_row(db, agent, "oe_a-1")
    now = datetime.now(UTC)
    t1, t2 = now - timedelta(hours=2), now - timedelta(hours=1)
    result_row(db, "oe_a-1", "s-1", "Builtin.Helpfulness", 1.0, label="Yes", at=t1, trace_id="t1")
    result_row(db, "oe_a-1", "s-2", "Builtin.Helpfulness", 0.5, label="Partly", at=t2)
    result_row(db, "oe_a-1", "s-3", "Builtin.Helpfulness", 0.75, label="Yes", at=t2)
    result_row(db, "oe_a-1", "s-1", "Builtin.GoalSuccessRate", 1.0, label="Yes", at=t1)
    result_row(db, "oe_a-1", "s-2", "Builtin.GoalSuccessRate", 0.0, label="No", at=t2)
    result_row(db, "oe_a-1", "s-4", "custom_judge-x", None, label=None, at=t2, error="boom")
    result_row(db, "oe_a-1", "s-9", "Builtin.Helpfulness", 0.0, at=now - timedelta(days=3))
    result_row(db, "oe_other", "s-9", "Builtin.Helpfulness", 0.0, at=t2)

    res = client.get("/api/eval/online/oe_a-1/results?range=6h")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["range"] == "6h" and body["log_group"] == online.results_log_group("oe_a-1")
    assert body["evaluators"] == [
        {"evaluator_id": "Builtin.GoalSuccessRate", "level": "TRACE", "mean": 0.5,
         "count": 2, "sessions": 2, "labels": {"Yes": 1, "No": 1}},
        {"evaluator_id": "Builtin.Helpfulness", "level": "TRACE", "mean": 0.75,
         "count": 3, "sessions": 3, "labels": {"Yes": 2, "Partly": 1}},
    ]
    series = body["series"]["Builtin.Helpfulness"]
    assert [p["count"] for p in series] == [1, 2]
    assert [p["mean"] for p in series] == [1.0, 0.625]  # (0.5 + 0.75) / 2 in the t2 bucket
    # Logs Insights bin() buckets are UTC; the wire carries them at +08:00 (N17-obs463)
    assert all(re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?\+08:00$", p["bucket"])
               for p in series)
    recent = body["recent"]
    assert len(recent) == 6 and recent[0]["time"] >= recent[-1]["time"]
    errored = next(r for r in recent if r["evaluator_id"] == "custom_judge-x")
    assert errored["error"] == "RuntimeError: boom" and errored["score"] is None
    scored = next(r for r in recent if r["session_id"] == "s-1" and r["trace_id"] == "t1")
    assert scored["score"] == 1.0 and scored["error"] is None and scored["label"] == "Yes"
    assert body["errors"] == {"count": 1, "first_message": "boom"}
    db.close()


def test_results_empty_and_bad_range(client):
    db = SessionLocal()
    agent = make_agent(db)
    ledger_row(db, agent, "oe_a-1")
    body = client.get("/api/eval/online/oe_a-1/results").json()
    assert body["range"] == "24h"
    assert body["evaluators"] == [] and body["series"] == {} and body["recent"] == []
    assert body["errors"] == {"count": 0, "first_message": None}
    assert client.get("/api/eval/online/oe_a-1/results?range=1y").status_code == 422
    db.close()


def test_series_bucket_width_follows_the_range():
    assert online.bucket_seconds("1h") == 300 and online.bucket_seconds("6h") == 900
    assert online.bucket_seconds("24h") == 3600 and online.bucket_seconds("7d") == 6 * 3600
    t = datetime(2026, 9, 2, 10, 37, 12, tzinfo=UTC)
    assert online.bucket_label(t, "24h") == "2026-09-02 10:00:00.000"
    assert online.bucket_label(t, "1h") == "2026-09-02 10:35:00.000"
