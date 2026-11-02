"""Best-effort removal of the S3 artifacts the deploy pipeline wrote for one agent.

What the platform writes per agent into the workspace artifacts bucket, keyed by the
agent's (workspace-unique, live) name — under ``projects/<pid>/`` for an agent of an
isolated project (``project_storage.artifact_key``; a deploy from before the project
was isolated left the unprefixed key, so both are candidates):

* ``agents/<name>/deployment_package.zip`` — the zip_runtime / studio code package
  (converted ``-rt`` twins are zip_runtime agents with their own name);
* ``agents/<name>/byoc_package.zip`` — a BYOC ``code_zip`` package;
* ``agents/<name>/canary/<canary>-<role>.zip`` — Runtime Canary mints (the canary's
  own cleanup keeps the one backing the live version until the agent is gone);
* ``builds/<name>/source.zip`` — the CodeBuild context of a container (方式A) or BYOC
  ``container_source`` agent.

So the agent's prefix ``agents/<name>/`` is listed and emptied, and the single build
key is deleted exactly — ``builds/`` also holds platform sources
(``builds/coding-runtime/``, ``builds/skill-lab-worker/``, CodeBuild's placeholder),
which are never touched. A harness writes nothing per agent (its skills are shared
registry prefixes, content-addressed assistant copies or versioned ``system-skills/``),
and a discovered runtime is not ours. BYOC staged uploads (``byoc/<ws>/<upload_id>/``)
are the BYOC method's own teardown (``byoc_uploads.delete_upload_objects``).

The keys derive from the name, and a deleted agent's name is reusable: when another
live agent of the workspace (any project) holds the name now, every key is its, and
nothing is deleted. No other ledger row reads these objects — a promotion bundle
carries an image digest or a staged upload, never a package, and a release rollback
re-points endpoints of the (now deleted) runtime.

The bucket is versioned: a plain DeleteObject leaves a delete marker, and the prior
versions expire under the bucket's lifecycle. Every failure is logged and swallowed
— an orphaned object must never block deleting an agent.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from app.core.errors import cleanup_error, is_not_found
from app.models.ledger import Agent
from app.services.workspace import WorkspaceContext

logger = logging.getLogger(__name__)

RUNTIME_METHODS = frozenset({"zip_runtime", "studio", "container", "byoc"})
# platform-owned build sources an agent name could collide with (agent names allow
# them): coding/image_build.py, skill_lab/worker_build.py, workspace_bootstrap.py
PLATFORM_KEYS = frozenset({
    "builds/coding-runtime/source.zip",
    "builds/skill-lab-worker/source.zip",
    "builds/placeholder/source.zip",
})
# one path segment, nothing that could widen a prefix
_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


def _project_variants(project_id: str | None, key: str) -> list[str]:
    from app.services.project_storage import s3_prefix

    keys = [f"{s3_prefix(project_id)}{key}"] if project_id else []
    return [*keys, key]


def artifact_locations(agent: Agent) -> tuple[list[str], list[str]]:
    """``(prefixes, exact_keys)`` the platform may have written for ``agent``; both
    empty for a method that writes nothing per agent or a name unsafe as a key."""
    name = str(agent.name or "")
    if agent.method not in RUNTIME_METHODS or not _SAFE_NAME.fullmatch(name):
        return [], []
    project_id = getattr(agent, "project_id", None)
    prefixes = _project_variants(project_id, f"agents/{name}/")
    keys = [k for k in _project_variants(project_id, f"builds/{name}/source.zip")
            if k not in PLATFORM_KEYS]
    return prefixes, keys


def _name_held_elsewhere(agent: Agent) -> bool:
    from app.core.db import SessionLocal
    from app.services.agent_names import live_holder

    db = SessionLocal()
    try:
        holder = live_holder(db, agent.workspace_id, agent.name)
        return holder is not None and holder.id != agent.id
    finally:
        db.close()


def _list(s3: Any, bucket: str, prefix: str) -> list[str]:
    keys: list[str] = []
    kwargs: dict[str, Any] = {"Bucket": bucket, "Prefix": prefix}
    while True:
        page = s3.list_objects_v2(**kwargs)
        keys.extend(str(o["Key"]) for o in page.get("Contents") or [] if o.get("Key"))
        token = page.get("NextContinuationToken")
        if not page.get("IsTruncated") or not token:
            return keys
        kwargs["ContinuationToken"] = token


def delete_agent_artifacts(
    agent: Agent, workspace: WorkspaceContext, s3_client: Any = None
) -> list[str]:
    """Delete the objects listed under the agent's own prefixes / exact keys; returns
    the keys deleted. Never raises."""
    try:
        prefixes, exact = artifact_locations(agent)
        if not prefixes and not exact:
            return []
        bucket = (workspace.resources or {}).get("artifacts_bucket")
        if not bucket:
            return []
        if _name_held_elsewhere(agent):
            logger.info("agent %s: name %r is held by another live agent — its S3 "
                        "artifacts are left in place", agent.id, agent.name)
            return []
        s3 = s3_client or workspace.client("s3")
    except Exception as exc:  # noqa: BLE001 — cleanup must never block a delete
        logger.warning("agent %s: S3 artifact cleanup skipped: %s", agent.id,
                       cleanup_error(exc))
        return []

    found: list[str] = []
    for prefix in prefixes:
        try:
            found.extend(_list(s3, bucket, prefix))
        except Exception as exc:  # noqa: BLE001
            logger.warning("agent %s: could not list s3://%s/%s: %s", agent.id, bucket,
                           prefix, cleanup_error(exc))
    for key in exact:
        try:  # listed, not blindly deleted: no delete marker for a key never written
            found.extend(k for k in _list(s3, bucket, key) if k == key)
        except Exception as exc:  # noqa: BLE001
            logger.warning("agent %s: could not list s3://%s/%s: %s", agent.id, bucket,
                           key, cleanup_error(exc))

    deleted: list[str] = []
    for key in dict.fromkeys(found):
        if key in PLATFORM_KEYS:
            continue
        try:
            s3.delete_object(Bucket=bucket, Key=key)
            deleted.append(key)
        except Exception as exc:  # noqa: BLE001
            if not is_not_found(exc):
                logger.warning("agent %s: S3 artifact s3://%s/%s left behind: %s",
                               agent.id, bucket, key, cleanup_error(exc))
    if deleted:
        logger.info("agent %s: deleted %d S3 artifact(s): %s", agent.id, len(deleted),
                    ", ".join(deleted))
    return deleted
