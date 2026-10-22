import { describe, expect, it } from "vitest";

import {
  classicOnlineToTasksUrl,
  newStrategy,
  newTaskParams,
  onlineToTasksUrl,
  parseTasksTab,
  tasksTabParams,
} from "./urls";

const q = (search: string) => new URLSearchParams(search);

describe("tasks tab", () => {
  it("opens 按 Agent without a tab, and on anything it does not know", () => {
    expect(parseTasksTab(q(""))).toBe("agents");
    expect(parseTasksTab(q("tab=agents"))).toBe("agents");
    expect(parseTasksTab(q("tab=nope"))).toBe("agents");
  });

  it("reads runs and the pre-merge all as 一次性评估, online as 持续评估", () => {
    expect(parseTasksTab(q("tab=runs"))).toBe("runs");
    expect(parseTasksTab(q("?tab=all"))).toBe("runs");
    expect(parseTasksTab(q("tab=online"))).toBe("online");
  });

  it("writes the bare page for 按 Agent and tab= for the others, round-tripping", () => {
    expect(tasksTabParams("agents")).toEqual({});
    expect(tasksTabParams("runs")).toEqual({ tab: "runs" });
    expect(tasksTabParams("online")).toEqual({ tab: "online" });
    for (const tab of ["agents", "runs", "online"] as const) {
      expect(parseTasksTab(q(new URLSearchParams(tasksTabParams(tab)).toString()))).toBe(tab);
    }
  });
});

describe("create form", () => {
  it("is the one-off wizard unless strategy=continuous", () => {
    expect(newStrategy(q("view=new"))).toBe("history");
    expect(newStrategy(q("view=new&agent=a1"))).toBe("history");
    expect(newStrategy(q("view=new&strategy=continuous"))).toBe("continuous");
    expect(newStrategy(q("view=new&strategy=bogus"))).toBe("history");
  });

  it("keeps the kind of the copied task, whatever strategy says", () => {
    expect(newStrategy(q("view=new&from=online:cfg-1"))).toBe("continuous");
    expect(newStrategy(q("view=new&from=run:r1"))).toBe("history");
    expect(newStrategy(q("view=new&from=run:r1&strategy=continuous"))).toBe("history");
  });

  it("switches forms carrying only the agent", () => {
    expect(newTaskParams("history")).toEqual({ view: "new" });
    expect(newTaskParams("continuous")).toEqual({ view: "new", strategy: "continuous" });
    expect(newTaskParams("continuous", "a/1")).toEqual({ view: "new", strategy: "continuous", agent: "a/1" });
    expect(newTaskParams("history", "a1")).toEqual({ view: "new", agent: "a1" });
    expect(newTaskParams("history", "")).toEqual({ view: "new" });
    expect(newTaskParams("history", null)).toEqual({ view: "new" });
  });
});

describe("retired 在线评估 URLs", () => {
  it("maps /v2/eval/online onto the 持续评估 tab and its sub-pages", () => {
    expect(onlineToTasksUrl("")).toBe("/v2/eval/tasks?tab=online");
    expect(onlineToTasksUrl("?view=new")).toBe("/v2/eval/tasks?view=new&strategy=continuous");
    expect(onlineToTasksUrl("?view=detail&id=cfg-1")).toBe("/v2/eval/tasks?view=detail&kind=online&id=cfg-1");
    expect(onlineToTasksUrl("?view=edit&id=cfg-1")).toBe("/v2/eval/tasks?view=edit&kind=online&id=cfg-1");
  });

  it("lands on the list for a sub-page without an id or an unknown view", () => {
    expect(onlineToTasksUrl("?view=detail")).toBe("/v2/eval/tasks?tab=online");
    expect(onlineToTasksUrl("?view=bogus&id=x")).toBe("/v2/eval/tasks?tab=online");
  });

  it("keeps an id intact through the encoding", () => {
    const url = onlineToTasksUrl(`?view=detail&id=${encodeURIComponent("lp a&b")}`);
    expect(new URLSearchParams(url.split("?")[1]).get("id")).toBe("lp a&b");
  });

  it("maps the classic /evaluation?view=online straight to the final URL", () => {
    expect(classicOnlineToTasksUrl("?view=online")).toBe("/v2/eval/tasks?tab=online");
    expect(classicOnlineToTasksUrl("?view=online&oe=new")).toBe("/v2/eval/tasks?view=new&strategy=continuous");
    expect(classicOnlineToTasksUrl("?view=online&oe=cfg-1")).toBe("/v2/eval/tasks?view=detail&kind=online&id=cfg-1");
    const odd = classicOnlineToTasksUrl(`?view=online&oe=${encodeURIComponent("a&b=c")}`);
    expect(new URLSearchParams(odd.split("?")[1]).get("id")).toBe("a&b=c");
  });
});
