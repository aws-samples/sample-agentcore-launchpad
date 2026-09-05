"""SQLAlchemy engine / session for the local ledger database."""

import json
from collections.abc import Generator
from datetime import UTC, datetime

from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import NullPool

from app.core.config import DATA_DIR, get_settings


class Base(DeclarativeBase):
    pass


DEFAULT_WORKSPACE_ID = "default"

# Every table whose rows belong to one (account, region) environment. `users`,
# `workspaces` and `user_workspaces` are hub-global and stay off this list.
WORKSPACE_SCOPED_TABLES = (
    "agents",
    "deployments",
    "chat_sessions",
    "chat_messages",
    "api_keys",
    "policy_decisions",
    "policy_changes",
    "jobs",
    "eval_datasets",
    "eval_runs",
    "online_eval_configs",
    "experiments",
    "runtime_canaries",
    "skill_lab_tasksets",
    "skill_lab_jobs",
)


def _make_engine():
    settings = get_settings()
    url = make_url(settings.database_url)
    is_sqlite = url.get_backend_name() == "sqlite"
    is_file_sqlite = is_sqlite and url.database not in (None, "", ":memory:")
    if is_sqlite:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
    if is_file_sqlite:
        return create_engine(
            settings.database_url,
            connect_args={"check_same_thread": False},
            # SQLAlchemy 2 defaults file SQLite databases to QueuePool(5+10). A
            # burst of sync FastAPI requests can consume that fixed pool and
            # block every worker for 30 seconds. SQLite connections are cheap
            # and request sessions already close deterministically, so avoid
            # the artificial cap and close each DBAPI connection per session.
            poolclass=NullPool,
        )
    if is_sqlite:
        return create_engine(
            settings.database_url,
            connect_args={"check_same_thread": False},
        )
    return create_engine(settings.database_url)


engine = _make_engine()
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db(bind=None) -> None:
    """Bring the ledger up to the models and seed the default workspace.

    ``bind`` defaults to the process engine; tests pass an upgrade candidate so
    they exercise this exact sequence rather than a copy of it.
    """
    bind = bind if bind is not None else engine
    Base.metadata.create_all(bind=bind)
    _migrate(bind)
    # Seeding after the drift check so a database that is behind the models is
    # never left half-migrated *and* half-seeded.
    assert_no_schema_drift(bind)
    _seed_default_workspace(bind)
    assert_every_row_has_a_workspace(bind)


def schema_drift(bind) -> dict[str, list[str]]:
    """Model columns missing from the live database, per table.

    `create_all` only creates tables that do not exist yet, so a column added to a
    model is invisible to an **existing** ledger until `_migrate` adds it. Forgetting
    that entry is silent in the hermetic suite (every test DB is freshly created) and
    surfaces in production as a 500 on whichever request first selects the column.
    This turns that class of mistake into one legible failure.
    """
    from sqlalchemy import inspect

    inspector = inspect(bind)
    live_tables = set(inspector.get_table_names())
    drift: dict[str, list[str]] = {}
    for name, table in Base.metadata.tables.items():
        if name not in live_tables:
            continue  # create_all handles a table that does not exist at all
        live_columns = {c["name"] for c in inspector.get_columns(name)}
        missing = [c.name for c in table.columns if c.name not in live_columns]
        if missing:
            drift[name] = missing
    return drift


def assert_no_schema_drift(bind) -> None:
    drift = schema_drift(bind)
    if not drift:
        return
    lines = [
        f"  ALTER TABLE {table} ADD COLUMN {column} ...;"
        for table, columns in sorted(drift.items())
        for column in columns
    ]
    raise RuntimeError(
        "ledger schema is behind the models — add the column(s) to `_migrate()` in "
        "app/core/db.py so existing databases are upgraded on startup:\n"
        + "\n".join(lines)
    )


def _migrate(bind) -> None:
    """Additive column migrations for the local SQLite ledger (no Alembic).

    Every column added to a model after its table shipped needs an entry here, or
    `assert_no_schema_drift` fails startup. Keep the statements idempotent.
    """
    from sqlalchemy import inspect, text

    inspector = inspect(bind)
    if "agents" in inspector.get_table_names():
        existing = {c["name"] for c in inspector.get_columns("agents")}
        if "registry_record_id" not in existing:
            with bind.begin() as conn:
                conn.execute(
                    text("ALTER TABLE agents ADD COLUMN registry_record_id VARCHAR(64)")
                )
    if "deployments" in inspector.get_table_names():
        existing = {c["name"] for c in inspector.get_columns("deployments")}
        if "image_digest" not in existing:
            with bind.begin() as conn:
                conn.execute(
                    text("ALTER TABLE deployments ADD COLUMN image_digest VARCHAR(80)")
                )
    if "users" in inspector.get_table_names():
        existing = {c["name"] for c in inspector.get_columns("users")}
        if "permissions" not in existing:
            with bind.begin() as conn:
                conn.execute(text("ALTER TABLE users ADD COLUMN permissions JSON"))
    if "eval_datasets" in inspector.get_table_names():
        existing = {c["name"] for c in inspector.get_columns("eval_datasets")}
        additions = {
            "description": "ALTER TABLE eval_datasets ADD COLUMN description TEXT DEFAULT ''",
            "cloud": "ALTER TABLE eval_datasets ADD COLUMN cloud JSON",
        }
        for column, ddl in additions.items():
            if column not in existing:
                with bind.begin() as conn:
                    conn.execute(text(ddl))
    if "experiments" in inspector.get_table_names():
        existing = {c["name"] for c in inspector.get_columns("experiments")}
        additions = {
            "running_action": "ALTER TABLE experiments ADD COLUMN running_action VARCHAR(24)",
            "progress": "ALTER TABLE experiments ADD COLUMN progress TEXT",
        }
        for column, ddl in additions.items():
            if column not in existing:
                with bind.begin() as conn:
                    conn.execute(text(ddl))
    if "chat_sessions" in inspector.get_table_names():
        existing = {c["name"] for c in inspector.get_columns("chat_sessions")}
        if "ended_at" not in existing:
            with bind.begin() as conn:
                conn.execute(text("ALTER TABLE chat_sessions ADD COLUMN ended_at DATETIME"))
    if "skill_lab_tasksets" in inspector.get_table_names():
        existing = {c["name"] for c in inspector.get_columns("skill_lab_tasksets")}
        if "sample" not in existing:
            with bind.begin() as conn:
                conn.execute(
                    text(
                        "ALTER TABLE skill_lab_tasksets "
                        "ADD COLUMN sample BOOLEAN DEFAULT 0 NOT NULL"
                    )
                )
    _migrate_workspace_columns(bind)


def _migrate_workspace_columns(bind) -> None:
    """Add `workspace_id` (+ its index) to every per-environment table.

    The DDL is spelled out per table rather than generated, because
    `tests/test_ledger_migration.py` reads the (table, column) pairs a migration
    handles off the source of every `_migrate*` function.
    """
    from sqlalchemy import inspect, text

    additions = {
        "agents": "ALTER TABLE agents ADD COLUMN workspace_id VARCHAR(32)",
        "deployments": "ALTER TABLE deployments ADD COLUMN workspace_id VARCHAR(32)",
        "chat_sessions": "ALTER TABLE chat_sessions ADD COLUMN workspace_id VARCHAR(32)",
        "chat_messages": "ALTER TABLE chat_messages ADD COLUMN workspace_id VARCHAR(32)",
        "api_keys": "ALTER TABLE api_keys ADD COLUMN workspace_id VARCHAR(32)",
        "policy_decisions": "ALTER TABLE policy_decisions ADD COLUMN workspace_id VARCHAR(32)",
        "policy_changes": "ALTER TABLE policy_changes ADD COLUMN workspace_id VARCHAR(32)",
        "jobs": "ALTER TABLE jobs ADD COLUMN workspace_id VARCHAR(32)",
        "eval_datasets": "ALTER TABLE eval_datasets ADD COLUMN workspace_id VARCHAR(32)",
        "eval_runs": "ALTER TABLE eval_runs ADD COLUMN workspace_id VARCHAR(32)",
        "online_eval_configs": (
            "ALTER TABLE online_eval_configs ADD COLUMN workspace_id VARCHAR(32)"
        ),
        "experiments": "ALTER TABLE experiments ADD COLUMN workspace_id VARCHAR(32)",
        "runtime_canaries": "ALTER TABLE runtime_canaries ADD COLUMN workspace_id VARCHAR(32)",
        "skill_lab_tasksets": "ALTER TABLE skill_lab_tasksets ADD COLUMN workspace_id VARCHAR(32)",
        "skill_lab_jobs": "ALTER TABLE skill_lab_jobs ADD COLUMN workspace_id VARCHAR(32)",
    }
    inspector = inspect(bind)
    live_tables = set(inspector.get_table_names())
    for table, ddl in additions.items():
        if table not in live_tables:
            continue
        if "workspace_id" not in {c["name"] for c in inspector.get_columns(table)}:
            with bind.begin() as conn:
                conn.execute(text(ddl))
        # `create_all` only builds indexes for tables it creates, so an upgraded
        # table needs its index here; IF NOT EXISTS keeps the fresh-database case
        # (where the model's index=True already applied) a no-op. Note that
        # `schema_drift` compares column names only and cannot catch a missing
        # index — `tests/test_workspaces.py` does.
        with bind.begin() as conn:
            conn.execute(
                text(
                    f"CREATE INDEX IF NOT EXISTS ix_{table}_workspace_id "
                    f"ON {table} (workspace_id)"
                )
            )


def unscoped_row_counts(bind) -> dict[str, int]:
    """Rows in per-environment tables that name no workspace, per table."""
    from sqlalchemy import inspect, text

    inspector = inspect(bind)
    live_tables = set(inspector.get_table_names())
    counts: dict[str, int] = {}
    for table in WORKSPACE_SCOPED_TABLES:
        if table not in live_tables:
            continue
        with bind.begin() as conn:
            found = conn.execute(
                text(f"SELECT COUNT(*) FROM {table} WHERE workspace_id IS NULL")  # noqa: S608
            ).scalar_one()
        if found:
            counts[table] = int(found)
    return counts


def assert_every_row_has_a_workspace(bind) -> None:
    """Fail startup on any row that belongs to no environment.

    The upgrade adopts pre-workspace rows once (see `_seed_default_workspace`);
    after that a NULL means a write path forgot to stamp `workspace_id`, and such a
    row is invisible to every scoped query — it would silently disappear from the
    console instead of erroring. Repeating the adopting UPDATE on every startup
    would paper over exactly that bug, so this refuses to boot instead.
    """
    counts = unscoped_row_counts(bind)
    if not counts:
        return
    listed = ", ".join(f"{table}={count}" for table, count in sorted(counts.items()))
    raise RuntimeError(
        "ledger rows with no workspace_id: "
        f"{listed}. A write path is not stamping the workspace — find the insert "
        "for that table and give it the request's (or the parent row's) workspace."
    )


def _seed_default_workspace(bind) -> None:
    """Mirror settings onto the `default` workspace and adopt pre-P2 rows + users.

    The row is refreshed on every startup rather than seeded once: `make
    bootstrap` and kb_gateway rewrite `launchpad.yaml` *after* the first seed, so
    a frozen snapshot would keep serving a stale identity — a fresh clone would
    pin `account_id=""` forever and the real account would later arrive as a
    *second* workspace beside the bogus default. Settings therefore stays
    authoritative for `default` (and only `default`) until the console owns the
    row; non-default workspaces are row-authoritative from birth.

    Raw SQL on purpose, for two reasons: this module cannot import the models
    (they import `Base` from here), and `PolicyChange`'s `before_update` listener
    rejects ORM updates of frozen audit rows. Idempotent — the mirror converges on
    settings, and the row adoption runs only on the insert (see below).
    """
    from sqlalchemy import inspect, text

    inspector = inspect(bind)
    live_tables = set(inspector.get_table_names())
    if "workspaces" not in live_tables:
        # `create_all` built nothing, which means `Base.metadata` was empty —
        # i.e. the model modules were never imported (the trap that makes an
        # out-of-process migration rehearsal silently skip the whole upgrade).
        raise RuntimeError(
            "`workspaces` is missing after create_all — import app.models.ledger "
            "(as app.main does) before calling init_db"
        )

    settings = get_settings()
    resources = settings.resources or {}
    # An empty resource map means bootstrap has not run (or its output was lost),
    # so the environment is registered but not usable — never claim "ready" for it.
    values = {
        "id": DEFAULT_WORKSPACE_ID,
        "name": "Default",
        "account_id": settings.account_id,
        "region": settings.region,
        "resources": json.dumps(resources),
        "bootstrap_status": "ready" if resources else "registered",
        # SQLAlchemy's SQLite DATETIME format, written directly because there is
        # no type coercion on a raw statement.
        "now": datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S.%f"),
    }
    with bind.begin() as conn:
        exists = conn.execute(
            text("SELECT 1 FROM workspaces WHERE id = :id"), {"id": DEFAULT_WORKSPACE_ID}
        ).first()
        if exists is None:
            conn.execute(
                text(
                    "INSERT INTO workspaces (id, name, account_id, region, role_arn,"
                    " external_id, bootstrap_status, resources, created_at, updated_at)"
                    " VALUES (:id, :name, :account_id, :region, NULL, NULL,"
                    " :bootstrap_status, :resources, :now, :now)"
                ),
                values,
            )
            # Accounts that predate workspaces already reach this environment, so
            # the upgrade grants it to them rather than locking the console.
            # Deliberately only on the insert (the migration moment): repeating it
            # every startup would make a revoked grant come back.
            conn.execute(
                text(
                    "INSERT INTO user_workspaces (user_id, workspace_id, created_at)"
                    " SELECT id, :id, :now FROM users"
                ),
                values,
            )
            # Adopt every pre-workspace row, once. Only on the insert branch: this
            # is the migration moment, and repeating it on every startup would
            # silently absorb rows a *new* write path failed to stamp — the exact
            # bug `assert_every_row_has_a_workspace` exists to surface.
            for table in WORKSPACE_SCOPED_TABLES:
                if table not in live_tables:
                    continue
                conn.execute(
                    text(
                        f"UPDATE {table} SET workspace_id = :id "  # noqa: S608
                        "WHERE workspace_id IS NULL"
                    ),
                    {"id": DEFAULT_WORKSPACE_ID},
                )
        else:
            # name/role_arn/external_id are operator-owned, so the mirror leaves
            # them alone. If a second workspace already claimed this (account,
            # region) while default held a bogus identity, UNIQUE(account_id,
            # region) raises here and startup fails loudly — that conflict needs
            # an operator decision, not a silent winner.
            conn.execute(
                text(
                    "UPDATE workspaces SET account_id = :account_id, region = :region,"
                    " resources = :resources, bootstrap_status = :bootstrap_status,"
                    " updated_at = :now WHERE id = :id"
                ),
                values,
            )
