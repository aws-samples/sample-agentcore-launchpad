import Editor, { DiffEditor, type Monaco, type MonacoDiffEditor, type OnMount } from "@monaco-editor/react";
import { Copy, FileDiff, X } from "lucide-react";
import { type KeyboardEvent, useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import { Markdown } from "../../../components/Markdown";
import { type CodingChange, type CodingChangeScope, codingApi, errorMessage } from "../../../lib/api";
import { useLoad, useV2Toast } from "../../hooks";
import { copyText } from "../observability/common";
import { Alert, Button, LinkButton, Segmented, Spin, Tag } from "../../ui";
import { languageFor, isMarkdown } from "./language";
import { isDirty, type OpenFile } from "./useOpenFiles";

export const CHANGES_TAB = "__changes__";

/** A request to show one file's diff in the Changes tab (a conversation click). */
export interface ChangeFocus {
  path: string;
  scope: CodingChangeScope;
  nonce: number;
}

const EDITOR_OPTIONS = {
  minimap: { enabled: false },
  automaticLayout: true,
  fontSize: 13,
  scrollBeyondLastLine: false,
  renderWhitespace: "selection" as const,
  tabSize: 4,
};

function modelUri(projectId: string, path: string): string {
  return `file:///coding/${encodeURIComponent(projectId)}/${path.split("/").map(encodeURIComponent).join("/")}`;
}

type CodeEditor = Parameters<OnMount>[0];

/**
 * Make the editor's model hold `draft`. A tab's model outlives the editor (it keeps
 * undo and the cursor across tab switches), so it can still hold text from before a
 * re-read that happened while no editor showed it — and the library pushes `value`
 * into the model only when the prop changes, never on mount or when it re-attaches an
 * existing model. Left stale, the editor would show old text and a save would write it
 * over the newer file (the sha256 is the newer one's).
 */
function syncModel(editor: CodeEditor | null, draft: string): void {
  const model = editor?.getModel();
  if (model && model.getValue() !== draft) model.setValue(draft);
}

/** Dispose a closed tab's model once no editor shows it any more. */
function disposeLater(monaco: Monaco | null, uri: string, tries = 3): void {
  if (!monaco) return;
  window.setTimeout(() => {
    const model = monaco.editor.getModel(monaco.Uri.parse(uri));
    if (!model) return;
    if (!model.isAttachedToEditor()) model.dispose();
    else if (tries > 1) disposeLater(monaco, uri, tries - 1);
  }, 300);
}

/**
 * The center pane: the Changes tab (diffs of the last turn / the uncommitted tree)
 * and one tab per open file. Files edit in Monaco (one model per file, so undo and
 * cursor survive tab switches); ⌘/Ctrl+S saves; everything is read-only while a turn
 * runs, because the agent may be writing the same files.
 */
export function EditorPane({
  projectId,
  files,
  active,
  onActivate,
  onClose,
  onDraft,
  onSave,
  onReload,
  onOverwrite,
  onTogglePreview,
  running,
  version,
  focus,
}: {
  projectId: string;
  files: OpenFile[];
  active: string;
  onActivate: (tab: string) => void;
  onClose: (path: string) => void;
  onDraft: (path: string, draft: string) => void;
  onSave: (path: string) => void;
  onReload: (path: string) => void;
  onOverwrite: (path: string) => void;
  onTogglePreview: (path: string) => void;
  running: boolean;
  version: number;
  focus: ChangeFocus | null;
}) {
  const { t } = useTranslation();
  const monacoRef = useRef<Monaco | null>(null);
  const file = files.find((f) => f.path === active) ?? null;

  // leaving the IDE drops this project's models (the editor itself is disposed by the library)
  useEffect(() => {
    // the latest instance is wanted here (Monaco loads after this effect ran)
    const holder = monacoRef;
    return () => {
      const monaco = holder.current;
      if (!monaco) return;
      const prefix = modelUri(projectId, "");
      window.setTimeout(() => {
        for (const model of monaco.editor.getModels()) {
          if (model.uri.toString(true).startsWith(prefix) && !model.isAttachedToEditor()) model.dispose();
        }
      }, 300);
    };
  }, [projectId]);

  const close = (path: string) => {
    onClose(path);
    disposeLater(monacoRef.current, modelUri(projectId, path));
  };

  const onKeyDown = (e: KeyboardEvent<HTMLDivElement>) => {
    // ⌘/Ctrl+S anywhere in the pane saves the open file instead of the browser's "save page"
    // (inside Monaco its own ⌘/Ctrl+S command already handled and prevented the key)
    if (!e.defaultPrevented && (e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "s") {
      e.preventDefault();
      if (file && !running) onSave(file.path);
    }
  };

  return (
    <div className="v2-coding-editor" onKeyDown={onKeyDown} data-testid="v2-coding-editor">
      <div className="v2-coding-tabs" role="tablist">
        <button
          type="button"
          role="tab"
          aria-selected={active === CHANGES_TAB}
          className={active === CHANGES_TAB ? "v2-coding-tab on" : "v2-coding-tab"}
          onClick={() => onActivate(CHANGES_TAB)}
          data-testid="v2-coding-tab-changes"
        >
          <FileDiff size={13} aria-hidden="true" />
          {t("v2.coding.changes.tab")}
        </button>
        {files.map((f) => (
          <span key={f.path} className={f.path === active ? "v2-coding-tab on" : "v2-coding-tab"} title={f.path}>
            <button
              type="button"
              role="tab"
              aria-selected={f.path === active}
              className="v2-coding-tab-open"
              onClick={() => onActivate(f.path)}
              onAuxClick={(e) => e.button === 1 && close(f.path)}
              data-testid={`v2-coding-tab-${f.path}`}
            >
              {f.path.slice(f.path.lastIndexOf("/") + 1)}
              {isDirty(f) && <span className="v2-coding-dirty" aria-label={t("v2.coding.editor.unsaved")} />}
            </button>
            <button
              type="button"
              className="v2-coding-tab-x"
              aria-label={t("v2.coding.editor.closeTab", { name: f.path })}
              title={t("v2.common.close")}
              onClick={() => close(f.path)}
            >
              <X size={12} aria-hidden="true" />
            </button>
          </span>
        ))}
      </div>
      <div className="v2-coding-editor-body">
        {active === CHANGES_TAB || !file ? (
          <ChangesView projectId={projectId} version={version} focus={focus} />
        ) : (
          <FileView
            projectId={projectId}
            file={file}
            running={running}
            monacoRef={monacoRef}
            onDraft={(draft) => onDraft(file.path, draft)}
            onSave={() => onSave(file.path)}
            onReload={() => onReload(file.path)}
            onOverwrite={() => onOverwrite(file.path)}
            onTogglePreview={() => onTogglePreview(file.path)}
          />
        )}
      </div>
    </div>
  );
}

function FileView({
  projectId,
  file,
  running,
  monacoRef,
  onDraft,
  onSave,
  onReload,
  onOverwrite,
  onTogglePreview,
}: {
  projectId: string;
  file: OpenFile;
  running: boolean;
  monacoRef: { current: Monaco | null };
  onDraft: (draft: string) => void;
  onSave: () => void;
  onReload: () => void;
  onOverwrite: () => void;
  onTogglePreview: () => void;
}) {
  const { t } = useTranslation();
  const dirty = isDirty(file);
  const markdown = isMarkdown(file.path);
  const saveRef = useRef(onSave);
  saveRef.current = onSave;
  const runningRef = useRef(running);
  runningRef.current = running;
  const draftRef = useRef(file.draft);
  draftRef.current = file.draft;
  const editorRef = useRef<CodeEditor | null>(null);
  const options = useMemo(() => ({ ...EDITOR_OPTIONS, readOnly: running }), [running]);
  // a tab switch re-attaches that file's model as it was (runs after the library's own
  // path/value effects: a child's effects run first)
  useEffect(() => {
    syncModel(editorRef.current, draftRef.current);
  }, [file.path]);

  return (
    <>
      <div className="v2-coding-editor-bar">
        <span className="mono v2-coding-editor-path" title={file.path}>
          {file.path}
        </span>
        {dirty && <Tag tone="orange">{t("v2.coding.editor.unsaved")}</Tag>}
        {running && <Tag tone="blue">{t("v2.coding.editor.readOnlyRunning")}</Tag>}
        <div className="end">
          {markdown && !file.binary && (
            <Segmented
              value={file.preview ? "preview" : "source"}
              onChange={(value) => {
                if ((value === "preview") !== file.preview) onTogglePreview();
              }}
              ariaLabel={t("v2.coding.editor.view")}
              options={[
                { value: "source", label: t("v2.coding.editor.source") },
                { value: "preview", label: t("v2.coding.editor.preview") },
              ]}
            />
          )}
          <Button
            kind="primary"
            size="sm"
            disabled={!dirty || running || file.saving || file.binary}
            title={running ? t("v2.coding.editor.readOnlyRunning") : t("v2.coding.editor.saveHint")}
            onClick={onSave}
            testId="v2-coding-save"
          >
            {t(file.saving ? "v2.coding.editor.saving" : "v2.common.save")}
          </Button>
        </div>
      </div>
      {file.conflict && (
        <Alert
          tone="warn"
          action={
            <span className="v2-coding-alert-actions">
              <LinkButton onClick={onReload} testId="v2-coding-conflict-reload">
                {t("v2.coding.editor.reload")}
              </LinkButton>
              <LinkButton danger onClick={onOverwrite} disabled={running} testId="v2-coding-conflict-overwrite">
                {t("v2.coding.editor.overwrite")}
              </LinkButton>
            </span>
          }
        >
          {t("v2.coding.editor.conflict")}
        </Alert>
      )}
      {file.changedOnDisk && !file.conflict && (
        <Alert tone="warn" action={<LinkButton onClick={onReload}>{t("v2.coding.editor.reload")}</LinkButton>}>
          {t("v2.coding.editor.changedOnDisk")}
        </Alert>
      )}
      <div className="v2-coding-editor-main">
        {file.error ? (
          <Alert tone="error" action={<LinkButton onClick={onReload}>{t("v2.common.retry")}</LinkButton>}>
            {file.error}
          </Alert>
        ) : file.loading ? (
          <Spin />
        ) : file.binary ? (
          <div className="v2-coding-placeholder">{t("v2.coding.editor.binary")}</div>
        ) : markdown && file.preview ? (
          <div className="v2-coding-md" data-testid="v2-coding-md-preview">
            <Markdown text={file.draft} />
          </div>
        ) : (
          <Editor
            height="100%"
            theme="vs"
            path={modelUri(projectId, file.path)}
            language={languageFor(file.path)}
            value={file.draft}
            onChange={(value) => onDraft(value ?? "")}
            loading={<Spin />}
            // models are ours to drop (tab close, leaving the IDE), so undo survives a
            // visit to the Changes tab
            keepCurrentModel
            onMount={(editor, monaco) => {
              monacoRef.current = monaco;
              editorRef.current = editor;
              syncModel(editor, draftRef.current);
              editor.addCommand(monaco.KeyMod.CtrlCmd | monaco.KeyCode.KeyS, () => {
                if (!runningRef.current) saveRef.current();
              });
            }}
            options={options}
          />
        )}
      </div>
    </>
  );
}

const STATUS_TONE = { A: "green", M: "blue", D: "red", R: "orange" } as const;

/** The Changes tab: what the last turn changed, or everything uncommitted, as a file
 *  list and a side-by-side diff (original = that revision, modified = the working tree). */
function ChangesView({ projectId, version, focus }: { projectId: string; version: number; focus: ChangeFocus | null }) {
  const { t } = useTranslation();
  const toast = useV2Toast();
  const [scope, setScope] = useState<CodingChangeScope>(focus?.scope ?? "turn");
  const [selected, setSelected] = useState<string | null>(focus?.path ?? null);
  const [lastFocus, setLastFocus] = useState(focus?.nonce ?? 0);
  if (focus && focus.nonce !== lastFocus) {
    // a conversation click: jump to that file (state adjusted during render, not in an effect)
    setLastFocus(focus.nonce);
    setScope(focus.scope);
    setSelected(focus.path);
  }
  const list = useLoad(() => codingApi.changes(projectId, scope), `${projectId}:${scope}:${version}`);
  const changes = list.data?.changes ?? [];
  const current: CodingChange | null = changes.find((c) => c.path === selected) ?? changes[0] ?? null;

  const copyPatch = async () => {
    try {
      const { diff } = await codingApi.diff(projectId, scope);
      await copyText(diff);
      toast("success", t("v2.coding.changes.copied"));
    } catch (err) {
      toast("error", errorMessage(err));
    }
  };

  return (
    <div className="v2-coding-changes" data-testid="v2-coding-changes">
      <div className="v2-coding-editor-bar">
        <Segmented
          value={scope}
          onChange={(value) => {
            setScope(value);
            setSelected(null);
          }}
          ariaLabel={t("v2.coding.changes.scope")}
          options={[
            { value: "turn", label: t("v2.coding.changes.turn") },
            { value: "worktree", label: t("v2.coding.changes.worktree") },
          ]}
        />
        <span className="v2-muted">{t("v2.coding.changes.count", { count: changes.length })}</span>
        <div className="end">
          <LinkButton onClick={list.reload}>{t("v2.common.refresh")}</LinkButton>
          <LinkButton onClick={() => void copyPatch()} disabled={changes.length === 0} testId="v2-coding-copy-patch">
            <Copy size={13} aria-hidden="true" /> {t("v2.coding.changes.copyPatch")}
          </LinkButton>
        </div>
      </div>
      {list.error ? (
        <Alert tone="error" action={<LinkButton onClick={list.reload}>{t("v2.common.retry")}</LinkButton>}>
          {list.error}
        </Alert>
      ) : list.loading && !list.data ? (
        <Spin />
      ) : changes.length === 0 ? (
        <div className="v2-coding-placeholder">
          {t(scope === "turn" ? "v2.coding.changes.emptyTurn" : "v2.coding.changes.emptyWorktree")}
        </div>
      ) : (
        <div className="v2-coding-changes-split">
          <ul className="v2-coding-changes-list" aria-label={t("v2.coding.changes.files")}>
            {changes.map((c) => (
              <li key={`${c.status}:${c.path}`}>
                <button
                  type="button"
                  className={c.path === current?.path ? "on" : undefined}
                  onClick={() => setSelected(c.path)}
                  title={c.old_path ? `${c.old_path} → ${c.path}` : c.path}
                  data-testid={`v2-coding-change-${c.path}`}
                >
                  <Tag tone={STATUS_TONE[c.status] ?? "gray"} title={t(`v2.coding.changes.status.${c.status}`)}>
                    {c.status}
                  </Tag>
                  <span className="mono">{c.path}</span>
                </button>
              </li>
            ))}
          </ul>
          {current && <ChangeDiff key={`${scope}:${current.path}`} projectId={projectId} scope={scope} change={current} version={version} />}
        </div>
      )}
    </div>
  );
}

function ChangeDiff({
  projectId,
  scope,
  change,
  version,
}: {
  projectId: string;
  scope: CodingChangeScope;
  change: CodingChange;
  version: number;
}) {
  const { t } = useTranslation();
  const texts = useLoad(async () => {
    const rev = scope === "turn" ? "turn" : "head";
    const [original, modified] = await Promise.all([
      change.status === "A" ? null : codingApi.readFile(projectId, change.old_path || change.path, rev),
      change.status === "D" ? null : codingApi.readFile(projectId, change.path),
    ]);
    const binary = !!original?.binary || !!modified?.binary;
    return {
      binary,
      original: original && original.exists !== false ? (original.content ?? "") : "",
      modified: modified && modified.exists !== false ? (modified.content ?? "") : "",
    };
  }, `${projectId}:${scope}:${change.status}:${change.old_path ?? ""}:${change.path}:${version}`);

  if (texts.error) return <Alert tone="error">{texts.error}</Alert>;
  if (!texts.data) return <Spin />;
  if (texts.data.binary) return <div className="v2-coding-placeholder">{t("v2.coding.editor.binary")}</div>;
  return (
    <div className="v2-coding-diff" data-testid="v2-coding-diff">
      <DiffView original={texts.data.original} modified={texts.data.modified} language={languageFor(change.path)} />
    </div>
  );
}

const DIFF_OPTIONS = {
  readOnly: true,
  originalEditable: false,
  automaticLayout: true,
  minimap: { enabled: false },
  scrollBeyondLastLine: false,
  renderSideBySide: true,
  fontSize: 13,
};

/** Monaco's DiffEditor, read-only. The models are kept past the editor's own dispose and
 *  dropped afterwards (the library otherwise disposes them under the live diff widget). */
function DiffView({ original, modified, language }: { original: string; modified: string; language: string }) {
  const editorRef = useRef<MonacoDiffEditor | null>(null);
  useEffect(
    () => () => {
      const model = editorRef.current?.getModel();
      window.setTimeout(() => {
        model?.original.dispose();
        model?.modified.dispose();
      }, 0);
    },
    [],
  );
  return (
    <DiffEditor
      height="100%"
      theme="vs"
      original={original}
      modified={modified}
      language={language}
      keepCurrentOriginalModel
      keepCurrentModifiedModel
      loading={<Spin />}
      onMount={(editor) => {
        editorRef.current = editor;
      }}
      options={DIFF_OPTIONS}
    />
  );
}
