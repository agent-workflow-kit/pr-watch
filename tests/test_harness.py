import pytest

from tests.harness import assert_valid, schema_errors, schema_names, schema_path

ERROR_DOC = {"error": {"code": "usage", "message": "unknown command"}}


def test_every_schema_file_is_discovered_by_name() -> None:
    names = schema_names()
    assert {"defs", "config", "event", "log", "cli/error", "hooks/post-poll"} <= set(names)


def test_valid_document_validates_through_the_shared_helper() -> None:
    assert_valid("cli/error", ERROR_DOC)


def test_valid_definition_validates_through_cross_file_refs() -> None:
    assert_valid("defs", "2026-01-02T03:04:05.123456Z", definition="ts")


def test_unknown_error_code_is_rejected() -> None:
    bad = {"error": {"code": "nope", "message": "x"}}
    assert schema_errors("cli/error", bad)


def test_invalid_document_raises_with_schema_name() -> None:
    with pytest.raises(AssertionError, match="cli/error rejected"):
        assert_valid("cli/error", {"error": {"code": "usage"}})


def test_extra_top_level_field_is_rejected_by_the_closed_envelope() -> None:
    assert schema_errors("cli/error", {**ERROR_DOC, "extra": 1})


def test_schema_name_cannot_escape_the_schemas_directory() -> None:
    with pytest.raises(ValueError):
        schema_path("../pyproject")
