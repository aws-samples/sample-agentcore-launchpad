"""Agent delete removes the S3 artifacts the deploy wrote under the agent's own keys.

Hermetic: the cleanup's S3 client is a stub (conftest no-ops the cleanup for every
other test; these tests put the real function back with the stub injected).
"""

from typing import Any

import pytest

import app.routers.agents as agents_router
from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.models.ledger import Agent
from app.services import agent_artifacts
from tests.conftest import ws_ctx

REAL_CLEANUP = agent_artifacts.delete_agent_artifacts
BUCKET = "launchpad-artifacts-test"


class FakeS3:
    def __init__(self, keys: list[str], *, fail: bool = False):
        self.keys = set(keys)
        self.fail = fail
        self.calls: list[tuple[str, str]] = []

    def list_objects_v2(self, Bucket: str, Prefix: str, **_kw: Any) -> dict[str, Any]:
        assert Bucket == BUCKET
        self.calls.append(("list", Prefix))
        if self.fail:
            raise RuntimeError("s3 unavailable")
        hits = sorted(k for k in self.keys if k.startswith(Prefix))
        return {"Contents": [{"Key": k} for k in hits], "IsTruncated": False}

    def delete_object(self, Bucket: str, Key: str) -> dict[str, Any]:
        assert Bucket == BUCKET
        self.calls.append(("delete", Key))
        if self.fail:
            raise RuntimeError("s3 unavailable")
        self.keys.discard(Key)
        return {}


@pytest.fixture
def s3(monkeypatch):
    """Route the real cleanup through one shared stub; quiet the other teardown steps."""
    holder: dict[str, FakeS3] = {"s3": FakeS3([])}
    monkeypatch.setattr(
        agent_artifacts, "delete_agent_artifacts",
        lambda agent, workspace, s3_client=None: REAL_CLEANUP(
            agent, workspace, s3_client=holder["s3"]),
    )
    monkeypatch.setattr(agents_router, "delete_agent_role", lambda *_a: True)
    monkeypatch.setattr(agents_router, "delete_agent_registry_record", lambda *_a: True)
    for method in (agents_router.zip_method, agents_router.container_method,
                   agents_router.byoc_method, agents_router.harness_method):
        monkeypatch.setattr(method, "delete_agent_resources", lambda *_a, **_k: None)
    return holder


def _agent(client, name: str, method: str = "zip_runtime") -> str:
    res = client.post("/api/agents", json={
        "name": name, "method": method, "system_prompt": "Answer concisely.",
    })
    assert res.status_code == 202, res.text
    return res.json()["agent"]["id"]


@pytest.fixture(autouse=True)
def no_real_deploy(monkeypatch):
    monkeypatch.setattr(agents_router, "start_deploy_async", lambda _jid: None)


SHARED = [
    "agents/pkg-agent-2/deployment_package.zip",  # a sibling name, not a sub-prefix
    "skills/web-search/SKILL.md",
    "system-skills/architect/1.5.8/SKILL.md",
    "byoc/default/abc/source.zip",
    "builds/coding-runtime/source.zip",
    "kb/hr/handbook.pdf",
]


def test_zip_runtime_agent_delete_removes_its_package(client, s3):
    agent_id = _agent(client, "pkg-agent")
    s3["s3"] = FakeS3([
        "agents/pkg-agent/deployment_package.zip",
        "agents/pkg-agent/canary/c1-candidate.zip",
        *SHARED,
    ])

    res = client.delete(f"/api/agents/{agent_id}")

    assert res.status_code == 200 and res.json()["deleted"] is True
    deleted = sorted(k for op, k in s3["s3"].calls if op == "delete")
    assert deleted == ["agents/pkg-agent/canary/c1-candidate.zip",
                       "agents/pkg-agent/deployment_package.zip"]
    assert s3["s3"].keys == set(SHARED)


def test_s3_failure_does_not_fail_the_agent_delete(client, s3):
    agent_id = _agent(client, "flaky-s3")
    s3["s3"] = FakeS3(["agents/flaky-s3/deployment_package.zip"], fail=True)

    res = client.delete(f"/api/agents/{agent_id}")

    assert res.status_code == 200 and res.json()["deleted"] is True
    assert s3["s3"].calls  # tried, failed, moved on
    db = SessionLocal()
    try:
        assert db.get(Agent, agent_id).status == "deleted"
    finally:
        db.close()


def test_delete_object_failure_is_swallowed():
    class DeleteFails(FakeS3):
        def delete_object(self, Bucket: str, Key: str) -> dict[str, Any]:
            raise RuntimeError("AccessDenied")

    agent = Agent(id="a1", name="nodel", method="zip_runtime",
                  workspace_id=DEFAULT_WORKSPACE_ID)
    fake = DeleteFails(["agents/nodel/deployment_package.zip"])
    assert REAL_CLEANUP(agent, ws_ctx({"artifacts_bucket": BUCKET}), s3_client=fake) == []


def test_harness_agent_delete_makes_no_s3_call(client, s3):
    agent_id = _agent(client, "managed-loop", method="harness")
    s3["s3"] = FakeS3(["agents/managed-loop/deployment_package.zip", *SHARED])

    assert client.delete(f"/api/agents/{agent_id}").status_code == 200

    assert s3["s3"].calls == []
    assert len(s3["s3"].keys) == len(SHARED) + 1


@pytest.mark.parametrize("method", ["harness", "discovered"])
def test_methods_without_per_agent_artifacts_have_no_locations(method):
    agent = Agent(id="a1", name="any-agent", method=method,
                  workspace_id=DEFAULT_WORKSPACE_ID)
    assert agent_artifacts.artifact_locations(agent) == ([], [])


def test_name_reused_by_a_live_agent_leaves_every_key(client, s3):
    """A deferred teardown that finishes after the name was reused must not take the
    new holder's package."""
    db = SessionLocal()
    try:
        old = Agent(name="reused", method="zip_runtime", status="deleted",
                    workspace_id=DEFAULT_WORKSPACE_ID, project_id="proj-test")
        new = Agent(name="reused", method="zip_runtime", status="active",
                    workspace_id=DEFAULT_WORKSPACE_ID, project_id="proj-test")
        db.add_all([old, new])
        db.commit()
        db.refresh(old)
        fake = FakeS3(["agents/reused/deployment_package.zip"])
        assert REAL_CLEANUP(old, ws_ctx({"artifacts_bucket": BUCKET}), s3_client=fake) == []
        assert fake.calls == []
        assert fake.keys == {"agents/reused/deployment_package.zip"}
    finally:
        db.close()


def test_container_agent_deletes_its_exact_build_key_and_both_prefixes():
    agent = Agent(id="a1", name="box-agent", method="container",
                  workspace_id=DEFAULT_WORKSPACE_ID, project_id="p-iso")
    fake = FakeS3([
        "builds/box-agent/source.zip",
        "builds/box-agent/notes.txt",  # not a key the platform writes
        "projects/p-iso/builds/box-agent/source.zip",
        "projects/p-iso/agents/box-agent/canary/c2-restore.zip",
        "projects/p-iso/agents/box-agent-x/deployment_package.zip",
        "projects/p-other/agents/box-agent/deployment_package.zip",
    ])

    deleted = REAL_CLEANUP(agent, ws_ctx({"artifacts_bucket": BUCKET}), s3_client=fake)

    assert sorted(deleted) == [
        "builds/box-agent/source.zip",
        "projects/p-iso/agents/box-agent/canary/c2-restore.zip",
        "projects/p-iso/builds/box-agent/source.zip",
    ]
    assert fake.keys == {
        "builds/box-agent/notes.txt",
        "projects/p-iso/agents/box-agent-x/deployment_package.zip",
        "projects/p-other/agents/box-agent/deployment_package.zip",
    }


@pytest.mark.parametrize("name", ["coding-runtime", "skill-lab-worker", "placeholder"])
def test_platform_build_sources_are_never_deleted(name):
    agent = Agent(id="a1", name=name, method="container", workspace_id=DEFAULT_WORKSPACE_ID)
    key = f"builds/{name}/source.zip"
    fake = FakeS3([key])
    assert REAL_CLEANUP(agent, ws_ctx({"artifacts_bucket": BUCKET}), s3_client=fake) == []
    assert fake.keys == {key}


def test_no_bucket_means_no_s3_call():
    agent = Agent(id="a1", name="pkg-agent", method="byoc", workspace_id=DEFAULT_WORKSPACE_ID)
    fake = FakeS3(["agents/pkg-agent/byoc_package.zip"])
    assert REAL_CLEANUP(agent, ws_ctx({}), s3_client=fake) == []
    assert fake.calls == []


def test_listing_follows_continuation_tokens():
    class Paged(FakeS3):
        def list_objects_v2(self, Bucket: str, Prefix: str, **kw: Any) -> dict[str, Any]:
            hits = sorted(k for k in self.keys if k.startswith(Prefix))
            if not hits or "ContinuationToken" in kw:
                return {"Contents": [{"Key": k} for k in hits[1:]], "IsTruncated": False}
            return {"Contents": [{"Key": hits[0]}], "IsTruncated": len(hits) > 1,
                    "NextContinuationToken": "t2"}

    agent = Agent(id="a1", name="paged", method="zip_runtime",
                  workspace_id=DEFAULT_WORKSPACE_ID)
    keys = ["agents/paged/canary/a-candidate.zip", "agents/paged/deployment_package.zip"]
    fake = Paged(keys)
    assert sorted(REAL_CLEANUP(agent, ws_ctx({"artifacts_bucket": BUCKET}),
                               s3_client=fake)) == keys


def test_deferred_teardown_sweep_cleans_artifacts_once_aws_lets_go(monkeypatch):
    from app.dlc import scheduler

    seen: list[str] = []
    monkeypatch.setattr(agents_router, "delete_agent_role", lambda *_a: True)
    monkeypatch.setattr(agents_router, "delete_agent_registry_record", lambda *_a: True)
    monkeypatch.setattr(agent_artifacts, "delete_agent_artifacts",
                        lambda agent, _ws: seen.append(agent.name) or [])
    agent = Agent(id="a1", name="late-agent", method="zip_runtime",
                  workspace_id=DEFAULT_WORKSPACE_ID)
    scheduler._finish_deferred_cleanup(agent, ws_ctx({"artifacts_bucket": BUCKET}))
    assert seen == ["late-agent"]
