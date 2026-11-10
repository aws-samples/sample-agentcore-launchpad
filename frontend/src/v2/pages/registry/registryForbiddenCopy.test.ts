/**
 * N17-obs269 (rev-r23 L1): `registry.forbidden` is also the Skill Lab publish refusal
 * (N17-obs263 — publish rewrites the SOURCE record), so the copy names publish next to edit,
 * reimport and delete, in both languages.
 */
import { describe, expect, it } from "vitest";
import en from "../../../locales/en/common.json";
import zh from "../../../locales/zh-CN/common.json";

describe("registry.forbidden copy", () => {
  it("names publish among the creator-or-admin actions (en + zh-CN)", () => {
    expect(en.apiErrors["registry.forbidden"]).toBe(
      "Only the record's creator or an administrator may edit, reimport, publish or delete it.",
    );
    expect(zh.apiErrors["registry.forbidden"]).toBe(
      "只有该记录的创建者或管理员才能编辑、重新导入、发布或删除它。",
    );
  });
});
