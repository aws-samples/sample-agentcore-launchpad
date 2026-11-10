/**
 * Pure helpers of the runtime-log export (N12-obs61): S3 key layout, redaction, truncation note.
 * Dependency-free so `cn/log-export-core.unit.test.ts` (node:test) can exercise them hermetically.
 */
export const EVIDENCE_BUCKET_DEFAULT = "launchpad-cn-evidence-123456789012";
export const RUNTIME_LOG_GROUP_PREFIX = "/aws/bedrock-agentcore/runtimes/";

/** Secret-shaped fragments that must never land in an exported log line (superset of `scn.mask`). */
export const SECRET_PATTERNS: Array<[RegExp, string]> = [
  [/(lp_[A-Za-z0-9]{6})[A-Za-z0-9_-]{8,}/g, "$1…"],                                   // Launchpad API keys
  [/(Bearer\s+)[^\s"']+/gi, "$1…"],                                                     // bearer tokens
  [/\b(AKIA|ASIA)[0-9A-Z]{16}\b/g, "$1…"],                                              // AWS access key ids
  [/("?(?:aws_secret_access_key|aws_session_token|x-amz-security-token)"?\s*[:=]\s*)["']?[^\s"',}]+/gi, "$1…"],
  [/("?(?:password|passwd|secret|api[_-]?key|apikey|x-api-key|authorization|token|client_secret)"?\s*[:=]\s*)["']?[^\s"',}]+/gi, "$1…"],
  [/\b(sk-[A-Za-z0-9]{4})[A-Za-z0-9_-]{12,}/g, "$1…"],                                  // OpenAI-compatible provider keys
];

export function redactLine(line: string): string {
  let out = line;
  for (const [rx, rep] of SECRET_PATTERNS) out = out.replace(rx, rep);
  return out;
}

/** `round-11/storyline-2/all` → `round-11`; anything without a `round-<n>` head → `round-unknown`. */
export function roundFromEvidenceRound(evidenceRound: string | undefined): string {
  const m = /^(round-[A-Za-z0-9]+)/.exec(evidenceRound ?? "");
  return m ? m[1] : "round-unknown";
}

/** Log-group name → safe object name: the leading slash dropped, every other `/` → `__`. */
export function logGroupObjectName(group: string): string {
  return `${group.replace(/^\/+/, "").replace(/\//g, "__")}.jsonl`;
}

export function logGroupS3Key(round: string, runId: string, group: string): string {
  const safeRun = runId.replace(/[^A-Za-z0-9_.-]/g, "_") || "run";
  return `${round}/${safeRun}/runtime-logs/${logGroupObjectName(group)}`;
}

export interface ExportLine { ts: string; stream: string; message: string }

/** One JSONL line per event (redacted); `undefined` when adding it would exceed `maxBytes`. */
export function formatEvent(ev: { timestamp?: number; logStreamName?: string; message?: string }, bytesSoFar: number, maxBytes: number): { line: string; bytes: number } | undefined {
  const line = `${JSON.stringify({ ts: new Date(Number(ev.timestamp ?? 0)).toISOString(), stream: String(ev.logStreamName ?? ""), message: redactLine(String(ev.message ?? "")) } satisfies ExportLine)}\n`;
  const bytes = Buffer.byteLength(line, "utf8");
  return bytesSoFar + bytes > maxBytes ? undefined : { line, bytes };
}

export function truncationNote(group: string, kept: number, maxBytes: number): string {
  return `${JSON.stringify({ ts: new Date().toISOString(), stream: "_export", message: `TRUNCATED: ${group} exceeded ${Math.round(maxBytes / 1024 / 1024)} MB after ${kept} events — older/later events not exported` })}\n`;
}
