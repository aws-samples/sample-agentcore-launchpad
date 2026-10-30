# ruff: noqa: F811  — pytest fixtures are imported by name from tests.test_visibility
"""E13 — Runtime canary: setup → traffic → verdict → advance → complete / rollback →
cleanup × caller (creator / other member / admin) × conversation CLEAR × restart, as a
model (round 16 channel A, Hypothesis ``RuleBasedStateMachine``). Hermetic: the actions
run inline (``canary_service._spawn``), the AWS-touching actions (setup, traffic's replay
job, verdict's evidence wait, rollback's mint) are ledger emulations that write exactly
the artifacts the real ones write, while ``advance``, ``complete`` and ``cleanup`` are the
REAL functions with the control plane / S3 seams stubbed.

Invariants and the contract clause each checks (never a threshold copied from code):

* **§8 403 before any side effect** — users-and-ownership.md §8: `setup, traffic, advance,
  complete, rollback, cleanup` are creator-or-admin, `verdict` is not; "anyone else
  answers 403 ``canary.forbidden`` … before any side effect"; "The gate runs after the
  kind's visibility lookup … and before any stage / status / in-use check (a forbidden
  caller learns nothing about the row's state)".
* **Legal transitions only** — evaluation-engine.md §canary / §routing: ramp stages
  ``RAMP_WEIGHTS`` 90/10 → 50/50 → 1/99; "``complete`` is the terminal policy … at the
  final 1/99 stage"; rollback "is the safety valve — allowed for ANY running canary";
  "``STOPPED`` (promote / complete / rollback / cleanup) ends routing"; the verdict gate
  (N-SCN-obs14 / obs15): ``insufficient-data`` blocked; a significant ``control-wins``
  terminal; a non-significant ``control-wins`` / ``tie`` / ``insufficient-n`` advance only
  with ``allow_non_significant``; ``complete`` refuses ``insufficient-n`` even with the
  override and ANY ``control-wins``; a verdict needs traffic at the current weights, an
  advance / complete a verdict at the current weights.
* **CLEAR owns the canary** — assistant-turns.md §10: "While a job is live … the Agents'
  canary action … routes 409 ``assistant.agent_clearing``"; the purge step
  ``canary:<id>`` runs ``act_cleanup`` and verifies by ``remaining_resources`` (ledger
  online configs + endpoints); "a running canary action blocks the clear before anything
  is deleted" (409 ``assistant.conversation_busy``).
* **Restart** — testing.md §1.3: "a Runtime canary loses its ``running_action`` flag and
  carries ``… interrupted by a backend restart — retry the action``"; N15-obs10: a canary
  under a live clear is skipped by that sweep.
* **Ledger after cleanup** — evaluation-engine.md: cleanup deletes "the per-arm online-eval
  configs, the session assignments" and stops the A/B row, so ``online.config_ids_for_
  owner`` is empty and no ``experiment_assignments`` row names the canary.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock

import pytest
from hypothesis import HealthCheck, note, settings
from hypothesis import strategies as st
from hypothesis.stateful import (
    RuleBasedStateMachine,
    invariant,
    precondition,
    rule,
    run_state_machine_as_test,
)

from app.assistant import purge
from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.core.errors import AppError
from app.deployer import teardown
from app.evaluation import online
from app.evaluation.models import OnlineEvalConfig
from app.main import create_app
from app.models.assistant import AssistantConversation, AssistantMessage, AssistantProposal
from app.models.ledger import Agent, Job
from app.optimization import canary_infra, canary_service, routing
from app.optimization.models import ExperimentAssignment, RuntimeCanary
from app.routers import agents as agents_router
from tests.conftest import ws_ctx
from tests.model.worlds import reset_canary_world
from tests.property.ledger_invariants import scan_ledger
from tests.test_visibility import (  # noqa: F401 — fixtures by name
    _active_agent,
    _dataset,
    ada,
    admin,
    app,
    bob,
)

SOAK = os.environ.get("LAUNCHPAD_SOAK") == "1"
SCALE = 10 if SOAK else 1
WS = DEFAULT_WORKSPACE_ID
MUTATING = ("setup", "traffic", "advance", "complete", "rollback", "cleanup")
ACTIONS = (*MUTATING, "verdict")
FINAL = len(canary_service.RAMP_WEIGHTS) - 1

VERDICTS: tuple[dict[str, Any], ...] = (
    {"verdict": "treatment-wins", "significant": True, "arms_n": {"control": 12, "treatment": 12}},
    {"verdict": "treatment-wins", "significant": False, "arms_n": {"control": 12, "treatment": 12}},
    {"verdict": "tie", "significant": False, "arms_n": {"control": 12, "treatment": 12}},
    {"verdict": "control-wins", "significant": True, "arms_n": {"control": 12, "treatment": 12}},
    {"verdict": "control-wins", "significant": False, "arms_n": {"control": 12, "treatment": 12}},
    {"verdict": "control-wins"},  # an older payload without the flag: stays terminal
    {"verdict": "insufficient-n", "arms_n": {"control": 32, "treatment": 4}, "min_arm_n": 10},
    {"verdict": "insufficient-data", "reason": "no new evaluator samples"},
)


def _verdict_gate(verdict: dict[str, Any] | None, *, override: bool, terminal: bool) -> str | None:
    """The ramp policy as the contract states it (evaluation-engine.md, N-SCN-obs14 / obs15):
    the error code the action must answer, or ``None`` when it may proceed."""
    if verdict is None:
        return "canary.stage_not_ready"
    outcome, significant = verdict.get("verdict"), verdict.get("significant")
    if outcome == "insufficient-data":
        return "canary.verdict_blocked"  # "insufficient-data stays blocked for both actions"
    if outcome == "control-wins" and (terminal or significant is not False):
        return "canary.verdict_blocked"  # terminal; "complete is refused on ANY control-wins"
    if terminal and outcome == "insufficient-n":
        return "canary.verdict_blocked"  # "blocked there even with the override"
    weak = outcome in ("tie", "insufficient-n") or significant is False
    if outcome != "treatment-wins" and not weak:
        return "canary.verdict_blocked"
    if weak and not override:
        return "canary.override_required"  # "requires explicit operator override"
    return None


# ---------------------------------------------------------------------------
# ledger helpers
# ---------------------------------------------------------------------------


def _get(canary_id: str) -> RuntimeCanary:
    db = SessionLocal()
    try:
        row = db.get(RuntimeCanary, canary_id)
        db.expunge(row)
        return row
    finally:
        db.close()


def _agent(agent_id: str) -> Agent:
    db = SessionLocal()
    try:
        row = db.get(Agent, agent_id)
        db.expunge(row)
        return row
    finally:
        db.close()


def _online_ids(canary_id: str) -> list[str]:
    return online.config_ids_for_owner(ws_ctx(id=WS), canary_id)


def _assignments(canary_id: str) -> int:
    db = SessionLocal()
    try:
        return db.query(ExperimentAssignment).filter_by(owner_id=canary_id).count()
    finally:
        db.close()


def _snapshot(canary_id: str) -> tuple:
    row = _get(canary_id)
    return (row.status, row.stage, row.running_action, row.artifacts)


def _new_canary(agent: Agent, created_by: str | None) -> str:
    db = SessionLocal()
    try:
        row = RuntimeCanary(
            workspace_id=WS, name=f"CANARY-{agent.name}", champion_agent_id=agent.id,
            champion_agent_name=agent.name, challenger_agent_id=agent.id,
            challenger_agent_name=agent.name, status="running", stage="setup",
            created_by=created_by, created_by_name="ada" if created_by else None,
            artifacts={
                "agent_meta": {"id": agent.id, "arn": agent.arn, "resource_id": agent.resource_id,
                               "runtime_name": agent.name},
                "edited_spec": {**agent.spec, "system_prompt": "candidate prompt"},
                "rounds": [],
            },
        )
        db.add(row)
        db.commit()
        return row.id
    finally:
        db.close()


# ---------------------------------------------------------------------------
# the action emulations (ledger-only twins of the AWS-touching actions)
# ---------------------------------------------------------------------------


class Seams:
    """What the real actions would do to AWS, recorded; the ledger writes are the real
    functions' (``_update``, ``_record_traffic_attempt``, ``_stop_ab_test``,
    ``online.create_for_owner`` / ``delete_for_owner``, ``routing.delete_assignments``)."""

    def __init__(self) -> None:
        self.deleted_endpoints: list[str] = []
        self.verdict_queue: list[dict[str, Any]] = []
        self.calls: list[str] = []

    def act_setup(self, canary_id: str, progress) -> dict[str, Any]:
        self.calls.append("setup")
        row = canary_service._get(canary_id)
        meta = row.artifacts["agent_meta"]
        db = SessionLocal()
        try:
            agent = db.get(Agent, meta["id"])
            if agent is None or agent.status == "deleted":
                raise RuntimeError("the agent behind this canary no longer exists")
            agent_version = agent.version  # N17-obs16: the base the candidate is built on
            ws = ws_ctx(id=WS)
            control = online.create_for_owner(
                ws, agent, name=f"can_{canary_id[:8]}_oec", evaluators=None,
                log_group="/lg/stable", service_name="svc-stable", sampling=100.0, timeout=2)
            treat = online.create_for_owner(
                ws, agent, name=f"can_{canary_id[:8]}_oet", evaluators=None,
                log_group="/lg/treat", service_name="svc-treat", sampling=100.0, timeout=2)
        finally:
            db.close()
        stable, treatment = f"stable{canary_id[:6]}", f"treat{canary_id[:6]}"
        cw, tw = canary_service.RAMP_WEIGHTS[0]
        variants = routing.target_variants(stable, treatment, cw, tw)
        setup = {
            "runtime_id": meta["resource_id"], "stable_endpoint": stable, "v_current": "1",
            "agent_version": agent_version,
            "gateway_id": canary_service.ROUTING_MODE, "test_name": f"can_{canary_id[:8]}",
            "ab_test_id": f"local_{canary_id}",
            "champion": {"target_name": stable, "online_eval_id": control.config_id},
            "challenger": {"target_name": treatment, "online_eval_id": treat.config_id},
            "ramp_stage": 0, "weights": routing.weights_of(variants), "v_candidate": "2",
            "candidate_s3_key": f"agents/{meta['runtime_name']}/canary/{canary_id}-candidate.zip",
            "treatment_endpoint": treatment, "policy_narrowing": None,
        }
        abtest = {"ab_test_id": f"local_{canary_id}", "variants": variants,
                  "execution_status": routing.RUNNING, "created_at": canary_service._now()}
        canary_service._update(canary_id, stage="setup",
                               artifact={"setup": setup, "abtest": abtest, "rounds": []})
        return setup

    def act_traffic(self, canary_id, prompts, dataset_info, progress, scenario_ids=None,
                    sessions_per_arm=None) -> dict[str, Any]:
        self.calls.append("traffic")
        row = canary_service._get(canary_id)
        db = SessionLocal()
        try:  # the pre-assigned sessions the replay job would have routed
            for i, _prompt in enumerate(prompts):
                db.add(ExperimentAssignment(workspace_id=WS, owner_kind="canary",
                                            owner_id=canary_id,
                                            session_id=f"s{os.urandom(6).hex()}-{i}",
                                            arm="C" if i % 2 else "T1"))
            db.commit()
        finally:
            db.close()
        return canary_service._record_traffic_attempt(
            canary_id, row, {"sent": len(prompts), "failed": 0}, dataset_info, baseline_n=0)

    def act_verdict(self, canary_id: str, progress) -> dict[str, Any]:
        self.calls.append("verdict")
        row = canary_service._get(canary_id)
        rounds, current = canary_service._current_round(row)
        if current is None or not current.get("traffic_attempts"):
            raise RuntimeError("current ramp stage has no traffic attempt")
        verdict = self.verdict_queue.pop(0) if self.verdict_queue else dict(VERDICTS[0])
        stored = {**verdict, "metrics": [], "baseline_n": 0, "recorded_at": canary_service._now()}
        current["verdict"] = stored
        canary_service._update(canary_id, stage="verdict", artifact={"rounds": rounds})
        return stored

    def act_rollback(self, canary_id: str, progress) -> dict[str, Any]:
        self.calls.append("rollback")
        row = canary_service._get(canary_id)
        stopped: dict[str, Any] = {}
        if (row.artifacts.get("setup") or {}).get("ab_test_id"):
            stopped = canary_service._stop_ab_test(canary_id, progress)
        result = {"winner": "champion", "restored_version": "3",
                  "ab_test_status": stopped.get("executionStatus"),
                  "rolled_back_at": canary_service._now()}
        canary_service._update(canary_id, status="rolled_back", stage="rollback",
                               artifact={"rollback": result})
        return result


@pytest.fixture
def seams(monkeypatch, tmp_path) -> Seams:
    s = Seams()
    monkeypatch.setattr(canary_service, "_spawn", lambda target: target())
    monkeypatch.setattr(canary_service, "control_client", lambda ws=None: MagicMock())
    monkeypatch.setattr(canary_service, "act_setup", s.act_setup)
    monkeypatch.setattr(canary_service, "act_traffic", s.act_traffic)
    monkeypatch.setattr(canary_service, "act_verdict", s.act_verdict)
    monkeypatch.setattr(canary_service, "act_rollback", s.act_rollback)
    # the real cleanup, its AWS seams recorded
    monkeypatch.setattr(canary_infra, "delete_endpoint_quiet",
                        lambda control, **kw: s.deleted_endpoints.append(kw["endpoint_name"]))
    monkeypatch.setattr(canary_infra, "wait_endpoints_gone", lambda control, **kw: [])
    monkeypatch.setattr(canary_service, "_cleanup_candidate_objects",
                        lambda row, control, workspace, progress: [])
    # the conversation CLEAR's AWS seams (assistant.purge)
    monkeypatch.setattr(purge, "LOCK_DIR", tmp_path / "locks")
    monkeypatch.setattr(purge, "_sleep", lambda _s: None)
    monkeypatch.setattr(canary_service, "remaining_resources", lambda row, ws: [
        {"category": f"online-eval:{c}", "status": "ACTIVE"} for c in _online_ids(row.id)])
    monkeypatch.setattr(agents_router, "delete_agent_cloud_resource", lambda agent, ws: True)
    monkeypatch.setattr(purge, "agent_remaining", lambda agent, ws: [])
    monkeypatch.setattr(purge, "foreign_endpoints", lambda agent, ws: [])
    monkeypatch.setattr(purge, "agent_residue", lambda agent, ws, log: [
        teardown.TeardownStep(teardown.STEP_ROLE, teardown.DELETED, "role")])
    monkeypatch.setattr(canary_infra, "delete_object_quiet", lambda key, ws, **kw: None)
    return s


# ---------------------------------------------------------------------------
# the machine
# ---------------------------------------------------------------------------


class CanaryMachine(RuleBasedStateMachine):
    def __init__(self, clients: dict[str, Any], ada_id: str, seams: Seams, dataset_id: str):
        super().__init__()
        reset_canary_world()
        seams.deleted_endpoints.clear()
        seams.calls.clear()
        self.clients = clients
        self.seams = seams
        self.dataset_id = dataset_id
        self.agent_id = _active_agent(f"canary-{os.urandom(3).hex()}", created_by=ada_id)
        self.agent = _agent(self.agent_id)
        self.canary_id = _new_canary(self.agent, ada_id)
        # the model
        self.status = "running"
        self.setup_done = False
        self.ramp_stage = 0
        self.traffic_at_stage = False
        self.verdict: dict[str, Any] | None = None
        self.stuck: str | None = None
        self.conversation_id: str | None = None
        self.clear_job: str | None = None
        self.cleared = False

    # -- expected answers -----------------------------------------------------
    def expected(self, caller: str, action: str, override: bool, with_dataset: bool) -> str | None:
        if caller == "bob" and action != "verdict":
            return "canary.forbidden"
        if self.clear_job is not None and not self.cleared:
            return "assistant.agent_clearing"
        if self.stuck:
            return "canary.action_in_flight"
        if action == "cleanup":
            # N17-obs452: a running canary past setup (the mint rolled DEFAULT to the
            # candidate) is completed or rolled back before it may be cleaned up
            if self.status == "running" and self.setup_done:
                return "canary.cleanup_requires_decision"
            return None
        if self.status != "running":
            return "canary.stage_not_ready"
        if action == "setup":
            return "canary.stage_not_ready" if self.setup_done else None
        if action == "rollback":
            return None
        if not self.setup_done:
            return "canary.stage_not_ready"
        if action == "traffic":
            return None if with_dataset else "canary.dataset_required"
        if action == "verdict":
            return None if self.traffic_at_stage else "canary.stage_not_ready"
        if self.verdict is None:
            return "canary.stage_not_ready"
        if action == "advance" and self.ramp_stage >= FINAL:
            return "canary.stage_not_ready"
        if action == "complete" and self.ramp_stage != FINAL:
            return "canary.stage_not_ready"
        return _verdict_gate(self.verdict, override=override, terminal=action == "complete")

    def apply(self, action: str) -> None:
        if action == "setup":
            self.setup_done = True
        elif action == "traffic":
            self.traffic_at_stage = True
            self.verdict = None
        elif action == "verdict":
            pass  # the stub records the drawn verdict
        elif action == "advance":
            self.ramp_stage += 1
            self.traffic_at_stage = False
            self.verdict = None
        elif action == "complete":
            self.status = "completed"
        elif action == "rollback":
            self.status = "rolled_back"
        elif action == "cleanup":
            self.status = "cleaned"

    # -- rules ----------------------------------------------------------------
    @precondition(lambda self: not self.cleared)
    @rule(caller=st.sampled_from(["ada", "ada", "bob", "admin"]),
          action=st.sampled_from(ACTIONS), override=st.booleans(),
          with_dataset=st.sampled_from([True, True, False]),
          verdict=st.sampled_from(VERDICTS))
    def act(self, caller, action, override, with_dataset, verdict):
        body: dict[str, Any] = {"action": action, "allow_non_significant": override}
        if action == "traffic" and with_dataset:
            body["dataset_id"] = self.dataset_id
        if action == "verdict":
            self.seams.verdict_queue = [dict(verdict)]
        before = _snapshot(self.canary_id)
        online_before = _online_ids(self.canary_id)
        calls_before = len(self.seams.calls)
        code = self.expected(caller, action, override, with_dataset)
        res = self.clients[caller].post(f"/api/runtime-canaries/{self.canary_id}/action",
                                        json=body)
        note(f"{caller} {action} override={override} → {res.status_code} "
             f"{res.json().get('code')} (model: {code}; stage {self.ramp_stage}, "
             f"verdict {self.verdict and self.verdict.get('verdict')})")
        assert res.status_code != 500, res.text
        if code is not None:
            assert res.status_code in (403, 409, 422), res.text
            assert res.json()["code"] == code, (res.json(), code)
            if code == "canary.forbidden":
                detail = res.json()["detail"]
                assert detail["canary_id"] == self.canary_id and detail["action"] == action
            # a refusal has no side effect: the row and the AWS seams are untouched
            assert _snapshot(self.canary_id) == before, "a refused action changed the row"
            assert _online_ids(self.canary_id) == online_before
            assert len(self.seams.calls) == calls_before
            return
        assert res.status_code == 202, res.text
        row = _get(self.canary_id)
        assert row.running_action is None and row.error is None, (row.running_action, row.error)
        self.apply(action)
        if action == "verdict":
            self.verdict = dict(verdict)
            _, current = canary_service._current_round(row)
            assert current["verdict"]["verdict"] == verdict["verdict"]
        if action == "advance":
            assert int(row.artifacts["setup"]["ramp_stage"]) == self.ramp_stage
            cw, tw = canary_service.RAMP_WEIGHTS[self.ramp_stage]
            assert row.artifacts["setup"]["weights"] == {"C": cw, "T1": tw}
        if action == "complete":
            agent = _agent(self.agent_id)
            assert agent.version == row.artifacts["setup"]["v_candidate"]
            assert agent.spec["system_prompt"] == "candidate prompt"
        if action == "cleanup":
            assert row.artifacts["abtest"]["execution_status"] == routing.STOPPED \
                if row.artifacts.get("abtest") else True
            assert _online_ids(self.canary_id) == []
            assert _assignments(self.canary_id) == 0
            if row.artifacts.get("setup", {}).get("runtime_id"):
                assert set(self.seams.deleted_endpoints) >= {
                    row.artifacts["setup"]["stable_endpoint"],
                    row.artifacts["setup"]["treatment_endpoint"]}

    @precondition(lambda self: not self.cleared and self.stuck is None and self.clear_job is None
                  and self.status == "running")
    @rule(action=st.sampled_from(MUTATING))
    def thread_dies_with_the_flag(self, action):
        """The action's daemon thread died without releasing ``running_action``. Only while
        no clear is live: the route refuses every action with ``assistant.agent_clearing``
        while a clear job is live, so a live thread cannot coexist with one (and
        ``request_purge`` refuses while a thread runs — ``conversation_busy``)."""
        canary_service._update(self.canary_id, running_action=action, progress="working…")
        self.stuck = action

    @rule()
    def restart(self):
        create_app(resume_jobs=True)
        if self.clear_job is not None:
            from tests.test_assistant_purge_dependencies import wait_job

            wait_job(self.clear_job)
            if self.cleared or _get(self.canary_id).status == "cleaned":
                self.after_clear()
        row = _get(self.canary_id)
        if self.stuck and not (self.clear_job and not self.cleared):
            assert row.running_action is None
            assert "interrupted by a backend restart" in (row.error or ""), row.error
            self.stuck = None
        # the startup sweep left nothing behind (a canary under a live clear is skipped by
        # both calls, N15-obs10)
        assert canary_service.clear_stale_running_actions() == []

    @precondition(lambda self: self.conversation_id is None and not self.cleared)
    @rule()
    def start_clear(self):
        """An architect conversation approved this Agent; its owner clears it."""
        db = SessionLocal()
        try:
            conv = AssistantConversation(workspace_id=WS, owner="ada", owner_principal="user:ada",
                                         title="canary demo", turns=1)
            db.add(conv)
            db.flush()
            db.add(AssistantProposal(workspace_id=WS, conversation_id=conv.id, revision=1,
                                     source="model", content={"name": self.agent.name},
                                     content_hash="h" * 64, status="approved",
                                     created_by="ada", approved_by="ada",
                                     approved_at=datetime.now(UTC), agent_id=self.agent_id))
            db.add(AssistantMessage(workspace_id=WS, conversation_id=conv.id, turn=1,
                                    role="user", text="hi"))
            db.commit()
            self.conversation_id = conv.id
            try:
                job, done = purge.request_purge(db, db.get(AssistantConversation, conv.id),
                                                can=lambda _p: True)
            except AppError as exc:
                # a running canary action blocks the clear before anything is deleted
                assert self.stuck is not None, exc.code
                assert exc.code == "assistant.conversation_busy", exc.code
                assert _snapshot(self.canary_id)[:3] == (
                    self.status if self.status != "cleaned" else "cleaned",
                    _get(self.canary_id).stage, self.stuck)
                self.conversation_id = None
                db.delete(db.get(AssistantConversation, conv.id))
                db.commit()
                return
            assert done is None and job.status == "queued"
            self.clear_job = job.id
        finally:
            db.close()

    @precondition(lambda self: self.clear_job is not None and not self.cleared)
    @rule()
    def finish_clear(self):
        purge.execute_purge_job(self.clear_job, workspace=ws_ctx(id=WS))
        db = SessionLocal()
        try:
            job = db.get(Job, self.clear_job)
            assert job.status == "succeeded", (job.status, job.error, job.payload.get("blocker"))
            assert db.get(AssistantConversation, self.conversation_id) is None
        finally:
            db.close()
        self.after_clear()

    def after_clear(self) -> None:
        self.cleared = True
        self.status = "cleaned"
        row = _get(self.canary_id)
        assert row.status == "cleaned" and row.running_action is None, (row.status, row.error)
        assert _agent(self.agent_id).status == "deleted"
        assert _online_ids(self.canary_id) == [] and _assignments(self.canary_id) == 0

    # -- invariants -----------------------------------------------------------
    @invariant()
    def ledger_has_no_orphans(self):
        """The shared scan, restricted to the rows this machine owns (another test's late
        background write must not be blamed on this walk)."""
        own = {self.canary_id, self.canary_id[:8], self.agent_id, self.conversation_id,
               self.clear_job} - {None}
        found = [v for v in scan_ledger(SessionLocal) if any(i in v for i in own)]
        assert found == []

    @invariant()
    def ledger_mirrors_the_model(self):
        row = _get(self.canary_id)
        assert row.status == self.status, (row.status, self.status)
        assert row.status in ("running", "completed", "rolled_back", "cleaned")
        assert row.running_action == self.stuck
        setup = row.artifacts.get("setup")
        assert bool(setup) == self.setup_done
        if setup and self.status == "running":
            assert int(setup["ramp_stage"]) == self.ramp_stage
            assert 0 <= self.ramp_stage <= FINAL
        abtest = row.artifacts.get("abtest")
        if abtest:
            expected = routing.RUNNING if self.status == "running" else routing.STOPPED
            assert abtest["execution_status"] == expected, (abtest["execution_status"], self.status)
        if self.status == "cleaned":
            assert _online_ids(self.canary_id) == [] and _assignments(self.canary_id) == 0
        elif self.setup_done:
            assert len(_online_ids(self.canary_id)) == 2
        if setup and self.status == "running":
            _, current = canary_service._current_round(row)
            assert (current is not None and bool(current.get("traffic_attempts"))) == \
                self.traffic_at_stage
            assert ((current or {}).get("verdict") is not None) == (self.verdict is not None)


def _run(clients, ada_id, seams, dataset_id, *, examples: int, steps: int) -> None:
    run_state_machine_as_test(
        lambda: CanaryMachine(clients, ada_id, seams, dataset_id),
        settings=settings(max_examples=examples, stateful_step_count=steps, deadline=None,
                          suppress_health_check=[HealthCheck.too_slow,
                                                 HealthCheck.function_scoped_fixture]),
    )


@pytest.fixture
def world(admin, ada, bob, seams):
    ada_id, ada_client = ada
    _bob_id, bob_client = bob
    return {"admin": admin, "ada": ada_client, "bob": bob_client}, ada_id, seams, _dataset(None)


def test_canary_state_machine(world):
    clients, ada_id, seams, dataset_id = world
    _run(clients, ada_id, seams, dataset_id, examples=3 * SCALE, steps=24 * SCALE)


@pytest.mark.slow
@pytest.mark.skipif(not SOAK, reason="soak variant — LAUNCHPAD_SOAK=1")
def test_soak_canary_state_machine(world):
    clients, ada_id, seams, dataset_id = world
    _run(clients, ada_id, seams, dataset_id, examples=30, steps=100)


def test_null_creator_canary_is_an_administrators_at_every_stage(world):
    """users-and-ownership.md §8: "NULL creator → administrator-only" — before any stage
    check, so the member learns nothing about the row's state."""
    clients, _ada_id, _seams, _dataset_id = world
    agent = _agent(_active_agent("legacy-canary-agent"))
    canary_id = _new_canary(agent, None)
    for action in MUTATING:
        res = clients["ada"].post(f"/api/runtime-canaries/{canary_id}/action",
                                  json={"action": action})
        assert res.status_code == 403 and res.json()["code"] == "canary.forbidden", res.text
    res = clients["admin"].post(f"/api/runtime-canaries/{canary_id}/action",
                                json={"action": "setup"})
    assert res.status_code == 202, res.text
    assert len(_online_ids(canary_id)) == 2
    res = clients["bob"].post(f"/api/runtime-canaries/{canary_id}/action",
                              json={"action": "verdict"})
    assert res.status_code == 409 and res.json()["code"] == "canary.stage_not_ready"


def test_cleanup_is_idempotent_and_leaves_no_online_config_or_assignment(world):
    """evaluation-engine.md: cleanup deletes the per-arm online configs, the assignments and
    stops the A/B row; a second cleanup of a ``cleaned`` canary changes nothing."""
    clients, ada_id, seams, dataset_id = world
    agent = _agent(_active_agent("twice-cleaned", created_by=ada_id))
    canary_id = _new_canary(agent, ada_id)
    url = f"/api/runtime-canaries/{canary_id}/action"
    assert clients["ada"].post(url, json={"action": "setup"}).status_code == 202
    assert clients["ada"].post(url, json={"action": "traffic", "dataset_id": dataset_id}
                               ).status_code == 202
    assert _assignments(canary_id) > 0 and len(_online_ids(canary_id)) == 2
    # N17-obs452: a running canary past setup is decided before its cleanup
    refused = clients["ada"].post(url, json={"action": "cleanup"})
    assert refused.status_code == 409
    assert refused.json()["code"] == "canary.cleanup_requires_decision"
    assert clients["ada"].post(url, json={"action": "rollback"}).status_code == 202
    assert clients["ada"].post(url, json={"action": "cleanup"}).status_code == 202
    first = _snapshot(canary_id)
    assert first[0] == "cleaned" and _online_ids(canary_id) == [] and _assignments(canary_id) == 0
    db = SessionLocal()
    try:
        assert db.query(OnlineEvalConfig).filter(
            OnlineEvalConfig.name.like(f"can_{canary_id[:8]}_%")).count() == 0
    finally:
        db.close()
    assert clients["ada"].post(url, json={"action": "cleanup"}).status_code == 202
    second = _snapshot(canary_id)
    assert second[:3] == first[:3] and second[3]["abtest"] == first[3]["abtest"]
    assert second[3]["cleanup"][0] == first[3]["cleanup"][0]  # the A/B row stays STOPPED
