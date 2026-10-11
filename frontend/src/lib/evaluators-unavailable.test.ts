import i18next, { type TFunction } from "i18next";
import { beforeAll, describe, expect, it } from "vitest";

import en from "../locales/en/common.json";
import zhCN from "../locales/zh-CN/common.json";
import { customUnavailableNote } from "./evaluators";

let tEn: TFunction;
let tZh: TFunction;

beforeAll(async () => {
  const make = async (lng: string) => {
    const inst = i18next.createInstance();
    await inst.init({
      lng,
      resources: { en: { common: en }, "zh-CN": { common: zhCN } },
      defaultNS: "common",
      interpolation: { escapeValue: false },
    });
    return inst.t;
  };
  tEn = await make("en");
  tZh = await make("zh-CN");
});

describe("custom evaluators unavailable note", () => {
  it("renders nothing on success or while loading", () => {
    expect(customUnavailableNote(tEn, undefined, "banner")).toBeNull();
    expect(customUnavailableNote(tEn, null, "inline")).toBeNull();
  });

  it("names the mapped AWS cause in the console's copy", () => {
    const marker = { code: "aws.access_denied", message: "AWS access denied" };
    const banner = customUnavailableNote(tEn, marker, "banner");
    expect(banner).toContain("Custom evaluators could not be loaded: AWS denied access");
    expect(banner).toContain("built-in evaluators only");
    expect(banner).not.toContain("..");
    expect(customUnavailableNote(tEn, marker, "inline")).toMatch(/^Custom evaluators could not be loaded \(AWS denied/);
    expect(customUnavailableNote(tZh, marker, "banner")).toMatch(/^自定义评估器加载失败：AWS 拒绝访问.*内置评估器。$/);
    expect(customUnavailableNote(tZh, marker, "banner")).not.toContain("。。");
  });

  it("translates the generic listing failure and falls back to the backend sentence", () => {
    const failed = customUnavailableNote(tEn, { code: "evaluators.list_failed", message: "x" }, "inline");
    expect(failed).toContain("The account's evaluators could not be listed");
    const unknown = customUnavailableNote(tEn, { code: "nope.unknown", message: "listing broke" }, "banner");
    expect(unknown).toContain("loaded: listing broke. The list");
  });
});
