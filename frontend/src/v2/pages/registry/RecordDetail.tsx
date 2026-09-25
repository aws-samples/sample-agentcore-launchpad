import { Copy } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { useNavigate, useSearchParams } from "react-router-dom";

import { useAuth } from "../../../auth/auth-context";
import { type AgentInfo, api, errorMessage, type LiveAgentCard } from "../../../lib/api";
import { descriptorExcerpt, parseAgentCard, parseSkillDefinition, skillPath } from "../../../lib/registry";
import { fmtTime } from "../../format";
import { useLoad, useV2Toast } from "../../hooks";
import {
  Alert,
  Button,
  Card,
  type Column,
  Confirm,
  Descriptions,
  FlowHeader,
  LinkButton,
  Spin,
  Table,
  Tag,
} from "../../ui";
import { type RegistryRecord, statusLabel } from "./common";
import { SourceTag, StatusTag, SystemTag, TypeTag } from "./tags";

type Lifecycle = "submit" | "approve" | "reject" | "disable";

/** DRAFT → PENDING_APPROVAL → APPROVED, with REJECTED / DEPRECATED as side exits. */
function LifecycleFlow({ status }: { status: string }) {
  const { t } = useTranslation();
  const order = ["DRAFT", "PENDING_APPROVAL", "APPROVED"];
  const at =
    status === "REJECTED" ? 1 : status === "DEPRECATED" ? 2 : Math.max(0, order.indexOf(status));
  return (
    <div className="v2-stages v2-registry-flow" data-testid="v2-registry-flow">
      {order.map((step, i) => {
        const failed = i === at && status === "REJECTED";
        const retired = i === at && status === "DEPRECATED";
        const cls = failed ? "failed" : i < at || (i === at && step === "APPROVED" && !retired) ? "succeeded" : i === at ? "running" : "";
        const label = failed ? statusLabel(t, "REJECTED") : retired ? statusLabel(t, "DEPRECATED") : statusLabel(t, step);
        return (
          <div key={step} className={`v2-stage ${cls}`}>
            <span className="n">{i + 1}</span>
            <div className="b">
              <div className="t">{label}</div>
              <div className="d">{t(`v2.registry.flow.${failed ? "REJECTED" : retired ? "DEPRECATED" : step}`)}</div>
            </div>
          </div>
        );
      })}
    </div>
  );
}

function LiveCardBlock({ record, reason }: { record: RegistryRecord; reason: string | null }) {
  const { t } = useTranslation();
  const [state, setState] = useState<{ loading: boolean; data: LiveAgentCard | null; error: string | null } | null>(null);
  const read = async () => {
    setState({ loading: true, data: null, error: null });
    try {
      setState({ loading: false, data: await api.registryLiveAgentCard(record.record_id), error: null });
    } catch (err) {
      setState({ loading: false, data: null, error: errorMessage(err) });
    }
  };
  const data = state?.data;
  const diff = data?.diff;
  const ok = data?.status_code != null && data.status_code >= 200 && data.status_code < 300;
  return (
    <div className="v2-stack" data-testid="v2-registry-live-card">
      <div className="v2-sub-title">{t("v2.registry.liveCard")}</div>
      <div className="v2-row">
        <Button
          disabled={Boolean(reason) || Boolean(state?.loading)}
          title={reason ?? t("registry.drawer.liveCardHint")}
          onClick={() => void read()}
          testId="v2-registry-live-card-btn"
        >
          {state?.loading ? t("registry.drawer.liveCardReading") : t("v2.registry.liveCardRead")}
        </Button>
        {data && <Tag tone={ok ? "green" : "orange"}>{t("registry.drawer.liveCardStatus", { code: data.status_code ?? "—" })}</Tag>}
        {data && diff && (
          <Tag tone={diff.identical ? "green" : "orange"}>
            {diff.identical ? t("registry.drawer.liveCardIdentical") : t("registry.drawer.liveCardDrift")}
          </Tag>
        )}
        {reason && <span className="v2-muted" style={{ fontSize: 13 }}>{reason}</span>}
      </div>
      {!reason && !state && <span className="v2-muted" style={{ fontSize: 13 }}>{t("registry.drawer.liveCardHint")}</span>}
      {state?.error && <Alert tone="error">{t("registry.drawer.liveCardFailed", { msg: state.error })}</Alert>}
      {data && diff && !diff.identical && (
        <Alert tone="warn">
          <ul className="v2-list mono" data-testid="v2-registry-live-diff">
            {diff.fields.map((f) => (
              <li key={f.field}>
                {t("registry.drawer.liveCardField", {
                  field: f.field,
                  record: String(f.record ?? "—"),
                  live: String(f.live ?? "—"),
                })}
              </li>
            ))}
            {diff.skills_only_in_live.length > 0 && (
              <li>{t("registry.drawer.liveCardSkillsOnlyLive", { ids: diff.skills_only_in_live.join(", ") })}</li>
            )}
            {diff.skills_only_in_record.length > 0 && (
              <li>{t("registry.drawer.liveCardSkillsOnlyRecord", { ids: diff.skills_only_in_record.join(", ") })}</li>
            )}
          </ul>
        </Alert>
      )}
      {data && (
        <details>
          <summary className="v2-muted" style={{ cursor: "pointer", fontSize: 13 }}>
            {t("registry.drawer.liveCardJson")}
          </summary>
          <pre className="v2-pre" style={{ marginTop: 8, maxHeight: 280 }}>{JSON.stringify(data.card, null, 2)}</pre>
        </details>
      )}
    </div>
  );
}

type CardSkill = NonNullable<NonNullable<ReturnType<typeof parseAgentCard>>["skills"]>[number];

export function RecordDetail({ id }: { id: string }) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const [, setParams] = useSearchParams();
  const toast = useV2Toast();
  const { isAdmin } = useAuth();
  const [tick, setTick] = useState(0);
  const detail = useLoad(() => api.registryRecord(id), `registry-record:${id}:${tick}`);
  // Ledger agents keyed by the registry record they own — decides whether an A2A
  // record can serve a live card; `null` (list failed) leaves the call to the backend.
  const agents = useLoad<Map<string, AgentInfo> | null>(async () => {
    try {
      const { agents: list } = await api.listAgents();
      const byRecord = new Map<string, AgentInfo>();
      for (const a of list) if (a.registry_record_id && a.status !== "deleted") byRecord.set(a.registry_record_id, a);
      return byRecord;
    } catch {
      return null;
    }
  }, "registry-agents");
  const discoverable = useLoad(async () => {
    try {
      return new Set((await api.registryDiscoverable()).records.map((r) => r.record_id));
    } catch {
      return null;
    }
  }, `registry-discoverable:${tick}`);
  const [busy, setBusy] = useState(false);
  const [confirm, setConfirm] = useState<"disable" | "delete" | null>(null);
  const [reimporting, setReimporting] = useState(false);
  const [reimportError, setReimportError] = useState<string | null>(null);

  const record = detail.data;
  const back = () => setParams({});

  if (!record) {
    return (
      <>
        <FlowHeader title={t("v2.registry.detailTitle")} onBack={back} />
        {detail.error ? <Alert tone="error">{t("registry.edit.loadFailed", { msg: detail.error })}</Alert> : <Spin />}
      </>
    );
  }

  // A system-managed Skill's lifecycle is an administrator's call; content edits
  // are refused for everyone. The server enforces both — the UI only hides.
  const lifecycleAllowed = !record.system || isAdmin;
  const skillMeta = parseSkillDefinition(record);
  const card = parseAgentCard(record);
  const hidden = discoverable.data ? !discoverable.data.has(record.record_id) : false;

  const act = async (action: Lifecycle) => {
    setBusy(true);
    try {
      const updated = await api.registryAction(record.record_id, action);
      toast("success", t("v2.registry.actionDone", { name: record.name, status: statusLabel(t, updated.status) }));
      setTick((n) => n + 1);
    } catch (err) {
      toast("error", t("common.actionFailed", { msg: errorMessage(err) }));
    } finally {
      setBusy(false);
      setConfirm(null);
    }
  };

  const remove = async () => {
    setBusy(true);
    try {
      await api.registryDelete(record.record_id);
      toast("success", t("registry.deleted", { name: record.name }));
      setParams({});
    } catch (err) {
      toast("error", t("common.actionFailed", { msg: errorMessage(err) }));
      setBusy(false);
      setConfirm(null);
    }
  };

  const reimport = async () => {
    setReimporting(true);
    setReimportError(null);
    try {
      const updated = await api.registryReimport(record.record_id);
      toast("success", t("registry.drawer.reimportOk", { name: updated.name }));
      setTick((n) => n + 1);
    } catch (err) {
      setReimportError(t("registry.drawer.reimportFailed", { msg: errorMessage(err) }));
    } finally {
      setReimporting(false);
    }
  };

  // "Use in new agent" prefills the classic agent wizard (?gateway= / ?skill=).
  const useInAgent = () => {
    if (record.type === "MCP") navigate(`/agents/new?gateway=${encodeURIComponent(record.name)}`);
    else if (record.type === "AGENT_SKILLS") navigate(`/agents/new?skill=${encodeURIComponent(skillPath(record))}`);
  };

  const liveCardReason = (() => {
    if (record.type !== "A2A") return t("registry.drawer.liveCardNotLaunchpad");
    if (agents.data === null || agents.data === undefined) return null;
    const agent = agents.data.get(record.record_id);
    if (!agent) return t("registry.drawer.liveCardNotLaunchpad");
    if (agent.spec.protocol !== "a2a") return t("registry.drawer.liveCardNotA2A");
    if (agent.status !== "active" || !agent.arn) return t("registry.drawer.liveCardNotReady", { status: agent.status });
    return null;
  })();

  const canEdit = record.type !== "A2A" && record.status !== "DEPRECATED" && !record.system;
  const canReimport =
    record.type === "AGENT_SKILLS" &&
    !record.system &&
    (skillMeta?.source?.kind === "git" || skillMeta?.source?.kind === "url") &&
    record.status !== "DEPRECATED";
  const approved = record.status === "APPROVED";

  const lifecycleButtons = lifecycleAllowed ? (
    <>
      {record.status === "DRAFT" && (
        <Button kind="primary" disabled={busy} onClick={() => void act("submit")} testId="v2-registry-submit">
          {t("v2.registry.action.submit")}
        </Button>
      )}
      {(record.status === "PENDING_APPROVAL" || record.status === "REJECTED") && (
        <Button kind="primary" disabled={busy} onClick={() => void act("approve")} testId="v2-registry-approve">
          {t("v2.registry.action.approve")}
        </Button>
      )}
      {record.status === "PENDING_APPROVAL" && (
        <Button disabled={busy} onClick={() => void act("reject")} testId="v2-registry-reject">
          {t("v2.registry.action.reject")}
        </Button>
      )}
      {approved && (
        <Button disabled={busy} onClick={() => setConfirm("disable")} testId="v2-registry-disable">
          {t("v2.registry.action.disable")}
        </Button>
      )}
      {record.status === "DEPRECATED" && <span className="v2-muted" style={{ fontSize: 13 }}>{t("v2.registry.deprecatedNote")}</span>}
    </>
  ) : (
    <span className="v2-muted" style={{ fontSize: 13 }} data-testid="v2-registry-member-readonly">
      {t("registry.drawer.system.memberReadOnly")}
    </span>
  );

  const skillColumns: Column<CardSkill>[] = [
    { key: "name", title: t("v2.registry.col.name"), render: (s) => <b>{s.name ?? s.id}</b> },
    { key: "desc", title: t("v2.registry.col.description"), render: (s) => s.description || "—" },
    {
      key: "tags",
      title: t("v2.registry.col.tags"),
      render: (s) => (
        <span className="v2-tags">
          {(s.tags ?? []).map((tg) => (
            <Tag key={tg} tone="outline">{tg}</Tag>
          ))}
        </span>
      ),
    },
  ];

  return (
    <>
      <FlowHeader
        title={
          <span className="v2-row">
            {record.name}
            <StatusTag status={record.status} />
            {record.system && <SystemTag />}
            {hidden && <Tag tone="orange" title={t("registry.consumer.notDiscoverableHint")}>{t("v2.registry.notDiscoverable")}</Tag>}
          </span>
        }
        onBack={back}
        end={
          <>
            {record.type !== "A2A" && (
              <Button
                kind="primary"
                disabled={!approved}
                title={approved ? undefined : t("registry.drawer.useNeedsApproved")}
                onClick={useInAgent}
                testId="v2-registry-use"
              >
                {t("v2.registry.useInAgent")}
              </Button>
            )}
            {canEdit && (
              <Button onClick={() => setParams({ view: "edit", id: record.record_id })} testId="v2-registry-edit">
                {t("v2.common.edit")}
              </Button>
            )}
            {!record.system && (
              <Button kind="danger" disabled={busy} onClick={() => setConfirm("delete")} testId="v2-registry-delete">
                {t("v2.common.delete")}
              </Button>
            )}
          </>
        }
      />
      {record.status_reason && <Alert tone={record.status === "REJECTED" ? "error" : "info"}>{record.status_reason}</Alert>}

      <Card title={t("v2.registry.basic")}>
        <Descriptions
          items={[
            { label: t("v2.registry.col.name"), value: record.name },
            { label: t("v2.registry.col.type"), value: <TypeTag type={record.type} /> },
            { label: t("v2.registry.recordId"), value: <span className="mono">{record.record_id}</span> },
            { label: t("v2.registry.col.version"), value: <span className="mono">{record.version ?? "—"}</span> },
            { label: t("v2.common.createdAt"), value: fmtTime(record.created_at) },
            { label: t("v2.registry.col.updated"), value: fmtTime(record.updated_at) },
            { label: t("v2.registry.col.description"), value: record.description || "—" },
            {
              label: t("v2.registry.discoverability"),
              value:
                discoverable.data == null
                  ? "—"
                  : hidden
                    ? t("v2.registry.notDiscoverable")
                    : t("v2.registry.discoverableYes"),
            },
          ]}
        />
      </Card>

      <Card title={t("v2.registry.lifecycle")} sub={t("v2.registry.lifecycleSub")} end={<span className="v2-row">{lifecycleButtons}</span>}>
        <LifecycleFlow status={record.status} />
        <p className="v2-muted" style={{ margin: 0, fontSize: 13 }}>
          {t(record.type === "A2A" ? "v2.registry.lifecycleNoteA2A" : "registry.drawer.useNeedsApproved")}
        </p>
      </Card>

      {record.system && (
        <Card title={t("v2.registry.systemTitle")} testId="v2-registry-system">
          <Alert>{t(isAdmin ? "registry.drawer.system.noteAdmin" : "registry.drawer.system.noteMember")}</Alert>
          <Descriptions
            items={[
              { label: t("v2.registry.systemPreset"), value: record.system.label },
              {
                label: t("v2.registry.systemRelease"),
                value: (
                  <span className="mono">
                    {record.system.skill_version ?? "—"}
                    {record.system.release_digest ? ` · ${record.system.release_digest}` : ""}
                  </span>
                ),
              },
              { label: t("v2.registry.systemPath"), value: <span className="mono">{record.system.path ?? "—"}</span> },
            ]}
          />
        </Card>
      )}

      {card && (
        <Card title={t("v2.registry.agentCard")} testId="v2-registry-agent-card">
          <Descriptions
            items={[
              {
                label: t("v2.registry.cardTransport"),
                value: (() => {
                  const transport = card.metadata?.["launchpad.transport"];
                  return <Tag tone={transport === "a2a-jsonrpc" ? "green" : "gray"}>{transport ?? "—"}</Tag>;
                })(),
              },
              { label: t("v2.registry.cardStreaming"), value: card.capabilities?.streaming ? t("v2.common.yes") : t("v2.common.no") },
              {
                label: t("v2.registry.cardUrl"),
                value: card.url ? (
                  <span className="v2-row" style={{ flexWrap: "nowrap" }}>
                    <span className="mono" style={{ wordBreak: "break-all" }}>{card.url}</span>
                    <LinkButton
                      title={t("registry.drawer.cardUrlCopy")}
                      onClick={() => {
                        void navigator.clipboard?.writeText(card.url ?? "").catch(() => undefined);
                        toast("success", t("registry.drawer.cardUrlCopied"));
                      }}
                    >
                      <Copy size={13} aria-hidden="true" />
                    </LinkButton>
                  </span>
                ) : (
                  "—"
                ),
              },
              { label: t("v2.registry.cardVersion"), value: <span className="mono">{card.version ?? "—"}</span> },
            ]}
          />
          {(card.skills ?? []).length > 0 && (
            <>
              <div className="v2-sub-title">{t("v2.registry.cardSkills", { n: (card.skills ?? []).length })}</div>
              <Table columns={skillColumns} rows={card.skills ?? []} rowKey={(s) => s.id ?? s.name ?? ""} density="dense" />
            </>
          )}
          <LiveCardBlock record={record} reason={liveCardReason} />
        </Card>
      )}

      {record.type === "AGENT_SKILLS" && (
        <Card
          title={t("v2.registry.bundle")}
          testId="v2-registry-bundle"
          end={
            <span className="v2-row">
              {/* Any status is evaluable — a fresh DRAFT skill is exactly what an
                  author wants to score before publishing it. */}
              <Button
                size="sm"
                onClick={() => navigate(`/v2/skill-lab?tab=eval&view=new&record=${encodeURIComponent(record.record_id)}`)}
                testId="v2-registry-skill-lab"
              >
                {t("v2.registry.evaluateInSkillLab")}
              </Button>
              {canReimport && (
                <Button size="sm" disabled={busy || reimporting} onClick={() => void reimport()} testId="v2-registry-reimport">
                  {reimporting ? t("registry.drawer.reimporting") : t("v2.registry.reimport")}
                </Button>
              )}
            </span>
          }
        >
          {reimportError && <Alert tone="error">{reimportError}</Alert>}
          <Descriptions
            items={[
              { label: t("v2.registry.source.label"), value: skillMeta?.source ? <SourceTag kind={skillMeta.source.kind} /> : "—" },
              { label: t("v2.registry.mountPath"), value: <span className="mono">{skillPath(record)}</span> },
              ...(skillMeta?.source?.url
                ? [{ label: t("v2.registry.sourceUrl"), value: <span className="mono">{skillMeta.source.url}</span> }]
                : []),
              ...(skillMeta?.source?.kind === "git"
                ? [
                    {
                      label: t("v2.registry.sourceCommit"),
                      // `ref` is what was asked for and may be a branch; the commit is the
                      // revision this record actually carries.
                      value: <span className="mono">{skillMeta.source.commit ?? t("registry.drawer.commitUnknown")}</span>,
                    },
                    { label: t("v2.registry.sourceRef"), value: <span className="mono">{skillMeta.source.ref ?? "—"}</span> },
                  ]
                : []),
            ]}
          />
          {skillMeta && skillMeta.files.length > 0 && (
            <>
              <div className="v2-sub-title">{t("v2.registry.files", { n: skillMeta.files.length })}</div>
              <pre className="v2-pre" style={{ maxHeight: 200 }} data-testid="v2-registry-files">{skillMeta.files.join("\n")}</pre>
            </>
          )}
        </Card>
      )}

      <Card title={t("v2.registry.descriptor")}>
        <pre className="v2-pre" style={{ maxHeight: 320 }}>{descriptorExcerpt(record)}</pre>
      </Card>

      <Confirm
        open={confirm === "disable"}
        title={t("v2.registry.disableTitle")}
        body={t("registry.confirmDisable.body", { name: record.name })}
        confirmLabel={t("v2.registry.action.disable")}
        danger
        busy={busy}
        onConfirm={() => void act("disable")}
        onClose={() => setConfirm(null)}
      />
      <Confirm
        open={confirm === "delete"}
        title={t("v2.registry.deleteTitle")}
        body={t("registry.confirmDelete.body", { name: record.name })}
        confirmLabel={t("v2.common.delete")}
        danger
        busy={busy}
        onConfirm={() => void remove()}
        onClose={() => setConfirm(null)}
      />
    </>
  );
}
