#!/usr/bin/env python3
"""Portable browser E2E for Skill Lab binary task assets.

Runs against an already-started local frontend/backend, writes only the requested
screenshot/network evidence directory, and deletes the task set it creates.
It uses the repository's locked Playwright Python dependency; no browser install
or /opt mutation is performed.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import time
import zipfile
from pathlib import Path

from playwright.sync_api import Request, Response, sync_playwright


def xlsx_bytes() -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("xl/workbook.xml", "<workbook/>")
    return out.getvalue()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:5173")
    parser.add_argument("--evidence-dir", default="/tmp/skill-lab-assets-e2e")
    parser.add_argument("--browser", default=os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE"))
    args = parser.parse_args()
    evidence = Path(args.evidence_dir)
    evidence.mkdir(parents=True, exist_ok=True)
    fixtures = evidence / "fixtures"
    fixtures.mkdir(exist_ok=True)
    (fixtures / "source.xlsx").write_bytes(xlsx_bytes())
    (fixtures / "brief.pdf").write_bytes(b"%PDF-1.7\nportable-browser-e2e")
    (fixtures / "pixel.png").write_bytes(b"\x89PNG\r\n\x1a\nportable-browser-e2e")

    network: list[dict[str, object]] = []
    taskset_id: str | None = None
    with sync_playwright() as playwright:
        launch = {"headless": True}
        if args.browser:
            launch["executable_path"] = args.browser
        browser = playwright.chromium.launch(**launch)
        page = browser.new_page(viewport={"width": 1440, "height": 1000})

        def request_finished(request: Request) -> None:
            if "/api/skill-lab/" not in request.url:
                return
            response: Response | None = request.response()
            entry: dict[str, object] = {
                "method": request.method,
                "url": request.url,
                "status": response.status if response else None,
                "content_type": request.headers.get("content-type", ""),
            }
            if request.method in {"POST", "PUT"} and "tasksets" in request.url:
                entry["json"] = request.post_data_json
            network.append(entry)

        page.on("requestfinished", request_finished)
        try:
            page.goto(f"{args.base_url}/skill-lab?view=tasksets&ts=new", wait_until="networkidle")
            page.get_by_test_id("taskset-name").fill(f"portable-assets-{int(time.time())}")
            row = page.get_by_test_id("task-row-tasks-0")
            row.locator("textarea").nth(0).fill("Inspect every attached binary file.")
            row.locator("textarea").nth(1).fill("PASS when all files are present.")
            page.get_by_test_id("task-assets-tasks-0").set_input_files(
                [fixtures / "source.xlsx", fixtures / "brief.pdf", fixtures / "pixel.png"]
            )
            # Upload starts asynchronously; save must stay unavailable until the
            # staged descriptors have returned and entered draft state.
            assert page.get_by_test_id("taskset-save").is_disabled()
            destinations = row.get_by_label("Destination path")
            destinations.nth(2).wait_for()
            destinations.nth(0).fill(".git/source.xlsx")
            assert page.get_by_test_id("taskset-save").is_disabled()
            destinations.nth(0).fill("inputs/source.xlsx")
            destinations.nth(1).fill("docs/brief.pdf")
            destinations.nth(2).fill("images/pixel.png")
            with page.expect_response(
                lambda response: (
                    response.request.method == "POST"
                    and response.url.endswith("/api/skill-lab/tasksets")
                )
            ) as create_info:
                page.get_by_test_id("taskset-save").click()
            create_response = create_info.value
            assert create_response.status == 201, create_response.text()
            create_json = create_response.request.post_data_json
            files = create_json["tasks_by_split"]["tasks"][0]["files"]
            assert set(files) == {"inputs/source.xlsx", "docs/brief.pdf", "images/pixel.png"}
            assert all("staged_asset" in value for value in files.values())
            upload = next(item for item in network if str(item["url"]).endswith("/task-assets"))
            assert str(upload["content_type"]).startswith("multipart/form-data; boundary=")

            taskset_id = str(create_response.json()["id"])
            assert taskset_id.startswith("ts_"), create_response.text()
            page.wait_for_url(f"**?view=tasksets&ts={taskset_id}")
            page.get_by_test_id("taskset-edit").click()
            edit_row = page.get_by_test_id("task-row-tasks-0")
            edit_row.get_by_label("Destination path").nth(2).wait_for()
            edit_row.locator('[data-testid^="task-asset-remove-"]').last.click()
            with page.expect_response(
                lambda response: (
                    response.request.method == "PUT"
                    and response.url.endswith(f"/api/skill-lab/tasksets/{taskset_id}")
                )
            ) as update_info:
                page.get_by_test_id("taskset-save").click()
            update_response = update_info.value
            assert update_response.status == 200, update_response.text()
            update_files = update_response.request.post_data_json["tasks_by_split"]["tasks"][0][
                "files"
            ]
            assert set(update_files) == {"inputs/source.xlsx", "docs/brief.pdf"}
            assert all(
                "asset" in value and "staged_asset" not in value for value in update_files.values()
            )

            page.screenshot(path=evidence / "skill-lab-assets-e2e.png", full_page=True)
        finally:
            if taskset_id:
                page.request.delete(f"{args.base_url}/api/skill-lab/tasksets/{taskset_id}")
            (evidence / "skill-lab-assets-network.json").write_text(
                json.dumps(network, indent=2), encoding="utf-8"
            )
            browser.close()
    print(json.dumps({"ok": True, "evidence_dir": str(evidence)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
