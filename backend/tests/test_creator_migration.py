"""The one-time creator re-point (Family B step 1, `core.db._migrate_creator_columns`).

Per table the ADD COLUMN(s), the re-point and (agents) the owner copy + DROP run in ONE
transaction, and the re-point is data-driven (rows still carrying a legacy value with no
display name) — so a ledger left half-migrated is finished on the next start, never skipped
(rev-r11 must-fix: usernames left in `api_keys.created_by` would read as deleted creators
and 401 every legacy /v1 key).

A deployed ledger carried a USERNAME in `created_by` on five tables (api_keys,
managed_memories, memory_stores, knowledge_bases, identity_providers) and the pre-#261
`agents.owner` username column. Startup moves the username to `created_by_name`,
resolves `created_by` to the matching `users.id` (NULL when no account has that name),
clears the owner id of adopted memories, drops `agents.owner`, and does all of it exactly
once — a second startup (or a ledger that already has `created_by_name`) is a no-op.
"""

import pytest
import sqlalchemy as sa

from app.core import db as db_module
from app.core.db import Base, assert_no_schema_drift

NOW = "2026-01-01 00:00:00"


def _legacy_ledger(tmp_path):
    """Today's tables, then regressed to the pre-Family-B shape with rows in them."""
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'ledger.db'}")
    Base.metadata.create_all(bind=engine)
    with engine.begin() as conn:
        for table in ("api_keys", "managed_memories", "memory_stores", "knowledge_bases",
                      "identity_providers", "agents", "chat_sessions"):
            conn.execute(sa.text(f"DROP INDEX ix_{table}_created_by"))
            conn.execute(sa.text(f"ALTER TABLE {table} DROP COLUMN created_by_name"))
        for table in ("agents", "chat_sessions"):
            conn.execute(sa.text(f"ALTER TABLE {table} DROP COLUMN created_by"))
        conn.execute(sa.text("ALTER TABLE agents ADD COLUMN owner VARCHAR(64)"))
        # two accounts; `Ada` is stored as typed, matched case-insensitively on username_key
        for uid, name in (("u-ada", "Ada"), ("u-bob", "bob")):
            conn.execute(sa.text(
                "INSERT INTO users (id, username, username_key, email, password_hash, role,"
                " status, created_by, login_count, created_at, updated_at) VALUES"
                f" ('{uid}', '{name}', '{name.lower()}', '{name.lower()}@x.cn', 'h', 'member',"
                f" 'active', 'self', 0, '{NOW}', '{NOW}')"
            ))
        conn.execute(sa.text(
            "INSERT INTO api_keys (id, workspace_id, name, prefix, key_hash, enabled,"
            " created_by, created_at) VALUES"
            f" ('k-ada', 'default', 'a', 'p', 'h1', 1, 'ada', '{NOW}'),"
            f" ('k-gone', 'default', 'g', 'p', 'h2', 1, 'ghost', '{NOW}'),"
            f" ('k-null', 'default', 'n', 'p', 'h3', 1, NULL, '{NOW}')"
        ))
        conn.execute(sa.text(
            "INSERT INTO managed_memories (id, workspace_id, memory_id, origin, created_by,"
            " created_at) VALUES"
            f" ('m-created', 'default', 'mem-1', 'created', 'bob', '{NOW}'),"
            f" ('m-adopted', 'default', 'mem-2', 'adopted', 'bob', '{NOW}')"
        ))
        conn.execute(sa.text(
            "INSERT INTO memory_stores (id, workspace_id, memory_id, name, description, status,"
            " event_expiry_days, strategies, namespace_keys, created_by, created_at, updated_at)"
            f" VALUES ('s1', 'default', 'mem-1', 'store', '', 'ACTIVE', 30, '[]', '[]', NULL,"
            f" '{NOW}', '{NOW}')"
        ))
        conn.execute(sa.text(
            "INSERT INTO knowledge_bases (kb_id, workspace_id, name, description, status,"
            " failure_reasons, data_sources, created_by, created_at, updated_at) VALUES"
            f" ('KB1', 'default', 'kb', '', 'ACTIVE', '[]', '[]', 'Ada', '{NOW}', '{NOW}')"
        ))
        conn.execute(sa.text(
            "INSERT INTO identity_providers (id, workspace_id, name, kind, vendor, arn,"
            " created_by, created_at) VALUES"
            f" ('i-admin', 'default', 'github', 'oauth2', 'Custom', 'arn', 'operator', '{NOW}'),"
            f" ('i-blank', 'default', 'slack', 'api_key', '', 'arn2', '', '{NOW}')"
        ))
        conn.execute(sa.text(
            "INSERT INTO agents (id, workspace_id, name, method, status, spec, owner,"
            " created_at, updated_at) VALUES"
            + ", ".join(
                f" ('{aid}', 'default', '{name}', '{method}', 'active', '{{}}', '{owner}',"
                f" '{NOW}', '{NOW}')"
                for aid, name, method, owner in (
                    ("a-ada", "hr", "zip_runtime", "ada"),
                    ("a-river", "old", "zip_runtime", "river"),
                    ("a-sys", "preset", "harness", "system"),
                    ("a-disc", "ext", "discovered_runtime", "aws-discovery"),
                    ("a-gone", "orphan", "zip_runtime", "ghost"),
                )
            )
        ))
    return engine


def _rows(engine, sql: str) -> dict:
    with engine.begin() as conn:
        return {row[0]: tuple(row[1:]) for row in conn.execute(sa.text(sql))}


def test_legacy_usernames_are_re_pointed_to_accounts_once(tmp_path):
    engine = _legacy_ledger(tmp_path)

    db_module._migrate(engine)
    assert_no_schema_drift(engine)

    keys = _rows(engine, "SELECT id, created_by, created_by_name FROM api_keys")
    assert keys == {
        "k-ada": ("u-ada", "ada"),  # resolved case-insensitively
        "k-gone": (None, "ghost"),  # no such account any more: name kept, no owner id
        "k-null": (None, None),  # legacy NULL stays unknown
    }
    memories = _rows(engine, "SELECT id, created_by, created_by_name FROM managed_memories")
    assert memories == {
        "m-created": ("u-bob", "bob"),
        "m-adopted": (None, "bob"),  # adopted: the adopter is not the owner
    }
    assert _rows(engine, "SELECT id, created_by, created_by_name FROM memory_stores") == {
        "s1": (None, None),
    }
    assert _rows(engine, "SELECT kb_id, created_by, created_by_name FROM knowledge_bases") == {
        "KB1": ("u-ada", "Ada"),
    }
    assert _rows(engine, "SELECT id, created_by, created_by_name FROM identity_providers") == {
        "i-admin": (None, "operator"),  # the config admin: name only
        "i-blank": (None, None),  # the `""` default meant unknown
    }
    agents = _rows(engine, "SELECT id, created_by, created_by_name FROM agents")
    assert agents == {
        "a-ada": ("u-ada", "ada"),
        "a-river": (None, None),  # the pre-obs9 column default never named anyone
        "a-sys": (None, None),  # presets and discovered runtimes record no creator
        "a-disc": (None, None),
        "a-gone": (None, "ghost"),
    }
    columns = {c["name"] for c in sa.inspect(engine).get_columns("agents")}
    assert "owner" not in columns  # retired (C5)
    for table in ("api_keys", "agents", "knowledge_bases", "chat_sessions"):
        names = {i["name"] for i in sa.inspect(engine).get_indexes(table)}
        assert f"ix_{table}_created_by" in names, table


def test_the_re_point_runs_only_once(tmp_path):
    engine = _legacy_ledger(tmp_path)
    db_module._migrate(engine)
    # an operator fixes a creator by hand; a second startup must not undo it
    with engine.begin() as conn:
        conn.execute(sa.text("UPDATE api_keys SET created_by = 'u-bob', created_by_name = 'bob'"
                             " WHERE id = 'k-gone'"))
        conn.execute(sa.text("DELETE FROM users WHERE id = 'u-ada'"))
    db_module._migrate(engine)
    assert_no_schema_drift(engine)
    keys = _rows(engine, "SELECT id, created_by, created_by_name FROM api_keys")
    assert keys["k-gone"] == ("u-bob", "bob")
    assert keys["k-ada"] == ("u-ada", "ada")  # a dangling id is kept: it marks a deleted creator


def test_a_ledger_without_a_users_table_records_names_only(tmp_path):
    engine = _legacy_ledger(tmp_path)
    with engine.begin() as conn:
        conn.execute(sa.text("DROP TABLE user_workspaces"))
        conn.execute(sa.text("DROP TABLE users"))
    db_module._migrate(engine)
    keys = _rows(engine, "SELECT id, created_by, created_by_name FROM api_keys")
    assert keys["k-ada"] == (None, "ada")


EXPECTED_KEYS = {"k-ada": ("u-ada", "ada"), "k-gone": (None, "ghost"), "k-null": (None, None)}
EXPECTED_AGENTS = {
    "a-ada": ("u-ada", "ada"), "a-river": (None, None), "a-sys": (None, None),
    "a-disc": (None, None), "a-gone": (None, "ghost"),
}


def _half_migrated(engine, *, agents_too: bool) -> None:
    """A ledger as a crash right after the ADD COLUMN would have left it before the
    atomicity fix: `created_by_name` exists, the legacy usernames still sit in
    `created_by` (and `agents.owner`), nothing was re-pointed."""
    with engine.begin() as conn:
        for table in ("api_keys", "managed_memories", "memory_stores", "knowledge_bases",
                      "identity_providers"):
            conn.execute(sa.text(f"ALTER TABLE {table} ADD COLUMN created_by_name VARCHAR(64)"))
        if agents_too:
            conn.execute(sa.text("ALTER TABLE agents ADD COLUMN created_by VARCHAR(32)"))
            conn.execute(sa.text("ALTER TABLE agents ADD COLUMN created_by_name VARCHAR(64)"))


def test_a_crash_after_the_add_column_is_finished_on_the_next_start(tmp_path):
    engine = _legacy_ledger(tmp_path)
    _half_migrated(engine, agents_too=True)

    db_module._migrate(engine)
    assert_no_schema_drift(engine)

    assert _rows(engine, "SELECT id, created_by, created_by_name FROM api_keys") == EXPECTED_KEYS
    assert _rows(engine, "SELECT id, created_by, created_by_name FROM managed_memories") == {
        "m-created": ("u-bob", "bob"), "m-adopted": (None, "bob"),
    }
    assert _rows(engine, "SELECT id, created_by, created_by_name FROM identity_providers") == {
        "i-admin": (None, "operator"), "i-blank": (None, None),
    }
    assert _rows(engine, "SELECT id, created_by, created_by_name FROM agents") == EXPECTED_AGENTS
    assert "owner" not in {c["name"] for c in sa.inspect(engine).get_columns("agents")}
    # and a key the half-migrated state would have read as "deleted creator" is sound
    assert EXPECTED_KEYS["k-ada"][0] == "u-ada"


def test_agents_owner_copy_never_overwrites_a_row_already_stamped(tmp_path):
    """`owner` still present AND `created_by_name` present (a row this build stamped
    after a partial upgrade): the stamped row is kept, the legacy rows are copied, the
    column is dropped — in the same transaction."""
    engine = _legacy_ledger(tmp_path)
    _half_migrated(engine, agents_too=True)
    with engine.begin() as conn:
        conn.execute(sa.text("UPDATE agents SET created_by = 'u-bob', created_by_name = 'bob'"
                             " WHERE id = 'a-ada'"))  # stamped by this build, owner says 'ada'

    db_module._migrate(engine)

    agents = _rows(engine, "SELECT id, created_by, created_by_name FROM agents")
    assert agents == {**EXPECTED_AGENTS, "a-ada": ("u-bob", "bob")}
    assert "owner" not in {c["name"] for c in sa.inspect(engine).get_columns("agents")}
    db_module._migrate(engine)  # idempotent
    assert _rows(engine, "SELECT id, created_by, created_by_name FROM agents") == agents


def test_each_table_upgrades_in_one_transaction(tmp_path, monkeypatch):
    """A failure inside a table's re-point rolls its ADD COLUMN back too: the next start
    sees the pre-upgrade shape and redoes the whole step instead of skipping it."""
    engine = _legacy_ledger(tmp_path)
    real = db_module._repoint_username_creator
    calls = {"n": 0}

    def boom(conn, table, source, has_users, **kw):
        calls["n"] += 1
        if table == "api_keys":
            raise RuntimeError("simulated crash mid re-point")
        return real(conn, table, source, has_users, **kw)

    monkeypatch.setattr(db_module, "_repoint_username_creator", boom)
    with pytest.raises(RuntimeError):
        db_module._migrate(engine)
    columns = {c["name"] for c in sa.inspect(engine).get_columns("api_keys")}
    assert "created_by_name" not in columns  # the ADD was rolled back with the re-point
    with engine.begin() as conn:
        assert conn.execute(sa.text("SELECT created_by FROM api_keys WHERE id = 'k-ada'")
                            ).scalar_one() == "ada"

    monkeypatch.setattr(db_module, "_repoint_username_creator", real)
    db_module._migrate(engine)
    assert _rows(engine, "SELECT id, created_by, created_by_name FROM api_keys") == EXPECTED_KEYS
