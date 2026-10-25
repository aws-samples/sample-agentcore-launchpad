import { EyeOff, LayoutDashboard, ListChecks, type LucideIcon, Scale, ShieldAlert, UserCheck } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";

import { useAuth } from "../../../auth/auth-context";
import { ErrorCallout } from "../../../components/ErrorCallout";
import { navSettingsApi } from "../../../lib/api/navSettings";
import { parseTimestamp } from "../../../lib/timestamps";
import { useFailure } from "../../failure";
import { useLoad, useToast } from "../../hooks";
import { useHiddenNav } from "../../hiddenNav";
import { FULL_HIDDEN, HOME_SECTIONS, hideableGroups, hideableKeys, menuMode, NORMAL_HIDDEN, PINNED_HIDEABLE } from "../../nav";
import { Btn, Chip, Notice, PageHead, Panel, Skeleton } from "../../ui";

interface Entry {
  key: string;
  labelKey: string;
  icon: LucideIcon;
}

/** The pinned rail entries Menu settings offers (`PINNED_HIDEABLE`): 智能体 and 对话 stay. */
const PINNED_LOOK: Record<string, Omit<Entry, "key">> = {
  "/v3": { labelKey: "v3.nav.home", icon: LayoutDashboard },
  "/v3/gate": { labelKey: "v3.nav.gate", icon: Scale },
};
const PINNED: Entry[] = PINNED_HIDEABLE.map((key) => ({ key, ...PINNED_LOOK[key] }));

/** The Command center sections Menu settings offers (`HOME_SECTIONS`). */
const SECTIONS: Entry[] = [
  { key: HOME_SECTIONS.launch, labelKey: "v3.onboard.launch.title", icon: ListChecks },
  { key: HOME_SECTIONS.turn, labelKey: "story.turn.title", icon: UserCheck },
];

/**
 * Menu settings (admin-only): which entries everyone — administrators included — sees
 * in the V3 rail and ⌘K, and which Command center sections show. Normal and Full fill
 * the draft; Save applies it. A view filter, not authorization — a hidden page stays
 * reachable by URL, and administrator-only entries are never offered.
 */
export function V3MenuSettings() {
  const { isAdmin } = useAuth();
  const { t } = useTranslation();
  if (!isAdmin) {
    return (
      <div className="v3-reveal" style={{ display: "grid", gap: 16 }}>
        <PageHead eyebrow={t("v2.nav.groupAdmin")} title={t("v3.menuSettings.title")} sub={t("auth.adminRequired.meta")} />
        <Panel>
          <div className="v3-empty" data-testid="v3-menu-settings-forbidden">
            <ShieldAlert size={28} aria-hidden="true" />
            <strong>{t("auth.adminRequired.title")}</strong>
            {t("auth.adminRequired.body")}
          </div>
        </Panel>
      </div>
    );
  }
  return <Editor />;
}

function Editor() {
  const { t } = useTranslation();
  const toast = useToast();
  const failed = useFailure();
  const load = useLoad(() => navSettingsApi.hiddenModules(), "v3-menu-settings");
  const shellNav = useHiddenNav();
  const groups = useMemo(hideableGroups, []);
  // every key this page offers: the only ones a save writes and a mode is judged on
  const known = useMemo(hideableKeys, []);
  const [hidden, setHidden] = useState<Set<string> | null>(null);
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    if (load.data) setHidden(new Set(load.data.hidden));
  }, [load.data]);

  const saved = useMemo(() => new Set(load.data?.hidden ?? []), [load.data]);
  const dirty = hidden !== null && (hidden.size !== saved.size || [...hidden].some((k) => !saved.has(k)));
  const toggle = (key: string, show: boolean) =>
    setHidden((prev) => {
      const next = new Set(prev ?? []);
      if (show) next.delete(key);
      else next.add(key);
      return next;
    });
  const fill = (preset: readonly string[]) => setHidden(new Set(preset.filter((k) => known.has(k))));
  const mode = hidden ? menuMode(hidden, known) : null;
  const save = async () => {
    if (!hidden) return;
    setBusy(true);
    try {
      failed.clear();
      // keep keys the catalog no longer lists out of the write: only what this page shows
      await navSettingsApi.setHiddenModules([...hidden].filter((k) => known.has(k)).sort());
      toast("ok", t("v3.menuSettings.saved"));
      load.reload();
      // the administrator's own rail and Command center follow at once
      shellNav.reload();
    } catch (err) {
      failed.fail(err);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="v3-reveal" style={{ display: "grid", gap: 16 }}>
      <PageHead eyebrow={t("v2.nav.groupAdmin")} title={t("v3.menuSettings.title")} sub={t("v3.menuSettings.sub")} />
      <ErrorCallout error={failed.error ?? load.failure} onDismiss={failed.clear} data-testid="v3-menu-settings-error" />
      <Notice>{t("v3.menuSettings.viewOnly")}</Notice>
      {hidden === null ? (
        <Panel><Skeleton rows={6} /></Panel>
      ) : (
        <>
          <Panel title={t("v3.menuSettings.quick")}
            end={<span data-testid="v3-menu-mode" data-mode={mode ?? undefined}>
              <Chip s={mode === "custom" ? "info" : "ok"}>
                {t("v3.menuSettings.currentMode", { mode: t(`v3.menuSettings.mode.${mode ?? "custom"}`) })}
              </Chip>
            </span>}>
            <div style={{ display: "grid", gap: 10 }}>
              <p style={{ margin: 0, color: "var(--v3-text-2)" }}>{t("v3.menuSettings.quickSub")}</p>
              <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
                <Btn kind={mode === "normal" ? "primary" : undefined} onClick={() => fill(NORMAL_HIDDEN)} data-testid="v3-menu-preset-normal">
                  {t("v3.menuSettings.mode.normal")}
                </Btn>
                <Btn kind={mode === "full" ? "primary" : undefined} onClick={() => fill(FULL_HIDDEN)} data-testid="v3-menu-preset-full">
                  {t("v3.menuSettings.mode.full")}
                </Btn>
              </div>
            </div>
          </Panel>
          <EntryPanel title={t("v3.menuSettings.pinned")} sub={t("v3.menuSettings.pinnedSub")} entries={PINNED} hidden={hidden} onToggle={toggle} />
          <EntryPanel title={t("v3.menuSettings.sections")} sub={t("v3.menuSettings.sectionsSub")} entries={SECTIONS} hidden={hidden} onToggle={toggle} />
          {groups.map((group) => (
            <Panel key={group.key} title={t(group.labelKey)}
              end={t("v3.menuSettings.shownCount", {
                shown: group.items.filter((i) => !hidden.has(i.railKey)).length,
                total: group.items.length,
              })}>
              <EntryGrid entries={group.items.map((i) => ({ key: i.railKey, labelKey: i.labelKey, icon: i.icon }))}
                hidden={hidden} onToggle={toggle} />
            </Panel>
          ))}
          <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
            <Btn kind="primary" busy={busy} disabled={!dirty} disabledReason={t("story.reason.noChanges")}
              onClick={() => void save()} data-testid="v3-menu-settings-save">
              {t("v3.menuSettings.save")}
            </Btn>
            <Btn kind="ghost" disabled={!dirty} disabledReason={t("story.reason.noChanges")}
              onClick={() => setHidden(new Set(saved))}>
              {t("v3.menuSettings.reset")}
            </Btn>
            {load.data?.updated_by && (
              <span style={{ color: "var(--v3-text-3)", fontSize: 12 }}>
                {t("v3.menuSettings.updated", {
                  who: load.data.updated_by,
                  when: parseTimestamp(load.data.updated_at)?.toLocaleString() ?? "—",
                })}
              </span>
            )}
          </div>
        </>
      )}
    </div>
  );
}

function EntryPanel({ title, sub, entries, hidden, onToggle }: {
  title: string;
  sub: string;
  entries: Entry[];
  hidden: ReadonlySet<string>;
  onToggle: (key: string, show: boolean) => void;
}) {
  const { t } = useTranslation();
  return (
    <Panel title={title}
      end={t("v3.menuSettings.shownCount", { shown: entries.filter((e) => !hidden.has(e.key)).length, total: entries.length })}>
      <div style={{ display: "grid", gap: 10 }}>
        <p style={{ margin: 0, color: "var(--v3-text-2)" }}>{sub}</p>
        <EntryGrid entries={entries} hidden={hidden} onToggle={onToggle} />
      </div>
    </Panel>
  );
}

/** One checkbox per entry: checked = shown. */
function EntryGrid({ entries, hidden, onToggle }: {
  entries: Entry[];
  hidden: ReadonlySet<string>;
  onToggle: (key: string, show: boolean) => void;
}) {
  const { t } = useTranslation();
  return (
    <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(220px, 1fr))", gap: 8 }}>
      {entries.map((entry) => {
        const Icon = entry.icon;
        const shown = !hidden.has(entry.key);
        return (
          <label key={entry.key} style={{ display: "flex", gap: 8, alignItems: "center", opacity: shown ? 1 : 0.55 }}>
            <input type="checkbox" checked={shown} onChange={(e) => onToggle(entry.key, e.target.checked)}
              data-testid={`v3-menu-item-${entry.key}`} />
            <Icon size={15} aria-hidden="true" />
            {t(entry.labelKey)}
            {!shown && <EyeOff size={13} aria-label={t("v3.menuSettings.hidden")} />}
          </label>
        );
      })}
    </div>
  );
}
