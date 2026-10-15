// The Budgets tab of `/v2/costs` (admin): spend levels, every account's limit and spend
// this period, spend with no account, and the flat per-turn estimate for runtimes that
// report no usage. Nothing is enforced until a level exists (services/budgets.py).
import { RefreshCw, RotateCcw } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";

import {
  api,
  type BudgetLevelInfo,
  type BudgetPerson,
  type BudgetSettings,
  type BudgetUnattributed,
  errorMessage,
} from "../../../lib/api";
import { currentPeriod, fmtResetDate, fmtShare, fmtUsd, recentPeriods } from "../../../lib/budgets";
import { creatorLabel, fmtTime } from "../../format";
import { useLoad, usePaged, useV2Toast } from "../../hooks";
import {
  Alert,
  Button,
  Card,
  type Column,
  Confirm,
  Field,
  FilterSelect,
  Kpi,
  LinkButton,
  Modal,
  Pager,
  SearchInput,
  Table,
  Tag,
} from "../../ui";
import { useBasisLabel, useSourceLabel } from "../usage/labels";
import { SourceTable, UsageBar } from "../usage/UsageParts";
import { AssignFields } from "./BudgetAssign";
import {
  type AssignDraft,
  amountFromInput,
  amountToInput,
  assignDraftFrom,
  assignDraftValid,
  assignmentPayload,
  levelCreatePayload,
  levelDraftFrom,
  levelDraftValid,
  levelPatchPayload,
  type LevelDraft,
  MAX_TURN_ESTIMATE_USD,
  type PeopleSort,
  type PeopleSortKey,
  peopleSummary,
  sortPeople,
} from "./budgets";

/* ---------- levels ---------- */

function LevelModal({
  editing,
  onClose,
  onSaved,
}: {
  /** null: a new level */
  editing: BudgetLevelInfo | null;
  onClose: () => void;
  onSaved: () => void;
}) {
  const { t } = useTranslation();
  const toast = useV2Toast();
  const [draft, setDraft] = useState<LevelDraft>(() => levelDraftFrom(editing));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const valid = levelDraftValid(draft);
  const amountBad = !!draft.amount.trim() && amountFromInput(draft.amount) === null;

  const submit = async () => {
    if (busy || !valid) return;
    setBusy(true);
    setError(null);
    try {
      if (editing) {
        const patch = levelPatchPayload(editing, draft);
        if (Object.keys(patch).length) await api.updateBudgetLevel(editing.id, patch);
        toast("success", t("v2.budgets.levelSaved", { name: draft.name.trim() }));
      } else {
        await api.createBudgetLevel(levelCreatePayload(draft));
        toast("success", t("v2.budgets.levelCreated", { name: draft.name.trim() }));
      }
      onSaved();
      onClose();
    } catch (err) {
      // a taken name (409) is shown next to the inputs
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal
      title={t(editing ? "v2.budgets.editLevel" : "v2.budgets.newLevel")}
      open
      onClose={onClose}
      testId="v2-budget-level-modal"
      footer={
        <>
          <Button onClick={onClose}>{t("v2.common.cancel")}</Button>
          <Button kind="primary" disabled={busy || !valid} onClick={() => void submit()} testId="v2-budget-level-submit">
            {t(editing ? "v2.common.save" : "v2.common.create")}
          </Button>
        </>
      }
    >
      <div className="v2-form">
        {error && (
          <div data-testid="v2-budget-level-error">
            <Alert tone="error">{error}</Alert>
          </div>
        )}
        <Field label={t("v2.budgets.levelName")} required>
          <input
            className="v2-input"
            maxLength={64}
            value={draft.name}
            placeholder={t("v2.budgets.levelNamePlaceholder")}
            onChange={(e) => setDraft((d) => ({ ...d, name: e.target.value }))}
            data-testid="v2-budget-level-name"
          />
        </Field>
        <Field
          label={t("v2.budgets.monthlyBudget")}
          hint={t("v2.budgets.monthlyBudgetHint")}
          error={amountBad ? t("v2.budgets.amountInvalid") : null}
          required
        >
          <input
            className="v2-input"
            inputMode="decimal"
            value={draft.amount}
            onChange={(e) => setDraft((d) => ({ ...d, amount: e.target.value }))}
            onKeyDown={(e) => e.key === "Enter" && void submit()}
            data-testid="v2-budget-level-amount"
          />
        </Field>
        <Field label={t("v2.budgets.default")} hint={t("v2.budgets.defaultHint")}>
          <label className="v2-check">
            <input
              type="checkbox"
              checked={draft.isDefault}
              onChange={(e) => setDraft((d) => ({ ...d, isDefault: e.target.checked }))}
              data-testid="v2-budget-level-default"
            />
            {t("v2.budgets.makeDefault")}
          </label>
        </Field>
      </div>
    </Modal>
  );
}

function LevelsCard({
  levels,
  loading,
  error,
  onRetry,
  onChanged,
}: {
  levels: BudgetLevelInfo[];
  loading: boolean;
  error: string | null;
  onRetry: () => void;
  onChanged: () => void;
}) {
  const { t } = useTranslation();
  const toast = useV2Toast();
  const [modal, setModal] = useState<{ editing: BudgetLevelInfo | null } | null>(null);
  const [toDelete, setToDelete] = useState<BudgetLevelInfo | null>(null);
  const [deleteError, setDeleteError] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);

  const makeDefault = async (level: BudgetLevelInfo) => {
    setBusyId(level.id);
    try {
      await api.updateBudgetLevel(level.id, { is_default: !level.is_default });
      toast("success", t(level.is_default ? "v2.budgets.defaultCleared" : "v2.budgets.defaultSet", { name: level.name }));
      onChanged();
    } catch (err) {
      toast("error", errorMessage(err));
    } finally {
      setBusyId(null);
    }
  };

  const remove = async () => {
    if (!toDelete) return;
    setBusyId(toDelete.id);
    setDeleteError(null);
    try {
      await api.deleteBudgetLevel(toDelete.id);
      toast("success", t("v2.budgets.levelDeleted", { name: toDelete.name }));
      setToDelete(null);
      onChanged();
    } catch (err) {
      // 409 budget.level_in_use: stays open with the reason
      setDeleteError(errorMessage(err));
    } finally {
      setBusyId(null);
    }
  };

  const columns: Column<BudgetLevelInfo>[] = [
    {
      key: "name",
      title: t("v2.budgets.colLevel"),
      render: (row) => (
        <span className="v2-row" style={{ gap: 6 }}>
          <span>{row.name}</span>
          {row.is_default && (
            <Tag tone="blue" testId={`v2-budget-level-default-${row.id}`}>
              {t("v2.budgets.default")}
            </Tag>
          )}
        </span>
      ),
    },
    {
      key: "amount",
      title: t("v2.budgets.colMonthly"),
      className: "nowrap",
      render: (row) => fmtUsd(row.monthly_budget_usd),
    },
    {
      key: "users",
      title: t("v2.budgets.colAssigned"),
      className: "nowrap",
      render: (row) => (row.is_default ? t("v2.budgets.assignedPlusDefault", { count: row.user_count }) : row.user_count),
    },
    { key: "creator", title: t("v2.common.creator"), className: "nowrap", render: (row) => creatorLabel(row) },
    {
      key: "actions",
      title: t("v2.common.actions"),
      className: "nowrap",
      render: (row) => (
        <span className="v2-row" style={{ gap: 12 }}>
          <LinkButton onClick={() => setModal({ editing: row })} testId={`v2-budget-level-edit-${row.id}`}>
            {t("v2.common.edit")}
          </LinkButton>
          <LinkButton disabled={busyId === row.id} onClick={() => void makeDefault(row)} testId={`v2-budget-level-toggle-default-${row.id}`}>
            {t(row.is_default ? "v2.budgets.unsetDefault" : "v2.budgets.setDefault")}
          </LinkButton>
          <LinkButton
            danger
            disabled={busyId === row.id}
            onClick={() => {
              setDeleteError(null);
              setToDelete(row);
            }}
            testId={`v2-budget-level-delete-${row.id}`}
          >
            {t("v2.common.delete")}
          </LinkButton>
        </span>
      ),
    },
  ];

  return (
    <>
      <Card
        title={t("v2.budgets.levelsTitle")}
        sub={t("v2.budgets.levelsSub")}
        flush
        testId="v2-budget-levels"
        end={
          <Button size="sm" kind="primary" onClick={() => setModal({ editing: null })} testId="v2-budget-level-new">
            {t("v2.budgets.newLevel")}
          </Button>
        }
      >
        <Table
          columns={columns}
          rows={levels}
          rowKey={(row) => row.id}
          loading={loading}
          error={error}
          onRetry={onRetry}
          empty={t("v2.budgets.levelsEmpty")}
          testId="v2-budget-levels-table"
        />
      </Card>
      {modal && (
        <LevelModal key={modal.editing?.id ?? "new"} editing={modal.editing} onClose={() => setModal(null)} onSaved={onChanged} />
      )}
      <Confirm
        open={toDelete !== null}
        title={t("v2.budgets.deleteTitle", { name: toDelete?.name ?? "" })}
        body={
          <div className="v2-stack">
            <span>{t("v2.budgets.deleteBody")}</span>
            {toDelete && toDelete.user_count > 0 && !deleteError && (
              <Alert tone="warn">{t("v2.budgets.deleteAssigned", { count: toDelete.user_count })}</Alert>
            )}
            {toDelete?.is_default && <Alert tone="warn">{t("v2.budgets.deleteDefault")}</Alert>}
            {deleteError && (
              <div data-testid="v2-budget-level-delete-error">
                <Alert tone="error">{deleteError}</Alert>
              </div>
            )}
          </div>
        }
        confirmLabel={t("v2.common.delete")}
        danger
        busy={busyId !== null}
        onConfirm={() => void remove()}
        onClose={() => setToDelete(null)}
      />
    </>
  );
}

/* ---------- people ---------- */

function AssignModal({
  person,
  levels,
  onClose,
  onSaved,
}: {
  person: BudgetPerson;
  levels: BudgetLevelInfo[];
  onClose: () => void;
  onSaved: () => void;
}) {
  const { t } = useTranslation();
  const toast = useV2Toast();
  const [draft, setDraft] = useState<AssignDraft>(() => assignDraftFrom(person));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const valid = assignDraftValid(draft);

  const submit = async () => {
    if (busy || !valid) return;
    setBusy(true);
    setError(null);
    try {
      await api.assignBudget(person.user_id, assignmentPayload(draft));
      toast("success", t("v2.budgets.assigned", { name: person.username }));
      onSaved();
      onClose();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal
      title={t("v2.budgets.assignTitle", { name: person.username })}
      open
      onClose={onClose}
      testId="v2-budget-assign-modal"
      footer={
        <>
          <Button onClick={onClose}>{t("v2.common.cancel")}</Button>
          <Button kind="primary" disabled={busy || !valid} onClick={() => void submit()} testId="v2-budget-assign-submit">
            {t("v2.common.save")}
          </Button>
        </>
      }
    >
      {error && (
        <div data-testid="v2-budget-assign-error">
          <Alert tone="error">{error}</Alert>
        </div>
      )}
      <AssignFields role={person.role} draft={draft} onChange={setDraft} levels={levels} />
      {person.by_source.length > 0 && (
        <div style={{ marginTop: 18 }}>
          <SourceTable rows={person.by_source} testId="v2-budget-assign-sources" />
        </div>
      )}
    </Modal>
  );
}

function PeopleCard({
  people,
  levels,
  loading,
  error,
  onRetry,
  onChanged,
}: {
  people: BudgetPerson[];
  levels: BudgetLevelInfo[];
  loading: boolean;
  error: string | null;
  onRetry: () => void;
  onChanged: () => void;
}) {
  const { t } = useTranslation();
  const basisLabel = useBasisLabel();
  const sourceLabel = useSourceLabel();
  const [query, setQuery] = useState("");
  const [sort, setSort] = useState<PeopleSort>({ key: "used", dir: "desc" });
  const [editing, setEditing] = useState<BudgetPerson | null>(null);
  const rows = sortPeople(people, sort, query);
  const paged = usePaged(rows, 20);

  const onSort = (key: string) =>
    setSort((s) =>
      s.key === key
        ? { key: s.key, dir: s.dir === "asc" ? "desc" : "asc" }
        : { key: key as PeopleSortKey, dir: key === "person" ? "asc" : "desc" },
    );

  const columns: Column<BudgetPerson>[] = [
    {
      key: "person",
      title: t("v2.budgets.colPerson"),
      sortable: true,
      render: (row) => (
        <>
          <Link className="v2-link" to={`/v2/users?view=detail&user=${encodeURIComponent(row.username)}`}>
            {row.username}
          </Link>
          <span className="sub">
            {t(row.role === "admin" ? "v2.budgets.roleAdmin" : "v2.budgets.roleMember")}
            {row.status !== "active" ? ` · ${t(`v2.budgets.status.${row.status}`)}` : ""}
          </span>
        </>
      ),
    },
    {
      key: "level",
      title: t("v2.budgets.colLevel"),
      render: (row) => (
        <>
          <span>{row.level?.name ?? "—"}</span>
          <span className="sub">{basisLabel(row.basis, row.level?.name)}</span>
        </>
      ),
    },
    {
      key: "limit",
      title: t("v2.budgets.colLimit"),
      className: "nowrap",
      render: (row) => (row.unlimited ? <span className="v2-muted">{t("v2.usage.unlimited")}</span> : fmtUsd(row.limit_usd)),
    },
    { key: "used", title: t("v2.budgets.colUsed"), className: "nowrap", sortable: true, render: (row) => fmtUsd(row.used_usd) },
    {
      key: "remaining",
      title: t("v2.budgets.colRemaining"),
      sortable: true,
      render: (row) =>
        row.unlimited ? (
          <span className="v2-muted">—</span>
        ) : (
          <>
            <span className="nowrap">{fmtUsd(row.remaining_usd)}</span>
            <UsageBar totals={row} testId={`v2-budget-bar-${row.user_id}`} />
          </>
        ),
    },
    {
      key: "estimated",
      title: t("v2.budgets.colEstimated"),
      className: "nowrap",
      render: (row) => (row.used_usd > 0 ? fmtShare(row.estimated_share) : <span className="v2-muted">—</span>),
    },
    {
      key: "top",
      title: t("v2.budgets.colTopSource"),
      className: "nowrap",
      render: (row) => (row.top_source ? sourceLabel(row.top_source) : <span className="v2-muted">—</span>),
    },
    {
      key: "actions",
      title: t("v2.common.actions"),
      className: "nowrap",
      render: (row) => (
        <LinkButton onClick={() => setEditing(row)} testId={`v2-budget-assign-${row.user_id}`}>
          {t("v2.budgets.assign")}
        </LinkButton>
      ),
    },
  ];

  return (
    <>
      <Card title={t("v2.budgets.peopleTitle")} sub={t("v2.budgets.peopleSub")} flush testId="v2-budget-people">
        <div style={{ padding: "12px 24px 0" }}>
          <SearchInput value={query} onChange={setQuery} placeholder={t("v2.budgets.search")} testId="v2-budget-people-search" />
        </div>
        <Table
          columns={columns}
          rows={paged.slice}
          rowKey={(row) => row.user_id}
          loading={loading}
          error={error}
          onRetry={onRetry}
          sort={sort}
          onSort={onSort}
          empty={t("v2.budgets.peopleEmpty")}
          testId="v2-budget-people-table"
        />
        {paged.pages > 1 && <Pager page={paged.page} pages={paged.pages} total={paged.total} onPage={paged.setPage} />}
      </Card>
      {editing && (
        <AssignModal
          key={editing.user_id}
          person={editing}
          levels={levels}
          onClose={() => setEditing(null)}
          onSaved={onChanged}
        />
      )}
    </>
  );
}

function UnattributedCard({ rows }: { rows: BudgetUnattributed[] }) {
  const { t } = useTranslation();
  const sourceLabel = useSourceLabel();
  if (rows.length === 0) return null;
  const columns: Column<BudgetUnattributed>[] = [
    {
      key: "who",
      title: t("v2.budgets.colPrincipal"),
      render: (row) =>
        row.user_name ? (
          <span>{row.user_name}</span>
        ) : row.user_id ? (
          <span className="v2-muted">
            {t("v2.budgets.deletedAccount")} <span className="mono">{row.user_id}</span>
          </span>
        ) : (
          <span className="v2-muted">{t("v2.budgets.unknownPrincipal")}</span>
        ),
    },
    { key: "used", title: t("v2.budgets.colUsed"), className: "nowrap", render: (row) => fmtUsd(row.used_usd) },
    {
      key: "estimated",
      title: t("v2.budgets.colEstimated"),
      className: "nowrap",
      render: (row) => (row.used_usd > 0 ? fmtShare(row.estimated_share) : "—"),
    },
    {
      key: "top",
      title: t("v2.budgets.colTopSource"),
      className: "nowrap",
      render: (row) => (row.by_source[0] ? sourceLabel(row.by_source[0].source) : "—"),
    },
  ];
  return (
    <Card title={t("v2.budgets.unattributedTitle")} sub={t("v2.budgets.unattributedSub")} flush testId="v2-budget-unattributed">
      <Table
        columns={columns}
        rows={rows}
        rowKey={(row) => `${row.user_id ?? ""}:${row.user_name ?? ""}`}
        testId="v2-budget-unattributed-table"
      />
    </Card>
  );
}

/* ---------- settings ---------- */

function SettingsCard({ settings, onSaved }: { settings: BudgetSettings; onSaved: (next: BudgetSettings) => void }) {
  const { t } = useTranslation();
  const toast = useV2Toast();
  const [value, setValue] = useState(() => amountToInput(settings.opaque_turn_estimate_usd));
  const [busy, setBusy] = useState(false);
  const parsed = amountFromInput(value, MAX_TURN_ESTIMATE_USD);
  const changed = parsed !== null && Math.abs(parsed - settings.opaque_turn_estimate_usd) > 1e-12;
  const atDefault = Math.abs(settings.opaque_turn_estimate_usd - settings.defaults.opaque_turn_estimate_usd) < 1e-12;

  const write = async (next: number | null) => {
    setBusy(true);
    try {
      const saved = await api.putBudgetSettings({ opaque_turn_estimate_usd: next });
      toast("success", t(next === null ? "v2.budgets.estimateReset" : "v2.budgets.estimateSaved"));
      onSaved(saved);
      setValue(amountToInput(saved.opaque_turn_estimate_usd));
    } catch (err) {
      toast("error", errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Card title={t("v2.budgets.settingsTitle")} sub={t("v2.budgets.settingsSub")} testId="v2-budget-settings">
      <Field
        label={t("v2.budgets.turnEstimate")}
        hint={t("v2.budgets.turnEstimateHint", { value: fmtUsd(settings.defaults.opaque_turn_estimate_usd) })}
        error={value.trim() && parsed === null ? t("v2.budgets.estimateInvalid", { max: MAX_TURN_ESTIMATE_USD }) : null}
      >
        <span className="v2-row" style={{ gap: 8 }}>
          <input
            className="v2-input"
            style={{ maxWidth: 160 }}
            inputMode="decimal"
            value={value}
            onChange={(e) => setValue(e.target.value)}
            aria-label={t("v2.budgets.turnEstimate")}
            data-testid="v2-budget-turn-estimate"
          />
          <Button size="sm" kind="primary" disabled={busy || !changed} onClick={() => void write(parsed)} testId="v2-budget-turn-estimate-save">
            {t("v2.common.save")}
          </Button>
          {!atDefault && (
            <Button size="sm" disabled={busy} onClick={() => void write(null)} testId="v2-budget-turn-estimate-reset">
              <RotateCcw size={13} aria-hidden="true" /> {t("v2.budgets.resetDefault")}
            </Button>
          )}
        </span>
      </Field>
      {settings.updated_at && (
        <span className="v2-muted">{t("v2.budgets.updated", { who: settings.updated_by || "—", when: fmtTime(settings.updated_at) })}</span>
      )}
    </Card>
  );
}

/* ---------- the tab ---------- */

export function BudgetsView({ period, onPeriod }: { period: string; onPeriod: (period: string) => void }) {
  const { t } = useTranslation();
  const [nonce, setNonce] = useState(0);
  const levels = useLoad(() => api.budgetLevels(), `v2-budget-levels:${nonce}`);
  const people = useLoad(() => api.budgetPeople(period), `v2-budget-people:${period}:${nonce}`);
  const settingsLoad = useLoad(() => api.budgetSettings(), "v2-budget-settings");
  const [settings, setSettings] = useState<BudgetSettings | null>(null);
  const shownSettings = settings ?? settingsLoad.data;
  const reload = () => setNonce((n) => n + 1);

  const now = currentPeriod();
  const periods = recentPeriods();
  if (!periods.includes(period)) periods.unshift(period);
  const levelRows = levels.data?.levels ?? [];
  const peopleRows = people.data?.people ?? [];
  const unattributed = people.data?.unattributed ?? [];
  const summary = peopleSummary(peopleRows, unattributed);

  return (
    <>
      <div className="v2-toolbar">
        <FilterSelect
          label={t("v2.usage.period")}
          value={period}
          onChange={onPeriod}
          options={periods.map((p) => ({ value: p, label: p === now ? t("v2.usage.thisMonth", { period: p }) : p }))}
          testId="v2-budget-period"
        />
        <Button onClick={reload} disabled={people.loading} testId="v2-budget-refresh">
          <RefreshCw size={14} aria-hidden="true" />
          {t("v2.common.refresh")}
        </Button>
        {people.data && period === now && (
          <span className="end v2-muted">{t("v2.budgets.resetsOn", { date: fmtResetDate(people.data.resets_at) })}</span>
        )}
      </div>
      <Alert tone="info">
        <span data-testid="v2-budget-note">{t("v2.budgets.note")}</span>
      </Alert>
      {levels.data && levelRows.length === 0 && (
        <Alert tone="warn">
          <span data-testid="v2-budget-not-enforced">{t("v2.budgets.notEnforced")}</span>
        </Alert>
      )}
      {people.error && (
        <Alert tone="error" action={<LinkButton onClick={people.reload}>{t("v2.common.retry")}</LinkButton>}>
          <span data-testid="v2-budget-people-error">{people.error}</span>
        </Alert>
      )}
      <div className="v2-kpis">
        <Kpi label={t("v2.budgets.kpiTotal")} value={people.data ? fmtUsd(summary.totalUsd) : "—"} sub={period} testId="v2-budget-kpi-total" />
        <Kpi
          label={t("v2.budgets.kpiBudgeted")}
          value={people.data ? `${summary.budgeted} / ${peopleRows.length}` : "—"}
          sub={t("v2.budgets.kpiBudgetedSub")}
          testId="v2-budget-kpi-budgeted"
        />
        <Kpi
          label={t("v2.budgets.kpiOver")}
          value={people.data ? summary.over : "—"}
          sub={t("v2.budgets.kpiOverSub", { count: summary.warn })}
          tone={summary.over ? "bad" : undefined}
          testId="v2-budget-kpi-over"
        />
        <Kpi
          label={t("v2.budgets.kpiEstimated")}
          value={people.data ? fmtShare(summary.estimatedShare) : "—"}
          sub={t("v2.budgets.kpiEstimatedSub")}
          testId="v2-budget-kpi-estimated"
        />
      </div>
      <LevelsCard
        levels={levelRows}
        loading={levels.loading}
        error={levels.error}
        onRetry={levels.reload}
        onChanged={reload}
      />
      <PeopleCard
        people={peopleRows}
        levels={levelRows}
        loading={people.loading}
        error={people.error}
        onRetry={people.reload}
        onChanged={reload}
      />
      <UnattributedCard rows={unattributed} />
      {shownSettings ? (
        <SettingsCard key={shownSettings.updated_at ?? "default"} settings={shownSettings} onSaved={setSettings} />
      ) : (
        settingsLoad.error && (
          <Alert tone="error" action={<LinkButton onClick={settingsLoad.reload}>{t("v2.common.retry")}</LinkButton>}>
            {settingsLoad.error}
          </Alert>
        )
      )}
    </>
  );
}
