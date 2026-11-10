"""EnvName suffix — the one rule that lets two Launchpad environments share an account+region.

Distribution extension: `memory_domain_mode=dedicated` selects an environment-owned
OpenSearch domain, admin role and discovery path. The D17 shared behavior described
below remains the default for existing configurations; see docs/cn/distribution.md.

Every fixed physical name the CDK app creates (stack names, role/bucket/repo/project/
function/queue/domain/API names) is passed through `suffixed()`. With the default empty
env name the result is the input itself, byte for byte, so an existing deployment is
unaffected; with `-c env_name=t1` every name gets `-t1` appended at its END, keeping the
`launchpad-` prefix the EC2 ops role's IAM wildcards (`role/launchpad-*`,
`repository/launchpad-*`, `project/launchpad-*`, `launchpad-memory-extract-*`) match on.

The backend (`backend/app/core/naming.py`) and the EC2 template
(`infra/ec2/launchpad-cn-ec2.yaml`, parameter `EnvName`) apply the identical rule and
the identical `ENV_NAME_PATTERN`, so a bad value fails fast in all three layers.

Why at most three characters: the cap was set when every environment owned its own
OpenSearch domain (28-character name limit, `lp-memory-cn-northwest-1-` already 25).
Since D17 the domain is shared and unsuffixed (`launchpad-memory-domain` stack), so the
limit is no longer binding — it is kept as-is because the backend's `Settings.env_name`
and the EC2 template's `AllowedPattern` share the literal regex, and relaxing it is a
separate decision (every other service limit has ample headroom).

Not suffixed, deliberately (D17): the shared domain stack `launchpad-memory-domain`, its
domain `lp-memory-<region>` and FGAC master role `launchpad-memory-admin-<region>`, and
the SSM parameters under `/launchpad/memory-domain/<region>/` — one set per
account+region, used by every environment. OpenSearch *index* names are prefixed per
environment by the backend (`app/core/naming.py::index_name`), never by the CDK app.
"""

import re

ENV_NAME_CONTEXT_KEY = "env_name"
ENV_NAME_MAX_LEN = 3
ENV_NAME_PATTERN = rf"^[a-z0-9]{{0,{ENV_NAME_MAX_LEN}}}$"
_ENV_NAME_RE = re.compile(ENV_NAME_PATTERN)
# D17: "kb" would give an environment the index prefix `launchpad-kb-`, which is the
# default environment's per-KB index pattern on the shared domain. Same set as
# `backend/app/core/naming.py::RESERVED_ENV_NAMES` and the EC2 template's
# `EnvNameIsNotReserved` rule.
RESERVED_ENV_NAMES = frozenset({"kb"})


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


def env_name_from_context(node) -> str:
    """The validated `env_name` CDK context value of `node` (an `App` or `Stack` node)."""
    return validate_env_name(node.try_get_context(ENV_NAME_CONTEXT_KEY))


def memory_domain_env(node) -> str:
    """Empty for the legacy shared domain; the environment for a dedicated domain."""
    mode = node.try_get_context("memory_domain_mode") or "shared"
    if mode not in ("shared", "dedicated"):
        raise ValueError("memory_domain_mode must be shared or dedicated")
    env_name = env_name_from_context(node)
    if mode == "dedicated" and not env_name:
        raise ValueError("A dedicated OpenSearch domain requires a nonempty env_name")
    return env_name if mode == "dedicated" else ""
