"""Stream B5: the interceptor's I7 decision lines become ``policy_decisions`` rows
(``source=gateway``) — idempotent on ``decision_id``, argument names only, and the audit API
shows the new columns for gateway rows while every other row's view stays byte-identical."""

from __future__ import annotations

import copy
import json
from typing import Any

from botocore.exceptions import ClientError

from app.core.db import SessionLocal
from app.models.policy import PolicyDecision
from app.services import policy_audit, policy_store
from tests.policy_principal.conftest import load_fixture


def _line(**overrides: Any) -> dict[str, Any]:
    line = copy.deepcopy(load_fixture("audit_lines.json")["audit_decision_line"])
    line["launchpad.policy.decision"].update(overrides)
    return line


def _event(line: dict[str, Any], ts: int = 1_793_022_001_000) -> dict[str, Any]:
    return {"timestamp": ts, "message": json.dumps(line)}


def test_a_decision_line_maps_onto_the_existing_columns_plus_the_gateway_ones():
    row = policy_audit.row_from_gateway_line(_line())
    assert row.id == "3f1e2d3c4b5a69788796a5b4c3d2e1f0" and row.source == "gateway"
    assert (row.workspace_id, row.principal_kind, row.principal_id) == (
        "default", "user", "hr-intern")
    assert (row.action, row.resource_kind, row.resource_id) == (
        "call_tool", "tool", "hr-database___create_payout")
    assert row.decision == "deny" and row.mode == "enforce"
    assert row.policy_ids == ["pol_payoutadm"] and row.policy_versions == {"pol_payoutadm": 2}
    assert (row.stage, row.event_seq, row.store_epoch) == ("final", 1042, "e3")
    assert row.input_keys == ["amount", "employee_id"]
    assert row.session_psid.endswith("-v3-g0")


def test_ingest_is_idempotent_and_skips_lambda_noise():
    noise = [{"timestamp": 1, "message": "START RequestId: x Version: $LATEST"},
             {"timestamp": 2, "message": json.dumps(
                 {"launchpad.policy.interceptor": {"event": "respond_failed"}})},
             {"timestamp": 3, "message": "{not json launchpad.policy.decision"}]
    line = _line(decision_id="a" * 32)
    with SessionLocal() as db:
        assert policy_audit.ingest_gateway_events(db, [*noise, _event(line), _event(line)]) == 1
        assert policy_audit.ingest_gateway_events(db, [_event(line)]) == 0
        assert db.get(PolicyDecision, "a" * 32).decision == "deny"


def test_unknown_caller_rows_land_in_the_default_workspace():
    row = policy_audit.row_from_gateway_line(_line(ws="unknown", principal={
        "kind": "unknown", "id": "somebody-else"}))
    assert row.workspace_id == "default" and row.principal_kind == "unknown"


def test_lines_that_are_not_i7_are_ignored():
    assert policy_audit.row_from_gateway_line({"other": {}}) is None
    assert policy_audit.row_from_gateway_line(_line(source="hub")) is None
    assert policy_audit.row_from_gateway_line(_line(decision_id="short")) is None


class FakeLogs:
    def __init__(self, pages: list[dict[str, Any]] | None = None, error: str | None = None):
        self.pages = pages or []
        self.error = error
        self.calls: list[dict[str, Any]] = []

    def filter_log_events(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise ClientError({"Error": {"Code": self.error}}, "FilterLogEvents")
        return self.pages.pop(0) if self.pages else {"events": []}


def test_poller_pages_advances_its_cursor_and_rereads_the_overlap():
    now = 1_793_022_100.0
    first = [_event(_line(decision_id="b" * 32), ts=int(now * 1000) - 1000)]
    second = [_event(_line(decision_id="c" * 32), ts=int(now * 1000))]
    logs = FakeLogs([{"events": first, "nextToken": "t"}, {"events": second}])
    ingester = policy_audit.GatewayDecisionIngester(logs, SessionLocal, log_group="/aws/lambda/x",
                                                    clock=lambda: now)
    assert ingester.poll_once() == 2
    assert logs.calls[1]["nextToken"] == "t"
    assert logs.calls[0]["filterPattern"] == '"launchpad.policy.decision"'
    assert ingester.cursor_ms == int(now * 1000)
    assert ingester.poll_once() == 0
    assert logs.calls[-1]["startTime"] == int(now * 1000) - policy_audit.INGEST_OVERLAP_MS


def test_poller_is_fail_soft():
    ingester = policy_audit.GatewayDecisionIngester(FakeLogs(error="AccessDeniedException"),
                                                    SessionLocal, log_group="/aws/lambda/x")
    assert ingester.poll_once() == 0 and ingester.last_error == "AccessDeniedException"


def test_ingest_never_starts_with_the_kill_switch_off():
    from types import SimpleNamespace

    assert policy_audit.start_gateway_ingest(SimpleNamespace(
        policy_gateway_interceptor=False)) is None


def test_gateway_log_group_follows_the_env_suffix():
    assert policy_audit.gateway_log_group("") == "/aws/lambda/launchpad-policy-interceptor"
    assert policy_audit.gateway_log_group("t1") == "/aws/lambda/launchpad-policy-interceptor-t1"


def test_decision_view_adds_the_gateway_fields_only_to_gateway_rows():
    gateway = policy_audit.row_from_gateway_line(_line())
    hub = PolicyDecision(id="d" * 32, workspace_id="default", source="hub", channel="chat",
                         principal_kind="user", principal_id="u", action="invoke",
                         resource_kind="agent", resource_id="ag", decision="allow",
                         mode="enforce", policy_ids=[], policy_versions={})
    hub_view = policy_store.decision_view(hub)
    assert not {"stage", "session_psid", "event_seq", "store_epoch", "input_keys"} & set(hub_view)
    view = policy_store.decision_view(gateway)
    assert view["stage"] == "final" and view["input_keys"] == ["amount", "employee_id"]
    assert set(view) - set(hub_view) == {"stage", "session_psid", "event_seq", "store_epoch",
                                         "input_keys", "ingested_at", "ingest_lag_ms",
                                         "log_only_would"}  # N17-obs508 (additive)
    assert view["log_only_would"] is False
    assert "EMP-1024" not in json.dumps(view, default=str)
    # N17-obs482: a gateway row ingested before the column existed reads null, never a guess
    assert (view["ingested_at"], view["ingest_lag_ms"]) == (None, None)


# ── N17-obs482: the ingest stamp ───────────────────────────────────────────


def test_ingest_stamps_ingested_at_from_the_hub_clock_never_from_the_line():
    """The row's ``created_at`` is the interceptor's decision time (``ts``); ``ingested_at`` is
    the hub's own clock at ingest. A line that carries an ``ingested_at`` of its own is not
    believed — the interceptor never sets it — and the audit view exposes both plus the lag."""
    from datetime import UTC, datetime

    decided = datetime(2026, 10, 26, 14, 0, 1, tzinfo=UTC)
    ingested = datetime(2026, 10, 26, 14, 0, 14, 500_000, tzinfo=UTC)  # +13.5 s
    line = _line(decision_id="e" * 32, ts=decided.isoformat(),
                 ingested_at="2001-01-01T00:00:00Z")  # a forged stamp is ignored
    assert policy_audit.row_from_gateway_line(line).ingested_at is None
    with SessionLocal() as db:
        assert policy_audit.ingest_gateway_events(db, [_event(line)], now=ingested) == 1
        row = db.get(PolicyDecision, "e" * 32)
        assert row.created_at.replace(tzinfo=UTC) == decided
        assert row.ingested_at.replace(tzinfo=UTC) == ingested
        view = policy_store.decision_view(row)
        assert view["ingested_at"] == row.ingested_at.isoformat()
        assert view["ingest_lag_ms"] == 13500.0
        # a re-read of the same line keeps the first stamp (idempotent on decision_id)
        later = datetime(2026, 10, 26, 14, 5, tzinfo=UTC)
        assert policy_audit.ingest_gateway_events(db, [_event(line)], now=later) == 0
        db.expire_all()
        assert db.get(PolicyDecision, "e" * 32).ingested_at.replace(tzinfo=UTC) == ingested


def test_ingest_without_a_clock_stamps_now_in_utc():
    from datetime import UTC, datetime

    before = datetime.now(UTC).replace(tzinfo=None)
    line = _line(decision_id="f" * 32)
    with SessionLocal() as db:
        policy_audit.ingest_gateway_events(db, [_event(line)])
        stamp = db.get(PolicyDecision, "f" * 32).ingested_at.replace(tzinfo=None)
    assert before <= stamp <= datetime.now(UTC).replace(tzinfo=None)


def test_the_audit_order_stays_the_decision_time_whatever_the_ingest_order():
    """Ordering unchanged (N17-obs482 is additive): the timeline is ``created_at`` descending —
    a line delivered late (ingested after a newer decision) still sorts by when it was decided."""
    from datetime import UTC, datetime

    older = _line(decision_id="1" * 32, ts="2026-10-26T14:00:01Z")
    newer = _line(decision_id="2" * 32, ts="2026-10-26T14:00:05Z")
    with SessionLocal() as db:
        policy_audit.ingest_gateway_events(
            db, [_event(newer)], now=datetime(2026, 10, 26, 14, 0, 20, tzinfo=UTC))
        policy_audit.ingest_gateway_events(
            db, [_event(older)], now=datetime(2026, 10, 26, 14, 1, 30, tzinfo=UTC))  # late
        rows = policy_store.list_decisions(db, "default", source="gateway", limit=10)
        assert [r.id for r in rows] == ["2" * 32, "1" * 32]
        lags = {r.id: policy_store.decision_view(r)["ingest_lag_ms"] for r in rows}
        assert lags == {"2" * 32: 15000.0, "1" * 32: 89000.0}


def test_a_temporal_set_mismatch_row_schedules_one_reconcile(monkeypatch):
    """N17-obs492 (re-review F3): the Lambda's `temporal_set_mismatch` denials are the signal
    that a shard is not on the bundle's set — the ingester asks for the same background
    reconcile the hub's own decide path uses (once per policy engine per batch)."""
    from app.services import policy_engine_decide

    scheduled: list[str] = []
    monkeypatch.setattr(policy_engine_decide, "schedule_reconcile",
                        lambda ws: scheduled.append(ws) or True)
    mismatch = _line(decision_id="a" * 32, decision="deny", engine_id="pe_0123456789ab",
                     reason="temporal_set_mismatch")
    other = _line(decision_id="b" * 32, decision="deny", reason="policy")
    db = SessionLocal()
    try:
        assert policy_audit.ingest_gateway_events(
            db, [_event(mismatch), _event(other), _event(mismatch)]) == 2
        assert scheduled == ["pe_0123456789ab"]
        # a re-read of the same lines stores nothing and schedules nothing more
        assert policy_audit.ingest_gateway_events(db, [_event(mismatch)]) == 0
        assert scheduled == ["pe_0123456789ab"]
    finally:
        db.close()
