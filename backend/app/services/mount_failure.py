"""Recognize a turn that failed because a runtime file-system mount did not mount.

An ``s3BucketMount`` is mounted at invoke time (not during init), with a 30 s timeout
per mount; a failure answers ``InvokeAgentRuntime`` HTTP **424** with a message such as
"S3 mount failed: access denied" / "the S3 bucket was not found" / "the mount
configuration was rejected" (cross-Region without ``s3BucketRegion``). Left alone it is
a bare 500 on the console and ``/v1``; ``mount_failure`` names it
(``agent.mount_failed``, 424) with AWS's reason, so the person can fix the bucket,
the role or the Region instead of retrying.

Kept free of other app imports besides ``core.errors``: ``services/invoke`` (the sync
routes) and ``services/chat`` (the SSE error event) both call it.
"""

import re
from typing import Any

from botocore.exceptions import ClientError

from app.core.errors import AppError, aws_error_message

MOUNT_FAILED = "agent.mount_failed"

_FAILED_DEPENDENCY = 424
_REASON = re.compile(r"S3 mount failed:?\s*(.*)", re.IGNORECASE | re.DOTALL)


def mount_failure(exc: BaseException) -> AppError | None:
    """The named 424 for a turn whose runtime mount failed, else ``None``.

    Matches on the text "S3 mount failed" only (a streamed or bearer-invoke failure
    keeps only the text). HTTP 424 alone is NOT enough: it is AgentCore's generic
    ``RuntimeClientError`` — every agent-container failure ("Received error (500) from
    runtime") answers 424 too, and must keep its existing handling. The reason is
    AWS's own short phrase, safe on ``/v1`` too."""
    if isinstance(exc, AppError):
        return exc if exc.code == MOUNT_FAILED else None
    text = aws_error_message(exc) if isinstance(exc, ClientError) else str(exc)
    found = _REASON.search(text)
    if found is None:
        return None
    reason = found.group(1).strip()[:300]
    detail: dict[str, Any] = {"reason": reason, "retryable": False}
    return AppError(
        MOUNT_FAILED,
        f"the agent's runtime could not mount its storage: {reason or 'unknown reason'}",
        detail,
        status_code=_FAILED_DEPENDENCY,
    )
