import type { CSSProperties } from "react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { useSearchParams } from "react-router-dom";

import {
  Btn,
  Chip,
  ConfirmDialog,
  DataTable,
  Pager,
  Panel,
  useTablePage,
  useToast,
} from "../components";
import type {
  SkillLabTask,
  SkillLabTasksetDetail,
  SkillLabTasksetInfo,
  SkillLabTasksetIssue,
  SkillLabTasksetMode,
} from "../lib/api";
import { api, ApiError } from "../lib/api";
import type { Drafts, TaskDraft } from "../lib/skillLabTasksets";
import {
  countsLabel,
  draftKey,
  emptyDraft,
  EXAMPLE_TASKS,
  excerpt,
  fileCount,
  interpretTasksetUpload,
  mirrorTaskErrors,
  seedDrafts,
  SINGLE_SPLIT,
  SPLIT_ORDER,
  splitsFor,
  suggestId,
  taskId,
  toDraft,
  toTask,
} from "../lib/skillLabTasksets";
import { TaskgenPanel } from "./skillLab/TaskgenPanel";

export function SkillLabTasksets() {
  const { t } = useTranslation();
  const toast = useToast();
  const [searchParams, setSearchParams] = useSearchParams();
  const tsParam = searchParams.get("ts");
  const creating = tsParam === "new";
  // AI generation sub-surface: "new" = wizard, otherwise a taskgen job id.
  const genParam = searchParams.get("gen");

  const [rows, setRows] = useState<SkillLabTasksetInfo[]>([]);
  const [loading, setLoading] = useState(true);
  const [listError, setListError] = useState<string | null>(null);
  const [detail, setDetail] = useState<SkillLabTasksetDetail | null>(null);
  const [confirmDelete, setConfirmDelete] =
    useState<SkillLabTasksetInfo | null>(null);

  // Editor state. Create and edit share it, and it is hydrated ONLY by an
  // explicit user action (select "new", press Edit) — a list refresh must never
  // re-render half-typed rows from server state.
  const [editing, setEditing] = useState(false);
  const [mode, setMode] = useState<SkillLabTasksetMode>("single");
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [drafts, setDrafts] = useState<Drafts>(() => seedDrafts("single"));
  // Hidden per-row file inputs, keyed by draft key: the visible picker is a
  // themed <Btn> that clicks the input for its own row.
  const assetInputs = useRef<Record<string, HTMLInputElement | null>>({});
  const jsonRef = useRef<HTMLInputElement>(null);
  const [tab, setTab] = useState<"rows" | "upload">("rows");
  const [uploadError, setUploadError] = useState<string | null>(null);
  const [uploadNote, setUploadNote] = useState<string | null>(null);
  const [uploadSplit, setUploadSplit] = useState<string>("train");
  const [helpOpen, setHelpOpen] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);
  const [issues, setIssues] = useState<SkillLabTasksetIssue[]>([]);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      setRows(await api.skillLabTasksets());
      setListError(null);
    } catch (err) {
      setListError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const resetEditor = useCallback((nextMode: SkillLabTasksetMode) => {
    setMode(nextMode);
    setName("");
    setDescription("");
    setDrafts(seedDrafts(nextMode));
    setTab("rows");
    setUploadError(null);
    setUploadNote(null);
    setUploadSplit(nextMode === "single" ? SINGLE_SPLIT : "train");
    setFormError(null);
    setIssues([]);
  }, []);

  /** Mode switch in the create wizard: keeps typed metadata, rebuilds splits. */
  const changeMode = (next: SkillLabTasksetMode) => {
    setMode(next);
    setDrafts((prev) => {
      const seed = seedDrafts(next);
      return Object.fromEntries(
        splitsFor(next).map((split) => [split, prev[split] ?? seed[split]]),
      ) as Drafts;
    });
    setUploadSplit(next === "single" ? SINGLE_SPLIT : "train");
    setIssues([]);
  };

  // Selection effect keys on the URL param alone, so it cannot fire while the
  // user edits (the param is stable through save/reload).
  useEffect(() => {
    setEditing(false);
    setFormError(null);
    setIssues([]);
    if (tsParam === "new") {
      setDetail(null);
      resetEditor("single");
      return;
    }
    if (!tsParam) {
      setDetail(null);
      return;
    }
    let stale = false;
    api
      .skillLabTasksetGet(tsParam)
      .then((result) => {
        if (!stale) setDetail(result);
      })
      .catch(() => {
        if (!stale) setDetail(null);
      });
    return () => {
      stale = true;
    };
  }, [tsParam, resetEditor]);

  const select = (id: string | null) => {
    setSearchParams(id ? { view: "tasksets", ts: id } : { view: "tasksets" });
  };

  const selectGen = (id: string | null) => {
    setSearchParams(id ? { view: "tasksets", gen: id } : { view: "tasksets" });
  };

  const selectedIndex = rows.findIndex((row) => row.id === tsParam);
  const { rows: pageRows, pagerProps } = useTablePage(rows, selectedIndex);
  const selected = selectedIndex >= 0 ? rows[selectedIndex] : null;

  const activeSplits = useMemo(
    () => splitsFor(mode).filter((split) => drafts[split] !== undefined),
    [mode, drafts],
  );

  /** Client-side mirror of the vendored validator, per row, before submit. */
  const mirrorErrors = useMemo(() => mirrorTaskErrors(drafts, t), [drafts, t]);

  const hasMirrorErrors = Object.values(mirrorErrors).some(
    (errs) => Object.keys(errs).length > 0,
  );
  const anyAssetBusy = Object.values(drafts).some((list) =>
    list.some((draft) => draft.assetBusy),
  );

  const patchDraft = (
    split: string,
    key: string,
    patch: Partial<TaskDraft>,
  ) => {
    setDrafts((prev) => ({
      ...prev,
      [split]: prev[split].map((draft) =>
        draft.key === key ? { ...draft, ...patch } : draft,
      ),
    }));
  };

  const uploadTaskAssets = async (
    split: string,
    key: string,
    files: File[],
  ) => {
    if (!files.length) return;
    patchDraft(split, key, { assetBusy: true, assetError: null });
    try {
      const response = await api.skillLabTaskAssetsUpload(files);
      setDrafts((prev) => ({
        ...prev,
        [split]: prev[split].map((draft) => {
          if (draft.key !== key) return draft;
          const assets = [
            ...draft.assets,
            ...response.assets.map((value) => ({
              key: draftKey(),
              path: `data/${value.name}`,
              value,
            })),
          ];
          return { ...draft, assets, assetBusy: false };
        }),
      }));
    } catch (err) {
      patchDraft(split, key, {
        assetBusy: false,
        assetError: err instanceof ApiError ? err.message : String(err),
      });
    }
  };

  const renameTaskAsset = (
    split: string,
    key: string,
    assetKey: string,
    path: string,
  ) => {
    setDrafts((prev) => ({
      ...prev,
      [split]: prev[split].map((draft) =>
        draft.key === key
          ? {
              ...draft,
              assets: draft.assets.map((asset) =>
                asset.key === assetKey ? { ...asset, path } : asset,
              ),
            }
          : draft,
      ),
    }));
  };

  const removeTaskAsset = (split: string, key: string, assetKey: string) => {
    setDrafts((prev) => ({
      ...prev,
      [split]: prev[split].map((draft) =>
        draft.key === key
          ? {
              ...draft,
              assets: draft.assets.filter((asset) => asset.key !== assetKey),
              assetError: null,
            }
          : draft,
      ),
    }));
  };

  const addRow = (split: string) => {
    setDrafts((prev) => {
      const list = prev[split] ?? [];
      const next = emptyDraft(1);
      return { ...prev, [split]: [...list, { ...next, id: suggestId(list) }] };
    });
  };

  const removeRow = (split: string, key: string) => {
    setDrafts((prev) => ({
      ...prev,
      [split]: prev[split].filter((draft) => draft.key !== key),
    }));
  };

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

  const startEdit = async (row: SkillLabTasksetInfo) => {
    setFormError(null);
    setIssues([]);
    try {
      const full = await api.skillLabTasksetGet(row.id, true);
      setDetail(full);
      setMode(full.info.mode);
      setName(full.info.name);
      setDescription(full.info.description ?? "");
      setDrafts(
        Object.fromEntries(
          splitsFor(full.info.mode).map((split) => [
            split,
            (full.tasks_by_split[split] ?? []).map(toDraft),
          ]),
        ) as Drafts,
      );
      setUploadSplit(full.info.mode === "single" ? SINGLE_SPLIT : "train");
      setTab("rows");
      setUploadError(null);
      setUploadNote(null);
      setEditing(true);
    } catch (err) {
      toast(
        t("common.actionFailed", {
          msg: err instanceof ApiError ? err.message : String(err),
        }),
      );
    }
  };

  const applyServerError = (err: unknown) => {
    if (err instanceof ApiError && err.code === "skill_lab.taskset_invalid") {
      const detailList = Array.isArray(err.detail)
        ? (err.detail as SkillLabTasksetIssue[])
        : [];
      setIssues(detailList);
      if (detailList.length === 0) setFormError(err.message);
      return;
    }
    setFormError(err instanceof ApiError ? err.message : String(err));
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
    const emptySplit = Object.entries(payload).find(
      ([, list]) => list.length === 0,
    );
    if (emptySplit) {
      setFormError(
        t("skillLab.tasksets.err.splitEmpty", { split: emptySplit[0] }),
      );
      return;
    }
    // An edit must never fall through to the create branch — that would publish a
    // duplicate set under a new id instead of reporting the lost target.
    const editTarget = editing
      ? (selected?.id ?? detail?.info.id ?? null)
      : null;
    if (editing && editTarget === null) {
      setFormError(t("skillLab.tasksets.err.editTargetGone"));
      return;
    }
    setBusy(true);
    try {
      if (editTarget !== null) {
        await api.skillLabTasksetUpdate(editTarget, {
          name: name.trim(),
          description,
          tasks_by_split: payload,
        });
        toast(t("skillLab.tasksets.updated"));
        setEditing(false);
        await load();
        setDetail(await api.skillLabTasksetGet(editTarget));
      } else {
        const created = await api.skillLabTasksetCreate({
          name: name.trim(),
          description,
          mode,
          tasks_by_split: payload,
        });
        toast(t("skillLab.tasksets.created"));
        await load();
        select(created.id);
      }
    } catch (err) {
      applyServerError(err);
    } finally {
      setBusy(false);
    }
  };

  const remove = async (row: SkillLabTasksetInfo) => {
    try {
      await api.skillLabTasksetDelete(row.id);
      toast(t("skillLab.tasksets.deleted"));
      if (tsParam === row.id) select(null);
      await load();
    } catch (err) {
      toast(
        t("common.actionFailed", {
          msg: err instanceof ApiError ? err.message : String(err),
        }),
      );
    }
  };

  const onFile = async (file: File) => {
    setUploadError(null);
    setUploadNote(null);
    let parsed: unknown;
    try {
      parsed = JSON.parse(await file.text());
    } catch (err) {
      setUploadError(
        t("skillLab.tasksets.upload.badJson", { msg: (err as Error).message }),
      );
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
        const base = Object.fromEntries(
          splitsFor(upload.mode).map((split) => [split, prev[split] ?? []]),
        ) as Drafts;
        return { ...base, ...upload.loaded };
      });
    }
    setUploadNote(upload.note);
    setTab("rows");
  };

  const splitIssues = (split: string) =>
    issues.filter((issue) => issue.split === split);
  // Anything the validator blamed on something other than a rendered split
  // ("mode", "name", "train/val") — never dropped, or the save would look silent.
  const globalIssues = issues.filter(
    (issue) => !splitsFor(mode).includes(issue.split),
  );

  /* ── row editor ─────────────────────────────────────────────────────────── */

  const rowEditor = (split: string) => (
    <div
      key={split}
      style={{ marginBottom: 14 }}
      data-testid={`taskset-split-${split}`}
    >
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: 8,
          marginBottom: 8,
          borderBottom: "1px solid rgba(var(--tint-rgb),.08)",
          paddingBottom: 6,
        }}
      >
        <span className="mono" style={{ fontSize: 11, letterSpacing: ".08em" }}>
          {split.toUpperCase()}
        </span>
        <span className="mono dim" style={{ fontSize: 10.5 }}>
          {t("skillLab.tasksets.rowCount", {
            count: (drafts[split] ?? []).length,
          })}
        </span>
        {split === "test" && (
          <span className="mono dim" style={{ fontSize: 10 }}>
            {t("skillLab.tasksets.testOptional")}
          </span>
        )}
        <Btn
          style={{ marginLeft: "auto" }}
          data-testid={`taskset-add-row-${split}`}
          onClick={() => addRow(split)}
        >
          + {t("skillLab.tasksets.addRow")}
        </Btn>
      </div>

      {(drafts[split] ?? []).map((draft, index) => {
        const error = mirrorErrors[split]?.[draft.key];
        const files = Object.keys(draft.files).length + draft.assets.length;
        return (
          <div
            key={draft.key}
            data-testid={`task-row-${split}-${index}`}
            style={{
              border: `1px solid ${error ? "var(--crit)" : "rgba(var(--tint-rgb),.08)"}`,
              borderRadius: 4,
              padding: "10px 12px",
              marginBottom: 8,
            }}
          >
            <div
              style={{
                display: "flex",
                gap: 6,
                alignItems: "center",
                marginBottom: 6,
              }}
            >
              <input
                className="input mono"
                value={draft.id}
                aria-label={t("skillLab.tasksets.field.id")}
                placeholder={taskId(index + 1)}
                style={{ maxWidth: 200, fontSize: 11 }}
                onChange={(e) =>
                  patchDraft(split, draft.key, { id: e.target.value })
                }
              />
              <input
                className="input mono"
                value={draft.taskType}
                aria-label={t("skillLab.tasksets.field.taskType")}
                placeholder={t("skillLab.tasksets.field.taskTypePlaceholder")}
                style={{ maxWidth: 180, fontSize: 11 }}
                onChange={(e) =>
                  patchDraft(split, draft.key, { taskType: e.target.value })
                }
              />
              {files > 0 && (
                <Chip tone="aqua" icon="◆">
                  {t("skillLab.tasksets.filesChip", { count: files })}
                </Chip>
              )}
              <Btn
                style={{ marginLeft: "auto" }}
                title={t("skillLab.tasksets.removeRow")}
                data-testid={`taskset-remove-row-${split}-${index}`}
                onClick={() => removeRow(split, draft.key)}
              >
                ✕
              </Btn>
            </div>
            <div className="field">
              <label>{t("skillLab.tasksets.field.question")}</label>
              <textarea
                className="input"
                rows={2}
                value={draft.question}
                style={{ resize: "vertical", fontSize: 11.5 }}
                onChange={(e) =>
                  patchDraft(split, draft.key, { question: e.target.value })
                }
              />
            </div>
            <div className="field" style={{ marginBottom: 0 }}>
              <label>{t("skillLab.tasksets.field.rubric")}</label>
              <textarea
                className="input"
                rows={2}
                value={draft.rubric}
                style={{ resize: "vertical", fontSize: 11.5 }}
                onChange={(e) =>
                  patchDraft(split, draft.key, { rubric: e.target.value })
                }
              />
            </div>
            <div className="field" style={{ marginTop: 8 }}>
              <label>{t("skillLab.tasksets.assets.label")}</label>
              {/* A `<label className="btn">` would lose the button styling:
                  `.field label` (0,1,1) outranks `.btn` (0,1,0) and forces
                  display:block plus the dim 9.5px field-caption type. So drive
                  a hidden input from a real button, as CreateAgent does. */}
              <div>
                <Btn
                  disabled={draft.assetBusy}
                  onClick={() => assetInputs.current[draft.key]?.click()}
                >
                  {draft.assetBusy
                    ? t("skillLab.tasksets.assets.uploading")
                    : t("skillLab.tasksets.assets.pick")}
                </Btn>
                <span
                  className="mono dim"
                  style={{ fontSize: 10, marginLeft: 10 }}
                >
                  {t("skillLab.tasksets.assets.hint")}
                </span>
              </div>
              <input
                ref={(node) => {
                  assetInputs.current[draft.key] = node;
                }}
                type="file"
                multiple
                accept=".xlsx,.pdf,.png,.jpg,.jpeg,.webp,.md,.txt,.csv"
                style={{ display: "none" }}
                disabled={draft.assetBusy}
                data-testid={`task-assets-${split}-${index}`}
                onChange={(event) => {
                  // Snapshot the File objects first: clearing `value` (so the
                  // same filename can be re-picked) empties the live FileList.
                  const picked = Array.from(event.target.files ?? []);
                  event.target.value = "";
                  void uploadTaskAssets(split, draft.key, picked);
                }}
              />
              {draft.assets.map((asset) => (
                <div
                  key={asset.key}
                  data-testid={`task-asset-${split}-${index}-${asset.key}`}
                  style={{
                    display: "grid",
                    gridTemplateColumns: "1fr auto",
                    gap: 8,
                    marginTop: 6,
                  }}
                >
                  <input
                    className="input mono"
                    value={asset.path}
                    aria-label={t("skillLab.tasksets.assets.destination")}
                    onChange={(event) =>
                      renameTaskAsset(
                        split,
                        draft.key,
                        asset.key,
                        event.target.value,
                      )
                    }
                  />
                  <Btn
                    data-testid={`task-asset-remove-${split}-${index}-${asset.key}`}
                    onClick={() => removeTaskAsset(split, draft.key, asset.key)}
                  >
                    {t("skillLab.tasksets.assets.remove")}
                  </Btn>
                  <span
                    className="mono dim"
                    style={{ gridColumn: "1 / -1", fontSize: 10 }}
                  >
                    {asset.value.name} · {asset.value.media_type} ·{" "}
                    {asset.value.size.toLocaleString()} B
                  </span>
                </div>
              ))}
              {draft.assetError && (
                <div
                  className="mono"
                  style={{ color: "var(--crit)", fontSize: 10.5 }}
                >
                  {draft.assetError}
                </div>
              )}
            </div>

            {error && (
              <div
                className="mono"
                style={{ color: "var(--crit)", fontSize: 10.5, marginTop: 6 }}
              >
                {error}
              </div>
            )}
          </div>
        );
      })}

      {splitIssues(split).length > 0 && (
        <div
          className="note"
          data-testid={`taskset-error-${split}`}
          style={{ borderColor: "var(--crit)", marginBottom: 8 }}
        >
          <span className="i" style={{ color: "var(--crit)" }}>
            [✕]
          </span>
          <span className="mono" style={{ fontSize: 10.5 }}>
            {splitIssues(split)
              .map((issue) => issue.message)
              .join(" · ")}
          </span>
        </div>
      )}
    </div>
  );

  const formatHelp = (
    <div className="field">
      <button
        type="button"
        className="selchip"
        style={{ cursor: "pointer" }}
        data-testid="taskset-help-toggle"
        onClick={() => setHelpOpen((open) => !open)}
      >
        {helpOpen ? "▾" : "▸"} {t("skillLab.tasksets.help.title")}
      </button>
      {helpOpen && (
        <div style={{ marginTop: 8 }} data-testid="taskset-help">
          {(
            [
              "id",
              "question",
              "rubric",
              "taskType",
              "files",
              "judgeMode",
            ] as const
          ).map((row) => (
            <div className="kv" key={row}>
              <span className="k mono">
                {t(`skillLab.tasksets.help.field.${row}.key`)}
              </span>
              <span
                className="v"
                style={{ textAlign: "left", flex: 1, marginLeft: 12 }}
              >
                {t(`skillLab.tasksets.help.field.${row}.text`)}
              </span>
            </div>
          ))}
          <div
            style={{
              display: "flex",
              alignItems: "center",
              gap: 8,
              margin: "8px 0 4px",
            }}
          >
            <span className="mono dim" style={{ fontSize: 10.5 }}>
              {t("skillLab.tasksets.help.example")}
            </span>
            <Btn
              data-testid="taskset-help-copy"
              onClick={() => {
                void navigator.clipboard
                  ?.writeText(EXAMPLE_TASKS)
                  .then(() => toast(t("skillLab.tasksets.help.copied")));
              }}
            >
              {t("skillLab.tasksets.help.copy")}
            </Btn>
          </div>
          <pre
            className="code"
            style={{ fontSize: 10.5, whiteSpace: "pre-wrap" }}
          >
            {EXAMPLE_TASKS}
          </pre>
          <div className="note" style={{ marginTop: 8 }}>
            <span className="i">[i]</span>
            <span>{t("skillLab.tasksets.help.note")}</span>
          </div>
        </div>
      )}
    </div>
  );

  const editor = (
    <Panel
      brk
      title={
        editing
          ? t("skillLab.tasksets.editTitle")
          : t("skillLab.tasksets.createTitle")
      }
      sub={
        editing && selected
          ? `${selected.id} · ${selected.mode}`
          : t("skillLab.tasksets.createSub")
      }
      style={{ "--i": 1 } as CSSProperties}
    >
      {!editing && (
        <div className="field">
          <label>{t("skillLab.tasksets.field.mode")}</label>
          <div className="selchips">
            {(["single", "split"] as const).map((option) => (
              <button
                key={option}
                type="button"
                className={`selchip${mode === option ? " on" : ""}`}
                style={{ cursor: "pointer" }}
                data-testid={`taskset-mode-${option}`}
                onClick={() => changeMode(option)}
              >
                {t(`skillLab.tasksets.mode.${option}`)}
              </button>
            ))}
          </div>
          <span className="mono dim" style={{ fontSize: 10.5 }}>
            {t(`skillLab.tasksets.mode.${mode}Hint`)}
          </span>
        </div>
      )}

      <div className="field">
        <label>{t("skillLab.tasksets.field.name")}</label>
        <input
          className="input"
          value={name}
          data-testid="taskset-name"
          onChange={(e) => setName(e.target.value)}
        />
      </div>
      <div className="field">
        <label>{t("skillLab.tasksets.field.description")}</label>
        <input
          className="input"
          value={description}
          data-testid="taskset-description"
          onChange={(e) => setDescription(e.target.value)}
        />
      </div>

      <div className="field">
        <div className="selchips">
          <button
            type="button"
            className={`selchip${tab === "rows" ? " on" : ""}`}
            style={{ cursor: "pointer" }}
            data-testid="taskset-tab-rows"
            onClick={() => setTab("rows")}
          >
            {t("skillLab.tasksets.tab.rows")}
          </button>
          <button
            type="button"
            className={`selchip${tab === "upload" ? " on" : ""}`}
            style={{ cursor: "pointer" }}
            data-testid="taskset-tab-upload"
            onClick={() => setTab("upload")}
          >
            {t("skillLab.tasksets.tab.upload")}
          </button>
        </div>
      </div>

      {tab === "upload" ? (
        <div className="field">
          <label>{t("skillLab.tasksets.upload.label")}</label>
          {mode === "split" && (
            <select
              className="input"
              value={uploadSplit}
              aria-label={t("skillLab.tasksets.upload.targetSplit")}
              style={{ marginBottom: 8 }}
              onChange={(e) => setUploadSplit(e.target.value)}
            >
              {SPLIT_ORDER.map((split) => (
                <option
                  key={split}
                  value={split}
                  style={{ background: "var(--panel)" }}
                >
                  {split}
                </option>
              ))}
            </select>
          )}
          {/* Themed button + hidden input, as with the per-task asset picker:
              `className="input"` styles only the box around the browser's own
              native file button. */}
          <div>
            <Btn onClick={() => jsonRef.current?.click()}>
              {t("skillLab.tasksets.upload.pick")}
            </Btn>
          </div>
          <input
            ref={jsonRef}
            type="file"
            accept=".json,application/json"
            style={{ display: "none" }}
            data-testid="taskset-upload-input"
            onChange={(e) => {
              const file = e.target.files?.[0];
              e.target.value = "";
              if (file) void onFile(file);
            }}
          />
          <span className="mono dim" style={{ fontSize: 10.5 }}>
            {t("skillLab.tasksets.upload.hint")}
          </span>
          {uploadError && (
            <div
              className="note"
              data-testid="taskset-upload-error"
              style={{ borderColor: "var(--crit)", marginTop: 8 }}
            >
              <span className="i" style={{ color: "var(--crit)" }}>
                [✕]
              </span>
              <span className="mono" style={{ fontSize: 10.5 }}>
                {uploadError}
              </span>
            </div>
          )}
        </div>
      ) : (
        activeSplits.map(rowEditor)
      )}

      {uploadNote && (
        <div
          className="note"
          style={{ marginBottom: 10 }}
          data-testid="taskset-upload-note"
        >
          <span className="i">[i]</span>
          <span className="mono" style={{ fontSize: 10.5 }}>
            {uploadNote}
          </span>
        </div>
      )}

      {formatHelp}

      {globalIssues.length > 0 && (
        <div
          className="note"
          data-testid="taskset-error-global"
          style={{ borderColor: "var(--crit)", marginTop: 10 }}
        >
          <span className="i" style={{ color: "var(--crit)" }}>
            [✕]
          </span>
          <span className="mono" style={{ fontSize: 10.5 }}>
            {globalIssues
              .map((issue) => `${issue.split}: ${issue.message}`)
              .join(" · ")}
          </span>
        </div>
      )}
      {formError && (
        <div
          className="note"
          style={{ borderColor: "var(--crit)", margin: "10px 0" }}
        >
          <span className="i" style={{ color: "var(--crit)" }}>
            [✕]
          </span>
          <span>{formError}</span>
        </div>
      )}

      <div
        style={{
          display: "flex",
          justifyContent: "flex-end",
          gap: 8,
          marginTop: 10,
        }}
      >
        <Btn
          data-testid="taskset-cancel"
          onClick={() => {
            if (editing) {
              setEditing(false);
              setFormError(null);
              setIssues([]);
            } else {
              select(null);
            }
          }}
        >
          {t("common.cancel")}
        </Btn>
        <Btn
          primary
          disabled={busy || anyAssetBusy || hasMirrorErrors || !name.trim()}
          aria-busy={busy || anyAssetBusy}
          data-testid="taskset-save"
          onClick={() => void save()}
        >
          ▸{" "}
          {anyAssetBusy
            ? t("skillLab.tasksets.assets.uploading")
            : editing
              ? t("skillLab.tasksets.save")
              : t("skillLab.tasksets.create")}
        </Btn>
      </div>
    </Panel>
  );

  /* ── detail ─────────────────────────────────────────────────────────────── */

  const detailPanel = selected && (
    <Panel
      brk
      title={selected.name}
      sub={`${selected.id} · ${selected.mode}`}
      end={
        selected.sample ? (
          <Chip tone="aqua">{t("skillLab.tasksets.sampleChip")}</Chip>
        ) : (
          <>
            <Btn
              data-testid="taskset-edit"
              onClick={() => void startEdit(selected)}
            >
              {t("skillLab.tasksets.edit")}
            </Btn>
            <Btn
              data-testid="taskset-delete"
              onClick={() => setConfirmDelete(selected)}
            >
              {t("skillLab.tasksets.delete")}
            </Btn>
          </>
        )
      }
      style={{ "--i": 1 } as CSSProperties}
    >
      <div className="kv">
        <span className="k mono">
          {t("skillLab.tasksets.field.description")}
        </span>
        <span className="v">{selected.description || "—"}</span>
      </div>
      <div className="kv">
        <span className="k mono">{t("skillLab.tasksets.col.counts")}</span>
        <span className="v mono">{countsLabel(selected.counts)}</span>
      </div>
      <div className="kv">
        <span className="k mono">{t("skillLab.tasksets.col.updated")}</span>
        <span className="v mono">
          {selected.updated_at
            ? new Date(selected.updated_at).toLocaleString()
            : "—"}
        </span>
      </div>

      {detail === null ? (
        <div className="empty">{t("common.loading")}</div>
      ) : (
        splitsFor(selected.mode)
          .filter((split) => detail.tasks_by_split[split] !== undefined)
          .map((split) => (
            <div
              key={split}
              style={{ marginTop: 12 }}
              data-testid={`taskset-detail-${split}`}
            >
              <div
                className="mono"
                style={{
                  fontSize: 11,
                  letterSpacing: ".08em",
                  marginBottom: 6,
                }}
              >
                {split.toUpperCase()}
              </div>
              <DataTable
                columns={[
                  { key: "id", label: t("skillLab.tasksets.field.id") },
                  {
                    key: "question",
                    label: t("skillLab.tasksets.field.question"),
                  },
                  { key: "rubric", label: t("skillLab.tasksets.field.rubric") },
                  { key: "type", label: t("skillLab.tasksets.field.taskType") },
                  { key: "files", label: t("skillLab.tasksets.field.files") },
                ]}
              >
                {detail.tasks_by_split[split].map((task) => (
                  <tr key={String(task.id)}>
                    <td className="mono">{String(task.id)}</td>
                    <td>{excerpt(String(task.question ?? ""))}</td>
                    <td>{excerpt(String(task.rubric ?? ""))}</td>
                    <td className="mono dim">
                      {typeof task.task_type === "string"
                        ? task.task_type
                        : "—"}
                    </td>
                    <td className="mono dim">{fileCount(task) || "—"}</td>
                  </tr>
                ))}
              </DataTable>
            </div>
          ))
      )}
      {detail?.truncated && (
        <div className="note" style={{ marginTop: 10 }}>
          <span className="i">[i]</span>
          <span>{t("skillLab.tasksets.previewTruncated")}</span>
        </div>
      )}
    </Panel>
  );

  // A deep link to a set that was deleted (or belongs to another workspace) must
  // say so — otherwise the page just renders a list and swallows the `ts=` param.
  const staleSelection =
    !creating && !editing && !loading && tsParam !== null && selected === null;

  // The AI-generation sub-surface replaces the list+detail entirely (it has its
  // own job list); importing refreshes the sets and jumps to the new one.
  if (genParam) {
    return (
      <TaskgenPanel
        genParam={genParam}
        tasksets={rows}
        onSelectJob={selectGen}
        onImported={(tasksetId) => {
          void load();
          select(tasksetId);
        }}
      />
    );
  }

  return (
    <>
      {!creating && !editing && (
        <Panel
          brk
          pad={false}
          title={t("skillLab.tasksets.listTitle")}
          sub={t("skillLab.tasksets.listSub")}
          end={
            <div style={{ display: "flex", gap: 8 }}>
              <Btn
                data-testid="taskgen-open-btn"
                onClick={() => selectGen("new")}
              >
                ✳ {t("skillLab.taskgen.open")}
              </Btn>
              <Btn
                primary
                data-testid="new-taskset-btn"
                onClick={() => select("new")}
              >
                + {t("skillLab.tasksets.new")}
              </Btn>
            </div>
          }
          style={{ "--i": 0, marginBottom: 14 } as CSSProperties}
        >
          <table data-testid="skill-lab-taskset-table">
            <thead>
              <tr>
                <th>{t("skillLab.tasksets.col.name")}</th>
                <th>{t("skillLab.tasksets.col.mode")}</th>
                <th>{t("skillLab.tasksets.col.counts")}</th>
                <th>{t("skillLab.tasksets.col.updated")}</th>
              </tr>
            </thead>
            <tbody>
              {pageRows.map((row) => (
                <tr
                  key={row.id}
                  data-testid={`taskset-row-${row.id}`}
                  onClick={() => select(row.id)}
                  style={{
                    cursor: "pointer",
                    background:
                      tsParam === row.id ? "rgba(var(--amber-rgb),.045)" : undefined,
                  }}
                >
                  <td className="pri">
                    {row.name}
                    {row.sample && (
                      <>
                        {" "}
                        <Chip tone="aqua">
                          {t("skillLab.tasksets.sampleChip")}
                        </Chip>
                      </>
                    )}
                  </td>
                  <td>
                    <Chip tone={row.mode === "split" ? "aqua" : "muted"}>
                      {t(`skillLab.tasksets.mode.${row.mode}`)}
                    </Chip>
                  </td>
                  <td className="mono dim">{countsLabel(row.counts)}</td>
                  <td className="mono dim">
                    {row.updated_at
                      ? new Date(row.updated_at).toLocaleString()
                      : "—"}
                  </td>
                </tr>
              ))}
              {loading && (
                <tr>
                  <td
                    colSpan={4}
                    className="dim mono"
                    style={{ textAlign: "center" }}
                  >
                    {t("common.loading")}
                  </td>
                </tr>
              )}
              {!loading && rows.length === 0 && listError === null && (
                <tr>
                  <td
                    colSpan={4}
                    className="dim mono"
                    style={{ textAlign: "center" }}
                  >
                    {t("skillLab.tasksets.empty")}
                  </td>
                </tr>
              )}
              {listError !== null && (
                <tr>
                  <td
                    colSpan={4}
                    className="dim mono"
                    style={{ textAlign: "center" }}
                  >
                    {listError}
                  </td>
                </tr>
              )}
            </tbody>
          </table>
          <Pager {...pagerProps} always />
        </Panel>
      )}

      {creating || editing ? (
        editor
      ) : staleSelection ? (
        <Panel
          brk
          title={t("skillLab.tasksets.gone.title")}
          style={{ "--i": 1 } as CSSProperties}
        >
          <div className="empty" data-testid="taskset-gone">
            {t("skillLab.tasksets.gone.body")}
          </div>
        </Panel>
      ) : (
        detailPanel
      )}

      <ConfirmDialog
        open={confirmDelete !== null}
        title={t("skillLab.tasksets.confirmDelete.title")}
        body={t("skillLab.tasksets.confirmDelete.body", {
          name: confirmDelete?.name ?? "",
        })}
        confirmLabel={t("skillLab.tasksets.delete")}
        onConfirm={() => {
          const row = confirmDelete;
          setConfirmDelete(null);
          if (row) void remove(row);
        }}
        onCancel={() => setConfirmDelete(null)}
      />
    </>
  );
}
