"""D21 (M1-d): the embedding row may change only while every vector index is empty."""

import pytest

from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.core.errors import AppError
from app.models.ledger import Workspace
from app.schemas import model_catalog
from app.services import embedding_guard, model_registry
from app.services import opensearch_http as core

SUBSTRATE = {"memory_os_endpoint": "search-a.example", "memory_id": "launchpad_memory-default",
             "memory_bucket": "b", "memory_queue_url": "q", "memory_prefix": "memory/"}


@pytest.fixture(autouse=True)
def _reset():
    yield
    model_registry.set_embedding_guard(None)
    model_catalog.reset()


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def _workspace(db, wid, resources, account="111111111111"):
    row = db.get(Workspace, wid)
    if row is None:
        row = Workspace(id=wid, name=wid, account_id=account, region="cn-northwest-1")
        db.add(row)
    row.resources = dict(resources)
    db.commit()
    return row


class FakeOS:
    """`_cat/indices/<pattern>?format=json` answering scripted rows; 404 when none match."""

    def __init__(self, rows_by_pattern):
        self.rows_by_pattern = rows_by_pattern
        self.calls = []

    def request(self, method, path, body=None, params=None):
        self.calls.append((method, path, params))
        pattern = path.rsplit("/", 1)[1]
        if pattern not in self.rows_by_pattern:
            raise core.OpenSearchError(404, "index_not_found_exception")
        return self.rows_by_pattern[pattern]


def _factory(fake):
    made = []

    def factory(ctx, endpoint):
        made.append((ctx.id, endpoint))
        return fake

    factory.made = made
    return factory


def _embedding_row(db):
    """A second embedding row (768 dims) under the seeded Bedrock provider."""
    seeded = model_registry.ensure_seeded(db)
    provider = next(p for p in model_registry._providers(db) if p.name == "bedrock")
    row = model_registry.create_model(db, provider, {
        "api_model_name": "amazon.titan-embed-text-v1", "kind": "embedding",
        "overrides": {"embedding_dimension": 768}}, actor="op")
    return seeded, provider, row


def test_zero_docs_everywhere_allows_the_change(db):
    _workspace(db, DEFAULT_WORKSPACE_ID, SUBSTRATE)
    fake = FakeOS({"launchpad-memory*": [{"index": "launchpad-memory", "docs.count": "0"}],
                   "launchpad-jobs*": [{"index": "launchpad-jobs", "docs.count": "12"}],
                   "launchpad-kb-*": [{"index": "launchpad-kb-docs", "docs.count": "0"},
                                      {"index": "launchpad-kb-kb1", "docs.count": "0"}]})
    counts = embedding_guard.index_doc_counts(db, factory=_factory(fake))
    # the jobs index holds no vectors and is never counted
    assert counts == [{"name": "launchpad-memory", "docs": 0},
                      {"name": "launchpad-kb-docs", "docs": 0},
                      {"name": "launchpad-kb-kb1", "docs": 0}]
    assert embedding_guard.guard(db, object(), factory=_factory(fake)) is None


def test_non_zero_docs_refuse_with_the_index_list_and_dimensions(db):
    _workspace(db, DEFAULT_WORKSPACE_ID, SUBSTRATE)
    _seeded, _provider, row = _embedding_row(db)
    fake = FakeOS({"launchpad-memory*": [{"index": "launchpad-memory", "docs.count": "3"}],
                   "launchpad-kb-*": [{"index": "launchpad-kb-docs", "docs.count": "0"},
                                      {"index": "launchpad-kb-kb1", "docs.count": "41"}]})
    with pytest.raises(AppError) as exc:
        embedding_guard.guard(db, row, factory=_factory(fake))
    err = exc.value
    assert err.code == "embedding.reindex_required" and err.status_code == 409
    assert err.detail["indices"] == [{"name": "launchpad-memory", "docs": 3},
                                     {"name": "launchpad-kb-kb1", "docs": 41}]
    assert err.detail["dimension_from"] == 1024 and err.detail["dimension_to"] == 768
    assert err.detail["model_from"] == core.EMBED_MODEL_ID
    assert "launchpad-kb-kb1 (41)" in str(err)


def test_one_count_per_shared_endpoint_and_no_substrate_means_no_call(db):
    _workspace(db, DEFAULT_WORKSPACE_ID, SUBSTRATE)
    _workspace(db, "t1", SUBSTRATE, account="222222222222")  # shared domain → counted once
    _workspace(db, "bare", {}, account="333333333333")  # no substrate → never asked
    fake = FakeOS({"launchpad-memory*": [{"index": "launchpad-memory", "docs.count": "0"}]})
    factory = _factory(fake)
    counts = embedding_guard.index_doc_counts(db, factory=factory)
    assert counts == [{"name": "launchpad-memory", "docs": 0}]
    assert factory.made == [(DEFAULT_WORKSPACE_ID, "search-a.example")]
    # the two other patterns answered 404 (nothing matches yet) and were treated as empty
    assert len(fake.calls) == 3


def test_guard_is_wired_into_the_registry_flip_and_disable(db):
    """Through `model_registry`: flipping the 768-dim row to default hits the guard; disabling
    a non-default embedding row never does (D21-obs1); an empty substrate lets the flip through."""
    _workspace(db, DEFAULT_WORKSPACE_ID, SUBSTRATE)
    _seeded, provider, row = _embedding_row(db)
    busy = FakeOS({"launchpad-memory*": [{"index": "launchpad-memory", "docs.count": "1"}]})
    embedding_guard.install()
    model_registry.set_embedding_guard(
        lambda d, r: embedding_guard.guard(d, r, factory=_factory(busy)))
    with pytest.raises(AppError) as exc:
        model_registry.update_model(db, provider, row, {"is_default": True}, actor="op")
    assert exc.value.code == "embedding.reindex_required"
    db.rollback()
    assert model_catalog.current().embedding_model().model_id == core.EMBED_MODEL_ID
    # D21-obs1 (rev-r8 on m1-d): disabling a NON-default embedding row is not an embedding
    # change — the busy substrate must not refuse it; the guard runs on the flip only
    model_registry.update_model(db, provider, row, {"enabled": False}, actor="op")
    assert row.enabled is False
    model_registry.update_model(db, provider, row, {"enabled": True}, actor="op")
    assert row.enabled is True
    empty = FakeOS({})
    model_registry.set_embedding_guard(
        lambda d, r: embedding_guard.guard(d, r, factory=_factory(empty)))
    model_registry.update_model(db, provider, row, {"is_default": True}, actor="op")
    assert model_catalog.current().embedding_model().embedding_dimension == 768


def test_install_registers_the_guard_slot():
    embedding_guard.install()
    assert model_registry._embedding_guard is embedding_guard.guard
