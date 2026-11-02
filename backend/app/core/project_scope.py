"""The project boundary inside the ORM (design §5.2): filter on read, stamp on write.

Three pieces, each failing closed:

* ``ProjectOwned`` / ``ProjectShared`` mixins give a model ``project_id``.
  *Owned* rows always belong to one project; *shared* rows may carry NULL, meaning
  workspace- or platform-level (a system preset agent, a governance change, a
  bootstrap job) — readable from every project.
* A ``do_orm_execute`` listener adds ``project_id = :current`` (shared: ``OR IS NULL``)
  to every ORM SELECT / UPDATE / DELETE while a project is current. The current
  project is a context variable set once per request by the route policy
  (``bind_request_project``); background threads never set it, so a deploy worker or
  the scheduler sees every row exactly as before.
* A ``before_flush`` listener stamps a new row's ``project_id``: from its parent row
  (``__project_parent__``: an eval run from its agent, a message from its
  conversation…), else the current project, else — only when the row's workspace
  is bound to exactly one project — that project. Otherwise the flush fails: an
  owned row with no project would be invisible everywhere, and silently filing it
  into some project would leak it.

``Session.get`` answers from the identity map without SQL, so the listener cannot
see it: ``ProjectSession`` (the app's session class) re-checks what ``get`` returns,
and ``owned_get`` does the same for any other session.

Cross-project shares (design §11.2): a model registered with ``register_shareable``
reads ``project_id = :current OR id IN :shared`` instead — ``shared`` being the row
ids other projects granted the current one (``access.shared_rows``, epoch-cached, read
once per context). Only reads widen: bulk UPDATE / DELETE keep ``project_id =
:current``, and a flush that would change or delete a row the current project sees
only through a share fails with ``share.read_only`` (404, like a missing id).
"""

import logging
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, ClassVar

from sqlalchemy import String, event, inspect, or_, text, true
from sqlalchemy.orm import (
    Mapped,
    ORMExecuteState,
    Session,
    mapped_column,
    object_session,
    with_loader_criteria,
)

logger = logging.getLogger(__name__)

_CURRENT_PROJECT: ContextVar[str | None] = ContextVar("launchpad_current_project", default=None)
# (project id, {table: row ids shared with it}) — read once per context and project
_SHARED_MEMO: ContextVar[tuple[str, Mapping[str, frozenset[str]]] | None] = ContextVar(
    "launchpad_shared_rows", default=None,
)
_LOADING_SHARED: ContextVar[bool] = ContextVar("launchpad_loading_shared", default=False)


class ProjectStampError(RuntimeError):
    """A new project-owned row whose project could not be determined."""


class _ProjectColumn:
    """The column both mixins share. Indexed per table (``ix_<table>_project_id``)."""

    project_id: Mapped[str | None] = mapped_column(String(32), index=True, default=None)
    # (attribute naming the parent row, parent table or tables). A derived row takes
    # its parent's project; None = stamped from the current project.
    __project_parent__: ClassVar[tuple[str, tuple[str, ...]] | None] = None
    # set by `register_shareable` (§11.2): reads also see rows shared with the project
    __project_shareable__: ClassVar[bool] = False
    # a new (non-platform) row of this model is never *stamped* into a workspace
    # `default` project — its writer names the project (agents: services/agent_project)
    # — unless the agent-project switch `agent_allow_default_project` is on
    __project_no_default_stamp__: ClassVar[bool] = False

    def project_platform_row(self) -> bool:
        """True for a row that is platform-level by nature and stays NULL (shared only)."""
        return False


class ProjectOwned(_ProjectColumn):
    """Rows that always belong to exactly one project."""


class ProjectShared(_ProjectColumn):
    """Rows that belong to a project, or to no project (NULL = workspace / platform level)."""


# ── the current project ───────────────────────────────────────────────────────


def current_project() -> str | None:
    return _CURRENT_PROJECT.get()


def set_current_project(project_id: str | None) -> None:
    """Set for the rest of the current context (one request's task)."""
    _CURRENT_PROJECT.set(project_id)
    _SHARED_MEMO.set(None)


@contextmanager
def project_context(project_id: str | None) -> Iterator[None]:
    """Run a block as ``project_id`` (None = unscoped), restoring what was current.

    Background workers enter it with their job's project; a few cross-project
    reads (an admin's fleet view, name-uniqueness checks) enter it with None."""
    token = _CURRENT_PROJECT.set(project_id)
    memo = _SHARED_MEMO.set(None)
    try:
        yield
    finally:
        _SHARED_MEMO.reset(memo)
        _CURRENT_PROJECT.reset(token)


# ── cross-project shares (§11.2) ──────────────────────────────────────────────

_SHAREABLE: dict[str, type] = {}  # table name -> model


def register_shareable(*models: type) -> None:
    """Let reads of these models see rows other projects shared with the current one
    (``resource_shares``). Call at import time, before any query of the model runs: the
    automatic criteria branch on the class flag when a statement is first compiled."""
    for model in models:
        if not issubclass(model, _ProjectColumn):
            raise TypeError(f"{model.__name__} has no project_id to share across")
        model.__project_shareable__ = True
        _SHAREABLE[model.__tablename__] = model


def shareable_models() -> dict[str, type]:
    return dict(_SHAREABLE)


def forget_shared_rows() -> None:
    """Drop this context's memo — after a share change in the same request or test."""
    _SHARED_MEMO.set(None)


def shared_rows(session: Session, pid: str) -> Mapping[str, frozenset[str]]:
    """``{table: row ids}`` other projects shared with ``pid`` (empty when none), read
    from the epoch-checked process cache once per context. Never raises: a failure to
    read the shares reads as no shares (fails closed)."""
    memo = _SHARED_MEMO.get()
    if memo is not None and memo[0] == pid:
        return memo[1]
    if not _SHAREABLE or _LOADING_SHARED.get():
        return {}
    from app.services import access  # the cache lives with the access snapshot

    token = _LOADING_SHARED.set(True)
    try:
        rows = access.shared_rows(session, pid)
    except Exception:  # noqa: BLE001 — e.g. a ledger without the table yet
        logger.debug("shared rows of %s unreadable", pid, exc_info=True)
        rows = {}
    finally:
        _LOADING_SHARED.reset(token)
    _SHARED_MEMO.set((pid, rows))
    return rows


def _shared_criteria(state: ORMExecuteState, pid: str) -> list[Any]:
    """One criterion per shareable model: strict for writes, widened for reads."""
    shared = shared_rows(state.session, pid) if state.is_select else {}
    out = []
    for table, model in _SHAREABLE.items():
        own: Any = model.project_id == pid
        if issubclass(model, ProjectShared):
            own = or_(own, model.project_id.is_(None))
        ids = shared.get(table)
        where = or_(own, model.id.in_(sorted(ids))) if ids else own
        out.append(with_loader_criteria(model, where, include_aliases=True))
    return out


# ── read: the automatic criteria ──────────────────────────────────────────────


@event.listens_for(Session, "do_orm_execute")
def _filter_to_current_project(state: ORMExecuteState) -> None:
    pid = _CURRENT_PROJECT.get()
    if pid is None:
        return
    if not (state.is_select or state.is_update or state.is_delete):
        return
    if state.is_column_load or state.is_relationship_load:
        return  # lazy loads of an already-loaded (already-filtered) row
    # a shareable model's own criterion comes from `_shared_criteria`; the mixin one is
    # a no-op for it (decided per class — the flag is fixed at import, so the compiled
    # statement cache stays correct)
    state.statement = state.statement.options(
        with_loader_criteria(
            ProjectOwned,
            lambda cls: true() if cls.__project_shareable__ else cls.project_id == pid,
            include_aliases=True,
        ),
        with_loader_criteria(
            ProjectShared,
            lambda cls: true() if cls.__project_shareable__ else or_(
                cls.project_id == pid, cls.project_id.is_(None)),
            include_aliases=True,
        ),
        *_shared_criteria(state, pid),
    )


def _shared_in(row: Any, pid: str, session: Session | None = None) -> bool:
    """Whether ``row`` (of another project) is shared with ``pid``."""
    if not type(row).__project_shareable__:
        return False
    session = session or object_session(row)
    if session is None:
        return False
    return getattr(row, "id", None) in shared_rows(session, pid).get(row.__tablename__, ())


def visible_in_current_project(row: Any, session: Session | None = None) -> bool:
    """Whether a loaded row may be shown while the current project is current."""
    pid = _CURRENT_PROJECT.get()
    if pid is None or not isinstance(row, _ProjectColumn):
        return True
    if row.project_id == pid:
        return True
    if isinstance(row, ProjectShared) and row.project_id is None:
        return True
    return _shared_in(row, pid, session)


class ProjectSession(Session):
    """The application's session class (``db.SessionLocal``): ``get`` honours the
    current project too. An identity-map hit runs no SQL, so the automatic criteria
    never see it; without this, every ``db.get(<owned model>, id)`` in request code
    would read — and, through parent stamping, write into — another project."""

    def get(self, entity: Any, ident: Any, **kwargs: Any) -> Any:
        row = super().get(entity, ident, **kwargs)
        if row is not None and not visible_in_current_project(row, self):
            return None
        return row


def owned_get[ModelT](db: Session, model: type[ModelT], ident: Any) -> ModelT | None:
    """``db.get`` that honours the current project on any session (``ProjectSession``
    already does). A row of another project reads as missing."""
    row = db.get(model, ident)
    if row is None or not visible_in_current_project(row, db):
        return None
    return row


# ── write: stamping ───────────────────────────────────────────────────────────


_NO_PARENT = object()


def _parent_project(session: Session, obj: _ProjectColumn) -> Any:
    """The parent row's project_id (None for a platform parent), or ``_NO_PARENT``."""
    spec = type(obj).__project_parent__
    if spec is None:
        return _NO_PARENT
    attr, tables = spec
    parent_id = getattr(obj, attr, None)
    if not parent_id:
        return _NO_PARENT
    # a parent created in this same flush is not in the database yet
    for pending in session.new:
        if (
            isinstance(pending, _ProjectColumn)
            and getattr(pending, "__tablename__", None) in tables
            and getattr(pending, "id", None) == parent_id
        ):
            return pending.project_id
    conn = session.connection()
    for table in tables:
        found = conn.execute(
            text(f"SELECT project_id FROM {table} WHERE id = :id"),  # noqa: S608 — fixed names
            {"id": parent_id},
        ).first()
        if found is not None:
            return found[0]
    return _NO_PARENT


def _sole_workspace_project(session: Session, workspace_id: str | None) -> str | None:
    """The project a workspace's row belongs to when there is no other candidate, only
    where that is unambiguous: the workspace's one bound project, or — for a workspace
    bound to none yet (registered by an older build, or only named by the row) — the
    platform's `default` project, which every workspace starts with."""
    if not workspace_id:
        # a row with no workspace already fails the workspace invariant at startup
        # (db.assert_every_row_has_a_workspace); file it like an unbound workspace's
        return _default_project(session)
    rows = session.connection().execute(
        text("SELECT project_id FROM project_workspaces WHERE workspace_id = :ws"),
        {"ws": workspace_id},
    ).fetchall()
    if len(rows) == 1:
        return rows[0][0]
    if not rows:
        return _default_project(session)
    return None


def _default_project(session: Session) -> str | None:
    found = session.connection().execute(
        text("SELECT id FROM projects WHERE id = 'default'")
    ).first()
    return found[0] if found else None


def _default_stamp_allowed() -> bool:
    """The agent-project switch (services/agent_project_settings): on, a
    ``__project_no_default_stamp__`` row may land in a ``default`` project. An attribute
    of the cached Settings (the console override is applied onto it) — no query."""
    from app.core.config import get_settings

    return bool(get_settings().agent_allow_default_project)


def _is_default_project(session: Session, pid: str) -> bool:
    found = session.connection().execute(
        text("SELECT is_default FROM projects WHERE id = :id"), {"id": pid},
    ).first()
    return pid == "default" or bool(found and found[0])


def _refuse_shared_writes(session: Session, pid: str) -> None:
    """A share never grants a write (§11.2): changing or deleting a row the current
    project reaches only through one fails as if the row were missing."""
    for obj in (*session.dirty, *session.deleted):
        if not (isinstance(obj, _ProjectColumn) and type(obj).__project_shareable__):
            continue
        history = inspect(obj).attrs.project_id.history
        owner = (history.deleted or history.unchanged or [obj.project_id])[0]
        if owner == pid or owner is None:
            continue
        if obj in session.dirty and not session.is_modified(obj):
            continue
        if not _shared_in(obj, pid, session):
            continue  # loaded outside the project (an admin path); not a share's write
        from app.core.errors import NotFoundError

        raise NotFoundError(
            "share.read_only",
            f"{obj.__tablename__} '{getattr(obj, 'id', '')}' is shared with this project "
            "read-only — only its own project changes it",
            {"id": getattr(obj, "id", None)},
        )


@event.listens_for(Session, "before_flush")
def _stamp_new_rows(session: Session, _flush_context: Any, _instances: Any) -> None:
    pid = _CURRENT_PROJECT.get()
    if pid is not None and _SHAREABLE:
        _refuse_shared_writes(session, pid)
    pending = [
        obj for obj in session.new
        if isinstance(obj, _ProjectColumn) and not obj.project_id
    ]
    if not pending:
        return
    with session.no_autoflush:
        for obj in pending:
            if isinstance(obj, ProjectShared) and obj.project_platform_row():
                continue
            parent = _parent_project(session, obj)
            if parent is None and isinstance(obj, ProjectShared):
                continue  # a platform parent (a system preset): the child is platform too
            pid = (
                (parent if parent is not _NO_PARENT else None)
                or _CURRENT_PROJECT.get()
                or _sole_workspace_project(session, getattr(obj, "workspace_id", None))
            )
            guarded = type(obj).__project_no_default_stamp__ and not _default_stamp_allowed()
            if pid and guarded and _is_default_project(session, pid):
                raise ProjectStampError(
                    f"new {type(obj).__name__} would be filed under the default project "
                    f"'{pid}' — set its project_id to a business project explicitly"
                )
            if pid:
                obj.project_id = pid
            elif isinstance(obj, ProjectOwned):
                raise ProjectStampError(
                    f"new {type(obj).__name__} has no project: no parent row carries one, "
                    "no project is current, and its workspace is not bound to exactly one — "
                    "run the write inside project_context(<project id>)"
                )
