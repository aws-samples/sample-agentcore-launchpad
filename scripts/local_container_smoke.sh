#!/usr/bin/env bash
# Local ARM64 smoke test for the Claude SDK container template.
# Builds the image natively (host is aarch64), runs it with the host's AWS
# credentials, and asserts a Bedrock-backed answer — the pre-CodeBuild gate.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CTX=/tmp/claude_sdk_smoke_ctx
NAME=launchpad-claude-smoke
PORT="${SMOKE_PORT:-18080}"

echo "── rendering build context"
(cd "$ROOT/backend" && uv run python - <<PY
from pathlib import Path
from app.schemas.agent import AgentSpec
from app.templates.claude_sdk_agent import assemble_build_context

spec = AgentSpec(
    name="local-smoke",
    method="container",
    system_prompt="You are a terse math assistant. Answer with just the number.",
)
assemble_build_context(spec, Path("$CTX"))
print("context:", sorted(p.name for p in Path("$CTX").iterdir()))
PY
)

echo "── docker build (linux/arm64)"
docker build --platform linux/arm64 -t "$NAME" "$CTX"

echo "── docker run"
docker rm -f "$NAME" >/dev/null 2>&1 || true
ENVFILE=
cleanup() {
  [[ -z "$ENVFILE" ]] || rm -f "$ENVFILE"
  docker rm -f "$NAME" >/dev/null 2>&1 || true
}
trap cleanup EXIT

ENVFILE=$(mktemp)
aws configure export-credentials --format env-no-export > "$ENVFILE"
echo "AWS_REGION=${AWS_REGION:-us-west-2}" >> "$ENVFILE"
docker run -d --name "$NAME" --env-file "$ENVFILE" -p "$PORT:8080" "$NAME" >/dev/null
rm -f "$ENVFILE"
ENVFILE=

echo "── waiting for /ping"
for _ in $(seq 1 30); do
  curl -sf "localhost:$PORT/ping" >/dev/null 2>&1 && break
  sleep 2
done
curl -sf "localhost:$PORT/ping" >/dev/null || { echo "container never became healthy"; docker logs "$NAME" | tail -20; exit 1; }

echo "── checking optional subagent scaffold in image"
docker exec "$NAME" sh -c \
  'test ! -d /app/.claude/agents || ls /app/.claude/agents/'

echo "── invoking: what is 2+2?"
ANSWER=$(curl -sf -X POST "localhost:$PORT/invocations" \
  -H 'Content-Type: application/json' \
  -d '{"prompt": "What is 2+2? Reply with just the number."}')
echo "response: $ANSWER"
echo "$ANSWER" | grep -q 4 || { echo "SMOKE FAIL: expected '4' in answer"; exit 1; }
echo "LOCAL CONTAINER SMOKE: PASS"
