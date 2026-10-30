"""Session/client factory — the only place boto3 clients are constructed.

Every AWS call in the app resolves its credentials and region here, keyed by the
workspace it runs in, so that targeting a second account/region is a matter of
handing down a different context rather than editing call sites. Sessions are
cached per key because a session carries the credential resolver — for a
cross-account workspace, the refreshable assume-role credentials — that must be
shared across threads.
"""

import logging
import os
import threading
import time
from functools import cache
from typing import TYPE_CHECKING, Any

import boto3
import botocore.session
import urllib3
from botocore.config import Config
from botocore.credentials import (
    AssumeRoleCredentialFetcher,
    DeferredRefreshableCredentials,
    _get_client_creator,
)
from botocore.exceptions import BotoCoreError, ClientError, UnknownRegionError

from app.services.opensearch_retry import DEFAULT_ATTEMPTS, RetryingHttp

if TYPE_CHECKING:  # importing it at runtime would be a cycle: workspace imports this
    from app.services.workspace import WorkspaceContext

SessionKey = tuple[str, str, str | None, str | None]

_SESSIONS: dict[SessionKey, boto3.Session] = {}
_CLIENTS: dict[tuple[SessionKey, str, str | None], Any] = {}
_LOCK = threading.Lock()

# The hub's own credentials are themselves an assumed role, and role chaining caps
# a chained session at one hour regardless of the spoke role's MaxSessionDuration
# — asking for more is rejected. Long jobs survive on refresh, not on duration.
ASSUME_ROLE_DURATION_S = 3600

ASSUME_ROLE_OPERATION = "AssumeRole"
# `AccessDenied` is not a modelled STS exception, so it can only be matched on the
# error code. A wrong ExternalId and a missing trust statement both return it.
_ASSUME_ROLE_DENIED_CODES = frozenset({"AccessDenied", "AccessDeniedException"})

# Passing any of these to botocore would silently override the workspace the
# client claims to target — and the cache key would not reflect it either.
FORBIDDEN_CFG = (
    "region_name",
    "aws_access_key_id",
    "aws_secret_access_key",
    "aws_session_token",
)

# Partition → public DNS suffix. The only place these literals may live.
_DNS_SUFFIX_BY_PARTITION = {
    "aws": "amazonaws.com",  # cn-lint: allow
    "aws-cn": "amazonaws.com.cn",
    "aws-us-gov": "amazonaws.com",  # cn-lint: allow
}


@cache
def partition_for_region(region: str) -> str:
    """`aws` | `aws-cn` | `aws-us-gov` for a region, from botocore's endpoint data.

    A Session is built only to read the endpoint resolver — no credentials are
    touched and nothing is cached in `_SESSIONS`.

    Raises `ValueError` (not botocore's own `UnknownRegionError`) for a region
    botocore's endpoint data does not recognize, so every caller — including the
    ones that build an AWS-agnostic HTTP error on top — sees one exception type
    for "not a real region" rather than having to import a botocore internal.
    """
    try:
        return str(boto3.Session().get_partition_for_region(region))
    except UnknownRegionError as exc:
        raise ValueError(f"unknown AWS region: {region!r}") from exc


def service_principal(service: str) -> str:
    """IAM service-principal name, e.g. ``bedrock-agentcore`` + ``amazonaws.com``.

    Service principals are **partition-invariant**: IAM in aws-cn accepts
    ``bedrock-agentcore`` + ``amazonaws.com`` and rejects
    the ``.com.cn`` form ("Invalid principal in policy", N11-bug5,
    verified live in cn-northwest-1 on 2026-10-10). Only resource ARNs and endpoint
    hostnames carry the partition suffix — use :func:`dns_suffix_for_partition` /
    ``WorkspaceContext.dns_suffix`` for those, never for principals.
    """
    return f"{service}.amazonaws.com"  # cn-lint: allow


def dns_suffix_for_partition(partition: str) -> str:
    return _DNS_SUFFIX_BY_PARTITION.get(partition, _DNS_SUFFIX_BY_PARTITION["aws"])


# Partition → AWS Management Console host. Console hostnames do NOT follow the DNS
# suffix rule (`console.amazonaws.com.cn` does not exist; the China console is
# `console.amazonaws.cn`), so deep links need their own table — the only place these
# literals may live, like the suffix table above.
_CONSOLE_HOST_BY_PARTITION = {
    "aws": "console.aws.amazon.com",  # cn-lint: allow
    "aws-cn": "console.amazonaws.cn",
    "aws-us-gov": "console.amazonaws-us-gov.com",
}


def console_host_for_partition(partition: str) -> str:
    """Management Console host for a partition (`console.amazonaws.cn` in aws-cn).
    A regional console URL is ``https://{region}.{host}/…``."""
    return _CONSOLE_HOST_BY_PARTITION.get(partition, _CONSOLE_HOST_BY_PARTITION["aws"])


# ── Global Bedrock (spec C3/C4) ───────────────────────────────────────────────
# The workspaces live in aws-cn; the models live in the aws partition and are reached
# with a Global Bedrock API key, which the hub process holds only as this env var.
# botocore selects bearer auth for signing name "bedrock" when it is set
# (botocore.utils.get_token_from_environment) and reads it when the client is CREATED.
GLOBAL_BEDROCK_BEARER_ENV = "AWS_BEARER_TOKEN_BEDROCK"
_GLOBAL_BEDROCK_CLIENTS: dict[str, Any] = {}


def require_global_bedrock_bearer() -> str:
    token = os.environ.get(GLOBAL_BEDROCK_BEARER_ENV, "").strip()
    if not token:
        raise RuntimeError(
            f"{GLOBAL_BEDROCK_BEARER_ENV} is not set — the hub reaches Global Bedrock "
            "(GLM 5.3 / Kimi K3) with a Bedrock API key, never with its aws-cn credentials"
        )
    return token


def global_bedrock_runtime_client(region: str) -> Any:
    """`bedrock-runtime` client for the GLOBAL partition, bearer-authenticated.

    Deliberately not keyed by a `WorkspaceContext`: this call must never sign with a
    workspace's (aws-cn) credentials. `region` is a Global region
    (`Settings.model_region`, us-west-2). `signature_version="bearer"` is pinned so a
    missing token fails here, loudly, instead of silently falling back to SigV4.
    """
    require_global_bedrock_bearer()
    with _LOCK:
        cached = _GLOBAL_BEDROCK_CLIENTS.get(region)
        if cached is None:
            cached = boto3.Session(region_name=region).client(
                "bedrock-runtime", config=Config(signature_version="bearer")
            )
            _GLOBAL_BEDROCK_CLIENTS[region] = cached
        return cached


# N12 (plan §B.2, risk R-3): the ONLY constructor of a strands chat model (`BedrockModel` /
# `OpenAIModel`) in the backend. Judges, the actor simulator, insight detectors and
# optimizers all receive the object it returns; a `model=None` / `str` anywhere downstream
# would let strands build a default Claude model with the hub's aws-cn credentials and fail
# without the bearer.
# Judge-client bounds (Round 3 follow-up): a stalled call ends within about 1–2× the judge
# deadline — strands_eval.judge_client (shared with the eval worker) holds the values; a
# caller's own `read_timeout_s` still wins.


def require_provider_api_key(provider: str) -> str:
    """Historical name of `provider_api_key` (N-R6) — every hub-side OpenAI-compatible
    caller (`_openai_compatible_model`, M1-d's embedder) may use either."""
    return provider_api_key(provider)


# ── D21 (M1-b): the hub's own copy of a registry provider's API key ────────────
# The admin registers a provider's key by NAMING an SSM SecureString parameter
# (`launchpad-<name>-api-key`, plan §4); the hub reads it itself through the `ssm` funnel
# entry below, hands the value once to AgentCore Identity (`services/model_credential.py`)
# and keeps a process-memory copy for its own chat/embedding calls. Nothing here logs,
# returns in an error, or persists a value — only parameter / variable NAMES.
PROVIDER_API_KEY_TTL_S = 600.0
# {(provider name, registry row id, SSM parameter name): (value, monotonic expiry)} — process
# memory only. N17-obs226 (rev-r20 B2): keyed by the ROW and the PARAMETER, not the name alone —
# a provider deleted and re-created under the same name (by anyone) is a new row with its own
# parameter and never inherits the old row's cached key; `invalidate_provider_api_key(name)`
# drops every entry of that name and the registry calls it on insert / delete / host change.
# rev-r22 (a): + the row's base_url, so a host change is a cache miss by construction
_PROVIDER_API_KEYS: dict[tuple[str, str | None, str, str], tuple[str, float]] = {}


def ssm_client(ctx: "WorkspaceContext", *, fresh: bool = False) -> Any:
    """`ssm` client in ``ctx``'s account/region — the credential flow's GetParameter reads
    (the EC2 ops role's `ReadLaunchpadSecretParameters` grant is the `launchpad-*-api-key`
    pattern). Built here so the funnel guard (`tests/test_client_funnel.py`) keeps holding.

    ``fresh=True`` (N17-obs232) builds an UNCACHED client off the same cached session — the
    same credential resolver (the hub's ambient chain: the instance role on a host), a new
    botocore client object — for the one retry of a refused best-effort `AddTagsToResource`.
    The cached client is never bound to another credential: the only token the environment
    carries (`AWS_BEARER_TOKEN_BEDROCK`) is scoped by botocore to the `bedrock` signing name
    (`botocore.tokens.ScopedEnvTokenProvider`) and is never consulted for `ssm`."""
    if fresh:
        return client("ssm", ctx, config=Config(user_agent_extra="launchpad-ssm-retry"))
    return client("ssm", ctx)


# The principal-token signer (stream A1) runs on every turn's critical path: a slow or
# unreachable KMS must cost at most ~1 s connect + 2 s read, twice, before the turn goes on
# without a token (D2). botocore's `max_attempts` counts RETRIES (2 → 3 calls, ~8 s against
# a hung endpoint); `total_max_attempts` counts calls, so 2 here is two attempts in total.
# `cache_token` keeps the one configured client cached.
PRINCIPAL_KMS_CONFIG = Config(connect_timeout=1, read_timeout=2,
                              retries={"total_max_attempts": 2, "mode": "standard"})


def principal_kms_client(ctx: "WorkspaceContext") -> Any:
    """The `kms` client of the principal-token signer — bounded timeouts and retries."""
    return client("kms", ctx, cache_token="principal-kms", config=PRINCIPAL_KMS_CONFIG)


def invalidate_provider_api_key(provider_name: str | None = None) -> None:
    """Drop the cached key for one provider (``None`` = all) — called by the credential
    flow after a registration / rotation so the next hub call reads the new value, and by the
    registry when a provider row is inserted, deleted or re-pointed at another host
    (N17-obs226)."""
    with _LOCK:
        if provider_name is None:
            _PROVIDER_API_KEYS.clear()
        else:
            for key in [k for k in _PROVIDER_API_KEYS if k[0] == provider_name]:
                _PROVIDER_API_KEYS.pop(key, None)


def cached_provider_api_key_rows(provider_name: str) -> list[tuple[str | None, str]]:
    """The (row id, parameter name) pairs currently cached for ``provider_name`` — a test /
    diagnostics view; never the values."""
    with _LOCK:
        return [(k[1], k[2]) for k in _PROVIDER_API_KEYS if k[0] == provider_name]


def provider_api_key(provider_name: str) -> str:
    """The hub's own copy of ``provider_name``'s API key (plan §4).

    Resolution: a Bedrock provider → `require_global_bedrock_bearer()`; otherwise, when the
    REGISTRY ROW holds a registered key (`ProviderRow.hub_holds_key(host_env=False)` — the
    row's key facts, written by `POST /credential`, a console-typed key and the bootstrap's
    `model-providers` stage on the hub; N17-obs280: the row is the hub's single source, no
    workspace `model_credentials` record is read here), the SSM parameter is read through
    `ssm_client` and cached for `PROVIDER_API_KEY_TTL_S` (a rotation lands after the TTL or
    at once via `invalidate_provider_api_key`); the provider's environment variable is the
    fallback (the host-boot path). The SSM read is deliberately gated on the row rather than
    attempted for every row: an unregistered provider must fail fast and offline — a stray
    GetParameter from a hermetic test (or a hub without the IAM grant yet) is a live call we
    never want. A deleted provider's same-named successor (obs229) and a row whose host
    changed (obs254: `update_provider` resets the key facts in the same commit as the new
    `base_url`) hold no key facts, whatever a clearing step's outcome.

    Raises `RuntimeError` naming the SSM parameter and the variable — never a value.
    """
    from app.schemas import model_catalog as mc

    row = mc.current().providers.get(provider_name)
    if row is None:
        raise RuntimeError(
            f"unknown model provider {provider_name!r} — it is not in the model registry "
            "(Settings → Model Providers)"
        )
    if row.kind == mc.PROVIDER_BEDROCK:
        return require_global_bedrock_bearer()
    now = time.monotonic()
    ssm_name = str(row.ssm_parameter or "")
    env_name = str(row.api_key_env or "")
    # rev-r22 (a): the host is part of the key — a value cached under the old snapshot can
    # never be served for a row re-pointed at another host, whatever the invalidation did
    cache_key = (provider_name, row.id, ssm_name, str(row.base_url or ""))
    # N17-obs229 / obs254 / obs280: the row alone licenses the read (see the docstring) — and
    # it gates the CACHE too (rev-r20 nit): a cached value is an SSM read, so a row without key
    # facts is never served one, whoever filled the entry and whatever invalidation was missed
    registered = row.hub_holds_key(host_env=False)
    if registered:
        with _LOCK:
            cached = _PROVIDER_API_KEYS.get(cache_key)
        if cached is not None and cached[1] > now:
            return cached[0]
    ssm_note = ""
    if ssm_name and registered:
        from app.services.model_credential import read_ssm_parameter
        from app.services.workspace import default_workspace_context

        try:
            secret = read_ssm_parameter(ssm_client(default_workspace_context()), ssm_name)
        except ClientError as exc:
            code = str(exc.response.get("Error", {}).get("Code") or "ClientError")
            ssm_note = f"; SSM parameter {ssm_name} could not be read ({code})"
        except (BotoCoreError, ValueError, RuntimeError) as exc:
            ssm_note = f"; SSM parameter {ssm_name} could not be read ({type(exc).__name__})"
        else:
            with _LOCK:
                _PROVIDER_API_KEYS[cache_key] = (secret.value, now + PROVIDER_API_KEY_TTL_S)
            return secret.value
    token = os.environ.get(env_name, "").strip() if env_name else ""
    if token:
        return token
    if ssm_name and not registered:
        # never registered, cleared on a host change (obs254) or a deleted row's successor
        raise RuntimeError(
            f"the provider row holds no registered key — SSM parameter {ssm_name} is not read "
            f"and {env_name or 'the provider API key variable'} is not set on the hub; enter "
            f"the {row.label} key under Settings → Model Providers (or store it as that SSM "
            "SecureString parameter and register it, deploy.md §1 / §8.6), or export the "
            "variable on the host"
        )
    raise RuntimeError(
        f"{env_name or 'the provider API key variable'} is not set — the hub reaches "
        f"{row.label} ({row.base_url}) with its API key; store it as SSM SecureString "
        f"parameter {ssm_name or 'launchpad-<provider>-api-key'} and register it under "
        "Settings → Model Providers (deploy.md §1 / §8.6), or export the variable on the "
        f"host{ssm_note}"
    )


def build_chat_model(
    model_id: str,
    *,
    region: str,
    max_tokens: int = 1024,
    temperature: float = 0.0,
    read_timeout_s: int | None = None,
    principal: Any = None,
    **model_config: Any,
) -> Any:
    """The ONE strands chat-model constructor on the hub, dispatching on the catalog's
    provider (N-R6): a Bedrock model → `strands.models.BedrockModel` on the GLOBAL
    partition, bearer-authenticated; an OpenAI-compatible model → `strands.models.openai.
    OpenAIModel` on the provider's endpoint with the hub's key for that provider.

    ``model_id`` must be in the CN catalog (422 ``model.not_allowed`` otherwise) and
    ``max_tokens`` is floored at the catalog minimum (spec C3). Inference fields the catalog
    says the model rejects are dropped (N12-bug11). Strands builds the `bedrock-runtime` /
    OpenAI client itself from what is handed in here — so the construction still happens
    from this file and never with workspace credentials.

    ``principal`` (N17-obs227, a `schemas.model_catalog.Principal`): the console account the
    model is built FOR — a model of another member's personal provider is refused first with
    422 ``models.model_not_visible`` (the hub would spend that member's key). ``None`` (the
    worker, SCORE NOW's in-process judge, the optimizer — callers without a request) keeps
    every row: their model ids were already held to the caller's visibility where they were
    named (evaluator create / update, plan approval, recommendation start, run actor).
    """
    from app.core.errors import AppError
    from app.schemas.agent import (
        MIN_MODEL_MAX_TOKENS,
        assert_model_available,
        assert_model_visible,
        hub_model_refusal_code,
        is_openai_compatible_model,
        supported_model_config,
    )

    # visibility (N17-obs227) first, then catalog AND a configured provider (N-R6-obs1): an
    # unconfigured provider is a 422 `model.provider_unconfigured` naming the SSM parameter,
    # not a RuntimeError later
    try:
        assert_model_visible(model_id, principal, where="judge model_id")
        assert_model_available(model_id, where="judge model_id")
    except ValueError as exc:
        raise AppError(hub_model_refusal_code(exc), str(exc), {"model_id": model_id},
                       status_code=422) from exc
    # N12-bug11: a field the catalog says this model rejects (Kimi K3: temperature) is
    # dropped here rather than sent — the worker's `_launchpad_*_model` do the same.
    inference = supported_model_config(model_id, {
        "max_tokens": max(int(max_tokens), MIN_MODEL_MAX_TOKENS),
        "temperature": temperature,
        **model_config,
    })
    # N17-obs302: every tool spec the model is handed (judges' structured output, actors'
    # tools) is normalized for strict providers — the runtimes' `_launchpad_chat_model` twin
    from app.templates.tool_schema import launchpad_normalize_model_tool_schemas

    if is_openai_compatible_model(model_id):
        return launchpad_normalize_model_tool_schemas(
            _openai_compatible_model(model_id, inference, read_timeout_s=read_timeout_s)
        )
    from strands.models import BedrockModel

    require_global_bedrock_bearer()
    from app.services.strands_eval.judge_client import (
        JUDGE_READ_TIMEOUT_S,
        bedrock_client_config,
    )

    config = bedrock_client_config(read_timeout_s=read_timeout_s or JUDGE_READ_TIMEOUT_S,
                                   signature_version="bearer")
    return launchpad_normalize_model_tool_schemas(BedrockModel(
        region_name=region,
        boto_client_config=config,
        model_id=model_id,
        **inference,
    ))


def _openai_compatible_model(model_id: str, inference: dict[str, Any], *,
                             read_timeout_s: int | None) -> Any:
    """`OpenAIModel` for a catalog model on an OpenAI-compatible endpoint: the endpoint and
    the id the endpoint receives come from the catalog row, the key from the hub's env for
    that provider (never from the Bedrock bearer). `params` is the chat-completions body."""
    from strands.models.openai import OpenAIModel

    from app.schemas.agent import model_catalog_table
    from app.services.strands_eval.judge_client import openai_client_limits

    entry = model_catalog_table()[model_id]
    api_key = require_provider_api_key(str(entry["provider"]))
    return OpenAIModel(
        client_args={
            "base_url": entry["base_url"],
            "api_key": api_key,
            **openai_client_limits(read_timeout_s),
        },
        model_id=str(entry["api_model_name"]),
        params=dict(inference),
        stream=bool(entry["streaming"]),
    )


def global_bedrock_model(model_id: str, *, region: str, **kwargs: Any) -> Any:
    """Historical name of `build_chat_model` — every judge / actor / optimizer caller uses
    it; kept as a thin alias so the N-R6 provider dispatch reaches them unchanged."""
    return build_chat_model(model_id, region=region, **kwargs)


def global_bedrock_openai_base_url(region: str) -> str:
    """OpenAI-compatible Chat Completions root of Global Bedrock in `region`
    (the Studio AI-fix backend speaks this; the bearer is the same API key)."""
    suffix = dns_suffix_for_partition(partition_for_region(region))
    return f"https://bedrock-runtime.{region}.{suffix}/openai/v1"


# --- CN memory/KB (N6) ---
# OpenSearch Service has no boto3 data-plane client; the self-hosted Memory/KB backends
# sign REST calls themselves (SigV4, service "es"). Built here so the credentials come
# from the workspace's session — the FGAC master role when `memory_admin_role_arn` is set
# on the context (ruling R13) — and so tests can swap the whole client for a fake.
_OPENSEARCH_HTTP: dict[tuple[SessionKey, str, int, float], Any] = {}
# The pool parameters are UNCHANGED from the pre-existing `OpenSearchHttp.__init__`
# default (`urllib3.Timeout(connect=5.0, read=30.0), retries=False`): N-SCN-obs6 only
# moved the construction here so the pool can be wrapped in `RetryingHttp`, which owns the
# retry policy (one place, hermetically testable; urllib3-level retries stay off so the two
# policies never stack). The 30 s read timeout is what the FGAC security API on a t3.small
# domain can take.
OPENSEARCH_CONNECT_TIMEOUT_SECONDS = 5.0
OPENSEARCH_READ_TIMEOUT_SECONDS = 30.0
# Connections KEPT per host (N17-obs238). urllib3's default is ONE: with `block=False`
# (urllib3's default, written out) a request that finds no idle connection opens its own —
# nothing ever queues in front of the read timeout — but when more connections return than
# the pool keeps, the surplus is closed and urllib3 logs "Connection pool is full,
# discarding connection" (twice in the 2026-10-21 forensics, while the console polled a KB
# concurrently: documents list, `/query`, `/sync`, the detail's job lookup); the next
# request then pays a fresh TLS handshake against the busy t3.small domain, which was part
# of the four read timeouts and four 502s. The interactive tier is one pool shared by the
# KB and the memory consoles (same session, endpoint, budget and timeout), so eight keeps
# both consoles' concurrent polls on warm connections; past eight the warning is back
# (harmless, a handshake — never a queue). Hub only: the Lambda's mirrored `OpenSearchHttp`
# default pool (`backend/lambdas/**`) is untouched.
OPENSEARCH_POOL_MAXSIZE = 8
# Retries are logged (message carries method, URL, failure type and attempt — never a
# body) under this logger so a flaky domain is visible before the budget is exhausted.
_OPENSEARCH_RETRY_LOG = logging.getLogger("app.services.opensearch_retry")


def _opensearch_pool(
    read_timeout: float = OPENSEARCH_READ_TIMEOUT_SECONDS, *,
    maxsize: int = OPENSEARCH_POOL_MAXSIZE,
) -> Any:
    return urllib3.PoolManager(
        timeout=urllib3.Timeout(connect=OPENSEARCH_CONNECT_TIMEOUT_SECONDS, read=read_timeout),
        retries=False,
        maxsize=maxsize,
        block=False,
    )


def opensearch_http(
    ctx: "WorkspaceContext",
    endpoint: str,
    *,
    attempts: int = DEFAULT_ATTEMPTS,
    read_timeout: float = OPENSEARCH_READ_TIMEOUT_SECONDS,
) -> Any:
    """The one SigV4 OpenSearch client per (session, endpoint, retry budget, read timeout).

    ``attempts`` is the caller's retry budget and ``read_timeout`` (seconds) the urllib3
    read timeout of the pool behind it; both are part of the cache key. The bootstrap
    stages (`services/bootstrap.py`, `workspace_bootstrap.py`) keep the defaults (3 attempts,
    30 s — what the FGAC security API on a t3.small domain can take); the console's
    request-thread callers (`memory_selfhosted`, `knowledge_selfhosted`) pass
    `opensearch_retry.INTERACTIVE_ATTEMPTS` (2) and
    `opensearch_retry.INTERACTIVE_READ_TIMEOUT_SECONDS` (10 s) so a dead or busy domain
    holds a request thread for at most ~21 s (2 × 10 s + 1 s backoff) instead of ~93 s
    (N-SCN-obs11: a 30 s read timeout on a poll turned a busy node into a 34 s stall).
    """
    from app.services.opensearch_http import OpenSearchHttp

    key: SessionKey = (ctx.account_id, ctx.region, ctx.role_arn, ctx.external_id)
    cache_key = (key, endpoint, attempts, float(read_timeout))
    with _LOCK:
        cached = _OPENSEARCH_HTTP.get(cache_key)
        if cached is None:
            def _creds(key=key):
                return get_session(*key).get_credentials().get_frozen_credentials()
            cached = OpenSearchHttp(
                endpoint, ctx.region, _creds,
                http=RetryingHttp(
                    _opensearch_pool(read_timeout), attempts=attempts,
                    log=_OPENSEARCH_RETRY_LOG.warning,
                ),
            )
            _OPENSEARCH_HTTP[cache_key] = cached
        return cached
# --- end CN memory/KB (N6) ---


def get_session(
    account_id: str,
    region: str,
    role_arn: str | None = None,
    external_id: str | None = None,
) -> boto3.Session:
    """Cached session for one (account, region, role) target.

    ``role_arn=None`` means the hub's own ambient credentials; a ``role_arn``
    means the hub assumes that role in the spoke account (``external_id`` is the
    workspace's shared secret with the spoke's trust policy).

    The cache is what makes assumed credentials correct, not just cheap: one
    session per key owns exactly one credentials object, and the refresh lock
    lives on that object. Two sessions for one target would refresh independently
    and neither would serialise the other, so ``reset_cache()`` must not run
    while a job is in flight.
    """
    key: SessionKey = (account_id, region, role_arn, external_id)
    with _LOCK:
        session = _SESSIONS.get(key)
        if session is None:
            session = (
                _assumed_role_session(account_id, region, role_arn, external_id)
                if role_arn
                else boto3.Session(region_name=region)
            )
            _SESSIONS[key] = session
        return session


def _role_session_name(account_id: str, region: str) -> str:
    """What the spoke account's CloudTrail records this hub as.

    The workspace id would read better, but a session is cached per
    (account, region, role) and does not know which workspace asked for it — and
    the UNIQUE(account_id, region) constraint makes this pair name the workspace
    just as precisely. Always within the 64-char / ``[\\w+=,.@-]*`` STS limits.
    """
    return f"launchpad-{account_id}-{region}"


def _assumed_role_session(
    account_id: str, region: str, role_arn: str, external_id: str | None
) -> boto3.Session:
    """A session whose credentials come from ``sts:AssumeRole``, auto-refreshing.

    Refresh matters operationally, so the semantics botocore gives us:

    * no AssumeRole call happens here — the first request that *signs* triggers it;
    * with more than 900 s left, a failed refresh is swallowed and the still-valid
      credentials are served (a transient STS error cannot kill a long job);
    * with less than 600 s left, a failed refresh propagates verbatim, at whatever
      call site happened to be signing — which is why `AssumeRole` failures are
      mapped centrally (`app/core/errors.py`) rather than at one route.

    Two private botocore APIs are used deliberately. ``inner._credentials``: the
    public ``set_credentials`` only takes static keys and cannot install a
    refreshable object; every cross-account recipe assigns this attribute and it
    has been stable across botocore 1.x. ``_get_client_creator``: the fetcher
    builds its STS client with no region of its own, so a plain
    ``session.create_client`` would sign against the *hub's* region — this is the
    helper botocore passes to its own AssumeRoleProvider, and it pins STS to the
    spoke's region (required for opt-in regions).
    """
    hub = botocore.session.get_session()
    source = hub.get_credentials()
    if source is None:
        # The fetcher would keep the None and fail at the first signed request with
        # an AttributeError from inside botocore, naming neither the hub nor the role.
        raise RuntimeError(
            "this backend has no AWS credentials of its own, so it cannot assume "
            f"{role_arn} — a cross-account workspace needs the hub's own role first"
        )
    extra_args: dict[str, Any] = {
        "RoleSessionName": _role_session_name(account_id, region),
        "DurationSeconds": ASSUME_ROLE_DURATION_S,
    }
    if external_id:
        extra_args["ExternalId"] = external_id
    fetcher = AssumeRoleCredentialFetcher(
        client_creator=_get_client_creator(hub, region),
        source_credentials=source,
        role_arn=role_arn,
        extra_args=extra_args,
    )
    inner = botocore.session.Session()
    inner._credentials = DeferredRefreshableCredentials(
        refresh_using=fetcher.fetch_credentials, method="assume-role"
    )
    inner.set_config_variable("region", region)
    return boto3.Session(botocore_session=inner)


def probe_caller_identity(
    account_id: str, region: str, role_arn: str, external_id: str | None
) -> str:
    """The account reached by assuming ``role_arn`` — a one-shot access probe.

    Deliberately **not** `get_session`: the arguments are unverified operator
    input from the registration form, and the cache key includes the ExternalId,
    so probing a mistyped one would park a session in the module cache under a key
    nothing will ever ask for again. A probe's credentials also have no business
    becoming the object that later in-flight work shares a refresh lock with.

    Reuse would nonetheless be *correct*, which is worth recording so nobody
    re-derives it from botocore: a `DeferredRefreshableCredentials` whose first
    fetch raises leaves `_frozen_credentials` None, so `refresh_needed()` stays
    True and the next access re-attempts the AssumeRole for real (the fetcher
    caches successes only). A failed probe cannot poison a key the operator then
    fixes and retries.

    Raises the STS `ClientError` verbatim — `is_assume_role_failure` +
    `assume_role_diagnostic` are what turn it into operator-facing text.
    """
    session = _assumed_role_session(account_id, region, role_arn, external_id)
    with _LOCK:
        # This session is private to the probe, but client construction in this
        # module always happens under the lock; a probe is not the place to make
        # that rule conditional.
        sts = session.client("sts")
    return str(sts.get_caller_identity().get("Account") or "")


def is_assume_role_failure(exc: BaseException) -> bool:
    """True for the ClientError STS raises when the hub cannot assume a spoke role.

    It surfaces wherever a request was being signed — a router, a bootstrap stage,
    a background deploy — not at session construction.
    """
    return isinstance(exc, ClientError) and exc.operation_name == ASSUME_ROLE_OPERATION


def assume_role_diagnostic(exc: ClientError) -> str:
    """Operator-actionable text for a failed AssumeRole.

    AccessDenied has three causes worth naming, because STS reports all three
    identically: the trust policy does not name this hub's role, the ExternalId
    does not match the one the spoke's stack was deployed with, or the hub role
    was recreated (AWS pins a role-ARN principal to the role's unique id, so a
    same-named replacement is a different principal).
    """
    error = exc.response.get("Error", {}) if isinstance(exc.response, dict) else {}
    code = str(error.get("Code") or "Unknown")
    message = str(error.get("Message") or "")
    if code in _ASSUME_ROLE_DENIED_CODES:
        cause = (
            "check the spoke role's trust policy (it must name this hub's role), "
            "the workspace's ExternalId (it must match the stack parameter), and "
            "whether either role was recreated"
        )
    else:
        cause = "STS refused the role assumption"
    return f"cannot assume this workspace's role ({code}) — {cause}. {message}".strip()


def client(
    service: str, ctx: "WorkspaceContext", cache_token: str | None = None, **cfg: Any
) -> Any:
    """boto3 client for ``service`` in ``ctx``'s account/region.

    Call sites go through ``WorkspaceContext.client`` rather than here, so that a
    client can never be built without naming the environment it targets.

    ``cfg`` is forwarded to botocore verbatim (e.g. ``config=Config(...)``) and
    normally opts the client out of the cache, since botocore ``Config`` objects
    compare by identity and would grow the cache without ever hitting it. A hot
    call site that passes cfg can supply ``cache_token`` — a string standing in
    for its cfg in the cache key — to get the cached client back.

    Construction always happens under the lock: building clients off a shared
    session is not thread-safe in boto3.
    """
    overrides = [name for name in FORBIDDEN_CFG if name in cfg]
    if overrides:
        raise ValueError(
            f"{', '.join(overrides)} defeat workspace targeting — set the region and "
            "credentials on the WorkspaceContext instead of on the client"
        )
    key: SessionKey = (ctx.account_id, ctx.region, ctx.role_arn, ctx.external_id)
    session = get_session(*key)
    with _LOCK:
        if cfg and cache_token is None:
            return session.client(service, **cfg)
        cached = _CLIENTS.get((key, service, cache_token))
        if cached is None:
            cached = session.client(service, **cfg)
            _CLIENTS[(key, service, cache_token)] = cached
        return cached


class FunnelSession:
    """A stand-in for ``boto3.Session`` for SDK classes that build their own clients.

    The `bedrock_agentcore` tool clients (``CodeInterpreter``) take an optional
    session and construct their control/data-plane clients off it. A real session
    would let them build clients outside this module's lock and cache, which is
    the reason `WorkspaceContext` hands out credentials rather than sessions — so
    they get this instead: a duck-typed session whose ``client()`` is the funnel.

    The SDK passes ``region_name`` explicitly, which `client()` rejects on
    purpose; it is accepted here only when it already agrees with the context,
    and dropped. It also passes a per-instance ``config``, so these clients are
    built fresh (under the lock) rather than served from the cache — right for a
    demo route, and not somewhere to add a `cache_token`. Anything else the SDK
    might reach for (``resource``, ``region_name`` as an attribute) is
    deliberately absent: an AttributeError from a future SDK version is a loud
    failure, whereas silently falling back to ambient credentials in a spoke
    account would not be.
    """

    __slots__ = ("_ctx",)

    def __init__(self, ctx: "WorkspaceContext") -> None:
        self._ctx = ctx

    def client(self, service_name: str, **kwargs: Any) -> Any:
        region = kwargs.pop("region_name", None)
        if region is not None and region != self._ctx.region:
            raise ValueError(
                f"{service_name} was asked for in {region} but this workspace is "
                f"{self._ctx.region}"
            )
        return client(service_name, self._ctx, **kwargs)


def sdk_session(ctx: "WorkspaceContext") -> FunnelSession:
    """Session-shaped funnel handle for an AWS SDK class (see `FunnelSession`)."""
    return FunnelSession(ctx)


def reset_cache() -> None:
    """Drop cached sessions/clients (config reload, tests).

    Not safe to call while work is in flight against a cross-account workspace: a
    dropped session takes its credentials object — and the refresh lock on it —
    with it, so threads still holding the old session and threads getting a new
    one would refresh the same role in parallel.
    """
    with _LOCK:
        _SESSIONS.clear()
        _CLIENTS.clear()
        _GLOBAL_BEDROCK_CLIENTS.clear()
        _OPENSEARCH_HTTP.clear()
        _PROVIDER_API_KEYS.clear()
