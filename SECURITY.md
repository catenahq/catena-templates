# Security policy

Email **security@catena.run** (see the full policy in
[catenahq/catena-ce SECURITY.md](https://github.com/catenahq/catena-ce/blob/HEAD/SECURITY.md)).

Scope for THIS repository: the per-template metadata (`sources/<id>.json`),
the hand-edited stack files (`blueprints/<id>/docker-compose.yml`), the
render pipeline (`build/render.py`), and the generated `templates.json`,
`catalog.json` and blueprint READMEs that client Portainer instances fetch
from raw.githubusercontent.com. A compose change that
weakens a template's isolation (network exposure, dropped auth labels,
privileged mounts) is squarely in scope.

Vulnerabilities in the upstream applications the templates deploy
belong upstream; we track and ship the fixed versions.
