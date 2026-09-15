import { useRef } from "react";
import { useTranslation } from "react-i18next";

import type { AssistantFishbone, FishboneBarrier, FishboneDimension } from "../lib/api";
import { FISHBONE_DIMENSIONS } from "../lib/api";
import { Btn } from "./Btn";
import { Chip } from "./Chip";
import type { ChipTone } from "./Chip";

/**
 * Agent-DLC five-dimension fishbone, rendered from the proposal's structured
 * `fishbone` member. Layout follows the methodology's template: the scenario is the
 * fish head, three dimensions ride above the spine and three below, each with up to
 * three sticky notes (the `selected` barriers). The SVG uses literal colours (no CSS
 * variables) so `outerHTML` is a complete, standalone file for DOWNLOAD SVG.
 */

const W = 1200;
const H = 640;
const SPINE_Y = 340;
const SPINE_X0 = 60;
const SPINE_X1 = 930;
const BONE_X = [300, 580, 860];
const BONE_DX = 60;
const BONE_DY = 215;
const NOTE_W = 200;
const NOTE_H = 56;
const NOTE_T = [0.3, 0.58, 0.86];
const HEAD = { x: 940, y: 292, w: 236, h: 96 };

const C = {
  bg: "#141816",
  line: "#3A423D",
  ink: "#E9EDEA",
  ink2: "#A3ACA6",
  ink3: "#6B756F",
  amber: "#FFB000",
  good: "#59C15A",
  note: "#0E1210",
  noteLine: "#2E3833",
};

const TOP: FishboneDimension[] = ["cognition", "quality", "responsibility"];
const BOTTOM: FishboneDimension[] = ["cost", "performance", "other"];

/** Greedy wrap by rendered width: CJK ≈ 1em, Latin ≈ 0.55em. */
function wrap(text: string, maxUnits: number, maxLines: number): string[] {
  const units = (ch: string) => (/[\u3000-\u9fff\uff00-\uffef]/.test(ch) ? 1 : 0.55);
  const lines: string[] = [];
  let cur = "";
  let curUnits = 0;
  for (const ch of text.trim()) {
    const u = units(ch);
    if (curUnits + u > maxUnits && cur) {
      lines.push(cur);
      cur = "";
      curUnits = 0;
      if (lines.length === maxLines) break;
    }
    cur += ch;
    curUnits += u;
  }
  if (cur && lines.length < maxLines) lines.push(cur);
  if (lines.length === maxLines) {
    const consumed = lines.join("").length;
    if (consumed < text.trim().length) lines[maxLines - 1] = `${lines[maxLines - 1].slice(0, -1)}…`;
  }
  return lines;
}

function selectedNotes(fb: AssistantFishbone, dim: FishboneDimension): FishboneBarrier[] {
  const all = fb.barriers[dim] ?? [];
  const picked = all.filter((b) => b.selected && b.confirmed);
  return picked.slice(0, 3);
}

const COVERAGE_TONE: Record<string, ChipTone> = {
  confirmed: "good",
  explored_empty: "muted",
  unresolved: "warn",
};

export function FishboneDiagram({ fishbone }: { fishbone: AssistantFishbone }) {
  const { t } = useTranslation();
  const svgRef = useRef<SVGSVGElement | null>(null);

  const dimLabel = (d: FishboneDimension) => t(`fishbone.dim.${d}`);
  const coverageLabel = (d: FishboneDimension) => {
    const c = fishbone.coverage[d];
    return c ? t(`fishbone.coverage.${c}`) : "";
  };

  const download = (name: string, mime: string, body: string) => {
    const blob = new Blob([body], { type: mime });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = name;
    a.click();
    URL.revokeObjectURL(url);
  };
  const slug = `${fishbone.customer}-${fishbone.date}-fishbone`.replace(/[^\w.-]+/g, "_");
  const downloadSvg = () => {
    const el = svgRef.current;
    if (!el) return;
    const markup = el.outerHTML.replace(
      "<svg",
      '<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink"',
    );
    download(`${slug}.svg`, "image/svg+xml;charset=utf-8", `<?xml version="1.0" encoding="UTF-8"?>\n${markup}`);
  };
  const downloadJson = () =>
    download(`${slug}.json`, "application/json;charset=utf-8", JSON.stringify(fishbone, null, 2));

  const header = `${fishbone.customer} · ${fishbone.date} · ${t(`fishbone.target.${fishbone.service_target}`)}`;
  const headLines = wrap(fishbone.use_case, 15, 3);

  const bone = (dim: FishboneDimension, bx: number, up: boolean) => {
    const dir = up ? -1 : 1;
    const tipX = bx - BONE_DX;
    const tipY = SPINE_Y + dir * BONE_DY;
    const notes = selectedNotes(fishbone, dim);
    const cov = fishbone.coverage[dim];
    const labelY = up ? tipY - 14 : tipY + 24;
    return (
      <g key={dim} data-dimension={dim}>
        <line x1={bx} y1={SPINE_Y} x2={tipX} y2={tipY} stroke={C.line} strokeWidth={2} />
        <text x={tipX} y={labelY} fill={C.amber} fontSize={14} fontWeight={700} textAnchor="middle"
              fontFamily="ui-sans-serif, system-ui, sans-serif">
          {dimLabel(dim)}
        </text>
        {cov && cov !== "confirmed" && (
          <text x={tipX} y={labelY + (up ? -16 : 16)} fill={cov === "unresolved" ? C.amber : C.ink3}
                fontSize={10} textAnchor="middle" fontFamily="ui-monospace, monospace">
            {coverageLabel(dim)}
          </text>
        )}
        {notes.map((n, i) => {
          const tt = NOTE_T[i];
          const px = bx - BONE_DX * tt;
          const py = SPINE_Y + dir * BONE_DY * tt;
          const x = px - 8 - NOTE_W;
          const y = py - NOTE_H / 2;
          const lines = wrap(n.sticky_text, 16, 3);
          return (
            <g key={i}>
              <line x1={px} y1={py} x2={x + NOTE_W} y2={py} stroke={C.line} strokeWidth={1.5} />
              <rect x={x} y={y} width={NOTE_W} height={NOTE_H} fill={C.note} stroke={C.noteLine} />
              <rect x={x} y={y} width={3} height={NOTE_H} fill={C.amber} />
              {lines.map((ln, k) => (
                <text key={k} x={x + 10} y={y + 17 + k * 14} fill={C.ink} fontSize={11}
                      fontFamily="ui-sans-serif, system-ui, sans-serif">
                  {ln}
                </text>
              ))}
            </g>
          );
        })}
      </g>
    );
  };

  const allNotes = FISHBONE_DIMENSIONS.flatMap((d) =>
    (fishbone.barriers[d] ?? []).map((b) => ({ dim: d, ...b })),
  );

  return (
    <div className="fishbone" data-testid="fishbone">
      <div className="row" style={{ gap: 8, alignItems: "center", flexWrap: "wrap", marginBottom: 8 }}>
        <span className="mono dim" style={{ fontSize: 10.5 }}>{header}</span>
        <span className="spacer" style={{ flex: 1 }} />
        {FISHBONE_DIMENSIONS.map((d) => (
          <Chip key={d} tone={COVERAGE_TONE[fishbone.coverage[d] ?? "unresolved"]} className="mono">
            {dimLabel(d)} · {coverageLabel(d) || "—"}
          </Chip>
        ))}
      </div>
      <svg
        ref={svgRef}
        viewBox={`0 0 ${W} ${H}`}
        width="100%"
        role="img"
        aria-label={t("fishbone.title")}
        data-testid="fishbone-svg"
        style={{ display: "block", background: C.bg, border: `1px solid ${C.noteLine}` }}
      >
        <rect x={0} y={0} width={W} height={H} fill={C.bg} />
        <text x={24} y={30} fill={C.ink2} fontSize={12} fontFamily="ui-monospace, monospace" letterSpacing={1}>
          {t("fishbone.title").toUpperCase()}
        </text>
        <text x={W - 24} y={30} fill={C.ink3} fontSize={11} textAnchor="end" fontFamily="ui-monospace, monospace">
          {header}
        </text>
        {/* spine */}
        <line x1={SPINE_X0} y1={SPINE_Y} x2={SPINE_X1} y2={SPINE_Y} stroke={C.ink2} strokeWidth={3} />
        <polygon points={`${SPINE_X0},${SPINE_Y - 10} ${SPINE_X0 + 18},${SPINE_Y} ${SPINE_X0},${SPINE_Y + 10}`} fill={C.ink2} />
        {/* head */}
        <rect x={HEAD.x} y={HEAD.y} width={HEAD.w} height={HEAD.h} rx={12} fill={C.note} stroke={C.amber} strokeWidth={2} />
        <text x={HEAD.x + 12} y={HEAD.y + 20} fill={C.amber} fontSize={10} fontFamily="ui-monospace, monospace" letterSpacing={1}>
          {t("fishbone.head").toUpperCase()}
        </text>
        {headLines.map((ln, k) => (
          <text key={k} x={HEAD.x + 12} y={HEAD.y + 42 + k * 17} fill={C.ink} fontSize={13} fontWeight={600}
                fontFamily="ui-sans-serif, system-ui, sans-serif">
            {ln}
          </text>
        ))}
        {TOP.map((d, i) => bone(d, BONE_X[i], true))}
        {BOTTOM.map((d, i) => bone(d, BONE_X[i], false))}
        <text x={24} y={H - 16} fill={C.ink3} fontSize={10} fontFamily="ui-monospace, monospace">
          {t("fishbone.footer")}
        </text>
      </svg>
      <div className="assist-actions" style={{ marginTop: 8 }}>
        <Btn onClick={downloadSvg} data-testid="fishbone-download-svg">{t("fishbone.downloadSvg")}</Btn>
        <Btn onClick={downloadJson} data-testid="fishbone-download-json">{t("fishbone.downloadJson")}</Btn>
        <span className="dim" style={{ fontSize: 11 }}>{t("fishbone.downloadHint")}</span>
      </div>
      {allNotes.length > 0 && (
        <details className="fishbone-details" data-testid="fishbone-barriers">
          <summary className="mono dim">{t("fishbone.allBarriers", { n: allNotes.length })}</summary>
          <table className="assist-gt">
            <thead>
              <tr>
                <th>{t("fishbone.col.dimension")}</th>
                <th>{t("fishbone.col.sticky")}</th>
                <th>{t("fishbone.col.evidence")}</th>
                <th>{t("fishbone.col.status")}</th>
              </tr>
            </thead>
            <tbody>
              {allNotes.map((n, i) => (
                <tr key={i}>
                  <td className="mono">{dimLabel(n.dim)}</td>
                  <td>{n.sticky_text}</td>
                  <td className="dim">
                    {n.evidence}
                    {n.customer_quote ? (
                      <div className="mono" style={{ fontSize: 10.5 }}>“{n.customer_quote}”</div>
                    ) : null}
                  </td>
                  <td>
                    <Chip tone={n.confirmed ? "good" : "warn"}>
                      {n.confirmed ? t("fishbone.confirmed") : t("fishbone.unconfirmed")}
                    </Chip>{" "}
                    {n.selected && <Chip tone="muted">{t("fishbone.selected")}</Chip>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </details>
      )}
      {(fishbone.parking_lot?.length ?? 0) > 0 && (
        <details className="fishbone-details" data-testid="fishbone-parking-lot">
          <summary className="mono dim">{t("fishbone.parkingLot", { n: fishbone.parking_lot!.length })}</summary>
          <ul className="assist-steps">
            {fishbone.parking_lot!.map((p, i) => (
              <li key={i}>
                {p.original}
                {p.converted_to ? <span className="dim"> → {p.converted_to}</span> : null}
              </li>
            ))}
          </ul>
        </details>
      )}
    </div>
  );
}
