"""The ledger must survive an in-place upgrade, not just a fresh create.

`conftest.py` points every test at a brand-new SQLite file, so `create_all` builds
each table with the current model columns and a missing `_migrate` entry is invisible.
These tests build a database the way a *deployed* box has one — tables created by an
older release — and assert startup upgrades it instead of 500-ing later.
"""

import sqlalchemy as sa

from app.core import db as db_module
from app.core.db import Base, assert_no_schema_drift, schema_drift


def _engine(tmp_path, name="ledger.db"):
    return sa.create_engine(f"sqlite:///{tmp_path / name}")


def _added_columns() -> set[tuple[str, str]]:
    """(table, column) pairs `_migrate` knows how to add, read off its own DDL."""
    import inspect as py_inspect
    import re

    source = py_inspect.getsource(db_module._migrate)
    return set(
        re.findall(r"ALTER TABLE (\w+) ADD COLUMN (\w+)", source)
    )


def test_migrate_declares_every_column_missing_from_an_older_table(tmp_path):
    """Drop each column `_migrate` handles, then prove `_migrate` puts it back.

    This is the round trip that a deployed upgrade performs.
    """
    engine = _engine(tmp_path)
    Base.metadata.create_all(bind=engine)
    pairs = _added_columns()
    assert pairs, "no ALTER statements found — did _migrate change shape?"

    with engine.begin() as conn:
        for table, column in sorted(pairs):
            conn.execute(sa.text(f"ALTER TABLE {table} DROP COLUMN {column}"))

    stale = schema_drift(engine)
    for table, column in pairs:
        assert column in stale.get(table, []), f"{table}.{column} was not dropped"

    db_module._migrate(engine)
    assert schema_drift(engine) == {}


def test_image_digest_is_added_to_an_existing_deployments_table(tmp_path):
    """Regression: the supply-chain work added this column with no migration entry,
    which made `/api/agents` 500 on every pre-existing ledger."""
    engine = _engine(tmp_path)
    Base.metadata.create_all(bind=engine)
    with engine.begin() as conn:
        conn.execute(sa.text("ALTER TABLE deployments DROP COLUMN image_digest"))

    db_module._migrate(engine)

    with engine.begin() as conn:
        # The failing request was a SELECT over the mapped columns.
        conn.execute(sa.select(Base.metadata.tables["deployments"]))


def test_schema_drift_names_the_missing_column(tmp_path):
    engine = _engine(tmp_path)
    Base.metadata.create_all(bind=engine)
    with engine.begin() as conn:
        conn.execute(sa.text("ALTER TABLE agents DROP COLUMN registry_record_id"))

    assert schema_drift(engine) == {"agents": ["registry_record_id"]}

    try:
        assert_no_schema_drift(engine)
    except RuntimeError as exc:
        assert "agents" in str(exc) and "registry_record_id" in str(exc)
        assert "_migrate()" in str(exc)
    else:  # pragma: no cover - the guard must fire
        raise AssertionError("assert_no_schema_drift did not raise")


def test_a_fresh_database_has_no_drift(tmp_path):
    engine = _engine(tmp_path)
    Base.metadata.create_all(bind=engine)
    db_module._migrate(engine)
    assert_no_schema_drift(engine)


def test_migrate_is_idempotent(tmp_path):
    engine = _engine(tmp_path)
    Base.metadata.create_all(bind=engine)
    db_module._migrate(engine)
    db_module._migrate(engine)
    assert_no_schema_drift(engine)
