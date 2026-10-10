# catenahq/catena-templates -- Portainer App Template catalog

This repo holds the Catena Portainer App Template catalog. See README.md
for layout, consumer model, and BASE URL setup.

## Edit rules

- A template is TWO hand-edited files: `sources/<id>.json` and
  `blueprints/<id>/docker-compose.yml`. The `id` MUST equal the source
  filename stem AND the blueprint directory name; the loader fails the
  build otherwise.
- Everything else is generated: the per-app `README.md`, `quiesce.yml`
  and placeholder `logo.svg`, plus `templates.json`, `catalog.json` and
  `index.html`. Never hand-edit a generated file -- the change is
  overwritten on the next render, and CI rejects the PR.
- A compose file carries no descriptive header. What the template is,
  what it replaces, how sign-in works and what each env var means live
  in `sources/<id>.json` and reach the reader through the generated
  README. Comments inside the compose explain a construct that does not
  read as written, beside that construct.
- Every consumer reads `main`, fetched raw: a merge reaches every new
  deploy at once, so `main` stays deployable.
- Every image pin is a version the update engine can order (catena-admin
  `payload/engines/stackupdate`): no floating or partial tags. The engine
  moves the pins; a template whose tag scheme it cannot order yet gets an
  override there first.
- No emojis or em-dashes in any artifact. Plain hyphens + straight
  quotes only. `npm run check:unicode` enforces, and also scans for the
  names of systems Catena stopped shipping.
- Code comments describe the template pipeline as it stands, not what it
  used to do. `npm run check:prose` enforces, over the Python and the
  YAML comments alike. Both gates live in catenahq/contracts and run
  from the sibling checkout; this repo holds no copy. A debt entry in
  `.github/prose-debt.txt` that has become clean FAILS the gate and must
  be deleted.
- Bilingual prose (the `x-catena.en` / `x-catena.fr` blocks): both
  required, no EN-only or FR-only templates.
- An entry `build/import_registry.py` writes carries
  `x-catena.status: imported` and publishes marked untested, with stubs
  for its bench pack, sizing and prose. Import only from MIT, Apache-2.0
  or BSD sources: never widen `origin.licence` in the schema, and never
  import an entry whose licence the importer reports unknown. An imported
  entry merges only once `make` passes and the app checker reports no
  error on it (catena-admin CI runs `catena-applint --catalog` over every
  blueprint): every error the importer records in `pending.findings` is
  fixed, or the entry is dropped. Finishing an imported entry means filling
  every stub, settling `pending`, then deleting `pending` and `status`;
  `origin` stays.
- No secrets, ever. Sentinel placeholders (`__CATENA_OPERATOR_WIRED__`)
  in templates.json. This repo is public and one file serves every
  client, so it cannot hold a per-host value; a client's Portainer reads
  its OWN host's render of it instead (catena-admin
  `shell/marketplace`, from `catalog.json`). Nothing resolves the
  sentinel in this repo, and the published templates.json is the
  render's INPUT rather than a catalog to deploy from.

## The render contract

`make render` (thin entrypoint: `build/render.py`, logic in `lib/`)
transforms `sources/` into:

- `blueprints/<id>/README.md` -- the template's own page: what it is,
  what it replaces, how sign-in works, the setup steps and the env
  table, EN then FR. Rendered from the `x-catena` prose, with every
  Jinja expression resolved to a placeholder first.
- `blueprints/<id>/quiesce.yml` -- a readable copy of
  `x-catena.quiesce`, when the entry declares one. The host reads the
  block from `catalog.json`.
- `blueprints/<id>/logo.svg` -- a deterministic placeholder, unless a
  hand-placed `logo.png` sits beside it.

`blueprints/<id>/docker-compose.yml` is NOT rendered: it is the
hand-edited input and Portainer clones it from this public repo as the
type-2 stackfile. Jinja stays in place; the env it reads is resolved by
the host's own catalog render before the deploy, and routing is
reconciled post-deploy by dashboard-sync. The render never writes into
that file, and it deletes a blueprint directory only when no source file
claims it any more.
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
differ in ways that are not symmetric. Four rules follow, all enforced by
`make lint` (`lib/swarm_lint.py`):

- **There is no start ordering.** `depends_on` is accepted by the loader,
  dropped by the deploy, and reported by nothing -- so it is banned outright
  rather than left to read as if it worked. A service that starts before its
  database is expected to exit; `deploy.restart_policy` brings it back. Where
  a crash-restart would be destructive (an installer that half-writes its
  state and then takes the upgrade branch on the retry) the service waits for
  its dependency ITSELF, in its entrypoint. Nextcloud is the worked example.
- **Every service runs until stopped.** `deploy.restart_policy.condition` is
  `any`. A service that finishes stays below its replica count, and Portainer
  fails a deploy when the first task it lists for such a service failed, so
  one failed run can fail every later deploy and leave Portainer's stored
  file behind what the services run. A setup step runs in a long-running
  service: before its process starts (ERPNext's `backend` creates the site,
  then serves it), or before it waits (Zammad's `zammad-init`).
- **A service that mounts state declares where it lives.** Named volume or
  host path means `deploy.placement.constraints:
  [node.labels.catena.role==data]`. At one node the constraint does nothing;
  the moment a second node joins, swarm may schedule the service onto it and
  CREATE the missing volume there, empty and without an error.
- **A published port is `mode: host`.** The swarm default is the ingress
  routing mesh, which replaces the client address with a mesh address before
  the packet arrives. Every port in this catalog belongs to a service that
  needs the real peer address.

`configs:` is refused outright unless the object is `external`. A swarm stack
file has no inline content form, and its `configs.file` reads a path beside
the compose that neither deploy path has: `build/render.py` puts only the
README, the logo and `quiesce.yml` beside the compose, and a stack
created from a posted `StackFileContent` string has no directory at all.
`docker stack config` cannot see either problem, because it resolves the path
against this repository, where the file does sit next to the compose. Config
files a template needs are written by the service's own entrypoint, and the
lint offers every inline script to the interpreter it names.

## Naming and identity

`name` and `x-catena.app_name` are the same string and both start with
`catena-`. That string is the Portainer stack name a client sees, so under
swarm it prefixes every service (`catena-nextcloud_app`), every task
container (`catena-nextcloud_app.1.<task-id>`) and every volume
(`catena-nextcloud_nc-data`).

`id` does NOT get the prefix: it is the join key every consumer already uses
and the blueprint directory name.

The host knows an app by its stack name and a service by its key in the
compose: swarm stamps both on every task (`com.docker.stack.namespace`,
`com.docker.swarm.service.name=<app_name>_<service>`). Each service that
declares `vps.route.host` is an address of the app, and the host reads that
address's labels (`vps.route.port`, `vps.auth.mode`, `vps.auth.groups`,
`vps.health.*`) from that service; it joins `catena-network`, where Traefik
and the address's oauth2-proxy reach it by its swarm name
`<app_name>_<service>`. The main address is the only one, or, of several, the
one whose service also carries `vps.route.main=true`, and `x-catena.domain`
names it. The host reads the app-wide labels (the sign-in labels
`vps.auth.oidc*`, `vps.auth.protected`, `vps.display-name`,
`vps.homepage.*`) from the main address's service alone.

The host runs a quiesce or lifecycle command in the container of the service
the block names, and `make lint` checks that a quiesce block's `service` is a
service the compose actually defines -- because a service no container runs
under fails the quiesce on every host, and the backup is then taken without
it.

## Validation layers

1. `sources.schema.json` -- field shapes, enums, required keys,
   the quiesce timeout cap, and the imported tier (an imported entry
   names its origin and its pending list; only a curated one has to
   carry a bench pack and a measured peak). Runs on every load, not just
   in CI.
2. Cross-file invariants in `lib/model.py` -- id matches filename, no
   duplicate slug, the compose file exists, no `env_managed_keys` entry
   that names nothing.
3. `Schema.json` -- the generated `templates.json` against the published
   Portainer App Templates format, because that file is what a client's
   Portainer fetches.
4. `make lint` -- the quiesce and lifecycle argv allowlists, the
   quiesce service check, the versioned volume check (a top-level volume
   that only long-running services other than a database mount), the
   sign-in labels (on the main address's service, `redirect_uris` beside
   `vps.auth.oidc=true`, and no catalog default for the three `OIDC_*`
   values the host's settings sync writes),
   central Postgres pin enforcement, and the
   swarm-compatibility gate above (which also offers each file to the real
   `docker stack config` loader when docker is on PATH, because the ban list
   was written against one docker version and the loader is the authority).

## Add a new template

1. `sources/<id>.json`. Required: `id`, `type` (2), `title`, `name`,
   `categories`, `platform`, and the `x-catena` block (`app_name`,
   `upstream_url`, `sso_mode`, `domain`, `compose_file`, `env_defaults`,
   `bench.pack`, `sizing.peak_ram_mb`, `en`, `fr`).
2. `blueprints/<id>/docker-compose.yml` (Jinja stays in place; the
   render never writes into it).
3. `blueprints/<id>/logo.png` (512x512 PNG, max 100KB). Optional.
4. If the application keeps live state that a file-level snapshot can
   tear and the backup's own Postgres and MariaDB dumps do not cover,
   add `x-catena.quiesce`: argv arrays the nightly maintenance runs
   inside one service before (`pre`) and after (`post`) its backup, with
   no shell. `post` must succeed when `pre` never ran: it also runs after
   a failed or resumed backup and after a restore. Real examples:
   `sources/nextcloud-s3-oidc.json` (maintenance mode),
   `sources/rocketchat-oidc.json` (MongoDB fsync lock).
5. If the application keeps a schema its new code expects migrated, add
   `x-catena.lifecycle`: `migrate` runs after every update the host applies
   to the image of the service it names, and after a forward restore (a
   start-time migration runs
   against the pre-replay database, which the replay then overwrites).
   `before_update` / `after_update` wrap an update, for an application
   with a maintenance mode. `ready` is one command the update lane runs
   before each phase, and a forward restore before its migration, until
   it exits 0, for an image whose entrypoint
   copies or upgrades the application before starting it. Commands are
   argv arrays -- `docker exec` gives them no shell. `versioned_volumes`
   names the volumes the new version of that service upgrades in place
   and the old one refuses to start on or misreads (code tree,
   configuration, add-ons, or a SQLite database; never a volume of the
   users' files the old one still reads): the lane copies them before an
   update, in the backup mode when the entry declares one, and puts the
   copies back when it rolls back, losing what was written since, as a
   database dump's replay does. An application that upgrades such a
   volume as it starts declares `service`, `versioned_volumes` and
   `timeout_seconds` alone. Real examples: `sources/outline.json`,
   `sources/nextcloud-s3-oidc.json`, `sources/erpnext.json`,
   `sources/actualbudget.json`.
6. `make` -- render + lint + test. Commit the regenerated artifacts.
7. Open a PR. CI must pass `build-and-verify.yml`, `check:unicode` and `check:prose`.

## Changing catalog.json

`sources.schema.json` is internal to this repo; the CONTRACT with every
consumer is `catalog.json`. A source change that leaves `catalog.json`
identical needs nothing else. A change to `catalog.json` lands in the same
merge window as its consumers, since they all read `main`:

1. ops `automation/helpers/templates_catalog.py` and
   `automation/operator-tools/generate-sizing-doc.py`.
2. The per-host render in catena-admin (`shell/marketplace`) and its
   catalog engine (`payload/engines/catalog`) -- the render reads
   `catalog.json` and rewrites `templates.json` for one host, so a shape
   change breaks a client's marketplace, not just a report.

## What does NOT live here

- The per-host render that resolves the sentinels, and the on-box
  minting of per-deploy app passwords. catenahq/catena-admin
  (`shell/marketplace`).
- On-box config key names: catenahq/catena-ce. The Keycloak client an
  app signs in with: catenahq/catena-admin's settings sync, from the
  app's sign-in labels.
- Per-VPS runtime state. All under `/var/lib/catena/` on each VPS.
- The client docs site. catenahq/docs is hand-written and reads nothing
  from this repo; a template's own documentation is its README here.

## Security invariants (machine-enforced -- do not weaken silently)

- No secrets, ever: sentinel placeholders only (gitleaks on every
  change; the catalog is public and fetched raw by every deployment).
- No unresolved Jinja in `templates.json`: it would become the literal
  value of the app's secret on a marketplace deploy. Asserted in
  `tests/test_render.py`.
- Generated artifacts are BUILD OUTPUTS; hand-edits fail the
  idempotent-render CI gate.
- Every catalog image ref is CVE-scanned (security.yml, through
  scanctl.yml's image pins);
  quiesce and lifecycle argv each pass their own allowlist in
  `lib/quiesce_lint.py`, with no shell operator.
- SPEC.md gate pointers must resolve (ops audit --check-public-specs).
