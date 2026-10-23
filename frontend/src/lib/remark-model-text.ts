import type { Parent, PhrasingContent, Root, Text } from "mdast";

/** Model-output repairs for chat markdown, applied after parsing.
 *
 * 1. Citation markers. OpenAI models on Bedrock (GPT-6 Sol, …) cite retrieved
 *    sources in-band as `U+E200 cite U+E202 <src> [U+E202 <src>…] U+E201` —
 *    private-use characters that render as tofu boxes ("□cite□a.md□"). A cite
 *    marker becomes an inline-code chip naming its sources; any other marker kind
 *    and stray private-use characters are dropped.
 * 2. CJK emphasis. CommonMark only opens/closes `**` when the run is left/right
 *    flanking, so `**结论：**它…` — a closing `**` after a full-width
 *    punctuation mark and before a CJK letter — stays literal text. A text node
 *    still holding `**…**` after parsing is split into real `strong` nodes.
 *
 * Code (inline and fenced) is never touched: only `text` nodes are rewritten.
 */
const MARKER = /\uE200([a-z_]+)\uE202([^\uE201]*)\uE201/g;
const STRAY = /[\uE200-\uE2FF]/g;
const STRONG = /\*\*([^*\n]+?)\*\*/g;

function citations(value: string): PhrasingContent[] {
  const out: PhrasingContent[] = [];
  let last = 0;
  for (const m of value.matchAll(MARKER)) {
    if (m.index > last) out.push({ type: "text", value: value.slice(last, m.index) });
    const sources = m[2].split("\uE202").map((s) => s.trim()).filter(Boolean);
    if (m[1].includes("cite") && sources.length) {
      out.push({ type: "inlineCode", value: sources.join(" · ") });
    }
    last = m.index + m[0].length;
  }
  if (last < value.length) out.push({ type: "text", value: value.slice(last) });
  return out.map((node) =>
    node.type === "text" ? { ...node, value: node.value.replace(STRAY, "") } : node,
  );
}

function emphasis(node: Text): PhrasingContent[] {
  const out: PhrasingContent[] = [];
  let last = 0;
  for (const m of node.value.matchAll(STRONG)) {
    if (m.index > last) out.push({ type: "text", value: node.value.slice(last, m.index) });
    out.push({ type: "strong", children: [{ type: "text", value: m[1] }] });
    last = m.index + m[0].length;
  }
  if (!out.length) return [node];
  if (last < node.value.length) out.push({ type: "text", value: node.value.slice(last) });
  return out;
}

function rewrite(parent: Parent): void {
  const children: Parent["children"] = [];
  for (const child of parent.children) {
    if (child.type === "text") {
      for (const piece of citations(child.value)) {
        if (piece.type === "text") {
          if (piece.value) children.push(...emphasis(piece));
        } else {
          children.push(piece);
        }
      }
      continue;
    }
    // code / inlineCode carry `value`, not children, so they are never descended into
    if ("children" in child) rewrite(child as Parent);
    children.push(child);
  }
  parent.children = children;
}

export function remarkModelText() {
  return (tree: Root) => {
    rewrite(tree);
  };
}
