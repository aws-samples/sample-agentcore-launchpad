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
vi.mock("./Detail", () => ({ TaskDetail: () => <div data-form="detail" /> }));
vi.mock("./List", () => ({ TaskList: () => <div data-form="list" /> }));

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
  it("lands on /v3/tasks with only the agent (also from /v3/online?view=new)", () => {
    expect(strategyUrl("continuous", "ag-1")).toBe("/v3/tasks?view=new&strategy=continuous&agent=ag-1");
    expect(strategyUrl("history", "ag-1")).toBe("/v3/tasks?view=new&agent=ag-1");
    expect(strategyUrl("history", null)).toBe("/v3/tasks?view=new");
  });
});
