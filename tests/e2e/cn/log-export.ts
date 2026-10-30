import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { ledger } from "./helpers";
import { EVIDENCE_BUCKET_DEFAULT, formatEvent, logGroupS3Key, roundFromEvidenceRound, RUNTIME_LOG_GROUP_PREFIX, truncationNote } from "./log-export-core";

/**
 * N12-obs61 — export the runtime log groups of the storyline agents (incl. the canary `-stable<hash>` / `-treat<hash>`
 * endpoint groups) to the private evidence bucket BEFORE the product's teardown deletes them, so a failed run can still be
 * investigated. Reads CloudWatch (`filter-log-events`, paginated) and writes redacted JSON lines to
 * `s3://<bucket>/round-<n>/<run-id>/runtime-logs/<log-group>.jsonl` with the `cn-northwest-1` profile — the ONLY write the
 * suite makes to AWS outside the product API (plan §7 carve-out; the bucket is private, encrypted, TLS-only).
 *
 *   SCENARIO_LOG_EXPORT=0           skip (default: on when the scenario runs)
 *   SCENARIO_LOG_EXPORT_HOURS=6     window of events to export (default 6 h)
 *   SCENARIO_LOG_EXPORT_MAX_MB=20   cap per log group; a TRUNCATED marker line is appended when hit
 *   SCENARIO_EVIDENCE_BUCKET=…      bucket override (default launchpad-cn-evidence-123456789012)
 */
export interface ExportedGroup { group: string; key: string; events: number; bytes: number; truncated: boolean; status: "uploaded" | "empty" | "error"; error?: string }

function aws(args: string, timeoutMs = 180_000): { ok: boolean; out: string } {
  // eslint-disable-next-line @typescript-eslint/no-require-imports
  const { execSync } = require("node:child_process") as typeof import("node:child_process");
  try {
    return { ok: true, out: execSync(`aws ${args} --profile cn-northwest-1 --region cn-northwest-1`, { encoding: "utf8", stdio: ["ignore", "pipe", "pipe"], timeout: timeoutMs, maxBuffer: 64 * 1024 * 1024 }) };
  } catch (err) {
    return { ok: false, out: `ERROR ${(err as { stderr?: string }).stderr ?? String(err)}`.slice(0, 500) };
  }
}

export function logExportEnabled(): boolean {
  return !/^(0|false|no|off)$/i.test(String(process.env.SCENARIO_LOG_EXPORT ?? "1"));
}

export function listRuntimeLogGroups(runtimeId: string): string[] {
  if (!runtimeId) return [];
  const r = aws(`logs describe-log-groups --log-group-name-prefix ${RUNTIME_LOG_GROUP_PREFIX}${runtimeId}- --query "logGroups[].logGroupName" --output json`);
  if (!r.ok) { ledger(`log-export list ${runtimeId}: ${r.out}`); return []; }
  try { return (JSON.parse(r.out) as string[]).filter((g) => typeof g === "string"); } catch { return []; }
}

/** Export one group: paginate filter-log-events since `sinceMs`, redact, bound, upload. */
export function exportLogGroup(group: string, runId: string, sinceMs: number, maxBytes: number, bucket: string): ExportedGroup {
  const round = roundFromEvidenceRound(process.env.N10_EVIDENCE_ROUND);
  const key = logGroupS3Key(round, runId, group);
  const tmp = path.join(fs.mkdtempSync(path.join(os.tmpdir(), "lp-logexport-")), "events.jsonl");
  const fd = fs.openSync(tmp, "w");
  let events = 0, bytes = 0, truncated = false, nextToken: string | undefined;
  try {
    for (let page = 0; page < 200 && !truncated; page++) {
      const r = aws(`logs filter-log-events --log-group-name "${group}" --start-time ${sinceMs} --limit 10000 --output json${nextToken ? ` --next-token "${nextToken}"` : ""}`);
      if (!r.ok) { fs.closeSync(fd); return { group, key, events, bytes, truncated, status: "error", error: r.out }; }
      const body = JSON.parse(r.out) as { events?: Array<{ timestamp?: number; logStreamName?: string; message?: string }>; nextToken?: string };
      for (const ev of body.events ?? []) {
        const f = formatEvent(ev, bytes, maxBytes);
        if (!f) { truncated = true; fs.writeSync(fd, truncationNote(group, events, maxBytes)); break; }
        fs.writeSync(fd, f.line); events += 1; bytes += f.bytes;
      }
      if (!body.nextToken || body.nextToken === nextToken) break;
      nextToken = body.nextToken;
    }
  } finally {
    fs.closeSync(fd);
  }
  if (events === 0 && !truncated) { fs.rmSync(path.dirname(tmp), { recursive: true, force: true }); return { group, key, events, bytes, truncated, status: "empty" }; }
  const up = aws(`s3 cp "${tmp}" "s3://${bucket}/${key}" --only-show-errors`);
  fs.rmSync(path.dirname(tmp), { recursive: true, force: true });
  return up.ok ? { group, key, events, bytes, truncated, status: "uploaded" } : { group, key, events, bytes, truncated, status: "error", error: up.out };
}

/** Export every group of the given runtimes; one ledger line per group plus a summary; never throws (evidence only). */
export function exportRuntimeLogs(runtimeIds: string[], runId: string, label: string): ExportedGroup[] {
  if (!logExportEnabled()) { ledger(`${label} runtime-log export skipped (SCENARIO_LOG_EXPORT=${process.env.SCENARIO_LOG_EXPORT})`); return []; }
  const hours = Number(process.env.SCENARIO_LOG_EXPORT_HOURS ?? 6);
  const maxBytes = Math.max(1, Number(process.env.SCENARIO_LOG_EXPORT_MAX_MB ?? 20)) * 1024 * 1024;
  const bucket = process.env.SCENARIO_EVIDENCE_BUCKET ?? EVIDENCE_BUCKET_DEFAULT;
  const sinceMs = Date.now() - hours * 3600 * 1000;
  const out: ExportedGroup[] = [];
  for (const rid of Array.from(new Set(runtimeIds.filter(Boolean)))) {
    for (const group of listRuntimeLogGroups(rid)) {
      const g = exportLogGroup(group, runId, sinceMs, maxBytes, bucket);
      out.push(g);
      ledger(`${label} runtime-log export ${group} → ${g.status} events=${g.events} bytes=${g.bytes}${g.truncated ? " TRUNCATED" : ""} s3://${bucket}/${g.key}${g.error ? ` ${g.error.slice(0, 160)}` : ""}`);
    }
  }
  ledger(`${label} runtime-log export done: ${out.filter((g) => g.status === "uploaded").length} uploaded, ${out.filter((g) => g.status === "empty").length} empty, ${out.filter((g) => g.status === "error").length} errors (window ${hours} h, cap ${maxBytes / 1024 / 1024} MB/group, bucket ${bucket})`);
  return out;
}
