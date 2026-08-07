"""Deploy the front-desk A2A routing demo agent.

Usage: uv run python scripts/deploy_frontdesk_agent.py [--api http://localhost:8000]
       [--name front-desk] [--model global.anthropic.claude-sonnet-4-6]

1. Ensures the execution role can search the registry and invoke harnesses
   (inline policy `launchpad-a2a-frontdesk`; InvokeAgentRuntime pre-exists).
2. Creates or redeploys in place a zip_runtime code_bundle spec
   (samples/frontdesk_agent/main.py) and polls until the agent is active.
"""

import argparse
import json
import sys
import time
from pathlib import Path

import boto3
import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import get_settings  # noqa: E402

SAMPLE = Path(__file__).resolve().parents[1] / "samples" / "frontdesk_agent" / "main.py"

FRONTDESK_PROMPT = (
    "Enterprise front desk: discovers APPROVED specialist agents in the "
    "AgentCore Registry by their A2A cards and routes every question to the "
    "best-matching specialist over its declared transport (standard A2A "
    "JSON-RPC or platform invoke). No domain knowledge of its own."
)


def ensure_iam(settings) -> None:
    role = settings.resources["execution_role_arn"].rsplit("/", 1)[-1]
    registry_arn = settings.resources["registry_arn"]
    harness_arn = (
        f"arn:aws:bedrock-agentcore:{settings.region}:{settings.account_id}:harness/*"
    )
    boto3.client("iam").put_role_policy(
        RoleName=role,
        PolicyName="launchpad-a2a-frontdesk",
        PolicyDocument=json.dumps({
            "Version": "2012-10-17",
            "Statement": [
                {"Effect": "Allow",
                 "Action": ["agent-registry:SearchDiscoverableRegistryRecords"],
                 "Resource": [registry_arn]},
                {"Effect": "Allow",
                 "Action": ["bedrock-agentcore:InvokeHarness"],
                 "Resource": [harness_arn]},
            ],
        }),
    )
    print(f"iam ✓ inline policy launchpad-a2a-frontdesk on {role}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--api", default="http://localhost:8000")
    parser.add_argument("--name", default="front-desk")
    parser.add_argument("--model", default="global.anthropic.claude-sonnet-4-6")
    args = parser.parse_args()

    settings = get_settings()
    ensure_iam(settings)

    code = SAMPLE.read_text(encoding="utf-8").replace(
        "__FRONTDESK_MODEL_ID__", args.model)
    spec = {
        "name": args.name,
        "method": "zip_runtime",
        "model_id": args.model,
        "system_prompt": FRONTDESK_PROMPT,
        "code_bundle": {"main.py": code},
        "memory": {"short_term": True, "long_term": False},
        "env": {
            "LAUNCHPAD_REGISTRY_ID": settings.resources["registry_id"],
            "FRONTDESK_NAME": args.name,
        },
    }
    agents_res = httpx.get(f"{args.api}/api/agents", timeout=30)
    agents_res.raise_for_status()
    existing = next(
        (
            agent
            for agent in agents_res.json().get("agents", [])
            if agent["name"] == args.name and agent["status"] != "deleted"
        ),
        None,
    )
    if existing:
        endpoint = f"{args.api}/api/agents/{existing['id']}/redeploy"
        action = "redeploying"
    else:
        endpoint = f"{args.api}/api/agents"
        action = "deploying"
    res = httpx.post(endpoint, json=spec, timeout=60)
    res.raise_for_status()
    agent_id = res.json()["agent"]["id"]
    print(f"{action} agent {agent_id} …")
    for _ in range(90):
        # Real-AWS deployment polling is intentionally paced and attempt-bounded.
        time.sleep(10)  # nosemgrep: arbitrary-sleep
        agent = httpx.get(f"{args.api}/api/agents/{agent_id}", timeout=30).json()
        status = agent.get("status")
        print("status:", status)
        if status in ("active", "failed"):
            print("arn:", agent.get("arn"))
            sys.exit(0 if status == "active" else 2)
    print("timed out waiting for active")
    sys.exit(2)


if __name__ == "__main__":
    main()
