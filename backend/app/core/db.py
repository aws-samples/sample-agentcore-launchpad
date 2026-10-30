"""SQLAlchemy engine / session for the local ledger database."""

import json
import logging
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime

from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import NullPool

from app.core.config import DATA_DIR, get_settings


class Base(DeclarativeBase):
    pass


DEFAULT_WORKSPACE_ID = "default"
# The largest integer SQLite stores. A client integer above it (an unbounded page offset,
# a revision, a journal id) would reach the driver and raise OverflowError — a 500 — so
# every integer query parameter the console accepts is bounded by it (N12-obs58).
SQLITE_MAX_INT = 2**63 - 1

# Every table whose rows belong to one (account, region) environment. `users`,
# `workspaces`, `user_workspaces`, `announcements` and the video tables (`videos`,
# `video_catalog_seed`) are hub-global.
WORKSPACE_SCOPED_TABLES = (
    "agents",
    "deployments",
    "chat_sessions",
    "chat_messages",
    "chat_turn_events",  # N-ARCH-obs15 v2: per-turn event log of the Chat playground
    "api_keys",
    "jobs",
    "assistant_conversations",
    "assistant_messages",
    "assistant_proposals",
    "assistant_turn_events",  # N-ARCH-obs15: per-turn event log
    "agent_name_claims",
    # --- N12 W-E5: the architect assistant's evaluation-assets plans / operations ---
    "assistant_evaluation_plans",
    "evaluation_asset_operations",
    "system_skill_records",
    "registry_records",
    "managed_memories",
    "identity_providers",
    "user_token_revocations",
    "oauth_pending_sessions",
    "user_grants",
    "resource_creators",  # Family B step 1 (upstream #261): creators of AWS-only kinds
    "spec_snapshots",  # upstream #277 (r15-a): one agent spec per publish
    # --- CN memory/KB (N6) ---
    "memory_stores",
    "knowledge_bases",
    # --- end CN memory/KB (N6) ---
    # --- CN evaluation / optimization (N12) ---
    "eval_datasets",
    "eval_runs",
    "eval_recommendations",
    "eval_pipelines",
    "online_eval_configs",
    "experiments",
    "runtime_canaries",
    "eval_evaluators",
    "eval_results",
    "online_samples",
    "config_bundles",
    "experiment_assignments",
    # --- end CN evaluation / optimization (N12) ---
    # --- Skill Lab (N13) ---
    "skill_lab_tasksets",
    "skill_lab_jobs",
    # --- end Skill Lab (N13) ---
    # --- self-hosted Policy (D20; Global model 2026-10-28) ---
    "policy_engines",
    "policies",
    "policy_versions",
    "policy_settings",
    "policy_changes",
    "policy_decisions",
    "eval_token_files",  # per-user policy D5: an eval / canary job's tokens.json record
    # --- end self-hosted Policy (D20) ---
    # --- model registry (D21): the per-workspace enable flags; the registry itself
    # (`model_providers`, `models`, `model_catalog_changes`) is hub-global ---
    "workspace_model_settings",
    # --- end model registry (D21) ---
)

# ── local Registry search index (N4) ───────────────────────────────────────
# A standalone FTS5 table mirrored from `registry_records` by triggers. The triggers
# deliberately name no `workspace_id`: SQLite refuses to drop a column a trigger
# references, and `tests/test_ledger_migration.py` drops every migrated column.
_REGISTRY_FTS_DDL = (
    "CREATE VIRTUAL TABLE IF NOT EXISTS registry_records_fts USING fts5("
    "record_id UNINDEXED, name, description, search_text, tokenize='unicode61')"
)
_REGISTRY_FTS_TRIGGERS = (
    "CREATE TRIGGER IF NOT EXISTS registry_records_ai AFTER INSERT ON registry_records BEGIN "
    "INSERT INTO registry_records_fts(record_id, name, description, search_text) "
    "VALUES (new.id, new.name, new.description, new.search_text); END",
    "CREATE TRIGGER IF NOT EXISTS registry_records_ad AFTER DELETE ON registry_records BEGIN "
    "DELETE FROM registry_records_fts WHERE record_id = old.id; END",
    "CREATE TRIGGER IF NOT EXISTS registry_records_au AFTER UPDATE OF name, description, "
    "search_text ON registry_records BEGIN "
    "DELETE FROM registry_records_fts WHERE record_id = old.id; "
    "INSERT INTO registry_records_fts(record_id, name, description, search_text) "
    "VALUES (new.id, new.name, new.description, new.search_text); END",
)
_REGISTRY_FTS_BACKFILL = (
    "INSERT INTO registry_records_fts(record_id, name, description, search_text) "
    "SELECT id, name, description, search_text FROM registry_records"
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


# Every module that declares a ``Base`` table. ``create_all`` only knows the models
# that have been imported, and the API process happens to import all of them before
# ``init_db`` (app.main pulls in every router); the bootstrap CLI does not — N11-r5-bug3
# (live, cold host): the registry stage ran ``init_db`` with only ``app.models.ledger``
# loaded, the later Skill Lab stage found the local backend already installed, skipped
# ``init_db`` and seeded into a ledger without ``skill_lab_tasksets``
# (``no such table`` ×3, task sets 0). ``init_db`` therefore imports this list itself;
# ``tests/test_ledger_model_modules.py`` fails when a new ``class X(Base)`` module is
# missing here. Lazy (inside the function): the model modules import ``Base`` from
# this module, so a top-level import would be circular.
MODEL_MODULES: tuple[str, ...] = (
    "app.models.ledger",
    "app.models.assistant",
    "app.models.announcement",
    "app.models.video",
    "app.evaluation.models",
    "app.optimization.models",
    "app.skill_lab.models",
    "app.models.policy",
    "app.models.model_registry",
)


def import_model_modules() -> None:
    """Load every ``Base`` model module so ``Base.metadata`` is complete."""
    import importlib

    for name in MODEL_MODULES:
        importlib.import_module(name)


def init_db(bind=None) -> None:
    """Bring the ledger up to the models and seed the default workspace.

    ``bind`` defaults to the process engine; tests pass an upgrade candidate so
    they exercise this exact sequence rather than a copy of it. The full model set
    is imported here first, so a CLI entry point (scripts/bootstrap.py) that never
    imports the API app still creates every table (N11-r5-bug3).
    """
    bind = bind if bind is not None else engine
    import_model_modules()
    Base.metadata.create_all(bind=bind)
    _migrate(bind)
    # Seeding after the drift check so a database that is behind the models is
    # never left half-migrated *and* half-seeded.
    assert_no_schema_drift(bind)
    _seed_default_workspace(bind)
    # Hub-global video directory: a one-time import of the bundled manifest (empty in the
    # CN build); later restarts never overwrite administrator changes.
    from app.services.videos import seed_legacy_catalog

    seed_legacy_catalog(bind)
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
        if "system_key" not in existing:
            with bind.begin() as conn:
                conn.execute(text("ALTER TABLE agents ADD COLUMN system_key VARCHAR(64)"))
                conn.execute(
                    text("CREATE INDEX IF NOT EXISTS ix_agents_system_key ON agents (system_key)")
                )
        if "attachment_version" not in existing:
            with bind.begin() as conn:
                conn.execute(text("ALTER TABLE agents ADD COLUMN attachment_version VARCHAR(16)"))
        if "last_strip" not in existing:  # N17-obs31
            with bind.begin() as conn:
                conn.execute(text("ALTER TABLE agents ADD COLUMN last_strip JSON"))
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
    for table, column, ddl in (
        ("chat_messages", "attachments", "ALTER TABLE chat_messages ADD COLUMN attachments JSON"),
        # stream A (per-user principal): the workspace half of the interceptor switch
        ("policy_settings", "gateway_interceptor",
         "ALTER TABLE policy_settings ADD COLUMN gateway_interceptor BOOLEAN DEFAULT 0 NOT NULL"),
        ("policy_settings", "engine_publish",
         "ALTER TABLE policy_settings ADD COLUMN engine_publish BOOLEAN DEFAULT 0 NOT NULL"),
        ("policy_settings", "engine_state",
         "ALTER TABLE policy_settings ADD COLUMN engine_state JSON"),
        # stream A5: the engine's installed set versions, per-policy LOG_ONLY, kind / dialect
        ("policy_settings", "policy_set_seq",
         "ALTER TABLE policy_settings ADD COLUMN policy_set_seq INTEGER DEFAULT 0 NOT NULL"),
        ("policy_settings", "temporal_set_version",
         "ALTER TABLE policy_settings ADD COLUMN temporal_set_version INTEGER DEFAULT 0 NOT NULL"),
        ("policy_settings", "temporal_digest",
         "ALTER TABLE policy_settings ADD COLUMN temporal_digest VARCHAR(64) DEFAULT '' NOT NULL"),
        ("policies", "enforcement_mode",
         "ALTER TABLE policies ADD COLUMN enforcement_mode VARCHAR(16) DEFAULT 'active' NOT NULL"),
        # Global model (2026-10-28): a policy belongs to one engine; a gateway decision row
        # names its gateway (both back-filled by services/policy_engines.migrate at startup)
        ("policies", "engine_id", "ALTER TABLE policies ADD COLUMN engine_id VARCHAR(32)"),
        ("policy_decisions", "gateway_id",
         "ALTER TABLE policy_decisions ADD COLUMN gateway_id VARCHAR(128)"),
        ("policy_decisions", "engine_id",
         "ALTER TABLE policy_decisions ADD COLUMN engine_id VARCHAR(32)"),
        ("policy_changes", "engine_id",
         "ALTER TABLE policy_changes ADD COLUMN engine_id VARCHAR(32)"),
        ("policy_versions", "kind",
         "ALTER TABLE policy_versions ADD COLUMN kind VARCHAR(16) DEFAULT 'stateless' NOT NULL"),
        ("policy_versions", "dialect",
         "ALTER TABLE policy_versions ADD COLUMN dialect VARCHAR(16) DEFAULT 'cedar' NOT NULL"),
        # N17-obs482: the hub's ingest time of a gateway decision row (NULL elsewhere)
        ("policy_decisions", "ingested_at",
         "ALTER TABLE policy_decisions ADD COLUMN ingested_at DATETIME"),
        # N-SCN-bug4 (r8-a): the answer bubble's max_tokens marker
        ("chat_messages", "truncated",
         "ALTER TABLE chat_messages ADD COLUMN truncated BOOLEAN DEFAULT 0 NOT NULL"),
        ("chat_sessions", "runtime_version",
         "ALTER TABLE chat_sessions ADD COLUMN runtime_version VARCHAR(16)"),
        # N10v2-obs11: who minted the key (a username then; Family B step 1 re-points it to
        # `users.id` in `_migrate_creator_columns`, which also owns the column now)
        ("api_keys", "created_by", "ALTER TABLE api_keys ADD COLUMN created_by VARCHAR(64)"),
        # N17-obs324: a TOOL_CALL row names the call it judged (trace id stays per row)
        ("eval_results", "span_id", "ALTER TABLE eval_results ADD COLUMN span_id VARCHAR(64)"),
        ("eval_results", "tool_name",
         "ALTER TABLE eval_results ADD COLUMN tool_name VARCHAR(256)"),
        # N17-obs324: the reconciliation status of the turn a row judged
        ("eval_results", "extraction_status",
         "ALTER TABLE eval_results ADD COLUMN extraction_status VARCHAR(16)"),
        # N-SCN-obs4: optional display label (e.g. the agent the key was minted for)
        ("api_keys", "label", "ALTER TABLE api_keys ADD COLUMN label VARCHAR(120)"),
        # N-SCN-obs1: console | public_api — rows that predate the column were all console
        ("chat_sessions", "source",
         "ALTER TABLE chat_sessions ADD COLUMN source VARCHAR(16) DEFAULT 'console'"),
        # N-ARCH-obs15 v2: the console turn running server-side for the session
        ("chat_sessions", "active_turn",
         "ALTER TABLE chat_sessions ADD COLUMN active_turn INTEGER"),
        ("chat_sessions", "active_turn_started_at",
         "ALTER TABLE chat_sessions ADD COLUMN active_turn_started_at DATETIME"),
        ("chat_sessions", "active_turn_token",
         "ALTER TABLE chat_sessions ADD COLUMN active_turn_token VARCHAR(32)"),
        # D20 P1-b: the deploy-time policy narrowing snapshot on the agent row
        ("agents", "policy_applied", "ALTER TABLE agents ADD COLUMN policy_applied JSON"),
        # N17-obs202: whether a system preset's model is an administrator pin (NULL = legacy)
        ("agents", "preset_model_pinned",
         "ALTER TABLE agents ADD COLUMN preset_model_pinned BOOLEAN"),
        # D21 (rev-r8): SSM key version facts added to model_providers after the table
        # first shipped (1182f140) — a ledger created by that build must upgrade cleanly
        ("model_providers", "key_version",
         "ALTER TABLE model_providers ADD COLUMN key_version INTEGER"),
        ("model_providers", "key_modified_at",
         "ALTER TABLE model_providers ADD COLUMN key_modified_at DATETIME"),
        # D21 (M1-d): the catalog version + model descriptor the generate stage deployed from
        ("deployments", "catalog_version",
         "ALTER TABLE deployments ADD COLUMN catalog_version INTEGER"),
        ("deployments", "model_descriptor",
         "ALTER TABLE deployments ADD COLUMN model_descriptor JSON"),
        # r12-g: password-change counter carried in the session cookie
        ("users", "session_epoch",
         "ALTER TABLE users ADD COLUMN session_epoch INTEGER DEFAULT 0 NOT NULL"),
        # r14-a (N14-obs1): per-store merge strategy knobs; NULL = defaults
        ("memory_stores", "merge_settings",
         "ALTER TABLE memory_stores ADD COLUMN merge_settings JSON"),
        # r16-i (N16-obs85): bounded tool-call summary on a chat `tool` row
        ("chat_messages", "tool_call", "ALTER TABLE chat_messages ADD COLUMN tool_call JSON"),
        # N17-obs203: a provider's visibility scope — every pre-existing row was an
        # administrator's hub-wide registration, so the default is `workspace`
        ("model_providers", "scope",
         "ALTER TABLE model_providers ADD COLUMN scope VARCHAR(16) DEFAULT 'workspace'"),
        # N17-obs203 §3: the console-entered key's last four characters + when (never the value)
        ("model_providers", "key_last4",
         "ALTER TABLE model_providers ADD COLUMN key_last4 VARCHAR(4)"),
        ("model_providers", "key_entered_at",
         "ALTER TABLE model_providers ADD COLUMN key_entered_at DATETIME"),
        # N17-obs203 §5: the journal row's provider scope (NULL = hub-wide / pre-obs203 rows,
        # which all were administrators' workspace-scope changes and keep counting as versions)
        ("model_catalog_changes", "scope",
         "ALTER TABLE model_catalog_changes ADD COLUMN scope VARCHAR(16)"),
        # per-user policy stream B5: the gateway interceptor's decision lines (I7) — which
        # stage decided, the policy session, the engine event / store epoch, argument NAMES
        ("policy_decisions", "stage",
         "ALTER TABLE policy_decisions ADD COLUMN stage VARCHAR(8)"),
        ("policy_decisions", "session_psid",
         "ALTER TABLE policy_decisions ADD COLUMN session_psid VARCHAR(128)"),
        ("policy_decisions", "event_seq",
         "ALTER TABLE policy_decisions ADD COLUMN event_seq INTEGER"),
        ("policy_decisions", "store_epoch",
         "ALTER TABLE policy_decisions ADD COLUMN store_epoch VARCHAR(32)"),
        ("policy_decisions", "input_keys",
         "ALTER TABLE policy_decisions ADD COLUMN input_keys JSON"),
    ):
        if table in inspector.get_table_names():
            existing = {c["name"] for c in inspector.get_columns(table)}
            if column not in existing:
                with bind.begin() as conn:
                    conn.execute(text(ddl))
    _migrate_inbound_auth_columns(bind)
    _migrate_identity_provider_columns(bind)
    # --- CN evaluation / optimization (N12) ---
    _migrate_evaluation_columns(bind)
    # --- end CN evaluation / optimization (N12) ---
    _migrate_oauth_session_columns(bind)
    _migrate_assistant_columns(bind)
    _migrate_workspace_columns(bind)
    _migrate_system_key_index(bind)
    _migrate_system_skill_records_columns(bind)
    _migrate_system_skill_records_index(bind)
    _migrate_identity_indexes(bind)
    _migrate_managed_memories_index(bind)
    _migrate_registry_records_index(bind)
    _migrate_registry_search_index(bind)
    # --- CN memory/KB (N6) ---
    _migrate_memory_stores_index(bind)
    _migrate_knowledge_bases_index(bind)
    _migrate_policy_indexes(bind)
    # --- end CN memory/KB (N6) ---
    # --- CN evaluation / optimization (N12) --- (after the workspace columns)
    _migrate_evaluation_indexes(bind)
    # --- end CN evaluation / optimization (N12) ---
    _migrate_model_registry_indexes(bind)  # D21 (after the workspace columns)
    _migrate_legacy_preset_providers(bind)  # N17-obs207 data fix (N17-obs282: moved here)
    # --- Family B step 1 (upstream #259 / #261): creators + API key lifecycle ---
    _migrate_creator_columns(bind)
    _migrate_api_key_lifecycle_columns(bind)
    _migrate_resource_creators_index(bind)
    # --- end Family B step 1 ---


# Every table that records its creator (`app.services.creators`): the two columns are
# added additively; the index on `created_by` is what step 2's visibility clause and the
# creator-or-admin gates filter on. Spelled out per table (not generated) because
# `tests/test_ledger_migration.py` reads the (table, column) pairs off this source.
_CREATOR_COLUMN_DDL: dict[str, dict[str, str]] = {
    # N17-obs203: `created_by` already existed (VARCHAR(64), the journal actor) — only the
    # display column is added; the re-point below turns the username into the ownership key
    "model_providers": {
        "created_by": "ALTER TABLE model_providers ADD COLUMN created_by VARCHAR(64)",
        "created_by_name": (
            "ALTER TABLE model_providers ADD COLUMN created_by_name VARCHAR(64)"
        ),
    },
    "agents": {
        "created_by": "ALTER TABLE agents ADD COLUMN created_by VARCHAR(32)",
        "created_by_name": "ALTER TABLE agents ADD COLUMN created_by_name VARCHAR(64)",
    },
    "chat_sessions": {
        "created_by": "ALTER TABLE chat_sessions ADD COLUMN created_by VARCHAR(32)",
        "created_by_name": "ALTER TABLE chat_sessions ADD COLUMN created_by_name VARCHAR(64)",
    },
    "api_keys": {
        "created_by": "ALTER TABLE api_keys ADD COLUMN created_by VARCHAR(32)",
        "created_by_name": "ALTER TABLE api_keys ADD COLUMN created_by_name VARCHAR(64)",
    },
    "managed_memories": {
        "created_by": "ALTER TABLE managed_memories ADD COLUMN created_by VARCHAR(32)",
        "created_by_name": (
            "ALTER TABLE managed_memories ADD COLUMN created_by_name VARCHAR(64)"
        ),
    },
    "memory_stores": {
        "created_by": "ALTER TABLE memory_stores ADD COLUMN created_by VARCHAR(32)",
        "created_by_name": "ALTER TABLE memory_stores ADD COLUMN created_by_name VARCHAR(64)",
    },
    "knowledge_bases": {
        "created_by": "ALTER TABLE knowledge_bases ADD COLUMN created_by VARCHAR(32)",
        "created_by_name": (
            "ALTER TABLE knowledge_bases ADD COLUMN created_by_name VARCHAR(64)"
        ),
    },
    "identity_providers": {
        "created_by": "ALTER TABLE identity_providers ADD COLUMN created_by VARCHAR(32)",
        "created_by_name": (
            "ALTER TABLE identity_providers ADD COLUMN created_by_name VARCHAR(64)"
        ),
    },
    "eval_datasets": {
        "created_by": "ALTER TABLE eval_datasets ADD COLUMN created_by VARCHAR(32)",
        "created_by_name": "ALTER TABLE eval_datasets ADD COLUMN created_by_name VARCHAR(64)",
    },
    "eval_runs": {
        "created_by": "ALTER TABLE eval_runs ADD COLUMN created_by VARCHAR(32)",
        "created_by_name": "ALTER TABLE eval_runs ADD COLUMN created_by_name VARCHAR(64)",
    },
    "online_eval_configs": {
        "created_by": "ALTER TABLE online_eval_configs ADD COLUMN created_by VARCHAR(32)",
        "created_by_name": (
            "ALTER TABLE online_eval_configs ADD COLUMN created_by_name VARCHAR(64)"
        ),
    },
    "eval_pipelines": {
        "created_by": "ALTER TABLE eval_pipelines ADD COLUMN created_by VARCHAR(32)",
        "created_by_name": "ALTER TABLE eval_pipelines ADD COLUMN created_by_name VARCHAR(64)",
    },
    "eval_evaluators": {
        "created_by": "ALTER TABLE eval_evaluators ADD COLUMN created_by VARCHAR(32)",
        "created_by_name": (
            "ALTER TABLE eval_evaluators ADD COLUMN created_by_name VARCHAR(64)"
        ),
    },
    "experiments": {
        "created_by": "ALTER TABLE experiments ADD COLUMN created_by VARCHAR(32)",
        "created_by_name": "ALTER TABLE experiments ADD COLUMN created_by_name VARCHAR(64)",
    },
    "runtime_canaries": {
        "created_by": "ALTER TABLE runtime_canaries ADD COLUMN created_by VARCHAR(32)",
        "created_by_name": (
            "ALTER TABLE runtime_canaries ADD COLUMN created_by_name VARCHAR(64)"
        ),
    },
    "skill_lab_jobs": {
        "created_by": "ALTER TABLE skill_lab_jobs ADD COLUMN created_by VARCHAR(32)",
        "created_by_name": "ALTER TABLE skill_lab_jobs ADD COLUMN created_by_name VARCHAR(64)",
    },
    "skill_lab_tasksets": {
        "created_by": "ALTER TABLE skill_lab_tasksets ADD COLUMN created_by VARCHAR(32)",
        "created_by_name": (
            "ALTER TABLE skill_lab_tasksets ADD COLUMN created_by_name VARCHAR(64)"
        ),
    },
    # N15-obs7: local registry records (MCP servers / skills) gate PUT / reimport / DELETE
    # on their creator; legacy rows keep NULL (an administrator's).
    "registry_records": {
        "created_by": "ALTER TABLE registry_records ADD COLUMN created_by VARCHAR(32)",
        "created_by_name": (
            "ALTER TABLE registry_records ADD COLUMN created_by_name VARCHAR(64)"
        ),
    },
}

# The five tables whose `created_by` held a USERNAME before Family B (N10v2-obs11 keys,
# issue-#55 managed memories, the N6 stores / KBs, identity Connections): re-pointed once.
_LEGACY_USERNAME_CREATOR_TABLES = (
    "api_keys",
    "managed_memories",
    "memory_stores",
    "knowledge_bases",
    "identity_providers",
    # N17-obs203: `model_providers.created_by` carried the journal ACTOR (a username; the seed
    # wrote `system`) — re-pointed to `users.id` the same way, the seed / bootstrap actors
    # recording no creator (`_LEGACY_CREATOR_SKIP`)
    "model_providers",
)
#: per legacy table, the `created_by` values that record NO creator (default: the empty string)
_LEGACY_CREATOR_SKIP: dict[str, tuple[str, ...]] = {
    "model_providers": ("", "system", "bootstrap"),
}
# `agents.owner` sentinels that never named a console account: the pre-obs9 column
# default, the system-preset stamp and the discovered-runtime stamp.
_AGENT_OWNER_SENTINELS = ("river", "system", "aws-discovery", "")


class _RawExecutor:
    """`conn.execute(text(...), params)` over a DBAPI cursor — what `_ddl_transaction`
    hands out on SQLite, so the migration body reads the same on both paths."""

    def __init__(self, cursor) -> None:
        self._cursor = cursor

    def execute(self, clause, params=None):
        return self._cursor.execute(str(clause), params or {})


@contextmanager
def _ddl_transaction(bind):
    """ONE transaction that includes the DDL. pysqlite's legacy transaction control
    commits implicitly before an ALTER TABLE, so `bind.begin()` cannot make
    "ADD COLUMN + re-point" atomic on SQLite: a crash after the ADD would leave the
    table half-upgraded. On SQLite the transaction is driven by hand on a raw
    connection in autocommit mode (explicit BEGIN / COMMIT / ROLLBACK — SQLite's DDL is
    transactional); any other backend gets the engine's ordinary transaction."""
    if bind.dialect.name != "sqlite":
        with bind.begin() as conn:
            yield conn
        return
    raw = bind.raw_connection()
    try:
        dbapi = raw.driver_connection
        previous = dbapi.isolation_level
        dbapi.isolation_level = None  # autocommit: no implicit BEGIN/COMMIT by the driver
        cursor = dbapi.cursor()
        try:
            cursor.execute("BEGIN")
            try:
                yield _RawExecutor(cursor)
            except BaseException:
                cursor.execute("ROLLBACK")
                raise
            cursor.execute("COMMIT")
        finally:
            cursor.close()
            dbapi.isolation_level = previous
    finally:
        raw.close()


def _migrate_creator_columns(bind) -> None:
    """The creator of every user-managed resource (`app.services.creators`):
    `created_by` (`users.id`, indexed) and `created_by_name` (display) on every table
    in `_CREATOR_COLUMN_DDL`. Existing rows keep NULL — a legacy row has an unknown
    creator and is never backfilled — with two one-time exceptions:

    * the five `_LEGACY_USERNAME_CREATOR_TABLES` carried a **username** in
      `created_by`: it moves to `created_by_name` (`""` → NULL) and `created_by`
      becomes the matching `users.id` (case-insensitive on `users.username_key`), NULL
      when no account has that name (the config admin / auth-disabled operator, a
      deleted account) — and NULL for an adopted managed memory, which pre-existed and
      so has no owner (upstream #261);
    * `agents.owner` (the pre-#261 username column, retired under C5) is copied the same
      way — its sentinels (`river` default, `system`, `aws-discovery`) record no creator
      — and the column is then DROPPED.

    Atomicity (rev-r11 must-fix): per table the ADD COLUMN(s), the re-point and, for
    `agents`, the owner copy + DROP run in ONE transaction, so a crash can never leave
    `created_by_name` present with usernames still sitting in `created_by` (which
    `creator_offboarded` would read as deleted accounts). The re-point is also
    data-driven rather than gated on "the column was just added": it touches exactly the
    rows that still carry a legacy value and no display name, so a ledger that was left
    half-migrated by any other means is finished on the next start, and a second start
    is a no-op. The agents copy is gated on the `owner` column still existing.
    """
    from sqlalchemy import inspect, text

    inspector = inspect(bind)
    live_tables = set(inspector.get_table_names())
    for table, columns in _CREATOR_COLUMN_DDL.items():
        if table not in live_tables:
            continue
        existing = {c["name"] for c in inspector.get_columns(table)}
        with _ddl_transaction(bind) as conn:
            for column, ddl in columns.items():
                if column not in existing:
                    conn.execute(text(ddl))
            if table in _LEGACY_USERNAME_CREATOR_TABLES:
                _repoint_username_creator(conn, table, "created_by", "users" in live_tables,
                                          skip=_LEGACY_CREATOR_SKIP.get(table, ("",)),
                                          adopted_without_owner=(table == "managed_memories"))
            if table == "agents" and "owner" in existing:
                _repoint_username_creator(conn, "agents", "owner", "users" in live_tables,
                                          skip=_AGENT_OWNER_SENTINELS)
                conn.execute(text("ALTER TABLE agents DROP COLUMN owner"))
            conn.execute(
                text(
                    f"CREATE INDEX IF NOT EXISTS ix_{table}_created_by "
                    f"ON {table} (created_by)"
                )
            )


def _repoint_username_creator(
    conn, table: str, source: str, has_users: bool, *,
    skip: tuple[str, ...] = ("",), adopted_without_owner: bool = False,
) -> None:
    """Rows of `table` that still carry a legacy username in `source` and have no
    `created_by_name` yet: `source` → `created_by_name` (values in `skip` record no
    name), `created_by` ← the account id with that username, NULL when none. Runs on
    the caller's connection (one transaction per table); a row already carrying a
    display name — stamped by this build, or re-pointed earlier — is never touched."""
    from sqlalchemy import text

    pending = [
        row[0] for row in conn.execute(text(
            f"SELECT rowid FROM {table} "  # noqa: S608
            f"WHERE {source} IS NOT NULL AND created_by_name IS NULL"
        ))
    ]
    placeholders = ", ".join(f":s{i}" for i in range(len(skip)))
    skip_params = {f"s{i}": value for i, value in enumerate(skip)}
    for chunk_start in range(0, len(pending), 500):
        chunk = pending[chunk_start:chunk_start + 500]
        ids = ", ".join(f":r{i}" for i in range(len(chunk)))
        params = {f"r{i}": rowid for i, rowid in enumerate(chunk)}
        conn.execute(
            text(
                f"UPDATE {table} SET created_by_name = {source} "  # noqa: S608
                f"WHERE rowid IN ({ids}) AND {source} NOT IN ({placeholders})"
            ),
            {**params, **skip_params},
        )
        if has_users:
            conn.execute(
                text(
                    f"UPDATE {table} SET created_by = ("  # noqa: S608
                    "SELECT u.id FROM users u "
                    f"WHERE u.username_key = lower({table}.created_by_name)) "
                    f"WHERE rowid IN ({ids})"
                ),
                params,
            )
        else:
            conn.execute(
                text(f"UPDATE {table} SET created_by = NULL WHERE rowid IN ({ids})"),  # noqa: S608
                params,
            )
        if adopted_without_owner:
            conn.execute(
                text(
                    f"UPDATE {table} SET created_by = NULL "  # noqa: S608
                    f"WHERE rowid IN ({ids}) AND origin = 'adopted'"
                ),
                params,
            )


def _migrate_api_key_lifecycle_columns(bind) -> None:
    """#259 credential lifecycle on `api_keys`: `last_used_at` (the creator columns are
    `_migrate_creator_columns`'). Existing rows keep NULL."""
    from sqlalchemy import inspect, text

    inspector = inspect(bind)
    if "api_keys" not in inspector.get_table_names():
        return
    existing = {c["name"] for c in inspector.get_columns("api_keys")}
    if "last_used_at" not in existing:
        with bind.begin() as conn:
            conn.execute(text("ALTER TABLE api_keys ADD COLUMN last_used_at DATETIME"))


def _migrate_resource_creators_index(bind) -> None:
    """The unique (workspace, kind, resource) index of `resource_creators`: a
    retried record of the same resource leaves one row. `create_all` builds it
    with the table; this keeps a ledger whose table lost it honest."""
    from sqlalchemy import inspect, text

    if "resource_creators" not in inspect(bind).get_table_names():
        return
    with bind.begin() as conn:
        conn.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_resource_creators_workspace_kind_resource "
                "ON resource_creators (workspace_id, kind, resource_id)"
            )
        )


def _migrate_model_registry_indexes(bind) -> None:
    """The model registry's indexes (D21, plan §2.1): the unique `(workspace_id, model_id)`
    flag index on `workspace_model_settings` (lost with the column in the workspace-column
    rehearsal — runs after `_migrate_workspace_columns`), `models(provider_id)`,
    `models(kind, enabled)` and `model_catalog_changes(created_at)`. All IF NOT EXISTS."""
    from sqlalchemy import inspect, text

    indexes = {
        "workspace_model_settings": (
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_workspace_model_settings_workspace_model "
            "ON workspace_model_settings (workspace_id, model_id)"
        ),
        "models": (
            "CREATE INDEX IF NOT EXISTS ix_models_kind_enabled ON models (kind, enabled)"
        ),
        "model_catalog_changes": (
            "CREATE INDEX IF NOT EXISTS ix_model_catalog_changes_created_at "
            "ON model_catalog_changes (created_at)"
        ),
    }
    live_tables = set(inspect(bind).get_table_names())
    for table, ddl in indexes.items():
        if table in live_tables:
            with bind.begin() as conn:
                conn.execute(text(ddl))
    if "models" in live_tables:
        with bind.begin() as conn:
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_models_provider_id ON models (provider_id)"))


#: provider names a pre-R13 seed marked `builtin` that are NOT the hub's: only the Global
#: Bedrock provider (the presets' and system agents' key) is undeletable (N17-obs207)
LEGACY_UNPINNED_PROVIDERS: tuple[str, ...] = ("siliconflow",)


def _migrate_legacy_preset_providers(bind) -> None:
    """N17-obs207 (user decision 2026-10-17 / 2026-10-21): SiliconFlow stopped being a preset in
    R13, but a ledger seeded before that still carries its row as `builtin` — undeletable, and
    the console showed no delete anywhere (the hub Bedrock row is builtin too). Flip the flags
    (`builtin`, `required`) once; its `source: builtin` model rows become deletable with it. No
    journal row (the catalog a runtime or a picker renders is unchanged — a version bump would
    only raise the preset banner); one log line when something changed. N17-obs282 (design
    simplification C7): a data migration, so it lives in this funnel (idempotent, `init_db`
    only) instead of `model_registry.ensure_seeded`, which the registry page's GET calls on
    every read."""
    from sqlalchemy import bindparam, inspect, text

    if "model_providers" not in inspect(bind).get_table_names():
        return
    statement = text(
        "UPDATE model_providers SET builtin = 0, required = 0 "
        "WHERE name IN :names AND builtin = 1"
    ).bindparams(bindparam("names", expanding=True))
    with bind.begin() as conn:
        changed = conn.execute(statement, {"names": list(LEGACY_UNPINNED_PROVIDERS)}).rowcount
    if changed:
        logging.getLogger("launchpad.model_registry").info(
            "model-registry: legacy preset provider(s) %s are no longer built in (deletable)",
            ", ".join(LEGACY_UNPINNED_PROVIDERS))


# --- CN evaluation / optimization (N12) ---
def _migrate_evaluation_columns(bind) -> None:
    """Additive columns of the Global baseline's evaluation / optimization tables
    (restored verbatim in N12 E0-2): a ledger created by an older Launchpad release
    has the tables without these columns. Same guarded ALTERs as the baseline."""
    from sqlalchemy import inspect, text

    additions = {
        "eval_datasets": {
            "description": "ALTER TABLE eval_datasets ADD COLUMN description TEXT DEFAULT ''",
            "cloud": "ALTER TABLE eval_datasets ADD COLUMN cloud JSON",
            # N17-obs407: the dataset's linked agent (trajectory tool names)
            "agent_id": "ALTER TABLE eval_datasets ADD COLUMN agent_id VARCHAR(32)",
            # D3-c: the local dataset's revision (older rows read as revision 1)
            "revision": "ALTER TABLE eval_datasets ADD COLUMN revision INTEGER DEFAULT 1",
        },
        "eval_runs": {
            "dataset_version": "ALTER TABLE eval_runs ADD COLUMN dataset_version VARCHAR(16)",
            "name": "ALTER TABLE eval_runs ADD COLUMN name VARCHAR(64)",
            "description": "ALTER TABLE eval_runs ADD COLUMN description TEXT",
            "log_source": "ALTER TABLE eval_runs ADD COLUMN log_source JSON",
            "budget_stops": "ALTER TABLE eval_runs ADD COLUMN budget_stops JSON",
            "record_errors": "ALTER TABLE eval_runs ADD COLUMN record_errors JSON",
            "session_failures": "ALTER TABLE eval_runs ADD COLUMN session_failures JSON",
            # scorecard stamps (evaluation/scorecard.py, upstream #289): NULL on older rows
            "agent_snapshot_seq": "ALTER TABLE eval_runs ADD COLUMN agent_snapshot_seq INTEGER",
            "dataset_fingerprint": (
                "ALTER TABLE eval_runs ADD COLUMN dataset_fingerprint VARCHAR(16)"
            ),
            "dataset_size": "ALTER TABLE eval_runs ADD COLUMN dataset_size INTEGER",
            "scenario_map": "ALTER TABLE eval_runs ADD COLUMN scenario_map JSON",
            "scenario_failures": "ALTER TABLE eval_runs ADD COLUMN scenario_failures JSON",
            # eval accuracy item 1: the per-session extraction reconciliation
            "extraction_check": "ALTER TABLE eval_runs ADD COLUMN extraction_check JSON",
            # P3.1 / P3.2: the run's judge model and its effective inference config
            "judge_model_id": "ALTER TABLE eval_runs ADD COLUMN judge_model_id VARCHAR(120)",
            "judge_inference": "ALTER TABLE eval_runs ADD COLUMN judge_inference JSON",
            # N17-obs407: trajectory names the run rewrote to the agent's canonical ones
            "tool_name_rewrites": "ALTER TABLE eval_runs ADD COLUMN tool_name_rewrites JSON",
            # D3-d / R3-c: the dataset split the run replayed
            "dataset_split": "ALTER TABLE eval_runs ADD COLUMN dataset_split VARCHAR(12)",
            # D3-c: the local dataset revision the run replayed
            "dataset_revision": "ALTER TABLE eval_runs ADD COLUMN dataset_revision INTEGER",
            # N17-obs407: non-blocking run warnings (expected tools never observed)
            "warnings": "ALTER TABLE eval_runs ADD COLUMN warnings JSON",
            # R3-f: the run's unit coverage
            "unit_coverage": "ALTER TABLE eval_runs ADD COLUMN unit_coverage JSON",
            # P8.3 / P8.4: the run's judge tokens and budget verdict
            "token_usage": "ALTER TABLE eval_runs ADD COLUMN token_usage JSON",
            # N17-obs418: the newest worker heartbeat the poller read
            "last_heartbeat_at": "ALTER TABLE eval_runs ADD COLUMN last_heartbeat_at VARCHAR(40)",
            # P7.1 / P7.6: the evaluator definitions at submit, the worker release
            "evaluator_snapshot": "ALTER TABLE eval_runs ADD COLUMN evaluator_snapshot JSON",
            "worker_release": "ALTER TABLE eval_runs ADD COLUMN worker_release VARCHAR(40)",
        },
        "eval_results": {
            # P8.3: the unit's judge tokens
            "token_usage": "ALTER TABLE eval_results ADD COLUMN token_usage JSON",
            "judge_model_id": (
                "ALTER TABLE eval_results ADD COLUMN judge_model_id VARCHAR(120)"
            ),
            "judge_inference": "ALTER TABLE eval_results ADD COLUMN judge_inference JSON",
        },
        # P7.1: the evaluator definition's version and hash
        "eval_evaluators": {
            "version": "ALTER TABLE eval_evaluators ADD COLUMN version INTEGER",
            "definition_hash": "ALTER TABLE eval_evaluators ADD COLUMN definition_hash VARCHAR(16)",
        },
        "online_samples": {
            "extraction_check": "ALTER TABLE online_samples ADD COLUMN extraction_check JSON",
            # P4.6: bounded retry of a transiently failed sample
            "attempts": "ALTER TABLE online_samples ADD COLUMN attempts INTEGER",
            "retry_at": "ALTER TABLE online_samples ADD COLUMN retry_at DATETIME",
            # N17-obs421: the sample's judge tokens
            "token_usage": "ALTER TABLE online_samples ADD COLUMN token_usage JSON",
            # N17-obs422: activity segments (an older row is its session's first)
            "segment": "ALTER TABLE online_samples ADD COLUMN segment INTEGER DEFAULT 1",
            "last_activity": "ALTER TABLE online_samples ADD COLUMN last_activity DATETIME",
            "segment_after": "ALTER TABLE online_samples ADD COLUMN segment_after DATETIME",
        },
        "eval_recommendations": {
            "accepted": "ALTER TABLE eval_recommendations ADD COLUMN accepted JSON",
        },
        # N17-obs447: a canary action's coded progress line
        "runtime_canaries": {
            "progress_detail": "ALTER TABLE runtime_canaries ADD COLUMN progress_detail JSON",
        },
        "experiments": {
            "running_action": "ALTER TABLE experiments ADD COLUMN running_action VARCHAR(24)",
            "progress": "ALTER TABLE experiments ADD COLUMN progress TEXT",
        },
        # CN (CA-3): the online config is local truth — the columns AWS used to hold
        "online_eval_configs": {
            "description": "ALTER TABLE online_eval_configs ADD COLUMN description TEXT",
            "mode": "ALTER TABLE online_eval_configs ADD COLUMN mode VARCHAR(12)",
            "evaluators": "ALTER TABLE online_eval_configs ADD COLUMN evaluators JSON",
            "insights": "ALTER TABLE online_eval_configs ADD COLUMN insights JSON",
            "clustering_frequencies": (
                "ALTER TABLE online_eval_configs ADD COLUMN clustering_frequencies JSON"
            ),
            "sampling_percentage": (
                "ALTER TABLE online_eval_configs ADD COLUMN sampling_percentage FLOAT"
            ),
            "session_timeout_minutes": (
                "ALTER TABLE online_eval_configs ADD COLUMN session_timeout_minutes INTEGER"
            ),
            "filters": "ALTER TABLE online_eval_configs ADD COLUMN filters JSON",
            # N17-obs444: when sampling last (re)started — NULL = since created_at
            "sampling_since": "ALTER TABLE online_eval_configs ADD COLUMN sampling_since DATETIME",
            "execution_status": (
                "ALTER TABLE online_eval_configs ADD COLUMN execution_status VARCHAR(10)"
            ),
            "status": "ALTER TABLE online_eval_configs ADD COLUMN status VARCHAR(10)",
            "failure_reason": "ALTER TABLE online_eval_configs ADD COLUMN failure_reason TEXT",
            "last_scanned_at": (
                "ALTER TABLE online_eval_configs ADD COLUMN last_scanned_at DATETIME"
            ),
            "updated_at": "ALTER TABLE online_eval_configs ADD COLUMN updated_at DATETIME",
            # N17-obs337: the log groups of a log-source (BYOC) config
            "log_groups": "ALTER TABLE online_eval_configs ADD COLUMN log_groups JSON",
            # P3.2: the judge snapshotted at create
            "judge_model_id": (
                "ALTER TABLE online_eval_configs ADD COLUMN judge_model_id VARCHAR(120)"
            ),
            "judge_inference": (
                "ALTER TABLE online_eval_configs ADD COLUMN judge_inference JSON"
            ),
        },
    }
    inspector = inspect(bind)
    live_tables = set(inspector.get_table_names())
    for table, columns in additions.items():
        if table not in live_tables:
            continue
        existing = {c["name"] for c in inspector.get_columns(table)}
        for column, ddl in columns.items():
            if column not in existing:
                with bind.begin() as conn:
                    conn.execute(text(ddl))


def _migrate_evaluation_indexes(bind) -> None:
    """The unique indexes of the CN evaluation tables that span `workspace_id` (N12):
    `create_all` builds them with a fresh table; this restores them on a table whose
    `workspace_id` was just re-added. Runs after `_migrate_workspace_columns`."""
    from sqlalchemy import inspect, text

    indexes = {
        "eval_evaluators": (
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_eval_evaluators_workspace_name "
            "ON eval_evaluators (workspace_id, name)"
        ),
        "online_samples": (
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_online_samples_config_session_segment "
            "ON online_samples (config_id, session_id, segment)"
        ),
        "experiment_assignments": (
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_experiment_assignments_owner_session "
            "ON experiment_assignments (owner_id, session_id)"
        ),
    }
    live_tables = set(inspect(bind).get_table_names())
    if "online_samples" in live_tables:
        # N17-obs422: a sample is unique per (config, session, SEGMENT) — the old
        # (config, session) index forbade the idle-resume segment
        with bind.begin() as conn:
            conn.execute(text("UPDATE online_samples SET segment = 1 WHERE segment IS NULL"))
            conn.execute(text("DROP INDEX IF EXISTS uq_online_samples_config_session"))
    for table, ddl in indexes.items():
        if table in live_tables:
            with bind.begin() as conn:
                conn.execute(text(ddl))
# --- end CN evaluation / optimization (N12) ---


def _migrate_inbound_auth_columns(bind) -> None:
    """Identity columns the earlier identity fork added (workspace policy blob,
    per-agent inbound-auth snapshot). A ledger upgraded by that fork already has
    them — each ALTER is guarded — and a ledger that never ran it gets them here.
    All NULL on upgrade: NULL mode reads back as IAM."""
    from sqlalchemy import inspect, text

    additions = {
        "workspaces": {
            "settings": "ALTER TABLE workspaces ADD COLUMN settings JSON",
        },
        "agents": {
            "inbound_auth_mode": "ALTER TABLE agents ADD COLUMN inbound_auth_mode VARCHAR(8)",
            "inbound_auth_config": "ALTER TABLE agents ADD COLUMN inbound_auth_config JSON",
        },
    }
    inspector = inspect(bind)
    live_tables = set(inspector.get_table_names())
    for table, columns in additions.items():
        if table not in live_tables:
            continue
        existing = {c["name"] for c in inspector.get_columns(table)}
        for column, ddl in columns.items():
            if column not in existing:
                with bind.begin() as conn:
                    conn.execute(text(ddl))


def _migrate_identity_provider_columns(bind) -> None:
    """P1 Connection columns on the `identity_providers` table the earlier
    identity fork created (its rows keep NULL: display-only fields)."""
    from sqlalchemy import inspect, text

    inspector = inspect(bind)
    if "identity_providers" not in inspector.get_table_names():
        return
    existing = {c["name"] for c in inspector.get_columns("identity_providers")}
    additions = {
        "client_id": "ALTER TABLE identity_providers ADD COLUMN client_id VARCHAR(256)",
        "scopes": "ALTER TABLE identity_providers ADD COLUMN scopes JSON",
        "template": "ALTER TABLE identity_providers ADD COLUMN template VARCHAR(32)",
        "description": "ALTER TABLE identity_providers ADD COLUMN description VARCHAR(200)",
    }
    for column, ddl in additions.items():
        if column not in existing:
            with bind.begin() as conn:
                conn.execute(text(ddl))


def _migrate_oauth_session_columns(bind) -> None:
    """Legacy caller-kind column on in-flight 3LO sessions, from the pre-N3
    identity fork's JWT-inbound era. Every Runtime inbound is AWS_IAM-only in
    this build, so upgraded rows (and every new one) read "iam"."""
    from sqlalchemy import inspect, text

    inspector = inspect(bind)
    if "oauth_pending_sessions" not in inspector.get_table_names():
        return
    existing = {c["name"] for c in inspector.get_columns("oauth_pending_sessions")}
    if "caller_kind" not in existing:
        with bind.begin() as conn:
            conn.execute(text(
                "ALTER TABLE oauth_pending_sessions "
                "ADD COLUMN caller_kind VARCHAR(16) DEFAULT 'iam' NOT NULL"
            ))


def _migrate_identity_indexes(bind) -> None:
    """The unique indexes of the identity tables. `create_all` builds them
    with a fresh table; this restores them on a table that predates them. Runs
    after `_migrate_workspace_columns` because each spans `workspace_id`."""
    from sqlalchemy import inspect, text

    indexes = {
        "identity_providers": (
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_identity_providers_ws_kind_name "
            "ON identity_providers (workspace_id, kind, name)"
        ),
        "user_token_revocations": (
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_user_token_revocations_ws_provider_user "
            "ON user_token_revocations (workspace_id, provider, user_id)"
        ),
        "oauth_pending_sessions": (
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_oauth_pending_sessions_session_uri "
            "ON oauth_pending_sessions (workspace_id, session_uri)"
        ),
        "user_grants": (
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_user_grants_ws_user_provider_agent "
            "ON user_grants (workspace_id, user_id, provider, agent_id)"
        ),
    }
    live_tables = set(inspect(bind).get_table_names())
    for table, ddl in indexes.items():
        if table in live_tables:
            with bind.begin() as conn:
                conn.execute(text(ddl))


def _migrate_system_skill_records_columns(bind) -> None:
    """SE-043 correction: the accepted-but-unverified id, the persisted create request
    and the update-intent high-water mark on a ledger created by the first candidate."""
    from sqlalchemy import inspect, text

    inspector = inspect(bind)
    if "system_skill_records" not in inspector.get_table_names():
        return
    existing = {c["name"] for c in inspector.get_columns("system_skill_records")}
    additions = {
        "pending_record_id": (
            "ALTER TABLE system_skill_records ADD COLUMN pending_record_id VARCHAR(64)"
        ),
        "create_request": "ALTER TABLE system_skill_records ADD COLUMN create_request JSON",
        "intent_version": (
            "ALTER TABLE system_skill_records ADD COLUMN intent_version VARCHAR(32)"
        ),
        "intent_digest": "ALTER TABLE system_skill_records ADD COLUMN intent_digest VARCHAR(64)",
        "intent_content_digest": (
            "ALTER TABLE system_skill_records ADD COLUMN intent_content_digest VARCHAR(64)"
        ),
    }
    for column, ddl in additions.items():
        if column not in existing:
            with bind.begin() as conn:
                conn.execute(text(ddl))


def _migrate_system_skill_records_index(bind) -> None:
    """The unique (workspace, preset) index that arbitrates concurrent system-skill
    registrations (SE-043). `create_all` builds it with the table on a fresh ledger;
    this keeps an upgraded ledger whose table predates the index honest."""
    from sqlalchemy import inspect, text

    if "system_skill_records" not in inspect(bind).get_table_names():
        return
    with bind.begin() as conn:
        conn.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_system_skill_records_workspace_preset "
                "ON system_skill_records (workspace_id, preset_key)"
            )
        )


def _migrate_managed_memories_index(bind) -> None:
    """The unique (workspace, memory) index behind memory ownership (issue #55):
    an adopt racing a create of the same id leaves one row. `create_all` builds it
    with the table; this keeps a ledger whose table lost it honest."""
    from sqlalchemy import inspect, text

    if "managed_memories" not in inspect(bind).get_table_names():
        return
    with bind.begin() as conn:
        conn.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_managed_memories_workspace_memory "
                "ON managed_memories (workspace_id, memory_id)"
            )
        )


# --- CN memory/KB (N6) ---
def _migrate_memory_stores_index(bind) -> None:
    """The unique (workspace, memory) index of the self-hosted Memory ledger (N6):
    mirrors `_migrate_managed_memories_index` — `create_all` builds it with a fresh
    table; this keeps an upgraded ledger whose table predates the index honest."""
    from sqlalchemy import inspect, text

    if "memory_stores" not in inspect(bind).get_table_names():
        return
    with bind.begin() as conn:
        conn.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_memory_stores_workspace_memory "
                "ON memory_stores (workspace_id, memory_id)"
            )
        )
# --- end CN memory/KB (N6) ---


# --- CN memory/KB (N6) ---
def _migrate_knowledge_bases_index(bind) -> None:
    """The unique (workspace, name) index of the self-hosted Knowledge Bases ledger
    (N6): mirrors `_migrate_memory_stores_index` — `create_all` builds it with a
    fresh table; this keeps an upgraded ledger whose table predates the index
    honest."""
    from sqlalchemy import inspect, text

    if "knowledge_bases" not in inspect(bind).get_table_names():
        return
    with bind.begin() as conn:
        conn.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_knowledge_bases_workspace_name "
                "ON knowledge_bases (workspace_id, name)"
            )
        )
# --- end CN memory/KB (N6) ---


def _migrate_policy_indexes(bind) -> None:
    """The unique indexes of the self-hosted Policy tables (D20): `(workspace_id, name)`
    on `policies`, `(workspace_id)` on `policy_settings` and `(policy_id, version)` on
    `policy_versions`. `create_all` builds
    them with a fresh table; this restores them on a table whose `workspace_id` was just
    re-added by `_migrate_workspace_columns`. Runs after it."""
    from sqlalchemy import inspect, text

    indexes = {
        "policies": (
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_policies_engine_name "
            "ON policies (engine_id, name)"
        ),
        "policy_settings": (
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_policy_settings_workspace "
            "ON policy_settings (workspace_id)"
        ),
        "policy_engines": (
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_policy_engines_workspace_name "
            "ON policy_engines (workspace_id, name)"
        ),
    }
    # Global model (2026-10-28): the engine / gateway lookups on upgraded tables (create_all
    # builds them only with a fresh table; a missing index is slower, never wrong)
    lookups = {
        "policies": "CREATE INDEX IF NOT EXISTS ix_policies_engine_id ON policies (engine_id)",
        "policy_changes": ("CREATE INDEX IF NOT EXISTS ix_policy_changes_engine_id "
                           "ON policy_changes (engine_id)"),
        "policy_decisions": ("CREATE INDEX IF NOT EXISTS ix_policy_decisions_gateway_id "
                             "ON policy_decisions (gateway_id)"),
    }
    indexes["policy_versions"] = (
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_policy_versions_policy_version "
        "ON policy_versions (policy_id, version)"
    )
    live_tables = set(inspect(bind).get_table_names())
    if "policies" in live_tables:
        with bind.begin() as conn:
            conn.execute(text("DROP INDEX IF EXISTS uq_policies_workspace_name"))
    for table, ddl in indexes.items():
        if table in live_tables:
            with bind.begin() as conn:
                conn.execute(text(ddl))
    for table, ddl in lookups.items():
        if table in live_tables:
            with bind.begin() as conn:
                conn.execute(text(ddl))


def _migrate_registry_records_index(bind) -> None:
    """The unique (workspace, name, record_version) index of the local Registry (N4).
    `create_all` builds it with a fresh table; this restores it on an upgraded ledger
    whose `workspace_id` column was just re-added."""
    from sqlalchemy import inspect, text

    if "registry_records" not in inspect(bind).get_table_names():
        return
    with bind.begin() as conn:
        conn.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_registry_records_ws_name_version "
                "ON registry_records (workspace_id, name, record_version)"
            )
        )
        conn.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_registry_records_ws_type_status "
                "ON registry_records (workspace_id, record_type, status)"
            )
        )


def _migrate_registry_search_index(bind) -> None:
    """FTS5 side table + sync triggers for the local Registry (N4). Idempotent; backfills
    from `registry_records` the one time it creates the table. A SQLite build without
    the FTS5 module gets a warning and no table — `services/registry_local.py` then
    searches with LIKE (`fts_index_present()` is how it finds out)."""
    from sqlalchemy import inspect, text
    from sqlalchemy.exc import OperationalError

    tables = set(inspect(bind).get_table_names())
    if "registry_records" not in tables:
        return
    fresh = "registry_records_fts" not in tables
    try:
        with bind.begin() as conn:
            conn.execute(text(_REGISTRY_FTS_DDL))
    except OperationalError as exc:
        if "fts5" in str(exc).lower():
            logging.getLogger("launchpad").warning(
                "SQLite has no FTS5 module — registry search falls back to LIKE"
            )
            return
        raise
    with bind.begin() as conn:
        for ddl in _REGISTRY_FTS_TRIGGERS:
            conn.execute(text(ddl))
        if fresh:
            conn.execute(text(_REGISTRY_FTS_BACKFILL))


def _migrate_system_key_index(bind) -> None:
    """The partial unique index that arbitrates concurrent preset installs.

    Runs after the column migrations because it spans `workspace_id` (added by
    `_migrate_workspace_columns`) and `system_key`. A table created by an older
    release has neither the column nor the index; `create_all` on a fresh ledger
    builds both from the model.
    """
    from sqlalchemy import inspect, text

    if "agents" not in inspect(bind).get_table_names():
        return
    with bind.begin() as conn:
        conn.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_agents_workspace_system_key "
                "ON agents (workspace_id, system_key) "
                "WHERE system_key IS NOT NULL AND status != 'deleted'"
            )
        )


def _migrate_assistant_columns(bind) -> None:
    """Additive columns of the assistant tables (SE-039 correction): the immutable
    owner principal (NULL = visible to nobody, never adopted by username), the
    turn claim and the revision allocator, the proposal bindings, the
    admin-published share and the author of each user message."""
    from sqlalchemy import inspect, text

    additions = {
        "assistant_conversations": {
            "owner_principal": (
                "ALTER TABLE assistant_conversations ADD COLUMN owner_principal VARCHAR(96)"
            ),
            "active_turn": "ALTER TABLE assistant_conversations ADD COLUMN active_turn INTEGER",
            "active_turn_started_at": (
                "ALTER TABLE assistant_conversations ADD COLUMN active_turn_started_at DATETIME"
            ),
            "revision_seq": (
                "ALTER TABLE assistant_conversations ADD COLUMN revision_seq INTEGER DEFAULT 0"
            ),
            "active_turn_token": (
                "ALTER TABLE assistant_conversations ADD COLUMN active_turn_token VARCHAR(32)"
            ),
            "preparation": (
                "ALTER TABLE assistant_conversations ADD COLUMN preparation JSON DEFAULT '{}'"
            ),
            "preparation_sources": (
                "ALTER TABLE assistant_conversations ADD COLUMN preparation_sources "
                "JSON DEFAULT '[]'"
            ),
            "preparation_token": (
                "ALTER TABLE assistant_conversations ADD COLUMN preparation_token VARCHAR(32)"
            ),
            "shared": (
                "ALTER TABLE assistant_conversations ADD COLUMN shared BOOLEAN NOT NULL DEFAULT 0"
            ),
            "shared_by": "ALTER TABLE assistant_conversations ADD COLUMN shared_by VARCHAR(64)",
            "shared_at": "ALTER TABLE assistant_conversations ADD COLUMN shared_at DATETIME",
            "purge_job_id": (
                "ALTER TABLE assistant_conversations ADD COLUMN purge_job_id VARCHAR(32)"
            ),
        },
        "assistant_proposals": {
            "bindings": "ALTER TABLE assistant_proposals ADD COLUMN bindings JSON",
            # N15-obs13: revision-level repair notes; legacy rows read as []
            "notes": "ALTER TABLE assistant_proposals ADD COLUMN notes JSON DEFAULT '[]'",
        },
        "assistant_messages": {
            "author": "ALTER TABLE assistant_messages ADD COLUMN author VARCHAR(64)",
        },
        # N12 W-E5 (SE-047/CA-5): operations approved before identity pinning existed get
        # an EMPTY pin — the worker/cleanup refuse them (review required) instead of
        # inventing bindings
        "evaluation_asset_operations": {
            "pinned": "ALTER TABLE evaluation_asset_operations ADD COLUMN pinned JSON",
        },
    }
    inspector = inspect(bind)
    live_tables = set(inspector.get_table_names())
    for table, columns in additions.items():
        if table not in live_tables:
            continue
        existing = {c["name"] for c in inspector.get_columns(table)}
        for column, ddl in columns.items():
            if column not in existing:
                with bind.begin() as conn:
                    conn.execute(text(ddl))


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
        "jobs": "ALTER TABLE jobs ADD COLUMN workspace_id VARCHAR(32)",
        "assistant_conversations": (
            "ALTER TABLE assistant_conversations ADD COLUMN workspace_id VARCHAR(32)"
        ),
        "assistant_messages": (
            "ALTER TABLE assistant_messages ADD COLUMN workspace_id VARCHAR(32)"
        ),
        "assistant_proposals": (
            "ALTER TABLE assistant_proposals ADD COLUMN workspace_id VARCHAR(32)"
        ),
        "assistant_turn_events": (
            "ALTER TABLE assistant_turn_events ADD COLUMN workspace_id VARCHAR(32)"
        ),
        "chat_turn_events": (  # N-ARCH-obs15 v2
            "ALTER TABLE chat_turn_events ADD COLUMN workspace_id VARCHAR(32)"
        ),
        "agent_name_claims": "ALTER TABLE agent_name_claims ADD COLUMN workspace_id VARCHAR(32)",
        "assistant_evaluation_plans": (
            "ALTER TABLE assistant_evaluation_plans ADD COLUMN workspace_id VARCHAR(32)"
        ),
        "evaluation_asset_operations": (
            "ALTER TABLE evaluation_asset_operations ADD COLUMN workspace_id VARCHAR(32)"
        ),
        "system_skill_records": (
            "ALTER TABLE system_skill_records ADD COLUMN workspace_id VARCHAR(32)"
        ),
        "skill_lab_tasksets": "ALTER TABLE skill_lab_tasksets ADD COLUMN workspace_id VARCHAR(32)",
        "skill_lab_jobs": "ALTER TABLE skill_lab_jobs ADD COLUMN workspace_id VARCHAR(32)",
        "managed_memories": "ALTER TABLE managed_memories ADD COLUMN workspace_id VARCHAR(32)",
        "identity_providers": (
            "ALTER TABLE identity_providers ADD COLUMN workspace_id VARCHAR(32)"
        ),
        "user_token_revocations": (
            "ALTER TABLE user_token_revocations ADD COLUMN workspace_id VARCHAR(32)"
        ),
        "oauth_pending_sessions": (
            "ALTER TABLE oauth_pending_sessions ADD COLUMN workspace_id VARCHAR(32)"
        ),
        "user_grants": "ALTER TABLE user_grants ADD COLUMN workspace_id VARCHAR(32)",
        "resource_creators": (
            "ALTER TABLE resource_creators ADD COLUMN workspace_id VARCHAR(32)"
        ),
        "registry_records": "ALTER TABLE registry_records ADD COLUMN workspace_id VARCHAR(32)",
        # born with the column (upstream #277); listed so the scoped-tables drift test
        # holds and the index is ensured
        "spec_snapshots": "ALTER TABLE spec_snapshots ADD COLUMN workspace_id VARCHAR(32)",
        # --- CN memory/KB (N6) ---
        "memory_stores": "ALTER TABLE memory_stores ADD COLUMN workspace_id VARCHAR(32)",
        "knowledge_bases": "ALTER TABLE knowledge_bases ADD COLUMN workspace_id VARCHAR(32)",
        # --- end CN memory/KB (N6) ---
        # --- CN evaluation / optimization (N12) ---
        "eval_datasets": "ALTER TABLE eval_datasets ADD COLUMN workspace_id VARCHAR(32)",
        "eval_runs": "ALTER TABLE eval_runs ADD COLUMN workspace_id VARCHAR(32)",
        "eval_recommendations": (
            "ALTER TABLE eval_recommendations ADD COLUMN workspace_id VARCHAR(32)"
        ),
        "eval_pipelines": "ALTER TABLE eval_pipelines ADD COLUMN workspace_id VARCHAR(32)",
        "online_eval_configs": (
            "ALTER TABLE online_eval_configs ADD COLUMN workspace_id VARCHAR(32)"
        ),
        "experiments": "ALTER TABLE experiments ADD COLUMN workspace_id VARCHAR(32)",
        "runtime_canaries": "ALTER TABLE runtime_canaries ADD COLUMN workspace_id VARCHAR(32)",
        "eval_evaluators": "ALTER TABLE eval_evaluators ADD COLUMN workspace_id VARCHAR(32)",
        "eval_results": "ALTER TABLE eval_results ADD COLUMN workspace_id VARCHAR(32)",
        "online_samples": "ALTER TABLE online_samples ADD COLUMN workspace_id VARCHAR(32)",
        "config_bundles": "ALTER TABLE config_bundles ADD COLUMN workspace_id VARCHAR(32)",
        "experiment_assignments": (
            "ALTER TABLE experiment_assignments ADD COLUMN workspace_id VARCHAR(32)"
        ),
        # --- end CN evaluation / optimization (N12) ---
        # --- self-hosted Policy (D20) ---
        # born with the column (Global policy model, 2026-10-28); listed for the drift test
        "policy_engines": "ALTER TABLE policy_engines ADD COLUMN workspace_id VARCHAR(32)",
        "policies": "ALTER TABLE policies ADD COLUMN workspace_id VARCHAR(32)",
        "policy_versions": "ALTER TABLE policy_versions ADD COLUMN workspace_id VARCHAR(32)",
        "policy_settings": "ALTER TABLE policy_settings ADD COLUMN workspace_id VARCHAR(32)",
        "policy_changes": "ALTER TABLE policy_changes ADD COLUMN workspace_id VARCHAR(32)",
        "policy_decisions": "ALTER TABLE policy_decisions ADD COLUMN workspace_id VARCHAR(32)",
        "eval_token_files": "ALTER TABLE eval_token_files ADD COLUMN workspace_id VARCHAR(32)",
        # --- end self-hosted Policy (D20) ---
        # --- model registry (D21) ---
        "workspace_model_settings": (
            "ALTER TABLE workspace_model_settings ADD COLUMN workspace_id VARCHAR(32)"
        ),
        # --- end model registry (D21) ---
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
    bootstrap` and later bootstrap steps rewrite `launchpad.yaml` *after* the first seed, so
    a frozen snapshot would keep serving a stale identity — a fresh clone would
    pin `account_id=""` forever and the real account would later arrive as a
    *second* workspace beside the bogus default. Settings therefore stays
    authoritative for `default` (and only `default`) until the console owns the
    row; non-default workspaces are row-authoritative from birth.

    Raw SQL on purpose: this module cannot import the models (they import `Base`
    from here). Idempotent — the mirror converges on settings, and the row
    adoption runs only on the insert (see below).
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
