import { useState } from "react";
import { useTranslation } from "react-i18next";

import { Chip, Panel } from "../../components";
import type { MemoryActor, MemoryEvent, MemorySessionRow } from "../../lib/api";
import { api } from "../../lib/api";
import { bytes, shortId, stamp } from "./format";
import { LoadMore } from "./LoadMore";
import { usePaged } from "./paged";

function ActorLabel({ actor }: { actor: MemoryActor }) {
  const { t } = useTranslation();
  if (!actor.scoped) {
    return (
      <>
        <b>{actor.human_actor}</b> <Chip tone="muted">{t("memoryPage.short.unscoped")}</Chip>
      </>
    );
  }
  return (
    <>
      <b>{actor.agent_name ?? t("memoryPage.short.deletedAgent")}</b>
      <span className="dim"> · {actor.human_actor}</span>
    </>
  );
}

/** One event's payload entries; long text is expandable, never silently cut. */
function EventCard({ event }: { event: MemoryEvent }) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  return (
    <div className="mem-event">
      <div className="mem-event-head">
        <span className="mono">{stamp(event.at)}</span>
        <span className="mono dim" title={event.event_id ?? ""}>
          {shortId(event.event_id, 8)}
        </span>
        {event.branch?.name && <Chip tone="aqua">{event.branch.name}</Chip>}
      </div>
      {event.payload.length === 0 && (
        <div className="dim mono">{t("memoryPage.short.noPayload")}</div>
      )}
      {event.payload.map((p, i) => (
        <div key={i} className="mem-turn">
          {p.kind === "blob" ? (
            <>
              <Chip tone="muted">{t("memoryPage.short.blob")}</Chip>
              <span className="mono dim">{bytes(p.blob_bytes)}</span>
            </>
          ) : (
            <>
              <Chip tone={p.role === "USER" ? "blue" : "amber"}>{p.role ?? "—"}</Chip>
              {/* Harness turns arrive as message envelopes; non-text parts are
                  shown as chips so a tool-only turn is not a blank bubble. */}
              {p.parts
                .filter((kind) => kind !== "text")
                .map((kind) => (
                  <Chip key={kind} tone="aqua">
                    {kind}
                  </Chip>
                ))}
              {p.text ? (
                <span className={open ? "mem-text open" : "mem-text"}>{p.text}</span>
              ) : (
                <span className="mono dim">{t("memoryPage.short.noText")}</span>
              )}
            </>
          )}
        </div>
      ))}
      {event.payload.some((p) => (p.text?.length ?? 0) > 240) && (
        <button className="rowact" onClick={() => setOpen(!open)}>
          {open ? t("memoryPage.short.collapse") : t("memoryPage.short.expand")}
        </button>
      )}
    </div>
  );
}

interface Props {
  actorId: string | null;
  sessionId: string | null;
  onSelectActor: (actorId: string | null) => void;
  onSelectSession: (sessionId: string | null) => void;
}

/**
 * Short-term memory = immutable events keyed on (actorId, sessionId), so the
 * drill-down mirrors that hierarchy exactly: actor → session → event.
 */
export function ShortTermTab({ actorId, sessionId, onSelectActor, onSelectSession }: Props) {
  const { t } = useTranslation();

  const actors = usePaged<MemoryActor>((token) => api.memoryActors(token), []);
  const sessions = usePaged<MemorySessionRow>(
    actorId ? (token) => api.memorySessions(actorId, token) : null,
    [actorId],
  );
  const events = usePaged<MemoryEvent>(
    actorId && sessionId ? (token) => api.memoryEvents(actorId, sessionId, token) : null,
    [actorId, sessionId],
  );

  return (
    <div className="mem-grid-3">
      <Panel
        title={t("memoryPage.short.actorsTitle")}
        sub={t("memoryPage.short.actorsSub")}
        pad={false}
      >
        <div className="mem-list">
          {actors.items.length === 0 && !actors.loading && (
            <div className="empty">{t("memoryPage.short.noActors")}</div>
          )}
          {actors.items.map((a) => (
            <button
              key={a.actor_id}
              className={`histrow${a.actor_id === actorId ? " on" : ""}`}
              onClick={() => onSelectActor(a.actor_id)}
              title={a.actor_id}
            >
              <span>
                <ActorLabel actor={a} />
              </span>
              <span className="mono dim">{shortId(a.actor_id, 10)}</span>
            </button>
          ))}
        </div>
        <LoadMore token={actors.token} onClick={actors.loadMore} />
      </Panel>

      <Panel
        title={t("memoryPage.short.sessionsTitle")}
        sub={actorId ? t("memoryPage.short.sessionsSub") : t("memoryPage.short.pickActor")}
        pad={false}
      >
        <div className="mem-list">
          {!actorId && <div className="empty">{t("memoryPage.short.pickActor")}</div>}
          {actorId && sessions.items.length === 0 && !sessions.loading && (
            <div className="empty">{t("memoryPage.short.noSessions")}</div>
          )}
          {sessions.items.map((s) => (
            <button
              key={s.session_id}
              className={`histrow${s.session_id === sessionId ? " on" : ""}`}
              onClick={() => onSelectSession(s.session_id)}
              title={s.session_id}
            >
              <span className="mono">{shortId(s.session_id, 10)}</span>
              <span className="dim mono">
                {stamp(s.created_at)}
                {/* only console-written sessions have a ledger row */}
                {s.ledger
                  ? ` · ${t("memoryPage.short.messages", { count: s.ledger.message_count })}`
                  : ` · ${t("memoryPage.short.externalSession")}`}
              </span>
            </button>
          ))}
        </div>
        <LoadMore token={sessions.token} onClick={sessions.loadMore} />
      </Panel>

      <Panel
        title={t("memoryPage.short.eventsTitle")}
        sub={sessionId ? t("memoryPage.short.eventsSub") : t("memoryPage.short.pickSession")}
        pad={false}
      >
        <div className="mem-list mem-timeline">
          {!sessionId && <div className="empty">{t("memoryPage.short.pickSession")}</div>}
          {sessionId && events.items.length === 0 && !events.loading && (
            <div className="empty">{t("memoryPage.short.noEvents")}</div>
          )}
          {events.items.map((e) => (
            <EventCard key={e.event_id ?? e.at} event={e} />
          ))}
        </div>
        <LoadMore token={events.token} onClick={events.loadMore} />
      </Panel>
    </div>
  );
}
