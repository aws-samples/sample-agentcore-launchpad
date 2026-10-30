"""N17-obs190 (cn/r17-u): the Skill Lab exec-jobs janitor counts what S3 REMOVED.

`runner.sweep_exec_jobs_prefix` deletes exec-jobs debris older than the TTL after every job
(skill-lab-executor.md §6). It used to issue a `Quiet` `DeleteObjects` per 1000 keys and log
"removed N stale object(s)" for the N it asked about; through the shared reporting deleter
(`services/s3_sweep`) a key S3 refused inside a 200 answer is logged as refused (count + S3
code, never a key or the message) and only the deleted ones are counted. Best-effort still:
a raised listing / delete is one "janitor skipped" line, never a failed job.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from botocore.exceptions import ClientError

from app.services import aws_clients
from app.skill_lab import runner
from app.skill_lab.infra import EXEC_JOBS_PREFIX
from tests.conftest import ws_ctx
from tests.memory_fakes import FakeS3

BUCKET = "bkt"
WS = ws_ctx({"artifacts_bucket": BUCKET})


class JanitorS3(FakeS3):
    """`FakeS3` with settable ages and a `delete_objects` that refuses ``refuse`` keys."""

    def age(self, key: str, days: int) -> None:
        self.objects[(BUCKET, key)]["LastModified"] = datetime.now(UTC) - timedelta(days=days)

    def delete_objects(self, *, Bucket, Delete, **kw):  # noqa: N803 — boto kwargs
        keys = [o["Key"] for o in Delete["Objects"]]
        kept = [k for k in keys if "refuse" in k]
        super().delete_objects(Bucket=Bucket, Delete={"Objects": [
            {"Key": k} for k in keys if k not in kept]})
        return {"Deleted": [{"Key": k} for k in keys if k not in kept],
                "Errors": [{"Key": k, "Code": "AccessDenied", "Message": "Access Denied"}
                           for k in kept]}


def _seed(monkeypatch) -> JanitorS3:
    s3 = JanitorS3()
    for name, days in (("old/in.tar.gz", 9), ("old/refuse.tar.gz", 9), ("fresh/in.tar.gz", 1)):
        key = f"{EXEC_JOBS_PREFIX}/{name}"
        s3.put_object(Bucket=BUCKET, Key=key, Body=b"x")
        s3.age(key, days)
    monkeypatch.setattr(aws_clients, "client", lambda *a, **k: s3)
    return s3


def test_the_janitor_removes_only_stale_objects_and_counts_what_s3_removed(monkeypatch):
    s3 = _seed(monkeypatch)
    log: list[str] = []
    runner.sweep_exec_jobs_prefix(WS, log.append)
    assert log == ["exec-jobs janitor: removed 1 stale object(s), 1 of 2 objects refused "
                   "(AccessDenied)"], log
    left = sorted(k for (_b, k) in s3.objects)
    assert left == [f"{EXEC_JOBS_PREFIX}/fresh/in.tar.gz", f"{EXEC_JOBS_PREFIX}/old/refuse.tar.gz"]
    assert "Access Denied" not in " ".join(log) and "refuse.tar.gz" not in " ".join(log)


def test_a_clean_sweep_logs_the_count_and_nothing_stale_means_silence(monkeypatch):
    s3 = _seed(monkeypatch)
    s3.objects.pop((BUCKET, f"{EXEC_JOBS_PREFIX}/old/refuse.tar.gz"))
    log: list[str] = []
    runner.sweep_exec_jobs_prefix(WS, log.append)
    assert log == ["exec-jobs janitor: removed 1 stale object(s)"]
    runner.sweep_exec_jobs_prefix(WS, log.append)  # nothing stale left
    assert log == ["exec-jobs janitor: removed 1 stale object(s)"]


def test_a_raised_delete_is_logged_as_skipped_never_raised(monkeypatch):
    s3 = _seed(monkeypatch)

    def boom(**kw):
        raise ClientError({"Error": {"Code": "SlowDown", "Message": "Please reduce"}},
                          "DeleteObjects")

    s3.delete_objects = boom  # type: ignore[method-assign]
    log: list[str] = []
    runner.sweep_exec_jobs_prefix(WS, log.append)
    assert len(log) == 1 and log[0].startswith("exec-jobs janitor skipped: ")
    assert len(s3.objects) == 3


def test_no_artifacts_bucket_means_no_call(monkeypatch):
    calls: list = []
    monkeypatch.setattr(aws_clients, "client", lambda *a, **k: calls.append(a))
    runner.sweep_exec_jobs_prefix(ws_ctx({}), lambda m: calls.append(m))
    assert calls == []
