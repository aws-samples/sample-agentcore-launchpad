import { useTranslation } from "react-i18next";

import {
  RUNTIME_MEMORY_BASELINES,
  runtimeConfigSupported,
  runtimeConfigValid,
  withRuntimePlatform,
} from "../../../lib/runtime-config";
import { Alert, Card, Field, Select } from "../../ui";
import type { SectionProps } from "./wizardKit";

/** Runtime intent only; a legacy edit can leave its live settings unchanged. */
export function RuntimeConfigCard({
  form, set, touched, preserve = false,
}: Pick<SectionProps, "form" | "set"> & { touched: boolean; preserve?: boolean }) {
  const { t } = useTranslation();
  if (!runtimeConfigSupported(form.method)) return null;
  const config = form.runtimeConfig;
  const keep = t("v2.agents.wizard.runtime.keep");
  return (
    <Card title={t("v2.agents.wizard.runtime.title")} testId="v2-agent-runtime-config">
      {preserve && <Alert>{t("v2.agents.wizard.runtime.preserveHint")}</Alert>}
      <div className="v2-form cols-2">
        <Field label={t("v2.agents.wizard.runtime.platform")} required hint={t("v2.agents.wizard.runtime.platformHint")}>
          <Select
            ariaLabel={t("v2.agents.wizard.runtime.platform")}
            value={config?.platform_version ?? ""}
            placeholder={preserve ? keep : undefined}
            options={[
              { value: "V2", label: t("v2.agents.wizard.runtime.v2") },
              { value: "V1", label: t("v2.agents.wizard.runtime.v1") },
            ]}
            onChange={(value) => set({
              runtimeConfig: value ? withRuntimePlatform(config, value === "V1" ? "V1" : "V2") : null,
            })}
            testId="v2-agent-platform-version"
          />
        </Field>
        <Field label={t("v2.agents.wizard.runtime.billing")} required hint={t("v2.agents.wizard.runtime.consumptionHint")}>
          <Select
            ariaLabel={t("v2.agents.wizard.runtime.billing")}
            value={config?.billing_model ?? ""}
            disabled={!config}
            placeholder={!config ? keep : undefined}
            options={[
              { value: "consumption", label: t("v2.agents.wizard.runtime.consumption") },
              { value: "committed_baseline", label: t("v2.agents.wizard.runtime.committed"), disabled: config?.platform_version !== "V2" },
            ]}
            onChange={(value) => config && value !== config.billing_model && set({ runtimeConfig: {
              ...config,
              billing_model: value === "consumption" ? "consumption" : "committed_baseline",
              minimum_memory_mib: null,
            } })}
            testId="v2-agent-billing-model"
          />
        </Field>
        {config?.billing_model === "committed_baseline" && (
          <Field
            label={t("v2.agents.wizard.runtime.baseline")}
            required full
            hint={t("v2.agents.wizard.runtime.baselineHint")}
            error={touched && !runtimeConfigValid(config) ? t("v2.agents.wizard.runtime.errBaseline") : undefined}
          >
            <Select
              ariaLabel={t("v2.agents.wizard.runtime.baseline")}
              value={config.minimum_memory_mib == null ? "" : String(config.minimum_memory_mib)}
              placeholder={t("v2.agents.wizard.runtime.chooseBaseline")}
              options={RUNTIME_MEMORY_BASELINES.map((size) => ({
                value: String(size),
                label: t("v2.agents.wizard.runtime.baselineOption", { memory: size / 1024, cpu: size / 4096 }),
              }))}
              onChange={(value) => set({ runtimeConfig: { ...config, minimum_memory_mib: value ? Number(value) : null } })}
              testId="v2-agent-runtime-baseline"
            />
          </Field>
        )}
      </div>
      {config?.platform_version === "V2" && <Alert>{t("v2.agents.wizard.runtime.v2Hint")}</Alert>}
      <a href="https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/runtime-how-it-works.html#runtime-platform-versions" target="_blank" rel="noreferrer">
        {t("v2.agents.wizard.runtime.docs")}
      </a>
    </Card>
  );
}
