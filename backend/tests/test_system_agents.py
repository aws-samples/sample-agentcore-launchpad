"""System-managed presets (SE-038): server-owned identity, protected mutation paths,
idempotent admin install, no AWS on reads — all hermetic (deploy launch stubbed, AWS
client factory made to fail loudly wherever a read path might reach for it)."""

import json
import threading
import time
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
from app.schemas.agent import DEFAULT_MODEL_ID, AgentSpec
from app.services import agent_iam, aws_clients
from app.services import users as users_service
from app.services.agentcore import harness as hc
from app.services.runtime_discovery import _display_name
from app.system_agents import presets, service
from app.system_agents.presets import ARCHITECT, InstallOptions, PresetEdit

from .conftest import ws_ctx

KEY = ARCHITECT.key
INSTALL = f"/api/system-agents/{KEY}/install"
BUCKET = "launchpad-artifacts-test"
OAUTH_PROVIDER = ("arn:aws:bedrock-agentcore:us-west-2:111122223333:token-vault/default/"
                  "oauth2credentialprovider/launchpad-gw-m2m")
READY_RESOURCES = {
    "artifacts_bucket": BUCKET,
    "execution_role_arn": "arn:aws:iam::111122223333:role/launchpad-agent-execution-role",
    "oauth_provider_arn": OAUTH_PROVIDER,
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
def no_network(monkeypatch):
    """Independent of the client-factory stubs: any socket connect is a failure, so a
    test that lost its stubs cannot quietly reach AWS (or anything else)."""
    import socket

    def refuse(self, *args, **kwargs):
        raise AssertionError(f"network connect attempted during a hermetic test: {args}")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)


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
    addressed = presets.build_spec(ARCHITECT, BUCKET, InstallOptions(), digest="ab" * 32)
    assert addressed.skills == [f"s3://{BUCKET}/system-skills/{ARCHITECT.name}/1.0.0-abababababab/"]
    assert presets.skill_release_from_spec(addressed.model_dump()) == ("1.0.0", "abababababab")
    assert presets.skill_version_from_spec(spec.model_dump()) == "1.0.0"
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
    ]  # the install itself scopes to the content-addressed release directory
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

    # the stored default carries the OpenAI reasoning effort; moving to Claude without
    # clearing it is an unsupported pairing and is refused before any job is queued
    refused = client.post(INSTALL, json={"model_id": "global.anthropic.claude-opus-5"})
    assert refused.status_code == 422 and refused.json()["code"] == "system_agent.invalid_options"
    assert _job_count() == 1
    changed = client.post(INSTALL, json={"model_id": "global.anthropic.claude-opus-5",
                                         "clear": ["reasoning_effort"]})
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
    # One ledger-only READ first. The host gate once saw a single 404 among six
    # simultaneous FIRST requests to a fresh app; FastAPI 0.139's `_IncludedRouter`
    # materializes its effective route candidates lazily on first match, so the very
    # first concurrent requests into a router can race that setup. The read warms
    # nothing the install race depends on (no rows, no jobs, no AWS) and every thread
    # below still races the SAME durable insert. Non-success bodies are captured.
    assert client.get("/api/system-agents").status_code == 200
    codes: list[int] = []
    bodies: list[str] = []
    errors: list[str] = []
    lock = threading.Lock()

    def worker() -> None:
        try:
            res = client.post(INSTALL, json={})
            with lock:
                codes.append(res.status_code)
                if res.status_code not in (200, 202):
                    bodies.append(res.text)
        except Exception as exc:  # noqa: BLE001 — collected, asserted below
            with lock:
                errors.append(repr(exc))

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors
    assert set(codes) <= {200, 202}, (codes, bodies)
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
    monkeypatch.setattr(system_router, "start_uninstall_async", lambda _jid: None)
    assert admin.delete(f"/api/system-agents/{KEY}").status_code == 202


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
                      spec=spec or presets.build_spec(
                          ARCHITECT, BUCKET, InstallOptions(),
                          digest=presets.snapshot_bundle(ARCHITECT).digest).model_dump())
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


# ---------------------------------------------------------------------------
# correction pass 3 — durable uninstall, atomic pin, immutable publication, KB IAM
# ---------------------------------------------------------------------------

import io  # noqa: E402
import pathlib  # noqa: E402
from hashlib import sha256  # noqa: E402

from botocore.exceptions import ClientError  # noqa: E402
from sqlalchemy import update  # noqa: E402

import app.deployer.pipeline as pipeline_module  # noqa: E402
from app.system_agents import uninstall as uninstall_module  # noqa: E402

DIGEST12 = presets.snapshot_bundle(ARCHITECT).digest[:12]
PREFIX = f"system-skills/{ARCHITECT.name}/1.0.0-{DIGEST12}/"
MANIFEST = f"{PREFIX}{service.MANIFEST_KEY}"


def _s3_error(code: str, op: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": code}}, op)


class _S3:
    """Fake S3 with real conditional-write semantics (If-None-Match: * / If-Match) and
    an optional hook run before each put (competing writers, corruption on store)."""

    def __init__(self, objects: dict | None = None):
        self.objects: dict[str, bytes] = dict(objects or {})
        self.puts: list[dict] = []
        self.before_put = None  # callable(fake, kwargs) → None
        self.corrupt_on_store: set[str] = set()

    def _etag(self, key: str) -> str:
        return '"' + sha256(self.objects[key]).hexdigest()[:16] + '"'

    def get_object(self, Bucket, Key):
        if Key not in self.objects:
            raise _s3_error("NoSuchKey", "GetObject")
        return {"Body": io.BytesIO(self.objects[Key]), "ETag": self._etag(Key)}

    def list_objects_v2(self, Bucket, Prefix, ContinuationToken=None):
        keys = sorted(k for k in self.objects if k.startswith(Prefix))
        start = int(ContinuationToken or 0)
        page, rest = keys[start:start + 2], keys[start + 2:]  # tiny pages exercise paging
        out = {"Contents": [{"Key": k} for k in page], "IsTruncated": bool(rest)}
        if rest:
            out["NextContinuationToken"] = str(start + 2)
        return out

    def put_object(self, **kwargs):
        if self.before_put:
            self.before_put(self, kwargs)
        key = kwargs["Key"]
        if kwargs.get("IfNoneMatch") == "*" and key in self.objects:
            raise _s3_error("PreconditionFailed", "PutObject")
        stale = key not in self.objects or self._etag(key) != kwargs.get("IfMatch")
        if "IfMatch" in kwargs and stale:
            raise _s3_error("PreconditionFailed", "PutObject")
        self.puts.append(kwargs)
        body = kwargs["Body"]
        self.objects[key] = body[:-1] + b"?" if key in self.corrupt_on_store else body
        return {}


def _snapshot():
    return presets.snapshot_bundle(ARCHITECT)


def _pinned_preset(monkeypatch, fake: _S3, *, spec=None, pin="valid"):
    """A preset row + deployment + job whose pin is valid / missing / malformed / stale.
    An earlier row from the same test is released first (one live key per workspace)."""
    db = SessionLocal()
    for row in db.query(Agent).filter(Agent.system_key == KEY, Agent.status != "deleted"):
        row.status = "deleted"
    db.commit()
    db.close()
    agent_id, dep_id, job_id = _persist_preset(spec=spec)
    db = SessionLocal()
    job = db.get(Job, job_id)
    payload = dict(job.payload)
    if pin == "valid":
        payload["preset_bundle"] = _snapshot().release()
    elif pin == "missing":
        payload.pop("preset_bundle", None)
    elif pin == "malformed":
        payload["preset_bundle"] = {"version": "1.0.0"}  # no digest, no files
    elif pin == "stale":
        payload["preset_bundle"] = {**_snapshot().release(), "digest": "f" * 64}
    job.payload = payload
    db.commit()
    db.close()
    monkeypatch.setattr(aws_clients, "client", lambda service_, ws, **kw: fake)
    return _ctx(agent_id, dep_id, job_id), _agent(agent_id)


# ---- (2) atomic release pin --------------------------------------------------


def test_install_pins_the_validated_snapshot_in_the_same_commit_as_the_job(client):
    _mark_ready()
    body = client.post(INSTALL, json={}).json()
    db = SessionLocal()
    try:
        job = db.get(Job, body["job_id"])
        pin = job.payload["preset_bundle"]
    finally:
        db.close()
    snap = _snapshot()
    assert pin == {"version": "1.0.0", "digest": snap.digest, "files": snap.file_digests()}
    assert "SKILL.md" in pin["files"] and len(pin["files"]) == len(snap.files)
    # the spec loads exactly this snapshot's content-addressed release directory
    assert body["agent"]["spec"]["skills"] == [
        f"s3://{BUCKET}/system-skills/{ARCHITECT.name}/1.0.0-{snap.digest[:12]}/"
    ]


def test_crash_inside_the_queue_commit_leaves_no_half_queued_install_or_repair(client, monkeypatch):
    """Row, deployment, job and pin are one transaction: a crash in create_deployment
    (the only commit) leaves nothing runnable behind — not a job without a pin."""
    _mark_ready()

    def boom(*_a, **_k):
        raise RuntimeError("simulated crash inside the queue commit")

    tolerant = TestClient(client.app, raise_server_exceptions=False)
    # nested contexts restore ONLY create_deployment — never the autouse AWS/deploy stubs
    with monkeypatch.context() as crash:
        crash.setattr(service, "create_deployment", boom)
        assert tolerant.post(INSTALL, json={}).status_code == 500
    assert _rows(system_key=KEY) == [] and _job_count() == 0
    agent_id = client.post(INSTALL, json={}).json()["agent"]["id"]
    _set_status(agent_id, "active", "arn:aws:bedrock-agentcore:us-west-2:1:harness/h1")
    with monkeypatch.context() as crash:
        crash.setattr(service, "create_deployment", boom)
        assert tolerant.post(INSTALL, json={"force": True}).status_code == 500
    assert _agent(agent_id).status == "active" and _job_count() == 1  # claim rolled back
    db = SessionLocal()
    try:
        assert all(j.payload.get("preset_bundle") for j in db.query(Job).all())
    finally:
        db.close()


@pytest.mark.parametrize("pin", ["missing", "malformed"])
def test_package_fails_closed_without_a_valid_release_pin(monkeypatch, pin):
    fake = _S3()
    ctx, agent = _pinned_preset(monkeypatch, fake, pin=pin)
    with pytest.raises(RuntimeError, match="no valid release pin"):
        STAGES["package"](ctx, agent)
    assert fake.puts == [] and fake.objects == {}


def test_package_refuses_a_stale_pin_and_a_queued_version_mismatch(monkeypatch):
    fake = _S3()
    ctx, agent = _pinned_preset(monkeypatch, fake, pin="stale")
    with pytest.raises(RuntimeError, match="pinned bundle mismatch"):
        STAGES["package"](ctx, agent)
    stale = presets.build_spec(ARCHITECT, BUCKET, InstallOptions()).model_dump()
    stale["skills"] = [f"s3://{BUCKET}/system-skills/{ARCHITECT.name}/0.9.0/"]
    ctx, agent = _pinned_preset(monkeypatch, fake, spec=stale)
    with pytest.raises(RuntimeError, match="queued release mismatch"):
        STAGES["package"](ctx, agent)
    assert fake.puts == []


def test_pipeline_resume_reruns_a_pinned_package_stage(monkeypatch):
    """A restarted worker re-enters the package stage through execute_deploy_job with
    the pin still on the job — the real resume path, not a direct stage call. The
    job's workspace row must carry the bucket the spec names (entry guard)."""
    _mark_ready()
    fake = _S3()
    ctx, agent = _pinned_preset(monkeypatch, fake)
    calls: list[str] = []
    monkeypatch.setitem(STAGES, "generate", lambda c, a: calls.append("generate") or
                        pipeline_module.StageResult(detail="stub"))
    for name in ("provision", "deploy", "register"):
        monkeypatch.setitem(STAGES, name, lambda c, a, n=name: calls.append(n) or
                            pipeline_module.StageResult(detail="stub"))
    pipeline_module.execute_deploy_job(ctx.job_id)
    assert calls == ["generate", "provision", "deploy", "register"]
    assert MANIFEST in fake.objects and _agent(agent.id).status == "active"


# ---- (3)/(4) one byte snapshot, conflict-safe publication, honest repair ------


def test_publish_uploads_the_snapshot_bytes_conditionally_and_verifies_readback(monkeypatch):
    fake = _S3()
    ctx, agent = _pinned_preset(monkeypatch, fake)
    reads = {"n": 0}
    real = presets.snapshot_bundle

    def counting(preset):
        reads["n"] += 1
        return real(preset)

    monkeypatch.setattr(presets, "snapshot_bundle", counting)
    result = STAGES["package"](ctx, agent)
    assert reads["n"] == 1  # the checkout is read ONCE per run
    monkeypatch.setattr(presets, "snapshot_bundle", real)
    snap = _snapshot()
    keys = [p["Key"] for p in fake.puts]
    assert keys[-1] == MANIFEST and all(k.startswith(PREFIX) for k in keys)
    assert all(p.get("IfNoneMatch") == "*" for p in fake.puts)  # never an unconditional write
    for rel, body in snap.files.items():
        assert fake.objects[f"{PREFIX}{rel}"] == body  # uploaded bytes ARE the snapshot bytes
    manifest = json.loads(fake.objects[MANIFEST])
    assert manifest["digest"] == snap.digest and manifest["files"] == snap.file_digests()
    assert "published" in result.detail and snap.digest[:12] in result.detail
    # identical replay: zero writes, still verified against every object
    n = len(fake.puts)
    again = STAGES["package"](ctx, agent)
    assert len(fake.puts) == n and "already published · verified" in again.detail


def test_publish_detects_a_bad_upload_by_reading_back(monkeypatch):
    fake = _S3()
    fake.corrupt_on_store.add(f"{PREFIX}SKILL.md")
    ctx, agent = _pinned_preset(monkeypatch, fake)
    with pytest.raises(RuntimeError, match="post-upload verification failed"):
        STAGES["package"](ctx, agent)


def test_publish_restarts_after_a_partial_upload_without_rewriting(monkeypatch):
    snap = _snapshot()
    partial = {f"{PREFIX}{rel}": body for rel, body in list(snap.files.items())[:2]}
    fake = _S3(partial)
    ctx, agent = _pinned_preset(monkeypatch, fake)
    result = STAGES["package"](ctx, agent)
    written = {p["Key"] for p in fake.puts}
    assert not (written & set(partial))  # existing identical objects were not rewritten
    assert MANIFEST in written and "published" in result.detail


def test_competing_publication_with_different_bytes_is_never_overwritten(monkeypatch):
    fake = _S3()
    ctx, agent = _pinned_preset(monkeypatch, fake)
    foreign = f"{PREFIX}references/intake-options.md"

    def competitor(s3: _S3, kwargs):
        if kwargs["Key"].endswith("SKILL.md") and foreign not in s3.objects:
            s3.objects[foreign] = b"# somebody else's release\n"  # lands after our manifest read

    fake.before_put = competitor
    with pytest.raises(RuntimeError, match="competing publication"):
        STAGES["package"](ctx, agent)
    assert fake.objects[foreign] == b"# somebody else's release\n"
    assert MANIFEST not in fake.objects


def test_competing_manifest_with_the_same_content_is_accepted(monkeypatch):
    snap = _snapshot()
    fake = _S3()
    ctx, agent = _pinned_preset(monkeypatch, fake)

    def racer(s3: _S3, kwargs):
        if kwargs["Key"] == MANIFEST and MANIFEST not in s3.objects:
            s3.objects[MANIFEST] = json.dumps({"name": ARCHITECT.name, **snap.release()}).encode()

    fake.before_put = racer
    assert "published" in STAGES["package"](ctx, agent).detail


def test_competing_manifest_with_different_content_fails(monkeypatch):
    fake = _S3()
    ctx, agent = _pinned_preset(monkeypatch, fake)

    def racer(s3: _S3, kwargs):
        if kwargs["Key"] == MANIFEST and MANIFEST not in s3.objects:
            s3.objects[MANIFEST] = json.dumps({"version": "1.0.0", "digest": "0" * 64,
                                               "files": {}}).encode()

    fake.before_put = racer
    with pytest.raises(RuntimeError, match="competing publication claimed"):
        STAGES["package"](ctx, agent)
    assert json.loads(fake.objects[MANIFEST])["digest"] == "0" * 64  # untouched


@pytest.mark.parametrize("manifest", [
    b"not json", json.dumps({"version": "1.0.0"}).encode(),
    json.dumps({"version": "1.0.0", "digest": "0" * 64, "files": {}}).encode(),
])
def test_malformed_or_conflicting_manifest_fails_before_any_write(monkeypatch, manifest):
    fake = _S3({MANIFEST: manifest})
    ctx, agent = _pinned_preset(monkeypatch, fake)
    with pytest.raises(RuntimeError, match="immutable"):
        STAGES["package"](ctx, agent)
    assert fake.puts == []


def _published(snap) -> dict[str, bytes]:
    objs = {f"{PREFIX}{rel}": body for rel, body in snap.files.items()}
    objs[MANIFEST] = json.dumps({"name": ARCHITECT.name, **snap.release()}).encode()
    return objs


def test_repair_restores_a_missing_required_object_instead_of_saying_verified(monkeypatch):
    snap = _snapshot()
    objs = _published(snap)
    del objs[f"{PREFIX}SKILL.md"]
    fake = _S3(objs)
    ctx, agent = _pinned_preset(monkeypatch, fake)
    result = STAGES["package"](ctx, agent)
    assert fake.objects[f"{PREFIX}SKILL.md"] == snap.files["SKILL.md"]
    assert [p["Key"] for p in fake.puts] == [f"{PREFIX}SKILL.md"]
    assert fake.puts[0].get("IfNoneMatch") == "*"
    assert "repaired" in result.detail and "verified" not in result.detail


@pytest.mark.parametrize("mutation", ["modified", "truncated"])
def test_repair_replaces_a_corrupt_object_only_against_its_etag(monkeypatch, mutation):
    snap = _snapshot()
    objs = _published(snap)
    good = snap.files["references/intake-options.md"]
    objs[f"{PREFIX}references/intake-options.md"] = (
        good.replace(b"Round one", b"Round 1", 1) if mutation == "modified"
        else good[: len(good) // 2]
    )
    fake = _S3(objs)
    ctx, agent = _pinned_preset(monkeypatch, fake)
    result = STAGES["package"](ctx, agent)
    put = fake.puts[-1]
    assert put["Key"] == f"{PREFIX}references/intake-options.md" and put["IfMatch"]
    assert fake.objects[put["Key"]] == good and "repaired" in result.detail
    # a corrupt object changed by someone else between read and repair is left alone
    fake2 = _S3(dict(objs))
    ticks = {"n": 0}

    def churn(s3: _S3, kw):  # the object changes again between our read and our If-Match put
        ticks["n"] += 1
        s3.objects[kw["Key"]] = b"changed again %d" % ticks["n"]

    fake2.before_put = churn
    ctx2, agent2 = _pinned_preset(monkeypatch, fake2)
    with pytest.raises(RuntimeError, match="changed while being repaired"):
        STAGES["package"](ctx2, agent2)


# ---- (1) durable uninstall: exclusive claim, fenced strict teardown -----------------


class _HarnessControl:
    """Fake bedrock-agentcore-control. DeleteHarness answers DELETING; GetHarness walks a
    scripted status sequence and finally raises ResourceNotFound (the only "gone").
    Gateway targets: paginated one per page; delete/get behaviour scripted per test."""

    class exceptions:
        class ResourceNotFoundException(Exception):
            pass

        class AccessDeniedException(Exception):
            pass

    def __init__(self, statuses=("DELETING", None), missing=False, targets=None,
                 target_delete="ok", target_statuses=("DELETING", None)):
        self.statuses = list(statuses)  # None ⇒ ResourceNotFound
        self.missing = missing
        self.calls: list[tuple] = []
        self.targets = list(targets or [])  # [{name, targetId}]
        self.target_delete = target_delete  # ok | denied | notfound
        self.target_statuses = list(target_statuses)
        self.target_calls: list[tuple] = []

    def delete_harness(self, harnessId):
        self.calls.append(("delete", harnessId))
        if self.missing:
            raise self.exceptions.ResourceNotFoundException(harnessId)
        return {"harness": {"harnessId": harnessId, "status": "DELETING"}}

    def get_harness(self, harnessId):
        self.calls.append(("get", harnessId))
        status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
        if status is None:
            raise self.exceptions.ResourceNotFoundException(harnessId)
        return {"harness": {"harnessId": harnessId, "status": status}}

    def list_gateway_targets(self, gatewayIdentifier, maxResults=100, nextToken=None):
        self.target_calls.append(("list", nextToken))
        i = int(nextToken or 0)
        page = {"items": self.targets[i:i + 1]}
        if i + 1 < len(self.targets):
            page["nextToken"] = str(i + 1)
        return page

    def delete_gateway_target(self, gatewayIdentifier, targetId):
        self.target_calls.append(("delete", targetId))
        if self.target_delete == "denied":
            raise self.exceptions.AccessDeniedException("AccessDenied: target remains")
        if self.target_delete == "notfound":
            raise self.exceptions.ResourceNotFoundException(targetId)
        return {"targetId": targetId, "status": "DELETING"}

    def get_gateway_target(self, gatewayIdentifier, targetId):
        self.target_calls.append(("get", targetId))
        status = (self.target_statuses.pop(0) if len(self.target_statuses) > 1
                  else self.target_statuses[0])
        if status is None:
            raise self.exceptions.ResourceNotFoundException(targetId)
        return {"targetId": targetId, "status": status}


class _Iam:
    """Fake IAM: one tagged per-agent role; delete_role may fail."""

    class _NoSuchEntity(Exception):
        pass

    def __init__(self, roles: dict | None = None, delete_ok=True):
        self.roles = dict(roles or {})  # name → {"Arn", "Tags"}
        self.delete_ok = delete_ok
        self.deleted: list[str] = []

    def get_role(self, RoleName):
        if RoleName not in self.roles:
            raise type("NoSuchEntityException", (Exception,), {})(RoleName)
        return {"Role": {"RoleName": RoleName, **self.roles[RoleName]}}

    def list_role_policies(self, RoleName):
        if RoleName not in self.roles:
            raise type("NoSuchEntityException", (Exception,), {})(RoleName)
        return {"PolicyNames": ["launchpad-caps-x"]}

    def delete_role_policy(self, RoleName, PolicyName):
        return {}

    def delete_role(self, RoleName):
        if not self.delete_ok:
            raise RuntimeError("DeleteConflict: role still attached")
        self.roles.pop(RoleName, None)
        self.deleted.append(RoleName)
        return {}


def _owned_role(agent_id: str) -> dict:
    name = agent_iam.role_name_for(ARCHITECT.name, agent_id)
    return {name: {"Arn": f"arn:aws:iam::111122223333:role/{name}",
                   "Tags": [{"Key": agent_iam.MANAGED_TAG_KEY, "Value": agent_id}]}}


def _strict_stubs(monkeypatch, control=None, role_ok=True, iam=None, agent_id=None):
    """Low-level teardown stubs: the real worker and real wrappers run; only the AWS
    clients are fakes. The lock directory is a fresh temp dir per test."""
    import tempfile

    control = control or _HarnessControl()
    monkeypatch.setattr(harness_module, "control_client", lambda _ws: control)
    monkeypatch.setattr(uninstall_module, "_sleep", lambda _s: None)
    monkeypatch.setattr(uninstall_module, "LOCK_DIR",
                        pathlib.Path(tempfile.mkdtemp(prefix="se038-locks-")))
    iam = iam or _Iam(_owned_role(agent_id) if agent_id else {}, delete_ok=role_ok)

    def client(service_name, ws, **kw):
        if service_name == "iam":
            return iam
        raise AssertionError(f"unexpected AWS client {service_name}")

    monkeypatch.setattr(aws_clients, "client", client)
    monkeypatch.setattr(system_router, "start_uninstall_async", lambda _j: None)
    return control, iam.deleted


def _active_preset(client) -> str:
    _mark_ready()
    agent_id = client.post(INSTALL, json={}).json()["agent"]["id"]
    _set_status(agent_id, "active", "arn:aws:bedrock-agentcore:us-west-2:1:harness/h1")
    return agent_id


def _job(job_id) -> Job:
    db = SessionLocal()
    try:
        return db.get(Job, job_id)
    finally:
        db.close()


def test_uninstall_claims_identity_and_only_a_verified_teardown_releases_it(client, monkeypatch):
    agent_id = _active_preset(client)
    control, role_calls = _strict_stubs(monkeypatch, agent_id=agent_id)
    res = client.delete(f"/api/system-agents/{KEY}")
    assert res.status_code == 202, res.text
    body = res.json()
    assert body["started"] is True and body["attempt"] == 1
    assert _agent(agent_id).status == "uninstalling"
    status = _status(client)
    assert status["status"] == "uninstalling" and status["operation"]["job_id"] == body["job_id"]
    assert status["can_install"] is False and status["can_repair"] is False
    assert status["can_uninstall"] is False  # a live job owns the teardown
    assert client.post(INSTALL, json={}).json()["code"] == "system_agent.uninstalling"
    assert client.post(INSTALL, json={"force": True}).json()["code"] == "system_agent.uninstalling"
    again = client.delete(f"/api/system-agents/{KEY}").json()
    assert again["job_id"] == body["job_id"] and again["started"] is False
    db = SessionLocal()
    db.add(Agent(workspace_id=DEFAULT_WORKSPACE_ID, name=ARCHITECT.name, method="harness",
                 status="deploying", spec={}, system_key=KEY))
    from sqlalchemy.exc import IntegrityError as _IE
    with pytest.raises(_IE):
        db.flush()
    db.rollback()
    db.close()

    uninstall_module.execute_uninstall_job(body["job_id"])
    # DeleteHarness accepted (DELETING) → polled until ResourceNotFound → role deleted
    assert control.calls[0] == ("delete", "h1") and control.calls[-1] == ("get", "h1")
    assert role_calls == [agent_iam.role_name_for(ARCHITECT.name, agent_id)]
    assert _agent(agent_id).status == "deleted"
    job = _job(body["job_id"])
    assert job.status == "succeeded" and job.payload["aws_resource_deleted"] is True
    assert {k: v["state"] for k, v in job.payload["progress"].items()} == {
        "kb_target": "done", "harness": "done", "role": "done"}
    assert job.payload["progress"]["harness"]["resource_id"] == "h1"
    assert _status(client)["status"] == "not_installed"
    assert client.post(INSTALL, json={}).json()["agent"]["id"] != agent_id


@pytest.mark.parametrize("scenario", ["delete_failed", "timeout", "role_false", "kb_target"])
def test_teardown_that_does_not_finish_stays_uninstalling_and_retryable(
    client, monkeypatch, scenario
):
    """Host reproduction: an accepted DeleteHarness (DELETING) is not a deletion, and
    an ignored IAM False is not a cleanup — none of these may mark the row deleted."""
    agent_id = _active_preset(client)
    target_name = uninstall_module.kbgw.agentic_target_name(ARCHITECT.name)
    if scenario == "delete_failed":
        control, _ = _strict_stubs(monkeypatch, _HarnessControl(["DELETING", "DELETE_FAILED"]),
                                   agent_id=agent_id)
    elif scenario == "timeout":
        control, _ = _strict_stubs(monkeypatch, _HarnessControl(["DELETING"]), agent_id=agent_id)
        monkeypatch.setattr(uninstall_module, "HARNESS_GONE_TIMEOUT_S", 0)
    elif scenario == "role_false":
        control, _ = _strict_stubs(monkeypatch, role_ok=False, agent_id=agent_id)
    else:
        # the ACTUAL nested path: DeleteGatewayTarget answers AccessDenied (the legacy
        # helper would swallow this and report success)
        control, _ = _strict_stubs(
            monkeypatch,
            _HarnessControl(targets=[{"name": "other", "targetId": "t-other"},
                                     {"name": target_name, "targetId": "t-old"}],
                            target_delete="denied"),
            agent_id=agent_id,
        )
        _mark_ready(resources={**READY_RESOURCES, "kb_gateway_id": "gw-1"})
    job_id = client.delete(f"/api/system-agents/{KEY}").json()["job_id"]
    uninstall_module.execute_uninstall_job(job_id)
    row = _agent(agent_id)
    assert row.status == "uninstalling" and row.error.startswith("uninstall failed")
    job = _job(job_id)
    assert job.status == "failed"
    progress = job.payload["progress"]
    if scenario == "kb_target":
        assert progress["kb_target"]["state"] == "failed" and "harness" not in progress
        assert progress["kb_target"]["target_id"] == "t-old"  # pinned before the delete
        assert "AccessDenied" in progress["kb_target"]["detail"]
        assert control.target_calls[:3] == [("list", None), ("list", "1"), ("delete", "t-old")]
        assert control.calls == []  # nothing later ran
    elif scenario == "role_false":
        assert progress["harness"]["state"] == "done" and progress["role"]["state"] == "failed"
    else:
        assert progress["harness"]["state"] == "failed" and "role" not in progress
    status = _status(client)
    assert status["status"] == "uninstalling" and status["operation"]["retryable"] is True
    assert status["can_uninstall"] is True and status["can_repair"] is False
    assert client.post(INSTALL, json={}).json()["code"] == "system_agent.uninstalling"
    # the retry carries verified steps forward, reuses the PINNED target id (no name
    # lookup), and finishes cleanly
    monkeypatch.setattr(uninstall_module, "HARNESS_GONE_TIMEOUT_S", 90)
    control2, _ = _strict_stubs(monkeypatch, _HarnessControl(missing=True), agent_id=agent_id)
    retry = client.delete(f"/api/system-agents/{KEY}").json()
    assert retry["started"] is True and retry["attempt"] == 2
    carried = _job(retry["job_id"]).payload.get("progress", {})
    assert all(v["state"] in ("done", "pinned") for v in carried.values())
    if scenario == "kb_target":
        # nothing was verified done, but the pinned target identity IS carried
        assert set(carried) == {"kb_target"}
        assert carried["kb_target"]["target_id"] == "t-old"
        assert carried["kb_target"]["gateway_id"] == "gw-1"
        assert carried["kb_target"]["state"] == "pinned"
    uninstall_module.execute_uninstall_job(retry["job_id"])
    assert _agent(agent_id).status == "deleted" and _job(retry["job_id"]).status == "succeeded"
    if scenario == "role_false":
        assert ("delete", "h1") not in control2.calls  # harness step carried (same id)
    if scenario == "kb_target":
        # the retry deleted the PINNED target and never resolved a name again
        assert ("delete", "t-old") in control2.target_calls
        assert all(c[0] != "list" for c in control2.target_calls)


def test_simultaneous_uninstall_requests_share_one_owner_job(client, monkeypatch):
    """Host probe replica: both callers finish their latest-job lookup before either
    claims — the optimistic CAS lets exactly one create the job."""
    agent_id = _active_preset(client)
    _strict_stubs(monkeypatch, agent_id=agent_id)
    barrier = threading.Barrier(2)
    original = service.latest_uninstall_job

    def synchronized(db, aid):
        found = original(db, aid)
        barrier.wait(timeout=10)
        return found

    monkeypatch.setattr(service, "latest_uninstall_job", synchronized)
    results, errors = [], []

    def worker():
        try:
            with SessionLocal() as db:
                res = service.uninstall_preset(db, db.get(Workspace, DEFAULT_WORKSPACE_ID),
                                               ARCHITECT)
                results.append((res.job.id, res.started, res.attempt))
        except Exception as exc:  # noqa: BLE001
            errors.append(repr(exc))

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for th in threads:
        th.start()
    for th in threads:
        th.join(timeout=20)
    assert not errors, errors
    assert len({r[0] for r in results}) == 1 and sorted(r[1] for r in results) == [False, True]
    db = SessionLocal()
    assert db.query(Job).filter(Job.type == service.UNINSTALL_JOB_TYPE).count() == 1
    db.close()
    assert _agent(agent_id).status == "uninstalling"


def test_simultaneous_failed_retries_share_one_new_attempt(client, monkeypatch):
    agent_id = _active_preset(client)
    _strict_stubs(monkeypatch, role_ok=False, agent_id=agent_id)
    first = client.delete(f"/api/system-agents/{KEY}").json()["job_id"]
    uninstall_module.execute_uninstall_job(first)
    assert _job(first).status == "failed" and _agent(agent_id).status == "uninstalling"
    barrier = threading.Barrier(2)
    original = service.latest_uninstall_job

    def synchronized(db, aid):
        found = original(db, aid)
        barrier.wait(timeout=10)
        return found

    monkeypatch.setattr(service, "latest_uninstall_job", synchronized)
    results = []

    def worker():
        with SessionLocal() as db:
            res = service.uninstall_preset(db, db.get(Workspace, DEFAULT_WORKSPACE_ID), ARCHITECT)
            results.append((res.job.id, res.started, res.attempt))

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for th in threads:
        th.start()
    for th in threads:
        th.join(timeout=20)
    assert len({r[0] for r in results}) == 1 and {r[2] for r in results} == {2}
    assert sorted(r[1] for r in results) == [False, True]
    db = SessionLocal()
    assert db.query(Job).filter(Job.type == service.UNINSTALL_JOB_TYPE).count() == 2
    db.close()


def test_duplicate_worker_invocations_run_exactly_once(client, monkeypatch):
    agent_id = _active_preset(client)
    control, role_calls = _strict_stubs(monkeypatch, agent_id=agent_id)
    job_id = client.delete(f"/api/system-agents/{KEY}").json()["job_id"]
    uninstall_module.execute_uninstall_job(job_id)
    uninstall_module.execute_uninstall_job(job_id)  # terminal → inert
    uninstall_module.execute_uninstall_job(job_id, resume=True)  # terminal → inert
    assert len(role_calls) == 1 and control.calls.count(("delete", "h1")) == 1
    assert _job(job_id).status == "succeeded"
    # a second worker racing on a QUEUED job loses the CAS and does nothing
    agent_id2 = client.post(INSTALL, json={}).json()["agent"]["id"]
    _set_status(agent_id2, "active", "arn:aws:bedrock-agentcore:us-west-2:1:harness/h2")
    job2 = client.delete(f"/api/system-agents/{KEY}").json()["job_id"]
    db = SessionLocal()
    db.get(Job, job2).status = "running"  # the first worker already claimed it
    db.commit()
    db.close()
    control2, role_calls2 = _strict_stubs(monkeypatch, agent_id=agent_id2)
    uninstall_module.execute_uninstall_job(job2)  # non-resume: not ours
    assert _job(job2).status == "running" and _agent(agent_id2).status == "uninstalling"
    assert role_calls2 == []
    uninstall_module.execute_uninstall_job(job2, resume=True)  # startup resume may adopt it
    assert _agent(agent_id2).status == "deleted"


def test_stale_attempt_cannot_finish_after_a_newer_attempt_was_queued(client, monkeypatch):
    agent_id = _active_preset(client)
    control, role_calls = _strict_stubs(monkeypatch, agent_id=agent_id)
    first = client.delete(f"/api/system-agents/{KEY}").json()["job_id"]
    db = SessionLocal()
    db.get(Job, first).status = "failed"  # attempt 1 "failed" (its worker is still alive)
    db.commit()
    db.close()
    second = client.delete(f"/api/system-agents/{KEY}").json()
    assert second["attempt"] == 2
    uninstall_module.execute_uninstall_job(first, resume=True)  # the stale worker wakes up
    assert control.calls == [] and role_calls == []  # inert: not the owner
    assert _job(first).status == "failed" and _agent(agent_id).status == "uninstalling"
    uninstall_module.execute_uninstall_job(second["job_id"])
    assert _agent(agent_id).status == "deleted"


def test_late_worker_after_reinstall_never_touches_the_replacement(client, monkeypatch):
    """Reviewer scenario: a worker paused mid-run resumes after the preset was torn
    down and reinstalled — it must not resolve the new install's KB target by name."""
    agent_id = _active_preset(client)
    db = SessionLocal()
    row = db.get(Agent, agent_id)
    row.spec = {**row.spec, "knowledge_bases": [KB_REF]}
    db.commit()
    db.close()
    _mark_ready(resources={**READY_RESOURCES, "kb_gateway_id": "gw-1"})
    target_name = uninstall_module.kbgw.agentic_target_name(ARCHITECT.name)
    control, role_calls = _strict_stubs(
        monkeypatch, _HarnessControl(targets=[{"name": target_name, "targetId": "NEW-TARGET"}]),
        agent_id=agent_id,
    )
    job_id = client.delete(f"/api/system-agents/{KEY}").json()["job_id"]

    # pause the (stale) worker right after it claimed the job, before any cloud step
    real_fence = uninstall_module._fence
    paused = {"n": 0}

    def fence_then_replace(db_, jid):
        paused["n"] += 1
        if paused["n"] == 1:
            # meanwhile another worker completes the same job and the admin reinstalls
            with SessionLocal() as other:
                other.execute(update(Job).where(Job.id == jid).values(status="succeeded"))
                other.execute(update(Agent).where(Agent.id == agent_id).values(status="deleted"))
                other.commit()
            res = client.post(INSTALL, json={"knowledge_bases": [KB_REF]})
            assert res.status_code == 202 and res.json()["agent"]["id"] != agent_id
            paused["new"] = res.json()["agent"]["id"]
        return real_fence(db_, jid)

    monkeypatch.setattr(uninstall_module, "_fence", fence_then_replace)
    uninstall_module.execute_uninstall_job(job_id)
    assert control.target_calls == [] and control.calls == [] and role_calls == []
    assert _agent(paused["new"]).status == "deploying"  # the replacement is untouched
    assert _job(job_id).status == "succeeded"  # the other worker's result stands


def test_crash_before_and_after_the_cloud_step_resumes_through_pending_jobs(client, monkeypatch):
    agent_id = _active_preset(client)
    control, role_calls = _strict_stubs(monkeypatch, agent_id=agent_id)
    job_id = client.delete(f"/api/system-agents/{KEY}").json()["job_id"]
    resumed: list[tuple[str, str]] = []
    monkeypatch.setattr(uninstall_module, "start_uninstall_resume",
                        lambda jid: resumed.append(("uninstall", jid)) or threading.Thread())
    monkeypatch.setattr(pipeline_module, "start_deploy_async",
                        lambda jid: resumed.append(("deploy", jid)) or threading.Thread())
    assert job_id in pipeline_module.resume_pending_jobs() and ("uninstall", job_id) in resumed
    assert _status(client)["status"] == "uninstalling"  # a crash never leaks not_installed
    # crash after DeleteHarness was accepted: the job is left `running`; the resume
    # worker adopts it, finds the harness already gone and completes idempotently
    db = SessionLocal()
    db.get(Job, job_id).status = "running"
    db.commit()
    db.close()
    _strict_stubs(monkeypatch, _HarnessControl(missing=True), agent_id=agent_id)
    uninstall_module.execute_uninstall_job(job_id)  # a non-resume worker must not adopt it
    assert _agent(agent_id).status == "uninstalling"
    uninstall_module.execute_uninstall_job(job_id, resume=True)
    assert _agent(agent_id).status == "deleted" and _job(job_id).status == "succeeded"


def test_worker_rejects_wrong_type_wrong_workspace_and_stolen_rows(client, monkeypatch):
    agent_id = _active_preset(client)
    control, role_calls = _strict_stubs(monkeypatch, agent_id=agent_id)
    job_id = client.delete(f"/api/system-agents/{KEY}").json()["job_id"]
    db = SessionLocal()
    deploy_job = db.query(Job).filter(Job.type == "deploy_agent").first().id
    db.close()
    uninstall_module.execute_uninstall_job(deploy_job)  # wrong type → inert
    assert _job(deploy_job).status == "queued" and control.calls == []
    db = SessionLocal()
    db.get(Job, job_id).workspace_id = "lab-use2"  # wrong workspace
    db.commit()
    db.close()
    uninstall_module.execute_uninstall_job(job_id)
    assert _job(job_id).status == "failed" and "workspaces" in _job(job_id).error
    assert control.calls == [] and _agent(agent_id).status == "uninstalling"
    retry = client.delete(f"/api/system-agents/{KEY}").json()["job_id"]
    _set_status(agent_id, "active")  # someone flipped the row out from under the job
    uninstall_module.execute_uninstall_job(retry)
    assert "no longer uninstalling" in _job(retry).error and control.calls == []
    assert _agent(agent_id).status == "active"


def test_uninstall_waits_for_a_running_deploy_and_repair_waits_for_uninstall(client, monkeypatch):
    _mark_ready()
    agent_id = client.post(INSTALL, json={}).json()["agent"]["id"]
    assert client.delete(f"/api/system-agents/{KEY}").json()["code"] == "agent.deploy_in_progress"
    _set_status(agent_id, "active", "arn:aws:bedrock-agentcore:us-west-2:1:harness/h1")
    stale = SessionLocal()
    stale_agent = stale.get(Agent, agent_id)  # loaded while still active
    _strict_stubs(monkeypatch)
    assert client.delete(f"/api/system-agents/{KEY}").status_code == 202
    with pytest.raises(AppError) as exc:  # the stale snapshot cannot steal the claim
        service._repair(stale, stale_agent, ARCHITECT, BUCKET, None, force=True)
    assert exc.value.code == "system_agent.uninstalling"
    stale.close()
    assert _job_count() == 2  # one deploy job, one uninstall job — nothing else


# ---- (3) release pin at job entry ---------------------------------------------------


def _legacy_uri_spec(skills):
    spec = presets.build_spec(ARCHITECT, BUCKET, InstallOptions(), digest=DIGEST12 * 6).model_dump()
    spec["skills"] = skills
    return spec


@pytest.mark.parametrize("pin,skills,package_state", [
    ("missing", None, "succeeded"),
    ("malformed", None, "skipped"),
    ("stale", None, "succeeded"),
    ("valid", [f"s3://{BUCKET}/system-skills/{ARCHITECT.name}/1.0.0/"], "succeeded"),  # legacy dir
    ("valid", [f"s3://other-bucket/system-skills/{ARCHITECT.name}/1.0.0-{DIGEST12}/"], "skipped"),
    ("valid", [f"s3://{BUCKET}/system-skills/other-preset/1.0.0-{DIGEST12}/"], "succeeded"),
    ("valid", [f"s3://{BUCKET}/system-skills/{ARCHITECT.name}/1.0.0-{DIGEST12}/",
               f"s3://{BUCKET}/skills/member-skill/"], "succeeded"),  # extra skill
    ("valid", [f"s3://{BUCKET}/agent-skills/x/{ARCHITECT.name}/1.0.0-{DIGEST12}/"], "skipped"),
])
def test_resumed_job_fails_closed_at_entry_on_bad_pin_or_wrong_release_uri(
    monkeypatch, pin, skills, package_state
):
    """The pipeline skips succeeded/skipped stages; the guard runs at job entry and
    requires the COMPLETE expected URI (workspace bucket, preset path, version and
    snapshot digest) — a legacy plain-version directory, another bucket, another
    preset's path, a foreign prefix or an extra skill all fail before any stage."""
    fake = _S3()
    spec = _legacy_uri_spec(skills) if skills else None
    ctx, agent = _pinned_preset(monkeypatch, fake, pin=pin, spec=spec)
    db = SessionLocal()
    dep = db.get(pipeline_module.Deployment, ctx.deployment_id)
    dep.stages = [{**st, "status": package_state if st["name"] in ("generate", "package")
                   else "pending"} for st in dep.stages]
    db.commit()
    db.close()
    control = _Control()
    monkeypatch.setattr(harness_module, "control_client", lambda _ws: control)
    for name in ("provision", "register"):
        monkeypatch.setitem(STAGES, name, lambda c, a: pytest.fail("a stage ran on a bad pin"))
    pipeline_module.execute_deploy_job(ctx.job_id)  # the real deploy stage is wired
    job = _job(ctx.job_id)
    assert job.status == "failed", job.error
    assert any(k in job.error for k in ("release pin", "mismatch", "pinned release is exactly"))
    assert control.creates == [] and control.updates == []  # no AWS write
    assert _agent(agent.id).status == "failed" and fake.puts == []


def test_entry_guard_accepts_exactly_the_expected_uri_and_names_it(monkeypatch):
    fake = _S3()
    ctx, agent = _pinned_preset(monkeypatch, fake)
    verdict = service.assert_job_release_pinned(_job(ctx.job_id).payload, agent, ctx.workspace)
    assert verdict["expected_uri"] == f"s3://{BUCKET}/system-skills/{ARCHITECT.name}/1.0.0-{DIGEST12}/"
    with pytest.raises(RuntimeError, match="artifacts_bucket missing"):
        service.assert_job_release_pinned(_job(ctx.job_id).payload, agent,
                                          ws_ctx({"execution_role_arn": "x"}))


# ---- (4) release-prefix integrity ----------------------------------------------------


def test_valid_superset_snapshot_cannot_add_bytes_to_the_winners_release_directory(
    monkeypatch, tmp_path
):
    """Two VALID snapshots of v1.0.0 (B = A + one reference file) publish to two
    content-addressed directories; B can never place a file the Harness would load
    under A's directory, and A's repair verifies the exact directory."""
    fake = _S3()
    ctx_a, agent_a = _pinned_preset(monkeypatch, fake)
    STAGES["package"](ctx_a, agent_a)
    snap_a = _snapshot()
    # build B from a copy of the bundle with an extra reference
    copy = tmp_path / "bundle"
    for rel, body in snap_a.files.items():
        (copy / rel).parent.mkdir(parents=True, exist_ok=True)
        (copy / rel).write_bytes(body)
    (copy / "references" / "extra.md").write_text("# extra\n", encoding="utf-8")
    preset_b = presets.SystemPreset(**{**ARCHITECT.__dict__, "skill_dir": str(copy)})
    monkeypatch.setattr(
        presets.SystemPreset, "skill_path",
        lambda self: pathlib.Path(self.skill_dir) if self.skill_dir.startswith("/")
        else presets.SKILLS_ROOT / self.name,
    )
    snap_b = presets.snapshot_bundle(preset_b)
    assert snap_b.digest != snap_a.digest and "references/extra.md" in snap_b.files
    prefix_a = ARCHITECT.skill_prefix(snap_a.digest)
    prefix_b = ARCHITECT.skill_prefix(snap_b.digest)
    assert prefix_a != prefix_b
    # B installs (repair) against its own snapshot — it lands in its own directory
    monkeypatch.setitem(presets.PRESETS, KEY, preset_b)
    spec_b = presets.build_spec(
        preset_b, BUCKET, InstallOptions(), digest=snap_b.digest
    ).model_dump()
    _set_status(agent_a.id, "deleted")
    ctx_b, agent_b = _pinned_preset(monkeypatch, fake, spec=spec_b)
    db = SessionLocal()
    job = db.get(Job, ctx_b.job_id)
    job.payload = {**job.payload, "preset_bundle": snap_b.release()}
    db.commit()
    db.close()
    STAGES["package"](ctx_b, agent_b)
    a_keys = {k for k in fake.objects if k.startswith(prefix_a)}
    expected_a = {f"{prefix_a}{rel}" for rel in snap_a.files}
    expected_a.add(f"{prefix_a}{service.MANIFEST_KEY}")
    assert a_keys == expected_a
    assert f"{prefix_b}references/extra.md" in fake.objects
    # A's directory later gains a foreign object: repair refuses (and does not delete it)
    monkeypatch.setitem(presets.PRESETS, KEY, ARCHITECT)
    fake.objects[f"{prefix_a}references/extra.md"] = b"# planted\n"
    with pytest.raises(RuntimeError, match="outside the pinned snapshot"):
        STAGES["package"](ctx_a, agent_a)
    assert fake.objects[f"{prefix_a}references/extra.md"] == b"# planted\n"


def test_final_verification_covers_the_manifest_and_wrong_type_json(monkeypatch):
    fake = _S3()
    ctx, agent = _pinned_preset(monkeypatch, fake)
    fake.corrupt_on_store.add(MANIFEST)  # the manifest bytes S3 stores differ
    with pytest.raises(RuntimeError, match="manifest is missing or does not describe"):
        STAGES["package"](ctx, agent)
    for raw in (b"[1]", b'"bad"', b"1", b"null"):
        fake2 = _S3({MANIFEST: raw})
        ctx2, agent2 = _pinned_preset(monkeypatch, fake2)
        with pytest.raises(RuntimeError, match="published release is immutable"):
            STAGES["package"](ctx2, agent2)
        assert fake2.puts == []


# ---- (5) KB-mode least privilege ---------------------------------------------------


def test_preset_kb_mode_grants_exactly_the_devguide_oauth_statements():
    spec = presets.build_spec(ARCHITECT, BUCKET, InstallOptions(
        knowledge_bases=(presets.KnowledgeBaseRef(**KB_REF),)))
    ctx = agent_iam.role_context(ws_ctx(READY_RESOURCES))
    doc = agent_iam.policy_document(spec, ctx, system_preset=True)
    by_sid = {s["Sid"]: s for s in doc["Statement"]}
    assert set(by_sid) == {
        "BedrockModels", "AgentCoreOAuth2TokenVaultDefault", "AgentCoreOAuth2TokenVaultPerProvider",
        "AgentCoreOAuth2Secret", "SkillBundleObjects", "SkillBundleList", "Telemetry",
        "TelemetryTracing",
    }
    base = "arn:aws:bedrock-agentcore:us-west-2:111122223333"
    token_action = "bedrock-agentcore:GetResourceOauth2Token"
    assert by_sid["AgentCoreOAuth2TokenVaultDefault"]["Action"] == token_action
    assert by_sid["AgentCoreOAuth2TokenVaultPerProvider"]["Action"] == token_action
    assert by_sid["AgentCoreOAuth2TokenVaultDefault"]["Resource"] == [
        f"{base}:token-vault/default",
        f"{base}:workload-identity-directory/default",
        f"{base}:workload-identity-directory/default/workload-identity/"
        "harness_aws_agent_solution_architect-*",
    ]
    assert by_sid["AgentCoreOAuth2TokenVaultPerProvider"]["Resource"] == OAUTH_PROVIDER
    assert by_sid["AgentCoreOAuth2Secret"]["Resource"] == (
        "arn:aws:secretsmanager:us-west-2:111122223333:secret:"
        "bedrock-agentcore-identity!default/oauth2/launchpad-gw-m2m-*"
    )
    actions = {a for s in doc["Statement"]
               for a in ([s["Action"]] if isinstance(s["Action"], str) else s["Action"])}
    for forbidden in ("bedrock-agentcore:GetResourceApiKey",
                      "bedrock-agentcore:GetWorkloadAccessToken",
                      "bedrock-agentcore:GetWorkloadAccessTokenForJWT",
                      "bedrock-agentcore:GetWorkloadAccessTokenForUserId",
                      "bedrock:Retrieve", "bedrock:AgenticRetrieveStream",
                      "bedrock:GetKnowledgeBase"):
        assert forbidden not in actions, forbidden
    resources = " ".join(str(s["Resource"]) for s in doc["Statement"])
    assert "bedrock-agentcore-identity!*" not in resources  # no family-wide secret
    assert "apikey" not in resources
    # ordinary agents with a KB keep their historical policy shape
    plain = AgentSpec(name="plain", method="harness", system_prompt="x", knowledge_bases=[KB_REF])
    plain_sids = {s["Sid"] for s in agent_iam.policy_document(plain, ctx)["Statement"]}
    assert {"AgentCoreWorkloadIdentity", "IdentityVaultSecrets", "ManagedKbRetrieval",
            "ManagedKbAgenticRetrieval"} <= plain_sids


def test_preset_kb_mode_requires_the_workspace_oauth_provider(client):
    ctx = agent_iam.role_context(ws_ctx({k: v for k, v in READY_RESOURCES.items()
                                         if k != "oauth_provider_arn"}))
    spec = presets.build_spec(ARCHITECT, BUCKET, InstallOptions(
        knowledge_bases=(presets.KnowledgeBaseRef(**KB_REF),)))
    with pytest.raises(ValueError, match="oauth_provider_arn"):
        agent_iam.policy_document(spec, ctx, system_preset=True)
    _mark_ready(resources={k: v for k, v in READY_RESOURCES.items() if k != "oauth_provider_arn"})
    res = client.post(INSTALL, json={"knowledge_bases": [KB_REF]})
    assert res.status_code == 409 and res.json()["code"] == "system_agent.workspace_not_ready"
    assert res.json()["detail"]["requirements"][0]["code"] == "missing_oauth_provider"
    assert _rows() == []
    # a docs-only install (no KB) does not need the provider at all
    assert client.post(INSTALL, json={}).status_code == 202


def test_ensure_role_applies_the_preset_policy_for_system_rows():
    captured: dict = {}

    class _Iam:
        def create_role(self, **kw):
            return {"Role": {"Arn": f"arn:aws:iam::111122223333:role/{kw['RoleName']}"}}

        def put_role_policy(self, **kw):
            captured[kw["PolicyName"]] = json.loads(kw["PolicyDocument"])

        def delete_role_policy(self, **kw):
            pass

    spec = presets.build_spec(ARCHITECT, BUCKET, InstallOptions(
        knowledge_bases=(presets.KnowledgeBaseRef(**KB_REF),)))
    agent = Agent(id="c" * 32, name=ARCHITECT.name, method="harness", system_key=KEY,
                  spec=spec.model_dump())
    agent_iam.ensure_role(_Iam(), agent, spec, agent_iam.role_context(ws_ctx(READY_RESOURCES)))
    sids = {s["Sid"] for s in captured[f"launchpad-caps-{ARCHITECT.name}"]["Statement"]}
    assert "AgentCoreOAuth2TokenVaultPerProvider" in sids and "ManagedKbRetrieval" not in sids


# ---- pass 5: strict KB cleanup, exclusive resume, historical role ownership ----------


def _kb_preset(client, monkeypatch, control) -> str:
    agent_id = _active_preset(client)
    _mark_ready(resources={**READY_RESOURCES, "kb_gateway_id": "gw-1"})
    _strict_stubs(monkeypatch, control, agent_id=agent_id)
    return agent_id


def test_kb_target_on_page_two_is_found_pinned_deleted_and_read_back_gone(client, monkeypatch):
    name = uninstall_module.kbgw.agentic_target_name(ARCHITECT.name)
    control = _HarnessControl(targets=[{"name": "a", "targetId": "t-a"},
                                       {"name": "b", "targetId": "t-b"},
                                       {"name": name, "targetId": "t-mine"}],
                              target_statuses=["DELETING", "DELETING", None])
    agent_id = _kb_preset(client, monkeypatch, control)
    # the DESIRED spec says no KB (a failed detach-repair rewrote it) — the historical
    # target must still be found and removed
    db = SessionLocal()
    row = db.get(Agent, agent_id)
    row.spec = {**row.spec, "knowledge_bases": []}
    db.commit()
    db.close()
    job_id = client.delete(f"/api/system-agents/{KEY}").json()["job_id"]
    uninstall_module.execute_uninstall_job(job_id)
    calls = control.target_calls
    assert calls[:3] == [("list", None), ("list", "1"), ("list", "2")]  # every page
    assert ("delete", "t-mine") in calls and calls.count(("get", "t-mine")) == 3
    progress = _job(job_id).payload["progress"]["kb_target"]
    assert progress["state"] == "done" and progress["target_id"] == "t-mine"
    assert "ResourceNotFound" in progress["detail"]
    assert _agent(agent_id).status == "deleted"


def test_kb_target_delete_notfound_is_done_and_pending_forever_is_a_timeout(client, monkeypatch):
    name = uninstall_module.kbgw.agentic_target_name(ARCHITECT.name)
    control = _HarnessControl(targets=[{"name": name, "targetId": "t1"}], target_delete="notfound")
    agent_id = _kb_preset(client, monkeypatch, control)
    job_id = client.delete(f"/api/system-agents/{KEY}").json()["job_id"]
    uninstall_module.execute_uninstall_job(job_id)
    detail = _job(job_id).payload["progress"]["kb_target"]["detail"]
    assert detail == "agentic target t1 already gone"
    assert _agent(agent_id).status == "deleted"
    # a target that never leaves DELETING is a bounded, retryable failure
    agent_id2 = client.post(INSTALL, json={}).json()["agent"]["id"]
    _set_status(agent_id2, "active", "arn:aws:bedrock-agentcore:us-west-2:1:harness/h2")
    control2 = _HarnessControl(targets=[{"name": name, "targetId": "t2"}],
                               target_statuses=["DELETING"])
    _strict_stubs(monkeypatch, control2, agent_id=agent_id2)
    monkeypatch.setattr(uninstall_module, "TARGET_GONE_TIMEOUT_S", 0)
    job2 = client.delete(f"/api/system-agents/{KEY}").json()["job_id"]
    uninstall_module.execute_uninstall_job(job2)
    assert _job(job2).status == "failed" and "TimeoutError" in _job(job2).error
    assert _job(job2).payload["progress"]["kb_target"]["target_id"] == "t2"
    assert _agent(agent_id2).status == "uninstalling" and control2.calls == []


def test_kb_retry_after_access_denied_uses_the_pinned_id_not_a_fresh_lookup(client, monkeypatch):
    name = uninstall_module.kbgw.agentic_target_name(ARCHITECT.name)
    control = _HarnessControl(targets=[{"name": name, "targetId": "t-old"}], target_delete="denied")
    agent_id = _kb_preset(client, monkeypatch, control)
    first = client.delete(f"/api/system-agents/{KEY}").json()["job_id"]
    uninstall_module.execute_uninstall_job(first)
    assert _job(first).status == "failed" and _agent(agent_id).status == "uninstalling"
    # a fresh name lookup would now resolve a DIFFERENT target (a replacement carrying
    # the same name); the ORDINARY retry request must carry the pinned identity itself
    control2 = _HarnessControl(targets=[{"name": name, "targetId": "t-other"}], missing=True)
    _strict_stubs(monkeypatch, control2, agent_id=agent_id)
    retry = client.delete(f"/api/system-agents/{KEY}").json()
    assert retry["started"] is True and retry["attempt"] == 2
    carried = _job(retry["job_id"]).payload["progress"]["kb_target"]
    assert carried["target_id"] == "t-old" and carried["gateway_id"] == "gw-1"
    assert carried["state"] == "pinned"  # identity carried, completion NOT
    uninstall_module.execute_uninstall_job(retry["job_id"])
    assert ("delete", "t-old") in control2.target_calls
    assert ("delete", "t-other") not in control2.target_calls
    assert all(c[0] != "list" for c in control2.target_calls)
    assert _agent(agent_id).status == "deleted"


def test_kb_target_with_gateway_missing_from_resources_is_retryable(client, monkeypatch):
    agent_id = _active_preset(client)
    db = SessionLocal()
    row = db.get(Agent, agent_id)
    row.spec = {**row.spec, "knowledge_bases": [KB_REF]}
    db.commit()
    db.close()
    control, _ = _strict_stubs(monkeypatch, agent_id=agent_id)  # resources lack kb_gateway_id
    job_id = client.delete(f"/api/system-agents/{KEY}").json()["job_id"]
    uninstall_module.execute_uninstall_job(job_id)
    assert _job(job_id).status == "failed" and "kb_gateway_id" in _job(job_id).error
    assert control.calls == [] and _agent(agent_id).status == "uninstalling"


def test_two_resume_workers_only_one_runs_and_a_live_holder_blocks_adoption(client, monkeypatch):
    agent_id = _active_preset(client)
    control, role_calls = _strict_stubs(monkeypatch, agent_id=agent_id)
    job_id = client.delete(f"/api/system-agents/{KEY}").json()["job_id"]
    db = SessionLocal()
    db.get(Job, job_id).status = "running"  # a crashed predecessor left it running
    db.commit()
    db.close()
    barrier = threading.Barrier(2, timeout=10)
    real_delete = control.delete_harness

    def slow_delete(harnessId):
        try:
            barrier.wait()  # hold the winner inside its first cloud step
        except threading.BrokenBarrierError:
            pass
        return real_delete(harnessId)

    control.delete_harness = slow_delete
    outcomes: list[str] = []

    def resume_worker():
        uninstall_module.execute_uninstall_job(job_id, resume=True)
        outcomes.append("ran")

    def late_worker():
        try:
            barrier.wait()  # start only once the winner is mid-step
        except threading.BrokenBarrierError:
            pass
        uninstall_module.execute_uninstall_job(job_id, resume=True)  # must be refused by the lock

    threads = [threading.Thread(target=resume_worker), threading.Thread(target=late_worker)]
    for th in threads:
        th.start()
    for th in threads:
        th.join(timeout=20)
    assert control.calls.count(("delete", "h1")) == 1 and len(role_calls) == 1
    assert _agent(agent_id).status == "deleted" and _job(job_id).status == "succeeded"
    # an externally held lock (a live worker in another process) blocks adoption …
    agent_id2 = client.post(INSTALL, json={}).json()["agent"]["id"]
    _set_status(agent_id2, "active", "arn:aws:bedrock-agentcore:us-west-2:1:harness/h2")
    control2, role_calls2 = _strict_stubs(monkeypatch, agent_id=agent_id2)
    job2 = client.delete(f"/api/system-agents/{KEY}").json()["job_id"]
    holder = uninstall_module._acquire_lock(agent_id2)
    assert holder is not None
    uninstall_module.execute_uninstall_job(job2, resume=True)
    assert control2.calls == [] and _job(job2).status == "queued"
    # … and its death (lock released) lets the resume proceed
    uninstall_module._release_lock(holder)
    uninstall_module.execute_uninstall_job(job2, resume=True)
    assert _agent(agent_id2).status == "deleted"


def test_role_cleanup_uses_installed_ownership_not_the_current_toggle(client, monkeypatch):
    agent_id = _active_preset(client)
    control, deleted = _strict_stubs(monkeypatch, agent_id=agent_id)
    # the operator flips per-agent roles OFF after the install
    import app.core.config as config_module
    monkeypatch.setattr(config_module, "get_settings",
                        lambda: _settings(per_agent_execution_roles=False))
    monkeypatch.setattr(uninstall_module, "get_settings",
                        lambda: _settings(per_agent_execution_roles=False), raising=False)
    job_id = client.delete(f"/api/system-agents/{KEY}").json()["job_id"]
    uninstall_module.execute_uninstall_job(job_id)
    assert deleted == [agent_iam.role_name_for(ARCHITECT.name, agent_id)]
    assert _agent(agent_id).status == "deleted"


def test_role_that_is_not_this_installations_is_never_deleted(client, monkeypatch):
    agent_id = _active_preset(client)
    name = agent_iam.role_name_for(ARCHITECT.name, agent_id)
    foreign = _Iam({name: {"Arn": f"arn:aws:iam::111122223333:role/{name}",
                           "Tags": [{"Key": agent_iam.MANAGED_TAG_KEY, "Value": "someone-else"}]}})
    control, _ = _strict_stubs(monkeypatch, iam=foreign)
    job_id = client.delete(f"/api/system-agents/{KEY}").json()["job_id"]
    uninstall_module.execute_uninstall_job(job_id)
    assert foreign.deleted == [] and name in foreign.roles
    assert _job(job_id).status == "failed" and "not this installation's role" in _job(job_id).error
    assert _agent(agent_id).status == "uninstalling"
    # the shared workspace role is refused even if it were tagged
    shared_name = agent_iam.role_name_for(ARCHITECT.name, agent_id)
    shared = _Iam({shared_name: {"Arn": READY_RESOURCES["execution_role_arn"],
                                 "Tags": [{"Key": agent_iam.MANAGED_TAG_KEY, "Value": agent_id}]}})
    _strict_stubs(monkeypatch, iam=shared)
    retry = client.delete(f"/api/system-agents/{KEY}").json()["job_id"]
    uninstall_module.execute_uninstall_job(retry)
    assert shared.deleted == [] and "shared workspace role" in _job(retry).error


def test_carried_progress_is_only_trusted_for_the_same_resource_id(client, monkeypatch):
    agent_id = _active_preset(client)
    control, _ = _strict_stubs(monkeypatch, role_ok=False, agent_id=agent_id)
    first = client.delete(f"/api/system-agents/{KEY}").json()["job_id"]
    uninstall_module.execute_uninstall_job(first)
    assert _job(first).payload["progress"]["harness"]["resource_id"] == "h1"
    _set_status(agent_id, "uninstalling", "arn:aws:bedrock-agentcore:us-west-2:1:harness/h9")
    control2, _ = _strict_stubs(monkeypatch, agent_id=agent_id)
    retry = client.delete(f"/api/system-agents/{KEY}").json()["job_id"]
    assert _job(retry).payload["progress"]["harness"]["resource_id"] == "h1"  # carried …
    uninstall_module.execute_uninstall_job(retry)
    assert ("delete", "h9") in control2.calls  # … but re-run because the id differs
    assert _agent(agent_id).status == "deleted"


# ---- pass 7: queued retry behind a retiring predecessor's lock ------------------------


def test_queued_retry_waits_for_the_retiring_predecessor_and_then_runs_once(client, monkeypatch):
    """The failed attempt commits its failure BEFORE it releases the lock; an operator
    can request the retry inside that window. The retry's worker must wait (bounded)
    for the release and then run exactly once — no restart, no manual state."""
    agent_id = _active_preset(client)
    control, deleted = _strict_stubs(monkeypatch, role_ok=False, agent_id=agent_id)
    monkeypatch.setattr(uninstall_module, "_lock_sleep", lambda s: time.sleep(0.01))
    monkeypatch.setattr(uninstall_module, "LOCK_WAIT_S", 10)
    failed_committed = threading.Event()
    release_predecessor = threading.Event()
    real_release = uninstall_module._release_lock

    def retiring_release(fd):
        # the predecessor has already committed its failure; hold the lock until told
        if fd is not None and not release_predecessor.is_set():
            failed_committed.set()
            release_predecessor.wait(timeout=10)
        real_release(fd)

    monkeypatch.setattr(uninstall_module, "_release_lock", retiring_release)
    first = client.delete(f"/api/system-agents/{KEY}").json()["job_id"]
    predecessor = threading.Thread(target=uninstall_module.execute_uninstall_job, args=(first,))
    predecessor.start()
    assert failed_committed.wait(timeout=10)
    assert _job(first).status == "failed"  # visible failure, lock still held

    # the operator retries inside the window: a distinct queued attempt
    launched: list[str] = []
    monkeypatch.setattr(system_router, "start_uninstall_async", launched.append)
    retry = client.delete(f"/api/system-agents/{KEY}").json()
    assert retry["started"] is True and retry["attempt"] == 2 and launched == [retry["job_id"]]
    good_iam = _Iam(_owned_role(agent_id))
    monkeypatch.setattr(aws_clients, "client",
                        lambda name, ws, **kw: good_iam if name == "iam" else pytest.fail(name))
    worker = threading.Thread(target=uninstall_module.execute_uninstall_job,
                              args=(retry["job_id"],))
    worker.start()
    time.sleep(0.1)
    assert _job(retry["job_id"]).status == "queued"  # waiting behind the retiring lock
    release_predecessor.set()
    predecessor.join(timeout=10)
    worker.join(timeout=15)
    assert _job(retry["job_id"]).status == "succeeded" and _agent(agent_id).status == "deleted"
    assert good_iam.deleted == [agent_iam.role_name_for(ARCHITECT.name, agent_id)]
    assert control.calls.count(("delete", "h1")) == 1  # harness step carried, not repeated
    db = SessionLocal()
    assert db.query(Job).filter(Job.type == service.UNINSTALL_JOB_TYPE).count() == 2
    db.close()


def test_queued_retry_that_times_out_stays_queued_and_the_next_request_relaunches_it(
    client, monkeypatch
):
    agent_id = _active_preset(client)
    control, _ = _strict_stubs(monkeypatch, agent_id=agent_id)
    monkeypatch.setattr(uninstall_module, "LOCK_WAIT_S", 0)
    monkeypatch.setattr(uninstall_module, "_lock_sleep", lambda s: None)
    launched: list[str] = []
    monkeypatch.setattr(system_router, "start_uninstall_async", launched.append)
    holder = uninstall_module._acquire_lock(agent_id)  # a live predecessor in another process
    job_id = client.delete(f"/api/system-agents/{KEY}").json()["job_id"]
    uninstall_module.execute_uninstall_job(job_id)  # bounded wait expires → gives up
    assert _job(job_id).status == "queued" and control.calls == []
    again = client.delete(f"/api/system-agents/{KEY}").json()
    assert again["job_id"] == job_id and again["started"] is False
    assert launched == [job_id, job_id]  # the repeated request relaunches the queued job
    uninstall_module._release_lock(holder)
    uninstall_module.execute_uninstall_job(job_id)
    assert _job(job_id).status == "succeeded" and _agent(agent_id).status == "deleted"
    # a RUNNING job is never relaunched by a repeated request
    agent_id2 = client.post(INSTALL, json={}).json()["agent"]["id"]
    _set_status(agent_id2, "active", "arn:aws:bedrock-agentcore:us-west-2:1:harness/h2")
    launched.clear()
    job2 = client.delete(f"/api/system-agents/{KEY}").json()["job_id"]
    db = SessionLocal()
    db.get(Job, job2).status = "running"
    db.commit()
    db.close()
    assert client.delete(f"/api/system-agents/{KEY}").json()["started"] is False
    assert launched == [job2]


# ---- pass 8: one live waiter per job -------------------------------------------------


def _hold_predecessor_lock(monkeypatch):
    """A real failed predecessor that holds its flock after committing the failure until
    told to release — the window in which an operator can already retry."""
    failed_committed = threading.Event()
    release = threading.Event()
    real_release = uninstall_module._release_lock

    def retiring_release(fd):
        if fd is not None and not release.is_set():
            failed_committed.set()
            release.wait(timeout=15)
        real_release(fd)

    monkeypatch.setattr(uninstall_module, "_release_lock", retiring_release)
    return failed_committed, release


def _use_real_starter(monkeypatch):
    monkeypatch.setattr(system_router, "start_uninstall_async",
                        uninstall_module.start_uninstall_async)


def test_repeated_deletes_on_a_queued_retry_share_one_waiting_worker(client, monkeypatch):
    agent_id = _active_preset(client)
    control, _ = _strict_stubs(monkeypatch, role_ok=False, agent_id=agent_id)
    monkeypatch.setattr(uninstall_module, "_lock_sleep", lambda s: time.sleep(0.01))
    monkeypatch.setattr(uninstall_module, "LOCK_WAIT_S", 15)
    failed_committed, release = _hold_predecessor_lock(monkeypatch)
    first = client.delete(f"/api/system-agents/{KEY}").json()["job_id"]
    predecessor = threading.Thread(target=uninstall_module.execute_uninstall_job, args=(first,))
    predecessor.start()
    assert failed_committed.wait(timeout=10) and _job(first).status == "failed"

    _use_real_starter(monkeypatch)  # the REAL route + starter from here on
    good_iam = _Iam(_owned_role(agent_id))
    monkeypatch.setattr(aws_clients, "client",
                        lambda name, ws, **kw: good_iam if name == "iam" else pytest.fail(name))
    # Count the job's own worker threads (named ``uninstall-<job8>`` by _launch),
    # not the whole process: TestClient/AnyIO portal threads come and go around
    # every request and made a bare active_count() delta flaky (host verify-2).
    def uninstall_workers() -> list[str]:
        return sorted(t.name for t in threading.enumerate()
                      if t.name.startswith("uninstall-") and t.is_alive())

    before = uninstall_workers()
    responses = [client.delete(f"/api/system-agents/{KEY}").json() for _ in range(12)]
    retry_job = responses[0]["job_id"]
    assert responses[0]["started"] is True and retry_job != first
    assert all(r["job_id"] == retry_job and r["started"] is False for r in responses[1:])
    waiter = uninstall_module.live_worker(retry_job)
    assert waiter is not None and waiter.is_alive()
    assert len(uninstall_module._WORKERS) == 1  # exactly one tracked worker for the job
    after = uninstall_workers()
    # one waiting thread for the retry job, not twelve — by identity, with evidence
    assert after.count(f"uninstall-{retry_job[:8]}") == 1, after
    assert len(after) - len(before) <= 1, (before, after)
    assert _job(retry_job).status == "queued"  # still waiting behind the retiring lock
    db = SessionLocal()
    assert db.query(Job).filter(Job.type == service.UNINSTALL_JOB_TYPE).count() == 2
    db.close()

    release.set()
    predecessor.join(timeout=10)
    waiter.join(timeout=20)
    assert _job(retry_job).status == "succeeded" and _agent(agent_id).status == "deleted"
    assert good_iam.deleted == [agent_iam.role_name_for(ARCHITECT.name, agent_id)]
    assert control.calls.count(("delete", "h1")) == 1  # no duplicate cloud effect
    assert uninstall_module.live_worker(retry_job) is None
    assert retry_job not in uninstall_module._WORKERS


def test_expired_waiter_is_cleared_and_the_next_request_wakes_one_fresh_worker(client, monkeypatch):
    agent_id = _active_preset(client)
    control, _ = _strict_stubs(monkeypatch, agent_id=agent_id)
    _use_real_starter(monkeypatch)
    # a controllable clock: the waiter parks in _lock_sleep until the test advances
    # time past the bound, so "expiry" is deterministic rather than a real timeout
    from types import SimpleNamespace

    clock = {"now": 1000.0}
    parked = threading.Event()
    advance = threading.Event()

    def lock_sleep(_s):
        parked.set()
        advance.wait(timeout=10)

    monkeypatch.setattr(uninstall_module, "time",
                        SimpleNamespace(monotonic=lambda: clock["now"], sleep=time.sleep))
    monkeypatch.setattr(uninstall_module, "LOCK_WAIT_S", 15)
    monkeypatch.setattr(uninstall_module, "_lock_sleep", lock_sleep)
    holder = uninstall_module._acquire_lock(agent_id)  # a live holder elsewhere
    job_id = client.delete(f"/api/system-agents/{KEY}").json()["job_id"]
    assert parked.wait(timeout=10)
    first_worker = uninstall_module.live_worker(job_id)
    assert first_worker is not None
    clock["now"] += 100  # the bounded wait expires …
    advance.set()
    first_worker.join(timeout=10)  # … and the worker gives up
    assert _job(job_id).status == "queued" and control.calls == []
    assert job_id not in uninstall_module._WORKERS  # registry cleared on exit
    parked.clear()
    advance.clear()

    again = [client.delete(f"/api/system-agents/{KEY}").json() for _ in range(5)]
    assert all(r["job_id"] == job_id and r["started"] is False for r in again)
    assert parked.wait(timeout=10)
    second_worker = uninstall_module.live_worker(job_id)
    assert second_worker is not None and second_worker is not first_worker
    assert len(uninstall_module._WORKERS) == 1  # the five requests coalesced again
    uninstall_module._release_lock(holder)
    advance.set()  # the waiter re-checks the lock and now acquires it
    second_worker.join(timeout=20)
    assert _job(job_id).status == "succeeded" and _agent(agent_id).status == "deleted"
    assert control.calls.count(("delete", "h1")) == 1
    assert job_id not in uninstall_module._WORKERS


def test_start_failure_leaves_no_registry_entry_and_terminal_jobs_stay_inert(client, monkeypatch):
    agent_id = _active_preset(client)
    control, _ = _strict_stubs(monkeypatch, agent_id=agent_id)
    job_id = client.delete(f"/api/system-agents/{KEY}").json()["job_id"]
    real_start = threading.Thread.start
    calls = {"n": 0}

    def failing_start(self):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("can't start new thread")
        return real_start(self)

    monkeypatch.setattr(threading.Thread, "start", failing_start)
    with pytest.raises(RuntimeError, match="start new thread"):
        uninstall_module.start_uninstall_async(job_id)
    assert job_id not in uninstall_module._WORKERS  # no dead placeholder
    worker = uninstall_module.start_uninstall_async(job_id)  # the next attempt launches
    worker.join(timeout=20)
    assert _job(job_id).status == "succeeded" and job_id not in uninstall_module._WORKERS
    # terminal job: both entrypoints run inert and leave the registry clean
    for starter in (uninstall_module.start_uninstall_async,
                    uninstall_module.start_uninstall_resume):
        t = starter(job_id)
        t.join(timeout=10)
    assert control.calls.count(("delete", "h1")) == 1 and job_id not in uninstall_module._WORKERS


def test_concurrent_callers_coalesce_on_one_worker(client, monkeypatch):
    agent_id = _active_preset(client)
    control, _ = _strict_stubs(monkeypatch, agent_id=agent_id)
    monkeypatch.setattr(uninstall_module, "LOCK_WAIT_S", 15)
    monkeypatch.setattr(uninstall_module, "_lock_sleep", lambda s: time.sleep(0.01))
    holder = uninstall_module._acquire_lock(agent_id)
    job_id = client.delete(f"/api/system-agents/{KEY}").json()["job_id"]
    barrier = threading.Barrier(8, timeout=10)
    threads_seen: list[threading.Thread] = []
    seen_lock = threading.Lock()

    def caller():
        barrier.wait()
        t = uninstall_module.start_uninstall_async(job_id)
        with seen_lock:
            threads_seen.append(t)

    callers = [threading.Thread(target=caller) for _ in range(8)]
    for c in callers:
        c.start()
    for c in callers:
        c.join(timeout=10)
    assert len({id(t) for t in threads_seen}) == 1  # every caller got the same worker
    assert len(uninstall_module._WORKERS) == 1
    uninstall_module._release_lock(holder)
    threads_seen[0].join(timeout=20)
    assert _job(job_id).status == "succeeded" and control.calls.count(("delete", "h1")) == 1


# ---------------------------------------------------------------------------
# SE-040: inference defaults + administrator-editable settings
# ---------------------------------------------------------------------------

SOL = "us.openai.gpt-5.6-sol"
OPUS = "global.anthropic.claude-opus-5"


def _spec_of(agent_id: str) -> dict:
    return dict(_agent(agent_id).spec)


def _rewrite_spec(agent_id: str, **changes) -> None:
    """Simulate a row written by an earlier build (e.g. pre-SE-040 defaults)."""
    db = SessionLocal()
    try:
        agent = db.get(Agent, agent_id)
        spec = dict(agent.spec)
        for key, value in changes.items():
            if value is None:
                spec.pop(key, None)
            else:
                spec[key] = value
        agent.spec = spec
        db.commit()
    finally:
        db.close()


def test_architect_defaults_are_gpt_sol_64k_output_high_effort():
    spec = presets.build_spec(ARCHITECT, BUCKET, ARCHITECT.default_options())
    assert (spec.model_id, spec.model_source) == (SOL, "bedrock")
    assert spec.max_tokens == 65536 and spec.reasoning_effort == "high"
    # the loop limits are separate knobs and unchanged
    assert (spec.max_iterations, spec.timeout_seconds) == (30, 900)
    params = build_create_params(spec, "arn:aws:iam::111:role/x", None)
    model = params["model"]["bedrockModelConfig"]
    assert model == {
        "modelId": SOL,
        "apiFormat": "converse_stream",
        "maxTokens": 65536,  # per model call — not InvokeHarness.maxTokens
        "additionalParams": {"additionalModelRequestFields": {"reasoning": {"effort": "high"}}},
    }
    assert "maxTokens" not in params and params["maxIterations"] == 30
    # UpdateHarness carries the identical model block (the live preset is updated in place)
    update = hc.wrap_params_for_update(params)
    assert update["model"] == params["model"]
    # a bare InstallOptions() is the catalogue default too (model/prompt/limits)
    bare = presets.build_spec(ARCHITECT, BUCKET, InstallOptions())
    assert (bare.model_id, bare.system_prompt) == (SOL, ARCHITECT.system_prompt)
    # retention clarification: memory-disabled ≠ nothing retained
    assert "never tell a customer that nothing is retained" in spec.system_prompt
    assert "CloudWatch" in spec.system_prompt


def test_ordinary_specs_send_exactly_the_request_they_always_did():
    stored = {"name": "plain-harness", "method": "harness", "system_prompt": "hi",
              "model_id": DEFAULT_MODEL_ID}
    spec = AgentSpec(**stored)
    assert spec.max_tokens is None and spec.reasoning_effort is None
    model = build_create_params(spec, "arn:aws:iam::111:role/x", None)["model"]
    assert model == {"bedrockModelConfig": {"modelId": DEFAULT_MODEL_ID,
                                            "apiFormat": "converse_stream"}}


@pytest.mark.parametrize("over", [
    {"reasoning_effort": "high"},  # Claude on Converse: the knob would leak
    {"model_id": "openai.gpt-5.6-sol", "model_source": "mantle", "reasoning_effort": "high"},
    {"model_id": SOL, "method": "zip_runtime", "reasoning_effort": "high"},
    {"method": "zip_runtime", "max_tokens": 4096},
    {"max_tokens": 0},
    {"max_tokens": presets.AgentSpec.model_fields["max_tokens"].metadata[1].le + 1},
    {"model_id": SOL, "reasoning_effort": "extreme"},
    {"model_id": SOL, "reasoning_effort": "none"},
])
def test_inference_knobs_reject_unsupported_pairings(over):
    base = {"name": "knobs", "method": "harness", "system_prompt": "hi"}
    with pytest.raises(ValueError):
        AgentSpec(**{**base, **over})


def test_inference_knobs_accept_openai_on_native_bedrock():
    spec = AgentSpec(name="knobs", method="harness", system_prompt="hi", model_id=SOL,
                     max_tokens=65536, reasoning_effort="high")
    assert spec.reasoning_effort == "high"
    # max_tokens alone is fine for any harness model
    assert AgentSpec(name="knobs", method="harness", system_prompt="hi",
                     max_tokens=8192).max_tokens == 8192


def test_execution_role_authorizes_the_us_openai_profile_on_the_dedicated_role():
    spec = presets.build_spec(ARCHITECT, BUCKET, ARCHITECT.default_options())
    ctx = agent_iam.role_context(ws_ctx(READY_RESOURCES))
    doc = agent_iam.policy_document(spec, ctx)
    models = next(s for s in doc["Statement"] if s["Sid"] == "BedrockModels")
    assert f"arn:aws:bedrock:{ctx.region}:{ctx.account_id}:inference-profile/{SOL}" in (
        models["Resource"]
    )
    assert "arn:aws:bedrock:*::foundation-model/openai.gpt-5.6-sol" in models["Resource"]
    assert "arn:aws:bedrock:*::foundation-model/*" not in models["Resource"]
    # the preset still deploys on its own role, never the shared one
    settings = get_settings()
    with pytest.raises(RuntimeError):
        service.require_dedicated_role(
            Agent(name=ARCHITECT.name, method="harness", system_key=KEY),
            READY_RESOURCES["execution_role_arn"], ws_ctx(READY_RESOURCES), settings,
        )


def test_status_exposes_stored_settings_and_build_defaults(client):
    _mark_ready()
    before = _status(client)
    assert before["settings"] == {} and before["defaults"]["model_id"] == SOL
    assert before["defaults"]["max_tokens"] == 65536
    assert before["defaults"]["reasoning_effort"] == "high"
    assert before["editable_fields"] == list(presets.EDITABLE_FIELDS)
    assert before["can_configure"] is False  # nothing to configure yet
    agent_id = client.post(INSTALL, json={}).json()["agent"]["id"]
    _set_status(agent_id, "active", "arn:aws:bedrock-agentcore:us-west-2:1:harness/h1")
    status = _status(client)
    assert status["settings"] == {**status["defaults"], "knowledge_bases": []}
    assert status["settings"]["system_prompt"] == ARCHITECT.system_prompt
    assert status["can_configure"] is True
    assert status["model_id"] == SOL  # legacy top-level members still agree


def test_new_install_sends_64k_and_high_effort_and_a_resume_regenerates_them(client):
    _mark_ready()
    res = client.post(INSTALL, json={})
    assert res.status_code == 202
    spec = AgentSpec(**res.json()["agent"]["spec"])
    params = build_create_params(spec, "arn:aws:iam::111:role/x", None)
    model = params["model"]["bedrockModelConfig"]
    assert model["maxTokens"] == 65536 and model["modelId"] == SOL
    assert model["additionalParams"]["additionalModelRequestFields"]["reasoning"] == {
        "effort": "high"
    }
    # the ledger round-trip is lossless: a resumed job rebuilds the same request
    stored = AgentSpec(**_spec_of(res.json()["agent"]["id"]))
    assert build_create_params(stored, "arn:aws:iam::111:role/x", None)["model"] == (
        params["model"]
    )


def test_old_spec_is_untouched_until_an_explicit_edit_and_partial_edits_preserve(client):
    _mark_ready()
    agent_id = client.post(INSTALL, json={}).json()["agent"]["id"]
    # a row an earlier build wrote: Sonnet, no knobs, an older prompt
    old_prompt = "You are the previous build's architect prompt."
    _rewrite_spec(agent_id, model_id=DEFAULT_MODEL_ID, max_tokens=None,
                  reasoning_effort=None, system_prompt=old_prompt)
    _set_status(agent_id, "active", "arn:aws:bedrock-agentcore:us-west-2:1:harness/h1")
    legacy = _spec_of(agent_id)

    # a read changes nothing; a bodiless repair is a no-op on an unchanged release
    status = _status(client)
    assert status["settings"]["model_id"] == DEFAULT_MODEL_ID
    assert status["settings"]["max_tokens"] is None
    assert status["settings"]["system_prompt"] == old_prompt
    same = client.post(INSTALL, json={})
    assert same.status_code == 200 and same.json()["changed"] is False
    assert _spec_of(agent_id) == legacy

    # a forced repair re-publishes but keeps every stored choice (prompt included)
    forced = client.post(INSTALL, json={"force": True})
    assert forced.status_code == 202
    after_force = _spec_of(agent_id)
    assert after_force["system_prompt"] == old_prompt
    assert after_force["model_id"] == DEFAULT_MODEL_ID and after_force["max_tokens"] is None
    assert after_force["skills"] == legacy["skills"]  # content-addressed release kept

    # a partial edit changes exactly the named member
    _set_status(agent_id, "active")
    edited = client.post(INSTALL, json={"max_tokens": 65536})
    assert edited.status_code == 202 and edited.json()["changed"] is True
    partial = _spec_of(agent_id)
    assert partial["max_tokens"] == 65536
    assert partial["model_id"] == DEFAULT_MODEL_ID and partial["system_prompt"] == old_prompt
    assert partial["reasoning_effort"] is None
    assert edited.json()["preset"]["settings"]["max_tokens"] == 65536

    # an explicit reset returns the named members (only) to this build's defaults
    _set_status(agent_id, "active")
    reset = client.post(INSTALL, json={
        "reset": ["model_id", "model_source", "reasoning_effort", "system_prompt"],
        "max_iterations": 12,
    })
    assert reset.status_code == 202, reset.text
    restored = _spec_of(agent_id)
    assert restored["model_id"] == SOL and restored["reasoning_effort"] == "high"
    assert restored["system_prompt"] == ARCHITECT.system_prompt
    assert restored["max_tokens"] == 65536 and restored["max_iterations"] == 12
    # the protected part of the spec never moved
    for key in ("name", "method", "tools", "allowed_tools", "memory", "skills", "env"):
        assert restored[key] == legacy[key], key


def test_stored_prompt_override_survives_repair_kb_edit_and_bundle_pin(client):
    _mark_ready()
    agent_id = client.post(INSTALL, json={}).json()["agent"]["id"]
    _set_status(agent_id, "active", "arn:aws:bedrock-agentcore:us-west-2:1:harness/h1")
    custom = "Custom architect prompt written by the administrator."
    res = client.post(INSTALL, json={"system_prompt": custom, "timeout_seconds": 1200})
    assert res.status_code == 202
    assert _spec_of(agent_id)["system_prompt"] == custom
    for body in ({"force": True},
                 {"knowledge_bases": [{"kb_id": "KB123ABC", "name": "guide"}]},
                 {"max_iterations": 40},
                 {"knowledge_bases": []}):
        _set_status(agent_id, "active")
        res = client.post(INSTALL, json=body)
        assert res.status_code == 202, (body, res.text)
        spec = _spec_of(agent_id)
        assert spec["system_prompt"] == custom and spec["timeout_seconds"] == 1200, body
        assert spec["model_id"] == SOL and spec["max_tokens"] == 65536, body
        # every repair still pins the content-addressed release and passes the job-entry guard
        job = _job(res.json()["job_id"])
        agent = _agent(agent_id)
        service.assert_job_release_pinned(job.payload, agent, ws_ctx(READY_RESOURCES))
    assert _spec_of(agent_id)["knowledge_bases"] == []


@pytest.mark.parametrize("body", [
    {"max_tokens": 0},
    {"max_tokens": 131073},
    {"max_tokens": "lots"},
    {"reasoning_effort": "extreme"},
    {"reasoning_effort": "none"},
    {"model_source": "openai"},
    {"model_id": ""},
    {"max_iterations": 0},
    {"max_iterations": 101},
    {"timeout_seconds": 5},
    {"system_prompt": ""},
    {"knowledge_bases": [{"kb_id": "not valid!"}]},
    # protected members are not reachable from the body at all
    {"name": "other-name"},
    {"allowed_tools": ["*"]},
    {"memory": {"short_term": True, "long_term": True}},
    {"skills": ["s3://elsewhere/x/"]},
    {"tools": [{"type": "builtin", "name": "code-interpreter"}]},
    {"system_key": "aws-agent-solution-architect"},
    {"env": {"X": "1"}},
    {"reset": ["allowed_tools"]},
    {"reset": ["max_tokens", "max_tokens"]},
    {"reset": ["max_tokens"], "max_tokens": 5},
    {"clear": ["system_prompt"]},
    {"clear": ["max_tokens"], "max_tokens": 5},
    {"clear": ["max_tokens"], "reset": ["max_tokens"]},
    # a valid shape that is an unsupported pairing
    {"model_id": OPUS},
    {"model_id": "openai.gpt-5.6-sol", "model_source": "mantle"},
])
def test_invalid_edits_fail_before_any_job_or_spec_change(client, body):
    _mark_ready()
    agent_id = client.post(INSTALL, json={}).json()["agent"]["id"]
    _set_status(agent_id, "active", "arn:aws:bedrock-agentcore:us-west-2:1:harness/h1")
    before, jobs = _spec_of(agent_id), _job_count()
    res = client.post(INSTALL, json=body)
    assert res.status_code == 422, (body, res.text)
    assert res.json()["code"] in ("validation.invalid_request", "system_agent.invalid_options")
    assert _spec_of(agent_id) == before and _job_count() == jobs
    assert _status(client)["status"] == "active"


def test_switching_to_claude_needs_an_explicit_effort_reset(client):
    _mark_ready()
    agent_id = client.post(INSTALL, json={}).json()["agent"]["id"]
    _set_status(agent_id, "active", "arn:aws:bedrock-agentcore:us-west-2:1:harness/h1")
    # ``reset`` returns the knob to the catalogue default (high), which Claude refuses
    still = client.post(INSTALL, json={"model_id": OPUS, "reset": ["reasoning_effort"]})
    assert still.status_code == 422 and still.json()["code"] == "system_agent.invalid_options"
    res = client.post(INSTALL, json={"model_id": OPUS, "clear": ["reasoning_effort"]})
    assert res.status_code == 202, res.text
    spec = _spec_of(agent_id)
    assert spec["model_id"] == OPUS and spec["reasoning_effort"] is None
    assert spec["max_tokens"] == 65536  # unrelated members untouched
    # clearing max_tokens sends no ceiling at all (the provider default applies again)
    _set_status(agent_id, "active")
    cleared = client.post(INSTALL, json={"clear": ["max_tokens"]})
    assert cleared.status_code == 202 and _spec_of(agent_id)["max_tokens"] is None
    _set_status(agent_id, "active")
    spec = _spec_of(agent_id)
    params = build_create_params(AgentSpec(**spec), "arn:aws:iam::111:role/x", None)
    assert "additionalParams" not in params["model"]["bedrockModelConfig"]
    assert "maxTokens" not in params["model"]["bedrockModelConfig"]


def test_edit_while_deploying_is_refused_but_a_bodiless_repair_coalesces(client):
    _mark_ready()
    first = client.post(INSTALL, json={}).json()
    agent_id, job_id = first["agent"]["id"], first["job_id"]
    coalesced = client.post(INSTALL, json={})
    assert coalesced.status_code == 202 and coalesced.json()["job_id"] == job_id
    assert coalesced.json()["changed"] is False
    refused = client.post(INSTALL, json={"max_tokens": 1000})
    assert refused.status_code == 409, refused.text
    assert refused.json()["code"] == "system_agent.deploy_in_progress"
    assert refused.json()["detail"]["job_id"] == job_id
    assert _spec_of(agent_id)["max_tokens"] == 65536 and _job_count() == 1
    # a settled row accepts the same edit
    _set_status(agent_id, "active", "arn:aws:bedrock-agentcore:us-west-2:1:harness/h1")
    accepted = client.post(INSTALL, json={"max_tokens": 1000})
    assert accepted.status_code == 202 and _spec_of(agent_id)["max_tokens"] == 1000
    # uninstalling refuses the edit too (existing semantics)
    _set_status(agent_id, service.STATUS_UNINSTALLING)
    assert client.post(INSTALL, json={"max_tokens": 2000}).status_code == 409
    assert _spec_of(agent_id)["max_tokens"] == 1000


def test_concurrent_edits_never_lose_an_accepted_update_silently(client):
    """Two administrators save different settings against the same active row: one
    claims the row and its job carries its spec; the other is told 409 with that job
    id — never a 2xx whose job silently ignores its values."""
    _mark_ready()
    agent_id = client.post(INSTALL, json={}).json()["agent"]["id"]
    _set_status(agent_id, "active", "arn:aws:bedrock-agentcore:us-west-2:1:harness/h1")
    before = _job_count()
    s1, s2 = SessionLocal(), SessionLocal()
    try:
        a1, a2 = s1.get(Agent, agent_id), s2.get(Agent, agent_id)
        r1 = service._repair(s1, a1, ARCHITECT, BUCKET, PresetEdit({"max_tokens": 1111}),
                             force=False)
        s1.commit()
        with pytest.raises(service.AppError) as exc:
            service._repair(s2, a2, ARCHITECT, BUCKET, PresetEdit({"max_tokens": 2222}),
                            force=False)
        s2.rollback()
    finally:
        s1.close()
        s2.close()
    assert exc.value.code == "system_agent.deploy_in_progress"
    assert exc.value.detail["job_id"] == r1.job.id
    assert _job_count() - before == 1
    assert _spec_of(agent_id)["max_tokens"] == 1111  # the accepted update is the stored one
    # the loser retries once the winner's job has settled and is accepted
    _set_status(agent_id, "active")
    res = client.post(INSTALL, json={"max_tokens": 2222})
    assert res.status_code == 202 and _spec_of(agent_id)["max_tokens"] == 2222


def test_member_sees_settings_but_cannot_edit_them_even_with_deploy_permission(gated):
    admin, member = gated
    _mark_ready()
    agent_id = admin.post(INSTALL, json={}).json()["agent"]["id"]
    _set_status(agent_id, "active", "arn:aws:bedrock-agentcore:us-west-2:1:harness/h1")
    perms = member.get("/api/auth/status").json()["permissions"]
    assert "agents.deploy" in perms  # the member holds the ordinary deploy right
    seen = _status(member)
    assert seen["settings"]["model_id"] == SOL and seen["settings"]["max_tokens"] == 65536
    assert seen["can_configure"] is False and seen["can_repair"] is False
    for body in ({"max_tokens": 1000}, {"system_prompt": "mine"}, {"reset": ["model_id"]}):
        res = member.post(INSTALL, json=body)
        assert res.status_code == 403 and res.json()["code"] == "auth.forbidden", body
    assert _spec_of(agent_id)["max_tokens"] == 65536 and _job_count() == 1
    # the administrator's edit through the same route is accepted
    assert admin.post(INSTALL, json={"max_tokens": 1000}).status_code == 202
    assert _status(member)["settings"]["max_tokens"] == 1000
    # and the admin edit grants no generic lifecycle bypass
    assert admin.delete(f"/api/agents/{agent_id}").status_code == 403
    assert admin.post(f"/api/agents/{agent_id}/convert").status_code == 403


# ---------------------------------------------------------------------------
# SE-040 correction: races through the REAL route, separate DB sessions, exact pauses
# ---------------------------------------------------------------------------


def _pause_before_claim(monkeypatch, marker_prompt: str):
    """Pause exactly ONE request — the edit whose resolved system prompt is
    ``marker_prompt`` — inside the release-pin → claim window: on the digest-bearing
    ``build_spec`` call ``_repair`` makes right after pinning the bundle and right
    before its compare-and-set. Every other caller is untouched."""
    real = presets.build_spec
    resolved, release = threading.Event(), threading.Event()

    def paused(preset, bucket, options, *, digest=None):
        spec = real(preset, bucket, options, digest=digest)
        if digest is not None and options.system_prompt == marker_prompt:
            resolved.set()
            assert release.wait(timeout=15), "the paused request was never released"
        return spec

    monkeypatch.setattr(service.catalogue, "build_spec", paused)
    return resolved, release


def _finish_job(res_json: dict, agent_id: str) -> None:
    """Run the pipeline's own completion for an accepted job (row back to active)."""
    from app.deployer.pipeline import _finish

    db = SessionLocal()
    try:
        _finish(db, res_json["job_id"], res_json["deployment_id"], agent_id, error=None)
    finally:
        db.close()


def test_delayed_partial_edit_cannot_revert_a_completed_concurrent_edit(client, monkeypatch):
    """Host reproduction #5: A {system_prompt} is resolved, then B {timeout_seconds}
    is accepted AND finishes before A claims. A must land on top of B's row (keeping
    1234) or be refused — never accepted with the timeout reverted to 900."""
    _mark_ready()
    agent_id = client.post(INSTALL, json={}).json()["agent"]["id"]
    _set_status(agent_id, "active", "arn:aws:bedrock-agentcore:us-west-2:1:harness/h1")
    resolved, release = _pause_before_claim(monkeypatch, "A pending prompt")
    outcome: dict = {}

    def run_a() -> None:
        outcome["a"] = client.post(INSTALL, json={"system_prompt": "A pending prompt"})

    thread = threading.Thread(target=run_a)
    thread.start()
    assert resolved.wait(timeout=15)
    b = client.post(INSTALL, json={"timeout_seconds": 1234})
    assert b.status_code == 202, b.text
    _finish_job(b.json(), agent_id)  # B's job completes: row active again, version moved
    assert _agent(agent_id).status == "active"
    release.set()
    thread.join(timeout=20)
    a = outcome["a"]
    assert a.status_code == 202, a.text  # resolved again on B's row and accepted
    spec = _spec_of(agent_id)
    assert spec["system_prompt"] == "A pending prompt"
    assert spec["timeout_seconds"] == 1234  # the member A omitted keeps B's value
    assert a.json()["agent"]["spec"]["timeout_seconds"] == 1234
    assert _job_count() == 3


def test_delayed_edit_while_the_other_is_still_deploying_is_refused_not_reverted(
    client, monkeypatch
):
    """Same window, but B has not finished: A is told 409 with B's job, and B's
    spec is untouched (no silent coalescing onto a job carrying other values)."""
    _mark_ready()
    agent_id = client.post(INSTALL, json={}).json()["agent"]["id"]
    _set_status(agent_id, "active", "arn:aws:bedrock-agentcore:us-west-2:1:harness/h1")
    resolved, release = _pause_before_claim(monkeypatch, "A pending prompt")
    outcome: dict = {}

    def run_a() -> None:
        outcome["a"] = client.post(INSTALL, json={"system_prompt": "A pending prompt"})

    thread = threading.Thread(target=run_a)
    thread.start()
    assert resolved.wait(timeout=15)
    b = client.post(INSTALL, json={"timeout_seconds": 1234})
    assert b.status_code == 202, b.text
    release.set()
    thread.join(timeout=20)
    a = outcome["a"]
    assert a.status_code == 409 and a.json()["code"] == "system_agent.deploy_in_progress"
    assert a.json()["detail"]["job_id"] == b.json()["job_id"]
    spec = _spec_of(agent_id)
    assert spec["timeout_seconds"] == 1234 and spec["system_prompt"] == ARCHITECT.system_prompt
    assert _job_count() == 2


def _race_two(client, bodies: list[dict], monkeypatch) -> list:
    """Two real requests that both pass the pre-claim checks before either claims:
    a barrier inside ``_release_pin`` holds each until the other arrives.

    Both requests are issued inside ONE ``with client:`` context held by the
    controlling thread. A context-less ``TestClient`` opens a separate AnyIO portal
    (its own event loop) per concurrent call, and FastAPI 0.139's lazily built
    ``IncludedRouter`` route-candidate cache is not safe across two loops racing to
    fill it: one request can be answered 404 before the router matches, leaving the
    other alone at the barrier (``BrokenBarrierError`` after 15 s). One context ⇒
    one loop routes both requests; the handlers still run as separate AnyIO service
    workers with their own SQL sessions, so the claim race itself is unchanged. The
    app has no lifespan handler — entering the context starts nothing (temp DB and
    the AWS/network guards stay in force).
    """
    real = service._release_pin
    barrier = threading.Barrier(2)
    arrivals: list[float] = []

    def together(preset):
        pin = real(preset)
        arrivals.append(time.monotonic())
        barrier.wait(timeout=15)
        return pin

    monkeypatch.setattr(service, "_release_pin", together)
    results: list = [None, None]
    errors: list[str] = [None, None]  # type: ignore[list-item]

    def run(i: int) -> None:
        try:
            results[i] = client.post(INSTALL, json=bodies[i])
        except Exception as exc:  # noqa: BLE001 — surfaced in the assertion below
            errors[i] = f"{type(exc).__name__}: {exc}"

    threads = [threading.Thread(target=run, args=(i,), name=f"race-{i}") for i in range(2)]
    with client:  # one portal / event loop for both concurrent requests
        for th in threads:
            th.start()
        for th in threads:
            th.join(timeout=30)
    diagnostics = {
        "bodies": bodies,
        "arrivals_at_barrier": len(arrivals),
        "errors": errors,
        "statuses": [r.status_code if r is not None else None for r in results],
        "responses": [r.text[:300] if r is not None else None for r in results],
    }
    assert not any(errors), diagnostics
    assert all(r is not None for r in results), diagnostics
    assert len(arrivals) == 2, diagnostics  # both really met at the barrier
    return results


def test_two_explicit_first_installs_with_different_settings_never_both_accept(
    client, monkeypatch
):
    """Host reproduction #6: the unique-index loser asked for other settings, so it is
    409 with the winner's job — not a 202 `changed: false` that stored the winner's
    values under its own request."""
    _mark_ready()
    results = _race_two(client, [{"max_tokens": 1111}, {"max_tokens": 2222}], monkeypatch)
    codes = sorted(r.status_code for r in results)
    assert codes == [202, 409], [(r.status_code, r.text) for r in results]
    winner = next(r for r in results if r.status_code == 202)
    loser = next(r for r in results if r.status_code == 409)
    asked = json.loads(winner.request.content)["max_tokens"]  # parse, never byte-compare
    assert winner.json()["agent"]["spec"]["max_tokens"] == asked
    assert loser.json()["code"] == "system_agent.deploy_in_progress"
    assert loser.json()["detail"]["job_id"] == winner.json()["job_id"]
    assert len(_rows(system_key=KEY)) == 1 and _job_count() == 1


def test_two_identical_or_bodiless_first_installs_still_coalesce(client, monkeypatch):
    _mark_ready()
    results = _race_two(client, [{"max_tokens": 4096}, {"max_tokens": 4096}], monkeypatch)
    assert sorted(r.status_code for r in results) == [202, 202]
    assert {r.json()["job_id"] for r in results} == {results[0].json()["job_id"]}
    assert sorted(r.json()["changed"] for r in results) == [False, True]
    assert _spec_of(results[0].json()["agent"]["id"])["max_tokens"] == 4096
    assert len(_rows(system_key=KEY)) == 1 and _job_count() == 1


def test_two_explicit_edits_on_an_active_row_refuse_the_loser_honestly(client, monkeypatch):
    _mark_ready()
    agent_id = client.post(INSTALL, json={}).json()["agent"]["id"]
    _set_status(agent_id, "active", "arn:aws:bedrock-agentcore:us-west-2:1:harness/h1")
    results = _race_two(client, [{"max_tokens": 1111}, {"max_tokens": 2222}], monkeypatch)
    assert sorted(r.status_code for r in results) == [202, 409], [r.text for r in results]
    winner = next(r for r in results if r.status_code == 202)
    loser = next(r for r in results if r.status_code == 409)
    assert loser.json()["detail"]["job_id"] == winner.json()["job_id"]
    assert _spec_of(agent_id)["max_tokens"] == winner.json()["agent"]["spec"]["max_tokens"]
    assert _job_count() == 2
