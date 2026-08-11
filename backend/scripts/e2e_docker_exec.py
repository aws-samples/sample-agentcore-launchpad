#!/usr/bin/env python3
"""E2E: studio local-debug docker sandbox backend (real containers, real AWS).

Requires a docker daemon, the exec image (scripts/setup_exec_docker.sh) and —
for the Bedrock step — ambient AWS credentials (instance role via IMDS is
enough; both launchpad boxes have hop limit 2).

Flow (all through the real local_exec/service code paths, docker backend forced
via settings monkeying — no HTTP server needed):
  1. print-only payload through execute_strands_code
  2. streaming payload through spawn_execution_subprocess + manual drain
  3. conversation-service turn (sync path, --messages replay)
  4. real Bedrock Mantle model call (skippable with --no-aws)
  5. timeout payload (busy loop) → killed at the deadline, container gone
  6. forward=false credential probe (only if the hardened network exists)
  7. final orphan sweep: no strands-exec-* containers left

Run:  cd backend && uv run python scripts/e2e_docker_exec.py [--no-aws]
"""

import argparse
import asyncio
import subprocess
import sys
import time

from app.core.config import get_settings
from app.services import local_exec

PASS = "\033[0;32mPASS\033[0m"
FAIL = "\033[0;31mFAIL\033[0m"

BEDROCK_CODE = """
import argparse
from strands import Agent
from strands.models.openai_responses import OpenAIResponsesModel

p = argparse.ArgumentParser()
p.add_argument("--user-input")
args = p.parse_args()

model = OpenAIResponsesModel(
    bedrock_mantle_config={"region_name": "us-east-1"},
    model_id="openai.gpt-5.6-terra",
    params={"max_output_tokens": 200},
)
agent = Agent(model=model, callback_handler=None)
print(agent(args.user_input or "Say OK"))
"""

CRED_PROBE = """
import urllib.request
try:
    import boto3
    creds = boto3.Session().get_credentials()
    print("boto3-creds:", "PRESENT" if creds else "NONE")
except Exception as exc:
    print("boto3-creds: NONE (", type(exc).__name__, ")")
try:
    req = urllib.request.Request(
        "http://169.254.169.254/latest/api/token",
        method="PUT",
        headers={"X-aws-ec2-metadata-token-ttl-seconds": "60"},
    )
    urllib.request.urlopen(req, timeout=3)
    print("imds: REACHABLE")
except Exception as exc:
    print("imds: BLOCKED (", type(exc).__name__, ")")
"""

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"  {PASS if ok else FAIL} {name}" + (f" — {detail}" if detail else ""))


def exec_containers() -> list[str]:
    out = subprocess.run(
        ["docker", "ps", "-aq", "--filter", "name=strands-exec-"],
        capture_output=True, text=True, timeout=15,
    )
    return out.stdout.split()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-aws", action="store_true", help="skip the Bedrock call")
    args = parser.parse_args()

    settings = get_settings()
    settings.studio_exec_backend = "docker"
    problem = local_exec.docker_exec_error(settings)
    if problem:
        print(f"precondition failed: {problem}")
        return 2

    # 1. one-shot execute
    print("== 1. one-shot execute (no AWS)")
    out = asyncio.run(local_exec.execute_strands_code("print('hello-from-sandbox')"))
    check("stdout captured", out.strip() == "hello-from-sandbox", repr(out.strip()))

    # 2. streaming spawn + drain
    print("== 2. streaming spawn")
    async def stream() -> str:
        run = await local_exec.spawn_execution_subprocess(
            "import time\nfor i in range(3):\n print('tick', i, flush=True)\n time.sleep(0.2)",
            None,
        )
        chunks = b""
        try:
            while True:
                data = await run.process.stdout.read(4096)
                if not data:
                    break
                chunks += data
            await run.process.wait()
        finally:
            run.cleanup()
        return chunks.decode()

    text = asyncio.run(stream())
    check("all ticks streamed", all(f"tick {i}" in text for i in range(3)), repr(text))

    # 3. conversation turn (sync --messages replay path)
    print("== 3. conversation turn")
    from app.models.conversation import CreateConversationRequest
    from app.services.conversation_service import ConversationService

    svc = ConversationService()
    echo_agent = (
        "import argparse, json\n"
        "p = argparse.ArgumentParser()\n"
        "p.add_argument('--messages')\n"
        "a = p.parse_args()\n"
        "msgs = json.loads(a.messages)\n"
        "print('echo:', msgs[-1]['content'][0]['text'])\n"
    )
    session = asyncio.run(svc.create_session(
        CreateConversationRequest(generated_code=echo_agent)
    ))
    reply = asyncio.run(svc.send_message(session.session_id, "ping-42"))
    check("turn succeeded", reply.success and "echo: ping-42" in reply.content,
          reply.error or reply.content)
    asyncio.run(svc.delete_session(session.session_id))

    # 4. real Bedrock Mantle call
    if args.no_aws:
        print("== 4. bedrock call — skipped (--no-aws)")
    else:
        print("== 4. bedrock mantle call (real AWS via IMDS)")
        out = asyncio.run(local_exec.execute_strands_code(
            BEDROCK_CODE, input_data="Reply with exactly: SANDBOX-OK"
        ))
        check("model answered", "SANDBOX-OK" in out, out.strip()[:120])

    # 5. timeout kill
    print("== 5. timeout kill")
    settings.execute_timeout_s = 10.0
    started = time.monotonic()
    try:
        asyncio.run(local_exec.execute_strands_code("while True: pass"))
        check("timeout raised", False, "returned instead of raising")
    except RuntimeError as exc:
        elapsed = time.monotonic() - started
        check("timeout raised near deadline",
              "timed out" in str(exc) and elapsed < 25, f"{elapsed:.1f}s")
    time.sleep(2)  # give --rm a beat
    leftovers = exec_containers()
    check("no container left after timeout", not leftovers, str(leftovers))

    # 6. hardened credential probe (only when the harden-net network exists)
    print("== 6. hardened credential probe")
    has_net = subprocess.run(
        ["docker", "network", "inspect", "launchpad-exec"],
        capture_output=True, timeout=15,
    ).returncode == 0
    if not has_net:
        print("  SKIP — launchpad-exec network not provisioned "
              "(run scripts/setup_exec_docker.sh --harden-net)")
    else:
        settings.studio_exec_forward_aws_credentials = False
        settings.studio_exec_docker_network = "launchpad-exec"
        settings.execute_timeout_s = 60.0
        out = asyncio.run(local_exec.execute_strands_code(CRED_PROBE))
        check("boto3 sees no credentials", "boto3-creds: NONE" in out, out.strip())
        check("IMDS blocked", "imds: BLOCKED" in out, out.strip())
        settings.studio_exec_forward_aws_credentials = True
        settings.studio_exec_docker_network = ""

    # 7. final orphan sweep
    print("== 7. orphan sweep")
    leftovers = exec_containers()
    check("no strands-exec-* containers remain", not leftovers, str(leftovers))

    failed = [name for name, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed"
          + (f" — FAILED: {', '.join(failed)}" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
