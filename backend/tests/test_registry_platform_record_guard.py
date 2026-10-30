"""N17-obs256 (rev-r23 review of cn/r17-am, 2026-10-22): a member could get a self-authored
MCP record APPROVED through the ``system``-actor reconcile — even over an administrator's
reject — by registering it under a not-yet-imported platform target's name with the
platform gateway's own URL, then calling ``POST /api/registry/sync-defaults`` (or importing
the platform gateway). ``is_platform_target_record`` was URL equality only.

Now (1) a platform target record must also have no creator — the platform paths create rows
outside any creator stamp — so the reconcile never adopts a member's record and the import
reports it as ``registry.name_held``; (2) the console register / edit routes refuse the
platform gateway URL (normalised: scheme/host case, trailing slash) with 422
``registry.platform_gateway_url_reserved`` for every caller. Real LocalRegistryBackend on
the hermetic ledger; the control plane and the gateway's tools/list are stubbed — no AWS.
"""

from __future__ import annotations

import pytest

from app.services import mcp_client, registry_console
from tests import test_visibility as tv
from tests.conftest import set_default_resources
from tests.test_registry_import_gateway import PLATFORM, PLATFORM_URL, TARGETS, _control

app, admin, ada, bob = tv.app, tv.admin, tv.ada, tv.bob

LIVE = [{"name": "office-facts___lookup", "description": "x", "inputSchema": {}}]
SAMPLE_SKILL = {
    "skill_md": "---\nname: expense-report-writer\n---\n",
    "definition": {"name": "expense-report-writer", "description": "", "version": "1.0.0",
                   "path": "s3://bkt/skills/expense-report-writer/", "files": []},
}


@pytest.fixture
def platform(monkeypatch, admin):
    set_default_resources({"gateway_url": PLATFORM_URL, "gateway_id": "gw-1",
                           "artifacts_bucket": "bkt"})
    monkeypatch.setattr(mcp_client, "tools_list", lambda _ws: list(LIVE))
    monkeypatch.setattr(registry_console, "upload_skill_bundle", lambda _ws: SAMPLE_SKILL)
    control = _control([PLATFORM], {"gw-1": TARGETS["gw-1"]})
    monkeypatch.setattr(registry_console, "control_client", lambda _ws: control)


def _squatted(created_by: str, name: str = "office-facts") -> str:
    """A member-authored MCP record carrying the platform gateway URL under a platform
    target's name — the state an install can already hold (registered before the URL
    refusal), so the reconcile guard is tested independently of the route refusal."""
    from app.models.ledger import RegistryRecord
    from app.services.registry_backend import build_mcp_descriptors

    return tv._add(RegistryRecord(
        record_type="MCP", name=name, description="member text", record_version="1.0.0-mcp",
        status="DRAFT", created_by=created_by, created_by_name="ada",
        descriptors=build_mcp_descriptors(
            target=name, description="member text", gateway_url=PLATFORM_URL, tools=None
        ),
    ))


def _act(client, rid: str, action: str):
    return client.post(f"/api/registry/records/{rid}/action", json={"action": action})


def _get(client, rid: str) -> dict:
    return client.get(f"/api/registry/records/{rid}").json()


# ---------- (1) the system reconcile never adopts a member's record ----------


def test_sync_defaults_leaves_a_member_record_under_the_platform_url_in_draft(
    platform, admin, ada
):
    """rev-r23 variant (a): DRAFT → member approve 403 → sync-defaults must NOT approve."""
    ada_id, ada_client = ada
    rid = _squatted(ada_id)
    assert _act(ada_client, rid, "approve").status_code == 403
    out = ada_client.post("/api/registry/sync-defaults")
    assert out.status_code == 200, out.text
    rec = _get(admin, rid)
    assert rec["status"] == "DRAFT", rec
    assert rec["description"] == "member text"
    row = next(r for r in out.json()["results"] if r["name"] == "office-facts")
    assert row["record_id"] == rid and row["reconciled"] is False and row["status"] == "DRAFT"
    # a description-only edit keeps it where it is — DRAFT, not APPROVED
    put = ada_client.put(f"/api/registry/records/{rid}", json={"description": "anything"})
    assert put.status_code == 200 and put.json()["status"] == "DRAFT"


def test_sync_defaults_does_not_override_an_administrators_reject(platform, admin, ada):
    """rev-r23 variant (b): submit → admin reject → member re-submit → sync-defaults must
    leave it PENDING_APPROVAL for an administrator."""
    ada_id, ada_client = ada
    rid = _squatted(ada_id)
    assert _act(ada_client, rid, "submit").json()["status"] == "PENDING_APPROVAL"
    assert _act(admin, rid, "reject").json()["status"] == "REJECTED"
    assert _act(ada_client, rid, "submit").json()["status"] == "PENDING_APPROVAL"
    assert ada_client.post("/api/registry/sync-defaults").status_code == 200
    rec = _get(admin, rid)
    assert rec["status"] == "PENDING_APPROVAL", rec
    assert rec["status_reason"] != registry_console.PLATFORM_TARGET_REASON


def test_platform_gateway_import_reports_a_member_record_as_name_held(platform, admin, ada):
    ada_id, ada_client = ada
    rid = _squatted(ada_id)
    _act(ada_client, rid, "submit")
    body = ada_client.post("/api/registry/import-gateway", json={}).json()
    item = next(r for r in body["results"] if r["name"] == "office-facts")
    assert item["error_code"] == registry_console.NAME_HELD_CODE
    assert item["record_id"] == rid and item["status"] == "PENDING_APPROVAL"
    assert item["held_by"]["origin"] == "member"  # never attributed to the gateway
    assert _get(admin, rid)["status"] == "PENDING_APPROVAL"
    # the preview says the same: held by a member's server, not imported
    preview = ada_client.get("/api/registry/import-gateway/preview").json()
    row = next(t for t in preview["gateways"][0]["targets"] if t["target"] == "office-facts")
    assert row["held"] is True and row["imported"] is False
    assert row["held_by"]["origin"] == "member"


def test_platform_records_are_still_created_approved_and_reconciled_by_system(
    platform, admin, ada
):
    """The platform path is unchanged for a trigger that may approve (an administrator's
    sync — N17-obs257): the target record is created with no creator, APPROVED by
    `system`; a creator-less stuck row is reconciled."""
    out = admin.post("/api/registry/sync-defaults").json()
    assert out["auto_approved"] is True
    row = next(r for r in out["results"] if r["name"] == "office-facts")
    assert row["created"] is True and row["status"] == "APPROVED"
    rec = _get(admin, row["record_id"])
    assert rec["created_by"] is None and rec["created_by_name"] is None

    stuck = _platform_row("weather", "PENDING_APPROVAL")
    LIVE.append({"name": "weather___now", "description": "x", "inputSchema": {}})
    try:
        again = admin.post("/api/registry/sync-defaults").json()["results"]
    finally:
        LIVE.pop()
    weather = next(r for r in again if r["name"] == "weather")
    assert weather["record_id"] == stuck and weather["reconciled"] is True
    assert _get(admin, stuck)["status"] == "APPROVED"


def _platform_row(name: str, status: str) -> str:
    """A creator-less platform target row (what the import path writes)."""
    from app.models.ledger import RegistryRecord
    from app.services.registry_backend import build_mcp_descriptors

    return tv._add(RegistryRecord(
        record_type="MCP", name=name, description="d", record_version="1.0.0-mcp",
        status=status, created_by=None, created_by_name=None,
        descriptors=build_mcp_descriptors(
            target=name, description="d", gateway_url=PLATFORM_URL, tools=None
        ),
    ))


# ---------- N17-obs257: only bootstrap or an administrator auto-approves ----------


def test_member_sync_only_submits_a_new_platform_target(platform, admin, ada):
    """User ruling 2026-10-22: a member-triggered sync creates the platform target record
    and SUBMITS it — PENDING_APPROVAL, no decision stamped, null creator kept."""
    _ada_id, ada_client = ada
    out = ada_client.post("/api/registry/sync-defaults").json()
    assert out["auto_approved"] is False
    row = next(r for r in out["results"] if r["name"] == "office-facts")
    assert row["created"] is True and row["status"] == "PENDING_APPROVAL"
    rec = _get(admin, row["record_id"])
    assert rec["status"] == "PENDING_APPROVAL" and rec["created_by"] is None
    # the record waits for the administrator; the admin's explicit approve publishes it
    assert _act(ada_client, row["record_id"], "approve").status_code == 403
    assert _act(admin, row["record_id"], "approve").json()["status"] == "APPROVED"


def test_a_member_chosen_endpoint_never_reaches_approved_through_a_member_sync(
    platform, admin, ada
):
    """rev-r23 round 2: members hold perm:identity.manage, so they can add an mcpServer
    target at any https endpoint to the PLATFORM gateway (Identity → gateway targets).
    Simulated here by that target appearing in the gateway's tools/list; a member's sync
    and a member's platform import both leave it PENDING_APPROVAL."""
    _ada_id, ada_client = ada
    LIVE.append({"name": "member-endpoint___exfil", "description": "x", "inputSchema": {}})
    try:
        out = ada_client.post("/api/registry/sync-defaults").json()["results"]
        row = next(r for r in out if r["name"] == "member-endpoint")
        assert row["status"] == "PENDING_APPROVAL"
        body = ada_client.post("/api/registry/import-gateway", json={}).json()
        assert body["auto_approved"] is False
        assert all(r["status"] != "APPROVED" for r in body["results"]), body
        assert _get(admin, row["record_id"])["status"] == "PENDING_APPROVAL"
        # and it shows in the administrator's pending queue
        pending = [r for r in admin.get("/api/registry/records").json()["records"]
                   if r["status"] == "PENDING_APPROVAL"]
        assert row["record_id"] in {r["record_id"] for r in pending}
    finally:
        LIVE.pop()


def test_member_import_of_the_platform_gateway_submits(platform, admin, ada):
    _ada_id, ada_client = ada
    body = ada_client.post("/api/registry/import-gateway", json={}).json()
    assert body["platform"] is True and body["auto_approved"] is False
    created = [r for r in body["results"] if r["created"]]
    assert created and all(r["status"] == "PENDING_APPROVAL" for r in created)
    admin_body = admin.post("/api/registry/import-gateway", json={}).json()
    assert admin_body["auto_approved"] is True
    # the administrator's import reconciles what the member's submitted
    assert all(r["status"] == "APPROVED" and r["reconciled"] for r in admin_body["results"]
               if r["name"] in {c["name"] for c in created})


def test_existing_pending_platform_row_untouched_by_member_promoted_by_admin(
    platform, admin, ada
):
    _ada_id, ada_client = ada
    stuck = _platform_row("office-facts", "PENDING_APPROVAL")
    before = _get(admin, stuck)
    row = next(r for r in ada_client.post("/api/registry/sync-defaults").json()["results"]
               if r["name"] == "office-facts")
    assert row["reconciled"] is False and row["status"] == "PENDING_APPROVAL"
    assert _get(admin, stuck)["updated_at"] == before["updated_at"]  # not touched
    row = next(r for r in admin.post("/api/registry/sync-defaults").json()["results"]
               if r["name"] == "office-facts")
    assert row["reconciled"] is True and row["status"] == "APPROVED"


def test_host_bootstrap_still_approves_the_platform_targets(platform, admin):
    """`make bootstrap` (ensure_registry_defaults) runs with no request and may approve."""
    from app.services import bootstrap as bs
    from tests.test_registry_import_gateway import _default_ws

    out = bs.ensure_registry_defaults(_default_ws(), log=lambda _m: None)
    office = next(r for r in out["records"] if r["name"] == "office-facts")
    assert office["created"] is True and office["status"] == "APPROVED"


def _bundle(version: str) -> dict:
    return {**SAMPLE_SKILL, "definition": {**SAMPLE_SKILL["definition"], "version": version}}


def _sample_result(client) -> dict:
    out = client.post("/api/registry/sync-defaults").json()["results"]
    return next(r for r in out if r["type"] == "AGENT_SKILLS")


def _backend_record(rid: str) -> dict:
    from app.services import registry_backend
    from tests.test_registry_import_gateway import _default_ws

    return registry_backend.get_backend().get_record(_default_ws(), rid)


# ---------- N17-obs262: the platform sample skill re-approves on every trigger ----------


def test_member_sync_after_a_bundle_bump_keeps_the_sample_skill_approved(
    platform, admin, ada, monkeypatch
):
    """User ruling N17-obs262 (2026-10-22): the sample skill's content is the platform's
    own (repo files), so a member-triggered refresh of an APPROVED record re-approves it
    through the platform path — `system` actor, platform reason — and it stays attachable.
    (Was obs257: re-submitted to PENDING_APPROVAL, dropped from deploys and the wizard.)"""
    from tests.test_registry_import_gateway import _default_ws

    _ada_id, ada_client = ada
    rid = _sample_result(admin)["record_id"]
    assert _act(admin, rid, "approve").json()["status"] == "APPROVED"
    for client, version in ((ada_client, "2.0.0"), (admin, "3.0.0")):
        monkeypatch.setattr(registry_console, "upload_skill_bundle",
                            lambda _ws, v=version: _bundle(v))
        run = _sample_result(client)
        assert (run["record_id"], run["updated"], run["status"]) == (rid, True, "APPROVED")
        rec = _backend_record(rid)
        assert rec["status"] == "APPROVED" and not rec.get("createdBy")
        assert rec["decidedBy"] == registry_console.PLATFORM_TARGET_ACTOR == "system"
        assert rec["statusReason"] == registry_console.PLATFORM_TARGET_REASON
        assert registry_console._skill_definition(rec)["version"] == version
        skills = registry_console.attachable_records(_default_ws(), gateways=[])["skills"]
        assert rid in {s["record_id"] for s in skills}
    # the host bootstrap path behaves the same and is idempotent once refreshed
    from app.services import bootstrap as bs

    out = bs.ensure_registry_defaults(_default_ws(), log=lambda _m: None)
    sample = next(r for r in out["records"] if r["type"] == "AGENT_SKILLS")
    assert (sample["record_id"], sample["status"]) == (rid, "APPROVED")


def test_a_new_sample_skill_on_a_member_trigger_is_only_submitted(platform, admin, ada):
    """obs257 stays for a NEW record: a member's sync creates the sample skill and submits
    it; only an administrator approves it."""
    _ada_id, ada_client = ada
    first = _sample_result(ada_client)
    assert (first["created"], first["status"], first["name_held"]) == (
        True, "PENDING_APPROVAL", False)
    assert _get(admin, first["record_id"])["created_by"] is None
    assert _act(ada_client, first["record_id"], "approve").status_code == 403
    assert _act(admin, first["record_id"], "approve").json()["status"] == "APPROVED"


def test_a_sample_skill_still_in_review_is_re_submitted_not_approved(
    platform, admin, ada, monkeypatch
):
    """A platform sample skill that was never approved stays in review after a refresh,
    whoever triggers it — the ruling covers only an APPROVED record."""
    _ada_id, ada_client = ada
    rid = _sample_result(ada_client)["record_id"]
    for client, version in ((ada_client, "2.0.0"), (admin, "3.0.0")):
        monkeypatch.setattr(registry_console, "upload_skill_bundle",
                            lambda _ws, v=version: _bundle(v))
        run = _sample_result(client)
        assert (run["updated"], run["status"]) == (True, "PENDING_APPROVAL")
        assert _backend_record(rid)["decidedBy"] is None


def _member_skill(status: str, version: str = "0.1.0") -> str:
    """A member-authored AGENT_SKILLS record under the sample skill's name (registered
    while the workspace had no sample record) — never the platform's."""
    from app.models.ledger import RegistryRecord
    from app.services.registry_backend import build_skills_descriptors

    name = registry_console.SKILL_NAME
    return tv._add(RegistryRecord(
        record_type="AGENT_SKILLS", name=name, description="member skill",
        record_version="1.0.0", status=status, created_by="ada-id", created_by_name="ada",
        descriptors=build_skills_descriptors(
            skill_md="# member", definition={"name": name, "version": version}),
    ))


@pytest.mark.parametrize("status", ["DRAFT", "PENDING_APPROVAL", "APPROVED"])
def test_a_member_authored_record_under_the_sample_name_is_never_adopted(
    platform, admin, ada, monkeypatch, status
):
    """N17-obs256 provenance, kept by N17-obs262: a member's skill under the sample's name
    is not the platform's — no trigger refreshes, approves, deduplicates or overwrites
    it, no platform row is created over it, and the bundle is not uploaded into its
    prefix. The result says ``name_held``."""
    from app.services import bootstrap as bs
    from tests.test_registry_import_gateway import _default_ws

    _ada_id, ada_client = ada
    held = _member_skill(status)
    before = _backend_record(held)
    uploads: list[str] = []
    monkeypatch.setattr(registry_console, "upload_skill_bundle",
                        lambda _ws: uploads.append("x") or _bundle("9.0.0"))
    for client in (ada_client, admin):
        run = _sample_result(client)
        assert (run["record_id"], run["created"], run["updated"], run["name_held"]) == (
            held, False, False, True)
        assert run["status"] == status
    out = bs.ensure_registry_defaults(_default_ws(), log=lambda _m: None)
    assert next(r for r in out["records"] if r["type"] == "AGENT_SKILLS")["name_held"]
    after = _backend_record(held)
    assert uploads == []
    assert (after["status"], after["decidedBy"], after["updatedAt"]) == (
        before["status"], before["decidedBy"], before["updatedAt"])
    assert registry_console._skill_definition(after)["version"] == "0.1.0"
    assert [r["recordId"] for r in registry_console.sample_skill_records(
        registry_console._backend(), _default_ws())] == [held]


# ---------- (2) the platform gateway URL is reserved on register / edit ----------


@pytest.mark.parametrize("url", [
    PLATFORM_URL,
    PLATFORM_URL + "/",
    PLATFORM_URL.replace("gw-1.gateway", "GW-1.Gateway"),
    PLATFORM_URL + "?x=1",
])
def test_register_refuses_the_platform_gateway_url(platform, admin, ada, url):
    _ada_id, ada_client = ada
    for who in (ada_client, admin):
        res = who.post("/api/registry/records", json={
            "type": "MCP", "name": "hr-database", "description": "m", "url": url,
        })
        assert res.status_code == 422, res.text
        assert res.json()["code"] == registry_console.PLATFORM_URL_RESERVED_CODE
    listed = admin.get("/api/registry/records").json()["records"]
    assert "hr-database" not in {r["name"] for r in listed}


_HOST = PLATFORM_URL.split("://", 1)[1].split("/", 1)[0]


@pytest.mark.parametrize("url", [
    f"https://{_HOST}:443/mcp",
    f"https://x@{_HOST}/mcp",
    f"https://{_HOST}./mcp",
    f"http://{_HOST}/mcp",
    f"https://{_HOST}/mcp/.",
    f"https://{_HOST}//mcp",
    f"https://{_HOST}/%6Dcp",
    "https://" + _HOST.replace("gw-1", "gw%2D1", 1) + "/mcp",
    f"https://{_HOST}/mcp;x",
    f"https://{_HOST}:443/mcp/",
])
def test_rev_r23_url_spellings_of_the_platform_gateway_are_all_refused(platform, ada, url):
    """N17-obs257 / rev-r23 R2-2: ten spellings of the same endpoint."""
    _ada_id, ada_client = ada
    res = ada_client.post("/api/registry/records", json={
        "type": "MCP", "name": "squat", "description": "m", "url": url,
    })
    assert res.status_code == 422, (url, res.text)
    assert res.json()["code"] == registry_console.PLATFORM_URL_RESERVED_CODE


def test_a_different_port_or_path_is_not_the_platform_gateway(platform, ada):
    _ada_id, ada_client = ada
    for i, url in enumerate((f"https://{_HOST}:8443/mcp", f"https://{_HOST}/other")):
        res = ada_client.post("/api/registry/records", json={
            "type": "MCP", "name": f"near-miss-{i}", "description": "m", "url": url,
        })
        assert res.status_code == 201, (url, res.text)


def test_edit_refuses_re_pointing_a_record_at_the_platform_gateway(platform, admin, ada):
    _ada_id, ada_client = ada
    res = ada_client.post("/api/registry/records", json={
        "type": "MCP", "name": "my-server", "description": "m",
        "url": "https://mcp.example.test/mcp",
    })
    assert res.status_code == 201, res.text
    rid = res.json()["record_id"]
    for who in (ada_client, admin):
        put = who.put(f"/api/registry/records/{rid}", json={"url": PLATFORM_URL + "/"})
        assert put.status_code == 422, put.text
        assert put.json()["code"] == registry_console.PLATFORM_URL_RESERVED_CODE
    assert ada_client.put(
        f"/api/registry/records/{rid}", json={"url": "https://mcp.example.test/v2"}
    ).status_code == 200


def test_another_gateways_url_is_not_reserved(platform, ada):
    """Only the workspace's own platform gateway is reserved; a different URL registers."""
    _ada_id, ada_client = ada
    res = ada_client.post("/api/registry/records", json={
        "type": "MCP", "name": "partner", "description": "m",
        "url": PLATFORM_URL.replace("gw-1", "gw-9"),
    })
    assert res.status_code == 201, res.text


# ---------- N17-obs261: the administrator's preview names what the import will approve ----------


def _platform_preview(client) -> dict:
    gateways = client.get("/api/registry/import-gateway/preview").json()["gateways"]
    return next(g for g in gateways if g["platform"])


def test_admin_preview_tags_member_submitted_rows_that_the_import_will_approve(
    platform, admin, ada
):
    """rev-r23 N1: an administrator's platform import selects every target and silently
    promoted the PENDING rows a member's import had only submitted; the preview just said
    "Imported · PENDING_APPROVAL". Now each such row is `will_approve` with `origin: member`,
    the gateway counts them, and every row names its target's endpoint (from the inventory
    already listed — no extra AWS call)."""
    _ada_id, ada_client = ada
    submitted = ada_client.post("/api/registry/import-gateway", json={}).json()["results"]
    assert {r["status"] for r in submitted} == {"PENDING_APPROVAL"}
    reason = _get(admin, submitted[0]["record_id"])
    assert reason["status_reason"] == registry_console.MEMBER_TRIGGERED_REASON
    calls_before = len(registry_console.control_client(None).method_calls)

    gw = _platform_preview(admin)
    by_name = {t["target"]: t for t in gw["targets"]}
    office, weather = by_name["office-facts"], by_name["weather"]
    assert office["will_approve"] is True and office["origin"] == "member"
    assert weather["will_approve"] is True and weather["origin"] == "member"
    assert gw["will_approve"] == 2 and gw["member_submissions_to_approve"] == 2
    assert (office["endpoint"], office["endpoint_kind"]) == ("https://example.test/mcp",
                                                             "mcpServer")
    assert weather["endpoint_kind"] == "lambda"
    assert weather["endpoint"].endswith(":function:weather")
    # the endpoint costs no call beyond the inventory listing the preview always made
    listing_calls = len(registry_console.control_client(None).method_calls) - calls_before
    member_view = _platform_preview(ada_client)  # same listing, member caller
    assert len(registry_console.control_client(None).method_calls) - calls_before == \
        2 * listing_calls
    # a member's import never approves: nothing is tagged for them, origin still shown
    assert all(t["will_approve"] is False for t in member_view["targets"])
    assert member_view["will_approve"] == 0 and member_view["member_submissions_to_approve"] == 0
    assert {t["origin"] for t in member_view["targets"]} == {"member"}
    # the preview never writes
    assert _get(admin, office["record_id"])["status"] == "PENDING_APPROVAL"


def test_admin_preview_counts_a_platform_draft_as_will_approve_not_as_a_member_submission(
    platform, admin
):
    from app.models.ledger import RegistryRecord
    from app.services.registry_backend import build_mcp_descriptors

    tv._add(RegistryRecord(  # an older import's DRAFT: platform-owned, never submitted
        record_type="MCP", name="weather", description="old", record_version="1.0.0-mcp",
        status="DRAFT", descriptors=build_mcp_descriptors(
            target="weather", description="old", gateway_url=PLATFORM_URL, tools=[]),
    ))
    gw = _platform_preview(admin)
    weather = next(t for t in gw["targets"] if t["target"] == "weather")
    office = next(t for t in gw["targets"] if t["target"] == "office-facts")
    assert (weather["will_approve"], weather["origin"]) == (True, "platform")
    assert gw["will_approve"] == 1 and gw["member_submissions_to_approve"] == 0
    # a target with no record yet is created APPROVED by an admin's import — not a promotion
    assert (office["record_id"], office["will_approve"], office["origin"]) == (None, False, None)


def test_an_approved_or_held_row_is_never_will_approve(platform, admin, ada):
    admin.post("/api/registry/import-gateway", json={"target_ids": ["tgt0000002"]})
    _squatted(ada[0])  # ada's own record under the office-facts name: held, not ours
    gw = _platform_preview(admin)
    by_name = {t["target"]: t for t in gw["targets"]}
    assert by_name["weather"]["status"] == "APPROVED"
    assert (by_name["weather"]["will_approve"], by_name["weather"]["origin"]) == (
        False, "platform")
    held = by_name["office-facts"]
    assert held["held"] is True and held["will_approve"] is False and held["origin"] is None
