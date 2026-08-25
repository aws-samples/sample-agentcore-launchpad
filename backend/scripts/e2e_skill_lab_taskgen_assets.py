#!/usr/bin/env python3
"""Real-AWS smoke for taskgen input attachments (excluded from make verify).

Answers the one question no hermetic test can: given the prompt this feature
writes, does a real model actually OPEN the attached document and ground its
questions in what is inside it? Presence of the bytes is not evidence of that,
so the assertions look for facts that appear only inside the workbook.

Requires a bootstrapped default workspace, AWS credentials, the Skill Lab worker
Runtime, and a Registry AGENT_SKILLS record id. Cleans up the job and any task
set it creates.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import tarfile
import time
import zipfile
from contextlib import suppress

import boto3
import httpx
from _e2e_client import e2e_client

# Facts that exist ONLY inside the workbook. A question mentioning any of them
# could not have been written without reading the file.
SHEET_NAME = "Q2Regions"
SECRET_COLUMN = "gross_margin_pct"
SECRET_REGION = "Zanzibar"


def xlsx_bytes() -> bytes:
    """A minimal but real xlsx: shared strings hold the facts we look for."""
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        archive.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/'
            'package/2006/content-types"/>',
        )
        archive.writestr(
            "xl/workbook.xml",
            '<?xml version="1.0"?><workbook><sheets>'
            f'<sheet name="{SHEET_NAME}" sheetId="1" r:id="rId1"/>'
            "</sheets></workbook>",
        )
        archive.writestr(
            "xl/sharedStrings.xml",
            '<?xml version="1.0"?><sst>'
            f"<si><t>region</t></si><si><t>revenue</t></si><si><t>{SECRET_COLUMN}</t></si>"
            f"<si><t>{SECRET_REGION}</t></si><si><t>APAC</t></si>"
            "</sst>",
        )
    return out.getvalue()


def _wait(client, job_id: str, timeout: int) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        response = client.get(f"/api/skill-lab/jobs/{job_id}")
        response.raise_for_status()
        job = response.json()
        if job["status"] in {"succeeded", "failed", "cancelled", "interrupted"}:
            return job
        time.sleep(5)
    raise SystemExit(f"timed out waiting for {job_id}")


def _exec_job_inputs(bucket: str, region: str, since: float) -> dict[str, bytes]:
    """Newest exec-job input tar written after `since`, as {member: bytes}.

    Keys are date-prefixed, so list_objects_v2 pages lexicographically — page all
    the way through rather than trusting the first page.
    """
    s3 = boto3.client("s3", region_name=region)
    newest: tuple[float, str] | None = None
    cursor: str | None = None
    while True:
        kwargs = {"Bucket": bucket, "Prefix": "skill-lab/exec-jobs/"}
        if cursor:
            kwargs["ContinuationToken"] = cursor
        page = s3.list_objects_v2(**kwargs)
        for item in page.get("Contents", []):
            if not str(item["Key"]).endswith("/in.tar.gz"):
                continue
            stamp = item["LastModified"].timestamp()
            if stamp >= since and (newest is None or stamp > newest[0]):
                newest = (stamp, str(item["Key"]))
        if not page.get("IsTruncated"):
            break
        cursor = page.get("NextContinuationToken")
    if newest is None:
        raise SystemExit("no exec-job in.tar.gz written during this run")
    raw = s3.get_object(Bucket=bucket, Key=newest[1])["Body"].read()
    with tarfile.open(fileobj=io.BytesIO(raw)) as tar:
        return {
            member.name: tar.extractfile(member).read()  # type: ignore[union-attr]
            for member in tar.getmembers()
            if member.isfile()
        }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--record-id", default=os.environ.get("SKILL_LAB_E2E_RECORD_ID"))
    parser.add_argument("--timeout", type=int, default=1800)
    parser.add_argument("--count", type=int, default=2)
    parser.add_argument("--region", default="us-west-2")
    parser.add_argument("--bucket", default=os.environ.get("SKILL_LAB_E2E_BUCKET"))
    args = parser.parse_args()
    if not args.record_id:
        parser.error("--record-id or SKILL_LAB_E2E_RECORD_ID is required")
    client = e2e_client(args.base_url, timeout=60)

    status = client.get("/api/skill-lab/status")
    status.raise_for_status()
    if not status.json().get("provisioned") or not status.json().get("venv_ready"):
        raise SystemExit(f"Skill Lab is not bootstrapped: {status.json()}")

    workbook = xlsx_bytes()
    staged = client.post(
        "/api/skill-lab/task-assets",
        files=[("files", ("regions.xlsx", workbook, "application/octet-stream"))],
    )
    staged.raise_for_status()
    staged_asset = staged.json()["assets"][0]["staged_asset"]

    started = time.time()
    job_id = None
    taskset_id = None
    try:
        created = client.post(
            "/api/skill-lab/jobs",
            json={
                "type": "taskgen",
                "skill_source": {"kind": "registry", "record_id": args.record_id},
                "attachments": [{"staged_asset": staged_asset}],
                "params": {"count": args.count},
            },
        )
        created.raise_for_status()
        job_id = created.json()["id"]
        job = _wait(client, job_id, args.timeout)
        if job["status"] != "succeeded":
            raise SystemExit(json.dumps(job, indent=2))

        results = client.get(f"/api/skill-lab/jobs/{job_id}/results")
        results.raise_for_status()
        payload = results.json()
        tasks = payload["tasks"]

        # 1. The bytes reached the worker at the contract path.
        if args.bucket:
            members = _exec_job_inputs(args.bucket, args.region, started)
            match = [name for name in members if name.endswith("work/data/regions.xlsx")]
            if not match:
                raise SystemExit(f"attachment absent from the exec input tar: {sorted(members)}")
            if members[match[0]] != workbook:
                raise SystemExit("attachment bytes differ inside the exec input tar")

        # 2. The model read it: at least one question cites something that exists
        #    only inside the workbook.
        text = json.dumps(tasks, ensure_ascii=False)
        cited = [
            fact for fact in (SHEET_NAME, SECRET_COLUMN, SECRET_REGION) if fact in text
        ]
        if not cited:
            raise SystemExit(
                "no generated task cites workbook-only content — the model may not have "
                f"opened the file. tasks: {text[:1200]}"
            )

        # 3. At least one task declared the document, and import binds it.
        declared = [task for task in tasks if task.get("attachments")]
        if not declared:
            raise SystemExit(f"no task declared the attachment: {text[:1200]}")

        imported = client.post(
            f"/api/skill-lab/jobs/{job_id}/import-taskset",
            json={"name": f"taskgen-assets-e2e-{int(time.time())}"},
        )
        imported.raise_for_status()
        taskset_id = imported.json()["taskset"]["id"]
        detail = client.get(f"/api/skill-lab/tasksets/{taskset_id}?full=true")
        detail.raise_for_status()
        bound = [
            value
            for task in detail.json()["tasks_by_split"]["tasks"]
            for value in (task.get("files") or {}).values()
            if isinstance(value, dict)
        ]
        if not bound:
            raise SystemExit("imported task set carries no asset descriptor")
        if any(row["name"] != "regions.xlsx" or row["size"] != len(workbook) for row in bound):
            raise SystemExit(f"unexpected descriptor metadata: {bound}")

        print(
            json.dumps(
                {
                    "ok": True,
                    "job_id": job_id,
                    "taskset_id": taskset_id,
                    "cited_workbook_facts": cited,
                    "declaring_tasks": [task["id"] for task in declared],
                    # Full per-task evidence: which field cites the workbook-only
                    # facts matters, and a task that references a file it neither
                    # declared nor inlined would be broken.
                    "tasks": [
                        {
                            "id": task.get("id"),
                            "attachments": task.get("attachments") or [],
                            "inline_files": sorted((task.get("files") or {}).keys()),
                            "cites_in_question": [
                                fact
                                for fact in (SHEET_NAME, SECRET_COLUMN, SECRET_REGION)
                                if fact in str(task.get("question", ""))
                            ],
                            "cites_in_rubric": [
                                fact
                                for fact in (SHEET_NAME, SECRET_COLUMN, SECRET_REGION)
                                if fact in str(task.get("rubric", ""))
                            ],
                            "question": task.get("question"),
                            "rubric": task.get("rubric"),
                        }
                        for task in tasks
                    ],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    finally:
        if taskset_id:
            with suppress(httpx.HTTPError):
                client.delete(f"/api/skill-lab/tasksets/{taskset_id}").raise_for_status()
        if job_id:
            with suppress(httpx.HTTPError):
                client.delete(f"/api/skill-lab/jobs/{job_id}").raise_for_status()
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
