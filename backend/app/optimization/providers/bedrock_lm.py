"""Bedrock ConverseStream as a plain text-completion callable for the providers.

Built through the workspace client funnel (``WorkspaceContext.client``), never a
bare boto3 client — the guard test in ``tests/test_client_funnel.py`` fails on
construction anywhere else. No ``temperature``: the current Claude 5 and GPT-5.6
inference profiles reject the field outright (live-verified 2026-09-03), and a
reflection call has no need for it.

Streaming rather than one-shot Converse, on purpose: a reflection over 30
sessions on Claude Opus 5 takes minutes, and the one-shot call sat behind
botocore's 60 s read timeout with five silent re-sends of the whole prompt
before surfacing ``ReadTimeoutError`` (prod, 2026-09-03). With the stream the
read timeout only bounds the silence between chunks; the standard retry mode
still covers throttling and dropped connections, and a retry now means the
model stalled, not that it was merely slow.
"""

from __future__ import annotations

from typing import Any

from botocore.config import Config

from app.core.config import get_settings
from app.services.workspace import WorkspaceContext


def runtime_client(workspace: WorkspaceContext) -> Any:
    """The providers' ``bedrock-runtime`` client: long read timeout, standard retries.

    ``cache_token`` keeps it on the funnel's cached path despite the per-settings
    Config (same pattern as ``agentcore.client.data_client``).
    """
    timeout = get_settings().prompt_opt_read_timeout_s
    return workspace.client(
        "bedrock-runtime",
        cache_token=f"read_timeout={timeout};max_attempts=5",
        config=Config(read_timeout=timeout, retries={"max_attempts": 5, "mode": "standard"}),
    )


def converse_text(
    workspace: WorkspaceContext,
    model_id: str,
    system: str,
    messages: list[dict[str, str]],
    max_tokens: int,
    client: Any = None,
) -> tuple[str, dict[str, Any]]:
    """One ConverseStream round → (concatenated text, usage summary).

    ``messages`` are ``[{"role": "user"|"assistant", "text": ...}]``. Only text
    deltas are collected — reasoning blocks the model may stream first are not
    part of the answer. Errors propagate (``botocore.exceptions.ClientError``,
    including the mid-stream ``EventStreamError``) so the provider can turn them
    into a FAILED result with the AWS error code.
    """
    rt = client or runtime_client(workspace)
    resp = rt.converse_stream(
        modelId=model_id,
        system=[{"text": system}],
        messages=[
            {"role": m["role"], "content": [{"text": m["text"]}]} for m in messages
        ],
        inferenceConfig={"maxTokens": max_tokens},
    )
    parts: list[str] = []
    stop_reason: str | None = None
    usage: dict[str, Any] = {}
    for event in resp.get("stream") or []:
        if not isinstance(event, dict):
            continue
        delta = (event.get("contentBlockDelta") or {}).get("delta") or {}
        if isinstance(delta.get("text"), str):
            parts.append(delta["text"])
        if "messageStop" in event:
            stop_reason = (event["messageStop"] or {}).get("stopReason")
        if "metadata" in event:
            usage = (event["metadata"] or {}).get("usage") or usage
    return "".join(parts), {
        "input_tokens": usage.get("inputTokens"),
        "output_tokens": usage.get("outputTokens"),
        "stop_reason": stop_reason,
    }


def converse_for(workspace: WorkspaceContext, client: Any = None):
    """Bind ``converse_text`` to a workspace as the providers' ``ConverseFn``."""

    def _fn(
        model_id: str, system: str, messages: list[dict[str, str]], max_tokens: int
    ) -> tuple[str, dict[str, Any]]:
        return converse_text(workspace, model_id, system, messages, max_tokens, client)

    return _fn
