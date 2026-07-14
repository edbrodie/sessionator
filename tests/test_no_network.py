"""Auditability: the no-telemetry pledge as a testable property (T-003).

Two static guarantees over ``src/sessionator``, enforced by a normal pytest run:

1. **No network-capable imports.** The package never imports a stdlib (or known
   third-party) module that can open a socket. A static AST scan is used rather
   than importing the package and inspecting ``sys.modules`` — pytest itself
   pulls in ``socket``/``ssl``, so a live ``sys.modules`` check would be a false
   positive. The scan reads the source, so the test's own environment is
   irrelevant.

2. **subprocess only ever launches an approved program.** Every
   ``subprocess.Popen/run/...`` call site launches either ``sys.executable`` (the
   detached backfill re-invokes Python) or a runtime-resolved harness-CLI path
   (``cli_path``) — never a hardcoded external binary. Alternate exec routes
   (``os.system``, ``os.popen``, ``os.exec*``/``os.spawn*``, ``pty.spawn``,
   ``subprocess.getoutput``) are banned outright.
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "sessionator"

# Top-level module names that can talk to the network. First path component is
# compared, so ``urllib.request`` / ``http.client`` are caught via ``urllib`` /
# ``http``.
FORBIDDEN_IMPORTS = {
    "socket", "ssl", "urllib", "http", "ftplib", "smtplib", "poplib",
    "imaplib", "nntplib", "telnetlib", "xmlrpc", "asyncio", "socketserver",
    "requests", "httpx", "aiohttp", "urllib3", "websocket", "websockets",
    "grpc", "paramiko",
}

# subprocess spawn functions and the argv-var / argv0 allowlists.
SPAWN_FUNCS = {"Popen", "run", "call", "check_call", "check_output"}
ALLOWED_ARGV_VARS = {"args"}       # a local holding a runtime-built argv list
ALLOWED_ARGV0_NAMES = {"cli_path"}  # a config-resolved harness CLI path

# Banned exec routes: (module, attr) pairs, plus attr-prefixes on os.
BANNED_CALLS = {
    ("os", "system"), ("os", "popen"),
    ("subprocess", "getoutput"), ("subprocess", "getstatusoutput"),
    ("pty", "spawn"), ("pty", "fork"),
}
BANNED_OS_PREFIXES = ("exec", "spawn")


def _py_files():
    return sorted(SRC.rglob("*.py"))


def _parse(path):
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


# --- 1. imports -------------------------------------------------------------

def _imported_top_level(tree):
    mods = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                mods.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                mods.add(node.module.split(".")[0])
    return mods


def test_no_network_capable_imports():
    offenders = {}
    for f in _py_files():
        bad = _imported_top_level(_parse(f)) & FORBIDDEN_IMPORTS
        if bad:
            offenders[str(f.relative_to(SRC))] = sorted(bad)
    assert not offenders, f"network-capable imports found: {offenders}"


# --- 2. subprocess ----------------------------------------------------------

def _is_sys_executable(node):
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "executable"
        and isinstance(node.value, ast.Name)
        and node.value.id == "sys"
    )


def _dotted(node):
    """(module, attr) for a ``mod.attr`` call target, else (None, attr/None)."""
    if isinstance(node, ast.Attribute):
        if isinstance(node.value, ast.Name):
            return node.value.id, node.attr
        return None, node.attr
    if isinstance(node, ast.Name):
        return None, node.id
    return None, None


def test_no_banned_exec_routes():
    offenders = []
    for f in _py_files():
        for node in ast.walk(_parse(f)):
            if not isinstance(node, ast.Call):
                continue
            mod, attr = _dotted(node.func)
            if (mod, attr) in BANNED_CALLS:
                offenders.append(f"{f.relative_to(SRC)}: {mod}.{attr}")
            elif mod == "os" and attr and attr.startswith(BANNED_OS_PREFIXES):
                offenders.append(f"{f.relative_to(SRC)}: os.{attr}")
    assert not offenders, f"banned exec routes: {offenders}"


def _spawn_calls(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            mod, attr = _dotted(node.func)
            if mod == "subprocess" and attr in SPAWN_FUNCS:
                yield node


def _first_positional(call):
    return call.args[0] if call.args else None


def _name_first_map(tree):
    """``var -> first-element node`` for simple ``var = [ ... ]`` assignments,
    so a ``[*base, …]`` argv can be resolved back to ``base``'s first element."""
    out = {}
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and isinstance(node.value, ast.List)
            and node.value.elts
        ):
            out[node.targets[0].id] = node.value.elts[0]
    return out


def _resolve_argv0(node, name_first, _seen=None):
    _seen = _seen or set()
    while isinstance(node, ast.Starred):
        node = node.value
    if isinstance(node, ast.Name) and node.id in name_first and node.id not in _seen:
        _seen.add(node.id)
        return _resolve_argv0(name_first[node.id], name_first, _seen)
    return node


def _argv0_allowed(node):
    if _is_sys_executable(node):
        return True
    return isinstance(node, ast.Name) and node.id in ALLOWED_ARGV0_NAMES


def test_subprocess_spawns_only_approved_programs():
    problems = []
    for f in _py_files():
        tree = _parse(f)
        name_first = _name_first_map(tree)
        for call in _spawn_calls(tree):
            argv = _first_positional(call)
            loc = f"{f.relative_to(SRC)}:{call.lineno}"
            if isinstance(argv, ast.List):
                if not argv.elts:
                    problems.append(f"{loc}: empty argv list")
                    continue
                argv0 = _resolve_argv0(argv.elts[0], name_first)
                if isinstance(argv0, ast.Constant):
                    problems.append(f"{loc}: hardcoded argv0 {argv0.value!r}")
                elif not _argv0_allowed(argv0):
                    problems.append(f"{loc}: unapproved argv0 {ast.dump(argv0)}")
            elif isinstance(argv, ast.Name):
                if argv.id not in ALLOWED_ARGV_VARS:
                    problems.append(f"{loc}: argv from unapproved var {argv.id!r}")
            else:
                problems.append(f"{loc}: unexpected argv shape {type(argv).__name__}")
    assert not problems, f"subprocess spawn issues: {problems}"


def _looks_like_argv_list(lst):
    """A list literal that carries an argv flag ('-x' / '--x' / 'exec') — i.e. a
    command line, not a data list. Used to force every such list to begin with an
    approved program even when it reaches subprocess indirectly (via a param)."""
    for elt in lst.elts:
        if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
            v = elt.value
            if v == "exec" or (v.startswith("-") and len(v) > 1):
                return True
    return False


def test_every_argv_list_starts_with_approved_program():
    problems = []
    for f in _py_files():
        tree = _parse(f)
        # Only relevant in files that actually spawn subprocesses.
        if "subprocess" not in _imported_top_level(tree):
            continue
        name_first = _name_first_map(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.List) and node.elts and _looks_like_argv_list(node):
                argv0 = _resolve_argv0(node.elts[0], name_first)
                if not _argv0_allowed(argv0):
                    problems.append(
                        f"{f.relative_to(SRC)}:{node.lineno}: argv list starts "
                        f"with {ast.dump(argv0)}"
                    )
    assert not problems, f"argv lists with unapproved argv0: {problems}"
