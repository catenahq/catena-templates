#!/usr/bin/env bash
# Render sources/ into the committed artifacts, from an environment that may not
# have uv.
#
# WHY THIS EXISTS. Renovate edits sources/ -- a compose pin, the central
# postgres default -- and cannot run `make render`, so it opens a PR whose
# generated artifacts still describe the old pin and render-idempotency fails.
# Every catalog bump was red for that reason and none of them for anything
# wrong with the bump. Renovate's postUpgradeTasks runs this instead, so the PR
# it opens is complete.
#
# The renovate container is not this repo's dev environment: it carries node and
# a python3, no uv and NO PIP --
#
#     Command failed: bash build/render-ci.sh
#     /usr/bin/python3: No module named pip
#
# which is what this script tried first and why PR 28's artifacts still
# described the old pins. So it installs nothing: lib/model.py treats jsonschema
# as optional and skips the JSON Schema layer without it, and the
# render-idempotency job re-renders under uv and compares. uv is still preferred
# where it exists, because that is what `make render` uses and the two must not
# be able to disagree.
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
