import { useState } from "react";
import { useTranslation } from "react-i18next";

import {
  api,
  ApiError,
  errorMessage,
  type V2FromSessionsResult,
  type V2Range,
  type V2SkippedSession,
} from "../../../lib/api";
import { useLoad, useV2Toast } from "../../hooks";
import { Alert, Button, Field, Modal, Segmented } from "../../ui";

/**
 * "加入数据集" — turn observed sessions (the trajectories behind the selected
 * traces) into dataset items: appended to an existing local dataset, or as a
 * new one. Shared by the trajectory list, the trajectory detail and insights.
 */
export function AddToDatasetModal({
  open,
  sessionIds,
  range,
  onClose,
  onDone,
}: {
  open: boolean;
  sessionIds: string[];
  range: V2Range;
  onClose: () => void;
  onDone?: (result: V2FromSessionsResult) => void;
}) {
  if (!open) return null;
  return <AddToDatasetBody sessionIds={sessionIds} range={range} onClose={onClose} onDone={onDone} />;
}

function AddToDatasetBody({
  sessionIds,
  range,
  onClose,
  onDone,
}: {
  sessionIds: string[];
  range: V2Range;
  onClose: () => void;
  onDone?: (result: V2FromSessionsResult) => void;
}) {
  const { t } = useTranslation();
  const toast = useV2Toast();
  const datasets = useLoad(() => api.v2Datasets(), "datasets");
  const receivable = (datasets.data?.datasets ?? []).filter((d) => d.kind !== "simulated");
  const [mode, setMode] = useState<"existing" | "new">("new");
  const [datasetId, setDatasetId] = useState("");
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [firstTurnOnly, setFirstTurnOnly] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<V2FromSessionsResult | null>(null);
  const [refused, setRefused] = useState<V2SkippedSession[]>([]);

  const submit = async () => {
    if (mode === "existing" && !datasetId) return setError(t("v2.addToDataset.errPick"));
    if (mode === "new" && !name.trim()) return setError(t("v2.addToDataset.errName"));
    setBusy(true);
    setError(null);
    try {
      const res = await api.v2DatasetFromSessions({
        session_ids: sessionIds,
        range,
        first_turn_only: firstTurnOnly,
        ...(mode === "existing" ? { dataset_id: datasetId } : { name: name.trim(), description }),
      });
      setResult(res);
      toast("success", t("v2.addToDataset.done", { count: res.added, name: res.dataset.name }));
      onDone?.(res);
    } catch (err) {
      setError(errorMessage(err));
      // nothing usable: the backend says why per session
      const detail = err instanceof ApiError ? (err.detail as { skipped?: V2SkippedSession[] } | null) : null;
      setRefused((detail?.skipped ?? []).filter((s) => s.session_id));
    } finally {
      setBusy(false);
    }
  };

  const skippedBySession = (result?.skipped ?? []).filter((s) => s.session_id);
  const duplicates = (result?.skipped ?? []).filter((s) => !s.session_id).length;

  return (
    <Modal
      open
      title={t("v2.addToDataset.title")}
      onClose={onClose}
      testId="v2-add-to-dataset"
      footer={
        result ? (
          <Button kind="primary" onClick={onClose}>
            {t("v2.common.done")}
          </Button>
        ) : (
          <>
            <Button onClick={onClose}>{t("v2.common.cancel")}</Button>
            <Button kind="primary" disabled={busy} onClick={() => void submit()} testId="v2-add-to-dataset-ok">
              {t("v2.common.confirm")}
            </Button>
          </>
        )
      }
    >
      {result ? (
        <div className="v2-stack">
          <Alert tone="success">
            {t("v2.addToDataset.result", { count: result.added, name: result.dataset.name, total: result.dataset.item_count })}
          </Alert>
          {duplicates > 0 && <Alert tone="warn">{t("v2.addToDataset.duplicates", { count: duplicates })}</Alert>}
          {skippedBySession.length > 0 && (
            <Alert tone="warn">
              {t("v2.addToDataset.skipped", { count: skippedBySession.length })}
              <ul style={{ margin: "4px 0 0 16px" }}>
                {skippedBySession.slice(0, 6).map((s) => (
                  <li key={s.session_id}>
                    <span className="mono">{s.session_id}</span> · {t(`v2.skipReason.${s.reason}`, { defaultValue: s.reason })}
                  </li>
                ))}
              </ul>
            </Alert>
          )}
        </div>
      ) : (
        <div className="v2-form">
          <Alert>{t("v2.addToDataset.hint", { count: sessionIds.length })}</Alert>
          {error && (
            <Alert tone="error">
              {error}
              {refused.length > 0 && (
                <ul style={{ margin: "4px 0 0 16px" }}>
                  {refused.slice(0, 6).map((s) => (
                    <li key={s.session_id}>
                      <span className="mono">{s.session_id}</span> · {t(`v2.skipReason.${s.reason}`, { defaultValue: s.reason })}
                    </li>
                  ))}
                </ul>
              )}
            </Alert>
          )}
          <Field label={t("v2.addToDataset.target")}>
            <Segmented
              value={mode}
              onChange={setMode}
              options={[
                { value: "new", label: t("v2.addToDataset.new") },
                { value: "existing", label: t("v2.addToDataset.existing") },
              ]}
            />
          </Field>
          {mode === "existing" ? (
            <Field label={t("v2.addToDataset.dataset")} required>
              <select className="v2-select" value={datasetId} onChange={(e) => setDatasetId(e.target.value)}>
                <option value="">{t("v2.common.choose")}</option>
                {receivable.map((d) => (
                  <option key={d.id} value={d.id}>
                    {d.name} · {t("v2.datasets.items", { count: d.item_count })}
                  </option>
                ))}
              </select>
            </Field>
          ) : (
            <>
              <Field label={t("v2.datasets.colName")} required>
                <input className="v2-input" value={name} maxLength={64} onChange={(e) => setName(e.target.value)} data-testid="v2-add-to-dataset-name" />
              </Field>
              <Field label={t("v2.datasets.description")}>
                <input className="v2-input" value={description} maxLength={1000} onChange={(e) => setDescription(e.target.value)} />
              </Field>
            </>
          )}
          <label className="v2-check">
            <input type="checkbox" checked={firstTurnOnly} onChange={(e) => setFirstTurnOnly(e.target.checked)} />
            {t("v2.addToDataset.firstTurnOnly")}
          </label>
        </div>
      )}
    </Modal>
  );
}
