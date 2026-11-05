import { useTranslation } from "react-i18next";

import type { StorageProvisionPlan } from "../../../lib/api/storage";
import { Chip, Notice } from "../../ui";

const GROUP_SIGNAL = { ok: "ok", missing: "act", not_found: "act" } as const;

/**
 * What a create would add to the network profile's VPC (storage-mounts §7), shown in
 * the create dialog before the administrator confirms: the new mount-target security
 * group with its VPC and its inbound / outbound rules, one mount target per
 * Availability Zone, the profile's own (agent) security groups — which the console
 * never edits — with the outbound rule each needs, and the S3 gateway endpoint that
 * keeps large reads on the private network at no charge.
 */
export function ProvisionPlan({ plan, storageId }: { plan: StorageProvisionPlan; storageId: string }) {
  const { t } = useTranslation();
  // the plan is read once per profile; the group's name follows the id as it is typed
  const groupName = `launchpad-mt-${storageId || "<id>"}`;
  const group = plan.mount_target_security_group;
  const vpc = `${plan.vpc_id ?? "?"}${plan.vpc_cidrs.length ? ` (${plan.vpc_cidrs.join(", ")})` : ""}`;
  const missing = plan.agent_security_groups.filter((g) => g.status !== "ok");
  const gateway = plan.s3_gateway_endpoint;
  return (
    <div style={{ display: "grid", gap: 10 }} data-testid="v3-storage-provision-plan">
      <b>{t("v3.storage.provision.plan.title")}</b>

      <div style={{ display: "grid", gap: 4 }}>
        <span>
          {t("v3.storage.provision.plan.newGroup")} <b className="mono">{groupName}</b>
          {" · "}{t("v3.storage.provision.plan.vpc")} <span className="mono">{vpc}</span>
        </span>
        <table className="v3-table" data-testid="v3-storage-plan-rules">
          <thead>
            <tr>
              <th>{t("v3.storage.provision.plan.direction")}</th>
              <th>{t("v3.storage.provision.plan.protocol")}</th>
              <th>{t("v3.storage.provision.plan.port")}</th>
              <th>{t("v3.storage.provision.plan.peer")}</th>
            </tr>
          </thead>
          <tbody>
            {group.inbound.map((r) => (
              <tr key={`in-${r.source_group_id}`}>
                <td>{t("v3.storage.provision.plan.inbound")}</td>
                <td className="mono">{r.protocol.toUpperCase()}</td>
                <td className="mono">{r.port}</td>
                <td className="mono">{r.source_group_id}</td>
              </tr>
            ))}
            {group.outbound.length === 0 && (
              <tr>
                <td>{t("v3.storage.provision.plan.outbound")}</td>
                <td colSpan={3}>{t("v3.storage.provision.plan.noOutbound")}</td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      <div style={{ display: "grid", gap: 4 }}>
        <span>{t("v3.storage.provision.plan.mountTargets")}</span>
        <ul style={{ margin: 0, paddingLeft: 18 }}>
          {plan.mount_targets.map((m) => (
            <li key={m.subnet_id} className="mono">{m.subnet_id} ({m.availability_zone_id})</li>
          ))}
        </ul>
      </div>

      <div style={{ display: "grid", gap: 4 }}>
        <span>{t("v3.storage.provision.plan.agentGroups")}</span>
        <table className="v3-table" data-testid="v3-storage-plan-agent-groups">
          <thead>
            <tr>
              <th>{t("v3.storage.provision.plan.group")}</th>
              <th>{t("v3.storage.provision.plan.needs")}</th>
              <th>{t("v3.storage.provision.plan.state")}</th>
            </tr>
          </thead>
          <tbody>
            {plan.agent_security_groups.map((g) => (
              <tr key={g.group_id}>
                <td className="mono">{g.group_id}{g.name ? ` (${g.name})` : ""}</td>
                <td className="mono">
                  {t("v3.storage.provision.plan.outbound")} {g.required_outbound.protocol.toUpperCase()} {g.required_outbound.port} → {groupName}
                </td>
                <td><Chip s={GROUP_SIGNAL[g.status]}>{t(`v3.storage.provision.plan.groupState.${g.status}`)}</Chip></td>
              </tr>
            ))}
          </tbody>
        </table>
        {missing.length > 0 && (
          <Notice s="act">
            {t("v3.storage.provision.plan.addOutbound", {
              groups: missing.map((g) => g.group_id).join(", "),
              group: groupName,
              cidr: plan.vpc_cidrs.join(", ") || plan.vpc_id || "",
            })}
          </Notice>
        )}
      </div>

      {gateway.status === "ok" ? (
        <Notice s="ok">
          {t("v3.storage.provision.plan.s3GatewayOk", { ids: gateway.endpoint_ids.join(", ") })}
        </Notice>
      ) : (
        <Notice s="info">
          <span data-testid="v3-storage-plan-s3-gateway">
            {t(gateway.status === "missing" ? "v3.storage.provision.plan.s3GatewayMissing"
              : "v3.storage.provision.plan.s3GatewayUnrouted", {
              service: gateway.service_name,
              subnets: gateway.unrouted_subnets.join(", "),
            })}{" "}
            <a href={gateway.docs} target="_blank" rel="noreferrer">{t("v3.storage.provision.plan.s3GatewayDocs")}</a>
          </span>
        </Notice>
      )}
    </div>
  );
}
