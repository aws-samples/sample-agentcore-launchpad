// "?" in the top bar: the command palette, the glossary and the demo videos, any
// time. Ported from console-v3's HelpMenu (v3/Shell.tsx) as an ARIA menu button.
// Step 3's tour and launch checklist slot in as further items.
import { BookOpen, CircleHelp, PlayCircle, Search } from "lucide-react";
import { type KeyboardEvent, type ReactNode, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { useNavigate } from "react-router-dom";

import { PALETTE_SHORTCUT } from "./CommandPalette";
import { openGlossary } from "./glossaryTerms";
import { openPalette, usePaletteOpen } from "./palette";

function menuItems(menu: HTMLElement | null): HTMLElement[] {
  return Array.from(menu?.querySelectorAll<HTMLElement>('[role="menuitem"]') ?? []);
}

interface HelpItem {
  key: string;
  icon: ReactNode;
  label: string;
  shortcut?: string;
  run: () => void;
}

export function HelpMenu() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);
  const buttonRef = useRef<HTMLButtonElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  const paletteOpen = usePaletteOpen();

  const items: HelpItem[] = [
    {
      key: "palette",
      icon: <Search size={15} aria-hidden="true" />,
      label: t("v2.help.palette"),
      shortcut: PALETTE_SHORTCUT,
      run: openPalette,
    },
    { key: "glossary", icon: <BookOpen size={15} aria-hidden="true" />, label: t("v2.help.glossary"), run: () => openGlossary() },
    {
      key: "videos",
      icon: <PlayCircle size={15} aria-hidden="true" />,
      label: t("v2.help.videos"),
      run: () => navigate("/v2/videos"),
    },
  ];

  // an outside press closes the menu
  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (!rootRef.current?.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", onDown);
    return () => document.removeEventListener("mousedown", onDown);
  }, [open]);
  // ⌘K over an open menu: the palette takes over
  useEffect(() => {
    if (paletteOpen) setOpen(false);
  }, [paletteOpen]);
  // opening moves focus onto the first item (menu-button pattern)
  useEffect(() => {
    if (open) menuItems(menuRef.current)[0]?.focus();
  }, [open]);

  // Hand focus back to the "?" button first: the palette and the glossary
  // remember the focused element to return to, and the item is about to unmount.
  const choose = (item: HelpItem) => {
    buttonRef.current?.focus();
    setOpen(false);
    item.run();
  };

  const onMenuKey = (e: KeyboardEvent<HTMLDivElement>) => {
    const list = menuItems(menuRef.current);
    const at = list.indexOf(document.activeElement as HTMLElement);
    const move = (to: number) => {
      e.preventDefault();
      list[(to + list.length) % list.length]?.focus();
    };
    switch (e.key) {
      case "Escape":
        e.preventDefault();
        e.stopPropagation();
        setOpen(false);
        buttonRef.current?.focus();
        break;
      case "Tab":
        // leave from the button, so Tab / Shift+Tab continue from the top bar
        buttonRef.current?.focus();
        setOpen(false);
        break;
      case "ArrowDown":
        move(at + 1);
        break;
      case "ArrowUp":
        move(at - 1);
        break;
      case "Home":
        move(0);
        break;
      case "End":
        move(list.length - 1);
        break;
    }
  };

  return (
    <div className="v2-help" ref={rootRef}>
      <button
        ref={buttonRef}
        type="button"
        className="v2-help-btn"
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label={t("v2.help.label")}
        title={t("v2.help.label")}
        onClick={() => setOpen((v) => !v)}
        onKeyDown={(e) => {
          if (!open && (e.key === "ArrowDown" || e.key === "ArrowUp")) {
            e.preventDefault();
            setOpen(true);
          }
        }}
        data-testid="v2-help"
      >
        <CircleHelp size={17} aria-hidden="true" />
      </button>
      {open && (
        <div className="v2-help-menu" role="menu" aria-label={t("v2.help.label")} ref={menuRef} onKeyDown={onMenuKey}>
          {items.map((item) => (
            <button
              key={item.key}
              type="button"
              role="menuitem"
              tabIndex={-1}
              onClick={() => choose(item)}
              data-testid={`v2-help-${item.key}`}
            >
              {item.icon}
              <span className="txt">{item.label}</span>
              {item.shortcut && <kbd aria-hidden="true">{item.shortcut}</kbd>}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
