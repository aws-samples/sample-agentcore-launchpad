#!/usr/bin/env python3
"""zh-CN typography gate: full-width punctuation inside Chinese copy.

Chinese prose uses full-width punctuation (，：；？！（）). The zh-CN locale
historically mixed both styles (`失败原因:` next to `失败原因：`). This script
makes the rule mechanical:

    python3 scripts/i18n_zh_punct.py --check   # exit 1 + list offending keys
    python3 scripts/i18n_zh_punct.py --fix     # rewrite the locale in place

Conversion rule — a half-width `,` `:` `;` `?` `!` `(` `)` is converted only
when at least one neighbouring character is Chinese context: a CJK ideograph
(U+4E00–U+9FFF), CJK / full-width punctuation (U+3000–U+303F, U+FF00–U+FFEF),
or the Chinese-prose punctuation `——` `“ ”` `‘ ’` (not `…`, which is
shared with Latin placeholders such as `arn:aws:…:…`). Brackets convert as a
pair: if either half of a matched `( … )` qualifies, both do, so
`密码(LAUNCHPAD_AUTH_PASSWORD)——` becomes `密码（LAUNCHPAD_AUTH_PASSWORD）——`.

Never touched: `{{placeholder}}` / `{placeholder}` spans, backtick spans,
URLs, ARNs, and any punctuation with Latin / digit / space on both sides
(`session.id`, `us-west-2`, `12:00`, `a, b`, `SKILL.md`, paths, CLI flags).
The en locale is never read or written.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_FILE = ROOT / "frontend" / "src" / "locales" / "zh-CN" / "common.json"

HALF_TO_FULL = {
    ",": "，",
    ":": "：",
    ";": "；",
    "?": "？",
    "!": "！",
    "(": "（",
    ")": "）",
}

# Characters that mark "Chinese prose" context on either side of a mark.
CJK_RE = re.compile(
    "["
    "一-鿿"  # CJK unified ideographs
    "　-〿"  # CJK symbols & punctuation (、。「」…)
    "＀-￯"  # full-width forms (，：；？！（）)
    "—"  # — (—— Chinese dash)
    "‘’“”"  # ‘ ’ “ ”
    "]"
)

# Spans that are never rewritten, whatever their neighbours are.
PROTECTED_RE = re.compile(
    r"\{\{[^{}]*\}\}"  # {{placeholder}}
    r"|\{[^{}\s]*\}"  # {placeholder} / {0,47}
    r"|`[^`]*`"  # `code`
    r"|https?://[A-Za-z0-9._~:/?#\[\]@!$&'*+,;=%-]+"  # URL (brackets excluded)
    r"|arn:aws[^\s()（）]*"  # ARN (incl. … placeholders)
)


def _protected_mask(text: str) -> list[bool]:
    mask = [False] * len(text)
    for m in PROTECTED_RE.finditer(text):
        for i in range(m.start(), m.end()):
            mask[i] = True
    return mask


def _is_cjk(ch: str) -> bool:
    return bool(ch) and bool(CJK_RE.match(ch))


def convert(text: str) -> str:
    """Return `text` with half-width marks adjacent to Chinese context made full-width.

    Runs to a fixpoint: converting `)` to `）` can make the `,` right after it
    qualify, so a single pass would not be idempotent.
    """
    while True:
        converted = _convert_once(text)
        if converted == text:
            return text
        text = converted


def _convert_once(text: str) -> str:
    if not text:
        return text
    mask = _protected_mask(text)
    n = len(text)

    def qualifies(i: int) -> bool:
        if mask[i]:
            return False
        left = text[i - 1] if i > 0 else ""
        right = text[i + 1] if i + 1 < n else ""
        return _is_cjk(left) or _is_cjk(right)

    out = list(text)
    to_convert: set[int] = set()

    # Non-bracket marks: local adjacency only.
    for i, ch in enumerate(text):
        if ch in ",:;?!" and qualifies(i):
            to_convert.add(i)

    # Brackets: pair them, then convert the pair if either half qualifies.
    stack: list[int] = []
    for i, ch in enumerate(text):
        if mask[i]:
            continue
        if ch == "(":
            stack.append(i)
        elif ch == ")":
            if stack:
                open_i = stack.pop()
                if qualifies(open_i) or qualifies(i):
                    to_convert.update((open_i, i))
            elif qualifies(i):
                to_convert.add(i)
    for open_i in stack:  # unmatched "(" — own adjacency only
        if qualifies(open_i):
            to_convert.add(open_i)

    for i in to_convert:
        out[i] = HALF_TO_FULL[text[i]]
    return "".join(out)


def _walk(obj, path: str = ""):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from _walk(v, f"{path}.{k}" if path else k)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from _walk(v, f"{path}[{i}]")
    elif isinstance(obj, str):
        yield path, obj


def _rewrite(obj):
    if isinstance(obj, dict):
        return {k: _rewrite(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_rewrite(v) for v in obj]
    if isinstance(obj, str):
        return convert(obj)
    return obj


def find_offenders(data) -> list[tuple[str, str, str]]:
    """(key, before, after) for every string a `--fix` would change."""
    out = []
    for key, value in _walk(data):
        fixed = convert(value)
        if fixed != value:
            out.append((key, value, fixed))
    return out


def run(path: Path, fix: bool) -> int:
    raw = path.read_text(encoding="utf-8")
    data = json.loads(raw)
    offenders = find_offenders(data)
    rel = path.relative_to(ROOT) if path.is_relative_to(ROOT) else path
    if not offenders:
        print(f"i18n_zh_punct: {rel}: full-width punctuation OK")
        return 0
    if fix:
        fixed = _rewrite(data)
        serialized = json.dumps(fixed, indent=2, ensure_ascii=False) + "\n"
        # Guard: our writer must reproduce the file's own layout so a --fix only
        # ever touches the punctuation, never the formatting.
        if json.dumps(data, indent=2, ensure_ascii=False) + "\n" != raw:
            print(f"i18n_zh_punct: {rel}: not in canonical indent=2 layout, refusing to rewrite")
            return 2
        path.write_text(serialized, encoding="utf-8")
        print(f"i18n_zh_punct: {rel}: fixed {len(offenders)} value(s)")
        for key, before, after in offenders:
            print(f"  {key}: {before!r} -> {after!r}")
        return 0
    print(f"i18n_zh_punct: {rel}: {len(offenders)} value(s) use half-width punctuation next to CJK")
    for key, before, after in offenders:
        print(f"  {key}: {before!r} -> {after!r}")
    print("i18n_zh_punct: run `python3 scripts/i18n_zh_punct.py --fix` to convert them")
    print("i18n_zh_punct: FAIL")
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="exit 1 if any value needs conversion")
    mode.add_argument("--fix", action="store_true", help="rewrite the locale file in place")
    parser.add_argument(
        "--file", type=Path, default=DEFAULT_FILE, help=f"locale file (default {DEFAULT_FILE})"
    )
    args = parser.parse_args(argv)
    return run(args.file.resolve(), fix=args.fix)


if __name__ == "__main__":
    sys.exit(main())
