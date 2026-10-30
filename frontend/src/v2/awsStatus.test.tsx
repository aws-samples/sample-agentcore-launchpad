// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import i18next from "i18next";
import { initReactI18next } from "react-i18next";
import { afterEach, beforeAll, describe, expect, it } from "vitest";

import en from "../locales/en/common.json";
import zhCN from "../locales/zh-CN/common.json";
import { AWS_STATUS_CODES, awsStatusLabel } from "./awsStatus";
import { ResourceTag } from "./pages/knowledge/common";

/** N17-obs456: the zh-CN KB detail page's 数据源 table read 「AVAILABLE」 and its 最近 ingestion
 *  column 「COMPLETE」 — `ResourceTag` passed the raw AgentCore / Bedrock-KB status through, and
 *  the memory pages did the same with memory / strategy statuses. Every known status now reads
 *  through `v2.awsStatus.<CODE>` (en + zh-CN), the tone unchanged; an unknown one stays raw. */

beforeAll(async () => {
  await i18next.use(initReactI18next).init({
    lng: "zh-CN", fallbackLng: "en", resources: { en: { common: en }, "zh-CN": { common: zhCN } },
    defaultNS: "common", interpolation: { escapeValue: false },
  });
});
afterEach(cleanup);

const zh = () => i18next.getFixedT("zh-CN");

describe("AWS resource status copy (N17-obs456)", () => {
  it("names every known status in both languages", () => {
    for (const code of AWS_STATUS_CODES) {
      for (const t of [i18next.getFixedT("en"), zh()]) {
        const label = awsStatusLabel(t, code);
        expect(label, code).toBeTruthy();
        expect(label, code).not.toMatch(/awsStatus/);
      }
      expect(awsStatusLabel(zh(), code), code).not.toBe(code);
    }
  });

  it("reads the data-source, ingestion-job and document statuses in Chinese", () => {
    expect(awsStatusLabel(zh(), "AVAILABLE")).toBe("可用");
    expect(awsStatusLabel(zh(), "COMPLETE")).toBe("已完成");
    expect(awsStatusLabel(zh(), "IN_PROGRESS")).toBe("进行中");
    expect(awsStatusLabel(zh(), "DELETE_UNSUCCESSFUL")).toBe("删除失败");
    expect(awsStatusLabel(zh(), "available")).toBe("可用"); // case-insensitive like resourceState
  });

  it("keeps an unknown status raw and a missing one as a dash", () => {
    expect(awsStatusLabel(zh(), "SOMETHING_NEW")).toBe("SOMETHING_NEW");
    expect(awsStatusLabel(zh(), null)).toBe("—");
    expect(awsStatusLabel(zh(), undefined)).toBe("—");
  });

  it("ResourceTag shows the translated status with its tone, and an explicit label wins", () => {
    render(<ResourceTag status="AVAILABLE" />);
    const tag = screen.getByText("可用");
    expect(tag.className).toContain("green");
    expect(screen.queryByText("AVAILABLE")).toBeNull();
    cleanup();
    render(<ResourceTag status="COMPLETE" />);
    expect(screen.getByText("已完成")).toBeTruthy();
    cleanup();
    render(<ResourceTag status="FAILED" />);
    expect(screen.getByText("失败").className).toContain("red");
    cleanup();
    render(<ResourceTag status="INDEXING" label="索引中（自定义）" />);
    expect(screen.getByText("索引中（自定义）")).toBeTruthy();
    cleanup();
    render(<ResourceTag status="BRAND_NEW" />);
    expect(screen.getByText("BRAND_NEW")).toBeTruthy();
  });
});

describe("runtime, endpoint, gateway and job statuses (N17-obs457)", () => {
  it("reads the agent, endpoint, gateway and deploy-job statuses in Chinese", () => {
    expect(awsStatusLabel(zh(), "READY")).toBe("就绪");
    expect(awsStatusLabel(zh(), "SUCCEEDED")).toBe("成功");
    expect(awsStatusLabel(zh(), "succeeded")).toBe("成功"); // the platform's lowercase job states
    expect(awsStatusLabel(zh(), "CREATE_FAILED")).toBe("创建失败");
    expect(awsStatusLabel(zh(), "UPDATE_UNSUCCESSFUL")).toBe("更新失败");
    expect(awsStatusLabel(zh(), "SYNCHRONIZING")).toBe("同步中");
    expect(awsStatusLabel(zh(), "queued")).toBe("排队中");
    expect(awsStatusLabel(zh(), "ERROR")).toBe("错误");
  });
});

describe("the architect deploy card and the clear dialog (N17-obs457)", () => {
  it("names the deploy job's status (部署 · 修订 N) instead of SUCCEEDED", async () => {
    const { MemoryRouter } = await import("react-router-dom");
    const { Outcome } = await import("./pages/assistant/Proposal");
    const approval = { job_id: "j-1", job_status: "succeeded", agent_id: "a1", agent_name: "travel",
      agent_status: "active", approved_by: "demo-admin", approved_at: null } as never;
    render(
      <MemoryRouter>
        <Outcome revision={2} approval={approval} job={null} agent={null} />
      </MemoryRouter>,
    );
    const tag = await screen.findByTestId("v2-assistant-outcome-job");
    expect(tag.textContent).toBe("成功");
    expect(tag.getAttribute("title")).toBe("succeeded");
    expect(screen.queryByText("SUCCEEDED")).toBeNull();
  });

  // the E22 recorder fails on these raw tokens on the deploy card and the agent deploy panels
  const RAW = ["SUCCEEDED", "RUNNING", "READY", "CREATING", "FAILED", "PENDING", "QUEUED", "ACTIVE", "CANCELLED", "INTERRUPTED"];

  it.each(["queued", "running", "succeeded", "failed", "cancelled", "interrupted"])(
    "names the deploy job in state %s and every stage state, never a raw token", async (state) => {
      const { MemoryRouter } = await import("react-router-dom");
      const { Outcome } = await import("./pages/assistant/Proposal");
      const approval = { job_id: "j-1", job_status: state, agent_id: "a1", agent_name: "travel",
        agent_status: state === "succeeded" ? "active" : state === "failed" ? "failed" : "deploying",
        approved_by: "demo-admin", approved_at: null } as never;
      const stages = ["pending", "running", "succeeded", "skipped", "failed", "cancelled", "interrupted"]
        .map((st, i) => ({ name: `s${i}`, status: st, detail: "" }));
      const agent = { id: "a1", name: "travel", status: "deploying", deployments: [{ job_id: "j-1", stages }] } as never;
      render(
        <MemoryRouter>
          <Outcome revision={2} approval={approval} job={null} agent={agent} />
        </MemoryRouter>,
      );
      const card = await screen.findByTestId("v2-assistant-outcome");
      for (const raw of RAW) expect(card.textContent, raw).not.toContain(raw);
      expect(card.textContent).not.toMatch(/assistantPage\.|v2\.agents\./);
      expect(screen.getByTestId("v2-assistant-outcome-job").textContent).toBe(awsStatusLabel(zh(), state));
    });

  it("words a stage detail's leading status (部署 · READY · arn) and keeps the rest", async () => {
    const { wordStatusPrefix } = await import("./awsStatus");
    expect(wordStatusPrefix(zh(), "READY · arn:aws-cn:bedrock-agentcore:x")).toBe("就绪 · arn:aws-cn:bedrock-agentcore:x");
    expect(wordStatusPrefix(zh(), "pip+zip 41.2s · 64.6MB")).toBe("pip+zip 41.2s · 64.6MB");
    expect(wordStatusPrefix(zh(), "")).toBe("");
  });

  it("words the canaries and the step of a conversation clear", async () => {
    const { clearCanaryList, clearStepText } = await import("../lib/assistant");
    const fp = { canaries: [{ id: "c", name: "CANARY-kid", status: "running",
      resources: { ab_test: true, endpoints: ["stableabc"], online_evaluations: 2 } }] } as never;
    expect(clearCanaryList(fp, zh())).toBe("CANARY-kid (进行中 · A/B 分流 · 2 个在线评估 · 端点 stableabc)");
    expect(clearCanaryList(fp)).toBe("CANARY-kid (running · A/B split · 2 online eval · endpoints stableabc)");
    expect(clearStepText({ key: "canary:c", state: "waiting", detail: "endpoint:x (DELETING)" } as never, zh()))
      .toBe("canary:c · 等待中 · endpoint:x (DELETING)"); // the detail is the server's text
  });
});

/** N17-obs456 / obs457: no V2 page puts a raw status enum in the DOM. Each shape below is the
 *  way a raw status used to reach the screen; a file listed in ALLOWED keeps one on purpose. */
describe("no V2 page renders a raw status enum (N17-obs456 / obs457 guard)", () => {
  const SHAPES: [string, RegExp][] = [
    ["JSX child", /(?<![=\w$])\{\s*[\w.?]+\.(status|job_status|agent_status|target_status)\s*((\?\?|\|\|)\s*"[^"]*"\s*)?\}/],
    ["upper-cased status", /String\([^)]*(status|Status)[^)]*\)\.toUpperCase\(\)/],
    ["status in a template", /\$\{\s*[\w.?]+\.(status|job_status|agent_status|target_status)\s*\}/],
    ["raw status as a t() param", /(\bt\(.*|^\s*)\bstatus:\s*[\w.?]+\.(status|status_after)\s*[,})]/],
    ["status options labelled raw", /statuses\.map\(\(s\) => \(\{ value: s, label: s \}\)\)/],
    ["render returns a raw status", /=>\s*[\w.?]+\.(status|extractionStatus)\s*(\?\?\s*"[^"]*"\s*)?[,}]/],
    ["progress or a raw status", /progress\s*\|\|\s*[\w.?]+\.status\b/],
    ["raw status in a Descriptions value", /\bvalue:\s*[\w.?]+\.(status|extractionStatus)\s*(\?\?|\|\||,)/],
  ];
  // file → why it keeps a raw value (never a status the user reads as copy)
  const ALLOWED: Record<string, string> = {
    "pages/governance/RateLimits.tsx": "the status only reaches a title= tooltip (blocker reason)",
    "pages/assistant/RunHistory.tsx": "a cache signature string, never rendered",
    "pages/canary/CanaryDetail.tsx": "a rowKey, never rendered",
    "pages/tasks/TaskDetail.tsx": "a cache key, never rendered",
    "pages/assistant/NextSteps.tsx": "a reloadKey, never rendered",
  };

  it("finds no raw status anywhere under src/v2", async () => {
    const fs = await import("node:fs");
    const path = await import("node:path");
    const root = path.resolve(__dirname);
    const walk = (dir: string): string[] =>
      fs.readdirSync(dir, { withFileTypes: true }).flatMap((e) =>
        e.isDirectory() ? walk(path.join(dir, e.name))
          : e.name.endsWith(".tsx") && !e.name.includes(".test.") ? [path.join(dir, e.name)] : []);
    const offenders: string[] = [];
    for (const file of walk(root)) {
      const rel = path.relative(root, file);
      if (rel in ALLOWED) continue;
      const lines = fs.readFileSync(file, "utf8").split("\n");
      lines.forEach((line, i) => {
        for (const [name, re] of SHAPES) {
          const m = re.exec(line);
          if (!m) continue;
          // a status inside a t() key, a class name, a key / test id / data- / title= attribute
          // is not copy the user reads
          if (/(\bt\(`|className=\{`|[kK]ey[=:(]|testId=|data-[\w-]+=|title=)/.test(line.slice(0, m.index))) continue;
          if (name === "upper-cased status" && /^\s*[!=]==/.test(line.slice(m.index + m[0].length))) continue;
          offenders.push(`${rel}:${i + 1} ${name}`);
        }
      });
    }
    expect(offenders).toEqual([]);
  });
});
