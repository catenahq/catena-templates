#!/usr/bin/env bash
# Render the committed artifacts after a pin move.
#
# Its caller is ops automation/operator-tools/bump_versions.py, run by
# catena-admin .github/workflows/engine-bump.yml: the update engine edits the
# render's inputs -- a compose pin in blueprints/<id>/ -- and runs this after
# each bump, so the generated artifacts in the PR it opens match the inputs.
# It needs uv, and renders in the repo's locked environment, as `make render`.
set -euo pipefail

# Bash's own expansion rather than dirname: a missing coreutils would otherwise
# cd to / and render nothing, reporting success.
cd "${BASH_SOURCE[0]%/*}/.."

exec uv run --locked build/render.py
