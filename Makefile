# Catena template catalog.
#
# Hand-edited, in blueprints/<id>/: the card (app.json), services.yml, x-catena.yml
# and the script files under hooks/, which the render packs into docker-compose.yml.
# The packed docker-compose.yml and the placeholder logo of each blueprint are generated
# by `make render` and drift-gated in CI: edit the input, run `make`, commit the diff.
# The catalog document is printed by build/catalog.py and committed nowhere.

.DEFAULT_GOAL := all
.PHONY: all render validate lint test verify preview serve image clean

## all: validate, render, lint, test -- the pre-commit pass
all: render lint test

## render: pack each compose and write each blueprint's placeholder logo
render:
	uv run build/render.py

## validate: schema-check the inputs, write nothing
validate:
	uv run build/validate.py

## lint: unattended-hook security lint + Postgres pins + swarm compatibility
lint:
	uv run build/lint_quiesce.py
	uv run build/lint_postgres_pins.py
	uv run build/lint_swarm.py

## test: unit tests over the build library and the image builders
test:
	uv run --locked pytest tests/ images/erpnext-helpdesk -q

## verify: the render check CI runs -- render must be idempotent against the commit
verify:
	bash ci/run render

## preview: write catalog.json and index.html into the git-ignored preview/
preview:
	mkdir -p preview
	uv run build/catalog.py > preview/catalog.json
	uv run build/preview.py > preview/index.html

## serve: preview the catalog locally on :8000
serve: preview
	uv run python -m http.server 8000 --directory preview

## image: build the nginx image that serves the catalog from this tree
image: preview
	docker build -t catena-templates:local .

## clean: drop the git-ignored preview/. blueprints/ is spared: it holds the hand-edited files.
clean:
	rm -rf preview
