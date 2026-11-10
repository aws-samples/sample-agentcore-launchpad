import { describe, expect, it } from "vitest";

import { ApiError } from "../../../lib/api";
import { classicModelProvidersToV2 } from "./classicUrl";
import { baseUrlProblem, credentialStatus, defaultIdPrefix, isProbeUnavailable, referrersFromError } from "./common";

describe("model providers helpers", () => {
  it("baseUrlProblem mirrors the §9.3 client-side half: https + hostname only", () => {
    expect(baseUrlProblem("")).toBe("required");
    expect(baseUrlProblem("api.siliconflow.cn")).toBe("https");
    expect(baseUrlProblem("http://api.siliconflow.cn/v1")).toBe("https");
    expect(baseUrlProblem("https://169.254.169.254/latest")).toBe("host");
    expect(baseUrlProblem("https://localhost:8000")).toBe("host");
    expect(baseUrlProblem("https://api.siliconflow.cn/v1")).toBeNull();
  });

  it("derives the Launchpad id prefix per kind", () => {
    expect(defaultIdPrefix("bedrock", "bedrock")).toBe("");
    expect(defaultIdPrefix("siliconflow", "openai_compatible")).toBe("siliconflow.");
  });

  it("reads referrers from the 409 detail in its documented and tolerated shapes", () => {
    const documented = new ApiError("model.in_use", "x", { referrers: [{ kind: "agent", id: "a1", name: "A" }] });
    expect(referrersFromError(documented)).toEqual([{ kind: "agent", id: "a1", name: "A" }]);
    const bare = new ApiError("model.in_use", "x", [{ kind: "evaluator", id: "e1" }]);
    expect(referrersFromError(bare)).toEqual([{ kind: "evaluator", id: "e1", name: null }]);
    const grouped = new ApiError("model_provider.in_use", "x", { agents: ["a2"], evaluators: [{ id: "x1" }] });
    expect(referrersFromError(grouped)).toEqual([
      { kind: "agent", id: "a2" },
      { kind: "evaluator", id: "x1", name: null },
    ]);
    expect(referrersFromError(new Error("boom"))).toEqual([]);
  });

  it("recognises the pre-M1-b 501 by code or by bare status", () => {
    expect(isProbeUnavailable(new ApiError("model_providers.probe_unavailable", "x", null))).toBe(true);
    expect(isProbeUnavailable(new ApiError("http.501", "x", null))).toBe(true);
    expect(isProbeUnavailable(new ApiError("model_providers.credential_unavailable", "x", null))).toBe(true);
    expect(isProbeUnavailable(new ApiError("model.probe_failed", "x", null))).toBe(false);
  });

  it("credential status: host env for the built-in Bedrock key, configured / missing for SSM", () => {
    const base = { credential_source: "ssm" as const, configured: false };
    expect(credentialStatus({ ...base, credential_source: "host_env" } as never)).toBe("host_env");
    expect(credentialStatus({ ...base, configured: true } as never)).toBe("configured");
    expect(credentialStatus(base as never)).toBe("missing");
  });

  it("maps the classic URL onto the V2 page keeping only view and provider", () => {
    expect(classicModelProvidersToV2("")).toBe("/v2/settings/model-providers");
    expect(classicModelProvidersToV2("?view=changes&provider=mp_x&foo=1")).toBe("/v2/settings/model-providers?view=changes&provider=mp_x");
  });
});
