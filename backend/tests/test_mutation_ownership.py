"""Creator-or-admin on the mutations that used to be plain MEMBER (r13-g, N13-obs15;
acceptance rulings E13 NC-16, E15 NC-15, E09 / E10 ownership).

`services/ownership.require_creator_or_admin` — the rule `require_agent_creator`
established for agents — now gates: every runtime-canary action except `verdict`;
Skill Lab job cancel / resume / publish / delete, task set PUT / DELETE and an expansion
applied to a task set; dataset PUT / DELETE; custom evaluator PUT / DELETE. For each
path: the creator passes, another member answers 403 `<kind>.forbidden` (naming the row
and its creator) before any side effect, an administrator passes, and a NULL-creator row
is an administrator's (the CN rule of rev-r11 — a member is refused). Reads stay shared;
a hidden row (workspace isolation) still answers the kind's 404 first.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from tests import test_visibility as tv
from tests.test_visibility import (
    _active_agent,
    _dataset,
    _evaluator,
    _isolate,
    _job,
    _member,
    _taskset,
)

app, admin, ada, bob = tv.app, tv.admin, tv.ada, tv.bob


def _forbidden(res, code: str, **detail) -> None:
    assert res.status_code == 403, res.text
    body = res.json()
    assert body["code"] == code, body
    for key, value in detail.items():
        assert body["detail"][key] == value, body["detail"]


def _canary(created_by, agent_id, **kw):
    from app.optimization.models import RuntimeCanary

    return tv._add(RuntimeCanary(
        name="c", champion_agent_id=agent_id, challenger_agent_id=agent_id,
        champion_agent_name="agent", challenger_agent_name="agent",
        created_by=created_by, created_by_name="someone" if created_by else None,
        stage="setup", artifacts={"setup": {"ab_test_id": "t"}}, **kw,
    ))


# ─── the shared helper ───────────────────────────────────────────────────────


def test_helper_rule_admin_creator_other_and_null_creator():
    from types import SimpleNamespace

    from app.core.errors import AppError
    from app.routers.auth import Identity
    from app.services import ownership

    row = SimpleNamespace(id="r1", created_by="u-ada", created_by_name="ada")
    ada_id = Identity(username="ada", role="member", user_id="u-ada")
    bob_id = Identity(username="bob", role="member", user_id="u-bob")
    root = Identity(username="operator", role="admin")  # the row-less config admin
    assert ownership.may_mutate(row, ada_id)
    assert ownership.may_mutate(row, root)
    assert not ownership.may_mutate(row, bob_id)
    assert not ownership.may_mutate(row, None)
    with pytest.raises(AppError) as exc:
        ownership.require_creator_or_admin(
            row, bob_id, "thing.forbidden", detail={"thing_id": "r1"})
    assert exc.value.status_code == 403
    assert exc.value.code == "thing.forbidden"
    assert exc.value.detail == {"created_by": "u-ada", "created_by_name": "ada", "thing_id": "r1"}
    # NULL creator → administrator-only (a member never owns an unknown creator's row)
    legacy = SimpleNamespace(id="r2", created_by=None, created_by_name=None)
    assert not ownership.may_mutate(legacy, ada_id)
    assert ownership.may_mutate(legacy, root)


# ─── runtime canaries ────────────────────────────────────────────────────────


@pytest.fixture
def canary_world(monkeypatch, admin, ada, bob):
    from app.optimization import canary_routers

    dispatched: list[tuple[str, str]] = []
    monkeypatch.setattr(canary_routers.canary_service, "stage_not_ready_reason", lambda *a: None)
    monkeypatch.setattr(canary_routers.canary_service, "assert_verdict_allows",
                        lambda *a, **k: None)
    monkeypatch.setattr(canary_routers.canary_service, "assert_setup_available", lambda *a: None)
    # N17-obs452's cleanup-decision gate has its own suite (test_canary_cleanup_guard.py)
    monkeypatch.setattr(canary_routers.canary_service, "refuse_cleanup_before_decision",
                        lambda *a: None)
    monkeypatch.setattr(
        canary_routers.canary_service, "run_action",
        lambda canary_id, action, fn: dispatched.append((canary_id, action)),
    )
    monkeypatch.setattr(canary_routers, "_eligible_agent",
                        lambda db, ws, aid, viewer=None: MagicMock(method="zip_runtime", id=aid))
    ada_id, ada_client = ada
    bob_id, bob_client = bob
    agent = _active_agent("canary-agent")
    return {
        "admin": admin, "ada": ada_client, "bob": bob_client,
        "ada_can": _canary(ada_id, agent), "legacy_can": _canary(None, agent),
        "dispatched": dispatched,
    }


@pytest.mark.parametrize(
    "action", ["setup", "traffic", "advance", "complete", "rollback", "cleanup"])
def test_canary_mutating_actions_are_creator_or_admin(canary_world, action):
    w = canary_world
    body = {"action": action}
    if action == "traffic":
        body["dataset_id"] = _dataset(None)
    url = f"/api/runtime-canaries/{w['ada_can']}/action"
    _forbidden(w["bob"].post(url, json=body), "canary.forbidden",
               canary_id=w["ada_can"], action=action, created_by_name="someone")
    assert w["dispatched"] == []  # nothing ran
    assert w["ada"].post(url, json=body).status_code == 202
    assert w["admin"].post(url, json=body).status_code == 202
    assert [a for _c, a in w["dispatched"]] == [action, action]


def test_canary_verdict_stays_open_to_every_member_who_sees_it(canary_world):
    w = canary_world
    res = w["bob"].post(f"/api/runtime-canaries/{w['ada_can']}/action", json={"action": "verdict"})
    assert res.status_code == 202, res.text
    assert [a for _c, a in w["dispatched"]] == ["verdict"]


def test_canary_null_creator_is_an_administrators(canary_world):
    w = canary_world
    url = f"/api/runtime-canaries/{w['legacy_can']}/action"
    _forbidden(w["bob"].post(url, json={"action": "cleanup"}), "canary.forbidden",
               created_by=None, created_by_name=None)
    _forbidden(w["ada"].post(url, json={"action": "cleanup"}), "canary.forbidden")
    assert w["admin"].post(url, json={"action": "cleanup"}).status_code == 202


def test_canary_hidden_row_is_404_before_the_ownership_403(canary_world):
    w = canary_world
    _isolate()
    res = w["bob"].post(f"/api/runtime-canaries/{w['ada_can']}/action", json={"action": "cleanup"})
    assert res.status_code == 404 and res.json()["code"] == "canary.not_found"


def test_canary_ownership_precedes_stage_and_in_flight_checks(monkeypatch, canary_world):
    """A forbidden caller learns nothing about the canary's state."""
    from app.optimization import canary_routers

    w = canary_world
    monkeypatch.setattr(canary_routers.canary_service, "stage_not_ready_reason", lambda *a: "later")
    _forbidden(w["bob"].post(f"/api/runtime-canaries/{w['ada_can']}/action",
                             json={"action": "advance"}), "canary.forbidden")
    res = w["ada"].post(f"/api/runtime-canaries/{w['ada_can']}/action", json={"action": "advance"})
    assert res.status_code == 409 and res.json()["code"] == "canary.stage_not_ready"


# ─── Skill Lab ───────────────────────────────────────────────────────────────


@pytest.fixture
def lab_world(monkeypatch, admin, ada, bob):
    from app.skill_lab import jobs as jobs_svc
    from app.skill_lab import tasksets as tasksets_svc

    reached: list[str] = []
    for name in ("cancel_job", "delete_job", "resume_job", "publish_job",
                 "apply_taskgen_expansion"):
        monkeypatch.setattr(jobs_svc, name, lambda *a, _n=name, **k: reached.append(_n) or {})
    for name in ("update_taskset", "delete_taskset"):
        monkeypatch.setattr(tasksets_svc, name, lambda *a, _n=name, **k: reached.append(_n) or {})
    ada_id, ada_client = ada
    bob_id, bob_client = bob
    ada_ts, legacy_ts = _taskset(ada_id), _taskset(None)
    return {
        "admin": admin, "ada": ada_client, "bob": bob_client, "reached": reached,
        "ada_ts": ada_ts, "legacy_ts": legacy_ts,
        "ada_job": _job(ada_id, taskset_id=ada_ts), "legacy_job": _job(None, taskset_id=legacy_ts),
        # bob's expansion job targeting ADA's task set: the target's creator decides
        "bob_job_on_ada_ts": _job(bob_id, taskset_id=ada_ts),
    }


_JOB_MUTATIONS = [
    ("POST", "/cancel", None, "cancel_job"),
    ("POST", "/resume", None, "resume_job"),
    ("POST", "/publish", {"reapprove": False}, "publish_job"),
    ("DELETE", "", None, "delete_job"),
]


@pytest.mark.parametrize("method,suffix,body,service", _JOB_MUTATIONS)
def test_skill_lab_job_mutations_are_creator_or_admin(lab_world, method, suffix, body, service):
    w = lab_world
    url = f"/api/skill-lab/jobs/{w['ada_job']}{suffix}"
    _forbidden(w["bob"].request(method, url, json=body), "skill_lab.forbidden",
               job_id=w["ada_job"], created_by_name=None)
    assert w["reached"] == []
    assert w["ada"].request(method, url, json=body).status_code == 200
    assert w["admin"].request(method, url, json=body).status_code == 200
    assert w["reached"] == [service, service]
    # a NULL-creator job is an administrator's
    legacy = f"/api/skill-lab/jobs/{w['legacy_job']}{suffix}"
    _forbidden(w["ada"].request(method, legacy, json=body), "skill_lab.forbidden", created_by=None)
    assert w["admin"].request(method, legacy, json=body).status_code == 200


@pytest.mark.parametrize("method,body,service", [
    ("PUT", {"name": "x", "tasks_by_split": {"tasks": []}}, "update_taskset"),
    ("DELETE", None, "delete_taskset"),
])
def test_skill_lab_taskset_mutations_are_creator_or_admin(lab_world, method, body, service):
    w = lab_world
    url = f"/api/skill-lab/tasksets/{w['ada_ts']}"
    _forbidden(w["bob"].request(method, url, json=body), "skill_lab.forbidden",
               taskset_id=w["ada_ts"])
    assert w["reached"] == []
    assert w["ada"].request(method, url, json=body).status_code == 200
    assert w["admin"].request(method, url, json=body).status_code == 200
    assert w["reached"] == [service, service]
    legacy = f"/api/skill-lab/tasksets/{w['legacy_ts']}"
    _forbidden(w["ada"].request(method, legacy, json=body), "skill_lab.forbidden")
    assert w["admin"].request(method, legacy, json=body).status_code == 200


def test_skill_lab_expansion_is_gated_by_the_target_tasksets_creator(lab_world):
    w = lab_world
    url = f"/api/skill-lab/jobs/{w['bob_job_on_ada_ts']}/apply-expansion"
    # bob created the job, ada the task set it writes into: bob may not apply it
    _forbidden(w["bob"].post(url), "skill_lab.forbidden", taskset_id=w["ada_ts"], action="expand")
    assert w["reached"] == []
    # ada may (the job is visible to her under the shared default), and so may an admin
    assert w["ada"].post(url).status_code == 200
    assert w["admin"].post(url).status_code == 200
    assert w["reached"] == ["apply_taskgen_expansion", "apply_taskgen_expansion"]


def test_skill_lab_import_taskset_stays_open_as_it_creates_the_importers_set(
    monkeypatch, lab_world,
):
    from app.skill_lab import jobs as jobs_svc

    w = lab_world
    monkeypatch.setattr(jobs_svc, "import_taskgen_taskset",
                        lambda *a, **k: w["reached"].append("import_taskgen_taskset") or {})
    res = w["bob"].post(f"/api/skill-lab/jobs/{w['ada_job']}/import-taskset", json={"name": "mine"})
    assert res.status_code == 201, res.text
    assert w["reached"] == ["import_taskgen_taskset"]


def test_skill_lab_sample_taskset_keeps_its_own_refusal_for_administrators(monkeypatch, admin, bob):
    """A sample set has no creator: members hit the ownership 403, administrators fall
    through to the service's `skill_lab.sample_readonly` (unchanged)."""
    from app.core.errors import AppError
    from app.skill_lab import tasksets as tasksets_svc

    def refuse(*a, **k):
        raise AppError("skill_lab.sample_readonly", "sample", status_code=409)

    monkeypatch.setattr(tasksets_svc, "delete_taskset", refuse)
    sample = _taskset(None, sample=True)
    _bob_id, bob_client = bob
    _forbidden(bob_client.delete(f"/api/skill-lab/tasksets/{sample}"), "skill_lab.forbidden")
    res = admin.delete(f"/api/skill-lab/tasksets/{sample}")
    assert res.status_code == 409 and res.json()["code"] == "skill_lab.sample_readonly"


# ─── datasets ────────────────────────────────────────────────────────────────


def test_dataset_update_and_delete_are_creator_or_admin(admin, ada, bob, app):
    ada_id, ada_client = ada
    _bob_id, bob_client = bob
    theirs = _dataset(ada_id, "ada-ds")
    legacy = _dataset(None, "legacy-ds")
    carol_id, carol = _member(app, "carol", role="admin")
    url = f"/api/eval/datasets/{theirs}"
    # another member: refused, nothing changed (the row is still listed under its name)
    _forbidden(bob_client.put(url, json={"name": "hijacked"}), "dataset.forbidden",
               dataset_id=theirs, action="edit", created_by=ada_id, created_by_name="x")
    _forbidden(bob_client.delete(url), "dataset.forbidden", action="delete")
    names = {d["id"]: d["name"] for d in bob_client.get("/api/eval/datasets").json()["datasets"]}
    assert names[theirs] == "ada-ds"
    # the creator and administrators (config admin + an admin account) may
    assert ada_client.put(url, json={"name": "renamed"}).status_code == 200
    assert carol.put(url, json={"description": "by carol"}).status_code == 200
    assert admin.put(url, json={"description": "by root"}).status_code == 200
    # a NULL-creator dataset is an administrator's
    _forbidden(ada_client.put(f"/api/eval/datasets/{legacy}", json={"name": "y"}),
               "dataset.forbidden", created_by=None)
    _forbidden(ada_client.delete(f"/api/eval/datasets/{legacy}"), "dataset.forbidden")
    assert admin.put(f"/api/eval/datasets/{legacy}", json={"name": "y"}).status_code == 200
    assert ada_client.delete(url).json() == {"deleted": True}
    assert admin.delete(f"/api/eval/datasets/{legacy}").status_code == 200


def test_dataset_sync_and_publish_version_are_creator_or_admin(monkeypatch, admin, ada, bob):
    """Both rewrite the row's `cloud` blob and the shared cloud draft (rev-r11 finding,
    N13-obs15a): the creator and administrators may, another member answers 403 before
    the store is touched."""
    from tests.evaluation.test_datasets_v2 import fake_store

    s3 = fake_store(monkeypatch)
    ada_id, ada_client = ada
    _bob_id, bob_client = bob
    ds = ada_client.post("/api/eval/datasets", json={
        "name": "mine", "items": [{"prompt": "2+2?", "expected": "4"}],
    }).json()["id"]
    _forbidden(bob_client.post(f"/api/eval/datasets/{ds}/sync-to-aws"), "dataset.forbidden",
               dataset_id=ds, action="sync", created_by=ada_id)
    assert s3.objects == {}  # nothing reached the store
    synced = ada_client.post(f"/api/eval/datasets/{ds}/sync-to-aws")
    assert synced.status_code == 200, synced.text
    _forbidden(bob_client.post(f"/api/eval/datasets/{ds}/publish-version"), "dataset.forbidden",
               action="publish")
    assert ada_client.post(f"/api/eval/datasets/{ds}/publish-version").status_code == 200
    assert admin.post(f"/api/eval/datasets/{ds}/sync-to-aws").status_code == 200
    assert admin.post(f"/api/eval/datasets/{ds}/publish-version").status_code == 200
    legacy = _dataset(None, "legacy")
    _forbidden(ada_client.post(f"/api/eval/datasets/{legacy}/sync-to-aws"), "dataset.forbidden",
               created_by=None)


def test_cloud_store_mutations_need_every_pointing_row_mutable(monkeypatch, admin, ada, bob):
    """The store has no creator of its own: its owner is derived from the local rows that
    point at it, and every one of them gets its `cloud` blob rewritten — so the caller
    must be allowed to mutate each (403 names the first row they may not). A store no row
    points at stays open to every member who sees it (unchanged)."""
    from app.evaluation import routers as eval_routers
    from tests.test_visibility import _FakeStore

    ada_id, ada_client = ada
    bob_id, bob_client = bob
    store = _FakeStore(["cloud-ada", "cloud-shared", "cloud-outside", "cloud-bob"])
    monkeypatch.setattr(eval_routers.datasets_local, "store_for", lambda ctx: store)
    ada_row = _dataset(ada_id, "ada-ds", cloud={"dataset_id": "cloud-ada"})
    _dataset(ada_id, "ada-ds2", cloud={"dataset_id": "cloud-shared"})
    _dataset(bob_id, "bob-ds", cloud={"dataset_id": "cloud-shared"})
    _dataset(bob_id, "bob-only", cloud={"dataset_id": "cloud-bob"})
    for call in (
        lambda c: c.delete("/api/eval/datasets/cloud/cloud-ada"),
        lambda c: c.delete("/api/eval/datasets/cloud/cloud-ada/versions/1"),
        lambda c: c.post("/api/eval/datasets/cloud/cloud-ada/publish-version"),
        lambda c: c.delete("/api/eval/datasets/cloud/cloud-shared"),  # bob's row AND ada's
    ):
        res = call(bob_client)
        assert res.status_code == 403 and res.json()["code"] == "dataset.forbidden", res.text
        assert res.json()["detail"]["created_by"] == ada_id
    assert bob_client.delete("/api/eval/datasets/cloud/cloud-ada").json()["detail"] == {
        "created_by": ada_id, "created_by_name": "x", "cloud_id": "cloud-ada",
        "dataset_id": ada_row, "action": "delete",
    }
    assert store.deleted == []
    # ownership precedes the in-use 409: bob learns nothing about cloud-ada's runs
    tv._run(ada_id, status="queued", dataset_id="cloud-ada")
    assert bob_client.delete("/api/eval/datasets/cloud/cloud-ada").status_code == 403
    # the creator of every pointing row, an administrator, and a row-less store
    assert bob_client.delete("/api/eval/datasets/cloud/cloud-bob").status_code == 200
    assert bob_client.delete("/api/eval/datasets/cloud/cloud-outside").status_code == 200
    assert ada_client.post("/api/eval/datasets/cloud/cloud-ada/publish-version").status_code == 200
    assert admin.delete("/api/eval/datasets/cloud/cloud-shared").status_code == 200
    assert store.deleted == ["cloud-bob", "cloud-outside", "cloud-shared"]


def test_dataset_hidden_row_is_404_before_the_ownership_403(ada, bob):
    ada_id, _ = ada
    _bob_id, bob_client = bob
    theirs = _dataset(ada_id)
    _isolate()
    for res in (bob_client.put(f"/api/eval/datasets/{theirs}", json={"name": "x"}),
                bob_client.delete(f"/api/eval/datasets/{theirs}")):
        assert res.status_code == 404 and res.json()["code"] == "dataset.not_found", res.text


# ─── evaluators ──────────────────────────────────────────────────────────────


_JUDGE_UPDATE = {
    "instructions": "Rate the {context} again", "model_id": "judge", "level": "SESSION",
}


def test_evaluator_update_and_delete_are_creator_or_admin(monkeypatch, admin, ada, bob):
    from app.evaluation import routers as eval_routers

    # the judge update re-validates the model against the catalog / workspace resources —
    # not what this test is about
    monkeypatch.setattr(eval_routers.evaluators_local, "update_judge",
                        lambda db, row, **k: setattr(
                            row, "description", k.get("description") or ""))
    ada_id, ada_client = ada
    _bob_id, bob_client = bob
    theirs = _evaluator("ada_judge", ada_id)
    legacy = _evaluator("legacy_judge", None)
    url = f"/api/eval/evaluators/{theirs}"
    _forbidden(bob_client.put(url, json=_JUDGE_UPDATE), "evaluator.forbidden",
               evaluator_id=theirs, action="edit", created_by=ada_id)
    _forbidden(bob_client.delete(url), "evaluator.forbidden", action="delete")
    assert bob_client.get(url).status_code == 200  # reads stay shared
    assert ada_client.put(url, json=_JUDGE_UPDATE).status_code == 200
    assert admin.put(url, json=_JUDGE_UPDATE).status_code == 200
    _forbidden(ada_client.put(f"/api/eval/evaluators/{legacy}", json=_JUDGE_UPDATE),
               "evaluator.forbidden", created_by=None)
    _forbidden(ada_client.delete(f"/api/eval/evaluators/{legacy}"), "evaluator.forbidden")
    assert admin.put(f"/api/eval/evaluators/{legacy}", json=_JUDGE_UPDATE).status_code == 200
    assert ada_client.delete(url).json() == {"deleted": True}
    assert admin.delete(f"/api/eval/evaluators/{legacy}").status_code == 200


def test_evaluator_hidden_row_is_404_before_the_ownership_403(ada, bob):
    ada_id, _ = ada
    _bob_id, bob_client = bob
    theirs = _evaluator("hidden_judge", ada_id)
    _isolate()
    for res in (bob_client.put(f"/api/eval/evaluators/{theirs}", json=_JUDGE_UPDATE),
                bob_client.delete(f"/api/eval/evaluators/{theirs}")):
        assert res.status_code == 404 and res.json()["code"] == "evaluator.not_found", res.text


# ─── the console learns the ownership key ────────────────────────────────────


def test_auth_status_carries_the_callers_user_id(admin, bob):
    """The V2 console compares `created_by` against it to offer creator-only actions."""
    bob_id, bob_client = bob
    assert bob_client.get("/api/auth/status").json()["user_id"] == bob_id
    assert admin.get("/api/auth/status").json()["user_id"] is None  # the row-less config admin


# ─── local registry records (N15-obs7) ───────────────────────────────────────


def _record(created_by, name: str = "team-search", record_type: str = "MCP") -> str:
    """A local registry record registered by `created_by` (NULL = legacy / platform)."""
    from app.models.ledger import RegistryRecord
    from app.services.registry_backend import build_mcp_descriptors

    return tv._add(RegistryRecord(
        record_type=record_type, name=name, description="d", record_version="1.0.0-mcp",
        status="DRAFT", created_by=created_by, created_by_name="ada" if created_by else None,
        descriptors=build_mcp_descriptors(
            target=name, description="d", gateway_url="https://mcp.internal/mcp", tools=None
        ),
    ))


def test_registry_record_update_delete_and_reimport_are_creator_or_admin(admin, ada, bob, app):
    ada_id, ada_client = ada
    _bob_id, bob_client = bob
    theirs = _record(ada_id)
    legacy = _record(None, name="legacy-server")
    carol_id, carol = _member(app, "carol", role="admin")
    url = f"/api/registry/records/{theirs}"
    # another member: refused before any side effect; the record is still readable as it was
    _forbidden(bob_client.put(url, json={"description": "hijacked"}), "registry.forbidden",
               record_id=theirs, action="edit", created_by=ada_id, created_by_name="ada")
    _forbidden(bob_client.put(url, json={"url": "https://evil.example/mcp"}), "registry.forbidden")
    _forbidden(bob_client.delete(url), "registry.forbidden", action="delete")
    _forbidden(bob_client.post(f"{url}/reimport"), "registry.forbidden", action="reimport")
    shown = bob_client.get(url).json()
    assert shown["description"] == "d"
    assert shown["created_by"] == ada_id and shown["created_by_name"] == "ada"
    listed = bob_client.get("/api/registry/records").json()["records"]
    assert theirs in {r["record_id"] for r in listed}
    # the creator and administrators (config admin + an admin account) may
    assert ada_client.put(url, json={"description": "mine"}).status_code == 200
    assert carol.put(url, json={"description": "by carol"}).status_code == 200
    assert admin.put(url, json={"description": "by root"}).status_code == 200
    # ownership precedes the type / status checks: the creator reaches the 400, bob never does
    assert ada_client.post(f"{url}/reimport").json()["code"] == "registry.not_reimportable"
    # a NULL-creator record is an administrator's
    _forbidden(ada_client.put(f"/api/registry/records/{legacy}", json={"description": "y"}),
               "registry.forbidden", created_by=None, created_by_name=None)
    _forbidden(ada_client.delete(f"/api/registry/records/{legacy}"), "registry.forbidden")
    legacy_url = f"/api/registry/records/{legacy}"
    assert admin.put(legacy_url, json={"description": "y"}).status_code == 200
    assert ada_client.delete(url).json() == {"deleted": True, "record_id": theirs}
    assert admin.delete(f"/api/registry/records/{legacy}").status_code == 200


def test_registry_reads_stay_shared_in_both_modes_and_the_gate_holds(ada, bob):
    """Contract §7 is unchanged: an MCP / skill record is never hidden — in `isolated` the
    other member still reads it (200, named) and still answers 403, never 404, on PUT."""
    ada_id, _ada_client = ada
    _bob_id, bob_client = bob
    theirs = _record(ada_id, name="shared-server")
    url = f"/api/registry/records/{theirs}"
    for mode in (tv.visibility.SHARED, tv.visibility.ISOLATED):
        _isolate(value=mode)
        assert bob_client.get(url).json()["name"] == "shared-server", mode
        assert theirs in {
            r["record_id"] for r in bob_client.get("/api/registry/records").json()["records"]
        }, mode
        _forbidden(bob_client.put(url, json={"description": "x"}), "registry.forbidden")
        _forbidden(bob_client.delete(url), "registry.forbidden")


def test_registry_register_and_import_stamp_the_caller(ada, bob):
    """`POST /api/registry/records` records its caller (`registry_backend.current_creator`),
    so the creator may edit what they registered and another member may not."""
    ada_id, ada_client = ada
    _bob_id, bob_client = bob
    res = ada_client.post("/api/registry/records", json={
        "type": "MCP", "name": "ada-search", "description": "ada's", "url": "https://m.example/mcp",
    })
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["created_by"] == ada_id and body["created_by_name"] == "ada"
    url = f"/api/registry/records/{body['record_id']}"
    assert ada_client.put(url, json={"description": "edited"}).status_code == 200
    _forbidden(bob_client.put(url, json={"description": "x"}), "registry.forbidden",
               created_by=ada_id, created_by_name="ada")
    # the ContextVar never leaks past the request: a record created outside it has no creator
    from app.services import registry_backend

    assert registry_backend.current_creator.get() is None


def test_registry_lifecycle_actions_stay_the_review_workflow(ada, bob):
    """`POST …/action` is not creator-gated: any member may submit a record they can see
    (the N4 state machine); the decision itself — approve / publish / reject / disable — is
    an administrator's (N17-obs248, tests/test_registry_approval_admin_only.py), and the
    system-skill guard still owns presets."""
    ada_id, _ada_client = ada
    _bob_id, bob_client = bob
    theirs = _record(ada_id, name="to-review")
    res = bob_client.post(f"/api/registry/records/{theirs}/action", json={"action": "submit"})
    assert res.status_code == 200, res.text
    assert res.json()["status"] == "PENDING_APPROVAL"
    res = bob_client.post(f"/api/registry/records/{theirs}/action", json={"action": "approve"})
    assert res.status_code == 403 and res.json()["code"] == "registry.approval_admin_only"
