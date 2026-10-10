import { Plus, Trash2 } from "lucide-react";
import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { useSearchParams } from "react-router-dom";

import { api, errorMessage } from "../../../lib/api";
import {
  type DatasetItem,
  draftToItem,
  emptyScenario,
  emptySimScenario,
  isMixedSimulated,
  parseDatasetImport,
  parseScenarioJson,
  SAMPLE_SCENARIOS,
  type ScenarioDraft,
  ScenarioJsonError,
  SIM_SAMPLE_SCENARIOS,
  type SimScenarioDraft,
  toDraft,
  toDrafts,
  toItems,
  toSimDrafts,
  toSimItems,
} from "../../../lib/datasetDrafts";
import { useLoad, useV2Toast } from "../../hooks";
import { Alert, Button, Card, Field, FlowHeader, OptionCard, Segmented, Spin, Tag } from "../../ui";

type Mode = "form" | "import";
type ScenarioType = "predefined" | "simulated";

const MAX_ITEMS = 200;

/** One editable string list (assertions) — a row per entry plus an add button. */
function StringList({
  values,
  onChange,
  addLabel,
  removeLabel,
  placeholder,
  testId,
}: {
  values: string[];
  onChange: (values: string[]) => void;
  addLabel: string;
  removeLabel: string;
  placeholder?: string;
  testId: string;
}) {
  return (
    <div className="v2-stack">
      {values.map((value, i) => (
        <div key={i} className="v2-row" style={{ flexWrap: "nowrap" }}>
          <input
            className="v2-input"
            value={value}
            placeholder={placeholder}
            onChange={(e) => onChange(values.map((x, j) => (j === i ? e.target.value : x)))}
            data-testid={`${testId}-${i}`}
          />
          <Button size="sm" title={removeLabel} onClick={() => onChange(values.filter((_, j) => j !== i))}>
            <Trash2 size={13} aria-hidden="true" />
          </Button>
        </div>
      ))}
      <div>
        <Button size="sm" onClick={() => onChange([...values, ""])} testId={`${testId}-add`}>
          <Plus size={13} aria-hidden="true" />
          {addLabel}
        </Button>
      </div>
    </div>
  );
}

/** 场景型: multi-turn conversation + assertions + expected tool trajectory, or raw JSON. */
function ScenarioBlock({
  index,
  scenario,
  canRemove,
  onChange,
  onRemove,
  onError,
}: {
  index: number;
  scenario: ScenarioDraft;
  canRemove: boolean;
  onChange: (next: ScenarioDraft) => void;
  onRemove: () => void;
  onError: (message: string | null) => void;
}) {
  const { t } = useTranslation();
  const patch = (p: Partial<ScenarioDraft>) => onChange({ ...scenario, ...p });
  const extraKeys = Object.keys(scenario.extra);
  // back to the form: re-parse; an invalid document stays in JSON mode with the error shown
  const closeJson = () => {
    if (scenario.json == null) return;
    try {
      const next = toDraft(parseScenarioJson(scenario.json), index);
      onError(null);
      onChange(next);
    } catch (err) {
      onError(t("v2.datasets.jsonInvalid", { index: index + 1, error: err instanceof Error ? err.message : String(err) }));
    }
  };
  return (
    <div className="v2-scenario" data-testid="v2-dataset-scenario">
      <div className="v2-scenario-head">
        <span className="v2-muted">{index + 1}</span>
        <input
          className="v2-input mono"
          style={{ maxWidth: 260 }}
          value={scenario.scenario_id}
          disabled={scenario.json != null}
          aria-label={t("v2.datasets.scenarioId")}
          placeholder={t("v2.datasets.scenarioId")}
          onChange={(e) => patch({ scenario_id: e.target.value })}
        />
        {scenario.json == null && (
          <Tag tone="outline">
            {scenario.turns.length > 1 ? t("v2.datasets.turnCount", { count: scenario.turns.length }) : t("v2.datasets.singleTurn")}
          </Tag>
        )}
        <div className="end">
          {scenario.json != null ? (
            <Button size="sm" onClick={closeJson} testId="v2-dataset-back-to-form">
              {t("v2.datasets.backToForm")}
            </Button>
          ) : (
            <Button size="sm" onClick={() => patch({ json: JSON.stringify(draftToItem(scenario), null, 2) })} testId="v2-dataset-edit-json">
              {t("v2.datasets.editJson")}
            </Button>
          )}
          <Button size="sm" title={t("v2.datasets.removeScenario")} disabled={!canRemove} onClick={onRemove}>
            <Trash2 size={13} aria-hidden="true" />
          </Button>
        </div>
      </div>
      {scenario.json != null ? (
        <>
          {scenario.jsonOnly && (
            <Alert tone="warn">
              {t("v2.datasets.jsonOnly", { reason: t(`evalPage.datasets.execution.jsonOnlyReason.${scenario.jsonOnly}`) })}
            </Alert>
          )}
          <Field label={t("v2.datasets.jsonLabel")} hint={t("v2.datasets.jsonHint")}>
            <textarea
              className="v2-textarea code"
              rows={14}
              value={scenario.json}
              onChange={(e) => patch({ json: e.target.value })}
              data-testid="v2-dataset-scenario-json"
            />
          </Field>
        </>
      ) : (
        <>
          {extraKeys.length > 0 && <span className="v2-muted">{t("v2.datasets.extraKeys", { keys: extraKeys.join(", ") })}</span>}
          <div className="v2-stack">
            {scenario.turns.map((turn, ti) => (
              <div key={ti} className="v2-row" style={{ alignItems: "flex-start", flexWrap: "nowrap" }}>
                <span className="v2-muted mono" style={{ width: 28, paddingTop: 6 }}>
                  T{ti + 1}
                </span>
                <textarea
                  className="v2-textarea"
                  style={{ minHeight: 56, flex: 3 }}
                  rows={2}
                  placeholder={t("v2.datasets.inputPlaceholder")}
                  value={turn.input}
                  onChange={(e) => patch({ turns: scenario.turns.map((x, j) => (j === ti ? { ...x, input: e.target.value } : x)) })}
                  aria-label={`Input ${index + 1}.${ti + 1}`}
                  data-testid={ti === 0 ? `v2-dataset-input-${index}` : `v2-dataset-input-${index}-${ti}`}
                />
                <textarea
                  className="v2-textarea"
                  style={{ minHeight: 56, flex: 2 }}
                  rows={2}
                  placeholder={t("v2.datasets.expectedPlaceholder")}
                  value={turn.expected_response}
                  onChange={(e) =>
                    patch({ turns: scenario.turns.map((x, j) => (j === ti ? { ...x, expected_response: e.target.value } : x)) })
                  }
                  aria-label={`${t("v2.datasets.expected")} ${index + 1}.${ti + 1}`}
                />
                <Button
                  size="sm"
                  title={t("v2.datasets.removeTurn")}
                  disabled={scenario.turns.length <= 1}
                  onClick={() => patch({ turns: scenario.turns.filter((_, j) => j !== ti) })}
                >
                  <Trash2 size={13} aria-hidden="true" />
                </Button>
              </div>
            ))}
            <div>
              <Button size="sm" onClick={() => patch({ turns: [...scenario.turns, { input: "", expected_response: "" }] })} testId="v2-dataset-add-turn">
                <Plus size={13} aria-hidden="true" />
                {t("v2.datasets.addTurn")}
              </Button>
            </div>
          </div>
          <div className="v2-form cols-2">
            <Field label={t("v2.datasets.assertions")} hint={t("v2.datasets.assertionsHint")}>
              <StringList
                values={scenario.assertions}
                onChange={(assertions) => patch({ assertions })}
                addLabel={t("v2.datasets.addAssertion")}
                removeLabel={t("v2.datasets.removeAssertion")}
                testId={`v2-dataset-assertion-${index}`}
              />
            </Field>
            <Field label={t("v2.datasets.trajectory")} hint={t("v2.datasets.trajectoryHint")}>
              <input
                className="v2-input mono"
                placeholder="calculator, current_time"
                value={scenario.expected_trajectory}
                onChange={(e) => patch({ expected_trajectory: e.target.value })}
                data-testid={`v2-dataset-trajectory-${index}`}
              />
            </Field>
          </div>
        </>
      )}
    </div>
  );
}

/** 模拟用户: an LLM actor persona (context + goal + traits) and its opening message. */
function PersonaBlock({
  index,
  persona,
  canRemove,
  onChange,
  onRemove,
}: {
  index: number;
  persona: SimScenarioDraft;
  canRemove: boolean;
  onChange: (next: SimScenarioDraft) => void;
  onRemove: () => void;
}) {
  const { t } = useTranslation();
  const patch = (p: Partial<SimScenarioDraft>) => onChange({ ...persona, ...p });
  return (
    <div className="v2-scenario" data-testid="v2-dataset-persona">
      <div className="v2-scenario-head">
        <span className="v2-muted">{index + 1}</span>
        <input
          className="v2-input mono"
          style={{ maxWidth: 260 }}
          value={persona.scenario_id}
          aria-label={t("v2.datasets.scenarioId")}
          placeholder={t("v2.datasets.scenarioId")}
          onChange={(e) => patch({ scenario_id: e.target.value })}
        />
        <div className="end">
          <Button size="sm" title={t("v2.datasets.removeScenario")} disabled={!canRemove} onClick={onRemove}>
            <Trash2 size={13} aria-hidden="true" />
          </Button>
        </div>
      </div>
      <div className="v2-form cols-2">
        <Field label={t("v2.datasets.simDescription")} full>
          <input className="v2-input" value={persona.scenario_description} onChange={(e) => patch({ scenario_description: e.target.value })} />
        </Field>
        <Field label={t("v2.datasets.simContext")} hint={t("v2.datasets.simContextHint")} required>
          <textarea
            className="v2-textarea"
            rows={3}
            value={persona.context}
            onChange={(e) => patch({ context: e.target.value })}
            data-testid={`v2-dataset-sim-context-${index}`}
          />
        </Field>
        <Field label={t("v2.datasets.simGoal")} hint={t("v2.datasets.simGoalHint")} required>
          <textarea
            className="v2-textarea"
            rows={3}
            value={persona.goal}
            onChange={(e) => patch({ goal: e.target.value })}
            data-testid={`v2-dataset-sim-goal-${index}`}
          />
        </Field>
        <Field label={t("v2.datasets.simInput")} hint={t("v2.datasets.simInputHint")} required full>
          <textarea
            className="v2-textarea"
            style={{ minHeight: 56 }}
            rows={2}
            value={persona.input}
            onChange={(e) => patch({ input: e.target.value })}
            data-testid={`v2-dataset-sim-input-${index}`}
          />
        </Field>
        <Field label={t("v2.datasets.simTraits")} hint={t("v2.datasets.simTraitsHint")}>
          <div className="v2-stack">
            {persona.traits.map((trait, ti) => (
              <div key={ti} className="v2-row" style={{ flexWrap: "nowrap" }}>
                <input
                  className="v2-input mono"
                  style={{ width: 140, flex: "none" }}
                  placeholder="expertise"
                  aria-label={t("v2.datasets.simTraitKey")}
                  value={trait.key}
                  onChange={(e) => patch({ traits: persona.traits.map((x, j) => (j === ti ? { ...x, key: e.target.value } : x)) })}
                />
                <input
                  className="v2-input"
                  placeholder="non-technical"
                  aria-label={t("v2.datasets.simTraitValue")}
                  value={trait.value}
                  onChange={(e) => patch({ traits: persona.traits.map((x, j) => (j === ti ? { ...x, value: e.target.value } : x)) })}
                />
                <Button size="sm" title={t("v2.datasets.removeTrait")} onClick={() => patch({ traits: persona.traits.filter((_, j) => j !== ti) })}>
                  <Trash2 size={13} aria-hidden="true" />
                </Button>
              </div>
            ))}
            <div>
              <Button size="sm" onClick={() => patch({ traits: [...persona.traits, { key: "", value: "" }] })}>
                <Plus size={13} aria-hidden="true" />
                {t("v2.datasets.addTrait")}
              </Button>
            </div>
          </div>
        </Field>
        <Field label={t("v2.datasets.simMaxTurns")} hint={t("v2.datasets.simMaxTurnsHint")}>
          <input
            className="v2-input mono"
            type="number"
            min={1}
            style={{ width: 120 }}
            value={persona.max_turns}
            onChange={(e) => patch({ max_turns: e.target.value })}
          />
        </Field>
        <Field label={t("v2.datasets.assertions")} hint={t("v2.datasets.simAssertionsHint")} full>
          <StringList
            values={persona.assertions}
            onChange={(assertions) => patch({ assertions })}
            addLabel={t("v2.datasets.addAssertion")}
            removeLabel={t("v2.datasets.removeAssertion")}
            testId={`v2-dataset-sim-assertion-${index}`}
          />
        </Field>
      </div>
    </div>
  );
}

// ─── create / edit ─────────────────────────────────────────────────────────
/**
 * The same three creation paths as the classic Datasets view: the 场景型 form
 * (multi-turn + assertions + expected trajectory, any scenario editable as raw
 * JSON), the 模拟用户 persona form, and JSON / JSONL import. The server infers the
 * kind from the items once; editing keeps the row's kind and its stored extras.
 */
export function DatasetEditor({ id }: { id: string | null }) {
  const { t } = useTranslation();
  const [, setParams] = useSearchParams();
  const toast = useV2Toast();
  const list = useLoad(() => api.v2Datasets(), "datasets");
  const ds = id ? (list.data?.datasets.find((d) => d.id === id) ?? null) : null;
  const [seeded, setSeeded] = useState(id === null);
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [mode, setMode] = useState<Mode>("form");
  const [type, setType] = useState<ScenarioType>("predefined");
  const [scenarios, setScenarios] = useState<ScenarioDraft[]>(() => [emptyScenario(1)]);
  const [personas, setPersonas] = useState<SimScenarioDraft[]>(() => [emptySimScenario(1)]);
  const [importText, setImportText] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const imported = useMemo(() => parseDatasetImport(importText), [importText]);

  if (!seeded && ds) {
    setSeeded(true);
    setName(ds.name);
    setDescription(ds.description);
    if (ds.kind === "simulated") {
      setType("simulated");
      setPersonas(toSimDrafts(ds.items));
    } else {
      setScenarios(toDrafts(ds.items));
    }
  }

  if (id && list.loading) return <Spin />;
  if (id && !ds) return <Alert tone="error">{list.error ?? t("v2.datasets.notFound")}</Alert>;
  // editing pins the type to the row's immutable kind; creating follows the picker
  const kind = ds?.kind ?? type;
  const activeMode: Mode = ds ? "form" : mode;
  const mixed = ds ? isMixedSimulated(ds.kind, ds.items) : false;
  const importError = imported.error
    ? imported.error.code === "badLine"
      ? t("evalPage.datasets.importBadLine", { line: imported.error.line })
      : t("evalPage.datasets.importNoScenarios")
    : null;

  const prefill = () => {
    setError(null);
    if (type === "simulated") {
      setName("support-personas-sample");
      setDescription("2 simulated personas (support + billing)");
      setPersonas(SIM_SAMPLE_SCENARIOS());
    } else {
      setName("math-gt-sample");
      setDescription("3 scenarios with ground truth (calculator trajectory)");
      setScenarios(SAMPLE_SCENARIOS());
    }
  };

  const save = async () => {
    if (!name.trim()) return setError(t("v2.datasets.errName"));
    let items: DatasetItem[];
    if (activeMode === "import") {
      if (importError) return setError(importError);
      items = imported.items;
      if (items.length === 0) return setError(t("evalPage.datasets.importEmpty"));
    } else {
      try {
        items = kind === "simulated" ? toSimItems(personas) : toItems(scenarios, ds ? ds.kind : "predefined");
      } catch (err) {
        if (err instanceof ScenarioJsonError) return setError(t("v2.datasets.jsonInvalid", { index: err.index, error: err.detail }));
        throw err;
      }
      if (items.length === 0) return setError(t("v2.datasets.errItems"));
    }
    if (items.length > MAX_ITEMS) return setError(t("v2.datasets.errTooMany"));
    setSaving(true);
    setError(null);
    try {
      if (id) {
        await api.v2UpdateDataset(id, { name: name.trim(), description, items });
        toast("success", t("v2.datasets.saved"));
        setParams({ tab: "datasets", view: "dataset", id });
      } else {
        const created = await api.v2CreateDataset({ name: name.trim(), description, items });
        toast("success", t("v2.datasets.created"));
        setParams({ tab: "datasets", view: "dataset", id: created.id });
      }
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setSaving(false);
    }
  };

  const count = activeMode === "import" ? imported.items.length : kind === "simulated" ? personas.length : scenarios.length;
  const recordsSub =
    activeMode === "import"
      ? t("v2.datasets.recordsSubImport")
      : kind === "simulated"
        ? t("v2.datasets.recordsSubSim")
        : t("v2.datasets.recordsSub");

  return (
    <>
      <FlowHeader
        title={id ? t("v2.datasets.editTitle") : t("v2.datasets.newTitle")}
        onBack={() => setParams(id ? { tab: "datasets", view: "dataset", id } : { tab: "datasets" })}
        end={
          <>
            {!id && activeMode === "form" && (
              <Button onClick={prefill} testId="v2-dataset-prefill">
                {t("v2.datasets.prefill")}
              </Button>
            )}
            <Button kind="primary" disabled={saving || mixed} onClick={() => void save()} testId="v2-dataset-save">
              {id ? t("v2.common.save") : t("v2.common.create")}
            </Button>
          </>
        }
      />
      {error && <Alert tone="error">{error}</Alert>}
      <Card title={t("v2.datasets.basic")}>
        <div className="v2-form cols-2">
          <Field label={t("v2.datasets.colName")} required>
            <input className="v2-input" value={name} maxLength={64} onChange={(e) => setName(e.target.value)} data-testid="v2-dataset-name" />
          </Field>
          {id ? (
            <Field label={t("v2.datasets.colKind")} hint={t("v2.datasets.kindHint")}>
              <input className="v2-input" disabled value={t(`v2.datasets.kind.${kind}`, { defaultValue: kind })} />
            </Field>
          ) : (
            <Field label={t("v2.datasets.mode")} hint={activeMode === "import" ? t("v2.datasets.modeImportHint") : t("v2.datasets.kindHint")}>
              <div>
                <Segmented
                  value={mode}
                  ariaLabel={t("v2.datasets.mode")}
                  onChange={(next) => {
                    setMode(next);
                    setError(null);
                  }}
                  options={[
                    { value: "form", label: t("v2.datasets.modeForm") },
                    { value: "import", label: t("v2.datasets.modeImport") },
                  ]}
                />
              </div>
            </Field>
          )}
          <Field label={t("v2.datasets.description")} full>
            <textarea className="v2-textarea" rows={2} value={description} maxLength={1000} onChange={(e) => setDescription(e.target.value)} />
          </Field>
          {!id && activeMode === "form" && (
            <Field label={t("v2.datasets.colKind")} full>
              <div className="v2-options">
                {(["predefined", "simulated"] as const).map((k) => (
                  <OptionCard
                    key={k}
                    title={t(`v2.datasets.kind.${k}`)}
                    desc={t(`v2.datasets.kindDesc.${k}`)}
                    on={type === k}
                    onClick={() => {
                      setType(k);
                      setError(null);
                    }}
                    testId={`v2-dataset-type-${k}`}
                  />
                ))}
              </div>
            </Field>
          )}
        </div>
      </Card>
      <Card
        title={t("v2.datasets.records")}
        sub={recordsSub}
        end={
          activeMode === "form" && !mixed ? (
            <Button
              size="sm"
              disabled={count >= MAX_ITEMS}
              onClick={() =>
                kind === "simulated"
                  ? setPersonas([...personas, emptySimScenario(personas.length + 1)])
                  : setScenarios([...scenarios, emptyScenario(scenarios.length + 1)])
              }
              testId="v2-dataset-add"
            >
              <Plus size={13} aria-hidden="true" />
              {kind === "simulated" ? t("v2.datasets.addPersona") : t("v2.datasets.addScenario")}
            </Button>
          ) : undefined
        }
      >
        {mixed ? (
          <Alert tone="warn">{t("v2.datasets.simMixed")}</Alert>
        ) : activeMode === "import" ? (
          <Field
            label={t("v2.datasets.importLabel")}
            error={importError}
            hint={importText.trim() ? t("v2.datasets.importPreview", { count: imported.items.length }) : undefined}
          >
            <textarea
              className="v2-textarea code"
              rows={14}
              placeholder={'{"scenarios": [...]}  |  {"prompt": "...", "expected": "..."} per line'}
              value={importText}
              onChange={(e) => setImportText(e.target.value)}
              data-testid="v2-dataset-import"
            />
          </Field>
        ) : kind === "simulated" ? (
          <div className="v2-stack" style={{ gap: 12 }}>
            {personas.map((persona, i) => (
              <PersonaBlock
                key={i}
                index={i}
                persona={persona}
                canRemove={personas.length > 1}
                onChange={(next) => setPersonas(personas.map((p, j) => (j === i ? next : p)))}
                onRemove={() => setPersonas(personas.filter((_, j) => j !== i))}
              />
            ))}
            <Alert>{t("v2.datasets.simHint")}</Alert>
          </div>
        ) : (
          <div className="v2-stack" style={{ gap: 12 }}>
            {scenarios.map((scenario, i) => (
              <ScenarioBlock
                key={i}
                index={i}
                scenario={scenario}
                canRemove={scenarios.length > 1}
                onChange={(next) => setScenarios(scenarios.map((s, j) => (j === i ? next : s)))}
                onRemove={() => setScenarios(scenarios.filter((_, j) => j !== i))}
                onError={setError}
              />
            ))}
          </div>
        )}
      </Card>
    </>
  );
}
