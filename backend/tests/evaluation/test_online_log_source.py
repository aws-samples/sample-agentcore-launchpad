"""N17-obs337 (audit item 13 / 7.5) — an online config can watch a CloudWatch log source.

Global's online data source is ``cloudWatchLogs {logGroupNames (≤ 10), serviceNames}``
("For agents running outside AgentCore Runtime, service name is configured in
OTEL_RESOURCE_ATTRIBUTES"). Ours required ``agent_id`` and held one log group, so a BYOC or
external agent could not be monitored online. ``POST /api/eval/online`` now takes exactly one
of ``agent_id`` or ``log_source {service_name, log_group_names[1..10]}`` (the batch run's
shape); the row keeps every group and the scheduler scans / the job reads all of them.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import create_engine, inspect, text

from app.core.db import SessionLocal, _migrate
from app.evaluation import online
from app.evaluation import online_scheduler as sched
from app.evaluation.models import OnlineEvalConfig
from tests.evaluation.test_online_configs import (
    CONFIG_ID_RE,
    LOG_GROUP,
    ledger_row,
    make_agent,
    stub_environment,
)
from tests.evaluation.test_online_scheduler import NOW, harness, scan_row

SERVICE = "langgraph-travel"
GROUPS = ["/byoc/travel/app", "/byoc/travel/worker"]


def _source(**overrides):
    return {"service_name": SERVICE, "log_group_names": list(GROUPS), **overrides}


def test_create_takes_a_log_source_instead_of_an_agent(client, monkeypatch):
    logs = stub_environment(monkeypatch, log_groups=GROUPS)
    res = client.post("/api/eval/online", json={
        "log_source": _source(), "evaluators": ["Builtin.Helpfulness"],
        "session_timeout_minutes": 10, "filters": []})
    assert res.status_code == 201, res.text
    body = res.json()
    assert CONFIG_ID_RE.match(body["config_id"]), body["config_id"]
    assert body["data_source"] == {"log_groups": GROUPS, "service_name": SERVICE}
    assert body["agent_id"] is None and body["agent_name"] == SERVICE
    assert body["owner"] == "agent" and body["execution_status"] == "ENABLED"
    assert {c["logGroupNamePrefix"] for c in logs.calls} == set(GROUPS)
    with SessionLocal() as db:
        row = db.query(OnlineEvalConfig).one()
        assert row.log_groups == GROUPS and row.log_group == GROUPS[0]
        assert row.service_name == SERVICE


@pytest.mark.parametrize("payload", [
    {},                                                         # no target
    {"agent_id": "a-1", "log_source": _source()},               # both targets
    {"log_source": _source(log_group_names=[])},                # 1..10 groups
    {"log_source": _source(log_group_names=[f"/g/{i}" for i in range(11)])},
    {"log_source": _source(log_group_names=["bad name"])},      # the batch name rule
    {"log_source": _source(service_name="has space")},
])
def test_create_needs_exactly_one_valid_target(client, monkeypatch, payload):
    stub_environment(monkeypatch, log_groups=GROUPS)
    res = client.post("/api/eval/online", json={
        **payload, "evaluators": ["Builtin.Helpfulness"], "filters": []})
    assert res.status_code == 422, res.text


def test_a_missing_log_group_is_refused_before_any_row(client, monkeypatch):
    stub_environment(monkeypatch, log_groups=GROUPS[:1])
    res = client.post("/api/eval/online", json={
        "log_source": _source(), "evaluators": ["Builtin.Helpfulness"], "filters": []})
    assert res.status_code == 400 and res.json()["code"] == "online_eval.no_telemetry"
    assert res.json()["detail"]["log_groups"] == GROUPS[1:]
    with SessionLocal() as db:
        assert db.query(OnlineEvalConfig).count() == 0


def test_the_scheduler_scans_every_group_and_the_job_reads_them():
    db = SessionLocal()
    row = ledger_row(db, make_agent(db), "oe_b-1", agent_id="", agent_name=SERVICE,
                     service_name=SERVICE, log_group=GROUPS[0], log_groups=list(GROUPS),
                     sampling_percentage=100.0, session_timeout_minutes=5)
    executor, queries, kwargs = harness(rows=[scan_row("session-byoc-1",
                                                       NOW - timedelta(minutes=20))])
    report = sched.tick(db, NOW, **kwargs)
    assert report.sampled == 1 and len(executor.submitted) == 1
    assert queries.seen[0]["log_groups"] == [*GROUPS, "aws/spans"]
    assert f'"{SERVICE}"' in queries.seen[0]["queries"]["scan"]
    spec = executor.submitted[0].spec
    assert spec["log_groups"] == [*GROUPS, "aws/spans"] and spec["service_name"] == SERVICE
    assert online._normalize(row)["data_source"]["log_groups"] == GROUPS
    db.close()


def test_an_agent_config_still_reads_its_one_runtime_group():
    db = SessionLocal()
    row = ledger_row(db, make_agent(db), "oe_b-2")
    assert online._normalize(row)["data_source"]["log_groups"] == [LOG_GROUP]
    assert sched.scan_log_groups(row, sched.get_settings()) == [LOG_GROUP, "aws/spans"]
    db.close()


def test_two_log_source_configs_on_different_services_are_not_duplicates(client, monkeypatch):
    stub_environment(monkeypatch, log_groups=GROUPS)
    for service in ("svc-a", "svc-b", "svc-b"):
        assert client.post("/api/eval/online", json={
            "log_source": _source(service_name=service), "evaluators": ["Builtin.Helpfulness"],
            "filters": []}).status_code == 201
    listed = client.get("/api/eval/online").json()["configs"]
    flags = sorted((c["data_source"]["service_name"], c["duplicate_enabled"]) for c in listed)
    assert flags == [("svc-a", False), ("svc-b", True), ("svc-b", True)]


def test_the_ledger_migration_adds_the_log_groups_column(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path}/old.db")
    with eng.begin() as conn:
        conn.execute(text(
            "CREATE TABLE online_eval_configs (id VARCHAR(16) PRIMARY KEY, "
            "agent_id VARCHAR(32), agent_name VARCHAR(64), config_id VARCHAR(80), "
            "config_arn VARCHAR(256), name VARCHAR(48), service_name VARCHAR(128), "
            "log_group VARCHAR(256), created_at DATETIME)"))
    _migrate(eng)
    assert "log_groups" in {c["name"] for c in inspect(eng).get_columns("online_eval_configs")}
    _migrate(eng)  # idempotent
