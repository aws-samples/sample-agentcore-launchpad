#!/usr/bin/env python3
"""Invoke a JWT-inbound AgentCore Runtime with a bearer token.

boto3 cannot send a bearer token, so a Runtime deployed with a
``customJWTAuthorizer`` is invoked over the data-plane HTTPS endpoint:

    POST https://bedrock-agentcore.{region}.amazonaws.com
         /runtimes/{urlencoded runtime ARN}/invocations?qualifier=DEFAULT
    Authorization: Bearer <jwt>
    X-Amzn-Bedrock-AgentCore-Runtime-Session-Id: <session id>

Two ways to obtain the token from a Cognito pool:

  machine (client_credentials — the M2M path)
      python invoke_with_jwt.py --agent-arn ARN \
          --client-id ID --client-secret SECRET \
          --token-url https://<domain>.auth.<region>.amazoncognito.com/oauth2/token \
          [--scope launchpad-gw/invoke] --prompt "hello"

  user (USER_PASSWORD_AUTH — a human identity, carries cognito:groups)
      python invoke_with_jwt.py --agent-arn ARN \
          --client-id ID --username USER --password PASS \
          [--region us-west-2] --prompt "hello"

  bring your own token (any OIDC IdP)
      python invoke_with_jwt.py --agent-arn ARN --token "$JWT" --prompt "hello"

Streaming aware: a text/event-stream response is printed delta by delta;
a buffered JSON body is printed at once. Requires: requests, boto3 (only for
USER_PASSWORD_AUTH). No AWS credentials are needed to invoke — the JWT is the
whole authentication.
"""

import argparse
import base64
import json
import sys
import urllib.parse
import uuid

import requests

SESSION_HEADER = "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id"


def m2m_token(token_url: str, client_id: str, client_secret: str, scope: str | None) -> str:
    """client_credentials grant against the IdP token endpoint (Cognito: the
    pool's hosted-UI domain /oauth2/token)."""
    basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    data = {"grant_type": "client_credentials"}
    if scope:
        data["scope"] = scope
    response = requests.post(
        token_url,
        data=data,
        headers={"Authorization": f"Basic {basic}"},
        timeout=10,
    )
    response.raise_for_status()
    return response.json()["access_token"]


def user_token(region: str, client_id: str, username: str, password: str) -> str:
    """USER_PASSWORD_AUTH against Cognito — a user access token whose claims
    (username, cognito:groups) custom-claim rules can match."""
    import boto3

    cognito = boto3.client("cognito-idp", region_name=region)
    result = cognito.initiate_auth(
        ClientId=client_id,
        AuthFlow="USER_PASSWORD_AUTH",
        AuthParameters={"USERNAME": username, "PASSWORD": password},
    )["AuthenticationResult"]
    return result["AccessToken"]


def invoke(
    agent_arn: str, region: str, token: str, prompt: str, session_id: str, actor_id: str
) -> int:
    escaped = urllib.parse.quote(agent_arn, safe="")
    url = (
        f"https://bedrock-agentcore.{region}.amazonaws.com"
        f"/runtimes/{escaped}/invocations?qualifier=DEFAULT"
    )
    response = requests.post(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            SESSION_HEADER: session_id,
        },
        json={"prompt": prompt, "actor_id": actor_id},
        stream=True,
        timeout=(10, 900),
    )
    print(f"HTTP {response.status_code}", file=sys.stderr)
    if response.status_code != 200:
        print(response.text[:2000], file=sys.stderr)
        return 1

    content_type = response.headers.get("content-type", "")
    if "text/event-stream" in content_type:
        # Print deltas as they arrive. Launchpad runtimes also emit a final
        # {"event": "complete", "result": <full text>} — only print it when no
        # deltas were streamed, or the answer would appear twice.
        saw_delta = False
        for line in response.iter_lines(decode_unicode=True):
            if not line or not line.startswith("data:"):
                continue
            try:
                event = json.loads(line[len("data:"):].strip())
            except ValueError:
                continue
            if isinstance(event, dict) and event.get("event") == "delta":
                saw_delta = True
                print(event.get("text", ""), end="", flush=True)
            elif (isinstance(event, dict) and event.get("event") == "complete"
                  and not saw_delta):
                print(event.get("result", ""), end="", flush=True)
        print()
    else:
        body = response.json()
        text = body.get("result") if isinstance(body, dict) else None
        print(text if isinstance(text, str) else json.dumps(body, ensure_ascii=False, indent=2))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--agent-arn", required=True, help="AgentCore Runtime ARN")
    parser.add_argument("--region", default="us-west-2")
    parser.add_argument("--prompt", default="Hello! What can you do?")
    parser.add_argument("--session-id", default=None,
                        help="runtime session id (>= 33 chars; default: random)")
    parser.add_argument("--actor-id", default="inbound-jwt-sample",
                        help="memory actor; Launchpad's console uses <agent id>__<username>, "
                             "so pass that to share the console user's memory")
    token_source = parser.add_argument_group("token source (pick one)")
    token_source.add_argument("--token", help="a ready-made JWT (any OIDC IdP)")
    token_source.add_argument("--client-id", help="IdP app client id")
    token_source.add_argument("--client-secret", help="client_credentials secret")
    token_source.add_argument("--token-url",
                              help="client_credentials token endpoint "
                                   "(Cognito: https://<domain>.auth.<region>"
                                   ".amazoncognito.com/oauth2/token)")
    token_source.add_argument("--scope", help="optional client_credentials scope")
    token_source.add_argument("--username", help="USER_PASSWORD_AUTH username")
    token_source.add_argument("--password", help="USER_PASSWORD_AUTH password")
    args = parser.parse_args()

    if args.token:
        token = args.token
    elif args.client_secret:
        if not (args.client_id and args.token_url):
            parser.error("client_credentials needs --client-id and --token-url")
        token = m2m_token(args.token_url, args.client_id, args.client_secret, args.scope)
    elif args.username:
        if not (args.client_id and args.password):
            parser.error("USER_PASSWORD_AUTH needs --client-id and --password")
        token = user_token(args.region, args.client_id, args.username, args.password)
    else:
        parser.error("provide --token, or --client-secret (M2M), or --username (user)")
        return 2

    session_id = args.session_id or uuid.uuid4().hex + uuid.uuid4().hex[:8]
    return invoke(args.agent_arn, args.region, token, args.prompt, session_id, args.actor_id)


if __name__ == "__main__":
    raise SystemExit(main())
