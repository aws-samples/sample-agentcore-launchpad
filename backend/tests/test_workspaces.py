"""Workspace core: schema upgrade, default-workspace seeding, context plumbing.

The reflection-based tests in `test_ledger_migration.py` see `ALTER TABLE`
statements only — they cannot see the backfill `UPDATE`s, the indexes, or the
seeded row, so the data half of the migration is covered here.
"""

import json
from datetime import datetime
from types import SimpleNamespace

import pytest
import sqlalchemy as sa

from app.core import db as db_module
from app.core.db import DEFAULT_WORKSPACE_ID, WORKSPACE_SCOPED_TABLES, Base, SessionLocal
from app.models.ledger import User, UserWorkspace, Workspace
from app.services import aws_clients
from app.services import workspace as ws

NOW = "2026-08-01 00:00:00.000000"


class _Settings:
    region = "eu-central-1"
    account_id = "111122223333"
    resources = {"artifacts_bucket": "seeded-bucket"}


@pytest.fixture
def hub_settings(monkeypatch):
    monkeypatch.setattr(db_module, "get_settings", lambda: _Settings())
    return _Settings()


@pytest.fixture
def settings(monkeypatch):
    """Hub settings a test can edit between `init_db` runs — the real thing changes
    the same way (`make bootstrap` and kb_gateway rewrite `launchpad.yaml`)."""
    stub = SimpleNamespace(account_id="", region="us-west-2", resources={})
    monkeypatch.setattr(db_module, "get_settings", lambda: stub)
    return stub


def _pre_p2_database(tmp_path):
    """A ledger as a pre-workspace release left it: no `workspaces` table and no
    `workspace_id` column anywhere, carrying rows that must survive the upgrade."""
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'ledger.db'}")
    Base.metadata.create_all(bind=engine)
    with engine.begin() as conn:
        for table in WORKSPACE_SCOPED_TABLES:
            conn.execute(sa.text(f"DROP INDEX ix_{table}_workspace_id"))
            conn.execute(sa.text(f"ALTER TABLE {table} DROP COLUMN workspace_id"))
        conn.execute(sa.text("DROP TABLE user_workspaces"))
        conn.execute(sa.text("DROP TABLE workspaces"))
        conn.execute(
            sa.text(
                "INSERT INTO agents (id, name, method, status, spec, owner,"
                " created_at, updated_at)"
                " VALUES ('a1', 'legacy', 'harness', 'active', '{}', 'river', :now, :now)"
            ),
            {"now": NOW},
        )
        conn.execute(
            sa.text(
                "INSERT INTO chat_messages (agent_id, session_id, role, text, created_at)"
                " VALUES ('a1', 's1', 'user', 'hi', :now)"
            ),
            {"now": NOW},
        )
        conn.execute(
            sa.text(
                "INSERT INTO jobs (id, type, status, payload, log, created_at, updated_at)"
                " VALUES ('j1', 'deploy_agent', 'succeeded', '{}', '', :now, :now)"
            ),
            {"now": NOW},
        )
        # the row with frozen audit fields: the backfill must reach it too, which
        # is why it is raw SQL and why workspace_id is not immutable
        conn.execute(
            sa.text(
                "INSERT INTO policy_changes (id, gateway_id, gateway_arn, gateway_name,"
                " operation, operator, status, before, requested, created_at)"
                " VALUES ('p1', 'gw-1', 'arn:gw', 'launchpad-gw', 'attach_engine',"
                " 'admin', 'succeeded', '{}', '{}', :now)"
            ),
            {"now": NOW},
        )
    return engine


def _null_counts(engine) -> dict[str, int]:
    with engine.begin() as conn:
        return {
            table: conn.execute(
                sa.text(f"SELECT COUNT(*) FROM {table} WHERE workspace_id IS NULL")
            ).scalar_one()
            for table in WORKSPACE_SCOPED_TABLES
        }


def _default_row(engine):
    with engine.begin() as conn:
        return conn.execute(
            sa.text(
                "SELECT account_id, region, bootstrap_status, resources, created_at,"
                " updated_at FROM workspaces WHERE id = :id"
            ),
            {"id": DEFAULT_WORKSPACE_ID},
        ).one()


def _index_names(engine, table) -> set[str]:
    with engine.begin() as conn:
        return {row[1] for row in conn.exec_driver_sql(f"PRAGMA index_list({table})")}


def test_upgrading_a_pre_workspace_database_adopts_every_row(tmp_path, hub_settings):
    engine = _pre_p2_database(tmp_path)

    db_module.init_db(engine)

    assert db_module.schema_drift(engine) == {}
    assert _null_counts(engine) == dict.fromkeys(WORKSPACE_SCOPED_TABLES, 0)
    with engine.begin() as conn:
        row = conn.execute(
            sa.text(
                "SELECT name, account_id, region, role_arn, bootstrap_status, resources"
                " FROM workspaces WHERE id = :id"
            ),
            {"id": DEFAULT_WORKSPACE_ID},
        ).one()
        assert row.name == "Default"
        assert (row.account_id, row.region) == (hub_settings.account_id, hub_settings.region)
        assert row.role_arn is None  # same-account until phase 3
        assert row.bootstrap_status == "ready"
        assert '"artifacts_bucket": "seeded-bucket"' in row.resources
        # pre-existing rows are intact, not recreated
        assert conn.execute(sa.text("SELECT name FROM agents WHERE id = 'a1'")).scalar_one() == (
            "legacy"
        )


def test_the_seeded_row_reads_back_through_the_orm(tmp_path, hub_settings):
    """The raw-SQL INSERT must produce a row the mapped types can load — a
    mis-serialized JSON or datetime only shows up on the read side."""
    engine = _pre_p2_database(tmp_path)
    db_module.init_db(engine)

    with sa.orm.Session(engine) as db:
        row = db.get(Workspace, DEFAULT_WORKSPACE_ID)
        assert row.resources == hub_settings.resources
        # a datetime, not the string that was written: the hand-written literal has
        # to match the format SQLAlchemy's SQLite DateTime reader expects
        assert isinstance(row.created_at, datetime)
        assert isinstance(row.updated_at, datetime)
        assert ws.workspace_context(row).region == hub_settings.region


def test_workspace_id_is_indexed_on_an_upgraded_table(tmp_path, hub_settings):
    """`schema_drift` compares column names only, so a dropped index is silent."""
    engine = _pre_p2_database(tmp_path)

    db_module.init_db(engine)

    for table in WORKSPACE_SCOPED_TABLES:
        assert f"ix_{table}_workspace_id" in _index_names(engine, table)


def test_seeding_is_idempotent_and_leaves_explicit_assignments_alone(tmp_path, hub_settings):
    engine = _pre_p2_database(tmp_path)
    db_module.init_db(engine)
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO agents (id, name, method, status, spec, owner, workspace_id,"
                " created_at, updated_at)"
                " VALUES ('a2', 'other-ws', 'harness', 'active', '{}', 'river', 'acct2-usw2',"
                " :now, :now)"
            ),
            {"now": NOW},
        )

    db_module.init_db(engine)

    with engine.begin() as conn:
        assert conn.execute(sa.text("SELECT COUNT(*) FROM workspaces")).scalar_one() == 1
        assert conn.execute(
            sa.text("SELECT workspace_id FROM agents WHERE id = 'a2'")
        ).scalar_one() == ("acct2-usw2")
    assert _null_counts(engine) == dict.fromkeys(WORKSPACE_SCOPED_TABLES, 0)


def test_the_default_row_follows_settings_from_a_fresh_clone_to_bootstrapped(tmp_path, settings):
    """A fresh clone has no account and no resources; `make bootstrap` fills both
    in afterwards. Seeding once would pin the empty identity — and claim "ready"
    for an environment that has nothing provisioned."""
    engine = _pre_p2_database(tmp_path)

    db_module.init_db(engine)

    first = _default_row(engine)
    assert (first.account_id, first.bootstrap_status) == ("", "registered")

    settings.account_id = "434444145045"
    settings.region = "us-west-2"
    settings.resources = {"artifacts_bucket": "launchpad-artifacts", "gateway_id": "gw-1"}
    db_module.init_db(engine)

    with engine.begin() as conn:
        assert conn.execute(sa.text("SELECT COUNT(*) FROM workspaces")).scalar_one() == 1
    row = _default_row(engine)
    assert (row.account_id, row.region) == ("434444145045", "us-west-2")
    assert row.bootstrap_status == "ready"
    assert json.loads(row.resources) == settings.resources
    assert row.created_at == first.created_at  # updated in place, not recreated
    assert row.updated_at > first.updated_at


def test_the_default_row_follows_a_later_resource_map_change(tmp_path, settings):
    """kb_gateway provisions lazily and rewrites launchpad.yaml long after the
    first seed, so the row has to converge on the newer map."""
    settings.account_id = "434444145045"
    settings.resources = {"gateway_id": "gw-1"}
    engine = _pre_p2_database(tmp_path)
    db_module.init_db(engine)
    assert _default_row(engine).bootstrap_status == "ready"

    settings.resources = {"gateway_id": "gw-1", "kb_gateway_id": "kbgw-2"}
    db_module.init_db(engine)

    assert json.loads(_default_row(engine).resources) == settings.resources


def test_the_mirror_only_touches_the_default_row(tmp_path, settings):
    """Settings describes the hub environment only; every other workspace is
    authoritative in its own row."""
    settings.account_id = "434444145045"
    settings.resources = {"gateway_id": "gw-1"}
    engine = _pre_p2_database(tmp_path)
    db_module.init_db(engine)
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO workspaces (id, name, account_id, region, role_arn,"
                " external_id, bootstrap_status, resources, created_at, updated_at)"
                " VALUES ('acct2-use2', 'Second', '444455556666', 'us-east-2', NULL, NULL,"
                " 'bootstrapping', '{\"gateway_id\": \"gw-2\"}', :now, :now)"
            ),
            {"now": NOW},
        )

    settings.resources = {"gateway_id": "gw-1", "kb_gateway_id": "kbgw-2"}
    db_module.init_db(engine)

    with engine.begin() as conn:
        second = conn.execute(
            sa.text(
                "SELECT account_id, region, bootstrap_status, resources, updated_at"
                " FROM workspaces WHERE id = 'acct2-use2'"
            )
        ).one()
    assert (second.account_id, second.region) == ("444455556666", "us-east-2")
    assert second.bootstrap_status == "bootstrapping"
    assert json.loads(second.resources) == {"gateway_id": "gw-2"}
    assert second.updated_at == NOW


def test_an_identity_collision_fails_startup_loudly(tmp_path, settings):
    """If a second workspace claimed the real (account, region) while default held
    a bogus one, silently picking a winner would split one environment across two
    workspaces. UNIQUE(account_id, region) turns it into a startup failure."""
    engine = _pre_p2_database(tmp_path)
    db_module.init_db(engine)
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO workspaces (id, name, account_id, region, role_arn,"
                " external_id, bootstrap_status, resources, created_at, updated_at)"
                " VALUES ('acct2', 'Second', '434444145045', 'us-west-2', NULL, NULL,"
                " 'ready', '{}', :now, :now)"
            ),
            {"now": NOW},
        )

    settings.account_id = "434444145045"
    settings.region = "us-west-2"
    with pytest.raises(sa.exc.IntegrityError):
        db_module.init_db(engine)


def test_migration_covers_exactly_the_workspace_scoped_tables():
    """The DDL list and the backfill list are separate literals; a table added to
    one and not the other would leave a column that is never populated."""
    import inspect as py_inspect
    import re

    source = py_inspect.getsource(db_module._migrate_workspace_columns)
    altered = set(re.findall(r"ALTER TABLE (\w+) ADD COLUMN workspace_id", source))
    assert altered == set(WORKSPACE_SCOPED_TABLES)


def test_the_models_carrying_workspace_id_are_exactly_the_scoped_tables():
    """Closes the loop on the other side: a model given the column but left off
    the list gets no migration (500 on a deployed box) and no backfill."""
    mapped = {
        name
        for name, table in Base.metadata.tables.items()
        if "workspace_id" in table.columns and name != "user_workspaces"  # the grant's own FK
    }
    assert mapped == set(WORKSPACE_SCOPED_TABLES)


def test_a_database_behind_the_models_is_not_seeded(tmp_path, hub_settings):
    """Ordering guarantee: the drift check runs before the seed, so an upgrade that
    cannot complete leaves no half-migrated, half-adopted ledger behind."""
    engine = _pre_p2_database(tmp_path)
    with engine.begin() as conn:
        # a model column no `_migrate` entry knows how to restore
        conn.execute(sa.text("ALTER TABLE agents DROP COLUMN spec"))

    with pytest.raises(RuntimeError, match="schema is behind the models"):
        db_module.init_db(engine)

    with engine.begin() as conn:
        assert conn.execute(sa.text("SELECT COUNT(*) FROM workspaces")).scalar_one() == 0
        assert conn.execute(
            sa.text("SELECT COUNT(*) FROM agents WHERE workspace_id IS NULL")
        ).scalar_one() == (1)


def test_seeding_refuses_to_run_against_unbuilt_metadata(tmp_path):
    """An out-of-process rehearsal that forgets to import the models gets an empty
    `Base.metadata`, so `create_all` builds nothing — that must be loud, because a
    silent skip looks exactly like a successful migration."""
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'empty.db'}")

    with pytest.raises(RuntimeError, match="import app.models.ledger"):
        db_module._seed_default_workspace(engine)


def test_one_workspace_per_account_and_region():
    db = SessionLocal()
    try:
        db.add(Workspace(id="a", name="A", account_id="1111", region="us-west-2"))
        db.commit()
        db.add(Workspace(id="b", name="B", account_id="1111", region="us-west-2"))
        with pytest.raises(sa.exc.IntegrityError):
            db.commit()
        db.rollback()
        db.add(Workspace(id="b", name="B", account_id="1111", region="us-east-1"))
        db.commit()
    finally:
        db.close()


def test_workspace_context_is_built_from_the_row():
    row = Workspace(
        id="acct2-usw2",
        name="Second",
        account_id="444455556666",
        region="us-east-2",
        resources={"gateway_id": "gw-2"},
    )

    ctx = ws.workspace_context(row)

    assert (ctx.account_id, ctx.region) == ("444455556666", "us-east-2")
    assert ctx.resources == {"gateway_id": "gw-2"}
    assert ctx.role_arn is None and ctx.external_id is None
    # a NULL resources column must not hand a None map to a resource lookup
    assert ws.workspace_context(
        Workspace(id="x", name="X", account_id="1", region="us-west-2", resources=None)
    ).resources == {}


def test_credentials_never_hands_out_the_session(monkeypatch):
    """Building clients off the session would bypass the factory's lock, cache and
    guard test — so the escape hatch returns credentials only."""
    frozen = object()
    monkeypatch.setattr(
        aws_clients,
        "get_session",
        lambda *a, **k: SimpleNamespace(
            get_credentials=lambda: SimpleNamespace(get_frozen_credentials=lambda: frozen)
        ),
    )
    ctx = ws.WorkspaceContext(account_id="1111", region="us-west-2")

    assert ctx.credentials() is frozen
    assert not hasattr(ctx, "session")


def test_grant_helpers():
    db = SessionLocal()
    try:
        db.add(Workspace(id="w1", name="W1", account_id="1111", region="us-west-2"))
        db.add(Workspace(id="w2", name="W2", account_id="1111", region="us-east-1"))
        user = User(
            id="u1",
            username="Member",
            username_key="member",
            email="member@example.com",
            password_hash="x",
        )
        db.add(user)
        db.commit()
        db.add_all(
            [
                UserWorkspace(user_id="u1", workspace_id="w2"),
                UserWorkspace(user_id="u1", workspace_id="w1"),
            ]
        )
        db.commit()

        assert ws.get_workspace_row(db, "w1").name == "W1"
        assert ws.get_workspace_row(db, "nope") is None
        assert ws.granted_workspace_ids(db, "u1") == ["w1", "w2"]
        assert ws.granted_workspace_ids(db, "u-none") == []
    finally:
        db.close()


# ── the no-NULL invariant (replaces the every-startup backfill) ──────────────


def _insert_unscoped_agent(engine, agent_id: str = "a-null") -> None:
    """An agent row a write path forgot to stamp."""
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO agents (id, name, method, status, spec, owner,"
                " workspace_id, created_at, updated_at)"
                f" VALUES ('{agent_id}', '{agent_id}', 'harness', 'active', '{{}}',"
                " 'river', NULL, :now, :now)"
            ),
            {"now": NOW},
        )


def test_a_row_with_no_workspace_fails_startup_and_names_its_table(tmp_path, hub_settings):
    """After the migration a NULL means a *new* write path is not stamping. Such a
    row is invisible to every scoped query, so refusing to boot beats hiding it."""
    engine = _pre_p2_database(tmp_path)
    db_module.init_db(engine)  # the upgrade adopts the pre-workspace rows

    _insert_unscoped_agent(engine)

    with pytest.raises(RuntimeError) as error:
        db_module.init_db(engine)
    assert "agents=1" in str(error.value)
    assert "workspace_id" in str(error.value)


def test_the_row_adoption_runs_only_on_the_migration(tmp_path, hub_settings):
    """The adopting UPDATE is one-shot: repeating it every startup would absorb the
    unstamped row above instead of surfacing it."""
    engine = _pre_p2_database(tmp_path)
    db_module.init_db(engine)
    _insert_unscoped_agent(engine)

    with pytest.raises(RuntimeError):
        db_module.init_db(engine)

    with engine.begin() as conn:
        adopted = conn.execute(
            sa.text("SELECT workspace_id FROM agents WHERE id = 'a-null'")
        ).scalar_one()
    assert adopted is None  # NOT quietly moved into `default`


def test_the_invariant_reports_every_offending_table(tmp_path, hub_settings):
    engine = _pre_p2_database(tmp_path)
    db_module.init_db(engine)
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO api_keys (id, name, prefix, key_hash, enabled,"
                " workspace_id, created_at)"
                " VALUES ('k1', 'k', 'lp…', 'hash', 1, NULL, :now)"
            ),
            {"now": NOW},
        )
    _insert_unscoped_agent(engine)

    assert db_module.unscoped_row_counts(engine) == {"agents": 1, "api_keys": 1}
