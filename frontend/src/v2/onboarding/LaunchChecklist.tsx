// The launch checklist on V2 Home (ported from console-v3's launch sequence):
// a lamp per step, the next one lit, done ones quiet. Every tick is read from
// what exists in the workspace. Hidden per workspace once dismissed (the help
// menu brings it back); it folds into one line once every step is done.
import { ArrowRight, Check, ChevronDown, ChevronUp, X } from "lucide-react";
import { useEffect, useId, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";

import { useAuth } from "../../auth/auth-context";
import { api, type AgentInfo } from "../../lib/api";
import { useWorkspace } from "../../workspace/workspace-context";
import { useLoad } from "../hooks";
import type { Signal } from "../signals";
import type { V2Task } from "../tasks";
import { Lamp } from "../ui";
import "./onboarding.css";
import { takeLaunchScroll, useLaunchHidden, useLaunchRequest } from "./state";
import { chatProbeIds, launchSteps, nextStep } from "./steps";

/** The probed agents (comma-joined ids) that have at least one chat session. */
async function loadChatted(probe: string): Promise<string[]> {
  if (!probe) return [];
  const ids = probe.split(",");
  // one agent's failure only leaves its own answer as "not yet"
  const found = await Promise.all(
    ids.map((id) =>
      api
        .listChatSessions(id)
        .then((r) => (r.sessions.length > 0 ? id : null))
        .catch(() => null),
    ),
  );
  return found.filter((id): id is string => id !== null);
}

export function LaunchChecklist({ agents, tasks }: { agents: AgentInfo[]; tasks: V2Task[] | null }) {
  const { t } = useTranslation();
  const { isAdmin } = useAuth();
  const { current: workspace } = useWorkspace();
  const ws = workspace?.id ?? "";
  const [hidden, setHidden] = useLaunchHidden(ws);
  const [expanded, setExpanded] = useState<boolean | null>(null);
  const stepsId = useId();
  const rootRef = useRef<HTMLElement>(null);
  const request = useLaunchRequest();

  // A stable string of the first few live agents: the 10 s agents poll returns a
  // new list each time, but the key — and so the request — only changes with the set.
  // Nothing is asked while the checklist is hidden or no agent is live yet.
  const probe = hidden ? "" : chatProbeIds(agents).join(",");
  const chatted = useLoad(() => loadChatted(probe), `v2-launch-chat:${ws}:${probe}`);

  const ready = Boolean(workspace) && !hidden && tasks !== null && chatted.data !== null;

  // the help menu's "Launch checklist" scrolls here, also right after navigating Home
  useEffect(() => {
    if (!ready || !rootRef.current || !takeLaunchScroll()) return;
    const reduced = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
    rootRef.current.scrollIntoView({ block: "center", behavior: reduced ? "auto" : "smooth" });
  }, [ready, request]);

  if (!ready) return null;

  const steps = launchSteps({ workspace, agents, chattedAgentIds: chatted.data ?? [], tasks, isAdmin });
  const done = steps.filter((s) => s.done).length;
  const next = nextStep(steps);
  const showSteps = expanded ?? next !== null;
  const title = next ? t("v2.onboard.launch.title") : t("v2.onboard.launch.launched");

  return (
    <section
      ref={rootRef}
      className="v2-card v2-launch"
      data-state={next ? "open" : "done"}
      data-testid="v2-launch"
      data-tour="launch"
      aria-label={t("v2.onboard.launch.title")}
    >
      <div className="v2-launch-head">
        <button
          type="button"
          className="v2-launch-toggle"
          aria-expanded={showSteps}
          aria-controls={stepsId}
          title={showSteps ? t("v2.onboard.launch.collapse") : t("v2.onboard.launch.expand")}
          onClick={() => setExpanded(!showSteps)}
          data-testid="v2-launch-toggle"
        >
          {next ? null : <Check size={15} className="v2-launch-tick" aria-hidden="true" />}
          <span>{title}</span>
          {showSteps ? <ChevronUp size={15} aria-hidden="true" /> : <ChevronDown size={15} aria-hidden="true" />}
        </button>
        <span className="v2-launch-count">{t("v2.onboard.launch.progress", { done, total: steps.length })}</span>
        <span className="v2-launch-meter" aria-hidden="true">
          {steps.map((s) => (
            <i key={s.key} data-on={s.done ? "true" : undefined} />
          ))}
        </span>
        {!next && <span className="v2-launch-sum">{t("v2.onboard.launch.allDone")}</span>}
        <button
          type="button"
          className="v2-launch-hide"
          aria-label={t("v2.onboard.launch.hideHint")}
          title={t("v2.onboard.launch.hideHint")}
          onClick={() => setHidden(true)}
          data-testid="v2-launch-hide"
        >
          <X size={15} aria-hidden="true" />
        </button>
      </div>

      {!showSteps && next && (
        <div className="v2-launch-next">
          <Lamp signal="wait" />
          <span className="label">{t("v2.onboard.launch.next")}</span>
          <b>{t(`v2.onboard.launch.${next.key}`)}</b>
          {next.href ? (
            <Link to={next.href} className="v2-btn sm primary">
              {t(`v2.onboard.launch.${next.key}Cta`)}
              <ArrowRight size={13} aria-hidden="true" />
            </Link>
          ) : (
            <span className="v2-muted">{t(next.hintKey)}</span>
          )}
        </div>
      )}

      <ol id={stepsId} hidden={!showSteps}>
        {steps.map((s, i) => {
          const isNext = s.key === next?.key;
          const signal: Signal = s.done ? "ok" : isNext ? "wait" : "off";
          return (
            <li key={s.key} data-state={s.done ? "done" : isNext ? "next" : "later"} data-testid={`v2-launch-step-${s.key}`}>
              <div className="top">
                <span className="n">{String(i + 1).padStart(2, "0")}</span>
                <Lamp signal={signal} live={isNext} />
                {s.done && <Check size={14} className="v2-launch-tick" role="img" aria-label={t("v2.onboard.launch.done")} />}
              </div>
              <b>{t(`v2.onboard.launch.${s.key}`)}</b>
              <small>{t(s.hintKey)}</small>
              {!s.done &&
                (s.href ? (
                  <Link to={s.href} className={isNext ? "v2-btn sm primary" : "v2-btn sm"}>
                    {t(`v2.onboard.launch.${s.key}Cta`)}
                    <ArrowRight size={13} aria-hidden="true" />
                  </Link>
                ) : (
                  <span className="v2-launch-noact">{t("v2.onboard.launch.askAdmin")}</span>
                ))}
            </li>
          );
        })}
      </ol>
    </section>
  );
}
