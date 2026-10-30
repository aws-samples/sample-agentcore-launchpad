"""Policy store (Global model, ``docs/cn/interfaces/policy-engines.md``): ledger CRUD of the
policies of a policy engine, versioning, publish with an immutable S3 copy, the operator
journal and the decision audit rows.

Plain module — no Protocol. The one AWS touch is the publish-time copy
``s3://<artifacts>/policy/policies/<policy_id>/v<n>.cedar`` through the client funnel
(``workspace.client("s3")``, ``put_object(IfNoneMatch="*")`` so a key is never rewritten).
Every mutation that changes what an engine evaluates (publish / retire / delete /
enforcement mode) installs the engine's ACTIVE set on the Dogwood engine BEFORE it commits
(``policy_engine_sync.install_engine``; a refusal rolls the change back), drops the engine's
compiled cedarpy set (``policy_cedar.invalidate``), writes the bundle of every gateway the
engine is attached to (``policy_gateway_publish``) and fires the registered change listeners.

The retired workspace settings (mode / switches / evidence / confirmation) have no functions
here any more; the gateway attachment carries the mode (``services/policy_engines``).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from botocore.exceptions import ClientError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.errors import AppError, NotFoundError
from app.models.policy import (
    ENFORCEMENT_MODES,
    Policy,
    PolicyChange,
    PolicyDecision,
    PolicyEngine,
    PolicyVersion,
)
from app.policy import schema_digest, schema_json
from app.schemas.policy import MAX_STATEMENT_CHARS, POLICY_NAME_PATTERN
from app.services import policy_audit, policy_cedar
from app.services.policy_backend import ValidationReport, get_backend
from app.services.workspace import WorkspaceContext

log = logging.getLogger("launchpad.policy")

S3_PREFIX = "policy"
_NAME_RE = re.compile(POLICY_NAME_PATTERN)

ChangeListener = Callable[[dict[str, Any]], None]
_listeners: list[ChangeListener] = []


def register_change_listener(fn: ChangeListener) -> None:
    """Called after every evaluation-relevant change with
    ``{event: publish|retire|delete|enforcement_mode, workspace_id, engine_id, policy_id?,
    version?}``."""
    if fn not in _listeners:
        _listeners.append(fn)


def clear_change_listeners() -> None:
    _listeners.clear()


def _changed(event: str, engine: PolicyEngine, **fields: Any) -> None:
    policy_cedar.invalidate(engine.id)
    payload = {"event": event, "workspace_id": engine.workspace_id, "engine_id": engine.id,
               **fields}
    for fn in list(_listeners):
        try:
            fn(payload)
        except Exception:  # a listener must never break the mutation it observes
            log.exception("policy change listener failed for %s", payload)


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


# ── journal ────────────────────────────────────────────────────────────────


def journal(
    db: Session,
    workspace_id: str,
    operation: str,
    *,
    operator: str | None,
    engine_id: str | None = None,
    policy_id: str | None = None,
    version: int | None = None,
    status: str = "succeeded",
    before: dict[str, Any] | None = None,
    requested: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    override_reason: str | None = None,
    error: str | None = None,
) -> PolicyChange:
    row = PolicyChange(
        workspace_id=workspace_id, engine_id=engine_id, policy_id=policy_id, version=version,
        operation=operation, operator=operator, status=status, before=before,
        requested=requested, after=after, override_reason=override_reason, error=error,
    )
    db.add(row)
    db.flush()
    return row


def list_changes(db: Session, workspace_id: str, *, engine_id: str | None = None,
                 limit: int = 200) -> list[PolicyChange]:
    stmt = select(PolicyChange).where(PolicyChange.workspace_id == workspace_id)
    if engine_id:
        stmt = stmt.where(PolicyChange.engine_id == engine_id)
    return list(db.scalars(stmt.order_by(PolicyChange.created_at.desc()).limit(limit)).all())


def change_view(row: PolicyChange) -> dict[str, Any]:
    return {
        "id": row.id, "engine_id": row.engine_id, "policy_id": row.policy_id,
        "version": row.version,
        "operation": row.operation, "operator": row.operator, "status": row.status,
        "before": row.before, "requested": row.requested, "after": row.after,
        "override_reason": row.override_reason, "error": row.error,
        "created_at": _iso(row.created_at),
    }


def _publish_gateways(db: Session, engine: PolicyEngine, *, trigger: str,
                      operator: str | None) -> None:
    """After the change committed: the bundle of every gateway attached to the engine (never
    raises; a no-op unless the bundle settings are on)."""
    from app.services import policy_gateway_publish

    policy_gateway_publish.publish_engine_gateways(db, engine, trigger=trigger, operator=operator)


def _engine_install(
    db: Session, engine: PolicyEngine, *, operation: str, policy_id: str | None,
    version: int | None, operator: str | None, before: dict[str, Any],
) -> dict[str, Any] | None:
    """Compile + install the pending ACTIVE set on the Dogwood engine BEFORE the caller commits
    (``policy_engine_sync.install_engine``; a no-op while the platform switch is off). A
    failure rolls the change back, journals ``engine.install`` and the operation ``failed`` and
    re-raises — the previous set stays live everywhere."""
    from app.services import policy_engine_sync

    try:
        result = policy_engine_sync.install_engine(db, engine)
    except AppError as exc:
        db.rollback()
        journal(db, engine.workspace_id, "engine.install", engine_id=engine.id,
                policy_id=policy_id, version=version, operator=operator, status="failed",
                before=before, requested={"trigger": operation}, error=exc.message)
        journal(db, engine.workspace_id, operation, engine_id=engine.id, policy_id=policy_id,
                version=version, operator=operator, status="failed", before=before,
                error=exc.code)
        db.commit()
        raise
    if result is not None:
        journal(db, engine.workspace_id, "engine.install", engine_id=engine.id,
                policy_id=policy_id, version=version, operator=operator,
                requested={"trigger": operation}, after=result)
    return result


# ── validation ─────────────────────────────────────────────────────────────

_GLOBAL_VOCABULARY = re.compile(r"\bAgentCore::|\btemporal\b")


def detect_dialect(statement: str) -> str:
    """The API has no ``dialect`` field (console contract 2026-10-28): a statement in Global's
    vocabulary (``AgentCore::…``) or with a temporal clause is compiled on the policy engine
    (``dogwood``); anything else is a D20 Launchpad-vocabulary statement validated by cedarpy
    (``cedar``, rewritten for the engine on install)."""
    return "dogwood" if _GLOBAL_VOCABULARY.search(statement or "") else "cedar"



def validate_statement(
    statement: str, *, dialect: str = "cedar", db: Session | None = None,
    engine: PolicyEngine | None = None, policy_id: str | None = None,
) -> ValidationReport:
    return _validated(statement, dialect=dialect, db=db, engine=engine, policy_id=policy_id)[0]


def _validated(
    statement: str, *, dialect: str = "cedar", db: Session | None = None,
    engine: PolicyEngine | None = None, policy_id: str | None = None,
) -> tuple[ValidationReport, str]:
    """``(report, kind)``. D20 first checks the Launchpad schema, then uses the install rewrite
    and compiler when engine routing is enabled. Both dialects compile against the engine's
    actual Gateway tools with the other ACTIVE policies (including T8 quotas). Without routing,
    D20 reports only ``launchpad_schema`` validation; Dogwood requires routing."""
    if len(statement) > MAX_STATEMENT_CHARS:
        raise AppError(
            "policy.invalid_statement", f"statement exceeds {MAX_STATEMENT_CHARS} characters",
            status_code=422,
        )
    if dialect == "cedar":
        from app.services import policy_engine_sync

        report = get_backend().validate(statement, schema_json())
        if not report.ok or db is None or engine is None or not policy_engine_sync.enabled():
            return report, "stateless"
        rewritten, warnings = policy_engine_sync.engine_statement(statement, dialect)
        report, kind = _dogwood_report(db, engine, rewritten, policy_id=policy_id)
        report.warnings = [*warnings, *report.warnings]
        return report, kind
    if dialect != "dogwood" or db is None or engine is None:
        raise AppError("policy.invalid_dialect", f"unknown statement language {dialect!r}")
    return _dogwood_report(db, engine, statement, policy_id=policy_id)


def _dogwood_report(
    db: Session, engine: PolicyEngine, statement: str, *, policy_id: str | None,
) -> tuple[ValidationReport, str]:
    from app.services import policy_engine_sync
    from app.services.policy_backend import ValidationIssue

    if not policy_engine_sync.enabled():
        raise AppError(
            "policy.engine_not_routed",
            "Dogwood statements are accepted only while the platform installs policies on the "
            "policy engine (Settings.policy_gateway_interceptor)", status_code=409,
        )
    draft_id = policy_id or "pol_draft"
    others = [p for p in policy_engine_sync.active_set(db, engine.id) if p["id"] != draft_id]
    # the two failure sources are told apart (`source`) — the Gateway `tools/list` (the schema
    # source) and the compiler itself (binary missing / no answer / refused)
    try:
        tools = policy_engine_sync.engine_tools_list(db, engine)
    except Exception as exc:  # noqa: BLE001 — a transport error on the way to the Gateway
        raise policy_engine_sync.schema_read_error(exc, "policy.compiler_unavailable") from None
    try:
        compiled = policy_engine_sync.compiler().compile(
            tools, [*others, {"id": draft_id, "version": 0, "mode": "active",
                              "statement": statement}])
    except AppError as exc:
        raise AppError("policy.compiler_unavailable", "the policy compiler did not answer",
                       {"source": "compiler", "reason": exc.code}, status_code=409) from None
    except Exception as exc:  # noqa: BLE001 — EngineUnavailable: binary missing / no answer
        raise AppError("policy.compiler_unavailable", "the policy compiler did not answer",
                       {"source": "compiler", "reason": type(exc).__name__},
                       status_code=409) from None
    errors = [
        ValidationIssue(message=str(e.get("message") or e), policy_id=e.get("policy_id") or None)
        if isinstance(e, dict) else ValidationIssue(message=str(e))
        for e in (compiled.errors or [])
    ]
    warnings = [str(w.get("message") or w) if isinstance(w, dict) else str(w)
                for w in (compiled.warnings or [])]
    mine = [i for i in (compiled.items or [])
            if str(i.get("id", "")).split("#", 1)[0] == draft_id]
    kind = "temporal" if any(i.get("kind") == "temporal" for i in mine) else "stateless"
    return ValidationReport(ok=bool(compiled.ok) and not errors, errors=errors,
                            warnings=warnings, scope="gateway_active_set"), kind


_INVOKE_ACTION = re.compile(r'Launchpad::Action::"invoke"')
INVOKE_RETIRED_MESSAGE = (
    "the invoke action no longer exists — policies decide Gateway tool calls only (contract "
    "amendment 9); remove the invoke statement(s), or retire this policy and publish the "
    "permit_workspace_agents_call_tools / permit_users_call_tools baselines instead"
)


def _require_valid(
    statement: str, *, dialect: str = "cedar", db: Session | None = None,
    engine: PolicyEngine | None = None, policy_id: str | None = None,
) -> tuple[ValidationReport, str]:
    if dialect == "cedar" and _INVOKE_ACTION.search(statement):
        # amendment 9: the schema has no `invoke` action any more, so the statement cannot
        # validate — name the reason instead of the generic schema report
        raise AppError(
            "policy.invalid_statement", INVOKE_RETIRED_MESSAGE,
            {"ok": False, "reason": "invoke_retired",
             "errors": [{"message": INVOKE_RETIRED_MESSAGE, "policy_id": None}],
             "warnings": [], "policy_ids": []},
            status_code=422,
        )
    report, kind = _validated(statement, dialect=dialect, db=db, engine=engine,
                              policy_id=policy_id)
    if not report.ok:
        raise AppError(
            "policy.invalid_statement", "the statement did not validate against the "
            "Launchpad schema" if report.scope == "launchpad_schema" else "the statement does not "
            "compile against the engine's Gateway tools", report.as_dict(), status_code=422,
        )
    return report, kind


# ── policies & versions ───────────────────────────────────────────────────


def default_engine(db: Session, workspace_id: str, *, create: bool = True) -> PolicyEngine | None:
    """The workspace's ``default_policy_engine`` (the one the startup migration creates for a
    pre-Global ledger), created on first use when ``create`` — the engine the convenience
    callers below resolve a bare workspace id to."""
    from app.services import policy_engines

    engine = policy_engines.find_by_name(db, workspace_id, policy_engines.DEFAULT_ENGINE_NAME)
    if engine is None and create:
        engine = PolicyEngine(workspace_id=workspace_id, name=policy_engines.DEFAULT_ENGINE_NAME,
                              description=policy_engines.DEFAULT_ENGINE_DESCRIPTION,
                              status="active", created_by=None, created_by_name="system")
        db.add(engine)
        db.flush()
    if engine is not None:
        # the migration rule: a policy row of the workspace that names no engine (written by a
        # pre-engine writer) belongs to the default engine
        db.query(Policy).filter(Policy.workspace_id == workspace_id,
                                Policy.engine_id.is_(None)).update(
            {Policy.engine_id: engine.id}, synchronize_session="fetch")
        db.flush()
    return engine


def settings_row(db: Session, workspace_id: str) -> PolicyEngine:
    """Pre-engine alias: the workspace's default engine row (the engine-set state —
    ``policy_set_seq``, ``temporal_set_version``, ``engine_state`` — lived on the per-workspace
    settings row before the Global model). Operator scripts and the older tests read it."""
    engine = default_engine(db, workspace_id, create=True)
    assert engine is not None
    return engine


def _engine_arg(db: Session, engine: PolicyEngine | str) -> PolicyEngine:
    """A ``PolicyEngine`` row, an engine id, or a WORKSPACE id (→ its default engine, created
    on first use). Routers pass the row; the deploy / test helpers may pass a workspace id."""
    if isinstance(engine, PolicyEngine):
        return engine
    row = db.get(PolicyEngine, engine)
    if row is not None:
        return row
    created = default_engine(db, engine, create=True)
    assert created is not None
    return created


def list_policies(db: Session, engine: PolicyEngine | str, *,
                  status: str | None = None) -> list[Policy]:
    """The policies of an engine (a workspace id lists the whole workspace's)."""
    if isinstance(engine, PolicyEngine) or db.get(PolicyEngine, engine) is not None:
        engine_id = engine.id if isinstance(engine, PolicyEngine) else engine
        stmt = select(Policy).where(Policy.engine_id == engine_id)
    else:
        stmt = select(Policy).where(Policy.workspace_id == engine)
    if status:
        stmt = stmt.where(Policy.status == status)
    return list(db.scalars(stmt.order_by(Policy.created_at, Policy.id)).all())


def list_workspace_policies(db: Session, workspace_id: str) -> list[Policy]:
    """Every policy of the workspace, engine by engine (the deny-card name lookup)."""
    return list(db.scalars(select(Policy).where(Policy.workspace_id == workspace_id)
                           .order_by(Policy.created_at, Policy.id)).all())


def get_policy(db: Session, workspace_id: str, policy_id: str,
               engine_id: str | None = None) -> Policy:
    """The policy (404 ``policy.not_found`` outside the workspace — or outside ``engine_id``
    when given: a policy is addressed under its engine)."""
    row = db.get(Policy, policy_id)
    if row is None or row.workspace_id != workspace_id or (
            engine_id is not None and row.engine_id != engine_id):
        raise NotFoundError("policy.not_found", f"policy {policy_id} was not found")
    return row


def engine_of(db: Session, policy: Policy) -> PolicyEngine:
    engine = db.get(PolicyEngine, policy.engine_id or "")
    if engine is None:
        raise AppError("policy_engine.not_found",
                       f"policy {policy.id} belongs to no policy engine (migration pending)",
                       status_code=409)
    return engine


def find_by_name(db: Session, engine_id: str, name: str) -> Policy | None:
    return db.scalars(
        select(Policy).where(Policy.engine_id == engine_id, Policy.name == name)
    ).first()


def list_versions(db: Session, policy: Policy) -> list[PolicyVersion]:
    return list(
        db.scalars(
            select(PolicyVersion)
            .where(PolicyVersion.policy_id == policy.id)
            .order_by(PolicyVersion.version)
        ).all()
    )


def get_version(db: Session, policy: Policy, version: int) -> PolicyVersion:
    row = db.scalars(
        select(PolicyVersion).where(
            PolicyVersion.policy_id == policy.id, PolicyVersion.version == version
        )
    ).first()
    if row is None:
        raise NotFoundError(
            "policy.version_not_found", f"policy {policy.id} has no version {version}"
        )
    return row


def active_version(db: Session, policy: Policy) -> PolicyVersion | None:
    if policy.active_version is None:
        return None
    return get_version(db, policy, policy.active_version)


def current_version(db: Session, policy: Policy) -> PolicyVersion | None:
    """The version the editor shows: the draft when one exists, else the active one."""
    if policy.draft_version is not None:
        return get_version(db, policy, policy.draft_version)
    return active_version(db, policy)


def active_policies(db: Session, engine: PolicyEngine | str) -> list[Policy]:
    return list_policies(db, engine, status="active")


def active_count(db: Session, engine: PolicyEngine | str) -> int:
    return len(active_policies(db, engine))


def version_view(row: PolicyVersion) -> dict[str, Any]:
    return {
        "id": row.id, "policy_id": row.policy_id,
        "version": row.version, "statement": row.statement, "schema_digest": row.schema_digest,
        "validation": row.validation, "authored_by": row.authored_by,
        "created_at": _iso(row.created_at), "published_at": _iso(row.published_at),
        "published_by": row.published_by, "s3_key": row.s3_key,
        "kind": row.kind or "stateless",
    }


def policy_view(db: Session, policy: Policy, *, versions: bool = False) -> dict[str, Any]:
    current = current_version(db, policy)
    active = active_version(db, policy)
    shown = active or current
    out: dict[str, Any] = {
        "id": policy.id, "engine_id": policy.engine_id, "name": policy.name,
        "description": policy.description,
        "status": policy.status, "active_version": policy.active_version,
        "draft_version": policy.draft_version,
        # Global `enforcementMode` (active | log_only) — always shown since the Global model
        "enforcement_mode": policy.enforcement_mode or "active",
        "statement": current.statement if current else "",
        "validation": current.validation if current else None,
        "policy_ids": (current.validation or {}).get("policy_ids", []) if current else [],
        "kind": (shown.kind if shown is not None else None) or "stateless",
        "published_at": _iso(active.published_at) if active else None,
        "s3_key": active.s3_key if active else None,
        "created_by": policy.created_by, "created_at": _iso(policy.created_at),
        "updated_at": _iso(policy.updated_at),
    }
    if versions:
        out["versions"] = [version_view(v) for v in list_versions(db, policy)]
    return out


def policy_names(db: Session, workspace_id: str, policy_ids: list[str]) -> dict[str, str]:
    """``{policy_id: name}`` for the ids of ``workspace_id`` that exist (the console's deny
    card names the policy — a decision row / stream frame carries only ids)."""
    ids = [str(i).split(":", 1)[0] for i in policy_ids if i]
    if not ids:
        return {}
    rows = db.scalars(select(Policy).where(Policy.id.in_(ids),
                                           Policy.workspace_id == workspace_id)).all()
    return {row.id: row.name for row in rows}


def set_enforcement_mode(
    db: Session, engine: PolicyEngine | str, policy_id: str, enforcement_mode: str, *,
    operator: str | None, is_admin: bool | None = None,
) -> Policy:
    """One policy's enforcement (Global ``enforcementMode``) — ``active`` or ``log_only``
    (records what it would deny). An ACTIVE policy is re-installed at once (``409`` and rolled
    back when the engine refuses, like a publish); journalled ``policy.enforcement_mode``.
    ``is_admin`` is accepted and ignored (the route table carries the permission now)."""
    if enforcement_mode not in ENFORCEMENT_MODES:
        raise AppError("policy.invalid_mode", f"unknown enforcement mode {enforcement_mode!r}",
                       status_code=422)
    engine = _engine_of_arg(db, engine, policy_id)
    policy = get_policy(db, engine.workspace_id, policy_id, engine.id)
    before = {"enforcement_mode": policy.enforcement_mode or "active"}
    if before["enforcement_mode"] == enforcement_mode:
        return policy
    policy.enforcement_mode = enforcement_mode
    policy.updated_at = _now()
    installed = None
    if policy.status == "active":
        installed = _engine_install(db, engine, operation="policy.enforcement_mode",
                                    policy_id=policy.id, version=policy.active_version,
                                    operator=operator, before=before)
    journal(db, engine.workspace_id, "policy.enforcement_mode", engine_id=engine.id,
            policy_id=policy.id, version=policy.active_version, operator=operator, before=before,
            requested={"enforcement_mode": enforcement_mode},
            after={"enforcement_mode": enforcement_mode})
    db.commit()
    db.refresh(policy)
    _changed("enforcement_mode", engine, policy_id=policy.id, version=policy.active_version)
    if installed is not None:
        _publish_gateways(db, engine, trigger="policy.enforcement_mode", operator=operator)
        db.refresh(policy)
    return policy


def _new_version(
    db: Session, policy: Policy, statement: str, report: ValidationReport, operator: str | None,
    *, dialect: str = "cedar", kind: str = "stateless",
) -> PolicyVersion:
    last = db.scalar(
        select(func.max(PolicyVersion.version)).where(PolicyVersion.policy_id == policy.id)
    ) or 0
    row = PolicyVersion(
        workspace_id=policy.workspace_id, policy_id=policy.id, version=int(last) + 1,
        statement=statement, schema_digest=schema_digest(), validation=report.as_dict(),
        authored_by=operator, dialect=dialect, kind=kind,
    )
    db.add(row)
    db.flush()
    policy.draft_version = row.version
    return row


def _engine_of_arg(db: Session, engine: PolicyEngine | str, policy_id: str) -> PolicyEngine:
    """``_engine_arg``, except that a WORKSPACE id resolves to the engine the policy already
    belongs to (a policy is addressed under its engine)."""
    if isinstance(engine, PolicyEngine):
        return engine
    row = db.get(PolicyEngine, engine)
    if row is not None:
        return row
    policy = get_policy(db, engine, policy_id)
    return engine_of(db, policy)


def create_policy(
    db: Session,
    engine: PolicyEngine | str,
    *,
    name: str,
    statement: str,
    operator: str | None,
    description: str = "",
    prefill: str | None = None,
    dialect: str = "cedar",
    enforcement_mode: str | None = None,
) -> Policy:
    engine = _engine_arg(db, engine)
    if not _NAME_RE.fullmatch(name or ""):
        raise AppError("policy.invalid_name", "name must match ^[A-Za-z][A-Za-z0-9_]{0,47}$")
    if find_by_name(db, engine.id, name) is not None:
        raise AppError(
            "policy.name_taken", f"a policy named {name!r} already exists in this engine",
            status_code=409,
        )
    if enforcement_mode is not None and enforcement_mode not in ENFORCEMENT_MODES:
        raise AppError("policy.invalid_mode", f"unknown enforcement mode {enforcement_mode!r}",
                       status_code=422)
    report, kind = _require_valid(statement, dialect=dialect, db=db, engine=engine)
    # Global parity (GLOBAL-DEVIATIONS item 5, lead ruling 2026-10-27): a new policy starts
    # `log_only` unless the request says otherwise — its first publish records what it would
    # decide without deciding; the operator promotes it with `set_enforcement_mode(active)`.
    policy = Policy(
        workspace_id=engine.workspace_id, engine_id=engine.id, name=name,
        description=description or "", status="draft", created_by=operator,
        enforcement_mode=enforcement_mode or "log_only",
    )
    db.add(policy)
    db.flush()
    version = _new_version(db, policy, statement, report, operator, dialect=dialect, kind=kind)
    requested = {"name": name, "prefill": prefill, "enforcement_mode": policy.enforcement_mode}
    if dialect != "cedar":
        requested["dialect"] = dialect
    journal(
        db, engine.workspace_id, "policy.create", engine_id=engine.id, policy_id=policy.id,
        version=version.version, operator=operator, requested=requested,
        after={"status": policy.status, "draft_version": version.version},
    )
    db.commit()
    db.refresh(policy)
    return policy


def update_policy(
    db: Session,
    engine: PolicyEngine | str,
    policy_id: str,
    *,
    operator: str | None,
    statement: str | None = None,
    description: str | None = None,
    dialect: str | None = None,
    agent_ids: Any = None,
) -> Policy:
    """A new statement becomes a new **draft** version; an ACTIVE version stays live
    until that draft is published (Global: "ACTIVE edits create a candidate"). ``agent_ids``
    is accepted and ignored (the retired agent scope)."""
    engine = _engine_of_arg(db, engine, policy_id)
    policy = get_policy(db, engine.workspace_id, policy_id, engine.id)
    before = {"description": policy.description, "draft_version": policy.draft_version}
    new_version = None
    if statement is not None:
        current = current_version(db, policy)
        # the language stays the current version's unless the request names one
        language = dialect or (current.dialect if current is not None else None) or "cedar"
        if current is None or current.statement != statement or \
                (current.dialect or "cedar") != language:
            report, kind = _require_valid(statement, dialect=language, db=db, engine=engine,
                                          policy_id=policy.id)
            new_version = _new_version(db, policy, statement, report, operator,
                                       dialect=language, kind=kind)
    if description is not None:
        policy.description = description
    policy.updated_at = _now()
    journal(
        db, engine.workspace_id, "policy.update", engine_id=engine.id, policy_id=policy.id,
        version=new_version.version if new_version else None, operator=operator, before=before,
        requested={"statement_changed": new_version is not None, "description": description},
        after={"draft_version": policy.draft_version},
    )
    db.commit()
    db.refresh(policy)
    return policy


def s3_key(policy_id: str, version: int) -> str:
    return f"{S3_PREFIX}/policies/{policy_id}/v{version}.cedar"


def _copy_to_s3(workspace: WorkspaceContext, policy_id: str, version: int, statement: str) -> str:
    bucket = str((workspace.resources or {}).get("artifacts_bucket") or "")
    if not bucket:
        raise AppError(
            "workspace.not_ready",
            "artifacts_bucket missing from this workspace's resource map — run its bootstrap",
            status_code=409,
        )
    key = s3_key(policy_id, version)
    try:
        workspace.client("s3").put_object(
            Bucket=bucket, Key=key, Body=statement.encode("utf-8"),
            ContentType="text/plain; charset=utf-8", IfNoneMatch="*",
        )
    except ClientError as exc:
        code = str((exc.response or {}).get("Error", {}).get("Code") or "")
        if code in {"PreconditionFailed", "412"}:
            return key  # already copied by an earlier attempt — immutable, so identical
        raise AppError(
            "policy.s3_copy_failed", f"could not write the published copy to S3: {exc}",
            {"bucket": bucket, "key": key}, status_code=502,
        ) from exc
    return key


def publish_policy(
    db: Session,
    workspace: WorkspaceContext,
    engine: PolicyEngine | str,
    policy_id: str | None = None,
    *,
    operator: str | None,
    version: int | None = None,
    reason: str | None = None,
) -> Policy:
    """Make ``version`` (default: the draft) the ACTIVE one. The S3 copy is written before
    the ledger commits, so a row never claims a copy that does not exist; the engine's set is
    installed before the commit too (a refusal rolls the publish back). The pre-engine form
    ``publish_policy(db, workspace, policy_id)`` resolves the engine from the policy."""
    if policy_id is None:
        policy_id = str(engine)
        engine = engine_of(db, get_policy(db, workspace.id, policy_id))
    engine = _engine_of_arg(db, engine, policy_id)
    policy = get_policy(db, workspace.id, policy_id, engine.id)
    target = version if version is not None else policy.draft_version
    if target is None:
        if policy.status == "retired" and policy.active_version is not None:
            target = policy.active_version  # re-activate the last published version
        else:
            raise AppError(
                "policy.no_draft", "nothing to publish — the policy has no draft version",
                status_code=409,
            )
    row = get_version(db, policy, target)
    validation = row.validation or {}
    if not validation.get("ok"):
        raise AppError(
            "policy.invalid_statement", "this version did not validate — fix it before publishing",
            validation, status_code=422,
        )
    if getattr(row, "dialect", "cedar") == "dogwood":
        # only the Dogwood engine reads Dogwood — a publish (or re-activation) while the
        # platform does not install on it would hand cedarpy a statement it cannot decide
        from app.services import policy_engine_sync

        if not policy_engine_sync.enabled():
            raise AppError(
                "policy.engine_not_routed",
                "a Dogwood version can be published only while the platform installs policies "
                "on the policy engine (Settings.policy_gateway_interceptor)",
                {"policy_id": policy.id, "version": row.version}, status_code=409,
            )
    before = {"status": policy.status, "active_version": policy.active_version,
              "draft_version": policy.draft_version}
    try:
        if row.s3_key is None:
            row.s3_key = _copy_to_s3(workspace, policy.id, row.version, row.statement)
    except AppError as exc:
        journal(
            db, workspace.id, "policy.publish", engine_id=engine.id, policy_id=policy.id,
            version=row.version, operator=operator, status="failed", before=before,
            requested={"version": row.version, "reason": reason}, error=exc.message,
        )
        db.commit()
        raise
    if row.published_at is None:
        row.published_at = _now()
        row.published_by = operator
    policy.active_version = row.version
    policy.status = "active"
    if policy.draft_version == row.version:
        policy.draft_version = None
    policy.updated_at = _now()
    installed = _engine_install(db, engine, operation="policy.publish", policy_id=policy.id,
                                version=row.version, operator=operator, before=before)
    journal(
        db, workspace.id, "policy.publish", engine_id=engine.id, policy_id=policy.id,
        version=row.version, operator=operator, before=before,
        requested={"version": row.version, "reason": reason},
        after={"status": policy.status, "active_version": policy.active_version,
               "draft_version": policy.draft_version, "s3_key": row.s3_key},
        override_reason=reason,
    )
    db.commit()
    db.refresh(policy)
    _changed("publish", engine, policy_id=policy.id, version=row.version)
    if installed is not None:
        _publish_gateways(db, engine, trigger="policy.publish", operator=operator)
        db.refresh(policy)
    return policy


def retire_policy(
    db: Session, engine: PolicyEngine | str, policy_id: str, *, operator: str | None,
    reason: str | None = None,
) -> Policy:
    """Retire an ACTIVE policy (Global: no guard on the last one — an ENFORCE gateway whose
    engine holds zero ACTIVE policies denies every call, Cedar default-deny)."""
    engine = _engine_of_arg(db, engine, policy_id)
    policy = get_policy(db, engine.workspace_id, policy_id, engine.id)
    if policy.status != "active":
        raise AppError("policy.not_active", "only an ACTIVE policy can be retired", status_code=409)
    before = {"status": policy.status, "active_version": policy.active_version}
    policy.status = "retired"
    policy.updated_at = _now()
    installed = _engine_install(db, engine, operation="policy.retire", policy_id=policy.id,
                                version=policy.active_version, operator=operator, before=before)
    journal(
        db, engine.workspace_id, "policy.retire", engine_id=engine.id, policy_id=policy.id,
        version=policy.active_version, operator=operator, before=before,
        requested={"reason": reason}, after={"status": policy.status}, override_reason=reason,
    )
    db.commit()
    db.refresh(policy)
    _changed("retire", engine, policy_id=policy.id, version=policy.active_version)
    if installed is not None:
        _publish_gateways(db, engine, trigger="policy.retire", operator=operator)
        db.refresh(policy)
    return policy


def delete_policy(
    db: Session, engine: PolicyEngine | str, policy_id: str, *, operator: str | None,
) -> None:
    engine = _engine_of_arg(db, engine, policy_id)
    policy = get_policy(db, engine.workspace_id, policy_id, engine.id)
    if policy.status == "active":
        raise AppError(
            "policy.active", "retire the policy before deleting it", status_code=409,
        )
    before = {"name": policy.name, "status": policy.status, "active_version": policy.active_version}
    for row in list_versions(db, policy):
        db.delete(row)
    db.delete(policy)
    journal(
        db, engine.workspace_id, "policy.delete", engine_id=engine.id, policy_id=policy_id,
        operator=operator, before=before, after={"deleted": True},
    )
    db.commit()
    _changed("delete", engine, policy_id=policy_id)


# ── decisions (audit rows) ────────────────────────────────────────────────


def record_decision(
    db: Session,
    workspace_id: str,
    *,
    source: str,
    channel: str,
    principal_kind: str,
    principal_id: str,
    action: str,
    resource_kind: str,
    resource_id: str,
    decision: str,
    mode: str,
    principal_label: str | None = None,
    agent_id: str | None = None,
    session_id: str | None = None,
    request_id: str | None = None,
    policy_ids: list[str] | None = None,
    policy_versions: dict[str, int] | None = None,
    bundle_version: int | None = None,
    reason: str | None = None,
    latency_ms: float | None = None,
    gateway_id: str | None = None,
    engine_id: str | None = None,
    commit: bool = True,
) -> PolicyDecision:
    """The ledger row every decision leaves (the system of record; P1-d mirrors it to
    CloudWatch). ``commit=False`` lets a caller batch it into its own transaction. ``mode`` is
    stored the Global way (``LOG_ONLY`` / ``ENFORCE``) whatever spelling the caller used."""
    mode = ROW_MODES.get(mode, mode)
    row = PolicyDecision(
        workspace_id=workspace_id, request_id=request_id, source=source, channel=channel,
        principal_kind=principal_kind, principal_id=principal_id,
        principal_label=principal_label, action=action, resource_kind=resource_kind,
        resource_id=resource_id, agent_id=agent_id, session_id=session_id, decision=decision,
        mode=mode, policy_ids=list(policy_ids or []), policy_versions=dict(policy_versions or {}),
        bundle_version=bundle_version, reason=reason, latency_ms=latency_ms,
        gateway_id=gateway_id, engine_id=engine_id,
    )
    db.add(row)
    if commit:
        db.commit()
        db.refresh(row)
        _prune_on_record(db)
    else:
        db.flush()
    # P1-d: mirror to CloudWatch (fail-soft, never blocks — the row above is the record).
    policy_audit.publish_row(row)
    return row


_record_counter = 0


def _prune_on_record(db: Session) -> None:
    """Every ``Settings.policy_decisions_prune_every`` committed records, drop rows older
    than the retention (rev-r8 nit: not only on the decisions read). Only on the commit path —
    a caller batching into its own transaction keeps control of it."""
    global _record_counter
    every = int(get_settings().policy_decisions_prune_every or 0)
    if every <= 0:
        return
    _record_counter += 1
    if _record_counter % every:
        return
    try:
        pruned = prune_decisions(db)
    except Exception:  # noqa: BLE001 — housekeeping must never fail a decision
        log.exception("policy_decisions prune-on-record failed")
        return
    if pruned:
        log.info("policy_decisions: pruned %d rows older than the retention", pruned)


def reset_record_counter() -> None:
    """Tests: make the next record the first of a prune window."""
    global _record_counter
    _record_counter = 0


# N17-obs508: the note the Gateway interceptor appends to an ALLOW row when a LOG_ONLY policy
# would have denied the call (`interceptor/core.py` `notes`) — the row's only would-deny signal
LOG_ONLY_WOULD_NOTE = "log_only policy would deny"


# rows spell the gateway mode the Global way (console contract 2026-10-28); historic rows keep
# what they were written with
ROW_MODES = {"log_only": "LOG_ONLY", "enforce": "ENFORCE"}


def _gateway_name(db: Session | None, gateway_id: str | None) -> str | None:
    if not gateway_id or db is None:
        return None
    from app.services import policy_engines

    owner = policy_engines.gateway_owner(db, gateway_id)
    if owner is None:
        return None
    from app.models.ledger import Workspace

    row = db.get(Workspace, owner)
    return str(((row.resources if row else None) or {}).get("gateway_name") or "") or None


def _log_only_would(row: PolicyDecision) -> bool:
    return (row.source == "gateway" and row.decision == "allow"
            and LOG_ONLY_WOULD_NOTE in (row.reason or ""))


def list_decisions(
    db: Session,
    workspace_id: str,
    *,
    engine_id: str | None = None,
    gateway_id: str | None = None,
    source: str | None = None,
    decision: str | None = None,
    agent_id: str | None = None,
    principal: str | None = None,
    since: datetime | None = None,
    limit: int = 200,
) -> list[PolicyDecision]:
    stmt = select(PolicyDecision).where(PolicyDecision.workspace_id == workspace_id)
    if engine_id:
        stmt = stmt.where(PolicyDecision.engine_id == engine_id)
    if gateway_id:
        stmt = stmt.where(PolicyDecision.gateway_id == gateway_id)
    if source:
        stmt = stmt.where(PolicyDecision.source == source)
    if decision == "would_deny":
        # N17-obs508: the would-deny filter also finds the gateway ALLOW rows a log_only policy
        # would have denied (the interceptor records the note, not a would_deny decision)
        stmt = stmt.where(
            (PolicyDecision.decision == "would_deny")
            | ((PolicyDecision.source == "gateway") & (PolicyDecision.decision == "allow")
               & PolicyDecision.reason.contains(LOG_ONLY_WOULD_NOTE))
        )
    elif decision:
        stmt = stmt.where(PolicyDecision.decision == decision)
    if agent_id:
        stmt = stmt.where(PolicyDecision.agent_id == agent_id)
    if principal:
        stmt = stmt.where(
            (PolicyDecision.principal_id == principal)
            | (PolicyDecision.principal_label == principal)
        )
    if since is not None:
        stmt = stmt.where(PolicyDecision.created_at >= since)
    return list(
        db.scalars(stmt.order_by(PolicyDecision.created_at.desc()).limit(limit)).all()
    )


def decision_view(row: PolicyDecision, db: Session | None = None) -> dict[str, Any]:
    """The ``policy_decisions`` columns (contract: `_decision_out`), plus the nested
    ``principal`` / ``resource`` objects as an additive convenience and ``gateway_name`` (the
    owning workspace's, when ``db`` is given). A ``gateway`` row additionally carries ``stage``
    / ``session_psid`` / ``event_seq`` / ``store_epoch`` / ``input_keys`` / ``ingested_at`` /
    ``ingest_lag_ms`` / ``log_only_would``."""
    view = _decision_base_view(row)
    view["gateway_name"] = _gateway_name(db, row.gateway_id)
    if row.source == "gateway":
        view.update(stage=row.stage, session_psid=row.session_psid, event_seq=row.event_seq,
                    store_epoch=row.store_epoch, input_keys=list(row.input_keys or []),
                    # N17-obs482 (additive): when the hub ingested the line, and the lag from
                    # the interceptor's decision time; both null for rows ingested before
                    ingested_at=_iso(row.ingested_at),
                    ingest_lag_ms=_ingest_lag_ms(row),
                    # N17-obs508 (additive): an ALLOW a log_only policy would have denied
                    log_only_would=_log_only_would(row))
    return view


def _ingest_lag_ms(row: PolicyDecision) -> float | None:
    """``ingested_at − created_at`` in milliseconds (N17-obs482), or ``None`` without a stamp.
    SQLite hands both back naive UTC; an aware pair is compared as is."""
    if row.ingested_at is None or row.created_at is None:
        return None
    ingested, created = row.ingested_at, row.created_at
    if (ingested.tzinfo is None) != (created.tzinfo is None):
        ingested = ingested.replace(tzinfo=None) if ingested.tzinfo else ingested
        created = created.replace(tzinfo=None) if created.tzinfo else created
    return round((ingested - created).total_seconds() * 1000, 1)


def _decision_base_view(row: PolicyDecision) -> dict[str, Any]:
    return {
        "id": row.id, "request_id": row.request_id, "source": row.source,
        "channel": row.channel,
        "gateway_id": row.gateway_id, "engine_id": row.engine_id,
        "principal_kind": row.principal_kind, "principal_id": row.principal_id,
        "principal_label": row.principal_label,
        "principal": {"kind": row.principal_kind, "id": row.principal_id,
                      "label": row.principal_label},
        "action": row.action,
        "resource_kind": row.resource_kind, "resource_id": row.resource_id,
        "resource": {"kind": row.resource_kind, "id": row.resource_id},
        "agent_id": row.agent_id, "session_id": row.session_id, "decision": row.decision,
        "mode": row.mode, "policy_ids": list(row.policy_ids or []),
        "policy_versions": dict(row.policy_versions or {}),
        "bundle_version": row.bundle_version, "reason": row.reason,
        "latency_ms": row.latency_ms, "created_at": _iso(row.created_at),
    }


def prune_decisions(db: Session, keep_days: int | None = None) -> int:
    """Delete decision rows older than ``Settings.policy_decisions_keep_days``."""
    days = keep_days if keep_days is not None else get_settings().policy_decisions_keep_days
    if days <= 0:
        return 0
    cutoff = _now() - timedelta(days=days)
    # `synchronize_session="fetch"`: the default (`auto` → `evaluate`) compares SQLite's
    # naive `created_at` with the aware cutoff in Python and raises when a decision row is
    # already loaded in the session (P1-d: prune-on-record runs inside request sessions).
    count = (
        db.query(PolicyDecision)
        .filter(PolicyDecision.created_at < cutoff)
        .delete(synchronize_session="fetch")
    )
    db.commit()
    return int(count or 0)


# ── capabilities / overview (delegated to the engine service) ──────────────


def capability_summary(db: Session, workspace_id: str) -> dict[str, Any]:
    """The ``policy`` block of ``GET /api/capabilities``: ``{engines, attached_gateways}``."""
    from app.services import policy_engines

    return policy_engines.capability_summary(db, workspace_id)


def overview_status(db: Session, workspace_id: str, *, engine_registered: bool) -> dict[str, Any]:
    from app.services import policy_engines

    return policy_engines.overview_status(db, workspace_id, engine_registered=engine_registered)
