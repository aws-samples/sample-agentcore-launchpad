"""One job-artefact writer for both executors (N12 E1-3b; plan §B.0 layout).

``JobSink`` is what a job runner writes — status, invocations, results, artefacts,
done — over an ``ObjectStore`` (``MemoryStore`` in the hermetic suite and ``make dev``,
``S3Store`` on the backend AND inside the Runtime worker). Pure library: the S3 client is
injected, so the worker zip ships this file byte-for-byte and the in-process executor runs
the identical writer.
"""

from __future__ import annotations

import json
import threading
from typing import Any, Protocol

from botocore.exceptions import ClientError

from app.evaluation import jobs
from app.evaluation.jobs import JobDone, JobSpec, JobStatus


class ObjectStore(Protocol):
    def put(self, key: str, body: bytes) -> None: ...
    def get(self, key: str) -> bytes | None: ...
    def list(self, prefix: str) -> list[str]: ...
    def delete(self, key: str) -> None: ...


class MemoryStore:
    """Dev / test backing: a dict with the S3 key layout."""

    def __init__(self) -> None:
        self._objects: dict[str, bytes] = {}
        self._lock = threading.Lock()

    def put(self, key: str, body: bytes) -> None:
        with self._lock:
            self._objects[key] = bytes(body)

    def get(self, key: str) -> bytes | None:
        with self._lock:
            return self._objects.get(key)

    def list(self, prefix: str) -> list[str]:
        with self._lock:
            return sorted(k for k in self._objects if k.startswith(prefix))

    def delete(self, key: str) -> None:
        with self._lock:
            self._objects.pop(key, None)


class S3Store:
    """The artifacts bucket through an injected S3 client."""

    def __init__(self, s3_client: Any, bucket: str) -> None:
        self._s3 = s3_client
        self.bucket = bucket

    def put(self, key: str, body: bytes) -> None:
        self._s3.put_object(Bucket=self.bucket, Key=key, Body=body)

    def get(self, key: str) -> bytes | None:
        try:
            return self._s3.get_object(Bucket=self.bucket, Key=key)["Body"].read()
        except KeyError:  # in-memory doubles
            return None
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in ("NoSuchKey", "404", "NotFound"):
                return None
            raise

    def list(self, prefix: str) -> list[str]:
        keys: list[str] = []
        token: str | None = None
        while True:
            kwargs: dict[str, Any] = {"Bucket": self.bucket, "Prefix": prefix}
            if token:
                kwargs["ContinuationToken"] = token
            page = self._s3.list_objects_v2(**kwargs)
            keys.extend(obj["Key"] for obj in page.get("Contents", []))
            token = page.get("NextContinuationToken")
            if not token:
                return sorted(keys)

    def delete(self, key: str) -> None:
        self._s3.delete_object(Bucket=self.bucket, Key=key)


class JobSink:
    """What a job runner writes: status, invocations, results, artefacts, done."""

    def __init__(self, store: ObjectStore, job: JobSpec) -> None:
        self._store = store
        self.job = job
        self._results: list[str] = []
        self._invocations: list[str] = []
        self._lock = threading.Lock()
        self._done = False
        self._last_status: tuple[str, dict[str, Any]] = ("invoking", {"done": 0, "total": 0,
                                                                      "message": ""})
        self._beat_stop = threading.Event()

    def status(self, phase: str, *, done: int = 0, total: int = 0, message: str = "") -> None:
        progress = {"done": done, "total": total, "message": message}
        with self._lock:
            self._last_status = (phase, progress)
        self._write_status(phase, progress)

    def _write_status(self, phase: str, progress: dict[str, Any]) -> None:
        status = JobStatus(phase=phase, progress=dict(progress))
        self._store.put(jobs.status_key(self.job.job_id, self.job.attempt),
                        json.dumps(status.to_dict()).encode("utf-8"))

    def start_heartbeat(self, interval_s: float) -> None:
        """N17-obs413: re-write the last status every ``interval_s`` until :meth:`finish` —
        the hub's staleness check then means "the worker is gone", not "a session (a judge
        call, a replay, a readiness wait) is slow". A failed write is retried next beat."""

        def beat() -> None:
            while not self._beat_stop.wait(interval_s):
                if self._done:
                    return
                with self._lock:
                    phase, progress = self._last_status
                try:
                    self._write_status(phase, progress)
                except Exception:  # noqa: BLE001 — the next beat tries again
                    pass

        threading.Thread(target=beat, daemon=True,
                         name=f"eval-heartbeat-{self.job.job_id}").start()

    def invocation(self, record: dict[str, Any]) -> None:
        with self._lock:
            self._invocations.append(json.dumps(record, default=str))
            body = ("\n".join(self._invocations) + "\n").encode("utf-8")
        self._store.put(jobs.invocations_key(self.job.job_id, self.job.attempt), body)

    def result(self, record: dict[str, Any], *, ordinal: int | None = None) -> None:
        """Append one result record, stamping the deterministic ``id`` when absent."""
        with self._lock:
            if "id" not in record:
                record = {**record, "id": jobs.record_id(
                    self.job.job_id, str(record.get("session_id")),
                    str(record.get("evaluator_id")),
                    len(self._results) if ordinal is None else ordinal)}
            self._results.append(json.dumps(record, default=str))
            body = ("\n".join(self._results) + "\n").encode("utf-8")
        self._store.put(jobs.results_key(self.job.job_id, self.job.attempt), body)

    def results(self, records: list[dict[str, Any]]) -> None:
        """Append several records with ONE store write (a session's worth)."""
        with self._lock:
            for record in records:
                if "id" not in record:
                    record = {**record, "id": jobs.record_id(
                        self.job.job_id, str(record.get("session_id")),
                        str(record.get("evaluator_id")), len(self._results))}
                self._results.append(json.dumps(record, default=str))
            body = ("\n".join(self._results) + "\n").encode("utf-8")
        self._store.put(jobs.results_key(self.job.job_id, self.job.attempt), body)

    @property
    def result_count(self) -> int:
        with self._lock:
            return len(self._results)

    def artifact(self, name: str, payload: Any) -> None:
        self._store.put(jobs.artifact_key(self.job.job_id, self.job.attempt, name),
                        json.dumps(payload, default=str).encode("utf-8"))

    def read_object(self, key: str) -> bytes | None:
        """One object of the job's bucket (D5: the hub-written ``tokens.json``)."""
        return self._store.get(key)

    def cancelled(self) -> bool:
        return self._store.get(jobs.cancel_key(self.job.job_id)) is not None

    def finish(self, state: str, *, error: str | None = None,
               summary: dict[str, Any] | None = None) -> None:
        with self._lock:
            count = len(self._results)
            self._done = True
        self._beat_stop.set()
        done = JobDone(state=state, error=error, result_count=count, summary=summary or {})
        self._store.put(jobs.done_key(self.job.job_id, self.job.attempt),
                        json.dumps(done.to_dict()).encode("utf-8"))

    @property
    def finished(self) -> bool:
        return self._done


class StoreReader:
    """status / done / ingest / cancel over the shared layout (both executors)."""

    def __init__(self, store: ObjectStore) -> None:
        self._store = store

    def _attempts(self, job_id: str) -> list[int]:
        return sorted({
            int(k[len(f"evaluations/{job_id}/a"):].split("/", 1)[0])
            for k in self._store.list(f"evaluations/{job_id}/a")
        })

    def attempt(self, job_id: str) -> int:
        """The attempt whose files are the job's truth (N17-obs413): the newest attempt that
        FINISHED (done.json), else the newest — an earlier attempt that completed while a
        re-dispatched one still runs is accepted, never ignored."""
        attempts = self._attempts(job_id)
        for attempt in reversed(attempts):
            if self._store.get(jobs.done_key(job_id, attempt)) is not None:
                return attempt
        return attempts[-1] if attempts else 1

    def status(self, job_id: str) -> JobStatus | None:
        attempt = self.attempt(job_id)
        done = self._store.get(jobs.done_key(job_id, attempt))
        if done is not None:
            final = JobDone.from_dict(json.loads(done))
            raw = self._store.get(jobs.status_key(job_id, attempt))
            status = JobStatus.from_dict(json.loads(raw)) if raw else JobStatus(phase="evaluating")
            status.state = final.state
            status.updated_at = final.finished_at
            return status
        raw = self._store.get(jobs.status_key(job_id, attempt))
        return JobStatus.from_dict(json.loads(raw)) if raw else None

    def done(self, job_id: str) -> JobDone | None:
        raw = self._store.get(jobs.done_key(job_id, self.attempt(job_id)))
        return JobDone.from_dict(json.loads(raw)) if raw else None

    def ingest(self, job_id: str, since: int = 0) -> list[dict[str, Any]]:
        raw = self._store.get(jobs.results_key(job_id, self.attempt(job_id)))
        if not raw:
            return []
        lines = [line for line in raw.decode("utf-8").splitlines() if line.strip()]
        return [json.loads(line) for line in lines[max(0, since):]]

    def invocations(self, job_id: str, attempt: int | None = None) -> list[dict[str, Any]]:
        raw = self._store.get(jobs.invocations_key(job_id, attempt or self.attempt(job_id)))
        if not raw:
            return []
        return [json.loads(line) for line in raw.decode("utf-8").splitlines() if line.strip()]

    def artifact(self, job_id: str, name: str) -> Any | None:
        raw = self._store.get(jobs.artifact_key(job_id, self.attempt(job_id), name))
        return json.loads(raw) if raw else None

    def attempt_artifacts(self, job_id: str, name: str) -> list[Any]:
        """``name`` of EVERY attempt that wrote it, in attempt order (N17-obs417: the usage
        sidecar of a failed / lost attempt still counts)."""
        out: list[Any] = []
        for attempt in self._attempts(job_id):
            raw = self._store.get(jobs.artifact_key(job_id, attempt, name))
            if raw:
                try:
                    out.append(json.loads(raw))
                except ValueError:
                    continue
        return out

    def cancel(self, job_id: str) -> None:
        self._store.put(jobs.cancel_key(job_id), b"cancel")

    def jobs_with_prefix(self, prefix: str) -> list[str]:
        """Job ids under ``evaluations/<prefix>…`` (a sharded run's ``<run>-s<i>`` jobs)."""
        ids = {k[len("evaluations/"):].split("/", 1)[0]
               for k in self._store.list(f"evaluations/{prefix}")}
        return sorted(i for i in ids if i.startswith(prefix))
