// N17-obs461: the SQLite ledger's datetimes reach the console as naive UTC ISO
// ("2026-10-24T10:29:54.079198") next to AWS-sourced ones with an offset ("…+00:00"). Every
// `new Date(<api string>)` read a naive one as BROWSER-LOCAL time, so in Asia/Shanghai the
// console showed ledger times 8 h early (live: run bdd265252e9f 创建时间 10:29:54, not 18:29:54)
// while offset-carrying fields on the same page were right. Every API time now parses
// through `lib/timestamps.parseTimestamp` (naive → UTC); this suite runs in Asia/Shanghai.
import { afterAll, beforeAll, describe, expect, it } from "vitest";

const ORIGINAL_TZ = process.env.TZ;
beforeAll(() => {
  process.env.TZ = "Asia/Shanghai";
});
afterAll(() => {
  process.env.TZ = ORIGINAL_TZ;
});

const NAIVE = "2026-10-24T10:29:54.079198";
const NAIVE_SPACE = "2026-10-24 10:29:54.079198";
const OFFSET = "2026-10-24T10:29:54.079198+00:00";
const ZULU = "2026-10-24T10:29:54.079Z";

describe("ledger times read as UTC in Asia/Shanghai (N17-obs461)", () => {
  it("the zone really is UTC+8 here", () => {
    expect(new Date(ZULU).getTimezoneOffset()).toBe(-480);
  });

  it("parseTimestamp reads naive, space-separated, +00:00 and Z as the same instant", async () => {
    const { parseTimestamp } = await import("../lib/timestamps");
    const want = Date.UTC(2026, 9, 24, 10, 29, 54, 79);
    for (const v of [NAIVE, NAIVE_SPACE, OFFSET, ZULU]) expect(parseTimestamp(v)?.getTime(), v).toBe(want);
    expect(parseTimestamp("2026-10-24")?.getTime()).toBe(Date.UTC(2026, 9, 24)); // a date-only value is UTC
    expect(parseTimestamp("not a time")).toBeNull();
    expect(parseTimestamp(null)).toBeNull();
  });

  it("fmtTime shows the Beijing time for every shape (18:29:54, not 10:29:54)", async () => {
    const { fmtTime } = await import("./format");
    for (const v of [NAIVE, NAIVE_SPACE, OFFSET, ZULU]) expect(fmtTime(v), v).toBe("2026-10-24 18:29:54");
  });

  it("the observability clocks, the heartbeat age and the skill-lab elapsed time agree", async () => {
    const { fmtClock, fmtClockShort } = await import("./pages/observability/format");
    for (const v of [NAIVE, OFFSET, ZULU]) {
      expect(fmtClock(v), v).toBe("18:29:54");
      expect(fmtClockShort(v), v).toBe("18:29");
    }
    const { heartbeatAge } = await import("./format");
    const now = Date.UTC(2026, 9, 24, 10, 30, 54, 79);
    for (const v of [NAIVE, OFFSET, ZULU]) expect(heartbeatAge(v, now), v).toBe(60);
    const { elapsed } = await import("../lib/skillLab");
    expect(elapsed({ started_at: NAIVE, finished_at: "2026-10-24T10:30:24.079198+00:00" } as never)).toBe("30s");
  });

  it("sorts mixed naive / offset times by instant, newest first", async () => {
    const { compareTimesDesc } = await import("../lib/timestamps");
    // lexically "…T10:29:54.079198" > "…T10:29:54.079198+00:00"? no — compare instants
    const rows = ["2026-10-24T10:00:00+00:00", "2026-10-24T11:00:00", "2026-10-24T10:30:00Z", null];
    expect([...rows].sort(compareTimesDesc)).toEqual(
      ["2026-10-24T11:00:00", "2026-10-24T10:30:00Z", "2026-10-24T10:00:00+00:00", null]);
  });
});

/** No source parses an API time with a raw `new Date(<value>)` / `Date.parse` again. */
describe("API times parse through parseTimestamp (N17-obs461 guard)", () => {
  // file → why a raw Date construction there is not an API time string
  const ALLOWED: Record<string, string> = {
    "lib/timestamps.ts": "the parser itself (after it normalised the zone)",
    "v2/pages/policy/Decisions.tsx": "builds a window start from Date.now() arithmetic",
    "v2/pages/skilllab/ArtifactBrowser.tsx": "formats an epoch-millisecond number",
    "v2/pages/observability/format.ts": "bucketDate: Logs Insights UTC bins, the Z appended explicitly",
  };
  it("finds no raw Date parse of a string outside the allow-list", async () => {
    const fs = await import("node:fs");
    const path = await import("node:path");
    const root = path.resolve(__dirname, "..");
    const walk = (dir: string): string[] =>
      fs.readdirSync(dir, { withFileTypes: true }).flatMap((e) =>
        e.isDirectory() ? walk(path.join(dir, e.name))
          : /\.(ts|tsx)$/.test(e.name) && !/\.(test|property)\./.test(e.name) ? [path.join(dir, e.name)] : []);
    const offenders: string[] = [];
    for (const file of walk(root)) {
      const rel = path.relative(root, file).split(path.sep).join("/");
      fs.readFileSync(file, "utf8").split("\n").forEach((line, i) => {
        // a raw parse, or a string sort / comparison of a time field (naive and offset values of
        // one instant compare unequal as text)
        const timeSort = /(_at|At|\btime)\b[^;]*\.localeCompare\(|localeCompare\([^)]*(_at|At\b|\btime\b)/;
        if (!/new Date\([^)]|Date\.parse\(/.test(line) && !timeSort.test(line)) return;
        if (rel === "v2/pages/observability/format.ts" && /bucket/.test(line)) return;
        if (rel in ALLOWED && rel !== "v2/pages/observability/format.ts") return;
        offenders.push(`${rel}:${i + 1}`);
      });
    }
    expect(offenders).toEqual([]);
  });
});

/** N17-obs463: the console formats in the DISPLAY zone (default Asia/Shanghai), whatever the
 *  browser's zone is, and the API now sends "+08:00" — the same instant. */
describe("times show in the display zone in any browser zone (N17-obs463)", () => {
  const SHANGHAI = "2026-10-24T18:29:54.079198+08:00"; // what the API returns for bdd265252e9f
  for (const tz of ["UTC", "Asia/Shanghai", "America/Los_Angeles"]) {
    it(`TZ=${tz}: every formatter shows 18:29:54 for the bdd265252e9f value`, async () => {
      const previous = process.env.TZ;
      process.env.TZ = tz;
      try {
        const { fmtTime } = await import("./format");
        const { fmtClock, fmtClockShort } = await import("./pages/observability/format");
        const { shortDate, shortTime } = await import("./scorecard");
        const { zonedClock, formatApiTime } = await import("../lib/timestamps");
        for (const v of [SHANGHAI, NAIVE, OFFSET, ZULU]) {
          expect(fmtTime(v), v).toBe("2026-10-24 18:29:54");
          expect(fmtClock(v), v).toBe("18:29:54");
          expect(fmtClockShort(v), v).toBe("18:29");
          expect(zonedClock(v), v).toBe("18:29:54");
          expect(shortDate(v), v).toBe("10-24");
          expect(shortTime(v), v).toBe("18:29");
          expect(formatApiTime(v, "en-GB", { hour: "2-digit", minute: "2-digit", hour12: false }), v).toBe("18:29");
        }
        // a job-log line just before midnight UTC is already the next day in Beijing
        expect(fmtTime("2026-10-24T23:59:30+00:00")).toBe("2026-10-25 07:59:30");
      } finally {
        process.env.TZ = previous;
      }
    });
  }

  it("the zone comes from the capabilities payload; an unknown one is ignored", async () => {
    const ts = await import("../lib/timestamps");
    const { parseCapabilities } = await import("../lib/capabilities");
    expect(ts.displayTimeZone()).toBe("Asia/Shanghai");
    parseCapabilities({ features: [], display_timezone: "UTC" } as never);
    expect(ts.zonedClock(SHANGHAI)).toBe("10:29:54");
    parseCapabilities({ features: [], display_timezone: "Mars/Olympus" } as never);
    expect(ts.displayTimeZone()).toBe("UTC");
    ts.setDisplayTimeZone("Asia/Shanghai");
    expect(ts.zonedClock(SHANGHAI)).toBe("18:29:54");
  });
});

/** No source formats a time in the BROWSER's zone again: no zone-less toLocaleTimeString /
 *  toLocaleDateString, no Date getHours/getDate/… wall-clock reads, no ISO `.slice(11, 19)`
 *  clock, no Date toLocaleString — only lib/timestamps' zoned helpers. */
describe("times format through the display-zone helpers (N17-obs463 guard)", () => {
  // number formatting that shares the toLocaleString name: file → the numeric receivers
  const NUMERIC_TO_LOCALE: Record<string, RegExp> = {
    "v2/format.ts": /value\.toLocaleString\(\)/,
    "v2/pages/observability/format.ts": /Math\.round\(n\)\.toLocaleString/,
    "v2/pages/skilllab/TaskgenWizard.tsx": /size\.toLocaleString/,
    "v2/pages/skilllab/TasksetEditor.tsx": /size\.toLocaleString/,
    "v2/pages/skilllab/TasksetDetail.tsx": /size\.toLocaleString/,
    "v2/pages/skilllab/EvalResults.tsx": /value\.toLocaleString/,
    "v2/pages/modelProviders/ModelTable.tsx": /value\.toLocaleString/,
    "v2/pages/agents/SnapshotsCard.tsx": /length\.toLocaleString/,
    "pages/CreateAgentStudio.tsx": /(length|MAX_CODE)\.toLocaleString/,
  };
  it("finds no browser-zone formatting outside lib/timestamps.ts", async () => {
    const fs = await import("node:fs");
    const path = await import("node:path");
    const root = path.resolve(__dirname, "..");
    const walk = (dir: string): string[] =>
      fs.readdirSync(dir, { withFileTypes: true }).flatMap((e) =>
        e.isDirectory() ? walk(path.join(dir, e.name))
          : /\.(ts|tsx)$/.test(e.name) && !/\.(test|property)\./.test(e.name) ? [path.join(dir, e.name)] : []);
    const offenders: string[] = [];
    const wallClock = /\.(getHours|getMinutes|getSeconds|getDate|getDay|getMonth|getFullYear)\(\)|\.toLocale(Time|Date)String\(|\.slice\(11, ?19\)|\.toTimeString\(|\.toDateString\(/;
    for (const file of walk(root)) {
      const rel = path.relative(root, file).split(path.sep).join("/");
      if (rel === "lib/timestamps.ts") continue;
      fs.readFileSync(file, "utf8").split("\n").forEach((line, i) => {
        const localeString = /\.toLocaleString\(/.test(line) && !(NUMERIC_TO_LOCALE[rel]?.test(line));
        if (wallClock.test(line) || localeString) offenders.push(`${rel}:${i + 1}`);
      });
    }
    expect(offenders).toEqual([]);
  });
});

/** N17-obs463 scope add (E22 recommendations card): a time can also bypass the formatters as a
 *  raw string — sliced, interpolated or passed straight into an i18n parameter. No source does
 *  that to a time field (`*_at`, `*At`, an event `ts`) any more. */
describe("time fields never reach the screen as raw strings (N17-obs463 guard)", () => {
  // file → line pattern that is not a displayed value (a React key, a dateTime attribute)
  const NOT_DISPLAYED: [string, RegExp][] = [
    ["v2/pages/assistant/Preparation.tsx", /key=\{`\$\{kbId\}:\$\{catalog\.fetched_at\}`\}/],
    ["v2/pages/memory/ShortTermTab.tsx", /key=\{e\.event_id \?\? `\$\{e\.at\}-\$\{i\}`\}/],
  ];
  it("finds no sliced / interpolated / raw i18n-param time field", async () => {
    const fs = await import("node:fs");
    const path = await import("node:path");
    const root = path.resolve(__dirname, "..");
    const walk = (dir: string): string[] =>
      fs.readdirSync(dir, { withFileTypes: true }).flatMap((e) =>
        e.isDirectory() ? walk(path.join(dir, e.name))
          : /\.(ts|tsx)$/.test(e.name) && !/\.(test|property)\./.test(e.name) ? [path.join(dir, e.name)] : []);
    const FIELD = String.raw`[A-Za-z_$][\w$]*(?:\??\.[A-Za-z_$][\w$]*)*(?:_at|At|\.ts|\.at)\b(?!\()`;
    const rules = [
      new RegExp(`${FIELD}\\??\\.(slice|substring|substr|replace|split|padEnd|padStart)\\(`), // string ops
      new RegExp(`\\$\\{\\s*${FIELD}\\s*\\}`), // template interpolation
      new RegExp(`(>|^)\\s*\\{\\s*${FIELD}\\s*(\\?\\?[^}]*)?\\}`), // a JSX text child
      new RegExp(`\\bt\\([^)]*\\b\\w+:\\s*${FIELD}\\s*(\\?\\?[^,}]*)?[,}]`), // a raw i18n parameter
    ];
    const offenders: string[] = [];
    for (const file of walk(root)) {
      const rel = path.relative(root, file).split(path.sep).join("/");
      if (rel === "lib/timestamps.ts") continue;
      fs.readFileSync(file, "utf8").split("\n").forEach((line, i) => {
        if (!rules.some((r) => r.test(line))) return;
        if (NOT_DISPLAYED.some(([f, r]) => f === rel && r.test(line))) return;
        offenders.push(`${rel}:${i + 1}: ${line.trim()}`);
      });
    }
    expect(offenders).toEqual([]);
  });

  it("the build tooltip and a video's date are formatted in the display zone", async () => {
    const { zonedDate } = await import("../lib/timestamps");
    expect(zonedDate("2026-10-24T23:30:00Z")).toBe("2026-10-25"); // already the 25th in Beijing
    expect(zonedDate("2026-10-24")).toBe("2026-10-24");
  });
});

/** The E22 recommendations card (default host, run db8209c7a1df, recommendation 0c9371e97e86):
 *  the picker's run time and the card's created time were naive ledger values, the accept time
 *  an aware one. The screen showed 05:19:02 / 05:47:55 (UTC) next to 13:57:39 (Beijing): the
 *  pre-obs461 parser still running in a tab opened before the 16d265be deploy. Every shape the
 *  card can receive — before (naive / +00:00) and after obs463 (+08:00) — now reads Beijing. */
describe("the recommendations card shows one zone (N17-obs463 scope add)", () => {
  for (const tz of ["UTC", "Asia/Shanghai"]) {
    it(`TZ=${tz}`, async () => {
      const previous = process.env.TZ;
      process.env.TZ = tz;
      try {
        const { fmtTime } = await import("./format");
        expect(fmtTime("2026-10-26T05:19:02.131882")).toBe("2026-10-26 13:19:02");
        expect(fmtTime("2026-10-26T13:19:02.131882+08:00")).toBe("2026-10-26 13:19:02");
        expect(fmtTime("2026-10-26T05:47:55.768260")).toBe("2026-10-26 13:47:55");
        expect(fmtTime("2026-10-26T05:57:39.771166+00:00")).toBe("2026-10-26 13:57:39");
        expect(fmtTime("2026-10-26T13:57:39.771166+08:00")).toBe("2026-10-26 13:57:39");
      } finally {
        process.env.TZ = previous;
      }
    });
  }
});
