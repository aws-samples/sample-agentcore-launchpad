"""aws-cn trace export for TypeScript (Node) BYOC agents — N17-obs465 (user ruling 2026-10-26:
「先自己维护，不然不支持typescript」; design approved by the lead the same day).

ADOT Node (<= 0.13.0) cannot sign its trace export in aws-cn (N17-obs459), so a Node agent on a
CN Runtime has no traces, no observability and nothing to evaluate. The platform therefore adds
ONE layer on top of a Node agent's image in its own CodeBuild recipe — the member never edits
anything:

    FROM <the member's image>
    COPY launchpad-adot-cn.cjs /opt/launchpad/launchpad-adot-cn.cjs
    ENV NODE_OPTIONS="--require /opt/launchpad/launchpad-adot-cn.cjs ${NODE_OPTIONS}"

``launchpad-adot-cn.cjs`` (next to this file) is feature-detected and fails open: it does
nothing outside aws-cn, nothing when ADOT signs the ``.amazonaws.com.cn`` endpoint itself (a
fixed release switches it off), and otherwise swaps ADOT's span exporters for ADOT's own SigV4
exporter on that endpoint. It prints one ``launchpad-adot-cn: <state> (<reason>)`` line that
the console reads back (``telemetry_status``). Its ADOT internals are pinned per supported
version by ``test/hook.test.cjs`` (``make verify``).

When the layer applies (:func:`decide`): the workspace is in the aws-cn partition, the spec did
not opt out (``byoc.cn_trace_exporter: false``) and the artefact is Node — a
``container_source`` zip whose ``package.json`` depends on ADOT Node, or a ``container_image``
whose image config names Node (``NODE_VERSION`` env / a ``node`` entrypoint). Python images are
never rebuilt. The derived image is the one deployed; the member's own image digest is kept next
to it on the row (``spec.byoc.telemetry.member_image``) and in the deploy log.
"""

from __future__ import annotations

import json
import shutil
import urllib.request
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
HOOK_NAME = "launchpad-adot-cn.cjs"
HOOK_PATH = HERE / HOOK_NAME
LAYER_DOCKERFILE_NAME = "Dockerfile.launchpad-adot-cn"
ADOT_NODE_PACKAGE = "@aws/aws-distro-opentelemetry-node-autoinstrumentation"
HOOK_LOG_PREFIX = "launchpad-adot-cn:"
HOOK_STATES = ("attached", "native", "skipped", "unavailable")

LAYER_DOCKERFILE = f"""\
# Platform-owned (N17-obs465): the aws-cn trace exporter for a Node agent. Built FROM the
# member's image by the launchpad-agent-builder recipe; the member's image is unchanged.
ARG LAUNCHPAD_MEMBER_IMAGE
FROM ${{LAUNCHPAD_MEMBER_IMAGE}}
COPY {HOOK_NAME} /opt/launchpad/{HOOK_NAME}
ENV NODE_OPTIONS="--require /opt/launchpad/{HOOK_NAME} ${{NODE_OPTIONS}}"
LABEL io.launchpad.cn-trace-exporter="N17-obs465"
"""


def stage_layer(build_root: Path) -> None:
    """Put the hook and the layer Dockerfile into a CodeBuild context."""
    shutil.copy2(HOOK_PATH, build_root / HOOK_NAME)
    (build_root / LAYER_DOCKERFILE_NAME).write_text(LAYER_DOCKERFILE, encoding="utf-8")


def node_adot_in_package_json(text: str | None) -> str | None:
    """The ADOT Node version range a ``package.json`` depends on (deps or devDeps), or None."""
    if not text:
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    for section in ("dependencies", "devDependencies", "optionalDependencies"):
        deps = data.get(section)
        if isinstance(deps, dict) and isinstance(deps.get(ADOT_NODE_PACKAGE), str):
            return deps[ADOT_NODE_PACKAGE]
    return None


def image_config_is_node(config: dict[str, Any]) -> bool:
    """An OCI / docker image config whose runtime is Node (the official images set
    ``NODE_VERSION``; else a ``node`` entrypoint / command)."""
    cfg = config.get("config") or config.get("container_config") or {}
    env = cfg.get("Env") or []
    if any(isinstance(e, str) and e.startswith("NODE_VERSION=") for e in env):
        return True
    words = [*(cfg.get("Entrypoint") or []), *(cfg.get("Cmd") or [])]
    return any(isinstance(w, str) and (w == "node" or w.endswith("/node")
                                       or w.startswith("node ")) for w in words)


def image_is_node(ecr: Any, repository: str, image_id: dict[str, str],
                  fetch: Any = None) -> bool | None:
    """Read a private ECR image's config (manifest → config blob, read-only) and say whether
    it is a Node image; None when it cannot be read (the caller then keeps today's path)."""
    try:
        images = ecr.batch_get_image(
            repositoryName=repository, imageIds=[image_id],
            acceptedMediaTypes=["application/vnd.docker.distribution.manifest.v2+json",
                                "application/vnd.oci.image.manifest.v1+json"],
        ).get("images") or []
        if not images:
            return None
        manifest = json.loads(images[0].get("imageManifest") or "{}")
        digest = (manifest.get("config") or {}).get("digest")
        if not digest:
            return None
        url = ecr.get_download_url_for_layer(repositoryName=repository,
                                             layerDigest=digest)["downloadUrl"]
        body = (fetch or _fetch)(url)
        return image_config_is_node(json.loads(body))
    except Exception:  # noqa: BLE001 — a read we cannot do never changes the deploy
        return None


def _fetch(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=30) as resp:  # noqa: S310 — ECR's presigned URL
        return resp.read()


def decide(partition: str, opted_in: bool, node: bool | None) -> tuple[bool, str]:
    """``(apply, reason)`` for the layer — the reason is what the console and the deploy log
    show (``not_aws_cn`` / ``opted_out`` / ``not_node`` / ``unknown_runtime`` / ``applied``)."""
    if partition != "aws-cn":
        return False, "not_aws_cn"
    if not opted_in:
        return False, "opted_out"
    if node is None:
        return False, "unknown_runtime"
    if not node:
        return False, "not_node"
    return True, "applied"


def parse_hook_line(message: str) -> dict[str, str] | None:
    """``{state, reason}`` of a runtime log line the hook printed, else None."""
    text = message.strip()
    if HOOK_LOG_PREFIX not in text:
        return None
    rest = text.split(HOOK_LOG_PREFIX, 1)[1].strip()
    state, _, reason = rest.partition(" ")
    if state not in HOOK_STATES:
        return None
    return {"state": state, "reason": reason.strip().removeprefix("(").removesuffix(")")}
