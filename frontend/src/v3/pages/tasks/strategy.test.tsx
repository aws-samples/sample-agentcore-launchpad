import { renderToStaticMarkup } from "react-dom/server";
import { MemoryRouter } from "react-router-dom";
import { beforeAll, describe, expect, it, vi } from "vitest";

import i18n from "../../../i18n";
import { V3Tasks } from "../Tasks";
import { strategyUrl } from "./model";
import { StrategySwitch } from "./StrategySwitch";

// the forms themselves load from the API: the dispatch only needs to pick one
vi.mock("./Wizard", () => ({ TaskWizard: () => <div data-form="wizard" /> }));
vi.mock("../online/Editor", () => ({
  OnlineEditor: ({ id, seedFrom }: { id: string | null; seedFrom?: string | null }) => (
    <div data-form="online" data-id={String(id)} data-seed={String(seedFrom ?? "")} />
  ),
}));
vi.mock("./Detail", () => ({ TaskDetail: () => <div data-form="run-detail" /> }));
vi.mock("./List", () => ({ RunList: () => <div data-form="runs" /> }));
vi.mock("../online/List", () => ({ OnlineList: () => <div data-form="online-list" /> }));
vi.mock("../online/Detail", () => ({ OnlineDetail: () => <div data-form="online-detail" /> }));
vi.mock("./ByAgent", () => ({ ByAgent: () => <div data-form="by-agent" /> }));
vi.mock("../../../v2/pages/tasks/AgentScorecard", () => ({ AgentScorecard: ({ agentKey }: { agentKey: string }) => <div data-form="agent" data-key={agentKey} /> }));
vi.mock("../../../v2/pages/tasks/RunCompare", () => ({ RunCompare: ({ a, b }: { a: string; b: string }) => <div data-form="compare" data-ab={`${a}:${b}`} /> }));
vi.mock("../../../auth/auth-context", () => ({ useAuth: () => ({ can: () => true }) }));

const at = (search: string) =>
  renderToStaticMarkup(
    <MemoryRouter initialEntries={[`/v3/tasks${search}`]}>
      <V3Tasks />
    </MemoryRouter>,
  );

beforeAll(async () => {
  vi.stubGlobal("document", { documentElement: {} });
  await i18n.changeLanguage("en");
});

describe("create: the run strategy picks the form", () => {
  it("a plain new task, or a run hand-off / copy, is the run wizard", () => {
    expect(at("?view=new")).toContain('data-form="wizard"');
    expect(at("?view=new&agent=ag-1&dataset=ds-1")).toContain('data-form="wizard"');
    expect(at("?view=new&from=run:r-1")).toContain('data-form="wizard"');
  });

  it("continuous, or a copy of an online config, is the online evaluation form", () => {
    expect(at("?view=new&strategy=continuous&agent=ag-1")).toContain('data-form="online" data-id="null" data-seed=""');
    expect(at("?view=new&from=online:cfg-1")).toContain('data-seed="cfg-1"');
    // a copy keeps the kind of its source, whatever strategy says
    expect(at("?view=new&strategy=continuous&from=run:r-1")).toContain('data-form="wizard"');
  });
});

describe("the list tabs and sub-pages (V2's URL contract)", () => {
  it("按 Agent is the bare page; 一次性评估 is tab=runs / the pre-merge tab=all; 持续评估 is tab=online", () => {
    expect(at("")).toContain('data-form="by-agent"');
    for (const search of ["?tab=runs", "?tab=all"]) expect(at(search), search).toContain('data-form="runs"');
    expect(at("?tab=online")).toContain('data-form="online-list"');
    // 按 Agent leads the tabs
    const html = at("");
    expect(html.indexOf(i18n.t("v2.scorecard.tabs.agents"))).toBeLessThan(html.indexOf(i18n.t("v2.scorecard.tabs.runs")));
  });

  it("an agent's page and a two-run compare are V2's, hosted", () => {
    expect(at("?view=agent&key=ag-1&bench=b")).toMatch(/class="v2 v3-host"[^]*data-form="agent" data-key="ag-1"/);
    expect(at("?view=compare&a=r1&b=r2")).toContain('data-ab="r1:r2"');
    // without their ids they fall back to the list
    expect(at("?view=agent")).toContain('data-form="by-agent"');
  });

  it("a detail opens by kind; edit is an online config's", () => {
    expect(at("?view=detail&kind=run&id=r1")).toContain('data-form="run-detail"');
    expect(at("?view=detail&id=r1")).toContain('data-form="run-detail"');
    expect(at("?view=detail&kind=online&id=c1")).toContain('data-form="online-detail"');
    expect(at("?view=edit&kind=online&id=c1")).toContain('data-form="online" data-id="c1"');
  });
});

describe("the strategy switch", () => {
  const render = (value: "history" | "continuous") =>
    renderToStaticMarkup(
      <MemoryRouter>
        <StrategySwitch value={value} agentId="ag-1" />
      </MemoryRouter>,
    );

  it("marks the current strategy, one-off first", () => {
    const html = render("history");
    const checked = [...html.matchAll(/aria-checked="(true|false)"/g)].map((m) => m[1]);
    expect(checked).toEqual(["true", "false"]);
    expect(html.indexOf(i18n.t("v2.scorecard.tabs.runs"))).toBeLessThan(html.indexOf(i18n.t("v2.scorecard.tabs.online")));
    expect([...render("continuous").matchAll(/aria-checked="(true|false)"/g)].map((m) => m[1])).toEqual(["false", "true"]);
  });
});

describe("strategyUrl", () => {
  it("lands on /v3/tasks with only the agent", () => {
    expect(strategyUrl("continuous", "ag-1")).toBe("/v3/tasks?view=new&strategy=continuous&agent=ag-1");
    expect(strategyUrl("history", "ag-1")).toBe("/v3/tasks?view=new&agent=ag-1");
    expect(strategyUrl("history", null)).toBe("/v3/tasks?view=new");
  });
});
