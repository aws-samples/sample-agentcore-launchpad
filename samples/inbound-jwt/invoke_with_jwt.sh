#!/usr/bin/env bash
# Invoke a JWT-inbound AgentCore Runtime with curl.
#
# Usage:
#   AGENT_ARN="arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/my_agent-AbCd123456"
#   REGION=us-west-2
#
#   # (a) machine token — Cognito client_credentials (M2M client):
#   TOKEN=$(curl -s -X POST "https://<domain>.auth.${REGION}.amazoncognito.com/oauth2/token" \
#     -u "${CLIENT_ID}:${CLIENT_SECRET}" \
#     -d "grant_type=client_credentials" | jq -r '.access_token')
#
#   # (b) user token — Cognito USER_PASSWORD_AUTH (no secret client):
#   TOKEN=$(aws cognito-idp initiate-auth --region "$REGION" \
#     --client-id "$CLIENT_ID" --auth-flow USER_PASSWORD_AUTH \
#     --auth-parameters "USERNAME=${USERNAME},PASSWORD=${PASSWORD}" \
#     | jq -r '.AuthenticationResult.AccessToken')
#
#   TOKEN="$TOKEN" AGENT_ARN="$AGENT_ARN" REGION="$REGION" ./invoke_with_jwt.sh "hello"
set -euo pipefail

: "${AGENT_ARN:?set AGENT_ARN to the runtime ARN}"
: "${TOKEN:?set TOKEN to a JWT that the runtime authorizer accepts}"
REGION="${REGION:-us-west-2}"
PROMPT="${1:-Hello! What can you do?}"
# memory actor; the Launchpad console uses <agent id>__<username>
ACTOR_ID="${ACTOR_ID:-inbound-jwt-sample}"

# the session-id header is required; ids must be >= 33 characters
SESSION_ID="${SESSION_ID:-$(python3 -c 'import uuid; print(uuid.uuid4().hex + uuid.uuid4().hex[:8])')}"

# the runtime ARN is URL-encoded into the path
ESCAPED_ARN=$(python3 -c 'import sys, urllib.parse; print(urllib.parse.quote(sys.argv[1], safe=""))' "$AGENT_ARN")

curl -sS -X POST \
  "https://bedrock-agentcore.${REGION}.amazonaws.com/runtimes/${ESCAPED_ARN}/invocations?qualifier=DEFAULT" \
  -H "Authorization: Bearer ${TOKEN}" \
  -H "Content-Type: application/json" \
  -H "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id: ${SESSION_ID}" \
  -d "$(python3 -c 'import json,sys; print(json.dumps({"prompt": sys.argv[1], "actor_id": sys.argv[2]}))' "$PROMPT" "$ACTOR_ID")"
echo
