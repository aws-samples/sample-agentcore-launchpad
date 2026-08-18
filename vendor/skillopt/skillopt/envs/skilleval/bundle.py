"""Multi-document skill bundle — train more than SKILL.md as one state.

The ReflACT trainer's trainable state is a single string (edits anchor on
exact substrings via ``skill.find(target)``), so a multi-file skill can only
evolve if its trainable documents travel together in one document. This
module is that codec: ``build_bundle`` joins SKILL.md plus a whitelist of
trainable text files under ``<!-- FILE: path -->`` headers, and
``split_bundle`` tolerantly parses a (possibly optimizer-mangled) bundle
back into per-file contents.

Layout contract: trainable reference files come first, **SKILL.md is always
the last section**. Untargeted ``append`` edits land at the document tail,
so keeping SKILL.md last preserves the single-document behavior of appends
growing SKILL.md.

Robustness contract (the gate is the quality backstop; this is the safety
one): sections whose path is not in the caller's whitelist are dropped, so
optimizer-written text can never choose filesystem locations. A missing or
mangled section simply doesn't appear in the parse result — callers fall
back to the seed copy of that file.

Also runnable as a CLI for seeding and deployment:

    python3 -m skillopt.envs.skilleval.bundle build <skill_dir> \
        --files references/a.md,references/b.md --out seed_bundle.md
    python3 -m skillopt.envs.skilleval.bundle split <bundle.md> \
        --skill_dir <skill_dir> --out_dir <deploy_dir>
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import sys

SKILL_MD = "SKILL.md"

_HEADER_RE = re.compile(r"^<!-- FILE: (?P<path>[^>]+?) -->\s*$", re.MULTILINE)


def _header(path: str) -> str:
    return f"<!-- FILE: {path} -->"


def normalize_rel_path(path: str) -> str:
    """Validate + normalize a bundle-relative path; raise ValueError if unsafe."""
    cleaned = str(path).strip().replace("\\", "/")
    if not cleaned:
        raise ValueError("empty path in bundle")
    if cleaned.startswith("/") or re.match(r"^[A-Za-z]:", cleaned):
        raise ValueError(f"absolute path not allowed in bundle: {path!r}")
    parts = cleaned.split("/")
    if ".." in parts:
        raise ValueError(f"parent traversal not allowed in bundle: {path!r}")
    return "/".join(p for p in parts if p not in ("", "."))


def build_bundle(skill_md: str, docs: list[tuple[str, str]]) -> str:
    """Join *docs* (``(rel_path, content)``, order preserved) + SKILL.md last."""
    sections = []
    for rel, content in docs:
        rel = normalize_rel_path(rel)
        if rel == SKILL_MD:
            raise ValueError("SKILL.md is added automatically; do not pass it in docs")
        sections.append(f"{_header(rel)}\n{content.rstrip()}\n")
    sections.append(f"{_header(SKILL_MD)}\n{skill_md.rstrip()}\n")
    return "\n".join(sections)


def is_bundle(text: str) -> bool:
    return bool(_HEADER_RE.search(text or ""))


def split_bundle(text: str, allowed: list[str] | None = None) -> dict[str, str]:
    """Parse a bundle into ``{rel_path: content}``. Never raises on content.

    - No headers at all → the whole text is SKILL.md (single-doc state).
    - Leading text before the first header is attached to the first section
      (an optimizer edit displaced the header; the gate judges the result).
    - Sections not in *allowed* (when given) are dropped, as are sections
      whose path fails safety normalization — bundle text can never pick
      filesystem locations outside the whitelist.
    - A repeated path keeps the last occurrence.
    """
    text = text or ""
    matches = list(_HEADER_RE.finditer(text))
    if not matches:
        return {SKILL_MD: text}

    allowed_set = None
    if allowed is not None:
        allowed_set = {normalize_rel_path(a) for a in allowed} | {SKILL_MD}

    docs: dict[str, str] = {}
    for i, m in enumerate(matches):
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        content = text[start:end].strip("\n")
        if i == 0 and m.start() > 0:
            lead = text[: m.start()].strip("\n")
            if lead:
                content = f"{lead}\n{content}" if content else lead
        try:
            rel = normalize_rel_path(m.group("path"))
        except ValueError:
            continue
        if allowed_set is not None and rel not in allowed_set:
            continue
        docs[rel] = content
    return docs


# ── CLI (seeding + deployment) ─────────────────────────────────────────────

def _cli_build(args: argparse.Namespace) -> None:
    skill_md_path = os.path.join(args.skill_dir, SKILL_MD)
    if not os.path.isfile(skill_md_path):
        sys.exit(f"error: no SKILL.md in {args.skill_dir}")
    rels = [r.strip() for r in args.files.split(",") if r.strip()]
    if not rels:
        sys.exit("error: --files is empty")
    docs = []
    for rel in rels:
        full = os.path.join(args.skill_dir, normalize_rel_path(rel))
        if not os.path.isfile(full):
            sys.exit(f"error: trainable file not found: {full}")
        with open(full, encoding="utf-8") as f:
            docs.append((rel, f.read()))
    with open(skill_md_path, encoding="utf-8") as f:
        skill_md = f.read()
    bundle = build_bundle(skill_md, docs)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(bundle)
    print(f"wrote {args.out} ({len(rels)} trainable file(s) + SKILL.md)")


def _cli_split(args: argparse.Namespace) -> None:
    with open(args.bundle, encoding="utf-8") as f:
        docs = split_bundle(f.read())
    if os.path.exists(args.out_dir):
        sys.exit(f"error: out_dir already exists: {args.out_dir}")
    if args.skill_dir:
        shutil.copytree(args.skill_dir, args.out_dir, symlinks=False)
    else:
        os.makedirs(args.out_dir)
    for rel, content in docs.items():
        dst = os.path.join(args.out_dir, rel)
        os.makedirs(os.path.dirname(dst) or args.out_dir, exist_ok=True)
        with open(dst, "w", encoding="utf-8") as f:
            f.write(content.rstrip() + "\n")
        print(f"  {rel} ({len(content)} chars)")
    print(f"deployable skill at {args.out_dir}")


def main() -> None:
    p = argparse.ArgumentParser(description="skilleval multi-doc bundle codec")
    sub = p.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build", help="build a seed bundle from a skill directory")
    b.add_argument("skill_dir")
    b.add_argument("--files", required=True,
                   help="comma-separated trainable files, relative to skill_dir")
    b.add_argument("--out", required=True)
    s = sub.add_parser("split", help="split a trained bundle into a deployable skill dir")
    s.add_argument("bundle")
    s.add_argument("--skill_dir", default="",
                   help="original skill dir; frozen files are copied from here")
    s.add_argument("--out_dir", required=True)
    args = p.parse_args()
    _cli_build(args) if args.cmd == "build" else _cli_split(args)


if __name__ == "__main__":
    main()
