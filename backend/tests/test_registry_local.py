"""LocalRegistryBackend — SQLite-backed RegistryBackend (N4). Hermetic: conftest's temp
ledger; no AWS client is ever built."""

import pytest

from app.core.errors import AppError, NotFoundError
from app.services import registry_backend
from app.services.registry_local import (
    STATUSES,
    LocalRegistryBackend,
    RegistryConflictError,
    registry_id_for,
)
from tests.conftest import ws_ctx

MCP = registry_backend.build_mcp_descriptors(
    target="hr-database", description="HR tools", gateway_url="https://gw.example/mcp",
    tools=[{"name": "hr-database___get_employee", "description": "look up", "inputSchema": {}}],
)
SKILL = registry_backend.build_skills_descriptors(
    skill_md="---\nname: expense-report-writer\n---\n# Expense report writer\nDraft reports.",
    definition={"name": "expense-report-writer", "description": "Draft expense reports",
                "path": "s3://bkt/skills/expense-report-writer/", "files": ["SKILL.md"]},
)


@pytest.fixture
def backend(client) -> LocalRegistryBackend:  # `client` builds the app → init_db ran
    return LocalRegistryBackend()


@pytest.fixture
def ws():
    return ws_ctx({"artifacts_bucket": "bkt"})


def _mcp(backend, ws, name="hr-database", **kw):
    return backend.upsert_record(
        ws, name=name, description="HR tools", descriptor_type="MCP", descriptors=MCP, **kw
    )


def test_registry_id_is_local_plus_workspace(backend, ws):
    assert backend.registry_id(ws) == registry_id_for(ws) == "local-default"


def test_upsert_creates_a_draft_with_the_type_qualified_initial_version(backend, ws):
    record, created = _mcp(backend, ws)
    assert created is True
    assert record["status"] == "DRAFT" and record["statusReason"] is None
    assert record["descriptorType"] == "MCP" and record["recordVersion"] == "1.0.0-mcp"
    assert record["descriptorTypes"] == ["mcpServer"] and record["displayName"] == "hr-database"
    assert record["descriptors"] == MCP
    assert record["createdAt"] and record["updatedAt"] and record["registryId"] == "local-default"
    assert record["submittedAt"] is None and record["decidedAt"] is None
    assert len(record["recordId"]) == 32


def test_upsert_on_the_same_name_returns_the_existing_record_and_never_overwrites(backend, ws):
    first, _ = _mcp(backend, ws)
    other = registry_backend.build_mcp_descriptors(
        target="hr-database", description="changed", gateway_url="https://other/mcp", tools=None
    )
    again, created = backend.upsert_record(
        ws, name="hr-database", description="changed", descriptor_type="MCP", descriptors=other
    )
    assert created is False and again["recordId"] == first["recordId"]
    assert backend.get_record(ws, first["recordId"])["descriptors"] == MCP  # untouched
    assert backend.get_record(ws, first["recordId"])["description"] == "HR tools"


def test_same_name_different_type_is_a_distinct_record(backend, ws):
    mcp, _ = _mcp(backend, ws, name="shared-name")
    skill, created = backend.upsert_record(
        ws, name="shared-name", description="d", descriptor_type="AGENT_SKILLS", descriptors=SKILL
    )
    assert created is True and skill["recordId"] != mcp["recordId"]
    assert skill["recordVersion"] == "1.0.0-skill"
    assert backend.find_record(ws, "shared-name", "AGENT_SKILLS")["recordId"] == skill["recordId"]
    assert backend.find_record(ws, "shared-name", "MCP")["recordId"] == mcp["recordId"]
    assert backend.find_record(ws, "nope") is None


def test_same_name_and_version_collision_across_types_is_not_a_second_row(backend, ws):
    mcp, _ = _mcp(backend, ws, name="pinned", record_version="2.0.0")
    again, created = backend.upsert_record(
        ws, name="pinned", description="d", descriptor_type="AGENT_SKILLS",
        descriptors=SKILL, record_version="2.0.0",
    )
    assert created is False and again["recordId"] == mcp["recordId"]
    assert len(backend.list_records(ws)) == 1


def test_wait_record_settled_is_an_immediate_get(backend, ws):
    record, _ = _mcp(backend, ws)
    calls = []
    settled = backend.wait_record_settled(ws, record["recordId"], sleeper=calls.append)
    assert settled["recordId"] == record["recordId"] and calls == []


def test_get_update_delete_are_workspace_scoped(backend, ws):
    record, _ = _mcp(backend, ws)
    other = ws_ctx(id="acct-usw1")
    with pytest.raises(NotFoundError):
        backend.get_record(other, record["recordId"])
    with pytest.raises(NotFoundError):
        backend.update_record(other, record["recordId"], description="x")
    with pytest.raises(NotFoundError):
        backend.delete_record(other, record["recordId"])
    assert backend.list_records(other) == []
    assert backend.find_record(other, "hr-database") is None
    # the same name is free in the other workspace
    _, created = _mcp(backend, other)
    assert created is True
    assert {r["registryId"] for r in backend.list_records(other)} == {"local-acct-usw1"}


def test_list_filters_by_type_and_status_and_rejects_unknown_type(backend, ws):
    mcp, _ = _mcp(backend, ws)
    backend.upsert_record(ws, name="exp", description="d", descriptor_type="AGENT_SKILLS",
                          descriptors=SKILL)
    backend.submit_record(ws, mcp["recordId"])
    assert [r["name"] for r in backend.list_records(ws, "MCP")] == ["hr-database"]
    assert [r["name"] for r in backend.list_records(ws, "skill")] == ["exp"]  # GA alias accepted
    pending = backend.list_records(ws, None, "PENDING_APPROVAL")
    assert [r["name"] for r in pending] == ["hr-database"]
    assert [r["name"] for r in backend.list_records(ws, None, "draft")] == ["exp"]
    with pytest.raises(ValueError):
        backend.list_records(ws, "gadget")


def test_discoverable_is_approved_only_without_descriptors(backend, ws):
    mcp, _ = _mcp(backend, ws)
    backend.upsert_record(ws, name="exp", description="d", descriptor_type="AGENT_SKILLS",
                          descriptors=SKILL)
    assert backend.list_discoverable_records(ws) == []
    backend.submit_record(ws, mcp["recordId"])
    backend.approve_record(ws, mcp["recordId"])
    (summary,) = backend.list_discoverable_records(ws)
    assert summary["recordId"] == mcp["recordId"] and "descriptors" not in summary
    assert summary["descriptorTypes"] == ["mcpServer"]
    assert backend.list_discoverable_records(ws, "AGENT_SKILLS") == []
    assert len(backend.list_discoverable_records(ws, "mcp")) == 1
    with pytest.raises(ValueError):
        backend.list_discoverable_records(ws, "gadget")


@pytest.mark.parametrize(("path", "final"), [
    (("submit", "approve"), "APPROVED"),
    (("submit", "reject"), "REJECTED"),
    (("submit", "reject", "approve"), "APPROVED"),          # REJECTED is recoverable
    (("submit", "reject", "submit"), "PENDING_APPROVAL"),
    (("submit", "approve", "disable"), "DEPRECATED"),
])
def test_valid_lifecycle_paths(backend, ws, path, final):
    record, _ = _mcp(backend, ws)
    ops = {"submit": backend.submit_record, "approve": backend.approve_record,
           "reject": backend.reject_record, "disable": backend.disable_record}
    for action in path:
        out = ops[action](ws, record["recordId"])
    assert out["status"] == final and final in STATUSES
    assert out["statusReason"].endswith("via launchpad console")
    if final == "PENDING_APPROVAL":
        assert out["submittedAt"] and out["decidedAt"] is None
    else:
        assert out["decidedAt"]


@pytest.mark.parametrize(("path", "bad"), [
    ((), "approve"),                       # DRAFT → APPROVED
    ((), "reject"),                        # DRAFT → REJECTED
    ((), "disable"),                       # DRAFT → DEPRECATED
    (("submit",), "submit"),               # PENDING → PENDING
    (("submit",), "disable"),              # PENDING → DEPRECATED
    (("submit", "approve"), "submit"),     # APPROVED → PENDING
    (("submit", "approve"), "approve"),
    (("submit", "approve"), "reject"),
    (("submit", "reject"), "reject"),
    (("submit", "reject"), "disable"),
    (("submit", "approve", "disable"), "submit"),   # DEPRECATED is terminal
    (("submit", "approve", "disable"), "approve"),
    (("submit", "approve", "disable"), "reject"),
    (("submit", "approve", "disable"), "disable"),
])
def test_invalid_transitions_are_409_conflicts_and_change_nothing(backend, ws, path, bad):
    record, _ = _mcp(backend, ws)
    ops = {"submit": backend.submit_record, "approve": backend.approve_record,
           "reject": backend.reject_record, "disable": backend.disable_record}
    for action in path:
        ops[action](ws, record["recordId"])
    before = backend.get_record(ws, record["recordId"])
    with pytest.raises(RegistryConflictError) as exc:
        ops[bad](ws, record["recordId"])
    assert exc.value.status_code == 409 and exc.value.code == "registry.invalid_transition"
    assert exc.value.detail["from_status"] == before["status"]
    assert backend.get_record(ws, record["recordId"]) == before


def test_set_record_status_rejects_an_unknown_status(backend, ws):
    record, _ = _mcp(backend, ws)
    with pytest.raises(ValueError):
        backend.set_record_status(ws, record["recordId"], "PUBLISHED", "nope")


def test_decided_by_comes_from_the_actor_contextvar(backend, ws):
    record, _ = _mcp(backend, ws)
    backend.submit_record(ws, record["recordId"])
    token = registry_backend.current_actor.set("admin")
    try:
        out = backend.approve_record(ws, record["recordId"])
    finally:
        registry_backend.current_actor.reset(token)
    assert out["decidedBy"] == "admin"


def test_description_only_update_keeps_status_and_version(backend, ws):
    record, _ = _mcp(backend, ws)
    backend.submit_record(ws, record["recordId"])
    backend.approve_record(ws, record["recordId"])
    out = backend.update_record(ws, record["recordId"], description="renamed")
    assert out["description"] == "renamed"
    assert out["status"] == "APPROVED" and out["recordVersion"] == "1.0.0-mcp"


def test_content_update_resets_to_draft_and_honours_the_version(backend, ws):
    record, _ = _mcp(backend, ws)
    backend.submit_record(ws, record["recordId"])
    backend.approve_record(ws, record["recordId"])
    new = registry_backend.build_mcp_descriptors(
        target="hr-database", description="v2", gateway_url="https://gw2/mcp", tools=None
    )
    out = backend.update_record(ws, record["recordId"], descriptors=new, record_version="1.1.0-mcp")
    assert out["status"] == "DRAFT" and out["statusReason"] == "updated — awaiting review"
    assert out["recordVersion"] == "1.1.0-mcp" and out["descriptors"] == new
    assert out["decidedAt"] is None and out["submittedAt"] is None


def test_update_to_a_version_this_name_already_carries_is_a_409(backend, ws):
    a, _ = _mcp(backend, ws, name="dup", record_version="1.0.0")
    backend.update_record(ws, a["recordId"], record_version="1.1.0")      # bump is fine
    assert backend.get_record(ws, a["recordId"])["recordVersion"] == "1.1.0"
    # a second row for the same (workspace, name) can only come from another type …
    b, created = backend.upsert_record(
        ws, name="dup", description="d", descriptor_type="AGENT_SKILLS",
        descriptors=SKILL, record_version="2.0.0",
    )
    assert created is True and b["recordId"] != a["recordId"]
    # … and moving it onto a version the name already has trips the unique index → 409
    with pytest.raises(RegistryConflictError) as exc:
        backend.update_record(ws, b["recordId"], record_version="1.1.0")
    assert exc.value.code == "registry.version_exists"
    assert backend.get_record(ws, b["recordId"])["recordVersion"] == "2.0.0"  # rolled back


def test_update_on_a_deprecated_record_is_a_409(backend, ws):
    record, _ = _mcp(backend, ws)
    backend.submit_record(ws, record["recordId"])
    backend.approve_record(ws, record["recordId"])
    backend.disable_record(ws, record["recordId"])
    with pytest.raises(RegistryConflictError) as exc:
        backend.update_record(ws, record["recordId"], description="late")
    assert exc.value.code == "registry.record_deprecated"


def test_delete_works_from_any_settled_state_and_404s_afterwards(backend, ws):
    record, _ = _mcp(backend, ws)
    backend.submit_record(ws, record["recordId"])
    backend.approve_record(ws, record["recordId"])
    backend.disable_record(ws, record["recordId"])
    backend.delete_record(ws, record["recordId"])
    with pytest.raises(NotFoundError) as exc:
        backend.get_record(ws, record["recordId"])
    assert exc.value.code == "registry.record_not_found"
    assert isinstance(exc.value, AppError)


def test_new_table_is_workspace_scoped_and_migrated():
    from app.core.db import WORKSPACE_SCOPED_TABLES, engine, schema_drift

    assert "registry_records" in WORKSPACE_SCOPED_TABLES
    assert schema_drift(engine) == {}
