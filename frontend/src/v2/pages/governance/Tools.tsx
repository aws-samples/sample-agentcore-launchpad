import { Monitor } from "lucide-react";
import { lazy, Suspense, useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import { api, type BrowserDemoResult, type GovernanceToolInfo } from "../../../lib/api";
import { governanceError } from "../../../lib/governance";
import { useLoad, usePaged, useV2Toast } from "../../hooks";
import {
  Alert,
  Button,
  Card,
  type Column,
  Descriptions,
  Drawer,
  Field,
  FilterSelect,
  LinkButton,
  Pager,
  SearchInput,
  Table,
  Tag,
} from "../../ui";
import { JsonBlock } from "./widgets";

const CODE_DEMO = "import math\nprint('sqrt(1764) =', math.isqrt(1764))";
const BROWSER_DEMO_URL = "https://example.com";
// The DCV live-view client is heavy; it loads only when a browser session is open
// (same lazy boundary as the classic tools view).
const BrowserLiveView = lazy(() =>
  import("bedrock-agentcore/browser/live-view").then((module) => ({ default: module.BrowserLiveView })),
);

/** Tool catalog of the Launchpad Gateway (targets + built-ins). */
function Catalog() {
  const { t } = useTranslation();
  const { data, loading, error, reload } = useLoad(
    () => api.governanceToolCatalog().catch((err: unknown) => Promise.reject(new Error(governanceError(err)))),
    "gov-tools",
  );
  const [source, setSource] = useState("");
  const [q, setQ] = useState("");
  const [selected, setSelected] = useState<GovernanceToolInfo | null>(null);
  const tools = useMemo(() => data?.tools ?? [], [data]);
  const rows = useMemo(() => {
    const needle = q.trim().toLowerCase();
    return tools.filter(
      (tool) =>
        (!source || tool.source === source) &&
        (!needle || `${tool.name} ${tool.description} ${tool.target ?? ""}`.toLowerCase().includes(needle)),
    );
  }, [tools, source, q]);
  const paged = usePaged(rows, 12);

  const columns: Column<GovernanceToolInfo>[] = [
    {
      key: "name",
      title: t("v2.governance.tools.colName"),
      render: (tool) => (
        <LinkButton onClick={() => setSelected(tool)} testId={`v2-governance-tool-${tool.name}`}>
          <span className="mono">{tool.name}</span>
        </LinkButton>
      ),
    },
    {
      key: "source",
      title: t("v2.governance.tools.colSource"),
      render: (tool) => (
        <Tag tone={tool.source === "gateway" ? "blue" : "gray"}>
          {tool.source === "gateway" ? "Gateway" : t("v2.governance.tools.builtin")}
          {tool.target ? ` · ${tool.target}` : ""}
        </Tag>
      ),
    },
    {
      key: "desc",
      title: t("v2.governance.description"),
      render: (tool) => (
        <span className="clip" title={tool.description}>
          {tool.description || "—"}
        </span>
      ),
    },
    { key: "auth", title: t("v2.governance.tools.colAuth"), render: (tool) => <span className="mono v2-muted">{tool.auth}</span> },
    {
      key: "ops",
      title: t("v2.common.actions"),
      className: "right",
      render: (tool) => (
        <div className="v2-actions">
          <LinkButton onClick={() => setSelected(tool)}>{t("v2.common.view")}</LinkButton>
        </div>
      ),
    },
  ];

  return (
    <Card
      title={t("v2.governance.tools.title")}
      sub={data?.gateway_url ? "launchpad-gw / MCP" : data ? t("governance.tools.offline") : undefined}
      testId="v2-governance-tools"
    >
      <div className="v2-toolbar">
        <Button onClick={reload}>{t("v2.common.refresh")}</Button>
        <FilterSelect
          label={t("v2.governance.tools.colSource")}
          value={source}
          allLabel={t("v2.common.all")}
          onChange={setSource}
          options={[
            { value: "gateway", label: "Gateway" },
            { value: "builtin", label: t("v2.governance.tools.builtin") },
          ]}
        />
        <div className="end">
          <SearchInput value={q} onChange={setQ} placeholder={t("v2.governance.tools.search")} />
          <span className="v2-count">{t("v2.common.total", { count: rows.length })}</span>
        </div>
      </div>
      <Table
        columns={columns}
        rows={paged.slice}
        rowKey={(tool) => tool.name}
        loading={loading}
        error={error}
        onRetry={reload}
        selectedKey={selected?.name}
        empty={t("v2.governance.tools.empty")}
      />
      <Pager page={paged.page} pages={paged.pages} total={paged.total} onPage={paged.setPage} />
      {selected && (
        <Drawer open title={<span className="mono">{selected.name}</span>} onClose={() => setSelected(null)} testId="v2-governance-tool-drawer">
          <Descriptions
            one
            items={[
              { label: t("v2.governance.tools.colSource"), value: selected.source },
              { label: t("v2.governance.tools.target"), value: selected.target ?? "—" },
              { label: t("v2.governance.tools.colAuth"), value: <span className="mono">{selected.auth}</span> },
            ]}
          />
          <div className="v2-sub-title">{t("v2.governance.description")}</div>
          <div style={{ whiteSpace: "pre-wrap" }}>{selected.description || "—"}</div>
          <div className="v2-sub-title">{t("v2.governance.tools.schema")}</div>
          <JsonBlock value={selected.inputSchema} />
        </Drawer>
      )}
    </Card>
  );
}

/** Built-in Code Interpreter: run a Python snippet in a fresh sandbox session. */
function CodeInterpreterDemo() {
  const { t } = useTranslation();
  const [code, setCode] = useState(CODE_DEMO);
  const [busy, setBusy] = useState(false);
  const [out, setOut] = useState<{ ok: boolean; text: string } | null>(null);

  const run = async () => {
    setBusy(true);
    setOut(null);
    try {
      const result = await api.runCodeInterpreterDemo(code);
      setOut({ ok: true, text: `${result.stdout}\n— session ${result.session_id} · ${result.latency_ms}ms` });
    } catch (error) {
      setOut({ ok: false, text: governanceError(error) });
    } finally {
      setBusy(false);
    }
  };

  return (
    <Card
      title={t("v2.governance.demo.ciTitle")}
      sub="aws.codeinterpreter.v1"
      end={
        <Button kind="primary" size="sm" disabled={busy || !code.trim()} onClick={() => void run()} testId="v2-governance-ci-run">
          {busy ? t("v2.governance.demo.running") : t("v2.governance.demo.run")}
        </Button>
      }
      testId="v2-governance-ci"
    >
      <Field label={t("v2.governance.demo.pythonCode")}>
        <textarea
          className="v2-textarea code"
          rows={8}
          value={code}
          maxLength={4000}
          disabled={busy}
          spellCheck={false}
          onChange={(e) => setCode(e.target.value)}
          data-testid="ci-code"
        />
      </Field>
      {out && (
        <pre className={out.ok ? "v2-pre v2-governance-out" : "v2-pre v2-governance-out err"} data-testid="ci-out" style={{ marginTop: 12 }}>
          {out.text}
        </pre>
      )}
    </Card>
  );
}

/** Built-in Browser: open a session on a URL with optional Web Bot Auth / profile, watch it live. */
function BrowserDemo() {
  const { t } = useTranslation();
  const toast = useV2Toast();
  const options = useLoad(
    () => api.browserDemoOptions().catch((err: unknown) => Promise.reject(new Error(governanceError(err)))),
    "gov-browser-options",
  );
  const [url, setUrl] = useState(BROWSER_DEMO_URL);
  const [webBotAuth, setWebBotAuth] = useState(false);
  const [browserIdentifier, setBrowserIdentifier] = useState("");
  const [profileIdentifier, setProfileIdentifier] = useState("");
  const [saveProfile, setSaveProfile] = useState(false);
  const [busy, setBusy] = useState(false);
  const [out, setOut] = useState("");
  const [session, setSession] = useState<BrowserDemoResult | null>(null);
  const sessionRef = useRef<string | null>(null);

  const signedBrowsers = useMemo(
    () => options.data?.browsers.filter((browser) => browser.status === "READY" && browser.web_bot_auth) ?? [],
    [options.data],
  );
  const readyProfiles = useMemo(
    () => options.data?.profiles.filter((profile) => profile.status === "READY") ?? [],
    [options.data],
  );
  // keep the picks valid when the options (re)load
  const effectiveBrowser = signedBrowsers.some((b) => b.identifier === browserIdentifier)
    ? browserIdentifier
    : (signedBrowsers[0]?.identifier ?? "");
  const effectiveProfile = readyProfiles.some((p) => p.identifier === profileIdentifier) ? profileIdentifier : "";

  // the live-view client rewrites document.title; keep the console's
  useEffect(() => {
    const expectedTitle = "AgentCore Launchpad";
    const titleNode = document.querySelector("title");
    if (!titleNode) return;
    const restoreTitle = () => {
      if (document.title !== expectedTitle) document.title = expectedTitle;
    };
    restoreTitle();
    const observer = new MutationObserver(restoreTitle);
    observer.observe(titleNode, { childList: true, characterData: true, subtree: true });
    return () => {
      observer.disconnect();
      restoreTitle();
    };
  }, []);

  // never leave a (billed) browser session running behind a closed page
  useEffect(
    () => () => {
      const sessionId = sessionRef.current;
      if (sessionId) void api.stopBrowserDemo(sessionId);
    },
    [],
  );

  const start = async () => {
    setBusy(true);
    setOut("");
    try {
      const previous = sessionRef.current;
      sessionRef.current = null;
      setSession(null);
      if (previous) await api.stopBrowserDemo(previous);
      const result = await api.runBrowserDemo({
        url: url.trim(),
        web_bot_auth: webBotAuth,
        browser_identifier: webBotAuth ? effectiveBrowser : null,
        profile_identifier: effectiveProfile || null,
        save_profile: Boolean(effectiveProfile) && saveProfile,
      });
      sessionRef.current = result.session_id;
      setSession(result);
      setOut(
        [
          `title: "${result.title}"`,
          `browser: ${result.browser_identifier}`,
          result.profile_identifier ? `profile: ${result.profile_identifier}` : null,
          `— session ${result.session_id} · ${result.latency_ms}ms`,
        ]
          .filter(Boolean)
          .join("\n"),
      );
    } catch (error) {
      setOut(governanceError(error));
    } finally {
      setBusy(false);
    }
  };

  const stop = async () => {
    const sessionId = sessionRef.current;
    if (!sessionId) return;
    setBusy(true);
    try {
      const result = await api.stopBrowserDemo(sessionId);
      sessionRef.current = null;
      setSession(null);
      setOut(result.profile_saved ? t("governance.demos.stoppedProfileSaved") : t("governance.demos.stopped"));
      if (result.profile_saved === false) toast("error", t("governance.demos.profileSaveFailed"));
    } catch (error) {
      toast("error", governanceError(error));
    } finally {
      setBusy(false);
    }
  };

  const locked = busy || session !== null;
  const startDisabled = busy || !url.trim() || (webBotAuth && !effectiveBrowser);

  return (
    <Card
      title={t("v2.governance.demo.brTitle")}
      sub="aws.browser.v1 / DCV"
      end={
        <span className="v2-row">
          <Tag tone={session ? "green" : "gray"} dot>
            {session ? t("v2.governance.demo.live") : t("v2.governance.demo.idle")}
          </Tag>
          {session ? (
            <Button size="sm" kind="danger" disabled={busy} onClick={() => void stop()} testId="v2-governance-br-stop">
              {t("v2.governance.demo.stop")}
            </Button>
          ) : (
            <Button size="sm" kind="primary" disabled={startDisabled} onClick={() => void start()} testId="v2-governance-br-start">
              {t("v2.governance.demo.start")}
            </Button>
          )}
        </span>
      }
      testId="v2-governance-browser"
    >
      <div className="v2-form cols-2">
        <Field label={t("v2.governance.demo.url")} full>
          <input
            className="v2-input mono"
            type="url"
            value={url}
            maxLength={2000}
            disabled={locked}
            spellCheck={false}
            onChange={(e) => setUrl(e.target.value)}
            data-testid="browser-url"
          />
        </Field>
        <Field label={t("v2.governance.demo.requestSigning")}>
          <label className={locked ? "v2-check disabled" : "v2-check"} style={{ height: 32 }}>
            <input type="checkbox" checked={webBotAuth} disabled={locked} onChange={(e) => setWebBotAuth(e.target.checked)} />
            {t("v2.governance.demo.webBotAuth")}
          </label>
        </Field>
        {webBotAuth ? (
          <Field label={t("v2.governance.demo.browserResource")}>
            <select
              className="v2-select mono"
              value={effectiveBrowser}
              disabled={locked || signedBrowsers.length === 0}
              onChange={(e) => setBrowserIdentifier(e.target.value)}
              data-testid="browser-resource"
            >
              {signedBrowsers.length === 0 && <option value="">{t("v2.governance.demo.noWebBotAuthBrowser")}</option>}
              {signedBrowsers.map((browser) => (
                <option key={browser.identifier} value={browser.identifier}>
                  {browser.name}
                </option>
              ))}
            </select>
          </Field>
        ) : (
          <div />
        )}
        <Field label={t("v2.governance.demo.profile")}>
          <select
            className="v2-select mono"
            value={effectiveProfile}
            disabled={locked}
            onChange={(e) => {
              setProfileIdentifier(e.target.value);
              if (!e.target.value) setSaveProfile(false);
            }}
            data-testid="browser-profile"
          >
            <option value="">{t("v2.governance.demo.noProfile")}</option>
            {readyProfiles.map((profile) => (
              <option key={profile.identifier} value={profile.identifier}>
                {profile.name}
              </option>
            ))}
          </select>
        </Field>
        <Field label={t("v2.governance.demo.profilePersistence")}>
          <label className={locked || !effectiveProfile ? "v2-check disabled" : "v2-check"} style={{ height: 32 }}>
            <input
              type="checkbox"
              checked={saveProfile}
              disabled={locked || !effectiveProfile}
              onChange={(e) => setSaveProfile(e.target.checked)}
            />
            {t("v2.governance.demo.saveProfile")}
          </label>
        </Field>
      </div>
      {options.error && (
        <div style={{ marginTop: 12 }}>
          <Alert tone="error">{options.error}</Alert>
        </div>
      )}
      <div className="v2-governance-readout" data-testid="br-out">
        <span className="mono">GET {url}</span>
        {out && <span className="mono ok">{out}</span>}
      </div>
      <div className="v2-governance-live" data-testid="browser-live-view">
        {session ? (
          <>
            <div className="v2-governance-live-empty">{t("v2.governance.demo.connecting")}</div>
            <div className="v2-governance-live-stream">
              <Suspense fallback={<div className="v2-governance-live-empty">{t("v2.governance.demo.connecting")}</div>}>
                <BrowserLiveView
                  signedUrl={session.live_view_url}
                  remoteWidth={session.viewport.width}
                  remoteHeight={session.viewport.height}
                />
              </Suspense>
            </div>
          </>
        ) : (
          <div className="v2-governance-live-empty">
            <Monitor size={28} aria-hidden="true" />
            <span>{t("v2.governance.demo.noSession")}</span>
          </div>
        )}
      </div>
    </Card>
  );
}

/** 工具 tab: the Gateway tool catalog and the two built-in tool demos. */
export function ToolsTab() {
  return (
    <>
      <Catalog />
      <div className="v2-governance-demos">
        <CodeInterpreterDemo />
        <BrowserDemo />
      </div>
    </>
  );
}
