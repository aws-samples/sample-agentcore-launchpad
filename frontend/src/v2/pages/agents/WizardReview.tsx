import { useTranslation } from "react-i18next";

import {
  type AgentForm,
  byocEnv,
  byocModelList,
  hasByoMounts,
  pythonLabel,
  resolveKb,
  skillNameFromPath,
  toolkitToolNames,
} from "../../../lib/agent-spec";
import { Alert, Card, Descriptions } from "../../ui";
import type { WizardCatalogs, WizardUi } from "./wizardKit";

/** The review step: what will be deployed, per method (read-only). */
export function WizardReview({
  form,
  cat,
  ui,
  shortTermOff = false,
}: {
  form: AgentForm;
  cat: WizardCatalogs;
  ui: WizardUi;
  /** re-publish of an agent deployed without short-term memory (kept off on save) */
  shortTermOff?: boolean;
}) {
  const { t } = useTranslation();
  const none = t("v2.agents.wizard.none");
  const list = (items: string[]) => (items.length ? items.join(", ") : none);
  const method = form.method;
  const mono = (v: string) => <span className="mono">{v}</span>;

  const skillNames = form.skills.map(
    (p) => cat.skills.find((s) => s.path === p)?.name ?? ui.customSkills.find((c) => c.path === p)?.name ?? skillNameFromPath(p),
  );
  const kbNames = form.selectedKbs.map((id) => resolveKb(id, cat.kbCatalog, []).name);
  const memory = {
    label: t("v2.agents.wizard.memoryOnly"),
    value: shortTermOff
      ? form.longTerm
        ? `${t("v2.agents.wizard.memoryLongOnly")} · ${form.memoryId || t("v2.agents.wizard.memoryDefault")}`
        : t("v2.agents.wizard.memoryOff")
      : (form.longTerm ? t("v2.agents.wizard.memoryLong") : t("v2.agents.wizard.memoryShort")) +
        ` · ${form.memoryId || t("v2.agents.wizard.memoryDefault")}`,
  };
  const head = [
    { label: t("v2.agents.colName"), value: form.name },
    { label: t("v2.agents.colMethod"), value: t(`v2.agents.wizard.method.${method}`) },
  ];

  let items: { label: string; value: React.ReactNode }[];
  if (method === "byoc") {
    const models = byocModelList(form.byocModels);
    const env = Object.keys(byocEnv(form.byocEnvRows));
    const upload = ui.byocUpload;
    items = [
      ...head,
      { label: t("v2.agents.wizard.artifactTitle"), value: t(`create.configure.byocKindName.${form.byocKind}`) },
      form.byocKind === "container_image"
        ? { label: t("create.configure.byocImageUri"), value: mono(form.byocImageUri.trim()) }
        : {
            label: t("create.configure.byocUpload"),
            value: upload ? mono(`${upload.original_filename} · sha256 ${upload.sha256.slice(0, 12)}…`) : none,
          },
      ...(form.byocKind === "code_zip"
        ? [
            {
              label: t("v2.agents.wizard.entryPython"),
              value: mono(`${form.byocEntrypoint.trim() || "main.py"} · ${pythonLabel(form.byocPython)}`),
            },
            {
              label: t("v2.agents.wizard.byocBuild"),
              value: form.byocInstallReqs ? t("v2.agents.wizard.on") : t("v2.agents.wizard.off"),
            },
          ]
        : []),
      { label: t("v2.agents.wizard.byocContractKind"), value: mono(form.byocRawContract ? "raw" : "launchpad_prompt") },
      { label: t("v2.agents.wizard.modelSource"), value: t(`v2.agents.wizard.source.${form.modelSource}`) },
      {
        label: t("create.configure.byocModels"),
        value: models.length ? (
          <span className="mono">
            {models[0]}
            {models.length > 1 ? ` + ${models.length - 1}` : ""}
          </span>
        ) : (
          none
        ),
      },
      { label: t("create.configure.byocEnv"), value: env.length ? mono(env.join(", ")) : none },
      { label: t("create.configure.byocDescription"), value: form.byocDescription || none },
    ];
  } else {
    const model = [
      { label: t("v2.agents.model"), value: mono(form.modelId.trim()) },
      { label: t("v2.agents.wizard.modelSource"), value: t(`v2.agents.wizard.source.${form.modelSource}`) },
    ];
    const common = [
      { label: t("v2.agents.wizard.skills"), value: list(skillNames) },
      { label: t("v2.agents.kbTitle"), value: list(kbNames) },
      memory,
    ];
    if (method === "harness") {
      items = [
        ...head,
        ...model,
        {
          label: t("v2.agents.wizard.tools"),
          value: list([
            ...form.tools.map((n) => t(`v2.agents.wizard.builtin.${n}`)),
            ...form.selectedGateway,
            ...form.selectedMcp,
            ...form.nativeTools.map((n) => t(`v2.agents.wizard.native.${n}`)),
          ]),
        },
        ...common,
        {
          label: t("v2.agents.wizard.loop"),
          value: t("v2.agents.wizard.loopValue", { iterations: form.maxIterations, seconds: form.timeoutSeconds }),
        },
      ];
    } else if (method === "zip_runtime") {
      const a2a = form.protocol === "a2a";
      const kit = toolkitToolNames(form.toolkits);
      items = [
        ...head,
        { label: t("v2.agents.protocol"), value: t(`v2.agents.wizard.protocol.${form.protocol}`) },
        ...model,
        {
          label: t("v2.agents.wizard.tools"),
          value: list([...(kit.length ? kit : ["calculator", "current_utc_time"]), ...(a2a ? [] : form.selectedGateway)]),
        },
        ...(a2a
          ? [{ label: t("v2.agents.wizard.a2aSkills"), value: list(form.a2aSkills.filter((s) => s.name.trim()).map((s) => s.name.trim())) }]
          : [{ label: t("v2.agents.wizard.toolkits"), value: list(form.toolkits) }]),
        ...common,
      ];
    } else {
      items = [
        ...head,
        { label: t("v2.agents.wizard.sdkTitle"), value: t("create.configure.agentSdkClaude") },
        ...model,
        { label: t("v2.agents.wizard.remoteMcp"), value: list(form.selectedMcp) },
        { label: t("v2.agents.wizard.mcpJson"), value: form.mcpServers.trim() ? t("v2.agents.wizard.set") : none },
        ...common,
        {
          label: t("v2.agents.wizard.fsTitle"),
          value: t("v2.agents.wizard.fsSummary", {
            session: form.sessionFs ? form.sessionMount : none,
            s3: form.s3Mounts.length,
            efs: form.efsMounts.length,
          }),
        },
        ...(hasByoMounts(form)
          ? [{ label: "VPC", value: mono(`${form.vpcSubnets} · ${form.vpcSgs}`) }]
          : []),
      ];
    }
  }

  return (
    <Card title={t("v2.agents.wizard.reviewTitle")} sub={t(`v2.agents.wizard.reviewSubBy.${method}`)} testId="v2-agent-review">
      <Descriptions items={items} />
      {method !== "byoc" && (
        <>
          <h3 className="v2-sub-title">{t("v2.agents.wizard.systemPrompt")}</h3>
          <pre className="v2-pre">{form.systemPrompt}</pre>
        </>
      )}
      <div className="v2-agents-foot">
        <Alert>{t("v2.agents.wizard.deployNote")}</Alert>
      </div>
    </Card>
  );
}
