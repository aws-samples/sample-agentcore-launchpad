"""D20 P1-d — the CloudWatch decision audit sink (`services/policy_audit.py`, plan §2.3(c)):
event shape (no secrets), batching, the background flusher, fail-soft on every CloudWatch
failure, the bounded queue, `record_decision` → sink mirroring, prune-on-record, the
`runtime_channel` state machine, the bootstrap log-group helper and the IAM grants."""

from __future__ import annotations

import json
import logging
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError

from app.core.db import SessionLocal
from app.models.policy import PolicyDecision
from app.services import policy_audit, policy_store

WS = "default"
ROOT = Path(__file__).resolve().parents[2]
FORBIDDEN_KEYS = {"prompt", "input", "body", "message", "api_key", "token", "secret",
                  "authorization", "password", "statement", "arguments"}


class StubLogs:
    """CloudWatch Logs stand-in: records streams/puts; `fail_with` makes the next put raise
    that ClientError code (or the exception instance given); `groups` drives describe."""

    def __init__(self, *, groups: list[str] | None = None) -> None:
        self.streams: list[tuple[str, str]] = []
        self.puts: list[dict] = []
        self.created_groups: list[str] = []
        self.retention: dict[str, int] = {}
        self.groups = list(groups or [])
        self.fail_with: str | BaseException | None = None

    def create_log_stream(self, logGroupName, logStreamName):  # noqa: N803
        key = (logGroupName, logStreamName)
        if key in self.streams:
            raise ClientError({"Error": {"Code": "ResourceAlreadyExistsException",
                                         "Message": "x"}}, "CreateLogStream")
        self.streams.append(key)

    def put_log_events(self, **kwargs):
        if self.fail_with is not None:
            fail, self.fail_with = self.fail_with, None
            if isinstance(fail, BaseException):
                raise fail
            raise ClientError({"Error": {"Code": fail, "Message": fail}}, "PutLogEvents")
        self.puts.append(kwargs)
        return {}

    def describe_log_groups(self, logGroupNamePrefix, limit=50):  # noqa: N803
        return {"logGroups": [{"logGroupName": g} for g in self.groups
                              if g.startswith(logGroupNamePrefix)]}

    def create_log_group(self, logGroupName):  # noqa: N803
        if logGroupName in self.groups:
            raise ClientError({"Error": {"Code": "ResourceAlreadyExistsException",
                                         "Message": "x"}}, "CreateLogGroup")
        self.groups.append(logGroupName)
        self.created_groups.append(logGroupName)

    def put_retention_policy(self, logGroupName, retentionInDays):  # noqa: N803
        self.retention[logGroupName] = retentionInDays


def _row(**over) -> PolicyDecision:
    fields = dict(
        id="dec_0001", workspace_id=WS, request_id="req-1", source="hub", channel="chat",
        principal_kind="user", principal_id="u-river", principal_label="river",
        action="invoke", resource_kind="agent", resource_id="ag1", agent_id="ag1",
        session_id="s-1", decision="deny", mode="enforce", policy_ids=["pol_a"],
        policy_versions={"pol_a": 3}, bundle_version=None, reason="forbid pol_a:v3",
        latency_ms=1.25, created_at=datetime(2026, 10, 14, 2, 30, tzinfo=UTC),
    )
    fields.update(over)
    return PolicyDecision(**fields)


def _sink(logs: StubLogs, **kw) -> policy_audit.CloudWatchDecisionSink:
    kw.setdefault("autostart", False)
    kw.setdefault("hostname", "hub-host")
    kw.setdefault("clock", lambda: 1_792_000_000.0)  # 2026-10-14 UTC
    kw.setdefault("env", "")
    return policy_audit.CloudWatchDecisionSink(client_factory=lambda _ws: logs, **kw)


# ── event shape ────────────────────────────────────────────────────────────


def test_event_carries_exactly_the_contract_fields_and_no_secrets():
    event = policy_audit.event_from_row(_row(), env="t1")
    assert set(event) == policy_audit.EVENT_FIELDS
    assert event["schema"] == "launchpad.policy.decision/1"
    assert event["decision_id"] == "dec_0001" and event["request_id"] == "req-1"
    assert event["ts"] == "2026-10-14T02:30:00+00:00" and event["env"] == "t1"
    assert event["principal"] == {"kind": "user", "id": "u-river", "label": "river"}
    assert event["resource"] == {"kind": "agent", "id": "ag1"}
    assert event["decision"] == "deny" and event["mode"] == "enforce"
    assert event["policy_ids"] == ["pol_a"] and event["policy_versions"] == {"pol_a": 3}
    assert event["latency_ms"] == 1.25 and event["reason"] == "forbid pol_a:v3"
    # C4: nothing a prompt, tool input, key or body could ride in
    flat = json.dumps(event)
    for key in FORBIDDEN_KEYS:
        assert f'"{key}"' not in flat, key
    assert set(event["principal"]) == {"kind", "id", "label"}
    assert set(event["resource"]) == {"kind", "id"}


def test_event_truncates_a_runaway_reason_and_defaults_the_env_from_settings():
    event = policy_audit.event_from_row(_row(reason="x" * 5000))
    assert len(event["reason"]) == policy_audit.REASON_MAX_CHARS + 1
    assert event["env"] == ""  # Settings.env_name of the test process


def test_log_group_name_follows_the_environment(monkeypatch):
    from app.core import naming

    assert policy_audit.log_group_name() == "/launchpad/policy/decisions"
    assert policy_audit.log_group_name("t1") == "/launchpad/policy/decisions-t1"
    monkeypatch.setattr(naming, "current_env_name", lambda: "t2")
    assert policy_audit.log_group_name() == "/launchpad/policy/decisions-t2"


# ── batching + shipping ────────────────────────────────────────────────────


def test_sink_batches_by_size_sorts_by_timestamp_and_names_the_stream():
    logs = StubLogs()
    sink = _sink(logs, batch_size=3)
    base = datetime(2026, 10, 14, 2, 0, tzinfo=UTC)
    # published out of order inside a batch — CloudWatch wants chronological events
    for i in (2, 0, 1, 3, 4, 5, 6):
        row = _row(id=f"dec_{i}", created_at=base + timedelta(seconds=i))
        assert sink.publish(policy_audit.event_from_row(row, env="")) is True

    assert sink.flush() == 7
    assert [len(p["logEvents"]) for p in logs.puts] == [3, 3, 1]
    first = logs.puts[0]
    assert first["logGroupName"] == "/launchpad/policy/decisions"
    assert first["logStreamName"] == "hub-hub-host-20261014"
    assert logs.streams == [("/launchpad/policy/decisions", "hub-hub-host-20261014")]
    stamps = [e["timestamp"] for e in first["logEvents"]]
    assert stamps == sorted(stamps)
    messages = [json.loads(e["message"]) for e in first["logEvents"]]
    assert [m["decision_id"] for m in messages] == ["dec_0", "dec_1", "dec_2"]
    assert set(messages[0]) == policy_audit.EVENT_FIELDS
    assert sink.status()["status"] == "ready" and sink.stats["shipped"] == 7


def test_sink_ships_each_workspace_through_its_own_client():
    seen: dict[str, StubLogs] = {}

    def factory(ws: str) -> StubLogs:
        return seen.setdefault(ws, StubLogs())

    sink = policy_audit.CloudWatchDecisionSink(
        client_factory=factory, autostart=False, hostname="h", env="")
    sink.publish(policy_audit.event_from_row(_row(id="a", workspace_id="default"), env=""))
    sink.publish(policy_audit.event_from_row(_row(id="b", workspace_id="ws_spoke"), env=""))
    assert sink.flush() == 2
    assert set(seen) == {"default", "ws_spoke"}
    shipped = [json.loads(e["message"])["workspace_id"]
               for e in seen["ws_spoke"].puts[0]["logEvents"]]
    assert shipped == ["ws_spoke"]


def test_background_thread_ships_within_the_flush_interval_without_a_flush_call():
    logs = StubLogs()
    sink = _sink(logs, autostart=True, flush_s=0.05, clock=time.time)
    sink.publish(policy_audit.event_from_row(_row(), env=""))
    deadline = time.monotonic() + 3.0
    while not logs.puts and time.monotonic() < deadline:
        time.sleep(0.01)
    sink.stop()
    assert len(logs.puts) == 1 and sink.status()["status"] == "ready"


# ── fail-soft ──────────────────────────────────────────────────────────────


def test_a_missing_log_group_is_reported_not_raised_and_recovers(caplog):
    logs = StubLogs()
    sink = _sink(logs)
    sink.publish(policy_audit.event_from_row(_row(), env=""))
    logs.fail_with = "ResourceNotFoundException"
    with caplog.at_level(logging.WARNING, logger="launchpad.policy.audit"):
        assert sink.flush() == 0
    state = sink.status()
    assert state["status"] == "missing" and "make bootstrap" in state["reason"]
    assert state["failed"] == 1 and state["shipped"] == 0
    assert any("not mirrored" in r.message for r in caplog.records)
    # the next decision ships once the group exists → ready again
    sink.publish(policy_audit.event_from_row(_row(id="dec_2"), env=""))
    assert sink.flush() == 1 and sink.status()["status"] == "ready"


@pytest.mark.parametrize("failure", [
    "AccessDeniedException",
    EndpointConnectionError(endpoint_url="https://logs.example"),
    RuntimeError("boom"),
])
def test_every_other_failure_is_unknown_with_a_reason_and_never_raises(failure):
    logs = StubLogs()
    sink = _sink(logs)
    sink.publish(policy_audit.event_from_row(_row(), env=""))
    logs.fail_with = failure
    assert sink.flush() == 0
    state = sink.status()
    assert state["status"] == "unknown" and state["reason"] and state["failed"] == 1


def test_warnings_are_rate_limited_per_failure_code(caplog):
    now = {"t": 1_000.0}
    logs = StubLogs()
    sink = _sink(logs, clock=lambda: now["t"])
    with caplog.at_level(logging.WARNING, logger="launchpad.policy.audit"):
        for _ in range(3):
            sink.publish(policy_audit.event_from_row(_row(), env=""))
            logs.fail_with = "ThrottlingException"
            sink.flush()
        now["t"] += 61
        sink.publish(policy_audit.event_from_row(_row(), env=""))
        logs.fail_with = "ThrottlingException"
        sink.flush()
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 2


def test_a_full_queue_drops_the_event_and_returns_false(caplog):
    logs = StubLogs()
    sink = _sink(logs, max_queue=2)
    with caplog.at_level(logging.WARNING, logger="launchpad.policy.audit"):
        assert sink.publish({"decision_id": "a", "workspace_id": WS}) is True
        assert sink.publish({"decision_id": "b", "workspace_id": WS}) is True
        assert sink.publish({"decision_id": "c", "workspace_id": WS}) is False
    assert sink.stats["dropped"] == 1 and sink.stats["queued"] == 2
    assert any("queue full" in r.message for r in caplog.records)
    assert sink.flush() == 2


def test_publish_row_is_a_noop_without_a_sink_and_never_raises(caplog):
    policy_audit.install(None)
    assert policy_audit.publish_row(_row()) is False
    policy_audit.install(_sink(StubLogs()))
    with caplog.at_level(logging.ERROR, logger="launchpad.policy.audit"):
        assert policy_audit.publish_row(object()) is False  # not a row at all
    assert any("could not build" in r.message for r in caplog.records)


# ── runtime_channel ────────────────────────────────────────────────────────


def test_runtime_channel_reflects_the_sink_state_machine():
    policy_audit.install(None)
    assert policy_audit.runtime_channel() == {
        "status": "missing",
        "reason": "audit sink not installed (policy feature off or policy_audit_enabled=false)",
        "log_group": "/launchpad/policy/decisions",
    }
    logs = StubLogs()
    sink = _sink(logs)
    policy_audit.install(sink)
    assert policy_audit.runtime_channel()["status"] == "unknown"
    sink.publish(policy_audit.event_from_row(_row(), env=""))
    sink.flush()
    channel = policy_audit.runtime_channel()
    assert channel["status"] == "ready" and channel["log_group"] == "/launchpad/policy/decisions"
    sink.publish(policy_audit.event_from_row(_row(), env=""))
    logs.fail_with = "ResourceNotFoundException"
    sink.flush()
    assert policy_audit.runtime_channel()["status"] == "missing"
    # an explicit probe (bootstrap) decides from describe_log_groups
    assert sink.mark_probed(StubLogs(groups=["/launchpad/policy/decisions"])) == "ready"
    assert sink.mark_probed(StubLogs(groups=["/launchpad/policy/decisions-t1"])) == "missing"


def test_install_replaces_and_stops_the_previous_sink():
    first, second = _sink(StubLogs()), _sink(StubLogs())
    policy_audit.install(first)
    policy_audit.install(second)
    assert policy_audit.current() is second and first._stop.is_set()
    policy_audit.install(None)
    assert policy_audit.current() is None and second._stop.is_set()


# ── record_decision integration ────────────────────────────────────────────


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def _record(db, **over):
    kwargs = dict(source="hub", channel="v1", principal_kind="api_key", principal_id="key_1",
                  principal_label="ci-key", action="invoke", resource_kind="agent",
                  resource_id="ag1", decision="allow", mode="log_only", agent_id="ag1",
                  request_id="req-9", policy_ids=["pol_x"], policy_versions={"pol_x": 1},
                  latency_ms=0.4)
    kwargs.update(over)
    return policy_store.record_decision(db, WS, **kwargs)


def test_record_decision_mirrors_the_ledger_row_to_the_sink(db):
    logs = StubLogs()
    sink = _sink(logs)
    policy_audit.install(sink)
    row = _record(db)
    assert sink.stats["queued"] == 1
    assert sink.flush() == 1
    event = json.loads(logs.puts[0]["logEvents"][0]["message"])
    assert event["decision_id"] == row.id and event["request_id"] == "req-9"
    assert event["principal"] == {"kind": "api_key", "id": "key_1", "label": "ci-key"}
    assert event["channel"] == "v1" and event["decision"] == "allow"
    assert event["policy_versions"] == {"pol_x": 1} and event["workspace_id"] == WS
    assert set(event) == policy_audit.EVENT_FIELDS
    # the ledger row is the record regardless of the mirror
    assert db.get(PolicyDecision, row.id) is not None


def test_record_decision_with_commit_false_still_mirrors_and_a_dead_sink_never_breaks_it(db):
    logs = StubLogs()
    sink = _sink(logs)
    policy_audit.install(sink)
    row = _record(db, commit=False)
    db.commit()
    assert sink.flush() == 1
    assert json.loads(logs.puts[0]["logEvents"][0]["message"])["decision_id"] == row.id
    # CloudWatch down: the decision still returns, the row still exists
    logs.fail_with = "ServiceUnavailableException"
    row2 = _record(db)
    assert sink.flush() == 0 and db.get(PolicyDecision, row2.id) is not None


def test_record_decision_prunes_every_n_committed_records(db, monkeypatch):
    from app.core.config import get_settings

    policy_audit.install(None)
    monkeypatch.setattr(get_settings(), "policy_decisions_prune_every", 3)
    monkeypatch.setattr(get_settings(), "policy_decisions_keep_days", 30)
    policy_store.reset_record_counter()
    old = _row(id="dec_old", created_at=datetime.now(UTC) - timedelta(days=40))
    db.add(old)
    db.commit()
    _record(db)
    _record(db, commit=False)  # a caller's own transaction never triggers housekeeping
    db.commit()
    _record(db)
    assert db.get(PolicyDecision, "dec_old") is not None  # 2 committed records so far
    _record(db)  # the 3rd committed record prunes
    assert db.get(PolicyDecision, "dec_old") is None
    assert db.query(PolicyDecision).count() == 4


def test_prune_on_record_can_be_disabled(db, monkeypatch):
    from app.core.config import get_settings

    policy_audit.install(None)
    monkeypatch.setattr(get_settings(), "policy_decisions_prune_every", 0)
    policy_store.reset_record_counter()
    db.add(_row(id="dec_old", created_at=datetime.now(UTC) - timedelta(days=40)))
    db.commit()
    for _ in range(5):
        _record(db)
    assert db.get(PolicyDecision, "dec_old") is not None


# ── bootstrap helper + grants ──────────────────────────────────────────────


def test_ensure_decision_log_group_creates_once_sets_retention_and_marks_the_sink_ready():
    logs = StubLogs()
    sink = _sink(StubLogs())
    policy_audit.install(sink)
    lines: list[str] = []
    out = policy_audit.ensure_decision_log_group(logs, retention_days=90, log=lines.append)
    assert out == {"log_group": "/launchpad/policy/decisions", "created": True,
                   "retention_days": 90}
    assert logs.created_groups == ["/launchpad/policy/decisions"]
    assert logs.retention == {"/launchpad/policy/decisions": 90}
    assert lines == ["policy decisions log group /launchpad/policy/decisions created · "
                     "retention 90 d"]
    assert policy_audit.runtime_channel()["status"] == "ready"
    # second run: exists, retention re-applied (idempotent)
    out = policy_audit.ensure_decision_log_group(logs, retention_days=30, log=lines.append)
    assert out["created"] is False and logs.retention["/launchpad/policy/decisions"] == 30
    assert "exists" in lines[-1]
    # retention 0 → unbounded, no put
    logs2 = StubLogs()
    policy_audit.ensure_decision_log_group(logs2, retention_days=0, log=lines.append)
    assert logs2.retention == {} and "unbounded" in lines[-1]


def test_ensure_decision_log_group_raises_other_errors_for_the_stage_to_degrade():
    class Denied(StubLogs):
        def create_log_group(self, logGroupName):  # noqa: N803
            raise ClientError({"Error": {"Code": "AccessDeniedException", "Message": "no"}},
                              "CreateLogGroup")

    with pytest.raises(ClientError):
        policy_audit.ensure_decision_log_group(Denied(), retention_days=90)


def test_probe_log_group_matches_the_exact_name():
    logs = StubLogs(groups=["/launchpad/policy/decisions-t1"])
    assert policy_audit.probe_log_group(logs, "/launchpad/policy/decisions") == "missing"
    logs.groups.append("/launchpad/policy/decisions")
    assert policy_audit.probe_log_group(logs, "/launchpad/policy/decisions") == "ready"


def test_roles_grant_what_the_sink_and_the_stage_call():
    """EC2 ops role: `logs:*` (nothing to add). Spoke workspace role: CreateLogGroup +
    PutRetentionPolicy (Observability Sid) and a narrow PolicyDecisionsLog Sid for
    CreateLogStream + PutLogEvents on /launchpad/policy/decisions*."""
    ec2 = (ROOT / "infra/ec2/launchpad-cn-ec2.yaml").read_text(encoding="utf-8")
    assert "- logs:*" in ec2
    spoke = (ROOT / "infra/spoke/launchpad-workspace-role.yaml").read_text(encoding="utf-8")
    for action in ("logs:CreateLogGroup", "logs:PutRetentionPolicy", "logs:CreateLogStream",
                   "logs:PutLogEvents"):
        assert f"- {action}" in spoke, action
    sid = spoke.split("Sid: PolicyDecisionsLog", 1)[1].split("Sid:", 1)[0]
    assert "logs:PutLogEvents" in sid and "logs:CreateLogStream" in sid
    assert "log-group:/launchpad/policy/decisions*" in sid
    assert "${AWS::Partition}" in sid and "arn:aws:" not in sid  # C2


def test_settings_knobs_default_as_documented():
    from app.core.config import Settings

    s = Settings()
    assert s.policy_audit_enabled is True
    assert (s.policy_audit_batch_size, s.policy_audit_flush_s, s.policy_audit_queue_max) == (
        100, 1.0, 5000)
    assert s.policy_decisions_prune_every == 500
    sink = policy_audit.CloudWatchDecisionSink.from_settings(autostart=False)
    assert (sink.batch_size, sink.flush_s, sink._queue.maxsize) == (100, 1.0, 5000)
