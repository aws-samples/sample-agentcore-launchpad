import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { useSearchParams } from "react-router-dom";

import { api, errorMessage } from "../../../lib/api";
import { useLoad, useV2Toast } from "../../hooks";
import { Alert, Button, Card, Field, FlowHeader, Select } from "../../ui";
import { versionOptions } from "./common";

/**
 * 新建金丝雀: the champion agent and the candidate edit (system prompt, plus the
 * code for a Studio agent). `champion=` / `sourceExp=` carry an experiment's
 * promote hand-off; picking another agent drops the source experiment.
 */
export function CanaryStart() {
  const { t } = useTranslation();
  const [params, setParams] = useSearchParams();
  const toast = useV2Toast();
  const handoffChampion = params.get("champion") ?? "";
  const agentsLoad = useLoad(() => api.listAgents(), "agents");
  const active = useMemo(() => (agentsLoad.data?.agents ?? []).filter((a) => a.status === "active"), [agentsLoad.data]);
  const eligible = active.filter((a) => a.canary_capability.eligible);
  const unsupported = active.filter((a) => !a.canary_capability.eligible);
  const [agentId, setAgentId] = useState(handoffChampion);
  const [sourceExp, setSourceExp] = useState(params.get("sourceExp") ?? "");
  const [prompt, setPrompt] = useState("");
  const [code, setCode] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // default to the first eligible agent once the list is in (a hand-off keeps its champion)
  useEffect(() => {
    if (!agentId && eligible[0]) setAgentId(eligible[0].id);
  }, [agentId, eligible]);

  const agent = active.find((a) => a.id === agentId) ?? null;
  const isStudio = agent?.method === "studio";
  // a Harness canary A/Bs two existing versions: control (any earlier) vs the latest
  const isHarness = agent?.method === "harness";
  const versionsLoad = useLoad(
    () => (isHarness && agentId ? api.agentVersions(agentId) : Promise.resolve(null)),
    `canary-versions:${isHarness ? agentId : ""}`,
  );
  const versions = useMemo(
    () => [...new Set((versionsLoad.data?.versions ?? []).map((v) => v.version).filter((v): v is string => !!v))]
      .sort((a, b) => Number(a) - Number(b)),
    [versionsLoad.data],
  );
  const latest = versions[versions.length - 1] ?? "";
  const earlier = versions.filter((v) => v !== latest);
  const [control, setControl] = useState("");
  useEffect(() => {
    setControl((prev) => (earlier.includes(prev) ? prev : (earlier[0] ?? "")));
  }, [earlier.join(",")]); // eslint-disable-line react-hooks/exhaustive-deps
  // the candidate starts from the champion's current spec; switching agents re-seeds it
  useEffect(() => {
    const spec = (agent?.spec ?? {}) as { system_prompt?: unknown; code?: unknown };
    setPrompt(typeof spec.system_prompt === "string" ? spec.system_prompt : "");
    setCode(typeof spec.code === "string" ? spec.code : "");
  }, [agent]);

  const hasEdit = isHarness ? !!control && !!latest : !!prompt.trim() || (isStudio && !!code.trim());
  const reason = (a: (typeof active)[number]) =>
    a.canary_capability.reason_code ? t(`canaryPage.reason.${a.canary_capability.reason_code}`) : a.canary_capability.reason;

  const create = async () => {
    setError(null);
    setBusy(true);
    try {
      const candidate: { system_prompt?: string; code?: string } = {};
      if (prompt.trim()) candidate.system_prompt = prompt;
      if (isStudio && code.trim()) candidate.code = code;
      const row = await api.createRuntimeCanary(
        isHarness
          ? { agent_id: agentId, harness_versions: { control, treatment: latest } }
          : { agent_id: agentId, candidate, ...(sourceExp ? { source_experiment_id: sourceExp } : {}) },
      );
      toast("success", t("v2.canary.created", { name: row.name }));
      setParams({ mode: "canary", canary: row.id });
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <FlowHeader
        title={t("canaryPage.create")}
        onBack={() => setParams({ mode: "canary" })}
        end={
          <Button kind="primary" disabled={busy || !agentId || !hasEdit} onClick={() => void create()} testId="v2-canary-create">
            {t("canaryPage.create")}
          </Button>
        }
      />
      <Alert>{t("canaryPage.createHint")}</Alert>
      {sourceExp && <Alert tone="success">{t("canaryPage.handoffSource", { id: sourceExp })}</Alert>}
      {error && <Alert tone="error">{error}</Alert>}
      <Card title={t("v2.canary.championTitle")}>
        <div className="v2-form">
          <Field label={t("canaryPage.agent")} required hint={unsupported.length ? t("canaryPage.eligibilityHint") : undefined}>
            <Select
              value={agentId}
              onChange={(v) => {
                setAgentId(v);
                if (sourceExp && v !== handoffChampion) setSourceExp("");
              }}
              testId="v2-canary-agent"
              placeholder={t("canaryPage.pickAgent")}
              options={[
                ...eligible.map((a) => ({ value: a.id, label: `${a.name} · ${a.method}` })),
                ...unsupported.map((a) => ({ value: a.id, label: `${a.name} · ${a.method} — ${reason(a)}`, disabled: true })),
              ]}
            />
          </Field>
        </div>
      </Card>
      {isHarness ? (
        <Card title={t("v2.canary.candidateTitle")} sub={t("assistantNext.canary.body")}>
          <div className="v2-form">
            {versionsLoad.error && <Alert tone="error">{versionsLoad.error}</Alert>}
            {!versionsLoad.loading && versions.length < 2 && <Alert tone="warn">{t("assistantNext.canary.needsTwoVersions")}</Alert>}
            <Field label={t("assistantNext.canary.control")} required>
              <Select
                value={control}
                options={versionOptions(earlier, t("assistantNext.canary.firstSuffix"))}
                disabled={earlier.length === 0}
                onChange={setControl}
                testId="v2-canary-control-version"
              />
            </Field>
            <span className="v2-muted">{t("assistantNext.canary.treatment", { v: latest || "—" })}</span>
          </div>
        </Card>
      ) : (
      <Card title={t("v2.canary.candidateTitle")} sub={t("canaryPage.candidateHint")}>
        <div className="v2-form">
          <Field label={t("canaryPage.candidatePrompt")}>
            <textarea
              className="v2-textarea code"
              rows={10}
              value={prompt}
              placeholder={t("canaryPage.candidatePromptPlaceholder")}
              onChange={(e) => setPrompt(e.target.value)}
              data-testid="v2-canary-prompt"
            />
          </Field>
          {isStudio && (
            <Field label={t("canaryPage.candidateCode")}>
              <textarea
                className="v2-textarea code"
                rows={14}
                value={code}
                placeholder={t("canaryPage.candidateCodePlaceholder")}
                onChange={(e) => setCode(e.target.value)}
                data-testid="v2-canary-code"
              />
            </Field>
          )}
        </div>
      </Card>
      )}
    </>
  );
}
