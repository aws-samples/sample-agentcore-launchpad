import i18next from "i18next";
import { beforeAll, describe, expect, it } from "vitest";

import en from "../locales/en/common.json";
import zhCN from "../locales/zh-CN/common.json";
import type { OverviewServiceDetail } from "./api";
import { serviceDetailText } from "./service-detail";

const i18n = i18next.createInstance();

const DETAIL: OverviewServiceDetail = {
  gateway: "gw-1",
  memory: "",
  registry: "blocked by account policy",
  observability: "aws/spans",
  evaluation: { runs: 15, pass_rate: 0.85 },
};

beforeAll(async () => {
  await i18n.init({
    lng: "en",
    fallbackLng: "en",
    resources: { en: { common: en }, "zh-CN": { common: zhCN } },
    defaultNS: "common",
    interpolation: { escapeValue: false },
  });
});

describe("serviceDetailText (WB-001 · r7-e)", () => {
  it("formats the structured evaluation detail in English with plurals", () => {
    const t = i18n.getFixedT("en");
    expect(serviceDetailText(t, "evaluation", DETAIL)).toBe("15 runs · 85 % pass");
    expect(
      serviceDetailText(t, "evaluation", { ...DETAIL, evaluation: { runs: 1, pass_rate: 1 } }),
    ).toBe("1 run · 100 % pass");
    // runs without scores yet: the count alone
    expect(
      serviceDetailText(t, "evaluation", { ...DETAIL, evaluation: { runs: 3, pass_rate: null } }),
    ).toBe("3 runs");
    expect(serviceDetailText(t, "evaluation", { ...DETAIL, evaluation: null })).toBe("");
  });

  it("formats it in zh-CN", () => {
    const t = i18n.getFixedT("zh-CN");
    expect(serviceDetailText(t, "evaluation", DETAIL)).toBe("15 次运行 · 通过率 85 %");
    expect(
      serviceDetailText(t, "evaluation", { ...DETAIL, evaluation: { runs: 1, pass_rate: null } }),
    ).toBe("1 次运行");
  });

  it("passes bootstrap-row details through untouched", () => {
    const t = i18n.getFixedT("en");
    expect(serviceDetailText(t, "gateway", DETAIL)).toBe("gw-1");
    expect(serviceDetailText(t, "memory", DETAIL)).toBe("");
    expect(serviceDetailText(t, "registry", DETAIL)).toBe("blocked by account policy");
  });
});

// D20 (P1-c3): the policy row's detail is the backend's "<mode> · <n> active" string — the
// V2 home card shows it as the row's tooltip once the row is lit; an absent detail (server
// without the feature, or not lit) is empty so the row keeps its "lights up after…" hint.
describe("serviceDetailText · policy row", () => {
  it("passes the mode · active summary through when present", () => {
    const t = i18n.getFixedT("en");
    expect(serviceDetailText(t, "policy", { ...DETAIL, policy: "enforce · 3 active" })).toBe("enforce · 3 active");
  });
  it("is empty when the server sends no policy detail", () => {
    const t = i18n.getFixedT("en");
    expect(serviceDetailText(t, "policy", DETAIL)).toBe("");
    expect(serviceDetailText(t, "policy", { ...DETAIL, policy: null })).toBe("");
  });
});
