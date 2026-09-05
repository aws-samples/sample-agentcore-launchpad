"""Cloud datasets — re-sync edits the AWS DRAFT in place, publish versions.

One local row ↔ one AWS Dataset: the first sync creates it, later syncs replace
the draft's examples (ListDatasetExamples → DeleteDatasetExamples →
AddDatasetExamples, polled to ACTIVE), PUBLISH VERSION snapshots the draft as an
immutable numbered version (CreateDatasetVersion → UPDATING → ACTIVE).
"""

from datetime import UTC, datetime
from unittest.mock import MagicMock, call

import pytest
from botocore.exceptions import ClientError

from app.evaluation import agentcore_eval as ac
from app.evaluation.scenarios import normalize_scenarios

ARN = "arn:aws:bedrock-agentcore:us-west-2:1:dataset/cloudds-1"
CREATED_AT = datetime(2026, 9, 5, 10, 0, tzinfo=UTC)


def _detail(status="ACTIVE", *, draft="MODIFIED", count=1, reason=None):
    d = {
        "datasetId": "cloudds-1", "datasetArn": ARN, "datasetName": "sync_me",
        "status": status, "draftStatus": draft, "exampleCount": count,
        "schemaType": "AGENTCORE_EVALUATION_PREDEFINED_V1",
    }
    if reason:
        d["failureReason"] = reason
    return d


def _not_found(op="GetDataset"):
    return ClientError(
        {"Error": {"Code": "ResourceNotFoundException", "Message": "no such dataset"}}, op
    )


def stub_cloud(monkeypatch, *, get_side_effect=None, examples=(), versions=()):
    """Control-plane stub whose GetDataset answers follow ``get_side_effect``
    (a list of details, the last one repeating) — so a test can script
    UPDATING → ACTIVE transitions."""
    stub = MagicMock()
    stub.create_dataset.return_value = {"datasetId": "cloudds-1", "datasetArn": ARN}
    seq = list(get_side_effect or [_detail()])

    def get_dataset(**_kwargs):
        item = seq.pop(0) if len(seq) > 1 else seq[0]
        if isinstance(item, Exception):
            raise item
        return item

    stub.get_dataset.side_effect = get_dataset
    stub.list_dataset_examples.return_value = {"examples": list(examples)}
    stub.list_dataset_versions.return_value = {"versions": list(versions)}
    stub.create_dataset_version.return_value = {
        "datasetId": "cloudds-1", "status": "UPDATING", "datasetVersion": "1",
    }
    monkeypatch.setattr("app.evaluation.routers.control_client", lambda _ws=None: stub)
    monkeypatch.setattr("app.evaluation.agentcore_eval.time.sleep", lambda _s: None)
    return stub


def _synced_row(client, monkeypatch, **stub_kwargs):
    """A local row that already has an ACTIVE cloud copy (first sync = create)."""
    stub = stub_cloud(monkeypatch, **stub_kwargs)
    ds = client.post("/api/eval/datasets", json={
        "name": "sync me!", "items": [{"prompt": "2+2?", "expected": "4"}],
    }).json()
    res = client.post(f"/api/eval/datasets/{ds['id']}/sync-to-aws")
    assert res.status_code == 200, res.text
    stub.create_dataset.assert_called_once()
    stub.create_dataset.reset_mock()
    stub.get_dataset.reset_mock()
    return stub, res.json()


# ─── poller ──────────────────────────────────────────────────────────────────
def test_poll_accepts_updating_then_active():
    client = MagicMock()
    client.get_dataset.side_effect = [
        {"status": "UPDATING"}, {"status": "UPDATING"}, {"status": "ACTIVE"},
    ]
    final = ac.poll_dataset_active(client, dataset_id="d", sleeper=lambda _s: None)
    assert final["status"] == "ACTIVE"
    assert client.get_dataset.call_count == 3


def test_poll_update_failed_is_terminal_with_reason():
    client = MagicMock()
    client.get_dataset.return_value = {"status": "UPDATE_FAILED", "failureReason": "boom"}
    with pytest.raises(RuntimeError, match="dataset update failed: boom"):
        ac.poll_dataset_active(client, dataset_id="d", sleeper=lambda _s: None)
    assert client.get_dataset.call_count == 1


def test_poll_create_failed_still_terminal():
    client = MagicMock()
    client.get_dataset.return_value = {"status": "CREATE_FAILED", "failureReason": "bad"}
    with pytest.raises(RuntimeError, match="dataset creation failed: bad"):
        ac.poll_dataset_active(client, dataset_id="d", sleeper=lambda _s: None)


# ─── wrappers ────────────────────────────────────────────────────────────────
def test_wrappers_call_shapes():
    client = MagicMock()
    ac.add_dataset_examples(client, dataset_id="d", examples=[{"scenario_id": "a"}])
    kwargs = client.add_dataset_examples.call_args.kwargs
    assert kwargs["datasetId"] == "d"
    assert kwargs["source"] == {"inlineExamples": {"examples": [{"scenario_id": "a"}]}}
    assert kwargs["clientToken"]
    ac.delete_dataset_examples(client, dataset_id="d", example_ids=["e1", "e2"])
    kwargs = client.delete_dataset_examples.call_args.kwargs
    assert kwargs["datasetId"] == "d" and kwargs["exampleIds"] == ["e1", "e2"]
    assert kwargs["clientToken"]
    ac.create_dataset_version(client, dataset_id="d")
    kwargs = client.create_dataset_version.call_args.kwargs
    assert kwargs["datasetId"] == "d" and kwargs["clientToken"]
    ac.delete_dataset(client, dataset_id="d", version="2")
    assert client.delete_dataset.call_args.kwargs == {"datasetId": "d", "datasetVersion": "2"}
    ac.delete_dataset(client, dataset_id="d")
    assert client.delete_dataset.call_args.kwargs == {"datasetId": "d"}


def test_list_dataset_versions_paginates():
    client = MagicMock()
    client.list_dataset_versions.side_effect = [
        {"versions": [{"datasetVersion": "2"}], "nextToken": "t"},
        {"versions": [{"datasetVersion": "1"}]},
    ]
    out = ac.list_dataset_versions(client, dataset_id="d")
    assert [v["datasetVersion"] for v in out] == ["2", "1"]
    assert client.list_dataset_versions.call_args_list == [
        call(datasetId="d"), call(datasetId="d", nextToken="t"),
    ]


# ─── sync ────────────────────────────────────────────────────────────────────
def test_first_sync_without_cloud_copy_creates(client, monkeypatch):
    stub = stub_cloud(monkeypatch)
    ds = client.post("/api/eval/datasets", json={
        "name": "fresh", "items": [{"prompt": "hi"}],
    }).json()
    res = client.post(f"/api/eval/datasets/{ds['id']}/sync-to-aws")
    assert res.status_code == 200, res.text
    stub.create_dataset.assert_called_once()
    stub.add_dataset_examples.assert_not_called()
    stub.delete_dataset_examples.assert_not_called()
    cloud = res.json()["cloud"]
    assert cloud["dataset_id"] == "cloudds-1"
    assert cloud["status"] == "ACTIVE"
    assert cloud["draft_status"] == "MODIFIED"
    assert cloud["example_count"] == 1
    assert cloud["versions"] == []


def test_resync_edits_draft_in_place(client, monkeypatch):
    stub, ds = _synced_row(
        client, monkeypatch,
        examples=[{"exampleId": "ex-1", "scenario_id": "item_1", "turns": []},
                  {"exampleId": "ex-2", "scenario_id": "item_2", "turns": []}],
        versions=[{"datasetVersion": "1", "exampleCount": 2, "createdAt": CREATED_AT}],
    )
    # edit locally, then re-sync
    edited = client.put(f"/api/eval/datasets/{ds['id']}", json={
        "name": "sync me!", "items": [{"prompt": "3+3?", "expected": "6"}, {"prompt": "x"}],
    })
    assert edited.status_code == 200, edited.text
    res = client.post(f"/api/eval/datasets/{ds['id']}/sync-to-aws")
    assert res.status_code == 200, res.text

    stub.create_dataset.assert_not_called()
    stub.list_dataset_examples.assert_called_once_with(datasetId="cloudds-1")
    stub.delete_dataset_examples.assert_called_once()
    del_kwargs = stub.delete_dataset_examples.call_args.kwargs
    assert del_kwargs["datasetId"] == "cloudds-1"
    assert del_kwargs["exampleIds"] == ["ex-1", "ex-2"]
    stub.add_dataset_examples.assert_called_once()
    add_kwargs = stub.add_dataset_examples.call_args.kwargs
    assert add_kwargs["datasetId"] == "cloudds-1"
    assert add_kwargs["source"] == {
        "inlineExamples": {"examples": normalize_scenarios(edited.json()["items"])}
    }
    # ordering: list → delete → add; GetDataset polled after each step
    names = [c[0] for c in stub.mock_calls if c[0] in {
        "list_dataset_examples", "delete_dataset_examples", "add_dataset_examples",
    }]
    assert names == ["list_dataset_examples", "delete_dataset_examples", "add_dataset_examples"]
    assert stub.get_dataset.call_count >= 3  # existence check + after delete + after add

    cloud = res.json()["cloud"]
    assert cloud["dataset_id"] == "cloudds-1"  # same dataset, not a new one
    assert cloud["arn"] == ARN
    assert cloud["status"] == "ACTIVE"
    assert cloud["draft_status"] == "MODIFIED"
    assert cloud["example_count"] == 1
    assert cloud["failure_reason"] is None
    assert cloud["versions"] == [
        {"version": "1", "example_count": 2, "created_at": CREATED_AT.isoformat()}
    ]


def test_resync_empty_draft_skips_delete(client, monkeypatch):
    stub, ds = _synced_row(client, monkeypatch, examples=[])
    res = client.post(f"/api/eval/datasets/{ds['id']}/sync-to-aws")
    assert res.status_code == 200, res.text
    stub.create_dataset.assert_not_called()
    stub.delete_dataset_examples.assert_not_called()
    stub.add_dataset_examples.assert_called_once()


def test_resync_waits_for_a_settling_draft_before_editing(client, monkeypatch):
    stub, ds = _synced_row(client, monkeypatch, examples=[{"exampleId": "ex-1"}])
    # existence check sees UPDATING → the poller must reach ACTIVE before editing
    seq = [_detail("UPDATING"), _detail("ACTIVE"), _detail("ACTIVE")]
    stub.get_dataset.side_effect = lambda **_k: seq.pop(0) if len(seq) > 1 else seq[0]
    res = client.post(f"/api/eval/datasets/{ds['id']}/sync-to-aws")
    assert res.status_code == 200, res.text
    stub.delete_dataset_examples.assert_called_once()
    stub.add_dataset_examples.assert_called_once()


def test_resync_falls_back_to_create_when_cloud_copy_is_gone(client, monkeypatch):
    stub, ds = _synced_row(client, monkeypatch)
    stub.get_dataset.side_effect = None
    stub.get_dataset.return_value = _detail()
    stub.create_dataset.return_value = {"datasetId": "cloudds-2", "datasetArn": ARN + "2"}
    calls = {"n": 0}

    def get_dataset(**kwargs):
        calls["n"] += 1
        if kwargs["datasetId"] == "cloudds-1":
            raise _not_found()
        return {**_detail(), "datasetId": "cloudds-2"}

    stub.get_dataset.side_effect = get_dataset
    res = client.post(f"/api/eval/datasets/{ds['id']}/sync-to-aws")
    assert res.status_code == 200, res.text
    stub.create_dataset.assert_called_once()
    stub.add_dataset_examples.assert_not_called()
    stub.delete_dataset_examples.assert_not_called()
    assert res.json()["cloud"]["dataset_id"] == "cloudds-2"


def test_resync_after_console_delete_creates_again(client, monkeypatch):
    stub, ds = _synced_row(client, monkeypatch)
    assert client.delete("/api/eval/datasets/cloud/cloudds-1").status_code == 200
    res = client.post(f"/api/eval/datasets/{ds['id']}/sync-to-aws")
    assert res.status_code == 200, res.text
    stub.create_dataset.assert_called_once()
    stub.add_dataset_examples.assert_not_called()
    assert res.json()["cloud"]["status"] == "ACTIVE"


def test_resync_other_aws_errors_are_not_swallowed(client, monkeypatch):
    stub, ds = _synced_row(client, monkeypatch)
    stub.get_dataset.side_effect = ClientError(
        {"Error": {"Code": "AccessDeniedException", "Message": "nope"}}, "GetDataset"
    )
    res = client.post(f"/api/eval/datasets/{ds['id']}/sync-to-aws")
    assert res.status_code == 403
    stub.create_dataset.assert_not_called()


def test_resync_update_failed_records_reason_and_keeps_id(client, monkeypatch):
    stub, ds = _synced_row(client, monkeypatch, examples=[{"exampleId": "ex-1"}])
    seq = [_detail("ACTIVE"), _detail("ACTIVE"), _detail("UPDATE_FAILED", reason="bad shape")]
    stub.get_dataset.side_effect = lambda **_k: seq.pop(0) if len(seq) > 1 else seq[0]
    res = client.post(f"/api/eval/datasets/{ds['id']}/sync-to-aws")
    assert res.status_code == 502
    assert res.json()["code"] == "dataset.sync_failed"
    assert "bad shape" in res.json()["message"]
    row = next(d for d in client.get("/api/eval/datasets").json()["datasets"]
               if d["id"] == ds["id"])
    assert row["cloud"]["dataset_id"] == "cloudds-1"
    assert row["cloud"]["status"] == "UPDATE_FAILED"
    assert "bad shape" in row["cloud"]["failure_reason"]


def test_resync_validation_error_from_add_is_502_sync_failed(client, monkeypatch):
    stub, ds = _synced_row(client, monkeypatch, examples=[])
    stub.add_dataset_examples.side_effect = ClientError(
        {"Error": {"Code": "ValidationException", "Message": "example 0 invalid"}},
        "AddDatasetExamples",
    )
    res = client.post(f"/api/eval/datasets/{ds['id']}/sync-to-aws")
    assert res.status_code == 502
    assert res.json()["code"] == "dataset.sync_failed"
    assert "example 0 invalid" in res.json()["message"]


# ─── publish version (local row) ─────────────────────────────────────────────
def test_publish_version_polls_through_updating(client, monkeypatch):
    stub, ds = _synced_row(client, monkeypatch)
    seq = [
        _detail("ACTIVE"),  # existence check
        _detail("UPDATING"),
        _detail("UPDATING"),
        _detail("ACTIVE", draft="UNMODIFIED", count=1),
    ]
    stub.get_dataset.side_effect = lambda **_k: seq.pop(0) if len(seq) > 1 else seq[0]
    stub.list_dataset_versions.return_value = {"versions": [
        {"datasetVersion": "1", "exampleCount": 1, "createdAt": CREATED_AT},
    ]}
    res = client.post(f"/api/eval/datasets/{ds['id']}/publish-version")
    assert res.status_code == 200, res.text
    stub.create_dataset_version.assert_called_once()
    assert stub.create_dataset_version.call_args.kwargs["datasetId"] == "cloudds-1"
    assert stub.get_dataset.call_count == 4
    cloud = res.json()["cloud"]
    assert cloud["dataset_id"] == "cloudds-1"
    assert cloud["status"] == "ACTIVE"
    assert cloud["draft_status"] == "UNMODIFIED"
    assert cloud["failure_reason"] is None
    assert cloud["versions"] == [
        {"version": "1", "example_count": 1, "created_at": CREATED_AT.isoformat()}
    ]
    assert cloud["synced_at"] == ds["cloud"]["synced_at"]  # publish is not a sync


def test_publish_version_update_failed_is_502_and_recorded(client, monkeypatch):
    stub, ds = _synced_row(
        client, monkeypatch,
        versions=[{"datasetVersion": "1", "exampleCount": 1, "createdAt": CREATED_AT}],
    )
    # the create path records no versions (a fresh dataset has none); a
    # re-sync loads the list so the test can prove a failed publish keeps it
    assert client.post(f"/api/eval/datasets/{ds['id']}/sync-to-aws").status_code == 200
    seq = [_detail("ACTIVE"), _detail("UPDATING"),
           _detail("UPDATE_FAILED", reason="snapshot quota exceeded")]
    stub.get_dataset.side_effect = lambda **_k: seq.pop(0) if len(seq) > 1 else seq[0]
    res = client.post(f"/api/eval/datasets/{ds['id']}/publish-version")
    assert res.status_code == 502
    assert res.json()["code"] == "dataset.publish_failed"
    assert "snapshot quota exceeded" in res.json()["message"]
    row = next(d for d in client.get("/api/eval/datasets").json()["datasets"]
               if d["id"] == ds["id"])
    assert row["cloud"]["status"] == "UPDATE_FAILED"
    assert "snapshot quota exceeded" in row["cloud"]["failure_reason"]
    assert row["cloud"]["dataset_id"] == "cloudds-1"
    assert [v["version"] for v in row["cloud"]["versions"]] == ["1"]  # kept


def test_publish_version_without_cloud_copy_is_409(client, monkeypatch):
    stub = stub_cloud(monkeypatch)
    ds = client.post("/api/eval/datasets", json={
        "name": "local only", "items": [{"prompt": "hi"}],
    }).json()
    res = client.post(f"/api/eval/datasets/{ds['id']}/publish-version")
    assert res.status_code == 409
    assert res.json()["code"] == "dataset.not_synced"
    stub.create_dataset_version.assert_not_called()


def test_publish_version_unknown_dataset_is_404(client, monkeypatch):
    stub_cloud(monkeypatch)
    assert client.post("/api/eval/datasets/nope/publish-version").status_code == 404


# ─── cloud-only rows ─────────────────────────────────────────────────────────
def test_cloud_detail_returns_draft_status_and_versions(client, monkeypatch):
    stub_cloud(
        monkeypatch,
        get_side_effect=[_detail("ACTIVE", draft="UNMODIFIED", count=3)],
        examples=[{"exampleId": "ex-1", "scenario_id": "s", "turns": [{"input": "hi"}]}],
        versions=[
            {"datasetVersion": "2", "exampleCount": 3, "createdAt": CREATED_AT},
            {"datasetVersion": "1", "exampleCount": 2,
             "createdAt": datetime(2026, 9, 1, tzinfo=UTC)},
        ],
    )
    res = client.get("/api/eval/datasets/cloud/cloudds-1")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["draft_status"] == "UNMODIFIED"
    assert body["failure_reason"] is None
    assert [v["version"] for v in body["versions"]] == ["2", "1"]
    assert body["versions"][0] == {
        "version": "2", "example_count": 3, "created_at": CREATED_AT.isoformat()
    }
    assert body["runnable"] is True


def test_cloud_list_passes_draft_status(client, monkeypatch):
    stub = stub_cloud(monkeypatch)
    stub.list_datasets.return_value = {"datasets": [
        {"datasetId": "cloudds-9", "datasetName": "remote", "status": "ACTIVE",
         "draftStatus": "MODIFIED", "exampleCount": 2},
    ]}
    res = client.get("/api/eval/datasets/cloud")
    assert res.json()["datasets"][0]["draftStatus"] == "MODIFIED"


def test_cloud_only_publish_version(client, monkeypatch):
    stub = stub_cloud(
        monkeypatch,
        get_side_effect=[_detail("UPDATING"), _detail("ACTIVE", draft="UNMODIFIED", count=2)],
        examples=[{"exampleId": "ex-1", "scenario_id": "s", "turns": [{"input": "hi"}]}],
    )
    stub.list_dataset_versions.return_value = {"versions": [
        {"datasetVersion": "1", "exampleCount": 2, "createdAt": CREATED_AT},
    ]}
    res = client.post("/api/eval/datasets/cloud/cloudds-1/publish-version")
    assert res.status_code == 200, res.text
    assert stub.create_dataset_version.call_args.kwargs["datasetId"] == "cloudds-1"
    body = res.json()
    assert body["datasetId"] == "cloudds-1"
    assert body["status"] == "ACTIVE"
    assert body["draft_status"] == "UNMODIFIED"
    assert [v["version"] for v in body["versions"]] == ["1"]


def test_cloud_only_publish_failure_is_502(client, monkeypatch):
    stub_cloud(monkeypatch, get_side_effect=[_detail("UPDATE_FAILED", reason="quota")])
    res = client.post("/api/eval/datasets/cloud/cloudds-1/publish-version")
    assert res.status_code == 502
    assert res.json()["code"] == "dataset.publish_failed"
    assert "quota" in res.json()["message"]


def test_cloud_only_publish_unknown_dataset_is_404(client, monkeypatch):
    stub = stub_cloud(monkeypatch)
    stub.create_dataset_version.side_effect = _not_found("CreateDatasetVersion")
    res = client.post("/api/eval/datasets/cloud/ghost/publish-version")
    assert res.status_code == 404


def test_delete_one_version_refreshes_local_cache(client, monkeypatch):
    stub, ds = _synced_row(
        client, monkeypatch,
        versions=[{"datasetVersion": "2", "exampleCount": 1, "createdAt": CREATED_AT},
                  {"datasetVersion": "1", "exampleCount": 1, "createdAt": CREATED_AT}],
    )
    # blob only carries versions after a re-sync/publish; re-sync to load them
    assert client.post(f"/api/eval/datasets/{ds['id']}/sync-to-aws").status_code == 200
    res = client.delete("/api/eval/datasets/cloud/cloudds-1/versions/1")
    assert res.status_code == 200
    assert res.json() == {"datasetId": "cloudds-1", "version": "1", "deleted": True}
    assert stub.delete_dataset.call_args.kwargs == {"datasetId": "cloudds-1",
                                                    "datasetVersion": "1"}
    row = next(d for d in client.get("/api/eval/datasets").json()["datasets"]
               if d["id"] == ds["id"])
    assert [v["version"] for v in row["cloud"]["versions"]] == ["2"]
    assert row["cloud"]["status"] == "ACTIVE"  # the dataset itself is untouched
