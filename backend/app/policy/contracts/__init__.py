"""Frozen interface contracts of the per-user principal + Gateway interceptor + Dogwood
engine work (stage 0 of plans/DESIGN-POLICY-USER-PRINCIPAL.md §14/§15; normative text:
docs/cn/interfaces/policy-principal-contracts.md).

This package holds DATA only — the JSON Schemas of I1-I8 and the constants every producer
and consumer must agree on — plus ``codec`` (stdlib + ``cryptography``), the one reference
implementation of the token wire format, the session key / psid derivation and the engine
request signature. Nothing here is imported by a request path while
``Settings.policy_gateway_interceptor`` is off; streams A (hub), B (interceptor Lambda,
which vendors ``codec.py``) and C (engine) build against these files, never against each
other's code.
"""

from __future__ import annotations

import json
from functools import cache
from pathlib import Path
from typing import Any

CONTRACT_VERSION = 1
_HERE = Path(__file__).resolve().parent

# ── I1 principal token ────────────────────────────────────────────────────
TOKEN_TYP = "lp-principal+jws"
TOKEN_ALG = "ES256"
TOKEN_ISSUER = "launchpad-hub"
# D4: platform templates 15 min (= the synchronous InvokeAgentRuntime limit), BYOC 5 min
TOKEN_TTL_S = 900
TOKEN_TTL_BYOC_S = 300
TOKEN_CLOCK_SKEW_S = 60
CHANNELS = ("chat", "console", "v1", "assistant", "eval", "canary", "system")
PRINCIPAL_KINDS = ("user", "api_key", "system")

# ── I2 carrier ────────────────────────────────────────────────────────────
META_KEY = "launchpad/principal"
# Global's header (policy-session-based-temporal.html); informational here — the gateway
# runs with passRequestHeaders=false (P0 b), the authoritative psid rides in the token.
POLICY_SESSION_HEADER = "x-amzn-bedrock-agentcore-policy-session-id"
PSID_PATTERN = r"[A-Za-z0-9-]{1,128}"

# amendment 3: the interceptor reads the pointer, then the immutable v<n>.json it names;
# the engine HMAC key is one shared S3 object of exactly 32 raw bytes, created once (by the
# interceptor bootstrap stage, IfNoneMatch) and installed byte-for-byte on the engine host
BUNDLE_POINTER_KEY = "policy/gateway/{gateway_id}/current.json"
BUNDLE_VERSION_KEY = "policy/gateway/{gateway_id}/v{version}.json"
# D5: eval / canary principal tokens of one job (all attempts), in the artifacts bucket
EVAL_TOKENS_KEY = "evaluations/{job_id}/tokens.json"
EVAL_TOKENS_REFRESH_S = 300
ENGINE_KEY_S3_KEY = "policy/gateway/{gateway_id}/engine-key"
ENGINE_KEY_BYTES = 32
# the hub mirrors the engines' local audit JSONL here (amendment 3)
ENGINE_AUDIT_LOG_GROUP = "/launchpad/policy/engine"

# ── I4 engine data plane ──────────────────────────────────────────────────
ENGINE_SHARD_PORTS = (7443, 7444)
HDR_TS = "X-LP-Ts"
HDR_NONCE = "X-LP-Nonce"
HDR_SIG = "X-LP-Sig"
ENGINE_SIG_SKEW_S = 60
ENGINE_NONCE_TTL_S = 300
ENGINE_ERROR_CODES = (
    "session_invalidated", "history_lost", "set_mismatch", "bad_request", "unauthorized",
    "unavailable",
)
# client timeouts (§13.5 latency budget)
ENGINE_STATELESS_TIMEOUT_S = 0.100
ENGINE_STATELESS_RETRY_TIMEOUT_S = 0.050
ENGINE_OWNER_TIMEOUT_S = 1.500

# ── I5 engine control plane ───────────────────────────────────────────────
# one control socket per shard process (amendment 2); the hub installs on every shard
ENGINE_CONTROL_SOCKET = "/run/launchpad-policy-engine/control-{shard}.sock"
# the engine's own audit lines (I7 `launchpad.policy.engine`) go to a local JSONL per shard
# on the hub host (amendment 2), read by the hub ingester beside the Lambda's CloudWatch lines
ENGINE_AUDIT_FILE = "/var/lib/launchpad-policy-engine/audit/shard-{shard}.jsonl"

# ── I8 JSON-RPC shapes (Global's exact denial text; policy_denials.parse_tool_denial) ──
JSONRPC_DENY_CODE = -32002
JSONRPC_SESSION_REQUIRED_CODE = -32602
DENY_PREFIX = "Tool Execution Denied: Tool call not allowed due to policy enforcement"
DEFAULT_DENY_REASON = "No policy applies to the request (denied by default)."
SESSION_REQUIRED_MESSAGE = "policy_session_required"
CONFLICT_MESSAGE = (
    "ConflictException: the policy session was invalidated by a policy change; "
    "start a new session"
)

# ── Dogwood vocabulary (D8, §13.3) ────────────────────────────────────────
ACTION_NAMESPACE = "AgentCore::Action"
GATEWAY_ENTITY = "AgentCore::Gateway"
IAM_ENTITY = "AgentCore::IamEntity"
TARGET_GROUP_PREFIX = "target:"

SCHEMAS = (
    "principal_token_header",
    "principal_token_claims",
    "gateway_bundle",
    "gateway_bundle_pointer",
    "eval_tokens",
    "engine_decide_request",
    "engine_decide_response",
    "engine_respond_request",
    "engine_respond_response",
    "engine_list_filter_request",
    "engine_list_filter_response",
    "engine_healthz",
    "engine_error",
    "engine_control_request",
    "engine_control_response",
    "audit_decision_line",
    "audit_engine_line",
    "jsonrpc_policy_error",
    "runtime_policy_denied_frame",
)


@cache
def load_schema(name: str) -> dict[str, Any]:
    """One contract schema (JSON Schema draft 2020-12) by its stem."""
    if name not in SCHEMAS:
        raise KeyError(f"unknown contract schema {name!r}")
    return json.loads((_HERE / f"{name}.schema.json").read_text(encoding="utf-8"))


def deny_message(reason: str) -> str:
    """I8 deny text — byte-for-byte Global's signature."""
    return f"{DENY_PREFIX} [{reason}]"
