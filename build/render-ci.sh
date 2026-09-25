#!/usr/bin/env bash
# Render the committed artifacts from an environment that may not have uv.
#
# Renovate edits the render's inputs -- a compose pin in blueprints/<id>/, the
# central postgres default in sources/_meta.json -- and cannot run
# `make render`. Its postUpgradeTasks runs this after each bump, so the
# generated artifacts in the PR it opens match the inputs and render-idempotency
# passes.
#
# The renovate container is not this repo's dev environment: it carries node and
# a python3, with no uv and no pip. So this installs nothing: lib/model.py treats
# jsonschema as optional and skips the JSON Schema layer without it, and the
# render-idempotency job re-renders under uv and compares. uv is preferred where
# it exists, because that is what `make render` uses and the two must not be
# able to disagree.
set -euo pipefail

# Bash's own expansion rather than dirname: this runs in whatever container
# Renovate is using, and the repo root must resolve before any external tool is
# assumed to exist. A missing coreutils here would otherwise cd to / and render
# nothing, reporting success.
cd "${BASH_SOURCE[0]%/*}/.."

if command -v uv >/dev/null 2>&1; then
    echo "render-ci: uv present; rendering through it"
    exec uv run build/render.py
fi

python=""
for candidate in python3 python; do
    if command -v "$candidate" >/dev/null 2>&1; then
        python="$candidate"
        break
    fi
done
if [ -z "$python" ]; then
    echo "render-ci: no uv and no python on PATH; cannot render" >&2
    exit 1
fi

echo "render-ci: rendering with $python"
exec "$python" build/render.py
