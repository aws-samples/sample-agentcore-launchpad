"""Mantle (Bedrock OpenAI-compatible gateway) chat wrapper.

The ``bedrock-mantle.<region>.api.aws/openai/v1`` endpoint serves
``openai.gpt*`` models exclusively through the Responses API —
``/v1/chat/completions`` returns 400 ``validation_error`` for them
(verified 2026-07-08; the older CloudFront mantle front accepted both).

``azure_openai.py`` stays untouched by design: this module wraps its
clients, converters and token tracker, forcing ``client.responses.create``
for the model/endpoint combinations that need it and delegating everything
else back to :mod:`skillopt.model.azure_openai` unchanged.  Dispatch lives
in :mod:`skillopt.model` (``__init__``): when a role's endpoint looks like
a mantle host, the role's chat calls route here instead of azure_openai.
"""
from __future__ import annotations

import time
from typing import Any

from skillopt.model import azure_openai as _az

# Endpoint host substrings that identify the Bedrock mantle gateway.
_MANTLE_HOST_MARKERS = ("bedrock-mantle",)

# Model-name prefixes that must use the Responses API on mantle.  A match
# requires the prefix to be followed by nothing or a separator, so
# "openai.gpt-5.5" and "openai.gpt5" match but "openai.gptx" does not.
_RESPONSES_MODEL_PREFIXES = ("openai.gpt",)


def is_mantle_endpoint(endpoint: str | None) -> bool:
    normalized = str(endpoint or "").strip().lower()
    return any(marker in normalized for marker in _MANTLE_HOST_MARKERS)


def uses_mantle(role: str) -> bool:
    """True when *role*'s configured endpoint is a mantle gateway."""
    try:
        return is_mantle_endpoint(_az._role_config(role)["endpoint"])
    except ValueError:
        return False


def _model_needs_responses(model: str) -> bool:
    normalized = str(model or "").strip().lower()
    for prefix in _RESPONSES_MODEL_PREFIXES:
        if normalized.startswith(prefix):
            rest = normalized[len(prefix):]
            if rest == "" or not rest[0].isalpha():
                return True
    return False


def needs_responses_api_for(deployment: str, role: str = "target") -> bool:
    """Mantle-aware Responses-API predicate for direct-client callers.

    ORs azure_openai's stock model list with the mantle endpoint rule, so
    modules that build their own requests (e.g. spreadsheetbench's codegen
    agent) route correctly on both real Azure/OpenAI and mantle.
    """
    if _az._needs_responses_api(deployment):
        return True
    return uses_mantle(role) and _model_needs_responses(deployment)


# ── Responses-API call implementations ───────────────────────────────────────
# Mirrors azure_openai._chat_impl / _chat_messages_impl's Responses branches,
# reusing its converters; kept separate so azure_openai.py stays unmodified.


def _responses_kwargs_base(
    deployment: str,
    max_completion_tokens: int,
    reasoning_effort: str | None,
    timeout: int | None,
) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "model": deployment,
        "max_output_tokens": max_completion_tokens,
    }
    actual_effort = reasoning_effort or _az.REASONING_EFFORT
    if actual_effort:
        kwargs["reasoning"] = {"effort": actual_effort}
    if timeout is not None:
        kwargs["timeout"] = timeout
    return kwargs


def _responses_usage(resp: Any) -> dict:
    if not (hasattr(resp, "usage") and resp.usage):
        return {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    prompt = getattr(resp.usage, "input_tokens", 0) or 0
    completion = getattr(resp.usage, "output_tokens", 0) or 0
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": prompt + completion,
    }


def _chat_impl(
    client: Any,
    deployment: str,
    system: str,
    user: str,
    max_completion_tokens: int,
    retries: int,
    stage: str,
    reasoning_effort: str | None = None,
    timeout: int | None = None,
) -> tuple[str, dict]:
    last_err = None
    for attempt in range(retries):
        try:
            kwargs = _responses_kwargs_base(
                deployment, max_completion_tokens, reasoning_effort, timeout
            )
            kwargs["instructions"] = system
            kwargs["input"] = [{"role": "user", "content": user}]
            resp = client.responses.create(**kwargs)
            text = getattr(resp, "output_text", None) or ""
            if not text:
                for item in getattr(resp, "output", None) or []:
                    for part in getattr(item, "content", []) or []:
                        if getattr(part, "type", "") == "output_text":
                            text += getattr(part, "text", "") or ""
            usage_info = _responses_usage(resp)
            _az.tracker.record(
                stage, usage_info["prompt_tokens"], usage_info["completion_tokens"]
            )
            return text, usage_info
        except Exception as e:  # noqa: BLE001
            last_err = e
            time.sleep(min(2 ** attempt, 30))
    raise RuntimeError(f"LLM call failed after {retries} retries: {last_err}")


def _chat_messages_impl(
    client: Any,
    deployment: str,
    messages: list[dict[str, Any]],
    max_completion_tokens: int,
    retries: int,
    stage: str,
    reasoning_effort: str | None = None,
    *,
    tools: list[dict[str, Any]] | None = None,
    tool_choice: str | dict[str, Any] | None = None,
    return_message: bool = False,
    timeout: int | None = None,
) -> tuple[Any, dict]:
    last_err = None
    for attempt in range(retries):
        try:
            input_items, instructions = _az._messages_to_responses_input(messages)
            kwargs = _responses_kwargs_base(
                deployment, max_completion_tokens, reasoning_effort, timeout
            )
            kwargs["input"] = input_items
            if instructions:
                kwargs["instructions"] = instructions
            if tools:
                kwargs["tools"] = [_az._chat_tool_to_responses_tool(t) for t in tools]
                if tool_choice is not None:
                    kwargs["tool_choice"] = tool_choice
            resp = client.responses.create(**kwargs)
            message, text = _az._responses_to_chat_message(resp)
            usage_info = _responses_usage(resp)
            _az.tracker.record(
                stage, usage_info["prompt_tokens"], usage_info["completion_tokens"]
            )
            return (message if return_message else text), usage_info
        except Exception as e:  # noqa: BLE001
            last_err = e
            time.sleep(min(2 ** attempt, 30))
    raise RuntimeError(f"LLM message call failed after {retries} retries: {last_err}")


# ── Public API (signature-compatible with azure_openai) ──────────────────────


def chat_optimizer(
    system: str,
    user: str,
    max_completion_tokens: int = 16384,
    retries: int = 5,
    stage: str = "optimizer",
    reasoning_effort: str | None = None,
    timeout: int | None = None,
) -> tuple[str, dict]:
    if not _model_needs_responses(_az.OPTIMIZER_DEPLOYMENT):
        return _az.chat_optimizer(
            system, user, max_completion_tokens, retries, stage, reasoning_effort, timeout
        )
    return _chat_impl(
        _az.get_optimizer_client(), _az.OPTIMIZER_DEPLOYMENT,
        system, user, max_completion_tokens, retries, stage, reasoning_effort, timeout,
    )


def chat_target(
    system: str,
    user: str,
    max_completion_tokens: int = 16384,
    retries: int = 5,
    stage: str = "target",
    reasoning_effort: str | None = None,
    timeout: int | None = None,
) -> tuple[str, dict]:
    if not _model_needs_responses(_az.TARGET_DEPLOYMENT):
        return _az.chat_target(
            system, user, max_completion_tokens, retries, stage, reasoning_effort, timeout
        )
    return _chat_impl(
        _az.get_target_client(), _az.TARGET_DEPLOYMENT,
        system, user, max_completion_tokens, retries, stage, reasoning_effort, timeout,
    )


def chat_with_deployment(
    deployment: str,
    system: str,
    user: str,
    max_completion_tokens: int = 16384,
    retries: int = 5,
    stage: str = "custom",
    reasoning_effort: str | None = None,
    timeout: int | None = None,
) -> tuple[str, dict]:
    if not _model_needs_responses(deployment):
        return _az.chat_with_deployment(
            deployment, system, user, max_completion_tokens, retries, stage,
            reasoning_effort, timeout,
        )
    return _chat_impl(
        _az.get_optimizer_client(), deployment,
        system, user, max_completion_tokens, retries, stage, reasoning_effort, timeout,
    )


def chat_optimizer_messages(
    messages: list[dict[str, Any]],
    max_completion_tokens: int = 16384,
    retries: int = 5,
    stage: str = "optimizer",
    reasoning_effort: str | None = None,
    *,
    tools: list[dict[str, Any]] | None = None,
    tool_choice: str | dict[str, Any] | None = None,
    return_message: bool = False,
    timeout: int | None = None,
) -> tuple[Any, dict]:
    if not _model_needs_responses(_az.OPTIMIZER_DEPLOYMENT):
        return _az.chat_optimizer_messages(
            messages, max_completion_tokens, retries, stage, reasoning_effort,
            tools=tools, tool_choice=tool_choice,
            return_message=return_message, timeout=timeout,
        )
    return _chat_messages_impl(
        _az.get_optimizer_client(), _az.OPTIMIZER_DEPLOYMENT,
        messages, max_completion_tokens, retries, stage, reasoning_effort,
        tools=tools, tool_choice=tool_choice,
        return_message=return_message, timeout=timeout,
    )


def chat_target_messages(
    messages: list[dict[str, Any]],
    max_completion_tokens: int = 16384,
    retries: int = 5,
    stage: str = "target",
    reasoning_effort: str | None = None,
    *,
    tools: list[dict[str, Any]] | None = None,
    tool_choice: str | dict[str, Any] | None = None,
    return_message: bool = False,
    timeout: int | None = None,
) -> tuple[Any, dict]:
    if not _model_needs_responses(_az.TARGET_DEPLOYMENT):
        return _az.chat_target_messages(
            messages, max_completion_tokens, retries, stage, reasoning_effort,
            tools=tools, tool_choice=tool_choice,
            return_message=return_message, timeout=timeout,
        )
    return _chat_messages_impl(
        _az.get_target_client(), _az.TARGET_DEPLOYMENT,
        messages, max_completion_tokens, retries, stage, reasoning_effort,
        tools=tools, tool_choice=tool_choice,
        return_message=return_message, timeout=timeout,
    )


def chat_messages_with_deployment(
    deployment: str,
    messages: list[dict[str, Any]],
    max_completion_tokens: int = 16384,
    retries: int = 5,
    stage: str = "custom",
    reasoning_effort: str | None = None,
    *,
    tools: list[dict[str, Any]] | None = None,
    tool_choice: str | dict[str, Any] | None = None,
    return_message: bool = False,
    timeout: int | None = None,
) -> tuple[Any, dict]:
    if not _model_needs_responses(deployment):
        return _az.chat_messages_with_deployment(
            deployment, messages, max_completion_tokens, retries, stage,
            reasoning_effort, tools=tools, tool_choice=tool_choice,
            return_message=return_message, timeout=timeout,
        )
    return _chat_messages_impl(
        _az.get_optimizer_client(), deployment,
        messages, max_completion_tokens, retries, stage, reasoning_effort,
        tools=tools, tool_choice=tool_choice,
        return_message=return_message, timeout=timeout,
    )
