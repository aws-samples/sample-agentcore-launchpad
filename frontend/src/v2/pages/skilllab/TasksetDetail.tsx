import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { useSearchParams } from "react-router-dom";

import { api, ApiError, errorMessage, type SkillLabAssetDescriptor, type SkillLabTask } from "../../../lib/api";
import { countsLabel, excerpt, fileCount, splitsFor } from "../../../lib/skillLabTasksets";
import { fmtTime } from "../../format";
import { useLoad, useV2Toast } from "../../hooks";
import {
  Alert,
  Button,
  Card,
  Confirm,
  Descriptions,
  Drawer,
  FlowHeader,
  LinkButton,
  Segmented,
  Spin,
  Table,
  Tag,
} from "../../ui";
import { GoneCard } from "./common";

const str = (value: unknown) => (typeof value === "string" ? value : "");

/** A task's `files` map: inline text or an uploaded asset descriptor per path. */
function TaskFiles({ task }: { task: SkillLabTask }) {
  const { t } = useTranslation();
  const files =
    task.files !== null && typeof task.files === "object" && !Array.isArray(task.files)
      ? (task.files as Record<string, string | SkillLabAssetDescriptor>)
      : {};
  const entries = Object.entries(files);
  if (entries.length === 0) return <span className="v2-muted">—</span>;
  return (
    <div className="v2-stack">
      {entries.map(([path, value]) =>
        typeof value === "string" ? (
          <div key={path}>
            <div className="mono">{path}</div>
            <pre className="v2-pre" style={{ maxHeight: 160 }}>
              {value}
            </pre>
          </div>
        ) : (
          <div key={path} className="v2-row">
            <span className="mono">{path}</span>
            <Tag tone="outline">{value.media_type}</Tag>
            <span className="v2-muted">
              {value.name} · {value.size.toLocaleString()} B
            </span>
          </div>
        ),
      )}
      <span className="v2-muted">{t("skillLab.tasksets.filesChip", { count: entries.length })}</span>
    </div>
  );
}

export function TasksetDetail({ id }: { id: string }) {
  const { t } = useTranslation();
  const [, setParams] = useSearchParams();
  const toast = useV2Toast();
  // a missing set (deleted, or another workspace's) resolves to null, not an error
  const detail = useLoad(
    () =>
      api.skillLabTasksetGet(id).catch((err: unknown) => {
        if (err instanceof ApiError && err.code === "skill_lab.taskset_not_found") return null;
        throw err;
      }),
    `skill-lab-taskset:${id}`,
  );
  const [split, setSplit] = useState<string | null>(null);
  const [openTask, setOpenTask] = useState<SkillLabTask | null>(null);
  const [confirm, setConfirm] = useState(false);
  const [busy, setBusy] = useState(false);
  const back = () => setParams({ tab: "tasksets" });

  const info = detail.data?.info ?? null;
  const splits = useMemo(
    () => (info ? splitsFor(info.mode).filter((s) => detail.data?.tasks_by_split[s] !== undefined) : []),
    [info, detail.data],
  );
  const current = split && splits.includes(split) ? split : (splits[0] ?? null);
  const tasks = current ? (detail.data?.tasks_by_split[current] ?? []) : [];

  if (!detail.loading && !detail.error && detail.data === null) {
    return <GoneCard title={t("skillLab.tasksets.gone.title")} body={t("skillLab.tasksets.gone.body")} onBack={back} />;
  }
  if (detail.error && !detail.data) {
    return (
      <>
        <FlowHeader title={id} onBack={back} />
        <Alert tone="error" action={<Button size="sm" onClick={detail.reload}>{t("v2.common.retry")}</Button>}>
          {detail.error}
        </Alert>
      </>
    );
  }
  if (!info) return <Spin />;

  const remove = async () => {
    setBusy(true);
    try {
      await api.skillLabTasksetDelete(info.id);
      toast("success", t("skillLab.tasksets.deleted"));
      back();
    } catch (err) {
      toast("error", errorMessage(err));
    } finally {
      setBusy(false);
      setConfirm(false);
    }
  };

  return (
    <>
      <FlowHeader
        title={
          <span className="v2-row">
            {info.name}
            <Tag tone={info.mode === "split" ? "blue" : "outline"}>{t(`skillLab.tasksets.mode.${info.mode}`)}</Tag>
            {info.sample && <Tag tone="blue">{t("skillLab.tasksets.sampleChip")}</Tag>}
          </span>
        }
        onBack={back}
        end={
          <>
            <Button onClick={() => setParams({ tab: "eval", view: "new", taskset: info.id })} testId="v2-taskset-eval">
              {t("v2.skillLab.evalWith")}
            </Button>
            <Button onClick={() => setParams({ tab: "train", view: "new", taskset: info.id })} testId="v2-taskset-train">
              {t("v2.skillLab.trainWith")}
            </Button>
            {!info.sample && (
              <>
                <Button onClick={() => setParams({ tab: "tasksets", view: "edit", id: info.id })} testId="v2-taskset-edit">
                  {t("v2.common.edit")}
                </Button>
                <Button kind="danger" disabled={busy} onClick={() => setConfirm(true)} testId="v2-taskset-delete">
                  {t("v2.common.delete")}
                </Button>
              </>
            )}
          </>
        }
      />
      {info.sample && <Alert>{t("v2.skillLab.sampleReadOnly")}</Alert>}
      <Card title={t("v2.skillLab.overview")}>
        <Descriptions
          items={[
            { label: "ID", value: <span className="mono">{info.id}</span> },
            { label: t("skillLab.tasksets.col.mode"), value: t(`skillLab.tasksets.mode.${info.mode}Hint`) },
            { label: t("skillLab.tasksets.col.counts"), value: <span className="mono">{countsLabel(info.counts)}</span> },
            { label: t("skillLab.tasksets.field.description"), value: info.description || "—" },
            { label: t("v2.common.createdAt"), value: fmtTime(info.created_at) },
            { label: t("skillLab.tasksets.col.updated"), value: fmtTime(info.updated_at) },
          ]}
        />
      </Card>
      <Card
        title={t("v2.skillLab.tasks")}
        end={
          splits.length > 1 && current ? (
            <Segmented
              value={current}
              onChange={setSplit}
              options={splits.map((s) => ({ value: s, label: `${s} · ${info.counts[s] ?? 0}` }))}
            />
          ) : undefined
        }
        testId="v2-taskset-tasks"
      >
        {detail.data?.truncated && <Alert>{t("skillLab.tasksets.previewTruncated")}</Alert>}
        <Table
          columns={[
            { key: "id", title: t("skillLab.tasksets.field.id"), className: "nowrap mono", render: (task: SkillLabTask) => String(task.id) },
            {
              key: "question",
              title: t("skillLab.tasksets.field.question"),
              render: (task: SkillLabTask) => <span title={str(task.question)}>{excerpt(str(task.question))}</span>,
            },
            {
              key: "rubric",
              title: t("skillLab.tasksets.field.rubric"),
              render: (task: SkillLabTask) => <span title={str(task.rubric)}>{excerpt(str(task.rubric))}</span>,
            },
            {
              key: "type",
              title: t("skillLab.tasksets.field.taskType"),
              className: "nowrap mono",
              render: (task: SkillLabTask) => str(task.task_type) || "—",
            },
            { key: "files", title: t("skillLab.tasksets.field.files"), className: "num", render: (task: SkillLabTask) => fileCount(task) || "—" },
            {
              key: "ops",
              title: t("v2.common.actions"),
              className: "right",
              render: (task: SkillLabTask) => <LinkButton onClick={() => setOpenTask(task)}>{t("v2.common.view")}</LinkButton>,
            },
          ]}
          rows={tasks}
          rowKey={(task) => String(task.id)}
          loading={detail.loading}
        />
      </Card>
      <Drawer
        open={openTask !== null}
        title={<span className="mono">{openTask ? String(openTask.id) : ""}</span>}
        onClose={() => setOpenTask(null)}
        testId="v2-taskset-task-drawer"
      >
        {openTask && (
          <div className="v2-stack">
            <Descriptions
              one
              items={[
                { label: t("skillLab.tasksets.field.taskType"), value: str(openTask.task_type) || "—" },
                ...(typeof openTask.judge_mode === "string" ? [{ label: "judge_mode", value: openTask.judge_mode }] : []),
              ]}
            />
            <h3 className="v2-sub-title">{t("skillLab.tasksets.field.question")}</h3>
            <pre className="v2-pre">{str(openTask.question)}</pre>
            <h3 className="v2-sub-title">{t("skillLab.tasksets.field.rubric")}</h3>
            <pre className="v2-pre">{str(openTask.rubric)}</pre>
            <h3 className="v2-sub-title">{t("skillLab.tasksets.field.files")}</h3>
            <TaskFiles task={openTask} />
          </div>
        )}
      </Drawer>
      <Confirm
        open={confirm}
        title={t("skillLab.tasksets.confirmDelete.title")}
        body={t("skillLab.tasksets.confirmDelete.body", { name: info.name })}
        confirmLabel={t("v2.common.delete")}
        danger
        busy={busy}
        onConfirm={() => void remove()}
        onClose={() => setConfirm(false)}
      />
    </>
  );
}
