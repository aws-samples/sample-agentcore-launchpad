#!/usr/bin/env node
// Publish one catalog entry through AWS CLI, using the caller's normal credentials.
// Run with --dry-run first. Existing immutable keys may never change content.
import { createHash } from "node:crypto";
import { readFile, writeFile } from "node:fs/promises";
import { basename, resolve } from "node:path";
import { execFileSync } from "node:child_process";
import { parseArgs } from "node:util";
import { fileURLToPath } from "node:url";

const root = fileURLToPath(new URL("../", import.meta.url));
const cacheControl = "public,max-age=31536000,immutable";
const { values } = parseArgs({
  options: {
    video: { type: "string" },
    "source-dir": { type: "string" },
    region: { type: "string", default: "us-west-2" },
    stack: { type: "string", default: "launchpad-videos" },
    "dry-run": { type: "boolean", default: false },
  },
});
if (!values.video || !values["source-dir"]) {
  throw new Error("Usage: node scripts/publish_videos.mjs --video <id> --source-dir <dir> [--dry-run]");
}
await import("./validate_video_catalog.mjs");
const catalog = JSON.parse(await readFile(resolve(root, "frontend/src/config/videos.json"), "utf8"));
const video = catalog.videos.find((item) => item.id === values.video);
if (!video) throw new Error(`Video not in catalog: ${values.video}`);
const aws = (...args) => execFileSync("aws", [...args, "--region", values.region, "--output", "json"], {
  encoding: "utf8", stdio: ["ignore", "pipe", "pipe"],
});
const stack = JSON.parse(aws("cloudformation", "describe-stacks", "--stack-name", values.stack));
const outputs = Object.fromEntries(stack.Stacks[0].Outputs.map((o) => [o.OutputKey, o.OutputValue]));
if (!outputs.BucketName || !outputs.BaseUrl) throw new Error("Stack must export BucketName and BaseUrl");
const files = [
  ...video.sources.map((s) => ({ url: s.url, type: s.type })),
  { url: video.posterUrl, type: "image/jpeg" },
  ...video.captions.map((c) => ({ url: c.url, type: "text/vtt; charset=utf-8" })),
];
// Prepare and validate the complete upload set before writing any object.
const uploads = [];
for (const file of files) {
  const url = new URL(file.url);
  if (url.origin !== outputs.BaseUrl || url.search || url.hash) {
    throw new Error(`Catalog URL must use this stack's unadorned CDN URL: ${file.url}`);
  }
  const key = decodeURIComponent(url.pathname.slice(1));
  if (!new RegExp(`^media/${video.id.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}/[a-zA-Z0-9_-]+/[^/]+$`).test(key)) {
    throw new Error(`Expected immutable media/<video-id>/<revision>/<filename>: ${key}`);
  }
  const path = resolve(values["source-dir"], basename(key));
  let body;
  try {
    body = await readFile(path);
  } catch (error) {
    if (error.code !== "ENOENT" || !path.endsWith(".vtt")) throw error;
    const srt = await readFile(path.replace(/\.vtt$/, ".srt"), "utf8");
    body = Buffer.from(`WEBVTT\n\n${srt.replace(/^\uFEFF/, "").replace(/\r\n/g, "\n").replace(/(\d\d:\d\d:\d\d),(\d{3})/g, "$1.$2")}`);
    if (!values["dry-run"]) await writeFile(path, body);
  }
  const sha256 = createHash("sha256").update(body).digest("hex");
  let exists = false;
  try {
    const current = JSON.parse(aws("s3api", "head-object", "--bucket", outputs.BucketName, "--key", key));
    if (current.Metadata?.sha256 !== sha256 || current.ContentLength !== body.length) {
      throw new Error(`Refusing to replace immutable content: ${key}. Use a new revision path.`);
    }
    if (current.ContentType !== file.type || current.CacheControl !== cacheControl) {
      throw new Error(`Immutable object has incompatible Content-Type or Cache-Control: ${key}. Use a new revision path.`);
    }
    exists = true;
  } catch (error) {
    if (!/\(404\)|\(NoSuchKey\)|\(NotFound\)/.test(String(error.stderr ?? ""))) throw error;
  }
  uploads.push({ ...file, key, path, sha256, bytes: body.length, exists });
}
for (const upload of uploads) {
  if (!values["dry-run"] && !upload.exists) {
    aws("s3api", "put-object", "--bucket", outputs.BucketName, "--key", upload.key,
      "--body", upload.path, "--content-type", upload.type,
      "--cache-control", cacheControl,
      // S3 validates the bytes AWS CLI actually reads, even if the local file
      // changed after preflight. A mismatch cannot become an immutable object.
      "--checksum-sha256", Buffer.from(upload.sha256, "hex").toString("base64"),
      "--metadata", JSON.stringify({ sha256: upload.sha256 }), "--if-none-match", "*");
  }
  console.log(JSON.stringify({
    action: upload.exists ? "unchanged" : values["dry-run"] ? "would-upload" : "uploaded",
    url: upload.url, bytes: upload.bytes, sha256: upload.sha256,
  }));
}
