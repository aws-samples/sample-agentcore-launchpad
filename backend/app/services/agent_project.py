"""The business project a new agent is filed under (agent-project rule).

Every non-system agent creation path — ``POST /api/agents``, a Harness → Runtime
conversion, a discovery import, the architect's approve, a promotion that creates its
target — files the new row under one *business* project: active, not a workspace
``default`` project, carrying a department, and bound to the agent's workspace.
System presets stay platform rows (``project_id`` NULL) and never come here; agents
already filed under ``default`` keep it.

One administrator switch loosens it (``services/agent_project_settings``, off by
default): with ``agent_allow_default_project`` on, a workspace ``default`` project is
eligible too and needs no department there (it never carries one); every other project
keeps the full rule. Such agents belong to no department — department budgets and
department administrators do not see them.

* ``check_agent_project`` — the row-level rule only (no caller): the promotion
  worker uses it for the target workspace.
* ``resolve_agent_project`` — a request's project for a new agent: the body's
  ``project_id`` when given (it must equal an ``X-Project`` header sent alongside),
  else the request's resolved project. A body project other than the resolved one is
  resolved for the caller (membership, like the header) and becomes the request's
  project for the rest of the handler, so quotas, pins and the ORM stamp all see it.
  Then ``check_agent_project`` and the route's own permission key in that project.

Every refusal is a 422 with a stable ``agent.project_*`` code and the projects page
as its next step.
"""

from typing import Any

from fastapi import Request
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.errors import AppError, NotFoundError, next_step
from app.core.project_scope import project_context, set_current_project
from app.services import agent_project_settings

PROJECT_REQUIRED = "agent.project_required"
PROJECT_NOT_FOUND = "agent.project_not_found"
PROJECT_MISMATCH = "agent.project_mismatch"
PROJECT_DEFAULT = "agent.project_default_not_allowed"
PROJECT_ARCHIVED = "agent.project_archived"
PROJECT_NO_DEPARTMENT = "agent.project_no_department"


def _refuse(code: str, message: str, project_id: str | None) -> AppError:
    return AppError(
        code, message, {"project_id": project_id}, status_code=422,
        next=[next_step("projects.open")],
    )


def _required(message: str) -> AppError:
    return AppError(PROJECT_REQUIRED, message, None, status_code=422,
                    next=[next_step("projects.open")])


def check_agent_project(db: Session, project_id: str | None, workspace_id: str | None) -> str:
    """The project a new agent may be filed under, or a 422. No caller check."""
    pid = (project_id or "").strip()
    if not pid:
        raise _required("a new agent belongs to a business project — name its project_id")
    with project_context(None):  # projects / bindings are not project-owned anyway
        row = db.execute(
            text(
                "SELECT p.id, p.status, p.is_default, p.department_id FROM projects p"
                " JOIN project_workspaces pw ON pw.project_id = p.id"
                " WHERE p.id = :id AND pw.workspace_id = :ws"
            ),
            {"id": pid, "ws": workspace_id or ""},
        ).first()
    if row is None:
        raise _refuse(PROJECT_NOT_FOUND,
                      f"project '{pid}' is not bound to workspace '{workspace_id}'", pid)
    is_default = bool(row[2]) or row[0] == "default"
    if is_default and not agent_project_settings.allowed():
        raise _refuse(PROJECT_DEFAULT,
                      "new agents cannot be created in the workspace default project — "
                      "pick a business project", pid)
    if row[1] != "active":
        raise _refuse(PROJECT_ARCHIVED, f"project '{pid}' is archived", pid)
    if not row[3] and not is_default:  # a default project has no department by design
        raise _refuse(PROJECT_NO_DEPARTMENT,
                      f"project '{pid}' belongs to no department — assign one first", pid)
    return pid


def resolve_agent_project(
    request: Request,
    db: Session,
    workspace_id: str,
    explicit: str | None,
    permission: str,
) -> str:
    """The request's project for a new agent (module docstring), validated."""
    from app.routers.auth import require_identity, require_permission
    from app.services import access

    wanted = (explicit or "").strip() or None
    header = (request.headers.get(access.PROJECT_HEADER) or "").strip() or None
    if wanted and header and wanted != header:
        raise _refuse(
            PROJECT_MISMATCH,
            f"project_id '{wanted}' does not match the {access.PROJECT_HEADER} header "
            f"'{header}'", wanted,
        )
    scope: Any = getattr(request.state, "project", None)
    if wanted and (scope is None or scope.id != wanted):
        identity = require_identity(request)
        try:
            with project_context(None):
                # read semantics: an archived project answers with its own code below
                scope = access.resolve_project(db, identity, workspace_id, wanted, "GET")
        except NotFoundError as exc:
            raise _refuse(PROJECT_NOT_FOUND, f"project '{wanted}' not found", wanted) from exc
        request.state.project = scope
        set_current_project(scope.id)
    if scope is None:
        raise _required("a new agent belongs to a business project — name its project_id")
    pid = check_agent_project(db, scope.id, workspace_id)
    # the route's key, in the project the agent lands in
    require_permission(request, permission)
    return pid


# the route keys that create an agent: the picker lists projects holding the one asked for
CREATE_PERMISSIONS = ("agents.deploy", "agents.import", "agents.convert")


def eligible_projects(
    db: Session, identity: Any, workspace_id: str, permission: str = "agents.deploy",
) -> list[dict[str, Any]]:
    """The projects of ``workspace_id`` the caller may create an agent in: active,
    bound to the workspace, a business project (not ``default``, with a department) —
    or, with the ``agent_allow_default_project`` switch on, a ``default`` project too —
    and, unless the caller has the administrator's data bypass, a membership whose
    effective keys hold ``permission`` (an ``inherit`` member's are its account's
    global keys). A project the caller does not reach is never listed."""
    from app.services import access

    with project_context(None):
        rows = db.execute(
            text(
                "SELECT p.id, p.name, p.department_id, d.name, pw.is_workspace_default,"
                " (p.is_default != 0 OR p.id = 'default') AS is_default"
                " FROM projects p"
                " JOIN project_workspaces pw ON pw.project_id = p.id"
                " LEFT JOIN departments d ON d.id = p.department_id"
                " WHERE pw.workspace_id = :ws AND p.status = 'active' AND ("
                "  (p.is_default = 0 AND p.id != 'default' AND p.department_id IS NOT NULL)"
                "  OR (:allow_default AND (p.is_default != 0 OR p.id = 'default')))"
                " ORDER BY p.name, p.id"
            ),
            {"ws": workspace_id, "allow_default": agent_project_settings.allowed()},
        ).fetchall()
        if access.has_data_bypass(identity):
            allowed = None
        else:
            snapshot = access.snapshot_for(db, identity)
            allowed = {
                m.project_id for m in snapshot.reachable(workspace_id)
                if permission in m.permissions
            }
    return [
        {
            "id": r[0], "name": r[1], "department_id": r[2], "department_name": r[3],
            "is_workspace_default": bool(r[4]), "is_default": bool(r[5]),
        }
        for r in rows
        if allowed is None or r[0] in allowed
    ]
