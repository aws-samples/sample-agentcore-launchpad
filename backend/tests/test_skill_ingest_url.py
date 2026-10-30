"""URL skill ingestion: bundle_from_url raw-md vs zip detection, the https
guard, undecodable-body rejection, the per-skill download cap, and the inspect
JSON url branch. No network — the fetcher seam (_fetch_url) is monkeypatched."""

import io
import zipfile

import httpx
import pytest

from app.services import skill_ingest as si


def _md(name: str = "url-skill", description: str = "d", version: str = "0.1.0") -> str:
    return f"---\nname: {name}\ndescription: {description}\nversion: {version}\n---\n# {name}\n"


def _zip_bytes(entries: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, content in entries.items():
            zf.writestr(name, content)
    return buf.getvalue()


def _stub_fetch(monkeypatch, data: bytes, content_type: str, capture: dict | None = None):
    def fake_fetch(url, max_bytes=si.SKILL_BUNDLE_MAX_BYTES):
        if capture is not None:
            capture["max_bytes"] = max_bytes
            capture["url"] = url
        return data, content_type

    monkeypatch.setattr(si, "_fetch_url", fake_fetch)


# ---------- acquirer: raw md vs zip detection ----------

def test_bundle_from_url_raw_md(monkeypatch):
    cap: dict = {}
    _stub_fetch(monkeypatch, _md("raw-skill").encode(), "text/markdown", cap)
    bundle = si.bundle_from_url("https://example.com/SKILL.md")
    try:
        assert bundle.name == "raw-skill"
        assert bundle.files == ["SKILL.md"]
        assert bundle.source.kind == "url"
        assert bundle.source.url == "https://example.com/SKILL.md"
        # the fetcher is handed the per-skill 50MB cap (this is one bundle, not a repo)
        assert cap["max_bytes"] == si.SKILL_BUNDLE_MAX_BYTES
    finally:
        bundle.close()


def test_bundle_from_url_zip_by_content_type(monkeypatch):
    _stub_fetch(
        monkeypatch,
        _zip_bytes({"SKILL.md": _md("z"), "scripts/x.py": "x\n"}),
        "application/zip",
    )
    bundle = si.bundle_from_url("https://example.com/download")
    try:
        assert bundle.name == "z"
        assert bundle.files == ["SKILL.md", "scripts/x.py"]
        assert bundle.source.kind == "url"
    finally:
        bundle.close()


def test_bundle_from_url_zip_by_magic_bytes(monkeypatch):
    # content-type lies (octet-stream) and the url has no .zip suffix → PK magic wins
    _stub_fetch(monkeypatch, _zip_bytes({"SKILL.md": _md("m")}), "application/octet-stream")
    bundle = si.bundle_from_url("https://example.com/blob")
    try:
        assert bundle.name == "m"
        assert bundle.source.kind == "url"
    finally:
        bundle.close()


def test_bundle_from_url_zip_by_suffix(monkeypatch):
    _stub_fetch(monkeypatch, _zip_bytes({"SKILL.md": _md("s")}), "")
    bundle = si.bundle_from_url("https://example.com/pack.zip")
    try:
        assert bundle.name == "s"
        assert bundle.source.kind == "url"
    finally:
        bundle.close()


# ---------- guards ----------

def test_bundle_from_url_rejects_non_https():
    for bad in ("http://example.com/x", "file:///tmp/x", "ftp://h/x", ""):
        with pytest.raises(si.SkillValidationError, match="https"):
            si.bundle_from_url(bad)


def test_bundle_from_url_rejects_undecodable_body(monkeypatch):
    # neither a zip (no content-type/suffix/magic) nor valid utf-8 text
    _stub_fetch(monkeypatch, b"\xff\xfe\x00\x01rubbish", "application/octet-stream")
    with pytest.raises(si.SkillValidationError, match="UTF-8"):
        si.bundle_from_url("https://example.com/blob")


def test_bundle_from_url_oversized_propagates(monkeypatch):
    # the fetcher enforces the streamed cap; bundle_from_url surfaces its error,
    # and the fetcher is called with the per-skill cap
    def boom(url, max_bytes=si.SKILL_BUNDLE_MAX_BYTES):
        assert max_bytes == si.SKILL_BUNDLE_MAX_BYTES
        raise si.SkillValidationError(f"download exceeds the {max_bytes} byte limit")

    monkeypatch.setattr(si, "_fetch_url", boom)
    with pytest.raises(si.SkillValidationError, match="exceeds"):
        si.bundle_from_url("https://example.com/huge.zip")


# ---------- SSRF guard (initial request + every redirect hop) ----------

# IP literals so getaddrinfo resolves offline (no DNS/network in the suite).
@pytest.mark.parametrize(
    "url",
    [
        "https://169.254.169.254/latest/meta-data/",  # cloud metadata (link-local)
        "https://127.0.0.1/skill.zip",  # loopback
        "https://10.0.0.5/skill.zip",  # RFC1918 private
        "https://localhost/skill.zip",  # blocklisted host name
        "https://foo.internal/skill.zip",  # .internal suffix
    ],
)
def test_assert_public_url_rejects_internal(url):
    with pytest.raises(si.SkillValidationError):
        si._assert_public_url(url)


def test_assert_public_url_allows_public():
    si._assert_public_url("https://8.8.8.8/skill.zip")  # public IP literal → no raise


def test_guarding_transport_blocks_before_delegating():
    def must_not_be_called(_req):  # pragma: no cover - asserts it isn't reached
        raise AssertionError("inner transport reached for a blocked host")

    guard = si._GuardingTransport(httpx.MockTransport(must_not_be_called))
    with pytest.raises(si.SkillValidationError):
        guard.handle_request(httpx.Request("GET", "https://169.254.169.254/x"))


def test_guarding_transport_delegates_for_public_host():
    guard = si._GuardingTransport(
        httpx.MockTransport(lambda _req: httpx.Response(200, content=b"ok"))
    )
    resp = guard.handle_request(httpx.Request("GET", "https://8.8.8.8/x"))
    assert resp.status_code == 200
