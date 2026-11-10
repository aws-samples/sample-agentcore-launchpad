"""Lambda entry point of ``launchpad-policy-interceptor[-env]`` (stream B1; design §5.2,
§13.6). One function serves the gateway's REQUEST and RESPONSE slots.

Environment (set by the ``policy_interceptor`` bootstrap stage):

- ``LAUNCHPAD_POLICY_BUCKET`` — the artifacts bucket holding ``policy/gateway/<gw>/…``;
- ``LAUNCHPAD_POLICY_GATEWAY_ID`` — the DEFAULT gateway (``launchpad-gw[-env]``'s id); an
  invocation whose client context names another gateway (``GATEWAY_ARN``) reads THAT
  gateway's bundle (Global model: one engine may be attached to several gateways);
- ``LAUNCHPAD_POLICY_BUNDLE_TTL_S`` — bundle cache seconds (default 30): past it a background
  refresh runs while the cached bundle is served (N17-obs483);
- ``LAUNCHPAD_POLICY_BUNDLE_MAX_STALE_S`` — the staleness bound (default 120): past it a request
  refreshes synchronously and fails closed if that fails.

N17-obs498 (M2 9c p99): the bundle is loaded in the INIT phase (``preload`` at import — the
S3 client build and the first pointer / bundle / key reads move out of the first request's
duration) and a pending refresh is finished during the RESPONSE invocation
(``BundleSource.settle``), never on the REQUEST decision path while traffic flows.

**Never raises** (P0 d: a raised error reaches the agent with the stack trace and the
gateway answers 500). Any internal failure on a REQUEST ``tools/call`` is a JSON-RPC deny
``interceptor_error`` — or a pass when the workspace was already known to be ``log_only`` —
and on a RESPONSE the gateway response passes through unchanged. Nothing is logged but the
I7 lines and fixed-vocabulary diagnostics: never a token, header or argument value.

This module is the interceptor Lambda's only AWS client construction site; it runs with the
interceptor role's ambient credentials, not the hub's (tests/test_client_funnel.py EXEMPT).
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

from . import codec
from .bundle import BundleSource
from .core import (
    Call,
    Deps,
    deny,
    is_response,
    on_request,
    on_response,
    pass_request,
    pass_response,
)
from .core import request_body as _request_body
from .engine_client import EngineClient

# one `Deps` per gateway (Global model: the same function may serve several gateways — each
# reads ITS bundle `policy/gateway/<gw>/…`); the gateway comes from the invocation
# (`GATEWAY_ARN` in the Lambda client context), else `LAUNCHPAD_POLICY_GATEWAY_ID`
_DEPS: dict[str, Deps] = {}


def _audit(line: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(line, separators=(",", ":"), ensure_ascii=False) + "\n")
    sys.stdout.flush()


# review F5 (N17-obs494): the function has a 5 s budget (`policy_interceptor.TIMEOUT_S`); a
# stalled S3 read must become a refresh FAILURE well inside it — the SWR source then serves the
# last bundle until `max_stale` and fails closed after — never the gateway's own timeout / 500
S3_CONNECT_TIMEOUT_S = 0.5
S3_READ_TIMEOUT_S = 1.5
S3_MAX_ATTEMPTS = 1


def s3_client_config():
    """The botocore ``Config`` of the bundle reader (one attempt, sub-second timeouts)."""
    from botocore.config import Config  # the Lambda runtime provides botocore

    return Config(connect_timeout=S3_CONNECT_TIMEOUT_S, read_timeout=S3_READ_TIMEOUT_S,
                  retries={"max_attempts": S3_MAX_ATTEMPTS})


def _s3_fetch():
    import boto3  # the Lambda runtime provides it; never imported by the hub

    client = boto3.client("s3", config=s3_client_config())

    def fetch(bucket: str, key: str) -> bytes:
        return client.get_object(Bucket=bucket, Key=key)["Body"].read()

    return fetch


def _engine(bundle, key: bytes) -> EngineClient:
    return EngineClient(endpoints=bundle.endpoints, shards=bundle.shards, key=key)


def gateway_id_of(gateway_arn: str | None) -> str | None:
    """``arn:…:gateway/<id>`` → ``<id>``; None for anything else."""
    if isinstance(gateway_arn, str) and "gateway/" in gateway_arn:
        return gateway_arn.rsplit("gateway/", 1)[-1] or None
    return None


def deps(gateway_id: str | None = None) -> Deps:
    gateway_id = gateway_id or os.environ["LAUNCHPAD_POLICY_GATEWAY_ID"]
    found = _DEPS.get(gateway_id)
    if found is None:
        source = BundleSource(
            bucket=os.environ["LAUNCHPAD_POLICY_BUCKET"],
            gateway_id=gateway_id, fetch=_s3_fetch(),
            ttl_s=float(os.environ.get("LAUNCHPAD_POLICY_BUNDLE_TTL_S") or 30),
            max_stale_s=float(os.environ.get("LAUNCHPAD_POLICY_BUNDLE_MAX_STALE_S") or 120),
            on_refresh=_audit,  # `bundle_refresh` diagnostic lines (obs498 follow-up)
        )
        found = _DEPS[gateway_id] = Deps(source=source, engine=_engine, audit=_audit)
    return found


def _call(event: Any, context: Any) -> Call:
    custom = getattr(getattr(context, "client_context", None), "custom", None) or {}
    request_id = custom.get("REQUEST_ID") or getattr(context, "aws_request_id", None) or "-"
    return Call(request_id=str(request_id), gateway_arn=custom.get("GATEWAY_ARN"))


def handle(event: Any, context: Any, dependencies: Deps) -> dict[str, Any]:
    """``handler`` with injected dependencies (tests); the never-raise boundary."""
    call = None
    try:
        call = _call(event, context)
        if not isinstance(event, dict):
            raise TypeError("event is not an object")
        if is_response(event):
            return on_response(event, call, dependencies)
        return on_request(event, call, dependencies)
    except Exception as exc:  # never raise: fail closed (or pass in log_only) instead
        try:
            dependencies.audit({"launchpad.policy.interceptor": {
                "event": "internal_error", "error": type(exc).__name__}})
        except Exception:
            pass
        return _fallback(event, call)


def _fallback(event: Any, call: Call | None) -> dict[str, Any]:
    try:
        if isinstance(event, dict) and is_response(event):
            return pass_response(event)
        body = _request_body(event) if isinstance(event, dict) else None
        method = body.get("method") if isinstance(body, dict) else None
        jsonrpc_id = body.get("id") if isinstance(body, dict) else None
        if method != "tools/call" or (call is not None and call.mode == "log_only"):
            return pass_request(body)
        return deny(jsonrpc_id, "interceptor_error", decision_id=codec.decision_id(
            f"req:{getattr(call, 'request_id', '-')}", jsonrpc_id, "final"), stage="final",
            reason_code="interceptor_error")
    except Exception:
        return {"interceptorOutputVersion": "1.0", "mcp": {"transformedGatewayResponse": {
            "statusCode": 200, "body": {"jsonrpc": "2.0", "id": None, "error": {
                "code": -32002, "message": "Tool Execution Denied: Tool call not allowed due "
                "to policy enforcement [interceptor_error]"}}}}}


def handler(event: Any, context: Any) -> dict[str, Any]:
    try:
        dependencies = deps(gateway_id_of(_call(event, context).gateway_arn))
    except Exception:
        return _fallback(event, None)
    return handle(event, context, dependencies)


def preload() -> bool:
    """INIT-phase warm-up (N17-obs498): build the dependencies and load the bundle before the
    first request, only when the Lambda environment is configured; never raises — a failure
    leaves the first request to load synchronously exactly as before."""
    if not (os.environ.get("LAUNCHPAD_POLICY_BUCKET")
            and os.environ.get("LAUNCHPAD_POLICY_GATEWAY_ID")):
        return False
    try:
        deps().source.get()
        return True
    except Exception:  # noqa: BLE001 — the request path reports the real failure
        return False


preload()
