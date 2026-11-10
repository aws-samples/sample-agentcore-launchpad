"""Materialization of a reviewed evaluation-assets plan (SE-047) — CN substrate (N12, CA-5).

One approved plan revision → one durable ``EvaluationAssetOperation`` → these owned
resources, created in this order, each from a persisted **intent** (unique name that
embeds the operation id, a per-intent provenance nonce or the exact request) so a
crash between a write and the ledger checkpoint, a restart or a concurrent click
resumes the SAME resources instead of creating new ones or adopting foreign ones:

1. ``dataset``          — the local Launchpad Dataset (ledger only; edits made afterwards
                          in the Evaluation console are the member's and are not
                          overwritten by a retry);
2. ``lambda_role``      — only with ``Settings.eval_code_evaluators_enabled``: a dedicated
                          Lambda execution role with ONLY log rights on its own log group
                          (one independent chain per code evaluator);
3. ``log_group``        — ``/aws/lambda/<function>`` with bounded retention;
4. ``lambda_function``  — the reviewed static handler (``lambda_runtime/handler.py``, the
                          CN code-evaluator protocol the evaluation worker speaks) +
                          single-evaluator ``rules.json`` + ``provenance.json`` as a
                          deterministic ZIP; one immutable published version whose
                          ``CodeSha256`` must equal the persisted digest;
5. ``evaluator:<key>``  — every judge / derived / code evaluator of the plan as an
                          ``eval_evaluators`` row (``evaluators_local``); a code evaluator
                          points at the published Lambda version.

The Global baseline created AgentCore evaluators, a Lambda resource policy for the
AgentCore service principal and an additive grant on the workspace execution role; none
of that exists in ``aws-cn`` (plan CA-5): evaluators are ledger rows, the evaluation
worker invokes code evaluators with its own role (``EvalWorkerCodeEvaluators``), and the
IAM-grant plumbing is deleted, not flagged. Without the setting the code chain and the
code evaluators of a plan are recorded ``skipped`` (the plan keeps the rule as a reviewed
intent) and the operation still ``succeeded`` for the dataset and the judges.

**Ownership.** Only a service-issued identity returned to THIS operation proves
ownership of a cloud resource: RoleId / ARN from CreateRole, the log group's
creationTime / ARN read right after CreateLogGroup, the FunctionArn / RevisionId
returned by CreateFunction. The provenance nonce travels inside the resource only as a
clue for a reviewer: it is copyable content, so after a lost response a resource found
under our name is **unknown** (never adopted, never deleted) and a collision established
at creation time is a **foreign collision** (``conflict``). A local evaluator row is
created in the same transaction as its checkpoint; a dispatched create whose checkpoint
never landed is reconciled by name only as ``unknown``.

**Fencing.** One host-local ``flock`` per operation (worker and cleanup) plus a database
lease token; before EVERY write the worker re-reads the lease token, re-checks that the
approver is still an active account holding ``agents.deploy`` and that the workspace row
still equals the identity pinned at approval (account, region, assume-role, execution
role ARN). A quick process restart re-acquires the free flock and resumes without
waiting for a lease timeout; a live worker is never stolen.

Nothing here deploys an agent, starts an evaluation, publishes a dataset snapshot,
enables online evaluation or invokes a model. Status reads are ledger-only. Every AWS
client comes from the workspace funnel (``WorkspaceContext.client``).
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

from botocore.exceptions import ClientError
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.assistant import evaluation_plan as plan_contract
from app.assistant import plan_notes, validation_codes
from app.assistant.principal import may_collaborate, principal_of
from app.core.config import DATA_DIR, get_settings
from app.core.db import SessionLocal
from app.core.errors import AppError, NotFoundError
from app.evaluation import evaluators_local
from app.evaluation.models import EvalDataset, EvalEvaluator
from app.models.assistant import (
    AssistantConversation,
    AssistantEvaluationPlan,
    AssistantProposal,
    EvaluationAssetOperation,
)
from app.models.ledger import User, Workspace
from app.models.ledger import _id as _new_id
from app.routers.auth import ROLE_ADMIN, Identity, granted_permissions
from app.schemas import model_catalog
from app.schemas.agent import (
    assert_model_available,
    assert_model_visible,
    hub_model_refusal_code,
)
from app.services import creators
from app.services import users as users_service
from app.services.agent_iam import CODE_EVALUATOR_FUNCTION_PREFIX
from app.services.workspace import WorkspaceContext, workspace_context

logger = logging.getLogger("launchpad.assistant.evaluation_assets")

# the console permission that may materialize / retry / clean up (parity with deploy)
PERMISSION = "agents.deploy"


def may_materialize(identity: Identity) -> bool:
    return identity.can(PERMISSION)


def permission_error() -> AppError:
    return AppError("auth.permission_required",
                    f"this action requires the {PERMISSION} permission",
                    {"permission": PERMISSION}, status_code=403)


def _authorized_user(user_id: str) -> Any:
    """EXISTS predicate: the account is active and unexpired. Evaluated inside the claim
    transaction (the permission itself is re-read by ``recheck`` / ``approver_authorized``)."""
    now = datetime.now(UTC)
    return select(User.id).where(
        User.id == user_id, User.status == "active",
        (User.expires_at.is_(None)) | (User.expires_at > now),
    ).exists()


HANDLER_PATH = Path(__file__).resolve().parent / "lambda_runtime" / "handler.py"
LOCK_DIR = DATA_DIR / "locks" / "eval-assets"
LAMBDA_RUNTIME = "python3.12"
LAMBDA_HANDLER = "handler.lambda_handler"
LAMBDA_MEMORY_MB = 256
LAMBDA_TIMEOUT_CAP_S = 300
LOG_RETENTION_DAYS = 14
LOGS_POLICY_NAME = "launchpad-evalfn-logs"
TAG_OPERATION = "launchpad:eval-operation"
TAG_MANAGED = "launchpad:managed"
TAG_PROVENANCE = "launchpad:provenance"
READBACK_ATTEMPTS = 30
READBACK_DELAY_S = 2.0
MAX_ATTEMPTS = 5  # bounded side-effect retries per operation (explicit, never silent)
CODE_CHAIN = ("lambda_role", "log_group", "lambda_function")
ACTIVE_STATUSES = ("queued", "running", "partial", "failed")
CLEANABLE_STATUSES = ("succeeded", "partial", "failed", "cleaning")
CODE_DISABLED_NOTE = ("Lambda code evaluators are not enabled in this build "
                      "(Settings.eval_code_evaluators_enabled) — the rule stays in the plan "
                      "as a reviewed intent; no Lambda is created and no evaluator row is "
                      "registered for it")

_CONFLICT_CODES = ("ConflictException", "ResourceConflictException", "AlreadyExistsException",
                   "EntityAlreadyExists", "ResourceAlreadyExistsException")
_NOT_FOUND_CODES = ("ResourceNotFoundException", "NotFoundException", "NoSuchEntity",
                    "NoSuchEntityException")

_LIVE_LOCK = threading.Lock()
_LIVE: dict[str, threading.Thread] = {}

ClientFactory = Callable[[WorkspaceContext, str], Any]
Recheck = Callable[[Session], Identity]


def _default_clients(workspace: WorkspaceContext, service: str) -> Any:
    return workspace.client(service)


def code_evaluators_enabled() -> bool:
    return bool(get_settings().eval_code_evaluators_enabled)


def _code(exc: BaseException) -> str:
    if isinstance(exc, ClientError):
        return str(exc.response.get("Error", {}).get("Code") or "")
    return ""


def _safe_error(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"[:500]


def _now() -> datetime:
    return datetime.now(UTC)


def _nonce() -> str:
    return secrets.token_hex(16)


# ---------------------------------------------------------------------------
# the Lambda package (deterministic)
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
    the intent (ownership clue on a lost CreateFunction response)."""
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


def function_name(op_id: str, code_group: str | None = None) -> str:
    suffix = "-" + hashlib.sha256(code_group.encode()).hexdigest()[:12] if code_group else ""
    # N17-obs394: the prefix the worker role's EvalWorkerCodeEvaluators grant covers
    return f"{CODE_EVALUATOR_FUNCTION_PREFIX}{op_id}{suffix}"


def compose_intents(plan: plan_contract.EvaluationPlan, op_id: str, *,
                    code_enabled: bool | None = None) -> list[dict[str, Any]]:
    enabled = code_evaluators_enabled() if code_enabled is None else code_enabled
    intents: list[dict[str, Any]] = [
        {"kind": "dataset", "key": "dataset", "name": plan.dataset.name, "status": "pending"},
    ]
    code = [e for e in plan.evaluators if isinstance(e, plan_contract.CodeEvaluator)]
    for entry in code:
        # Bare keys/names remain compatible with single-code operations.
        scoped = len(code) > 1
        fn = function_name(op_id, entry.key if scoped else None)
        rules = {"version": 1, "evaluators": {
            entry.name: entry.rules.model_dump(exclude_none=True),
        }}
        nonce = _nonce()
        _, digest = build_package(rules, nonce)
        _, rules_digest = build_package(rules)
        status = "pending" if enabled else "skipped"
        error = None if enabled else CODE_DISABLED_NOTE
        chain = [
            {"kind": "lambda_role", "key": "lambda_role", "name": fn, "status": status,
             "error": error, "nonce": _nonce()},
            {"kind": "log_group", "key": "log_group", "name": f"/aws/lambda/{fn}",
             "status": status, "error": error, "nonce": _nonce()},
            {"kind": "lambda_function", "key": "lambda_function", "name": fn,
             "status": status, "error": error, "nonce": nonce, "digest": digest,
             "rules_digest": rules_digest, "rules": rules,
             "timeout_s": min(entry.lambda_timeout_s, LAMBDA_TIMEOUT_CAP_S)},
        ]
        for res in chain:
            if scoped:
                res["code_group"] = entry.key
                res["key"] = f"{res['kind']}:{entry.key}"
        intents.extend(chain)
    for e in plan.evaluators:
        if e.kind in plan_contract.CLOUD_KINDS:
            skipped = e.kind == "code" and not enabled
            intents.append({
                "kind": "evaluator", "key": f"evaluator:{e.key}", "plan_key": e.key,
                "name": getattr(e, "name", ""), "definition": e.kind,
                "status": "skipped" if skipped else "pending",
                "error": CODE_DISABLED_NOTE if skipped else None,
                "reference_dependent": plan_contract.reference_dependent(e),
                **({"code_group": e.key} if e.kind == "code" and len(code) > 1 else {}),
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
        # N17-obs296: ``{code, params, message}`` per error for the console's own language
        "validation_error_details": validation_codes.details(row.validation_errors),
        # N17-obs308: the platform-written evaluator / recommendation notes, coded
        "note_details": plan_notes.note_details(row.content),
        "summary": plan_contract.plan_summary(
            row.content, resources=op.resources if op is not None else None) if valid else None,
        "created_by": row.created_by,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "operation_id": op.id if op else None,
    }


_PUBLIC_RESOURCE_KEYS = ("kind", "key", "plan_key", "code_group", "name", "status", "definition",
                         "error", "digest", "rules_digest", "reference_dependent", "attempts",
                         "result", "cleanup", "owned", "recovered")


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
        # the Global "legacy shared Lambda package" condition cannot arise here: every
        # CN operation packages one evaluator per function from the first write
        "requires_new_plan": False,
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
    if plan is not None:
        from app.assistant.tool_catalog import rule_catalog_errors

        errors += rule_catalog_errors(
            plan.evaluators, proposal.content, conversation.catalog or {}, scenarios=plan.scenarios,
        )
        if errors:
            plan = None
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


# evaluator resources of an operation that may still exist (``deleted`` / ``skipped``
# are the only states in which the name is known to be free again)
_NAME_FREE_STATES = ("deleted", "skipped")


def ledger_evaluator_names(db: Session, workspace_id: str) -> set[str]:
    """Names of evaluators the workspace's asset operations created or may have created
    (an uncertain create); never-dispatched and definitely rejected creates hold none."""
    taken: set[str] = set()
    ops = db.query(EvaluationAssetOperation).filter(
        EvaluationAssetOperation.workspace_id == workspace_id).all()
    for op in ops:
        for res in op.resources or []:
            if res.get("kind") != "evaluator" or not res.get("name") \
                    or res.get("status") in _NAME_FREE_STATES:
                continue
            if (res.get("result") or {}).get("evaluator_id") or _uncertain_create(res):
                taken.add(str(res["name"]))
    return taken


def taken_evaluator_names(db: Session, workspace: Workspace | None) -> set[str]:
    """Evaluator names a new plan must not reuse: every name an asset operation of the
    workspace recorded and has not deleted, plus the workspace's evaluator rows.

    Evaluator names are unique per workspace (``eval_evaluators``), and a second plan of
    the same conversation carries the proposal's names again — creating those would end
    ``conflict`` ("an evaluator named … exists that this operation cannot prove it
    created"). Both halves are ledger reads; no AWS call.
    """
    if workspace is None:
        return set()
    taken = ledger_evaluator_names(db, workspace.id)
    for row in evaluators_local.list_rows(db, workspace.id):
        taken.add(str(row.name))
    return taken


def rename_taken_evaluators(
    draft: dict[str, Any], taken: set[str], revision: int,
) -> dict[str, Any]:
    """A deterministic fresh name for every created evaluator whose name is taken:
    ``<name>_r<proposal revision>`` (then ``_2``, ``_3`` …), within the 48-character
    name limit. The reviewer sees the note; nothing else changes."""
    used = set(taken)
    for entry in draft.get("evaluators") or []:
        if entry.get("kind") not in plan_contract.CLOUD_KINDS or not entry.get("name"):
            continue
        name = str(entry["name"])
        if name not in used:
            used.add(name)
            continue
        n, candidate = 1, name
        while candidate in used:
            suffix = f"_r{revision}" + (f"_{n}" if n > 1 else "")
            candidate = name[:48 - len(suffix)] + suffix
            n += 1
        used.add(candidate)
        entry["name"] = candidate
        note = f"renamed from '{name}': that evaluator name is already taken in this workspace"
        entry["note"] = f"{entry['note']} · {note}" if entry.get("note") else note
    return draft


def prepare_plan(
    db: Session, conversation: AssistantConversation, *, revision: int, created_by: str,
) -> AssistantEvaluationPlan:
    """The platform draft for one proposal revision (no side effects beyond the row;
    ledger reads only to keep evaluator names unique)."""
    proposal = _proposal(db, conversation.id, revision)
    draft = plan_contract.draft_plan(
        proposal.content, revision=proposal.revision, content_hash=proposal.content_hash,
        agent_name=str(proposal.content.get("name") or "agent"),
    )
    draft = rename_taken_evaluators(
        draft, taken_evaluator_names(db, db.get(Workspace, conversation.workspace_id)),
        proposal.revision,
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
    """Fresh check that the approver is still an active account holding ``PERMISSION``.
    ``None`` when fine, else the reason. The config admin has no row and stays
    authorized."""
    if op.approver_user_id is None:
        return None
    user = db.get(User, op.approver_user_id)
    if user is None:
        return "the approving account no longer exists"
    if user.status != "active" or users_service.is_expired(user):
        return "the approving account is disabled or expired"
    if user.role != ROLE_ADMIN and PERMISSION not in granted_permissions(user.permissions):
        return f"the approving account no longer holds the {PERMISSION} permission"
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


def _require_judge_models(plan: plan_contract.EvaluationPlan,
                          resources: dict[str, Any] | None = None,
                          principal: model_catalog.Principal | None = None) -> None:
    """Judge / derived evaluators run on the CN model catalog only (spec C3): a model
    outside it is refused at approval — never a silent fallback at creation time. N17-obs227:
    a model of another member's personal provider is refused FIRST for the APPROVER's
    principal (422 `models.model_not_visible`) — the materialised judges would spend that
    member's key on every evaluation."""
    for e in plan.evaluators:
        model_id = getattr(e, "model_id", None)
        if e.kind in ("judge", "derived") and model_id:
            try:
                assert_model_visible(str(model_id), principal,
                                     where=f"evaluators.{e.key}.model_id")
                # catalog AND a configured provider in this workspace (N-R6-obs1)
                assert_model_available(str(model_id), where=f"evaluators.{e.key}.model_id",
                                       resources=resources)
            except ValueError as exc:
                raise AppError(hub_model_refusal_code(exc), str(exc),
                               {"model_id": model_id, "key": e.key}, status_code=422) from exc


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
    approver_is_admin: bool = False,
) -> Materialization:
    """Claim exactly one plan revision for materialization — atomically.

    The claim is a conditional UPDATE of the plan row (still ``draft``, still this
    hash, still the newest revision) in the same transaction that inserts the
    operation with every intent and the pinned workspace identity; ``recheck`` (when
    given) re-resolves the caller from the database inside that transaction and must
    still hold ``PERMISSION`` and collaborate on the conversation. Repeated / concurrent
    calls for the same plan return the recorded operation (200); a superseded, edited or
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
    from app.assistant.tool_catalog import rule_catalog_errors

    catalog_errors = rule_catalog_errors(
        plan.evaluators, _proposal(db, conversation.id, plan_row.source_revision).content,
        conversation.catalog or {}, scenarios=plan.scenarios,
    )
    if catalog_errors:
        raise AppError("assistant.evaluation_plan_invalid",
                       "the tool allowlist no longer validates against the selected catalog",
                       {"errors": catalog_errors}, status_code=409)
    _require_judge_models(plan, row.resources or {},
                          model_catalog.Principal(approver_user_id, approver_is_admin))
    fresh_row = db.get(Workspace, row.id)
    if fresh_row is None or fresh_row.bootstrap_status != "ready" or not (
            fresh_row.resources or {}).get("execution_role_arn"):
        raise AppError("assistant.workspace_not_ready",
                       "this workspace is not bootstrapped (no execution role)",
                       status_code=409)
    # a clash would only surface mid-operation as a "cannot prove it created" conflict;
    # refuse it here, before any write (a member edit may reintroduce a taken name)
    taken = taken_evaluator_names(db, fresh_row)
    clashes = sorted(e.name for e in plan.evaluators
                     if e.kind in plan_contract.CLOUD_KINDS and e.name in taken)
    if clashes:
        raise AppError("assistant.evaluation_plan_name_taken",
                       f"evaluator name(s) already taken in this workspace: {', '.join(clashes)}"
                       " — prepare a new plan revision (the platform renames them) or rename "
                       "them in the plan", {"names": clashes}, status_code=409)
    pinned = pin_workspace(fresh_row)
    op = EvaluationAssetOperation(
        id=_new_id(),  # assigned NOW: every intent name embeds it
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
        current = db.get(AssistantConversation, conversation.id)
        if not may_materialize(identity) or not may_collaborate(current, principal_of(identity)):
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
        conditions.append(_authorized_user(op.approver_user_id))
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
    """A create whose response (or checkpoint) was lost before the service-issued
    identity was recorded, while a resource with our name now exists. Ownership is
    UNPROVABLE: a nonce / tag / digest / name is copyable content, not identity.
    Recorded as ``unknown`` (never adopted, never deleted); re-evaluated on retry."""


def _approver(op: EvaluationAssetOperation) -> creators.Creator:
    """The creator of what an operation materializes: the plan's approver (upstream #261)."""
    return creators.Creator(user_id=op.approver_user_id, name=op.approved_by)


class _Fence:
    """Lease token + approver + pinned-workspace check before every write."""

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


def _resource(resources: list[dict[str, Any]], key: str, *,
              code_group: str | None = None) -> dict[str, Any]:
    """The intent of ``key`` (a kind for a code-chain member, scoped by ``code_group``)."""
    for r in resources:
        if r.get("key") == key:
            return r
    for r in resources:
        if r.get("kind") == key and r.get("code_group") == code_group:
            return r
    raise KeyError(key)


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
            conversation = db.get(AssistantConversation, op.conversation_id)
            self.catalog = dict((conversation.catalog if conversation else None) or {})
            # the client context is built from the workspace row that the fence has
            # just proven equal to the pinned identity (never from mutable defaults)
            self.workspace = workspace_context(db.get(Workspace, op.workspace_id))
            broken_groups: dict[str | None, str] = {}
            for r in op.resources or []:
                if r["kind"] in CODE_CHAIN and r.get("status") == "conflict":
                    broken_groups[r.get("code_group")] = (
                        f"{r['key']} is {r['status']} ({r.get('error')})")
            for key in [r["key"] for r in op.resources or []]:
                op = self.fence.guard(db)
                resources = json.loads(json.dumps(op.resources or []))
                res = _resource(resources, key)
                group = res.get("code_group")
                chain_broken = broken_groups.get(group)
                # ``unknown`` is re-evaluated (the operator may have removed the resource)
                # — never adopted
                if res.get("status") in ("ready", "skipped", "conflict"):
                    continue
                if res.get("status") == "failed" and int(res.get("attempts") or 0) >= MAX_ATTEMPTS:
                    if res["kind"] in CODE_CHAIN:
                        broken_groups[group] = f"{key} exhausted its attempts"
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
                    if res["kind"] in CODE_CHAIN:
                        broken_groups[group] = f"{key} is a conflict ({exc})"
                except _Unknown as exc:
                    res["status"] = "unknown"
                    res["error"] = str(exc)
                    self.fence.save(db, op, resources, f"{key}:unknown")
                    if res["kind"] in CODE_CHAIN:
                        broken_groups[group] = f"{key} is unknown ({exc})"
                except Exception as exc:  # noqa: BLE001 — recorded per resource
                    logger.warning("evaluation assets %s step %s failed: %s", op.id, key, exc)
                    res["status"] = "failed"
                    res["error"] = _safe_error(exc)
                    self.fence.save(db, op, resources, f"{key}:failed")
                    if res["kind"] in CODE_CHAIN:
                        broken_groups[group] = f"{key} failed ({_safe_error(exc)})"
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
        # N17-obs375: the dataset stores the names the agent calls its tools by (a Harness
        # agent calls a Gateway tool `launchpad_gw_<record>___<tool>`), or every trajectory
        # matcher fails on the declared name
        from app.assistant.tool_catalog import resolve_trajectory

        for item in items:
            if item.get("expected_trajectory"):
                item["expected_trajectory"] = resolve_trajectory(
                    item["expected_trajectory"], self.proposal_content, self.catalog)
        from app.evaluation.routers import _validate_items  # dataset ingress gate

        _validate_items(items)
        dataset = EvalDataset(
            workspace_id=op.workspace_id, name=plan.dataset.name, locale=plan.dataset.locale,
            description=plan.dataset.description, items=items, kind="predefined",
        )
        creators.stamp(dataset, _approver(op))  # the materializer acts for the approver
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

    # -- IAM role for the function (code evaluators enabled only) ------------------

    def _step_lambda_role(self, db, op, resources, res) -> None:
        iam = self._client("iam")
        ws = self.workspace
        assert ws is not None
        name = res["name"]
        nonce = res["nonce"]
        fn = _resource(resources, "lambda_function", code_group=res.get("code_group"))["name"]
        trust = {"Version": "2012-10-17", "Statement": [{
            "Effect": "Allow", "Principal": {"Service": ws.service_principal("lambda")},
            "Action": "sts:AssumeRole",
            "Condition": {"StringEquals": {"aws:SourceAccount": op.account_id}},
        }]}
        log_arn = ws.arn("logs", f"log-group:/aws/lambda/{fn}")
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
            self.fence.save(db, op, resources, f"{res['key']}:intent")
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
                try:
                    iam.get_role(RoleName=name)
                except ClientError as look:
                    if _code(look) in _NOT_FOUND_CODES and not isinstance(exc, ClientError):
                        raise exc from look  # transport failure and nothing exists: failed
                    raise
                if not isinstance(exc, ClientError) or prior:
                    raise _Unknown(
                        f"a CreateRole response was lost and a role named {name} exists; "
                        "its RoleId was never recorded, so this operation cannot prove it "
                        "created it (a provenance nonce is copyable) — review it manually, "
                        "then retry") from exc
                raise _Conflict(f"an IAM role named {name} already exists and was not "
                                "created by this operation") from exc
            expected_arn = ws.arn("iam", f"role/{name}", region="")
            if created.get("Arn") != expected_arn:
                raise _Conflict(f"role ARN {created.get('Arn')} is not {expected_arn}")
            res["result"] = {"role_arn": created["Arn"], "role_id": created["RoleId"],
                             "create_date": str(created.get("CreateDate") or ""),
                             "trust_document": trust}
            res["owned"] = True
            self.fence.save(db, op, resources, f"{res['key']}:accepted")
        self._write(db, iam.put_role_policy, RoleName=name, PolicyName=LOGS_POLICY_NAME,
                    PolicyDocument=json.dumps(policy))
        res["result"]["policy"] = LOGS_POLICY_NAME
        res["result"]["policy_document"] = policy

    # -- log group ---------------------------------------------------------------

    def _step_log_group(self, db, op, resources, res) -> None:
        logs = self._client("logs")
        ws = self.workspace
        assert ws is not None
        name = res["name"]
        nonce = res["nonce"]
        if not (res.get("result") or {}).get("created"):
            prior = bool(res.get("intent"))  # an earlier attempt already issued a create
            res["intent"] = {"requested_at": _now().isoformat(),
                             "dispatched": int((res.get("intent") or {}).get("dispatched") or 0)
                             + 1}
            self.fence.save(db, op, resources, f"{res['key']}:intent")
            try:
                self._write(db, logs.create_log_group, logGroupName=name,
                            tags={TAG_OPERATION: op.id, TAG_MANAGED: "true",
                                  TAG_PROVENANCE: nonce})
            except (_LeaseLost, _Stop):
                raise
            except Exception as exc:  # noqa: BLE001 — classified below
                if isinstance(exc, ClientError) and _code(exc) not in _CONFLICT_CODES:
                    raise
                if self._describe_log_group(logs, name) is None:
                    raise  # nothing exists: a plain failure (or a definite rejection)
                if not isinstance(exc, ClientError) or prior:
                    raise _Unknown(
                        f"a CreateLogGroup response was lost and a log group named {name} "
                        "exists; its creation identity was never recorded, so this operation "
                        "cannot prove it created it — review it manually, then retry") from exc
                raise _Conflict(f"log group {name} already exists and was not created by "
                                "this operation") from exc
            # CreateLogGroup returns no identity: read the service-issued creationTime /
            # ARN NOW, before the acceptance checkpoint and before any further write.
            mine = self._describe_log_group(logs, name)
            if mine is None:
                raise RuntimeError(f"log group {name} was created but could not be read back")
            expected_arn = ws.arn("logs", f"log-group:{name}")
            if str(mine.get("arn") or "").rstrip("*").rstrip(":") != expected_arn \
                    or not mine.get("creationTime"):
                raise _Conflict(f"log group identity {mine.get('arn')!r} / "
                                f"{mine.get('creationTime')!r} is not the expected {expected_arn}")
            res["result"] = {"created": True, "arn": mine.get("arn"),
                             "creation_time": mine.get("creationTime")}
            res["owned"] = True
            self.fence.save(db, op, resources, f"{res['key']}:accepted")
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

    # -- the function -------------------------------------------------------------

    def _step_lambda_function(self, db, op, resources, res) -> None:
        plan = self.plan
        assert plan is not None
        lam = self._client("lambda")
        role = _resource(resources, "lambda_role", code_group=res.get("code_group"))
        role_arn = (role.get("result") or {}).get("role_arn")
        if not role_arn:
            raise RuntimeError("lambda role not ready")
        payload, digest = build_package(res["rules"], res["nonce"])
        if digest != res.get("digest"):
            raise RuntimeError("package digest differs from the persisted intent")
        sha_b64 = code_sha256_b64(digest)
        name = res["name"]
        stored = res.get("result") or {}
        if not stored.get("function_arn"):
            prior = bool(res.get("request"))  # an earlier attempt already issued a create
            request = {
                "FunctionName": name, "Runtime": LAMBDA_RUNTIME, "Role": role_arn,
                "Handler": LAMBDA_HANDLER,
                "Description": f"Launchpad reviewed code evaluator (operation {op.id})",
                "Timeout": int(res["timeout_s"]), "MemorySize": LAMBDA_MEMORY_MB,
                "Publish": False,
                "Tags": {TAG_OPERATION: op.id, TAG_MANAGED: "true"},
            }
            res["request"] = {**request, "CodeSha256": sha_b64}
            self.fence.save(db, op, resources, f"{res['key']}:intent")
            created = None
            for attempt in range(6):
                try:
                    created = self._write(db, lam.create_function, **request,
                                          Code={"ZipFile": payload})
                    break
                except (_LeaseLost, _Stop):
                    raise
                except Exception as exc:  # noqa: BLE001 — classified below
                    code = _code(exc)
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
            assert created is not None
            bad = sorted(k for k, want in (
                ("FunctionName", name), ("Runtime", LAMBDA_RUNTIME), ("Role", role_arn),
                ("Handler", LAMBDA_HANDLER), ("CodeSha256", sha_b64),
                ("Timeout", int(res["timeout_s"])), ("MemorySize", LAMBDA_MEMORY_MB),
            ) if created.get(k) != want)
            if bad or not created.get("RevisionId"):
                raise _Conflict(f"CreateFunction answered with an identity that differs from "
                                f"the reviewed request on {bad or ['RevisionId']}")
            res["result"] = {"function_arn": created["FunctionArn"], "function_name": name,
                             "revision_id": created["RevisionId"],
                             "code_sha256": created.get("CodeSha256")}
            res["owned"] = True
            self.fence.save(db, op, resources, f"{res['key']}:accepted")
        stored = res["result"]
        if not stored.get("revision_id"):
            raise _Unknown(f"function {name}: the service identity returned by CreateFunction "
                           "was never recorded — ownership cannot be proven; review manually")
        cfg = self._wait_function_active(lam, name)
        if cfg.get("FunctionArn") != stored["function_arn"] or cfg.get("CodeSha256") != sha_b64:
            raise _Conflict("$LATEST differs from the approved identity (FunctionArn / "
                            "CodeSha256) — the function was changed or replaced; not publishing")
        if not stored.get("version_arn"):
            try:
                published = self._write(db, lam.publish_version, FunctionName=name,
                                        CodeSha256=sha_b64)
            except ClientError as exc:
                if _code(exc) in ("PreconditionFailedException", "InvalidParameterValueException"):
                    raise _Conflict("the function's code changed between our readback and "
                                    f"PublishVersion ({_code(exc)}) — not publishing") from exc
                raise
            if published.get("CodeSha256") != sha_b64 or not published.get("Version"):
                raise _Conflict("PublishVersion answered with a version whose CodeSha256 is not "
                                "the reviewed package — not ours")
            stored["version"] = str(published["Version"])
            stored["version_arn"] = published["FunctionArn"]
            stored["readback"] = {k: cfg.get(k) for k in ("Runtime", "Handler", "Timeout",
                                                          "MemorySize", "State", "CodeSha256")}
            self.fence.save(db, op, resources, f"{res['key']}:published")

    def _wait_function_active(self, lam: Any, name: str) -> dict[str, Any]:
        """Bounded wait for ``State == Active`` AND ``LastUpdateStatus == Successful``
        (the documented end of the first initialization). ``Failed`` on either is a
        failure; ``InProgress`` / a missing status is unknown and cannot be published."""
        cfg: dict[str, Any] = {}
        for _ in range(READBACK_ATTEMPTS):
            cfg = lam.get_function_configuration(FunctionName=name)
            state, upd = cfg.get("State"), cfg.get("LastUpdateStatus")
            if state == "Active" and upd == "Successful":
                return cfg
            if state == "Failed" or upd == "Failed":
                raise RuntimeError(f"function state {state} / last update {upd}: "
                                   f"{cfg.get('StateReason') or cfg.get('LastUpdateStatusReason')}")
            self.sleep(READBACK_DELAY_S)
        raise RuntimeError(f"function did not become Active / Successful in time (State="
                           f"{cfg.get('State')}, LastUpdateStatus={cfg.get('LastUpdateStatus')})")

    # -- existing evaluator references -------------------------------------------

    def _step_existing(self, db, op, resources, res) -> None:
        """Bind an EXISTING evaluator reference: canonical builtins (incl. trajectory)
        by catalog; a custom evaluator by its exact row in THIS workspace and — decoded
        from that row's definition, or from the owning plan's rules for a code evaluator
        this platform created — reference coverage of every target the plan's scenarios
        produce. A reference the plan cannot feed is never 'ready'."""
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
        row = (None if evaluators_local.is_managed_id(evaluator_id)
               else evaluators_local.find(db, op.workspace_id, evaluator_id))
        if row is None:
            raise RuntimeError(f"evaluator {evaluator_id} does not exist in this workspace")
        if row.status != "ACTIVE":
            raise RuntimeError(f"evaluator {evaluator_id} is {row.status}, not usable")
        definition = evaluators_local.definition_of(row)
        # A code evaluator this platform created in the SAME workspace has its declarative
        # rules on record (its owning operation's plan): decode its needs from them and
        # hold them against every target of THIS plan. A code evaluator nobody here owns
        # keeps its explicit unknown note.
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
        needs, kind = coverage.needs_from_definition(definition, rules)
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
            gaps = coverage.coverage_gaps(items, needs, str(row.level))
            if gaps:
                raise _Conflict(f"{evaluator_id} reads {', '.join(sorted(needs))} but these "
                                f"targets carry none: {', '.join(gaps[:6])}")
        res["result"] = {"evaluator_id": evaluator_id, "level": row.level, "status": row.status,
                         "name": row.name, "source": source, "definition": kind,
                         "reference_needs": sorted(needs) if needs is not None else None,
                         "note": note}
        res["reference_dependent"] = bool(needs) if needs is not None else None

    # -- created evaluators (ledger rows) ------------------------------------------

    def _step_evaluator(self, db, op, resources, res) -> None:
        plan = self.plan
        assert plan is not None
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
            # the dispatch is recorded durably BEFORE the row is written: a crash between
            # the row's commit and the acceptance checkpoint leaves a dispatched create
            # whose outcome is unknown — a row found under our name is then never adopted
            history = res.get("create_history")
            if history is None:
                history = res["create_history"] = []
            uncertain_before = any(e.get("outcome") in UNCERTAIN_OUTCOMES for e in history)
            history.append({"at": _now().isoformat(), "outcome": "dispatched"})
            self.fence.save(db, op, resources, f"{res['key']}:dispatch")
            try:
                row = self._create_row(db, op, entry, request)
            except (_LeaseLost, _Stop):
                raise
            except AppError as exc:
                if exc.code == "evaluator.name_taken":
                    history[-1]["outcome"] = "conflict"
                    if uncertain_before:
                        raise _Unknown(
                            f"an evaluator named {entry.name} exists and an earlier create of "
                            "this operation has no recorded outcome — it may be ours, but a "
                            "name is copyable content, so ownership stays unproven; review "
                            "it manually") from exc
                    raise _Conflict(f"an evaluator named {entry.name} exists that this "
                                    "operation cannot prove it created") from exc
                history[-1]["outcome"] = "rejected"  # this dispatch: not created
                raise
            except Exception:
                history[-1]["outcome"] = "lost"
                raise
            history[-1]["outcome"] = "created"
            res["result"] = {"evaluator_id": row.id, "name": row.name, "level": row.level,
                             "status": row.status, "definition": row.definition}
            res["owned"] = True
            self.fence.save(db, op, resources, f"{res['key']}:accepted")
            stored = res["result"]
        row = evaluators_local.find(db, op.workspace_id, stored["evaluator_id"])
        if row is None:
            raise RuntimeError(f"evaluator {stored['evaluator_id']} disappeared after creation")
        mismatch = [k for k, want in (
            ("name", request["evaluatorName"]), ("level", request["level"]),
            ("definition", request["definition"]),
        ) if getattr(row, k) != want]
        if mismatch:
            raise _Conflict(f"evaluator readback differs from the request on {mismatch}")
        stored.update({"status": row.status, "level": row.level, "name": row.name})

    def _create_row(self, db: Session, op, entry, request: dict[str, Any]) -> EvalEvaluator:
        self.fence.guard(db)  # fresh fence FIRST, then the write
        creator = _approver(op)  # the evaluator's creator is the plan's approver
        if isinstance(entry, plan_contract.JudgeEvaluator):
            return evaluators_local.create_judge(
                db, op.workspace_id, name=entry.name, instructions=entry.instructions,
                rating_scale=request["rating_scale"], model_id=request["model_id"],
                level=entry.level, description=request["description"], creator=creator,
            )
        if isinstance(entry, plan_contract.DerivedEvaluator):
            return evaluators_local.create_derived(
                db, op.workspace_id, name=entry.name,
                base_evaluator_id=entry.base_evaluator_id, model_id=request["model_id"],
                description=request["description"], creator=creator,
            )
        return evaluators_local.create_code(
            db, op.workspace_id, name=entry.name, lambda_arn=request["lambda_arn"],
            lambda_timeout_s=request["lambda_timeout_s"], level=entry.level,
            description=request["description"], creator=creator,
        )

    def _evaluator_request(self, entry, res, resources) -> dict[str, Any]:
        """The durable create request (what the row will carry) — a resumed attempt
        rebuilds exactly this and refuses a plan that drifted."""
        base = {"evaluatorName": entry.name,
                "description": (entry.description or entry.title)[:plan_contract.MAX_DESCRIPTION],
                "definition": entry.kind}
        if isinstance(entry, plan_contract.JudgeEvaluator):
            return {**base, "level": entry.level, "instructions": entry.instructions,
                    "rating_scale": [r.model_dump() for r in entry.rating_scale],
                    "model_id": entry.model_id}
        if isinstance(entry, plan_contract.DerivedEvaluator):
            return {**base, "level": evaluators_local.base_level(entry.base_evaluator_id),
                    "base_evaluator_id": entry.base_evaluator_id, "model_id": entry.model_id}
        fn = _resource(resources, "lambda_function", code_group=res.get("code_group"))
        version_arn = (fn.get("result") or {}).get("version_arn")
        if not version_arn:
            raise RuntimeError("code evaluator needs the published Lambda version")
        return {**base, "level": entry.level, "lambda_arn": version_arn,
                "lambda_timeout_s": int(entry.lambda_timeout_s)}


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
    """Startup: re-wake ONLY operations an authorized caller explicitly approved and that
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
    # a read-only ``existing`` reference recorded as a conflict is re-evaluated (the
    # member may have fixed the plan's targets or the referenced evaluator); owned
    # conflicts stay as prerequisites for review
    reopened = [r["key"] for r in resources
                if r.get("kind") == "existing" and r.get("status") == "conflict"]
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
# cleanup — delete exactly what the operation proved it owns
# ---------------------------------------------------------------------------


def cleanup_operation(
    db: Session, op: EvaluationAssetOperation, workspace: WorkspaceContext, *,
    clients: ClientFactory = _default_clients, sleeper: Callable[[float], None] = time.sleep,
) -> EvaluationAssetOperation:
    """Delete exactly the resources this operation proved it owns, dependency first,
    one persisted checkpoint per effect, under the same host lock / lease fence /
    re-authorization / pinned-identity checks as the worker.

    * evaluator rows first (id, name, level and definition must still equal the
      recorded request; an owned row that changed stays a reviewable ``conflict``);
    * the function, the log group and the dedicated role are removed ONLY when no owned
      evaluator of their chain remains, and each only after the identity recorded when
      its create succeeded still matches exactly (FunctionArn / CodeSha256; log-group
      creationTime / ARN; RoleId / ARN);
    * a create whose response was lost before the service-issued identity was recorded
      stays ``unknown`` while a resource with our name exists — nothing deleted;
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
    """A create may have gone out: an intent/request/result was persisted. Decided by the
    durable dispatch record, never by the display status — ``pending`` or ``blocked``
    after a crash still carries the intent of the create that went out."""
    return bool(r.get("intent") or r.get("request") or r.get("result") or r.get("owned")
                or r.get("create_history"))


UNCERTAIN_OUTCOMES = ("dispatched", "lost")


def _uncertain_create(r: dict[str, Any], *, exclude_last: bool = False) -> bool:
    """An evaluator create of this operation whose outcome is unknown (crash before the
    checkpoint) and that no later attempt has since resolved into an id."""
    if (r.get("result") or {}).get("evaluator_id"):
        return False
    history = list(r.get("create_history") or [])
    if exclude_last:
        history = history[:-1]
    return any(e.get("outcome") in UNCERTAIN_OUTCOMES for e in history)


def _cleanup(db: Session, fence: _Fence, workspace: WorkspaceContext,
             clients: ClientFactory, sleeper: Callable[[float], None]) -> EvaluationAssetOperation:
    op = fence.guard(db)
    resources = json.loads(json.dumps(op.resources or []))

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
        return _attempted(r) or r.get("status") in ("unknown", "retained", "delete_failed")

    # 1. evaluator rows — reconcile unknown creates, verify identity, delete
    for r in [x for x in resources if x["kind"] == "evaluator"]:
        if r.get("status") in ("deleted", "skipped"):
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
                if r.get("status") == "conflict" and not r.get("owned"):
                    continue  # foreign collision established at creation: never ours
                mark(r, "deleted", "every create was refused — nothing created")
                checkpoint(f"{r['key']}:reconciled")
                continue
            # a row may have been committed with a lost checkpoint; a row under our name
            # is a candidate for the reviewer, never proof — the name is copyable content
            found = db.query(EvalEvaluator).filter(
                EvalEvaluator.workspace_id == op.workspace_id,
                EvalEvaluator.name == request.get("evaluatorName")).first()
            hint = (f"; an evaluator with this name exists: {found.id}" if found is not None
                    else "; no evaluator with this name exists right now")
            mark(r, "unknown", "a create of this operation has no recorded outcome "
                               f"(name {request.get('evaluatorName')}); this operation cannot "
                               f"prove whether it created one{hint} — review it, then clean "
                               "up again")
            checkpoint(f"{r['key']}:unknown")
            continue
        if r.get("status") == "conflict" and not r.get("owned"):
            continue  # foreign collision: never ours
        eid = result["evaluator_id"]
        row = db.get(EvalEvaluator, eid)
        if row is None or row.workspace_id != op.workspace_id:
            mark(r, "deleted", "already gone")
            checkpoint(f"{r['key']}:gone")
            continue
        if (row.name != request.get("evaluatorName") or row.level != request.get("level")
                or row.definition != request.get("definition")):
            mark(r, "conflict", "owned evaluator was changed after creation (name / level / "
                                "definition differ from the recorded request) — review it "
                                "before deleting; not removed")
            checkpoint(f"{r['key']}:drift")
            continue
        fence.guard(db)
        evaluators_local.delete(db, row)
        mark(r, "deleted", None)
        checkpoint(f"{r['key']}:cleanup")

    # 2. each code chain waits only for its own evaluators (function → log group → role)
    groups = {r.get("code_group") for r in resources if r["kind"] in CODE_CHAIN}
    for group in groups:
        chain = {r["kind"]: r for r in resources
                 if r["kind"] in CODE_CHAIN and r.get("code_group") == group}
        evaluators_left = [r["key"] for r in resources if r["kind"] == "evaluator"
                           and r.get("definition") == "code"
                           and r.get("code_group") == group and unresolved(r)]
        if evaluators_left:
            for r in chain.values():
                if r.get("status") not in ("deleted", "skipped", "pending", "blocked"):
                    mark(r, "retained", f"owned evaluators remain: {', '.join(evaluators_left)}")
            checkpoint("code-chain:retained")
            continue
        fn = chain.get("lambda_function")
        if fn is not None and fn.get("status") not in ("deleted", "skipped"):
            fence.guard(db)
            _cleanup_function(clients(workspace, "lambda"), fn, mark, gone, sleeper)
            checkpoint(f"{fn['key']}:cleanup")
        lg = chain.get("log_group")
        if lg is not None and lg.get("status") not in ("deleted", "skipped"):
            if fn is not None and fn.get("status") != "deleted":
                mark(lg, "retained", f"the function is {fn.get('status')}")
            else:
                fence.guard(db)
                _cleanup_log_group(clients(workspace, "logs"), lg, mark, gone)
            checkpoint(f"{lg['key']}:cleanup")
        role = chain.get("lambda_role")
        if role is not None and role.get("status") not in ("deleted", "skipped"):
            if fn is not None and fn.get("status") != "deleted":
                mark(role, "retained", f"the function is {fn.get('status')}")
            else:
                fence.guard(db)
                _cleanup_role(clients(workspace, "iam"), role, mark, gone)
            checkpoint(f"{role['key']}:cleanup")

    remaining = [r["key"] for r in resources if unresolved(r)]
    final = "cleaned" if not remaining else "partial"
    error = None if not remaining else "cleanup left owned or unknown resources: " + ", ".join(
        remaining)
    fence.save(db, op, resources, "cleanup:finished", status=final, error=error,
               worker_token=None)
    db.expire_all()
    refreshed = db.get(EvaluationAssetOperation, op.id)
    assert refreshed is not None
    return refreshed


def _cleanup_function(lam: Any, fn: dict[str, Any], mark, gone, sleeper) -> None:
    result = fn.get("result") or {}
    if not result.get("function_arn") or not result.get("revision_id"):
        if not _attempted(fn):
            mark(fn, "deleted", "no create was attempted")
            return
        mark(fn, "unknown", "a CreateFunction of this operation has no recorded identity — "
                            "a function under our name is not provably ours; review manually")
        return
    name = result.get("function_name") or fn["name"]
    try:
        cfg = lam.get_function_configuration(FunctionName=name)
    except Exception as exc:  # noqa: BLE001
        if gone(exc):
            mark(fn, "deleted", "already gone")
        else:
            mark(fn, "delete_failed", _safe_error(exc))
        return
    sha = (fn.get("request") or {}).get("CodeSha256")
    if cfg.get("FunctionArn") != result["function_arn"] or (sha and cfg.get("CodeSha256") != sha):
        mark(fn, "conflict", "the function's identity differs from the recorded one "
                             "(FunctionArn / CodeSha256) — review it before deleting; not removed")
        return
    try:
        lam.delete_function(FunctionName=name)
    except Exception as exc:  # noqa: BLE001
        if not gone(exc):
            mark(fn, "delete_failed", f"DeleteFunction did not answer cleanly ({_safe_error(exc)})")
            return
    for _ in range(READBACK_ATTEMPTS):
        try:
            lam.get_function_configuration(FunctionName=name)
        except Exception as exc:  # noqa: BLE001
            if gone(exc):
                mark(fn, "deleted", None)
                return
            mark(fn, "delete_failed", f"readback after DeleteFunction failed ({_safe_error(exc)})")
            return
        sleeper(READBACK_DELAY_S)
    mark(fn, "delete_failed", "DeleteFunction accepted but the function is still present")


def _cleanup_log_group(logs: Any, lg: dict[str, Any], mark, gone) -> None:
    result = lg.get("result") or {}
    name = lg["name"]
    if not result.get("creation_time") or not result.get("arn"):
        if not _attempted(lg):
            mark(lg, "deleted", "no create was attempted")
            return
        mark(lg, "unknown", "a CreateLogGroup of this operation has no recorded identity — "
                            "a group under our name is not provably ours; review manually")
        return
    groups = logs.describe_log_groups(logGroupNamePrefix=name).get("logGroups") or []
    mine = [g for g in groups if g.get("logGroupName") == name]
    if not mine:
        mark(lg, "deleted", "already gone")
        return
    if mine[0].get("creationTime") != result["creation_time"] \
            or mine[0].get("arn") != result["arn"]:
        mark(lg, "conflict", "log group was re-created (creationTime / ARN differ) — not ours")
        return
    try:
        logs.delete_log_group(logGroupName=name)
    except Exception as exc:  # noqa: BLE001
        if not gone(exc):
            mark(lg, "delete_failed", _safe_error(exc))
            return
    mark(lg, "deleted", None)


def _cleanup_role(iam: Any, role: dict[str, Any], mark, gone) -> None:
    result = role.get("result") or {}
    name = role["name"]
    if not result.get("role_id"):
        if not _attempted(role):
            mark(role, "deleted", "no create was attempted")
            return
        mark(role, "unknown", "a CreateRole of this operation has no recorded RoleId — a role "
                              "under our name is not provably ours; review manually")
        return
    try:
        current = iam.get_role(RoleName=name)["Role"]
    except Exception as exc:  # noqa: BLE001
        if gone(exc):
            mark(role, "deleted", "already gone")
        else:
            mark(role, "delete_failed", _safe_error(exc))
        return
    if current.get("RoleId") != result["role_id"] or current.get("Arn") != result.get("role_arn"):
        mark(role, "conflict", "role was replaced (RoleId / ARN differ) — not ours; not removed")
        return
    try:
        if result.get("policy"):
            try:
                iam.delete_role_policy(RoleName=name, PolicyName=result["policy"])
            except Exception as exc:  # noqa: BLE001
                if not gone(exc):
                    raise
        iam.delete_role(RoleName=name)
    except Exception as exc:  # noqa: BLE001
        if not gone(exc):
            mark(role, "delete_failed", _safe_error(exc))
            return
    mark(role, "deleted", None)


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
                        "code_group": r.get("code_group"),
                        "definition": r.get("definition"),
                        "level": request.get("level"),
                        "reference_dependent": bool(r.get("reference_dependent"))}
    return None


def managed_package_evaluator_count(
    db: Session, owner: dict[str, Any] | None,
) -> int | None:
    """Rule sets in the Lambda package behind a managed code evaluator (always one in
    CN: every function packages exactly one evaluator); ``None`` when unknown."""
    if not owner or owner.get("definition") != "code":
        return None
    op = db.get(EvaluationAssetOperation, owner["operation_id"])
    if op is None:
        return None
    functions = [r for r in op.resources or [] if r.get("kind") == "lambda_function"
                 and r.get("code_group") == owner.get("code_group")]
    if len(functions) != 1:
        return None
    evaluators = (functions[0].get("rules") or {}).get("evaluators")
    return len(evaluators) if isinstance(evaluators, dict) else None


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
