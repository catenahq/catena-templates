# Security policy

Email **security@catena.run** (see the full policy in
[catenahq/catena-ce SECURITY.md](https://github.com/catenahq/catena-ce/blob/main/SECURITY.md)).

Scope for THIS repository: the catalog metadata (`source/catalog.yml`,
`source/sizing-data.yml`), the per-template compose files
(`source/compose/`), the render pipeline (`build/render.py`), and the
generated `blueprints/` + `templates.json` that client Portainer
instances fetch from raw.githubusercontent.com. A compose change that
weakens a template's isolation (network exposure, dropped auth labels,
privileged mounts) is squarely in scope.

Vulnerabilities in the upstream applications the templates deploy
belong upstream; we track and ship the fixed versions.
