"""The fake crontab in tests/fakebin: grammar, exit codes, and proof the real one is unreachable."""

from __future__ import annotations

import os
import pwd
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.conftest import FAKEBIN


def crontab(
    *args: str, stdin: bytes = b"", env: dict[str, str] | None = None
) -> tuple[int, bytes, str]:
    proc = subprocess.run(
        ["crontab", *args], input=stdin, capture_output=True, timeout=30, check=False, env=env
    )
    return proc.returncode, proc.stdout, proc.stderr.decode()


def test_crontab_by_name_is_the_fake_not_the_stub_and_leaves_no_hit(sandbox_bin: Path) -> None:
    assert shutil.which("crontab") == str(FAKEBIN / "crontab")
    assert crontab("-l")[0] == 1
    assert (sandbox_bin / "hits.log").read_text() == ""


def test_no_real_crontab_is_on_path(sandbox_bin: Path) -> None:
    # PATH holds only tests/fakebin (the fake) and the sandbox bin (the exit-99 stub)
    assert os.environ["PATH"].split(os.pathsep) == [str(FAKEBIN), str(sandbox_bin)]
    assert not (sandbox_bin / "crontab").is_symlink()
    assert os.environ["HOME"] != pwd.getpwuid(os.geteuid()).pw_dir


def test_list_with_no_table_is_exit_1_and_the_cron_message() -> None:
    code, out, err = crontab("-l")
    assert (code, out) == (1, b"")
    assert err.startswith("no crontab for ")


def test_install_from_stdin_then_list_is_byte_exact() -> None:
    table = b"MAILTO=x\r\n* * * * * echo \xff\xfe\n# c\n"
    assert crontab("-", stdin=table)[0] == 0
    assert crontab("-l")[1] == table


def test_a_table_without_a_final_newline_is_refused_and_the_table_is_kept() -> None:
    crontab("-", stdin=b"# keep\n")
    code, _, err = crontab("-", stdin=b"# a\n* * * * * x")
    assert code == 1
    assert err == "new crontab file is missing newline before EOF, can't install.\n"
    assert crontab("-l")[1] == b"# keep\n"


def test_empty_stdin_installs_an_empty_table() -> None:
    assert crontab("-", stdin=b"")[0] == 0
    assert crontab("-l")[:2] == (0, b"")


def test_remove_then_remove_again() -> None:
    crontab("-", stdin=b"# x\n")
    assert crontab("-r")[0] == 0
    code, _, err = crontab("-r")
    assert code == 1 and err.startswith("no crontab for ")
    assert crontab("-l")[0] == 1


@pytest.mark.parametrize(
    "line", ["* * * * echo hi", "garbage", "61 x y z w cmd", "@sometimes cmd", "1 2 3 4 5"]
)
def test_malformed_lines_are_refused_and_the_table_is_kept(line: str) -> None:
    crontab("-", stdin=b"# keep\n")
    code, _, err = crontab("-", stdin=f"{line}\n".encode())
    assert code == 1 and "errors in crontab file" in err
    assert crontab("-l")[1] == b"# keep\n"


def test_nul_byte_is_refused() -> None:
    assert crontab("-", stdin=b"# a\0b\n")[0] == 1


@pytest.mark.parametrize("args", [(), ("-e",), ("-u", "root", "-l"), ("-l", "x"), ("file",)])
def test_other_grammar_is_a_usage_error(args: tuple[str, ...]) -> None:
    code, _, err = crontab(*args)
    assert code == 1 and err.startswith("usage:")


def test_write_failure_switch(isolated_env: Path) -> None:
    (isolated_env / ".fake-crontab-fail-writes").touch()
    assert crontab("-", stdin=b"# x\n")[0] == 1
    assert crontab("-l")[0] == 1  # nothing was stored


def test_the_fake_refuses_the_real_home(monkeypatch: pytest.MonkeyPatch) -> None:
    real = pwd.getpwuid(os.geteuid()).pw_dir
    monkeypatch.setenv("HOME", real)
    code, _, err = crontab("-l")
    assert code == 97 and "real home" in err
    assert not (Path(real) / ".fake-crontab").exists()
    monkeypatch.setenv("HOME", "relative")
    assert crontab("-l")[0] == 97


def test_the_table_lives_under_the_sandbox_home(isolated_env: Path) -> None:
    crontab("-", stdin=b"# x\n")
    assert (isolated_env / ".fake-crontab").read_bytes() == b"# x\n"
