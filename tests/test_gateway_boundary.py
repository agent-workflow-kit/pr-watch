"""Import boundary: only `gateway` may start, signal or replace a process (so calls can be faked).

Scope: a static scan (ast) of every module under src/pr_watch except gateway.py. It flags
importing a process-starting module (subprocess, pty, multiprocessing, ctypes, posix, runpy,
concurrent.futures.process, ...), calling os process starters or signallers (system, popen, fork,
exec*, spawn*, kill, killpg, setsid), asyncio subprocess helpers, ProcessPoolExecutor, and the
indirect routes to those (`import os as o`, `x = os`, `from os import *`, importlib and
`__import__` with a banned or non-literal name, `getattr` with a banned literal name).
Alias bindings are collected per module before the check, so order does not matter.

Limits, stated plainly: this is a lint against accidents and lazy shortcuts, not a sandbox.
Strings built at run time (`exec`, `eval`, `getattr(os, name)` with a variable, `globals()`
tricks) cannot be seen and are not detected. `socket` is out of scope: it starts no process.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src" / "pr_watch"
ALLOWED = {"gateway.py"}
BANNED_MODULES = {
    "subprocess", "_posixsubprocess", "pty", "multiprocessing", "pexpect", "ctypes", "posix",
    "runpy",
}  # fmt: skip
BANNED_DOTTED = ("concurrent.futures.process",)
BANNED_OS = {
    "system", "popen", "fork", "forkpty", "posix_spawn", "posix_spawnp", "startfile",
    "kill", "killpg", "setsid",
}  # fmt: skip
BANNED_OS_PREFIXES = ("exec", "spawn")
BANNED_ATTRS = {"ProcessPoolExecutor", "subprocess_exec", "subprocess_shell"}
BANNED_ATTR_PREFIXES = ("create_subprocess_",)
IMPORTERS = {"__import__", "importlib.import_module"}  # dotted, as the call is spelled


def violations(source: str) -> list[str]:
    """Every way `source` reaches a process starter, signaller or replacer."""
    tree = ast.parse(source)
    os_names, importer_names = _aliases(tree)
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found += [f"import {a.name}" for a in node.names if _banned_module(a.name)]
        elif isinstance(node, ast.ImportFrom):
            found += _from_import(node)
        elif isinstance(node, ast.Attribute):
            if isinstance(node.value, ast.Name) and node.value.id in os_names:
                if _banned_os(node.attr):
                    found.append(f"os.{node.attr}")
            if _banned_attr(node.attr) or node.attr == "__import__":
                found.append(f".{node.attr}")
        elif isinstance(node, ast.Name) and _banned_attr(node.id):
            found.append(node.id)
        elif isinstance(node, ast.Call):
            found += _call(node, importer_names)
    return found


def _aliases(tree: ast.AST) -> tuple[set[str], set[str]]:
    """Names bound to the os module, and names that import modules by string."""
    os_names = {"os"}
    importers = set(IMPORTERS)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name == "os" and a.asname:
                    os_names.add(a.asname)
                if a.name == "importlib" and a.asname:
                    importers.add(f"{a.asname}.import_module")
        elif isinstance(node, ast.ImportFrom):
            for a in node.names:
                bound = a.asname or a.name
                if node.module == "importlib" and a.name == "import_module":
                    importers.add(bound)
                if node.module == "builtins" and a.name == "__import__":
                    importers.add(bound)
                if node.module == "importlib" and a.name == "__import__":
                    importers.add(bound)
        elif isinstance(node, ast.Assign) and isinstance(node.value, ast.Name):
            if node.value.id in os_names:
                os_names.update(t.id for t in node.targets if isinstance(t, ast.Name))
            if node.value.id in importers:
                importers.update(t.id for t in node.targets if isinstance(t, ast.Name))
    return os_names, importers


def _from_import(node: ast.ImportFrom) -> list[str]:
    module = node.module or ""
    found: list[str] = []
    if _banned_module(module):
        found.append(f"from {module} import ...")
    for a in node.names:
        if module == "os" and (a.name == "*" or _banned_os(a.name)):
            found.append(f"from os import {a.name}")
        if _banned_module(f"{module}.{a.name}") and not _banned_module(module):
            found.append(f"from {module} import {a.name}")
        if _banned_attr(a.name):
            found.append(f"from {module} import {a.name}")
    return found


def _call(node: ast.Call, importers: set[str]) -> list[str]:
    name = _call_name(node)
    arg = node.args[0] if node.args else None
    if name in importers:
        if not isinstance(arg, ast.Constant) or _banned_module(str(arg.value)):
            return [f"{name}(...)"]  # dynamic or banned: refuse what cannot be proven
    if name == "getattr" and len(node.args) >= 2:
        lit = node.args[1]
        if isinstance(lit, ast.Constant) and isinstance(lit.value, str):
            if _banned_os(lit.value) or _banned_module(lit.value) or _banned_attr(lit.value):
                return [f"getattr(..., {lit.value!r})"]
    return []


def _banned_module(module: str) -> bool:
    return module.split(".")[0] in BANNED_MODULES or any(
        module == d or module.startswith(d + ".") for d in BANNED_DOTTED
    )


def _banned_os(name: str) -> bool:
    return name in BANNED_OS or name.startswith(BANNED_OS_PREFIXES)


def _banned_attr(name: str) -> bool:
    return name in BANNED_ATTRS or name.startswith(BANNED_ATTR_PREFIXES)


def _call_name(node: ast.Call) -> str:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        return f"{func.value.id}.{func.attr}"
    return ""


def test_only_the_gateway_starts_processes() -> None:
    sources = sorted(SRC.rglob("*.py"))
    assert sources, "no sources found: the boundary test would pass vacuously"
    assert SRC / "gateway.py" in sources
    offenders = {
        p.relative_to(SRC).as_posix(): v
        for p in sources
        if p.name not in ALLOWED and (v := violations(p.read_text(encoding="utf-8")))
    }
    assert offenders == {}


def test_the_gateway_itself_does_use_subprocess() -> None:
    # Guards the detector: if it could not see the gateway's import, it could see nothing.
    assert violations((SRC / "gateway.py").read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    "source",
    [
        "import subprocess",
        "import subprocess as sp",
        "import os, subprocess",
        "from subprocess import run",
        "from subprocess import *",
        "import subprocess.something",
        "import multiprocessing",
        "from multiprocessing import Process",
        "import pty",
        "import os\nos.system('x')",
        "import os\nos.popen('x')",
        "import os\nos.execvp('x', [])",
        "import os\nos.spawnlp(0, 'x')",
        "import os\nos.fork()",
        "from os import system",
        "from os import execv",
        "__import__('subprocess')",
        "import importlib\nimportlib.import_module('subprocess')",
        "import importlib\nimportlib.import_module(name)",
        "import ctypes",
        "from ctypes import CDLL",
        "import posix",
        "import runpy",
        "import concurrent.futures.process",
        "from concurrent.futures.process import ProcessPoolExecutor",
        "from concurrent.futures import ProcessPoolExecutor",
        "from concurrent.futures import process",
        "import concurrent.futures as cf\ncf.ProcessPoolExecutor()",
        "import asyncio\nasyncio.create_subprocess_exec('x')",
        "import asyncio\nasyncio.create_subprocess_shell('x')",
        "from asyncio import create_subprocess_exec",
        "import asyncio\nasyncio.get_event_loop().subprocess_exec(None, 'x')",
        "import os as o\no.system('x')",
        "import os as o\no.fork()",
        "import os\nx = os\nx.execv('a', [])",
        "from os import *",
        "from os import system as s",
        "import os\nos.kill(1, 9)",
        "import os\nos.killpg(1, 9)",
        "import os\nos.setsid()",
        "from os import kill",
        "import importlib as il\nil.import_module('subprocess')",
        "from importlib import import_module as im\nim('subprocess')",
        "from importlib import import_module as im\nim(name)",
        "from importlib import import_module\nimport_module('ctypes')",
        "import builtins\nx = builtins.__import__\nx('os')",
        "from builtins import __import__ as imp\nimp('subprocess')",
        "getattr(__import__('os'), 'system')",
        "import os\ngetattr(os, 'system')('x')",
        "import os\ngetattr(os, 'kill')",
        "def f():\n    import subprocess\n",
        "try:\n    import subprocess\nexcept ImportError:\n    pass",
    ],
)
def test_detector_flags_every_way_in(source: str) -> None:
    assert violations(source)


@pytest.mark.parametrize(
    "source",
    [
        "import os\nos.getpid()",
        "from pr_watch import gateway\ngateway.run",
        "import json, sys",
        "import os\nos.path.join('a', 'b')",
        "import os as o\no.path.join('a', 'b')\no.environ.get('X')",
        "from os import path, getcwd",
        "import importlib\nimportlib.import_module('json')",
        "x = getattr(obj, 'name')",
        "import concurrent.futures\nconcurrent.futures.ThreadPoolExecutor()",
        "import asyncio\nasyncio.sleep(1)",
    ],
)
def test_detector_accepts_innocent_code(source: str) -> None:
    assert violations(source) == []


def test_a_new_module_importing_subprocess_fails_the_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pkg = tmp_path / "pr_watch"
    pkg.mkdir()
    (pkg / "gateway.py").write_text("import subprocess\n")
    (pkg / "sneaky.py").write_text("import subprocess\n")
    monkeypatch.setattr("tests.test_gateway_boundary.SRC", pkg)
    with pytest.raises(AssertionError, match="sneaky.py"):
        test_only_the_gateway_starts_processes()
