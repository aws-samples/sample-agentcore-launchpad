import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import {
  api,
  ApiError,
  errorMessage,
  type ModelProbeOut,
  type ModelProviderOut,
  type ModelProviderProbeOut,
  type ModelRowOut,
} from "../../../lib/api";
import { invalidateCapabilities } from "../../../lib/capabilities";
import { useV2Toast } from "../../hooks";
import { Alert, Button, Modal, Spin, Tag } from "../../ui";
import { isProbeUnavailable, SOURCE_TONE } from "./common";

const FINDING_KEYS = ["exists", "tools", "streaming", "temperature", "top_p", "embedding_dimension"] as const;

function fmtValue(value: unknown): string {
  if (value === true) return "✓";
  if (value === false) return "✗";
  if (value === null || value === undefined) return "—";
  return String(value);
}

/** The §7.4 findings of a failed add-with-probe (422 `model.probe_failed` → `detail`). */
export function ProbeFindings({ error }: { error: unknown }) {
  const { t } = useTranslation();
  if (!(error instanceof ApiError)) return <div>{errorMessage(error)}</div>;
  const detail = (error.detail ?? {}) as { findings?: Record<string, unknown>; errors?: Record<string, string> };
  const findings = detail.findings ?? {};
  const errors = detail.errors ?? {};
  return (
    <div className="v2-mp-probe" data-testid="v2-mp-probe-findings">
      <div className="row">
        {FINDING_KEYS.filter((k) => k in findings).map((k) => (
          <Tag key={k} tone={findings[k] === false ? "red" : "green"}>
            {t(`modelProviders.finding.${k}`)} {fmtValue(findings[k])}
          </Tag>
        ))}
      </div>
      {Object.entries(errors).map(([field, msg]) => (
        <div className="err" key={field}>
          <b>{t(`modelProviders.finding.${field}`, field)}</b>: {msg}
        </div>
      ))}
    </div>
  );
}

/**
 * Provider connectivity probe (plan §7.1): runs on open, shows reachability, latency,
 * the model ids the endpoint lists, or the sanitised error. A server without the probe
 * module (501 `model_providers.probe_unavailable`) gets the "not available yet" hint.
 */
export function ProviderProbeDialog({
  provider,
  onClose,
  onDone,
}: {
  provider: ModelProviderOut;
  onClose: () => void;
  onDone: () => void;
}) {
  const { t } = useTranslation();
  const [result, setResult] = useState<ModelProviderProbeOut | null>(null);
  const [error, setError] = useState<{ unavailable: boolean; message: string } | null>(null);
  useEffect(() => {
    let live = true;
    api.modelProviders
      .probe(provider.id)
      .then((r) => {
        if (!live) return;
        setResult(r);
        onDone();
      })
      .catch((err: unknown) => live && setError({ unavailable: isProbeUnavailable(err), message: errorMessage(err) }));
    return () => {
      live = false;
    };
  }, [provider.id, onDone]);
  return (
    <Modal
      open
      title={t("modelProviders.probe.providerTitle", { label: provider.label })}
      onClose={onClose}
      testId="v2-mp-provider-probe"
      footer={<Button kind="primary" onClick={onClose}>{t("v2.common.close")}</Button>}
    >
      {error ? (
        <Alert tone={error.unavailable ? "warn" : "error"}>
          <span data-testid="v2-mp-probe-error">{error.unavailable ? t("modelProviders.probe.unavailable") : error.message}</span>
        </Alert>
      ) : !result ? (
        <Spin label={t("modelProviders.probe.running")} />
      ) : (
        <div className="v2-mp-probe" data-testid="v2-mp-probe-result">
          <div className="row">
            <Tag tone={result.state === "ok" ? "green" : "red"} dot>
              {t(`modelProviders.probeState.${result.state}`)}
            </Tag>
            {result.latency_ms != null && <span>{t("modelProviders.probe.latency", { latency: result.latency_ms })}</span>}
            <span className="v2-muted">{t("modelProviders.probe.fromHost")}</span>
          </div>
          {result.error && <div className="err">{result.error}</div>}
          {result.models_seen.length > 0 && (
            <>
              <div className="v2-muted">{t("modelProviders.probe.modelsSeen", { count: result.models_seen.length })}</div>
              <div className="seen">
                {result.models_seen.map((id) => (
                  <Tag key={id} tone="outline">
                    {id}
                  </Tag>
                ))}
              </div>
            </>
          )}
        </div>
      )}
    </Modal>
  );
}

/**
 * Model re-probe (plan §7.4): a dry run on open, the stored-vs-probed diff, and *Apply
 * probed values* which writes only into fields whose source is not `manual`.
 */
export function ModelProbeDialog({
  provider,
  model,
  onClose,
  onApplied,
}: {
  provider: ModelProviderOut;
  model: ModelRowOut;
  onClose: () => void;
  onApplied: () => void;
}) {
  const { t } = useTranslation();
  const toast = useV2Toast();
  const [result, setResult] = useState<ModelProbeOut | null>(null);
  const [error, setError] = useState<{ unavailable: boolean; message: string } | null>(null);
  const [applying, setApplying] = useState(false);
  useEffect(() => {
    let live = true;
    api.modelProviders
      .probeModel(provider.id, model.id, false)
      .then((r) => live && setResult(r))
      .catch((err: unknown) => live && setError({ unavailable: isProbeUnavailable(err), message: errorMessage(err) }));
    return () => {
      live = false;
    };
  }, [provider.id, model.id]);

  const diff = Object.entries(result?.diff ?? {});
  const applicable = diff.filter(([, d]) => d.source !== "manual" && d.stored !== d.probed);

  const apply = async () => {
    setApplying(true);
    try {
      await api.modelProviders.probeModel(provider.id, model.id, true);
      invalidateCapabilities();
      toast("success", t("modelProviders.toast.probeApplied", { label: model.label }));
      onApplied();
      onClose();
    } catch (err) {
      toast("error", t("modelProviders.actionFailed", { msg: errorMessage(err) }));
    } finally {
      setApplying(false);
    }
  };

  return (
    <Modal
      open
      title={t("modelProviders.probe.modelTitle", { label: model.label })}
      onClose={onClose}
      testId="v2-mp-model-probe"
      footer={
        <>
          <Button onClick={onClose}>{t("v2.common.close")}</Button>
          <Button kind="primary" disabled={!result || applying || applicable.length === 0} onClick={() => void apply()} testId="v2-mp-probe-apply">
            {t("modelProviders.action.applyProbed")}
          </Button>
        </>
      }
    >
      {error ? (
        <Alert tone={error.unavailable ? "warn" : "error"}>
          <span data-testid="v2-mp-probe-error">{error.unavailable ? t("modelProviders.probe.unavailable") : error.message}</span>
        </Alert>
      ) : !result ? (
        <Spin label={t("modelProviders.probe.running")} />
      ) : (
        <div className="v2-mp-probe" data-testid="v2-mp-probe-diff">
          <div className="row">
            {FINDING_KEYS.filter((k) => k in result.findings).map((k) => (
              <Tag key={k} tone={(result.findings as Record<string, unknown>)[k] === false ? "red" : "green"}>
                {t(`modelProviders.finding.${k}`)} {fmtValue((result.findings as Record<string, unknown>)[k])}
              </Tag>
            ))}
            {result.latency_ms != null && <span>{t("modelProviders.probe.latency", { latency: result.latency_ms })}</span>}
          </div>
          {Object.entries(result.errors ?? {}).map(([field, msg]) => (
            <div className="err" key={field}>
              <b>{t(`modelProviders.finding.${field}`, field)}</b>: {msg}
            </div>
          ))}
          {result.raw_hint && (result.raw_hint.max_output_tokens || result.raw_hint.context_length) && (
            <Alert tone="info">
              {t("modelProviders.probe.rawHint", {
                max: result.raw_hint.max_output_tokens ?? "—",
                context: result.raw_hint.context_length ?? "—",
              })}
            </Alert>
          )}
          <div className="v2-table-wrap">
            <table className="v2-table dense v2-mp-diff">
              <thead>
                <tr>
                  <th>{t("modelProviders.diff.field")}</th>
                  <th>{t("modelProviders.diff.stored")}</th>
                  <th>{t("modelProviders.diff.probed")}</th>
                  <th>{t("modelProviders.col.source")}</th>
                </tr>
              </thead>
              <tbody>
                {diff.length === 0 && (
                  <tr>
                    <td colSpan={4} className="v2-muted">
                      {t("modelProviders.diff.none")}
                    </td>
                  </tr>
                )}
                {diff.map(([field, d]) => {
                  const changed = d.stored !== d.probed;
                  return (
                    <tr key={field} data-testid={`v2-mp-diff-${field}`}>
                      <td>{t(`modelProviders.cap.${field}`, field)}</td>
                      <td>
                        <code>{fmtValue(d.stored)}</code>
                      </td>
                      <td className={changed ? "changed" : undefined}>
                        <code>{fmtValue(d.probed)}</code>
                      </td>
                      <td>
                        <Tag tone={SOURCE_TONE[d.source] ?? "gray"} title={d.source === "manual" ? t("modelProviders.diff.manualKept") : undefined}>
                          {t(`modelProviders.source.${d.source}`)}
                        </Tag>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
          <span className="v2-muted">{t("modelProviders.diff.applyHint")}</span>
        </div>
      )}
    </Modal>
  );
}
