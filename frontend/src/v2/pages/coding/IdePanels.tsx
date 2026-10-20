import { FileCode2, FolderTree, PanelLeftClose, PanelLeftOpen, PanelRightClose, PanelRightOpen } from "lucide-react";
import { type PointerEvent, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import { dragWidth, keyWidth, type Panel, type PanelLayout } from "./layout";

const NAME_KEY: Record<Panel, string> = { files: "v2.coding.ide.files", workbench: "v2.coding.layout.workbench" };
const COLLAPSE_KEY: Record<Panel, string> = {
  files: "v2.coding.layout.collapseFiles",
  workbench: "v2.coding.layout.collapseWorkbench",
};
const EXPAND_KEY: Record<Panel, string> = {
  files: "v2.coding.layout.expandFiles",
  workbench: "v2.coding.layout.expandWorkbench",
};

/** The header button that hides an open side panel (inline: collapse to its rail;
 *  overlay: close the overlay). `data-focus` lets the IDE move focus to it. */
export function PanelToggle({ panel, controls, onClick }: { panel: Panel; controls: string; onClick: () => void }) {
  const { t } = useTranslation();
  const Icon = panel === "files" ? PanelLeftClose : PanelRightClose;
  const label = t(COLLAPSE_KEY[panel]);
  return (
    <button
      type="button"
      className="v2-coding-icon-btn"
      aria-label={label}
      title={label}
      aria-expanded
      aria-controls={controls}
      onClick={onClick}
      data-focus={`${panel}-toggle`}
      data-testid={`v2-coding-${panel}-toggle`}
    >
      <Icon size={15} aria-hidden="true" />
    </button>
  );
}

/** CJK names stack one upright character per line (horizontal metrics — vertical
 *  writing relies on the font's vertical advances, which some fallback fonts lack and
 *  then overlap every glyph); Latin names run rotated in vertical writing mode. */
const CJK = /[\u3040-\u30ff\u3400-\u9fff\uf900-\ufaff]/;

export interface RailBadge {
  key: string;
  /** shown in the badge; null = a dot */
  text: string | null;
  title: string;
  tone?: "warn";
}

/**
 * A collapsed side panel: a slim strip with the panel's icon, its name (vertical) and
 * count badges. The whole strip is one button that opens the panel — inline when it
 * fits, else as an overlay (then the strip closes it again).
 */
export function PaneRail({
  panel,
  controls,
  expanded,
  badges = [],
  onClick,
}: {
  panel: Panel;
  controls: string;
  /** the panel shows as an overlay next to this rail */
  expanded: boolean;
  badges?: RailBadge[];
  onClick: () => void;
}) {
  const { t } = useTranslation();
  const name = t(NAME_KEY[panel]);
  const Toggle =
    panel === "files" ? (expanded ? PanelLeftClose : PanelLeftOpen) : expanded ? PanelRightClose : PanelRightOpen;
  const Icon = panel === "files" ? FolderTree : FileCode2;
  const action = t(expanded ? COLLAPSE_KEY[panel] : EXPAND_KEY[panel]);
  return (
    <div className={`v2-coding-rail v2-coding-rail-${panel}`} data-testid={`v2-coding-${panel}-rail`}>
      <button
        type="button"
        className="v2-coding-rail-btn"
        aria-label={[name, ...badges.map((b) => b.title)].join(" · ")}
        title={action}
        aria-expanded={expanded}
        aria-controls={controls}
        onClick={onClick}
        data-focus={`${panel}-rail`}
      >
        <Toggle size={16} aria-hidden="true" className="v2-coding-rail-toggle" />
        <Icon size={16} aria-hidden="true" />
        <span className={CJK.test(name) ? "v2-coding-rail-label cjk" : "v2-coding-rail-label"} aria-hidden="true">
          {name}
        </span>
        {badges.map((b) => (
          <span
            key={b.key}
            className={`v2-coding-rail-badge${b.text === null ? " dot" : ""}${b.tone ? ` ${b.tone}` : ""}`}
            title={b.title}
            aria-hidden="true"
          >
            {b.text}
          </span>
        ))}
      </button>
    </div>
  );
}

/**
 * The drag handle between a side panel and the conversation (a focusable
 * `separator`): drag with the pointer (captured, so the pointer may leave the handle),
 * ←/→ by 16 px (Shift: 64 px), Home/End to the ends, double-click resets the default.
 * Widths stay inside the panel's current range, so the conversation keeps its minimum.
 */
export function PaneResizer({
  panel,
  pane,
  controls,
  onWidth,
  onResizing,
}: {
  panel: Panel;
  pane: PanelLayout;
  controls: string;
  /** a new width for the panel; null = reset to the default */
  onWidth: (width: number | null) => void;
  onResizing: (active: boolean) => void;
}) {
  const { t } = useTranslation();
  const drag = useRef<{ pointer: number; x: number; width: number; min: number; max: number } | null>(null);
  const [active, setActive] = useState(false);

  const end = (e: PointerEvent<HTMLDivElement>) => {
    if (drag.current?.pointer !== e.pointerId) return;
    drag.current = null;
    setActive(false);
    onResizing(false);
  };

  return (
    <div
      role="separator"
      aria-orientation="vertical"
      aria-label={t(panel === "files" ? "v2.coding.layout.resizeFiles" : "v2.coding.layout.resizeWorkbench")}
      aria-controls={controls}
      aria-valuenow={pane.width}
      aria-valuemin={pane.min}
      aria-valuemax={pane.max}
      tabIndex={0}
      title={t("v2.coding.layout.resizeHint")}
      className={`v2-coding-resizer v2-coding-resizer-${panel}${active ? " on" : ""}`}
      onPointerDown={(e) => {
        if (e.button !== 0) return;
        // no text selection, no native drag; the handle keeps the keyboard focus
        e.preventDefault();
        e.currentTarget.focus();
        e.currentTarget.setPointerCapture(e.pointerId);
        drag.current = { pointer: e.pointerId, x: e.clientX, width: pane.width, min: pane.min, max: pane.max };
        setActive(true);
        onResizing(true);
      }}
      onPointerMove={(e) => {
        const d = drag.current;
        if (!d || d.pointer !== e.pointerId) return;
        onWidth(dragWidth(panel, d.width, e.clientX - d.x, d));
      }}
      onPointerUp={end}
      onPointerCancel={end}
      onLostPointerCapture={end}
      onKeyDown={(e) => {
        const next = keyWidth(panel, e.key, e.shiftKey, pane.width, pane);
        if (next === null) return;
        e.preventDefault();
        onWidth(next);
      }}
      onDoubleClick={() => onWidth(null)}
      data-testid={`v2-coding-resize-${panel}`}
    />
  );
}
