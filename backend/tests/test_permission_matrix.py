# ruff: noqa: F811  — pytest fixtures are imported by name from tests.test_visibility
"""The living permission spec: role × resource kind × operation, in both visibility modes.

One table (``EXPECTED`` + ``LISTED`` below) says what every console caller gets from every
resource kind's list / get / create / update / delete, and every cell is asserted against
the running app — so a change to ``core/route_policy.py``, to a creator / visibility gate
or to a handler's own refusal shows up here as a named cell, and a reviewer can read the
posture in one file. Derived from:

* ``app/core/route_policy.py`` — ``ADMIN`` → 403 for members, ``perm:*`` default-granted
  (so a plain member passes), ``PUBLIC`` reachable without a session; an anonymous caller
  gets 401 from the auth middleware on everything else.
* ``docs/cn/interfaces/users-and-ownership.md`` §4 (step-1 member rules: own API keys and
  own chat sessions in BOTH modes; agent redeploy / delete creator-or-admin → 403
  ``agent.forbidden``; hidden key / session → 404) and §7 (``isolated``: a member sees a
  row whose creator is NULL, theirs or a current admin's; a hidden id answers the kind's
  existing not-found; policies stay shared; the by-id CHAT session routes are own-or-admin
  in BOTH modes since N16-obs70/71 — only the observability by-id surfaces stay open under
  ``shared``).

Roles: ``anon`` (no session), ``member`` (the creator of the seeded row), ``member-other``
(another approved member), ``admin`` (a ``users`` row with role admin), ``config-admin``
(the ``LAUNCHPAD_AUTH_USERNAME`` operator — an administrator with no ``users`` row).

Every mutation that would reach AWS is replaced at the handler's seam (deploy start, agent
teardown, runtime session stop, the KB / memory backends, the canary stage
check) so a cell proves the AUTHORIZATION outcome; where no hermetic execution exists the
cell is a probe whose 409 / 422 proves the gate was passed (the pattern of
``test_member_permissions.py``). A cell where the code and the spec disagree is kept as the
spec and listed in ``DISAGREEMENTS`` with the reason — never rewritten to match the code;
such a cell must keep disagreeing (strict-xfail semantics: a cell that starts to agree
fails the test until it is removed from the list).

Shape: one test per kind builds one world (``create_app`` + five sessions) and walks every
(mode, op, role) cell on a freshly seeded row; every mismatch is reported, not just the
first. How to add a cell: add a ``Kind`` (seed + request builders) and its rows in
``EXPECTED`` / ``LISTED``; the walk enumerates every role and mode.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

import app.routers.agents as agents_router
import app.routers.chat as chat_router
from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.evaluation.models import EvalDataset, EvalEvaluator, EvalRun, OnlineEvalConfig
from app.models.ledger import Agent, ApiKey, ChatMessage, ChatSession, KnowledgeBase, ManagedMemory
from app.optimization import canary_routers
from app.optimization.models import RuntimeCanary
from app.routers.apikeys import hash_key
from app.services import aws_clients, knowledge_backend, memory_backend, visibility
from app.services import users as users_service
from tests.conftest import set_default_resources
from tests.policy_fakes import BASELINE, FakeS3
from tests.test_announcements import CONTENT as ANNOUNCEMENT
from tests.test_videos import CONTENT as VIDEO
from tests.test_visibility import (  # noqa: F401 — fixtures by name
    PEER,
    _add,
    _FakeKnowledgeBackend,
    _isolate,
    _member,
    admin,
    app,
)

ROLES = ("anon", "member", "member-other", "admin", "config-admin")
MODES = ("shared", "isolated")
OPS = ("list", "get", "create", "update", "delete")

ARN = "arn:aws-cn:bedrock-agentcore:cn-northwest-1:111122223333:runtime/{rid}"
MEM_DEFAULT = "bootstrap_mem-0123456789"
RATING = [{"value": 1.0, "label": "pass", "definition": "ok"},
          {"value": 0.0, "label": "fail", "definition": "bad"}]

Request = tuple[str, str, Any, dict[str, Any] | None]  # method, path, json, query
Builder = Callable[[Any, str], Request]


@dataclass
class Kind:
    name: str
    seed: Callable[[Any], str]  # (world) -> the seeded row's id, created by `member`
    ops: dict[str, Builder]
    list_ids: Callable[[dict[str, Any]], set[str]] = field(default=lambda body: set())


def _ids(key: str, field_name: str = "id") -> Callable[[dict[str, Any]], set[str]]:
    return lambda body: {str(item.get(field_name)) for item in body.get(key, [])}


def _any_ids(body: dict[str, Any]) -> set[str]:
    """Ids of every dict in the first list-valued member of the response."""
    for value in body.values():
        if isinstance(value, list):
            return {str(item.get("id")) for item in value if isinstance(item, dict)}
    return set()


# ─── the world: five callers, hermetic seams ─────────────────────────────────


class _TeardownReport:
    def as_dict(self) -> dict[str, Any]:
        return {"aws_resource_deleted": False, "removed": [], "failed": [], "steps": []}


def _fake_teardown(db, agent, workspace):
    agent.status = "deleted"
    db.commit()
    return _TeardownReport()


class _FakeMemoryBackend:
    """Lists the bootstrap memory plus every managed row the ledger holds."""

    def list_memory_resources(self, ws):
        db = SessionLocal()
        try:
            ids = [MEM_DEFAULT] + [r.memory_id for r in db.query(ManagedMemory).all()]
        finally:
            db.close()
        return {"items": [{"id": mid, "status": "ACTIVE"} for mid in ids]}

    def get_memory_resource(self, ws, memory_id):
        return {"id": memory_id, "status": "ACTIVE"}

    def update_memory_resource(self, ws, memory_id, **kw):
        return {"id": memory_id, "status": "ACTIVE", **kw}

    def delete_memory_resource(self, ws, memory_id):
        return {"id": memory_id, "deleted": True}

    def create_memory_resource(self, ws, name, description, expiry, strategies, keys,
                               merge=None):  # r14-a: per-store merge settings
        return {"id": f"{name}-NEW000001", "status": "CREATING", "name": name}


@pytest.fixture
def world(app, admin, monkeypatch):
    """Clients for the five roles plus the ids the kinds build their paths from. Every
    AWS-reaching seam is replaced so a cell's outcome is the authorization decision."""
    # the matrix asserts authorization, not KDF strength: registrations and logins run at a
    # token PBKDF2 cost (a stored hash carries its own iteration count, so verification
    # stays consistent)
    monkeypatch.setattr(users_service, "_ITERATIONS", 1_000)
    ada_id, ada = _member(app, "ada")
    bob_id, bob = _member(app, "bob")
    carol_id, carol = _member(app, "carol", role="admin")
    anon = TestClient(app, client=PEER)
    monkeypatch.setattr(agents_router, "start_deploy_async", lambda jid: None)
    monkeypatch.setattr(agents_router, "teardown_agent_row", _fake_teardown)
    monkeypatch.setattr(chat_router, "stop_agent_session",
                        lambda agent, sid, workspace: {"ended": True, "already_ended": False})
    monkeypatch.setattr(canary_routers.canary_service, "stage_not_ready_reason",
                        lambda *a: "not now")
    monkeypatch.setattr(aws_clients, "client", lambda service, ctx, **kw: FakeS3())
    monkeypatch.setattr(aws_clients, "get_session", lambda *a, **k: pytest.fail("session"))
    knowledge_backend.set_backend(_FakeKnowledgeBackend())
    monkeypatch.setattr(memory_backend, "get_backend", lambda: _FakeMemoryBackend())
    set_default_resources({"memory_id": MEM_DEFAULT,
                           "artifacts_bucket": "launchpad-artifacts-test"})
    legacy_agent = _add(Agent(
        name="legacy-agent", method="zip_runtime", status="active", created_by=None,
        arn=ARN.format(rid="legacy_abcdefghij"), resource_id="legacy_abcdefghij",
        spec={"name": "legacy-agent", "method": "zip_runtime", "system_prompt": "p",
              "protocol": "http"},
    ))
    yield SimpleNamespace(
        app=app, cell=0, revision=None, name=None, mode="shared",
        clients={"anon": anon, "member": ada, "member-other": bob, "admin": carol,
                 "config-admin": admin},
        ada_id=ada_id, bob_id=bob_id, carol_id=carol_id, legacy_agent=legacy_agent,
    )
    knowledge_backend.set_backend(None)


# ─── kinds: seed + request builders (every seed is unique per cell: `w.cell`) ──


def _agent_spec(name: str, prompt: str = "p") -> dict[str, Any]:
    return {"name": name, "method": "zip_runtime", "system_prompt": prompt}


def _seed_agent(w) -> str:
    w.name = f"ada-agent-{w.cell}"
    rid = f"ada_agent_{w.cell}_abcdefghij"
    return _add(Agent(
        name=w.name, method="zip_runtime", status="active", created_by=w.ada_id,
        created_by_name="ada", arn=ARN.format(rid=rid), resource_id=rid,
        spec={**_agent_spec(w.name), "protocol": "http"},
    ))


def _seed_session(w) -> str:
    sid = f"sess-ada-matrix-{w.cell:04d}"
    _add(ChatSession(agent_id=w.legacy_agent, session_id=sid, actor_id="ada",
                     created_by=w.ada_id, created_by_name="ada", turns=1))
    _add(ChatMessage(agent_id=w.legacy_agent, session_id=sid, role="user", text="hi"))
    return sid


def _seed_key(w) -> str:
    return _add(ApiKey(name="ada-key", prefix="lp_live_ada…",
                       key_hash=hash_key(f"lp_live_{w.cell:032d}"),
                       created_by=w.ada_id, created_by_name="ada"))


def _seed_dataset(w) -> str:
    return _add(EvalDataset(name="ds", kind="legacy", items=[{"prompt": "hi"}],
                            created_by=w.ada_id, created_by_name="ada"))


def _seed_evaluator(w) -> str:
    return _add(EvalEvaluator(name=f"ada_judge_{w.cell}", level="SESSION", definition="judge",
                              instructions="Rate the {context}", rating_scale=RATING,
                              model_id="judge", created_by=w.ada_id, created_by_name="ada"))


def _seed_run(w) -> str:
    return _add(EvalRun(agent_id=w.legacy_agent, agent_name="legacy-agent", status="failed",
                        mode="evaluators", created_by=w.ada_id, created_by_name="ada"))


def _seed_canary(w) -> str:
    return _add(RuntimeCanary(name="c", champion_agent_id=w.legacy_agent,
                              champion_agent_name="legacy-agent",
                              challenger_agent_id=w.legacy_agent,
                              challenger_agent_name="legacy-agent", status="completed",
                              created_by=w.ada_id, created_by_name="ada"))


def _seed_online_config(w) -> str:
    config_id = f"oe_ada_{w.cell}"
    _add(OnlineEvalConfig(
        workspace_id=DEFAULT_WORKSPACE_ID, agent_id=w.legacy_agent, agent_name="legacy-agent",
        config_id=config_id, config_arn=f"arn:{config_id}", name=f"oe_ada_agent_{w.cell}",
        service_name="legacy_abcdefghij.DEFAULT",
        log_group="/aws/bedrock-agentcore/runtimes/legacy_abcdefghij-DEFAULT",
        evaluators=["Builtin.Helpfulness"], sampling_percentage=5.0,
        session_timeout_minutes=15, created_by=w.ada_id, created_by_name="ada"))
    return config_id


def _seed_kb(w) -> str:
    kb_id = f"KBADA{w.cell:04d}"
    return _add(KnowledgeBase(kb_id=kb_id, name=kb_id, status="ACTIVE",
                              created_by=w.ada_id, created_by_name="ada"))


def _seed_memory(w) -> str:
    memory_id = f"mem_ada_{w.cell}-AAAA"
    _add(ManagedMemory(memory_id=memory_id, origin="created", created_by=w.ada_id,
                       created_by_name="ada"))
    return memory_id


def _seed_user(w) -> str:
    user_id, _client = _member(w.app, f"dave{w.cell}")  # the account the cell acts on
    return user_id


def _seed_workspace(w) -> str:
    return DEFAULT_WORKSPACE_ID


def _seed_video(w) -> str:
    res = w.clients["config-admin"].post("/api/videos/manage", json=VIDEO)
    assert res.status_code == 201, res.text
    w.revision = res.json()["revision"]
    return res.json()["id"]


def _seed_announcement(w) -> str:
    res = w.clients["config-admin"].post("/api/announcements", json=ANNOUNCEMENT)
    assert res.status_code == 201, res.text
    w.revision = res.json()["revision"]
    return res.json()["id"]


def _seed_policy(w) -> str:
    """Global model: a policy belongs to an engine — the seed makes one per cell and keeps
    its id on the world for the request builders."""
    eng = w.clients["member"].post("/api/policy-engines", json={"name": f"ada_engine_{w.cell}"})
    assert eng.status_code == 201, eng.text
    w.engine_id = eng.json()["id"]
    res = w.clients["member"].post(f"/api/policy-engines/{w.engine_id}/policies",
                                  json={"name": f"ada_policy_{w.cell}", "statement": BASELINE})
    assert res.status_code == 201, res.text
    return res.json()["id"]


def _get(path: str) -> Builder:
    return lambda w, rid: ("GET", path.format(id=rid), None, None)


def _req(method: str, path: str, body: Any = None,
         query: Callable[[Any], dict[str, Any]] | None = None) -> Builder:
    return lambda w, rid: (method, path.format(id=rid), body, query(w) if query else None)


def _revision(w) -> dict[str, Any]:
    return {"expected_revision": w.revision}


KINDS: list[Kind] = [
    Kind("agents", _seed_agent, {
        "list": _get("/api/agents"),
        "get": _get("/api/agents/{id}"),
        "create": lambda w, rid: ("POST", "/api/agents", _agent_spec(f"matrix-new-{w.cell}"),
                                  None),
        "update": lambda w, rid: ("POST", f"/api/agents/{rid}/redeploy",
                                  _agent_spec(w.name, "p2"), None),
        "delete": _req("DELETE", "/api/agents/{id}"),
    }, _ids("agents")),
    Kind("chat_sessions", _seed_session, {
        "list": lambda w, rid: ("GET", f"/api/chat/{w.legacy_agent}/sessions", None, None),
        "get": lambda w, rid: ("GET", f"/api/chat/{w.legacy_agent}/history", None,
                               {"session_id": rid}),
        "create": lambda w, rid: ("POST", f"/api/chat/{w.legacy_agent}", {}, None),
        "update": lambda w, rid: ("POST", f"/api/chat/{w.legacy_agent}/sessions/{rid}/stop",
                                  None, None),
    }, _ids("sessions", "session_id")),
    Kind("api_keys", _seed_key, {
        "list": _get("/api/apikeys"),
        "create": _req("POST", "/api/apikeys", {"name": "k"}),
        "update": _req("POST", "/api/apikeys/{id}/disable"),
        "delete": _req("DELETE", "/api/apikeys/{id}"),
    }, _ids("keys")),
    Kind("datasets", _seed_dataset, {
        "list": _get("/api/eval/datasets"),  # there is no by-id GET for datasets
        "create": _req("POST", "/api/eval/datasets",
                       {"name": "golden", "items": [{"prompt": "hi"}]}),
        "update": _req("PUT", "/api/eval/datasets/{id}", {"description": "x"}),
        "delete": _req("DELETE", "/api/eval/datasets/{id}"),
    }, _ids("datasets")),
    Kind("evaluators", _seed_evaluator, {
        "list": _get("/api/eval/evaluators"),
        "get": _get("/api/eval/evaluators/{id}"),
        "create": lambda w, rid: ("POST", "/api/eval/evaluators", {
            "name": f"my_judge_{w.cell}", "instructions": "Rate the tone of {context}",
            "level": "SESSION", "description": "checks tone", "rating_scale": RATING}, None),
        "update": _req("PUT", "/api/eval/evaluators/{id}",
                       {"instructions": "Rate the tone of {context} again", "level": "SESSION"}),
        "delete": _req("DELETE", "/api/eval/evaluators/{id}"),
    }, _ids("evaluators")),
    Kind("runs", _seed_run, {
        "list": _get("/api/eval/runs"),
        "get": _get("/api/eval/runs/{id}"),
        "create": _req("POST", "/api/eval/runs", {}),  # probe: 422 proves the gate passed
        "update": _req("POST", "/api/eval/runs/{id}/stop"),  # a failed run: 409 run.not_active
        "delete": _req("DELETE", "/api/eval/runs/{id}"),
    }, _ids("runs")),
    Kind("canaries", _seed_canary, {
        "list": _get("/api/runtime-canaries"),
        "get": _get("/api/runtime-canaries/{id}"),
        "create": _req("POST", "/api/runtime-canaries", {}),
        "update": _req("POST", "/api/runtime-canaries/{id}/action", {"action": "cleanup"}),
    }, _ids("canaries")),
    # N17-obs426: online evaluation configs (edit / delete, and pause / resume below)
    Kind("online_configs", _seed_online_config, {
        "list": _get("/api/eval/online"),
        "get": _get("/api/eval/online/{id}"),
        "update": _req("PATCH", "/api/eval/online/{id}", {"description": "x"}),
        "delete": _req("DELETE", "/api/eval/online/{id}"),
    }, _ids("configs", "config_id")),
    Kind("online_configs_pause", _seed_online_config, {
        "update": _req("POST", "/api/eval/online/{id}/pause"),
    }),
    Kind("online_configs_resume", _seed_online_config, {
        "update": _req("POST", "/api/eval/online/{id}/resume"),
    }),
    Kind("knowledge_bases", _seed_kb, {
        "list": _get("/api/knowledge-bases"),
        "get": _get("/api/knowledge-bases/{id}"),
        "create": lambda w, rid: ("POST", "/api/knowledge-bases", {"name": f"kbnew{w.cell}"},
                                  None),
        "update": _req("PATCH", "/api/knowledge-bases/{id}", {"description": "x"}),
        "delete": _req("DELETE", "/api/knowledge-bases/{id}"),
    }, _ids("items", "kb_id")),
    Kind("memories", _seed_memory, {
        "list": _get("/api/memory/resources"),
        "get": _get("/api/memory/resources/{id}"),
        "create": lambda w, rid: ("POST", "/api/memory/resources",
                                  {"name": f"fresh_notes_{w.cell}", "strategies": []}, None),
        "update": _req("PUT", "/api/memory/resources/{id}", {"description": "x"}),
        "delete": _req("DELETE", "/api/memory/resources/{id}"),
    }, _ids("items")),
    Kind("users", _seed_user, {
        "list": _get("/api/users"),
        "create": lambda w, rid: ("POST", "/api/auth/register", {
            "username": f"newbie{w.cell}", "email": f"newbie{w.cell}@acme-corp.com",
            "password": "sufficient-pass"}, None),
        "update": _req("PATCH", "/api/users/{id}", {"status": "active"}),
        "delete": _req("DELETE", "/api/users/{id}"),
    }, _ids("items")),
    Kind("workspace_visibility", _seed_workspace, {
        "get": _get("/api/workspaces/{id}/settings/visibility"),
        "update": lambda w, rid: ("PUT", f"/api/workspaces/{rid}/settings/visibility",
                                  {"resource_visibility": w.mode}, None),
    }),
    Kind("videos", _seed_video, {
        "list": _get("/api/videos/manage"),
        "get": _get("/api/videos/manage/{id}"),
        "create": _req("POST", "/api/videos/manage", VIDEO),
        "update": lambda w, rid: ("PUT", f"/api/videos/manage/{rid}",
                                  {**VIDEO, "expected_revision": w.revision}, None),
        "delete": _req("DELETE", "/api/videos/manage/{id}", query=_revision),
    }, _any_ids),
    Kind("videos_feed", _seed_video, {"list": _get("/api/videos")}, _any_ids),
    Kind("announcements", _seed_announcement, {
        "list": _get("/api/announcements/manage"),
        "get": _get("/api/announcements/{id}"),
        "create": _req("POST", "/api/announcements", ANNOUNCEMENT),
        "update": lambda w, rid: ("PUT", f"/api/announcements/{rid}",
                                  {**ANNOUNCEMENT, "expected_revision": w.revision}, None),
        "delete": _req("DELETE", "/api/announcements/{id}", query=_revision),
    }, _any_ids),
    Kind("announcements_feed", _seed_announcement, {"list": _get("/api/announcements")},
         _any_ids),
    Kind("policies", _seed_policy, {
        "list": lambda w, rid: ("GET", f"/api/policy-engines/{w.engine_id}/policies", None, None),
        "get": lambda w, rid: ("GET", f"/api/policy-engines/{w.engine_id}/policies/{rid}", None,
                               None),
        "create": lambda w, rid: ("POST", f"/api/policy-engines/{w.engine_id}/policies",
                                  {"name": f"p_{w.cell}", "statement": BASELINE}, None),
        "update": lambda w, rid: ("PUT", f"/api/policy-engines/{w.engine_id}/policies/{rid}",
                                  {"description": "x"}, None),
        "delete": lambda w, rid: ("DELETE", f"/api/policy-engines/{w.engine_id}/policies/{rid}",
                                  None, None),
    }, _ids("policies")),
]
BY_NAME = {k.name: k for k in KINDS}

# ─── THE TABLE ───────────────────────────────────────────────────────────────
# status per role; a tuple is (shared, isolated). Column order = ROLES:
#   anon  member  member-other  admin  config-admin
UNAUTH = 401
HIDDEN = (200, 404)  # visible under shared, the kind's not-found under isolated

# a workspace-visibility kind with no creator gate on its mutations (runs, KBs, memories):
# everyone with a session may act on what they can see. Datasets, evaluators and canary
# actions became creator-or-admin in r13-g (users-and-ownership §8) — see their rows.
VISIBLE_RW = {
    "list": (UNAUTH, 200, 200, 200, 200),
    "get": (UNAUTH, 200, HIDDEN, 200, 200),
    "update": (UNAUTH, 200, HIDDEN, 200, 200),
    "delete": (UNAUTH, 200, HIDDEN, 200, 200),
}
# hub-global administration (route_policy ADMIN): members are refused outright
ADMIN_ONLY_ROW = (UNAUTH, 403, 403, 200, 200)


def _admin_only(*ops: str) -> dict[str, tuple]:
    return dict.fromkeys(ops, ADMIN_ONLY_ROW)


def _only(table: dict[str, tuple], *ops: str) -> dict[str, tuple]:
    return {op: table[op] for op in ops}


EXPECTED: dict[str, dict[str, tuple]] = {
    "agents": {
        **VISIBLE_RW,
        "create": (UNAUTH, 202, 202, 202, 202),  # perm:agents.deploy, default granted
        # creator-or-admin (§4): a stranger is 403 agent.forbidden, or 404 once hidden
        "update": (UNAUTH, 202, (403, 404), 202, 202),
        "delete": (UNAUTH, 200, (403, 404), 200, 200),
    },
    "chat_sessions": {
        "list": (UNAUTH, 200, 200, 200, 200),  # own sessions only (both modes) — see LISTED
        "get": (UNAUTH, 200, 404, 200, 200),  # another member's history reads as missing
        "create": (UNAUTH, 422, 422, 422, 422),  # an empty prompt is refused after the gate
        # N16-obs70: another member's session is 404 by id in BOTH modes (a member used
        # to be able to end an administrator's live session under shared)
        "update": (UNAUTH, 200, 404, 200, 200),
    },
    "api_keys": {
        "list": (UNAUTH, 200, 200, 200, 200),  # own keys only (both modes) — see LISTED
        "create": (UNAUTH, 201, 201, 201, 201),
        "update": (UNAUTH, 200, 404, 200, 200),  # a key the caller may not manage is 404
        "delete": (UNAUTH, 200, 404, 200, 200),
    },
    "datasets": {
        "list": VISIBLE_RW["list"],
        "create": (UNAUTH, 201, 201, 201, 201),
        # creator-or-admin (§8, r13-g): a stranger is 403 dataset.forbidden, or 404 once hidden
        "update": (UNAUTH, 200, (403, 404), 200, 200),
        "delete": (UNAUTH, 200, (403, 404), 200, 200),
    },
    "evaluators": {
        **_only(VISIBLE_RW, "list", "get"),
        "create": (UNAUTH, 201, 201, 201, 201),
        # creator-or-admin (§8, r13-g): 403 evaluator.forbidden, or 404 once hidden
        "update": (UNAUTH, 200, (403, 404), 200, 200),
        "delete": (UNAUTH, 200, (403, 404), 200, 200),
    },
    "runs": {
        **VISIBLE_RW,
        "create": (UNAUTH, 422, 422, 422, 422),  # perm:eval.run default granted; empty body
        "update": (UNAUTH, 409, (409, 404), 409, 409),  # stop of a failed run: run.not_active
    },
    "canaries": {
        "list": (UNAUTH, 200, 200, 200, 200),
        "get": (UNAUTH, 200, HIDDEN, 200, 200),
        "create": (UNAUTH, 422, 422, 422, 422),
        # the ownership gate (§8, r13-g) answers before the stage check: a stranger is 403
        # canary.forbidden (404 once hidden); the creator / admin reach the 409 stage refusal
        "update": (UNAUTH, 409, (403, 404), 409, 409),
    },
    # creator-or-admin (N17-obs426, §8): a stranger is 403 online_config.forbidden — under
    # shared a member could pause / edit / delete an administrator's config — or 404 once
    # hidden
    "online_configs": {
        **_only(VISIBLE_RW, "list", "get"),
        "update": (UNAUTH, 200, (403, 404), 200, 200),
        "delete": (UNAUTH, 200, (403, 404), 200, 200),
    },
    "online_configs_pause": {"update": (UNAUTH, 200, (403, 404), 200, 200)},
    "online_configs_resume": {"update": (UNAUTH, 200, (403, 404), 200, 200)},
    # creator-or-admin (N17-obs530, §8): a stranger is 403 auth.forbidden on PATCH / DELETE
    # (shared) or 404 once hidden; query and incremental sync stay open to every viewer
    "knowledge_bases": {
        **_only(VISIBLE_RW, "list", "get"),
        "create": (UNAUTH, 202, 202, 202, 202),
        "update": (UNAUTH, 200, (403, 404), 200, 200),
        "delete": (UNAUTH, 200, (403, 404), 200, 200),
    },
    "memories": {**VISIBLE_RW, "create": (UNAUTH, 201, 201, 201, 201)},  # perm:memory.manage
    "users": {
        **_admin_only("list", "update", "delete"),
        "create": (201, 201, 201, 201, 201),  # POST /api/auth/register is PUBLIC
    },
    "workspace_visibility": _admin_only("get", "update"),
    "videos": {**_admin_only("list", "get", "update", "delete"),
               "create": (UNAUTH, 403, 403, 201, 201)},
    "videos_feed": {"list": (UNAUTH, 200, 200, 200, 200)},  # published snapshots, members
    "announcements": {**_admin_only("list", "get", "update", "delete"),
                      "create": (UNAUTH, 403, 403, 201, 201)},
    "announcements_feed": {"list": (UNAUTH, 200, 200, 200, 200)},
    "policies": {  # shared in both modes (§7); perm:policy.manage default granted
        "list": (UNAUTH, 200, 200, 200, 200),
        "get": (UNAUTH, 200, 200, 200, 200),
        "create": (UNAUTH, 201, 201, 201, 201),
        "update": (UNAUTH, 200, 200, 200, 200),
        "delete": (UNAUTH, 200, 200, 200, 200),
    },
}

# whether the seeded row (created by `member`) appears in the role's list — a tuple is
# (shared, isolated); roles whose list is refused are not listed here
OWN_ONLY = {"member": True, "member-other": False, "admin": True, "config-admin": True}
ISOLATABLE = {"member": True, "member-other": (True, False), "admin": True, "config-admin": True}
LISTED: dict[str, dict[str, bool | tuple[bool, bool]]] = {
    "agents": ISOLATABLE, "datasets": ISOLATABLE, "evaluators": ISOLATABLE, "runs": ISOLATABLE,
    "canaries": ISOLATABLE, "knowledge_bases": ISOLATABLE, "online_configs": ISOLATABLE,
    "memories": ISOLATABLE,
    "chat_sessions": OWN_ONLY, "api_keys": OWN_ONLY,
    "users": {"admin": True, "config-admin": True},
    "videos": {"admin": True, "config-admin": True},
    "announcements": {"admin": True, "config-admin": True},
    # a draft is not published: the member feed does not carry it
    "videos_feed": dict.fromkeys(OWN_ONLY, False),
    "announcements_feed": dict.fromkeys(OWN_ONLY, False),
    "policies": dict.fromkeys(OWN_ONLY, True),
}

# cells where the running code and the spec above disagree: kept as the spec, listed here
# with the reason, so the gate stays green and the disagreement stays visible (N12-obs58).
# A listed cell that starts to AGREE fails the kind's test until it is removed (strict).
DISAGREEMENTS: dict[tuple[str, str, str, str], str] = {}


def _pick(value: Any, mode: str) -> Any:
    return value[MODES.index(mode)] if isinstance(value, tuple) else value


def _cells(kind: Kind):
    for mode in MODES:
        for op in OPS:
            if op in kind.ops:
                for role in ROLES:
                    yield mode, op, role


@pytest.mark.parametrize("kind", KINDS, ids=[k.name for k in KINDS])
def test_every_cell_of_the_kind_answers_as_the_spec_says(world, kind):
    mismatches: list[str] = []
    for mode, op, role in _cells(kind):
        world.cell += 1
        world.mode = mode
        _isolate(value=visibility.SHARED if mode == "shared" else visibility.ISOLATED)
        rid = kind.seed(world)
        method, path, body, query = kind.ops[op](world, rid)
        res = world.clients[role].request(method, path, json=body, params=query)
        expected = _pick(EXPECTED[kind.name][op][ROLES.index(role)], mode)
        problems: list[str] = []
        if res.status_code != expected:
            problems.append(f"expected {expected}, got {res.status_code} {res.text[:200]}")
        elif op == "list" and res.status_code == 200 and role in LISTED.get(kind.name, {}):
            want = _pick(LISTED[kind.name][role], mode)
            if (rid in kind.list_ids(res.json())) is not want:
                problems.append(f"row {'not ' if want else ''}listed: {res.text[:200]}")
        key = (kind.name, op, role, mode)
        label = "-".join(key) + f" ({method} {path})"
        if key in DISAGREEMENTS:
            if not problems:
                mismatches.append(f"{label}: agrees with the spec now — remove it from "
                                  "DISAGREEMENTS")
        elif problems:
            mismatches.append(f"{label}: " + "; ".join(problems))
    assert not mismatches, f"{len(mismatches)} cell(s) off the spec:\n" + "\n".join(mismatches)


def test_the_table_covers_every_operation_and_only_known_roles():
    for kind in KINDS:
        assert set(kind.ops) == set(EXPECTED[kind.name]), kind.name
        for op, row in EXPECTED[kind.name].items():
            assert op in OPS and len(row) == len(ROLES), (kind.name, op)
    for name, roles in LISTED.items():
        assert "list" in BY_NAME[name].ops, name
        assert set(roles) <= set(ROLES) - {"anon"}, name
    for kind_name, op, role, mode in DISAGREEMENTS:
        assert op in BY_NAME[kind_name].ops and role in ROLES and mode in MODES


def test_anonymous_refusals_carry_the_session_error(world):
    """The 401 cells are the auth middleware's one answer, not a handler's."""
    res = world.clients["anon"].get("/api/agents")
    assert res.status_code == 401 and res.json()["code"].startswith("auth.")


def test_matrix_size_is_what_the_doc_says():
    """``docs/cn/interfaces/testing.md`` quotes the matrix size; keep them in step."""
    cells = sum(len(k.ops) for k in KINDS) * len(ROLES) * len(MODES)
    assert len(KINDS) == 19 and cells == 700, (len(KINDS), cells)
