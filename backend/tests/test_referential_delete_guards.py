"""Referential delete guards on datasets and custom evaluators (r13-g, N13-obs16;
acceptance rulings E09 NC-11, E10 NC-08).

`evaluation/references.py`: deleting a dataset an ACTIVE run replays or a pipeline
outputs into, or an evaluator an ACTIVE run / an online config / a running canary
names, answers 409 `<kind>.in_use` with a bounded referrer list —
unless an ADMINISTRATOR passes `?force=true`. A member's `force` is 403 `auth.forbidden`.
The completed-run rule: a finished run keeps its snapshot (`dataset_name`, its scores)
and never blocks a deletion; only `service.ACTIVE_STATUSES` runs do. A cloud dataset is
additionally held by any run that pinned one of its published versions (the existing
`dataset.version_pinned` rule, applied to the whole store).
"""

from __future__ import annotations

import pytest

from tests import test_visibility as tv
from tests.test_visibility import (
    _canary,
    _dataset,
    _evaluator,
    _FakeStore,
    _online_config,
    _pipeline,
    _run,
)

app, admin, ada, bob = tv.app, tv.admin, tv.ada, tv.bob


def _in_use(res, code: str, *expected: tuple[str, str]) -> dict:
    assert res.status_code == 409, res.text
    body = res.json()
    assert body["code"] == code, body
    refs = {(r["kind"], r["id"]) for r in body["detail"]["referrers"]}
    assert refs == set(expected), refs
    assert body["detail"]["total"] == len(expected)
    assert body["detail"]["force_allowed"] == "admin"
    return body["detail"]


def _still_listed(client, url: str, row_id: str, key: str) -> bool:
    return row_id in {r["id"] for r in client.get(url).json()[key]}


# ─── datasets ────────────────────────────────────────────────────────────────


def test_dataset_in_use_by_an_active_run_or_a_pipeline_output(admin, ada):
    ada_id, ada_client = ada
    ds = _dataset(ada_id, "replayed")
    active = _run(ada_id, status="evaluating", dataset_id=ds, dataset_name="replayed")
    pipeline = _pipeline(ada_id, output_dataset=ds)
    # a finished run is history: it names the dataset but holds a snapshot of it
    _run(ada_id, status="completed", dataset_id=ds, dataset_name="replayed")
    _run(ada_id, status="failed", dataset_id=ds, dataset_name="replayed")
    detail = _in_use(ada_client.delete(f"/api/eval/datasets/{ds}"), "dataset.in_use",
                     ("run", active), ("pipeline", pipeline))
    assert detail["id"] == ds and detail["truncated"] is False
    # the creator's 409 and the administrator's 409 are the same answer
    _in_use(admin.delete(f"/api/eval/datasets/{ds}"), "dataset.in_use",
            ("run", active), ("pipeline", pipeline))
    assert _still_listed(ada_client, "/api/eval/datasets", ds, "datasets")


def test_completed_runs_never_block_a_dataset_deletion(ada):
    ada_id, ada_client = ada
    ds = _dataset(ada_id, "history-only")
    for status in ("completed", "failed", "stopped"):
        _run(ada_id, status=status, dataset_id=ds, dataset_name="history-only")
    res = ada_client.delete(f"/api/eval/datasets/{ds}")
    assert res.status_code == 200 and res.json() == {"deleted": True}
    # the finished runs keep their snapshot name
    names = {r["dataset_name"] for r in ada_client.get("/api/eval/runs").json()["runs"]}
    assert names == {"history-only"}


def test_dataset_force_is_an_administrators_override(admin, ada):
    ada_id, ada_client = ada
    ds = _dataset(ada_id, "forced")
    pipeline = _pipeline(ada_id, output_dataset=ds)
    # the creator (a member) cannot force, referenced or not
    res = ada_client.delete(f"/api/eval/datasets/{ds}?force=true")
    assert res.status_code == 403 and res.json()["code"] == "auth.forbidden", res.text
    free = _dataset(ada_id, "free")
    res = ada_client.delete(f"/api/eval/datasets/{free}?force=true")
    assert res.status_code == 403 and res.json()["code"] == "auth.forbidden"
    assert _still_listed(ada_client, "/api/eval/datasets", free, "datasets")
    # an administrator may; the pipeline keeps its dangling output id
    res = admin.delete(f"/api/eval/datasets/{ds}?force=true")
    assert res.status_code == 200 and res.json() == {"deleted": True, "forced": True}
    assert not _still_listed(admin, "/api/eval/datasets", ds, "datasets")
    kept = admin.get(f"/api/eval/pipelines/{pipeline}").json()
    assert kept["config"]["output"]["dataset_id"] == ds
    # force on an unreferenced row is an ordinary delete (no `forced` flag)
    res = admin.delete(f"/api/eval/datasets/{free}?force=true")
    assert res.status_code == 200 and res.json() == {"deleted": True}


def test_ownership_precedes_the_in_use_check(ada, bob):
    """Another member learns nothing about the dataset's referrers."""
    ada_id, _ = ada
    _bob_id, bob_client = bob
    ds = _dataset(ada_id)
    _pipeline(ada_id, output_dataset=ds)
    res = bob_client.delete(f"/api/eval/datasets/{ds}")
    assert res.status_code == 403 and res.json()["code"] == "dataset.forbidden"


def test_referrer_list_is_bounded(ada):
    from app.evaluation import references

    ada_id, ada_client = ada
    ds = _dataset(ada_id)
    for _ in range(references.LIMIT + 3):
        _pipeline(ada_id, output_dataset=ds)
    detail = ada_client.delete(f"/api/eval/datasets/{ds}").json()["detail"]
    assert detail["total"] == references.LIMIT + 3
    assert len(detail["referrers"]) == references.LIMIT
    assert detail["truncated"] is True


def test_cloud_dataset_held_by_active_runs_and_pinned_versions(monkeypatch, admin, ada):
    from app.evaluation import routers as eval_routers

    ada_id, ada_client = ada
    store = _FakeStore(["cloud-a", "cloud-free", "cloud-forced"])
    monkeypatch.setattr(eval_routers.datasets_local, "store_for", lambda ctx: store)
    active = _run(ada_id, status="invoking", dataset_id="cloud-a", dataset_name="cloud:a")
    pinned = _run(ada_id, status="completed", dataset_id="cloud-a", dataset_name="cloud:a",
                  dataset_version="2")
    _run(ada_id, status="completed", dataset_id="cloud-a", dataset_name="cloud:a")  # draft: history
    detail = _in_use(ada_client.delete("/api/eval/datasets/cloud/cloud-a"), "dataset.in_use",
                     ("run", active), ("run", pinned))
    by_id = {r["id"]: r for r in detail["referrers"]}
    assert by_id[pinned]["dataset_version"] == "2" and "dataset_version" not in by_id[active]
    assert store.deleted == []
    # a completed draft replay alone does not hold the store
    _run(ada_id, status="completed", dataset_id="cloud-free", dataset_name="cloud:free")
    assert ada_client.delete("/api/eval/datasets/cloud/cloud-free").json()["deleted"] is True
    # force: administrators only
    _run(ada_id, status="completed", dataset_id="cloud-forced", dataset_version="1")
    res = ada_client.delete("/api/eval/datasets/cloud/cloud-forced?force=true")
    assert res.status_code == 403 and res.json()["code"] == "auth.forbidden"
    res = admin.delete("/api/eval/datasets/cloud/cloud-forced?force=true")
    assert res.status_code == 200 and res.json()["forced"] is True
    assert store.deleted == ["cloud-free", "cloud-forced"]


# ─── evaluators ──────────────────────────────────────────────────────────────


@pytest.fixture
def judge(ada):
    ada_id, _ = ada
    return _evaluator("held_judge", ada_id)


def test_evaluator_in_use_by_runs_configs_and_canaries(admin, ada, judge):
    ada_id, ada_client = ada
    agent = tv._active_agent("ref-agent", ada_id)
    active = _run(ada_id, status="queued", evaluators=[judge, "Builtin.Helpfulness"])
    _run(ada_id, status="completed", evaluators=[judge])  # history
    _run(ada_id, status="waiting", evaluators=["Builtin.Helpfulness"])  # another evaluator
    config = _online_config("cfg-held", "held", ada_id, agent_id=agent, evaluators=[judge])
    _online_config("cfg-other", "other", ada_id, agent_id=agent, evaluators=["Builtin.Helpfulness"])
    canary = _canary(ada_id, champion_agent_id=agent, challenger_agent_id=agent,
                     artifacts={"online_evaluators": [judge]})
    _canary(ada_id, champion_agent_id=agent, challenger_agent_id=agent, status="cleaned",
            artifacts={"online_evaluators": [judge]})  # finished canary: history
    legacy_canary = _canary(ada_id, champion_agent_id=agent, challenger_agent_id=agent,
                            artifacts={"setup": {"online_evaluators": [judge]}})
    detail = _in_use(
        ada_client.delete(f"/api/eval/evaluators/{judge}"), "evaluator.in_use",
        ("run", active), ("online_config", "cfg-held"), ("canary", canary),
        ("canary", legacy_canary),
    )
    assert detail["id"] == judge
    assert ada_client.get(f"/api/eval/evaluators/{judge}").status_code == 200
    # the same 409 for an administrator without force; with force the row goes and the
    # referrers keep the dangling id
    _in_use(admin.delete(f"/api/eval/evaluators/{judge}"), "evaluator.in_use",
            ("run", active), ("online_config", config), ("canary", canary),
            ("canary", legacy_canary))
    res = ada_client.delete(f"/api/eval/evaluators/{judge}?force=true")
    assert res.status_code == 403 and res.json()["code"] == "auth.forbidden"
    res = admin.delete(f"/api/eval/evaluators/{judge}?force=true")
    assert res.status_code == 200 and res.json() == {"deleted": True, "forced": True}
    assert ada_client.get(f"/api/eval/evaluators/{judge}").status_code == 404
    assert admin.get(f"/api/eval/online/{config}").json()["evaluators"] == [judge]


def test_completed_runs_never_block_an_evaluator_deletion(ada, judge):
    ada_id, ada_client = ada
    for status in ("completed", "failed", "stopped"):
        _run(ada_id, status=status, evaluators=[judge])
    res = ada_client.delete(f"/api/eval/evaluators/{judge}")
    assert res.status_code == 200 and res.json() == {"deleted": True}
    # the finished runs keep the evaluator id in their record
    assert all(judge in r["evaluators"] for r in ada_client.get("/api/eval/runs").json()["runs"])


def test_operation_managed_evaluator_409_stays_first(monkeypatch, admin, judge):
    from app.evaluation import managed

    monkeypatch.setattr(managed, "managed_evaluator", lambda db, ws, eid: {"operation_id": "op-1"})
    res = admin.delete(f"/api/eval/evaluators/{judge}?force=true")
    assert res.status_code == 409 and res.json()["code"] == "evaluator.managed_by_operation"


def test_referrers_helpers_are_workspace_scoped(ada, judge):
    """Rows of another workspace never hold this workspace's dataset / evaluator."""
    from app.core.db import SessionLocal
    from app.evaluation import references

    ada_id, _ = ada
    ds = _dataset(ada_id)
    _run(ada_id, status="queued", dataset_id=ds, evaluators=[judge], workspace_id="ws-other")
    _pipeline(ada_id, output_dataset=ds)  # this workspace
    db = SessionLocal()
    try:
        assert [r["kind"] for r in references.dataset_referrers(db, "default", ds)] == ["pipeline"]
        assert references.evaluator_referrers(db, "default", judge) == []
        assert references.dataset_referrers(db, "ws-other", ds)[0]["kind"] == "run"
    finally:
        db.close()
