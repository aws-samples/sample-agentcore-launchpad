"""EnvName suffix — the backend half of the rule that lets two Launchpad environments
share one account+region (N11).

Distribution extension: `memory_domain_mode=dedicated` selects an environment-owned
OpenSearch domain, admin role and discovery path. The D17 shared behavior described
below remains the default for existing configurations; see docs/cn/distribution.md.

`resource_name(base)` is the ONE function every place that creates or looks up a
fixed `launchpad-*` resource or stack name goes through. With the default empty
`Settings.env_name` it returns `base` unchanged, byte for byte; with
`LAUNCHPAD_ENV_NAME=t1` it appends `-t1` at the END (`launchpad-gw-t1`,
`launchpad-agent-execution-role-cn-northwest-1-t1`), keeping the `launchpad-` prefix the
EC2 ops role's IAM wildcards are scoped to.

The CDK app (`infra/stacks/naming.py`) and the EC2 template (`infra/ec2/
launchpad-cn-ec2.yaml`, parameter `EnvName`) apply the identical rule with the identical
`ENV_NAME_PATTERN`, so a bad value fails fast in all three layers. The pattern caps the
suffix at three characters because the OpenSearch domain name (28 max) in
`lp-memory-cn-northwest-1-<env>` is the binding service limit.

Names that are *not* suffixed, deliberately: S3 key prefixes, the local Registry id,
graft markers / fences in generated code, the account-shared `launchpad-global-bedrock`
model credential (same key for every environment; create-or-rotate idempotent; never
deleted by teardown), and — since D17 — the shared OpenSearch domain stack
`launchpad-memory-domain` / domain `lp-memory-<region>` / its FGAC master role
`launchpad-memory-admin-<region>`, which every environment in the account+region uses.

OpenSearch **index** names are *prefixed* rather than suffixed (D17, `index_name()`):
`launchpad-memory` / `launchpad-jobs` / `launchpad-kb-docs` / `launchpad-kb-<id>` in the
default environment, `launchpad-<env>-memory` / `-jobs` / `-kb-docs` / `-kb-<id>` otherwise,
and the per-environment FGAC role `launchpad_memory_rw[_<env>]` is limited to
`index_patterns()` — so two environments sharing one domain cannot see each other's
data. The same rule is mirrored, byte for byte, into the OpenSearch core block of
`app/services/opensearch_http.py` (and from there into the runtime template and the
extractor Lambda, which read the environment from `LAUNCHPAD_ENV_NAME`);
`tests/test_index_naming.py` pins the two copies together.

This module must not import `app.core.config` at module level — `config.py` imports
`ENV_NAME_PATTERN` from here to validate the setting.
"""

import re

ENV_NAME_ENV_VAR = "LAUNCHPAD_ENV_NAME"
ENV_NAME_MAX_LEN = 3
ENV_NAME_PATTERN = rf"^[a-z0-9]{{0,{ENV_NAME_MAX_LEN}}}$"
_ENV_NAME_RE = re.compile(ENV_NAME_PATTERN)
# D17: an environment called "kb" would name its indices `launchpad-kb-*`, which is
# exactly the default environment's per-KB index pattern (`launchpad-kb-<id>`) — the two
# FGAC roles would overlap. Refused in all three layers (CDK context, `Settings.env_name`,
# the EC2 template's `EnvNameIsNotReserved` rule).
RESERVED_ENV_NAMES = frozenset({"kb"})

# Tag carried by per-agent IAM roles created under a non-empty env name, so
# `scripts/teardown.py` sweeps only its own environment's orphans.
ENV_TAG_KEY = "launchpad:env"

# The shared ECR repository's base name (CDK `AgentsRepo`, workspace `storage` stage,
# and the `resources["ecr_repo"]` fallback the deployers use when the map lacks it).
ECR_REPO_BASE = "launchpad-agents"


def validate_env_name(value: object) -> str:
    """`value` as a validated env name (``None`` → ``""``), or ``ValueError``."""
    text = "" if value is None else str(value)
    if not _ENV_NAME_RE.fullmatch(text):
        raise ValueError(
            f"env_name {text!r} is invalid: it must match {ENV_NAME_PATTERN} "
            f"(lowercase letters and digits, at most {ENV_NAME_MAX_LEN} characters)"
        )
    if text in RESERVED_ENV_NAMES:
        raise ValueError(
            f"env_name {text!r} is reserved: its index prefix `launchpad-{text}-` would "
            "overlap the default environment's `launchpad-kb-*` indices (D17)"
        )
    return text


def suffixed(base: str, env_name: str) -> str:
    """`base` unchanged when `env_name` is empty, else ``f"{base}-{env_name}"``."""
    env_name = validate_env_name(env_name)
    return base if not env_name else f"{base}-{env_name}"


def current_env_name() -> str:
    """`Settings.env_name` (``LAUNCHPAD_ENV_NAME``), resolved at call time so a test
    can monkeypatch this one function instead of the settings cache."""
    from app.core.config import get_settings

    return get_settings().env_name


def resource_name(base: str, env_name: str | None = None) -> str:
    """The physical name of the fixed resource `base` in this environment.

    `env_name=None` (the normal call) reads `Settings.env_name`; an explicit value is for
    callers that already hold one (tests, the CDK-context consistency check).
    """
    return suffixed(base, current_env_name() if env_name is None else env_name)


def memory_domain_env() -> str:
    """Environment suffix for a dedicated domain, or empty for legacy sharing."""
    from app.core.config import get_settings

    if get_settings().memory_domain_mode == "shared":
        return ""
    env_name = current_env_name()
    if not env_name:
        raise ValueError("A dedicated OpenSearch domain requires a nonempty env_name")
    return env_name


def default_ecr_repo() -> str:
    return resource_name(ECR_REPO_BASE)


# ── D17: OpenSearch index names and FGAC scope, per environment ───────────────────────

# Base of the per-environment FGAC security role (`memory_bootstrap.py`).
SECURITY_ROLE_BASE = "launchpad_memory_rw"


def _env(env_name: str | None) -> str:
    return current_env_name() if env_name is None else validate_env_name(env_name)


def index_name(base: str, env_name: str | None = None) -> str:
    """The OpenSearch index `base` ("memory", "jobs", "kb-docs", "kb-<id>") of this
    environment on the shared domain: `launchpad-<base>` in the default environment,
    `launchpad-<env>-<base>` otherwise.

    This expression is the ONE rule; `app/services/opensearch_http.py::index_name`
    carries the identical body for the runtime template and the extractor Lambda, which
    cannot import `app.*` (pinned by `tests/test_index_naming.py`).
    """
    env_name = _env(env_name)
    return f"launchpad-{base}" if not env_name else f"launchpad-{env_name}-{base}"


def memory_index(env_name: str | None = None) -> str:
    return index_name("memory", env_name)


def jobs_index(env_name: str | None = None) -> str:
    return index_name("jobs", env_name)


def kb_docs_index(env_name: str | None = None) -> str:
    return index_name("kb-docs", env_name)


def kb_index(kb_id: str, env_name: str | None = None) -> str:
    return index_name(f"kb-{kb_id.lower()}", env_name)


def index_patterns(env_name: str | None = None) -> list[str]:
    """The FGAC `index_patterns` of this environment's security role — everything
    `index_name()` can produce for it and nothing another environment's can.

    The default environment enumerates its three families explicitly instead of
    `launchpad-*` (which would also match every `launchpad-<env>-…` index); a named
    environment owns exactly its `launchpad-<env>-` prefix. The `-` after the env name is
    what keeps `launchpad-t1-*` from matching `launchpad-t10-…`, and `RESERVED_ENV_NAMES`
    is what keeps `launchpad-kb-*` from matching an environment's indices.
    """
    env_name = _env(env_name)
    if not env_name:
        return ["launchpad-memory*", "launchpad-jobs*", "launchpad-kb-*"]
    return [f"launchpad-{env_name}-*"]


def security_role_name(env_name: str | None = None) -> str:
    """`launchpad_memory_rw` in the default environment, `launchpad_memory_rw_<env>`
    otherwise (OpenSearch role names allow `_`, and the base already uses it)."""
    env_name = _env(env_name)
    return SECURITY_ROLE_BASE if not env_name else f"{SECURITY_ROLE_BASE}_{env_name}"
