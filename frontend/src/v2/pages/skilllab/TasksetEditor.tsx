import { Plus, Trash2 } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { useSearchParams } from "react-router-dom";

import {
  api,
  ApiError,
  errorMessage,
  type SkillLabTask,
  type SkillLabTasksetIssue,
  type SkillLabTasksetMode,
} from "../../../lib/api";
import {
  draftKey,
  type Drafts,
  emptyDraft,
  EXAMPLE_TASKS,
  interpretTasksetUpload,
  mirrorTaskErrors,
  seedDrafts,
  SINGLE_SPLIT,
  SPLIT_ORDER,
  splitsFor,
  suggestId,
  TASK_ASSET_ACCEPT,
  type TaskDraft,
  taskId,
  toDraft,
  toTask,
} from "../../../lib/skillLabTasksets";
import { useV2Toast } from "../../hooks";
import {
  Alert,
  Button,
  Card,
  Descriptions,
  Field,
  FlowHeader,
  LinkButton,
  OptionCard,
  Segmented,
  Spin,
  Tag,
} from "../../ui";

type EditorTab = "rows" | "upload";

/**
 * Create / edit a task set (`?tab=tasksets&view=new|edit&id=`). The editor is
 * hydrated ONLY on entry — a background refresh must never re-render half-typed
 * rows from server state. Editing cannot change the layout (the backend refuses
 * mismatched split keys).
 */
export function TasksetEditor({ id }: { id: string | null }) {
  const { t } = useTranslation();
  const [, setParams] = useSearchParams();
  const toast = useV2Toast();
  const editing = id !== null;

  const [loadError, setLoadError] = useState<string | null>(null);
  const [hydrated, setHydrated] = useState(!editing);
  const [mode, setMode] = useState<SkillLabTasksetMode>("single");
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [drafts, setDrafts] = useState<Drafts>(() => seedDrafts("single"));
  const [tab, setTab] = useState<EditorTab>("rows");
  const [uploadSplit, setUploadSplit] = useState<string>(SINGLE_SPLIT);
  const [uploadError, setUploadError] = useState<string | null>(null);
  const [uploadNote, setUploadNote] = useState<string | null>(null);
  const [helpOpen, setHelpOpen] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);
  const [issues, setIssues] = useState<SkillLabTasksetIssue[]>([]);
  const [busy, setBusy] = useState(false);
  const assetInputs = useRef<Record<string, HTMLInputElement | null>>({});
  const jsonRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (id === null) return;
    let stale = false;
    api
      .skillLabTasksetGet(id, true)
      .then((full) => {
        if (stale) return;
        setMode(full.info.mode);
        setName(full.info.name);
        setDescription(full.info.description ?? "");
        setDrafts(
          Object.fromEntries(
            splitsFor(full.info.mode).map((split) => [split, (full.tasks_by_split[split] ?? []).map(toDraft)]),
          ) as Drafts,
        );
        setUploadSplit(full.info.mode === "single" ? SINGLE_SPLIT : "train");
        setHydrated(true);
      })
      .catch((err: unknown) => {
        if (!stale) setLoadError(errorMessage(err));
      });
    return () => {
      stale = true;
    };
  }, [id]);

  const back = () =>
    editing ? setParams({ tab: "tasksets", view: "detail", id }) : setParams({ tab: "tasksets" });

  /** Mode switch while creating: keeps typed metadata, rebuilds splits. */
  const changeMode = (next: SkillLabTasksetMode) => {
    setMode(next);
    setDrafts((prev) => {
      const seed = seedDrafts(next);
      return Object.fromEntries(splitsFor(next).map((split) => [split, prev[split] ?? seed[split]])) as Drafts;
    });
    setUploadSplit(next === "single" ? SINGLE_SPLIT : "train");
    setIssues([]);
  };

  const activeSplits = useMemo(() => splitsFor(mode).filter((split) => drafts[split] !== undefined), [mode, drafts]);
  const mirrorErrors = useMemo(() => mirrorTaskErrors(drafts, t), [drafts, t]);
  const hasMirrorErrors = Object.values(mirrorErrors).some((errs) => Object.keys(errs).length > 0);
  const anyAssetBusy = Object.values(drafts).some((list) => list.some((draft) => draft.assetBusy));

  const patchDraft = (split: string, key: string, patch: Partial<TaskDraft>) =>
    setDrafts((prev) => ({
      ...prev,
      [split]: prev[split].map((draft) => (draft.key === key ? { ...draft, ...patch } : draft)),
    }));

  const uploadTaskAssets = async (split: string, key: string, files: File[]) => {
    if (!files.length) return;
    patchDraft(split, key, { assetBusy: true, assetError: null });
    try {
      const response = await api.skillLabTaskAssetsUpload(files);
      setDrafts((prev) => ({
        ...prev,
        [split]: prev[split].map((draft) =>
          draft.key !== key
            ? draft
            : {
                ...draft,
                assets: [
                  ...draft.assets,
                  ...response.assets.map((value) => ({ key: draftKey(), path: `data/${value.name}`, value })),
                ],
                assetBusy: false,
              },
        ),
      }));
    } catch (err) {
      patchDraft(split, key, { assetBusy: false, assetError: errorMessage(err) });
    }
  };

  const patchAsset = (split: string, key: string, assetKey: string, path: string | null) =>
    setDrafts((prev) => ({
      ...prev,
      [split]: prev[split].map((draft) =>
        draft.key !== key
          ? draft
          : {
              ...draft,
              assets:
                path === null
                  ? draft.assets.filter((a) => a.key !== assetKey)
                  : draft.assets.map((a) => (a.key === assetKey ? { ...a, path } : a)),
              assetError: path === null ? null : draft.assetError,
            },
      ),
    }));

  const addRow = (split: string) =>
    setDrafts((prev) => {
      const list = prev[split] ?? [];
      return { ...prev, [split]: [...list, { ...emptyDraft(1), id: suggestId(list) }] };
    });

  const removeRow = (split: string, key: string) =>
    setDrafts((prev) => ({ ...prev, [split]: prev[split].filter((draft) => draft.key !== key) }));

  /** Splits to send: split mode keeps train+val always, test only when filled. */
  const payloadSplits = (): Record<string, SkillLabTask[]> => {
    const payload: Record<string, SkillLabTask[]> = {};
    for (const split of splitsFor(mode)) {
      const list = drafts[split] ?? [];
      if (split === "test" && list.length === 0) continue;
      payload[split] = list.map(toTask);
    }
    return payload;
  };

  const applyServerError = (err: unknown) => {
    if (err instanceof ApiError && err.code === "skill_lab.taskset_invalid") {
      const list = Array.isArray(err.detail) ? (err.detail as SkillLabTasksetIssue[]) : [];
      setIssues(list);
      if (list.length === 0) setFormError(err.message);
      return;
    }
    setFormError(errorMessage(err));
  };

  const save = async () => {
    setFormError(null);
    setIssues([]);
    if (anyAssetBusy) return;
    if (!name.trim()) {
      setFormError(t("skillLab.tasksets.err.nameRequired"));
      return;
    }
    const payload = payloadSplits();
    const emptySplit = Object.entries(payload).find(([, list]) => list.length === 0);
    if (emptySplit) {
      setFormError(t("skillLab.tasksets.err.splitEmpty", { split: emptySplit[0] }));
      return;
    }
    setBusy(true);
    try {
      if (id !== null) {
        await api.skillLabTasksetUpdate(id, { name: name.trim(), description, tasks_by_split: payload });
        toast("success", t("skillLab.tasksets.updated"));
        setParams({ tab: "tasksets", view: "detail", id });
      } else {
        const created = await api.skillLabTasksetCreate({
          name: name.trim(),
          description,
          mode,
          tasks_by_split: payload,
        });
        toast("success", t("skillLab.tasksets.created"));
        setParams({ tab: "tasksets", view: "detail", id: created.id });
      }
    } catch (err) {
      applyServerError(err);
    } finally {
      setBusy(false);
    }
  };

  const onFile = async (file: File) => {
    setUploadError(null);
    setUploadNote(null);
    let parsed: unknown;
    try {
      parsed = JSON.parse(await file.text());
    } catch (err) {
      setUploadError(t("skillLab.tasksets.upload.badJson", { msg: (err as Error).message }));
      return;
    }
    const upload = interpretTasksetUpload(parsed, { mode, uploadSplit, editing }, t);
    if (upload.kind === "error") {
      setUploadError(upload.message);
      return;
    }
    if (upload.kind === "split") {
      setDrafts((prev) => ({ ...prev, [upload.split]: upload.drafts }));
    } else {
      setMode(upload.mode);
      setDrafts((prev) => {
        const base = Object.fromEntries(splitsFor(upload.mode).map((split) => [split, prev[split] ?? []])) as Drafts;
        return { ...base, ...upload.loaded };
      });
    }
    setUploadNote(upload.note);
    setTab("rows");
  };

  const splitIssues = (split: string) => issues.filter((issue) => issue.split === split);
  // Anything the validator blamed on something other than a rendered split
  // ("mode", "name", "train/val") — never dropped, or the save would look silent.
  const globalIssues = issues.filter((issue) => !splitsFor(mode).includes(issue.split));

  if (loadError) {
    return (
      <>
        <FlowHeader title={t("skillLab.tasksets.editTitle")} onBack={back} />
        <Alert tone="error">{loadError}</Alert>
      </>
    );
  }
  if (!hydrated) return <Spin />;

  const taskRow = (split: string, draft: TaskDraft, index: number) => {
    const error = mirrorErrors[split]?.[draft.key];
    const files = Object.keys(draft.files).length + draft.assets.length;
    return (
      <div
        key={draft.key}
        className={error ? "v2-skilllab-task invalid" : "v2-skilllab-task"}
        data-testid={`v2-task-row-${split}-${index}`}
      >
        <div className="v2-skilllab-task-head">
          <span className="v2-muted mono">#{index + 1}</span>
          <input
            className="v2-input mono"
            value={draft.id}
            aria-label={t("skillLab.tasksets.field.id")}
            placeholder={taskId(index + 1)}
            style={{ maxWidth: 220 }}
            onChange={(e) => patchDraft(split, draft.key, { id: e.target.value })}
          />
          <input
            className="v2-input mono"
            value={draft.taskType}
            aria-label={t("skillLab.tasksets.field.taskType")}
            placeholder={t("skillLab.tasksets.field.taskTypePlaceholder")}
            style={{ maxWidth: 220 }}
            onChange={(e) => patchDraft(split, draft.key, { taskType: e.target.value })}
          />
          {files > 0 && <Tag tone="blue">{t("skillLab.tasksets.filesChip", { count: files })}</Tag>}
          <span style={{ marginLeft: "auto" }}>
            <LinkButton danger title={t("skillLab.tasksets.removeRow")} onClick={() => removeRow(split, draft.key)}>
              <Trash2 size={14} aria-hidden="true" /> {t("skillLab.tasksets.removeRow")}
            </LinkButton>
          </span>
        </div>
        <div className="v2-form cols-2">
          <Field label={t("skillLab.tasksets.field.question")} required>
            <textarea
              className="v2-textarea"
              rows={3}
              value={draft.question}
              onChange={(e) => patchDraft(split, draft.key, { question: e.target.value })}
            />
          </Field>
          <Field label={t("skillLab.tasksets.field.rubric")} required>
            <textarea
              className="v2-textarea"
              rows={3}
              value={draft.rubric}
              onChange={(e) => patchDraft(split, draft.key, { rubric: e.target.value })}
            />
          </Field>
          <Field label={t("skillLab.tasksets.assets.label")} full hint={t("skillLab.tasksets.assets.hint")} error={draft.assetError}>
            <div className="v2-stack">
              <div>
                <Button size="sm" disabled={draft.assetBusy} onClick={() => assetInputs.current[draft.key]?.click()}>
                  {draft.assetBusy ? t("skillLab.tasksets.assets.uploading") : t("skillLab.tasksets.assets.pick")}
                </Button>
              </div>
              <input
                ref={(node) => {
                  assetInputs.current[draft.key] = node;
                }}
                type="file"
                multiple
                accept={TASK_ASSET_ACCEPT}
                style={{ display: "none" }}
                disabled={draft.assetBusy}
                data-testid={`v2-task-assets-${split}-${index}`}
                onChange={(event) => {
                  // Snapshot the File objects first: clearing `value` (so the
                  // same filename can be re-picked) empties the live FileList.
                  const picked = Array.from(event.target.files ?? []);
                  event.target.value = "";
                  void uploadTaskAssets(split, draft.key, picked);
                }}
              />
              {draft.assets.map((asset) => (
                <div key={asset.key} className="v2-skilllab-asset">
                  <input
                    className="v2-input mono"
                    value={asset.path}
                    aria-label={t("skillLab.tasksets.assets.destination")}
                    onChange={(e) => patchAsset(split, draft.key, asset.key, e.target.value)}
                  />
                  <span className="v2-muted">
                    {asset.value.name} · {asset.value.media_type} · {asset.value.size.toLocaleString()} B
                  </span>
                  <LinkButton danger onClick={() => patchAsset(split, draft.key, asset.key, null)}>
                    {t("skillLab.tasksets.assets.remove")}
                  </LinkButton>
                </div>
              ))}
              {Object.keys(draft.files).length > 0 && (
                <span className="v2-muted mono">{Object.keys(draft.files).join(" · ")}</span>
              )}
            </div>
          </Field>
        </div>
        {error && <div className="v2-skilllab-err">{error}</div>}
      </div>
    );
  };

  return (
    <>
      <FlowHeader
        title={editing ? t("skillLab.tasksets.editTitle") : t("skillLab.tasksets.createTitle")}
        onBack={back}
        end={
          <>
            <Button onClick={back}>{t("v2.common.cancel")}</Button>
            <Button
              kind="primary"
              disabled={busy || anyAssetBusy || hasMirrorErrors || !name.trim()}
              onClick={() => void save()}
              testId="v2-taskset-save"
            >
              {anyAssetBusy
                ? t("skillLab.tasksets.assets.uploading")
                : editing
                  ? t("skillLab.tasksets.save")
                  : t("skillLab.tasksets.create")}
            </Button>
          </>
        }
      />
      <Card title={t("v2.skillLab.basicInfo")} sub={t("skillLab.tasksets.createSub")}>
        {!editing && (
          <div className="v2-options" style={{ marginBottom: 16, gridTemplateColumns: "repeat(2, minmax(0, 1fr))" }}>
            {(["single", "split"] as const).map((option) => (
              <OptionCard
                key={option}
                title={t(`skillLab.tasksets.mode.${option}`)}
                desc={t(`skillLab.tasksets.mode.${option}Hint`)}
                on={mode === option}
                onClick={() => changeMode(option)}
                testId={`v2-taskset-mode-${option}`}
              />
            ))}
          </div>
        )}
        <div className="v2-form cols-2">
          <Field label={t("skillLab.tasksets.field.name")} required>
            <input className="v2-input" value={name} onChange={(e) => setName(e.target.value)} data-testid="v2-taskset-name" />
          </Field>
          <Field label={t("skillLab.tasksets.field.description")}>
            <input
              className="v2-input"
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              data-testid="v2-taskset-description"
            />
          </Field>
          {editing && (
            <Field label={t("skillLab.tasksets.field.mode")}>
              <span>
                <Tag tone={mode === "split" ? "blue" : "outline"}>{t(`skillLab.tasksets.mode.${mode}`)}</Tag>{" "}
                <span className="v2-muted">{t(`skillLab.tasksets.mode.${mode}Hint`)}</span>
              </span>
            </Field>
          )}
        </div>
      </Card>

      <Card
        title={t("v2.skillLab.tasks")}
        end={
          <Segmented
            value={tab}
            onChange={setTab}
            options={(["rows", "upload"] as const).map((v) => ({ value: v, label: t(`skillLab.tasksets.tab.${v}`) }))}
          />
        }
        testId="v2-taskset-editor-tasks"
      >
        {uploadNote && <Alert tone="success">{uploadNote}</Alert>}
        {tab === "upload" ? (
          <div className="v2-form cols-2">
            {mode === "split" && (
              <Field label={t("skillLab.tasksets.upload.targetSplit")}>
                <select className="v2-select" value={uploadSplit} onChange={(e) => setUploadSplit(e.target.value)}>
                  {SPLIT_ORDER.map((split) => (
                    <option key={split} value={split}>
                      {split}
                    </option>
                  ))}
                </select>
              </Field>
            )}
            <Field label={t("skillLab.tasksets.upload.label")} hint={t("skillLab.tasksets.upload.hint")} error={uploadError} full>
              <div>
                <Button onClick={() => jsonRef.current?.click()}>{t("skillLab.tasksets.upload.pick")}</Button>
              </div>
              <input
                ref={jsonRef}
                type="file"
                accept=".json,application/json"
                style={{ display: "none" }}
                data-testid="v2-taskset-upload-input"
                onChange={(e) => {
                  const file = e.target.files?.[0];
                  e.target.value = "";
                  if (file) void onFile(file);
                }}
              />
            </Field>
          </div>
        ) : (
          activeSplits.map((split) => (
            <section key={split} className="v2-skilllab-split" data-testid={`v2-taskset-split-${split}`}>
              <div className="v2-skilllab-split-head">
                <b className="mono">{split.toUpperCase()}</b>
                <span className="v2-muted">{t("skillLab.tasksets.rowCount", { count: (drafts[split] ?? []).length })}</span>
                {split === "test" && <Tag tone="outline">{t("skillLab.tasksets.testOptional")}</Tag>}
                <span style={{ marginLeft: "auto" }}>
                  <Button size="sm" onClick={() => addRow(split)} testId={`v2-taskset-add-${split}`}>
                    <Plus size={14} aria-hidden="true" />
                    {t("skillLab.tasksets.addRow")}
                  </Button>
                </span>
              </div>
              {(drafts[split] ?? []).map((draft, index) => taskRow(split, draft, index))}
              {splitIssues(split).length > 0 && (
                <Alert tone="error">{splitIssues(split).map((issue) => issue.message).join(" · ")}</Alert>
              )}
            </section>
          ))
        )}
        {globalIssues.length > 0 && (
          <Alert tone="error">{globalIssues.map((issue) => `${issue.split}: ${issue.message}`).join(" · ")}</Alert>
        )}
        {formError && <Alert tone="error">{formError}</Alert>}
      </Card>

      <Card
        title={t("skillLab.tasksets.help.title")}
        end={
          <LinkButton onClick={() => setHelpOpen((open) => !open)} testId="v2-taskset-help-toggle">
            {helpOpen ? t("v2.common.collapse") : t("v2.common.expand")}
          </LinkButton>
        }
      >
        {helpOpen ? (
          <div className="v2-stack" data-testid="v2-taskset-help">
            <Descriptions
              one
              items={(["id", "question", "rubric", "taskType", "files", "judgeMode"] as const).map((row) => ({
                label: t(`skillLab.tasksets.help.field.${row}.key`),
                value: t(`skillLab.tasksets.help.field.${row}.text`),
              }))}
            />
            <div className="v2-row">
              <span className="v2-muted">{t("skillLab.tasksets.help.example")}</span>
              <Button
                size="sm"
                onClick={() => {
                  void navigator.clipboard
                    ?.writeText(EXAMPLE_TASKS)
                    .then(() => toast("success", t("skillLab.tasksets.help.copied")));
                }}
              >
                {t("skillLab.tasksets.help.copy")}
              </Button>
            </div>
            <pre className="v2-pre">{EXAMPLE_TASKS}</pre>
            <Alert>{t("skillLab.tasksets.help.note")}</Alert>
          </div>
        ) : (
          <span className="v2-muted">{t("skillLab.tasksets.upload.hint")}</span>
        )}
      </Card>
    </>
  );
}
