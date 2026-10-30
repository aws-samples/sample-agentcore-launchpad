"""Spec C4: the Global Bedrock API key enters through one env var, lands in an AgentCore
Identity API-key credential provider, and is never written or logged."""

import json

import pytest
from botocore.exceptions import ClientError

from app.services import model_credential as mc

ARN = (
    "arn:aws-cn:bedrock-agentcore:cn-northwest-1:123456789012:token-vault/default/"
    "apikeycredentialprovider/launchpad-global-bedrock"
)
SECRET = (
    "arn:aws-cn:secretsmanager:cn-northwest-1:123456789012:secret:"
    "bedrock-agentcore-identity!default/apikey/launchpad-global-bedrock-AbCdEf"
)


def _not_found() -> ClientError:
    return ClientError(
        {"Error": {"Code": "ResourceNotFoundException", "Message": "no such provider"}},
        "GetApiKeyCredentialProvider",
    )


class FakeControl:
    def __init__(self, existing: bool) -> None:
        self.existing = existing
        self.calls: list[tuple[str, dict]] = []

    def get_api_key_credential_provider(self, **kw):
        self.calls.append(("get", kw))
        if not self.existing:
            raise _not_found()
        return {"name": kw["name"], "credentialProviderArn": ARN, "apiKeySecretArn": SECRET}

    def create_api_key_credential_provider(self, **kw):
        self.calls.append(("create", kw))
        return {"name": kw["name"], "credentialProviderArn": ARN, "apiKeySecretArn": SECRET}

    def update_api_key_credential_provider(self, **kw):
        self.calls.append(("update", kw))
        return {"name": kw["name"], "credentialProviderArn": ARN, "apiKeySecretArn": SECRET}


def test_constants_match_the_schema_default():
    from app.schemas.agent import DEFAULT_MODEL_CREDENTIAL

    assert mc.MODEL_CREDENTIAL_ENV == "LAUNCHPAD_GLOBAL_BEDROCK_API_KEY"
    assert mc.MODEL_CREDENTIAL_NAME == DEFAULT_MODEL_CREDENTIAL == "launchpad-global-bedrock"


def test_missing_env_var_is_a_clear_error_naming_it():
    with pytest.raises(mc.MissingModelCredentialError, match="LAUNCHPAD_GLOBAL_BEDROCK_API_KEY"):
        mc.read_global_bedrock_api_key({})
    with pytest.raises(mc.MissingModelCredentialError):
        mc.read_global_bedrock_api_key({"LAUNCHPAD_GLOBAL_BEDROCK_API_KEY": "   "})
    assert mc.read_global_bedrock_api_key({"LAUNCHPAD_GLOBAL_BEDROCK_API_KEY": " k1 "}) == "k1"


def test_creates_the_provider_when_absent():
    control = FakeControl(existing=False)
    logs: list[str] = []
    result = mc.ensure_api_key_credential_provider(
        control, "launchpad-global-bedrock", "k1", logs.append
    )
    assert result == {
        "arn": ARN,
        "secret_arn": SECRET,
        "name": "launchpad-global-bedrock",
        "created": True,
        "rotated": False,
    }
    assert [c[0] for c in control.calls] == ["get", "create"]
    assert control.calls[1][1] == {"name": "launchpad-global-bedrock", "apiKey": "k1"}
    assert "k1" not in " ".join(logs) and "created" in " ".join(logs)


def test_rotates_in_place_when_present():
    control = FakeControl(existing=True)
    logs: list[str] = []
    result = mc.ensure_api_key_credential_provider(
        control, "launchpad-global-bedrock", "k2", logs.append
    )
    assert result["created"] is False and result["arn"] == ARN and result["secret_arn"] == SECRET
    assert result["rotated"] is True
    assert [c[0] for c in control.calls] == ["get", "update"]
    assert control.calls[1][1] == {"name": "launchpad-global-bedrock", "apiKey": "k2"}
    assert "k2" not in " ".join(logs)


def test_rotate_false_leaves_an_existing_provider_untouched_and_creates_a_missing_one():
    """D21-obs5: the caller established the key is current → no Update call, one `up to
    date` line, `rotated: False`; a provider Identity does not have is still created."""
    control = FakeControl(existing=True)
    logs: list[str] = []
    result = mc.ensure_api_key_credential_provider(
        control, "launchpad-siliconflow", "k3", logs.append, rotate=False, key_version=7)
    assert result["created"] is False and result["rotated"] is False and result["arn"] == ARN
    assert [c[0] for c in control.calls] == ["get"]
    assert logs == ["api-key credential provider launchpad-siliconflow up to date (key version 7)"]
    absent = FakeControl(existing=False)
    result = mc.ensure_api_key_credential_provider(absent, "launchpad-siliconflow", "k3",
                                                   rotate=False, key_version=7)
    assert result["created"] is True and result["rotated"] is False
    assert [c[0] for c in absent.calls] == ["get", "create"]
    assert "k3" not in " ".join(logs)


def test_key_is_current_compares_version_then_date_and_never_without_a_stored_version():
    stamp = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)
    secret = mc.SsmSecret(value="v", version=5, modified_at=stamp)
    assert mc.key_is_current(secret, version=5, modified_at=stamp) is True
    assert mc.key_is_current(secret, version=5, modified_at=None) is True  # date unknown
    assert mc.key_is_current(secret, version=4, modified_at=stamp) is False
    assert mc.key_is_current(secret, version=None, modified_at=stamp) is False  # first run
    assert mc.key_is_current(secret, version=5, modified_at=stamp.replace(hour=13)) is False
    # a host_env key (no SSM metadata) can never be proven current → rotate
    env_key = mc.SsmSecret(value="v", version=None, modified_at=None)
    assert mc.key_is_current(env_key, version=None, modified_at=None) is False


def test_other_client_errors_propagate():
    class Denied(FakeControl):
        def get_api_key_credential_provider(self, **kw):
            raise ClientError(
                {"Error": {"Code": "AccessDeniedException"}}, "GetApiKeyCredentialProvider"
            )

    with pytest.raises(ClientError):
        mc.ensure_api_key_credential_provider(Denied(existing=True), "x", "k")


def test_ensure_model_credential_reads_the_env_and_maps_resources():
    control = FakeControl(existing=False)
    # A single-char key like "k" is a substring of "bedrock"/"token" in the ARNs
    # themselves, which would make the leak-check below trip on the ARNs rather
    # than on an actual leak — use a value that cannot collide by coincidence.
    secret_value = "sekret-xyz123"
    result = mc.ensure_model_credential(
        control, environ={"LAUNCHPAD_GLOBAL_BEDROCK_API_KEY": secret_value}
    )
    resources = mc.model_credential_resources(result)
    assert resources == {
        "model_credential_arn": ARN,
        "model_credential_name": "launchpad-global-bedrock",
        "model_credential_secret_arn": SECRET,
    }
    assert secret_value not in json.dumps(resources)  # the key never reaches launchpad.yaml
    with pytest.raises(mc.MissingModelCredentialError):
        mc.ensure_model_credential(control, environ={})


# ─── N-R6: one Identity provider per configured row of PROVIDERS ────────────────────


@pytest.mark.siliconflow
def test_read_provider_api_key_is_required_for_bedrock_and_optional_otherwise():
    env = {"LAUNCHPAD_GLOBAL_BEDROCK_API_KEY": "b", "LAUNCHPAD_SILICONFLOW_API_KEY": " s "}
    assert mc.read_provider_api_key("bedrock", env) == "b"
    assert mc.read_provider_api_key("siliconflow", env) == "s"
    assert mc.read_provider_api_key("siliconflow",
                                    {"LAUNCHPAD_GLOBAL_BEDROCK_API_KEY": "b"}) is None
    with pytest.raises(mc.MissingModelCredentialError, match="LAUNCHPAD_GLOBAL_BEDROCK_API_KEY"):
        mc.read_provider_api_key("bedrock", {"LAUNCHPAD_SILICONFLOW_API_KEY": "s"})


@pytest.mark.siliconflow
def test_ensure_provider_credentials_creates_every_configured_provider():
    control = FakeControl(existing=False)
    logs: list[str] = []
    results = mc.ensure_provider_credentials(
        control, logs.append,
        {"LAUNCHPAD_GLOBAL_BEDROCK_API_KEY": "b-key", "LAUNCHPAD_SILICONFLOW_API_KEY": "s-key"},
    )
    assert set(results) == {"bedrock", "siliconflow"}
    assert results["bedrock"]["name"] == "launchpad-global-bedrock"
    assert results["bedrock"]["created"]
    assert results["siliconflow"]["name"] == "launchpad-siliconflow"
    assert results["siliconflow"]["created"] and results["siliconflow"]["configured"]
    creates = [kw for op, kw in control.calls if op == "create"]
    assert [(c["name"], c["apiKey"]) for c in creates] == [
        ("launchpad-global-bedrock", "b-key"), ("launchpad-siliconflow", "s-key")]
    assert "s-key" not in "\n".join(logs) and "b-key" not in "\n".join(logs)
    resources = mc.provider_credential_resources(results)
    assert resources["model_credential_name"] == "launchpad-global-bedrock"
    assert set(resources["model_credentials"]) == {"bedrock", "siliconflow"}
    assert resources["model_credentials"]["siliconflow"]["configured"] is True
    assert "s-key" not in json.dumps(resources)


@pytest.mark.siliconflow
def test_ensure_provider_credentials_skips_an_unconfigured_optional_provider_with_a_line():
    control = FakeControl(existing=True)
    logs: list[str] = []
    results = mc.ensure_provider_credentials(control, logs.append,
                                             {"LAUNCHPAD_GLOBAL_BEDROCK_API_KEY": "b-key"})
    assert results["bedrock"]["configured"] and not results["bedrock"]["created"]
    assert results["siliconflow"] == {"configured": False, "name": "launchpad-siliconflow",
                                      "created": False}
    assert not any(kw.get("name") == "launchpad-siliconflow" for _op, kw in control.calls)
    line = next(line for line in logs if "not configured" in line)
    assert "launchpad-siliconflow" in line and "LAUNCHPAD_SILICONFLOW_API_KEY" in line
    assert "launchpad-siliconflow-api-key" in line and "SiliconFlow models stay unavailable" in line
    resources = mc.provider_credential_resources(results)
    assert resources["model_credentials"]["siliconflow"]["configured"] is False
    assert resources["model_credentials"]["siliconflow"]["arn"] == ""
    assert mc.credential_summary(results)["siliconflow"] == {
        "name": "launchpad-siliconflow", "created": False, "configured": False}


def test_ensure_provider_credentials_requires_the_bedrock_key_before_touching_identity():
    control = FakeControl(existing=False)
    with pytest.raises(mc.MissingModelCredentialError):
        mc.ensure_provider_credentials(control, environ={"LAUNCHPAD_SILICONFLOW_API_KEY": "s"})
    assert control.calls == []


# ── D21 (M1-b): the registry half — the key comes from an SSM parameter ──────────────────
from datetime import UTC, datetime  # noqa: E402

KEY = "sf-live-KEY-0123456789abcdef"


class FakeSsm:
    def __init__(self, params: dict[str, tuple[str, int]] | None = None,
                 error: str | None = None) -> None:
        self.params = dict(params or {})
        self.error = error
        self.calls: list[tuple[str, bool]] = []

    def get_parameter(self, Name, WithDecryption=False):  # noqa: N803
        self.calls.append((Name, WithDecryption))
        if self.error:
            raise ClientError({"Error": {"Code": self.error, "Message": "denied"}}, "GetParameter")
        if Name not in self.params:
            raise ClientError({"Error": {"Code": "ParameterNotFound",
                                         "Message": f"Parameter {Name} not found."}},
                              "GetParameter")
        value, version = self.params[Name]
        return {"Parameter": {"Name": Name, "Value": value, "Version": version,
                              "LastModifiedDate": datetime(2026, 10, 14, 12, 0, tzinfo=UTC)}}


def test_read_ssm_parameter_returns_version_and_date_and_hides_the_value_in_repr():
    ssm = FakeSsm({"launchpad-acme-api-key": (f"  {KEY}  ", 3)})
    secret = mc.read_ssm_parameter(ssm, "launchpad-acme-api-key")
    assert secret.value == KEY and secret.version == 3
    assert secret.modified_at == datetime(2026, 10, 14, 12, 0, tzinfo=UTC)
    assert KEY not in repr(secret) and KEY not in str(secret) and "version=3" in repr(secret)
    assert ssm.calls == [("launchpad-acme-api-key", True)]  # WithDecryption always


def test_read_ssm_parameter_refuses_names_outside_the_pattern_before_any_call():
    ssm = FakeSsm()
    for bad in ("launchpad-console-password", "/launchpad-acme-api-key", "other-api-key", ""):
        with pytest.raises(ValueError):
            mc.read_ssm_parameter(ssm, bad)
    assert ssm.calls == []


def test_read_ssm_parameter_missing_or_empty_is_a_parameter_missing_error():
    with pytest.raises(mc.ParameterMissingError, match="does not exist") as exc:
        mc.read_ssm_parameter(FakeSsm(), "launchpad-acme-api-key")
    assert exc.value.parameter == "launchpad-acme-api-key" and not exc.value.empty
    with pytest.raises(mc.ParameterMissingError, match="is empty") as exc:
        mc.read_ssm_parameter(FakeSsm({"launchpad-acme-api-key": ("   ", 1)}),
                              "launchpad-acme-api-key")
    assert exc.value.empty
    with pytest.raises(ClientError):  # anything else (AccessDenied) propagates for the caller
        mc.read_ssm_parameter(FakeSsm(error="AccessDeniedException"), "launchpad-acme-api-key")


def test_rotation_line_names_the_ssm_parameter_when_given_as_the_source():
    control = FakeControl(existing=True)
    logs: list[str] = []
    mc.ensure_api_key_credential_provider(control, "launchpad-acme", KEY, logs.append,
                                          source="SSM parameter launchpad-acme-api-key (version 3)")
    assert logs == ["api-key credential provider launchpad-acme reused · key rotated from "
                    "SSM parameter launchpad-acme-api-key (version 3)"]
    assert KEY not in "\n".join(logs)


def test_credential_record_adds_version_and_source_only_when_known():
    base = {"name": "launchpad-acme", "arn": ARN, "secret_arn": SECRET, "configured": True,
            "created": True}
    assert mc.credential_record(base) == {"name": "launchpad-acme", "arn": ARN,
                                         "secret_arn": SECRET, "configured": True}
    full = mc.credential_record(base, key_version=3, source="ssm")
    assert full["key_version"] == 3 and full["source"] == "ssm"
    assert set(full) == {"name", "arn", "secret_arn", "configured", "key_version", "source"}
    # the result's own keys are honoured too (the bootstrap pass carries them)
    assert mc.credential_record({**base, "key_version": 9, "source": "ssm"})["key_version"] == 9


class TestEnsureRegistryCredentials:
    """The bootstrap's registry pass: every `credential_source: ssm` row (the built-in
    SiliconFlow row on a stock registry) is read from SSM and registered in Identity."""

    @pytest.fixture
    def db(self):
        from app.core.db import SessionLocal
        from app.schemas import model_catalog
        from app.services import model_registry

        session = SessionLocal()
        model_registry.ensure_seeded(session)
        yield session
        session.close()
        model_catalog.reset()

    def _siliconflow(self, db):
        from sqlalchemy import select

        from app.models.model_registry import ModelProvider

        return db.scalar(select(ModelProvider).where(ModelProvider.name == "siliconflow"))

    @pytest.mark.siliconflow(registry=True)
    def test_registers_the_ssm_backed_rows_and_records_the_version(self, db):
        control = FakeControl(existing=False)
        ssm = FakeSsm({"launchpad-siliconflow-api-key": (KEY, 5)})
        logs: list[str] = []
        results = mc.ensure_registry_credentials(control, ssm, logs.append, db=db, actor="boot")
        assert set(results) == {"siliconflow"}  # the Bedrock row is host_env → skipped
        result = results["siliconflow"]
        assert result["configured"] and result["created"] and result["key_version"] == 5
        assert result["source"] == "ssm" and result["name"] == "launchpad-siliconflow"
        creates = [kw["name"] for op, kw in control.calls if op == "create"]
        assert creates == ["launchpad-siliconflow"]
        assert ssm.calls == [("launchpad-siliconflow-api-key", True)]
        row = self._siliconflow(db)
        assert row.key_version == 5 and row.key_modified_at is not None
        from app.services import model_registry

        change = model_registry.list_changes(db, limit=1)[0]
        assert change.operation == "provider.credential" and change.actor == "boot"
        assert change.after == {"configured": True, "created": True, "rotated": False,
                                "key_version": 5,
                                "key_modified_at": "2026-10-14T12:00:00+00:00"}
        assert KEY not in "\n".join(logs) and KEY not in json.dumps(results)
        assert KEY not in json.dumps([change.before, change.after])
        # second pass: rotation in place, the version follows the parameter
        ssm.params["launchpad-siliconflow-api-key"] = (KEY + "-v6", 6)
        control.existing = True
        again = mc.ensure_registry_credentials(control, ssm, logs.append, db=db)
        assert again["siliconflow"]["created"] is False and again["siliconflow"]["key_version"] == 6
        assert self._siliconflow(db).key_version == 6
        assert any("rotated from SSM parameter launchpad-siliconflow-api-key (version 6)" in line
                   for line in logs)

    @pytest.mark.siliconflow(registry=True)
    def test_a_second_pass_with_the_same_parameter_version_is_a_no_op(self, db):
        """D21-obs5: bootstrap twice with an unchanged SSM parameter → the provider is
        created once, Identity is only read afterwards, exactly one `provider.credential`
        row exists and the catalog version is the same after the second run; a changed
        version rotates, journals and still leaves the catalog version alone (credential
        rows are audit-only — `test_model_registry.py` pins the stamp rule)."""
        from app.schemas import model_catalog
        from app.services import model_registry

        def credential_rows():
            return [c for c in model_registry.list_changes(db, limit=50)
                    if c.operation == "provider.credential"]

        control = FakeControl(existing=False)
        ssm = FakeSsm({"launchpad-siliconflow-api-key": (KEY, 5)})
        logs: list[str] = []
        first = mc.ensure_registry_credentials(control, ssm, logs.append, db=db, actor="boot")
        assert first["siliconflow"]["created"] is True and first["siliconflow"]["rotated"] is False
        version_after_first = model_registry.stamp(db)
        assert model_catalog.current().version == version_after_first
        control.existing = True
        again = mc.ensure_registry_credentials(control, ssm, logs.append, db=db, actor="boot")
        assert again["siliconflow"] == {**first["siliconflow"], "created": False,
                                        "rotated": False}
        assert [op for op, _ in control.calls] == ["get", "create", "get"]  # no update
        assert len(credential_rows()) == 1
        assert model_registry.stamp(db) == version_after_first
        assert model_catalog.current().version == version_after_first
        assert self._siliconflow(db).key_version == 5
        assert logs[-1] == ("api-key credential provider launchpad-siliconflow up to date "
                            "(key version 5)")
        assert not any("rotated" in line for line in logs)
        # the parameter moved to v6 → rotate + journal; the catalog version still stands
        ssm.params["launchpad-siliconflow-api-key"] = (KEY + "-v6", 6)
        third = mc.ensure_registry_credentials(control, ssm, logs.append, db=db, actor="boot")
        assert third["siliconflow"]["rotated"] is True and third["siliconflow"]["key_version"] == 6
        assert control.calls[-1][0] == "update"
        rows = credential_rows()
        assert len(rows) == 2 and rows[0].after["rotated"] is True
        assert rows[0].after["key_version"] == 6 and rows[0].before["key_version"] == 5
        assert self._siliconflow(db).key_version == 6
        assert model_registry.stamp(db) == version_after_first
        assert model_catalog.current().version == version_after_first
        assert KEY not in "\n".join(logs)

    @pytest.mark.siliconflow(registry=True)
    def test_a_spoke_pass_is_a_no_op_when_its_own_map_already_holds_the_version(self, db):
        """D21-obs5 on a spoke (`record_on_row=False`): the hub row carries no version for
        it, so the comparison runs against the caller's `resources.model_credentials` map."""
        control = FakeControl(existing=True)
        ssm = FakeSsm({"launchpad-siliconflow-api-key": (KEY, 8)})
        known = {"siliconflow": {"name": "launchpad-siliconflow", "configured": True,
                                 "key_version": 8, "source": "ssm"}}
        results = mc.ensure_registry_credentials(control, ssm, db=db, record_on_row=False,
                                                 known_versions=known)
        assert results["siliconflow"]["rotated"] is False
        assert [op for op, _ in control.calls] == ["get"]
        # a stale map version → rotate; a record that is not configured → rotate
        for stale in ({"siliconflow": {**known["siliconflow"], "key_version": 7}},
                      {"siliconflow": {**known["siliconflow"], "configured": False}}, None):
            control.calls.clear()
            out = mc.ensure_registry_credentials(control, ssm, db=db, record_on_row=False,
                                                 known_versions=stale)
            assert out["siliconflow"]["rotated"] is True
            assert [op for op, _ in control.calls] == ["get", "update"]

    @pytest.mark.siliconflow(registry=True)
    def test_a_spoke_pass_leaves_the_hub_rows_version_and_journal_alone(self, db):
        """D21-obs4: `record_on_row=False` (a spoke workspace's bootstrap) registers the
        provider and reports the version on its result — for the spoke's own resource map —
        but never writes the hub-global row's key_version / key_modified_at or the journal."""
        from app.services import model_registry

        control = FakeControl(existing=False)
        ssm = FakeSsm({"launchpad-siliconflow-api-key": (KEY, 8)})
        results = mc.ensure_registry_credentials(control, ssm, db=db, actor="bootstrap:ws-two",
                                                 record_on_row=False)
        assert results["siliconflow"]["configured"] and results["siliconflow"]["key_version"] == 8
        assert [kw["name"] for op, kw in control.calls if op == "create"] == [
            "launchpad-siliconflow"]
        row = self._siliconflow(db)
        assert row.key_version is None and row.key_modified_at is None
        assert not [c for c in model_registry.list_changes(db, limit=50)
                    if c.operation == "provider.credential"]

    @pytest.mark.siliconflow(registry=True)
    def test_a_missing_parameter_is_one_line_and_not_configured(self, db):
        control = FakeControl(existing=False)
        logs: list[str] = []
        results = mc.ensure_registry_credentials(control, FakeSsm(), logs.append, db=db)
        assert results["siliconflow"] == {"configured": False, "name": "launchpad-siliconflow",
                                          "created": False}
        assert control.calls == []
        [line] = [line for line in logs if "not configured" in line]
        assert "launchpad-siliconflow-api-key does not exist" in line
        assert "Register credential" in line and "SiliconFlow models stay unavailable" in line
        assert self._siliconflow(db).key_version is None

    @pytest.mark.siliconflow(registry=True)
    def test_an_unreadable_parameter_degrades_with_the_code(self, db):
        control = FakeControl(existing=False)
        logs: list[str] = []
        results = mc.ensure_registry_credentials(control, FakeSsm(error="AccessDeniedException"),
                                                 logs.append, db=db)
        assert results["siliconflow"]["configured"] is False
        assert results["siliconflow"]["error"] == "AccessDeniedException"
        assert control.calls == []
        assert any("could not be read (AccessDeniedException" in line
                   and "launchpad-*-api-key" in line for line in logs)

    @pytest.mark.siliconflow(registry=True)
    def test_an_identity_refusal_degrades_per_provider(self, db):
        class RefusingControl(FakeControl):
            def create_api_key_credential_provider(self, **kw):
                raise ClientError({"Error": {"Code": "ValidationException",
                                             "Message": f"bad key {kw['apiKey']}"}},
                                  "CreateApiKeyCredentialProvider")

        logs: list[str] = []
        ssm = FakeSsm({"launchpad-siliconflow-api-key": (KEY, 1)})
        results = mc.ensure_registry_credentials(RefusingControl(existing=False), ssm,
                                                 logs.append, db=db)
        assert results["siliconflow"]["configured"] is False
        assert results["siliconflow"]["error"] == "ValidationException"
        assert KEY not in "\n".join(logs)
        assert self._siliconflow(db).key_version is None
