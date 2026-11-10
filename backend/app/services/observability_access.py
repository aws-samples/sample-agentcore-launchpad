"""Member telemetry needs workspace provenance, in addition to owner visibility.

The AWS sources are account-wide. A caller-supplied agent id is a transcript hint,
never evidence that an arbitrary session belongs to that agent.
"""

import math
from collections.abc import Callable
from functools import cached_property
from typing import Any

from sqlalchemy.orm import Session

from app.core.errors import NotFoundError
from app.evaluation.models import EvalRun
from app.models.assistant import AssistantMessage
from app.models.ledger import Agent, ChatSession
from app.models.policy import PolicyRuntimeSession
from app.services import observability, runtime_sessions, visibility
from app.services.visibility import Viewer
from app.services.workspace import WorkspaceContext


class TelemetryAccess:
    def __init__(self, db: Session, workspace: WorkspaceContext, viewer: Viewer, *, admin: bool,
                 denied_sessions: frozenset[str] = frozenset()):
        self.db, self.workspace, self.viewer, self.admin = db, workspace, viewer, admin
        self.denied_sessions = denied_sessions

    def records_guard(self, *, session_ids: frozenset[str] | None = None
                      ) -> Callable[[str, list[dict[str, Any]]], None] | None:
        """Freeze authorization before readers fan out; no DB/session is captured."""
        if self.admin:
            return None
        allowed = frozenset(name for name in self.services if self.service(name))
        blocked = self.denied_sessions | frozenset(
            sid for sid, visible in self.replayed_sessions.items() if not visible
        )

        def guard(session_id: str, records: list[dict[str, Any]]) -> None:
            if session_id in blocked or (session_ids is not None and session_id not in session_ids):
                raise NotFoundError("observability.session_not_found", "session not found")
            for record in records:
                if not isinstance(record, dict):
                    raise NotFoundError("observability.session_not_found", "session not found")
                resource = record.get("resource")
                resource = resource if isinstance(resource, dict) else {}
                attrs = resource.get("attributes")
                attrs = attrs if isinstance(attrs, dict) else resource
                service = _attribute(attrs, "service", "name")
                sessions = observability.record_session_ids(record)
                if (not isinstance(service, str) or service.split(".", 1)[0] not in allowed
                        or sessions != {session_id}):
                    raise NotFoundError("observability.session_not_found", "session not found")

        return guard

    @cached_property
    def services(self) -> dict[str, list[Agent]]:
        """Exact SDK service bases only; display-name suffix matching is not authorization."""
        out = {}
        for agent in self.db.query(Agent).filter(Agent.workspace_id == self.workspace.id):
            if agent.resource_id:
                base = agent.resource_id.rsplit("-", 1)[0]
                for name in (base, f"harness_{base}", agent.resource_id):
                    out.setdefault(name, []).append(agent)
        return out

    def service(self, name: str | None) -> bool:
        agents = self.services.get(name.split(".", 1)[0]) if isinstance(name, str) else None
        return bool(agents) and all(
            visibility.sees(self.viewer, self.db, agent.created_by) for agent in agents or []
        )

    @cached_property
    def ledger_sessions(self) -> set[str]:
        ws = self.workspace.id
        sessions = {sid for (sid,) in self.db.query(ChatSession.session_id).filter(
            ChatSession.workspace_id == ws)}
        sessions.update(sid for (sid,) in self.db.query(AssistantMessage.runtime_session_id)
                        .filter(AssistantMessage.workspace_id == ws))
        rows = self.db.query(PolicyRuntimeSession.session_id,
                             PolicyRuntimeSession.runtime_session_id).join(
            Agent, Agent.id == PolicyRuntimeSession.agent_id).filter(Agent.workspace_id == ws)
        sessions.update(sid for row in rows for sid in row)
        return {sid for sid in sessions if sid}

    def known_session(self, session_id: str | None) -> bool:
        if not session_id:
            return False
        if session_id in self.replayed_sessions:
            return self.replayed_sessions[session_id]
        return session_id in self.ledger_sessions

    @cached_property
    def replayed_sessions(self) -> dict[str, bool]:
        # A bounded number of ledger queries per request, not one JSON scan per trace.
        out = {}
        rows = self.db.query(EvalRun).filter(EvalRun.workspace_id == self.workspace.id).order_by(
            EvalRun.created_at.asc())
        for run in rows:
            readable = visibility.sees(self.viewer, self.db, run.created_by)
            out.update({sid: readable for sid in run.scenario_map or {}})
        return out

    def row(self, row: dict[str, Any]) -> bool:
        if self.admin:
            return True
        # A latest(service) aggregate cannot prove every contributor is local.
        if float(row.get("service_count") or 0) > 1:
            return False
        known = self.known_session(row.get("session_id"))
        if self.replayed_sessions.get(row.get("session_id")) is False:
            return False
        if row.get("service"):
            return self.service(row["service"])
        return known

    def trace(self, payload: dict[str, Any]) -> bool:
        if self.admin:
            return True
        meta = payload.get("meta") or {}
        if meta.get("provenance_complete") is False:
            return False
        services = meta.get("services") or ([meta["service"]] if meta.get("service") else [])
        sessions = meta.get("session_ids") or []
        if any(self.replayed_sessions.get(sid) is False for sid in sessions):
            return False
        if any(not self.service(name) for name in services):
            return False
        if len(sessions) > 1 and any(not self.known_session(sid) for sid in sessions):
            return False
        return self.row(meta) or (
            not services and bool(sessions) and all(self.known_session(sid) for sid in sessions)
        )

    def trace_row(self, row: dict[str, Any]) -> dict[str, Any]:
        """Authorize root enrichment independently, without mutating the shared cache."""
        result = dict(row)
        root = result.pop("_root_enrichment", None)
        if not self.admin and root is not None:
            allowed = self.service(root.get("service")) and all(
                sid not in self.denied_sessions and self.replayed_sessions.get(sid) is not False
                for sid in root.get("session_ids") or []
            )
            if not allowed:
                result.update(root_operation="trace", duration_ms=root["aggregate_duration_ms"])
        return result

    def session(self, payload: dict[str, Any]) -> bool:
        if self.admin:
            return True
        rows = payload.get("traces") or []
        return all(self.row(row) for row in rows) and (
            bool(rows) or self.known_session(payload.get("session_id"))
        )

    def require_session(self, session_id: str, range_key: str = "7d") -> None:
        """Unknown external sessions require fresh metadata from a local runtime.

        No transcript/prompt data is fetched here. Existing ledger sessions need no
        AWS call. Seven days matches the longest observability range; admins retain
        account-level diagnostics for older/unattributed sessions.
        """
        if self.admin or self.known_session(session_id):
            return
        self.require_remote_session(session_id, observability.RANGE_HOURS[range_key])

    def require_remote_session(self, session_id: str, hours: int) -> None:
        """Authorize the complete remote source, even when a local id collides.

        A local ledger row proves only local transcript ownership. It cannot
        authorize an account-level aggregate or an evaluation engine's source.
        """
        if self.admin:
            return
        if self.replayed_sessions.get(session_id) is False:
            raise NotFoundError("observability.session_not_found", "session not found")
        self._require_source(observability.q_session_provenance(session_id), hours, "session")

    def require_remote_trace(self, trace_id: str, hours: int) -> None:
        if not self.admin:
            self._require_source(observability.q_trace_provenance(trace_id), hours, "trace")

    def require_remote_conversation(self, session_id: str, hours: int, *,
                                    source_ids: list[str] | None = None) -> None:
        """Check all sources the conversation reader consumes, not only its display id.

        An empty original/derived id is normal when all turns ran under another id.
        Every nonempty source must independently pass the existing provenance rule;
        one visible source never excuses a foreign or unattributed contributor.
        Single-session evaluation readers retain their exact-id authorization.
        """
        if self.admin:
            return
        sources = source_ids if source_ids is not None else runtime_sessions.source_session_ids(
            self.db, self.workspace.id, session_id, self.viewer)
        if any(sid in self.denied_sessions or self.replayed_sessions.get(sid) is False
               for sid in sources):
            raise NotFoundError("observability.session_not_found", "session not found")
        queries = {
            ("ownership" if index == 0 else f"ownership{index}"):
                observability.q_session_provenance(sid)
            for index, sid in enumerate(sources)
        }
        results = observability.run_insights_queries(queries, hours, workspace=self.workspace)
        found = False
        for key in queries:
            rows = results[key]
            if not rows:
                continue
            if not self._readable_source(rows):
                raise NotFoundError("observability.session_not_found", "session not found")
            found = True
        if not found:
            raise NotFoundError("observability.session_not_found", "session not found")

    def _require_source(self, query: str, hours: int, kind: str) -> None:
        rows = observability.run_insights_queries(
            {"ownership": query}, hours, workspace=self.workspace,
        )["ownership"]
        if not self._readable_source(rows):
            raise NotFoundError(f"observability.{kind}_not_found", f"{kind} not found")

    def _readable_source(self, rows: list[dict[str, Any]]) -> bool:
        if len(rows) == 1:
            row = rows[0]
            try:
                total, attributed, services = (
                    float(row.get(key) or 0)
                    for key in ("record_count", "attributed_count", "service_count")
                )
            except (TypeError, ValueError):
                total, attributed, services = 0.0, 0.0, 0.0
            if (math.isfinite(total) and total.is_integer() and total > 0
                    and attributed == total and services == 1
                    and self.service(row.get("service"))):
                return True
        return False


def _attribute(attrs: dict[str, Any], parent: str, name: str) -> Any:
    value = attrs.get(f"{parent}.{name}")
    nested = attrs.get(parent)
    return value if value is not None else nested.get(name) if isinstance(nested, dict) else None
