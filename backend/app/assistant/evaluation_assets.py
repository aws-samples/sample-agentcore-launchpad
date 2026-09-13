"""Materialization of a reviewed evaluation-assets plan (SE-047).

One approved plan revision → one durable ``EvaluationAssetOperation`` → these owned
resources, created in this order, each from a persisted **intent** (unique name that
embeds the operation id, a per-intent random *provenance nonce*, a stable client
token or the exact request) so a lost response, a crash between a cloud success and
the ledger write, a restart or a concurrent click resumes the SAME resources instead
of creating new ones or adopting foreign ones by name or by copyable tags:

1. ``dataset``        — the local Launchpad Dataset (ledger only; never synced to AWS
                        here; edits made afterwards in the Evaluation console are the
                        member's and are not overwritten by a retry);
2. ``lambda_role``    — a dedicated Lambda execution role with ONLY log rights on its
                        own log group (only when the plan has code evaluators);
3. ``log_group``      — ``/aws/lambda/<function>`` with bounded retention;
4. ``lambda_function``— the reviewed static handler + canonical ``rules.json`` +
                        ``provenance.json`` (the nonce) as a deterministic ZIP; one
                        immutable published version whose ``CodeSha256`` must equal
                        the persisted digest; bounded timeout/memory/reserved
                        concurrency, no provisioned concurrency;
5. ``lambda_permission`` — resource policy for ``bedrock-agentcore.amazonaws.com``
                        scoped by ``SourceAccount`` on the published version only;
6. ``role_grant``     — optional additive inline policy on the workspace execution
                        role (identity — ARN **and** RoleId — pinned at approval and
                        re-read before the write) granting Invoke/GetFunction on the
                        exact published version ARN only; trust and other policies
                        untouched;
7. ``evaluator:<key>``— every judge / derived / code evaluator of the plan, created
                        with a stable ``clientToken`` and read back until ACTIVE with
                        id, name, level and configuration equal to the request.

**Ownership proof.** The nonce is generated when the intent is persisted and travels
inside the resource (role description + tag, log-group tag, the Lambda package bytes
→ ``CodeSha256``; evaluators use the service's own ``clientToken`` idempotency). A
resource found under our name after a lost response is ours only if it carries the
nonce nobody else could have known before our create call; anything else is a
**foreign collision** (recorded, never adopted, never deleted).

**Fencing.** One host-local ``flock`` per operation (worker and cleanup) plus a
database lease token; before EVERY cloud write the worker re-reads the lease token,
re-checks the approver is still an active administrator and that the workspace row
still equals the identity pinned at approval (account, region, assume-role, execution
role ARN/RoleId). A quick process restart re-acquires the free flock and resumes
without waiting for a lease timeout; a live worker is never stolen.

Nothing here deploys an agent, starts an evaluation, syncs a dataset to AWS, enables
online evaluation or invokes a model. Status reads are ledger-only. Every AWS client
comes from the workspace funnel (``WorkspaceContext.client``).
"""

from __future__ import annotations

import base64
import fcntl
import hashlib
import io
import json
import logging
import os
import secrets
import threading
import time
import zipfile
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from botocore.exceptions import ClientError
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.assistant import evaluation_plan as plan_contract
from app.assistant.principal import principal_of
from app.core.config import DATA_DIR
from app.core.db import SessionLocal
from app.core.errors import AppError, NotFoundError
from app.evaluation.agentcore_eval import ALL_BUILTIN_EVALUATORS
from app.evaluation.models import EvalDataset
from app.models.assistant import (
    AssistantConversation,
    AssistantEvaluationPlan,
    AssistantProposal,
    EvaluationAssetOperation,
)
from app.models.ledger import User, Workspace
from app.models.ledger import _id as _new_id
from app.routers.auth import ROLE_ADMIN, Identity
from app.services import users as users_service
from app.services.workspace import WorkspaceContext, workspace_context

logger = logging.getLogger(__name__)

HANDLER_PATH = Path(__file__).resolve().parent / "lambda_runtime" / "handler.py"
LOCK_DIR = DATA_DIR / "locks" / "eval-assets"
LAMBDA_RUNTIME = "python3.12"
LAMBDA_HANDLER = "handler.lambda_handler"
LAMBDA_MEMORY_MB = 256
LAMBDA_RESERVED_CONCURRENCY = 5
LAMBDA_TIMEOUT_CAP_S = 300
LOG_RETENTION_DAYS = 14
LOGS_POLICY_NAME = "launchpad-evalfn-logs"
PERMISSION_SID = "launchpad-agentcore-evaluations"
AGENTCORE_PRINCIPAL = "bedrock-agentcore.amazonaws.com"
TAG_OPERATION = "launchpad:eval-operation"
TAG_MANAGED = "launchpad:managed"
TAG_PROVENANCE = "launchpad:provenance"
READBACK_ATTEMPTS = 30
READBACK_DELAY_S = 2.0
MAX_ATTEMPTS = 5  # bounded side-effect retries per operation (explicit, never silent)
USABLE_EVALUATOR_STATUSES = ("ACTIVE", "READY")
CODE_CHAIN = ("lambda_role", "log_group", "lambda_function", "lambda_permission", "role_grant")
ACTIVE_STATUSES = ("queued", "running", "partial", "failed")
CLEANABLE_STATUSES = ("succeeded", "partial", "failed", "cleaning")

_CONFLICT_CODES = ("ConflictException", "ResourceConflictException", "AlreadyExistsException",
                   "EntityAlreadyExists", "ResourceAlreadyExistsException")
_NOT_FOUND_CODES = ("ResourceNotFoundException", "NotFoundException", "NoSuchEntity",
                    "NoSuchEntityException")
_LIVE: dict[str, threading.Thread] = {}
_LIVE_LOCK = threading.Lock()

ClientFactory = Callable[[WorkspaceContext, str], Any]
Recheck = Callable[[Session], Identity]


def _default_clients(workspace: WorkspaceContext, service: str) -> Any:
    return workspace.client(service)


def _code(exc: BaseException) -> str:
    if isinstance(exc, ClientError):
        return str(exc.response.get("Error", {}).get("Code") or "")
    return type(exc).__name__


def _safe_error(exc: BaseException) -> str:
    return f"{_code(exc)}: {str(exc)[:400]}"


def _now() -> datetime:
    return datetime.now(UTC)


def _nonce() -> str:
    return secrets.token_hex(16)


# ---------------------------------------------------------------------------
# deterministic Lambda package
# ---------------------------------------------------------------------------


def canonical_rules(plan: plan_contract.EvaluationPlan) -> dict[str, Any]:
    """``rules.json``: evaluator NAME → declarative rules (frozen mapping; the handler
    refuses names it does not know)."""
    return {
        "version": 1,
        "evaluators": {
            e.name: e.rules.model_dump(exclude_none=True)
            for e in plan.evaluators
            if isinstance(e, plan_contract.CodeEvaluator)
        },
    }


def build_package(rules: dict[str, Any], nonce: str = "") -> tuple[bytes, str]:
    """(zip bytes, sha256 hex) — byte-identical for identical (rules, nonce): fixed
    entry order, fixed timestamp, fixed permissions, canonical JSON. The nonce lives
    in ``provenance.json`` so the digest can only be reproduced by whoever persisted
    the intent (ownership proof on a lost CreateFunction response)."""
    handler = HANDLER_PATH.read_bytes()
    files = {
        "handler.py": handler,
        "rules.json": json.dumps(rules, sort_keys=True, ensure_ascii=False,
                                 separators=(",", ":")).encode("utf-8"),
    }
    if nonce:
        files["provenance.json"] = json.dumps({"nonce": nonce}, separators=(",", ":")).encode()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, data in sorted(files.items()):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            zf.writestr(info, data)
    payload = buf.getvalue()
    return payload, hashlib.sha256(payload).hexdigest()


def code_sha256_b64(digest_hex: str) -> str:
    """Lambda reports/expects CodeSha256 as base64 of the raw sha256."""
    return base64.b64encode(bytes.fromhex(digest_hex)).decode("ascii")


# ---------------------------------------------------------------------------
# intents
# ---------------------------------------------------------------------------


def function_name(op_id: str) -> str:
    return f"launchpad-evalfn-{op_id}"


def _token(op_id: str, key: str) -> str:
    # CreateEvaluator clientToken: [A-Za-z0-9-], stable per intent
    return f"lp-evalop-{op_id}-{key}-{secrets.token_hex(8)}".replace("_", "-")


def compose_intents(plan: plan_contract.EvaluationPlan, op_id: str) -> list[dict[str, Any]]:
    fn = function_name(op_id)
    intents: list[dict[str, Any]] = [
        {"kind": "dataset", "key": "dataset", "name": plan.dataset.name, "status": "pending"},
    ]
    code = [e for e in plan.evaluators if isinstance(e, plan_contract.CodeEvaluator)]
    if code:
        rules = canonical_rules(plan)
        nonce = _nonce()
        _, digest = build_package(rules, nonce)
        _, rules_digest = build_package(rules)
        intents += [
            {"kind": "lambda_role", "key": "lambda_role", "name": fn, "status": "pending",
             "nonce": _nonce()},
            {"kind": "log_group", "key": "log_group", "name": f"/aws/lambda/{fn}",
             "status": "pending", "nonce": _nonce()},
            {"kind": "lambda_function", "key": "lambda_function", "name": fn,
             "status": "pending", "nonce": nonce, "digest": digest, "rules_digest": rules_digest,
             "rules": rules,
             "timeout_s": min(max(e.lambda_timeout_s for e in code), LAMBDA_TIMEOUT_CAP_S)},
            {"kind": "lambda_permission", "key": "lambda_permission", "name": PERMISSION_SID,
             "status": "pending"},
            {"kind": "role_grant", "key": "role_grant", "name": f"launchpad-evalop-{op_id}",
             "status": "pending" if plan.grant_workspace_execution_role else "skipped",
             "error": None if plan.grant_workspace_execution_role
             else "not requested by the plan"},
        ]
    for e in plan.evaluators:
        if e.kind in plan_contract.CLOUD_KINDS:
            intents.append({
                "kind": "evaluator", "key": f"evaluator:{e.key}", "plan_key": e.key,
                "name": getattr(e, "name", ""), "definition": e.kind, "status": "pending",
                "client_token": _token(op_id, e.key),
                "reference_dependent": plan_contract.reference_dependent(e),
            })
        elif e.kind == "existing":
            intents.append({
                "kind": "existing", "key": f"existing:{e.key}", "plan_key": e.key,
                "name": e.evaluator_id, "status": "pending",
            })
    return intents


# ---------------------------------------------------------------------------
# projections
# ---------------------------------------------------------------------------


def plan_out(row: AssistantEvaluationPlan, op: EvaluationAssetOperation | None) -> dict[str, Any]:
    valid = not row.validation_errors and isinstance(row.content, dict)
    return {
        "id": row.id,
        "conversation_id": row.conversation_id,
        "proposal_id": row.proposal_id,
        "source_revision": row.source_revision,
        "source_content_hash": row.source_content_hash,
        "revision": row.revision,
        "source": row.source,
        "status": row.status,
        "content": row.content,
        "content_hash": row.content_hash,
        "validation_errors": row.validation_errors or [],
        "summary": plan_contract.plan_summary(row.content) if valid else None,
        "created_by": row.created_by,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "operation_id": op.id if op else None,
    }


_PUBLIC_RESOURCE_KEYS = ("kind", "key", "plan_key", "name", "status", "definition", "error",
                         "digest", "rules_digest", "reference_dependent", "attempts", "result",
                         "cleanup", "owned", "recovered")


def operation_out(op: EvaluationAssetOperation) -> dict[str, Any]:
    resources = []
    for r in op.resources or []:
        out = {k: r.get(k) for k in _PUBLIC_RESOURCE_KEYS if k in r}
        result = r.get("result") or {}
        if r.get("kind") == "dataset" and result.get("dataset_id"):
            out["link"] = f"/evaluation?view=datasets&ds={result['dataset_id']}"
        if r.get("kind") in ("evaluator", "existing") and result.get("evaluator_id"):
            out["link"] = f"/evaluation?view=evaluators&ev={result['evaluator_id']}"
        resources.append(out)
    return {
        "id": op.id,
        "conversation_id": op.conversation_id,
        "plan_id": op.plan_id,
        "plan_revision": op.plan_revision,
        "plan_hash": op.plan_hash,
        "proposal_revision": op.proposal_revision,
        "approved_by": op.approved_by,
        "account_id": op.account_id,
        "region": op.region,
        "pinned": {k: v for k, v in (op.pinned or {}).items() if k != "external_id"},
        "status": op.status,
        "attempts": op.attempts,
        "max_attempts": MAX_ATTEMPTS,
        "dataset_id": op.dataset_id,
        "error": op.error,
        "resources": resources,
        "created_at": op.created_at.isoformat() if op.created_at else None,
        "updated_at": op.updated_at.isoformat() if op.updated_at else None,
        "running": live_worker(op.id) is not None or not _flock_free(op.id),
    }


def live_worker(op_id: str) -> threading.Thread | None:
    with _LIVE_LOCK:
        t = _LIVE.get(op_id)
        return t if t is not None and t.is_alive() else None


# ---------------------------------------------------------------------------
# host-local exclusion (one worker OR one cleanup per operation, across processes)
# ---------------------------------------------------------------------------


def _lock_path(op_id: str) -> Path:
    LOCK_DIR.mkdir(parents=True, exist_ok=True)
    return LOCK_DIR / f"{op_id}.lock"


@contextmanager
def _flock(op_id: str):
    """Yields True when this caller holds the operation's exclusive host lock."""
    fd = os.open(_lock_path(op_id), os.O_RDWR | os.O_CREAT, 0o600)
    held = False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            held = True
        except OSError:
            held = False
        yield held
    finally:
        try:
            if held:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _flock_free(op_id: str) -> bool:
    with _flock(op_id) as held:
        return held


# ---------------------------------------------------------------------------
# plan revisions (ledger only)
# ---------------------------------------------------------------------------


def plans_of(db: Session, conversation_id: str) -> list[AssistantEvaluationPlan]:
    return (
        db.query(AssistantEvaluationPlan)
        .filter(AssistantEvaluationPlan.conversation_id == conversation_id)
        .order_by(AssistantEvaluationPlan.revision.asc())
        .all()
    )


def operations_of(db: Session, conversation_id: str) -> list[EvaluationAssetOperation]:
    return (
        db.query(EvaluationAssetOperation)
        .filter(EvaluationAssetOperation.conversation_id == conversation_id)
        .order_by(EvaluationAssetOperation.created_at.asc())
        .all()
    )


def operation_for_plan(db: Session, plan_id: str) -> EvaluationAssetOperation | None:
    return (
        db.query(EvaluationAssetOperation)
        .filter(EvaluationAssetOperation.plan_id == plan_id).first()
    )


def _proposal(db: Session, conversation_id: str, revision: int) -> AssistantProposal:
    row = (
        db.query(AssistantProposal)
        .filter(AssistantProposal.conversation_id == conversation_id,
                AssistantProposal.revision == revision)
        .first()
    )
    if row is None:
        raise AppError("assistant.proposal_stale", "that proposal revision does not exist",
                       {"revision": revision}, status_code=409)
    if row.status == "invalid" or not isinstance(row.content, dict) or not row.content.get(
            "name"):
        raise AppError("assistant.evaluation_plan_source_invalid",
                       "an evaluation plan needs a shape-valid proposal revision",
                       {"revision": revision}, status_code=409)
    return row


def _store_plan(
    db: Session, conversation: AssistantConversation, proposal: AssistantProposal,
    raw: Any, *, source: str, created_by: str,
) -> AssistantEvaluationPlan:
    """Validate and append one plan revision (draft or invalid); older drafts of the
    conversation become superseded. Unique (conversation, revision) — a loser of a
    concurrent write re-allocates the number."""
    plan, errors = plan_contract.validate_plan(
        raw, proposal.content, revision=proposal.revision, content_hash=proposal.content_hash
    )
    content = plan.model_dump() if plan else (raw if isinstance(raw, dict) else {})
    for _attempt in range(5):
        db.rollback()
        db.execute(
            update(AssistantEvaluationPlan)
            .where(AssistantEvaluationPlan.conversation_id == conversation.id,
                   AssistantEvaluationPlan.status.in_(("draft", "invalid")))
            .values(status="superseded")
        )
        latest = db.execute(
            select(func.max(AssistantEvaluationPlan.revision))
            .where(AssistantEvaluationPlan.conversation_id == conversation.id)
        ).scalar()
        row = AssistantEvaluationPlan(
            workspace_id=conversation.workspace_id,
            conversation_id=conversation.id,
            proposal_id=proposal.id,
            source_revision=proposal.revision,
            source_content_hash=proposal.content_hash,
            revision=(latest or 0) + 1,
            source=source,
            content=content,
            content_hash=plan_contract.canonical_hash(content),
            validation_errors=errors,
            status="draft" if plan else "invalid",
            created_by=created_by,
        )
        db.add(row)
        try:
            db.commit()
            return row
        except IntegrityError:
            db.rollback()
    raise AppError("assistant.evaluation_plan_conflict",
                   "could not allocate a plan revision; retry", status_code=409)


def prepare_plan(
    db: Session, conversation: AssistantConversation, *, revision: int, created_by: str
) -> AssistantEvaluationPlan:
    """The platform draft for one proposal revision (no side effects beyond the row)."""
    proposal = _proposal(db, conversation.id, revision)
    draft = plan_contract.draft_plan(
        proposal.content, revision=proposal.revision, content_hash=proposal.content_hash,
        agent_name=str(proposal.content.get("name") or "agent"),
    )
    return _store_plan(db, conversation, proposal, draft, source="platform",
                       created_by=created_by)


def edit_plan(
    db: Session, conversation: AssistantConversation, raw: Any, *, created_by: str
) -> AssistantEvaluationPlan:
    if plan_contract.serialized_bytes(raw) > plan_contract.PLAN_MAX_BYTES:
        raise AppError("assistant.evaluation_plan_too_large",
                       f"the plan exceeds {plan_contract.PLAN_MAX_BYTES} bytes",
                       {"max_bytes": plan_contract.PLAN_MAX_BYTES}, status_code=413)
    revision = raw.get("source_revision") if isinstance(raw, dict) else None
    if not isinstance(revision, int):
        raise AppError("assistant.evaluation_plan_invalid",
                       "plan.source_revision must name the proposal revision", status_code=422)
    proposal = _proposal(db, conversation.id, revision)
    return _store_plan(db, conversation, proposal, raw, source="member", created_by=created_by)


# ---------------------------------------------------------------------------
# approval → operation (the only path that may create an operation)
# ---------------------------------------------------------------------------


@dataclass
class Materialization:
    operation: EvaluationAssetOperation
    started: bool  # True ⇔ this call created the operation (caller launches the worker)


def approver_authorized(db: Session, op: EvaluationAssetOperation) -> str | None:
    """Fresh check that the approver is still an active administrator. ``None`` when
    fine, else the reason. The config admin has no row and stays authorized."""
    if op.approver_user_id is None:
        return None
    user = db.get(User, op.approver_user_id)
    if user is None:
        return "the approving account no longer exists"
    if user.status != "active" or users_service.is_expired(user):
        return "the approving account is disabled or expired"
    if user.role != ROLE_ADMIN:
        return "the approving account is no longer an administrator"
    return None


def pin_workspace(row: Workspace) -> dict[str, Any]:
    return {
        "workspace_id": row.id,
        "account_id": row.account_id,
        "region": row.region,
        "role_arn": row.role_arn,
        "external_id": row.external_id,
        "execution_role_arn": (row.resources or {}).get("execution_role_arn"),
    }


def pinned_drift(pinned: dict[str, Any], row: Workspace | None) -> list[str]:
    """Which pinned identity fields no longer match the live workspace row."""
    if row is None:
        return ["workspace"]
    current = pin_workspace(row)
    return sorted(k for k in current if pinned.get(k) != current[k])


def _role_name(arn: str) -> str:
    return str(arn).rsplit("/", 1)[-1]


def _role_tags(role: dict[str, Any]) -> dict[str, str]:
    return {str(t.get("Key")): str(t.get("Value")) for t in (role.get("Tags") or [])
            if isinstance(t, dict)}


def _trusted_execution_role(iam: Any, role_arn: str) -> dict[str, Any]:
    """The workspace execution role, resolved by name from its ARN and accepted only
    when the ARN matches AND it carries the platform tag (never by name prefix)."""
    role = iam.get_role(RoleName=_role_name(role_arn))["Role"]
    if role.get("Arn") != role_arn or _role_tags(role).get(TAG_MANAGED) != "true":
        raise AppError(
            "assistant.execution_role_untrusted",
            f"the workspace execution role {role_arn} is not a platform-managed role — refusing "
            "to write a grant on it; set grant_workspace_execution_role to false or attach "
            "Invoke/GetFunction on the created Lambda version manually",
            {"role_arn": role_arn}, status_code=409,
        )
    return role


def approve_plan(
    db: Session,
    conversation: AssistantConversation,
    row: Workspace,
    *,
    plan_revision: int,
    plan_hash: str,
    approved_by: str,
    approver_user_id: str | None,
    recheck: Recheck | None = None,
    clients: ClientFactory = _default_clients,
) -> Materialization:
    """Claim exactly one plan revision for materialization — atomically.

    The claim is a conditional UPDATE of the plan row (still ``draft``, still this
    hash, still the newest revision) in the same transaction that inserts the
    operation with every intent and the pinned workspace identity; ``recheck`` (when
    given) re-resolves the caller from the database inside that transaction and must
    still be an administrator who owns the conversation. Repeated / concurrent calls
    for the same plan return the recorded operation (200); a superseded, edited or
    already-claimed plan is ``409 assistant.evaluation_plan_stale`` before any write.
    """
    plan_row = (
        db.query(AssistantEvaluationPlan)
        .filter(AssistantEvaluationPlan.conversation_id == conversation.id,
                AssistantEvaluationPlan.revision == plan_revision)
        .first()
    )
    if plan_row is None or plan_row.content_hash != plan_hash:
        raise AppError("assistant.evaluation_plan_stale",
                       "that plan revision/hash is not what is stored",
                       {"revision": plan_revision}, status_code=409)
    existing = operation_for_plan(db, plan_row.id)
    if existing is not None:
        return Materialization(existing, False)
    if plan_row.status not in ("draft",):
        raise AppError("assistant.evaluation_plan_not_approvable",
                       f"plan revision {plan_revision} is {plan_row.status}",
                       {"revision": plan_revision, "errors": plan_row.validation_errors},
                       status_code=409)
    plan, errors = plan_contract.validate_plan(
        plan_row.content, _proposal(db, conversation.id, plan_row.source_revision).content,
        revision=plan_row.source_revision, content_hash=plan_row.source_content_hash,
    )
    if plan is None:
        raise AppError("assistant.evaluation_plan_invalid",
                       "the plan no longer validates against its proposal revision",
                       {"errors": errors}, status_code=409)
    fresh_row = db.get(Workspace, row.id)
    if fresh_row is None or fresh_row.bootstrap_status != "ready" or not (
            fresh_row.resources or {}).get("execution_role_arn"):
        raise AppError("assistant.workspace_not_ready",
                       "this workspace is not bootstrapped (no execution role)",
                       status_code=409)
    pinned = pin_workspace(fresh_row)
    has_code = any(isinstance(e, plan_contract.CodeEvaluator) for e in plan.evaluators)
    if has_code and plan.grant_workspace_execution_role:
        # the grant target's identity is approved NOW (ARN + RoleId + platform tag) and
        # re-compared before the write; a replaced role is refused, never re-adopted
        role = _trusted_execution_role(clients(workspace_context(fresh_row), "iam"),
                                       str(pinned["execution_role_arn"]))
        pinned["execution_role_id"] = role["RoleId"]
    op = EvaluationAssetOperation(
        id=_new_id(),  # assigned NOW: every intent name/token embeds it
        workspace_id=conversation.workspace_id,
        conversation_id=conversation.id,
        plan_id=plan_row.id,
        plan_revision=plan_row.revision,
        plan_hash=plan_row.content_hash,
        proposal_revision=plan_row.source_revision,
        owner_principal=str(conversation.owner_principal),
        approved_by=approved_by,
        approver_user_id=approver_user_id,
        account_id=fresh_row.account_id,
        region=fresh_row.region,
        pinned=pinned,
        status="queued",
    )
    op.resources = compose_intents(plan, op.id)
    # --- the atomic claim -----------------------------------------------------
    db.rollback()
    db.expire_all()
    if recheck is not None:
        identity = recheck(db)
        owner = db.execute(select(AssistantConversation.owner_principal)
                           .where(AssistantConversation.id == conversation.id)).scalar()
        if not identity.is_admin or owner is None or owner != principal_of(identity):
            db.rollback()
            raise NotFoundError("assistant.conversation_not_found", "conversation not found")
        op.approved_by = identity.username
        op.approver_user_id = identity.user_id
    newest = select(func.max(AssistantEvaluationPlan.revision)).where(
        AssistantEvaluationPlan.conversation_id == conversation.id
    ).scalar_subquery()
    claimed = db.execute(
        update(AssistantEvaluationPlan)
        .where(AssistantEvaluationPlan.id == plan_row.id,
               AssistantEvaluationPlan.status == "draft",
               AssistantEvaluationPlan.content_hash == plan_hash,
               AssistantEvaluationPlan.revision == newest)
        .values(status="approved")
    ).rowcount
    if claimed != 1:
        db.rollback()
        winner = operation_for_plan(db, plan_row.id)
        if winner is not None:
            return Materialization(winner, False)
        raise AppError("assistant.evaluation_plan_stale",
                       "the plan changed (edited, superseded or already claimed) — review the "
                       "current revision", {"revision": plan_revision}, status_code=409)
    db.add(op)
    try:
        db.commit()
    except IntegrityError:  # a concurrent approval won the unique(plan_id)
        db.rollback()
        winner = operation_for_plan(db, plan_row.id)
        if winner is None:  # pragma: no cover
            raise
        return Materialization(winner, False)
    return Materialization(op, True)


# ---------------------------------------------------------------------------
# the worker
# ---------------------------------------------------------------------------


class _LeaseLost(RuntimeError):
    pass


class _Stop(RuntimeError):
    """Stop the operation with a recorded reason (authorization / identity changed)."""


class _Conflict(RuntimeError):
    """A resource this operation cannot prove it owns — recorded, never adopted."""


class _Fence:
    """Lease token + approver + pinned-workspace check before every cloud write."""

    def __init__(self, op_id: str, token: str, *, status: str) -> None:
        self.op_id = op_id
        self.token = token
        self.status = status

    def load(self, db: Session) -> EvaluationAssetOperation:
        db.expire_all()
        op = db.get(EvaluationAssetOperation, self.op_id)
        if op is None or op.worker_token != self.token or op.status != self.status:
            raise _LeaseLost(f"operation {self.op_id} lease lost")
        return op

    def guard(self, db: Session) -> EvaluationAssetOperation:
        op = self.load(db)
        reason = approver_authorized(db, op)
        if reason:
            raise _Stop(reason)
        drift = pinned_drift(op.pinned or {}, db.get(Workspace, op.workspace_id))
        if drift:
            raise _Stop(f"workspace identity changed since approval ({', '.join(drift)})")
        db.execute(update(EvaluationAssetOperation)
                   .where(EvaluationAssetOperation.id == op.id,
                          EvaluationAssetOperation.worker_token == self.token)
                   .values(heartbeat_at=_now()))
        db.commit()
        return op

    def save(self, db: Session, op: EvaluationAssetOperation, resources: list[dict[str, Any]],
             event: str, **fields: Any) -> None:
        line = json.dumps({"at": _now().isoformat(), "event": event, **{
            k: v for k, v in fields.items() if k in ("status", "error", "dataset_id")}},
            ensure_ascii=False)
        rows = db.execute(
            update(EvaluationAssetOperation)
            .where(EvaluationAssetOperation.id == op.id,
                   EvaluationAssetOperation.worker_token == self.token)
            .values(resources=resources, log=(op.log or "") + line + "\n", **fields)
        ).rowcount
        db.commit()
        if rows != 1:
            raise _LeaseLost("lease lost while saving")
        db.expire_all()


def _resource(resources: list[dict[str, Any]], key: str) -> dict[str, Any]:
    for r in resources:
        if r.get("key") == key:
            return r
    raise KeyError(key)


def _policy_document(raw: Any) -> Any:
    """GetRolePolicy returns the document URL-encoded (string) or decoded (dict)."""
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(unquote(str(raw)))
    except ValueError:
        return None


def _statement_matches(stmt: dict[str, Any], expected: dict[str, Any]) -> bool:
    def norm(v: Any) -> Any:
        return sorted(v) if isinstance(v, list) else ([v] if isinstance(v, str) else v)

    if stmt.get("Effect") != expected["Effect"]:
        return False
    if norm(stmt.get("Action")) != norm(expected["Action"]):
        return False
    if norm(stmt.get("Resource")) != norm(expected["Resource"]):
        return False
    if stmt.get("Principal") != expected["Principal"]:
        return False
    cond = stmt.get("Condition") or {}
    # AWS may spell the key AWS:SourceAccount or aws:SourceAccount
    got = {k.lower(): {kk.lower(): vv for kk, vv in v.items()} for k, v in cond.items()}
    want = {k.lower(): {kk.lower(): vv for kk, vv in v.items()}
            for k, v in expected["Condition"].items()}
    return got == want


class _Runner:
    def __init__(self, op_id: str, clients: ClientFactory, sleeper: Callable[[float], None],
                 token: str) -> None:
        self.op_id = op_id
        self.clients = clients
        self.sleep = sleeper
        self.fence = _Fence(op_id, token, status="running")
        self.workspace: WorkspaceContext | None = None
        self.plan: plan_contract.EvaluationPlan | None = None
        self.proposal_content: dict[str, Any] = {}

    def _client(self, service: str) -> Any:
        assert self.workspace is not None
        return self.clients(self.workspace, service)

    def _write(self, db: Session, fn: Callable[..., Any], **kwargs: Any) -> Any:
        """A cloud write: fresh fence FIRST, then the call."""
        self.fence.guard(db)
        return fn(**kwargs)

    # -- run -----------------------------------------------------------------

    def run(self) -> None:
        db = SessionLocal()
        try:
            op = self.fence.guard(db)
            plan_row = db.get(AssistantEvaluationPlan, op.plan_id)
            proposal = _proposal(db, op.conversation_id, op.proposal_revision)
            plan, errors = plan_contract.validate_plan(
                plan_row.content, proposal.content, revision=plan_row.source_revision,
                content_hash=plan_row.source_content_hash,
            )
            if plan is None:
                raise _Stop("plan no longer validates: " + "; ".join(errors[:3]))
            self.plan = plan
            self.proposal_content = dict(proposal.content)
            # the client context is built from the workspace row that the fence has
            # just proven equal to the pinned identity (never from mutable defaults)
            self.workspace = workspace_context(db.get(Workspace, op.workspace_id))
            chain_broken: str | None = None
            for key in [r["key"] for r in op.resources or []]:
                op = self.fence.guard(db)
                resources = json.loads(json.dumps(op.resources or []))
                res = _resource(resources, key)
                if res.get("status") in ("ready", "skipped", "conflict"):
                    continue
                if res.get("status") == "failed" and int(res.get("attempts") or 0) >= MAX_ATTEMPTS:
                    continue
                if chain_broken and (res["kind"] in CODE_CHAIN or res.get("definition") == "code"):
                    res["status"] = "blocked"
                    res["error"] = f"not attempted: {chain_broken}"
                    self.fence.save(db, op, resources, f"{key}:blocked")
                    continue
                res["attempts"] = int(res.get("attempts") or 0) + 1
                try:
                    getattr(self, f"_step_{res['kind']}")(db, op, resources, res)
                    res["status"] = "ready"
                    res["error"] = None
                    self.fence.save(db, op, resources, f"{key}:ready")
                except (_LeaseLost, _Stop):
                    raise
                except _Conflict as exc:
                    res["status"] = "conflict"
                    res["error"] = str(exc)
                    self.fence.save(db, op, resources, f"{key}:conflict")
                    if res["kind"] in CODE_CHAIN and res["kind"] != "role_grant":
                        chain_broken = f"{key} is a conflict ({exc})"
                except Exception as exc:  # noqa: BLE001 — recorded per resource
                    logger.warning("evaluation assets %s step %s failed: %s", op.id, key, exc)
                    res["status"] = "failed"
                    res["error"] = _safe_error(exc)
                    self.fence.save(db, op, resources, f"{key}:failed")
                    if res["kind"] in CODE_CHAIN and res["kind"] != "role_grant":
                        chain_broken = f"{key} failed ({_safe_error(exc)})"
            op = self.fence.load(db)
            self._finalize_dataset(db, op)
            statuses = [r.get("status") for r in op.resources or []]
            if all(s in ("ready", "skipped") for s in statuses):
                final, error = "succeeded", None
            else:
                final = "partial" if any(s == "ready" for s in statuses) else "failed"
                error = "; ".join(f"{r['key']}: {r.get('error')}" for r in op.resources or []
                                  if r.get("status") in ("failed", "conflict", "blocked"))[:2000]
            self.fence.save(db, op, op.resources or [], "finished", status=final, error=error,
                            worker_token=None, dataset_id=op.dataset_id)
        except _LeaseLost:
            logger.info("evaluation assets %s: lease lost, another worker owns it", self.op_id)
        except _Stop as exc:
            self._abort(db, f"stopped: {exc}")
        except Exception as exc:  # noqa: BLE001
            logger.exception("evaluation assets %s crashed", self.op_id)
            self._abort(db, _safe_error(exc))
        finally:
            db.close()

    def _abort(self, db: Session, error: str) -> None:
        db.rollback()
        db.execute(update(EvaluationAssetOperation)
                   .where(EvaluationAssetOperation.id == self.op_id,
                          EvaluationAssetOperation.worker_token == self.fence.token)
                   .values(status="failed", error=error, worker_token=None))
        db.commit()

    # -- dataset (ledger only) --------------------------------------------------

    def _applies(self, golden_test_id: str) -> list[str]:
        assert self.plan is not None
        out = []
        for e in self.plan.evaluators:
            if e.kind in plan_contract.CLOUD_KINDS or e.kind == "existing":
                if not e.golden_test_ids or golden_test_id in e.golden_test_ids:
                    out.append(e.key)
        return out

    def _step_dataset(self, db, op, resources, res) -> None:
        plan = self.plan
        assert plan is not None
        if (res.get("result") or {}).get("dataset_id"):
            if db.get(EvalDataset, res["result"]["dataset_id"]) is not None:
                return  # already created; member edits afterwards are theirs
        provenance = {
            "conversation_id": op.conversation_id,
            "proposal_revision": op.proposal_revision,
            "plan_revision": op.plan_revision,
            "plan_hash": op.plan_hash,
            "operation_id": op.id,
            # plan-local keys → kind / golden tests / gate; evaluator ids are filled in
            # by the finalize step once the evaluators exist
            "evaluators": {
                e.key: {"kind": e.kind, "golden_test_ids": list(e.golden_test_ids),
                        "blocking": e.blocking, "threshold": e.threshold,
                        "evaluator_id": getattr(e, "evaluator_id", None)}
                for e in plan.evaluators
            },
        }
        gts = {str(g.get("id")): g for g in self.proposal_content.get("golden_tests") or []
               if isinstance(g, dict)}
        items = [
            plan_contract.dataset_item(s, provenance, golden_test=gts.get(s.golden_test_id),
                                       applies=self._applies(s.golden_test_id))
            for s in plan.scenarios
        ]
        from app.evaluation.execution import validate_items
        from app.evaluation.routers import _validate_items  # dataset ingress gate

        _validate_items(items)
        validate_items(items)
        dataset = EvalDataset(
            workspace_id=op.workspace_id, name=plan.dataset.name, locale=plan.dataset.locale,
            description=plan.dataset.description, items=items, kind="predefined",
        )
        db.add(dataset)
        db.flush()
        res["result"] = {"dataset_id": dataset.id, "item_count": len(items)}
        op.dataset_id = dataset.id
        self.fence.save(db, op, resources, "dataset:created", dataset_id=dataset.id)

    def _finalize_dataset(self, db, op) -> None:
        """Write the resolved evaluator ids into the items' ``launchpad_assets`` map —
        only that map, only for items still carrying this operation's provenance."""
        if not op.dataset_id:
            return
        dataset = db.get(EvalDataset, op.dataset_id)
        if dataset is None:
            return
        ids = {r["plan_key"]: (r.get("result") or {}).get("evaluator_id")
               for r in op.resources or [] if r.get("plan_key")}
        items = json.loads(json.dumps(dataset.items or []))
        changed = False
        for item in items:
            assets = ((item.get("metadata") or {}).get("launchpad_assets") or {})
            if assets.get("operation_id") != op.id:
                continue
            for key, entry in (assets.get("evaluators") or {}).items():
                if ids.get(key) and entry.get("evaluator_id") != ids[key]:
                    entry["evaluator_id"] = ids[key]
                    changed = True
        if changed:
            dataset.items = items
            db.commit()

    # -- IAM role for the function -----------------------------------------------

    def _step_lambda_role(self, db, op, resources, res) -> None:
        iam = self._client("iam")
        name = res["name"]
        nonce = res["nonce"]
        fn = function_name(op.id)
        trust = {"Version": "2012-10-17", "Statement": [{
            "Effect": "Allow", "Principal": {"Service": "lambda.amazonaws.com"},
            "Action": "sts:AssumeRole",
            "Condition": {"StringEquals": {"aws:SourceAccount": op.account_id}},
        }]}
        log_arn = f"arn:aws:logs:{op.region}:{op.account_id}:log-group:/aws/lambda/{fn}"
        policy = {"Version": "2012-10-17", "Statement": [{
            "Effect": "Allow",
            "Action": ["logs:CreateLogStream", "logs:PutLogEvents"],
            "Resource": [log_arn, f"{log_arn}:*"],
        }]}
        description = f"Launchpad code-evaluator Lambda role; operation {op.id}; provenance {nonce}"
        stored = res.get("result") or {}
        if stored.get("role_id"):
            current = iam.get_role(RoleName=name)["Role"]
            if current["RoleId"] != stored["role_id"]:
                raise _Conflict(f"role {name} was replaced (RoleId differs) — not ours")
        else:
            res["intent"] = {"requested_at": _now().isoformat()}
            self.fence.save(db, op, resources, "lambda_role:intent")
            try:
                created = self._write(
                    db, iam.create_role, RoleName=name,
                    AssumeRolePolicyDocument=json.dumps(trust), Description=description,
                    Tags=[{"Key": TAG_OPERATION, "Value": op.id},
                          {"Key": TAG_MANAGED, "Value": "true"},
                          {"Key": TAG_PROVENANCE, "Value": nonce}],
                )["Role"]
            except ClientError as exc:
                if _code(exc) not in _CONFLICT_CODES:
                    raise
                # lost response? only OUR nonce (persisted before the call, never
                # published elsewhere) proves the role is the one we created
                current = iam.get_role(RoleName=name)["Role"]
                if nonce in str(current.get("Description") or "") \
                        and _role_tags(current).get(TAG_PROVENANCE) == nonce:
                    created = current
                    res["recovered"] = True
                else:
                    raise _Conflict(f"an IAM role named {name} already exists and was not "
                                    "created by this operation") from exc
            res["result"] = {"role_arn": created["Arn"], "role_id": created["RoleId"]}
            res["owned"] = True
            self.fence.save(db, op, resources, "lambda_role:accepted")
        self._write(db, iam.put_role_policy, RoleName=name, PolicyName=LOGS_POLICY_NAME,
                    PolicyDocument=json.dumps(policy))
        back = _policy_document(iam.get_role_policy(RoleName=name, PolicyName=LOGS_POLICY_NAME)
                                .get("PolicyDocument"))
        if back != policy:
            raise _Conflict("role policy readback differs from the reviewed document")
        res["result"]["policy"] = LOGS_POLICY_NAME
        res["result"]["policy_document"] = policy

    # -- log group ---------------------------------------------------------------

    def _step_log_group(self, db, op, resources, res) -> None:
        logs = self._client("logs")
        name = res["name"]
        nonce = res["nonce"]
        if not (res.get("result") or {}).get("created"):
            res["intent"] = {"requested_at": _now().isoformat()}
            self.fence.save(db, op, resources, "log_group:intent")
            try:
                self._write(db, logs.create_log_group, logGroupName=name,
                            tags={TAG_OPERATION: op.id, TAG_MANAGED: "true",
                                  TAG_PROVENANCE: nonce})
            except ClientError as exc:
                if _code(exc) not in _CONFLICT_CODES:
                    raise
                if not self._log_group_is_ours(logs, name, nonce):
                    raise _Conflict(f"log group {name} already exists and was not created by "
                                    "this operation") from exc
                res["recovered"] = True
            res["result"] = {"created": True}
            res["owned"] = True
            self.fence.save(db, op, resources, "log_group:accepted")
        self._write(db, logs.put_retention_policy, logGroupName=name,
                    retentionInDays=LOG_RETENTION_DAYS)
        groups = logs.describe_log_groups(logGroupNamePrefix=name).get("logGroups") or []
        mine = [g for g in groups if g.get("logGroupName") == name]
        if not mine or mine[0].get("retentionInDays") != LOG_RETENTION_DAYS:
            raise _Conflict("log group readback differs (retention)")
        res["result"]["retention_days"] = LOG_RETENTION_DAYS
        res["result"]["arn"] = mine[0].get("arn")

    @staticmethod
    def _log_group_is_ours(logs: Any, name: str, nonce: str) -> bool:
        groups = logs.describe_log_groups(logGroupNamePrefix=name).get("logGroups") or []
        mine = [g for g in groups if g.get("logGroupName") == name]
        if not mine:
            return False
        arn = str(mine[0].get("arn") or "").rstrip("*").rstrip(":")
        tags = logs.list_tags_for_resource(resourceArn=arn).get("tags") or {}
        return tags.get(TAG_PROVENANCE) == nonce

    # -- the function -------------------------------------------------------------

    def _step_lambda_function(self, db, op, resources, res) -> None:
        plan = self.plan
        assert plan is not None
        lam = self._client("lambda")
        role = _resource(resources, "lambda_role")
        role_arn = (role.get("result") or {}).get("role_arn")
        if not role_arn:
            raise RuntimeError("lambda role not ready")
        payload, digest = build_package(canonical_rules(plan), res["nonce"])
        if digest != res.get("digest"):
            raise RuntimeError("package digest differs from the persisted intent")
        sha_b64 = code_sha256_b64(digest)
        name = res["name"]
        stored = res.get("result") or {}
        if not stored.get("function_arn"):
            request = {
                "FunctionName": name, "Runtime": LAMBDA_RUNTIME, "Role": role_arn,
                "Handler": LAMBDA_HANDLER,
                "Description": f"Launchpad reviewed code evaluator (operation {op.id})",
                "Timeout": int(res["timeout_s"]), "MemorySize": LAMBDA_MEMORY_MB,
                "Publish": False,
                "Tags": {TAG_OPERATION: op.id, TAG_MANAGED: "true"},
            }
            res["request"] = {**request, "CodeSha256": sha_b64}
            self.fence.save(db, op, resources, "lambda_function:intent")
            created = None
            for attempt in range(6):
                try:
                    created = self._write(db, lam.create_function, **request,
                                          Code={"ZipFile": payload})
                    break
                except ClientError as exc:
                    code = _code(exc)
                    if code in _CONFLICT_CODES:
                        # lost response replay: the digest embeds our persisted nonce, so
                        # an equal CodeSha256 is proof the bytes came from this intent
                        cfg = (lam.get_function(FunctionName=name).get("Configuration") or {})
                        if cfg.get("CodeSha256") == sha_b64 and cfg.get("Role") == role_arn:
                            created = cfg
                            res["recovered"] = True
                            break
                        raise _Conflict(f"a Lambda function named {name} exists that this "
                                        "operation cannot prove it created") from exc
                    if code == "InvalidParameterValueException" and "assumed" in str(exc) \
                            and attempt < 5:
                        self.sleep(5)  # IAM propagation of the brand-new role
                        continue
                    raise
            res["result"] = {"function_arn": created["FunctionArn"], "function_name": name}
            res["owned"] = True
            self.fence.save(db, op, resources, "lambda_function:accepted")
        cfg = self._wait_function_active(lam, name)
        if cfg.get("CodeSha256") != sha_b64:
            raise _Conflict("function code differs from the reviewed package")
        stored = res["result"]
        if not stored.get("version"):
            stored["publish_requested_at"] = _now().isoformat()
            self.fence.save(db, op, resources, "lambda_function:publish_intent")
            try:
                published = self._write(db, lam.publish_version, FunctionName=name,
                                        CodeSha256=sha_b64)
            except ClientError as exc:
                if _code(exc) not in _CONFLICT_CODES + ("InvalidParameterValueException",):
                    raise
                published = None
            # reconcile against the real version list: exactly ONE published version
            # may carry our digest (Lambda does not re-publish unchanged code)
            versions = [v for v in (lam.list_versions_by_function(FunctionName=name)
                                    .get("Versions") or [])
                        if v.get("Version") != "$LATEST" and v.get("CodeSha256") == sha_b64]
            if published is not None and published.get("Version") not in {
                    v.get("Version") for v in versions}:
                versions.append(published)
            if len(versions) != 1:
                raise _Conflict(f"{len(versions)} published versions carry the reviewed digest "
                                "— refusing to pick one")
            stored["version"] = versions[0]["Version"]
            stored["version_arn"] = versions[0]["FunctionArn"]
            self.fence.save(db, op, resources, "lambda_function:published")
        self._write(db, lam.put_function_concurrency, FunctionName=name,
                    ReservedConcurrentExecutions=LAMBDA_RESERVED_CONCURRENCY)
        reserved = lam.get_function_concurrency(FunctionName=name).get(
            "ReservedConcurrentExecutions")
        if reserved != LAMBDA_RESERVED_CONCURRENCY:
            raise _Conflict(f"reserved concurrency readback is {reserved}")
        back = lam.get_function(FunctionName=name, Qualifier=stored["version"])
        cfg = back.get("Configuration") or {}
        expected = {
            "CodeSha256": sha_b64, "Runtime": LAMBDA_RUNTIME, "Handler": LAMBDA_HANDLER,
            "Role": role_arn, "Version": stored["version"], "Timeout": int(res["timeout_s"]),
            "MemorySize": LAMBDA_MEMORY_MB, "State": "Active",
        }
        mismatch = sorted(k for k, want in expected.items() if cfg.get(k) != want)
        if mismatch:
            raise _Conflict(f"published version readback differs on {mismatch}")
        stored["readback"] = {k: cfg.get(k) for k in (*expected, "FunctionArn")}
        stored["readback"]["ReservedConcurrentExecutions"] = reserved

    def _wait_function_active(self, lam: Any, name: str) -> dict[str, Any]:
        for _ in range(READBACK_ATTEMPTS):
            cfg = lam.get_function_configuration(FunctionName=name)
            state = cfg.get("State")
            if state == "Active":
                return cfg
            if state == "Failed":
                raise RuntimeError(f"function state Failed: {cfg.get('StateReason')}")
            self.sleep(READBACK_DELAY_S)
        raise RuntimeError("function did not become Active in time")

    # -- resource policy ----------------------------------------------------------

    def _permission_statement(self, op, fn_result: dict[str, Any]) -> dict[str, Any]:
        return {
            "Sid": PERMISSION_SID, "Effect": "Allow", "Action": "lambda:InvokeFunction",
            "Principal": {"Service": AGENTCORE_PRINCIPAL},
            "Resource": fn_result["version_arn"],
            "Condition": {"StringEquals": {"AWS:SourceAccount": op.account_id}},
        }

    def _step_lambda_permission(self, db, op, resources, res) -> None:
        lam = self._client("lambda")
        fn = _resource(resources, "lambda_function")
        stored = fn.get("result") or {}
        if not stored.get("version"):
            raise RuntimeError("function version not published")
        expected = self._permission_statement(op, stored)
        try:
            self._write(db, lam.add_permission, FunctionName=stored["function_name"],
                        StatementId=PERMISSION_SID, Action="lambda:InvokeFunction",
                        Principal=AGENTCORE_PRINCIPAL, SourceAccount=op.account_id,
                        Qualifier=stored["version"])
        except ClientError as exc:
            if _code(exc) not in _CONFLICT_CODES:
                raise
        policy = json.loads(lam.get_policy(FunctionName=stored["function_name"],
                                           Qualifier=stored["version"])["Policy"])
        ours = [s for s in policy.get("Statement", []) if s.get("Sid") == PERMISSION_SID]
        if len(ours) != 1 or not _statement_matches(ours[0], expected):
            raise _Conflict("resource policy statement differs from the reviewed scope "
                            "(principal / action / version / account)")
        res["result"] = {"principal": AGENTCORE_PRINCIPAL, "source_account": op.account_id,
                         "qualifier": stored["version"], "statement": expected}
        res["owned"] = True

    # -- additive grant on the workspace execution role ------------------------------

    def _step_role_grant(self, db, op, resources, res) -> None:
        iam = self._client("iam")
        fn = _resource(resources, "lambda_function")
        stored = fn.get("result") or {}
        if not stored.get("version_arn"):
            raise RuntimeError("function version not published")
        pinned = op.pinned or {}
        role_arn = str(pinned.get("execution_role_arn") or "")
        role_id = pinned.get("execution_role_id")
        if not role_arn or not role_id:
            raise _Conflict("execution role identity was not pinned at approval — grant refused")
        current = iam.get_role(RoleName=_role_name(role_arn))["Role"]
        if current.get("Arn") != role_arn or current.get("RoleId") != role_id \
                or _role_tags(current).get(TAG_MANAGED) != "true":
            raise _Conflict("execution role identity differs from the one approved (ARN / "
                            "RoleId / platform tag) — grant refused")
        document = {"Version": "2012-10-17", "Statement": [{
            "Sid": "LaunchpadEvalOpInvoke", "Effect": "Allow",
            "Action": ["lambda:InvokeFunction", "lambda:GetFunction"],
            "Resource": [stored["version_arn"]],  # the published version only, never $LATEST
        }]}
        res["result"] = {"role_arn": role_arn, "role_id": role_id, "policy_name": res["name"],
                         "policy_document": document}
        self.fence.save(db, op, resources, "role_grant:intent")
        self._write(db, iam.put_role_policy, RoleName=_role_name(role_arn),
                    PolicyName=res["name"], PolicyDocument=json.dumps(document))
        back = _policy_document(iam.get_role_policy(RoleName=_role_name(role_arn),
                                                    PolicyName=res["name"]).get("PolicyDocument"))
        if back != document:
            raise _Conflict("grant readback differs from the reviewed document")
        res["owned"] = True

    # -- evaluators -----------------------------------------------------------------

    def _step_existing(self, db, op, resources, res) -> None:
        evaluator_id = res["name"]
        if evaluator_id in ALL_BUILTIN_EVALUATORS:
            res["result"] = {"evaluator_id": evaluator_id,
                             "level": ALL_BUILTIN_EVALUATORS[evaluator_id], "source": "builtin"}
            return
        control = self._client("bedrock-agentcore-control")
        try:
            detail = control.get_evaluator(evaluatorId=evaluator_id)
        except ClientError as exc:
            if _code(exc) in _NOT_FOUND_CODES:
                raise RuntimeError(f"evaluator {evaluator_id} does not exist in this "
                                   "workspace") from exc
            raise
        if detail.get("evaluatorId") != evaluator_id:
            raise _Conflict(f"GetEvaluator returned {detail.get('evaluatorId')!r} for "
                            f"{evaluator_id!r} — reference not bound")
        if detail.get("status") not in USABLE_EVALUATOR_STATUSES:
            raise RuntimeError(f"evaluator {evaluator_id} is {detail.get('status')}, not usable")
        res["result"] = {"evaluator_id": evaluator_id, "evaluator_arn": detail.get("evaluatorArn"),
                         "level": detail.get("level"), "status": detail.get("status"),
                         "name": detail.get("evaluatorName"), "source": "existing"}

    def _step_evaluator(self, db, op, resources, res) -> None:
        plan = self.plan
        assert plan is not None
        control = self._client("bedrock-agentcore-control")
        entry = next(e for e in plan.evaluators if e.key == res["plan_key"])
        request = self._evaluator_request(entry, res, resources)
        stored = res.get("result") or {}
        if res.get("request") is None:
            res["request"] = request
            self.fence.save(db, op, resources, f"{res['key']}:intent")
        elif res["request"] != request:
            raise _Conflict("the persisted create request differs from the plan — refused")
        if not stored.get("evaluator_id"):
            try:
                created = self._write(db, control.create_evaluator, **request)
            except ClientError as exc:
                if _code(exc) in _CONFLICT_CODES:
                    raise _Conflict(f"an evaluator named {entry.name} exists that this "
                                    "operation cannot prove it created (token replay refused)"
                                    ) from exc
                raise
            res["result"] = {"evaluator_id": created["evaluatorId"],
                             "evaluator_arn": created.get("evaluatorArn")}
            res["owned"] = True
            self.fence.save(db, op, resources, f"{res['key']}:accepted")
            stored = res["result"]
        detail = None
        for _ in range(READBACK_ATTEMPTS):
            detail = control.get_evaluator(evaluatorId=stored["evaluator_id"])
            if detail.get("status") in USABLE_EVALUATOR_STATUSES:
                break
            if detail.get("status") in ("FAILED", "DELETING"):
                raise RuntimeError(f"evaluator status {detail.get('status')}")
            self.sleep(READBACK_DELAY_S)
        if detail is None or detail.get("status") not in USABLE_EVALUATOR_STATUSES:
            raise RuntimeError("evaluator did not become ACTIVE in time")
        mismatch = [k for k, want in (
            ("evaluatorId", stored["evaluator_id"]), ("evaluatorName", request["evaluatorName"]),
            ("level", request["level"]), ("evaluatorConfig", request["evaluatorConfig"]),
        ) if detail.get(k) != want]
        if mismatch:
            raise _Conflict(f"evaluator readback differs from the request on {mismatch}")
        stored.update({"status": detail.get("status"), "level": detail.get("level"),
                       "name": detail.get("evaluatorName"),
                       "evaluator_arn": detail.get("evaluatorArn") or stored.get("evaluator_arn")})

    def _evaluator_request(self, entry, res, resources) -> dict[str, Any]:
        base = {"evaluatorName": entry.name, "description": entry.description or entry.title,
                "clientToken": res["client_token"]}
        if isinstance(entry, plan_contract.JudgeEvaluator):
            return {**base, "level": entry.level, "evaluatorConfig": {"llmAsAJudge": {
                "instructions": entry.instructions,
                "ratingScale": {"numerical": [r.model_dump() for r in entry.rating_scale]},
                "modelConfig": {"bedrockEvaluatorModelConfig": {"modelId": entry.model_id}},
            }}}
        if isinstance(entry, plan_contract.DerivedEvaluator):
            level = ALL_BUILTIN_EVALUATORS.get(entry.base_evaluator_id)
            if level is None:
                level = str(self._client("bedrock-agentcore-control")
                            .get_evaluator(evaluatorId=entry.base_evaluator_id)["level"])
            return {**base, "level": level, "evaluatorConfig": {"derived": {
                "baseEvaluatorId": entry.base_evaluator_id,
                "modelConfig": {"bedrockEvaluatorModelConfig": {"modelId": entry.model_id}},
            }}}
        fn = _resource(resources, "lambda_function")
        version_arn = (fn.get("result") or {}).get("version_arn")
        if not version_arn:
            raise RuntimeError("code evaluator needs the published Lambda version")
        return {**base, "level": entry.level, "evaluatorConfig": {"codeBased": {"lambdaConfig": {
            "lambdaArn": version_arn, "lambdaTimeoutInSeconds": entry.lambda_timeout_s}}}}


# ---------------------------------------------------------------------------
# lease + dispatch
# ---------------------------------------------------------------------------


def claim_lease(db: Session, op_id: str) -> str | None:
    """Conditional UPDATE under the operation's host lock: one writer. Returns the
    lease token when this caller now owns the run, ``None`` when the operation is
    finished / exhausted / being cleaned."""
    token = secrets.token_hex(16)
    rows = db.execute(
        update(EvaluationAssetOperation)
        .where(
            EvaluationAssetOperation.id == op_id,
            EvaluationAssetOperation.status.in_(ACTIVE_STATUSES),
            EvaluationAssetOperation.attempts < MAX_ATTEMPTS,
        )
        .values(status="running", worker_token=token, heartbeat_at=_now(),
                attempts=EvaluationAssetOperation.attempts + 1, error=None)
    ).rowcount
    db.commit()
    return token if rows == 1 else None


def run_operation(
    op_id: str, *, clients: ClientFactory = _default_clients,
    sleeper: Callable[[float], None] = time.sleep,
) -> bool:
    """Run (or resume) one operation synchronously. False when another worker holds
    the operation's host lock or nothing is left to run."""
    with _flock(op_id) as held:
        if not held:
            return False
        db = SessionLocal()
        try:
            token = claim_lease(db, op_id)
        finally:
            db.close()
        if token is None:
            return False
        _Runner(op_id, clients, sleeper, token).run()
        return True


def start_async(op_id: str, **kwargs: Any) -> threading.Thread | None:
    with _LIVE_LOCK:
        current = _LIVE.get(op_id)
        if current is not None and current.is_alive():
            return None
        thread = threading.Thread(target=run_operation, args=(op_id,), kwargs=kwargs,
                                  name=f"eval-assets-{op_id}", daemon=True)
        _LIVE[op_id] = thread
        thread.start()
        return thread


def resume_operations() -> list[str]:
    """Startup: re-wake ONLY operations an administrator explicitly approved and that
    were interrupted (queued/running). The host lock decides whether a worker is
    still alive — a quick restart resumes at once, a live worker is never stolen.
    Finished, partial and failed operations need an explicit retry."""
    db = SessionLocal()
    try:
        ids = [
            r[0] for r in db.query(EvaluationAssetOperation.id)
            .filter(EvaluationAssetOperation.status.in_(("queued", "running")))
            .all()
        ]
    finally:
        db.close()
    return [op_id for op_id in ids if start_async(op_id) is not None]


def retry_operation(db: Session, op: EvaluationAssetOperation) -> bool:
    """Explicit retry of a partial/failed operation (bounded by MAX_ATTEMPTS)."""
    if op.status in ("succeeded", "cleaned", "cleaning"):
        return False
    if op.attempts >= MAX_ATTEMPTS:
        raise AppError("assistant.evaluation_assets_exhausted",
                       f"the operation reached its {MAX_ATTEMPTS} attempts; clean up the owned "
                       "resources and prepare a new plan revision", status_code=409)
    if live_worker(op.id) is not None or not _flock_free(op.id):
        raise AppError("assistant.evaluation_assets_running",
                       "the operation is still running", status_code=409)
    db.execute(update(EvaluationAssetOperation)
               .where(EvaluationAssetOperation.id == op.id,
                      EvaluationAssetOperation.status.in_(("partial", "failed")))
               .values(status="queued", worker_token=None))
    db.commit()
    return start_async(op.id) is not None


# ---------------------------------------------------------------------------
# cleanup of owned cloud artifacts (never the dataset, never foreign resources)
# ---------------------------------------------------------------------------


def cleanup_operation(
    db: Session, op: EvaluationAssetOperation, workspace: WorkspaceContext, *,
    clients: ClientFactory = _default_clients,
) -> EvaluationAssetOperation:
    """Delete exactly the cloud resources this operation proved it owns, dependency
    first, one persisted checkpoint per effect, under the same host lock / lease
    fence / re-authorization / pinned-identity checks as the worker.

    * evaluators first (identity, name, level and config read back must equal the
      recorded request; an owned evaluator that changed stays as a reviewable
      ``conflict``, an evaluator locked by an online configuration stays
      ``delete_failed``);
    * the additive grant, the function (with its resource policy), the log group and
      the dedicated role are removed ONLY when no owned evaluator remains — and each
      only after its identity (RoleId / CodeSha256 / provenance tag) still matches;
    * the local Dataset stays (a member asset); foreign resources are never touched;
      ``cleaned`` is recorded only when nothing owned remains.
    """
    if op.status == "cleaned":
        return op  # idempotent: nothing owned remains
    with _flock(op.id) as held:
        if not held or live_worker(op.id) is not None:
            raise AppError("assistant.evaluation_assets_running",
                           "the operation is still running; wait for it to finish",
                           status_code=409)
        token = secrets.token_hex(16)
        rows = db.execute(
            update(EvaluationAssetOperation)
            .where(EvaluationAssetOperation.id == op.id,
                   EvaluationAssetOperation.status.in_(CLEANABLE_STATUSES))
            .values(status="cleaning", worker_token=token)
        ).rowcount
        db.commit()
        if rows != 1:
            raise AppError("assistant.evaluation_assets_running",
                           f"the operation is {op.status}; nothing to clean up", status_code=409)
        fence = _Fence(op.id, token, status="cleaning")
        try:
            return _cleanup(db, fence, workspace, clients)
        except _LeaseLost as exc:
            raise AppError("assistant.evaluation_assets_running",
                           "another actor took over the operation", status_code=409) from exc
        except _Stop as exc:
            db.rollback()
            db.execute(update(EvaluationAssetOperation)
                       .where(EvaluationAssetOperation.id == op.id,
                              EvaluationAssetOperation.worker_token == token)
                       .values(status="partial", error=f"cleanup stopped: {exc}",
                               worker_token=None))
            db.commit()
            raise AppError("assistant.evaluation_assets_stopped", f"cleanup stopped: {exc}",
                           status_code=409) from exc


def _cleanup(db: Session, fence: _Fence, workspace: WorkspaceContext,
             clients: ClientFactory) -> EvaluationAssetOperation:
    op = fence.guard(db)
    resources = json.loads(json.dumps(op.resources or []))
    by_key = {r["key"]: r for r in resources}

    def mark(r: dict[str, Any], status: str, note: str | None = None) -> None:
        r["cleanup"] = {"at": _now().isoformat(), "ok": status == "deleted", "note": note}
        r["status"] = status

    def checkpoint(event: str) -> None:
        nonlocal op
        fence.save(db, op, resources, event)
        op = fence.load(db)

    def gone(exc: ClientError) -> bool:
        return _code(exc) in _NOT_FOUND_CODES

    def owned(r: dict[str, Any]) -> bool:
        return bool(r.get("owned")) and bool(r.get("result"))

    # 1. evaluators (identity + config must still be ours)
    control = clients(workspace, "bedrock-agentcore-control")
    for r in [x for x in resources if x["kind"] == "evaluator"]:
        if not owned(r) or r.get("status") == "deleted":
            if r.get("status") not in ("deleted",) and not owned(r):
                mark(r, "deleted", "nothing created by this operation")
            continue
        eid = r["result"].get("evaluator_id")
        request = r.get("request") or {}
        try:
            detail = control.get_evaluator(evaluatorId=eid)
        except ClientError as exc:
            if gone(exc):
                mark(r, "deleted", "already gone")
                checkpoint(f"{r['key']}:gone")
                continue
            mark(r, "delete_failed", _safe_error(exc))
            checkpoint(f"{r['key']}:readback_failed")
            continue
        if detail.get("evaluatorId") != eid or detail.get("evaluatorName") != request.get(
                "evaluatorName") or detail.get("evaluatorConfig") != request.get(
                "evaluatorConfig") or detail.get("level") != request.get("level"):
            mark(r, "conflict", "owned evaluator was changed after creation — review it before "
                                "deleting; not removed")
            checkpoint(f"{r['key']}:drift")
            continue
        try:
            fence.guard(db)
            control.delete_evaluator(evaluatorId=eid)
            mark(r, "deleted")
        except ClientError as exc:
            mark(r, "deleted" if gone(exc) else "delete_failed",
                 None if gone(exc) else _safe_error(exc))
        checkpoint(f"{r['key']}:cleanup")
    evaluators_left = [r["key"] for r in resources if r["kind"] == "evaluator"
                       and owned(r) and r.get("status") != "deleted"]
    # 2. the code chain — only once every owned evaluator is gone (dependency DAG)
    chain = [by_key[k] for k in ("role_grant", "lambda_permission", "lambda_function",
                                 "log_group", "lambda_role") if k in by_key]
    if evaluators_left:
        for r in chain:
            if owned(r) and r.get("status") != "deleted":
                mark(r, "retained", "kept: owned evaluator(s) still reference the function: "
                                    + ", ".join(evaluators_left))
        checkpoint("chain:retained")
    else:
        fn = by_key.get("lambda_function")
        fn_result = (fn or {}).get("result") or {}
        grant = by_key.get("role_grant")
        if grant and owned(grant) and grant.get("status") != "deleted":
            iam = clients(workspace, "iam")
            try:
                current = iam.get_role(RoleName=_role_name(grant["result"]["role_arn"]))["Role"]
                back = _policy_document(iam.get_role_policy(
                    RoleName=_role_name(grant["result"]["role_arn"]),
                    PolicyName=grant["result"]["policy_name"]).get("PolicyDocument"))
                if current.get("RoleId") != grant["result"].get("role_id"):
                    mark(grant, "conflict", "execution role was replaced — grant left untouched")
                elif back != grant["result"].get("policy_document"):
                    mark(grant, "conflict", "grant document differs from ours — left untouched")
                else:
                    fence.guard(db)
                    iam.delete_role_policy(RoleName=_role_name(grant["result"]["role_arn"]),
                                           PolicyName=grant["result"]["policy_name"])
                    mark(grant, "deleted")
            except ClientError as exc:
                mark(grant, "deleted" if gone(exc) else "delete_failed",
                     None if gone(exc) else _safe_error(exc))
            checkpoint("role_grant:cleanup")
        if fn and owned(fn) and fn.get("status") != "deleted":
            lam = clients(workspace, "lambda")
            try:
                cfg = lam.get_function(FunctionName=fn_result["function_name"]).get(
                    "Configuration") or {}
                if cfg.get("CodeSha256") != code_sha256_b64(fn["digest"]):
                    mark(fn, "conflict", "function code differs from ours — left untouched")
                else:
                    fence.guard(db)
                    lam.delete_function(FunctionName=fn_result["function_name"])
                    mark(fn, "deleted")
                    perm = by_key.get("lambda_permission")
                    if perm and perm.get("status") not in ("pending", "blocked"):
                        mark(perm, "deleted", "deleted with the function")
            except ClientError as exc:
                mark(fn, "deleted" if gone(exc) else "delete_failed",
                     None if gone(exc) else _safe_error(exc))
            checkpoint("lambda_function:cleanup")
        lg = by_key.get("log_group")
        if lg and owned(lg) and lg.get("status") != "deleted":
            logs = clients(workspace, "logs")
            try:
                if not _Runner._log_group_is_ours(logs, lg["name"], lg["nonce"]):
                    groups = logs.describe_log_groups(logGroupNamePrefix=lg["name"]).get(
                        "logGroups") or []
                    if any(g.get("logGroupName") == lg["name"] for g in groups):
                        mark(lg, "conflict", "log group no longer carries our provenance — "
                                             "left untouched")
                    else:
                        mark(lg, "deleted", "already gone")
                else:
                    fence.guard(db)
                    logs.delete_log_group(logGroupName=lg["name"])
                    mark(lg, "deleted")
            except ClientError as exc:
                mark(lg, "deleted" if gone(exc) else "delete_failed",
                     None if gone(exc) else _safe_error(exc))
            checkpoint("log_group:cleanup")
        role = by_key.get("lambda_role")
        if role and owned(role) and role.get("status") != "deleted" and (
                fn is None or fn.get("status") in ("deleted", "pending", "blocked")):
            iam = clients(workspace, "iam")
            try:
                current = iam.get_role(RoleName=role["name"])["Role"]
                if current["RoleId"] != role["result"]["role_id"]:
                    mark(role, "conflict", "RoleId differs — not deleting a role we did not create")
                else:
                    fence.guard(db)
                    try:
                        iam.delete_role_policy(RoleName=role["name"], PolicyName=LOGS_POLICY_NAME)
                    except ClientError as exc:
                        if not gone(exc):
                            raise
                    iam.delete_role(RoleName=role["name"])
                    mark(role, "deleted")
            except ClientError as exc:
                mark(role, "deleted" if gone(exc) else "delete_failed",
                     None if gone(exc) else _safe_error(exc))
            checkpoint("lambda_role:cleanup")
    remaining = [r["key"] for r in resources
                 if owned(r) and r.get("status") not in ("deleted", "skipped")]
    final = "cleaned" if not remaining else "partial"
    error = None if not remaining else "cleanup incomplete: " + ", ".join(remaining)
    fence.save(db, op, resources, "cleanup:finished", status=final, error=error,
               worker_token=None)
    db.expire_all()
    return db.get(EvaluationAssetOperation, op.id)


# ---------------------------------------------------------------------------
# managed-association projection for the ordinary Evaluation surfaces
# ---------------------------------------------------------------------------


def managed_evaluator(
    db: Session, workspace_id: str | None, evaluator_id: str
) -> dict[str, Any] | None:
    """The operation that owns ``evaluator_id`` (created it and has not cleaned it), with
    the plan facts other surfaces enforce: ``reference_dependent`` (needs ground truth)
    and the definition kind."""
    query = db.query(EvaluationAssetOperation).filter(
        EvaluationAssetOperation.status.notin_(("cleaned",)))
    if workspace_id is not None:
        query = query.filter(EvaluationAssetOperation.workspace_id == workspace_id)
    for op in query.all():
        for r in op.resources or []:
            if r.get("kind") == "evaluator" and (r.get("result") or {}).get(
                    "evaluator_id") == evaluator_id and r.get("status") != "deleted":
                return {"operation_id": op.id, "conversation_id": op.conversation_id,
                        "plan_revision": op.plan_revision, "plan_key": r.get("plan_key"),
                        "definition": r.get("definition"),
                        "reference_dependent": bool(r.get("reference_dependent"))}
    return None


def owned_operation(
    db: Session, conversation: AssistantConversation, op_id: str
) -> EvaluationAssetOperation:
    op = db.get(EvaluationAssetOperation, op_id)
    if op is None or op.conversation_id != conversation.id \
            or op.workspace_id != conversation.workspace_id:
        raise NotFoundError("assistant.operation_not_found", "operation not found")
    return op


def managed_reference_gap(
    db: Session, workspace_id: str | None, evaluator_ids: list[str], available: set[str],
) -> dict[str, list[str]]:
    """Managed reference-driven CODE evaluators whose ground truth the given scope lacks
    (``available`` = fields the dataset can supply; empty for live / online traffic).
    Judges are covered by their prompt placeholders elsewhere; a code evaluator's rules
    are only known here, from its owning operation's plan."""
    gap: dict[str, list[str]] = {}
    for evaluator_id in evaluator_ids:
        owner = managed_evaluator(db, workspace_id, evaluator_id)
        if not owner or owner.get("definition") != "code" or not owner.get(
                "reference_dependent"):
            continue
        plan = db.get(AssistantEvaluationPlan, db.get(
            EvaluationAssetOperation, owner["operation_id"]).plan_id)
        needs: set[str] = set()
        for e in (plan.content or {}).get("evaluators") or [] if plan else []:
            if e.get("kind") == "code" and e.get("key") == owner.get("plan_key"):
                for c in (e.get("rules") or {}).get("checks") or []:
                    if c.get("type") == "reference_trajectory":
                        needs.add("expected_tool_trajectory")
                    if c.get("type") == "reference_response":
                        needs.add("expected_response")
        missing = sorted(needs - available)
        if missing:
            gap[evaluator_id] = missing
    return gap
