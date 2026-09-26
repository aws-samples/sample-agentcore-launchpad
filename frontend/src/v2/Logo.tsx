import { useId } from "react";

/**
 * The AgentOps mark: the ∞ Ops loop on the V2 blue tile. The dim left lobe is the
 * build half (create, generate, package), the bright right lobe the run half
 * (deploy, invoke, observe, evaluate), and the cyan node is the agent riding it —
 * the console's create → deploy → invoke → observe cycle, never finished.
 */
export function V2Logo({ size = 28, className }: { size?: number; className?: string }) {
  const gradient = `v2-logo-${useId().replace(/:/g, "")}`;
  return (
    <svg width={size} height={size} viewBox="0 0 32 32" className={className} aria-hidden="true" focusable="false">
      <defs>
        <linearGradient id={gradient} x1="0" y1="0" x2="1" y2="1">
          <stop offset="0" stopColor="#2f7bff" />
          <stop offset="1" stopColor="#1250e6" />
        </linearGradient>
      </defs>
      <rect width="32" height="32" rx="8" fill={`url(#${gradient})`} />
      <path
        d="M16 16C13 9.5 5.5 9.5 5.5 16C5.5 22.5 13 22.5 16 16"
        fill="none"
        stroke="#fff"
        strokeOpacity=".55"
        strokeWidth="2.6"
        strokeLinecap="round"
      />
      <path
        d="M16 16C19 9.5 26.5 9.5 26.5 16C26.5 22.5 19 22.5 16 16"
        fill="none"
        stroke="#fff"
        strokeWidth="2.6"
        strokeLinecap="round"
      />
      <circle cx="22.4" cy="11.1" r="2.9" fill="#5ee7ff" stroke="#fff" strokeWidth="1.5" />
    </svg>
  );
}
