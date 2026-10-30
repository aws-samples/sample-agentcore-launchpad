"""One reporting S3 deleter for every best-effort sweep (N17-obs118 / N17-obs119, cn/r17-t).

Three sweeps used to delete and look away: the registry skill-record sweep swallowed every
per-key failure (`skill_ingest._delete_keys` was `except Exception: pass`, written for the
registration's orphan cleanup where masking the ORIGINAL failure was the point), and the
memory-store / knowledge-base prefix purges called `DeleteObjects` once per page without
reading its answer — S3 answers HTTP 200 with `Deleted[]` AND `Errors[{Key, Code, Message}]`
for the keys it refused (a per-key AccessDenied, a lifecycle lock), so a purge "succeeded"
while the keys stayed. Both reported the files gone with residue behind the answer.

This module is the one place that deletes keys for a sweep:

* :func:`delete_keys` batches the keys to the 1000-key `DeleteObjects` ceiling and reads
  every answer's `Errors[]` into :class:`SweepResult.failed` (key → the S3 error CODE, never
  its message). A `DeleteObjects` call that RAISES (a throttle, AccessDenied on the whole
  batch, a transport failure) propagates to the caller — that is the raised failure the
  callers' guards already report (`redacted_error`).
* :func:`delete_prefix` lists the prefix (paginated) and deletes what it finds.
* :meth:`SweepResult.raise_for_residue` turns refused keys into :class:`S3SweepResidue`, so a
  step loop that already guards a raised failure (`memory_selfhosted.delete_memory_resource`,
  `kb_cleanup.run_steps`) reports the residue the same way: `skipped` / `pending` plus one
  redacted warning line (`s3: S3SweepResidue: 2 of 3 objects refused (AccessDenied ×2)`).

Nothing here retries: the KB cleanup job re-runs its pending step, the memory purge and the
registry sweep are reported for the operator (memory-backend.md / registry-backend.md).
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any

DELETE_BATCH = 1000  # the DeleteObjects ceiling


class S3SweepResidue(RuntimeError):
    """A sweep that left refused keys behind. The text names counts and S3 error CODES only
    (no key, no S3 message), so `redacted_error` may carry it verbatim into a response."""

    def __init__(self, result: SweepResult) -> None:
        self.result = result
        super().__init__(result.summary())


@dataclass
class SweepResult:
    """What a sweep did: `deleted` keys and `failed` {key: S3 error code}."""

    deleted: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)

    @property
    def attempted(self) -> int:
        return len(self.deleted) + len(self.failed)

    @property
    def complete(self) -> bool:
        return not self.failed

    def summary(self) -> str:
        """`2 of 3 objects refused (AccessDenied ×2)` — codes and counts, never a key or a
        message (the operator reads it in a response body / a WARNING line)."""
        if not self.failed:
            return f"{len(self.deleted)} object(s) deleted"
        codes = ", ".join(
            f"{code} ×{n}" if n > 1 else code
            for code, n in sorted(Counter(self.failed.values()).items())
        )
        return f"{len(self.failed)} of {self.attempted} objects refused ({codes})"

    def raise_for_residue(self) -> SweepResult:
        if self.failed:
            raise S3SweepResidue(self)
        return self


def delete_keys(s3: Any, bucket: str, keys: list[str]) -> SweepResult:
    """Delete ``keys`` in `DeleteObjects` batches and report per key. A refused key
    (`Errors[]`) lands in ``failed`` with its code; a key S3 neither lists as deleted nor as
    refused (a `Quiet`-style answer) counts as deleted. A RAISED `DeleteObjects` propagates."""
    result = SweepResult()
    for start in range(0, len(keys), DELETE_BATCH):
        batch = keys[start:start + DELETE_BATCH]
        answer = s3.delete_objects(
            Bucket=bucket, Delete={"Objects": [{"Key": k} for k in batch]}
        ) or {}
        refused = {
            str(e.get("Key")): str(e.get("Code") or "Error")
            for e in answer.get("Errors") or [] if e.get("Key")
        }
        result.failed.update(refused)
        result.deleted.extend(k for k in batch if k not in refused)
    return result


def delete_prefix(s3: Any, bucket: str, prefix: str) -> SweepResult:
    """List every object under ``prefix`` (paginated) and delete it through
    :func:`delete_keys`. An empty prefix is refused, never expanded to the whole bucket. A
    raised listing or delete propagates (the caller's guard reports it)."""
    if not prefix:
        raise ValueError("refusing to sweep an empty S3 prefix")
    keys: list[str] = []
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
        keys.extend(obj["Key"] for obj in page.get("Contents") or [])
    return delete_keys(s3, bucket, keys)
