"""Agent delete tears down everything the agent left behind (N10-bug7 / N10-bug8).

Fakes stand in for IAM, CloudWatch Logs and S3 through the `aws_clients.client` funnel;
the registry is the real ledger-backed `LocalRegistryBackend`. Each residue step is
best-effort: one failing step is reported and the rest still run; the API answers 200
and lists what was removed.
"""

import json
import logging
from pathlib import Path
from typing import Any

import pytest
from botocore.exceptions import ClientError
from fastapi.testclient import TestClient
from sqlalchemy.orm.exc import DetachedInstanceError

import app.routers.agents as agents_router
from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.deployer import build_dirs, pipeline, teardown
from app.main import create_app
from app.models.ledger import Agent, Job
from app.services import agent_iam, aws_clients, memory_selfhosted
from app.services import opensearch_http as core
from app.services import registry_backend as reg
from app.services.agentcore import runtime as rt
from app.services.registry_local import LocalRegistryBackend
from tests.conftest import set_default_resources, settle_deploy_jobs, ws_ctx
from tests.memory_fakes import FakeOpenSearch

RESOURCES = {
    "artifacts_bucket": "bkt",
    "memory_os_endpoint": "https://os.example.test",
    "memory_id": "launchpad_memory-default",
    "memory_prefix": "memory",
    "registry_id": "local-default",
}
WS = ws_ctx(RESOURCES, id=DEFAULT_WORKSPACE_ID)
RUNTIME_ID = "e2e_harness_x-abc123"


class _NoSuchEntityException(Exception):
    pass


class _AccessDenied(Exception):
    pass


class _DeleteConflict(Exception):
    pass


class FakeIam:
    """IAM as the hub's EC2 ops role sees it: no ``iam:ListRolePolicies`` (the live
    reason the roles survived), ``DeleteRole`` refuses a role that still carries inline
    policies."""

    def __init__(self, roles: dict[str, set[str]], *, allow_list: bool = False,
                 fail_delete: bool = False):
        self.roles = roles  # role name → inline policy names
        self.allow_list = allow_list
        self.fail_delete = fail_delete
        self.calls: list[tuple[str, ...]] = []

    def list_role_policies(self, RoleName):
        self.calls.append(("list_role_policies", RoleName))
        if not self.allow_list:
            raise _AccessDenied("AccessDenied: iam:ListRolePolicies")
        if RoleName not in self.roles:
            raise _NoSuchEntityException("NoSuchEntity")
        return {"PolicyNames": sorted(self.roles[RoleName])}

    def delete_role_policy(self, RoleName, PolicyName):
        self.calls.append(("delete_role_policy", RoleName, PolicyName))
        policies = self.roles.get(RoleName)
        if policies is None or PolicyName not in policies:
            raise _NoSuchEntityException("NoSuchEntity")
        policies.discard(PolicyName)

    def delete_role(self, RoleName):
        self.calls.append(("delete_role", RoleName))
        if self.fail_delete:
            raise _AccessDenied("AccessDenied: iam:DeleteRole")
        if RoleName not in self.roles:
            raise _NoSuchEntityException("NoSuchEntity")
        if self.roles[RoleName]:
            raise _DeleteConflict("DeleteConflict: Cannot delete entity, must delete policies")
        del self.roles[RoleName]


class _ResourceNotFoundException(Exception):
    pass


class FakeControl:
    """bedrock-agentcore-control after DeleteAgentRuntime: the runtime stays DELETING for
    ``deleting_polls`` GetAgentRuntime calls, then ResourceNotFound (N10v4-obs9)."""

    def __init__(self, deleting_polls: int = 0):
        self.deleting_polls = deleting_polls
        self.polls: list[str] = []

    def get_agent_runtime(self, agentRuntimeId):
        self.polls.append(agentRuntimeId)
        if len(self.polls) <= self.deleting_polls:
            return {"agentRuntimeId": agentRuntimeId, "status": "DELETING"}
        raise _ResourceNotFoundException("ResourceNotFoundException: runtime not found")


class FakeLogs:
    def __init__(self, groups: list[str], *, fail: bool = False,
                 reappear_after_delete: list[str] | None = None,
                 reappear_on_describe: int | None = None,
                 fail_describe_from: int | None = None):
        self.groups = list(groups)
        self.fail = fail
        self.deleted: list[str] = []
        # N10v4-obs9: groups the runtime's trailing OTel flush re-creates (empty) once the
        # first delete has gone through — they show up on the NEXT describe, or (N-SCN-obs8,
        # a late flush) on the ``reappear_on_describe``-th describe only.
        self.reappear_after_delete = list(reappear_after_delete or [])
        self.reappear_on_describe = reappear_on_describe
        self.fail_describe_from = fail_describe_from  # the describe index that starts failing
        self.describes = 0

    def describe_log_groups(self, logGroupNamePrefix, nextToken=None):
        if nextToken is None:
            self.describes += 1
            if self.fail_describe_from and self.describes >= self.fail_describe_from:
                raise _AccessDenied("AccessDenied: logs:DescribeLogGroups")
            due = (self.reappear_on_describe is None and bool(self.deleted)) or (
                self.describes == self.reappear_on_describe)
            if due and self.reappear_after_delete:
                self.groups.extend(g for g in self.reappear_after_delete if g not in self.groups)
                self.reappear_after_delete = []
        matching = sorted(g for g in self.groups if g.startswith(logGroupNamePrefix))
        # one group per page to exercise the nextToken loop
        start = int(nextToken or 0)
        page = matching[start:start + 1]
        out: dict[str, Any] = {"logGroups": [{"logGroupName": g} for g in page]}
        if start + 1 < len(matching):
            out["nextToken"] = str(start + 1)
        return out

    def delete_log_group(self, logGroupName):
        if self.fail:
            raise _AccessDenied("AccessDenied: logs:DeleteLogGroup")
        if logGroupName not in self.groups:
            raise _ResourceNotFoundException("ResourceNotFoundException: no such log group")
        self.groups.remove(logGroupName)
        self.deleted.append(logGroupName)


class FakeS3:
    def __init__(self, objects: dict[str, set[str]]):
        self.objects = objects  # bucket → keys
        self.deletes: list[tuple[str, list[str]]] = []

    def list_objects_v2(self, Bucket, Prefix, MaxKeys=1000, ContinuationToken=None):
        # S3's token is a cursor over the key space (keys after the last one served),
        # so deletes between pages never skip a key — modelled the same way here.
        keys = sorted(k for k in self.objects.get(Bucket, set())
                      if k.startswith(Prefix) and k > (ContinuationToken or ""))
        page = keys[:2]  # tiny pages: the pagination loop is under test
        out: dict[str, Any] = {"Contents": [{"Key": k} for k in page], "IsTruncated": False}
        if len(keys) > 2:
            out["IsTruncated"] = True
            out["NextContinuationToken"] = page[-1]
        return out

    def delete_objects(self, Bucket, Delete):
        keys = [o["Key"] for o in Delete["Objects"]]
        self.deletes.append((Bucket, keys))
        self.objects[Bucket] -= set(keys)


def _agent(**overrides) -> Agent:
    fields = {
        "id": "agent0001-aaaa-bbbb-cccc-000000000001",
        "workspace_id": DEFAULT_WORKSPACE_ID,
        "name": "e2e-harness",
        "method": "harness",
        "status": "active",
        "spec": {"system_prompt": "x"},
        "resource_id": RUNTIME_ID,
        "arn": f"arn:aws-cn:bedrock-agentcore:cn-northwest-1:111:runtime/{RUNTIME_ID}",
    }
    fields.update(overrides)
    return Agent(**fields)


def _role_of(agent: Agent) -> str:
    return agent_iam.role_name_for(agent.name, agent.id)


def _fakes(agent: Agent, **kw) -> dict[str, Any]:
    """The account after a deploy: role with both platform inline policies, a runtime
    log group, artifacts and memory snapshots for this agent AND a neighbour that must
    survive."""
    other_id = "agent0002-aaaa-bbbb-cccc-000000000002"
    iam = FakeIam(
        {_role_of(agent): {
            agent_iam.capability_policy_name(agent.name), agent_iam.fs_policy_name(agent.name),
        }},
        allow_list=kw.get("allow_list", False), fail_delete=kw.get("fail_role", False),
    )
    logs = FakeLogs(
        [f"/aws/bedrock-agentcore/runtimes/{RUNTIME_ID}-DEFAULT",
         f"/aws/bedrock-agentcore/runtimes/{RUNTIME_ID}-DEFAULT-2",
         f"/aws/bedrock-agentcore/runtimes/{RUNTIME_ID}2-DEFAULT",  # longer id — not ours
         "/aws/bedrock-agentcore/runtimes/other-DEFAULT"],
        fail=kw.get("fail_logs", False),
        reappear_after_delete=kw.get("reappear_logs"),
        reappear_on_describe=kw.get("reappear_on_describe"),
        fail_describe_from=kw.get("fail_describe_from"),
    )
    control = FakeControl(deleting_polls=kw.get("deleting_polls", 0))
    s3 = FakeS3({"bkt": {
        f"agents/{agent.name}/deployment_package.zip",
        f"agents/{agent.name}/manifest.json",
        f"agents/{agent.name}/skills/a.md",
        f"agents/{agent.name}-two/deployment_package.zip",  # prefix neighbour
        "agents/other/deployment_package.zip",
        f"memory/launchpad_memory-default/{agent.id}__river/session_s1/events/0001.json",
        f"memory/launchpad_memory-default/{agent.id}__river/session_s1/events/0002.json",
        f"memory/launchpad_memory-default/{agent.id}__alice/session_s2/events/0001.json",
        f"memory/launchpad_memory-default/{other_id}__river/session_s1/events/0001.json",
        "skills/expense-report-writer/SKILL.md",
    }})
    # N11-obs19: long-term records of this agent's two scoped actors, a neighbour's, and
    # one of another workspace's store that shares the index (D17 shared domain)
    os_fake = FakeOpenSearch()
    os_fake.ensure_index(core.memory_index(""), core.memory_index_body())

    def _rec(rid: str, actor: str, store: str = "launchpad_memory-default") -> None:
        os_fake.put_doc(core.memory_index(""), rid, {
            "store_id": store, "workspace_id": "default", "actor_id": actor,
            "namespace": f"/facts/{actor}", "strategy": "semantic", "session_id": None,
            "text": rid, "created_at": "2026-10-09T12:00:00+00:00", "source_event_ids": [],
        })

    _rec("rec-river", f"{agent.id}__river")
    _rec("rec-alice", f"{agent.id}__alice")
    _rec("rec-other-agent", f"{other_id}__river")
    _rec("rec-other-store", f"{agent.id}__river", store="launchpad_memory-team-b")
    return {"iam": iam, "logs": logs, "s3": s3, "os": os_fake, "bedrock-agentcore-control": control}


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch) -> list[float]:
    """The teardown's one sleeper (runtime-gone poll interval, log-group settle delays)
    records instead of sleeping — the delete-API tests run with the real Settings."""
    slept: list[float] = []
    monkeypatch.setattr(teardown, "_SLEEP", slept.append)
    return slept


@pytest.fixture(autouse=True)
def inline_settle(monkeypatch) -> list[str]:
    """The settle's one thread starter runs its target inline (N-SCN-obs8) so a test sees
    the whole settle before the step returns; ``spawned`` records the thread names."""
    spawned: list[str] = []

    def spawn(target, name):
        spawned.append(name)
        target()

    monkeypatch.setattr(teardown, "_SPAWN", spawn)
    return spawned


def _settle_jobs() -> list:
    db = SessionLocal()
    try:
        return list(db.query(Job).filter(Job.type == teardown.SETTLE_JOB_TYPE)
                    .order_by(Job.created_at).all())
    finally:
        db.close()


def _events(job) -> list[str]:
    return [json.loads(line)["msg"] for line in job.log.splitlines() if line.strip()]


def _funnel(monkeypatch, fakes: dict[str, Any]) -> list[str]:
    asked: list[str] = []

    def client(service, ctx, cache_token=None, **cfg):
        asked.append(service)
        if service not in fakes:
            raise AssertionError(f"teardown asked for an unexpected client: {service}")
        return fakes[service]

    monkeypatch.setattr(aws_clients, "client", client)
    # the records purge reaches OpenSearch through its own funnel entry (N11-obs19) with
    # the request-thread retry budget (N-SCN-obs6 follow-up: `attempts=2`)
    monkeypatch.setattr(
        aws_clients, "opensearch_http",
        lambda ctx, endpoint, *, attempts: fakes.get("os") or FakeOpenSearch(),
    )
    return asked


@pytest.fixture
def registry():
    backend = LocalRegistryBackend()
    reg.set_backend(backend)
    return backend


def _a2a_record(backend, agent: Agent, *, approve: bool = False) -> str:
    card = reg.build_a2a_card(name=agent.name, description="d", arn=agent.arn or "",
                              method=agent.method, workspace=WS)
    record, _ = backend.upsert_record(
        WS, name=agent.name, description="Launchpad agent", descriptor_type="A2A",
        descriptors=reg.build_a2a_descriptors(card),
    )
    backend.submit_record(WS, record["recordId"])
    if approve:
        backend.approve_record(WS, record["recordId"])
    return record["recordId"]


def _settings(per_agent: bool = True, *, runtime_gone_timeout_s: float = 0,
              log_group_settle_s: tuple[float, ...] = ()):
    from types import SimpleNamespace

    return SimpleNamespace(
        per_agent_execution_roles=per_agent,
        teardown_runtime_gone_timeout_s=runtime_gone_timeout_s,
        teardown_log_group_settle_s=list(log_group_settle_s),
    )


SETTLE = (10.0, 30.0, 60.0)


def _by_step(steps) -> dict[str, teardown.TeardownStep]:
    return {s.step: s for s in steps}


# ── the residue steps ──────────────────────────────────────────────────────────


def _local_build_dir(agent: Agent, *, size: int = 3) -> Path:
    """N17-obs151: the `data/builds/<agent.name>` scratch a package stage left behind (the
    conftest points the root at a temp dir)."""
    path = build_dirs.build_dir_for(agent.name)
    (path / "pkg").mkdir(parents=True)
    (path / "pkg" / "main.py").write_bytes(b"x" * size)
    (path / "deployment_package.zip").write_bytes(b"z" * size)
    return path


def test_cleanup_removes_every_residue_and_only_this_agents(monkeypatch, registry):
    agent = _agent()
    agent.registry_record_id = _a2a_record(registry, agent)
    fakes = _fakes(agent)
    _funnel(monkeypatch, fakes)
    build_dir = _local_build_dir(agent)
    neighbour = _local_build_dir(Agent(name=f"{agent.name}-two"))
    log: list[str] = []

    steps = _by_step(teardown.cleanup_residue(agent, WS, settings=_settings(), log=log.append))

    assert [s.step for s in steps.values()] == list(teardown.RESIDUE_STEPS)
    assert {s.status for s in steps.values()} == {"deleted"}
    # N17-obs151: the local scratch went with the rest — the neighbour's stays
    assert not build_dir.exists() and neighbour.is_dir()
    assert steps["build_dir"].detail == f"6 bytes · builds/{agent.name}"
    assert any("deleted local build dir" in line for line in log)
    # role: both platform inline policies went first, then the role — without listing
    assert _role_of(agent) not in fakes["iam"].roles
    deletes = [c for c in fakes["iam"].calls if c[0] == "delete_role_policy"]
    assert {c[2] for c in deletes} == {
        agent_iam.capability_policy_name(agent.name), agent_iam.fs_policy_name(agent.name)}
    assert fakes["iam"].calls.index(deletes[-1]) < fakes["iam"].calls.index(
        ("delete_role", _role_of(agent)))
    # log groups: both of this runtime, not the longer-id neighbour nor "other"
    assert fakes["logs"].deleted == [
        f"/aws/bedrock-agentcore/runtimes/{RUNTIME_ID}-DEFAULT",
        f"/aws/bedrock-agentcore/runtimes/{RUNTIME_ID}-DEFAULT-2"]
    assert f"/aws/bedrock-agentcore/runtimes/{RUNTIME_ID}2-DEFAULT" in fakes["logs"].groups
    # S3: the agent's artifact prefix and every scoped actor of the agent; neighbours stay
    left = fakes["s3"].objects["bkt"]
    assert not {k for k in left if k.startswith(f"agents/{agent.name}/")}
    assert not {k for k in left if f"/{agent.id}__" in k}
    assert f"agents/{agent.name}-two/deployment_package.zip" in left
    assert "agents/other/deployment_package.zip" in left
    assert any("agent0002" in k for k in left)
    assert "skills/expense-report-writer/SKILL.md" in left
    assert steps["artifacts"].detail.startswith("3 object(s)")
    assert steps["memory_snapshots"].detail.startswith("3 object(s)")
    # OpenSearch (N11-obs19): both scoped actors' records went; the neighbour agent's
    # record and the other store's record (same index, D17) stay
    assert steps["memory_records"].detail.startswith("2 record(s)")
    assert set(fakes["os"].indices[core.memory_index("")]) == {"rec-other-agent", "rec-other-store"}
    # registry: the A2A record is gone
    assert registry.find_record(WS, agent.name, "A2A") is None
    assert "was PENDING_APPROVAL" in steps["registry_record"].detail
    assert any("deleted execution role" in line for line in log)


# ── N10v4-obs9: log groups go last, after the runtime is gone, with one re-check ──────
# Live (twice, both A2A runtimes): DELETE reported the group removed, yet it existed again
# with creationTime +1 s and 0 bytes — the runtime's OTel exporter flushed after our
# DeleteLogGroup. Round-3's Harness runtime had been idle, so it never showed.


def test_log_groups_are_deleted_last_and_only_after_the_runtime_is_gone(monkeypatch):
    agent = _agent()
    _local_build_dir(agent)
    fakes = _fakes(agent, deleting_polls=2)
    asked = _funnel(monkeypatch, fakes)
    settings = _settings(runtime_gone_timeout_s=60)
    monkeypatch.setattr(teardown.time, "monotonic", iter(range(0, 1000)).__next__)

    steps = teardown.cleanup_residue(agent, WS, settings=settings)

    assert [s.step for s in steps] == list(teardown.RESIDUE_STEPS)
    assert steps[-1].step == "log_groups" and steps[-1].status == "deleted"
    # the registry record went BEFORE the control-plane poll and the logs client
    assert asked.index("bedrock-agentcore-control") < asked.index("logs")
    assert asked.index("iam") < asked.index("bedrock-agentcore-control")
    # DELETING, DELETING, then NotFound — two interval sleeps, no re-check delay (0)
    assert fakes["bedrock-agentcore-control"].polls == [RUNTIME_ID] * 3
    assert "re-created" not in steps[-1].detail and "still present" not in steps[-1].detail


def test_a_group_the_trailing_flush_re_creates_is_deleted_again(monkeypatch, no_sleep,
                                                                 inline_settle):
    """N-SCN-obs8: the group comes back on the first settle check (+10 s) → deleted again;
    the +30 s and +60 s checks find nothing and the schedule ends. Settle outcome on the
    `log_group_settle` job, the step carries its id, the request thread never slept."""
    agent = _agent()
    flushed = f"/aws/bedrock-agentcore/runtimes/{RUNTIME_ID}-DEFAULT"
    fakes = _fakes(agent, reappear_logs=[flushed])
    _funnel(monkeypatch, fakes)
    log: list[str] = []

    steps = _by_step(teardown.cleanup_residue(
        agent, WS, settings=_settings(log_group_settle_s=SETTLE), log=log.append))

    step = steps["log_groups"]
    assert step.status == "deleted"
    assert fakes["logs"].deleted == [
        flushed, f"/aws/bedrock-agentcore/runtimes/{RUNTIME_ID}-DEFAULT-2", flushed]
    assert flushed not in fakes["logs"].groups  # gone for good
    assert f"/aws/bedrock-agentcore/runtimes/{RUNTIME_ID}2-DEFAULT" in fakes["logs"].groups
    # the schedule: +10, +30, +60 s after the first delete → sleeps of 10, 20, 30
    assert no_sleep == [10, 20, 30]
    assert fakes["logs"].describes == 4  # the first listing + three settle checks
    assert "settle re-check at +10/30/60 s in the background" in step.detail
    assert inline_settle == [f"log-group-settle-{agent.id}"]
    # the job: filed before the thread, succeeded, every check an event
    jobs = _settle_jobs()
    assert len(jobs) == 1 and step.job_id == jobs[0].id and f"(job {jobs[0].id})" in step.detail
    job = jobs[0]
    assert job.status == "succeeded" and job.error is None
    assert job.workspace_id == agent.workspace_id
    assert job.payload == {"agent_id": agent.id, "runtime_id": RUNTIME_ID,
                           "log_group_prefix": teardown.runtime_log_group_prefix(RUNTIME_ID),
                           "schedule_s": [10.0, 30.0, 60.0]}
    events = _events(job)
    assert any("settle check +10 s: 1 re-created" in e for e in events)
    assert any("settle check +30 s: nothing" in e for e in events)
    assert any("settle check +60 s: nothing" in e for e in events)
    assert events[-1].startswith("settle done: 3 check(s) · 1 re-created and deleted again")
    assert "stopped early" not in events[-1]
    assert any("re-created, settle check +10 s" in line for line in log)


def test_settle_stops_early_when_two_consecutive_checks_find_nothing(monkeypatch, no_sleep):
    """Never re-created → exactly one delete per group, two checks (+10, +30 s), no third."""
    agent = _agent()
    fakes = _fakes(agent)
    _funnel(monkeypatch, fakes)

    steps = _by_step(teardown.cleanup_residue(
        agent, WS, settings=_settings(log_group_settle_s=SETTLE)))

    step = steps["log_groups"]
    assert step.status == "deleted" and "re-created" not in step.detail
    assert len(fakes["logs"].deleted) == 2
    assert no_sleep == [10, 20] and fakes["logs"].describes == 3
    job = _settle_jobs()[-1]
    assert job.status == "succeeded"
    assert _events(job)[-1] == ("settle done: 2 check(s) · 0 re-created and deleted again · "
                                "stopped early (two consecutive empty checks)")


def test_a_late_flush_landing_before_the_30s_check_is_caught(monkeypatch, no_sleep):
    """Run #6 live: re-created 18 s after the delete — nothing at +10 s, there at +30 s →
    deleted; +60 s empty; the schedule ends (one empty check is not two)."""
    agent = _agent()
    flushed = f"/aws/bedrock-agentcore/runtimes/{RUNTIME_ID}-DEFAULT"
    fakes = _fakes(agent, reappear_logs=[flushed], reappear_on_describe=3)
    _funnel(monkeypatch, fakes)

    teardown.cleanup_residue(agent, WS, settings=_settings(log_group_settle_s=SETTLE))

    assert fakes["logs"].deleted == [
        flushed, f"/aws/bedrock-agentcore/runtimes/{RUNTIME_ID}-DEFAULT-2", flushed]
    assert flushed not in fakes["logs"].groups
    assert no_sleep == [10, 20, 30] and fakes["logs"].describes == 4
    events = _events(_settle_jobs()[-1])
    assert any("settle check +30 s: 1 re-created" in e for e in events)


def test_a_failing_settle_never_fails_the_purge(monkeypatch):
    """The listing starts failing on the second settle check: the step stays `deleted`,
    the purge returns normally, the job ends `failed` with the error — and the remaining
    check still ran."""
    agent = _agent()
    fakes = _fakes(agent, fail_describe_from=3)
    _funnel(monkeypatch, fakes)
    log: list[str] = []

    steps = _by_step(teardown.cleanup_residue(
        agent, WS, settings=_settings(log_group_settle_s=SETTLE), log=log.append))

    assert steps["log_groups"].status == "deleted"
    assert not [s.step for s in steps.values() if s.status == "failed"]
    assert fakes["logs"].describes == 4  # first listing, +10 ok, +30 fails, +60 fails
    job = _settle_jobs()[-1]
    assert job.status == "failed"
    assert job.error and "AccessDenied" in job.error
    assert any("settle check +30 s failed" in line for line in log)


def test_the_settle_runs_off_the_request_thread(monkeypatch, no_sleep):
    """Without the inline fixture: the step returns with the settle only *started* — no
    sleep on the request thread, the job still `queued`, the thread name recorded."""
    agent = _agent()
    fakes = _fakes(agent, reappear_logs=[f"/aws/bedrock-agentcore/runtimes/{RUNTIME_ID}-DEFAULT"])
    _funnel(monkeypatch, fakes)
    started: list = []
    monkeypatch.setattr(teardown, "_SPAWN", lambda target, name: started.append((name, target)))

    steps = _by_step(teardown.cleanup_residue(
        agent, WS, settings=_settings(log_group_settle_s=SETTLE)))

    assert steps["log_groups"].status == "deleted"
    assert no_sleep == [] and fakes["logs"].describes == 1
    assert [name for name, _ in started] == [f"log-group-settle-{agent.id}"]
    job = _settle_jobs()[-1]
    assert job.status == "queued" and steps["log_groups"].job_id == job.id
    # running the captured target is the whole settle
    started[0][1]()
    assert fakes["logs"].describes == 4 and no_sleep == [10, 20, 30]
    assert _settle_jobs()[-1].status == "succeeded"


def test_spawn_thread_starts_a_daemon():
    import threading

    done = threading.Event()
    thread = teardown._spawn_thread(done.set, "settle-test")
    assert thread.daemon and thread.name == "settle-test"
    assert done.wait(5)
    thread.join(5)


def test_an_empty_schedule_disables_the_settle(monkeypatch, no_sleep, inline_settle):
    agent = _agent()
    fakes = _fakes(agent, reappear_logs=[f"/aws/bedrock-agentcore/runtimes/{RUNTIME_ID}-DEFAULT"])
    _funnel(monkeypatch, fakes)
    before = len(_settle_jobs())

    steps = _by_step(teardown.cleanup_residue(agent, WS, settings=_settings()))

    step = steps["log_groups"]
    assert step.status == "deleted" and step.job_id is None and "settle" not in step.detail
    assert inline_settle == [] and no_sleep == [] and fakes["logs"].describes == 1
    assert len(_settle_jobs()) == before


def test_settle_schedule_is_sorted_positive_and_deduplicated():
    assert teardown.settle_schedule(_settings(log_group_settle_s=(30, 10, 0, 10, 60))) == (
        10.0, 30.0, 60.0)
    assert teardown.settle_schedule(_settings()) == ()
    from types import SimpleNamespace

    assert teardown.settle_schedule(SimpleNamespace()) == teardown.DEFAULT_LOG_GROUP_SETTLE_S


def test_settle_log_groups_alone_handles_a_group_that_will_not_delete():
    """Direct use (scripts/teardown.py): a delete refusal is an error in the outcome, the
    pass still completes and never raises."""
    flushed = "/aws/bedrock-agentcore/runtimes/r-1-DEFAULT"
    logs = FakeLogs([], reappear_after_delete=[flushed])
    logs.deleted.append("primed")  # the first delete "happened" before the settle
    logs.fail = True
    slept: list[float] = []
    out = teardown.settle_log_groups(
        logs, "/aws/bedrock-agentcore/runtimes/r-1-", schedule=SETTLE, log=lambda m: None,
        sleeper=slept.append)
    # the group is found (and refused) on every check — three errors, never an exception
    assert out.checks == 3 and out.re_deleted == [] and len(out.errors) == 3
    assert "AccessDenied" in out.errors[0] and slept == [10, 20, 30]
    assert out.summary().endswith(f"3 error(s) · {out.errors[0]}")


def test_a_runtime_still_deleting_at_the_deadline_is_noted_not_fatal(monkeypatch):
    agent = _agent()
    fakes = _fakes(agent, deleting_polls=99)
    _funnel(monkeypatch, fakes)
    log: list[str] = []
    steps = _by_step(teardown.cleanup_residue(
        agent, WS, settings=_settings(runtime_gone_timeout_s=0), log=log.append))
    step = steps["log_groups"]
    assert step.status == "deleted"  # best-effort: the groups still go
    assert "runtime still present after 0s" in step.detail
    assert fakes["bedrock-agentcore-control"].polls == [RUNTIME_ID]  # timeout 0 = one probe
    assert any("still present" in line for line in log)
    assert len(fakes["logs"].deleted) == 2


def test_settings_default_the_wait_and_the_settle_on():
    from app.core.config import Settings

    s = Settings()
    assert s.teardown_runtime_gone_timeout_s == 120
    assert s.teardown_log_group_settle_s == [10.0, 30.0, 60.0]
    assert teardown.settle_schedule(s) == teardown.DEFAULT_LOG_GROUP_SETTLE_S


@pytest.mark.parametrize("bad", [[30, 10], [10, 10], [0, 30], [10, 301]])
def test_settings_refuse_a_settle_schedule_that_is_not_ascending_and_bounded(bad):
    from pydantic import ValidationError

    from app.core.config import Settings

    with pytest.raises(ValidationError):
        Settings(teardown_log_group_settle_s=bad)


def test_role_is_deleted_even_when_the_ops_role_cannot_list_inline_policies(monkeypatch):
    """The live N10 leak: `agent_iam.delete_role` lists inline policies first; without
    `iam:ListRolePolicies` that is denied and DeleteRole then hits DeleteConflict. The
    teardown removes the platform's known policy names before the helper runs."""
    agent = _agent(registry_record_id=None)
    fakes = _fakes(agent, allow_list=False)
    _funnel(monkeypatch, fakes)

    steps = _by_step(teardown.cleanup_residue(agent, WS, settings=_settings(), log=print))

    assert steps["execution_role"].status == "deleted"
    assert steps["execution_role"].detail == _role_of(agent)
    assert fakes["iam"].roles == {}


def test_one_failing_step_does_not_stop_the_rest(monkeypatch, registry):
    agent = _agent()
    agent.registry_record_id = _a2a_record(registry, agent, approve=True)
    fakes = _fakes(agent, fail_logs=True)
    _funnel(monkeypatch, fakes)
    log: list[str] = []

    steps = _by_step(teardown.cleanup_residue(agent, WS, settings=_settings(), log=log.append))

    assert steps["log_groups"].status == "failed"
    assert "AccessDenied" in steps["log_groups"].detail
    assert steps["execution_role"].status == "deleted"
    assert steps["artifacts"].status == "deleted"
    assert steps["memory_snapshots"].status == "deleted"
    assert steps["registry_record"].status == "deleted"
    assert registry.find_record(WS, agent.name, "A2A") is None
    assert any("log_groups" in line and "failed" in line for line in log)


def test_an_exception_inside_a_step_is_reported_not_raised(monkeypatch):
    agent = _agent(registry_record_id=None)
    fakes = _fakes(agent)

    class Boom:
        def __getattr__(self, _name):
            raise RuntimeError("s3 is down")

    fakes["s3"] = Boom()
    _funnel(monkeypatch, fakes)

    steps = _by_step(teardown.cleanup_residue(agent, WS, settings=_settings(), log=print))

    assert steps["artifacts"] == teardown.TeardownStep(
        "artifacts", "failed", "RuntimeError: s3 is down", reason="RuntimeError: s3 is down")
    assert steps["memory_snapshots"].status == "failed"
    # N-SCN-obs9: the report carries the per-step reason, keyed by step
    report = teardown.TeardownReport(aws_resource_deleted=True, steps=list(steps.values()))
    assert report.as_dict()["failed_detail"] == {
        "artifacts": "RuntimeError: s3 is down",
        "memory_snapshots": "RuntimeError: s3 is down",
    }
    assert set(report.as_dict()["failed_detail"]) == set(report.failed)
    assert steps["execution_role"].status == "deleted"
    assert steps["log_groups"].status == "deleted"
    assert steps["registry_record"].status == "skipped"


def test_a_role_the_helper_cannot_delete_is_a_failed_step_with_the_sweep_hint(monkeypatch):
    agent = _agent(registry_record_id=None)
    fakes = _fakes(agent, fail_role=True)
    _funnel(monkeypatch, fakes)

    steps = _by_step(teardown.cleanup_residue(agent, WS, settings=_settings(), log=print))

    assert steps["execution_role"].status == "failed"
    assert _role_of(agent) in steps["execution_role"].detail
    assert "scripts/teardown.py" in steps["execution_role"].detail
    assert steps["log_groups"].status == "deleted"


def test_steps_skip_when_there_is_nothing_to_remove(monkeypatch):
    """No runtime id, no registry record, no bucket, no memory substrate: every step
    skips with a reason and no AWS client beyond IAM is ever requested."""
    agent = _agent(resource_id=None, registry_record_id=None)
    fakes = {"iam": FakeIam({})}
    asked = _funnel(monkeypatch, fakes)
    bare = ws_ctx({}, id=DEFAULT_WORKSPACE_ID)

    steps = _by_step(teardown.cleanup_residue(agent, bare, settings=_settings(), log=print))

    # NoSuchEntity → nothing to remove: skipped, not "deleted" (N16-obs76)
    assert steps["execution_role"].status == "skipped"
    assert steps["execution_role"].detail.endswith("did not exist")
    assert steps["log_groups"] == teardown.TeardownStep(
        "log_groups", "skipped", "agent has no runtime id")
    assert steps["artifacts"].status == "skipped"
    assert steps["memory_snapshots"] == teardown.TeardownStep(
        "memory_snapshots", "skipped", "self-hosted memory is not provisioned")
    assert steps["memory_records"] == teardown.TeardownStep(
        "memory_records", "skipped", "self-hosted memory is not provisioned")
    assert steps["registry_record"] == teardown.TeardownStep(
        "registry_record", "skipped", "agent has no registry record")
    assert asked == ["iam"]


def test_shared_execution_role_is_never_deleted(monkeypatch):
    agent = _agent(registry_record_id=None)
    fakes = _fakes(agent)
    asked = _funnel(monkeypatch, fakes)

    steps = _by_step(teardown.cleanup_residue(agent, WS, settings=_settings(False), log=print))

    assert steps["execution_role"].status == "skipped"
    assert "iam" not in asked
    assert _role_of(agent) in fakes["iam"].roles


def test_registry_record_that_is_not_this_agents_a2a_card_is_left_alone(monkeypatch, registry):
    agent = _agent()
    record, _ = registry.upsert_record(
        WS, name="hr-database", description="d", descriptor_type="MCP",
        descriptors=reg.build_mcp_descriptors(target="hr-database", description="d",
                                              gateway_url="https://gw", tools=None),
    )
    agent.registry_record_id = record["recordId"]
    _funnel(monkeypatch, _fakes(agent))

    steps = _by_step(teardown.cleanup_residue(agent, WS, settings=_settings(), log=print))

    assert steps["registry_record"].status == "skipped"
    assert "not this agent's A2A record" in steps["registry_record"].detail
    assert registry.get_record(WS, record["recordId"])["name"] == "hr-database"


def test_a_registry_record_already_gone_is_skipped(monkeypatch, registry):
    agent = _agent(registry_record_id="rec-gone")
    _funnel(monkeypatch, _fakes(agent))

    steps = _by_step(teardown.cleanup_residue(agent, WS, settings=_settings(), log=print))

    assert steps["registry_record"].status == "skipped"
    assert "already gone" in steps["registry_record"].detail


def test_no_registry_backend_is_a_skipped_step(monkeypatch):
    agent = _agent(registry_record_id="rec-1")
    _funnel(monkeypatch, _fakes(agent))

    steps = _by_step(teardown.cleanup_residue(agent, WS, settings=_settings(), log=print))

    assert steps["registry_record"] == teardown.TeardownStep(
        "registry_record", "skipped", "no registry backend in this build")


def test_snapshot_prefix_follows_the_spec_pinned_store_and_the_scoped_actor():
    from app.services.memory_selfhosted import substrate_for

    sub = substrate_for(WS)
    agent = _agent()
    assert teardown.snapshot_prefix(agent, sub) == (
        f"memory/launchpad_memory-default/{agent.id}__")
    pinned = _agent(spec={"memory": {"memory_id": "launchpad_memory-team"}})
    assert teardown.snapshot_prefix(pinned, sub) == f"memory/launchpad_memory-team/{agent.id}__"
    assert teardown.runtime_log_group_prefix("rt-1") == "/aws/bedrock-agentcore/runtimes/rt-1-"
    assert teardown.artifact_prefix(agent) == "agents/e2e-harness/"


def test_purge_agent_records_follows_the_spec_pinned_store(monkeypatch):
    """N11-obs19: the purge is a single delete_by_query on the environment's memory index,
    filtered on the store the spec pins (or the workspace default) AND the agent's actor
    prefix `<agent_id>__` — never on the bare agent id, never across stores."""
    agent = _agent(spec={"system_prompt": "x", "memory": {"memory_id": "launchpad_memory-team-b"}})
    fakes = _fakes(agent)
    _funnel(monkeypatch, fakes)

    deleted = memory_selfhosted.purge_agent_records(
        WS, agent.id, memory_id="launchpad_memory-team-b")

    assert deleted == 1
    assert set(fakes["os"].indices[core.memory_index("")]) == {
        "rec-river", "rec-alice", "rec-other-agent"}
    # default store when nothing is pinned
    assert memory_selfhosted.purge_agent_records(WS, agent.id) == 2
    assert set(fakes["os"].indices[core.memory_index("")]) == {"rec-other-agent"}
    # a workspace without the substrate is refused before any client is built
    bare = ws_ctx({}, id=DEFAULT_WORKSPACE_ID)
    with pytest.raises(Exception) as excinfo:
        memory_selfhosted.purge_agent_records(bare, agent.id)
    assert getattr(excinfo.value, "code", None) == "memory.not_provisioned"


def test_delete_s3_prefix_refuses_an_empty_prefix():
    with pytest.raises(ValueError):
        teardown.delete_s3_prefix(FakeS3({"bkt": {"a"}}), "bkt", "")


# ── through the API ────────────────────────────────────────────────────────────


SPEC = {"name": "e2e-harness", "method": "harness", "system_prompt": "Answer concisely."}


@pytest.fixture
def no_real_deploy(monkeypatch):
    monkeypatch.setattr(agents_router, "start_deploy_async", lambda jid: None)


def _deployed_agent(client, registry) -> Agent:
    agent_id = client.post("/api/agents", json=SPEC).json()["agent"]["id"]
    settle_deploy_jobs(agent_id)  # the deploy finished — its delete is not refused
    db = SessionLocal()
    try:
        row = db.get(Agent, agent_id)
        row.status = "active"
        row.resource_id = RUNTIME_ID
        row.arn = f"arn:aws-cn:bedrock-agentcore:cn-northwest-1:111:runtime/{RUNTIME_ID}"
        row.registry_record_id = _a2a_record(registry, row)
        db.commit()
        db.expunge(row)
        return row
    finally:
        db.close()


def test_delete_api_reports_what_was_removed(client, monkeypatch, registry, no_real_deploy):
    set_default_resources(RESOURCES)
    agent = _deployed_agent(client, registry)
    runtime_deletes: list[str] = []
    monkeypatch.setattr(agents_router.zip_method, "delete_agent_resources",
                        lambda a, _ws: runtime_deletes.append(a.resource_id))
    fakes = _fakes(agent)
    _funnel(monkeypatch, fakes)
    build_dir = _local_build_dir(agent)

    res = client.delete(f"/api/agents/{agent.id}")

    assert res.status_code == 200, res.text
    body = res.json()
    assert body["deleted"] is True and body["agent_id"] == agent.id
    assert body["aws_resource_deleted"] is True
    # log_groups LAST (N10v4-obs9) — after the runtime is observed gone; the local build dir
    # (N17-obs151) is one more residue step before them
    assert body["removed"] == ["runtime", "execution_role", "artifacts", "memory_snapshots",
                               "memory_records", "registry_record", "build_dir", "log_groups"]
    assert not build_dir.exists()
    assert fakes["bedrock-agentcore-control"].polls == [RUNTIME_ID]
    assert body["failed"] == []
    assert [s["step"] for s in body["steps"]] == body["removed"]
    assert body["steps"][0]["detail"] == RUNTIME_ID
    assert runtime_deletes == [RUNTIME_ID]
    # N-SCN-obs8: the real Settings schedule the settle; its job is the step's `job_id`
    # and readable through the jobs route once the (here inline) thread has run
    logs_step = body["steps"][-1]
    assert logs_step["step"] == "log_groups"
    assert "settle re-check at +10/30/60 s" in logs_step["detail"]
    job_res = client.get(f"/api/jobs/{logs_step['job_id']}")
    assert job_res.status_code == 200, (logs_step, job_res.text)
    job = job_res.json()
    assert job["type"] == "log_group_settle" and job["status"] == "succeeded"
    assert job["events"][-1]["msg"].startswith("settle done: 2 check(s)")
    assert fakes["logs"].describes == 3
    assert _role_of(agent) not in fakes["iam"].roles
    assert registry.find_record(WS, agent.name, "A2A") is None
    assert client.get("/api/agents").json()["agents"] == []


def test_delete_api_succeeds_and_names_the_failed_step(client, monkeypatch, registry,
                                                       no_real_deploy):
    set_default_resources(RESOURCES)
    agent = _deployed_agent(client, registry)
    monkeypatch.setattr(agents_router.zip_method, "delete_agent_resources", lambda a, _ws: None)
    fakes = _fakes(agent, fail_logs=True)
    _funnel(monkeypatch, fakes)

    res = client.delete(f"/api/agents/{agent.id}")

    assert res.status_code == 200, res.text
    body = res.json()
    assert body["deleted"] is True and body["aws_resource_deleted"] is True
    assert body["failed"] == ["log_groups"]
    assert "log_groups" not in body["removed"] and "registry_record" in body["removed"]
    # N-SCN-obs9: the API says WHY, per step — additive next to `failed`
    assert list(body["failed_detail"]) == ["log_groups"]
    assert body["failed_detail"]["log_groups"].startswith("2 of 2 log group delete(s) failed · ")
    assert "AccessDenied" in body["failed_detail"]["log_groups"]
    assert body["failed_detail"]["log_groups"] == body["steps"][-1]["reason"]
    db = SessionLocal()
    assert db.get(Agent, agent.id).status == "deleted"
    db.close()


def test_a_runtime_delete_failure_keeps_the_row_and_touches_no_residue(monkeypatch, registry,
                                                                        no_real_deploy):
    """The Runtime delete is the one hard step: when it fails the agent is NOT marked
    deleted (the member retries) and the role/log/S3/registry steps never run."""
    client = TestClient(create_app(), raise_server_exceptions=False)
    set_default_resources(RESOURCES)
    agent = _deployed_agent(client, registry)

    def boom(_agent, _ws):
        raise RuntimeError("DeleteAgentRuntime failed")

    monkeypatch.setattr(agents_router.zip_method, "delete_agent_resources", boom)
    fakes = _fakes(agent)
    asked = _funnel(monkeypatch, fakes)

    res = client.delete(f"/api/agents/{agent.id}")

    assert res.status_code == 500
    assert asked == []
    assert _role_of(agent) in fakes["iam"].roles
    assert registry.get_record(WS, agent.registry_record_id)["name"] == agent.name
    db = SessionLocal()
    assert db.get(Agent, agent.id).status == "active"
    db.close()


def test_delete_agent_row_keeps_the_boolean_contract(monkeypatch, registry, no_real_deploy,
                                                     client):
    """`assistant/purge.py` records only whether the Runtime went — the boolean wrapper
    stays, over the same teardown."""
    set_default_resources(RESOURCES)
    agent = _deployed_agent(client, registry)
    monkeypatch.setattr(agents_router.zip_method, "delete_agent_resources", lambda a, _ws: None)
    _funnel(monkeypatch, _fakes(agent))
    db = SessionLocal()
    try:
        row = db.get(Agent, agent.id)
        assert agents_router.delete_agent_row(db, row, WS) is True
        assert row.status == "deleted"
    finally:
        db.close()
    assert registry.find_record(WS, agent.name, "A2A") is None


def _never_provisioned_agent(client) -> Agent:
    """The explorer's F-TEARDOWN case (N16-obs76): the deploy failed at package, so no
    Runtime, execution role or registry record was ever created."""
    agent_id = client.post("/api/agents", json=SPEC).json()["agent"]["id"]
    db = SessionLocal()
    try:
        row = db.get(Agent, agent_id)
        row.status = "failed"
        row.resource_id = None
        row.arn = None
        db.commit()
        db.expunge(row)
        return row
    finally:
        db.close()


def test_delete_of_a_never_provisioned_agent_reports_skipped_not_deleted(
    client, monkeypatch, registry, no_real_deploy
):
    """N16-obs76: the report used to say runtime + execution_role "deleted" (and
    `aws_resource_deleted: true`) for an agent whose deploy never reached provision.
    Only what was actually removed is `removed`; the rest is `skipped` with a reason."""
    set_default_resources(RESOURCES)
    agent = _never_provisioned_agent(client)
    # the stubbed launch leaves the deploy job queued; the deploy "failed at generate" here,
    # so settle it — a delete during a live job is 409 agent.deploy_in_progress (N16-obs83)
    settle_deploy_jobs(agent.id, status="failed")
    runtime_deletes: list[str] = []
    monkeypatch.setattr(agents_router.zip_method, "delete_agent_resources",
                        lambda a, _ws: runtime_deletes.append(a.resource_id))
    fakes = _fakes(agent)
    fakes["iam"] = FakeIam({})  # no role was ever created
    _funnel(monkeypatch, fakes)

    res = client.delete(f"/api/agents/{agent.id}")

    assert res.status_code == 200, res.text
    body = res.json()
    assert body["deleted"] is True and body["aws_resource_deleted"] is False
    assert "runtime" not in body["removed"] and "execution_role" not in body["removed"]
    assert body["skipped"][:2] == ["runtime", "execution_role"]
    assert body["failed"] == []
    by_step = {s["step"]: s for s in body["steps"]}
    assert by_step["runtime"]["status"] == "skipped"
    assert "no runtime was created" in by_step["runtime"]["detail"]
    assert by_step["execution_role"]["status"] == "skipped"
    assert "did not exist" in by_step["execution_role"]["detail"]
    # every step is accounted for exactly once across the three lists
    assert sorted(body["removed"] + body["skipped"] + body["failed"]) == sorted(by_step)
    # the ledger half still happened
    assert client.get(f"/api/agents/{agent.id}").status_code == 404


class _RuntimeControl:
    """bedrock-agentcore-control with the N16-obs87 quirk: ``DeleteAgentRuntime`` on an id
    that no longer exists answers AccessDeniedException (not ResourceNotFound), while
    ``GetAgentRuntime`` on the same id says ResourceNotFound."""

    def __init__(self, *, exists: bool, delete_error: str | None = None,
                 gone_after_delete: bool = False):
        self.exists = exists
        self.delete_error = delete_error
        self.gone_after_delete = gone_after_delete
        self.calls: list[str] = []

    def get_agent_runtime(self, agentRuntimeId):
        self.calls.append("get")
        if not self.exists:
            raise ClientError({"Error": {"Code": "ResourceNotFoundException",
                                         "Message": "no such runtime"}}, "GetAgentRuntime")
        return {"agentRuntimeId": agentRuntimeId, "status": "READY"}

    def delete_agent_runtime(self, agentRuntimeId):
        self.calls.append("delete")
        if self.delete_error:
            if self.gone_after_delete:
                self.exists = False
            raise ClientError({"Error": {"Code": self.delete_error, "Message": "refused"}},
                              "DeleteAgentRuntime")
        self.exists = False
        return {}


def test_delete_runtime_if_present_reads_back_before_and_after(monkeypatch):
    gone = _RuntimeControl(exists=False)
    assert rt.delete_runtime_if_present(gone, "rt-1") == rt.RUNTIME_ABSENT
    assert gone.calls == ["get"]  # no delete attempted on a runtime that is not there
    live = _RuntimeControl(exists=True)
    assert rt.delete_runtime_if_present(live, "rt-1") == rt.RUNTIME_DELETED
    assert live.calls == ["get", "delete"]
    # AccessDenied from the delete, runtime reads back gone → it went
    quirk = _RuntimeControl(exists=True, delete_error="AccessDeniedException",
                            gone_after_delete=True)
    assert rt.delete_runtime_if_present(quirk, "rt-1") == rt.RUNTIME_DELETED
    assert quirk.calls == ["get", "delete", "get"]
    # AccessDenied and the runtime is still there → a real permission gap, surfaced
    denied = _RuntimeControl(exists=True, delete_error="AccessDeniedException")
    with pytest.raises(ClientError, match="AccessDenied"):
        rt.delete_runtime_if_present(denied, "rt-1")
    # any other refusal propagates untouched
    other = _RuntimeControl(exists=True, delete_error="ConflictException")
    with pytest.raises(ClientError, match="Conflict"):
        rt.delete_runtime_if_present(other, "rt-1")


@pytest.mark.parametrize("control, expect_status, expect_detail", [
    (dict(exists=False), "skipped", "already gone"),
    (dict(exists=True, delete_error="AccessDeniedException", gone_after_delete=True),
     "deleted", RUNTIME_ID),
])
def test_delete_api_succeeds_when_the_runtime_is_already_gone(
    client, monkeypatch, registry, no_real_deploy, control, expect_status, expect_detail
):
    """N16-obs87 (live t1 c1438eaf): an agent whose runtime no longer existed could never
    be deleted — DeleteAgentRuntime answered AccessDenied, the console aborted with 403
    before touching the row. The readback decides: not found → skipped; AccessDenied but
    gone → deleted; the row and its residue go either way."""
    set_default_resources(RESOURCES)
    agent = _deployed_agent(client, registry)
    stub = _RuntimeControl(**control)
    monkeypatch.setattr(agents_router.zip_method, "control_client", lambda _ws=None: stub)
    _funnel(monkeypatch, _fakes(agent))

    res = client.delete(f"/api/agents/{agent.id}")

    assert res.status_code == 200, res.text
    body = res.json()
    by_step = {s["step"]: s for s in body["steps"]}
    assert by_step["runtime"]["status"] == expect_status
    assert expect_detail in by_step["runtime"]["detail"]
    assert body["aws_resource_deleted"] is (expect_status == "deleted")
    assert "execution_role" in body["removed"] and body["removed"][-1] == "log_groups"
    assert body["failed"] == []
    assert client.get(f"/api/agents/{agent.id}").status_code == 404


def test_delete_api_still_surfaces_a_real_access_denied(client, monkeypatch, registry,
                                                        no_real_deploy):
    set_default_resources(RESOURCES)
    agent = _deployed_agent(client, registry)
    stub = _RuntimeControl(exists=True, delete_error="AccessDeniedException")
    monkeypatch.setattr(agents_router.zip_method, "control_client", lambda _ws=None: stub)
    _funnel(monkeypatch, _fakes(agent))

    res = client.delete(f"/api/agents/{agent.id}")

    assert res.status_code == 403, res.text
    assert res.json()["code"] == "aws.access_denied"
    assert stub.calls == ["get", "delete", "get"]  # re-read, still there → refused
    assert client.get(f"/api/agents/{agent.id}").status_code == 200  # the row stays


def test_a_stub_returning_a_bare_bool_is_normalised():
    assert agents_router._as_report(False).as_dict() == {
        "aws_resource_deleted": False, "removed": [], "skipped": [], "failed": [],
        "failed_detail": {}, "steps": []}
    report = teardown.TeardownReport(True, [teardown.TeardownStep("runtime", "deleted", "r")])
    assert agents_router._as_report(report) is report


# ---------------------------------------------------------------------------------------
# N-SCN-obs8 review follow-ups (cn/r7-h): the settle survives a backend restart, and its
# log line names the agent after the request's ORM instance is gone.
# ---------------------------------------------------------------------------------------

def _interrupted_settle_job(agent: Agent, status: str, *, workspace_id: str | None = None,
                            payload: dict[str, Any] | None = None) -> str:
    """A `log_group_settle` row as a backend that died mid-settle (or before starting it)
    leaves it: the payload `_settle_job_create` wrote, no terminal status."""
    db = SessionLocal()
    try:
        job = Job(
            workspace_id=workspace_id or agent.workspace_id,
            type=teardown.SETTLE_JOB_TYPE,
            status=status,
            payload=payload if payload is not None else {
                "agent_id": agent.id, "runtime_id": RUNTIME_ID,
                "log_group_prefix": teardown.runtime_log_group_prefix(RUNTIME_ID),
                "schedule_s": [10.0, 30.0, 60.0],
            },
        )
        db.add(job)
        db.commit()
        return job.id
    finally:
        db.close()


def _job(job_id: str) -> Job:
    db = SessionLocal()
    try:
        return db.get(Job, job_id)
    finally:
        db.close()


@pytest.mark.parametrize("status", ["running", "queued"])
def test_resume_pending_jobs_re_runs_an_interrupted_settle(monkeypatch, no_sleep,
                                                           inline_settle, status):
    """A `log_group_settle` job left `running` (died mid-settle) or `queued` (died before
    the thread claimed it) is re-run on startup against its recorded prefix: the group the
    trailing flush re-created while the backend was down is deleted, the row closes
    `succeeded`. The agent row does not exist any more — the payload is enough."""
    agent = _agent()  # never persisted: the delete removed it before the restart
    flushed = f"/aws/bedrock-agentcore/runtimes/{RUNTIME_ID}-DEFAULT"
    logs = FakeLogs([flushed, f"/aws/bedrock-agentcore/runtimes/{RUNTIME_ID}2-DEFAULT"])
    asked = _funnel(monkeypatch, {"logs": logs})
    job_id = _interrupted_settle_job(agent, status)
    monkeypatch.setattr(pipeline, "start_deploy_async",
                        lambda jid: pytest.fail(f"deploy starter took {jid}"))

    resumed = pipeline.resume_pending_jobs()

    assert resumed == [job_id]
    assert inline_settle == [f"log-group-settle-resume-{job_id[:8]}"]
    assert asked == ["logs"]  # the workspace was rebuilt from `jobs.workspace_id`
    assert logs.deleted == [flushed]  # the neighbour runtime's group survives
    assert f"/aws/bedrock-agentcore/runtimes/{RUNTIME_ID}2-DEFAULT" in logs.groups
    # the whole recorded schedule, from now: +10 s finds it, +30 / +60 s empty
    assert no_sleep == [10, 20, 30] and logs.describes == 3
    job = _job(job_id)
    assert job.status == "succeeded" and job.error is None
    events = _events(job)
    assert events[0].startswith("interrupted by backend restart — re-running the settle of "
                                f"{teardown.runtime_log_group_prefix(RUNTIME_ID)}* at +10/30/60 s")
    assert any("settle check +10 s: 1 re-created" in e for e in events)
    assert events[-1].startswith("settle done: 3 check(s) · 1 re-created and deleted again")


def test_resume_takes_only_open_settle_jobs_and_leaves_other_types_alone(monkeypatch,
                                                                         inline_settle):
    """A finished settle is inert; an unregistered job type is not touched; a `deploy_agent`
    job still goes to its own starter — the settle starter never sees it."""
    agent = _agent()
    done = _interrupted_settle_job(agent, "succeeded")
    db = SessionLocal()
    other = Job(workspace_id=agent.workspace_id, type="export_report", status="running",
                payload={})
    deploy = Job(workspace_id=agent.workspace_id, type="deploy_agent", status="running",
                 payload={"agent_id": agent.id, "deployment_id": "d1"})
    db.add_all([other, deploy])
    db.commit()
    other_id, deploy_id = other.id, deploy.id
    db.close()
    asked = _funnel(monkeypatch, {})
    launched: list[str] = []
    monkeypatch.setattr(pipeline, "start_deploy_async", launched.append)

    resumed = pipeline.resume_pending_jobs()

    assert resumed == [deploy_id] and launched == [deploy_id]
    assert inline_settle == [] and asked == []
    assert _job(done).status == "succeeded"
    assert _job(other_id).status == "running"


def test_resume_of_a_settle_whose_workspace_is_gone_fails_the_job_without_raising(
        monkeypatch, inline_settle, caplog):
    """The workspace row the job points at was deleted meanwhile: no client is built, the
    row closes `failed` naming the restart, startup continues."""
    agent = _agent()
    job_id = _interrupted_settle_job(agent, "running", workspace_id="ws-gone")
    asked = _funnel(monkeypatch, {})

    with caplog.at_level(logging.INFO, logger="app.deployer.teardown"):
        assert pipeline.resume_pending_jobs() == [job_id]

    assert inline_settle == [f"log-group-settle-resume-{job_id[:8]}"] and asked == []
    job = _job(job_id)
    assert job.status == "failed"
    assert job.error.startswith("interrupted by backend restart; not re-run: workspace 'ws-gone'")
    assert "no longer exists" in job.error
    assert _events(job) == [job.error]
    assert any(f"agent {agent.id}: log-group settle job {job_id}: interrupted" in r.getMessage()
               for r in caplog.records)


def test_resume_refuses_a_settle_payload_without_a_runtime_prefix(monkeypatch, inline_settle):
    """A damaged payload never turns the resume into a listing/delete outside the runtime
    log-group tree: no client, row `failed` with the reason."""
    agent = _agent()
    job_id = _interrupted_settle_job(agent, "queued",
                                     payload={"agent_id": agent.id, "log_group_prefix": "/aws/"})
    asked = _funnel(monkeypatch, {})

    assert pipeline.resume_pending_jobs() == [job_id]

    assert asked == []
    job = _job(job_id)
    assert job.status == "failed"
    assert "not re-run: payload carries no runtime log-group prefix ('/aws/')" in job.error


def test_schedule_from_payload_cleans_or_defaults():
    assert teardown._schedule_from_payload([30, 10.0, 0, 10]) == (10.0, 30.0)
    assert teardown._schedule_from_payload([]) == teardown.DEFAULT_LOG_GROUP_SETTLE_S
    assert teardown._schedule_from_payload(None) == teardown.DEFAULT_LOG_GROUP_SETTLE_S
    assert teardown._schedule_from_payload(["ten"]) == teardown.DEFAULT_LOG_GROUP_SETTLE_S


def test_settle_log_line_names_the_agent_after_its_orm_instance_expired(monkeypatch, registry,
                                                                        no_sleep, caplog):
    """`routers/agents.py` captures `agent.id` once before the residue steps: the settle
    thread logs after the request has deleted the row and closed its session. Simulated
    with an expired, detached instance (`agent.id` raises) and a deferred settle thread —
    the lazily evaluated lambda would have raised there and left the job `running`."""
    set_default_resources(RESOURCES)
    db = SessionLocal()
    agent = _agent()
    db.add(agent)
    db.commit()
    agent_id = agent.id
    fakes = _fakes(agent, reappear_logs=[f"/aws/bedrock-agentcore/runtimes/{RUNTIME_ID}-DEFAULT"])
    _funnel(monkeypatch, fakes)
    monkeypatch.setattr(agents_router.zip_method, "delete_agent_resources", lambda a, _ws: None)
    deferred: list = []
    monkeypatch.setattr(teardown, "_SPAWN", lambda target, name: deferred.append(target))

    report = agents_router._delete_agent_resources(agent, WS)

    assert report.aws_resource_deleted and len(deferred) == 1
    db.expire(agent)
    db.close()
    with pytest.raises(DetachedInstanceError):
        _ = agent.id  # the state the settle thread meets live
    with caplog.at_level(logging.INFO, logger="app.routers.agents"):
        deferred[0]()  # the settle thread runs now
    messages = [r.getMessage() for r in caplog.records]
    assert any(m.startswith(f"agent {agent_id}: settle done: 3 check(s) · 1 re-created")
               for m in messages), messages
    assert _settle_jobs()[-1].status == "succeeded"


# ── N-SCN-obs9: `failed_detail` reasons never carry a response body ─────────────


def test_short_reason_classifies_without_echoing_bodies_or_long_messages():
    body = "<html>" + "x" * 400 + "</html>"
    assert teardown.short_reason(core.OpenSearchError(503, body)) == "OpenSearchError: HTTP 503"
    from botocore.exceptions import ClientError

    denied = ClientError({"Error": {"Code": "AccessDeniedException", "Message": "User x is "
                                    "not authorized to perform logs:DeleteLogGroup on y"}},
                         "DeleteLogGroup")
    assert teardown.short_reason(denied) == "ClientError: AccessDeniedException on DeleteLogGroup"
    multi = RuntimeError("first line\nsecond line with a stack")
    assert teardown.short_reason(multi) == "RuntimeError: first line"
    long = ValueError("v" * 500)
    text = teardown.short_reason(long)
    assert len(text) == teardown._REASON_MAX and text.endswith("…")
    assert teardown.short_reason(KeyError()) == "KeyError"


def test_a_failed_memory_records_step_names_the_opensearch_status_not_the_body(monkeypatch):
    """The live N-SCN-obs9 shape: `failed=["memory_records"]` with no reason. Now
    `failed_detail.memory_records` says `OpenSearchError: HTTP 503` — a transient on the
    substrate, not a leak — while the step's `detail` keeps the longer `_err` text."""
    agent = _agent(registry_record_id=None)
    fakes = _fakes(agent)
    _funnel(monkeypatch, fakes)
    monkeypatch.setattr(memory_selfhosted, "has_substrate", lambda _ws: True)

    def purge(_ws, _agent_id, _memory_id):
        raise core.OpenSearchError(503, '{"error":{"reason":"circuit_breaking_exception …"}}')

    monkeypatch.setattr(memory_selfhosted, "purge_agent_records", purge)
    steps = teardown.cleanup_residue(agent, WS, settings=_settings(), log=print)
    report = teardown.TeardownReport(aws_resource_deleted=True, steps=steps)
    assert report.failed == ["memory_records"]
    assert report.as_dict()["failed_detail"] == {"memory_records": "OpenSearchError: HTTP 503"}
    assert "circuit_breaking" not in report.as_dict()["failed_detail"]["memory_records"]


# ── N17-obs190 (cn/r17-u): a key refused inside a 200 DeleteObjects is residue, not a delete ──
class PartialDeleteS3(FakeS3):
    """`FakeS3` whose `delete_objects` refuses every key containing ``refuse`` the way S3
    does — HTTP 200, the key in `Errors[]` with a code — and deletes the rest."""

    def delete_objects(self, Bucket, Delete):  # noqa: N803 — boto kwargs
        keys = [o["Key"] for o in Delete["Objects"]]
        kept = [k for k in keys if "refuse" in k]
        super().delete_objects(Bucket=Bucket, Delete={"Objects": [
            {"Key": k} for k in keys if k not in kept]})
        return {"Deleted": [{"Key": k} for k in keys if k not in kept],
                "Errors": [{"Key": k, "Code": "AccessDenied", "Message": "Access Denied"}
                           for k in kept]}


def test_a_key_refused_inside_a_200_delete_objects_is_a_failed_step_not_a_count(monkeypatch):
    """N17-obs190: `delete_s3_prefix` used to return the number of keys it ASKED S3 to delete
    and the step said `deleted · N object(s)` while S3's 200 body named the keys it refused.
    Through the shared deleter the artifacts step is `failed` with the sweep summary (counts
    + S3 code, never a key or the message) as its `reason`, the refused key is still there,
    the deleted ones are gone, and the other steps still run."""
    agent = _agent(registry_record_id=None)
    fakes = _fakes(agent)
    s3 = PartialDeleteS3(fakes["s3"].objects)
    s3.objects["bkt"].add(f"agents/{agent.name}/refuse-me.zip")
    fakes["s3"] = s3
    _funnel(monkeypatch, fakes)
    log: list[str] = []

    steps = _by_step(teardown.cleanup_residue(agent, WS, settings=_settings(), log=log.append))

    artifacts = steps["artifacts"]
    assert artifacts.status == "failed", artifacts
    assert artifacts.reason == "1 of 4 objects refused (AccessDenied)", artifacts
    assert artifacts.detail.startswith("3 deleted, 1 of 4 objects refused (AccessDenied) · s3://bkt/")
    assert "refuse-me" not in artifacts.detail and "Access Denied" not in artifacts.detail
    left = {k for k in s3.objects["bkt"] if k.startswith(f"agents/{agent.name}/")}
    assert left == {f"agents/{agent.name}/refuse-me.zip"}  # honest: exactly the refused key
    assert steps["memory_snapshots"].status == "deleted"
    assert steps["execution_role"].status == "deleted"
    report = teardown.TeardownReport(aws_resource_deleted=True, steps=list(steps.values()))
    assert report.as_dict()["failed_detail"] == {
        "artifacts": "1 of 4 objects refused (AccessDenied)"}
    assert any("1 of 4 objects refused (AccessDenied)" in line for line in log)


def test_delete_s3_prefix_reports_deleted_and_refused_keys_across_pages():
    s3 = PartialDeleteS3({"bkt": {"p/a", "p/b-refuse", "p/c", "p/d-refuse", "p/e", "q/x"}})
    result = teardown.delete_s3_prefix(s3, "bkt", "p/")
    assert sorted(result.deleted) == ["p/a", "p/c", "p/e"]
    assert result.failed == {"p/b-refuse": "AccessDenied", "p/d-refuse": "AccessDenied"}
    assert result.summary() == "2 of 5 objects refused (AccessDenied ×2)"
    assert len(s3.deletes) == 3  # the fake's two-key pages, every page swept
    assert s3.objects["bkt"] == {"p/b-refuse", "p/d-refuse", "q/x"}
