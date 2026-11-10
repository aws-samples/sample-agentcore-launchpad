import { api, type GatewayPolicyEngine } from "../../../lib/api";
import { governanceError } from "../../../lib/governance";

/** The 移除 confirmation body + action, shared by both entry points. */
export async function detachEngine(
  gatewayId: string,
  t: (key: string, opts?: Record<string, unknown>) => string,
  toast: (tone: "success" | "error", text: string) => void,
  gatewayName: string,
): Promise<GatewayPolicyEngine | null> {
  try {
    const next = await api.detachPolicyEngine(gatewayId);
    toast("success", t("v2.policy.attach.detached", { gateway: gatewayName }));
    return next;
  } catch (err) {
    toast("error", governanceError(err));
    return null;
  }
}
