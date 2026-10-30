from app.routers import governance as governance_router


def test_gateway_list_envelope_reports_the_scoped_account_and_region(
    client, monkeypatch
):
    monkeypatch.setattr(governance_router, "control_client", lambda _ws=None: object())
    monkeypatch.setattr(
        governance_router.governance_service,
        "list_gateway_views",
        lambda *_args, **_kwargs: [],
    )

    body = client.get("/api/governance/gateways").json()
    assert body["gateways"] == []
    assert set(body) == {"gateways", "account_id", "region"}
    assert body["region"] == "cn-northwest-1"  # the default workspace's region
