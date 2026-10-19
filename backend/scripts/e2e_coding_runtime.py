"""Coding runtime smoke — REAL AWS, not part of the verify gate.

Prereqs: `make bootstrap` (artifacts bucket, shared CodeBuild project + ECR repo)
and credentials for the default workspace's account. Billable: a CodeBuild run
when the image is new, a few minutes of microVM time and one short GPT-6 turn.

    cd backend && uv run python scripts/e2e_coding_runtime.py              # install + smoke
    cd backend && uv run python scripts/e2e_coding_runtime.py --skip-install
    cd backend && uv run python scripts/e2e_coding_runtime.py --uninstall  # remove at the end

Steps (each asserts and prints what it saw):

1. install / repair through the same Job the admin route queues (role → image →
   runtime → READY), keys recorded on the workspace;
2. `status` on a fresh session (fresh init: git repo + AGENTS.md);
3. a short turn that writes and runs a file, followed live with `events`
   long-polls — agent text deltas and command output must stream;
4. `snapshot` to a presigned PUT the script mints (the runtime role has no S3);
5. StopRuntimeSession → the same session comes back with its files and thread
   (session storage), on a new microVM (new boot id);
6. a brand-new session given the snapshot's presigned GET as `restore_url`
   restores the project before its first action.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from botocore.config import Config  # noqa: E402

from app.coding import infra, install, runtime_client  # noqa: E402
from app.core.config import get_settings  # noqa: E402
from app.core.db import SessionLocal, init_db  # noqa: E402
from app.models.ledger import Job  # noqa: E402
from app.services.agentcore import runtime as rt  # noqa: E402
from app.services.agentcore.client import data_client  # noqa: E402
from app.services.workspace import WorkspaceContext, default_workspace_context  # noqa: E402

PROMPT = (
    "Create a file hello.py that prints 'hello from the coding runtime', run it with "
    "python3, then commit it with git. Reply with the program's output only."
)
TURN_TIMEOUT_S = 15 * 60


def log(msg: str) -> None:
    print(f"{datetime.now(UTC):%H:%M:%S} {msg}", flush=True)


def check(cond: bool, what: str) -> None:
    if not cond:
        sys.exit(f"FAIL: {what}")
    log(f"ok · {what}")


def _s3(ws: WorkspaceContext) -> Any:
    return ws.client("s3", cache_token="e2e-coding-s3v4",
                     config=Config(signature_version="s3v4"))


def presign(ws: WorkspaceContext, method: str, key: str) -> str:
    bucket = str(ws.resources.get("artifacts_bucket") or "")
    return _s3(ws).generate_presigned_url(
        method, Params={"Bucket": bucket, "Key": key}, ExpiresIn=900
    )


def act(ws: WorkspaceContext, session: str, action: str, **payload: Any) -> Any:
    arn = str(ws.resources.get(infra.KEY_RUNTIME_ARN) or "")
    t0 = time.monotonic()
    result = runtime_client.call(ws, arn, session, action, **payload)
    if action != "events":
        log(f"{action} · {time.monotonic() - t0:.1f}s")
    return result


def do_install(ws: WorkspaceContext) -> None:
    db = SessionLocal()
    try:
        job, started = install.request_job(
            db, ws.id, install.INSTALL_JOB_TYPE, requested_by=None,
            requested_by_name="e2e_coding_runtime",
        )
    finally:
        db.close()
    check(started, f"install job {job.id} queued")
    install.execute_job(job.id)  # synchronously: the same body the route's thread runs
    db = SessionLocal()
    try:
        done = db.get(Job, job.id)
        for line in (done.log or "").splitlines():
            event = json.loads(line)
            print(f"   [{event.get('stage')}] {event.get('msg')}")
        check(done.status == "succeeded", f"install job succeeded ({done.error or 'no error'})")
    finally:
        db.close()


def follow_turn(ws: WorkspaceContext, session: str, after: int, turn_id: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    deadline = time.monotonic() + TURN_TIMEOUT_S
    cursor = after
    while time.monotonic() < deadline:
        page = act(ws, session, "events", after=cursor, wait_ms=20000, limit=2000)
        for event in page["events"]:
            method = event["method"]
            counts[method] = counts.get(method, 0) + 1
            params = event.get("params") or {}
            if method == "item/commandExecution/outputDelta":
                print(f"   $ {params.get('delta', '').rstrip()}")
            elif method == "item/completed" and (params.get("item") or {}).get("type") == \
                    "agentMessage":
                print(f"   agent: {params['item'].get('text', '')[:300]}")
            elif method in ("launchpad/turnUsage", "launchpad/turnAbandoned") and \
                    params.get("turnId") == turn_id:
                print(f"   {method}: {json.dumps(params)[:400]}")
                return counts
        cursor = page["next_after"]
    sys.exit(f"FAIL: turn {turn_id} did not finish within {TURN_TIMEOUT_S}s")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--skip-install", action="store_true")
    parser.add_argument("--uninstall", action="store_true", help="uninstall at the end")
    parser.add_argument("--model", default=get_settings().coding_default_model)
    parser.add_argument("--keep", action="store_true", help="keep the snapshot objects")
    args = parser.parse_args()

    init_db()  # idempotent; a fresh checkout's ledger has no tables until the app starts
    ws = default_workspace_context()
    if not ws.resources.get("artifacts_bucket"):
        sys.exit("artifacts_bucket missing — run `make bootstrap` first")
    if not args.skip_install:
        do_install(ws)
        ws = default_workspace_context()  # re-read the keys the job recorded
    arn = str(ws.resources.get(infra.KEY_RUNTIME_ARN) or "")
    check(bool(arn), f"runtime installed · {arn}")

    session = runtime_client.new_session_id()
    restored_session = runtime_client.new_session_id()
    prefix = f"coding/{ws.id}/e2e-{session[:12]}"
    snapshot_key = f"{prefix}/snapshots/{datetime.now(UTC):%Y%m%dT%H%M%SZ}.tgz"
    data = data_client(ws)
    try:
        first = act(ws, session, "status")
        check(first["thread_id"] is None and not first["busy"], "fresh session is idle")
        tree = act(ws, session, "tree")
        check(any(e["path"] == "AGENTS.md" for e in tree["entries"]), "AGENTS.md template present")

        started = act(ws, session, "start_turn", prompt=PROMPT, model=args.model, effort="low")
        log(f"turn {started['turn_id']} on thread {started['thread_id']}")
        counts = follow_turn(ws, session, started["seq"], started["turn_id"])
        log(f"event counts: {json.dumps(counts, sort_keys=True)}")
        check(counts.get("item/agentMessage/delta", 0) > 0, "agent text streamed as deltas")
        check(counts.get("item/commandExecution/outputDelta", 0) > 0 or
              counts.get("item/started", 0) > 0, "command activity streamed")
        check(counts.get("launchpad/turnUsage", 0) == 1, "turn completed with usage")
        hello = act(ws, session, "read_file", path="hello.py")
        check(bool(hello.get("exists")), "the agent wrote hello.py")
        changes = act(ws, session, "changes", scope="turn")
        log(f"turn changes: {changes['changes']}")

        snap = act(ws, session, "snapshot", put_url=presign(ws, "put_object", snapshot_key))
        check(snap["bytes"] > 0, f"snapshot {snap['bytes']} bytes → s3://…/{snapshot_key}")

        rt.stop_runtime_session(data, runtime_arn=arn, session_id=session)
        log("StopRuntimeSession sent")
        again = act(ws, session, "status")
        check(again["boot_id"] != first["boot_id"], "a new microVM serves the stopped session")
        check(again["thread_id"] == started["thread_id"], "thread survives the stop")
        check(act(ws, session, "read_file", path="hello.py").get("exists"),
              "files survive the stop (session storage)")

        restore_url = presign(ws, "get_object", snapshot_key)
        fresh = act(ws, restored_session, "status", restore_url=restore_url)
        check(fresh["restored"] is True, "a fresh session restored from the snapshot")
        check(fresh["thread_id"] == started["thread_id"], "restored state carries the thread")
        check(act(ws, restored_session, "read_file", path="hello.py",
                  restore_url=restore_url).get("exists"), "restored project has hello.py")
    finally:
        for sid in (session, restored_session):
            try:
                rt.stop_runtime_session(data, runtime_arn=arn, session_id=sid)
            except Exception as exc:  # noqa: BLE001 — best-effort cleanup
                log(f"stop {sid[:12]}…: {exc.__class__.__name__}")
        if not args.keep:
            bucket = str(ws.resources.get("artifacts_bucket") or "")
            listed = _s3(ws).list_objects_v2(Bucket=bucket, Prefix=prefix + "/")
            for obj in listed.get("Contents", []):
                _s3(ws).delete_object(Bucket=bucket, Key=obj["Key"])
    if args.uninstall:
        db = SessionLocal()
        try:
            job, _ = install.request_job(db, ws.id, install.UNINSTALL_JOB_TYPE,
                                         requested_by=None, requested_by_name="e2e")
        finally:
            db.close()
        install.execute_job(job.id)
        log(f"uninstall job {job.id} finished")
    log("PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
