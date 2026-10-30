"""Attachables catalog (APPROVED MCP + AGENT_SKILLS only) joined to live Gateways of the
workspace region, the sync-defaults route, and the assistant catalog wiring."""

import json
from unittest.mock import MagicMock

import pytest

import app.routers.registry as registry_router
from app.services import aws_clients
from app.services import registry_backend as reg
from app.services import registry_console as console
from app.services.registry_local import LocalRegistryBackend
from tests.conftest import set_default_resources, ws_ctx

GW_URL = "https://abc123.gateway.bedrock-agentcore.cn-northwest-1.amazonaws.com.cn/mcp"
RESOURCES = {"artifacts_bucket": "bkt", "gateway_url": GW_URL, "gateway_id": "abc123",
             "registry_id": "local-default"}
WS = ws_ctx(RESOURCES, region="cn-northwest-1")


def _gateway(gateway_id="abc123", url=GW_URL, authorizer="AWS_IAM", name="launchpad-gw"):
    return {"gatewayId": gateway_id, "gatewayArn": f"arn:gw/{gateway_id}", "name": name,
            "gatewayUrl": url, "protocolType": "MCP", "authorizerType": authorizer}


def _mcp(backend, name, url, tools=None, approve=True):
    record, _ = backend.upsert_record(
        WS, name=name, description=f"{name} server", descriptor_type="MCP",
        descriptors=reg.build_mcp_descriptors(target=name, description="d", gateway_url=url,
                                              tools=tools),
    )
    if approve:
        backend.submit_record(WS, record["recordId"])
        backend.approve_record(WS, record["recordId"])
    return record["recordId"]


def _skill(backend, name, path="s3://bkt/skills/x/", approve=True):
    record, _ = backend.upsert_record(
        WS, name=name, description="skill", descriptor_type="AGENT_SKILLS",
        descriptors=reg.build_skills_descriptors(
            skill_md=f"---\nname: {name}\n---\n# x",
            definition={"name": name, "description": "summarize", "path": path}),
    )
    if approve:
        backend.submit_record(WS, record["recordId"])
        backend.approve_record(WS, record["recordId"])
    return record["recordId"]


@pytest.fixture
def backend(client):
    backend = LocalRegistryBackend()
    reg.set_backend(backend)
    registry_router._attachables_cache.clear()
    return backend


def test_gateway_endpoint_detection_is_partition_and_region_aware():
    assert console.gateway_endpoint(GW_URL, WS) == "abc123"
    assert console.gateway_endpoint("https://mcp.deepwiki.com/mcp", WS) is None
    global_url = "https://abc123.gateway.bedrock-agentcore.us-west-2.amazonaws.com/mcp"
    assert console.gateway_endpoint(global_url, WS) is None  # another partition/region
    assert console.gateway_endpoint(global_url, ws_ctx(region="us-west-2")) == "abc123"


def test_only_approved_mcp_and_skill_records_are_offered(backend):
    gw = _mcp(backend, "hr-database", GW_URL,
              tools=[{"name": "hr-database___get_employee", "description": "", "inputSchema": {}}])
    remote = _mcp(backend, "deepwiki", "https://mcp.deepwiki.com/mcp")
    _mcp(backend, "draft-server", "https://draft.example/mcp", approve=False)
    skill = _skill(backend, "meeting-summarizer", "s3://bkt/skills/meeting-summarizer/")
    _skill(backend, "pending-skill", approve=False)
    backend.upsert_record(WS, name="broken", description="", descriptor_type="MCP",
                          descriptors={"mcp": {}})

    catalog = console.attachable_records(WS, gateways=[_gateway()])

    by_name = {m["name"]: m for m in catalog["mcp_servers"]}
    assert set(by_name) == {"hr-database", "deepwiki"}
    assert by_name["hr-database"] == {
        "name": "hr-database", "description": "hr-database server", "url": GW_URL,
        "gateway": True, "record_id": gw, "runtime_tools": ["hr-database___get_employee"],
        "gateway_id": "abc123", "gateway_arn": "arn:gw/abc123", "gateway_name": "launchpad-gw",
        "attachable": True, "attachability_reason": None, "auth_type": "aws_iam",
    }
    assert by_name["deepwiki"] == {
        "name": "deepwiki", "description": "deepwiki server", "url": "https://mcp.deepwiki.com/mcp",
        "gateway": False, "record_id": remote, "gateway_id": None, "gateway_arn": None,
        "attachable": True, "attachability_reason": None, "auth_type": "none",
    }
    assert catalog["skills"] == [{
        "name": "meeting-summarizer", "description": "summarize",
        "path": "s3://bkt/skills/meeting-summarizer/", "record_id": skill,
    }]


def test_unresolved_gateway_endpoint_is_never_an_unauthenticated_server(backend):
    _mcp(backend, "ghost", GW_URL.replace("abc123", "gone999"))
    (row,) = console.attachable_records(WS, gateways=[_gateway()])["mcp_servers"]
    assert row["gateway"] is True and row["attachable"] is False and row["auth_type"] is None
    assert "could not be resolved" in row["attachability_reason"]


def test_non_iam_gateway_is_listed_unattachable(backend):
    _mcp(backend, "jwt-gw", GW_URL)
    (row,) = console.attachable_records(WS, gateways=[_gateway(authorizer="CUSTOM_JWT")])[
        "mcp_servers"]
    assert row["attachable"] is False and row["auth_type"] == "oauth"
    assert row["attachability_reason"].startswith("Gateway outbound auth")
    (row,) = console.attachable_records(WS, gateways=[_gateway(authorizer="NONE")])["mcp_servers"]
    assert row["attachable"] is True and row["auth_type"] == "none"


def test_attachable_records_never_leak_another_workspaces_records(backend):
    """APPROVED records of workspace B must never surface in workspace A's catalog —
    the registry lifecycle gate (R8) is per-workspace, not global."""
    other_ws = ws_ctx({"artifacts_bucket": "bkt"}, id="acct-usw1", region="cn-northwest-1")
    _mcp(backend, "hr-database", GW_URL)
    _skill(backend, "meeting-summarizer")
    record, _ = backend.upsert_record(
        other_ws, name="other-ws-tool", description="d", descriptor_type="MCP",
        descriptors=reg.build_mcp_descriptors(
            target="other-ws-tool", description="d",
            gateway_url="https://mcp.deepwiki.com/mcp", tools=None),
    )
    backend.submit_record(other_ws, record["recordId"])
    backend.approve_record(other_ws, record["recordId"])

    catalog_a = console.attachable_records(WS, gateways=[_gateway()])
    catalog_b = console.attachable_records(other_ws, gateways=[])

    assert "other-ws-tool" not in {m["name"] for m in catalog_a["mcp_servers"]}
    assert {m["name"] for m in catalog_b["mcp_servers"]} == {"other-ws-tool"}
    assert catalog_b["skills"] == []  # workspace A's skill never leaks either


def test_live_gateways_come_from_the_workspace_control_plane(backend, monkeypatch):
    control = MagicMock()
    control.list_gateways.return_value = {"items": [{"gatewayId": "abc123", "protocolType": "MCP"}]}
    control.get_gateway.return_value = _gateway()
    monkeypatch.setattr(console, "control_client", lambda _ws: control)
    _mcp(backend, "hr-database", GW_URL)
    catalog = console.attachable_records(WS)
    assert catalog["mcp_servers"][0]["gateway_id"] == "abc123"
    assert control.get_gateway.call_count == 1


def test_attachables_route_caches_per_workspace_and_refreshes(client, backend, monkeypatch):
    set_default_resources(RESOURCES)
    calls = []

    def fake(_ws):
        calls.append(1)
        return {"mcp_servers": [], "skills": []}

    monkeypatch.setattr(console, "attachable_records", fake)
    assert client.get("/api/registry/attachables").json() == {"mcp_servers": [], "skills": []}
    client.get("/api/registry/attachables")
    assert calls == [1]
    client.get("/api/registry/attachables?refresh=true")
    assert calls == [1, 1]


def test_sync_defaults_registers_targets_and_the_sample_skill(client, backend, monkeypatch):
    set_default_resources(RESOURCES)
    uploaded: list[str] = []
    s3 = MagicMock()
    s3.upload_file.side_effect = lambda src, bucket, key: uploaded.append(key)
    monkeypatch.setattr(aws_clients, "client", lambda *a, **k: s3)
    monkeypatch.setattr(console.mcp_client, "tools_list", lambda _ws: [
        {"name": "hr-database___get_employee", "description": "x", "inputSchema": {}},
        {"name": "hr-database___list_teams", "description": "y", "inputSchema": {}},
        {"name": "x_amz_bedrock_agentcore_search", "description": "", "inputSchema": {}},
    ])
    res = client.post("/api/registry/sync-defaults")
    assert res.status_code == 200, res.text
    results = res.json()["results"]
    assert [(r["name"], r["type"], r["created"]) for r in results] == [
        ("hr-database", "MCP", True), ("expense-report-writer", "AGENT_SKILLS", True)]
    assert "skills/expense-report-writer/SKILL.md" in uploaded
    assert [r["status"] for r in results] == ["APPROVED", "PENDING_APPROVAL"]
    records = {r["name"]: r for r in backend.list_records(WS)}
    # N10-bug1: the platform's own gateway target is system-owned → APPROVED on import,
    # decided by "system" with the platform reason; the member-style skill still queues
    assert records["hr-database"]["status"] == "APPROVED"
    assert records["hr-database"]["decidedBy"] == console.PLATFORM_TARGET_ACTOR == "system"
    assert records["hr-database"]["statusReason"] == console.PLATFORM_TARGET_REASON
    tools = json.loads(records["hr-database"]["descriptors"]["mcp"]["tools"]["inlineContent"])
    assert [t["name"] for t in tools["tools"]] == ["hr-database___get_employee",
                                                   "hr-database___list_teams"]
    assert records["expense-report-writer"]["status"] == "PENDING_APPROVAL"
    assert records["expense-report-writer"]["decidedBy"] is None
    # second sync: everything reused, statuses untouched (a disabled target stays disabled)
    backend.disable_record(WS, records["hr-database"]["recordId"])
    again = client.post("/api/registry/sync-defaults").json()["results"]
    assert all(r["created"] is False for r in again)
    assert [r["status"] for r in again] == ["DEPRECATED", "PENDING_APPROVAL"]
    assert backend.get_record(WS, records["hr-database"]["recordId"])["status"] == "DEPRECATED"


def test_sync_defaults_reconciles_platform_targets_left_pending_by_an_upgrade(
    client, backend, monkeypatch
):
    """N11-r3-bug1 — the live default env had imported its gateway-target records as
    PENDING_APPROVAL before r2-B auto-approved NEW ones, so bootstrap kept reporting
    ``office-facts already imported (… · PENDING_APPROVAL)``. Every run now reconciles a
    platform-owned target still in DRAFT / PENDING_APPROVAL to APPROVED (decidedBy
    system); a member's record under a target's name, an approved record and a disabled
    one are never touched; the second run is a no-op."""
    set_default_resources(RESOURCES)
    monkeypatch.setattr(aws_clients, "client", lambda *a, **k: MagicMock())
    monkeypatch.setattr(console.mcp_client, "tools_list", lambda _ws: [
        {"name": "office-facts___lookup", "description": "x", "inputSchema": {}},
        {"name": "hr-database___get_employee", "description": "y", "inputSchema": {}},
        {"name": "payroll___run", "description": "z", "inputSchema": {}},
        {"name": "legacy___ping", "description": "w", "inputSchema": {}},
    ])
    # the upgraded state: pre-r2-B imports, one submitted, one still DRAFT
    pending = _mcp(backend, "office-facts", GW_URL, approve=False)
    backend.submit_record(WS, pending)
    draft = _mcp(backend, "hr-database", GW_URL, approve=False)
    # a member registered THEIR OWN server under a target's name (external URL)
    member = console.register_mcp_server(WS, "payroll", "d", "https://mcp.example/mcp")
    # an admin retired this target: a decision, not an upgrade residue
    disabled = _mcp(backend, "legacy", GW_URL)
    backend.disable_record(WS, disabled)
    lines: list[str] = []

    results = console.ensure_default_records(WS, auto_approve=True, log=lines.append)

    by_name = {r["name"]: r for r in results}
    assert (by_name["office-facts"]["reconciled"], by_name["office-facts"]["status"]) == (
        True, "APPROVED")
    assert (by_name["hr-database"]["reconciled"], by_name["hr-database"]["status"]) == (
        True, "APPROVED")
    assert (by_name["payroll"]["reconciled"], by_name["payroll"]["status"]) == (False, "DRAFT")
    assert (by_name["legacy"]["reconciled"], by_name["legacy"]["status"]) == (
        False, "DEPRECATED")
    assert all(r["created"] is False for r in results if r["type"] == "MCP")
    for record_id in (pending, draft):
        record = backend.get_record(WS, record_id)
        assert record["status"] == "APPROVED"
        assert record["decidedBy"] == console.PLATFORM_TARGET_ACTOR == "system"
        assert record["statusReason"] == console.PLATFORM_TARGET_REASON
    assert backend.get_record(WS, member["recordId"])["status"] == "DRAFT"
    assert backend.get_record(WS, member["recordId"])["decidedBy"] is None
    assert backend.get_record(WS, disabled)["status"] == "DEPRECATED"
    assert any("office-facts" in line and "reconciled" in line for line in lines)
    assert any("payroll" in line and "left alone" in line for line in lines)

    # idempotent: nothing left to reconcile, statuses stay
    again = {r["name"]: r for r in console.ensure_default_records(WS, auto_approve=True)}
    assert all(r["reconciled"] is False for r in again.values())
    assert [again[n]["status"] for n in ("office-facts", "hr-database", "payroll", "legacy")] == [
        "APPROVED", "APPROVED", "DRAFT", "DEPRECATED"]


def _plant_skill_row(name, record_type, version, status, created_at, definition_version):
    """A duplicate the way a live env ended up with it — written to the ledger directly
    (the backend's own upsert can no longer create one)."""
    from datetime import UTC, datetime

    from app.core.db import SessionLocal
    from app.models.ledger import RegistryRecord

    db = SessionLocal()
    try:
        row = RegistryRecord(
            workspace_id=WS.id, record_type=record_type, name=name, description="dup",
            record_version=version, status=status,
            descriptors=reg.build_skills_descriptors(
                skill_md="# x", definition={"name": name, "version": definition_version}),
            created_at=datetime.fromisoformat(created_at).replace(tzinfo=UTC),
        )
        db.add(row)
        db.commit()
        return row.id
    finally:
        db.close()


def test_sample_skill_seeding_is_idempotent_and_removes_duplicates(backend, monkeypatch):
    """N11-obs31: every bootstrap after a release change added a PENDING_APPROVAL copy of
    the sample skill. Seeding is keyed by name + kind over the workspace's records (a
    legacy GA-typed row counts), duplicates are removed keeping the oldest APPROVED
    record (else the oldest), and a bundle version change refreshes the kept record's
    content (back into review) instead of creating another record."""
    set_default_resources(RESOURCES)
    monkeypatch.setattr(aws_clients, "client", lambda *a, **k: MagicMock())
    monkeypatch.setattr(console.mcp_client, "tools_list", lambda _ws: [])
    bundle = console.upload_skill_bundle(WS)  # S3 is a MagicMock; the files are the repo's
    shipped = bundle["definition"]["version"]
    monkeypatch.setattr(console, "upload_skill_bundle", lambda ws, skill_name=None: bundle)

    first = console.ensure_default_records(WS, auto_approve=True)[-1]
    assert first["created"] is True and first["status"] == "PENDING_APPROVAL"
    rid = first["record_id"]
    # unchanged re-run: the same record, nothing created, nothing removed
    again = console.ensure_default_records(WS, auto_approve=True)[-1]
    assert again["record_id"] == rid
    assert (again["created"], again["updated"], again["removed_duplicates"]) == (False, False, [])

    # the live shape: the original got approved later; two leftover PENDING copies from
    # earlier releases exist — one OLDER than the approved one, one under the GA type name
    backend.approve_record(WS, rid)
    older = _plant_skill_row(console.SKILL_NAME, "AGENT_SKILLS", "0.9.0", "PENDING_APPROVAL",
                             "2026-01-01T00:00:00", "0.9.0")
    legacy = _plant_skill_row(console.SKILL_NAME, "SKILL", "0.8.0", "PENDING_APPROVAL",
                              "2026-02-01T00:00:00", "0.8.0")
    assert len(console.sample_skill_records(backend, WS)) == 3
    lines: list[str] = []
    result = console.ensure_default_records(WS, auto_approve=True, log=lines.append)[-1]
    assert result["record_id"] == rid and result["status"] == "APPROVED"  # approved wins
    assert sorted(result["removed_duplicates"]) == sorted([older, legacy])
    assert result["created"] is False and result["updated"] is False
    assert [r["recordId"] for r in console.sample_skill_records(backend, WS)] == [rid]
    assert sum("duplicate" in line and "removed" in line for line in lines) == 2

    # a release change: the bundle's version moves → the kept record is refreshed and
    # re-enters review; no new record
    newer = {**bundle, "definition": {**bundle["definition"], "version": "9.9.9"}}
    monkeypatch.setattr(console, "upload_skill_bundle", lambda ws, skill_name=None: newer)
    result = console.ensure_default_records(WS, auto_approve=True)[-1]
    assert (result["record_id"], result["created"], result["updated"]) == (rid, False, True)
    # approved before → approved after the refresh, with the new version (lead ruling:
    # platform-shipped content is re-approved by `system`, never parked in review)
    assert result["status"] == "APPROVED"
    record = backend.get_record(WS, rid)
    assert record["status"] == "APPROVED"
    assert record["decidedBy"] == console.PLATFORM_TARGET_ACTOR == "system"
    assert record["statusReason"] == console.PLATFORM_TARGET_REASON
    assert json.loads(record["descriptors"]["agentSkills"]["skillDefinition"]["inlineContent"])[
        "version"] == "9.9.9" != shipped
    assert len(console.sample_skill_records(backend, WS)) == 1
    # and the refreshed state is stable
    result = console.ensure_default_records(WS, auto_approve=True)[-1]
    assert (result["updated"], result["removed_duplicates"]) == (False, [])

    # no approved copy: the oldest survives
    backend.delete_record(WS, rid)
    a = _plant_skill_row(console.SKILL_NAME, "AGENT_SKILLS", "1.0.0", "PENDING_APPROVAL",
                         "2026-03-01T00:00:00", "9.9.9")
    b = _plant_skill_row(console.SKILL_NAME, "AGENT_SKILLS", "1.1.0", "DRAFT",
                         "2026-03-02T00:00:00", "9.9.9")
    result = console.ensure_default_records(WS, auto_approve=True)[-1]
    assert result["record_id"] == a and result["removed_duplicates"] == [b]
    # a record that was NOT approved stays in review after a refresh
    newest = {**bundle, "definition": {**bundle["definition"], "version": "10.0.0"}}
    monkeypatch.setattr(console, "upload_skill_bundle", lambda ws, skill_name=None: newest)
    result = console.ensure_default_records(WS, auto_approve=True)[-1]
    assert (result["record_id"], result["updated"], result["status"]) == (
        a, True, "PENDING_APPROVAL")
    assert backend.get_record(WS, a)["decidedBy"] is None


def test_imported_gateway_targets_are_what_the_wizard_lists(client, backend, monkeypatch):
    """The Create-Agent wizard reads APPROVED records (`/records?status=APPROVED` and
    `/attachables`): right after the import both carry the platform targets, with no
    human approval in between (N10-bug1 repro: "No approved gateway targets")."""
    set_default_resources(RESOURCES)
    monkeypatch.setattr(aws_clients, "client", lambda *a, **k: MagicMock())
    monkeypatch.setattr(console.mcp_client, "tools_list", lambda _ws: [
        {"name": "hr-database___get_employee", "description": "x", "inputSchema": {}},
        {"name": "office-facts___lookup", "description": "y", "inputSchema": {}},
    ])
    monkeypatch.setattr(console, "_list_live_mcp_gateways", lambda _ws: [_gateway()])

    client.post("/api/registry/sync-defaults")

    approved = client.get("/api/registry/records?status=APPROVED").json()["records"]
    assert sorted(r["name"] for r in approved) == ["hr-database", "office-facts"]
    assert {r["type"] for r in approved} == {"MCP"}
    pending = client.get("/api/registry/records?status=PENDING_APPROVAL").json()["records"]
    assert [r["name"] for r in pending] == ["expense-report-writer"]
    catalog = client.get("/api/registry/attachables").json()
    servers = {s["name"]: s for s in catalog["mcp_servers"]}
    assert set(servers) == {"hr-database", "office-facts"}
    assert servers["hr-database"]["gateway_id"] == "abc123"
    assert servers["hr-database"]["attachable"] is True
    assert servers["hr-database"]["runtime_tools"] == ["hr-database___get_employee"]
    assert catalog["skills"] == []  # the skill still awaits a human approval


def test_a_member_registered_mcp_server_still_needs_approval(client, backend):
    """Auto-approval is for the platform's own gateway targets only: an MCP server a
    member registers starts DRAFT and goes through submit → approve."""
    record = console.register_mcp_server(WS, "team-mcp", "d", "https://mcp.example/mcp")
    assert record["status"] == "DRAFT"
    assert record["decidedBy"] is None


def test_sync_defaults_without_a_gateway_imports_only_the_skill(client, backend, monkeypatch):
    """A spoke's gateway starts empty and a build may lack the gateway altogether: no
    MCP import, no tools/list call, the sample skill still lands."""
    set_default_resources({"artifacts_bucket": "bkt", "registry_id": "local-default"})
    monkeypatch.setattr(aws_clients, "client", lambda *a, **k: MagicMock())
    monkeypatch.setattr(console.mcp_client, "tools_list",
                        lambda _ws: pytest.fail("tools/list must not be called"))
    lines: list[str] = []

    results = console.ensure_default_records(
        ws_ctx({"artifacts_bucket": "bkt"}, region="cn-northwest-1"), log=lines.append,
        auto_approve=True)

    assert [(r["name"], r["type"], r["created"], r["status"]) for r in results] == [
        ("expense-report-writer", "AGENT_SKILLS", True, "PENDING_APPROVAL")]
    assert lines[0].startswith("no gateway_url in the resource map")


def test_assistant_catalog_reads_the_attachables(backend, monkeypatch):
    from app.assistant import service

    _mcp(backend, "hr-database", GW_URL)
    _skill(backend, "meeting-summarizer")
    monkeypatch.setattr(console, "attachable_records", _catalog_with_gateway)
    monkeypatch.setattr(service, "skill_content_snapshot", lambda ws, path: {
        "source_prefix": path, "content_digest": "d", "object_count": 1, "total_bytes": 1})
    catalog = service.fetch_catalog(WS)
    tools = {t["key"]: t for t in catalog["tools"]}
    assert tools["gateway:hr-database"]["attachable"] is True
    assert tools["gateway:hr-database"]["gateway_arn"] == "arn:gw/abc123"
    assert tools["gateway:hr-database"]["auth_type"] == "aws_iam"
    assert [s["name"] for s in catalog["skills"]] == ["meeting-summarizer"]


_real_attachables = console.attachable_records


def _catalog_with_gateway(ws):
    return _real_attachables(ws, gateways=[_gateway()])
