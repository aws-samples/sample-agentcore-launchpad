# ruff: noqa: F811  — pytest fixtures are imported by name from the world module
"""The fault world of the WORKSPACE graph (r17-ws; testing.md §6.9 / §6.10).

``WorkspaceFaults`` = ``WorkspaceWorld`` + ``FaultMixin``: for ONE step a recorded / botocore
shape is served at the bootstrap stage's AWS seam — the first call the stage issues (`ecr
DescribeRepositories` for `validate-access`, the N17-obs112 shape; `iam CreateRole`; the
Identity provider read; `s3 CreateBucket` / `ecr CreateRepository`; `codebuild CreateProject`;
`CreateGateway`; the xray / logs calls of the degrading stages) or at the two service-level
stubs (the registry default import, the Skill Lab worker). Per stage the lead asked for four
injections, each a family of ``faults.FAMILIES``:

* **AccessDenied** (`access_denied`: `AccessDeniedException` / `AccessDenied`, armed for the
  whole step so `retry_access_denied`'s propagation retry sees it again) — plus the
  **AssumeRole failure** variant (`at` suffix `/assume_role`: the same `ClientError` with
  `operation_name == "AssumeRole"`, what botocore raises when the spoke credentials cannot be
  refreshed before the stage's first signed call);
* **region-unsupported** (`region_unsupported`: botocore `UnknownServiceError` — endpoint
  resolution fails before any connect — and `UnrecognizedClientException` —
  `workspace_bootstrap._REGION_UNSUPPORTED_CODES`); the resolved-but-unreachable endpoint
  (`ws_endpoint_unreachable`, botocore `EndpointConnectionError`) rides with the transport
  `timeout` family since N17-obs200: retried once, then the DISTINCT "unreachable from this host
  … network/DNS, not the region" sentence, never the region verdict;
* **interruption mid-stage** (`interrupted`: a `BaseException` from the seam — AFTER the write
  for a create call, so the resume must ADOPT, not re-create);
* and the transient classes the other graphs serve (`5xx`, `throttle`, `timeout`).

Oracles after a faulted stage (every one cites architecture.md "Workspaces" or api.md
"Console Workspaces API — bootstrap"):

* **stage status persisted** — the job `failed`, the stage `failed` with a detail that names the
  error (for `validate-access` the stage's own sentence: the denial text, "region not
  supported", "transient — retry" for a throttle / timeout after the one retry — N17-obs132 —
  the AssumeRole diagnostic), the workspace `failed`; a degrading stage instead
  `succeeded` with `unavailable · <code>` / `defaults not imported (<class>)` and the job went
  on; an interrupted job is left `running` with the stage `running`;
* **no half-recorded resource ids** — the resource map after the failure equals the map the
  previous stage left (a stage records per stage, never per call), and after the resume no fake
  resource was created twice (the resume adopted what the interrupted write made);
* **job resumable** — `POST /api/workspaces/{id}/bootstrap` answers 202 again, the stages that
  succeeded are carried forward, and the run reaches the planned node (`fault_dst == edge.dst`:
  the recovery converges, the walk continues as if the edge had succeeded);
* **error text has no secrets** — the Global Bedrock API key never appears in the job log, the
  job error, a stage detail or the row.

A refusal edge gets the seam-faulted axis: every fake method armed, the refusal attempted, the
seam never reached (a workspace refusal is decided on the ledger).
"""

from __future__ import annotations

from typing import Any

import pytest
from botocore.exceptions import ClientError

from app.services import workspace_bootstrap as wb
from tests.graph.fault_seams import FaultMixin
from tests.graph.faults import Interrupted, Outcome, Shape, shapes, with_at
from tests.graph.paths import Edge
from tests.graph.test_graph_workspace import (
    DENIAL_TEXT,
    FAST,
    PROBE_DETAIL,
    PSEUDO_REGISTRY,
    PSEUDO_SKILL_LAB,
    WorkspaceWorld,
)

#: the seam(s) a shape is served at, per stage — the first AWS call the stage issues, or the
#: write whose interruption the resume must adopt
STAGE_SEAMS: dict[str, tuple[tuple[str, str], ...]] = {
    # sts:GetCallerIdentity cannot be denied (implicit allow) — it is the ASSUME-ROLE seam only
    "validate-access": (("ecr", "describe_repositories"),),
    "iam": (("iam", "create_role"),),
    "model-credential": (("bedrock-agentcore-control", "get_api_key_credential_provider"),
                         ("bedrock-agentcore-control", "create_api_key_credential_provider")),
    "storage": (("s3", "create_bucket"), ("ecr", "create_repository")),
    "codebuild": (("codebuild", "create_project"),),
    "gateway": (("bedrock-agentcore-control", "create_gateway"),),
    "registry": (PSEUDO_REGISTRY,),
    "memory": (),  # a non-hub-colocated workspace makes no AWS call here
    "skill-lab": (PSEUDO_SKILL_LAB,),
    "observability": (("xray", "update_trace_segment_destination"),
                      ("logs", "describe_resource_policies")),
    "policy": (("logs", "create_log_group"),),
    "finalize": (),  # ledger only
}
#: the stages whose failure degrades the feature instead of failing the job
DEGRADING = frozenset({"registry", "skill-lab", "observability", "policy"})
#: the stage each edge runs (the probe edges run `validate-access` alone)
STAGE_OF_EDGE: dict[str, str] = {
    "validate_only": "validate-access",
    "validate_only_again": "validate-access",
    "run_validate": "validate-access",
    "run_validate_from_probed": "validate-access",
    "resume_from_early": "validate-access",
    "run_iam": "iam",
    "run_model_credential": "model-credential",
    "run_storage": "storage",
    "resume_from_mid": "storage",
    "run_codebuild": "codebuild",
    "run_gateway": "gateway",
    "run_registry": "registry",
    "run_memory": "memory",
    "run_skill_lab": "skill-lab",
    "run_observability": "observability",
    "run_policy": "policy",
    "finalize": "finalize",
}
#: the edges that START a job (POST … /bootstrap) before running it
STARTS_JOB = frozenset({"run_validate", "run_validate_from_probed", "resume_from_early",
                        "resume_from_mid"})
PROBE_EDGES = frozenset({"validate_only", "validate_only_again"})
ASSUME_ROLE_SUFFIX = "/assume_role"
#: the stage-level families served (the SSE / OpenSearch / absent / conflict families have no
#: seam on this graph)
STAGE_FAMILIES = {
    "5xx": ("s3_internal_error",),
    "throttle": ("s3_slow_down",),
    "timeout": ("rt_read_timeout", "ws_endpoint_unreachable"),
    "access_denied": ("rt_access_denied", "s3_access_denied"),
    "region_unsupported": ("ws_unknown_service", "ws_unrecognized_client"),
    "interrupted": ("process_death",),
}
WRITES = frozenset({"jobs", "workspace_row"})
#: the third `validate-access` verdict (N17-obs132): a probe retried once, then named transient
TRANSIENT_TEXT = "transient — retry"
#: the fourth (N17-obs200): the endpoint resolved but could not be reached — retried once, then
#: named as the hub's network / DNS, with the host redacted to service + region
UNREACHABLE_TEXT = "unreachable from this host"
UNREACHABLE_VERDICT = "network/DNS, not the region"
CLAUSE_RECORDS = ("architecture.md 'Workspaces' — a stage that fails leaves the job failed with "
                  "the stage's detail, the workspace failed, and the resource map exactly as the "
                  "previous stage left it; the next POST … /bootstrap is a resume")
CLAUSE_INTERRUPTED = ("architecture.md 'Workspaces' — an interrupted job is left running; the "
                      "resume continues from the first stage that has not succeeded, reading the "
                      "identifiers already recorded, so no resource is provisioned twice")
CLAUSE_DEGRADE = ("architecture.md 'Workspaces' — skill-lab / observability / policy degrade with "
                  "a reason instead of failing; registry imports the default records best-effort")
CLAUSE_PROBE = ("api.md 'Console Workspaces API — bootstrap' — a validate-only run provisions "
                "nothing, hands the row back the status it had, the job's terminal status is the "
                "verdict with the stage's own sentence")


def _shape_pool(family: str) -> list[Shape]:
    names = STAGE_FAMILIES.get(family, ())
    by_name = {s.name: s for s in shapes()}
    return [by_name[n] for n in names]


def _assume_role_error() -> ClientError:
    return ClientError(
        {"Error": {"Code": "AccessDenied",
                   "Message": "User: arn:aws-cn:sts::444455556666:assumed-role/launchpad-cn-ops/i "
                              "is not authorized to perform: sts:AssumeRole on resource: "
                              "arn:aws-cn:iam::111122223333:role/launchpad-workspace-role"},
         "ResponseMetadata": {"HTTPStatusCode": 403}},
        "AssumeRole",
    )


def _operation(method: str) -> str:
    return "".join(part.capitalize() for part in method.split("_"))


class WorkspaceFaults(FaultMixin, WorkspaceWorld):
    SEAMS = ("stage",)

    def __init__(self, clients, seams) -> None:
        super().__init__(clients, seams)
        self._init_faults()
        self.seams.calls = self.seam_calls

    # -- planning ---------------------------------------------------------------------
    @classmethod
    def fault_shapes(cls, edge: Edge, family: str) -> list[Shape]:
        if edge.is_refusal:
            return _shape_pool(family)[:1]  # one representative: the seam must not be reached
        stage = STAGE_OF_EDGE.get(edge.id)
        if stage is None:
            return []  # the failure / removal / register edges inject their own shape
        seams = STAGE_SEAMS[stage]
        if not seams:
            return []
        out: list[Shape] = []
        for shape in _shape_pool(family):
            for service, method in seams:
                out.append(with_at(shape, f"{service}.{method}"))
        if family == "access_denied":
            # the credential refresh fails before the stage's FIRST signed call
            service, method = (("sts", "get_caller_identity") if stage == "validate-access"
                               else seams[0])
            out.append(with_at(_shape_pool(family)[0],
                               f"{service}.{method}{ASSUME_ROLE_SUFFIX}"))
        return out

    @classmethod
    def fault_dst(cls, edge: Edge, shape: Shape) -> str:
        return edge.dst  # the recovery converges: the step ends where the edge would have

    # -- arming -----------------------------------------------------------------------
    def _arm(self, shape: Shape) -> None:
        at = shape.at
        assume = at.endswith(ASSUME_ROLE_SUFFIX)
        if assume:
            at = at[: -len(ASSUME_ROLE_SUFFIX)]
        service, method = at.split(".", 1)
        operation = _operation(method)
        if assume:
            raise_ = _assume_role_error
        else:
            raise_ = lambda: shape.exception(operation)  # noqa: E731
        # an AccessDenied stays armed for the whole step (retry_access_denied re-runs the
        # stage); an interruption fires once — the process died, the resume finds a healthy AWS
        self.seams.calls = self.seam_calls  # fault_step rebinds the list per step
        self.seams.arm(service, method, raise_, once=shape.kind == "interrupt")

    def _arm_all(self, shape: Shape) -> None:
        self.seams.calls = self.seam_calls
        self.seams.arm("*", "*", lambda: shape.exception("AnyOperation"))

    def _disarm(self) -> None:
        self.seams.disarm()

    # -- the hook ----------------------------------------------------------------------
    def fault(self, edge: Edge, shape: Shape) -> Outcome:
        if edge.is_refusal:
            try:
                return self.refusal_with_faulted_seams(edge, shape, lambda: self._arm_all(shape))
            finally:
                self._disarm()
        stage = STAGE_OF_EDGE[edge.id]
        self._arm(shape)
        try:
            if edge.id in PROBE_EDGES:
                return self._faulted_probe(edge, shape)
            if shape.kind == "interrupt":
                return self._interrupted_stage(edge, stage, shape)
            if stage in DEGRADING:
                return self._degraded_stage(edge, stage, shape)
            return self._failed_stage(edge, stage, shape)
        finally:
            self._disarm()

    # -- the step shapes --------------------------------------------------------------
    def _start_if_needed(self, edge: Edge) -> None:
        if edge.id in STARTS_JOB:
            self.heal()
            carried = (["validate-access", "iam", "model-credential"]
                       if edge.id == "resume_from_mid" else [])
            self._start_full(edge, expect_carried=carried)

    def _expected_detail_fragments(self, stage: str, shape: Shape) -> list[str]:
        """What the stage detail must say for the shape — `validate-access` has its own
        sentences, every other stage reports the raw exception class / code."""
        if shape.at.endswith(ASSUME_ROLE_SUFFIX):
            return ["cannot assume this workspace's role"]
        if stage != "validate-access":
            return [shape.code or type(shape.exception()).__name__]
        if shape.family == "region_unsupported":
            return ["region not supported"]
        if shape.family == "access_denied":
            return [DENIAL_TEXT, shape.code or ""]
        if shape.kind == "endpoint":
            # N17-obs200 (fixed cn/r17-w): the resolved-but-unreachable endpoint is retried once
            # and then named as the hub's network / DNS — the fourth verdict; neither the region
            # sentence nor the plain transient one
            return ["EndpointConnectionError", UNREACHABLE_TEXT, UNREACHABLE_VERDICT]
        # N17-obs132 (fixed cn/r17-v): a transport timeout / throttle / 5xx is retried once and
        # then named as transient — the third verdict of architecture.md "Workspaces", neither
        # the denial sentence nor the region one
        return [shape.code or type(shape.exception()).__name__, TRANSIENT_TEXT]

    def _assert_failed(self, stage: str, shape: Shape, map_before: dict[str, Any]) -> str:
        job = self.job()
        records = self.stage_records()
        assert job.status == "failed", (stage, shape.label, job.status, job.error)
        assert records[stage]["status"] == "failed", records[stage]
        detail = records[stage]["detail"]
        for fragment in self._expected_detail_fragments(stage, shape):
            assert fragment in detail, (stage, shape.label, fragment, detail)
        if stage == "validate-access" and shape.family in ("5xx", "throttle", "timeout"):
            assert "region not supported" not in detail, (
                f"a transient {shape.label} at validate-access was reported as an unsupported "
                f"region: {detail}")
            assert DENIAL_TEXT not in detail, (
                f"a transient {shape.label} at validate-access was reported as a denial: {detail}")
            if shape.kind == "endpoint":  # N17-obs200: its own sentence, not the transient one
                assert TRANSIENT_TEXT not in detail, (
                    f"an unreachable endpoint at validate-access was reported as a plain "
                    f"transient: {detail}")
                assert "https://" not in detail and shape.endpoint not in detail, (
                    f"the endpoint URL leaked into the stage detail: {detail}")
        assert job.error == detail
        assert self.resources() == map_before, (
            "a failed stage changed the resource map", stage, shape.label)
        self.assert_no_secret()
        return detail

    def _failed_stage(self, edge: Edge, stage: str, shape: Shape) -> Outcome:
        self._start_if_needed(edge)
        map_before = self.resources()
        succeeded_before = [n for n, s in self.stage_records().items()
                            if s["status"] == "succeeded"]
        self.run_until(None)
        detail = self._assert_failed(stage, shape, map_before)
        assert self.row().bootstrap_status == "failed"
        assert self.seam_calls, "the shape was never served"
        # recovery: heal, POST again (a resume carrying the succeeded stages), reach the node
        self._disarm()
        self._start_full(edge, expect_carried=succeeded_before)
        self.advance(stage)
        return Outcome(dst=edge.dst, writes=WRITES, clause=CLAUSE_RECORDS,
                       observation=f"{stage} failed: {detail[:120]}")

    def _degraded_stage(self, edge: Edge, stage: str, shape: Shape) -> Outcome:
        self._start_if_needed(edge)
        record = self.advance(stage)
        detail = record["detail"]
        if stage == "registry":
            assert "defaults not imported" in detail, detail
            assert self.resources()["registry_id"] == f"local-{self.ws_id}"
        else:
            assert detail.startswith("unavailable ·"), (stage, shape.label, detail)
            code = ("AccessDenied" if shape.at.endswith(ASSUME_ROLE_SUFFIX)
                    else shape.code or type(shape.exception()).__name__)
            assert code in detail, (stage, shape.label, detail)
        if stage == "policy":
            resources = self.resources()
            assert resources["policy_log_group"] == ""
            assert resources["policy_log_group_unavailable_reason"]
        assert self.seam_calls, "the shape was never served"
        self.assert_no_secret()
        return Outcome(dst=edge.dst, writes=WRITES, clause=CLAUSE_DEGRADE,
                       observation=f"{stage} degraded: {detail[:120]}")

    def _interrupted_stage(self, edge: Edge, stage: str, shape: Shape) -> Outcome:
        self._start_if_needed(edge)
        map_before = self.resources()
        with pytest.raises(Interrupted):
            wb.execute_bootstrap_job(self.job_id, timeouts=FAST)
        job = self.job()
        records = self.stage_records()
        assert job.status == "running", job.status  # what a process death leaves behind
        assert records[stage]["status"] == "running", records[stage]
        assert self.row().bootstrap_status == "bootstrapping"
        gained = {k: v for k, v in self.resources().items() if map_before.get(k) != v}
        # architecture.md: `registry` records the local identifier, THEN imports the default
        # records best-effort — an interruption inside the import legitimately leaves the id
        allowed = {"registry_id", "registry_unavailable_reason"} if stage == "registry" else set()
        assert set(gained) <= allowed, ("an interrupted stage recorded half its ids", gained)
        self._disarm()
        self.advance(stage)  # the resume adopts what the interrupted write made
        self._assert_nothing_twice()
        return Outcome(dst=edge.dst, writes=WRITES, clause=CLAUSE_INTERRUPTED,
                       observation=f"{stage} interrupted at {shape.at} and resumed")

    def _faulted_probe(self, edge: Edge, shape: Shape) -> Outcome:
        before = self.row().bootstrap_status
        created_before = self._created()
        body = self.expect(self.post_bootstrap({"validate_only": True}), edge, status=202)
        self.job_id = body["job_id"]
        if shape.kind == "interrupt":
            with pytest.raises(Interrupted):
                wb.execute_bootstrap_job(self.job_id, timeouts=FAST)
            assert self.job().status == "running"
            self._disarm()
            self.run_until(None)
            assert self.job().status == "succeeded", self.job().error
        else:
            self.run_until(None)
            self._assert_failed("validate-access", shape, {})
            assert self.row().bootstrap_status == before, (
                "a failed probe must hand the status back", self.row().bootstrap_status)
            # recovery: the next probe passes once the seam is healthy
            self._disarm()
            body = self.expect(self.post_bootstrap({"validate_only": True}), edge, status=202)
            self.job_id = body["job_id"]
            self.run_until(None)
        assert self.job().status == "succeeded"
        assert self.stage_records()["validate-access"]["detail"] == PROBE_DETAIL
        assert self.row().bootstrap_status == before
        assert self._created() == created_before, "a probe provisioned something"
        self.assert_no_secret()
        return Outcome(dst=edge.dst, writes=WRITES, clause=CLAUSE_PROBE)

    # -- helpers -----------------------------------------------------------------------
    def _assert_nothing_twice(self) -> None:
        a = self.aws
        for label, created in (("roles", a.iam.created), ("repos", a.ecr.created),
                               ("projects", a.codebuild.created),
                               ("gateways", a.control.created_gateways)):
            assert len(created) == len(set(created)), (label, created)
        buckets = [c["bucket"] for c in a.s3.created]
        assert len(buckets) == len(set(buckets)), buckets
        creates = [c for c in a.control.provider_calls if c.startswith("create:")]
        assert len(creates) == len(set(creates)), creates

