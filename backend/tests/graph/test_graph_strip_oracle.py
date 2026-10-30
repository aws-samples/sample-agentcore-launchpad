# ruff: noqa: F811  — pytest fixtures are imported by name from the sibling suites
"""Strip / unlink oracles on every force / delete seam of the self-hosted ports (round 4 of the
loop engine, r17-i — the N16-obs114 / N17-obs31 class: a delete that leaves a dangling pointer).

Every scenario is the contract's own sentence turned into a check on what the delete leaves
behind — never a threshold copied from code:

* KB `?force=true` (api.md Console Knowledge Bases API): "strips it from every mounted agent's
  spec first" — every mounting agent: the creator's active one, another member's, a `deploying`
  one (N17-obs31 fixed in cn/r17-h; a member's force is 403 `auth.forbidden`, N17-obs4);
* memory store delete (api.md Console Memory API): `409 memory.in_use` while a live agent pins it;
  once no live agent does, 200 and "the managed registration is dropped" (the id reads
  `404 memory.not_managed` afterwards);
* registry skill record delete (registry-backend.md): the ledger row goes — and the objects the
  registration uploaded under `skills/<name>/` should go with it; they do not (N16-obs35, open:
  a rediscovery, pinned strict as KNOWN);
* dataset / evaluator `?force=true` (evaluation-engine.md §22): an administrator may delete anyway,
  `forced: true`, "the referencing rows keep their dangling id" (a queued run keeps `dataset_id`;
  an online config keeps the evaluator id) — an explicit decision, not a cascade; a member's
  force is `403 auth.forbidden`.
"""

from __future__ import annotations

import uuid

from app.core.db import SessionLocal
from app.evaluation.models import EvalRun, OnlineEvalConfig
from app.models.ledger import Agent
from app.services import aws_clients, knowledge_backend, memory_backend
from tests.conftest import set_default_resources, ws_ctx
from tests.graph.test_graph_crossgraph import kbmem_fakes  # noqa: F401 — fixture by name
from tests.memory_fakes import FakeEmbedder, FakeOpenSearch, FakeS3, FakeSQS  # noqa: F401
from tests.test_memory_selfhosted import SUBSTRATE
from tests.test_registry_manage import FakeS3 as RegistryFakeS3
from tests.test_visibility import (  # noqa: F401 — fixtures + helpers by name
    _active_agent,
    _add,
    _dataset,
    _evaluator,
    _online_config,
    ada,
    admin,
    app,
    bob,
)


def _set(model, row_id: str, **fields) -> None:
    db = SessionLocal()
    try:
        row = db.get(model, row_id)
        for key, value in fields.items():
            setattr(row, key, value)
        db.commit()
    finally:
        db.close()


def _spec(agent_id: str) -> dict:
    db = SessionLocal()
    try:
        return dict(db.get(Agent, agent_id).spec or {})
    finally:
        db.close()


# ---------------------------------------------------------------------------
# knowledge base force
# ---------------------------------------------------------------------------


def test_kb_force_strips_every_mounting_agent(admin, ada, bob, kbmem_fakes):
    """api.md: `force=true` strips the KB from EVERY mounted agent's spec — the creator's active
    agent, another member's, a `deploying` one — before the row goes; the ledger scan's
    "pins knowledge base that no longer exists" never fires (N17-obs31 fixed in cn/r17-h)."""
    ada_id, ada_client = ada
    bob_id, bob_client = bob
    tag = uuid.uuid4().hex[:6]
    ws = ws_ctx(SUBSTRATE, region="cn-northwest-1", id="default")
    kb = knowledge_backend.get_backend().create_kb(ws, f"s-kb-{tag}", "", {"mode": "upload"})
    kb_id = kb["kb_id"]
    mounts = {}
    for who, creator, status in (("ada", ada_id, "active"), ("bob", bob_id, "active"),
                                 ("dep", ada_id, "deploying")):
        aid = _active_agent(f"s-kb-{who}-{tag}", created_by=creator)
        spec = _spec(aid)
        spec["knowledge_bases"] = [{"kb_id": kb_id}]
        _set(Agent, aid, spec=spec, status=status)
        mounts[who] = aid
    # a member's force is the administrator's override (N17-obs4)
    res = ada_client.delete(f"/api/knowledge-bases/{kb_id}?force=true")
    assert (res.status_code, res.json()["code"]) == (403, "auth.forbidden"), res.text
    # the plain delete 409s while agents mount it
    res = admin.delete(f"/api/knowledge-bases/{kb_id}")
    assert (res.status_code, res.json()["code"]) == (409, "kb.has_attached_agents"), res.text
    # api.md: "A mounting agent whose deploy job is queued / running refuses the force as a
    # whole with 409 agent.deploy_in_progress … and nothing is written" (agents.md §1)
    from tests.graph.executor import ledger_digest

    before = ledger_digest()
    res = admin.delete(f"/api/knowledge-bases/{kb_id}?force=true")
    assert (res.status_code, res.json()["code"]) == (409, "agent.deploy_in_progress"), res.text
    assert [a["id"] for a in res.json()["detail"]["agents"]] == [mounts["dep"]], res.text
    assert ledger_digest() == before, "the refused force wrote something"
    for who, aid in mounts.items():
        assert [k["kb_id"] for k in _spec(aid).get("knowledge_bases", [])] == [kb_id], who
    _set(Agent, mounts["dep"], status="active")  # the job ended: the force may proceed
    res = admin.delete(f"/api/knowledge-bases/{kb_id}?force=true")
    assert res.status_code == 200 and res.json()["deleted"] is True, res.text
    assert {a["id"] for a in res.json()["stripped_agents"]} == set(mounts.values()), res.json()
    for who, aid in mounts.items():
        assert [k["kb_id"] for k in _spec(aid).get("knowledge_bases", [])] == [], (who, _spec(aid))
    from tests.property.ledger_invariants import scan_ledger

    own = [v for v in scan_ledger(SessionLocal) if any(a in v for a in mounts.values())]
    assert own == [], own
    assert admin.get(f"/api/knowledge-bases/{kb_id}").status_code == 404


# ---------------------------------------------------------------------------
# memory store delete
# ---------------------------------------------------------------------------


def test_memory_store_delete_unlinks_only_after_the_last_live_pin(admin, ada, kbmem_fakes):
    """api.md Console Memory API: `409 memory.in_use` while a live agent's spec pins it; once the
    agent is deleted the delete is 200, the managed registration is dropped and the id reads
    `404 memory.not_managed` (api.md Ownership) — the store never outlives its registration."""
    ada_id, _ada_client = ada
    tag = uuid.uuid4().hex[:6]
    res = admin.post("/api/memory/resources", json={"name": f"s_mem_{tag}"})
    assert res.status_code == 201, res.text
    memory_id = res.json()["id"]
    aid = _active_agent(f"s-mem-{tag}", created_by=ada_id)
    spec = _spec(aid)
    spec["memory"] = {"memory_id": memory_id}
    _set(Agent, aid, spec=spec)
    res = admin.delete(f"/api/memory/resources/{memory_id}")
    assert (res.status_code, res.json()["code"]) == (409, "memory.in_use"), res.text
    assert {a["id"] for a in res.json()["detail"]["agents"]} == {aid}
    _set(Agent, aid, status="deleted")  # what the agent's DELETE leaves (mark, not remove)
    res = admin.delete(f"/api/memory/resources/{memory_id}")
    assert res.status_code == 200, res.text
    res = admin.get(f"/api/memory/resources/{memory_id}")
    assert (res.status_code, res.json()["code"]) == (404, "memory.not_managed"), res.text
    res = admin.delete(f"/api/memory/resources/{memory_id}")
    assert (res.status_code, res.json()["code"]) == (404, "memory.not_managed"), res.text
    assert memory_backend.get_backend() is not None


# ---------------------------------------------------------------------------
# registry skill record delete — N16-obs35 (open) rediscovered
# ---------------------------------------------------------------------------


def test_registry_skill_record_delete_removes_its_objects(client, monkeypatch):
    """registry-backend.md: a record's registration uploads `skills/<name>/SKILL.md`; the delete
    of the record removes what the registration created (the N16-obs114 / N17-obs31 unlink
    rule applied to the artifacts bucket). N16-obs35, fixed in cn/r17-j: the route sweeps the
    `skills/<name>/` prefix after the row delete when nothing else references it."""
    set_default_resources({"artifacts_bucket": "bkt", "registry_id": "local-default"})
    fake = RegistryFakeS3()
    monkeypatch.setattr(aws_clients, "client", lambda *a, **k: fake)
    name = f"strip-skill-{uuid.uuid4().hex[:6]}"
    res = client.post("/api/registry/records", json={
        "type": "AGENT_SKILLS", "name": name,
        "skill_md": f"---\nname: {name}\n---\n# {name}",
    })
    assert res.status_code == 201, res.text
    record_id = res.json()["record_id"]
    assert fake.uploaded == [f"skills/{name}/SKILL.md"]
    res = client.delete(f"/api/registry/records/{record_id}")
    assert res.status_code == 200 and res.json()["deleted"] is True, res.text
    assert client.get(f"/api/registry/records/{record_id}").status_code == 404
    assert f"skills/{name}/SKILL.md" in fake.deleted, (fake.deleted, "N16-obs35")
    assert res.json()["objects"] == {"objects_deleted": True, "prefix": f"skills/{name}/"}


# ---------------------------------------------------------------------------
# dataset / evaluator force — the referencing rows keep their dangling id (§22)
# ---------------------------------------------------------------------------


def test_dataset_force_keeps_the_queued_runs_dangling_id(admin, ada, bob):
    """§22: an administrator's `?force=true` deletes anyway (`forced: true`, the referencing rows
    keep their dangling id — "an explicit administrator decision, not a cascade"); a member's
    force is 403 `auth.forbidden`; the plain delete is 409 `dataset.in_use` naming the run."""
    ada_id, ada_client = ada
    _bob_id, bob_client = bob
    tag = uuid.uuid4().hex[:6]
    agent_id = _active_agent(f"s-ds-{tag}", created_by=ada_id)
    dataset_id = _dataset(ada_id, f"s-ds-{tag}")
    run_id = _add(EvalRun(agent_id=agent_id, agent_name=f"s-ds-{tag}", status="queued",
                          mode="evaluators", evaluators=["Builtin.Correctness"],
                          created_by=ada_id, created_by_name="ada", dataset_id=dataset_id,
                          dataset_name=f"s-ds-{tag}"))
    res = ada_client.delete(f"/api/eval/datasets/{dataset_id}")
    assert (res.status_code, res.json()["code"]) == (409, "dataset.in_use"), res.text
    assert {(r["kind"], r["id"]) for r in res.json()["detail"]["referrers"]} == {("run", run_id)}
    res = ada_client.delete(f"/api/eval/datasets/{dataset_id}?force=true")
    assert (res.status_code, res.json()["code"]) == (403, "auth.forbidden"), res.text
    res = bob_client.delete(f"/api/eval/datasets/{dataset_id}?force=true")
    assert res.status_code == 403, res.text  # another member: the §8 ownership gate (N17-obs30)
    res = admin.delete(f"/api/eval/datasets/{dataset_id}?force=true")
    assert res.status_code == 200 and res.json() == {"deleted": True, "forced": True}, res.text
    db = SessionLocal()
    try:
        run = db.get(EvalRun, run_id)
        assert (run.status, run.dataset_id) == ("queued", dataset_id)
        assert run.dataset_name == f"s-ds-{tag}"
    finally:
        db.close()
    assert ada_client.get(f"/api/eval/runs/{run_id}").json()["dataset_id"] == dataset_id


def test_evaluator_force_keeps_the_online_configs_dangling_id(admin, ada):
    """§22: the custom evaluator an online config lists blocks the plain delete (409
    `evaluator.in_use` naming the config); the administrator's force deletes anyway and the
    config "skips the unknown evaluator" — its row keeps the id (no cascade, no rewrite)."""
    ada_id, ada_client = ada
    tag = uuid.uuid4().hex[:6]
    agent_id = _active_agent(f"s-ev-{tag}", created_by=ada_id)
    evaluator_id = _evaluator(f"s-ev-{tag}", ada_id)
    config_id = _online_config(f"cfg-{tag}", f"s-cfg-{tag}", ada_id, agent_id=agent_id,
                               evaluators=[evaluator_id], status="ENABLED")
    res = ada_client.delete(f"/api/eval/evaluators/{evaluator_id}")
    assert (res.status_code, res.json()["code"]) == (409, "evaluator.in_use"), res.text
    assert ("online_config", config_id) in {(r["kind"], r["id"])
                                            for r in res.json()["detail"]["referrers"]}
    res = ada_client.delete(f"/api/eval/evaluators/{evaluator_id}?force=true")
    assert (res.status_code, res.json()["code"]) == (403, "auth.forbidden"), res.text
    res = admin.delete(f"/api/eval/evaluators/{evaluator_id}?force=true")
    assert res.status_code == 200 and res.json().get("forced") is True, res.text
    db = SessionLocal()
    try:
        cfg = db.query(OnlineEvalConfig).filter(OnlineEvalConfig.config_id == config_id).one()
        assert evaluator_id in list(cfg.evaluators or []), cfg.evaluators
    finally:
        db.close()
    assert ada_client.get(f"/api/eval/evaluators/{evaluator_id}").status_code == 404
