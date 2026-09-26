"""Exercise publication failure boundaries without credentials or AWS requests."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

AWS_STUB = r"""#!/usr/bin/env node
const fs = require("node:fs");
const path = require("node:path");
const { createHash } = require("node:crypto");
const args = process.argv.slice(2);
const option = (name) => args[args.indexOf(name) + 1];
const mode = process.env.VIDEO_TEST_MODE;
function fail(message) { console.error(message); process.exit(1); }
if (args[0] === "cloudformation") {
  console.log(JSON.stringify({ Stacks: [{ Outputs: [
    { OutputKey: "BucketName", OutputValue: "test-bucket" },
    { OutputKey: "BaseUrl", OutputValue: "https://test.cloudfront.net" },
  ] }] }));
} else if (args[1] === "head-object") {
  const filename = path.basename(option("--key"));
  const local = path.join(process.env.VIDEO_TEST_MEDIA, filename);
  if (mode === "mutated" && filename === "video.mp4") {
    fs.appendFileSync(local, "changed after hashing");
  }
  if (mode === "denied") fail("An error occurred (403) when calling HeadObject");
  if (["existing", "conflict", "metadata"].includes(mode)) {
    const body = fs.readFileSync(local);
    const type = filename.endsWith(".mp4") ? "video/mp4" : filename.endsWith(".webm")
      ? "video/webm" : filename.endsWith(".jpg") ? "image/jpeg" : "text/vtt; charset=utf-8";
    console.log(JSON.stringify({
      Metadata: { sha256: mode === "conflict" && filename === "poster.jpg"
        ? "different-content" : createHash("sha256").update(body).digest("hex") },
      ContentLength: body.length,
      ContentType: mode === "metadata" ? "application/octet-stream" : type,
      CacheControl: "public,max-age=31536000,immutable",
    }));
  } else fail("An error occurred (404) when calling HeadObject");
} else if (args[1] === "put-object") {
  const body = fs.readFileSync(option("--body"));
  if (args.includes("--checksum-sha256") &&
      option("--checksum-sha256") !== createHash("sha256").update(body).digest("base64")) {
    fail("BadDigest: body changed after validation");
  }
  if (option("--if-none-match") !== "*") fail("Missing conditional creation");
  fs.appendFileSync(process.env.VIDEO_TEST_LOG, JSON.stringify({
    key: option("--key"), body: body.toString(), type: option("--content-type"),
  }) + "\n");
  console.log("{}");
} else fail("Unexpected AWS operation");
"""


@pytest.fixture
def publication(tmp_path):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    for name in ("publish_videos.mjs", "validate_video_catalog.mjs"):
        shutil.copyfile(ROOT / "scripts" / name, scripts / name)
    catalog_path = tmp_path / "backend/app/data/videos.initial.json"
    catalog_path.parent.mkdir(parents=True)
    base = "https://test.cloudfront.net/media/tutorial/r1/"
    text = {"en": "Tutorial", "zh-CN": "教程"}
    catalog = {"schemaVersion": 1, "videos": [{
        "id": "tutorial", "title": text, "description": text,
        "publishedAt": "2026-09-16", "durationSeconds": 10, "language": "zh-CN",
        "posterUrl": base + "poster.jpg",
        "sources": [
            {"url": base + "video.mp4", "type": "video/mp4"},
            {"url": base + "video.webm", "type": "video/webm"},
        ],
        "captions": [{"url": base + "video.vtt", "language": "zh-CN", "label": "中文"}],
        "chapters": [{"startSeconds": 0, "title": text}],
    }]}
    catalog["categories"] = [{"id": "platform", "title": text}]
    catalog["collections"] = [{
        "id": "tutorial", "categoryId": "platform", "title": text,
        "description": text, "videoIds": ["tutorial"],
    }]
    catalog_path.write_text(json.dumps(catalog))
    media = tmp_path / "media"
    media.mkdir()
    for name in ("video.mp4", "video.webm", "poster.jpg"):
        (media / name).write_bytes(b"fixture bytes")
    (media / "video.srt").write_text("1\n00:00:00,000 --> 00:00:01,000\nCaption\n")
    executable = tmp_path / "aws"
    executable.write_text(AWS_STUB)
    executable.chmod(0o755)
    log = tmp_path / "uploads.jsonl"

    def publish(mode="missing", *, dry_run=False):
        command = [
            "node", str(scripts / "publish_videos.mjs"),
            "--video", "tutorial", "--source-dir", str(media),
        ]
        if dry_run:
            command.append("--dry-run")
        return subprocess.run(
            command, capture_output=True, text=True, timeout=30,
            env={
                **os.environ, "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}",
                "VIDEO_TEST_MODE": mode, "VIDEO_TEST_MEDIA": str(media),
                "VIDEO_TEST_LOG": str(log),
            },
        )

    return publish, media, log, catalog_path


@pytest.mark.parametrize("mode", ["conflict", "denied", "metadata", "mutated"])
def test_publication_refuses_unsafe_writes(publication, mode):
    publish, media, log, _ = publication
    (media / "video.vtt").write_text("WEBVTT\n\n")
    result = publish(mode)
    assert result.returncode != 0, result.stdout
    assert not log.exists(), "No object should be written on validation failure"


def test_existing_objects_are_skipped_and_dry_run_writes_nothing(publication):
    publish, media, log, _ = publication
    result = publish(dry_run=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.count("would-upload") == 4
    assert not (media / "video.vtt").exists()
    assert not log.exists()
    (media / "video.vtt").write_text("WEBVTT\n\n")
    result = publish("existing")
    assert result.returncode == 0, result.stderr
    assert result.stdout.count("unchanged") == 4
    assert not log.exists()


def test_missing_late_asset_prevents_partial_publication(publication):
    publish, media, log, _ = publication
    (media / "video.srt").unlink()
    result = publish()
    assert result.returncode != 0
    assert not log.exists()


def test_upload_converts_subtitles_and_creates_all_assets(publication):
    publish, _, log, _ = publication
    result = publish()
    assert result.returncode == 0, result.stderr
    uploads = [json.loads(line) for line in log.read_text().splitlines()]
    assert len(uploads) == 4
    captions = next(upload for upload in uploads if upload["key"].endswith(".vtt"))
    assert captions["type"] == "text/vtt; charset=utf-8"
    assert captions["body"].startswith("WEBVTT\n\n")
    assert "00:00:00.000 --> 00:00:01.000" in captions["body"]


def test_catalog_rejects_nonexistent_calendar_date(publication):
    publish, _, log, catalog_path = publication
    catalog = json.loads(catalog_path.read_text())
    catalog["videos"][0]["publishedAt"] = "2026-02-30"
    catalog_path.write_text(json.dumps(catalog))
    result = publish(dry_run=True)
    assert result.returncode != 0
    assert "invalid date" in result.stderr
    assert not log.exists()


@pytest.mark.parametrize(("mutation", "message"), [
    (lambda c: c["collections"][0].update(categoryId="missing"), "unknown category"),
    (lambda c: c["collections"][0].update(videoIds=["missing"]), "unknown video"),
    (lambda c: c["collections"][0]["videoIds"].append("tutorial"), "exactly one collection"),
    (lambda c: c["collections"].clear(), "every video must belong"),
    (lambda c: c["collections"][0].update(videoIds=[]), "collection must contain videos"),
    (lambda c: c["collections"][0].update(title={"en": "Title"}), "title.zh-CN is required"),
    (lambda c: c["categories"].append(c["categories"][0]), "category: IDs must be unique"),
    (lambda c: c["collections"].append(c["collections"][0]), "collection: IDs must be unique"),
])
def test_catalog_rejects_broken_library_before_publication(publication, mutation, message):
    publish, _, log, catalog_path = publication
    catalog = json.loads(catalog_path.read_text())
    mutation(catalog)
    catalog_path.write_text(json.dumps(catalog))
    result = publish(dry_run=True)
    assert result.returncode != 0
    assert message in result.stderr
    assert not log.exists()


def test_catalog_accepts_an_empty_library(publication):
    _, _, log, catalog_path = publication
    catalog = json.loads(catalog_path.read_text())
    catalog.update(videos=[], collections=[])
    catalog_path.write_text(json.dumps(catalog))
    result = subprocess.run(
        ["node", str(catalog_path.parents[3] / "scripts/validate_video_catalog.mjs")],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert not log.exists()
