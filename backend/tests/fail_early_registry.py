"""Every late-failure site of the deploy pipeline, classified (design §1.3 B).

``tests/test_fail_early_guard.py`` scans ``app/deployer/*.py`` for every
``raise HarnessRenderError(`` / ``raise RuntimeError(`` and requires each one to be listed
here — either as **moved** (creation-time validation now refuses the same input before any
agent row or job exists; the raise stays as the last line of defence for stored specs) or
as **late** (the condition needs the artifact, the network or a build step and cannot be
known from the request). A site missing from this table fails the guard naming
``file:line`` — nobody can add a new input-only refusal to a stage without classifying it.

A site is identified by its file and the constant prefix of its message (f-string
placeholders are dropped), so line shifts never break the registry; a message edit does,
on purpose — the reviewer re-reads the classification.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Disposition = Literal["moved", "late"]


@dataclass(frozen=True)
class Site:
    file: str
    message: str  # constant prefix of the raise message (see ``test_fail_early_guard``)
    disposition: Disposition
    # moved: the creation-time error code that now refuses the input
    code: str = ""
    # late: why the request alone cannot decide it
    reason: str = ""


MOVED, LATE = "moved", "late"

REGISTRY: tuple[Site, ...] = (
    # ── harness_local.py ─────────────────────────────────────────────────────
    Site("harness_local.py", "builtin tool(s) not rendered by the self-built Harness",
         MOVED, code="agent.method_unsupported"),
    Site("harness_local.py", "mcp tool : the self-built Harness can mount unauthenticated",
         MOVED, code="agent.method_unsupported"),
    Site("harness_local.py", "mcp tool : config.url (http/https) is required",
         MOVED, code="validation.invalid_request"),
    Site("harness_local.py", "a Gateway tool must name a gateway_id",
         MOVED, code="agent.gateway_tool_unbound"),
    Site("harness_local.py", "artifacts_bucket missing from this workspace's resource map",
         MOVED, code="workspace.resource_missing"),
    Site("harness_local.py", "render_harness_bundle got method", LATE,
         reason="internal assertion — the pipeline dispatches by method"),
    Site("harness_local.py", "harness template missing at", LATE,
         reason="build-time: the vendored template tree (template self-check covers it)"),
    Site("harness_local.py", " missing from  ()", LATE,
         reason="build-time: the native-tool .hbs of the vendored template"),
    Site("harness_local.py", "rendered  does not compile", LATE,
         reason="build-time: renderer output (template self-check covers it)"),
    Site("harness_local.py", "unrendered __LAUNCHPAD_ placeholder in", MOVED,
         code="validation.invalid_request"),  # AgentSpec._no_reserved_placeholder (rev-r16)
    # ── zip_runtime.py ───────────────────────────────────────────────────────
    Site("zip_runtime.py", "artifacts_bucket missing from this workspace's resource map",
         MOVED, code="workspace.resource_missing"),
    Site("zip_runtime.py", "could not resolve the requirements into a hashed lockfile", LATE,
         reason="needs the package index (uv resolve); message carries the fix hints"),
    Site("zip_runtime.py", "pip install of the locked requirements failed", LATE,
         reason="needs the package index (pip install)"),
    # ── byoc.py ──────────────────────────────────────────────────────────────
    Site("byoc.py", "byoc spec has no byoc settings block", MOVED,
         code="validation.invalid_request"),
    Site("byoc.py", "'' is not a private ECR image URI", MOVED,
         code="validation.invalid_request"),
    Site("byoc.py", "image  lives in /; this workspace deploys from / only", MOVED,
         code="agent.method_unsupported"),
    Site("byoc.py", "artifacts_bucket missing from this workspace's resource map",
         MOVED, code="workspace.resource_missing"),
    Site("byoc.py", "image  not found in ECR", LATE,
         reason="needs ECR (describe_images)"),
    Site("byoc.py", "the zip's requirements.txt was refused", LATE,
         reason="needs the uploaded artifact's requirements.txt (S3)"),
    Site("byoc.py", "pip install of the zip's locked requirements failed", LATE,
         reason="needs the package index (pip install)"),
    Site("byoc.py", "entrypoint '' not found in the uploaded zip", LATE,
         reason="needs the uploaded artifact (S3); the upload response already lists "
                "detected.entrypoint_candidates so the console can refuse the pick"),
    Site("byoc.py", "no Dockerfile at the zip root", LATE,
         reason="needs the uploaded artifact (S3); the upload response already carries "
                "detected.has_dockerfile so the console can warn before publish"),
    # ── image_build.py ───────────────────────────────────────────────────────
    Site("image_build.py", "artifacts_bucket/codebuild_project missing from this workspace",
         MOVED, code="workspace.resource_missing"),  # conditional `requires` (rev-r16)
    Site("image_build.py", "image @ has blocking vulnerabilities", LATE,
         reason="needs the ECR scan result of the image just built"),
    # ── pipeline.py / registration.py ────────────────────────────────────────
    Site("pipeline.py", "ledger rows missing for job", LATE,
         reason="internal: a job whose rows were deleted underneath it"),
    Site("registration.py", "unknown system preset key", LATE,
         reason="internal: a preset row whose key left the catalog"),
)
