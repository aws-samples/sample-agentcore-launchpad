import { beforeAll, describe, expect, it, vi } from "vitest";

import i18n from "../i18n";
import { lambdaArnPlaceholder, targetKindLabel } from "./api/mcpReviews";
import { scoresUninformative, sourceReady } from "./knowledgeBases";
import { deletedMessage } from "./registry";

// Prod QA 2026-11-02 (QA-3): registry / knowledge base / gateway governance fixes.

beforeAll(async () => {
  vi.stubGlobal("document", { documentElement: {} });
  await i18n.changeLanguage("en");
});

describe("deletedMessage (R7)", () => {
  const t = i18n.t.bind(i18n) as unknown as Parameters<typeof deletedMessage>[0];

  it("is the plain toast when the files went too, or the record is no Skill", () => {
    expect(deletedMessage(t, "probe", { deleted: true, record_id: "r", files: { status: "deleted" } })).toBe("“probe” deleted.");
    expect(deletedMessage(t, "probe", { deleted: true, record_id: "r", files: null })).toBe("“probe” deleted.");
    expect(deletedMessage(t, "probe", undefined)).toBe("“probe” deleted.");
  });

  it("names the agents that kept the skill files", () => {
    const msg = deletedMessage(t, "probe", {
      deleted: true, record_id: "r", files: { status: "kept_in_use", agents: ["a1", "a2"] },
    });
    expect(msg).toContain("a1, a2");
    expect(msg).toContain("kept");
  });

  it("names a Harness of another hub on the account that kept them", () => {
    const msg = deletedMessage(t, "probe", {
      deleted: true, record_id: "r", files: { status: "kept_in_use", agents: [], harnesses: ["prod2_hr"] },
    });
    expect(msg).toContain("prod2_hr");
  });

  it("says honestly why the files stayed when the Harness scan failed", () => {
    const msg = deletedMessage(t, "probe", {
      deleted: true, record_id: "r", files: { status: "kept_unverified", reason: "ThrottlingException: slow down" },
    });
    expect(msg).toContain("ThrottlingException: slow down");
    expect(msg).toContain("kept");
  });

  it("says when the files could not be removed", () => {
    const msg = deletedMessage(t, "probe", {
      deleted: true, record_id: "r", files: { status: "failed", prefix: "s3://bkt/skills/probe/" },
    });
    expect(msg).toContain("s3://bkt/skills/probe/");
  });
});

describe("knowledge base readiness and scores (K3/K4)", () => {
  it("lists documents only for an AVAILABLE data source", () => {
    expect(sourceReady("AVAILABLE")).toBe(true);
    expect(sourceReady("available")).toBe(true);
    for (const status of ["CREATING", "DELETING", "", null, undefined]) expect(sourceReady(status)).toBe(false);
  });

  it("flags a result set where every score is the degenerate 1.0", () => {
    expect(scoresUninformative([{ score: 1 }])).toBe(true);
    expect(scoresUninformative([{ score: 1 }, { score: 1 }, { score: null }])).toBe(true);
    expect(scoresUninformative([{ score: 1 }, { score: 0.61 }])).toBe(false);
    expect(scoresUninformative([{ score: 0.98 }])).toBe(false);
    expect(scoresUninformative([{ score: null }])).toBe(false);
    expect(scoresUninformative([])).toBe(false);
  });
});

describe("Lambda target helpers (G3)", () => {
  it("names the current workspace's account and region in the ARN example", () => {
    expect(lambdaArnPlaceholder({ account_id: "434444145045", region: "us-east-1" })).toBe(
      "arn:aws:lambda:us-east-1:434444145045:function:crm-tools",
    );
    expect(lambdaArnPlaceholder({ account_id: "1", region: "cn-north-1" })).toMatch(/^arn:aws-cn:lambda:cn-north-1:1:/);
    expect(lambdaArnPlaceholder({ account_id: "1", region: "us-gov-west-1" })).toMatch(/^arn:aws-us-gov:/);
    expect(lambdaArnPlaceholder(null)).toBe("arn:aws:lambda:<region>:<account-id>:function:crm-tools");
  });

  it("labels target kinds", () => {
    expect(targetKindLabel("lambda")).toBe("Lambda");
    expect(targetKindLabel("openApiSchema")).toBe("OpenAPI");
    expect(targetKindLabel("somethingNew")).toBe("somethingNew");
  });
});
