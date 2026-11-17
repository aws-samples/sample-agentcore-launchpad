import type { RuntimeConfigInput } from "./api";

// Agent Runtime intent, separate from published runtime versions and worker defaults.
export const RUNTIME_MEMORY_BASELINES = [1024, 2048, 4096, 8192] as const;

export const runtimeConfigSupported = (method: string): boolean =>
  ["zip_runtime", "studio", "container", "byoc"].includes(method);

/** Fresh intent only. An old stored spec without this field stays unconfigured. */
export const defaultRuntimeConfig = (): RuntimeConfigInput => ({
  platform_version: "V2",
  billing_model: "consumption",
  minimum_memory_mib: null,
});

export function runtimeConfigValid(config: RuntimeConfigInput | null | undefined): boolean {
  if (config == null) return true;
  if (config.platform_version !== "V1" && config.platform_version !== "V2") return false;
  if (config.billing_model === "consumption") return config.minimum_memory_mib == null;
  return config.billing_model === "committed_baseline" &&
    config.platform_version === "V2" &&
    RUNTIME_MEMORY_BASELINES.some((size) => size === config.minimum_memory_mib);
}

/** V1 cannot carry a committed baseline; switching platforms clears stale intent. */
export function withRuntimePlatform(
  config: RuntimeConfigInput | null | undefined,
  platform: RuntimeConfigInput["platform_version"],
): RuntimeConfigInput {
  const current = config ?? defaultRuntimeConfig();
  return platform === "V1"
    ? { platform_version: platform, billing_model: "consumption", minimum_memory_mib: null }
    : { ...current, platform_version: platform };
}
