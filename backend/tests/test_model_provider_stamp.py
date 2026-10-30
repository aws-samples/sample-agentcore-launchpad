"""Catalog-version decoupling for owner-bound providers (N17-obs203 §5).

`model_registry.stamp` — the catalog version the preset snapshots (`preset_snapshot_behind`)
and `Deployment.catalog_version` compare against — must NOT move for a PERSONAL-scope provider's
changes: a member's own provider is invisible to the shared catalog, so an "architect preset is
behind" banner for it would be noise (the N17-obs150 / obs202 class). Only workspace-scope
changes bump it; a scope flip (sharing or unsharing) is journaled `workspace` and bumps once.
The snapshot's `revision` (`content_stamp`) still counts every row, so the 30 s refresher of a
second process reloads a personal provider all the same.
"""

from __future__ import annotations

import pytest

from app.core.db import SessionLocal
from app.schemas import model_catalog
from app.services import model_registry
from tests import test_visibility as tv

app, admin, ada = tv.app, tv.admin, tv.ada

PREFIX = "/api/settings/model-providers"
URL = "https://api.ppinfra.com/v3/openai"
pytestmark = pytest.mark.siliconflow


@pytest.fixture(autouse=True)
def _hermetic():
    yield
    model_registry.set_probe_hooks(None)
    model_catalog.reset()


def _version(client) -> int:
    res = client.get("/api/capabilities")
    assert res.status_code == 200
    return int(res.json()["catalog_version"])


def _stamps() -> tuple[int, int]:
    db = SessionLocal()
    try:
        return model_registry.stamp(db), model_registry.content_stamp(db)
    finally:
        db.close()


def test_a_personal_provider_and_its_models_leave_the_catalog_version_alone(admin, ada):
    _ada_id, member = ada
    version, content = _stamps()
    assert _version(admin) == version
    row = member.post(PREFIX, json={"name": "ppio", "kind": "openai_compatible", "label": "PPIO",
                                    "base_url": URL}).json()
    model = member.post(f"{PREFIX}/{row['id']}/models",
                        json={"api_model_name": "deepseek/deepseek-v4", "probe": False}).json()
    assert member.put(f"{PREFIX}/{row['id']}", json={"label": "PPIO!"}).status_code == 200
    assert member.put(f"{PREFIX}/{row['id']}/models/{model['id']}",
                      json={"label": "DS"}).status_code == 200
    after_version, after_content = _stamps()
    assert after_version == version  # the catalog version did not move …
    assert after_content > content  # … the content revision did (four rows)
    assert _version(admin) == version and _version(member) == version
    assert admin.get(PREFIX).json()["catalog_version"] == version
    # the published snapshot carries both
    assert model_catalog.current().version == version
    assert model_catalog.current().revision == after_content
    assert "ppio" in model_catalog.current().providers
    # deleting the personal provider does not move it either
    assert member.delete(f"{PREFIX}/{row['id']}").status_code == 204
    assert _stamps()[0] == version


def test_sharing_bumps_once_and_a_shared_providers_changes_keep_bumping(admin, ada):
    _ada_id, member = ada
    version = _stamps()[0]
    row = member.post(PREFIX, json={"name": "ppio", "kind": "openai_compatible", "label": "PPIO",
                                    "base_url": URL}).json()
    assert _stamps()[0] == version
    shared = admin.put(f"{PREFIX}/{row['id']}", json={"scope": "workspace"})
    assert shared.status_code == 200
    bumped = _stamps()[0]
    assert bumped > version  # sharing = the shared catalog gained the rows: one bump
    assert bumped == _version(admin)
    # a label edit on a shared row bumps (the shared catalog's labels render in pickers)
    assert admin.put(f"{PREFIX}/{row['id']}", json={"label": "PPIO shared"}).status_code == 200
    assert _stamps()[0] > bumped
    # unsharing bumps too (the shared catalog lost the rows), then personal edits stop bumping
    assert admin.put(f"{PREFIX}/{row['id']}", json={"scope": "personal"}).status_code == 200
    unshared = _stamps()[0]
    assert unshared > bumped
    # N17-obs225: sharing transferred the row to the workspace — after unsharing it is an
    # administrator's personal row (the former creator no longer sees it) and its personal
    # edits stop bumping
    assert member.get(f"{PREFIX}/{row['id']}").status_code == 404
    assert admin.put(f"{PREFIX}/{row['id']}", json={"label": "admins again"}).status_code == 200
    assert _stamps()[0] == unshared
    # an administrator's workspace-scope provider bumps on create, as before D21
    assert admin.post(PREFIX, json={"name": "acme", "kind": "openai_compatible", "label": "A",
                                    "base_url": URL}).status_code == 201
    assert _stamps()[0] > unshared


def test_the_journal_rows_carry_the_scope_they_were_judged_by(admin, ada):
    from sqlalchemy import select

    from app.models.model_registry import ModelCatalogChange

    _ada_id, member = ada
    row = member.post(PREFIX, json={"name": "ppio", "kind": "openai_compatible", "label": "PPIO",
                                    "base_url": URL}).json()
    admin.put(f"{PREFIX}/{row['id']}", json={"scope": "workspace"})
    admin.put(f"{PREFIX}/{row['id']}", json={"scope": "personal"})
    # N17-obs225: after the share → unshare round trip the row is an administrator's personal
    # row (ownership moved to the workspace on sharing); the member no longer sees it
    assert member.put(f"{PREFIX}/{row['id']}", json={"label": "x"}).status_code == 404
    admin.put(f"{PREFIX}/{row['id']}", json={"label": "x"})
    db = SessionLocal()
    try:
        rows = db.scalars(select(ModelCatalogChange).where(
            ModelCatalogChange.target_id == row["id"]).order_by(ModelCatalogChange.id)).all()
        assert [(r.operation, r.scope) for r in rows] == [
            ("provider.create", "personal"),
            ("provider.update", "workspace"),  # share
            ("provider.update", "workspace"),  # unshare — the shared catalog lost the rows
            ("provider.update", "personal"),
        ]
        # the seed and pre-obs203 rows (NULL scope) keep counting as catalog versions
        seed = db.scalar(select(ModelCatalogChange).where(ModelCatalogChange.operation == "seed"))
        assert seed.scope is None and model_registry.stamp(db) >= seed.id
    finally:
        db.close()


def test_a_second_process_still_reloads_a_personal_provider(admin, ada):
    """The refresher compares the content revision: a published snapshot that predates a
    member's personal provider is replaced even though the catalog version is unchanged."""
    _ada_id, member = ada
    member.post(PREFIX, json={"name": "ppio", "kind": "openai_compatible", "label": "PPIO",
                              "base_url": URL})
    fresh = model_catalog.current()
    assert "ppio" in fresh.providers
    # simulate the OTHER process: it published the pre-ppio snapshot (same version, older revision)
    without = {k: v for k, v in fresh.providers.items() if k != "ppio"}
    stale = model_catalog.Catalog(version=fresh.version, providers=without,
                                  models=dict(fresh.models), revision=fresh.revision - 1)
    model_catalog.publish(stale)
    assert "ppio" not in model_catalog.current().providers
    model_registry.install_refresher()
    reloaded = model_catalog.force_refresh()
    assert "ppio" in reloaded.providers and reloaded.version == fresh.version
    assert reloaded.revision == fresh.revision
