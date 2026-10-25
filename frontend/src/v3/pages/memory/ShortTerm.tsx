import { MessagesSquare, RefreshCw, Search } from "lucide-react";
import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";

import { api, type MemoryActor, type MemoryEvent, type MemorySessionRow } from "../../../lib/api";
import { actorText, agentLabel, fmtBytes, shortId, type TokenPaged, useTokenPaged } from "../../../v2/pages/memory/common";
import { ago } from "../../format";
import { Btn, Chip, Panel } from "../../ui";
import { LoadMore, PaneState, SourceBadge } from "./common";

/** ~3 lines show; long or many-line text offers expand rather than being cut. */
const needsExpand = (text: string | null) => !!text && (text.length > 240 || text.split("\n").length > 3);

function EventCard({ event }: { event: MemoryEvent }) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  return (
    <div className="v3-mem-event">
      <div className="head">
        <span>{ago(event.at)}</span>
        <span className="mono" title={event.event_id ?? ""}>{shortId(event.event_id, 8)}</span>
        {event.branch?.name && <Chip>{event.branch.name}</Chip>}
      </div>
      {event.payload.length === 0 && <div className="v3-mem-muted">{t("v3.memory.noPayload")}</div>}
      {event.payload.map((p, i) => (
        <div key={i} className="turn">
          {p.kind === "blob" ? (
            <div className="row"><Chip>{t("v3.memory.blob")}</Chip><span className="mono v3-mem-muted">{fmtBytes(p.blob_bytes)}</span></div>
          ) : p.kind === "json" ? (
            <>
              {/* a JSON payload is not a conversational turn: never give it a role */}
              <div className="row"><Chip>JSON</Chip><span className="v3-mem-muted">{t("v3.memory.jsonHint")}</span></div>
              <div className={open ? "text mono open" : "text mono"}>{p.text ?? ""}</div>
            </>
          ) : (
            <>
              <div className="row">
                <Chip s={p.role === "USER" ? "info" : "ok"}>{p.role ?? "—"}</Chip>
                {p.parts.filter((k) => k !== "text").map((k) => <Chip key={k}>{k}</Chip>)}
              </div>
              {p.text ? <div className={open ? "text open" : "text"}>{p.text}</div> : <div className="v3-mem-muted">{t("v3.memory.noText")}</div>}
            </>
          )}
        </div>
      ))}
      {event.payload.some((p) => needsExpand(p.text)) && (
        <button type="button" className="v3-btn ghost sm" onClick={() => setOpen(!open)}>{open ? t("v3.memory.collapse") : t("v3.memory.expand")}</button>
      )}
    </div>
  );
}

/**
 * Short-term memory = immutable events keyed on (actorId, sessionId): actor →
 * session → event. The actor is the platform's scoped `<agent>__<human>` id,
 * decoded for display.
 */
export function ShortTerm({
  actors,
  actorId,
  sessionId,
  onSelectActor,
  onSelectSession,
}: {
  actors: TokenPaged<MemoryActor>;
  actorId: string | null;
  sessionId: string | null;
  onSelectActor: (id: string | null) => void;
  onSelectSession: (id: string | null) => void;
}) {
  const { t } = useTranslation();
  const [q, setQ] = useState("");
  const sessions = useTokenPaged<MemorySessionRow>(
    actorId ? (token) => api.memorySessions(actorId, token) : null,
    `v3-sessions:${actorId ?? ""}`,
  );
  const events = useTokenPaged<MemoryEvent>(
    actorId && sessionId ? (token) => api.memoryEvents(actorId, sessionId, token) : null,
    `v3-events:${actorId ?? ""}:${sessionId ?? ""}`,
  );
  const shown = useMemo(() => {
    const needle = q.trim().toLowerCase();
    return needle
      ? actors.items.filter((a) => `${a.actor_id} ${a.agent_name ?? ""} ${a.human_actor}`.toLowerCase().includes(needle))
      : actors.items;
  }, [actors.items, q]);
  const session = sessions.items.find((s) => s.session_id === sessionId) ?? null;
  const actor = actors.items.find((a) => a.actor_id === actorId) ?? null;

  return (
    <div className="v3-mem-browse">
      <Panel title={t("v3.memory.actorsTitle")} end={<Btn size="sm" kind="ghost" onClick={actors.reload}><RefreshCw size={13} /></Btn>}>
        <p className="v3-mem-muted" style={{ margin: "0 0 10px", fontSize: 12.5 }}>{t("v3.memory.actorsSub")}</p>
        <div style={{ position: "relative", marginBottom: 10 }}>
          <Search size={14} style={{ position: "absolute", left: 12, top: 11, color: "var(--v3-text-3)" }} aria-hidden="true" />
          <input className="v3-input" style={{ paddingLeft: 34 }} value={q} onChange={(e) => setQ(e.target.value)}
            placeholder={t("v3.memory.searchActor")} aria-label={t("v3.memory.searchActor")} />
        </div>
        <div className="v3-mem-list">
          <PaneState list={actors} empty={t("v3.memory.noActors")} />
          {shown.map((a) => (
            <button key={a.actor_id} type="button" className={a.actor_id === actorId ? "v3-mem-item on" : "v3-mem-item"}
              onClick={() => onSelectActor(a.actor_id)} title={a.actor_id}>
              <span>
                {a.scoped ? (
                  <><b>{agentLabel(t, a)}</b><span className="v3-mem-muted"> · {a.human_actor}</span></>
                ) : (
                  <><b>{a.human_actor}</b> <Chip>{t("v3.memory.unscoped")}</Chip></>
                )}
              </span>
              <small className="mono">{shortId(a.actor_id, 12)} <SourceBadge source={a.source} /></small>
            </button>
          ))}
        </div>
        <LoadMore list={actors} />
      </Panel>

      <Panel title={t("v3.memory.sessionsTitle")}>
        <div className="v3-mem-list">
          {!actorId ? <div className="v3-mem-empty">{t("v3.memory.pickActor")}</div> : <PaneState list={sessions} empty={t("v3.memory.noSessions")} />}
          {sessions.items.map((s) => (
            <button key={s.session_id} type="button" className={s.session_id === sessionId ? "v3-mem-item on" : "v3-mem-item"}
              onClick={() => onSelectSession(s.session_id)} title={s.session_id}>
              <span className="mono">{shortId(s.session_id, 12)} <SourceBadge source={s.source} /></span>
              {/* only console-written sessions have a ledger row */}
              <small>{ago(s.created_at)} · {s.ledger ? t("v3.memory.messages", { count: s.ledger.message_count }) : t("v3.memory.externalSession")}</small>
            </button>
          ))}
        </div>
        <LoadMore list={sessions} />
      </Panel>

      <Panel
        title={t("v3.memory.eventsTitle")}
        end={
          session?.ledger ? (
            <Link className="v3-btn sm ghost"
              to={`/v3/chat?agent=${encodeURIComponent(session.ledger.agent_id)}&session=${encodeURIComponent(session.session_id)}`}>
              <MessagesSquare size={13} /> {t("v3.memory.openInChat")}
            </Link>
          ) : undefined
        }
      >
        {actorId && sessionId && (
          <div className="mono v3-mem-muted" style={{ fontSize: 12, marginBottom: 10 }} title={`${actorId} / ${sessionId}`}>
            {actor ? actorText(t, actor) : shortId(actorId, 12)} / {shortId(sessionId, 12)}
          </div>
        )}
        <div className="v3-mem-list timeline">
          {!sessionId ? <div className="v3-mem-empty">{t("v3.memory.pickSession")}</div> : <PaneState list={events} empty={t("v3.memory.noEvents")} />}
          {events.items.map((e, i) => <EventCard key={e.event_id ?? `${e.at}-${i}`} event={e} />)}
        </div>
        <LoadMore list={events} />
      </Panel>
    </div>
  );
}
