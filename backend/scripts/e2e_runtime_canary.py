#!/usr/bin/env python3
"""E2E: production target-based canary against real AWS (self-cleaning).

Drives the Model-1 canary end-to-end at the SERVICE layer (no HTTP/backend
process needed) on an existing active zip_runtime agent:

  setup   → mint a candidate version + dedicated gateway + stable/treatment
            named endpoints + qualifier targets + per-variant online-eval + a
            90/10 target-based A/B test
  route   → active_canary_route returns the live-gateway form
  invoke  → invoke_agent_text routes real traffic through the canary gateway
  rollback→ roll-forward: re-deploy the current spec so DEFAULT returns to
            v_current behavior (production never keeps the untested candidate)
  cleanup → tear down the dedicated gateway + both endpoints + targets + eval
            + A/B test (the agent keeps throwaway runtime versions — harmless)

Validates the real AgentCore shapes that unit tests stub (named endpoints,
qualifier targets, per-canary gateway, SigV4 gateway invoke, roll-forward).
The chosen agent's DEFAULT is restored by rollback; only throwaway versions and
the (deleted) dedicated resources are created.

Run:  cd backend && uv run python scripts/e2e_runtime_canary.py [agent-name]
"""

import sys
import time

from app.core.db import SessionLocal
from app.models.ledger import Agent
from app.optimization import canary_service as cs
from app.optimization.canary_routers import CandidateEdit, _resolve_edited_spec
from app.optimization.models import RuntimeCanary
from app.services.agentcore.client import control_client
from app.services.invoke import invoke_agent_text

AGENT_NAME = sys.argv[1] if len(sys.argv) > 1 else "eval-target"


def log(msg: str) -> None:
    print(f"  [{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def _get_agent(name: str) -> Agent:
    db = SessionLocal()
    try:
        agent = (
            db.query(Agent)
            .filter(Agent.name == name, Agent.status == "active")
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
    agent = _get_agent(AGENT_NAME)
    print(f"── E2E target: {agent.name} ({agent.resource_id})", flush=True)

    candidate = CandidateEdit(
        system_prompt=(
            (agent.spec or {}).get("system_prompt", "")
            + "\n\n[canary-e2e candidate] Always end replies with 'CANARY-OK'."
        )
    )
    edited_spec = _resolve_edited_spec(agent, candidate)
    canary = cs.start_canary(agent, edited_spec)
    canary_id = canary.id
    print(f"── created canary row {canary_id}", flush=True)

    cleaned = False
    try:
        # ── setup ────────────────────────────────────────────────────────────
        print("── setup (mint candidate + gateway + endpoints + A/B)…", flush=True)
        setup = cs.act_setup(canary_id, log)
        for key in (
            "v_current", "v_candidate", "stable_endpoint", "treatment_endpoint",
            "gateway_id", "gateway_url", "ab_test_id", "runtime_id",
        ):
            assert setup.get(key), f"setup artifact missing {key!r}: {setup}"
        assert setup["champion"]["target_name"] and setup["challenger"]["target_name"]
        assert setup["v_candidate"] != setup["v_current"], "candidate must be a new version"
        print(
            f"   v_current={setup['v_current']} v_candidate={setup['v_candidate']} "
            f"gw={setup['gateway_id']} ab={setup['ab_test_id']}", flush=True)
        print(
            f"   endpoints: stable={setup['stable_endpoint']} "
            f"treatment={setup['treatment_endpoint']} "
            f"targets: {setup['champion']['target_name']}/"
            f"{setup['challenger']['target_name']}", flush=True)

        # ── route: live-gateway form ─────────────────────────────────────────
        route = cs.active_canary_route(agent.id)
        assert route and route.get("gateway_url") and route.get("control_target"), \
            f"expected live-gateway route, got {route}"
        print(f"── active_canary_route LIVE: control_target={route['control_target']}", flush=True)

        # ── invoke: routed through the canary gateway (real SigV4) ───────────
        print("── invoke via canary gateway…", flush=True)
        fresh = _get_agent(AGENT_NAME)
        result = invoke_agent_text(fresh, "Say hello in five words.")
        text = (result.get("text") or "").strip()
        assert text, f"gateway-routed invoke returned no text: {result}"
        print(f"   gateway invoke OK · {text[:120]!r}", flush=True)

        # ── rollback: roll-forward restores v_current on DEFAULT ─────────────
        print("── rollback (roll-forward)…", flush=True)
        rb = cs.act_rollback(canary_id, log)
        assert rb.get("winner") == "champion" and rb.get("restored_version"), rb
        assert "experimental_only" not in rb, "rollback must not be experimental_only"
        print(f"   rolled back · restored_version={rb['restored_version']}", flush=True)

        # after rollback the canary is not running → invoke goes direct (DEFAULT
        # = rolled-forward v_current behavior), no gateway route
        assert cs.active_canary_route(agent.id) is None, "route should be gone after rollback"
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

        # ── verify no leaks: dedicated gateway + named endpoints gone ────────
        # DeleteGateway is accepted async; poll until it drops from the list.
        control = control_client()
        gw_name = f"lp-canary-{canary_id}"
        for _ in range(30):
            gateways = control.list_gateways(maxResults=100).get("items", [])
            if not any(g.get("name") == gw_name for g in gateways):
                break
            # Real-AWS cleanup polling is intentionally paced and attempt-bounded.
            time.sleep(4)  # nosemgrep: arbitrary-sleep
        else:
            raise AssertionError("dedicated canary gateway still present after cleanup")
        eps = control.list_agent_runtime_endpoints(
            agentRuntimeId=setup["runtime_id"], maxResults=50
        ).get("runtimeEndpoints", [])
        ep_names = {e.get("name") for e in eps}
        assert setup["stable_endpoint"] not in ep_names, "stable endpoint leaked"
        assert setup["treatment_endpoint"] not in ep_names, "treatment endpoint leaked"
        print("── no leaks: dedicated gateway + both named endpoints deleted", flush=True)

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
