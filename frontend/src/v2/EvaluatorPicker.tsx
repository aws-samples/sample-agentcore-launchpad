import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";

import { type EvaluatorRow } from "../lib/api";
import { evaluatorLabel, type EvaluatorLevel } from "../lib/evaluators";
import { usePaged } from "./hooks";
import { FilterSelect, Pager, SearchInput, Table, Tag } from "./ui";

/**
 * Multi-select of evaluators in the anchor pages' table form (level filter,
 * search, picked tags, a hard maximum). `blockedReason` disables a row and
 * names why (e.g. it needs ground truth the live traffic cannot carry).
 */
export function EvaluatorPicker({
  evaluators,
  loading,
  error,
  onRetry,
  selected,
  onChange,
  max,
  blockedReason,
  testIdPrefix = "v2-eval",
}: {
  evaluators: EvaluatorRow[];
  loading?: boolean;
  error?: string | null;
  onRetry?: () => void;
  selected: string[];
  onChange: (next: string[]) => void;
  max: number;
  blockedReason?: (e: EvaluatorRow) => string | null;
  testIdPrefix?: string;
}) {
  const { t } = useTranslation();
  const [level, setLevel] = useState("");
  const [q, setQ] = useState("");
  const name = (e: EvaluatorRow) => (e.source === "custom" ? (e.name ?? e.id) : evaluatorLabel(t, e.id));
  const rows = useMemo(() => {
    const needle = q.trim().toLowerCase();
    return evaluators.filter((e) => {
      if (level && e.level !== level) return false;
      return !needle || `${e.id} ${e.name ?? ""} ${evaluatorLabel(t, e.id)}`.toLowerCase().includes(needle);
    });
  }, [evaluators, level, q, t]);
  const paged = usePaged(rows, 10);
  const toggle = (id: string) =>
    onChange(selected.includes(id) ? selected.filter((x) => x !== id) : [...selected, id]);

  return (
    <>
      <div className="v2-toolbar">
        <FilterSelect
          label={t("v2.evaluators.colLevel")}
          value={level}
          allLabel={t("v2.common.all")}
          onChange={setLevel}
          options={(["SESSION", "TRACE", "TOOL_CALL"] as EvaluatorLevel[]).map((l) => ({ value: l, label: t(`v2.level.${l}`) }))}
        />
        <span className="v2-muted">{t("v2.picker.picked", { count: selected.length, max })}</span>
        <div className="end">
          <SearchInput value={q} onChange={setQ} placeholder={t("v2.evaluators.search")} />
        </div>
      </div>
      {selected.length > 0 && (
        <div className="v2-tags" style={{ marginBottom: 12 }}>
          {selected.map((id) => {
            const row = evaluators.find((e) => e.id === id);
            return (
              <Tag key={id} tone="blue">
                {row ? name(row) : evaluatorLabel(t, id)}
              </Tag>
            );
          })}
        </div>
      )}
      <Table
        columns={[
          {
            key: "sel",
            title: "",
            width: 36,
            render: (e: EvaluatorRow) => {
              const on = selected.includes(e.id);
              const blocked = blockedReason?.(e) ?? null;
              return (
                <input
                  type="checkbox"
                  aria-label={e.id}
                  checked={on}
                  disabled={!on && (!!blocked || selected.length >= max)}
                  title={blocked ?? undefined}
                  onChange={() => toggle(e.id)}
                  data-testid={`${testIdPrefix}-${e.id}`}
                />
              );
            },
          },
          {
            key: "name",
            title: t("v2.evaluators.colName"),
            render: (e: EvaluatorRow) => (
              <>
                {name(e)}
                <span className="sub mono">{e.id}</span>
              </>
            ),
          },
          {
            key: "source",
            title: t("v2.evaluators.colSource"),
            render: (e: EvaluatorRow) => (
              <span className="v2-row">
                {t(`v2.evaluators.source.${e.source}`)}
                {e.provider && <Tag tone="outline">{e.provider}</Tag>}
              </span>
            ),
          },
          { key: "level", title: t("v2.evaluators.colLevel"), render: (e: EvaluatorRow) => t(`v2.level.${e.level}`, { defaultValue: e.level }) },
          {
            key: "note",
            title: t("v2.picker.colNote"),
            render: (e: EvaluatorRow) => {
              const blocked = blockedReason?.(e);
              return blocked ? <Tag tone="orange">{blocked}</Tag> : <span className="v2-muted">—</span>;
            },
          },
        ]}
        rows={paged.slice}
        rowKey={(e) => e.id}
        loading={loading}
        error={error}
        onRetry={onRetry}
        density="dense"
      />
      <Pager page={paged.page} pages={paged.pages} total={paged.total} onPage={paged.setPage} />
    </>
  );
}
