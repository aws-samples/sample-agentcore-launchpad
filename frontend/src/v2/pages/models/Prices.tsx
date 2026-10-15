// The price surfaces of `/v2/models` (admin): the model price editor (`?view=prices`) —
// the one merged price map the Spend page and the budgets price usage with — and the
// AgentCore fee rates with their calibration factors (`?view=rates`). Both are stored
// in this deployment's ledger and apply at once (services/rates.py).
import { Plus, RotateCcw } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";

import {
  type AgentCoreRates,
  api,
  errorMessage,
  type ModelPriceEntry,
  type ModelPriceTable,
} from "../../../lib/api";
import { fmtRate } from "../../../lib/budgets";
import { fmtTime } from "../../format";
import { useLoad, usePaged, useV2Toast } from "../../hooks";
import {
  Alert,
  Button,
  Card,
  type Column,
  Confirm,
  Field,
  LinkButton,
  Modal,
  Pager,
  SearchInput,
  Spin,
  Table,
  Tag,
} from "../../ui";
import {
  amountFromInput,
  amountToInput,
  cacheRate,
  isPriceKey,
  MAX_PRICE_PER_MTOK,
  MAX_RATE,
  mergeUnpriced,
  type PriceDraft,
  priceDraftFrom,
  priceDraftValid,
  pricePayload,
  type PriceRow,
  priceRows,
  type RateGroup,
  type RateRow,
  ratePatch,
  rateRows,
  unitKey,
} from "../costs/budgets";

/* ---------- model prices ---------- */

function PriceModal({
  modelId,
  entry,
  factors,
  onClose,
  onSaved,
}: {
  /** null: a new key, typed in the dialog */
  modelId: string | null;
  entry: ModelPriceEntry | null;
  factors: ModelPriceTable["cache_factors"];
  onClose: () => void;
  onSaved: () => void;
}) {
  const { t } = useTranslation();
  const toast = useV2Toast();
  const [draft, setDraft] = useState<PriceDraft>(() => priceDraftFrom(modelId ?? "", entry?.rates ?? null));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const valid = priceDraftValid(draft);
  const set = (patch: Partial<PriceDraft>) => setDraft((d) => ({ ...d, ...patch }));
  const bad = (raw: string) => !!raw.trim() && amountFromInput(raw, MAX_PRICE_PER_MTOK) === null;
  const input = amountFromInput(draft.input, MAX_PRICE_PER_MTOK);

  const submit = async () => {
    if (busy || !valid) return;
    setBusy(true);
    setError(null);
    try {
      await api.setModelPrice(draft.modelId.trim(), pricePayload(draft));
      toast("success", t("v2.models.prices.saved", { model: draft.modelId.trim() }));
      onSaved();
      onClose();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  const rateField = (field: "input" | "output" | "cache_read" | "cache_write", required: boolean, placeholder?: string) => (
    <Field
      label={t(`v2.models.prices.field.${field}`)}
      required={required}
      error={bad(draft[field]) ? t("v2.models.prices.invalid", { max: MAX_PRICE_PER_MTOK }) : null}
      hint={field === "cache_read" || field === "cache_write" ? t(`v2.models.prices.hint.${field}`, { factor: factors[field] }) : undefined}
    >
      <input
        className="v2-input"
        inputMode="decimal"
        value={draft[field]}
        placeholder={placeholder}
        onChange={(e) => set({ [field]: e.target.value })}
        onKeyDown={(e) => e.key === "Enter" && void submit()}
        data-testid={`v2-price-${field}`}
      />
    </Field>
  );

  return (
    <Modal
      title={entry ? t("v2.models.prices.editTitle") : t("v2.models.prices.addTitle")}
      open
      onClose={onClose}
      testId="v2-price-modal"
      footer={
        <>
          <Button onClick={onClose}>{t("v2.common.cancel")}</Button>
          <Button kind="primary" disabled={busy || !valid} onClick={() => void submit()} testId="v2-price-submit">
            {t("v2.common.save")}
          </Button>
        </>
      }
    >
      <div className="v2-form">
        {error && (
          <div data-testid="v2-price-error">
            <Alert tone="error">{error}</Alert>
          </div>
        )}
        <Field
          label={t("v2.models.prices.modelId")}
          hint={modelId ? undefined : t("v2.models.prices.modelIdHint")}
          error={!modelId && draft.modelId.trim() && !isPriceKey(draft.modelId) ? t("v2.models.prices.modelIdInvalid") : null}
          required
        >
          {modelId ? (
            <span className="mono" data-testid="v2-price-model-fixed">{modelId}</span>
          ) : (
            <input
              className="v2-input mono"
              maxLength={200}
              value={draft.modelId}
              placeholder="global.anthropic.claude-sonnet-5"
              onChange={(e) => set({ modelId: e.target.value })}
              data-testid="v2-price-model"
            />
          )}
        </Field>
        <span className="v2-muted">{t("v2.models.prices.unitNote")}</span>
        {rateField("input", true)}
        {rateField("output", true)}
        {rateField(
          "cache_read",
          false,
          input !== null ? t("v2.models.prices.derivedPlaceholder", { value: fmtRate(input * factors.cache_read) }) : undefined,
        )}
        {rateField(
          "cache_write",
          false,
          input !== null ? t("v2.models.prices.derivedPlaceholder", { value: fmtRate(input * factors.cache_write) }) : undefined,
        )}
        {entry?.overridden && entry.base && (
          <span className="v2-muted" data-testid="v2-price-base">
            {t("v2.models.prices.baseRates", { input: fmtRate(entry.base.input), output: fmtRate(entry.base.output) })}
          </span>
        )}
        <Alert tone="warn">{t("v2.models.prices.confirmGlobal")}</Alert>
      </div>
    </Modal>
  );
}

export function PricesView() {
  const { t } = useTranslation();
  const toast = useV2Toast();
  const table = useLoad(() => api.modelPrices(), "v2-model-prices");
  // span-seen models the map cannot price (the Spend page's default window, 60 s cache);
  // a failed scan only hides those rows
  const spans = useLoad(() => api.costs("24h"), "v2-model-prices-spans");
  const [query, setQuery] = useState("");
  const [modal, setModal] = useState<{ modelId: string | null; entry: ModelPriceEntry | null } | null>(null);
  const [toReset, setToReset] = useState<ModelPriceEntry | null>(null);
  const [busy, setBusy] = useState(false);

  const data = table.data;
  const factors = data?.cache_factors ?? { cache_read: 0.1, cache_write: 1.25 };
  const unpriced = mergeUnpriced(data?.unpriced_models ?? [], spans.data?.unpriced_models ?? []);
  const rows = priceRows(data?.prices ?? [], unpriced, query);
  const missing = rows.filter((r) => r.unpriced).length;
  const paged = usePaged(rows, 25);

  const reset = async () => {
    if (!toReset) return;
    setBusy(true);
    try {
      await api.clearModelPrice(toReset.model_id);
      toast("success", t("v2.models.prices.resetDone", { model: toReset.model_id }));
      setToReset(null);
      table.reload();
    } catch (err) {
      toast("error", errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  const rateCell = (row: PriceRow, field: "input" | "output") =>
    row.entry?.rates?.[field] != null ? fmtRate(row.entry.rates[field]) : <span className="v2-muted">—</span>;
  const cacheCell = (row: PriceRow, field: "cache_read" | "cache_write") => {
    const rate = cacheRate(row.entry?.rates ?? null, field, factors);
    if (rate.value === null) return <span className="v2-muted">—</span>;
    return (
      <>
        <span>{fmtRate(rate.value)}</span>
        {rate.derived && <span className="sub">{t("v2.models.prices.derived", { factor: factors[field] })}</span>}
      </>
    );
  };

  const columns: Column<PriceRow>[] = [
    {
      key: "model",
      title: t("v2.models.prices.colModel"),
      render: (row) => (
        <div className="v2-stack" style={{ gap: 4 }}>
          <span className="mono" style={{ wordBreak: "break-all" }}>{row.model_id}</span>
          <span className="v2-row" style={{ gap: 6 }}>
            {row.unpriced && (
              <Tag tone="red" testId={`v2-price-unpriced-${row.model_id}`}>
                {t("v2.models.prices.unpriced")}
              </Tag>
            )}
            {row.unpriced?.map((where) => (
              <Tag key={where} tone="outline">
                {t(`v2.models.prices.seenIn.${where}`, { defaultValue: where })}
              </Tag>
            ))}
            {row.entry?.overridden && (
              <Tag tone="orange" testId={`v2-price-overridden-${row.model_id}`}>
                {t("v2.models.overridden")}
              </Tag>
            )}
          </span>
        </div>
      ),
    },
    { key: "input", title: t("v2.models.prices.field.input"), className: "nowrap", render: (row) => rateCell(row, "input") },
    { key: "output", title: t("v2.models.prices.field.output"), className: "nowrap", render: (row) => rateCell(row, "output") },
    { key: "cache_read", title: t("v2.models.prices.field.cache_read"), className: "nowrap", render: (row) => cacheCell(row, "cache_read") },
    { key: "cache_write", title: t("v2.models.prices.field.cache_write"), className: "nowrap", render: (row) => cacheCell(row, "cache_write") },
    {
      key: "updated",
      title: t("v2.models.prices.colUpdated"),
      className: "nowrap",
      render: (row) =>
        row.entry?.overridden ? (
          <>
            <span>{row.entry.updated_by || "—"}</span>
            <span className="sub">{fmtTime(row.entry.updated_at)}</span>
          </>
        ) : (
          <span className="v2-muted">—</span>
        ),
    },
    {
      key: "actions",
      title: t("v2.common.actions"),
      className: "nowrap",
      render: (row) => (
        <span className="v2-row" style={{ gap: 12 }}>
          <LinkButton onClick={() => setModal({ modelId: row.model_id, entry: row.entry })} testId={`v2-price-edit-${row.model_id}`}>
            {row.unpriced ? t("v2.models.prices.setPrice") : t("v2.common.edit")}
          </LinkButton>
          {row.entry?.overridden && (
            <LinkButton onClick={() => setToReset(row.entry)} testId={`v2-price-reset-${row.model_id}`}>
              {t("v2.models.reset")}
            </LinkButton>
          )}
        </span>
      ),
    },
  ];

  return (
    <>
      <Alert tone="info">
        <span data-testid="v2-prices-note">{t("v2.models.prices.note")}</span>
      </Alert>
      {missing > 0 && (
        <Alert tone="warn">
          <span data-testid="v2-prices-unpriced">{t("v2.models.prices.unpricedWarn", { count: missing })}</span>
        </Alert>
      )}
      {spans.error && <span className="v2-muted" data-testid="v2-prices-spans-error">{t("v2.models.prices.spansError")}</span>}
      <Card
        title={t("v2.models.prices.title")}
        sub={t("v2.models.prices.sub")}
        flush
        testId="v2-prices"
        end={
          <Button size="sm" kind="primary" onClick={() => setModal({ modelId: null, entry: null })} testId="v2-price-add">
            <Plus size={13} aria-hidden="true" />
            {t("v2.models.prices.add")}
          </Button>
        }
      >
        <div style={{ padding: "12px 24px 0" }}>
          <SearchInput value={query} onChange={setQuery} placeholder={t("v2.models.prices.search")} testId="v2-prices-search" />
        </div>
        <Table
          columns={columns}
          rows={paged.slice}
          rowKey={(row) => row.model_id}
          loading={table.loading}
          error={table.error}
          onRetry={table.reload}
          empty={t("v2.models.prices.empty")}
          testId="v2-prices-table"
        />
        {paged.pages > 1 && <Pager page={paged.page} pages={paged.pages} total={paged.total} onPage={paged.setPage} />}
      </Card>
      {modal && (
        <PriceModal
          key={modal.modelId ?? "new"}
          modelId={modal.modelId}
          entry={modal.entry}
          factors={factors}
          onClose={() => setModal(null)}
          onSaved={table.reload}
        />
      )}
      <Confirm
        open={toReset !== null}
        title={t("v2.models.prices.resetTitle")}
        body={
          <div className="v2-stack">
            <span className="mono">{toReset?.model_id}</span>
            <span>
              {toReset?.base
                ? t("v2.models.prices.resetBody", { input: fmtRate(toReset.base.input), output: fmtRate(toReset.base.output) })
                : t("v2.models.prices.resetBodyNone")}
            </span>
          </div>
        }
        confirmLabel={t("v2.models.reset")}
        busy={busy}
        onConfirm={() => void reset()}
        onClose={() => setToReset(null)}
      />
    </>
  );
}

/* ---------- AgentCore rates ---------- */

function RateModal({ row, onClose, onSaved }: { row: RateRow; onClose: () => void; onSaved: (view: AgentCoreRates) => void }) {
  const { t } = useTranslation();
  const toast = useV2Toast();
  const [value, setValue] = useState(() => amountToInput(row.value));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const parsed = amountFromInput(value, MAX_RATE);
  const name = t(`v2.models.rates.key.${row.key}`, { defaultValue: row.key });

  const write = async (next: number | null) => {
    setBusy(true);
    setError(null);
    try {
      const view = await api.putAgentcoreRates(ratePatch(row.group, row.key, next));
      toast("success", t(next === null ? "v2.models.rates.resetDone" : "v2.models.rates.saved", { name }));
      onSaved(view);
      onClose();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal
      title={name}
      open
      onClose={onClose}
      testId="v2-rate-modal"
      footer={
        <>
          <Button onClick={onClose}>{t("v2.common.cancel")}</Button>
          {row.changed && (
            <Button disabled={busy} onClick={() => void write(null)} testId="v2-rate-reset">
              <RotateCcw size={13} aria-hidden="true" /> {t("v2.models.reset")}
            </Button>
          )}
          <Button kind="primary" disabled={busy || parsed === null} onClick={() => void write(parsed)} testId="v2-rate-submit">
            {t("v2.common.save")}
          </Button>
        </>
      }
    >
      <div className="v2-form">
        {error && (
          <div data-testid="v2-rate-error">
            <Alert tone="error">{error}</Alert>
          </div>
        )}
        <Field
          label={row.group === "rates" ? t("v2.models.rates.valueUsd", { unit: t(unitKey(row.unit), { defaultValue: row.unit }) }) : t(unitKey(row.unit), { defaultValue: row.unit })}
          hint={row.default !== null ? t("v2.models.rates.defaultHint", { value: fmtRate(row.default, row.group === "rates") }) : undefined}
          error={value.trim() && parsed === null ? t("v2.models.rates.invalid") : null}
        >
          <input
            className="v2-input"
            inputMode="decimal"
            value={value}
            onChange={(e) => setValue(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && parsed !== null && void write(parsed)}
            data-testid="v2-rate-value"
          />
        </Field>
        <span className="mono v2-muted">{row.key}</span>
      </div>
    </Modal>
  );
}

function RateTable({ rows, onEdit, testId }: { rows: RateRow[]; onEdit: (row: RateRow) => void; testId: string }) {
  const { t } = useTranslation();
  const currency = (row: RateRow) => row.group === "rates";
  const unit = (row: RateRow) => t(unitKey(row.unit), { defaultValue: row.unit });
  const columns: Column<RateRow>[] = [
    {
      key: "item",
      title: t("v2.models.rates.colItem"),
      render: (row) => (
        <>
          <span>{t(`v2.models.rates.key.${row.key}`, { defaultValue: row.key })}</span>
          <span className="sub mono">{row.key}</span>
        </>
      ),
    },
    {
      key: "value",
      title: t("v2.models.rates.colValue"),
      className: "nowrap",
      render: (row) => (
        <span className="v2-row" style={{ gap: 6 }} data-testid={`v2-rate-value-${row.key}`}>
          <span>
            {fmtRate(row.value, currency(row))}
            <span className="v2-muted"> {currency(row) ? `/ ${unit(row)}` : unit(row)}</span>
          </span>
          {row.changed && <Tag tone="orange">{t("v2.models.overridden")}</Tag>}
        </span>
      ),
    },
    {
      key: "default",
      title: t("v2.models.rates.colDefault"),
      className: "nowrap",
      render: (row) => <span className="v2-muted">{fmtRate(row.default, currency(row))}</span>,
    },
    {
      key: "actions",
      title: t("v2.common.actions"),
      className: "nowrap",
      render: (row) => (
        <LinkButton onClick={() => onEdit(row)} testId={`v2-rate-edit-${row.key}`}>
          {t("v2.common.edit")}
        </LinkButton>
      ),
    },
  ];
  return <Table columns={columns} rows={rows} rowKey={(row) => `${row.group}:${row.key}`} testId={testId} />;
}

export function AgentCoreRatesView() {
  const { t } = useTranslation();
  const loaded = useLoad(() => api.agentcoreRates(), "v2-agentcore-rates");
  const [saved, setSaved] = useState<AgentCoreRates | null>(null);
  const [editing, setEditing] = useState<RateRow | null>(null);
  const view = saved ?? loaded.data;
  const groups: RateGroup[] = ["rates", "calibration"];

  return (
    <>
      <Alert tone="info">
        <span data-testid="v2-rates-note">{t("v2.models.rates.note")}</span>
      </Alert>
      {loaded.error && !view && (
        <Alert tone="error" action={<LinkButton onClick={loaded.reload}>{t("v2.common.retry")}</LinkButton>}>
          {loaded.error}
        </Alert>
      )}
      {view?.updated_at && (
        <span className="v2-muted">{t("v2.models.updated", { who: view.updated_by || "—", when: fmtTime(view.updated_at) })}</span>
      )}
      {groups.map((group) => (
        <Card
          key={group}
          title={t(`v2.models.rates.${group}Title`)}
          sub={t(`v2.models.rates.${group}Sub`)}
          flush
          testId={`v2-rates-${group}`}
        >
          {view ? (
            <RateTable rows={rateRows(view, group)} onEdit={setEditing} testId={`v2-rates-${group}-table`} />
          ) : (
            loaded.loading && <Spin />
          )}
        </Card>
      ))}
      {editing && <RateModal key={`${editing.group}:${editing.key}`} row={editing} onClose={() => setEditing(null)} onSaved={setSaved} />}
    </>
  );
}
