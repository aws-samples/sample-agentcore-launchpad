# ruff: noqa: F811  — pytest fixtures are imported by name from tests.test_visibility
"""E19 — the WORKSPACE lifecycle graph walked through the real routes and the real bootstrap
job (round 17, r17-ws; N17-obs117).

Graph: ``tests/graph/graphs/workspace.yaml``. Hermetic: the stateful fake AWS of
``tests/test_workspace_bootstrap.py`` (create-then-list returns what was created, so a resumed
stage really adopts) behind the client funnel, the Skill Lab worker and the registry default
import stubbed at their service seams, every ``workspace_bootstrap.STAGES`` entry wrapped so the
world can INTERRUPT the job right before a named stage — the shape a backend restart leaves (the
job `running`, the next stage `running`) — and resume it with ``execute_bootstrap_job`` the way
``resume_pending_jobs`` does. The administrator registers and drives; bob is a member.

The world's extra oracle: the WORKSPACE ROW digest (status + resource map) — the executor's
19 counted tables do not include `workspaces`, and a refused call that flipped the row's status
or touched its map must be caught (rev-r16's in-place-UPDATE lesson).
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from typing import Any

import pytest

from app.core.db import SessionLocal
from app.models.ledger import Job, Workspace
from app.services import aws_clients, registry_console
from app.services import workspace_bootstrap as wb
from app.skill_lab import infra as skill_lab_infra
from app.skill_lab import samples as skill_lab_samples
from tests.graph import neighbourhood
from tests.graph.executor import GRAPHS, World, assert_tables_explicit, params_for
from tests.graph.paths import Edge
from tests.graph.walks import run_walk, walk_seeds
from tests.test_visibility import ada, admin, app, bob  # noqa: F401 — fixtures by name
from tests.test_workspace_bootstrap import ACCOUNT, FakeAws, _client_error

SOAK = os.environ.get("LAUNCHPAD_SOAK") == "1"
#: the hub's OTHER CN region — the N17-obs112 shape; the fake STS answers the hub's own
#: account, so the workspace is same-account / other-region
REGION = "cn-north-1"
#: a distinctive token for the secret oracle: it must never reach the job log or the row
SECRET = "sk-graph-SECRET-7f3a9c-never-logged"
STAGE_AFTER: dict[str, str | None] = {
    s: (wb.STAGE_ORDER[i + 1] if i + 1 < len(wb.STAGE_ORDER) else None)
    for i, s in enumerate(wb.STAGE_ORDER)
}
FAST = wb.Timeouts(
    gateway_s=1, iam_attempts=2, iam_delay_s=0,
    access_denied_attempts=2, access_denied_delay_s=0, sleeper=lambda _s: None,
)
DENIAL_TEXT = "the hub's credentials must be able to provision this workspace"
PROBE_DETAIL = f"{REGION} usable · 5 services probed"


class Interrupted(BaseException):
    """Not an ``Exception``: the runner's `except Exception` never sees it, so the job is left
    exactly as a process death leaves it (`running`, the stage `running`)."""


#: fake methods that WRITE: an `interrupt` armed on one of them fires AFTER the write (the
#: process died once the resource existed — the shape a resume must adopt, not re-create)
WRITE_METHODS = frozenset({
    "create_role", "create_bucket", "create_repository", "create_project", "create_gateway",
    "create_api_key_credential_provider", "create_log_group", "update_trace_segment_destination",
})
#: pseudo seams: service-level stubs the fixture installs (no fake client behind them)
PSEUDO_REGISTRY = ("registry_console", "ensure_default_records")
PSEUDO_SKILL_LAB = ("skill_lab", "ensure_skill_lab_worker")


class Seams:
    """What the fixture patches, shared by every world of a test (fresh fakes per world).

    ``arm(service, method, raise_)`` makes the next call(s) of one fake method (or of every
    fake method, ``service="*"``) raise what ``raise_()`` builds — the fault layer's hook
    (``tests/graph/fault_seams_workspace.py``); every served fault is appended to ``calls``.
    """

    def __init__(self) -> None:
        self.aws: FakeAws | None = None
        self.stop_at: str | None = None  # raise Interrupted right BEFORE this stage runs
        self.before_stage: dict[str, Any] = {}  # stage → callable(ctx) run before the stage
        self.armed: tuple[str, str] | None = None
        self.raise_: Any = None
        self.once: bool = False
        self.calls: list[str] = []

    def arm(self, service: str, method: str, raise_: Any, *, once: bool = False) -> None:
        self.armed, self.raise_, self.once = (service, method), raise_, once

    def disarm(self) -> None:
        self.armed, self.raise_, self.once = None, None, False

    def fire(self, service: str, method: str, inner: Any = None, *args: Any, **kw: Any) -> Any:
        """Serve the armed fault at ``(service, method)`` — or delegate to ``inner``."""
        if self.armed is not None and self.armed[1] == method and self.armed[0] in ("*", service):
            exc = self.raise_()
            self.calls.append(f"{service}.{method}")
            if self.once:
                self.disarm()
            if isinstance(exc, BaseException) and not isinstance(exc, Exception) \
                    and method in WRITE_METHODS and inner is not None:
                inner(*args, **kw)  # the write happened; the process died afterwards
            raise exc
        if self.armed is not None and self.armed == ("*", "*"):
            self.calls.append(f"{service}.{method}")
            raise self.raise_()
        return inner(*args, **kw) if inner is not None else None

    def client(self, service: str, ctx: Any, cache_token: str | None = None, **cfg: Any) -> Any:
        assert self.aws is not None, "a world must own the fake AWS before a client is built"
        return _Faulty(service, self.aws.client(service, ctx, cache_token, **cfg), self)


class _Faulty:
    """A fake service behind the fault hook: every method call goes through ``Seams.fire``."""

    def __init__(self, service: str, inner: Any, seams: Seams) -> None:
        self._service, self._inner, self._seams = service, inner, seams

    def __getattr__(self, name: str) -> Any:
        target = getattr(self._inner, name)
        if not callable(target):
            return target

        def call(*args: Any, **kw: Any) -> Any:
            return self._seams.fire(self._service, name, target, *args, **kw)

        return call


@pytest.fixture
def seams(monkeypatch) -> Seams:
    seams = Seams()
    monkeypatch.setattr(aws_clients, "client", seams.client)
    monkeypatch.setenv("LAUNCHPAD_GLOBAL_BEDROCK_API_KEY", SECRET)
    # the route must not start the worker thread: the world runs the job inline
    monkeypatch.setattr(wb, "start_bootstrap_async", lambda job_id: None)
    # the Skill Lab worker (CodeBuild image + runtime) and the sample seeding have their own
    # focused fakes; here the stage's own bookkeeping is what the graph asserts
    def fake_worker(ws, ws_id, log=None):
        seams.fire(*PSEUDO_SKILL_LAB)
        return {
            "skill_lab_worker_role_arn": ws.arn("iam", "role/launchpad-skill-lab-worker-x",
                                                region=""),
            "skill_lab_worker_image_tag": "sha-fake",
            "skill_lab_worker_image_digest": "sha256:fake",
            "skill_lab_worker_runtime_id": "rt-fake",
            "skill_lab_worker_runtime_arn": ws.arn("bedrock-agentcore", "runtime/rt-fake"),
        }

    def fake_defaults(ws, log=None, auto_approve=False):
        seams.fire(*PSEUDO_REGISTRY)
        return []

    monkeypatch.setattr(skill_lab_infra, "ensure_skill_lab_worker", fake_worker)
    monkeypatch.setattr(skill_lab_samples, "ensure_skill_lab_samples",
                        lambda db, ws_id, ws, log=None: "samples stubbed")
    monkeypatch.setattr(registry_console, "ensure_default_records", fake_defaults)
    real_stages = dict(wb.STAGES)

    def wrap(name: str):
        def stage(ctx):
            if seams.stop_at == name:
                raise Interrupted(f"interrupted before {name}")
            hook = seams.before_stage.get(name)
            if hook is not None:
                hook(ctx)
            return real_stages[name](ctx)
        return stage

    for name in wb.STAGE_ORDER:
        monkeypatch.setitem(wb.STAGES, name, wrap(name))
    return seams


class WorkspaceWorld(World):
    graph = "workspace"
    UNDRIVEN: dict[str, str] = {}
    XFAIL: dict[str, str] = {}

    def __init__(self, clients, seams: Seams) -> None:
        super().__init__(clients)
        self.seams = seams
        self.aws = FakeAws()
        self.aws.expect_region = REGION
        seams.aws = self.aws
        seams.stop_at = None
        seams.before_stage.clear()
        seams.disarm()
        seams.calls = []
        self.ws_id = f"g-ws-{uuid.uuid4().hex[:8]}"
        self.job_id: str | None = None
        self.map_before_failure: dict[str, Any] | None = None

    # -- helpers -------------------------------------------------------------------
    def row(self) -> Workspace | None:
        db = SessionLocal()
        try:
            row = db.get(Workspace, self.ws_id)
            if row is not None:
                db.expunge(row)
            return row
        finally:
            db.close()

    def job(self, job_id: str | None = None) -> Job:
        db = SessionLocal()
        try:
            job = db.get(Job, job_id or self.job_id)
            assert job is not None, job_id or self.job_id
            db.expunge(job)
            return job
        finally:
            db.close()

    def jobs(self) -> list[Job]:
        db = SessionLocal()
        try:
            rows = db.query(Job).filter(Job.workspace_id == self.ws_id).all()
            for r in rows:
                db.expunge(r)
            return rows
        finally:
            db.close()

    def resources(self) -> dict[str, Any]:
        row = self.row()
        return dict(row.resources or {}) if row is not None else {}

    def stage_records(self, job_id: str | None = None) -> dict[str, dict[str, Any]]:
        return {s["name"]: s for s in wb.job_stages(self.job(job_id))}

    def post_bootstrap(self, body: dict | None = None, client=None):
        client = client or self.admin
        if body is None:
            return client.post(f"/api/workspaces/{self.ws_id}/bootstrap")
        return client.post(f"/api/workspaces/{self.ws_id}/bootstrap", json=body)

    def run_until(self, stop_at: str | None) -> None:
        """Execute (or resume) the current job inline; ``stop_at`` interrupts the job right
        before that stage, the way a restart would."""
        self.seams.stop_at = stop_at
        try:
            if stop_at is None:
                wb.execute_bootstrap_job(self.job_id, timeouts=FAST)
            else:
                with pytest.raises(Interrupted):
                    wb.execute_bootstrap_job(self.job_id, timeouts=FAST)
        finally:
            self.seams.stop_at = None

    def assert_interrupted_after(self, stage: str) -> None:
        """The s_<stage> node: the stage succeeded, the next one `running`, the job `running`,
        the row `bootstrapping`."""
        records = self.stage_records()
        assert records[stage]["status"] == "succeeded", (stage, records[stage])
        nxt = STAGE_AFTER[stage]
        assert nxt is not None
        assert records[nxt]["status"] == "running", (nxt, records[nxt])
        assert self.job().status == "running"
        assert self.row().bootstrap_status == "bootstrapping"
        self.assert_no_secret()

    def assert_recorded(self, keys: list[str]) -> None:
        resources = self.resources()
        missing = [k for k in keys if not resources.get(k)]
        assert missing == [], (missing, sorted(resources))

    def assert_no_secret(self) -> None:
        """The Global Bedrock API key never reaches the job log, the job error, a stage detail
        or the row (architecture.md 'the key material is never written to the row or the job
        log')."""
        job = self.job()
        blob = json.dumps({"log": job.log, "error": job.error, "payload": job.payload,
                           "resources": self.resources()}, ensure_ascii=False, default=str)
        assert SECRET not in blob, "the Global Bedrock API key leaked into the job or the row"

    def advance(self, stage: str, keys: list[str] | None = None) -> dict[str, Any]:
        """Resume the interrupted job through ``stage`` and interrupt before the next one."""
        self.run_until(STAGE_AFTER[stage])
        self.assert_interrupted_after(stage)
        if keys:
            self.assert_recorded(keys)
        return self.stage_records()[stage]

    def heal(self) -> None:
        """Undo the instance-attribute faults the failure edges arm (deleting the shadowing
        attribute exposes the fake's own method again)."""
        for obj, name in (("ecr", "describe_repositories"), ("s3", "create_bucket")):
            service = getattr(self.aws, obj)
            if name in service.__dict__:
                delattr(service, name)

    # -- the row digest (the executor's tables do not include `workspaces`) ------------
    def extra_counts(self) -> dict[str, int]:
        row = self.row()
        payload = None if row is None else {
            "status": row.bootstrap_status, "resources": row.resources, "name": row.name,
        }
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode())
        return {"workspace_row": digest.hexdigest()}  # type: ignore[dict-item]

    # -- edges: registration + the probe --------------------------------------------
    def edge_register(self, edge: Edge) -> None:
        res = self.admin.post("/api/workspaces", json={
            "id": self.ws_id, "name": f"graph {self.ws_id}", "account_id": ACCOUNT,
            "region": REGION,
        })
        body = self.expect(res, edge)
        assert body["bootstrap_status"] == "registered" and body["cross_account"] is False
        self.ids.add(self.ws_id)
        assert self.resources() == {}

    def _validate_only(self, edge: Edge) -> None:
        before = self.row().bootstrap_status
        created_before = self._created()
        body = self.expect(self.post_bootstrap({"validate_only": True}), edge)
        assert body["mode"] == "validate-only"
        assert [s["name"] for s in body["stages"]] == ["validate-access"]
        self.job_id = body["job_id"]
        self.run_until(None)
        job = self.job()
        assert job.status == "succeeded", job.error
        assert self.stage_records()["validate-access"]["detail"] == PROBE_DETAIL
        assert self.row().bootstrap_status == before, "a probe must hand the status back"
        assert self._created() == created_before, "a probe provisioned something"
        latest = self.admin.get(f"/api/workspaces/{self.ws_id}/bootstrap").json()
        assert latest["job"]["id"] == self.job_id and latest["job"]["mode"] == "validate-only"
        self.assert_no_secret()

    edge_validate_only = _validate_only
    edge_validate_only_again = _validate_only

    def _created(self) -> dict[str, int]:
        a = self.aws
        return {"roles": len(a.iam.created), "buckets": len(a.s3.created),
                "repos": len(a.ecr.created), "projects": len(a.codebuild.created),
                "gateways": len(a.control.created_gateways),
                "providers": len([c for c in a.control.provider_calls if c.startswith("create")])}

    # -- edges: the full job, stage by stage ------------------------------------------
    def _start_full(self, edge: Edge, *, expect_carried: list[str]) -> None:
        body = self.expect(self.post_bootstrap(), edge)
        assert body["mode"] == "full"
        self.job_id = body["job_id"]
        carried = [s["name"] for s in body["stages"] if s["status"] == "succeeded"]
        assert carried == expect_carried, (carried, expect_carried)
        for s in body["stages"]:
            if s["status"] == "succeeded":
                assert s["detail"] == "already provisioned by an earlier run", s
        assert self.row().bootstrap_status == "bootstrapping"

    def edge_run_validate(self, edge: Edge) -> None:
        self._start_full(edge, expect_carried=[])
        self.run_until("iam")
        self.assert_interrupted_after("validate-access")
        assert self.stage_records()["validate-access"]["detail"] == PROBE_DETAIL
        assert self.resources() == {}

    def edge_run_validate_from_probed(self, edge: Edge) -> None:
        self._start_full(edge, expect_carried=[])  # the probe is not carried forward
        self.run_until("iam")
        self.assert_interrupted_after("validate-access")
        assert self.stage_records()["validate-access"]["detail"] == PROBE_DETAIL

    def edge_run_iam(self, edge: Edge) -> None:
        self.advance("iam", ["execution_role_arn", "gateway_role_arn"])
        assert len(self.aws.iam.created) == 2, self.aws.iam.created
        assert self.resources()["execution_role_arn"].endswith(
            f"role/launchpad-agent-execution-role-{REGION}")

    def edge_run_model_credential(self, edge: Edge) -> None:
        self.advance("model-credential", ["model_credential_arn"])
        assert any(c.startswith("create:") for c in self.aws.control.provider_calls)

    def edge_run_storage(self, edge: Edge) -> None:
        self.advance("storage", ["artifacts_bucket", "ecr_repo", "ecr_repo_uri"])
        assert self.resources()["artifacts_bucket"] == f"launchpad-artifacts-{ACCOUNT}-{REGION}"
        assert self.aws.s3.created[0]["location"] == REGION

    def edge_run_codebuild(self, edge: Edge) -> None:
        self.advance("codebuild", ["codebuild_project"])
        assert len(self.aws.iam.created) == 3  # + the CodeBuild service role

    def edge_run_gateway(self, edge: Edge) -> None:
        detail = self.advance("gateway", ["gateway_id", "gateway_arn", "gateway_url"])
        assert detail["detail"].endswith("· no demo targets"), detail
        assert self.aws.control.created_gateways == ["launchpad-gw"]

    def edge_run_registry(self, edge: Edge) -> None:
        self.advance("registry", ["registry_id"])
        assert self.resources()["registry_id"] == f"local-{self.ws_id}"

    def edge_run_memory(self, edge: Edge) -> None:
        detail = self.advance("memory")
        assert detail["detail"] == "unavailable · hub substrate only", detail
        assert self.resources().get("memory_unavailable_reason")

    def edge_run_skill_lab(self, edge: Edge) -> None:
        self.advance("skill-lab", ["skill_lab_worker_runtime_arn", "skill_lab_worker_role_arn"])

    def edge_run_observability(self, edge: Edge) -> None:
        detail = self.advance("observability")
        assert detail["detail"].startswith("transaction search active"), detail
        assert self.aws.xray.updates == ["CloudWatchLogs"]

    def edge_run_policy(self, edge: Edge) -> None:
        self.advance("policy", ["policy_log_group"])
        assert self.resources()["policy_log_group_unavailable_reason"] == ""

    def edge_finalize(self, edge: Edge) -> None:
        self.run_until(None)
        job = self.job()
        assert job.status == "succeeded", job.error
        assert all(s["status"] == "succeeded" for s in wb.job_stages(job)), wb.job_stages(job)
        assert self.row().bootstrap_status == "ready"
        self.assert_recorded(list(wb.REQUIRED_RESOURCE_KEYS))
        self.assert_no_secret()

    # -- edges: failures + resume -----------------------------------------------------
    def edge_validate_fails_denied(self, edge: Edge) -> None:
        def denied(**_kw):
            raise _client_error("AccessDeniedException", "DescribeRepositories")

        self.aws.ecr.describe_repositories = denied  # type: ignore[method-assign]
        body = self.expect(self.post_bootstrap(), edge, status=202)
        self.job_id = body["job_id"]
        self.run_until(None)
        job = self.job()
        records = self.stage_records()
        assert job.status == "failed" and records["validate-access"]["status"] == "failed"
        detail = records["validate-access"]["detail"]
        assert f"ecr DescribeRepositories failed in {REGION}: AccessDeniedException" in detail
        assert DENIAL_TEXT in detail and job.error == detail
        assert all(s["status"] == "pending" for n, s in records.items() if n != "validate-access")
        assert self.row().bootstrap_status == "failed"
        assert self.resources() == {}, "a refused probe must create / record nothing"
        assert self._created() == {k: 0 for k in self._created()}
        self.assert_no_secret()

    def edge_storage_fails_foreign_bucket(self, edge: Edge) -> None:
        def foreign(**_kw):
            raise _client_error("BucketAlreadyExists", "CreateBucket")

        before = self.resources()
        self.aws.s3.create_bucket = foreign  # type: ignore[method-assign]
        self.run_until(None)
        job = self.job()
        records = self.stage_records()
        assert job.status == "failed" and records["storage"]["status"] == "failed"
        assert "exists in another account" in records["storage"]["detail"]
        assert records["model-credential"]["status"] == "succeeded"
        assert records["codebuild"]["status"] == "pending"
        assert self.row().bootstrap_status == "failed"
        assert self.resources() == before, "a failed stage must leave the map as it was"
        self.map_before_failure = before
        self.assert_no_secret()

    def edge_resume_from_early(self, edge: Edge) -> None:
        self.heal()
        self._start_full(edge, expect_carried=[])
        self.run_until("iam")
        self.assert_interrupted_after("validate-access")
        assert self.stage_records()["validate-access"]["detail"] == PROBE_DETAIL

    def edge_resume_from_mid(self, edge: Edge) -> None:
        self.heal()
        roles_before = list(self.aws.iam.created)
        self._start_full(edge, expect_carried=["validate-access", "iam", "model-credential"])
        self.run_until("codebuild")
        self.assert_interrupted_after("storage")
        self.assert_recorded(["artifacts_bucket", "ecr_repo", "ecr_repo_uri"])
        # nothing provisioned twice: the resumed run read the recorded identifiers
        assert self.aws.iam.created == roles_before
        assert len(self.aws.s3.created) == 1 and self.aws.ecr.created == ["launchpad-agents"]
        for key, value in (self.map_before_failure or {}).items():
            assert self.resources().get(key) == value, key

    # -- edges: removal -----------------------------------------------------------------
    def edge_detach_registered(self, edge: Edge) -> None:
        self.expect(self.admin.delete(f"/api/workspaces/{self.ws_id}"), edge)
        assert self.row() is None
        assert self.ws_id not in {w["id"] for w in self.admin.get("/api/workspaces").json()[
            "workspaces"]}

    def _purge(self, edge: Edge, *, expect_keys: set[str] | None = None,
               expect_jobs: bool = False) -> None:
        jobs_before = len(self.jobs())
        body = self.expect(self.admin.post(f"/api/workspaces/{self.ws_id}/purge"), edge)
        assert body["purged"] is True and body["dry_run"] is False
        if expect_jobs:
            assert body["rows"].get("jobs", 0) >= 1 and jobs_before >= 1, body["rows"]
        else:
            assert "jobs" not in body["rows"], body["rows"]
        if expect_keys is not None:
            assert set(body["resource_keys"]) == expect_keys, body["resource_keys"]
        assert self.row() is None and self.jobs() == []

    def edge_purge_registered(self, edge: Edge) -> None:
        self._purge(edge, expect_keys=set())

    def edge_purge_probed(self, edge: Edge) -> None:
        self._purge(edge, expect_keys=set(), expect_jobs=True)

    def edge_purge_failed_early(self, edge: Edge) -> None:
        self._purge(edge, expect_keys=set(), expect_jobs=True)

    def edge_purge_failed_mid(self, edge: Edge) -> None:
        recorded = {k for k, v in (self.map_before_failure or {}).items() if v}
        assert {"execution_role_arn", "gateway_role_arn", "model_credential_arn"} <= recorded
        self._purge(edge, expect_keys=recorded, expect_jobs=True)

    # -- edges: refusals ----------------------------------------------------------------
    def _conflict(self, edge: Edge, body: dict | None, status: str) -> None:
        out = self.expect(self.post_bootstrap(body), edge)
        assert out["detail"]["bootstrap_status"] == status, out

    def edge_bootstrap_conflict_running(self, edge: Edge) -> None:
        self._conflict(edge, None, "bootstrapping")

    def edge_bootstrap_conflict_ready(self, edge: Edge) -> None:
        self._conflict(edge, None, "ready")

    def edge_validate_only_conflict_ready(self, edge: Edge) -> None:
        self._conflict(edge, {"validate_only": True}, "ready")

    def edge_unsupported_stages(self, edge: Edge) -> None:
        out = self.expect(self.post_bootstrap({"stages": ["iam"]}), edge)
        assert out["detail"]["supported"] == ["validate-access"]
        assert self.row().bootstrap_status == "registered"

    def edge_member_cannot_bootstrap(self, edge: Edge) -> None:
        self.expect(self.post_bootstrap(None, client=self.bob), edge)
        self.expect(self.post_bootstrap({"validate_only": True}, client=self.bob), edge)
        assert self.row().bootstrap_status == "registered"

    def _write_refused(self, edge: Edge) -> None:
        res = self.admin.post(
            "/api/agents",
            json={"name": f"ws-{self.ws_id}", "method": "zip_runtime", "system_prompt": "hi"},
            headers={"X-Workspace": self.ws_id},
        )
        out = self.expect(res, edge)
        assert out["detail"]["bootstrap_status"] == self.row().bootstrap_status

    edge_write_refused_registered = _write_refused
    edge_write_refused_failed = _write_refused

    def _purge_refused(self, edge: Edge, reason: str) -> None:
        out = self.expect(self.admin.post(f"/api/workspaces/{self.ws_id}/purge"), edge)
        assert out["detail"]["reason"] == reason, out
        assert self.row() is not None

    def edge_purge_refused_bootstrapping(self, edge: Edge) -> None:
        self._purge_refused(edge, "bootstrapping")

    def edge_purge_refused_ready(self, edge: Edge) -> None:
        self._purge_refused(edge, "ready")

    def _detach_in_use(self, edge: Edge) -> None:
        out = self.expect(self.admin.delete(f"/api/workspaces/{self.ws_id}"), edge)
        assert out["detail"]["rows"].get("jobs", 0) >= 1, out
        assert self.row() is not None

    edge_detach_in_use_probed = _detach_in_use
    edge_detach_in_use_failed = _detach_in_use
    edge_detach_in_use_ready = _detach_in_use

    def _bootstrap_gone(self, edge: Edge) -> None:
        self.expect(self.post_bootstrap(), edge)
        self.expect(self.post_bootstrap({"validate_only": True}), edge)
        assert self.row() is None

    edge_bootstrap_after_detach = _bootstrap_gone
    edge_bootstrap_after_purge = _bootstrap_gone


@pytest.fixture
def make_world(admin, ada, bob, seams):
    """A fresh world (fresh fake AWS, a new workspace id) per call — the walk shrinker
    replays on fresh worlds."""
    _ada_id, ada_client = ada
    _bob_id, bob_client = bob
    return lambda: WorkspaceWorld(
        {"admin": admin, "ada": ada_client, "bob": bob_client}, seams)


@pytest.fixture
def world(make_world) -> WorkspaceWorld:
    return make_world()


def test_every_edge_is_driven_or_declared():
    assert_tables_explicit(WorkspaceWorld)


@pytest.mark.parametrize("path", params_for(WorkspaceWorld))
def test_workspace_path(world, path):
    world.run(path)


@pytest.mark.slow
@pytest.mark.skipif(not SOAK, reason="soak variant — LAUNCHPAD_SOAK=1")
@pytest.mark.parametrize("path", params_for(WorkspaceWorld, include_covered_pairs=True))
def test_soak_workspace_every_edge_pair(world, path):
    world.run(path)


@pytest.mark.parametrize("seed", walk_seeds())
def test_workspace_walk(make_world, seed):
    """Seeded random walk (20–60 edges) through every enabled edge — refusal loops included —
    on a fresh world; a failure is shrunk on fresh worlds and reported with its minimal edge
    sequence + repro-hash (``tests/graph/walks.py``)."""
    run_walk(make_world, WorkspaceWorld, GRAPHS["workspace"], seed)


@pytest.mark.parametrize("path", neighbourhood.params_for(WorkspaceWorld, GRAPHS))
def test_workspace_neighbourhood(world, path):
    world.run(path)


@pytest.mark.slow
@pytest.mark.skipif(not SOAK, reason="soak variant — LAUNCHPAD_SOAK=1")
@pytest.mark.parametrize(
    "path", neighbourhood.params_for(WorkspaceWorld, GRAPHS, neighbourhood.SOAK_KINDS))
def test_soak_workspace_neighbourhood_pairs(world, path):
    world.run(path)
