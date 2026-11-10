"""Portable workshop context. Only explicit config selects a live environment."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import time
from pathlib import Path
from urllib.parse import urlparse

import boto3
import requests

PACKAGE = Path(__file__).resolve().parents[1]
LEARNERS = [f"lab{i:02}" for i in range(1, 11)]
# Settings that may change between runs of the SAME environment (they are excluded from the
# run identity digest): the service-desk account list and its per-account publish targets,
# and the model (N17-obs557: the model is an experiment-time choice — the organizer flips
# ``model_id`` in the lab config and re-runs ``reset_student.py --apply``; a digest that
# included it made every script refuse the state_dir after the change, so the manifest copy
# was edited by hand instead). ``initialize()`` still records ``model_id`` into the manifest
# (the card renders it); ``student_kit_lib.sync_model_id`` keeps that copy equal to the config.
# ``employee_agent_prebuilt`` (default true) only selects the wording of the card's 员工 Agent
# row (organizer manual 1.12: 已预建，直接打开 / 未预建，场景一 3.1.1 由学员创建); flipping it
# between ``--cards-only`` runs must not change the run identity either.
MUTABLE_KEYS = ("staff_accounts", "staff_publish_targets", "model_id", "employee_agent_prebuilt")
USERNAME = r"[a-z][a-z0-9_-]{0,31}"
S3_PREFIX_URI = r"s3://[a-z0-9][a-z0-9.-]{1,61}[a-z0-9](/[A-Za-z0-9_./-]*)?"


def read_config(path: Path) -> dict:
    cfg = json.loads(path.read_text())
    for key in ("scenario", "tools_user"):
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", cfg[key]):
            raise ValueError(f"Invalid {key}")
    if not re.fullmatch(r"\d{12}", cfg["account_id"]):
        raise ValueError("Specify the destination account_id")
    if cfg["region"] != "cn-northwest-1":
        raise ValueError("This lab targets cn-northwest-1")
    if not re.fullmatch(r"i-[a-f0-9]{8,17}", cfg["tools_host"]):
        raise ValueError("tools_host must be an existing SSM-managed instance")
    if not 1024 <= int(cfg["tools_port"]) <= 65535:
        raise ValueError("tools_port must be between 1024 and 65535")
    if not isinstance(cfg.get("console_http_lab", False), bool):
        raise ValueError("console_http_lab must be a boolean")
    scheme = "http" if cfg.get("console_http_lab") else "https"
    for key in ("console", "api_base"):
        url = urlparse(cfg[key])
        if (url.scheme != scheme or not url.hostname or url.username or url.password
                or url.path not in ("", "/") or url.query or url.fragment):
            raise ValueError(f"{key} must be an {scheme.upper()} origin without credentials")
    if cfg["console"].rstrip("/") != cfg["api_base"].rstrip("/"):
        raise ValueError("console and api_base must use the same primary origin")
    if not isinstance(cfg.get("tls_verify", True), (bool, str)):
        raise ValueError("tls_verify must be true, false, or a CA file path")
    if not isinstance(cfg.get("employee_agent_prebuilt", True), bool):
        raise ValueError("employee_agent_prebuilt must be a boolean")
    cfg["state_dir"] = str((path.parent / cfg["state_dir"]).resolve())
    staff = cfg.get("staff_accounts", ["itdesk01"])
    if (not isinstance(staff, list) or not staff or len(set(staff)) != len(staff)
            or not all(isinstance(s, str) and re.fullmatch(USERNAME, s) for s in staff)):
        raise ValueError("staff_accounts must be a non-empty list of distinct lowercase usernames")
    if set(staff) & set(LEARNERS):
        raise ValueError("staff_accounts must not reuse the learner names lab01-lab10")
    targets = cfg.get("staff_publish_targets", {})
    if (not isinstance(targets, dict) or set(targets) - set(staff)
            or not all(isinstance(v, str) and re.fullmatch(S3_PREFIX_URI, v)
                       for v in targets.values())):
        raise ValueError("staff_publish_targets must map staff usernames to s3://bucket/prefix")
    cfg["staff_accounts"] = staff
    cfg["staff_publish_targets"] = {u: t.rstrip("/") + "/" for u, t in targets.items()}
    return cfg


if not os.environ.get("LAUNCHPAD_LAB_CONFIG"):
    raise RuntimeError("Set LAUNCHPAD_LAB_CONFIG to your own config JSON; no default live target")
CONFIG = read_config(Path(os.environ["LAUNCHPAD_LAB_CONFIG"]).resolve())
ROOT = Path(CONFIG["state_dir"])
BASE = CONFIG["api_base"].rstrip("/")
PUBLIC = CONFIG["console"].rstrip("/")
SCENARIO = CONFIG["scenario"]
NAME = "launchpad-" + SCENARIO
PREFIX = "/launchpad/" + SCENARIO + "/"
AWS = boto3.Session(profile_name=CONFIG.get("profile"), region_name=CONFIG["region"])
SSM = AWS.client("ssm")
STAFF = CONFIG["staff_accounts"]
STAFF_PUBLISH_TARGETS = CONFIG["staff_publish_targets"]
CONFIG_DIGEST = hashlib.sha256(
    json.dumps({k: v for k, v in CONFIG.items() if k not in MUTABLE_KEYS}, sort_keys=True).encode()
).hexdigest()
# N17-obs557 migration: manifests written before ``model_id`` became mutable carry the digest
# that still included it. Such a manifest is accepted once and re-stamped with the current
# digest on the next save, so existing environments survive the change without a hand edit.
_LEGACY_MUTABLE_KEYS = tuple(k for k in MUTABLE_KEYS if k != "model_id")


def legacy_config_digest(model_id):
    """The pre-obs557 digest of this config as it stood with ``model_id`` — the manifest keeps
    the value the digest was stamped with, so an environment whose config has since moved to
    another model is still recognised."""
    merged = {**{k: v for k, v in CONFIG.items() if k not in _LEGACY_MUTABLE_KEYS},
              "model_id": model_id}
    return hashlib.sha256(json.dumps(merged, sort_keys=True).encode()).hexdigest()


LEGACY_CONFIG_DIGEST = legacy_config_digest(CONFIG["model_id"])


def staff_environment(shared_target: str) -> dict[str, str]:
    """The two service environment values: every staff account publishes to its configured
    target, or to the shared knowledge-base source when none is configured."""
    targets = {u: STAFF_PUBLISH_TARGETS.get(u, shared_target) for u in STAFF}
    env = {
        "ITSM_STAFF_ACCOUNTS": ",".join(STAFF),
        "ITSM_STAFF_PUBLISH_TARGETS": json.dumps(targets, separators=(",", ":"), sort_keys=True),
    }
    for value in env.values():
        if "'" in value or "\n" in value:  # written as single-quoted systemd Environment= values
            raise ValueError("staff settings must not contain quotes or newlines")
    return env


def publish_targets(shared_target: str) -> list[str]:
    """Every ``s3://bucket/prefix/`` the Gateway Lambda may publish an article to: the shared
    knowledge-base source plus each configured ``staff_publish_targets`` value (sorted, unique)."""
    targets = json.loads(staff_environment(shared_target)["ITSM_STAFF_PUBLISH_TARGETS"]).values()
    return sorted(set(targets) | {shared_target})


def publish_grant_resources(targets) -> list[str]:
    """The S3 object ARNs of ``publish_targets`` (``arn:aws-cn:s3:::<bucket>/<prefix>*``)."""
    return ["arn:aws-cn:s3:::" + t[len("s3://"):] + "*" for t in targets]


PUBLISH_POLICY_NAME = "PublishOnlyWorkshopKnowledge"


def publish_policy_document(targets) -> dict:
    """The Lambda role's inline policy: ``s3:PutObject`` on the publish targets only."""
    return {
        "Version": "2012-10-17",
        "Statement": [
            {"Effect": "Allow", "Action": "s3:PutObject",
             "Resource": publish_grant_resources(targets)}
        ],
    }


def publish_endpoint_policy_document(targets) -> dict:
    """The Lambda's S3 Gateway VPC endpoint policy: the same ``s3:PutObject`` grant for any
    principal (the endpoint has no identity of its own; the role policy narrows the caller)."""
    return {
        "Version": "2012-10-17",
        "Statement": [
            {"Effect": "Allow", "Principal": "*", "Action": "s3:PutObject",
             "Resource": publish_grant_resources(targets)}
        ],
    }


def _as_list(value) -> list:
    return list(value) if isinstance(value, list) else [value]


def _policy_shape(document: dict) -> list[tuple]:
    """A policy document reduced to what it grants (AWS may re-order or re-list fields)."""
    shape = []
    for st in _as_list(document.get("Statement") or []):
        principal = st.get("Principal")
        if isinstance(principal, dict):
            principal = json.dumps(principal, sort_keys=True)
        shape.append((
            st.get("Effect"), tuple(sorted(_as_list(st.get("Action") or []))),
            principal, tuple(sorted(_as_list(st.get("Resource") or []))),
        ))
    return sorted(shape, key=repr)


def grant_publish_targets(manifest: dict, targets, *, role_name: str | None = None) -> dict:
    """N17-obs525: let the Gateway Lambda publish to EVERY target — (a) the inline role policy
    ``PublishOnlyWorkshopKnowledge`` on the Lambda role (``manifest.roles.lambda``, else
    ``NAME-lambda``) and (b) the S3 Gateway VPC endpoint policy the Lambda reaches S3 through
    (``manifest.lambda_s3_endpoint``, when the manifest has one — a target missing from either
    grant publishes with AccessDenied). The endpoint is modified only when its live policy
    grants something else. Returns ``{"role": <name>, "endpoint": <id> | None,
    "endpoint_changed": bool, "targets": [...]}``."""
    targets = sorted(set(targets))
    if role_name is None:
        role_arn = str((manifest.get("roles") or {}).get("lambda") or "")
        role_name = role_arn.rsplit("/", 1)[-1] if role_arn else NAME + "-lambda"
    AWS.client("iam").put_role_policy(
        RoleName=role_name,
        PolicyName=PUBLISH_POLICY_NAME,
        PolicyDocument=json.dumps(publish_policy_document(targets)),
    )
    result = {"role": role_name, "endpoint": None, "endpoint_changed": False, "targets": targets}
    endpoint = manifest.get("lambda_s3_endpoint")
    if endpoint:
        ec2 = AWS.client("ec2")
        wanted = publish_endpoint_policy_document(targets)
        current = ec2.describe_vpc_endpoints(VpcEndpointIds=[endpoint])["VpcEndpoints"][0]
        live = json.loads(current.get("PolicyDocument") or "{}")
        result["endpoint"] = endpoint
        if _policy_shape(live) != _policy_shape(wanted):
            ec2.modify_vpc_endpoint(VpcEndpointId=endpoint, PolicyDocument=json.dumps(wanted))
            result["endpoint_changed"] = True
    return result


# N17-obs558: the package is the source of truth for the lab assets; the state_dir copy every
# script reads (``ROOT / "lab-assets/..."``) is a cache. ``lab-assets/`` mirrors the package
# (new and changed files are copied over); ``policies/`` is only seeded (missing files added)
# because ``prepare_policy.py`` renders the Cedar for the configured staff accounts into the
# state_dir copy and ``build_manual.py`` reads that rendered text.
ASSET_DIRS = {"lab-assets": "mirror", "policies": "seed"}


def sync_assets(package: Path | None = None, root: Path | None = None, *, quiet: bool = False):
    """Refresh the state_dir asset cache from the package (idempotent; byte comparison).

    Copies every ``lab-assets/`` file that is missing or differs, adds missing ``policies/``
    files, never deletes extra files (generated policies, scratch) and skips ``__pycache__``.
    Returns the changed paths relative to the state_dir and prints one line when non-empty."""
    package = PACKAGE if package is None else Path(package)
    root = ROOT if root is None else Path(root)
    changed: list[str] = []
    for dirname, mode in ASSET_DIRS.items():
        source = package / dirname
        if not source.is_dir():
            continue
        for src in sorted(source.rglob("*")):
            if "__pycache__" in src.parts or not src.is_file():
                continue
            dst = root / src.relative_to(package)
            if dst.exists() and (mode == "seed" or dst.read_bytes() == src.read_bytes()):
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dst)
            changed.append(dst.relative_to(root).as_posix())
    if changed and not quiet:
        print(f"Assets synced from the package ({len(changed)} file(s)): " + ", ".join(changed),
              flush=True)
    return changed


def initialize() -> None:
    """Explicit run directory, never the shipped example or another run's ledger."""
    actual = AWS.client("sts").get_caller_identity()["Account"]
    if actual != CONFIG["account_id"]:
        raise RuntimeError("AWS account does not match config; no changes made")
    ROOT.mkdir(parents=True, exist_ok=True)
    load()  # refuse a config change before copying anything
    sync_assets()
    for name in ("handbook.md", "doc-page.js"):
        shutil.copyfile(PACKAGE / name, ROOT / name)
    (ROOT / "evidence").mkdir(exist_ok=True)
    save(load())


def secret(name):
    return SSM.get_parameter(Name=name, WithDecryption=True)["Parameter"]["Value"]


def session(user=None):
    user = user or CONFIG["admin_username"]
    c = requests.Session()
    c.verify = CONFIG.get("tls_verify", True)
    c.headers["X-Workspace"] = CONFIG["workspace"]
    key = (
        CONFIG["admin_password_parameter"]
        if user == CONFIG["admin_username"]
        else PREFIX + "users/" + user
    )
    r = c.post(
        BASE + "/api/auth/login", json={"username": user, "password": secret(key)}, timeout=60
    )
    if r.status_code != 200:
        raise RuntimeError(f"Login {user}: HTTP {r.status_code}")
    return c


def api(c, method, path, body=None, *, schema_read_attempts=1, **kw):
    for attempt in range(max(1, schema_read_attempts)):
        r = c.request(method, BASE + path, json=body, timeout=180, **kw)
        try:
            out = r.json()
        except ValueError:
            raise RuntimeError(f"{method} {path}: HTTP {r.status_code}, not JSON") from None
        if r.ok:
            return out
        detail = out.get("detail") or {}
        # A just-updated Gateway may be READY in the control plane while tools/list
        # still returns an HTTP error. Retry only this read; never a publish or
        # a Cedar/compiler failure. The caller explicitly opts in for validation.
        waiting_for_schema = (
            path.endswith("/validate")
            and r.status_code == 409
            and out.get("code") == "policy.compiler_unavailable"
            and isinstance(detail, dict)
            and detail.get("source") == "tools_list"
            and detail.get("reason") == "HTTPStatusError"
        )
        if waiting_for_schema and attempt + 1 < schema_read_attempts:
            print("Gateway schema is not ready; retrying validation in 10 seconds", flush=True)
            time.sleep(10)
            continue
        raise RuntimeError(f"{method} {path}: HTTP {r.status_code}: {str(out)[:600]}")


def load():
    p = ROOT / "manifest.json"
    if p.exists():
        state = json.loads(p.read_text())
        stored = state.get("config_sha256")
        legacy = {LEGACY_CONFIG_DIGEST, legacy_config_digest(state.get("model_id"))}
        if stored != CONFIG_DIGEST and stored in legacy:
            state["config_sha256"] = CONFIG_DIGEST  # pre-obs557 manifest: adopt, re-stamp on save
        if state.get("config_sha256") != CONFIG_DIGEST:
            raise RuntimeError("This state_dir belongs to another config; use its original config")
        return state
    return {
        "config_sha256": CONFIG_DIGEST,
        "scenario": SCENARIO,
        "workspace": CONFIG["workspace"],
        "region": CONFIG["region"],
        "account_id": CONFIG["account_id"],
        "console": PUBLIC,
        "model_id": CONFIG["model_id"],
        "users": {},
        "skills": {},
        "datasets": {},
        "agents": {},
        "kb": None,
        "gateway": None,
        "reference_host": CONFIG["tools_host"],
        "shared_host_ownership": "referenced",
    }


def merge_save(state, keys):
    """Write only ``keys`` of ``state`` into the manifest under the shared lock."""
    import fcntl

    with (ROOT / "manifest.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        merged = load()
        for key in keys:
            if key in state:
                merged[key] = state[key]
        save(merged)


def save(state):
    tmp = ROOT / "manifest.tmp"
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2, default=str) + "\n")
    tmp.replace(ROOT / "manifest.json")


def evidence(name, obj):
    (ROOT / "evidence").mkdir(parents=True, exist_ok=True)
    (ROOT / "evidence" / name).write_text(
        json.dumps(obj, ensure_ascii=False, indent=2, default=str) + "\n"
    )


def run_marker_guard(path: str, *, legacy_digests=()) -> str:
    """Shell preamble for ``remote`` scripts that act on the installed scenario directory
    ``path`` (already shell-quoted): the host's ``.scenario`` / ``.run`` markers must name this
    scenario and run. N17-obs559: a bare ``test`` failed with exit 1 and no output, so
    ``evidence/<label>.json`` could not explain why; the guard echoes both values before it
    exits. A ``.run`` marker that still carries a legacy digest (pre-obs557, ``model_id`` in
    the digest) is accepted and rewritten to the current digest — the same adoption ``load()``
    performs on the manifest — and a migration line is printed."""
    legacy = sorted({str(d) for d in legacy_digests if d and d != CONFIG_DIGEST})
    current = shlex.quote(CONFIG_DIGEST)
    migrate = ""
    if legacy:
        migrate = f"""
  {"|".join(shlex.quote(d) for d in legacy)})
    echo "run marker migrated: $RUN_MARKER -> {CONFIG_DIGEST} (N17-obs557 digest change)"
    printf '%s' {current} > {path}/.run ;;"""
    return f"""SCENARIO_MARKER="$(cat {path}/.scenario)"
if [ "$SCENARIO_MARKER" != {shlex.quote(SCENARIO)} ]; then
  echo "scenario mismatch: host .scenario=$SCENARIO_MARKER expected={SCENARIO}"; exit 1
fi
RUN_MARKER="$(cat {path}/.run)"
case "$RUN_MARKER" in
  {current}) ;;{migrate}
  *) echo "run mismatch: host .run=$RUN_MARKER expected={CONFIG_DIGEST}"; exit 1 ;;
esac"""


def remote(script, label, timeout=300):
    cid = SSM.send_command(
        InstanceIds=[CONFIG["tools_host"]],
        DocumentName="AWS-RunShellScript",
        Parameters={"commands": [script], "executionTimeout": [str(timeout)]},
        Comment=f"{SCENARIO}: {label}",
    )["Command"]["CommandId"]
    deadline = time.monotonic() + timeout + 60
    while time.monotonic() < deadline:
        try:
            result = SSM.get_command_invocation(CommandId=cid, InstanceId=CONFIG["tools_host"])
        except SSM.exceptions.InvocationDoesNotExist:
            time.sleep(2)
            continue
        if result["Status"] not in ("Pending", "InProgress", "Delayed"):
            safe = {
                k: result.get(k)
                for k in ("CommandId", "Status", "StandardOutputContent", "StandardErrorContent")
            }
            evidence(label + ".json", safe)
            if result["Status"] != "Success":
                raise RuntimeError(f"SSM {label} failed; see evidence/{label}.json")
            return safe
        time.sleep(3)
    raise TimeoutError(f"SSM command {cid} still pending; inspect before retrying")
