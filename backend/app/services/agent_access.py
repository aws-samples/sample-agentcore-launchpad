"""Agent visibility — the one choke point every agent surface loads through.

Agents follow the workspace `resource_visibility` rule of `services/visibility.py`
on `Agent.created_by` (stamped by `services/creators.py` on console creation,
conversion and architect approval; NULL for discovered runtimes, system presets
and legacy rows, which therefore stay visible to every member).

- `agent_in` is the by-id load: workspace + visibility. Each caller keeps its own
  miss answer (`agent.not_found` 404, `agent.not_active` 400, …), so a hidden agent
  is indistinguishable from a missing one on that route.
- `agent_clause` is the same rule as SQL, for lists (filter before any limit).
- `hidden_agent_ids` / `HiddenAgents` serve the derived surfaces that only *name*
  an agent (KB used-by, observability rows, memory actors, registry records, …).
- `check_pins` refuses a spec that pins a KB / memory the caller cannot see.

The `viewer` default is `Viewer.ALL`: deploy pipeline, resume, workers, purge, the
observability resolver and name claims stay unfiltered. Only routers pass
`require_viewer`. A non-filtering viewer (shared workspace, any admin) costs no
extra query anywhere in this module.
"""

from __future__ import annotations

from functools import cached_property
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.errors import AppError, NotFoundError
from app.models.ledger import Agent, ChatSession
from app.services import creators, visibility
from app.services.memory import SCOPE_SEP, spec_memory_id
from app.services.visibility import Viewer
from app.services.workspace import WorkspaceContext


def agent_clause(viewer: Viewer) -> Any:
    """`sees` on `Agent.created_by` as a SQL predicate (`true()` when unfiltered)."""
    return visibility.clause(viewer, Agent.created_by)


def agent_in(
    db: Session,
    workspace_id: str | None,
    agent_id: str | None,
    *,
    viewer: Viewer = Viewer.ALL,
    include_deleted: bool = False,
) -> Agent | None:
    """The agent when it lives in this workspace and `viewer` may see it, else None.

    A foreign, missing, hidden (and, unless `include_deleted`, deleted) id all read
    the same, so the caller's miss error discloses nothing.
    """
    if not agent_id:
        return None
    agent = db.get(Agent, agent_id)
    if agent is None or agent.workspace_id != workspace_id:
        return None
    if not include_deleted and agent.status == "deleted":
        return None
    if not visibility.sees(viewer, db, agent.created_by):
        return None
    return agent


def agent_visible(db: Session, viewer: Viewer, agent: Agent | None) -> bool:
    return agent is not None and visibility.sees(viewer, db, agent.created_by)


def require_agent_visible(db: Session, viewer: Viewer, agent: Agent | None,
                          error: AppError) -> None:
    """Raise `error` when `agent` is hidden from `viewer` (cross-module mutations:
    the run / experiment / canary row was visible, its agent must be too)."""
    if agent is not None and not visibility.sees(viewer, db, agent.created_by):
        raise error


def hidden_agent_ids(db: Session, viewer: Viewer, workspace_id: str | None) -> set[str]:
    """Every agent id of the workspace hidden from `viewer` — deleted rows included,
    since a deleted agent's history (sessions, memory actors) is still its creator's.
    One query; empty without one when the viewer does not filter."""
    if not viewer.filters:
        return set()
    rows = db.execute(
        select(Agent.id).where(
            Agent.workspace_id == workspace_id,
            Agent.created_by.is_not(None),
            ~agent_clause(viewer),
        )
    )
    return {agent_id for (agent_id,) in rows}


def hidden_agent_names(db: Session, viewer: Viewer, workspace_id: str | None) -> set[str]:
    """Names of the **live** agents hidden from `viewer` — for derived lists that only
    carry names (KB / memory used-by, Connections refs). Live names are unique per
    workspace, so a name identifies one agent. Empty without a query when unfiltered."""
    if not viewer.filters:
        return set()
    rows = db.execute(
        select(Agent.name).where(
            Agent.workspace_id == workspace_id,
            Agent.status != "deleted",
            Agent.created_by.is_not(None),
            ~agent_clause(viewer),
        )
    )
    return {name for (name,) in rows}


class HiddenAgents:
    """A request-scoped snapshot of the agents hidden from one viewer, for the
    surfaces that hold an agent only by derived key — an actor id, a span
    `service.name`, a registry record id, a session id. Applied after any
    per-workspace cache (same pattern as `assistant.sessions.PrivateSessions`).

    Falsy when nothing is hidden, so callers skip all filtering work."""

    def __init__(self, db: Session, viewer: Viewer, workspace_id: str | None):
        self._db = db
        self.workspace_id = workspace_id
        self.ids: frozenset[str] = frozenset(hidden_agent_ids(db, viewer, workspace_id))

    def __bool__(self) -> bool:
        return bool(self.ids)

    # ── keys ────────────────────────────────────────────────────────────────

    def hides(self, agent_id: str | None) -> bool:
        return bool(agent_id) and agent_id in self.ids

    def hides_actor(self, actor_id: str | None) -> bool:
        """A memory actor `<agent>__<human>` (`memory.scoped_actor`) of a hidden agent."""
        if not self.ids or not actor_id:
            return False
        agent_part, sep, _ = actor_id.partition(SCOPE_SEP)
        return bool(sep) and agent_part in self.ids

    def hides_namespace(self, namespace: str | None) -> bool:
        """A long-term namespace (`/facts/<actor>`, …) carrying a hidden agent's actor."""
        if not self.ids or not namespace:
            return False
        return any(self.hides_actor(part) for part in namespace.split("/"))

    @cached_property
    def _resolver(self):
        from app.services.observability import build_agent_resolver

        return build_agent_resolver(self._db, self.workspace_id)

    def hides_service(self, service: str | None) -> bool:
        """A span `service.name` resolved (observability resolver) to a hidden agent."""
        if not self.ids or not service:
            return False
        agent = self._resolver(service)
        return agent is not None and agent.id in self.ids

    @cached_property
    def session_ids(self) -> frozenset[str]:
        """Chat + eval-run session ids that belong to hidden agents (ledger-attributed)."""
        if not self.ids:
            return frozenset()
        from app.evaluation.models import EvalRun

        out = {
            sid
            for (sid,) in self._db.query(ChatSession.session_id).filter(
                ChatSession.workspace_id == self.workspace_id,
                ChatSession.agent_id.in_(self.ids),
            )
            if sid
        }
        for (sids,) in self._db.query(EvalRun.session_ids).filter(
            EvalRun.workspace_id == self.workspace_id, EvalRun.agent_id.in_(self.ids)
        ):
            out.update(s for s in (sids or []) if isinstance(s, str) and s)
        return frozenset(out)

    def hides_session(self, session_id: str | None) -> bool:
        return bool(self.ids) and bool(session_id) and session_id in self.session_ids

    @cached_property
    def record_ids(self) -> frozenset[str]:
        """Registry record ids the hidden agents' `register` stage created."""
        if not self.ids:
            return frozenset()
        return frozenset(
            rid
            for (rid,) in self._db.query(Agent.registry_record_id).filter(
                Agent.id.in_(self.ids), Agent.registry_record_id.is_not(None)
            )
            if rid
        )

    def hides_record(self, record_id: str | None) -> bool:
        return bool(self.ids) and bool(record_id) and record_id in self.record_ids

    # ── payloads ────────────────────────────────────────────────────────────

    def mentions(self, payload: Any, depth: int = 0) -> bool:
        """True when an observability payload is attributable to a hidden agent:
        an `agent_id` of one, a `service` resolving to one, or a hidden session id."""
        if not self.ids or depth > 12:
            return False
        if isinstance(payload, str):
            return self.hides_session(payload)
        if isinstance(payload, dict):
            for key, value in payload.items():
                if key == "agent_id" and isinstance(value, str) and self.hides(value):
                    return True
                if key == "service" and isinstance(value, str) and self.hides_service(value):
                    return True
                if self.mentions(value, depth + 1):
                    return True
            return False
        if isinstance(payload, list | tuple):
            return any(self.mentions(v, depth + 1) for v in payload)
        return False


# ── spec pins ────────────────────────────────────────────────────────────────


def _kb_ids(spec: dict[str, Any] | None) -> set[str]:
    out: set[str] = set()
    for ref in (spec or {}).get("knowledge_bases") or []:
        kb_id = ref.get("kb_id") if isinstance(ref, dict) else getattr(ref, "kb_id", None)
        if kb_id:
            out.add(kb_id)
    return out


def check_pins(
    db: Session,
    viewer: Viewer,
    workspace: WorkspaceContext,
    spec: dict[str, Any] | None,
    *,
    previous_spec: dict[str, Any] | None = None,
) -> None:
    """Refuse a spec that pins a KB or memory hidden from `viewer`.

    With `previous_spec` (redeploy / inbound-auth) only the ids **added** relative to
    the stored spec are checked, so an agent pinned while the workspace was shared
    keeps re-publishing after a switch to isolated. Runs before any row change and
    any AWS call; the miss answers are the ones a missing id gets (`kb.not_found`,
    `agent.memory_not_managed`).
    """
    if not viewer.filters:
        return
    added_kbs = _kb_ids(spec) - _kb_ids(previous_spec)
    if added_kbs:
        hidden = visibility.hidden_aws_ids(viewer, db, workspace.id, creators.KB, added_kbs)
        if hidden:
            raise NotFoundError(
                "kb.not_found", "knowledge base not found", {"kb_id": sorted(hidden)[0]}
            )
    memory_id = spec_memory_id(spec)
    if memory_id and (previous_spec is None or memory_id != spec_memory_id(previous_spec)):
        from app.services import memory_ownership

        if memory_id in memory_ownership.hidden_ids(db, workspace, viewer):
            raise AppError(
                "agent.memory_not_managed",
                f"memory '{memory_id}' is not managed by this workspace — pick one from "
                "the Memory resources list (an administrator can adopt an existing one)",
                {"memory_id": memory_id},
                status_code=422,
            )
