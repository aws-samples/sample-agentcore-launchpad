import { useState } from "react";
import { useTranslation } from "react-i18next";

import { type AgentForm, hasNfsMounts } from "../../../lib/agent-spec";
import { type NetworkChoice, networkChoices, RUNTIME_NETWORK_PURPOSES } from "../../../lib/api/agent-network";
import { useNetworkProfiles } from "../../../lib/useNetworkProfiles";
import { Alert, Card, Field, Segmented, Select, Spin, Tag, type TagTone } from "../../ui";
import type { SectionProps } from "./wizardKit";

type Mode = "PUBLIC" | "VPC";

const STATUS_TONE: Record<string, TagTone> = { ok: "green", warn: "orange", blocked: "red", unchecked: "gray" };

const initialMode = (form: AgentForm): Mode => (form.networkProfileId || form.networkVpc ? "VPC" : "PUBLIC");

/**
 * The wizard's network step (projects §12.6), for every method: PUBLIC (AgentCore's
 * managed network) or a VPC network profile of the project's allowed set. A blocked
 * profile cannot be chosen; a warn / unchecked one can, with its warning. The deploy
 * re-runs the profile's preflight for this agent and refuses before any job starts
 * (`network.preflight_failed`), as it refuses PUBLIC in a workspace that forbids it.
 */
export function NetworkSection({ form, set }: Omit<SectionProps, "cat" | "err">) {
  const { t } = useTranslation();
  const { profiles, error } = useNetworkProfiles();
  const [mode, setMode] = useState<Mode>(() => initialMode(form));
  // a profile set from outside (the wizard preselects the project's default once the
  // list lands) shows the VPC side, as a pick here would
  if (mode === "PUBLIC" && form.networkProfileId) setMode("VPC");
  const choices: NetworkChoice[] = profiles ? networkChoices(profiles, form.networkProfileId, RUNTIME_NETWORK_PURPOSES) : [];
  const current = choices.find((c) => c.profile.id === form.networkProfileId) ?? null;
  // a container's legacy inline ids (BYO mounts, no profile) stay in the storage card
  const inline = form.method === "container" && !form.networkProfileId && hasNfsMounts(form);

  const choose = (next: Mode) => {
    setMode(next);
    if (next === "PUBLIC") {
      set({ networkProfileId: "", networkVpc: false });
      return;
    }
    const pick = choices.find((c) => c.profile.is_default && !c.disabled) ?? choices.find((c) => !c.disabled);
    set({ networkProfileId: form.networkProfileId || pick?.profile.id || "", networkVpc: true });
  };

  return (
    <Card title={t("network.title")} sub={t("network.sub")} testId="v2-agent-network">
      <div className="v2-form">
        <Segmented<Mode>
          value={mode}
          ariaLabel={t("network.title")}
          options={[
            { value: "PUBLIC", label: t("network.public") },
            { value: "VPC", label: t("network.vpc") },
          ]}
          onChange={choose}
        />
        {mode === "PUBLIC" && (
          <div className="v2-muted" data-testid="v2-agent-network-public">
            {t(inline ? "network.inlineNote" : "network.publicNote")}
          </div>
        )}
        {mode === "VPC" && profiles === null && <Spin />}
        {mode === "VPC" && error && <Alert tone="error">{error}</Alert>}
        {mode === "VPC" && profiles !== null && choices.length === 0 && !error && (
          <Alert tone="warn">{t("network.none")}</Alert>
        )}
        {mode === "VPC" && choices.length > 0 && (
          <Field
            label={t("network.profile")}
            required
            error={form.networkVpc && !form.networkProfileId ? t("network.errPick") : undefined}
          >
            <Select
              value={form.networkProfileId}
              placeholder={t("network.pick")}
              ariaLabel={t("network.profile")}
              testId="v2-agent-network-profile"
              options={choices.map((c) => ({
                value: c.profile.id,
                label: `${c.profile.name}${c.profile.is_default ? ` · ${t("network.default")}` : ""} · ${t(`network.status.${c.profile.status}`, { defaultValue: c.profile.status })}`,
                disabled: c.disabled,
              }))}
              onChange={(id) => set({ networkProfileId: id, networkVpc: true })}
            />
          </Field>
        )}
        {mode === "VPC" && form.networkProfileId && !current && profiles !== null && (
          // the stored profile is outside this project's list (or gone): shown, not editable
          <div className="v2-muted" data-testid="v2-agent-network-stored">
            {t("network.stored", { id: form.networkProfileId })}
          </div>
        )}
        {mode === "VPC" && current && (
          <div className="v2-form" data-testid="v2-agent-network-detail">
            <div>
              <Tag tone={STATUS_TONE[current.profile.status] ?? "gray"} dot>
                {t(`network.status.${current.profile.status}`, { defaultValue: current.profile.status })}
              </Tag>{" "}
              {current.profile.checked_at && (
                <span className="v2-muted">{t("network.checkedAt", { at: current.profile.checked_at })}</span>
              )}
            </div>
            <div className="v2-muted mono">
              {t("network.subnets", { ids: current.profile.subnet_ids.join(", ") })}
              <br />
              {t("network.groups", { ids: current.profile.security_group_ids.join(", ") })}
            </div>
            {current.profile.status === "blocked" && <Alert tone="error">{t("network.blocked")}</Alert>}
            {current.caution && (
              <Alert tone="warn">
                {t(current.profile.status === "unchecked" ? "network.unchecked" : "network.warn")}
              </Alert>
            )}
          </div>
        )}
        {mode === "VPC" && <Alert>{t("network.preflightNote")}</Alert>}
        <div className="v2-muted">{t("network.republishNote")}</div>
      </div>
    </Card>
  );
}
