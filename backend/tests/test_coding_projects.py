"""Coding projects backend (parent task 10-19-coding-agent, child coding-backend):
the private-owner access matrix, turns + idempotent settlement + budgets, files,
snapshots, the SSE proxy (incl. idle mode), the watcher and the deploy handoff.

Hermetic: the coding runtime is a stub (`FakeRuntime`, the action protocol of
`app/coding/image/src/server.mjs`), S3 a stub (`FakeS3`), snapshots run inline.
"""

from __future__ import annotations

import itertools
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.coding import (
    deploy,
    downloads,
    files,
    infra,
    install,
    projects,
    runtime_client,
    s3urls,
    snapshots,
    stream,
    turns,
    watcher,
)
from app.coding.models import CodingProject, CodingTurn
from app.coding.runtime_client import CodingRuntimeError
from app.core.config import get_settings
from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.core.errors import AppError
from app.main import create_app
from app.models.budgets import BudgetLevel, UsageEvent, UsageSession
from app.models.ledger import Agent, Job, User
from app.routers import agents as agents_router
from app.services import byoc_uploads
from app.services import users as users_service
from tests.conftest import set_default_resources

ADMIN = {"username": "operator", "password": "s3cret-pass"}
PEER = ("127.0.0.1", 4321)
ARN = "arn:aws:bedrock-agentcore:us-west-2:111122223333:runtime/launchpad_coding_runtime-x"
BUCKET = "launchpad-artifacts-test"
MODEL = "global.openai.gpt-6-sol"
INSTALLED = {
    "artifacts_bucket": BUCKET,
    "execution_role_arn": "arn:aws:iam::111122223333:role/launchpad-test-execution",
    infra.KEY_RUNTIME_ID: "launchpad_coding_runtime-x",
    infra.KEY_RUNTIME_ARN: ARN,
    infra.KEY_MODELS: list(get_settings().coding_models),
}
# a live turnUsage (2026-10-19): input includes cached + cache write; output
# includes reasoning
USAGE = {"totalTokens": 94905, "inputTokens": 94630, "cachedInputTokens": 58007,
         "cacheWriteInputTokens": 36613, "outputTokens": 275, "reasoningOutputTokens": 8}


# ── stubs ────────────────────────────────────────────────────────────────────


class FakeRuntime:
    """The coding runtime's action protocol, per session, in memory."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.logs: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self.seq: dict[str, int] = defaultdict(int)
        self.active: dict[str, dict[str, Any]] = {}
        self.files: dict[str, dict[str, str]] = defaultdict(dict)
        self.turn_base: dict[str, dict[str, str]] = {}
        self.errors: dict[str, CodingRuntimeError] = {}
        self._ids = itertools.count(1)

    def __call__(self, ctx, arn, session_id, action, *, read_timeout=None, client=None,
                 **payload):
        assert arn == ARN
        self.calls.append((session_id, action, dict(payload)))
        if action in self.errors:
            raise self.errors[action]
        return getattr(self, f"do_{action}")(session_id, payload)

    # helpers the tests drive
    def append(self, session: str, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self.seq[session] += 1
        record = {"seq": self.seq[session], "ts": 1_760_000_000_000 + self.seq[session],
                  "method": method, "params": params}
        self.logs[session].append(record)
        return record

    def finish(self, session: str, status: str = "completed", usage=USAGE,
               error: str | None = None) -> str:
        turn = self.active.pop(session)
        self.append(session, "item/agentMessage/delta",
                    {"turnId": turn["turn_id"], "itemId": "m1", "delta": "done"})
        self.append(session, "turn/completed", {
            "threadId": turn["thread_id"],
            "turn": {"id": turn["turn_id"], "status": status,
                     "error": {"message": error} if error else None},
        })
        self.append(session, "launchpad/turnUsage", {
            "turnId": turn["turn_id"], "threadId": turn["thread_id"], "model": turn["model"],
            "status": status, "usage": usage,
        })
        return turn["turn_id"]

    def actions(self, action: str) -> list[tuple[str, dict[str, Any]]]:
        return [(s, p) for s, a, p in self.calls if a == action]

    # the actions
    def do_status(self, s, p):
        return {"active_turn": self.active.get(s), "seq": self.seq[s]}

    def do_start_turn(self, s, p):
        if s in self.active:
            raise CodingRuntimeError("turn_running", "a turn is running in this project")
        seq_before = self.seq[s]
        turn_id = f"turn-{next(self._ids)}"
        self.active[s] = {"turn_id": turn_id, "thread_id": "thread-1", "model": p["model"],
                          "effort": p["effort"]}
        self.turn_base[s] = dict(self.files[s])
        self.append(s, "turn/started", {"turn": {"id": turn_id}})
        return {"thread_id": "thread-1", "turn_id": turn_id, "seq": seq_before}

    def do_interrupt(self, s, p):
        return {"interrupted": s in self.active}

    def do_events(self, s, p):
        after = int(p.get("after") or 0)
        limit = int(p.get("limit") or 500)
        records = [r for r in self.logs[s] if r["seq"] > after]
        if p.get("compact"):
            done = {r["params"].get("turnId") for r in self.logs[s]
                    if r["method"] == "turn/completed"}
            done |= {r["params"]["turn"]["id"] for r in self.logs[s]
                     if r["method"] == "turn/completed"}
            records = [r for r in records if not (
                r["method"].endswith("/delta") and r["params"].get("turnId") in done)]
        page = records[:limit]
        next_after = page[-1]["seq"] if page else after
        if len(page) == len(records):
            next_after = max(next_after, self.seq[s])
        return {"events": page, "next_after": next_after, "seq": self.seq[s],
                "cursor_ahead": after > self.seq[s], "active_turn": self.active.get(s),
                "boot_id": "boot-1"}

    def do_tree(self, s, p):
        if p["path"] == "gone":
            raise CodingRuntimeError("not_found", "gone does not exist")
        return {"path": p["path"], "entries": [
            {"path": "src", "dir": True},
            {"path": "main.py", "dir": False, "size": 12},
            {"path": "link", "dir": False, "symlink": True, "size": 0},
        ], "truncated": False}

    def do_read_file(self, s, p):
        if p.get("rev") == "turn" and s not in self.turn_base:
            raise CodingRuntimeError("no_turn", "no turn has run in this project yet")
        source = self.turn_base.get(s, {}) if p.get("rev") == "turn" else self.files[s]
        if p["path"] == "logo.png":
            return {"path": p["path"], "exists": True, "content": None, "binary": True,
                    "sha256": "b" * 64, "size": 99}
        if p["path"] not in source:
            return {"path": p["path"], "exists": False}
        return {"path": p["path"], "exists": True, "content": source[p["path"]],
                "binary": False, "sha256": f"sha-{source[p['path']]}",
                "size": len(source[p["path"]])}

    def do_write_file(self, s, p):
        current = self.files[s].get(p["path"])
        if "expected_sha256" in p:
            have = f"sha-{current}" if current is not None else None
            if have != p["expected_sha256"]:
                raise CodingRuntimeError("conflict", f"{p['path']} changed",
                                         detail={"sha256": have})
        self.files[s][p["path"]] = p["content"]
        return {"path": p["path"], "sha256": f"sha-{p['content']}", "size": len(p["content"]),
                "created": current is None}

    def do_changes(self, s, p):
        if p["scope"] == "turn" and s not in self.turn_base:
            raise CodingRuntimeError("no_turn", "no turn has run in this project yet")
        return {"scope": p["scope"], "base": "t", "changes": [
            {"path": "main.py", "status": "M"}, {"path": "b.py", "status": "R", "old_path": "a.py"},
        ]}

    def do_diff(self, s, p):
        if p["scope"] == "turn" and s not in self.turn_base:
            raise CodingRuntimeError("no_turn", "no turn has run in this project yet")
        return {"scope": p["scope"], "diff": "--- a/main.py\n+++ b/main.py\n", "truncated": False}

    def do_snapshot(self, s, p):
        assert p["put_url"].startswith("https://put/")
        return {"bytes": 1234, "files": 3, "ms": 5}

    def do_export(self, s, p):
        assert p["put_url"].startswith("https://put/")
        return {"bytes": 456, "files": 2, "root_files": ["main.py"]}

    def do_upload_file(self, s, p):
        assert p["put_url"].startswith("https://put/")
        if p["path"] in self.files[s]:
            return {"bytes": len(self.files[s][p["path"]].encode()),
                    "name": p["path"].rsplit("/", 1)[-1]}
        if p["path"] == "src":
            raise CodingRuntimeError("path_invalid", "src is not a regular file")
        raise CodingRuntimeError("not_found", f"{p['path']} does not exist")


class FakeS3:
    def __init__(self) -> None:
        self.objects: set[str] = set()
        self.downloads: list[str] = []
        self.presigned: list[tuple[str, dict[str, Any], int]] = []
        self.fail_delete = False
        self.size = 456  # what HeadObject reports for an export

    def generate_presigned_url(self, method, Params, ExpiresIn):  # noqa: N803 — boto3 names
        assert Params["Bucket"] == BUCKET and ExpiresIn <= 900
        self.presigned.append((method, dict(Params), ExpiresIn))
        verb = "put" if method == "put_object" else "get"
        if verb == "put":
            self.objects.add(Params["Key"])
        return f"https://{verb}/{Params['Key']}"

    def list_objects_v2(self, Bucket, Prefix, ContinuationToken=None):  # noqa: N803
        return {"Contents": [{"Key": k} for k in sorted(self.objects) if k.startswith(Prefix)],
                "IsTruncated": False}

    def delete_objects(self, Bucket, Delete):  # noqa: N803
        if self.fail_delete:
            return {"Errors": [{"Key": Delete["Objects"][0]["Key"], "Code": "AccessDenied",
                                "Message": "no"}]}
        for item in Delete["Objects"]:
            self.objects.discard(item["Key"])
        return {}

    def delete_object(self, Bucket, Key):  # noqa: N803
        self.objects.discard(Key)

    def head_object(self, Bucket, Key):  # noqa: N803
        assert Bucket == BUCKET and Key in self.objects
        return {"ContentLength": self.size}

    def download_file(self, bucket, key, path):
        import io
        import zipfile

        self.downloads.append(key)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("main.py", "print('hi')\n")
        with open(path, "wb") as fh:
            fh.write(buf.getvalue())


class Staged:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.dockerfile = False
        self.candidates = ["main.py"]

    def __call__(self, workspace, **kwargs):
        self.calls.append(kwargs)
        n = len(self.calls)
        return {"upload_id": f"up{n}", "workspace_id": workspace.id, "sha256": kwargs["sha256"],
                "size_bytes": kwargs["size_bytes"], "root_prefix": "",
                "detected": {"entrypoint_candidates": self.candidates,
                             "has_dockerfile": self.dockerfile, "has_requirements": False}}


@pytest.fixture
def fake(monkeypatch) -> FakeRuntime:
    stub = FakeRuntime()
    monkeypatch.setattr(runtime_client, "call", stub)
    return stub


@pytest.fixture
def s3(monkeypatch) -> FakeS3:
    stub = FakeS3()
    monkeypatch.setattr(s3urls, "_s3", lambda ctx: stub)
    return stub


@pytest.fixture(autouse=True)
def _inline_snapshots(monkeypatch):
    monkeypatch.setattr(snapshots, "_spawn", lambda fn, name: fn())
    snapshots._FAILED_AT.clear()
    yield
    snapshots._FAILED_AT.clear()


@pytest.fixture
def stopped(monkeypatch) -> list[str]:
    sessions: list[str] = []

    def stop(client, *, runtime_arn, session_id, qualifier=None):
        sessions.append(session_id)
        return {}

    monkeypatch.setattr(projects.rt, "stop_runtime_session", stop)
    monkeypatch.setattr(projects, "data_client", lambda ctx, read_timeout=None: object())
    return sessions


@pytest.fixture
def staged(monkeypatch) -> Staged:
    stub = Staged()
    monkeypatch.setattr(byoc_uploads, "stage_upload", stub)
    return stub


@pytest.fixture
def deploys(monkeypatch) -> list[str]:
    jobs: list[str] = []
    monkeypatch.setattr(agents_router, "start_deploy_async", lambda job_id: jobs.append(job_id))
    return jobs


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setenv("LAUNCHPAD_AUTH_USERNAME", ADMIN["username"])
    monkeypatch.setenv("LAUNCHPAD_AUTH_PASSWORD", ADMIN["password"])
    get_settings.cache_clear()
    application = create_app()
    set_default_resources(INSTALLED)
    yield application
    get_settings.cache_clear()


@pytest.fixture
def admin(app):
    with TestClient(app, client=PEER) as client:
        assert client.post("/api/auth/login", json=ADMIN).status_code == 200
        yield client


def _member(app, username: str, *, permissions: dict[str, bool] | None = None
            ) -> tuple[str, TestClient]:
    password = "sufficient-pass"
    client = TestClient(app, client=PEER)
    assert client.post("/api/auth/register", json={
        "username": username, "email": f"{username}@acme-corp.com", "password": password,
    }).status_code == 201
    db = SessionLocal()
    try:
        user = users_service.find_by_username(db, username)
        user.status = users_service.STATUS_ACTIVE
        user.role = "member"
        user.expires_at = datetime.now(UTC) + timedelta(days=7)
        if permissions is not None:
            user.permissions = permissions
        users_service.set_workspace_grants(db, user, [DEFAULT_WORKSPACE_ID])
        db.commit()
        user_id = user.id
    finally:
        db.close()
    assert client.post("/api/auth/login", json={"username": username, "password": password}
                       ).status_code == 200
    return user_id, client


@pytest.fixture
def ada(app, admin) -> tuple[str, TestClient]:
    return _member(app, "ada")


@pytest.fixture
def bob(app, admin) -> tuple[str, TestClient]:
    return _member(app, "bob")


def _create(client: TestClient, name: str = "My agent") -> dict[str, Any]:
    response = client.post("/api/coding/projects", json={"name": name, "description": "d"})
    assert response.status_code == 201, response.text
    return response.json()


def _project(project_id: str) -> CodingProject:
    db = SessionLocal()
    try:
        row = db.get(CodingProject, project_id)
        db.expunge(row)
        return row
    finally:
        db.close()


def _turns(project_id: str) -> list[CodingTurn]:
    db = SessionLocal()
    try:
        rows = db.query(CodingTurn).filter(CodingTurn.project_id == project_id).all()
        for row in rows:
            db.expunge(row)
        return rows
    finally:
        db.close()


def _usage_rows() -> list[UsageEvent]:
    db = SessionLocal()
    try:
        rows = db.query(UsageEvent).filter(UsageEvent.source == "coding").all()
        for row in rows:
            db.expunge(row)
        return rows
    finally:
        db.close()


def _start(client: TestClient, project_id: str, **body) -> Any:
    return client.post(f"/api/coding/projects/{project_id}/turns",
                       json={"prompt": "build it", "model": MODEL, "effort": "low", **body})


def _set(project_id: str, **values) -> None:
    db = SessionLocal()
    try:
        row = db.get(CodingProject, project_id)
        for key, value in values.items():
            setattr(row, key, value)
        db.commit()
    finally:
        db.close()


# ── access matrix ─────────────────────────────────────────────────────────────


def test_the_owner_creates_lists_reads_and_renames(ada):
    _, client = ada
    created = _create(client)
    assert created["owned"] is True and created["status"] == "idle"
    assert created["created_by_name"] == "ada" and created["agent_id"] is None
    assert set(created) >= {"id", "name", "description", "status", "active_turn_id", "model",
                            "effort", "agent_id", "snapshot_at", "created_at", "updated_at",
                            "created_by", "created_by_name"}
    listed = client.get("/api/coding/projects").json()["projects"]
    assert [p["id"] for p in listed] == [created["id"]]
    detail = client.get(f"/api/coding/projects/{created['id']}").json()
    assert detail["turns"] == [] and detail["active_turn"] is None
    renamed = client.patch(f"/api/coding/projects/{created['id']}", json={"name": "  New  name "})
    assert renamed.status_code == 200 and renamed.json()["name"] == "New name"
    row = _project(created["id"])
    assert row.owner_principal.startswith("user:") and len(row.session_id) == 64


def test_a_blank_name_is_refused(ada):
    _, client = ada
    assert client.post("/api/coding/projects", json={"name": "   "}).status_code == 422
    assert client.post("/api/coding/projects", json={"name": "x" * 81}).status_code == 422


BY_ID_ROUTES = [
    ("GET", "", None), ("PATCH", "", {"name": "x"}), ("DELETE", "", None),
    ("POST", "/turns", {"prompt": "hi", "model": MODEL}), ("POST", "/interrupt", None),
    ("GET", "/events", None), ("GET", "/tree", None), ("GET", "/file?path=main.py", None),
    ("PUT", "/file", {"path": "main.py", "content": "x"}), ("GET", "/changes", None),
    ("GET", "/diff", None), ("POST", "/download", {}),
    ("POST", "/download", {"path": "main.py"}), ("POST", "/deploy", {}),
]


def test_another_member_gets_404_on_every_by_id_route(ada, bob, fake, s3):
    _, owner = ada
    _, other = bob
    project = _create(owner)
    assert other.get("/api/coding/projects").json()["projects"] == []
    for method, rest, body in BY_ID_ROUTES:
        response = other.request(method, f"/api/coding/projects/{project['id']}{rest}",
                                 json=body)
        assert response.status_code == 404, (method, rest, response.text)
        assert response.json()["code"] == "coding.project_not_found"
    assert fake.calls == [], "a foreign project never reaches the runtime"
    assert _project(project["id"]).name == "My agent"


def test_a_missing_id_reads_exactly_like_a_foreign_one(ada):
    _, client = ada
    response = client.get("/api/coding/projects/nope")
    assert response.status_code == 404 and response.json()["code"] == "coding.project_not_found"


def test_admin_lists_all_and_deletes_but_cannot_open_a_members_project(
        admin, ada, fake, s3, stopped):
    _, owner = ada
    project = _create(owner)
    assert admin.get("/api/coding/projects").json()["projects"] == []
    every = admin.get("/api/coding/projects?all=1").json()["projects"]
    assert [(p["id"], p["owned"]) for p in every] == [(project["id"], False)]
    for method, rest, body in BY_ID_ROUTES:
        if method == "DELETE":
            continue
        response = admin.request(method, f"/api/coding/projects/{project['id']}{rest}",
                                 json=body)
        assert response.status_code == 404, (method, rest)
    assert fake.calls == []
    session = _session_of(project)
    deleted = admin.delete(f"/api/coding/projects/{project['id']}")
    assert deleted.status_code == 200 and deleted.json() == {"deleted": True}
    assert owner.get("/api/coding/projects").json()["projects"] == []
    assert stopped == [session]


def _session_of(project: dict[str, Any]) -> str:
    db = SessionLocal()
    try:
        return db.get(CodingProject, project["id"]).session_id
    finally:
        db.close()


def test_a_project_lives_in_its_workspace_only(ada):
    from app.routers.auth import Identity

    user_id, client = ada
    project = _create(client)
    me = Identity(username="ada", role="member", user_id=user_id)
    db = SessionLocal()
    try:
        assert projects.project_in(db, DEFAULT_WORKSPACE_ID, project["id"], me) is not None
        assert projects.project_in(db, "west", project["id"], me) is None
        boss = Identity(username="boss", role="admin", user_id="someone-else")
        assert projects.project_in(db, DEFAULT_WORKSPACE_ID, project["id"], boss) is None
        assert projects.project_in(db, DEFAULT_WORKSPACE_ID, project["id"], boss,
                                   admin_ok=True) is not None
        assert projects.project_in(db, "west", project["id"], boss, admin_ok=True) is None
    finally:
        db.close()


def test_the_detail_carries_the_newest_twenty_turns_oldest_first(ada):
    _, client = ada
    project = _create(client)
    base = datetime(2026, 10, 1, tzinfo=UTC)
    db = SessionLocal()
    try:
        db.add_all([
            CodingTurn(workspace_id=DEFAULT_WORKSPACE_ID, project_id=project["id"],
                       turn_id=f"t{i:02d}", prompt=f"p{i}", model=MODEL, effort="low",
                       status="completed", started_at=base + timedelta(minutes=i))
            for i in range(25)
        ])
        db.commit()
    finally:
        db.close()
    detail = client.get(f"/api/coding/projects/{project['id']}").json()
    assert [t["turn_id"] for t in detail["turns"]] == [f"t{i:02d}" for i in range(5, 25)]


def test_a_member_asking_for_all_sees_only_their_own(ada, bob):
    _, a = ada
    _, b = bob
    _create(a)
    mine = _create(b, "bob's")
    assert [p["id"] for p in b.get("/api/coding/projects?all=1").json()["projects"]] == [mine["id"]]


def test_the_config_admin_owns_its_own_projects(admin, ada):
    _, member = ada
    project = _create(admin)
    assert project["owned"] is True and project["created_by"] is None
    assert _project(project["id"]).owner_principal == "config-admin"
    assert admin.get(f"/api/coding/projects/{project['id']}").status_code == 200
    assert member.get(f"/api/coding/projects/{project['id']}").status_code == 404


def test_the_local_operator_owns_projects_when_auth_is_off(client):
    project = _create(client)
    assert _project(project["id"]).owner_principal == "local-operator"
    assert client.get(f"/api/coding/projects/{project['id']}").status_code == 200


def test_a_null_owner_matches_nobody(ada):
    _, client = ada
    project = _create(client)
    _set(project["id"], owner_principal=None)
    assert client.get(f"/api/coding/projects/{project['id']}").status_code == 404
    assert client.get("/api/coding/projects").json()["projects"] == []


def test_delete_stops_the_session_deletes_the_prefix_and_the_rows(ada, fake, s3, stopped):
    _, client = ada
    project = _create(client)
    session = _session_of(project)
    prefix = s3urls.project_prefix(DEFAULT_WORKSPACE_ID, project["id"])
    s3.objects |= {f"{prefix}snapshots/a.tgz", f"{prefix}exports/b.zip",
                   f"{prefix}downloads/20261020T000000000000Z-ab12cd34/p.zip",
                   "coding/other/x.tgz"}
    assert client.delete(f"/api/coding/projects/{project['id']}").json() == {"deleted": True}
    assert stopped == [session]
    assert s3.objects == {"coding/other/x.tgz"}
    db = SessionLocal()
    try:
        assert db.get(CodingProject, project["id"]) is None
    finally:
        db.close()


def test_delete_is_refused_while_a_turn_runs_and_fails_loudly_on_s3(ada, fake, s3, stopped):
    _, client = ada
    project = _create(client)
    _set(project["id"], status="running", active_turn_id="t")
    refused = client.delete(f"/api/coding/projects/{project['id']}")
    assert refused.status_code == 409 and refused.json()["code"] == "coding.turn_running"
    _set(project["id"], status="idle", active_turn_id=None)
    s3.objects.add(f"{s3urls.project_prefix(DEFAULT_WORKSPACE_ID, project['id'])}snapshots/a")
    s3.fail_delete = True
    failed = client.delete(f"/api/coding/projects/{project['id']}")
    assert failed.status_code == 502 and failed.json()["code"] == "coding.delete_failed"
    assert _project(project["id"]).status == "idle", "a failed delete is retryable"


def test_delete_without_the_runtime_skips_the_session_stop(ada, s3, stopped):
    _, client = ada
    project = _create(client)
    set_default_resources({"artifacts_bucket": BUCKET})
    assert client.delete(f"/api/coding/projects/{project['id']}").status_code == 200
    assert stopped == []


# ── turns ─────────────────────────────────────────────────────────────────────


def test_start_returns_the_running_turn_and_a_second_start_is_refused(ada, fake, s3):
    user_id, client = ada
    project = _create(client)
    started = _start(client, project["id"])
    assert started.status_code == 202, started.text
    turn = started.json()
    assert turn["status"] == "running" and turn["turn_id"] == "turn-1"
    assert turn["prompt"] == "build it" and turn["model"] == MODEL and turn["effort"] == "low"
    assert set(turn) >= {"id", "turn_id", "prompt", "model", "effort", "status", "error",
                         "usage", "started_at", "completed_at"}
    row = _project(project["id"])
    assert row.status == "running" and row.active_turn_id == "turn-1"
    assert row.model == MODEL and row.effort == "low"
    session, payload = fake.actions("start_turn")[0]
    assert session == row.session_id and payload["prompt"] == "build it"
    assert "restore_url" not in payload, "no snapshot yet → nothing to restore"
    db = SessionLocal()
    try:
        mapped = db.get(UsageSession, (row.session_id, DEFAULT_WORKSPACE_ID))
        assert mapped is not None and mapped.user_id == user_id and mapped.resource_id == ARN
        assert mapped.source == "coding"
    finally:
        db.close()
    again = _start(client, project["id"])
    assert again.status_code == 409 and again.json()["code"] == "coding.turn_running"
    assert len(fake.actions("start_turn")) == 1
    detail = client.get(f"/api/coding/projects/{project['id']}").json()
    assert detail["status"] == "running" and detail["active_turn"] == "turn-1"
    assert [t["turn_id"] for t in detail["turns"]] == ["turn-1"]


def test_start_defaults_the_model_and_effort(ada, fake):
    _, client = ada
    project = _create(client)
    response = client.post(f"/api/coding/projects/{project['id']}/turns", json={"prompt": "x"})
    assert response.status_code == 202
    assert response.json()["model"] == get_settings().coding_default_model
    assert response.json()["effort"] == get_settings().coding_default_effort


@pytest.mark.parametrize("body, status_code, code", [
    ({"model": "global.openai.gpt-5"}, 422, "coding.model_invalid"),
    ({"effort": "extreme"}, 422, "coding.effort_invalid"),
    ({"prompt": "é" * 20000}, 413, "coding.prompt_too_long"),
    ({"prompt": "   "}, 422, "validation.invalid_request"),
])
def test_start_validates_before_any_runtime_call(ada, fake, body, status_code, code):
    _, client = ada
    project = _create(client)
    response = _start(client, project["id"], **body)
    assert response.status_code == status_code and response.json()["code"] == code
    assert fake.calls == [] and _project(project["id"]).status == "idle"


def test_a_model_not_in_the_installed_list_needs_a_repair(ada, fake):
    _, client = ada
    project = _create(client)
    set_default_resources({**INSTALLED, infra.KEY_MODELS: ["global.openai.gpt-6-astra"]})
    response = _start(client, project["id"], model=MODEL)
    assert response.status_code == 422 and response.json()["code"] == "coding.model_invalid"


def test_start_without_the_runtime_is_409_not_installed(ada, fake):
    _, client = ada
    project = _create(client)
    set_default_resources({"artifacts_bucket": BUCKET})
    response = _start(client, project["id"])
    assert response.status_code == 409
    assert response.json()["code"] == "coding.runtime_not_installed"
    events = client.get(f"/api/coding/projects/{project['id']}/events")
    assert events.status_code == 409
    assert events.json()["code"] == "coding.runtime_not_installed"


def test_start_is_refused_while_the_runtime_is_being_uninstalled(ada, fake):
    _, client = ada
    project = _create(client)
    db = SessionLocal()
    try:
        install.request_job(db, DEFAULT_WORKSPACE_ID, install.UNINSTALL_JOB_TYPE,
                            requested_by=None, requested_by_name="operator")
    finally:
        db.close()
    response = _start(client, project["id"])
    assert response.status_code == 409 and response.json()["code"] == "coding.runtime_busy"


def test_a_runtime_failure_releases_the_claim(ada, fake):
    _, client = ada
    project = _create(client)
    fake.errors["start_turn"] = CodingRuntimeError("runtime_unavailable", "throttled",
                                                   aws_code="ThrottlingException")
    response = _start(client, project["id"])
    assert response.status_code == 502 and response.json()["code"] == "coding.runtime_error"
    assert response.json()["detail"]["aws_error_code"] == "ThrottlingException"
    row = _project(project["id"])
    assert row.status == "idle" and row.active_turn_id is None and _turns(project["id"]) == []


def test_a_runtime_turn_the_ledger_lost_is_adopted(ada, fake):
    _, client = ada
    project = _create(client)
    session = _session_of(project)
    fake.active[session] = {"turn_id": "turn-lost", "thread_id": "th", "model": MODEL,
                            "effort": "low"}
    response = _start(client, project["id"])
    assert response.status_code == 409 and response.json()["code"] == "coding.turn_running"
    row = _project(project["id"])
    assert row.status == "running" and row.active_turn_id == "turn-lost"
    assert [t.turn_id for t in _turns(project["id"])] == ["turn-lost"]


def _user_level(user_id: str, usd: float) -> None:
    db = SessionLocal()
    try:
        level = BudgetLevel(name=f"lvl-{user_id[:6]}", monthly_budget_usd=usd)
        db.add(level)
        db.flush()
        db.get(User, user_id).budget_level_id = level.id
        db.commit()
    finally:
        db.close()


def test_a_member_over_budget_gets_402_before_the_runtime(ada, fake):
    user_id, client = ada
    project = _create(client)
    _user_level(user_id, 1.0)
    db = SessionLocal()
    try:
        from app.services import budgets

        db.add(UsageEvent(user_id=user_id, period=budgets.period_of(), source="chat",
                          usd=2.0))
        db.commit()
    finally:
        db.close()
    response = _start(client, project["id"])
    assert response.status_code == 402 and response.json()["code"] == "budget.exceeded"
    assert fake.calls == []


def test_an_unpriced_model_is_refused_for_a_budgeted_member(ada, fake, monkeypatch):
    user_id, client = ada
    project = _create(client)
    _user_level(user_id, 100.0)
    unpriced = "global.openai.gpt-9-test"
    monkeypatch.setattr(get_settings(), "coding_models", [*get_settings().coding_models,
                                                          unpriced])
    set_default_resources({**INSTALLED, infra.KEY_MODELS: [*INSTALLED[infra.KEY_MODELS],
                                                           unpriced]})
    response = _start(client, project["id"], model=unpriced)
    assert response.status_code == 402 and response.json()["code"] == "budget.model_unpriced"
    assert fake.calls == [] and _project(project["id"]).status == "idle"


def test_interrupt_asks_the_runtime_only_while_running(ada, fake):
    _, client = ada
    project = _create(client)
    idle = client.post(f"/api/coding/projects/{project['id']}/interrupt")
    assert idle.json() == {"interrupted": False} and fake.actions("interrupt") == []
    _start(client, project["id"])
    assert client.post(f"/api/coding/projects/{project['id']}/interrupt").json() == {
        "interrupted": True}


# ── settlement ────────────────────────────────────────────────────────────────


def test_the_watcher_settles_charges_once_and_snapshots(ada, fake, s3):
    user_id, client = ada
    project = _create(client)
    _start(client, project["id"])
    session = _session_of(project)
    assert watcher.run_once()["settled"] == [], "the turn still runs"
    turn_id = fake.finish(session)
    outcome = watcher.run_once()
    assert outcome["settled"] == [turn_id] and outcome["failures"] == []
    (turn,) = _turns(project["id"])
    assert turn.status == "completed" and turn.settled_at is not None
    assert turn.usage == {"input": 94630, "cached_input": 58007, "cache_write": 36613,
                          "output": 275, "reasoning_output": 8}
    row = _project(project["id"])
    assert row.status == "idle" and row.active_turn_id is None
    assert row.last_seq == fake.seq[session]
    (usage,) = _usage_rows()
    assert usage.user_id == user_id and usage.model_id == MODEL
    assert (usage.tokens_in, usage.cache_read, usage.cache_write, usage.tokens_out) == (
        10, 58007, 36613, 275), "codex counts made disjoint — nothing double counted"
    assert usage.usd == pytest.approx((10 * 2 + 275 * 10 + 58007 * 0.2 + 36613 * 2.5) / 1e6)
    assert usage.ref_type == "coding_turn" and usage.ref_id == turn.id
    assert usage.session_id == row.session_id
    assert row.snapshot_key and row.snapshot_key.startswith(
        f"coding/{DEFAULT_WORKSPACE_ID}/{project['id']}/snapshots/")
    assert row.snapshot_at is not None and len(fake.actions("snapshot")) == 1
    # a second pass, and the SSE path reading the same records, change nothing
    assert watcher.run_once()["settled"] == []
    turns.settle_from_records(project["id"], fake.logs[session], {})
    assert len(_usage_rows()) == 1 and len(fake.actions("snapshot")) == 1
    detail = client.get(f"/api/coding/projects/{project['id']}").json()
    assert detail["turns"][0]["usage"]["input"] == 94630 and detail["snapshot_at"]


def test_time_floored_event_seqs_round_trip_through_the_ledger(ada, fake, s3, clock):
    """A log (re)started on empty or restored session storage begins at
    Date.now() * 1000 (~1.8e15, eventlog.mjs advanceTo): the cursors are 64-bit."""
    _, client = ada
    project = _create(client)
    session = _session_of(project)
    floor = 1_792_000_000_000_000
    fake.seq[session] = floor
    assert _start(client, project["id"]).status_code == 202
    assert _project(project["id"]).last_seq == floor
    fake.finish(session)
    assert len(watcher.run_once()["settled"]) == 1
    row = _project(project["id"])
    assert row.last_seq == row.end_seq == fake.seq[session] > 2**31
    gen = stream.events(_ctx(), project["id"], row.end_seq)
    assert next(gen).startswith("event: status")
    assert next(gen) == stream.KEEPALIVE, "resumed past the end: idle, no replay"
    gen.close()


def test_racing_settlements_charge_exactly_once(ada, fake, s3):
    import threading

    _, client = ada
    project = _create(client)
    _start(client, project["id"])
    session = _session_of(project)
    turn_id = fake.finish(session)
    results: list[bool] = []
    barrier = threading.Barrier(4)

    def race():
        barrier.wait()
        results.append(turns.settle(project["id"], turn_id, status="completed", usage=USAGE))

    threads = [threading.Thread(target=race) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(results) == [False, False, False, True]
    assert len(_usage_rows()) == 1


def test_an_interrupted_turn_is_settled_and_charged(ada, fake, s3):
    _, client = ada
    project = _create(client)
    _start(client, project["id"])
    fake.finish(_session_of(project), status="interrupted")
    watcher.run_once()
    (turn,) = _turns(project["id"])
    assert turn.status == "interrupted" and len(_usage_rows()) == 1
    assert _start(client, project["id"]).status_code == 202, "a new turn is accepted"


def test_a_failed_turn_without_usage_charges_nothing(ada, fake, s3):
    _, client = ada
    project = _create(client)
    _start(client, project["id"])
    zero = {k: 0 for k in USAGE}
    fake.finish(_session_of(project), status="failed", usage=zero, error="model refused")
    watcher.run_once()
    (turn,) = _turns(project["id"])
    assert turn.status == "failed" and turn.error == "model refused"
    assert _usage_rows() == []


def test_an_abandoned_turn_is_settled_with_its_last_usage(ada, fake, s3):
    _, client = ada
    project = _create(client)
    started = _start(client, project["id"]).json()
    session = _session_of(project)
    fake.active.pop(session)
    fake.append(session, "launchpad/turnAbandoned",
                {"turnId": started["turn_id"], "reason": "runtime_restarted", "usage": USAGE})
    watcher.run_once()
    (turn,) = _turns(project["id"])
    assert turn.status == "abandoned" and "runtime_restarted" in turn.error
    (usage,) = _usage_rows()
    assert usage.detail.get("failed") is True


def test_a_failed_settlement_is_read_again_not_abandoned(ada, fake, s3, monkeypatch):
    """A settlement that raised (a locked ledger) must not let the watcher's cursor
    pass the end record: past it the turn would look lost and go uncharged."""
    _, client = ada
    project = _create(client)
    _start(client, project["id"])
    session = _session_of(project)
    turn_id = fake.finish(session)
    real_settle = turns.settle
    attempts = {"n": 0}

    def flaky_settle(*args, **kwargs):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RuntimeError("database is locked")
        return real_settle(*args, **kwargs)

    monkeypatch.setattr(turns, "settle", flaky_settle)
    first = watcher.run_once()
    assert first["settled"] == [] and first["failures"] == []
    row = _project(project["id"])
    assert row.status == "running" and row.last_seq < fake.seq[session]
    assert _turns(project["id"])[0].status == "running", "not abandoned"
    assert watcher.run_once()["settled"] == [turn_id]
    (turn,) = _turns(project["id"])
    assert turn.status == "completed" and len(_usage_rows()) == 1
    assert _project(project["id"]).last_seq == fake.seq[session]


def test_a_turn_the_runtime_forgot_is_abandoned_by_the_watcher(ada, fake, s3):
    _, client = ada
    project = _create(client)
    _start(client, project["id"])
    session = _session_of(project)
    fake.active.pop(session)  # session storage came back from an older snapshot
    fake.seq[session] += 1_000_000
    watcher.run_once()
    (turn,) = _turns(project["id"])
    assert turn.status == "abandoned" and _usage_rows() == []
    assert _project(project["id"]).status == "idle"


def test_a_turn_past_the_microvm_lifetime_is_abandoned(ada, fake, s3):
    _, client = ada
    project = _create(client)
    _start(client, project["id"])
    assert watcher.run_once()["settled"] == []
    later = datetime.now(UTC) + timedelta(hours=9)
    assert watcher.run_once(now=later)["settled"] == ["turn-1"]
    assert _turns(project["id"])[0].status == "abandoned"


def test_a_dead_start_claim_is_released(ada):
    _, client = ada
    project = _create(client)
    _set(project["id"], status="running", active_turn_id=None,
         updated_at=datetime.now(UTC) - timedelta(minutes=6))
    assert watcher.run_once()["released"] == 1
    assert _project(project["id"]).status == "idle"


def test_the_watcher_is_off_by_switch_and_at_most_one_thread(monkeypatch):
    import threading

    assert watcher.start(get_settings()) is None  # conftest switches it off
    parked = threading.Event()
    monkeypatch.setattr(watcher, "_THREAD", None)  # restored at teardown
    monkeypatch.setattr(watcher, "_sleep", lambda seconds: parked.wait())
    settings = get_settings().model_copy(update={"coding_watcher_enabled": True})
    first = watcher.start(settings)
    assert first is not None and watcher.start(settings) is first and watcher.running()


# ── snapshots ─────────────────────────────────────────────────────────────────


def test_restore_url_is_folded_once_a_snapshot_exists(ada, fake, s3):
    _, client = ada
    project = _create(client)
    client.get(f"/api/coding/projects/{project['id']}/tree")
    assert "restore_url" not in fake.actions("tree")[0][1]
    snapshots.take(project["id"])
    key = _project(project["id"]).snapshot_key
    client.get(f"/api/coding/projects/{project['id']}/tree")
    assert fake.actions("tree")[1][1]["restore_url"] == f"https://get/{key}"


def test_snapshots_are_pruned_to_the_newest_three(ada, fake, s3):
    _, client = ada
    project = _create(client)
    for _ in range(5):
        snapshots.take(project["id"])
    prefix = f"{s3urls.project_prefix(DEFAULT_WORKSPACE_ID, project['id'])}snapshots/"
    kept = sorted(k for k in s3.objects if k.startswith(prefix))
    assert len(kept) == 3 and _project(project["id"]).snapshot_key == kept[-1]


def test_a_save_is_snapshotted_after_the_debounce(ada, fake, s3):
    _, client = ada
    project = _create(client)
    saved = client.put(f"/api/coding/projects/{project['id']}/file",
                       json={"path": "main.py", "content": "print(1)"})
    assert saved.status_code == 200
    dirty = _project(project["id"]).dirty_at
    assert dirty is not None
    assert watcher.run_once()["snapshots"] == 0, "debounced"
    later = datetime.now(UTC) + timedelta(seconds=31)
    assert watcher.run_once(now=later)["snapshots"] == 1
    row = _project(project["id"])
    assert row.dirty_at is None and row.snapshot_key


def test_a_failed_background_snapshot_stays_dirty_and_backs_off(ada, fake, s3):
    _, client = ada
    project = _create(client)
    fake.errors["snapshot"] = CodingRuntimeError("upload_failed", "S3 said 403")
    assert snapshots.take_quietly(project["id"]) is False
    assert _project(project["id"]).dirty_at is not None
    later = datetime.now(UTC) + timedelta(minutes=1)
    assert watcher.run_once(now=later)["snapshots"] == 0
    assert len(fake.actions("snapshot")) == 1, "backed off"


# ── files ─────────────────────────────────────────────────────────────────────


def test_tree_is_proxied_root_relative(ada, fake):
    _, client = ada
    project = _create(client)
    tree = client.get(f"/api/coding/projects/{project['id']}/tree?path=.&depth=1").json()
    assert tree["entries"] == [
        {"path": "src", "dir": True, "size": None},
        {"path": "main.py", "dir": False, "size": 12},
        {"path": "link", "dir": False, "size": 0, "symlink": True},
    ]
    assert fake.actions("tree")[0][1] == {"path": ".", "depth": 1}
    gone = client.get(f"/api/coding/projects/{project['id']}/tree?path=gone").json()
    assert gone["entries"] == []


@pytest.mark.parametrize("path", ["../etc/passwd", "a/../../b", "/etc/passwd", ".git/config",
                                  "C:/x", "a\x00b"])
def test_bad_paths_are_refused_before_any_runtime_call(ada, fake, path):
    _, client = ada
    project = _create(client)
    base = f"/api/coding/projects/{project['id']}"
    for response in (
        client.get(f"{base}/file", params={"path": path}),
        client.put(f"{base}/file", json={"path": path, "content": "x"}),
        client.get(f"{base}/tree", params={"path": path}),
    ):
        assert response.status_code == 422, response.text
        assert response.json()["code"] == "coding.path_invalid"
    assert fake.calls == []


def test_read_file_shapes_text_binary_missing_and_revisions(ada, fake):
    _, client = ada
    project = _create(client)
    session = _session_of(project)
    fake.files[session]["main.py"] = "print(1)"
    base = f"/api/coding/projects/{project['id']}/file"
    text = client.get(base, params={"path": "main.py"}).json()
    assert text == {"path": "main.py", "exists": True, "content": "print(1)",
                    "sha256": "sha-print(1)", "size": 8, "binary": False}
    binary = client.get(base, params={"path": "logo.png"}).json()
    assert binary["binary"] is True and binary["content"] is None
    missing = client.get(base, params={"path": "nope.py"}).json()
    assert missing["exists"] is False and missing["content"] is None
    no_turn = client.get(base, params={"path": "main.py", "rev": "turn"}).json()
    assert no_turn["exists"] is False, "no turn yet: the turn revision is empty"
    client.get(base, params={"path": "main.py", "rev": "head"})
    assert fake.actions("read_file")[-1][1]["rev"] == "head"
    assert client.get(base, params={"path": "main.py", "rev": "bogus"}).status_code == 422


def test_write_conflict_turn_running_and_size(ada, fake):
    _, client = ada
    project = _create(client)
    session = _session_of(project)
    fake.files[session]["main.py"] = "v2"
    base = f"/api/coding/projects/{project['id']}/file"
    stale = client.put(base, json={"path": "main.py", "content": "mine",
                                   "expected_sha256": "sha-v1"})
    assert stale.status_code == 409 and stale.json()["code"] == "coding.file_conflict"
    assert stale.json()["detail"] == {"sha256": "sha-v2"}
    ok = client.put(base, json={"path": "main.py", "content": "mine", "expected_sha256": "sha-v2"})
    assert ok.status_code == 200 and ok.json()["sha256"] == "sha-mine"
    new = client.put(base, json={"path": "new.py", "content": "n", "expected_sha256": None})
    assert new.status_code == 200
    assert fake.actions("write_file")[-1][1]["expected_sha256"] is None, "null passes through"
    client.put(base, json={"path": "main.py", "content": "force"})
    assert "expected_sha256" not in fake.actions("write_file")[-1][1], "omitted = overwrite"
    big = client.put(base, json={"path": "big.txt", "content": "x" * (2 * 1024 * 1024 + 1)})
    assert big.status_code == 413 and big.json()["code"] == "coding.file_too_large"
    calls = len(fake.calls)
    _set(project["id"], status="running", active_turn_id="t")
    busy = client.put(base, json={"path": "main.py", "content": "y"})
    assert busy.status_code == 409 and busy.json()["code"] == "coding.turn_running"
    assert len(fake.calls) == calls


def test_changes_and_diff_are_proxied_and_empty_before_a_turn(ada, fake):
    _, client = ada
    project = _create(client)
    base = f"/api/coding/projects/{project['id']}"
    changes = client.get(f"{base}/changes?scope=worktree").json()
    assert changes["changes"] == [{"path": "main.py", "status": "M"},
                                  {"path": "b.py", "status": "R", "old_path": "a.py"}]
    assert client.get(f"{base}/changes?scope=turn").json()["changes"] == []
    assert client.get(f"{base}/diff?scope=turn").json()["diff"] == ""
    assert client.get(f"{base}/diff").json()["diff"].startswith("--- a/main.py")
    assert client.get(f"{base}/changes?scope=bogus").status_code == 422


def test_a_restore_failure_is_its_own_error(ada, fake, s3):
    _, client = ada
    project = _create(client)
    fake.errors["tree"] = CodingRuntimeError("restore_failed", "snapshot download failed")
    response = client.get(f"/api/coding/projects/{project['id']}/tree")
    assert response.status_code == 502 and response.json()["code"] == "coding.restore_failed"


# ── the event stream ──────────────────────────────────────────────────────────


def _frames(body: str) -> list[dict[str, Any]]:
    import json

    out = []
    for chunk in body.split("\n\n"):
        if not chunk.strip():
            continue
        if chunk.startswith(":"):
            out.append({"comment": chunk})
            continue
        frame: dict[str, Any] = {}
        for line in chunk.split("\n"):
            field, _, value = line.partition(": ")
            frame[field] = json.loads(value) if field == "data" else value
        out.append(frame)
    return out


class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += seconds


@pytest.fixture
def clock(monkeypatch) -> Clock:
    c = Clock()
    monkeypatch.setattr(stream, "_now", c.now)
    monkeypatch.setattr(stream, "_sleep", c.sleep)
    return c


def _ctx():
    from app.services.workspace import context_for_workspace

    return context_for_workspace(DEFAULT_WORKSPACE_ID)


def test_sse_framing_status_first_then_records_and_reconnect(ada, fake, s3, clock):
    _, client = ada
    project = _create(client)
    _start(client, project["id"])
    session = _session_of(project)
    fake.append(session, "item/agentMessage/delta", {"turnId": "turn-1", "itemId": "m",
                                                      "delta": "hel"})

    def advancing_events(s, p):
        clock.t += 600  # every long-poll "takes" 10 min
        return FakeRuntime.do_events(fake, s, p)

    fake.do_events = advancing_events
    body = "".join(stream.events(_ctx(), project["id"], 0))
    frames = _frames(body)
    assert frames[0]["event"] == "status"
    assert frames[0]["data"] == {"project_status": "running", "active_turn": "turn-1"}
    records = [f for f in frames if f.get("event") == "event"]
    assert [r["id"] for r in records] == ["1", "2"]
    assert records[1]["data"]["method"] == "item/agentMessage/delta"
    assert any("comment" in f for f in frames), "an empty long-poll is a keep-alive"
    assert frames[-1]["event"] == "reconnect"
    waits = [p["wait_ms"] for _, p in fake.actions("events")]
    assert set(waits) == {stream.POLL_WAIT_MS}, "a running project long-polls"
    assert all(p["compact"] is True for _, p in fake.actions("events"))


def test_sse_resumes_after_the_cursor_and_last_event_id(ada, fake, s3, clock, monkeypatch):
    _, client = ada
    project = _create(client)
    seen: list[int] = []

    async def body(ctx, pid, after):
        seen.append(after)
        for chunk in ():
            yield chunk

    monkeypatch.setattr(stream, "body", body)
    base = f"/api/coding/projects/{project['id']}/events"
    client.get(f"{base}?after=7")
    client.get(base, headers={"Last-Event-ID": "12"})
    client.get(f"{base}?after=3", headers={"Last-Event-ID": "9"})
    assert seen == [7, 12, 9]
    response = client.get(f"{base}?after=1")
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"] == "no-cache"


def test_sse_runtime_failure_is_an_error_frame(ada, fake, s3, clock):
    _, client = ada
    project = _create(client)
    fake.errors["events"] = CodingRuntimeError("runtime_unavailable", "boom")
    frames = _frames("".join(stream.events(_ctx(), project["id"], 0)))
    assert frames[0]["event"] == "status"
    assert frames[-1] == {"event": "error",
                          "data": {"code": "coding.runtime_error", "message": "boom"}}


def test_sse_idle_mode_polls_the_ledger_not_the_runtime(ada, fake, s3, clock):
    _, client = ada
    project = _create(client)
    session = _session_of(project)
    fake.append(session, "launchpad/restored", {"bytes": 1})
    gen = stream.events(_ctx(), project["id"], 0)
    assert next(gen).startswith("event: status")
    assert "launchpad/restored" in next(gen)
    for _ in range(40):  # 10 min of idle ledger polls (a keep-alive every 15 s)
        chunk = next(gen)
        assert chunk.startswith((": keep-alive", "event: status")) is True
    events_calls = fake.actions("events")
    assert len(events_calls) == 1 and events_calls[0][1]["wait_ms"] == 0, (
        "the replay drained without waiting, then the runtime was left alone")
    # a turn is claimed in the ledger → back to runtime long-polls
    _set(project["id"], status="running", active_turn_id="turn-9")
    fake.active[session] = {"turn_id": "turn-9", "thread_id": "t", "model": MODEL,
                            "effort": "low"}
    chunk = next(gen)
    while not chunk.startswith("event: status"):
        chunk = next(gen)
    assert '"active_turn": "turn-9"' in chunk
    next(gen)
    assert fake.actions("events")[-1][1]["wait_ms"] == stream.POLL_WAIT_MS
    gen.close()


def test_sse_settles_a_turn_it_forwards_and_goes_idle(ada, fake, s3, clock):
    _, client = ada
    project = _create(client)
    _start(client, project["id"])
    session = _session_of(project)
    fake.finish(session)
    gen = stream.events(_ctx(), project["id"], 0)
    chunks = [next(gen) for _ in range(8)]
    gen.close()
    assert any("launchpad/turnUsage" in c for c in chunks)
    (turn,) = _turns(project["id"])
    assert turn.settled_at is not None and len(_usage_rows()) == 1
    assert _project(project["id"]).status == "idle"
    assert len(fake.actions("events")) == 1, "drained once, then the ledger"


def test_sse_resuming_an_idle_project_past_its_last_turn_leaves_the_microvm_asleep(
        ada, fake, s3, clock):
    _, client = ada
    project = _create(client)
    _start(client, project["id"])
    session = _session_of(project)
    fake.finish(session)
    watcher.run_once()
    end = _project(project["id"]).end_seq
    assert end == fake.seq[session], "the turnUsage record's seq"
    calls = len(fake.actions("events"))
    gen = stream.events(_ctx(), project["id"], end)
    assert next(gen).startswith("event: status")
    for _ in range(5):
        assert next(gen) == stream.KEEPALIVE
    gen.close()
    assert len(fake.actions("events")) == calls, "a reconnect of an idle tab: no replay"
    behind = stream.events(_ctx(), project["id"], end - 1)
    next(behind)
    next(behind)
    behind.close()
    assert len(fake.actions("events")) == calls + 1, "a client that missed the end replays"


def test_sse_keeps_reading_a_turn_recorded_while_its_page_was_read(ada, fake, s3, clock):
    """A long-poll woken by `turn/started` can answer before the runtime marks the
    turn active, while the ledger already recorded it: the stream must not take the
    new turn for a finished one and fall asleep."""
    _, client = ada
    project = _create(client)
    session = _session_of(project)
    first = True

    def racing_events(s, p):
        nonlocal first
        if first:
            first = False
            fake.append(s, "turn/started", {"turn": {"id": "turn-new"}})
            _set(project["id"], status="running", active_turn_id="turn-new")
            page = FakeRuntime.do_events(fake, s, p)
            page["active_turn"] = None
            return page
        fake.active[s] = {"turn_id": "turn-new", "thread_id": "t", "model": MODEL,
                          "effort": "low"}
        return FakeRuntime.do_events(fake, s, p)

    fake.do_events = racing_events
    gen = stream.events(_ctx(), project["id"], 0)
    next(gen)  # status
    assert "turn/started" in next(gen)
    assert next(gen) == stream.KEEPALIVE  # the second read: a long-poll, still runtime
    gen.close()
    reads = fake.actions("events")
    assert len(reads) == 2 and reads[1][1]["wait_ms"] == stream.POLL_WAIT_MS
    assert session


def test_sse_an_unexpected_failure_is_an_error_frame_not_an_exception(
        ada, fake, s3, clock, monkeypatch):
    _, client = ada
    project = _create(client)
    calls = {"n": 0}
    real_load = stream._load

    def flaky_load(project_id):
        calls["n"] += 1
        if calls["n"] > 1:
            raise RuntimeError("database is locked")
        return real_load(project_id)

    monkeypatch.setattr(stream, "_load", flaky_load)
    frames = _frames("".join(stream.events(_ctx(), project["id"], 0)))
    assert [f.get("event") for f in frames] == ["status", "error"]
    assert frames[-1]["data"]["code"] == "coding.runtime_error"
    assert "database" not in frames[-1]["data"]["message"], "internals stay in the log"


def test_sse_route_body_pauses_on_the_event_loop_not_in_a_worker_thread(
        ada, fake, s3, clock, monkeypatch):
    """Starlette runs a sync body's every `next()` in the shared worker pool: an
    idle IDE tab must not hold one of its threads while it waits."""
    import anyio

    _, client = ada
    project = _create(client)
    blocking: list[float] = []
    awaited: list[float] = []
    monkeypatch.setattr(stream, "_sleep", lambda seconds: blocking.append(seconds))

    async def fake_async_sleep(seconds):
        awaited.append(seconds)
        clock.t += seconds

    monkeypatch.setattr(stream, "_async_sleep", fake_async_sleep)

    async def read(n):
        chunks = []
        gen = stream.body(_ctx(), project["id"], 0)
        try:
            async for chunk in gen:
                chunks.append(chunk)
                if len(chunks) == n:
                    break
        finally:
            await gen.aclose()
        return chunks

    chunks = anyio.run(read, 4)
    assert chunks[0].startswith("event: status")
    assert chunks[1:] == [stream.KEEPALIVE] * 3, "an idle project polls the ledger"
    assert awaited and set(awaited) == {stream.IDLE_POLL_SECONDS}
    assert blocking == [], "no pause slept inside a worker thread"
    assert len(fake.actions("events")) == 1, "the replay drained once, then the ledger"


def test_sse_route_streams_through_the_owner_check(ada, fake, s3, clock):
    _, client = ada
    project = _create(client)
    fake.errors["events"] = CodingRuntimeError("runtime_unavailable", "stop")
    response = client.get(f"/api/coding/projects/{project['id']}/events?after=0")
    frames = _frames(response.text)
    assert [f.get("event") for f in frames] == ["status", "error"]


# ── downloads ─────────────────────────────────────────────────────────────────


def _download(client: TestClient, project_id: str, **body) -> Any:
    return client.post(f"/api/coding/projects/{project_id}/download", json=body)


def _presigned(s3: FakeS3, method: str) -> list[tuple[dict[str, Any], int]]:
    return [(params, ttl) for m, params, ttl in s3.presigned if m == method]


def test_a_file_download_stages_the_file_and_answers_a_short_lived_attachment_url(
        ada, fake, s3):
    _, client = ada
    project = _create(client)
    session = _session_of(project)
    fake.files[session]["src/main.py"] = "print(1)"
    response = _download(client, project["id"], path="./src//main.py")
    assert response.status_code == 200, response.text
    body = response.json()
    [(put, put_ttl)] = _presigned(s3, "put_object")
    key = put["Key"]
    prefix = s3urls.downloads_prefix(DEFAULT_WORKSPACE_ID, project["id"])
    assert key.startswith(prefix) and key.endswith("/main.py")
    stamp_and_rand = key[len(prefix):].split("/")[0]
    assert len(stamp_and_rand.split("-")[1]) == 8, "a random part keeps keys apart"
    assert put_ttl <= s3urls.URL_TTL_S
    assert fake.actions("upload_file") == [
        (session, {"path": "src/main.py", "put_url": f"https://put/{key}"})]
    [(get, get_ttl)] = _presigned(s3, "get_object")
    assert get == {
        "Bucket": BUCKET, "Key": key,
        "ResponseContentDisposition": "attachment; filename=\"main.py\"; "
                                      "filename*=UTF-8''main.py",
        "ResponseContentType": "application/octet-stream",
    }
    assert get_ttl == 300
    assert body == {"url": f"https://get/{key}", "filename": "main.py", "size": 8,
                    "expires_in": 300}


def test_the_project_downloads_as_a_zip_of_the_export_file_set(ada, fake, s3):
    _, client = ada
    project = _create(client, "Weather Bot!")
    session = _session_of(project)
    for body in ({}, {"path": None}):
        response = _download(client, project["id"], **body)
        assert response.status_code == 200, response.text
        assert response.json()["filename"] == "weather-bot.zip"
        assert response.json()["size"] == 456
    exports = fake.actions("export")
    assert len(exports) == 2 and all(s == session and set(p) == {"put_url"}
                                     for s, p in exports)
    assert fake.actions("upload_file") == []
    get, _ = _presigned(s3, "get_object")[-1]
    assert get["Key"].endswith("/weather-bot.zip")
    assert get["ResponseContentType"] == "application/zip"
    assert get["ResponseContentDisposition"] == (
        "attachment; filename=\"weather-bot.zip\"; filename*=UTF-8''weather-bot.zip")


def test_non_ascii_names_get_an_rfc5987_disposition_and_an_ascii_key(ada, fake, s3):
    _, client = ada
    project = _create(client, "天气 助手")
    session = _session_of(project)
    archive = _download(client, project["id"]).json()
    assert archive["filename"] == "天气-助手.zip"
    get, _ = _presigned(s3, "get_object")[-1]
    assert get["Key"].endswith("/coding.zip") and get["Key"].isascii()
    assert get["ResponseContentDisposition"] == (
        "attachment; filename=\"coding.zip\"; "
        "filename*=UTF-8''%E5%A4%A9%E6%B0%94-%E5%8A%A9%E6%89%8B.zip")
    fake.files[session]['docs/报告 "v1".md'] = "# hi"
    one = _download(client, project["id"], path='docs/报告 "v1".md').json()
    assert one["filename"] == "报告 v1.md", "quotes never reach the header"
    get, _ = _presigned(s3, "get_object")[-1]
    assert get["Key"].endswith("/__ v1.md")
    assert get["ResponseContentDisposition"] == (
        "attachment; filename=\"__ v1.md\"; filename*=UTF-8''%E6%8A%A5%E5%91%8A%20v1.md")


@pytest.mark.parametrize("path", ["../etc/passwd", "a/../../b", "/etc/passwd", ".git/config",
                                  "C:/x", "a\x00b", "", "."])
def test_download_paths_are_validated_before_any_runtime_or_s3_call(ada, fake, s3, path):
    _, client = ada
    project = _create(client)
    response = _download(client, project["id"], path=path)
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "coding.path_invalid"
    assert fake.calls == [] and s3.presigned == []


def test_download_runtime_errors_map_to_console_codes(ada, fake, s3):
    _, client = ada
    project = _create(client)
    missing = _download(client, project["id"], path="nope.py")
    assert missing.status_code == 404 and missing.json()["code"] == "coding.file_not_found"
    assert missing.json()["detail"] == {"path": "nope.py"}
    directory = _download(client, project["id"], path="src")
    assert directory.status_code == 422 and directory.json()["code"] == "coding.path_invalid"
    fake.errors["upload_file"] = CodingRuntimeError(
        "file_too_large", "huge.bin is larger than 200 MB", detail={"size": 300 * 2**20})
    huge = _download(client, project["id"], path="huge.bin")
    assert huge.status_code == 413 and huge.json()["code"] == "coding.download_too_large"
    assert huge.json()["detail"] == {"max_bytes": 200 * 2**20, "size": 300 * 2**20}
    fake.errors["export"] = CodingRuntimeError("file_too_large", "the export is 61 MB")
    big = _download(client, project["id"])
    assert big.status_code == 413 and big.json()["detail"] == {"max_bytes": 50 * 2**20}
    fake.errors["export"] = CodingRuntimeError("upload_failed", "upload answered HTTP 403")
    failed = _download(client, project["id"])
    assert failed.status_code == 502 and failed.json()["code"] == "coding.runtime_error"
    fake.errors["upload_file"] = CodingRuntimeError("bad_request",
                                                    'unknown action "upload_file"')
    outdated = _download(client, project["id"], path="main.py")
    assert outdated.status_code == 409 and outdated.json()["code"] == "coding.runtime_outdated"
    assert _presigned(s3, "get_object") == [], "no download URL for a failed staging"


def test_download_without_the_runtime_is_409_before_any_aws_call(ada, fake, s3):
    _, client = ada
    project = _create(client)
    set_default_resources({"artifacts_bucket": BUCKET})
    response = _download(client, project["id"])
    assert response.status_code == 409
    assert response.json()["code"] == "coding.runtime_not_installed"
    assert fake.calls == [] and s3.presigned == []


def test_downloads_are_allowed_while_a_turn_runs_and_restore_first(ada, fake, s3):
    _, client = ada
    project = _create(client)
    session = _session_of(project)
    fake.files[session]["main.py"] = "x"
    _set(project["id"], status="running", active_turn_id="t", snapshot_key="coding/snap.tgz")
    response = _download(client, project["id"], path="main.py")
    assert response.status_code == 200, response.text
    _, payload = fake.actions("upload_file")[-1]
    assert payload["restore_url"] == "https://get/coding/snap.tgz"


def test_old_downloads_are_pruned_at_the_next_download(ada, fake, s3):
    _, client = ada
    project = _create(client)
    prefix = s3urls.downloads_prefix(DEFAULT_WORKSPACE_ID, project["id"])
    other = s3urls.downloads_prefix(DEFAULT_WORKSPACE_ID, "someone-else")
    now = datetime.now(UTC)
    old = f"{prefix}{s3urls._stamp(now - timedelta(minutes=20))}-aaaaaaaa/a.zip"
    recent = f"{prefix}{s3urls._stamp(now - timedelta(minutes=5))}-bbbbbbbb/b.zip"
    stray = f"{prefix}not-a-stamp/c.zip"
    snapshot = f"{s3urls.project_prefix(DEFAULT_WORKSPACE_ID, project['id'])}snapshots/s.tgz"
    foreign = f"{other}{s3urls._stamp(now - timedelta(hours=2))}-cccccccc/d.zip"
    s3.objects |= {old, recent, stray, snapshot, foreign}
    assert _download(client, project["id"]).status_code == 200
    assert old not in s3.objects and stray not in s3.objects
    assert {recent, snapshot, foreign} <= s3.objects
    [(put, _)] = _presigned(s3, "put_object")
    assert put["Key"] in s3.objects
    s3.fail_delete = True
    s3.objects.add(old)
    assert _download(client, project["id"]).status_code == 200, "a failed prune is not fatal"


def test_download_naming_units():
    assert downloads.archive_names("Weather Bot!") == ("weather-bot.zip", "weather-bot.zip")
    assert downloads.archive_names("42 things") == ("42-things.zip", "42-things.zip")
    assert downloads.archive_names("天气") == ("天气.zip", "coding.zip")
    assert downloads.archive_names("!!!") == ("coding.zip", "coding.zip")
    assert downloads.clean_filename("..") == "download"
    assert downloads.clean_filename('a"b\\c\r\nd\x00.txt') == "abcd.txt"
    header = downloads.content_disposition("x\r\nSet-Cookie: y=1.txt")
    assert "\r" not in header and "\n" not in header
    assert header == ("attachment; filename=\"xSet-Cookie_ y_1.txt\"; "
                      "filename*=UTF-8''xSet-Cookie%3A%20y%3D1.txt")
    assert downloads.content_disposition("ü.txt").startswith('attachment; filename="_.txt";')
    key = s3urls.download_key("ws", "p", "f.zip", datetime(2026, 10, 20, 1, 2, 3, tzinfo=UTC))
    assert key.startswith("coding/ws/p/downloads/20261020T010203000000Z-")
    assert key.endswith("/f.zip")
    pruned = s3urls.prune_downloads(
        SimpleNamespace(resources={"artifacts_bucket": BUCKET}), "ws", "p",
        now=datetime(2026, 10, 20, 1, 20, tzinfo=UTC),
        client=_PruneS3([key, "coding/ws/p/downloads/20261020T011000000000Z-ab/g.zip"]))
    assert pruned == [key]


class _PruneS3:
    def __init__(self, keys: list[str]) -> None:
        self.keys = keys

    def list_objects_v2(self, Bucket, Prefix, ContinuationToken=None):  # noqa: N803
        return {"Contents": [{"Key": k} for k in self.keys if k.startswith(Prefix)],
                "IsTruncated": False}

    def delete_objects(self, Bucket, Delete):  # noqa: N803
        return {}


# ── deploy handoff ────────────────────────────────────────────────────────────


def _agent(agent_id: str) -> Agent:
    db = SessionLocal()
    try:
        row = db.get(Agent, agent_id)
        db.expunge(row)
        return row
    finally:
        db.close()


def test_the_first_deploy_creates_a_byoc_agent_stamped_with_the_caller(
        ada, fake, s3, staged, deploys):
    user_id, client = ada
    project = _create(client, "Weather Bot!")
    response = client.post(f"/api/coding/projects/{project['id']}/deploy", json={})
    assert response.status_code == 202, response.text
    result = response.json()
    assert result["created"] is True and result["job_id"] in deploys
    agent = _agent(result["agent_id"])
    assert agent.name == "weather-bot" and agent.method == "byoc"
    assert agent.created_by == user_id and agent.created_by_name == "ada"
    assert agent.spec["byoc"]["artifact_kind"] == "code_zip"
    assert agent.spec["byoc"]["upload_id"] == "up1"
    assert agent.spec["byoc"]["entrypoint"] == "main.py"
    assert agent.spec["system_prompt"] == "d", "the project description by default"
    assert staged.calls[0]["uploaded_by"] == "ada"
    assert staged.calls[0]["filename"] == "weather-bot.zip"
    # snapshot BEFORE the export; the export object is cleaned up after staging
    order = [a for _, a, _ in fake.calls if a in ("snapshot", "export")]
    assert order == ["snapshot", "export"]
    assert not any("/exports/" in k for k in s3.objects)
    assert _project(project["id"]).agent_id == agent.id
    assert client.get(f"/api/coding/projects/{project['id']}").json()["agent_id"] == agent.id
    db = SessionLocal()
    try:
        assert db.get(Job, result["job_id"]).type == "deploy_agent"
    finally:
        db.close()


def test_a_second_deploy_republishes_the_same_agent(ada, fake, s3, staged, deploys):
    _, client = ada
    project = _create(client)
    first = client.post(f"/api/coding/projects/{project['id']}/deploy",
                        json={"agent_name": "my-coded-agent"}).json()
    busy = client.post(f"/api/coding/projects/{project['id']}/deploy", json={})
    assert busy.status_code == 409 and busy.json()["code"] == "agent.deploy_in_progress"
    db = SessionLocal()
    try:
        db.get(Agent, first["agent_id"]).status = "active"
        db.commit()
    finally:
        db.close()
    second = client.post(f"/api/coding/projects/{project['id']}/deploy",
                         json={"description": "v2 of the agent"})
    assert second.status_code == 202, second.text
    assert second.json()["created"] is False and second.json()["agent_id"] == first["agent_id"]
    agent = _agent(first["agent_id"])
    assert agent.spec["byoc"]["upload_id"] == "up2" and agent.status == "deploying"
    assert agent.spec["system_prompt"] == "v2 of the agent"
    assert len(deploys) == 2


def test_a_deleted_agent_is_not_redeployed_a_new_one_is_created(ada, fake, s3, staged, deploys):
    _, client = ada
    project = _create(client)
    first = client.post(f"/api/coding/projects/{project['id']}/deploy",
                        json={"agent_name": "first-agent"}).json()
    db = SessionLocal()
    try:
        db.get(Agent, first["agent_id"]).status = "deleted"
        db.commit()
    finally:
        db.close()
    assert client.get(f"/api/coding/projects/{project['id']}").json()["agent_id"] is None
    again = client.post(f"/api/coding/projects/{project['id']}/deploy",
                        json={"agent_name": "second-agent"}).json()
    assert again["created"] is True and again["agent_id"] != first["agent_id"]


def test_a_taken_agent_name_is_refused_before_the_export(ada, fake, s3, staged, deploys):
    _, client = ada
    first = _create(client, "one")
    second = _create(client, "two")
    client.post(f"/api/coding/projects/{first['id']}/deploy", json={"agent_name": "taken-name"})
    exports = len(fake.actions("export"))
    clash = client.post(f"/api/coding/projects/{second['id']}/deploy",
                        json={"agent_name": "taken-name"})
    assert clash.status_code == 409 and clash.json()["code"] == "agent.name_exists"
    assert len(fake.actions("export")) == exports and len(staged.calls) == 1


def test_a_root_dockerfile_deploys_as_container_source(ada, fake, s3, staged, deploys):
    _, client = ada
    project = _create(client)
    staged.dockerfile = True
    result = client.post(f"/api/coding/projects/{project['id']}/deploy", json={}).json()
    assert _agent(result["agent_id"]).spec["byoc"]["artifact_kind"] == "container_source"


def test_deploy_validation(ada, fake, s3, staged, deploys):
    _, client = ada
    project = _create(client)
    base = f"/api/coding/projects/{project['id']}/deploy"
    no_docker = client.post(base, json={"artifact_kind": "container_source"})
    assert no_docker.status_code == 422 and no_docker.json()["code"] == "coding.deploy_failed"
    assert no_docker.json()["detail"] == {"reason": "dockerfile_missing"}
    no_entry = client.post(base, json={"entrypoint": "app.py"})
    assert no_entry.status_code == 422 and no_entry.json()["code"] == "coding.deploy_failed"
    assert no_entry.json()["detail"] == {"reason": "entrypoint_missing", "entrypoint": "app.py",
                                         "candidates": ["main.py"]}
    assert client.post(base, json={"agent_name": "Bad Name"}).status_code == 422
    _set(project["id"], status="running", active_turn_id="t")
    running = client.post(base, json={})
    assert running.status_code == 409 and running.json()["code"] == "coding.turn_running"
    assert deploys == []


def test_an_oversized_export_is_refused_before_the_download(ada, fake, s3, staged, deploys):
    _, client = ada
    project = _create(client)
    s3.size = byoc_uploads.MAX_ZIP_BYTES + 1
    response = client.post(f"/api/coding/projects/{project['id']}/deploy", json={})
    assert response.status_code == 413 and response.json()["code"] == "byoc.upload_too_large"
    assert s3.downloads == [] and staged.calls == [] and deploys == []
    assert not [k for k in s3.objects if "/exports/" in k], "the export is cleaned up"


def test_deploy_needs_the_deploy_permission(app, admin, fake, s3, staged, deploys):
    _, client = _member(app, "dee", permissions={"agents.deploy": False})
    project = _create(client)
    response = client.post(f"/api/coding/projects/{project['id']}/deploy", json={})
    assert response.status_code == 403
    assert fake.calls == [] and deploys == []


def test_agent_name_from_project_names():
    assert deploy.agent_name_from("Weather Bot!") == "weather-bot"
    assert deploy.agent_name_from("天气") == "coding"
    assert deploy.agent_name_from("42 things") == "coding-42-things"
    assert deploy.agent_name_from("a") == "a-agent"
    assert len(deploy.agent_name_from("x" * 200)) == 48


# ── units ─────────────────────────────────────────────────────────────────────


def test_meter_usage_is_disjoint_and_never_negative():
    ledger = turns.ledger_usage(USAGE)
    assert turns.meter_usage(ledger) == {"inputTokens": 10, "outputTokens": 275,
                                         "cacheReadInputTokens": 58007,
                                         "cacheWriteInputTokens": 36613}
    odd = {"input": 5, "cached_input": 9, "cache_write": 0, "output": 1}
    assert turns.meter_usage(odd)["inputTokens"] == 0
    assert turns.ledger_usage(None) is None
    assert turns.ledger_usage({"inputTokens": -1, "outputTokens": True})["input"] == 0


def test_clean_path_mirrors_the_runtime_guard():
    assert files.clean_path("./src//a.py") == "src/a.py"
    assert files.clean_path(".", allow_root=True) == "."
    assert files.clean_path("a\\b.py") == "a/b.py"
    for bad in ("", "..", "x/../y", "/abs", ".git", ".git/HEAD"):
        with pytest.raises(AppError) as refused:
            files.clean_path(bad)
        assert refused.value.code == "coding.path_invalid"


def test_api_error_maps_the_container_codes():
    conflict = runtime_client.api_error(CodingRuntimeError("conflict", "changed",
                                                           detail={"sha256": "abc"}))
    assert (conflict.code, conflict.status_code, conflict.detail) == (
        "coding.file_conflict", 409, {"sha256": "abc"})
    for code, mapped, http in [
        ("turn_running", "coding.turn_running", 409), ("no_turn", "coding.no_turn", 409),
        ("path_invalid", "coding.path_invalid", 422),
        ("file_too_large", "coding.file_too_large", 413),
        ("restore_failed", "coding.restore_failed", 502),
        ("internal", "coding.runtime_error", 502),
        ("runtime_protocol", "coding.runtime_error", 502),
    ]:
        error = runtime_client.api_error(CodingRuntimeError(code, "m"))
        assert (error.code, error.status_code) == (mapped, http)
