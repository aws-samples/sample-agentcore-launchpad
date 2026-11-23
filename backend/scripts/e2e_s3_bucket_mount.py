#!/usr/bin/env python3
"""E2E for the `s3bucket` storage catalog kind — REAL AWS through a running backend.

Walks the member flow end to end on one workspace (task 11-23-s3mount-p1-catalog-kind):

1. create a throwaway bucket ``launchpad-e2e-s3m-<account>-<rand>``
2. register it as catalog entry ``e2e-s3m-<rand>`` (``scope: per_session``, ``rw``),
   check it, allow it for the suite's project
3. deploy a PUBLIC zip_runtime agent (platform V2) mounting it at ``/mnt/<id>``
4. one console chat turn (the mount happens at invoke time: a failed mount would
   answer ``424 agent.mount_failed``)
5. write a file inside that session (InvokeAgentRuntimeCommand) and find the object
   at ``<prefix><agent_id>/<session_id>/`` through the S3 API
6. a second session does not see the first session's file
7. re-publish → the mount is still on the runtime and a new session writes again
8. apply the entry's recommended bucket policy → a session still writes, the caller
   (no SourceSid) still reads; the policy is removed again
9. cleanup: agent, catalog entry, bucket, runtime log groups (``--keep`` skips it)

Run against a backend that serves this branch (the dev runbook's throwaway recipe):

    cd backend && uv run uvicorn app.main:app --port 8011 &
    uv run python scripts/e2e_s3_bucket_mount.py --base http://127.0.0.1:8011

Not part of `make verify`.
"""

from __future__ import annotations

import argparse
import json
import shlex
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _e2e_client import E2E_PROJECT_ID, chat_sse, e2e_client  # noqa: E402
from botocore.exceptions import ClientError  # noqa: E402

from app.services.agentcore import runtime as rt  # noqa: E402
from app.services.agentcore.client import control_client, data_client  # noqa: E402
from app.services.workspace import default_workspace_context  # noqa: E402

MODEL = "global.anthropic.claude-sonnet-5"
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


class E2E:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.client = e2e_client(args.base, timeout=600)
        self.ws = default_workspace_context()
        self.s3 = self.ws.client("s3")
        self.control = control_client(self.ws)
        self.data = data_client(self.ws, read_timeout=120)
        self.suffix = uuid.uuid4().hex[:8]
        self.bucket = f"launchpad-e2e-s3m-{self.ws.account_id}-{self.suffix}"
        self.storage_id = f"e2e-s3m-{self.suffix}"
        self.mount = f"/mnt/{self.storage_id}"
        self.agent_id: str | None = None
        self.runtime_name: str | None = None

    # ── helpers ──────────────────────────────────────────────────────────────

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
        """Run ``sh -c <script>`` inside the agent's session; (exit code, stdout+stderr)."""
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
            for key, val in event.items():
                if key != "chunk":
                    out.append(f"[{key}] {val}")
        return code, "".join(out)

    def head(self, key: str) -> bool:
        try:
            self.s3.head_object(Bucket=self.bucket, Key=key)
            return True
        except ClientError:
            return False

    def write_and_find(self, session: str, name: str) -> bool:
        token = uuid.uuid4().hex[:12]
        code, out = self.command(session, f"echo {token} > {self.mount}/{name} && "
                                          f"cat {self.mount}/{name}")
        key = f"{self.prefix}{self.agent_id}/{session}/{name}"
        found = False
        for _ in range(6):  # Mountpoint uploads on close; give S3 a moment
            if self.head(key):
                found = True
                break
            time.sleep(5)  # nosemgrep: arbitrary-sleep
        return check(code == 0 and token in out and found,
                     f"session write {name}: exit={code} out={out.strip()[:120]!r} "
                     f"object {key} visible={found}")

    # ── flow ─────────────────────────────────────────────────────────────────

    prefix = "e2e/"

    def run(self) -> None:
        params: dict[str, Any] = {"Bucket": self.bucket}
        if self.ws.region != "us-east-1":
            params["CreateBucketConfiguration"] = {"LocationConstraint": self.ws.region}
        self.s3.create_bucket(**params)
        log(f"bucket {self.bucket}")

        entry = self.api("POST", "/api/storage-mounts", json={
            "id": self.storage_id, "name": "E2E S3 bucket", "kind": "s3bucket",
            "bucket": self.bucket, "prefix": self.prefix, "scope": "per_session",
            "max_mode": "rw", "data_class": "scratch"})
        check(entry.get("status") in ("ok", "warn"),
              f"registered + checked: status={entry.get('status')} "
              f"results={[r.get('check') for r in entry.get('check_results') or []]}")
        current = self.api("GET", f"/api/storage-mounts/projects/{E2E_PROJECT_ID}")
        ids = sorted({*(e["id"] for e in current.get("storage") or []), self.storage_id})
        self.api("PUT", f"/api/storage-mounts/projects/{E2E_PROJECT_ID}",
                 json={"storage_ids": ids})

        spec = {
            "name": f"e2e-s3m-{self.suffix}", "method": "zip_runtime", "model_id": MODEL,
            "system_prompt": "You are a terse test agent. Answer in one short sentence.",
            "toolkits": [],
            "runtime_config": {"platform_version": "V2", "billing_model": "consumption"},
            "filesystem": {"mounts": [{"storage_id": self.storage_id, "mode": "rw"}]},
        }
        body = self.api("POST", "/api/agents", json=spec)
        self.agent_id = body["agent"]["id"]
        agent = self.wait_agent()
        if not check(agent["status"] == "active", f"agent {self.agent_id} deployed"):
            return
        live = rt.get_runtime(self.control, agent["resource_id"])
        self.runtime_name = live.get("agentRuntimeName")
        bucket_entries = [fs for fs in live.get("filesystemConfigurations") or []
                          if "s3BucketMount" in fs]
        expected_prefix = f"{self.prefix}{self.agent_id}/{{runtimeSessionId}}/"
        check(bool(bucket_entries)
              and bucket_entries[0]["s3BucketMount"].get("prefix") == expected_prefix
              and live.get("platformVersion") == "V2",
              f"live runtime: platform={live.get('platformVersion')} mounts={bucket_entries}")

        turn = chat_sse(self.client, str(self.agent_id), "Say hello.")
        session_a = turn["session_id"]
        check(bool(turn.get("text")), f"chat turn answered (session {session_a[:12]}…)")
        self.write_and_find(session_a, "a.txt")

        session_b = new_session()
        code, out = self.command(session_b, f"ls -A {self.mount}")
        check(code == 0 and "a.txt" not in out,
              f"second session sees its own empty folder: exit={code} ls={out.strip()!r}")

        self.api("POST", f"/api/agents/{self.agent_id}/redeploy",
                 json=self.api("GET", f"/api/agents/{self.agent_id}")["spec"])
        agent = self.wait_agent()
        live = rt.get_runtime(self.control, agent["resource_id"])
        check(any("s3BucketMount" in fs for fs in live.get("filesystemConfigurations") or []),
              "re-publish keeps the bucket mount")
        self.write_and_find(new_session(), "after-redeploy.txt")

        detail = self.api("GET", f"/api/storage-mounts/{self.storage_id}")
        policy = detail.get("bucket_policy")
        if check(bool(policy), "storage detail offers a recommended bucket policy"):
            doc = policy if isinstance(policy, dict) else json.loads(policy)
            doc = doc.get("policy", doc) if isinstance(doc, dict) else doc
            self.s3.put_bucket_policy(Bucket=self.bucket, Policy=json.dumps(doc))
            time.sleep(10)  # nosemgrep: arbitrary-sleep — bucket policy propagation
            try:
                self.write_and_find(new_session(), "under-policy.txt")
                check(self.head(f"{self.prefix}{self.agent_id}/{session_a}/a.txt"),
                      "caller (no SourceSid) still reads under the recommended policy")
            finally:
                self.s3.delete_bucket_policy(Bucket=self.bucket)

    # ── cleanup ──────────────────────────────────────────────────────────────

    def cleanup(self) -> None:
        if self.args.keep:
            log(f"--keep: agent {self.agent_id}, entry {self.storage_id}, bucket {self.bucket}")
            return
        if self.agent_id:
            self._quiet("agent", lambda: self.client.delete(f"/api/agents/{self.agent_id}"))
            deadline = time.time() + 600
            while time.time() < deadline:
                res = self.client.get(f"/api/agents/{self.agent_id}")
                if res.status_code == 404 or res.json().get("status") == "deleted":
                    break
                time.sleep(10)  # nosemgrep: arbitrary-sleep
        self._quiet("project allow-list", self._unallow)
        self._quiet("catalog entry",
                    lambda: self.client.delete(f"/api/storage-mounts/{self.storage_id}"))
        self._quiet("bucket", self._delete_bucket)
        self._quiet("log groups", self._delete_log_groups)

    def _unallow(self) -> None:
        current = self.api("GET", f"/api/storage-mounts/projects/{E2E_PROJECT_ID}")
        ids = [e["id"] for e in current.get("storage") or [] if e["id"] != self.storage_id]
        self.api("PUT", f"/api/storage-mounts/projects/{E2E_PROJECT_ID}",
                 json={"storage_ids": ids})

    def _delete_bucket(self) -> None:
        for page in self.s3.get_paginator("list_objects_v2").paginate(Bucket=self.bucket):
            keys = [{"Key": o["Key"]} for o in page.get("Contents", [])]
            if keys:
                self.s3.delete_objects(Bucket=self.bucket, Delete={"Objects": keys})
        self.s3.delete_bucket(Bucket=self.bucket)

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
    args = parser.parse_args()
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
