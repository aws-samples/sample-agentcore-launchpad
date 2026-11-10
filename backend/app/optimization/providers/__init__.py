"""Recommendation providers for the RECOMMEND stage and the run recommendations.

Importing this package registers every concrete provider (side-effect imports
below — add a new provider module here for it to exist, like deployer methods
in ``app/main.py``). Both providers run on the evaluation worker (N12 E2-4/E2-5).
"""

from app.optimization.providers import gepa_lite, harness_optimizer  # noqa: F401 — register
from app.optimization.providers.registry import (
    DEFAULT_PROVIDER,
    PROVIDER_IDS,
    describe_providers,
    get_provider,
    label_for_model_id,
    list_providers,
    provider_enabled,
    register_provider,
    takes_target,
)

__all__ = [
    "DEFAULT_PROVIDER",
    "PROVIDER_IDS",
    "describe_providers",
    "get_provider",
    "label_for_model_id",
    "list_providers",
    "provider_enabled",
    "register_provider",
    "takes_target",
]
