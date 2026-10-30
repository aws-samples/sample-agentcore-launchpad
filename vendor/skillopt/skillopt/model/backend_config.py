"""Runtime backend configuration for optimizer/target model calls."""
from __future__ import annotations

import os

from skillopt.model.common import default_model_for_backend, normalize_backend_name


OPTIMIZER_BACKEND = normalize_backend_name(os.environ.get("OPTIMIZER_BACKEND", "openai_chat"))
TARGET_BACKEND = normalize_backend_name(os.environ.get("TARGET_BACKEND", "openai_chat"))

# LAUNCHPAD PATCH (D16): the only exec backend is OpenCode; its configuration
# lives in skillopt.model.opencode_backend and is re-exported here so the
# pipelines keep one import site for backend configuration.
from skillopt.model.opencode_backend import (  # noqa: E402,F401
    OPENCODE_BACKEND,
    configure_opencode_exec,
    get_opencode_exec_config,
)

EXEC_BACKENDS = frozenset({OPENCODE_BACKEND})
CHAT_BACKENDS = frozenset({"openai_chat", "qwen_chat", "minimax_chat", "bedrock_chat"})


def set_optimizer_backend(backend: str) -> None:
    global OPTIMIZER_BACKEND
    OPTIMIZER_BACKEND = normalize_backend_name(backend or "openai_chat")
    if OPTIMIZER_BACKEND not in CHAT_BACKENDS:
        raise ValueError(
            f"Unsupported optimizer backend: {OPTIMIZER_BACKEND!r}. "
            f"Supported values are {sorted(CHAT_BACKENDS)}."
        )
    os.environ["OPTIMIZER_BACKEND"] = OPTIMIZER_BACKEND


def get_optimizer_backend() -> str:
    return OPTIMIZER_BACKEND


def set_target_backend(backend: str) -> None:
    global TARGET_BACKEND
    TARGET_BACKEND = normalize_backend_name(backend or "openai_chat")
    if TARGET_BACKEND not in CHAT_BACKENDS | EXEC_BACKENDS:
        raise ValueError(
            f"Unsupported target backend: {TARGET_BACKEND!r}. "
            f"Supported values are {sorted(CHAT_BACKENDS | EXEC_BACKENDS)}."
        )
    os.environ["TARGET_BACKEND"] = TARGET_BACKEND


def get_target_backend() -> str:
    return TARGET_BACKEND


def is_target_exec_backend() -> bool:
    return TARGET_BACKEND in EXEC_BACKENDS


def is_optimizer_chat_backend() -> bool:
    return OPTIMIZER_BACKEND in CHAT_BACKENDS


def is_target_chat_backend() -> bool:
    return TARGET_BACKEND in CHAT_BACKENDS
