# Catena template catalog.
#
# sources/ is hand-edited. Everything else at the repo root is generated
# by `make render` and drift-gated in CI, so the only workflow that ever
# needs to change is: edit sources/, run `make`, commit the diff.

.DEFAULT_GOAL := all
.PHONY: all render validate lint test verify serve image clean

## all: validate, render, lint, test -- the pre-commit pass
all: render lint test

## render: sources/ -> blueprints/ + templates.json + catalog.json + index.html
render:
	uv run build/render.py

## validate: schema-check sources/ and the committed templates.json, write nothing
validate:
	uv run build/validate.py

## lint: unattended-hook security lint + central Postgres pin enforcement
lint:
	uv run build/lint_quiesce.py
	uv run build/lint_postgres_pins.py

## test: unit tests over the build library
test:
	uv run --with pytest --with jsonschema pytest tests/ -q

## verify: what CI runs -- render must be idempotent against the commit
verify: render
	git diff --exit-code blueprints templates.json catalog.json index.html

## serve: preview the generated catalog locally on :8000
serve:
	python3 -m http.server 8000

## image: build the nginx image that serves the catalog from this tree
image:
	docker build -t catena-templates:local .

## clean: drop every generated artifact (they are committed; this is for bisecting)
clean:
	rm -rf blueprints templates.json catalog.json index.html
