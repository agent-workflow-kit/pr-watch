"""Fake `crontab` for tests (entry point: tests/fakebin/crontab). It shadows the sandbox's exit-99
stub because tests/fakebin is first on PATH.

The table lives in one scratch file, `$HOME/.fake-crontab` (HOME is the per-test sandbox home), and
the real crontab is never read or written. Refused (exit 97) when HOME is not absolute or is the
password-database home of the current user, so a mis-set environment cannot be mistaken for a
sandbox (this fake never touches a real table either way; it only refuses to hide that mistake).

Argv grammar (the parts pr-watch uses, as in cronie and vixie cron):

- `crontab -l`   print the table byte for byte; with no table: stderr `no crontab for <user>`,
                 exit 1.
- `crontab -`    replace the table with stdin (empty stdin gives an empty table, as cronie does;
                 non-empty stdin without a final newline is refused: `new crontab file is missing
                 newline before EOF, can't install.`, exit 1, as cronie and vixie cron do).
                 The text is checked line by line like cron does: a malformed line makes the
                 install fail with exit 1 and `errors in crontab file, can't install.`, the stored
                 table unchanged.
- `crontab -r`   remove the table; with none: stderr `no crontab for <user>`, exit 1.
- anything else (`-e`, `-u`, a file operand, unknown flags, extra operands): usage text, exit 1.

Known gaps against a real cron: only plausible line shapes are checked (see `_valid_line`); a
table is not size-limited; `-i`, `-u`, `-e` and `-T` are not implemented; LC_ALL is ignored (the
message is always the C-locale one pr-watch looks for).

A file `$HOME/.fake-crontab-fail-writes` makes `-` and `-r` fail with exit 1 (the gateway passes
only PATH and HOME to a child, so a marker file is the switch a test can reach).
"""

import os
import pwd
import re
import sys

EXIT_REFUSED = 97
_ENV_LINE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\s*=")
_NUMERIC = re.compile(r"^[0-9*/,\-]+$")
_NAMED = re.compile(r"^[0-9*/,\-A-Za-z]+$")  # month and weekday fields allow names
_MONTHS = frozenset("jan feb mar apr may jun jul aug sep oct nov dec".split())
_WEEKDAYS = frozenset("sun mon tue wed thu fri sat".split())
_LIMITS = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))  # a number outside its field is an error
_SPECIAL = {
    "@reboot",
    "@yearly",
    "@annually",
    "@monthly",
    "@weekly",
    "@daily",
    "@midnight",
    "@hourly",
}


def _user() -> str:
    try:
        return pwd.getpwuid(os.geteuid()).pw_name
    except KeyError:
        return str(os.geteuid())


def _table_path() -> str:
    home = os.environ.get("HOME", "")
    if not home.startswith("/"):
        sys.stderr.write("fake-crontab: HOME is not an absolute sandbox directory\n")
        sys.exit(EXIT_REFUSED)
    try:
        real_home = pwd.getpwuid(os.geteuid()).pw_dir
    except KeyError:
        real_home = ""
    if real_home and os.path.realpath(home) == os.path.realpath(real_home):
        sys.stderr.write("fake-crontab: HOME is the real home directory; refusing\n")
        sys.exit(EXIT_REFUSED)
    return os.path.join(home, ".fake-crontab")


def _field_ok(index: int, field: str) -> bool:
    if not (_NAMED if index >= 3 else _NUMERIC).match(field):
        return False
    names = _MONTHS if index == 3 else _WEEKDAYS
    if any(word.lower() not in names for word in re.findall(r"[A-Za-z]+", field)):
        return False
    low, high = _LIMITS[index]
    for number in re.findall(r"[0-9]+", field):
        if not low <= int(number) <= high:
            return field.startswith("*/") and int(number) > 0
    return True


def _valid_line(line: str) -> bool:
    stripped = line.strip(" \t\r")
    if not stripped or stripped.startswith("#"):
        return True
    if _ENV_LINE.match(stripped):
        return True
    parts = stripped.split(None, 5)
    if parts[0].startswith("@"):
        return parts[0] in _SPECIAL and len(parts) >= 2
    return len(parts) == 6 and all(_field_ok(i, p) for i, p in enumerate(parts[:5]))


def _writes_fail() -> bool:
    return os.path.exists(os.path.join(os.environ["HOME"], ".fake-crontab-fail-writes"))


def _no_crontab() -> int:
    sys.stderr.write(f"no crontab for {_user()}\n")
    return 1


def _usage() -> int:
    sys.stderr.write("usage: crontab [-l | -r | -]\n")
    return 1


def _read(path: str) -> bytes | None:
    try:
        with open(path, "rb") as fh:
            return fh.read()
    except FileNotFoundError:
        return None


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    path = _table_path()
    if len(argv) != 1:
        return _usage()
    flag = argv[0]
    if flag == "-l":
        data = _read(path)
        if data is None:
            return _no_crontab()
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()
        return 0
    if flag == "-r":
        if _writes_fail():
            sys.stderr.write("fake-crontab: write refused\n")
            return 1
        try:
            os.unlink(path)
        except FileNotFoundError:
            return _no_crontab()
        return 0
    if flag == "-":
        data = sys.stdin.buffer.read()
        if _writes_fail():
            sys.stderr.write("fake-crontab: write refused\n")
            return 1
        if data and not data.endswith(b"\n"):
            sys.stderr.write("new crontab file is missing newline before EOF, can't install.\n")
            return 1
        text = data.decode("utf-8", "surrogateescape")
        if b"\0" in data or not all(_valid_line(line) for line in text.split("\n")):
            sys.stderr.write("errors in crontab file, can't install.\n")
            return 1
        tmp = f"{path}.{os.getpid()}"
        with open(tmp, "wb") as fh:
            fh.write(data)
        os.replace(tmp, path)
        return 0
    return _usage()
