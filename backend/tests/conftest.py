import os
import tempfile
from pathlib import Path

# Isolate tests from data/launchpad.db BEFORE any app import binds the engine.
_TEST_DB = os.path.join(tempfile.mkdtemp(prefix="launchpad-test-"), "test.db")
os.environ["LAUNCHPAD_DATABASE_URL"] = f"sqlite:///{_TEST_DB}"
# The cookie-signing secret is generated on first use and persisted 0600 (r12-g); the
# suite keeps it beside the temp ledger so it never touches data/session_secret.
os.environ["LAUNCHPAD_SESSION_SECRET_FILE"] = os.path.join(
    os.path.dirname(_TEST_DB), "session_secret"
)

# Most tests exercise the console with no password configured, and TestClient's
# peer address is the literal "testclient" rather than a loopback IP — so the
# open-console guard would refuse them all. The suite accepts an open console on
# purpose; test_open_console.py clears this to assert the guard itself.
os.environ["LAUNCHPAD_ALLOW_OPEN_CONSOLE"] = "true"

# The online-evaluation scheduler (N12) is a hub daemon; tests drive its tick()
# directly instead of racing a thread that would query Logs Insights.
os.environ["LAUNCHPAD_ONLINE_EVAL_ENABLED"] = "0"

# The installed-preset auto-repair sweep (N17-obs206) is a hub daemon too; tests drive
# `auto_repair.AutoRepairScheduler.run_due()` with a fake clock and launcher instead of
# racing a thread that would claim repair jobs behind the test's back.
os.environ["LAUNCHPAD_SYSTEM_PRESETS_AUTO_REPAIR"] = "0"

# The architect turn's automatic replay waits ASSISTANT_REPLAY_BACKOFF_SECONDS (default
# 15) after a transient upstream failure; the hermetic suite replays at once. The one
# test of the wait itself (`test_a_transient_replay_waits_before_the_fresh_session`)
# patches the settings object.
os.environ["LAUNCHPAD_ASSISTANT_REPLAY_BACKOFF_SECONDS"] = "0"
# The evaluation worker's scenario replay / judge retries wait 5 s then 15 s after a
# transient upstream error (N12-obs53 / N12-obs55); the hermetic suite retries at once —
# the backoff tests inject their own wait list / sleep seam.
os.environ["LAUNCHPAD_EVAL_TRANSIENT_RETRY_BACKOFF_SECONDS"] = "0"

# C8 — the suite must never reach AWS, and a test that does must fail LOUDLY on every
# host, not only on a credential-less CI runner. Before any client import, strip every
# credential source botocore would otherwise resolve from the developer's / EC2 host's
# environment: the profile variables, static keys, the container / web-identity
# providers, the shared files (pointed at paths that do not exist) and the instance
# metadata endpoint. A leaked client call then raises `NoCredentialsError` at the first
# request (verified: with a key present it instead signed and sent the request to the real
# endpoint and "passed" on a 403 or a real answer). Obviously-fake static keys were
# deliberately NOT substituted — they would turn the loud failure back into a signed
# request against a real endpoint. Tests that exercise credential plumbing set the
# variables they need themselves (`monkeypatch.setenv`). The region variables are left as
# the host has them: no credential resolves either way, and the hosts' gate already runs
# `env -u AWS_REGION make verify`.
for _var in (
    "AWS_PROFILE",
    "AWS_DEFAULT_PROFILE",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
    "AWS_CONTAINER_CREDENTIALS_FULL_URI",
    "AWS_CONTAINER_AUTHORIZATION_TOKEN",
    "AWS_WEB_IDENTITY_TOKEN_FILE",
    "AWS_ROLE_ARN",
):
    os.environ.pop(_var, None)
os.environ["AWS_EC2_METADATA_DISABLED"] = "true"
os.environ["AWS_SHARED_CREDENTIALS_FILE"] = str(Path(_TEST_DB).with_name("no-aws-credentials"))
os.environ["AWS_CONFIG_FILE"] = str(Path(_TEST_DB).with_name("no-aws-config"))

# The hub holds its Global Bedrock API key only as this env var (spec C4), and a hub
# without it refuses every Skill Lab job and architect turn at submit
# (`aws_clients.require_global_bedrock_bearer`). The hermetic suite never contacts Global
# Bedrock, yet ~60 tests walk that guard, so a placeholder is always present — and the
# host's REAL key is replaced by it, so a leaked bearer-authenticated `bedrock-runtime`
# call can never spend it (it fails with a 403 at the Global endpoint instead). The tests
# of the guard itself `monkeypatch.delenv` the variable.
os.environ["AWS_BEARER_TOKEN_BEDROCK"] = "hermetic-placeholder-not-a-key"

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
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import event  # noqa: E402

from app.core.db import Base, _seed_default_workspace, engine  # noqa: E402
from app.main import create_app  # noqa: E402
from app.services.workspace import WorkspaceContext  # noqa: E402


@event.listens_for(engine, "connect")
def _no_fsync_for_the_hermetic_ledger(dbapi_connection, _record):
    """The temp ledger needs no durability across an OS crash, yet SQLite's default
    `synchronous=FULL` fsyncs every commit — and the job-shaped suites (the bootstrap and
    deploy pipelines log every stage message with its own commit) spent most of their wall
    time in that fsync (r17-ws: one 13-stage workspace graph path went from 8.8 s to 1.3 s).
    Per connection because the file engine uses NullPool. Journal mode is untouched: the
    product's `delete` mode stays what the restart / restore tests observe."""
    dbapi_connection.execute("PRAGMA synchronous=OFF")


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


def settle_deploy_jobs(agent_id: str, status: str = "succeeded") -> None:
    """Mark every deploy job (and Deployment) of ``agent_id`` terminal.

    N16-obs83: ``DELETE /api/agents/{id}`` refuses (409 ``agent.deploy_in_progress``)
    while a deploy job is ``queued`` / ``running``. Tests that stub the deploy launch
    leave the job ``queued`` forever; a test that simulates "the deploy finished" calls
    this before deleting the agent.
    """
    from app.core.db import SessionLocal
    from app.models.ledger import Deployment, Job

    db = SessionLocal()
    try:
        for dep in db.query(Deployment).filter(Deployment.agent_id == agent_id).all():
            dep.status = status
            if dep.job_id:
                job = db.get(Job, dep.job_id)
                if job is not None:
                    job.status = status
        db.commit()
    finally:
        db.close()


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app())


@pytest.fixture(autouse=True)
def hermetic_build_root(monkeypatch, tmp_path):
    """N17-obs151: `deployer/build_dirs.BUILD_ROOT` (the `data/builds/<agent>` scratch the
    package stage assembles in, the delete route removes and the startup sweep trims) points at
    a per-test temp dir, so neither `create_app(resume_jobs=True)` nor a teardown test can
    touch the checkout's own `data/builds`."""
    from app.deployer import build_dirs

    monkeypatch.setattr(build_dirs, "BUILD_ROOT", tmp_path / "builds")


@pytest.fixture(autouse=True)
def reset_engine_simulation_limits():
    """The per-user engine-simulation window (A6) and the attach / detach in-flight set are
    in-process state, like the login guard."""
    from app.routers import policy_engines as policy_engines_router
    from app.services import policy_engines

    policy_engines_router.reset_engine_simulation_limits()
    policy_engines.reset_busy()
    yield
    policy_engines_router.reset_engine_simulation_limits()
    policy_engines.reset_busy()


@pytest.fixture(autouse=True)
def reset_login_guard():
    """The sign-in lockout / per-peer windows are in-process state (r12-g); every
    TestClient shares the `testclient` peer, so wrong-password probes in one test
    must never throttle the next. Reset before AND after."""
    from app.services import login_guard

    login_guard.reset()
    yield
    login_guard.reset()


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
def isolated_config_file(tmp_path, monkeypatch):
    # merge_workspace_resources mirrors default-workspace keys into
    # config/launchpad.yaml via bootstrap.write_config — a hermetic test must
    # never write the developer's real file. bootstrap imports CONFIG_FILE by
    # value, so its own binding is the one to repoint.
    from app.services import bootstrap

    monkeypatch.setattr(bootstrap, "CONFIG_FILE", tmp_path / "launchpad-test.yaml")


@pytest.fixture(autouse=True)
def hermetic_kb_cleanup(monkeypatch):
    """A KB delete whose storage cleanup fails files a `kb_cleanup` job and starts a
    daemon thread that sleeps 5/30/120 s between retries (WB-008). The suite never wants
    a real thread or a real sleep: the starter records nothing and the sleeper returns at
    once. `tests/test_kb_cleanup.py` overrides both to run the target inline."""
    from app.services import kb_cleanup

    monkeypatch.setattr(kb_cleanup, "_SPAWN", lambda target, name: None)
    monkeypatch.setattr(kb_cleanup, "_SLEEP", lambda seconds: None)


@pytest.fixture(autouse=True)
def reset_feature_backends():
    """No memory/registry/knowledge backend is registered in this build; a test that
    installs a fake must not leak it into the next one. Reset before AND after: some
    module registers a backend as an import-time side effect, so a test asserting
    `feature.unavailable` can see a leftover backend if it happens to run first in a
    fresh process, before any post-test reset has ever run."""
    from app.routers import registry as registry_router
    from app.services import (
        knowledge_backend,
        knowledge_selfhosted,
        memory_backend,
        model_registry,
        policy_backend,
        policy_cedar,
        policy_store,
        registry_backend,
    )

    def reset() -> None:
        memory_backend.set_backend(None)
        registry_backend.set_backend(None)
        knowledge_backend.set_backend(None)
        # D20: the Cedar engine slot, the per-workspace compiled-set cache and the
        # publish listeners are process-level too.
        policy_backend.set_backend(None)
        policy_cedar.invalidate()
        policy_store.clear_change_listeners()
        # The KB detail's ingestion-job cache (N-SCN-obs11) is process-level too; a
        # `[]` cached by one test's detail read must not shadow another test's job.
        knowledge_selfhosted.reset_jobs_cache()
        # The attachables catalogue is a process-level TTL cache; a test that warmed it
        # would otherwise hand the next test a 200 off the cache even with the backend
        # cleared (xdist flake in test_feature_backends).
        registry_router._attachables_cache.clear()
        # D21 (M1-d): `create_app()` installs the embedding guard and the preset-snapshot
        # warning producer into process-level slots of the model registry.
        model_registry.set_embedding_guard(None)
        model_registry.clear_warning_producers()

    reset()
    yield
    reset()


@pytest.fixture(autouse=True)
def hermetic_policy_audit(monkeypatch):
    """D20 P1-d: `create_app()` installs the CloudWatch decisions sink, whose default
    client factory resolves the decision's workspace through the funnel. No hermetic test
    may build that `logs` client, so the factory is replaced by a recorder and the slot is
    emptied before AND after (the sink thread is process-level). Returns the recorder:
    `.calls` holds every `put_log_events` kwargs the sink shipped."""
    from unittest.mock import MagicMock

    from app.services import policy_audit, policy_store

    recorder = MagicMock(name="hermetic-logs")
    recorder.calls = []
    recorder.put_log_events.side_effect = lambda **kw: recorder.calls.append(kw) or {}
    monkeypatch.setattr(policy_audit, "_default_logs_client", lambda _ws: recorder)
    policy_audit.install(None)
    policy_store.reset_record_counter()
    yield recorder
    policy_audit.install(None)
    policy_store.reset_record_counter()


@pytest.fixture(autouse=True)
def hermetic_log_retention(monkeypatch):
    """N11-obs56: the deploy stage ends by setting the runtime log group's retention
    through the workspace's funnel `logs` client. A stage test that stubs only the control
    client (StubRuntimeControl, `_client_router`) must never build a real one, so the step
    is replaced by a recorder; `tests/test_log_retention.py` exercises the real function
    with a stub logs client. Returns the runtime ids the stages asked for."""
    from app.deployer import log_retention

    asked: list[str] = []

    def record(ctx, runtime_id):
        asked.append(runtime_id)
        return f"log-group retention: hermetic stub ({runtime_id})"

    monkeypatch.setattr(log_retention, "apply_runtime_log_retention", record)
    return asked


@pytest.fixture(autouse=True)
def reset_aws_client_cache():
    # Tests that stub aws_clients.get_session still populate the module-level
    # client cache; a fake cached under a real key must not leak across tests.
    from app.services import aws_clients

    yield
    aws_clients.reset_cache()


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "siliconflow(registry=False): R13 (N13-obs7) — SiliconFlow left the built-in seed; "
        "give this test the provider back the way an administrator adds it (see "
        "tests/model_fixtures.py). Snapshot path by default; registry path with the `client` "
        "fixture or registry=True.",
    )


@pytest.fixture(autouse=True)
def siliconflow_marker(request):
    """R13 (N13-obs7): a fresh ledger seeds Global Bedrock + Titan only. Tests of the
    `openai_compatible` kind opt back in with `@pytest.mark.siliconflow` (module-level
    `pytestmark` or per test): the provider + its four historical rows are published into the
    holder, or — when the test builds the app (`client`) or says `registry=True` — registered
    through the admin path on top of the Bedrock seed. The holder is reset afterwards."""
    marker = request.node.get_closest_marker("siliconflow")
    if marker is None:
        yield
        return
    from app.schemas import model_catalog
    from tests import model_fixtures

    registry = bool(marker.kwargs.get("registry")) or "client" in request.fixturenames
    if registry:
        if "client" in request.fixturenames:
            request.getfixturevalue("client")  # create_app → init_db + the Bedrock seed
        model_fixtures.register_siliconflow()
    else:
        model_fixtures.publish_siliconflow()
    yield
    model_catalog.reset()


@pytest.fixture(autouse=True)
def reset_model_role_overrides():
    # r15-e: the model-role override snapshot is process-global (`services/model_roles`),
    # while `clean_tables` wipes the `platform_settings` rows after every test — drop the
    # snapshot before AND after so a role set in one test never leaks into the next.
    from app.services import model_roles

    model_roles.reset_overrides()
    yield
    model_roles.reset_overrides()


@pytest.fixture(autouse=True)
def reset_model_registry_slots():
    # D21-obs9: the probe-hooks and embedding-guard slots are process-global
    # (`app.main` registers `ModelProbeHooks()` at import; `test_model_probe.py` sets
    # fakes), so a test asserting the empty slot (`require_probe_hooks` → 501) failed
    # whenever a sibling on the same xdist worker had imported the app first. Reset
    # BEFORE and after every test; the per-file `_hermetic` after-resets stay.
    from app.services import model_registry

    model_registry.set_probe_hooks(None)
    model_registry.set_embedding_guard(None)
    yield
    model_registry.set_probe_hooks(None)
    model_registry.set_embedding_guard(None)


@pytest.fixture(autouse=True)
def forget_test_deploy_methods():
    """N16-obs89e: a test that registers a fake deploy method (``pipeline.register_method`` in
    test_deploy_cancel / test_pipeline / test_spec_snapshots / …) must not leak it into a later
    test on the same xdist worker — test_method_capabilities asserts the four built-in methods
    exactly, so the batch gate went red purely on scheduling. Snapshot the registry before the
    test and drop every name it added afterwards (the built-ins are never touched)."""
    from app.deployer import pipeline as _pipeline

    before_methods = set(_pipeline._METHODS)
    before_caps = set(_pipeline._CAPABILITIES)
    yield
    for name in set(_pipeline._METHODS) - before_methods:
        _pipeline._METHODS.pop(name, None)
    for name in set(_pipeline._CAPABILITIES) - before_caps:
        _pipeline._CAPABILITIES.pop(name, None)
