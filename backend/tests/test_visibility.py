"""Workspace member visibility isolation (`app.services.visibility`; upstream #262 ported).

A workspace is `shared` (default — every member sees every resource) or
`isolated`: a member then sees a resource iff they created it, its creator is
unknown (NULL), or its creator is a *current* admin. Admins see everything. A
hidden resource answers the kind's ordinary not-found, everywhere: lists,
pickers, by-id routes and create-time references.

CN adaptations: every kind is ledger-backed (KBs are `knowledge_bases` rows, custom
evaluators `eval_evaluators` rows, cloud datasets S3 snapshots behind
`datasets_local.store_for`), so the checks read the ledger clause instead of
upstream's `resource_creators` side table; the step-1 member rules on API keys and chat
sessions hold in both modes (`test_chat_session_visibility.py`, `test_apikeys_api.py`).
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.config import get_settings
from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.evaluation.models import EvalDataset, EvalPipeline
from app.main import create_app
from app.models.ledger import User, Workspace
from app.services import users as users_service
from app.services import visibility
from app.services.visibility import Viewer

ADMIN = {"username": "operator", "password": "s3cret-pass"}
PEER = ("127.0.0.1", 4321)


# ─── fixtures: an auth-enabled console with two members ───────────────────────


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
    return _member(app, "ada")


@pytest.fixture
def bob(app, admin):
    return _member(app, "bob")


def _isolate(workspace_id: str = DEFAULT_WORKSPACE_ID, value: str = visibility.ISOLATED) -> None:
    db = SessionLocal()
    try:
        visibility.set_mode(db, db.get(Workspace, workspace_id), value)
    finally:
        db.close()


def _set_role(user_id: str, role: str) -> None:
    db = SessionLocal()
    try:
        db.get(User, user_id).role = role
        db.commit()
    finally:
        db.close()


def _add(row: Any) -> str:
    """Insert a ledger row into the default workspace; returns its id."""
    db = SessionLocal()
    try:
        if getattr(row, "workspace_id", None) is None:
            row.workspace_id = DEFAULT_WORKSPACE_ID
        db.add(row)
        db.commit()
        return getattr(row, "id", None) or getattr(row, "kb_id", None) or row.memory_id
    finally:
        db.close()


def _dataset(created_by: str | None, name: str = "ds", **kw: Any) -> str:
    kw.setdefault("kind", "legacy")
    kw.setdefault("items", [{"prompt": "hi"}])
    return _add(
        EvalDataset(
            name=name, created_by=created_by, created_by_name="x" if created_by else None, **kw
        )
    )


def _refused(client: TestClient, code: str, *calls: tuple, status: int = 404) -> None:
    """Every `(method, url[, json])` call answers `status` with `code`."""
    for method, url, *body in calls:
        res = client.request(method, url, json=body[0] if body else None)
        assert res.status_code == status, (method, url, res.text)
        assert res.json()["code"] == code, (method, url, res.text)


def _ids(body: dict, key: str, field: str = "id") -> set[str]:
    return {item[field] for item in body[key]}


def _ws(workspace_id: str) -> Workspace:
    db = SessionLocal()
    try:
        return db.get(Workspace, workspace_id)
    finally:
        db.close()


# ─── the helper itself ───────────────────────────────────────────────────────


def test_mode_defaults_to_shared_and_ignores_unknown_values():
    assert visibility.mode(None) == visibility.SHARED
    assert visibility.mode(Workspace(id="w", settings=None)) == visibility.SHARED
    assert visibility.mode(Workspace(id="w", settings={"resource_visibility": "x"})) == "shared"
    assert visibility.mode(Workspace(id="w", settings={"resource_visibility": "isolated"})) == (
        visibility.ISOLATED
    )


def test_set_mode_keeps_other_settings_and_rejects_unknown_values():
    """D20 keeps its policy settings in the same JSON: a mode switch must not drop them."""
    db = SessionLocal()
    try:
        row = db.get(Workspace, DEFAULT_WORKSPACE_ID)
        row.settings = {"policy_mode": "enforce"}
        db.commit()
        visibility.set_mode(db, row, visibility.ISOLATED)
        db.expire_all()
        row = db.get(Workspace, DEFAULT_WORKSPACE_ID)
        assert row.settings == {"policy_mode": "enforce", "resource_visibility": "isolated"}
        with pytest.raises(ValueError):
            visibility.set_mode(db, row, "private")
    finally:
        db.close()


def test_viewer_principal_key():
    assert Viewer.ALL.principal_key() == "all"
    assert Viewer(user_id="u1", filters=False).principal_key() == "all"
    assert Viewer(user_id="u1", filters=True).principal_key() == "u:u1"


def _users() -> dict[str, str]:
    db = SessionLocal()
    try:
        out = {}
        for name, role in (("mia", "member"), ("max", "member"), ("ann", "admin")):
            user = User(
                username=name, username_key=name, email=f"{name}@acme-corp.com",
                password_hash="x", role=role,
            )
            db.add(user)
            db.commit()
            out[name] = user.id
        return out
    finally:
        db.close()


def test_sees_and_clause_agree_on_every_creator_shape():
    users = _users()
    creators = [None, users["mia"], users["max"], users["ann"], "dangling-id"]
    for creator in creators:
        _dataset(creator, name=f"ds-{creator}")
    viewers = [
        Viewer.ALL,
        Viewer(user_id=users["mia"], filters=True),
        Viewer(user_id=users["max"], filters=True),
        Viewer(user_id=None, filters=True),
    ]
    db = SessionLocal()
    try:
        for viewer in viewers:
            by_row = {c for c in creators if visibility.sees(viewer, db, c)}
            by_sql = {
                row.created_by
                for row in db.scalars(
                    select(EvalDataset).where(visibility.clause(viewer, EvalDataset.created_by))
                )
            }
            assert by_row == by_sql, viewer
        mia = Viewer(user_id=users["mia"], filters=True)
        # own, unknown, current admin — never a peer's, never a deleted user's
        assert {c for c in creators if visibility.sees(mia, db, c)} == {
            None, users["mia"], users["ann"]
        }
        assert {c for c in creators if visibility.sees(Viewer.ALL, db, c)} == set(creators)
    finally:
        db.close()


def test_a_demoted_admin_creator_becomes_private():
    users = _users()
    db = SessionLocal()
    try:
        max_ = Viewer(user_id=users["max"], filters=True)
        assert visibility.sees(max_, db, users["ann"])
        db.get(User, users["ann"]).role = "member"
        db.commit()
        assert not visibility.sees(max_, db, users["ann"])
    finally:
        db.close()


def test_hidden_aws_ids_only_hides_known_foreign_creators():
    """The `resource_creators` side table is empty in the CN build (every kind has a
    ledger row) but keeps upstream's API: an id with no creator row stays visible."""
    from app.services import creators as creators_svc
    from app.services.creators import Creator

    users = _users()
    db = SessionLocal()
    try:
        for rid, owner in (("kb-mia", "mia"), ("kb-max", "max"), ("kb-ann", "ann")):
            creators_svc.record(
                db, DEFAULT_WORKSPACE_ID, creators_svc.KB, rid, Creator(users[owner], owner)
            )
        creators_svc.record(
            db, DEFAULT_WORKSPACE_ID, creators_svc.KB, "kb-cfg", Creator(None, "operator")
        )
        ids = ["kb-mia", "kb-max", "kb-ann", "kb-cfg", "kb-outside"]
        mia = Viewer(user_id=users["mia"], filters=True)
        assert visibility.hidden_aws_ids(mia, db, DEFAULT_WORKSPACE_ID, creators_svc.KB, ids) == {
            "kb-max"
        }
        assert visibility.hidden_aws_ids(Viewer.ALL, db, DEFAULT_WORKSPACE_ID, "kb", ids) == set()
        # another workspace's creator rows never hide anything here
        assert visibility.hidden_aws_ids(mia, db, "other", creators_svc.KB, ids) == set()
    finally:
        db.close()


def test_hidden_ledger_ids_reads_the_kinds_own_column():
    users = _users()
    mine = _dataset(users["mia"], "mine")
    theirs = _dataset(users["max"], "theirs")
    _dataset(None, "legacy")
    _dataset(users["ann"], "published")
    db = SessionLocal()
    try:
        mia = Viewer(user_id=users["mia"], filters=True)
        hidden = visibility.hidden_ledger_ids(
            mia, db, EvalDataset.id, EvalDataset.created_by,
            EvalDataset.workspace_id == DEFAULT_WORKSPACE_ID,
        )
        assert hidden == {theirs}
        assert mine not in hidden
        assert visibility.hidden_ledger_ids(
            Viewer.ALL, db, EvalDataset.id, EvalDataset.created_by
        ) == set()
    finally:
        db.close()


def test_viewer_for_principal():
    users = _users()
    db = SessionLocal()
    try:
        shared = db.get(Workspace, DEFAULT_WORKSPACE_ID)
        assert visibility.viewer_for_principal(db, shared, f"user:{users['mia']}") is Viewer.ALL
        isolated = Workspace(id="w", settings={"resource_visibility": "isolated"})
        assert visibility.viewer_for_principal(db, isolated, f"user:{users['mia']}") == Viewer(
            user_id=users["mia"], filters=True
        )
        assert visibility.viewer_for_principal(db, isolated, f"user:{users['ann']}") is Viewer.ALL
        assert visibility.viewer_for_principal(db, isolated, "config-admin") is Viewer.ALL
        assert visibility.viewer_for_principal(db, isolated, "local-operator") is Viewer.ALL
        assert visibility.viewer_for_principal(db, isolated, "user:gone").filters
    finally:
        db.close()


# ─── the setting ─────────────────────────────────────────────────────────────


def test_the_setting_defaults_to_shared_and_only_admins_change_it(app, admin, ada):
    _, member = ada
    url = f"/api/workspaces/{DEFAULT_WORKSPACE_ID}/settings/visibility"
    assert admin.get(url).json() == {"resource_visibility": "shared"}
    listed = admin.get("/api/workspaces").json()
    rows = listed["workspaces"] if isinstance(listed, dict) else listed
    assert {w["id"]: w["resource_visibility"] for w in rows}[DEFAULT_WORKSPACE_ID] == "shared"

    for method in (member.get, lambda u: member.put(u, json={"resource_visibility": "isolated"})):
        refused = method(url)
        assert refused.status_code == 403
        assert refused.json()["code"] == "auth.forbidden"

    assert admin.put(url, json={"resource_visibility": "private"}).status_code == 422
    put = admin.put(url, json={"resource_visibility": "isolated"})
    assert put.status_code == 200
    assert put.json() == {"resource_visibility": "isolated"}
    assert admin.get(url).json() == {"resource_visibility": "isolated"}
    assert admin.put(url, json={"resource_visibility": "shared"}).json() == {
        "resource_visibility": "shared"
    }
    assert admin.get("/api/workspaces/nope/settings/visibility").status_code == 404


def test_the_setting_works_on_a_workspace_that_is_not_bootstrapped(admin):
    db = SessionLocal()
    try:
        db.add(Workspace(id="fresh", name="Fresh", account_id="444455556666",
                         region="cn-north-1", bootstrap_status="registered", resources={}))
        db.commit()
    finally:
        db.close()
    url = "/api/workspaces/fresh/settings/visibility"
    assert admin.get(url).json() == {"resource_visibility": "shared"}
    put = admin.put(url, json={"resource_visibility": "isolated"})
    assert put.status_code == 200, put.text
    assert admin.get(url).json() == {"resource_visibility": "isolated"}
    # the default workspace is untouched
    assert visibility.mode(_ws(DEFAULT_WORKSPACE_ID)) == visibility.SHARED


def test_switching_takes_effect_on_the_next_request(admin, ada, bob):
    ada_id, _ = ada
    _, bob_client = bob
    ds = _dataset(ada_id)
    url = f"/api/workspaces/{DEFAULT_WORKSPACE_ID}/settings/visibility"
    assert ds in _ids(bob_client.get("/api/eval/datasets").json(), "datasets")
    admin.put(url, json={"resource_visibility": "isolated"})
    assert ds not in _ids(bob_client.get("/api/eval/datasets").json(), "datasets")
    admin.put(url, json={"resource_visibility": "shared"})
    assert ds in _ids(bob_client.get("/api/eval/datasets").json(), "datasets")


def test_the_config_admin_is_an_unfiltered_viewer(admin, ada):
    """The config admin has no `users` row (`user_id` None) and is an administrator: it
    sees everything under isolation, and what it creates has an unknown creator."""
    ada_id, ada_client = ada
    theirs = _dataset(ada_id)
    created = admin.post("/api/eval/datasets", json={
        "name": "cfg", "kind": "legacy", "items": [{"prompt": "hi"}],
    })
    assert created.status_code == 201, created.text
    assert created.json()["created_by"] is None
    _isolate()
    assert theirs in _ids(admin.get("/api/eval/datasets").json(), "datasets")
    # a NULL-creator dataset stays visible to every member
    assert created.json()["id"] in _ids(ada_client.get("/api/eval/datasets").json(), "datasets")


# ─── datasets, cloud datasets, pipelines ─────────────────────────────────────


def test_datasets_shared_by_default(ada, bob):
    ada_id, ada_client = ada
    _, bob_client = bob
    ds = _dataset(ada_id)
    assert ds in _ids(bob_client.get("/api/eval/datasets").json(), "datasets")
    # shared = every member SEES it; changing it is the creator's (r13-g, N13-obs15 —
    # before that ruling bob's rename answered 200 here)
    refused = bob_client.put(f"/api/eval/datasets/{ds}", json={"name": "renamed"})
    assert refused.status_code == 403 and refused.json()["code"] == "dataset.forbidden"
    assert ada_client.put(f"/api/eval/datasets/{ds}", json={"name": "renamed"}).status_code == 200


def test_isolated_datasets(admin, ada, bob, app):
    ada_id, ada_client = ada
    bob_id, bob_client = bob
    ann_id, _ = _member(app, "ann", role="admin")
    mine = _dataset(bob_id, "bob-ds")
    theirs = _dataset(ada_id, "ada-ds")
    unknown = _dataset(None, "legacy-ds")
    published = _dataset(ann_id, "admin-ds")
    _isolate()

    assert _ids(bob_client.get("/api/eval/datasets").json(), "datasets") == {
        mine, unknown, published
    }
    assert _ids(admin.get("/api/eval/datasets").json(), "datasets") == {
        mine, theirs, unknown, published
    }
    for call in (
        lambda: bob_client.put(f"/api/eval/datasets/{theirs}", json={"name": "x"}),
        lambda: bob_client.delete(f"/api/eval/datasets/{theirs}"),
        lambda: bob_client.post(f"/api/eval/datasets/{theirs}/sync-to-aws"),
        lambda: bob_client.post(f"/api/eval/datasets/{theirs}/publish-version"),
    ):
        refused = call()
        assert refused.status_code == 404, refused.text
        assert refused.json()["code"] == "dataset.not_found"
    assert ada_client.put(f"/api/eval/datasets/{theirs}", json={"name": "x"}).status_code == 200
    # a NULL-creator dataset is VISIBLE to bob but an administrator's to change (r13-g,
    # N13-obs15 — the CN NULL-creator rule; this call answered 200 before that ruling)
    unknown_edit = bob_client.put(f"/api/eval/datasets/{unknown}", json={"name": "y"})
    assert unknown_edit.status_code == 403 and unknown_edit.json()["code"] == "dataset.forbidden"
    assert admin.put(f"/api/eval/datasets/{unknown}", json={"name": "y"}).status_code == 200

    # a demoted admin's dataset turns private
    _set_role(ann_id, "member")
    assert published not in _ids(bob_client.get("/api/eval/datasets").json(), "datasets")


class _FakeStore:
    """The S3 snapshot store behind cloud datasets (`datasets_local.store_for`), in memory."""

    def __init__(self, ids: list[str]):
        self.metas = {
            cid: {"datasetId": cid, "datasetName": cid, "status": "ACTIVE",
                  "schemaType": "AGENT_INPUT_OUTPUT", "versions": []}
            for cid in ids
        }
        self.deleted: list[str] = []

    def list_meta(self):
        return list(self.metas.values())

    def meta(self, cloud_id):
        return self.metas.get(cloud_id)

    def require(self, cloud_id):
        from app.core.errors import NotFoundError

        if cloud_id not in self.metas:
            raise NotFoundError("dataset.not_found", f"cloud dataset {cloud_id} not found")
        return self.metas[cloud_id]

    def draft_examples(self, cloud_id):
        return []

    def delete(self, cloud_id):
        self.deleted.append(cloud_id)

    def publish_version(self, cloud_id):
        return self.metas[cloud_id]

    def delete_version(self, cloud_id, version):
        self.deleted.append(f"{cloud_id}@{version}")
        return self.metas[cloud_id]


def test_isolated_cloud_datasets_follow_their_local_rows(monkeypatch, ada, bob):
    from app.evaluation import routers as eval_routers

    ada_id, _ = ada
    bob_id, bob_client = bob
    _dataset(ada_id, "ada-ds", cloud={"dataset_id": "cloud-ada"})
    _dataset(bob_id, "bob-ds", cloud={"dataset_id": "cloud-both"})
    _dataset(ada_id, "ada-ds2", cloud={"dataset_id": "cloud-both"})
    store = _FakeStore(["cloud-ada", "cloud-both", "cloud-outside"])
    monkeypatch.setattr(eval_routers.datasets_local, "store_for", lambda ctx: store)

    shared = _ids(bob_client.get("/api/eval/datasets/cloud").json(), "datasets", "datasetId")
    assert shared == {"cloud-ada", "cloud-both", "cloud-outside"}
    _isolate()
    isolated = _ids(bob_client.get("/api/eval/datasets/cloud").json(), "datasets", "datasetId")
    assert isolated == {"cloud-both", "cloud-outside"}
    for call in (
        lambda: bob_client.get("/api/eval/datasets/cloud/cloud-ada"),
        lambda: bob_client.delete("/api/eval/datasets/cloud/cloud-ada"),
        lambda: bob_client.delete("/api/eval/datasets/cloud/cloud-ada/versions/1"),
        lambda: bob_client.post("/api/eval/datasets/cloud/cloud-ada/publish-version"),
    ):
        refused = call()
        assert refused.status_code == 404, refused.text
        assert refused.json()["code"] == "dataset.not_found"
    assert store.deleted == []
    assert bob_client.delete("/api/eval/datasets/cloud/cloud-outside").status_code == 200
    assert store.deleted == ["cloud-outside"]


def _pipeline(created_by: str | None, output_dataset: str | None = None) -> str:
    output = {"dataset_id": output_dataset} if output_dataset else {"dataset_name": "out"}
    return _add(
        EvalPipeline(
            name="p", config={"source": {}, "processing": {}, "output": output},
            created_by=created_by,
        )
    )


def test_isolated_pipelines(admin, ada, bob):
    ada_id, ada_client = ada
    bob_id, bob_client = bob
    mine = _pipeline(bob_id)
    theirs = _pipeline(ada_id)
    _isolate()

    assert _ids(bob_client.get("/api/eval/pipelines").json(), "pipelines") == {mine}
    assert _ids(admin.get("/api/eval/pipelines").json(), "pipelines") == {mine, theirs}
    body = {"name": "p", "output": {"dataset_name": "out"}}
    for call in (
        lambda: bob_client.get(f"/api/eval/pipelines/{theirs}"),
        lambda: bob_client.put(f"/api/eval/pipelines/{theirs}", json=body),
        lambda: bob_client.delete(f"/api/eval/pipelines/{theirs}"),
        lambda: bob_client.post(f"/api/eval/pipelines/{theirs}/run"),
    ):
        refused = call()
        assert refused.status_code == 404, refused.text
        assert refused.json()["code"] == "pipeline.not_found"
    assert ada_client.get(f"/api/eval/pipelines/{theirs}").status_code == 200


def test_a_pipeline_cannot_target_a_hidden_dataset(ada, bob):
    ada_id, _ = ada
    _, bob_client = bob
    theirs = _dataset(ada_id, "ada-ds", kind="predefined")
    _isolate()
    created = bob_client.post(
        "/api/eval/pipelines", json={"name": "p", "output": {"dataset_id": theirs}}
    )
    assert created.status_code == 404
    assert created.json()["code"] == "dataset.not_found"
    appended = bob_client.post(
        "/api/eval/datasets/from-sessions",
        json={"session_ids": ["session-0001"], "dataset_id": theirs},
    )
    assert appended.status_code == 404
    assert appended.json()["code"] == "dataset.not_found"


def test_internal_dataset_lookups_stay_unfiltered(ada, bob):
    """Internal references keep working: the pipeline background thread (and every
    other non-router caller) resolves a dataset with the default `Viewer.ALL`."""
    from app.evaluation.routers import _dataset_in
    from app.routers.workspaces import WorkspaceScope

    ada_id, _ = ada
    bob_id, _ = bob
    theirs = _dataset(ada_id)
    _isolate()
    db = SessionLocal()
    try:
        ws = WorkspaceScope(
            id=DEFAULT_WORKSPACE_ID, row=db.get(Workspace, DEFAULT_WORKSPACE_ID), context=None
        )
        assert _dataset_in(db, ws, theirs).id == theirs
        with pytest.raises(Exception) as hidden:
            _dataset_in(db, ws, theirs, viewer=Viewer(user_id=bob_id, filters=True))
        assert getattr(hidden.value, "code", None) == "dataset.not_found"
    finally:
        db.close()


# ─── eval runs ───────────────────────────────────────────────────────────────


def _run(created_by: str | None, status: str = "failed", **kw: Any) -> str:
    from app.evaluation.models import EvalRun

    return _add(
        EvalRun(agent_id="a1", agent_name="agent", status=status, created_by=created_by, **kw)
    )


ARN = "arn:aws-cn:bedrock-agentcore:cn-northwest-1:111122223333:runtime/{rid}"


def _active_agent(name: str = "vis-agent", created_by: str | None = None) -> str:
    from app.models.ledger import Agent

    rid = f"{name.replace('-', '_')}-abcdefghij"
    return _add(
        Agent(
            name=name, method="zip_runtime", status="active", created_by=created_by,
            created_by_name="someone" if created_by else None,
            arn=ARN.format(rid=rid), resource_id=rid,
            spec={"name": name, "method": "zip_runtime", "system_prompt": "p",
                  "protocol": "http"},
        )
    )


def test_isolated_runs_list_page_and_total(admin, ada, bob):
    ada_id, _ = ada
    bob_id, bob_client = bob
    mine = {_run(bob_id) for _ in range(3)}
    theirs = {_run(ada_id) for _ in range(4)}
    unknown = _run(None)
    _isolate()

    page = bob_client.get("/api/eval/runs?limit=2").json()
    assert page["total"] == 4  # 3 own + 1 creator-unknown, counted before paging
    assert len(page["runs"]) == 2
    everything = bob_client.get("/api/eval/runs?limit=200").json()
    assert _ids(everything, "runs") == mine | {unknown}
    assert admin.get("/api/eval/runs?limit=200").json()["total"] == 8
    assert _ids(admin.get("/api/eval/runs?limit=200").json(), "runs") == mine | theirs | {unknown}


def test_isolated_run_by_id_routes_are_404(ada, bob):
    ada_id, ada_client = ada
    _, bob_client = bob
    theirs = _run(ada_id)
    _isolate()
    rec = {"kinds": ["system_prompt"]}
    for call in (
        lambda: bob_client.get(f"/api/eval/runs/{theirs}"),
        lambda: bob_client.get(f"/api/eval/runs/{theirs}/results"),
        lambda: bob_client.post(f"/api/eval/runs/{theirs}/stop"),
        lambda: bob_client.post(f"/api/eval/runs/{theirs}/recheck"),
        lambda: bob_client.get(f"/api/eval/runs/{theirs}/recommendation-inputs"),
        lambda: bob_client.get(f"/api/eval/runs/{theirs}/recommendations"),
        lambda: bob_client.post(f"/api/eval/runs/{theirs}/recommendations", json=rec),
        lambda: bob_client.post(f"/api/eval/runs/{theirs}/recommendations/r1/accept"),
        lambda: bob_client.delete(f"/api/eval/runs/{theirs}"),
    ):
        refused = call()
        assert refused.status_code == 404, refused.text
        assert refused.json()["code"] == "run.not_found"
    assert ada_client.get(f"/api/eval/runs/{theirs}").status_code == 200
    assert ada_client.delete(f"/api/eval/runs/{theirs}").status_code == 200


def test_isolated_queue_names_only_visible_runs(monkeypatch, ada, bob):
    from app.evaluation import routers as eval_routers

    ada_id, _ = ada
    bob_id, bob_client = bob
    mine = _run(bob_id, status="evaluating")
    theirs = _run(ada_id, status="queued")
    monkeypatch.setattr(
        eval_routers.run_queue, "state",
        lambda: {"running": [mine], "queued": [theirs], "max_concurrent": 2},
    )
    assert bob_client.get("/api/eval/queue").json()["queued"] == [theirs]
    _isolate()
    state = bob_client.get("/api/eval/queue").json()
    assert state["running"] == [mine]
    assert state["queued"] == []


def test_a_run_cannot_start_on_a_hidden_dataset(monkeypatch, ada, bob):
    from app.evaluation import routers as eval_routers

    ada_id, _ = ada
    _, bob_client = bob
    agent_id = _active_agent()
    theirs = _dataset(ada_id, cloud={"dataset_id": "cloud-ada"})
    monkeypatch.setattr(
        eval_routers.datasets_local, "store_for", lambda ctx: _FakeStore(["cloud-ada"])
    )
    _isolate()
    for body in (
        {"agent_id": agent_id, "dataset_id": theirs, "evaluators": ["Builtin.Helpfulness"]},
        {"agent_id": agent_id, "cloud_dataset_id": "cloud-ada",
         "evaluators": ["Builtin.Helpfulness"]},
    ):
        refused = bob_client.post("/api/eval/runs", json=body)
        assert refused.status_code == 404, refused.text
        assert refused.json()["code"] == "dataset.not_found"


# ─── online eval configs ─────────────────────────────────────────────────────


def _canary(created_by: str | None, **kw: Any) -> str:
    from app.optimization.models import RuntimeCanary

    kw.setdefault("champion_agent_id", "a1")
    kw.setdefault("challenger_agent_id", "a1")
    return _add(RuntimeCanary(
        name="c", champion_agent_name="agent", challenger_agent_name="agent",
        created_by=created_by, **kw,
    ))


def _online_config(config_id: str, name: str, created_by: str | None, **kw: Any) -> str:
    from app.evaluation.models import OnlineEvalConfig

    kw.setdefault("agent_id", "a1")
    _add(OnlineEvalConfig(
        agent_name="agent", config_id=config_id, config_arn=f"arn:{config_id}",
        name=name, service_name="s", log_group="g", created_by=created_by, **kw,
    ))
    return config_id


@pytest.fixture
def online_world(ada, bob):
    """Five ledger configs: ada's agent config, ada's canary arm (records no creator — it
    follows the canary's), bob's canary arm, a legacy arm of the retired configuration
    experiment (N13-obs2: no owner row resolves any more, so it follows nobody and stays
    visible like an unknown creator's), and one with an unknown creator."""
    ada_id, _ = ada
    bob_id, _ = bob
    ada_can = _canary(ada_id)
    can_id = _canary(bob_id)
    return {
        "row": _online_config("oe_ada-1111111111", "oe_ada", ada_id),
        "exp": _online_config(f"can_{ada_can[:8]}_oe1-2222222222", f"can_{ada_can[:8]}_oe1", None),
        "legacy": _online_config("exp_deadbeef_oe1-5555555555", "exp_deadbeef_oe1", None),
        "can": _online_config(f"can_{can_id[:8]}_oec-3333333333", f"can_{can_id[:8]}_oec", None),
        "ext": _online_config("outside-4444444444", "oe_outside", None),
    }


def test_online_configs_shared_by_default(online_world, bob):
    _, bob_client = bob
    listed = bob_client.get("/api/eval/online").json()["configs"]
    assert {c["config_id"] for c in listed} == set(online_world.values())


def test_isolated_online_configs(online_world, admin, ada, bob):
    _, ada_client = ada
    _, bob_client = bob
    _isolate()
    listed = bob_client.get("/api/eval/online").json()
    assert {c["config_id"] for c in listed["configs"]} == {
        online_world["can"], online_world["legacy"], online_world["ext"]}
    assert listed["total"] == 3
    assert {c["config_id"] for c in admin.get("/api/eval/online").json()["configs"]} == set(
        online_world.values()
    )
    assert {c["config_id"] for c in ada_client.get("/api/eval/online").json()["configs"]} == {
        online_world["row"], online_world["exp"], online_world["legacy"], online_world["ext"]
    }
    for hidden in (online_world["row"], online_world["exp"]):
        base = f"/api/eval/online/{hidden}"
        _refused(
            bob_client, "online_eval.not_found",
            ("GET", base), ("PATCH", base, {"description": "x"}), ("GET", f"{base}/reports"),
            ("POST", f"{base}/reports", {}), ("GET", f"{base}/reports/batch-1"),
            ("POST", f"{base}/pause"), ("POST", f"{base}/resume"), ("DELETE", base),
            ("GET", f"{base}/results"),
        )
    assert bob_client.get(f"/api/eval/online/{online_world['can']}").status_code == 200
    assert bob_client.get(f"/api/eval/online/{online_world['ext']}/results").status_code == 200
    assert ada_client.get(f"/api/eval/online/{online_world['exp']}").status_code == 200


def test_online_reports_drop_hidden_console_runs(online_world, ada, bob):
    ada_id, ada_client = ada
    bob_id, bob_client = bob
    cfg = online_world["ext"]
    _run(ada_id, dataset_name=f"online:{cfg}", batch_eval_id="batch-ada", mode="insights")
    _run(bob_id, dataset_name=f"online:{cfg}", batch_eval_id="batch-bob", mode="insights")
    assert len(bob_client.get(f"/api/eval/online/{cfg}/reports").json()["reports"]) == 2
    _isolate()
    rows = bob_client.get(f"/api/eval/online/{cfg}/reports").json()["reports"]
    assert [r["batch_id"] for r in rows] == ["batch-bob"]
    refused = bob_client.get(f"/api/eval/online/{cfg}/reports/batch-ada")
    assert refused.status_code == 404
    assert refused.json()["code"] == "online_eval.report_not_found"
    assert len(ada_client.get(f"/api/eval/online/{cfg}/reports").json()["reports"]) == 1


# ─── custom evaluators ───────────────────────────────────────────────────────


def _evaluator(name: str, created_by: str | None) -> str:
    from app.evaluation.models import EvalEvaluator

    return _add(EvalEvaluator(
        name=name, level="SESSION", definition="judge", instructions="Rate the {context}",
        rating_scale=[{"value": 1.0, "label": "pass", "definition": "ok"},
                      {"value": 0.0, "label": "fail", "definition": "bad"}],
        model_id="judge", created_by=created_by,
        created_by_name="someone" if created_by else None,
    ))


@pytest.fixture
def evaluator_world(ada, bob):
    ada_id, _ = ada
    bob_id, _ = bob
    return {
        "ada": _evaluator("ev_ada", ada_id),
        "bob": _evaluator("ev_bob", bob_id),
        "out": _evaluator("ev_out", None),
    }


def test_isolated_evaluators(evaluator_world, admin, ada, bob):
    _, ada_client = ada
    _, bob_client = bob

    def custom(client):
        return {
            e["id"] for e in client.get("/api/eval/evaluators").json()["evaluators"]
            if e["source"] != "builtin"
        }

    everything = set(evaluator_world.values())
    assert custom(bob_client) == everything
    _isolate()
    assert custom(bob_client) == {evaluator_world["bob"], evaluator_world["out"]}
    assert custom(admin) == everything
    builtins = [
        e for e in bob_client.get("/api/eval/evaluators").json()["evaluators"]
        if e["source"] == "builtin"
    ]
    assert builtins  # built-ins are never filtered
    hidden = evaluator_world["ada"]
    body = {"instructions": "Rate the {context} please", "level": "SESSION"}
    _refused(
        bob_client, "evaluator.not_found",
        ("GET", f"/api/eval/evaluators/{hidden}"),
        ("PUT", f"/api/eval/evaluators/{hidden}", body),
        ("DELETE", f"/api/eval/evaluators/{hidden}"),
    )
    assert ada_client.get(f"/api/eval/evaluators/{hidden}").status_code == 200
    assert bob_client.get(f"/api/eval/evaluators/{evaluator_world['out']}").status_code == 200
    db = SessionLocal()
    try:
        from app.evaluation.models import EvalEvaluator

        assert db.get(EvalEvaluator, hidden) is not None  # the refused DELETE touched nothing
    finally:
        db.close()


def test_hidden_evaluators_cannot_be_referenced(evaluator_world, monkeypatch, bob):
    from unittest.mock import MagicMock

    from app.optimization import canary_routers

    _, bob_client = bob
    agent_id = _active_agent()
    ds = _dataset(None)
    monkeypatch.setattr(canary_routers, "_eligible_agent",
                        lambda db, ws, aid, viewer=None: MagicMock(method="zip_runtime", id=aid))
    monkeypatch.setattr(canary_routers, "_resolve_edited_spec", lambda agent, cand: {})
    _isolate()
    hidden = evaluator_world["ada"]

    run = bob_client.post("/api/eval/runs", json={
        "agent_id": agent_id, "dataset_id": ds, "evaluators": [hidden],
    })
    assert run.status_code == 422, run.text
    assert run.json()["code"] == "run.evaluator_not_found"

    created = bob_client.post("/api/eval/online", json={
        "agent_id": agent_id, "evaluators": [hidden],
    })
    assert created.status_code == 400, created.text
    assert created.json()["code"] == "online_eval.evaluator_unsupported"

    canary = bob_client.post("/api/runtime-canaries", json={
        "agent_id": agent_id, "candidate": {"system_prompt": "new"},
        "online_evaluators": [hidden],
    })
    assert canary.status_code == 400, canary.text
    assert canary.json()["code"] == "canary.evaluator_unsupported"

    score = bob_client.post("/api/observability/sessions/session-0001-abcdef/evaluate", json={
        "evaluator_ids": [hidden],
    })
    assert score.status_code == 404, score.text
    assert score.json()["code"] == "evaluator.not_found"


# ─── runtime canaries ────────────────────────────────────────────────────────


def test_isolated_canaries_filter_before_the_limit(admin, ada, bob):
    ada_id, _ = ada
    bob_id, bob_client = bob
    mine_can = _canary(bob_id)
    # 25 newer foreign rows would push the member's own out of a post-hoc .limit(20)
    for _ in range(25):
        _canary(ada_id)
    _isolate()
    assert _ids(bob_client.get("/api/runtime-canaries").json(), "canaries") == {mine_can}
    assert len(admin.get("/api/runtime-canaries").json()["canaries"]) == 20


def test_isolated_canary_by_id_routes_are_404(ada, bob):
    ada_id, ada_client = ada
    _, bob_client = bob
    can = _canary(ada_id)
    _isolate()
    _refused(
        bob_client, "canary.not_found",
        ("GET", f"/api/runtime-canaries/{can}"),
        ("POST", f"/api/runtime-canaries/{can}/action", {"action": "cleanup"}),
    )
    assert ada_client.get(f"/api/runtime-canaries/{can}").status_code == 200


def test_canary_references_respect_isolation(monkeypatch, ada, bob):
    from app.optimization import canary_routers

    ada_id, _ = ada
    bob_id, bob_client = bob
    agent_id = _active_agent()
    hidden_ds = _dataset(ada_id)
    can = _canary(bob_id, champion_agent_id=agent_id, challenger_agent_id=agent_id,
                  stage="setup", artifacts={"setup": {"ab_test_id": "t"}})
    monkeypatch.setattr(canary_routers.canary_service, "stage_not_ready_reason", lambda *a: None)
    _isolate()

    refused = bob_client.post(f"/api/runtime-canaries/{can}/action",
                              json={"action": "traffic", "dataset_id": hidden_ds})
    assert refused.status_code == 404, refused.text
    assert refused.json()["code"] == "dataset.not_found"


# ─── knowledge bases ─────────────────────────────────────────────────────────


class _FakeKnowledgeBackend:
    """The `KnowledgeBackend` Protocol over the ledger rows: lists what the ledger holds
    and records every by-id call, so a refusal can be shown to precede the backend."""

    def __init__(self):
        self.calls: list[str] = []

    def list_kbs(self, ws):
        from app.models.ledger import KnowledgeBase

        db = SessionLocal()
        try:
            return [
                {"kb_id": r.kb_id, "name": r.name, "status": r.status,
                 "attached_agents": [], "created_by": r.created_by,
                 "created_by_name": r.created_by_name}
                for r in db.query(KnowledgeBase).filter(KnowledgeBase.workspace_id == ws.id)
            ]
        finally:
            db.close()

    def __getattr__(self, name):
        def fn(*args, **kwargs):
            self.calls.append(name)
            return {"kb_id": args[1] if len(args) > 1 else None, "attached_agents": []}
        return fn


def _kb(kb_id: str, created_by: str | None) -> str:
    from app.models.ledger import KnowledgeBase

    return _add(KnowledgeBase(
        kb_id=kb_id, name=kb_id, status="ACTIVE", created_by=created_by,
        created_by_name="someone" if created_by else None,
    ))


@pytest.fixture
def kb_world(ada, bob):
    from app.services import knowledge_backend

    ada_id, _ = ada
    bob_id, _ = bob
    _kb("KBADA", ada_id)
    _kb("KBBOB", bob_id)
    _kb("KBOUT", None)
    backend = _FakeKnowledgeBackend()
    knowledge_backend.set_backend(backend)
    yield backend
    knowledge_backend.set_backend(None)


def test_isolated_knowledge_bases(kb_world, admin, ada, bob):
    _, ada_client = ada
    _, bob_client = bob

    def kbs(client, query=""):
        return {i["kb_id"] for i in client.get(f"/api/knowledge-bases{query}").json()["items"]}

    assert kbs(bob_client) == {"KBADA", "KBBOB", "KBOUT"}
    _isolate()
    assert kbs(bob_client) == {"KBBOB", "KBOUT"}
    assert kbs(bob_client, "?status=ACTIVE") == {"KBBOB", "KBOUT"}  # the agent picker
    assert kbs(admin) == {"KBADA", "KBBOB", "KBOUT"}
    base = "/api/knowledge-bases/KBADA"
    _refused(
        bob_client, "kb.not_found",
        ("GET", base), ("PATCH", base, {"description": "x"}), ("DELETE", base),
        ("POST", f"{base}/data-sources", {"mode": "upload"}),
        ("DELETE", f"{base}/data-sources/DS1"), ("POST", f"{base}/data-sources/DS1/sync"),
        ("POST", f"{base}/data-sources/DS1/sync", {"full": True}),  # N17-obs272: same gate
        ("GET", f"{base}/data-sources/DS1/ingestion-jobs"),
        ("GET", f"{base}/data-sources/DS1/documents"),
        ("POST", f"{base}/query", {"text": "hello"}),
    )
    upload = bob_client.post(f"{base}/files", files={"files": ("a.txt", b"hi")})
    assert upload.status_code == 404
    assert upload.json()["code"] == "kb.not_found"
    assert kb_world.calls == []  # refused before any backend call
    assert ada_client.get(base).status_code == 200
    assert bob_client.get("/api/knowledge-bases/KBOUT").status_code == 200


# ─── managed memories ────────────────────────────────────────────────────────


MEM_ID = "bootstrap_mem-0123456789"


class _FakeMemoryBackend:
    def __init__(self, ids: list[str]):
        self.ids = ids

    def list_memory_resources(self, ws):
        return {"items": [{"id": mid, "status": "ACTIVE"} for mid in self.ids]}

    def get_memory_resource(self, ws, memory_id):
        return {"id": memory_id, "status": "ACTIVE"}

    def update_memory_resource(self, ws, memory_id, **kw):
        return {"id": memory_id, "status": "ACTIVE", **kw}

    def delete_memory_resource(self, ws, memory_id):
        return {"id": memory_id, "deleted": True}


def test_isolated_managed_memories(monkeypatch, admin, ada, bob):
    import app.services.memory_ownership as mo
    from app.models.ledger import ManagedMemory
    from app.services import memory_backend
    from tests.conftest import set_default_resources

    ada_id, ada_client = ada
    bob_id, bob_client = bob
    set_default_resources({"memory_id": MEM_ID})
    fake = _FakeMemoryBackend([MEM_ID, "mem_ada-AAAA", "mem_bob-BBBB", "detected-CCCC"])
    monkeypatch.setattr(memory_backend, "get_backend", lambda: fake)
    for mid, owner in (("mem_ada-AAAA", ada_id), ("mem_bob-BBBB", bob_id)):
        _add(ManagedMemory(memory_id=mid, origin="created", created_by=owner))

    def listed(client):
        return {m["id"]: m["managed"] for m in client.get("/api/memory/resources").json()["items"]}

    everything = {MEM_ID: True, "mem_ada-AAAA": True, "mem_bob-BBBB": True,
                  "detected-CCCC": False}
    assert listed(bob_client) == everything
    _isolate()
    # the hidden managed memory is gone entirely, not degraded to "detected"
    assert listed(bob_client) == {MEM_ID: True, "mem_bob-BBBB": True, "detected-CCCC": False}
    assert listed(admin) == everything
    base = "/api/memory/resources/mem_ada-AAAA"
    _refused(
        bob_client, "memory.not_managed",
        ("GET", base), ("PUT", base, {"description": "x"}), ("DELETE", base),
    )
    assert ada_client.get(base).status_code == 200
    assert bob_client.get(f"/api/memory/resources/{MEM_ID}").status_code == 200
    # pin/deploy/chat paths keep the unfiltered ownership check
    db = SessionLocal()
    try:
        from app.services.workspace import workspace_context

        ctx = workspace_context(_ws(DEFAULT_WORKSPACE_ID))
        assert mo.is_managed(db, ctx, "mem_ada-AAAA")
        assert mo.hidden_ids(db, ctx, Viewer(user_id=bob_id, filters=True)) == {"mem_ada-AAAA"}
        assert mo.hidden_ids(db, ctx, Viewer.ALL) == set()
    finally:
        db.close()


# ─── Skill Lab jobs + task sets ──────────────────────────────────────────────


def _taskset(created_by: str | None, **kw: Any) -> str:
    from app.skill_lab.models import SkillLabTaskset

    return _add(SkillLabTaskset(name="ts", mode="single", created_by=created_by, **kw))


def _job(created_by: str | None, taskset_id: str = "ts-x") -> str:
    from app.skill_lab.models import SkillLabJob

    return _add(SkillLabJob(type="eval", status="failed", taskset_id=taskset_id,
                            created_by=created_by))


def test_isolated_skill_lab(monkeypatch, admin, ada, bob):
    from app.skill_lab import jobs as jobs_svc

    ada_id, ada_client = ada
    bob_id, bob_client = bob
    my_ts, their_ts, sample_ts = _taskset(bob_id), _taskset(ada_id), _taskset(None, sample=True)
    my_job, their_job = _job(bob_id), _job(ada_id)
    reached: list[str] = []
    for name in ("cancel_job", "delete_job", "import_taskgen_taskset",
                 "apply_taskgen_expansion", "resume_job", "publish_job"):
        monkeypatch.setattr(jobs_svc, name, lambda *a, _n=name, **k: reached.append(_n) or {})
    # submission reaches the task-set lookup (where isolation answers) without a worker
    monkeypatch.setattr(jobs_svc.runner, "require_worker", lambda _ws: None)
    monkeypatch.setattr(jobs_svc.runner, "require_model_credential", lambda _ws: None)

    def tasksets(client):
        return {t["id"] for t in client.get("/api/skill-lab/tasksets").json()}

    def jobs(client):
        return {j["id"] for j in client.get("/api/skill-lab/jobs").json()}

    assert {my_ts, their_ts, sample_ts} <= tasksets(bob_client)
    _isolate()
    assert their_ts not in tasksets(bob_client)
    assert {my_ts, sample_ts} <= tasksets(bob_client)
    assert {my_ts, their_ts, sample_ts} <= tasksets(admin)
    assert jobs(bob_client) == {my_job}
    assert jobs(admin) == {my_job, their_job}

    ts = f"/api/skill-lab/tasksets/{their_ts}"
    _refused(
        bob_client, "skill_lab.taskset_not_found",
        ("GET", ts), ("PUT", ts, {"name": "x", "tasks_by_split": {}}), ("DELETE", ts),
        ("POST", "/api/skill-lab/jobs",
         {"type": "eval", "skill_source": {}, "taskset_id": their_ts}),
        ("POST", "/api/skill-lab/jobs",
         {"type": "train", "skill_source": {}, "taskset_id": their_ts}),
        ("POST", "/api/skill-lab/jobs",
         {"type": "taskgen", "skill_source": {}, "taskset_id": their_ts,
          "target_split": "tasks"}),
    )
    job = f"/api/skill-lab/jobs/{their_job}"
    _refused(
        bob_client, "skill_lab.job_not_found",
        ("GET", job), ("POST", f"{job}/cancel"), ("DELETE", job), ("GET", f"{job}/log"),
        ("GET", f"{job}/results"), ("POST", f"{job}/import-taskset", {"name": "x"}),
        ("POST", f"{job}/apply-expansion"), ("POST", f"{job}/resume"),
        ("POST", f"{job}/publish", {}), ("GET", f"{job}/train-summary"),
        ("GET", f"{job}/diff"), ("GET", f"{job}/artifacts"),
        ("GET", f"{job}/artifacts/raw?path=x"),
    )
    assert reached == []
    assert ada_client.get(job).status_code == 200
    assert bob_client.get(f"/api/skill-lab/tasksets/{sample_ts}").status_code != 404
    # the owner's own expansion still reaches the service (whose target load is unfiltered)
    assert ada_client.post(f"{job}/apply-expansion").status_code == 200
    assert reached == ["apply_taskgen_expansion"]


# ─── overview aggregates ─────────────────────────────────────────────────────


def test_overview_counts_only_visible_runs(monkeypatch, admin, ada, bob):
    import app.routers.overview as overview_mod

    ada_id, _ = ada
    bob_id, bob_client = bob
    monkeypatch.setattr(overview_mod, "_traces_active", lambda _ws: False)
    overview_mod._cache.clear()
    _run(bob_id, status="completed", scores=[{"score": 1.0}])
    _run(ada_id, status="completed", scores=[{"score": 0.0}])
    _run(ada_id, status="completed", scores=[{"score": 0.0}])
    shared = bob_client.get("/api/overview").json()
    assert (shared["eval_runs"], shared["eval_pass_rate"]) == (3, 0.333)
    _isolate()
    isolated = bob_client.get("/api/overview").json()
    assert (isolated["eval_runs"], isolated["eval_pass_rate"]) == (1, 1.0)
    assert admin.get("/api/overview").json()["eval_runs"] == 3


def test_online_quality_is_per_viewer_and_cached_per_viewer(admin, ada, bob):
    import app.routers.overview as overview_mod

    ada_id, ada_client = ada
    bob_id, bob_client = bob
    overview_mod._cache.clear()
    _online_config("oe_ada-1", "oe_ada", ada_id, agent_id="agent-ada")
    _online_config("oe_bob-1", "oe_bob", bob_id, agent_id="agent-bob")
    _isolate()
    bob_tile = bob_client.get("/api/overview/online-quality").json()
    assert bob_tile["configs"] == 1 and bob_tile["cached"] is False
    admin_tile = admin.get("/api/overview/online-quality").json()
    assert admin_tile["configs"] == 2 and admin_tile["cached"] is False  # no leak from bob
    assert ada_client.get("/api/overview/online-quality").json()["configs"] == 1
    assert bob_client.get("/api/overview/online-quality").json()["cached"] is True
    overview_mod._cache.clear()


# ─── architect catalog ───────────────────────────────────────────────────────


def test_the_architect_catalog_lists_the_owners_visible_kbs(kb_world, ada, bob, monkeypatch):
    from app.assistant import service as assistant
    from app.services import registry_console
    from tests.conftest import ws_ctx

    # The catalog's MCP section lists the workspace's live Gateways through the
    # `bedrock-agentcore-control` funnel client; this test is about KB visibility and
    # must not reach AWS (NoCredentialsError on a credential-less runner).
    monkeypatch.setattr(registry_console, "_list_live_mcp_gateways", lambda ws: [])

    ada_id, _ = ada
    bob_id, _ = bob

    def kbs(principal: str) -> set[str]:
        db = SessionLocal()
        try:
            viewer = assistant.owner_viewer(db, DEFAULT_WORKSPACE_ID, principal)
            catalog = assistant.restrict_catalog(
                db, DEFAULT_WORKSPACE_ID, viewer, assistant.fetch_catalog(ws_ctx())
            )
        finally:
            db.close()
        return {k["kb_id"] for k in catalog["knowledge_bases"]}

    everything = {"KBADA", "KBBOB", "KBOUT"}
    assert kbs(f"user:{bob_id}") == everything
    _isolate()
    assert kbs(f"user:{bob_id}") == {"KBBOB", "KBOUT"}
    assert kbs(f"user:{ada_id}") == {"KBADA", "KBOUT"}
    assert kbs("config-admin") == everything
    # the deploy-job re-validation reads the unrestricted catalog: everything
    assert {k["kb_id"] for k in assistant.fetch_catalog(ws_ctx())["knowledge_bases"]} == everything


def test_a_catalog_snapshot_taken_while_shared_is_restricted_on_read(ada, bob):
    """A conversation's catalog is a snapshot; one taken before the workspace was
    isolated must not keep showing the owner (or prompting with) a hidden KB."""
    from app.assistant import service as assistant

    ada_id, _ = ada
    bob_id, _ = bob
    _kb("KBADA", ada_id)
    snapshot = {"knowledge_bases": [{"kb_id": kb, "name": kb} for kb in ("KBADA", "KBOUT")],
                "tools": [{"name": "t"}]}
    conversation = SimpleNamespace(
        catalog=snapshot, workspace_id=DEFAULT_WORKSPACE_ID, owner_principal=f"user:{bob_id}",
    )
    db = SessionLocal()
    try:
        assert assistant.owner_catalog(db, conversation) == snapshot
        _isolate()
        restricted = assistant.owner_catalog(db, conversation)
        assert [k["kb_id"] for k in restricted["knowledge_bases"]] == ["KBOUT"]
        assert restricted["tools"] == snapshot["tools"]
        assert conversation.catalog == snapshot  # the stored snapshot is not rewritten
        conversation.owner_principal = f"user:{ada_id}"
        assert assistant.owner_catalog(db, conversation) == snapshot
    finally:
        db.close()


# ─── shared (default) regression across the ledger-backed kinds ──────────────


def test_shared_workspaces_show_every_members_resources(ada, bob):
    ada_id, _ = ada
    _, bob_client = bob
    rows = {
        "datasets": _dataset(ada_id),
        "pipelines": _pipeline(ada_id),
        "runs": _run(ada_id),
        "canaries": _canary(ada_id),
    }
    job, taskset = _job(ada_id), _taskset(ada_id)
    assert rows["datasets"] in _ids(bob_client.get("/api/eval/datasets").json(), "datasets")
    assert rows["pipelines"] in _ids(bob_client.get("/api/eval/pipelines").json(), "pipelines")
    assert rows["runs"] in _ids(bob_client.get("/api/eval/runs").json(), "runs")
    assert rows["canaries"] in _ids(bob_client.get("/api/runtime-canaries").json(), "canaries")
    assert job in {j["id"] for j in bob_client.get("/api/skill-lab/jobs").json()}
    assert taskset in {t["id"] for t in bob_client.get("/api/skill-lab/tasksets").json()}
    for url in (f"/api/eval/pipelines/{rows['pipelines']}", f"/api/eval/runs/{rows['runs']}",
                f"/api/runtime-canaries/{rows['canaries']}", f"/api/skill-lab/jobs/{job}"):
        assert bob_client.get(url).status_code == 200, url


def test_isolated_in_use_counts_follow_the_masked_list(monkeypatch, admin, ada, bob):
    """N13-obs22a (rev-r11 nit on N13-obs22): an isolated member deleting a credential pinned
    by another member's agent gets the 409 ``identity.in_use`` with the hidden agent absent
    AND ``total`` / ``truncated`` recomputed from what they may see — an empty list and
    ``total: 0`` still refuses the delete; the admin view keeps the real count."""
    import app.routers.identity as identity_router
    from app.models.ledger import Agent
    from tests.test_identity_connections import make_control

    ada_id, _ada_client = ada
    bob_id, bob_client = bob
    control = make_control(api=[{"name": "shared-key"}])
    monkeypatch.setattr(identity_router, "control_client", lambda _ctx: control)

    def pinned(name: str, created_by: str) -> str:
        return _add(Agent(
            name=name, method="zip_runtime", status="active", created_by=created_by,
            created_by_name="someone",
            spec={"name": name, "model_id": "global.zai.glm-5.3", "model_credential": "shared-key"},
        ))

    theirs = pinned("ada-agent", ada_id)
    _isolate()

    res = bob_client.delete("/api/identity/connections/api_key/shared-key")
    assert res.status_code == 409, res.text
    assert res.json()["code"] == "identity.in_use"
    assert res.json()["detail"] == {"referenced_by": [], "total": 0, "truncated": False}
    control.delete_api_key_credential_provider.assert_not_called()

    mine = pinned("bob-agent", bob_id)
    res = bob_client.delete("/api/identity/connections/api_key/shared-key")
    assert res.status_code == 409
    detail = res.json()["detail"]
    assert [r["id"] for r in detail["referenced_by"]] == [mine]
    assert detail["total"] == 1 and detail["truncated"] is False

    res = admin.delete("/api/identity/connections/api_key/shared-key")
    assert res.status_code == 409
    detail = res.json()["detail"]
    assert {r["id"] for r in detail["referenced_by"]} == {theirs, mine}
    assert detail["total"] == 2 and detail["truncated"] is False


def test_isolated_run_masks_the_names_of_rows_the_viewer_cannot_see(app, admin, ada, bob):
    """N15-obs8: an administrator's run is visible to every member, but it names a dataset
    / an agent that may be hidden from the viewer — the id stays, the name is nulled and
    `hidden.<ref>` is true so the console renders its placeholder. The creator, every admin
    and a viewer who sees the rows keep the names; a shared workspace masks nothing."""
    from app.evaluation.models import EvalRun

    ada_id, ada_client = ada
    _bob_id, bob_client = bob
    carol_id, carol = _member(app, "carol", role="admin")
    ada_ds = _dataset(ada_id, name="ada-secret")
    ada_agent = _active_agent("ada-agent", created_by=ada_id)
    run = _add(EvalRun(
        agent_id=ada_agent, agent_name="ada-agent", dataset_id=ada_ds, dataset_name="ada-secret",
        status="completed", created_by=carol_id, created_by_name="carol",
    ))
    plain = {"agent": False, "dataset": False}

    def seen_by(client) -> dict:
        listed = {r["id"]: r for r in client.get("/api/eval/runs").json()["runs"]}
        by_id = client.get(f"/api/eval/runs/{run}").json()
        assert listed[run] == by_id  # list and by-id agree
        return by_id

    # shared: nothing is masked for anyone
    for client in (admin, carol, ada_client, bob_client):
        row = seen_by(client)
        assert (row["dataset_name"], row["agent_name"], row["hidden"]) == (
            "ada-secret", "ada-agent", plain)

    _isolate()
    # bob sees carol's run (an admin's) but neither ada's dataset nor ada's agent
    assert ada_ds not in _ids(bob_client.get("/api/eval/datasets").json(), "datasets")
    row = seen_by(bob_client)
    assert row["dataset_id"] == ada_ds and row["dataset_name"] is None
    assert row["agent_id"] == ada_agent and row["agent_name"] is None
    assert row["hidden"] == {"agent": True, "dataset": True}
    assert "ada-secret" not in bob_client.get("/api/eval/runs").text
    # the rows' creator and the administrators keep the names
    for client in (ada_client, carol, admin):
        row = seen_by(client)
        assert (row["dataset_name"], row["agent_name"], row["hidden"]) == (
            "ada-secret", "ada-agent", plain)
    # a dataset-less window run of an admin's references nothing to mask
    window = _add(EvalRun(
        agent_id="", agent_name="svc", dataset_name="window:24h", status="completed",
        log_source={"service_name": "svc", "log_group_names": ["/g"]},
        created_by=carol_id, created_by_name="carol",
    ))
    row = bob_client.get(f"/api/eval/runs/{window}").json()
    assert row["dataset_name"] == "window:24h" and row["agent_name"] == "svc"
    assert row["hidden"] == plain
