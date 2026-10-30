"""Partition-aware URLs of an AgentCore Browser session (N15-obs17).

`bedrock_agentcore.tools.BrowserClient` 1.17.0 builds the hosts of its two signed URLs —
`generate_ws_headers()` (the CDP automation stream) and `generate_live_view_url()` (the DCV
live view) — from a Global-only suffix, so in `aws-cn` both dial a host that does not
resolve (`getaddrinfo ENOTFOUND`). The data-plane boto3 client the SDK constructs for the
session's region already resolved the right endpoint, so the hub signs the two URLs itself
from that host — exactly what the Harness builtin
(`templates/harness_builtin_browser.py.tmpl::_stream_endpoint`) does inside the bundle.
The template must render standalone, so it cannot import this module;
`tests/test_browser_stream.py` asserts the two resolve the same stream URL for an aws-cn
client so they cannot drift.

Credentials are passed in (resolved through `aws_clients.get_session` by the caller) —
this module constructs no client and reads no ambient credential itself (C2).
"""

from __future__ import annotations

import datetime
from typing import Any
from urllib.parse import urlparse

from botocore.auth import SigV4Auth, SigV4QueryAuth
from botocore.awsrequest import AWSRequest

SIGNING_SERVICE = "bedrock-agentcore"


def stream_path(identifier: str, session_id: str) -> str:
    """The CDP automation stream of one session (same shape as the SDK's)."""
    return f"/browser-streams/{identifier}/sessions/{session_id}/automation"


def live_view_path(identifier: str, session_id: str) -> str:
    return f"/browser-streams/{identifier}/sessions/{session_id}/live-view"


def data_plane_host(client: Any) -> str:
    """The host of the client's resolved data-plane endpoint — partition-aware by
    construction (botocore's endpoint resolver, never a literal)."""
    host = urlparse(str(client.data_plane_client.meta.endpoint_url)).netloc
    if not host:
        raise RuntimeError("the browser client has no resolved data-plane endpoint")
    return host


def _session_parts(client: Any) -> tuple[str, str, str]:
    identifier, session_id = client.identifier, client.session_id
    if not identifier or not session_id:
        raise RuntimeError("the browser session has not been started")
    return data_plane_host(client), str(identifier), str(session_id)


def stream_endpoint(client: Any, credentials: Any) -> tuple[str, dict[str, str]]:
    """``(wss URL, SigV4 headers)`` for the session's CDP automation stream.

    Mirrors ``BrowserClient.generate_ws_headers`` with the host taken from the client's
    data-plane endpoint; the websocket handshake headers are left to the websocket client
    (Playwright / websockets add their own). ``credentials`` is a botocore credentials
    object (``Session.get_credentials()``) or an already frozen one.
    """
    host, identifier, session_id = _session_parts(client)
    path = stream_path(identifier, session_id)
    frozen = _frozen(credentials)
    request = AWSRequest(
        method="GET",
        url=f"https://{host}{path}",
        headers={
            "host": host,
            "x-amz-date": datetime.datetime.now(datetime.UTC).strftime("%Y%m%dT%H%M%SZ"),
        },
    )
    SigV4Auth(frozen, SIGNING_SERVICE, client.region).add_auth(request)
    headers = {
        "X-Amz-Date": request.headers["x-amz-date"],
        "Authorization": request.headers["Authorization"],
    }
    if frozen.token:
        headers["X-Amz-Security-Token"] = frozen.token
    return f"wss://{host}{path}", headers


def live_view_url(client: Any, credentials: Any, expires: int) -> str:
    """A pre-signed (SigV4 query) URL of the session's DCV live view, valid ``expires``
    seconds — ``BrowserClient.generate_live_view_url`` on the partition-aware host."""
    host, identifier, session_id = _session_parts(client)
    request = AWSRequest(
        method="GET",
        url=f"https://{host}{live_view_path(identifier, session_id)}",
        headers={"host": host},
    )
    SigV4QueryAuth(
        credentials=_frozen(credentials), service_name=SIGNING_SERVICE,
        region_name=client.region, expires=expires,
    ).add_auth(request)
    if not request.url:
        raise RuntimeError("failed to sign the live view url")
    return str(request.url)


def _frozen(credentials: Any) -> Any:
    if credentials is None:
        raise RuntimeError("no AWS credentials to sign the browser stream with")
    freeze = getattr(credentials, "get_frozen_credentials", None)
    return freeze() if callable(freeze) else credentials
