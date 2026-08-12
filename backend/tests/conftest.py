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

from app.core.db import Base, engine  # noqa: E402
from app.main import create_app  # noqa: E402


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app())


@pytest.fixture(autouse=True)
def clean_tables():
    yield
    with engine.begin() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            conn.execute(table.delete())


@pytest.fixture(autouse=True)
def reset_aws_client_cache():
    # Tests that stub aws_clients.get_session still populate the module-level
    # client cache; a fake cached under a real key must not leak across tests.
    from app.services import aws_clients

    yield
    aws_clients.reset_cache()
