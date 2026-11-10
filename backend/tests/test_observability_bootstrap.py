"""Direct unit tests for ensure_transaction_search — the workspace_bootstrap
observability stage tests (test_workspace_bootstrap.py) exercise it through the
stage wrapper; these exercise the function itself."""

from app.services.observability_bootstrap import ensure_transaction_search


class _FakeXray:
    def __init__(self, states: list[dict]):
        self._states = list(states)
        self.update_calls: list[str] = []

    def get_trace_segment_destination(self):
        return self._states.pop(0) if len(self._states) > 1 else self._states[0]

    def update_trace_segment_destination(self, Destination):  # noqa: N803
        self.update_calls.append(Destination)


def test_already_active_is_a_no_op():
    xray = _FakeXray([{"Destination": "CloudWatchLogs", "Status": "ACTIVE"}])
    result = ensure_transaction_search(xray)
    assert result == {"enabled": True, "changed": False, "status": "ACTIVE"}
    assert xray.update_calls == []


def test_destination_xray_triggers_update_then_waits_for_active():
    # Mirrors the live cn-northwest-1 fact recorded 2026-10-08:
    # Destination=XRay, Status=ACTIVE (Transaction Search not yet enabled there).
    xray = _FakeXray(
        [
            {"Destination": "XRay", "Status": "ACTIVE"},
            {"Destination": "CloudWatchLogs", "Status": "PENDING"},
            {"Destination": "CloudWatchLogs", "Status": "ACTIVE"},
        ]
    )
    result = ensure_transaction_search(xray)
    assert xray.update_calls == ["CloudWatchLogs"]
    assert result == {"enabled": True, "changed": True, "status": "ACTIVE"}


def test_poll_exhausts_without_becoming_active(monkeypatch):
    import app.services.observability_bootstrap as ob

    monkeypatch.setattr(ob, "TRANSACTION_SEARCH_POLL_ATTEMPTS", 2)
    monkeypatch.setattr(ob, "TRANSACTION_SEARCH_POLL_INTERVAL_S", 0)
    xray = _FakeXray([{"Destination": "XRay", "Status": "ACTIVE"}])
    xray._states = [{"Destination": "XRay", "Status": "ACTIVE"}] + [
        {"Destination": "CloudWatchLogs", "Status": "PENDING"}
    ] * 3
    result = ensure_transaction_search(xray)
    assert xray.update_calls == ["CloudWatchLogs"]
    assert result == {"enabled": False, "changed": True, "status": "PENDING"}


def test_pending_switch_to_cloudwatch_logs_waits_instead_of_updating_again():
    # N11-bug11 (live): re-running bootstrap while the first switch was still
    # PENDING re-issued the update -> InvalidRequestException from X-Ray.
    xray = _FakeXray(
        [
            {"Destination": "CloudWatchLogs", "Status": "PENDING"},
            {"Destination": "CloudWatchLogs", "Status": "ACTIVE"},
        ]
    )
    result = ensure_transaction_search(xray)
    assert xray.update_calls == []
    assert result == {"enabled": True, "changed": False, "status": "ACTIVE"}
