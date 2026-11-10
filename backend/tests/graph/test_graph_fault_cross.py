# ruff: noqa: F811  — pytest fixtures are imported by name from the cross-graph module
"""Cross-graph walks with the shared entity's seam faulted (round 6, r17-l; testing.md §6.9):
`agent × chat_session` where the injected delete of the shared agent FIRST meets a recorded
Runtime shape (AccessDenied / Conflict / Throttling / the documented 5xx / a transport timeout)
and is refused — the chat side keeps walking on the agent that stayed — and lands clean on the
retry. The oracle is the agent graph's delete contract (api.md AWS_ERROR_MAP, N16-obs87), the
cross module's dangling-reference scan after every step and the digest on the refused delete."""

from __future__ import annotations

import os
from typing import Any

import pytest
from botocore.exceptions import ClientError

import app.routers.agents as agents_router
from app.core.errors import aws_error_code
from tests.graph import faults
from tests.graph.crossgraph import cross_seeds, loop_round
from tests.graph.executor import ledger_digest
from tests.graph.fault_seams import _assert_envelope_detail, _mapped, documented_unhandled
from tests.graph.test_graph_crossgraph import (  # noqa: F401 — fixtures by name
    AGENT_LIVE_NODES,
    AgentChat,
    _clients,
    _run,
    agent_hooks,
    chat_producer,
)
from tests.test_visibility import ada, admin, app, bob  # noqa: F401 — fixtures by name

SOAK = os.environ.get("LAUNCHPAD_SOAK") == "1"
#: the delete-seam shapes a cross walk draws from (one per family that has a Runtime client shape)
DELETE_SHAPES = tuple(
    s for fam in faults.FAMILIES
    for s in faults.shapes(family=fam, seam="runtime", kinds=("client", "transport"))[:1]
)


class AgentChatFaulted(AgentChat):
    """``AgentChat`` whose injected delete meets a shape on `DeleteAgentRuntime` first."""

    pairing = "agent_x_chat"

    def __init__(self, clients, seed, hooks, app, producer, plane, shape: faults.Shape) -> None:
        super().__init__(clients, seed, hooks, app, producer, plane)
        self.shape = shape
        self.faulted_once = False

    def inject(self) -> bool:
        if self.node_a not in AGENT_LIVE_NODES or self.a.agent_id != self.shared_agent:
            return False
        if self.faulted_once:
            return super().inject()  # the retry: the clean delete the plan asked for
        self.faulted_once = True
        shape = self.shape
        calls: list[str] = []

        def delete_agent_resources(agent, ws):
            calls.append("DeleteAgentRuntime")
            raise shape.exception("DeleteAgentRuntime")

        real = agents_router.zip_method.delete_agent_resources
        agents_router.zip_method.delete_agent_resources = delete_agent_resources
        try:
            before = ledger_digest()
            try:
                res = self.ada.delete(f"/api/agents/{self.shared_agent}")
            except ClientError as exc:
                if not documented_unhandled(exc):
                    raise
                res = None
                self.note(f"inject: faulted delete ({shape.label}) → documented unhandled 500 "
                          f"({aws_error_code(exc)})")
            except Exception as exc:  # noqa: BLE001 — the fault escaped: the finding's shape
                known = faults.known_symptom(faults.EnvelopeViolation(
                    f"escaped the error handlers as {type(exc).__name__}"), "delete_active")
                if known:
                    pytest.xfail(f"{known} — cross {self.plan.id}: faulted delete ({shape.label}) "
                                 f"escaped as {type(exc).__name__}")
                raise
            if res is not None:
                # api.md "Errors" for a ClientError code; "Transport failures" (N17-obs94) for
                # the read timeout — 504 aws.timeout {error_type}, the row kept
                mapped = _mapped(shape)
                assert mapped is not None, (shape.label, res.status_code, res.text[:300])
                status, code = mapped
                self.expect(res, status, code, what=f"faulted delete ({shape.label})")
                _assert_envelope_detail(res.json(), shape, operation="DeleteAgentRuntime",
                                        public=False, what=f"faulted delete ({shape.label})")
                self.note(f"inject: faulted delete ({shape.label}) → {status} {code}")
            after = ledger_digest()
            changed = sorted(k for k in after if before.get(k) != after[k])
            assert not changed, f"the refused delete changed ledger table(s): {changed}"
            assert calls == ["DeleteAgentRuntime"], calls
        finally:
            agents_router.zip_method.delete_agent_resources = real
        assert self.agent_status(self.shared_agent) in ("active", "failed")
        return False  # not injected yet: the plan retries at the next step


def _seeds() -> list[int]:
    return cross_seeds(soak=SOAK)


def _params() -> list[Any]:
    out = []
    for seed in _seeds():
        shape = DELETE_SHAPES[(seed + loop_round()) % len(DELETE_SHAPES)]
        # N17-obs287: a seeded cross fault walk is off the gate like the per-graph fault walks
        marks = () if faults.fault_walks_enabled() else (faults.OFF_GATE,)
        out.append(pytest.param(seed, shape, id=f"X{seed:04d}-{shape.label}", marks=marks))
    return out


@pytest.mark.parametrize(("seed", "shape"), _params())
def test_agent_x_chat_with_the_delete_seam_faulted(app, admin, ada, bob, agent_hooks,
                                                   chat_producer, seed, shape):
    clients, _ = _clients(admin, ada, bob)
    producer, plane = chat_producer
    _run(AgentChatFaulted(clients, seed, agent_hooks, app, producer, plane, shape))
