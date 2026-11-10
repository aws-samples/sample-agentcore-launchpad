"""System-managed presets (SE-038): server-owned identity, protected mutation paths,
idempotent admin install, no AWS on reads — all hermetic (deploy launch stubbed, AWS
client factory made to fail loudly wherever a read path might reach for it).

N7: every preset is a `method="harness"` self-built Harness, rendered by
`deployer.harness_local` and deployed through the ordinary `zip_runtime` provision/
deploy/register stages (plan ruling R14) — not the removed AWS-managed Harness API.
"""

import json
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app.routers.agents as agents_router
import app.routers.system_agents as system_router
from app.core.config import get_settings
from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.core.errors import AppError
from app.deployer import harness_local, zip_runtime
from app.deployer.environment import runtime_environment
from app.deployer.harness_local import STAGES
from app.deployer.pipeline import StageContext, create_deployment
from app.main import create_app
from app.models.ledger import Agent, Job, Workspace
from app.schemas.agent import DEFAULT_MODEL_ID, AgentSpec
from app.services import agent_iam, aws_clients
from app.services import harness_convert as hc
from app.services import users as users_service
from app.services.runtime_discovery import _display_name
from app.system_agents import presets, service
from app.system_agents.presets import ARCHITECT, InstallOptions, PresetEdit
from tests.conftest import settle_deploy_jobs

from .conftest import ws_ctx

FIXTURE_TEMPLATE = Path(__file__).parent / "fixtures" / "harness_runtime_min"


@pytest.fixture(autouse=True)
def fixture_template(monkeypatch):
    """Every render in this file uses the minimal fixture template (Task 1), not the
    real vendored one — swapped in for every test, same as test_harness_local.py."""
    monkeypatch.setattr(harness_local, "TEMPLATE_DIR", FIXTURE_TEMPLATE)


KEY = ARCHITECT.key
INSTALL = f"/api/system-agents/{KEY}/install"
BUCKET = "launchpad-artifacts-test"
VER = ARCHITECT.skill_version  # the catalogue version this build ships
READY_RESOURCES = {
    "artifacts_bucket": BUCKET,
    "execution_role_arn": "arn:aws-cn:iam::111122223333:role/launchpad-agent-execution-role",
    "memory_arn": ("arn:aws-cn:bedrock-agentcore:cn-northwest-1:111122223333:memory/"
                   "launchpad_memory-x"),
    "memory_id": "launchpad_memory-x",
}
RUNTIME_ARN = "arn:aws-cn:bedrock-agentcore:cn-northwest-1:111122223333:runtime/arch-1"


def _rt_ctx(resources: dict | None = None, **fields):
    """``ws_ctx`` pinned to the CN region/partition this preset actually deploys to."""
    fields.setdefault("region", "cn-northwest-1")
    return ws_ctx(resources if resources is not None else READY_RESOURCES, **fields)


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
    assert spec.model_id == DEFAULT_MODEL_ID and spec.reasoning_effort is None
    assert spec.skills == [f"s3://{BUCKET}/system-skills/{ARCHITECT.name}/{VER}/"]
    addressed = presets.build_spec(ARCHITECT, BUCKET, InstallOptions(), digest="ab" * 32)
    assert addressed.skills == [f"s3://{BUCKET}/system-skills/{ARCHITECT.name}/{VER}-abababababab/"]
    assert presets.skill_release_from_spec(addressed.model_dump()) == (VER, "abababababab")
    assert presets.skill_version_from_spec(spec.model_dump()) == VER
    assert spec.allowed_tools == ["shell", "file_*", "@aws_knowledge"]
    assert "*" not in spec.allowed_tools
    assert [t.type for t in spec.tools] == ["mcp"]
    assert spec.tools[0].config["url"] == "https://knowledge-mcp.global.api.aws"
    assert not [t for t in spec.tools if t.type == "builtin"]
    # no secrets / account ids in the prompt
    assert "arn:aws" not in spec.system_prompt and "111122223333" not in spec.system_prompt
    bundle = harness_local.render_harness_bundle(spec, _rt_ctx())
    main, client = bundle.files["main.py"], bundle.files["mcp_client/client.py"]
    assert "def shell(" in main and "def file_operations(" in main
    assert hc.GW_SOFT_CLIENT in client and "https://knowledge-mcp.global.api.aws" in client
    assert spec.tools[0].config["auth"] == "none"  # public server → no identity grant
    # persistent memory is explicitly opted out (the flags cannot express
    # "short-term only" against the real API; see presets.build_spec)
    assert spec.memory.short_term is False and spec.memory.long_term is False
    assert "LAUNCHPAD_MEMORY_ID" not in runtime_environment(spec, READY_RESOURCES)


def test_ordinary_harness_spec_sends_empty_allowed_tools():
    """An empty ordinary selection cannot expose Harness native tools."""
    spec = AgentSpec(name="plain-harness", method="harness", system_prompt="hi")
    assert spec.allowed_tools is None
    bundle = harness_local.render_harness_bundle(spec, _rt_ctx())
    assert "def shell(" not in bundle.files["main.py"]
    assert "def file_operations(" not in bundle.files["main.py"]


def test_allowed_tools_shape_is_validated():
    with pytest.raises(ValueError):
        AgentSpec(name="plain-harness", method="harness", system_prompt="hi",
                  allowed_tools=["a/b/c"])


def test_execution_role_grants_only_the_versioned_skill_prefix_and_no_broad_tools():
    spec = presets.build_spec(ARCHITECT, BUCKET, InstallOptions())
    ctx = agent_iam.role_context(_rt_ctx())
    doc = agent_iam.policy_document(spec, ctx)
    sids = {s["Sid"] for s in doc["Statement"]}
    assert "AgentCoreCodeInterpreter" not in sids and "AgentCoreBrowser" not in sids
    assert "EcrPull" not in sids and "ManagedKbRetrieval" not in sids
    # unauthenticated docs-only MCP + no persistent memory ⇒ no credential/memory access
    assert "AgentCoreWorkloadIdentity" not in sids and "IdentityVaultSecrets" not in sids
    assert "AgentCoreMemory" not in sids
    assert sids == {"ModelCredentialApiKey", "ModelCredentialVaultSecret",
                    "SkillBundleObjects", "SkillBundleList", "LaunchpadBundleRead",
                    "Telemetry", "TelemetryTracing"}
    objects = next(s for s in doc["Statement"] if s["Sid"] == "SkillBundleObjects")
    assert objects["Resource"] == [
        ctx.arn("s3", f"{BUCKET}/system-skills/{ARCHITECT.name}/{VER}/*", region="", account="")
    ]  # the install itself scopes to the content-addressed release directory
    # member-writable prefixes are never readable by the preset's role
    assert not any("/skills/" in r or "/agent-skills/" in r for r in objects["Resource"])
    for statement in doc["Statement"]:
        for action in ([statement["Action"]] if isinstance(statement["Action"], str)
                       else statement["Action"]):
            assert not action.startswith(("iam:", "s3:Put", "s3:Delete", "bedrock-agentcore:Create"
                                          "AgentRuntime", "bedrock-agentcore:Delete"))


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
        "managed": True, "key": KEY, "label": ARCHITECT.label, "skill_version": VER,
        "protected_actions": ["redeploy", "delete", "convert", "canary"],
    }
    assert agent["spec"]["allowed_tools"] == ["shell", "file_*", "@aws_knowledge"]
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
    _set_status(agent_id, "active", "arn:aws-cn:bedrock-agentcore:cn-northwest-1:1:runtime/h1")
    assert _status(client)["status"] == "active"

    same = client.post(INSTALL, json={})
    assert same.status_code == 200 and same.json()["changed"] is False
    # the stable outcome: the job that produced the active preset, no new one
    assert same.json()["job_id"] == first_job and _job_count() == 1

    # a model outside the CN catalog is an unsupported pairing, refused before any
    # job is queued; the other allowed model (Kimi) is accepted
    refused = client.post(INSTALL, json={"model_id": "global.anthropic.claude-opus-5"})
    assert refused.status_code == 422 and refused.json()["code"] == "system_agent.invalid_options"
    assert _job_count() == 1
    changed = client.post(INSTALL, json={"model_id": "global.moonshotai.kimi-k3"})
    assert changed.status_code == 202 and changed.json()["changed"] is True
    assert _job_count() == 2
    db = SessionLocal()
    try:
        job = db.get(Job, changed.json()["job_id"])
        assert job.payload["mode"] == "update"
        assert db.get(Agent, agent_id).spec["model_id"] == "global.moonshotai.kimi-k3"
        # the administrator's earlier choice is what a bodiless repair keeps
    finally:
        db.close()
    _set_status(agent_id, "active")
    kept = client.post(INSTALL, json={})
    assert kept.status_code == 200 and kept.json()["preset"]["model_id"] == (
        "global.moonshotai.kimi-k3"
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


def test_install_with_knowledge_base_mounts_only_the_given_reference(client, monkeypatch):
    from app.services import knowledge_backend

    monkeypatch.setattr(knowledge_backend, "_backend", object())
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


def test_reserved_name_is_refused_for_ordinary_agents_and_spoofed_metadata_is_refused(client):
    res = client.post("/api/agents", json={
        "name": ARCHITECT.name, "method": "harness", "system_prompt": "impostor",
    })
    assert res.status_code == 422 and res.json()["code"] == "agent.name_reserved"
    assert res.json()["detail"] == {"name": ARCHITECT.name, "reserved_for": ARCHITECT.key}
    assert _rows() == []

    # N10v3 spoof rule, tightened by N16-obs78: the system metadata is never honoured —
    # a body naming it is now 422 naming the keys (was: silently dropped, 202)
    spoof = client.post("/api/agents", json={
        "name": "totally-ordinary", "method": "harness", "system_prompt": "hi",
        "system_key": KEY, "system": {"managed": True}, "owner": "system",
    })
    assert spoof.status_code == 422
    assert spoof.json()["code"] == "validation.invalid_request"
    assert sorted(e["loc"][-1] for e in spoof.json()["detail"]) == ["owner", "system", "system_key"]
    assert _rows(name="totally-ordinary") == []


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
    _set_status(agent_id, "active", "arn:aws-cn:bedrock-agentcore:cn-northwest-1:1:runtime/h1")
    monkeypatch.setattr(agents_router, "_delete_agent_resources",
                        lambda *a, **k: pytest.fail("teardown reached for a system agent"))
    spec = client.get(f"/api/agents/{agent_id}").json()["spec"]

    res = client.delete(f"/api/agents/{agent_id}")
    assert res.status_code == 403 and res.json()["code"] == "agent.system_managed"
    assert res.json()["detail"]["action"] == "delete"
    res = client.post(f"/api/agents/{agent_id}/redeploy", json=spec)
    assert res.status_code == 403 and res.json()["detail"]["action"] == "redeploy"
    # /convert is gone (R15 — conversion has no CN implementation); not re-tested here
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
    _set_status(agent_id, "active", "arn:aws-cn:bedrock-agentcore:cn-northwest-1:1:runtime/h1")
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
    # /convert is gone (R15 — conversion has no CN implementation); not re-tested here
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
    _set_status(agent_id, "active", "arn:aws-cn:bedrock-agentcore:cn-northwest-1:1:runtime/h2")
    spec = created.json()["agent"]["spec"]
    assert member.post(f"/api/agents/{agent_id}/redeploy", json=spec).status_code == 202
    _set_status(agent_id, "active")
    settle_deploy_jobs(agent_id)  # N16-obs83: the redeploy job must end before a delete
    monkeypatch.setattr(agents_router, "_delete_agent_resources", lambda *a, **k: True)
    res = member.delete(f"/api/agents/{agent_id}")
    assert res.status_code == 200 and res.json()["deleted"] is True


def test_admin_maintains_the_preset_through_the_dedicated_routes_only(gated, monkeypatch):
    admin, _member = gated
    _mark_ready()
    agent_id = admin.post(INSTALL, json={}).json()["agent"]["id"]
    _set_status(agent_id, "active", "arn:aws-cn:bedrock-agentcore:cn-northwest-1:1:runtime/h1")
    # even the administrator does not edit a preset through the ordinary routes
    assert admin.delete(f"/api/agents/{agent_id}").status_code == 403
    assert admin.post(INSTALL, json={"force": True}).status_code == 202
    _set_status(agent_id, "active")
    monkeypatch.setattr(system_router, "start_uninstall_async", lambda _jid: None)
    assert admin.delete(f"/api/system-agents/{KEY}").status_code == 202


# ---------------------------------------------------------------------------
# package stage: versioned, checksummed skill upload inside the job
# ---------------------------------------------------------------------------


def test_package_stage_builds_a_zip_for_ordinary_harness_agents(monkeypatch, tmp_path):
    """The self-built Harness renders a multi-file bundle and zips it — the package
    stage is never a no-op for `method="harness"` (plan R1/Task 3)."""
    agent = Agent(id="b" * 32, name="plain", method="harness", status="deploying",
                  spec=AgentSpec(name="plain", method="harness", system_prompt="hi").model_dump())
    uploads: list[tuple[str, str]] = []

    class S3:
        def upload_file(self, path, bucket, key):
            uploads.append((bucket, key))

    ws = _rt_ctx()
    monkeypatch.setattr(type(ws), "client", lambda self, name: S3())
    ctx = StageContext(agent_id=agent.id, deployment_id="d", job_id="j", workspace=ws)

    def fake_build_zip(code, requirements, build_dir, *, on_pkg_ready=None, on_lock_ready=None):
        pkg = tmp_path / "pkg"
        pkg.mkdir(exist_ok=True)
        (pkg / "main.py").write_text(code)
        if on_pkg_ready:
            on_pkg_ready(pkg)
        import zipfile

        archive = tmp_path / "pkg.zip"
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("main.py", code)
        return archive

    monkeypatch.setattr(harness_local, "build_zip", fake_build_zip)
    monkeypatch.setattr(harness_local, "record_input_contract", lambda ctx, code: None)
    STAGES["generate"](ctx, agent)
    result = STAGES["package"](ctx, agent)
    assert not result.skipped and "s3 ✓" in result.detail
    assert uploads == [("launchpad-artifacts-test", "agents/plain/deployment_package.zip")]


# ---------------------------------------------------------------------------
# correction pass 2 — host-reproduced findings
# ---------------------------------------------------------------------------

from app.core.config import get_settings as _real_settings  # noqa: E402

KB_REF = {"kb_id": "KB123ABC", "name": "whitepaper", "description": "the guide"}


def _settings(**over):
    return _real_settings().model_copy(update=over)


class _Control:
    """create_agent_runtime/update_agent_runtime/get_agent_runtime stub — the shape
    `zip_runtime`'s shared provision/deploy stages talk to (the self-built Harness
    carries no AWS API of its own; copy of test_zip_runtime_deployer.py's fake)."""

    class exceptions:
        class ResourceNotFoundException(Exception):
            pass

    def __init__(self, fail_first_create: bool = False):
        self.creates: list[dict] = []
        self.updates: list[dict] = []
        self.fail_first_create = fail_first_create

    def create_agent_runtime(self, **kwargs):
        self.creates.append(kwargs)
        if self.fail_first_create and len(self.creates) == 1:
            raise ConnectionError("socket closed after the request was accepted")
        return {"agentRuntimeId": "rt-1", "agentRuntimeArn": RUNTIME_ARN,
                "agentRuntimeVersion": "1", "status": "READY"}

    def update_agent_runtime(self, **kwargs):
        self.updates.append(kwargs)
        return {"agentRuntimeId": kwargs["agentRuntimeId"], "agentRuntimeArn": RUNTIME_ARN,
                "agentRuntimeVersion": "2", "status": "READY"}

    def list_agent_runtimes(self, **_kwargs):  # N10v2-bug1: nothing to adopt here
        return {"agentRuntimes": []}

    def get_agent_runtime(self, agentRuntimeId):
        return {"agentRuntimeId": agentRuntimeId, "agentRuntimeArn": RUNTIME_ARN,
                "status": "READY", "agentRuntimeVersion": "2" if self.updates else "1"}


def _persist_preset(status="deploying", resource_id=None, spec=None) -> str:
    db = SessionLocal()
    try:
        agent = Agent(workspace_id=DEFAULT_WORKSPACE_ID, name=ARCHITECT.name, method="harness",
                      status=status, system_key=KEY, resource_id=resource_id,
                      arn=RUNTIME_ARN if resource_id else None,
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
    logs: list[str] = []
    ctx = StageContext(agent_id=agent_id, deployment_id=deployment_id, job_id=job_id,
                       workspace=_rt_ctx(), log=logs.append)
    ctx.scratch.update(scratch)
    ctx.logs = logs  # the harness package stage logs package_preset_skills' detail;
    # its own StageResult.detail is the pip+zip summary (R1/Task 3) — check both
    return ctx


def _agent(agent_id):
    db = SessionLocal()
    try:
        return db.get(Agent, agent_id)
    finally:
        db.close()


def test_preset_provision_and_deploy_fail_closed_when_per_agent_roles_are_disabled(
    monkeypatch,
):
    """R14: a preset is never deployed on the shared workspace role. zip_runtime's
    shared provision/deploy stages call service.require_dedicated_role for every
    system_key agent — toggling the global per-agent-roles setting off must fail
    closed, not silently fall back to the shared role."""
    agent_id, dep_id, job_id = _persist_preset()
    control = _Control()
    monkeypatch.setattr(zip_runtime, "control_client", lambda _ws: control)
    monkeypatch.setattr(zip_runtime, "get_settings",
                        lambda: _settings(per_agent_execution_roles=False))
    with pytest.raises(RuntimeError, match="per-agent execution roles"):
        STAGES["provision"](_ctx(agent_id, dep_id, job_id), _agent(agent_id))
    assert control.creates == []  # refused before any AWS call
    # the deploy stage refuses too, even on a resumed job with no provision scratch
    deploy_ctx = _ctx(agent_id, dep_id, job_id,
                      execution_role_arn=READY_RESOURCES["execution_role_arn"])
    with pytest.raises(RuntimeError, match="per-agent execution roles"):
        STAGES["deploy"](deploy_ctx, _agent(agent_id))
    assert control.creates == []
    monkeypatch.setattr(zip_runtime, "get_settings", _real_settings)


def test_ordinary_harness_keeps_the_shared_role_when_per_agent_roles_are_off(
    monkeypatch,
):
    """The dedicated-role guard is preset-only; an ordinary agent still falls back to
    the shared workspace role when per-agent roles are globally disabled."""
    db = SessionLocal()
    agent = Agent(workspace_id=DEFAULT_WORKSPACE_ID, name="plain-shared", method="harness",
                  status="deploying",
                  spec=AgentSpec(name="plain-shared", method="harness",
                                 system_prompt="x").model_dump())
    db.add(agent)
    db.flush()
    dep, job = create_deployment(db, agent)
    ids = (agent.id, dep.id, job.id)
    db.close()
    monkeypatch.setattr(zip_runtime, "get_settings",
                        lambda: _settings(per_agent_execution_roles=False))
    control = _Control()
    monkeypatch.setattr(zip_runtime, "control_client", lambda _ws: control)
    ctx = _ctx(*ids)
    result = STAGES["provision"](ctx, _agent(ids[0]))
    assert ctx.scratch["execution_role_arn"] == READY_RESOURCES["execution_role_arn"]
    assert "shared" in result.detail
    monkeypatch.setattr(zip_runtime, "get_settings", _real_settings)


# ---- (5) KB-mode least privilege: direct tools only, no OAuth provider --------


def test_preset_kb_mode_grants_the_direct_kb_tool_statements_no_oauth(client, monkeypatch):
    """R13/R16 + Step 0: KB-gateway OAuth reads have no CN implementation and are
    gone — a KB-mounting preset gets exactly the same workload-identity shape an
    ordinary agent with a KB gets, never an OAuth-provider-scoped statement."""
    spec = presets.build_spec(ARCHITECT, BUCKET, InstallOptions(
        knowledge_bases=(presets.KnowledgeBaseRef(**KB_REF),)))
    ctx = agent_iam.role_context(_rt_ctx())
    doc = agent_iam.policy_document(spec, ctx)
    sids = {s["Sid"] for s in doc["Statement"]}
    assert {"AgentCoreWorkloadIdentity", "IdentityVaultSecrets"} <= sids
    assert not {"ManagedKbRetrieval", "ManagedKbAgenticRetrieval", "AgentCoreMemory",
                "AgentCoreCodeInterpreter"} & sids
    for sid in sids:
        assert "OAuth2" not in sid
    resources = " ".join(str(s["Resource"]) for s in doc["Statement"])
    assert "oauth2credentialprovider" not in resources
    # an ordinary agent with the same KB gets the identical KB-driven shape — no
    # preset-only path (the preset's extra sids are its skills, unrelated to the KB)
    plain = AgentSpec(name="plain", method="harness", system_prompt="x", knowledge_bases=[KB_REF])
    plain_sids = {s["Sid"] for s in agent_iam.policy_document(plain, ctx)["Statement"]}
    assert {"AgentCoreWorkloadIdentity", "IdentityVaultSecrets"} <= plain_sids
    assert not {"ManagedKbRetrieval", "ManagedKbAgenticRetrieval"} & plain_sids
    # installing with a KB succeeds without any oauth_provider_arn in the resource map,
    # once a knowledge-base backend is registered (R13: the direct-tools prerequisite)
    assert "oauth_provider_arn" not in READY_RESOURCES
    from app.services import knowledge_backend

    monkeypatch.setattr(knowledge_backend, "_backend", object())
    _mark_ready()
    res = client.post(INSTALL, json={"knowledge_bases": [KB_REF]})
    assert res.status_code == 202, res.text


def test_preset_kb_mode_requires_a_registered_knowledge_backend(client, monkeypatch):
    """Without a knowledge-base backend registered, nothing could ever answer the
    mount — refused at request time, before any row (Step 0 rework of the old
    oauth_provider_arn-gated test)."""
    from app.services import knowledge_backend

    monkeypatch.setattr(knowledge_backend, "_backend", None)
    _mark_ready()
    res = client.post(INSTALL, json={"knowledge_bases": [KB_REF]})
    assert res.status_code == 409 and res.json()["code"] == "system_agent.workspace_not_ready"
    assert res.json()["detail"]["requirements"][0]["code"] == "missing_knowledge_backend"
    assert _rows() == []
    # a docs-only install (no KB) does not need a backend at all
    first = client.post(INSTALL, json={})
    assert first.status_code == 202
    _set_status(first.json()["agent"]["id"], "active")
    # once a backend is registered, the same request succeeds
    monkeypatch.setattr(knowledge_backend, "_backend", object())
    res2 = client.post(INSTALL, json={"knowledge_bases": [KB_REF]})
    assert res2.status_code == 202, res2.text


# ---- (4) durable maintenance claims --------------------------------------------


def test_two_sessions_repairing_the_same_active_preset_share_one_job(client):
    """Host reproduction: both sessions loaded the active row before either wrote."""
    _mark_ready()
    agent_id = client.post(INSTALL, json={}).json()["agent"]["id"]
    _set_status(agent_id, "active", "arn:aws-cn:bedrock-agentcore:cn-northwest-1:1:runtime/h1")
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
    _set_status(agent_id, "active", "arn:aws-cn:bedrock-agentcore:cn-northwest-1:1:runtime/h1")
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







def test_preset_status_reports_runtime_version_and_releases(client):
    """N12-obs31 (c): the preset entry used to show no version/release although the agent
    row said version 1 — now the live runtime version, this build's release (what a
    repair compares against) and the release the stored spec pins are all projected."""
    from app.system_agents.presets import EVAL_WORKER

    _mark_ready()
    agent_id = client.post(INSTALL, json={}).json()["agent"]["id"]
    _set_status(agent_id, "active", RUNTIME_ARN)
    db = SessionLocal()
    db.get(Agent, agent_id).version = "3"
    db.commit()
    db.close()

    st = _status(client)
    assert st["version"] == "3"
    rel = st["release"]
    assert rel["version"] == VER and len(rel["digest"]) == 12
    assert rel["tag"] == f"{VER}-{rel['digest']}"
    assert st["installed_release"] == rel["tag"]  # a fresh install pins this build's release
    # the template preset projects its template release even when not installed
    rows = {p["key"]: p for p in client.get("/api/system-agents").json()["presets"]}
    worker = rows[EVAL_WORKER.key]
    expected = presets.template_release(EVAL_WORKER)
    assert worker["release"] == {"version": expected["version"],
                                 "digest": str(expected["digest"])[:12], "tag": expected["tag"]}
    assert worker["version"] is None and worker["installed_release"] is None
    assert worker["last_job"] is None
    # review #5: the release is memoised per preset key — the second read hashes nothing
    calls = {"n": 0}
    real = presets.bundle_release

    def counting(preset):
        calls["n"] += 1
        return real(preset)

    import pytest as _pytest
    with _pytest.MonkeyPatch.context() as mp:
        mp.setattr(presets, "bundle_release", counting)
        assert client.get("/api/system-agents").status_code == 200
    assert calls["n"] == 0
    assert service.release_info(presets.ARCHITECT) == rel


def _break_bundle(monkeypatch, message: str) -> None:
    """Make this build unable to release the architect bundle — what the live host saw
    when `render_skill_md` demanded the disabled SiliconFlow rows (N-ARCH-obs19)."""
    def broken(preset):
        raise ValueError(message)

    monkeypatch.setattr(presets, "snapshot_bundle", broken)
    monkeypatch.setattr(presets, "bundle_release", broken)
    service._RELEASE_INFO.clear()


def _heal_bundle(monkeypatch, originals: tuple) -> None:
    """Restore the two patched attributes only — never `monkeypatch.undo()`, which would
    also drop the autouse fixtures' patches (template dir, no-real-deploy)."""
    monkeypatch.setattr(presets, "snapshot_bundle", originals[0])
    monkeypatch.setattr(presets, "bundle_release", originals[1])
    service._RELEASE_INFO.clear()


def test_install_and_repair_refuse_an_unreleasable_bundle_with_409_not_500(
        client, no_real_deploy, monkeypatch):
    """N-ARCH-obs19 (live 2026-10-16 06:52Z): `POST …/install {}` on the installed
    architect answered 500 from `_release_pin` → `snapshot_bundle` → `render_skill_md`
    (`ValueError: … states no model catalog (missing ['siliconflow.…'])`). The route must
    answer a structured `409 system_agent.bundle_invalid` carrying the message, claim and
    write nothing, and the status read must expose the same message as `release_error`."""
    _mark_ready()
    originals = (presets.snapshot_bundle, presets.bundle_release)
    reason = ("preset 'aws-agent-solution-architect': SKILL.md states no model catalog "
              "(missing ['x'])")
    _break_bundle(monkeypatch, reason)

    # first install: refused before any row or job exists
    refused = client.post(INSTALL, json={})
    assert refused.status_code == 409, refused.text
    body = refused.json()
    assert body["code"] == "system_agent.bundle_invalid"
    assert reason in body["message"] and "1.7" in body["message"]
    assert body["detail"]["preset_key"] == KEY and body["detail"]["skill_version"] == VER
    assert body["detail"]["reason"] == reason and "catalog_version" in body["detail"]
    status = _status(client)
    assert status["status"] == "not_installed" and status["agent_id"] is None
    assert status["release"] is None and status["release_error"] == reason
    assert _job_count() == 0

    # a healthy build installs; the preset becomes active
    _heal_bundle(monkeypatch, originals)
    first = client.post(INSTALL, json={})
    assert first.status_code == 202, first.text
    agent_id = first.json()["agent"]["id"]
    _set_status(agent_id, "active", "arn:aws-cn:bedrock-agentcore:cn-northwest-1:1:runtime/h1")
    healthy = _status(client)
    assert healthy["release"]["version"] == VER and healthy["release_error"] is None

    # repair on a build that cannot release: same verdict, the active row is untouched
    _break_bundle(monkeypatch, reason)
    repair = client.post(INSTALL, json={"force": True})
    assert repair.status_code == 409 and repair.json()["code"] == "system_agent.bundle_invalid"
    after = _status(client)
    assert after["status"] == "active" and after["agent_id"] == agent_id
    assert after["error"] is None and after["last_deploy_failed"] is False
    assert after["release"] is None and after["release_error"] == reason
    assert after["installed_release"] == healthy["installed_release"]
    assert _job_count() == 1


def test_release_memo_follows_the_published_catalog_version(client):
    """N-ARCH-obs19 (b): the release of a Harness preset is a function of the published
    model catalog (the SKILL.md states it), so a registry write must move the tag the
    status read reports without a restart — the memo is keyed by catalog version."""
    import dataclasses

    from app.schemas import model_catalog

    service._RELEASE_INFO.clear()
    before = _status(client)["release"]
    assert before["version"] == VER
    cat = model_catalog.current()
    fake = "global.example.obs19-model"
    row = dataclasses.replace(cat.models["global.zai.glm-5.3"], model_id=fake, label=fake,
                              api_model_name=fake, default=False)
    try:
        model_catalog.publish(model_catalog.extended(cat, models={fake: row},
                                                     version=cat.version + 1))
        after = _status(client)["release"]
        assert after["version"] == VER and after["digest"] != before["digest"]
        assert after["tag"] != before["tag"]
        assert service.release_info(presets.ARCHITECT) == after
        # bounded: one entry per preset, the newest catalog version (rev-r11 nit)
        keys = [k for k in service._RELEASE_INFO if k[0] == KEY]
        assert keys == [(KEY, cat.version + 1)]
    finally:
        model_catalog.reset()
        service._RELEASE_INFO.clear()
    assert _status(client)["release"] == before


def test_preset_job_is_readable_through_the_preset_route_for_admins(gated, monkeypatch):
    """N12-obs31 (a): a preset install job filed under a workspace id the agents API does
    not expose answered 404 on GET /api/jobs/{id}; the preset route resolves it through
    the preset's agent row, lists it as `last_job`, and is administrator-only."""
    admin, member = gated
    _mark_ready()
    body = admin.post(INSTALL, json={}).json()
    job_id, agent_id = body["job_id"], body["agent"]["id"]
    st = _status(admin)
    assert st["last_job"]["id"] == job_id and st["last_job"]["type"] == "deploy_agent"
    assert st["last_job"]["status"] == "queued"

    db = SessionLocal()
    db.get(Job, job_id).workspace_id = "system-workspace"  # how the live install was filed
    db.commit()
    db.close()
    assert admin.get(f"/api/jobs/{job_id}").status_code == 404  # the agents API's view
    res = admin.get(f"/api/system-agents/{KEY}/jobs/{job_id}")
    assert res.status_code == 200, res.text
    assert res.json()["id"] == job_id and res.json()["status"] == "queued"
    assert isinstance(res.json()["events"], list)
    assert member.get(f"/api/system-agents/{KEY}/jobs/{job_id}").status_code == 403
    assert admin.get(f"/api/system-agents/{KEY}/jobs/nope").status_code == 404
    # another preset's route does not see this job
    assert admin.get(f"/api/system-agents/eval-worker/jobs/{job_id}").status_code == 404
    # an ordinary agent's job is not readable through the preset route either
    db = SessionLocal()
    other = Agent(workspace_id=DEFAULT_WORKSPACE_ID, name="plain-agent", method="harness",
                  status="deploying", spec={})
    db.add(other)
    db.flush()
    _dep, other_job = create_deployment(db, other)
    db.close()
    assert admin.get(f"/api/system-agents/{KEY}/jobs/{other_job.id}").status_code == 404

    # the uninstall job becomes the preset's last job and stays readable after `deleted`
    _set_status(agent_id, "active", RUNTIME_ARN)
    monkeypatch.setattr(system_router, "start_uninstall_async", lambda _jid: None)
    un = admin.delete(f"/api/system-agents/{KEY}").json()
    assert _status(admin)["last_job"]["id"] == un["job_id"]
    _set_status(agent_id, "deleted")
    assert admin.get(f"/api/system-agents/{KEY}/jobs/{un['job_id']}").status_code == 200


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
PREFIX = f"system-skills/{ARCHITECT.name}/{VER}-{DIGEST12}/"
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

    def upload_file(self, path, bucket, key):
        """The harness package stage's deployment-zip upload — not part of the
        versioned skill publish's conditional-write contract."""
        with open(path, "rb") as fh:
            self.objects[key] = fh.read()

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


def _fake_build_zip(code, requirements, build_dir, *, on_pkg_ready=None, on_lock_ready=None):
    """Stand-in for `harness_local.build_zip`: no real pip/uv resolution. These tests
    exercise the versioned-skill S3 publish contract, not the zip-build mechanics
    (already covered by test_harness_local.py / test_zip_runtime_deployer.py)."""
    import zipfile

    pkg = Path(build_dir) / "pkg"
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "main.py").write_text(code)
    if on_pkg_ready:
        on_pkg_ready(pkg)
    archive = Path(build_dir) / "pkg.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("main.py", code)
    return archive


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
        payload["preset_bundle"] = {"version": VER}  # no digest, no files
    elif pin == "stale":
        payload["preset_bundle"] = {**_snapshot().release(), "digest": "f" * 64}
    job.payload = payload
    db.commit()
    db.close()
    monkeypatch.setattr(aws_clients, "client", lambda service_, ws, **kw: fake)
    monkeypatch.setattr(harness_local, "build_zip", _fake_build_zip)
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
    assert pin == {"version": VER, "digest": snap.digest, "files": snap.file_digests()}
    assert "SKILL.md" in pin["files"] and len(pin["files"]) == len(snap.files)
    # the spec loads exactly this snapshot's content-addressed release directory
    assert body["agent"]["spec"]["skills"] == [
        f"s3://{BUCKET}/system-skills/{ARCHITECT.name}/{VER}-{snap.digest[:12]}/"
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
    _set_status(agent_id, "active", "arn:aws-cn:bedrock-agentcore:cn-northwest-1:1:runtime/h1")
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

    def fake_build_zip(code, requirements, build_dir, *, on_pkg_ready=None, on_lock_ready=None):
        import zipfile

        archive = build_dir.parent / "pkg.zip"
        archive.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("main.py", code)
        return archive

    monkeypatch.setattr(harness_local, "build_zip", fake_build_zip)
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
    STAGES["package"](ctx, agent)
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
    log_text = " ".join(ctx.logs)
    assert "published" in log_text and snap.digest[:12] in log_text
    # identical replay: zero writes, still verified against every object
    n = len(fake.puts)
    ctx.logs.clear()
    STAGES["package"](ctx, agent)
    assert len(fake.puts) == n and "already published · verified" in " ".join(ctx.logs)


def test_package_stage_publishes_skills_before_building_the_zip(monkeypatch):
    """R5: the harness package stage publishes the versioned skill bundle BEFORE it
    builds the deployment zip — a publish failure must never ship a stale or foreign
    bundle inside an already-built package."""
    fake = _S3()
    ctx, agent = _pinned_preset(monkeypatch, fake)
    order: list[str] = []
    real_publish = service.package_preset_skills

    def spy_publish(ctx_, agent_):
        order.append("publish")
        return real_publish(ctx_, agent_)

    def spy_build_zip(code, requirements, build_dir, *, on_pkg_ready=None, on_lock_ready=None):
        order.append("build_zip")
        return _fake_build_zip(code, requirements, build_dir,
                               on_pkg_ready=on_pkg_ready, on_lock_ready=on_lock_ready)

    monkeypatch.setattr(service, "package_preset_skills", spy_publish)
    monkeypatch.setattr(harness_local, "build_zip", spy_build_zip)
    STAGES["package"](ctx, agent)
    assert order == ["publish", "build_zip"]


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
    STAGES["package"](ctx, agent)
    written = {p["Key"] for p in fake.puts}
    assert not (written & set(partial))  # existing identical objects were not rewritten
    assert MANIFEST in written and "published" in " ".join(ctx.logs)


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
    STAGES["package"](ctx, agent)
    assert "published" in " ".join(ctx.logs)


def test_competing_manifest_with_different_content_fails(monkeypatch):
    fake = _S3()
    ctx, agent = _pinned_preset(monkeypatch, fake)

    def racer(s3: _S3, kwargs):
        if kwargs["Key"] == MANIFEST and MANIFEST not in s3.objects:
            s3.objects[MANIFEST] = json.dumps({"version": VER, "digest": "0" * 64,
                                               "files": {}}).encode()

    fake.before_put = racer
    with pytest.raises(RuntimeError, match="competing publication claimed"):
        STAGES["package"](ctx, agent)
    assert json.loads(fake.objects[MANIFEST])["digest"] == "0" * 64  # untouched


@pytest.mark.parametrize("manifest", [
    b"not json", json.dumps({"version": VER}).encode(),
    json.dumps({"version": VER, "digest": "0" * 64, "files": {}}).encode(),
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
    STAGES["package"](ctx, agent)
    assert fake.objects[f"{PREFIX}SKILL.md"] == snap.files["SKILL.md"]
    assert [p["Key"] for p in fake.puts] == [f"{PREFIX}SKILL.md"]
    assert fake.puts[0].get("IfNoneMatch") == "*"
    log_text = " ".join(ctx.logs)
    assert "repaired" in log_text and "verified" not in log_text


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
    STAGES["package"](ctx, agent)
    put = fake.puts[-1]
    assert put["Key"] == f"{PREFIX}references/intake-options.md" and put["IfMatch"]
    assert fake.objects[put["Key"]] == good and "repaired" in " ".join(ctx.logs)
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


class _RuntimeControl:
    """Fake bedrock-agentcore-control for the Runtime teardown step. DeleteAgentRuntime
    is accepted; GetAgentRuntime walks a scripted status sequence and finally raises
    ResourceNotFoundException (the only "gone") — the shape `uninstall._teardown_runtime`
    polls, mirroring the pre-CN DeleteHarness/GetHarness loop its docstring describes.
    `kb_target` has no CN implementation and is always skipped without any AWS call."""

    class exceptions:
        class ResourceNotFoundException(Exception):
            pass

    def __init__(self, statuses=("DELETING", None), missing=False):
        self.statuses = list(statuses)  # None ⇒ ResourceNotFound
        self.missing = missing
        self.calls: list[tuple] = []

    def delete_agent_runtime(self, agentRuntimeId):
        self.calls.append(("delete", agentRuntimeId))
        if self.missing:
            raise self.exceptions.ResourceNotFoundException(agentRuntimeId)
        return {}

    def get_agent_runtime(self, agentRuntimeId):
        self.calls.append(("get", agentRuntimeId))
        status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
        if status is None:
            raise self.exceptions.ResourceNotFoundException(agentRuntimeId)
        return {"agentRuntimeId": agentRuntimeId, "status": status}


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


class _DeniedListIam(_Iam):
    """IAM as the hub's EC2 ops role saw it live (N10v2-bug8): ListRolePolicies is
    denied, and DeleteRole answers DeleteConflict while an inline policy remains."""

    def __init__(self, roles, attached: list[str]):
        super().__init__(roles)
        self.attached = list(attached)
        self.policy_deletes: list[str] = []

    def list_role_policies(self, RoleName):
        raise RuntimeError("AccessDenied: iam:ListRolePolicies on resource: role " + RoleName)

    def delete_role_policy(self, RoleName, PolicyName):
        self.policy_deletes.append(PolicyName)
        if PolicyName not in self.attached:
            raise type("NoSuchEntityException", (Exception,), {})(PolicyName)
        self.attached.remove(PolicyName)
        return {}

    def delete_role(self, RoleName):
        if self.attached:
            raise RuntimeError("DeleteConflict: Cannot delete entity, must delete policies first")
        return super().delete_role(RoleName)


def _owned_role(agent_id: str) -> dict:
    name = agent_iam.role_name_for(ARCHITECT.name, agent_id)
    return {name: {"Arn": f"arn:aws-cn:iam::111122223333:role/{name}",
                   "Tags": [{"Key": agent_iam.MANAGED_TAG_KEY, "Value": agent_id}]}}


def _strict_stubs(monkeypatch, control=None, role_ok=True, iam=None, agent_id=None):
    """Low-level teardown stubs: the real worker and real wrappers run; only the AWS
    clients are fakes. The lock directory is a fresh temp dir per test."""
    import tempfile

    control = control or _RuntimeControl()
    monkeypatch.setattr(zip_runtime, "control_client", lambda _ws: control)
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
    _set_status(agent_id, "active", "arn:aws-cn:bedrock-agentcore:cn-northwest-1:1:runtime/h1")
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
    # DeleteAgentRuntime accepted → polled until ResourceNotFound → role deleted
    assert control.calls[0] == ("delete", "h1") and control.calls[-1] == ("get", "h1")
    assert role_calls == [agent_iam.role_name_for(ARCHITECT.name, agent_id)]
    assert _agent(agent_id).status == "deleted"
    job = _job(body["job_id"])
    assert job.status == "succeeded" and job.payload["aws_resource_deleted"] is True
    assert {k: v["state"] for k, v in job.payload["progress"].items()} == {
        "kb_target": "done", "harness": "done", "role": "done", "registry": "done"}
    assert job.payload["progress"]["harness"]["resource_id"] == "h1"
    assert job.payload["progress"]["kb_target"]["detail"] == "skipped: not managed in this build"
    assert _status(client)["status"] == "not_installed"
    assert client.post(INSTALL, json={}).json()["agent"]["id"] != agent_id


def _preset_registry_records(agent_id: str):
    """A local registry holding what an installed preset leaves there: its A2A record
    (linked from the agent row) and its protected AGENT_SKILLS record (mapping row)."""
    from app.models.ledger import SystemSkillRecord
    from app.services import registry_backend as reg
    from app.services.registry_local import LocalRegistryBackend, registry_id_for
    from app.services.workspace import context_for_workspace

    backend = LocalRegistryBackend()
    reg.set_backend(backend)
    ws = context_for_workspace(DEFAULT_WORKSPACE_ID)
    card = reg.build_a2a_card(
        name=ARCHITECT.name, description="d",
        arn="arn:aws-cn:bedrock-agentcore:cn-northwest-1:1:runtime/h1", method="harness",
        workspace=ws,
    )
    a2a, _ = backend.upsert_record(
        ws, name=ARCHITECT.name, description="Launchpad agent", descriptor_type="A2A",
        descriptors=reg.build_a2a_descriptors(card),
    )
    skill, _ = backend.upsert_record(
        ws, name=ARCHITECT.name, description="methodology", descriptor_type="AGENT_SKILLS",
        descriptors=reg.build_skills_descriptors(
            skill_md="# skill", definition={"name": ARCHITECT.name, "version": "1.5.9",
                                            "files": []},
        ),
    )
    db = SessionLocal()
    db.get(Agent, agent_id).registry_record_id = a2a["recordId"]
    db.add(SystemSkillRecord(
        workspace_id=DEFAULT_WORKSPACE_ID, preset_key=KEY, skill_name=ARCHITECT.name,
        registry_id=registry_id_for(ws), record_id=skill["recordId"], client_token="tok",
        status="registered", release_version="1.5.9",
    ))
    db.commit()
    db.close()
    return backend, ws, a2a["recordId"], skill["recordId"]


def _record_exists(backend, ws, record_id: str) -> bool:
    try:
        backend.get_record(ws, record_id)
    except Exception as exc:  # noqa: BLE001
        assert getattr(exc, "status_code", None) == 404, exc
        return False
    return True


def _mapping_rows() -> int:
    from app.models.ledger import SystemSkillRecord

    db = SessionLocal()
    try:
        return db.query(SystemSkillRecord).filter(SystemSkillRecord.preset_key == KEY).count()
    finally:
        db.close()


def test_uninstall_deletes_the_presets_registry_records(client, monkeypatch):
    """N10v3-bug6 / N11-r4-bug1: the uninstall removed runtime + role but left the A2A
    record and the protected AGENT_SKILLS record (403 for every ordinary delete). The
    `registry` step now deletes both and the mapping row; `skill_registration` reads
    back empty."""
    agent_id = _active_preset(client)
    backend, ws, a2a_id, skill_id = _preset_registry_records(agent_id)
    _strict_stubs(monkeypatch, agent_id=agent_id)
    # the protected record is refused on the ordinary path right up to the uninstall
    assert client.delete(f"/api/registry/records/{skill_id}").status_code == 403
    assert _mapping_rows() == 1

    body = client.delete(f"/api/system-agents/{KEY}").json()
    uninstall_module.execute_uninstall_job(body["job_id"])

    job = _job(body["job_id"])
    assert job.status == "succeeded", job.error
    progress = job.payload["progress"]
    assert progress["registry"]["state"] == "done"
    assert progress["registry"]["resource_id"] == a2a_id
    assert a2a_id in progress["registry"]["detail"] and skill_id in progress["registry"]["detail"]
    assert job.payload["teardown"] == {
        "removed": ["harness", "role", "registry"], "skipped": ["kb_target"], "failed": []}
    assert not _record_exists(backend, ws, a2a_id)
    assert not _record_exists(backend, ws, skill_id)
    assert _mapping_rows() == 0
    assert _agent(agent_id).status == "deleted"
    status = _status(client)
    assert status["status"] == "not_installed" and status["skill_registration"] is None


def test_uninstall_registry_step_is_idempotent_and_retryable(client, monkeypatch):
    """Records already gone → the step still completes; a registry error fails the job at
    `registry` with runtime + role carried as done, and the retry finishes the teardown."""
    agent_id = _active_preset(client)
    backend, ws, a2a_id, skill_id = _preset_registry_records(agent_id)
    control, _roles = _strict_stubs(monkeypatch, agent_id=agent_id)
    backend.delete_record(ws, a2a_id)  # someone removed the A2A record out of band
    real_delete = backend.delete_record
    calls = {"n": 0}

    def flaky_delete(workspace, record_id):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("ThrottlingException: slow down")
        return real_delete(workspace, record_id)

    monkeypatch.setattr(backend, "delete_record", flaky_delete)

    body = client.delete(f"/api/system-agents/{KEY}").json()
    uninstall_module.execute_uninstall_job(body["job_id"])
    job = _job(body["job_id"])
    assert job.status == "failed" and "registry: RuntimeError: Throttling" in job.error
    assert job.payload["progress"]["harness"]["state"] == "done"
    assert job.payload["progress"]["role"]["state"] == "done"
    assert job.payload["progress"]["registry"]["state"] == "failed"
    assert job.payload["teardown"]["failed"] == ["registry"]
    assert _agent(agent_id).status == "uninstalling"
    assert _record_exists(backend, ws, skill_id) and _mapping_rows() == 1
    op = _status(client)["operation"]
    assert op["retryable"] is True and op["failed"] == ["registry"]

    runtime_calls = len(control.calls)
    retry = client.delete(f"/api/system-agents/{KEY}").json()
    assert retry["attempt"] == 2 and retry["job_id"] != body["job_id"]
    uninstall_module.execute_uninstall_job(retry["job_id"])
    job2 = _job(retry["job_id"])
    assert job2.status == "succeeded", job2.error
    assert len(control.calls) == runtime_calls  # runtime + role carried, not re-deleted
    assert "already gone" in job2.payload["progress"]["registry"]["detail"]  # the A2A one
    assert not _record_exists(backend, ws, skill_id) and _mapping_rows() == 0
    assert _agent(agent_id).status == "deleted"


def test_uninstall_without_a_registry_backend_skips_the_registry_step(client, monkeypatch):
    agent_id = _active_preset(client)
    _strict_stubs(monkeypatch, agent_id=agent_id)
    body = client.delete(f"/api/system-agents/{KEY}").json()
    uninstall_module.execute_uninstall_job(body["job_id"])
    job = _job(body["job_id"])
    assert job.status == "succeeded", job.error
    assert job.payload["progress"]["registry"]["detail"].startswith("skipped")
    assert job.payload["teardown"]["skipped"] == ["kb_target", "registry"]


@pytest.mark.parametrize("scenario", ["timeout", "role_false"])
def test_teardown_that_does_not_finish_stays_uninstalling_and_retryable(
    client, monkeypatch, scenario
):
    """Host reproduction: an accepted DeleteAgentRuntime is not a deletion, and an
    ignored IAM False is not a cleanup — neither may mark the row deleted."""
    agent_id = _active_preset(client)
    if scenario == "timeout":
        control, _ = _strict_stubs(monkeypatch, _RuntimeControl(["DELETING"]), agent_id=agent_id)
        monkeypatch.setattr(uninstall_module, "RUNTIME_GONE_TIMEOUT_S", 0)
    else:
        control, _ = _strict_stubs(monkeypatch, role_ok=False, agent_id=agent_id)
    job_id = client.delete(f"/api/system-agents/{KEY}").json()["job_id"]
    uninstall_module.execute_uninstall_job(job_id)
    row = _agent(agent_id)
    assert row.status == "uninstalling" and row.error.startswith("uninstall failed")
    job = _job(job_id)
    assert job.status == "failed"
    progress = job.payload["progress"]
    if scenario == "role_false":
        assert progress["harness"]["state"] == "done" and progress["role"]["state"] == "failed"
    else:
        assert progress["harness"]["state"] == "failed" and "role" not in progress
    status = _status(client)
    assert status["status"] == "uninstalling" and status["operation"]["retryable"] is True
    assert status["can_uninstall"] is True and status["can_repair"] is False
    # N10v2-bug8: the failed job says what it did remove and where it stopped
    if scenario == "role_false":
        assert status["operation"]["removed"] == ["harness"]
        assert status["operation"]["failed"] == ["role"]
    else:
        assert status["operation"]["removed"] == []
        assert status["operation"]["failed"] == ["harness"]
    assert status["operation"]["skipped"] == ["kb_target"]
    assert job.payload["teardown"]["failed"] == status["operation"]["failed"]
    assert client.post(INSTALL, json={}).json()["code"] == "system_agent.uninstalling"
    # the retry carries verified steps forward and finishes cleanly
    monkeypatch.setattr(uninstall_module, "RUNTIME_GONE_TIMEOUT_S", 90)
    control2, _ = _strict_stubs(monkeypatch, _RuntimeControl(missing=True), agent_id=agent_id)
    retry = client.delete(f"/api/system-agents/{KEY}").json()
    assert retry["started"] is True and retry["attempt"] == 2
    carried = _job(retry["job_id"]).payload.get("progress", {})
    assert all(v["state"] in ("done", "pinned") for v in carried.values())
    uninstall_module.execute_uninstall_job(retry["job_id"])
    assert _agent(agent_id).status == "deleted" and _job(retry["job_id"]).status == "succeeded"
    if scenario == "role_false":
        assert ("delete", "h1") not in control2.calls  # harness step carried (same id)


def test_uninstall_survives_a_denied_policy_listing_by_deleting_the_known_names(
    client, monkeypatch
):
    """N10v2-bug8 (live 2026-10-10): `iam:ListRolePolicies` denied → DeleteConflict →
    the architect preset stuck in `uninstalling`, its role (inline policy
    launchpad-caps-…) leaked. The two platform policy names are deterministic, so a
    denied listing falls back to them and the uninstall completes."""
    agent_id = _active_preset(client)
    caps = agent_iam.capability_policy_name(ARCHITECT.name)
    iam = _DeniedListIam(_owned_role(agent_id), attached=[caps])
    _control, role_calls = _strict_stubs(monkeypatch, iam=iam, agent_id=agent_id)
    job_id = client.delete(f"/api/system-agents/{KEY}").json()["job_id"]
    uninstall_module.execute_uninstall_job(job_id)

    assert caps in iam.policy_deletes
    assert agent_iam.fs_policy_name(ARCHITECT.name) in iam.policy_deletes  # absent → ignored
    assert role_calls == [agent_iam.role_name_for(ARCHITECT.name, agent_id)]
    assert _agent(agent_id).status == "deleted"
    job = _job(job_id)
    assert job.status == "succeeded"
    assert job.payload["teardown"] == {
        "removed": ["harness", "role"], "skipped": ["kb_target", "registry"], "failed": []}
    assert _status(client)["status"] == "not_installed"


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
    _set_status(agent_id2, "active", "arn:aws-cn:bedrock-agentcore:cn-northwest-1:1:runtime/h2")
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
    down and reinstalled — it must not touch the new install's resources."""
    agent_id = _active_preset(client)
    control, role_calls = _strict_stubs(monkeypatch, agent_id=agent_id)
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
            res = client.post(INSTALL, json={})
            assert res.status_code == 202 and res.json()["agent"]["id"] != agent_id
            paused["new"] = res.json()["agent"]["id"]
        return real_fence(db_, jid)

    monkeypatch.setattr(uninstall_module, "_fence", fence_then_replace)
    uninstall_module.execute_uninstall_job(job_id)
    assert control.calls == [] and role_calls == []
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
    # crash after DeleteAgentRuntime was accepted: the job is left `running`; the resume
    # worker adopts it, finds the runtime already gone and completes idempotently
    db = SessionLocal()
    db.get(Job, job_id).status = "running"
    db.commit()
    db.close()
    _strict_stubs(monkeypatch, _RuntimeControl(missing=True), agent_id=agent_id)
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
    _set_status(agent_id, "active", "arn:aws-cn:bedrock-agentcore:cn-northwest-1:1:runtime/h1")
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
    ("valid", [f"s3://{BUCKET}/system-skills/{ARCHITECT.name}/{VER}/"], "succeeded"),  # legacy dir
    ("valid", [f"s3://other-bucket/system-skills/{ARCHITECT.name}/{VER}-{DIGEST12}/"], "skipped"),
    ("valid", [f"s3://{BUCKET}/system-skills/other-preset/{VER}-{DIGEST12}/"], "succeeded"),
    ("valid", [f"s3://{BUCKET}/system-skills/{ARCHITECT.name}/{VER}-{DIGEST12}/",
               f"s3://{BUCKET}/skills/member-skill/"], "succeeded"),  # extra skill
    ("valid", [f"s3://{BUCKET}/agent-skills/x/{ARCHITECT.name}/{VER}-{DIGEST12}/"], "skipped"),
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
    monkeypatch.setattr(zip_runtime, "control_client", lambda _ws: control)
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
    assert verdict["expected_uri"] == f"s3://{BUCKET}/system-skills/{ARCHITECT.name}/{VER}-{DIGEST12}/"
    with pytest.raises(RuntimeError, match="artifacts_bucket missing"):
        service.assert_job_release_pinned(_job(ctx.job_id).payload, agent,
                                          ws_ctx({"execution_role_arn": "x"}))


# ---- (4) release-prefix integrity ----------------------------------------------------


def test_valid_superset_snapshot_cannot_add_bytes_to_the_winners_release_directory(
    monkeypatch, tmp_path
):
    """Two VALID snapshots of the same version (B = A + one reference file) publish to two
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


# ---- (5) KB-mode least privilege: reworked above as test_preset_kb_mode_grants_the_
# direct_kb_tool_statements_no_oauth / test_preset_kb_mode_requires_a_registered_
# knowledge_backend (Step 0 — the KB-gateway OAuth path has no CN implementation) ----


def test_ensure_role_applies_the_preset_policy_for_system_rows():
    captured: dict = {}

    class _Iam:
        def create_role(self, **kw):
            return {"Role": {"Arn": f"arn:aws-cn:iam::111122223333:role/{kw['RoleName']}"}}

        def put_role_policy(self, **kw):
            captured[kw["PolicyName"]] = json.loads(kw["PolicyDocument"])

        def delete_role_policy(self, **kw):
            pass

    spec = presets.build_spec(ARCHITECT, BUCKET, InstallOptions(
        knowledge_bases=(presets.KnowledgeBaseRef(**KB_REF),)))
    agent = Agent(id="c" * 32, name=ARCHITECT.name, method="harness", system_key=KEY,
                  spec=spec.model_dump())
    agent_iam.ensure_role(_Iam(), agent, spec, agent_iam.role_context(_rt_ctx()))
    sids = {s["Sid"] for s in captured[f"launchpad-caps-{ARCHITECT.name}"]["Statement"]}
    assert "AgentCoreWorkloadIdentity" in sids and "ManagedKbRetrieval" not in sids


# ---- pass 5: exclusive resume, historical role ownership (kb_target has no CN
# implementation and is always skipped without any AWS call — see uninstall._STEPS)


def test_two_resume_workers_only_one_runs_and_a_live_holder_blocks_adoption(client, monkeypatch):
    agent_id = _active_preset(client)
    control, role_calls = _strict_stubs(monkeypatch, agent_id=agent_id)
    job_id = client.delete(f"/api/system-agents/{KEY}").json()["job_id"]
    db = SessionLocal()
    db.get(Job, job_id).status = "running"  # a crashed predecessor left it running
    db.commit()
    db.close()
    barrier = threading.Barrier(2, timeout=10)
    real_delete = control.delete_agent_runtime

    def slow_delete(agentRuntimeId):
        try:
            barrier.wait()  # hold the winner inside its first cloud step
        except threading.BrokenBarrierError:
            pass
        return real_delete(agentRuntimeId)

    control.delete_agent_runtime = slow_delete
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
    _set_status(agent_id2, "active", "arn:aws-cn:bedrock-agentcore:cn-northwest-1:1:runtime/h2")
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
    foreign = _Iam({name: {"Arn": f"arn:aws-cn:iam::111122223333:role/{name}",
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
    _set_status(agent_id, "uninstalling",
               "arn:aws-cn:bedrock-agentcore:cn-northwest-1:1:runtime/h9")
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
    _set_status(agent_id2, "active", "arn:aws-cn:bedrock-agentcore:cn-northwest-1:1:runtime/h2")
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

KIMI = "global.moonshotai.kimi-k3"  # the only other allowed model id (spec C3)


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


def test_architect_defaults_are_glm_64k_output_no_reasoning_effort():
    """R14: the preset stays on the CN catalog default (GLM 5.3) — no OpenAI model
    is in ALLOWED_MODEL_IDS, so reasoning_effort is never set."""
    spec = presets.build_spec(ARCHITECT, BUCKET, ARCHITECT.default_options())
    assert (spec.model_id, spec.model_source) == (DEFAULT_MODEL_ID, "bedrock")
    assert spec.max_tokens == 65536 and spec.reasoning_effort is None
    # the loop limits are separate knobs
    assert (spec.max_iterations, spec.timeout_seconds) == (100, 900)
    bundle = harness_local.render_harness_bundle(spec, _rt_ctx())
    load = bundle.files["model/load.py"]
    assert f'MODEL_ID = "{DEFAULT_MODEL_ID}"' in load and "MODEL_MAX_TOKENS = 65536" in load
    # a bare InstallOptions() is the catalogue default too (model/prompt/limits)
    bare = presets.build_spec(ARCHITECT, BUCKET, InstallOptions())
    assert (bare.model_id, bare.system_prompt) == (DEFAULT_MODEL_ID, ARCHITECT.system_prompt)
    # retention clarification: memory-disabled ≠ nothing retained
    assert "never tell a customer that nothing is retained" in spec.system_prompt
    assert "CloudWatch" in spec.system_prompt


@pytest.mark.parametrize("over", [
    {"reasoning_effort": "high"},  # no OpenAI model in ALLOWED_MODEL_IDS: always refused
    {"model_id": KIMI, "reasoning_effort": "high"},
    {"model_id": DEFAULT_MODEL_ID, "method": "zip_runtime", "reasoning_effort": "high"},
    {"method": "zip_runtime", "max_tokens": 4096},
    {"max_tokens": 0},
    {"max_tokens": presets.AgentSpec.model_fields["max_tokens"].metadata[1].le + 1},
    {"model_id": "global.anthropic.claude-opus-5"},  # not in ALLOWED_MODEL_IDS
])
def test_inference_knobs_reject_unsupported_pairings(over):
    base = {"name": "knobs", "method": "harness", "system_prompt": "hi"}
    with pytest.raises(ValueError):
        AgentSpec(**{**base, **over})


def test_inference_knobs_accept_max_tokens_on_either_allowed_model():
    # reasoning_effort can never be set in this CN build (no OpenAI model id allowed)
    for model_id in (DEFAULT_MODEL_ID, KIMI):
        spec = AgentSpec(name="knobs", method="harness", system_prompt="hi",
                         model_id=model_id, max_tokens=8192)
        assert spec.max_tokens == 8192 and spec.reasoning_effort is None


def test_execution_role_authorizes_the_global_bedrock_api_key_exchange():
    spec = presets.build_spec(ARCHITECT, BUCKET, ARCHITECT.default_options())
    ctx = agent_iam.role_context(_rt_ctx())
    doc = agent_iam.policy_document(spec, ctx)
    api_key = next(s for s in doc["Statement"] if s["Sid"] == "ModelCredentialApiKey")
    # N-R6: the grant covers the `launchpad-*` provider family (Bedrock, SiliconFlow, …)
    assert spec.model_credential.startswith("launchpad-")
    assert ctx.arn("bedrock-agentcore", "token-vault/default/apikeycredentialprovider/"
                  "launchpad-*") in api_key["Resource"]
    # the preset still deploys on its own role, never the shared one
    settings = get_settings()
    with pytest.raises(RuntimeError):
        service.require_dedicated_role(
            Agent(name=ARCHITECT.name, method="harness", system_key=KEY),
            READY_RESOURCES["execution_role_arn"], _rt_ctx(), settings,
        )


def test_status_exposes_stored_settings_and_build_defaults(client):
    _mark_ready()
    before = _status(client)
    assert before["settings"] == {} and before["defaults"]["model_id"] == DEFAULT_MODEL_ID
    assert before["defaults"]["max_tokens"] == 65536
    assert before["defaults"]["reasoning_effort"] is None
    assert before["editable_fields"] == list(presets.EDITABLE_FIELDS)
    assert before["can_configure"] is False  # nothing to configure yet
    agent_id = client.post(INSTALL, json={}).json()["agent"]["id"]
    _set_status(agent_id, "active", "arn:aws-cn:bedrock-agentcore:cn-northwest-1:1:runtime/h1")
    status = _status(client)
    assert status["settings"] == {**status["defaults"], "knowledge_bases": []}
    assert status["settings"]["system_prompt"] == ARCHITECT.system_prompt
    assert status["can_configure"] is True
    assert status["model_id"] == DEFAULT_MODEL_ID  # legacy top-level members still agree


def test_new_install_sends_64k_and_a_resume_regenerates_them(client):
    _mark_ready()
    res = client.post(INSTALL, json={})
    assert res.status_code == 202
    spec = AgentSpec(**res.json()["agent"]["spec"])
    bundle = harness_local.render_harness_bundle(spec, _rt_ctx())
    assert "MODEL_MAX_TOKENS = 65536" in bundle.files["model/load.py"]
    # the ledger round-trip is lossless: a resumed job rebuilds the same request
    stored = AgentSpec(**_spec_of(res.json()["agent"]["id"]))
    again = harness_local.render_harness_bundle(stored, _rt_ctx())
    assert again.files["model/load.py"] == bundle.files["model/load.py"]


def test_old_spec_is_untouched_until_an_explicit_edit_and_partial_edits_preserve(client):
    _mark_ready()
    agent_id = client.post(INSTALL, json={}).json()["agent"]["id"]
    # a row an earlier build wrote: Sonnet, no knobs, an older prompt
    old_prompt = "You are the previous build's architect prompt."
    _rewrite_spec(agent_id, model_id=DEFAULT_MODEL_ID, max_tokens=None,
                  reasoning_effort=None, system_prompt=old_prompt)
    _set_status(agent_id, "active", "arn:aws-cn:bedrock-agentcore:cn-northwest-1:1:runtime/h1")
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
    assert restored["model_id"] == DEFAULT_MODEL_ID and restored["reasoning_effort"] is None
    assert restored["system_prompt"] == ARCHITECT.system_prompt
    assert restored["max_tokens"] == 65536 and restored["max_iterations"] == 12
    # the protected part of the spec never moved
    for key in ("name", "method", "tools", "allowed_tools", "memory", "skills", "env"):
        assert restored[key] == legacy[key], key


def test_stored_prompt_override_survives_repair_kb_edit_and_bundle_pin(client, monkeypatch):
    from app.services import knowledge_backend

    monkeypatch.setattr(knowledge_backend, "_backend", object())
    _mark_ready()
    agent_id = client.post(INSTALL, json={}).json()["agent"]["id"]
    _set_status(agent_id, "active", "arn:aws-cn:bedrock-agentcore:cn-northwest-1:1:runtime/h1")
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
        assert spec["model_id"] == DEFAULT_MODEL_ID and spec["max_tokens"] == 65536, body
        # every repair still pins the content-addressed release and passes the job-entry guard
        job = _job(res.json()["job_id"])
        agent = _agent(agent_id)
        service.assert_job_release_pinned(job.payload, agent, _rt_ctx())
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
    {"model_id": "global.anthropic.claude-opus-5"},  # not in ALLOWED_MODEL_IDS
    {"model_id": "openai.gpt-5.6-sol", "model_source": "mantle"},
])
def test_invalid_edits_fail_before_any_job_or_spec_change(client, body):
    _mark_ready()
    agent_id = client.post(INSTALL, json={}).json()["agent"]["id"]
    _set_status(agent_id, "active", "arn:aws-cn:bedrock-agentcore:cn-northwest-1:1:runtime/h1")
    before, jobs = _spec_of(agent_id), _job_count()
    res = client.post(INSTALL, json=body)
    assert res.status_code == 422, (body, res.text)
    assert res.json()["code"] in ("validation.invalid_request", "system_agent.invalid_options")
    assert _spec_of(agent_id) == before and _job_count() == jobs
    assert _status(client)["status"] == "active"


def test_switching_between_the_two_allowed_models_keeps_other_settings(client):
    _mark_ready()
    agent_id = client.post(INSTALL, json={}).json()["agent"]["id"]
    _set_status(agent_id, "active", "arn:aws-cn:bedrock-agentcore:cn-northwest-1:1:runtime/h1")
    res = client.post(INSTALL, json={"model_id": KIMI})
    assert res.status_code == 202, res.text
    spec = _spec_of(agent_id)
    assert spec["model_id"] == KIMI and spec["reasoning_effort"] is None
    assert spec["max_tokens"] == 65536  # unrelated members untouched
    # clearing max_tokens sends no ceiling at all (the provider default applies again)
    _set_status(agent_id, "active")
    cleared = client.post(INSTALL, json={"clear": ["max_tokens"]})
    assert cleared.status_code == 202 and _spec_of(agent_id)["max_tokens"] is None
    _set_status(agent_id, "active")
    spec = _spec_of(agent_id)
    bundle = harness_local.render_harness_bundle(AgentSpec(**spec), _rt_ctx())
    assert f'MODEL_ID = "{KIMI}"' in bundle.files["model/load.py"]
    # the model-aware platform default (N10-bug4: Kimi/GLM → 16384), not a flat 4096
    assert "MODEL_MAX_TOKENS = 16384" in bundle.files["model/load.py"]
    # resetting the model returns to the catalogue default
    _set_status(agent_id, "active")
    reset = client.post(INSTALL, json={"reset": ["model_id"]})
    assert reset.status_code == 202 and _spec_of(agent_id)["model_id"] == DEFAULT_MODEL_ID


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
    _set_status(agent_id, "active", "arn:aws-cn:bedrock-agentcore:cn-northwest-1:1:runtime/h1")
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
    _set_status(agent_id, "active", "arn:aws-cn:bedrock-agentcore:cn-northwest-1:1:runtime/h1")
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
    _set_status(agent_id, "active", "arn:aws-cn:bedrock-agentcore:cn-northwest-1:1:runtime/h1")
    perms = member.get("/api/auth/status").json()["permissions"]
    assert "agents.deploy" in perms  # the member holds the ordinary deploy right
    seen = _status(member)
    assert seen["settings"]["model_id"] == DEFAULT_MODEL_ID
    assert seen["settings"]["max_tokens"] == 65536
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
    _set_status(agent_id, "active", "arn:aws-cn:bedrock-agentcore:cn-northwest-1:1:runtime/h1")
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
    _set_status(agent_id, "active", "arn:aws-cn:bedrock-agentcore:cn-northwest-1:1:runtime/h1")
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
    _set_status(agent_id, "active", "arn:aws-cn:bedrock-agentcore:cn-northwest-1:1:runtime/h1")
    results = _race_two(client, [{"max_tokens": 1111}, {"max_tokens": 2222}], monkeypatch)
    assert sorted(r.status_code for r in results) == [202, 409], [r.text for r in results]
    winner = next(r for r in results if r.status_code == 202)
    loser = next(r for r in results if r.status_code == 409)
    assert loser.json()["detail"]["job_id"] == winner.json()["job_id"]
    assert _spec_of(agent_id)["max_tokens"] == winner.json()["agent"]["spec"]["max_tokens"]
    assert _job_count() == 2


# ── D21 (M1-d): preset snapshot catalog version + the Settings warning ──
@pytest.mark.siliconflow
def test_preset_status_exposes_the_catalog_version_and_the_behind_warning(client):
    from app.models.ledger import Deployment
    from app.schemas.agent import catalog_version
    from app.services import model_registry

    _mark_ready()
    agent_id = client.post(INSTALL, json={}).json()["agent"]["id"]
    _set_status(agent_id, "active", RUNTIME_ARN)
    db = SessionLocal()
    try:
        current = model_registry.stamp(db)
        st = _status(client)
        # the deploy job is stubbed in this suite (no generate stage ran) → no pin yet, and
        # a row without a pin never raises the banner
        assert st["catalog_version"] == {"installed": None, "current": catalog_version()}
        assert service.catalog_snapshot_warnings(db) == []
        dep = (db.query(Deployment).filter(Deployment.agent_id == agent_id)
               .order_by(Deployment.started_at.desc()).first())
        dep.catalog_version = current  # what the generate stage writes (test_catalog_hint)
        db.commit()
        st = _status(client)
        assert st["catalog_version"] == {"installed": current, "current": catalog_version()}
        assert service.catalog_snapshot_warnings(db) == []
        # D21-obs5: a credential registration (what every bootstrap's `model_providers`
        # stage used to journal) is audit-only — it must not make the preset look behind
        from datetime import UTC, datetime

        from app.models.model_registry import ModelProvider
        from app.services import model_credential

        sf = db.query(ModelProvider).filter_by(name="siliconflow").one()
        model_credential.record_registered_key(
            db, sf, version=2, modified_at=datetime(2026, 10, 14, 12, 0, tzinfo=UTC),
            created=False, rotated=True, actor="bootstrap")
        assert model_registry.list_changes(db, limit=1)[0].operation == "provider.credential"
        assert model_registry.stamp(db) == current == catalog_version()
        assert service.catalog_snapshot_warnings(db) == []
        assert _status(client)["catalog_version"] == {"installed": current,
                                                      "current": current}
        # D21-obs6: provider / model probe rows (what two admin Probe clicks journal) are
        # audit-only as well — the preset must not look behind after them
        from app.models.model_registry import Model

        kimi = db.query(Model).filter_by(model_id="global.moonshotai.kimi-k3").one()
        model_registry._journal(db, "provider.probe", "admin", sf.id,
                                {"probe": {"state": "never"}}, {"probe": {"state": "ok"}})
        model_registry._journal(db, "model.probe", "admin", kimi.id,
                                {"tools": True}, {"tools": True})
        db.commit()
        assert [c.operation for c in model_registry.list_changes(db, limit=2)] == [
            "model.probe", "provider.probe"]
        assert model_registry.stamp(db) == current == catalog_version()
        assert service.catalog_snapshot_warnings(db) == []
        assert _status(client)["catalog_version"] == {"installed": current,
                                                      "current": current}
        dep.catalog_version = current - 1
        db.commit()
        st = _status(client)
        assert st["catalog_version"]["installed"] == current - 1
        [warning] = service.catalog_snapshot_warnings(db)
        assert warning["code"] == "preset_snapshot_behind"
        assert warning["detail"]["installed"] == current - 1
        assert warning["detail"]["current"] == current and warning["detail"]["preset_key"] == KEY
        assert f"v{current - 1}" in warning["message"] and f"v{current}" in warning["message"]
        # N17-obs208: the message names the real console path (there is no "System agents" page)
        assert warning["message"].endswith(
            "open New agent → System presets and repair the snapshot")
        assert "System agents" not in warning["message"]
        # the producer is what `model_registry.warnings` appends (installed by create_app)
        model_registry.add_warning_producer(service.catalog_snapshot_warnings)
        codes = [w["code"] for w in model_registry.warnings(db)]
        assert codes.count("preset_snapshot_behind") == 1
        # a not-installed preset projects None / current
        rows = {p["key"]: p for p in client.get("/api/system-agents").json()["presets"]}
        others = [p for k, p in rows.items() if k != KEY]
        assert all(p["catalog_version"] == {"installed": None, "current": catalog_version()}
                   for p in others)
    finally:
        db.close()


def test_status_current_and_catalog_behind_read_one_source_in_a_stale_process(
    client, no_real_deploy
):
    """N17-obs281 (design simplification C1): `catalog_version.current` on the status read and
    the `(installed, current)` of `catalog_behind` (banner, `snapshot_behind`, the repair
    decision) come from ONE source, `service.current_catalog_version` (the ledger stamp). Before,
    `current` was the in-process snapshot, so a process whose snapshot lagged the ledger (a
    second process wrote; the 30 s refresher not run yet) answered "current v<N-1>" next to
    `snapshot_behind: true` — model-registry.md §9 says the behind signals never disagree."""
    from app.models.ledger import Deployment
    from app.schemas import model_catalog
    from app.schemas.agent import catalog_version
    from app.services import model_registry

    _mark_ready()
    agent_id = client.post(INSTALL, json={}).json()["agent"]["id"]
    _set_status(agent_id, "active", RUNTIME_ARN)
    db = SessionLocal()
    try:
        current = model_registry.stamp(db)
        dep = (db.query(Deployment).filter(Deployment.agent_id == agent_id)
               .order_by(Deployment.started_at.desc(), Deployment.id.desc()).first())
        dep.catalog_version = current - 1
        db.commit()
        # this process's snapshot lags the ledger by one version (the second-process shape)
        fresh = model_catalog.current()
        model_catalog.publish(model_catalog.Catalog(
            version=current - 1, providers=fresh.providers, models=fresh.models,
            workspace_disabled=fresh.workspace_disabled, revision=fresh.revision - 1))
        assert int(catalog_version()) == current - 1
        st = _status(client)
        assert st["catalog_version"] == {"installed": current - 1, "current": current}
        assert st["snapshot_behind"] is True
        assert service.catalog_behind(db, db.get(Agent, agent_id)) == (current - 1, current)
        assert service.current_catalog_version(db) == current
        [warning] = service.catalog_snapshot_warnings(db)
        assert warning["detail"]["current"] == st["catalog_version"]["current"]
    finally:
        model_registry.refresh(db)
        db.close()


def test_repair_republishes_when_the_pinned_catalog_is_behind_the_stamp(
    client, no_real_deploy
):
    """N17-obs234: a bodiless install / repair of an ACTIVE preset whose latest deployment
    pinned an OLDER model catalog than the ledger's is a re-publish (202, job, `changed`),
    not the "already current" no-op — no `force` needed; `snapshot_behind` on the status
    read is the same verdict. An equal or newer pin with an unchanged spec stays a no-op."""
    from app.models.ledger import Deployment
    from app.services import model_registry

    def latest_deployment(agent_id: str) -> Deployment:
        return (db.query(Deployment).filter(Deployment.agent_id == agent_id)
                .order_by(Deployment.started_at.desc(), Deployment.id.desc()).first())

    _mark_ready()
    first = client.post(INSTALL, json={}).json()
    agent_id, first_job = first["agent"]["id"], first["job_id"]
    _set_status(agent_id, "active", RUNTIME_ARN)
    db = SessionLocal()
    try:
        current = model_registry.stamp(db)
        # no pin yet (the stubbed job ran no generate stage): never behind, a no-op
        assert _status(client)["snapshot_behind"] is False
        same = client.post(INSTALL, json={})
        assert same.status_code == 200 and same.json()["changed"] is False
        assert same.json()["preset"]["snapshot_behind"] is False and _job_count() == 1

        # pinned at the current stamp → equal → still a no-op
        dep = latest_deployment(agent_id)
        dep.catalog_version = current
        db.commit()
        assert _status(client)["snapshot_behind"] is False
        assert client.post(INSTALL, json={}).status_code == 200 and _job_count() == 1

        # a NEWER pin (another process published) is not behind either
        dep.catalog_version = current + 1
        db.commit()
        assert _status(client)["snapshot_behind"] is False
        assert client.post(INSTALL, json={}).status_code == 200 and _job_count() == 1

        # behind: the live default-workspace shape (installed v10, stamp v20)
        dep.catalog_version = current - 1
        db.commit()
        st = _status(client)
        assert st["snapshot_behind"] is True and st["status"] == "active"
        assert st["catalog_version"]["installed"] == current - 1
        assert [w["detail"]["agent_id"] for w in service.catalog_snapshot_warnings(db)] == [
            agent_id]
        repaired = client.post(INSTALL, json={})  # the card's "Repair snapshot": no force
        assert repaired.status_code == 202, repaired.text
        body = repaired.json()
        assert body["changed"] is True and body["created"] is False
        assert body["job_id"] not in (None, first_job) and _job_count() == 2
        assert body["job_id"] in no_real_deploy  # the claim winner launched it
        assert body["preset"]["status"] == "deploying"
        job = db.get(Job, body["job_id"])
        reason = f"catalog snapshot behind v{current - 1} < v{current}"
        assert job.payload["mode"] == "update"
        assert job.payload["repair_reason"] == reason and job.payload["note"] == reason
        assert "preset_bundle" in job.payload  # the release pin still rides along
        db.expire_all()
        assert db.get(Agent, agent_id).status == "deploying"
        # repeated clicks while the repair runs coalesce onto the same job
        again = client.post(INSTALL, json={})
        assert again.status_code == 202 and again.json()["job_id"] == body["job_id"]
        assert again.json()["changed"] is False and _job_count() == 2

        # the repair's own deployment carries no pin yet (stubbed job) → the verdict clears
        _set_status(agent_id, "active", RUNTIME_ARN)
        db.expire_all()
        assert latest_deployment(agent_id).id == body["deployment_id"]
        assert _status(client)["snapshot_behind"] is False
        assert service.catalog_snapshot_warnings(db) == []
        settled = client.post(INSTALL, json={})
        assert settled.status_code == 200 and settled.json()["changed"] is False
        assert _job_count() == 2
    finally:
        db.close()


def test_warning_producers_are_fail_soft(client):
    from app.services import model_registry

    def boom(db):
        raise RuntimeError("ledger locked")

    model_registry.add_warning_producer(boom)
    db = SessionLocal()
    try:
        assert isinstance(model_registry.warnings(db), list)  # no raise
    finally:
        db.close()


# ── N16-obs111a: a canary still naming the preset blocks the uninstall ────────────────────


def test_uninstall_is_409_agent_in_use_while_a_canary_names_the_preset(client):
    """The create path refuses presets as canary arms (`system-managed`), so only a legacy row
    can name one — and it must still block: the uninstall would tear down the Runtime the
    canary routes to (same orphan shape as N16-obs111)."""
    from app.optimization.models import RuntimeCanary

    agent_id = _active_preset(client)
    db = SessionLocal()
    try:
        row = RuntimeCanary(
            name="legacy", workspace_id=DEFAULT_WORKSPACE_ID, status="running", stage="traffic",
            champion_agent_id="someone-else", champion_agent_name="x",
            challenger_agent_id=agent_id, challenger_agent_name="preset",
        )
        db.add(row)
        db.commit()
        canary_id = row.id
    finally:
        db.close()

    res = client.delete(f"/api/system-agents/{KEY}")
    assert res.status_code == 409, res.text
    assert res.json()["code"] == "agent.in_use"
    assert [c["id"] for c in res.json()["detail"]["canaries"]] == [canary_id]
    assert _rows(system_key=KEY)[0].status == "active"  # the claim was never taken
    assert _job_count() == 1  # the install job only — no uninstall job queued

    db = SessionLocal()
    try:
        db.get(RuntimeCanary, canary_id).status = "cleaned"
        db.commit()
    finally:
        db.close()
    assert client.delete(f"/api/system-agents/{KEY}").status_code == 202


# ---------------------------------------------------------------------------
# N17-obs202 (user ruling 2026-10-22): presets FOLLOW the model-defaults page
# ---------------------------------------------------------------------------


def _set_role(name: str, model_id: str | None) -> None:
    """Move (or reset) a platform model role the way the console's Model roles tab does."""
    from app.services import model_roles

    db = SessionLocal()
    try:
        if model_id is None:
            model_roles.clear_override(db, name, "ops")
        else:
            model_roles.set_override(db, name, model_id, "ops")
    finally:
        db.close()


def _job_payload(job_id: str) -> dict:
    db = SessionLocal()
    try:
        return dict(db.get(Job, job_id).payload or {})
    finally:
        db.close()


def test_preset_model_follows_the_agent_default_role_and_the_constant_only_when_unset(
    monkeypatch,
):
    from app.schemas.agent import agent_default_model_id

    # the role is the catalog default today → the resolved model is the constant
    assert ARCHITECT.model_id is None and presets.EVAL_WORKER.model_id is None
    assert ARCHITECT.resolved_model_id() == agent_default_model_id() == DEFAULT_MODEL_ID
    spec = presets.build_spec(ARCHITECT, BUCKET, ARCHITECT.default_options())
    assert spec.model_id == DEFAULT_MODEL_ID
    # the administrator moves the role on the model-defaults page → resolved per call
    _set_role("agent_default", KIMI)
    assert ARCHITECT.resolved_model_id() == KIMI
    assert presets.build_spec(ARCHITECT, BUCKET, InstallOptions()).model_id == KIMI
    assert presets.build_spec(presets.EVAL_WORKER, BUCKET, InstallOptions()).model_id == KIMI
    assert presets.default_settings(ARCHITECT)["model_id"] == KIMI
    # an explicit option still wins over the role
    assert presets.build_spec(ARCHITECT, BUCKET, InstallOptions(model_id=DEFAULT_MODEL_ID)) \
        .model_id == DEFAULT_MODEL_ID
    # a blank role (defensive: Settings refuses one, but the helper never returns "")
    monkeypatch.setattr(get_settings(), "agent_default_model_id", "")
    assert agent_default_model_id() == DEFAULT_MODEL_ID
    assert ARCHITECT.resolved_model_id() == DEFAULT_MODEL_ID


def test_install_takes_the_role_model_and_the_status_says_it_follows_the_default(client):
    _mark_ready()
    _set_role("agent_default", KIMI)
    before = _status(client)
    assert before["defaults"]["model_id"] == KIMI  # what an install would take, live
    assert before["model_origin"] is None and before["default_changed"] is False
    res = client.post(INSTALL, json={})
    assert res.status_code == 202, res.text
    agent_id = res.json()["agent"]["id"]
    assert _spec_of(agent_id)["model_id"] == KIMI
    assert _agent(agent_id).preset_model_pinned is False
    assert res.json()["preset"]["model_origin"] == "default"
    _set_status(agent_id, "active", RUNTIME_ARN)
    status = _status(client)
    assert (status["model_id"], status["model_origin"]) == (KIMI, "default")
    assert status["default_changed"] is False
    assert status["settings"]["model_id"] == KIMI == status["defaults"]["model_id"]


def test_role_change_after_install_flags_default_changed_and_repair_applies_it(client):
    _mark_ready()
    first = client.post(INSTALL, json={}).json()
    agent_id, first_job = first["agent"]["id"], first["job_id"]
    assert _spec_of(agent_id)["model_id"] == DEFAULT_MODEL_ID
    _set_status(agent_id, "active", RUNTIME_ARN)
    # nothing moved: a bodiless repair is a no-op and the card shows no note
    same = client.post(INSTALL, json={})
    assert same.status_code == 200 and same.json()["changed"] is False
    assert _status(client)["default_changed"] is False

    # the administrator moves the agent default on the model-defaults page
    _set_role("agent_default", KIMI)
    status = _status(client)
    assert status["default_changed"] is True and status["model_origin"] == "default"
    assert status["model_id"] == DEFAULT_MODEL_ID  # the installed spec, not the role
    assert status["defaults"]["model_id"] == KIMI  # what a repair applies
    assert status["can_repair"] is True and status["snapshot_behind"] is False

    # the same "Repair" click as N17-obs234: bodiless, the SERVER re-publishes
    repaired = client.post(INSTALL, json={})
    assert repaired.status_code == 202 and repaired.json()["changed"] is True, repaired.text
    assert repaired.json()["job_id"] != first_job and _job_count() == 2
    payload = _job_payload(repaired.json()["job_id"])
    assert payload["mode"] == "update"
    assert payload["repair_reason"] == f"model default changed {DEFAULT_MODEL_ID} -> {KIMI}"
    assert _spec_of(agent_id)["model_id"] == KIMI
    assert _agent(agent_id).preset_model_pinned is False  # still following the role
    assert repaired.json()["preset"]["default_changed"] is False
    _set_status(agent_id, "active")
    after = _status(client)
    assert (after["model_id"], after["model_origin"], after["default_changed"]) == (
        KIMI, "default", False)
    # and the preset stays current until the role moves again
    assert client.post(INSTALL, json={}).status_code == 200
    _set_role("agent_default", None)  # reset to the built-in default
    assert _status(client)["default_changed"] is True
    assert client.post(INSTALL, json={}).status_code == 202
    assert _spec_of(agent_id)["model_id"] == DEFAULT_MODEL_ID


def test_admin_override_wins_and_is_kept_on_repair_when_the_role_moves(client):
    _mark_ready()
    agent_id = client.post(INSTALL, json={}).json()["agent"]["id"]
    _set_status(agent_id, "active", RUNTIME_ARN)
    # the 配置 edit pins the model explicitly
    pinned = client.post(INSTALL, json={"model_id": KIMI})
    assert pinned.status_code == 202 and _spec_of(agent_id)["model_id"] == KIMI
    assert _agent(agent_id).preset_model_pinned is True
    assert pinned.json()["preset"]["model_origin"] == "override"
    _set_status(agent_id, "active")
    status = _status(client)
    assert status["model_origin"] == "override" and status["default_changed"] is False

    # the role moves to the OTHER model: an override never follows and never flags
    _set_role("agent_default", KIMI)
    _set_role("agent_default", None)
    assert _status(client)["default_changed"] is False
    kept = client.post(INSTALL, json={})
    assert kept.status_code == 200 and kept.json()["changed"] is False
    assert kept.json()["preset"]["model_id"] == KIMI
    assert kept.json()["preset"]["model_origin"] == "override"
    # an edit of ANOTHER member keeps the pin too
    other = client.post(INSTALL, json={"max_iterations": 50})
    assert other.status_code == 202 and _spec_of(agent_id)["model_id"] == KIMI
    assert _agent(agent_id).preset_model_pinned is True
    _set_status(agent_id, "active")

    # `reset: ["model_id"]` returns the row to following the role (the role is the
    # constant again now, so the model changes back and the spec is re-published)
    reset = client.post(INSTALL, json={"reset": ["model_id"]})
    assert reset.status_code == 202, reset.text
    assert _spec_of(agent_id)["model_id"] == DEFAULT_MODEL_ID
    assert _agent(agent_id).preset_model_pinned is False
    assert reset.json()["preset"]["model_origin"] == "default"
    _set_status(agent_id, "active")
    _set_role("agent_default", KIMI)
    assert _status(client)["default_changed"] is True  # follows again


def test_legacy_rows_without_the_pin_flag_are_classified_by_the_old_constant(client):
    """A row written before `preset_model_pinned` existed could only be the constant (the
    one default then) or an administrator's explicit other model."""
    _mark_ready()
    agent_id = client.post(INSTALL, json={}).json()["agent"]["id"]
    db = SessionLocal()
    try:
        db.get(Agent, agent_id).preset_model_pinned = None
        db.commit()
    finally:
        db.close()
    _set_status(agent_id, "active", RUNTIME_ARN)
    assert service.model_pinned(_agent(agent_id)) is False
    assert _status(client)["model_origin"] == "default"
    _set_role("agent_default", KIMI)
    assert _status(client)["default_changed"] is True
    _set_role("agent_default", None)
    _rewrite_spec(agent_id, model_id=KIMI)  # the only way a legacy row carried Kimi
    assert service.model_pinned(_agent(agent_id)) is True
    status = _status(client)
    assert status["model_origin"] == "override" and status["default_changed"] is False
    assert client.post(INSTALL, json={}).status_code == 200  # nothing to re-publish


def test_architect_plan_judges_follow_the_judge_role():
    from app.assistant import evaluation_plan as plan
    from app.schemas.agent import judge_default_model_id

    assert plan.default_judge_model() == judge_default_model_id() == DEFAULT_MODEL_ID
    judge_entry = {"kind": "judge", "key": "quality", "title": "Quality judge",
                   "name": "quality_judge",
                   "instructions": "Rate the answer against the rubric."}
    derived_entry = {"kind": "derived", "key": "helpful", "title": "Helpfulness",
                     "name": "helpful", "base_evaluator_id": "Builtin.Helpfulness"}
    judge, derived = plan.JudgeEvaluator(**judge_entry), plan.DerivedEvaluator(**derived_entry)
    assert judge.model_id == derived.model_id == DEFAULT_MODEL_ID
    _set_role("judge", KIMI)  # the model-defaults page, no restart
    assert plan.default_judge_model() == KIMI
    judge, derived = plan.JudgeEvaluator(**judge_entry), plan.DerivedEvaluator(**derived_entry)
    assert judge.model_id == derived.model_id == KIMI
    # an entry naming its own judge keeps it
    explicit = plan.JudgeEvaluator(**judge_entry, model_id=DEFAULT_MODEL_ID)
    assert explicit.model_id == DEFAULT_MODEL_ID


def test_a_process_without_the_role_snapshot_still_resolves_the_ledger_roles(client):
    """rev-r25 F1 on N17-obs202: the bootstrap CLI (`make bootstrap` → `install_preset(
    EVAL_WORKER, None)`) never calls `model_roles.load_overrides`; before the fix it resolved
    `agent_default` to env / built-in and re-published a role-following preset on GLM."""
    from app.services import model_roles

    _mark_ready()
    _set_role("agent_default", KIMI)
    agent_id = client.post(INSTALL, json={}).json()["agent"]["id"]
    assert _spec_of(agent_id)["model_id"] == KIMI
    _set_status(agent_id, "active", RUNTIME_ARN)
    model_roles.reset_overrides()  # = a fresh process: snapshot empty, ledger row present
    assert ARCHITECT.resolved_model_id() == DEFAULT_MODEL_ID  # what the CLI used to see
    db = SessionLocal()
    try:
        row = db.get(Workspace, DEFAULT_WORKSPACE_ID)
        outcome = service.install_preset(db, row, ARCHITECT, None)
        assert outcome.changed is False and _spec_of(agent_id)["model_id"] == KIMI
        assert _job_count() == 1  # no re-publish
        assert ARCHITECT.resolved_model_id() == KIMI  # the ledger's override is loaded now
        # the status read judges `default_changed` on the ledger's roles too
        model_roles.reset_overrides()
        assert _status(client)["default_changed"] is False
    finally:
        db.close()
