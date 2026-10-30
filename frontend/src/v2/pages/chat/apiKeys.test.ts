import i18next, { type TFunction } from "i18next";
import { beforeAll, describe, expect, it } from "vitest";

import en from "../../../locales/en/common.json";
import zhCN from "../../../locales/zh-CN/common.json";
import type { ApiKeyInfo } from "../../../lib/api";
import { apiKeyMeta, apiKeyOffboardedNote } from "./apiKeys";

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

const base: ApiKeyInfo = { id: "k1", name: "console-1", prefix: "lp_live_ab12…", enabled: true };

describe("api key lifecycle copy", () => {
  it("names the creator and when the key was last used", () => {
    const meta = apiKeyMeta(tEn, { ...base, created_by_name: "ada", last_used_at: "2026-10-11T09:30:00Z" });
    expect(meta).toMatch(/^Created by ada · last used 2026-10-1\d \d\d:\d\d:00$/);
    expect(apiKeyMeta(tZh, { ...base, created_by_name: "ada" })).toBe("创建者：ada · 从未使用");
  });

  it("reads a legacy key's creator as unknown", () => {
    expect(apiKeyMeta(tEn, { ...base, created_by_name: null })).toBe("Created by unknown · never used");
    expect(apiKeyMeta(tZh, base)).toBe("创建者：未知 · 从未使用");
  });

  it("explains an offboarded creator, and is silent otherwise", () => {
    expect(apiKeyOffboardedNote(tEn, base)).toBeNull();
    for (const reason of ["deleted", "disabled", "expired", "no_workspace_grant"] as const) {
      const note = apiKeyOffboardedNote(tEn, { ...base, creator_offboarded: reason });
      expect(note).toContain("Mint a new key");
      expect(apiKeyOffboardedNote(tZh, { ...base, creator_offboarded: reason })).toContain("请新建密钥");
    }
  });

  it("translates the re-enable refusal", () => {
    expect(tEn("apiErrors.apikey.creator_offboarded")).toContain("cannot be re-enabled");
    expect(tZh("apiErrors.apikey.creator_offboarded")).toContain("无法重新启用此密钥");
  });
});
