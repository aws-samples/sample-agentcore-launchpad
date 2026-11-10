"""E19 — workspace visibility switch under concurrent reads (round 16, channel A; §7).

The administrator flips the default workspace between ``shared`` and ``isolated`` while
members list and read agents from other threads of the same process (the one uvicorn
worker's thread pool). Pinned:

* every list response is WHOLLY old-mode or WHOLLY new-mode — the set of ids a member gets
  is either the shared set or that member's isolated set, never a mix (the viewer is
  resolved once per request from the workspace row);
* a by-id read of an agent the member cannot see answers the kind's not-found, with the
  SAME body as an id that does not exist at all;
* a random sequence of mode flips and role changes keeps the list equal to the oracle set
  computed from the contract rule (created_by NULL / own / a current admin's).
"""

from __future__ import annotations

import threading
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from hypothesis import HealthCheck, given
from hypothesis import settings as h_settings
from hypothesis import strategies as st

from app.core.config import get_settings
from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.main import create_app
from app.models.ledger import Agent, User, Workspace
from app.services import users as users_service
from app.services import visibility

pytestmark = pytest.mark.property

ADMIN = {"username": "operator", "password": "s3cret-pass"}
ARN = "arn:aws-cn:bedrock-agentcore:cn-northwest-1:111122223333:runtime/{rid}"
VISIBILITY = f"/api/workspaces/{DEFAULT_WORKSPACE_ID}/settings/visibility"


def _member(app, username: str, role: str = "member") -> tuple[str, TestClient]:
    db = SessionLocal()
    try:
        user = users_service.register_user(
            db, username, f"{username}@acme-corp.com", "Correct-Horse-Battery-0000",
            created_by="operator", role=role)
        user.status = users_service.STATUS_ACTIVE
        user.expires_at = datetime.now(UTC) + timedelta(days=7)
        users_service.set_workspace_grants(db, user, [DEFAULT_WORKSPACE_ID])
        db.commit()
        user_id = user.id
    finally:
        db.close()
    client = TestClient(app, client=("10.3.3.3", 1))
    login = client.post("/api/auth/login",
                        json={"username": username, "password": "Correct-Horse-Battery-0000"})
    assert login.status_code == 200, login.text
    return user_id, client


def _agent(name: str, created_by: str | None) -> str:
    rid = f"{name.replace('-', '_')}-abcdefghij"
    db = SessionLocal()
    try:
        row = Agent(name=name, method="zip_runtime", status="active", created_by=created_by,
                    created_by_name="someone" if created_by else None,
                    arn=ARN.format(rid=rid), resource_id=rid, workspace_id=DEFAULT_WORKSPACE_ID,
                    spec={"name": name, "method": "zip_runtime", "system_prompt": "p",
                          "protocol": "http"})
        db.add(row)
        db.commit()
        return row.id
    finally:
        db.close()


def _set_mode(value: str) -> None:
    db = SessionLocal()
    try:
        visibility.set_mode(db, db.get(Workspace, DEFAULT_WORKSPACE_ID), value)
    finally:
        db.close()


def _set_role(user_id: str, role: str) -> None:
    db = SessionLocal()
    try:
        db.get(User, user_id).role = role
        db.commit()
    finally:
        db.close()


@pytest.fixture
def world(monkeypatch):
    monkeypatch.setenv("LAUNCHPAD_AUTH_USERNAME", ADMIN["username"])
    monkeypatch.setenv("LAUNCHPAD_AUTH_PASSWORD", ADMIN["password"])
    monkeypatch.setattr(users_service, "_ITERATIONS", 1000)
    get_settings.cache_clear()
    app = create_app()
    admin = TestClient(app, client=("10.9.9.9", 1))
    assert admin.post("/api/auth/login", json=ADMIN).status_code == 200
    ada_id, ada = _member(app, "ada")
    bob_id, bob = _member(app, "bob")
    carol_id, carol = _member(app, "carol", role="admin")
    agents = {
        "ada1": _agent("ada-one", ada_id), "ada2": _agent("ada-two", ada_id),
        "ada3": _agent("ada-three", ada_id),
        "bob1": _agent("bob-one", bob_id), "bob2": _agent("bob-two", bob_id),
        "carol1": _agent("carol-one", carol_id),
        "legacy": _agent("legacy-one", None),
    }
    w = SimpleNamespace(app=app, admin=admin, ada=ada, bob=bob, carol=carol,
                        ada_id=ada_id, bob_id=bob_id, carol_id=carol_id, agents=agents)
    yield w
    for client in (admin, ada, bob, carol):
        client.close()
    get_settings.cache_clear()


def _ids(client: TestClient) -> set[str]:
    res = client.get("/api/agents")
    assert res.status_code == 200, res.text
    return {a["id"] for a in res.json()["agents"]}


def _oracle(world, viewer_id: str, viewer_role: str, mode: str, roles: dict[str, str]) -> set:
    """§7 "The rule": a member in an isolated workspace sees a row whose created_by is NULL,
    their own users.id, or the id of a user whose CURRENT role is admin; admins see
    everything; shared (absent / unknown) → everything."""
    everything = set(world.agents.values())
    if mode == visibility.SHARED or viewer_role == "admin":
        return everything
    creators = {world.agents["ada1"]: world.ada_id, world.agents["ada2"]: world.ada_id,
                world.agents["ada3"]: world.ada_id, world.agents["bob1"]: world.bob_id,
                world.agents["bob2"]: world.bob_id, world.agents["carol1"]: world.carol_id,
                world.agents["legacy"]: None}
    return {aid for aid, creator in creators.items()
            if creator is None or creator == viewer_id or roles.get(creator) == "admin"}


# ── concurrent reads while toggling ──────────────────────────────────────────


ROUNDS = 6  # flips; even rounds set ISOLATED, odd rounds SHARED (the world starts SHARED)
BARRIER_S = 60.0


def test_concurrent_lists_are_wholly_one_mode_while_the_switch_flips(world):
    """§7: a list is wholly one mode — never a page half shared, half isolated — while the
    administrator flips the switch underneath concurrent readers.

    N16-obs89f: the first version let three reader threads free-run against 60 flips and
    then demanded `len(seen) >= 10` — a budget, not a synchronization, so under host load the
    readers managed six snapshots and the test failed on an untouched tree (1 / 3 runs on
    d6af4893 at load 12). Every observation is explicit now: per round each reader takes one
    snapshot BEFORE the flip (barrier), races the flip with further snapshots, and takes one
    AFTER the flip has returned (event) — so a pre-flip and a post-flip snapshot per reader
    per round exist by construction, their modes are known exactly, and the racing snapshots
    must still be wholly one mode. The snapshot count is a diagnostic only."""
    roles = {world.ada_id: "member", world.bob_id: "member", world.carol_id: "admin"}
    shared = _oracle(world, world.bob_id, "member", visibility.SHARED, roles)
    isolated = _oracle(world, world.bob_id, "member", visibility.ISOLATED, roles)
    assert shared != isolated
    pages = {visibility.SHARED: frozenset(shared), visibility.ISOLATED: frozenset(isolated)}
    hidden, unknown = world.agents["ada1"], str(uuid.uuid4())
    readers_n = 3
    ready = threading.Barrier(readers_n + 1, timeout=BARRIER_S)   # round start
    go = threading.Barrier(readers_n + 1, timeout=BARRIER_S)      # pre snapshots taken → flip
    done = threading.Barrier(readers_n + 1, timeout=BARRIER_S)    # round end
    flipped = [threading.Event() for _ in range(ROUNDS)]
    # (phase, round, page) — phase ∈ pre | race | post
    seen: list[tuple[str, int, frozenset[str]]] = []
    by_id: list[tuple[int, dict]] = []
    errors: list[str] = []
    lock = threading.Lock()

    def mode_of(i: int) -> str:
        return visibility.ISOLATED if i % 2 == 0 else visibility.SHARED

    def toggler() -> None:
        try:
            for i in range(ROUNDS):
                ready.wait()
                go.wait()  # every reader holds a pre-flip snapshot of this round
                res = world.admin.put(VISIBILITY, json={"resource_visibility": mode_of(i)})
                if res.status_code != 200:
                    with lock:
                        errors.append(f"PUT {res.status_code} {res.text}")
                flipped[i].set()
                done.wait()
        except threading.BrokenBarrierError as exc:
            with lock:
                errors.append(f"toggler barrier: {exc!r}")
            for ev in flipped:
                ev.set()

    def reader() -> None:
        client = TestClient(world.app, client=("10.3.3.3", 1), cookies=world.bob.cookies)

        def snapshot(phase: str, i: int) -> None:
            res = client.get("/api/agents")
            if res.status_code != 200:
                with lock:
                    errors.append(f"GET {res.status_code} {res.text}")
                return
            one = client.get(f"/api/agents/{hidden}")
            with lock:
                seen.append((phase, i, frozenset(a["id"] for a in res.json()["agents"])))
                by_id.append((one.status_code, one.json()))

        try:
            for i in range(ROUNDS):
                ready.wait()
                snapshot("pre", i)
                go.wait()
                while not flipped[i].is_set():  # race the flip
                    snapshot("race", i)
                snapshot("post", i)
                done.wait()
        except threading.BrokenBarrierError as exc:
            with lock:
                errors.append(f"reader barrier: {exc!r}")
        finally:
            client.close()

    threads = [threading.Thread(target=reader, name=f"vis-reader-{k}") for k in range(readers_n)]
    threads.append(threading.Thread(target=toggler, name="vis-toggler"))
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=BARRIER_S * ROUNDS)
    assert not any(t.is_alive() for t in threads), "a reader or the toggler is stuck"
    assert not errors, errors[:5]
    # the invariant: every page — pre, racing or post — is wholly one mode
    mixed = [(phase, i, set(page)) for phase, i, page in seen if page not in pages.values()]
    assert not mixed, f"mixed pages observed: {mixed[:3]}"
    # the observations exist by construction and their modes are known exactly
    for i in range(ROUNDS):
        before = visibility.SHARED if i == 0 else mode_of(i - 1)
        pre = [page for phase, r, page in seen if phase == "pre" and r == i]
        post = [page for phase, r, page in seen if phase == "post" and r == i]
        assert len(pre) == readers_n and len(post) == readers_n, (i, len(pre), len(post))
        assert set(pre) == {pages[before]}, (i, before, pre)
        assert set(post) == {pages[mode_of(i)]}, (i, mode_of(i), post)
    assert {page for _p, _i, page in seen} == set(pages.values()), "both modes must be observed"
    # by-id: 200 under shared, and the exact unknown-id 404 under isolated
    not_found = world.bob.get(f"/api/agents/{unknown}")
    assert not_found.status_code == 404
    statuses = {status for status, _ in by_id}
    assert statuses == {200, 404}, statuses
    for status, body in by_id:
        if status == 404:
            assert body == not_found.json(), (body, not_found.json())


def test_hidden_and_unknown_ids_answer_the_same_not_found(world):
    # §7: "A hidden id answers the kind's existing not-found" — indistinguishable from a
    # missing one (§8: "so it stays indistinguishable from a missing one")
    _set_mode(visibility.ISOLATED)
    hidden = world.bob.get(f"/api/agents/{world.agents['ada1']}")
    unknown = world.bob.get(f"/api/agents/{uuid.uuid4()}")
    assert hidden.status_code == unknown.status_code == 404
    assert hidden.json() == unknown.json()
    # headers that could leak the difference are the same too
    for header in ("content-type", "content-length"):
        assert hidden.headers.get(header) == unknown.headers.get(header), header


# ── the oracle over random flip / role sequences ─────────────────────────────


@h_settings(max_examples=15, deadline=None,
            suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow])
@given(ops=st.lists(st.sampled_from((
    "shared", "isolated", "carol_member", "carol_admin", "ada_admin", "ada_member",
)), min_size=1, max_size=5))
def test_every_list_matches_the_contract_oracle(world, ops):
    mode = visibility.SHARED
    roles = {world.ada_id: "member", world.bob_id: "member", world.carol_id: "admin"}
    _set_mode(mode)
    for uid, role in roles.items():
        _set_role(uid, role)
    for op in ops:
        if op in (visibility.SHARED, visibility.ISOLATED):
            mode = op
            res = world.admin.put(VISIBILITY, json={"resource_visibility": op})
            assert res.status_code == 200 and res.json() == {"resource_visibility": op}
        else:
            who, role = op.split("_")
            uid = {"carol": world.carol_id, "ada": world.ada_id}[who]
            roles[uid] = role
            _set_role(uid, role)
        for viewer_id, client in ((world.bob_id, world.bob), (world.ada_id, world.ada),
                                  (world.carol_id, world.carol)):
            expected = _oracle(world, viewer_id, roles[viewer_id], mode, roles)
            assert _ids(client) == expected, (op, viewer_id, mode, roles)
            # by-id agrees with the list (one agent of every creator kind)
            for key in ("ada1", "bob1", "carol1", "legacy"):
                aid = world.agents[key]
                one = client.get(f"/api/agents/{aid}")
                assert (one.status_code == 200) == (aid in expected), (op, key)
        assert _ids(world.admin) == set(world.agents.values())
        assert world.admin.get(VISIBILITY).json() == {"resource_visibility": mode}
