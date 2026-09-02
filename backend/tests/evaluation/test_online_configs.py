"""Per-agent online evaluation configs — lifecycle, owner matrix, results parsing.

Stubbed control client; asserts the boto3 payload shapes AWS is picky about
(complete ``rule`` on every Update — AWS replaces it wholesale) and the
owner-based permission matrix (agent / experiment / external).
"""

from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

import app.evaluation.service as svc
from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.core.errors import AppError
from app.evaluation import online
from app.evaluation.models import OnlineEvalConfig
from app.models.ledger import Agent
from tests.conftest import set_default_resources

ROLE = "arn:aws:iam::111122223333:role/launchpad-agent-execution-role"
LOG_GROUP = "/aws/bedrock-agentcore/runtimes/rt-1-DEFAULT"
SERVICE = "eval_agent_abc123.DEFAULT"
T0 = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)


class ConflictException(Exception):
    pass


class ValidationException(Exception):
    pass


class ResourceNotFoundException(Exception):
    pass


def make_agent(db, name="eval-agent", method="zip_runtime", resource_id="rt-1") -> Agent:
    agent = Agent(
        workspace_id=DEFAULT_WORKSPACE_ID, name=name, method=method, status="active",
        arn=f"arn:aws:bedrock-agentcore:us-west-2:1:runtime/{resource_id}",
        resource_id=resource_id, spec={"name": name},
    )
    db.add(agent)
    db.commit()
    return agent


def aws_detail(config_id, name, *, enabled=True, log_group=LOG_GROUP, filters=None,
               evaluators=("Builtin.Helpfulness", "Builtin.Refusal"), status="ACTIVE"):
    rule = {
        "samplingConfig": {"samplingPercentage": 5.0},
        "sessionConfig": {"sessionTimeoutMinutes": 3},
    }
    if filters is not None:
        rule["filters"] = filters
    return {
        "onlineEvaluationConfigId": config_id,
        "onlineEvaluationConfigArn": (
            f"arn:aws:bedrock-agentcore:us-west-2:1:online-evaluation-config/{config_id}"
        ),
        "onlineEvaluationConfigName": name,
        "description": "d",
        "status": status,
        "executionStatus": "ENABLED" if enabled else "DISABLED",
        "rule": rule,
        "dataSourceConfig": {
            "cloudWatchLogs": {"logGroupNames": [log_group], "serviceNames": [SERVICE]}
        },
        "evaluators": [{"evaluatorId": e} for e in evaluators],
        "outputConfig": {"cloudWatchConfig": {
            "logGroupName": f"/aws/bedrock-agentcore/evaluations/results/{config_id}"}},
        "evaluationExecutionRoleArn": ROLE,
        "createdAt": T0,
        "updatedAt": T0,
    }


def summary_of(detail):
    keys = ("onlineEvaluationConfigId", "onlineEvaluationConfigArn",
            "onlineEvaluationConfigName", "description", "status", "executionStatus",
            "createdAt", "updatedAt")
    return {k: detail[k] for k in keys}


def stub_environment(monkeypatch, details=None):
    """Control stub whose Get/List serve ``details`` (config_id → Get payload)."""
    details = dict(details or {})
    control = MagicMock()
    control.list_online_evaluation_configs.return_value = {
        "onlineEvaluationConfigs": [summary_of(d) for d in details.values()]
    }

    def get(onlineEvaluationConfigId):  # noqa: N803 — boto3 shape
        if onlineEvaluationConfigId not in details:
            raise ResourceNotFoundException("gone")
        return details[onlineEvaluationConfigId]

    control.get_online_evaluation_config.side_effect = get
    control.create_online_evaluation_config.return_value = {
        "onlineEvaluationConfigId": "oe_new-AbCdEf1234",
        "onlineEvaluationConfigArn": "arn:oe_new",
        "status": "CREATING",
        "executionStatus": "ENABLED",
        "createdAt": T0,
        "outputConfig": {"cloudWatchConfig": {
            "logGroupName": "/aws/bedrock-agentcore/evaluations/results/oe_new-AbCdEf1234"}},
    }
    monkeypatch.setattr(online, "control_client", lambda _ctx: control)
    # resolve_telemetry (real) needs GetAgentRuntime for the service name
    monkeypatch.setattr(svc, "control_client", lambda _ws=None: MagicMock())
    monkeypatch.setattr(
        svc.rt, "get_runtime", lambda client, rid: {"agentRuntimeName": "eval_agent_abc123"}
    )
    set_default_resources({"execution_role_arn": ROLE})
    return control, details


def ledger_row(db, agent, config_id, name="oe_eval_agent_aaaaaa"):
    row = OnlineEvalConfig(
        workspace_id=DEFAULT_WORKSPACE_ID, agent_id=agent.id, agent_name=agent.name,
        config_id=config_id, config_arn=f"arn:{config_id}", name=name,
        service_name=SERVICE, log_group=LOG_GROUP,
    )
    db.add(row)
    db.commit()
    return row


# ─── pure helpers ───────────────────────────────────────────────────────────


def test_generate_name_matches_aws_regex():
    for raw in ("HR Assistant 財報", "a" * 80, "---", ""):
        name = online.generate_name(raw, suffix="abc123")
        assert online.NAME_RE.match(name), name
    assert online.generate_name("hr-assistant", suffix="abc123") == "oe_hr_assistant_abc123"


def test_owner_classification():
    assert online.owner_of("exp_1234abcd_oe1", "x", set()) == "experiment"
    assert online.owner_of("can_1234abcd_oec", "x", set()) == "experiment"
    assert online.owner_of("exp_1234abcd_oe1", "x", {"x"}) == "agent"  # ledger wins
    assert online.owner_of("quick_start", "y", set()) == "external"


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


# ─── create (AC1–AC3) ───────────────────────────────────────────────────────


def test_create_payload_and_ledger_row(client, monkeypatch):
    db = SessionLocal()
    agent = make_agent(db)
    control, _ = stub_environment(monkeypatch)

    res = client.post("/api/eval/online", json={
        "agent_id": agent.id,
        "evaluators": ["Builtin.Helpfulness", "Builtin.Refusal"],
        "sampling_percentage": 25,
        "session_timeout_minutes": 5,
        "filters": [{"key": "session.id", "operator": "Contains",
                     "value": {"stringValue": "vip"}}],
        "enable_on_create": False,
    })
    assert res.status_code == 201, res.text
    body = res.json()
    kwargs = control.create_online_evaluation_config.call_args.kwargs
    assert kwargs["dataSourceConfig"] == {
        "cloudWatchLogs": {"logGroupNames": [LOG_GROUP], "serviceNames": [SERVICE]}
    }
    assert kwargs["evaluators"] == [
        {"evaluatorId": "Builtin.Helpfulness"}, {"evaluatorId": "Builtin.Refusal"}
    ]
    assert kwargs["rule"] == {
        "samplingConfig": {"samplingPercentage": 25.0},
        "sessionConfig": {"sessionTimeoutMinutes": 5},
        "filters": [{"key": "session.id", "operator": "Contains", "value": {"stringValue": "vip"}}],
    }
    assert kwargs["evaluationExecutionRoleArn"] == ROLE
    assert kwargs["enableOnCreate"] is False
    assert online.NAME_RE.match(kwargs["onlineEvaluationConfigName"])
    assert kwargs["clientToken"]

    assert body["owner"] == "agent"
    assert body["agent_id"] == agent.id
    assert body["status"] == "CREATING"
    assert body["results_log_group"].endswith("/oe_new-AbCdEf1234")
    assert body["filter_count"] == 1 and body["sampling_percentage"] == 25.0
    row = db.query(OnlineEvalConfig).one()
    assert row.config_id == "oe_new-AbCdEf1234"
    assert row.workspace_id == DEFAULT_WORKSPACE_ID
    assert row.log_group == LOG_GROUP and row.service_name == SERVICE


@pytest.mark.parametrize("evaluators, code", [
    (["Builtin.TrajectoryInOrderMatch"], "online_eval.evaluator_unsupported"),
    (["Builtin.NotAThing"], "online_eval.evaluator_unsupported"),
])
def test_create_rejects_unsupported_evaluators_without_aws_call(
    client, monkeypatch, evaluators, code
):
    db = SessionLocal()
    agent = make_agent(db)
    control, _ = stub_environment(monkeypatch)
    res = client.post("/api/eval/online", json={"agent_id": agent.id, "evaluators": evaluators})
    assert res.status_code == 400
    assert res.json()["code"] == code
    control.create_online_evaluation_config.assert_not_called()
    assert db.query(OnlineEvalConfig).count() == 0


def test_create_rejects_ground_truth_judge(client, monkeypatch):
    db = SessionLocal()
    agent = make_agent(db)
    control, _ = stub_environment(monkeypatch)
    control.get_evaluator.return_value = {
        "evaluatorConfig": {"llmAsAJudge": {"instructions": "Compare to {expected_response}"}}
    }
    res = client.post("/api/eval/online", json={"agent_id": agent.id, "evaluators": ["gt_judge-x"]})
    assert res.status_code == 400
    assert res.json()["code"] == "online_eval.evaluator_unsupported"
    assert res.json()["detail"]["placeholders"] == ["expected_response"]
    control.create_online_evaluation_config.assert_not_called()


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
    # exactly one typed filter value
    assert client.post("/api/eval/online", json={
        "agent_id": agent.id, "evaluators": ["Builtin.Helpfulness"],
        "filters": [{"key": "k", "operator": "Equals", "value": {}}],
    }).status_code == 422


def test_create_cold_agent_maps_log_group_validation(client, monkeypatch):
    db = SessionLocal()
    agent = make_agent(db)
    control, _ = stub_environment(monkeypatch)
    control.create_online_evaluation_config.side_effect = ValidationException(
        "An error occurred (ValidationException): One or more specified log groups do not exist"
    )
    res = client.post(
        "/api/eval/online", json={"agent_id": agent.id, "evaluators": ["Builtin.Helpfulness"]}
    )
    assert res.status_code == 400
    assert res.json()["code"] == "online_eval.no_telemetry"
    assert res.json()["detail"]["log_group"] == LOG_GROUP
    assert db.query(OnlineEvalConfig).count() == 0


def test_create_retries_once_on_name_conflict(client, monkeypatch):
    db = SessionLocal()
    agent = make_agent(db)
    control, _ = stub_environment(monkeypatch)
    ok = control.create_online_evaluation_config.return_value
    control.create_online_evaluation_config.side_effect = [ConflictException("same name"), ok]
    res = client.post(
        "/api/eval/online", json={"agent_id": agent.id, "evaluators": ["Builtin.Helpfulness"]}
    )
    assert res.status_code == 201, res.text
    names = [c.kwargs["onlineEvaluationConfigName"]
             for c in control.create_online_evaluation_config.call_args_list]
    assert len(names) == 2 and names[0] != names[1]

    control.create_online_evaluation_config.side_effect = ConflictException("same name")
    res = client.post(
        "/api/eval/online", json={"agent_id": agent.id, "evaluators": ["Builtin.Helpfulness"]}
    )
    assert res.status_code == 409
    assert res.json()["code"] == "online_eval.conflict"


def test_create_requires_active_agent_and_bootstrapped_workspace(client, monkeypatch):
    db = SessionLocal()
    agent = make_agent(db)
    stub_environment(monkeypatch)
    res = client.post("/api/eval/online", json={
        "agent_id": "nope", "evaluators": ["Builtin.Helpfulness"]})
    assert res.json()["code"] == "agent.not_active"
    set_default_resources({})
    res = client.post(
        "/api/eval/online", json={"agent_id": agent.id, "evaluators": ["Builtin.Helpfulness"]}
    )
    assert res.status_code == 400
    assert res.json()["code"] == "online_eval.workspace_not_bootstrapped"


# ─── list / detail (AC4) ────────────────────────────────────────────────────


def test_list_classifies_enriches_and_flags(client, monkeypatch):
    db = SessionLocal()
    agent = make_agent(db)
    other = make_agent(db, name="other", resource_id="rt-2")
    details = {
        "oe_a-1": aws_detail("oe_a-1", "oe_eval_agent_aaaaaa"),
        "oe_a-2": aws_detail("oe_a-2", "oe_eval_agent_bbbbbb"),
        "exp_1-1": aws_detail("exp_1-1", "exp_12345678_oe1"),
        "can_1-1": aws_detail("can_1-1", "can_12345678_oec", enabled=False),
        "ext-1": aws_detail("ext-1", "quick_start",
                            log_group="/aws/bedrock-agentcore/runtimes/rt-2-DEFAULT"),
        "ext-2": aws_detail("ext-2", "someone_else", log_group="/aws/foo", status="ERROR"),
    }
    control, _ = stub_environment(monkeypatch, details)
    ledger_row(db, agent, "oe_a-1", "oe_eval_agent_aaaaaa")
    ledger_row(db, agent, "oe_a-2", "oe_eval_agent_bbbbbb")

    res = client.get("/api/eval/online")
    assert res.status_code == 200, res.text
    rows = {c["config_id"]: c for c in res.json()["configs"]}
    assert res.json()["total"] == 6
    assert rows["oe_a-1"]["owner"] == "agent" and rows["oe_a-1"]["agent_id"] == agent.id
    assert rows["exp_1-1"]["owner"] == "experiment"
    assert rows["can_1-1"]["owner"] == "experiment"
    assert rows["ext-1"]["owner"] == "external"
    assert rows["ext-1"]["matched_agent"] == {"id": other.id, "name": "other"}
    assert rows["ext-2"]["matched_agent"] is None
    assert rows["ext-2"]["status"] == "ERROR"
    # both agent configs ENABLED on the same agent → flagged; others not
    assert rows["oe_a-1"]["duplicate_enabled"] and rows["oe_a-2"]["duplicate_enabled"]
    assert not rows["ext-1"]["duplicate_enabled"] and not rows["exp_1-1"]["duplicate_enabled"]
    # experiment rows are summary-only (no Get), everything else enriched
    fetched = {c.kwargs["onlineEvaluationConfigId"]
               for c in control.get_online_evaluation_config.call_args_list}
    assert fetched == {"oe_a-1", "oe_a-2", "ext-1", "ext-2"}
    assert rows["exp_1-1"]["detailed"] is False and rows["exp_1-1"]["evaluators"] == []
    assert rows["oe_a-1"]["detailed"] is True
    assert rows["oe_a-1"]["evaluators"] == ["Builtin.Helpfulness", "Builtin.Refusal"]
    assert rows["oe_a-1"]["results_log_group"] == (
        "/aws/bedrock-agentcore/evaluations/results/oe_a-1"
    )


def test_list_degrades_a_failing_get_to_its_summary(client, monkeypatch):
    details = {"ext-1": aws_detail("ext-1", "quick_start")}
    control, _ = stub_environment(monkeypatch, details)
    control.get_online_evaluation_config.side_effect = RuntimeError("throttled")
    rows = client.get("/api/eval/online").json()["configs"]
    assert rows[0]["config_id"] == "ext-1" and rows[0]["detailed"] is False
    assert rows[0]["status"] == "ACTIVE"


def test_get_detail_and_404(client, monkeypatch):
    db = SessionLocal()
    agent = make_agent(db)
    details = {"oe_a-1": aws_detail("oe_a-1", "oe_eval_agent_aaaaaa",
                                    filters=[{"key": "k", "operator": "Equals",
                                              "value": {"stringValue": "v"}}])}
    stub_environment(monkeypatch, details)
    ledger_row(db, agent, "oe_a-1")
    body = client.get("/api/eval/online/oe_a-1").json()
    assert body["owner"] == "agent" and body["filters"][0]["key"] == "k"
    assert body["data_source"] == {"log_groups": [LOG_GROUP], "service_name": SERVICE}
    assert body["execution_role_arn"] == ROLE
    res = client.get("/api/eval/online/missing")
    assert res.status_code == 404 and res.json()["code"] == "online_eval.not_found"


# ─── patch / pause / resume / delete (AC5–AC6) ──────────────────────────────


def test_patch_sends_complete_rule_and_keeps_filters(client, monkeypatch):
    db = SessionLocal()
    agent = make_agent(db)
    filters = [{"key": "session.id", "operator": "Contains", "value": {"stringValue": "vip"}}]
    details = {"oe_a-1": aws_detail("oe_a-1", "oe_eval_agent_aaaaaa", filters=filters)}
    control, _ = stub_environment(monkeypatch, details)
    ledger_row(db, agent, "oe_a-1")

    res = client.patch("/api/eval/online/oe_a-1", json={"sampling_percentage": 50})
    assert res.status_code == 200, res.text
    kwargs = control.update_online_evaluation_config.call_args.kwargs
    assert kwargs["onlineEvaluationConfigId"] == "oe_a-1" and kwargs["clientToken"]
    # sampling changed, timeout + filters carried over from the current config —
    # AWS replaces `rule` wholesale, so a partial rule would have dropped them
    assert kwargs["rule"] == {
        "samplingConfig": {"samplingPercentage": 50.0},
        "sessionConfig": {"sessionTimeoutMinutes": 3},
        "filters": filters,
    }
    assert "evaluators" not in kwargs and "description" not in kwargs

    control.update_online_evaluation_config.reset_mock()
    res = client.patch("/api/eval/online/oe_a-1", json={
        "description": "new", "evaluators": ["Builtin.Helpfulness"], "filters": []})
    kwargs = control.update_online_evaluation_config.call_args.kwargs
    assert kwargs["description"] == "new"
    assert kwargs["evaluators"] == [{"evaluatorId": "Builtin.Helpfulness"}]
    assert kwargs["rule"] == {  # explicit empty filters clears them
        "samplingConfig": {"samplingPercentage": 5.0},
        "sessionConfig": {"sessionTimeoutMinutes": 3},
    }

    # AWS description is min 1 char: clearing it must fall back to the auto text,
    # not forward "" (botocore ParamValidationError → 500)
    control.update_online_evaluation_config.reset_mock()
    assert client.patch("/api/eval/online/oe_a-1", json={"description": "  "}).status_code == 200
    desc = control.update_online_evaluation_config.call_args.kwargs["description"]
    assert desc == "Launchpad online evaluation · eval-agent"

    control.update_online_evaluation_config.reset_mock()
    assert client.patch("/api/eval/online/oe_a-1", json={}).status_code == 200
    control.update_online_evaluation_config.assert_not_called()


def test_patch_rejects_non_agent_owners(client, monkeypatch):
    details = {
        "exp_1-1": aws_detail("exp_1-1", "exp_12345678_oe1"),
        "ext-1": aws_detail("ext-1", "quick_start"),
    }
    control, _ = stub_environment(monkeypatch, details)
    for cid in ("exp_1-1", "ext-1"):
        res = client.patch(f"/api/eval/online/{cid}", json={"sampling_percentage": 1})
        assert res.status_code == 403 and res.json()["code"] == "online_eval.read_only"
    control.update_online_evaluation_config.assert_not_called()


def test_pause_resume_owner_matrix(client, monkeypatch):
    db = SessionLocal()
    agent = make_agent(db)
    details = {
        "oe_a-1": aws_detail("oe_a-1", "oe_eval_agent_aaaaaa"),
        "exp_1-1": aws_detail("exp_1-1", "exp_12345678_oe1"),
        "ext-1": aws_detail("ext-1", "quick_start", enabled=False),
    }
    control, _ = stub_environment(monkeypatch, details)
    ledger_row(db, agent, "oe_a-1")

    assert client.post("/api/eval/online/oe_a-1/pause").status_code == 200
    kwargs = control.update_online_evaluation_config.call_args.kwargs
    assert kwargs["executionStatus"] == "DISABLED" and "rule" not in kwargs
    assert client.post("/api/eval/online/ext-1/resume").status_code == 200
    assert control.update_online_evaluation_config.call_args.kwargs["executionStatus"] == "ENABLED"
    res = client.post("/api/eval/online/exp_1-1/pause")
    assert res.status_code == 403 and res.json()["code"] == "online_eval.read_only"
    assert control.update_online_evaluation_config.call_count == 2


def test_delete_owner_matrix_and_ledger(client, monkeypatch):
    db = SessionLocal()
    agent = make_agent(db)
    details = {
        "oe_a-1": aws_detail("oe_a-1", "oe_eval_agent_aaaaaa"),
        "exp_1-1": aws_detail("exp_1-1", "exp_12345678_oe1"),
        "ext-1": aws_detail("ext-1", "quick_start"),
    }
    control, _ = stub_environment(monkeypatch, details)
    ledger_row(db, agent, "oe_a-1")

    res = client.delete("/api/eval/online/oe_a-1")
    assert res.status_code == 200, res.text
    assert res.json()["results_log_group"] == "/aws/bedrock-agentcore/evaluations/results/oe_a-1"
    control.delete_online_evaluation_config.assert_called_with(onlineEvaluationConfigId="oe_a-1")
    assert db.query(OnlineEvalConfig).count() == 0

    assert client.delete("/api/eval/online/ext-1").status_code == 200
    res = client.delete("/api/eval/online/exp_1-1")
    assert res.status_code == 403
    assert control.delete_online_evaluation_config.call_count == 2
    assert client.delete("/api/eval/online/missing").status_code == 404


def test_delete_reconciles_ledger_when_aws_already_lost_it(client, monkeypatch):
    db = SessionLocal()
    agent = make_agent(db)
    control, _ = stub_environment(monkeypatch, {})
    ledger_row(db, agent, "oe_gone")
    res = client.delete("/api/eval/online/oe_gone")
    assert res.status_code == 200
    control.delete_online_evaluation_config.assert_not_called()
    assert db.query(OnlineEvalConfig).count() == 0


# ─── results (AC7) ──────────────────────────────────────────────────────────


def test_results_parse_rows(client, monkeypatch):
    seen = {}

    def fake_run(queries, hours, log_groups=None, workspace=None):
        seen["queries"] = queries
        seen["hours"] = hours
        seen["log_groups"] = log_groups
        return {
            "summary": [
                {"evaluator": "Builtin.Helpfulness", "level": "Trace", "mean": "0.75",
                 "count": "4", "sessions": "3"},
                {"evaluator": "Builtin.GoalSuccessRate", "level": "Session", "mean": "0.5",
                 "count": "2", "sessions": "2"},
            ],
            "labels": [
                {"evaluator": "Builtin.GoalSuccessRate", "label": "Yes", "count": "1"},
                {"evaluator": "Builtin.GoalSuccessRate", "label": "No", "count": "1"},
            ],
            "series": [
                {"evaluator": "Builtin.Helpfulness", "bucket": "2026-09-02 10:00:00.000",
                 "mean": "1", "count": "2"},
                {"evaluator": "Builtin.Helpfulness", "bucket": "2026-09-02 11:00:00.000",
                 "mean": "0.5", "count": "2"},
            ],
            "recent": [
                {"time": "2026-09-02 11:05:00.000", "session_id": "s-1", "trace_id": "t-1",
                 "evaluator": "Builtin.Helpfulness", "level": "Trace", "score": "1.0",
                 "label": "Yes", "explanation": "helpful"},
                {"time": "2026-09-02 11:04:00.000", "session_id": "s-2",
                 "evaluator": "custom_judge-x", "error_type": "ValidationException",
                 "error_message": "boom"},
            ],
            "errors": [{"count": "1", "first_message": "boom"}],
        }

    monkeypatch.setattr(online, "run_insights_queries", fake_run)
    res = client.get("/api/eval/online/oe_a-1/results?range=6h")
    assert res.status_code == 200, res.text
    body = res.json()
    assert seen["hours"] == 6
    assert seen["log_groups"] == ["/aws/bedrock-agentcore/evaluations/results/oe_a-1"]
    assert set(seen["queries"]) == {"summary", "labels", "series", "recent", "errors"}
    assert "bin(15m)" in seen["queries"]["series"]
    assert body["range"] == "6h"
    assert body["evaluators"] == [
        {"evaluator_id": "Builtin.GoalSuccessRate", "level": "Session", "mean": 0.5,
         "count": 2, "sessions": 2, "labels": {"Yes": 1, "No": 1}},
        {"evaluator_id": "Builtin.Helpfulness", "level": "Trace", "mean": 0.75,
         "count": 4, "sessions": 3, "labels": {}},
    ]
    assert [p["mean"] for p in body["series"]["Builtin.Helpfulness"]] == [1.0, 0.5]
    assert body["recent"][0]["score"] == 1.0 and body["recent"][0]["error"] is None
    assert body["recent"][1]["error"] == "ValidationException: boom"
    assert body["errors"] == {"count": 1, "first_message": "boom"}


def test_results_empty_when_log_group_missing(client, monkeypatch):
    monkeypatch.setattr(
        online, "run_insights_queries",
        lambda queries, hours, log_groups=None, workspace=None: {k: [] for k in queries},
    )
    body = client.get("/api/eval/online/oe_a-1/results").json()
    assert body["range"] == "24h"
    assert body["evaluators"] == [] and body["series"] == {} and body["recent"] == []
    assert body["errors"] == {"count": 0, "first_message": None}
    assert client.get("/api/eval/online/oe_a-1/results?range=1y").status_code == 422
