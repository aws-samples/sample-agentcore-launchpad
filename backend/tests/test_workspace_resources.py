"""Where per-workspace state is written and cached.

Slice 3 moved the resource map's ownership onto the workspace row and keyed every
module-level cache by workspace. Both are invisible in a single-workspace world
and wrong the moment a second environment exists, so they get their own tests.
"""


from types import SimpleNamespace

import pytest

import app.routers.overview as overview_mod
from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.models.ledger import Workspace
from app.services import bootstrap
from app.services import observability as obs
from app.services import workspace as ws

from .conftest import ws_ctx

OTHER = "acct-euc1"


@pytest.fixture
def other_workspace() -> str:
    db = SessionLocal()
    try:
        db.add(
            Workspace(
                id=OTHER,
                name="Frankfurt",
                account_id="444455556666",
                region="eu-central-1",
                bootstrap_status="ready",
                resources={"gateway_id": "gw-euc1"},
            )
        )
        db.commit()
    finally:
        db.close()
    return OTHER


def _row_resources(workspace_id: str) -> dict:
    db = SessionLocal()
    try:
        return dict(db.get(Workspace, workspace_id).resources or {})
    finally:
        db.close()


class TestMergeWorkspaceResources:
    """The default workspace's resources are re-converged from launchpad.yaml on
    every startup (core/db.init_db), so a row-only merge silently evaporates at
    the next restart — the trap that wiped prod's Skill Lab worker keys
    (2026-08-20). The merge itself now owns the yaml mirror."""

    def test_default_merge_lands_in_the_yaml_too(self, monkeypatch):
        import yaml as yaml_mod


        monkeypatch.setattr(ws.get_settings, "cache_clear", lambda: None)
        context = ws_ctx({}, id=DEFAULT_WORKSPACE_ID)
        ws.merge_workspace_resources(context, {"skill_lab_worker_runtime_arn": "arn:rt"})

        assert _row_resources(DEFAULT_WORKSPACE_ID)["skill_lab_worker_runtime_arn"] == "arn:rt"
        # conftest repoints bootstrap.CONFIG_FILE at a per-test temp file
        persisted = yaml_mod.safe_load(bootstrap.CONFIG_FILE.read_text())
        assert persisted["resources"]["skill_lab_worker_runtime_arn"] == "arn:rt"

    def test_non_default_merge_stays_off_the_yaml(self, other_workspace):

        db = SessionLocal()
        try:
            context = ws.workspace_context(db.get(Workspace, other_workspace))
        finally:
            db.close()
        ws.merge_workspace_resources(context, {"skill_lab_worker_runtime_arn": "arn:rt"})

        assert _row_resources(other_workspace)["skill_lab_worker_runtime_arn"] == "arn:rt"
        assert not bootstrap.CONFIG_FILE.exists()


class _NoMetrics:
    """CloudWatch stand-in for the dashboard's tokens-by-model fallback: no
    `gen_ai.client.token.usage` metric exists, so the fallback contributes nothing.
    Without it `get_dashboard` builds the workspace's real `cloudwatch` client through
    the funnel — the one call in this file that reached AWS (NoCredentialsError on a
    credential-less runner)."""

    def get_paginator(self, name: str):
        assert name == "list_metrics"
        return SimpleNamespace(paginate=lambda **_kw: [])


class TestCachesAreKeyedByWorkspace:
    def test_a_dashboard_cached_for_one_workspace_is_not_served_to_another(self):
        obs.reset_cache()
        hub = ws_ctx(id=DEFAULT_WORKSPACE_ID)
        other = ws_ctx(id=OTHER, region="eu-central-1")
        logs = _CountingLogs()
        cw = _NoMetrics()

        obs.get_dashboard("24h", hub, logs=logs, cw=cw)
        again = obs.get_dashboard("24h", hub, logs=logs, cw=cw)
        elsewhere = obs.get_dashboard("24h", other, logs=logs, cw=cw)

        assert again["cache"]["hit"] is True  # same workspace → cached
        assert elsewhere["cache"]["hit"] is False  # other workspace → its own query
        # one dashboard build runs five Insights queries; two builds ran, not three
        assert logs.queries == 10
        obs.reset_cache()

    def test_the_overview_tiles_cache_per_workspace(self, monkeypatch):
        overview_mod._cache.clear()
        seen: list[str] = []

        class _Registry:
            def list_records(self, context, descriptor_type=None, status=None):
                seen.append(context.id)
                return [{"recordId": "r1", "descriptorType": "A2A", "status": "APPROVED"}]

        from app.services import registry_backend

        registry_backend.set_backend(_Registry())
        hub = ws_ctx(id=DEFAULT_WORKSPACE_ID)

        overview_mod._registry_assets(hub)
        overview_mod._registry_assets(hub)
        overview_mod._registry_assets(ws_ctx(id=OTHER))

        assert seen == [DEFAULT_WORKSPACE_ID, OTHER]
        overview_mod._cache.clear()

class _CountingLogs:
    """Logs Insights stub that reports how many query rounds it served."""

    def __init__(self) -> None:
        self.queries = 0

    def start_query(self, **_kwargs):
        self.queries += 1
        return {"queryId": f"q{self.queries}"}

    def get_query_results(self, queryId):  # noqa: N803 — botocore casing
        return {"status": "Complete", "results": []}
