"""Is a spoke account's `LaunchpadWorkspaceRole` stack running the current template?

A spoke stack is deployed and updated by the account's administrator, never by the
hub, so nothing used to tell anyone that a stack had fallen behind the template in
the repo — a stale spoke surfaced only as AccessDenied in whichever feature needed
the newer statement (2026-10-23 Transaction Search, 2026-10-28 knowledge bases).

Both templates in `infra/spoke/` tag the role with `launchpad:template-version`, and
grant it `iam:GetRole` on itself so the hub can read that tag. A stack from before
the tag existed cannot read itself, which is itself the answer: it is outdated.
`backend/tests/test_spoke_template_version.py` keeps `EXPECTED_VERSION` in step with
the templates' content.
"""

import threading
import time
from typing import Any

from botocore.exceptions import BotoCoreError, ClientError

from app.services import aws_clients
from app.services.workspace import WorkspaceContext

EXPECTED_VERSION = 1
VERSION_TAG = "launchpad:template-version"
VARIANT_TAG = "launchpad:role"
# launchpad:role tag value → the template that deploys it.
TEMPLATES = {
    "workspace": "infra/spoke/launchpad-workspace-role.yaml",
    "workspace-prod": "infra/spoke/launchpad-workspace-role-prod.yaml",
}
DEFAULT_STACK_NAME = "launchpad-workspace-role"

CURRENT = "current"
OUTDATED = "outdated"
NEWER = "newer"
NOT_APPLICABLE = "not_applicable"
UNKNOWN = "unknown"

_ACCESS_DENIED = frozenset({"AccessDenied", "AccessDeniedException"})
_TTL_SECONDS = 600.0
_cache: dict[tuple[str, str], tuple[float, dict[str, Any]]] = {}
_lock = threading.Lock()


def stack_command(variant: str | None) -> str:
    """The update the spoke administrator runs. Parameters are left out on purpose:
    `deploy` reuses a stack's existing values for every parameter not overridden,
    which keeps the ExternalId out of anything the console displays."""
    template = TEMPLATES.get(variant or "", TEMPLATES["workspace"])
    return (
        "aws cloudformation deploy"
        f" --template-file {template}"
        f" --stack-name {DEFAULT_STACK_NAME}"
        " --capabilities CAPABILITY_NAMED_IAM"
        " --profile <spoke-admin> --region <the stack's region>"
    )


def _role_name(role_arn: str) -> str:
    # arn:aws:iam::<account>:role/<optional/path/>name
    return role_arn.rsplit("/", 1)[-1]


def classify(tags: dict[str, str]) -> dict[str, Any]:
    """The verdict for the tags GetRole returned."""
    variant = tags.get(VARIANT_TAG)
    raw = tags.get(VERSION_TAG, "")
    deployed = int(raw) if raw.isdigit() else None
    if deployed is None or deployed < EXPECTED_VERSION:
        status = OUTDATED
    elif deployed > EXPECTED_VERSION:
        status = NEWER
    else:
        status = CURRENT
    return {"status": status, "deployed": deployed, "variant": variant}


def _read(ctx: WorkspaceContext) -> dict[str, Any]:
    assert ctx.role_arn
    try:
        role = ctx.client("iam").get_role(RoleName=_role_name(ctx.role_arn))["Role"]
    except ClientError as exc:
        if aws_clients.is_assume_role_failure(exc):
            raise  # the central handler answers 502 with the trust/ExternalId fix
        code = str(exc.response.get("Error", {}).get("Code", ""))
        if code in _ACCESS_DENIED:
            # Every template since the tag grants GetRole on the role itself.
            return {"status": OUTDATED, "deployed": None, "variant": None,
                    "reason": "unreadable"}
        return {"status": UNKNOWN, "deployed": None, "variant": None, "reason": code}
    except BotoCoreError as exc:
        return {"status": UNKNOWN, "deployed": None, "variant": None,
                "reason": type(exc).__name__}
    tags = {t["Key"]: t["Value"] for t in role.get("Tags") or []}
    return classify(tags)


def check(ctx: WorkspaceContext, *, refresh: bool = False) -> dict[str, Any]:
    """`{status, deployed, expected, variant, template, stack_command, reason?}`.

    Cached per (workspace, role) for ten minutes — the answer changes only when an
    administrator updates the stack, and `refresh` is how the console re-asks then.
    """
    if not ctx.role_arn:
        return {"status": NOT_APPLICABLE, "deployed": None, "expected": EXPECTED_VERSION,
                "variant": None, "template": None, "stack_command": None}
    key = (ctx.id, ctx.role_arn)
    now = time.monotonic()
    with _lock:
        hit = _cache.get(key)
    if hit and not refresh and hit[0] > now:
        return dict(hit[1])
    out = _read(ctx)
    variant = out.get("variant")
    out |= {
        "expected": EXPECTED_VERSION,
        "template": TEMPLATES.get(variant or "", TEMPLATES["workspace"]),
        "stack_command": stack_command(variant),
    }
    if out["status"] != UNKNOWN:  # a transient failure is not worth remembering
        with _lock:
            _cache[key] = (now + _TTL_SECONDS, out)
    return dict(out)


def clear_cache() -> None:
    with _lock:
        _cache.clear()
