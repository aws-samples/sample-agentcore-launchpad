import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";

import {
  api,
  type Policy,
  type PolicyEngine,
  type PolicyPrincipalKind,
  type PolicySimulateRequest,
  type PolicySimulateResult,
} from "../../../lib/api";
import { governanceError, isToolPattern, parseArgumentsObject, patternExampleName } from "../../../lib/governance";
import { isoFromMs, zonedInputToMs } from "../../../lib/timestamps";
import { useLoad, useV2Toast } from "../../hooks";
import { Alert, Button, Card, Field, Select, Spin, Tag } from "../../ui";
import { EngineHistoryEditor, EngineSimResult } from "./EngineSimulation";
import { historyEvents, type HistoryRow } from "./engineHistory";

// Policies decide TOOL CALLS only (Global parity): the simulated principal is the user / API
// key / system principal behind the call (Stage BT); the calling agent is Stage A.
const PRINCIPALS: PolicyPrincipalKind[] = ["user", "api_key", "system"];

/**
 * 模拟器 (Global's "Policy Test", re-targeted at the self-hosted engine): dry-run one
 * `call_tool` — a principal, a calling agent, a Gateway tool with its input, optionally the
 * earlier events of the policy session — against THIS engine's set, evaluated for the chosen
 * gateway's resource. Nothing is called. Shows Stage A / Stage BT / final with the determining
 * policies and the replayed trace.
 *
 * Pattern rows: the entity catalog may list an fnmatch PATTERN (`crm___*`, `pattern: true`)
 * rather than a concrete tool. Choosing one reveals a "concrete tool name" input prefilled
 * from the pattern and the request sends THAT id — the engine decides concrete tools, never
 * globs.
 */
export function PolicySimulator({ engine, policies }: { engine: PolicyEngine; policies: Policy[] }) {
  const { t } = useTranslation();
  const toast = useV2Toast();
  const gateways = useLoad(
    () => api.listGovernanceGateways().catch((err: unknown) => Promise.reject(new Error(governanceError(err)))),
    "policy-sim-gateways",
  );
  const [gatewayId, setGatewayId] = useState("");
  const [principalKind, setPrincipalKind] = useState<PolicyPrincipalKind>("user");
  const [principalId, setPrincipalId] = useState("");
  const [callingAgent, setCallingAgent] = useState("");
  const [toolId, setToolId] = useState("");
  /** concrete tool id typed for a pattern row; "" ⇒ the derived example */
  const [exampleName, setExampleName] = useState("");
  const [inputText, setInputText] = useState("{}");
  /** `datetime-local` in the display zone; "" ⇒ now */
  const [atText, setAtText] = useState("");
  const [history, setHistory] = useState<HistoryRow[]>([]);
  const [result, setResult] = useState<PolicySimulateResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [running, setRunning] = useState(false);

  // the attached gateways first, so the default simulation is one the interceptor would make
  const gatewayOptions = useMemo(() => {
    const rows = gateways.data?.gateways ?? [];
    const attached = new Set(engine.attachments.map((a) => a.gateway_id));
    return [...rows]
      .sort((a, b) => Number(attached.has(b.id)) - Number(attached.has(a.id)))
      .map((g) => ({ value: g.id, label: attached.has(g.id) ? `${g.name} · ${t("v2.policy.sim.attached")}` : g.name }));
  }, [engine.attachments, gateways.data, t]);
  const effectiveGateway = gatewayOptions.some((o) => o.value === gatewayId) ? gatewayId : gatewayOptions[0]?.value ?? "";
  // the pickers: users / API keys / agents of the workspace and the tools of THE CHOSEN gateway
  const catalog = useLoad(
    () => (effectiveGateway ? api.policyEntities(engine.id, effectiveGateway).catch((err: unknown) => Promise.reject(new Error(governanceError(err)))) : Promise.resolve(null)),
    `policy-sim-entities:${engine.id}:${effectiveGateway}`,
  );
  const entities = catalog.data;
  const principalOptions = useMemo(() => {
    if (!entities) return [];
    if (principalKind === "user") return entities.users.map((u) => ({ value: u.username, label: `${u.username} · ${u.role}` }));
    if (principalKind === "api_key") return entities.api_keys.map((k) => ({ value: k.id, label: k.name }));
    return [{ value: "hub", label: "hub" }];
  }, [entities, principalKind]);
  const toolOptions = useMemo(() => {
    if (!entities) return [];
    return entities.tools.map((tool) => ({
      value: tool.name,
      label: (tool.pattern ?? isToolPattern(tool.name)) ? `${tool.name} ${t("v2.governance.policy.sim.patternSuffix")}` : tool.name,
    }));
  }, [entities, t]);
  const effectivePrincipal = principalOptions.some((o) => o.value === principalId) ? principalId : principalOptions[0]?.value ?? "";
  const effectiveTool = toolOptions.some((o) => o.value === toolId) ? toolId : toolOptions[0]?.value ?? "";
  const selectedTool = entities?.tools.find((tool) => tool.name === effectiveTool);
  const selectedIsPattern = !!selectedTool && (selectedTool.pattern ?? isToolPattern(selectedTool.name));
  const exampleValue = exampleName || (selectedIsPattern ? patternExampleName(effectiveTool) : "");
  // what the request names: the concrete example for a pattern row, the row itself otherwise
  const requestTool = selectedIsPattern ? exampleValue.trim() : effectiveTool;
  const agentOptions = (entities?.agents ?? []).filter((a) => !a.system).map((a) => ({ value: a.id, label: a.name }));
  const effectiveAgent = agentOptions.some((o) => o.value === callingAgent) ? callingAgent : agentOptions[0]?.value ?? "";
  const toolNames = (entities?.tools ?? []).filter((tool) => !(tool.pattern ?? isToolPattern(tool.name))).map((tool) => tool.name);
  const activeCount = policies.filter((p) => p.status === "active").length;

  const fail = (message: string) => {
    setError(message);
    toast("error", message);
  };

  const run = async () => {
    if (running || !effectiveGateway || !effectivePrincipal || !requestTool) return;
    const input = parseArgumentsObject(inputText);
    if (input === null) return fail(t("v2.governance.policy.sim.engine.inputInvalid"));
    const atMs = atText.trim() ? zonedInputToMs(atText) : Date.now();
    if (atMs === null) return fail(t("v2.governance.policy.sim.engine.atInvalid"));
    const events = historyEvents(history, atMs);
    if (!Array.isArray(events)) return fail(t("v2.governance.policy.sim.engine.historyInvalid", { row: events.invalid + 1 }));
    const body: PolicySimulateRequest = {
      gateway_id: effectiveGateway,
      principal: { kind: principalKind, id: effectivePrincipal },
      agent_id: effectiveAgent || null,
      tool: requestTool,
      input,
      at: atText.trim() ? isoFromMs(atMs) : null,
      history: events,
    };
    setRunning(true);
    setError(null);
    setResult(null);
    try {
      setResult(await api.simulatePolicy(engine.id, body));
    } catch (requestError) {
      fail(governanceError(requestError));
    } finally {
      setRunning(false);
    }
  };

  return (
    <Card
      title={t("v2.governance.policy.sim.title")}
      sub={t("v2.policy.sim.sub", { name: engine.name })}
      end={
        <Button kind="primary" size="sm" disabled={running || !effectiveGateway || !effectivePrincipal || !requestTool} onClick={() => void run()} testId="v2-policy-sim-run">
          {running ? t("v2.governance.policy.sim.running") : t("v2.governance.policy.sim.run")}
        </Button>
      }
      testId="v2-policy-simulator"
    >
      {!entities && catalog.loading && <Spin />}
      {activeCount === 0 && <Alert tone="warn">{t("v2.policy.sim.noActive")}</Alert>}
      {gateways.error && <Alert tone="error">{gateways.error}</Alert>}
      {catalog.error && <Alert tone="warn">{catalog.error}</Alert>}
      <div className="v2-form cols-2">
        <Field label={t("v2.policy.sim.gateway")} hint={t("v2.policy.sim.gatewayHint")}>
          <Select
            value={effectiveGateway}
            disabled={running || gatewayOptions.length === 0}
            placeholder={gatewayOptions.length === 0 ? t("v2.governance.policy.sim.noCandidates") : undefined}
            options={gatewayOptions}
            onChange={setGatewayId}
            testId="v2-policy-sim-gateway"
          />
        </Field>
        <Field label={t("v2.governance.policy.sim.action")} hint={t("v2.governance.policy.sim.actionHint")}>
          <div className="v2-row">
            <Tag tone="outline" testId="v2-policy-sim-action">
              <span className="mono">{t("v2.governance.policy.action.call_tool")}</span>
            </Tag>
          </div>
        </Field>
        <Field label={t("v2.governance.policy.sim.principalKind")}>
          <Select
            value={principalKind}
            disabled={running}
            options={PRINCIPALS.map((kind) => ({ value: kind, label: t(`v2.governance.policy.principal.${kind}`) }))}
            onChange={(v) => {
              setPrincipalKind(v as PolicyPrincipalKind);
              setPrincipalId("");
            }}
            testId="v2-policy-sim-principal-kind"
          />
        </Field>
        <Field label={t("v2.governance.policy.sim.principal")}>
          <Select
            mono
            value={effectivePrincipal}
            disabled={running || principalOptions.length === 0}
            placeholder={principalOptions.length === 0 ? t("v2.governance.policy.sim.noCandidates") : undefined}
            options={principalOptions}
            onChange={setPrincipalId}
            testId="v2-policy-sim-principal"
          />
        </Field>
        <Field label={t("v2.governance.policy.sim.engine.agent")} hint={t("v2.governance.policy.sim.engine.agentHint")}>
          <Select
            value={effectiveAgent}
            disabled={running || agentOptions.length === 0}
            placeholder={agentOptions.length === 0 ? t("v2.governance.policy.sim.noCandidates") : undefined}
            options={agentOptions}
            onChange={setCallingAgent}
            testId="v2-policy-sim-engine-agent"
          />
        </Field>
        <Field label={t("v2.governance.policy.sim.resource.tool")}>
          <Select
            mono
            value={effectiveTool}
            disabled={running || toolOptions.length === 0}
            placeholder={toolOptions.length === 0 ? t("v2.governance.policy.sim.noCandidates") : undefined}
            options={toolOptions}
            onChange={(v) => {
              setToolId(v);
              setExampleName("");
            }}
            testId="v2-policy-sim-resource"
          />
        </Field>
        {selectedIsPattern && (
          <Field label={t("v2.governance.policy.sim.patternExample")} full hint={t("v2.governance.policy.sim.patternExampleHint", { pattern: effectiveTool })}>
            <input
              className="v2-input mono"
              value={exampleValue}
              disabled={running}
              spellCheck={false}
              onChange={(e) => setExampleName(e.target.value)}
              data-testid="v2-policy-sim-example"
            />
          </Field>
        )}
        <Field label={t("v2.governance.policy.sim.engine.input")} full hint={t("v2.governance.policy.sim.engine.inputHint")}>
          <textarea
            className="v2-textarea code"
            rows={3}
            value={inputText}
            disabled={running}
            spellCheck={false}
            onChange={(e) => setInputText(e.target.value)}
            data-testid="v2-policy-sim-engine-input"
          />
        </Field>
        <Field label={t("v2.governance.policy.sim.engine.at")} hint={t("v2.governance.policy.sim.engine.atHint")}>
          <input
            type="datetime-local"
            className="v2-input mono"
            value={atText}
            disabled={running}
            onChange={(e) => setAtText(e.target.value)}
            data-testid="v2-policy-sim-engine-at"
          />
        </Field>
        <Field label={t("v2.governance.policy.sim.engine.history")} full hint={t("v2.governance.policy.sim.engine.historyHint")}>
          <EngineHistoryEditor rows={history} tools={toolNames} disabled={running} onChange={setHistory} />
        </Field>
      </div>
      {error && (
        <div style={{ marginTop: 12 }}>
          <Alert tone="error">{error}</Alert>
        </div>
      )}
      {result && <EngineSimResult result={result} />}
    </Card>
  );
}
