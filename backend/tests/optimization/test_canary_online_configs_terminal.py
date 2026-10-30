"""N17-obs442 — a finished canary's per-arm online configs stop judging traffic.

``act_setup`` creates two ENABLED experiment-owned configs (``can_<id8>_oec`` / ``_oet``);
complete and rollback only stopped the A/B row, so the configs stayed ENABLED and the
scheduler kept sampling and judging the agent's traffic for an experiment that was over
(live: can_50564110 rolled_back, both configs ACTIVE). Complete, rollback and cleanup now
set the canary's configs to DISABLED in the same commit as the status change. Complete and
rollback keep the rows; cleanup still deletes them, and if the delete fails the configs it
leaves are DISABLED. Another canary's configs and a user's agent config are never touched.
"""

from unittest.mock import MagicMock

from sqlalchemy import select

import app.optimization.canary_service as canary_svc
from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.evaluation import online
from app.evaluation.models import OnlineEvalConfig
from app.schemas.agent import AgentSpec
from tests.optimization.test_runtime_canaries import (
    _abtest_artifact,
    _agent,
    _live_artifacts,
    _mk_canary,
    _persist_agents,
    _reload,
    _round,
    _setup_artifact,
)


def _config(name: str, *, agent_id: str = "a1", status: str = "ENABLED") -> str:
    db = SessionLocal()
    try:
        row = OnlineEvalConfig(
            workspace_id=DEFAULT_WORKSPACE_ID, agent_id=agent_id, agent_name="subject",
            config_id=f"id-{name}", config_arn=f"arn:{name}", name=name,
            service_name="svc", log_group="/lg", execution_status=status, status="ACTIVE",
        )
        db.add(row)
        db.commit()
        return row.config_id
    finally:
        db.close()


def _statuses() -> dict[str, str]:
    db = SessionLocal()
    try:
        return {r.name: r.execution_status for r in db.scalars(select(OnlineEvalConfig))}
    finally:
        db.close()


def _arms(canary_id: str) -> tuple[str, str]:
    tag = canary_id[:8]
    _config(f"can_{tag}_oec")
    _config(f"can_{tag}_oet")
    return f"can_{tag}_oec", f"can_{tag}_oet"


def _bystanders() -> None:
    _config("can_zzzzzzzz_oec")  # another canary, still running
    _config("hr-helpdesk-online")  # the user's own agent config


BYSTANDERS = {"can_zzzzzzzz_oec": "ENABLED", "hr-helpdesk-online": "ENABLED"}


def test_complete_disables_the_canarys_arm_configs_and_keeps_them(monkeypatch):
    spec = AgentSpec(name="subject", method="zip_runtime", system_prompt="orig").model_dump()
    (agent_id,) = _persist_agents(_agent("subject", spec=spec))
    row = _mk_canary(agent_id=agent_id, artifacts={
        "agent_meta": {"id": agent_id}, "edited_spec": spec,
        "setup": _setup_artifact(ramp_stage=2), "abtest": _abtest_artifact(ramp_stage=2),
        "rounds": [_round(2, verdict={"verdict": "treatment-wins", "significant": True})],
    })
    oec, oet = _arms(row.id)
    _bystanders()
    monkeypatch.setattr(canary_svc, "control_client", MagicMock)

    canary_svc.act_complete(row.id, lambda _m: None, allow_non_significant=False)

    assert _reload(row.id).status == "completed"
    assert _statuses() == {oec: "DISABLED", oet: "DISABLED", **BYSTANDERS}


def test_rollback_disables_the_canarys_arm_configs_and_keeps_them(monkeypatch):
    spec = AgentSpec(name="subject", method="zip_runtime", system_prompt="orig").model_dump()
    (agent_id,) = _persist_agents(_agent("subject", spec=spec))
    row = _mk_canary(agent_id=agent_id, artifacts={
        "agent_meta": {"id": agent_id}, "edited_spec": {}, "setup": _setup_artifact(),
        "abtest": _abtest_artifact(), "rounds": []})
    oec, oet = _arms(row.id)
    _bystanders()
    monkeypatch.setattr(canary_svc, "control_client", MagicMock)
    monkeypatch.setattr(canary_svc.canary_infra, "mint_candidate_version",
                        lambda **kw: ("1", "9", "agents/subject/canary/x.zip"))

    canary_svc.act_rollback(row.id, lambda _m: None)

    assert _reload(row.id).status == "rolled_back"
    assert _statuses() == {oec: "DISABLED", oet: "DISABLED", **BYSTANDERS}


def test_a_failed_transition_leaves_the_configs_as_they_were(monkeypatch):
    # the disable rides the status commit: a rollback that fails before it changes nothing
    spec = AgentSpec(name="subject", method="zip_runtime", system_prompt="orig").model_dump()
    (agent_id,) = _persist_agents(_agent("subject", spec=spec))
    row = _mk_canary(agent_id=agent_id, artifacts={
        "agent_meta": {"id": agent_id}, "edited_spec": {}, "setup": _setup_artifact(),
        "abtest": _abtest_artifact(), "rounds": []})
    oec, oet = _arms(row.id)
    monkeypatch.setattr(canary_svc, "control_client", MagicMock)

    def boom(**_kw):
        raise RuntimeError("mint failed")

    monkeypatch.setattr(canary_svc.canary_infra, "mint_candidate_version", boom)
    try:
        canary_svc.act_rollback(row.id, lambda _m: None)
    except RuntimeError:
        pass
    assert _reload(row.id).status == "running"
    assert _statuses() == {oec: "ENABLED", oet: "ENABLED"}


def test_cleanup_whose_delete_fails_leaves_the_configs_disabled(monkeypatch):
    row = _mk_canary(artifacts=_live_artifacts())
    oec, oet = _arms(row.id)
    _bystanders()

    def refuse(*_a, **_kw):
        raise RuntimeError("ledger locked")

    monkeypatch.setattr(online, "delete_for_owner", refuse)
    monkeypatch.setattr(canary_svc.canary_infra, "delete_endpoint_quiet", lambda c, **kw: None)
    monkeypatch.setattr(canary_svc.canary_infra, "wait_endpoints_gone", lambda c, **kw: [])
    monkeypatch.setattr(canary_svc, "control_client", MagicMock)

    results = canary_svc.act_cleanup(row.id, lambda _m: None)

    skipped = [r for r in results if r["category"].startswith("online-eval:")]
    assert skipped and all(r["status"] == "skipped" and "DISABLED" in r["detail"]
                           for r in skipped)
    assert _reload(row.id).status == "cleaned"
    assert _statuses() == {oec: "DISABLED", oet: "DISABLED", **BYSTANDERS}


def test_cleanup_still_deletes_the_configs(monkeypatch):
    row = _mk_canary(artifacts=_live_artifacts())
    _arms(row.id)
    _bystanders()
    monkeypatch.setattr(canary_svc.canary_infra, "delete_endpoint_quiet", lambda c, **kw: None)
    monkeypatch.setattr(canary_svc.canary_infra, "wait_endpoints_gone", lambda c, **kw: [])
    monkeypatch.setattr(canary_svc, "control_client", MagicMock)

    canary_svc.act_cleanup(row.id, lambda _m: None)

    assert _statuses() == BYSTANDERS


def test_disable_for_owner_on_the_callers_session_waits_for_its_commit():
    oec, oet = _arms("cafebabe-0000")
    ws = type("Ws", (), {"id": DEFAULT_WORKSPACE_ID})()
    db = SessionLocal()
    try:
        assert sorted(online.disable_for_owner(ws, "cafebabe-0000", db=db)) == [
            f"id-{oec}", f"id-{oet}"]
        db.rollback()
    finally:
        db.close()
    assert _statuses() == {oec: "ENABLED", oet: "ENABLED"}
    assert sorted(online.disable_for_owner(ws, "cafebabe-0000")) == [f"id-{oec}", f"id-{oet}"]
    assert _statuses() == {oec: "DISABLED", oet: "DISABLED"}
    assert online.disable_for_owner(ws, "cafebabe-0000") == []  # already disabled
