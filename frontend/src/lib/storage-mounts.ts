/**
 * BYO mounts (S3 Files / EFS) in the wizard — design storage-mounts §3 (S1).
 *
 * Every method mounts them now (`backend/app/schemas/agent.py` `BYO_MOUNT_METHODS`),
 * always inside the agent's VPC network profile. Until the storage catalog exists a
 * mount is an inline access point ARN, which only an administrator may add or change
 * (D-2; the backend answers `storage.inline_not_allowed` otherwise): a member sees the
 * rows an administrator set, read-only, and may still re-publish with them.
 */
import type { AgentMethod } from "./agent-spec";
import type { StorageKind } from "./api/storage";
import { runtimeConfigSupported } from "./runtime-config";

/** The wizard's methods plus Studio (published from the canvas, not the wizard). */
export type MountMethod = AgentMethod | "studio";

/** Methods whose spec accepts BYO mounts — kept equal to the backend set. */
export const BYO_MOUNT_METHODS: readonly MountMethod[] = ["harness", "zip_runtime", "studio", "container", "byoc"];

export const byoMethod = (method: MountMethod) => BYO_MOUNT_METHODS.includes(method);

/** What the storage card shows for the BYO rows. */
export type ByoRowsMode = "hidden" | "edit" | "readonly";

/** Hidden where the method mounts none; editable for an administrator; otherwise
 *  read-only, and only when the agent already has some (nothing to show a member). */
export function byoRowsMode(method: MountMethod, isAdmin: boolean, hasRows: boolean): ByoRowsMode {
  if (!byoMethod(method)) return "hidden";
  if (isAdmin) return "edit";
  return hasRows ? "readonly" : "hidden";
}

/**
 * The network an NFS-backed (S3 Files / EFS) mount needs (§3.2): a network profile. An S3
 * bucket mount needs none, so callers pass `hasByo` for NFS mounts only. Only a container agent may
 * still carry the legacy inline subnets / security groups instead (they predate
 * profiles); every other method is told to pick a profile in the network step.
 */
export function mountNeedsProfile(form: {
  method: MountMethod;
  networkProfileId: string;
  hasByo: boolean;
}): boolean {
  return form.hasByo && !form.networkProfileId && form.method !== "container";
}

/** Why an S3 bucket mount cannot go on this agent, as the i18n key suffix of
 *  `v3.storage.picker.*`: the managed Harness drops the member (probe 2026-11-23), and a
 *  Runtime needs platform V2 — an agent whose settings are unset ("keep existing") may
 *  be on V1, and the server refuses unless V2 is chosen explicitly. Null for any other
 *  kind of storage, or when nothing stands in the way. */
export function bucketMountIssue(
  kind: StorageKind | undefined,
  method: MountMethod,
  runtimeConfig: { platform_version?: string } | null | undefined,
): "noHarness" | "needsV2" | null {
  if (kind !== "s3bucket") return null;
  if (method === "harness") return "noHarness";
  if (runtimeConfigSupported(method) && runtimeConfig?.platform_version !== "V2") return "needsV2";
  return null;
}
