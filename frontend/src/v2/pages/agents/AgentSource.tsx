import Editor from "@monaco-editor/react";
import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { useSearchParams } from "react-router-dom";

import { type AgentSourceFile, type AgentSourceInfo, ApiError, api, errorMessage } from "../../../lib/api";
import { formatBytes } from "../../../lib/knowledgeBases";
import { fmtTime } from "../../format";
import { useLoad, useV2Toast } from "../../hooks";
import { Alert, Button, Card, Descriptions, FlowHeader, Spin, Tag } from "../../ui";
import { languageFor } from "../coding/language";
import { copyText } from "../observability/common";
import { withView } from "./agentsListParams";

/* Read-only, so none of the IDE's editing options; `occurrencesHighlight: "off"` for
 * the reason documented in coding/EditorPane.tsx (an unhandled `Canceled` from the
 * CDN script when the editor unmounts mid-debounce). */
const VIEWER_OPTIONS = {
  readOnly: true,
  domReadOnly: true,
  minimap: { enabled: false },
  automaticLayout: true,
  fontSize: 13,
  scrollBeyondLastLine: false,
  occurrencesHighlight: "off" as const,
  renderValidationDecorations: "off" as const,
};

type SourceLoad = { view: AgentSourceInfo } | { unavailable: string; message: string };

const UNAVAILABLE_REASONS = new Set(["method", "no_resource", "deleted", "gone", "not_code", "unreadable", "no_bucket"]);

function unavailableReason(err: ApiError): string {
  const reason = (err.detail as { reason?: unknown } | null)?.reason;
  return typeof reason === "string" && UNAVAILABLE_REASONS.has(reason) ? reason : "other";
}

const WRAPPED_OPTIONS = { ...VIEWER_OPTIONS, wordWrap: "on" as const };

function CodeViewer({ path, content, wrap }: { path: string; content: string; wrap?: boolean }) {
  return (
    <div className="v2-source-editor" data-testid="v2-agent-source-editor">
      <Editor
        height="100%"
        theme="vs"
        language={languageFor(path)}
        value={content}
        loading={<Spin />}
        options={wrap ? WRAPPED_OPTIONS : VIEWER_OPTIONS}
      />
    </div>
  );
}

function CopyButton({ text }: { text: string }) {
  const { t } = useTranslation();
  const toast = useV2Toast();
  return (
    <Button
      onClick={() =>
        copyText(text).then(
          () => toast("success", t("v2.agents.source.copied")),
          () => toast("error", t("v2.agents.source.copyFailed")),
        )
      }
      testId="v2-agent-source-copy"
    >
      {t("v2.agents.source.copy")}
    </Button>
  );
}

/** The file pane: the selected file's content, or why it is not shown (with a load
 *  button for a file the response budget left out). */
function FilePane({
  agentId,
  file,
  onLoaded,
}: {
  agentId: string;
  file: AgentSourceFile;
  onLoaded: (file: AgentSourceFile) => void;
}) {
  const { t } = useTranslation();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const loadOne = async () => {
    setBusy(true);
    setError(null);
    try {
      const res = await api.agentSource(agentId, file.path);
      const one = res.files.find((f) => f.path === file.path);
      if (one) onLoaded(one);
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="v2-source-pane">
      <div className="v2-source-pane-head">
        <span className="mono">{file.path}</span>
        <span className="v2-muted">{formatBytes(file.size)}</span>
        {file.truncated && <Tag tone="orange">{t("v2.agents.source.truncated")}</Tag>}
        <span className="end">{file.content !== null && <CopyButton text={file.content} />}</span>
      </div>
      {error && <Alert tone="error">{error}</Alert>}
      {file.content !== null ? (
        <CodeViewer path={file.path} content={file.content} />
      ) : file.omitted === "binary" ? (
        <Alert>{t("v2.agents.source.binary")}</Alert>
      ) : (
        <Alert
          action={
            <Button onClick={() => void loadOne()} disabled={busy} testId="v2-agent-source-load">
              {busy ? t("v2.common.loading") : t("v2.agents.source.loadFile")}
            </Button>
          }
        >
          {t("v2.agents.source.budget")}
        </Alert>
      )}
    </div>
  );
}

function CodeView({ view }: { view: AgentSourceInfo }) {
  const { t } = useTranslation();
  const [selected, setSelected] = useState(view.files[0]?.path ?? "");
  // files read one by one with `?path=` (left out of the first response by its budget)
  const [loaded, setLoaded] = useState<Record<string, AgentSourceFile>>({});
  const listed = view.files.find((f) => f.path === selected) ?? view.files[0];
  const file = listed ? (loaded[listed.path] ?? listed) : undefined;

  const meta = [
    { label: t("v2.agents.source.origin"), value: t(`v2.agents.source.origins.${view.origin}`) },
    ...(view.entrypoint ? [{ label: t("v2.agents.source.entrypoint"), value: <span className="mono">{view.entrypoint}</span> }] : []),
    ...(view.runtime_version
      ? [{ label: t("v2.agents.source.runtimeVersion"), value: <span className="mono">{view.runtime_version}</span> }]
      : []),
    ...(view.package_updated_at ? [{ label: t("v2.agents.source.packagedAt"), value: fmtTime(view.package_updated_at) }] : []),
    ...(view.origin === "upload"
      ? [
          { label: t("v2.agents.source.uploadFile"), value: <span className="mono">{view.original_filename ?? "—"}</span> },
          {
            label: t("v2.agents.source.uploadedBy"),
            value: `${view.uploaded_by ?? "—"} · ${fmtTime(view.uploaded_at ?? null)}`,
          },
          { label: "SHA-256", value: <span className="mono">{view.sha256 ? `${view.sha256.slice(0, 16)}…` : "—"}</span> },
        ]
      : []),
    {
      label: t("v2.agents.source.fileCount"),
      value:
        view.files_total > view.files.length
          ? t("v2.agents.source.filesCapped", { shown: view.files.length, total: view.files_total })
          : String(view.files_total),
    },
  ];

  return (
    <>
      <Card title={t("v2.agents.source.metaTitle")} sub={t(`v2.agents.source.originSub.${view.origin}`)}>
        <Descriptions items={meta} />
      </Card>
      {view.files.length === 0 ? (
        <Alert>{t("v2.agents.source.noFiles")}</Alert>
      ) : (
        <Card title={t("v2.agents.source.filesTitle")} flush>
          <div className="v2-source-split">
            <ul className="v2-source-files" data-testid="v2-agent-source-files">
              {view.files.map((f) => (
                <li key={f.path}>
                  <button
                    type="button"
                    className={f.path === file?.path ? "active" : undefined}
                    onClick={() => setSelected(f.path)}
                    title={f.path}
                  >
                    <span className="mono">{f.path}</span>
                    {f.omitted === "binary" && <span className="v2-muted">{t("v2.agents.source.binaryTag")}</span>}
                  </button>
                </li>
              ))}
            </ul>
            {file && (
              <FilePane
                key={file.path}
                agentId={view.agent_id}
                file={file}
                onLoaded={(one) => setLoaded((prev) => ({ ...prev, [one.path]: one }))}
              />
            )}
          </div>
        </Card>
      )}
    </>
  );
}

function HarnessView({ view }: { view: AgentSourceInfo }) {
  const { t } = useTranslation();
  const json = useMemo(() => JSON.stringify(view.config ?? {}, null, 2), [view.config]);
  return (
    <>
      <Alert>{t("v2.agents.source.harnessNote")}</Alert>
      <Card
        title={t("v2.agents.source.harnessTitle")}
        sub={t("v2.agents.source.harnessSub", { version: view.harness_version ?? "—" })}
        end={<CopyButton text={json} />}
      >
        {/* a system prompt is one long JSON string — wrap it */}
        <CodeViewer path="harness.json" content={json} wrap />
      </Card>
    </>
  );
}

function ImageView({ view }: { view: AgentSourceInfo }) {
  const { t } = useTranslation();
  return (
    <>
      <Alert>{t("v2.agents.source.imageNote")}</Alert>
      <Card title={t("v2.agents.source.imageTitle")}>
        <Descriptions
          one
          items={[
            { label: t("v2.agents.source.imageUri"), value: <span className="mono">{view.image_uri ?? "—"}</span> },
            { label: t("v2.agents.source.imageDigest"), value: <span className="mono">{view.image_digest ?? "—"}</span> },
          ]}
        />
      </Card>
    </>
  );
}

/**
 * 代码 — read-only: the code AWS holds for this agent (the package its Runtime runs,
 * 方式A's build context, or the member's BYOC upload), a Harness's configuration
 * (a managed loop has no code), or the image a BYOC agent was deployed from.
 * Read back on open (`GET /api/agents/{id}/source`).
 */
export function AgentSource({ id }: { id: string }) {
  const { t } = useTranslation();
  const [, setParams] = useSearchParams();
  const { data, loading, error, reload } = useLoad<SourceLoad>(
    () =>
      api.agentSource(id).then(
        (view) => ({ view }),
        (err: unknown) => {
          if (err instanceof ApiError && err.code === "agent.source_unavailable") {
            return { unavailable: unavailableReason(err), message: err.message };
          }
          throw err;
        },
      ),
    `source:${id}`,
  );
  const view = data && "view" in data ? data.view : null;
  const stale = view && view.deployment_status && view.deployment_status !== "succeeded";

  return (
    <>
      <FlowHeader
        title={t("v2.agents.source.title", { name: view?.name ?? id })}
        onBack={() => setParams((prev) => withView(prev, "detail", id))}
        end={<Button onClick={reload}>{t("v2.common.refresh")}</Button>}
      />
      {error && <Alert tone="error">{error}</Alert>}
      {loading && !data && <Spin />}
      {data && "unavailable" in data && (
        <Alert>{t(`v2.agents.source.unavailable.${data.unavailable}`, { defaultValue: data.message })}</Alert>
      )}
      {view && (
        <div className="v2-form" data-testid="v2-agent-source-page">
          {stale && (
            <Alert tone="warn">
              {t("v2.agents.source.stale", { status: t(`v2.agents.stage.${view.deployment_status}`, { defaultValue: view.deployment_status }) })}
            </Alert>
          )}
          {view.kind === "harness_config" ? (
            <HarnessView view={view} />
          ) : view.kind === "image" ? (
            <ImageView view={view} />
          ) : (
            <CodeView key={view.files.map((f) => f.path).join("\n")} view={view} />
          )}
        </div>
      )}
    </>
  );
}
