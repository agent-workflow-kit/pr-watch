"""Import boundary: only `gateway` may start a process, so every call can be faked."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src" / "pr_watch"
ALLOWED = {"gateway.py"}
BANNED_MODULES = {"subprocess", "_posixsubprocess", "pty", "multiprocessing", "pexpect"}
BANNED_OS = {
    "system", "popen", "fork", "forkpty", "posix_spawn", "posix_spawnp", "startfile",
}  # fmt: skip
BANNED_OS_PREFIXES = ("exec", "spawn")


def violations(source: str) -> list[str]:
    """Every way `source` imports a process-starting module or calls an os process starter."""
    found: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            found += [f"import {a.name}" for a in node.names if _banned(a.name)]
        elif isinstance(node, ast.ImportFrom):
            if node.module and _banned(node.module):
                found.append(f"from {node.module} import ...")
            if node.module == "os":
                found += [f"from os import {a.name}" for a in node.names if _banned_os(a.name)]
        elif isinstance(node, ast.Attribute):
            if isinstance(node.value, ast.Name) and node.value.id == "os" and _banned_os(node.attr):
                found.append(f"os.{node.attr}")
        elif isinstance(node, ast.Call):
            name = _call_name(node)
            if name in ("__import__", "importlib.import_module", "import_module"):
                arg = node.args[0] if node.args else None
                if not isinstance(arg, ast.Constant) or _banned(str(arg.value)):
                    found.append(f"{name}(...)")  # dynamic or banned: refuse what cannot be proven
    return found


def _banned(module: str) -> bool:
    return module.split(".")[0] in BANNED_MODULES


def _banned_os(name: str) -> bool:
    return name in BANNED_OS or name.startswith(BANNED_OS_PREFIXES)


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
        "def f():\n    import subprocess\n",
        "try:\n    import subprocess\nexcept ImportError:\n    pass",
    ],
)
def test_detector_flags_every_way_in(source: str) -> None:
    assert violations(source)


@pytest.mark.parametrize(
    "source",
    ["import os\nos.getpid()", "from pr_watch import gateway\ngateway.run", "import json, sys"],
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
