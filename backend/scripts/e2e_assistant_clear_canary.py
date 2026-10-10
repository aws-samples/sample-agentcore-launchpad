#!/usr/bin/env python3
"""E2E (issue #246): CLEAR an architect conversation whose Harness Agent runs a canary.

REAL AWS, self-cleaning by design — the clear under test IS the cleanup. Run it against
a THROWAWAY backend whose ledger this script can also open (it seeds the conversation
rows directly: architect turns are LLM-driven and not what is under test):

    export LAUNCHPAD_DATABASE_URL=sqlite:////tmp/clear-e2e/launchpad.db
    uv run uvicorn app.main:app --port 8011 &          # same env, no --reload
    uv run python scripts/e2e_assistant_clear_canary.py --base http://localhost:8011

Flow: deploy a Harness Agent (v1) → re-publish (v2) → seed a conversation whose
approved proposal owns it → Harness canary v1 vs v2 at 50/50, setup on AWS (named
endpoints, dedicated gateway + targets + trace delivery, online evaluations, A/B test)
→ footprint must list the canary → DELETE the conversation → follow the clear job
(re-DELETE resumes a job that stopped on a still-DELETING blocker) → AWS readback: the
Harness, its backing runtime, both endpoints, gateway, A/B test, online evaluations,
trace delivery source and the execution role are gone; the shared KB gateway and the
shared execution role are still there.
"""

import argparse
import sys
import time
import uuid
from datetime import UTC, datetime

from _e2e_client import e2e_client

NAME = f"e2e-clear-{uuid.uuid4().hex[:6]}"
PROMPT_V1 = "You are a concise math assistant. Answer with just the result."
PROMPT_V2 = "You are a concise math assistant. Answer with just the number, nothing else."


def _wait_active(client, agent_id: str, timeout: int) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        agent = client.get(f"/api/agents/{agent_id}").json()
        if agent["status"] in ("active", "failed"):
            return agent
        time.sleep(5)  # nosemgrep: arbitrary-sleep
    raise SystemExit(f"agent {agent_id} not active after {timeout}s")


def _seed_conversation(agent_id: str) -> str:
    from app.core.db import DEFAULT_WORKSPACE_ID, SessionLocal
    from app.models.assistant import AssistantConversation, AssistantMessage, AssistantProposal

    db = SessionLocal()
    try:
        conv = AssistantConversation(workspace_id=DEFAULT_WORKSPACE_ID, owner="e2e",
                                     owner_principal="local-operator",
                                     title=f"#246 smoke {NAME}", turns=1)
        db.add(conv)
        db.flush()
        db.add(AssistantMessage(workspace_id=DEFAULT_WORKSPACE_ID, conversation_id=conv.id,
                                turn=1, role="user", text="build a math helper"))
        db.add(AssistantProposal(
            workspace_id=DEFAULT_WORKSPACE_ID, conversation_id=conv.id, revision=1,
            source="model", content={"name": NAME}, content_hash="0" * 64,
            status="approved", created_by="e2e", approved_by="e2e",
            approved_at=datetime.now(UTC), agent_id=agent_id))
        db.commit()
        return conv.id
    finally:
        db.close()


def _gone(fn, *args, **kwargs) -> bool:
    try:
        fn(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001
        return type(exc).__name__ in {"ResourceNotFoundException", "NotFoundException",
                                      "NoSuchEntityException", "NoSuchEntity"}
    return False


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://localhost:8011")
    parser.add_argument("--deploy-timeout", type=int, default=600)
    parser.add_argument("--setup-timeout", type=int, default=1200)
    parser.add_argument("--clear-timeout", type=int, default=3600)
    args = parser.parse_args()
    client = e2e_client(args.base, timeout=120)

    print(f"── deploying Harness agent {NAME} (v1)…")
    res = client.post("/api/agents", json={"name": NAME, "method": "harness",
                                           "system_prompt": PROMPT_V1,
                                           "memory": {"short_term": False, "long_term": False}})
    res.raise_for_status()
    agent_id = res.json()["agent"]["id"]
    agent = _wait_active(client, agent_id, args.deploy_timeout)
    if agent["status"] != "active":
        print(f"FAILED: deploy {agent['status']}")
        return 1
    spec = agent["spec"]
    print(f"   agent {agent_id} · harness {agent['resource_id']} · v{agent.get('version')}")

    print("── re-publishing (v2)…")
    res = client.post(f"/api/agents/{agent_id}/redeploy", json={**spec,
                                                                "system_prompt": PROMPT_V2})
    res.raise_for_status()
    agent = _wait_active(client, agent_id, args.deploy_timeout)
    versions = [str(v.get("version")) for v in
                client.get(f"/api/agents/{agent_id}/versions").json().get("versions", [])]
    print(f"   versions {versions} · ledger v{agent.get('version')}")
    latest = max(versions, key=int)
    control = min(versions, key=int)

    cid = _seed_conversation(agent_id)
    print(f"── seeded conversation {cid}")

    print(f"── Harness canary v{control} vs v{latest} at 50/50, setup on AWS…")
    res = client.post("/api/runtime-canaries", json={
        "agent_id": agent_id, "harness_versions": {"control": control, "treatment": latest},
        "start_stage": 1})
    res.raise_for_status()
    canary_id = res.json()["id"]
    client.post(f"/api/runtime-canaries/{canary_id}/action",
                json={"action": "setup"}).raise_for_status()
    deadline = time.time() + args.setup_timeout
    canary: dict = {}
    while time.time() < deadline:
        canary = client.get(f"/api/runtime-canaries/{canary_id}").json()
        if not canary["running_action"]:
            break
        time.sleep(10)  # nosemgrep: arbitrary-sleep
    setup = (canary["artifacts"] or {}).get("setup") or {}
    if canary.get("error") or not setup.get("ab_test_id"):
        print(f"FAILED: canary setup error={canary.get('error')} setup={setup}")
        return 1
    meta = canary["artifacts"]["agent_meta"]
    print(f"   canary {canary_id}: gateway {setup.get('gateway_id')} · "
          f"A/B {setup.get('ab_test_id')}"
          f" · endpoints {setup.get('stable_endpoint')}, {setup.get('treatment_endpoint')}")

    fp = client.get(f"/api/assistant/architect/conversations/{cid}/footprint").json()
    assert [c["id"] for c in fp["canaries"]] == [canary_id], fp["canaries"]
    assert fp["canaries"][0]["pending"] is True and fp["blockers"] == [], fp
    print(f"── footprint OK: agents {[a['id'] for a in fp['agents']]} · canary pending")

    print("── CLEAR…")
    attempts = 0
    deadline = time.time() + args.clear_timeout
    last_step = None
    while True:
        attempts += 1
        res = client.delete(f"/api/assistant/architect/conversations/{cid}")
        if res.status_code == 200:
            break
        assert res.status_code == 202, res.text
        job_id = res.json()["job_id"]
        print(f"   attempt {attempts}: job {job_id}")
        while True:
            job = client.get(f"/api/jobs/{job_id}").json()
            step = (job.get("payload") or {}).get("step")
            if step != last_step and step:
                print(f"     {time.strftime('%H:%M:%S')} {step['key']} · {step['state']} · "
                      f"{step['detail'][:140]}")
                last_step = step
            if job["status"] in ("succeeded", "failed"):
                break
            time.sleep(5)  # nosemgrep: arbitrary-sleep
        if job["status"] == "succeeded":
            break
        blocker = (job.get("payload") or {}).get("blocker") or {}
        print(f"   job failed: {blocker.get('code')} — {blocker.get('message')}")
        if blocker.get("code") != "assistant.purge_still_deleting" or time.time() > deadline:
            return 1
    print(f"── clear succeeded after {attempts} attempt(s)")
    assert client.get(f"/api/assistant/architect/conversations/{cid}").status_code == 404

    print("── AWS readback…")
    from app.optimization import canary_harness
    from app.services.agent_iam import role_name_for, shared_role_arn
    from app.services.agentcore.client import control_client, data_client
    from app.services.workspace import context_for_workspace

    ws = context_for_workspace("default")
    ctl, dat, logs, iam = (control_client(ws), data_client(ws), ws.client("logs"),
                           ws.client("iam"))
    harness_id = meta["resource_id"]
    source, _dest = canary_harness.trace_delivery_names(setup["gateway_id"])
    checks = {
        f"harness {harness_id}": _gone(ctl.get_harness, harnessId=harness_id),
        f"backing runtime {meta.get('backing_runtime_id')}": _gone(
            ctl.get_agent_runtime, agentRuntimeId=meta["backing_runtime_id"]),
        f"gateway {setup['gateway_id']}": _gone(ctl.get_gateway,
                                                gatewayIdentifier=setup["gateway_id"]),
        f"A/B test {setup['ab_test_id']}": _gone(dat.get_ab_test, abTestId=setup["ab_test_id"]),
        f"trace source {source}": _gone(logs.get_delivery_source, name=source),
        f"role {role_name_for(NAME, agent_id)}": _gone(iam.get_role,
                                                       RoleName=role_name_for(NAME, agent_id)),
    }
    for arm in ("champion", "challenger"):
        oe = (setup.get(arm) or {}).get("online_eval_id")
        if oe:
            checks[f"online eval {oe}"] = _gone(ctl.get_online_evaluation_config,
                                                onlineEvaluationConfigId=oe)
    shared = {
        "shared execution role": not _gone(iam.get_role,
                                           RoleName=shared_role_arn(ws).rsplit("/", 1)[-1]),
    }
    if ws.resources.get("kb_gateway_id"):
        shared["KB gateway"] = not _gone(ctl.get_gateway,
                                         gatewayIdentifier=ws.resources["kb_gateway_id"])
    ok = True
    for label, gone in checks.items():
        print(f"   {'gone ' if gone else 'LEFT '} {label}")
        ok &= gone
    for label, present in shared.items():
        print(f"   {'kept ' if present else 'LOST '} {label}")
        ok &= present
    record = agent.get("registry_record_id")
    if record:
        print(f"   note: A2A registry record {record} — the agent delete path does not remove it")
    print("PASS" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
