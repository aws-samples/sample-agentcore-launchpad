"""Cloud datasets as S3 snapshots (N12 E1-2, CA-6) — sync edits the DRAFT in place,
publish versions are immutable.

One local row ↔ one snapshot store ``eval-datasets/<cloud_id>/``: the first sync creates
it, later syncs replace ``draft.json``, PUBLISH VERSION copies the draft to ``v<n>.json``
(never overwritten — a second publish is ``v2``). The baseline's HTTP codes, the ``cloud``
blob keys and the detail / list shapes are unchanged; only the backing store moved from
AgentCore Datasets (absent in aws-cn) to the workspace artifacts bucket.
"""

import json

from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.evaluation.models import EvalRun
from tests.evaluation.test_datasets_v2 import SCENARIO, fake_store, seed_cloud

BUCKET = "bucket"


def _keys(s3, cloud_id):
    return sorted(k for (_b, k) in s3.objects if k.startswith(f"eval-datasets/{cloud_id}/"))


def _meta(s3, cloud_id):
    return json.loads(s3.objects[(BUCKET, f"eval-datasets/{cloud_id}/meta.json")])


def _synced_row(client, monkeypatch, items=None):
    s3 = fake_store(monkeypatch)
    ds = client.post("/api/eval/datasets", json={
        "name": "sync me!", "items": items or [{"prompt": "2+2?", "expected": "4"}],
    }).json()
    res = client.post(f"/api/eval/datasets/{ds['id']}/sync-to-aws")
    assert res.status_code == 200, res.text
    return s3, res.json()


# ─── sync ────────────────────────────────────────────────────────────────────
def test_first_sync_without_cloud_copy_creates(client, monkeypatch):
    s3, ds = _synced_row(client, monkeypatch)
    cloud = ds["cloud"]
    assert set(cloud) == {"dataset_id", "arn", "status", "synced_at", "failure_reason",
                          "draft_status", "example_count", "versions"}
    cloud_id = cloud["dataset_id"]
    assert cloud_id.startswith("cds_")
    assert cloud["arn"] == f"arn:aws-cn:s3:::{BUCKET}/eval-datasets/{cloud_id}"
    assert cloud["status"] == "ACTIVE" and cloud["failure_reason"] is None
    assert cloud["draft_status"] == "MODIFIED" and cloud["example_count"] == 1
    assert cloud["versions"] == [] and cloud["synced_at"]
    assert _keys(s3, cloud_id) == [f"eval-datasets/{cloud_id}/draft.json",
                                   f"eval-datasets/{cloud_id}/meta.json"]
    meta = _meta(s3, cloud_id)
    assert meta["datasetName"] == "sync_me" and meta["workspaceId"] == DEFAULT_WORKSPACE_ID
    assert meta["schemaType"] == "AGENTCORE_EVALUATION_PREDEFINED_V1"
    draft = json.loads(s3.objects[(BUCKET, f"eval-datasets/{cloud_id}/draft.json")])
    assert draft["examples"] == [{"scenario_id": "item_1",
                                  "turns": [{"input": "2+2?", "expected_response": "4"}]}]


def test_resync_edits_draft_in_place(client, monkeypatch):
    s3, ds = _synced_row(client, monkeypatch)
    cloud_id = ds["cloud"]["dataset_id"]
    assert client.post(f"/api/eval/datasets/{ds['id']}/publish-version").status_code == 200
    edited = client.put(f"/api/eval/datasets/{ds['id']}", json={
        "items": [{"prompt": "3+3?", "expected": "6"}, {"prompt": "x"}],
    })
    assert edited.status_code == 200, edited.text
    res = client.post(f"/api/eval/datasets/{ds['id']}/sync-to-aws")
    assert res.status_code == 200, res.text
    cloud = res.json()["cloud"]
    assert cloud["dataset_id"] == cloud_id  # same dataset, not a new one
    assert cloud["status"] == "ACTIVE" and cloud["draft_status"] == "MODIFIED"
    assert cloud["example_count"] == 2 and cloud["failure_reason"] is None
    assert [v["version"] for v in cloud["versions"]] == ["1"]  # published versions survive
    draft = json.loads(s3.objects[(BUCKET, f"eval-datasets/{cloud_id}/draft.json")])
    assert [e["scenario_id"] for e in draft["examples"]] == ["item_1", "item_2"]
    v1 = json.loads(s3.objects[(BUCKET, f"eval-datasets/{cloud_id}/v1.json")])
    assert v1["examples"][0]["turns"][0]["input"] == "2+2?"  # the snapshot is untouched


def test_resync_after_console_delete_creates_again(client, monkeypatch):
    s3, ds = _synced_row(client, monkeypatch)
    old_id = ds["cloud"]["dataset_id"]
    assert client.delete(f"/api/eval/datasets/cloud/{old_id}").status_code == 200
    res = client.post(f"/api/eval/datasets/{ds['id']}/sync-to-aws")
    assert res.status_code == 200, res.text
    assert res.json()["cloud"]["status"] == "ACTIVE"
    assert res.json()["cloud"]["dataset_id"] != old_id
    assert _keys(s3, old_id) == []


def test_resync_falls_back_to_create_when_cloud_copy_is_gone(client, monkeypatch):
    s3, ds = _synced_row(client, monkeypatch)
    old_id = ds["cloud"]["dataset_id"]
    for key in _keys(s3, old_id):
        s3.objects.pop((BUCKET, key))
    res = client.post(f"/api/eval/datasets/{ds['id']}/sync-to-aws")
    assert res.status_code == 200, res.text
    assert res.json()["cloud"]["dataset_id"] != old_id


def test_sync_store_failure_is_502_and_recorded(client, monkeypatch):
    s3, ds = _synced_row(client, monkeypatch)
    cloud_id = ds["cloud"]["dataset_id"]

    def boom(**kwargs):
        raise RuntimeError("bucket on fire")

    monkeypatch.setattr(s3, "put_object", boom)
    res = client.post(f"/api/eval/datasets/{ds['id']}/sync-to-aws")
    assert res.status_code == 502
    assert res.json()["code"] == "dataset.sync_failed"
    assert "bucket on fire" in res.json()["message"]
    row = next(d for d in client.get("/api/eval/datasets").json()["datasets"]
               if d["id"] == ds["id"])
    assert row["cloud"]["dataset_id"] == cloud_id  # the id is kept
    assert row["cloud"]["status"] == "UPDATE_FAILED"
    assert "bucket on fire" in row["cloud"]["failure_reason"]


def test_first_sync_failure_records_create_failed(client, monkeypatch):
    s3 = fake_store(monkeypatch)
    ds = client.post("/api/eval/datasets", json={"name": "failing", "items": [{"prompt": "x"}]})

    def boom(**kwargs):
        raise RuntimeError("bad examples")

    monkeypatch.setattr(s3, "put_object", boom)
    res = client.post(f"/api/eval/datasets/{ds.json()['id']}/sync-to-aws")
    assert res.status_code == 502
    assert res.json()["code"] == "dataset.sync_failed"
    row = next(d for d in client.get("/api/eval/datasets").json()["datasets"]
               if d["id"] == ds.json()["id"])
    assert row["cloud"]["status"] == "CREATE_FAILED"
    assert "bad examples" in row["cloud"]["failure_reason"]


def test_sync_without_an_artifacts_bucket_is_503(client, monkeypatch):
    from app.evaluation import datasets_local
    from tests.conftest import set_default_resources
    from tests.evaluation.fakes import FakeS3

    set_default_resources({})
    monkeypatch.setattr(datasets_local, "s3_client", lambda _ws: FakeS3())
    ds = client.post("/api/eval/datasets", json={"name": "x", "items": [{"prompt": "x"}]})
    res = client.post(f"/api/eval/datasets/{ds.json()['id']}/sync-to-aws")
    assert res.status_code == 503
    assert res.json()["code"] == "dataset.store_unavailable"


# ─── publish version (local row) ─────────────────────────────────────────────
def test_publish_version_snapshots_the_draft(client, monkeypatch):
    s3, ds = _synced_row(client, monkeypatch)
    cloud_id = ds["cloud"]["dataset_id"]
    res = client.post(f"/api/eval/datasets/{ds['id']}/publish-version")
    assert res.status_code == 200, res.text
    cloud = res.json()["cloud"]
    assert cloud["dataset_id"] == cloud_id and cloud["status"] == "ACTIVE"
    assert cloud["draft_status"] == "UNMODIFIED" and cloud["failure_reason"] is None
    assert len(cloud["versions"]) == 1
    assert cloud["versions"][0]["version"] == "1"
    assert cloud["versions"][0]["example_count"] == 1
    assert cloud["versions"][0]["created_at"]
    assert cloud["synced_at"] == ds["cloud"]["synced_at"]  # publish is not a sync
    assert f"eval-datasets/{cloud_id}/v1.json" in _keys(s3, cloud_id)


def test_published_versions_are_immutable(client, monkeypatch):
    s3, ds = _synced_row(client, monkeypatch)
    cloud_id = ds["cloud"]["dataset_id"]
    assert client.post(f"/api/eval/datasets/{ds['id']}/publish-version").status_code == 200
    v1_before = s3.objects[(BUCKET, f"eval-datasets/{cloud_id}/v1.json")]
    client.put(f"/api/eval/datasets/{ds['id']}", json={"items": [{"prompt": "changed"}]})
    assert client.post(f"/api/eval/datasets/{ds['id']}/sync-to-aws").status_code == 200
    res = client.post(f"/api/eval/datasets/{ds['id']}/publish-version")
    assert res.status_code == 200
    assert [v["version"] for v in res.json()["cloud"]["versions"]] == ["2", "1"]  # newest first
    assert s3.objects[(BUCKET, f"eval-datasets/{cloud_id}/v1.json")] == v1_before
    v2 = json.loads(s3.objects[(BUCKET, f"eval-datasets/{cloud_id}/v2.json")])
    assert v2["examples"][0]["turns"][0]["input"] == "changed"


def test_publish_after_a_deleted_version_never_reuses_its_number(client, monkeypatch):
    s3, ds = _synced_row(client, monkeypatch)
    cloud_id = ds["cloud"]["dataset_id"]
    assert client.post(f"/api/eval/datasets/{ds['id']}/publish-version").status_code == 200
    assert client.post(f"/api/eval/datasets/{ds['id']}/publish-version").status_code == 200
    assert client.delete(f"/api/eval/datasets/cloud/{cloud_id}/versions/2").status_code == 200
    res = client.post(f"/api/eval/datasets/{ds['id']}/publish-version")
    assert [v["version"] for v in res.json()["cloud"]["versions"]] == ["3", "1"]


def test_publish_version_failure_is_502_and_recorded(client, monkeypatch):
    s3, ds = _synced_row(client, monkeypatch)
    assert client.post(f"/api/eval/datasets/{ds['id']}/publish-version").status_code == 200

    def boom(**kwargs):
        raise RuntimeError("snapshot quota exceeded")

    monkeypatch.setattr(s3, "put_object", boom)
    res = client.post(f"/api/eval/datasets/{ds['id']}/publish-version")
    assert res.status_code == 502
    assert res.json()["code"] == "dataset.publish_failed"
    assert "snapshot quota exceeded" in res.json()["message"]
    row = next(d for d in client.get("/api/eval/datasets").json()["datasets"]
               if d["id"] == ds["id"])
    assert row["cloud"]["status"] == "UPDATE_FAILED"
    assert "snapshot quota exceeded" in row["cloud"]["failure_reason"]
    assert row["cloud"]["dataset_id"] == ds["cloud"]["dataset_id"]
    assert [v["version"] for v in row["cloud"]["versions"]] == ["1"]  # kept


def test_publish_version_without_cloud_copy_is_409(client, monkeypatch):
    fake_store(monkeypatch)
    ds = client.post("/api/eval/datasets", json={
        "name": "local only", "items": [{"prompt": "hi"}],
    }).json()
    res = client.post(f"/api/eval/datasets/{ds['id']}/publish-version")
    assert res.status_code == 409
    assert res.json()["code"] == "dataset.not_synced"


def test_publish_version_unknown_dataset_is_404(client, monkeypatch):
    fake_store(monkeypatch)
    assert client.post("/api/eval/datasets/nope/publish-version").status_code == 404


# ─── cloud-only rows ─────────────────────────────────────────────────────────
def test_cloud_detail_returns_draft_status_and_versions(client, monkeypatch):
    s3 = fake_store(monkeypatch)
    seed_cloud(s3, cloud_id="cloudds-1", name="sync_me", examples=[SCENARIO],
               versions=("1", "2"), draft_status="UNMODIFIED")
    res = client.get("/api/eval/datasets/cloud/cloudds-1")
    assert res.status_code == 200, res.text
    body = res.json()
    assert set(body) == {"datasetId", "name", "status", "schemaType", "exampleCount",
                         "draft_status", "failure_reason", "versions", "runnable",
                         "has_ground_truth"}
    assert body["draft_status"] == "UNMODIFIED" and body["failure_reason"] is None
    assert [v["version"] for v in body["versions"]] == ["2", "1"]
    assert set(body["versions"][0]) == {"version", "example_count", "created_at"}
    assert body["runnable"] is True and body["has_ground_truth"] is True


def test_cloud_detail_unknown_or_foreign_is_404(client, monkeypatch):
    s3 = fake_store(monkeypatch)
    seed_cloud(s3, cloud_id="cloudds-9", workspace_id="other")
    assert client.get("/api/eval/datasets/cloud/cloudds-9").status_code == 404
    assert client.get("/api/eval/datasets/cloud/ghost").status_code == 404


def test_cloud_list_passes_draft_status_and_hides_foreign_stores(client, monkeypatch):
    s3 = fake_store(monkeypatch)
    seed_cloud(s3, cloud_id="cloudds-9", name="remote", examples=[SCENARIO, SCENARIO],
               draft_status="MODIFIED")
    seed_cloud(s3, cloud_id="cloudds-x", name="foreign", workspace_id="other")
    res = client.get("/api/eval/datasets/cloud")
    datasets = res.json()["datasets"]
    assert [d["datasetId"] for d in datasets] == ["cloudds-9"]
    assert datasets[0] == {
        "datasetId": "cloudds-9", "name": "remote", "status": "ACTIVE",
        "schemaType": "AGENTCORE_EVALUATION_PREDEFINED_V1", "exampleCount": 2,
        "draftStatus": "MODIFIED", "updatedAt": datasets[0]["updatedAt"],
    }
    assert datasets[0]["updatedAt"]


def test_cloud_only_publish_version(client, monkeypatch):
    s3 = fake_store(monkeypatch)
    seed_cloud(s3, cloud_id="cloudds-1", examples=[SCENARIO], draft_status="MODIFIED")
    res = client.post("/api/eval/datasets/cloud/cloudds-1/publish-version")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["datasetId"] == "cloudds-1" and body["status"] == "ACTIVE"
    assert body["draft_status"] == "UNMODIFIED"
    assert [v["version"] for v in body["versions"]] == ["1"]
    assert (BUCKET, "eval-datasets/cloudds-1/v1.json") in s3.objects


def test_cloud_only_publish_unknown_dataset_is_404(client, monkeypatch):
    fake_store(monkeypatch)
    assert client.post("/api/eval/datasets/cloud/ghost/publish-version").status_code == 404


def test_delete_one_version_refreshes_local_cache(client, monkeypatch):
    s3, ds = _synced_row(client, monkeypatch)
    cloud_id = ds["cloud"]["dataset_id"]
    for _ in range(2):
        assert client.post(f"/api/eval/datasets/{ds['id']}/publish-version").status_code == 200
    res = client.delete(f"/api/eval/datasets/cloud/{cloud_id}/versions/1")
    assert res.status_code == 200
    assert res.json() == {"datasetId": cloud_id, "version": "1", "deleted": True}
    assert (BUCKET, f"eval-datasets/{cloud_id}/v1.json") not in s3.objects
    assert (BUCKET, f"eval-datasets/{cloud_id}/v2.json") in s3.objects
    row = next(d for d in client.get("/api/eval/datasets").json()["datasets"]
               if d["id"] == ds["id"])
    assert [v["version"] for v in row["cloud"]["versions"]] == ["2"]
    assert row["cloud"]["status"] == "ACTIVE"  # the dataset itself is untouched
    assert client.delete(f"/api/eval/datasets/cloud/{cloud_id}/versions/1").status_code == 404


def test_delete_a_version_a_run_pinned_is_409(client, monkeypatch):
    s3, ds = _synced_row(client, monkeypatch)
    cloud_id = ds["cloud"]["dataset_id"]
    assert client.post(f"/api/eval/datasets/{ds['id']}/publish-version").status_code == 200
    with SessionLocal() as db:
        db.add(EvalRun(workspace_id=DEFAULT_WORKSPACE_ID, agent_id="a", agent_name="n",
                       dataset_id=cloud_id, dataset_version="1", status="completed"))
        db.commit()
    res = client.delete(f"/api/eval/datasets/cloud/{cloud_id}/versions/1")
    assert res.status_code == 409
    assert res.json()["code"] == "dataset.version_pinned"
    assert (BUCKET, f"eval-datasets/{cloud_id}/v1.json") in s3.objects


def test_delete_cloud_dataset_removes_the_store_and_marks_rows(client, monkeypatch):
    s3, ds = _synced_row(client, monkeypatch)
    cloud_id = ds["cloud"]["dataset_id"]
    assert client.post(f"/api/eval/datasets/{ds['id']}/publish-version").status_code == 200
    res = client.delete(f"/api/eval/datasets/cloud/{cloud_id}")
    assert res.status_code == 200
    assert res.json() == {"datasetId": cloud_id, "deleted": True}
    assert _keys(s3, cloud_id) == []
    row = next(d for d in client.get("/api/eval/datasets").json()["datasets"]
               if d["id"] == ds["id"])
    assert row["cloud"]["status"] == "deleted"
    assert client.delete(f"/api/eval/datasets/cloud/{cloud_id}").status_code == 404
