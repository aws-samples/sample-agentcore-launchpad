import { Plus, Search, Share2, Trash2 } from "lucide-react";
import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link, useNavigate } from "react-router-dom";

import { ApiErrorDetail } from "../../components/ApiErrorDetail";
import { useAuth } from "../../auth/auth-context";
import { ErrorCallout } from "../../components/ErrorCallout";
import {
  api,
  ApiError,
  type AssistantConversationFootprint,
  type AssistantConversationSummary,
} from "../../lib/api";
import { methodLabel } from "../../components/methodChipMeta";
import { clearAgentList, clearOperationsText, isContextTrimmed, shortConversationTitle } from "../../lib/assistant";
import { useWorkspace } from "../../workspace/workspace-context";
import { useFailure } from "../failure";
import { ago } from "../format";
import { useLoad, useToast } from "../hooks";
import { ArchitectModelPanel } from "./assistant/ArchitectModel";
import { conversationSignal } from "../signals";
import { Btn, Chip, Confirm, Empty, Filters, Lamp, Notice, PageHead, Panel, type Signal, Skeleton, Stat } from "../ui";

const open = (id: string) => `/v2/assistant?view=detail&id=${encodeURIComponent(id)}`;

/** CLEAR a conversation and everything it created, with its footprint shown first. */
function useClear(onCleared: () => void) {
  const { t } = useTranslation();
  const toast = useToast();
  const failed = useFailure();
  const [target, setTarget] = useState<{ id: string; title: string; fp: AssistantConversationFootprint | null } | null>(null);
  const [busy, setBusy] = useState(false);

  const ask = async (c: AssistantConversationSummary) => {
    const title = c.title || c.id.slice(0, 8);
    setTarget({ id: c.id, title, fp: null });
    try {
      failed.clear();
      const fp = await api.assistantConversationFootprint(c.id);
      setTarget((cur) => (cur?.id === c.id ? { ...cur, fp } : cur));
    } catch (err) {
      setTarget(null);
      failed.fail(err);
    }
  };
  const fp = target?.fp ?? null;
  const cloud = fp ? fp.operations.filter((o) => o.status !== "cleaned") : [];
  const blocked = fp ? fp.blockers.length > 0 || !fp.can_clear : true;
  const run = async () => {
    if (!target || !fp || blocked) return;
    setBusy(true);
    try {
      failed.clear();
      const res = await api.assistantDeleteConversation(target.id);
      toast("ok", t("assistantPage.clear.doneToast", {
        title: target.title, agents: res.agents.length, operations: res.operations_cleaned.length, datasets: res.datasets.length,
      }));
      setTarget(null);
      onCleared();
    } catch (err) {
      failed.fail(err);
      setTarget(null);
    } finally {
      setBusy(false);
    }
  };
  const dialog = target ? (
    <Confirm
      title={t("assistantPage.clear.title", { title: shortConversationTitle(target.title, target.id.slice(0, 8)) })}
      confirmLabel={busy ? t("assistantPage.clear.working") : t("assistantPage.clear.confirm")}
      cancelLabel={t("v3.common.cancel")}
      danger
      busy={busy || !fp || blocked}
      onCancel={() => !busy && setTarget(null)}
      onConfirm={() => void run()}
    >
      {!fp ? (
        <Skeleton rows={2} />
      ) : (
        <div style={{ display: "grid", gap: 8 }}>
          <div>{t("assistantPage.clear.intro", { turns: fp.turns, proposals: fp.proposals })}</div>
          {fp.agents.length > 0 && <div>{t("assistantPage.clear.agents", { list: clearAgentList(fp, methodLabel) })}</div>}
          {cloud.length > 0 && (
            <div>{clearOperationsText(t, cloud)}</div>
          )}
          {fp.datasets.length > 0 && (
            <div>{t("assistantPage.clear.datasets", { list: fp.datasets.map((d) => `${d.name} (${t("assistantEval.items", { n: d.item_count })})`).join(", ") })}</div>
          )}
          {!fp.agents.length && !cloud.length && !fp.datasets.length && <div>{t("assistantPage.clear.onlyTranscript")}</div>}
          {fp.blockers.length > 0 ? (
            <Notice s="act">{t("assistantPage.clear.blockers", { list: fp.blockers.map((b) => b.reason).join("; ") })}</Notice>
          ) : !fp.can_clear ? (
            <Notice s="wait">{t("assistantPage.clear.noPermission")}</Notice>
          ) : (
            <Notice s="wait">{t(fp.agents.length || cloud.length ? "assistantPage.clear.irreversible" : "assistantPage.clear.irreversibleLocal")}</Notice>
          )}
        </div>
      )}
    </Confirm>
  ) : null;
  return { ask: (c: AssistantConversationSummary) => void ask(c), dialog, failed };
}

/**
 * The assistant's desk: every design conversation and where it stands — still
 * being talked through, a proposal waiting for review, approved into an agent.
 * A conversation itself opens in its working view (discussion → proposal →
 * deploy → evaluation assets), hosted from V2.
 */
export function V3Assistant() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const toast = useToast();
  const failed = useFailure();
  const { isAdmin } = useAuth();
  const { current } = useWorkspace();
  const ws = current?.id ?? "";
  const status = useLoad(() => api.assistantStatus(), `v3-assistant-status:${ws}`);
  const list = useLoad(() => api.assistantConversations(), `v3-assistant-list:${ws}`);
  const clear = useClear(() => list.reload());
  const [creating, setCreating] = useState(false);
  const [sharing, setSharing] = useState<string | null>(null);
  const [scope, setScope] = useState<"all" | "mine" | "shared">("all");
  const [state, setState] = useState<"all" | Signal>("all");
  const [q, setQ] = useState("");

  const all = useMemo(() => list.data?.conversations ?? [], [list.data]);
  const rows = useMemo(() => {
    const needle = q.trim().toLowerCase();
    return all
      .filter((c) => (scope === "mine" ? c.mine : scope === "shared" ? !c.mine || c.shared : true))
      .filter((c) => state === "all" || conversationSignal(c) === state)
      .filter((c) => !needle || `${c.title} ${c.id} ${c.owner}`.toLowerCase().includes(needle))
      .sort((a, b) => (b.updated_at ?? "").localeCompare(a.updated_at ?? ""));
  }, [all, scope, state, q]);
  const count = (s: Signal) => all.filter((c) => conversationSignal(c) === s).length;
  const live = all.filter((c) => c.turn_in_progress !== null).length;

  const s = status.data;
  const create = async () => {
    setCreating(true);
    try {
      failed.clear();
      const detail = await api.assistantCreateConversation();
      if (detail.catalog.warnings.length) failed.fail(t("assistantPage.catalogWarnings", { list: detail.catalog.warnings.join("; ") }));
      navigate(open(detail.id));
    } catch (err) {
      failed.fail(err);
      if (err instanceof ApiError && err.code === "assistant.unavailable") status.reload();
    } finally {
      setCreating(false);
    }
  };
  const share = async (c: AssistantConversationSummary) => {
    setSharing(c.id);
    try {
      failed.clear();
      const res = await api.assistantSetSharing(c.id, !c.shared);
      toast("ok", t(res.shared ? "v2.assistant.sharedToast" : "v2.assistant.unsharedToast"));
      list.reload();
    } catch (err) {
      failed.fail(err);
    } finally {
      setSharing(null);
    }
  };

  return (
    <div className="v3-reveal" style={{ display: "grid", gap: 16 }}>
      <PageHead
        eyebrow={t("v3.assistant.eyebrow")}
        title={t("v3.assistant.title")}
        sub={t("v3.assistant.sub")}
        end={
          <Btn kind="primary" busy={creating} disabled={!s?.available} disabledReason={t("story.reason.assistantUnavailable")} onClick={() => void create()}>
            <Plus size={14} /> {t("v3.assistant.new")}
          </Btn>
        }
      />

      <ErrorCallout error={failed.error} onDismiss={failed.clear} data-testid="v3-assistant-error" />
      <ErrorCallout error={clear.failed.error} onDismiss={clear.failed.clear} data-testid="v3-assistant-clear-error" />
      {status.error && <Notice s="act"><ApiErrorDetail error={status.failure ?? status.error} /></Notice>}
      {s && !s.available && (
        <Notice s="wait">
          {t("assistantPage.unavailableBody", { label: s.preset.label, status: t(`create.system.status.${s.preset.status}`) })}{" "}
          {isAdmin ? t("assistantPage.unavailableAdmin") : t("assistantPage.unavailableMember")}{" "}
          {isAdmin && <Link to="/agents/new" style={{ textDecoration: "underline" }}>{t("assistantPage.goToPresets")}</Link>}
        </Notice>
      )}

      {isAdmin && ws && <ArchitectModelPanel workspaceId={ws} />}

      <div className="v3-grid c4">
        <Panel><Stat label={t("v3.assistant.conversations")} value={list.data ? all.length : "—"} foot={s ? `${s.owner}` : undefined} /></Panel>
        <Panel signal={live ? "info" : undefined}><Stat label={t("v3.assistant.thinking")} value={list.data ? live : "—"} foot={t("v3.assistant.thinkingFoot")} /></Panel>
        <Panel signal={count("wait") ? "wait" : undefined}>
          <Stat label={t("v3.assistant.toReview")} value={list.data ? count("wait") : "—"} signal={count("wait") ? "wait" : undefined} foot={t("v3.assistant.toReviewFoot")} />
        </Panel>
        <Panel signal={count("ok") ? "ok" : undefined}><Stat label={t("v3.assistant.approved")} value={list.data ? count("ok") : "—"} foot={t("v3.assistant.approvedFoot")} /></Panel>
      </div>

      <div style={{ display: "flex", gap: 12, alignItems: "center", flexWrap: "wrap" }}>
        <Filters
          value={scope}
          onChange={setScope}
          options={[
            { value: "all", label: t("v3.assistant.scopeAll") },
            { value: "mine", label: t("v3.assistant.scopeMine") },
            { value: "shared", label: t("v3.assistant.scopeShared") },
          ]}
        />
        <span style={{ width: 1, height: 20, background: "var(--v3-line)" }} aria-hidden="true" />
        <Filters
          value={state}
          onChange={setState}
          options={[
            { value: "all", label: t("v3.assistant.anyState") },
            { value: "info", label: t("v3.assistant.talking"), s: "info", count: count("info") },
            { value: "wait", label: t("assistantPage.status.draft"), s: "wait", count: count("wait") },
            { value: "ok", label: t("assistantPage.status.approved"), s: "ok", count: count("ok") },
            { value: "act", label: t("assistantPage.status.invalid"), s: "act", count: count("act") },
          ]}
        />
        <div style={{ marginLeft: "auto", position: "relative", flex: "1 1 200px", maxWidth: 300 }}>
          <Search size={14} style={{ position: "absolute", left: 12, top: 11, color: "var(--v3-text-3)" }} aria-hidden="true" />
          <input className="v3-input" style={{ paddingLeft: 34 }} value={q} onChange={(e) => setQ(e.target.value)}
            placeholder={t("v3.assistant.search")} aria-label={t("v3.assistant.search")} />
        </div>
      </div>

      <Panel flush>
        {list.loading && !list.data ? (
          <div style={{ padding: 20 }}><Skeleton rows={5} /></div>
        ) : list.error ? (
          <div style={{ padding: 20 }}><Notice s="act"><ApiErrorDetail error={list.failure ?? list.error} /></Notice></div>
        ) : rows.length === 0 ? (
          <Empty title={all.length ? t("v3.assistant.none") : t("v3.assistant.empty")}>{!all.length && t("v3.assistant.emptySub")}</Empty>
        ) : (
          <table className="v3-table">
            <thead>
              <tr>
                <th />
                <th>{t("v3.assistant.conversation")}</th>
                <th>{t("v3.assistant.owner")}</th>
                <th>{t("v3.assistant.proposal")}</th>
                <th className="num">{t("v3.assistant.turns")}</th>
                <th className="num">{t("v3.assistant.updated")}</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {rows.map((c) => {
                const sig = conversationSignal(c);
                return (
                  <tr key={c.id} className="click" onClick={() => navigate(open(c.id))}>
                    <td style={{ width: 30 }}><Lamp s={sig} live={c.turn_in_progress !== null} /></td>
                    <td>
                      <div className="v3-name">
                        <div>
                          <b>{c.title || c.id.slice(0, 8)}</b>
                          <small>{c.id.slice(0, 8)} · {ago(c.created_at)}{c.shared ? ` · ${t("v2.assistant.shared")}` : ""}</small>
                        </div>
                      </div>
                    </td>
                    <td style={{ color: "var(--v3-text-2)" }}>{c.owner}</td>
                    <td>
                      {c.proposal_status ? (
                        <span style={{ display: "inline-flex", gap: 6, alignItems: "center" }}>
                          <Chip s={sig === "off" || sig === "info" ? undefined : sig}>{t(`assistantPage.status.${c.proposal_status}`)}</Chip>
                          <span className="mono" style={{ color: "var(--v3-text-3)" }}>r{c.proposal_revision}</span>
                        </span>
                      ) : (
                        <span style={{ color: "var(--v3-text-3)" }}>{c.turn_in_progress !== null ? t("assistantPage.streaming") : t("v2.assistant.noProposal")}</span>
                      )}
                    </td>
                    <td className="num">
                      {isContextTrimmed(c) && (
                        <span style={{ marginRight: 6 }}>
                          <Chip s="wait" title={t("v3.assistant.context.trimmedTip")}>{t("v3.assistant.context.trimmed")}</Chip>
                        </span>
                      )}
                      {c.turns}
                    </td>
                    <td className="num" style={{ color: "var(--v3-text-3)" }}>{ago(c.updated_at)}</td>
                    <td style={{ width: 1, whiteSpace: "nowrap" }} onClick={(e) => e.stopPropagation()}>
                      <div style={{ display: "flex", gap: 4, justifyContent: "flex-end" }}>
                        {isAdmin && (
                          <Btn size="sm" kind="ghost" disabled={sharing !== null} onClick={() => void share(c)}
                            title={t(c.shared ? "v2.assistant.unshare" : "v2.assistant.share")}>
                            <Share2 size={13} /> {t(c.shared ? "v2.assistant.unshare" : "v2.assistant.share")}
                          </Btn>
                        )}
                        {c.mine && (
                          <Btn size="sm" kind="ghost" onClick={() => clear.ask(c)} title={t("assistantPage.clear.action")}>
                            <Trash2 size={13} />
                          </Btn>
                        )}
                      </div>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </Panel>
      {clear.dialog}
    </div>
  );
}
