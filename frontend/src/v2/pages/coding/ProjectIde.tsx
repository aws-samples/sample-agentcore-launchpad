import { Pencil, Rocket } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link, useSearchParams } from "react-router-dom";

import { useAuth } from "../../../auth/auth-context";
import { ApiError, codingApi, type CodingProjectDetail, type CodingTurnRow, errorMessage } from "../../../lib/api";
import { budgetRefusalOf } from "../../../lib/budgets";
import { useLoad, useV2Toast } from "../../hooks";
import { refreshMyUsageIfLimited } from "../../myUsage";
import { Alert, Button, FlowHeader, LinkButton, Spin, Tag } from "../../ui";
import { affectsFiles, type CodingEventRecord, liveTurn, withLedgerTurns } from "./codingEvents";
import { ProjectStatusTag } from "./common";
import { Composer, type ComposerError } from "./Composer";
import { Conversation } from "./Conversation";
import { DeployModal } from "./DeployModal";
import { CHANGES_TAB, type ChangeFocus, EditorPane } from "./EditorPane";
import { FileTree } from "./FileTree";
import { fmtClock } from "./format";
import { loadPrefs, pickOffered, savePrefs } from "./prefs";
import { ProjectFormModal } from "./ProjectList";
import { RuntimeCard } from "./RuntimeCard";
import { useCodingStream } from "./useCodingStream";
import { useOpenFiles } from "./useOpenFiles";

/** Ledger rows by codex turn id: the project detail wins over a just-started row. */
function mergeRows(detail: CodingTurnRow[], pending: CodingTurnRow[]): CodingTurnRow[] {
  const known = new Set(detail.map((r) => r.turn_id));
  return [...detail, ...pending.filter((r) => !known.has(r.turn_id))];
}

/** The running turn's elapsed time. Its own component, so the one-second tick
 *  re-renders this label only, not the whole IDE (tree, editor, conversation). */
function Elapsed({ since }: { since: number }) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, []);
  return (
    <span className="v2-muted v2-coding-elapsed" data-testid="v2-coding-elapsed">
      {fmtClock(now - since)}
    </span>
  );
}

/**
 * One project's IDE: file tree | editor + diffs | conversation. A turn runs in the
 * coding runtime on its own; this page only watches it (the event stream) and can
 * interrupt it, so leaving or reloading never stops a turn — reopening replays the
 * history and resumes the live tail.
 */
export function ProjectIde({ id }: { id: string }) {
  const { t } = useTranslation();
  const toast = useV2Toast();
  const [, setParams] = useSearchParams();
  const { can } = useAuth();
  const runtime = useLoad(() => codingApi.runtime(), "coding-runtime");
  const detail = useLoad(
    () =>
      codingApi.getProject(id).then(
        (project): { project: CodingProjectDetail | null } => ({ project }),
        (err: unknown) => {
          if (err instanceof ApiError && err.code === "coding.project_not_found") return { project: null };
          throw err;
        },
      ),
    `coding-project:${id}`,
  );
  const project = detail.data?.project ?? null;
  const reloadDetail = detail.reload;
  const ready = runtime.data?.status === "ready";

  /* ---------- files: refresh after the agent touched them ---------- */
  const [filesVersion, setFilesVersion] = useState(0);
  const filesTimer = useRef<number | undefined>(undefined);
  const settleTimer = useRef<number | undefined>(undefined);
  const bumpFiles = useCallback((delay = 600) => {
    window.clearTimeout(filesTimer.current);
    filesTimer.current = window.setTimeout(() => setFilesVersion((v) => v + 1), delay);
  }, []);
  useEffect(
    () => () => {
      window.clearTimeout(filesTimer.current);
      window.clearTimeout(settleTimer.current);
    },
    [],
  );

  const onRecords = useCallback(
    (records: CodingEventRecord[]) => {
      if (records.some(affectsFiles)) bumpFiles();
      if (records.some((r) => r.method === "turn/completed" || r.method === "launchpad/turnAbandoned")) {
        // the backend settles the turn (usage, snapshot) as it sees the completion
        window.clearTimeout(settleTimer.current);
        settleTimer.current = window.setTimeout(reloadDetail, 1500);
        refreshMyUsageIfLimited();
      }
    },
    [bumpFiles, reloadDetail],
  );

  const stream = useCodingStream(id, ready && project !== null, onRecords);
  const [pending, setPending] = useState<CodingTurnRow[]>([]);
  const rows = useMemo(() => mergeRows(project?.turns ?? [], pending), [project, pending]);
  const view = useMemo(() => withLedgerTurns(stream.conversation, rows), [stream.conversation, rows]);
  const live = liveTurn(view);
  const [starting, setStarting] = useState(false);
  const running = live !== null || starting;

  // a new status frame that disagrees with the ledger row we hold → re-read the row
  // (read through a ref: a detail reload alone must not re-run this)
  const ledgerStatus = useRef(project?.status);
  ledgerStatus.current = project?.status;
  const streamStatus = stream.status;
  useEffect(() => {
    const status = streamStatus?.projectStatus;
    if (status && ledgerStatus.current && status !== ledgerStatus.current) reloadDetail();
  }, [streamStatus, reloadDetail]);

  /* ---------- editor tabs ---------- */
  const openFiles = useOpenFiles(id);
  const [active, setActive] = useState<string>(CHANGES_TAB);
  const [focus, setFocus] = useState<ChangeFocus | null>(null);
  const refreshAll = openFiles.refreshAll;
  useEffect(() => {
    if (filesVersion > 0) refreshAll();
  }, [filesVersion, refreshAll]);

  const closeTab = (path: string) => {
    const at = openFiles.files.findIndex((f) => f.path === path);
    if (active === path) {
      const rest = openFiles.files.filter((f) => f.path !== path);
      setActive(rest[Math.min(at, rest.length - 1)]?.path ?? CHANGES_TAB);
    }
    openFiles.close(path);
  };

  const save = async (path: string, overwrite = false) => {
    try {
      const result = await openFiles.save(path, overwrite);
      if (result === "saved") {
        toast("success", t("v2.coding.editor.saved", { path }));
        bumpFiles(0);
      }
    } catch (err) {
      toast("error", errorMessage(err));
    }
  };

  // stable, so the memoized conversation rows are not re-rendered by it
  const lastTurnId = useRef<string | null>(null);
  lastTurnId.current = view.turns[view.turns.length - 1]?.turnId ?? null;
  const openChange = useCallback((path: string, turnId: string) => {
    // the latest turn's files diff against its start; an older turn's against HEAD
    setFocus({ path, scope: lastTurnId.current === turnId ? "turn" : "worktree", nonce: Date.now() });
    setActive(CHANGES_TAB);
  }, []);

  /* ---------- composer ---------- */
  const stored = useMemo(() => loadPrefs(id), [id]);
  const [modelChoice, setModelChoice] = useState<string | null>(null);
  const [effortChoice, setEffortChoice] = useState<string | null>(null);
  const models = runtime.data?.models ?? [];
  const efforts = runtime.data?.efforts ?? [];
  const model = pickOffered(modelChoice ?? stored.model ?? project?.model, runtime.data?.default_model ?? "", models);
  const effort = pickOffered(effortChoice ?? stored.effort ?? project?.effort, runtime.data?.default_effort ?? "", efforts);
  const choose = (next: { model?: string; effort?: string }) => {
    if (next.model) setModelChoice(next.model);
    if (next.effort) setEffortChoice(next.effort);
    savePrefs(id, { model: next.model ?? model, effort: next.effort ?? effort });
  };
  const [composerError, setComposerError] = useState<ComposerError | null>(null);
  const [interrupting, setInterrupting] = useState(false);
  if (interrupting && !running) setInterrupting(false);

  const send = async (prompt: string): Promise<boolean> => {
    setStarting(true);
    setComposerError(null);
    try {
      const row = await codingApi.startTurn(id, { prompt, model, effort });
      setPending((prev) => [...prev, row]);
      if (stream.phase === "stopped") stream.restart();
      return true;
    } catch (err) {
      setComposerError({ text: errorMessage(err), budget: budgetRefusalOf(err) });
      if (err instanceof ApiError && err.code === "coding.turn_running") reloadDetail();
      return false;
    } finally {
      setStarting(false);
    }
  };

  const interrupt = async () => {
    setInterrupting(true);
    try {
      const res = await codingApi.interrupt(id);
      if (!res.interrupted) {
        setInterrupting(false);
        reloadDetail();
      }
    } catch (err) {
      setInterrupting(false);
      toast("error", errorMessage(err));
    }
  };

  const [deploying, setDeploying] = useState(false);
  const [editing, setEditing] = useState(false);
  const back = () => setParams({});

  /* ---------- render ---------- */
  if (!detail.data) {
    return (
      <>
        <FlowHeader title={t("v2.coding.title")} onBack={back} />
        {detail.error ? (
          <Alert tone="error" action={<LinkButton onClick={detail.reload}>{t("v2.common.retry")}</LinkButton>}>
            {detail.error}
          </Alert>
        ) : (
          <Spin />
        )}
      </>
    );
  }
  if (!project) {
    return (
      <>
        <FlowHeader title={t("v2.coding.title")} onBack={back} />
        <Alert tone="warn" action={<LinkButton onClick={back}>{t("v2.coding.ide.backToList")}</LinkButton>}>
          {t("v2.coding.ide.notFound", { id })}
        </Alert>
      </>
    );
  }

  const canDeploy = can("agents.deploy");
  const phaseNote =
    stream.phase === "retrying"
      ? t("v2.coding.stream.retrying")
      : stream.phase === "paused"
        ? t("v2.coding.stream.paused")
        : stream.phase === "connecting"
          ? t("v2.coding.stream.connecting")
          : null;

  return (
    <div className="v2-coding-ide" data-testid="v2-coding-ide">
      <FlowHeader
        title={
          <span className="v2-coding-title">
            {project.name}
            <ProjectStatusTag status={running ? "running" : "idle"} />
            {live && live.startedAt !== null && <Elapsed key={live.turnId} since={live.startedAt} />}
          </span>
        }
        onBack={back}
        end={
          <>
            {project.agent_id && (
              <Link className="v2-btn sm" to={`/v2/agents?view=detail&id=${encodeURIComponent(project.agent_id)}`} data-testid="v2-coding-agent-link">
                {t("v2.coding.ide.openAgent")}
              </Link>
            )}
            <Button size="sm" onClick={() => setEditing(true)}>
              <Pencil size={13} aria-hidden="true" />
              {t("v2.coding.projects.rename")}
            </Button>
            <Button
              kind="primary"
              size="sm"
              disabled={!ready || running || !canDeploy}
              title={!canDeploy ? t("v2.coding.deploy.noPermission") : running ? t("v2.coding.deploy.whileRunning") : undefined}
              onClick={() => setDeploying(true)}
              testId="v2-coding-deploy"
            >
              <Rocket size={13} aria-hidden="true" />
              {t(project.agent_id ? "v2.coding.deploy.redeploy" : "v2.coding.deploy.button")}
            </Button>
          </>
        }
      />
      {!ready && <RuntimeCard runtime={runtime.data} loading={runtime.loading} error={runtime.error} reload={runtime.reload} />}
      {ready && (
        <div className="v2-coding-grid">
          <section className="v2-coding-pane v2-coding-pane-tree">
            <FileTree
              projectId={id}
              version={filesVersion}
              activePath={active === CHANGES_TAB ? null : active}
              onOpen={(path) => setActive(openFiles.open(path))}
            />
          </section>
          <section className="v2-coding-pane v2-coding-pane-main">
            <EditorPane
              projectId={id}
              files={openFiles.files}
              active={active}
              onActivate={setActive}
              onClose={closeTab}
              onDraft={openFiles.setDraft}
              onSave={(path) => void save(path)}
              onReload={openFiles.reload}
              onOverwrite={(path) => void save(path, true)}
              onTogglePreview={openFiles.togglePreview}
              running={running}
              version={filesVersion}
              focus={focus}
            />
          </section>
          <section className="v2-coding-pane v2-coding-pane-chat">
            <div className="v2-coding-pane-head">
              <span>{t("v2.coding.conversation.title")}</span>
              {phaseNote && (
                <Tag tone="gray" testId="v2-coding-stream-phase">
                  {phaseNote}
                </Tag>
              )}
            </div>
            {stream.error && (stream.phase === "stopped" || stream.phase === "retrying") && (
              <Alert
                tone={stream.phase === "stopped" ? "error" : "warn"}
                action={
                  stream.phase === "stopped" ? <LinkButton onClick={stream.restart}>{t("v2.common.retry")}</LinkButton> : undefined
                }
              >
                {stream.error}
              </Alert>
            )}
            <Conversation view={view} onOpenChange={openChange} />
            <Composer
              models={models}
              efforts={efforts}
              model={model}
              effort={effort}
              onModel={(m) => choose({ model: m })}
              onEffort={(e) => choose({ effort: e })}
              running={live !== null}
              disabled={!ready}
              disabledReason={t("v2.coding.composer.runtimeNotReady")}
              sending={starting}
              interrupting={interrupting}
              error={composerError}
              onSend={send}
              onInterrupt={() => void interrupt()}
            />
          </section>
        </div>
      )}
      {deploying && (
        <DeployModal
          project={project}
          open
          onClose={() => setDeploying(false)}
          onDeployed={() => reloadDetail()}
        />
      )}
      {editing && (
        <ProjectFormModal
          open
          project={project}
          onClose={() => setEditing(false)}
          onSaved={() => {
            setEditing(false);
            toast("success", t("v2.coding.projects.saved"));
            reloadDetail();
          }}
        />
      )}
    </div>
  );
}
