// File path → Monaco language id (Monaco's built-in basic languages only; anything
// unknown is plain text).

const BY_NAME: Record<string, string> = {
  dockerfile: "dockerfile",
  containerfile: "dockerfile",
  makefile: "plaintext",
  ".gitignore": "plaintext",
  ".dockerignore": "plaintext",
  ".env": "ini",
  ".editorconfig": "ini",
  "requirements.txt": "plaintext",
  "uv.lock": "ini",
  "cargo.lock": "ini",
  "package-lock.json": "json",
};

const BY_EXT: Record<string, string> = {
  py: "python",
  pyi: "python",
  js: "javascript",
  mjs: "javascript",
  cjs: "javascript",
  jsx: "javascript",
  ts: "typescript",
  mts: "typescript",
  cts: "typescript",
  tsx: "typescript",
  json: "json",
  jsonc: "json",
  jsonl: "plaintext",
  md: "markdown",
  markdown: "markdown",
  mdx: "markdown",
  yml: "yaml",
  yaml: "yaml",
  toml: "ini",
  ini: "ini",
  cfg: "ini",
  conf: "ini",
  sh: "shell",
  bash: "shell",
  zsh: "shell",
  html: "html",
  htm: "html",
  css: "css",
  scss: "scss",
  less: "less",
  xml: "xml",
  svg: "xml",
  sql: "sql",
  go: "go",
  rs: "rust",
  java: "java",
  kt: "kotlin",
  kts: "kotlin",
  rb: "ruby",
  php: "php",
  c: "c",
  h: "c",
  cc: "cpp",
  cpp: "cpp",
  cxx: "cpp",
  hpp: "cpp",
  cs: "csharp",
  swift: "swift",
  r: "r",
  lua: "lua",
  pl: "perl",
  ps1: "powershell",
  graphql: "graphql",
  gql: "graphql",
  tf: "hcl",
  hcl: "hcl",
  txt: "plaintext",
  log: "plaintext",
  csv: "plaintext",
};

function baseName(path: string): string {
  return path.slice(path.lastIndexOf("/") + 1).toLowerCase();
}

export function languageFor(path: string): string {
  const name = baseName(path);
  if (BY_NAME[name]) return BY_NAME[name];
  if (name.startsWith("dockerfile.") || name.endsWith(".dockerfile")) return "dockerfile";
  const dot = name.lastIndexOf(".");
  if (dot <= 0 && !name.startsWith(".")) return "plaintext";
  return BY_EXT[name.slice(dot + 1)] ?? "plaintext";
}

export function isMarkdown(path: string): boolean {
  return languageFor(path) === "markdown";
}
