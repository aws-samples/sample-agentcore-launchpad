# ruff: noqa: F811  — pytest fixtures are imported by name from the world modules
"""The seam hooks of the fault-injected walks (round 6, r17-l): one ``World.fault(edge, shape)``
per lifecycle graph, serving a recorded shape (``tests/graph/faults.py``) at the data-plane seam
the edge reaches, then judging the outcome with the executor's oracles plus the fault layer's own
(no half-written row, the envelope, the terminal state, the recovery path).

Seams per graph (what the world's fixture stubs, re-pointed for ONE step):

* **agent** — the deploy stages (the recording fake stage's ``hooks[stage]`` raises the shape:
  the real `_stage_provision` / `_stage_deploy` / `_stage_register` let the same botocore
  exceptions propagate), the Runtime delete (``zip_method.delete_agent_resources``), the chat
  producer (``chat_service.invoke_agent_events``) and, for the SSE-level shapes, the real invoke
  chain over a fake data plane (``invoke_service.data_client``);
* **chat_session** — the gated producer (a ``FaultScript`` that raises after its head), the
  `/v1` sync wrapper (``invoke_service.rt.invoke_runtime_text``), the StopRuntimeSession plane
  and the real chain for the SSE-level shapes;
* **eval_run** — the worker's replay invoke and judge (``stub_environment(invoke_errors=…,
  judge=…)``);
* **memory_record** — the extractor's OpenSearch (``World.deps.opensearch`` behind a
  ``FaultyOpenSearch``) and the hub backend's (a backend built over the faulty fake for one call);
* **reference** — the memory store's S3 / OpenSearch purge seams, the KB cleanup steps and the
  agent's Runtime delete.

Every refusal edge of these graphs gets the "seam faulted" axis (the round-4 matrix's new
axis): the shape is armed on every seam of the world, the refusal is attempted through its
normal edge method, and the oracle is that the seam was never reached (``seam_calls == 0``) and
nothing was written — unless the contract says the read behind the refusal IS the seam (the
memory record delete reads the stored fields before its 404), in which case the fault's own
envelope is the answer.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from typing import Any

import pytest
from botocore.exceptions import ClientError
from fastapi.testclient import TestClient

import app.evaluation.service as svc
import app.routers.agents as agents_router
import app.services.chat as chat_service
import app.services.invoke as invoke_service
from app.core.db import SessionLocal
from app.core.errors import (
    AWS_ERROR_MAP,
    TRANSPORT_ERROR_MESSAGES,
    aws_error_code,
    mapped_transport_error,
)
from app.deployer import pipeline
from app.deployer.pipeline import execute_deploy_job
from app.models.ledger import Agent, ChatMessage
from app.services import live_turns, memory_backend
from app.services.agentcore import runtime as rt
from app.services.memory_selfhosted import SelfHostedMemoryBackend
from app.services.strands_eval.jobs_batch import TRANSIENT_SCENARIO_ATTEMPTS
from tests.evaluation.test_runs_flow import stub_environment
from tests.faults.conftest import FlakyJudge, _bedrock_5xx, _throttle
from tests.graph import coverage
from tests.graph.faults import (
    JOB_TABLES,
    RUN_TABLES,
    TURN_TABLES,
    EnvelopeViolation,
    HalfWrite,
    Interrupted,
    LeakedSeamText,
    Outcome,
    Shape,
    shapes,
    with_at,
)
from tests.graph.paths import Edge
from tests.graph.test_graph_agent import AgentWorld
from tests.graph.test_graph_chat_session import BUDGET_S, ChatWorld, _frames, _session
from tests.graph.test_graph_eval_run import ITEMS, EvalRunWorld, _results
from tests.graph.test_graph_memory_record import MemoryRecordWorld
from tests.graph.test_graph_reference import ReferenceWorld
from tests.memory_fakes import FakeSQS
from tests.property.ledger_invariants import scan_memory_index
from tests.property.test_chat_concurrency import Script, _deltas
from tests.property.test_memory_merge_properties import JOBS, MEM, FaultyOpenSearch

#: text a console envelope must never carry (the recorded IAM 403 body / OpenSearch bodies /
#: botocore's framing) — `opensearch_error_handler`: "a static, per-code sentence, never str(exc)"
LEAK_MARKERS = (
    "is not authorized",
    "assumed-role",
    "root_cause",
    "An error occurred (",
    "Traceback",
)


def documented_unhandled(exc: BaseException) -> bool:
    """api.md "Errors": "Any other AWS error code (e.g. `InternalServerException`) remains an
    unhandled 500 with the traceback in the backend log" — the one documented bare 500: a
    botocore `ClientError` whose code is outside `AWS_ERROR_MAP`. A transport exception (504 /
    502 since N17-obs94) or a wrapper's `RuntimeError` is NOT covered by that sentence."""
    return isinstance(exc, ClientError) and aws_error_code(exc) not in AWS_ERROR_MAP


class Patcher:
    """setattr with restore — the fault layer re-points a seam for one step only."""

    def __init__(self) -> None:
        self.saved: list[tuple[Any, str, Any]] = []

    def set(self, obj: Any, name: str, value: Any) -> None:
        self.saved.append((obj, name, getattr(obj, name)))
        setattr(obj, name, value)

    def restore(self) -> None:
        for obj, name, value in reversed(self.saved):
            setattr(obj, name, value)
        self.saved.clear()


class TurnRecorder:
    """Records every detached chat turn started while installed, so a wait for a turn to end is
    a join on its driver thread (the N16-obs89d rule), never a poll."""

    def __init__(self) -> None:
        self.turns: list[Any] = []
        self.lock = threading.Lock()

    def install(self, patcher: Patcher) -> None:
        real_start = live_turns.LiveTurnRegistry.start
        recorder = self

        def recording_start(registry, live, target, args, **kw):
            started = real_start(registry, live, target, args, **kw)
            with recorder.lock:
                recorder.turns.append(started)
            return started

        patcher.set(live_turns.LiveTurnRegistry, "start", recording_start)

    def join(self, agent_id: str, session_id: str) -> None:
        deadline = time.monotonic() + BUDGET_S
        with self.lock:
            mine = [
                t
                for t in self.turns
                if getattr(t, "agent_id", None) == agent_id
                and getattr(t, "session_id", None) == session_id
            ]
        for live in mine:
            thread = live.thread
            if thread is None or thread is threading.current_thread():
                continue
            thread.join(timeout=max(0.0, deadline - time.monotonic()))
            assert not thread.is_alive(), "the faulted turn's driver did not finish in the budget"


class FaultPlane:
    """A data-plane client for the SSE-level shapes: ``invoke_agent_runtime`` answers the shape's
    raw lines (an SSE body, or a JSON body for ``at == "json"``); ``stop_runtime_session`` is a
    recorder (the chat world's plane keeps its own)."""

    def __init__(self, shape: Shape, stop: Any = None) -> None:
        self.shape, self.stop, self.calls = shape, stop, 0

    def invoke_agent_runtime(self, **params: Any) -> dict[str, Any]:
        self.calls += 1
        lines = [line.encode("utf-8") for line in self.shape.lines]
        if self.shape.at == "json":
            raw = b"".join(lines)

            class Body:
                def read(self_inner) -> bytes:
                    return raw

                def close(self_inner) -> None:
                    pass

            return {"response": Body(), "contentType": "application/json"}
        sse: list[bytes] = []
        for line in lines:
            sse += [line, b""]

        class Stream:
            def iter_lines(self_inner, chunk_size=None):
                yield from sse

            def close(self_inner) -> None:
                pass

        return {"response": Stream(), "contentType": "text/event-stream; charset=utf-8"}

    def stop_runtime_session(self, **params: Any) -> dict[str, Any]:
        if self.stop is not None:
            return self.stop.stop_runtime_session(**params)
        return {"runtimeSessionId": params["runtimeSessionId"], "statusCode": 200}


def _runtime_exception_shapes(family: str) -> list[Shape]:
    """The Runtime seam's exception shapes of a family (what a botocore call / a stream raises)."""
    return shapes(family=family, seam="runtime", kinds=("client", "transport", "frame"))


def _runtime_client_shapes(family: str) -> list[Shape]:
    return shapes(family=family, seam="runtime", kinds=("client", "transport"))


def _representative(family: str, seams: tuple[str, ...]) -> list[Shape]:
    """One shape per family for the refusal axis — from the world's own seams (the seam must not
    be reached, so one cell per family keeps the grid honest and bounded)."""
    pool = [
        s
        for s in shapes(
            family=family, kinds=("client", "transport", "frame", "opensearch", "os_transport")
        )
        if s.seam in seams
    ]
    return pool[:1]


def _envelope(res: Any, status: int, code: str, *, what: str) -> dict[str, Any]:
    assert res.status_code == status, (what, res.status_code, res.text[:400])
    body = res.json()
    assert body.get("code") == code, (what, body)
    assert set(body) >= {"code", "message", "detail"}, (what, body)
    # api.md "Errors": a console `aws.*` envelope's `message` IS the AWS message (prefix stripped)
    # — the IAM sentence with the role ARN is the operator's to read; only /v1 masks it (asserted
    # by the /v1 handler). Every OTHER envelope is a platform sentence and must carry no seam text.
    if not code.startswith("aws."):
        for marker in LEAK_MARKERS:
            if marker in res.text:
                raise LeakedSeamText((what, marker, res.text[:400]))
    else:
        assert "Traceback" not in res.text and "An error occurred (" not in res.text, res.text[:300]
    return body


def _mapped(shape: Shape) -> tuple[int, str] | None:
    """The envelope the contract names for an exception shape: api.md "Errors" for a
    `ClientError` code (`AWS_ERROR_MAP`), the transport family since N17-obs94 (504
    `aws.timeout` for the read timeout — `core.errors.mapped_transport_error`); None where
    no sentence maps the shape."""
    if shape.kind == "client":
        return AWS_ERROR_MAP.get(shape.code or "")
    if shape.kind == "transport":
        return mapped_transport_error(shape.exception())
    if shape.kind == "frame":
        return 502, "agent.runtime_error"  # N17-obs95 (sync entrances only)
    return None


def _assert_envelope_detail(body: dict[str, Any], shape: Shape, *, operation: str, public: bool,
                            what: str) -> None:
    """`detail` per shape kind: a `ClientError` carries `{aws_error_code[, operation]}` (the
    operation on the console only); a transport exception `{error_type}` on both sides, with
    the static message and no endpoint URL anywhere (api.md "Transport failures")."""
    if shape.kind == "client":
        expected = {"aws_error_code": shape.code}
        if not public:
            expected["operation"] = operation
        assert body["detail"] == expected, (what, body)
        return
    if shape.kind == "transport":
        assert body["detail"] == {"error_type": type(shape.exception()).__name__}, (what, body)
        assert body["message"] == TRANSPORT_ERROR_MESSAGES[body["code"]], (what, body)
        assert shape.endpoint not in json.dumps(body) and "runtimes/" not in json.dumps(body), (
            what, body)
        return
    if shape.kind == "frame":
        # N17-obs95: the frame's structured detail, as the chat `error` event carries it (§4)
        assert body["detail"]["error_type"] == shape.error_type, (what, body)
        assert set(body["detail"]) >= {"error", "message", "error_type"}, (what, body)
        assert body["message"].startswith("runtime returned error: "), (what, body)
        return
    raise AssertionError((what, shape.label, "no envelope rule for this shape kind"))


def _call_or_escape(
    fn: Callable[[], Any], shape: Shape, *, what: str
) -> tuple[Any, BaseException | None]:
    """Perform a route call that may escape the error handlers. A documented unhandled 500
    (`InternalServerException`, api.md "Errors") is returned as the exception; anything else
    that escapes is an ``EnvelopeViolation`` (the finding), chained to the cause."""
    try:
        return fn(), None
    except AssertionError:
        raise
    except Exception as exc:  # noqa: BLE001 — classified below
        if documented_unhandled(exc):
            return None, exc
        raise EnvelopeViolation(
            f"{what}: the {shape.label} fault escaped the error handlers as "
            f"{type(exc).__name__} (a bare 500; api.md 'Errors': an AWS-side failure is still "
            f"returned as an envelope, never as a bare 500 — no sentence names this shape)"
        ) from exc


class FaultMixin:
    """The generic faulted step: digest before / after, the world's ``fault`` hook, the
    half-write / invariant / planned-node oracles, coverage."""

    observations: list[str]

    def _init_faults(self) -> None:
        self.observations = []
        self.seam_calls: list[str] = []
        self.patcher = Patcher()

    # -- the planning API (pure) ---------------------------------------------------------
    @classmethod
    def fault_shapes(cls, edge: Edge, family: str) -> list[Shape]:
        raise NotImplementedError

    @classmethod
    def fault_dst(cls, edge: Edge, shape: Shape) -> str:
        raise NotImplementedError

    # -- the step -------------------------------------------------------------------------
    def fault(self, edge: Edge, shape: Shape) -> Outcome:
        raise NotImplementedError

    def fault_step(self, edge: Edge, shape: Shape, *, expected_dst: str) -> Outcome:
        self.before(edge)
        before = self.digest()
        self.seam_calls = []
        try:
            out = self.fault(edge, shape)
        finally:
            self.patcher.restore()
        after = self.digest()
        changed = sorted(k for k in after if before.get(k) != after[k])
        extra = [k for k in changed if k not in out.writes]
        if extra:
            raise HalfWrite(
                f"{edge.id}@{shape.label}: the failed call changed {extra} — the contract's "
                f"failure state names only {sorted(out.writes) or 'nothing'} "
                f"({out.clause or 'no sentence'})"
            )
        assert out.dst == expected_dst, (
            f"{edge.id}@{shape.label}: the contract's failure node is {expected_dst!r} but the "
            f"world observed {out.dst!r}"
        )
        self.after_fault(edge, shape, out)
        self.check_invariants(edge)
        coverage.record_edge(self.graph, edge.id)
        if out.observation:
            self.observations.append(f"{edge.id}@{shape.label}: {out.observation}")
        return out

    def after_fault(self, edge: Edge, shape: Shape, out: Outcome) -> None:
        """Runs after the half-write oracle: a world undoes here what ``before(edge)`` staged for
        a transition the fault refused (the entity is back at the edge's source node)."""

    def refusal_with_faulted_seams(
        self, edge: Edge, shape: Shape, arm: Callable[[], None], *, reaches_seam: bool = False
    ) -> Outcome:
        """The refusal axis: arm the shape on every seam, drive the refusal's own edge method,
        and prove the seam was never reached (or, where the contract's decision IS the seam's
        read, that the fault's envelope answered and nothing was written)."""
        arm()
        if reaches_seam:
            return self.fault(edge, shape)
        getattr(self, f"edge_{edge.id}")(edge)
        assert not self.seam_calls, (
            f"refusal {edge.id!r} reached the data-plane seam ({self.seam_calls}) — a refusal is "
            f"decided on the ledger before any AWS call"
        )
        return Outcome(
            dst=edge.dst,
            clause="the edge's own contract sentence (a refusal writes "
            "nothing and reaches no seam)",
        )


# ---------------------------------------------------------------------------
# agent
# ---------------------------------------------------------------------------

STAGES = ("provision", "deploy", "register")
STAGE_OPS = {
    "provision": "CreateRole",
    "deploy": "UpdateAgentRuntime",
    "register": "CreateRegistryRecord",
}
DEPLOY_EDGES = ("deploy_succeeds", "redeploy_succeeds", "cold_redeploy_succeeds")
DELETE_EDGES = ("delete_active", "delete_failed")


class AgentFaults(FaultMixin, AgentWorld):
    SEAMS = ("runtime", "ssm")
    """``AgentWorld`` + the fault hook. Shapes per edge family:

    * the deploy edges — a Runtime / IAM / SSM exception raised inside `provision` / `deploy` /
      `register` (agents.md §4.2: a first deploy or a cold re-publish that fails → `failed`; §4.3:
      a failed update keeps the live version unless the failure is inside the `deploy` stage) and
      the process-death shape (the job left `running`, resumed to `active` / `succeeded`);
    * the delete edges — the control plane's answer to `DeleteAgentRuntime` (N16-obs87:
      AccessDenied with the runtime still present → 403, row kept; api.md `AWS_ERROR_MAP`;
      `RUNTIME_ABSENT` → 200 with the runtime step skipped);
    * `chat_active` — the Runtime's failure during the turn (`chat_stream`: "errors surface as an
      `error` event"; assistant-turns.md §8 parity) and the SSE-level shapes through the real chain.
    """

    def __init__(self, clients, hooks) -> None:
        super().__init__(clients, hooks)
        self._init_faults()
        self.recorder = TurnRecorder()

    @classmethod
    def fault_shapes(cls, edge: Edge, family: str) -> list[Shape]:
        if edge.id in DEPLOY_EDGES:
            if family == "interrupted":
                return [
                    with_at(s, at)
                    for s in shapes(family=family, kinds=("interrupt",))
                    for at in STAGES
                ]
            pool = _runtime_client_shapes(family) + shapes(family=family, seam="ssm")
            return [with_at(s, at) for s in pool for at in STAGES]
        if edge.id in DELETE_EDGES:
            if family == "absent":
                return [s for s in shapes(family=family, seam="runtime") if s.kind == "absent"]
            if family == "not_found":
                return []  # the wrapper maps NotFound itself (N16-obs87); the stub sits above it
            return _runtime_client_shapes(family)
        if edge.id == "chat_active":
            return _runtime_exception_shapes(family) + shapes(
                family=family, seam="runtime", kinds=("lines",)
            )
        if edge.is_refusal:
            return _representative(family, cls.SEAMS)
        return []

    @classmethod
    def fault_dst(cls, edge: Edge, shape: Shape) -> str:
        if edge.id in DEPLOY_EDGES:
            if shape.kind == "interrupt":
                return "active"
            if edge.id == "redeploy_succeeds" and shape.at != "deploy":
                return "active"  # §4.3 keeps the live version
            return "failed"  # §4.2 (first deploy / cold) and §4.3 (inside `deploy`)
        if edge.id in DELETE_EDGES:
            return edge.dst if shape.kind == "absent" else edge.src
        return edge.dst

    # -- seams ------------------------------------------------------------------------------
    def _arm_delete(self, shape: Shape) -> None:
        calls = self.seam_calls

        def delete_agent_resources(agent, ws):
            calls.append("DeleteAgentRuntime")
            if shape.kind == "absent":
                return rt.RUNTIME_ABSENT
            raise shape.exception("DeleteAgentRuntime")

        self.patcher.set(agents_router.zip_method, "delete_agent_resources", delete_agent_resources)

    def _arm_chat(self, shape: Shape) -> None:
        calls = self.seam_calls
        if shape.kind == "lines":
            plane = FaultPlane(shape)
            self.patcher.set(
                chat_service, "invoke_agent_events", invoke_service.invoke_agent_events
            )
            self.patcher.set(invoke_service, "data_client", lambda _ws=None: plane)
            return

        def producer(agent, prompt, session_id=None, actor_id="river", **_kw):
            calls.append("InvokeAgentRuntime")
            yield {"event": "delta", "data": {"text": "half an answer "}}
            raise shape.exception()

        self.patcher.set(chat_service, "invoke_agent_events", producer)

    def _arm_stage(self, shape: Shape) -> None:
        calls = self.seam_calls

        def boom(ctx, agent, _shape=shape):
            calls.append(STAGE_OPS[_shape.at])
            raise _shape.exception(STAGE_OPS[_shape.at])

        self.hooks.clear()
        self.hooks[shape.at] = boom

    def _arm_all(self, shape: Shape) -> None:
        self._arm_delete(
            shape
            if shape.kind != "lines"
            else shapes(family="5xx", seam="runtime", kinds=("client",))[0]
        )
        if shape.kind != "interrupt":
            self._arm_chat(shape)

    # -- the hook ----------------------------------------------------------------------------
    def fault(self, edge: Edge, shape: Shape) -> Outcome:
        if edge.is_refusal:
            return self.refusal_with_faulted_seams(edge, shape, lambda: self._arm_all(shape))
        if edge.id in DEPLOY_EDGES:
            return self._faulted_stage(edge, shape)
        if edge.id in DELETE_EDGES:
            return self._faulted_delete(edge, shape)
        if edge.id == "chat_active":
            return self._faulted_chat(edge, shape)
        raise NotImplementedError(edge.id)

    def _faulted_stage(self, edge: Edge, shape: Shape) -> Outcome:
        self._arm_stage(shape)
        try:
            if shape.kind == "interrupt":
                return self._interrupted_stage(edge, shape)
            execute_deploy_job(self.job_id)
        finally:
            self.hooks.clear()
        row, job, dep = self.row(), self.job(), self.deployment()
        name = type(shape.exception()).__name__
        assert (job.status, dep.status) == ("failed", "failed"), (job.status, dep.status, job.error)
        stages = {s["name"]: s for s in dep.stages}
        assert stages[shape.at]["status"] == "failed", stages
        assert name in (stages[shape.at].get("detail") or ""), stages[shape.at]
        assert name in (job.error or "") and name in (row.error or ""), (job.error, row.error)
        expected = self.fault_dst(edge, shape)
        assert row.status == expected, (edge.id, shape.label, row.status, expected)
        out = self.ada.get(f"/api/agents/{self.agent_id}").json()
        if expected == "active":
            assert out["last_deploy_failed"] is True and name in (out["last_deploy_error"] or ""), (
                out
            )
            clause = "agents.md §4.3 — a failed update keeps the live version"
        else:
            clause = (
                "agents.md §4.2 — a first deploy / cold re-publish that fails marks the agent "
                "`failed`"
                if edge.id != "redeploy_succeeds"
                else "agents.md §4.3 — a failure inside the `deploy` stage itself marks the agent "
                "`failed`"
            )
        # recovery: the terminal job is inert for the resume path (execute_deploy_job's claim)
        before = (job.status, job.error, dep.status, [s["status"] for s in dep.stages])
        execute_deploy_job(self.job_id)
        job2, dep2 = self.job(), self.deployment()
        assert (job2.status, job2.error, dep2.status, [s["status"] for s in dep2.stages]) == before
        db = SessionLocal()
        try:
            assert pipeline.in_flight_deploy_job(db, self.agent_id) is None
        finally:
            db.close()
        return Outcome(dst=expected, writes=JOB_TABLES, clause=clause)

    def _interrupted_stage(self, edge: Edge, shape: Shape) -> Outcome:
        with pytest.raises(Interrupted):
            execute_deploy_job(self.job_id)
        job, dep = self.job(), self.deployment()
        assert job.status == "running", job.status  # what a process death leaves behind
        stages = {s["name"]: s for s in dep.stages}
        assert stages[shape.at]["status"] == "running", stages
        self.hooks.clear()
        # the startup resume adopts a `running` job (direct callers are resume drivers)
        execute_deploy_job(self.job_id)
        row, job, dep = self.row(), self.job(), self.deployment()
        assert (row.status, job.status, dep.status) == ("active", "succeeded", "succeeded"), (
            row.status,
            job.status,
            dep.status,
            job.error,
        )
        assert all(s["status"] in ("succeeded", "skipped") for s in dep.stages), dep.stages
        assert row.arn and row.resource_id and row.error is None
        return Outcome(
            dst="active",
            writes=JOB_TABLES | {"spec_snapshots"},
            clause="architecture.md 'Deploy is an async, resumable job' — "
            "resume_pending_jobs re-runs interrupted jobs from the first "
            "non-succeeded stage",
        )

    def _faulted_delete(self, edge: Edge, shape: Shape) -> Outcome:
        self._arm_delete(shape)
        if shape.kind == "absent":
            res = self.ada.delete(f"/api/agents/{self.agent_id}")
            body = _envelope_free(res, 200, what=edge.id)
            assert body["deleted"] is True and body["aws_resource_deleted"] is False, body
            steps = {s["step"]: s for s in body["steps"]}
            assert steps["runtime"]["status"] == "skipped", steps["runtime"]
            # N16-obs87 (read back gone → "already gone") or N16-obs76 (the deploy never reached
            # provision — a `failed` first deploy — → "no runtime was created")
            expected = "already gone" if self.row().resource_id else "no runtime was created"
            assert expected in steps["runtime"]["detail"], steps["runtime"]
            assert self.row().status == "deleted"
            return Outcome(
                dst=edge.dst,
                writes=frozenset({"agents"}),
                clause="N16-obs87 — a runtime already gone on readback: the row and "
                "its residue still go, `runtime` reported skipped",
            )
        src_status = self.row().status
        res, escaped = _call_or_escape(
            lambda: self.ada.delete(f"/api/agents/{self.agent_id}"), shape, what=edge.id
        )
        if escaped is not None:
            observation = (
                f"documented unhandled 500 ({aws_error_code(escaped)} on "
                f"DeleteAgentRuntime); row kept"
            )
        else:
            mapped = _mapped(shape)
            assert mapped is not None, (shape.label, res.status_code, res.text[:300])
            status, code = mapped
            body = _envelope(res, status, code, what=edge.id)
            _assert_envelope_detail(body, shape, operation="DeleteAgentRuntime", public=False,
                                    what=edge.id)
            observation = ""
        assert self.row().status == src_status, (self.row().status, src_status)
        assert self.ada.get(f"/api/agents/{self.agent_id}").status_code == 200
        return Outcome(
            dst=edge.src,
            clause="api.md 'Errors' (AWS_ERROR_MAP) + N16-obs87 (a real "
            "AccessDenied → 403, the row kept)",
            observation=observation,
        )

    def _faulted_chat(self, edge: Edge, shape: Shape) -> Outcome:
        self.recorder.install(self.patcher)
        self._arm_chat(shape)
        res = self.chat(self.ada)
        assert res.status_code == 200, (res.status_code, res.text[:300])
        frames = [(k, s, d) for k, s, d in _frames(res.text) if k != "heartbeat"]
        kinds = [k for k, _s, _d in frames]
        assert kinds[0] == "meta", kinds
        sid = frames[0][2]["session_id"]
        self.recorder.join(self.agent_id, sid)
        row = _session(self.agent_id, sid)
        assert row is not None and row.active_turn is None and row.turns == 1, row and (
            row.active_turn,
            row.turns,
        )
        terminal, data = kinds[-1], frames[-1][2]
        texts = "".join(d.get("text", "") for k, _s, d in frames if k == "delta")
        if shape.kind == "lines":
            # no contract sentence names a malformed / partial frame: observe, register as DOC
            assert terminal in ("done", "error"), kinds
            observation = (
                f"terminal `{terminal}`; the reply text rendered to the member was "
                f"{texts[:160]!r}; error data "
                f"{json.dumps(data)[:160] if terminal == 'error' else '—'}"
            )
            clause = None
        else:
            assert terminal == "error", kinds
            name = type(shape.exception()).__name__
            assert name in data.get("message", ""), data
            observation = ""
            if shape.kind == "frame" and "detail" not in data:
                observation = (
                    "the SDK error frame's `error_type` is not carried as "
                    "`detail.error_type` on the chat `error` event (assistant-turns.md "
                    "§4 names it for the architect turn; §8 parity names no exception)"
                )
            clause = (
                "chat_stream: 'Never raises mid-stream; errors surface as an `error` event' "
                "(assistant-turns.md §4: `{message}` rendered verbatim for a non-AppError)"
            )
        # recovery: the reattach replays the persisted log and ends on the terminal frame
        box: dict[str, Any] = {}

        def go():
            r = self.ada.get(
                f"/api/chat/{self.agent_id}/sessions/{sid}/turns/1/stream", params={"from": 0}
            )
            box["status"], box["text"] = r.status_code, r.text

        t = threading.Thread(target=go, daemon=True)
        t.start()
        t.join(timeout=BUDGET_S)
        assert "status" in box, "the reattach after a faulted turn never answered (tails forever)"
        assert box["status"] == 200, box
        replay = [(k, s) for k, s, _d in _frames(box["text"]) if k != "heartbeat"]
        assert replay == [(k, s) for k, s, _d in frames], (replay, [(k, s) for k, s, _d in frames])
        self.ids.add(sid)
        return Outcome(dst=edge.dst, writes=TURN_TABLES, clause=clause, observation=observation)


def _envelope_free(res: Any, status: int, *, what: str) -> dict[str, Any]:
    assert res.status_code == status, (what, res.status_code, res.text[:400])
    return res.json()


# ---------------------------------------------------------------------------
# chat session
# ---------------------------------------------------------------------------

TURN_EDGES = ("first_turn", "next_turn", "console_turn_after_v1")
V1_EDGES = ("v1_turn", "v1_turn_again")
STOP_EDGES = ("stop_open", "stop_after_v1")


class FaultScript(Script):
    """A producer run that streams its head and then raises ANY exception (the client /
    transport shapes), where ``Script`` raises the SDK frame only."""

    def __init__(self, head: list[dict], exc: BaseException) -> None:
        super().__init__(head)
        self.exc = exc

    def run(self):
        yield from self.head
        raise self.exc


class FaultStopPlane:
    """The chat world's StopPlane, raising the armed shape once (or answering "gone")."""

    def __init__(self, inner: Any, shape: Shape, calls: list[str]) -> None:
        self.inner, self.shape, self.calls = inner, shape, calls

    def stop_runtime_session(self, **params: Any) -> dict[str, Any]:
        self.calls.append("StopRuntimeSession")
        if self.shape.code == "ResourceNotFoundException":
            # a session AWS says is gone stays gone: the inner plane's later stops say so too
            self.inner.calls.append(params["runtimeSessionId"])
        raise self.shape.exception("StopRuntimeSession")


class ChatFaults(FaultMixin, ChatWorld):
    SEAMS = ("runtime",)
    """``ChatWorld`` + the fault hook. Shapes per edge family:

    * the gated turn edges — the producer dies after two deltas with the shape (the SDK frame
      the wrapper raises, or the botocore exception of the call): the turn ends on a terminal
      `error`, the claim is released, the reattach replays what was persisted (assistant-turns.md
      §4 / §8.2 / §8.3); the SSE-level shapes run through the real chain over a fake data plane
      and end on `error` too (N17-obs96 / N17-obs97: a malformed frame, a REJECTED body);
    * the `/v1` edges — the sync wrapper raises: the mapped `aws.*` envelope with the public
      message, nothing ledgered (`record_turn` runs after the invoke); `InternalServerException`
      is the documented unhandled 500; a transport failure is 504 `aws.timeout` (N17-obs94); the
      SDK's terminal error frame is 502 `agent.runtime_error` with the frame's detail (N17-obs95);
    * the stop edges — `StopRuntimeSession` answers the shape: NotFound → `already_ended: true`
      (api.md), a conflict → 409 `aws.conflict`, the row's `ended_at` untouched.
    """

    def __init__(self, clients, agent_id, producer, plane, tab, api_key) -> None:
        super().__init__(clients, agent_id, producer, plane, tab, api_key)
        self._init_faults()
        self.terminals: dict[int, str] = {}

    @classmethod
    def fault_shapes(cls, edge: Edge, family: str) -> list[Shape]:
        if edge.id in TURN_EDGES:
            return _runtime_exception_shapes(family) + shapes(
                family=family, seam="runtime", kinds=("lines",)
            )
        if edge.id in V1_EDGES:
            return _runtime_exception_shapes(family)
        if edge.id in STOP_EDGES:
            return [s for s in _runtime_client_shapes(family)]
        if edge.is_refusal:
            return _representative(family, cls.SEAMS)
        return []

    @classmethod
    def fault_dst(cls, edge: Edge, shape: Shape) -> str:
        if edge.id in TURN_EDGES:
            return "open"  # the turn ended (on `error`): the session is open again
        if edge.id in V1_EDGES:
            return edge.src
        if edge.id in STOP_EDGES:
            return edge.dst if shape.code == "ResourceNotFoundException" else edge.src
        return edge.dst

    # -- seams ------------------------------------------------------------------------------
    def _arm_v1(self, shape: Shape) -> None:
        calls = self.seam_calls

        def invoke_runtime_text(_client, _arn, prompt, *, session_id, **_kw):
            calls.append("InvokeAgentRuntime")
            raise shape.exception()

        self.patcher.set(invoke_service.rt, "invoke_runtime_text", invoke_runtime_text)

    def _arm_stop(self, shape: Shape) -> None:
        plane = FaultStopPlane(self.plane, shape, self.seam_calls)
        self.patcher.set(
            invoke_service,
            "data_client",
            lambda _ws=None, _p=plane: _p,
        )

    def _arm_turn(self, shape: Shape) -> FaultScript | None:
        if shape.kind == "lines":
            plane = FaultPlane(shape, stop=self.plane)
            self.patcher.set(
                chat_service, "invoke_agent_events", invoke_service.invoke_agent_events
            )
            self.patcher.set(invoke_service, "data_client", lambda _ws=None: plane)
            return None
        script = FaultScript(_deltas(2, "h"), shape.exception())
        self.producer.queue.append(script)
        return script

    def _arm_all(self, shape: Shape) -> FaultScript | None:
        exc_shape = (
            shape
            if shape.kind in ("client", "transport")
            else shapes(family="5xx", seam="runtime", kinds=("client",))[0]
        )
        self._arm_v1(exc_shape)
        self._arm_stop(exc_shape)
        return self._arm_turn(exc_shape)

    # -- the hook ----------------------------------------------------------------------------
    def fault(self, edge: Edge, shape: Shape) -> Outcome:
        if edge.is_refusal:
            armed: list[FaultScript | None] = []
            out = self.refusal_with_faulted_seams(
                edge, shape, lambda: armed.append(self._arm_all(shape))
            )
            script = armed[0] if armed else None
            if script is not None:
                assert script in self.producer.queue, (
                    f"refusal {edge.id!r} consumed the producer script — the runtime was invoked"
                )
                self.producer.queue.remove(script)
            return out
        if edge.id in TURN_EDGES:
            return self._faulted_turn(edge, shape)
        if edge.id in V1_EDGES:
            return self._faulted_v1(edge, shape)
        if edge.id in STOP_EDGES:
            return self._faulted_stop(edge, shape)
        raise NotImplementedError(edge.id)

    def _faulted_turn(self, edge: Edge, shape: Shape) -> Outcome:
        self._arm_turn(shape)
        box: dict[str, Any] = {}

        def go():
            r = self.tab.post(
                self.base, json={"prompt": f"p{self.turn + 1}", "session_id": self.sid}
            )
            box["status"], box["text"] = r.status_code, r.text

        thread = threading.Thread(target=go, name="graph-chat-fault", daemon=True)
        thread.start()
        thread.join(timeout=BUDGET_S)
        assert "status" in box, "the faulted turn never answered"
        assert box["status"] == 200, box
        self.turn += 1
        self.ids.add(self.sid)
        self.producer.wait_finished(self.agent_id, self.sid)
        frames = [(k, s, d) for k, s, d in _frames(box["text"]) if k != "heartbeat"]
        kinds = [k for k, _s, _d in frames]
        assert kinds and kinds[0] == "meta" and frames[0][2]["turn"] == self.turn, (
            kinds,
            self.turn,
        )
        terminal, data = kinds[-1], frames[-1][2]
        row = _session(self.agent_id, self.sid)
        assert row is not None and row.active_turn is None and row.turns == self.turn, row and (
            row.active_turn,
            row.turns,
        )
        texts = "".join(d.get("text", "") for k, _s, d in frames if k == "delta")
        if shape.kind == "lines":
            # N17-obs96 / N17-obs97: a malformed frame or a REJECTED body is a protocol error —
            # the turn ends on `error` carrying the frame's detail, the cut text / the refusal
            # never reads as the reply (api.md "Malformed and refusing runtime answers")
            assert terminal == "error", (kinds, texts[:160])
            assert data.get("detail", {}).get("error_type") in (
                rt.MALFORMED_FRAME_TYPE, rt.REJECTED_BODY_TYPE), data  # N17-obs98: §4 parity
            assert not texts.lstrip().startswith("{") and "unknown job_type" not in texts, texts
            clause = (
                "api.md Console Chat API 'Malformed and refusing runtime answers' — a non-JSON "
                "data line / a code+state body ends the turn on `error`, never as reply text"
            )
            observation = ""
        else:
            assert terminal == "error", kinds
            name = type(shape.exception()).__name__
            assert name in data.get("message", ""), data
            head = "".join(d["data"]["text"] for d in _deltas(2, "h"))
            assert texts == head, (texts, head)  # the partial answer the failed attempt is kept
            clause = (
                "assistant-turns.md §8.2 — `error` keeps the partial answer; §4 — `{message}` "
                "rendered verbatim for a non-AppError, `detail {error_type, …}` for the SDK frame"
            )
            observation = ""
            if shape.kind == "frame":
                # N17-obs98 (§8.2 "Terminal `error` data — §4 parity"): the frame's error_type
                assert data.get("detail", {}).get("error_type") == shape.error_type, data
        # the history keeps the user row and the error row (and the partial answer)
        db = SessionLocal()
        try:
            roles = [
                m.role
                for m in db.query(ChatMessage)
                .filter(ChatMessage.session_id == self.sid)
                .order_by(ChatMessage.id)
                .all()
            ]
        finally:
            db.close()
        assert roles.count("user") == self.turn, roles
        if terminal == "error":
            assert "error" in roles, roles
        self.post_frames[self.turn] = frames
        self.terminals[self.turn] = terminal
        # recovery: the reattach replays the log to the same terminal frame (§8.3)
        self.edge_reattach_open(edge)
        return Outcome(dst="open", writes=TURN_TABLES, clause=clause, observation=observation)

    def edge_reattach_open(self, edge: Edge) -> None:
        """§8.3 — the replay is the persisted log, ended by THE turn's terminal frame: `done` for
        a completed turn, `error` for one the fault layer ended (the base edge assumes `done`)."""
        res = self.ada.get(
            f"{self.base}/sessions/{self.sid}/turns/{self.turn}/stream", params={"from": 0}
        )
        assert res.status_code == 200, (res.status_code, res.text[:300])
        replay = [(k, s, d) for k, s, d in _frames(res.text) if k != "heartbeat"]
        assert replay[-1][0] == self.terminals.get(self.turn, "done"), [k for k, _s, _d in replay]
        posted = self.post_frames[self.turn]
        assert [(k, s) for k, s, _d in replay] == [(k, s) for k, s, _d in posted]

    def _faulted_v1(self, edge: Edge, shape: Shape) -> Outcome:
        self._arm_v1(shape)
        turns_before = _session(self.agent_id, self.sid).turns
        res, escaped = _call_or_escape(
            lambda: self.ada.post(
                f"/v1/agents/{self.agent_id}/invoke",
                json={"prompt": "via v1", "session_id": self.sid},
                headers={"X-Api-Key": self.api_key},
            ),
            shape,
            what=edge.id,
        )
        if escaped is not None:
            observation = (
                f"documented unhandled 500 ({aws_error_code(escaped)} on InvokeAgentRuntime)"
            )
        else:
            mapped = _mapped(shape)
            assert mapped is not None, (shape.label, res.status_code, res.text[:300])
            status, code = mapped
            body = _envelope(res, status, code, what=edge.id)
            # /v1: the generic per-code sentence, `detail` = the AWS code only (api.md "Errors");
            # a transport shape: the static sentence + `{error_type}` (N17-obs94)
            _assert_envelope_detail(body, shape, operation="InvokeAgentRuntime", public=True,
                                    what=edge.id)
            if shape.kind == "client":
                assert shape.message not in body["message"], body  # the raw AWS text stays inside
            observation = ""
        assert _session(self.agent_id, self.sid).turns == turns_before
        return Outcome(
            dst=edge.src,
            clause="api.md 'Errors' — /v1 shares the handler: same status "
            "and code, a generic per-code message",
            observation=observation,
        )

    def _faulted_stop(self, edge: Edge, shape: Shape) -> Outcome:
        self._arm_stop(shape)
        res, escaped = _call_or_escape(
            lambda: self.ada.post(f"{self.base}/sessions/{self.sid}/stop"), shape, what=edge.id
        )
        row = _session(self.agent_id, self.sid)
        if escaped is not None:
            assert row.ended_at is None, "the stop failed but the row was stamped ended"
            return Outcome(
                dst=edge.src,
                clause="api.md 'Errors' — the documented unhandled 500",
                observation=f"documented unhandled 500 ({aws_error_code(escaped)} on "
                "StopRuntimeSession); ended_at untouched",
            )
        if shape.code == "ResourceNotFoundException":
            body = _envelope_free(res, 200, what=edge.id)
            assert body["already_ended"] is True and body["ended"] is True and body["ended_at"]
            self.ended_at = body["ended_at"]
            assert row.ended_at is not None
            return Outcome(
                dst=edge.dst,
                writes=frozenset({"chat_sessions"}),
                clause="api.md END SESSION — `already_ended: true` when AWS answered "
                "ResourceNotFoundException: a success, not an error",
            )
        mapped = _mapped(shape)
        assert mapped is not None, (shape.label, res.status_code, res.text[:300])
        status, code = mapped
        body = _envelope(res, status, code, what=edge.id)
        _assert_envelope_detail(body, shape, operation="StopRuntimeSession", public=False,
                                what=edge.id)
        assert row.ended_at is None, "the stop failed but the row was stamped ended"
        return Outcome(
            dst=edge.src,
            clause="api.md END SESSION — a RetryableConflictException "
            "that outlives botocore's retries is 409 aws.conflict; "
            "AWS_ERROR_MAP for the rest; the row untouched",
        )


# ---------------------------------------------------------------------------
# evaluation run
# ---------------------------------------------------------------------------

REPLAY_AT = ("once", "first_scenario", "all")


class EvalRunFaults(FaultMixin, EvalRunWorld):
    SEAMS = ("runtime",)
    """``EvalRunWorld`` + the fault hook at the ONE data-plane seam of the run graph: the worker's
    replay invoke (and its judge). evaluation-engine.md "Per-scenario failure semantics
    (N-SCN-bug1)": a transient shape is replayed on the same pre-assigned session up to 3 attempts
    (`once` → the run completes clean, one retry), a scenario still failing after its attempts is
    an errored scenario with one error record per evaluator (`first_scenario` → completed-with-
    errors, `k of n evaluator records errored`), only every scenario failing fails the run
    (`all`); a non-transient shape (404 / 403) gets one attempt. The judge: a throttle is retried
    (`judge_once`), a persistent 5xx is error records → `failed` when every record errored."""

    def __init__(self, *args, **kw) -> None:
        super().__init__(*args, **kw)
        self._init_faults()

    @classmethod
    def fault_shapes(cls, edge: Edge, family: str) -> list[Shape]:
        if edge.id != "dispatch_completes":
            return []  # every other edge is decided on the ledger (the queue is held)
        pool = _runtime_exception_shapes(family)
        out = [with_at(s, at) for s in pool for at in REPLAY_AT if at != "once" or s.transient]
        # round 8 (r17-r): the poller's S3 artefact-store reads (status.json / results.jsonl)
        out += [
            with_at(s, at)
            for s in shapes(family=family, seam="s3", kinds=("client",))
            for at in STORE_ATS
        ]
        if family == "throttle":
            out.append(with_at(_from_judge("throttle"), "judge_once"))
        if family == "5xx":
            out.append(with_at(_from_judge("5xx"), "judge_all"))
        return out

    @classmethod
    def fault_dst(cls, edge: Edge, shape: Shape) -> str:
        if shape.at in STORE_ATS:
            # a store read the poller cannot do fails the run; `recheck` re-attaches it (the
            # recovery oracle inside the step) and settles `completed` from done.json — a
            # NoSuchKey reads as "not yet written" (StoreReader) and the run completes directly
            return "completed"
        return "failed" if shape.at in ("all", "judge_all") else "completed"

    def fault(self, edge: Edge, shape: Shape) -> Outcome:
        assert edge.id == "dispatch_completes", edge.id
        if shape.at in STORE_ATS:
            return self._faulted_store_read(edge, shape)
        if shape.at.startswith("judge"):
            return self._faulted_judge(edge, shape)
        exc = shape.exception()
        transient = shape.transient
        attempts = TRANSIENT_SCENARIO_ATTEMPTS if transient else 1
        if shape.at == "once":
            assert transient, (
                shape.label,
                "a non-transient shape is not retried — use first_scenario",
            )
            errors = [exc]
        elif shape.at == "first_scenario":
            errors = [exc] * attempts
        else:
            errors = [exc] * (attempts * len(ITEMS))
        self.env, calls = stub_environment(self.monkeypatch, invoke_errors=list(errors))
        self.calls = calls
        try:
            out = self.dispatch(self.run_id)
        finally:
            self.env, self.calls = stub_environment(self.monkeypatch)
        rows = _results(self.run_id)
        if shape.at == "once":
            assert out["status"] == "completed" and not out.get("error"), out
            assert calls["n"] == len(ITEMS) + 1, calls["n"]
            assert calls["sessions"][0] == calls["sessions"][1], calls["sessions"]
            assert rows == len(ITEMS) * 2
            self.completed.append(self.run_id)
            clause = (
                "evaluation-engine.md N-SCN-bug1 — a transient is replayed on the SAME "
                "pre-assigned session; the run completes clean"
            )
        elif shape.at == "first_scenario":
            assert out["status"] == "completed", out
            assert calls["n"] == attempts + (len(ITEMS) - 1), calls["n"]
            assert out["error"] and "of 4 evaluator records errored" in out["error"], out["error"]
            failures = out.get("session_failures") or {}
            assert failures.get("failed") == 1 and failures.get("kind") == "replay_failed", failures
            assert rows == len(ITEMS) * 2  # the errored session still has one record per evaluator
            self.completed.append(self.run_id)
            clause = (
                "evaluation-engine.md N-SCN-bug1 — an errored scenario, not a failed run: "
                "one error record per evaluator, completed-with-errors"
            )
        else:
            assert out["status"] == "failed", out
            assert "scenarios failed to replay" in (
                out.get("error") or ""
            ) or "evaluator records failed" in (out.get("error") or ""), out.get("error")
            clause = (
                "evaluation-engine.md N-SCN-bug1 — only when EVERY replayed scenario failed "
                "does the run fail"
            )
        return Outcome(dst=self.fault_dst(edge, shape), writes=RUN_TABLES, clause=clause)

    def _faulted_store_read(self, edge: Edge, shape: Shape) -> Outcome:
        """evaluation-engine.md §5: `_poll` mirrors `status.json` and ingests `results.jsonl`;
        `execute_run` "never raises" — an exception lands on the row as `failed` with the type.
        N17-obs123 (cn/r17-t): a TRANSIENT read fault (SlowDown / InternalError) is retried in
        place (`_read_store`, `STORE_READ_BACKOFF_S`) and never fails the run; a non-transient
        one (AccessDenied) still does, and the recovery path the contract names is `recheck_run`
        (re-attach by `batch_eval_id`, settle from `done.json`)."""
        self.env, calls = stub_environment(self.monkeypatch)
        self.calls = calls
        self.monkeypatch.setattr(svc, "STORE_READ_BACKOFF_S", (0.01, 0.01, 0.01))
        # rev-r17 on cn/r17-t: injected sleep only — the walks must never really wait
        self.monkeypatch.setattr(svc, "_sleep", lambda _s: None)
        exc = shape.exception("GetObject")
        store = OnceFaultyStore(self.env.store, STORE_KEY[shape.at], exc)
        self.env.executor._reader._store = store
        try:
            out = self.dispatch(self.run_id)
        finally:
            self.env.executor._reader._store = self.env.store
        assert store.calls == 1, "the poller never read the faulted key"
        if shape.code == "NoSuchKey":
            assert out["status"] == "completed" and not out.get("error"), out
            self.completed.append(self.run_id)
            return Outcome(
                dst="completed",
                writes=RUN_TABLES,
                clause="sink.S3Store.get — a missing key reads as not-yet-written; the "
                "poller keeps polling",
            )
        # the worker's job finished on its own thread regardless: done.json is there
        assert self.env.executor.wait(self.run_id, timeout_s=BUDGET_S)
        observation = ""
        if out["status"] == "failed":
            assert "ClientError" in (out.get("error") or ""), out
            observation = (
                f"one {shape.code} on the poller's {STORE_KEY[shape.at]} read failed "
                f"the run ({out['error'][:80]}…) while the worker job completed"
            )
            # recovery: recheck re-attaches by batch_eval_id and settles from done.json
            res = self.ada.post(f"/api/eval/runs/{self.run_id}/recheck")
            assert res.status_code in (200, 202), (res.status_code, res.text[:300])
            self.queue.dispatch(self.run_id)
            out = self.ada.get(f"/api/eval/runs/{self.run_id}").json()
            clause = ("evaluation-engine.md §5 — `recheck_run` re-attaches by batch_eval_id and "
                      "settles from done.json (a NON-transient read fault fails the run)")
        else:
            clause = ("evaluation-engine.md §5 — the poller mirrors status.json / ingests results; "
                      "a transient read fault is retried in place (N17-obs123)")
        assert out["status"] == "completed" and not out.get("error"), out
        assert _results(self.run_id) == len(ITEMS) * 2
        self.completed.append(self.run_id)
        return Outcome(dst="completed", writes=RUN_TABLES, clause=clause, observation=observation)

    def _faulted_judge(self, edge: Edge, shape: Shape) -> Outcome:
        n = 1 if shape.at == "judge_once" else 50
        judge = FlakyJudge(
            [_throttle("client") if shape.family == "throttle" else _bedrock_5xx("client")] * n,
            overrides={"score": "Very helpful", "reasoning": "clear"},
        )
        self.env, self.calls = stub_environment(self.monkeypatch, judge=judge)
        try:
            out = self.dispatch(self.run_id)
        finally:
            self.env, self.calls = stub_environment(self.monkeypatch)
        if shape.at == "judge_once":
            assert out["status"] == "completed" and not out.get("error"), out
            assert judge.attempts >= len(ITEMS) * 2 + 1, judge.attempts
            self.completed.append(self.run_id)
            return Outcome(
                dst="completed",
                writes=RUN_TABLES,
                clause="tests/faults/test_worker_faults.py — a throttled judge call is "
                "retried with a bounded back-off",
            )
        assert out["status"] == "failed", out
        assert "evaluator records failed" in (out.get("error") or ""), out.get("error")
        return Outcome(
            dst="failed",
            writes=RUN_TABLES,
            clause="evaluation/service._errors_verdict — every record errored → failed; "
            "the judge 5xx is an error record (tests/faults pin, r12-f spec xfail)",
        )


STORE_ATS = ("store_status", "store_results")
STORE_KEY = {"store_status": "status.json", "store_results": "results.jsonl"}


class OnceFaultyStore:
    """The in-process executor's object store whose FIRST `get` of a key ending in ``suffix``
    raises the shape (what `S3Store.get` raises for an unmapped S3 code; a NoSuchKey is the
    store's own "not yet written" → None)."""

    def __init__(self, inner: Any, suffix: str, exc: BaseException) -> None:
        self.inner, self.suffix, self.exc, self.armed, self.calls = inner, suffix, exc, True, 0

    def get(self, key: str):
        if key.endswith(self.suffix) and self.armed:
            self.armed = False
            self.calls += 1
            code = getattr(self.exc, "response", {}).get("Error", {}).get("Code")
            if code in ("NoSuchKey", "404", "NotFound"):
                return None  # S3Store.get's own mapping of a missing key
            raise self.exc
        return self.inner.get(key)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)


def _from_judge(family: str) -> Shape:
    f = _throttle("client") if family == "throttle" else _bedrock_5xx("client")
    return Shape(
        name=f"judge_{f.name}",
        family=family,
        seam="runtime",
        source="tests/faults/conftest.py (worker_judge_*)",
        kind="client",
        code=f.code,
        status=f.http_status,
        message=f.error,
    )


# ---------------------------------------------------------------------------
# memory record
# ---------------------------------------------------------------------------

EXTRACTOR_EDGES = (
    "add",
    "update",
    "supersede",
    "soft_delete",
    "add_duplicate",
    "delete_low_confidence",
)
API_DELETE_EDGES = ("api_delete_active", "api_delete_superseded", "api_delete_soft_deleted")
#: the OpenSearch calls each extractor op makes, in order (handler.py `_apply_op`): the first is
#: the candidate read (before any write), the rest are the op's writes (N17-obs90 fixed in
#: cn/r17-n: the supersede pair — successor + retired target — is ONE `bulk` request)
EXTRACTOR_CALLS = {
    "add": ("knn_search", "put_doc"),
    "update": ("knn_search", "put_doc"),
    "supersede": ("knn_search", "bulk"),
    "soft_delete": ("knn_search", "update_doc"),
    "add_duplicate": ("knn_search",),
    "delete_low_confidence": ("knn_search",),
}
#: memory-backend.md "Error codes" + routers/memory._guard (N14-obs4) — the hub answer per shape
OS_ENVELOPE = {
    429: (503, "memory.backend_unavailable"),
    502: (503, "memory.backend_unavailable"),
    503: (503, "memory.backend_unavailable"),
    404: (404, "memory.not_found"),
}


class OnceFaulty:
    """`FakeOpenSearch` whose named method raises the shape on its FIRST call only, then passes
    through — the extractor's own `_job(FAILED)` bookkeeping uses the same methods, and a fault
    that hits it too is N16-obs32's open domain-wide shape (pinned in
    test_memory_merge_properties), not this round's question."""

    def __init__(
        self,
        inner: Any,
        method: str,
        exc: BaseException,
        calls: list[str],
        index: str | None = None,
    ) -> None:
        self.inner, self.method, self.exc, self.calls = inner, method, exc, calls
        self.index = index  # fault only a call on this index (None: the method's first call)
        self.armed = True

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self.inner, name)
        if name != self.method:
            return attr

        def once(*args: Any, **kwargs: Any) -> Any:
            self.calls.append(name)
            target = args[0] if args else kwargs.get("index")
            if self.armed and (self.index is None or target == self.index):
                self.armed = False
                raise self.exc
            return attr(*args, **kwargs)

        return once


class EmptyRead:
    """The eventually-consistent empty read, the way OpenSearch's near-real-time search really
    behaves: the k-NN / search answer carries no hits — the previous write (`refresh=False`)
    is in no refreshed segment yet — until the caller issues a `_refresh` request, after which
    every read sees the index (N17-obs101 fixed in cn/r17-n: the extractor refreshes right
    before its nearest-record re-check, so a dedupe decided on the stale read is the finding
    this seam reproduces on the pre-fix code)."""

    def __init__(self, inner: Any, calls: list[str]) -> None:
        self.inner, self.calls = inner, calls
        self.refreshed = False

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self.inner, name)
        if name == "request":

            def request(method, path, *a, **k):
                if method == "POST" and path.endswith("/_refresh"):
                    self.calls.append("_refresh")
                    self.refreshed = True
                return attr(method, path, *a, **k)

            return request
        if name == "knn_search":

            def knn_search(*a, **k):
                self.calls.append(name)
                return attr(*a, **k) if self.refreshed else []

            return knn_search
        if name == "search":

            def search(*a, **k):
                self.calls.append(name)
                if self.refreshed:
                    return attr(*a, **k)
                return {"hits": {"hits": [], "total": {"value": 0, "relation": "eq"}}}

            return search
        return attr


class MemoryFaults(FaultMixin, MemoryRecordWorld):
    SEAMS = ("opensearch",)
    """``MemoryRecordWorld`` + the fault hook at the two OpenSearch seams. The extractor
    (handler.py `RetryableError`: "Transient failure — let SQS redeliver"): the fault lands at the
    candidate read or at one of the op's writes; the job row is FAILED with the exception type,
    the index must carry no half-written pair (`scan_memory_index` — memory-backend.md "Merge
    strategy (v2)"), and the redelivery (the same event again, seam healthy) must converge to the
    op's documented effect. The hub (memory-backend.md "Error codes", routers/memory `_guard`,
    N14-obs4): 429 / 5xx → 503 `memory.backend_unavailable`, a transport failure → 502
    `memory.unavailable`, 404 → 404 `memory.not_found`; the record delete reads the stored fields
    before deleting (the read's failure answers) and nothing is removed on a failed call."""

    def __init__(self, clients, handler) -> None:
        super().__init__(clients, handler)
        self._init_faults()

    @classmethod
    def fault_shapes(cls, edge: Edge, family: str) -> list[Shape]:
        pool = shapes(family=family, seam="opensearch")
        if edge.id in EXTRACTOR_EDGES:
            calls = EXTRACTOR_CALLS[edge.id]
            if family == "absent":
                return [
                    with_at(s, "knn_search")
                    for s in pool
                    if s.kind == "absent" and edge.id != "add"
                ]
            if family == "interrupted":
                return [
                    with_at(s, at)
                    for s in shapes(family=family, kinds=("interrupt",))
                    for at in calls[1:]
                ]
            return [with_at(s, at) for s in pool if s.kind != "absent" for at in calls]
        if edge.id in API_DELETE_EDGES or edge.id in ("api_delete_wrong_actor", "api_delete_again"):
            ats = ("get_doc", "delete_doc") if edge.id in API_DELETE_EDGES else ("get_doc",)
            if edge.id == "api_delete_superseded":
                ats = (*ats, "update_doc")
            return [with_at(s, at) for s in pool if s.kind != "absent" for at in ats]
        if edge.id == "list_hides_inactive":
            return [with_at(s, "search") for s in pool if s.kind != "absent"]
        if edge.is_refusal:
            return _representative(family, cls.SEAMS)
        return []

    @classmethod
    def fault_dst(cls, edge: Edge, shape: Shape) -> str:
        if edge.id in EXTRACTOR_EDGES:
            return edge.src if shape.kind == "absent" else edge.dst  # redelivery applies the op
        if edge.id in API_DELETE_EDGES:
            return edge.src
        return edge.dst

    # -- seams ------------------------------------------------------------------------------
    def _faulty(self, shape: Shape, at: str, *, once: bool = False) -> Any:
        if shape.kind == "absent":
            return EmptyRead(self.w.os, self.seam_calls)
        if once:
            return OnceFaulty(self.w.os, at, shape.exception(), self.seam_calls, index=MEM)
        faulty = FaultyOpenSearch(self.w.os, {at: shape.exception()})
        faulty.calls = self.seam_calls
        return faulty

    def _hub_over(self, opensearch: Any) -> None:
        memory_backend.set_backend(
            SelfHostedMemoryBackend(
                s3=self.w.s3,
                opensearch=opensearch,
                sqs=FakeSQS(),
                embedder=self.w.emb,
                clock=lambda: self.w.now,
            )
        )

    def _arm_all(self, shape: Shape) -> None:
        os_shape = (
            shape if shape.seam == "opensearch" else shapes(family="5xx", seam="opensearch")[0]
        )
        self.w.deps.opensearch = self._faulty(os_shape, "knn_search")
        self._hub_over(self._faulty(os_shape, "get_doc"))

    def _disarm(self) -> None:
        self.w.deps.opensearch = self.w.os
        self._hub_over(self.w.os)

    # -- the hook ----------------------------------------------------------------------------
    def fault(self, edge: Edge, shape: Shape) -> Outcome:
        try:
            if edge.is_refusal and edge.id not in (
                "api_delete_wrong_actor",
                "api_delete_again",
                "add_duplicate",
                "delete_low_confidence",
            ):
                return self.refusal_with_faulted_seams(edge, shape, lambda: self._arm_all(shape))
            if edge.id in EXTRACTOR_EDGES:
                return self._faulted_extraction(edge, shape)
            if edge.id in API_DELETE_EDGES or edge.id in (
                "api_delete_wrong_actor",
                "api_delete_again",
            ):
                return self._faulted_api_delete(edge, shape)
            if edge.id == "list_hides_inactive":
                return self._faulted_list(edge, shape)
            raise NotImplementedError(edge.id)
        finally:
            self._disarm()

    def _faulted_extraction(self, edge: Edge, shape: Shape) -> Outcome:
        self.w.deps.opensearch = self._faulty(shape, shape.at, once=True)
        before = {rid: dict(d) for rid, d in self.w.docs.items()}
        method = getattr(self, f"edge_{edge.id}")
        if shape.kind == "absent":
            # the stale read: the op sees no candidate — observe what the extractor did
            try:
                method(edge)
                observation = "the stale read changed nothing the edge asserts"
            except (AssertionError, KeyError) as exc:
                observation = (
                    f"the stale candidate read made the op diverge from the edge's "
                    f"contract: {type(exc).__name__}: {str(exc)[:200]}"
                )
            new = sorted(set(self.w.docs) - set(before))
            violations = scan_memory_index(self.w.docs)
            if violations:
                raise HalfWrite(f"{edge.id}@{shape.label}: stale read left {violations}")
            # the observation is recorded; the index goes back to what the graph's node means so
            # the walk continues from `src` on a consistent world (the extra record a stale read
            # produced is the observation, not a state the graph models)
            index = self.w.os.indices[MEM]
            index.clear()
            index.update({rid: dict(d) for rid, d in before.items()})
            return Outcome(
                dst=edge.src,
                writes=frozenset({"memory_docs", "memory_digest"}),
                clause=None,
                observation=f"{observation}; new documents {new}",
            )
        failure = Interrupted if shape.kind == "interrupt" else self.w.h.RetryableError
        with pytest.raises(failure):
            method(edge)
        jobs = [j for j in self.w.os.indices[JOBS].values() if j.get("kind") == "memory_extract"]
        if shape.kind != "interrupt":
            failed = [j for j in jobs if j.get("status") == "FAILED"]
            assert failed and any(
                type(shape.exception()).__name__ in (j.get("failure_reason") or "") for j in failed
            ), [j.get("failure_reason") for j in jobs]
        after = self.w.docs
        violations = scan_memory_index(after)
        if violations:
            raise HalfWrite(
                f"{edge.id}@{shape.label}: the failed extraction left a half-written pair in the "
                f"index — {violations} (memory-backend.md 'Merge strategy (v2)': the target gets "
                f"`superseded_by` in the same operation; handler.py RetryableError: the message is "
                f"redelivered — until then the index is inconsistent)"
            )
        if shape.at != "knn_search" and after != before:
            raise HalfWrite(
                f"{edge.id}@{shape.label}: the failed write landed "
                f"{sorted(set(after) - set(before))} "
                f"before the exception (no sentence names the index state between a failed "
                f"extraction and its redelivery)"
            )
        # recovery: the redelivery — the same event again, seam healthy — converges
        self.w.deps.opensearch = self.w.os
        method(edge)
        assert scan_memory_index(self.w.docs) == []
        return Outcome(
            dst=edge.dst,
            writes=frozenset({"memory_docs", "memory_digest"}),
            clause="handler.py RetryableError — 'Transient failure — let SQS redeliver'; "
            "the redelivery applies the op (memory-backend.md 'Merge strategy "
            "(v2)')",
        )

    def _expected_hub(self, shape: Shape) -> tuple[int, str]:
        if shape.kind == "os_transport":
            return 502, "memory.unavailable"
        return OS_ENVELOPE.get(shape.status or 0, (502, "memory.unavailable"))

    def _faulted_api_delete(self, edge: Edge, shape: Shape) -> Outcome:
        self._hub_over(self._faulty(shape, shape.at))
        docs_before = {rid: dict(d) for rid, d in self.w.docs.items()}
        actor = "agent2__other" if edge.id == "api_delete_wrong_actor" else None
        kwargs = {"actor": actor} if actor else {}
        res = self.delete(self.admin, self.rid, **kwargs)
        status, code = self._expected_hub(shape)
        _envelope(res, status, code, what=f"{edge.id}@{shape.label}")
        if edge.id != "api_delete_again":
            assert self.rid in self.w.docs, "a failed delete removed the record"
        if self.w.docs != docs_before:
            changed = sorted(
                rid
                for rid in set(docs_before) | set(self.w.docs)
                if docs_before.get(rid) != self.w.docs.get(rid)
            )
            raise HalfWrite(
                f"{edge.id}@{shape.label}: the failed delete ({status} {code}) changed documents "
                f"{changed} — memory-backend.md 'The record delete unlinks': the neighbours are "
                f"unlinked 'in the same operation'; a delete that fails after the unlink leaves "
                f"the "
                f"record present with its links already cut"
            )
        return Outcome(
            dst=edge.src if edge.id in API_DELETE_EDGES else edge.dst,
            clause="memory-backend.md 'Error codes' (429 / 5xx → 503 backend_unavailable, "
            "404 → memory.not_found) + routers/memory._guard (transport → 502 "
            "memory.unavailable); nothing removed on a failed call",
        )

    def _faulted_list(self, edge: Edge, shape: Shape) -> Outcome:
        self._hub_over(self._faulty(shape, "search"))
        res = self.admin.get("/api/memory/records", params={"actor_id": "agent1__river"})
        status, code = self._expected_hub(shape)
        _envelope(res, status, code, what=f"{edge.id}@{shape.label}")
        return Outcome(
            dst=edge.dst,
            clause="memory-backend.md 'Error codes' — a read answers the "
            "mapped envelope, nothing written",
        )


# ---------------------------------------------------------------------------
# reference (cross-entity deletes)
# ---------------------------------------------------------------------------

S3_ATS = ("list_objects_v2", "delete_objects")


class ReferenceFaults(FaultMixin, ReferenceWorld):
    SEAMS = ("runtime", "opensearch", "s3")
    """``ReferenceWorld`` + the fault hook at the storage seams of the cross-entity deletes: the
    memory store delete's S3 purge and OpenSearch `delete_by_query` (memory-backend.md / N14-obs4:
    "the delete is complete once the ledger row is gone; the OpenSearch purge is best-effort"),
    the KB force delete's cleanup steps (WB-008: every storage step is best-effort, the pending
    ones go to a retriable `kb_cleanup` job named in the response) and the agent's Runtime delete
    (as in the agent graph)."""

    def __init__(self, clients, ada_id, fakes) -> None:
        super().__init__(clients, ada_id)
        self._init_faults()
        self.fakes = fakes

    @classmethod
    def fault_shapes(cls, edge: Edge, family: str) -> list[Shape]:
        if edge.id == "memory_delete_after_agent_gone":
            s3 = [with_at(s, at) for s in shapes(family=family, seam="s3") for at in S3_ATS]
            os_ = [
                with_at(s, "delete_by_query")
                for s in shapes(family=family, seam="opensearch")
                if s.kind != "absent"
            ]
            return s3 + os_
        if edge.id == "kb_force_delete_strips":
            return [
                with_at(s, "delete_index")
                for s in shapes(family=family, seam="opensearch")
                if s.kind != "absent"
            ] + [with_at(s, "list_objects_v2") for s in shapes(family=family, seam="s3")]
        if edge.id == "agent_delete_releases_kb":
            if family == "absent":
                return [s for s in shapes(family=family, seam="runtime") if s.kind == "absent"]
            if family == "not_found":
                return []
            return _runtime_client_shapes(family)
        if edge.is_refusal:
            return _representative(family, cls.SEAMS)
        return []

    @classmethod
    def fault_dst(cls, edge: Edge, shape: Shape) -> str:
        if edge.id == "agent_delete_releases_kb":
            return edge.dst if shape.kind == "absent" else edge.src
        return edge.dst

    # -- seams ------------------------------------------------------------------------------
    def _arm_s3(self, shape: Shape, at: str) -> None:
        calls = self.seam_calls

        def boom(**kw):
            calls.append(at)
            raise shape.exception(at)

        self.patcher.set(self.fakes["s3"], at, boom)

    def _arm_os(self, shape: Shape, at: str) -> None:
        calls = self.seam_calls

        def boom(*a, **kw):
            calls.append(at)
            raise shape.exception()

        self.patcher.set(self.fakes["opensearch"], at, boom)

    def _arm_delete(self, shape: Shape) -> None:
        calls = self.seam_calls

        def delete_agent_resources(agent, ws):
            calls.append("DeleteAgentRuntime")
            if shape.kind == "absent":
                return rt.RUNTIME_ABSENT
            raise shape.exception("DeleteAgentRuntime")

        self.patcher.set(agents_router.zip_method, "delete_agent_resources", delete_agent_resources)

    def _arm_all(self, shape: Shape) -> None:
        if shape.seam == "s3":
            self._arm_s3(shape, "list_objects_v2")
        elif shape.seam == "opensearch":
            self._arm_os(shape, "delete_by_query")
            self._arm_os(shape, "delete_index")
        else:
            self._arm_delete(
                shape
                if shape.kind in ("client", "transport")
                else shapes(family="5xx", seam="runtime", kinds=("client",))[0]
            )

    def after_fault(self, edge: Edge, shape: Shape, out: Outcome) -> None:
        """`before("agent_delete_releases_kb")` settles the canary (`cleaned`) and the run
        (`completed`) so the delete is not refused on the ledger; when the FAULT refused it the
        agent is back at `wired`, whose node means "canary running, run active" — restore them."""
        if edge.id == "agent_delete_releases_kb" and out.dst == edge.src:
            from app.evaluation.models import EvalRun
            from app.optimization.models import RuntimeCanary
            from tests.graph.test_graph_reference import _set

            _set(RuntimeCanary, self.canary_id, status="running")
            _set(EvalRun, self.run_id, status="evaluating")

    # -- the hook ----------------------------------------------------------------------------
    def fault(self, edge: Edge, shape: Shape) -> Outcome:
        if edge.is_refusal:
            return self.refusal_with_faulted_seams(edge, shape, lambda: self._arm_all(shape))
        if edge.id == "memory_delete_after_agent_gone":
            return self._faulted_memory_delete(edge, shape)
        if edge.id == "kb_force_delete_strips":
            return self._faulted_kb_force(edge, shape)
        if edge.id == "agent_delete_releases_kb":
            return self._faulted_agent_delete(edge, shape)
        raise NotImplementedError(edge.id)

    def _faulted_memory_delete(self, edge: Edge, shape: Shape) -> Outcome:
        if shape.seam == "s3":
            self._arm_s3(shape, shape.at)
        else:
            self._arm_os(shape, shape.at)
        url = f"/api/memory/resources/{self.memory_id}"
        res, escaped = _call_or_escape(lambda: self.admin.delete(url), shape, what=edge.id)
        gone = self.admin.get(url).status_code == 404
        if escaped is not None or res.status_code != 200:
            answer = (
                f"{type(escaped).__name__} escaped ({aws_error_code(escaped)})"
                if escaped
                else f"{res.status_code} {res.json().get('code')}"
            )
            if gone:
                self.gone.add("memory")
                raise HalfWrite(
                    f"{edge.id}@{shape.label}: the memory store's ledger row is GONE (GET → 404) "
                    f"but "
                    f"the member's delete answered {answer}; the OpenSearch purge after the S3 "
                    f"step "
                    f"never ran — memory-backend.md / N14-obs4: 'the delete is complete once the "
                    f"ledger row is gone; the … purge is best-effort' names OpenSearch only, the "
                    f"S3 "
                    f"purge is unguarded"
                )
            raise AssertionError((edge.id, shape.label, answer, "row kept"))
        body = res.json()
        assert body["id"] == self.memory_id and body["deleted"] is True, body
        step = "s3" if shape.seam == "s3" else "index"
        assert body["cleanup"][step] == "skipped", body
        assert body["cleanup"] == {**{"s3": "ok", "index": "ok"}, step: "skipped"}, body
        assert len(body["cleanup_warnings"]) == 1, body
        if any(marker in res.text for marker in LEAK_MARKERS):
            raise LeakedSeamText(f"{edge.id}@{shape.label}: cleanup_warnings carry {res.text}")
        assert gone
        self.gone.add("memory")
        return Outcome(
            dst=edge.dst,
            writes=frozenset({"managed_memories"}),
            clause="memory-backend.md / N14-obs4 + N17-obs93 — the delete is complete once the "
            "ledger row is gone; both purges are best-effort for every fault shape, the "
            "skipped step is reported with a redacted warning",
        )

    def _faulted_kb_force(self, edge: Edge, shape: Shape) -> Outcome:
        if shape.seam == "s3":
            self._arm_s3(shape, shape.at)
        else:
            self._arm_os(shape, shape.at)
        res = self.admin.delete(f"/api/knowledge-bases/{self.kb_id}?force=true")
        body = _envelope_free(res, 200, what=f"{edge.id}@{shape.label}")
        assert body["deleted"] is True and [a["id"] for a in body["stripped_agents"]] == [
            self.agent_id
        ]
        cleanup = body.get("cleanup") or {}
        pending = [s for s, v in cleanup.items() if v != "ok"]
        assert pending and body.get("cleanup_job_id"), (
            "a faulted cleanup step must leave a pending step and a retriable cleanup job",
            body,
        )
        assert "Traceback" not in res.text, res.text[:400]
        leaked = [m for m in LEAK_MARKERS if m in json.dumps(body.get("cleanup_warnings") or [])]
        if leaked:  # N17-obs99 fixed in cn/r17-n: type + status / code + operation only
            raise LeakedSeamText(
                f"{edge.id}@{shape.label}: cleanup_warnings carry raw seam text {leaked} — "
                f"knowledge-backend.md WB-008: never a message or a response body"
            )
        observation = ""
        out = self.ada.get(f"/api/agents/{self.agent_id}").json()
        assert out["spec"].get("knowledge_bases", []) == [] and out["kb_stale"] is True
        assert self.admin.get(f"/api/knowledge-bases/{self.kb_id}").status_code == 404
        self.gone.add("kb")
        return Outcome(
            dst=edge.dst,
            writes=frozenset({"knowledge_bases", "agents", "jobs"}),
            clause="WB-008 (knowledge_selfhosted.delete_kb) — every storage step is "
            "best-effort; the pending steps go to a retriable kb_cleanup job named "
            "in the response",
            observation=observation,
        )

    def _faulted_agent_delete(self, edge: Edge, shape: Shape) -> Outcome:
        self._arm_delete(shape)
        if shape.kind == "absent":
            res = self.ada.delete(f"/api/agents/{self.agent_id}")
            body = _envelope_free(res, 200, what=edge.id)
            assert body["deleted"] is True and body["aws_resource_deleted"] is False, body
            assert self.agent_status() == "deleted"
            res = self.admin.delete(f"/api/knowledge-bases/{self.kb_id}")
            assert res.status_code == 200 and res.json()["deleted"] is True, res.text
            self.gone.add("kb")
            return Outcome(
                dst=edge.dst,
                writes=frozenset({"agents", "knowledge_bases", "jobs"}),
                clause="N16-obs87 + API_KB_DELETE — the runtime already gone: the row goes, "
                "the KB is released",
            )
        res, escaped = _call_or_escape(
            lambda: self.ada.delete(f"/api/agents/{self.agent_id}"), shape, what=edge.id
        )
        if escaped is None:
            mapped = _mapped(shape)
            assert mapped is not None, (shape.label, res.status_code, res.text[:300])
            _envelope(res, *mapped, what=edge.id)
        assert self.agent_status() == "active"
        res = self.admin.delete(f"/api/knowledge-bases/{self.kb_id}")
        assert res.status_code == 409 and res.json()["code"] == "kb.has_attached_agents", res.text
        return Outcome(
            dst=edge.src,
            clause="api.md 'Errors' + N16-obs87 — the row kept, the KB still "
            "mounted (409 kb.has_attached_agents)",
            observation=f"documented unhandled 500 ({aws_error_code(escaped)})" if escaped else "",
        )


WORLDS: dict[str, type] = {
    "agent": AgentFaults,
    "chat_session": ChatFaults,
    "eval_run": EvalRunFaults,
    "memory_record": MemoryFaults,
    "reference": ReferenceFaults,
}


def make_client_dict(
    admin: TestClient, ada: tuple[str, TestClient], bob: tuple[str, TestClient]
) -> tuple[dict[str, TestClient], str]:
    ada_id, ada_client = ada
    _bob_id, bob_client = bob
    return {"admin": admin, "ada": ada_client, "bob": bob_client}, ada_id


# ---------------------------------------------------------------------------
# runtime canary (round 7, r17-m): the cleanup's AWS seams
# ---------------------------------------------------------------------------

from app.optimization import canary_infra, canary_service  # noqa: E402
from app.optimization.canary_service import cleanup_blockers  # noqa: E402
from tests.graph.test_graph_runtime_canary import CanaryWorld  # noqa: E402

CLEANUP_EDGES = ("cleanup_completed", "cleanup_rolled_back", "cleanup_again")
#: where a cleanup shape lands: the endpoint delete, the live-version read that guards the
#: candidate-zip delete, the candidate-zip delete itself, the endpoints-gone wait
CLEANUP_ATS = ("endpoint", "candidate_version", "candidate_object", "endpoints_gone")
CANARY_TABLES = frozenset({"runtime_canaries", "online_eval_configs", "experiment_assignments"})


def _modelled(exc: ClientError) -> ClientError:
    """boto3 raises the service's MODELLED exception class — `type(exc).__name__` is the AWS
    code (`ResourceNotFoundException`), which is what `canary_infra._NOT_FOUND` matches; a bare
    `ClientError` would be a stub artefact the real wrapper never sees."""
    code = aws_error_code(exc)
    cls = type(code, (ClientError,), {})
    return cls(exc.response, exc.operation_name)


class CanaryFaults(FaultMixin, CanaryWorld):
    SEAMS = ("runtime", "s3")
    """``CanaryWorld`` + the fault hook at the cleanup's AWS seams (evaluation-engine.md
    "Cleanup waits for the named endpoints to disappear (N-SCN-obs3)": every delete is
    per-category tolerant, a refused endpoint delete is a `skipped` row, "the row flips to
    `cleaned` either way", a listing error leaves the names pending and never fails the
    cleanup; `cleanup_blockers`: "`skipped` is never success" except the live-version zip and the
    endpoints-gone marker). The recovery oracle is a second `cleanup` on a healthy seam
    (`cleanup_again` is a graph edge): the skipped categories must come back `deleted`."""

    def __init__(self, clients, ada_id, seams, dataset_id, agent_id=None) -> None:
        super().__init__(clients, ada_id, seams, dataset_id, agent_id)
        self._init_faults()

    @classmethod
    def fault_shapes(cls, edge: Edge, family: str) -> list[Shape]:
        if edge.id in CLEANUP_EDGES:
            if family == "absent":
                return [
                    with_at(s, "endpoints_gone")
                    for s in shapes(family=family, seam="runtime")
                    if s.kind == "absent"
                ]
            out = [
                with_at(s, at)
                for s in _runtime_client_shapes(family)
                for at in ("endpoint", "candidate_version")
            ]
            out += [
                with_at(s, "candidate_object")
                for s in shapes(family=family, seam="s3")
                if s.kind == "client"
            ]
            return out
        if edge.is_refusal:
            return _representative(family, cls.SEAMS)
        return []

    @classmethod
    def fault_dst(cls, edge: Edge, shape: Shape) -> str:
        return "cleaned" if edge.id in CLEANUP_EDGES else edge.dst

    # -- seams ------------------------------------------------------------------------------
    def _arm_cleanup(self, shape: Shape) -> None:
        calls = self.seam_calls
        if shape.at == "endpoint":
            exc = shape.exception("DeleteAgentRuntimeEndpoint")
            if isinstance(exc, ClientError):
                exc = _modelled(exc)
            real = (
                canary_infra.delete_endpoint_quiet.__wrapped__
                if hasattr(canary_infra.delete_endpoint_quiet, "__wrapped__")
                else None
            )

            def delete_runtime_endpoint(client, *, runtime_id, endpoint_name):
                calls.append("DeleteAgentRuntimeEndpoint")
                raise exc

            # the REAL quiet wrapper over a raising control plane (the fixture replaced the
            # wrapper with a recorder; the not-found swallow is the wrapper's own rule)
            self.patcher.set(
                canary_infra, "delete_endpoint_quiet", real or _REAL_DELETE_ENDPOINT_QUIET
            )
            self.patcher.set(canary_infra.rt, "delete_runtime_endpoint", delete_runtime_endpoint)
        elif shape.at == "candidate_version":

            def current_version(control, runtime_id):
                calls.append("GetAgentRuntime")
                raise shape.exception("GetAgentRuntime")

            self.patcher.set(canary_service, "_cleanup_candidate_objects", _REAL_CANDIDATE_OBJECTS)
            self.patcher.set(canary_infra, "current_version", current_version)
        elif shape.at == "candidate_object":

            def delete_object_quiet(key, workspace, **kw):
                calls.append("DeleteObject")
                raise shape.exception("DeleteObject")

            self.patcher.set(canary_service, "_cleanup_candidate_objects", _REAL_CANDIDATE_OBJECTS)
            self.patcher.set(canary_infra, "current_version", lambda control, runtime_id: "1")
            self.patcher.set(canary_infra, "delete_object_quiet", delete_object_quiet)
        elif shape.at == "endpoints_gone":

            def wait_endpoints_gone(control, *, runtime_id, endpoint_names, timeout_s, log):
                calls.append("ListAgentRuntimeEndpoints")
                return list(endpoint_names)  # still listed at the deadline (eventually consistent)

            self.patcher.set(canary_infra, "wait_endpoints_gone", wait_endpoints_gone)
        else:
            raise NotImplementedError(shape.at)

    def _arm_all(self, shape: Shape) -> None:
        exc_shape = (
            shape
            if shape.kind in ("client", "transport")
            else shapes(family="5xx", seam="runtime", kinds=("client",))[0]
        )
        self._arm_cleanup(with_at(exc_shape, "endpoint"))

    # -- the hook ----------------------------------------------------------------------------
    def fault(self, edge: Edge, shape: Shape) -> Outcome:
        if edge.is_refusal:
            return self.refusal_with_faulted_seams(edge, shape, lambda: self._arm_all(shape))
        if edge.id in CLEANUP_EDGES:
            return self._faulted_cleanup(edge, shape)
        raise NotImplementedError(edge.id)

    def _faulted_cleanup(self, edge: Edge, shape: Shape) -> Outcome:
        setup = self.row().artifacts.get("setup") or {}
        if not setup:
            # a canary rolled back from `fresh` (or cleaned without a setup) owns no endpoints
            # and no candidate zip: the cleanup never reaches the faulted seam — a plain step
            self._cleanup(edge)
            return Outcome(
                dst="cleaned",
                writes=CANARY_TABLES,
                clause="evaluation-engine.md N-SCN-obs3 — nothing to delete at this node",
                observation="no setup artifacts: the faulted seam was not reached",
            )
        self._arm_cleanup(shape)
        res = self.act("cleanup")
        assert res.status_code == 202, (res.status_code, res.text[:300])
        row = self.row()
        assert row.running_action is None, row.running_action
        assert row.error is None, ("the cleanup failed as a whole", row.error)
        assert row.status == "cleaned", row.status  # "flips to `cleaned` either way"
        results = list(row.artifacts.get("cleanup") or [])
        assert _online_ids_of(self.canary_id) == [] and _assignments_of(self.canary_id) == 0
        blockers = cleanup_blockers(results)
        skipped = [r for r in results if r.get("status") == "skipped"]
        # the endpoint seam raises the MODELLED class (its name = the AWS code); the other seams
        # raise the shape as built
        name = (
            ""
            if shape.kind == "absent"
            else (
                (shape.code or "")
                if shape.at == "endpoint" and shape.kind == "client"
                else type(shape.exception()).__name__
            )
        )
        observation = ""
        if shape.at == "endpoint":
            swallowed = shape.code == "ResourceNotFoundException"
            endpoints = [r for r in results if r["category"].startswith("endpoint:")]
            assert endpoints, results
            if swallowed:
                assert all(r["status"] == "deleted" for r in endpoints), endpoints
                assert not blockers, blockers
                clause = (
                    "canary_infra.delete_endpoint_quiet — a not-found is swallowed (already gone)"
                )
            else:
                assert all(r["status"] == "skipped" and name in r["detail"] for r in endpoints), (
                    endpoints
                )
                assert blockers and all(b["category"].startswith("endpoint:") for b in blockers)
                clause = (
                    "evaluation-engine.md N-SCN-obs3 — a refused endpoint delete is a `skipped` "
                    "row, the row flips to `cleaned` either way; cleanup_blockers names it"
                )
                observation = (
                    f"cleaned WITH blockers {[b['category'] for b in blockers]} — the "
                    f"agent delete is unblocked (agents.md §1.1: `cleaned` never blocks) "
                    f"while the endpoints stayed; only a manual `cleanup` again retries"
                )
        elif shape.at == "candidate_version":
            zips = [r for r in results if r["category"].startswith("s3:")]
            assert zips and all(
                r["status"] == "skipped"
                and "live version unknown" in r["detail"]
                and name in r["detail"]
                for r in zips
            ), results
            assert not blockers, blockers  # s3: skips are not blockers by contract
            clause = (
                "canary_service._cleanup_candidate_objects — unknown live version → keep "
                "every object (reported skipped, never a blocker)"
            )
        elif shape.at == "candidate_object":
            zips = [r for r in results if r["category"].startswith("s3:")]
            assert zips and any(r["status"] == "skipped" and name in r["detail"] for r in zips), (
                results
            )
            assert not blockers, blockers
            clause = (
                "canary_service._cleanup_candidate_objects — per-key tolerance (skipped, not a "
                "blocker)"
            )
        else:  # endpoints_gone
            gone = [r for r in results if r["category"] == "endpoints-gone"]
            assert (
                gone and gone[0]["status"] == "skipped" and "still listed" in gone[0]["detail"]
            ), results
            assert row.artifacts.get("endpoints_pending"), row.artifacts.keys()
            assert not blockers, blockers
            clause = (
                "evaluation-engine.md N-SCN-obs3 — names still listed at the deadline: "
                "`endpoints-gone` skipped + `endpoints_pending`, the row `cleaned`"
            )
        # recovery: a second cleanup on a healthy seam converges (the `cleanup_again` edge)
        self.patcher.restore()
        if skipped and shape.at in ("endpoint", "candidate_object"):
            res = self.act("cleanup")
            assert res.status_code == 202, res.text[:300]
            again = list(self.row().artifacts.get("cleanup") or [])
            left = [r for r in cleanup_blockers(again)]
            assert not left, ("a healthy cleanup again did not clear the blockers", left)
        return Outcome(dst="cleaned", writes=CANARY_TABLES, clause=clause, observation=observation)


_REAL_DELETE_ENDPOINT_QUIET = canary_infra.delete_endpoint_quiet
_REAL_CANDIDATE_OBJECTS = canary_service._cleanup_candidate_objects


def _online_ids_of(canary_id: str) -> list[str]:
    from tests.model.test_canary_state_machine import _online_ids

    return _online_ids(canary_id)


def _assignments_of(canary_id: str) -> int:
    from tests.model.test_canary_state_machine import _assignments

    return _assignments(canary_id)


WORLDS["runtime_canary"] = CanaryFaults


# ---------------------------------------------------------------------------
# architect proposal (round 9, r17-s): the CLEAR job's AWS readbacks
# ---------------------------------------------------------------------------

from app.assistant import purge  # noqa: E402
from app.deployer import teardown  # noqa: E402
from tests.graph.executor import COUNTED  # noqa: E402
from tests.graph.test_graph_architect_proposal import CONV_URL, ProposalWorld, _rows  # noqa: E402
from tests.test_assistant_purge_dependencies import wait_job  # noqa: E402

PURGE_ATS = ("agent_readback", "foreign_endpoints", "agent_delete", "residue_role")
ALL_TABLES = frozenset(m.__tablename__ for m in COUNTED)
#: assistant-turns.md §10 — the blocker a faulted step lands on the job
PURGE_BLOCKER = {
    "agent_readback": "assistant.purge_step_failed",
    "foreign_endpoints": "assistant.purge_step_failed",
    "agent_delete": "assistant.purge_step_failed",  # ConflictException → purge_agent_conflict
    "residue_role": "assistant.purge_role_remains",
}


class ProposalFaults(FaultMixin, ProposalWorld):
    SEAMS = ("runtime",)
    """``ProposalWorld`` + the fault hook at the CLEAR job's AWS readbacks (assistant-turns.md
    §10): `agent_remaining` (GetAgentRuntime, the `_wait_gone` probe), `foreign_endpoints`
    (ListAgentRuntimeEndpoints), `delete_agent_cloud_resource` (DeleteAgentRuntime) and the
    residue sweep's load-bearing role step. Contract: a step's exception is "recorded,
    retryable" — the job `failed` with a named blocker (`purge_step_failed`,
    `purge_agent_conflict`, `purge_role_remains`), the conversation rows and the Agent row kept
    (the ledger flips only after the resource is read back gone), checkpoints in
    `payload.progress` and "an accepted Runtime delete request carry forward to the next
    attempt"; the recovery oracle is the retry (a second DELETE → a new job unioned with the
    previous pins) converging to `succeeded` with the delete NOT re-issued when it was accepted."""

    def __init__(self, clients, harness, cloud) -> None:
        super().__init__(clients, harness)
        self._init_faults()
        self.cloud = cloud

    @classmethod
    def fault_shapes(cls, edge: Edge, family: str) -> list[Shape]:
        if edge.id == "clear_deployed":
            pool = _runtime_client_shapes(family)
            out = [
                with_at(s, at)
                for s in pool
                for at in ("agent_readback", "foreign_endpoints", "agent_delete")
            ]
            if family in ("access_denied", "5xx", "throttle"):
                out += [with_at(s, "residue_role") for s in pool if s.kind == "client"]
            return out
        if edge.is_refusal:
            return _representative(family, cls.SEAMS)
        return []

    @classmethod
    def fault_dst(cls, edge: Edge, shape: Shape) -> str:
        return "cleared" if edge.id == "clear_deployed" else edge.dst

    # -- seams ------------------------------------------------------------------------------
    def _arm_purge(self, shape: Shape) -> None:
        calls = self.seam_calls
        exc = shape.exception(
            {
                "agent_readback": "GetAgentRuntime",
                "foreign_endpoints": "ListAgentRuntimeEndpoints",
                "agent_delete": "DeleteAgentRuntime",
                "residue_role": "DeleteRole",
            }[shape.at]
        )
        if isinstance(exc, ClientError):
            exc = _modelled(exc)
        if shape.at in ("agent_readback", "foreign_endpoints"):
            name = "agent_remaining" if shape.at == "agent_readback" else "foreign_endpoints"
            real = getattr(purge, name)
            state = {"armed": True}

            def once(agent, workspace, _real=real, _exc=exc):
                calls.append(name)
                if state["armed"]:
                    state["armed"] = False
                    if purge._is_not_found(_exc):
                        # the REAL readback wrappers swallow a not-found control-plane answer
                        # ("already gone") — the cloud fixture replaced the wrapper, so the
                        # wrapper's own rule is applied here, at the same layer
                        return []
                    raise _exc
                return _real(agent, workspace)

            self.patcher.set(purge, name, once)
        elif shape.at == "agent_delete":
            self.cloud.delete_error = exc
        else:
            self.cloud.residue = [
                teardown.TeardownStep(
                    teardown.STEP_ROLE, teardown.FAILED, f"{type(exc).__name__}: {exc}"
                )
            ]

    def _disarm_purge(self) -> None:
        self.patcher.restore()
        self.cloud.delete_error = None
        self.cloud.residue = [
            teardown.TeardownStep(teardown.STEP_ROLE, teardown.DELETED, "launchpad-agent-kid"),
            teardown.TeardownStep(teardown.STEP_ARTIFACTS, teardown.DELETED, "agents/kid/"),
            teardown.TeardownStep(
                teardown.STEP_REGISTRY, teardown.SKIPPED, "agent has no registry record"
            ),
            teardown.TeardownStep(teardown.STEP_LOGS, teardown.DELETED, "1 group(s)"),
        ]

    # -- the hook ----------------------------------------------------------------------------
    def fault(self, edge: Edge, shape: Shape) -> Outcome:
        if edge.is_refusal:
            try:
                return self.refusal_with_faulted_seams(
                    edge, shape, lambda: self._arm_purge(with_at(shape, "agent_delete"))
                )
            finally:
                self._disarm_purge()  # the cloud knobs live outside the patcher
        if edge.id == "clear_deployed":
            return self._faulted_clear(edge, shape)
        raise NotImplementedError(edge.id)

    def _faulted_clear(self, edge: Edge, shape: Shape) -> Outcome:
        self._arm_purge(shape)
        deletes_before = sum(1 for e in self.cloud.events if e.startswith("agent-delete:"))
        try:
            res = self.admin.delete(f"{CONV_URL}/{self.cid}")
            assert res.status_code == 202, (res.status_code, res.text[:300])
            job = wait_job(res.json()["job_id"])
        finally:
            self._disarm_purge()
        swallowed = (
            shape.at in ("agent_readback", "foreign_endpoints")
            and shape.code == "ResourceNotFoundException"
        )
        if swallowed:
            # purge.agent_remaining / foreign_endpoints: a not-found readback is "gone" by design
            assert job.status == "succeeded", (job.status, job.error)
            self.assert_purged()
            return Outcome(
                dst="cleared",
                writes=ALL_TABLES,
                clause="purge.agent_remaining — a not-found readback reads as gone",
            )
        assert job.status == "failed", (job.status, job.error, job.payload)
        blocker = (job.payload or {}).get("blocker") or {}
        expected = PURGE_BLOCKER[shape.at]
        if shape.at == "agent_delete" and shape.code == "ConflictException":
            expected = "assistant.purge_agent_conflict"
        assert blocker.get("code") == expected, (blocker, expected)
        # nothing half-deleted: the conversation rows and the Agent row stay
        assert _rows(self.cid)["conversation"] == 1, _rows(self.cid)
        db = SessionLocal()
        try:
            agent = db.get(Agent, self.agent_id)
            assert agent is not None and agent.status != "deleted", agent and agent.status
        finally:
            db.close()
        progress = (job.payload or {}).get("progress") or {}
        key = f"agent:{self.agent_id}"
        accepted = bool((progress.get(key) or {}).get("delete_requested"))
        if shape.at == "agent_readback":
            # the delete was accepted before the readback failed: the checkpoint carries it
            assert accepted, progress
        elif shape.at in ("foreign_endpoints", "agent_delete"):
            assert not accepted, progress
        # recovery: the retry — a new job unioned with the previous pins — converges
        res = self.admin.delete(f"{CONV_URL}/{self.cid}")
        assert res.status_code == 202, (res.status_code, res.text[:300])
        job2 = wait_job(res.json()["job_id"])
        assert job2.status == "succeeded", (
            job2.status,
            job2.error,
            (job2.payload or {}).get("blocker"),
        )
        assert (job2.payload or {}).get("previous_job_id") == job.id
        self.assert_purged()
        deletes = (
            sum(1 for e in self.cloud.events if e.startswith("agent-delete:")) - deletes_before
        )
        observation = ""
        if accepted:
            # "an accepted Runtime delete request carries forward to the next attempt"
            assert deletes == 1, ("the retry re-issued DeleteAgentRuntime", self.cloud.events[-8:])
        elif deletes != 2 and shape.at == "agent_delete":
            observation = f"the retry issued {deletes - 1} delete(s) after a refused first one"
        db = SessionLocal()
        try:
            assert db.get(Agent, self.agent_id).status == "deleted"
        finally:
            db.close()
        return Outcome(
            dst="cleared",
            writes=ALL_TABLES,
            clause="assistant-turns.md §10 — a step's exception is recorded, retryable "
            f"({expected}); the retry resumes from the verified checkpoints",
            observation=observation,
        )


WORLDS["architect_proposal"] = ProposalFaults
