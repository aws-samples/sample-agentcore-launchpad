import type { TFunction } from "i18next";
import { describe, expect, it } from "vitest";

import { benchName } from "./scorecardText";

const t = ((key: string) => key) as unknown as TFunction;

describe("benchName", () => {
  it("names a dataset, live traffic, or the hidden placeholder (N15-obs15)", () => {
    expect(benchName(t, { kind: "dataset", label: "golden", dataset_id: "d1" })).toBe("golden");
    expect(benchName(t, { kind: "dataset", label: null, dataset_id: "d1" })).toBe("d1");
    expect(benchName(t, { kind: "live", label: null, dataset_id: null })).toBe("v2.compare.live");
    // a hidden dataset shows the placeholder, never its id
    expect(benchName(t, { kind: "dataset", label: null, hidden: true, dataset_id: "d1" })).toBe("v2.common.hiddenRef");
    expect(benchName(t, { kind: "cloud", label: null, hidden: true, dataset_id: "cl-1" })).toBe("v2.common.hiddenRef");
  });
});
