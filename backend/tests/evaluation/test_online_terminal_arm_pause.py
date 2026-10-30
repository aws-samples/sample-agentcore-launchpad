"""N17-obs445 — a finished canary's arm configs can be paused (and resumed).

Before N17-obs442, complete / rollback left a canary's per-arm configs ENABLED; the user chose
to pause the two arms of rolled-back canary 50564110, but ``POST /api/eval/online/<can_…>/pause``
answered 403 ``online_eval.read_only`` for every caller (``_require_owner(row, ("agent",))``).
Now pause / resume of an experiment-owned config is allowed once its owning canary (or legacy
experiment) is TERMINAL, creator-or-admin against the owning run's creator (an arm config
records none). While the canary runs — the protected 9f0d0a50 / d88a369a, the EV-07 canary —
its arms stay read-only. Edit and delete stay agent-config only. The payload names the owning
run additively (``owner_run``) so the console can offer 暂停 on such a row.
"""

from __future__ import annotations

import pytest

from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.core.errors import AppError
from app.evaluation import online
from app.evaluation.models import OnlineEvalConfig
from app.optimization.models import RuntimeCanary
from app.routers.auth import Identity
from tests.conftest import ws_ctx
from tests.evaluation.test_online_configs import ledger_row, make_agent

ADA = Identity(username="ada", role="member", user_id="u-ada")
BOB = Identity(username="bob", role="member", user_id="u-bob")
CAROL = Identity(username="carol", role="admin", user_id="u-carol")


def _canary(db, status: str, *, created_by="u-ada", created_by_name="ada") -> RuntimeCanary:
    row = RuntimeCanary(workspace_id=DEFAULT_WORKSPACE_ID, name="CANARY-x",
                        champion_agent_id="a1", champion_agent_name="x",
                        challenger_agent_id="a1", challenger_agent_name="x", status=status,
                        stage="setup", artifacts={}, created_by=created_by,
                        created_by_name=created_by_name)
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _arm(db, canary: RuntimeCanary, arm: str = "oet") -> OnlineEvalConfig:
    name = f"can_{canary.id[:8]}_{arm}"
    return ledger_row(db, make_agent(db, resource_id=f"rt-{canary.id[:6]}-{arm}"),
                      f"{name}-x", name=name, execution_status="ENABLED")


def _status(db, config_id: str) -> str:
    db.expire_all()
    return db.query(OnlineEvalConfig).filter_by(config_id=config_id).one().execution_status


@pytest.mark.parametrize("status", ["completed", "rolled_back", "cleaned"])
def test_a_terminal_canarys_arm_can_be_paused_and_resumed_by_its_creator(status):
    db = SessionLocal()
    arm = _arm(db, _canary(db, status))
    out = online.set_execution_status(db, ws_ctx(), arm.config_id, enabled=False, identity=ADA)
    assert out["execution_status"] == "DISABLED" == _status(db, arm.config_id)
    online.set_execution_status(db, ws_ctx(), arm.config_id, enabled=True, identity=ADA)
    assert _status(db, arm.config_id) == "ENABLED"
    db.close()


def test_a_running_canarys_arm_stays_read_only_for_everyone():
    db = SessionLocal()
    arm = _arm(db, _canary(db, "running"))
    for who in (ADA, CAROL, None):
        with pytest.raises(AppError) as exc:
            online.set_execution_status(db, ws_ctx(), arm.config_id, enabled=False,
                                        identity=who)
        assert exc.value.code == "online_eval.read_only" and exc.value.status_code == 403
    assert _status(db, arm.config_id) == "ENABLED"
    db.close()


def test_creator_or_admin_against_the_canarys_creator():
    db = SessionLocal()
    arm = _arm(db, _canary(db, "rolled_back"))
    with pytest.raises(AppError) as exc:
        online.set_execution_status(db, ws_ctx(), arm.config_id, enabled=False, identity=BOB)
    assert exc.value.code == "online_config.forbidden" and exc.value.status_code == 403
    assert _status(db, arm.config_id) == "ENABLED"
    online.set_execution_status(db, ws_ctx(), arm.config_id, enabled=False, identity=CAROL)
    assert _status(db, arm.config_id) == "DISABLED"
    # a canary with no recorded creator is an administrator's
    legacy = _arm(db, _canary(db, "completed", created_by=None, created_by_name=None), "oec")
    with pytest.raises(AppError) as exc:
        online.set_execution_status(db, ws_ctx(), legacy.config_id, enabled=False,
                                    identity=ADA)
    assert exc.value.code == "online_config.forbidden"
    db.close()


def test_edit_and_delete_of_a_terminal_canarys_arm_stay_refused():
    db = SessionLocal()
    arm = _arm(db, _canary(db, "rolled_back"))
    for call in (lambda: online.patch_config(db, ws_ctx(), arm.config_id,
                                             {"sampling_percentage": 50.0}, identity=ADA),
                 lambda: online.delete_config(db, ws_ctx(), arm.config_id, identity=ADA)):
        with pytest.raises(AppError) as exc:
            call()
        assert exc.value.code == "online_eval.read_only"
    db.close()


def test_an_arm_without_its_canary_stays_read_only():
    db = SessionLocal()
    name = "can_deadbeef_oet"
    orphan = ledger_row(db, make_agent(db, resource_id="rt-orphan"), f"{name}-x", name=name,
                        execution_status="ENABLED")
    with pytest.raises(AppError) as exc:
        online.set_execution_status(db, ws_ctx(), orphan.config_id, enabled=False,
                                    identity=CAROL)
    assert exc.value.code == "online_eval.read_only"
    db.close()


def test_the_payload_names_the_owning_run():
    db = SessionLocal()
    done = _canary(db, "rolled_back")
    live = _canary(db, "running", created_by="u-bob", created_by_name="bob")
    done_arm, live_arm = _arm(db, done), _arm(db, live)
    agent_cfg = ledger_row(db, make_agent(db, resource_id="rt-plain"), "oe_plain-1",
                           name="oe_plain")
    rows = {c["config_id"]: c for c in online.list_configs(db, ws_ctx())}
    assert rows[done_arm.config_id]["owner_run"] == {
        "kind": "canary", "id": done.id, "status": "rolled_back", "terminal": True,
        "created_by": "u-ada", "created_by_name": "ada"}
    assert rows[live_arm.config_id]["owner_run"]["terminal"] is False
    assert rows[agent_cfg.config_id]["owner_run"] is None
    assert online.get_config(db, ws_ctx(), done_arm.config_id)["owner_run"]["id"] == done.id
    db.close()


def test_the_pause_route_answers_200_for_a_terminal_arm_and_403_for_a_running_one(client):
    db = SessionLocal()
    done_arm = _arm(db, _canary(db, "rolled_back", created_by=None, created_by_name=None))
    live_arm = _arm(db, _canary(db, "running"))
    db.close()
    ok = client.post(f"/api/eval/online/{done_arm.config_id}/pause")
    assert ok.status_code == 200 and ok.json()["execution_status"] == "DISABLED"
    refused = client.post(f"/api/eval/online/{live_arm.config_id}/pause")
    assert refused.status_code == 403 and refused.json()["code"] == "online_eval.read_only"
