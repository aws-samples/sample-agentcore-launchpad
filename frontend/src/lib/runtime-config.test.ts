import { describe, expect, it } from "vitest";

import { defaultRuntimeConfig, RUNTIME_MEMORY_BASELINES, runtimeConfigSupported, runtimeConfigValid, withRuntimePlatform } from "./runtime-config";

describe("Runtime intent", () => {
  it("defaults fresh intent to V2/consumption and never shares a mutable object", () => {
    expect(defaultRuntimeConfig()).toEqual({ platform_version: "V2", billing_model: "consumption", minimum_memory_mib: null });
    expect(defaultRuntimeConfig()).not.toBe(defaultRuntimeConfig());
  });

  it.each(RUNTIME_MEMORY_BASELINES)("accepts the %s MiB V2 baseline", (minimum_memory_mib) => {
    expect(runtimeConfigValid({ platform_version: "V2", billing_model: "committed_baseline", minimum_memory_mib })).toBe(true);
  });

  it.each([null, undefined, 512, 3072, 4096.5, Number.NaN])("rejects an absent or unsupported baseline: %s", (minimum_memory_mib) => {
    expect(runtimeConfigValid({ platform_version: "V2", billing_model: "committed_baseline", minimum_memory_mib })).toBe(false);
  });

  it("preserves unknown legacy intent and rejects mismatched billing configurations", () => {
    expect(runtimeConfigValid(null)).toBe(true);
    expect(runtimeConfigValid(undefined)).toBe(true);
    expect(runtimeConfigValid({ platform_version: "V1", billing_model: "committed_baseline", minimum_memory_mib: 4096 })).toBe(false);
    expect(runtimeConfigValid({ ...defaultRuntimeConfig(), minimum_memory_mib: 4096 })).toBe(false);
  });

  it("clears a committed baseline when changing to V1 and does not resurrect it on V2", () => {
    const baseline = { platform_version: "V2" as const, billing_model: "committed_baseline" as const, minimum_memory_mib: 4096 };
    expect(withRuntimePlatform(baseline, "V2")).toEqual(baseline);
    const v1 = withRuntimePlatform(baseline, "V1");
    expect(v1).toEqual({ platform_version: "V1", billing_model: "consumption", minimum_memory_mib: null });
    expect(withRuntimePlatform(v1, "V2")).toEqual(defaultRuntimeConfig());
  });

  it("offers controls only for Runtime-backed methods", () => {
    for (const method of ["zip_runtime", "studio", "container", "byoc"]) expect(runtimeConfigSupported(method)).toBe(true);
    for (const method of ["harness", "discovered_runtime", "coding"]) expect(runtimeConfigSupported(method)).toBe(false);
  });
});
