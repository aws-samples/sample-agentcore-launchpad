import { ArrowRight, Bot, MessagesSquare, Scale, Search } from "lucide-react";
import { type KeyboardEvent, type ReactNode, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { useNavigate } from "react-router-dom";

import type { AgentInfo } from "../lib/api";
import { score } from "./score";
import { agentSignal } from "./signals";
import { Lamp } from "./ui";

export interface Command {
  id: string;
  group: string;
  label: string;
  hint?: string;
  /** extra words it answers to, e.g. the English name under a Chinese UI */
  keywords?: string;
  icon?: ReactNode;
  run: () => void;
}

/**
 * ⌘K — one keystroke to any agent, page or action. The console's primary way
 * through: everything reachable from the rail is also here, plus an entry per
 * agent ("open", "chat with", "release gate of"). `gate={false}` drops the per-agent
 * gate entries, for when Menu settings hides 放行门.
 */
export function CommandPalette({
  open,
  onClose,
  agents,
  pages,
  gate = true,
}: {
  open: boolean;
  onClose: () => void;
  agents: AgentInfo[];
  pages: Command[];
  gate?: boolean;
}) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const [query, setQuery] = useState("");
  const [cursor, setCursor] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);

  // Focus in the same commit that shows the input: a deferred focus dropped the
  // first keys of anyone typing straight after ⌘K.
  useLayoutEffect(() => {
    if (open) {
      setQuery("");
      setCursor(0);
      inputRef.current?.focus();
    }
  }, [open]);

  const commands = useMemo<Command[]>(() => {
    const go = (to: string) => () => {
      navigate(to);
      onClose();
    };
    const perAgent = agents.flatMap((agent) => {
      const name = agent.display_name || agent.name;
      const lamp = <Lamp s={agentSignal(agent)} live={agent.status === "active"} />;
      return [
        { id: `a:${agent.id}`, group: t("v3.cmdk.agents"), label: name, hint: agent.name, icon: lamp,
          run: go(`/v3/agents?id=${agent.id}`) },
        { id: `c:${agent.id}`, group: t("v3.cmdk.actions"), label: t("v3.cmdk.chatWith", { name }),
          icon: <MessagesSquare size={15} />, run: go(`/v3/chat?agent=${agent.id}`) },
        ...(gate ? [{ id: `g:${agent.id}`, group: t("v3.cmdk.actions"), label: t("v3.cmdk.gateOf", { name }),
          icon: <Scale size={15} />, run: go(`/v3/gate?agent=${agent.id}`) }] : []),
      ];
    });
    return [...pages.map((p) => ({ ...p, run: () => { p.run(); onClose(); } })), ...perAgent];
  }, [agents, pages, navigate, onClose, t, gate]);

  const results = useMemo(() => {
    const ranked = commands
      .map((c) => ({
        c,
        s: Math.max(score(query, c.label), score(query, c.hint ?? "") * 0.8, score(query, c.keywords ?? "") * 0.7),
      }))
      .filter((r) => r.s > 0);
    if (query.trim()) ranked.sort((a, b) => b.s - a.s);
    return ranked.slice(0, 40).map((r) => r.c);
  }, [commands, query]);

  useEffect(() => setCursor(0), [query]);

  if (!open) return null;

  const onKey = (e: KeyboardEvent<HTMLDivElement>) => {
    if (e.key === "Escape") onClose();
    else if (e.key === "ArrowDown") {
      e.preventDefault();
      setCursor((c) => Math.min(results.length - 1, c + 1));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setCursor((c) => Math.max(0, c - 1));
    } else if (e.key === "Enter") {
      e.preventDefault();
      results[cursor]?.run();
    }
  };

  let lastGroup = "";
  return (
    <div className="v3-cmdk-mask" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className="v3-cmdk" role="dialog" aria-modal="true" aria-label={t("v3.cmdk.label")} onKeyDown={onKey}>
        <div style={{ position: "relative" }}>
          <Search size={16} style={{ position: "absolute", left: 20, top: 20, color: "var(--v3-text-3)" }} aria-hidden="true" />
          <input
            ref={inputRef}
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder={t("v3.cmdk.placeholder")}
            style={{ paddingLeft: 46 }}
            aria-label={t("v3.cmdk.placeholder")}
            role="combobox"
            aria-expanded="true"
            aria-controls="v3-cmdk-list"
          />
        </div>
        <div className="v3-cmdk-list" id="v3-cmdk-list" role="listbox">
          {results.length === 0 && <div className="v3-cmdk-group">{t("v3.cmdk.none")}</div>}
          {results.map((c, i) => {
            const header = c.group !== lastGroup ? c.group : null;
            lastGroup = c.group;
            return (
              <div key={c.id}>
                {header && <div className="v3-cmdk-group">{header}</div>}
                <button
                  type="button"
                  className="v3-cmdk-item"
                  role="option"
                  aria-selected={i === cursor}
                  onMouseEnter={() => setCursor(i)}
                  onClick={c.run}
                >
                  {c.icon ?? <Bot size={15} />}
                  <span>{c.label}</span>
                  {c.hint && c.hint !== c.label && <span className="hint">{c.hint}</span>}
                  <span className="go">
                    <ArrowRight size={13} />
                  </span>
                </button>
              </div>
            );
          })}
        </div>
        <div className="v3-cmdk-foot">
          <span><kbd>↑↓</kbd> {t("v3.cmdk.move")}</span>
          <span><kbd>↵</kbd> {t("v3.cmdk.open")}</span>
          <span><kbd>esc</kbd> {t("v3.cmdk.close")}</span>
        </div>
      </div>
    </div>
  );
}
