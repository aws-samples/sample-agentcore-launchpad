"""Application settings.

Sources, in ascending precedence: defaults < config/launchpad.yaml < environment
< init kwargs. `config/launchpad.yaml` is written by the bootstrap script
(phase 2) with real resource ARNs; before bootstrap the defaults keep the app
runnable locally.
"""

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import Field, SecretStr
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
CONFIG_FILE = REPO_ROOT / "config" / "launchpad.yaml"
DATA_DIR = REPO_ROOT / "data"


def load_yaml_config(path: Path = CONFIG_FILE) -> dict[str, Any]:
    if not path.is_file():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}


class _YamlSource(PydanticBaseSettingsSource):
    def __call__(self) -> dict[str, Any]:
        # Late-bind CONFIG_FILE so tests can repoint it.
        return load_yaml_config(CONFIG_FILE)

    def get_field_value(self, field, field_name):  # pragma: no cover - unused hook
        return None, field_name, False


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="LAUNCHPAD_", extra="ignore")

    app_name: str = "AgentCore Launchpad"
    version: str = "0.1.0"
    region: str = "us-west-2"
    database_url: str = f"sqlite:///{DATA_DIR / 'launchpad.db'}"
    cors_origins: list[str] = ["http://localhost:5173", "http://127.0.0.1:5173"]

    # Optional local operator gate for the console. This remains independent
    # from Cognito demo users and the public /v1 API-key surface.
    auth_username: str = Field(default="admin", min_length=1, max_length=64)
    auth_password: SecretStr | None = None
    auth_cookie_secure: bool = False

    # Populated by bootstrap (phase 2+); empty until then.
    account_id: str = ""
    resources: dict[str, Any] = {}

    # AgentCore synchronous runtime requests may run for up to 15 minutes.
    # Keep the SDK read timeout above that service limit so buffered agents can
    # return their final response.
    agentcore_read_timeout_s: int = Field(default=1000, gt=0)

    # Studio local-debug (un-deployed flow execution + AI fix). The control-plane
    # backend env has no strands/openai; generated code runs in the dedicated
    # interpreter provisioned by scripts/setup_exec_env.sh. Endpoints return a
    # friendly 503 pointing at that script when the interpreter is missing.
    studio_exec_python: str = str(DATA_DIR / "exec-venv" / "bin" / "python")
    execute_timeout_s: float = 300.0
    # AI-fix coding backend (slice 3 consumes these; declared here so the whole
    # local-debug surface shares one settings block).
    codegen_backend: str = "claude"
    codegen_model: str = "global.anthropic.claude-sonnet-5"
    codegen_timeout_s: float = 180.0
    codegen_max_repair_rounds: int = 2

    # Advisory USD-per-1M-token prices for observability cost estimates.
    # Keys are substring-matched against gen_ai.request.model ids; unknown
    # models report tokens with a null cost. Overridable in launchpad.yaml,
    # and refreshed from litellm's public price file by
    # app.services.model_prices (manual button + periodic daemon).
    model_prices: dict[str, Any] = {
        # sonnet-5 is not in litellm's public price file yet, so this entry
        # mirrors Sonnet 4.6 rather than being sourced — without it the
        # observability page reports sonnet-5 tokens with a null cost. The
        # refresher overwrites it once upstream publishes the real numbers.
        "sonnet-5": {"input": 3.0, "output": 15.0},
        "sonnet-4-6": {"input": 3.0, "output": 15.0},
        "opus-4-8": {"input": 5.0, "output": 25.0},
        "sonnet-4-5": {"input": 3.0, "output": 15.0},
        "nemotron-nano": {"input": 0.2, "output": 0.6},
    }
    model_prices_meta: dict[str, Any] = {}  # written by the price refresher
    model_prices_source_url: str = (
        "https://raw.githubusercontent.com/BerriAI/litellm/main/"
        "model_prices_and_context_window.json"
    )
    model_prices_refresh_hours: int = 24  # 0 disables the periodic refresher

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls,
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (
            init_settings,
            env_settings,
            dotenv_settings,
            _YamlSource(settings_cls),
            file_secret_settings,
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
