import { ExternalLink } from "lucide-react";
import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";

import { api, type MemoryActor, type MemoryEvent, type MemorySessionRow } from "../../../lib/api";
import { fmtTime } from "../../format";
import { Alert, Button, Card, LinkButton, SearchInput, Spin, Tag } from "../../ui";
import { actorText, fmtBytes, shortId, type TokenPaged, useTokenPaged } from "./common";
import { LoadMore } from "./widgets";

/** The clamp shows ~3 lines; pretty-printed JSON can exceed that in far fewer
 *  than 240 characters, so line count is a second reason to offer expand. */
function needsExpand(text: string | null): boolean {
  if (!text) return false;
  return text.length > 240 || text.split("\n").length > 3;
}

/** One event's payload entries; long text is expandable, never silently cut. */
function EventCard({ event }: { event: MemoryEvent }) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  return (
    <div className="v2-memory-event" data-testid="v2-memory-event">
      <div className="v2-memory-event-head">
        <span>{fmtTime(event.at)}</span>
        <span className="mono v2-muted" title={event.event_id ?? ""}>
          {shortId(event.event_id, 8)}
        </span>
        {event.branch?.name && <Tag tone="outline">{event.branch.name}</Tag>}
      </div>
      {event.payload.length === 0 && <div className="v2-muted">{t("memoryPage.short.noPayload")}</div>}
      {event.payload.map((p, i) => (
        <div key={i} className="v2-memory-turn">
          {p.kind === "blob" ? (
            <div className="v2-row">
              <Tag tone="gray">{t("memoryPage.short.blob")}</Tag>
              <span className="mono v2-muted">{fmtBytes(p.blob_bytes)}</span>
            </div>
          ) : p.kind === "json" ? (
            <>
              {/* A JSON payload is not a conversational turn: label it as JSON,
                  never with a role. The text is the value serialized server-side,
                  so null / false / 0 / "" are rendered as themselves. */}
              <div className="v2-row">
                <Tag tone="outline">{t("memoryPage.short.json")}</Tag>
                <span className="v2-muted">{t("memoryPage.short.jsonHint")}</span>
              </div>
              <div className={open ? "v2-memory-text mono open" : "v2-memory-text mono"}>{p.text ?? ""}</div>
            </>
          ) : (
            <>
              <div className="v2-row">
                <Tag tone={p.role === "USER" ? "blue" : "green"}>{p.role ?? "—"}</Tag>
                {/* Harness turns arrive as message envelopes; non-text parts are
                    shown as tags so a tool-only turn is not a blank bubble. */}
                {p.parts
                  .filter((kind) => kind !== "text")
                  .map((kind) => (
                    <Tag key={kind} tone="outline">
                      {kind}
                    </Tag>
                  ))}
              </div>
              {p.text ? (
                <div className={open ? "v2-memory-text open" : "v2-memory-text"}>{p.text}</div>
              ) : (
                <div className="v2-muted">{t("memoryPage.short.noText")}</div>
              )}
            </>
          )}
        </div>
      ))}
      {event.payload.some((p) => needsExpand(p.text)) && (
        <LinkButton onClick={() => setOpen(!open)}>{open ? t("v2.common.collapse") : t("v2.common.expand")}</LinkButton>
      )}
    </div>
  );
}

function PaneState<T>({ list, empty }: { list: TokenPaged<T>; empty: string }) {
  const { t } = useTranslation();
  if (list.loading && list.items.length === 0) return <Spin />;
  if (list.error)
    return (
      <Alert tone="error" action={<LinkButton onClick={list.reload}>{t("v2.common.retry")}</LinkButton>}>
        {list.error}
      </Alert>
    );
  if (list.items.length === 0) return <div className="v2-memory-empty">{empty}</div>;
  return null;
}

interface Props {
  actors: TokenPaged<MemoryActor>;
  actorId: string | null;
  sessionId: string | null;
  onSelectActor: (actorId: string | null) => void;
  onSelectSession: (sessionId: string | null) => void;
}

/**
 * Short-term memory = immutable events keyed on (actorId, sessionId), so the
 * drill-down mirrors that hierarchy exactly: actor → session → event. The actor
 * is the platform's scoped `<agent>__<human>` id, decoded for display.
 */
export function ShortTermTab({ actors, actorId, sessionId, onSelectActor, onSelectSession }: Props) {
  const { t } = useTranslation();
  const [q, setQ] = useState("");

  const sessions = useTokenPaged<MemorySessionRow>(
    actorId ? (token) => api.memorySessions(actorId, token) : null,
    `sessions:${actorId ?? ""}`,
  );
  const events = useTokenPaged<MemoryEvent>(
    actorId && sessionId ? (token) => api.memoryEvents(actorId, sessionId, token) : null,
    `events:${actorId ?? ""}:${sessionId ?? ""}`,
  );

  const shownActors = useMemo(() => {
    const needle = q.trim().toLowerCase();
    if (!needle) return actors.items;
    return actors.items.filter((a) => `${a.actor_id} ${a.agent_name ?? ""} ${a.human_actor}`.toLowerCase().includes(needle));
  }, [actors.items, q]);

  const session = sessions.items.find((s) => s.session_id === sessionId) ?? null;
  const actor = actors.items.find((a) => a.actor_id === actorId) ?? null;

  return (
    <div className="v2-memory-browse">
      <Card
        title={t("memoryPage.short.actorsTitle")}
        end={
          <Button size="sm" onClick={actors.reload}>
            {t("v2.common.refresh")}
          </Button>
        }
        testId="v2-memory-actors"
      >
        <p className="v2-muted v2-memory-hint" style={{ margin: "0 0 10px" }}>
          {t("memoryPage.short.actorsSub")}
        </p>
        <SearchInput value={q} onChange={setQ} placeholder={t("v2.memory.searchActor")} />
        <div className="v2-memory-list">
          <PaneState list={actors} empty={t("memoryPage.short.noActors")} />
          {shownActors.map((a) => (
            <button
              key={a.actor_id}
              type="button"
              className={`v2-memory-item${a.actor_id === actorId ? " on" : ""}`}
              onClick={() => onSelectActor(a.actor_id)}
              title={a.actor_id}
            >
              <span className="v2-memory-item-main">
                {a.scoped ? (
                  <>
                    <b>{a.agent_name ?? t("memoryPage.short.deletedAgent")}</b>
                    <span className="v2-muted"> · {a.human_actor}</span>
                  </>
                ) : (
                  <>
                    <b>{a.human_actor}</b> <Tag tone="gray">{t("v2.memory.unscoped")}</Tag>
                  </>
                )}
              </span>
              <span className="mono v2-muted v2-memory-item-sub">{shortId(a.actor_id, 12)}</span>
            </button>
          ))}
        </div>
        <LoadMore token={actors.token} loading={actors.loading} onClick={actors.loadMore} testId="v2-memory-actors-more" />
      </Card>

      <Card
        title={t("memoryPage.short.sessionsTitle")}
        sub={actorId ? t("memoryPage.short.sessionsSub") : undefined}
        testId="v2-memory-sessions"
      >
        <div className="v2-memory-list">
          {!actorId ? (
            <div className="v2-memory-empty">{t("memoryPage.short.pickActor")}</div>
          ) : (
            <PaneState list={sessions} empty={t("memoryPage.short.noSessions")} />
          )}
          {sessions.items.map((s) => (
            <button
              key={s.session_id}
              type="button"
              className={`v2-memory-item${s.session_id === sessionId ? " on" : ""}`}
              onClick={() => onSelectSession(s.session_id)}
              title={s.session_id}
            >
              <span className="v2-memory-item-main mono">{shortId(s.session_id, 12)}</span>
              <span className="v2-muted v2-memory-item-sub">
                {fmtTime(s.created_at)}
                {/* only console-written sessions have a ledger row */}
                {" · "}
                {s.ledger ? t("memoryPage.short.messages", { count: s.ledger.message_count }) : t("memoryPage.short.externalSession")}
              </span>
            </button>
          ))}
        </div>
        <LoadMore token={sessions.token} loading={sessions.loading} onClick={sessions.loadMore} testId="v2-memory-sessions-more" />
      </Card>

      <Card
        title={t("memoryPage.short.eventsTitle")}
        sub={sessionId ? t("memoryPage.short.eventsSub") : undefined}
        end={
          session?.ledger ? (
            <Link
              className="v2-link v2-row"
              style={{ gap: 4 }}
              to={`/v2/chat?agent=${encodeURIComponent(session.ledger.agent_id)}&session=${encodeURIComponent(session.session_id)}`}
              data-testid="v2-memory-open-chat"
            >
              {t("v2.memory.openInChat")}
              <ExternalLink size={13} aria-hidden="true" />
            </Link>
          ) : undefined
        }
        testId="v2-memory-events"
      >
        {actorId && sessionId && (
          <div className="v2-memory-context mono v2-muted" title={`${actorId} / ${sessionId}`}>
            {actor ? actorText(t, actor) : shortId(actorId, 12)}
            {" / "}
            {shortId(sessionId, 12)}
          </div>
        )}
        <div className="v2-memory-list v2-memory-timeline">
          {!sessionId ? (
            <div className="v2-memory-empty">{t("memoryPage.short.pickSession")}</div>
          ) : (
            <PaneState list={events} empty={t("memoryPage.short.noEvents")} />
          )}
          {events.items.map((e, i) => (
            <EventCard key={e.event_id ?? `${e.at}-${i}`} event={e} />
          ))}
        </div>
        <LoadMore token={events.token} loading={events.loading} onClick={events.loadMore} testId="v2-memory-events-more" />
      </Card>
    </div>
  );
}
