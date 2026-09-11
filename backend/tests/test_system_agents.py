"""System-managed presets (SE-038): server-owned identity, protected mutation paths,
idempotent admin install, no AWS on reads — all hermetic (deploy launch stubbed, AWS
client factory made to fail loudly wherever a read path might reach for it)."""

import json
import threading
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

import app.routers.agents as agents_router
import app.routers.system_agents as system_router
from app.core.config import get_settings
from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.deployer.harness import STAGES, build_create_params
from app.deployer.pipeline import StageContext, create_deployment
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
    ledger-only
    refusals must happen before the teardown helper)."""

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
    assert spec.tools[0].config["auth"] == "none"  # public server → no identity grant
    # persistent memory is explicitly opted out (the flags cannot express
    # "short-term only" against the real API; see presets.build_spec)
    assert spec.memory.short_term is False and spec.memory.long_term is False
    assert params["memory"] == {"disabled": {}}
    assert params["clientToken"] if "clientToken" in params else True


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
    # unauthenticated docs-only MCP + no persistent memory ⇒ no credential/memory access
    assert "AgentCoreWorkloadIdentity" not in sids and "IdentityVaultSecrets" not in sids
    assert "AgentCoreMemory" not in sids
    assert sids == {"BedrockModels", "SkillBundleObjects", "SkillBundleList",
                    "Telemetry", "TelemetryTracing"}
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
    assert "bootstrap_not_ready" in {r["code"] for r in status["requirements"]}
    assert all(r["message"] for r in status["requirements"])
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
    first = client.post(INSTALL, json={}).json()
    agent_id, first_job = first["agent"]["id"], first["job_id"]
    _set_status(agent_id, "active", "arn:aws:bedrock-agentcore:us-west-2:1:harness/h1")
    assert _status(client)["status"] == "active"

    same = client.post(INSTALL, json={})
    assert same.status_code == 200 and same.json()["changed"] is False
    # the stable outcome: the job that produced the active preset, no new one
    assert same.json()["job_id"] == first_job and _job_count() == 1

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
                twin = Agent(workspace_id=workspace_id, name=preset.name, method="harness",
                             status="deploying", spec={}, system_key=preset.key)
                other.add(twin)
                other.flush()
                _dep, job = create_deployment(other, twin)
                winner["job"] = job.id
            finally:
                other.close()
        return found

    winner: dict = {}
    monkeypatch.setattr(service, "find_installed", sneaky)
    res = client.post(INSTALL, json={})
    assert res.status_code == 202, res.text  # the winner's job is in flight
    assert res.json()["created"] is False and res.json()["changed"] is False
    assert res.json()["job_id"] == winner["job"]  # stable outcome id, not None
    assert len(_rows(system_key=KEY)) == 1 and _job_count() == 1


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


def test_package_stage_still_skips_for_ordinary_harness_agents():
    agent = Agent(id="b" * 32, name="plain", method="harness", status="deploying", spec={})
    ctx = StageContext(agent_id=agent.id, deployment_id="d", job_id="j",
                       workspace=ws_ctx(READY_RESOURCES))
    result = STAGES["package"](ctx, agent)
    assert result.skipped and "no build required" in result.detail


# ---------------------------------------------------------------------------
# correction pass 2 — host-reproduced findings
# ---------------------------------------------------------------------------

import app.deployer.harness as harness_module  # noqa: E402
import app.optimization.canary_service as canary_svc  # noqa: E402
import app.optimization.service as exp_svc  # noqa: E402
from app.core.config import get_settings as _real_settings  # noqa: E402
from app.core.errors import AppError  # noqa: E402
from app.optimization.models import Experiment, RuntimeCanary  # noqa: E402
from app.services import knowledge  # noqa: E402

DEDICATED = "arn:aws:iam::111122223333:role/launchpad-agent-dedicated"
KB_GW = {"arn": "arn:aws:bedrock-agentcore:us-west-2:111122223333:gateway/kb",
         "oauth_provider_arn": "arn:aws:bedrock-agentcore:us-west-2:111122223333:token-vault/x"}
KB_REF = {"kb_id": "KB123ABC", "name": "whitepaper", "description": "the guide"}


def _settings(**over):
    return _real_settings().model_copy(update=over)


class _Control:
    """CreateHarness/UpdateHarness/GetHarness stub that records kwargs and can be
    told to blow up after accepting a create (crash-before-persist simulation)."""

    def __init__(self, fail_first_create: bool = False):
        self.creates: list[dict] = []
        self.updates: list[dict] = []
        self.fail_first_create = fail_first_create

    def create_harness(self, **kwargs):
        self.creates.append(kwargs)
        if self.fail_first_create and len(self.creates) == 1:
            raise ConnectionError("socket closed after the request was accepted")
        return {"harness": {"harnessId": "h1", "arn": "arn:h1", "harnessVersion": "1"}}

    def update_harness(self, **kwargs):
        self.updates.append(kwargs)
        return {"harness": {"harnessId": kwargs["harnessId"], "arn": "arn:h1",
                            "harnessVersion": "2"}}

    def get_harness(self, harnessId):
        return {"harness": {"harnessId": harnessId, "arn": "arn:h1", "status": "READY",
                            "harnessVersion": "2" if self.updates else "1"}}


def _persist_preset(status="deploying", resource_id=None, spec=None) -> str:
    db = SessionLocal()
    try:
        agent = Agent(workspace_id=DEFAULT_WORKSPACE_ID, name=ARCHITECT.name, method="harness",
                      status=status, system_key=KEY, resource_id=resource_id,
                      arn=f"arn:h/{resource_id}" if resource_id else None,
                      spec=spec
                      or presets.build_spec(ARCHITECT, BUCKET, InstallOptions()).model_dump())
        db.add(agent)
        db.flush()
        deployment, job = create_deployment(db, agent, mode="update" if resource_id else "create")
        return agent.id, deployment.id, job.id
    finally:
        db.close()


def _ctx(agent_id, deployment_id, job_id, **scratch):
    ctx = StageContext(agent_id=agent_id, deployment_id=deployment_id, job_id=job_id,
                       workspace=ws_ctx(READY_RESOURCES), log=lambda _m: None)
    ctx.scratch.update(scratch)
    return ctx


def _agent(agent_id):
    db = SessionLocal()
    try:
        return db.get(Agent, agent_id)
    finally:
        db.close()


# ---- (1) actual Harness role -------------------------------------------------


def test_create_request_carries_the_provisioned_dedicated_role_and_client_token(monkeypatch):
    agent_id, dep_id, job_id = _persist_preset()
    control = _Control()
    monkeypatch.setattr(harness_module, "control_client", lambda _ws: control)
    monkeypatch.setattr(harness_module.agent_iam, "provision_execution_role",
                        lambda *a, **k: (DEDICATED, "iam role · dedicated"))
    ctx = _ctx(agent_id, dep_id, job_id)
    agent = _agent(agent_id)
    STAGES["generate"](ctx, agent)
    assert ctx.scratch["create_params"]["executionRoleArn"] == READY_RESOURCES["execution_role_arn"]
    STAGES["provision"](ctx, agent)
    STAGES["deploy"](ctx, agent)
    assert control.creates[0]["executionRoleArn"] == DEDICATED  # never the shared role
    assert control.creates[0]["clientToken"] == f"lp-{dep_id}"
    assert len(control.creates[0]["clientToken"]) >= 33
    assert control.creates[0]["memory"] == {"disabled": {}}
    assert control.creates[0]["allowedTools"] == ["file_*", "@aws_knowledge"]


def test_resume_without_scratch_rederives_the_dedicated_role(monkeypatch):
    """A restart drops ctx.scratch; the update must still name the per-agent role."""
    agent_id, dep_id, job_id = _persist_preset(status="deploying", resource_id="h1")
    control = _Control()
    monkeypatch.setattr(harness_module, "control_client", lambda _ws: control)
    ctx = _ctx(agent_id, dep_id, job_id, mode="update")  # no create_params, no role
    STAGES["deploy"](ctx, _agent(agent_id))
    expected = (f"arn:aws:iam::111122223333:role/"
                f"{agent_iam.role_name_for(ARCHITECT.name, agent_id)}")
    assert control.updates[0]["executionRoleArn"] == expected
    assert control.updates[0]["clientToken"] == f"lp-{dep_id}"
    assert control.updates[0]["allowedTools"] == ["file_*", "@aws_knowledge"]


def test_ordinary_harness_also_gets_its_provisioned_role_on_create(monkeypatch):
    """Parity fix: the provision stage's role was never applied to the request."""
    db = SessionLocal()
    agent = Agent(workspace_id=DEFAULT_WORKSPACE_ID, name="plain", method="harness",
                  status="deploying",
                  spec=AgentSpec(name="plain", method="harness", system_prompt="x").model_dump())
    db.add(agent)
    db.flush()
    dep, job = create_deployment(db, agent)
    agent_id, dep_id, job_id = agent.id, dep.id, job.id
    db.close()
    control = _Control()
    monkeypatch.setattr(harness_module, "control_client", lambda _ws: control)
    monkeypatch.setattr(harness_module.agent_iam, "provision_execution_role",
                        lambda *a, **k: (DEDICATED, "iam role · dedicated"))
    ctx = _ctx(agent_id, dep_id, job_id)
    a = _agent(agent_id)
    STAGES["generate"](ctx, a)
    STAGES["provision"](ctx, a)
    STAGES["deploy"](ctx, a)
    assert control.creates[0]["executionRoleArn"] == DEDICATED
    assert "allowedTools" not in control.creates[0]  # generic agents keep the API default


def test_preset_fails_closed_when_per_agent_roles_are_disabled(monkeypatch):
    agent_id, dep_id, job_id = _persist_preset()
    monkeypatch.setattr(harness_module, "get_settings",
                        lambda: _settings(per_agent_execution_roles=False))
    control = _Control()
    monkeypatch.setattr(harness_module, "control_client", lambda _ws: control)
    with pytest.raises(RuntimeError, match="per-agent execution roles"):
        STAGES["generate"](_ctx(agent_id, dep_id, job_id), _agent(agent_id))
    # and the deploy stage refuses a shared-role request even if generate was skipped
    ctx = _ctx(agent_id, dep_id, job_id, execution_role_arn=READY_RESOURCES["execution_role_arn"])
    monkeypatch.setattr(harness_module, "get_settings", _real_settings)
    with pytest.raises(RuntimeError, match="shared workspace"):
        STAGES["deploy"](ctx, _agent(agent_id))
    assert control.creates == []  # refused before the AWS call
    # readiness surfaces the same requirement without any AWS call
    import app.core.config as config_module
    monkeypatch.setattr(config_module, "get_settings",
                        lambda: _settings(per_agent_execution_roles=False))
    db = SessionLocal()
    try:
        row = db.get(Workspace, DEFAULT_WORKSPACE_ID)
        row.bootstrap_status = "ready"
        row.resources = dict(READY_RESOURCES)
        db.commit()
        codes = {r["code"] for r in service.workspace_requirements(row)}
    finally:
        db.close()
    assert codes == {"per_agent_roles_disabled"}


def test_ordinary_harness_keeps_the_shared_role_when_per_agent_roles_are_off(monkeypatch):
    db = SessionLocal()
    agent = Agent(workspace_id=DEFAULT_WORKSPACE_ID, name="plain2", method="harness",
                  status="deploying",
                  spec=AgentSpec(name="plain2", method="harness", system_prompt="x").model_dump())
    db.add(agent)
    db.flush()
    dep, job = create_deployment(db, agent)
    ids = (agent.id, dep.id, job.id)
    db.close()
    monkeypatch.setattr(harness_module, "get_settings",
                        lambda: _settings(per_agent_execution_roles=False))
    control = _Control()
    monkeypatch.setattr(harness_module, "control_client", lambda _ws: control)
    ctx = _ctx(*ids)
    a = _agent(ids[0])
    STAGES["generate"](ctx, a)
    STAGES["provision"](ctx, a)
    STAGES["deploy"](ctx, a)
    assert control.creates[0]["executionRoleArn"] == READY_RESOURCES["execution_role_arn"]


# ---- (6) crash-retry identity ------------------------------------------------


def test_create_retry_after_accepted_call_repeats_the_same_client_token(monkeypatch):
    agent_id, dep_id, job_id = _persist_preset()
    control = _Control(fail_first_create=True)
    monkeypatch.setattr(harness_module, "control_client", lambda _ws: control)
    monkeypatch.setattr(harness_module.agent_iam, "retry_iam_propagation",
                        lambda fn, log: fn())  # no propagation retry in the unit
    ctx = _ctx(agent_id, dep_id, job_id, execution_role_arn=DEDICATED)
    with pytest.raises(ConnectionError):
        STAGES["deploy"](ctx, _agent(agent_id))
    assert _agent(agent_id).resource_id is None  # nothing persisted — the crash case
    STAGES["deploy"](_ctx(agent_id, dep_id, job_id, execution_role_arn=DEDICATED),
                     _agent(agent_id))  # resumed job: same deployment, fresh scratch
    tokens = [c["clientToken"] for c in control.creates]
    assert tokens == [f"lp-{dep_id}", f"lp-{dep_id}"]  # AWS dedupes → one harness


# ---- (3) KB retrieval tool + verification --------------------------------------


def test_mounted_kb_adds_only_its_gateway_tool_to_allowed_tools():
    spec = presets.build_spec(ARCHITECT, BUCKET, InstallOptions(
        knowledge_bases=(presets.KnowledgeBaseRef(**KB_REF),)))
    params = build_create_params(spec, DEDICATED, None, kb_gateway=KB_GW)
    assert [t["name"] for t in params["tools"]] == ["aws_knowledge", "launchpad_kb_gw"]
    assert params["allowedTools"] == ["file_*", "@aws_knowledge", "@launchpad_kb_gw"]
    # without a mount nothing is added; ordinary unrestricted specs stay unrestricted
    plain = build_create_params(presets.build_spec(ARCHITECT, BUCKET, InstallOptions()),
                                DEDICATED, None, kb_gateway=KB_GW)
    assert plain["allowedTools"] == ["file_*", "@aws_knowledge"]
    generic = AgentSpec(name="plain", method="harness", system_prompt="x",
                        knowledge_bases=[KB_REF])
    assert "allowedTools" not in build_create_params(generic, DEDICATED, None, kb_gateway=KB_GW)
    # the KB mode needs the gateway's OAuth path, and only that
    doc = agent_iam.policy_document(spec, agent_iam.role_context(ws_ctx(READY_RESOURCES)))
    sids = {s["Sid"] for s in doc["Statement"]}
    assert {"AgentCoreWorkloadIdentity", "IdentityVaultSecrets", "ManagedKbRetrieval"} <= sids
    assert "AgentCoreMemory" not in sids and "AgentCoreCodeInterpreter" not in sids


class _KbClient:
    class exceptions:
        class ResourceNotFoundException(Exception):
            pass

    def __init__(self, kbs: dict):
        self.kbs = kbs

    def get_knowledge_base(self, knowledgeBaseId):
        if knowledgeBaseId not in self.kbs:
            raise self.exceptions.ResourceNotFoundException(knowledgeBaseId)
        return {"knowledgeBase": self.kbs[knowledgeBaseId]}


def test_provision_verifies_knowledge_bases_in_the_target_workspace(monkeypatch):
    import app.services.agentcore.client as ac_client

    spec = presets.build_spec(ARCHITECT, BUCKET, InstallOptions(
        knowledge_bases=(presets.KnowledgeBaseRef(**KB_REF),)))
    ctx = _ctx("a" * 32, "d" * 32, "j" * 32)
    monkeypatch.setattr(ac_client, "agent_client", lambda _ws: _KbClient({}))
    with pytest.raises(RuntimeError, match="does not exist in workspace default"):
        service.verify_knowledge_bases(ctx, spec)
    monkeypatch.setattr(ac_client, "agent_client", lambda _ws: _KbClient({
        "KB123ABC": {"status": "CREATING", "knowledgeBaseConfiguration": {"type": "MANAGED"}}}))
    with pytest.raises(RuntimeError, match="not ACTIVE"):
        service.verify_knowledge_bases(ctx, spec)
    monkeypatch.setattr(ac_client, "agent_client", lambda _ws: _KbClient({
        "KB123ABC": {"status": "ACTIVE", "knowledgeBaseConfiguration": {"type": "VECTOR"}}}))
    with pytest.raises(RuntimeError, match="not a MANAGED"):
        service.verify_knowledge_bases(ctx, spec)
    logs: list[str] = []
    ctx.log = logs.append
    monkeypatch.setattr(ac_client, "agent_client", lambda _ws: _KbClient({
        "KB123ABC": {"status": "ACTIVE", "knowledgeBaseConfiguration": {"type": "MANAGED"}}}))
    service.verify_knowledge_bases(ctx, spec)
    assert logs == ["knowledge base KB123ABC verified · MANAGED · ACTIVE"]


# ---- (2) KB force delete never rewrites a protected spec ----------------------


def _mount_kb_on_preset_and_ordinary(client) -> tuple[str, str]:
    _mark_ready()
    preset_id = client.post(INSTALL, json={"knowledge_bases": [KB_REF]}).json()["agent"]["id"]
    _set_status(preset_id, "active", "arn:aws:bedrock-agentcore:us-west-2:1:harness/h1")
    db = SessionLocal()
    try:
        plain = Agent(workspace_id=DEFAULT_WORKSPACE_ID, name="kb-user", method="harness",
                      status="active", spec={"name": "kb-user", "method": "harness",
                                             "knowledge_bases": [KB_REF]})
        db.add(plain)
        db.commit()
        return preset_id, plain.id
    finally:
        db.close()


def test_kb_force_delete_is_refused_before_any_mutation_when_a_preset_mounts_it(client):
    preset_id, plain_id = _mount_kb_on_preset_and_ordinary(client)
    before = {a.id: a.spec for a in _rows()}
    res = client.delete("/api/knowledge-bases/KB123ABC?force=true")
    assert res.status_code == 409, res.text
    assert res.json()["code"] == "kb.attached_to_system_agent"
    assert res.json()["detail"]["agents"] == [ARCHITECT.name]
    assert client.delete("/api/knowledge-bases/KB123ABC").status_code == 409
    # no spec changed — the ordinary agent's mount included — and no AWS client was
    # built (the autouse factory stub would have raised)
    assert {a.id: a.spec for a in _rows()} == before
    assert _agent(preset_id).spec["knowledge_bases"] == [KB_REF]
    assert _agent(plain_id).spec["knowledge_bases"] == [KB_REF]


def test_member_kb_force_delete_is_refused_with_mixed_attachments(gated):
    admin, member = gated
    preset_id, plain_id = _mount_kb_on_preset_and_ordinary(admin)
    res = member.delete("/api/knowledge-bases/KB123ABC?force=true")
    assert res.status_code == 409 and res.json()["code"] == "kb.attached_to_system_agent"
    assert _agent(preset_id).spec["knowledge_bases"] == [KB_REF]
    assert _agent(plain_id).spec["knowledge_bases"] == [KB_REF]


def test_strip_helper_refuses_protected_mounts_and_keeps_ordinary_semantics(client, monkeypatch):
    preset_id, plain_id = _mount_kb_on_preset_and_ordinary(client)
    workspace = ws_ctx({**READY_RESOURCES, "kb_gateway_id": "gw-1"}, id=DEFAULT_WORKSPACE_ID)
    with pytest.raises(AppError) as exc:
        knowledge._strip_kb_from_agents(workspace, "KB123ABC")
    assert exc.value.code == "kb.attached_to_system_agent"
    assert _agent(preset_id).spec["knowledge_bases"] == [KB_REF]
    # an ordinary-only KB still strips (parity with the pre-existing behaviour)
    db = SessionLocal()
    other = Agent(workspace_id=DEFAULT_WORKSPACE_ID, name="kb-only", method="harness",
                  status="active", spec={"name": "kb-only", "method": "harness",
                                         "knowledge_bases": [{"kb_id": "KBPLAIN01", "name": "p",
                                                              "description": ""}]})
    db.add(other)
    db.commit()
    other_id = other.id
    db.close()
    monkeypatch.setattr(knowledge, "control_client", lambda _ws: object())
    monkeypatch.setattr(knowledge.kb_gateway, "sync_agentic_target", lambda *a, **k: None)
    assert knowledge._strip_kb_from_agents(workspace, "KBPLAIN01") == ["kb-only"]
    assert _agent(other_id).spec["knowledge_bases"] == []
    assert _agent(plain_id).spec["knowledge_bases"] == [KB_REF]


# ---- (4) durable maintenance claims --------------------------------------------


def test_two_sessions_repairing_the_same_active_preset_share_one_job(client):
    """Host reproduction: both sessions loaded the active row before either wrote."""
    _mark_ready()
    agent_id = client.post(INSTALL, json={}).json()["agent"]["id"]
    _set_status(agent_id, "active", "arn:aws:bedrock-agentcore:us-west-2:1:harness/h1")
    before = _job_count()
    s1, s2 = SessionLocal(), SessionLocal()
    try:
        a1, a2 = s1.get(Agent, agent_id), s2.get(Agent, agent_id)
        r1 = service._repair(s1, a1, ARCHITECT, BUCKET, None, force=True)
        s1.commit()
        r2 = service._repair(s2, a2, ARCHITECT, BUCKET, None, force=True)
        s2.commit()
    finally:
        s1.close()
        s2.close()
    assert r1.changed is True and r2.changed is False
    assert r1.job.id == r2.job.id  # the loser returns the winner's in-flight job
    assert _job_count() - before == 1
    assert _agent(agent_id).status == "deploying"


def test_repair_after_uninstall_reports_not_installed_and_uninstall_waits_for_jobs(
    client, monkeypatch
):
    _mark_ready()
    agent_id = client.post(INSTALL, json={}).json()["agent"]["id"]
    # a running job blocks uninstall …
    assert client.delete(f"/api/system-agents/{KEY}").json()["code"] == "agent.deploy_in_progress"
    _set_status(agent_id, "active", "arn:aws:bedrock-agentcore:us-west-2:1:harness/h1")
    stale = SessionLocal()
    stale_agent = stale.get(Agent, agent_id)  # loaded while still active
    monkeypatch.setattr(system_router, "_delete_agent_resources", lambda *a, **k: True)
    assert client.delete(f"/api/system-agents/{KEY}").status_code == 200
    with pytest.raises(AppError) as exc:  # … and a repair on the stale snapshot loses
        service._repair(stale, stale_agent, ARCHITECT, BUCKET, None, force=True)
    stale.close()
    assert exc.value.code == "system_agent.not_installed"
    assert _job_count() == 1


def test_uninstall_teardown_failure_leaves_a_retryable_failed_row(client, monkeypatch):
    _mark_ready()
    agent_id = client.post(INSTALL, json={}).json()["agent"]["id"]
    _set_status(agent_id, "active", "arn:aws:bedrock-agentcore:us-west-2:1:harness/h1")

    def boom(*a, **k):
        raise RuntimeError("DeleteHarness throttled")

    monkeypatch.setattr(system_router, "_delete_agent_resources", boom)
    tolerant = TestClient(client.app, raise_server_exceptions=False)
    res = tolerant.delete(f"/api/system-agents/{KEY}")
    assert res.status_code == 500
    row = _agent(agent_id)
    assert row.status == "failed" and "DeleteHarness throttled" in row.error
    assert _status(client)["status"] == "failed" and _status(client)["can_uninstall"] is True


def test_status_readiness_and_operation_verdicts_for_installed_presets(client):
    _mark_ready()
    agent_id = client.post(INSTALL, json={}).json()["agent"]["id"]
    status = _status(client)
    assert status["can_install"] is False and status["can_repair"] is False
    assert status["can_uninstall"] is False  # deploying
    _set_status(agent_id, "active", "arn:aws:bedrock-agentcore:us-west-2:1:harness/h1")
    status = _status(client)
    assert status["can_repair"] is True and status["can_uninstall"] is True
    assert status["memory"] == "disabled"
    # the workspace loses its bucket after the install: repair is refused, uninstall
    # is still offered, and the reason is reported on the installed preset
    _mark_ready(resources={k: v for k, v in READY_RESOURCES.items() if k != "artifacts_bucket"})
    status = _status(client)
    assert status["status"] == "active"
    assert [r["code"] for r in status["requirements"]] == ["missing_artifacts_bucket"]
    assert status["can_repair"] is False and status["can_uninstall"] is True
    assert client.post(INSTALL, json={"force": True}).json()["code"] == (
        "system_agent.workspace_not_ready"
    )


# ---- (6) pinned, immutable bundle publication -----------------------------------


class _FakeS3:
    def __init__(self, objects: dict | None = None):
        self.objects: dict[str, bytes] = dict(objects or {})
        self.puts: list[dict] = []

    class _NoSuchKey(Exception):
        pass

    def get_object(self, Bucket, Key):
        if Key not in self.objects:
            exc = self._NoSuchKey(Key)
            exc.__class__.__name__ = "NoSuchKey"
            raise type("NoSuchKey", (Exception,), {})(Key)
        import io
        return {"Body": io.BytesIO(self.objects[Key])}

    def put_object(self, **kwargs):
        self.puts.append(kwargs)
        self.objects[kwargs["Key"]] = kwargs["Body"]
        return {}


def _package_ctx(monkeypatch, fake: _FakeS3, spec=None, pinned=None):
    agent_id, dep_id, job_id = _persist_preset(spec=spec)
    if pinned is not None:
        db = SessionLocal()
        job = db.get(Job, job_id)
        job.payload = {**job.payload, "preset_bundle": pinned}
        db.commit()
        db.close()
    monkeypatch.setattr(aws_clients, "client", lambda service, ws, **kw: fake)
    return _ctx(agent_id, dep_id, job_id), _agent(agent_id)


PREFIX = f"system-skills/{ARCHITECT.name}/1.0.0/"


def test_package_publishes_checksummed_files_then_manifest_and_replays_as_noop(monkeypatch):
    fake = _FakeS3()
    ctx, agent = _package_ctx(monkeypatch, fake, pinned=presets.bundle_release(ARCHITECT))
    result = STAGES["package"](ctx, agent)
    digest, files = presets.bundle_digest(ARCHITECT.skill_path())
    keys = [p["Key"] for p in fake.puts]
    assert keys[-1] == f"{PREFIX}{service.MANIFEST_KEY}"  # manifest written LAST
    assert sorted(keys[:-1]) == sorted(f"{PREFIX}{f}" for f in files)
    assert all(p["Bucket"] == BUCKET and p["ChecksumSHA256"] for p in fake.puts)
    assert digest[:12] in result.detail and "v1.0.0" in result.detail
    manifest = json.loads(fake.objects[f"{PREFIX}{service.MANIFEST_KEY}"])
    assert manifest["digest"] == digest and manifest["version"] == "1.0.0"
    # replay (resume / repair): verified no-op, zero writes
    n = len(fake.puts)
    again = STAGES["package"](ctx, agent)
    assert len(fake.puts) == n and "already published" in again.detail


def test_package_refuses_a_queued_release_whose_spec_pins_another_version(monkeypatch):
    fake = _FakeS3()
    stale = presets.build_spec(ARCHITECT, BUCKET, InstallOptions()).model_dump()
    stale["skills"] = [f"s3://{BUCKET}/system-skills/{ARCHITECT.name}/0.9.0/"]
    ctx, agent = _package_ctx(monkeypatch, fake, spec=stale)
    with pytest.raises(RuntimeError, match="queued release mismatch"):
        STAGES["package"](ctx, agent)
    assert fake.puts == []  # refused before any cloud write


def test_package_refuses_bytes_that_differ_from_the_pinned_release(monkeypatch):
    fake = _FakeS3()
    ctx, agent = _package_ctx(monkeypatch, fake,
                              pinned={"version": "1.0.0", "digest": "f" * 64, "files": 5})
    with pytest.raises(RuntimeError, match="pinned bundle mismatch"):
        STAGES["package"](ctx, agent)
    assert fake.puts == []


def test_package_treats_a_published_version_as_immutable(monkeypatch):
    fake = _FakeS3({f"{PREFIX}{service.MANIFEST_KEY}":
                    json.dumps({"version": "1.0.0", "digest": "0" * 64}).encode()})
    ctx, agent = _package_ctx(monkeypatch, fake, pinned=presets.bundle_release(ARCHITECT))
    with pytest.raises(RuntimeError, match="immutable"):
        STAGES["package"](ctx, agent)
    assert fake.puts == []


# ---- (5) stale experiment / canary rows ---------------------------------------


def _installed_active(client) -> str:
    _mark_ready()
    agent_id = client.post(INSTALL, json={}).json()["agent"]["id"]
    _set_status(agent_id, "active", "arn:aws:bedrock-agentcore:us-west-2:1:harness/h1")
    return agent_id


def test_stale_experiment_referencing_the_preset_cannot_act(client, monkeypatch):
    agent_id = _installed_active(client)
    db = SessionLocal()
    exp = Experiment(workspace_id=DEFAULT_WORKSPACE_ID, name="EXP-stale", agent_id=agent_id,
                     agent_name=ARCHITECT.name, status="running", stage="abtest",
                     artifacts={"agent_meta": {"system_prompt": "x"},
                                "abtest": {"ab_test_id": "ab1"},
                                "recommend": {"recommended_prompt": "evil"}})
    db.add(exp)
    db.commit()
    exp_id = exp.id
    db.close()
    monkeypatch.setattr(exp_svc, "data_client", lambda *_a, **_k: pytest.fail("AWS reached"))
    spec_before = _agent(agent_id).spec
    for action in ("accept", "bundles", "promote", "traffic", "verdict", "cleanup"):
        res = client.post(f"/api/experiments/{exp_id}/action", json={"action": action})
        assert res.status_code == 403, (action, res.text)
        assert res.json()["code"] == "agent.system_managed"
    # direct service entry points (what a background thread would run) refuse too
    with pytest.raises(AppError) as exc:
        exp_svc.act_promote(exp_id, exp_svc._noop)
    assert exc.value.code == "agent.system_managed"
    with pytest.raises(AppError):
        exp_svc.run_action(exp_id, "promote", lambda _p: pytest.fail("action body ran"))
    db = SessionLocal()
    row = db.get(Experiment, exp_id)
    assert row.running_action is None and row.status == "running"  # no state written
    db.close()
    assert _agent(agent_id).spec == spec_before


def test_stale_canary_referencing_the_preset_cannot_act(client, monkeypatch):
    agent_id = _installed_active(client)
    db = SessionLocal()
    row = RuntimeCanary(workspace_id=DEFAULT_WORKSPACE_ID, name="CANARY-stale",
                        champion_agent_id=agent_id, champion_agent_name=ARCHITECT.name,
                        challenger_agent_id=agent_id, challenger_agent_name=ARCHITECT.name,
                        status="running", stage="verdict",
                        artifacts={"agent_meta": {"id": agent_id, "resource_id": "h1"},
                                   "edited_spec": {"system_prompt": "evil"}, "rounds": [],
                                   "setup": {"ab_test_id": "ab1", "v_candidate": "9"},
                                   "verdict": {"decision": "promote"}})
    db.add(row)
    db.commit()
    canary_id = row.id
    db.close()
    monkeypatch.setattr(canary_svc, "data_client", lambda *_a, **_k: pytest.fail("AWS reached"))
    monkeypatch.setattr(canary_svc, "control_client", lambda *_a, **_k: pytest.fail("AWS reached"))
    spec_before = _agent(agent_id).spec
    for action in ("setup", "traffic", "verdict", "advance", "complete", "rollback", "cleanup"):
        res = client.post(f"/api/runtime-canaries/{canary_id}/action", json={"action": action})
        assert res.status_code == 403, (action, res.text)
        assert res.json()["code"] == "agent.system_managed"
    for fn in (
        lambda: canary_svc.act_complete(canary_id, exp_svc._noop, allow_non_significant=True),
        lambda: canary_svc.act_rollback(canary_id, exp_svc._noop),
        lambda: canary_svc.act_setup(canary_id, exp_svc._noop),
        lambda: canary_svc.run_action(canary_id, "complete", lambda _p: pytest.fail("ran")),
    ):
        with pytest.raises(AppError) as exc:
            fn()
        assert exc.value.code == "agent.system_managed"
    db = SessionLocal()
    row = db.get(RuntimeCanary, canary_id)
    assert row.running_action is None and row.status == "running"
    db.close()
    assert _agent(agent_id).spec == spec_before and _agent(agent_id).version is None


def test_allowed_tools_enforce_sdk_bounds():
    with pytest.raises(ValueError, match="1–64 characters"):
        AgentSpec(name="plain-harness", method="harness", system_prompt="hi",
                  allowed_tools=["x" * 65])
    ok = AgentSpec(name="plain-harness", method="harness", system_prompt="hi",
                   allowed_tools=["*", "x" * 64, "@srv/tool"])
    assert ok.allowed_tools == ["*", "x" * 64, "@srv/tool"]
