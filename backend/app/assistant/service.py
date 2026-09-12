"""Architect assistant service: conversations, turns, proposal revisions, approval.

Invariants (``tests/test_assistant.py`` pins each one with request/fault probes):

* **Principal + workspace bound.** Every read and write filters on
  ``(workspace_id, owner_principal)``; another principal's, another workspace's or
  an unowned (NULL principal) conversation is a 404 — a recycled username inherits
  nothing.
* **Discussion never writes AWS.** A turn makes exactly one data-plane call
  (``InvokeHarness`` on the preset) and ledger writes. No proposal, valid or not,
  creates anything until a separate approval.
* **One in-flight turn per conversation.** A turn is an atomic conditional claim on
  the conversation row (``active_turn``); a concurrent turn is refused (409), a
  claim left behind by a dead process is reclaimed after ``TURN_CLAIM_TTL_S`` and on
  startup. The private runtime session id is written to the ledger *before* the
  data-plane call. A stream that is cut off persists the partial answer as an
  interrupted turn and never yields a proposal from incomplete output.
* **Server-owned bounded replay.** The preset runs with persistent memory disabled
  and every turn uses a fresh runtime session id, so the transcript is replayed
  through ``InvokeHarness.messages``, paired by turn, within one FINAL budget that
  includes the protocol preamble and the current message; omitted older turns are
  disclosed, the current message is never truncated (too large → 413).
* **Monotonic, unique revisions.** Model emissions and member edits both allocate
  their revision from the conversation's ``revision_seq`` inside the same short
  write transaction that stores the row (unique index), so two concurrent writers
  never share a number. One serialized-byte cap applies to both sources before
  validation; content is stored verbatim or not at all — never "fixed".
* **Approval is the only executor.** It names an exact revision + hash (content AND
  resolved bindings). Catalog and resource reads happen outside any lock; then one
  short transaction that first takes the conversation write lock re-resolves the
  caller's account, deploy permission, workspace grant and readiness from the
  database, re-reads the revision, claims the agent name atomically (shared with
  ordinary creation), claims the revision (draft → approved) and writes agent +
  deployment + job with their ids linked, in one commit. A repeated or concurrent
  approval of the same revision returns the recorded outcome (and re-wakes a queued
  job whose starter died); a different revision cannot ride on it.
* **Exact reviewed resources or nothing.** The pinned bindings (gateway ARN +
  outbound-auth identity, skill record + S3 content digest, KB-gateway
  prerequisites, memory ARN) are compared against the live resolution at approval
  and again at job entry; drift fails closed before any AWS write. No gateway is
  ever created by this flow.
"""

import hashlib
import json
import logging
import queue
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from botocore.exceptions import ClientError
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.assistant import proposal as proposal_contract
from app.assistant.principal import principal_of
from app.core.db import SessionLocal
from app.core.errors import AppError, NotFoundError
from app.deployer.pipeline import create_deployment
from app.models.assistant import AssistantConversation, AssistantMessage, AssistantProposal
from app.models.ledger import Agent, Job, Workspace
from app.routers.auth import Identity
from app.schemas.agent import ToolRef
from app.services import agent_names, knowledge, registry_console
from app.services.agentcore import harness as hc
from app.services.agentcore.client import control_client, data_client
from app.services.kb_gateway import gateway_identity
from app.services.memory import scoped_actor
from app.services.skill_ingest import SKILL_BUNDLE_MAX_BYTES
from app.services.workspace import WorkspaceContext
from app.system_agents import service as system_agents
from app.system_agents.presets import ARCHITECT
from app.system_agents.presets import is_reserved_name as _reserved

logger = logging.getLogger("launchpad.assistant")

PRESET = ARCHITECT
PERMISSION_DEPLOY = "agents.deploy"
MAX_PROMPT_CHARS = 100_000
MAX_PROMPT_BYTES = 300_000
# The FINAL composed request (preamble + catalog + replayed turns + current message)
# never exceeds this many characters; the newest turns that fit are kept.
MAX_REPLAY_CHARS = 160_000
MAX_REPLAY_TURNS = 12
CATALOG_MAX_ENTRIES = 60
# Explicit state bounds: a conversation is a requirement-intake session, not a log.
MAX_TURNS = 200
MAX_REVISIONS = 50
# A turn claim older than this belongs to a dead process (the harness itself times
# out at ≤ 3600 s; the preset uses 900 s) and may be reclaimed.
TURN_CLAIM_TTL_S = 1800
# SSE keep-alive cadence while the upstream is silent; also the longest the turn's
# consumer ever blocks, so cancellation is observed within one interval.
HEARTBEAT_S = 1.0

# ---------------------------------------------------------------------------
# the model-facing protocol (server-composed, appended to the FIRST user turn)
# ---------------------------------------------------------------------------

PROTOCOL_PREAMBLE = f"""\
# Launchpad assistant protocol

You are running inside AgentCore Launchpad's **architect assistant**. The person you
talk to wants to create ONE new business agent as a **managed Harness** in this
Launchpad workspace. Follow your methodology (Workshop baseline → confirm → only the
missing impactful questions → architecture, trade-offs, assumptions, manual tasks →
golden tests and evaluator recommendations), and keep every reply as ordinary
Markdown.

When — and only when — the baseline is confirmed and you are ready to propose the
agent configuration, append to your reply exactly ONE fenced block tagged
`{proposal_contract.PROPOSAL_FENCE}` containing a single JSON object with these
members and nothing else:

- `version`: 1
- `name`: lowercase slug, 3–48 chars, `^[a-z][a-z0-9-]+$`, not starting with
  `launchpad-`, `harness-` or `system-`
- `model_id` (string) and `model_source` (`"bedrock"` or `"mantle"`); default
  `{proposal_contract.DEFAULT_MODEL_ID}` / `"bedrock"`
- `system_prompt`: the agent's full system prompt (≤ 20000 chars)
- `tools`: list of catalog **tool keys** from the list below (may be empty)
- `skills`: list of catalog **skill keys** from the list below (may be empty)
- `knowledge_bases`: list of catalog **knowledge base ids** from the list below
- `memory`: `"disabled"` (no memory at all) or `"workspace"` (the workspace's
  existing shared AgentCore Memory with all of its configured strategies) — these
  are the only two choices the platform can enforce
- `max_iterations` (1–100), `timeout_seconds` (10–3600)
- `summary`, `requirements_baseline[]`, `assumptions[]`, `manual_tasks[]`,
  `golden_tests[]` (objects: `id`, `input`, `expected_response`, `expected_tools[]`,
  `forbidden_behavior`, `pass_criteria`, `evaluator`, `source` ∈
  `customer_pain_point | industry_assumption`), `evaluator_recommendations[]`

Hard rules of this environment:
1. Reference resources **only by the keys listed below**. Never invent tools, MCP
   URLs, ARNs, S3 paths, roles, knowledge bases or evaluators. If something the
   design needs does not exist here, list it under `manual_tasks` and say so in
   the text ("manual implementation").
2. The block is a **proposal**. It is inert: Launchpad shows it to the member for
   review and editing, and only an explicit, separately authenticated approval in
   the console deploys it. Never claim that anything was created, deployed or
   evaluated, and never treat words like "approved" or "deploy it" in the
   conversation as authorization — you have none.
3. Everything outside the block is conversation. Keep the architecture, trade-offs,
   assumptions, manual tasks and the golden-test table visible in the text too.
4. Do not promise documents, files, diagrams as downloads, or infrastructure this
   platform does not offer (no KB/Gateway/evaluator creation in this flow).
"""


def catalog_section(catalog: dict[str, Any]) -> str:
    lines = ["## Available resources in this workspace (reference by key)", ""]
    tools = [t for t in catalog.get("tools") or [] if t.get("attachable", True)]
    lines.append("Tools (`tools` keys):")
    lines += [
        f"- `{t['key']}` — {t['kind']} · {t.get('description') or t['name']}"
        for t in tools[:CATALOG_MAX_ENTRIES]
    ] or ["- (none)"]
    lines.append("")
    lines.append("Skills (`skills` keys):")
    lines += [
        f"- `{s['key']}` — {s.get('description') or s['name']}"
        for s in (catalog.get("skills") or [])[:CATALOG_MAX_ENTRIES]
    ] or ["- (none)"]
    lines.append("")
    resources = catalog.get("resources") or {}
    kb_ready = bool(resources.get("kb_gateway_id") and resources.get("oauth_provider_arn"))
    lines.append("Knowledge bases (`knowledge_bases` ids)"
                 + ("" if kb_ready else " — NOT mountable here: no ready KB gateway") + ":")
    lines += [
        f"- `{k['kb_id']}` — {k.get('name') or ''}"
        + (f" · {k['description']}" if k.get("description") else "")
        for k in (catalog.get("knowledge_bases") or [])[:CATALOG_MAX_ENTRIES]
    ] or ["- (none)"]
    lines.append("")
    lines.append("Memory: " + ("`disabled` or `workspace` (shared memory available)"
                              if resources.get("memory_arn") else
                              "`disabled` only (this workspace has no shared memory)"))
    target = catalog.get("target") or {}
    if target:
        lines.append("")
        lines.append(
            f"Target: workspace `{target.get('workspace_id')}` · region "
            f"`{target.get('region')}` (account shown to the member, not to you)."
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# catalog (the only AWS reads besides the turn itself)
# ---------------------------------------------------------------------------


def _short(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {str(exc)[:200]}"


def skill_source_prefix(path: str) -> str:
    """``s3://bucket/dir/`` — the exact directory the Harness loads for this source
    (same normalization as the deployer: a legacy ``…/SKILL.md`` means its parent)."""
    prefix = path.removesuffix("SKILL.md")
    return prefix if prefix.endswith("/") else prefix + "/"


def read_skill_bytes(workspace: WorkspaceContext, source_prefix: str) -> dict[str, bytes]:
    """Every object under the normalized directory, ``relative key → bytes``. Bounded by
    the platform skill bundle cap; raises ``ValueError`` when it is exceeded."""
    bucket, _, prefix = source_prefix[5:].partition("/")
    s3 = workspace.client("s3")
    files: dict[str, bytes] = {}
    total = 0
    kwargs: dict[str, Any] = {"Bucket": bucket, "Prefix": prefix}
    while True:
        page = s3.list_objects_v2(**kwargs)
        for obj in page.get("Contents") or []:
            key = obj["Key"]
            if key.endswith("/"):
                continue
            body = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
            total += len(body)
            if total > SKILL_BUNDLE_MAX_BYTES:
                raise ValueError(f"skill bundle under {source_prefix} exceeds the size cap")
            files[key[len(prefix):]] = body
        if not page.get("IsTruncated"):
            break
        kwargs["ContinuationToken"] = page.get("NextContinuationToken")
    return files


def bytes_digest(files: dict[str, bytes]) -> str:
    digest = hashlib.sha256()
    for rel in sorted(files):
        digest.update(rel.encode())
        digest.update(b"\0")
        digest.update(files[rel])
        digest.update(b"\0")
    return digest.hexdigest()


def skill_content_snapshot(workspace: WorkspaceContext, path: str) -> dict[str, Any] | None:
    """Identity of a skill bundle's CURRENT content: the normalized source directory
    and the sha256 of every loaded object's real bytes. ``None`` when unreadable/empty."""
    if not path.startswith("s3://"):
        return None
    prefix = skill_source_prefix(path)
    files = read_skill_bytes(workspace, prefix)
    if not files:
        return None
    return {
        "source_prefix": prefix,
        "content_digest": bytes_digest(files),
        "object_count": len(files),
        "total_bytes": sum(len(b) for b in files.values()),
    }


ASSISTANT_SKILLS_PREFIX = "assistant-skills"


def skill_copy_uri(bucket: str, digest: str) -> str:
    return f"s3://{bucket}/{ASSISTANT_SKILLS_PREFIX}/{digest[:16]}/"


def publish_skill_copy(workspace: WorkspaceContext, pinned: dict[str, Any]) -> str:
    """Approved package stage only: read the reviewed source directory, prove its bytes
    still hash to the reviewed digest, and publish them as an immutable,
    content-addressed copy under the workspace's own artifacts bucket
    (``assistant-skills/<digest16>/…``, conditional writes — an existing object must
    carry the same bytes). The Harness request then loads the COPY, never the mutable
    source. Nothing is ever deleted here."""
    bucket = (workspace.resources or {}).get("artifacts_bucket")
    if not bucket:
        raise RuntimeError("workspace has no artifacts_bucket to publish the reviewed skill copy")
    files = read_skill_bytes(workspace, pinned["source_prefix"])
    digest = bytes_digest(files) if files else None
    if not files or digest != pinned.get("content_digest"):
        raise RuntimeError(
            f"skill bytes under {pinned['source_prefix']} no longer match the reviewed content "
            f"({(digest or 'empty')[:12]} vs {str(pinned.get('content_digest'))[:12]}) — refusing "
            "to deploy changed skill bytes"
        )
    uri = skill_copy_uri(bucket, digest)
    key_prefix = uri[len(f"s3://{bucket}/"):]
    s3 = workspace.client("s3")
    for rel, body in sorted(files.items()):
        key = key_prefix + rel
        try:
            s3.put_object(Bucket=bucket, Key=key, Body=body, IfNoneMatch="*")
        except ClientError as exc:
            code = (exc.response or {}).get("Error", {}).get("Code")
            if code not in ("PreconditionFailed", "412"):
                raise
            existing = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
            if existing != body:  # same digest can never carry different bytes
                raise RuntimeError(
                    f"immutable skill copy {key} exists with different bytes — refusing to deploy"
                ) from exc
    return uri


def verify_skill_copy(workspace: WorkspaceContext, copy_uri: str, pinned: dict[str, Any]) -> None:
    files = read_skill_bytes(workspace, copy_uri)
    if not files or bytes_digest(files) != pinned.get("content_digest"):
        raise RuntimeError(f"immutable skill copy {copy_uri} does not match the reviewed digest")


def fetch_catalog(workspace: WorkspaceContext) -> dict[str, Any]:
    """Existing, attachable resources of the workspace with their deployment identity.

    Same sources as the create wizard (APPROVED registry MCP + AGENT_SKILLS records,
    ACTIVE managed KBs) plus what an approval pins: the live gateway ARN and
    outbound-auth identity per gateway record, the S3 content digest per skill, and
    the workspace prerequisites (shared memory ARN, KB gateway + OAuth provider,
    execution role). A failing source degrades to an empty list plus a warning.
    """
    warnings: list[str] = []
    tools: list[dict[str, Any]] = []
    skills: list[dict[str, Any]] = []
    kbs: list[dict[str, Any]] = []
    try:
        records = registry_console.attachable_records(workspace)
        gateway_refs: list[ToolRef] = []
        for server in records.get("mcp_servers") or []:
            if server.get("gateway"):
                if not (server.get("record_id") and server.get("gateway_id")):
                    continue
                entry = {
                    "key": f"gateway:{server['name']}",
                    "kind": "gateway",
                    "name": server["name"],
                    "description": server.get("description") or "",
                    "record_id": server["record_id"],
                    "gateway_id": server["gateway_id"],
                    "gateway_arn": server.get("gateway_arn"),
                    "gateway_name": None,
                    "auth_type": server.get("auth_type"),
                    "outbound_auth": None,
                    "attachable": bool(server.get("attachable", True)),
                    "reason": server.get("attachability_reason"),
                }
                tools.append(entry)
                if entry["attachable"]:
                    gateway_refs.append(ToolRef(
                        type="gateway", name=server["name"],
                        config={"record_id": server["record_id"],
                                "gateway_id": server["gateway_id"]},
                    ))
            elif server.get("url"):
                tools.append({
                    "key": f"mcp:{server['name']}",
                    "kind": "mcp",
                    "name": server["name"],
                    "description": server.get("description") or "",
                    "url": server["url"],
                    "record_id": server.get("record_id"),
                    "attachable": bool(server.get("attachable", True)),
                    "reason": server.get("attachability_reason"),
                })
        if gateway_refs:
            # The live gateway identity (ARN + outbound-auth identity) the deployer
            # will attach — resolved with the same helper the deploy stage uses.
            try:
                by_arn = {
                    a["gateway_arn"]: a
                    for a in registry_console.resolve_gateway_attachments(gateway_refs, workspace)
                }
                by_id = {a.get("gateway_id"): a for a in by_arn.values()}
                for entry in tools:
                    if entry["kind"] != "gateway":
                        continue
                    att = by_id.get(entry["gateway_id"])
                    if att is None:
                        entry["attachable"] = False
                        entry["reason"] = entry["reason"] or "gateway could not be resolved"
                        continue
                    entry["gateway_arn"] = att.get("gateway_arn")
                    entry["gateway_name"] = att.get("gateway_name")
                    entry["auth_type"] = att.get("auth_type")
                    entry["outbound_auth"] = att.get("outbound_auth")
            except (AppError, ClientError, KeyError, ValueError) as exc:
                warnings.append(f"gateway resolution unavailable: {_short(exc)}")
                for entry in tools:
                    if entry["kind"] == "gateway":
                        entry["attachable"] = False
                        entry["reason"] = "gateway could not be resolved"
        for skill in records.get("skills") or []:
            if not skill.get("path"):
                continue
            snapshot = None
            try:
                snapshot = skill_content_snapshot(workspace, skill["path"])
            except (ClientError, KeyError, ValueError) as exc:
                warnings.append(f"skill '{skill['name']}' content unreadable: {_short(exc)}")
            skills.append({
                "key": skill["name"],
                "name": skill["name"],
                "description": skill.get("description") or "",
                "path": skill["path"],
                "record_id": skill.get("record_id"),
                "source_prefix": (snapshot or {}).get("source_prefix"),
                "content_digest": (snapshot or {}).get("content_digest"),
                "object_count": (snapshot or {}).get("object_count"),
                "total_bytes": (snapshot or {}).get("total_bytes"),
            })
    except (AppError, ClientError, KeyError, ValueError) as exc:
        warnings.append(f"registry catalog unavailable: {_short(exc)}")
    try:
        for item in knowledge.list_kbs(workspace):
            if item.get("status") == "ACTIVE" and item.get("kb_id"):
                kbs.append({
                    "kb_id": item["kb_id"],
                    "name": item.get("name") or "",
                    "description": item.get("description") or "",
                })
    except (AppError, ClientError, KeyError, ValueError) as exc:
        warnings.append(f"knowledge base catalog unavailable: {_short(exc)}")
    res = workspace.resources or {}
    kb_gateway: dict[str, Any] | None = None
    if res.get("kb_gateway_id"):
        try:  # the live configuration a KB mount would depend on (read-only)
            detail = control_client(workspace).get_gateway(gatewayIdentifier=res["kb_gateway_id"])
            kb_gateway = gateway_identity(detail)
        except Exception as exc:  # unreadable → not mountable here (never created)
            warnings.append(f"knowledge-base gateway unreadable: {_short(exc)}")
    return {
        "fetched_at": datetime.now(UTC).isoformat(),
        "tools": tools,
        "skills": skills,
        "knowledge_bases": kbs,
        "warnings": warnings,
        "resources": {
            "memory_arn": res.get("memory_arn"),
            "kb_gateway_id": res.get("kb_gateway_id"),
            "kb_gateway_arn": res.get("kb_gateway_arn"),
            "oauth_provider_arn": res.get("oauth_provider_arn"),
            "execution_role_arn": res.get("execution_role_arn"),
            "kb_gateway": kb_gateway,
        },
        "target": {
            "workspace_id": workspace.id,
            "account_id": workspace.account_id,
            "region": workspace.region,
        },
    }


# ---------------------------------------------------------------------------
# availability
# ---------------------------------------------------------------------------

REASON_PRESET = "preset_not_active"


def preset_agent(db: Session, workspace_id: str) -> Agent | None:
    agent = system_agents.find_installed(db, workspace_id, PRESET)
    return agent if agent is not None and agent.status == "active" and agent.arn else None


def deploy_requirements(row: Workspace) -> list[dict[str, str]]:
    missing: list[dict[str, str]] = []
    if row.bootstrap_status != "ready":
        missing.append({"code": system_agents.REQ_BOOTSTRAP,
                        "message": f"workspace bootstrap is '{row.bootstrap_status}'"})
    if not (row.resources or {}).get("execution_role_arn"):
        missing.append({"code": system_agents.REQ_ROLE,
                        "message": "execution_role_arn missing from the workspace resource map"})
    return missing


def availability(db: Session, row: Workspace, identity: Identity) -> dict[str, Any]:
    preset = system_agents.preset_status(db, row, PRESET, is_admin=identity.is_admin)
    agent = preset_agent(db, row.id)
    res = row.resources or {}
    return {
        "workspace_id": row.id,
        "account_id": row.account_id,
        "region": row.region,
        "available": agent is not None,
        "reasons": [] if agent is not None else [REASON_PRESET],
        "preset": {
            "key": preset["key"],
            "label": preset["label"],
            "status": preset["status"],
            "agent_id": preset["agent_id"],
            "requirements": preset["requirements"],
            "can_install": preset["can_install"],
        },
        "can_deploy": identity.can(PERMISSION_DEPLOY),
        "deploy_requirements": deploy_requirements(row),
        "capabilities": {
            "shared_memory": bool(res.get("memory_arn")),
            "kb_gateway": bool(res.get("kb_gateway_id") and res.get("oauth_provider_arn")),
        },
        "is_admin": identity.is_admin,
        "owner": identity.username,
        "principal": principal_of(identity),
    }


def _require_available(db: Session, row: Workspace) -> Agent:
    agent = preset_agent(db, row.id)
    if agent is None:
        status = system_agents.preset_status(db, row, PRESET, is_admin=False)
        raise AppError(
            "assistant.unavailable",
            f"the {PRESET.label} preset is not active in this workspace "
            f"(status: {status['status']}); an administrator installs it from the "
            "System presets panel",
            {"preset_status": status["status"], "requirements": status["requirements"]},
            status_code=409,
        )
    return agent


# ---------------------------------------------------------------------------
# conversations
# ---------------------------------------------------------------------------


def owned_conversation(
    db: Session, workspace_id: str, principal: str, conversation_id: str
) -> AssistantConversation:
    """Principal + workspace bound; anything else — including a legacy row with no
    principal — is indistinguishable from missing."""
    row = db.get(AssistantConversation, conversation_id)
    if (
        row is None
        or row.workspace_id != workspace_id
        or row.owner_principal is None
        or row.owner_principal != principal
    ):
        raise NotFoundError("assistant.conversation_not_found", "conversation not found")
    return row


def list_conversations(db: Session, workspace_id: str, principal: str) -> list[dict[str, Any]]:
    rows = (
        db.query(AssistantConversation)
        .filter(
            AssistantConversation.workspace_id == workspace_id,
            AssistantConversation.owner_principal == principal,
        )
        .order_by(AssistantConversation.updated_at.desc())
        .limit(50)
        .all()
    )
    return [conversation_summary(db, r) for r in rows]


def create_conversation(
    db: Session, row: Workspace, workspace: WorkspaceContext, identity: Identity, title: str
) -> AssistantConversation:
    agent = _require_available(db, row)
    catalog = fetch_catalog(workspace)
    conversation = AssistantConversation(
        workspace_id=row.id,
        owner=identity.username,
        owner_principal=principal_of(identity),
        preset_agent_id=agent.id,
        title=(title or "")[:200],
        catalog=catalog,
    )
    db.add(conversation)
    db.commit()
    return conversation


def refresh_catalog(
    db: Session, conversation: AssistantConversation, workspace: WorkspaceContext
) -> dict[str, Any]:
    catalog = fetch_catalog(workspace)  # AWS reads outside any lock
    _lock_conversation(db, conversation.id)
    db.execute(update(AssistantConversation).where(AssistantConversation.id == conversation.id)
               .values(catalog=catalog))
    db.commit()
    db.expire(conversation)
    return catalog


def _messages(db: Session, conversation_id: str) -> list[AssistantMessage]:
    return (
        db.query(AssistantMessage)
        .filter(AssistantMessage.conversation_id == conversation_id)
        .order_by(AssistantMessage.id.asc())
        .all()
    )


def _proposals(db: Session, conversation_id: str) -> list[AssistantProposal]:
    return (
        db.query(AssistantProposal)
        .filter(AssistantProposal.conversation_id == conversation_id)
        .order_by(AssistantProposal.revision.asc())
        .all()
    )


def latest_proposal(db: Session, conversation_id: str) -> AssistantProposal | None:
    return (
        db.query(AssistantProposal)
        .filter(AssistantProposal.conversation_id == conversation_id)
        .order_by(AssistantProposal.revision.desc())
        .first()
    )


def proposal_by_revision(
    db: Session, conversation_id: str, revision: int
) -> AssistantProposal | None:
    return (
        db.query(AssistantProposal)
        .filter(AssistantProposal.conversation_id == conversation_id,
                AssistantProposal.revision == revision)
        .first()
    )


def conversation_summary(db: Session, row: AssistantConversation) -> dict[str, Any]:
    proposal = latest_proposal(db, row.id)
    return {
        "id": row.id,
        "title": row.title,
        "turns": row.turns,
        "turn_in_progress": row.active_turn,
        "status": row.status,
        "proposal_status": proposal.status if proposal else None,
        "proposal_revision": proposal.revision if proposal else None,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


def conversation_detail(db: Session, row: AssistantConversation) -> dict[str, Any]:
    return {
        **conversation_summary(db, row),
        "catalog": row.catalog or {},
        "messages": [
            {
                "id": m.id,
                "turn": m.turn,
                "role": m.role,
                "text": m.text,
                "name": m.name,
                "at": m.created_at.isoformat() if m.created_at else None,
            }
            for m in _messages(db, row.id)
        ],
        "proposals": [proposal_out(db, p) for p in _proposals(db, row.id)],
    }


def proposal_out(db: Session, p: AssistantProposal) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": p.id,
        "conversation_id": p.conversation_id,
        "revision": p.revision,
        "source": p.source,
        "status": p.status,
        "content": p.content,
        "content_hash": p.content_hash,
        "bindings": proposal_contract.bindings_view(p.bindings),
        "validation_errors": p.validation_errors or [],
        "created_by": p.created_by,
        "created_at": p.created_at.isoformat() if p.created_at else None,
        "approval": None,
        "rejected_by": p.rejected_by,
        "rejected_at": p.rejected_at.isoformat() if p.rejected_at else None,
    }
    if p.status == "approved":
        out["approval"] = _approval_out(db, p)
    return out


def _approval_out(db: Session, p: AssistantProposal) -> dict[str, Any]:
    agent = db.get(Agent, p.agent_id) if p.agent_id else None
    job = db.get(Job, p.job_id) if p.job_id else None
    return {
        "approved_by": p.approved_by,
        "approved_at": p.approved_at.isoformat() if p.approved_at else None,
        "agent_id": p.agent_id,
        "agent_name": agent.name if agent else None,
        "agent_status": agent.status if agent else None,
        "agent_error": agent.error if agent else None,
        "deployment_id": p.deployment_id,
        "job_id": p.job_id,
        "job_status": job.status if job else None,
    }


# ---------------------------------------------------------------------------
# the conversation write lock + turn claim
# ---------------------------------------------------------------------------


def _lock_conversation(db: Session, conversation_id: str) -> None:
    """Take the SQLite write lock on the conversation row as the FIRST statement
    of a transaction. Concurrent writers block until this transaction ends and
    then re-read current state; nothing here reads before it writes, so a shared
    → reserved upgrade can never deadlock. Callers keep the transaction short and
    never do network I/O while holding it."""
    db.rollback()  # start clean: no stale shared lock from an earlier read
    db.expire_all()
    rows = db.execute(
        update(AssistantConversation)
        .where(AssistantConversation.id == conversation_id)
        .values(updated_at=datetime.now(UTC))
    ).rowcount
    if rows != 1:
        db.rollback()
        raise NotFoundError("assistant.conversation_not_found", "conversation not found")


def _turn_limit_error() -> AppError:
    return AppError(
        "assistant.conversation_full",
        f"this conversation reached {MAX_TURNS} turns; start a new one",
        {"max_turns": MAX_TURNS}, status_code=409,
    )


def _claim_is_stale(row: AssistantConversation, now: datetime) -> bool:
    if row.active_turn is None or row.active_turn_started_at is None:
        return False
    started = row.active_turn_started_at
    if started.tzinfo is None:
        started = started.replace(tzinfo=UTC)
    return now - started > timedelta(seconds=TURN_CLAIM_TTL_S)


# Single-host live ownership: turns streaming in THIS process. A claim whose owner is
# still alive here is never taken over, whatever its age; TTL takeover is for orphans
# of a dead process only. The durable token still fences every write.
_LIVE_TURNS: dict[str, str] = {}
_LIVE_TURNS_LOCK = threading.Lock()


def _turn_live_here(conversation_id: str) -> bool:
    with _LIVE_TURNS_LOCK:
        return conversation_id in _LIVE_TURNS


def claim_turn(db: Session, conversation_id: str) -> tuple[int, str]:
    """Atomically reserve the next turn number, refusing while another turn is in
    flight (`409 assistant.turn_in_progress`) or the conversation is full. A claim
    older than ``TURN_CLAIM_TTL_S`` is taken over; the previous holder's token is
    invalidated so it can never publish (fail-safe, no result stealing)."""
    now = datetime.now(UTC)
    token = uuid.uuid4().hex
    # ONE ownership acquisition: the process-level registry lock is held across the
    # durable claim AND its local publication, so no other claimer in this process
    # can observe "claimed but not yet live" and take the turn over as an orphan.
    with _LIVE_TURNS_LOCK:
        return _claim_turn_locked(db, conversation_id, now, token)


def _claim_turn_locked(
    db: Session, conversation_id: str, now: datetime, token: str
) -> tuple[int, str]:
    _lock_conversation(db, conversation_id)
    claimed = db.execute(
        update(AssistantConversation)
        .where(
            AssistantConversation.id == conversation_id,
            AssistantConversation.active_turn.is_(None),
            AssistantConversation.turns < MAX_TURNS,
        )
        .values(turns=AssistantConversation.turns + 1,
                active_turn=AssistantConversation.turns + 1,
                active_turn_started_at=now, active_turn_token=token)
    ).rowcount
    if claimed != 1:
        row = db.get(AssistantConversation, conversation_id)
        if (row is not None and _claim_is_stale(row, now) and row.turns < MAX_TURNS
                and conversation_id not in _LIVE_TURNS):  # registry lock is held
            claimed = db.execute(
                update(AssistantConversation)
                .where(AssistantConversation.id == conversation_id,
                       AssistantConversation.active_turn == row.active_turn,
                       AssistantConversation.active_turn_token == row.active_turn_token)
                .values(turns=AssistantConversation.turns + 1,
                        active_turn=AssistantConversation.turns + 1,
                        active_turn_started_at=now, active_turn_token=token)
            ).rowcount
        if claimed != 1:
            db.rollback()
            if row is None:
                raise NotFoundError("assistant.conversation_not_found", "conversation not found")
            if row.turns >= MAX_TURNS and row.active_turn is None:
                raise _turn_limit_error()
            # another request held the claim when ours ran (it may have finished
            # since — a lost race is still "someone else's turn", never a fabricated one)
            raise AppError(
                "assistant.turn_in_progress",
                "another turn of this conversation was in flight; send the message again",
                {"active_turn": row.active_turn}, status_code=409,
            )
    db.commit()
    _LIVE_TURNS[conversation_id] = token  # published before any other claimer may run
    db.expire_all()
    turn = db.execute(
        select(AssistantConversation.active_turn)
        .where(AssistantConversation.id == conversation_id)
    ).scalar_one()
    return turn, token


def _holds_claim(db: Session, conversation_id: str, turn: int, token: str) -> bool:
    row = db.execute(
        select(AssistantConversation.active_turn, AssistantConversation.active_turn_token)
        .where(AssistantConversation.id == conversation_id)
    ).first()
    return row is not None and row[0] == turn and row[1] == token


def release_turn(db: Session, conversation_id: str, turn: int, token: str) -> None:
    db.rollback()
    db.execute(
        update(AssistantConversation)
        .where(AssistantConversation.id == conversation_id,
               AssistantConversation.active_turn == turn,
               AssistantConversation.active_turn_token == token)
        .values(active_turn=None, active_turn_started_at=None, active_turn_token=None,
                updated_at=datetime.now(UTC))
    )
    db.commit()


def clear_stale_turn_claims() -> int:
    """Startup: a claim can only be held by a live request of THIS process, so every
    claim found at boot belongs to a process that died mid-turn."""
    db = SessionLocal()
    try:
        count = db.execute(
            update(AssistantConversation)
            .where(AssistantConversation.active_turn.isnot(None))
            .values(active_turn=None, active_turn_started_at=None, active_turn_token=None)
        ).rowcount
        db.commit()
        return count
    finally:
        db.close()


def require_turn_capacity(conversation: AssistantConversation) -> None:
    """409 before a stream opens (a refusal inside the stream would be a 500)."""
    if (conversation.turns or 0) + 1 > MAX_TURNS:
        raise _turn_limit_error()
    if conversation.active_turn is not None and (
        _turn_live_here(conversation.id) or not _claim_is_stale(conversation, datetime.now(UTC))
    ):
        raise AppError(
            "assistant.turn_in_progress",
            f"turn {conversation.active_turn} of this conversation is still streaming; wait "
            "for it to finish",
            {"active_turn": conversation.active_turn}, status_code=409,
        )


# ---------------------------------------------------------------------------
# replay
# ---------------------------------------------------------------------------


def _preamble(conversation: AssistantConversation) -> str:
    return PROTOCOL_PREAMBLE + "\n" + catalog_section(conversation.catalog or {})


def check_prompt(conversation: AssistantConversation, prompt: str) -> None:
    """The current message is never truncated: too large for the final request → 413."""
    if len(prompt) > MAX_PROMPT_CHARS or len(prompt.encode("utf-8")) > MAX_PROMPT_BYTES:
        raise AppError(
            "assistant.prompt_too_large",
            f"the message exceeds {MAX_PROMPT_CHARS} characters / {MAX_PROMPT_BYTES} bytes",
            {"max_chars": MAX_PROMPT_CHARS, "max_bytes": MAX_PROMPT_BYTES}, status_code=413,
        )
    if len(_preamble(conversation)) + len(prompt) + 200 > MAX_REPLAY_CHARS:
        raise AppError(
            "assistant.prompt_too_large",
            "the message plus the protocol preamble exceeds the request budget; shorten "
            "the message or split it over several turns",
            {"max_request_chars": MAX_REPLAY_CHARS}, status_code=413,
        )


def compose_messages(
    conversation: AssistantConversation, history: list[AssistantMessage], prompt: str
) -> tuple[list[dict[str, Any]], int]:
    """The bounded ``InvokeHarness.messages`` replay and how many older turns were
    omitted from it.

    Turns are paired by their turn number (never by insert order): each replayed
    turn is the member's text plus the assistant's reply when there was one; a
    failed/interrupted turn is replayed with an explicit marker instead of a reply
    and counts against the budget like any other. The newest turns that fit the
    FINAL budget (preamble + catalog + turns + current message) are kept, the count
    of omitted older turns is disclosed in the preamble, and the current message is
    always sent whole (``check_prompt`` refuses one that cannot fit).
    """
    by_turn: dict[int, dict[str, list[str]]] = {}
    for m in history:
        if m.role not in ("user", "assistant") or not m.text:
            continue
        slot = by_turn.setdefault(m.turn, {"user": [], "assistant": []})
        slot[m.role].append(m.text)
    turns = [
        ("\n\n".join(slot["user"]), "\n\n".join(slot["assistant"]) or None)
        for _, slot in sorted(by_turn.items())
        if slot["user"]
    ]
    preamble = _preamble(conversation)
    budget = MAX_REPLAY_CHARS - len(preamble) - len(prompt) - 200
    kept: list[tuple[str, str | None]] = []
    used = 0
    for user_text, reply in reversed(turns):
        size = len(user_text) + len(reply or "") + 80
        if len(kept) >= MAX_REPLAY_TURNS or used + size > budget:
            break
        kept.insert(0, (user_text, reply))
        used += size
    omitted = len(turns) - len(kept)
    if omitted:
        preamble += (
            f"\n\n> Replay note: {omitted} earlier turn(s) of this conversation were omitted "
            "to fit the request; ask the member to restate anything you need from them."
        )
    messages: list[dict[str, Any]] = []
    pending_user: list[str] = []
    for user_text, reply in kept:
        pending_user.append(user_text)
        if reply is None:
            pending_user.append("(no assistant reply was produced for the message above)")
            continue
        messages.append({"role": "user", "content": [{"text": "\n\n".join(pending_user)}]})
        messages.append({"role": "assistant", "content": [{"text": reply}]})
        pending_user = []
    pending_user.append(prompt)
    messages.append({"role": "user", "content": [{"text": "\n\n".join(pending_user)}]})
    first = messages[0]["content"][0]["text"]
    messages[0]["content"][0]["text"] = preamble + "\n\n---\n\n## Member message\n\n" + first
    return messages, omitted


# ---------------------------------------------------------------------------
# revisions
# ---------------------------------------------------------------------------


def _allocate_revision(db: Session, conversation_id: str) -> int:
    """Next revision number, unique per conversation, allocated inside the caller's
    write transaction (which already holds the conversation lock)."""
    db.execute(
        update(AssistantConversation)
        .where(AssistantConversation.id == conversation_id)
        .values(revision_seq=AssistantConversation.revision_seq + 1)
    )
    revision = db.execute(
        select(AssistantConversation.revision_seq)
        .where(AssistantConversation.id == conversation_id)
    ).scalar_one()
    if revision > MAX_REVISIONS:
        db.rollback()
        raise AppError(
            "assistant.conversation_full",
            f"this conversation reached {MAX_REVISIONS} proposal revisions; start a new one",
            {"max_revisions": MAX_REVISIONS}, status_code=409,
        )
    return revision


def _supersede_drafts(db: Session, conversation_id: str) -> None:
    db.execute(
        update(AssistantProposal)
        .where(
            AssistantProposal.conversation_id == conversation_id,
            AssistantProposal.status.in_(("draft", "invalid")),
        )
        .values(status="superseded")
    )


def record_proposal(
    db: Session,
    conversation_id: str,
    catalog: dict[str, Any],
    workspace_id: str | None,
    raw: Any,
    *,
    source: str,
    created_by: str,
    extra_errors: list[str] | None = None,
) -> AssistantProposal:
    """Store one new revision inside the caller's locked transaction (no commit):
    valid → ``draft`` with bindings; otherwise ``invalid`` with the errors and the
    bounded raw object — never altered to make it pass."""
    content, display, errors = proposal_contract.validate(raw, catalog)
    errors = list(extra_errors or []) + errors
    if proposal_contract.serialized_bytes(display) > proposal_contract.PROPOSAL_MAX_BYTES:
        # the NORMALIZED content (defaults filled in) is what gets stored and hashed;
        # it must respect the same cap as the raw input
        display = {"_rejected": "normalized proposal exceeds the size limit"}
        errors = errors + [
            f"normalized proposal exceeds {proposal_contract.PROPOSAL_MAX_BYTES} bytes"]
    valid = content is not None and not errors
    bindings = proposal_contract.resource_bindings(content, catalog) if valid else None
    revision = _allocate_revision(db, conversation_id)
    _supersede_drafts(db, conversation_id)
    row = AssistantProposal(
        workspace_id=workspace_id,
        conversation_id=conversation_id,
        revision=revision,
        source=source,
        content=display,
        content_hash=proposal_contract.revision_hash(display, bindings),
        bindings=bindings,
        validation_errors=errors,
        status="draft" if valid else "invalid",
        created_by=created_by,
    )
    db.add(row)
    db.flush()
    return row


# ---------------------------------------------------------------------------
# turns
# ---------------------------------------------------------------------------


def _persist_partial(
    db: Session, conversation: AssistantConversation | tuple[str, str | None], turn: int,
    token: str, session_id: str, text: str, error: str,
) -> bool:
    """Persist a partial/failed turn — only while this worker still holds the claim
    (a reclaimed turn's late worker writes nothing). ``conversation`` may be the plain
    ``(id, workspace_id)`` pair: the finalizer runs after the owning session may have
    been closed, so it must not touch a (detached) ORM instance."""
    if isinstance(conversation, tuple):
        conversation_id, workspace_id = conversation
    else:
        conversation_id, workspace_id = conversation.id, conversation.workspace_id
    _lock_conversation(db, conversation_id)
    if not _holds_claim(db, conversation_id, turn, token):
        db.rollback()
        return False
    if text:
        db.add(AssistantMessage(
            workspace_id=workspace_id, conversation_id=conversation_id,
            turn=turn, role="assistant", text=text, runtime_session_id=session_id,
        ))
    db.add(AssistantMessage(
        workspace_id=workspace_id, conversation_id=conversation_id,
        turn=turn, role="error", text=error[:4000], runtime_session_id=session_id,
    ))
    db.commit()
    return True


class _ClaimLost(AppError):
    """Raised inside a turn when its claim was taken over: writes stop immediately."""

    def __init__(self) -> None:
        super().__init__(
            "assistant.turn_superseded",
            "this turn's claim was taken over; nothing was written or invoked for it",
            status_code=409,
        )


class TurnRun:
    """Ownership handle for one streaming turn.

    The response object (not garbage collection) drives cleanup: ``cancel`` flags
    the run, closes the upstream event stream so a blocked read returns, and then
    closes the generator (bounded retries while it is still executing in the
    worker thread) so ``run_turn``'s ``finally`` persists the partial answer as an
    interrupted turn and releases the claim. Closing the transport is not a claim
    that the AWS-side computation stopped.
    """

    def __init__(self) -> None:
        self.cancelled = False
        self.upstream: Any = None
        self.generator: Iterator[Any] | None = None  # the response body iterator
        self.inner: Iterator[dict[str, Any]] | None = None  # run_turn itself
        self.finished = False
        self._lock = threading.Lock()

    def attach_upstream(self, stream: Any) -> None:
        self.upstream = stream
        if self.cancelled:
            self._close_upstream()

    def _close_upstream(self) -> None:
        close = getattr(self.upstream, "close", None)
        if callable(close):
            try:
                close()
            except Exception:  # pragma: no cover — best effort
                pass

    def signal(self) -> None:
        """Non-blocking: mark cancelled and close the upstream stream so a worker
        blocked in the read returns NOW — called from the response's own disconnect
        handling before anything waits for the worker to unwind."""
        with self._lock:
            if self.finished:
                return
            self.cancelled = True
        self._close_upstream()

    def cancel(self, wait_s: float = 5.0) -> None:
        with self._lock:
            if self.finished:
                return
            self.cancelled = True
        self._close_upstream()
        deadline = time.monotonic() + wait_s
        # outer (response body) first, then run_turn itself: explicit, never refcount-
        # or GC-dependent
        for gen in (self.generator, self.inner):
            if gen is None:
                continue
            while True:
                try:
                    gen.close()  # raises GeneratorExit at the pending yield → finally runs
                    break
                except ValueError:  # "generator already executing" in the worker thread
                    if time.monotonic() > deadline:
                        logger.warning("assistant: turn generator still executing after %.1fs",
                                       wait_s)
                        return
                    time.sleep(0.02)
                except Exception:  # pragma: no cover — cleanup must not raise
                    logger.exception("assistant: closing the turn generator failed")
                    break


def run_turn(
    db: Session,
    conversation: AssistantConversation,
    row: Workspace,
    workspace: WorkspaceContext,
    identity: Identity,
    prompt: str,
    run: TurnRun | None = None,
) -> Iterator[dict[str, Any]]:
    """One assistant turn as SSE-ready events:
    ``meta → (tool|delta)* → (proposal)? → done`` or ``error``.

    The turn number is claimed atomically first; the user row — carrying the fresh
    private runtime session id — is committed before the data-plane call; the reply
    and any proposal land together in one locked transaction once the stream
    completed. A stream that errors, or is closed by the client before completion,
    persists the partial answer with an ``error`` row (no proposal) and releases the
    claim; nothing is retried automatically.
    """
    run = run or TurnRun()
    agent = _require_available(db, row)
    check_prompt(conversation, prompt)
    conversation_id = conversation.id
    turn, token = claim_turn(db, conversation_id)  # durable claim + live publication
    conversation = db.get(AssistantConversation, conversation_id)
    ids = (conversation_id, conversation.workspace_id)  # plain values for the finalizer
    session_id = hc.new_session_id()
    parts: list[str] = []
    terminal = False  # True once a terminal row (reply or error) was persisted
    producer: threading.Thread | None = None

    def owned() -> bool:
        with _LIVE_TURNS_LOCK:
            live = _LIVE_TURNS.get(conversation_id) == token
        return live and _holds_claim(db, conversation_id, turn, token)

    try:
        history = [m for m in _messages(db, conversation_id) if m.turn != turn]
        messages, omitted = compose_messages(conversation, history, prompt)
        # the FIRST write is fenced like every other one: composing the replay took
        # time, and ownership may have been replaced meanwhile
        _lock_conversation(db, conversation_id)
        if not owned():
            db.rollback()
            raise _ClaimLost()
        conversation = db.get(AssistantConversation, conversation_id)
        db.add(AssistantMessage(
            workspace_id=conversation.workspace_id, conversation_id=conversation_id,
            turn=turn, role="user", text=prompt, runtime_session_id=session_id,
        ))
        if not conversation.title:
            conversation.title = prompt.strip().splitlines()[0][:120] if prompt.strip() else ""
        conversation.preset_agent_id = agent.id
        db.commit()
        yield {"event": "meta", "data": {"conversation_id": conversation_id, "turn": turn,
                                         "session_id": session_id, "agent": agent.name,
                                         "omitted_turns": omitted}}
        try:
            actor = scoped_actor(agent.id, identity.username)
            # Upstream production runs in its own thread; this generator only ever
            # waits on the queue for one heartbeat interval at a time. The response
            # layer can therefore always reach a send/cancel point promptly, and a
            # disconnect closes the upstream (unblocking the producer) instead of
            # waiting behind an uncancellable blocked read.
            events: queue.Queue = queue.Queue()

            def produce() -> None:
                try:
                    for produced in hc.invoke_harness_events(
                        data_client(workspace), agent.arn, messages,
                        session_id=session_id, actor_id=actor, on_stream=run.attach_upstream,
                    ):
                        events.put(("event", produced))
                        if run.cancelled:
                            break
                    events.put(("end", None))
                except BaseException as exc:  # surfaced to the consumer below
                    events.put(("error", exc))

            # no data-plane call without CURRENT ownership
            _lock_conversation(db, conversation_id)
            if not owned():
                db.rollback()
                raise _ClaimLost()
            db.rollback()
            producer = threading.Thread(target=produce, daemon=True,
                                        name=f"assistant-turn-{conversation_id[:8]}-{turn}")
            producer.start()
            while True:
                try:
                    kind, event = events.get(timeout=HEARTBEAT_S)
                except queue.Empty:
                    if run.cancelled:
                        break
                    yield {"event": "heartbeat", "data": {}}
                    continue
                if kind == "end":
                    break
                if kind == "error":
                    raise event
                if run.cancelled:
                    break  # the owner cancelled: handled as interrupted below
                if event["event"] == "tool":
                    # fenced like every other write: only the current claim holder
                    _lock_conversation(db, conversation_id)
                    if not _holds_claim(db, conversation_id, turn, token):
                        db.rollback()
                        raise _ClaimLost()
                    db.add(AssistantMessage(
                        workspace_id=conversation.workspace_id, conversation_id=conversation_id,
                        turn=turn, role="tool", text="", name=event["data"].get("name"),
                        runtime_session_id=session_id,
                    ))
                    db.commit()
                elif event["event"] == "delta":
                    parts.append(event["data"].get("text", ""))
                yield event
        except Exception as exc:
            _persist_partial(db, ids, turn, token, session_id, "".join(parts),
                             f"{type(exc).__name__}: {exc}")
            terminal = True
            payload: dict[str, Any] = {"message": f"{type(exc).__name__}: {exc}"}
            if isinstance(exc, AppError):
                payload = {"code": exc.code, "message": exc.message}
            yield {"event": "error", "data": payload}
            return
        if run.cancelled:
            # the owner cancelled while we were blocked upstream: the stream ended
            # because it was closed, not because the reply completed
            _persist_partial(db, ids, turn, token, session_id, "".join(parts),
                             "interrupted: the response stream was closed before the reply "
                             "completed")
            terminal = True
            yield {"event": "error", "data": {"message": "interrupted"}}
            return
        text = "".join(parts)
        block, block_errors = proposal_contract.extract_block(text)
        _lock_conversation(db, conversation_id)
        if not _holds_claim(db, conversation_id, turn, token):
            # our claim was reclaimed as stale while we streamed: never publish
            db.rollback()
            terminal = True
            yield {"event": "error", "data": {"code": "assistant.turn_superseded",
                                              "message": "this turn's claim expired and was "
                                                         "taken over; its reply was discarded"}}
            return
        db.add(AssistantMessage(
            workspace_id=conversation.workspace_id, conversation_id=conversation_id,
            turn=turn, role="assistant", text=text, runtime_session_id=session_id,
        ))
        proposal_event: dict[str, Any] | None = None
        if block is not None or block_errors:
            raw: Any = block if block is not None else {}
            if isinstance(raw, str):
                try:
                    raw = json.loads(raw)
                except ValueError:
                    pass  # kept as the string; parse_content reports the JSON error
            fresh = db.get(AssistantConversation, conversation_id)
            revision = record_proposal(
                db, conversation_id, fresh.catalog or {}, fresh.workspace_id, raw,
                source="model", created_by=identity.username, extra_errors=block_errors,
            )
            proposal_event = {"event": "proposal", "data": proposal_out(db, revision)}
        db.commit()
        terminal = True
        if proposal_event is not None:
            yield proposal_event
        yield {"event": "done", "data": {"turn": turn}}
    finally:
        if not terminal:
            # GeneratorExit (client went away) or an unexpected error before the
            # terminal row: keep what the member saw, mark the turn interrupted, and
            # never derive a proposal from an incomplete reply.
            try:
                _persist_partial(
                    db, ids, turn, token, session_id, "".join(parts),
                    "interrupted: the response stream was closed before the reply completed",
                )
            except Exception:  # pragma: no cover — teardown must not mask the cause
                logger.exception("assistant: could not persist interrupted turn %s", turn)
        try:
            release_turn(db, conversation_id, turn, token)
        except Exception:  # pragma: no cover
            logger.exception("assistant: could not release turn claim %s", turn)
        with _LIVE_TURNS_LOCK:
            if _LIVE_TURNS.get(conversation_id) == token:
                del _LIVE_TURNS[conversation_id]
        # A terminal ledger row does not mean the transport is closed: on every exit
        # (completion, early error, claim loss, cancel) close the upstream stream and
        # give the producer a bounded chance to finish — never leave a blocked reader.
        run.signal()
        if producer is not None:
            producer.join(timeout=HEARTBEAT_S * 3)
            if producer.is_alive():  # pragma: no cover — logged, never hidden
                logger.warning("assistant: turn %s producer still alive after cleanup", turn)
        run.finished = True


# ---------------------------------------------------------------------------
# member edits / reject
# ---------------------------------------------------------------------------


def edit_proposal(
    db: Session, conversation: AssistantConversation, content: Any, identity: Identity
) -> AssistantProposal:
    """A member edit is a NEW revision (never a mutation of an approved one) and
    needs its own approval. The byte cap is enforced before anything is stored."""
    if proposal_contract.serialized_bytes(content) > proposal_contract.PROPOSAL_MAX_BYTES:
        raise AppError(
            "assistant.proposal_too_large",
            f"the proposal exceeds {proposal_contract.PROPOSAL_MAX_BYTES} bytes",
            {"max_bytes": proposal_contract.PROPOSAL_MAX_BYTES}, status_code=413,
        )
    conversation_id = conversation.id
    _lock_conversation(db, conversation_id)
    fresh = db.get(AssistantConversation, conversation_id)
    row = record_proposal(db, conversation_id, fresh.catalog or {}, fresh.workspace_id, content,
                          source="member", created_by=identity.username)
    db.commit()
    return row


def reject_proposal(
    db: Session, conversation: AssistantConversation, revision: int, identity: Identity
) -> AssistantProposal:
    conversation_id = conversation.id
    _lock_conversation(db, conversation_id)
    row = proposal_by_revision(db, conversation_id, revision)
    latest = latest_proposal(db, conversation_id)
    if row is None or latest is None or latest.revision != revision:
        db.rollback()
        raise AppError("assistant.proposal_stale",
                       "that proposal revision is not the current one",
                       {"current_revision": latest.revision if latest else None},
                       status_code=409)
    changed = db.execute(
        update(AssistantProposal)
        .where(AssistantProposal.id == row.id, AssistantProposal.status.in_(("draft", "invalid")))
        .values(status="rejected", rejected_by=identity.username, rejected_at=datetime.now(UTC))
    ).rowcount
    db.commit()
    db.expire_all()
    row = db.get(AssistantProposal, row.id)
    if changed != 1 and row.status == "approved":
        raise AppError("assistant.proposal_already_approved",
                       "an approved proposal cannot be rejected; its deployment already exists",
                       {"revision": row.revision, "approval": _approval_out(db, row)},
                       status_code=409)
    return row


# ---------------------------------------------------------------------------
# approval — the only executor
# ---------------------------------------------------------------------------


@dataclass
class ApprovalOutcome:
    proposal: AssistantProposal
    agent: Agent
    job_id: str | None
    deployment_id: str | None
    started: bool  # True ⇔ this call created the job (caller launches it)
    job_status: str | None = None


def _outcome(db: Session, row: AssistantProposal, *, started: bool) -> ApprovalOutcome:
    agent = db.get(Agent, row.agent_id) if row.agent_id else None
    if agent is None:  # pragma: no cover — the approval commit always links an agent
        raise AppError("assistant.approval_inconsistent",
                       "approved proposal has no agent row", status_code=500)
    job = db.get(Job, row.job_id) if row.job_id else None
    return ApprovalOutcome(row, agent, row.job_id, row.deployment_id, started,
                           job.status if job else None)


def _stale(current: AssistantProposal | None, message: str) -> AppError:
    return AppError("assistant.proposal_stale", message,
                    {"current_revision": current.revision if current else None},
                    status_code=409)


Recheck = Callable[[Session], tuple[Identity, Workspace]]


def _revalidate_caller(
    db: Session, conversation_id: str, identity: Identity, row: Workspace,
    recheck: "Recheck | None",
) -> None:
    """Current auth + permission + grant + readiness (via ``recheck``) and the immutable
    principal equality against the conversation owner. Raises the auth error itself."""
    if recheck is not None:
        approver, fresh_row = recheck(db)
    else:
        approver, fresh_row = identity, db.get(Workspace, row.id)
    conversation = db.get(AssistantConversation, conversation_id)
    if (
        conversation is None
        or fresh_row is None
        or principal_of(approver) != principal_of(identity)
        or conversation.owner_principal != principal_of(approver)
    ):
        raise NotFoundError("assistant.conversation_not_found", "conversation not found")


def approve_proposal(
    db: Session,
    conversation: AssistantConversation,
    row: Workspace,
    workspace: WorkspaceContext,
    identity: Identity,
    *,
    revision: int,
    content_hash: str,
    recheck: Recheck | None = None,
) -> ApprovalOutcome:
    """Execute exactly one revision, exactly once.

    ``recheck(db)`` re-resolves the caller (account still active, still holds the
    deploy permission, still granted this workspace) and the workspace row from the
    database **inside** the write transaction, so a revocation during the catalog
    reads is honoured; without it the request-start identity is used (tests only).
    """
    conversation_id = conversation.id
    # 1. the exact requested revision — an already approved one returns its outcome
    #    even when newer revisions exist (that is the idempotent retry path)
    proposal = proposal_by_revision(db, conversation_id, revision)
    latest = latest_proposal(db, conversation_id)
    if proposal is None:
        raise _stale(latest, "that proposal revision does not exist — review the latest")
    if proposal.content_hash != content_hash:
        raise _stale(latest, "the proposal content changed since it was shown — review it again")
    if proposal.status == "approved":
        _revalidate_caller(db, conversation_id, identity, row, recheck)
        return _outcome(db, proposal, started=False)
    if proposal.status != "draft":
        raise AppError("assistant.proposal_not_approvable",
                       f"a {proposal.status} proposal cannot be approved",
                       {"status": proposal.status, "errors": proposal.validation_errors},
                       status_code=409)
    if latest is None or latest.revision != revision:
        raise _stale(latest, "that proposal revision is not the current one — review the latest")
    # 2. request-start snapshot checks (cheap refusals before any cloud read)
    if not identity.can(PERMISSION_DEPLOY):
        raise AppError("auth.permission_required",
                       f"This action requires the '{PERMISSION_DEPLOY}' permission",
                       {"permission": PERMISSION_DEPLOY}, status_code=403)
    requirements = deploy_requirements(row)
    if requirements:
        raise AppError("assistant.workspace_not_ready",
                       "this workspace cannot deploy yet: "
                       + "; ".join(m["message"] for m in requirements),
                       {"requirements": requirements}, status_code=409)
    # 3. LIVE catalog + resource identity — network I/O, outside every lock
    def winner_after_io() -> ApprovalOutcome | None:
        """A twin may have executed this exact revision while we were reading: its
        recorded outcome is authoritative — but only for a caller who is STILL the
        authorized owner right now (re-resolved), never as a way around an auth
        or ownership failure."""
        db.rollback()
        db.expire_all()
        current = db.get(AssistantProposal, proposal.id)
        if current is None or current.status != "approved":
            return None
        _revalidate_caller(db, conversation_id, identity, row, recheck)
        return _outcome(db, current, started=False)

    try:
        live_catalog = fetch_catalog(workspace)
    except Exception as exc:
        winner = winner_after_io()
        if winner is not None:
            return winner
        raise AppError(
            "assistant.catalog_unavailable",
            f"the workspace catalog could not be read: {_short(exc)}",
            {"revision": proposal.revision}, status_code=502,
        ) from exc
    winner = winner_after_io()
    if winner is not None:
        return winner
    content, _display, errors = proposal_contract.validate(proposal.content, live_catalog)
    if content is None or errors:
        raise AppError("assistant.proposal_invalid",
                       "the proposal no longer validates against the workspace: "
                       + "; ".join(errors),
                       {"errors": errors, "revision": proposal.revision}, status_code=409)
    live_bindings = proposal_contract.resource_bindings(content, live_catalog)
    if proposal.bindings is None or live_bindings != proposal.bindings:
        raise AppError(
            "assistant.bindings_changed",
            "the resources this proposal binds to changed since it was reviewed — refresh "
            "the catalog and review a new revision",
            {"revision": proposal.revision,
             "changed": proposal_contract.binding_diff(proposal.bindings, live_bindings)},
            status_code=409,
        )
    if _reserved(content.name):
        raise AppError("agent.name_reserved",
                       f"'{content.name}' is reserved for a system-managed preset",
                       {"name": content.name}, status_code=409)
    spec = proposal_contract.to_agent_spec(content, live_catalog)
    # 4. ONE short write transaction: lock → recheck principal/workspace → re-read
    #    revision → name claim → revision claim → agent + deployment + job → commit
    _lock_conversation(db, conversation_id)
    try:
        if recheck is not None:
            approver, fresh_row = recheck(db)
        else:
            approver, fresh_row = identity, db.get(Workspace, row.id)
        # The principal that started the request, the principal resolved NOW and
        # the conversation's owner must be one and the same: an account replaced
        # under the same username during the catalog read is not the approver.
        fresh_conversation = db.get(AssistantConversation, conversation_id)
        if (
            fresh_conversation is None
            or principal_of(approver) != principal_of(identity)
            or fresh_conversation.owner_principal != principal_of(approver)
        ):
            raise NotFoundError("assistant.conversation_not_found", "conversation not found")
        if fresh_row is None or deploy_requirements(fresh_row):
            raise AppError("assistant.workspace_not_ready",
                           "this workspace cannot deploy yet",
                           {"requirements": deploy_requirements(fresh_row) if fresh_row else []},
                           status_code=409)
        current = db.get(AssistantProposal, proposal.id)
        if current.status == "approved":  # recheck() already ran above in this transaction
            db.rollback()
            return _outcome(db, current, started=False)
        if current.status != "draft" or current.content_hash != content_hash:
            raise _stale(latest_proposal(db, conversation_id),
                         "the proposal changed while it was being approved — review the latest")
        now = datetime.now(UTC)
        claimed = db.execute(
            update(AssistantProposal)
            .where(AssistantProposal.id == proposal.id, AssistantProposal.status == "draft")
            .values(status="approved", approved_by=approver.username, approved_at=now)
        ).rowcount
        if claimed != 1:  # pragma: no cover — the lock serializes writers
            raise _stale(latest_proposal(db, conversation_id), "the proposal changed meanwhile")
        agent = Agent(
            workspace_id=fresh_row.id,
            name=spec.name,
            method="harness",
            status="deploying",
            spec=spec.model_dump(),
            owner=approver.username,
        )
        db.add(agent)
        db.flush()
        agent_names.claim_agent_name(db, fresh_row.id, spec.name, agent.id)  # 409 → rollback
        deployment, job = create_deployment(
            db, agent, commit=False,
            payload_extra={"assistant": {
                "conversation_id": conversation_id,
                "proposal_id": proposal.id,
                "revision": proposal.revision,
                "approved_by": approver.username,
                "content": proposal.content,
                "bindings": proposal.bindings,
            }},
        )
        db.execute(
            update(AssistantProposal)
            .where(AssistantProposal.id == proposal.id)
            .values(agent_id=agent.id, deployment_id=deployment.id, job_id=job.id)
        )
        db.commit()
    except (AppError, IntegrityError) as exc:
        if isinstance(exc, AppError) and exc.code.split(".")[0] in ("auth", "workspace") or (
            isinstance(exc, NotFoundError)
        ):
            db.rollback()
            raise  # an authorization/ownership failure is never converted into a success
        db.rollback()
        db.expire_all()
        current = db.get(AssistantProposal, proposal.id)
        if current is not None and current.status == "approved":
            # a racing approval of this very revision won — hand back ITS outcome, to
            # a caller who is still the authorized owner
            _revalidate_caller(db, conversation_id, identity, row, recheck)
            return _outcome(db, current, started=False)
        raise
    db.expire_all()
    proposal = db.get(AssistantProposal, proposal.id)
    logger.info("assistant: proposal %s r%s approved by %s → agent %s job %s",
                proposal.id, proposal.revision, approver.username, agent.id, job.id)
    return ApprovalOutcome(proposal, agent, job.id, deployment.id, True, "queued")


# ---------------------------------------------------------------------------
# job entry guard: the pipeline deploys the reviewed bindings or nothing
# ---------------------------------------------------------------------------

_SPEC_KEYS = ("name", "method", "model_id", "model_source", "system_prompt", "tools",
              "skills", "knowledge_bases", "memory", "max_iterations", "timeout_seconds")


def assert_job_bindings_pinned(
    payload: dict[str, Any] | None, agent: Agent, workspace: WorkspaceContext
) -> None:
    """Run at deploy-job entry (fresh or resumed) BEFORE any stage: the approved
    content must still resolve, in the live workspace, to exactly the pinned
    bindings, the KB gateway prerequisites must exist (no gateway is created), and
    the agent's stored spec must be the pinned one. Any drift raises → the job lands
    as failed without touching AWS."""
    pin = (payload or {}).get("assistant") or {}
    pinned = pin.get("bindings")
    content_raw = pin.get("content")
    if not isinstance(pinned, dict) or not isinstance(content_raw, dict):
        raise RuntimeError("assistant job has no pinned bindings — refusing to deploy")
    copies = pin.get("skill_copies") or {}
    for key in _SPEC_KEYS:
        if key == "skills" and copies:
            expected = [copies.get(path, path) for path in pinned.get("skills") or []]
            if (agent.spec or {}).get("skills") == expected:
                continue
        if (agent.spec or {}).get(key) != pinned.get(key):
            raise RuntimeError(
                f"assistant job: agent spec member '{key}' differs from the approved "
                "bindings — refusing to deploy"
            )
    live_catalog = fetch_catalog(workspace)
    content, _display, errors = proposal_contract.validate(content_raw, live_catalog)
    if content is None or errors:
        raise RuntimeError("assistant job: approved proposal no longer validates in this "
                           "workspace: " + "; ".join(errors))
    live = proposal_contract.resource_bindings(content, live_catalog)
    if live != pinned:
        changed = proposal_contract.binding_diff(pinned, live)
        raise RuntimeError(
            "assistant job: reviewed resource bindings changed since approval "
            f"({', '.join(changed)}) — refusing to deploy the drifted resources; approve a "
            "fresh revision"
        )
    if content.knowledge_bases:
        res = workspace.resources or {}
        kb = (pinned.get("resources") or {}).get("kb_gateway") or {}
        if not (res.get("kb_gateway_id") == kb.get("gateway_id")
                and res.get("kb_gateway_arn") == kb.get("gateway_arn")
                and res.get("oauth_provider_arn") == kb.get("oauth_provider_arn")):
            raise RuntimeError("assistant job: the knowledge-base gateway this approval was "
                               "reviewed against is not the workspace's gateway anymore")
