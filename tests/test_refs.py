"""refs: every user value is validated before argv; references parse offline."""

from __future__ import annotations

import re
from typing import Any

import pytest

from pr_watch import refs
from pr_watch.refs import (
    Label,
    PrId,
    PrRef,
    RefError,
    clean_me_name,
    match_stored,
    parse_cursor,
    parse_duration,
    parse_every,
    parse_hook,
    parse_host,
    parse_label,
    parse_limit,
    parse_owner,
    parse_pr,
    parse_reference,
    parse_repo,
    parse_type,
    parse_watch_id,
    resolve_owner,
    watch_key,
)
from tests.harness import load_schema, schema_errors

# Values that must never reach argv, whatever the field.
HOSTILE = [
    "",
    " ",
    "-x",
    "--config=/x",
    "--",
    "-",
    "a b",
    " github.com/o/r#1",
    "github.com/o/r#1 ",
    "github.com/o/r#1\n",
    "github.com/o/r#1\r\n--config=/x",
    "github.com/o/r\x00#1",
    "github.com/o/r\t#1",
    "github.com/o/\x1b[31mr#1",
    "github.com/o/r‮#1",
    "github.com/o/r​#1",
    "github.com/o/r #1",
    "github.cоm/o/r#1",  # Cyrillic o
    "ｇithub.com/o/r#1",  # fullwidth g
    "github.com/ö/r#1",
    "github.com/o/r#1" + "9" * 5000,
    "a" * 5000,
]


def _err(call: Any, *args: Any) -> RefError:
    with pytest.raises(RefError) as info:
        call(*args)
    return info.value


def _never_echoes(exc: RefError, value: str) -> None:
    text = f"{exc.code} {exc} {exc.kind}"
    assert len(value.strip()) < 4 or value not in text
    for chunk in re.findall(r"[^\s]{3,}", value):
        assert chunk not in text


# --- PR references --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "canonical"),
    [
        ("github.com/o/r#1", "github.com/o/r#1"),
        ("GitHub.COM/Owner/Repo#42", "github.com/Owner/Repo#42"),
        (
            "gitlab.example.test/g/sub/deep/p#1234567890",
            "gitlab.example.test/g/sub/deep/p#1234567890",
        ),
        ("host.test:8443/o/r#5", "host.test:8443/o/r#5"),
        ("host.test:443/o/r#5", "host.test/o/r#5"),
        ("www.github.com/o/r#5", "github.com/o/r#5"),
        ("o/r#1", "o/r#1"),
        ("https://github.com/o/r/pull/7", "github.com/o/r#7"),
        ("https://GitHub.com/O/R/pull/7/", "github.com/O/R#7"),
        ("https://github.com/o/r/pull/7/files", "github.com/o/r#7"),
        ("https://github.com/o/r/pull/7/commits/abc1234", "github.com/o/r#7"),
        ("https://github.com/o/r/pull/7.diff", "github.com/o/r#7"),
        ("https://github.com/o/r/pull/7?diff=split&w=1#discussion-r1.2", "github.com/o/r#7"),
        ("https://github.com:443/o/r/pull/7", "github.com/o/r#7"),
        ("https://gitlab.com/g/s/p/-/merge_requests/9", "gitlab.com/g/s/p#9"),
        ("https://gitlab.com/g/p/merge_requests/9/diffs", "gitlab.com/g/p#9"),
        ("https://gitlab.com/g/p/-/merge_requests/9/pipelines/", "gitlab.com/g/p#9"),
        ("https://GitLab.com/g/p/-/merge_requests/9", "gitlab.com/g/p#9"),
        ("github.com/o/-r#1", "github.com/o/-r#1"),
        ("https://github.com/o/-r/pull/1", "github.com/o/-r#1"),
        ("https://www.github.com/o/r/pull/1", "github.com/o/r#1"),
        ("https://h.test/o/r/pull/1", "h.test/o/r#1"),
        ("https://h.test/g/p/-/merge_requests/1", "h.test/g/p#1"),
        ("h.test/a/-c#1", "h.test/a/-c#1"),
    ],
)
def test_parse_pr_accepts_and_canonicalizes(text: str, canonical: str) -> None:
    ref = parse_pr(text)
    assert isinstance(ref, PrRef)
    assert ref.canonical == canonical
    assert not schema_errors("defs", ref.canonical, "pr")  # the canonical form is a `pr`


@pytest.mark.parametrize(
    "text",
    [
        *HOSTILE,
        "github.com/o/r#0",
        "github.com/o/r#01",
        "github.com/o/r#12345678901",
        "github.com/o/r#7abc",
        "github.com/o/r#-1",
        "github.com/o/r#+1",
        "github.com/o/r#１",  # fullwidth digit
        "github.com/o/r!7",
        "github.com/o/r",
        "github.com/o/r#",
        "github.com/o/r/#1",
        "github.com/o/r/#1/",
        "github.com//r#1",
        "github.com/o//r#1",
        "github.com/o/./r#1",
        "github.com/o/../r#1",
        "github.com/../r#1",
        "github.com/o/r/..#1",
        "github.com/-o/r#1",
        "gitlab.com/g/-s#1",
        "h.test/a/b/-c#1",
        "HTTPS://github.com/o/r/pull/1",
        "HTTPS://gitlab.com/g/p/-/merge_requests/1",
        "https://github.com/g/p/-/merge_requests/1",
        "https://gitlab.com/o/r/pull/1",
        "https://www.gitlab.com/o/r/pull/1",
        "github.com/o/r.git#1",
        "github.com/o/r%2e#1",
        "github.com/o/r@x#1",
        "github.com/o\\r#1",
        "github.com:0/o/r#1",
        "github.com:65536/o/r#1",
        "github.com:0443/o/r#1",
        "github.com:/o/r#1",
        "user@github.com/o/r#1",
        "user:pw@github.com/o/r#1",
        "github.com./o/r#1",
        ".github.com/o/r#1",
        "-github.com/o/r#1",
        "github-.com/o/r#1",
        "github..com/o/r#1",
        "git hub.com/o/r#1",
        "http://github.com/o/r/pull/7",
        "ssh://github.com/o/r/pull/7",
        "git@github.com:o/r.git",
        "//github.com/o/r/pull/7",
        "https://",
        "https:///o/r/pull/7",
        "https://github.com",
        "https://github.com/o/r",
        "https://github.com/o/r/pull/",
        "https://github.com/o/r/pull/0",
        "https://github.com/o/r/pull/7abc",
        "https://github.com/o/r/pull/7/files/extra",
        "https://github.com/o/r/pull/7/../../x",
        "https://github.com/o/r/pull/7/%2e%2e",
        "https://github.com/o/r/pull/7%2f",
        "https://github.com/o/r/pull/7 ",
        "https://github.com/o/r/pull/7\n",
        "https://github.com/o/r/pull/7\t",
        "https://github.com/o/r/pull/7?q=a b",
        "https://github.com/o/r/pull/7?q=%41",
        "https://github.com/o/r/pull/7?q=a@b",
        "https://github.com/o/r/pull/7?q=a\\b",
        "https://github.com/o/r/pull/7?q=a;b",
        "https://github.com/o/r/pull/7#a/b",
        "https://github.com/o/r/pull/7#a#b",
        "https://github.com/o/r/pull/7?a?b",
        "https://github.com/o/r/pull/7#?",
        "https://user@github.com/o/r/pull/7",
        "https://user:pw@github.com/o/r/pull/7",
        "https://github.com@evil.test/o/r/pull/7",
        "https://github.com\\@evil.test/o/r/pull/7",
        "https://github.com:/o/r/pull/7",
        "https://github.com:0/o/r/pull/7",
        "https://github.com:99999/o/r/pull/7",
        "https://github.com/o/pull/1",  # one segment, no repo
        "https://github.com/a/b/c/pull/1",  # github takes exactly two segments
        "https://gitlab.com/-/merge_requests/9",
        "https://gitlab.com/g/-/-/merge_requests/9",
        "https://gitlab.com/g/merge_requests/p/-/merge_requests/9",
        "https://gitlab.com/-g/p/-/merge_requests/9",
        "https://gitlab.com/g/-p/-/merge_requests/9",
        "https://gitlab.com/g/p/-/merge_requests/9/diffs/extra",
        "https://gitlab.com/g/p/-/merge_requests/!9",
        "https://github.com/o/r/pull/7/commits/--config=/x",
        "https://github.com/o/r/issues/7",
        "https://github.com/o/r/pull/7.diff.patch",
        "https://github.com/ö/r/pull/7",
    ],
)
def test_parse_pr_refuses(text: str) -> None:
    exc = _err(parse_pr, text)
    assert exc.code == "invalid_pr"
    _never_echoes(exc, text)


def test_parse_pr_refuses_non_strings_and_pr_ids() -> None:
    assert _err(parse_pr, "gh:pr:abc123").code == "invalid_pr"  # a pr_id is not a PR reference


@pytest.mark.parametrize("path", ["o/r", "g/s/p", "a.b/c_d-e"])
def test_parse_pr_keeps_path_case_and_lowercases_host_only(path: str) -> None:
    ref = parse_pr(f"HOST.Test/{path.upper()}#3")
    assert ref.host == "host.test"
    assert ref.path == path.upper()


def test_parse_pr_error_hint_for_unrecognized_url() -> None:
    exc = _err(parse_pr, "https://github.com/o/r/issues/7")
    assert "host/path#N" in str(exc)


def test_pr_number_and_repo_parts() -> None:
    ref = parse_pr("github.com/o/r#12")
    assert (ref.host, ref.path, ref.number) == ("github.com", "o/r", 12)
    assert ref.repo == "github.com/o/r"
    assert ref.key == "github.com/o/r#12"
    assert parse_pr("github.com/O/R#12").key == "github.com/o/r#12"


# --- references that may also be a pr_id ----------------------------------------------------


def test_parse_reference_pr_url_or_pr_id() -> None:
    assert parse_reference("github.com/o/r#1") == parse_pr("github.com/o/r#1")
    assert parse_reference("https://github.com/o/r/pull/1") == parse_pr("github.com/o/r#1")
    assert parse_reference("gh:PR_kwDOAbc123") == PrId("gh:PR_kwDOAbc123")


@pytest.mark.parametrize(
    "text",
    [
        *HOSTILE,
        "https://github.com/o/r/issues/1",  # URL-shaped: never falls back to a pr_id
        "HTTPS://github.com/o/r/pull/1",
        "https://github.com/o/r",
        "github.com/o/r#0",  # `#` present: a PR form, never a pr_id
        "..",
        "a..b",
        "-abc",
        "a" * 129,
        "ab c",
        "ab\x00c",
        "ab\nc",
        "abc‮",
    ],
)
def test_parse_reference_refuses(text: str) -> None:
    exc = _err(parse_reference, text)
    assert exc.code == "invalid_pr"
    _never_echoes(exc, text)


def test_pr_id_matches_the_schema_grammar() -> None:
    for value in ["a", "gh:PR_x/y=z+1.2-3", "A" * 128]:
        assert not schema_errors("defs", value, "pr_id")
        assert parse_reference(value) == PrId(value)


# --- offline matching ------------------------------------------------------------------------

ROWS = [
    ("github.com/Owner/Repo#7", "id-1"),
    ("github.com/other/repo#7", "id-2"),
    ("gitlab.com/g/p#7", "id-3"),
]


def test_match_is_case_insensitive_in_host_and_path() -> None:
    assert match_stored(parse_pr("GITHUB.com/owner/REPO#7"), ROWS) == "id-1"
    assert match_stored(parse_pr("https://github.com/OWNER/repo/pull/7"), ROWS) == "id-1"


def test_match_pr_id_is_exact() -> None:
    assert match_stored(PrId("id-2"), ROWS) == "id-2"
    assert match_stored(PrId("ID-2"), ROWS) is None


def test_match_none_when_absent() -> None:
    assert match_stored(parse_pr("github.com/o/r#8"), ROWS) is None
    assert match_stored(parse_pr("github.com/o/r#7"), []) is None


def test_match_more_than_one_row_is_usage_with_pr_id_hint() -> None:
    rows = [("github.com/o/r#7", "id-a"), ("github.com/O/R#7", "id-b")]
    exc = _err(match_stored, parse_pr("github.com/o/r#7"), rows)
    assert exc.code == "usage"
    assert exc.kind == "ambiguous_reference"
    assert "pr_id" in str(exc)
    assert parse_pr("github.com/o/r#7").repo not in str(exc)


def test_match_same_row_twice_is_not_ambiguous() -> None:
    rows = [("github.com/o/r#7", "id-a"), ("github.com/o/r#7", "id-a")]
    assert match_stored(parse_pr("github.com/o/r#7"), rows) == "id-a"


def test_match_does_not_match_a_longer_number_or_path() -> None:
    rows = [("github.com/o/r#77", "id-a"), ("github.com/o/r2#7", "id-b")]
    assert match_stored(parse_pr("github.com/o/r#7"), rows) is None


# --- host and repo ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "host"),
    [
        ("github.com", "github.com"),
        ("GitHub.COM", "github.com"),
        ("h.test:8443", "h.test:8443"),
        ("h.test:443", "h.test"),
        ("localhost", "localhost"),
        ("a" * 63 + ".test", "a" * 63 + ".test"),
    ],
)
def test_parse_host(text: str, host: str) -> None:
    assert parse_host(text) == host
    assert not schema_errors("defs", host, "host")


@pytest.mark.parametrize(
    "text",
    [
        *HOSTILE,
        "-x",
        "--host=x",
        "h.test/",
        "h.test/o",
        "h.test#1",
        "h.test:0",
        "h.test:65536",
        "h.test:08443",
        "h.test:",
        "h.test:80:80",
        "h..test",
        "h.test.",
        "-h.test",
        "h-.test",
        "user@h.test",
        "h_t.test",
        "a" * 64 + ".test",
        ".".join(["a" * 60] * 5),
        "h.tеst",  # Cyrillic e
        "h.test\n",
        "https://h.test",
    ],
)
def test_parse_host_refuses(text: str) -> None:
    exc = _err(parse_host, text)
    assert (exc.code, exc.kind) == ("usage", "bad_host")
    _never_echoes(exc, text)


@pytest.mark.parametrize(
    ("text", "repo"),
    [
        ("github.com/o/r", "github.com/o/r"),
        ("GitHub.com/Owner/Repo", "github.com/Owner/Repo"),
        ("h.test:8443/g/s/p", "h.test:8443/g/s/p"),
        ("h.test:443/g/p", "h.test/g/p"),
        ("h.test/a.b/c_d-e", "h.test/a.b/c_d-e"),
        ("h.test/.hidden/x", "h.test/.hidden/x"),
        ("github.com/o/-r", "github.com/o/-r"),
    ],
)
def test_parse_repo(text: str, repo: str) -> None:
    assert parse_repo(text) == repo
    assert not schema_errors("defs", repo, "repo_id")


@pytest.mark.parametrize(
    "text",
    [
        *HOSTILE,
        "github.com",
        "github.com/",
        "github.com//r",
        "github.com/o/",
        "github.com/o//r",
        "github.com/./r",
        "github.com/o/..",
        "github.com/../r",
        "github.com/o/../r",
        "github.com/o/r/../..",
        "github.com/-o/r",
        "gitlab.com/g/-s",
        "h.test/a/b/-c",
        "github.com/o/--config=/x",
        "github.com/o/r.git",
        "github.com/o/r#1",
        "github.com/o/r?x=1",
        "github.com/o/r%2e%2e",
        "github.com/o/r@x",
        "github.com/o\\r",
        "github.com/o/r/\x00",
        "github.com/o/r/\n",
        "user@github.com/o/r",
        "https://github.com/o/r",
        "github.com/ö/r",
        "github.com/" + "a/" * 200 + "r",
    ],
)
def test_parse_repo_refuses(text: str) -> None:
    exc = _err(parse_repo, text)
    assert (exc.code, exc.kind) == ("usage", "bad_repo")
    _never_echoes(exc, text)


def test_host_and_repo_patterns_have_one_owner() -> None:
    from pr_watch import config

    assert config._HOST == refs.HOST
    assert config._REPO == refs.REPO


# --- owner -----------------------------------------------------------------------------------


@pytest.mark.parametrize("text", ["review-respond", "human:jake", "a", "a.b_c@d:e-f", "x" * 64])
def test_parse_owner(text: str) -> None:
    assert parse_owner(text) == text
    assert not schema_errors("defs", text, "owner")


@pytest.mark.parametrize(
    "text",
    [*HOSTILE, "-a", ".a", "_a", "Upper", "a b", "a/b", "a=b", "x" * 65, "ä", "a\n", "a,b"],
)
def test_parse_owner_refuses(text: str) -> None:
    exc = _err(parse_owner, text)
    assert (exc.code, exc.kind) == ("usage", "bad_owner")
    _never_echoes(exc, text)


@pytest.mark.parametrize(
    ("raw", "cleaned"),
    [
        ("Jake", "jake"),
        ("jake.kausler", "jake.kausler"),
        ("Jake Kausler", "jake-kausler"),
        ("  jake", "jake"),
        ("-.jake", "jake"),
        ("__a", "a"),
        ("jäke", "j-ke"),
        ("İstanbul", "-stanbul".lstrip("-")),
        ("a/b\\c\n", "a-b-c-"),
        ("\x00\x1b[31mjake", "31m" + "jake"),
        ("x" * 200, "x" * 58),
    ],
)
def test_clean_me_name(raw: str, cleaned: str) -> None:
    assert clean_me_name(raw) == cleaned


def test_clean_me_name_empty_results() -> None:
    assert clean_me_name("") == ""
    assert clean_me_name("---") == ""
    assert clean_me_name("日本語") == ""


def test_clean_me_name_makes_a_valid_owner_after_human_prefix() -> None:
    for raw in ["Jake", "x" * 500, "A.b:c@d-e", "-_-a", "ünï"]:
        cleaned = clean_me_name(raw)
        if cleaned:
            assert not schema_errors("defs", f"human:{cleaned}", "owner")


def test_resolve_owner_explicit_wins_and_is_validated() -> None:
    assert resolve_owner("review-respond", interactive=False, me_name=None) == "review-respond"
    exc = _err(lambda: resolve_owner("Bad Owner", interactive=True, me_name="jake"))
    assert exc.kind == "bad_owner"


def test_resolve_owner_on_a_tty_is_human_me_name() -> None:
    assert resolve_owner(None, interactive=True, me_name="Jake K") == "human:jake-k"


def test_resolve_owner_falls_back_to_the_os_user() -> None:
    assert (
        resolve_owner(None, interactive=True, me_name=None, os_user=lambda: "Alice")
        == "human:alice"
    )


def test_resolve_owner_without_tty_is_owner_required() -> None:
    exc = _err(lambda: resolve_owner(None, interactive=False, me_name="jake"))
    assert (exc.code, exc.kind) == ("usage", "owner_required")
    assert str(exc) == "--owner is required when not run from a terminal"


@pytest.mark.parametrize("me", ["---", "日本語", ""])
def test_resolve_owner_empty_cleaned_name_asks_for_owner(me: str) -> None:
    exc = _err(lambda: resolve_owner(None, interactive=True, me_name=me))
    assert (exc.code, exc.kind) == ("usage", "owner_required")
    assert "--owner" in str(exc)
    assert me not in str(exc) or me == ""


def test_resolve_owner_no_os_user_asks_for_owner() -> None:
    exc = _err(lambda: resolve_owner(None, interactive=True, me_name=None, os_user=lambda: None))
    assert exc.kind == "owner_required"


# --- labels ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "label"),
    [
        ("k=v", Label("k", "v")),
        ("team=a/b#1", Label("team", "a/b#1")),
        ("k_1-x=A.b:c@d+e-f", Label("k_1-x", "A.b:c@d+e-f")),
        ("k=" + "v" * 128, Label("k", "v" * 128)),
    ],
)
def test_parse_label(text: str, label: Label) -> None:
    assert parse_label(text) == label
    assert not schema_errors("defs", text, "label")


@pytest.mark.parametrize(
    "text",
    [
        *HOSTILE,
        "k",
        "k=",
        "=v",
        "K=v",
        "1k=v",
        "k=v=w",
        "k==v",
        "k=v w",
        "k=v\n",
        "k=v\x00",
        "k=ü",
        "k=v;rm",
        "k=$(x)",
        "k=" + "v" * 129,
        "k" * 33 + "=v",
        "--config=/x",
        "-k=v",
        "k =v",
    ],
)
def test_parse_label_refuses(text: str) -> None:
    exc = _err(parse_label, text)
    assert (exc.code, exc.kind) == ("usage", "bad_label")
    _never_echoes(exc, text)


# --- cursor, duration, limit, type, hook, watch id --------------------------------------------


def test_parse_cursor() -> None:
    assert parse_cursor("v1.abcdefg2.0") == "v1.abcdefg2.0"
    assert parse_cursor("v1.abcdefgh.123456789012345678")
    assert not schema_errors("defs", "v1.abcdefg2.5", "cursor")


@pytest.mark.parametrize(
    "text",
    [
        *HOSTILE,
        "v1.abcdefg2",
        "v2.abcdefg2.1",
        "v1.ABCDEFG2.1",
        "v1.abcdefg1.1",  # 1 is not base32
        "v1.abcdefg.1",
        "v1.abcdefg2.-1",
        "v1.abcdefg2.1234567890123456789",
        "v1.abcdefg2.1\n",
        "v1.abcdefg2.１",
        "v1.abcdefg2.",
    ],
)
def test_parse_cursor_refuses(text: str) -> None:
    exc = _err(parse_cursor, text)
    assert (exc.code, exc.kind) == ("usage", "bad_cursor")
    _never_echoes(exc, text)


@pytest.mark.parametrize("text", ["0", "1ms", "30s", "5m", "12h", "1d", "3650d"])
def test_parse_duration(text: str) -> None:
    assert parse_duration(text) == text


@pytest.mark.parametrize(
    "text",
    [
        *HOSTILE,
        "1",
        "5",
        "-5m",
        "5 m",
        "5M",
        "1.5h",
        "0s ",
        "00",
        "3651d",
        "5w",
        "m",
        "１d",
        "1d\n",
    ],
)
def test_parse_duration_refuses(text: str) -> None:
    exc = _err(parse_duration, text)
    assert (exc.code, exc.kind) == ("usage", "bad_duration")
    _never_echoes(exc, text)


@pytest.mark.parametrize("text", ["5m", "15m", "1h", "6h", "60m"])
def test_parse_every(text: str) -> None:
    assert parse_every(text) == text


@pytest.mark.parametrize("text", [*HOSTILE, "7m", "5s", "0", "90m", "5h", "1d", "-5m", "0m", "5"])
def test_parse_every_refuses(text: str) -> None:
    exc = _err(parse_every, text)
    assert (exc.code, exc.kind) == ("usage", "bad_every")
    _never_echoes(exc, text)


@pytest.mark.parametrize(("text", "n"), [("1", 1), ("100", 100), ("1000", 1000)])
def test_parse_limit(text: str, n: int) -> None:
    assert parse_limit(text) == n


@pytest.mark.parametrize(
    "text", [*HOSTILE, "0", "1001", "-1", "+5", "01", "1.0", "1e3", "1_0", "０", "٣", "5\n", " 5"]
)
def test_parse_limit_refuses(text: str) -> None:
    exc = _err(parse_limit, text)
    assert (exc.code, exc.kind) == ("usage", "bad_limit")
    _never_echoes(exc, text)


def test_event_types_equal_the_schema() -> None:
    assert list(refs.EVENT_TYPES) == load_schema("defs")["$defs"]["event_type"]["enum"]


def test_parse_type() -> None:
    for t in refs.EVENT_TYPES:
        assert parse_type(t) == t
    for bad in [*HOSTILE, "Pushed", "push", "pushed\n", "post-poll", "*"]:
        exc = _err(parse_type, bad)
        assert (exc.code, exc.kind) == ("usage", "bad_type")


def test_parse_hook() -> None:
    assert parse_hook("on-pushed=echo hi") == ("on-pushed", "echo hi")
    assert parse_hook("post-poll=a=b") == ("post-poll", "a=b")
    assert parse_hook("on-ci=  x\ty\nz") == ("on-ci", "  x\ty\nz")
    assert len(refs.HOOK_NAMES) == 16
    assert "post-poll" in refs.HOOK_NAMES


@pytest.mark.parametrize(
    "text",
    [
        "",
        "echo",
        "=echo",
        "on-pushed=",
        "on-pushed=   ",
        "on-pushed=\n",
        "on-bogus=echo",
        "ON-PUSHED=echo",
        "on-pushed =echo",
        "-on-pushed=echo",
        "--hook=x",
        "on-pushed=echo\x00",
        "on-pushed=echo\x1b[0m",
        "on-pushed=echo\r",
        "on-pushed=echo‮",
        "on-pushed=" + "x" * 4097,
        "on-pushed=\u00a0",
        "on-pushed=\u3000",
        "on-pushed=\u200b",
        "on-pushed=\u2028",
        "on-pushed=\u2800",
        "on-pushed=\u115f\u1160\u3164\uffa0 ",
        "on-pushed=echo\u200b",
    ],
)
def test_parse_hook_refuses(text: str) -> None:
    exc = _err(parse_hook, text)
    assert (exc.code, exc.kind) == ("usage", "bad_hook")
    _never_echoes(exc, text)


def test_parse_watch_id() -> None:
    assert parse_watch_id("w-abcdefg234") == "w-abcdefg234"
    for bad in [
        *HOSTILE,
        "w-abcdefg23",
        "w-ABCDEFG234",
        "w-abcdefg231",
        "x-abcdefg234",
        "w-abcdefg234\n",
    ]:
        exc = _err(parse_watch_id, bad)
        assert (exc.code, exc.kind) == ("usage", "bad_watch_id")


# --- watch keys ------------------------------------------------------------------------------


def test_watch_key_is_canonical_sorted_and_deduplicated() -> None:
    assert watch_key([], []) == "mine+open"
    assert watch_key(["b.test", "a.test", "b.test"], []) == "mine+open;host=a.test,b.test"
    assert (
        watch_key(["a.test"], ["a.test/z/y", "a.test/a/b", "a.test/z/y"])
        == "mine+open;host=a.test;repo=a.test/a/b,a.test/z/y"
    )
    assert watch_key([], ["a.test/a/b"]) == "mine+open;repo=a.test/a/b"


@pytest.mark.parametrize(
    "key",
    [
        "mine+open",
        "mine+open;host=a.test,b.test:8443",
        "mine+open;host=a.test;repo=a.test/a/b,a.test/c/d",
        "mine+open;repo=a.test/a/b",
    ],
)
def test_watch_key_grammar_matches_the_schema(key: str) -> None:
    assert not schema_errors("defs", key, "watch_key")
    assert refs.WATCH_KEY.fullmatch(key)


def test_watch_key_is_ordered_by_bytes_not_locale() -> None:
    assert watch_key(["b.test", "B.test"], []) == "mine+open;host=b.test"
    assert watch_key([], ["h.test/B/x", "h.test/a/x"]) == "mine+open;repo=h.test/a/x,h.test/b/x"


def test_validators_do_not_hang_on_adversarial_input() -> None:
    nasty = "github.com/" + "a/" * 300 + "#1"
    assert _err(parse_pr, nasty).code == "invalid_pr"
    nasty_url = "https://github.com/" + "a-" * 400 + "/pull/1"
    assert _err(parse_pr, nasty_url).code == "invalid_pr"
    assert _err(parse_pr, "https://gitlab.com/" + "a/" * 400 + "-/merge_requests/x").code


# --- review round 1 ---------------------------------------------------------------------------


def test_r1_url_forms_with_a_leading_dash_repo_name() -> None:
    assert parse_pr("github.com/o/-r#1").canonical == "github.com/o/-r#1"
    assert parse_pr("https://github.com/o/-r/pull/1").canonical == "github.com/o/-r#1"
    assert parse_pr("h.test/a/-c#1").number == 1


@pytest.mark.parametrize(
    "text", ["https://github.com/-o/r/pull/1", "https://gitlab.com/g/-s/-/merge_requests/1"]
)
def test_r1_url_forms_refused(text: str) -> None:
    assert _err(parse_pr, text).code == "invalid_pr"


def test_r7_repo_id_is_capped_as_a_whole() -> None:
    host = "h.test/"
    path = "a" * (255 - len(host))
    assert parse_repo(host + path) == host + path
    assert _err(parse_repo, host + path + "a").kind == "bad_repo"
    assert parse_pr(f"{host}{path}#1").repo == host + path
    assert _err(parse_pr, f"{host}{path}a#1").code == "invalid_pr"
    assert parse_pr(f"https://{host}{path}/-/merge_requests/1").repo == host + path
    assert _err(parse_pr, f"https://{host}{path}a/-/merge_requests/1").code == "invalid_pr"


def test_r8_watch_key_folds_the_case_of_repos() -> None:
    assert watch_key([], ["h.test/O/R", "h.test/o/r"]) == "mine+open;repo=h.test/o/r"
    assert watch_key([], ["h.test/B/x", "h.test/a/x"]) == "mine+open;repo=h.test/a/x,h.test/b/x"


def test_r9_watch_key_longer_than_4096_is_usage() -> None:
    repos = [f"h.test/o/{'r' * 100}{i:03d}" for i in range(50)]
    exc = _err(watch_key, [], repos)
    assert (exc.code, exc.kind) == ("usage", "filters_too_long")
    assert len(watch_key([], repos[:30])) <= 4096


def test_r14_hook_names_are_shared_with_config() -> None:
    from pr_watch import config

    assert set(refs.HOOK_NAMES) == set(config.HOOK_NAMES)
    assert len(refs.HOOK_NAMES) == len(set(refs.HOOK_NAMES))


@pytest.mark.parametrize("cmd", ["\u00a0", "\u3000", "\u200b", "\u2028", "\u2800", "", " \n"])
def test_r2_check_hook_cmd_refuses(cmd: str) -> None:
    assert not refs.check_hook_cmd(cmd)


def test_r2_check_hook_cmd_accepts() -> None:
    assert refs.check_hook_cmd("echo hi")
    assert refs.check_hook_cmd("a\n\tb")
    assert refs.check_hook_cmd("\u00a0echo")
    assert refs.check_hook_cmd("echo \u00e9")
