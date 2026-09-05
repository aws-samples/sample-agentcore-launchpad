"""zh-CN full-width punctuation gate (`scripts/i18n_zh_punct.py`).

Hermetic: the conversion rule is exercised on literal strings and the CLI on a
temp locale file; the real locale is only asserted clean (it is checked in).
"""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "i18n_zh_punct.py"
ZH_LOCALE = REPO / "frontend" / "src" / "locales" / "zh-CN" / "common.json"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("launchpad_i18n_zh_punct", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


punct = _load()


@pytest.mark.parametrize(
    ("before", "after"),
    [
        # the two console spot checks from the direction brief
        ("失败原因:", "失败原因："),
        ("前缀(可选)", "前缀（可选）"),
        # every mark, CJK on one side only
        ("登录,{{days}} 天", "登录，{{days}} 天"),
        ("阶段失败:{{msg}}", "阶段失败：{{msg}}"),
        ("开头;仅允许", "开头；仅允许"),
        ("是否继续?", "是否继续？"),
        ("完成!", "完成！"),
        # bracket pairs convert together even when one half touches Latin
        ("密码(LAUNCHPAD_AUTH_PASSWORD)——开放", "密码（LAUNCHPAD_AUTH_PASSWORD）——开放"),
        ("task_type(可选)", "task_type（可选）"),
        ("JSON(可选)", "JSON（可选）"),
        # Chinese-prose punctuation counts as context
        ("节点‘{{label}}’:{{action}}", "节点‘{{label}}’：{{action}}"),
        ("删除“{{name}}”?", "删除“{{name}}”？"),
        # cascade: the comma qualifies only once the bracket is full-width
        ("位置(或 S3),Agent 即可", "位置（或 S3），Agent 即可"),
    ],
)
def test_converts_half_width_next_to_cjk(before: str, after: str):
    assert punct.convert(before) == after
    assert punct.convert(after) == after  # idempotent


@pytest.mark.parametrize(
    "text",
    [
        "",
        "Hello, world: (ok)?",  # pure Latin
        "us-west-2 · session.id · SKILL.md · 12:00",
        "arn:aws:s3files:…:file-system/…/access-point/…",  # ARN with … placeholders
        "arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/abc",
        "https://api.openai.com/v1?view=experiment",
        "config/launchpad.yaml · model_prices",
        "get_config_bundle() 读取",  # Latin-adjacent brackets stay
        "在 NEW RUN 中选择 ACTOR MODEL;goal 达成",  # `;` has Latin on both sides
        "^[a-zA-Z][a-zA-Z0-9_]{0,47}$ 结尾",  # regex quantifier braces are protected
        "`make bootstrap(--yes)` 创建",  # backtick span
        "{{count,number}} 个",  # placeholder internals
        "中文——中文 · 中文",  # correct dash and middle dot untouched
    ],
)
def test_leaves_technical_fragments_alone(text: str):
    assert punct.convert(text) == text


def test_url_stays_intact_when_followed_by_chinese_bracket():
    assert punct.convert("https://api.openai.com/v1(默认)") == "https://api.openai.com/v1（默认）"


def _run(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args], capture_output=True, text=True, check=False
    )


def test_cli_check_fails_and_lists_keys_then_fix_makes_it_pass(tmp_path: Path):
    locale = tmp_path / "common.json"
    data = {
        "evalPage": {"runs": {"failureReason": "失败原因:"}},
        "knowledge": {"source": {"prefix": "前缀(可选)"}},
        "clean": {"ok": "失败原因：", "tech": "arn:aws:s3files:…:file-system/…"},
    }
    locale.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    check = _run(["--check", "--file", str(locale)])
    assert check.returncode == 1
    assert "evalPage.runs.failureReason" in check.stdout
    assert "knowledge.source.prefix" in check.stdout
    assert "clean.ok" not in check.stdout
    assert "clean.tech" not in check.stdout

    fix = _run(["--fix", "--file", str(locale)])
    assert fix.returncode == 0, fix.stdout + fix.stderr
    fixed = json.loads(locale.read_text(encoding="utf-8"))
    assert fixed["evalPage"]["runs"]["failureReason"] == "失败原因："
    assert fixed["knowledge"]["source"]["prefix"] == "前缀（可选）"
    assert fixed["clean"] == data["clean"]
    # key order and layout are preserved: only the punctuation moved
    assert list(fixed) == list(data)

    assert _run(["--check", "--file", str(locale)]).returncode == 0


def test_fix_refuses_non_canonical_layout(tmp_path: Path):
    locale = tmp_path / "common.json"
    locale.write_text('{"a": "失败原因:"}', encoding="utf-8")  # not indent=2
    assert _run(["--fix", "--file", str(locale)]).returncode == 2
    assert locale.read_text(encoding="utf-8") == '{"a": "失败原因:"}'


def test_checked_in_zh_locale_is_clean():
    assert _run(["--check", "--file", str(ZH_LOCALE)]).returncode == 0
