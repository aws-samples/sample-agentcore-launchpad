"""Workspace member visibility isolation (`app.services.visibility`).

A workspace is `shared` (default — every member sees every resource) or
`isolated`: a member then sees a resource iff they created it, its creator is
unknown (NULL), or its creator is a *current* admin. Admins see everything. A
hidden resource answers the kind's ordinary not-found, everywhere: lists,
pickers, by-id routes and create-time references.
"""

from datetime import UTC, datetime, timedelta
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
        return row.id
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


# ─── the helper itself ───────────────────────────────────────────────────────


def test_mode_defaults_to_shared_and_ignores_unknown_values():
    assert visibility.mode(None) == visibility.SHARED
    assert visibility.mode(Workspace(id="w", settings=None)) == visibility.SHARED
    assert visibility.mode(Workspace(id="w", settings={"resource_visibility": "x"})) == "shared"
    assert visibility.mode(Workspace(id="w", settings={"resource_visibility": "isolated"})) == (
        visibility.ISOLATED
    )


def test_set_mode_keeps_other_settings_and_rejects_unknown_values():
    db = SessionLocal()
    try:
        row = db.get(Workspace, DEFAULT_WORKSPACE_ID)
        row.settings = {"inbound_auth_default": {"type": "iam"}}
        db.commit()
        visibility.set_mode(db, row, visibility.ISOLATED)
        db.expire_all()
        row = db.get(Workspace, DEFAULT_WORKSPACE_ID)
        assert row.settings == {
            "inbound_auth_default": {"type": "iam"},
            "resource_visibility": "isolated",
        }
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
                         region="eu-west-1", bootstrap_status="registered", resources={}))
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


def _ws(workspace_id: str) -> Workspace:
    db = SessionLocal()
    try:
        return db.get(Workspace, workspace_id)
    finally:
        db.close()


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


# ─── datasets, cloud datasets, pipelines ─────────────────────────────────────


def test_datasets_shared_by_default(ada, bob):
    ada_id, _ = ada
    _, bob_client = bob
    ds = _dataset(ada_id)
    assert ds in _ids(bob_client.get("/api/eval/datasets").json(), "datasets")
    assert bob_client.put(f"/api/eval/datasets/{ds}", json={"name": "renamed"}).status_code == 200


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
    assert bob_client.put(f"/api/eval/datasets/{unknown}", json={"name": "y"}).status_code == 200

    # a demoted admin's dataset turns private
    _set_role(ann_id, "member")
    assert published not in _ids(bob_client.get("/api/eval/datasets").json(), "datasets")


def test_isolated_cloud_datasets_follow_their_local_rows(monkeypatch, ada, bob):
    from app.evaluation import routers as eval_routers

    ada_id, _ = ada
    bob_id, bob_client = bob
    _dataset(ada_id, "ada-ds", cloud={"dataset_id": "cloud-ada"})
    _dataset(bob_id, "bob-ds", cloud={"dataset_id": "cloud-both"})
    _dataset(ada_id, "ada-ds2", cloud={"dataset_id": "cloud-both"})
    listed = [
        {"datasetId": cid, "datasetName": cid, "status": "ACTIVE"}
        for cid in ("cloud-ada", "cloud-both", "cloud-outside")
    ]
    monkeypatch.setattr(eval_routers, "control_client", lambda ctx: object())
    monkeypatch.setattr(eval_routers.ac, "list_datasets", lambda client: listed)
    deleted: list[str] = []
    monkeypatch.setattr(
        eval_routers.ac, "delete_dataset",
        lambda client, dataset_id, version=None: deleted.append(dataset_id),
    )

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
    assert deleted == []
    assert bob_client.delete("/api/eval/datasets/cloud/cloud-outside").status_code == 200


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


def _active_agent() -> str:
    from app.models.ledger import Agent

    return _add(
        Agent(
            name="vis-agent", method="zip_runtime", status="active",
            arn="arn:aws:bedrock-agentcore:us-west-2:111122223333:runtime/vis-abcdefghij",
            resource_id="vis-abcdefghij", spec={"protocol": "http"},
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
        lambda: bob_client.put(f"/api/eval/runs/{theirs}/recommendations/r1/edit", json={}),
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


def test_a_run_cannot_start_on_a_hidden_dataset(ada, bob):
    ada_id, _ = ada
    _, bob_client = bob
    agent_id = _active_agent()
    theirs = _dataset(ada_id, cloud={"dataset_id": "cloud-ada"})
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


def _experiment(created_by: str | None, **kw: Any) -> str:
    from app.optimization.models import Experiment

    return _add(Experiment(name="e", agent_id="a1", agent_name="agent",
                           created_by=created_by, **kw))


def _canary(created_by: str | None, **kw: Any) -> str:
    from app.optimization.models import RuntimeCanary

    return _add(RuntimeCanary(
        name="c", champion_agent_id="a1", champion_agent_name="agent",
        challenger_agent_id="a1", challenger_agent_name="agent", created_by=created_by, **kw,
    ))


@pytest.fixture
def online_world(monkeypatch, ada, bob):
    """Four configs: ada's agent config (ledger row), ada's experiment config, bob's
    canary config and one made outside the console."""
    from app.evaluation import online
    from app.evaluation.models import OnlineEvalConfig
    from tests.evaluation.test_online_configs import aws_detail, stub_environment

    ada_id, _ = ada
    bob_id, _ = bob
    exp_id = _experiment(ada_id)
    can_id = _canary(bob_id)
    ids = {
        "row": "oe_ada-1111111111",
        "exp": f"exp_{exp_id[:8]}_oe1-2222222222",
        "can": f"can_{can_id[:8]}_oec-3333333333",
        "ext": "outside-4444444444",
    }
    details = {cid: aws_detail(cid, cid.split("-")[0]) for cid in ids.values()}
    control, _ = stub_environment(monkeypatch, details)
    _add(OnlineEvalConfig(
        agent_id="a1", agent_name="agent", config_id=ids["row"], config_arn="arn:x",
        name="oe_ada", service_name="s", log_group="g", created_by=ada_id,
    ))
    monkeypatch.setattr(online, "results", lambda ws, cid, rk, **kw: {"range": rk, "ok": cid})
    return ids


def test_online_configs_shared_by_default(online_world, bob):
    _, bob_client = bob
    listed = bob_client.get("/api/eval/online").json()["configs"]
    assert {c["config_id"] for c in listed} == set(online_world.values())


def test_isolated_online_configs(online_world, admin, ada, bob):
    _, ada_client = ada
    _, bob_client = bob
    _isolate()
    listed = bob_client.get("/api/eval/online").json()
    assert {c["config_id"] for c in listed["configs"]} == {online_world["can"], online_world["ext"]}
    assert listed["total"] == 2
    assert {c["config_id"] for c in admin.get("/api/eval/online").json()["configs"]} == set(
        online_world.values()
    )
    assert {c["config_id"] for c in ada_client.get("/api/eval/online").json()["configs"]} == {
        online_world["row"], online_world["exp"], online_world["ext"]
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
    assert bob_client.get(f"/api/eval/online/{online_world['ext']}/results").json()["ok"]
    assert ada_client.get(f"/api/eval/online/{online_world['exp']}").status_code == 200


def test_online_reports_drop_hidden_console_runs(online_world, ada, bob, monkeypatch):
    from app.evaluation import online

    ada_id, ada_client = ada
    bob_id, bob_client = bob
    cfg = online_world["ext"]
    _run(ada_id, dataset_name=f"online:{cfg}", batch_eval_id="batch-ada")
    _run(bob_id, dataset_name=f"online:{cfg}", batch_eval_id="batch-bob")
    batches = [{"batchEvaluationId": b, "status": "COMPLETED"} for b in ("batch-ada", "batch-bob")]
    monkeypatch.setattr(online, "data_client", lambda ctx: object())
    monkeypatch.setattr(online.ac, "list_batch_evaluations", lambda data, limit: batches)
    arn = f"arn:aws:bedrock-agentcore:us-west-2:1:online-evaluation-config/{cfg}"
    monkeypatch.setattr(online, "_source_info", lambda data, bid: (arn, {}))
    _isolate()
    rows = bob_client.get(f"/api/eval/online/{cfg}/reports").json()["reports"]
    assert len(rows) == 1 and rows[0]["origin"] == "console"
    refused = bob_client.get(f"/api/eval/online/{cfg}/reports/batch-ada")
    assert refused.status_code == 404
    assert refused.json()["code"] == "online_eval.report_not_found"
    assert len(ada_client.get(f"/api/eval/online/{cfg}/reports").json()["reports"]) == 1


# ─── custom evaluators ───────────────────────────────────────────────────────


def _record(kind: str, resource_id: str, user_id: str | None, name: str = "x") -> None:
    from app.services import creators
    from app.services.creators import Creator

    db = SessionLocal()
    try:
        creators.record(db, DEFAULT_WORKSPACE_ID, kind, resource_id, Creator(user_id, name))
    finally:
        db.close()


@pytest.fixture
def evaluator_world(monkeypatch, ada, bob):
    from unittest.mock import MagicMock

    from app.services import creators

    ada_id, _ = ada
    bob_id, _ = bob
    stub = MagicMock()
    stub.list_evaluators.return_value = {"evaluators": [
        {"evaluatorId": eid, "evaluatorName": eid, "evaluatorType": etype,
         "level": "SESSION", "status": "ACTIVE"}
        for eid, etype in (("ev_ada-1", "Custom"), ("ev_bob-1", "Custom"),
                           ("ev_out-1", "Custom"), ("ThirdParty.Judge", "ThirdParty"))
    ]}
    stub.get_evaluator.side_effect = lambda evaluatorId: {  # noqa: N803 — boto3 shape
        "evaluatorId": evaluatorId, "evaluatorName": evaluatorId, "level": "SESSION",
        "status": "ACTIVE", "evaluatorConfig": {"llmAsAJudge": {"instructions": "{context}"}},
    }
    monkeypatch.setattr("app.evaluation.routers.control_client", lambda _ws=None: stub)
    _record(creators.EVALUATOR, "ev_ada-1", ada_id)
    _record(creators.EVALUATOR, "ev_bob-1", bob_id)
    return stub


def test_isolated_evaluators(evaluator_world, admin, ada, bob):
    _, ada_client = ada
    _, bob_client = bob

    def custom(client):
        return {
            e["id"] for e in client.get("/api/eval/evaluators").json()["evaluators"]
            if e["source"] != "builtin"
        }

    everything = {"ev_ada-1", "ev_bob-1", "ev_out-1", "ThirdParty.Judge"}
    assert custom(bob_client) == everything
    _isolate()
    assert custom(bob_client) == {"ev_bob-1", "ev_out-1", "ThirdParty.Judge"}
    assert custom(admin) == everything
    builtins = [
        e for e in bob_client.get("/api/eval/evaluators").json()["evaluators"]
        if e["source"] == "builtin"
    ]
    assert builtins  # built-ins are never filtered
    body = {"instructions": "Rate the {context} please", "level": "SESSION"}
    _refused(
        bob_client, "aws.not_found",
        ("GET", "/api/eval/evaluators/ev_ada-1"),
        ("PUT", "/api/eval/evaluators/ev_ada-1", body),
        ("DELETE", "/api/eval/evaluators/ev_ada-1"),
    )
    evaluator_world.delete_evaluator.assert_not_called()
    evaluator_world.update_evaluator.assert_not_called()
    assert ada_client.get("/api/eval/evaluators/ev_ada-1").status_code == 200
    assert bob_client.get("/api/eval/evaluators/ev_out-1").status_code == 200


def test_hidden_evaluators_cannot_be_referenced(evaluator_world, monkeypatch, bob):
    from unittest.mock import MagicMock

    from app.evaluation import online
    from app.optimization import canary_routers
    from app.optimization import routers as exp_routers

    _, bob_client = bob
    agent_id = _active_agent()
    ds = _dataset(None)
    exp = _experiment(None, stage="bundles", artifacts={"bundles": {}})
    monkeypatch.setattr(online, "control_client", lambda _ctx: MagicMock())
    monkeypatch.setattr(exp_routers.service, "assert_shared_gateway_available",
                        lambda *a, **k: None)
    monkeypatch.setattr(exp_routers, "control_client", lambda _ctx=None: evaluator_world)
    monkeypatch.setattr(canary_routers, "control_client", lambda _ctx=None: evaluator_world)
    monkeypatch.setattr(canary_routers, "_eligible_agent",
                        lambda db, ws, aid: MagicMock(method="zip_runtime", id=aid))
    monkeypatch.setattr(canary_routers, "_resolve_edited_spec", lambda agent, cand: {})
    monkeypatch.setattr(exp_routers.service, "run_action", lambda *a, **k: None)
    _isolate()

    run = bob_client.post("/api/eval/runs", json={
        "agent_id": agent_id, "dataset_id": ds, "evaluators": ["ev_ada-1"],
    })
    assert run.status_code == 422, run.text
    assert run.json()["code"] == "run.evaluator_not_found"

    created = bob_client.post("/api/eval/online", json={
        "agent_id": agent_id, "evaluators": ["ev_ada-1"],
    })
    assert created.status_code == 400, created.text
    assert created.json()["code"] == "online_eval.evaluator_unsupported"

    gateway = bob_client.post(f"/api/experiments/{exp}/action", json={
        "action": "gateway", "online_evaluators": ["ev_ada-1"],
    })
    assert gateway.status_code == 400, gateway.text
    assert gateway.json()["code"] == "experiment.evaluator_unsupported"

    canary = bob_client.post("/api/runtime-canaries", json={
        "agent_id": agent_id, "candidate": {"system_prompt": "new"},
        "online_evaluators": ["ev_ada-1"],
    })
    assert canary.status_code == 400, canary.text
    assert canary.json()["code"] == "canary.evaluator_unsupported"

    score = bob_client.post("/api/observability/sessions/session-0001-abcdef/evaluate", json={
        "evaluator_ids": ["ev_ada-1"],
    })
    assert score.status_code == 404, score.text
    assert score.json()["code"] == "aws.not_found"


# ─── experiments + runtime canaries ──────────────────────────────────────────


def test_isolated_experiments_and_canaries_filter_before_the_limit(admin, ada, bob):
    ada_id, _ = ada
    bob_id, bob_client = bob
    mine_exp = _experiment(bob_id)
    mine_can = _canary(bob_id)
    # 25 newer foreign rows would push the member's own out of a post-hoc .limit(20)
    for _ in range(25):
        _experiment(ada_id)
        _canary(ada_id)
    _isolate()
    assert _ids(bob_client.get("/api/experiments").json(), "experiments") == {mine_exp}
    assert _ids(bob_client.get("/api/runtime-canaries").json(), "canaries") == {mine_can}
    assert len(admin.get("/api/experiments").json()["experiments"]) == 20


def test_isolated_experiment_and_canary_by_id_routes_are_404(ada, bob):
    ada_id, ada_client = ada
    _, bob_client = bob
    exp = _experiment(ada_id)
    can = _canary(ada_id)
    _isolate()
    _refused(
        bob_client, "experiment.not_found",
        ("GET", f"/api/experiments/{exp}"),
        ("POST", f"/api/experiments/{exp}/action", {"action": "cleanup"}),
    )
    _refused(
        bob_client, "canary.not_found",
        ("GET", f"/api/runtime-canaries/{can}"),
        ("POST", f"/api/runtime-canaries/{can}/action", {"action": "cleanup"}),
    )
    assert ada_client.get(f"/api/experiments/{exp}").status_code == 200
    assert ada_client.get(f"/api/runtime-canaries/{can}").status_code == 200


def test_experiment_and_canary_references_respect_isolation(monkeypatch, ada, bob):
    from unittest.mock import MagicMock

    from app.optimization import canary_routers
    from app.optimization import routers as exp_routers

    ada_id, _ = ada
    bob_id, bob_client = bob
    hidden_ds = _dataset(ada_id)
    hidden_run = _run(ada_id, status="completed")
    hidden_exp = _experiment(ada_id, artifacts={"promote": {"status": "promoted"}})
    exp = _experiment(bob_id, stage="abtest", artifacts={"abtest": {}})
    can = _canary(bob_id, stage="setup", artifacts={"setup": {"ab_test_id": "t"}})
    monkeypatch.setattr(exp_routers.service, "stage_not_ready_reason", lambda *a: None)
    monkeypatch.setattr(canary_routers.canary_service, "stage_not_ready_reason", lambda *a: None)
    monkeypatch.setattr(canary_routers, "_eligible_agent",
                        lambda db, ws, aid: MagicMock(method="zip_runtime", id=aid))
    monkeypatch.setattr(canary_routers.service, "promotion_complete", lambda artifacts: True)
    _isolate()

    for url in (f"/api/experiments/{exp}/action", f"/api/runtime-canaries/{can}/action"):
        refused = bob_client.post(url, json={"action": "traffic", "dataset_id": hidden_ds})
        assert refused.status_code == 404, refused.text
        assert refused.json()["code"] == "dataset.not_found"

    rec = bob_client.post(f"/api/experiments/{exp}/action", json={
        "action": "recommend", "recommend_source_run_id": hidden_run,
    })
    assert rec.status_code == 404, rec.text
    assert rec.json()["code"] == "experiment.recommend_source_not_found"

    canary = bob_client.post("/api/runtime-canaries", json={
        "agent_id": "a1", "candidate": {"system_prompt": "new"},
        "source_experiment_id": hidden_exp,
    })
    assert canary.status_code == 400, canary.text
    assert canary.json()["code"] == "canary.source_experiment_invalid"


# ─── knowledge bases ─────────────────────────────────────────────────────────


@pytest.fixture
def kb_world(monkeypatch, ada, bob):
    from app.services import creators, knowledge

    ada_id, _ = ada
    bob_id, _ = bob
    _record(creators.KB, "KBADA", ada_id)
    _record(creators.KB, "KBBOB", bob_id)
    monkeypatch.setattr(knowledge, "list_kbs", lambda _ws: [
        {"kb_id": kb, "name": kb, "status": "ACTIVE"} for kb in ("KBADA", "KBBOB", "KBOUT")
    ])
    calls: list[str] = []

    def stub(name):
        def fn(*args, **kwargs):
            calls.append(name)
            return {"kb_id": args[1] if len(args) > 1 else None}
        return fn

    for name in ("get_kb_detail", "update_description", "delete_kb", "upload_files",
                 "add_data_source", "delete_data_source", "start_sync",
                 "list_ingestion_jobs", "list_documents", "query"):
        monkeypatch.setattr(knowledge, name, stub(name))
    return calls


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
        ("GET", f"{base}/data-sources/DS1/ingestion-jobs"),
        ("GET", f"{base}/data-sources/DS1/documents"),
        ("POST", f"{base}/query", {"text": "hello"}),
    )
    upload = bob_client.post(f"{base}/files", files={"files": ("a.txt", b"hi")})
    assert upload.status_code == 404
    assert upload.json()["code"] == "kb.not_found"
    assert kb_world == []  # refused before any AWS call
    assert ada_client.get(base).status_code == 200
    assert bob_client.get("/api/knowledge-bases/KBOUT").status_code == 200


# ─── managed memories ────────────────────────────────────────────────────────


def test_isolated_managed_memories(monkeypatch, admin, ada, bob):
    import app.services.memory_admin as ma
    import app.services.memory_ownership as mo
    from app.models.ledger import ManagedMemory
    from tests.conftest import set_default_resources
    from tests.test_memory_ownership import StatusControl
    from tests.test_memory_resources import MEM_ID

    ada_id, ada_client = ada
    bob_id, bob_client = bob
    set_default_resources({"memory_id": MEM_ID})
    stub = StatusControl(memories=[
        {"id": mid, "arn": f"arn:mem:{mid}", "status": "ACTIVE"}
        for mid in (MEM_ID, "mem_ada-AAAA", "mem_bob-BBBB", "detected-CCCC")
    ])
    monkeypatch.setattr(ma, "control_client", lambda _ws=None: stub)
    monkeypatch.setattr(mo, "control_client", lambda _ws=None: stub)
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
    # pin/deploy/chat paths keep the unfiltered ownership check (agent pins: child 3)
    db = SessionLocal()
    try:
        ws = _ws(DEFAULT_WORKSPACE_ID)
        from app.services.workspace import workspace_context

        assert mo.is_managed(db, workspace_context(ws), "mem_ada-AAAA")
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
    monkeypatch.setattr(overview_mod, "console_list", lambda _ws: [])
    monkeypatch.setattr(overview_mod, "_traces_active", lambda _ws: False)
    monkeypatch.setattr(overview_mod, "attached_policy_engine_id", lambda _c, _ws: "")
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


def test_online_quality_is_per_viewer_and_cached_per_viewer(monkeypatch, admin, ada, bob):
    import app.routers.overview as overview_mod
    from app.evaluation import online
    from app.evaluation.models import OnlineEvalConfig

    ada_id, ada_client = ada
    bob_id, bob_client = bob
    overview_mod._cache.clear()
    for cid, owner in (("oe_ada-1", ada_id), ("oe_bob-1", bob_id)):
        _add(OnlineEvalConfig(agent_id=f"agent-{cid}", agent_name="a", config_id=cid,
                              config_arn="arn", name=cid, service_name="s", log_group="g",
                              created_by=owner))
    queried: list[set[str]] = []

    def run(queries, hours, **kw):
        queried.append({c for c in ("oe_ada-1", "oe_bob-1") if c in queries["pairs"]})
        return {"pairs": [], "totals": []}

    monkeypatch.setattr(online, "run_insights_queries", run)
    _isolate()
    bob_tile = bob_client.get("/api/overview/online-quality").json()
    assert bob_tile["configs"] == 1 and bob_tile["cached"] is False
    admin_tile = admin.get("/api/overview/online-quality").json()
    assert admin_tile["configs"] == 2 and admin_tile["cached"] is False  # no leak from bob
    assert ada_client.get("/api/overview/online-quality").json()["configs"] == 1
    assert bob_client.get("/api/overview/online-quality").json()["cached"] is True
    assert queried == [{"oe_bob-1"}, {"oe_ada-1", "oe_bob-1"}, {"oe_ada-1"}]
    overview_mod._cache.clear()


# ─── architect catalog ───────────────────────────────────────────────────────


def test_the_architect_catalog_lists_the_owners_visible_kbs(monkeypatch, ada, bob):
    from app.assistant import service as assistant
    from app.core.errors import AppError
    from app.services import creators, knowledge
    from tests.conftest import ws_ctx

    ada_id, _ = ada
    bob_id, _ = bob
    _record(creators.KB, "KBADA", ada_id)
    _record(creators.KB, "KBBOB", bob_id)
    monkeypatch.setattr(knowledge, "list_kbs", lambda _ws: [
        {"kb_id": kb, "name": kb, "status": "ACTIVE"} for kb in ("KBADA", "KBBOB", "KBOUT")
    ])

    def unavailable(*_a, **_k):
        raise AppError("registry.unavailable", "no registry")

    monkeypatch.setattr(assistant.registry_console, "attachable_records", unavailable)
    monkeypatch.setattr(assistant, "catalog_evaluators", lambda ws, warnings: [])

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
    from types import SimpleNamespace

    from app.assistant import service as assistant
    from app.services import creators

    ada_id, _ = ada
    bob_id, _ = bob
    _record(creators.KB, "KBADA", ada_id)
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
        "experiments": _experiment(ada_id),
        "canaries": _canary(ada_id),
    }
    job, taskset = _job(ada_id), _taskset(ada_id)
    assert rows["datasets"] in _ids(bob_client.get("/api/eval/datasets").json(), "datasets")
    assert rows["pipelines"] in _ids(bob_client.get("/api/eval/pipelines").json(), "pipelines")
    assert rows["runs"] in _ids(bob_client.get("/api/eval/runs").json(), "runs")
    assert rows["experiments"] in _ids(bob_client.get("/api/experiments").json(), "experiments")
    assert rows["canaries"] in _ids(bob_client.get("/api/runtime-canaries").json(), "canaries")
    assert job in {j["id"] for j in bob_client.get("/api/skill-lab/jobs").json()}
    assert taskset in {t["id"] for t in bob_client.get("/api/skill-lab/tasksets").json()}
    for url in (f"/api/eval/pipelines/{rows['pipelines']}", f"/api/eval/runs/{rows['runs']}",
                f"/api/experiments/{rows['experiments']}",
                f"/api/runtime-canaries/{rows['canaries']}", f"/api/skill-lab/jobs/{job}"):
        assert bob_client.get(url).status_code == 200, url
