"""Knowledge-base fixes from the 2026-11-02 prod QA (QA-3, K3/K5/K6). AWS is faked.

- K3: a data source that is not AVAILABLE yet answers an empty, not-ready document
  page instead of ListKnowledgeBaseDocuments' 400 ("Data source not in available
  status.").
- K5: a KB still CREATING is refused (409) before anything is torn down.
- K6: a deleted KB's uploads under `kb/<id>/` (and a project KB's
  `projects/<pid>/kb/<id>/`) are purged from the artifacts bucket.
"""

import logging
from unittest.mock import MagicMock

import pytest

from app.core.errors import AppError
from app.services import aws_clients, knowledge, project_kb

from .conftest import client_error, ws_ctx


class ValidationException(Exception):
    pass


class ResourceNotFoundException(Exception):
    pass


def _client(*, kb_status="ACTIVE", ds_status="AVAILABLE"):
    client = MagicMock()
    client.exceptions.ValidationException = ValidationException
    client.exceptions.ResourceNotFoundException = ResourceNotFoundException
    client.exceptions.ConflictException = type("ConflictException", (Exception,), {})
    client.get_knowledge_base.return_value = {"knowledgeBase": {
        "status": kb_status, "knowledgeBaseConfiguration": {"type": "MANAGED"}}}
    client.get_data_source.return_value = {"dataSource": {"status": ds_status}}
    client.list_knowledge_base_documents.return_value = {"documentDetails": []}
    return client


# ── K3 ────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("status", ["CREATING", "DELETING", "DELETE_UNSUCCESSFUL"])
def test_documents_of_a_source_that_is_not_available_are_an_empty_not_ready_page(
    monkeypatch, status
):
    client = _client(ds_status=status)
    monkeypatch.setattr(knowledge, "agent_client", lambda _ws=None: client)
    out = knowledge.list_documents(ws_ctx(), "KB1", "DS1")
    assert out == {"documents": [], "next_token": None, "page_size": 50,
                   "ready": False, "data_source_status": status}
    client.list_knowledge_base_documents.assert_not_called()


def test_a_source_that_flips_between_the_reads_is_still_not_an_error(monkeypatch):
    client = _client()
    client.list_knowledge_base_documents.side_effect = ValidationException(
        "Data source not in available status.")
    monkeypatch.setattr(knowledge, "agent_client", lambda _ws=None: client)
    out = knowledge.list_documents(ws_ctx(), "KB1", "DS1")
    assert out["ready"] is False and out["documents"] == []


def test_other_validation_errors_still_surface(monkeypatch):
    client = _client()
    client.list_knowledge_base_documents.side_effect = ValidationException("bad token")
    monkeypatch.setattr(knowledge, "agent_client", lambda _ws=None: client)
    with pytest.raises(ValidationException):
        knowledge.list_documents(ws_ctx(), "KB1", "DS1", token="garbage")


def test_an_available_source_lists_and_says_ready(monkeypatch):
    client = _client()
    monkeypatch.setattr(knowledge, "agent_client", lambda _ws=None: client)
    monkeypatch.setattr(knowledge, "_s3_object_meta", lambda *a: {})
    out = knowledge.list_documents(ws_ctx(), "KB1", "DS1")
    assert out["ready"] is True and out["data_source_status"] == "AVAILABLE"
    client.list_knowledge_base_documents.assert_called_once()


# ── K5 ────────────────────────────────────────────────────────────────────────


def test_a_creating_kb_is_refused_before_anything_is_torn_down(monkeypatch):
    client = _client(kb_status="CREATING")
    monkeypatch.setattr(knowledge, "agent_client", lambda _ws=None: client)
    torn: list[str] = []
    monkeypatch.setattr(knowledge, "_list_data_sources", lambda c, kb: torn.append("ds") or [])
    monkeypatch.setattr(knowledge, "_delete_kb_policy", lambda *a, **k: torn.append("policy"))
    with pytest.raises(AppError) as exc:
        knowledge.delete_kb(ws_ctx({"kb_gateway_id": "gw"}), "KB1")
    assert exc.value.code == "kb.delete_conflict" and exc.value.status_code == 409
    assert torn == []
    client.delete_knowledge_base.assert_not_called()


# ── K6 ────────────────────────────────────────────────────────────────────────


class FakeS3:
    def __init__(self, keys, fail_prefix=None):
        self.keys = list(keys)
        self.deleted: list[str] = []
        self.fail_prefix = fail_prefix

    def get_paginator(self, name):
        assert name == "list_objects_v2"
        s3 = self

        class _P:
            def paginate(self, Bucket, Prefix):  # noqa: N803
                if Prefix == s3.fail_prefix:
                    raise client_error("AccessDenied")
                yield {"Contents": [{"Key": k} for k in s3.keys if k.startswith(Prefix)]}

        return _P()

    def delete_objects(self, Bucket, Delete):  # noqa: N803
        self.deleted.extend(o["Key"] for o in Delete["Objects"])
        return {}


def _deletable(monkeypatch, s3, placement=None):
    client = _client()
    monkeypatch.setattr(knowledge, "agent_client", lambda _ws=None: client)
    monkeypatch.setattr(knowledge, "_list_data_sources", lambda c, kb: [])
    monkeypatch.setattr(knowledge, "_delete_kb_policy", lambda *a, **k: None)
    monkeypatch.setattr(aws_clients, "client", lambda *a, **k: s3)
    if placement is not None:
        monkeypatch.setattr(project_kb, "placement_of", lambda ws, d: placement)
    return client


def test_delete_purges_the_kb_upload_prefix_only(monkeypatch):
    s3 = FakeS3(["kb/KB1/a.md", "kb/KB1/sub/b.pdf", "kb/KB10/c.md", "skills/x/SKILL.md"])
    client = _deletable(monkeypatch, s3)
    out = knowledge.delete_kb(ws_ctx({"artifacts_bucket": "bkt"}), "KB1")
    client.delete_knowledge_base.assert_called_once_with(knowledgeBaseId="KB1")
    assert sorted(s3.deleted) == ["kb/KB1/a.md", "kb/KB1/sub/b.pdf"]
    assert out["files_removed"] == 2


def test_a_project_kb_purges_both_its_prefix_and_a_migrated_original(monkeypatch):
    s3 = FakeS3(["kb/KB1/old.md", "projects/p1/kb/KB1/new.md", "projects/p1/kb/KB2/x.md"])
    placement = project_kb.KbPlacement(role_arn="arn:role", root="projects/p1/kb/",
                                       project_id="p1")
    _deletable(monkeypatch, s3, placement)
    out = knowledge.delete_kb(ws_ctx({"artifacts_bucket": "bkt"}), "KB1")
    assert sorted(s3.deleted) == ["kb/KB1/old.md", "projects/p1/kb/KB1/new.md"]
    assert out["files_removed"] == 2


def test_a_purge_failure_is_logged_and_never_fails_the_delete(monkeypatch, caplog):
    s3 = FakeS3(["kb/KB1/a.md"], fail_prefix="kb/KB1/")
    _deletable(monkeypatch, s3)
    caplog.set_level(logging.WARNING, logger=knowledge.__name__)
    out = knowledge.delete_kb(ws_ctx({"artifacts_bucket": "bkt"}), "KB1")
    assert out["deleted"] is True and out["files_removed"] == 0
    assert any("not purged" in r.getMessage() for r in caplog.records)
