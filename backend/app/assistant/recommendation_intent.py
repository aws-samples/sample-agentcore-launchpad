"""Recommendation entries of a proposal: their text and their **gate intent**.

A proposal's ``evaluator_recommendations`` are prose by origin; the member reads them, and
the evaluation plan (``evaluation_plan.py``) turns them into evaluator entries. Two
facts an architect states in a recommendation decide how an evaluator is shown and
reviewed: whether it **blocks publishing** and at what **threshold**. WB-006 (2026-10-14):
"``hr-guardrails-judge`` must block publishing at threshold 1.00" drafted into an entry
``{"blocking": false, "threshold": null}`` shown as *informational*, and the plan
validated. This module is the single place that reads that intent, from either source:

* **structured** — an entry may be an object ``{"text", "evaluator_id"?, "blocking"?,
  "threshold"?, "metric"?}`` (the shape the architect skill states; WB-009 made the
  architect's object arrays valid instead of a shape error);
* **parsed** — a plain string is scanned for EN / ZH markers ("blocking", "block
  publishing", "gate", "阻断", "门禁"; "threshold 1.00", "阈值 1.00", "≥ 0.9", "至少
  0.9"; "metric: …", "指标：…"). Structured members always win over the prose.

``normalise_entry`` additionally repairs the common *non-canonical* object forms the
architect emits (``{name|title|recommendation|evaluator: …}``) into the canonical shape,
recording a ``proposal_repaired`` note on the entry — information is folded into the
text, never dropped. Pure functions; nothing here touches AWS or the ledger.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# The members a canonical object entry may carry (``note`` is the platform's repair note
# and is accepted so a stored revision round-trips through a member edit).
ACCEPTED_MEMBERS = ("text", "evaluator_id", "blocking", "threshold", "metric", "note")
RECOMMENDATION_SHAPE_RULE = (
    "must be a non-empty sentence (at most 1,000 characters) or an object "
    '{"text": "<the recommendation>", "evaluator_id"?: "<Builtin.* id or seed evaluator '
    'key>", "blocking"?: true|false, "threshold"?: 0–1, "metric"?: "<name>"}'
)
REPAIR_NOTE_PREFIX = "proposal_repaired"

# Aliases the normaliser accepts, in priority order (first present wins).
_TEXT_KEYS = ("text", "recommendation", "description", "rationale", "reason", "summary",
              "detail", "details", "why", "purpose")
_NAME_KEYS = ("name", "title", "evaluator", "evaluator_name", "label", "scorer")
_ID_KEYS = ("evaluator_id", "evaluatorId", "id")
_BLOCKING_KEYS = ("blocking", "gate", "gating", "is_blocking", "hard_gate", "required",
                  "mandatory", "block", "blocks_publishing")
_THRESHOLD_KEYS = ("threshold", "min_score", "minimum_score", "pass_threshold",
                   "suggested_threshold", "target", "target_score", "pass_rate")
_METRIC_KEYS = ("metric", "dimension", "measure", "signal", "input_signal")

# An evaluator reference as the plan can resolve it: a catalog id (``Builtin.Refusal``),
# or a seed evaluator key / name (``hr-guardrails-judge`` / ``hr_guardrails_judge``).
_ID_LIKE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,95}$")
_CATALOG_ID_RE = re.compile(r"^(?:Builtin|ThirdParty)\.[A-Za-z0-9_.]+$")

_NUMBER = r"(\d+(?:\.\d+)?)\s*(%|％)?"
# Explicit negations come first: "non-blocking", "not a gate", "不阻断", "仅供参考".
_NOT_BLOCKING_RE = re.compile(
    r"non-?\s?blocking|not\s+(?:a\s+)?(?:blocking|gate|gating)|no\s+gate|informational|"
    r"advisory[- ]only|advisory\b|avoid\s+blocking|without\s+blocking|never\s+blocks?\b|"
    r"not\s+block\b|不阻断|非阻断|避免阻断|不要阻断|防止阻断|不应阻断|不会阻断|不作为门禁|非门禁|"
    r"仅供参考|仅作参考|参考性",
    re.IGNORECASE,
)
# Positive markers are deliberately narrow (rev-r8 on cn/r9-d): a gate is a release /
# publishing decision, so bare "阻断" (a user flow may be "阻断") and bare "must pass"
# (a transcript "must pass" as context) are NOT gates — 阻断 needs 发布 / 上线 / 部署,
# "must pass" a gate / score / release noun; 门禁 alone is a gate.
_BLOCKING_RE = re.compile(
    r"\bblocking\b|\bblocks?\s+(?:the\s+)?(?:publish(?:ing)?|publication|release|"
    r"deploy(?:ment)?|promotion|rollout|go-live)|\bblocked\s+(?:from\s+)?(?:publish|release)|"
    r"\b(?:hard|release|publish(?:ing)?|regression|quality|safety)?\s?gate\b|\bgating\b|"
    r"\bmust\s+pass\s+(?:before\b|at\s+(?:least\s+)?\d|with\s+(?:a\s+)?(?:score|threshold)|"
    r"(?:the\s+)?(?:gate|threshold|bar)\b)|\bgo/no-go\b|"
    r"阻断(?:发布|上线|部署)|阻塞发布|拦截发布|门禁|必须通过(?=.{0,6}(?:评估|评测|门禁|阈值|测试))|"
    r"不得发布|禁止发布",
    re.IGNORECASE,
)
# A threshold next to its keyword ("threshold 90", "阈值 0.9") may be a bare integer read as
# a percentage; a BARE comparison ("≥ N", "at least N", "至少 N") is a score only when the
# number is a decimal ≤ 1, carries %, or sits next to a score noun — "at least 10 golden
# tests" and "≥ 3 tool calls" are counts, never thresholds.
_KEYWORD_THRESHOLD_RES = (
    re.compile(r"(?:threshold|cut-?off|minimum\s+score|min\.?\s+score|pass\s+(?:rate|score))"
               r"\s*(?:of|at|is|:|=|≥|>=|at\s+least)?\s*" + _NUMBER, re.IGNORECASE),
    re.compile(r"(?:阈值|门限|及格线|通过率|最低分|最低得分)\s*(?:为|是|设为|:|：|≥|>=|不低于|至少)?\s*"
               + _NUMBER),
)
_SCORE_NOUN = r"(?:\s*(?:score|rate|points?|threshold|分|率|阈值))?"
_BARE_THRESHOLD_RES = (
    re.compile(r"(?:≥|>=|⩾)\s*" + _NUMBER + _SCORE_NOUN, re.IGNORECASE),
    re.compile(r"(?:at\s+least|not\s+(?:less|lower)\s+than|no\s+(?:less|lower)\s+than|"
               r"minimum\s+of)\s*" + _NUMBER + _SCORE_NOUN, re.IGNORECASE),
    re.compile(r"(?:至少|不低于|不少于|不小于)\s*" + _NUMBER + _SCORE_NOUN),
)
_SCORE_NOUN_RE = re.compile(r"score|rate|points?|threshold|分|率|阈值", re.IGNORECASE)
_METRIC_RES = (
    re.compile(r"\bmetric\s*[:=]\s*[\"'“]?([A-Za-z][\w .%/-]{1,60}?)[\"'”]?(?=[,;.)]|\s+(?:at|with|"
               r"threshold|≥|>=)|$)", re.IGNORECASE),
    re.compile(r"指标\s*[:：]\s*[\"'“「]?([^\s，,;；。）)]{1,30})"),
)
_METRIC_LEXICON = (
    ("pass rate", re.compile(r"\bpass\s+rate\b", re.IGNORECASE)),
    ("refusal rate", re.compile(r"\brefusal\s+rate\b", re.IGNORECASE)),
    ("accuracy", re.compile(r"\baccuracy\b", re.IGNORECASE)),
    ("mean score", re.compile(r"\b(?:mean|average)\s+score\b", re.IGNORECASE)),
    ("通过率", re.compile(r"通过率")),
    ("拒答率", re.compile(r"拒答率")),
    ("准确率", re.compile(r"准确率")),
)
_TEXT_MAX = 1000


def _threshold_value(raw: Any, *, bare: bool = False) -> float | None:
    """A threshold as the plan stores it (0..1): numbers, numeric strings, percentages;
    a bare number in (1, 100] is read as a percentage (``90`` → 0.9) — only next to a
    ``threshold`` keyword / member, never in a ``bare`` comparison, where the number must
    be a decimal ≤ 1, a percentage, or sit next to a score noun. ``None`` when the value
    cannot be a score."""
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int | float):
        value = float(raw)
        percent = False
        decimal = not float(raw).is_integer()
        noun = False
    elif isinstance(raw, str):
        m = re.search(_NUMBER, raw)
        if not m:
            return None
        value = float(m.group(1))
        percent = bool(m.group(2)) or "%" in raw
        decimal = "." in m.group(1)
        noun = bool(_SCORE_NOUN_RE.search(raw[m.end():]))
    else:
        return None
    if bare and not (percent or noun or (decimal and value <= 1.0)):
        return None
    if percent:
        value = value / 100.0
    elif value > 1.0 and value <= 100.0 and (not bare or noun):
        value = value / 100.0  # "threshold 90" / "≥ 90 points" are percentages
    if 0.0 <= value <= 1.0:
        return round(value, 4)
    return None


def _blocking_value(raw: Any) -> bool | None:
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        if _NOT_BLOCKING_RE.search(raw):
            return False
        if _BLOCKING_RE.search(raw) or raw.strip().lower() in ("true", "yes", "是", "required"):
            return True
        if raw.strip().lower() in ("false", "no", "否", "optional"):
            return False
    return None


@dataclass
class Intent:
    """What one recommendation says about a gate: ``blocking`` (``True`` / explicit
    ``False`` / unstated ``None``), ``threshold`` (0..1), ``metric``, the evaluator it
    names, and where each fact came from (``stated`` members or ``parsed`` prose)."""

    text: str = ""
    evaluator_id: str | None = None
    blocking: bool | None = None
    threshold: float | None = None
    metric: str | None = None
    source: str | None = None  # "stated" | "parsed" | None
    markers: list[str] = field(default_factory=list)

    @property
    def has_intent(self) -> bool:
        """A gate the plan must carry: blocking, or a stated threshold."""
        return self.blocking is True or self.threshold is not None

    def describe(self) -> str:
        """``blocking · threshold 1`` — the wording the UI and the errors share."""
        parts: list[str] = []
        if self.blocking is True:
            parts.append("blocking")
        if self.threshold is not None:
            parts.append(f"threshold {self.threshold:g}")
        if self.metric:
            parts.append(f"metric {self.metric}")
        return " · ".join(parts)


def parse_intent(text: str) -> Intent:
    """The EN / ZH prose fallback: markers → ``Intent`` with ``source="parsed"`` when any
    fact was found. Negations ("non-blocking", "仅供参考") win over the positive markers."""
    out = Intent(text=text)
    if not isinstance(text, str) or not text:
        return out
    if _NOT_BLOCKING_RE.search(text):
        out.blocking = False
        out.markers.append("not-blocking")
    else:
        m = _BLOCKING_RE.search(text)
        if m:
            out.blocking = True
            out.markers.append(m.group(0).strip())
    for bare, patterns in ((False, _KEYWORD_THRESHOLD_RES), (True, _BARE_THRESHOLD_RES)):
        for rx in patterns:
            m = rx.search(text)
            if m:
                value = _threshold_value(m.group(0), bare=bare)
                if value is not None:
                    out.threshold = value
                    out.markers.append(m.group(0).strip())
                    break
        if out.threshold is not None:
            break
    if out.blocking is not None or out.threshold is not None:
        out.source = "parsed"
    # a metric is only meaningful next to a gate: without one, "accuracy" in a sentence is
    # ordinary language, not a measurement rule (rev-r8 nit A)
    if out.blocking is True or out.threshold is not None:
        for rx in _METRIC_RES:
            m = rx.search(text)
            if m:
                out.metric = m.group(1).strip()[:200]
                break
        if out.metric is None:
            for label, rx in _METRIC_LEXICON:
                if rx.search(text):
                    out.metric = label
                    break
    return out


def recommendation_text(entry: Any) -> str:
    """The sentence of an entry — the string itself, or an object's ``text``."""
    if isinstance(entry, str):
        return entry
    if isinstance(entry, dict) and isinstance(entry.get("text"), str):
        return entry["text"]
    return str(entry)


def resolve_intent(entry: Any) -> Intent:
    """Structured members first, prose fills the gaps. ``source`` is ``stated`` when any
    gate member was given on the object, ``parsed`` when the prose supplied it, ``None``
    when the recommendation says nothing about a gate."""
    text = recommendation_text(entry)
    parsed = parse_intent(text)
    if not isinstance(entry, dict):
        return parsed
    out = Intent(text=text, markers=list(parsed.markers))
    evaluator_id = entry.get("evaluator_id")
    out.evaluator_id = evaluator_id.strip() if isinstance(evaluator_id, str) and \
        evaluator_id.strip() else None
    stated = False
    blocking = entry.get("blocking")
    if isinstance(blocking, bool):
        out.blocking, stated = blocking, True
    else:
        out.blocking = parsed.blocking
    threshold = _threshold_value(entry.get("threshold")) \
        if entry.get("threshold") is not None else None
    if threshold is not None:
        out.threshold, stated = threshold, True
    else:
        out.threshold = parsed.threshold
    metric = entry.get("metric")
    if isinstance(metric, str) and metric.strip():
        out.metric = metric.strip()[:200]
    else:
        out.metric = parsed.metric
    if stated:
        out.source = "stated"
    elif parsed.source:
        out.source = "parsed"
    return out


def _first_str(obj: dict[str, Any], keys: tuple[str, ...]) -> tuple[str | None, str | None]:
    for k in keys:
        v = obj.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip(), k
    return None, None


def _first_present(obj: dict[str, Any], keys: tuple[str, ...]) -> tuple[Any, str | None]:
    for k in keys:
        if k in obj and obj[k] is not None:
            return obj[k], k
    return None, None


def _scalar(v: Any) -> str | None:
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, int | float):
        return f"{v:g}"
    if isinstance(v, str):
        return v.strip() or None
    if isinstance(v, list) and all(isinstance(x, str | int | float) for x in v):
        return ", ".join(str(x) for x in v) or None
    return None


def normalise_entry(raw: Any, index: int) -> tuple[Any, str | None]:
    """One ``evaluator_recommendations`` entry as the contract stores it.

    Returns ``(entry, error)``: a string is returned verbatim; a canonical object (only
    ``ACCEPTED_MEMBERS``, a string ``text``) is returned untouched; any other object is
    rebuilt from its aliases — text from ``text|recommendation|description|…``, the
    evaluator from ``name|title|evaluator`` (kept as ``evaluator_id`` when it looks like
    an id, folded into the text otherwise), ``blocking`` / ``threshold`` / ``metric`` from
    their aliases, every other scalar member folded into the text as ``k: v`` — and carries
    ``note: "proposal_repaired: …"`` saying what was read from where. An entry that cannot
    be read (no string anywhere, a number, a list) is returned as-is with a readable
    error so the proposal becomes an *invalid* revision."""
    where = f"evaluator_recommendations[{index}]"
    if isinstance(raw, str):
        return raw, None
    if not isinstance(raw, dict):
        return raw, f"{where}: {RECOMMENDATION_SHAPE_RULE}"
    keys = list(raw.keys())
    if set(keys) <= set(ACCEPTED_MEMBERS) and isinstance(raw.get("text"), str) \
            and raw["text"].strip():
        return raw, None
    text, text_key = _first_str(raw, _TEXT_KEYS)
    name, name_key = _first_str(raw, _NAME_KEYS)
    evaluator_id, id_key = _first_str(raw, _ID_KEYS)
    reads: list[str] = []
    folded: list[str] = []
    if evaluator_id is not None and not _ID_LIKE_RE.match(evaluator_id):
        folded.append(f"{id_key}: {evaluator_id}")
        evaluator_id, id_key = None, None
    if evaluator_id is None and name is not None and _ID_LIKE_RE.match(name) \
            and (_CATALOG_ID_RE.match(name) or "-" in name or "_" in name or "." in name):
        evaluator_id, id_key = name, name_key
        name, name_key = None, None
    if text is None and name is None and evaluator_id is None:
        # nothing that reads as a recommendation
        return raw, f"{where}: {RECOMMENDATION_SHAPE_RULE}"
    if text_key:
        reads.append(f"text from `{text_key}`" if text_key != "text" else "text")
    if id_key:
        reads.append(f"evaluator_id from `{id_key}`")
    blocking_raw, blocking_key = _first_present(raw, _BLOCKING_KEYS)
    blocking = _blocking_value(blocking_raw) if blocking_key else None
    if blocking_key and blocking is None:
        folded.append(f"{blocking_key}: {_scalar(blocking_raw)}")
    elif blocking_key and blocking_key != "blocking":
        reads.append(f"blocking from `{blocking_key}`")
    threshold_raw, threshold_key = _first_present(raw, _THRESHOLD_KEYS)
    threshold = _threshold_value(threshold_raw) if threshold_key else None
    if threshold_key and threshold is None:
        folded.append(f"{threshold_key}: {_scalar(threshold_raw)}")
    elif threshold_key and threshold_key != "threshold":
        reads.append(f"threshold from `{threshold_key}`")
    metric, metric_key = _first_str(raw, _METRIC_KEYS)
    if metric_key and metric_key != "metric":
        reads.append(f"metric from `{metric_key}`")
    used = {text_key, name_key, id_key, blocking_key, threshold_key, metric_key, "note"}
    for k in keys:
        if k in used:
            continue
        scalar = _scalar(raw[k])
        folded.append(f"{k}: {scalar}" if scalar is not None else f"{k}: (dropped)")
    head = text or ""
    if name and name not in head:
        head = f"{name} — {head}" if head else name
    if evaluator_id and evaluator_id not in head:
        head = f"{evaluator_id}: {head}" if head else evaluator_id
    extras = "; ".join(f for f in folded if not f.endswith("(dropped)"))
    if extras:
        head = f"{head} ({extras})" if head else extras
    head = head[:_TEXT_MAX]
    entry: dict[str, Any] = {"text": head}
    if evaluator_id:
        entry["evaluator_id"] = evaluator_id
    if blocking is not None:
        entry["blocking"] = blocking
    if threshold is not None:
        entry["threshold"] = threshold
    if metric:
        entry["metric"] = metric[:200]
    note_bits = list(reads)
    folded_keys = [f.split(":", 1)[0] for f in folded]
    if folded_keys:
        note_bits.append("members folded into the text: " + ", ".join(folded_keys))
    existing_note = raw.get("note")
    note = f"{REPAIR_NOTE_PREFIX}: object entry normalised"
    if note_bits:
        note += " — " + "; ".join(note_bits)
    if isinstance(existing_note, str) and existing_note.strip():
        note = f"{existing_note.strip()} | {note}"
    entry["note"] = note[:_TEXT_MAX]
    return entry, None


def normalise_entries(raw: Any) -> tuple[Any, list[str], bool]:
    """``normalise_entry`` over a whole list: ``(entries, errors, changed)``. A non-list
    is returned untouched (the schema reports it)."""
    if not isinstance(raw, list):
        return raw, [], False
    out: list[Any] = []
    errors: list[str] = []
    changed = False
    for i, item in enumerate(raw):
        entry, error = normalise_entry(item, i)
        if error:
            errors.append(error)
        if entry is not item:
            changed = True
        out.append(entry)
    return (out if changed else raw), errors, changed


def dump_entry(entry: Any) -> Any:
    """The stored form of an entry: strings verbatim; objects without ``None`` members
    (a revision stays readable and hashes the same whichever optional members a later
    edit leaves out)."""
    if isinstance(entry, dict):
        return {k: v for k, v in entry.items() if v is not None}
    return entry
