import { describe, expect, it } from "vitest";

import { onlineHref, onlineList, v3OnlineToTasks } from "./actions";

describe("online config URLs live under 评估任务", () => {
  it("maps the former /v3/online vocabulary onto /v3/tasks", () => {
    expect(onlineHref({ view: "new" })).toBe("/v3/tasks?view=new&strategy=continuous");
    expect(onlineHref({ view: "new", agent: "ag-1" })).toBe("/v3/tasks?view=new&strategy=continuous&agent=ag-1");
    expect(onlineHref({ view: "detail", id: "c 1" })).toBe("/v3/tasks?view=detail&kind=online&id=c+1");
    expect(onlineHref({ view: "edit", id: "c1" })).toBe("/v3/tasks?view=edit&kind=online&id=c1");
    expect(onlineHref({ view: "detail" })).toBe(onlineList);
    expect(onlineList).toBe("/v3/tasks?tab=online");
  });

  it("redirects an old /v3/online link to the same state", () => {
    expect(v3OnlineToTasks("")).toBe("/v3/tasks?tab=online");
    expect(v3OnlineToTasks("?view=new")).toBe("/v3/tasks?view=new&strategy=continuous");
    expect(v3OnlineToTasks("?view=detail&id=c1")).toBe("/v3/tasks?view=detail&kind=online&id=c1");
    expect(v3OnlineToTasks("?view=edit&id=c1")).toBe("/v3/tasks?view=edit&kind=online&id=c1");
  });
});
