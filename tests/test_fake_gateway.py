"""The fake backend plug-in point: scripted stdout, stderr, exits, delays and oversized output."""

from __future__ import annotations

import signal
from typing import Any

import pytest

from pr_watch import gateway
from pr_watch.gateway import STDERR_CAP, LaunchError
from tests.gateway_support import FakeGateway


def call(*argv: str, **kw: Any) -> gateway.Result:
    kw.setdefault("env", {})
    kw.setdefault("timeout", 10)
    kw.setdefault("max_output", 1000)
    return gateway.run(list(argv), **kw)


def test_scripts_stdout_stderr_and_exit_code(fake_gateway: FakeGateway) -> None:
    fake_gateway.on("forgectl", "show", stdout='{"a": 1}', stderr="warn", returncode=20)
    result = call("forgectl", "show", "x")
    assert (result.stdout, result.stderr, result.returncode) == ('{"a": 1}', "warn", 20)


def test_calls_are_recorded_with_the_validated_request(fake_gateway: FakeGateway) -> None:
    fake_gateway.on("crontab")
    call("crontab", "-", stdin="line\n", env={"A": "1"}, cwd="/tmp")
    request = fake_gateway.calls[0]
    assert fake_gateway.argvs() == [["crontab", "-"]]
    assert (request.stdin, dict(request.env), request.cwd) == (b"line\n", {"A": "1"}, "/tmp")


def test_scripts_match_by_prefix_then_custom_matcher_first_wins(fake_gateway: FakeGateway) -> None:
    fake_gateway.on("forgectl", "snapshot", match=lambda r: "bad" in r.argv, returncode=1)
    fake_gateway.on("forgectl", stdout="generic")
    assert call("forgectl", "snapshot", "bad").returncode == 1
    assert call("forgectl", "snapshot", "good").stdout == "generic"


def test_times_limits_a_script_then_falls_through(fake_gateway: FakeGateway) -> None:
    fake_gateway.on("t", returncode=1, times=2).on("t", returncode=0)
    assert [call("t").returncode for _ in range(4)] == [1, 1, 0, 0]


def test_a_delay_longer_than_the_timeout_is_a_timeout(fake_gateway: FakeGateway) -> None:
    fake_gateway.on("slow", delay=30, stdout="never")
    result = call("slow", timeout=5)
    assert result.timed_out and result.returncode == -signal.SIGTERM and result.stdout == ""
    assert result.elapsed == 5


def test_a_delay_within_the_timeout_just_succeeds_without_real_sleeping(
    fake_gateway: FakeGateway,
) -> None:
    fake_gateway.on("slow", delay=3, stdout="ok")
    result = call("slow", timeout=5)
    assert (result.stdout, result.timed_out, result.elapsed) == ("ok", False, 3)


def test_oversized_output_is_cut_at_the_cap_and_flagged(fake_gateway: FakeGateway) -> None:
    fake_gateway.on("big", stdout="x" * 40_000)
    result = call("big", max_output=1000)
    assert result.too_large and len(result.stdout) == 1000 and result.returncode == -signal.SIGTERM


def test_output_exactly_at_the_cap_is_fine(fake_gateway: FakeGateway) -> None:
    fake_gateway.on("edge", stdout="x" * 1000)
    assert not call("edge", max_output=1000).too_large


def test_stderr_is_capped_like_the_real_backend(fake_gateway: FakeGateway) -> None:
    fake_gateway.on("noisy", stderr="e" * (STDERR_CAP + 10))
    result = call("noisy")
    assert len(result.stderr) == STDERR_CAP and result.stderr_truncated


def test_bytes_output_decodes_like_the_real_backend(fake_gateway: FakeGateway) -> None:
    fake_gateway.on("raw", stdout=b"\xff ok")
    assert call("raw").stdout == "\udcff ok"


def test_a_launch_error_can_be_scripted(fake_gateway: FakeGateway) -> None:
    fake_gateway.on("gone", launch_error="gone: not found in PATH")
    with pytest.raises(LaunchError):
        call("gone")


def test_an_unscripted_call_fails_loudly(fake_gateway: FakeGateway) -> None:
    fake_gateway.on("known")
    with pytest.raises(AssertionError, match="unscripted gateway call"):
        call("unknown")


def test_the_real_backend_is_restored_after_the_fixture() -> None:
    with gateway.use_backend(lambda r: gateway.Result(returncode=5)):
        assert call("anything").returncode == 5
    assert call("echo", "real", env=gateway.minimal_env()).stdout == "real\n"


def test_discard_output_drops_stdout_and_stderr_like_the_real_backend(
    fake_gateway: FakeGateway,
) -> None:
    fake_gateway.on("quiet", stdout="out", stderr="err")
    result = call("quiet", discard_output=True)
    assert (result.stdout, result.stderr, result.stderr_truncated) == ("", "", False)


def test_discard_output_drops_an_oversized_stderr_flag_too(fake_gateway: FakeGateway) -> None:
    fake_gateway.on("noisy", stderr="e" * (STDERR_CAP + 10))
    assert not call("noisy", discard_output=True).stderr_truncated
