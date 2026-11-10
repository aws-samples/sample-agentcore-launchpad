import i18next from "i18next";
import { beforeAll, describe, expect, it } from "vitest";

import en from "../locales/en/common.json";
import zhCN from "../locales/zh-CN/common.json";
import { onlineQualityFoot } from "./online-quality";

const i18n = i18next.createInstance();

beforeAll(async () => {
  await i18n.init({
    lng: "en",
    fallbackLng: "en",
    resources: { en: { common: en }, "zh-CN": { common: zhCN } },
    defaultNS: "common",
    interpolation: { escapeValue: false },
  });
});

describe("onlineQualityFoot (WB-002)", () => {
  it("pluralises both counts in English", () => {
    const t = i18n.getFixedT("en");
    expect(onlineQualityFoot(t, 1, 1)).toBe("1 session · 1 agent");
    expect(onlineQualityFoot(t, 4, 2)).toBe("4 sessions · 2 agents");
    expect(onlineQualityFoot(t, 4, 1)).toBe("4 sessions · 1 agent");
    expect(onlineQualityFoot(t, 0, 0)).toBe("0 sessions · 0 agents");
  });

  it("keeps the single zh-CN form for every count", () => {
    const t = i18n.getFixedT("zh-CN");
    expect(onlineQualityFoot(t, 1, 1)).toBe("1 个会话 · 1 个 agent");
    expect(onlineQualityFoot(t, 4, 2)).toBe("4 个会话 · 2 个 agent");
  });
});
