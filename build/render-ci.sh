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
# a python, not necessarily uv. render.py needs exactly one third-party module,
# so the fallback is cheap and does not need uv's resolver. uv is still
# preferred where it exists, because that is what `make render` uses and the two
# must not be able to disagree.
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

# Only import-check rather than install unconditionally: a container that
# already has it should not need the network, and a pip that cannot reach the
# index should fail loudly here rather than inside the render.
if ! "$python" -c "import jsonschema" >/dev/null 2>&1; then
    echo "render-ci: installing jsonschema for $python"
    "$python" -m pip install --quiet --disable-pip-version-check jsonschema
fi

echo "render-ci: rendering with $python"
exec "$python" build/render.py
