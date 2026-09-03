"""Bedrock Converse as a plain text-completion callable for the providers.

Built through the workspace client funnel (``WorkspaceContext.client``), never a
bare boto3 client — the guard test in ``tests/test_client_funnel.py`` fails on
construction anywhere else. No ``temperature``: the current Claude 5 and GPT-5.6
inference profiles reject the field outright (live-verified 2026-09-03), and a
reflection call has no need for it.
"""

from __future__ import annotations

from typing import Any

from app.services.workspace import WorkspaceContext


def converse_text(
    workspace: WorkspaceContext,
    model_id: str,
    system: str,
    messages: list[dict[str, str]],
    max_tokens: int,
    client: Any = None,
) -> tuple[str, dict[str, Any]]:
    """One Converse round → (concatenated text, usage summary).

    ``messages`` are ``[{"role": "user"|"assistant", "text": ...}]``. Errors
    propagate (``botocore.exceptions.ClientError`` for AWS-side failures) so the
    provider can turn them into a FAILED result with the AWS error code.
    """
    rt = client or workspace.client("bedrock-runtime")
    resp = rt.converse(
        modelId=model_id,
        system=[{"text": system}],
        messages=[
            {"role": m["role"], "content": [{"text": m["text"]}]} for m in messages
        ],
        inferenceConfig={"maxTokens": max_tokens},
    )
    content = ((resp.get("output") or {}).get("message") or {}).get("content") or []
    text = "".join(block.get("text", "") for block in content if isinstance(block, dict))
    usage = resp.get("usage") or {}
    return text, {
        "input_tokens": usage.get("inputTokens"),
        "output_tokens": usage.get("outputTokens"),
        "stop_reason": resp.get("stopReason"),
    }


def converse_for(workspace: WorkspaceContext, client: Any = None):
    """Bind ``converse_text`` to a workspace as the providers' ``ConverseFn``."""

    def _fn(
        model_id: str, system: str, messages: list[dict[str, str]], max_tokens: int
    ) -> tuple[str, dict[str, Any]]:
        return converse_text(workspace, model_id, system, messages, max_tokens, client)

    return _fn
