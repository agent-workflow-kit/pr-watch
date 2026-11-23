.PHONY: verify test live lint fmt fmt-check typecheck mdlint

verify: lint fmt-check typecheck test mdlint

test:
	uv run pytest

# Real hosts through the real forgectl; needs the opt-in of docs/live.md, never part of verify.
live:
	uv run pytest -m live -s -rs tests/live

lint:
	uv run ruff check .

fmt:
	uv run ruff format .

fmt-check:
	uv run ruff format --check .

typecheck:
	uv run pyright

mdlint:
	uv run pymarkdown --config .pymarkdown.json scan -r --respect-gitignore .
