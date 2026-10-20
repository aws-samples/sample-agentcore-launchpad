import { Download, Loader2, Pencil, Rocket } from "lucide-react";
import { type KeyboardEvent, type MouseEvent, useCallback, useEffect, useId, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link, useSearchParams } from "react-router-dom";

import { useAuth } from "../../../auth/auth-context";
import { ApiError, codingApi, type CodingProjectDetail, type CodingTurnRow, errorMessage } from "../../../lib/api";
import { budgetRefusalOf } from "../../../lib/budgets";
import { useLoad, useV2Toast } from "../../hooks";
import { refreshMyUsageIfLimited } from "../../myUsage";
import { Alert, Button, FlowHeader, LinkButton, Spin, Tag } from "../../ui";
import { affectsFiles, type CodingEventRecord, liveTurn, turnChangedPaths, withLedgerTurns } from "./codingEvents";
import { ProjectStatusTag } from "./common";
import { Composer, type ComposerError } from "./Composer";
import { Conversation } from "./Conversation";
import { DeployModal } from "./DeployModal";
import { CHANGES_TAB, type ChangeFocus, EditorPane, type LineReveal } from "./EditorPane";
import { FileTree } from "./FileTree";
import { fmtClock } from "./format";
import { PaneRail, PaneResizer, PanelToggle, type RailBadge } from "./IdePanels";
import { type Panel, WORKBENCH_COMPACT } from "./layout";
import { loadPrefs, pickOffered, savePrefs } from "./prefs";
import { ProjectFormModal } from "./ProjectList";
import { RuntimeCard } from "./RuntimeCard";
import { useCodingDownload } from "./useCodingDownload";
import { useCodingStream } from "./useCodingStream";
import { useIdeLayout } from "./useIdeLayout";
import { isDirty, useOpenFiles } from "./useOpenFiles";

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
 * One project's IDE: Files · Conversation (center) · Workbench (editor tabs + diffs).
 * The side panels collapse to rails and resize (`useIdeLayout`); opening a file from
 * anywhere brings the Workbench back. A turn runs in the coding runtime on its own; this
 * page only watches it (the event stream) and can interrupt it, so leaving or reloading
 * never stops a turn — reopening replays the history and resumes the live tail.
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

  /* ---------- layout: side panels ---------- */
  const ide = useIdeLayout();
  const { layout, overlay, open: showPanel, close: hidePanel, closeOverlay } = ide;
  const filesId = useId();
  const benchId = useId();
  const [resizing, setResizing] = useState(false);
  const gridEl = useRef<HTMLDivElement | null>(null);
  const attachGrid = ide.gridRef;
  const setGrid = useCallback(
    (el: HTMLDivElement | null) => {
      gridEl.current = el;
      attachGrid(el);
    },
    [attachGrid],
  );
  // focus follows a toggle (its button disappears with the panel or the rail)
  const focusNext = useRef<string | null>(null);
  useEffect(() => {
    const target = focusNext.current;
    if (!target) return;
    focusNext.current = null;
    gridEl.current?.querySelector<HTMLElement>(`[data-focus="${target}"]`)?.focus();
  });
  const collapsePanel = (panel: Panel) => {
    hidePanel(panel);
    focusNext.current = `${panel}-rail`;
  };
  const railClick = (panel: Panel) => {
    if (overlay === panel) {
      hidePanel(panel);
      return;
    }
    showPanel(panel);
    focusNext.current = `${panel}-toggle`;
  };
  const overlayKeyDown = (panel: Panel) => (e: KeyboardEvent<HTMLElement>) => {
    // Esc closes the overlay — not when the editor or a dropdown used the key
    if (e.key !== "Escape" || e.defaultPrevented || overlay !== panel) return;
    if (e.target instanceof Element && e.target.closest(".monaco-editor")) return;
    e.preventDefault();
    collapsePanel(panel);
  };
  // a click anywhere else in the IDE closes the overlay. Capture phase: a click that
  // opens a file (tree, message link, file-change card) runs its own handler after
  // this one in the same batch, so the Workbench overlay stays without a flicker
  const overlayOutside = (e: MouseEvent<HTMLDivElement>) => {
    if (overlay === null || !(e.target instanceof Node)) return;
    const own = [
      document.getElementById(overlay === "files" ? filesId : benchId),
      gridEl.current?.querySelector(`[data-focus="${overlay}-rail"]`),
    ];
    if (!own.some((el) => el?.contains(e.target as Node))) closeOverlay();
  };

  /* ---------- editor tabs ---------- */
  const openFiles = useOpenFiles(id);
  const [active, setActive] = useState<string>(CHANGES_TAB);
  const [focus, setFocus] = useState<ChangeFocus | null>(null);
  const [reveal, setReveal] = useState<LineReveal | null>(null);
  const revealNonce = useRef(0);
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
    setReveal((r) => (r?.path === path ? null : r));
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
  const openChange = useCallback(
    (path: string, turnId: string) => {
      // the latest turn's files diff against its start; an older turn's against HEAD
      setFocus({ path, scope: lastTurnId.current === turnId ? "turn" : "worktree", nonce: Date.now() });
      setActive(CHANGES_TAB);
      showPanel("workbench");
    },
    [showPanel],
  );

  // a Markdown link into the project (agent text, a `.md` preview): open that file,
  // at the linked line when there is one (stable: the conversation rows are memoized)
  const openTab = openFiles.open;
  const openFile = useCallback(
    (path: string, line: number | null = null, column: number | null = null) => {
      const opened = openTab(path);
      setActive(opened);
      setReveal(line === null ? null : { path: opened, line, column, nonce: ++revealNonce.current });
      showPanel("workbench");
    },
    [openTab, showPanel],
  );
  const revealed = useCallback((nonce: number) => setReveal((r) => (r?.nonce === nonce ? null : r)), []);

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

  /* ---------- downloads (a file, or the project as a zip) ---------- */
  const { download, pending: downloadPending } = useCodingDownload();
  const downloadFile = useCallback((path: string) => void download(id, path), [download, id]);
  const fileDownloading = useCallback((path: string) => downloadPending(id, path), [downloadPending, id]);
  const zipping = downloadPending(id);

  const lastTurn = view.turns[view.turns.length - 1];
  const changedCount = useMemo(() => turnChangedPaths(lastTurn).length, [lastTurn]);

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

  const filesMode = layout.files.open ? "inline" : overlay === "files" ? "overlay" : "collapsed";
  const benchMode = layout.workbench.open ? "inline" : overlay === "workbench" ? "overlay" : "collapsed";
  const benchWidth = benchMode === "overlay" ? ide.overlayWidthOf("workbench") : layout.workbench.width;
  const benchBadges: RailBadge[] = [];
  if (openFiles.files.length > 0) {
    benchBadges.push({
      key: "tabs",
      text: String(openFiles.files.length),
      title: t("v2.coding.layout.openTabs", { count: openFiles.files.length }),
    });
  }
  if (changedCount > 0) {
    benchBadges.push({
      key: "changes",
      text: `±${changedCount}`,
      title: t("v2.coding.layout.turnChanges", { count: changedCount }),
    });
  }
  if (openFiles.files.some(isDirty)) {
    benchBadges.push({ key: "dirty", text: null, title: t("v2.coding.editor.unsaved"), tone: "warn" });
  }

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
              size="sm"
              disabled={!ready || zipping}
              title={!ready ? t("v2.coding.composer.runtimeNotReady") : t("v2.coding.download.projectHint")}
              onClick={() => void download(id)}
              testId="v2-coding-download-project"
            >
              {zipping ? (
                <Loader2 size={13} className="v2-coding-spin" aria-hidden="true" />
              ) : (
                <Download size={13} aria-hidden="true" />
              )}
              {t(zipping ? "v2.coding.download.preparing" : "v2.coding.download.project")}
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
        <div
          ref={setGrid}
          className={resizing ? "v2-coding-grid resizing" : "v2-coding-grid"}
          style={{ gridTemplateColumns: layout.columns }}
          onClickCapture={overlayOutside}
          data-testid="v2-coding-grid"
        >
          {filesMode !== "inline" && (
            <PaneRail panel="files" controls={filesId} expanded={filesMode === "overlay"} onClick={() => railClick("files")} />
          )}
          <section
            id={filesId}
            className={`v2-coding-pane v2-coding-pane-files is-${filesMode}`}
            style={filesMode === "overlay" ? { width: ide.overlayWidthOf("files") } : undefined}
            aria-label={t("v2.coding.ide.files")}
            onKeyDown={overlayKeyDown("files")}
            data-testid="v2-coding-pane-files"
          >
            <FileTree
              projectId={id}
              version={filesVersion}
              activePath={active === CHANGES_TAB ? null : active}
              onOpen={(path) => openFile(path)}
              onDownload={downloadFile}
              downloading={fileDownloading}
              headEnd={<PanelToggle panel="files" controls={filesId} onClick={() => collapsePanel("files")} />}
            />
          </section>
          {layout.files.open && (
            <PaneResizer
              panel="files"
              pane={layout.files}
              controls={filesId}
              onWidth={(w) => ide.setWidth("files", w)}
              onResizing={setResizing}
            />
          )}
          <section
            className="v2-coding-pane v2-coding-pane-chat"
            aria-label={t("v2.coding.conversation.title")}
            data-testid="v2-coding-pane-chat"
          >
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
            <Conversation view={view} onOpenChange={openChange} onOpenFile={openFile} />
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
          {layout.workbench.open && (
            <PaneResizer
              panel="workbench"
              pane={layout.workbench}
              controls={benchId}
              onWidth={(w) => ide.setWidth("workbench", w)}
              onResizing={setResizing}
            />
          )}
          <section
            id={benchId}
            className={`v2-coding-pane v2-coding-pane-workbench is-${benchMode}${benchWidth < WORKBENCH_COMPACT ? " compact" : ""}`}
            style={benchMode === "overlay" ? { width: benchWidth } : undefined}
            aria-label={t("v2.coding.layout.workbench")}
            onKeyDown={overlayKeyDown("workbench")}
            data-testid="v2-coding-pane-workbench"
          >
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
              onOpenFile={openFile}
              onDownload={downloadFile}
              downloading={fileDownloading}
              running={running}
              version={filesVersion}
              focus={focus}
              reveal={reveal}
              onRevealed={revealed}
              headEnd={<PanelToggle panel="workbench" controls={benchId} onClick={() => collapsePanel("workbench")} />}
            />
          </section>
          {benchMode !== "inline" && (
            <PaneRail
              panel="workbench"
              controls={benchId}
              expanded={benchMode === "overlay"}
              badges={benchBadges}
              onClick={() => railClick("workbench")}
            />
          )}
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
