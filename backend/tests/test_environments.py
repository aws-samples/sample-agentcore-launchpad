"""Environment comparison and drift detection (`app.services.environments`).

Ported from the console-v3 branch without workspace tiers or the promotion bundle
digest: the baseline is the caller's current workspace, the fingerprint is inlined,
and both reads apply grants + each workspace's visibility mode.
"""

from datetime import UTC, datetime, timedelta

import pytest
from botocore.exceptions import ClientError
from fastapi.testclient import TestClient

from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.models.ledger import Agent, Deployment, Workspace
from app.routers import environments as environments_router
from app.services import environments as env_service
from app.services import users as users_service
from tests import test_visibility as tv
from tests.test_visibility import PEER, _isolate

SPEC = {"name": "fleet-agent", "method": "zip_runtime", "system_prompt": "v1", "model_id": "m"}

# the auth-enabled console of the workspace visibility suite
app, admin = tv.app, tv.admin


def add_workspace(ws_id: str, name: str | None = None) -> str:
    db = SessionLocal()
    try:
        if db.get(Workspace, ws_id) is None:
            db.add(Workspace(id=ws_id, name=name or ws_id.title(),
                             account_id="7" + str(sum(map(ord, ws_id))).zfill(11),
                             region="us-east-1",
                             bootstrap_status="ready"))
            db.commit()
    finally:
        db.close()
    return ws_id


def add_agent(ws_id, *, name="fleet-agent", spec=None, method="zip_runtime", version="2",
              status="active", resource_id="rt-1", image=None, created_by=None) -> str:
    db = SessionLocal()
    try:
        agent = Agent(workspace_id=ws_id, name=name, method=method, status=status,
                      spec=dict(spec or {**SPEC, "name": name, "method": method}),
                      version=version, resource_id=resource_id, arn="arn:x",
                      created_by=created_by, created_by_name="someone" if created_by else None)
        db.add(agent)
        db.flush()
        db.add(Deployment(workspace_id=ws_id, agent_id=agent.id, status="succeeded",
                          image_digest=image))
        db.commit()
        return agent.id
    finally:
        db.close()


def compare(client, agent="fleet-agent", **headers):
    res = client.get("/api/environments/compare", params={"agent": agent}, headers=headers)
    assert res.status_code == 200, res.text
    return res.json()


def rows_of(body):
    return {r["workspace"]["id"]: r for r in body["environments"]}


def _member(app, username: str, workspaces: list[str]) -> tuple[str, TestClient]:
    """An approved member granted exactly `workspaces`, and a logged-in client."""
    password = "sufficient-pass"
    client = TestClient(app, client=PEER)
    registered = client.post(
        "/api/auth/register",
        json={"username": username, "email": f"{username}@acme-corp.com", "password": password},
    )
    assert registered.status_code == 201, registered.text
    db = SessionLocal()
    try:
        user = users_service.find_by_username(db, username)
        user.status = users_service.STATUS_ACTIVE
        user.expires_at = datetime.now(UTC) + timedelta(days=7)
        users_service.set_workspace_grants(db, user, workspaces)
        db.commit()
        user_id = user.id
    finally:
        db.close()
    login = client.post("/api/auth/login", json={"username": username, "password": password})
    assert login.status_code == 200, login.text
    # several grants: the console always names the workspace (the header selector)
    client.headers["X-Workspace"] = DEFAULT_WORKSPACE_ID
    return user_id, client


# ── compare ───────────────────────────────────────────────────────────────


def test_compare_judges_every_workspace_against_the_current_one(client):
    add_workspace("ws-b", "Bravo")
    add_workspace("ws-c", "Charlie")
    add_workspace("ws-d", "Delta")
    add_agent(DEFAULT_WORKSPACE_ID, version="4")
    add_agent("ws-b", version="7")  # same spec, another AWS version: still the same
    add_agent("ws-c", spec={**SPEC, "system_prompt": "v0 - older", "model_id": "m2"})
    add_agent("ws-d", status="deleted")  # a deleted agent is not there

    body = compare(client)
    rows = rows_of(body)
    assert body["baseline_workspace"] == DEFAULT_WORKSPACE_ID
    # the baseline first, then the others by name
    assert [r["workspace"]["id"] for r in body["environments"]] == [
        DEFAULT_WORKSPACE_ID, "ws-b", "ws-c", "ws-d"]
    assert rows[DEFAULT_WORKSPACE_ID]["state"] == "baseline"
    assert rows[DEFAULT_WORKSPACE_ID]["baseline"] is True
    assert rows["ws-b"]["state"] == "same" and rows["ws-b"]["changed_groups"] == []
    assert rows["ws-c"]["state"] == "differs"
    assert rows["ws-c"]["changed_groups"] == ["prompt", "model"]
    assert rows["ws-d"]["state"] == "absent" and rows["ws-d"]["agent"] is None
    agent = rows["ws-b"]["agent"]
    assert agent["version"] == "7" and agent["method"] == "zip_runtime"
    assert agent["last_deploy"]["status"] == "succeeded"
    assert rows["ws-b"]["workspace"] == {
        "id": "ws-b", "name": "Bravo", "account_id": "700000000377", "region": "us-east-1",
        "status": "ready",
    }
    assert agent["spec_digest"] == env_service.spec_digest("zip_runtime", SPEC)
    assert body["summary"] == {
        "workspaces": 4, "present": 3, "same": 1, "differs": 1, "absent": 1,
        "distinct_spec_digests": 2, "aligned": False,
    }


def test_switching_workspace_switches_the_baseline(client):
    add_workspace("ws-b")
    add_agent(DEFAULT_WORKSPACE_ID)
    add_agent("ws-b", spec={**SPEC, "system_prompt": "other"})
    body = compare(client, **{"X-Workspace": "ws-b"})
    rows = rows_of(body)
    assert body["baseline_workspace"] == "ws-b"
    assert body["environments"][0]["workspace"]["id"] == "ws-b"
    assert rows["ws-b"]["state"] == "baseline"
    assert rows[DEFAULT_WORKSPACE_ID]["state"] == "differs"


def test_identical_specs_are_aligned(client):
    add_workspace("ws-b")
    add_agent(DEFAULT_WORKSPACE_ID, version="9")
    add_agent("ws-b", version="1")
    body = compare(client)
    assert body["summary"]["aligned"] is True
    assert {r["state"] for r in body["environments"]} == {"baseline", "same"}


def test_deploy_time_stamps_never_make_two_specs_differ(client):
    add_workspace("ws-b")
    byoc = {**SPEC, "method": "byoc", "byoc": {"artifact_kind": "upload", "upload_id": "u1"}}

    def stamped(sha: str) -> dict:
        return {**byoc, "byoc": {**byoc["byoc"], "provenance": {"sha256": sha}}}

    add_agent(DEFAULT_WORKSPACE_ID, method="byoc", spec=stamped("aaa"))
    add_agent("ws-b", method="byoc", spec=stamped("bbb"))
    rows = rows_of(compare(client))
    assert rows["ws-b"]["state"] == "same"
    assert env_service.spec_digest("byoc", stamped("aaa")) == env_service.spec_digest(
        "byoc", byoc)


def test_a_different_image_is_a_difference(client):
    add_workspace("ws-b")
    add_workspace("ws-c")
    add_agent(DEFAULT_WORKSPACE_ID, method="container", image="sha256:aaa")
    add_agent("ws-b", method="container", image="sha256:bbb")
    add_agent("ws-c", method="container", image=None)  # unknown digest: not judged
    rows = rows_of(compare(client))
    assert rows["ws-b"]["state"] == "differs" and rows["ws-b"]["changed_groups"] == ["image"]
    assert rows["ws-c"]["state"] == "same"


def test_without_a_baseline_agent_nothing_is_judged(client):
    add_workspace("ws-b")
    add_agent("ws-b")
    body = compare(client)
    rows = rows_of(body)
    assert rows[DEFAULT_WORKSPACE_ID]["state"] == "absent"
    assert rows["ws-b"]["state"] == "no_baseline"
    assert body["summary"]["aligned"] is False
    assert client.get("/api/environments/compare").status_code == 422


def test_a_member_only_sees_the_workspaces_they_are_granted(app, admin):
    add_workspace("ws-b")
    add_workspace("ws-c")
    add_agent(DEFAULT_WORKSPACE_ID)
    add_agent("ws-b")
    add_agent("ws-c", spec={**SPEC, "system_prompt": "secret prod prompt"})
    _, member = _member(app, "viewer", [DEFAULT_WORKSPACE_ID, "ws-b"])
    body = compare(member)
    assert [r["workspace"]["id"] for r in body["environments"]] == [DEFAULT_WORKSPACE_ID, "ws-b"]
    assert "ws-c" not in str(body)
    # an administrator sees every workspace
    assert len(compare(admin)["environments"]) == 3


def test_each_workspaces_visibility_mode_applies(app, admin):
    add_workspace("ws-b")
    ada_id, ada = _member(app, "ada", [DEFAULT_WORKSPACE_ID, "ws-b"])
    bob_id, _ = _member(app, "bob", [DEFAULT_WORKSPACE_ID, "ws-b"])
    add_agent(DEFAULT_WORKSPACE_ID, created_by=ada_id)
    add_agent("ws-b", created_by=bob_id)  # bob's twin of the same name

    assert rows_of(compare(ada))["ws-b"]["state"] == "same"  # both shared
    _isolate("ws-b")  # only ws-b isolates
    rows = rows_of(compare(ada))
    assert rows["ws-b"]["state"] == "absent" and rows["ws-b"]["agent"] is None
    assert rows[DEFAULT_WORKSPACE_ID]["state"] == "baseline"
    assert rows_of(compare(admin))["ws-b"]["state"] == "same"  # admins see all
    # ada's own agent in the isolated default is still hers
    _isolate()
    assert rows_of(compare(ada))[DEFAULT_WORKSPACE_ID]["state"] == "baseline"
    _, carl = _member(app, "carl", [DEFAULT_WORKSPACE_ID, "ws-b"])
    assert {r["state"] for r in compare(carl)["environments"]} == {"absent"}


# ── drift ─────────────────────────────────────────────────────────────────


class Control:
    """A stand-in for the bedrock-agentcore-control client, keyed by resource id."""

    def __init__(self, runtimes=None, harnesses=None):
        self.runtimes = runtimes or {}
        self.harnesses = harnesses or {}
        self.reads: list[str] = []

    def get_agent_runtime(self, agentRuntimeId):
        return self._get(self.runtimes, agentRuntimeId)

    def get_harness(self, harnessId):
        return {"harness": self._get(self.harnesses, harnessId)}

    def _get(self, table, key):
        self.reads.append(key)
        found = table[key]
        if isinstance(found, Exception):
            raise found
        return found


def error(code):
    return ClientError({"Error": {"Code": code, "Message": "m"}}, "GetAgentRuntime")


@pytest.fixture
def stub(monkeypatch):
    def install(control):
        monkeypatch.setattr(environments_router, "control_client", lambda _ctx: control)
        return control

    return install


def drift(client, **params):
    res = client.get("/api/environments/drift", params=params)
    assert res.status_code == 200, res.text
    return res.json()


def by_name(body):
    return {a["name"]: a for a in body["agents"]}


def test_matching_aws_state_is_in_sync(client, stub):
    add_agent(DEFAULT_WORKSPACE_ID, resource_id="rt-ok", version="2")
    stub(Control(runtimes={"rt-ok": {"status": "READY", "agentRuntimeVersion": "2"}}))
    body = drift(client)
    assert body["state"] == "in_sync" and body["counts"] == {"in_sync": 1, "drift": 0, "unknown": 0}
    agent = by_name(body)["fleet-agent"]
    assert (agent["ledger_version"], agent["aws_version"], agent["aws_status"]) == (
        "2", "2", "READY")
    assert agent["kind"] == "runtime" and body["truncated"] is False
    assert isinstance(body["elapsed_ms"], int) and body["limit"] == 100


def test_a_console_edit_shows_up_as_a_version_change(client, stub):
    add_agent(DEFAULT_WORKSPACE_ID, resource_id="rt-1", version="2")
    stub(Control(runtimes={"rt-1": {"status": "READY", "agentRuntimeVersion": "5"}}))
    body = drift(client)
    agent = by_name(body)["fleet-agent"]
    assert body["state"] == "drift" and agent["state"] == "drift"
    assert agent["findings"] == [{"code": "version_changed", "expected": "2", "observed": "5"}]


def test_missing_and_unhealthy_resources_are_drift(client, stub):
    add_agent(DEFAULT_WORKSPACE_ID, name="gone", resource_id="rt-gone")
    add_agent(DEFAULT_WORKSPACE_ID, name="sick", resource_id="rt-sick")
    add_agent(DEFAULT_WORKSPACE_ID, name="h", method="harness", resource_id="h-1", version="3")
    stub(Control(
        runtimes={"rt-gone": error("ResourceNotFoundException"),
                  "rt-sick": {"status": "UPDATE_FAILED", "agentRuntimeVersion": "2"}},
        harnesses={"h-1": {"status": "READY", "harnessVersion": "3"}},
    ))
    agents = by_name(drift(client))
    assert agents["gone"]["findings"][0]["code"] == "resource_missing"
    assert agents["sick"]["findings"][0] == {
        "code": "unhealthy", "expected": "READY", "observed": "UPDATE_FAILED"}
    assert agents["h"]["state"] == "in_sync" and agents["h"]["kind"] == "harness"


@pytest.mark.parametrize(
    "answer,reason",
    [
        (error("ThrottlingException"), "unreadable"),
        (error("AccessDeniedException"), "unreadable"),
        (RuntimeError("boom"), "unreadable"),
        ({}, "no_status_reported"),
        ({"status": "UPDATING", "agentRuntimeVersion": "3"}, "transitioning"),
        ({"status": "READY"}, "version_not_comparable"),
    ],
)
def test_anything_we_cannot_judge_is_unknown_never_in_sync(client, stub, answer, reason):
    add_agent(DEFAULT_WORKSPACE_ID, resource_id="rt-1", version="2")
    stub(Control(runtimes={"rt-1": answer}))
    body = drift(client)
    agent = by_name(body)["fleet-agent"]
    assert agent["state"] == "unknown" and agent["reason"] == reason
    assert body["state"] == "unknown" and body["counts"]["in_sync"] == 0


def test_a_running_canary_explains_a_newer_version(client, stub):
    from app.optimization.models import RuntimeCanary

    agent_id = add_agent(DEFAULT_WORKSPACE_ID, resource_id="rt-1", version="2")
    db = SessionLocal()
    try:
        db.add(RuntimeCanary(workspace_id=DEFAULT_WORKSPACE_ID, name="c",
                             champion_agent_id=agent_id, champion_agent_name="fleet-agent",
                             challenger_agent_id="x", challenger_agent_name="y",
                             status="running"))
        db.commit()
    finally:
        db.close()
    stub(Control(runtimes={"rt-1": {"status": "READY", "agentRuntimeVersion": "3"}}))
    agent = by_name(drift(client))["fleet-agent"]
    assert agent["state"] == "unknown" and agent["reason"] == "canary_active"


def test_one_bad_read_does_not_hide_the_drift_next_to_it(client, stub):
    add_agent(DEFAULT_WORKSPACE_ID, name="a-drifted", resource_id="rt-a", version="1")
    add_agent(DEFAULT_WORKSPACE_ID, name="b-broken", resource_id="rt-b")
    stub(Control(runtimes={"rt-a": {"status": "READY", "agentRuntimeVersion": "9"},
                           "rt-b": RuntimeError("x")}))
    body = drift(client)
    assert body["state"] == "drift" and body["counts"] == {"in_sync": 0, "drift": 1, "unknown": 1}


def test_drift_covers_only_this_workspaces_live_agents(client, stub):
    add_workspace("ws-b")
    add_agent("ws-b", name="elsewhere", resource_id="rt-p")
    add_agent(DEFAULT_WORKSPACE_ID, name="draft", status="draft", resource_id=None)
    add_agent(DEFAULT_WORKSPACE_ID, name="mine", resource_id="rt-m")
    control = stub(Control(runtimes={"rt-m": {"status": "READY", "agentRuntimeVersion": "2"}}))
    body = drift(client)
    assert list(by_name(body)) == ["mine"] and control.reads == ["rt-m"]
    assert drift(client, agent="nope")["checked"] == 0
    assert drift(client, agent="nope")["state"] == "in_sync"  # nothing to disagree with
    assert drift(client, agent="mine")["checked"] == 1


def test_drift_reads_aws_only_for_agents_the_member_can_see(app, admin, stub):
    ada_id, ada = _member(app, "ada", [DEFAULT_WORKSPACE_ID])
    bob_id, _ = _member(app, "bob", [DEFAULT_WORKSPACE_ID])
    add_agent(DEFAULT_WORKSPACE_ID, name="ada-agent", resource_id="rt-ada", created_by=ada_id)
    add_agent(DEFAULT_WORKSPACE_ID, name="bob-agent", resource_id="rt-bob", created_by=bob_id)
    ready = {"status": "READY", "agentRuntimeVersion": "2"}
    control = stub(Control(runtimes={"rt-ada": ready, "rt-bob": ready}))
    assert set(by_name(drift(ada))) == {"ada-agent", "bob-agent"}  # shared
    _isolate()
    control.reads.clear()
    body = drift(ada)
    assert list(by_name(body)) == ["ada-agent"] and control.reads == ["rt-ada"]
    assert "bob-agent" not in str(body)
    assert drift(ada, agent="bob-agent")["checked"] == 0
    assert set(by_name(drift(admin))) == {"ada-agent", "bob-agent"}


def test_the_limit_is_reported_rather_than_silent():
    for i in range(3):
        add_agent(DEFAULT_WORKSPACE_ID, name=f"n{i}", resource_id=f"r{i}")
    db = SessionLocal()
    try:
        body = env_service.detect_drift(
            db, workspace_id=DEFAULT_WORKSPACE_ID, control=Control(runtimes={
                f"r{i}": {"status": "READY", "agentRuntimeVersion": "2"} for i in range(3)}),
            limit=2,
        )
    finally:
        db.close()
    assert body["truncated"] is True and body["checked"] == 2
    assert [a["name"] for a in body["agents"]] == ["n0", "n1"]


def test_the_cap_applies_after_visibility_filtering():
    from app.services.visibility import Viewer

    for i in range(3):
        add_agent(DEFAULT_WORKSPACE_ID, name=f"hidden{i}", resource_id=f"h{i}",
                  created_by="someone-else")
    add_agent(DEFAULT_WORKSPACE_ID, name="z-mine", resource_id="m", created_by="me")
    db = SessionLocal()
    try:
        body = env_service.detect_drift(
            db, workspace_id=DEFAULT_WORKSPACE_ID,
            control=Control(runtimes={"m": {"status": "READY", "agentRuntimeVersion": "2"}}),
            viewer=Viewer(user_id="me", filters=True), limit=1,
        )
    finally:
        db.close()
    assert [a["name"] for a in body["agents"]] == ["z-mine"] and body["truncated"] is False
