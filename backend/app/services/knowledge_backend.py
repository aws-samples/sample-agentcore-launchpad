"""Knowledge-base feature boundary (N6 implements on S3 + OpenSearch).

Mirrors the former `services/knowledge.py` public surface; the former KB connector
gateway module (AgentCore KB gateway) has no CN equivalent and is gone for good — the
`POST /api/knowledge-bases/ensure-gateway` route is removed with it.
"""

import logging
from datetime import UTC, datetime
from typing import Any, Protocol

from app.core.errors import AppError
from app.core.features import feature_unavailable
from app.models.ledger import Agent, Deployment, Job
from app.services.workspace import WorkspaceContext


class KBAttachedError(AppError):
    """A KB with agents mounted was deleted without ``force`` — 409 carrying the
    blocking agent names in ``detail.agents`` (same envelope shape as agents.py)."""

    def __init__(self, kb_id: str, agents: list[str]):
        super().__init__(
            "kb.has_attached_agents",
            f"knowledge base is attached to {len(agents)} agent(s); "
            "pass force=true to delete anyway",
            detail={"agents": agents},
            status_code=409,
        )
        self.agents = agents


class KnowledgeBackend(Protocol):
    """What a knowledge-base backend must provide (routers/knowledge.py + deployer)."""

    def list_kbs(self, workspace: WorkspaceContext) -> list[dict[str, Any]]:
        """``GET /api/knowledge-bases`` — ``[{kb_id, name, description, status, …}]``;
        also the assistant catalog and the Create-Agent picker (``?status=ACTIVE``)."""
        ...

    def get_kb_detail(self, workspace: WorkspaceContext, kb_id: str) -> dict[str, Any]:
        """``GET /api/knowledge-bases/{kb_id}`` — detail incl. data sources and recent jobs;
        also the system-preset install's KB verification."""
        ...

    def create_kb(
        self, workspace: WorkspaceContext, name: str, description: str, source: dict[str, Any],
        *, creator: Any = None,
    ) -> dict[str, Any]:
        """``POST /api/knowledge-bases`` (202) — ``source`` is ``{mode, bucket, prefix}``;
        ``creator`` (``services.creators.Creator`` | None) is stamped on the KB's ledger
        row and echoed as ``created_by`` / ``created_by_name`` on every list / detail."""
        ...

    def update_description(
        self, workspace: WorkspaceContext, kb_id: str, description: str,
    ) -> dict[str, Any]:
        """``PATCH /api/knowledge-bases/{kb_id}``."""
        ...

    def delete_kb(
        self, workspace: WorkspaceContext, kb_id: str, force: bool = False,
    ) -> dict[str, Any]:
        """``DELETE /api/knowledge-bases/{kb_id}`` — raises ``KBAttachedError`` (409) when
        agents still mount the KB and ``force`` is false."""
        ...

    def upload_files(
        self, workspace: WorkspaceContext, kb_id: str, files: list[tuple[str, bytes]],
    ) -> dict[str, Any]:
        """``POST /api/knowledge-bases/{kb_id}/files`` — ``{"keys": [...], "job": {...} | None}``:
        the stored object keys, plus the ingestion sync a successful upload starts on the
        upload-mode data source (N17-obs204). ``job`` is ``{job_id, status, coalesced,
        covers_upload}``: ``coalesced`` when a job of that source was already running and no
        second one was enqueued; ``covers_upload: false`` when that running job may have
        listed the prefix before these puts landed (a sync started before this upload may not
        cover it; the next upload or a manual sync does). ``None`` when the upload succeeded
        but the sync could not be started — the caller syncs by hand.
        `sync_error` (exception type only) accompanies `job: None` when the sync step failed.
        """
        ...

    def add_data_source(
        self, workspace: WorkspaceContext, kb_id: str, source: dict[str, Any],
    ) -> dict[str, Any]:
        """``POST /api/knowledge-bases/{kb_id}/data-sources`` — the source plus ``created``:
        201 ``created: true`` for a new one; mode ``upload`` is idempotent per KB, so a KB
        that already has an upload-mode source answers 200 with it and ``created: false``
        (N17-obs243). Mode ``existing`` always appends."""
        ...

    def delete_data_source(
        self, workspace: WorkspaceContext, kb_id: str, ds_id: str,
    ) -> dict[str, Any]:
        """``DELETE /api/knowledge-bases/{kb_id}/data-sources/{ds_id}``."""
        ...

    def start_sync(
        self, workspace: WorkspaceContext, kb_id: str, ds_id: str, *, full: bool = False,
    ) -> dict[str, Any]:
        """``POST /api/knowledge-bases/{kb_id}/data-sources/{ds_id}/sync``. ``full`` (body
        ``{"full": true}``, N17-obs272) forces a complete rebuild — every document re-chunked
        and re-embedded; the default sync is incremental (N17-obs246)."""
        ...

    def list_ingestion_jobs(
        self, workspace: WorkspaceContext, kb_id: str, ds_id: str,
    ) -> list[dict[str, Any]]:
        """``GET /api/knowledge-bases/{kb_id}/data-sources/{ds_id}/ingestion-jobs``."""
        ...

    def list_documents(
        self, workspace: WorkspaceContext, kb_id: str, ds_id: str, *,
        page_size: int = 50, token: str | None = None,
    ) -> dict[str, Any]:
        """``GET /api/knowledge-bases/{kb_id}/data-sources/{ds_id}/documents`` — a page
        ``{items, next_token}``."""
        ...

    def query(
        self, workspace: WorkspaceContext, kb_id: str, text: str, number_of_results: int = 8,
    ) -> list[dict[str, Any]]:
        """``POST /api/knowledge-bases/{kb_id}/query`` — the retrieval playground."""
        ...

    # ledger-side guards the deployer and uninstall use
    def attached_agents(self, workspace: WorkspaceContext, kb_id: str) -> list[str]:
        """Names of live agents whose spec mounts ``kb_id`` (delete guard)."""
        ...

    def refuse_if_attached_to_system_agent(self, workspace: WorkspaceContext, kb_id: str) -> None:
        """Raise when a system-managed preset mounts ``kb_id`` (system_agents/uninstall)."""
        ...


_backend: KnowledgeBackend | None = None


logger = logging.getLogger(__name__)


def set_backend(backend: KnowledgeBackend | None) -> None:
    global _backend
    _backend = backend


def backend_or_none() -> KnowledgeBackend | None:
    return _backend


def get_backend() -> KnowledgeBackend:
    if _backend is None:
        raise feature_unavailable("knowledge_bases")
    return _backend


# ── ledger-side guards (no backend needed; N1 contract note) ───────────────
def _mounting(rows, kb_id: str) -> list[str]:
    return sorted(
        a.name for a in rows
        if any(k.get("kb_id") == kb_id for k in ((a.spec or {}).get("knowledge_bases") or []))
    )


def attached_agent_names(db, workspace: WorkspaceContext, kb_id: str) -> list[str]:
    """Live agents of this workspace whose spec mounts ``kb_id``, by name."""
    rows = (
        db.query(Agent)
        .filter(Agent.workspace_id == workspace.id, Agent.status != "deleted")
        .all()
    )
    return _mounting(rows, kb_id)


def mounting_agents(db, workspace: WorkspaceContext, kb_id: str) -> list[Agent]:
    """Every live agent of the workspace whose spec mounts ``kb_id`` — hidden-by-visibility
    and `deploying` rows included (the ledger, not a caller's view), oldest first."""
    rows = (
        db.query(Agent)
        .filter(Agent.workspace_id == workspace.id, Agent.status != "deleted")
        .order_by(Agent.created_at.asc())
        .all()
    )
    return [
        a for a in rows
        if any(k.get("kb_id") == kb_id for k in ((a.spec or {}).get("knowledge_bases") or []))
    ]


def strip_kb_from_agents(
    db, workspace: WorkspaceContext, kb_id: str, *, by: str | None = None,
) -> list[dict[str, Any]]:
    """The promise behind ``DELETE /api/knowledge-bases/{kb_id}?force=true`` (N17-obs31 =
    N16-obs34): in ONE transaction drop the KB from EVERY mounting agent's
    ``spec.knowledge_bases`` — hidden and visible alike — and mark each agent
    ``last_strip`` (``kb_stale`` on its projection: the deployed Runtime keeps its retrieval
    tool until the next re-publish, which clears the mark). Shared by every backend: the
    router calls it before the backend's ``delete_kb``, so the ledger never keeps a mount on
    a gone KB, and a storage failure afterwards leaves at worst a KB with no mounts.

    A mounting agent whose deploy job is live (``queued`` / ``running``, or a ``deploying``
    row) refuses the whole force with ``409 agent.deploy_in_progress {kb_id, agents:
    [{id, name, job_id, stage, job_status}]}`` BEFORE anything is written — the job owns the
    agent (agents.md §1), and a strip racing a package stage would ship a spec the ledger no
    longer has. A strip is not a publish: no spec snapshot; the agent's latest deploy job
    log records the event instead — AFTER the one commit, best-effort (a log failure neither
    splits the strip nor fails the delete). Returns ``[{id, name}]`` of the stripped agents."""
    from app.deployer.pipeline import _append_log, in_flight_deploy_job

    agents = mounting_agents(db, workspace, kb_id)
    busy: list[dict[str, Any]] = []
    for agent in agents:
        live = in_flight_deploy_job(db, agent.id)
        if live is not None or agent.status == "deploying":
            job, _dep, stage = live if live is not None else (None, None, None)
            busy.append({"id": agent.id, "name": agent.name,
                         "job_id": job.id if job else None, "stage": stage,
                         "job_status": job.status if job else agent.status})
    if busy:
        raise AppError(
            "agent.deploy_in_progress",
            f"{len(busy)} agent(s) mounting this knowledge base have a deployment in progress "
            "— wait for the job(s) to finish or fail, then force the delete",
            {"kb_id": kb_id, "agents": busy},
            status_code=409,
        )
    now = datetime.now(UTC).isoformat()
    stripped: list[dict[str, Any]] = []
    for agent in agents:
        spec = dict(agent.spec or {})
        spec["knowledge_bases"] = [
            k for k in (spec.get("knowledge_bases") or []) if k.get("kb_id") != kb_id
        ]
        agent.spec = spec
        previous = list((agent.last_strip or {}).get("kb_ids") or [])
        agent.last_strip = {"kb_ids": sorted({*previous, kb_id}), "at": now, "by": by}
        agent.updated_at = datetime.now(UTC)
        stripped.append({"id": agent.id, "name": agent.name})
    # ONE commit for every agent (rev-r17 on f30450e6: `_append_log` commits per call, so a
    # log line inside the loop made a partial strip possible). The job-log lines are
    # informational and follow the commit, best-effort: a log failure never undoes or
    # splits the strip and never fails the delete.
    db.commit()
    for agent in agents:
        latest = (
            db.query(Job)
            .join(Deployment, Deployment.job_id == Job.id)
            .filter(Deployment.agent_id == agent.id)
            .order_by(Job.created_at.desc())
            .first()
        )
        if latest is None:
            continue
        try:
            _append_log(db, latest.id, "ledger",
                        f"knowledge base {kb_id} stripped from the spec by a forced delete"
                        f"{f' ({by})' if by else ''} — re-publish to drop its retrieval tool")
        except Exception:  # noqa: BLE001 — informational; the strip is already committed
            logger.warning("knowledge: could not log the KB %s strip on job %s of agent %s",
                           kb_id, latest.id, agent.id, exc_info=True)
            db.rollback()
    return stripped


def refuse_if_attached_to_system_agent(db, workspace: WorkspaceContext, kb_id: str) -> None:
    rows = (
        db.query(Agent)
        .filter(
            Agent.workspace_id == workspace.id, Agent.status != "deleted",
            Agent.system_key.isnot(None),
        )
        .all()
    )
    names = _mounting(rows, kb_id)
    if names:
        raise AppError(
            "kb.attached_to_system_agent",
            f"knowledge base is mounted by system-managed preset(s) "
            f"{', '.join(names)} — uninstall them first",
            detail={"agents": names}, status_code=409,
        )
