#!/usr/bin/env node
// Control inventory for the V2 console (round 16, r16-m).
//
// Walks every V2 page module (`src/v2/pages/**`, plus the shell) with the TypeScript
// compiler API and emits one row per interactive control: where it is (page route,
// `?view=` sub-page, file:line), how a test reaches it (`data-testid`, or a derived
// selector when the control has none), what it is (button / link / tab / toggle /
// row-action / option / dialog-action), what it should do (an `/api` call, a navigation,
// a dialog, or a local state change — inferred from the click handler through the typed
// client in `src/lib/api.ts`), who may see it (the `isAdmin` / `can(...)` / `mayMutate`
// guards around it) and whether it is destructive (delete / publish / retire / …).
//
// The emitted JSON (`src/v2/__inventory__/controls.json`) is a committed snapshot:
// `control-inventory.test.ts` regenerates it in-process and fails on drift, so a new
// control has to be registered by re-running `node scripts/control-inventory.mjs --write`.
// Controls that no test can reach (no testId) or whose effect the generator cannot infer
// are findings in their own right: they must be listed in
// `src/v2/__inventory__/untestable.json` (append-only, each entry names its register row).
//
// No type checker is involved (syntax trees only), so inference is heuristic and
// deliberately conservative: when the handler cannot be followed the effect is `null`
// rather than a guess.
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import ts from "typescript";

const HERE = path.dirname(fileURLToPath(import.meta.url));
export const FRONTEND_ROOT = path.resolve(HERE, "..");
export const INVENTORY_DIR = path.join(FRONTEND_ROOT, "src", "v2", "__inventory__");
export const CONTROLS_JSON = path.join(INVENTORY_DIR, "controls.json");
export const UNTESTABLE_JSON = path.join(INVENTORY_DIR, "untestable.json");

const SK = ts.SyntaxKind;

// JSX tags that are interactive by construction (ui.tsx primitives + raw elements).
const CONTROL_TAGS = new Map([
  ["Button", "button"],
  ["LinkButton", "link"],
  ["OptionCard", "option"],
  ["Segmented", "tab"],
  ["SubTabs", "tab"],
  ["Steps", "tab"],
  ["Select", "toggle"],
  ["FilterSelect", "toggle"],
  ["Switch", "toggle"],
  ["Pager", "button"],
  ["Link", "nav-link"],
  ["NavLink", "nav-link"],
  ["button", "button"],
  ["a", "nav-link"],
  ["summary", "toggle"],
]);
// Dialog primitives (and anything whose tag looks like one) — the "dialog" effect target.
const DIALOG_TAG = /(^(Confirm|Modal|Drawer|ModalFrame|DrawerFrame)$)|Dialog$|Drawer$|Modal$/;
const DESTRUCTIVE_RE =
  /delete|remove|purge|publish|unpublish|retire|rollback|reset|revoke|uninstall|install|stop|disable|promote|redeploy|deploy|approve|reject|signout|sign-out|logout|teardown|clear|detach|unmount|cancel-run|abort|regenerate|rotate|force/i;

/* ───────────────────────────── helpers ───────────────────────────── */

function read(file) {
  return fs.readFileSync(file, "utf8");
}

function parse(file) {
  return ts.createSourceFile(file, read(file), ts.ScriptTarget.ES2022, true, file.endsWith(".tsx") ? ts.ScriptKind.TSX : ts.ScriptKind.TS);
}

function* descendants(node) {
  const stack = [node];
  while (stack.length) {
    const n = stack.pop();
    yield n;
    const kids = [];
    ts.forEachChild(n, (c) => {
      kids.push(c);
    });
    for (let i = kids.length - 1; i >= 0; i--) stack.push(kids[i]);
  }
}

function tagName(node) {
  const tag = ts.isJsxElement(node) ? node.openingElement.tagName : ts.isJsxSelfClosingElement(node) ? node.tagName : null;
  return tag ? tag.getText() : null;
}

function attrsOf(node) {
  const el = ts.isJsxElement(node) ? node.openingElement : node;
  const out = new Map();
  for (const a of el.attributes.properties) {
    if (ts.isJsxAttribute(a)) out.set(a.name.getText(), a.initializer ?? null);
    else if (ts.isJsxSpreadAttribute(a)) out.set(`...${a.expression.getText()}`, a.expression);
  }
  return out;
}

/** The JS expression inside a JSX attribute initializer (or the string literal). */
function attrExpr(init) {
  if (!init) return null;
  if (ts.isStringLiteral(init)) return init;
  if (ts.isJsxExpression(init)) return init.expression ?? null;
  return init;
}

/** Render a string-ish expression to a pattern: template holes become `{…}`. */
function renderString(expr, depth = 0) {
  if (!expr) return null;
  if (ts.isStringLiteral(expr) || ts.isNoSubstitutionTemplateLiteral(expr)) return expr.text;
  if (ts.isTemplateExpression(expr)) {
    let s = expr.head.text;
    for (const span of expr.templateSpans) {
      s += holeFor(span.expression, depth) + span.literal.text;
    }
    return s;
  }
  if (ts.isBinaryExpression(expr) && expr.operatorToken.kind === SK.PlusToken) {
    const l = renderString(expr.left, depth);
    const r = renderString(expr.right, depth);
    if (l !== null && r !== null) return l + r;
  }
  if (ts.isParenthesizedExpression(expr)) return renderString(expr.expression, depth);
  if (ts.isConditionalExpression(expr)) {
    const a = renderString(expr.whenTrue, depth);
    const b = renderString(expr.whenFalse, depth);
    if (a !== null && b !== null) return a === b ? a : `${a}|${b}`;
  }
  if (ts.isCallExpression(expr) && /^(encodeURIComponent|String)$/.test(expr.expression.getText())) return holeFor(expr.arguments[0], depth);
  return null;
}

function holeFor(expr, depth) {
  // `${encodeURIComponent(agentId)}` → `{agentId}`; anything else → `{…}`
  let e = expr;
  while (ts.isCallExpression(e) && /^(encodeURIComponent|String)$/.test(e.expression.getText()) && e.arguments.length) e = e.arguments[0];
  if (ts.isIdentifier(e)) return `{${e.text}}`;
  if (ts.isPropertyAccessExpression(e)) return `{${e.name.text}}`;
  const inner = renderString(e, depth + 1);
  return inner === null ? "{…}" : inner;
}

/* ───────────────────────── api.ts route map ─────────────────────── */

/**
 * `api.listAgents` → `GET /api/agents`; `api.modelProviders.remove` → `DELETE
 * /api/settings/model-providers/{id}`. Resolves the request helpers (`request`,
 * `requestEmpty`, `requestForm`, `fetch`) and the small path-builder helpers
 * (`policyPath(id)`, `modelProviderPath(id, "/credential")`, `MODEL_PROVIDERS_BASE`).
 */
export function buildApiMap(apiFile = path.join(FRONTEND_ROOT, "src", "lib", "api.ts")) {
  const sf = parse(apiFile);
  const consts = new Map(); // name → string constant
  const helpers = new Map(); // name → FunctionDeclaration
  const objects = new Map(); // name → ObjectLiteralExpression
  for (const st of sf.statements) {
    if (ts.isVariableStatement(st)) {
      for (const d of st.declarationList.declarations) {
        if (!ts.isIdentifier(d.name) || !d.initializer) continue;
        if (ts.isObjectLiteralExpression(d.initializer)) objects.set(d.name.text, d.initializer);
        const s = renderString(d.initializer);
        if (s !== null) consts.set(d.name.text, s);
      }
    } else if (ts.isFunctionDeclaration(st) && st.name) {
      helpers.set(st.name.text, st);
    }
  }

  const renderPath = (expr, bindings = new Map()) => {
    if (!expr) return null;
    if (ts.isIdentifier(expr)) {
      if (bindings.has(expr.text)) return bindings.get(expr.text);
      if (consts.has(expr.text)) return consts.get(expr.text);
      return `{${expr.text}}`;
    }
    if (ts.isCallExpression(expr) && ts.isIdentifier(expr.expression) && helpers.has(expr.expression.text)) {
      const fn = helpers.get(expr.expression.text);
      const inner = new Map();
      fn.parameters.forEach((p, i) => {
        const arg = expr.arguments[i];
        const v = arg ? renderPath(arg, bindings) : p.initializer ? renderPath(p.initializer, bindings) : "";
        inner.set(p.name.getText(), v ?? `{${p.name.getText()}}`);
      });
      const ret = [...descendants(fn.body)].find((n) => ts.isReturnStatement(n) && n.expression);
      return ret ? renderPath(ret.expression, inner) : null;
    }
    if (ts.isTemplateExpression(expr)) {
      let s = expr.head.text;
      for (const span of expr.templateSpans) {
        const v = renderPath(span.expression, bindings);
        s += (v === null ? "{…}" : v.startsWith("{") || v.startsWith("/") || v === "" ? v : v) + span.literal.text;
      }
      return s;
    }
    if (ts.isCallExpression(expr) && /^(encodeURIComponent|String)$/.test(expr.expression.getText())) {
      return renderPath(expr.arguments[0], bindings);
    }
    if (ts.isPropertyAccessExpression(expr)) return `{${expr.name.text}}`;
    if (ts.isBinaryExpression(expr) && expr.operatorToken.kind === SK.PlusToken) {
      const l = renderPath(expr.left, bindings);
      const r = renderPath(expr.right, bindings);
      return l === null || r === null ? null : l + r;
    }
    if (ts.isParenthesizedExpression(expr)) return renderPath(expr.expression, bindings);
    if (ts.isConditionalExpression(expr)) {
      const a = renderPath(expr.whenTrue, bindings);
      const b = renderPath(expr.whenFalse, bindings);
      return a === b ? a : `${a}|${b}`;
    }
    const s = renderString(expr);
    return s;
  };

  const routeOfBody = (fnLike) => {
    // first request-ish call inside the arrow / method body
    for (const n of descendants(fnLike)) {
      if (!ts.isCallExpression(n)) continue;
      const callee = n.expression.getText();
      const m = callee.match(/^(request|requestEmpty|requestForm|getJson|fetch|streamRequest|sseRequest)\b/);
      if (!m) continue;
      let method = m[1] === "requestForm" ? "POST" : "GET";
      const pathArg = n.arguments[0];
      const init = n.arguments[1];
      if (init && ts.isObjectLiteralExpression(init)) {
        for (const p of init.properties) {
          if (ts.isPropertyAssignment(p) && p.name.getText() === "method") {
            const v = renderString(p.initializer);
            if (v) method = v.toUpperCase();
          }
        }
      }
      const rendered = renderPath(pathArg);
      if (rendered === null) continue;
      return `${method} ${rendered.split("?")[0]}`;
    }
    // a method that delegates to another api method: `foo: (id) => bar(id)`
    return null;
  };

  const map = new Map();
  const addObject = (prefix, obj) => {
    for (const p of obj.properties) {
      const key = p.name?.getText();
      if (!key) continue;
      if (ts.isPropertyAssignment(p)) {
        if (ts.isIdentifier(p.initializer) && objects.has(p.initializer.text)) {
          addObject(`${prefix}${key}.`, objects.get(p.initializer.text));
          continue;
        }
        const r = routeOfBody(p.initializer);
        if (r) map.set(`${prefix}${key}`, r);
        else map.set(`${prefix}${key}`, null);
      } else if (ts.isMethodDeclaration(p)) {
        const r = routeOfBody(p);
        map.set(`${prefix}${key}`, r ?? null);
      } else if (ts.isShorthandPropertyAssignment(p)) {
        if (objects.has(key)) addObject(`${prefix}${key}.`, objects.get(key));
      }
    }
  };
  for (const [name, obj] of objects) {
    if (name === "api") addObject("api.", obj);
    else if (/Api$/.test(name)) addObject(`${name}.`, obj);
  }
  // second pass: unresolved entries that delegate to a resolved sibling (`x: (...) => api.y(...)`)
  for (const [k, v] of map) {
    if (v !== null) continue;
    const obj = objects.get(k.split(".")[0]) ?? objects.get("api");
    if (!obj) continue;
  }
  return map;
}

/* ───────────────────────── route map (App.tsx) ─────────────────────── */

export function buildRouteMap(appFile = path.join(FRONTEND_ROOT, "src", "App.tsx")) {
  const sf = parse(appFile);
  const imports = new Map(); // local name → module specifier
  for (const st of sf.statements) {
    if (ts.isImportDeclaration(st) && st.importClause) {
      const spec = st.moduleSpecifier.text;
      if (st.importClause.name) imports.set(st.importClause.name.text, spec);
      const nb = st.importClause.namedBindings;
      if (nb && ts.isNamedImports(nb)) for (const e of nb.elements) imports.set(e.name.text, spec);
    }
    // lazy(() => import("./v2/pages/Agents").then(m => ({ default: m.V2Agents })))
    if (ts.isVariableStatement(st)) {
      for (const d of st.declarationList.declarations) {
        if (!d.initializer || !ts.isIdentifier(d.name)) continue;
        const txt = d.initializer.getText();
        const m = txt.match(/import\(\s*["']([^"']+)["']\s*\)/);
        if (m) imports.set(d.name.text, m[1]);
      }
    }
  }
  const routes = new Map(); // component local name → route path
  const visit = (node, prefix) => {
    if (ts.isJsxElement(node) || ts.isJsxSelfClosingElement(node)) {
      if (tagName(node) === "Route") {
        const attrs = attrsOf(node);
        const p = attrExpr(attrs.get("path"));
        const pathText = p ? renderString(p) : attrs.has("index") ? "" : null;
        const full = pathText === null ? prefix : pathText.startsWith("/") ? pathText : [prefix, pathText].filter((s) => s !== undefined).join("/").replace(/\/+/g, "/");
        const el = attrExpr(attrs.get("element"));
        if (el && (ts.isJsxSelfClosingElement(el) || ts.isJsxElement(el))) {
          const comp = tagName(el);
          if (comp && !routes.has(comp)) routes.set(comp, full === "" ? "/" : full.startsWith("/") ? full : `/${full}`);
        }
        if (ts.isJsxElement(node)) for (const c of node.children) visit(c, full);
        return;
      }
    }
    ts.forEachChild(node, (c) => visit(c, prefix));
  };
  visit(sf, "");
  // component → file
  const files = new Map();
  for (const [comp, route] of routes) {
    const spec = imports.get(comp);
    if (!spec) continue;
    files.set(resolveModule(appFile, spec), route);
  }
  return files; // absolute file → route
}

function resolveModule(fromFile, spec) {
  const base = path.resolve(path.dirname(fromFile), spec);
  for (const cand of [base, `${base}.tsx`, `${base}.ts`, path.join(base, "index.tsx"), path.join(base, "index.ts")]) {
    if (fs.existsSync(cand) && fs.statSync(cand).isFile()) return cand;
  }
  return base;
}

/* ─────────────────────── import graph of src/v2 ────────────────────── */

function listTsx(dir) {
  const out = [];
  for (const e of fs.readdirSync(dir, { withFileTypes: true })) {
    const p = path.join(dir, e.name);
    if (e.isDirectory()) {
      if (e.name === "__inventory__") continue;
      out.push(...listTsx(p));
    } else if (/\.tsx?$/.test(e.name) && !/\.test\.tsx?$/.test(e.name) && !/\.d\.ts$/.test(e.name)) out.push(p);
  }
  return out.sort();
}

function importsOf(sf) {
  const out = new Map(); // local name → {file, exported}
  const files = new Set();
  for (const st of sf.statements) {
    if (!ts.isImportDeclaration(st) || !st.importClause) continue;
    const spec = st.moduleSpecifier.text;
    if (!spec.startsWith(".")) continue;
    const file = resolveModule(sf.fileName, spec);
    files.add(file);
    if (st.importClause.name) out.set(st.importClause.name.text, { file, exported: "default" });
    const nb = st.importClause.namedBindings;
    if (nb && ts.isNamedImports(nb)) for (const e of nb.elements) out.set(e.name.text, { file, exported: (e.propertyName ?? e.name).text });
  }
  return { names: out, files };
}

/* ─────────────────────────── per-file analysis ─────────────────────── */

class FileCtx {
  constructor(file, sf, imports) {
    this.file = file;
    this.sf = sf;
    this.imports = imports;
    this.decls = new Map(); // top-level + function-scoped declarations by name → node
    this.setters = new Map(); // setter → state var
    this.dialogs = []; // {testId, tag, line, openVars:Set}
    this.collect();
  }
  collect() {
    for (const n of descendants(this.sf)) {
      if (ts.isVariableDeclaration(n) && n.initializer) {
        if (ts.isIdentifier(n.name)) this.decls.set(n.name.text, n.initializer);
        else if (ts.isArrayBindingPattern(n.name) && n.name.elements.length >= 2) {
          const [state, setter] = n.name.elements;
          const init = n.initializer;
          if (ts.isCallExpression(init) && /^(useState|useReducer)$/.test(init.expression.getText()) && ts.isBindingElement(state) && ts.isBindingElement(setter)) {
            this.setters.set(setter.name.getText(), state.name.getText());
          }
        }
      } else if (ts.isFunctionDeclaration(n) && n.name) {
        this.decls.set(n.name.text, n);
      }
      if ((ts.isJsxElement(n) || ts.isJsxSelfClosingElement(n)) && DIALOG_TAG.test(tagName(n) ?? "")) {
        const attrs = attrsOf(n);
        const tid = attrs.has("testId") ? renderString(attrExpr(attrs.get("testId"))) : attrs.has("data-testid") ? renderString(attrExpr(attrs.get("data-testid"))) : null;
        const tag = tagName(n);
        const openVars = new Set();
        const openE = attrExpr(attrs.get("open"));
        if (openE) for (const d of descendants(openE)) if (ts.isIdentifier(d)) openVars.add(d.text);
        // `{foo && <Dialog/>}` / `foo ? <Dialog/> : null`
        let p = n.parent;
        while (p && !ts.isReturnStatement(p) && !ts.isArrowFunction(p) && !ts.isFunctionDeclaration(p)) {
          if (ts.isBinaryExpression(p) && p.operatorToken.kind === SK.AmpersandAmpersandToken) for (const d of descendants(p.left)) if (ts.isIdentifier(d)) openVars.add(d.text);
          if (ts.isConditionalExpression(p)) for (const d of descendants(p.condition)) if (ts.isIdentifier(d)) openVars.add(d.text);
          p = p.parent;
        }
        this.dialogs.push({ testId: tid ?? (tag === "Confirm" ? "v2-confirm" : null), tag, line: this.line(n), openVars });
      }
    }
  }
  line(node) {
    return this.sf.getLineAndCharacterOfPosition(node.getStart(this.sf)).line + 1;
  }
}

const fileCache = new Map();
function ctxFor(file) {
  if (!fileCache.has(file)) {
    if (!fs.existsSync(file)) return null;
    const sf = parse(file);
    fileCache.set(file, new FileCtx(file, sf, importsOf(sf)));
  }
  return fileCache.get(file);
}

/** Find the exported declaration `name` in `file` (function / const). */
function exportedDecl(file, name) {
  const ctx = ctxFor(file);
  if (!ctx) return null;
  const d = ctx.decls.get(name);
  return d ? { node: d, ctx } : null;
}

/**
 * Collect effects from a handler expression. Follows identifiers to their in-file
 * declaration (and through relative imports), member calls on objects returned by
 * in-file hooks (`actions.askDelete` → `useAgentActions()` → returned literal), up to
 * `depth` hops. Returns an ordered, de-duplicated effect list.
 */
function effectsOf(expr, ctx, apiMap, pageRoute, depth = 0, seen = new Set()) {
  const out = [];
  if (!expr || depth > 4) return out;
  const emitted = new Set(); // per-call dedupe; `seen` only tracks visited declarations
  const push = (e) => {
    const k = JSON.stringify(e);
    if (!emitted.has(k)) {
      emitted.add(k);
      out.push(e);
    }
  };
  const bodyOf = (fn) => {
    if (ts.isArrowFunction(fn) || ts.isFunctionExpression(fn) || ts.isFunctionDeclaration(fn) || ts.isMethodDeclaration(fn)) return fn.body ?? null;
    return null;
  };
  // `useCallback(() => …, [])`, `useMemo`, `async () => …`, `void run()`
  let e = expr;
  while (ts.isParenthesizedExpression(e) || ts.isAsExpression(e) || ts.isNonNullExpression(e) || ts.isAwaitExpression(e) || (ts.isVoidExpression(e))) e = e.expression;
  if (ts.isCallExpression(e) && /^(useCallback|useMemo)$/.test(e.expression.getText())) e = e.arguments[0];

  const analyzeBody = (body, bctx) => {
    if (!body) return;
    for (const n of descendants(body)) {
      // nested function definitions that are *not* invoked are still followed (effects queue)
      if (ts.isCallExpression(n)) {
        const calleeText = n.expression.getText().replace(/\s+/g, "").replace(/\?\./g, ".");
        // `fileRef.current?.click()` — a hidden <input type=file> picker
        if (/\.click$/.test(calleeText) && /ref|input|picker|file/i.test(calleeText)) {
          push({ type: "local", detail: "file-picker" });
          continue;
        }
        // api.X(...) / api.a.b(...) / chatApi.x(...)
        const apiM = calleeText.match(/^((?:api|[a-zA-Z]+Api)(?:\.[a-zA-Z0-9_]+)+)$/);
        if (apiM) {
          const key = apiM[1];
          const route = apiMap.get(key);
          push({ type: "api", detail: route ?? `unresolved:${key}` });
          continue;
        }
        if (/^(navigate)$/.test(calleeText)) {
          const arg = n.arguments[0];
          const s = renderString(arg) ?? (arg && ts.isCallExpression(arg) ? helperString(arg, bctx) : null);
          if (s !== null) push({ type: "navigate", detail: s });
          else if (arg && ts.isNumericLiteral(arg) || (arg && ts.isPrefixUnaryExpression(arg))) push({ type: "navigate", detail: "history:back" });
          else if (arg && ts.isObjectLiteralExpression(arg)) push({ type: "navigate", detail: objectNav(arg, pageRoute) });
          else push({ type: "navigate", detail: "{…}" });
          continue;
        }
        if (/^(setParams|setSearchParams|setView|setQuery)$/.test(calleeText)) {
          const arg = n.arguments[0];
          push({ type: "navigate", detail: arg ? objectNav(arg, pageRoute) : `${pageRoute}?{…}` });
          continue;
        }
        if (/^(window\.open|location\.assign|window\.location\.assign)$/.test(calleeText)) {
          push({ type: "navigate", detail: renderString(n.arguments[0]) ?? "{external}" });
          continue;
        }
        if (/^(fetch)$/.test(calleeText)) {
          push({ type: "api", detail: `FETCH ${renderString(n.arguments[0]) ?? "{…}"}` });
          continue;
        }
        if (/^(navigator\.clipboard\.writeText|copy|copyText|download|saveAs|URL\.createObjectURL|window\.print)$/.test(calleeText)) {
          push({ type: "local", detail: calleeText });
          continue;
        }
        if (/^(e|ev|event)\.(preventDefault|stopPropagation)$/.test(calleeText)) continue;
        if (/^(toast|t|console\.[a-z]+|String|Number|JSON\.[a-z]+|Object\.[a-z]+|Array\.[a-z]+|Math\.[a-z]+|Promise\.[a-z]+|setTimeout|clearTimeout|requestAnimationFrame|void)$/.test(calleeText)) continue;
        if (ts.isIdentifier(n.expression)) {
          const name = n.expression.text;
          // `void logout()` — the auth context's sign-out is `POST /api/auth/logout`
          if (/^(logout|signOut)$/.test(name)) {
            push({ type: "api", detail: apiMap.get("api.logout") ?? "POST /api/auth/logout" });
            continue;
          }
          // state setter → dialog or local
          if (bctx.setters.has(name)) {
            const stateVar = bctx.setters.get(name);
            const dlg = bctx.dialogs.find((d) => d.openVars.has(stateVar));
            const arg = n.arguments[0];
            const closing = arg && (arg.kind === SK.FalseKeyword || arg.kind === SK.NullKeyword || (ts.isIdentifier(arg) && arg.text === "undefined"));
            if (dlg && !closing) push({ type: "dialog", detail: dlg.testId ?? `${dlg.tag}@${path.basename(bctx.file)}:${dlg.line}` });
            else push({ type: "local", detail: `${name}(${closing ? "close" : arg ? "…" : ""})` });
            continue;
          }
          // in-file function / const handler
          const d = bctx.decls.get(name);
          if (d && !seen.has(`decl:${bctx.file}:${name}`)) {
            seen.add(`decl:${bctx.file}:${name}`);
            for (const eff of effectsOf(d, bctx, apiMap, pageRoute, depth + 1, seen)) push(eff);
            continue;
          }
          // imported helper (relative) — follow one hop
          const imp = bctx.imports.names.get(name);
          if (imp && !/\/lib\/api$/.test(imp.file)) {
            const target = exportedDecl(imp.file, imp.exported);
            if (target && !seen.has(`decl:${imp.file}:${imp.exported}`)) {
              seen.add(`decl:${imp.file}:${imp.exported}`);
              for (const eff of effectsOf(target.node, target.ctx, apiMap, pageRoute, depth + 1, seen)) push(eff);
              continue;
            }
          }
          // a prop callback (onClose, onBack, onChange, reload, …) — the parent's concern
          if (/^(on[A-Z]\w*|reload|refresh|refetch|retry|close|open|back|next|dismiss|toggle|select|load|run|submit|save|apply|reset|start|stop|cancel|confirm|ask\w*|set\w*|patch\w*|change\w*|mark\w*|clear\w*|add\w*|remove\w*|update\w*|choose\w*|pick\w*|go\w*|show\w*|hide\w*|begin\w*|finish\w*|end\w*|copy\w*|move\w*|swap\w*|edit\w*|insert\w*|delete\w*|replace\w*)$/.test(name)) {
            push({ type: "local", detail: `callback:${name}` });
            continue;
          }
          if (imp) push({ type: "local", detail: `call:${name}` });
          continue;
        }
        // member call: actions.askDelete(...) → hook returning object
        if (ts.isPropertyAccessExpression(n.expression) && ts.isIdentifier(n.expression.expression)) {
          const objName = n.expression.expression.text;
          const prop = n.expression.name.text;
          const objInit = bctx.decls.get(objName);
          if (objInit && ts.isCallExpression(objInit) && ts.isIdentifier(objInit.expression)) {
            const hook = objInit.expression.text;
            const hookDecl = bctx.decls.get(hook) ?? (bctx.imports.names.get(hook) ? exportedDecl(bctx.imports.names.get(hook).file, bctx.imports.names.get(hook).exported) : null);
            const hookNode = hookDecl?.node ?? hookDecl;
            const hookCtx = hookDecl?.ctx ?? bctx;
            if (hookNode && !seen.has(`hook:${hook}.${prop}`)) {
              seen.add(`hook:${hook}.${prop}`);
              const propNode = returnedProperty(hookNode, prop);
              if (propNode) {
                for (const eff of effectsOf(propNode, hookCtx, apiMap, pageRoute, depth + 1, seen)) push(eff);
                continue;
              }
            }
          }
          // props.onX(...) / row.action(...)
          if (/^(props|p|o|opts|options|row|item|r|a|s)$/.test(objName) || /^on[A-Z]/.test(prop)) {
            push({ type: "local", detail: `callback:${objName}.${prop}` });
            continue;
          }
          if (/^(navigator|document|window|localStorage|sessionStorage|URL|history)$/.test(objName)) {
            push({ type: "local", detail: calleeText });
            continue;
          }
          if (/^(e|ev|event)$/.test(objName)) continue;
          if (/^(i18n|JSON|Object|Array|Promise|Date|Math|Number|String|console|Boolean|Symbol|Intl|Reflect)$/.test(objName)) continue;
          if (/\.(map|filter|find|findIndex|some|every|includes|indexOf|join|split|trim|slice|concat|reduce|has|get|set|add|delete|keys|values|entries|forEach|sort|flat|flatMap|toLowerCase|toUpperCase|startsWith|endsWith|replace|match|test|push|pop|shift|unshift|splice|toString|toISOString|localeCompare|padStart|padEnd|at|fill|from|of|then|catch|finally)$/.test(calleeText)) continue;
          push({ type: "local", detail: `call:${calleeText}` });
          continue;
        }
      }
    }
  };

  if (ts.isIdentifier(e)) {
    const d = ctx.decls.get(e.text);
    if (d && !seen.has(`decl:${ctx.file}:${e.text}`)) {
      seen.add(`decl:${ctx.file}:${e.text}`);
      return effectsOf(d, ctx, apiMap, pageRoute, depth + 1, seen);
    }
    if (ctx.setters.has(e.text)) {
      // onClick={setOpen} is rare; treat like a setter call with unknown arg
      const stateVar = ctx.setters.get(e.text);
      const dlg = ctx.dialogs.find((x) => x.openVars.has(stateVar));
      return [dlg ? { type: "dialog", detail: dlg.testId ?? dlg.tag } : { type: "local", detail: `${e.text}(…)` }];
    }
    const imp = ctx.imports.names.get(e.text);
    if (imp && !/\/lib\/api$/.test(imp.file)) {
      const target = exportedDecl(imp.file, imp.exported);
      if (target) return effectsOf(target.node, target.ctx, apiMap, pageRoute, depth + 1, seen);
    }
    if (/^(on[A-Z]\w*|reload|refresh|close|onClose|back|retry|toggle)$/.test(e.text)) return [{ type: "local", detail: `callback:${e.text}` }];
    return [{ type: "local", detail: `call:${e.text}` }];
  }
  if (ts.isPropertyAccessExpression(e)) {
    // onClick={actions.askDelete} / onClick={props.onBack}
    const objName = ts.isIdentifier(e.expression) ? e.expression.text : null;
    const prop = e.name.text;
    if (objName) {
      const objInit = ctx.decls.get(objName);
      if (objInit && ts.isCallExpression(objInit) && ts.isIdentifier(objInit.expression)) {
        const hook = objInit.expression.text;
        const hookDecl = ctx.decls.get(hook);
        if (hookDecl) {
          const propNode = returnedProperty(hookDecl, prop);
          if (propNode) return effectsOf(propNode, ctx, apiMap, pageRoute, depth + 1, seen);
        }
      }
    }
    return [{ type: "local", detail: `callback:${e.getText()}` }];
  }
  const body = bodyOf(e);
  if (body) {
    // a block body or an expression body: descendants() walks either tree from its root
    analyzeBody(body, ctx);
    return out;
  }
  if (ts.isCallExpression(e) || ts.isBinaryExpression(e) || ts.isConditionalExpression(e) || ts.isObjectLiteralExpression(e)) {
    analyzeBody(e, ctx);
    return out;
  }
  return out;
}

/** `editPath(a)` → the in-file helper's returned string pattern (`/agents/{id}/edit`). */
function helperString(call, ctx) {
  if (!ts.isIdentifier(call.expression)) return null;
  const d = ctx.decls.get(call.expression.text);
  if (!d) return null;
  const fn = ts.isFunctionDeclaration(d) || ts.isArrowFunction(d) || ts.isFunctionExpression(d) ? d : null;
  if (!fn || !fn.body) return null;
  if (!ts.isBlock(fn.body)) return renderString(fn.body);
  for (const n of descendants(fn.body)) {
    if (ts.isReturnStatement(n) && n.expression) {
      const s = renderString(n.expression);
      if (s !== null) return s;
    }
  }
  return null;
}

/** `return { askDelete: (a) => …, dialog }` → the initializer of `prop`. */
function returnedProperty(fnNode, prop) {
  const body = fnNode.body ?? (ts.isVariableDeclaration(fnNode) ? fnNode.initializer?.body : null) ?? (ts.isArrowFunction(fnNode) ? fnNode.body : null);
  const root = body ?? fnNode;
  for (const n of descendants(root)) {
    if (ts.isReturnStatement(n) && n.expression && ts.isObjectLiteralExpression(n.expression)) {
      for (const p of n.expression.properties) {
        if (p.name?.getText() === prop) {
          if (ts.isPropertyAssignment(p)) return p.initializer;
          if (ts.isShorthandPropertyAssignment(p)) return ts.factory.createIdentifier(prop);
          if (ts.isMethodDeclaration(p)) return p;
        }
      }
    }
    if (ts.isObjectLiteralExpression(n) && !ts.isBlock(root)) {
      for (const p of n.properties) if (p.name?.getText() === prop && ts.isPropertyAssignment(p)) return p.initializer;
    }
  }
  return null;
}

/** `setParams({ view: "new", id: a.id })` → `/v2/agents?view=new&id={…}`. */
function objectNav(arg, pageRoute) {
  if (ts.isObjectLiteralExpression(arg)) {
    const parts = [];
    for (const p of arg.properties) {
      if (!ts.isPropertyAssignment(p)) {
        if (ts.isShorthandPropertyAssignment(p)) parts.push(`${p.name.text}={${p.name.text}}`);
        continue;
      }
      const k = p.name.getText().replace(/^["']|["']$/g, "");
      const v = renderString(p.initializer);
      parts.push(`${k}=${v === null ? "{…}" : v}`);
    }
    return `${pageRoute}?${parts.join("&")}`;
  }
  if (ts.isArrowFunction(arg) || ts.isFunctionExpression(arg)) return `${pageRoute}?{fn}`;
  const s = renderString(arg);
  return s === null ? `${pageRoute}?{…}` : `${pageRoute}?${s}`;
}

/* ───────────────────────────── guards (roles) ─────────────────────── */

function guardsAround(node, ctx) {
  const guards = new Set();
  const scan = (expr) => {
    if (!expr) return;
    for (const d of descendants(expr)) {
      if (ts.isIdentifier(d) && d.text === "isAdmin") guards.add("admin");
      if (ts.isCallExpression(d)) {
        const c = d.expression.getText();
        if (/(^|\.)can$/.test(c)) guards.add(`perm:${renderString(d.arguments[0]) ?? "…"}`);
        if (/mayMutate$/.test(c)) guards.add("creator");
        if (/creatorOnly$|canEdit$|canDelete$|canMutate$|owns$/.test(c)) guards.add("creator");
      }
      if (ts.isPropertyAccessExpression(d) && /^(isAdmin|admin)$/.test(d.name.text)) guards.add("admin");
    }
  };
  let p = node.parent;
  while (p && !ts.isSourceFile(p)) {
    if (ts.isBinaryExpression(p) && (p.operatorToken.kind === SK.AmpersandAmpersandToken || p.operatorToken.kind === SK.QuestionQuestionToken)) scan(p.left);
    if (ts.isConditionalExpression(p)) scan(p.condition);
    if (ts.isIfStatement(p)) scan(p.expression);
    p = p.parent;
  }
  // `disabled={!can("x")}` / `disabled={!isAdmin}` on the control itself
  const attrs = attrsOf(node);
  const dis = attrExpr(attrs.get("disabled"));
  if (dis) scan(dis);
  // early-return guards in the enclosing component(s): `if (!isAdmin) return <Forbidden/>` —
  // not an ancestor of the control, but every control after it is admin-only
  let fn = node.parent;
  while (fn && !ts.isSourceFile(fn)) {
    if ((ts.isFunctionDeclaration(fn) || ts.isArrowFunction(fn) || ts.isFunctionExpression(fn)) && fn.body && ts.isBlock(fn.body)) {
      for (const st of fn.body.statements) {
        if (st.pos > node.pos) break;
        if (ts.isIfStatement(st) && /(!\s*(auth\.)?isAdmin\b|isAdmin\s*===\s*false|role\s*!==\s*"admin")/.test(st.expression.getText()) && /\breturn\b/.test(st.thenStatement.getText())) guards.add("admin");
        if (ts.isIfStatement(st) && /(^|[^!])\b(auth\.)?isAdmin\b/.test(st.expression.getText()) && /\breturn\b/.test(st.thenStatement.getText()) && !/!\s*(auth\.)?isAdmin/.test(st.expression.getText())) guards.add("member-only-branch");
      }
    }
    fn = fn.parent;
  }
  void ctx;
  return [...guards].sort();
}

function rolesFromGuards(guards, adminPage) {
  if (adminPage || guards.includes("admin")) return ["admin"];
  const perm = guards.find((g) => g.startsWith("perm:"));
  if (perm) return ["admin", `member+${perm}`];
  if (guards.includes("creator")) return ["admin", "member(creator)"];
  return ["admin", "member"];
}

/* ───────────────────────────── label ─────────────────────────────── */

function labelOf(node, attrs) {
  // children: t("key") / literal text / <Icon/> only
  const texts = [];
  const scanExpr = (e) => {
    if (!e) return;
    for (const d of descendants(e)) {
      if (ts.isCallExpression(d) && d.expression.getText() === "t" && d.arguments[0]) {
        const k = renderString(d.arguments[0]);
        if (k) texts.push(`t:${k}`);
      }
    }
  };
  if (ts.isJsxElement(node)) {
    for (const c of node.children) {
      if (ts.isJsxText(c)) {
        const s = c.text.trim();
        if (s) texts.push(s);
      } else if (ts.isJsxExpression(c) && c.expression) {
        scanExpr(c.expression);
        if (!texts.length && (ts.isIdentifier(c.expression) || ts.isPropertyAccessExpression(c.expression))) texts.push(`{${c.expression.getText()}}`);
      }
    }
  }
  if (!texts.length) {
    for (const k of ["label", "title", "aria-label", "confirmLabel", "placeholder"]) {
      const e = attrExpr(attrs.get(k));
      if (e) {
        scanExpr(e);
        const s = renderString(e);
        if (!texts.length && s) texts.push(s);
        if (texts.length) break;
      }
    }
  }
  return texts[0] ?? null;
}

/* ───────────────────────────── generate ──────────────────────────── */

export function generate(root = FRONTEND_ROOT) {
  fileCache.clear();
  const apiMap = buildApiMap(path.join(root, "src", "lib", "api.ts"));
  const routeByFile = buildRouteMap(path.join(root, "src", "App.tsx"));
  const v2 = path.join(root, "src", "v2");
  const pagesDir = path.join(v2, "pages");
  const files = [...listTsx(pagesDir), path.join(v2, "V2Shell.tsx"), path.join(v2, "NewVersionBanner.tsx"), path.join(v2, "ResultsView.tsx"), path.join(v2, "EvaluatorPicker.tsx"), path.join(v2, "InsightsPanel.tsx"), path.join(v2, "InsightClusters.tsx"), path.join(v2, "RunCoverage.tsx")].filter((f) => fs.existsSync(f));

  // owning page per file: BFS over relative imports from each routed page file
  const owners = new Map(); // file → {route, view}
  const adminPages = new Set();
  {
    const navSrc = read(path.join(v2, "nav.ts"));
    for (const m of navSrc.matchAll(/to:\s*"([^"]+)"[^}]*admin:\s*true/g)) adminPages.add(m[1]);
  }
  const pageEntries = [...routeByFile.entries()].filter(([f]) => f.startsWith(pagesDir) || f.startsWith(v2));
  // a routed page whose component bails out for non-admins (`if (!isAdmin) return <Forbidden/>`)
  // is admin-only as a whole, even when nav.ts does not flag it (Announcements, video management)
  for (const [pageFile, route] of pageEntries) {
    const pctx = ctxFor(pageFile);
    if (!pctx) continue;
    for (const n of descendants(pctx.sf)) {
      if (ts.isIfStatement(n) && /!\s*(auth\.)?isAdmin\b/.test(n.expression.getText()) && /\breturn\b/.test(n.thenStatement.getText())) {
        const comp = enclosingComponent(n);
        // only when the guard sits in the routed component itself (not in some sub-tab)
        if (comp && new RegExp(`\\b${comp}\\b`).test(fs.readFileSync(path.join(root, "src", "App.tsx"), "utf8"))) adminPages.add(route);
      }
    }
  }
  for (const [pageFile, route] of pageEntries) {
    const queue = [[pageFile, null, null]];
    const seen = new Set();
    while (queue.length) {
      const [f, view, tab] = queue.shift();
      if (seen.has(f) || !fs.existsSync(f)) continue;
      seen.add(f);
      if (!owners.has(f)) owners.set(f, { route, view, tab });
      const ctx = ctxFor(f);
      if (!ctx) continue;
      // `if (view === "new") return <AgentWizard/>` → the component's file gets that view
      const viewOf = viewDispatch(ctx);
      const tabOf = viewDispatch(ctx, "tab");
      for (const [local, { file, exported }] of ctx.imports.names) {
        if (!file.startsWith(v2) || /\/lib\//.test(file)) continue;
        // several components of one file may be dispatched under different views / tabs
        // (`DatasetsTab` on tab=datasets, `DatasetDetail` on view=dataset): remember them per
        // exported component, and give the file the first non-null view / tab as its default
        const cv = viewOf.get(local) ?? null;
        const ct = tabOf.get(local) ?? null;
        const own = owners.get(file) ?? { route, view: null, tab: null, components: new Map() };
        own.components ??= new Map();
        if (!own.components.has(exported)) own.components.set(exported, { view: cv ?? view, tab: ct ?? tab });
        if (own.route === route) {
          own.view ??= cv ?? view;
          own.tab ??= ct ?? tab;
        }
        owners.set(file, own);
        if (seen.has(file)) continue;
        queue.push([file, cv ?? view, ct ?? tab]);
      }
    }
  }
  owners.set(path.join(v2, "V2Shell.tsx"), { route: "/v2/*", view: null, tab: null });
  // files nobody routed to directly (shared widgets): inherit the owner of the first file importing them
  for (let pass = 0; pass < 3; pass++) {
    for (const f of files) {
      if (owners.has(f)) continue;
      for (const [of, own] of owners) {
        const octx = ctxFor(of);
        if (octx && octx.imports.files.has(f)) {
          owners.set(f, own);
          break;
        }
      }
    }
  }

  const rows = [];
  for (const file of files) {
    const ctx = ctxFor(file);
    if (!ctx) continue;
    const owner = owners.get(file) ?? { route: "(unrouted)", view: null, tab: null };
    const rel = path.relative(root, file).replace(/\\/g, "/");
    // components this file dispatches per `?view=` (`if (view === "edit") return <EvaluatorEditor/>`),
    // closed over the in-file components they render, so a control inside the editor's sub-form
    // is attributed to `view=edit` and the crawler opens that URL
    const viewOfComponent = inFileViews(ctx);
    const tabOfComponent = inFileViews(ctx, "tab");
    let ordinal = 0;
    for (const n of descendants(ctx.sf)) {
      if (!ts.isJsxElement(n) && !ts.isJsxSelfClosingElement(n)) continue;
      const tag = tagName(n);
      if (!tag) continue;
      const enclosing = enclosingComponent(n);
      const fromParent = enclosing ? owner.components?.get(enclosing) : null;
      // a component the parent dispatches explicitly keeps the parent's (possibly null) view / tab;
      // only components nobody dispatches inherit the file default
      const viewHere = enclosing && viewOfComponent.has(enclosing) ? viewOfComponent.get(enclosing) : fromParent ? fromParent.view : owner.view;
      const tabHere = enclosing && tabOfComponent.has(enclosing) ? tabOfComponent.get(enclosing) : fromParent ? fromParent.tab : owner.tab;
      const attrs = attrsOf(n);
      const isControl = CONTROL_TAGS.has(tag) || attrs.has("onClick") || attrs.has("onConfirm") || (tag === "input" && /checkbox|radio|file/.test(renderString(attrExpr(attrs.get("type"))) ?? "")) || (tag === "Confirm");
      if (!isControl || tag === "details") continue;
      // raw <a>/<button> inside ui.tsx primitives are not re-scanned (ui.tsx is not in `files`)
      let kind = CONTROL_TAGS.get(tag) ?? (tag === "Confirm" ? "dialog-action" : tag === "input" ? "toggle" : "clickable");
      // row action: rendered inside a table column `render:` / `actions:` callback or a `.map(` over rows
      let p = n.parent;
      let inRender = false;
      while (p && !ts.isSourceFile(p)) {
        if (ts.isPropertyAssignment(p) && /^(render|actions|cell|renderRow|rowActions)$/.test(p.name.getText())) inRender = true;
        if (ts.isCallExpression(p) && /\.map$/.test(p.expression.getText())) inRender = true;
        p = p.parent;
      }
      if (inRender && (kind === "link" || kind === "button")) kind = "row-action";
      if (tag === "Link" && /^(v2-btn|btn)/.test(renderString(attrExpr(attrs.get("className"))) ?? "")) kind = "button";

      const tidE = attrExpr(attrs.get("testId")) ?? attrExpr(attrs.get("data-testid"));
      const testId = tidE ? renderString(tidE) : null;
      const label = labelOf(n, attrs);
      const line = ctx.line(n);
      ordinal += 1;

      // effect
      let effects = [];
      if (tag === "Confirm") {
        effects = effectsOf(attrExpr(attrs.get("onConfirm")), ctx, apiMap, owner.route);
      } else if (tag === "Link" || tag === "NavLink" || tag === "a") {
        const to = attrExpr(attrs.get("to")) ?? attrExpr(attrs.get("href"));
        const s = to ? renderString(to) : null;
        if (s !== null) effects.push({ type: "navigate", detail: s });
        else if (to && ts.isCallExpression(to)) effects.push({ type: "navigate", detail: `${to.expression.getText()}(…)` });
        else if (to && ts.isObjectLiteralExpression(to)) effects.push({ type: "navigate", detail: objectNav(to, owner.route) });
        else if (to) effects.push({ type: "navigate", detail: `{${to.getText().slice(0, 40)}}` });
        if (attrs.has("onClick")) effects.push(...effectsOf(attrExpr(attrs.get("onClick")), ctx, apiMap, owner.route));
        if (attrs.has("download")) effects.push({ type: "local", detail: "download" });
      } else if (kind === "toggle" || kind === "tab") {
        const h = attrExpr(attrs.get("onChange")) ?? attrExpr(attrs.get("onClick")) ?? attrExpr(attrs.get("onSelect"));
        effects = effectsOf(h, ctx, apiMap, owner.route);
        if (!effects.length) effects = [{ type: "local", detail: tag === "SubTabs" || tag === "Segmented" || tag === "Steps" ? "switch-tab" : "select" }];
      } else if (tag === "Pager") {
        effects = [{ type: "local", detail: "page" }];
      } else if (tag === "Steps") {
        effects = [{ type: "local", detail: "step" }];
      } else if (tag === "summary") {
        effects = [{ type: "local", detail: "expand" }];
      } else if (tag === "input") {
        const h = attrExpr(attrs.get("onChange"));
        effects = h ? effectsOf(h, ctx, apiMap, owner.route) : [];
        if (!effects.length) effects = [{ type: "local", detail: "toggle" }];
      } else {
        const h = attrExpr(attrs.get("onClick"));
        if (h) effects = effectsOf(h, ctx, apiMap, owner.route);
        else if (renderString(attrExpr(attrs.get("type"))) === "submit") effects = [{ type: "local", detail: "form-submit" }];
      }
      // a Confirm renders two controls: cancel (close) and ok (onConfirm) — both reachable via fixed testIds
      const rowsHere = [];
      if (tag === "Confirm") {
        rowsHere.push({ testId: "v2-confirm-ok", kind: "dialog-action", label: labelOf(n, new Map([["confirmLabel", attrs.get("confirmLabel")]])), effects, destructive: attrs.has("danger") || effects.some((e) => DESTRUCTIVE_RE.test(e.detail)) });
      } else {
        rowsHere.push({ testId, kind, label, effects, destructive: false });
      }
      for (const r of rowsHere) {
        const guards = guardsAround(n, ctx);
        const danger = attrs.has("danger") || renderString(attrExpr(attrs.get("kind"))) === "danger";
        const destructive = r.destructive || danger || DESTRUCTIVE_RE.test(r.testId ?? "") || DESTRUCTIVE_RE.test(String(r.label ?? "")) || r.effects.some((e) => e.type === "api" && /^(DELETE|PUT|PATCH|POST) /.test(e.detail) && DESTRUCTIVE_RE.test(e.detail)) || r.effects.some((e) => e.type === "api" && e.detail.startsWith("DELETE "));
        const disabled = attrs.has("disabled");
        const hasHint = attrs.has("title") || attrs.has("hint") || attrs.has("aria-label");
        const primary = r.effects.find((e) => e.type === "api") ?? r.effects.find((e) => e.type === "navigate") ?? r.effects.find((e) => e.type === "dialog") ?? r.effects[0] ?? null;
        rows.push({
          id: `${rel}:${r.testId ?? `#${ordinal}`}`,
          page: owner.route,
          view: viewHere,
          tab: tabHere,
          file: rel,
          line,
          tag,
          kind: r.kind,
          testId: r.testId,
          selector: r.testId ? `[data-testid="${r.testId}"]` : derivedSelector(tag, r.label, kind),
          label: r.label,
          effect: primary ? { type: primary.type, detail: primary.detail } : null,
          effects: r.effects,
          guards,
          roles: rolesFromGuards(guards, adminPages.has(owner.route)),
          destructive,
          disabledWithoutHint: disabled && !hasHint,
        });
      }
    }
  }
  // reformat-stable order: page, file, then source order within the file (the encounter order the
  // ordinals were assigned in) — never `line`, which a pure reformat shifts
  rows.forEach((r, i) => { r._seq = i; });
  rows.sort((a, b) => a.page.localeCompare(b.page) || a.file.localeCompare(b.file) || a._seq - b._seq);
  rows.forEach((r) => { delete r._seq; });
  // stable duplicate suffixes for rows that share an id (same testId twice in one file)
  const seenIds = new Map();
  for (const r of rows) {
    const c = (seenIds.get(r.id) ?? 0) + 1;
    seenIds.set(r.id, c);
    if (c > 1) r.id = `${r.id}~${c}`;
  }
  return { generatedFrom: "scripts/control-inventory.mjs", count: rows.length, controls: rows };
}

/** Components rendered under a `view === "x"` guard — `if`, ternary or `&&` — plus the
 *  `selected`-indirection (`const sel = view === "edit" ? id : null; … {sel && <Editor/>}`). */
function viewDispatch(ctx, param = "view") {
  const out = new Map();
  const add = (view, subtree) => {
    for (const d of descendants(subtree)) {
      const tag = tagName(d);
      if (tag && /^[A-Z]/.test(tag) && !out.has(tag)) out.set(tag, view);
    }
  };
  const derived = new Map(); // variable → view it stands for (`const selected = view === "new" ? … : null`)
  for (const n of descendants(ctx.sf)) {
    if (ts.isVariableDeclaration(n) && ts.isIdentifier(n.name) && n.initializer) {
      const v = n.initializer.getText().match(new RegExp(`\\b${param}\\b[^?]*===\\s*"([^"]+)"`))?.[1];
      if (v) derived.set(n.name.text, v);
    }
  }
  const viewOfCond = (expr) => {
    const txt = expr.getText();
    const direct = txt.match(new RegExp(`\\b${param}\\s*===\\s*"([^"]+)"`))?.[1];
    if (direct) return direct;
    for (const [name, v] of derived) if (new RegExp(`\\b${name}\\b`).test(txt) && !/!\s*$/.test(txt.split(name)[0] ?? "")) return v;
    return null;
  };
  for (const n of descendants(ctx.sf)) {
    if (ts.isIfStatement(n)) {
      const v = viewOfCond(n.expression);
      if (v) add(v, n.thenStatement);
    } else if (ts.isConditionalExpression(n)) {
      const v = viewOfCond(n.condition);
      if (v) add(v, n.whenTrue);
    } else if (ts.isBinaryExpression(n) && n.operatorToken.kind === SK.AmpersandAmpersandToken) {
      const v = viewOfCond(n.left);
      if (v) add(v, n.right);
    }
  }
  return out;
}

/** Name of the top-level function / const component a node sits in. */
function enclosingComponent(node) {
  let p = node.parent;
  while (p && !ts.isSourceFile(p)) {
    if (ts.isFunctionDeclaration(p) && p.name && ts.isSourceFile(p.parent)) return p.name.text;
    if (ts.isVariableDeclaration(p) && ts.isIdentifier(p.name) && p.parent?.parent && ts.isVariableStatement(p.parent.parent) && ts.isSourceFile(p.parent.parent.parent)) return p.name.text;
    p = p.parent;
  }
  return null;
}

/** `if (view === "edit") return <EvaluatorEditor/>` → Map(component → view), closed over in-file children. */
function inFileViews(ctx, param = "view") {
  const views = new Map();
  for (const [tag, view] of viewDispatch(ctx, param)) if (ctx.decls.has(tag)) views.set(tag, view);
  // close over components rendered inside a view component (two passes suffice in practice)
  for (let pass = 0; pass < 2; pass++) {
    for (const [comp, view] of [...views]) {
      const decl = ctx.decls.get(comp);
      if (!decl) continue;
      for (const d of descendants(decl)) {
        const tag = tagName(d);
        if (tag && ctx.decls.has(tag) && !views.has(tag) && /^[A-Z]/.test(tag)) views.set(tag, view);
      }
    }
  }
  return views;
}

function derivedSelector(tag, label, kind) {
  const l = label && label.startsWith("t:") ? null : label;
  if (tag === "Pager") return ".v2-pager button";
  if (tag === "Steps") return ".v2-steps button";
  if (tag === "Link" || tag === "a") return l ? `a:has-text("${l}")` : "a";
  if (tag === "button" || tag === "Button" || tag === "LinkButton") return l ? `button:has-text("${l}")` : `button`;
  return `${tag}${kind ? `[data-kind=${kind}]` : ""}`;
}

/**
 * The compared payload of an inventory: every row minus `line`. `line` stays in the committed
 * file for navigation but a pure reformat of a page must not fail the drift guard, so the
 * guard (and `--check`) compare this projection, keyed by the reformat-stable `id`.
 */
export function semantic(inv) {
  return { count: inv.count, controls: inv.controls.map((row) => { const rest = { ...row }; delete rest.line; return rest; }) };
}

/** Rows that a test cannot reach or whose effect is unknown — the finding set. */
export function untestable(inv) {
  return inv.controls.filter((c) => !c.testId || !c.effect).map((c) => ({ id: c.id, file: c.file, line: c.line, reason: !c.testId && !c.effect ? "no-testid+no-effect" : !c.testId ? "no-testid" : "no-effect" }));
}

/** One control (or register entry) per line: diff-friendly and a third of the pretty size. */
export function stableJson(obj) {
  const rows = obj.controls ?? obj.entries;
  if (!Array.isArray(rows)) return `${JSON.stringify(obj, null, 2)}\n`;
  const key = obj.controls ? "controls" : "entries";
  const head = { ...obj };
  delete head[key];
  const headText = JSON.stringify(head, null, 2).replace(/\n\}$/, "");
  return `${headText},\n  "${key}": [\n${rows.map((r) => `    ${JSON.stringify(r)}`).join(",\n")}\n  ]\n}\n`;
}

/* ───────────────────────────── CLI ──────────────────────────────── */

const invokedDirectly = process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url);
if (invokedDirectly) {
  const args = new Set(process.argv.slice(2));
  const inv = generate();
  const un = untestable(inv);
  const byPage = {};
  for (const c of inv.controls) byPage[c.page] = (byPage[c.page] ?? 0) + 1;
  if (args.has("--write")) {
    fs.mkdirSync(INVENTORY_DIR, { recursive: true });
    fs.writeFileSync(CONTROLS_JSON, stableJson(inv));
    // the untestable register is append-only: keep existing entries (and their register ids), add new ones
    let existing = [];
    if (fs.existsSync(UNTESTABLE_JSON)) existing = JSON.parse(read(UNTESTABLE_JSON)).entries ?? [];
    const known = new Map(existing.map((e) => [e.id, e]));
    for (const u of un) if (!known.has(u.id)) existing.push({ ...u, register: "N16-obs99" });
    // `floor` = the largest entry count ever written: the guard fails when entries are deleted
    const prevFloor = fs.existsSync(UNTESTABLE_JSON) ? (JSON.parse(read(UNTESTABLE_JSON)).floor ?? 0) : 0;
    fs.writeFileSync(UNTESTABLE_JSON, stableJson({ generatedFrom: "scripts/control-inventory.mjs --write", note: "append-only register of controls no test can reach (no testId) or whose effect the generator cannot infer; each entry names its bugs-round-16 row; `floor` is the entry count that may never decrease", floor: Math.max(prevFloor, existing.length), entries: existing }));
    console.log(`wrote ${CONTROLS_JSON} (${inv.count} controls) and ${UNTESTABLE_JSON} (${existing.length} entries)`);
  }
  if (args.has("--check")) {
    const committed = fs.existsSync(CONTROLS_JSON) ? JSON.parse(read(CONTROLS_JSON)) : null;
    if (!committed || JSON.stringify(semantic(committed)) !== JSON.stringify(semantic(inv))) {
      console.error("control inventory drift: run `node scripts/control-inventory.mjs --write`");
      process.exit(1);
    }
  }
  const stats = {
    controls: inv.count,
    withTestId: inv.controls.filter((c) => c.testId).length,
    withEffect: inv.controls.filter((c) => c.effect).length,
    destructive: inv.controls.filter((c) => c.destructive).length,
    disabledWithoutHint: inv.controls.filter((c) => c.disabledWithoutHint).length,
    untestable: un.length,
    byKind: Object.fromEntries([...inv.controls.reduce((m, c) => m.set(c.kind, (m.get(c.kind) ?? 0) + 1), new Map())].sort()),
    byEffectType: Object.fromEntries([...inv.controls.reduce((m, c) => m.set(c.effect?.type ?? "none", (m.get(c.effect?.type ?? "none") ?? 0) + 1), new Map())].sort()),
    byPage,
  };
  console.log(JSON.stringify(stats, null, 2));
}
