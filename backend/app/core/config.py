"""Application settings.

Sources, in ascending precedence: defaults < config/launchpad.yaml < environment
< init kwargs. `config/launchpad.yaml` is written by the bootstrap script
(phase 2) with real resource ARNs; before bootstrap the defaults keep the app
runnable locally.
"""

import os
from collections.abc import Callable
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import Field, SecretStr, field_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

from app.core.features import FEATURES
from app.core.naming import ENV_NAME_PATTERN, validate_env_name

# The CN model catalog lives in app.schemas.agent (DEFAULT_MODEL_ID, allowed_model_ids(),
# MIN_MODEL_MAX_TOKENS); that module imports this one, so the defaults below are literals
# and the validators import the catalog lazily. tests/test_settings_eval.py pins equality.
_CN_DEFAULT_MODEL_ID = "global.zai.glm-5.3"
_CN_MODEL_IDS = ["global.zai.glm-5.3", "global.moonshotai.kimi-k3"]
# N17-obs449: the insights FailureAnalysis judge. GLM-5.3's reasoning on the detector prompt
# overran 16384 output tokens before its answer (diagnostic 2026-10-25: 65.6k reasoning chars,
# no text); Kimi K3 answered both probe sessions within 8k, finding R1's known wrong answer.
# One line to switch.
_CN_FAILURE_DETECTION_MODEL_ID = "global.moonshotai.kimi-k3"
_CN_MIN_MODEL_MAX_TOKENS = 256

REPO_ROOT = Path(__file__).resolve().parents[3]
CONFIG_FILE = REPO_ROOT / "config" / "launchpad.yaml"
DATA_DIR = REPO_ROOT / "data"


def load_yaml_config(path: Path = CONFIG_FILE) -> dict[str, Any]:
    if not path.is_file():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}


def raw_setting(name: str, default: str) -> str:
    """One scalar setting with the Settings precedence (default < launchpad.yaml <
    `LAUNCHPAD_`-prefixed env), readable WITHOUT constructing `Settings` — for modules
    `Settings`' own validators import (a `get_settings()` call there would be a circular
    import). Since M1-e (C5) the model catalog no longer reads any setting this way."""
    env = os.environ.get(f"LAUNCHPAD_{name.upper()}")
    if env is not None and env.strip():
        return env.strip()
    value = load_yaml_config().get(name)
    if value is None or str(value).strip() == "":
        return default
    return str(value).strip()


class _YamlSource(PydanticBaseSettingsSource):
    def __call__(self) -> dict[str, Any]:
        # Late-bind CONFIG_FILE so tests can repoint it.
        return load_yaml_config(CONFIG_FILE)

    def get_field_value(self, field, field_name):  # pragma: no cover - unused hook
        return None, field_name, False


def parse_backoff_seconds(value: str | float | int | None) -> tuple[float, ...]:
    """``"5,15"`` → ``(5.0, 15.0)`` — the comma-separated wait list of
    ``eval_transient_retry_backoff_seconds`` (a bare number is one entry; ``"0"`` means
    retry at once). Every entry must be a non-negative number; the list may not be empty."""
    text = str(value if value is not None else "").strip()
    if not text:
        raise ValueError("eval_transient_retry_backoff_seconds must list at least one wait "
                         "in seconds (e.g. \"5,15\"; \"0\" retries at once)")
    waits: list[float] = []
    for part in text.split(","):
        part = part.strip()
        if not part:
            raise ValueError("eval_transient_retry_backoff_seconds has an empty entry")
        try:
            wait = float(part)
        except ValueError as exc:
            raise ValueError(
                f"eval_transient_retry_backoff_seconds entry {part!r} is not a number") from exc
        if wait < 0 or wait != wait or wait == float("inf"):
            raise ValueError(
                f"eval_transient_retry_backoff_seconds entry {part!r} must be ≥ 0 seconds")
        waits.append(wait)
    return tuple(waits)


# PutRetentionPolicy accepts exactly these values (plus 0 = "do not set one" here).
LOG_RETENTION_DAYS_ALLOWED: frozenset[int] = frozenset({
    0, 1, 3, 5, 7, 14, 30, 60, 90, 120, 150, 180, 365, 400, 545, 731, 1096, 1827, 2192,
    2557, 2922, 3288, 3653,
})


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="LAUNCHPAD_", extra="ignore")

    app_name: str = "AgentCore Launchpad"
    version: str = "0.1.0"
    region: str = "cn-northwest-1"
    # Where the Bedrock *models* live (Global cross-region profiles reached with an
    # API key — spec C3/C4; N2 wires the credential). Distinct from `region`, where
    # AgentCore and every workspace resource live.
    model_region: str = "us-west-2"  # region-boundary: global-bedrock
    # N11: optional environment suffix so two complete Launchpad environments can
    # coexist in one account+region. Empty (default) keeps every stack / resource
    # name exactly as before; "t1" makes every fixed `launchpad-*` name end in
    # `-t1` (app/core/naming.py::resource_name). The CDK app (`-c env_name=t1`)
    # and the EC2 template (parameter `EnvName`) share this exact pattern; the
    # EC2 host writes LAUNCHPAD_ENV_NAME into /etc/launchpad/env.
    env_name: str = Field(default="", pattern=ENV_NAME_PATTERN)
    # Opt-in preserves existing installations; new distribution configs use dedicated.
    memory_domain_mode: Literal["shared", "dedicated"] = "shared"
    # Console modules this deployment exposes (GET /api/capabilities). Default = the
    # kept module set; a subset hides modules from navigation and routing.
    features: list[str] = list(FEATURES)
    # N17-obs463 (user ruling 2026-10-26): the IANA zone every API time is returned in and
    # the console shows (GET /api/capabilities.display_timezone). Storage stays UTC; the
    # API serialises the same instant with this zone's offset (app/core/display_time.py).
    display_timezone: str = "Asia/Shanghai"
    # --- D20 self-hosted Policy -------------------------------------------------
    # Mode a workspace's `policy_settings` row starts in (plan §8.1-5: observe first,
    # promote to `enforce` with evidence). `off` disables evaluation until an operator
    # opts in.
    policy_default_mode: Literal["off", "log_only", "enforce"] = "log_only"
    # Retention of `policy_decisions` rows (the audit's system of record); CloudWatch
    # keeps the 90-day cross-check (P1-d).
    policy_decisions_keep_days: int = 30
    # `record_decision` prunes expired rows every N writes (plus every `GET /audit`), so
    # an un-audited workspace cannot grow unbounded; 0 disables the write-side prune.
    policy_decisions_prune_every: int = 500
    # Per-user principal + Gateway interceptor + Dogwood engine (plans/DESIGN-POLICY-USER-
    # PRINCIPAL.md §13-§15; contracts: docs/cn/interfaces/policy-principal-contracts.md).
    # Global kill switch: nothing of the work (token
    # mint, `_meta` injection, interceptor attach, engine calls) is active unless this AND
    # the workspace's `policy_settings.gateway_interceptor` are on. Off = byte-identical.
    policy_gateway_interceptor: bool = False
    # Which gateway the `policy-interceptor` bootstrap stage provisions the interceptor for:
    # `platform` = this environment's `launchpad-gw[-env]` (resources.gateway_id); `probe` = the
    # kept P0 probe gateway `launchpad-probe-gw` (milestone M1 only — design §14 / runbook).
    policy_interceptor_target: Literal["platform", "probe"] = "platform"
    # Stream A1 (contract I1): the hub's principal-token signing key — an ECC_NIST_P256
    # SIGN_VERIFY KMS key (id, ARN or alias) in the hub's account; only the hub role may Sign.
    # Empty = no token can be minted (`principal_token.PrincipalTokenUnavailable`).
    policy_principal_kms_key_id: str = ""
    # stream A5a-2: what the hub needs to compose the I3 gateway bundle after an engine install
    # (`services/policy_gateway_publish.py`) — the engine shards' private endpoints
    # (`<ip>:<port>`, one per shard) and the hub's own role ARN (the interceptor's hub caller).
    # Both empty (the default) = the hub never writes a gateway bundle (deploy-ops sets them).
    policy_engine_endpoints: list[str] = Field(default_factory=list)
    policy_hub_role_arn: str = ""
    # D20 P1-d: the CloudWatch mirror of `policy_decisions` (services/policy_audit.py).
    # `enabled=False` keeps decisions in the ledger only; the batch/flush/queue knobs bound
    # the background sink (≤ batch_size events or ≤ flush_s seconds per put_log_events,
    # queue_max events buffered before the sink drops with a WARN).
    policy_audit_enabled: bool = True
    policy_audit_batch_size: int = Field(default=100, ge=1, le=10000)
    policy_audit_flush_s: float = Field(default=1.0, gt=0, le=60)
    policy_audit_queue_max: int = Field(default=5000, ge=1, le=1_000_000)
    # What an EMPTY applicable ACTIVE set means — for the simulator and, as the gateway
    # bundle's `empty_set_decision`, for the interceptor. Global parity (user ruling
    # 2026-10-27, contract amendment 9): `deny` (default) = Cedar's default-deny — an attached
    # engine with no matching permit denies the tool call (publish the two call_tool baseline
    # permits first). The legacy D20-obs1 value `allow` treats zero ACTIVE policies as "policy
    # not configured" (ALLOW with reason `no_active_policies`) until the first ACTIVE policy.
    # (the former `policy_empty_set_decision` knob is gone — Global model: an attached engine with
    # no matching permit denies; the bundle carries the constant `empty_set_decision: "deny"`)
    # N-ARCH-obs18 (live 2026-10-16): the architect turn's single automatic replay fired
    # 3 s after a Bedrock `internalServerException` and hit the identical 5xx. When the
    # failure is a transient upstream error (5xx / throttle / read timeout / a stream cut
    # with no reply) the replay waits this long first, in heartbeat slices, cancellable.
    # A `max_tokens` replay never waits. Tests set it to 0 (conftest env).
    assistant_replay_backoff_seconds: float = 15.0
    # N17-obs295: a model proposal that fails ONLY the structural evaluation-plan routing
    # rules (`assistant/validation_codes.AUTOCORRECT_CODES`) gets ONE internal correction
    # call inside the same turn; `false` stores the invalid revision as before.
    assistant_proposal_autocorrect: bool = True
    # N17-obs468: a reply with NO proposal block that imitates the replay placeholder or claims a
    # submitted revision gets ONE corrective internal call in the same turn; `false` keeps
    # only the transcript notice that the proposal was not updated.
    assistant_proposal_reask: bool = True

    @field_validator("env_name")
    @classmethod
    def _env_name_not_reserved(cls, value: str) -> str:
        # The regex above is what the other two layers share; the reserved-name check
        # (D17: "kb") is the one rule a regex cannot carry through pydantic.
        return validate_env_name(value)

    @field_validator("display_timezone")
    @classmethod
    def _known_zone(cls, value: str) -> str:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError  # noqa: PLC0415

        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"display_timezone: unknown IANA time zone {value!r}") from exc
        return value

    @field_validator("features")
    @classmethod
    def _known_features(cls, value: list[str]) -> list[str]:
        unknown = [f for f in value if f not in FEATURES]
        if unknown:
            raise ValueError(
                f"unknown features: {', '.join(unknown)} (allowed: {', '.join(FEATURES)})"
            )
        return value

    @field_validator("agent_default_model_id", "eval_judge_model_id", "eval_actor_model_id",
                     "prompt_opt_default_model_id",
                     # N16-obs24: the Skill Lab roles obey the same catalog rule
                     "skill_lab_judge_model_id", "skill_lab_target_model_id",
                     # N17-obs252: every platform model choice is a catalog model role
                     "memory_extract_model_id", "codegen_model",
                     # N17-obs449: the insights failure-detection judge
                     "eval_failure_detection_model_id")
    @classmethod
    def _catalog_model_id(cls, value: str) -> str:
        from app.schemas.agent import allowed_model_ids

        allowed = allowed_model_ids()
        if value not in allowed:
            raise ValueError(
                f"model.not_allowed: {value!r} is not in the CN model catalog "
                f"({', '.join(allowed)})"
            )
        return value

    @field_validator("eval_judge_fallback_model_id")
    @classmethod
    def _optional_catalog_model_id(cls, value: str) -> str:
        value = (value or "").strip()
        return cls._catalog_model_id(value) if value else ""

    @field_validator("prompt_opt_models")
    @classmethod
    def _catalog_model_ids(cls, value: list[str]) -> list[str]:
        from app.schemas.agent import allowed_model_ids

        if not value:
            raise ValueError("prompt_opt_models must name at least one catalog model")
        allowed = allowed_model_ids()
        unknown = [m for m in value if m not in allowed]
        if unknown:
            raise ValueError(
                f"model.not_allowed: {', '.join(unknown)} not in the CN model catalog "
                f"({', '.join(allowed)})"
            )
        return list(dict.fromkeys(value))

    database_url: str = f"sqlite:///{DATA_DIR / 'launchpad.db'}"
    cors_origins: list[str] = ["http://localhost:5173", "http://127.0.0.1:5173"]

    # Where a browser reaches this console (scheme + host [+ port], no path).
    # Feeds the as_user (3LO) OAuth return URL below; the dev default matches
    # the vite dev server the CORS default already trusts.
    public_base_url: str = "http://localhost:5173"
    # The exact page AgentCore Identity redirects to after IdP consent. Empty =
    # derived as {public_base_url}/auth/return; set explicitly only behind a
    # path-rewriting proxy. Deployers inject it as LAUNCHPAD_OAUTH_RETURN_URL
    # and allow-list it on the agent's workload identity (deployer/return_url.py).
    oauth_return_url: str = ""

    def resolved_oauth_return_url(self) -> str:
        return self.oauth_return_url or f"{self.public_base_url.rstrip('/')}/auth/return"

    # `start.py` already exports LAUNCHPAD_RUN_MODE for its children; this field
    # is what finally consumes it. The security posture keys off this single
    # value instead of each control guessing at production-ness on its own:
    # secure cookies + HSTS (see routers.auth.cookie_secure), local execution
    # (see local_exec_enabled), and the create_app() open-console assertion.
    run_mode: Literal["dev", "prod"] = "dev"

    # Optional local operator gate for the console. This remains independent
    # from the public /v1 API-key surface.
    # `auth_username`/`auth_password` describe the built-in admin, which is
    # config-driven only (never a `users` row) so it can never be locked out
    # from the console.
    auth_username: str = Field(default="admin", min_length=1, max_length=64)
    auth_password: SecretStr | None = None
    # Effective value is `auth_cookie_secure or run_mode == "prod"` — read it
    # through `routers.auth.cookie_secure()`, never directly, so prod is secure
    # by default while yaml can still force it on in dev.
    auth_cookie_secure: bool = False
    # Explicit, service-wide demo transport: keep production authentication while allowing
    # its one session cookie over both HTTP and HTTPS. Default deployments stay unchanged.
    console_http_lab: bool = False
    # Absolute lifetime of a console session cookie (r12-g, N12-obs57). The cookie
    # carries its own signed expiry and is NOT refreshed on activity, so this is a
    # hard ceiling rather than an idle timeout; a registered account's session is
    # additionally clamped to the account's `expires_at`.
    auth_session_ttl_seconds: int = Field(default=12 * 3600, ge=60, le=30 * 86400)

    # Login protection (r12-g, N12-obs57). The console may sit behind the open NLB,
    # so the sign-in form is the only gate. Everything here is in-process state
    # (`services/login_guard.py`): one uvicorn worker, no Redis; a restart clears it.
    # PRIMARY control — per submitted account name (existing or not, so a lock never
    # discloses whether an account exists): after `threshold` consecutive failures
    # the name is locked for `base` seconds, doubling with every further failure up
    # to `max`; attempts during a lock answer 429 `auth.locked` and do not count; a
    # successful sign-in or an admin `PATCH /api/users/{id} {"unlock": true}` clears it.
    auth_login_lockout_enabled: bool = True
    auth_login_lockout_threshold: int = Field(default=5, ge=1, le=100)
    auth_login_lockout_base_seconds: int = Field(default=60, ge=1, le=86400)
    auth_login_lockout_max_seconds: int = Field(default=30 * 60, ge=1, le=7 * 86400)
    # SECONDARY control — failed sign-ins per source IP in a sliding window (429
    # `auth.rate_limited`). Behind the console NLB (client-IP preservation OFF) every
    # caller presents as the NLB's private IP, so this window is shared by all users
    # there and is deliberately loose; `0` disables it. Only the transport peer is
    # used — `X-Forwarded-For` is never trusted (see `routers.auth._peer_is_loopback`).
    auth_login_ip_limit: int = Field(default=20, ge=0, le=100_000)
    auth_login_ip_window_seconds: int = Field(default=300, ge=1, le=86400)
    # `/v1`: invalid / missing `X-Api-Key` attempts per source IP (429
    # `auth.rate_limited`); valid keys are never counted. `0` disables it.
    auth_api_key_ip_limit: int = Field(default=20, ge=0, le=100_000)
    auth_api_key_ip_window_seconds: int = Field(default=300, ge=1, le=86400)
    # Password policy for registration and admin resets (r12-g; relaxed mode r14-c,
    # N14-obs3, user decision 2026-10-19). Existing hashes are untouched until the
    # account next changes its password. `strict` (default, the r12-g behaviour)
    # refuses, besides the length, a password equal to the username / email / email
    # local part (case-insensitive) and the embedded top-common list
    # (`services/users.py`); `relaxed` checks the minimum length only. The length
    # floor is 6 (pydantic refuses a lower setting at load, as before).
    auth_password_policy: Literal["strict", "relaxed"] = "strict"
    auth_password_min_length: int = Field(default=12, ge=6, le=128)
    # Self-registration abuse controls (r12-g, A-2). Attempts per source IP (every
    # `POST /api/auth/register` that passes body validation; `0` disables) — the same
    # NLB caveat as the sign-in window applies, so onboarding a whole team through the
    # NLB counts against one address; raise the limit for that hour if it bites.
    auth_register_ip_limit: int = Field(default=10, ge=0, le=100_000)
    auth_register_ip_window_seconds: int = Field(default=3600, ge=1, le=7 * 86400)
    # Ceiling on `status=pending` accounts: beyond it self-registration answers 409
    # `auth.registration_backlog` (journaled) until an admin approves / rejects some;
    # admin-created accounts are never counted or refused. `0` = no ceiling.
    auth_registration_pending_max: int = Field(default=50, ge=0, le=100_000)

    # Session-cookie signing secret (r12-g, A-3). Operators may pin it with
    # `LAUNCHPAD_SESSION_SECRET` (any string, 64 hex chars recommended); otherwise a
    # random 32-byte secret is generated on first start and persisted 0600 at
    # `session_secret_file` (under the gitignored `data/`). Rotating either value
    # signs every session out once. The admin password is NOT part of the key any more.
    session_secret: SecretStr | None = None
    session_secret_file: str = str(DATA_DIR / "session_secret")

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
    # N17-obs206 (user ruling 2026-10-22): installed system presets in EVERY workspace are
    # re-published automatically when they fall behind — the installed snapshot pins an
    # older model catalog than the ledger's, or this build ships a newer release; the
    # repair decision is `system_agents.service._repair`'s own. The sweep
    # (`system_agents/auto_repair.py`) runs once
    # `start_delay_s` after startup and `debounce_s` after the last catalog-shaping registry
    # write (a burst of provider edits coalesces into one sweep); its deploy jobs run one at
    # a time per host. `false` turns both triggers off (the per-card 修复快照 stays).
    system_presets_auto_repair: bool = True
    system_presets_auto_repair_start_delay_s: int = Field(default=60, ge=0, le=3600)
    system_presets_auto_repair_debounce_s: int = Field(default=180, ge=0, le=3600)
    # N11-obs56: CloudWatch Logs retention for the per-runtime log group
    # (`/aws/bedrock-agentcore/runtimes/<runtime_id>-DEFAULT`), applied by the deploy stage
    # (create-if-missing + PutRetentionPolicy, idempotent, fail-soft). Without it the
    # service-created group keeps every log line forever. Must be one of the values the API
    # accepts; 0 disables the step. The stacks' Lambda log groups carry the same 90 days
    # from CDK (infra/stacks/base_stack.py `LAMBDA_LOG_RETENTION`).
    log_group_retention_days: int = Field(default=90, ge=0)

    @field_validator("log_group_retention_days")
    @classmethod
    def _check_log_group_retention_days(cls, value: int) -> int:
        if value not in LOG_RETENTION_DAYS_ALLOWED:
            raise ValueError(
                "log_group_retention_days must be 0 (disabled) or one of the values "
                f"CloudWatch Logs accepts: {sorted(LOG_RETENTION_DAYS_ALLOWED - {0})}"
            )
        return value

    # N10v4-obs9 (live, twice): an A2A runtime's trailing OTel flush re-created its
    # just-deleted log group ~1 s after the teardown, empty. The log-group step now runs
    # LAST, waits for the Runtime to be gone (GetAgentRuntime → ResourceNotFound, up to
    # this long), deletes, then settles in the background (N-SCN-obs8: live re-creations
    # at +2 s and +18 s outran the old 5 s inline re-check): the prefix is re-listed at
    # each offset below (seconds after the first delete), anything re-created is deleted
    # again, two consecutive empty checks end it early. 0 disables the wait; an empty
    # list disables the settle (tests).
    teardown_runtime_gone_timeout_s: int = Field(default=120, ge=0, le=900)
    teardown_log_group_settle_s: list[float] = Field(default=[10.0, 30.0, 60.0], max_length=8)

    @field_validator("teardown_log_group_settle_s")
    @classmethod
    def _settle_schedule_ascending(cls, value: list[float]) -> list[float]:
        if any(v <= 0 or v > 300 for v in value):
            raise ValueError("teardown_log_group_settle_s offsets must be in (0, 300] seconds")
        if value != sorted(value) or len(set(value)) != len(value):
            raise ValueError("teardown_log_group_settle_s offsets must be strictly ascending")
        return value

    # N17-obs151: retention of the local `data/builds/<agent>` package scratch (one directory
    # per agent name, recreated on every build; `deployer/build_dirs.py`). On agent delete the
    # agent's directory goes with the other residue; at startup (before interrupted jobs are
    # resumed) the sweep removes the directories of agents without a live ledger row, keeps each
    # live agent's newest `builds_keep_per_agent` directories (today one exists per agent, so
    # any value >= 1 keeps it; 0 purges live agents' scratch too) and trims the oldest first
    # until the root is under `builds_max_gb` GiB (0 = no cap). Keys of config/launchpad.yaml
    # (deploy.md §8.10).
    builds_keep_per_agent: int = Field(default=2, ge=0, le=50)
    builds_max_gb: float = Field(default=10.0, ge=0, le=10_000)

    # Container image supply chain (T10). The gate runs after the push, before the
    # image can back a runtime. The threshold and the off switch are configurable
    # on purpose: an un-overridable gate strands every agent the first time a base
    # image picks up a CVE, and an operator needs a way to ship anyway knowingly.
    image_scan_enabled: bool = True
    image_scan_block_severities: list[str] = ["CRITICAL"]
    image_scan_timeout_s: int = Field(default=300, gt=0)

    # --- CN build-chain mirrors (N5) ---
    # China network mirrors for the build chain (spec §3.F / I5 — not yet
    # provided by the operator, so these are sensible defaults, parameterized).
    pip_index_url: str = "https://pypi.tuna.tsinghua.edu.cn/simple"
    # N11-obs25 / N13-bug1: optional secondary index for mirror outages (tuna served an
    # expired certificate on 2026-10-10). Forwarded as PIP_EXTRA_INDEX_URL + UV_INDEX to
    # the zip resolve, CodeBuild and the Skill Lab worker image. It must be uv-complete:
    # https://mirror.nju.edu.cn/pypi/web/simple is; mirrors.aliyun.com advertises PEP 658
    # .metadata files it does not serve (404) and makes `uv pip` hard-fail. Empty = none.
    pip_extra_index_url: str = ""
    # N11-r3-bug7: the BUILD mirror is a separate knob from the HOST mirror above. Both
    # Skill Lab worker-image builds in cn-northwest-1 died 0.2 s after "Looking in
    # indexes" with no retry/TLS output — tuna answers CodeBuild's NAT egress with an
    # immediate 4xx while the EC2 hosts get 200 from the same URL (nothing had ever
    # pip-installed inside CodeBuild in this account, so BYOC container builds are hit
    # too). Both CodeBuild projects (hub via infra/stacks/base_stack.py, spokes via
    # services/workspace_bootstrap.py) and the worker buildspec resolve from these two:
    # NJU is the only independent uv-complete mirror probed (N11-obs27); Tencent is
    # pip/uv-clean and is the fallback the Dockerfile retries on. Hosts keep tuna.
    codebuild_pip_index_url: str = "https://mirror.nju.edu.cn/pypi/web/simple"
    codebuild_pip_fallback_index_url: str = "https://mirrors.cloud.tencent.com/pypi/simple"
    npm_registry: str = "https://registry.npmmirror.com"
    # Debian apt mirror host for images built in China (the Skill Lab exec worker).
    apt_mirror: str = "mirrors.aliyun.com"
    # Off by default (R4): a bad BASE_IMAGE hard-fails every container build,
    # where a slow/unreachable pip or npm mirror only degrades. Flip on only
    # after `scripts/mirror_base_images.sh` has actually populated the ECR
    # prefix below in this account/region.
    base_image_mirror_enabled: bool = False
    base_image_mirror_prefix: str = "base"
    # --- end CN build-chain mirrors (N5) ---

    # The manylinux level the zip deploy paths resolve/install Python wheels for
    # (consumed via app/core/runtime_target.py — see its module docstring for the
    # measured evidence: AgentCore Runtime = AL2023, glibc 2.34, aarch64,
    # 2026-09-18). manylinux_2_28 matches what current wheel builders publish;
    # "manylinux2014" is the documented safe fallback (the official docs'
    # conservative recommendation) if a runtime image ever reports older glibc.
    runtime_python_platform: str = Field(
        default="manylinux_2_28", pattern=r"^manylinux(2014|_2_\d+)$"
    )

    # AgentCore synchronous runtime requests may run for up to 15 minutes.
    # Keep the SDK read timeout above that service limit so buffered agents can
    # return their final response.
    agentcore_read_timeout_s: int = Field(default=1000, gt=0)

    # N-SCN-bug4: how many times a generated runtime (harness / zip_runtime / studio)
    # continues a turn whose completion stopped on `max_tokens` before it gives up with a
    # visible notice. The templates default to 1 themselves; a different value is
    # injected as LAUNCHPAD_MAX_TOKENS_CONTINUATIONS unless the spec's own `env` pins it;
    # 0 = never continue (notice only).
    max_tokens_continuations: int = Field(default=1, ge=0, le=5)
    # N-SCN-bug4 ⑤ (user ruling 2026-10-14): the output cap a max_tokens *continuation*
    # call uses instead of the model's MODEL_MAX_TOKENS — a cut-off turn is finished
    # "concisely", so a lower cap bounds the second half. 0 (default) = off: the
    # continuation runs with the model's own cap and the templates stay byte for byte;
    # a non-zero value is injected as LAUNCHPAD_CONTINUATION_MAX_TOKENS unless the spec's
    # own `env` pins it. Restored to the previous cap after the continuation (Harness
    # agents are cached per session).
    continuation_max_tokens: int = Field(default=0, ge=0, le=65536)

    # --- CN evaluation / optimization (N12) ---
    # How many canary replay prompts are sent at once (optimization.traffic). Each
    # prompt is an independent
    # session, so this only trades wall clock against how hard the target runtime is
    # pushed; the effective value is clamped to TRAFFIC_MAX_CONCURRENCY, never raised
    # by it — a too-large value must clamp rather than refuse to boot the app.
    traffic_concurrency: int = Field(default=10, ge=1)
    # How many evaluation runs execute concurrently (the cap of app/evaluation/queue.py,
    # also reused by Skill Lab's queue). Each run is one async job on the evaluation
    # worker (plan §B.0: parallel jobs = parallel Runtime sessions), so the Global account
    # quota of 5 batch evaluations no longer binds; 20 keeps the hub's polling and the
    # judge fan-out bounded (evaluation-engine.md §"settings"). Defined ONCE — a second
    # `le=5` copy of this field (the fork baseline's quota line) used to shadow this one
    # (pydantic keeps the last definition), so the effective cap was 5 (r8-nits, item 4;
    # tests/test_settings_eval.py guards against a field being declared twice).
    eval_max_concurrent_runs: int = Field(default=3, ge=1, le=20)
    # How long one evaluation run waits for its job (evaluator, insights and
    # online-report runs alike). A run that still outlives this can be re-checked
    # (POST /api/eval/runs/{id}/recheck). Ceiling = the worker's 8 h async budget.
    eval_batch_wait_s: int = Field(default=7200, ge=60, le=28800)
    # N-SCN-obs14: scored records EACH arm must carry before an experiment / canary verdict
    # may be directional (treatment-wins / control-wins / tie); below it the verdict is
    # `insufficient-n` (operator-overridable, never terminal) even when a p-value says
    # "significant". A record is one evaluator score of one session, so this is ≈ sessions
    # per arm. 10 matches the 9-prompt replay dataset across the three ramp stages: a
    # 90/10 round leaves the treatment at ~1, 50/50 adds ~4–5 each and 1/99 adds ~9 to the
    # treatment, so both arms clear the bar by the final stage without padding the dataset.
    verdict_min_arm_n: int = Field(default=10, ge=1, le=1000)
    # N-SCN-obs3: how long canary `cleanup` waits (on its background job thread) for the
    # named endpoints it deleted to stop being listed — DeleteAgentRuntimeEndpoint is
    # eventually consistent (DELETING → gone, 1–2 min live). Still-listed names at the
    # deadline are recorded as `artifacts.endpoints_pending`; the row flips to `cleaned`
    # either way. 0 = one listing, no wait.
    canary_cleanup_endpoints_gone_timeout_s: int = Field(default=180, ge=0, le=900)
    # Where evaluation / optimization jobs execute (plan §B.0, CA-8 ruling). "runtime"
    # = the system-managed `launchpad_eval_worker` AgentCore Runtime, invoked
    # asynchronously — the only production executor. "inprocess" runs the same engine
    # library on this host and is allowed only for the hermetic suite, `make dev`
    # without a deployed worker, and the synchronous SCORE NOW call; in run_mode=prod
    # the batch/online/insights/recommend/optimize submit paths refuse it with 409
    # `evaluation.executor_not_allowed` (evaluation/executor.py::select_executor).
    eval_executor: Literal["runtime", "inprocess"] = "runtime"
    # Judge / actor-simulator models (strands_evals evaluators, ActorSimulator): CN
    # catalog ids only (spec C3), built solely by aws_clients.global_bedrock_model on
    # the Global Bedrock bearer; max_tokens >= MIN_MODEL_MAX_TOKENS.
    eval_judge_model_id: str = _CN_DEFAULT_MODEL_ID
    # r15-e (upstream #275 subset) — the model roles an administrator may override on
    # Settings → Model Providers → Model roles (`services/model_roles.py`; ledger
    # `platform_settings`, re-applied to every rebuilt Settings by the hook below).
    # Precedence: console override > env > launchpad.yaml > default. Read them per call
    # through `get_settings()`, never at import. `agent_default_model_id` is the model of
    # a spec submitted without one (`schemas.agent.agent_default_model_id()`) and the V2
    # wizard's pre-selection (`GET /api/model-defaults`); `eval_judge_fallback_model_id`
    # is reserved ("" = none) — no consumer falls back to it in this build.
    agent_default_model_id: str = _CN_DEFAULT_MODEL_ID
    eval_judge_fallback_model_id: str = ""
    # N17-obs449: the judge of the insights FailureAnalysis detector (`strands_eval.insights`)
    # — a separate role because a reasoning judge can spend any budget before answering it;
    # every other judge call stays on `eval_judge_model_id`
    eval_failure_detection_model_id: str = _CN_FAILURE_DETECTION_MODEL_ID
    # N17-obs252 (user ruling 2026-10-22 "需要都读模型角色来确定模型"): the model the memory
    # extraction pipeline (hub → `memory_extract` SQS message → Lambda) summarises with — a
    # chat model, no tools; its input limit bounds one extraction (N17-obs237). The hub
    # names it in every message (cn/r17-aj); the `memory_extract` role on the Model roles tab.
    memory_extract_model_id: str = _CN_DEFAULT_MODEL_ID
    # rev-r25 F3 on N17-obs252: the console features whose model roles are HIDDEN on the Model
    # roles tab — must mirror the console's `HIDDEN_SURFACES.features` (`skill_lab`) and
    # `HIDDEN_SURFACES.createEntries.studio = false` (`studio`), frontend/src/v2/hiddenSurfaces.ts.
    # A hidden role is not a registry referrer (disabling / deleting a model is never blocked
    # by a role the administrator cannot see); when its model goes away its console override
    # is dropped (one WARNING) so a re-enabled feature never points at a dead model
    # (`services/model_roles.on_model_unavailable`).
    hidden_role_features: list[str] = ["skill_lab", "studio"]
    # D21 (plan §9.3) → N17-obs203 (user decision 2026-10-21): the operator host allowlist
    # is no longer a precondition for registering an OpenAI-compatible provider. EMPTY (the
    # default) = any public https host passes the write-time check and the per-request SSRF
    # guard (`model_probe.guard_base_url`: https only, a hostname, DNS resolved before every
    # request with loopback / private / link-local / IMDS refused). NON-EMPTY = "lock mode":
    # only the listed hosts (exact or `*.suffix`) are accepted — 422
    # `model_provider.host_not_allowed`. Operator-set (yaml / env), never widened from the
    # console.
    model_provider_allowed_hosts: list[str] = []
    # N17-obs203: hosts that are refused ALWAYS (exact or `*.suffix`), whatever the allowlist
    # says — the operator's denylist for an endpoint the region boundary must never reach.
    model_provider_denied_hosts: list[str] = []
    eval_judge_max_tokens: int = Field(default=1024, ge=_CN_MIN_MODEL_MAX_TOKENS, le=65536)
    # N-SCN-bug2: eval_judge_max_tokens is the FLOOR a judge / optimizer / summariser call
    # starts from (the catalog raises it per model — GLM 5.3 4096, Kimi K3 2048, see
    # schemas.agent.MODEL_CAPABILITIES["default_judge_max_tokens"]); a call that hits the
    # model's output limit is retried with the budget doubled, at most twice, never above
    # this cap (evaluation/judge_budget.py, services/strands_eval/model_calls.py).
    eval_judge_max_tokens_cap: int = Field(default=16384, ge=_CN_MIN_MODEL_MAX_TOKENS,
                                           le=65536)
    # judge calls per job are I/O-bound: sessions scored in parallel on the worker
    eval_judge_concurrency: int = Field(default=4, ge=1, le=32)
    # N12-obs53 / N12-obs55 (live 2026-10-16): the worker's scenario replay and its judge
    # calls retry a transient upstream error (Bedrock `internalServerException` on
    # ConverseStream, throttling, a read timeout — `runtime_invoke.is_upstream_transient`)
    # up to 3 attempts, waiting these many seconds before the 2nd / 3rd attempt (the last
    # value repeats). Comma-separated; "0" retries at once. Rendered into the worker on
    # install / repair; the in-process executor and SCORE NOW read it live. Tests set "0".
    eval_transient_retry_backoff_seconds: str = "5,15"

    @field_validator("eval_transient_retry_backoff_seconds")
    @classmethod
    def _transient_backoff_parses(cls, value: str) -> str:
        parse_backoff_seconds(value)  # raises on a non-numeric / negative / empty entry
        return value

    @property
    def eval_transient_retry_backoff(self) -> tuple[float, ...]:
        """``eval_transient_retry_backoff_seconds`` as the wait tuple the worker takes."""
        return parse_backoff_seconds(self.eval_transient_retry_backoff_seconds)
    eval_actor_model_id: str = _CN_DEFAULT_MODEL_ID
    # Job sharding / health (plan §B.0): a run whose estimated judge time exceeds the
    # budget, or whose session count exceeds the cap, is split into shard jobs; a job
    # whose status.json is older than eval_job_stale_s is re-dispatched (attempt+1).
    eval_job_budget_s: int = Field(default=25200, ge=60, le=28800)
    eval_job_max_sessions: int = Field(default=150, ge=1, le=1000)
    eval_job_stale_s: int = Field(default=600, ge=30, le=7200)
    # from-sessions jobs (N-SCN-obs10) running at once per backend process; each one
    # fans out FROM_SESSIONS_WORKERS Logs Insights reads, so N filed jobs would otherwise
    # be N × 4 concurrent CloudWatch queries. A job past the cap stays `queued` on a slot.
    from_sessions_job_concurrency: int = Field(default=2, ge=1, le=8)
    # Optional CloudWatch mirror of every result record (EMF) under this log-group
    # prefix so the Global "results log group" link resolves; "" = SQLite only.
    eval_results_log_group_prefix: str = ""
    # R3-f: a run that judged fewer than this share of its expected units (turns, sessions,
    # tool calls) is flagged — the console warns and the coverage EMF line marks it
    eval_coverage_min: float = Field(default=0.9, ge=0.0, le=1.0)
    # P2.2: the wall-clock bound of ONE judge call (the client's read timeout × its retries ×
    # the engine's transient retries had no overall cap); past it the unit is a
    # `judge_deadline` error row. Carried to the worker on the job spec.
    eval_judge_deadline_s: int = Field(default=300, ge=10, le=3600)
    # P8.4: an optional judge-token budget per run (0 = none), split over the run's shards; a
    # job that spent its share starts no further session and completes with what it judged
    eval_run_token_budget: int = Field(default=0, ge=0)
    # Lambda code evaluators (the Global CustomCode kind) — off until a workspace
    # opts in; the worker role then gains lambda:InvokeFunction on launchpad-evalfn-* (the
    # flag rides in the worker preset spec env, so switching it on redeploys the worker).
    eval_code_evaluators_enabled: bool = False
    # Online evaluation scheduler (plan §B.3): a hub daemon thread scanning idle
    # sessions every tick and dispatching one online_eval job per config per tick.
    online_eval_enabled: bool = True
    online_eval_tick_s: int = Field(default=60, ge=10, le=3600)
    online_eval_batch: int = Field(default=20, ge=1, le=200)
    # P4.6: a sample whose evaluation failed for a transient reason (throttling, timeout,
    # infrastructure / telemetry readiness, a failed or lost job) is dispatched again after
    # each wait — one retry per entry ([] = never retry); an agent failure never is
    online_eval_retry_backoff_s: list[int] = Field(default_factory=lambda: [300, 900],
                                                   max_length=5)
    # Recommendation providers for a run's recommendations
    # (app/optimization/providers). Model ids come from the CN catalog (spec C3) and
    # are invoked through the Global Bedrock bearer; the list is what the console
    # offers, the default leads it.
    prompt_opt_default_model_id: str = _CN_DEFAULT_MODEL_ID
    prompt_opt_models: list[str] = list(_CN_MODEL_IDS)
    # strands-harness-optimizer provider (plan §B.5, R-4): disabled by default — its
    # reflection agent carries a `shell` tool; gepa_lite stays the default provider.
    prompt_opt_harness_optimizer_enabled: bool = False
    prompt_opt_epochs: int = Field(default=2, ge=1, le=10)
    # Sessions a provider reads from the pinned run (worst-first + a best-scoring
    # contrast set) and the reflection call's output budget.
    prompt_opt_max_sessions: int = Field(default=30, ge=3, le=100)
    prompt_opt_max_tokens: int = Field(default=8192, ge=512, le=16000)
    # Read timeout of the (streaming) reflection call — bounds the gap between chunks.
    prompt_opt_read_timeout_s: int = Field(default=900, gt=0)
    # --- end CN evaluation / optimization (N12) ---

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
    # Local debug uses the operator's ambient credentials; forwarding them is on
    # by default (scripts/setup_exec_env.sh). Turning it off is the hardened
    # posture and obliges the caller to supply bedrock_api_key with the request.
    studio_exec_forward_aws_credentials: bool = True
    # Resource ceilings for that subprocess, alongside the existing timeout.
    studio_exec_memory_mb: int = Field(default=2048, gt=0)
    studio_exec_cpu_seconds: int = Field(default=300, gt=0)
    studio_exec_max_processes: int = Field(default=64, gt=0)
    studio_exec_max_file_mb: int = Field(default=256, gt=0)
    # AI-fix coding backend: Kimi K3 over Global Bedrock's OpenAI-compatible endpoint
    # (bearer = AWS_BEARER_TOKEN_BEDROCK in the backend's env).
    codegen_backend: str = "kimi"
    codegen_model: str = "global.moonshotai.kimi-k3"
    codegen_timeout_s: float = 180.0
    codegen_max_repair_rounds: int = 2

    # Skill Lab (vendored SkillOpt integration). Evaluation/training CLIs from
    # vendor/skillopt run as subprocesses in the dedicated interpreter below
    # (provisioned by bootstrap from vendor/skillopt/requirements-launchpad.txt);
    # the backend process itself never imports the vendored tree. Rollout
    # execution goes to the launchpad_skill_lab_worker AgentCore runtime (the
    # OpenCode CLI, D16); the judge/optimizer role calls Global Bedrock Converse
    # with the hub's API key (bedrock_chat backend).
    skill_lab_python: str = str(DATA_DIR / "skill-lab-venv" / "bin" / "python")
    skill_lab_max_concurrent_jobs: int = Field(default=1, ge=1, le=4)
    # Judge default: GLM 5.3 (D10 — the CN default model); target default: Kimi K3,
    # the model the OpenCode spike validates. Both are Global Bedrock cross-region
    # profile ids (model-credential.md catalog); the bedrock_chat judge rejects bare ids.
    skill_lab_judge_model_id: str = "global.zai.glm-5.3"
    skill_lab_target_model_id: str = "global.moonshotai.kimi-k3"
    # Sandbox launcher argv (shlex-split) for the agentic judge's artifact
    # parsers — runs on the backend HOST (upstream architecture; the worker
    # microVM cannot run bubblewrap). Strict-AppArmor hosts where unprivileged
    # bwrap is blocked set "sudo -n bwrap" (upstream's SKILLOPT_JUDGE_SANDBOX
    # escape hatch); the vendored fail-closed boundary probe still applies.
    skill_lab_judge_sandbox: str = "bwrap"
    # Mirrors the OpenCode CLI version pinned into the exec-worker image. The
    # Dockerfile's `ARG OPENCODE_VERSION` is what the build actually uses (and what
    # the image's content hash covers); this mirrors it for display, with parity
    # asserted in tests/test_skill_lab_foundation.py — bump both together.
    # 1.18.35 is the version the N13 spike validated inside an arm64 container
    # (docs/cn/reports/n13-opencode-spike.md §9); the tarball sha256 is pinned beside
    # it in the Dockerfile.
    skill_lab_worker_opencode_version: str = "1.18.35"

    # Logs Insights SOURCE for every observability query builder (observability.py
    # q_* functions). "both" (default) reads the shared aws/spans destination AND
    # every per-runtime log group — belt-and-suspenders while Transaction Search's
    # availability in cn-northwest-1 is still being confirmed (CN-MIGRATION-ANALYSIS
    # §7: Destination=XRay as of 2026-10-08, ACTIVE once bootstrap flips it to
    # CloudWatchLogs). Narrow to "spans" or "runtime_logs" if one source turns out
    # empty or unavailable in this account/region.
    observability_spans_source: Literal["spans", "runtime_logs", "both"] = "both"

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


# Callables run on every freshly built ``Settings`` before ``get_settings`` caches it. The
# console model-role overrides register one (`app.services.model_roles`), so an override
# survives every ``get_settings.cache_clear()`` (default-workspace resource writes and the
# tests clear the cache at runtime). A registry rather than an import keeps this module free
# of app imports (upstream #275 mechanism).
_SETTINGS_HOOKS: list[Callable[["Settings"], None]] = []


def register_settings_hook(hook: Callable[["Settings"], None]) -> None:
    """Run ``hook`` on every Settings ``get_settings`` builds (idempotent)."""
    if hook not in _SETTINGS_HOOKS:
        _SETTINGS_HOOKS.append(hook)


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    for hook in _SETTINGS_HOOKS:
        hook(settings)
    return settings
