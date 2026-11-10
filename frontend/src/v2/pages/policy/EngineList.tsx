import { Plus } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { Link, useNavigate } from "react-router-dom";

import { useAuth } from "../../../auth/auth-context";
import { api, type PolicyEngine } from "../../../lib/api";
import { governanceError } from "../../../lib/governance";
import { fmtTime } from "../../format";
import { useLoad, usePaged, useV2Toast } from "../../hooks";
import { Button, Card, type Column, Field, Kpi, LinkButton, Modal, PageHeader, Pager, Table, Tag } from "../../ui";
import { ENGINE_NAME_RE, engineHref, gatewayPolicyHref } from "./common";
import { PolicyModeTag } from "./widgets";

/**
 * 策略引擎 list (Global's PolicyEngine resource: many per workspace). Each row names its
 * policies and the gateways it is attached to; CREATE opens the name / description dialog.
 */
export function EngineList() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const { can } = useAuth();
  const manage = can("policy.manage");
  const { data, loading, error, reload } = useLoad(
    () => api.listPolicyEngines().catch((err: unknown) => Promise.reject(new Error(governanceError(err)))),
    "policy-engines",
  );
  const [createOpen, setCreateOpen] = useState(false);
  const engines = data?.engines ?? [];
  const paged = usePaged(engines, 12);
  const attachedGateways = new Set(engines.flatMap((e) => e.attachments.map((a) => a.gateway_id))).size;

  const columns: Column<PolicyEngine>[] = [
    {
      key: "name",
      title: t("v2.policy.colEngine"),
      render: (e) => (
        <>
          <LinkButton onClick={() => navigate(engineHref(e.id))} testId={`v2-policy-engine-open-${e.name}`}>
            <span className="mono">{e.name}</span>
          </LinkButton>
          {e.description && <span className="sub clip">{e.description}</span>}
        </>
      ),
    },
    {
      key: "policies",
      title: t("v2.policy.colPolicies"),
      render: (e) => <span data-testid={`v2-policy-engine-count-${e.name}`}>{e.policy_count}</span>,
    },
    {
      key: "gateways",
      title: t("v2.policy.colAttached"),
      render: (e) =>
        e.attachments.length === 0 ? (
          <span className="v2-muted">{t("v2.policy.notAttached")}</span>
        ) : (
          <div className="v2-tags" data-testid={`v2-policy-engine-attachments-${e.name}`}>
            {e.attachments.map((a) => (
              <Link key={a.gateway_id} className="v2-governance-link" to={gatewayPolicyHref(a.gateway_id)} title={a.gateway_id}>
                {a.gateway_name ?? a.gateway_id}
                <PolicyModeTag mode={a.mode} />
              </Link>
            ))}
          </div>
        ),
    },
    { key: "created", title: t("v2.governance.colUpdated"), className: "nowrap", render: (e) => fmtTime(e.updated_at ?? e.created_at) },
    {
      key: "ops",
      title: t("v2.common.actions"),
      className: "right",
      render: (e) => (
        <div className="v2-actions">
          <LinkButton onClick={() => navigate(engineHref(e.id))} testId={`v2-policy-engine-view-${e.name}`}>{t("v2.common.view")}</LinkButton>
          <LinkButton onClick={() => navigate(engineHref(e.id, "gateways"))} testId={`v2-policy-engine-gateways-${e.name}`}>{t("v2.policy.tab.gateways")}</LinkButton>
        </div>
      ),
    },
  ];

  return (
    <>
      <PageHeader title={t("nav.policy")} desc={t("v2.policy.desc")} />
      {data && (
        <div className="v2-kpis">
          <Kpi label={t("v2.policy.kpi.engines")} value={engines.length} />
          <Kpi label={t("v2.policy.kpi.policies")} value={engines.reduce((n, e) => n + e.policy_count, 0)} />
          <Kpi label={t("v2.policy.kpi.attachedGateways")} value={attachedGateways} tone={attachedGateways > 0 ? "good" : undefined} />
        </div>
      )}
      <Card title={t("v2.policy.listTitle")} sub={t("v2.policy.listSub")} testId="v2-policy-engines">
        <div className="v2-toolbar">
          <Button kind="primary" disabled={!manage} onClick={() => setCreateOpen(true)} testId="v2-policy-engine-new">
            <Plus size={14} aria-hidden="true" />
            {t("v2.policy.newEngine")}
          </Button>
          <Button onClick={reload} testId="v2-policy-engines-refresh">{t("v2.common.refresh")}</Button>
          <div className="end">
            <span className="v2-count">{t("v2.common.total", { count: engines.length })}</span>
          </div>
        </div>
        <Table
          columns={columns}
          rows={paged.slice}
          rowKey={(e) => e.id}
          loading={loading}
          error={error}
          onRetry={reload}
          empty={t("v2.policy.empty")}
          testId="v2-policy-engine-table"
        />
        <Pager page={paged.page} pages={paged.pages} total={paged.total} onPage={paged.setPage} />
      </Card>
      <CreateEngineDialog
        open={createOpen}
        onClose={() => setCreateOpen(false)}
        onCreated={(engine) => {
          setCreateOpen(false);
          navigate(engineHref(engine.id));
        }}
      />
    </>
  );
}

/** `POST /api/policy-engines {name, description}` — the name follows Global's rule. */
export function CreateEngineDialog({
  open,
  onClose,
  onCreated,
}: {
  open: boolean;
  onClose: () => void;
  onCreated: (engine: PolicyEngine) => void;
}) {
  const { t } = useTranslation();
  const toast = useV2Toast();
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [busy, setBusy] = useState(false);
  const nameValid = ENGINE_NAME_RE.test(name);
  const submit = async () => {
    setBusy(true);
    try {
      const engine = await api.createPolicyEngine({ name, description: description.trim() || null });
      toast("success", t("v2.policy.messages.engineCreated", { name: engine.name }));
      setName("");
      setDescription("");
      onCreated(engine);
    } catch (err) {
      toast("error", governanceError(err));
    } finally {
      setBusy(false);
    }
  };
  return (
    <Modal
      open={open}
      title={t("v2.policy.newEngine")}
      onClose={onClose}
      testId="v2-policy-engine-create"
      footer={
        <>
          <Button onClick={onClose} testId="v2-policy-engine-create-cancel">{t("v2.common.cancel")}</Button>
          <Button kind="primary" disabled={!nameValid || busy} onClick={() => void submit()} testId="v2-policy-engine-create-ok">
            {t("v2.common.create")}
          </Button>
        </>
      }
    >
      <div className="v2-form">
        <Field label={t("v2.policy.engineName")} required error={name && !nameValid ? t("v2.governance.policy.invalidName") : null} hint={t("v2.policy.engineNameHint")}>
          <input className="v2-input mono" value={name} disabled={busy} onChange={(e) => setName(e.target.value)} data-testid="v2-policy-engine-name" />
        </Field>
        <Field label={t("v2.governance.description")}>
          <input className="v2-input" value={description} disabled={busy} onChange={(e) => setDescription(e.target.value)} data-testid="v2-policy-engine-description" />
        </Field>
        <Tag tone="outline">{t("v2.policy.engineCreateNote")}</Tag>
      </div>
    </Modal>
  );
}
