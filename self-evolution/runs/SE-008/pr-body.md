## Summary

The zh-CN locale mixed half-width and full-width punctuation inside Chinese sentences (`失败原因:` next to `失败原因：`, `前缀(可选)`). This makes the rule mechanical and applies it.

- `scripts/i18n_zh_punct.py --check|--fix`: converts `, : ; ? ! ( )` to `，：；？！（）` only where a neighbouring character is Chinese context (CJK ideographs, CJK/full-width punctuation, `——`, curly quotes); brackets convert as a pair. Never touches `{{placeholders}}`, backtick spans, URLs, ARNs, or marks with Latin/digits/space on both sides (`session.id`, `http(s)`, `get_config_bundle()`). The en locale is never read or written.
- `--fix` applied: 182 keys changed, every change a single-character punctuation swap (verified character-by-character); key set and placeholders unchanged; parity intact.
- `scripts/verify.sh` runs `--check` after `i18n_check.py`; hermetic pytest `backend/tests/test_i18n_zh_punct.py` covers the rule, the protected spans, and check-fails-then-fix-passes. `CLAUDE.md` and both READMEs mention the gate.

## Verification

- `make verify` PASS (with the new `i18n_zh_punct` step).
- Independent diff check: 182 keys differ, no length or non-punctuation change, en untouched; console renders `失败原因：` and `前缀（可选）` in zh-CN.

Self-evolution direction SE-008 (`ux` path).
