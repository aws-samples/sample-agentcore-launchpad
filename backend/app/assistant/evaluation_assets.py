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

**Ownership.** Only a service-issued identity returned to THIS operation proves
ownership: RoleId / ARN from CreateRole, the log group's creationTime / ARN read right
after CreateLogGroup, the FunctionArn / RevisionId returned by CreateFunction, the
evaluatorId / ARN returned by CreateEvaluator (whose ``clientToken`` is natively
idempotent). The provenance nonce travels inside the resource (role description + tag,
log-group tag, the Lambda package bytes → ``CodeSha256``) only as a *clue* for a
reviewer: it is copyable content, so after a lost response a resource found under our
name is **unknown** (never adopted, never deleted, dependents retained) and a
collision established at creation time is a **foreign collision** (``conflict``,
never adopted, never deleted). Every dispatched create is recorded durably BEFORE the
call (``intent`` / ``request`` / ``create_history``); cleanup reasons from that
history, never from a display status — ``pending`` / ``blocked`` with a dispatched
create is an effect that may exist.

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
import functools
import hashlib
import io
import json
import logging
import os
import re
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
from botocore.loaders import Loader
from botocore.model import ServiceModel, Shape
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
                         "cleanup", "owned", "recovered", "review", "reviews")


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
    # ownership and CURRENT authorization are predicates of the same write: a demotion /
    # disablement / owner change committed by another session before this UPDATE makes
    # it a no-op, so no approved plan or operation is ever persisted for a stale caller
    owner_ok = select(AssistantConversation.id).where(
        AssistantConversation.id == conversation.id,
        AssistantConversation.owner_principal == op.owner_principal,
    ).exists()
    conditions = [
        AssistantEvaluationPlan.id == plan_row.id,
        AssistantEvaluationPlan.status == "draft",
        AssistantEvaluationPlan.content_hash == plan_hash,
        AssistantEvaluationPlan.revision == newest,
        owner_ok,
    ]
    if op.approver_user_id is not None:
        conditions.append(select(User.id).where(
            User.id == op.approver_user_id, User.role == ROLE_ADMIN, User.status == "active",
            (User.expires_at.is_(None)) | (User.expires_at > _now()),
        ).exists())
    claimed = db.execute(
        update(AssistantEvaluationPlan).where(*conditions).values(status="approved")
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


class _Unknown(RuntimeError):
    """A create call whose response was lost before the service-issued identity was
    recorded, while a resource with our name now exists. Ownership is UNPROVABLE:
    a nonce / tag / digest is copyable content, not identity. Recorded as ``unknown``
    (never adopted, never deleted); re-evaluated on retry, dependents retained."""


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
        if not op.pinned:
            raise _Stop("operation predates workspace identity pinning — review required; "
                        "prepare a new plan revision instead")
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


THIRD_PARTY_PREFIX = "ThirdParty."  # AWS-managed partner evaluators (DeepEval, AutoEval…)

FUNCTION_IDENTITY_FIELDS = ("FunctionArn", "Version", "CodeSha256", "Role", "Runtime",
                            "Handler", "Timeout", "MemorySize", "RevisionId")
# The allowlisted raw CreateFunction answer kept IMMUTABLY on the intent at acceptance
# (SE-049): the initial RevisionId, the provisioning state and LastModified are what a
# later review compares the settled function and its CloudTrail create event against.
# Nothing here is a credential or an actor; ``ResponseMetadata.RequestId`` is stored
# separately as ``request_id``.
CREATE_RESPONSE_FIELDS = (
    "FunctionName", "FunctionArn", "Version", "CodeSha256", "Role", "Runtime", "Handler",
    "Timeout", "MemorySize", "Description", "CodeSize", "RevisionId", "State", "StateReason",
    "StateReasonCode", "LastUpdateStatus", "LastModified", "PackageType", "Architectures",
    "EphemeralStorage", "Environment", "Layers", "VpcConfig", "KMSKeyArn",
    "FileSystemConfigs", "DeadLetterConfig", "SigningProfileVersionArn", "SigningJobArn",
    "LoggingConfig", "TracingConfig", "SnapStart", "ImageConfigResponse",
    "RuntimeVersionConfig",
)
# Optional, security-relevant configuration that must be EQUAL between the verified
# CreateFunction answer and the settled ``$LATEST`` for a revision review: equal code and
# role alone must never bless changed extras. Empty containers count as absent.
OPTIONAL_CONFIG_FIELDS = (
    "Description", "PackageType", "Architectures", "EphemeralStorage", "Environment",
    "Layers", "VpcConfig", "KMSKeyArn", "FileSystemConfigs", "DeadLetterConfig",
    "SigningProfileVersionArn", "SigningJobArn", "LoggingConfig", "TracingConfig",
    "SnapStart", "ImageConfigResponse", "RuntimeVersionConfig",
)
LAMBDA_CREATE_EVENT_NAMES = ("CreateFunction20150331",)
LAMBDA_EVENT_SOURCE = "lambda.amazonaws.com"
CLOUDTRAIL_PAGE_BUDGET = 5


INVENTORY_PAGE_BUDGET = 50


class _IncompleteInventory(RuntimeError):
    """A paginated Lambda inventory that could NOT be read to a valid terminal page:
    page budget exhausted with a NextMarker remaining, a repeated / unusable marker, a
    malformed page, a missing or wrong-typed collection, or a malformed entry. Never an
    empty or complete inventory — the caller records it and refuses ready / delete."""


def _paged_inventory(fetch: Callable[..., Any], collection: str, identity: str, what: str,
                     ) -> list[dict[str, Any]]:
    """Read a Lambda ``Marker`` / ``NextMarker`` inventory to completion. Returns entries
    ONLY after a structurally valid terminal page (no ``NextMarker``) — bounded by
    ``INVENTORY_PAGE_BUDGET`` pages, and explicit about every reason it could not finish."""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    marker: str | None = None
    for _ in range(INVENTORY_PAGE_BUDGET):
        page = fetch(**({"Marker": marker} if marker else {}))
        if not isinstance(page, dict):
            raise _IncompleteInventory(f"{what}: page is not an object")
        entries = page.get(collection)
        if not isinstance(entries, list):
            raise _IncompleteInventory(f"{what}: page carries no {collection} list")
        for e in entries:
            if not isinstance(e, dict) or not isinstance(e.get(identity), str) \
                    or not e.get(identity):
                raise _IncompleteInventory(f"{what}: entry without a usable {identity}")
        out += entries
        nxt = page.get("NextMarker")
        if nxt is None or nxt == "":
            return out  # the only way out with data: a terminal page
        if not isinstance(nxt, str) or nxt in seen or nxt == marker:
            raise _IncompleteInventory(f"{what}: unusable or repeated NextMarker")
        seen.add(nxt)
        marker = nxt
    raise _IncompleteInventory(f"{what}: {INVENTORY_PAGE_BUDGET} pages read and a NextMarker "
                               "remains — inventory not complete")


def _function_versions(lam: Any, name: str) -> list[dict[str, Any]]:
    return _paged_inventory(lambda **kw: lam.list_versions_by_function(FunctionName=name, **kw),
                            "Versions", "Version", f"versions of {name}")


def _function_aliases(lam: Any, name: str) -> list[dict[str, Any]]:
    return _paged_inventory(lambda **kw: lam.list_aliases(FunctionName=name, **kw),
                            "Aliases", "Name", f"aliases of {name}")


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


_LIFECYCLE_FIELDS = ("State", "LastUpdateStatus")


def _latest_drift(cfg: dict[str, Any], approved: dict[str, Any],
                  revision_id: str | None) -> list[str]:
    bad = sorted(k for k, want in approved.items() if cfg.get(k) != want)
    if not cfg.get("RevisionId"):
        bad.append("RevisionId")
    elif revision_id is not None and cfg.get("RevisionId") != revision_id:
        bad.append("RevisionId")
    return bad


def initial_revision_conflict_eligible(res: dict[str, Any]) -> bool:
    """Is this Lambda intent's RevisionId drift the FIRST-initialization transition —
    an accepted, owned CreateFunction whose ``$LATEST`` was never published, given
    concurrency or re-pinned by this platform, so the recorded baseline still IS the
    initial RevisionId the service answered with? Anything else (a re-pinned baseline, a
    published version, a publish intent, a lost create) is ordinary drift and stays a
    conflict no review can lift."""
    stored = res.get("result") or {}
    initial = stored.get("initial_revision_id") or (stored.get("created_identity") or {}).get(
        "RevisionId")
    return bool(
        res.get("owned") and stored.get("function_arn") and initial
        and stored.get("revision_id") == initial
        and not stored.get("version") and not stored.get("version_arn")
        and not stored.get("publish_requested_at") and not stored.get("readback")
        and not stored.get("latest_readback")
        and not (res.get("request") or {}).get("Publish")
    )


def initialization_transition_evidence(stored: dict[str, Any], cfg: dict[str, Any]
                                       ) -> dict[str, Any] | None:
    """Is a RevisionId-only drift PROVABLY Lambda's own Pending → Active transition?

    Lambda bumps ``RevisionId`` when a new function leaves ``Pending``; nothing else about
    the function changes. Any UpdateFunctionCode / UpdateFunctionConfiguration moves
    ``LastModified`` (and the field it changed), so the evidence is: CreateFunction
    answered ``Pending``, ``$LATEST`` is now ``Active`` / ``Successful``, ``LastModified``
    is byte-identical to the accepted answer, and every recorded identity field except
    ``RevisionId`` — plus every optional configuration member — is unchanged. Returns
    the evidence that is written to the audit trail, or None when the drift is anything
    else (which stays a review-required conflict)."""
    answer = stored.get("create_response") or {}
    identity = stored.get("created_identity") or {}
    if answer.get("State") != "Pending":
        return None
    if cfg.get("State") != "Active" or cfg.get("LastUpdateStatus") != "Successful":
        return None
    if not answer.get("LastModified") or cfg.get("LastModified") != answer.get("LastModified"):
        return None
    changed = [k for k in FUNCTION_IDENTITY_FIELDS
               if k != "RevisionId" and cfg.get(k) != identity.get(k)]
    changed += [k for k in OPTIONAL_CONFIG_FIELDS
                if (cfg.get(k) or None) != (answer.get(k) or None)]
    if changed or not cfg.get("RevisionId"):
        return None
    return {"create_state": answer.get("State"),
            "create_state_reason_code": answer.get("StateReasonCode"),
            "last_modified": cfg.get("LastModified"), "state": cfg.get("State"),
            "last_update_status": cfg.get("LastUpdateStatus"),
            "code_sha256": cfg.get("CodeSha256"), "unchanged": "LastModified and every "
            "identity / optional configuration member; only RevisionId moved"}


def _retryable_conflict(res: dict[str, Any]) -> bool:
    """Conflicts an explicit retry may re-attempt without adopting anything: a read-only
    ``existing`` binding (no ownership, no write), and an owned Lambda whose only drift is
    the initialization transition the worker can settle with evidence."""
    if res.get("status") != "conflict":
        return False
    if res.get("kind") == "existing":
        return True
    return (res.get("kind") == "lambda_function"
            and (res.get("review") or {}).get("kind") == "initial_revision_changed"
            and not (res.get("review") or {}).get("resolved_by")
            and initial_revision_conflict_eligible(res))


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
            for r in op.resources or []:
                if r["kind"] in CODE_CHAIN and r["kind"] != "role_grant" and r.get(
                        "status") == "conflict":
                    chain_broken = f"{r['key']} is {r['status']} ({r.get('error')})"
                    break
            for key in [r["key"] for r in op.resources or []]:
                op = self.fence.guard(db)
                resources = json.loads(json.dumps(op.resources or []))
                res = _resource(resources, key)
                # ``unknown`` is re-evaluated (the operator may have removed the resource;
                # a native idempotency token may recover an evaluator) — never adopted
                if res.get("status") in ("ready", "skipped", "conflict"):
                    continue
                if res.get("status") == "blocked" and not chain_broken:
                    pass  # prerequisite repaired earlier in this run: attempt it now
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
                except _Unknown as exc:
                    res["status"] = "unknown"
                    res["error"] = str(exc)
                    self.fence.save(db, op, resources, f"{key}:unknown")
                    if res["kind"] in CODE_CHAIN and res["kind"] != "role_grant":
                        chain_broken = f"{key} is unknown ({exc})"
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
                                  if r.get("status") in ("failed", "conflict", "blocked",
                                                         "unknown"))[:2000]
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
        from app.evaluation.routers import _validate_items  # dataset ingress gate

        _validate_items(items)
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
            if current["RoleId"] != stored["role_id"] or current.get("Arn") != stored.get(
                    "role_arn"):
                raise _Conflict(f"role {name} was replaced (RoleId / ARN differ) — not ours")
        else:
            prior = bool(res.get("intent"))  # an earlier attempt already issued a create
            res["intent"] = {"requested_at": _now().isoformat(),
                             "dispatched": int((res.get("intent") or {}).get("dispatched") or 0)
                             + 1}
            self.fence.save(db, op, resources, "lambda_role:intent")
            try:
                created = self._write(
                    db, iam.create_role, RoleName=name,
                    AssumeRolePolicyDocument=json.dumps(trust), Description=description,
                    Tags=[{"Key": TAG_OPERATION, "Value": op.id},
                          {"Key": TAG_MANAGED, "Value": "true"},
                          {"Key": TAG_PROVENANCE, "Value": nonce}],
                )["Role"]
            except (_LeaseLost, _Stop):
                raise
            except Exception as exc:  # noqa: BLE001 — classified below
                if isinstance(exc, ClientError) and _code(exc) not in _CONFLICT_CODES:
                    raise  # a definite rejection: nothing was created
                # name taken (collision) or the response was lost: the service-issued
                # RoleId was never recorded, and a nonce in a description / tag is
                # copyable content — ownership cannot be proven either way
                try:
                    current = iam.get_role(RoleName=name)["Role"]
                except ClientError as look:
                    if _code(look) in _NOT_FOUND_CODES and not isinstance(exc, ClientError):
                        raise exc from look  # transport failure and nothing exists: plain failed
                    raise
                if not isinstance(exc, ClientError) or prior:
                    raise _Unknown(
                        f"a CreateRole response was lost and a role named {name} exists; "
                        "its RoleId was never recorded, so this operation cannot prove it "
                        "created it (a provenance nonce is copyable) — review it manually, "
                        "then retry") from exc
                raise _Conflict(f"an IAM role named {name} already exists and was not "
                                "created by this operation") from exc
            expected_arn = f"arn:aws:iam::{op.account_id}:role/{name}"
            if created.get("Arn") != expected_arn:
                raise _Conflict(f"role ARN {created.get('Arn')} is not {expected_arn}")
            res["result"] = {"role_arn": created["Arn"], "role_id": created["RoleId"],
                             "create_date": str(created.get("CreateDate") or ""),
                             "trust_document": trust}
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
            prior = bool(res.get("intent"))  # an earlier attempt already issued a create
            res["intent"] = {"requested_at": _now().isoformat(),
                             "dispatched": int((res.get("intent") or {}).get("dispatched") or 0)
                             + 1}
            self.fence.save(db, op, resources, "log_group:intent")
            try:
                self._write(db, logs.create_log_group, logGroupName=name,
                            tags={TAG_OPERATION: op.id, TAG_MANAGED: "true",
                                  TAG_PROVENANCE: nonce})
            except (_LeaseLost, _Stop):
                raise
            except Exception as exc:  # noqa: BLE001 — classified below
                if isinstance(exc, ClientError) and _code(exc) not in _CONFLICT_CODES:
                    raise
                groups = logs.describe_log_groups(logGroupNamePrefix=name).get("logGroups") or []
                if not any(g.get("logGroupName") == name for g in groups):
                    if isinstance(exc, ClientError):
                        raise
                    raise exc from None  # transport failure, nothing exists: plain failed
                if not isinstance(exc, ClientError) or prior:
                    # the service-issued creationTime was never recorded; a provenance tag
                    # is copyable content — ownership cannot be proven
                    raise _Unknown(
                        f"a CreateLogGroup response was lost and a log group named {name} "
                        "exists; its creation identity was never recorded, so this operation "
                        "cannot prove it created it — review it manually, then retry") from exc
                raise _Conflict(f"log group {name} already exists and was not created by "
                                "this operation") from exc
            # CreateLogGroup returns no identity: read the service-issued creationTime /
            # ARN NOW, before the acceptance checkpoint and before any further write. Until
            # this read succeeds the create is a dispatched intent without identity (a
            # retry that finds the name taken records ``unknown``, never adopts).
            mine = self._describe_log_group(logs, name)
            if mine is None:
                raise RuntimeError(f"log group {name} was created but could not be read back")
            expected_arn = f"arn:aws:logs:{op.region}:{op.account_id}:log-group:{name}"
            if str(mine.get("arn") or "").rstrip("*").rstrip(":") != expected_arn \
                    or not mine.get("creationTime"):
                raise _Conflict(f"log group identity {mine.get('arn')!r} / "
                                f"{mine.get('creationTime')!r} is not the expected {expected_arn}")
            res["result"] = {"created": True, "arn": mine.get("arn"),
                             "creation_time": mine.get("creationTime")}
            res["owned"] = True
            self.fence.save(db, op, resources, "log_group:accepted")
        rec = res["result"]
        # identity FIRST, then the write: a re-created group (creationTime differs) is not
        # ours and never receives our retention policy
        mine = self._describe_log_group(logs, name)
        if mine is None:
            raise RuntimeError(f"log group {name} disappeared after creation")
        if not rec.get("creation_time") or not rec.get("arn"):
            raise _Unknown(f"log group {name}: creation identity was never recorded — "
                           "ownership cannot be proven; review it manually")
        if mine.get("creationTime") != rec["creation_time"] or mine.get("arn") != rec["arn"]:
            raise _Conflict("log group was re-created (creationTime / ARN differ) — not ours")
        self._write(db, logs.put_retention_policy, logGroupName=name,
                    retentionInDays=LOG_RETENTION_DAYS)
        mine = self._describe_log_group(logs, name)
        if mine is None or mine.get("retentionInDays") != LOG_RETENTION_DAYS:
            raise _Conflict("log group readback differs (retention)")
        if mine.get("creationTime") != rec["creation_time"] or mine.get("arn") != rec["arn"]:
            raise _Conflict("log group was re-created during the retention write — not ours")
        rec["retention_days"] = LOG_RETENTION_DAYS

    def _describe_log_group(self, logs: Any, name: str) -> dict[str, Any] | None:
        for attempt in range(3):
            groups = logs.describe_log_groups(logGroupNamePrefix=name).get("logGroups") or []
            mine = [g for g in groups if g.get("logGroupName") == name]
            if mine:
                return mine[0]
            if attempt < 2:
                self.sleep(READBACK_DELAY_S)
        return None

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
            prior = bool(res.get("request"))  # an earlier attempt already issued a create
            dispatched = int(res.get("dispatched") or 0) + 1
            res["dispatched"] = dispatched
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
                except (_LeaseLost, _Stop):
                    raise
                except Exception as exc:  # noqa: BLE001 — classified below
                    code = _code(exc) if isinstance(exc, ClientError) else ""
                    if isinstance(exc, ClientError) and code not in _CONFLICT_CODES:
                        if code == "InvalidParameterValueException" and "assumed" in str(exc) \
                                and attempt < 5:
                            self.sleep(5)  # IAM propagation of the brand-new role
                            continue
                        raise
                    # name taken or response lost: the package (and its embedded nonce) is
                    # downloadable content, so an equal CodeSha256 proves nothing about
                    # WHO created the function — never adopted
                    try:
                        lam.get_function(FunctionName=name)
                    except ClientError as look:
                        if _code(look) in _NOT_FOUND_CODES and not isinstance(exc, ClientError):
                            raise exc from look  # nothing exists: plain failed
                        raise
                    if not isinstance(exc, ClientError) or prior:
                        raise _Unknown(
                            f"a CreateFunction response was lost and a function named {name} "
                            "exists; its service identity was never recorded, so this "
                            "operation cannot prove it created it — review it manually, then "
                            "retry") from exc
                    raise _Conflict(f"a Lambda function named {name} exists that this "
                                    "operation cannot prove it created") from exc
            # the service-issued identity returned to THIS call is persisted immediately
            # and every approved field is verified before acceptance — a response that
            # does not describe the reviewed function is never our baseline
            approved = self._approved_latest(op, name, role_arn, sha_b64, int(res["timeout_s"]))
            bad = sorted(k for k, want in approved.items()
                         if k not in _LIFECYCLE_FIELDS and created.get(k) != want)
            if bad or not created.get("RevisionId"):
                raise _Conflict(f"CreateFunction answered with an identity that differs from "
                                f"the reviewed request on {bad or ['RevisionId']}")
            # the raw lifecycle answer (initial RevisionId, State / StateReasonCode,
            # LastModified, request id) is kept immutably next to the identity: a
            # revision that moves while the function provisions is reviewed against it
            res["result"] = {"function_arn": created["FunctionArn"], "function_name": name,
                             "revision_id": created["RevisionId"],
                             "initial_revision_id": created["RevisionId"],
                             "created_identity": {k: created.get(k)
                                                  for k in FUNCTION_IDENTITY_FIELDS},
                             "create_response": {k: created[k] for k in CREATE_RESPONSE_FIELDS
                                                 if k in created},
                             "request_id": (created.get("ResponseMetadata") or {}).get(
                                 "RequestId")}
            res["owned"] = True
            self.fence.save(db, op, resources, "lambda_function:accepted")
        stored = res["result"]
        if not stored.get("revision_id"):
            raise _Unknown(f"function {name}: the service identity returned by CreateFunction "
                           "was never recorded — ownership cannot be proven; review manually")
        approved = self._approved_latest(op, name, role_arn, sha_b64, int(res["timeout_s"]))
        cfg = self._wait_function_active(lam, name)
        # $LATEST must still be exactly what the service returned to us (RevisionId included)
        # before ANY further write; our own later writes re-pin it deliberately below. A
        # RevisionId that ALONE moved during the first initialization is not rebased
        # automatically: it is recorded as a review-required conflict an administrator
        # settles with the CreateFunction CloudTrail event (review_lambda_initial_revision).
        drift = _latest_drift(cfg, approved, stored["revision_id"])
        if drift == ["RevisionId"] and initial_revision_conflict_eligible(res):
            evidence = initialization_transition_evidence(stored, cfg)
            if evidence is not None:
                # Lambda's own Pending → Active transition, proven by an unchanged
                # LastModified and configuration: settled here with an append-only
                # audit entry, never a silent rebase. Anything less stays a review.
                now = _now().isoformat()
                settle_id = secrets.token_hex(8)
                stored.setdefault("revision_history", []).append(
                    {"at": now, "from": stored["revision_id"], "to": cfg["RevisionId"],
                     "reason": "initial_activation_settled", "settle_id": settle_id,
                     "evidence": evidence})
                stored["revision_id"] = cfg["RevisionId"]
                stored["settled_revision_id"] = cfg["RevisionId"]
                if res.get("review"):
                    res["review"] = {**res["review"], "resolved_by": settle_id,
                                     "resolution": "initial_activation_settled"}
                self.fence.save(db, op, resources, "lambda_function:settled")
                drift = []
        if drift == ["RevisionId"] and initial_revision_conflict_eligible(res):
            res["review"] = {
                "kind": "initial_revision_changed",
                "observed_revision_id": cfg.get("RevisionId"),
                "observed_last_modified": cfg.get("LastModified"),
                "observed_at": _now().isoformat(),
                "resolution": "lambda-revision-review",
            }
            raise _Conflict("$LATEST differs from the approved identity before publish on "
                            "['RevisionId'] — the function was changed or replaced; refusing "
                            "to continue. The RevisionId moved while the function was still "
                            "provisioning: an administrator may review it against the "
                            "CreateFunction CloudTrail event (lambda-revision-review)")
        self._require_latest(cfg, approved, stored["revision_id"], "before publish")
        if not stored.get("settled_revision_id"):
            stored["settled_revision_id"] = cfg["RevisionId"]  # unchanged since creation
        if not stored.get("version"):
            # immediately before the FIRST resumed mutation: the function must still be
            # exactly what was verified (the reviewed baseline when a review re-queued
            # this intent) and nobody may have published, aliased, given a policy or
            # reserved concurrency to it meanwhile — those are never adopted or overwritten
            dispatched = int(stored.get("publish_dispatches") or 0)
            self._prepublish_guard(lam, op, resources, stored, dispatched)
            stored["publish_requested_at"] = _now().isoformat()
            stored["publish_dispatches"] = dispatched + 1
            self.fence.save(db, op, resources, "lambda_function:publish_intent")
            try:
                # RevisionId is a real precondition of PublishVersion (installed model): the
                # publish fails instead of blessing a function replaced in the window
                published = self._write(db, lam.publish_version, FunctionName=name,
                                        CodeSha256=sha_b64, RevisionId=stored["revision_id"])
            except ClientError as exc:
                if _code(exc) == "PreconditionFailedException":
                    raise _Conflict("$LATEST changed between our readback and PublishVersion "
                                    "(RevisionId precondition failed) — not publishing") from exc
                if _code(exc) not in _CONFLICT_CODES + ("InvalidParameterValueException",):
                    raise
                if dispatched == 0:
                    # our very first PublishVersion was refused: a same-digest version that
                    # exists now was NOT published by this operation (the digest is
                    # downloadable content) — never adopted
                    raise _Conflict("PublishVersion was refused on this operation's first "
                                    "dispatch; a version carrying the reviewed digest is not "
                                    "ours to adopt — review required") from exc
                published = None
            # reconcile against the real version list: exactly ONE published version
            # may carry our digest (Lambda does not re-publish unchanged code) — and only
            # because THIS operation dispatched a publish whose answer may have been lost
            versions = [v for v in _function_versions(lam, name)
                        if v.get("Version") != "$LATEST" and v.get("CodeSha256") == sha_b64]
            if published is not None and published.get("Version") not in {
                    v.get("Version") for v in versions}:
                versions.append(published)
            if len(versions) != 1:
                raise _Conflict(f"{len(versions)} published versions carry the reviewed digest "
                                "— refusing to pick one")
            stored["version"] = versions[0]["Version"]
            stored["version_arn"] = versions[0]["FunctionArn"]
            # our own publish may legitimately move $LATEST's RevisionId: re-pin it right
            # after that exact effect, with every other approved field still equal
            latest = lam.get_function(FunctionName=name).get("Configuration") or {}
            self._require_latest(latest, approved, None, "after publish")
            stored["revision_id"] = latest["RevisionId"]
            self.fence.save(db, op, resources, "lambda_function:published")
        latest = lam.get_function(FunctionName=name).get("Configuration") or {}
        self._require_latest(latest, approved, stored["revision_id"], "before concurrency")
        reserved = lam.get_function_concurrency(FunctionName=name).get(
            "ReservedConcurrentExecutions")
        if reserved is not None and reserved != LAMBDA_RESERVED_CONCURRENCY:
            raise _Conflict(f"reserved concurrency {reserved} was set by someone else — not "
                            "overwriting it")
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
            "FunctionArn": f"arn:aws:lambda:{op.region}:{op.account_id}:function:{name}:"
                           f"{stored['version']}",
        }
        mismatch = sorted(k for k, want in expected.items() if cfg.get(k) != want)
        if mismatch:
            raise _Conflict(f"published version readback differs on {mismatch}")
        if not cfg.get("RevisionId"):
            raise _Conflict("the published version carries no RevisionId — identity incomplete")
        # the settled whole-function snapshot (after the platform's LAST write): cleanup
        # deletes the whole function only if $LATEST (every approved field, RevisionId
        # included), the reserved concurrency, the version set and the alias set are still
        # exactly these. Our own concurrency write is the last effect, so the RevisionId is
        # re-pinned deliberately here and nowhere later.
        latest = lam.get_function(FunctionName=name).get("Configuration") or {}
        self._require_latest(latest, approved, None, "after concurrency")
        versions = sorted(v.get("Version") for v in _function_versions(lam, name))
        aliases = sorted(a.get("Name") for a in _function_aliases(lam, name))
        if versions != sorted(["$LATEST", stored["version"]]) or aliases:
            raise _Conflict("the function carries versions / aliases this operation did not "
                            f"publish: {versions} {aliases}")
        stored["readback"] = {k: cfg.get(k) for k in expected}
        stored["readback"]["ReservedConcurrentExecutions"] = reserved
        stored["readback"]["RevisionId"] = cfg.get("RevisionId")
        stored["latest_readback"] = {k: latest.get(k) for k in FUNCTION_IDENTITY_FIELDS}
        stored["revision_id"] = latest["RevisionId"]
        stored["versions"] = versions
        stored["aliases"] = aliases

    def _prepublish_guard(self, lam: Any, op, resources: list[dict[str, Any]],
                          stored: dict[str, Any], dispatched: int) -> None:
        """Before the operation's first mutation of an accepted function: no version this
        operation did not dispatch, no alias, a proven-absent resource policy, no reserved
        concurrency; and, after a reviewed recovery, the whole configuration, the tags and
        the dependencies exactly as the review verified them."""
        name = stored["function_name"]
        sha_b64 = (stored.get("created_identity") or {}).get("CodeSha256")
        baseline = stored.get("reviewed_baseline")
        if baseline:
            got = lam.get_function(FunctionName=name)
            cfg, tags = got.get("Configuration") or {}, got.get("Tags") or {}
            drift = _members_differ(cfg, baseline.get("configuration") or {})
            if drift or tags != (baseline.get("tags") or {}):
                raise _Conflict(f"$LATEST differs from the reviewed baseline on "
                                f"{drift or ['Tags']} — refusing to continue")
            bad, _ = _dependency_drift(self._client("iam"), self._client("logs"), op.id,
                                       resources)
            if bad:
                raise _Conflict(f"the function's dependencies differ from the reviewed "
                                f"baseline ({bad}) — refusing to continue")
        versions = sorted(v.get("Version") for v in _function_versions(lam, name)
                          if v.get("Version") == "$LATEST" or dispatched == 0
                          or v.get("CodeSha256") != sha_b64)
        if versions != ["$LATEST"]:
            raise _Conflict(f"the function carries versions {versions} this operation did not "
                            "publish — not adopting")
        aliases = sorted(a.get("Name") for a in _function_aliases(lam, name))
        if aliases:
            raise _Conflict(f"the function carries aliases {aliases} this operation did not "
                            "create — refusing to continue")
        try:
            _policy_absent(lam, name)
        except _ReviewRefused as exc:
            raise _Conflict(str(exc)) from exc
        reserved = lam.get_function_concurrency(FunctionName=name).get(
            "ReservedConcurrentExecutions")
        if reserved is not None:
            raise _Conflict(f"reserved concurrency {reserved} is set before this operation's "
                            "first write — not overwriting it")

    @staticmethod
    def _approved_latest(op, name: str, role_arn: str, sha_b64: str, timeout_s: int
                         ) -> dict[str, Any]:
        """Every approved field of the reviewed function's ``$LATEST``."""
        return {"FunctionName": name,
                "FunctionArn": f"arn:aws:lambda:{op.region}:{op.account_id}:function:{name}",
                "Runtime": LAMBDA_RUNTIME, "Handler": LAMBDA_HANDLER, "Role": role_arn,
                "CodeSha256": sha_b64, "Timeout": timeout_s, "MemorySize": LAMBDA_MEMORY_MB,
                "Version": "$LATEST", "State": "Active", "LastUpdateStatus": "Successful"}

    @staticmethod
    def _require_latest(cfg: dict[str, Any], approved: dict[str, Any],
                        revision_id: str | None, when: str) -> None:
        bad = _latest_drift(cfg, approved, revision_id)
        if bad:
            raise _Conflict(f"$LATEST differs from the approved identity {when} on {bad} — "
                            "the function was changed or replaced; refusing to continue")

    def _wait_function_active(self, lam: Any, name: str) -> dict[str, Any]:
        """Bounded wait for ``State == Active`` AND ``LastUpdateStatus == Successful``
        (the documented end of the first initialization). ``Failed`` on either is a
        failure; ``InProgress`` / a missing status is unknown and cannot be published."""
        cfg: dict[str, Any] = {}
        for _ in range(READBACK_ATTEMPTS):
            cfg = lam.get_function_configuration(FunctionName=name)
            state, update = cfg.get("State"), cfg.get("LastUpdateStatus")
            if state == "Active" and update == "Successful":
                return cfg
            if state == "Failed" or update == "Failed":
                raise RuntimeError(f"function state {state} / last update {update}: "
                                   f"{cfg.get('StateReason') or cfg.get('LastUpdateStatusReason')}")
            self.sleep(READBACK_DELAY_S)
        raise RuntimeError(f"function did not become Active / Successful in time (State="
                           f"{cfg.get('State')}, LastUpdateStatus={cfg.get('LastUpdateStatus')})")

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
        """Bind an EXISTING evaluator reference: canonical builtins (incl. trajectory)
        by catalog; custom ids by exact identity, exactly one complete configuration and
        — decoded from that real configuration, or from the owning plan's rules for a
        code evaluator this platform created in the same workspace — reference coverage
        of every target the plan's scenarios produce. A reference the plan cannot feed
        is never 'ready'."""
        from app.evaluation import coverage

        plan = self.plan
        assert plan is not None
        evaluator_id = res["name"]
        items = [plan_contract.dataset_item(sc, {"plan": "coverage"}) for sc in plan.scenarios]
        if evaluator_id in coverage.KNOWN_BUILTINS:
            level = coverage.KNOWN_BUILTINS[evaluator_id]
            gaps = coverage.coverage_gaps(items, coverage.builtin_needs(evaluator_id), level)
            if gaps:
                raise _Conflict(f"{evaluator_id} reads expected_tool_trajectory but these targets "
                                f"carry none: {', '.join(gaps[:6])}")
            res["result"] = {"evaluator_id": evaluator_id, "level": level, "source": "builtin",
                             "reference_needs": sorted(coverage.builtin_needs(evaluator_id))}
            res["reference_dependent"] = bool(coverage.builtin_needs(evaluator_id))
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
        if evaluator_id.startswith(THIRD_PARTY_PREFIX):
            # an AWS-managed partner evaluator: a partition-level resource (no region /
            # account in its ARN), locked, with no evaluatorConfig to bind and no
            # reference input — bound by exact identity, never by configuration
            self._check_third_party_identity(detail, evaluator_id)
            if detail.get("status") not in USABLE_EVALUATOR_STATUSES:
                raise RuntimeError(f"evaluator {evaluator_id} is {detail.get('status')}, "
                                   "not usable")
            res["result"] = {"evaluator_id": evaluator_id,
                             "evaluator_arn": detail.get("evaluatorArn"),
                             "level": detail.get("level"), "status": detail.get("status"),
                             "name": detail.get("evaluatorName"), "source": "third_party",
                             "provider": detail.get("provider"), "definition": None,
                             "reference_needs": [], "note": None}
            res["reference_dependent"] = False
            return
        self._check_evaluator_identity(op, detail, evaluator_id)
        if detail.get("status") not in USABLE_EVALUATOR_STATUSES:
            raise RuntimeError(f"evaluator {evaluator_id} is {detail.get('status')}, not usable")
        # A code evaluator this platform created in the SAME workspace has its declarative
        # rules on record (its owning operation's plan): decode its needs from them and
        # hold them against every target of THIS plan. Another workspace's association is
        # never read; a code evaluator nobody here owns keeps its explicit unknown note.
        owner = managed_evaluator(db, op.workspace_id, evaluator_id)
        rules = managed_rules(db, owner) if owner and owner.get("definition") == "code" \
            else None
        if rules is not None:
            conflicts = plan_contract.code_rule_capability_conflict_errors(
                [plan_contract.CodeCheck.model_validate(check) for check in rules],
                self.proposal_content, evaluator_key=evaluator_id, scenarios=plan.scenarios,
            )
            if conflicts:
                raise _Conflict("; ".join(conflicts))
        needs, kind = coverage.needs_from_config(detail, rules)
        note = None
        source = "existing"
        if needs is None:
            note = ("external code evaluator: its reference requirements are unknown to this "
                    "platform — verify them before running it on this Dataset")
        else:
            if rules is not None:
                source = "managed"
                note = (f"managed code evaluator owned by operation {owner['operation_id']}: "
                        "reference needs decoded from its plan rules")
            gaps = coverage.coverage_gaps(items, needs, str(detail.get("level")))
            if gaps:
                raise _Conflict(f"{evaluator_id} reads {', '.join(sorted(needs))} but these "
                                f"targets carry none: {', '.join(gaps[:6])}")
        res["result"] = {"evaluator_id": evaluator_id, "evaluator_arn": detail.get("evaluatorArn"),
                         "level": detail.get("level"), "status": detail.get("status"),
                         "name": detail.get("evaluatorName"), "source": source,
                         "definition": kind,
                         "reference_needs": sorted(needs) if needs is not None else None,
                         "note": note}
        res["reference_dependent"] = bool(needs) if needs is not None else None

    def _step_evaluator(self, db, op, resources, res) -> None:
        plan = self.plan
        assert plan is not None
        control = self._client("bedrock-agentcore-control")
        entry = next(e for e in plan.evaluators if e.key == res["plan_key"])
        request = self._evaluator_request(entry, res, resources)
        stored = res.get("result") or {}
        legacy_request = res.get("request") is not None  # persisted by an EARLIER attempt
        if not legacy_request:
            res["request"] = request
            self.fence.save(db, op, resources, f"{res['key']}:intent")
        elif res["request"] != request:
            raise _Conflict("the persisted create request differs from the plan — refused")
        if not stored.get("evaluator_id"):
            # every dispatch is recorded durably BEFORE the call; an entry that never gets
            # an outcome (crash) or a lost response is uncertain forever, and no later
            # rejection can erase it — only the token replay answering with an id can
            history = res.get("create_history")
            if history is None:
                history = res["create_history"] = []
                if legacy_request and _uncertain_create(res):
                    # a request persisted before dispatch histories existed, with no id
                    # and no recorded definite rejection: an earlier create may have
                    # succeeded — migrate that uncertainty durably, together with the new
                    # dispatch record, BEFORE the call (a later 403 / 409 cannot erase it)
                    history.append({"at": _now().isoformat(), "outcome": "legacy-uncertain",
                                    "note": "request persisted by an earlier attempt without "
                                            "an id or a recorded rejection"})
            history.append({"at": _now().isoformat(), "outcome": "dispatched"})
            self.fence.save(db, op, resources, f"{res['key']}:dispatch")
            uncertain_before = _uncertain_create(res, exclude_last=True)
            try:
                created = self._write(db, control.create_evaluator, **request)
            except (_LeaseLost, _Stop):
                raise
            except ClientError as exc:
                http = (exc.response.get("ResponseMetadata") or {}).get("HTTPStatusCode")
                if _code(exc) in _CONFLICT_CODES:
                    history[-1]["outcome"] = "conflict"
                    if uncertain_before:
                        raise _Unknown(
                            f"an evaluator named {entry.name} exists and an earlier create of "
                            "this operation has no recorded outcome — it may be ours; the "
                            "token replay did not answer with an id, so ownership stays "
                            "unproven; review manually") from exc
                    raise _Conflict(f"an evaluator named {entry.name} exists that this "
                                    "operation cannot prove it created (token replay refused)"
                                    ) from exc
                if isinstance(http, int) and 400 <= http < 500:
                    history[-1]["outcome"] = "rejected"  # this dispatch: not created
                else:
                    history[-1]["outcome"] = "lost"  # 5xx / unknown: may have been created
                raise
            except Exception:
                history[-1]["outcome"] = "lost"
                raise
            history[-1]["outcome"] = "created"
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
        self._check_evaluator_identity(op, detail, stored["evaluator_id"])
        stored.update({"status": detail.get("status"), "level": detail.get("level"),
                       "name": detail.get("evaluatorName"),
                       "evaluator_arn": detail.get("evaluatorArn") or stored.get("evaluator_arn")})

    @staticmethod
    def _check_third_party_identity(detail: dict[str, Any], evaluator_id: str) -> None:
        """Exact AWS-managed identity: the partition-level ARN, the ThirdParty type and a
        supported level — the same shape ``Builtin.*`` carries."""
        expected_arn = f"arn:aws:bedrock-agentcore:::evaluator/{evaluator_id}"
        if detail.get("evaluatorArn") != expected_arn:
            raise _Conflict(f"evaluator ARN {detail.get('evaluatorArn')!r} is not {expected_arn}")
        if detail.get("evaluatorType") != "ThirdParty":
            raise _Conflict(f"evaluator {evaluator_id} is {detail.get('evaluatorType')!r}, "
                            "not an AWS-managed third-party evaluator")
        if detail.get("level") not in ("TRACE", "TOOL_CALL", "SESSION"):
            raise _Conflict(f"evaluator level {detail.get('level')!r} is not supported")

    @staticmethod
    def _check_evaluator_identity(op, detail: dict[str, Any], evaluator_id: str) -> None:
        """Exact ARN (partition/region/account/resource), supported level and a known
        configuration kind — never another identity or an unknown config."""
        expected_arn = (f"arn:aws:bedrock-agentcore:{op.region}:{op.account_id}:evaluator/"
                        f"{evaluator_id}")
        if detail.get("evaluatorArn") != expected_arn:
            raise _Conflict(f"evaluator ARN {detail.get('evaluatorArn')!r} is not {expected_arn}")
        if detail.get("level") not in ("TRACE", "TOOL_CALL", "SESSION"):
            raise _Conflict(f"evaluator level {detail.get('level')!r} is not supported")
        from app.evaluation import coverage

        problem = coverage.validate_evaluator_config(detail.get("evaluatorConfig"))
        if problem:
            raise _Conflict(f"evaluator configuration is not bindable: {problem}")

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
    resources = json.loads(json.dumps(op.resources or []))
    reopened = [r["key"] for r in resources if _retryable_conflict(r)]
    for r in resources:
        if r["key"] in reopened:
            r["status"], r["error"] = "pending", None
    values: dict[str, Any] = {"status": "queued", "worker_token": None}
    if reopened:
        values["resources"] = resources
        values["log"] = (op.log or "") + json.dumps(
            {"at": _now().isoformat(), "event": "retry:reopened", "resources": reopened},
            ensure_ascii=False) + "\n"
    db.execute(update(EvaluationAssetOperation)
               .where(EvaluationAssetOperation.id == op.id,
                      EvaluationAssetOperation.status.in_(("partial", "failed")))
               .values(**values))
    db.commit()
    return start_async(op.id) is not None


# ---------------------------------------------------------------------------
# SE-049 — reviewed recovery of ONE conflict: the initial Lambda RevisionId moved
# ---------------------------------------------------------------------------


@dataclass
class RevisionReview:
    operation: EvaluationAssetOperation
    review: dict[str, Any]
    started: bool  # True ⇔ this call recorded the review and re-queued the worker


@functools.lru_cache(maxsize=1)
def _lambda_model() -> ServiceModel:
    """The INSTALLED Lambda service model: structure member names decide which keys are
    casing-normalized between the SDK and CloudTrail renderings; maps (Environment
    Variables, Tags) and scalars are compared verbatim."""
    return ServiceModel(Loader().load_service_model("lambda", "service-2"), service_name="lambda")


def _from_cloudtrail(value: Any, shape: Shape) -> Any:
    """Rebuild the SDK casing of a CloudTrail ``requestParameters`` / ``responseElements``
    value along the installed model: CloudTrail lowers the first character of structure
    member names (``functionArn``, ``kMSKeyArn``), so a member is matched case-insensitively
    against the shape's members; an unknown member is kept verbatim (it can never equal an
    approved member and is refused); map keys and every scalar stay untouched — a data-map
    key's case or an empty string value is content, never normalized."""
    if shape.type_name == "structure":
        if not isinstance(value, dict):
            return value
        members = {m.lower(): (m, sub) for m, sub in shape.members.items()}
        out: dict[str, Any] = {}
        for k, v in value.items():
            hit = members.get(str(k).lower())
            out[hit[0] if hit else str(k)] = _from_cloudtrail(v, hit[1]) if hit else v
        return out
    if shape.type_name == "list":
        return [_from_cloudtrail(v, shape.member) for v in value] if isinstance(value, list) \
            else value
    if shape.type_name == "map":
        return {str(k): _from_cloudtrail(v, shape.value) for k, v in value.items()} \
            if isinstance(value, dict) else value
    return value


_MISSING = object()  # "the member is absent" — never equal to null, "" or a wrong type


def _members_differ(left: dict[str, Any], right: dict[str, Any]) -> list[str]:
    """Members that differ between two SDK-cased configurations, presence included: a
    member present with ``null`` or another type is a difference from an absent one."""
    return sorted(k for k in set(left) | set(right)
                  if left.get(k, _MISSING) != right.get(k, _MISSING)
                  or type(left.get(k, _MISSING)) is not type(right.get(k, _MISSING)))


_SCALAR_TYPES: dict[str, tuple[type, ...]] = {
    "string": (str,), "integer": (int,), "long": (int,), "boolean": (bool,),
    "double": (int, float), "float": (int, float), "timestamp": (str, datetime),
    "blob": (bytes, str),
}


def _shape_problems(value: Any, shape: Shape, path: str) -> list[str]:
    """Type problems of an SDK-cased value against the installed model shape. A modelled
    member must carry a value of its modelled type (``null`` is never valid); an
    unmodelled member is kept for the unapproved-member refusal but may not be ``null``."""
    if shape.type_name == "structure":
        if not isinstance(value, dict):
            return [f"{path}: not a structure"]
        out: list[str] = []
        for k, v in value.items():
            member = shape.members.get(str(k))
            if member is None:
                if v is None:
                    out.append(f"{path}.{k}: null")
                continue
            out += _shape_problems(v, member, f"{path}.{k}")
        return out
    if shape.type_name == "list":
        if not isinstance(value, list):
            return [f"{path}: not a list"]
        return [p for i, v in enumerate(value) for p in _shape_problems(v, shape.member,
                                                                         f"{path}[{i}]")]
    if shape.type_name == "map":
        if not isinstance(value, dict):
            return [f"{path}: not a map"]
        out = []
        for k, v in value.items():
            if not isinstance(k, str):
                out.append(f"{path}: non-string key")
            out += _shape_problems(v, shape.value, f"{path}[{k!r}]")
        return out
    allowed = _SCALAR_TYPES.get(shape.type_name)
    if allowed is None:
        return []
    if isinstance(value, bool) and bool not in allowed:
        return [f"{path}: boolean where {shape.type_name} expected"]
    if not isinstance(value, allowed):
        return [f"{path}: {type(value).__name__} where {shape.type_name} expected"]
    return []


def _validated(value: Any, shape_name: str, what: str) -> dict[str, Any]:
    """Model-validate an SDK-cased configuration and fold its well-formed empty envelopes;
    a malformed value (``null``, wrong type) is refused, never normalized."""
    problems = _shape_problems(value, _lambda_model().shape_for(shape_name), what)
    if problems:
        raise _ReviewRefused("assistant.lambda_revision_review_unverified",
                             f"{what} is malformed against the installed Lambda model "
                             f"({problems[:5]}) — refusing to compare it", fields=problems[:20])
    return _fold_envelopes(value)


def _fold_envelopes(cfg: dict[str, Any]) -> dict[str, Any]:
    """The ONLY absent-versus-empty equivalences, on documented service envelopes of the
    function configuration (CloudTrail answers ``environment: {}`` where the SDK omits the
    member; an unset VPC reads as empty lists), and only for their WELL-FORMED empty shape:
    ``Environment`` ``{}`` / ``{"Variables": {}}``, ``Layers`` / ``FileSystemConfigs`` ``[]``,
    a ``VpcConfig`` whose lists are empty lists, ``VpcId`` an empty string and
    ``Ipv6AllowedForDualStack`` false. A ``null``, an empty string or a list where a map is
    expected is not empty — it is malformed and stays a difference. Nothing inside a data
    map is touched."""
    out = dict(cfg)
    env = out.get("Environment", _MISSING)
    if isinstance(env, dict) and set(env) <= {"Variables"} \
            and env.get("Variables", {}) == {} and isinstance(env.get("Variables", {}), dict):
        out.pop("Environment")
    for member in ("Layers", "FileSystemConfigs"):
        if member in out and isinstance(out[member], list) and out[member] == []:
            out.pop(member)
    vpc = out.get("VpcConfig", _MISSING)
    if isinstance(vpc, dict) \
            and set(vpc) <= {"SubnetIds", "SecurityGroupIds", "VpcId", "Ipv6AllowedForDualStack"} \
            and all(isinstance(vpc.get(k, []), list) and vpc.get(k, []) == []
                    for k in ("SubnetIds", "SecurityGroupIds")) \
            and isinstance(vpc.get("VpcId", ""), str) and vpc.get("VpcId", "") == "" \
            and vpc.get("Ipv6AllowedForDualStack", False) is False:
        out.pop("VpcConfig")
    return out


# Lifecycle members that legitimately differ between the CreateFunction answer (Pending)
# and the settled $LATEST (Active / Successful, new RevisionId); everything else must be
# identical, member for member.
_FUNCTION_LIFECYCLE_MEMBERS = ("State", "StateReason", "StateReasonCode", "LastUpdateStatus",
                               "LastUpdateStatusReason", "LastUpdateStatusReasonCode",
                               "RevisionId")
# Members the reviewed request fixes exactly (checked against the recorded request).
_REQUEST_APPROVED_MEMBERS = ("FunctionName", "Runtime", "Role", "Handler", "Description",
                             "Timeout", "MemorySize")
# Documented service defaults for members the reviewed request does NOT set. A member the
# answer carries must equal the request's value, one of these defaults, or be refused.
_FUNCTION_DEFAULTS: dict[str, Any] = {
    "PackageType": "Zip", "Architectures": ["x86_64"], "TracingConfig": {"Mode": "PassThrough"},
    "EphemeralStorage": {"Size": 512},
    "SnapStart": {"ApplyOn": "None", "OptimizationStatus": "Off"},
}
_RUNTIME_VERSION_ARN = r"^arn:aws:lambda:[a-z0-9-]+::runtime:[0-9a-f]+$"


def _unapproved_members(response: dict[str, Any], request: dict[str, Any], *, name: str,
                        region: str) -> list[str]:
    """Every member of a (SDK-cased, envelope-folded) CreateFunction answer that is neither
    fixed by the reviewed request, a documented default, the service-issued identity /
    lifecycle, or an informational member with a checkable shape — unknown means refused."""
    bad: list[str] = []
    identity = {"FunctionArn", "Version", "CodeSha256", "LastModified", "CodeSize",
                *_FUNCTION_LIFECYCLE_MEMBERS}
    for member, value in response.items():
        if member in _REQUEST_APPROVED_MEMBERS:
            if value != request.get(member):
                bad.append(member)
        elif member in identity:
            if member == "CodeSize" and not (isinstance(value, int) and value > 0):
                bad.append(member)
        elif member in _FUNCTION_DEFAULTS:
            if value != (request[member] if member in request else _FUNCTION_DEFAULTS[member]):
                bad.append(member)
        elif member == "LoggingConfig":
            if value != request.get("LoggingConfig", {"LogFormat": "Text",
                                                      "LogGroup": f"/aws/lambda/{name}"}):
                bad.append(member)
        elif member == "RuntimeVersionConfig":
            arn = value.get("RuntimeVersionArn") if isinstance(value, dict) else None
            if set(value or {}) != {"RuntimeVersionArn"} or not isinstance(arn, str) \
                    or not re.match(_RUNTIME_VERSION_ARN, arn) \
                    or not arn.startswith(f"arn:aws:lambda:{region}::runtime:"):
                bad.append(member)
        elif member in request:
            if value != request[member]:
                bad.append(member)
        else:
            bad.append(member)  # Environment, Layers, VPC, KMS, DurableConfig, … not approved
    return bad


class _ReviewRefused(AppError):
    def __init__(self, code: str, message: str, status_code: int = 409, **detail: Any) -> None:
        super().__init__(code, message, status_code=status_code, detail=detail or None)


def _lookup_create_event(cloudtrail: Any, event_id: str) -> dict[str, Any]:
    """Server-side read of the nominated CloudTrail event (never the client's JSON).
    Exactly one, well-formed event or the review fails closed."""
    events: list[dict[str, Any]] = []
    kwargs: dict[str, Any] = {"LookupAttributes": [{"AttributeKey": "EventId",
                                                    "AttributeValue": event_id}],
                              "MaxResults": 50}
    for _ in range(CLOUDTRAIL_PAGE_BUDGET):
        page = cloudtrail.lookup_events(**kwargs)
        events.extend(page.get("Events") or [])
        token = page.get("NextToken")
        if not token or len(events) > 1:
            break
        kwargs["NextToken"] = token
    else:
        raise _ReviewRefused("assistant.lambda_revision_review_unverified",
                             "CloudTrail event history did not terminate within the page budget")
    if len(events) != 1:
        raise _ReviewRefused(
            "assistant.lambda_revision_review_unverified",
            f"CloudTrail returned {len(events)} events for the nominated event id (event history "
            "is eventually consistent; exactly one CreateFunction record is required)")
    raw = events[0].get("CloudTrailEvent")
    try:
        event = json.loads(raw) if isinstance(raw, str) else None
    except ValueError:
        event = None
    if not isinstance(event, dict) or events[0].get("EventId") != event_id \
            or event.get("eventID") != event_id:
        raise _ReviewRefused("assistant.lambda_revision_review_unverified",
                             "the CloudTrail record is malformed or names a different event id")
    return event


def _verify_create_event(event: dict[str, Any], op: EvaluationAssetOperation,
                         res: dict[str, Any], expected_created: str) -> dict[str, Any]:
    """The nominated event must be THE successful CreateFunction this operation
    dispatched: source, operation, account, region; its request must equal the recorded
    request member for member (an extra member is not approved); its answer must carry the
    recorded FunctionArn / initial RevisionId / CodeSha256 in the provisioning state, every
    other member fixed by the request or a documented default (unknown → refused), and —
    when the acceptance kept the raw answer — equal that immutable snapshot. Returns the
    answer in SDK casing."""
    stored = res.get("result") or {}
    request = res.get("request") or {}
    bad: list[str] = []
    if event.get("eventSource") != LAMBDA_EVENT_SOURCE:
        bad.append("eventSource")
    if event.get("eventName") not in LAMBDA_CREATE_EVENT_NAMES:
        bad.append("eventName")
    if event.get("errorCode") or event.get("errorMessage"):
        bad.append("errorCode")
    if event.get("readOnly") not in (False, "false"):
        bad.append("readOnly")
    if event.get("awsRegion") != op.region:
        bad.append("awsRegion")
    if event.get("recipientAccountId") != op.account_id:
        bad.append("recipientAccountId")
    if not event.get("requestID") or not isinstance(event.get("requestID"), str):
        bad.append("requestID")
    if stored.get("request_id") and event.get("requestID") != stored["request_id"]:
        bad.append("requestID")
    raw_params = event.get("requestParameters")
    raw_response = event.get("responseElements")
    if not isinstance(raw_params, dict) or not isinstance(raw_response, dict):
        raise _ReviewRefused("assistant.lambda_revision_review_unverified",
                             "the CloudTrail record carries no request / response elements")
    model = _lambda_model()
    params = _from_cloudtrail(raw_params, model.shape_for("CreateFunctionRequest"))
    params.pop("Code", None)  # the package bytes are never rendered; the digest is compared
    params = _validated(params, "CreateFunctionRequest", "the event's requestParameters")
    approved_request = _fold_envelopes({k: v for k, v in request.items() if k != "CodeSha256"})
    bad += [f"requestParameters.{k}" for k in _members_differ(params, approved_request)]
    if request.get("Publish") or params.get("Publish") not in (False, None):
        bad.append("requestParameters.Publish")
    response = _validated(_from_cloudtrail(raw_response, model.shape_for("FunctionConfiguration")),
                          "FunctionConfiguration", "the event's responseElements")
    expect_response = {
        "FunctionName": res.get("name"), "FunctionArn": stored.get("function_arn"),
        "Version": "$LATEST", "CodeSha256": request.get("CodeSha256"),
        "RevisionId": expected_created, "State": "Pending", "StateReasonCode": "Creating",
    }
    for field, want in expect_response.items():
        if want in (None, "") or response.get(field) != want:
            bad.append(f"responseElements.{field}")
    if not isinstance(response.get("LastModified"), str) or not response.get("LastModified"):
        bad.append("responseElements.LastModified")
    bad += [f"responseElements.{m}" for m in _unapproved_members(
        {k: v for k, v in response.items() if k not in expect_response},
        request, name=str(res.get("name")), region=op.region)]
    # a create accepted after SE-049 kept the raw answer: the event must agree with it on
    # every allowlisted member — present, absent and equal alike
    snapshot = stored.get("create_response")
    if isinstance(snapshot, dict):
        folded = _validated(snapshot, "FunctionConfiguration", "the accepted create answer")
        bad += [f"create_response.{field}" for field in _members_differ(
            {k: v for k, v in folded.items() if k in CREATE_RESPONSE_FIELDS},
            {k: v for k, v in response.items() if k in CREATE_RESPONSE_FIELDS})]
    if bad:
        raise _ReviewRefused(
            "assistant.lambda_revision_review_unverified",
            "the CloudTrail event is not the successful CreateFunction this operation "
            f"recorded (differs on {sorted(set(bad))})", fields=sorted(set(bad)))
    return response


def _policy_absent(lam: Any, name: str) -> None:
    """Complete configured absence of a resource policy is proven ONLY by a NotFound;
    any returned document — empty, foreign or malformed — is not absence."""
    try:
        got = lam.get_policy(FunctionName=name)
    except ClientError as exc:
        if _code(exc) in _NOT_FOUND_CODES:
            return
        raise
    doc = _policy_document((got or {}).get("Policy"))
    if not isinstance(doc, dict):
        raise _ReviewRefused("assistant.lambda_revision_review_unverified",
                             "GetPolicy answered with an unreadable document — absence of a "
                             "resource policy cannot be proven")
    raise _ReviewRefused("assistant.lambda_revision_review_unverified",
                         "the function carries a resource policy this operation did not write")


def _read_function_inventory(lam: Any, name: str) -> dict[str, Any]:
    try:
        versions = sorted(v.get("Version") for v in _function_versions(lam, name))
        aliases = sorted(a.get("Name") for a in _function_aliases(lam, name))
    except _IncompleteInventory as exc:
        raise _ReviewRefused("assistant.lambda_revision_review_unverified", str(exc)) from exc
    reserved = lam.get_function_concurrency(FunctionName=name).get("ReservedConcurrentExecutions")
    return {"versions": versions, "aliases": aliases, "reserved_concurrency": reserved}


def _verify_settled_function(lam: Any, op: EvaluationAssetOperation, res: dict[str, Any],
                             response: dict[str, Any], expected_created: str,
                             expected_current: str) -> dict[str, Any]:
    """The settled ``$LATEST`` must be the verified create answer plus EXACTLY the lifecycle
    transition: every non-lifecycle member identical (present, absent and equal alike — an
    extra member such as a durable / tenancy / capacity / master configuration is a
    difference), ``Active`` / ``Successful``, the nominated current RevisionId, the approved
    tags; still ``$LATEST`` only, no alias, a proven-absent resource policy, no reserved
    concurrency. Returns the strict baseline the resumed worker re-validates."""
    name = res["name"]
    request = res.get("request") or {}
    try:
        got = lam.get_function(FunctionName=name)
    except ClientError as exc:
        if _code(exc) in _NOT_FOUND_CODES:
            raise _ReviewRefused("assistant.lambda_revision_review_unverified",
                                 f"function {name} no longer exists") from exc
        raise
    cfg = got.get("Configuration") or {}
    tags = got.get("Tags") or {}
    current = _validated(cfg, "FunctionConfiguration", "the current $LATEST configuration")
    if not isinstance(tags, dict) or any(not isinstance(k, str) or not isinstance(v, str)
                                         for k, v in tags.items()):
        raise _ReviewRefused("assistant.lambda_revision_review_unverified",
                             "the function's tags are malformed — refusing to compare them")
    left = {k: v for k, v in response.items() if k not in _FUNCTION_LIFECYCLE_MEMBERS}
    right = {k: v for k, v in current.items() if k not in _FUNCTION_LIFECYCLE_MEMBERS}
    bad = _members_differ(left, right)
    if cfg.get("State") != "Active":
        bad.append("State")
    if cfg.get("LastUpdateStatus") != "Successful":
        bad.append("LastUpdateStatus")
    if cfg.get("RevisionId") != expected_current or expected_current == expected_created \
            or cfg.get("RevisionId") == expected_created:
        bad.append("RevisionId")
    for member in _REQUEST_APPROVED_MEMBERS:
        if cfg.get(member) != request.get(member):
            bad.append(member)
    if cfg.get("CodeSha256") != request.get("CodeSha256") \
            or cfg.get("FunctionArn") != (res.get("result") or {}).get("function_arn"):
        bad.append("CodeSha256/FunctionArn")
    if tags != (request.get("Tags") or {}):
        bad.append("Tags")
    if bad:
        raise _ReviewRefused(
            "assistant.lambda_revision_review_unverified",
            f"$LATEST is not the verified create answer after its first initialization "
            f"(differs on {sorted(set(bad))}) — not an initialization transition",
            fields=sorted(set(bad)))
    inventory = _read_function_inventory(lam, name)
    if inventory["versions"] != ["$LATEST"] or inventory["aliases"]:
        raise _ReviewRefused("assistant.lambda_revision_review_unverified",
                             f"the function carries versions {inventory['versions']} / aliases "
                             f"{inventory['aliases']} — this operation published nothing; not "
                             "an initialization state")
    _policy_absent(lam, name)
    if inventory["reserved_concurrency"] is not None:
        raise _ReviewRefused("assistant.lambda_revision_review_unverified",
                             f"reserved concurrency {inventory['reserved_concurrency']} is set — "
                             "this operation wrote none")
    return {"configuration": cfg, "tags": tags, **inventory, "resource_policy": "absent"}


def _dependency_drift(iam: Any, logs: Any, op_id: str, resources: list[dict[str, Any]],
                      ) -> tuple[list[str], dict[str, Any]]:
    """Compare the role and the log group the function depends on with what this
    operation recorded when it created them — identity AND configuration (RoleId / ARN /
    trust / inline policy / tags; creationTime / ARN / retention / tags). A ``ready`` ledger
    status blesses nothing: the worker skips ready dependencies, so drift is read here.
    Returns the differing fields and the current snapshot."""
    bad: list[str] = []
    role = _resource(resources, "lambda_role")
    rr = role.get("result") or {}
    if role.get("status") != "ready" or not rr.get("role_id") or not rr.get("role_arn") \
            or not rr.get("trust_document") or not rr.get("policy_document"):
        raise _ReviewRefused("assistant.lambda_revision_review_not_applicable",
                             "the Lambda role is not a ready, fully recorded resource of this "
                             "operation")
    role_name = _role_name(rr["role_arn"])
    try:
        current = iam.get_role(RoleName=role_name)["Role"]
        inline = _policy_document(iam.get_role_policy(
            RoleName=role_name, PolicyName=LOGS_POLICY_NAME).get("PolicyDocument"))
    except ClientError as exc:
        raise _ReviewRefused("assistant.lambda_revision_review_unverified",
                             f"the Lambda role cannot be read: {_safe_error(exc)}") from exc
    if current.get("RoleId") != rr["role_id"] or current.get("Arn") != rr["role_arn"]:
        bad.append("role.identity")
    if _policy_document(current.get("AssumeRolePolicyDocument")) != rr["trust_document"]:
        bad.append("role.trust")
    if inline != rr["policy_document"]:
        bad.append("role.inline_policy")
    role_tags = _role_tags(current)
    if role_tags.get(TAG_OPERATION) != op_id or role_tags.get(TAG_MANAGED) != "true":
        bad.append("role.tags")
    group = _resource(resources, "log_group")
    gr = group.get("result") or {}
    if group.get("status") != "ready" or not gr.get("creation_time") or not gr.get("arn") \
            or not gr.get("retention_days"):
        raise _ReviewRefused("assistant.lambda_revision_review_not_applicable",
                             "the log group is not a ready, fully recorded resource of this "
                             "operation")
    found = [g for g in (logs.describe_log_groups(logGroupNamePrefix=group["name"])
                         .get("logGroups") or []) if g.get("logGroupName") == group["name"]]
    mine = found[0] if len(found) == 1 else {}
    if mine.get("creationTime") != gr["creation_time"] or mine.get("arn") != gr["arn"]:
        bad.append("log_group.identity")
    if mine.get("retentionInDays") != gr["retention_days"]:
        bad.append("log_group.retention")
    try:
        group_tags = logs.list_tags_for_resource(
            resourceArn=str(mine.get("arn") or "").rstrip("*").rstrip(":")).get("tags") or {}
    except ClientError:
        group_tags = {}
    if group_tags.get(TAG_OPERATION) != op_id or group_tags.get(TAG_MANAGED) != "true":
        bad.append("log_group.tags")
    trust = _policy_document(current.get("AssumeRolePolicyDocument"))
    snapshot = {"role": {"role_id": current.get("RoleId"), "role_arn": current.get("Arn"),
                         "trust_document": trust, "policy_document": inline, "tags": role_tags},
                "log_group": {"arn": mine.get("arn"), "creation_time": mine.get("creationTime"),
                              "retention_days": mine.get("retentionInDays"), "tags": group_tags}}
    return bad, snapshot


def _verify_chain_provenance(clients: ClientFactory, workspace: WorkspaceContext,
                             op_id: str, resources: list[dict[str, Any]]) -> dict[str, Any]:
    bad, snapshot = _dependency_drift(clients(workspace, "iam"), clients(workspace, "logs"),
                                      op_id, resources)
    if bad:
        raise _ReviewRefused("assistant.lambda_revision_review_unverified",
                             f"the function's dependencies differ from what this operation "
                             f"recorded ({bad}) — review required", fields=bad)
    return snapshot


def _bind_operation(db: Session, op: EvaluationAssetOperation, plan_hash: str,
                    principal: str | None) -> AssistantEvaluationPlan:
    """Fresh, exact binding of the operation to what the reviewer names: the approved plan
    row (this id, this revision, this hash column AND the canonical hash of its current
    content), the conversation's current owner == the operation's recorded owner == the
    caller, the approver still authorized and the workspace still the pinned identity.
    Every read here is fresh (the caller expires the session first)."""
    if op.plan_hash != plan_hash:
        raise _ReviewRefused("assistant.evaluation_plan_stale",
                             "plan_hash does not name this operation's approved plan")
    plan_row = db.get(AssistantEvaluationPlan, op.plan_id)
    if plan_row is None or plan_row.status != "approved" \
            or plan_row.revision != op.plan_revision \
            or plan_row.conversation_id != op.conversation_id \
            or plan_row.content_hash != plan_hash \
            or plan_contract.canonical_hash(plan_row.content or {}) != plan_hash:
        raise _ReviewRefused("assistant.evaluation_plan_stale",
                             "the approved plan revision no longer matches the operation "
                             "(row, revision, hash or content changed)")
    owner = db.execute(select(AssistantConversation.owner_principal)
                       .where(AssistantConversation.id == op.conversation_id,
                              AssistantConversation.workspace_id == op.workspace_id)).scalar()
    if owner is None or owner != op.owner_principal \
            or (principal is not None and owner != principal):
        raise NotFoundError("assistant.operation_not_found", "operation not found")
    stop = approver_authorized(db, op)
    if stop:
        raise AppError("assistant.evaluation_assets_stopped", stop, status_code=409)
    if not op.pinned:
        raise AppError("assistant.evaluation_assets_stopped",
                       "operation predates workspace identity pinning — review required; "
                       "prepare a new plan revision instead", status_code=409)
    drift = pinned_drift(op.pinned or {}, db.get(Workspace, op.workspace_id))
    if drift:
        raise AppError("assistant.evaluation_assets_stopped",
                       f"workspace identity changed since approval ({', '.join(drift)})",
                       status_code=409)
    return plan_row


def _review_target(op: EvaluationAssetOperation, plan_hash: str,
                   expected_created: str) -> dict[str, Any]:
    """Ledger-only eligibility: the operation is partial/failed on exactly this plan
    hash, pinned, its Lambda intent is an accepted, owned CreateFunction blocked SOLELY
    by the first-initialization RevisionId drift, and nothing else unrelated conflicts."""
    if op.plan_hash != plan_hash:
        raise _ReviewRefused("assistant.evaluation_plan_stale",
                             "plan_hash does not name this operation's approved plan")
    if op.status not in ("partial", "failed"):
        raise _ReviewRefused("assistant.lambda_revision_review_not_applicable",
                             f"the operation is {op.status}; only a partial / failed operation "
                             "can be reviewed")
    if not op.pinned:
        raise _ReviewRefused("assistant.evaluation_assets_stopped",
                             "operation predates workspace identity pinning — review required; "
                             "prepare a new plan revision instead")
    res = next((r for r in op.resources or [] if r.get("kind") == "lambda_function"), None)
    if res is None:
        raise _ReviewRefused("assistant.lambda_revision_review_not_applicable",
                             "the operation has no Lambda function intent")
    stored = res.get("result") or {}
    initial = stored.get("initial_revision_id") or (stored.get("created_identity") or {}).get(
        "RevisionId")
    if res.get("status") != "conflict" or not initial_revision_conflict_eligible(res):
        raise _ReviewRefused(
            "assistant.lambda_revision_review_not_applicable",
            f"the Lambda function is {res.get('status')} and not blocked solely by the "
            "first-initialization RevisionId change (a lost create, a published version or a "
            "re-pinned baseline is not reviewable this way)")
    marker = res.get("review") or {}
    if marker.get("kind") != "initial_revision_changed" \
            and "before publish on ['RevisionId']" not in str(res.get("error") or ""):
        raise _ReviewRefused("assistant.lambda_revision_review_not_applicable",
                             "the recorded conflict is not the pre-publish RevisionId drift")
    if initial != expected_created:
        raise _ReviewRefused("assistant.lambda_revision_review_stale",
                             "expected_created_revision_id is not the RevisionId CreateFunction "
                             "answered this operation with")
    allowed_blocked = {r["key"] for r in op.resources or []
                       if r.get("kind") in ("lambda_permission", "role_grant")
                       or r.get("definition") == "code"}
    for r in op.resources or []:
        if r is res or r.get("status") in ("ready", "skipped", "pending"):
            continue
        if r.get("status") == "blocked" and r["key"] in allowed_blocked:
            continue
        raise _ReviewRefused(
            "assistant.lambda_revision_review_not_applicable",
            f"{r['key']} is {r.get('status')} — an unrelated open outcome must be resolved "
            "first; this review lifts only the Lambda initialization conflict")
    return res


def _same_review(entry: dict[str, Any], *, event_id: str, expected_created: str,
                 expected_current: str, plan_hash: str) -> bool:
    return (entry.get("event_id") == event_id
            and entry.get("expected_created_revision_id") == expected_created
            and entry.get("expected_current_revision_id") == expected_current
            and entry.get("plan_hash") == plan_hash)


def review_lambda_initial_revision(
    db: Session, op: EvaluationAssetOperation, *, plan_hash: str, expected_created: str,
    expected_current: str, event_id: str, reason: str, reviewer: str,
    reviewer_user_id: str | None, recheck: Recheck | None = None,
    clients: ClientFactory = _default_clients,
) -> RevisionReview:
    """Administrator + owner reviewed recovery of exactly one conflict: the RevisionId
    CreateFunction answered with moved during the function's first initialization
    (Pending → Active) and the worker refused to publish. Nothing is inferred: the
    nominated CloudTrail event is read server-side and must be THIS operation's
    successful CreateFunction; the settled ``$LATEST`` must equal that answer up to the
    documented lifecycle transition and the nominated current RevisionId; role / log
    provenance, empty version / alias / policy / concurrency inventories are required.
    The review is an append-only audit entry (the original create evidence is never
    overwritten), the baseline moves to the reviewed RevisionId, the Lambda conflict
    and its blocked dependents are re-queued and the ordinary worker resumes — its
    PublishVersion still carries both preconditions, so a later change fails there.
    The exact same request is idempotent (returns the recorded review, no cloud read);
    a different one after a review is refused. No cloud write happens here."""
    reason = (reason or "").strip()
    if not reason:
        raise _ReviewRefused("assistant.lambda_revision_review_reason_required",
                             "a non-empty review reason is required", status_code=422)
    if expected_created == expected_current:
        raise _ReviewRefused("assistant.lambda_revision_review_stale",
                             "expected_created_revision_id equals expected_current_revision_id — "
                             "there is no revision transition to review", status_code=422)
    # every binding is read fresh, before anything else: the operation, its plan row
    # (hash column AND canonical content), the conversation owner, the approver, the
    # pinned workspace. The route's own reads are not trusted here.
    db.expire_all()
    fresh = db.get(EvaluationAssetOperation, op.id)
    if fresh is None:
        raise NotFoundError("assistant.operation_not_found", "operation not found")
    op = fresh
    _bind_operation(db, op, plan_hash, None)
    res_now = next((r for r in op.resources or [] if r.get("kind") == "lambda_function"), None)
    prior = [e for e in ((res_now or {}).get("reviews") or [])
             if e.get("kind") == "initial_revision_changed"]
    if prior:
        matched = [e for e in prior if _same_review(
            e, event_id=event_id, expected_created=expected_created,
            expected_current=expected_current, plan_hash=plan_hash)]
        if matched:
            # the exact same request after the same bindings: the recorded review, no
            # cloud read, no write, no second launch
            return RevisionReview(operation=op, review=matched[-1], started=False)
        raise _ReviewRefused("assistant.lambda_revision_review_stale",
                             "this Lambda initialization was already reviewed with a different "
                             "event / revision; a second, different review is refused")
    if op.attempts >= MAX_ATTEMPTS:
        raise AppError("assistant.evaluation_assets_exhausted",
                       f"the operation reached its {MAX_ATTEMPTS} attempts; clean up the owned "
                       "resources and prepare a new plan revision", status_code=409)
    if live_worker(op.id) is not None or not _flock_free(op.id):
        raise AppError("assistant.evaluation_assets_running",
                       "the operation is still running", status_code=409)
    res = _review_target(op, plan_hash, expected_created)
    workspace = workspace_context(db.get(Workspace, op.workspace_id))
    resources = json.loads(json.dumps(op.resources or []))
    # positive, server-read evidence: the nominated CloudTrail record IS our create; the
    # dependencies still are what we recorded; $LATEST is that answer, settled
    event = _lookup_create_event(clients(workspace, "cloudtrail"), event_id)
    response = _verify_create_event(event, op, res, expected_created)
    dependencies = _verify_chain_provenance(clients, workspace, op.id, resources)
    baseline = _verify_settled_function(clients(workspace, "lambda"), op, res, response,
                                        expected_created, expected_current)
    baseline["dependencies"] = dependencies
    # persist under the host lock, in one conditional write whose predicates are the
    # CURRENT owner / approver / reviewer authorization, the exact plan row and the
    # pinned workspace: a change committed by another session up to this statement
    # makes it a no-op — the caller is re-resolved from the database inside the lock
    with _flock(op.id) as held:
        if not held:
            raise AppError("assistant.evaluation_assets_running",
                           "the operation is still running", status_code=409)
        db.rollback()
        db.expire_all()  # BEFORE the recheck: its reads must not hit cached rows
        fresh_identity = recheck(db) if recheck else None
        principal = principal_of(fresh_identity) if fresh_identity else None
        if fresh_identity is not None and not fresh_identity.is_admin:
            raise AppError("auth.admin_required", "administrator role required", status_code=403)
        db.expire_all()
        op = db.get(EvaluationAssetOperation, op.id)
        if op is None:
            raise NotFoundError("assistant.operation_not_found", "operation not found")
        if op.worker_token is not None:
            raise AppError("assistant.evaluation_assets_running",
                           "the operation is still running", status_code=409)
        plan_row = _bind_operation(db, op, plan_hash, principal)
        target = _review_target(op, plan_hash, expected_created)
        if (target.get("result") or {}).get("revision_id") != expected_created \
                or target.get("reviews"):
            raise _ReviewRefused("assistant.lambda_revision_review_stale",
                                 "the operation changed while the review was verified")
        resources = json.loads(json.dumps(op.resources or []))
        fn = _resource(resources, "lambda_function")
        stored = fn["result"]
        cfg = baseline["configuration"]
        now = _now().isoformat()
        review = {
            "kind": "initial_revision_changed",
            "id": secrets.token_hex(8),
            "at": now,
            "reviewer": fresh_identity.username if fresh_identity else reviewer,
            "reviewer_user_id": fresh_identity.user_id if fresh_identity else reviewer_user_id,
            "reason": reason[:1000],
            "operation_id": op.id, "plan_id": op.plan_id, "plan_revision": op.plan_revision,
            "plan_hash": op.plan_hash, "owner_principal": op.owner_principal,
            "event_id": event_id, "event_time": event.get("eventTime"),
            "event_name": event.get("eventName"), "request_id": event.get("requestID"),
            "expected_created_revision_id": expected_created,
            "expected_current_revision_id": expected_current,
            "verified": {"RevisionId": cfg.get("RevisionId"), "CodeSha256": cfg.get("CodeSha256"),
                         "LastModified": cfg.get("LastModified"), "State": cfg.get("State"),
                         "LastUpdateStatus": cfg.get("LastUpdateStatus"),
                         "create_state": response.get("State"),
                         "create_state_reason_code": response.get("StateReasonCode")},
            "old": {"status": fn.get("status"), "error": fn.get("error"),
                    "revision_id": stored.get("revision_id"),
                    "review": fn.get("review")},
            "new": {"status": "pending", "revision_id": expected_current,
                    "settled_revision_id": expected_current},
        }
        # append-only: the create answer, identity and initial RevisionId stay untouched;
        # the strict reviewed baseline travels with the intent for the worker to re-verify
        fn.setdefault("reviews", []).append(review)
        stored.setdefault("revision_history", []).append(
            {"at": now, "from": stored.get("revision_id"), "to": expected_current,
             "reason": "initial_activation_reviewed", "review_id": review["id"]})
        stored["revision_id"] = expected_current
        stored["settled_revision_id"] = expected_current
        stored["reviewed_baseline"] = {**baseline, "review_id": review["id"], "at": now}
        fn["status"], fn["error"] = "pending", None
        if fn.get("review"):
            fn["review"] = {**fn["review"], "resolved_by": review["id"]}
        released = [fn["key"]]
        for r in resources:
            if r.get("status") == "blocked" and (
                    r.get("kind") in ("lambda_permission", "role_grant")
                    or r.get("definition") == "code"):
                r["status"], r["error"] = "pending", None
                released.append(r["key"])
        line = json.dumps({"at": now, "event": "lambda_function:reviewed",
                           "review_id": review["id"], "released": released},
                          ensure_ascii=False)
        owner_ok = select(AssistantConversation.id).where(
            AssistantConversation.id == op.conversation_id,
            AssistantConversation.workspace_id == op.workspace_id,
            AssistantConversation.owner_principal == op.owner_principal,
            *([AssistantConversation.owner_principal == principal] if principal else []),
        ).exists()
        # every value the review relied on is a predicate of this one statement — the
        # exact JSON of the validated plan content, the operation's pinned identity and
        # intents, and the workspace resources included — so a change committed by another
        # session up to the write itself makes it a no-op
        plan_ok = select(AssistantEvaluationPlan.id).where(
            AssistantEvaluationPlan.id == plan_row.id,
            AssistantEvaluationPlan.conversation_id == op.conversation_id,
            AssistantEvaluationPlan.revision == op.plan_revision,
            AssistantEvaluationPlan.status == "approved",
            AssistantEvaluationPlan.content_hash == plan_hash,
            AssistantEvaluationPlan.content == plan_row.content,
        ).exists()
        pinned = op.pinned or {}
        ws_row = db.get(Workspace, op.workspace_id)
        workspace_ok = select(Workspace.id).where(
            Workspace.id == op.workspace_id,
            Workspace.account_id == pinned.get("account_id"),
            Workspace.region == pinned.get("region"),
            Workspace.role_arn.is_(None) if pinned.get("role_arn") is None
            else Workspace.role_arn == pinned.get("role_arn"),
            Workspace.external_id.is_(None) if pinned.get("external_id") is None
            else Workspace.external_id == pinned.get("external_id"),
            Workspace.resources == (ws_row.resources if ws_row is not None else {}),
        ).exists()
        conditions = [
            EvaluationAssetOperation.id == op.id,
            EvaluationAssetOperation.status == op.status,
            EvaluationAssetOperation.status.in_(("partial", "failed")),
            EvaluationAssetOperation.worker_token.is_(None),
            EvaluationAssetOperation.attempts == op.attempts,
            EvaluationAssetOperation.plan_id == plan_row.id,
            EvaluationAssetOperation.plan_revision == op.plan_revision,
            EvaluationAssetOperation.plan_hash == plan_hash,
            EvaluationAssetOperation.owner_principal == op.owner_principal,
            EvaluationAssetOperation.approver_user_id.is_(None) if op.approver_user_id is None
            else EvaluationAssetOperation.approver_user_id == op.approver_user_id,
            EvaluationAssetOperation.pinned == op.pinned,
            EvaluationAssetOperation.resources == op.resources,
            owner_ok, plan_ok, workspace_ok,
        ]
        for user_id in {op.approver_user_id, review["reviewer_user_id"]} - {None}:
            conditions.append(select(User.id).where(
                User.id == user_id, User.role == ROLE_ADMIN, User.status == "active",
                (User.expires_at.is_(None)) | (User.expires_at > _now()),
            ).exists())
        rows = db.execute(
            update(EvaluationAssetOperation)
            .where(*conditions)
            .values(resources=resources, status="queued", error=None,
                    log=(op.log or "") + line + "\n")
        ).rowcount
        if rows != 1:
            db.rollback()
            raise AppError("assistant.evaluation_assets_stopped",
                           "the operation's owner, plan, approver, reviewer or workspace changed "
                           "while the review was recorded — nothing written", status_code=409)
        db.commit()
        db.expire_all()
        op = db.get(EvaluationAssetOperation, op.id)
    # the worker launches only after the review is committed (a crash here leaves a
    # ``queued`` operation that startup resume / an explicit retry picks up)
    started = start_async(op.id, clients=clients) is not None \
        if clients is not _default_clients else start_async(op.id) is not None
    return RevisionReview(operation=op, review=review, started=started)


# ---------------------------------------------------------------------------
# cleanup of owned cloud artifacts (never the dataset, never foreign resources)
# ---------------------------------------------------------------------------


def cleanup_operation(
    db: Session, op: EvaluationAssetOperation, workspace: WorkspaceContext, *,
    clients: ClientFactory = _default_clients, sleeper: Callable[[float], None] = time.sleep,
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
      only after the identity snapshot recorded when its create/readback succeeded still
      matches exactly (RoleId / ARN / trust / inline policy; log-group creationTime / ARN /
      retention; the published version AND ``$LATEST`` incl. RevisionId, the version set
      and the alias set). A whole-function delete is confirmed by a bounded unqualified
      GetFunction NotFound before the log group and role are touched;
    * a create whose response was lost before the service-issued identity was recorded
      stays ``unknown`` while a resource with our name exists (a nonce / tag / digest is
      copyable content, never ownership) — dependents retained, nothing deleted; a lost
      CreateEvaluator stays ``unknown`` even when ListEvaluators does not show the name
      (visibility cannot prove the create never happened; a worker retry replays the
      idempotency token, cleanup never creates);
    * the local Dataset stays (a member asset); foreign resources are never touched;
      ``cleaned`` is recorded only when nothing owned remains.

    Limitation: DeleteFunction / DeleteLogGroup / DeleteRole / DeleteEvaluator carry no
    precondition token (installed models), so the check→delete window is one API call
    wide against an external administrator; it cannot be closed atomically.
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
            return _cleanup(db, fence, workspace, clients, sleeper)
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
        except AppError:
            raise
        except Exception as exc:  # noqa: BLE001 — never leave the row 'cleaning' with a token
            logger.exception("evaluation assets %s cleanup failed", op.id)
            db.rollback()
            db.execute(update(EvaluationAssetOperation)
                       .where(EvaluationAssetOperation.id == op.id,
                              EvaluationAssetOperation.worker_token == token)
                       .values(status="partial", error=f"cleanup failed: {_safe_error(exc)}",
                               worker_token=None))
            db.commit()
            raise AppError("assistant.evaluation_assets_cleanup_failed",
                           f"cleanup failed and was recorded as partial: {_safe_error(exc)}",
                           status_code=502) from exc


def _attempted(r: dict[str, Any]) -> bool:
    """A create call may have gone out: an intent/request/result was persisted. This is
    decided by the durable dispatch record, never by the display status — ``pending`` or
    ``blocked`` after a crash still carries the intent of the create that went out."""
    return bool(r.get("intent") or r.get("request") or r.get("result") or r.get("owned")
                or r.get("create_history"))


UNCERTAIN_OUTCOMES = ("dispatched", "lost", "legacy-uncertain")


def _uncertain_create(r: dict[str, Any], *, exclude_last: bool = False) -> bool:
    """An evaluator create of this operation whose outcome is unknown (crash before the
    response, lost response, 5xx) and that no token replay has since resolved into an
    id. Older records without a history but with a request are uncertain unless they
    recorded a definite rejection."""
    if (r.get("result") or {}).get("evaluator_id"):
        return False
    history = list(r.get("create_history") or [])
    if not history:  # a record from before dispatch histories existed
        return bool(r.get("request")) and r.get("create_outcome") != "rejected"
    if exclude_last:
        history = history[:-1]
    return any(e.get("outcome") in UNCERTAIN_OUTCOMES for e in history)


def _cleanup(db: Session, fence: _Fence, workspace: WorkspaceContext,
             clients: ClientFactory, sleeper: Callable[[float], None]) -> EvaluationAssetOperation:
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

    def gone(exc: BaseException) -> bool:
        return isinstance(exc, ClientError) and _code(exc) in _NOT_FOUND_CODES

    def unresolved(r: dict[str, Any]) -> bool:
        if r.get("kind") in ("dataset", "existing"):
            return False  # the local Dataset stays by design; references own nothing
        if r.get("status") in ("deleted", "skipped"):
            return False
        if r.get("kind") == "evaluator" and _uncertain_create(r):
            return True  # a dispatched create with no outcome: an effect may exist
        if r.get("status") == "conflict" and not r.get("owned"):
            return False  # a foreign pre-existing resource: never ours, nothing to delete
        # ``pending`` / ``blocked`` count exactly when a create was dispatched
        return _attempted(r) or r.get("status") in ("unknown", "delete_pending", "retained")

    control = clients(workspace, "bedrock-agentcore-control")
    # 1. evaluators — reconcile unknown creates, verify identity, delete, CONFIRM gone
    for r in [x for x in resources if x["kind"] == "evaluator"]:
        if r.get("status") == "deleted":
            continue
        result = r.get("result") or {}
        request = r.get("request") or {}
        if not result.get("evaluator_id"):
            if not _attempted(r):
                if r.get("status") in ("pending", "blocked"):
                    continue  # never dispatched: nothing can exist
                mark(r, "deleted", "no create was attempted")
                continue
            if not _uncertain_create(r):
                # every recorded dispatch was answered with a definite 4xx rejection or a
                # creation-time collision: nothing of ours was created
                if r.get("status") == "conflict" and not r.get("owned"):
                    continue  # foreign collision established at creation: never ours
                mark(r, "deleted", "the service rejected every create (4xx) — nothing created")
                checkpoint(f"{r['key']}:reconciled")
                continue
            # a CreateEvaluator may have succeeded with a lost response. The token cannot
            # be replayed here without creating, and a listing cannot prove the create
            # never happened (visibility is eventual) — so the outcome stays UNKNOWN either
            # way; the name lookup only adds the candidate id for the reviewer.
            hint = ""
            try:
                names = {e.get("evaluatorName"): e for e in _list_evaluators(control)}
                found = names.get(request.get("evaluatorName"))
                hint = (f"; an evaluator with this name is listed: {found.get('evaluatorId')}"
                        if found else "; no evaluator with this name is listed right now, "
                        "which does not prove the create never succeeded")
            except ClientError as exc:
                hint = f"; could not list evaluators: {_safe_error(exc)}"
            except Exception as exc:  # noqa: BLE001 — transport: still unknown
                hint = f"; could not list evaluators: {_safe_error(exc)}"
            mark(r, "unknown", "a CreateEvaluator of this operation has no recorded outcome "
                               f"(name {request.get('evaluatorName')}, clientToken "
                               f"{request.get('clientToken')}); this operation cannot prove "
                               f"whether it created one{hint} — retry the operation (the "
                               "idempotency token recovers or creates it under our "
                               "ownership), then clean up again")
            checkpoint(f"{r['key']}:unknown")
            continue
        if r.get("status") == "conflict" and not r.get("owned"):
            continue  # foreign collision: never ours
        eid = result["evaluator_id"]
        try:
            detail = control.get_evaluator(evaluatorId=eid)
        except Exception as exc:  # noqa: BLE001 — incl. transport: recorded, retryable
            if gone(exc):
                mark(r, "deleted", "already gone")
                checkpoint(f"{r['key']}:gone")
                continue
            mark(r, "delete_failed", _safe_error(exc))
            checkpoint(f"{r['key']}:readback_failed")
            continue
        if detail.get("status") != "DELETING" and (
                detail.get("evaluatorId") != eid or detail.get("evaluatorName") != request.get(
                "evaluatorName") or detail.get("evaluatorConfig") != request.get(
                "evaluatorConfig") or detail.get("level") != request.get("level")
                or not result.get("evaluator_arn")
                or detail.get("evaluatorArn") != result.get("evaluator_arn")):
            mark(r, "conflict", "owned evaluator was changed after creation (id / name / level "
                                "/ config / ARN differ from the recorded identity) — review it "
                                "before deleting; not removed")
            checkpoint(f"{r['key']}:drift")
            continue
        if detail.get("status") != "DELETING":
            fence.guard(db)
            try:
                control.delete_evaluator(evaluatorId=eid)
            except Exception as exc:  # noqa: BLE001 — a lost delete response is recorded
                if not gone(exc):
                    mark(r, "delete_failed" if isinstance(exc, ClientError) else "delete_pending",
                         f"DeleteEvaluator did not answer cleanly ({_safe_error(exc)}) — the "
                         "evaluator may or may not be deleting; retry later")
                    checkpoint(f"{r['key']}:cleanup")
                    continue
        # DeleteEvaluator is accepted asynchronously: only a NotFound readback proves gone
        state, note = "delete_pending", ("DeleteEvaluator accepted but the evaluator is still "
                                         "present — retry later")
        for _ in range(READBACK_ATTEMPTS):
            try:
                control.get_evaluator(evaluatorId=eid)
            except Exception as exc:  # noqa: BLE001
                if gone(exc):
                    state = "deleted"
                else:
                    note = f"readback after DeleteEvaluator failed ({_safe_error(exc)}) — retry"
                break
            sleeper(READBACK_DELAY_S)
        mark(r, state, None if state == "deleted" else note)
        checkpoint(f"{r['key']}:cleanup")
    evaluators_left = [r["key"] for r in resources if r["kind"] == "evaluator" and unresolved(r)]
    # 2. the code chain — only once every evaluator is confirmed gone (dependency DAG)
    chain = [by_key[k] for k in ("role_grant", "lambda_permission", "lambda_function",
                                 "log_group", "lambda_role") if k in by_key]
    if evaluators_left:
        for r in chain:
            if unresolved(r):
                mark(r, "retained", "kept: evaluator(s) still present or unresolved: "
                                    + ", ".join(evaluators_left))
        checkpoint("chain:retained")
    else:
        fn = by_key.get("lambda_function")
        fn_result = (fn or {}).get("result") or {}
        grant = by_key.get("role_grant")
        if grant and unresolved(grant):
            iam = clients(workspace, "iam")
            g = grant.get("result") or {}
            try:
                if not g.get("policy_name"):
                    mark(grant, "deleted", "no grant was written")
                else:
                    current = iam.get_role(RoleName=_role_name(g["role_arn"]))["Role"]
                    try:
                        back = _policy_document(iam.get_role_policy(
                            RoleName=_role_name(g["role_arn"]),
                            PolicyName=g["policy_name"]).get("PolicyDocument"))
                    except ClientError as exc:
                        if not gone(exc):
                            raise
                        back = None
                    if back is None:
                        mark(grant, "deleted", "grant not present")
                    elif current.get("RoleId") != g.get("role_id") or current.get(
                            "Arn") != g.get("role_arn"):
                        mark(grant, "conflict",
                             "execution role was replaced — grant left untouched")
                    elif back != g.get("policy_document"):
                        mark(grant, "conflict", "grant document differs from ours — left untouched")
                    else:
                        fence.guard(db)
                        iam.delete_role_policy(RoleName=_role_name(g["role_arn"]),
                                               PolicyName=g["policy_name"])
                        mark(grant, "deleted")
            except (_LeaseLost, _Stop):
                raise
            except Exception as exc:  # noqa: BLE001 — incl. a lost delete response
                mark(grant, "deleted" if gone(exc) else "delete_failed",
                     None if gone(exc) else _safe_error(exc))
            checkpoint("role_grant:cleanup")
        if fn and unresolved(fn):
            lam = clients(workspace, "lambda")
            try:
                _cleanup_function(lam, fn, fn_result, lambda: fence.guard(db), mark, sleeper)
                if fn.get("status") == "deleted":
                    perm = by_key.get("lambda_permission")
                    if perm and perm.get("status") not in ("pending", "blocked"):
                        mark(perm, "deleted", "deleted with the function")
            except (_LeaseLost, _Stop):
                raise
            except Exception as exc:  # noqa: BLE001 — incl. a lost delete response
                # only the helper's unqualified GetFunction NotFound proves absence; any
                # other failure (ancillary NotFound included) keeps the dependencies
                mark(fn, "delete_failed", _safe_error(exc))
            checkpoint("lambda_function:cleanup")
        fn_gone = fn is None or fn.get("status") == "deleted" or (
            fn.get("status") in ("pending", "blocked") and not _attempted(fn))
        lg = by_key.get("log_group")
        if lg and unresolved(lg):
            logs = clients(workspace, "logs")
            try:
                if not fn_gone:
                    mark(lg, "retained", "kept: the function still exists")
                else:
                    groups = logs.describe_log_groups(logGroupNamePrefix=lg["name"]).get(
                        "logGroups") or []
                    mine = [g for g in groups if g.get("logGroupName") == lg["name"]]
                    rec = lg.get("result") or {}
                    if not mine:
                        mark(lg, "deleted", "already gone")
                    elif not rec.get("creation_time") or not rec.get("arn"):
                        # a lost CreateLogGroup response or an incomplete snapshot: the
                        # provenance tag is copyable, so nothing here proves ownership
                        mark(lg, "unknown", "a log group with our name exists but its creation "
                                            "identity was never recorded — this operation "
                                            "cannot prove it created it; review it manually")
                    elif (mine[0].get("creationTime") != rec["creation_time"]
                          or mine[0].get("arn") != rec["arn"]
                          or mine[0].get("retentionInDays") != rec.get("retention_days")):
                        mark(lg, "conflict", "log group differs from the recorded identity "
                                             "(creationTime / ARN / retention) — it was "
                                             "re-created or changed; left untouched")
                    elif not _Runner._log_group_is_ours(logs, lg["name"], lg["nonce"]):
                        mark(lg, "conflict", "log group no longer carries our provenance — "
                                             "left untouched")
                    else:
                        fence.guard(db)
                        logs.delete_log_group(logGroupName=lg["name"])
                        mark(lg, "deleted")
            except (_LeaseLost, _Stop):
                raise
            except Exception as exc:  # noqa: BLE001 — incl. a lost delete response
                mark(lg, "deleted" if gone(exc) else "delete_failed",
                     None if gone(exc) else _safe_error(exc))
            checkpoint("log_group:cleanup")
        role = by_key.get("lambda_role")
        if role and unresolved(role):
            iam = clients(workspace, "iam")
            try:
                if not fn_gone:
                    mark(role, "retained", "kept: the function still exists")
                else:
                    try:
                        current = iam.get_role(RoleName=role["name"])["Role"]
                    except ClientError as exc:
                        if not gone(exc):
                            raise
                        current = None
                    rec = role.get("result") or {}
                    if current is None:
                        mark(role, "deleted", "already gone")
                    elif not rec.get("role_id"):
                        # lost CreateRole response: the RoleId was never recorded and a nonce
                        # in the description / tags is copyable — never adopted, never deleted
                        mark(role, "unknown", "a role with our name exists but its RoleId was "
                                              "never recorded — this operation cannot prove it "
                                              "created it; review it manually")
                    elif (current.get("RoleId") != rec["role_id"]
                          or current.get("Arn") != rec.get("role_arn")):
                        mark(role, "conflict", "RoleId / ARN differ from the recorded identity — "
                                               "not deleting a role we did not create")
                    elif not rec.get("trust_document") or not rec.get("policy_document"):
                        mark(role, "conflict", "the role's trust / policy snapshot was never "
                                               "recorded — review required")
                    elif _policy_document(current.get("AssumeRolePolicyDocument")) != rec[
                            "trust_document"]:
                        mark(role, "conflict", "the role's trust policy differs from the one "
                                               "this operation wrote — left untouched")
                    else:
                        try:
                            back = _policy_document(iam.get_role_policy(
                                RoleName=role["name"], PolicyName=LOGS_POLICY_NAME
                            ).get("PolicyDocument"))
                        except ClientError as exc:
                            if not gone(exc):
                                raise
                            back = None
                        if back is not None and back != rec["policy_document"]:
                            mark(role, "conflict", "the role's inline policy differs from the "
                                                   "one this operation wrote — left untouched")
                        else:
                            fence.guard(db)
                            if back is not None:
                                iam.delete_role_policy(RoleName=role["name"],
                                                       PolicyName=LOGS_POLICY_NAME)
                            fence.guard(db)  # a fresh fence for the SECOND mutation too
                            iam.delete_role(RoleName=role["name"])
                            mark(role, "deleted")
            except (_LeaseLost, _Stop):
                raise
            except Exception as exc:  # noqa: BLE001 — incl. a lost delete response
                mark(role, "deleted" if gone(exc) else "delete_failed",
                     None if gone(exc) else _safe_error(exc))
            checkpoint("lambda_role:cleanup")
    remaining = [r["key"] for r in resources if unresolved(r)]
    final = "cleaned" if not remaining else "partial"
    error = None if not remaining else "cleanup incomplete: " + ", ".join(remaining)
    fence.save(db, op, resources, "cleanup:finished", status=final, error=error,
               worker_token=None)
    db.expire_all()
    return db.get(EvaluationAssetOperation, op.id)


def _cleanup_function(lam: Any, fn: dict[str, Any], fn_result: dict[str, Any],
                      guard: Callable[[], Any], mark: Callable[..., None],
                      sleeper: Callable[[float], None]) -> None:
    """Whole-function cleanup with exact identity: the recorded published version AND the
    unqualified ``$LATEST`` (incl. RevisionId), the version set and the alias set must
    equal the settled snapshot; a missing version is NOT a missing function; after
    DeleteFunction only a bounded unqualified GetFunction NotFound marks ``deleted``."""
    name = fn["name"]

    def get(qualifier: str | None) -> dict[str, Any] | None:
        try:
            kwargs = {"Qualifier": qualifier} if qualifier else {}
            return lam.get_function(FunctionName=name, **kwargs).get("Configuration") or {}
        except ClientError as exc:
            if _code(exc) in _NOT_FOUND_CODES:
                return None
            raise

    if not fn_result.get("function_arn"):
        # a CreateFunction may have succeeded with a lost response: the package (and its
        # nonce) is downloadable content, so an equal CodeSha256 is not ownership
        if get(None) is None:
            mark(fn, "deleted", "no function with our name exists — nothing to delete")
        else:
            mark(fn, "unknown", "a function with our name exists but its service identity was "
                                "never recorded — this operation cannot prove it created it; "
                                "review it manually")
        return
    snapshot = fn_result.get("readback") or {}
    latest_snapshot = fn_result.get("latest_readback") or {}
    version = fn_result.get("version")
    incomplete = [k for k in FUNCTION_IDENTITY_FIELDS
                  if snapshot.get(k) in (None, "") or latest_snapshot.get(k) in (None, "")]
    if not version or not isinstance(fn_result.get("versions"), list) \
            or not fn_result.get("versions") or not isinstance(fn_result.get("aliases"), list) \
            or snapshot.get("ReservedConcurrentExecutions") is None:
        incomplete.append("inventory")
    if incomplete:
        mark(fn, "conflict", f"the function's identity snapshot is incomplete ({incomplete}) — "
                             "review required before any delete")
        return
    latest = get(None)
    if latest is None:
        mark(fn, "deleted", "already gone")
        return
    current_version = get(version)
    if current_version is None:
        mark(fn, "conflict", f"our published version {version} is missing but the function "
                             "still exists — not ours to delete as a whole; review")
        return
    drift = sorted(k for k in FUNCTION_IDENTITY_FIELDS
                   if current_version.get(k) != snapshot.get(k))
    if drift:
        mark(fn, "conflict", f"published version {version} differs from the recorded "
                             f"identity on {drift} — left untouched")
        return
    # a pending delete flips $LATEST to State=Pending; everything else must still match
    latest_drift = sorted(k for k in FUNCTION_IDENTITY_FIELDS
                          if latest.get(k) != latest_snapshot.get(k))
    if latest_drift:
        mark(fn, "conflict", f"$LATEST differs from the recorded identity on {latest_drift} "
                             "— the function was changed since we made it; left untouched")
        return
    reserved = lam.get_function_concurrency(FunctionName=name).get("ReservedConcurrentExecutions")
    if reserved != snapshot.get("ReservedConcurrentExecutions"):
        mark(fn, "conflict", f"reserved concurrency is {reserved}, recorded "
                             f"{snapshot.get('ReservedConcurrentExecutions')} — the function "
                             "was changed since we made it; left untouched")
        return
    versions = sorted(v.get("Version") for v in _function_versions(lam, name))
    aliases = sorted(a.get("Name") for a in _function_aliases(lam, name))
    if versions != sorted(fn_result["versions"]) or aliases != sorted(fn_result["aliases"]):
        mark(fn, "conflict", f"the function carries versions {versions} / aliases {aliases} "
                             f"beyond the recorded {sorted(fn_result['versions'])} / "
                             f"{sorted(fn_result['aliases'])} — left untouched")
        return
    guard()
    lam.delete_function(FunctionName=name)  # idempotent re-drive of a pending delete too
    # DeleteFunction accepted: only an unqualified NotFound proves the function is gone
    for _ in range(READBACK_ATTEMPTS):
        if get(None) is None:
            mark(fn, "deleted")
            return
        sleeper(READBACK_DELAY_S)
    mark(fn, "delete_pending", "DeleteFunction accepted but the function is still present — "
                               "log group and role retained; retry later")


def _list_evaluators(control: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    token = None
    for _ in range(50):
        kwargs = {"nextToken": token} if token else {}
        page = control.list_evaluators(**kwargs)
        out += page.get("evaluators") or page.get("evaluatorSummaries") or []
        token = page.get("nextToken")
        if not token:
            break
    return out


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
                request = r.get("request") or {}
                return {"operation_id": op.id, "conversation_id": op.conversation_id,
                        "plan_revision": op.plan_revision, "plan_key": r.get("plan_key"),
                        "definition": r.get("definition"),
                        "level": request.get("level"),
                        "evaluator_config": request.get("evaluatorConfig"),
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


def managed_rules(db: Session, owner: dict[str, Any] | None) -> list[dict[str, Any]] | None:
    """The declarative rule checks of a managed code evaluator (from its owning plan)."""
    if not owner or owner.get("definition") != "code":
        return None
    op = db.get(EvaluationAssetOperation, owner["operation_id"])
    plan = db.get(AssistantEvaluationPlan, op.plan_id) if op else None
    for e in (plan.content or {}).get("evaluators") or [] if plan else []:
        if e.get("kind") == "code" and e.get("key") == owner.get("plan_key"):
            return list((e.get("rules") or {}).get("checks") or [])
    return []


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
