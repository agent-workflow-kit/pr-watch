"""Config loading (docs/interface.md, Config and Environment variables): files, drop-ins,
environment, per-host and per-repo overrides, hostile YAML and the ownership and mode checks."""

from __future__ import annotations

import os
import stat
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import pytest
import yaml

import pr_watch.config as config_module
from pr_watch.config import (
    ENV_KEYS,
    Config,
    ConfigError,
    LoadedConfig,
    default_config_path,
    duration_ms,
    load_config,
)
from tests.harness import ROOT

Write = Callable[..., Path]
SECRET = "sekrit-value-123"


@pytest.fixture
def cfgdir(isolated_env: Path) -> Path:
    """The default config directory inside the fake HOME (mode 0700)."""
    directory = isolated_env / ".config" / "pr-watch"
    directory.mkdir(parents=True, mode=0o700)
    return directory


@pytest.fixture
def write() -> Write:
    def _write(path: Path, text: str, mode: int = 0o600) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        path.chmod(mode)
        return path

    return _write


@pytest.fixture
def dropins(cfgdir: Path) -> Path:
    directory = cfgdir / "config.d"
    directory.mkdir(mode=0o700)
    return directory


def load(path: Path | None = None, env: Mapping[str, str] | None = None) -> LoadedConfig:
    return load_config(path, environ=env if env is not None else {})


def one_file(cfgdir: Path, write: Write, text: str, env: Mapping[str, str] | None = None) -> Config:
    write(cfgdir / "config.yaml", text)
    return load(env=env).config


def invalid(
    cfgdir: Path, write: Write, text: str, env: Mapping[str, str] | None = None
) -> ConfigError:
    write(cfgdir / "config.yaml", text)
    with pytest.raises(ConfigError) as info:
        load(env=env)
    return info.value


# --- defaults and locations --------------------------------------------------------------------


def test_defaults_without_any_file(isolated_env: Path) -> None:
    loaded = load()
    c = loaded.config
    assert loaded.files == ()
    assert loaded.warnings == ()
    assert c.poll.interval_hint == "5m"
    assert c.poll.full_every is None and c.poll.concurrency is None
    assert (c.poll.host_low_water, c.poll.overlap, c.poll.hosts_ttl) == (100, "60s", "1h")
    assert (c.poll.max_duration, c.poll.max_snapshots) == ("15m", 40)
    assert c.hosts == {} and c.repos == {}
    assert c.retention.events == "30d"
    assert c.expiry.after_terminal == "7d"
    assert c.failures.unreachable_after == 5
    assert c.detect_force_push is False
    assert (c.ci.settle_polls, c.ci.pending_max_age) == (1, "6h")
    assert (c.forgectl.timeout, c.forgectl.max_output_mib) == ("120s", 32)
    assert (c.hooks.max_per_poll, c.hooks.max_per_pr_per_poll, c.hooks.concurrency) == (50, 10, 4)
    assert c.hooks.env_allow == [] and c.hooks.on_approved == []
    assert c.cron.env_file is None
    assert c.me.name is None


def test_empty_and_comment_only_files_give_defaults(cfgdir: Path, write: Write) -> None:
    assert one_file(cfgdir, write, "").poll.interval_hint == "5m"
    assert one_file(cfgdir, write, "# nothing\n").poll.interval_hint == "5m"


def test_default_path_uses_xdg_config_home_when_absolute(tmp_path: Path) -> None:
    env = {"XDG_CONFIG_HOME": str(tmp_path / "xdg"), "HOME": str(tmp_path / "h")}
    assert default_config_path(env) == tmp_path / "xdg" / "pr-watch" / "config.yaml"


def test_default_path_ignores_relative_xdg_config_home(tmp_path: Path) -> None:
    env = {"XDG_CONFIG_HOME": "relative/dir", "HOME": str(tmp_path / "h")}
    assert default_config_path(env) == tmp_path / "h" / ".config" / "pr-watch" / "config.yaml"


def test_load_reads_default_file_and_reports_files(cfgdir: Path, write: Write) -> None:
    main = write(cfgdir / "config.yaml", "poll: {concurrency: 7}\n")
    env = {"HOME": str(cfgdir.parent.parent), "XDG_CONFIG_HOME": str(cfgdir.parent)}
    loaded = load_config(None, environ=env)
    assert loaded.config.poll.concurrency == 7
    assert loaded.files == (main,)


def test_explicit_config_replaces_default_file(cfgdir: Path, write: Write, tmp_path: Path) -> None:
    write(cfgdir / "config.yaml", "poll: {concurrency: 7}\n")
    other = write(tmp_path / "other.yaml", "poll: {concurrency: 9}\n")
    assert load(other).config.poll.concurrency == 9


def test_explicit_config_must_exist(tmp_path: Path) -> None:
    missing = tmp_path / "nope.yaml"
    with pytest.raises(ConfigError) as info:
        load(missing)
    assert "nope.yaml" in str(info.value)


def test_explicit_dangling_symlink_is_an_error(tmp_path: Path) -> None:
    link = tmp_path / "link.yaml"
    link.symlink_to(tmp_path / "gone.yaml")
    with pytest.raises(ConfigError):
        load(link)


# --- drop-ins ----------------------------------------------------------------------------------


def test_dropins_merge_after_main_file_in_lexical_order(
    cfgdir: Path, dropins: Path, write: Write
) -> None:
    write(
        cfgdir / "config.yaml",
        "poll: {concurrency: 1, full_every: 2}\nfailures: {unreachable_after: 3}\n",
    )
    write(dropins / "20-b.yaml", "poll: {concurrency: 3}\n")
    write(dropins / "10-a.yaml", "poll: {concurrency: 2, host_low_water: 7}\n")
    c = load().config
    assert c.poll.concurrency == 3  # 20-b is last
    assert c.poll.full_every == 2  # maps merge, untouched keys survive
    assert c.poll.host_low_water == 7
    assert c.failures.unreachable_after == 3


def test_dropin_order_is_bytewise(cfgdir: Path, dropins: Path, write: Write) -> None:
    write(dropins / "B.yaml", "poll: {concurrency: 2}\n")
    write(dropins / "a.yaml", "poll: {concurrency: 3}\n")
    assert load().config.poll.concurrency == 3  # "B" < "a" bytewise, so a.yaml is last


def test_hook_lists_append_across_files(cfgdir: Path, dropins: Path, write: Write) -> None:
    write(cfgdir / "config.yaml", "hooks:\n  on-approved: [{cmd: one}]\n  on-ci: [{cmd: ci}]\n")
    write(dropins / "10.yaml", "hooks:\n  on-approved: [{cmd: two, timeout: 5}]\n")
    write(dropins / "20.yaml", "hooks:\n  on-approved: [{cmd: three, async: false}]\n")
    hooks = load().config.hooks
    assert [h.cmd for h in hooks.on_approved] == ["one", "two", "three"]
    assert [h.timeout for h in hooks.on_approved] == [30, 5, 30]
    assert [h.async_ for h in hooks.on_approved] == [True, True, False]
    assert [h.cmd for h in hooks.on_ci] == ["ci"]


def test_env_allow_is_replaced_not_appended(cfgdir: Path, dropins: Path, write: Write) -> None:
    write(cfgdir / "config.yaml", "hooks: {env_allow: [A, B]}\n")
    write(dropins / "10.yaml", "hooks: {env_allow: [C]}\n")
    assert load().config.hooks.env_allow == ["C"]


def test_hosts_and_repos_maps_merge_per_key(cfgdir: Path, dropins: Path, write: Write) -> None:
    write(cfgdir / "config.yaml", "hosts:\n  github.com: {concurrency: 2}\n")
    write(
        dropins / "10.yaml",
        "hosts:\n  github.com: {full_every: 9}\n  gitlab.com: {concurrency: 1}\n",
    )
    hosts = load().config.hosts
    assert hosts["github.com"].concurrency == 2 and hosts["github.com"].full_every == 9
    assert hosts["gitlab.com"].concurrency == 1


def test_dropins_are_read_without_a_main_file(dropins: Path, write: Write) -> None:
    write(dropins / "10.yaml", "poll: {concurrency: 5}\n")
    assert load().config.poll.concurrency == 5


def test_only_visible_yaml_files_are_dropins(dropins: Path, write: Write) -> None:
    write(dropins / "10.yaml", "poll: {concurrency: 5}\n")
    write(dropins / "20.yml", "poll: {concurrency: 6}\n")
    write(dropins / "30.yaml.bak", "poll: {concurrency: 7}\n")
    write(dropins / ".40.yaml", "poll: {concurrency: 8}\n")
    write(dropins / "50.txt", "not yaml: [\n")
    assert load().config.poll.concurrency == 5


def test_explicit_config_reads_dropins_beside_it(tmp_path: Path, write: Write) -> None:
    main = write(tmp_path / "custom" / "x.yaml", "poll: {concurrency: 2}\n")
    (tmp_path / "custom" / "config.d").mkdir(mode=0o700)
    write(tmp_path / "custom" / "config.d" / "10.yaml", "poll: {full_every: 4}\n")
    c = load(main).config
    assert (c.poll.concurrency, c.poll.full_every) == (2, 4)


def test_dropin_error_names_the_dropin_file(cfgdir: Path, dropins: Path, write: Write) -> None:
    write(cfgdir / "config.yaml", "poll: {concurrency: 2}\n")
    bad = write(dropins / "10-bad.yaml", "poll: {concurrency: 0}\n")
    with pytest.raises(ConfigError) as info:
        load()
    assert str(bad) in str(info.value) and "poll.concurrency" in str(info.value)
    assert info.value.source == str(bad)
    assert info.value.key == "poll.concurrency"


def test_dropin_directory_named_like_a_file_is_refused(dropins: Path) -> None:
    (dropins / "x.yaml").mkdir()
    with pytest.raises(ConfigError, match=r"x\.yaml"):
        load()


def test_config_d_that_is_not_a_directory_is_refused(cfgdir: Path) -> None:
    (cfgdir / "config.d").write_text("x")
    with pytest.raises(ConfigError, match="config.d"):
        load()


def test_too_many_dropins_are_refused(dropins: Path, write: Write) -> None:
    for i in range(257):
        write(dropins / f"{i:04}.yaml", "")
    with pytest.raises(ConfigError, match="drop-in"):
        load()


# --- hostile YAML ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "- a\n- b\n",
        "just a string\n",
        "42\n",
        "true\n",
    ],
)
def test_non_mapping_root_is_refused(cfgdir: Path, write: Write, text: str) -> None:
    err = invalid(cfgdir, write, text)
    assert "mapping" in str(err) and "config.yaml" in str(err)


def test_alias_bomb_is_refused_without_expansion(cfgdir: Path, write: Write) -> None:
    lines = ["a0: &a0 [x, x, x, x, x, x, x, x, x]"]
    for i in range(1, 12):
        refs = ", ".join([f"*a{i - 1}"] * 9)
        lines.append(f"a{i}: &a{i} [{refs}]")
    err = invalid(cfgdir, write, "\n".join(lines) + "\n")
    assert "alias" in str(err) or "anchor" in str(err)


def test_anchor_alone_is_refused(cfgdir: Path, write: Write) -> None:
    assert "anchor" in str(invalid(cfgdir, write, "poll: &p {concurrency: 2}\n"))


def test_explicit_tags_are_refused_and_never_run(cfgdir: Path, write: Write) -> None:
    marker = cfgdir / "pwned"
    text = f"poll: !!python/object/apply:os.system ['touch {marker}']\n"
    err = invalid(cfgdir, write, text)
    assert "tag" in str(err)
    assert not marker.exists()


def test_a_plain_str_tag_is_refused_too(cfgdir: Path, write: Write) -> None:
    assert "tag" in str(invalid(cfgdir, write, "poll: {interval_hint: !!str 5m}\n"))


def test_duplicate_keys_are_refused(cfgdir: Path, write: Write) -> None:
    err = invalid(cfgdir, write, "poll: {concurrency: 1}\npoll: {concurrency: 2}\n")
    assert "duplicate" in str(err)


def test_duplicate_nested_keys_are_refused(cfgdir: Path, write: Write) -> None:
    err = invalid(cfgdir, write, "poll:\n  concurrency: 1\n  concurrency: 2\n")
    assert "duplicate" in str(err)


def test_non_string_keys_are_refused(cfgdir: Path, write: Write) -> None:
    assert "string" in str(invalid(cfgdir, write, "1: x\n"))


def test_yaml_merge_key_is_not_special(cfgdir: Path, write: Write) -> None:
    err = invalid(cfgdir, write, "<<: {poll: {concurrency: 2}}\n")
    assert "<<" in str(err)


@pytest.mark.parametrize("word", ["yes", "no", "on", "off", "Yes", "y"])
def test_yaml_1_1_booleans_are_not_booleans(cfgdir: Path, write: Write, word: str) -> None:
    err = invalid(cfgdir, write, f"detect_force_push: {word}\n")
    assert "detect_force_push" in str(err)


@pytest.mark.parametrize("number", ["0x10", "0o17", "1_0", "010", "5.0", "1e1"])
def test_non_decimal_integers_are_refused(cfgdir: Path, write: Write, number: str) -> None:
    err = invalid(cfgdir, write, f"poll: {{concurrency: {number}}}\n")
    assert "poll.concurrency" in str(err)


def test_dates_stay_strings(cfgdir: Path, write: Write) -> None:
    err = invalid(cfgdir, write, "poll: {interval_hint: 2001-01-01}\n")
    assert "poll.interval_hint" in str(err)


def test_invalid_syntax_names_file_and_never_echoes_input(cfgdir: Path, write: Write) -> None:
    err = invalid(cfgdir, write, f"poll: {{concurrency: [{SECRET}\n")
    assert "config.yaml" in str(err) and "line" in str(err)
    assert SECRET not in str(err)


def test_multiple_documents_are_refused(cfgdir: Path, write: Write) -> None:
    invalid(cfgdir, write, "poll: {concurrency: 2}\n---\npoll: {concurrency: 3}\n")


def test_invalid_utf8_is_refused(cfgdir: Path) -> None:
    path = cfgdir / "config.yaml"
    path.write_bytes(b"poll: {interval_hint: \xff\xfe}\n")
    path.chmod(0o600)
    with pytest.raises(ConfigError, match="UTF-8"):
        load()


def test_deep_nesting_is_an_error_not_a_crash(cfgdir: Path, write: Write) -> None:
    text = "a: " + "[" * 5000 + "]" * 5000 + "\n"
    err = invalid(cfgdir, write, text)
    assert "config.yaml" in str(err)


def test_oversized_file_is_refused(cfgdir: Path, write: Write) -> None:
    err = invalid(cfgdir, write, "# " + "x" * (256 * 1024) + "\n")
    assert "larger" in str(err)


def test_fifo_is_refused_without_blocking(cfgdir: Path) -> None:
    os.mkfifo(cfgdir / "config.yaml", 0o600)
    with pytest.raises(ConfigError, match="regular"):
        load()


def test_unreadable_file_is_an_error(cfgdir: Path, write: Write) -> None:
    if os.geteuid() == 0:
        pytest.skip("root reads everything")
    write(cfgdir / "config.yaml", "", mode=0o000)
    with pytest.raises(ConfigError, match="cannot be read"):
        load()


# --- validation --------------------------------------------------------------------------------


def test_unknown_top_level_key_is_named(cfgdir: Path, write: Write) -> None:
    err = invalid(cfgdir, write, "bogus: 1\n")
    assert err.key == "bogus" and "bogus" in str(err) and "config.yaml" in str(err)


def test_unknown_nested_keys_are_named(cfgdir: Path, write: Write) -> None:
    assert invalid(cfgdir, write, "poll: {bogus: 1}\n").key == "poll.bogus"
    assert invalid(cfgdir, write, "hooks: {on-nothing: []}\n").key == "hooks.on-nothing"
    assert invalid(cfgdir, write, "hooks: {on_pushed: []}\n").key == "hooks.on_pushed"
    assert (
        invalid(cfgdir, write, "hosts: {github.com: {bogus: 1}}\n").key == "hosts.github.com.bogus"
    )
    assert (
        invalid(cfgdir, write, "repos: {github.com/o/r: {poll: {}}}\n").key
        == "repos.github.com/o/r.poll"
    )
    assert invalid(cfgdir, write, "me: {github: x}\n").key == "me.github"


@pytest.mark.parametrize(
    ("text", "key"),
    [
        ("poll: {concurrency: '4'}\n", "poll.concurrency"),
        ("poll: {concurrency: true}\n", "poll.concurrency"),
        ("poll: {concurrency: 4.5}\n", "poll.concurrency"),
        ("poll: {concurrency: null}\n", "poll.concurrency"),
        ("poll: {full_every: ~}\n", "poll.full_every"),
        ("poll: 5\n", "poll"),
        ("poll: [1]\n", "poll"),
        ("detect_force_push: 1\n", "detect_force_push"),
        ("detect_force_push: 'true'\n", "detect_force_push"),
        ("detect_force_push: null\n", "detect_force_push"),
        ("hooks: {env_allow: FOO}\n", "hooks.env_allow"),
        ("hooks: {env_allow: [1]}\n", "hooks.env_allow.0"),
        ("hooks: {on-ci: {cmd: x}}\n", "hooks.on-ci"),
        ("hooks: {on-ci: [x]}\n", "hooks.on-ci.0"),
        ("hooks: {on-ci: [{timeout: 5}]}\n", "hooks.on-ci.0.cmd"),
        ("hooks: {on-ci: [{cmd: ''}]}\n", "hooks.on-ci.0.cmd"),
        ("hooks: {on-ci: [{cmd: 5}]}\n", "hooks.on-ci.0.cmd"),
        ("hooks: {on-ci: [{cmd: x, timeout: '5'}]}\n", "hooks.on-ci.0.timeout"),
        ("hooks: {on-ci: [{cmd: x, async: 1}]}\n", "hooks.on-ci.0.async"),
        ("hooks: {on-ci: [{cmd: x, extra: 1}]}\n", "hooks.on-ci.0.extra"),
        ("cron: {env_file: 5}\n", "cron.env_file"),
        ("me: {name: null}\n", "me.name"),
        ("me: {name: ''}\n", "me.name"),
        ("retention: {events: 30}\n", "retention.events"),
        ("forgectl: {timeout: 120}\n", "forgectl.timeout"),
    ],
)
def test_wrong_types_name_the_key(cfgdir: Path, write: Write, text: str, key: str) -> None:
    err = invalid(cfgdir, write, text)
    assert err.key == key
    assert key in str(err)


@pytest.mark.parametrize(
    ("text", "key"),
    [
        ("poll: {concurrency: 0}\n", "poll.concurrency"),
        ("poll: {concurrency: 65}\n", "poll.concurrency"),
        ("poll: {full_every: 0}\n", "poll.full_every"),
        ("poll: {full_every: 10001}\n", "poll.full_every"),
        ("poll: {host_low_water: -1}\n", "poll.host_low_water"),
        ("poll: {max_snapshots: -1}\n", "poll.max_snapshots"),
        ("poll: {max_snapshots: 1001}\n", "poll.max_snapshots"),
        ("failures: {unreachable_after: 0}\n", "failures.unreachable_after"),
        ("failures: {unreachable_after: 1001}\n", "failures.unreachable_after"),
        ("ci: {settle_polls: -1}\n", "ci.settle_polls"),
        ("ci: {settle_polls: 11}\n", "ci.settle_polls"),
        ("forgectl: {max_output_mib: 0}\n", "forgectl.max_output_mib"),
        ("forgectl: {max_output_mib: 257}\n", "forgectl.max_output_mib"),
        ("hooks: {max_per_poll: -1}\n", "hooks.max_per_poll"),
        ("hooks: {max_per_poll: 100001}\n", "hooks.max_per_poll"),
        ("hooks: {max_per_pr_per_poll: 0}\n", "hooks.max_per_pr_per_poll"),
        ("hooks: {max_per_pr_per_poll: 1001}\n", "hooks.max_per_pr_per_poll"),
        ("hooks: {concurrency: 0}\n", "hooks.concurrency"),
        ("hooks: {concurrency: 33}\n", "hooks.concurrency"),
        ("hooks: {on-ci: [{cmd: x, timeout: 0}]}\n", "hooks.on-ci.0.timeout"),
        ("hooks: {on-ci: [{cmd: x, timeout: 601}]}\n", "hooks.on-ci.0.timeout"),
        ("hooks: {on-ci: [{cmd: x, timeout: -5}]}\n", "hooks.on-ci.0.timeout"),
        ("hosts: {github.com: {concurrency: 65}}\n", "hosts.github.com.concurrency"),
        ("hosts: {github.com: {full_every: 0}}\n", "hosts.github.com.full_every"),
        (
            "repos: {github.com/o/r: {ci: {settle_polls: 11}}}\n",
            "repos.github.com/o/r.ci.settle_polls",
        ),
        (
            "repos: {github.com/o/r: {failures: {unreachable_after: 0}}}\n",
            "repos.github.com/o/r.failures.unreachable_after",
        ),
        ("me: {name: " + "x" * 129 + "}\n", "me.name"),
        ("cron: {env_file: ''}\n", "cron.env_file"),
    ],
)
def test_out_of_range_values_name_the_key(cfgdir: Path, write: Write, text: str, key: str) -> None:
    err = invalid(cfgdir, write, text)
    assert err.key == key and key in str(err)


@pytest.mark.parametrize(
    "value",
    [
        "29d",
        "7d",
        "0d",
        "30h",
        "720h",
        "30",
        "30.5d",
        "030d",
        "-30d",
        "30D",
        "30d\n",
        " 30d",
        "d",
        "",
    ],
)
def test_retention_below_30_days_or_not_whole_days_is_refused_not_clamped(
    cfgdir: Path, write: Write, value: str
) -> None:
    err = invalid(cfgdir, write, f"retention: {{events: {yaml.safe_dump(value).strip()}}}\n")
    assert err.key == "retention.events"


@pytest.mark.parametrize("value", ["30d", "31d", "99d", "100d", "365d", "9999d"])
def test_retention_accepts_whole_days_of_at_least_30(
    cfgdir: Path, write: Write, value: str
) -> None:
    assert one_file(cfgdir, write, f"retention: {{events: {value}}}\n").retention.events == value


@pytest.mark.parametrize(
    "value", ["1m", "5m", "10m", "15m", "20m", "30m", "60m", "1h", "2h", "12h", "24h"]
)
def test_interval_hint_accepts_the_every_forms(cfgdir: Path, write: Write, value: str) -> None:
    assert (
        one_file(cfgdir, write, f"poll: {{interval_hint: {value}}}\n").poll.interval_hint == value
    )


@pytest.mark.parametrize(
    "value",
    [
        "7m",
        "45m",
        "61m",
        "120m",
        "5s",
        "5",
        "0m",
        "0h",
        "5H",
        "5M",
        "5m\n",
        "5d",
        "5h30m",
        "25h",
        "5h ",
        "7h",
        "9h",
        "48h",
        "-5m",
        "1.5h",
    ],
)
def test_interval_hint_refuses_other_forms(cfgdir: Path, write: Write, value: str) -> None:
    err = invalid(cfgdir, write, f"poll: {{interval_hint: {yaml.safe_dump(value).strip()}}}\n")
    assert err.key == "poll.interval_hint"


@pytest.mark.parametrize("value", ["60s", "1.5s", "5 m", "5x", "-5s", "s", "5m\n", "1e3s", "five"])
def test_bad_durations_are_refused(cfgdir: Path, write: Write, value: str) -> None:
    err = invalid(
        cfgdir,
        write,
        f"poll: {{overlap: {yaml.safe_dump(value).strip()}}}\n"
        if value != "60s"
        else "poll: {overlap: 60}\n",
    )
    assert err.key == "poll.overlap"


@pytest.mark.parametrize("value", ["0", "0s", "500ms", "60s", "5m", "1h", "7d"])
def test_durations_accept_the_documented_forms(cfgdir: Path, write: Write, value: str) -> None:
    c = one_file(cfgdir, write, f"poll: {{overlap: '{value}'}}\n")
    assert c.poll.overlap == value


def test_absurdly_long_duration_digits_are_an_error_not_a_crash(cfgdir: Path, write: Write) -> None:
    err = invalid(cfgdir, write, "poll: {overlap: '" + "9" * 5000 + "s'}\n")
    assert err.key == "poll.overlap"


def test_duration_ms_converts_every_unit() -> None:
    assert duration_ms("0") == 0
    assert duration_ms("250ms") == 250
    assert duration_ms("60s") == 60_000
    assert duration_ms("5m") == 300_000
    assert duration_ms("2h") == 7_200_000
    assert duration_ms("30d") == 30 * 86_400_000
    with pytest.raises(ValueError):
        duration_ms("1.5s")


def test_error_never_echoes_the_offending_value(cfgdir: Path, write: Write) -> None:
    for text in (
        f"poll: {{interval_hint: {SECRET}}}\n",
        f"poll: {{concurrency: {SECRET}}}\n",
        f"detect_force_push: {SECRET}\n",
        f"retention: {{events: {SECRET}}}\n",
        f"hooks: {{env_allow: [LD_{SECRET}]}}\n",
        f"hooks: {{on-ci: [{{cmd: x, timeout: {SECRET}}}]}}\n",
    ):
        assert SECRET not in str(invalid(cfgdir, write, text)), text


def test_several_errors_are_listed_and_capped(cfgdir: Path, write: Write) -> None:
    err = invalid(cfgdir, write, "poll: {concurrency: 0, full_every: 0, host_low_water: -1}\n")
    assert "poll.concurrency" in str(err) and "poll.full_every" in str(err)
    assert "poll.host_low_water" in str(err)
    many = "\n".join(f"k{i}: 1" for i in range(30))
    err = invalid(cfgdir, write, many + "\n")
    assert "more" in str(err)


def test_keys_with_control_characters_are_escaped_in_messages(cfgdir: Path, write: Write) -> None:
    err = invalid(cfgdir, write, 'poll: {"a\\x1b[31mb": 1}\n')
    assert "\x1b" not in str(err)


@pytest.mark.parametrize("host", ["github.com", "gitlab.example.com:8443", "a", "a.b-c.d", "h:1"])
def test_valid_host_keys(cfgdir: Path, write: Write, host: str) -> None:
    c = one_file(cfgdir, write, f"hosts: {{'{host}': {{concurrency: 3}}}}\n")
    assert c.hosts[host].concurrency == 3


@pytest.mark.parametrize(
    "host", ["GitHub.com", "bad host", "-x", "x-", "a..b", "h:123456", "", "a/b"]
)
def test_invalid_host_keys(cfgdir: Path, write: Write, host: str) -> None:
    err = invalid(cfgdir, write, f"hosts: {{'{host}': {{concurrency: 3}}}}\n")
    assert err.key is not None and err.key.startswith("hosts")


@pytest.mark.parametrize("repo", ["github.com/o/r", "gitlab.example.com:8443/g/s/p", "h/a.b_c-d"])
def test_valid_repo_keys(cfgdir: Path, write: Write, repo: str) -> None:
    c = one_file(cfgdir, write, f"repos: {{'{repo}': {{detect_force_push: true}}}}\n")
    assert c.repos[repo].detect_force_push is True


@pytest.mark.parametrize(
    "repo",
    [
        "github.com",
        "github.com/o/../r",
        "github.com/./r",
        "github.com//r",
        "github.com/o r",
        "GitHub.com/o/r",
        "github.com/o/",
        "/o/r",
    ],
)
def test_invalid_repo_keys(cfgdir: Path, write: Write, repo: str) -> None:
    err = invalid(cfgdir, write, f"repos: {{'{repo}': {{detect_force_push: true}}}}\n")
    assert err.key is not None and err.key.startswith("repos")


def test_repo_override_null_is_refused(cfgdir: Path, write: Write) -> None:
    err = invalid(cfgdir, write, "repos: {h/o/r: {detect_force_push: null}}\n")
    assert err.key == "repos.h/o/r.detect_force_push"


# --- hooks.env_allow ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name", ["PR_WATCH_X", "PR_WATCH_", "LD_PRELOAD", "LD_", "DYLD_INSERT_LIBRARIES"]
)
def test_env_allow_reserved_prefixes_are_rejected(cfgdir: Path, write: Write, name: str) -> None:
    err = invalid(cfgdir, write, f"hooks: {{env_allow: [FOO, {name}]}}\n")
    assert err.key == "hooks.env_allow.1"
    assert "PR_WATCH_" in str(err)


@pytest.mark.parametrize("name", ["1X", "A=B", "A B", "", "A-B", "é", "A" * 129])
def test_env_allow_invalid_names_are_rejected(cfgdir: Path, write: Write, name: str) -> None:
    err = invalid(cfgdir, write, f"hooks: {{env_allow: ['{name}']}}\n")
    assert err.key == "hooks.env_allow.0"


@pytest.mark.parametrize(
    "name",
    ["GH_TOKEN", "MY_SECRET", "API_KEY", "DB_PASSWORD", "my_token", "KEYCHAIN", "XPASSWORDX"],
)
def test_env_allow_secret_like_names_warn(cfgdir: Path, write: Write, name: str) -> None:
    write(cfgdir / "config.yaml", f"hooks: {{env_allow: [HOME, {name}]}}\n")
    loaded = load()
    assert loaded.config.hooks.env_allow == ["HOME", name]
    assert [w.code for w in loaded.warnings] == ["config_warning"]
    assert name in loaded.warnings[0].message
    assert "HOME" not in loaded.warnings[0].message
    assert loaded.warnings[0].key == "hooks.env_allow"


@pytest.mark.parametrize("name", ["HOME", "LANG", "PATH", "EDITOR", "SHELL", "LOGNAME"])
def test_env_allow_ordinary_names_do_not_warn(cfgdir: Path, write: Write, name: str) -> None:
    write(cfgdir / "config.yaml", f"hooks: {{env_allow: [{name}]}}\n")
    assert load().warnings == ()


def test_env_allow_warning_comes_once_per_name_after_merge(
    cfgdir: Path, dropins: Path, write: Write
) -> None:
    write(cfgdir / "config.yaml", "hooks: {env_allow: [GH_TOKEN]}\n")
    write(dropins / "10.yaml", "hooks: {env_allow: [GH_TOKEN, OTHER_SECRET]}\n")
    assert len(load().warnings) == 2


# --- environment overrides ---------------------------------------------------------------------

SAMPLE_ENV: dict[str, str] = {
    "poll.interval_hint": "10m",
    "poll.full_every": "9",
    "poll.concurrency": "8",
    "poll.host_low_water": "5",
    "poll.overlap": "30s",
    "poll.hosts_ttl": "2h",
    "poll.max_duration": "20m",
    "poll.max_snapshots": "0",
    "retention.events": "45d",
    "expiry.after_terminal": "3d",
    "failures.unreachable_after": "9",
    "detect_force_push": "true",
    "ci.settle_polls": "3",
    "ci.pending_max_age": "2h",
    "forgectl.timeout": "30s",
    "forgectl.max_output_mib": "64",
    "hooks.max_per_poll": "0",
    "hooks.max_per_pr_per_poll": "2",
    "hooks.concurrency": "2",
    "me.name": "alice",
}


def env_name(path: str) -> str:
    return "PR_WATCH_" + path.upper().replace(".", "__")


def attr(config: Config, path: str) -> Any:
    value: Any = config
    for part in path.split("."):
        value = getattr(value, part)
    return value


def test_sample_env_covers_every_env_key() -> None:
    assert set(SAMPLE_ENV) == set(ENV_KEYS)


@pytest.mark.parametrize("path", sorted(SAMPLE_ENV))
def test_each_env_key_overrides_its_default(isolated_env: Path, path: str) -> None:
    c = load(env={env_name(path): SAMPLE_ENV[path]}).config
    expected: Any = SAMPLE_ENV[path]
    if expected in ("true", "false"):
        expected = expected == "true"
    elif expected.isdigit():
        expected = int(expected)
    assert attr(c, path) == expected


def test_env_overrides_files(cfgdir: Path, dropins: Path, write: Write) -> None:
    write(cfgdir / "config.yaml", "poll: {concurrency: 2, full_every: 3}\n")
    write(dropins / "10.yaml", "poll: {concurrency: 3}\n")
    c = load(env={"PR_WATCH_POLL__CONCURRENCY": "6"}).config
    assert c.poll.concurrency == 6 and c.poll.full_every == 3


def test_files_override_builtins_and_env_overrides_files_for_detect_force_push(
    cfgdir: Path, write: Write
) -> None:
    write(cfgdir / "config.yaml", "detect_force_push: true\n")
    assert load().config.detect_force_push is True
    assert load(env={"PR_WATCH_DETECT_FORCE_PUSH": "false"}).config.detect_force_push is False


@pytest.mark.parametrize("value", ["True", "TRUE", "1", "0", "yes", "no", "", " true", "true "])
def test_env_bool_parse_errors_name_the_variable(isolated_env: Path, value: str) -> None:
    with pytest.raises(ConfigError) as info:
        load(env={"PR_WATCH_DETECT_FORCE_PUSH": value})
    assert "PR_WATCH_DETECT_FORCE_PUSH" in str(info.value)
    assert info.value.key == "detect_force_push"


@pytest.mark.parametrize(
    "value", ["abc", "1.5", " 5", "5 ", "+5", "1_0", "0x10", "", "٣", "5\n", "9" * 400]
)
def test_env_integer_parse_errors_name_the_variable(isolated_env: Path, value: str) -> None:
    with pytest.raises(ConfigError) as info:
        load(env={"PR_WATCH_POLL__CONCURRENCY": value})
    assert "PR_WATCH_POLL__CONCURRENCY" in str(info.value)
    assert info.value.key == "poll.concurrency"
    assert info.value.source == "environment variable PR_WATCH_POLL__CONCURRENCY"


def test_env_out_of_range_names_the_variable_not_the_file(cfgdir: Path, write: Write) -> None:
    write(cfgdir / "config.yaml", "poll: {concurrency: 2}\n")
    with pytest.raises(ConfigError) as info:
        load(env={"PR_WATCH_POLL__CONCURRENCY": "65"})
    assert "PR_WATCH_POLL__CONCURRENCY" in str(info.value)
    assert "config.yaml" not in str(info.value)


def test_env_pattern_errors_name_the_variable_and_never_echo(isolated_env: Path) -> None:
    for name, value in (
        ("PR_WATCH_RETENTION__EVENTS", SECRET),
        ("PR_WATCH_RETENTION__EVENTS", "7d"),
        ("PR_WATCH_POLL__INTERVAL_HINT", "7m"),
        ("PR_WATCH_POLL__OVERLAP", SECRET),
    ):
        with pytest.raises(ConfigError) as info:
            load(env={name: value})
        assert name in str(info.value)
        assert SECRET not in str(info.value)


def test_env_me_name_must_not_be_empty(isolated_env: Path) -> None:
    with pytest.raises(ConfigError, match="PR_WATCH_ME__NAME"):
        load(env={"PR_WATCH_ME__NAME": ""})


def test_file_errors_are_reported_even_when_env_is_valid(cfgdir: Path, write: Write) -> None:
    write(cfgdir / "config.yaml", "poll: {concurrency: 0}\n")
    with pytest.raises(ConfigError, match="config.yaml"):
        load(env={"PR_WATCH_POLL__CONCURRENCY": "4"})


@pytest.mark.parametrize(
    "name",
    [
        "PR_WATCH_CRON__ENV_FILE",
        "PR_WATCH_HOSTS__GITHUB.COM__CONCURRENCY",
        "PR_WATCH_REPOS__X",
        "PR_WATCH_HOOKS__ENV_ALLOW",
        "PR_WATCH_HOOKS__ON-CI",
        "PR_WATCH_HOOKS__ON_CI",
    ],
)
def test_env_cannot_set_lists_cron_hosts_or_repos(isolated_env: Path, name: str) -> None:
    loaded = load(env={name: "FOO"})
    assert loaded.config == Config()
    assert [w.code for w in loaded.warnings] == ["config_warning"]
    assert name in loaded.warnings[0].message


@pytest.mark.parametrize(
    "name",
    ["PR_WATCH_NOPE", "PR_WATCH_POLL__NOPE", "PR_WATCH_", "PR_WATCH_POLL", "PR_WATCH_POLL__"],
)
def test_unknown_env_names_warn_and_are_ignored(isolated_env: Path, name: str) -> None:
    loaded = load(env={name: "1"})
    assert loaded.config == Config()
    assert len(loaded.warnings) == 1 and name in loaded.warnings[0].message


@pytest.mark.parametrize(
    "name", ["PR_WATCH_HOOK", "PR_WATCH_ID", "PR_WATCH_EVENT_ID", "PR_WATCH_OBJECT_TYPE"]
)
def test_hook_variables_are_ignored_silently(isolated_env: Path, name: str) -> None:
    loaded = load(env={name: "on-approved"})
    assert loaded.config == Config() and loaded.warnings == ()


def test_env_names_are_case_sensitive_and_foreign_names_ignored(isolated_env: Path) -> None:
    env = {"pr_watch_poll__concurrency": "8", "PRWATCH_POLL__CONCURRENCY": "8", "OTHER": "1"}
    loaded = load(env=env)
    assert loaded.config == Config() and loaded.warnings == ()


def test_load_reads_os_environ_by_default(
    isolated_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PR_WATCH_POLL__CONCURRENCY", "11")
    assert load_config().config.poll.concurrency == 11


# --- per-host and per-repo overrides -----------------------------------------------------------


def test_host_settings_builtin_defaults_by_backend(isolated_env: Path) -> None:
    c = load().config
    gh = c.host_settings("github.com", "github")
    gl = c.host_settings("gitlab.example.com", "gitlab")
    assert (gh.full_every, gh.concurrency) == (6, 4)
    assert (gl.full_every, gl.concurrency) == (12, 2)


def test_host_settings_precedence_host_then_env_then_file_then_builtin(
    cfgdir: Path, write: Write
) -> None:
    write(
        cfgdir / "config.yaml",
        "poll: {full_every: 20, concurrency: 5}\nhosts:\n  github.com: {concurrency: 2}\n",
    )
    env = {"PR_WATCH_POLL__CONCURRENCY": "7", "PR_WATCH_POLL__FULL_EVERY": "30"}
    c = load(env=env).config
    gh = c.host_settings("github.com", "github")
    assert gh.concurrency == 2  # hosts entry beats env
    assert gh.full_every == 30  # env beats file
    other = c.host_settings("gitlab.com", "gitlab")
    assert (other.full_every, other.concurrency) == (30, 7)  # not listed: global (env)
    assert (
        load().config.host_settings("gitlab.com", "gitlab").full_every == 20
    )  # file beats built-in


def test_host_settings_partial_override_falls_back_per_key(cfgdir: Path, write: Write) -> None:
    c = one_file(cfgdir, write, "hosts: {gitlab.com: {full_every: 3}}\n")
    s = c.host_settings("gitlab.com", "gitlab")
    assert (s.full_every, s.concurrency) == (3, 2)


def test_host_settings_rejects_unknown_backend(isolated_env: Path) -> None:
    with pytest.raises(ValueError):
        load().config.host_settings("h", "bitbucket")  # type: ignore[arg-type]


def test_repo_settings_defaults(isolated_env: Path) -> None:
    s = load().config.repo_settings("github.com/o/r")
    assert (s.detect_force_push, s.settle_polls, s.unreachable_after) == (False, 1, 5)


def test_repo_settings_precedence_repo_then_env_then_file_then_builtin(
    cfgdir: Path, write: Write
) -> None:
    write(
        cfgdir / "config.yaml",
        "detect_force_push: false\nci: {settle_polls: 4}\nfailures: {unreachable_after: 6}\n"
        "repos:\n  github.com/o/r: {detect_force_push: true, ci: {settle_polls: 0}}\n",
    )
    env = {
        "PR_WATCH_DETECT_FORCE_PUSH": "false",
        "PR_WATCH_CI__SETTLE_POLLS": "2",
        "PR_WATCH_FAILURES__UNREACHABLE_AFTER": "9",
    }
    c = load(env=env).config
    s = c.repo_settings("github.com/o/r")
    assert s.detect_force_push is True  # repo beats env
    assert s.settle_polls == 0  # repo beats env, zero is a real value
    assert s.unreachable_after == 9  # not overridden by the repo: env beats file
    other = c.repo_settings("github.com/o/other")
    assert (other.detect_force_push, other.settle_polls, other.unreachable_after) == (False, 2, 9)
    base = load().config.repo_settings("github.com/o/r")
    assert (base.settle_polls, base.unreachable_after) == (0, 6)


def test_repo_override_false_beats_global_true(cfgdir: Path, write: Write) -> None:
    c = one_file(
        cfgdir, write, "detect_force_push: true\nrepos: {h/o/r: {detect_force_push: false}}\n"
    )
    assert c.repo_settings("h/o/r").detect_force_push is False
    assert c.repo_settings("h/o/x").detect_force_push is True


def test_config_is_immutable(isolated_env: Path) -> None:
    c = load().config
    with pytest.raises(Exception):  # noqa: B017 (pydantic ValidationError)
        c.detect_force_push = True  # type: ignore[misc]


# --- ownership and modes -----------------------------------------------------------------------


@pytest.mark.parametrize("mode", [0o600, 0o644, 0o640 & ~0o020, 0o400, 0o444])
def test_files_not_writable_by_others_are_accepted(cfgdir: Path, write: Write, mode: int) -> None:
    write(cfgdir / "config.yaml", "poll: {concurrency: 2}\n", mode=mode)
    assert load().config.poll.concurrency == 2


@pytest.mark.parametrize("mode", [0o620, 0o660, 0o602, 0o606, 0o666, 0o622, 0o777])
def test_group_or_world_writable_main_file_is_refused(
    cfgdir: Path, write: Write, mode: int
) -> None:
    write(cfgdir / "config.yaml", "poll: {concurrency: 2}\n", mode=mode)
    with pytest.raises(ConfigError) as info:
        load()
    assert "config.yaml" in str(info.value) and "writable" in str(info.value)
    assert info.value.source is not None and info.value.key is None


def test_writable_explicit_config_is_refused(tmp_path: Path, write: Write) -> None:
    path = write(tmp_path / "c.yaml", "", mode=0o664)
    with pytest.raises(ConfigError, match="writable"):
        load(path)


def test_writable_dropin_is_refused(dropins: Path, write: Write) -> None:
    write(dropins / "10.yaml", "", mode=0o666)
    with pytest.raises(ConfigError, match=r"10\.yaml"):
        load()


@pytest.mark.parametrize("mode", [0o770, 0o775, 0o757, 0o777, 0o720])
def test_group_or_world_writable_config_d_is_refused(
    cfgdir: Path, dropins: Path, write: Write, mode: int
) -> None:
    write(dropins / "10.yaml", "poll: {concurrency: 2}\n")
    dropins.chmod(mode)
    with pytest.raises(ConfigError, match=r"config\.d"):
        load()


@pytest.mark.parametrize("mode", [0o700, 0o755, 0o500])
def test_safe_config_d_modes_are_accepted(dropins: Path, write: Write, mode: int) -> None:
    write(dropins / "10.yaml", "poll: {concurrency: 2}\n")
    dropins.chmod(mode)
    try:
        assert load().config.poll.concurrency == 2
    finally:
        dropins.chmod(0o700)


def test_config_file_owned_by_another_user_is_refused(
    cfgdir: Path, write: Write, monkeypatch: pytest.MonkeyPatch
) -> None:
    write(cfgdir / "config.yaml", "")
    monkeypatch.setattr(config_module, "_euid", lambda: os.geteuid() + 1)
    with pytest.raises(ConfigError, match="owned by another user"):
        load()


def test_dropin_and_config_d_owned_by_another_user_are_refused(
    dropins: Path, write: Write, monkeypatch: pytest.MonkeyPatch
) -> None:
    write(dropins / "10.yaml", "")
    monkeypatch.setattr(config_module, "_euid", lambda: os.geteuid() + 1)
    with pytest.raises(ConfigError, match="owned by another user"):
        load()


def test_explicit_file_owned_by_another_user_is_refused(
    tmp_path: Path, write: Write, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = write(tmp_path / "c.yaml", "")
    monkeypatch.setattr(config_module, "_euid", lambda: os.geteuid() + 1)
    with pytest.raises(ConfigError, match="owned by another user"):
        load(path)


def test_permission_refusal_happens_before_parsing(cfgdir: Path, write: Write) -> None:
    write(cfgdir / "config.yaml", "{{{ not yaml", mode=0o666)
    with pytest.raises(ConfigError, match="writable"):
        load()


def test_symlinked_config_file_is_checked_at_its_target(
    cfgdir: Path, tmp_path: Path, write: Write
) -> None:
    good = write(tmp_path / "dots" / "good.yaml", "poll: {concurrency: 2}\n")
    link = cfgdir / "config.yaml"
    link.symlink_to(good)
    assert load().config.poll.concurrency == 2
    good.chmod(0o666)
    with pytest.raises(ConfigError, match="writable"):
        load()


def test_symlinked_config_d_is_checked_at_its_target(
    cfgdir: Path, tmp_path: Path, write: Write
) -> None:
    target = tmp_path / "dots" / "drops"
    write(target / "10.yaml", "poll: {concurrency: 4}\n")
    target.chmod(0o700)
    (cfgdir / "config.d").symlink_to(target)
    assert load().config.poll.concurrency == 4
    target.chmod(0o775)
    try:
        with pytest.raises(ConfigError, match="writable"):
            load()
    finally:
        target.chmod(0o700)


def test_symlinked_dropin_is_checked_at_its_target(
    dropins: Path, tmp_path: Path, write: Write
) -> None:
    target = write(tmp_path / "t.yaml", "poll: {concurrency: 4}\n", mode=0o666)
    (dropins / "10.yaml").symlink_to(target)
    with pytest.raises(ConfigError, match="writable"):
        load()


def test_total_size_of_all_files_is_capped(dropins: Path, write: Write) -> None:
    body = "# " + "x" * (200 * 1024) + "\n"
    for i in range(6):
        write(dropins / f"{i}.yaml", body)
    with pytest.raises(ConfigError, match="together"):
        load()


# --- example config and error type -------------------------------------------------------------


def test_config_example_loads_and_equals_defaults(tmp_path: Path, write: Write) -> None:
    text = (ROOT / "config.example.yaml").read_text(encoding="utf-8")
    path = write(tmp_path / "example.yaml", text)
    loaded = load(path)
    assert loaded.warnings == ()
    assert loaded.config == Config()


def test_config_error_is_typed_for_the_cli_frame() -> None:
    err = ConfigError(source="f.yaml", key="poll.concurrency", message="must be at most 64")
    assert err.code == "config_invalid"
    assert str(err) == "f.yaml: poll.concurrency: must be at most 64"
    assert isinstance(err, Exception)
    assert stat.S_ISREG(os.stat(ROOT / "pyproject.toml").st_mode)
