import { describe, expect, it } from "vitest";

import { builtinsNotInCatalog } from "./evaluators";

describe("builtinsNotInCatalog", () => {
  const catalog = [{ id: "Builtin.Helpfulness" }, { id: "my_judge-abc" }];

  it("names copied built-ins the region catalog does not list", () => {
    expect(
      builtinsNotInCatalog(["Builtin.Helpfulness", "Builtin.ContextRelevance", "my_judge-abc"], catalog),
    ).toEqual(["Builtin.ContextRelevance"]);
  });

  it("never prunes custom ids, which a failed account listing leaves out", () => {
    expect(builtinsNotInCatalog(["other_judge-xyz"], catalog)).toEqual([]);
  });

  it("prunes nothing before the catalog has loaded", () => {
    expect(builtinsNotInCatalog(["Builtin.ContextRelevance"], [])).toEqual([]);
  });
});
