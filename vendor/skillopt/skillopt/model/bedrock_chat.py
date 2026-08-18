"""Bedrock-native chat backend (Converse API) for optimizer and target paths.

LAUNCHPAD PATCH (see LAUNCHPAD_DEVIATIONS.md): this module does not exist upstream.
It gives the optimizer role (reflection/patch generation and the skilleval chat
judge) a zero-key path to Bedrock: boto3's default credential chain (instance
role / env / profile) with a plain ``converse`` call. No endpoint, bearer token,
or CLI dependency. Mirrors qwen_backend.py's surface so model/__init__.py can
dispatch to it like any other chat backend.

boto3 is imported lazily at call time so importing skillopt.model stays possible
without boto3 installed (only exec-runner and bedrock_chat paths need it).
"""
from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from typing import Any

from skillopt.model.common import TokenTracker, default_model_for_backend

_RETRYABLE_ERROR_CODES = {
    "ThrottlingException",
    "TooManyRequestsException",
    "ServiceUnavailableException",
    "ModelTimeoutException",
    "InternalServerException",
    "ModelNotReadyException",
}


@dataclass
class BedrockChatConfig:
    region: str
    deployment: str
    timeout_seconds: float
    temperature: float | None


def _parse_optional_float(value: Any) -> float | None:
    if value is None:
        return None
    raw = str(value).strip()
    return float(raw) if raw else None


def _role_env(role: str, key: str, default: str) -> str:
    role_key = f"{role.upper()}_BEDROCK_CHAT_{key}"
    generic_key = f"BEDROCK_CHAT_{key}"
    return os.environ.get(role_key) or os.environ.get(generic_key) or default


def _initial_config(role: str) -> BedrockChatConfig:
    deployment_env = "OPTIMIZER_DEPLOYMENT" if role == "optimizer" else "TARGET_DEPLOYMENT"
    return BedrockChatConfig(
        region=_role_env(role, "REGION", os.environ.get("AWS_REGION") or "us-west-2"),
        deployment=(
            os.environ.get(f"{role.upper()}_BEDROCK_CHAT_MODEL")
            or os.environ.get("BEDROCK_CHAT_MODEL")
            or os.environ.get(deployment_env)
            or default_model_for_backend("bedrock_chat")
        ),
        timeout_seconds=float(_role_env(role, "TIMEOUT_SECONDS", "300") or 300),
        temperature=_parse_optional_float(_role_env(role, "TEMPERATURE", "")),
    )


OPTIMIZER_CONFIG = _initial_config("optimizer")
TARGET_CONFIG = _initial_config("target")

_config_lock = threading.Lock()
_client_lock = threading.Lock()
_clients: dict[tuple[str, float], Any] = {}
tracker = TokenTracker()


def _client(region: str, timeout_seconds: float) -> Any:
    """Return a cached bedrock-runtime client for (region, timeout)."""
    key = (region, timeout_seconds)
    with _client_lock:
        cached = _clients.get(key)
        if cached is not None:
            return cached
        try:
            import boto3
            from botocore.config import Config
        except ImportError as exc:  # pragma: no cover - env dependent
            raise RuntimeError(
                "bedrock_chat backend requires boto3 (pip install boto3)"
            ) from exc
        client = boto3.client(
            "bedrock-runtime",
            region_name=region,
            config=Config(
                connect_timeout=30,
                read_timeout=timeout_seconds,
                retries={"max_attempts": 1},
            ),
        )
        _clients[key] = client
        return client


def _split_messages(messages: list[dict[str, Any]]) -> tuple[list[dict], list[dict]]:
    """Convert chat-completions-style messages into Converse system + messages."""
    system: list[dict] = []
    converse: list[dict] = []
    for message in messages:
        role = str(message.get("role") or "user")
        content = message.get("content")
        if isinstance(content, list):
            text = "\n".join(
                str(part.get("text", "")) if isinstance(part, dict) else str(part)
                for part in content
            )
        else:
            text = str(content or "")
        if role == "system":
            if text:
                system.append({"text": text})
            continue
        if role not in {"user", "assistant"}:
            role = "user"
        converse.append({"role": role, "content": [{"text": text}]})
    return system, converse


def _error_code(exc: Exception) -> str:
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        return str((response.get("Error") or {}).get("Code") or "")
    return ""


def _chat_messages_impl(
    messages: list[dict[str, Any]],
    max_completion_tokens: int,
    retries: int,
    stage: str,
    *,
    role: str,
    tools: list[dict[str, Any]] | None = None,
    tool_choice: str | dict[str, Any] | None = None,
    return_message: bool = False,
    timeout: float | None = None,
) -> tuple[str, dict[str, int]]:
    if tools or tool_choice or return_message:
        raise NotImplementedError(
            "bedrock_chat does not support tools/tool_choice/return_message "
            "(no optimizer-role caller uses them; see LAUNCHPAD_DEVIATIONS.md)"
        )
    config = OPTIMIZER_CONFIG if role == "optimizer" else TARGET_CONFIG
    timeout_s = float(timeout) if timeout else config.timeout_seconds
    system, converse_messages = _split_messages(messages)
    if not converse_messages:
        raise ValueError("bedrock_chat requires at least one non-system message")
    inference: dict[str, Any] = {"maxTokens": int(max_completion_tokens)}
    if config.temperature is not None:
        inference["temperature"] = config.temperature
    request: dict[str, Any] = {
        "modelId": config.deployment,
        "messages": converse_messages,
        "inferenceConfig": inference,
    }
    if system:
        request["system"] = system

    attempts = max(1, int(retries))
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            client = _client(config.region, timeout_s)
            payload = client.converse(**request)
        except Exception as exc:  # noqa: BLE001 - classified below
            code = _error_code(exc)
            retryable = code in _RETRYABLE_ERROR_CODES or (
                not code and exc.__class__.__name__ in {"ReadTimeoutError", "ConnectionError", "EndpointConnectionError"}
            )
            if not retryable or attempt == attempts - 1:
                raise
            last_error = exc
            time.sleep(min(2**attempt, 15))
            continue
        content = ((payload.get("output") or {}).get("message") or {}).get("content") or []
        text = "\n".join(
            str(part["text"]) for part in content if isinstance(part, dict) and part.get("text")
        ).strip()
        usage_raw = payload.get("usage") or {}
        prompt_tokens = int(usage_raw.get("inputTokens") or 0)
        completion_tokens = int(usage_raw.get("outputTokens") or 0)
        usage = {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": int(usage_raw.get("totalTokens") or (prompt_tokens + completion_tokens)),
        }
        if not text:
            last_error = RuntimeError(
                f"bedrock_chat returned an empty response (model {config.deployment}, "
                f"stopReason {payload.get('stopReason')!r})"
            )
            if attempt == attempts - 1:
                raise last_error
            time.sleep(min(2**attempt, 15))
            continue
        tracker.record(stage, prompt_tokens, completion_tokens)
        return text, usage
    raise last_error or RuntimeError("bedrock_chat: exhausted retries")


def chat_optimizer(
    system: str,
    user: str,
    max_completion_tokens: int = 16384,
    retries: int = 5,
    stage: str = "optimizer",
    reasoning_effort: str | None = None,
    timeout: float | None = None,
) -> tuple[str, dict[str, int]]:
    del reasoning_effort
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    return _chat_messages_impl(
        messages, max_completion_tokens, retries, stage, role="optimizer", timeout=timeout
    )


def chat_target(
    system: str,
    user: str,
    max_completion_tokens: int = 16384,
    retries: int = 5,
    stage: str = "target",
    reasoning_effort: str | None = None,
    timeout: float | None = None,
) -> tuple[str, dict[str, int]]:
    del reasoning_effort
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    return _chat_messages_impl(
        messages, max_completion_tokens, retries, stage, role="target", timeout=timeout
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
    timeout: float | None = None,
) -> tuple[Any, dict[str, int]]:
    del reasoning_effort
    return _chat_messages_impl(
        messages,
        max_completion_tokens,
        retries,
        stage,
        role="optimizer",
        tools=tools,
        tool_choice=tool_choice,
        return_message=return_message,
        timeout=timeout,
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
    timeout: float | None = None,
) -> tuple[Any, dict[str, int]]:
    del reasoning_effort
    return _chat_messages_impl(
        messages,
        max_completion_tokens,
        retries,
        stage,
        role="target",
        tools=tools,
        tool_choice=tool_choice,
        return_message=return_message,
        timeout=timeout,
    )


def configure_bedrock_chat(
    *,
    region: str | None = None,
    optimizer_model: str | None = None,
    target_model: str | None = None,
    timeout_seconds: float | None = None,
    temperature: float | None = None,
) -> None:
    with _config_lock:
        for config in (OPTIMIZER_CONFIG, TARGET_CONFIG):
            if region:
                config.region = region
            if timeout_seconds is not None:
                config.timeout_seconds = float(timeout_seconds)
            if temperature is not None:
                config.temperature = temperature
        if optimizer_model:
            OPTIMIZER_CONFIG.deployment = optimizer_model
        if target_model:
            TARGET_CONFIG.deployment = target_model


def get_token_summary() -> dict[str, dict[str, int]]:
    return tracker.summary()


def reset_token_tracker() -> None:
    tracker.reset()


def set_reasoning_effort(effort: str | None) -> None:
    del effort


def set_target_deployment(deployment: str) -> None:
    TARGET_CONFIG.deployment = deployment or default_model_for_backend("bedrock_chat")
    os.environ["TARGET_DEPLOYMENT"] = TARGET_CONFIG.deployment


def set_optimizer_deployment(deployment: str) -> None:
    OPTIMIZER_CONFIG.deployment = deployment or default_model_for_backend("bedrock_chat")
    os.environ["OPTIMIZER_DEPLOYMENT"] = OPTIMIZER_CONFIG.deployment
