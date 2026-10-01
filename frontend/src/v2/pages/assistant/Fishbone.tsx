import { ChevronRight, Download } from "lucide-react";
import { useRef } from "react";
import { useTranslation } from "react-i18next";

import type { AssistantFishbone, FishboneBarrier, FishboneDimension } from "../../../lib/api";
import { FISHBONE_DIMENSIONS } from "../../../lib/api";
import {
  boneNotes,
  downloadFishboneJson,
  downloadFishboneSvg,
  FB,
  FISHBONE_BOTTOM,
  FISHBONE_TOP,
  textUnits,
  wrapText,
} from "../../../lib/fishbone";
import { Button, Table, Tag, type TagTone } from "../../ui";

/**
 * The Agent-DLC fishbone on the V2 light theme: the classic diagram's geometry
 * (lib/fishbone.ts) with the V2 palette — primary-blue spine and scenario head,
 * dimension pills, white note cards. The SVG still uses literal colours (no CSS
 * variables) so `outerHTML` is a complete, standalone file for the SVG download.
 */

const { W, H, SPINE_Y, SPINE_X0, SPINE_X1, BONE_X, BONE_DX, BONE_DY, NOTE_W, NOTE_H, NOTE_T, HEAD } = FB;

const C = {
  bg: "#f7f8fa",
  card: "#ffffff",
  line: "#e5e6eb",
  bone: "#c9cdd4",
  ink: "#1d2129",
  ink2: "#4e5969",
  ink3: "#86909c",
  primary: "#1664ff",
  primarySoft: "#e8f3ff",
  primaryLine: "#bedaff",
  onPrimary: "#ffffff",
  onPrimary2: "#d6e6ff",
  warn: "#d25f00",
  warnSoft: "#fff7e8",
  warnLine: "#ffcf8b",
  gray: "#f2f3f5",
};
const SANS =
  '-apple-system, BlinkMacSystemFont, "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif';
const SHADOW = "v2-fishbone-shadow";

const COVERAGE_TONE: Record<string, TagTone> = {
  confirmed: "green",
  explored_empty: "gray",
  unresolved: "orange",
};

type Row = FishboneBarrier & { dim: FishboneDimension };

function Pill({ cx, cy, text, size, fill, stroke, ink, weight }: {
  cx: number; cy: number; text: string; size: number; fill: string; stroke: string; ink: string; weight: number;
}) {
  const h = size + 12;
  const w = textUnits(text) * size + 22;
  return (
    <g>
      <rect x={cx - w / 2} y={cy - h / 2} width={w} height={h} rx={h / 2} fill={fill} stroke={stroke} />
      <text x={cx} y={cy + size * 0.36} fill={ink} fontSize={size} fontWeight={weight} textAnchor="middle" fontFamily={SANS}>
        {text}
      </text>
    </g>
  );
}

export function V2Fishbone({ fishbone }: { fishbone: AssistantFishbone }) {
  const { t } = useTranslation();
  const svgRef = useRef<SVGSVGElement | null>(null);

  const dimLabel = (d: FishboneDimension) => t(`fishbone.dim.${d}`);
  const coverageLabel = (d: FishboneDimension) => {
    const c = fishbone.coverage[d];
    return c ? t(`fishbone.coverage.${c}`) : "";
  };

  const header = `${fishbone.customer} · ${fishbone.date} · ${t(`fishbone.target.${fishbone.service_target}`)}`;
  const headLines = wrapText(fishbone.use_case, 15, 3, true);
  const footerLines = wrapText(t("fishbone.footer"), (W - 48) / 11.5, 2, true);

  const bone = (dim: FishboneDimension, bx: number, up: boolean) => {
    const dir = up ? -1 : 1;
    const tipX = bx - BONE_DX;
    const tipY = SPINE_Y + dir * BONE_DY;
    const notes = boneNotes(fishbone, dim);
    const cov = fishbone.coverage[dim];
    const label = dimLabel(dim);
    const labelY = tipY + dir * 18;
    const labelW = textUnits(label) * 13 + 22;
    const covText = coverageLabel(dim);
    return (
      <g key={dim} data-dimension={dim}>
        <line x1={bx} y1={SPINE_Y} x2={tipX} y2={tipY} stroke={C.bone} strokeWidth={2} strokeLinecap="round" />
        <Pill cx={tipX} cy={labelY} text={label} size={13} weight={600}
              fill={C.primarySoft} stroke={C.primaryLine} ink={C.primary} />
        {cov && cov !== "confirmed" && (
          <Pill cx={tipX + labelW / 2 + 6 + (textUnits(covText) * 11 + 22) / 2} cy={labelY} text={covText} size={11} weight={500}
                fill={cov === "unresolved" ? C.warnSoft : C.gray}
                stroke={cov === "unresolved" ? C.warnLine : C.line}
                ink={cov === "unresolved" ? C.warn : C.ink3} />
        )}
        {notes.map((n, i) => {
          const tt = NOTE_T[i];
          const px = bx - BONE_DX * tt;
          const py = SPINE_Y + dir * BONE_DY * tt;
          const x = px - 10 - NOTE_W;
          const y = py - NOTE_H / 2;
          const suggested = !n.confirmed;
          const body = wrapText(n.sticky_text, 15, suggested ? 2 : 3, true);
          const lines = suggested ? [t("fishbone.suggested"), ...body] : body;
          const top = y + NOTE_H / 2 - ((lines.length - 1) * 15) / 2 + 4;
          return (
            <g key={i} data-suggested={suggested || undefined}>
              <line x1={px} y1={py} x2={x + NOTE_W} y2={py} stroke={C.bone} strokeWidth={1} />
              <circle cx={px} cy={py} r={3} fill={C.card} stroke={suggested ? C.warnLine : C.primary} strokeWidth={1.5} />
              <rect x={x} y={y} width={NOTE_W} height={NOTE_H} rx={6}
                    fill={suggested ? C.warnSoft : C.card}
                    stroke={suggested ? C.warnLine : C.line}
                    strokeDasharray={suggested ? "4 3" : undefined}
                    filter={suggested ? undefined : `url(#${SHADOW})`} />
              <rect x={x + 8} y={y + 10} width={3} height={NOTE_H - 20} rx={1.5} fill={suggested ? C.warnLine : C.primary} />
              {lines.map((ln, k) => (
                <text key={k} x={x + 17} y={top + k * 15} fontFamily={SANS}
                      fill={suggested && k === 0 ? C.warn : suggested ? C.ink2 : C.ink}
                      fontSize={suggested && k === 0 ? 11 : 11.5} fontWeight={suggested && k === 0 ? 600 : 400}>
                  {ln}
                </text>
              ))}
            </g>
          );
        })}
      </g>
    );
  };

  const rows: Row[] = FISHBONE_DIMENSIONS.flatMap((d) =>
    (fishbone.barriers[d] ?? []).map((b) => ({ dim: d, ...b })),
  );
  const parking = fishbone.parking_lot ?? [];

  return (
    <div className="v2-assistant-fb" data-testid="fishbone">
      <div className="v2-tags v2-assistant-fb-bar">
        {FISHBONE_DIMENSIONS.map((d) => (
          <Tag key={d} dot tone={COVERAGE_TONE[fishbone.coverage[d] ?? "unresolved"]}>
            {dimLabel(d)} · {coverageLabel(d) || "—"}
          </Tag>
        ))}
      </div>
      <div className="v2-assistant-fb-canvas">
        <svg
          ref={svgRef}
          viewBox={`0 0 ${W} ${H}`}
          width="100%"
          role="img"
          aria-label={t("fishbone.title")}
          data-testid="fishbone-svg"
          style={{ display: "block", minWidth: 760 }}
        >
          <defs>
            <filter id={SHADOW} x="-10%" y="-20%" width="120%" height="150%">
              <feDropShadow dx={0} dy={1} stdDeviation={1.5} floodColor="#000000" floodOpacity={0.08} />
            </filter>
          </defs>
          <rect x={0} y={0} width={W} height={H} fill={C.bg} />
          <text x={24} y={38} fill={C.ink} fontSize={16} fontWeight={600} fontFamily={SANS}>
            {t("fishbone.title")}
          </text>
          <text x={W - 24} y={38} fill={C.ink3} fontSize={12} textAnchor="end" fontFamily={SANS}>
            {header}
          </text>
          <line x1={24} y1={58} x2={W - 24} y2={58} stroke={C.line} />
          {/* spine */}
          <line x1={SPINE_X0 + 10} y1={SPINE_Y} x2={SPINE_X1} y2={SPINE_Y} stroke={C.primary} strokeWidth={3} strokeLinecap="round" />
          <polygon points={`${SPINE_X0},${SPINE_Y - 9} ${SPINE_X0 + 16},${SPINE_Y} ${SPINE_X0},${SPINE_Y + 9}`}
                   fill={C.primary} stroke={C.primary} strokeWidth={2} strokeLinejoin="round" />
          {/* head */}
          <rect x={HEAD.x} y={HEAD.y} width={HEAD.w} height={HEAD.h} rx={10} fill={C.primary} filter={`url(#${SHADOW})`} />
          <text x={HEAD.x + 16} y={HEAD.y + 24} fill={C.onPrimary2} fontSize={11} fontWeight={500} fontFamily={SANS}>
            {t("fishbone.head")}
          </text>
          {headLines.map((ln, k) => (
            <text key={k} x={HEAD.x + 16} y={HEAD.y + 46 + k * 18} fill={C.onPrimary} fontSize={13} fontWeight={600}
                  fontFamily={SANS}>
              {ln}
            </text>
          ))}
          {FISHBONE_TOP.map((d, i) => bone(d, BONE_X[i], true))}
          {FISHBONE_BOTTOM.map((d, i) => bone(d, BONE_X[i], false))}
          {footerLines.map((ln, k) => (
            <text key={k} x={24} y={H - 18 - (footerLines.length - 1 - k) * 16} fill={C.ink3} fontSize={11.5}
                  fontFamily={SANS}>
              {ln}
            </text>
          ))}
        </svg>
      </div>
      <div className="v2-assistant-fb-foot">
        <span className="v2-muted">{t("fishbone.downloadHint")}</span>
        <span className="end">
          <Button size="sm" onClick={() => downloadFishboneSvg(svgRef.current, fishbone)} testId="fishbone-download-svg">
            <Download size={14} aria-hidden="true" />
            {t("v2.assistant.fishbone.downloadSvg")}
          </Button>
          <Button size="sm" onClick={() => downloadFishboneJson(fishbone)} testId="fishbone-download-json">
            <Download size={14} aria-hidden="true" />
            {t("v2.assistant.fishbone.downloadJson")}
          </Button>
        </span>
      </div>
      {rows.length > 0 && (
        <details className="v2-assistant-fb-more" data-testid="fishbone-barriers">
          <summary>
            <ChevronRight size={14} className="chev" aria-hidden="true" />
            {t("v2.assistant.fishbone.allBarriers", { n: rows.length })}
          </summary>
          <Table
            density="dense"
            rows={rows}
            rowKey={(r) => `${r.dim}:${r.sticky_text}`}
            columns={[
              { key: "dim", title: t("fishbone.col.dimension"), width: 150, render: (r) => dimLabel(r.dim) },
              { key: "sticky", title: t("fishbone.col.sticky"), render: (r) => r.sticky_text },
              {
                key: "evidence",
                title: t("fishbone.col.evidence"),
                render: (r) => (
                  <>
                    <span className="v2-muted">{r.evidence}</span>
                    {r.customer_quote ? <span className="sub">“{r.customer_quote}”</span> : null}
                  </>
                ),
              },
              {
                key: "status",
                title: t("fishbone.col.status"),
                width: 170,
                render: (r) => (
                  <span className="v2-tags">
                    <Tag tone={r.confirmed ? "green" : "orange"}>
                      {r.confirmed ? t("fishbone.confirmed") : t("fishbone.suggested")}
                    </Tag>
                    {r.selected && <Tag tone="blue">{t("fishbone.selected")}</Tag>}
                  </span>
                ),
              },
            ]}
          />
        </details>
      )}
      {parking.length > 0 && (
        <details className="v2-assistant-fb-more" data-testid="fishbone-parking-lot">
          <summary>
            <ChevronRight size={14} className="chev" aria-hidden="true" />
            {t("v2.assistant.fishbone.parkingLot", { n: parking.length })}
          </summary>
          <ul>
            {parking.map((p, i) => (
              <li key={i}>
                {p.original}
                {p.converted_to ? <span className="v2-muted"> → {p.converted_to}</span> : null}
              </li>
            ))}
          </ul>
        </details>
      )}
    </div>
  );
}
