import os
import tempfile

# Isolate tests from data/launchpad.db BEFORE any app import binds the engine.
_TEST_DB = os.path.join(tempfile.mkdtemp(prefix="launchpad-test-"), "test.db")
os.environ["LAUNCHPAD_DATABASE_URL"] = f"sqlite:///{_TEST_DB}"

# Most tests exercise the console with no password configured, and TestClient's
# peer address is the literal "testclient" rather than a loopback IP — so the
# open-console guard would refuse them all. The suite accepts an open console on
# purpose; test_open_console.py clears this to assert the guard itself.
os.environ["LAUNCHPAD_ALLOW_OPEN_CONSOLE"] = "true"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.core.db import Base, _seed_default_workspace, engine  # noqa: E402
from app.main import create_app  # noqa: E402
from app.services.workspace import WorkspaceContext  # noqa: E402


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


@pytest.fixture(autouse=True)
def reset_aws_client_cache():
    # Tests that stub aws_clients.get_session still populate the module-level
    # client cache; a fake cached under a real key must not leak across tests.
    from app.services import aws_clients

    yield
    aws_clients.reset_cache()
