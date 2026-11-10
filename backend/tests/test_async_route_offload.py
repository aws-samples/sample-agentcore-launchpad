"""N-SCN-obs13: `async def` routes that do blocking work (S3 puts, git clones, zip
extraction) must hand it to the threadpool, never run it on the event loop.

Two layers: a hermetic guard that walks every mounted route and fails when an
``async def`` endpoint *calls* a known blocking helper outside ``run_in_threadpool`` /
``to_thread``; and behavioural checks that the two routes this round fixed actually go
through ``run_in_threadpool`` (patched to record what it was handed)."""

import ast
import inspect
import textwrap

import pytest
from fastapi.routing import APIRoute

from app.main import create_app
from app.services import knowledge_backend
from app.services.knowledge_selfhosted import SelfHostedKnowledgeBackend
from tests.conftest import set_default_resources
from tests.memory_fakes import FakeEmbedder, FakeOpenSearch, FakeS3, FakeSQS
from tests.test_memory_selfhosted import SUBSTRATE, T0

# Callee spellings (as `ast.unparse(call.func)` renders them) that block the thread:
# the routers' `_guard(...)` wrappers around backend methods, a backend method called on
# the Protocol slot, boto3/requests/subprocess, and the registry's acquire/stage helpers
# (git clone, URL fetch, zip extraction to disk).
BLOCKING_CALLEES = (
    "_guard",
    "get_backend().",
    "boto3.",
    "requests.",
    "subprocess.",
    "_acquire_source",
    "_stage_and_respond",
    "bundle_from_zip",
    "bundles_from_git",
    "bundle_from_url",
)
# Wrapping calls that move the work off the loop.
OFFLOADERS = ("run_in_threadpool", "to_thread", "run_sync")
# Legitimately async endpoints that trip a pattern for a documented reason — none today.
ALLOWLIST: dict[str, str] = {}


def _api_routes():
    def walk(routes):
        for route in routes:
            if type(route).__name__ == "_IncludedRouter":
                yield from walk(route.original_router.routes)
            else:
                yield route

    return [r for r in walk(create_app().routes) if isinstance(r, APIRoute)]


def _offloaded(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> bool:
    cur = parents.get(node)
    while cur is not None:
        if isinstance(cur, ast.Call) and any(
            name in ast.unparse(cur.func) for name in OFFLOADERS
        ):
            return True
        cur = parents.get(cur)
    return False


def _violations(fn) -> list[str]:
    tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
    parents: dict[ast.AST, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        callee = ast.unparse(node.func)
        if any(p in callee for p in BLOCKING_CALLEES) and not _offloaded(node, parents):
            out.append(f"line {node.lineno}: {ast.unparse(node)[:90]}")
    return out


def test_async_routes_do_not_call_blocking_helpers_on_the_event_loop():
    """Walk every mounted route; for each `async def` endpoint, every call to a known
    blocking helper must sit inside `run_in_threadpool(...)` / `to_thread` /
    `run_sync`. Passing the helper *as an argument* to the offloader is the fix shape
    (`await run_in_threadpool(_guard, backend.upload_files, ...)`), so it is not a call
    node and does not trip the check."""
    offenders = {}
    async_routes = 0
    for route in _api_routes():
        fn = route.endpoint
        if not inspect.iscoroutinefunction(fn):
            continue
        async_routes += 1
        name = f"{fn.__module__}.{fn.__name__}"
        if name in ALLOWLIST:
            continue
        found = _violations(fn)
        if found:
            offenders[name] = found
    assert async_routes >= 3  # the studio routers, BYOC upload, KB upload, skill inspect
    assert offenders == {}, "\n".join(f"{k}: {v}" for k, v in offenders.items())


def test_the_guard_recognises_the_bad_shape_and_the_fixed_shape():
    """Self-check of the detector on the two shapes this round was about."""

    async def bad(request, kb_id):  # pragma: no cover — parsed, never run
        files = await request.form()
        return _guard(knowledge_backend.get_backend().upload_files, kb_id, files)  # noqa: F821

    async def good(request, kb_id):  # pragma: no cover — parsed, never run
        from fastapi.concurrency import run_in_threadpool

        files = await request.form()
        return await run_in_threadpool(
            _guard, knowledge_backend.get_backend().upload_files, kb_id, files,  # noqa: F821
        )

    assert len(_violations(bad)) == 1 and "_guard(" in _violations(bad)[0]
    assert _violations(good) == []


class _Recorder:
    """Stands in for `run_in_threadpool`: records the callable and runs it inline."""

    def __init__(self):
        self.calls: list[tuple] = []

    async def __call__(self, fn, *args, **kwargs):
        self.calls.append((fn, args, kwargs))
        return fn(*args, **kwargs)


@pytest.fixture
def kb_backend(client):
    fakes = {
        "s3": FakeS3(), "opensearch": FakeOpenSearch(), "sqs": FakeSQS(),
        "embedder": FakeEmbedder(),
    }
    fakes["opensearch"].ensure_index("launchpad-jobs", {})
    be = SelfHostedKnowledgeBackend(**fakes, clock=lambda: T0)
    knowledge_backend.set_backend(be)
    set_default_resources(SUBSTRATE)
    return be, fakes


def test_kb_file_upload_runs_the_s3_put_in_the_threadpool(client, kb_backend, monkeypatch):
    from app.routers import knowledge as knowledge_router
    from tests.conftest import ws_ctx

    be, fakes = kb_backend
    kb = be.create_kb(ws_ctx(SUBSTRATE, region="cn-northwest-1"), "docs", "", {"mode": "upload"})
    rec = _Recorder()
    monkeypatch.setattr(knowledge_router, "run_in_threadpool", rec)
    res = client.post(
        f"/api/knowledge-bases/{kb['kb_id']}/files",
        files={"files": ("a.txt", b"hello", "text/plain")},
    )
    assert res.status_code == 200, res.text
    assert len(res.json()["keys"]) == 1
    (fn, args, _), = rec.calls
    assert fn is knowledge_router._guard and args[0].__name__ == "upload_files"
    assert fakes["s3"].objects  # the put really happened, through the recorder


def test_kb_file_upload_errors_still_map_through_guard_inside_the_threadpool(
    client, kb_backend, monkeypatch,
):
    """The offload keeps `_guard`'s translation: a failing backend still yields the
    `kb.*` envelope, not a 500."""
    from app.routers import knowledge as knowledge_router
    from tests.conftest import ws_ctx

    be, fakes = kb_backend
    kb = be.create_kb(ws_ctx(SUBSTRATE, region="cn-northwest-1"), "docs", "", {"mode": "upload"})

    def boom(*a, **k):
        raise RuntimeError("s3 is down")

    fakes["s3"].put_object = boom
    rec = _Recorder()
    monkeypatch.setattr(knowledge_router, "run_in_threadpool", rec)
    res = client.post(
        f"/api/knowledge-bases/{kb['kb_id']}/files",
        files={"files": ("a.txt", b"hello", "text/plain")},
    )
    assert res.status_code == 502 and res.json()["code"] == "kb.unavailable"
    assert len(rec.calls) == 1


def test_skill_inspect_acquires_and_stages_in_the_threadpool(client, monkeypatch):
    from app.routers import registry as registry_router
    from tests.test_registry_update import _multifile_zip

    rec = _Recorder()
    monkeypatch.setattr(registry_router, "run_in_threadpool", rec)
    res = client.post(
        "/api/registry/skills/inspect",
        files={"file": ("bundle.zip", _multifile_zip(), "application/zip")},
    )
    assert res.status_code == 200, res.text
    assert [fn.__name__ for fn, _, _ in rec.calls] == ["bundle_from_zip", "_stage_and_respond"]
    registry_router._drop_staging(res.json()["staging_id"])


def test_skill_inspect_json_source_is_acquired_in_the_threadpool(client, monkeypatch):
    from app.routers import registry as registry_router

    rec = _Recorder()
    monkeypatch.setattr(registry_router, "run_in_threadpool", rec)
    res = client.post(
        "/api/registry/skills/inspect", json={"source": {"kind": "bogus"}},
    )
    assert res.status_code == 400 and res.json()["code"] == "registry.invalid_source"
    assert [fn.__name__ for fn, _, _ in rec.calls] == ["_acquire_source"]
