#!/usr/bin/env python3
"""E2E for the Claude SDK container path — REAL AWS.

Flow: [local docker smoke] → POST /api/agents (container) → poll stages
      (CodeBuild+ECR+Runtime) → invoke → observability → optional Evaluation
      → DELETE.

Run:
  cd backend
  uv run python scripts/e2e_claude_sdk.py [--keep] [--skip-local] [--with-eval]
"""

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

import httpx
from _e2e_client import e2e_client

AGENT_NAME = "e2e-claude-sdk"
SYSTEM_PROMPT = (
    "You are a terse assistant. Answer with just the result."
)
EVAL_DATASET_NAME = "e2e-claude-native-telemetry"


def wait_for_observability(
    client: httpx.Client,
    *,
    agent_name: str,
    session_id: str,
    timeout_s: int = 600,
) -> dict:
    """Return the native Claude trace for the exact Runtime session."""
    deadline = time.time() + timeout_s
    last = ""
    while time.time() < deadline:
        response = client.get(
            "/api/observability/traces",
            params={
                "range": "1h",
                "agent": agent_name,
                "session": session_id,
                "force": "true",
            },
        )
        response.raise_for_status()
        traces = response.json().get("traces") or []
        for trace in traces:
            if trace.get("session_id") != session_id:
                continue
            detail_response = client.get(
                f"/api/observability/traces/{trace['trace_id']}",
                params={"range": "1h", "force": "true"},
            )
            if not detail_response.is_success:
                continue
            detail = detail_response.json()
            native = next(
                (
                    span
                    for span in detail.get("spans") or []
                    if span.get("name") == "ClaudeAgentSDK.query"
                ),
                None,
            )
            if (
                native
                and (native.get("attributes") or {}).get("session.id") == session_id
            ):
                return {"trace": trace, "detail": detail, "native_span": native}
        state = f"{len(traces)} candidate trace(s)"
        if state != last:
            print(f"  [{time.strftime('%H:%M:%S')}] {state}; waiting for native span")
            last = state
        time.sleep(15)  # nosemgrep: arbitrary-sleep
    raise TimeoutError("native Claude trace did not become visible in Observability")


def wait_for_eval_run(
    client: httpx.Client, run_id: str, timeout_s: int = 1800
) -> dict:
    deadline = time.time() + timeout_s
    last = ""
    while time.time() < deadline:
        response = client.get(f"/api/eval/runs/{run_id}")
        response.raise_for_status()
        run = response.json()
        state = f"{run['status']} (queue={run['queue_position']})"
        if state != last:
            print(f"  [{time.strftime('%H:%M:%S')}] {state}")
            last = state
        if run["status"] in ("completed", "failed"):
            return run
        time.sleep(15)  # nosemgrep: arbitrary-sleep
    raise TimeoutError("Claude SDK evaluation run did not finish")


def create_evaluation_dataset(client: httpx.Client) -> str:
    print("── creating native-telemetry evaluation dataset…")
    dataset_response = client.post(
        "/api/eval/datasets",
        json={
            "name": EVAL_DATASET_NAME,
            "items": [
                {
                    "prompt": "What is 7 multiplied by 8? Reply with just the number.",
                    "expected": "56",
                }
            ],
        },
    )
    dataset_response.raise_for_status()
    return dataset_response.json()["id"]


def run_evaluation(client: httpx.Client, agent_id: str, dataset_id: str) -> dict:
    print("── starting AgentCore Batch Evaluation (Builtin.Correctness)…")
    run_response = client.post(
        "/api/eval/runs",
        json={
            "agent_id": agent_id,
            "dataset_id": dataset_id,
            "evaluators": ["Builtin.Correctness"],
            "wait_seconds": 180,
        },
    )
    run_response.raise_for_status()
    result = wait_for_eval_run(client, run_response.json()["id"])
    if result["status"] != "completed":
        raise AssertionError(f"Claude SDK evaluation failed: {result.get('error')}")
    scores = {
        score["evaluatorId"]: score["score"] for score in result.get("scores") or []
    }
    correctness = scores.get("Builtin.Correctness")
    if correctness is None or float(correctness) < 0.99:
        raise AssertionError(f"expected Correctness 1.0, got: {scores}")
    print(f"  batch: {result['batch_eval_id']}")
    print(f"  scores: {json.dumps(result['scores'], indent=1)}")
    return result


def assert_evaluation_session_observable(
    client: httpx.Client, session_id: str, timeout_s: int = 300
) -> None:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        response = client.get(
            f"/api/observability/sessions/{session_id}",
            params={"range": "1h", "force": "true"},
        )
        if response.is_success:
            payload = response.json()
            transcript = payload.get("transcript") or {}
            traces = payload.get("traces") or []
            for trace in traces:
                detail_response = client.get(
                    f"/api/observability/traces/{trace['trace_id']}",
                    params={"range": "1h", "force": "true"},
                )
                if not detail_response.is_success:
                    continue
                native = next(
                    (
                        span
                        for span in detail_response.json().get("spans") or []
                        if span.get("name") == "ClaudeAgentSDK.query"
                        and (span.get("attributes") or {}).get("session.id") == session_id
                    ),
                    None,
                )
                if (
                    transcript.get("available")
                    and transcript.get("source") == "eval"
                    and transcript.get("origin") == "logs"
                    and transcript.get("turns")
                    and native
                ):
                    print(
                        "  evaluation session observable · "
                        f"{len(traces)} trace(s) · transcript origin=logs"
                    )
                    return
        time.sleep(15)  # nosemgrep: arbitrary-sleep
    raise TimeoutError("evaluation session did not become visible in Observability")


def cleanup_resources(
    client: httpx.Client,
    *,
    agent_id: str | None,
    dataset_id: str | None,
) -> None:
    errors: list[str] = []
    if dataset_id:
        print("── deleting evaluation dataset…")
        try:
            client.delete(f"/api/eval/datasets/{dataset_id}").raise_for_status()
        except Exception as exc:  # noqa: BLE001 - finish all cleanup attempts
            errors.append(f"dataset {dataset_id}: {exc}")
    if agent_id:
        print("── deleting agent…")
        try:
            client.delete(f"/api/agents/{agent_id}").raise_for_status()
            status = client.get(f"/api/agents/{agent_id}").json()["status"]
            print(f"ledger status after delete: {status}")
        except Exception as exc:  # noqa: BLE001 - report after all cleanup attempts
            errors.append(f"agent {agent_id}: {exc}")
    if errors:
        raise RuntimeError("cleanup failed: " + "; ".join(errors))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://localhost:8000")
    parser.add_argument("--keep", action="store_true")
    parser.add_argument("--skip-local", action="store_true")
    parser.add_argument("--with-eval", action="store_true")
    parser.add_argument("--timeout", type=int, default=1500)
    args = parser.parse_args()

    if not args.skip_local:
        print("── local docker smoke (pre-CodeBuild gate)…")
        repo_root = Path(__file__).resolve().parents[2]
        smoke = subprocess.run(
            ["bash", str(repo_root / "scripts" / "local_container_smoke.sh")],
            capture_output=True,
            text=True,
        )
        print(smoke.stdout[-600:])
        if smoke.returncode != 0:
            print(f"local smoke failed:\n{smoke.stderr[-1500:]}")
            return 1

    client = e2e_client(args.base, timeout=180)
    for agent in client.get("/api/agents").json()["agents"]:
        if agent["name"] == AGENT_NAME:
            print(f"cleaning leftover agent {agent['id']}")
            client.delete(f"/api/agents/{agent['id']}").raise_for_status()
    for dataset in client.get("/api/eval/datasets").json().get("datasets") or []:
        if dataset["name"] == EVAL_DATASET_NAME:
            print(f"cleaning leftover dataset {dataset['id']}")
            client.delete(f"/api/eval/datasets/{dataset['id']}").raise_for_status()

    agent_id: str | None = None
    dataset_id: str | None = None
    try:
        print("── creating container agent…")
        res = client.post(
            "/api/agents",
            json={
                "name": AGENT_NAME,
                "method": "container",
                "system_prompt": SYSTEM_PROMPT,
                "memory": {"short_term": False, "long_term": False},
            },
        )
        res.raise_for_status()
        body = res.json()
        agent_id, job_id = body["agent"]["id"], body["job_id"]
        print(f"agent {agent_id} · job {job_id}")

        seen: set[str] = set()
        deadline = time.time() + args.timeout
        status = "deploying"
        job: dict = {}
        while time.time() < deadline:
            job = client.get(f"/api/jobs/{job_id}").json()
            for ev in job["events"]:
                key = f"{ev['ts']}{ev['msg'][:40]}"
                if key not in seen:
                    seen.add(key)
                    print(
                        f"  [{ev['ts'][11:19]}] {ev['stage']:<9} {ev['msg'][:100]}"
                    )
            status = client.get(f"/api/agents/{agent_id}").json()["status"]
            if status in ("active", "failed"):
                break
            # Real-AWS deployment polling is intentionally paced and attempt-bounded.
            time.sleep(10)  # nosemgrep: arbitrary-sleep

        if status != "active":
            raise AssertionError(
                f"agent deployment failed: status={status} error={job.get('error')}"
            )

        arn = client.get(f"/api/agents/{agent_id}").json()["arn"]
        print(f"── runtime READY · arn {arn}")

        print("── invoking: what is 6*7?")
        inv = client.post(
            f"/api/agents/{agent_id}/invoke",
            json={"prompt": "What is 6*7? Reply with just the number."},
        )
        inv.raise_for_status()
        answer = inv.json()
        print(f"answer: {answer['text']!r} · {answer['latency_ms']}ms")
        assert "42" in answer["text"], f"expected '42', got: {answer['text']!r}"
        print("── invoke assertion PASSED")

        print("── waiting for native Claude telemetry in Launchpad Observability…")
        observed = wait_for_observability(
            client,
            agent_name=AGENT_NAME,
            session_id=answer["session_id"],
        )
        native = observed["native_span"]
        assert native["category"] == "agent", native
        assert native.get("model"), native
        assert native["attributes"]["session.id"] == answer["session_id"], native
        tokens = native.get("tokens") or {}
        assert tokens.get("input", 0) > 0 and tokens.get("output", 0) > 0, native
        print(
            f"  trace {observed['trace']['trace_id']} · "
            f"session={answer['session_id']} · model={native['model']} · "
            f"tokens={tokens['input']:.0f}/{tokens['output']:.0f}"
        )

        if args.with_eval:
            dataset_id = create_evaluation_dataset(client)
            result = run_evaluation(client, agent_id, dataset_id)
            session_ids = result.get("session_ids") or []
            assert session_ids, "evaluation completed without runtime session ids"
            assert_evaluation_session_observable(client, session_ids[-1])
    finally:
        if args.keep:
            if dataset_id:
                print(f"--keep set: leaving dataset {dataset_id}")
            if agent_id:
                print(f"--keep set: leaving agent {agent_id} deployed")
        else:
            cleanup_resources(
                client,
                agent_id=agent_id,
                dataset_id=dataset_id,
            )

    print("E2E CLAUDE SDK: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
