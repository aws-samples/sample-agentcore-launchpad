"""The workshop screenshot recapture script: its pure parts and its manifest.

The capture itself drives a real console and is run by hand; these tests pin what
decides *which* shots run and how a manifest entry is read.
"""

from __future__ import annotations

import contextlib
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "backend" / "scripts" / "capture_workshop_screens.py"
MANIFEST = ROOT / "docs" / "workshop-screens.yaml"


def _load():
    spec = importlib.util.spec_from_file_location("capture_workshop_screens", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve their module by name
    spec.loader.exec_module(module)
    return module


cws = _load()


def _ctx():
    return cws.Ctx(url="https://console.example", passwords={},
                   agents={"lab-service-assistant": "a1"}, users={"biz-owner": "u1"},
                   facts={"lab-service-assistant": {"criteria_signed": True, "api_keys": 0}})


def test_page_helpers_load_as_bare_functions():
    for name in ("OVERLAY_JS", "UNCLAMP_JS", "PROBE_JS"):
        js = getattr(cws, name)
        assert js.lstrip().startswith("("), name
        assert "// @" not in js


def test_crop_rect_grows_to_every_drawn_badge():
    rect = {"x": 100, "y": 100, "w": 200, "h": 50}
    drawn = [{"x": 96, "y": 96, "w": 208, "h": 58},          # the frame
             {"x": 304, "y": 108, "w": 34, "h": 34},          # a 'right' badge
             {"x": 50, "y": 60, "w": 34, "h": 34}]            # a 'left' / 'top' one
    assert cws.crop_rect(rect, drawn) == {"x": 50, "y": 60, "w": 288, "h": 94}


@contextlib.contextmanager
def _chromium(**page_opts):
    """A headless Chromium page, or a skip when this host has none.

    ``sync_playwright().start()`` marks an asyncio loop as running on this thread until
    ``stop()``; skipping without it (no browser installed) leaves the loop behind, and
    every later ``asyncio.run()`` in the same worker fails. So ``stop()`` always runs.
    """
    sync_api = pytest.importorskip("playwright.sync_api")
    try:
        pw = sync_api.sync_playwright().start()
    except Exception as exc:  # noqa: BLE001 - no Playwright driver on this host
        pytest.skip(f"no headless Chromium: {exc}")
    try:
        try:
            browser = pw.chromium.launch()
        except Exception as exc:  # noqa: BLE001 - no browser on this host
            pytest.skip(f"no headless Chromium: {exc}")
        try:
            yield browser.new_page(**page_opts)
        finally:
            browser.close()
    finally:
        pw.stop()


def test_overlay_badges_stay_on_the_page_and_are_reported():
    """In a real page: every badge position is inside the document, and the returned
    rects cover each drawn element whole (what the crop is grown to)."""
    with _chromium(viewport={"width": 400, "height": 300}) as page:
        page.set_content("<body style='margin:0'><div style='height:300px'></div></body>")
        boxes = [{"n": 1, "rect": {"x": 0, "y": 0, "w": 60, "h": 20}},                # corner
                 {"n": 2, "rect": {"x": 360, "y": 100, "w": 40, "h": 20}, "badge": "right"},
                 {"n": 3, "rect": {"x": 4, "y": 150, "w": 40, "h": 20}, "badge": "left"},
                 {"n": 4, "rect": {"x": 100, "y": 2, "w": 40, "h": 20}, "badge": "top"},
                 {"n": 5, "rect": {"x": 200, "y": 200, "w": 40, "h": 20}, "badge": "bottom"}]
        drawn = page.evaluate(cws.OVERLAY_JS, {"boxes": boxes, "pad": 4})
        badges = page.evaluate(
            "() => [...document.querySelectorAll('#__ws_overlay > div')]"
            ".filter(d => d.textContent).map(d => d.getBoundingClientRect().toJSON())")
        assert len(badges) == 5 and len(drawn) == 10
        # 'bottom' sits below its frame (the frame ends at y 200 + 20 + the pad)
        assert badges[4]["top"] >= 224, badges[4]
        for b in badges:
            assert b["left"] >= 0 and b["top"] >= 0 and b["right"] <= 400, b
            assert any(d["x"] <= b["left"] and d["y"] <= b["top"]
                       and d["x"] + d["w"] >= b["right"] and d["y"] + d["h"] >= b["bottom"]
                       for d in drawn), b


def test_a_host_without_chromium_skips_and_still_stops_playwright(monkeypatch):
    """The skip path stops the driver too (a leaked running loop broke every later
    ``asyncio.run()`` in the xdist worker on a host with Playwright but no browser)."""
    sync_api = pytest.importorskip("playwright.sync_api")
    stopped: list[bool] = []

    def launch():
        raise RuntimeError("Executable doesn't exist")

    pw = SimpleNamespace(chromium=SimpleNamespace(launch=launch),
                         stop=lambda: stopped.append(True))
    monkeypatch.setattr(sync_api, "sync_playwright", lambda: SimpleNamespace(start=lambda: pw))
    with pytest.raises(pytest.skip.Exception, match="no headless Chromium"):
        with _chromium():
            pass
    assert stopped == [True]


def test_per_lang_picks_the_language_and_falls_back_to_english():
    assert cws.per_lang({"zh": "登录", "en": "Sign in"}, "zh") == "登录"
    assert cws.per_lang({"en": "Sign in"}, "zh") == "Sign in"
    assert cws.per_lang("plain", "zh") == "plain"
    # a dict that is not a language pair is passed through
    assert cws.per_lang({"n": 1}, "zh") == {"n": 1}


def test_expand_names_to_ids_and_skips_a_missing_name():
    ctx = _ctx()
    path = cws.expand("/v3/agents?id={agent:lab-service-assistant}", ctx, "zh")
    assert path == "/v3/agents?id=a1"
    with pytest.raises(cws.SkipShot):
        cws.expand("/v3/agents?id={agent:nope}", ctx, "zh")


@pytest.mark.parametrize("requires, skips", [
    (["agent:lab-service-assistant"], False),
    (["agent:lab-policy-advisor"], True),
    (["user:biz-owner"], False),
    (["fact:lab-service-assistant.criteria_signed"], False),
    (["fact:lab-service-assistant.criteria_signed=true"], False),
    (["fact:lab-service-assistant.api_keys"], True),
    (["state:a sealed golden set"], True),
])
def test_requires(requires, skips):
    shot = {"name": "x", "requires": requires}
    if skips:
        with pytest.raises(cws.SkipShot):
            cws.check_requires(shot, _ctx())
    else:
        cws.check_requires(shot, _ctx())


def test_base_storage_defaults_and_overrides():
    store = cws.base_storage("en", {"storage": {"lp.v3.create.chooser": None}})
    assert store["launchpad_v3_theme"] == "dark"
    assert store["i18nextLng"] == "en"
    assert store["launchpad_v3_tour_done"] == "1"
    assert store["lp.v3.create.chooser"] is None


def test_manifest_is_well_formed():
    shots = yaml.safe_load(MANIFEST.read_text())["shots"]
    names = [s["name"] for s in shots]
    assert len(names) == len(set(names))
    accounts = set(cws.PASSWORD_OUTPUTS) | {"anon"}
    for shot in shots:
        assert shot["status"] in {"keep", "replace", "new"}, shot["name"]
        if shot.get("manual"):
            continue
        # an account the walk itself created signs in from LAUNCHPAD_WS_PASSWORD_<ACCOUNT>
        assert shot["account"] in accounts or shot.get("walk_account"), shot["name"]
        # a path is one string, or one per language ({zh: ..., en: ...})
        paths = shot["path"].values() if isinstance(shot["path"], dict) else [shot["path"]]
        assert all(p.startswith("/") for p in paths), shot["name"]
        numbers = [h["n"] for h in shot.get("highlights", []) if h.get("n") is not None]
        # badges follow the caption: 1, 2, 3 ... with no gap
        assert numbers == list(range(1, len(numbers) + 1)), shot["name"]


def test_assume_state_satisfies_state_but_not_missing_agents():
    ctx = _ctx()
    ctx.assume_state = True
    cws.check_requires({"name": "x", "requires": ["state:a sealed golden set"]}, ctx)
    with pytest.raises(cws.SkipShot):
        cws.check_requires({"name": "x", "requires": ["state:fresh", "agent:nope"]}, ctx)


def test_relabel_shows_the_workshop_region_in_text_and_inputs():
    with _chromium() as page:
        page.set_content("<body><span>Default · us-west-2</span><b>US-WEST-2</b>"
                         "<input value='arn:aws:bedrock:us-west-2:1:x'><i>us-east-1</i></body>")
        n = page.evaluate(cws.RELABEL_JS, {"from": "us-west-2", "to": "us-east-1"})
        assert n == 3
        assert page.locator("span").inner_text() == "Default · us-east-1"
        assert page.locator("b").inner_text() == "US-EAST-1"
        assert page.locator("input").input_value() == "arn:aws:bedrock:us-east-1:1:x"


def test_relabel_pairs_cover_the_region_and_its_az_ids():
    assert cws.relabel_pairs("us-west-2", "us-east-1") == [
        ("us-west-2", "us-east-1"), ("usw2-az", "use1-az")]
    assert cws.relabel_pairs("us-east-1", "us-east-1")[1:] == []
    assert cws._az_prefix("ap-southeast-2") == "apse2"
