"""Schema validation helper: every `--json` output, hook payload and config is checked here."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError
from referencing import Registry, Resource

ROOT = Path(__file__).resolve().parent.parent
SCHEMAS_DIR = ROOT / "schemas"


def schema_names() -> list[str]:
    """Every schema as a name relative to schemas/ without `.json` (`cli/list`, `defs`)."""
    return sorted(
        p.relative_to(SCHEMAS_DIR).with_suffix("").as_posix() for p in SCHEMAS_DIR.rglob("*.json")
    )


def schema_path(name: str) -> Path:
    path = (SCHEMAS_DIR / f"{name}.json").resolve()
    if not path.is_relative_to(SCHEMAS_DIR.resolve()):
        raise ValueError(f"schema name escapes schemas/: {name!r}")
    return path


def load_schema(name: str) -> dict[str, Any]:
    with schema_path(name).open(encoding="utf-8") as fh:
        return json.load(fh)


@lru_cache(maxsize=1)
def _registry() -> Registry[Any]:
    resources = []
    for name in schema_names():
        doc = load_schema(name)
        resources.append((doc["$id"], Resource.from_contents(doc)))
    return Registry().with_resources(resources)


def validator(name: str, definition: str | None = None) -> Draft202012Validator:
    """A validator for one schema, or for one entry of its `$defs` when `definition` is given.

    No format assertions: the schemas use patterns only.
    """
    if definition is None:
        return Draft202012Validator(load_schema(name), registry=_registry())
    ref = {"$ref": f"{load_schema(name)['$id']}#/$defs/{definition}"}
    return Draft202012Validator(ref, registry=_registry())


def schema_errors(name: str, instance: Any, definition: str | None = None) -> list[ValidationError]:
    return list(validator(name, definition).iter_errors(instance))


def assert_valid(name: str, instance: Any, definition: str | None = None) -> None:
    errors = schema_errors(name, instance, definition)
    if errors:
        lines = [
            f"  {'/'.join(map(str, e.absolute_path)) or '<root>'}: {e.message}" for e in errors
        ]
        raise AssertionError(f"{name} rejected the document:\n" + "\n".join(lines))
