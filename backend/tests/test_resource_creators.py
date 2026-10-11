"""Who created each user-managed resource (`app.services.creators`).

Every in-scope create path records the calling principal as `created_by`
(`users.id`, NULL for principals without a `users` row) + `created_by_name`
(username, display only); list and detail responses return both. AWS-only kinds
(knowledge bases, custom evaluators) keep theirs in `resource_creators`. Legacy
rows are never backfilled, and a client can never set the creator.
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient

import app.optimization.canary_service as canary_svc
import app.optimization.service as exp_svc
import app.services.memory_admin as ma
import app.services.memory_ownership as mo
from app.core import db as db_module
from app.core.config import get_settings
from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal, assert_no_schema_drift
from app.evaluation import pipeline_routers
from app.evaluation.models import EvalDataset, EvalPipeline, EvalRun, OnlineEvalConfig
from app.main import create_app
from app.models.ledger import Agent, ManagedMemory, ResourceCreator
from app.optimization import readiness
from app.optimization.models import Experiment, RuntimeCanary
from app.services import creators, knowledge
from app.services import users as users_service
from app.services.creators import Creator
from app.skill_lab import jobs as jobs_svc
from app.skill_lab.models import SkillLabJob, SkillLabTaskset
from tests.conftest import set_default_resources
from tests.evaluation.test_pipelines import fake_sessions  # noqa: F401 — fixture by import
from tests.test_memory_ownership import StatusControl
from tests.test_memory_resources import MEM_ID

ADMIN = {"username": "operator", "password": "s3cret-pass"}
PEER = ("127.0.0.1", 4321)
FORGED = {"created_by": "u-forged", "created_by_name": "mallory"}

# The ledger-backed tables carrying `created_by` + `created_by_name`.
LEDGER_TABLES = (
    "eval_datasets",
    "eval_pipelines",
    "eval_runs",
    "online_eval_configs",
    "experiments",
    "runtime_canaries",
    "skill_lab_jobs",
    "skill_lab_tasksets",
)


# ─── fixtures: an auth-enabled console with a member ───────────────────────────


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setenv("LAUNCHPAD_AUTH_USERNAME", ADMIN["username"])
    monkeypatch.setenv("LAUNCHPAD_AUTH_PASSWORD", ADMIN["password"])
    get_settings.cache_clear()
    yield create_app()
    get_settings.cache_clear()


@pytest.fixture
def admin(app):
    with TestClient(app, client=PEER) as client:
        assert client.post("/api/auth/login", json=ADMIN).status_code == 200
        yield client


def _member(app, username: str, *, role: str = "member") -> tuple[str, TestClient]:
    """An approved account on the default workspace, and a logged-in client for it."""
    password = "sufficient-pass"
    client = TestClient(app, client=PEER)
    registered = client.post(
        "/api/auth/register",
        json={"username": username, "email": f"{username}@acme-corp.com", "password": password},
    )
    assert registered.status_code == 201, registered.text
    db = SessionLocal()
    try:
        user = users_service.find_by_username(db, username)
        user.status = users_service.STATUS_ACTIVE
        user.role = role
        user.expires_at = datetime.now(UTC) + timedelta(days=7)
        users_service.set_workspace_grants(db, user, [DEFAULT_WORKSPACE_ID])
        db.commit()
        user_id = user.id
    finally:
        db.close()
    login = client.post("/api/auth/login", json={"username": username, "password": password})
    assert login.status_code == 200, login.text
    return user_id, client


@pytest.fixture
def ada(app, admin):
    """A member (`users.id`, client)."""
    return _member(app, "ada")


def _creator(body: dict) -> tuple:
    return body["created_by"], body["created_by_name"]


def _agent(name: str = "creator-agent") -> Agent:
    db = SessionLocal()
    try:
        agent = Agent(
            workspace_id=DEFAULT_WORKSPACE_ID, name=name, method="zip_runtime", status="active",
            arn=f"arn:aws:bedrock-agentcore:us-west-2:111122223333:runtime/{name}-abcdefghij",
            resource_id=f"{name}-abcdefghij",
            spec={"protocol": "http", "system_prompt": "orig"},
        )
        db.add(agent)
        db.commit()
        db.refresh(agent)
        db.expunge(agent)
        return agent
    finally:
        db.close()


# ─── the helper itself ───────────────────────────────────────────────────────


def test_stamp_and_fields_round_trip():
    row = EvalDataset(workspace_id=DEFAULT_WORKSPACE_ID, name="d", items=[])
    assert creators.fields(row) == {"created_by": None, "created_by_name": None}
    creators.stamp(row, None)  # no human principal: nothing is recorded
    assert creators.fields(row) == {"created_by": None, "created_by_name": None}
    creators.stamp(row, Creator("u1", "ada"))
    assert creators.fields(row) == {"created_by": "u1", "created_by_name": "ada"}
    assert creators.fields(None) == creators.empty()
    assert creators.fields(Creator(None, "operator")) == {
        "created_by": None, "created_by_name": "operator",
    }


def test_creator_of_an_identity():
    from app.routers.auth import ROLE_ADMIN, Identity

    assert Creator.of(None) is None
    assert Creator.of(Identity(username="ada", role="member", user_id="u1")) == Creator("u1", "ada")
    assert Creator.of(Identity(username="operator", role=ROLE_ADMIN)) == Creator(None, "operator")


def test_record_lookup_forget_are_workspace_and_kind_scoped():
    db = SessionLocal()
    try:
        creators.record(db, DEFAULT_WORKSPACE_ID, creators.KB, "KB1", Creator("u1", "ada"))
        # a retried record keeps the first creator and leaves one row
        creators.record(db, DEFAULT_WORKSPACE_ID, creators.KB, "KB1", Creator("u2", "bruno"))
        creators.record(db, DEFAULT_WORKSPACE_ID, creators.KB, "KB2", None)  # nothing
        creators.record(db, "other", creators.KB, "KB1", Creator("u3", "cy"))
        assert db.query(ResourceCreator).count() == 2

        found = creators.lookup(db, DEFAULT_WORKSPACE_ID, creators.KB, ["KB1", "KB2", ""])
        assert found == {"KB1": {"created_by": "u1", "created_by_name": "ada"}}
        assert creators.lookup(db, DEFAULT_WORKSPACE_ID, creators.EVALUATOR, ["KB1"]) == {}
        assert creators.lookup_one(db, DEFAULT_WORKSPACE_ID, creators.KB, "KB2") == (
            creators.empty()
        )

        creators.forget(db, DEFAULT_WORKSPACE_ID, creators.KB, "KB1")
        creators.forget(db, DEFAULT_WORKSPACE_ID, creators.KB, "missing")  # no-op
        assert creators.lookup(db, DEFAULT_WORKSPACE_ID, creators.KB, ["KB1"]) == {}
        assert creators.lookup(db, "other", creators.KB, ["KB1"])["KB1"]["created_by"] == "u3"
    finally:
        db.close()


def test_resource_creators_is_workspace_scoped():
    assert "resource_creators" in db_module.WORKSPACE_SCOPED_TABLES


# ─── migration ───────────────────────────────────────────────────────────────


def test_a_pre_change_ledger_gains_the_creator_columns(tmp_path):
    """Deployed tables predating creators: startup adds the columns + indexes and
    leaves legacy rows NULL; `managed_memories.created_by` (a username until now)
    moves to `created_by_name` and is re-pointed at the matching `users.id`."""
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'ledger.db'}")
    db_module.Base.metadata.create_all(bind=engine)
    with engine.begin() as conn:
        for table in LEDGER_TABLES:
            conn.execute(sa.text(f"DROP INDEX ix_{table}_created_by"))
            for column in ("created_by", "created_by_name"):
                conn.execute(sa.text(f"ALTER TABLE {table} DROP COLUMN {column}"))
        conn.execute(sa.text("DROP INDEX ix_managed_memories_created_by"))
        conn.execute(sa.text("ALTER TABLE managed_memories DROP COLUMN created_by_name"))
        conn.execute(sa.text("DROP TABLE resource_creators"))
        conn.execute(
            sa.text(
                "INSERT INTO users (id, username, username_key, email, password_hash, role,"
                " status, login_count, created_by, created_at, updated_at) VALUES ('u-ada',"
                " 'ada', 'ada', 'ada@acme-corp.com', 'x', 'member', 'active', 0, 'self', :now,"
                " :now)"
            ),
            {"now": "2026-01-01 00:00:00"},
        )
        for row_id, memory_id, origin, by in (
            ("m1", "mem-ada", "created", "ada"),         # a member's console create
            ("m2", "mem-admin", "created", "admin"),     # config admin: no users row
            ("m3", "mem-adopted", "adopted", "ada"),     # pre-existed in AWS
            ("m4", "mem-unknown", "created", None),
        ):
            conn.execute(
                sa.text(
                    "INSERT INTO managed_memories (id, workspace_id, memory_id, origin,"
                    " created_by, created_at) VALUES (:id, 'default', :mem, :origin, :by,"
                    " '2026-01-01 00:00:00')"
                ),
                {"id": row_id, "mem": memory_id, "origin": origin, "by": by},
            )
        conn.execute(
            sa.text(
                "INSERT INTO eval_datasets (id, workspace_id, name, kind, locale, description,"
                " items, created_at) VALUES ('d1', 'default', 'old', 'legacy', 'en', '', '[]',"
                " '2026-01-01 00:00:00')"
            )
        )

    db_module._migrate(engine)
    db_module._migrate(engine)  # idempotent: the memory reinterpretation runs once

    assert_no_schema_drift(engine)
    inspector = sa.inspect(engine)
    for table in (*LEDGER_TABLES, "managed_memories"):
        names = {index["name"] for index in inspector.get_indexes(table)}
        assert f"ix_{table}_created_by" in names, table
    assert "resource_creators" not in inspector.get_table_names()  # create_all's job
    with engine.begin() as conn:
        legacy = conn.execute(
            sa.text("SELECT created_by, created_by_name FROM eval_datasets")
        ).one()
        memories = dict(
            (r[0], (r[1], r[2]))
            for r in conn.execute(
                sa.text("SELECT memory_id, created_by, created_by_name FROM managed_memories")
            )
        )
    assert tuple(legacy) == (None, None)
    assert memories == {
        "mem-ada": ("u-ada", "ada"),
        "mem-admin": (None, "admin"),
        "mem-adopted": (None, "ada"),
        "mem-unknown": (None, None),
    }


def test_init_db_builds_the_resource_creators_table_with_its_indexes(tmp_path):
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'ledger.db'}")
    db_module.Base.metadata.create_all(bind=engine)
    db_module._migrate(engine)
    names = {index["name"] for index in sa.inspect(engine).get_indexes("resource_creators")}
    assert {
        "ix_resource_creators_workspace_id",
        "ix_resource_creators_created_by",
        "uq_resource_creators_workspace_kind_resource",
    } <= names


# ─── legacy rows read null on every ledger-backed list ────────────────────────


def test_legacy_rows_list_with_no_creator(client):
    db = SessionLocal()
    try:
        db.add_all([
            EvalDataset(workspace_id=DEFAULT_WORKSPACE_ID, name="d", items=[{"prompt": "x"}]),
            EvalPipeline(workspace_id=DEFAULT_WORKSPACE_ID, name="p", config={}),
            EvalRun(workspace_id=DEFAULT_WORKSPACE_ID, agent_id="a", agent_name="a",
                    evaluators=[], status="completed"),
            Experiment(workspace_id=DEFAULT_WORKSPACE_ID, name="e", agent_id="a",
                       agent_name="a"),
            RuntimeCanary(workspace_id=DEFAULT_WORKSPACE_ID, name="c",
                          champion_agent_id="a", champion_agent_name="a",
                          challenger_agent_id="a", challenger_agent_name="a"),
            SkillLabJob(workspace_id=DEFAULT_WORKSPACE_ID, type="eval", status="succeeded",
                        taskset_id="ts_1"),
            SkillLabTaskset(workspace_id=DEFAULT_WORKSPACE_ID, name="t", mode="single",
                            counts={"tasks": 1}),
        ])
        db.commit()
    finally:
        db.close()
    null = (None, None)
    assert _creator(client.get("/api/eval/datasets").json()["datasets"][0]) == null
    assert _creator(client.get("/api/eval/pipelines").json()["pipelines"][0]) == null
    assert _creator(client.get("/api/eval/runs").json()["runs"][0]) == null
    assert _creator(client.get("/api/experiments").json()["experiments"][0]) == null
    assert _creator(client.get("/api/runtime-canaries").json()["canaries"][0]) == null
    assert _creator(client.get("/api/skill-lab/jobs").json()[0]) == null
    assert _creator(client.get("/api/skill-lab/tasksets").json()[0]) == null


# ─── datasets + pipelines ────────────────────────────────────────────────────


def test_a_member_dataset_records_the_member_and_ignores_a_posted_creator(ada):
    user_id, client = ada
    created = client.post(
        "/api/eval/datasets", json={"name": "mine", "items": [{"prompt": "hi"}], **FORGED}
    )
    assert created.status_code == 201, created.text
    assert _creator(created.json()) == (user_id, "ada")
    uploaded = client.post(
        "/api/eval/datasets/upload",
        json={"name": "up", "jsonl": '{"prompt": "hi"}\n', **FORGED},
    )
    assert uploaded.status_code == 201, uploaded.text
    assert _creator(uploaded.json()) == (user_id, "ada")
    listed = {d["id"]: d for d in client.get("/api/eval/datasets").json()["datasets"]}
    assert _creator(listed[created.json()["id"]]) == (user_id, "ada")


def test_the_config_admin_records_a_name_only(admin):
    created = admin.post("/api/eval/datasets", json={"name": "a", "items": [{"prompt": "hi"}]})
    assert created.status_code == 201, created.text
    assert _creator(created.json()) == (None, ADMIN["username"])


def test_the_auth_disabled_operator_records_a_name_only(client):
    created = client.post("/api/eval/datasets", json={"name": "a", "items": [{"prompt": "hi"}]})
    assert _creator(created.json()) == (None, get_settings().auth_username)


def test_a_pipeline_records_its_creator_and_its_run_dataset_the_run_starter(
    app, admin, fake_sessions  # noqa: F811
):
    ada_id, ada_client = _member(app, "ada")
    bruno_id, bruno_client = _member(app, "bruno")
    created = ada_client.post("/api/eval/pipelines", json={
        "name": "nightly", "source": {"agent": "hr-bot"},
        "output": {"dataset_name": "from-traces"}, **FORGED,
    })
    assert created.status_code == 201, created.text
    pipeline = created.json()
    assert _creator(pipeline) == (ada_id, "ada")
    assert _creator(ada_client.get(f"/api/eval/pipelines/{pipeline['id']}").json()) == (
        ada_id, "ada",
    )

    started = bruno_client.post(f"/api/eval/pipelines/{pipeline['id']}/run")
    assert started.status_code == 202, started.text
    after = bruno_client.get(f"/api/eval/pipelines/{pipeline['id']}").json()
    dataset_id = after["last_run"]["dataset_id"]
    assert dataset_id, after["last_run"]
    db = SessionLocal()
    try:
        dataset = db.get(EvalDataset, dataset_id)
        assert (dataset.created_by, dataset.created_by_name) == (bruno_id, "bruno")
        # the pipeline keeps its own creator
        assert db.get(EvalPipeline, pipeline["id"]).created_by == ada_id
    finally:
        db.close()


def test_an_appending_pipeline_run_keeps_the_target_datasets_creator(client):
    db = SessionLocal()
    try:
        dataset = EvalDataset(workspace_id=DEFAULT_WORKSPACE_ID, name="t", kind="predefined",
                              items=[{"scenario_id": "s", "turns": [{"input": "x"}]}],
                              created_by="u-orig", created_by_name="orig")
        db.add(dataset)
        db.commit()
        _, added = pipeline_routers._write(
            db, MagicMock(id=DEFAULT_WORKSPACE_ID), dataset_id=dataset.id, dataset_name=None,
            description="", items=[{"scenario_id": "s2", "turns": [{"input": "y"}]}],
            skipped=[], dedupe=True, creator=Creator("u-new", "new"),
        )
        assert added == 1
        db.refresh(dataset)
        assert (dataset.created_by, dataset.created_by_name) == ("u-orig", "orig")
    finally:
        db.close()


# ─── eval runs + online configs ──────────────────────────────────────────────


def test_a_member_eval_run_records_the_member(app, ada, monkeypatch):
    from tests.evaluation.test_runs_flow import stub_environment

    user_id, client = ada
    agent = _agent()
    stub_environment(monkeypatch)
    res = client.post("/api/eval/runs", json={
        "agent_id": agent.id, "session_ids": ["sess-" + "a" * 30], "mode": "insights",
        **FORGED,
    })
    assert res.status_code in (200, 201), res.text
    run = res.json()
    assert _creator(run) == (user_id, "ada")
    assert _creator(client.get(f"/api/eval/runs/{run['id']}").json()) == (user_id, "ada")


def test_an_online_config_records_its_creator_and_report_runs_theirs(ada, monkeypatch):
    from app.evaluation import online
    from tests.evaluation.test_online_configs import stub_environment

    user_id, client = ada
    agent = _agent()
    control, details = stub_environment(monkeypatch)
    res = client.post("/api/eval/online", json={
        "agent_id": agent.id, "evaluators": ["Builtin.Helpfulness"], **FORGED,
    })
    assert res.status_code == 201, res.text
    assert _creator(res.json()) == (user_id, "ada")
    db = SessionLocal()
    try:
        row = db.query(OnlineEvalConfig).one()
        assert (row.created_by, row.created_by_name) == (user_id, "ada")
    finally:
        db.close()

    seen: dict = {}

    def fake_report(db, ws, config_id, range_key, **kw):
        seen.update(kw)
        return {"run_id": "r", "status": "queued", "queue_position": 0,
                "range": range_key, "config_id": config_id}

    monkeypatch.setattr(online, "start_report", fake_report)
    assert client.post("/api/eval/online/oe_x/reports", json={}).status_code == 202
    assert seen["creator"] == Creator(user_id, "ada")


def test_online_configs_without_a_row_list_no_creator(client, monkeypatch):
    from tests.evaluation.test_online_configs import aws_detail, stub_environment

    stub_environment(monkeypatch, {"exp_1_oe1-9999999999": aws_detail(
        "exp_1_oe1-9999999999", "exp_1_oe1")})
    configs = client.get("/api/eval/online").json()["configs"]
    assert configs and all(_creator(c) == (None, None) for c in configs)


def test_start_report_stamps_the_run(client, monkeypatch):
    """`start_report` hands its creator to `submit_run`, which stamps the row."""
    from app.evaluation import online
    from app.evaluation import service as eval_service

    seen: dict = {}

    def fake_submit(**kw):
        seen.update(kw)
        return MagicMock(id="r1", status="queued", queue_position=0)

    agent = _agent()
    monkeypatch.setattr(online, "get_config", lambda *a, **k: {
        "owner": "agent", "mode": "insights", "agent_id": agent.id,
        "insights": ["Builtin.X"], "arn": "arn:oe",
    })
    monkeypatch.setattr(eval_service, "submit_run", fake_submit)
    db = SessionLocal()
    try:
        ws = MagicMock(id=DEFAULT_WORKSPACE_ID)
        online.start_report(db, ws, "oe_x", "6h", creator=Creator("u1", "ada"))
    finally:
        db.close()
    assert seen["creator"] == Creator("u1", "ada")


# ─── evaluators + knowledge bases (AWS-only) ──────────────────────────────────


def _stub_evaluators(monkeypatch) -> MagicMock:
    stub = MagicMock()
    stub.create_evaluator.return_value = {
        "evaluatorId": "my_judge-abc123", "evaluatorArn": "arn:ev/my_judge-abc123",
    }
    stub.get_evaluator.return_value = {
        "evaluatorId": "my_judge-abc123", "evaluatorName": "my_judge", "level": "SESSION",
        "status": "ACTIVE", "evaluatorConfig": {"llmAsAJudge": {"instructions": "{session}"}},
    }
    stub.list_evaluators.return_value = {"evaluators": [
        {"evaluatorId": "my_judge-abc123", "evaluatorName": "my_judge",
         "evaluatorType": "Custom", "level": "SESSION", "status": "ACTIVE"},
        {"evaluatorId": "outside-xyz", "evaluatorName": "outside",
         "evaluatorType": "Custom", "level": "SESSION", "status": "ACTIVE"},
    ]}
    monkeypatch.setattr("app.evaluation.routers.control_client", lambda _ws=None: stub)
    return stub


def test_a_console_evaluator_shows_its_creator_until_deleted(ada, monkeypatch):
    user_id, client = ada
    _stub_evaluators(monkeypatch)
    res = client.post("/api/eval/evaluators", json={
        "name": "my_judge", "instructions": "Rate {session}", **FORGED,
    })
    assert res.status_code == 201, res.text
    assert _creator(res.json()) == (user_id, "ada")

    listed = {e["id"]: e for e in client.get("/api/eval/evaluators").json()["evaluators"]}
    assert _creator(listed["my_judge-abc123"]) == (user_id, "ada")
    assert _creator(listed["outside-xyz"]) == (None, None)  # made outside the console
    assert all(_creator(e) == (None, None) for e in listed.values() if e["source"] == "builtin")
    detail = client.get("/api/eval/evaluators/my_judge-abc123").json()
    assert _creator(detail) == (user_id, "ada")

    assert client.delete("/api/eval/evaluators/my_judge-abc123").status_code == 200
    db = SessionLocal()
    try:
        assert db.query(ResourceCreator).count() == 0
    finally:
        db.close()


def _stub_kbs(monkeypatch) -> MagicMock:
    set_default_resources({"kb_role_arn": "arn:aws:iam::111:role/kb"})
    stub = MagicMock()
    stub.create_knowledge_base.return_value = {"knowledgeBase": {"knowledgeBaseId": "KB123"}}
    stub.get_knowledge_base.return_value = {
        "knowledgeBase": {"knowledgeBaseConfiguration": {"type": "MANAGED"}}
    }
    monkeypatch.setattr(knowledge, "agent_client", lambda _ws=None: stub)
    monkeypatch.setattr(knowledge, "_start_source_completion", lambda *_a, **_k: None)
    monkeypatch.setattr(knowledge, "_list_data_sources", lambda *_a: [])
    monkeypatch.setattr(knowledge, "_delete_kb_policy", lambda *_a: None)
    monkeypatch.setattr(
        knowledge, "get_kb_detail", lambda _ws, kb_id: {"kb_id": kb_id, "status": "CREATING"}
    )
    monkeypatch.setattr(knowledge, "list_kbs", lambda _ws: [
        {"kb_id": "KB123", "name": "docs"}, {"kb_id": "KBOUT", "name": "outside"},
    ])
    return stub


def test_a_console_knowledge_base_shows_its_creator_until_deleted(ada, monkeypatch):
    user_id, client = ada
    _stub_kbs(monkeypatch)
    res = client.post("/api/knowledge-bases", json={"name": "docs", **FORGED})
    assert res.status_code == 202, res.text
    assert _creator(res.json()) == (user_id, "ada")

    items = {i["kb_id"]: i for i in client.get("/api/knowledge-bases").json()["items"]}
    assert _creator(items["KB123"]) == (user_id, "ada")
    assert _creator(items["KBOUT"]) == (None, None)
    assert _creator(client.get("/api/knowledge-bases/KB123").json()) == (user_id, "ada")

    assert client.delete("/api/knowledge-bases/KB123").status_code == 200
    db = SessionLocal()
    try:
        assert db.query(ResourceCreator).count() == 0
    finally:
        db.close()


def test_create_kb_outside_the_router_records_nothing(monkeypatch):
    """`scripts/e2e_knowledge_base.py` calls the service with no principal."""
    from tests.conftest import ws_ctx

    _stub_kbs(monkeypatch)
    knowledge.create_kb(ws_ctx({"kb_role_arn": "arn:kb"}, id=DEFAULT_WORKSPACE_ID), "d", "", {})
    db = SessionLocal()
    try:
        assert db.query(ResourceCreator).count() == 0
    finally:
        db.close()


# ─── memory ──────────────────────────────────────────────────────────────────


@pytest.fixture
def memory_control(monkeypatch):
    set_default_resources({"memory_id": MEM_ID})
    stub = StatusControl(memories=[
        {"id": MEM_ID, "arn": "arn:mem:1", "status": "ACTIVE"},
        {"id": "existing-AAAA", "arn": "arn:mem:2", "status": "ACTIVE"},
    ])
    monkeypatch.setattr(ma, "control_client", lambda _ws=None: stub)
    monkeypatch.setattr(mo, "control_client", lambda _ws=None: stub)
    return stub


def test_a_created_memory_records_its_creator_and_an_adopted_one_only_a_name(
    app, admin, memory_control
):
    user_id, client = _member(app, "ada")
    res = client.post("/api/memory/resources", json={"name": "notes", "strategies": [], **FORGED})
    assert res.status_code == 201, res.text
    created = res.json()
    assert _creator(created) == (user_id, "ada")
    assert _creator(client.get(f"/api/memory/resources/{created['id']}").json()) == (
        user_id, "ada",
    )

    boss_id, boss = _member(app, "boss", role="admin")
    adopted = boss.post("/api/memory/resources/existing-AAAA/adopt")
    assert adopted.status_code == 200, adopted.text
    # it pre-existed in AWS: the adopter is named, but owns nothing
    assert _creator(adopted.json()) == (None, "boss")

    memory_control.memories.append({"id": created["id"], "arn": "arn:mem:3", "status": "ACTIVE"})
    items = {m["id"]: m for m in client.get("/api/memory/resources").json()["items"]}
    assert _creator(items[created["id"]]) == (user_id, "ada")
    assert _creator(items["existing-AAAA"]) == (None, "boss")
    assert _creator(items[MEM_ID]) == (None, None)  # the bootstrap memory has no row
    db = SessionLocal()
    try:
        row = db.query(ManagedMemory).filter_by(memory_id="existing-AAAA").one()
        assert row.origin == "adopted" and row.created_by is None
    finally:
        db.close()


# ─── experiments + canaries ──────────────────────────────────────────────────


def test_a_member_experiment_and_canary_record_the_member(ada, monkeypatch):
    user_id, client = ada
    agent = _agent()
    control = MagicMock()
    control.get_agent_runtime.side_effect = lambda agentRuntimeId: {
        "agentRuntimeName": f"runtime-{agentRuntimeId}"
    }
    monkeypatch.setattr(exp_svc, "control_client", lambda _ws=None: control)
    monkeypatch.setattr(canary_svc, "control_client", lambda _ws=None: control)
    monkeypatch.setattr(readiness, "project_readiness", lambda *_a, **_k: {"state": "ready"})

    exp = client.post("/api/experiments", json={"agent_id": agent.id, **FORGED})
    assert exp.status_code == 201, exp.text
    assert _creator(exp.json()) == (user_id, "ada")
    assert _creator(client.get(f"/api/experiments/{exp.json()['id']}").json()) == (
        user_id, "ada",
    )

    canary = client.post("/api/runtime-canaries", json={
        "agent_id": agent.id, "candidate": {"system_prompt": "new prompt"}, **FORGED,
    })
    assert canary.status_code == 201, canary.text
    assert _creator(canary.json()) == (user_id, "ada")
    listed = client.get("/api/runtime-canaries").json()["canaries"]
    assert _creator(listed[0]) == (user_id, "ada")


def test_a_harness_canary_records_its_creator(monkeypatch):
    agent = _agent("harness-subject")
    monkeypatch.setattr(canary_svc, "control_client", lambda _ws=None: MagicMock())
    monkeypatch.setattr(canary_svc, "_agent_meta", lambda *_a: {})
    row = canary_svc.start_harness_canary(
        agent, control_version="1", treatment_version="2",
        workspace=MagicMock(), creator=Creator("u1", "ada"),
    )
    assert (row.created_by, row.created_by_name) == ("u1", "ada")


# ─── Skill Lab ───────────────────────────────────────────────────────────────


def test_skill_lab_routes_hand_the_caller_to_the_services(ada, monkeypatch):
    user_id, client = ada
    seen: list = []

    def capture(name):
        def fake(*_a, **kw):
            seen.append((name, kw.get("creator")))
            return {"id": "x"}
        return fake

    for name in ("submit_eval_job", "submit_train_job", "submit_taskgen_job",
                 "import_taskgen_taskset"):
        monkeypatch.setattr(jobs_svc, name, capture(name))
    source = {"kind": "registry", "record_id": "rec-1"}
    for body in (
        {"type": "eval", "taskset_id": "ts_1", "skill_source": source},
        {"type": "train", "taskset_id": "ts_1", "skill_source": source},
        {"type": "taskgen", "skill_source": source},
    ):
        assert client.post("/api/skill-lab/jobs", json={**body, **FORGED}).status_code == 201
    res = client.post("/api/skill-lab/jobs/job_1/import-taskset", json={"name": "n"})
    assert res.status_code == 201, res.text
    assert seen == [
        ("submit_eval_job", Creator(user_id, "ada")),
        ("submit_train_job", Creator(user_id, "ada")),
        ("submit_taskgen_job", Creator(user_id, "ada")),
        ("import_taskgen_taskset", Creator(user_id, "ada")),
    ]


def test_a_member_taskset_records_the_member(ada, tmp_path, monkeypatch):
    import sys
    from types import SimpleNamespace

    from app.skill_lab import tasksets as taskset_svc
    from tests.test_skill_lab_tasksets import VALID_TASK

    monkeypatch.setattr(taskset_svc, "TASKSETS_DIR", tmp_path / "tasksets")
    monkeypatch.setattr(
        taskset_svc, "get_settings", lambda: SimpleNamespace(skill_lab_python=sys.executable)
    )
    user_id, client = ada
    res = client.post("/api/skill-lab/tasksets", json={
        "name": "mine", "mode": "single", "tasks_by_split": {"tasks": [VALID_TASK]}, **FORGED,
    })
    assert res.status_code == 201, res.text
    assert _creator(res.json()) == (user_id, "ada")
    assert _creator(client.get("/api/skill-lab/tasksets").json()[0]) == (user_id, "ada")

