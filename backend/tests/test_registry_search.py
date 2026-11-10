"""Registry search: FTS5 side table + triggers (startup migration), ranked prefix search,
LIKE fallback when FTS5 is unavailable, workspace scoping, CJK substrings."""

import sqlite3

import pytest
import sqlalchemy as sa

from app.core import db as db_module
from app.core.db import Base
from app.services import registry_backend
from app.services.registry_local import FTS_TABLE, LocalRegistryBackend, fts_index_present
from tests.conftest import ws_ctx

HAS_FTS5 = True
try:
    sqlite3.connect(":memory:").execute("CREATE VIRTUAL TABLE t USING fts5(a)")
except sqlite3.OperationalError:
    HAS_FTS5 = False

needs_fts5 = pytest.mark.skipif(not HAS_FTS5, reason="this SQLite has no FTS5 module")


def _skill(name: str, body: str):
    return registry_backend.build_skills_descriptors(
        skill_md=f"---\nname: {name}\n---\n{body}",
        definition={"name": name, "description": body[:60], "path": f"s3://b/skills/{name}/"},
    )


def _seed(backend, ws):
    backend.upsert_record(ws, name="hr-database", description="HR MCP tools",
                          descriptor_type="MCP", descriptors=registry_backend.build_mcp_descriptors(
                              target="hr-database", description="HR", gateway_url="https://gw/mcp",
                              tools=[{"name": "hr-database___get_employee",
                                      "description": "employee lookup", "inputSchema": {}}]))
    backend.upsert_record(
        ws, name="expense-report-writer", description="Draft expense reports",
        descriptor_type="AGENT_SKILLS",
        descriptors=_skill("expense-report-writer", "# Expense reports\n费用报告"),
    )
    backend.upsert_record(
        ws, name="meeting-notes", description="Summaries",
        descriptor_type="AGENT_SKILLS",
        descriptors=_skill("meeting-notes", "# Meetings\nexpense is mentioned once"),
    )


@pytest.fixture
def ws():
    return ws_ctx()


@needs_fts5
def test_startup_migration_creates_the_fts_table_and_triggers(client):
    engine = db_module.engine
    assert fts_index_present(engine)
    with engine.connect() as conn:
        triggers = {
            row[0] for row in conn.execute(sa.text(
                "SELECT name FROM sqlite_master WHERE type='trigger' "
                "AND tbl_name='registry_records'"
            ))
        }
    assert triggers == {"registry_records_ai", "registry_records_ad", "registry_records_au"}


@needs_fts5
def test_triggers_keep_the_index_in_sync(client, ws):
    backend = LocalRegistryBackend()
    assert backend.fts_enabled
    record, _ = backend.upsert_record(ws, name="hr-database", description="HR tools",
                                      descriptor_type="MCP", descriptors={"mcp": {"server": {
                                          "inlineContent": "{}"}}})

    def fts_rows():
        with db_module.engine.connect() as conn:
            return conn.execute(sa.text(
                f"SELECT record_id, description FROM {FTS_TABLE}"  # noqa: S608
            )).all()

    assert fts_rows() == [(record["recordId"], "HR tools")]
    backend.update_record(ws, record["recordId"], description="People tools")
    assert fts_rows() == [(record["recordId"], "People tools")]
    backend.delete_record(ws, record["recordId"])
    assert fts_rows() == []


@needs_fts5
def test_fts_search_matches_prefixes_in_name_description_and_descriptor_text(client, ws):
    backend = LocalRegistryBackend()
    _seed(backend, ws)
    assert [r["name"] for r in backend.search_records(ws, "employ")] == ["hr-database"]  # tool name
    hits = [r["name"] for r in backend.search_records(ws, "expense")]
    assert hits[0] == "expense-report-writer"  # name+description+body outrank one mention
    assert set(hits) == {"expense-report-writer", "meeting-notes"}
    assert [r["name"] for r in backend.search_records(ws, "hr database")] == ["hr-database"]  # AND
    assert backend.search_records(ws, "zzz-nothing") == []
    assert backend.search_records(ws, "  ") == []
    assert len(backend.search_records(ws, "e", max_results=1)) == 1


def test_cjk_and_substring_queries_are_served_by_the_like_leg(client, ws):
    backend = LocalRegistryBackend()
    _seed(backend, ws)
    assert [r["name"] for r in backend.search_records(ws, "报告")] == ["expense-report-writer"]
    hits = backend.search_records(ws, "xpense-rep")
    assert [r["name"] for r in hits] == ["expense-report-writer"]


def test_like_fallback_when_fts_is_unavailable(client, ws):
    backend = LocalRegistryBackend(fts=False)
    _seed(backend, ws)
    assert [r["name"] for r in backend.search_records(ws, "employ")] == ["hr-database"]
    assert {r["name"] for r in backend.search_records(ws, "expense")} == {
        "expense-report-writer", "meeting-notes"}
    assert [r["name"] for r in backend.search_records(ws, "hr tools")] == ["hr-database"]
    assert backend.search_records(ws, "nothing-here") == []


def test_search_is_workspace_scoped_and_spans_every_status(client, ws):
    backend = LocalRegistryBackend()
    _seed(backend, ws)
    other = ws_ctx(id="acct-usw1")
    backend.upsert_record(other, name="hr-database", description="HR MCP tools",
                          descriptor_type="MCP", descriptors={"mcp": {"server": {
                              "inlineContent": "{}"}}})
    mine = backend.search_records(ws, "hr")
    assert [r["registryId"] for r in mine] == ["local-default"]
    theirs = backend.search_records(other, "hr")
    assert [r["registryId"] for r in theirs] == ["local-acct-usw1"]
    # a DRAFT is searchable in the publisher console (ruling R5)
    assert mine[0]["status"] == "DRAFT"


def test_migration_is_idempotent_and_backfills_existing_rows(tmp_path):
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'ledger.db'}")
    Base.metadata.create_all(bind=engine)
    with engine.begin() as conn:  # rows that predate the search index
        conn.execute(sa.text(
            "INSERT INTO registry_records (id, workspace_id, record_type, name, description, "
            "record_version, status, descriptors, tags, search_text, created_at, updated_at) "
            "VALUES ('r1', 'default', 'MCP', 'old-record', 'legacy', '1.0.0-mcp', 'DRAFT', "
            "'{}', '{}', 'legacy tool words', '2026-01-01', '2026-01-01')"
        ))
    db_module._migrate(engine)
    db_module._migrate(engine)  # second run: IF NOT EXISTS everywhere, no double backfill
    if not HAS_FTS5:
        assert not fts_index_present(engine)
        return
    with engine.connect() as conn:
        rows = conn.execute(sa.text(
            f"SELECT record_id FROM {FTS_TABLE} WHERE {FTS_TABLE} MATCH '\"legacy\"*'"  # noqa: S608
        )).all()
    assert rows == [("r1",)]


def test_missing_fts5_module_degrades_with_a_warning(tmp_path, monkeypatch, caplog):
    """Simulate a SQLite without FTS5: the migration logs once and leaves no side table;
    the backend constructed on that engine reports fts_enabled=False."""
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'nofts.db'}")
    Base.metadata.create_all(bind=engine)
    real_begin = engine.begin

    class _NoFts:
        def __init__(self, ctx):
            self._ctx = ctx

        def __enter__(self):
            self._conn = self._ctx.__enter__()
            return self

        def __exit__(self, *exc):
            return self._ctx.__exit__(*exc)

        def execute(self, statement, *args, **kwargs):
            if "USING fts5" in str(statement):
                raise sa.exc.OperationalError(str(statement), {}, Exception("no such module: fts5"))
            return self._conn.execute(statement, *args, **kwargs)

    monkeypatch.setattr(engine, "begin", lambda: _NoFts(real_begin()))
    with caplog.at_level("WARNING", logger="launchpad"):
        db_module._migrate_registry_search_index(engine)
    assert "FTS5" in caplog.text
    assert not fts_index_present(engine)
    assert LocalRegistryBackend(engine).fts_enabled is False
