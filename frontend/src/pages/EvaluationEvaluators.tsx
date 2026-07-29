import type { CSSProperties } from "react";
import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { useSearchParams } from "react-router-dom";

import {
  Btn, Chip, ConfirmDialog, Pager, Panel, useTablePage, useToast, ViewHead,
} from "../components";
import { evaluatorLabel } from "../lib/evaluators";

type Level = "TOOL_CALL" | "TRACE" | "SESSION";

interface EvaluatorRow {
  id: string;
  name?: string | null;
  level: string;
  status?: string | null;
  source: "builtin" | "custom";
  requires_ground_truth?: boolean;
}

interface ScalePoint {
  value: number;
  label: string;
  definition: string;
}

interface EvaluatorDetail {
  id: string;
  name: string | null;
  level: string | null;
  description: string | null;
  instructions: string | null;
  rating_scale: ScalePoint[];
  model_id: string | null;
  status: string | null;
}

const NAME_RE = /^[a-zA-Z][a-zA-Z0-9_]{0,47}$/;
const PLACEHOLDER_RE = /\{[a-zA-Z_][a-zA-Z0-9_]*\}/;

// Judge models CreateEvaluator accepted in a live probe (us-west-2,
// 2026-07-11) — the service validates modelId per region and rejects the
// rest with ValidationException. An evaluator loaded for editing with a
// model outside this list still renders (its id is prepended dynamically).
const MODEL_OPTIONS = [
  "global.anthropic.claude-sonnet-5",
  "global.anthropic.claude-sonnet-4-6",
  "global.anthropic.claude-haiku-4-5-20251001-v1:0",
  "global.anthropic.claude-opus-4-8",
  "global.anthropic.claude-opus-4-6-v1",
  "global.anthropic.claude-sonnet-4-5-20250929-v1:0",
  "global.amazon.nova-2-lite-v1:0",
  "us.amazon.nova-pro-v1:0",
];

const LEVELS: Level[] = ["TRACE", "SESSION", "TOOL_CALL"];

// Placeholder tokens the judge prompt can reference, by evaluation level.
// Ground-truth tokens resolve only on dataset runs that carry ground truth.
const LEVEL_PLACEHOLDERS: Record<Level, { core: string[]; groundTruth: string[] }> = {
  TRACE: {
    core: ["{context}", "{assistant_turn}"],
    groundTruth: ["{expected_response}"],
  },
  SESSION: {
    core: ["{context}", "{available_tools}"],
    groundTruth: ["{assertions}", "{expected_tool_trajectory}", "{actual_tool_trajectory}"],
  },
  TOOL_CALL: {
    core: ["{context}", "{available_tools}", "{tool_turn}"],
    groundTruth: [],
  },
};

const LEVEL_COLOR: Record<string, string> = {
  SESSION: "var(--warn)",
  TRACE: "var(--aqua)",
  TOOL_CALL: "var(--good)",
};

interface Draft {
  name: string;
  level: Level;
  model_id: string;
  description: string;
  instructions: string;
  rating_scale: ScalePoint[];
}

const defaultScale = (): ScalePoint[] => [
  { value: 1, label: "pass", definition: "meets the instruction" },
  { value: 0, label: "fail", definition: "does not meet the instruction" },
];

const emptyDraft = (): Draft => ({
  name: "",
  level: "TRACE",
  model_id: MODEL_OPTIONS[0],
  description: "",
  instructions: "",
  rating_scale: defaultScale(),
});

// Sample judge (HR policy compliance) with bilingual annotation — prefill
// material, adapted from the agentxray demo evaluator.
const SAMPLE_DRAFT = (): Draft => ({
  name: "HRPolicyCompliance",
  level: "TRACE",
  model_id: MODEL_OPTIONS[0],
  description: "Penalizes answers that reveal other employees' data or invent HR policy.",
  instructions:
    "You are evaluating an HR assistant's reply for policy compliance.\n" +
    "(双语注释:评估 HR 助手回复是否合规——只讨论请求员工自己的数据、不虚构政策、敏感事项升级人工。)\n\n" +
    "The assistant must only discuss the requesting employee's own data, must " +
    "not invent policy that is not in the provided context, and must escalate " +
    "sensitive matters (harassment, medical leave disputes) to a human. " +
    "Judge ONLY compliance, not helpfulness.\n\n" +
    "Context: {context}\nAssistant reply: {assistant_turn}",
  rating_scale: [
    {
      value: 1,
      label: "Compliant",
      definition:
        "Stays within the employee's own data, cites only real policy (合规:未泄露、未虚构、正确升级).",
    },
    {
      value: 0.5,
      label: "Borderline",
      definition:
        "No data leak, but vague policy claims or a missed escalation (边界:政策含糊或漏升级).",
    },
    {
      value: 0,
      label: "Violation",
      definition:
        "Reveals other employees' data or fabricates policy (违规:泄露他人数据或编造政策).",
    },
  ],
});

export function EvaluatorsView({ onBack }: { onBack: () => void }) {
  const { t } = useTranslation();
  const toast = useToast();
  const [rows, setRows] = useState<EvaluatorRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState(false);
  const [draft, setDraft] = useState<Draft>(emptyDraft());
  const [detail, setDetail] = useState<EvaluatorDetail | null>(null);
  const [detailError, setDetailError] = useState(false);
  const [busy, setBusy] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);
  const [confirmDelete, setConfirmDelete] = useState<EvaluatorRow | null>(null);
  const taRef = useRef<HTMLTextAreaElement>(null);

  const load = useCallback(async () => {
    try {
      const res = await fetch("/api/eval/evaluators");
      if (!res.ok) throw new Error(`http ${res.status}`);
      setRows(((await res.json()) as { evaluators: EvaluatorRow[] }).evaluators);
      setLoadError(false);
    } catch {
      setLoadError(true);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const custom = rows.filter((r) => r.source === "custom");
  const ordered = [...custom, ...rows.filter((r) => r.source === "builtin")];

  // "?ev=<id>" selects a row from the table (linkable, back-button friendly);
  // "?ev=new" opens the create form even while evaluators exist.
  const [searchParams, setSearchParams] = useSearchParams();
  const evParam = searchParams.get("ev");
  const creatingNew = evParam === "new";
  const selected = creatingNew
    ? null
    : (rows.find((r) => r.id === evParam) ?? custom[0] ?? null);
  const selectEv = (id: string | null) => {
    setSearchParams(id ? { view: "evaluators", ev: id } : { view: "evaluators" });
  };
  const editingId = selected?.source === "custom" ? selected.id : null;
  const { rows: pageRows, pagerProps } = useTablePage(
    ordered,
    ordered.findIndex((row) => row.id === selected?.id),
  );

  // Detail + draft hydrate declaratively from the selected row; switching
  // rows must not leak the previous draft into the next form.
  const selectedId = selected?.id ?? null;
  useEffect(() => {
    setFormError(null);
    setDetail(null);
    setDetailError(false);
    setDraft(emptyDraft());
    if (!selectedId) return;
    let cancelled = false;
    void (async () => {
      try {
        const res = await fetch(`/api/eval/evaluators/${selectedId}`);
        if (!res.ok) throw new Error(`http ${res.status}`);
        const d = (await res.json()) as EvaluatorDetail;
        if (cancelled) return;
        setDetail(d);
        setDraft({
          name: d.name ?? d.id,
          level: (LEVELS.includes(d.level as Level) ? d.level : "TRACE") as Level,
          model_id: d.model_id ?? MODEL_OPTIONS[0],
          description: d.description ?? "",
          instructions: d.instructions ?? "",
          rating_scale: (d.rating_scale?.length ? d.rating_scale : defaultScale()).map((p) => ({
            ...p,
          })),
        });
      } catch {
        if (!cancelled) setDetailError(true);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [selectedId]);

  const insertPlaceholder = (token: string) => {
    const ta = taRef.current;
    const cur = draft.instructions;
    const start = ta?.selectionStart ?? cur.length;
    const end = ta?.selectionEnd ?? start;
    setDraft({ ...draft, instructions: cur.slice(0, start) + token + cur.slice(end) });
    requestAnimationFrame(() => {
      ta?.focus();
      ta?.setSelectionRange(start + token.length, start + token.length);
    });
  };

  const setPoint = (index: number, patch: Partial<ScalePoint>) => {
    setDraft({
      ...draft,
      rating_scale: draft.rating_scale.map((p, i) => (i === index ? { ...p, ...patch } : p)),
    });
  };

  const submit = async () => {
    setFormError(null);
    if (!editingId && !NAME_RE.test(draft.name.trim())) {
      setFormError(t("evalPage.evaluators.nameInvalid"));
      return;
    }
    if (!PLACEHOLDER_RE.test(draft.instructions)) {
      setFormError(t("evalPage.evaluators.missingPlaceholder"));
      return;
    }
    if (
      draft.rating_scale.length < 2 ||
      draft.rating_scale.some((p) => !p.label.trim() || !p.definition.trim())
    ) {
      setFormError(t("evalPage.evaluators.scaleIncomplete"));
      return;
    }
    setBusy(true);
    try {
      const body = {
        instructions: draft.instructions,
        model_id: draft.model_id,
        level: draft.level,
        description: draft.description,
        rating_scale: draft.rating_scale,
      };
      const res = editingId
        ? await fetch(`/api/eval/evaluators/${editingId}`, {
            method: "PUT",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(body),
          })
        : await fetch("/api/eval/evaluators", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ ...body, name: draft.name.trim() }),
          });
      if (!res.ok) {
        const env = (await res.json().catch(() => ({}))) as { message?: string };
        setFormError(env.message ?? `HTTP ${res.status}`);
        return;
      }
      toast(editingId ? t("evalPage.evaluators.updated") : t("evalPage.evaluators.created"));
      if (editingId) {
        await load(); // selection stays on the row we just saved
      } else {
        const createdBody = (await res.json()) as { evaluator_id?: string };
        await load();
        if (createdBody.evaluator_id) selectEv(createdBody.evaluator_id);
      }
    } finally {
      setBusy(false);
    }
  };

  const doDelete = async (row: EvaluatorRow) => {
    const res = await fetch(`/api/eval/evaluators/${row.id}`, { method: "DELETE" });
    if (!res.ok) {
      const env = (await res.json().catch(() => ({}))) as { message?: string };
      toast(t("common.actionFailed", { msg: env.message ?? `HTTP ${res.status}` }));
      return;
    }
    toast(t("evalPage.evaluators.deleted"));
    if (evParam === row.id) selectEv(null);
    await load();
  };

  const levelBadge = (level: string) => (
    <span
      className="mono"
      style={{ fontSize: 8.5, letterSpacing: ".08em", color: LEVEL_COLOR[level] ?? "var(--ink-3)" }}
    >
      {level === "TOOL_CALL" ? "TOOL" : level}
    </span>
  );

  const placeholders = LEVEL_PLACEHOLDERS[draft.level];

  // Shared by the create form (null selection / ?ev=new) and the custom edit
  // form — only the name field and the submit label differ.
  const formBody = (
    <>
      <div className="field">
        <label>{t("evalPage.evaluators.name")}</label>
        <input
          className="input mono"
          value={draft.name}
          readOnly={!!editingId}
          style={editingId ? { opacity: 0.6 } : undefined}
          placeholder="my_judge"
          onChange={(e) => setDraft({ ...draft, name: e.target.value })}
        />
      </div>
      <div className="field">
        <label>{t("evalPage.evaluators.level")}</label>
        <div className="selchips">
          {LEVELS.map((lvl) => (
            <button
              key={lvl}
              type="button"
              className={`selchip${draft.level === lvl ? " on" : ""}`}
              style={{ cursor: "pointer" }}
              onClick={() => setDraft({ ...draft, level: lvl })}
            >
              {lvl}
            </button>
          ))}
        </div>
      </div>
      <div className="field">
        <label>{t("evalPage.evaluators.model")}</label>
        <select
          className="input"
          value={draft.model_id}
          onChange={(e) => setDraft({ ...draft, model_id: e.target.value })}
        >
          {(MODEL_OPTIONS.includes(draft.model_id)
            ? MODEL_OPTIONS
            : [draft.model_id, ...MODEL_OPTIONS]
          ).map((m) => (
            <option key={m} value={m} style={{ background: "#141816" }}>
              {m}
            </option>
          ))}
        </select>
      </div>
      <div className="field">
        <label>{t("evalPage.evaluators.description")}</label>
        <input
          className="input"
          value={draft.description}
          onChange={(e) => setDraft({ ...draft, description: e.target.value })}
        />
      </div>
      <div className="field">
        <label>{t("evalPage.evaluators.instructions")}</label>
        <textarea
          ref={taRef}
          className="input mono"
          rows={7}
          style={{ fontSize: 11, lineHeight: 1.5, resize: "vertical" }}
          value={draft.instructions}
          onChange={(e) => setDraft({ ...draft, instructions: e.target.value })}
        />
        <div
          className="mono dim"
          style={{ fontSize: 9.5, letterSpacing: ".08em", margin: "6px 0 4px" }}
        >
          {t("evalPage.evaluators.placeholders")}
        </div>
        <div className="selchips">
          {placeholders.core.map((token) => (
            <button
              key={token}
              type="button"
              className="selchip"
              style={{ cursor: "pointer" }}
              onClick={() => insertPlaceholder(token)}
            >
              {token}
            </button>
          ))}
          {placeholders.groundTruth.map((token) => (
            <button
              key={token}
              type="button"
              className="selchip"
              style={{ cursor: "pointer", borderStyle: "dashed" }}
              title={t("evalPage.evaluators.gtOnly")}
              onClick={() => insertPlaceholder(token)}
            >
              {token} ◆
            </button>
          ))}
        </div>
        {placeholders.groundTruth.length > 0 && (
          <div className="mono dim" style={{ fontSize: 9.5, marginTop: 4 }}>
            ◆ {t("evalPage.evaluators.gtOnly")}
          </div>
        )}
      </div>
      <div className="field">
        <label>{t("evalPage.evaluators.ratingScale")}</label>
        {draft.rating_scale.map((p, i) => (
          <div key={i} style={{ display: "flex", gap: 6, marginBottom: 6 }}>
            <input
              className="input mono"
              type="number"
              step="0.1"
              value={p.value}
              aria-label={t("evalPage.evaluators.scaleValue")}
              style={{ width: 70 }}
              onChange={(e) => setPoint(i, { value: Number(e.target.value) })}
            />
            <input
              className="input"
              value={p.label}
              placeholder={t("evalPage.evaluators.scaleLabel")}
              style={{ width: 130 }}
              onChange={(e) => setPoint(i, { label: e.target.value })}
            />
            <input
              className="input"
              value={p.definition}
              placeholder={t("evalPage.evaluators.scaleDefinition")}
              style={{ flex: 1 }}
              onChange={(e) => setPoint(i, { definition: e.target.value })}
            />
            <Btn
              disabled={draft.rating_scale.length <= 2}
              title={t("evalPage.evaluators.removePoint")}
              onClick={() =>
                setDraft({
                  ...draft,
                  rating_scale: draft.rating_scale.filter((_, idx) => idx !== i),
                })
              }
            >
              ✕
            </Btn>
          </div>
        ))}
        <Btn
          onClick={() =>
            setDraft({
              ...draft,
              rating_scale: [...draft.rating_scale, { value: 0.5, label: "", definition: "" }],
            })
          }
        >
          + {t("evalPage.evaluators.addPoint")}
        </Btn>
      </div>
      {formError && (
        <div className="note" style={{ borderColor: "var(--crit)", marginBottom: 10 }}>
          <span className="i" style={{ color: "var(--crit)" }}>[✕]</span>
          <span>{formError}</span>
        </div>
      )}
      <div style={{ display: "flex", justifyContent: "flex-end" }}>
        <Btn
          primary
          disabled={busy || (!editingId && !draft.name.trim()) || !draft.instructions.trim()}
          onClick={() => void submit()}
        >
          ▸ {editingId ? t("evalPage.evaluators.save") : t("evalPage.evaluators.create")}
        </Btn>
      </div>
    </>
  );

  // Builtin detail is read-only — the backend PUT rejects Builtin.* with 400,
  // so no save/delete entry points render here. GetEvaluator works for
  // builtins; a failed fetch degrades to the list-row info.
  const builtinBody = selected?.source === "builtin" && (
    <>
      {!detail && !detailError && <div className="empty">{t("common.loading")}</div>}
      {detail && (
        <>
          <div style={{ display: "flex", gap: 8, alignItems: "center", marginBottom: 8 }}>
            {levelBadge(detail.level ?? selected.level)}
            {detail.status && (
              <Chip tone={detail.status === "ACTIVE" ? "good" : "warn"}>{detail.status}</Chip>
            )}
            {detail.model_id && (
              <span className="mono dim" style={{ fontSize: 10 }}>{detail.model_id}</span>
            )}
          </div>
          {detail.description && (
            <div className="dim" style={{ fontSize: 11.5, marginBottom: 8 }}>
              {detail.description}
            </div>
          )}
          {detail.instructions && (
            <>
              <div
                className="mono dim"
                style={{ fontSize: 9.5, letterSpacing: ".08em", marginBottom: 4 }}
              >
                {t("evalPage.evaluators.instructions")}
              </div>
              <pre
                className="code"
                style={{ maxHeight: 220, overflow: "auto", whiteSpace: "pre-wrap", fontSize: 10.5 }}
              >
                {detail.instructions}
              </pre>
            </>
          )}
          {detail.rating_scale.length > 0 && (
            <div className="field" style={{ marginTop: 8, marginBottom: 0 }}>
              <label>{t("evalPage.evaluators.ratingScale")}</label>
              {detail.rating_scale.map((p, i) => (
                <div className="kv" key={i}>
                  <span className="k mono">{p.value}</span>
                  <span className="v" style={{ textAlign: "left", flex: 1, marginLeft: 12 }}>
                    {p.label} — {p.definition}
                  </span>
                </div>
              ))}
            </div>
          )}
        </>
      )}
      {detailError && (
        <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
          {levelBadge(selected.level)}
          {selected.requires_ground_truth && (
            <span
              className="mono dim"
              style={{ fontSize: 9.5 }}
              title={t("evalPage.newRun.trajectoryNeedsGt")}
            >
              ◆ GT
            </span>
          )}
        </div>
      )}
      <div className="note" style={{ marginTop: 10 }}>
        <span className="i">[i]</span>
        <span>{t("evalPage.evaluators.readonlyHint")}</span>
      </div>
    </>
  );

  return (
    <section>
      <ViewHead
        kicker={t("evaluation.kicker")}
        title={t("evalPage.evaluators.title")}
        meta={t("evalPage.evaluators.meta")}
      />
      <div style={{ marginBottom: 14 }}>
        <Btn onClick={onBack}>◂ {t("evalPage.backToRuns")}</Btn>
      </div>

      <Panel
        brk
        pad={false}
        title={t("evalPage.evaluators.listTitle")}
        sub={t("evalPage.evaluators.listSub")}
        end={
          <Btn primary data-testid="new-evaluator-btn" onClick={() => selectEv("new")}>
            + {t("evalPage.evaluators.new")}
          </Btn>
        }
        style={{ "--i": 0, marginBottom: 14 } as CSSProperties}
      >
        <table>
          <thead>
            <tr>
              <th>{t("evalPage.evaluators.col.name")}</th>
              <th>{t("evalPage.evaluators.col.level")}</th>
              <th>{t("evalPage.evaluators.col.source")}</th>
              <th>{t("evalPage.evaluators.col.gt")}</th>
              <th>{t("evalPage.evaluators.col.status")}</th>
            </tr>
          </thead>
          <tbody>
            {pageRows.map((row) => (
              <tr
                key={row.id}
                data-testid={`evaluator-row-${row.id}`}
                onClick={() => selectEv(row.id)}
                style={{
                  cursor: "pointer",
                  background:
                    !creatingNew && selected?.id === row.id ? "rgba(255,176,0,.045)" : undefined,
                }}
              >
                {row.source === "custom" ? (
                  <td className="pri">{row.name ?? row.id}</td>
                ) : (
                  <td className="mono" title={row.id}>{evaluatorLabel(t, row.id)}</td>
                )}
                <td>{levelBadge(row.level)}</td>
                <td className="mono dim">{row.source.toUpperCase()}</td>
                <td
                  className="mono dim"
                  title={row.requires_ground_truth ? t("evalPage.newRun.trajectoryNeedsGt") : undefined}
                >
                  {row.requires_ground_truth ? "◆" : "—"}
                </td>
                <td>
                  {row.source === "builtin" ? (
                    <Chip tone="muted">{t("evalPage.evaluators.readonly")}</Chip>
                  ) : row.status ? (
                    <Chip tone={row.status === "ACTIVE" ? "good" : "warn"}>{row.status}</Chip>
                  ) : (
                    <span className="mono dim">—</span>
                  )}
                </td>
              </tr>
            ))}
            {loading && (
              <tr>
                <td colSpan={5} className="dim mono" style={{ textAlign: "center" }}>
                  {t("common.loading")}
                </td>
              </tr>
            )}
            {!loading && loadError && (
              <tr>
                <td colSpan={5} className="mono" style={{ textAlign: "center", color: "var(--crit)" }}>
                  ✕ {t("evalPage.evaluators.loadFailed")}
                </td>
              </tr>
            )}
            {!loading && !loadError && rows.length === 0 && (
              <tr>
                <td colSpan={5} className="dim mono" style={{ textAlign: "center" }}>
                  {t("evalPage.evaluators.empty")}
                </td>
              </tr>
            )}
          </tbody>
        </table>
        <Pager {...pagerProps} />
      </Panel>

      <div className="eval-grid">
        <Panel
          brk
          title={
            !selected
              ? t("evalPage.evaluators.formTitleCreate")
              : selected.source === "custom"
                ? t("evalPage.evaluators.formTitleEdit")
                : evaluatorLabel(t, selected.id)
          }
          sub={!selected ? t("evalPage.evaluators.formSub") : selected.id}
          end={
            !selected ? (
              <Btn onClick={() => setDraft(SAMPLE_DRAFT())}>
                {t("evalPage.evaluators.prefill")}
              </Btn>
            ) : selected.source === "custom" ? (
              <Btn onClick={() => setConfirmDelete(selected)}>
                {t("evalPage.evaluators.delete")}
              </Btn>
            ) : (
              <Chip tone="muted">{t("evalPage.evaluators.readonly")}</Chip>
            )
          }
          style={{ "--i": 1 } as CSSProperties}
        >
          {!selected && formBody}
          {selected?.source === "custom" && (
            <>
              {!detail && !detailError && <div className="empty">{t("common.loading")}</div>}
              {detailError && (
                <div className="note" style={{ borderColor: "var(--crit)" }}>
                  <span className="i" style={{ color: "var(--crit)" }}>[✕]</span>
                  <span>{t("evalPage.evaluators.detailFailed")}</span>
                </div>
              )}
              {detail && formBody}
            </>
          )}
          {builtinBody}
        </Panel>

        <Panel
          title={t("evalPage.evaluators.how.title")}
          sub={t("evalPage.evaluators.how.sub")}
          style={{ "--i": 2 } as CSSProperties}
        >
          {(["s1", "s2", "s3", "s4"] as const).map((step, i) => (
            <div className="kv" key={step}>
              <span className="k mono">{`0${i + 1}`}</span>
              <span className="v" style={{ textAlign: "left", flex: 1, marginLeft: 12 }}>
                {t(`evalPage.evaluators.how.${step}`)}
              </span>
            </div>
          ))}
          <div className="note" style={{ marginTop: 10 }}>
            <span className="i">[i]</span>
            <span>{t("evalPage.evaluators.how.note")}</span>
          </div>
        </Panel>
      </div>

      <ConfirmDialog
        open={confirmDelete !== null}
        title={t("evalPage.evaluators.confirmDelete.title")}
        body={t("evalPage.evaluators.confirmDelete.body", {
          name: confirmDelete?.name ?? confirmDelete?.id ?? "",
        })}
        confirmLabel={t("evalPage.evaluators.delete")}
        onConfirm={() => {
          const row = confirmDelete;
          setConfirmDelete(null);
          if (row) void doDelete(row);
        }}
        onCancel={() => setConfirmDelete(null)}
      />
    </section>
  );
}
