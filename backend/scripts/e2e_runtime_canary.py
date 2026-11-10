#!/usr/bin/env python3
"""E2E: production target-based canary against real AWS (self-cleaning).

Drives the Model-1 canary end-to-end at the SERVICE layer (no HTTP/backend
process needed) on an existing active zip_runtime agent:

  setup   → mint a candidate version + stable/treatment named endpoints +
            per-variant local online-eval configs + the local 90/10 A/B row
  route   → routing.active_owner answers the canary with endpoint qualifiers
  invoke  → invoke_agent_text sends real traffic with ``qualifier=stable…|treat…``
  rollback→ roll-forward: re-deploy the current spec so DEFAULT returns to
            v_current behavior (production never keeps the untested candidate)
  cleanup → tear down both endpoints + the online-eval configs + assignments
            (the agent keeps throwaway runtime versions — harmless)

CN (N12 W-E3): no per-canary Gateway, targets or AgentCore A/B test exist in
aws-cn; the split lives in Launchpad's invoke chain. Validates the real AgentCore
shapes that unit tests stub (named endpoints — R-7 probe —, UpdateAgentRuntime
versions, roll-forward). The chosen agent's DEFAULT is restored by rollback.

Run:  cd backend && uv run python scripts/e2e_runtime_canary.py [agent-name]
"""

import argparse
import time

from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
from app.models.ledger import Agent
from app.optimization import canary_service as cs
from app.optimization import routing
from app.optimization.canary_routers import CandidateEdit, _resolve_edited_spec
from app.optimization.models import RuntimeCanary
from app.services.agentcore.client import control_client
from app.services.invoke import invoke_agent_text
from app.services.workspace import default_workspace_context


def _parse_agent_name() -> str:
    """Parsed at import time because the module-level flow below reads it.

    Going through argparse (rather than `sys.argv[1]`) is what makes `--help`
    print usage instead of silently canary-ing the default agent on real AWS.
    """
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "agent_name", nargs="?", default="eval-target",
        help="active zip_runtime agent to canary (default: eval-target)",
    )
    return parser.parse_args().agent_name


AGENT_NAME = _parse_agent_name()


def log(msg: str) -> None:
    print(f"  [{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def _get_agent(name: str) -> Agent:
    db = SessionLocal()
    try:
        # Hub workspace only: names are unique per workspace, and the leak check
        # at the end lists gateways in the hub region.
        agent = (
            db.query(Agent)
            .filter(
                Agent.workspace_id == DEFAULT_WORKSPACE_ID,
                Agent.name == name,
                Agent.status == "active",
            )
            .first()
        )
        if agent is None:
            raise SystemExit(f"no active agent named {name!r}")
        db.expunge(agent)
        return agent
    finally:
        db.close()


def _delete_canary_row(canary_id: str) -> None:
    db = SessionLocal()
    try:
        row = db.get(RuntimeCanary, canary_id)
        if row is not None:
            db.delete(row)
            db.commit()
    finally:
        db.close()


def main() -> int:
    workspace = default_workspace_context()
    agent = _get_agent(AGENT_NAME)
    print(f"── E2E target: {agent.name} ({agent.resource_id})", flush=True)

    candidate = CandidateEdit(
        system_prompt=(
            (agent.spec or {}).get("system_prompt", "")
            + "\n\n[canary-e2e candidate] Always end replies with 'CANARY-OK'."
        )
    )
    edited_spec = _resolve_edited_spec(agent, candidate)
    canary = cs.start_canary(agent, edited_spec, workspace)
    canary_id = canary.id
    print(f"── created canary row {canary_id}", flush=True)

    cleaned = False
    try:
        # ── setup ────────────────────────────────────────────────────────────
        print("── setup (mint candidate + endpoints + local A/B row)…", flush=True)
        setup = cs.act_setup(canary_id, log)
        for key in (
            "v_current", "v_candidate", "stable_endpoint", "treatment_endpoint",
            "ab_test_id", "runtime_id",
        ):
            assert setup.get(key), f"setup artifact missing {key!r}: {setup}"
        assert setup["champion"]["target_name"] and setup["challenger"]["target_name"]
        assert setup["v_candidate"] != setup["v_current"], "candidate must be a new version"
        print(
            f"   v_current={setup['v_current']} v_candidate={setup['v_candidate']} "
            f"ab={setup['ab_test_id']}", flush=True)
        print(
            f"   endpoints: stable={setup['stable_endpoint']} "
            f"treatment={setup['treatment_endpoint']} "
            f"targets: {setup['champion']['target_name']}/"
            f"{setup['challenger']['target_name']}", flush=True)

        # ── route: the canary owns the agent's traffic, arms = endpoints ─────
        db = SessionLocal()
        try:
            owner = routing.active_owner(db, agent.id)
        finally:
            db.close()
        assert owner and owner.kind == "canary" and owner.id == canary_id, owner
        assert owner.arm("C").qualifier == setup["stable_endpoint"]
        assert owner.arm("T1").qualifier == setup["treatment_endpoint"]
        print(f"── routing.active_owner LIVE: weights={owner.weights}", flush=True)

        # ── invoke: routed to a named endpoint by qualifier (R-7 probe) ──────
        print("── invoke through the canary split…", flush=True)
        fresh = _get_agent(AGENT_NAME)
        result = invoke_agent_text(fresh, "Say hello in five words.")
        text = (result.get("text") or "").strip()
        assert text, f"routed invoke returned no text: {result}"
        db = SessionLocal()
        try:
            arms = routing.assigned_sessions(db, canary_id)
        finally:
            db.close()
        assert result["session_id"] in arms, "live session must be assigned an arm"
        print(f"   routed invoke OK · arm={arms[result['session_id']]} · {text[:120]!r}",
              flush=True)

        # ── rollback: roll-forward restores v_current on DEFAULT ─────────────
        print("── rollback (roll-forward)…", flush=True)
        rb = cs.act_rollback(canary_id, log)
        assert rb.get("winner") == "champion" and rb.get("restored_version"), rb
        assert "experimental_only" not in rb, "rollback must not be experimental_only"
        print(f"   rolled back · restored_version={rb['restored_version']}", flush=True)

        # after rollback the canary is not running → invoke goes direct (DEFAULT
        # = rolled-forward v_current behavior), no routing owner
        db = SessionLocal()
        try:
            assert routing.active_owner(db, agent.id) is None, "owner should be gone"
        finally:
            db.close()
        fresh = _get_agent(AGENT_NAME)
        post = invoke_agent_text(fresh, "Say hello in five words.")
        assert (post.get("text") or "").strip(), f"post-rollback invoke empty: {post}"
        print(f"   post-rollback direct invoke OK · {post['text'][:120]!r}", flush=True)

        # ── cleanup: tear down dedicated resources ───────────────────────────
        print("── cleanup…", flush=True)
        results = cs.act_cleanup(canary_id, log)
        cleaned = True
        for row in results:
            print(f"   {row.get('status','?'):<8} {row.get('category','?')}", flush=True)
        skipped = [r for r in results if r.get("status") != "deleted"]
        assert not skipped, f"cleanup skipped: {[r['category'] for r in skipped]}"

        # ── verify no leaks: both named endpoints gone ───────────────────────
        control = control_client(workspace)
        eps = control.list_agent_runtime_endpoints(
            agentRuntimeId=setup["runtime_id"], maxResults=50
        ).get("runtimeEndpoints", [])
        ep_names = {e.get("name") for e in eps}
        assert setup["stable_endpoint"] not in ep_names, "stable endpoint leaked"
        assert setup["treatment_endpoint"] not in ep_names, "treatment endpoint leaked"
        print("── no leaks: both named endpoints deleted", flush=True)

        if skipped:
            print(f"!! cleanup had {len(skipped)} non-deleted categories", flush=True)
            return 1
        print("\nE2E RUNTIME CANARY: PASS", flush=True)
        return 0
    finally:
        if not cleaned:
            print("── FINALLY: best-effort cleanup…", flush=True)
            try:
                cs.act_cleanup(canary_id, log)
            except Exception as exc:  # noqa: BLE001
                print(f"   cleanup error: {type(exc).__name__}: {exc}", flush=True)
        _delete_canary_row(canary_id)


if __name__ == "__main__":
    raise SystemExit(main())
