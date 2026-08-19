"""Application settings.

Sources, in ascending precedence: defaults < config/launchpad.yaml < environment
< init kwargs. `config/launchpad.yaml` is written by the bootstrap script
(phase 2) with real resource ARNs; before bootstrap the defaults keep the app
runnable locally.
"""

from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

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

    # `start.py` already exports LAUNCHPAD_RUN_MODE for its children; this field
    # is what finally consumes it. The security posture keys off this single
    # value instead of each control guessing at production-ness on its own:
    # secure cookies + HSTS (see routers.auth.cookie_secure), local execution
    # (see local_exec_enabled), and the create_app() open-console assertion.
    run_mode: Literal["dev", "prod"] = "dev"

    # Optional local operator gate for the console. This remains independent
    # from Cognito demo users and the public /v1 API-key surface.
    # `auth_username`/`auth_password` describe the built-in admin, which is
    # config-driven only (never a `users` row) so it can never be locked out
    # from the console.
    auth_username: str = Field(default="admin", min_length=1, max_length=64)
    auth_password: SecretStr | None = None
    # Effective value is `auth_cookie_secure or run_mode == "prod"` — read it
    # through `routers.auth.cookie_secure()`, never directly, so prod is secure
    # by default while yaml can still force it on in dev.
    auth_cookie_secure: bool = False

    # An unauthenticated console refuses non-loopback callers (T1). This is the
    # explicit "I accept an open console on a reachable interface" override; the
    # hermetic test suite sets it because it runs without a password on purpose.
    allow_open_console: bool = False

    # Self-service registration for the console (only meaningful while the gate
    # is enabled — an open console has no accounts to register).
    auth_registration_enabled: bool = True
    # Default: a new registration waits in `pending` until an admin approves it,
    # and its validity window starts at approval. Set false for the older
    # instant-activation behavior.
    auth_registration_require_approval: bool = True
    auth_registration_valid_days: int = Field(default=7, gt=0, le=3650)
    # "Company email" is enforced as a free-/disposable-mail blacklist. An
    # operator can pin their own corporate domains with the allow list, which
    # short-circuits the blacklist when non-empty.
    auth_allowed_email_domains: list[str] = []
    auth_blocked_email_domains: list[str] = [
        # global free mail
        "gmail.com", "googlemail.com", "yahoo.com", "yahoo.co.jp", "ymail.com",
        "hotmail.com", "outlook.com", "live.com", "msn.com", "aol.com",
        "icloud.com", "me.com", "mac.com", "proton.me", "protonmail.com",
        "gmx.com", "gmx.de", "zoho.com", "mail.com", "mail.ru",
        "yandex.com", "yandex.ru",
        # CN free mail
        "qq.com", "foxmail.com", "163.com", "126.com", "yeah.net",
        "sina.com", "sina.cn", "sohu.com", "tom.com", "21cn.com",
        "139.com", "189.cn", "aliyun.com",
        # disposable
        "mailinator.com", "guerrillamail.com", "10minutemail.com",
        "tempmail.com", "temp-mail.org", "trashmail.com", "throwawaymail.com",
        "yopmail.com", "sharklasers.com", "getnada.com", "dispostable.com",
    ]

    # Populated by bootstrap (phase 2+); empty until then.
    account_id: str = ""
    resources: dict[str, Any] = {}

    # Each agent gets its own least-privilege execution role derived from its spec
    # (app/services/agent_iam.py) instead of every agent assuming one shared role.
    # The escape hatch exists because an over-tight policy fails at *invoke* time,
    # not deploy time: an operator hitting that needs a way back without a code
    # change. Turning it off reverts to the shared role.
    per_agent_execution_roles: bool = True
    # Warn on the Overview surface once this many Launchpad-managed roles exist.
    # The IAM default quota is 1000 roles per account, consumed one per agent.
    agent_role_count_warn_threshold: int = Field(default=800, gt=0)

    # Container image supply chain (T10). The gate runs after the push, before the
    # image can back a runtime. The threshold and the off switch are configurable
    # on purpose: an un-overridable gate strands every agent the first time a base
    # image picks up a CVE, and an operator needs a way to ship anyway knowingly.
    image_scan_enabled: bool = True
    image_scan_block_severities: list[str] = ["CRITICAL"]
    image_scan_timeout_s: int = Field(default=300, gt=0)

    # AgentCore synchronous runtime requests may run for up to 15 minutes.
    # Keep the SDK read timeout above that service limit so buffered agents can
    # return their final response.
    agentcore_read_timeout_s: int = Field(default=1000, gt=0)

    # How many experiment/canary replay prompts are posted through the A/B gateway
    # at once (optimization.service.send_gateway_traffic). Each prompt is an
    # independent session, so this only trades wall clock against how hard the
    # target runtime is pushed; the effective value is clamped to
    # TRAFFIC_MAX_CONCURRENCY, never raised by it. Only the lower direction is
    # interesting in practice: an operator hitting AgentCore throttling needs to
    # dial this to 3 from yaml/env, and a too-large value must clamp rather than
    # refuse to boot the app — hence no upper bound here.
    traffic_concurrency: int = Field(default=10, ge=1)

    # How many evaluation runs (batch evaluations / insights analyses) execute
    # concurrently. AgentCore allows 5 active batch evaluations per account
    # (hard quota); the default of 3 leaves headroom for other batch-eval
    # consumers in the same account, and the upper bound refuses configs that
    # could never fit the quota anyway.
    eval_max_concurrent_runs: int = Field(default=3, ge=1, le=5)

    # Studio local-debug (un-deployed flow execution + AI fix). The control-plane
    # backend env has no strands/openai; generated code runs in the dedicated
    # interpreter provisioned by scripts/setup_exec_env.sh. Endpoints return a
    # friendly 503 pointing at that script when the interpreter is missing.
    studio_exec_python: str = str(DATA_DIR / "exec-venv" / "bin" / "python")
    execute_timeout_s: float = 300.0
    # Which sandbox runs the generated code. "subprocess" = the historical path
    # (dedicated host interpreter + rlimits + optional uid drop); "docker" = a
    # one-shot container per run from studio_exec_docker_image, provisioned by
    # scripts/setup_exec_docker.sh. Selecting docker is itself the operator's
    # prod opt-in: the code no longer runs on the control-plane host, so
    # local_exec_enabled() serves the endpoints in prod without also setting
    # LAUNCHPAD_STUDIO_LOCAL_EXEC_ENABLED=true.
    studio_exec_backend: Literal["subprocess", "docker"] = "subprocess"
    studio_exec_docker_image: str = "launchpad-studio-exec:latest"
    # Docker network the exec containers join. Empty = docker's default bridge.
    # Set to the network created by scripts/setup_exec_docker.sh --harden-net
    # (whose subnet a DOCKER-USER rule blocks from IMDS) when running with
    # studio_exec_forward_aws_credentials=false.
    studio_exec_docker_network: str = ""
    # Running caller-supplied Python is the platform's sharpest edge (T2), so it
    # is off in prod unless an operator explicitly accepts the risk. None = derive
    # from run_mode; read it through `services.local_exec.local_exec_enabled()`.
    studio_local_exec_enabled: bool | None = None
    # Dedicated OS user the execution subprocess drops to, created by
    # scripts/setup_exec_env.sh together with a firewall rule denying that uid
    # egress to the instance metadata service. Empty = no uid drop (the child
    # then keeps the backend's own uid, and IMDS stays reachable to code that
    # asks for it directly — Linux-only hardening).
    studio_exec_user: str = ""
    # The default Mantle path mints its bearer token from the ambient AWS
    # credentials, so local debug works off the operator's profile with no API key
    # (scripts/setup_exec_env.sh). Forwarding them is therefore on by default;
    # turning it off is the hardened posture and obliges the caller to supply
    # bedrock_api_key / openai_api_key with the request.
    studio_exec_forward_aws_credentials: bool = True
    # Resource ceilings for that subprocess, alongside the existing timeout.
    studio_exec_memory_mb: int = Field(default=2048, gt=0)
    studio_exec_cpu_seconds: int = Field(default=300, gt=0)
    studio_exec_max_processes: int = Field(default=64, gt=0)
    studio_exec_max_file_mb: int = Field(default=256, gt=0)
    # AI-fix coding backend (slice 3 consumes these; declared here so the whole
    # local-debug surface shares one settings block).
    codegen_backend: str = "claude"
    codegen_model: str = "global.anthropic.claude-sonnet-5"
    codegen_timeout_s: float = 180.0
    codegen_max_repair_rounds: int = 2

    # Skill Lab (vendored SkillOpt integration). Evaluation/training CLIs from
    # vendor/skillopt run as subprocesses in the dedicated interpreter below
    # (provisioned by bootstrap from vendor/skillopt/requirements-launchpad.txt);
    # the backend process itself never imports the vendored tree. Rollout
    # execution goes to the launchpad_skill_lab_worker AgentCore runtime; the
    # judge/optimizer role calls Bedrock directly (bedrock_chat backend).
    skill_lab_python: str = str(DATA_DIR / "skill-lab-venv" / "bin" / "python")
    skill_lab_max_concurrent_jobs: int = Field(default=1, ge=1, le=4)
    # Judge default mirrors the upstream studio: gpt-5.6-sol. Must be a Converse
    # inference-profile id (the bedrock_chat judge rejects bare model ids); the
    # agentic judge routes it by family — openai.* runs on the host codex CLI
    # with the profile prefix stripped (runner.judge_exec_route).
    skill_lab_judge_model_id: str = "us.openai.gpt-5.6-sol"
    skill_lab_target_model_id: str = "global.anthropic.claude-opus-5"
    # Default target model for the codex_exec backend: the Bedrock catalog slug
    # the baked codex config resolves through its amazon-bedrock provider (NOT a
    # Converse inference-profile id — codex does its own model resolution).
    skill_lab_codex_target_model_id: str = "openai.gpt-5.6-sol"
    # Bedrock model catalog staged into the worker image's codex-home. Read from
    # the backend host at build-context assembly; `{}` fallback when absent. The
    # file embeds proprietary model instructions, so it is never committed.
    skill_lab_codex_catalog_path: str = "~/.codex/model-catalogs/bedrock-models.json"
    # Sandbox launcher argv (shlex-split) for the agentic judge's artifact
    # parsers — runs on the backend HOST (upstream architecture; the worker
    # microVM cannot run bubblewrap). Strict-AppArmor hosts where unprivileged
    # bwrap is blocked set "sudo -n bwrap" (upstream's SKILLOPT_JUDGE_SANDBOX
    # escape hatch); the vendored fail-closed boundary probe still applies.
    skill_lab_judge_sandbox: str = "bwrap"
    # Reports the claude CLI version baked into the exec-worker image. The
    # Dockerfile's `ARG CLAUDE_CLI_VERSION` is what the build actually uses (and
    # what the image's content hash covers); this mirrors it for display, with
    # parity asserted in tests/test_skill_lab_foundation.py — bump both together.
    skill_lab_worker_cli_version: str = "2.1.234"
    # Same mirror/parity contract for the codex CLI (`ARG CODEX_CLI_VERSION`).
    skill_lab_worker_codex_version: str = "0.147.0"

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
        # global.amazon.nova-2-lite-v1:0 has no litellm key of its own (only the
        # bare + us./eu./apac. ids), so a box that has not refreshed yet would
        # report Nova 2 Lite traffic with a null cost. Numbers are litellm's
        # bare amazon.nova-2-lite-v1:0 = the global-profile price.
        "nova-2-lite": {"input": 0.3, "output": 2.5},
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
