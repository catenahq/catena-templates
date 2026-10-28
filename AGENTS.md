# catenahq/catena-templates -- Portainer App Template catalog

This repo holds the Catena Portainer App Template catalog. See README.md
for layout, consumer model, and BASE URL setup.

## Edit rules

- A template is TWO hand-edited files: `sources/<id>.json` and
  `blueprints/<id>/docker-compose.yml`. The `id` MUST equal the source
  filename stem AND the blueprint directory name; the loader fails the
  build otherwise.
- Everything else is generated: the per-app `README.md` and placeholder
  `logo.svg`, plus `templates.json`, `catalog.json` and `index.html`.
  Never hand-edit a generated file -- the change is overwritten on the
  next render, and CI rejects the PR.
- What the server needs to back up, update and restore an app (backup
  mode, update and restore commands, object-store check, the env
  expressions Catena keeps set) is the root `x-catena:` block of the
  app's own `blueprints/<id>/docker-compose.yml`, and nowhere else. That
  includes the app's env: every key the deploy form shows, and how each is
  set.
  Portainer stores that file byte for byte, so the block travels with the
  deployed app and the server never reads this catalog to manage it. The
  block holds no `$` (Docker interpolates it at deploy), except in a
  `hooks` body, which writes each `$` as `$$`, and no image reference (an
  update rewrites those); it starts with `schema: 2`.
  `block.schema.json` is its format; catena-admin
  `payload/engines/appdecl` reads it. Three fields serve other lanes:
  `hooks.scan` (reads a file on stdin; a block that provides `antivirus`
  declares it), `hooks.reconcile` (counts the objects the database indexes that
  the object store lacks) and `probe` (how the boot probe starts the app alone
  and asks whether it came up). `make lint` holds their commands to the
  allowlists.
- An application other applications use declares what it gives in the block's
  `provides` (`{<capability>: {<name>: <key of its env>}}`). A user of it reads
  `{{ provided.<capability>.<name> }}` in a managed expression, never another
  app's `.env` and never a store secret kept for it. The value is empty when no
  application provides the capability, so the user's hook handles absence, and a
  capability is provided by one running application at a time. `make lint`
  fails a read no template provides.
- An application that offers access levels to invited people declares them in the
  block's `access` (`{<level>: {label: {en, fr}}}`); Catena names a realm group
  per level and admits its members at the application's own sign-in; a users
  hook, when declared, receives each member's levels.
- The outgoing mail chosen in Settings reaches an application as the facts
  `smtp_relay_host`, `smtp_relay_port`, `smtp_relay_user`, `smtp_relay_tls`
  (`ssl` or `tls`) and `smtp_relay_sender` in a managed expression, empty when
  no relay is chosen. The password is the stored secret `smtp_password`.
- An application with no web address (a scanner, a relay) leaves `domain` out of
  its source and names `version_from` in its block.
- An application that runs on the server's own network declares the
  `host_network` privilege and, in `vps.expose.tcp` and `vps.expose.udp` labels,
  each port it opens, a range of at most 1024 ports. Its compose joins the
  external network named `host`, because a stack deploy ignores `network_mode`.
  `make lint` fails a block that asks for the privilege and declares no port.
  Its start script writes any configuration file from the environment when the
  container starts, so no secret reaches the service's arguments.
- A compose file carries no descriptive header. What the template is,
  what it replaces, how sign-in works and what each env var means live
  in `sources/<id>.json` and reach the reader through the generated
  README. Comments inside the compose explain a construct that does not
  read as written, beside that construct.
- The workflows `app-ci.yml`, `build-and-verify.yml`, `app-dispatch.yml` and
  `app-tag.yml` also serve other repositories through `workflow_call`; their
  steps run unchanged for this repository (`catalog` true), and a change to
  them keeps both paths working. The build tools read another tree through
  `TEMPLATES_ROOT`, and the schemas always come from this checkout.
- A merge to `main` reaches every new deploy at once, so `main` stays
  deployable. A deployed app keeps the compose, and so the block, it was
  deployed with: a merge changes no running app.
- An entry that an older Catena release deploys in a shape it cannot
  serve names the oldest release it works on in
  `x-catena.requires_catena`, the one place that release is written: the
  render puts it in the Portainer description and the README, so the
  prose never repeats it.
- Every image pin is a version the update engine can order (catena-admin
  `payload/engines/stackupdate`): no floating tags. A tag scheme the generic
  rules do not read (a partial `18.6-alpine`, a build number, a date stamp) is
  declared by the app itself in `x-catena.images.<service>.rule`, written
  with no `^` or `$` (the whole tag is matched). Each app pins and moves its
  own database; there is no catalog-wide pin. The engine moves the pins, one
  app per pull request.
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
  fixed and its line deleted, or the entry is dropped. `make lint` refuses
  an error line and the line recording that the checker did not run.
  Never import an entry whose title or id holds a word on the contracts
  banned-word list; the importer skips it. One catalog app per upstream
  project: an entry the importer records as an alias (`origin.aliases`,
  `sources/_aliases.json`) is not imported again. Imports reach `main` one
  app per pull request on `import/<id>` (`--wave`), never ahead of an open
  update or vulnerability fix; keep the daily and open limits
  conservative. Finishing an imported entry
  means filling every stub, settling `pending`, then deleting `pending`
  and `status`; `origin` stays.
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
  labels. Three classes of value render to the sentinel: the block's
  `managed` env keys, `lookup('password', ...)` expressions, and
  anything still carrying Jinja (Portainer has no template engine, so an
  unresolved expression would become the app's literal secret).
- `catalog.json` -- the machine view the seeding consumers read, in one
  fetch: the flat per-template fields, `sizing`, the EN/FR prose, and the
  compose block lifted under the keys `env`, `lifecycle`, `quiesce`,
  `object_store` and `env_managed_keys`. `env_defaults` is the
  env as `KEY=value` lines: a setting's default, the `lookup('password', ...)`
  expression of a generated key, a managed key's expression.
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
README and the logo beside the compose, and a stack
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

1. `sources.schema.json` -- field shapes, enums, required keys, and the
   imported tier (an imported entry names its origin and its pending
   list; only a curated one has to carry a bench pack and a measured
   peak). `block.schema.json` -- the compose's `x-catena` block: its
   `schema`, the env shapes, the env key deny list (`DOCKER_*`, `PATH`,
   `HOME`, `LD_*`, `*_PROXY`), the quiesce timeout cap, argv shapes. Both run
   on every load, not just in CI.
2. Cross-file invariants in `lib/model.py` -- id matches filename, no
   duplicate slug, the compose file exists and its block declares an env.
3. `Schema.json` -- the generated `templates.json` against the published
   Portainer App Templates format, because that file is what a client's
   Portainer fetches.
4. `make lint` -- the block holds no `$` (outside a hooks body) and no
   image reference of its compose, the quiesce, lifecycle and hooks argv
   allowlists, a hooks script that parses in the interpreter it names, a
   managed expression that reads no store secret outside the block's
   `secrets` and none on the deny list, the
   quiesce service check, the `host_paths` check (a normalised absolute path
   holding neither the Docker socket's folders nor the compose folders; an app
   binds a host path outside its own data folder only when its unchanged
   catalog file declares it there), the versioned volume check (a top-level volume
   that only long-running services other than a database mount), the
   sign-in labels (on the main address's service, `redirect_uris` beside
   `vps.auth.oidc=true`, `vps.auth.oidc.scopes` holding client scope names,
   `vps.auth.oidc.introspect` never beside `vps.auth.oidc.public`, and no
   catalog default for the three `OIDC_*`
   values the host's settings sync writes), an imported entry's pending
   findings (no app checker error, and no record that the checker did not
   run), the Postgres pin check (a partial pin declares its `partial_line`
   rule in the block), and the
   swarm-compatibility gate above (which also offers each file to the real
   `docker stack config` loader when docker is on PATH, because the ban list
   was written against one docker version and the loader is the authority).

## Add a new template

README.md, "How to add a template", says how; these are the rules.

- Every app declares a verify hook (`x-catena.hooks.verify`) that makes the
  app's own server answer on the path users reach, checks each feature the
  app claims and its server provides, writes nothing, prints one line, exits 0
  only when every check holds, and answers ok while a maintenance mode that a
  backup or an update holds is on.
- `test.yml` seeds through the app itself (its API or command line) wherever
  the app allows it. Its asserts exercise the app; an assert that reads only
  data the seed itself wrote is never enough.
- An app that keeps live state a file snapshot can tear declares `quiesce`,
  one that migrates a schema declares `lifecycle` with `ready`, and one that
  keeps its files in a bucket the host can repoint declares `object_store`.
  `post` must succeed when `pre` never ran.
- Never weaken or remove a check, an assert or a leg to make an app pass:
  fix the app or its declaration.
- One app or none per branch, pull request and commit.

## Changing catalog.json

`sources.schema.json` and `block.schema.json` are internal to this repo; the
CONTRACT with every seeding consumer is `catalog.json`. A source or block
change that leaves `catalog.json` identical needs nothing else. A change to
`catalog.json` lands in the same merge window as its consumers, since they
fetch `main`:

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
