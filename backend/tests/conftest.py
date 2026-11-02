import os
import tempfile
from pathlib import Path

# Isolate tests from data/launchpad.db BEFORE any app import binds the engine.
_TEST_DB = os.path.join(tempfile.mkdtemp(prefix="launchpad-test-"), "test.db")
os.environ["LAUNCHPAD_DATABASE_URL"] = f"sqlite:///{_TEST_DB}"

# Most tests exercise the console with no password configured, and TestClient's
# peer address is the literal "testclient" rather than a loopback IP — so the
# open-console guard would refuse them all. The suite accepts an open console on
# purpose; test_open_console.py clears this to assert the guard itself.
os.environ["LAUNCHPAD_ALLOW_OPEN_CONSOLE"] = "true"

# app.main's module-level app starts this daemon during import. It otherwise wakes
# after 90 seconds, makes network requests and races tests that replace price YAML.
# Manual refresh and price-merge behavior remain covered by test_model_prices.py.
os.environ["LAUNCHPAD_MODEL_PRICES_REFRESH_HOURS"] = "0"
# The coding watcher (on by default): it would invoke the coding runtime every
# 15 s. tests/test_coding_repos.py drives run_once() / start() directly.
os.environ["LAUNCHPAD_CODING_WATCHER_ENABLED"] = "false"

# Docker-backend tests assert literal `docker ...` argv; pin the container CLI so
# "auto" resolution does not pick finch on a host without docker. The resolver's
# own tests pass container_cli explicitly.
os.environ["LAUNCHPAD_CONTAINER_CLI"] = "docker"

# Isolate configuration reads before db/main bind settings or seed the default
# workspace. Host YAML may select another Region or Docker execution in production.
# Tests that exercise YAML precedence still repoint this late-bound path themselves.
import app.core.config as config_mod  # noqa: E402

config_mod.CONFIG_FILE = Path(_TEST_DB).with_name("launchpad.yaml")
# API happy paths require a bootstrapped default workspace. Supply only a small,
# explicit fake baseline; feature tests replace the resource map they exercise.
config_mod.CONFIG_FILE.write_text(
    "account_id: '111122223333'\n"
    "resources:\n"
    "  execution_role_arn: arn:aws:iam::111122223333:role/launchpad-test-execution\n"
    "  artifacts_bucket: launchpad-artifacts-test\n",
    encoding="utf-8",
)
config_mod.get_settings.cache_clear()

import pytest  # noqa: E402
from botocore.exceptions import ClientError  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.core.db import Base, _seed_default_projects, _seed_default_workspace, engine  # noqa: E402
from app.dlc import scheduler as _dlc_scheduler  # noqa: E402
from app.main import create_app  # noqa: E402
from app.services.workspace import WorkspaceContext  # noqa: E402

# Importing app.main builds the module-level `app = create_app(resume_jobs=True)`,
# which starts the real Agent-DLC scheduler thread in every test process. Its
# 60 s tick then runs scheduled tasks against whatever a test has monkeypatched —
# on a loaded host a tick lands inside a test and doubles its calls (seen with
# test_the_scheduler_checks_each_server_in_its_project_and_skips_archived).
# Tests drive scheduler.tick() / run_* directly; stop the background loop.
_dlc_scheduler.stop()
if _dlc_scheduler._THREAD is not None:
    _dlc_scheduler._THREAD.join(timeout=5)


def ws_ctx(resources: dict | None = None, **fields) -> WorkspaceContext:
    """A workspace context for tests that call a service directly.

    Services take the environment as an argument now, so a test supplies the
    resource map here instead of monkeypatching `get_settings` on the module.
    """
    return WorkspaceContext(
        account_id=fields.pop("account_id", "111122223333"),
        region=fields.pop("region", "us-west-2"),
        resources=dict(resources or {}),
        **fields,
    )


def client_error(code: str, message: str = "", operation: str = "Op") -> ClientError:
    """The `ClientError` a boto3 call raises for AWS error `code`."""
    return ClientError({"Error": {"Code": code, "Message": message or code}}, operation)


@pytest.fixture
def workspace() -> WorkspaceContext:
    return ws_ctx()


def set_default_resources(resources: dict) -> None:
    """Put a resource map on the `default` workspace row.

    The row — not settings — is what a request's context reads, so an API test
    that needs a bootstrapped environment writes it here. Call this AFTER the app
    exists: `init_db` mirrors settings onto the row at startup and would overwrite
    it. The autouse table wipe re-seeds the row, so nothing leaks between tests.
    """
    from app.core.db import SessionLocal
    from app.models.ledger import Workspace

    db = SessionLocal()
    try:
        row = db.get(Workspace, "default")
        row.resources = dict(resources)
        db.commit()
    finally:
        db.close()


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app())


# The business project new agents land in (services/agent_project): a workspace's
# `default` project may not hold a new agent, so the suite seeds one with a
# department and binds it to the `default` workspace in place of `default` — the
# workspace's one project, so header-less requests and ORM stamping both land there.
# A test about the `default` project binds it back itself.
TEST_DEPARTMENT = "dept-test"
TEST_PROJECT = "proj-test"


def _seed_test_business_project(bind) -> None:
    from sqlalchemy import text

    now = "2026-01-01 00:00:00.000000"
    with bind.begin() as conn:
        conn.execute(
            text("INSERT INTO departments (id, name, description, created_at, updated_at)"
                 " SELECT :d, 'Test department', '', :now, :now"
                 " WHERE NOT EXISTS (SELECT 1 FROM departments WHERE id = :d)"),
            {"d": TEST_DEPARTMENT, "now": now},
        )
        conn.execute(
            text("INSERT INTO projects (id, name, description, department_id, status,"
                 " is_default, created_at, updated_at)"
                 " SELECT :p, 'Test project', '', :d, 'active', 0, :now, :now"
                 " WHERE NOT EXISTS (SELECT 1 FROM projects WHERE id = :p)"),
            {"p": TEST_PROJECT, "d": TEST_DEPARTMENT, "now": now},
        )
        conn.execute(text("DELETE FROM project_workspaces WHERE workspace_id = 'default'"
                          " AND project_id = 'default'"))
        conn.execute(
            text("INSERT INTO project_workspaces (project_id, workspace_id,"
                 " is_workspace_default, enabled_at)"
                 " SELECT :p, 'default', 1, :now WHERE NOT EXISTS (SELECT 1 FROM"
                 " project_workspaces WHERE project_id = :p AND workspace_id = 'default')"),
            {"p": TEST_PROJECT, "now": now},
        )


@pytest.fixture(autouse=True, scope="session")
def test_business_project():
    _seed_test_business_project(engine)


@pytest.fixture(autouse=True)
def clean_tables():
    yield
    with engine.begin() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            conn.execute(table.delete())
    # The wipe takes the `default` workspace with it, and the request boundary
    # resolves every workspace-scoped route against it. Tests that build no app
    # (so never run init_db) would otherwise see a ledger with no workspace at
    # all, which a real process cannot have.
    _seed_default_workspace(engine)
    # ...and the `default` project bound to it (design §8.3), with the process-local
    # access cache dropped: the wiped `access_epoch` restarts at 0, so a snapshot
    # cached by the previous test would otherwise look current.
    _seed_default_projects(engine)
    _seed_test_business_project(engine)
    # the console overlay (core/config) is process-wide: a console write in one test
    # must not leak into the next one's Settings
    from app.core.config import get_settings, reset_console_overrides_for_tests

    if reset_console_overrides_for_tests():
        get_settings.cache_clear()
    from app.services import access, project_quotas

    access.reset_cache_for_tests()
    # the budget gate's month-to-date spend cache is keyed by project id
    project_quotas.reset_cache_for_tests()


@pytest.fixture(autouse=True)
def isolated_config_file(tmp_path, monkeypatch):
    # merge_workspace_resources mirrors default-workspace keys into
    # config/launchpad.yaml via bootstrap.write_config — a hermetic test must
    # never write the developer's real file. bootstrap imports CONFIG_FILE by
    # value, so its own binding is the one to repoint.
    from app.services import bootstrap

    monkeypatch.setattr(bootstrap, "CONFIG_FILE", tmp_path / "launchpad-test.yaml")


@pytest.fixture(autouse=True)
def no_background_retag(monkeypatch):
    # A project rename / transfer / cost-tag change starts its re-tag run after the
    # commit (cost_tags.start_queued). The run calls AWS tagging APIs, so a hermetic
    # test never starts one; tests of the run call execute_backfill_job themselves.
    from app.services import cost_tags

    monkeypatch.setattr(cost_tags, "start_backfill_async", lambda job_id: None)


@pytest.fixture(autouse=True)
def no_agent_artifact_cleanup(monkeypatch):
    # Every agent delete lists + deletes the agent's own S3 artifacts through the
    # workspace's real S3 client. A hermetic delete test never reaches S3; the cleanup's
    # own tests (test_agent_artifacts.py) restore the real function with a stub client.
    from app.services import agent_artifacts

    monkeypatch.setattr(agent_artifacts, "delete_agent_artifacts", lambda *a, **k: [])


@pytest.fixture(autouse=True)
def no_traffic_history(monkeypatch):
    # Pricing a passive run or an online config (eval-cost §1.1) sizes it from the
    # agent's last 7 days — a Logs Insights scan. Hermetic tests see no traffic;
    # tests of the sizing replace this themselves.
    from app.dlc import cost

    monkeypatch.setattr(cost, "window_sessions", lambda *a, **k: 0)


@pytest.fixture(autouse=True)
def reset_auth_throttle():
    # Every TestClient shares one peer address, so the per-IP login/register windows
    # (services/auth_throttle.py) would otherwise fill up across a worker's tests.
    from app.services import auth_throttle

    auth_throttle.reset_for_tests()
    yield
    auth_throttle.reset_for_tests()


@pytest.fixture(autouse=True)
def reset_aws_client_cache():
    # Tests that stub aws_clients.get_session still populate the module-level
    # client cache; a fake cached under a real key must not leak across tests.
    from app.services import aws_clients

    yield
    aws_clients.reset_cache()
