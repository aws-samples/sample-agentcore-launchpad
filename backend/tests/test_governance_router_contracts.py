from datetime import UTC, datetime

from app.routers import governance as governance_router


def _operation() -> dict:
    return {
        "id": "a" * 32,
        "gateway_id": "gw-1",
        "operation": "attach_engine",
        "status": "pending",
    }


def test_governance_mutation_and_poll_responses_wrap_operation(client, monkeypatch):
    operation = _operation()
    requests = []
    monkeypatch.setattr(governance_router, "control_client", lambda: object())
    monkeypatch.setattr(
        governance_router.governance_service,
        "queue_engine_attach",
        lambda _db, _control, _gateway_id, request: requests.append(request)
        or operation,
    )
    monkeypatch.setattr(
        governance_router.governance_service,
        "run_policy_change",
        lambda *_args: None,
    )
    monkeypatch.setattr(
        governance_router.governance_service,
        "get_operation",
        lambda *_args: operation,
    )

    response = client.post(
        "/api/governance/gateways/gw-1/engine",
        json={
            "expected_gateway_updated_at": datetime.now(UTC).isoformat(),
            "authorization_model": "allowlist",
        },
    )
    assert response.status_code == 202
    assert response.json() == {"operation": operation}
    assert requests[0].mode == "ENFORCE"

    response = client.get(f"/api/governance/operations/{operation['id']}")
    assert response.status_code == 200
    assert response.json() == {"operation": operation}


def test_gateway_list_envelope_reports_the_scoped_account_and_region(
    client, monkeypatch
):
    monkeypatch.setattr(governance_router, "control_client", lambda: object())
    monkeypatch.setattr(
        governance_router.governance_service,
        "list_gateway_views",
        lambda *_args, **_kwargs: [],
    )

    body = client.get("/api/governance/gateways").json()
    assert body["gateways"] == []
    assert set(body) == {"gateways", "account_id", "region"}
    assert body["region"] == governance_router.get_settings().region


def test_governance_generation_start_uses_frontend_contract(client, monkeypatch):
    operation = _operation()
    monkeypatch.setattr(governance_router, "control_client", lambda: object())
    monkeypatch.setattr(
        governance_router.governance_service,
        "start_generation",
        lambda *_args: {
            "id": "generation-1",
            "status": "GENERATING",
            "operation": operation,
        },
    )

    response = client.post(
        "/api/governance/gateways/gw-1/generations",
        json={
            "expected_gateway_updated_at": datetime.now(UTC).isoformat(),
            "text": "Allow finance analysts to read approved reports.",
            "name": "finance_reports",
        },
    )
    assert response.status_code == 202
    assert response.json() == {
        "operation": operation,
        "generation_id": "generation-1",
        "status": "GENERATING",
    }


def test_governance_registry_routes_delegate_typed_requests(client, monkeypatch):
    preview = {
        "gateway_id": "gw-1",
        "gateway_name": "finance",
        "gateway_url": "https://gw.example/mcp",
        "proposed": {},
        "outcome": "created",
        "changed": False,
        "exact_record": None,
        "name_conflict": None,
        "legacy_records": [],
    }
    imported = {"outcome": "created"}
    retired = {"retired": ["legacy-1"], "skipped": []}
    seen: dict[str, object] = {}
    monkeypatch.setattr(governance_router, "control_client", lambda: object())
    monkeypatch.setattr(
        governance_router.governance_service,
        "gateway_registry_preview",
        lambda *_args: preview,
    )

    def import_record(_control, _gateway_id, request):
        seen["import"] = request
        return imported

    def retire_records(_control, _gateway_id, request):
        seen["retire"] = request
        return retired

    monkeypatch.setattr(
        governance_router.governance_service,
        "import_gateway_registry",
        import_record,
    )
    monkeypatch.setattr(
        governance_router.governance_service,
        "retire_gateway_legacy_records",
        retire_records,
    )

    response = client.get("/api/governance/gateways/gw-1/registry-preview")
    assert response.status_code == 200
    assert response.json() == preview

    timestamp = datetime.now(UTC).isoformat()
    response = client.post(
        "/api/governance/gateways/gw-1/registry-import",
        json={
            "expected_gateway_updated_at": timestamp,
            "record_name": "finance-catalog",
            "apply_update": True,
        },
    )
    assert response.status_code == 200
    assert response.json() == imported
    assert seen["import"].record_name == "finance-catalog"

    response = client.post(
        "/api/governance/gateways/gw-1/retire-legacy-records",
        json={
            "expected_gateway_updated_at": timestamp,
            "record_ids": ["legacy-1"],
        },
    )
    assert response.status_code == 200
    assert response.json() == retired
    assert seen["retire"].record_ids == ["legacy-1"]
