"""System-managed presets (SE-038): server-owned identity, protected mutation paths,
idempotent admin install, no AWS on reads — all hermetic (deploy launch stubbed, AWS
client factory made to fail loudly wherever a read path might reach for it)."""

import threading
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

import app.routers.agents as agents_router
import app.routers.system_agents as system_router
from app.core.config import get_settings
from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.deployer.harness import STAGES, build_create_params
from app.deployer.pipeline import StageContext
from app.main import create_app
from app.models.ledger import Agent, Job, Workspace
from app.optimization.service import canary_capability, experiment_capability
from app.schemas.agent import AgentSpec
from app.services import agent_iam, aws_clients
from app.services import users as users_service
from app.services.runtime_discovery import _display_name
from app.system_agents import presets, service
from app.system_agents.presets import ARCHITECT, InstallOptions

from .conftest import ws_ctx

KEY = ARCHITECT.key
INSTALL = f"/api/system-agents/{KEY}/install"
BUCKET = "launchpad-artifacts-test"
READY_RESOURCES = {
    "artifacts_bucket": BUCKET,
    "execution_role_arn": "arn:aws:iam::111122223333:role/launchpad-agent-execution-role",
    "memory_arn": "arn:aws:bedrock-agentcore:us-west-2:111122223333:memory/launchpad_memory-x",
    "memory_id": "launchpad_memory-x",
}


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def no_real_deploy(monkeypatch):
    launched: list[str] = []
    monkeypatch.setattr(agents_router, "start_deploy_async", lambda jid: launched.append(jid))
    monkeypatch.setattr(system_router, "start_deploy_async", lambda jid: launched.append(jid))
    yield launched


@pytest.fixture(autouse=True)
def no_aws_clients(monkeypatch):
    """Any AWS client construction during these tests is a bug (reads must be
    ledger-only; refusals must happen before the teardown helper)."""

    def boom(*args, **kwargs):
        raise AssertionError(f"AWS client requested during a hermetic test: {args} {kwargs}")

    monkeypatch.setattr(aws_clients, "client", boom)
    monkeypatch.setattr(aws_clients, "get_session", boom)


def _mark_ready(workspace_id: str = DEFAULT_WORKSPACE_ID, resources: dict | None = None) -> None:
    db = SessionLocal()
    try:
        row = db.get(Workspace, workspace_id)
        row.bootstrap_status = "ready"
        row.resources = dict(resources or READY_RESOURCES)
        db.commit()
    finally:
        db.close()


def _set_status(agent_id: str, status: str, arn: str | None = None) -> None:
    db = SessionLocal()
    try:
        agent = db.get(Agent, agent_id)
        agent.status = status
        if arn:
            agent.arn = arn
            agent.resource_id = arn.rsplit("/", 1)[-1]
        db.commit()
    finally:
        db.close()


def _rows(**filters) -> list[Agent]:
    db = SessionLocal()
    try:
        q = db.query(Agent)
        for key, value in filters.items():
            q = q.filter(getattr(Agent, key) == value)
        return q.all()
    finally:
        db.close()


def _job_count() -> int:
    db = SessionLocal()
    try:
        return db.query(Job).count()
    finally:
        db.close()


def _status(client: TestClient, headers: dict | None = None) -> dict:
    res = client.get("/api/system-agents", headers=headers or {})
    assert res.status_code == 200, res.text
    presets_ = {p["key"]: p for p in res.json()["presets"]}
    return presets_[KEY]


# ---------------------------------------------------------------------------
# the preset itself: bundle, spec, IAM
# ---------------------------------------------------------------------------


def test_bundle_is_valid_versioned_and_free_of_desktop_assumptions():
    bundle = presets.load_bundle(ARCHITECT)
    try:
        assert bundle.name == ARCHITECT.name
        assert bundle.version == ARCHITECT.skill_version
        assert "SKILL.md" in bundle.files
        assert any(f.startswith("references/") for f in bundle.files)
        # methodology + guidance only: no scripts, binaries or office documents
        assert not [f for f in bundle.files if f.endswith((".py", ".pdf", ".docx", ".sh"))]
        text = "\n".join((bundle.root / f).read_text(encoding="utf-8") for f in bundle.files)
        for forbidden in ("KiroCrew", "__KIROCREW", "macOS", "drawio", "ask_question",
                          "file_send", "~/Downloads", "sandbox:/mnt"):
            assert forbidden not in text, f"source-package assumption leaked: {forbidden!r}"
        # locale follows the user, not a hard-wired language
        assert "language of the customer's most recent message" in text
        # never claims PDF retrieval it cannot perform
        lowered = text.lower()
        assert "never claim to have retrieved" in lowered or "never claim original" in lowered
    finally:
        bundle.close()


def test_bundle_digest_is_deterministic_and_content_sensitive(tmp_path):
    digest, files = presets.bundle_digest(ARCHITECT.skill_path())
    again, _ = presets.bundle_digest(ARCHITECT.skill_path())
    assert digest == again and len(digest) == 64 and "SKILL.md" in files
    copy = tmp_path / "copy"
    copy.mkdir()
    for rel in files:
        target = copy / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((ARCHITECT.skill_path() / rel).read_bytes())
    assert presets.bundle_digest(copy)[0] == digest
    (copy / "SKILL.md").write_text("changed", encoding="utf-8")
    assert presets.bundle_digest(copy)[0] != digest


def test_load_bundle_refuses_a_version_drift(monkeypatch):
    drifted = presets.SystemPreset(
        key=ARCHITECT.key, name=ARCHITECT.name, label=ARCHITECT.label,
        description="", skill_version="9.9.9", system_prompt="x", allowed_tools=("file_*",),
    )
    with pytest.raises(ValueError, match="bump both together"):
        presets.load_bundle(drifted)


def test_build_spec_is_server_owned_and_constrained():
    spec = presets.build_spec(ARCHITECT, BUCKET, InstallOptions())
    assert spec.name == ARCHITECT.name and spec.method == "harness"
    assert spec.skills == [f"s3://{BUCKET}/system-skills/{ARCHITECT.name}/1.0.0/"]
    assert spec.memory.long_term is False and spec.memory.short_term is True
    assert spec.allowed_tools == ["file_*", "@aws_knowledge"]
    assert "shell" not in spec.allowed_tools and "*" not in spec.allowed_tools
    assert [t.type for t in spec.tools] == ["mcp"]
    assert spec.tools[0].config["url"] == "https://knowledge-mcp.global.api.aws"
    assert not [t for t in spec.tools if t.type == "builtin"]
    # no secrets / account ids in the prompt
    assert "arn:aws" not in spec.system_prompt and "111122223333" not in spec.system_prompt
    params = build_create_params(spec, "arn:aws:iam::111:role/x", None)
    assert params["allowedTools"] == ["file_*", "@aws_knowledge"]
    assert params["skills"] == [{"s3": {"uri": spec.skills[0]}}]
    assert params["tools"][0]["type"] == "remote_mcp"
    assert "memory" not in params  # no memory arn given → none attached


def test_ordinary_harness_spec_sends_no_allowed_tools():
    """Parity: existing generic Harness agents keep the API default (all tools)."""
    spec = AgentSpec(name="plain-harness", method="harness", system_prompt="hi")
    assert spec.allowed_tools is None
    assert "allowedTools" not in build_create_params(spec, "arn:aws:iam::111:role/x", None)


def test_allowed_tools_shape_is_validated():
    with pytest.raises(ValueError):
        AgentSpec(name="plain-harness", method="harness", system_prompt="hi",
                  allowed_tools=["a/b/c"])


def test_execution_role_grants_only_the_versioned_skill_prefix_and_no_broad_tools():
    spec = presets.build_spec(ARCHITECT, BUCKET, InstallOptions())
    ctx = agent_iam.role_context(ws_ctx(READY_RESOURCES))
    doc = agent_iam.policy_document(spec, ctx)
    sids = {s["Sid"] for s in doc["Statement"]}
    assert "AgentCoreCodeInterpreter" not in sids and "AgentCoreBrowser" not in sids
    assert "EcrPull" not in sids and "ManagedKbRetrieval" not in sids
    objects = next(s for s in doc["Statement"] if s["Sid"] == "SkillBundleObjects")
    assert objects["Resource"] == [
        f"arn:aws:s3:::{BUCKET}/system-skills/{ARCHITECT.name}/1.0.0/*"
    ]
    # member-writable prefixes are never readable by the preset's role
    assert not any("/skills/" in r or "/agent-skills/" in r for r in objects["Resource"])
    for statement in doc["Statement"]:
        for action in ([statement["Action"]] if isinstance(statement["Action"], str)
                       else statement["Action"]):
            assert not action.startswith(("iam:", "s3:Put", "s3:Delete", "bedrock-agentcore:Create"
                                          "AgentRuntime", "bedrock-agentcore:Delete"))


def test_capability_projections_refuse_system_agents():
    row = Agent(name=ARCHITECT.name, method="zip_runtime", status="active", spec={},
                system_key=KEY, arn="arn:aws:bedrock-agentcore:us-west-2:1:runtime/x")
    assert experiment_capability(row)["reason_code"] == "system-managed"
    assert canary_capability(row)["reason_code"] == "system-managed"


def test_discovery_import_never_takes_a_reserved_name():
    db = SessionLocal()
    try:
        name = _display_name(db, ARCHITECT.name, "harness-1234567890", None,
                             workspace_id=DEFAULT_WORKSPACE_ID)
    finally:
        db.close()
    assert name != ARCHITECT.name and name.startswith(ARCHITECT.name)


# ---------------------------------------------------------------------------
# status + install (open console ⇒ implicit admin)
# ---------------------------------------------------------------------------


def _mark_unready(workspace_id: str = DEFAULT_WORKSPACE_ID) -> None:
    """A workspace whose bootstrap never ran (the seeded row mirrors whatever
    config the developer's box has, so the test pins the state explicitly)."""
    db = SessionLocal()
    try:
        row = db.get(Workspace, workspace_id)
        row.bootstrap_status = "registered"
        row.resources = {}
        db.commit()
    finally:
        db.close()


def test_startup_and_reads_provision_nothing(client):
    """create_app + a status read + an agents list leave the ledger without a preset
    and build no AWS client (the autouse factory stub would raise)."""
    _mark_unready()
    assert _rows(system_key=KEY) == []
    status = _status(client)
    assert status["status"] == "configuration_required"
    assert status["can_install"] is False
    assert any("bootstrap" in r for r in status["requirements"])
    assert client.get("/api/agents").json()["agents"] == []
    assert _rows(system_key=KEY) == [] and _job_count() == 0


def test_install_refused_until_workspace_is_ready(client):
    _mark_unready()
    res = client.post(INSTALL, json={})
    assert res.status_code == 409
    assert res.json()["code"] == "system_agent.workspace_not_ready"
    assert _rows() == [] and _job_count() == 0


def test_install_is_explicit_and_idempotent_while_deploying(client, no_real_deploy):
    _mark_ready()
    assert _status(client)["status"] == "not_installed"
    first = client.post(INSTALL, json={})
    assert first.status_code == 202, first.text
    body = first.json()
    assert body["created"] is True and body["changed"] is True
    agent = body["agent"]
    assert agent["name"] == ARCHITECT.name
    assert agent["system"] == {
        "managed": True, "key": KEY, "label": ARCHITECT.label, "skill_version": "1.0.0",
        "protected_actions": ["redeploy", "delete", "convert", "experiment", "canary"],
    }
    assert agent["spec"]["allowed_tools"] == ["file_*", "@aws_knowledge"]
    assert no_real_deploy == [body["job_id"]]
    assert _status(client)["status"] == "deploying"

    # repeated click while the job runs: same job, no new row, no new job
    second = client.post(INSTALL, json={})
    assert second.status_code == 202
    assert second.json()["job_id"] == body["job_id"]
    assert second.json()["created"] is False and second.json()["changed"] is False
    assert len(_rows(system_key=KEY)) == 1 and _job_count() == 1
    assert no_real_deploy == [body["job_id"]]

    listed = client.get("/api/agents").json()["agents"]
    assert [a["system"]["managed"] for a in listed] == [True]
    detail = client.get(f"/api/agents/{agent['id']}").json()
    assert detail["system"]["key"] == KEY
    assert [s["name"] for s in detail["deployments"][0]["stages"]] == [
        "generate", "package", "provision", "deploy", "register",
    ]


def test_install_on_active_preset_is_a_no_op_unless_changed_or_forced(client, no_real_deploy):
    _mark_ready()
    agent_id = client.post(INSTALL, json={}).json()["agent"]["id"]
    _set_status(agent_id, "active", "arn:aws:bedrock-agentcore:us-west-2:1:harness/h1")
    assert _status(client)["status"] == "active"

    same = client.post(INSTALL, json={})
    assert same.status_code == 200 and same.json()["changed"] is False
    assert same.json()["job_id"] is None and _job_count() == 1

    changed = client.post(INSTALL, json={"model_id": "global.anthropic.claude-opus-5"})
    assert changed.status_code == 202 and changed.json()["changed"] is True
    assert _job_count() == 2
    db = SessionLocal()
    try:
        job = db.get(Job, changed.json()["job_id"])
        assert job.payload["mode"] == "update"
        assert db.get(Agent, agent_id).spec["model_id"] == "global.anthropic.claude-opus-5"
        # the administrator's earlier choice is what a bodiless repair keeps
    finally:
        db.close()
    _set_status(agent_id, "active")
    kept = client.post(INSTALL, json={})
    assert kept.status_code == 200 and kept.json()["preset"]["model_id"] == (
        "global.anthropic.claude-opus-5"
    )

    forced = client.post(INSTALL, json={"force": True})
    assert forced.status_code == 202 and _job_count() == 3
    assert len(_rows(system_key=KEY)) == 1


def test_failed_preset_is_repaired_by_install(client):
    _mark_ready()
    agent_id = client.post(INSTALL, json={}).json()["agent"]["id"]
    _set_status(agent_id, "failed")
    status = _status(client)
    assert status["status"] == "failed"
    res = client.post(INSTALL, json={})
    assert res.status_code == 202 and res.json()["changed"] is True
    assert _status(client)["status"] == "deploying" and len(_rows(system_key=KEY)) == 1


def test_install_with_knowledge_base_mounts_only_the_given_reference(client):
    _mark_ready()
    res = client.post(INSTALL, json={"knowledge_bases": [
        {"kb_id": "KB123ABC", "name": "whitepaper", "description": "the guide"}
    ]})
    assert res.status_code == 202
    spec = res.json()["agent"]["spec"]
    assert spec["knowledge_bases"] == [
        {"kb_id": "KB123ABC", "name": "whitepaper", "description": "the guide"}
    ]
    assert _status(client)["knowledge_bases"][0]["kb_id"] == "KB123ABC"


def test_reserved_name_is_refused_for_ordinary_agents_and_spoofed_metadata_is_ignored(client):
    res = client.post("/api/agents", json={
        "name": ARCHITECT.name, "method": "harness", "system_prompt": "impostor",
    })
    assert res.status_code == 409 and res.json()["code"] == "agent.name_reserved"
    assert _rows() == []

    spoof = client.post("/api/agents", json={
        "name": "totally-ordinary", "method": "harness", "system_prompt": "hi",
        "system_key": KEY, "system": {"managed": True}, "owner": "system",
    })
    assert spoof.status_code == 202
    assert spoof.json()["agent"]["system"] is None
    row = _rows(name="totally-ordinary")[0]
    assert row.system_key is None and row.owner != "system"
    assert "system_key" not in row.spec and "system" not in row.spec


def test_preexisting_ordinary_agent_with_the_reserved_name_is_never_adopted(client):
    _mark_ready()
    db = SessionLocal()
    try:  # written before the name was reserved — bypasses the API guard on purpose
        squatter = Agent(workspace_id=DEFAULT_WORKSPACE_ID, name=ARCHITECT.name,
                         method="harness", status="active", spec={"name": ARCHITECT.name})
        db.add(squatter)
        db.commit()
        squatter_id = squatter.id
    finally:
        db.close()
    status = _status(client)
    assert status["name_collision"]["agent_id"] == squatter_id
    assert status["can_install"] is False
    res = client.post(INSTALL, json={})
    assert res.status_code == 409
    assert res.json()["code"] == "system_agent.name_collision"
    assert res.json()["detail"]["agent_id"] == squatter_id
    assert _rows(system_key=KEY) == [] and _job_count() == 0
    assert _rows(name=ARCHITECT.name)[0].system_key is None


def test_concurrent_installs_collapse_onto_one_agent_and_one_job(client):
    _mark_ready()
    codes: list[int] = []
    errors: list[str] = []
    lock = threading.Lock()

    def worker() -> None:
        try:
            res = client.post(INSTALL, json={})
            with lock:
                codes.append(res.status_code)
        except Exception as exc:  # noqa: BLE001 — collected, asserted below
            with lock:
                errors.append(repr(exc))

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors
    assert set(codes) <= {200, 202}, codes
    assert len(_rows(system_key=KEY)) == 1
    assert _job_count() == 1


def test_install_race_loser_returns_the_winner(client, monkeypatch):
    """Deterministic version of the race: the winner's row lands between the
    handler's existence check and its INSERT."""
    _mark_ready()
    real = service.find_installed
    calls = {"n": 0}

    def sneaky(db, workspace_id, preset):
        calls["n"] += 1
        found = real(db, workspace_id, preset)
        if calls["n"] == 2 and found is None:  # the install handler's own pre-check
            other = SessionLocal()
            try:
                other.add(Agent(workspace_id=workspace_id, name=preset.name, method="harness",
                                status="deploying", spec={}, system_key=preset.key))
                other.commit()
            finally:
                other.close()
        return found

    monkeypatch.setattr(service, "find_installed", sneaky)
    res = client.post(INSTALL, json={})
    assert res.status_code == 200, res.text
    assert res.json()["created"] is False and res.json()["job_id"] is None
    assert len(_rows(system_key=KEY)) == 1 and _job_count() == 0


def test_admin_uninstall_frees_the_key_for_reinstall(client, monkeypatch):
    _mark_ready()
    agent_id = client.post(INSTALL, json={}).json()["agent"]["id"]
    _set_status(agent_id, "active", "arn:aws:bedrock-agentcore:us-west-2:1:harness/h1")
    torn_down: list[str] = []
    monkeypatch.setattr(system_router, "_delete_agent_resources",
                        lambda agent, ws: torn_down.append(agent.id) or True)
    res = client.delete(f"/api/system-agents/{KEY}")
    assert res.status_code == 200 and res.json() == {
        "deleted": True, "agent_id": agent_id, "aws_resource_deleted": True,
    }
    assert torn_down == [agent_id]
    assert _status(client)["status"] == "not_installed"
    assert client.delete(f"/api/system-agents/{KEY}").status_code == 404
    again = client.post(INSTALL, json={})
    assert again.status_code == 202 and again.json()["agent"]["id"] != agent_id
    assert len(_rows(system_key=KEY, status="deploying")) == 1


def test_uninstall_refused_while_deploying(client):
    _mark_ready()
    client.post(INSTALL, json={})
    res = client.delete(f"/api/system-agents/{KEY}")
    assert res.status_code == 409 and res.json()["code"] == "agent.deploy_in_progress"


def test_unknown_preset_is_404(client):
    _mark_ready()
    assert client.post("/api/system-agents/nope/install", json={}).status_code == 404
    assert client.delete("/api/system-agents/nope").status_code == 404


def test_cross_workspace_isolation(client):
    _mark_ready()
    db = SessionLocal()
    try:
        db.add(Workspace(id="lab-use2", name="lab", account_id="111122223333",
                         region="us-east-2", bootstrap_status="ready",
                         resources=dict(READY_RESOURCES)))
        db.commit()
    finally:
        db.close()
    other = {"X-Workspace": "lab-use2"}
    agent_id = client.post(INSTALL, json={}).json()["agent"]["id"]
    assert _status(client, other)["status"] == "not_installed"
    # the default workspace's preset is invisible + immutable from the other one
    assert client.get(f"/api/agents/{agent_id}", headers=other).status_code == 404
    assert client.delete(f"/api/agents/{agent_id}", headers=other).status_code == 404
    assert client.delete(f"/api/system-agents/{KEY}", headers=other).status_code == 404
    res = client.post(INSTALL, json={}, headers=other)
    assert res.status_code == 202 and res.json()["agent"]["id"] != agent_id
    assert sorted(a.workspace_id for a in _rows(system_key=KEY)) == ["default", "lab-use2"]


def test_ordinary_mutations_are_refused_before_any_aws_call(client, monkeypatch):
    _mark_ready()
    agent_id = client.post(INSTALL, json={}).json()["agent"]["id"]
    _set_status(agent_id, "active", "arn:aws:bedrock-agentcore:us-west-2:1:harness/h1")
    monkeypatch.setattr(agents_router, "_delete_agent_resources",
                        lambda *a, **k: pytest.fail("teardown reached for a system agent"))
    spec = client.get(f"/api/agents/{agent_id}").json()["spec"]

    res = client.delete(f"/api/agents/{agent_id}")
    assert res.status_code == 403 and res.json()["code"] == "agent.system_managed"
    assert res.json()["detail"]["action"] == "delete"
    res = client.post(f"/api/agents/{agent_id}/redeploy", json=spec)
    assert res.status_code == 403 and res.json()["detail"]["action"] == "redeploy"
    res = client.post(f"/api/agents/{agent_id}/convert")
    assert res.status_code == 403 and res.json()["detail"]["action"] == "convert"
    row = _rows(system_key=KEY)[0]
    assert row.status == "active" and row.spec == spec and _job_count() == 1


# ---------------------------------------------------------------------------
# gated console: member vs administrator
# ---------------------------------------------------------------------------

ADMIN_CREDS = {"username": "operator", "password": "s3cret-pass"}
MEMBER_CREDS = {
    "username": "member-user",
    "email": "member-user@acme-corp.com",
    "password": "sufficient-pass",
}


@pytest.fixture
def gated(monkeypatch):
    monkeypatch.setenv("LAUNCHPAD_AUTH_USERNAME", ADMIN_CREDS["username"])
    monkeypatch.setenv("LAUNCHPAD_AUTH_PASSWORD", ADMIN_CREDS["password"])
    get_settings.cache_clear()
    app = create_app()
    with (
        TestClient(app, client=("127.0.0.1", 4321)) as admin,
        TestClient(app, client=("127.0.0.1", 4321)) as member,
    ):
        assert admin.post("/api/auth/login", json=ADMIN_CREDS).status_code == 200
        assert member.post("/api/auth/register", json=MEMBER_CREDS).status_code == 201
        db = SessionLocal()
        try:
            user = users_service.find_by_username(db, MEMBER_CREDS["username"])
            user.status = users_service.STATUS_ACTIVE
            user.expires_at = datetime.now(UTC) + timedelta(days=7)
            users_service.set_workspace_grants(db, user, [DEFAULT_WORKSPACE_ID])
            db.commit()
        finally:
            db.close()
        login = member.post("/api/auth/login", json={
            "username": MEMBER_CREDS["username"], "password": MEMBER_CREDS["password"],
        })
        assert login.status_code == 200 and login.json()["role"] == "member"
        yield admin, member
    get_settings.cache_clear()


def test_member_with_full_lifecycle_permissions_cannot_touch_the_preset(gated, monkeypatch):
    admin, member = gated
    _mark_ready()
    installed = admin.post(INSTALL, json={})
    assert installed.status_code == 202, installed.text
    agent_id = installed.json()["agent"]["id"]
    _set_status(agent_id, "active", "arn:aws:bedrock-agentcore:us-west-2:1:harness/h1")
    monkeypatch.setattr(agents_router, "_delete_agent_resources",
                        lambda *a, **k: pytest.fail("teardown reached for a system agent"))

    # the member holds every perm:agents.* by default …
    perms = member.get("/api/auth/status").json()
    assert perms["role"] == "member"
    # … can see and use the preset like any agent …
    listed = member.get("/api/agents").json()["agents"]
    assert [a["system"]["managed"] for a in listed] == [True]
    assert _status(member)["status"] == "active"
    assert _status(member)["can_install"] is False  # server says: admin only
    # … but every mutation path refuses before AWS
    spec = listed[0]["spec"]
    assert member.delete(f"/api/agents/{agent_id}").json()["code"] == "agent.system_managed"
    assert member.post(f"/api/agents/{agent_id}/redeploy", json=spec).status_code == 403
    assert member.post(f"/api/agents/{agent_id}/convert").status_code == 403
    assert member.post(INSTALL, json={}).status_code == 403
    assert member.post(INSTALL, json={}).json()["code"] == "auth.forbidden"
    assert member.delete(f"/api/system-agents/{KEY}").status_code == 403
    assert _rows(system_key=KEY)[0].status == "active" and _job_count() == 1


def test_member_keeps_ordinary_agent_lifecycle_parity(gated, monkeypatch):
    admin, member = gated
    _mark_ready()
    created = member.post("/api/agents", json={
        "name": "member-harness", "method": "harness", "system_prompt": "hi",
    })
    assert created.status_code == 202, created.text
    agent_id = created.json()["agent"]["id"]
    assert created.json()["agent"]["system"] is None
    _set_status(agent_id, "active", "arn:aws:bedrock-agentcore:us-west-2:1:harness/h2")
    spec = created.json()["agent"]["spec"]
    assert member.post(f"/api/agents/{agent_id}/redeploy", json=spec).status_code == 202
    _set_status(agent_id, "active")
    monkeypatch.setattr(agents_router, "_delete_agent_resources", lambda *a, **k: True)
    res = member.delete(f"/api/agents/{agent_id}")
    assert res.status_code == 200 and res.json()["deleted"] is True


def test_admin_maintains_the_preset_through_the_dedicated_routes_only(gated, monkeypatch):
    admin, _member = gated
    _mark_ready()
    agent_id = admin.post(INSTALL, json={}).json()["agent"]["id"]
    _set_status(agent_id, "active", "arn:aws:bedrock-agentcore:us-west-2:1:harness/h1")
    # even the administrator does not edit a preset through the ordinary routes
    assert admin.delete(f"/api/agents/{agent_id}").status_code == 403
    assert admin.post(INSTALL, json={"force": True}).status_code == 202
    _set_status(agent_id, "active")
    monkeypatch.setattr(system_router, "_delete_agent_resources", lambda *a, **k: True)
    assert admin.delete(f"/api/system-agents/{KEY}").status_code == 200


# ---------------------------------------------------------------------------
# package stage: versioned, checksummed skill upload inside the job
# ---------------------------------------------------------------------------


class _FakeS3:
    def __init__(self) -> None:
        self.puts: list[dict] = []

    def put_object(self, **kwargs):
        self.puts.append(kwargs)
        return {}


def test_package_stage_uploads_the_versioned_checksummed_bundle(monkeypatch):
    fake = _FakeS3()
    workspace = ws_ctx(READY_RESOURCES)
    monkeypatch.setattr(aws_clients, "client", lambda service, ws, **kw: fake)
    agent = Agent(id="a" * 32, name=ARCHITECT.name, method="harness", status="deploying",
                  spec=presets.build_spec(ARCHITECT, BUCKET, InstallOptions()).model_dump(),
                  system_key=KEY)
    logs: list[str] = []
    ctx = StageContext(agent_id=agent.id, deployment_id="d", job_id="j", workspace=workspace,
                       log=logs.append)
    result = STAGES["package"](ctx, agent)
    assert not result.skipped
    prefix = f"system-skills/{ARCHITECT.name}/1.0.0/"
    keys = sorted(p["Key"] for p in fake.puts)
    assert keys[0].startswith(prefix) and f"{prefix}SKILL.md" in keys
    assert all(k.startswith(prefix) for k in keys)
    assert all(p["Bucket"] == BUCKET and p["ChecksumSHA256"] for p in fake.puts)
    assert "v1.0.0" in result.detail and "sha256" in result.detail
    digest, files = presets.bundle_digest(ARCHITECT.skill_path())
    assert digest[:12] in result.detail and len(keys) == len(files)
    # idempotent: a re-run (resume / repair) rewrites the same keys
    STAGES["package"](ctx, agent)
    assert sorted(p["Key"] for p in fake.puts[len(files):]) == keys


def test_package_stage_still_skips_for_ordinary_harness_agents():
    agent = Agent(id="b" * 32, name="plain", method="harness", status="deploying", spec={})
    ctx = StageContext(agent_id=agent.id, deployment_id="d", job_id="j",
                       workspace=ws_ctx(READY_RESOURCES))
    result = STAGES["package"](ctx, agent)
    assert result.skipped and "no build required" in result.detail
