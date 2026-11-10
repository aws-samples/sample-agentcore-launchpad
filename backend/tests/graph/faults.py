"""Fault-injected walks (round 17, loop engine round 6, r17-l) — the recorded seam error
shapes replayed at the data-plane seams WHILE the lifecycle graphs walk.

Rounds 1–5 walked the graphs over a healthy stubbed AWS; round 5 yielded nothing, so the
method changes (loop_rounds.yaml header). A **fault walk** is a seeded random walk
(``tests/graph/walks.py``) in which 1–3 steps carry a **shape**: one of the REAL error / response
shapes recorded at the Runtime / OpenSearch / S3 / SSM seams on 2026-10-19
(``tests/fixtures/seams/``, the N-ARCH-obs18 / N14-obs4 live shapes of ``tests/faults`` and
``tests/property/test_memory_merge_properties.REAL_FAULTS``). The shape is served at the edge's
data-plane seam by the world (``World.fault(edge, shape)`` — one hook per world, implemented in
``tests/graph/fault_seams.py``), and the walk continues from the node the CONTRACT says the
failure leaves the entity in (``World.fault_dst``), so the history after a fault is as long as
the one before it.

Oracles after a faulted step (every one cites a contract clause or an invariant of
testing.md §6.7 / §6.8 — a step that fails one is a FINDING, shrunk and registered like a walk
failure, never a weaker oracle):

* the **contract's answer** where a sentence exists — the mapped envelope of api.md "Errors use
  the platform envelope" (`aws.*`, `memory.*` codes, N16-obs87's AccessDenied rule), the stage
  failure sentences of agents.md §1 / §4, the replay semantics of evaluation-engine.md
  (`error_kind: infrastructure`), the memory extractor's `RetryableError`;
* **no half-written row** — the executor's per-table digest (plus the world's extra stores):
  a refused / failed call changed nothing, or exactly the tables the contract's failure state
  names (``Outcome.writes``); the own-id ledger scan and the world's extra scans
  (`scan_memory_index`, dangling references) are clean;
* the **terminal state** — the job / turn / run reaches a terminal status inside a bounded
  wait that is a join on its driver (never a sleep), and the next edge is enabled at the
  contract's node;
* the **error envelope** — an HTTP failure is `{code, message, detail}` (api.md "never a bare
  500"); a failure that escapes the handlers (TestClient re-raises it) is an
  ``EnvelopeViolation`` unless api.md names it as the documented unhandled 500
  (`InternalServerException`), and NO raw AWS / OpenSearch body text reaches an envelope
  (`opensearch_error_handler`: "a static, per-code sentence, never str(exc)" — the recorded
  IAM 403 body carries the role ARN);
* the **retry / resume path converges** — `pipeline.execute_deploy_job` on the terminal job is
  inert and a job a crash left `running` resumes to a clean terminal state; a reattach after a
  faulted turn replays the persisted log and ends on the terminal frame; the extractor's
  redelivery after a faulted extraction leaves a clean index with the op applied.

A fault at an edge for which NO contract sentence names the outcome is still driven; its
observed behaviour is recorded (``Outcome.observation``) and registered as a DOC gap.

Budget: NOT in ``make verify`` since r17-s2 (N17-obs287, design review 10.5 — rounds 8–9 of
this method yielded nothing, so under the loop stop rule the seeded walks are on-demand).
``LAUNCHPAD_FAULT_WALKS=1`` (``make graph-faults``, also set by ``make graph-coverage``) runs
the fixed shape: ``VERIFY_SEEDS`` (4) seeds per graph, the family drawn by the seed; under
``LAUNCHPAD_SOAK=1`` (``make graph-soak``) every shape family × ``SOAK_SEEDS_PER_FAMILY`` (20)
seeds per graph, in the round's fresh window (`LAUNCHPAD_LOOP_ROUND`). The deterministic fault
grids next to the walks (the refusal-seam axis, the workspace stage injection, the pinned
`test_N17_obs*` regressions and the `test_graph_fault_probes_*` probes) stay in the gate.
"""

from __future__ import annotations

import json
import os
import random
import traceback
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import pytest
import urllib3
from botocore.exceptions import (
    ClientError,
    EndpointConnectionError,
    ReadTimeoutError,
    UnknownServiceError,
)

from app.services import opensearch_http as core
from tests.faults.conftest import STREAMING_MESSAGE, Fault, _bedrock_5xx, _read_timeout, _throttle
from tests.graph import coverage
from tests.graph.paths import Edge, Graph
from tests.graph.walks import SHRINK_BUDGET, Failure, loop_round, repro_hash, soak

SEAMS_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "seams"

#: The shape families a soak iterates (one axis of the soak grid; the verify seeds draw one).
FAMILIES: tuple[str, ...] = (
    "5xx",
    "throttle",
    "timeout",
    "frame",
    "not_found",
    "access_denied",
    "conflict",
    "absent",
    "interrupted",
    # r17-ws (the workspace graph): a service whose endpoint cannot be RESOLVED / no such
    # operation in the workspace's region — the `validate-access` "region not supported"
    # verdict class (N17-obs200: a resolved-but-unreachable endpoint is NOT in it — that shape,
    # `ws_endpoint_unreachable`, is served in the transport `timeout` family)
    "region_unsupported",
)
MIN_LEN, MAX_LEN = 20, 60
VERIFY_SEEDS = 4
SOAK_SEEDS_PER_FAMILY = 20
MIN_FAULTS, MAX_FAULTS = 1, 3


def _fixture(rel: str) -> dict[str, Any]:
    return json.loads((SEAMS_DIR / rel).read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# the shapes
# ---------------------------------------------------------------------------


class EnvelopeViolation(AssertionError):
    """A route answered a fault with no envelope at all — the exception escaped the handlers
    (api.md "An AWS-side failure … is still returned as an envelope, never as a bare 500") and
    no sentence names it as the documented unhandled 500."""


class HalfWrite(AssertionError):
    """A failed call left a partial write: tables / stores changed that the contract's failure
    state does not name (testing.md §6.7 "no half-written row")."""


class LeakedSeamText(AssertionError):
    """Raw seam body text (the IAM sentence, a role ARN, an OpenSearch body) reached a console
    envelope (`opensearch_error_handler`: "a static, per-code sentence, never str(exc)")."""


@dataclass(frozen=True)
class Shape:
    """One recorded failure shape. ``kind`` says how it is served: ``client`` (a botocore
    ``ClientError``), ``transport`` (botocore ``ReadTimeoutError``), ``frame`` (the SDK's
    terminal error frame inside an SSE stream), ``lines`` (raw SSE ``data:`` lines — the
    malformed / partial-frame shapes), ``opensearch`` (``OpenSearchError(status, body)``),
    ``os_transport`` (``urllib3.exceptions.ReadTimeoutError``), ``absent`` (an eventually-
    consistent empty read / already-gone answer — no exception), ``interrupt`` (a
    ``BaseException`` that kills the worker mid-step: the restart class), ``endpoint``
    (botocore ``EndpointConnectionError`` — the endpoint resolved but could not be connected
    to: the N17-obs200 network / DNS verdict, served in the transport ``timeout`` family),
    ``unknown_service`` (botocore ``UnknownServiceError`` — endpoint resolution failed before
    any connect: the ``region_unsupported`` verdict)."""

    name: str
    family: str
    seam: str  # runtime | opensearch | s3 | ssm | stage | any
    source: str  # where the shape was recorded / which fixture pins it
    kind: str
    code: str | None = None
    status: int | None = None
    message: str = ""
    error_type: str | None = None
    endpoint: str = ""
    body: str = ""
    lines: tuple[str, ...] = ()
    at: str = (
        ""  # a variant tag the world interprets (the stage / method / attempt the shape lands at)
    )

    @property
    def label(self) -> str:
        return f"{self.name}{'/' + self.at if self.at else ''}"

    def exception(self, operation: str = "InvokeAgentRuntime") -> BaseException:
        if self.kind == "client":
            return ClientError(
                {
                    "Error": {"Code": self.code or "", "Message": self.message},
                    "ResponseMetadata": {"HTTPStatusCode": self.status or 500},
                },
                operation,
            )
        if self.kind == "transport":
            return ReadTimeoutError(
                endpoint_url=self.endpoint or "https://seam.test/", error=self.message
            )
        if self.kind == "opensearch":
            return core.OpenSearchError(self.status or 500, self.body)
        if self.kind == "os_transport":
            return urllib3.exceptions.ReadTimeoutError(
                None, "/launchpad-memory/_search", self.message
            )
        if self.kind == "frame":
            from app.services.agentcore import runtime as rt

            return rt.sdk_stream_error(self.frame())
        if self.kind == "interrupt":
            return Interrupted(self.message or "worker process died mid-step (fault layer)")
        if self.kind == "endpoint":
            return EndpointConnectionError(endpoint_url=self.endpoint or "https://seam.test/")
        if self.kind == "unknown_service":
            return UnknownServiceError(service_name=self.message or "ecr",
                                       known_service_names=["s3", "iam"])
        raise TypeError(f"shape {self.name} ({self.kind}) is not an exception shape")

    def partial_delete(self, keys: list[str]) -> dict[str, Any]:
        """The `s3_delete_partial` body: the first key deleted, every other key refused."""
        assert self.kind == "partial", self.name
        return {
            "Deleted": [{"Key": keys[0]}] if keys else [],
            "Errors": [
                {"Key": k, "Code": "AccessDenied", "Message": "Access Denied"} for k in keys[1:]
            ],
        }

    def frame(self) -> dict[str, Any]:
        assert self.kind == "frame", self.name
        return {"error": self.message, "error_type": self.error_type, "message": STREAMING_MESSAGE}

    @property
    def transient(self) -> bool:
        """What the ONE upstream-transient predicate says about this shape (runtime_invoke)."""
        from app.services.agentcore import runtime as rt

        if self.kind in ("client", "transport", "frame"):
            return rt.is_upstream_transient(self.exception())
        return False


class Interrupted(BaseException):
    """Not an ``Exception``: the pipeline's ``except Exception`` never sees it, so the job is
    left exactly as a process death leaves it (`running`, the stage `running`)."""


def _from_fault(f: Fault, family: str, seam: str, source: str, **over: Any) -> Shape:
    kind = (
        "frame" if f.surface == "frame" else ("transport" if f.surface == "transport" else "client")
    )
    return Shape(
        name=f"{seam}_{f.name}_{f.surface}",
        family=family,
        seam=seam,
        source=source,
        kind=kind,
        code=f.code,
        status=f.http_status,
        message=f.error,
        error_type=f.error_type,
        **over,
    )


def _recorded_client(rel: str, family: str, seam: str, name: str) -> Shape:
    d = _fixture(rel)["response_or_error"]
    return Shape(
        name=name,
        family=family,
        seam=seam,
        source=f"tests/fixtures/seams/{rel}",
        kind="client",
        code=d["Error"]["Code"],
        status=d["ResponseMetadata"]["HTTPStatusCode"],
        message=d["Error"]["Message"],
    )


def _recorded_os(rel: str, family: str, name: str) -> Shape:
    d = _fixture(rel)["response_or_error"]
    return Shape(
        name=name,
        family=family,
        seam="opensearch",
        source=f"tests/fixtures/seams/{rel}",
        kind="opensearch",
        status=d["http_status"],
        body=json.dumps(d["body"]),
    )


def _build_shapes() -> dict[str, Shape]:
    faults_src = "tests/faults/conftest.py (N-ARCH-obs18 live shapes)"
    rt_timeout = _fixture("agentcore-runtime/invoke_read_timeout.json")["response_or_error"]
    os_timeout = _fixture("opensearch-memory/read_timeout.json")["response_or_error"]
    partial = _fixture("agentcore-runtime/invoke_stream_success.json")["response_or_error"]
    healthy = _fixture("agentcore-runtime/invoke_second_turn_same_session.json")[
        "response_or_error"
    ]
    rejected = _fixture("agentcore-runtime/eval_worker_invalid_job_payload.json")[
        "response_or_error"
    ]
    conflict = _fixture("console-api/agents_delete_seam-probe-unsupported.json")[
        "response_or_error"
    ]
    iam403 = _fixture("opensearch-memory/iam_identity_403_instance_role.json")["response_or_error"]
    from tests.property.test_memory_merge_properties import REAL_FAULTS

    shapes = [
        # -- 5xx -------------------------------------------------------------------------
        _from_fault(_bedrock_5xx("client"), "5xx", "runtime", faults_src),
        _from_fault(_bedrock_5xx("frame"), "5xx", "runtime", faults_src),
        Shape(
            "os_503_no_master",
            "5xx",
            "opensearch",
            "test_memory_merge_properties.REAL_FAULTS (N14-obs4)",
            "opensearch",
            status=503,
            body=str(REAL_FAULTS["unavailable_503"])[len("OpenSearch HTTP 503: ") :],
        ),
        Shape(
            "os_502_bad_gateway",
            "5xx",
            "opensearch",
            "test_memory_merge_properties.REAL_FAULTS",
            "opensearch",
            status=502,
            body=str(REAL_FAULTS["bad_gateway_502"])[len("OpenSearch HTTP 502: ") :],
        ),
        Shape(
            "s3_internal_error",
            "5xx",
            "s3",
            "botocore shape (S3 InternalError, not recorded on t1)",
            "client",
            code="InternalError",
            status=500,
            message="We encountered an internal error. Please try again.",
        ),
        # -- throttle ----------------------------------------------------------------------
        _from_fault(_throttle("client"), "throttle", "runtime", faults_src),
        _from_fault(_throttle("frame"), "throttle", "runtime", faults_src),
        Shape(
            "os_429_rejected",
            "throttle",
            "opensearch",
            "test_memory_merge_properties.REAL_FAULTS (N14-obs4)",
            "opensearch",
            status=429,
            body=str(REAL_FAULTS["throttle_429"])[len("OpenSearch HTTP 429: ") :],
        ),
        Shape(
            "s3_slow_down",
            "throttle",
            "s3",
            "botocore shape (S3 SlowDown 503)",
            "client",
            code="SlowDown",
            status=503,
            message="Please reduce your request rate.",
        ),
        # -- timeout -----------------------------------------------------------------------
        Shape(
            "rt_read_timeout",
            "timeout",
            "runtime",
            "tests/fixtures/seams/agentcore-runtime/invoke_read_timeout.json",
            "transport",
            endpoint=rt_timeout["kwargs"]["endpoint_url"],
            message=rt_timeout["kwargs"]["error"],
        ),
        _from_fault(_read_timeout("frame"), "timeout", "runtime", faults_src),
        Shape(
            "os_read_timeout",
            "timeout",
            "opensearch",
            "tests/fixtures/seams/opensearch-memory/read_timeout.json",
            "os_transport",
            message=os_timeout["str"],
        ),
        # -- frame (malformed / partial SSE, the worker's REJECTED body) -----------------------
        Shape(
            "rt_frame_no_workload_token",
            "frame",
            "runtime",
            "tests/fixtures/seams/agentcore-runtime/invoke_stream_success.json",
            "frame",
            message=partial["first_3_lines"][0][len('data: {"error": "') :].split('", "erro')[0],
            error_type="RuntimeError",
        ),
        Shape(
            "rt_partial_frame",
            "frame",
            "runtime",
            "tests/fixtures/seams/agentcore-runtime/invoke_stream_success.json (the recorder's "
            "300-char cut = a connection dropped mid-frame)",
            "lines",
            lines=(*healthy["first_3_lines"], partial["first_3_lines"][0]),
        ),
        Shape(
            "rt_partial_frame_only",
            "frame",
            "runtime",
            "tests/fixtures/seams/agentcore-runtime/invoke_stream_success.json (the whole "
            "response is the one cut line, as recorded)",
            "lines",
            lines=(partial["first_3_lines"][0],),
        ),
        Shape(
            "rt_rejected_json_body",
            "frame",
            "runtime",
            "tests/fixtures/seams/agentcore-runtime/eval_worker_invalid_job_payload.json",
            "lines",
            lines=(rejected["first_3_lines"][0],),
            at="json",
        ),
        # -- not_found (the runtime-gone / 404-after-create class) ----------------------------
        _recorded_client(
            "agentcore-runtime/invoke_unknown_runtime.json", "not_found", "runtime", "rt_not_found"
        ),
        _recorded_os(
            "opensearch-memory/index_not_found_404.json", "not_found", "os_index_not_found"
        ),
        _recorded_client(
            "ssm-getparameter/get_parameter_not_found.json",
            "not_found",
            "ssm",
            "ssm_parameter_not_found",
        ),
        Shape(
            "s3_no_such_key",
            "not_found",
            "s3",
            "botocore shape (S3 NoSuchKey 404)",
            "client",
            code="NoSuchKey",
            status=404,
            message="The specified key does not exist.",
        ),
        # -- access_denied (N16-obs87 class) -------------------------------------------------
        Shape(
            "rt_access_denied",
            "access_denied",
            "runtime",
            "synthesised from the recorded IAM sentence family "
            "(opensearch-memory/iam_identity_403_instance_role.json); the Runtime text was not "
            "captured on 2026-10-19",
            "client",
            code="AccessDeniedException",
            status=403,
            message=iam403["body"]["Message"].replace(
                "es:ESHttpGet", "bedrock-agentcore:InvokeAgentRuntime"
            ),
        ),
        _recorded_os(
            "opensearch-memory/iam_identity_403_instance_role.json", "access_denied", "os_iam_403"
        ),
        _recorded_os(
            "opensearch-memory/search_bad_query_400.json", "access_denied", "os_bad_query_400"
        ),
        Shape(
            "s3_access_denied",
            "access_denied",
            "s3",
            "botocore shape (S3 AccessDenied 403)",
            "client",
            code="AccessDenied",
            status=403,
            message="Access Denied",
        ),
        # -- conflict ----------------------------------------------------------------------
        Shape(
            "rt_conflict_modifying",
            "conflict",
            "runtime",
            "tests/fixtures/seams/console-api/agents_delete_seam-probe-unsupported.json",
            "client",
            code=conflict["body"]["detail"]["aws_error_code"],
            status=409,
            message=conflict["body"]["message"].removesuffix(" Wait and try again."),
        ),
        Shape(
            "rt_retryable_conflict",
            "conflict",
            "runtime",
            "api.md 'Errors' (RetryableConflictException on StopRuntimeSession)",
            "client",
            code="RetryableConflictException",
            status=409,
            message="The session is being modified by another operation.",
        ),
        # -- absent (eventually-consistent empty read / already gone) -------------------------
        Shape(
            "rt_already_gone",
            "absent",
            "runtime",
            "services/agentcore/runtime.delete_runtime_if_present → RUNTIME_ABSENT (N16-obs87)",
            "absent",
        ),
        Shape(
            "os_empty_read",
            "absent",
            "opensearch",
            "an OpenSearch read before the refresh — eventually consistent (memory-backend.md "
            "`refresh=False` writes)",
            "absent",
        ),
        # -- round 7 (r17-m): the embedding model and the S3 bulk-delete seams ---------------
        Shape(
            "bedrock_embed_validation",
            "not_found",
            "bedrock",
            "botocore shape (Bedrock InvokeModel ValidationException — the embedding model id "
            "unknown / not enabled in the region; not recorded on t1)",
            "client",
            code="ValidationException",
            status=400,
            message="The provided model identifier is invalid.",
        ),
        Shape(
            "bedrock_embed_throttle",
            "throttle",
            "bedrock",
            "tests/faults/conftest.py throttle shape on InvokeModel (the embedding call)",
            "client",
            code="ThrottlingException",
            status=429,
            message="Too many requests, please wait before trying again.",
        ),
        Shape(
            "bedrock_embed_5xx",
            "5xx",
            "bedrock",
            "tests/faults/conftest.py bedrock_5xx shape on InvokeModel (the embedding call)",
            "client",
            code="InternalServerException",
            status=500,
            message="Try your request again.",
        ),
        Shape(
            "s3_delete_partial",
            "access_denied",
            "s3",
            "S3 DeleteObjects partial-failure body (`Deleted[]` + `Errors[{Key, Code, Message}]` — "
            "a 200 whose body names the keys that stayed; not recorded on t1)",
            "partial",
        ),
        # -- interrupted (the restart class: tests/faults/test_restart_sweeps.py) -------------
        Shape(
            "process_death",
            "interrupted",
            "stage",
            "tests/faults/test_restart_sweeps.py — a worker killed mid-stage",
            "interrupt",
            message="worker process died mid-step (fault layer)",
        ),
        # -- region_unsupported (r17-ws: the workspace graph's region verdict) -----------------
        Shape(
            "ws_unknown_service",
            "region_unsupported",
            "stage",
            "botocore shape (UnknownServiceError — endpoint resolution fails before any connect: "
            "the installed botocore has no model / endpoint for the service in the region's "
            "partition; not recorded live: no CN region without ECR / CodeBuild was ever probed)",
            "unknown_service",
            message="ecr",
        ),
        # N17-obs200 (fixed cn/r17-w): the resolved-but-unreachable endpoint is a NETWORK verdict,
        # served with the transport class — retried once, then "unreachable from this host …
        # network/DNS, not the region", never the region sentence
        Shape(
            "ws_endpoint_unreachable",
            "timeout",
            "stage",
            "botocore shape (EndpointConnectionError — the endpoint resolved, the connect failed; "
            "a DNS / network outage on the hub's side, even to a supported region)",
            "endpoint",
            endpoint="https://api.ecr.cn-north-1.amazonaws.com.cn/",
        ),
        Shape(
            "ws_unrecognized_client",
            "region_unsupported",
            "stage",
            "botocore shape (UnrecognizedClientException 403 — the region's endpoint does not "
            "know the caller; workspace_bootstrap._REGION_UNSUPPORTED_CODES)",
            "client",
            code="UnrecognizedClientException",
            status=403,
            message="The security token included in the request is invalid.",
        ),
    ]
    out = {s.name: s for s in shapes}
    assert len(out) == len(shapes), "duplicate shape name"
    return out


SHAPES: dict[str, Shape] = _build_shapes()


def shapes(
    *, family: str | None = None, seam: str | None = None, kinds: tuple[str, ...] | None = None
) -> list[Shape]:
    out = [
        s
        for s in SHAPES.values()
        if (family is None or s.family == family)
        and (seam is None or s.seam == seam)
        and (kinds is None or s.kind in kinds)
    ]
    return sorted(out, key=lambda s: s.name)


def with_at(shape: Shape, at: str) -> Shape:
    return replace(shape, at=at)


# ---------------------------------------------------------------------------
# the outcome a world reports for a faulted step
# ---------------------------------------------------------------------------


@dataclass
class Outcome:
    """What ``World.fault`` observed: the node the entity is in now, the ledger tables / extra
    stores the contract's failure state allows to change (anything else is a ``HalfWrite``), the
    contract clause the answer was checked against (or ``None`` — a DOC gap) and a free-text
    observation for the report."""

    dst: str
    writes: frozenset[str] = frozenset()
    clause: str | None = None
    observation: str = ""
    seam_calls: int = 0


#: ledger tables a deploy-job failure may legitimately change (agents.md §1 / §4: the Job, the
#: Deployment and the Agent row's status / error)
JOB_TABLES = frozenset({"agents", "deployments", "jobs"})
#: a chat turn's own tables (assistant-turns.md §8: the session row, the messages, the event log)
TURN_TABLES = frozenset({"chat_sessions", "chat_messages", "chat_turn_events"})
#: an evaluation dispatch (evaluation-engine.md §22: the run row and its result records)
RUN_TABLES = frozenset({"eval_runs", "eval_results"})


# ---------------------------------------------------------------------------
# fault walks
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Step:
    edge: Edge
    shape: Shape | None
    dst: str  # the node the step leaves the entity in (edge.dst, or the world's fault_dst)

    @property
    def token(self) -> str:
        return f"{self.edge.id}@{self.shape.label}" if self.shape else self.edge.id


@dataclass(frozen=True)
class FaultWalk:
    graph: str
    seed: int
    family: str
    steps: tuple[Step, ...]
    requested: int
    slot_kinds: tuple[str, ...] = ()  # the kind of slot each served fault came from, in order

    @property
    def id(self) -> str:
        return f"{self.graph}/F{self.seed:04d}-{self.family}"

    @property
    def label(self) -> str:
        return ">".join(s.token for s in self.steps)

    @property
    def faults(self) -> list[Step]:
        return [s for s in self.steps if s.shape is not None]


def fault_seeds(family: str) -> list[int]:
    """Verify: seeds 0–3 per graph (the family is the seed's — see ``verify_params``); soak: the
    round's fresh window ``[(r-1)·20, r·20)`` per family."""
    if not soak():
        return list(range(VERIFY_SEEDS))
    r = loop_round()
    return list(range((r - 1) * SOAK_SEEDS_PER_FAMILY, r * SOAK_SEEDS_PER_FAMILY))


def verify_params() -> list[tuple[str, int]]:
    """The verify shape: 4 seeds per graph, seed ``i`` walking family ``FAMILIES[i]`` — four
    families per gate run, deterministic."""
    return [(FAMILIES[i % len(FAMILIES)], i) for i in range(VERIFY_SEEDS)]


def soak_params() -> list[tuple[str, int]]:
    return [(fam, seed) for fam in FAMILIES for seed in fault_seeds(fam)]


#: N17-obs287: the seeded fault walks are exploratory probes, collected but skipped in the gate
OFF_GATE = pytest.mark.skip(
    reason="seeded fault walk — LAUNCHPAD_FAULT_WALKS=1 (make graph-faults) or LAUNCHPAD_SOAK=1")


def fault_walks_enabled() -> bool:
    """Whether the seeded fault walks run: ``LAUNCHPAD_FAULT_WALKS=1`` (the fixed verify shape)
    or ``LAUNCHPAD_SOAK=1`` (every family, the round's fresh window)."""
    return soak() or os.environ.get("LAUNCHPAD_FAULT_WALKS") == "1"


def params() -> list[Any]:
    cases = soak_params() if soak() else verify_params()
    marks = () if fault_walks_enabled() else (OFF_GATE,)
    return [pytest.param(fam, seed, id=f"{fam}-{seed:04d}", marks=marks) for fam, seed in cases]


def _servable(choices: list[Edge], world_cls: type, family: str) -> list[Edge]:
    """The enabled edges a shape of ``family`` can be served at — transition edges first (a
    refusal's seam is never reached; the refusal axis covers those cells on their own)."""
    transitions = [e for e in choices if not e.is_refusal and world_cls.fault_shapes(e, family)]
    return transitions or [e for e in choices if world_cls.fault_shapes(e, family)]


def _route(
    graph: Graph,
    rng: random.Random,
    length: int,
    exclude: frozenset[str],
    world_cls: type,
    family: str,
    fault_at: dict[int, str],
    opportunities: list[tuple[int, bool]] | None = None,
) -> tuple[list[Step], list[str]]:
    """Walk ``length`` steps. ``fault_at`` maps a step index to the KIND of slot drawn there
    (``"transition"`` / ``"refusal"``): a transition slot is served only at a step whose node
    enables a servable transition edge (it carries forward until one appears), a refusal slot at
    the next step that can serve anything. Returns the steps and the kind of every fault served,
    in order."""
    node = graph.initial
    steps: list[Step] = []
    kinds: list[str] = []
    pending: list[str] = []
    for i in range(length):
        choices = sorted(
            (e for e in graph.out_edges(node) if e.id not in exclude), key=lambda e: e.id
        )
        if not choices:
            break
        servable = _servable(choices, world_cls, family)
        has_transition = bool(servable) and not servable[0].is_refusal
        if opportunities is not None and servable:
            opportunities.append((i, has_transition))
        if i in fault_at:
            pending.append(fault_at[i])
        edge = choices[rng.randrange(len(choices))]
        shape: Shape | None = None
        slot = None
        if pending and servable:
            if has_transition:
                slot = pending.pop(0)
            elif "refusal" in pending:
                pending.remove("refusal")
                slot = "refusal"
        if slot is not None:
            if edge not in servable:
                edge = servable[rng.randrange(len(servable))]
            options = world_cls.fault_shapes(edge, family)
            shape = options[rng.randrange(len(options))]
            kinds.append(slot)
        dst = world_cls.fault_dst(edge, shape) if shape is not None else edge.dst
        steps.append(Step(edge, shape, dst))
        node = dst
    return steps, kinds


def random_fault_walk(
    graph: Graph,
    seed: int,
    family: str,
    world_cls: type,
    *,
    exclude: frozenset[str] = frozenset(),
    min_len: int = MIN_LEN,
    max_len: int = MAX_LEN,
) -> FaultWalk:
    """Deterministic in ``(graph, seed, family, world_cls)``. The route is the uniform random
    walk of ``walks.random_walk`` (same draw: edges sorted by id, one ``randrange`` per step);
    1–3 of its steps carry a shape of ``family`` the world can serve at that edge
    (``world_cls.fault_shapes(edge, family)``) and the walk continues from
    ``world_cls.fault_dst(edge, shape)``. The fault slots are drawn among the steps of the
    UNFAULTED route at which a shape could be served — transition edges first (a refusal's
    seam is never reached): a slot drawn at a transition opportunity is served only at a
    transition seam (it carries forward until the diverged route reaches one), a slot drawn at a
    refusal-only opportunity at the next step that can serve anything. ``slot_kinds`` records
    the kind of every fault served, so a faulted refusal is always a refusal slot."""
    key = f"fault:{graph.name}:{family}:{seed}"
    rng = random.Random(key)
    length = rng.randint(min_len, max_len)
    wanted = rng.randint(MIN_FAULTS, MAX_FAULTS)
    opportunities: list[tuple[int, bool]] = []
    _route(
        graph, random.Random(key + ":route"), length, exclude, world_cls, family, {}, opportunities
    )
    slots_rng = random.Random(key + ":slots")
    transitions = [i for i, is_transition in opportunities if is_transition]
    refusals = [i for i, is_transition in opportunities if not is_transition]
    picked = {
        i: "transition" for i in slots_rng.sample(transitions, k=min(wanted, len(transitions)))
    }
    if len(picked) < wanted and refusals:
        for i in slots_rng.sample(refusals, k=min(wanted - len(picked), len(refusals))):
            picked[i] = "refusal"
    steps, kinds = _route(
        graph, random.Random(key + ":route"), length, exclude, world_cls, family, picked
    )
    return FaultWalk(graph.name, seed, family, tuple(steps), length, tuple(kinds))


# ---------------------------------------------------------------------------
# running + shrinking (mirrors walks.py over Steps)
# ---------------------------------------------------------------------------


def run_steps(world: Any, steps: tuple[Step, ...]) -> Failure | None:
    try:
        for i, step in enumerate(steps):
            try:
                if step.shape is None:
                    world.step(step.edge)
                else:
                    world.fault_step(step.edge, step.shape, expected_dst=step.dst)
            except Exception as exc:  # noqa: BLE001 — every oracle failure is a finding
                return Failure(i, step.token, exc)
        return None
    finally:
        world.close()


def _nodes(steps: tuple[Step, ...]) -> list[str]:
    return [steps[0].edge.src, *(s.dst for s in steps)] if steps else []


def _cycles(steps: tuple[Step, ...]) -> list[tuple[int, int]]:
    nodes = _nodes(steps)
    out = [
        (i, j)
        for i in range(len(steps))
        for j in range(i + 1, len(steps) + 1)
        if nodes[i] == nodes[j]
    ]
    return sorted(out, key=lambda ij: (-(ij[1] - ij[0]), ij[0]))


def shrink(
    make_world: Callable[[], Any],
    steps: tuple[Step, ...],
    failure: Failure,
    *,
    budget: int = SHRINK_BUDGET,
) -> tuple[tuple[Step, ...], Failure, int]:
    """Cut after the failing step, then remove cycles (over the PLANNED nodes, faults included)
    while the same step keeps failing with the same exception type — fresh world per attempt.
    A faulted step is never removed unless it sits inside a removed cycle."""
    current = steps[: failure.index + 1]
    current_failure = failure
    replays = 0
    progress = True
    while progress and replays < budget:
        progress = False
        for i, j in _cycles(current):
            if j >= len(current):
                continue
            candidate = (*current[:i], *current[j:])
            if replays >= budget:
                break
            replays += 1
            got = run_steps(make_world(), candidate)
            if got is not None and got.same_as(current_failure) and got.index == len(candidate) - 1:
                current, current_failure, progress = candidate, got, True
                break
    return current, current_failure, replays


def finding_text(walk: FaultWalk, minimal: tuple[Step, ...], failure: Failure, replays: int) -> str:
    label = ">".join(s.token for s in minimal)
    tb = "".join(traceback.format_exception(failure.exc)).strip().splitlines()[-12:]
    return (
        f"fault walk {walk.id} ({len(walk.steps)} steps, {len(walk.faults)} faults) failed at "
        f"step {failure.index + 1} ({failure.edge_id})\n"
        f"minimal step sequence ({len(minimal)} steps, {replays} shrink replays):\n  {label}\n"
        f"repro-hash: {repro_hash(label)}\n"
        f"{failure.head}\n" + "\n".join("  " + line for line in tb)
    )


#: ``repro-hash`` of a shrunk failing step sequence → the register id + reason (a known
#: finding); the dedicated strict pin lives in ``test_graph_fault_pins.py``.
KNOWN: dict[str, str] = {}

#: Every register row the fault walks opened — ``(register id, graph, minimal step sequence /
#: probe)``; the loop-engine table counts them per graph (``loop_report.py`` →
#: `graph_fault_findings`). Round 6 (r17-l, 2026-10-20).
FINDINGS: tuple[tuple[str, str, str], ...] = (
    (
        "N17-obs90",
        "memory_record",
        "add>supersede@os_503_no_master/update_doc — the extractor's supersede writes the "
        "successor "
        "(put_doc) before it retires the target (update_doc): a fault between the two leaves both "
        "ACTIVE, the successor naming a target that carries no superseded_by",
    ),
    (
        "N17-obs91",
        "memory_record",
        "add>supersede, then DELETE the chain HEAD with delete_doc faulted — the "
        "unlink-before-delete "
        "order revives the predecessor (superseded_by cleared) while the head stays",
    ),
    (
        "N17-obs92",
        "memory_record",
        "api_delete_again@os_bad_query_400/get_doc — routers/memory._guard's 502 "
        "memory.unavailable "
        "message embeds str(exc): the raw OpenSearch body (root_cause / the IAM sentence with the "
        "role ARN) reaches the console",
    ),
    (
        "N17-obs93",
        "reference",
        "agent_delete_releases_kb>memory_delete_after_agent_gone@s3_slow_down/list_objects_v2 — "
        "the "
        "memory store's ledger row is deleted, then the unguarded S3 purge raises: 502 to the "
        "member, "
        "the OpenSearch purge never runs",
    ),
    (
        "N17-obs94",
        "chat_session",
        "first_turn>turn_completes>v1_turn@rt_read_timeout (also agent.delete_active, "
        "chat_session.stop_open) — a botocore transport exception (ReadTimeoutError) has no "
        "envelope "
        "mapping on any route: a bare 500",
    ),
    (
        "N17-obs95",
        "chat_session",
        "first_turn>turn_completes>v1_turn@rt_frame_no_workload_token — the /v1 sync invoke whose "
        "stream ends on the SDK terminal error frame escapes as SdkStreamError: a bare 500 (Chat "
        "renders the same frame as an `error` event)",
    ),
    (
        "N17-obs96",
        "chat_session",
        "first_turn@rt_partial_frame_only — a truncated SSE data line that is the whole response "
        "is "
        "rendered VERBATIM as the assistant's reply, terminal `done`",
    ),
    (
        "N17-obs97",
        "chat_session",
        "first_turn@rt_rejected_json_body/json — a structured refusal body (state REJECTED, code, "
        "message) is rendered as the assistant's reply, terminal `done`",
    ),
    (
        "N17-obs99",
        "reference",
        "kb_force_delete_strips@os_iam_403/delete_index (every storage shape) — the KB force "
        "delete's `cleanup_warnings` carry the raw seam text (the OpenSearch body with the IAM "
        "sentence / root_cause, botocore's framing); WB-008 names the warnings, not what they may "
        "carry (DOC)",
    ),
    (
        "N17-obs100",
        "agent",
        "create>deploy_succeeds@<stage fault>/provision>redeploy_from_failed>"
        "cold_redeploy_succeeds>"
        "redeploy>redeploy_succeeds@rt_conflict_modifying/deploy>redeploy_from_failed>"
        "cold_redeploy_fails — after a failure INSIDE `deploy` (agent `failed`, §4.3: the "
        "runtime's "
        "state is unknown) a re-publish that fails before `deploy` flips the agent back to "
        "`active` "
        "(`_keeps_live_version` sees the older succeeded deployment); §4.2 says `failed`",
    ),
    (
        "N17-obs101",
        "memory_record",
        "add>add_duplicate@os_empty_read/knn_search — an eventually-consistent (empty) candidate "
        "read "
        "lets the extractor add the near-duplicate: two ACTIVE records at cosine 0.95 ≥ 0.92 in "
        "one "
        "namespace (memory-backend.md 'no two ACTIVE records at cosine ≥ the dedupe threshold'); "
        "two concurrent extractions for one actor are the live shape",
    ),
    (
        "N17-obs118",
        "reference",
        "registry_skill_delete@s3_access_denied/delete_object (probe) — the skill sweep reports "
        "objects_deleted: true while the per-key delete was refused (_delete_keys swallows)",
    ),
    (
        "N17-obs119",
        "reference",
        "memory_delete_after_agent_gone@s3_delete_partial/delete_objects (probe) — the purge never "
        "reads a DeleteObjects 200's Errors[]; refused keys stay, the delete answers complete",
    ),
    (
        "N17-obs120",
        "reference",
        "kb_ingest@bedrock_embed_throttle/embed (probe) — a transient embedding fault on one chunk "
        "fails the document permanently (one attempt); the ingest never consults the shared "
        "transient predicate",
    ),
    (
        "N17-obs121",
        "runtime_canary",
        "cleanup_*@<Runtime shape>/endpoint — a refused endpoint delete leaves the row `cleaned` "
        "with a cleanup_blockers entry; agents.md: `cleaned` never blocks the agent delete (DOC)",
    ),
    (
        "N17-obs122",
        "memory_record",
        "add>supersede@os_503_no_master/update_doc>redelivery (probe) — the SQS redelivery after "
        "the half-written supersede targets the successor (self-supersede → noop): the original "
        "stays active for good",
    ),
    (
        "N17-obs123",
        "eval_run",
        "create>dispatch_completes@s3_slow_down/store_status — one transient S3 fault on the "
        "poller's status.json / results.jsonl read fails the run while the worker job completes; "
        "only a manual recheck re-attaches it",
    ),
    (
        "N17-obs98",
        "chat_session",
        "first_turn@runtime_bedrock_5xx_frame — the chat `error` event for the SDK frame carries "
        "{message} only, no detail.error_type (DOC: §8 parity names no exception to §4)",
    ),
    # r17-ws (the workspace graph's fault world, 2026-10-20)
    (
        "N17-obs132",
        "workspace",
        "register>run_validate@rt_read_timeout/ecr.describe_repositories — a transient botocore "
        "ReadTimeoutError at a validate-access probe is reported as 'region not supported; "
        "register the workspace in a region where AgentCore and its dependencies are available' "
        "(_probe maps EVERY BotoCoreError to the missing-endpoint verdict)",
    ),
)

#: A register row keyed on the SYMPTOM (a substring of the oracle's message) rather than the
#: shrunk label: the shrinker's label varies with the random prefix while the failing step and
#: its oracle message are the finding — the walk xfails with the id, the dedicated strict pin
#: (``test_graph_fault_pins.py``) drives the minimal sequence and flips when the fix lands.
#: A register row keyed on the SYMPTOM — ``(edge prefix, substring of the oracle's message)`` →
#: register id. The edge prefix anchors the symptom to the step that produced it (rev-r17 nit B
#: on round 6: a bare substring such as "('active', 'failed')" would pin a DIFFERENT edge's
#: failure to an existing row); a different edge producing the same text is a NEW finding.
#: The walk's failing step token is ``<edge>@<shape>``; the prefix matches the edge id part.
KNOWN_SYMPTOMS: dict[tuple[str, str], str] = {
    # empty since cn/r17-v flipped N17-obs132 (the three validate-access entries); a new symptom
    # row is added here together with its register row and its strict pin
}


def known_symptom(exc: BaseException, token: str = "") -> str | None:
    """The register id a failure maps to: the oracle message carries the marker AND the failing
    step's edge (``token`` = ``<edge>@<shape>`` or a bare edge id) starts with the row's edge
    prefix. An empty prefix is route-independent by design (obs94: one missing handler)."""
    text = str(exc)
    edge = token.split("@", 1)[0]
    for (prefix, marker), rid in KNOWN_SYMPTOMS.items():
        if marker in text and edge.startswith(prefix):
            return rid
    return None


REPORT_ENV = "LAUNCHPAD_FAULT_REPORT"


def report(walk: FaultWalk, world: Any, failure: Failure | None, label: str | None = None) -> None:
    """``LAUNCHPAD_FAULT_REPORT=<path.jsonl>``: one line per walk — the steps, the faults served,
    the world's observations (the no-sentence shapes: what the member actually saw) and the
    failure, if any. What the round's DOC rows are written from."""
    path = os.environ.get(REPORT_ENV)
    if not path:
        return
    line = {
        "walk": walk.id,
        "family": walk.family,
        "steps": len(walk.steps),
        "faults": [s.token for s in walk.faults],
        "observations": list(getattr(world, "observations", []) or []),
        "failure": None
        if failure is None
        else {
            "step": failure.index + 1,
            "token": failure.edge_id,
            "head": failure.head,
            "minimal": label,
            "repro_hash": repro_hash(label or ""),
        },
    }
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(line, ensure_ascii=False) + "\n")


def run_fault_walk(
    make_world: Callable[[], Any], world_cls: type, graph: Graph, family: str, seed: int
) -> FaultWalk:
    walk = random_fault_walk(graph, seed, family, world_cls, exclude=frozenset(world_cls.XFAIL))
    assert walk.steps, (graph.name, seed, "the initial node has no enabled edge")
    if not walk.faults:
        pytest.skip(
            f"{walk.id}: no edge of the walk can serve a {family!r} shape "
            f"(the family has no seam on this graph)"
        )
    world = make_world()
    failure = run_steps(world, walk.steps)
    if failure is None:
        coverage.record_path(graph.name, walk.id)
        report(walk, world, None)
        return walk
    minimal, final, replays = shrink(make_world, walk.steps, failure)
    label = ">".join(s.token for s in minimal)
    report(walk, world, final, label)
    text = finding_text(walk, minimal, final, replays)
    known = KNOWN.get(repro_hash(label)) or known_symptom(final.exc, final.edge_id)
    if known:
        pytest.xfail(f"{known} — {label}\n{final.head}")
    raise AssertionError(text) from final.exc
