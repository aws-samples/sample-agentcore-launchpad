"""Configuration bundles — local rows + immutable S3 copies read by the runtime (N12 E2-6).

The Global ``ConfigurationBundle`` service is gone in ``aws-cn`` (plan CA-2/CA-3). A bundle
version is one ``config_bundles`` row (``components.configuration = {system_prompt, tools:
{<name>: {description}}}``) mirrored byte-for-byte to
``s3://<artifacts>/bundles/<agent_id>/<bundle_id>/v<n>.json`` — never overwritten: a new
prompt is a new version. The runtime reads it through the request pointer
``launchpad.bundle=<bundle_id>:<version>`` that travels in three channels (CA-2):

1. the ``baggage`` request header (primary; the SDK's experiment keys ride along so
   ``BaggageSpanProcessor`` stamps every span with the arm),
2. the ``X-Amzn-Bedrock-AgentCore-Runtime-Custom-Launchpad-Bundle`` header,
3. ``payload["launchpad"]["bundle"]`` (guaranteed — it is our JSON).

:data:`RUNTIME_BUNDLE_READER` is the ONE runtime-side reader; the Strands template, the
Harness-export graft and the Studio shim embed it verbatim (``tests/test_bundle_template.py``
pins the three copies). The ``harness_optimizer`` worker job writes its candidate
versions with the same key layout (``bundle_key``) and routes its rollouts with
:func:`baggage_for`.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.db import SessionLocal
from app.core.errors import AppError, NotFoundError
from app.optimization.models import ConfigBundle
from app.services.workspace import WorkspaceContext

BUNDLE_PREFIX = "bundles"
BAGGAGE_KEY = "launchpad.bundle"
EXPERIMENT_ARN_KEY = "aws.agentcore.gateway.routing_experiment_arn"
EXPERIMENT_VARIANT_KEY = "aws.agentcore.gateway.routing_experiment_variant_name"
BUNDLE_HEADER = "X-Amzn-Bedrock-AgentCore-Runtime-Custom-Launchpad-Bundle"
BUNDLE_BUCKET_ENV = "LAUNCHPAD_BUNDLE_BUCKET"  # set by deployer/environment.py (W-E3)
BUNDLE_AGENT_ENV = "LAUNCHPAD_AGENT_ID"
READER_START = "# <launchpad-bundle-reader:v2>"
READER_END = "# </launchpad-bundle-reader:v2>"

# The runtime-side reader (plain Python 3.13, stdlib + boto3 + the AgentCore SDK). Kept
# alias-free so every host embeds the SAME text; the host's public helper
# (`_config_bundle` / `_launchpad_config_bundle` / `launchpad_config_bundle`) wraps
# `_launchpad_bundle_ref()` + `_launchpad_fetch_bundle()` in a try/except → {}.
RUNTIME_BUNDLE_READER = f'''\
{READER_START}
# N12 CA-2: the pointer `launchpad.bundle=<bundle_id>:<version>` of this request → the
# immutable S3 copy `bundles/<agent_id>/<bundle_id>/v<version>.json` → components.configuration
import json as _lp_json
import logging as _lp_logging
import os as _lp_os
import urllib.parse as _lp_url
from contextvars import ContextVar as _LPContextVar

import boto3 as _lp_boto3
from bedrock_agentcore.runtime.context import BedrockAgentCoreContext as _LPBundleContext

LAUNCHPAD_BUNDLE_HEADER = "x-amzn-bedrock-agentcore-runtime-custom-launchpad-bundle"
LAUNCHPAD_BUNDLE_BAGGAGE_KEY = "launchpad.bundle"
_launchpad_bundle_ref_var = _LPContextVar("launchpad_bundle_ref", default=None)
_launchpad_bundle_cache = {{}}
_launchpad_bundle_logger = _lp_logging.getLogger("launchpad.bundle")
_launchpad_bundle_logger.setLevel(_lp_logging.INFO)


def _launchpad_bundle_log(level, message):
    """One line per fetch outcome (N12-obs37). The line names key / version / byte size /
    exception class only — never the bundle body. A stderr handler of our own is attached
    only when the host configured no logging at all (Python's last-resort handler drops INFO)."""
    if not _launchpad_bundle_logger.hasHandlers():
        handler = _lp_logging.StreamHandler()
        handler.setFormatter(_lp_logging.Formatter("%(levelname)s %(message)s"))
        _launchpad_bundle_logger.addHandler(handler)
    _launchpad_bundle_logger.log(level, message)


def _launchpad_capture_bundle_ref(payload):
    """Channel 3 (payload alias): call once at the entrypoint with the request payload."""
    ref = None
    if isinstance(payload, dict) and isinstance(payload.get("launchpad"), dict):
        ref = payload["launchpad"].get("bundle")
    _launchpad_bundle_ref_var.set(str(ref).strip() if ref else None)


def _launchpad_bundle_ref():
    """The bundle pointer of this request: payload alias → Custom header → baggage."""
    ref = _launchpad_bundle_ref_var.get()
    if ref:
        return ref
    try:
        headers = _LPBundleContext.get_request_headers() or {{}}
    except Exception:
        headers = {{}}
    for key, value in headers.items():
        if str(key).lower() == LAUNCHPAD_BUNDLE_HEADER and value:
            return str(value).strip()
    for key, value in headers.items():
        if str(key).lower() != "baggage" or not value:
            continue
        for entry in str(value).split(","):
            name, _, raw = entry.strip().partition("=")
            if name.strip() == LAUNCHPAD_BUNDLE_BAGGAGE_KEY:
                return _lp_url.unquote(raw.split(";", 1)[0].strip())
    return None


def _launchpad_fetch_bundle(ref):
    """`components.configuration` of the pointed bundle version, cached per ref.

    Fail-soft (a failure means the production prompt, as before) but never silent: one INFO
    line per fetch, one WARNING per failure, one WARNING when the reader cannot run at all.
    """
    cached = _launchpad_bundle_cache.get(ref)
    if cached is not None:
        return cached
    bundle_id, _, version = str(ref).rpartition(":")
    agent_id = _lp_os.environ.get("LAUNCHPAD_AGENT_ID", "")
    if not agent_id:
        _launchpad_bundle_log(
            _lp_logging.WARNING,
            "launchpad.bundle: bundle reader disabled: LAUNCHPAD_AGENT_ID not set (ref=%s)" % ref,
        )
        return {{}}
    key = "bundles/%s/%s/v%s.json" % (agent_id, bundle_id, version)
    try:
        body = _lp_boto3.session.Session().client("s3").get_object(
            Bucket=_lp_os.environ.get("LAUNCHPAD_BUNDLE_BUCKET", ""), Key=key
        )["Body"].read()
        components = (_lp_json.loads(body) or {{}}).get("components") or {{}}
        configuration = dict(components.get("configuration") or {{}})
    except Exception as exc:  # NoSuchKey / AccessDenied / timeout / bad JSON — all fail-soft
        detail = " ".join(str(exc).split())[:200]
        _launchpad_bundle_log(
            _lp_logging.WARNING,
            "launchpad.bundle: fetch failed key=%s error=%s: %s"
            % (key, type(exc).__name__, detail),
        )
        return {{}}
    _launchpad_bundle_log(
        _lp_logging.INFO,
        "launchpad.bundle: fetched key=%s bytes=%d version=%s" % (key, len(body), version),
    )
    _launchpad_bundle_cache[ref] = configuration
    return configuration
{READER_END}
'''


# ─── identities / keys ───────────────────────────────────────────────────────


def new_bundle_id() -> str:
    return "bndl_" + uuid.uuid4().hex[:12]


def bundle_key(agent_id: str, bundle_id: str, version: int) -> str:
    return f"{BUNDLE_PREFIX}/{agent_id}/{bundle_id}/v{int(version)}.json"


def bundle_ref(bundle_id: str, version: int) -> str:
    return f"{bundle_id}:{int(version)}"


def parse_ref(ref: str) -> tuple[str, int]:
    bundle_id, sep, version = str(ref).rpartition(":")
    if not sep or not bundle_id or not version.isdigit():
        raise ValueError(f"not a bundle ref (<bundle_id>:<version>): {ref!r}")
    return bundle_id, int(version)


def components_for(
    system_prompt: str | None, tool_descriptions: dict[str, str] | None,
) -> dict[str, Any]:
    """The Global ``components`` shape the runtime's ``resolve_tool_description`` reads."""
    configuration: dict[str, Any] = {}
    if system_prompt is not None:
        configuration["system_prompt"] = str(system_prompt)
    configuration["tools"] = {
        str(name): {"description": str(text)}
        for name, text in sorted((tool_descriptions or {}).items())
    }
    return {"configuration": configuration}


def _encode(value: str) -> str:
    # W3C baggage: values percent-encoded; `,` `;` `=` and whitespace never travel raw
    return quote(str(value), safe="-_.~:/")


def baggage_for(ref: str, experiment_id: str, arm: str) -> str:
    """The W3C ``baggage`` header for a bundle arm — the bundle pointer plus the SDK's
    experiment keys. One implementation: W-E3's ``routing.baggage_for`` (test-pinned)."""
    from app.optimization import routing  # local — routing is E3's and imports the ledger

    return routing.baggage_for(ref, experiment_id, arm)


def launchpad_meta(ref: str, experiment_id: str | None = None, arm: str | None = None) -> dict:
    """``payload["launchpad"]`` — the guaranteed alias channel (CA-2)."""
    meta: dict[str, Any] = {"bundle": ref}
    if experiment_id:
        meta["experiment"] = experiment_id
    if arm:
        meta["variant"] = arm
    return meta


# ─── rows + S3 copies ───────────────────────────────────────────────────────


def _ws_id(ws: Any) -> str:
    return str(ws.id)


def _ctx(ws: Any) -> WorkspaceContext:
    return ws.context if hasattr(ws, "context") else ws


@contextmanager
def _session(db: Session | None):
    if db is not None:
        yield db
        return
    own = SessionLocal()
    try:
        yield own
    finally:
        own.close()


def _bucket(ctx: WorkspaceContext) -> str:
    bucket = str(ctx.resources.get("artifacts_bucket") or "")
    if not bucket:
        raise AppError(
            "bundle.workspace_not_bootstrapped",
            "this workspace has no artifacts bucket yet — run bootstrap first",
            status_code=400,
        )
    return bucket


def bundle_arn(ctx: WorkspaceContext, bucket: str, key: str) -> str:
    """The S3 object ARN (partition-aware) — what the Global bundleArn slot now carries."""
    return ctx.arn("s3", f"{bucket}/{key}", region="", account="")


def _write(
    ws: Any, agent_id: str, bundle_id: str, version: int, name: str,
    components: dict[str, Any], *, db: Session, s3: Any,
) -> dict[str, Any]:
    ctx = _ctx(ws)
    bucket = _bucket(ctx)
    key = bundle_key(agent_id, bundle_id, version)
    exists = db.get(ConfigBundle, (bundle_id, version))
    if exists is not None:
        raise AppError("bundle.version_exists",
                       f"bundle {bundle_id} already has version {version} — versions are "
                       "immutable; create a new one", status_code=409)
    body = json.dumps({
        "bundle_id": bundle_id, "version": version, "agent_id": agent_id, "name": name,
        "components": components, "created_at": datetime.now(UTC).isoformat(),
    }, ensure_ascii=False, indent=2).encode("utf-8")
    (s3 or ctx.client("s3")).put_object(
        Bucket=bucket, Key=key, Body=body, ContentType="application/json",
    )
    row = ConfigBundle(id=bundle_id, version=version, workspace_id=_ws_id(ws),
                       agent_id=agent_id, name=name, components=components, s3_key=key)
    db.add(row)
    db.commit()
    return {"bundle_id": bundle_id, "version": version, "arn": bundle_arn(ctx, bucket, key),
            "s3_key": key, "name": name}


def create_bundle(
    ws: Any, agent: Any, *, name: str, system_prompt: str | None,
    tool_descriptions: dict[str, str] | None, db: Session | None = None, s3: Any = None,
) -> dict[str, Any]:
    """A new bundle (version 1) for ``agent`` → ``{bundle_id, arn, version, s3_key, name}``."""
    agent_id = str(getattr(agent, "id", None) or agent)
    with _session(db) as session:
        return _write(ws, agent_id, new_bundle_id(), 1, str(name)[:64],
                      components_for(system_prompt, tool_descriptions), db=session, s3=s3)


def new_version(
    ws: Any, bundle_id: str, *, system_prompt: str | None,
    tool_descriptions: dict[str, str] | None, name: str | None = None,
    db: Session | None = None, s3: Any = None,
) -> dict[str, Any]:
    """Append an immutable version to an existing bundle (``max(version) + 1``)."""
    with _session(db) as session:
        rows = list(session.scalars(select(ConfigBundle).where(
            ConfigBundle.id == bundle_id, ConfigBundle.workspace_id == _ws_id(ws))))
        if not rows:
            raise NotFoundError("bundle.not_found", f"bundle {bundle_id} not found")
        latest = max(rows, key=lambda r: r.version)
        return _write(ws, latest.agent_id, bundle_id, latest.version + 1,
                      str(name or latest.name)[:64],
                      components_for(system_prompt, tool_descriptions), db=session, s3=s3)


def get_bundle(ws: Any, bundle_id: str, version: int | None = None,
               *, db: Session | None = None) -> ConfigBundle:
    """One version (default: the latest) of a bundle in this workspace."""
    with _session(db) as session:
        query = select(ConfigBundle).where(
            ConfigBundle.id == bundle_id, ConfigBundle.workspace_id == _ws_id(ws))
        if version is not None:
            query = query.where(ConfigBundle.version == int(version))
        rows = list(session.scalars(query))
        if not rows:
            raise NotFoundError("bundle.not_found", f"bundle {bundle_id} not found")
        row = max(rows, key=lambda r: r.version)
        session.refresh(row)
        if db is None:
            session.expunge(row)
        return row


def list_bundles(ws: Any, agent_id: str, *, db: Session | None = None) -> list[ConfigBundle]:
    with _session(db) as session:
        rows = list(session.scalars(select(ConfigBundle).where(
            ConfigBundle.workspace_id == _ws_id(ws), ConfigBundle.agent_id == agent_id
        ).order_by(ConfigBundle.created_at.asc(), ConfigBundle.version.asc())))
        if db is None:
            for row in rows:
                session.refresh(row)
                session.expunge(row)
        return rows


def delete_bundle_objects(ws: Any, refs: Sequence[str], *, s3: Any = None) -> int:
    """Best-effort removal of S3 copies (cleanup of temporary optimizer bundles)."""
    ctx = _ctx(ws)
    client = s3 or ctx.client("s3")
    removed = 0
    with _session(None) as session:
        for ref in refs:
            try:
                bundle_id, version = parse_ref(ref)
            except ValueError:
                continue
            row = session.get(ConfigBundle, (bundle_id, version))
            if row is None:
                continue
            try:
                client.delete_object(Bucket=_bucket(ctx), Key=row.s3_key)
            except Exception:  # noqa: BLE001 — cleanup never fails the caller
                pass
            session.delete(row)
            removed += 1
        session.commit()
    return removed
