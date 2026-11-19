"""The pydantic model and schemas/config.json must accept and refuse the same documents.

A differential test: every leaf of the schema is probed with a pool of boundary and hostile
values, every object level with an extra key, and every map with a pool of keys; the schema's
verdict (jsonschema) must equal the model's. Known, intended differences are listed in
`EXCEPTIONS` and each has its own test in test_config.py.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Any

import pytest
from pydantic import ValidationError

from pr_watch.config import ENV_KEYS, Config
from tests.harness import ROOT, load_schema, validator

SCHEMA = load_schema("config")
CHECK = validator("config")

VALUES: list[Any] = [
    None, True, False, 0, 1, -1, 5, 10, 11, 30, 32, 33, 63, 64, 65, 100, 600, 601, 1000, 1001,
    10000, 10001, 100000, 100001, 1.5, "", "x", "5m", "7m", "1h", "5h", "30d", "29d", "100d",
    "30h", "60s", "500ms", "0", "7d", "PR_WATCH_X", "LD_X", "DYLD_X", "FOO", "1X", "A=B",
    "x" * 4096, "x" * 4097, "x" * 128, "x" * 129, [], {}, ["a"], {"a": 1},
]  # fmt: skip
HOST_KEYS = ["github.com", "gitlab.example.com:8443", "a", "GitHub.com", "bad host", "a..b",
             "-x", "x-", "h:123456", "", "a/b", "h:99999"]  # fmt: skip
REPO_KEYS = ["github.com/o/r", "h:80/a/b/c", "github.com", "github.com/o/../r", "github.com/./r",
             "github.com//r", "github.com/o r", "GitHub.com/o/r", "github.com/o/", "/o/r",
             "h/a.b_c-d"]  # fmt: skip
SAMPLE_HOST = "github.com"
SAMPLE_REPO = "github.com/o/r"


def model_accepts(document: dict[str, Any]) -> bool:
    try:
        Config.model_validate(document)
    except ValidationError:
        return False
    return True


def nest(path: tuple[Any, ...], value: Any) -> dict[str, Any]:
    node: Any = value
    for part in reversed(path):
        node = [node] if part == 0 else {part: node}
    if path[-2:-1] == (0,) and path[-1] != "cmd":  # a hook entry needs its cmd
        parent: Any = node
        for part in path[:-1]:
            parent = parent[0] if part == 0 else parent[part]
        parent["cmd"] = "x"
    return node


def leaves(schema: dict[str, Any], path: tuple[Any, ...] = ()) -> Iterator[tuple[Any, ...]]:
    if "properties" in schema:
        for key, sub in schema["properties"].items():
            yield from leaves(sub, (*path, key))
    elif "patternProperties" in schema:
        sample = SAMPLE_HOST if path == ("hosts",) else SAMPLE_REPO
        (sub,) = schema["patternProperties"].values()
        yield from leaves(sub, (*path, sample))
    elif schema.get("type") == "array":
        yield from leaves(schema["items"], (*path, 0))
    else:
        yield path


def object_paths(schema: dict[str, Any], path: tuple[Any, ...] = ()) -> Iterator[tuple[Any, ...]]:
    if "properties" in schema:
        yield path
        for key, sub in schema["properties"].items():
            yield from object_paths(sub, (*path, key))
    elif "patternProperties" in schema:
        yield path
        sample = SAMPLE_HOST if path == ("hosts",) else SAMPLE_REPO
        (sub,) = schema["patternProperties"].values()
        yield from object_paths(sub, (*path, sample))
    elif schema.get("type") == "array" and schema["items"].get("type") == "object":
        yield from object_paths(schema["items"], (*path, 0))


ALL_LEAVES = sorted(set(leaves(SCHEMA)), key=str)


def doc_with_extra(path: tuple[Any, ...]) -> dict[str, Any]:
    node: Any = {"bogus": 1}
    if path and path[-1] == 0:
        node["cmd"] = "x"
    for part in reversed(path):
        node = [node] if part == 0 else {part: node}
    return node


def _non_dividing(value: Any) -> bool:
    count, unit = int(value[:-1]), value[-1]
    return bool(60 % count if unit == "m" else 24 % count)


def test_the_schema_has_the_expected_leaves() -> None:
    assert len(ALL_LEAVES) >= 40
    assert ("hooks", "on-ci", 0, "cmd") in ALL_LEAVES
    assert ("hosts", SAMPLE_HOST, "concurrency") in ALL_LEAVES
    assert ("hooks", "env_allow", 0) in ALL_LEAVES


@pytest.mark.parametrize("path", ALL_LEAVES, ids=lambda p: ".".join(map(str, p)))
def test_leaf_verdicts_agree(path: tuple[Any, ...]) -> None:
    disagreements = []
    for value in VALUES:
        doc = nest(path, value)
        want = CHECK.is_valid(doc)
        got = model_accepts(doc)
        if path == ("poll", "interval_hint") and want and not got and _non_dividing(value):
            continue  # the loader's divisibility rule, which a pattern cannot express
        if want != got:
            disagreements.append((value if len(str(value)) < 40 else "<long>", want, got))
    assert not disagreements, f"(value, schema, model): {disagreements}"


@pytest.mark.parametrize(
    "path", sorted(object_paths(SCHEMA), key=str), ids=lambda p: ".".join(map(str, p)) or "root"
)
def test_extra_keys_are_refused_by_both(path: tuple[Any, ...]) -> None:
    doc = doc_with_extra(path)
    assert not CHECK.is_valid(doc)
    assert not model_accepts(doc)


@pytest.mark.parametrize("keys, field", [(HOST_KEYS, "hosts"), (REPO_KEYS, "repos")])
def test_map_key_verdicts_agree(keys: list[str], field: str) -> None:
    for key in keys:
        doc = {field: {key: {}}}
        assert CHECK.is_valid(doc) == model_accepts(doc), key


def test_all_defaults_in_the_schema_match_the_model() -> None:
    defaults = Config().model_dump(by_alias=True)

    def walk(schema: dict[str, Any], model: Any, path: str) -> None:
        for key, sub in schema.get("properties", {}).items():
            if path.startswith("repos") or key not in model:
                continue
            if "default" in sub and sub["default"] is not None:
                assert model[key] == sub["default"], f"{path}.{key}"
            if sub.get("type") == "object" and isinstance(model[key], dict):
                walk(sub, model[key], f"{path}.{key}")

    walk(SCHEMA, defaults, "")


def test_env_keys_are_exactly_the_documented_scalar_keys() -> None:
    """interface.md: scalar keys of poll, retention, expiry, failures, ci, forgectl,
    detect_force_push, hooks.max_per_poll, hooks.max_per_pr_per_poll, hooks.concurrency, me.name."""
    expected: set[str] = set()
    props = SCHEMA["properties"]
    for group in ("poll", "retention", "expiry", "failures", "ci", "forgectl", "me"):
        expected |= {f"{group}.{k}" for k in props[group]["properties"]}
    expected.add("detect_force_push")
    expected |= {"hooks.max_per_poll", "hooks.max_per_pr_per_poll", "hooks.concurrency"}
    assert set(ENV_KEYS) == expected


def test_env_key_types_match_the_schema() -> None:
    kinds = {"string": "str", "integer": "int", "boolean": "bool"}
    for path, kind in ENV_KEYS.items():
        node: Any = SCHEMA
        for part in path.split("."):
            node = node["properties"][part]
        assert kinds[node["type"]] == kind, path


def test_model_patterns_equal_the_schema_patterns_up_to_the_ecma_anchor() -> None:
    schema_patterns = set(
        re.findall(
            r'"pattern": "((?:[^"\\]|\\.)*)"', (ROOT / "schemas" / "config.json").read_text()
        )
    )
    schema_patterns = {p.encode().decode("unicode_escape") for p in schema_patterns}
    model_patterns = {p.replace(r"\Z", "$") for p in _model_patterns()}
    # env_allow's pattern is enforced by a validator with its own messages, not a field pattern.
    assert schema_patterns - model_patterns <= {
        r"^(?!PR_WATCH_|LD_|DYLD_)[A-Za-z_][A-Za-z0-9_]{0,127}$"
    }
    assert model_patterns <= schema_patterns


def _model_patterns() -> set[str]:
    schema = Config.model_json_schema(by_alias=True)
    found: set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if isinstance(node.get("pattern"), str):
                found.add(node["pattern"])
            for key, value in node.items():
                if key == "propertyNames":
                    walk(value)
                else:
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(schema)
    return found
