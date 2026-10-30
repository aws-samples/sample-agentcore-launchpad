"""r7-i addendum 6: backend log records carry level + logger name, once, without
stepping on a root logger somebody else configured."""

import io
import logging

from app.core import logging as lp_logging


def _with_bare_root(monkeypatch):
    """pytest's capture owns the real root handlers; give the function a root logger that
    looks like the one uvicorn leaves behind (no handlers, NOTSET). The quiet library
    loggers are reset to NOTSET too so a previous `configure_logging()` in the process
    does not leak into the test."""
    root = logging.getLogger()
    monkeypatch.setattr(root, "handlers", [])
    monkeypatch.setattr(root, "level", logging.NOTSET)
    for name in lp_logging.QUIET_LOGGERS + lp_logging.PINNED_LOGGERS:
        monkeypatch.setattr(logging.getLogger(name), "level", logging.NOTSET)
    return root


def test_configure_logging_installs_one_formatted_stderr_handler(monkeypatch):
    root = _with_bare_root(monkeypatch)
    assert lp_logging.configure_logging() is True
    (handler,) = root.handlers
    assert handler.get_name() == lp_logging.HANDLER_NAME
    assert handler.formatter._fmt == lp_logging.LOG_FORMAT == "%(levelname)s %(name)s: %(message)s"
    assert root.level == logging.INFO
    # a retry record renders greppably by level and by logger name
    buf = io.StringIO()
    handler.setStream(buf)
    logging.getLogger("app.services.opensearch_retry").warning(
        "opensearch POST https://x/launchpad-jobs/_search: ReadTimeoutError — retry 1/1 in 1s"
    )
    logging.getLogger("launchpad.knowledge").info("detail served from cache")
    lines = buf.getvalue().splitlines()
    assert lines[0].startswith("WARNING app.services.opensearch_retry: opensearch POST")
    assert lines[1] == "INFO launchpad.knowledge: detail served from cache"


def test_configure_logging_is_idempotent_and_defers_to_an_existing_config(monkeypatch):
    root = _with_bare_root(monkeypatch)
    assert lp_logging.configure_logging() is True
    assert lp_logging.configure_logging() is False  # second create_app() in a process
    assert len(root.handlers) == 1
    # an operator's own root handler (uvicorn --log-config, dictConfig) is left alone
    root = _with_bare_root(monkeypatch)
    theirs = logging.NullHandler()
    root.addHandler(theirs)
    assert lp_logging.configure_logging() is False
    assert root.handlers == [theirs]


def test_create_app_configures_logging_first():
    import inspect

    from app import main

    body = inspect.getsource(main.create_app)
    first_statement = body.split("\n", 1)[1].strip()
    assert first_statement.startswith("configure_logging()")


def test_uvicorn_access_logger_is_not_touched(monkeypatch):
    """Uvicorn's access logger has its own handler and `propagate=False`; our root handler
    must not change how (or whether) access lines are formatted."""
    _with_bare_root(monkeypatch)
    access = logging.getLogger("uvicorn.access")
    monkeypatch.setattr(access, "propagate", False)
    before = (list(access.handlers), access.propagate, access.level)
    lp_logging.configure_logging()
    assert (list(access.handlers), access.propagate, access.level) == before


def test_httpx_and_httpcore_are_quiet_at_warning_while_launchpad_stays_at_info(monkeypatch):
    """r7-j: root at INFO let `httpx` log one INFO line per model/judge call into the
    journal, burying the WARNINGs operators grep for."""
    root = _with_bare_root(monkeypatch)
    assert lp_logging.configure_logging() is True
    assert lp_logging.QUIET_LOGGERS == ("httpx", "httpcore")
    for name in lp_logging.QUIET_LOGGERS:
        assert logging.getLogger(name).level == logging.WARNING
    for name in ("launchpad.knowledge", "app.services.opensearch_retry", "launchpad"):
        assert logging.getLogger(name).getEffectiveLevel() == logging.INFO
    buf = io.StringIO()
    root.handlers[0].setStream(buf)
    logging.getLogger("httpx").info('HTTP Request: POST https://bedrock/converse "200 OK"')
    logging.getLogger("httpcore.connection").info("connect_tcp.started")
    logging.getLogger("httpx").warning("pool exhausted")
    logging.getLogger("launchpad.knowledge").info("detail served from cache")
    assert buf.getvalue().splitlines() == [
        "WARNING httpx: pool exhausted",
        "INFO launchpad.knowledge: detail served from cache",
    ]


def test_an_operator_s_explicit_httpx_level_is_left_alone(monkeypatch):
    _with_bare_root(monkeypatch)
    logging.getLogger("httpx").setLevel(logging.DEBUG)  # e.g. a dictConfig that wants it
    lp_logging.configure_logging()
    assert logging.getLogger("httpx").level == logging.DEBUG
    assert logging.getLogger("httpcore").level == logging.WARNING  # the untouched one still is


def test_quiet_loggers_are_applied_even_when_the_root_is_already_configured(monkeypatch):
    """`uvicorn --log-config` owning the root must not re-enable the per-request flood."""
    root = _with_bare_root(monkeypatch)
    root.addHandler(logging.NullHandler())
    assert lp_logging.configure_logging() is False
    assert logging.getLogger("httpx").level == logging.WARNING


def test_botocore_boto3_and_urllib3_are_pinned_at_info_even_under_a_root_at_debug(monkeypatch):
    """rev-r20 Nit 3: at DEBUG botocore logs request / response bodies — a PutParameter or
    GetParameter of a `launchpad-*-api-key` SecureString with its value. An operator's root
    DEBUG must not reach those loggers by inheritance; an explicit level on them is kept."""
    root = _with_bare_root(monkeypatch)
    assert lp_logging.configure_logging(logging.DEBUG) is True
    assert root.level == logging.DEBUG
    assert lp_logging.PINNED_LOGGERS == ("botocore", "boto3", "urllib3")
    for name in lp_logging.PINNED_LOGGERS:
        assert logging.getLogger(name).level == logging.INFO, name
        assert logging.getLogger(f"{name}.parsers").getEffectiveLevel() == logging.INFO, name
    buf = io.StringIO()
    root.handlers[0].setStream(buf)
    secret = "sk-live-LEAKED-0123456789abcdef"
    logging.getLogger("botocore.parsers").debug(
        "Response body: {'Parameter': {'Name': 'launchpad-ppio-api-key', 'Value': '%s'}}", secret)
    logging.getLogger("botocore.endpoint").debug("Making request ... body: Value=%s", secret)
    logging.getLogger("urllib3.connectionpool").debug("https://ssm... Value=%s", secret)
    logging.getLogger("botocore.credentials").info("Found credentials in environment")
    logging.getLogger("launchpad.model_probe").debug("probe detail")  # launchpad DEBUG renders
    lines = buf.getvalue().splitlines()
    assert secret not in buf.getvalue()
    assert lines == ["INFO botocore.credentials: Found credentials in environment",
                     "DEBUG launchpad.model_probe: probe detail"]
    # even when the operator owns the root (dictConfig at DEBUG), the pin still applies …
    root = _with_bare_root(monkeypatch)
    root.addHandler(logging.NullHandler())
    root.setLevel(logging.DEBUG)
    assert lp_logging.configure_logging() is False
    assert logging.getLogger("botocore").level == logging.INFO
    # … but an explicit operator level on one of them is left alone
    _with_bare_root(monkeypatch)
    logging.getLogger("botocore").setLevel(logging.WARNING)
    lp_logging.configure_logging()
    assert logging.getLogger("botocore").level == logging.WARNING
    assert logging.getLogger("boto3").level == logging.INFO
