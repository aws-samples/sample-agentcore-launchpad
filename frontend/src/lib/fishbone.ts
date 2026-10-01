// Agent-DLC fishbone — the layout and helpers shared by the classic diagram
// (components/FishboneDiagram.tsx) and the V2 one (v2/pages/assistant/Fishbone.tsx).
// Both skins draw the same geometry; only colours, type and chrome differ.
import type { AssistantFishbone, FishboneBarrier, FishboneDimension } from "./api";

export const FB = {
  W: 1200,
  H: 640,
  SPINE_Y: 340,
  SPINE_X0: 60,
  SPINE_X1: 930,
  BONE_X: [300, 580, 860],
  BONE_DX: 60,
  BONE_DY: 215,
  NOTE_W: 200,
  NOTE_H: 56,
  NOTE_T: [0.3, 0.58, 0.86],
  HEAD: { x: 940, y: 292, w: 236, h: 96 },
} as const;

export const FISHBONE_TOP: FishboneDimension[] = ["cognition", "quality", "responsibility"];
export const FISHBONE_BOTTOM: FishboneDimension[] = ["cost", "performance", "other"];

/** Rendered width of one character in em: CJK ≈ 1em, Latin ≈ 0.55em. */
const charUnits = (ch: string) => (/[\u3000-\u9fff\uff00-\uffef]/.test(ch) ? 1 : 0.55);

/** Approximate rendered width of `text` in em. */
export function textUnits(text: string): number {
  let n = 0;
  for (const ch of text) n += charUnits(ch);
  return n;
}

/**
 * Greedy wrap by rendered width: CJK ≈ 1em, Latin ≈ 0.55em. With `keepWords`, a
 * Latin word that would straddle a line break moves whole to the next line.
 */
export function wrapText(text: string, maxUnits: number, maxLines: number, keepWords = false): string[] {
  const lines: string[] = [];
  let cur = "";
  let curUnits = 0;
  let truncated = false;
  for (const ch of text.trim()) {
    const u = charUnits(ch);
    if (curUnits + u > maxUnits && cur) {
      const word = keepWords && /[A-Za-z0-9]/.test(ch) ? /[A-Za-z0-9]+$/.exec(cur)?.[0] ?? "" : "";
      const carry = word.length < cur.length ? word : "";
      const head = cur.slice(0, cur.length - carry.length);
      lines.push(keepWords ? head.trimEnd() : head);
      cur = carry;
      curUnits = textUnits(carry);
      if (lines.length === maxLines) {
        truncated = true;
        break;
      }
    }
    cur += ch;
    curUnits += u;
  }
  if (cur && !truncated) {
    if (lines.length < maxLines) lines.push(cur);
    else truncated = true;
  }
  if (truncated) lines[maxLines - 1] = `${lines[maxLines - 1].slice(0, -1)}…`;
  return lines;
}

/**
 * The notes one bone carries: the selected barriers; else the confirmed ones (a model
 * that confirmed but did not select must not leave the bone blank); else the
 * architect's suggestions still awaiting the customer's yes (drawn dashed).
 */
export function boneNotes(fb: AssistantFishbone, dim: FishboneDimension): FishboneBarrier[] {
  const all = fb.barriers[dim] ?? [];
  const picked = all.filter((b) => b.selected && b.confirmed);
  if (picked.length) return picked.slice(0, 3);
  const confirmed = all.filter((b) => b.confirmed);
  return (confirmed.length ? confirmed : all).slice(0, 3);
}

export const fishboneSlug = (fb: AssistantFishbone) =>
  `${fb.customer}-${fb.date}-fishbone`.replace(/[^\w.-]+/g, "_");

function download(name: string, mime: string, body: string) {
  const blob = new Blob([body], { type: mime });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  a.click();
  URL.revokeObjectURL(url);
}

/** Save the rendered `<svg>` as a standalone file (it must use literal colours). */
export function downloadFishboneSvg(el: SVGSVGElement | null, fb: AssistantFishbone) {
  if (!el) return;
  const markup = el.outerHTML.replace(
    "<svg",
    '<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink"',
  );
  download(`${fishboneSlug(fb)}.svg`, "image/svg+xml;charset=utf-8", `<?xml version="1.0" encoding="UTF-8"?>\n${markup}`);
}

export function downloadFishboneJson(fb: AssistantFishbone) {
  download(`${fishboneSlug(fb)}.json`, "application/json;charset=utf-8", JSON.stringify(fb, null, 2));
}
