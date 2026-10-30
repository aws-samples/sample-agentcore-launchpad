import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { useSearchParams } from "react-router-dom";

import { api, errorMessage } from "../../../lib/api";
import { capabilityReasonText } from "../../../lib/capability-reason";
import { useLoad, useV2Toast } from "../../hooks";
import { Alert, Button, Card, Field, FlowHeader, Select } from "../../ui";
import { CanaryEvaluatorFields } from "./CanaryEvaluators";
import { canaryEligible, readCanarySeed, sessionsPerArm } from "./common";
import { useCanaryEvaluators } from "./evaluatorChoice";

/**
 * 新建金丝雀: the champion agent and the candidate edit (system prompt, plus the
 * code for a Studio agent), and the evaluators both versions are scored by plus the
 * primary evaluator that decides the verdict (N12-obs46). `champion=` preselects the agent;
 * `rec=` names the prompt recommendation whose 「用金丝雀验证」 opened the page (N17-obs309) —
 * its prompt seeds the candidate once for that champion and stays editable.
 */
export function CanaryStart() {
  const { t } = useTranslation();
  const [params, setParams] = useSearchParams();
  const toast = useV2Toast();
  const handoffChampion = params.get("champion") ?? "";
  const agentsLoad = useLoad(() => api.listAgents(), "agents");
  const active = useMemo(() => (agentsLoad.data?.agents ?? []).filter((a) => a.status === "active"), [agentsLoad.data]);
  const eligible = active.filter(canaryEligible);
  const unsupported = active.filter((a) => !canaryEligible(a));
  const [handoffPrompt] = useState(() => readCanarySeed(params.get("rec")));
  // picking another champion drops the hand-off (picking it back re-reads its spec)
  const [leftHandoff, setLeftHandoff] = useState(false);
  const [agentId, setAgentId] = useState(handoffChampion);
  const [prompt, setPrompt] = useState("");
  const [code, setCode] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const evaluators = useCanaryEvaluators();
  // R3-d: the experiment design — the planned sessions per arm, shown before start
  const [design, setDesign] = useState({ mde: "0.2", sd: "0.3", alpha: "0.05", power: "0.8" });
  const designInput = {
    mde: Number(design.mde), sd: Number(design.sd), alpha: Number(design.alpha),
    power: Number(design.power),
  };
  const plannedN = sessionsPerArm(designInput);

  // default to the first eligible agent once the list is in (a hand-off keeps its champion)
  useEffect(() => {
    if (!agentId && eligible[0]) setAgentId(eligible[0].id);
  }, [agentId, eligible]);

  const agent = active.find((a) => a.id === agentId) ?? null;
  const isStudio = agent?.method === "studio";
  // the candidate starts from the champion's current spec; switching agents re-seeds it
  useEffect(() => {
    const spec = (agent?.spec ?? {}) as { system_prompt?: unknown; code?: unknown };
    if (agent && handoffPrompt !== null && !leftHandoff && agent.id === handoffChampion) {
      setPrompt(handoffPrompt);
    } else {
      setPrompt(typeof spec.system_prompt === "string" ? spec.system_prompt : "");
    }
    setCode(typeof spec.code === "string" ? spec.code : "");
  }, [agent, handoffChampion, handoffPrompt, leftHandoff]);

  const hasEdit = !!prompt.trim() || (isStudio && !!code.trim());
  const reason = (a: (typeof active)[number]) => capabilityReasonText(t, "canaryPage", a.canary_capability);

  const create = async () => {
    setError(null);
    setBusy(true);
    try {
      const candidate: { system_prompt?: string; code?: string } = {};
      if (prompt.trim()) candidate.system_prompt = prompt;
      if (isStudio && code.trim()) candidate.code = code;
      const row = await api.createRuntimeCanary({
        agent_id: agentId, candidate, ...evaluators.body, design: designInput,
      });
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
          <Button
            kind="primary"
            disabled={busy || !agentId || !hasEdit || evaluators.selected.length === 0
              || plannedN === null}
            onClick={() => void create()}
            testId="v2-canary-create"
          >
            {t("canaryPage.create")}
          </Button>
        }
      />
      <Alert>{t("canaryPage.createHint")}</Alert>
      {error && <Alert tone="error">{error}</Alert>}
      <Card title={t("v2.canary.championTitle")}>
        <div className="v2-form">
          <Field label={t("canaryPage.agent")} required hint={unsupported.length ? t("canaryPage.eligibilityHint") : undefined}>
            <Select
              value={agentId}
              onChange={(v) => {
                if (v !== agentId) setLeftHandoff(true);
                setAgentId(v);
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
      <Card
        title={t("v2.canary.eval.title")}
        sub={t("v2.canary.eval.summary", { evaluators: evaluators.names || "—", primary: evaluators.primaryName })}
      >
        <CanaryEvaluatorFields choice={evaluators} disabled={busy} />
      </Card>
      <Card title={t("v2.canary.design.title")} sub={t("v2.canary.design.hint")}>
        <div className="v2-form">
          {(["mde", "sd", "alpha", "power"] as const).map((key) => (
            <Field
              key={key}
              label={t(`v2.canary.design.${key}`)}
              hint={key === "mde" || key === "sd" ? t(`v2.canary.design.${key}Hint`) : undefined}
            >
              <input
                className="v2-input"
                type="number"
                step={key === "alpha" ? 0.01 : 0.05}
                min={0}
                max={1}
                value={design[key]}
                onChange={(e) => setDesign((d) => ({ ...d, [key]: e.target.value }))}
                data-testid={`v2-canary-design-${key}`}
              />
            </Field>
          ))}
          {plannedN === null ? (
            <Alert tone="error">{t("v2.canary.design.invalid")}</Alert>
          ) : (
            <Alert>
              <strong data-testid="v2-canary-design-planned">
                {t("v2.canary.design.planned", { n: plannedN })}
              </strong>
              {" · "}
              {t("v2.canary.design.plannedHint")}
            </Alert>
          )}
        </div>
      </Card>
    </>
  );
}
