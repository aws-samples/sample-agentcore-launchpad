"""Security pass: input validation on public endpoints (phase 13)."""


def test_agent_name_regex_rejected(client):
    res = client.post(
        "/api/agents",
        json={"name": "Bad Name!", "method": "harness", "system_prompt": "x"},
    )
    assert res.status_code == 422


def test_studio_code_size_cap(client):
    res = client.post(
        "/api/agents",
        json={
            "name": "cap-check",
            "method": "studio",
            "system_prompt": "x",
            "code": "x" * 200_001,
        },
    )
    assert res.status_code == 422


def test_system_prompt_size_cap(client):
    res = client.post(
        "/api/agents",
        json={"name": "cap-check", "method": "harness", "system_prompt": "x" * 20_001},
    )
    assert res.status_code == 422


def test_browser_demo_blocks_internal_urls(client):
    for url in (
        "file:///etc/passwd",
        "http://169.254.169.254/latest/meta-data/",
        "http://localhost:8000/api/agents",
        "http://10.0.0.1/",
        "http://172.16.0.1/",
        "http://2130706433/",  # decimal-encoded 127.0.0.1
        "http://0x7f000001/",  # hex-encoded 127.0.0.1
        "http://[::1]/",  # IPv6 loopback
        "http://[fd00::1]/",  # IPv6 unique-local
    ):
        res = client.post("/api/demos/browser", json={"url": url})
        assert res.status_code == 400, url
        assert res.json()["code"] == "tools.url_blocked"


def test_browser_demo_allows_public_https():
    from app.routers.tools import _validate_demo_url

    _validate_demo_url("https://example.com")  # must not raise


def test_public_api_requires_key(client):
    res = client.post("/v1/agents/whatever/invoke", json={"prompt": "hi"})
    assert res.status_code in (401, 403)
