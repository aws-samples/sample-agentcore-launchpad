"""Live verification of the Managed KB backend service chain (step 2 gate).

create KB (upload mode) → upload sample docs → wait DS AVAILABLE → sync →
wait ingestion COMPLETE → playground queries with content assertions.
Run: cd backend && PYTHONPATH=. python scripts/e2e_knowledge_base.py
"""

import sys
import time
from pathlib import Path

from app.services import knowledge
from app.services.workspace import default_workspace_context

KB_NAME = "aurora-deck-docs"
SAMPLES = Path(__file__).resolve().parents[2] / "samples" / "kb_docs"


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def main() -> int:
    # Hub workspace: this gate verifies the deployment's own KB chain.
    ws = default_workspace_context()
    existing = [kb for kb in knowledge.list_kbs(ws) if kb["name"] == KB_NAME]
    if existing:
        kb_id = existing[0]["kb_id"]
        log(f"reusing existing KB {kb_id}")
        detail = knowledge.get_kb_detail(ws, kb_id)
        if not detail["data_sources"]:
            log("no data source yet — replaying upload-mode source (client flow)")
            detail = knowledge.add_data_source(ws, kb_id, {"mode": "upload"})
    else:
        log("creating KB (upload mode)…")
        detail = knowledge.create_kb(
            ws,
            KB_NAME,
            "Aurora Deck product documentation and support runbook. Use for questions "
            "about Aurora Deck features, versions, pricing, refunds, and escalations.",
            {"mode": "upload"},
        )
        kb_id = detail["kb_id"]
        log(f"created KB {kb_id} status={detail['status']}")

    files = [(p.name, p.read_bytes()) for p in sorted(SAMPLES.glob("*.md"))]
    keys = knowledge.upload_files(ws, kb_id, files)
    log(f"uploaded {len(keys)} files: {keys}")

    ds_id = None
    deadline = time.time() + 480
    while time.time() < deadline:
        detail = knowledge.get_kb_detail(ws, kb_id)
        if not detail["data_sources"]:
            log(f"kb status={detail['status']} · waiting for data source setup")
            # Real-AWS data-source polling is intentionally paced and attempt-bounded.
            time.sleep(10)  # nosemgrep: arbitrary-sleep
            continue
        ds = detail["data_sources"][0]
        ds_id = ds["ds_id"]
        log(f"data source {ds_id} status={ds['status']}")
        if ds["status"] == "AVAILABLE":
            break
        if ds["status"] in ("DELETE_UNSUCCESSFUL", "FAILED"):
            log(f"FAIL: data source status {ds['status']} · {ds['failure_reasons']}")
            return 1
        # Real-AWS data-source polling is intentionally paced and attempt-bounded.
        time.sleep(15)  # nosemgrep: arbitrary-sleep
    else:
        log("FAIL: data source never became AVAILABLE")
        return 1

    log("starting ingestion…")
    job = knowledge.start_sync(ws, kb_id, ds_id)
    log(f"job {job['job_id']} status={job['status']}")

    deadline = time.time() + 900
    while time.time() < deadline:
        jobs = knowledge.list_ingestion_jobs(ws, kb_id, ds_id)
        top = jobs[0] if jobs else {}
        log(f"ingestion status={top.get('status')} stats={top.get('statistics')}")
        if top.get("status") == "COMPLETE":
            break
        if top.get("status") in ("FAILED", "STOPPED"):
            log(f"FAIL: ingestion {top.get('status')} · {top.get('failure_reasons')}")
            return 1
        # Real-AWS ingestion polling is intentionally paced and attempt-bounded.
        time.sleep(20)  # nosemgrep: arbitrary-sleep
    else:
        log("FAIL: ingestion did not complete in time")
        return 1

    checks = [
        ("What is the refund policy for the Pro plan?", "30 days"),
        ("What rendering pipeline does Aurora Deck use?", "WebGPU"),
    ]
    failures = 0
    for text, needle in checks:
        results = knowledge.query(ws, kb_id, text, number_of_results=4)
        hit = any(needle.lower() in r["text"].lower() for r in results)
        log(f"query {text!r} → {len(results)} results · top score "
            f"{results[0]['score'] if results else None} · contains {needle!r}: {hit}")
        for r in results[:2]:
            log(f"  · {r['score']:.3f} {r['location_uri']} :: {r['text'][:100]!r}")
        if not hit:
            failures += 1
    log("PASS" if failures == 0 else f"FAIL: {failures} query check(s) missed")
    print(f"KB_ID={kb_id}")
    return failures


if __name__ == "__main__":
    sys.exit(main())
