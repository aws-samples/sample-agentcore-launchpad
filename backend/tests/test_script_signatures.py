"""`backend/scripts/*` call the app layer, and nothing else checks that they can.

The scripts are live-only (real AWS, `make bootstrap` first), so they are not part
of the verify gate — a service function that gains a parameter leaves them
importable and ruff-clean while raising `TypeError` on the first real run. That is
how the workspace threading broke `e2e_knowledge_base.py` (every `knowledge.*`
call gained a leading workspace) and `e2e_kb_gateway.py`
(`get_cognito_token(workspace, username)` — arity still bound, so only the type
was wrong).

Two checks per call site: the arguments must bind to the live signature, and a
parameter annotated `WorkspaceContext` must not receive a string/number literal
(the shape that made the `get_cognito_token` break bind cleanly while passing
`"admin"` as the workspace).

Both work without importing the scripts: several parse `sys.argv` at import time,
and one starts a real canary from module scope. Deliberately conservative —
anything it cannot resolve statically (classes, `*args`, locally shadowed names,
attributes of non-module objects, a workspace passed through a variable) is
skipped rather than guessed at, so a failure here is a real mismatch.
"""

import ast
import importlib
import inspect
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"


def _imported(module_name: str, attr: str):
    try:
        module = importlib.import_module(module_name)
    except Exception:  # pragma: no cover - an unimportable app module fails elsewhere
        return None
    try:
        return getattr(module, attr)
    except AttributeError:
        try:
            return importlib.import_module(f"{module_name}.{attr}")
        except Exception:
            return None


def _app_imports(tree: ast.AST) -> tuple[dict[str, object], dict[str, object]]:
    """Local name → app function, and local name → app module.

    Walks nested imports too: some scripts import inside the function that uses
    the symbol, which is exactly where drift hides.
    """
    functions: dict[str, object] = {}
    modules: dict[str, object] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if not node.module or not node.module.startswith("app"):
                continue
            for alias in node.names:
                target = _imported(node.module, alias.name)
                local = alias.asname or alias.name
                if inspect.isfunction(target):
                    functions[local] = target
                elif inspect.ismodule(target):
                    modules[local] = target
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if not alias.name.startswith("app"):
                    continue
                target = _imported(alias.name.rsplit(".", 1)[0], alias.name.rsplit(".", 1)[-1])
                if inspect.ismodule(target):
                    modules[alias.asname or alias.name.split(".")[0]] = target
    return functions, modules


def _mismatches(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    functions, modules = _app_imports(tree)
    shadowed = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    }
    found: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        target = None
        if isinstance(func, ast.Name) and func.id not in shadowed:
            target = functions.get(func.id)
        elif isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
            module = modules.get(func.value.id)
            candidate = getattr(module, func.attr, None) if module is not None else None
            target = candidate if inspect.isfunction(candidate) else None
        if target is None:
            continue
        if any(isinstance(arg, ast.Starred) for arg in node.args) or any(
            kw.arg is None for kw in node.keywords
        ):
            continue  # unpacking: the call shape is not knowable statically
        signature = inspect.signature(target)
        where = f"{path.name}:{node.lineno}: {ast.unparse(func)}(…)"
        described = f"{target.__module__}.{target.__qualname__}{signature}"
        try:
            # Bound to the AST nodes rather than to placeholders, so the argument
            # that landed on each parameter can be inspected below.
            bound = signature.bind(*node.args, **{kw.arg: kw.value for kw in node.keywords})
        except TypeError as exc:
            found.append(f"{where} does not match {described} — {exc}")
            continue
        for name, argument in bound.arguments.items():
            annotation = str(signature.parameters[name].annotation)
            if "WorkspaceContext" not in annotation:
                continue
            if not isinstance(argument, ast.Constant):
                continue
            if argument.value is None and "None" in annotation:
                continue  # an optional workspace: the callee resolves it
            found.append(
                f"{where} passes the literal {argument.value!r} as the workspace "
                f"parameter {name!r} of {described}"
            )
    return found


def test_every_script_call_matches_the_live_app_signature():
    mismatches = [line for path in sorted(SCRIPTS_DIR.glob("*.py")) for line in _mismatches(path)]
    assert not mismatches, (
        "backend/scripts calls the app layer with a signature that no longer exists. "
        "These scripts only run against real AWS, so nothing else catches it:\n"
        + "\n".join(mismatches)
    )


DEPLOYER_DIR = Path(__file__).resolve().parents[1] / "app" / "deployer"


def test_every_deployer_call_matches_the_live_service_signature():
    """The deploy stages run on background threads, and their unit tests stub
    the service functions they call — so a stale call arity there survives the
    suite and only fails on a live deploy (the us-east-2 register stage did
    exactly that, 2026-08-12). Bind the deployer's cross-package calls the same
    way the scripts are bound."""
    mismatches = [
        line for path in sorted(DEPLOYER_DIR.glob("*.py")) for line in _mismatches(path)
    ]
    assert not mismatches, (
        "app/deployer calls a service with a signature that no longer exists; "
        "stage unit tests stub these calls, so only a live deploy catches it:\n"
        + "\n".join(mismatches)
    )


def test_the_scanner_still_resolves_app_calls():
    """Guards the guard: if the resolution logic silently stopped matching
    anything, the assertion above would pass vacuously forever."""
    resolved = 0
    for path in sorted(SCRIPTS_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        functions, modules = _app_imports(tree)
        resolved += len(functions) + len(modules)
    assert resolved > 20, f"only {resolved} app symbols resolved across backend/scripts"
