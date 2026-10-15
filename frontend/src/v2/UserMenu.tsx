// The signed-in person in the top bar, as an ARIA menu button (the HelpMenu pattern):
// My usage — their own spend against their monthly budget — and, when sign-in is on,
// Sign out. The My usage item shows the share used once a limit applies.
import { ChevronDown, LogOut, Wallet } from "lucide-react";
import { type KeyboardEvent, type ReactNode, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { useNavigate } from "react-router-dom";

import { fmtPercent, usageTone } from "../lib/budgets";
import { MY_USAGE_PATH } from "./BudgetBanner";
import { useMyUsage } from "./myUsage";
import { usePaletteOpen } from "./palette";

function menuItems(menu: HTMLElement | null): HTMLElement[] {
  return Array.from(menu?.querySelectorAll<HTMLElement>('[role="menuitem"]') ?? []);
}

interface UserItem {
  key: string;
  icon: ReactNode;
  label: string;
  aside?: ReactNode;
  run: () => void;
}

export function UserMenu({
  displayName,
  authRequired,
  onLogout,
}: {
  displayName: string;
  authRequired: boolean;
  onLogout: () => void;
}) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);
  const buttonRef = useRef<HTMLButtonElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  const paletteOpen = usePaletteOpen();
  const usage = useMyUsage(displayName).data;
  const tone = usage ? usageTone(usage) : "ok";

  const items: UserItem[] = [
    {
      key: "usage",
      icon: <Wallet size={15} aria-hidden="true" />,
      label: t("v2.usage.title"),
      aside:
        usage && !usage.unlimited ? (
          <span className={`v2-user-pct ${tone}`}>{fmtPercent(usage.percent_used)}</span>
        ) : undefined,
      run: () => navigate(MY_USAGE_PATH),
    },
    ...(authRequired
      ? [{ key: "logout", icon: <LogOut size={15} aria-hidden="true" />, label: t("auth.logout"), run: onLogout }]
      : []),
  ];

  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (!rootRef.current?.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", onDown);
    return () => document.removeEventListener("mousedown", onDown);
  }, [open]);
  useEffect(() => {
    if (paletteOpen) setOpen(false);
  }, [paletteOpen]);
  useEffect(() => {
    if (open) menuItems(menuRef.current)[0]?.focus();
  }, [open]);

  const choose = (item: UserItem) => {
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
    <div className="v2-user" ref={rootRef}>
      <button
        ref={buttonRef}
        type="button"
        className="v2-user-btn"
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label={t("v2.usage.userMenu", { name: displayName })}
        onClick={() => setOpen((v) => !v)}
        onKeyDown={(e) => {
          if (!open && (e.key === "ArrowDown" || e.key === "ArrowUp")) {
            e.preventDefault();
            setOpen(true);
          }
        }}
        data-testid="v2-user-menu"
      >
        <span className="v2-avatar">{displayName.slice(0, 1).toUpperCase()}</span>
        <span>{displayName}</span>
        <ChevronDown size={14} aria-hidden="true" />
      </button>
      {open && (
        <div className="v2-help-menu" role="menu" aria-label={displayName} ref={menuRef} onKeyDown={onMenuKey}>
          {items.map((item) => (
            <button
              key={item.key}
              type="button"
              role="menuitem"
              tabIndex={-1}
              onClick={() => choose(item)}
              data-testid={`v2-user-${item.key}`}
            >
              {item.icon}
              <span className="txt">{item.label}</span>
              {item.aside}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}
