import { useTranslation } from "react-i18next";
import { useSearchParams } from "react-router-dom";

import { api, type MemoryActor } from "../../lib/api";
import { useLoad } from "../hooks";
import { Alert, Button, Card, LinkButton, PageHeader, Spin, SubTabs } from "../ui";
import "./memory/memory.css";
import { useTokenPaged } from "./memory/common";
import { LongTermTab } from "./memory/LongTermTab";
import { OverviewTab } from "./memory/OverviewTab";
import { ResourceDetail, ResourceEditor, ResourceList } from "./memory/Resources";
import { ShortTermTab } from "./memory/ShortTermTab";

type MemoryTab = "overview" | "short-term" | "long-term" | "resources";
const MEMORY_TABS: MemoryTab[] = ["overview", "short-term", "long-term", "resources"];

/**
 * 记忆 — AgentCore Memory: the platform memory resource and its strategies,
 * short-term events (actor → session → event), long-term records (listing +
 * semantic retrieval) and memory-resource management. Tabs are `?tab=`, the
 * selection lives in `?actor=`/`?session=`/`?strategy=` so any state is
 * reload- and link-safe, and resource sub-pages are `?view=`
 * (resource / resource-new / resource-edit).
 */
export function V2Memory() {
  const { t } = useTranslation();
  const [params, setParams] = useSearchParams();
  const view = params.get("view");
  const id = params.get("id");
  const tabParam = params.get("tab") as MemoryTab | null;
  const tab: MemoryTab = tabParam && MEMORY_TABS.includes(tabParam) ? tabParam : "overview";
  const actor = params.get("actor");
  const session = params.get("session");
  const strategy = params.get("strategy");

  // fetched once for every tab: all need `configured`, long-term needs the
  // strategy names, so switching tabs must not refetch it
  const overview = useLoad(() => api.memoryOverview(), "memory-overview");
  // actors are shared by short- and long-term, loaded only when one is shown
  const needsActors = !view && (tab === "short-term" || tab === "long-term");
  const actors = useTokenPaged<MemoryActor>(needsActors ? (token) => api.memoryActors(token) : null, `actors:${needsActors}`);

  if (view === "resource" && id) return <ResourceDetail key={id} id={id} />;
  if (view === "resource-new") return <ResourceEditor id={null} />;
  if (view === "resource-edit" && id) return <ResourceEditor key={id} id={id} />;

  const update = (patch: Record<string, string | null>) =>
    setParams((prev) => {
      const next = new URLSearchParams(prev);
      for (const [k, v] of Object.entries(patch)) {
        if (v) next.set(k, v);
        else next.delete(k);
      }
      return next;
    });

  const header = (
    <PageHeader
      title={t("nav.memory")}
      desc={t("v2.memory.desc")}
      tabs={
        <SubTabs
          value={tab}
          // actor / session / strategy survive a tab switch (short- and long-term share the actor)
          onChange={(next) => update({ tab: next })}
          tabs={MEMORY_TABS.map((value) => ({ value, label: t(`v2.memory.tab.${value}`) }))}
        />
      }
    />
  );

  const data = overview.data;
  if (!data) {
    return (
      <>
        {header}
        {overview.error ? (
          <Alert tone="error" action={<LinkButton onClick={overview.reload}>{t("v2.common.retry")}</LinkButton>}>
            {t("memoryPage.loadFailed", { msg: overview.error })}
          </Alert>
        ) : (
          <Card>
            <Spin />
          </Card>
        )}
      </>
    );
  }

  // Before `make bootstrap` there is no platform memory to browse — say so once
  // instead of letting every tab fail its own way. Resource management still works.
  if (!data.configured && tab !== "resources") {
    return (
      <>
        {header}
        <Card title={t("memoryPage.notConfiguredTitle")}>
          <p className="v2-muted" style={{ margin: "0 0 12px" }}>{t("memoryPage.notConfiguredBody")}</p>
          <pre className="v2-pre">make bootstrap</pre>
          <div style={{ marginTop: 12 }}>
            <Button onClick={overview.reload}>{t("v2.common.refresh")}</Button>
          </div>
        </Card>
      </>
    );
  }

  return (
    <>
      {header}
      {tab === "overview" && <OverviewTab overview={data} onReload={overview.reload} />}
      {tab === "short-term" && (
        <ShortTermTab
          actors={actors}
          actorId={actor}
          sessionId={session}
          // a session belongs to one actor: picking an actor clears it
          onSelectActor={(next) => update({ actor: next, session: null })}
          onSelectSession={(next) => update({ session: next })}
        />
      )}
      {tab === "long-term" && (
        <LongTermTab
          actors={actors}
          strategies={data.strategies}
          actorId={actor}
          strategyId={strategy}
          onSelectActor={(next) => update({ actor: next, session: null })}
          onSelectStrategy={(next) => update({ strategy: next })}
        />
      )}
      {tab === "resources" && <ResourceList />}
    </>
  );
}
