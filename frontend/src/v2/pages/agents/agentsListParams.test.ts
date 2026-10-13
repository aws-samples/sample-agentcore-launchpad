import { describe, expect, it } from "vitest";

import {
  LIST_DEFAULTS,
  listOnly,
  parseListParams,
  withView,
  writeListParams,
} from "./agentsListParams";

const p = (s: string) => new URLSearchParams(s);

describe("parseListParams", () => {
  it("is all defaults on an empty query", () => {
    expect(parseListParams(p(""))).toEqual(LIST_DEFAULTS);
  });

  it("reads q, status and method", () => {
    expect(parseListParams(p("q=hr%20bot&status=failed&method=harness"))).toEqual({
      q: "hr bot",
      status: "failed",
      method: "harness",
    });
  });

  it("ignores unknown status and method values", () => {
    expect(parseListParams(p("status=broken&method=lambda&q=x"))).toEqual({
      q: "x",
      status: "",
      method: "",
    });
  });

  it("keeps the search text exactly as typed", () => {
    expect(parseListParams(p("q=%20a%20")).q).toBe(" a ");
  });

  it("does not treat view/id as filters", () => {
    expect(parseListParams(p("view=detail&id=a1"))).toEqual(LIST_DEFAULTS);
  });
});

describe("writeListParams", () => {
  it("omits defaults", () => {
    expect(writeListParams(p(""), LIST_DEFAULTS).toString()).toBe("");
  });

  it("removes a filter set back to all", () => {
    const next = writeListParams(p("status=active&method=byoc&q=x"), { ...LIST_DEFAULTS, q: "x" });
    expect(next.toString()).toBe("q=x");
  });

  it("keeps other params untouched", () => {
    const next = writeListParams(p("view=detail&id=a1"), { q: "", status: "active", method: "" });
    expect(next.get("view")).toBe("detail");
    expect(next.get("id")).toBe("a1");
    expect(next.get("status")).toBe("active");
  });

  it("does not mutate its input", () => {
    const prev = p("status=active");
    writeListParams(prev, LIST_DEFAULTS);
    expect(prev.toString()).toBe("status=active");
  });

  it("round-trips through parse", () => {
    const f = { q: "a&b=c", status: "deploying", method: "zip_runtime" } as const;
    expect(parseListParams(writeListParams(p(""), f))).toEqual(f);
  });
});

describe("listOnly / withView", () => {
  it("listOnly drops view, id and invalid filters", () => {
    expect(listOnly(p("view=detail&id=a1&status=failed&method=nope&x=1")).toString()).toBe(
      "status=failed",
    );
  });

  it("withView sets the sub-view and carries the filters", () => {
    const next = withView(p("view=detail&id=old&q=hr&method=harness"), "identity", "a2");
    expect(next.get("view")).toBe("identity");
    expect(next.get("id")).toBe("a2");
    expect(parseListParams(next)).toEqual({ q: "hr", status: "", method: "harness" });
    expect(listOnly(next).toString()).toBe("q=hr&method=harness");
  });

  it("withView on a bare list is just view + id", () => {
    expect(withView(p(""), "detail", "a1").toString()).toBe("view=detail&id=a1");
  });
});
