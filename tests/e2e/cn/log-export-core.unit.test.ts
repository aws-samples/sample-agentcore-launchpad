// Hermetic unit test of the runtime-log export helpers (N12-obs61): `npm run unit` → node:test, no Playwright, no AWS.
// eslint-disable-next-line @typescript-eslint/no-require-imports
const { test } = require("node:test") as typeof import("node:test");
// eslint-disable-next-line @typescript-eslint/no-require-imports
const assert: typeof import("node:assert/strict") = require("node:assert/strict");
// eslint-disable-next-line @typescript-eslint/no-require-imports
const core = require("./log-export-core.ts") as typeof import("./log-export-core");

test("redactLine masks Launchpad keys, bearer tokens, AWS keys, provider keys and secret-shaped fields", () => {
  const line = 'auth Bearer eyJabc.def.ghi key lp_ABCDEF0123456789XYZ aws AKIAABCDEFGHIJKLMNOP "aws_secret_access_key": "wJalrXUtnFEMI/K7MDENG" password=hunter2 x-api-key: sk-abcd1234567890abcdef';
  const red = core.redactLine(line);
  for (const leak of ["eyJabc", "0123456789XYZ", "ABCDEFGHIJKLMNOP", "wJalrXUtnFEMI", "hunter2", "1234567890abcdef"]) assert.ok(!red.includes(leak), `${leak} leaked: ${red}`);
  assert.ok(red.includes("lp_ABCDEF…") && red.includes("Bearer …") && red.includes("AKIA…") && red.includes("x-api-key: …"), red);
  assert.equal(core.redactLine("provider answered with sk-abcd1234567890abcdef in the body"), "provider answered with sk-abcd… in the body");
  assert.equal(core.redactLine("plain runtime line with no secrets, status 200"), "plain runtime line with no secrets, status 200");
});

test("S3 key layout: round-<n>/<run-id>/runtime-logs/<group>.jsonl", () => {
  assert.equal(core.roundFromEvidenceRound("round-11/storyline-2/all"), "round-11");
  assert.equal(core.roundFromEvidenceRound(undefined), "round-unknown");
  assert.equal(core.logGroupObjectName("/aws/bedrock-agentcore/runtimes/e2es_hr_runtime_x-AbC123-treat76caca"), "aws__bedrock-agentcore__runtimes__e2es_hr_runtime_x-AbC123-treat76caca.jsonl");
  assert.equal(core.logGroupS3Key("round-11", "10160845", "/aws/bedrock-agentcore/runtimes/r-DEFAULT"), "round-11/10160845/runtime-logs/aws__bedrock-agentcore__runtimes__r-DEFAULT.jsonl");
  assert.equal(core.logGroupS3Key("round-11", "run id/with spaces", "/g"), "round-11/run_id_with_spaces/runtime-logs/g.jsonl");
});

test("formatEvent bounds the export and the truncation note names the group", () => {
  const ev = { timestamp: 1792116767628, logStreamName: "s1", message: "token=abc123secret ok" };
  const f = core.formatEvent(ev, 0, 10_000);
  assert.ok(f && f.line.endsWith("\n") && f.bytes === Buffer.byteLength(f.line));
  const parsed = JSON.parse(f!.line) as { ts: string; stream: string; message: string };
  assert.equal(parsed.stream, "s1"); assert.equal(parsed.ts, "2026-10-16T02:12:47.628Z"); assert.ok(!parsed.message.includes("abc123secret"));
  assert.equal(core.formatEvent(ev, 9_990, 10_000), undefined);
  const note = JSON.parse(core.truncationNote("/g", 42, 20 * 1024 * 1024)) as { message: string; stream: string };
  assert.ok(note.message.includes("TRUNCATED: /g") && note.message.includes("20 MB") && note.message.includes("42 events") && note.stream === "_export");
});
