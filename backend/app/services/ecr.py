"""ECR digest resolution and the scan gate for the container deploy path (T10).

Two jobs, both about knowing exactly what is being deployed:

* **digest resolution** — the container path tags images `{agent}-v{version}`, a
  mutable tag. Deploying by tag means what a runtime executes can change under it
  without any record. `resolve_digest` turns the tag into the immutable
  `sha256:…` that `CreateAgentRuntime` is given instead.
* **the scan gate** — ECR scans on push (see `infra/stacks/base_stack.py`); this
  reads the result and lets the caller refuse to deploy a vulnerable image.

The client is passed in, not built here: ECR is not an AgentCore service, so it
does not belong behind `services/agentcore/client.py`, and the deployer already
constructs its own `s3`/`codebuild` clients the same way. Injection is what makes
these testable against a stub.
"""

import time
from collections.abc import Callable
from typing import Any

# describe_image_scan_findings status values we care about. Anything else is
# treated as "the gate did not complete" rather than as clean.
SCAN_COMPLETE = "COMPLETE"
SCAN_IN_PROGRESS = "IN_PROGRESS"
SCAN_FAILED = "FAILED"


class ScanUnavailable(RuntimeError):
    """The scan result could not be read (not enabled, image gone, API error).

    Deliberately distinct from "scanned and clean" so a caller cannot mistake an
    absent gate for a passed one.
    """


class ScanTimeout(RuntimeError):
    """The scan was still running when the deadline passed."""


class ImageBlocked(RuntimeError):
    """The image carries findings at or above the configured severities."""


def resolve_digest(client: Any, repository: str, tag: str) -> str:
    """The immutable `sha256:…` digest currently behind `tag`."""
    response = client.describe_images(
        repositoryName=repository, imageIds=[{"imageTag": tag}]
    )
    details = response.get("imageDetails") or []
    if not details or not details[0].get("imageDigest"):
        raise ScanUnavailable(
            f"ECR returned no digest for {repository}:{tag} — cannot pin the "
            "deployment to an image"
        )
    return details[0]["imageDigest"]


def wait_for_scan(
    client: Any,
    repository: str,
    digest: str,
    timeout_s: int = 300,
    interval_s: int = 5,
    sleeper: Callable[[float], None] = time.sleep,
    on_status: Callable[[str], None] | None = None,
) -> dict[str, int]:
    """Poll until the push scan finishes; return its severity counts.

    Raises `ScanTimeout` if it is still running at the deadline and
    `ScanUnavailable` if the scan failed or the findings cannot be read — never a
    silent empty result, which would read as "clean".
    """
    deadline = time.monotonic() + timeout_s
    last_status = None
    while True:
        try:
            response = client.describe_image_scan_findings(
                repositoryName=repository, imageId={"imageDigest": digest}
            )
        except Exception as exc:  # noqa: BLE001 — any read failure means no gate
            raise ScanUnavailable(
                f"could not read scan findings for {repository}@{digest}: {exc}"
            ) from exc

        status = (response.get("imageScanStatus") or {}).get("status", "UNKNOWN")
        if status != last_status and on_status is not None:
            on_status(status)
        last_status = status

        if status == SCAN_COMPLETE:
            findings = response.get("imageScanFindings") or {}
            return dict(findings.get("findingSeverityCounts") or {})
        if status == SCAN_FAILED:
            description = (response.get("imageScanStatus") or {}).get("description", "")
            raise ScanUnavailable(f"ECR scan failed for {repository}@{digest}: {description}")
        if status != SCAN_IN_PROGRESS:
            raise ScanUnavailable(
                f"unexpected ECR scan status {status!r} for {repository}@{digest}"
            )
        if time.monotonic() > deadline:
            raise ScanTimeout(
                f"ECR scan for {repository}@{digest} still {status} after {timeout_s}s"
            )
        sleeper(interval_s)


def blocking_findings(
    counts: dict[str, int], severities: list[str]
) -> dict[str, int]:
    """The subset of severity counts that should stop a deploy."""
    wanted = {severity.upper() for severity in severities}
    return {
        severity: count
        for severity, count in counts.items()
        if severity.upper() in wanted and count
    }


def blocking_packages(
    client: Any,
    repository: str,
    digest: str,
    severities: list[str],
    limit: int = 8,
) -> list[str]:
    """`CVE-… (package version)` for the findings that blocked, heaviest first.

    A count alone ("CRITICAL 4") does not tell an operator whether the problem is
    their own dependency or an unpatched OS package in the base image — which in
    practice it usually is, and which decides whether rebuilding can even help.
    Read separately from `wait_for_scan` so its contract stays "counts only", and
    best-effort: a failure here must not replace a real blocking error.
    """
    wanted = {severity.upper() for severity in severities}
    order = ["CRITICAL", "HIGH", "MEDIUM", "LOW", "INFORMATIONAL", "UNDEFINED"]
    try:
        response = client.describe_image_scan_findings(
            repositoryName=repository, imageId={"imageDigest": digest}
        )
        findings = (response.get("imageScanFindings") or {}).get("findings") or []
    except Exception:  # noqa: BLE001 — advisory detail only
        return []

    def rank(finding: dict) -> int:
        severity = (finding.get("severity") or "").upper()
        return order.index(severity) if severity in order else 99

    out = []
    for finding in sorted(findings, key=rank):
        if (finding.get("severity") or "").upper() not in wanted:
            continue
        attributes = {
            attribute.get("key"): attribute.get("value")
            for attribute in finding.get("attributes") or []
        }
        package = attributes.get("package_name") or "?"
        version = attributes.get("package_version") or "?"
        out.append(f"{finding.get('name', '?')} ({package} {version})")
        if len(out) >= limit:
            break
    return out


def format_counts(counts: dict[str, int]) -> str:
    """Severity counts for a job-log line, heaviest first."""
    order = ["CRITICAL", "HIGH", "MEDIUM", "LOW", "INFORMATIONAL", "UNDEFINED"]
    ranked = sorted(
        counts.items(),
        key=lambda item: (order.index(item[0].upper()) if item[0].upper() in order else 99),
    )
    return ", ".join(f"{severity} {count}" for severity, count in ranked) or "none"
