import { RefreshCw } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import { api, errorMessage, type V2LogFormat, type V2LogsPreview, type V2PipelineSource, type V2Range } from "../../../lib/api";
import {
  cleanSource,
  defaultLogFormat,
  GENAI_SESSION_FIELD,
  LOG_PRESETS,
  logSourceProblem,
  pathOptions,
  PROBLEM_KEY,
  switchPreset,
} from "../../../lib/pipelineLogs";
import { fmtTime, RANGES, rangeLabel } from "../../format";
import { Alert, Button, Card, Field, OptionCard, Select, Spin, Tag } from "../../ui";
import { LogGroupsInput } from "../tasks/LogSourceFields";

/** The 运行日志 source fields of the pipeline wizard's first step. */
export function LogSourceFields({
  source,
  onChange,
}: {
  source: V2PipelineSource;
  onChange: (patch: Partial<V2PipelineSource>) => void;
}) {
  const { t } = useTranslation();
  return (
    <div className="v2-form cols-2">
      <Field label={t("v2.pipelines.logs.groups")} required hint={t("v2.pipelines.logs.groupsHint")} full>
        <LogGroupsInput value={source.log_groups ?? []} onChange={(log_groups) => onChange({ log_groups })} />
      </Field>
      <Field label={t("v2.common.timeRange")}>
        <Select value={source.range} options={RANGES.map((r) => ({ value: r, label: rangeLabel(t, r) }))} onChange={(v) => onChange({ range: v as V2Range })} />
      </Field>
      <Field label={t("v2.pipelines.logs.keyword")} hint={t("v2.pipelines.logs.keywordHint")}>
        <input
          className="v2-input"
          value={source.keyword ?? ""}
          maxLength={128}
          onChange={(e) => onChange({ keyword: e.target.value })}
          data-testid="v2-pipeline-logs-keyword"
        />
      </Field>
      <Field label={t("v2.pipelines.maxSessions")} hint={t("v2.pipelines.maxSessionsHint")}>
        <input
          className="v2-input"
          type="number"
          min={1}
          max={50}
          value={source.max_sessions}
          onChange={(e) => onChange({ max_sessions: Math.max(1, Math.min(50, Number(e.target.value) || 1)) })}
        />
      </Field>
    </div>
  );
}

function PathInput({
  label,
  hint,
  required,
  value,
  placeholder,
  list,
  onChange,
  testId,
}: {
  label: string;
  hint: string;
  required?: boolean;
  value: string | null | undefined;
  placeholder?: string;
  list: string;
  onChange: (value: string) => void;
  testId: string;
}) {
  return (
    <Field label={label} hint={hint} required={required}>
      <input
        className="v2-input mono"
        value={value ?? ""}
        maxLength={128}
        placeholder={placeholder}
        list={list}
        onChange={(e) => onChange(e.target.value)}
        data-testid={testId}
      />
    </Field>
  );
}

/**
 * 格式转换: the rule that turns each log record into conversation turns, beside a
 * preview of what it extracts right now (`POST /api/eval/pipelines/preview-logs`
 * — the converter a run uses). The preview refreshes on demand; it is loaded once
 * when the step opens with a complete rule.
 */
export function LogFormatStep({
  source,
  onChange,
}: {
  source: V2PipelineSource;
  onChange: (patch: Partial<V2PipelineSource>) => void;
}) {
  const { t } = useTranslation();
  const fmt = source.format ?? defaultLogFormat();
  const setFmt = (patch: Partial<V2LogFormat>) => onChange({ format: { ...fmt, ...patch } });
  const problem = logSourceProblem(source);
  const [preview, setPreview] = useState<V2LogsPreview | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const latest = useRef(source);
  latest.current = source;

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setPreview(await api.v2PreviewPipelineLogs(cleanSource(latest.current)));
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setLoading(false);
    }
  }, []);
  const opened = useRef(false);
  useEffect(() => {
    if (opened.current || logSourceProblem(latest.current)) return;
    opened.current = true;
    void load();
  }, [load]);

  const paths = preview?.paths ?? [];
  const roleList = (roles: string[] | undefined) => (roles ?? []).join(",");
  const reasons = Object.entries(preview?.failed ?? {});

  return (
    <>
      <Card title={t("v2.pipelines.logs.formatTitle")} sub={t("v2.pipelines.logs.formatSub")}>
        <datalist id="v2-log-paths">
          {pathOptions(paths).map((p) => (
            <option key={p} value={p} />
          ))}
        </datalist>
        <datalist id="v2-log-session-paths">
          {pathOptions(paths, true).map((p) => (
            <option key={p} value={p} />
          ))}
        </datalist>
        <div className="v2-options" style={{ marginBottom: 20 }}>
          {LOG_PRESETS.map((preset) => (
            <OptionCard
              key={preset}
              title={t(`v2.pipelines.logs.preset.${preset}`)}
              desc={t(`v2.pipelines.logs.presetDesc.${preset}`)}
              on={fmt.preset === preset}
              onClick={() => onChange({ format: switchPreset(fmt, preset) })}
              testId={`v2-pipeline-logs-preset-${preset}`}
            />
          ))}
        </div>
        <div className="v2-form cols-2">
          {fmt.preset === "genai" && (
            <PathInput
              label={t("v2.pipelines.logs.sessionField")}
              hint={t("v2.pipelines.logs.sessionGenaiHint")}
              value={fmt.session_field}
              placeholder={GENAI_SESSION_FIELD}
              list="v2-log-session-paths"
              onChange={(session_field) => setFmt({ session_field })}
              testId="v2-pipeline-logs-session"
            />
          )}
          {fmt.preset === "message" && (
            <>
              <PathInput
                label={t("v2.pipelines.logs.sessionField")}
                hint={t("v2.pipelines.logs.sessionHint")}
                required
                value={fmt.session_field}
                placeholder="conversation_id"
                list="v2-log-session-paths"
                onChange={(session_field) => setFmt({ session_field })}
                testId="v2-pipeline-logs-session"
              />
              <PathInput
                label={t("v2.pipelines.logs.textField")}
                hint={t("v2.pipelines.logs.textHint")}
                required
                value={fmt.text_field}
                placeholder="content"
                list="v2-log-paths"
                onChange={(text_field) => setFmt({ text_field })}
                testId="v2-pipeline-logs-text"
              />
              <PathInput
                label={t("v2.pipelines.logs.roleField")}
                hint={t("v2.pipelines.logs.roleHint")}
                required
                value={fmt.role_field}
                placeholder="role"
                list="v2-log-paths"
                onChange={(role_field) => setFmt({ role_field })}
                testId="v2-pipeline-logs-role"
              />
              <Field label={t("v2.pipelines.logs.userRoles")} hint={t("v2.pipelines.logs.rolesHint")}>
                <input className="v2-input mono" value={roleList(fmt.user_roles)} onChange={(e) => setFmt({ user_roles: e.target.value.split(",") })} />
              </Field>
              <Field label={t("v2.pipelines.logs.assistantRoles")} hint={t("v2.pipelines.logs.rolesHint")}>
                <input className="v2-input mono" value={roleList(fmt.assistant_roles)} onChange={(e) => setFmt({ assistant_roles: e.target.value.split(",") })} />
              </Field>
            </>
          )}
          {fmt.preset === "exchange" && (
            <>
              <PathInput
                label={t("v2.pipelines.logs.inputField")}
                hint={t("v2.pipelines.logs.inputHint")}
                required
                value={fmt.input_field}
                placeholder="query"
                list="v2-log-paths"
                onChange={(input_field) => setFmt({ input_field })}
                testId="v2-pipeline-logs-input"
              />
              <PathInput
                label={t("v2.pipelines.logs.outputField")}
                hint={t("v2.pipelines.logs.outputHint")}
                value={fmt.output_field}
                placeholder="answer"
                list="v2-log-paths"
                onChange={(output_field) => setFmt({ output_field })}
                testId="v2-pipeline-logs-output"
              />
              <PathInput
                label={t("v2.pipelines.logs.sessionField")}
                hint={t("v2.pipelines.logs.sessionExchangeHint")}
                value={fmt.session_field}
                placeholder="request_id"
                list="v2-log-session-paths"
                onChange={(session_field) => setFmt({ session_field })}
                testId="v2-pipeline-logs-session"
              />
            </>
          )}
        </div>
      </Card>

      <Card
        title={t("v2.pipelines.logs.previewTitle")}
        sub={preview ? t("v2.pipelines.logs.previewStats", { events: preview.events, parsed: preview.parsed, sessions: preview.sessions_found }) : undefined}
        end={
          <Button size="sm" disabled={loading || problem !== null} onClick={() => void load()} testId="v2-pipeline-logs-preview">
            <RefreshCw size={13} aria-hidden="true" />
            {t("v2.pipelines.logs.refresh")}
          </Button>
        }
        testId="v2-pipeline-logs-preview-card"
      >
        <div className="v2-stack" style={{ gap: 12 }}>
          {problem && <Alert tone="warn">{t(PROBLEM_KEY[problem])}</Alert>}
          {error && <Alert tone="error">{error}</Alert>}
          {preview?.truncated && <Alert tone="warn">{t("v2.pipelines.logs.truncated")}</Alert>}
          {preview && reasons.length > 0 && (
            <div className="v2-tags" data-testid="v2-pipeline-logs-failed">
              {reasons.map(([reason, n]) => (
                <Tag key={reason} tone="orange">
                  {t(`v2.pipelines.logs.reason.${reason}`, { defaultValue: reason })} · {n}
                </Tag>
              ))}
            </div>
          )}
          {preview && preview.events > 0 && preview.sessions.length === 0 && <Alert tone="warn">{t("v2.pipelines.logs.nothingParsed")}</Alert>}
          {loading ? (
            <Spin />
          ) : preview && preview.events === 0 ? (
            <span className="v2-muted">{t("v2.pipelines.logs.noEvents")}</span>
          ) : preview ? (
            <div className="v2-grid-2">
              <div className="v2-stack" style={{ gap: 8 }}>
                <span className="v2-ilabel">{t("v2.pipelines.logs.raw")}</span>
                {preview.samples.map((sample, i) => (
                  <div key={i} className="v2-stack" style={{ gap: 4 }}>
                    <span className="v2-muted mono">
                      {fmtTime(sample.timestamp ? `${sample.timestamp.replace(" ", "T")}Z` : null)} · {sample.stream}
                    </span>
                    <pre className="v2-pre" style={{ maxHeight: 160 }}>
                      {sample.message}
                    </pre>
                  </div>
                ))}
              </div>
              <div className="v2-stack" style={{ gap: 8 }} data-testid="v2-pipeline-logs-sessions">
                <span className="v2-ilabel">{t("v2.pipelines.logs.converted")}</span>
                {preview.sessions.map((s) => (
                  <div key={s.session_id} className="v2-scenario" style={{ gap: 8 }}>
                    <div className="v2-scenario-head">
                      <span className="mono" title={s.session_id} style={{ flex: 1, minWidth: 0, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                        {s.session_id}
                      </span>
                      <span className="v2-muted" style={{ whiteSpace: "nowrap" }}>
                        {t("v2.pipelines.logs.records", { count: s.records })}
                      </span>
                    </div>
                    {s.turns.map((turn, ti) => (
                      <div key={ti} className="v2-row" style={{ alignItems: "flex-start", flexWrap: "nowrap" }}>
                        <Tag tone={turn.role.toLowerCase() === "user" ? "blue" : "green"}>
                          {turn.role.toLowerCase() === "user" ? t("v2.pipelines.logs.user") : t("v2.pipelines.logs.assistant")}
                        </Tag>
                        <span style={{ whiteSpace: "pre-wrap", wordBreak: "break-word" }}>{turn.text}</span>
                      </div>
                    ))}
                    {s.turns.length === 0 && <span className="v2-muted">{t("v2.pipelines.logs.noTurns")}</span>}
                  </div>
                ))}
              </div>
            </div>
          ) : null}
        </div>
      </Card>
    </>
  );
}
