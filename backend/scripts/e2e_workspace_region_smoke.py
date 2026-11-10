#!/usr/bin/env python3
"""Release smoke (REAL AWS, deploy-ops): can this hub provision a workspace in the OTHER CN region?

N17-obs112 (2026-10-20): a same-account workspace in `cn-north-1` failed at `validate-access`
with `ecr DescribeRepositories … AccessDeniedException`, because the hub's EC2 role pins its
ECR / CodeBuild / Secrets Manager / SSM statements to the stack's region. The default host now
runs with `HubAdminAccess=true`; t1 stays least-privilege. This script is the live proof, run
after every host update (`docs/cn/interfaces/deploy.md` §8.1 "After each host update"):

  1. register a throwaway workspace `smoke-<region>-<stamp>` in the region given (the same
     account as the hub — `GET /api/workspaces/hub-identity` — unless `--account` says otherwise);
  2. `POST /api/workspaces/{id}/bootstrap {"validate_only": true}` — the `validate-access` stage
     ALONE: nothing is provisioned, the row keeps its status, the job's result is the verdict;
  3. poll `GET /api/jobs/{job_id}` (`X-Workspace: <id>`) until the job is terminal;
  4. ALWAYS remove the workspace again (`POST /api/workspaces/{id}/purge` — the job row makes a
     plain DELETE a `409 workspace.in_use`).

Exit codes:
  0 — the job SUCCEEDED: the hub can provision a workspace in that region;
  2 — the job FAILED with an access denial (`AccessDenied*`, `UnauthorizedOperation`): on a
      least-privilege host (t1, `HubAdminAccess=false`) this is BY DESIGN — the message says so
      and names the stage detail; on the default host it is a regression;
  1 — anything else (a non-denial failure, a region the service does not serve, a timeout, a
      refused registration, the purge failing — the workspace id is printed so it can be
      purged by hand).

Run on the host (the backend's own credentials and the console admin session):

  cd /home/ubuntu/launchpad/backend
  LAUNCHPAD_E2E_USERNAME=<admin> LAUNCHPAD_E2E_PASSWORD=<pw> \\
    uv run python scripts/e2e_workspace_region_smoke.py --region cn-north-1 [--base http://127.0.0.1:8000]

Hermetic tests never run this (C8); it is not part of `make verify`.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import UTC, datetime
from typing import Any

import httpx
from _e2e_client import e2e_client

EXIT_OK, EXIT_ERROR, EXIT_DENIED = 0, 1, 2
DENIAL_CODES = ("AccessDenied", "AccessDeniedException", "UnauthorizedOperation")
# `routers/workspaces.py` slug rule: 2–32 chars, lowercase / digits / `-`, alphanumeric first
_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{1,31}$")


def _say(message: str) -> None:
    print(f"[smoke] {message}", flush=True)


def _fail(message: str, code: int = EXIT_ERROR) -> int:
    print(f"[smoke] FAIL: {message}", file=sys.stderr, flush=True)
    return code


def _body(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return response.text[:400]


def workspace_id_for(region: str, now: datetime | None = None) -> str:
    stamp = (now or datetime.now(UTC)).strftime("%Y%m%d%H%M%S")
    candidate = f"smoke-{region}-{stamp}"
    if not _SLUG.match(candidate):
        raise ValueError(f"{candidate!r} is not a valid workspace id")
    return candidate


def hub_account(client: httpx.Client) -> str:
    response = client.get("/api/workspaces/hub-identity")
    if response.status_code != 200:
        raise RuntimeError(f"GET /api/workspaces/hub-identity → {response.status_code} "
                           f"{_body(response)}")
    return str(response.json()["account_id"])


def register(client: httpx.Client, workspace_id: str, account: str, region: str) -> None:
    response = client.post("/api/workspaces", json={
        "id": workspace_id,
        "name": f"release smoke {region}",
        "account_id": account,
        "region": region,
    })
    if response.status_code != 201:
        raise RuntimeError(f"POST /api/workspaces → {response.status_code} {_body(response)}")


def start_validate_only(client: httpx.Client, workspace_id: str) -> str:
    response = client.post(f"/api/workspaces/{workspace_id}/bootstrap",
                           json={"validate_only": True})
    if response.status_code != 202:
        raise RuntimeError(f"POST …/bootstrap → {response.status_code} {_body(response)}")
    ack = response.json()
    if ack.get("mode") != "validate-only":
        raise RuntimeError(f"the backend did not run a validate-only job: {ack}")
    return str(ack["job_id"])


def wait_terminal(client: httpx.Client, workspace_id: str, job_id: str, timeout_s: float,
                  poll_s: float = 2.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_s
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        response = client.get(f"/api/jobs/{job_id}", headers={"X-Workspace": workspace_id})
        if response.status_code != 200:
            raise RuntimeError(f"GET /api/jobs/{job_id} → {response.status_code} "
                               f"{_body(response)}")
        last = response.json()
        if last.get("status") in ("succeeded", "failed"):
            return last
        time.sleep(poll_s)
    raise TimeoutError(f"job {job_id} still {last.get('status')!r} after {timeout_s:.0f}s")


def purge(client: httpx.Client, workspace_id: str) -> None:
    response = client.post(f"/api/workspaces/{workspace_id}/purge")
    if response.status_code != 200:
        raise RuntimeError(f"POST …/purge → {response.status_code} {_body(response)}")


def stage_detail(job: dict[str, Any]) -> str:
    for entry in (job.get("payload") or {}).get("stages") or []:
        if entry.get("name") == "validate-access":
            return str(entry.get("detail") or "")
    return ""


def is_denial(job: dict[str, Any]) -> bool:
    text = f"{job.get('error') or ''} {stage_detail(job)}"
    return any(code in text for code in DENIAL_CODES)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--region", required=True,
                        help="the workspace region to probe (the hub's OTHER CN region)")
    parser.add_argument("--base", default="http://127.0.0.1:8000")
    parser.add_argument("--account", default=None,
                        help="account id (default: the hub's, from /api/workspaces/hub-identity)")
    parser.add_argument("--timeout", type=float, default=300.0,
                        help="seconds to wait for the job (default 300)")
    parser.add_argument("--keep", action="store_true",
                        help="do NOT purge the throwaway workspace afterwards (debugging)")
    args = parser.parse_args()

    client = e2e_client(args.base, timeout=60)
    workspace_id = workspace_id_for(args.region)
    try:
        account = args.account or hub_account(client)
    except Exception as exc:  # noqa: BLE001 — every failure is the exit code + message
        return _fail(f"cannot read the hub identity: {exc}")

    _say(f"hub {args.base} · account {account} · probing region {args.region} as {workspace_id}")
    registered = False
    exit_code = EXIT_ERROR
    try:
        register(client, workspace_id, account, args.region)
        registered = True
        job_id = start_validate_only(client, workspace_id)
        _say(f"validate-only job {job_id} queued")
        job = wait_terminal(client, workspace_id, job_id, args.timeout)
        detail = stage_detail(job)
        if job["status"] == "succeeded":
            _say(f"PASS: {detail} — the hub can provision a workspace in {args.region}")
            exit_code = EXIT_OK
        elif is_denial(job):
            print(
                "[smoke] DENIED (exit 2): the hub role may not provision a workspace in "
                f"{args.region}.\n        stage detail: {detail or job.get('error')}\n"
                "        On a least-privilege host (t1, HubAdminAccess=false) this is BY DESIGN "
                "— the scoped launchpad-bootstrap policy is pinned to the stack's region "
                "(N17-obs112). On the default host (HubAdminAccess=true) it is a regression: "
                "check the stack's HubAdminAccess parameter and the instance role's managed "
                "policies.",
                file=sys.stderr, flush=True,
            )
            exit_code = EXIT_DENIED
        else:
            exit_code = _fail(f"validate-access failed for a reason other than a denial: "
                              f"{detail or job.get('error')}")
        print(json.dumps({"workspace_id": workspace_id, "job_id": job_id,
                          "status": job["status"], "detail": detail}, ensure_ascii=False))
    except Exception as exc:  # noqa: BLE001 — every failure is the exit code + message
        exit_code = _fail(f"{type(exc).__name__}: {exc}")
    finally:
        if registered and not args.keep:
            try:
                purge(client, workspace_id)
                _say(f"purged throwaway workspace {workspace_id}")
            except Exception as exc:  # noqa: BLE001
                exit_code = _fail(
                    f"could not purge {workspace_id} ({exc}) — remove it by hand: "
                    f"POST /api/workspaces/{workspace_id}/purge")
        elif registered:
            _say(f"kept {workspace_id} (--keep); purge it when done")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
