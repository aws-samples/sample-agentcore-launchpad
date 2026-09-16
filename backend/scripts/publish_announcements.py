"""Explicitly publish the reviewed initial notices through an administrator session."""

import argparse
import json
from contextlib import closing
from pathlib import Path

from _e2e_client import e2e_client

from app.routers.announcements import AnnouncementContent

DEFAULT_FILE = Path(__file__).resolve().parents[2] / "config" / "announcements.initial.json"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--file", type=Path, default=DEFAULT_FILE)
    parser.add_argument("--apply", action="store_true", help="Publish; otherwise preview only.")
    args = parser.parse_args()
    contents = [
        AnnouncementContent.model_validate(item).model_dump()
        for item in json.loads(args.file.read_text())
    ]
    if len({item["title"] for item in contents}) != len(contents):
        raise SystemExit("Input notice titles must be unique.")
    with closing(e2e_client(args.base_url, timeout=30)) as client:
        existing = []
        while True:
            response = client.get(
                "/api/announcements/manage", params={"limit": 100, "offset": len(existing)},
            )
            response.raise_for_status()
            page = response.json()
            existing.extend(page["announcements"])
            if len(existing) >= page["total"]:
                break
        planned = []
        for content in contents:
            matches = [
                row for row in existing if content["title"] in (
                    row["content"]["title"], (row["published_content"] or {}).get("title"),
                )
            ]
            if len(matches) > 1:
                raise SystemExit(f"Ambiguous existing title: {content['title']}")
            row = matches[0] if matches else None
            if row and row["published_content"] == content:
                print(json.dumps({"action": "unchanged", "id": row["id"]}, ensure_ascii=False))
                continue
            if row and row["content"] != content:
                raise SystemExit(
                    f"Existing notice was edited; manage it in the console: {row['id']}"
                )
            planned.append((content, row))
        for content, row in planned:
            if not args.apply:
                print(json.dumps({"action": "would-publish", "title": content["title"]},
                                 ensure_ascii=False))
                continue
            if row is None:
                response = client.post("/api/announcements", json=content)
                response.raise_for_status()
                row = response.json()
            response = client.post(
                f"/api/announcements/{row['id']}/publish",
                json={"expected_revision": row["revision"]},
            )
            response.raise_for_status()
            published = response.json()
            print(json.dumps({
                "action": "published", "id": published["id"],
                "title": content["title"], "published_at": published["published_at"],
            }, ensure_ascii=False))


if __name__ == "__main__":
    main()
