"""`run_mode` and the settings it drives (secure cookies, local execution).

`start.py` has exported LAUNCHPAD_RUN_MODE into its children since before this
setting existed; these tests pin that the env var is actually consumed now, and
that the two derived helpers are read through their accessors rather than off the
raw field.
"""

import pytest

from app.core.config import Settings, get_settings
from app.routers.auth import cookie_secure
from app.services.local_exec import local_exec_enabled


@pytest.fixture
def clear_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


class TestRunMode:
    def test_defaults_to_dev(self):
        assert Settings().run_mode == "dev"

    def test_reads_the_env_var_start_py_already_exports(
        self, monkeypatch, clear_settings_cache
    ):
        # start.py:_service_definitions puts this in the child environment.
        monkeypatch.setenv("LAUNCHPAD_RUN_MODE", "prod")
        assert get_settings().run_mode == "prod"

    def test_rejects_an_unknown_mode(self, monkeypatch, clear_settings_cache):
        monkeypatch.setenv("LAUNCHPAD_RUN_MODE", "staging")
        with pytest.raises(ValueError):
            Settings()


class TestCookieSecure:
    def test_off_in_dev(self):
        assert cookie_secure(Settings()) is False

    def test_on_in_prod_without_being_configured(self):
        assert cookie_secure(Settings(run_mode="prod")) is True

    def test_yaml_can_still_force_it_on_in_dev(self):
        assert cookie_secure(Settings(auth_cookie_secure=True)) is True


class TestLocalExecEnabled:
    def test_enabled_in_dev(self):
        assert local_exec_enabled(Settings()) is True

    def test_disabled_in_prod_by_default(self):
        assert local_exec_enabled(Settings(run_mode="prod")) is False

    def test_explicit_opt_in_overrides_prod(self):
        settings = Settings(run_mode="prod", studio_local_exec_enabled=True)
        assert local_exec_enabled(settings) is True

    def test_explicit_opt_out_overrides_dev(self):
        settings = Settings(run_mode="dev", studio_local_exec_enabled=False)
        assert local_exec_enabled(settings) is False
