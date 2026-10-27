import { useTranslation } from "react-i18next";

import {
  type AgentForm,
  byocEnv,
  byocModelList,
  contextLayerSupported,
  contextLayerVia,
  hasByoMounts,
  managedGatewayAttached,
  pythonLabel,
  resolveKb,
  skillNameFromPath,
  toolkitToolNames,
} from "../../../lib/agent-spec";
import type { InboundAuth } from "../../../lib/api";
import { networkProfilesApi } from "../../../lib/api/projects";
import { mcpStdioArgs } from "../../../lib/mcp-stdio";
import { inboundCapable, issuerMismatch, jwtSummary, wizardDeployedJwt } from "../../../lib/inbound-auth";
import { useLoad } from "../../hooks";
import { Alert, Card, Descriptions } from "../../ui";
import { InlineKbState } from "./InlineKb";
import { type InlineKb, inlineKbBlock } from "./useInlineKb";
import { ReachabilityWarning } from "./InboundAuthFields";
import { strandsReviewItems, strandsReviewSpec } from "./strandsReview";
import type { WizardCatalogs, WizardUi } from "./wizardKit";

/** The review step: what will be deployed, per method (read-only). */
export function WizardReview({
  form,
  cat,
  ui,
  shortTermOff = false,
  inlineKb = null,
  workspaceDefault = null,
  cognitoIssuer = null,
  project = null,
}: {
  form: AgentForm;
  cat: WizardCatalogs;
  ui: WizardUi;
  /** a new agent's business project (`Department · Project`); null on a re-publish */
  project?: string | null;
  /** re-publish of an agent deployed without short-term memory (kept off on save) */
  shortTermOff?: boolean;
  /** the knowledge base created inside the wizard, when one is mounted */
  inlineKb?: InlineKb | null;
  /** the workspace inbound default an unpinned agent inherits */
  workspaceDefault?: InboundAuth | null;
  /** the workspace pool's issuer — what platform invokes present */
  cognitoIssuer?: string | null;
}) {
  const { t } = useTranslation();
  // the picked profile's name; a failed read shows its id
  const profiles = useLoad(() => networkProfilesApi.list(), "v2-agent-review-network-profiles");
  const none = t("v2.agents.wizard.none");
  const list = (items: string[]) => (items.length ? items.join(", ") : none);
  const method = form.method;
  const mono = (v: string) => <span className="mono">{v}</span>;

  const skillNames = form.skills.map(
    (p) => cat.skills.find((s) => s.path === p)?.name ?? ui.customSkills.find((c) => c.path === p)?.name ?? skillNameFromPath(p),
  );
  const kbValue: React.ReactNode = form.selectedKbs.length ? (
    form.selectedKbs.map((id, i) => (
      <span key={id} data-testid={id === inlineKb?.kb_id ? "v2-agent-review-inline-kb" : undefined}>
        {i > 0 && ", "}
        {id === inlineKb?.kb_id ? inlineKb.name : resolveKb(id, cat.kbCatalog, []).name}
        {id === inlineKb?.kb_id && (
          <>
            {" "}
            <InlineKbState inline={inlineKb} />
          </>
        )}
      </span>
    ))
  ) : (
    none
  );
  const kbBlock = inlineKbBlock(inlineKb, form.selectedKbs);
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
  const mcpServer = method === "byoc" && form.protocol === "mcp";
  if (mcpServer) {
    const env = Object.keys(byocEnv(form.byocEnvRows));
    const upload = ui.byocUpload;
    // the bridge is the entrypoint; the row shows the argv it starts
    const stdio = form.mcpStdio?.on && form.byocKind === "code_zip" ? form.mcpStdio : null;
    items = [
      ...head,
      { label: t("v2.agents.wizard.mcp.kind"), value: t("v2.agents.wizard.mcp.kindValue") },
      { label: t("v2.agents.wizard.mcp.targetName"), value: mono(form.mcpTargetName.trim()) },
      ...(stdio
        ? [{
            label: t("mcpServer.stdio.label"),
            value: (
              <>
                {mono([stdio.command.trim(), ...mcpStdioArgs(stdio.args)].join(" "))} · {t(`mcpServer.stdio.baseName.${stdio.base}`)}
              </>
            ),
          }]
        : []),
      { label: t("v2.agents.wizard.artifactTitle"), value: t(`create.configure.byocKindName.${form.byocKind}`) },
      form.byocKind === "container_image"
        ? { label: t("create.configure.byocImageUri"), value: mono(form.byocImageUri.trim()) }
        : {
            label: t("create.configure.byocUpload"),
            value: upload ? mono(`${upload.original_filename} · sha256 ${upload.sha256.slice(0, 12)}…`) : none,
          },
      ...(form.byocKind === "code_zip" && !stdio
        ? [{
            label: t("v2.agents.wizard.entryPython"),
            value: mono(`${form.byocEntrypoint.trim() || "main.py"} · ${pythonLabel(form.byocPython)}`),
          }]
        : []),
      { label: t("create.configure.byocEnv"), value: env.length ? mono(env.join(", ")) : none },
      { label: t("create.configure.byocDescription"), value: form.byocDescription || none },
    ];
  } else if (method === "byoc") {
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
    const context = form.contextLayer && contextLayerSupported(form)
      ? [{
          label: t("v2.contextLayer.wizard.title"),
          value: (
            <span data-testid="v2-agent-review-context">
              {form.contextLayer.namespaceName || none} · {mono(form.contextLayer.namespaceId)} ·{" "}
              <span data-testid="v2-agent-review-context-via">
                {t(
                  contextLayerVia(form.contextLayer) === "gateway"
                    ? "v2.contextLayer.wizard.viaGateway"
                    : "v2.contextLayer.wizard.viaDirect",
                )}
              </span>
            </span>
          ),
        }]
      : [];
    const common = [
      { label: t("v2.agents.wizard.skills"), value: list(skillNames) },
      { label: t("v2.agents.kbTitle"), value: kbValue },
      ...context,
      memory,
    ];
    // harness / Strands: managed session storage is their only mount
    const sessionFs = {
      label: t("v2.agents.wizard.fsSession"),
      value: form.sessionFs ? mono(form.sessionMount) : none,
    };
    if (method === "harness") {
      items = [
        ...head,
        ...model,
        {
          label: t("v2.agents.wizard.tools"),
          value: list([
            ...form.tools.map((n) => t(`v2.agents.wizard.builtin.${n}`)),
            ...(managedGatewayAttached(form) ? [t("v2.agents.wizard.managedGateway")] : []),
            ...form.selectedGateway,
            ...form.selectedMcp,
            ...form.nativeTools.map((n) => t(`v2.agents.wizard.native.${n}`)),
          ]),
        },
        ...common,
        sessionFs,
        {
          label: t("v2.agents.wizard.loop"),
          value: t("v2.agents.wizard.loopValue", { iterations: form.maxIterations, seconds: form.timeoutSeconds }),
        },
      ];
    } else if (method === "zip_runtime") {
      const a2a = form.protocol === "a2a";
      const kit = toolkitToolNames(form.toolkits);
      const strands = strandsReviewSpec(form);
      // picked Strands tools replace the template's calculator and current_utc_time
      const base = kit.length ? kit : strands.strands_tools?.length ? [] : ["calculator", "current_utc_time"];
      items = [
        ...head,
        { label: t("v2.agents.protocol"), value: t(`v2.agents.wizard.protocol.${form.protocol}`) },
        ...model,
        {
          label: t("v2.agents.wizard.tools"),
          value: list([
            ...base,
            ...(strands.strands_tools ?? []),
            ...(a2a ? [] : form.selectedGateway),
            ...(managedGatewayAttached(form) ? [t("v2.agents.wizard.managedGateway")] : []),
          ]),
        },
        ...strandsReviewItems(strands, t),
        ...(a2a
          ? [{ label: t("v2.agents.wizard.a2aSkills"), value: list(form.a2aSkills.filter((s) => s.name.trim()).map((s) => s.name.trim())) }]
          : [{ label: t("v2.agents.wizard.toolkits"), value: list(form.toolkits) }]),
        ...common,
        sessionFs,
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
      ];
    }
  }

  // every method deploys into the chosen network profile (projects §12.6); none ⇒ PUBLIC,
  // except a container's legacy inline subnets (BYO mounts, no profile)
  const profile = form.networkProfileId ? profiles.data?.profiles.find((p) => p.id === form.networkProfileId) : null;
  items = [
    ...items,
    {
      label: t("network.title"),
      value: form.networkProfileId
        ? profile
          ? <>{profile.name} · {mono(profile.id)}</>
          : t("v2.agents.wizard.networkProfile", { id: form.networkProfileId })
        : method === "container" && hasByoMounts(form)
          ? mono(`${form.vpcSubnets} · ${form.vpcSgs}`)
          : t("network.public"),
    },
  ];

  const inboundShown = inboundCapable(method, form.protocol) && !!form.inbound;
  const deployedJwt = inboundShown ? wizardDeployedJwt(form.inbound, form.inboundJwt, workspaceDefault) : null;
  // The last screen before 部署: say now that the console will be refused, not at the first Chat.
  const mismatch = deployedJwt ? issuerMismatch(deployedJwt.discovery_url, cognitoIssuer) : null;
  if (inboundShown) {
    items = [
      ...items,
      {
        label: t("inboundAuth.title"),
        value:
          form.inbound === "jwt" && deployedJwt
            ? mono(`JWT · ${jwtSummary(deployedJwt)}`)
            : t(`inboundAuth.choice.${form.inbound}`),
      },
      ...(deployedJwt
        ? [{ label: t("inboundAuth.discoveryUrl"), value: mono(deployedJwt.discovery_url || none) }]
        : []),
    ];
  }

  return (
    <Card
      title={t("v2.agents.wizard.reviewTitle")}
      sub={t(mcpServer ? "v2.agents.wizard.mcp.reviewSub" : `v2.agents.wizard.reviewSubBy.${method}`)}
      testId="v2-agent-review"
    >
      <Descriptions items={project ? [{ label: t("v3.create.project.label"), value: project }, ...items] : items} />
      {mismatch && <ReachabilityWarning mismatch={mismatch} testId="v2-agent-review-issuer-warning" />}
      {method !== "byoc" && (
        <>
          <h3 className="v2-sub-title">{t("v2.agents.wizard.systemPrompt")}</h3>
          <pre className="v2-pre">{form.systemPrompt}</pre>
        </>
      )}
      <div className="v2-agents-foot">
        {kbBlock === "waiting" && <Alert tone="warn">{t("v2.agents.wizard.inlineKb.waitActive")}</Alert>}
        {kbBlock === "failed" && <Alert tone="error">{t("v2.agents.wizard.inlineKb.failedBlock")}</Alert>}
        {inlineKb && kbBlock === null && form.selectedKbs.includes(inlineKb.kb_id) && (
          <Alert>{t("v2.agents.wizard.inlineKb.note")}</Alert>
        )}
        <Alert>{t("v2.agents.wizard.deployNote")}</Alert>
      </div>
    </Card>
  );
}
