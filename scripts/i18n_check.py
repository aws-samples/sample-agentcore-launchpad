#!/usr/bin/env python3
"""i18n gate.

Default: fail if en and zh-CN locale files don't share identical key sets.
--strict adds:
  * hardcoded-string scan — JSX text literals and user-facing attributes in
    frontend/src that look like natural language (not routed through t());
    technical tokens (API names, ARNs, units, symbols, ALL-CAPS chips) are
    allowlisted by shape, plus an explicit literal allowlist below
  * key-usage audit — t("…") references missing from en/common.json fail;
    locale keys never referenced are reported (report-only)
"""

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOCALES = ROOT / "frontend" / "src" / "locales"
SRC = ROOT / "frontend" / "src"
BASE, OTHER = "en", "zh-CN"

# Literal JSX/attribute strings that look like prose but are locale-invariant.
ALLOWLIST = {
    "AgentCore Launchpad",
    "Strands Studio",
    "IBM Plex Mono",
}
# Shapes that never need translation: symbols/punctuation, numbers+units,
# versions, ALL-CAPS chips, single technical tokens (camelCase, snake_case,
# dotted/hyphenated ids), arrow pipelines, env assignments, ARNs.
TECHNICAL_PATTERNS = [
    r"^[\W\d_\s]+$",  # symbols / digits only
    r"^[A-Z0-9 ·/&_\-→⇄+=.:%()#]+$",  # ALL-CAPS chip text
    r"^\S+$",  # single token (no spaces)
    r"arn:aws",  # ARNs
    r"^[\w.\-\[\]]+ ?[=:] ?\S*$",  # KEY=value / key: value
    r"^[\w.\-()]+( [·/→⇄|+—-] [\w.\-()†✓]+)+( ✓)?$",  # token · token → token
    r"^[\w.\-]+ \d[\w.]*$",  # name + version number
    r"^[\w.\-]+ [·—] ",  # token · annotation
]
TECHNICAL_RES = [re.compile(p) for p in TECHNICAL_PATTERNS]
NATURAL_RE = re.compile(r"[A-Za-z]{2,}\s+[a-z]{2,}")  # two+ words, prose-like
ATTR_RE = re.compile(r'(?:placeholder|title|aria-label|alt|label)="([^"{}]+)"')
JSX_TEXT_RE = re.compile(r">([^<>{}]+)<")
T_CALL_RE = re.compile(r"""[^\w.]t\(\s*(["'`])(.+?)\1""")


def flatten(obj: dict, prefix: str = "") -> set[str]:
    keys: set[str] = set()
    for k, v in obj.items():
        path = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict):
            keys |= flatten(v, path)
        else:
            keys.add(path)
    return keys


def parity_check() -> tuple[bool, set[str]]:
    base_dir, other_dir = LOCALES / BASE, LOCALES / OTHER
    if not base_dir.is_dir() or not other_dir.is_dir():
        print(f"i18n_check: missing locale dir ({base_dir} or {other_dir})")
        return True, set()

    failed = False
    all_keys: set[str] = set()
    base_files = sorted(p.name for p in base_dir.glob("*.json"))
    other_files = sorted(p.name for p in other_dir.glob("*.json"))
    if base_files != other_files:
        print(f"i18n_check: namespace files differ: {BASE}={base_files} {OTHER}={other_files}")
        failed = True

    for name in base_files:
        if name not in other_files:
            continue
        en_keys = flatten(json.loads((base_dir / name).read_text(encoding="utf-8")))
        zh_keys = flatten(json.loads((other_dir / name).read_text(encoding="utf-8")))
        all_keys |= en_keys
        only_en = sorted(en_keys - zh_keys)
        only_zh = sorted(zh_keys - en_keys)
        if only_en or only_zh:
            failed = True
            if only_en:
                print(f"i18n_check: {name}: missing in {OTHER}: {only_en}")
            if only_zh:
                print(f"i18n_check: {name}: missing in {BASE}: {only_zh}")
        else:
            print(f"i18n_check: {name}: {len(en_keys)} keys, parity OK")

    return failed, all_keys


def is_hardcoded(text: str) -> bool:
    text = text.strip()
    if not text or text in ALLOWLIST:
        return False
    if any(p.search(text) for p in TECHNICAL_RES):
        return False
    return bool(NATURAL_RE.search(text))


def strict_checks(locale_keys: set[str]) -> bool:
    failed = False

    findings: list[tuple[str, int, str]] = []
    used_keys: set[str] = set()
    dynamic_prefixes: set[str] = set()
    all_source: list[str] = []
    for path in sorted(SRC.rglob("*.tsx")) + sorted(SRC.rglob("*.ts")):
        rel = path.relative_to(ROOT)
        text = path.read_text(encoding="utf-8")
        all_source.append(text)
        for lineno, line in enumerate(text.splitlines(), 1):
            for match in JSX_TEXT_RE.finditer(line):
                if is_hardcoded(match.group(1)):
                    findings.append((str(rel), lineno, match.group(1).strip()))
            for match in ATTR_RE.finditer(line):
                if is_hardcoded(match.group(1)):
                    findings.append((str(rel), lineno, match.group(1).strip()))
            for match in T_CALL_RE.finditer(line):
                key = match.group(2)
                if "${" in key:
                    dynamic_prefixes.add(key.split("${", 1)[0])
                else:
                    used_keys.add(key)
    # keys referenced as data (t(entry.labelKey), tab arrays, …): any quoted
    # literal equal to a locale key counts as used
    source_blob = "\n".join(all_source)
    quoted_literals = set(re.findall(r"""["'`]([\w.\-]+)["'`]""", source_blob))

    if findings:
        failed = True
        for rel, lineno, text in findings:
            print(f"i18n_check: hardcoded string {rel}:{lineno}: {text!r}")
    print(f"i18n_check: hardcoded-string findings: {len(findings)}")

    # i18next resolves `t("k", {count})` through `k_one` / `k_other` (plural suffixes):
    # a base key with plural variants is present (N16-obs57, round 16).
    plural = ("_zero", "_one", "_two", "_few", "_many", "_other")
    missing = sorted(
        k for k in used_keys
        if k not in locale_keys and not any(f"{k}{s}" in locale_keys for s in plural)
    )
    if missing:
        failed = True
        print(f"i18n_check: t() keys missing from locales: {missing}")
    else:
        print(f"i18n_check: all {len(used_keys)} static t() keys resolve")

    unused = sorted(
        k
        for k in locale_keys
        if k not in used_keys
        and k not in quoted_literals
        and not any(k.startswith(p) for p in dynamic_prefixes)
    )
    if unused:  # report-only: dynamic composition can hide legitimate uses
        print(f"i18n_check: unused-key report ({len(unused)}): {unused}")
    else:
        print("i18n_check: unused-key report: none")

    return failed


def main() -> int:
    strict = "--strict" in sys.argv
    failed, locale_keys = parity_check()
    if strict and strict_checks(locale_keys):
        failed = True
    print(f"i18n_check: {'FAIL' if failed else 'PASS'}{' (strict)' if strict else ''}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
