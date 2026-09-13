import type { CSSProperties, HTMLAttributes, ReactNode } from "react";

/** `data-*` / aria attributes fall through to the panel root (probes, test ids). */
interface PanelProps extends Omit<HTMLAttributes<HTMLDivElement>, "title" | "className" | "style"> {
  title?: ReactNode;
  sub?: ReactNode;
  end?: ReactNode;
  brk?: boolean;
  pad?: boolean;
  className?: string;
  style?: CSSProperties;
  children?: ReactNode;
}

export function Panel({
  title,
  sub,
  end,
  brk = false,
  pad = true,
  className = "",
  style,
  children,
  ...rest
}: PanelProps) {
  return (
    <div
      {...rest}
      className={["panel", brk ? "brk" : "", className].filter(Boolean).join(" ")}
      style={style}
    >
      {(title || sub || end) && (
        <div className="phead">
          {title != null && <h2>{title}</h2>}
          {sub != null && <span className="sub">{sub}</span>}
          {end != null && <div className="end">{end}</div>}
        </div>
      )}
      {pad ? <div className="pbody">{children}</div> : children}
    </div>
  );
}
