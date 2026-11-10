#!/usr/bin/env node
// Turns a crawl JSON (crawl.mjs output) into the human matrix
// docs/cn/reports/control-crawl-<date>.md (page × role → status counts) and copies the
// machine result next to the inventory (frontend/src/v2/__inventory__/crawl-<stamp>.json).
//
//   node tests/e2e/cn/controls/report.mjs <crawl-YYYYMMDD.json> [--shots=<dir>] [--no-copy]
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const REPO = path.resolve(HERE, "../../../..");
const [file, ...rest] = process.argv.slice(2);
if (!file) {
  console.error("usage: report.mjs <crawl.json> [--shots=dir] [--no-copy]");
  process.exit(2);
}
const flags = Object.fromEntries(rest.map((a) => a.replace(/^--/, "").split("=")));
const crawl = JSON.parse(fs.readFileSync(file, "utf8"));
const stamp = crawl.date; // YYYYMMDD
const dateIso = `${stamp.slice(0, 4)}-${stamp.slice(4, 6)}-${stamp.slice(6, 8)}`;

const GREEN = new Set(["OK", "VALIDATION", "ALREADY-SELECTED", "REFUSED", "ABSENT-OK", "DISABLED-OK", "CANCELLED", "SKIPPED-DESTRUCTIVE"]);
const ORDER = ["OK", "VALIDATION", "ALREADY-SELECTED", "REFUSED", "FLAKY", "DEAD", "BROKEN", "MISLABELED", "DISABLED-NO-HINT", "ROLE-LEAK", "ERROR", "NOT-FOUND", "DISABLED-OK", "CANCELLED", "SKIPPED-DESTRUCTIVE", "ABSENT-OK"];
const results = crawl.results;
const roles = crawl.roles;
const pages = [...new Set(results.map((r) => r.page))].sort();

const cell = (page, role) => {
  const rows = results.filter((r) => r.page === page && r.role === role);
  if (!rows.length) return "—";
  const counts = {};
  for (const r of rows) counts[r.status] = (counts[r.status] ?? 0) + 1;
  const bad = rows.filter((r) => !GREEN.has(r.status) && r.status !== "NOT-FOUND").length;
  const parts = ORDER.filter((s) => counts[s]).map((s) => `${s} ${counts[s]}`);
  return `${bad ? "🔴" : "🟢"} ${rows.length} · ${parts.join(", ")}`;
};

const totals = {};
for (const r of results) totals[r.status] = (totals[r.status] ?? 0) + 1;

let md = `# Control crawl — ${dateIso} (round 16, r16-m)\n\n`;
md += `Live click-through of every row in \`frontend/src/v2/__inventory__/controls.json\` on **${crawl.base}** (build \`${crawl.build}\`), roles ${roles.join(" + ")}. `;
md += `Crawler: \`tests/e2e/cn/controls/crawl.mjs\` (not in \`make verify\`); machine result: \`frontend/src/v2/__inventory__/crawl-${stamp}.json\`; screenshots for every non-green cell under \`${flags.shots ?? "/home/ubuntu/designs/controls-" + stamp}/\`.\n\n`;
md += `Per control the crawler navigates to the inventory's page (+ \`?view=\` + fixture id), asserts presence (or absence when \`roles\` = admin only), clicks, and waits ≤ 5 s for the inventory's effect (an \`/api\` request on the route pattern, a URL change, a dialog by testId, or a DOM delta), recording network + console/page errors. Destructive controls stop at their Confirm, which is cancelled; a direct mutation is only fired on an \`x16m-\` fixture row.\n\n`;
md += `## Status legend\n\n| status | meaning |\n|---|---|\n`;
md += `| OK | expected effect observed within 5 s |\n| VALIDATION | a save / submit on an incomplete form refused client-side (validation copy, no request) |\n| ALREADY-SELECTED | an option / tab / toggle whose only candidate was already selected — a no-op by design |\n| REFUSED | the server answered 403 / 409 / 422 (business rule such as models.in_use) and the UI surfaced it in a dialog or toast |\n| FLAKY | first attempt DEAD/BROKEN/MISLABELED, retry after reload OK |\n| DEAD | clicked, nothing happened (no network, URL, dialog or DOM change) |\n| BROKEN | an effect fired but an \`/api\` call answered 4xx/5xx or the page logged a console / page error |\n| MISLABELED | something happened but not what the inventory says (the inventory row or the control is wrong) |\n| DISABLED-NO-HINT | rendered disabled with no \`title\` / aria reason — violates the r14-b/r15-d disabled-with-hint contract |\n| ROLE-LEAK | admin-only control visible to the member |\n| ERROR | crawler exception (navigation / selector) |\n| NOT-FOUND | not reachable in the crawled page state (needs a state the fixtures do not produce, or a label the crawler cannot resolve) — a coverage gap, not a verdict |\n| DISABLED-OK | disabled with a reason tooltip |\n| CANCELLED | Confirm OK buttons are never pressed; the opener's dialog was asserted and cancelled |\n| SKIPPED-DESTRUCTIVE | direct mutation on a non-fixture row — deliberately not clicked |\n| ABSENT-OK | admin-only control correctly absent for the member |\n\n`;
md += `## Totals\n\n| status | count |\n|---|---|\n${ORDER.filter((s) => totals[s]).map((s) => `| ${s} | ${totals[s]} |`).join("\n")}\n| **total cells** | **${results.length}** |\n\n`;
md += `## Matrix — page × role\n\n| page | ${roles.join(" | ")} |\n|---|${roles.map(() => "---").join("|")}|\n`;
for (const p of pages) md += `| \`${p}\` | ${roles.map((r) => cell(p, r)).join(" | ")} |\n`;

const bad = results.filter((r) => !GREEN.has(r.status) && r.status !== "NOT-FOUND");
md += `\n## Non-green cells (${bad.length})\n\n| status | role | page | control | expected | observed | shot |\n|---|---|---|---|---|---|---|\n`;
for (const r of bad.sort((a, b) => ORDER.indexOf(a.status) - ORDER.indexOf(b.status) || a.page.localeCompare(b.page))) {
  const exp = r.expected ? `${r.expected.type}: ${r.expected.detail}` : "—";
  md += `| ${r.status} | ${r.role} | \`${r.page}${r.view ? `?view=${r.view}` : ""}\` | \`${r.testId ?? r.id.split(":").pop()}\` (${r.kind}) | ${exp.replace(/\|/g, "\\|")} | ${String(r.detail ?? "").replace(/\|/g, "\\|").slice(0, 160)} | ${r.shot ? `\`${path.basename(r.shot)}\`` : ""} |\n`;
}
const nf = results.filter((r) => r.status === "NOT-FOUND");
const nfByPage = {};
for (const r of nf) nfByPage[r.page] = (nfByPage[r.page] ?? 0) + 1;
md += `\n## Coverage gaps (NOT-FOUND, ${nf.length})\n\nControls the crawler could not reach in the fixture state, by page: ${Object.entries(nfByPage).sort((a, b) => b[1] - a[1]).map(([p, n]) => `\`${p}\` ${n}`).join(", ")}. `;
md += `The two causes are (a) no \`testId\` and a data-bound label (e.g. a row's name) so no selector can be derived — the \`untestable.json\` register — and (b) a page state the fixtures do not produce (wizard steps after step 1, dialogs that open from another control, populated traces / sessions / canaries).\n`;
md += `\n## Fixtures\n\n\`\`\`json\n${JSON.stringify(crawl.fixtures, null, 2)}\n\`\`\`\n`;

const reportPath = path.join(REPO, "docs/cn/reports", `control-crawl-${dateIso}.md`);
fs.writeFileSync(reportPath, md);
console.log(`wrote ${reportPath}`);
if (!("no-copy" in flags)) {
  const dest = path.join(REPO, "frontend/src/v2/__inventory__", `crawl-${stamp}.json`);
  // strip screenshot paths and network bodies to keep the committed file small and free of host paths
  const slim = {
    ...crawl,
    results: crawl.results.map((r) => ({
      id: r.id, role: r.role, page: r.page, tab: r.tab ?? null, view: r.view ?? null, testId: r.testId, kind: r.kind,
      status: r.status, detail: String(r.detail ?? "").slice(0, 200), expected: r.expected, via: r.via ?? null,
      network: (r.network ?? []).slice(0, 4), errors: (r.errors ?? []).slice(0, 2), shot: r.shot ? path.basename(r.shot) : null,
    })),
  };
  // one result per line: diff-friendly, a third of the pretty size
  const head = { ...slim }; delete head.results;
  fs.writeFileSync(dest, `${JSON.stringify(head, null, 2).replace(/\n\}$/, "")},\n  "results": [\n${slim.results.map((r) => `    ${JSON.stringify(r)}`).join(",\n")}\n  ]\n}\n`);
  console.log(`wrote ${dest}`);
}
