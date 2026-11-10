"""The gateway bundle the hub writes for every gateway with a policy-engine attachment
(Global model; contract ``docs/cn/interfaces/policy-engines.md`` §Bundle, schema
``launchpad.policy.gateway/3``).

The interceptor Lambda of a gateway reads ONE bundle (``policy/gateway/<gw>/v<n>.json`` behind
the ``current.json`` pointer): the engine endpoints, the principal-token public keys, the agent
role map and the gateway's ``attachment`` — ``{engine_id, mode}`` plus the attached engine's
installed set facts (set version, counts, governed actions) — or ``attachment: null`` for a
gateway that is not attached (the interceptor passes everything; it is detached anyway).

* Written after every successful engine install (every gateway attached to that engine), after
  an attach / detach / mode change (that gateway), and by the reconciler whenever a live bundle
  no longer says what the hub would compose now (``reconcile``). Off unless both
  ``Settings.policy_engine_endpoints`` and ``Settings.policy_hub_role_arn`` are set
  (``configured``).
* A write failure never undoes the install that preceded it: it is journalled
  (``gateway.bundle`` failed), the engine's ``engine_state.bundle_pending`` stays true, and the
  next install / attachment change or ``reconcile`` (startup, every 300 s) writes it again.
* Written with the forward-only CAS writer (``policy_bundle.write_gateway_bundle``: the next
  free ``v<n>``, then the pointer only forward).
"""

from __future__ import annotations

import hashlib
import json
import logging
import random
import threading
from collections.abc import Callable
from typing import Any

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import AppError
from app.models.policy import (
    PolicyChange,
    PolicyEngine,
    PolicyEngineAttachment,
    PolicyGatewayAttachment,
)
from app.policy import contracts

logger = logging.getLogger("launchpad.policy")

# one gateway-bundle write at a time in this process (an install and the reconciler never
# compose from interleaved reads); the CAS writer covers a second hub process
_PUBLISH_LOCK = threading.Lock()
RECONCILE_EVERY_S = 300.0
RECONCILE_JITTER_S = 30.0
UNPUBLISHED_SET = "unpublished"  # the bundle's set version while the engine has installed nothing


def configured() -> bool:
    settings = get_settings()
    return bool(settings.policy_engine_endpoints) and bool(settings.policy_hub_role_arn)


def hub_role_name(arn: str) -> str:
    """``arn:<partition>:iam::<acct>:role/<path/>name`` → ``name`` (what the interceptor
    compares the caller's role with)."""
    if ":role/" not in arn:
        raise AppError("policy.gateway_bundle_invalid",
                       "policy_hub_role_arn must be an IAM role ARN", status_code=500)
    return arn.rsplit("/", 1)[-1]


def engine_published(db: Session, engine: PolicyEngine) -> bool:
    """The Dogwood engine holds the hub's CURRENT ACTIVE set of this engine
    (``policy_engine_sync.engine_decides``) and the install left its ``engine_state``. The /3
    bundle carries no flag for it: a set the engine does not hold under the engine key answers
    ``set_mismatch`` at decide time and the Lambda fails closed."""
    from app.services import policy_engine_sync

    state = engine.engine_state
    return (isinstance(state, dict) and bool(state)
            and policy_engine_sync.engine_decides(db, engine))


def interceptor_governs(db: Session, workspace_id: str) -> bool:
    """The hub-side half of "the Gateway interceptor decides this workspace's tool calls": the
    workspace gateway is attached, its engine is published AND the bundle write after the
    install succeeded (``bundle_pending`` False). The AWS attach state is the caller's
    (``policy_enforcement.carrier_decision``)."""
    from app.services import policy_engines

    engine = policy_engines.engine_for_workspace_gateway(db, workspace_id)
    if engine is None or not engine_published(db, engine):
        return False
    state = engine.engine_state
    return not (isinstance(state, dict) and state.get("bundle_pending"))


def _attachment_state(db: Session, row: PolicyEngineAttachment | None) -> Any:
    """The bundle's attachment block of a gateway: ``None`` when not attached, else the
    engine's installed set facts under the attachment's mode. A migrated / legacy state (the
    set still sits under the workspace key) is described as ``unpublished`` with zero
    policies — the Lambda then denies (empty set) until the reconciler re-installs it."""
    from app.services import policy_engine_sync
    from app.services.policy_bundle import GatewayAttachmentState

    if row is None:
        return None
    engine = db.get(PolicyEngine, row.engine_id)
    if engine is None:
        return None
    state = engine.engine_state if isinstance(engine.engine_state, dict) else {}
    if policy_engine_sync.engine_state_is_legacy(state):
        state = {}
    return GatewayAttachmentState(
        engine_id=engine.id, mode=row.mode,
        active_count=int(state.get("active_count") or 0),
        temporal_count=int(state.get("temporal_count") or 0),
        policy_set_version=str(state.get("policy_set_version") or "") or UNPUBLISHED_SET,
        temporal_set_version=int(state.get("temporal_set_version") or 0),
        governed_actions=tuple(state.get("governed_actions") or ()),
        temporal_referenced_actions=tuple(state.get("temporal_referenced_actions") or ()),
    )


def _live_bundle(ws_ctx: Any, bucket: str, gateway_id: str) -> dict[str, Any] | None:
    """The bundle the pointer names now, else None (no pointer / unreadable)."""
    from app.policy.interceptor.bundle import pointer_key

    try:
        s3 = ws_ctx.client("s3")
        current = json.loads(s3.get_object(Bucket=bucket, Key=pointer_key(gateway_id))["Body"]
                             .read())
        return json.loads(s3.get_object(Bucket=bucket, Key=str(current["key"]))["Body"].read())
    except Exception:  # noqa: BLE001 — nothing live: the write decides
        return None


_VOLATILE = ("version", "issued_at")


def content_digest(bundle: dict[str, Any]) -> str:
    """sha256 of the canonical body of a bundle minus ``version`` / ``issued_at`` — what the
    drift comparison of the reconciler reads (lead note b: digests, not bodies)."""
    body = {k: v for k, v in bundle.items() if k not in _VOLATILE}
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False).encode("utf-8")).hexdigest()


def same_content(a: dict[str, Any] | None, b: dict[str, Any]) -> bool:
    """Two bundles describe the same posture (equal ``content_digest``)."""
    return isinstance(a, dict) and content_digest(a) == content_digest(b)


def _next_version(ws_ctx: Any, bucket: str, gateway_id: str) -> int:
    from app.policy.interceptor.bundle import pointer_key

    try:
        raw = ws_ctx.client("s3").get_object(Bucket=bucket, Key=pointer_key(gateway_id))
        held = json.loads(raw["Body"].read()).get("version")
    except Exception:  # noqa: BLE001 — no pointer yet (or unreadable): the CAS writer decides
        held = None
    return held + 1 if isinstance(held, int) and not isinstance(held, bool) else 1


def compose(db: Session, gateway_id: str, gateway_arn: str, *, version: int) -> dict[str, Any]:
    """The /3 bundle of ``gateway_id`` (validated by the Lambda's own parser before it is
    returned)."""
    from app.core import naming
    from app.services import policy_bundle, policy_engines, principal_mint

    settings = get_settings()
    signer = principal_mint._signer()
    return policy_bundle.build_gateway_bundle(
        gateway_id=gateway_id, gateway_arn=gateway_arn, version=version,
        env=naming.current_env_name(), hub_role=hub_role_name(settings.policy_hub_role_arn),
        pubkeys={signer.kid: signer.public_spki_b64()},
        role_map=policy_bundle.gateway_role_map(db),
        attachment=_attachment_state(db, policy_engines.attachment_of(db, gateway_id)),
        endpoints=list(settings.policy_engine_endpoints),
        shards=len(contracts.ENGINE_SHARD_PORTS),
    )


def _set_pending(db: Session, engine_id: str | None, pending: bool,
                 *, gateway_id: str, bundle_version: int | None = None) -> None:
    if not engine_id:
        return
    engine = db.get(PolicyEngine, engine_id)
    if engine is None:
        return
    state = dict(engine.engine_state or {})
    gateways = dict(state.get("gateway_bundles") or {})
    item = dict(gateways.get(gateway_id) or {})
    item["pending"] = pending
    if bundle_version is not None:
        item["version"] = bundle_version
    gateways[gateway_id] = item
    state["gateway_bundles"] = gateways
    state["bundle_pending"] = any(g.get("pending") for g in gateways.values())
    if bundle_version is not None and state.get("bundle_version") != bundle_version:
        state["bundle_version"] = bundle_version
    engine.engine_state = state


def publish_gateway(db: Session, gateway_id: str, *, trigger: str, operator: str | None,
                    workspace_id: str | None = None,
                    only_if_changed: bool = False) -> dict[str, Any] | None:
    """Compose + write the bundle of ``gateway_id``; ``None`` when nothing is to be written
    (bundle settings off, no bucket, or — with ``only_if_changed`` — the live bundle already
    says the same). ``workspace_id`` = the workspace whose journal takes the row (the engine's
    when attached; else the gateway owner's). Never raises: a failure is a ``gateway.bundle``
    failed journal row and ``bundle_pending`` stays true for a retry."""
    if not configured():
        return None
    with _PUBLISH_LOCK:
        return _publish_locked(db, gateway_id, trigger=trigger, operator=operator,
                               workspace_id=workspace_id, only_if_changed=only_if_changed)


def publish_engine_gateways(db: Session, engine: PolicyEngine, *, trigger: str,
                            operator: str | None) -> list[dict[str, Any]]:
    """After an install: the bundle of EVERY gateway attached to ``engine``."""
    from app.services import policy_engines

    if not configured():
        return []
    out = []
    attachments = policy_engines.attachments_of(db, engine.id)
    for row in attachments:
        _set_pending(db, engine.id, True, gateway_id=row.gateway_id)
    db.commit()
    for row in attachments:
        result = publish_gateway(db, row.gateway_id, trigger=trigger, operator=operator,
                                 workspace_id=engine.workspace_id)
        if result is not None:
            out.append(result)
    return out


def _publish_locked(db: Session, gateway_id: str, *, trigger: str, operator: str | None,
                    workspace_id: str | None, only_if_changed: bool) -> dict[str, Any] | None:
    from app.services import policy_bundle, policy_engines, policy_store

    attachment = policy_engines.attachment_of(db, gateway_id)
    engine = db.get(PolicyEngine, attachment.engine_id) if attachment is not None else None
    ws_for_journal = workspace_id or (engine.workspace_id if engine else None) \
        or _attachment_owners(db).get(gateway_id) or policy_engines.gateway_owner(db, gateway_id)
    if not ws_for_journal:
        return None
    if policy_engines.migration_pending(db, ws_for_journal):
        logger.warning("policy: gateway %s retains its old bundle while workspace %s migrates",
                       gateway_id, ws_for_journal)
        return None
    facts = policy_engines.gateway_facts(db, gateway_id, workspace_id=ws_for_journal)
    try:
        ws_ctx, bucket = facts["context"], facts["artifacts_bucket"]
        if not bucket or not facts["gateway_arn"]:
            raise AppError("policy.bundle_write_failed",
                           "artifacts_bucket / gateway_arn missing from the resource map",
                           status_code=409)
        bundle = compose(db, gateway_id, facts["gateway_arn"],
                         version=_next_version(ws_ctx, bucket, gateway_id))
        if only_if_changed:
            live = _live_bundle(ws_ctx, bucket, gateway_id)
            if live is None and bundle["attachment"] is None:
                return None  # never attached, nothing live: nothing to retract
            if same_content(live, bundle):
                _set_pending(db, engine.id if engine else None, False, gateway_id=gateway_id)
                db.commit()
                return None
        key, version = policy_bundle.write_gateway_bundle(ws_ctx, bucket, bundle)
    except Exception as exc:  # noqa: BLE001 — the install stands; the bundle is retried
        code = exc.code if isinstance(exc, AppError) else type(exc).__name__
        _set_pending(db, engine.id if engine else None, True, gateway_id=gateway_id)
        policy_store.journal(db, ws_for_journal, "gateway.bundle", operator=operator,
                             engine_id=engine.id if engine else None, status="failed",
                             requested={"trigger": trigger, "gateway_id": gateway_id},
                             error=str(code))
        db.commit()
        logger.warning("policy: gateway bundle of %s not written (%s) — retried later",
                       gateway_id, code)
        return None
    _set_pending(db, engine.id if engine else None, False,
                 gateway_id=gateway_id, bundle_version=version)
    result = {"gateway_id": gateway_id, "version": version, "key": key,
              "attachment": bundle["attachment"]}
    policy_store.journal(db, ws_for_journal, "gateway.bundle", operator=operator,
                         engine_id=engine.id if engine else None,
                         requested={"trigger": trigger}, after=result)
    db.commit()
    return result


def _attachment_owners(db: Session) -> dict[str, str]:
    """Attachment operation history retains the bucket owner after the relationship is gone."""
    owners: dict[str, str] = {}
    # Only low-volume attach/detach operations, not per-call audits or bundle publications.
    for row in db.query(PolicyChange).filter(PolicyChange.operation.in_(
            ("gateway.attach", "gateway.detach"))).order_by(PolicyChange.created_at.desc()).all():
        gateway_id = str((row.requested or {}).get("gateway_id")
                         or (row.after or {}).get("gateway_id") or "")
        if gateway_id:
            owners.setdefault(gateway_id, row.workspace_id)
    return owners


def _candidate_gateways(db: Session) -> dict[str, str | None]:
    """Every gateway the reconciler looks at → the engine attached (None when none): the
    attached gateways, every gateway whose interceptor the hub ever proved attached and every
    workspace's own gateway (a detached one may still hold a live bundle with an attachment to
    retract)."""
    from app.models.ledger import Workspace

    out: dict[str, str | None] = {}
    for row in db.query(PolicyEngineAttachment).all():
        out[row.gateway_id] = row.engine_id
    for row in db.query(PolicyGatewayAttachment).all():
        out.setdefault(row.gateway_id, None)
    for gateway_id in _attachment_owners(db):
        out.setdefault(gateway_id, None)
    for ws in db.query(Workspace).all():  # every workspace's own gateway
        gateway_id = str(((ws.resources or {}).get("gateway_id")) or "")
        if gateway_id:
            out.setdefault(gateway_id, None)
    return out


def reconcile(db: Session) -> list[str]:
    """Write the bundle of every gateway attached to an engine still ``bundle_pending`` (a
    write that failed after its install), and of every other candidate gateway whose live
    bundle no longer says what the hub would compose now (an attachment changed while the hub
    was down, a hand-written bundle, a legacy /2 bundle). An engine whose ``engine_state`` is
    legacy / migrated (N17-obs488; the workspace-era key) is re-installed first — that marks it
    pending, so its gateways' corrected bundles are written in the same pass. Returns the
    gateways written; at most ONE drift rewrite per pass, pending gateways always."""
    from app.services import policy_engine_sync, policy_engines

    if not configured():
        return []
    policy_engines.retry_migrations(db)
    pending: set[str] = set()
    for engine in db.query(PolicyEngine).filter(PolicyEngine.status != "deleted").all():
        state = engine.engine_state if isinstance(engine.engine_state, dict) else None
        if state and policy_engine_sync.engine_state_is_legacy(state) \
                and policy_engine_sync.enabled():
            try:
                policy_engine_sync.install_engine(db, engine, force=True)
                db.commit()
                logger.warning("policy: engine %s carried a legacy / migrated state — "
                               "re-installed as %s", engine.id,
                               (engine.engine_state or {}).get("policy_set_version"))
            except AppError:
                db.rollback()
                logger.exception("policy: re-install of engine %s failed; retried on the next "
                                 "reconcile", engine.id)
                continue
            state = engine.engine_state if isinstance(engine.engine_state, dict) else None
        if state and state.get("bundle_pending"):
            from app.services import policy_engines

            pending.update(a.gateway_id for a in policy_engines.attachments_of(db, engine.id))
    written: list[str] = []
    drift_written = False
    for gateway_id, _engine_id in sorted(_candidate_gateways(db).items()):
        if gateway_id not in pending and drift_written:
            continue  # lead note b: at most ONE drift rewrite per pass (pending ones always)
        if publish_gateway(db, gateway_id, trigger="reconcile", operator=None,
                           only_if_changed=gateway_id not in pending):
            written.append(gateway_id)
            if gateway_id not in pending:
                drift_written = True
    return written


def reconcile_once() -> list[str]:
    """One reconcile pass in its own session (the periodic reconciler and startup). Runs with
    the platform switch OFF too (review F2 / S4): a retracted attachment must reach the Lambda."""
    from app.core.db import SessionLocal

    if not configured():
        return []
    db = SessionLocal()
    try:
        return reconcile(db)
    finally:
        db.close()


def run_reconciler(stop: threading.Event, *,
                   wait: Callable[[float], bool] | None = None) -> None:
    """Every ``RECONCILE_EVERY_S`` (+ up to ``RECONCILE_JITTER_S`` of jitter, so several hub
    processes do not write in step): write the bundle of every gateway whose engine is
    ``bundle_pending`` or whose live bundle drifted. ``wait(seconds) -> stopped`` is the test
    seam."""
    wait = wait or stop.wait
    while not wait(RECONCILE_EVERY_S + random.uniform(0, RECONCILE_JITTER_S)):  # noqa: S311
        try:
            written = reconcile_once()
            if written:
                logger.info("policy: reconciled %d pending gateway bundle(s)", len(written))
        except Exception:  # noqa: BLE001 — the next pass tries again
            logger.exception("policy: gateway bundle reconcile failed")


_RECONCILER: threading.Thread | None = None
_STOP = threading.Event()


def start_reconciler() -> bool:
    """Start the periodic reconciler once per process (a daemon thread); ``False`` when it is
    already running or the bundle settings are off."""
    global _RECONCILER
    if not configured() or (_RECONCILER is not None and _RECONCILER.is_alive()):
        return False
    _STOP.clear()
    _RECONCILER = threading.Thread(target=run_reconciler, args=(_STOP,),
                                   name="policy-gateway-reconciler", daemon=True)
    _RECONCILER.start()
    return True
