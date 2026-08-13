"""SigV4-signed POSTs to an AgentCore Gateway HTTP endpoint.

A gateway front-doors a target-based A/B test: a SigV4-signed POST to
``{gateway_url}/{target}/invocations`` is routed to a variant by weight, sticky
per ``X-Amzn-Bedrock-AgentCore-Runtime-Session-Id``. Shared by the experiment
traffic seed (``optimization.service``) and the canary invoke route.
"""

import json
from typing import Any

import httpx
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest

from app.services.workspace import WorkspaceContext

SESSION_HEADER = "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id"


def _default_signer(creds: Any, region: str, aws_request: AWSRequest) -> None:
    SigV4Auth(creds, "bedrock-agentcore", region).add_auth(aws_request)


def sigv4_post(
    url: str,
    json_body: dict[str, Any],
    workspace: WorkspaceContext,
    *,
    session_id: str | None = None,
    poster: Any = None,
    signer: Any = None,
    timeout: float = 120,
) -> Any:
    """SigV4-sign (service ``bedrock-agentcore``) and POST ``json_body`` to ``url``.

    Returns the raw HTTP response (callers inspect ``.status_code`` / body). When
    ``session_id`` is set, the sticky ``X-Amzn-Bedrock-AgentCore-Runtime-Session-Id``
    header pins the A/B variant for that session. ``poster``/``signer`` are test
    injection seams — no real AWS or network when both are supplied.
    """
    credentials = workspace.credentials()
    signer = signer or _default_signer

    body = json.dumps(json_body)
    headers = {"Content-Type": "application/json"}
    if session_id:
        headers[SESSION_HEADER] = session_id
    aws_request = AWSRequest(method="POST", url=url, data=body, headers=headers)
    signer(credentials, workspace.region, aws_request)

    signed_headers = dict(aws_request.headers)
    if poster:
        return poster(url, body, signed_headers)
    with httpx.Client(timeout=timeout) as client:
        return client.post(url, content=body, headers=signed_headers)
