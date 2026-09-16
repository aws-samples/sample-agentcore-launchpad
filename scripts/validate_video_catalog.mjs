#!/usr/bin/env node
// Build-time validation: catalog edits must not ship broken playback/navigation.
import { readFile } from "node:fs/promises";

const catalog = JSON.parse(await readFile(
  new URL("../frontend/src/config/videos.json", import.meta.url), "utf8",
));
function requireValue(condition, message) {
  if (!condition) throw new Error(`Video catalog: ${message}`);
}
function localized(value, field) {
  for (const language of ["en", "zh-CN"]) {
    requireValue(typeof value?.[language] === "string" && value[language].trim(), `${field}.${language} is required`);
  }
}
function mediaUrl(value, id) {
  const url = new URL(value);
  requireValue(url.protocol === "https:" && !url.username && !url.password && !url.search && !url.hash,
    `${id}: media must use permanent HTTPS URLs without credentials, queries, or fragments`);
  const path = decodeURIComponent(url.pathname);
  requireValue(new RegExp(`^/media/${id}/[a-zA-Z0-9_-]+/[^/]+$`).test(path),
    `${id}: expected /media/<id>/<revision>/<filename>`);
  return url;
}
requireValue(catalog.schemaVersion === 1 && Array.isArray(catalog.videos), "unsupported schema");
const ids = new Set();
for (const video of catalog.videos) {
  const { id } = video;
  requireValue(typeof id === "string" && /^[a-z0-9]+(?:-[a-z0-9]+)*$/.test(id) && !ids.has(id),
    "video IDs must be unique lowercase slugs");
  ids.add(id);
  localized(video.title, `${id}.title`);
  localized(video.description, `${id}.description`);
  requireValue(Number.isFinite(video.durationSeconds) && video.durationSeconds > 0, `${id}: invalid duration`);
  requireValue(typeof video.publishedAt === "string"
    && /^\d{4}-\d{2}-\d{2}$/.test(video.publishedAt)
    && Number.isFinite(Date.parse(video.publishedAt))
    && new Date(video.publishedAt).toISOString().slice(0, 10) === video.publishedAt,
  `${id}: invalid date`);
  requireValue(typeof video.language === "string" && video.language.length > 0, `${id}: missing language`);
  const poster = mediaUrl(video.posterUrl, id);
  requireValue(poster.pathname.endsWith(".jpg"), `${id}: poster must be JPEG`);
  requireValue(Array.isArray(video.sources) && video.sources.length > 0, `${id}: missing sources`);
  const types = new Set();
  for (const source of video.sources) {
    const url = mediaUrl(source.url, id);
    requireValue(["video/mp4", "video/webm"].includes(source.type) && !types.has(source.type),
      `${id}: sources must have distinct supported MIME types`);
    types.add(source.type);
    requireValue(url.origin === poster.origin && url.pathname.endsWith(`.${source.type.split("/")[1]}`),
      `${id}: inconsistent source origin or file extension`);
  }
  requireValue(Array.isArray(video.captions), `${id}: captions must be an array`);
  for (const caption of video.captions) {
    const url = mediaUrl(caption.url, id);
    requireValue(url.origin === poster.origin && url.pathname.endsWith(".vtt"), `${id}: captions must use CDN WebVTT`);
    requireValue(typeof caption.language === "string" && caption.language && typeof caption.label === "string" && caption.label,
      `${id}: missing caption language/label`);
  }
  requireValue(Array.isArray(video.chapters), `${id}: chapters must be an array`);
  let previous = -1;
  for (const chapter of video.chapters) {
    requireValue(Number.isFinite(chapter.startSeconds) && chapter.startSeconds >= 0
      && chapter.startSeconds > previous && chapter.startSeconds < video.durationSeconds,
    `${id}: chapter offsets must increase within the video duration`);
    localized(chapter.title, `${id}.chapter`);
    previous = chapter.startSeconds;
  }
}
console.log(`Video catalog: ${catalog.videos.length} entries validated`);
