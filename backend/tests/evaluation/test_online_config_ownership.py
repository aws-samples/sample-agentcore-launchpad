"""N17-obs426 — online evaluation config mutations are creator-or-admin.

Pause / resume / edit / delete checked only that the config was agent-owned (not an
experiment arm), so in the shared visibility mode any member could pause or delete an
administrator's config — and with it the whole online scoring of that agent. Now every
mutation is ``services/ownership.require_creator_or_admin`` (403 ``online_config.forbidden``,
naming the config's creator), after the visibility lookup and before any change. The
role × mode cells are in ``tests/test_permission_matrix.py`` (``online_configs*``).
"""

from __future__ import annotations

import pytest

from app.core.db import SessionLocal
from app.core.errors import AppError
from app.evaluation import online
from app.evaluation.models import OnlineEvalConfig
from app.routers.auth import Identity
from tests.conftest import ws_ctx
from tests.evaluation.test_online_configs import ledger_row, make_agent

ADA = Identity(username="ada", role="member", user_id="u-ada")
BOB = Identity(username="bob", role="member", user_id="u-bob")
CAROL = Identity(username="carol", role="admin", user_id="u-carol")


def _config(db, config_id: str, **cols) -> OnlineEvalConfig:
    return ledger_row(db, make_agent(db), config_id, name=f"oe_{config_id}_agent", **cols)


@pytest.mark.parametrize("action", ["pause", "resume", "edit", "delete"])
def test_a_member_may_not_mutate_another_members_config_and_nothing_changes(action):
    db = SessionLocal()
    row = _config(db, f"own-{action}", created_by="u-ada", created_by_name="ada",
                  execution_status="ENABLED")
    call = {
        "pause": lambda who: online.set_execution_status(db, ws_ctx(), row.config_id,
                                                         enabled=False, identity=who),
        "resume": lambda who: online.set_execution_status(db, ws_ctx(), row.config_id,
                                                          enabled=True, identity=who),
        "edit": lambda who: online.patch_config(db, ws_ctx(), row.config_id,
                                                {"sampling_percentage": 50.0}, identity=who),
        "delete": lambda who: online.delete_config(db, ws_ctx(), row.config_id, identity=who),
    }[action]
    with pytest.raises(AppError) as exc:
        call(BOB)
    assert exc.value.code == "online_config.forbidden" and exc.value.status_code == 403
    assert exc.value.detail["created_by_name"] == "ada"
    assert exc.value.detail["config_id"] == row.config_id
    db.expire_all()
    kept = db.query(OnlineEvalConfig).filter_by(config_id=row.config_id).one()
    assert (kept.execution_status, kept.sampling_percentage) == ("ENABLED", 5.0)
    call(ADA)  # the creator may
    db.close()


def test_an_administrators_config_is_safe_from_members_and_a_null_creator_is_an_admins():
    db = SessionLocal()
    admins = _config(db, "admins-1", created_by="u-carol", created_by_name="carol")
    legacy = _config(db, "legacy-1")  # created_by NULL: the config admin's / a legacy row
    for row in (admins, legacy):
        with pytest.raises(AppError) as exc:
            online.set_execution_status(db, ws_ctx(), row.config_id, enabled=False,
                                        identity=ADA)
        assert exc.value.code == "online_config.forbidden"
        assert online.set_execution_status(db, ws_ctx(), row.config_id, enabled=False,
                                           identity=CAROL)["execution_status"] == "DISABLED"
    db.close()
