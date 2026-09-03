"""Recommendation providers for the experiment RECOMMEND stage.

Importing this package registers every concrete provider (side-effect imports
below — add a new provider module here for it to exist, like deployer methods
in ``app/main.py``).
"""

from app.optimization.providers import agentcore, gepa_lite  # noqa: F401 — register
from app.optimization.providers.registry import (
    DEFAULT_PROVIDER,
    PROVIDER_IDS,
    describe_providers,
    get_provider,
    label_for_model_id,
    list_providers,
    register_provider,
)

__all__ = [
    "DEFAULT_PROVIDER",
    "PROVIDER_IDS",
    "describe_providers",
    "get_provider",
    "label_for_model_id",
    "list_providers",
    "register_provider",
]
