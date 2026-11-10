"""Feature registry for the CN build.

`FEATURES` is the closed set a console may show; `Settings.features` (core/config)
selects which of them this deployment enables. `feature_unavailable()` is the one
error every not-yet-implemented backend raises (spec N1: routers return 503
``feature.unavailable`` until a backend is registered).
"""

from typing import Literal

from app.core.errors import AppError

Feature = Literal[
    "agents",
    "chat",
    "registry",
    "memory",
    "knowledge_bases",
    "identity",
    "gateway",
    "observability",
    "workspaces",
    "users",
    "videos",
    "evaluation",
    "optimization",
    "skill_lab",
    "policy",
]
FEATURES: tuple[str, ...] = (
    "agents",
    "chat",
    "registry",
    "memory",
    "knowledge_bases",
    "identity",
    "gateway",
    "observability",
    "workspaces",
    "users",
    # hub-global tutorial video library (Learn → Videos; admin Video management)
    "videos",
    "evaluation",
    "optimization",
    "skill_lab",
    # D20: self-hosted Cedar Policy (engine + store + API; enforcement follows)
    "policy",
)


def feature_unavailable(feature: str) -> AppError:
    return AppError(
        "feature.unavailable",
        f"'{feature}' has no backend registered in this build",
        {"feature": feature},
        status_code=503,
    )
