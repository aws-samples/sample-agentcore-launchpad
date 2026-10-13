// What on the gateway inventory needs someone, ranked (ported from the V3 console). Pure:
// the gateway list turns each entry into a row and owns the copy and the navigation.
import type { GovernanceGatewaySummary } from "../../../lib/api";
import { governanceStatusLevel } from "../../../lib/governance";
import { rankBySignal, type Signal } from "../../signals";
import type { GatewaySection } from "./common";

export type GatewayAttentionKind = "notReady" | "engineMissing" | "logOnly" | "targets";

export interface GatewayEntry {
  key: string;
  kind: GatewayAttentionKind;
  signal: Signal;
  gatewayId: string;
  /** the gateway detail section that handles it; none = overview */
  section?: Extract<GatewaySection, "policies" | "decisions" | "targets">;
  /** values the copy interpolates */
  values: Record<string, string | number>;
}

/** An AWS / governance status as a Signal. ENFORCE is a state, not a verdict (as `govTone`). */
export function govSignal(status: string | null | undefined): Signal {
  if (status?.toUpperCase() === "ENFORCE") return "info";
  const level = governanceStatusLevel(status);
  return level === "good" ? "ok" : level === "crit" ? "act" : level === "warn" ? "wait" : "off";
}

const needsLook = (s: Signal) => s === "act" || s === "wait";

export function gatewayAttention(gateways: GovernanceGatewaySummary[]): GatewayEntry[] {
  const out: GatewayEntry[] = [];
  for (const g of gateways) {
    const status = govSignal(g.status);
    if (needsLook(status)) {
      out.push({
        key: `${g.id}:notReady`,
        kind: "notReady",
        signal: status,
        gatewayId: g.id,
        values: { name: g.name, detail: g.status_reasons.join("; ") || g.status },
      });
    }
    // a deleted engine outranks its (stale) mode: only one engine row per gateway
    if (g.policy_engine?.missing) {
      out.push({
        key: `${g.id}:engineMissing`,
        kind: "engineMissing",
        signal: "act",
        gatewayId: g.id,
        section: "policies",
        values: { name: g.name, engine: g.policy_engine.id },
      });
    } else if (g.policy_engine?.mode === "LOG_ONLY") {
      out.push({
        key: `${g.id}:logOnly`,
        kind: "logOnly",
        signal: "wait",
        gatewayId: g.id,
        section: "decisions",
        values: { name: g.name },
      });
    }
    const bad = g.targets.filter((target) => needsLook(govSignal(target.status)));
    if (bad.length) {
      out.push({
        key: `${g.id}:targets`,
        kind: "targets",
        signal: bad.some((target) => govSignal(target.status) === "act") ? "act" : "wait",
        gatewayId: g.id,
        section: "targets",
        values: {
          name: g.name,
          count: bad.length,
          names: bad.map((target) => `${target.name} · ${target.status}`).join("  ·  "),
        },
      });
    }
  }
  return rankBySignal(out);
}
