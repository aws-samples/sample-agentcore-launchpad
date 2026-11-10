"""The memory console shows a member of an isolated workspace only THEIR OWN actors
(N17-obs531).

Workshop dry run 2026-10-29: a student (member, `isolated` workspace) called
`GET /api/memory/actors` and received 85 actors, among them other students' and the
administrator's `<agent>__<username>` ids — `routers/memory` dropped only the actors of
HIDDEN agents and never restricted the human segment. Now (`routers/memory._Hidden`) an actor
is a member's iff its human segment is their username (an unscoped id iff it IS their
username); every other actor is dropped from the lists and answers `404
memory.actor_not_found` on every keyed route — the same envelope a hidden agent's actor gets.
Administrators and `shared` workspaces are unchanged.
"""

from types import SimpleNamespace

import pytest

from app.models.ledger import Agent
from app.services import memory_backend as mb
from tests import test_visibility as tv
from tests.test_visibility import _add, _isolate, _member, _refused

app, admin, ada, bob = tv.app, tv.admin, tv.ada, tv.bob

ARN = "arn:aws-cn:bedrock-agentcore:cn-northwest-1:111122223333:runtime/shared_agent-abcdefghij"


@pytest.fixture
def scope(monkeypatch, app, admin, ada, bob):
    """One creator-unknown agent (visible to everyone, so the hidden-AGENT axis stays quiet)
    with actors for bob, ada and the administrator, plus two unscoped ids."""
    carol_id, carol_client = _member(app, "carol", role="admin")
    agent = _add(Agent(
        name="shared-agent", method="zip_runtime", status="active", created_by=None,
        arn=ARN, resource_id="shared_agent-abcdefghij",
        spec={"name": "shared-agent", "method": "zip_runtime", "system_prompt": "p"},
    ))
    actors = {
        "bob": f"{agent}__bob", "ada": f"{agent}__ada", "carol": f"{agent}__carol",
        "bob_unscoped": "bob", "default": "default",
    }
    fake = SimpleNamespace(
        list_actors=lambda *a, **k: {
            "items": [mb.decode_actor(x) for x in actors.values()], "next_token": None},
        list_sessions=lambda *a, **k: {"items": [{"session_id": "s-1"}], "next_token": None},
        list_events=lambda *a, **k: {"items": [], "next_token": None},
        resolve_namespaces=lambda _ws, actor, *a, **k: [
            {"namespace": f"/facts/{actor}", "strategy_id": "s1", "resolvable": True}],
        list_records=lambda *a, **k: {"items": [], "next_token": None},
        search_records=lambda *a, **k: {"items": []},
        list_extraction_jobs=lambda *a, **k: {
            "items": [{"actor_id": x} for x in actors.values()], "next_token": None},
        delete_record=lambda *a, **k: {"deleted": True},
    )
    monkeypatch.setattr(mb, "get_backend", lambda: fake)
    return SimpleNamespace(
        agent=agent, actors=actors, admin=admin, ada=ada[1], bob=bob[1], carol=carol_client,
    )


def _actor_ids(client) -> list[str]:
    res = client.get("/api/memory/actors")
    assert res.status_code == 200, res.text
    return [a["actor_id"] for a in res.json()["items"]]


def _keyed_routes(actor: str) -> list[tuple]:
    return [
        ("GET", f"/api/memory/sessions?actor_id={actor}"),
        ("GET", f"/api/memory/events?actor_id={actor}&session_id=s-1"),
        ("GET", f"/api/memory/namespaces?actor_id={actor}"),
        ("GET", f"/api/memory/records?actor_id={actor}"),
        ("GET", f"/api/memory/records?namespace=/facts/{actor}"),
        ("POST", "/api/memory/records/search", {"query": "q", "actor_id": actor}),
        ("POST", "/api/memory/records/search", {"query": "q", "namespace": f"/facts/{actor}"}),
        ("GET", f"/api/memory/extraction-jobs?actor_id={actor}"),
        ("DELETE", f"/api/memory/records/r1?actor_id={actor}"),
    ]


def test_an_isolated_member_lists_only_their_own_actors(scope):
    everyone = list(scope.actors.values())
    assert _actor_ids(scope.bob) == everyone  # shared: unchanged
    _isolate()
    assert _actor_ids(scope.bob) == [scope.actors["bob"], scope.actors["bob_unscoped"]]
    assert _actor_ids(scope.ada) == [scope.actors["ada"]]
    assert _actor_ids(scope.admin) == everyone
    assert _actor_ids(scope.carol) == everyone
    jobs = scope.bob.get("/api/memory/extraction-jobs").json()["items"]
    assert [j["actor_id"] for j in jobs] == [scope.actors["bob"], scope.actors["bob_unscoped"]]


def test_another_users_actor_answers_404_on_every_keyed_route(scope):
    _isolate()
    for foreign in ("ada", "carol", "default"):
        _refused(scope.bob, "memory.actor_not_found", *_keyed_routes(scope.actors[foreign]))
    # a namespace carrying another human's SCOPED actor is foreign even when the request
    # names the caller's own actor_id (explicit namespace wins in `_resolve_namespace`)
    res = scope.bob.get(
        f"/api/memory/records?actor_id={scope.actors['bob']}&namespace=/facts/{scope.actors['ada']}"
    )
    assert res.status_code == 404 and res.json()["code"] == "memory.actor_not_found"


def test_the_members_own_actors_stay_readable(scope):
    _isolate()
    for own in ("bob", "bob_unscoped"):
        actor = scope.actors[own]
        for method, url, *body in _keyed_routes(actor)[:-1]:
            res = scope.bob.request(method, url, json=body[0] if body else None)
            assert res.status_code == 200, (method, url, res.text)
    # the delete keeps its own rule (N10v2-bug6): the agent has no creator → administrator only
    res = scope.bob.delete(f"/api/memory/records/r1?actor_id={scope.actors['bob']}")
    assert res.status_code == 403 and res.json()["code"] == "memory.record_forbidden"


def test_administrators_and_shared_workspaces_read_every_actor(scope):
    # shared: bob reads ada's actor
    for method, url, *body in _keyed_routes(scope.actors["ada"])[:-1]:
        res = scope.bob.request(method, url, json=body[0] if body else None)
        assert res.status_code == 200, (method, url, res.text)
    _isolate()
    for client in (scope.admin, scope.carol):
        for method, url, *body in _keyed_routes(scope.actors["ada"]):
            res = client.request(method, url, json=body[0] if body else None)
            assert res.status_code == 200, (method, url, res.text)
