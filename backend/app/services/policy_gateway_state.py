"""The persisted interceptor attach state of a gateway (per-user policy stream B; read by
stream A's runtime-guard disarm).

Written ONLY from ``gateway_bootstrap.set_interceptor``'s verified read-back (its ``record``
hook, wired by ``scripts/attach_policy_interceptor.py``): attach sets it, detach clears it, an
update whose read-back fails writes nothing. Readers treat a missing row as "not attached",
so the guard stays armed until an attach was proven.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from app.models.policy import PolicyGatewayAttachment


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def gateway_interceptor_attached(db: Session, gateway_id: str | None) -> bool:
    """True only when the gateway's last verified state is "attached"."""
    if not gateway_id:
        return False
    row = db.get(PolicyGatewayAttachment, gateway_id)
    return bool(row and row.attached)


def attachment(db: Session, gateway_id: str) -> dict[str, Any] | None:
    row = db.get(PolicyGatewayAttachment, gateway_id)
    if row is None:
        return None
    return {"gateway_id": row.gateway_id, "interceptor_arn": row.interceptor_arn,
            "attached": bool(row.attached), "attached_at": _iso(row.attached_at),
            "detached_at": _iso(row.detached_at)}


def record_attach(db: Session, gateway_id: str, interceptor_arn: str) -> None:
    row = db.get(PolicyGatewayAttachment, gateway_id) or PolicyGatewayAttachment(
        gateway_id=gateway_id)
    row.interceptor_arn, row.attached = interceptor_arn, True
    row.attached_at, row.detached_at = datetime.now(UTC), None
    db.add(row)
    db.commit()


def record_detach(db: Session, gateway_id: str) -> None:
    row = db.get(PolicyGatewayAttachment, gateway_id) or PolicyGatewayAttachment(
        gateway_id=gateway_id)
    row.attached, row.detached_at = False, datetime.now(UTC)
    db.add(row)
    db.commit()
