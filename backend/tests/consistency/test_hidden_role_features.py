"""rev-r25 follow-up on N17-obs252: `Settings.hidden_role_features` is the server-side twin
of the console's hidden surfaces — `frontend/src/v2/hiddenSurfaces.ts::HIDDEN_SURFACES`:
`features` (whole features the console drops, e.g. `skill_lab`) and `createEntries.studio`
(the Strands Studio entry). A hidden role is not a registry referrer and loses its override
when its model dies (`services/model_roles.role_hidden` / `on_model_unavailable`), so the two
sides must agree: showing Skill Lab or Studio in the console again without updating the
setting (or hiding one without listing it) fails here, not in production. The TS file is read
as text, like the other twins under `tests/consistency`. (r17-s2 / N17-obs285 merged the
console's three flag files into `hiddenSurfaces.ts`; this twin follows it.)
"""
from __future__ import annotations

import re
from pathlib import Path

from app.core.config import Settings
from app.services import model_roles

ROOT = Path(__file__).resolve().parents[3]
HIDDEN_SURFACES = ROOT / "frontend" / "src" / "v2" / "hiddenSurfaces.ts"


def _section(name: str) -> str:
    """The text of one top-level section of `HIDDEN_SURFACES` (`name: [...]` / `name: {...}`)."""
    text = HIDDEN_SURFACES.read_text(encoding="utf-8")
    body = re.search(r"export const HIDDEN_SURFACES\s*=\s*\{(.*)\}\s*as const;", text, re.S)
    assert body, "hiddenSurfaces.ts no longer declares HIDDEN_SURFACES"
    match = re.search(rf"^\s*{name}:\s*(\[[^\]]*\]|\{{[^}}]*\}})", body.group(1), re.M)
    assert match, f"HIDDEN_SURFACES no longer has a `{name}` section"
    return match.group(1)


def _console_hidden_features() -> set[str]:
    return set(re.findall(r'"([a-z_]+)"', _section("features")))


def _console_studio_shown() -> bool:
    match = re.search(r"^\s*studio:\s*(true|false)\s*,", _section("createEntries"), re.M)
    assert match, "HIDDEN_SURFACES.createEntries no longer declares `studio`"
    return match.group(1) == "true"


def test_the_parser_reads_the_shipped_values():
    """Guards the regexes themselves: today's build hides Skill Lab and the Studio entry."""
    assert _console_hidden_features() == {"skill_lab"}
    assert _console_studio_shown() is False


def test_hidden_role_features_mirror_the_console():
    expected = _console_hidden_features() | ({"studio"} if not _console_studio_shown() else set())
    configured = set(Settings.model_fields["hidden_role_features"].default)
    assert configured == expected, (
        f"Settings.hidden_role_features {sorted(configured)} must equal the console's hidden "
        f"surfaces {sorted(expected)} (hiddenSurfaces.ts HIDDEN_SURFACES.features + "
        "createEntries.studio) — update core/config.py in the same commit"
    )


def test_every_role_feature_is_a_known_console_surface():
    """A role may only be tagged with a feature the console can hide: a backend feature
    (`core.features.FEATURES`) or the Studio entry."""
    from app.core.features import FEATURES

    for role in model_roles.ROLES:
        if role.feature is not None:
            assert role.feature in FEATURES or role.feature == "studio", role
