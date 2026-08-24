#!/usr/bin/env python3
"""Real-AWS Skill Lab binary input smoke (excluded from make verify).

Requires a bootstrapped default workspace, AWS credentials, a Registry skill record id,
and the Skill Lab worker Runtime. It uploads PDF/PNG/XLSX inputs, creates a task set,
submits evaluation, then verifies the Runtime rollout retained byte-identifying markers.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import time
import zipfile
from contextlib import suppress

import httpx
from _e2e_client import e2e_client


def xlsx_bytes() -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("xl/workbook.xml", "<workbook/>")
        archive.writestr("xl/sharedStrings.xml", "LAUNCHPAD_ASSET_XLSX")
    return out.getvalue()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--record-id", default=os.environ.get("SKILL_LAB_E2E_RECORD_ID"))
    parser.add_argument("--timeout", type=int, default=1800)
    args = parser.parse_args()
    if not args.record_id:
        parser.error("--record-id or SKILL_LAB_E2E_RECORD_ID is required")
    client = e2e_client(args.base_url, timeout=60)
    status_response = client.get("/api/skill-lab/status")
    status_response.raise_for_status()
    status = status_response.json()
    if not status.get("provisioned") or not status.get("venv_ready"):
        raise SystemExit(f"Skill Lab is not bootstrapped: {status}")
    records = client.get("/api/registry/records", params={"type": "AGENT_SKILLS"})
    records.raise_for_status()
    if args.record_id not in {
        record.get("record_id") for record in records.json().get("records", [])
    }:
        raise SystemExit(f"Registry AGENT_SKILLS record not found: {args.record_id}")
    fixtures = {
        "source.xlsx": xlsx_bytes(),
        "brief.pdf": b"%PDF-1.7\nLAUNCHPAD_ASSET_PDF",
        "pixel.png": b"\x89PNG\r\n\x1a\nLAUNCHPAD_ASSET_PNG",
    }
    uploaded = client.post(
        "/api/skill-lab/task-assets",
        files=[
            ("files", (name, data, "application/octet-stream")) for name, data in fixtures.items()
        ],
    )
    uploaded.raise_for_status()
    descriptors = uploaded.json()["assets"]
    task = {
        "id": "binary_inputs",
        "question": "Inspect every file under data and report each LAUNCHPAD_ASSET marker.",
        "rubric": "PASS only when XLSX, PDF, and PNG markers are all reported.",
        "files": {f"data/{asset['name']}": asset for asset in descriptors},
    }
    taskset_id = None
    job_id = None
    try:
        created = client.post(
            "/api/skill-lab/tasksets",
            json={
                "name": f"asset-e2e-{int(time.time())}",
                "mode": "single",
                "tasks_by_split": {"tasks": [task]},
            },
        )
        created.raise_for_status()
        taskset_id = created.json()["id"]
        job = client.post(
            "/api/skill-lab/jobs",
            json={
                "type": "eval",
                "taskset_id": taskset_id,
                "skill_source": {"kind": "registry", "record_id": args.record_id},
                "params": {"workers": 1, "judge_mode": "chat"},
            },
        )
        job.raise_for_status()
        job_id = job.json()["id"]
        deadline = time.time() + args.timeout
        while time.time() < deadline:
            current_response = client.get(f"/api/skill-lab/jobs/{job_id}")
            current_response.raise_for_status()
            current = current_response.json()
            if current["status"] in {"succeeded", "failed", "cancelled", "interrupted"}:
                if current["status"] != "succeeded":
                    raise SystemExit(json.dumps(current, indent=2))
                results_response = client.get(f"/api/skill-lab/jobs/{job_id}/results")
                results_response.raise_for_status()
                text = json.dumps(results_response.json())
                missing = [
                    marker
                    for marker in (
                        "LAUNCHPAD_ASSET_XLSX",
                        "LAUNCHPAD_ASSET_PDF",
                        "LAUNCHPAD_ASSET_PNG",
                    )
                    if marker not in text
                ]
                if missing:
                    raise SystemExit(f"Runtime result did not observe markers: {missing}")
                print(
                    json.dumps({"taskset_id": taskset_id, "job_id": job_id, "ok": True}, indent=2)
                )
                return 0
            time.sleep(5)
        raise SystemExit(f"timed out waiting for {job_id}")
    finally:
        # Jobs retain immutable snapshots and therefore block task-set deletion.
        if job_id:
            with suppress(httpx.HTTPError):
                client.delete(f"/api/skill-lab/jobs/{job_id}").raise_for_status()
        if taskset_id:
            with suppress(httpx.HTTPError):
                client.delete(f"/api/skill-lab/tasksets/{taskset_id}").raise_for_status()
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
