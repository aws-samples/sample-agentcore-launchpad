import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { useSearchParams } from "react-router-dom";

import { Panel, useToast, ViewHead } from "../components";
import type { MemoryOverview } from "../lib/api";
import { api } from "../lib/api";
import { LongTermTab } from "./memory/LongTermTab";
import { OverviewTab } from "./memory/OverviewTab";
import { ResourcesTab } from "./memory/ResourcesTab";
import { ShortTermTab } from "./memory/ShortTermTab";

const VIEWS = ["overview", "short-term", "long-term", "resources"] as const;
type ViewKey = (typeof VIEWS)[number];

/**
 * AgentCore Memory console — read-only.
 *
 * Sub-surfaces are `?view=` params (project convention) and selection lives in
 * `?actor=`/`?session=`/`?strategy=` so any state is reload- and link-safe.
 *
 * `/overview` is fetched once here rather than per tab: every tab needs the
 * `configured` flag, and the long-term view needs the strategy list to build
 * namespaces, so switching tabs must not refetch it.
 */
export function Memory() {
  const { t } = useTranslation();
  const toast = useToast();
  const [params, setParams] = useSearchParams();

  const viewParam = params.get("view") as ViewKey | null;
  const view: ViewKey = viewParam && VIEWS.includes(viewParam) ? viewParam : "overview";
  const actor = params.get("actor");
  const session = params.get("session");
  const strategy = params.get("strategy");

  const [overview, setOverview] = useState<MemoryOverview | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const seq = useRef(0);

  const load = useCallback(() => {
    const id = ++seq.current;
    setLoading(true);
    setError(null);
    api
      .memoryOverview()
      .then((res) => {
        if (id !== seq.current) return; // guard against out-of-order responses
        setOverview(res);
        setLoading(false);
      })
      .catch((err: unknown) => {
        if (id !== seq.current) return;
        const msg = err instanceof Error ? err.message : String(err);
        setError(msg);
        setLoading(false);
        toast(t("memoryPage.loadFailed", { msg }), "crit");
      });
  }, [t, toast]);

  useEffect(load, [load]);

  const switchView = (next: ViewKey) => {
    setParams((prev) => {
      const p = new URLSearchParams(prev);
      p.set("view", next);
      return p;
    });
  };

  /** Selecting an actor must clear the session — a session belongs to one actor. */
  const selectActor = (actorId: string | null) => {
    setParams((prev) => {
      const p = new URLSearchParams(prev);
      if (actorId) p.set("actor", actorId);
      else p.delete("actor");
      p.delete("session");
      return p;
    });
  };

  const selectSession = (sessionId: string | null) => {
    setParams((prev) => {
      const p = new URLSearchParams(prev);
      if (sessionId) p.set("session", sessionId);
      else p.delete("session");
      return p;
    });
  };

  const selectStrategy = (strategyId: string | null) => {
    setParams((prev) => {
      const p = new URLSearchParams(prev);
      if (strategyId) p.set("strategy", strategyId);
      else p.delete("strategy");
      return p;
    });
  };

  const head = (
    <ViewHead
      kicker={t("memoryPage.kicker")}
      title={t("memoryPage.title")}
      meta={t("memoryPage.meta")}
    />
  );

  if (loading && !overview) {
    return (
      <section>
        {head}
        <Panel>
          <div className="empty">{t("common.loading")}</div>
        </Panel>
      </section>
    );
  }

  if (error && !overview) {
    return (
      <section>
        {head}
        <Panel title={t("memoryPage.errorTitle")}>
          <div className="empty">{error}</div>
          <button className="refresh" onClick={load}>
            ⟳ {t("memoryPage.refresh")}
          </button>
        </Panel>
      </section>
    );
  }

  // Before `make bootstrap` there is no memory resource to visualize at all —
  // say so once instead of letting all three tabs fail their own way.
  if (overview && !overview.configured) {
    return (
      <section>
        {head}
        <Panel title={t("memoryPage.notConfiguredTitle")}>
          <p className="sub">{t("memoryPage.notConfiguredBody")}</p>
          <code className="mono">make bootstrap</code>
        </Panel>
      </section>
    );
  }

  return (
    <section>
      {head}

      <div className="obs-bar">
        {VIEWS.map((key) => (
          <button
            key={key}
            className={`obs-tab${view === key ? " active" : ""}`}
            onClick={() => switchView(key)}
            data-testid={`memory-tab-${key}`}
          >
            {t(`memoryPage.tabs.${key}`)}
          </button>
        ))}
        <span className="spacer" />
        <span className="cachehint">{loading ? t("common.loading") : ""}</span>
        <button className="refresh" onClick={load}>
          ⟳ {t("memoryPage.refresh")}
        </button>
      </div>

      {overview && view === "overview" && <OverviewTab overview={overview} />}
      {overview && view === "short-term" && (
        <ShortTermTab
          actorId={actor}
          sessionId={session}
          onSelectActor={selectActor}
          onSelectSession={selectSession}
        />
      )}
      {overview && view === "long-term" && (
        <LongTermTab
          strategies={overview.strategies}
          actorId={actor}
          strategyId={strategy}
          onSelectActor={selectActor}
          onSelectStrategy={selectStrategy}
        />
      )}
      {overview && view === "resources" && <ResourcesTab />}
    </section>
  );
}
