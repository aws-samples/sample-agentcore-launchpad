#!/usr/bin/env python3
"""Live probes for AgentCore Runtime ``s3BucketMount`` — REAL AWS, throwaway resources.

Settles what the devguide (runtime-filesystem-configurations) leaves open before the
platform offers S3 bucket mounts (task 11-23-s3-bucket-mounts). Unlike
`probe_storage_mounts.py` it talks to the control plane directly: no console spec can
express the mount yet. The agent is a stdlib HTTP server (no model) that performs file
and S3 operations on request, so every verdict is deterministic.

    P1  V2 + PUBLIC create with a fixed prefix; write/read through the mount; the object
        is visible through the S3 API under the prefix
    P2  the same request with platformVersion V1 → the exact refusal
    P3  Harness create with the entry in agentCoreRuntimeEnvironment (accepted? backing
        runtime platform? a shell write lands in the bucket?)
    P4  per-session prefix ``sessions/{runtimeSessionId}/`` + a role policy scoped with
        ``aws:AgentCoreRuntimeSourceSid``: own prefix works, another session's prefix is
        denied (direct S3 calls from inside the session)
    P4b a whole-bucket role + the bucket-policy Deny the agent-files bucket would carry
        (``Null aws:AgentCoreRuntimeSourceSid = false``): sessions stay in their prefix,
        the caller (no SourceSid) still reads everything
    P5  a session id with ``#`` under a ``{runtimeSessionId}`` prefix → error shape
    P6  UpdateAgentRuntime without ``filesystemConfigurations`` clears the mount
    P7  append / rename / in-place edit through the mount → what the agent sees
    P8  VPC network mode (only with --subnet/--security-group; needs an S3 route)
    P9  an execution role whose permissions boundary omits S3 → does the mount fail?
    P10 a mount of a missing bucket → the invoke error (status + message)

Run:  cd backend && uv run python scripts/probe_s3_bucket_mount.py \\
          --account 123456789012 --region us-west-2 \\
          [--probe P1 --probe P4 ...] [--subnet subnet-a --security-group sg-b] \\
          [--harness-model us.anthropic.claude-haiku-4-5-20251001-v1:0] \\
          [--out ../.trellis/tasks/11-23-s3mount-p0-compat-probe/research/probe-results.md] \\
          [--keep]

Everything it creates is named ``launchpad-probe-s3m-*`` / ``probe_s3m_*``, tagged
``launchpad:probe=s3m``, and deleted in ``finally`` (``--keep`` skips that). Not run by
the test suite.
"""

from __future__ import annotations

import argparse
import io
import json
import subprocess
import sys
import tempfile
import time
import traceback
import uuid
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from botocore.exceptions import ClientError  # noqa: E402

from app.services.agentcore import harness as hc  # noqa: E402
from app.services.agentcore import runtime as rt  # noqa: E402
from app.services.agentcore.client import control_client, data_client  # noqa: E402
from app.services.workspace import WorkspaceContext  # noqa: E402

ALL_PROBES = ("P1", "P2", "P3", "P4", "P4b", "P5", "P6", "P7", "P8", "P9", "P10")
TAGS = {"launchpad:probe": "s3m"}
MOUNT = "/mnt/probe"

# The probe agent: /ping + /invocations on :8080, one JSON op per invocation.
AGENT_MAIN = r'''
import json, os, traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def _err(exc):
    return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def op(body):
    kind = body.get("op")
    path = body.get("path")
    try:
        if kind == "write":
            with open(path, "w") as fh:
                fh.write(body["text"])
            return {"ok": True}
        if kind == "read":
            with open(path) as fh:
                return {"ok": True, "result": fh.read()}
        if kind == "list":
            return {"ok": True, "result": sorted(os.listdir(path))}
        if kind == "append":
            with open(path, "a") as fh:
                fh.write(body["text"])
            return {"ok": True}
        if kind == "edit":
            with open(path, "r+") as fh:
                fh.seek(0)
                fh.write(body["text"])
            return {"ok": True}
        if kind == "rename":
            os.rename(body["src"], body["dst"])
            return {"ok": True}
        if kind == "mounts":
            with open("/proc/mounts") as fh:
                return {"ok": True, "result": [l for l in fh if "/mnt" in l]}
        if kind == "s3":
            import boto3
            s3 = boto3.client("s3", region_name=body.get("region"))
            action = body["action"]
            if action == "get":
                obj = s3.get_object(Bucket=body["bucket"], Key=body["key"])
                return {"ok": True, "result": obj["Body"].read().decode()}
            if action == "put":
                s3.put_object(Bucket=body["bucket"], Key=body["key"], Body=b"x")
                return {"ok": True}
            if action == "list":
                res = s3.list_objects_v2(Bucket=body["bucket"], Prefix=body["prefix"])
                return {"ok": True, "result": [o["Key"] for o in res.get("Contents", [])]}
        return {"ok": False, "error": f"unknown op {kind}"}
    except Exception as exc:  # noqa: BLE001 - the probe reports every failure
        return _err(exc)


class H(BaseHTTPRequestHandler):
    def _send(self, code, payload):
        data = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self._send(200, {"status": "Healthy"})

    def do_POST(self):
        size = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(size) or b"{}")
        except Exception:
            body = {}
        try:
            self._send(200, op(body))
        except Exception:
            self._send(200, {"ok": False, "error": traceback.format_exc()[-500:]})

    def log_message(self, *args):
        pass


ThreadingHTTPServer(("0.0.0.0", 8080), H).serve_forever()
'''


def log(msg: str) -> None:
    print(f"{datetime.now(UTC):%H:%M:%S} {msg}", flush=True)


def _code(exc: BaseException) -> str:
    if isinstance(exc, ClientError):
        err = exc.response.get("Error", {})
        status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        return f"{err.get('Code')} (HTTP {status}): {err.get('Message')}"
    return f"{type(exc).__name__}: {exc}"


def session_id() -> str:
    return uuid.uuid4().hex + uuid.uuid4().hex


def build_zip(with_boto3: bool) -> bytes:
    """main.py, plus boto3 vendored at the zip root for the in-session S3 calls."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("main.py", AGENT_MAIN)
        if with_boto3:
            with tempfile.TemporaryDirectory() as tmp:
                # boto3 and its dependencies are pure Python: no platform pinning
                subprocess.run(["uv", "pip", "install", "--quiet", "--target", tmp, "boto3"],
                               check=True)
                root = Path(tmp)
                for path in root.rglob("*"):
                    if path.is_file() and "__pycache__" not in path.parts:
                        zf.write(path, path.relative_to(root).as_posix())
    return buf.getvalue()


class Probe:
    def __init__(self, ws: WorkspaceContext, args: argparse.Namespace) -> None:
        self.ws, self.args = ws, args
        self.suffix = uuid.uuid4().hex[:8]
        self.bucket = f"launchpad-probe-s3m-{ws.account_id}-{self.suffix}"
        self.control = control_client(ws)
        self.data = data_client(ws, read_timeout=120)
        self.s3 = ws.client("s3")
        self.iam = ws.client("iam")
        self.runtimes: list[str] = []
        self.harnesses: list[str] = []
        self.roles: list[str] = []
        self.policies: list[str] = []
        self.results: list[dict[str, Any]] = []
        self.code_key = "code/probe.zip"

    # ── bookkeeping ──────────────────────────────────────────────────────────

    def record(self, probe: str, verdict: str, detail: str, **extra: Any) -> None:
        self.results.append({"probe": probe, "verdict": verdict, "detail": detail, **extra})
        log(f"{probe}: {verdict} — {detail}")

    # ── shared resources ─────────────────────────────────────────────────────

    def setup(self) -> None:
        params: dict[str, Any] = {"Bucket": self.bucket}
        if self.ws.region != "us-east-1":
            params["CreateBucketConfiguration"] = {"LocationConstraint": self.ws.region}
        self.s3.create_bucket(**params)
        self.s3.put_bucket_tagging(Bucket=self.bucket, Tagging={
            "TagSet": [{"Key": k, "Value": v} for k, v in TAGS.items()]})
        log(f"bucket {self.bucket}")
        self.s3.put_object(Bucket=self.bucket, Key=self.code_key, Body=build_zip(True))
        self.main_role = self.role("main", self.main_policy())
        self.wide_role = self.role("wide", self.wide_policy())
        time.sleep(15)  # nosemgrep: arbitrary-sleep — IAM propagation before CreateAgentRuntime

    def _trust(self) -> dict[str, Any]:
        return {"Version": "2012-10-17", "Statement": [{
            "Effect": "Allow", "Principal": {"Service": "bedrock-agentcore.amazonaws.com"},
            "Action": "sts:AssumeRole",
            "Condition": {"StringEquals": {"aws:SourceAccount": self.ws.account_id}}}]}

    def _base_statements(self) -> list[dict[str, Any]]:
        return [
            {"Effect": "Allow", "Action": ["logs:CreateLogGroup", "logs:CreateLogStream",
                                           "logs:PutLogEvents", "logs:DescribeLogStreams"],
             "Resource": "*"},
            {"Effect": "Allow", "Action": ["bedrock:InvokeModel",
                                           "bedrock:InvokeModelWithResponseStream"],
             "Resource": "*"},
            {"Effect": "Allow", "Action": ["s3:GetObject"],
             "Resource": f"arn:aws:s3:::{self.bucket}/{self.code_key}"},
        ]

    def main_policy(self) -> dict[str, Any]:
        """Fixed prefixes `p*/` read-write; `sessions/<own sid>/` only (documented shape)."""
        b = f"arn:aws:s3:::{self.bucket}"
        sid = "${aws:AgentCoreRuntimeSourceSid}"
        rw = ["s3:GetObject", "s3:PutObject", "s3:AbortMultipartUpload", "s3:DeleteObject"]
        return {"Version": "2012-10-17", "Statement": [
            *self._base_statements(),
            {"Effect": "Allow", "Action": "s3:ListBucket", "Resource": b,
             "Condition": {"StringLike": {"s3:prefix": ["p*"]}}},
            {"Effect": "Allow", "Action": rw, "Resource": f"{b}/p*"},
            {"Effect": "Allow", "Action": "s3:ListBucket", "Resource": b,
             "Condition": {"StringLike": {"s3:prefix": [f"sessions/{sid}/*",
                                                        f"sessions/{sid}"]}}},
            {"Effect": "Allow", "Action": rw, "Resource": f"{b}/sessions/{sid}/*"},
        ]}

    def wide_policy(self) -> dict[str, Any]:
        b = f"arn:aws:s3:::{self.bucket}"
        return {"Version": "2012-10-17", "Statement": [
            *self._base_statements(),
            {"Effect": "Allow", "Action": "s3:ListBucket", "Resource": b},
            {"Effect": "Allow", "Action": ["s3:GetObject", "s3:PutObject",
                                           "s3:AbortMultipartUpload", "s3:DeleteObject"],
             "Resource": f"{b}/*"},
        ]}

    def role(self, name: str, policy: dict[str, Any], boundary: str | None = None) -> str:
        role_name = f"launchpad-probe-s3m-{name}-{self.suffix}"
        params: dict[str, Any] = {
            "RoleName": role_name, "AssumeRolePolicyDocument": json.dumps(self._trust()),
            "Tags": [{"Key": k, "Value": v} for k, v in TAGS.items()]}
        if boundary:
            params["PermissionsBoundary"] = boundary
        arn = self.iam.create_role(**params)["Role"]["Arn"]
        self.roles.append(role_name)
        self.iam.put_role_policy(RoleName=role_name, PolicyName="probe",
                                 PolicyDocument=json.dumps(policy))
        return arn

    # ── runtimes ─────────────────────────────────────────────────────────────

    def create_runtime(self, tag: str, mounts: list[dict[str, Any]], *, role: str,
                       platform: str = "V2", network: dict[str, Any] | None = None) -> str:
        name = f"probe_s3m_{tag}_{self.suffix}"
        res = self.control.create_agent_runtime(
            agentRuntimeName=name,
            agentRuntimeArtifact={"codeConfiguration": {
                "code": {"s3": {"bucket": self.bucket, "prefix": self.code_key}},
                "runtime": "PYTHON_3_13", "entryPoint": ["main.py"]}},
            roleArn=role,
            networkConfiguration=network or {"networkMode": "PUBLIC"},
            protocolConfiguration={"serverProtocol": "HTTP"},
            platformVersion=platform,
            filesystemConfigurations=mounts,
            tags=TAGS,
        )
        runtime_id = res["agentRuntimeId"]
        self.runtimes.append(runtime_id)
        return runtime_id

    def ready(self, runtime_id: str) -> dict[str, Any]:
        return rt.wait_runtime_ready(self.control, runtime_id, timeout_s=self.args.timeout,
                                     on_status=lambda s: log(f"  {runtime_id}: {s}"))

    def call(self, runtime_id: str, sid: str, body: dict[str, Any]) -> dict[str, Any]:
        """One invocation; a ClientError comes back as ``{ok: False, aws_error}``."""
        arn = rt.get_runtime(self.control, runtime_id)["agentRuntimeArn"]
        if body.get("op") == "s3":  # the in-session boto3 client needs a Region
            body = {**body, "region": self.ws.region}
        try:
            res = self.data.invoke_agent_runtime(
                agentRuntimeArn=arn, runtimeSessionId=sid, qualifier="DEFAULT",
                contentType="application/json", accept="application/json",
                payload=json.dumps(body).encode())
            raw = res["response"].read()
            return json.loads(raw or b"{}")
        except ClientError as exc:
            return {"ok": False, "aws_error": _code(exc)}

    def stop(self, runtime_id: str, sid: str) -> None:
        arn = rt.get_runtime(self.control, runtime_id)["agentRuntimeArn"]
        try:
            self.data.stop_runtime_session(agentRuntimeArn=arn, runtimeSessionId=sid,
                                           qualifier="DEFAULT")
        except ClientError:
            pass

    def head(self, key: str) -> bool:
        try:
            self.s3.head_object(Bucket=self.bucket, Key=key)
            return True
        except ClientError:
            return False

    # ── probes ───────────────────────────────────────────────────────────────

    def p1(self) -> str:
        rid = self.create_runtime("p1", [{"s3BucketMount": {
            "bucket": self.bucket, "prefix": "p1/", "mountPath": MOUNT}}], role=self.main_role)
        detail = self.ready(rid)
        sid, token = session_id(), uuid.uuid4().hex[:12]
        write = self.call(rid, sid, {"op": "write", "path": f"{MOUNT}/hello.txt", "text": token})
        read = self.call(rid, sid, {"op": "read", "path": f"{MOUNT}/hello.txt"})
        mounts = self.call(rid, sid, {"op": "mounts"})
        visible = self.head("p1/hello.txt")
        ok = write.get("ok") and read.get("result") == token and visible
        self.record("P1", "PASS" if ok else "FAIL",
                    f"write={write} read={read} s3_object_visible={visible}",
                    platform=detail.get("platformVersion"), mounts=mounts.get("result"),
                    live_entry=detail.get("filesystemConfigurations"))
        self.p1_runtime, self.p1_session = rid, sid
        return rid

    def p2(self) -> None:
        try:
            rid = self.create_runtime("p2", [{"s3BucketMount": {
                "bucket": self.bucket, "prefix": "p2/", "mountPath": MOUNT}}],
                role=self.main_role, platform="V1")
        except ClientError as exc:
            self.record("P2", "REFUSED_AT_CREATE", _code(exc))
            return
        try:
            self.ready(rid)
            res = self.call(rid, session_id(), {"op": "list", "path": MOUNT})
            self.record("P2", "ACCEPTED_ON_V1", f"create accepted; invoke={res}")
        except (RuntimeError, TimeoutError) as exc:
            self.record("P2", "FAILED_AFTER_CREATE", str(exc))

    def p3(self) -> None:
        name = f"probe_s3m_h_{self.suffix}"
        params = {
            "harnessName": name, "executionRoleArn": self.main_role,
            "model": {"bedrockModelConfig": {"modelId": self.args.harness_model,
                                             "apiFormat": "converse_stream"}},
            "systemPrompt": [{"text": "You are a file-system probe. Use the shell tool "
                                      "and follow instructions exactly."}],
            "environment": {"agentCoreRuntimeEnvironment": {"filesystemConfigurations": [
                {"s3BucketMount": {"bucket": self.bucket, "prefix": "p3/",
                                   "mountPath": MOUNT}}]}},
            "allowedTools": ["shell", "file_operations"],
            "tags": TAGS,
        }
        try:
            res = hc.create_harness(self.control, params)
        except ClientError as exc:
            self.record("P3", "REFUSED_AT_CREATE", _code(exc))
            return
        harness_id = str(res["harnessId"])
        self.harnesses.append(harness_id)
        try:
            live = hc.wait_harness_ready(self.control, harness_id, timeout_s=self.args.timeout)
        except (RuntimeError, TimeoutError) as exc:
            self.record("P3", "FAILED_AFTER_CREATE", str(exc))
            return
        env = (live.get("environment") or {}).get("agentCoreRuntimeEnvironment") or {}
        backing = {}
        if env.get("agentRuntimeId"):
            backing = rt.get_runtime(self.control, env["agentRuntimeId"])
        token = uuid.uuid4().hex[:12]
        try:
            answer = hc.invoke_harness_text(
                self.data, live["arn"],
                f"Run this exact shell command and reply with its output only: "
                f"echo {token} > {MOUNT}/h.txt && cat {MOUNT}/h.txt")["text"]
        except Exception as exc:  # noqa: BLE001 — the probe reports every failure
            answer = f"invoke failed: {_code(exc)}"
        visible = self.head("p3/h.txt")
        self.record("P3", "PASS" if visible else "FAIL",
                    f"answer={answer[:200]!r} s3_object_visible={visible}",
                    backing_platform=backing.get("platformVersion"),
                    live_entry=env.get("filesystemConfigurations"))

    def p4(self) -> None:
        rid = self.create_runtime("p4", [{"s3BucketMount": {
            "bucket": self.bucket, "prefix": "sessions/{runtimeSessionId}/",
            "mountPath": MOUNT}}], role=self.main_role)
        self.ready(rid)
        a, b = session_id(), session_id()
        self.s3.put_object(Bucket=self.bucket, Key=f"sessions/{b}/seed.txt", Body=b"b-secret")
        wa = self.call(rid, a, {"op": "write", "path": f"{MOUNT}/a.txt", "text": "A"})
        own = self.call(rid, a, {"op": "s3", "action": "get", "bucket": self.bucket,
                                 "key": f"sessions/{a}/a.txt"})
        other_get = self.call(rid, a, {"op": "s3", "action": "get", "bucket": self.bucket,
                                       "key": f"sessions/{b}/seed.txt"})
        other_list = self.call(rid, a, {"op": "s3", "action": "list", "bucket": self.bucket,
                                        "prefix": f"sessions/{b}/"})
        mount_b = self.call(rid, b, {"op": "list", "path": MOUNT})
        ok = (wa.get("ok") and self.head(f"sessions/{a}/a.txt") and own.get("ok")
              and not other_get.get("ok") and not other_list.get("ok")
              and mount_b.get("result") == ["seed.txt"])
        self.record("P4", "PASS" if ok else "FAIL",
                    f"write_own={wa} get_own={own} get_other={other_get} "
                    f"list_other={other_list} session_b_sees={mount_b}")
        self.p4_runtime = rid

    def p4b(self) -> None:
        b = f"arn:aws:s3:::{self.bucket}"
        sid = "${aws:AgentCoreRuntimeSourceSid}"
        policy = {"Version": "2012-10-17", "Statement": [
            {"Sid": "DenyObjectsOutsideOwnSession", "Effect": "Deny", "Principal": "*",
             "Action": ["s3:GetObject", "s3:PutObject", "s3:AbortMultipartUpload",
                        "s3:DeleteObject"],
             "NotResource": f"{b}/sessions/{sid}/*",
             "Condition": {"Null": {"aws:AgentCoreRuntimeSourceSid": "false"}}},
            {"Sid": "DenyListOutsideOwnSession", "Effect": "Deny", "Principal": "*",
             "Action": "s3:ListBucket", "Resource": b,
             "Condition": {"Null": {"aws:AgentCoreRuntimeSourceSid": "false"},
                           "StringNotLike": {"s3:prefix": [f"sessions/{sid}/*",
                                                           f"sessions/{sid}"]}}},
        ]}
        rid = self.create_runtime("p4b", [{"s3BucketMount": {
            "bucket": self.bucket, "prefix": "sessions/{runtimeSessionId}/",
            "mountPath": MOUNT}}], role=self.wide_role)
        self.ready(rid)
        self.s3.put_bucket_policy(Bucket=self.bucket, Policy=json.dumps(policy))
        time.sleep(10)  # nosemgrep: arbitrary-sleep — bucket policy propagation
        try:
            a, b2 = session_id(), session_id()
            self.s3.put_object(Bucket=self.bucket, Key=f"sessions/{b2}/seed.txt", Body=b"x")
            wa = self.call(rid, a, {"op": "write", "path": f"{MOUNT}/a.txt", "text": "A"})
            other = self.call(rid, a, {"op": "s3", "action": "get", "bucket": self.bucket,
                                       "key": f"sessions/{b2}/seed.txt"})
            outside = self.call(rid, a, {"op": "s3", "action": "get", "bucket": self.bucket,
                                         "key": self.code_key})
            caller_reads = self.head(f"sessions/{a}/a.txt")
            ok = wa.get("ok") and not other.get("ok") and not outside.get("ok") and caller_reads
            self.record("P4b", "PASS" if ok else "FAIL",
                        f"write_own={wa} get_other={other} get_outside={outside} "
                        f"caller_reads_session_object={caller_reads}")
        finally:
            self.s3.delete_bucket_policy(Bucket=self.bucket)

    def p5(self) -> None:
        rid = getattr(self, "p4_runtime", None) or self.create_runtime("p5", [{
            "s3BucketMount": {"bucket": self.bucket, "prefix": "sessions/{runtimeSessionId}/",
                              "mountPath": MOUNT}}], role=self.main_role)
        self.ready(rid)
        bad = f"{uuid.uuid4().hex}#feishu#chat-1234"
        res = self.call(rid, bad, {"op": "list", "path": MOUNT})
        self.record("P5", "REFUSED" if not res.get("ok") else "ACCEPTED", f"{res}")

    def p6(self) -> None:
        # its own runtime: clearing P1's would break P7, which reuses P1's mount
        rid = self.create_runtime("p6", [{"s3BucketMount": {
            "bucket": self.bucket, "prefix": "p6/", "mountPath": MOUNT}}], role=self.main_role)
        self.ready(rid)
        self.control.update_agent_runtime(
            agentRuntimeId=rid,
            agentRuntimeArtifact={"codeConfiguration": {
                "code": {"s3": {"bucket": self.bucket, "prefix": self.code_key}},
                "runtime": "PYTHON_3_13", "entryPoint": ["main.py"]}},
            roleArn=self.main_role, networkConfiguration={"networkMode": "PUBLIC"},
            protocolConfiguration={"serverProtocol": "HTTP"})
        detail = self.ready(rid)
        live = detail.get("filesystemConfigurations") or []
        self.record("P6", "CLEARED" if not live else "KEPT",
                    f"filesystemConfigurations after omitted update: {live}",
                    platform_after=detail.get("platformVersion"))

    def p7(self) -> None:
        rid = getattr(self, "p1_runtime", None) or self.p1()
        sid = getattr(self, "p1_session", None) or session_id()
        self.call(rid, sid, {"op": "write", "path": f"{MOUNT}/base.txt", "text": "abc"})
        out = {
            "append": self.call(rid, sid, {"op": "append", "path": f"{MOUNT}/base.txt",
                                           "text": "d"}),
            "edit": self.call(rid, sid, {"op": "edit", "path": f"{MOUNT}/base.txt",
                                         "text": "Z"}),
            "rename": self.call(rid, sid, {"op": "rename", "src": f"{MOUNT}/base.txt",
                                           "dst": f"{MOUNT}/moved.txt"}),
            "overwrite": self.call(rid, sid, {"op": "write", "path": f"{MOUNT}/base.txt",
                                              "text": "new"}),
            "read_back": self.call(rid, sid, {"op": "read", "path": f"{MOUNT}/base.txt"}),
        }
        self.record("P7", "OBSERVED", json.dumps(out)[:800])

    def p8(self) -> None:
        if not (self.args.subnet and self.args.security_group):
            self.record("P8", "SKIPPED", "needs --subnet and --security-group")
            return
        network = {"networkMode": "VPC", "networkModeConfig": {
            "subnets": self.args.subnet, "securityGroups": self.args.security_group}}
        rid = self.create_runtime("p8", [{"s3BucketMount": {
            "bucket": self.bucket, "prefix": "p8/", "mountPath": MOUNT}}],
            role=self.main_role, network=network)
        self.ready(rid)
        sid = session_id()
        res = self.call(rid, sid, {"op": "write", "path": f"{MOUNT}/v.txt", "text": "v"})
        visible = self.head("p8/v.txt")
        self.record("P8", "PASS" if res.get("ok") and visible else "FAIL",
                    f"write={res} s3_object_visible={visible}")

    def p9(self) -> None:
        boundary = {"Version": "2012-10-17", "Statement": [
            {"Effect": "Allow", "Action": ["logs:*"], "Resource": "*"},
            {"Effect": "Allow", "Action": ["s3:GetObject"],
             "Resource": f"arn:aws:s3:::{self.bucket}/{self.code_key}"}]}
        arn = self.iam.create_policy(
            PolicyName=f"launchpad-probe-s3m-boundary-{self.suffix}",
            PolicyDocument=json.dumps(boundary))["Policy"]["Arn"]
        self.policies.append(arn)
        role = self.role("bounded", self.wide_policy(), boundary=arn)
        time.sleep(15)  # nosemgrep: arbitrary-sleep — IAM propagation
        rid = self.create_runtime("p9", [{"s3BucketMount": {
            "bucket": self.bucket, "prefix": "p9/", "mountPath": MOUNT}}], role=role)
        self.ready(rid)
        res = self.call(rid, session_id(), {"op": "list", "path": MOUNT})
        self.record("P9", "BOUNDARY_ENFORCED" if not res.get("ok") else "BOUNDARY_IGNORED",
                    f"{res}")

    def p10(self) -> None:
        rid = self.create_runtime("p10", [{"s3BucketMount": {
            "bucket": f"launchpad-probe-s3m-missing-{self.suffix}", "mountPath": MOUNT}}],
            role=self.main_role)
        try:
            self.ready(rid)
        except (RuntimeError, TimeoutError) as exc:
            self.record("P10", "FAILED_AT_CREATE", str(exc))
            return
        res = self.call(rid, session_id(), {"op": "list", "path": MOUNT})
        self.record("P10", "OBSERVED", f"{res}")

    # ── cleanup + report ─────────────────────────────────────────────────────

    def cleanup(self) -> None:
        if self.args.keep:
            log(f"--keep: bucket {self.bucket}, runtimes {self.runtimes}, "
                f"harnesses {self.harnesses}, roles {self.roles}")
            return
        for harness_id in self.harnesses:
            self._quiet(f"harness {harness_id}", hc.delete_harness, self.control, harness_id)
        for runtime_id in self.runtimes:
            self._quiet(f"runtime {runtime_id}", rt.delete_runtime, self.control, runtime_id)
        # a role deleted under a still-DELETING runtime can wedge that deletion
        self._quiet("runtimes gone", self._wait_gone)
        for role_name in self.roles:
            self._quiet(f"role {role_name}", self._delete_role, role_name)
        for arn in self.policies:
            self._quiet(f"policy {arn}", self.iam.delete_policy, PolicyArn=arn)
        self._quiet(f"bucket {self.bucket}", self._delete_bucket)
        self._quiet("runtime log groups", self._delete_log_groups)

    def _delete_log_groups(self) -> None:
        """The `/aws/bedrock-agentcore/runtimes/<name>-<id>-DEFAULT` groups the runtimes made."""
        logs = self.ws.client("logs")
        prefix = "/aws/bedrock-agentcore/runtimes/probe_s3m_"
        for page in logs.get_paginator("describe_log_groups").paginate(logGroupNamePrefix=prefix):
            for group in page.get("logGroups", []):
                if self.suffix in group["logGroupName"]:
                    logs.delete_log_group(logGroupName=group["logGroupName"])

    def _quiet(self, what: str, fn: Any, *a: Any, **kw: Any) -> None:
        try:
            fn(*a, **kw)
            log(f"deleted {what}")
        except Exception as exc:  # noqa: BLE001 — cleanup reports, never stops
            log(f"cleanup of {what} failed: {_code(exc)}")

    def _wait_gone(self, timeout_s: int = 300) -> None:
        deadline = time.monotonic() + timeout_s
        pending = set(self.runtimes)
        while pending and time.monotonic() < deadline:
            for runtime_id in sorted(pending):
                try:
                    rt.get_runtime(self.control, runtime_id)
                except ClientError as exc:
                    if exc.response.get("Error", {}).get("Code") == "ResourceNotFoundException":
                        pending.discard(runtime_id)
            if pending:
                time.sleep(10)  # nosemgrep: arbitrary-sleep — polling a deletion
        if pending:
            log(f"runtimes still deleting after {timeout_s}s: {sorted(pending)}")

    def _delete_role(self, role_name: str) -> None:
        for name in self.iam.list_role_policies(RoleName=role_name)["PolicyNames"]:
            self.iam.delete_role_policy(RoleName=role_name, PolicyName=name)
        try:
            self.iam.delete_role_permissions_boundary(RoleName=role_name)
        except ClientError:
            pass
        self.iam.delete_role(RoleName=role_name)

    def _delete_bucket(self) -> None:
        paginator = self.s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket):
            keys = [{"Key": o["Key"]} for o in page.get("Contents", [])]
            if keys:
                self.s3.delete_objects(Bucket=self.bucket, Delete={"Objects": keys})
        self.s3.delete_bucket(Bucket=self.bucket)

    def report(self) -> str:
        lines = [
            "# s3BucketMount probe results", "",
            f"- date: {datetime.now(UTC):%Y-%m-%d %H:%M} UTC",
            f"- account/region: {self.ws.account_id} / {self.ws.region}",
            f"- suffix: {self.suffix}", "",
            "| Probe | Verdict | Detail |", "|---|---|---|",
        ]
        for r in self.results:
            detail = r["detail"].replace("|", "\\|").replace("\n", " ")[:600]
            lines.append(f"| {r['probe']} | {r['verdict']} | {detail} |")
        lines += ["", "## Raw", "", "```json",
                  json.dumps(self.results, indent=2, default=str), "```", ""]
        return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--account", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--probe", action="append", choices=ALL_PROBES)
    parser.add_argument("--subnet", action="append")
    parser.add_argument("--security-group", action="append")
    parser.add_argument("--harness-model",
                        default="us.anthropic.claude-haiku-4-5-20251001-v1:0")
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument("--out")
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args()
    probes = args.probe or list(ALL_PROBES)
    ws = WorkspaceContext(account_id=args.account, region=args.region)
    probe = Probe(ws, args)
    try:
        probe.setup()
        for name in probes:
            try:
                getattr(probe, name.lower())()
            except Exception as exc:  # noqa: BLE001 — one probe failing must not stop the rest
                probe.record(name, "ERROR", f"{_code(exc)}\n{traceback.format_exc()[-400:]}")
    finally:
        probe.cleanup()
        text = probe.report()
        if args.out:
            Path(args.out).write_text(text)
            log(f"wrote {args.out}")
        print(text)
    return 0 if all(r["verdict"] not in ("FAIL", "ERROR") for r in probe.results) else 1


if __name__ == "__main__":
    sys.exit(main())
