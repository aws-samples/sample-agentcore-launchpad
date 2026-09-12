"""Ledger rows of the Launchpad architect assistant (SE-039).

Three tables, all workspace-scoped and owner-bound:

* ``assistant_conversations`` — one requirement-intake conversation between one
  console member and the protected ``aws-agent-solution-architect`` preset. It
  carries the catalog snapshot the model may reference (existing tools, S3 skills
  and knowledge bases of the workspace) so a proposal can be validated without a
  cloud read on every turn.
* ``assistant_messages`` — the server-owned transcript. The harness runs with
  persistent memory disabled and every turn uses a **fresh** runtime session id, so
  this table — not the service — is what carries context between turns (bounded
  replay through ``InvokeHarness.messages``). ``runtime_session_id`` is recorded so
  the generic Chat / invoke entrances can refuse to reuse an assistant session.
* ``assistant_proposals`` — every revision of the structured, **inert** proposal
  (model-emitted or member-edited). A revision is approved exactly once; the
  approval stamps approver, time and the created agent id in the same commit as
  the agent / deployment / job rows, so a retry or a concurrent click converges on
  one recorded outcome.
"""

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, ForeignKey, Index, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base
from app.models.ledger import _id, _now


class AssistantConversation(Base):
    __tablename__ = "assistant_conversations"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    workspace_id: Mapped[str | None] = mapped_column(String(32), index=True, default=None)
    # Display username of the member who opened it. Ownership is decided by the
    # IMMUTABLE principal below, never by this name: usernames can be deleted and
    # re-registered under a new account.
    owner: Mapped[str] = mapped_column(String(64), index=True)
    # Stable principal: ``user:<users.id>`` for a registered account,
    # ``config-admin`` for the built-in administrator, ``local-operator`` with the
    # login gate off. Nullable only for the additive migration — a NULL principal is
    # visible to nobody (fail closed), it is never adopted by a username match.
    owner_principal: Mapped[str | None] = mapped_column(String(96), index=True, default=None)
    # The preset agent row the turns were sent to (re-resolved on every turn).
    preset_agent_id: Mapped[str | None] = mapped_column(String(32), default=None)
    title: Mapped[str] = mapped_column(String(200), default="")
    # {fetched_at, tools[], skills[], knowledge_bases[], warnings[]}
    catalog: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    turns: Mapped[int] = mapped_column(default=0)
    # One in-flight turn per conversation: the turn number a request claimed
    # (atomic conditional UPDATE) and when, so an interrupted claim can be reclaimed
    # after ``TURN_CLAIM_TTL_S`` and cleared on startup.
    active_turn: Mapped[int | None] = mapped_column(default=None)
    active_turn_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
    # Random token minted with the claim: every write the turn makes is conditioned
    # on it, so a worker whose stale claim was reclaimed can never publish.
    active_turn_token: Mapped[str | None] = mapped_column(String(32), default=None)
    # Monotonic revision allocator, bumped inside the same transaction that writes
    # a revision row (unique index below), so two concurrent writers never share one.
    revision_seq: Mapped[int] = mapped_column(default=0)
    status: Mapped[str] = mapped_column(String(16), default="open")  # open | archived
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now
    )


class AssistantMessage(Base):
    __tablename__ = "assistant_messages"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    workspace_id: Mapped[str | None] = mapped_column(String(32), index=True, default=None)
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("assistant_conversations.id"), index=True
    )
    turn: Mapped[int] = mapped_column(default=0)
    role: Mapped[str] = mapped_column(String(16))  # user | assistant | tool | error
    text: Mapped[str] = mapped_column(Text, default="")
    name: Mapped[str | None] = mapped_column(String(80), default=None)  # tool name
    # The per-turn AgentCore runtime session id (assistant turns only).
    runtime_session_id: Mapped[str | None] = mapped_column(String(80), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    __table_args__ = (
        Index("ix_assistant_messages_runtime_session_id", "runtime_session_id"),
    )


class AssistantProposal(Base):
    __tablename__ = "assistant_proposals"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    workspace_id: Mapped[str | None] = mapped_column(String(32), index=True, default=None)
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("assistant_conversations.id"), index=True
    )
    revision: Mapped[int] = mapped_column(default=1)
    source: Mapped[str] = mapped_column(String(16))  # model | member
    # The validated proposal content (see app.assistant.proposal.ProposalContent);
    # for an invalid model emission the raw parse errors live in validation_errors
    # and content holds whatever structure could be kept for display.
    content: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    content_hash: Mapped[str] = mapped_column(String(64))
    # The concrete resources the content resolved to against the catalog snapshot
    # at revision time (the AgentSpec that WOULD be deployed: MCP URLs, gateway
    # record/gateway ids, S3 skill paths, KB ids). Approval re-resolves against the
    # live catalog and refuses when the result differs — a catalog change can never
    # silently deploy a different binding under an approved logical key.
    bindings: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=None)
    validation_errors: Mapped[list[str]] = mapped_column(JSON, default=list)
    # draft | approved | rejected | superseded | invalid
    status: Mapped[str] = mapped_column(String(16), default="draft", index=True)
    created_by: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    # Approval outcome — written in the SAME commit as the agent row.
    approved_by: Mapped[str | None] = mapped_column(String(64), default=None)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)
    agent_id: Mapped[str | None] = mapped_column(String(32), default=None)
    # Denormalized after the commit (best effort); readers fall back to the agent.
    deployment_id: Mapped[str | None] = mapped_column(String(32), default=None)
    job_id: Mapped[str | None] = mapped_column(String(32), default=None)
    rejected_by: Mapped[str | None] = mapped_column(String(64), default=None)
    rejected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)

    __table_args__ = (
        UniqueConstraint("conversation_id", "revision", name="uq_assistant_proposals_revision"),
    )


class AgentNameClaim(Base):
    """Live agent names of a workspace, claimed atomically by every creation path.

    ``agents.name`` is only API-checked for uniqueness (a deleted agent's name is
    reusable), so two concurrent creations — an assistant approval and an ordinary
    ``POST /api/agents``, or two approvals — could both pass the holder query. The
    composite primary key makes the claim an INSERT that exactly one writer wins;
    the row is released when the agent is deleted. Legacy agents predate the table
    and are still caught by the holder query, never by this claim. The unique column is
    ``claim_key`` = ``<workspace_id>:<name>``.
    """

    __tablename__ = "agent_name_claims"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_id)
    workspace_id: Mapped[str | None] = mapped_column(String(32), index=True, default=None)
    name: Mapped[str] = mapped_column(String(64))
    # ``<workspace_id>:<name>`` — the single unique column that IS the atomic claim
    # (a composite index over workspace_id would tie the scoped column to the claim
    # and break the ledger upgrade round trip).
    claim_key: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    agent_id: Mapped[str] = mapped_column(String(32), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    @staticmethod
    def key_for(workspace_id: str, name: str) -> str:
        return f"{workspace_id}:{name}"
