"""Policy engines and gateway attachments — the Global AgentCore Policy model
(``docs/cn/interfaces/policy-engines.md``, user-confirmed requirement 2026-10-28).

* ``PolicyEngine`` (Global's PolicyEngine): a named container of policies, many per workspace.
  CRUD here; the policies themselves live in ``policy_store`` (``Policy.engine_id``).
* ``PolicyEngineAttachment`` (Global's ``Gateway.policyEngineConfiguration``): at most ONE
  engine per gateway with a gateway-level ``mode`` (``LOG_ONLY`` records, ``ENFORCE`` denies);
  one engine may be attached to many gateways. **Attach** = write that gateway's bundle
  (``policy_gateway_publish``) and attach Launchpad's interceptor Lambda to THAT gateway
  (``gateway_bootstrap.set_interceptor`` through ``interceptor_hook``); **detach** = bundle
  ``attachment: null`` + the interceptor detached. A gateway without an attachment is
  unguarded: its interceptor is not attached, nothing is decided.
* ``migrate`` (startup, idempotent): the pre-Global ledger (one implicit policy set per
  workspace, the workspace mode and the two switches) becomes one ``default_policy_engine``
  per workspace holding its policies, attached to the workspace gateway with the old mode when
  the old posture had the interceptor live; ``policy_engine.migrated`` journal row per workspace.

AWS touches go through two seams tests replace: ``interceptor_hook`` (the Gateway
UpdateGateway attach / detach + the verified-state record) and the bundle writer in
``policy_gateway_publish``. Both are inert while the platform switch
``Settings.policy_gateway_interceptor`` is off.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from botocore.exceptions import ClientError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import AppError, NotFoundError
from app.models.ledger import Workspace
from app.models.policy import (
    ATTACHMENT_MODES,
    Policy,
    PolicyChange,
    PolicyDecision,
    PolicyEngine,
    PolicyEngineAttachment,
    PolicySettings,
)
from app.schemas.policy import POLICY_NAME_PATTERN

log = logging.getLogger("launchpad.policy")

_NAME_RE = re.compile(POLICY_NAME_PATTERN)
DEFAULT_ENGINE_NAME = "default_policy_engine"
DEFAULT_ENGINE_DESCRIPTION = "默认策略引擎 — migrated from the workspace policy set"
# an attach / detach holds the gateway while its AWS update runs (409 `gateway.policy_engine_busy`)
_BUSY: set[str] = set()
_BUSY_LOCK = threading.Lock()


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


# ── engines ─────────────────────────────────────────────────────────────────


def list_engines(db: Session, workspace_id: str) -> list[PolicyEngine]:
    return list(db.scalars(
        select(PolicyEngine).where(PolicyEngine.workspace_id == workspace_id,
                                   PolicyEngine.status != "deleted")
        .order_by(PolicyEngine.created_at, PolicyEngine.id)).all())


def get_engine(db: Session, workspace_id: str, engine_id: str, *,
               include_deleted: bool = False) -> PolicyEngine:
    row = db.get(PolicyEngine, engine_id)
    if (row is None or row.workspace_id != workspace_id
            or (row.status == "deleted" and not include_deleted)):
        raise NotFoundError("policy_engine.not_found", f"policy engine {engine_id} was not found")
    return row


def find_by_name(db: Session, workspace_id: str, name: str) -> PolicyEngine | None:
    return db.scalars(select(PolicyEngine).where(
        PolicyEngine.workspace_id == workspace_id, PolicyEngine.name == name)).first()


def policy_count(db: Session, engine_id: str, *, status: str | None = None) -> int:
    stmt = select(func.count()).select_from(Policy).where(Policy.engine_id == engine_id)
    if status:
        stmt = stmt.where(Policy.status == status)
    return int(db.scalar(stmt) or 0)


def create_engine(
    db: Session, workspace_id: str, *, name: str, description: str = "",
    operator: str | None, operator_name: str | None = None,
) -> PolicyEngine:
    if not _NAME_RE.fullmatch(name or ""):
        raise AppError("policy_engine.invalid_name",
                       "name must match ^[A-Za-z][A-Za-z0-9_]{0,47}$", status_code=422)
    if find_by_name(db, workspace_id, name) is not None:
        raise AppError("policy_engine.name_taken",
                       f"a policy engine named {name!r} already exists in this workspace",
                       status_code=409)
    engine = PolicyEngine(workspace_id=workspace_id, name=name, description=description or "",
                          status="active", created_by=operator,
                          created_by_name=operator_name or operator)
    db.add(engine)
    db.flush()
    _journal(db, engine, "policy_engine.create", operator=operator,
             requested={"name": name, "description": description or ""},
             after={"id": engine.id, "status": engine.status})
    db.commit()
    db.refresh(engine)
    return engine


def update_engine(db: Session, workspace_id: str, engine_id: str, *,
                  description: str | None, operator: str | None) -> PolicyEngine:
    engine = get_engine(db, workspace_id, engine_id)
    before = {"description": engine.description}
    if description is not None and description != engine.description:
        engine.description = description
        engine.updated_at = _now()
        _journal(db, engine, "policy_engine.update", operator=operator, before=before,
                 requested={"description": description}, after={"description": description})
        db.commit()
        db.refresh(engine)
    return engine


def delete_engine(db: Session, workspace_id: str, engine_id: str, *, operator: str | None,
                  force: bool = False) -> None:
    """409 ``policy_engine.attached`` while any gateway is attached; 409
    ``policy_engine.has_policies`` unless ``force``. Archive retired policies and immutable
    versions under a deleted owner; release the live engine name."""
    from app.services import policy_store

    engine = get_engine(db, workspace_id, engine_id)
    attached = attachments_of(db, engine.id)
    if attached:
        raise AppError("policy_engine.attached",
                       "detach the engine from its gateways before deleting it",
                       {"gateway_ids": [a.gateway_id for a in attached]}, status_code=409)
    policies = list(db.scalars(select(Policy).where(Policy.engine_id == engine.id)).all())
    if policies and not force:
        raise AppError("policy_engine.has_policies",
                       "the engine still holds policies — retire / delete them, or pass "
                       "?force=true to retire them with the engine",
                       {"policy_ids": [p.id for p in policies]}, status_code=409)
    before = {"name": engine.name, "policies": [p.id for p in policies]}
    engine.status = "deleting"
    db.flush()
    for policy in policies:
        policy.status, policy.active_version = "retired", None
        policy_store.journal(
            db, workspace_id, "policy.retire", engine_id=engine.id, policy_id=policy.id,
            operator=operator, requested={"reason": "policy engine deleted"})
    _journal(db, engine, "policy_engine.delete", operator=operator, before=before,
             requested={"force": force}, after={"deleted": True})
    engine.engine_state = {**(engine.engine_state or {}), "deleted_name": engine.name}
    engine.name, engine.status = f"deleted_{engine.id}", "deleted"
    db.commit()
    from app.services import policy_cedar

    policy_cedar.invalidate(engine_id)


def _journal(db: Session, engine: PolicyEngine, operation: str, *, operator: str | None,
             before: dict[str, Any] | None = None, requested: dict[str, Any] | None = None,
             after: dict[str, Any] | None = None, status: str = "succeeded",
             error: str | None = None) -> PolicyChange:
    row = PolicyChange(workspace_id=engine.workspace_id, engine_id=engine.id, operation=operation,
                       operator=operator, status=status, before=before, requested=requested,
                       after=after, error=error)
    db.add(row)
    db.flush()
    return row


def last_engine_install(db: Session, engine_id: str) -> dict[str, Any] | None:
    """The newest ``engine.install`` journal row of the engine (ok or failed) — ``None``
    before the hub first installed its set."""
    row = db.scalars(
        select(PolicyChange).where(PolicyChange.engine_id == engine_id,
                                   PolicyChange.operation == "engine.install")
        .order_by(PolicyChange.created_at.desc(), PolicyChange.id.desc()).limit(1)
    ).first()
    if row is None:
        return None
    after = row.after if isinstance(row.after, dict) else {}
    requested = row.requested if isinstance(row.requested, dict) else {}
    return {"status": "ok" if row.status == "succeeded" else "failed",
            "trigger": requested.get("trigger"),
            "policy_set_version": after.get("policy_set_version"),
            "error": row.error, "operator": row.operator, "at": _iso(row.created_at)}


def engine_view(db: Session, engine: PolicyEngine) -> dict[str, Any]:
    """``GET /api/policy-engines`` item / ``GET /api/policy-engines/{id}``."""
    from app.services import policy_engine_sync

    state = engine.engine_state if isinstance(engine.engine_state, dict) else None
    seq = int(engine.policy_set_seq or 0)
    return {
        "id": engine.id, "name": engine.name, "description": engine.description,
        "status": engine.status, "workspace_id": engine.workspace_id,
        "policy_count": policy_count(db, engine.id),
        "active_policies": policy_count(db, engine.id, status="active"),
        "attachments": [attachment_summary(db, a) for a in attachments_of(db, engine.id)],
        # the Dogwood set installed for this engine (set id = the engine id)
        "policy_set_version": f"ps-{seq}" if seq > 0 else None,
        "temporal_set_version": int(engine.temporal_set_version or 0),
        "temporal_count": int(state.get("temporal_count") or 0) if state else 0,
        "has_user_permit": bool(state and state.get("has_user_permit")),
        "warnings": [] if state and state.get("has_user_permit") else ["no_user_permit"],
        "bundle_pending": bool(state.get("bundle_pending")) if state else False,
        "engine_decides": policy_engine_sync.engine_decides(db, engine),
        "last_install": last_engine_install(db, engine.id),
        "created_by": engine.created_by, "created_by_name": engine.created_by_name,
        "created_at": _iso(engine.created_at), "updated_at": _iso(engine.updated_at),
    }


def engine_status_view(db: Session, engine: PolicyEngine) -> dict[str, Any]:
    """``GET /api/policy-engines/{id}/status``: each shard's readiness, store epoch and the
    set it holds for THIS engine, whether that matches what the hub last installed. Never
    raises: an unreachable shard is ``ready: false`` with its error code; ``available: false``
    (no engine call) while the platform switch is off."""
    from app.services import policy_engine_control, policy_engine_sync

    if not policy_engine_sync.enabled():
        return {"available": False, "healthy": False, "shards": [],
                "engine": engine_view(db, engine)}
    try:
        raw = policy_engine_control.status()
    except Exception as exc:  # noqa: BLE001 — a health read must degrade, never 500
        log.warning("policy: engine status read failed (%s)", type(exc).__name__)
        raw = [{"shard": i, "ready": False, "error": "unavailable"}
               for i in range(policy_engine_control.shard_count())]
    seq = int(engine.policy_set_seq or 0)
    expected = f"ps-{seq}" if seq > 0 else None
    shards = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        sets = item.get("sets") if isinstance(item.get("sets"), dict) else {}
        held = sets.get(engine.id)
        shard: dict[str, Any] = {
            "shard": item.get("shard"), "ready": item.get("ready") is True,
            "store_epoch": item.get("store_epoch"),
            "policy_set_version": held if isinstance(held, str) else None,
            "in_sync": expected is not None and held == expected,
        }
        if item.get("error"):
            shard["error"] = str(item["error"])
        shards.append(shard)
    healthy = bool(shards) and all(s["ready"] for s in shards)
    return {"available": True, "healthy": healthy, "shards": shards,
            "engine": engine_view(db, engine)}


# ── attachments ─────────────────────────────────────────────────────────────


def attachments_of(db: Session, engine_id: str) -> list[PolicyEngineAttachment]:
    return list(db.scalars(
        select(PolicyEngineAttachment).where(PolicyEngineAttachment.engine_id == engine_id)
        .order_by(PolicyEngineAttachment.gateway_id)).all())


def attachment_of(db: Session, gateway_id: str) -> PolicyEngineAttachment | None:
    return db.get(PolicyEngineAttachment, gateway_id) if gateway_id else None


def engine_for_gateway(db: Session, gateway_id: str | None) -> PolicyEngine | None:
    row = attachment_of(db, gateway_id or "")
    return db.get(PolicyEngine, row.engine_id) if row is not None else None


def workspace_gateway(db: Session, workspace_id: str) -> tuple[str, str]:
    """``(gateway_id, gateway_arn)`` of the workspace's own Gateway ("" when not bootstrapped)."""
    row = db.get(Workspace, workspace_id)
    resources = (row.resources if row else None) or {}
    return str(resources.get("gateway_id") or ""), str(resources.get("gateway_arn") or "")


def engine_for_workspace_gateway(db: Session, workspace_id: str) -> PolicyEngine | None:
    """The engine attached to the workspace's own Gateway (what the agents of the workspace
    call) — the token mint reads its temporal-set version; None = unguarded."""
    gateway_id, _arn = workspace_gateway(db, workspace_id)
    return engine_for_gateway(db, gateway_id)


def gateway_owner(db: Session, gateway_id: str) -> str | None:
    """The workspace whose resource map names ``gateway_id`` (the bootstrap gateway)."""
    for row in db.query(Workspace).all():
        if str(((row.resources or {}).get("gateway_id")) or "") == gateway_id:
            return row.id
    return None


def gateway_facts(db: Session, gateway_id: str, *, workspace_id: str) -> dict[str, Any]:
    """What the bundle writer and the views need of a gateway: its owning workspace (else the
    engine's), the ARN, the name and the artifacts bucket its interceptor reads."""
    from app.services.workspace import context_for_workspace

    owner = gateway_owner(db, gateway_id) or workspace_id
    ws_row = db.get(Workspace, owner)
    resources = (ws_row.resources if ws_row else None) or {}
    arn = str(resources.get("gateway_arn") or "") if \
        str(resources.get("gateway_id") or "") == gateway_id else ""
    ctx = context_for_workspace(workspace_id)
    local_resources = ctx.resources or {}
    if not arn:
        arn = ctx.arn("bedrock-agentcore", f"gateway/{gateway_id}")
    name = str(resources.get("gateway_name") or "") if \
        str(resources.get("gateway_id") or "") == gateway_id else ""
    return {"gateway_id": gateway_id, "workspace_id": owner, "gateway_arn": arn,
            "gateway_name": name or None,
            "artifacts_bucket": str(local_resources.get("artifacts_bucket") or ""),
            "interceptor_function_arn": str(
                local_resources.get("policy_interceptor_function_arn")
                or ""),
            "context": ctx}


def gateway_snapshot(facts: dict[str, Any]) -> dict[str, Any]:
    """Read the actual gateway before any attachment mutation."""
    from app.services.agentcore.client import control_client
    from app.services.agentcore.gateway import get_gateway

    try:
        return get_gateway(control_client(facts["context"]), facts["gateway_id"])
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ResourceNotFoundException":
            raise NotFoundError("gateway.not_found", "gateway was not found") from None
        raise


def checked_gateway(db: Session, gateway_id: str, workspace_id: str, *,
                    mutation: bool = True) -> dict[str, Any]:
    facts = gateway_facts(db, gateway_id, workspace_id=workspace_id)
    ctx = facts["context"]
    arn_parts = facts["gateway_arn"].split(":")
    if len(arn_parts) > 4 and arn_parts[3] != ctx.region:
        raise AppError("gateway.policy_engine_region",
                       "the gateway and policy engine must be in the same region", status_code=422)
    if not get_settings().policy_gateway_interceptor:
        return facts
    detail = gateway_snapshot(facts)
    parts = str(detail.get("gatewayArn") or "").split(":")
    if len(parts) < 6 or parts[3] != ctx.region or parts[4] != ctx.account_id:
        raise AppError("gateway.policy_engine_region",
                       "the gateway must belong to the engine's account and region",
                       status_code=422)
    foreign = [str((entry.get("interceptor", {}).get("lambda") or {}).get("arn") or "")
               for entry in detail.get("interceptorConfigurations") or []]
    foreign = [arn for arn in foreign if arn != facts["interceptor_function_arn"]]
    facts.update(gateway_arn=detail["gatewayArn"], gateway_name=detail.get("name"),
                 gateway_url=detail.get("gatewayUrl"), role_arn=detail.get("roleArn"),
                 foreign=bool(foreign), foreign_lambda=foreign[0] if foreign else None,
                 live_attached=any(
                     (entry.get("interceptor", {}).get("lambda") or {}).get("arn")
                     == facts["interceptor_function_arn"]
                     for entry in detail.get("interceptorConfigurations") or []))
    if foreign and mutation:
        raise AppError("gateway.policy_engine_foreign",
                       "this gateway is managed by another environment",
                       {"foreign_lambda": foreign[0]}, status_code=409)
    return facts


def attachment_summary(db: Session, row: PolicyEngineAttachment) -> dict[str, Any]:
    engine = db.get(PolicyEngine, row.engine_id)
    owner = gateway_owner(db, row.gateway_id)
    ws_row = db.get(Workspace, owner) if owner else None
    resources = (ws_row.resources if ws_row else None) or {}
    return {"gateway_id": row.gateway_id,
            "gateway_name": str(resources.get("gateway_name") or "") or None,
            "engine_id": row.engine_id, "engine_name": engine.name if engine else None,
            "mode": row.mode, "attached_by": row.attached_by,
            "attached_at": _iso(row.attached_at), "updated_at": _iso(row.updated_at)}


def attachment_view(db: Session, gateway_id: str, *,
                    workspace_id: str | None = None) -> dict[str, Any]:
    """``GET /api/governance/gateways/{gid}/policy-engine``."""
    from app.services import policy_gateway_state

    row = attachment_of(db, gateway_id)
    interceptor = policy_gateway_state.attachment(db, gateway_id) or {}
    engine = db.get(PolicyEngine, row.engine_id) if row is not None else None
    state = engine.engine_state if engine is not None and isinstance(engine.engine_state, dict) \
        else None
    gateway_state = (state or {}).get("gateway_bundles", {}).get(gateway_id)
    bundle_version = (gateway_state or {}).get("version") if gateway_state is not None else \
        (state or {}).get("bundle_version")
    bundle_pending = bool(gateway_state.get("pending")) if gateway_state is not None else \
        bool((state or {}).get("bundle_pending"))
    out: dict[str, Any] = {
        "gateway_id": gateway_id, "attached": row is not None,
        "engine_id": row.engine_id if row else None,
        "engine_name": engine.name if engine else None,
        "mode": row.mode if row else None,
        "attached_by": row.attached_by if row else None,
        "attached_at": _iso(row.attached_at) if row else None,
        "interceptor": {
            "attached": bool(interceptor.get("attached")),
            "interceptor_arn": interceptor.get("interceptor_arn"),
            "attached_at": interceptor.get("attached_at"),
            "bundle_version": bundle_version,
            "bundle_pending": bundle_pending,
        },
    }
    if workspace_id is not None:
        facts = checked_gateway(db, gateway_id, workspace_id, mutation=False)
        out.update(foreign=facts.get("foreign", False),
                   foreign_lambda=facts.get("foreign_lambda"),
                   bundle_bucket=facts["artifacts_bucket"],
                   bundle_prefix=f"policy/gateway/{gateway_id}/")
        if "live_attached" in facts:
            out["interceptor"]["attached"] = facts["live_attached"]
    return out


def _busy(gateway_id: str) -> bool:
    with _BUSY_LOCK:
        if gateway_id in _BUSY:
            return True
        _BUSY.add(gateway_id)
        return False


def _free(gateway_id: str) -> None:
    with _BUSY_LOCK:
        _BUSY.discard(gateway_id)


def reset_busy() -> None:
    with _BUSY_LOCK:
        _BUSY.clear()


# The AWS half of attach / detach: `interceptor_hook(db, facts, attach) -> dict` performs the
# Gateway UpdateGateway (attach or detach Launchpad's interceptor Lambda on THAT gateway) and
# records the verified outcome (`policy_gateway_state`). The default is the real one below;
# tests replace it. Inert (`{"interceptor": "skipped", ...}`) while the platform switch is off
# or the workspace has no interceptor function.
InterceptorHook = Callable[[Session, dict[str, Any], bool], dict[str, Any]]


def _default_interceptor_hook(db: Session, facts: dict[str, Any], attach: bool) -> dict[str, Any]:
    from app.services import gateway_bootstrap, policy_gateway_state
    from app.services.agentcore.client import control_client

    if not get_settings().policy_gateway_interceptor:
        return {"interceptor": "skipped", "reason": "platform_switch_off"}
    function_arn = str(facts.get("interceptor_function_arn") or "")
    if not function_arn:
        raise AppError("gateway.policy_engine_not_ready",
                       "the policy interceptor function is not configured", status_code=409)
    gateway_id = str(facts["gateway_id"])

    def record(attached: bool) -> None:
        if attached:
            policy_gateway_state.record_attach(db, gateway_id, function_arn)
        else:
            policy_gateway_state.record_detach(db, gateway_id)

    if attach:
        _ensure_engine_key(facts)
        _grant_gateway_invoke(facts)
    report = gateway_bootstrap.set_interceptor(
        control_client(facts["context"]), gateway_id, function_arn, attach=attach, apply=True,
        record=record)
    return {"interceptor": report.get("action"), "function_arn": function_arn}


def _grant_gateway_invoke(facts: dict[str, Any]) -> None:
    """A function-specific inline policy preserves this role's other interceptor grants."""
    import hashlib

    role = str(facts.get("role_arn") or "")
    if not role:
        raise AppError("gateway.policy_engine_role", "gateway execution role is missing",
                       status_code=409)
    function = facts["interceptor_function_arn"]
    name = "launchpad-policy-interceptor-" + hashlib.sha256(function.encode()).hexdigest()[:16]
    iam = facts["context"].client("iam")
    iam.put_role_policy(
        RoleName=role.rsplit("/", 1)[-1], PolicyName=name,
        PolicyDocument=json.dumps({"Version": "2012-10-17", "Statement": [{
            "Sid": "InvokePolicyInterceptor", "Effect": "Allow",
            "Action": "lambda:InvokeFunction", "Resource": function}]}))


def _ensure_engine_key(facts: dict[str, Any]) -> None:
    """A second gateway on the same bucket needs the engine HMAC key under ITS prefix
    (``policy/gateway/<gw>/engine-key``): copy the owning workspace gateway's object when the
    attached gateway is not the owner's own. Missing keys refuse the attachment."""
    from app.policy.interceptor.bundle import engine_key_key

    bucket = str(facts.get("artifacts_bucket") or "")
    ctx = facts.get("context")
    if not bucket or ctx is None:
        raise AppError("gateway.policy_engine_not_ready", "interceptor bucket is missing",
                       status_code=409)
    gateway_id = str(facts["gateway_id"])
    source_gateway = str((ctx.resources or {}).get("gateway_id") or "")
    if not source_gateway:
        raise AppError("gateway.policy_engine_not_ready", "engine key source is missing",
                       status_code=409)
    s3 = ctx.client("s3")
    try:
        s3.head_object(Bucket=bucket, Key=engine_key_key(gateway_id))
        return
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") not in {"404", "NoSuchKey", "NotFound"}:
            raise
        if source_gateway == gateway_id:
            raise AppError("gateway.policy_engine_not_ready", "engine key is missing",
                           status_code=409) from exc
    s3.copy_object(Bucket=bucket, Key=engine_key_key(gateway_id),
                   CopySource={"Bucket": bucket, "Key": engine_key_key(source_gateway)})


interceptor_hook: InterceptorHook = _default_interceptor_hook


def attach(
    db: Session, workspace_id: str, gateway_id: str, *, engine_id: str, mode: str,
    operator: str | None,
) -> PolicyEngineAttachment:
    """``PUT /api/governance/gateways/{gid}/policy-engine``: idempotent. A change of engine or mode
    rewrites the gateway's bundle; the first attach (no row yet) also attaches the interceptor
    to that gateway. The ledger row is written first (the posture of record), then the bundle,
    then the interceptor — a failed AWS step is journalled ``gateway.attach`` ``failed`` and
    the reconciler retries the bundle; the interceptor state stays whatever the read-back
    proved (``policy_gateway_state``)."""
    if mode not in ATTACHMENT_MODES:
        raise AppError("gateway.policy_engine_mode_invalid",
                       f"mode must be one of {', '.join(ATTACHMENT_MODES)}", status_code=422)
    engine = get_engine(db, workspace_id, engine_id)
    if _busy(gateway_id):
        raise AppError("gateway.policy_engine_busy",
                       "an attach / detach of this gateway is still in flight", status_code=409)
    try:
        facts = checked_gateway(db, gateway_id, workspace_id)
        row = attachment_of(db, gateway_id)
        before = ({"engine_id": row.engine_id, "mode": row.mode} if row is not None
                  else {"engine_id": None, "mode": None})
        first = row is None
        membership_changed = first or row.engine_id != engine.id
        if row is None:
            row = PolicyEngineAttachment(gateway_id=gateway_id, engine_id=engine.id, mode=mode,
                                          attached_by=operator, attached_at=_now())
            db.add(row)
        else:
            from app.services import policy_gateway_state

            state = engine.engine_state if isinstance(engine.engine_state, dict) else {}
            if (row.engine_id == engine.id and row.mode == mode
                    and not state.get("bundle_pending")
                    and facts.get("live_attached",
                                  policy_gateway_state.gateway_interceptor_attached(
                                      db, gateway_id))):
                if not policy_gateway_state.gateway_interceptor_attached(db, gateway_id):
                    policy_gateway_state.record_attach(
                        db, gateway_id, facts["interceptor_function_arn"])
                return row
            previous_engine = row.engine_id
            row.engine_id, row.mode, row.updated_at = engine.id, mode, _now()
            if previous_engine != engine.id:
                from app.services import policy_cedar, policy_gateway_publish

                policy_cedar.invalidate(previous_engine)
                policy_gateway_publish._set_pending(
                    db, previous_engine, False, gateway_id=gateway_id)
        db.flush()
        if membership_changed:
            from app.services import policy_engine_sync

            try:
                policy_engine_sync.install_engine(db, engine)
            except Exception:
                db.rollback()
                raise
        db.commit()
        db.refresh(row)
        from app.services import policy_gateway_publish

        try:
            if membership_changed:
                policy_gateway_publish.publish_engine_gateways(
                    db, engine, trigger="gateway.attach", operator=operator)
            else:
                policy_gateway_publish.publish_gateway(
                    db, gateway_id, trigger="gateway.attach", operator=operator,
                    workspace_id=workspace_id)
            state = engine.engine_state if isinstance(engine.engine_state, dict) else {}
            if policy_gateway_publish.configured() and state.get("bundle_pending"):
                raise AppError("policy.bundle_write_failed", "gateway bundle is pending",
                               status_code=409)
            from app.services import policy_gateway_state

            if not facts.get("live_attached",
                             policy_gateway_state.gateway_interceptor_attached(db, gateway_id)):
                interceptor_hook(db, facts, True)
        except Exception as exc:
            _journal(db, engine, "gateway.attach", operator=operator, status="failed",
                     requested={"gateway_id": gateway_id}, error=type(exc).__name__)
            db.commit()
            if isinstance(exc, AppError):
                raise
            raise AppError("gateway.policy_engine_attach_failed",
                           "gateway attachment is incomplete; retry to finish",
                           status_code=409) from exc
        _journal(db, engine, "gateway.attach", operator=operator, before=before,
                 requested={"gateway_id": gateway_id, "engine_id": engine.id, "mode": mode},
                 after={"gateway_id": gateway_id, "engine_id": engine.id, "mode": mode,
                        "first_attach": first})
        db.commit()
        return row
    finally:
        _free(gateway_id)


def detach(db: Session, workspace_id: str, gateway_id: str, *, operator: str | None) -> bool:
    """``DELETE /api/governance/gateways/{gid}/policy-engine``: the bundle is rewritten with
    ``attachment: null`` and the interceptor detached from the gateway; decisions history is
    kept. ``False`` when nothing was attached (idempotent)."""
    from app.services import policy_gateway_publish, policy_gateway_state, policy_store

    if _busy(gateway_id):
        raise AppError("gateway.policy_engine_busy",
                       "an attach / detach of this gateway is still in flight", status_code=409)
    try:
        facts = checked_gateway(db, gateway_id, workspace_id)
        row = attachment_of(db, gateway_id)
        if row is None and not facts.get(
                "live_attached", policy_gateway_state.gateway_interceptor_attached(db, gateway_id)):
            if policy_gateway_state.gateway_interceptor_attached(db, gateway_id):
                policy_gateway_state.record_detach(db, gateway_id)
            return False
        engine = get_engine(db, workspace_id, row.engine_id) if row else None
        engine_id = engine.id if engine else None
        before = {"engine_id": engine_id, "mode": row.mode if row else None}
        if row is not None:
            db.delete(row)
            policy_gateway_publish._set_pending(db, engine_id, False, gateway_id=gateway_id)
        db.commit()
        try:
            result = policy_gateway_publish.publish_gateway(
                db, gateway_id, trigger="gateway.detach", operator=operator,
                workspace_id=workspace_id)
            if policy_gateway_publish.configured() and result is None:
                raise AppError("policy.bundle_write_failed", "gateway bundle is pending",
                               status_code=409)
            interceptor_hook(db, facts, False)
        except Exception as exc:  # noqa: BLE001
            policy_store.journal(db, workspace_id, "gateway.detach", operator=operator,
                                 engine_id=engine_id, status="failed",
                                 requested={"gateway_id": gateway_id}, error=type(exc).__name__)
            db.commit()
            if isinstance(exc, AppError):
                raise
            raise AppError("gateway.policy_engine_detach_failed",
                           "gateway detachment is incomplete; retry to finish",
                           status_code=409) from exc
        policy_store.journal(db, workspace_id, "gateway.detach", operator=operator,
                             engine_id=engine_id, before=before,
                             after={"gateway_id": gateway_id, "engine_id": None})
        db.commit()
        return True
    finally:
        _free(gateway_id)


# ── capabilities / overview ─────────────────────────────────────────────────


def capability_summary(db: Session, workspace_id: str) -> dict[str, Any]:
    """The ``policy`` block of ``GET /api/capabilities``: ``{engines, attached_gateways}``."""
    engines = list_engines(db, workspace_id)
    ids = [e.id for e in engines]
    attached = 0
    if ids:
        attached = int(db.scalar(select(func.count()).select_from(PolicyEngineAttachment)
                                 .where(PolicyEngineAttachment.engine_id.in_(ids))) or 0)
    return {"engines": len(engines), "attached_gateways": attached}


def overview_status(db: Session, workspace_id: str, *, engine_registered: bool) -> dict[str, Any]:
    """``services.policy`` is true iff the Cedar engine is registered and ≥ 1 gateway of the
    workspace's engines is attached with ≥ 1 ACTIVE policy; ``detail`` = ``"<n> engine(s) ·
    <m> gateway(s)"``."""
    summary = capability_summary(db, workspace_id)
    live = False
    for engine in list_engines(db, workspace_id):
        if attachments_of(db, engine.id) and policy_count(db, engine.id, status="active") > 0:
            live = True
            break
    return {"enabled": bool(engine_registered and live),
            "detail": f"{summary['engines']} engine(s) · {summary['attached_gateways']} gateway(s)",
            **summary}


# ── startup migration (idempotent, per workspace) ───────────────────────────


def _old_settings(db: Session, workspace_id: str) -> PolicySettings | None:
    return db.scalars(select(PolicySettings).where(
        PolicySettings.workspace_id == workspace_id)).first()


def _migrated(db: Session, workspace_id: str) -> bool:
    return db.scalars(select(PolicyChange.id).where(
        PolicyChange.workspace_id == workspace_id,
        PolicyChange.operation == "policy_engine.migrated").limit(1)).first() is not None


def migration_pending(db: Session, workspace_id: str) -> bool:
    """Orphaned policies are the durable indication that old enforcement must be retained."""
    return db.scalar(select(Policy.id).where(
        Policy.workspace_id == workspace_id, Policy.engine_id.is_(None)).limit(1)) is not None


def retry_migrations(db: Session) -> list[str]:
    """Retry each workspace independently; a failed migration cannot block healthy gateways."""
    migrated = []
    ids = list(db.scalars(select(Workspace.id).order_by(Workspace.created_at, Workspace.id)))
    for workspace_id in ids:
        try:
            migrated.extend(migrate(db, workspace_id=workspace_id))
        except Exception:  # noqa: BLE001 — preserve the old bundle and retry next pass
            db.rollback()
            log.exception("policy: migration of workspace %s failed; old bundles retained",
                          workspace_id)
    return migrated


def migrate(db: Session, *, workspace_id: str | None = None) -> list[str]:
    """Pre-Global ledger → the engine model, once per workspace (``policy_engine.migrated``
    journal row). Idempotent: a workspace already journalled is only re-checked for policies
    the migration has not visited (``engine_id`` NULL — none after a clean run). Returns the
    workspaces migrated in this pass."""
    from app.services import policy_gateway_state

    migrated: list[str] = []
    workspaces = select(Workspace).order_by(Workspace.created_at, Workspace.id)
    if workspace_id is not None:
        workspaces = workspaces.where(Workspace.id == workspace_id)
    for ws_row in db.scalars(workspaces).all():
        workspace_id = ws_row.id
        orphans = list(db.scalars(select(Policy).where(
            Policy.workspace_id == workspace_id, Policy.engine_id.is_(None))).all())
        already = _migrated(db, workspace_id)
        old = _old_settings(db, workspace_id)
        gateway_id, _arn = workspace_gateway(db, workspace_id)
        if already and not orphans:
            if (old is not None and old.mode == "off" and gateway_id
                    and attachment_of(db, gateway_id) is None
                    and get_settings().policy_gateway_interceptor
                    and policy_gateway_state.gateway_interceptor_attached(db, gateway_id)):
                detach(db, workspace_id, gateway_id, operator="migration")
            continue
        if not orphans:
            continue
        engines = list_engines(db, workspace_id)
        engine: PolicyEngine | None = None
        if orphans or (old is not None and not already):
            engine = next((e for e in engines if e.name == DEFAULT_ENGINE_NAME), None) or \
                (engines[0] if engines and orphans else None)
            if engine is None and (orphans or (old is not None and old.policy_set_seq)):
                engine = PolicyEngine(workspace_id=workspace_id, name=DEFAULT_ENGINE_NAME,
                                      description=DEFAULT_ENGINE_DESCRIPTION, status="active",
                                      created_by=None, created_by_name="migration")
                db.add(engine)
                db.flush()
        for policy in orphans:
            policy.engine_id = engine.id if engine is not None else None
            from app.services.policy_scope_migration import bake_scope

            bake_scope(db, policy)
        if engine is not None and old is not None and not already:
            # the engine-set facts of the workspace era: carried as a MIGRATED state so the
            # reconciler re-installs the set under the engine key (the Dogwood store still holds
            # it under the workspace key) and rewrites the bundle
            engine.policy_set_seq = int(old.policy_set_seq or 0)
            engine.temporal_set_version = int(old.temporal_set_version or 0)
            engine.temporal_digest = old.temporal_digest or ""
            engine.engine_state = {**(old.engine_state or {}), "bundle_pending": True,
                                   "migrated_from_workspace": workspace_id}
        gateway_id, _arn = workspace_gateway(db, workspace_id)
        attachment_made = None
        if engine is not None and gateway_id and not already and old is not None:
            live = (bool(old.gateway_interceptor) and bool(old.engine_publish)
                    and policy_gateway_state.gateway_interceptor_attached(db, gateway_id)
                    and old.mode in ("log_only", "enforce"))
            if live and attachment_of(db, gateway_id) is None:
                db.add(PolicyEngineAttachment(
                    gateway_id=gateway_id, engine_id=engine.id,
                    mode="ENFORCE" if old.mode == "enforce" else "LOG_ONLY",
                    attached_by="migration", attached_at=_now()))
                attachment_made = "ENFORCE" if old.mode == "enforce" else "LOG_ONLY"
        if gateway_id:
            # every gateway decision row of the workspace names its gateway (and engine)
            db.query(PolicyDecision).filter(
                PolicyDecision.workspace_id == workspace_id, PolicyDecision.source == "gateway",
                PolicyDecision.gateway_id.is_(None)).update(
                {PolicyDecision.gateway_id: gateway_id}, synchronize_session=False)
        if engine is not None:
            db.query(PolicyDecision).filter(
                PolicyDecision.workspace_id == workspace_id,
                PolicyDecision.engine_id.is_(None)).update(
                {PolicyDecision.engine_id: engine.id}, synchronize_session=False)
            db.query(PolicyChange).filter(
                PolicyChange.workspace_id == workspace_id, PolicyChange.engine_id.is_(None),
                PolicyChange.operation != "policy_engine.migrated").update(
                {PolicyChange.engine_id: engine.id}, synchronize_session=False)
        if not already:
            db.add(PolicyChange(
                workspace_id=workspace_id, engine_id=engine.id if engine else None,
                operation="policy_engine.migrated", operator="migration",
                before={"mode": old.mode if old else None,
                        "gateway_interceptor": bool(old.gateway_interceptor) if old else None,
                        "engine_publish": bool(old.engine_publish) if old else None,
                        "policies": len(orphans)},
                after={"engine_id": engine.id if engine else None,
                       "engine_name": engine.name if engine else None,
                       "gateway_id": gateway_id or None, "attachment_mode": attachment_made}))
        db.commit()
        if (old is not None and old.mode == "off" and gateway_id
                and attachment_of(db, gateway_id) is None
                and get_settings().policy_gateway_interceptor
                and policy_gateway_state.gateway_interceptor_attached(db, gateway_id)):
            detach(db, workspace_id, gateway_id, operator="migration")
        migrated.append(workspace_id)
        log.info("policy: workspace %s migrated to the engine model (engine=%s, policies=%d, "
                 "attachment=%s)", workspace_id, engine.id if engine else None, len(orphans),
                 attachment_made)
    return migrated


def migrate_once() -> list[str]:
    """``migrate`` in its own session (startup). Never raises."""
    from app.core.db import SessionLocal

    db = SessionLocal()
    try:
        return retry_migrations(db)
    except Exception:  # noqa: BLE001 — the next start retries
        db.rollback()
        log.exception("policy: engine-model migration failed")
        return []
    finally:
        db.close()
