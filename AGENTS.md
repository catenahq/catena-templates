# catenahq/catena-templates -- Portainer App Template catalog

This repo holds the Catena Portainer App Template catalog. See README.md
for layout, consumer model, and BASE URL setup.

## Edit rules

- `sources/` is canonical. `blueprints/`, `templates.json`,
  `catalog.json` and `index.html` are generated. Never hand-edit a
  generated file -- the change is overwritten on the next render, and CI
  rejects the PR.
- One template is one file: `sources/<id>.json`. The `id` MUST equal the
  filename stem; the loader fails the build otherwise.
- Every change is a deliberate version bump. Tag a `vX.Y.Z` release on
  every merge to main; catenahq/ops pulls via that tag.
- No emojis or em-dashes in any artifact. Plain hyphens + straight
  quotes only. `npm run check:unicode` enforces.
- Bilingual prose (the `x-catena.en` / `x-catena.fr` blocks): both
  required, no EN-only or FR-only templates.
- No secrets, ever. Sentinel placeholders (`__CATENA_OPERATOR_WIRED__`)
  in templates.json for env vars the converge owns.

## The render contract

`make render` (thin entrypoint: `build/render.py`, logic in `lib/`)
transforms `sources/` into:

- `blueprints/<id>/docker-compose.yml` -- the compose file copied
  verbatim; it IS the Portainer type-2 stackfile. Jinja stays in place;
  the converge reconciles env + routing post-deploy.
- `blueprints/<id>/quiesce.yml` -- when the entry declares
  `x-catena.quiesce`.
- `templates.json` -- one Portainer App Template per source: type 2,
  title/name/description/note/categories/logo, `repository{url,
  stackfile}` pointing at `blueprints/<id>/`, and `env` with human
  labels. Three classes of value render to the sentinel: declared
  `env_managed_keys`, `lookup('password', ...)` expressions, and
  anything still carrying Jinja (Portainer has no template engine, so an
  unresolved expression would become the app's literal secret).
- `catalog.json` -- the machine view every Catena consumer reads, in one
  fetch: the flat per-template fields, `sizing`, and the EN/FR prose.
- `index.html` -- a static preview of the catalog.

The render must be idempotent: running it twice produces byte-identical
outputs. CI verifies with `git diff --exit-code` over all four.

## Every compose here is a SWARM stack file

The client host runs a single-node swarm carrying the catena services, and
every template deploys onto it (Portainer template type 2). `docker stack
deploy` reads these files, not `docker compose up`, and the two loaders
differ in ways that are not symmetric. Three rules follow, all enforced by
`make lint` (`lib/swarm_lint.py`):

- **There is no start ordering.** `depends_on` is accepted by the loader,
  dropped by the deploy, and reported by nothing -- so it is banned outright
  rather than left to read as if it worked. A service that starts before its
  database is expected to exit; `deploy.restart_policy` brings it back. Where
  a crash-restart would be destructive (an installer that half-writes its
  state and then takes the upgrade branch on the retry) the service waits for
  its dependency ITSELF, in its entrypoint. Nextcloud is the worked example.
- **A service that mounts state declares where it lives.** Named volume or
  host path means `deploy.placement.constraints:
  [node.labels.catena.role==data]`. At one node the constraint does nothing;
  the moment a second node joins, swarm may schedule the service onto it and
  CREATE the missing volume there, empty and without an error.
- **A published port is `mode: host`.** The swarm default is the ingress
  routing mesh, which replaces the client address with a mesh address before
  the packet arrives. Every port in this catalog belongs to a service that
  needs the real peer address.

`configs:` has no inline form here either: a swarm stack file accepts
`configs.file` (read from beside the compose, which does not exist when a
stack is deployed from a posted string) or an external object. Config files a
template needs are written by the service's own entrypoint, and the lint
offers every inline script to the interpreter it names.

## Validation layers

1. `sources.schema.json` -- field shapes, enums, required keys,
   the quiesce timeout cap. Runs on every load, not just in CI.
2. Cross-file invariants in `lib/model.py` -- id matches filename, no
   duplicate slug, the compose file exists, no `env_managed_keys` entry
   that names nothing.
3. `Schema.json` -- the generated `templates.json` against the published
   Portainer App Templates format, because that file is what a client's
   Portainer fetches.
4. `make lint` -- quiesce-hook allowlist + shellcheck, post-restore
   migration argv allowlist, central Postgres pin enforcement, and the
   swarm-compatibility gate above (which also offers each file to the real
   `docker stack config` loader when docker is on PATH, because the ban list
   was written against one docker version and the loader is the authority).

## Add a new template

1. `sources/<id>.json`. Required: `id`, `type` (3), `title`, `name`,
   `categories`, `platform`, and the `x-catena` block (`app_name`,
   `upstream_url`, `sso_mode`, `domain`, `compose_file`, `env_defaults`,
   `bench.pack`, `sizing.peak_ram_mb`, `en`, `fr`).
2. `sources/compose/<id>.compose.yml` (existing Jinja-templated form is
   fine -- render copies it verbatim).
3. `sources/assets/<id>/logo.png` (512x512 PNG, max 100KB). Optional.
4. If the template has write traffic during backup, add
   `x-catena.quiesce`. Stateless / read-only templates omit it. Real
   examples: `sources/nextcloud-s3-oidc.json`,
   `sources/rocketchat-oidc.json`.
5. If the application migrates its own schema at container start, add
   `x-catena.post_restore_migrate`. That start-time migration runs
   against the pre-replay database and the replay then overwrites it, so
   after a restore across versions it has effectively not run. Commands
   are argv arrays -- `docker exec` gives them no shell. Real examples:
   `sources/outline.json`, `sources/nextcloud-s3-oidc.json`.
6. `make` -- render + lint + test. Commit the regenerated artifacts.
7. Open a PR. CI must pass `build-and-verify.yml` and `check:unicode`.
8. After merge, `git tag -a vX.Y.Z -m "..." && git push --tags`.

## When to bump the schema

`sources.schema.json` is internal to this repo; the CONTRACT with
catenahq/ops is `catalog.json`. Changing the source shape is a minor
bump when `catalog.json` comes out identical. Changing `catalog.json`
is a major bump and needs coordinated PRs:

1. Land the new shape here behind a major version bump.
2. Update `automation/helpers/templates_catalog.py` in catenahq/ops in
   the same merge window.
3. Update `generate-template-docs.py` + `generate-sizing-doc.py`.
4. Update the Ansible loader in catena-ce
   (`roles/infrastructure/tasks/_templates_catalog_load.yml`).
5. Bump the vendored tarball in catenahq/ops via the bump workflow.

## What does NOT live here

- Operator-side wiring (on-box config key names, OIDC client minting
  flow, `env_managed_keys` re-injection logic). All in catenahq/ops.
- Per-VPS runtime state. All under `/var/lib/catena/` on each VPS.
- Docs site copy. catenahq/docs generates the per-template pages from
  `catalog.json` via a sibling-write generator.

## Security invariants (machine-enforced -- do not weaken silently)

- No secrets, ever: sentinel placeholders only (gitleaks on every
  change; the catalog is public and fetched raw by every deployment).
- No unresolved Jinja in `templates.json`: it would become the literal
  value of the app's secret on a marketplace deploy. Asserted in
  `tests/test_render.py`.
- Generated artifacts are BUILD OUTPUTS; hand-edits fail the
  idempotent-render CI gate.
- Every catalog image ref is CVE-scanned (trivy-images workflow);
  quiesce snippets pass the allowlist + path restriction in
  `lib/quiesce_lint.py` (no curl/wget, no rm outside the app's data
  path), and post-restore migration argv pass their own, tighter
  allowlist in the same module.
- SPEC.md gate pointers must resolve (ops audit --check-public-specs).
