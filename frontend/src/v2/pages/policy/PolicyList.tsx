import { Plus } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { useNavigate } from "react-router-dom";

import { useAuth } from "../../../auth/auth-context";
import { api, type Policy, type PolicyEngine, type PolicyEnforcementMode, type PolicyListResponse } from "../../../lib/api";
import { governanceError } from "../../../lib/governance";
import { fmtTime } from "../../format";
import { usePaged, useV2Toast } from "../../hooks";
import { Alert, Button, Card, type Column, Confirm, FilterSelect, Kpi, LinkButton, Pager, Table } from "../../ui";
import { engineHref, policyEditorHref } from "./common";
import { PolicyEnforcementTag, PolicyKindTag, PolicyStatusTag } from "./widgets";

/**
 * 策略 tab of one engine: the engine's policies with their lifecycle actions (new / edit /
 * publish / retire / delete) and, per ACTIVE policy, Global's `enforcementMode` toggle —
 * 生效 (denies) or 仅记录 (records what it would deny). Publishing is a plain confirmation:
 * no workspace id, no reason (Global parity; N17-obs505 / N17-obs506).
 */
export function PoliciesTab({
  engine,
  list,
  loading,
  error,
  reload,
}: {
  engine: PolicyEngine;
  list: PolicyListResponse | null;
  loading: boolean;
  error: string | null;
  reload: () => void;
}) {
  const { t } = useTranslation();
  const toast = useV2Toast();
  const navigate = useNavigate();
  const { can, isAdmin } = useAuth();
  const [status, setStatus] = useState("");
  const [confirm, setConfirm] = useState<{ action: "retire" | "delete" | "publish"; policy: Policy } | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const manage = can("policy.manage");
  const policies = list?.policies ?? [];
  const rows = policies.filter((p) => !status || p.status === status);
  const paged = usePaged(rows, 12);
  const active = policies.filter((p) => p.status === "active").length;
  const logOnly = policies.filter((p) => p.status === "active" && p.enforcement_mode === "log_only").length;
  const drafts = policies.filter((p) => p.draft_version != null && p.status !== "retired").length;

  const openEditor = (policyId?: string) => navigate(policyEditorHref(engine.id, policyId ?? null));

  const act = async () => {
    if (!confirm) return;
    setBusy(confirm.policy.id);
    try {
      if (confirm.action === "retire") await api.retirePolicy(engine.id, confirm.policy.id);
      else if (confirm.action === "delete") await api.deletePolicy(engine.id, confirm.policy.id);
      else await api.publishPolicy(engine.id, confirm.policy.id, {});
      toast("success", t(`v2.governance.policy.messages.${confirm.action}`, { name: confirm.policy.name }));
      reload();
    } catch (err) {
      toast("error", governanceError(err));
    } finally {
      setBusy(null);
      setConfirm(null);
    }
  };

  // Global's per-policy enforcementMode; an ACTIVE policy is re-installed on the engine at once
  const changeEnforcement = async (policy: Policy, mode: PolicyEnforcementMode) => {
    setBusy(policy.id);
    try {
      await api.setPolicyEnforcementMode(engine.id, policy.id, mode);
      toast("success", t("v2.governance.policy.enforcementToggle.saved"));
      reload();
    } catch (err) {
      toast("error", governanceError(err));
    } finally {
      setBusy(null);
    }
  };

  const columns: Column<Policy>[] = [
    {
      key: "name",
      title: t("v2.governance.policy.colName"),
      render: (p) => (
        <>
          <LinkButton onClick={() => openEditor(p.id)} testId={`v2-policy-open-${p.name}`}>
            <span className="mono">{p.name}</span>
          </LinkButton>
          {p.description && <span className="sub clip">{p.description}</span>}
        </>
      ),
    },
    {
      key: "status",
      title: t("v2.governance.colStatus"),
      render: (p) => (
        <div className="v2-tags">
          <PolicyStatusTag status={p.status} testId={`v2-policy-status-${p.name}`} />
          <PolicyKindTag kind={p.kind} testId={`v2-policy-kind-${p.name}`} />
        </div>
      ),
    },
    {
      key: "enforcement",
      title: t("v2.policy.colEnforcement"),
      render: (p) =>
        p.status === "retired" ? (
          <span className="v2-muted">—</span>
        ) : (
          <label className="v2-check" title={t(isAdmin ? "v2.governance.policy.enforcementToggle.help" : "v2.policy.attach.adminOnly")}>
            <input
              type="checkbox"
              checked={p.enforcement_mode === "log_only"}
              disabled={!isAdmin || busy !== null}
              onChange={(e) => void changeEnforcement(p, e.target.checked ? "log_only" : "active")}
              data-testid={`v2-policy-log-only-${p.name}`}
            />
            <PolicyEnforcementTag mode={p.enforcement_mode} testId={`v2-policy-enforcement-${p.name}`} />
          </label>
        ),
    },
    {
      key: "versions",
      title: t("v2.governance.policy.colVersion"),
      render: (p) => (
        <>
          <span className="mono">{p.active_version != null ? `v${p.active_version}` : "—"}</span>
          {p.draft_version != null && <span className="sub mono">{t("v2.governance.policy.draftVersion", { version: p.draft_version })}</span>}
        </>
      ),
    },
    { key: "updated", title: t("v2.governance.colUpdated"), className: "nowrap", render: (p) => fmtTime(p.updated_at) },
    {
      key: "ops",
      title: t("v2.common.actions"),
      className: "right",
      render: (p) => (
        <div className="v2-actions">
          <LinkButton onClick={() => openEditor(p.id)} testId={`v2-policy-edit-${p.name}`}>{t("v2.common.edit")}</LinkButton>
          {manage && p.draft_version != null && p.status !== "retired" && (
            <LinkButton onClick={() => setConfirm({ action: "publish", policy: p })} testId={`v2-policy-publish-${p.name}`}>
              {t("v2.governance.policy.publish")}
            </LinkButton>
          )}
          {manage && p.status === "active" && (
            <LinkButton danger onClick={() => setConfirm({ action: "retire", policy: p })} testId={`v2-policy-retire-${p.name}`}>
              {t("v2.governance.policy.retire")}
            </LinkButton>
          )}
          {manage && p.status !== "active" && (
            <LinkButton danger onClick={() => setConfirm({ action: "delete", policy: p })} testId={`v2-policy-delete-${p.name}`}>
              {t("v2.common.delete")}
            </LinkButton>
          )}
        </div>
      ),
    },
  ];

  return (
    <>
      {list && (
        <div className="v2-kpis">
          <Kpi label={t("v2.governance.policy.kpi.active")} value={active} tone={active > 0 ? "good" : undefined} />
          <Kpi label={t("v2.policy.kpi.logOnly")} value={logOnly} />
          <Kpi label={t("v2.governance.policy.kpi.drafts")} value={drafts} />
          <Kpi label={t("v2.policy.kpi.attachedGateways")} value={engine.attachments.length} tone={engine.attachments.length > 0 ? "good" : undefined} />
        </div>
      )}
      <Card title={t("v2.governance.policy.listTitle")} sub={t("v2.governance.policy.listSub")} testId="v2-policy-list">
        <div className="v2-toolbar">
          <Button kind="primary" disabled={!manage} onClick={() => openEditor()} testId="v2-policy-new">
            <Plus size={14} aria-hidden="true" />
            {t("v2.governance.policy.new")}
          </Button>
          <Button onClick={() => navigate(engineHref(engine.id, "simulate"))} testId="v2-policy-simulate">
            {t("v2.governance.policy.sim.title")}
          </Button>
          <Button onClick={reload} testId="v2-policy-list-refresh">{t("v2.common.refresh")}</Button>
          <FilterSelect
            label={t("v2.governance.colStatus")}
            value={status}
            allLabel={t("v2.common.all")}
            onChange={setStatus}
            options={["draft", "active", "retired"].map((value) => ({ value, label: t(`v2.governance.policy.status.${value}`) }))}
          />
          <div className="end">
            <span className="v2-count">{t("v2.common.total", { count: rows.length })}</span>
          </div>
        </div>
        {list && active === 0 && engine.attachments.length > 0 && <Alert tone="warn">{t("v2.policy.firstPolicyHint")}</Alert>}
        <Table
          columns={columns}
          rows={paged.slice}
          rowKey={(p) => p.id}
          loading={loading}
          error={error}
          onRetry={reload}
          empty={t("v2.governance.policy.empty")}
          testId="v2-policy-table"
        />
        <Pager page={paged.page} pages={paged.pages} total={paged.total} onPage={paged.setPage} />
      </Card>
      <Confirm
        open={confirm !== null}
        title={t(`v2.governance.policy.confirmTitle.${confirm?.action ?? "publish"}`)}
        body={t(`v2.governance.policy.confirmBody.${confirm?.action ?? "publish"}`, {
          name: confirm?.policy.name,
          version: confirm?.policy.draft_version,
        })}
        confirmLabel={t("v2.common.confirm")}
        danger={confirm?.action !== "publish"}
        busy={busy !== null}
        onConfirm={() => void act()}
        onClose={() => setConfirm(null)}
      />
    </>
  );
}
