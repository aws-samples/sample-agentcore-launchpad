#!/usr/bin/env python3
"""E2E for the session output files (`filesystem.outputs`) — REAL AWS through a running backend.

Task 11-23-s3mount-p2-agent-files. Two modes:

``--setup-bucket``  create-or-adopt the workspace's ``launchpad-agent-files-<acct>-<region>``
    bucket with the same code the spoke bootstrap `storage` stage runs
    (``_ensure_agent_files_bucket``: BPA, SSE-S3, no versioning, 30-day lifecycle, policy),
    then prove the backend credentials are NOT locked out by the SourceSid Deny (put / get /
    list / delete of a probe key). Prints the line to add under ``resources:`` in
    ``config/launchpad.yaml``. The bucket is infrastructure and is never deleted here.

(default)  the member flow, against a backend whose workspace has ``agent_files_bucket``:

1. deploy a PUBLIC zip_runtime agent (V2) with ``filesystem.outputs`` on
2. live runtime readback: the ``s3BucketMount`` on the platform bucket, prefix
   ``projects/<project>/agents/<agent_id>/sessions/{runtimeSessionId}/``, ``/mnt/outputs``
3. one console chat turn, then write ``report.md`` into ``/mnt/outputs`` inside that session
   (InvokeAgentRuntimeCommand) → the Files API lists it, the presigned download returns the
   same bytes, a ``../`` path is refused
4. a second session of the same agent lists nothing from the first
5. re-publish keeps the mount
6. delete the agent → the agent's prefix is gone from the bucket
7. cleanup (agent, log groups); ``--keep`` skips it

Run against a backend that serves this branch:

    cd backend && uv run uvicorn app.main:app --port 8011 &
    uv run python scripts/e2e_agent_files.py --setup-bucket
    # add the printed `agent_files_bucket:` line to config/launchpad.yaml, restart the backend
    uv run python scripts/e2e_agent_files.py --base http://127.0.0.1:8011

Not part of `make verify`.
"""

from __future__ import annotations

import argparse
import shlex
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx  # noqa: E402
from _e2e_client import E2E_PROJECT_ID, chat_sse, e2e_client  # noqa: E402
from botocore.exceptions import ClientError  # noqa: E402

from app.services import workspace_bootstrap as wb  # noqa: E402
from app.services.agentcore import runtime as rt  # noqa: E402
from app.services.agentcore.client import control_client, data_client  # noqa: E402
from app.services.workspace import default_workspace_context  # noqa: E402

MODEL = "global.anthropic.claude-sonnet-5"
MOUNT = "/mnt/outputs"
failures: list[str] = []


def log(msg: str) -> None:
    print(f"{datetime.now(UTC):%H:%M:%S} {msg}", flush=True)


def check(cond: bool, what: str) -> bool:
    log(("ok · " if cond else "FAIL · ") + what)
    if not cond:
        failures.append(what)
    return cond


def new_session() -> str:
    return uuid.uuid4().hex + uuid.uuid4().hex


# ── bucket setup + backend-lockout probe ─────────────────────────────────────


def setup_bucket() -> int:
    ws = default_workspace_context()
    bucket = agent_files_bucket_name(ws)
    ctx = wb.BootstrapContext(workspace_id="default", job_id="e2e-agent-files",
                              workspace=ws, log=log)
    wb._ensure_agent_files_bucket(ctx, ws.client("s3"), bucket)
    s3 = ws.client("s3")
    time.sleep(10)  # nosemgrep: arbitrary-sleep — bucket policy propagation
    key = f"projects/_e2e/agents/_probe-{uuid.uuid4().hex[:8]}/sessions/x/probe.txt"
    ok = False
    try:
        s3.put_object(Bucket=bucket, Key=key, Body=b"probe")
        got = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
        listed = s3.list_objects_v2(Bucket=bucket, Prefix=key).get("KeyCount", 0)
        s3.delete_object(Bucket=bucket, Key=key)
        ok = got == b"probe" and listed == 1
    except ClientError as exc:
        log(f"backend credentials are DENIED by the bucket policy: {exc}")
    check(ok, "backend credentials (no SourceSid) can put/get/list/delete under the policy")
    pol = s3.get_bucket_lifecycle_configuration(Bucket=bucket)["Rules"]
    check(any(r.get("Expiration", {}).get("Days") == 30 for r in pol), f"30-day expiry rule: {pol}")
    ver = s3.get_bucket_versioning(Bucket=bucket).get("Status")
    check(ver in (None, ""), f"versioning off (status={ver!r})")
    log(f"add under `resources:` in config/launchpad.yaml:  agent_files_bucket: {bucket}")
    return 0 if not failures else 1


def agent_files_bucket_name(ws: Any) -> str:
    return wb._agent_files_bucket_name(wb.BootstrapContext(
        workspace_id="default", job_id="e2e-agent-files", workspace=ws))


# ── member flow ──────────────────────────────────────────────────────────────


class E2E:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.client = e2e_client(args.base, timeout=600)
        self.ws = default_workspace_context()
        self.s3 = self.ws.client("s3")
        self.control = control_client(self.ws)
        self.data = data_client(self.ws, read_timeout=120)
        self.suffix = uuid.uuid4().hex[:8]
        self.agent_id: str | None = None
        self.runtime_name: str | None = None
        self.bucket = (self.ws.resources or {}).get("agent_files_bucket") or ""

    def api(self, method: str, path: str, **kw: Any) -> Any:
        res = self.client.request(method, path, **kw)
        if res.status_code >= 400:
            raise RuntimeError(f"{method} {path} → {res.status_code}: {res.text[:500]}")
        return res.json() if res.content else None

    def wait_agent(self) -> dict[str, Any]:
        deadline = time.time() + self.args.timeout
        while time.time() < deadline:
            agent = self.api("GET", f"/api/agents/{self.agent_id}")
            if agent["status"] in ("active", "failed"):
                return agent
            time.sleep(10)  # nosemgrep: arbitrary-sleep — real deploy polling
        raise TimeoutError(f"agent {self.agent_id} not active after {self.args.timeout}s")

    def command(self, session: str, script: str) -> tuple[int | None, str]:
        arn = self.api("GET", f"/api/agents/{self.agent_id}")["arn"]
        res = self.data.invoke_agent_runtime_command(
            agentRuntimeArn=arn, runtimeSessionId=session, qualifier="DEFAULT",
            body={"command": f"sh -c {shlex.quote(script)}", "timeout": 60})
        out, code = [], None
        for event in res["stream"]:
            chunk = event.get("chunk") or {}
            delta = chunk.get("contentDelta") or {}
            out += [delta.get("stdout") or "", delta.get("stderr") or ""]
            if "contentStop" in chunk:
                code = chunk["contentStop"].get("exitCode")
        return code, "".join(out)

    def files(self, session: str) -> dict[str, Any]:
        return self.api("GET", f"/api/chat/{self.agent_id}/sessions/{session}/files")

    def agent_objects(self) -> list[str]:
        prefix = f"projects/{E2E_PROJECT_ID}/agents/{self.agent_id}/"
        keys: list[str] = []
        for page in self.s3.get_paginator("list_objects_v2").paginate(
                Bucket=self.bucket, Prefix=prefix):
            keys += [o["Key"] for o in page.get("Contents", [])]
        return keys

    def run(self) -> None:
        if not check(bool(self.bucket),
                     "workspace has resources.agent_files_bucket (run --setup-bucket first)"):
            return
        spec = {
            "name": f"e2e-files-{self.suffix}", "method": "zip_runtime", "model_id": MODEL,
            "system_prompt": "You are a terse test agent. Answer in one short sentence.",
            "toolkits": [],
            "runtime_config": {"platform_version": "V2", "billing_model": "consumption"},
            "filesystem": {"outputs": {"mount_path": MOUNT}},
        }
        body = self.api("POST", "/api/agents", json=spec)
        self.agent_id = body["agent"]["id"]
        agent = self.wait_agent()
        if not check(agent["status"] == "active", f"agent {self.agent_id} deployed"):
            return
        live = rt.get_runtime(self.control, agent["resource_id"])
        self.runtime_name = live.get("agentRuntimeName")
        mounts = [fs for fs in live.get("filesystemConfigurations") or []
                  if "s3BucketMount" in fs]
        want = f"projects/{E2E_PROJECT_ID}/agents/{self.agent_id}/sessions/{{runtimeSessionId}}/"
        mount = mounts[0]["s3BucketMount"] if mounts else {}
        check(mount.get("bucket") == self.bucket and mount.get("prefix") == want
              and mount.get("mountPath") == MOUNT and live.get("platformVersion") == "V2",
              f"live runtime mount: {mounts}")

        turn = chat_sse(self.client, str(self.agent_id), "Say hello.")
        session = turn["session_id"]
        check(bool(turn.get("text")), f"chat turn answered (session {session[:12]}…)")
        empty = self.files(session)
        check(empty.get("enabled") is True and empty.get("files") == [],
              f"Files API before any write: {empty}")

        token = uuid.uuid4().hex
        code, out = self.command(session, f"printf %s {token} > {MOUNT}/report.md && "
                                          f"cat {MOUNT}/report.md")
        check(code == 0 and token in out, f"in-session write: exit={code} out={out.strip()[:80]!r}")
        listing: dict[str, Any] = {}
        for _ in range(6):  # Mountpoint uploads on close; give S3 a moment
            listing = self.files(session)
            if listing.get("files"):
                break
            time.sleep(5)  # nosemgrep: arbitrary-sleep
        names = [f["key_rel"] for f in listing.get("files") or []]
        check(names == ["report.md"], f"Files API lists the write: {listing}")
        dl = self.api("GET", f"/api/chat/{self.agent_id}/sessions/{session}/files/download",
                      params={"path": "report.md"})
        got = httpx.get(dl["url"], timeout=60)
        check(got.status_code == 200 and got.text == token,
              f"presigned download bytes match (HTTP {got.status_code}, "
              f"expires_in={dl.get('expires_in')})")
        bad = self.client.get(f"/api/chat/{self.agent_id}/sessions/{session}/files/download",
                              params={"path": "../x"})
        check(bad.status_code in (400, 422), f"`..` path refused (HTTP {bad.status_code})")
        gone = self.client.get(f"/api/chat/{self.agent_id}/sessions/{new_session()}/files")
        check(gone.status_code == 404, f"unknown session → 404 (HTTP {gone.status_code})")

        other = chat_sse(self.client, str(self.agent_id), "Say hi again.")
        check(other["session_id"] != session
              and self.files(other["session_id"]).get("files") == [],
              "a second session of the same agent does not see the first session's file")

        self.api("POST", f"/api/agents/{self.agent_id}/redeploy",
                 json=self.api("GET", f"/api/agents/{self.agent_id}")["spec"])
        agent = self.wait_agent()
        live = rt.get_runtime(self.control, agent["resource_id"])
        check(any("s3BucketMount" in fs for fs in live.get("filesystemConfigurations") or []),
              "re-publish keeps the output-folder mount")

        check(len(self.agent_objects()) >= 1, "objects exist under the agent prefix before delete")
        self.client.delete(f"/api/agents/{self.agent_id}")
        self.wait_deleted()
        left = self.agent_objects()
        check(not left, f"agent delete removed its prefix (left: {left[:3]})")
        self.agent_id = None

    def wait_deleted(self) -> None:
        deadline = time.time() + 600
        while time.time() < deadline:
            res = self.client.get(f"/api/agents/{self.agent_id}")
            if res.status_code == 404 or res.json().get("status") == "deleted":
                return
            time.sleep(10)  # nosemgrep: arbitrary-sleep

    def cleanup(self) -> None:
        if self.args.keep:
            log(f"--keep: agent {self.agent_id}")
            return
        if self.agent_id:
            self._quiet("agent", lambda: self.client.delete(f"/api/agents/{self.agent_id}"))
            self.wait_deleted()
            self._quiet("agent objects", self._delete_objects)
        self._quiet("log groups", self._delete_log_groups)

    def _delete_objects(self) -> None:
        keys = [{"Key": k} for k in self.agent_objects()]
        if keys:
            self.s3.delete_objects(Bucket=self.bucket, Delete={"Objects": keys})

    def _delete_log_groups(self) -> None:
        if not self.runtime_name:
            return
        logs = self.ws.client("logs")
        prefix = f"/aws/bedrock-agentcore/runtimes/{self.runtime_name}-"
        for page in logs.get_paginator("describe_log_groups").paginate(logGroupNamePrefix=prefix):
            for group in page.get("logGroups", []):
                logs.delete_log_group(logGroupName=group["logGroupName"])

    def _quiet(self, what: str, fn: Any) -> None:
        try:
            fn()
            log(f"cleaned {what}")
        except Exception as exc:  # noqa: BLE001 — cleanup reports, never stops
            log(f"cleanup of {what} failed: {exc}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base", default="http://127.0.0.1:8011")
    parser.add_argument("--timeout", type=int, default=1200)
    parser.add_argument("--keep", action="store_true")
    parser.add_argument("--setup-bucket", action="store_true")
    args = parser.parse_args()
    if args.setup_bucket:
        rc = setup_bucket()
        log("PASS" if not failures else f"FAILED: {failures}")
        return rc
    e2e = E2E(args)
    try:
        e2e.run()
    except Exception as exc:  # noqa: BLE001 — report, then clean up
        failures.append(f"aborted: {exc}")
        log(f"FAIL · aborted: {exc}")
    finally:
        e2e.cleanup()
    log("PASS" if not failures else f"FAILED: {failures}")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
