"""SQLAlchemy engine / session for the local ledger database."""

from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import NullPool

from app.core.config import DATA_DIR, get_settings


class Base(DeclarativeBase):
    pass


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


def init_db() -> None:
    Base.metadata.create_all(bind=engine)
    _migrate(engine)
    assert_no_schema_drift(engine)


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
