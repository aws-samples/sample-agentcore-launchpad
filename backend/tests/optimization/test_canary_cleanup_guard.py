"""N17-obs452 — a running canary past setup cannot be cleaned up until it is decided.

Setup's mint (``UpdateAgentRuntime``) auto-rolls the runtime's DEFAULT endpoint — production —
to the untested candidate; only the invoke chain's A/B pinning keeps live traffic on the
stable endpoint. ``cleanup`` stops the A/B row and deletes both named endpoints but rolls
nothing forward, and it was allowed on a RUNNING canary, so cleaning one up silently left
production on the candidate while the ledger still said ``v_current`` (no snapshot).

Now the action route refuses ``cleanup`` of a running canary with 409
``canary.cleanup_requires_decision`` until it is completed or rolled back
(``canary_service.refuse_cleanup_before_decision``):

* setup recorded ``v_candidate`` (the mint ran) → refused;
* a partial setup (the stable endpoint, no ``v_candidate`` — setup failed): the mint may or
  may not have run, so the runtime's LIVE version is read: equal to ``setup.v_current`` —
  DEFAULT never moved — the cleanup is allowed; different, or unreadable → refused;
* never set up (no setup artifact: nothing minted), and every terminal canary → allowed.

Rollback stays available on every running canary (the safety valve), so a refusal is never
a dead end. The architect's conversation purge calls ``act_cleanup`` directly and is not
gated: it deletes the canary's Agent and its runtime right after.
"""

import pytest

import app.optimization.canary_service as canary_svc
from app.core.errors import AppError
from tests.optimization.test_runtime_canaries import _live_artifacts, _mk_canary, _reload

PARTIAL = {"agent_meta": {"id": "a1"}, "edited_spec": {}, "rounds": [],
           "setup": {"runtime_id": "subject-res", "stable_endpoint": "stablecan",
                     "v_current": "4", "gateway_id": "invoke-chain"}}


def _dispatch(monkeypatch) -> list[str]:
    dispatched: list[str] = []
    monkeypatch.setattr(canary_svc, "run_action",
                        lambda canary_id, action, fn: dispatched.append(action))
    return dispatched


def _live_version(monkeypatch, value):
    def read(_control, _runtime_id):
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(canary_svc, "control_client", lambda _ws: object())
    monkeypatch.setattr(canary_svc.canary_infra, "current_version", read)


def test_a_live_canary_cannot_be_cleaned_up_before_a_decision(client, monkeypatch):
    row = _mk_canary(artifacts=_live_artifacts())
    dispatched = _dispatch(monkeypatch)
    res = client.post(f"/api/runtime-canaries/{row.id}/action", json={"action": "cleanup"})
    assert res.status_code == 409
    body = res.json()
    assert body["code"] == "canary.cleanup_requires_decision"
    assert body["detail"]["state"] == "live"
    assert dispatched == []
    assert _reload(row.id).status == "running"


@pytest.mark.parametrize("status", ["completed", "rolled_back", "cleaned"])
def test_a_decided_canary_cleans_up(client, monkeypatch, status):
    row = _mk_canary(status=status, artifacts=_live_artifacts())
    dispatched = _dispatch(monkeypatch)
    res = client.post(f"/api/runtime-canaries/{row.id}/action", json={"action": "cleanup"})
    assert res.status_code == 202 and dispatched == ["cleanup"]


def test_a_canary_never_set_up_cleans_up(client, monkeypatch):
    row = _mk_canary()  # no setup artifact: nothing was minted
    dispatched = _dispatch(monkeypatch)
    res = client.post(f"/api/runtime-canaries/{row.id}/action", json={"action": "cleanup"})
    assert res.status_code == 202 and dispatched == ["cleanup"]


def test_a_failed_setup_cleans_up_only_while_default_never_moved(client, monkeypatch):
    row = _mk_canary(artifacts=PARTIAL)
    dispatched = _dispatch(monkeypatch)
    _live_version(monkeypatch, "4")  # DEFAULT still serves v_current: the mint never ran
    res = client.post(f"/api/runtime-canaries/{row.id}/action", json={"action": "cleanup"})
    assert res.status_code == 202 and dispatched == ["cleanup"]


@pytest.mark.parametrize("live", ["5", RuntimeError("throttled")])
def test_a_failed_setup_whose_default_moved_or_is_unknown_is_refused(client, monkeypatch, live):
    row = _mk_canary(artifacts=PARTIAL)
    dispatched = _dispatch(monkeypatch)
    _live_version(monkeypatch, live)
    res = client.post(f"/api/runtime-canaries/{row.id}/action", json={"action": "cleanup"})
    assert res.status_code == 409
    body = res.json()
    assert body["code"] == "canary.cleanup_requires_decision"
    assert body["detail"]["state"] == "partial_setup"
    assert body["detail"]["v_current"] == "4"
    assert body["detail"]["live_version"] == ("5" if live == "5" else None)
    assert dispatched == []


def test_rollback_stays_available_where_cleanup_is_refused(monkeypatch):
    for artifacts in (_live_artifacts(), PARTIAL):
        row = _mk_canary(artifacts=artifacts)
        assert canary_svc.stage_not_ready_reason(_reload(row.id), "rollback") is None


def test_the_service_guard_is_the_route_s_rule(monkeypatch):
    row = _mk_canary(artifacts=_live_artifacts())
    with pytest.raises(AppError) as exc:
        canary_svc.refuse_cleanup_before_decision(_reload(row.id))
    assert exc.value.status_code == 409
