import { useState } from "react";
import { useTranslation } from "react-i18next";

import { api, type ModelCatalogChange } from "../../../lib/api";
import { fmtTime } from "../../format";
import { useLoad } from "../../hooks";
import { Button, Card, type Column, LinkButton, Table, Tag } from "../../ui";
import { changedKeys } from "./common";

const PAGE = 50;

function short(value: unknown): string {
  if (value === null || value === undefined) return "—";
  if (typeof value === "object") {
    const text = JSON.stringify(value);
    return text.length > 48 ? `${text.slice(0, 45)}…` : text;
  }
  return String(value);
}

function Summary({ change }: { change: ModelCatalogChange }) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const keys = changedKeys(change.before, change.after);
  const shown = keys.slice(0, 4);
  return (
    <div className="v2-mp-diffsum" data-testid={`v2-mp-change-${change.id}`}>
      {keys.length === 0 && <span className="v2-muted">{t("modelProviders.changes.noDiff")}</span>}
      {shown.map((k) => (
        <span key={k}>
          <code>{k}</code>: <code>{short(change.before?.[k])}</code> → <code>{short(change.after?.[k])}</code>
        </span>
      ))}
      {keys.length > shown.length && <span className="v2-muted">+{keys.length - shown.length}</span>}
      {(change.before || change.after) && (
        <LinkButton onClick={() => setOpen((v) => !v)}>{t(open ? "v2.common.collapse" : "v2.common.expand")}</LinkButton>
      )}
      {open && <pre className="v2-mp-json">{JSON.stringify({ before: change.before, after: change.after }, null, 2)}</pre>}
    </div>
  );
}

/**
 * `?view=changes`: the immutable `model_catalog_changes` journal, newest first (plan §2.3).
 * N17-obs203 §4: with `targetId` the same table narrowed to ONE provider's audit trail
 * (who · when · which action · the host / scope / key last4 that changed — never a key), shown
 * compact under the selected provider's models for administrators.
 */
export function Changes({ targetId, compact = false }: { targetId?: string; compact?: boolean } = {}) {
  const { t } = useTranslation();
  const [before, setBefore] = useState<number | undefined>(undefined);
  const [older, setOlder] = useState<ModelCatalogChange[]>([]);
  const pageSize = compact ? 10 : PAGE;
  const page = useLoad(() => api.modelProviders.changes(pageSize, before, targetId), `mp-changes:${targetId ?? "all"}:${before ?? "head"}`);
  const items = before === undefined ? (page.data?.items ?? []) : [...older, ...(page.data?.items ?? [])];
  const last = items[items.length - 1];
  const more = (page.data?.items.length ?? 0) >= pageSize;

  const columns: Column<ModelCatalogChange>[] = [
    { key: "version", title: t("modelProviders.changes.version"), className: "v2-mp-nowrap", render: (c) => <code>v{c.id}</code> },
    { key: "time", title: t("modelProviders.changes.time"), className: "v2-mp-nowrap", render: (c) => fmtTime(c.created_at) },
    { key: "actor", title: t("modelProviders.changes.actor"), render: (c) => c.actor ?? <span className="v2-muted">—</span> },
    {
      key: "op",
      title: t("modelProviders.changes.operation"),
      render: (c) => <Tag tone={c.operation.endsWith(".delete") ? "red" : c.operation.endsWith(".create") || c.operation === "seed" ? "green" : "blue"}>{c.operation}</Tag>,
    },
    { key: "target", title: t("modelProviders.changes.target"), render: (c) => <code>{c.target_id ?? "—"}</code> },
    { key: "summary", title: t("modelProviders.changes.summary"), render: (c) => <Summary change={c} /> },
  ];

  return (
    <Card
      title={t(compact ? "modelProviders.changes.providerTitle" : "modelProviders.changes.title")}
      sub={t(compact ? "modelProviders.changes.providerSub" : "modelProviders.changes.sub")}
      testId={compact ? "v2-mp-provider-changes" : "v2-mp-changes"}
    >
      <Table
        columns={columns}
        rows={items}
        rowKey={(c) => String(c.id)}
        loading={page.loading}
        error={page.error ? t("modelProviders.loadFailed", { msg: page.error }) : null}
        onRetry={page.reload}
        empty={t("modelProviders.changes.empty")}
        density="dense"
        testId="v2-mp-changes-table"
      />
      {more && last && (
        <div className="v2-row" style={{ justifyContent: "center", marginTop: 12 }}>
          <Button
            disabled={page.loading}
            onClick={() => {
              setOlder(items);
              setBefore(last.id);
            }}
            testId="v2-mp-changes-more"
          >
            {t("modelProviders.changes.loadMore")}
          </Button>
        </div>
      )}
    </Card>
  );
}
