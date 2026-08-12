"""Where per-workspace state is written and cached.

Slice 3 moved the resource map's ownership onto the workspace row and keyed every
module-level cache by workspace. Both are invisible in a single-workspace world
and wrong the moment a second environment exists, so they get their own tests.
"""

from unittest.mock import MagicMock

import pytest

import app.routers.overview as overview_mod
from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.models.ledger import Workspace
from app.services import governance, kb_gateway
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


class TestLazyProvisioningWritesTheRow:
    """`ensure_kb_gateway_persisted` is the one resource created mid-request, so it
    is where "the row owns the resource map" is actually exercised."""

    def _control(self):
        control = MagicMock()
        control.list_gateways.return_value = {"items": []}
        control.create_gateway.return_value = {"gatewayId": "kbgw-1"}
        control.get_gateway.return_value = {
            "gatewayId": "kbgw-1",
            "gatewayArn": "arn:aws:bedrock-agentcore:eu-central-1:1:gateway/kbgw-1",
            "gatewayUrl": "https://kbgw-1.example/mcp",
            "status": "READY",
        }
        return control

    def test_a_non_default_workspace_records_the_ids_on_its_row(
        self, other_workspace, monkeypatch
    ):
        monkeypatch.setattr(kb_gateway, "_wait_gateway_ready", lambda *a, **k: None)
        db = SessionLocal()
        try:
            context = ws.workspace_context(db.get(Workspace, other_workspace))
        finally:
            db.close()
        context.resources.update(
            user_pool_id="pool", user_pool_client_id="client", gateway_role_arn="arn:role"
        )
        written: list[dict] = []
        monkeypatch.setattr(
            kb_gateway, "write_config", lambda payload: written.append(payload)
        )

        gateway = kb_gateway.ensure_kb_gateway_persisted(self._control(), context)

        assert gateway["id"] == "kbgw-1"
        assert _row_resources(other_workspace)["kb_gateway_id"] == "kbgw-1"
        assert _row_resources(other_workspace)["kb_gateway_url"].endswith("/mcp")
        # the in-memory context sees it too — the rest of this request reads it
        assert context.resources["kb_gateway_id"] == "kbgw-1"
        assert written == []  # the yaml mirror is for `default` only

    def test_the_default_workspace_also_mirrors_into_the_yaml(self, monkeypatch):
        """`make bootstrap` and the startup settings mirror both read the yaml; a
        row-only write would be overwritten on the next boot."""
        monkeypatch.setattr(kb_gateway, "_wait_gateway_ready", lambda *a, **k: None)
        written: list[dict] = []
        monkeypatch.setattr(
            kb_gateway, "write_config", lambda payload: written.append(payload)
        )
        monkeypatch.setattr(kb_gateway.get_settings, "cache_clear", lambda: None)
        context = ws_ctx(
            {
                "user_pool_id": "pool",
                "user_pool_client_id": "client",
                "gateway_role_arn": "arn:role",
            },
            id=DEFAULT_WORKSPACE_ID,
        )

        kb_gateway.ensure_kb_gateway_persisted(self._control(), context)

        assert _row_resources(DEFAULT_WORKSPACE_ID)["kb_gateway_id"] == "kbgw-1"
        assert written == [
            {
                "resources": {
                    "kb_gateway_id": "kbgw-1",
                    "kb_gateway_arn": (
                        "arn:aws:bedrock-agentcore:eu-central-1:1:gateway/kbgw-1"
                    ),
                    "kb_gateway_url": "https://kbgw-1.example/mcp",
                }
            }
        ]

    def test_an_already_provisioned_gateway_is_reused_without_a_write(self, monkeypatch):
        control = MagicMock()
        context = ws_ctx(
            {"kb_gateway_id": "kbgw-old", "kb_gateway_url": "https://old/mcp"}
        )

        gateway = kb_gateway.ensure_kb_gateway_persisted(control, context)

        assert gateway["id"] == "kbgw-old"
        control.create_gateway.assert_not_called()


class TestCachesAreKeyedByWorkspace:
    def test_a_dashboard_cached_for_one_workspace_is_not_served_to_another(self):
        obs.reset_cache()
        hub = ws_ctx(id=DEFAULT_WORKSPACE_ID)
        other = ws_ctx(id=OTHER, region="eu-central-1")
        logs = _CountingLogs()

        obs.get_dashboard("24h", hub, logs=logs)
        again = obs.get_dashboard("24h", hub, logs=logs)
        elsewhere = obs.get_dashboard("24h", other, logs=logs)

        assert again["cache"]["hit"] is True  # same workspace → cached
        assert elsewhere["cache"]["hit"] is False  # other workspace → its own query
        # one dashboard build runs five Insights queries; two builds ran, not three
        assert logs.queries == 10
        obs.reset_cache()

    def test_the_overview_tiles_cache_per_workspace(self, monkeypatch):
        overview_mod._cache.clear()
        seen: list[str] = []

        def listed(context):
            seen.append(context.id)
            return [{"recordId": "r1", "descriptorType": "A2A", "status": "APPROVED"}]

        monkeypatch.setattr(overview_mod, "console_list", listed)
        hub = ws_ctx(id=DEFAULT_WORKSPACE_ID)

        overview_mod._registry_assets(hub)
        overview_mod._registry_assets(hub)
        overview_mod._registry_assets(ws_ctx(id=OTHER))

        assert seen == [DEFAULT_WORKSPACE_ID, OTHER]
        overview_mod._cache.clear()

    def test_the_attached_engine_is_cached_per_workspace(self):
        governance._engine_cache.clear()
        control = MagicMock()
        control.get_gateway.return_value = {
            "policyEngineConfiguration": {
                "arn": "arn:aws:bedrock-agentcore:us-west-2:1:policy-engine/launchpad_pe-a"
            }
        }
        control.list_policy_engines.return_value = {
            "policyEngines": [
                {"policyEngineId": "launchpad_pe-a", "name": "launchpad_pe"}
            ]
        }
        hub = ws_ctx({"gateway_id": "gw-hub"}, id=DEFAULT_WORKSPACE_ID)
        other = ws_ctx({"gateway_id": "gw-euc1"}, id=OTHER)

        governance.attached_policy_engine_id(control, hub)
        governance.attached_policy_engine_id(control, hub)
        governance.attached_policy_engine_id(control, other)

        asked = [c.kwargs["gatewayIdentifier"] for c in control.get_gateway.call_args_list]
        assert asked == ["gw-hub", "gw-euc1"]
        governance._engine_cache.clear()


class TestGovernanceReadsTheLiveAttachment:
    """`resources["policy_engine_id"]` has had no writer since policy setup became
    opt-in, so a fresh workspace has no such key — the id comes off the Gateway."""

    def _control(self, engine_arn: str | None):
        control = MagicMock()
        control.get_gateway.return_value = {
            "policyEngineConfiguration": {"arn": engine_arn} if engine_arn else {}
        }
        control.list_policy_engines.return_value = {
            "policyEngines": (
                [{"policyEngineId": "launchpad_pe-a", "name": "launchpad_pe"}]
                if engine_arn
                else []
            )
        }
        return control

    def test_an_attached_engine_is_found_without_any_config_key(self):
        governance._engine_cache.clear()
        workspace = ws_ctx({"gateway_id": "gw-1"})  # no policy_engine_id anywhere

        engine_id = governance.require_attached_policy_engine_id(
            self._control(
                "arn:aws:bedrock-agentcore:us-west-2:1:policy-engine/launchpad_pe-a"
            ),
            workspace,
        )

        assert engine_id == "launchpad_pe-a"
        governance._engine_cache.clear()

    def test_no_attachment_is_the_not_bootstrapped_error(self):
        governance._engine_cache.clear()
        with pytest.raises(Exception) as error:
            governance.require_attached_policy_engine_id(
                self._control(None), ws_ctx({"gateway_id": "gw-1"})
            )
        assert error.value.code == "policy.not_bootstrapped"
        governance._engine_cache.clear()

    def test_a_workspace_without_a_gateway_reports_unconfigured(self):
        governance._engine_cache.clear()
        assert governance.attached_policy_engine_id(MagicMock(), ws_ctx()) == ""

    def test_the_policies_route_answers_on_a_freshly_bootstrapped_workspace(
        self, client, monkeypatch
    ):
        """The regression this replaces: every fresh workspace 503'd forever because
        nothing writes `policy_engine_id` any more."""
        import app.routers.governance as governance_router

        from .conftest import set_default_resources

        governance._engine_cache.clear()
        set_default_resources({"gateway_id": "gw-1"})
        control = self._control(
            "arn:aws:bedrock-agentcore:us-west-2:1:policy-engine/launchpad_pe-a"
        )
        control.get_gateway.return_value = {
            "policyEngineConfiguration": {
                "arn": "arn:aws:bedrock-agentcore:us-west-2:1:policy-engine/launchpad_pe-a",
                "mode": "ENFORCE",
            }
        }
        control.list_policies.return_value = {"policies": []}
        monkeypatch.setattr(
            governance_router, "control_client", lambda _ws=None: control
        )
        monkeypatch.setattr(
            governance_router.policy_api,
            "find_policy_engine",
            lambda _c, engine_id: {
                "policyEngineId": engine_id,
                "policyEngineArn": (
                    "arn:aws:bedrock-agentcore:us-west-2:1:policy-engine/launchpad_pe-a"
                ),
                "name": "launchpad_pe",
                "status": "READY",
            },
        )

        body = client.get("/api/governance/policies")

        assert body.status_code == 200, body.text
        assert body.json()["engine"]["id"] == "launchpad_pe-a"
        assert body.json()["engine"]["attached"] is True
        governance._engine_cache.clear()


class _CountingLogs:
    """Logs Insights stub that reports how many query rounds it served."""

    def __init__(self) -> None:
        self.queries = 0

    def start_query(self, **_kwargs):
        self.queries += 1
        return {"queryId": f"q{self.queries}"}

    def get_query_results(self, queryId):  # noqa: N803 — botocore casing
        return {"status": "Complete", "results": []}
