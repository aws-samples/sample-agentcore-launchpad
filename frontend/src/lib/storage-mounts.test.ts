import { describe, expect, it } from "vitest";

import en from "../locales/en/common.json";
import zh from "../locales/zh-CN/common.json";
import { BYO_MOUNT_METHODS, bucketMountIssue, byoMethod, byoRowsMode, mountNeedsProfile } from "./storage-mounts";

describe("BYO mount methods (storage-mounts §3)", () => {
  it("lists every method, as the backend does", () => {
    expect([...BYO_MOUNT_METHODS].sort()).toEqual(["byoc", "container", "harness", "studio", "zip_runtime"]);
    expect(byoMethod("harness")).toBe(true);
    expect(byoMethod("byoc")).toBe(true);
  });
});

describe("byoRowsMode (D-2: inline mounts are admin-only)", () => {
  it("an administrator edits", () => {
    expect(byoRowsMode("zip_runtime", true, false)).toBe("edit");
    expect(byoRowsMode("harness", true, true)).toBe("edit");
  });

  it("a member sees an administrator's rows read-only, and none otherwise", () => {
    expect(byoRowsMode("container", false, true)).toBe("readonly");
    expect(byoRowsMode("container", false, false)).toBe("hidden");
  });
});

describe("mountNeedsProfile", () => {
  it("asks for a profile when a non-container agent mounts without one", () => {
    expect(mountNeedsProfile({ method: "harness", networkProfileId: "", hasByo: true })).toBe(true);
    expect(mountNeedsProfile({ method: "byoc", networkProfileId: "np-1", hasByo: true })).toBe(false);
    expect(mountNeedsProfile({ method: "zip_runtime", networkProfileId: "", hasByo: false })).toBe(false);
  });

  it("leaves a container's legacy inline network to the storage card", () => {
    expect(mountNeedsProfile({ method: "container", networkProfileId: "", hasByo: true })).toBe(false);
  });
});

describe("storage.mount locales", () => {
  const keys = (o: object, prefix = ""): string[] =>
    Object.entries(o).flatMap(([k, v]) =>
      typeof v === "object" && v !== null ? keys(v as object, `${prefix}${k}.`) : [`${prefix}${k}`],
    );

  it("has the same keys in both languages", () => {
    expect(keys(zh.storage.mount).sort()).toEqual(keys(en.storage.mount).sort());
  });

  it("uses full-width punctuation in zh-CN", () => {
    for (const text of keys(zh.storage.mount).map((k) =>
      k.split(".").reduce<unknown>((o, part) => (o as Record<string, unknown>)[part], zh.storage.mount),
    )) {
      expect(String(text)).not.toMatch(/[一-鿿][,;:?!]|[,;:?!]\s*[一-鿿]/);
    }
  });
});

describe("bucketMountIssue (an S3 bucket mount needs a Runtime on platform V2)", () => {
  const v2 = { platform_version: "V2" };
  it("names nothing for any other kind of storage", () => {
    expect(bucketMountIssue("s3files", "harness", null)).toBeNull();
    expect(bucketMountIssue(undefined, "zip_runtime", null)).toBeNull();
  });

  it("refuses the managed Harness, whatever its settings", () => {
    expect(bucketMountIssue("s3bucket", "harness", v2)).toBe("noHarness");
  });

  it("asks for V2 until it is chosen explicitly (V1, or the settings left unset)", () => {
    expect(bucketMountIssue("s3bucket", "zip_runtime", { platform_version: "V1" })).toBe("needsV2");
    expect(bucketMountIssue("s3bucket", "container", null)).toBe("needsV2");
    expect(bucketMountIssue("s3bucket", "byoc", undefined)).toBe("needsV2");
    expect(bucketMountIssue("s3bucket", "zip_runtime", v2)).toBeNull();
  });
});

describe("the picker's V2 and Harness copy", () => {
  it("exists in both locales", () => {
    for (const locale of [en, zh]) {
      const picker = (locale as { v3: { storage: { picker: Record<string, string> } } }).v3.storage.picker;
      expect(picker.needsV2).toBeTruthy();
      expect(picker.noHarness).toBeTruthy();
    }
  });
});
