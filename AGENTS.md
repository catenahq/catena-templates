# catenahq/catena-templates -- the Catena app catalog

This repo holds the Catena app catalog. See README.md for layout, who reads
the repository, and the checks.

## Edit rules

- A template is one folder, `blueprints/<id>/`, whose hand-edited files are
  `app.json`, `services.yml`, `x-catena.yml`, the script files under `hooks/`, an
  optional `test.yml` and an optional `logo.png`. The folder name is the app's id
  (an `app.json` has no `id` field). The compose carries no `vps.homepage.*` label
  (`make lint` refuses any) and the app's web address is derived from its labels.
  No README is kept per app: a template's description and setup steps are in its
  `app.json`.
- Everything else is generated: the packed `docker-compose.yml` of each app and
  its placeholder `logo.svg`, plus `catalog.json` and `index.html`.
  Never hand-edit a generated file -- the change is overwritten on the
  next render, and CI rejects the change.
- A hand-placed `logo.png` is the project's own logo, unmodified, made by
  `build/logo.py`, and its `app.json` names it in `logo`; it is used
  whatever its owner's trademark terms say.
- What the server needs to back up, update and restore an app (backup
  mode, update and restore commands, object-store check, the env
  expressions Catena keeps set) is the app's own `blueprints/<id>/x-catena.yml`,
  which the render packs into the root `x-catena:` block of its
  `docker-compose.yml`, and nowhere else. That
  includes the app's env: every key the deploy form shows, and how each is
  set.
  A host adds the packed compose byte for byte, so the block travels with the
  deployed app and the server never reads this catalog to manage it. The
  block holds no `$` (Docker interpolates it at deploy), except in a
  script, which the render writes with each `$` doubled, and no image
  reference (an update rewrites those); `x-catena.yml` starts with `schema: 2`.
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
  (`ssl` or `tls`) and `smtp_relay_sender` in a managed expression. The password
  is the stored secret `smtp_password`. None of them exists until a relay is
  chosen, so each read carries `| default('')`.
- An application with no web address (a scanner, a relay) has no service
  labelled `vps.route.host` and names `version_from` in its block.
- An application that runs on the server's own network declares the
  `host_network` privilege and, in `vps.expose.tcp` and `vps.expose.udp` labels,
  each port it opens, a range of at most 1024 ports. A service of the block that
  listens on no host port carries `vps.expose.none: "true"` instead, and never
  both; Catena opens nothing for it. Its compose joins the
  external network named `host`, because a stack deploy ignores `network_mode`.
  `make lint` fails a block that asks for the privilege and declares no port.
  Its start script writes any configuration file from the environment when the
  container starts, so no secret reaches the service's arguments.
- A compose file carries no descriptive header. What the template is,
  what it replaces, how sign-in works and what each env var means live
  in `app.json`, which the catalog and the panel show. Comments inside
  `services.yml` and `x-catena.yml` explain a construct that does not
  read as written, beside that construct.
- The workflows `app-ci.yml`, `build-and-verify.yml`, `app-dispatch.yml` and
  `app-tag.yml` also serve other repositories through `workflow_call`; their
  steps run unchanged for this repository (`catalog` true), and a change to
  them keeps both paths working; `app-main.yml` serves this repository alone.
  A caller of `app-ci.yml` or `build-and-verify.yml` grants `actions: read`.
  The build tools read another tree through
  `TEMPLATES_ROOT`, and the schemas always come from this checkout.
- A change on `main` reaches every new deploy at once, so `main` stays
  deployable. A deployed app keeps the compose, and so the block, it was
  deployed with: a new commit changes no running app.
- An entry that an older Catena release deploys in a shape it cannot
  serve names the oldest release it works on in
  `requires_catena` (a key of `x-catena.yml`), the one place that release is
  written: the render carries it to `catalog.json` and `index.html`, so the prose
  never repeats it.
- An app whose folder holds `services.yml` is held as neat files, as every app here
  is: edit `services.yml`, `x-catena.yml` and `hooks/`, never its
  `docker-compose.yml`, which the render packs from them (`lib/pack.py`; README.md,
  "An app held as neat files"). A multi-line script of a hook or a lifecycle step
  is a file under `hooks/`, named for its place (`hooks/verify.js`) and referenced
  in `x-catena.yml` by its path alone; its extension names the interpreter. A
  one-line command stays argv in `x-catena.yml`. A script file writes each `$`
  once: the render doubles it. A file in `hooks/` that `x-catena.yml` does not name
  stops the render. A snippet version (`snippets/<name>/<n>.<ext>`) that two or
  more apps name is never edited: add a version and move each app to it in its own
  commit.
- A file's or field's class (runtime, verification or presentation) comes from
  `lib/versions.py` (`FILE_CLASSES`, `FIELD_CLASSES`). A new file type or field
  gets a row there; anything unlisted is runtime.
- A block field a published catena-admin release does not read reaches `main`
  only once the release that reads it is published, with
  `requires_catena` raised to it; app-ci's released app checker
  refuses it before. The block's own `requires_catena` is such a field: a
  compose sets it only once the release that reads it is published.
- Every image pin is a version the update engine can order (catena-admin
  `payload/engines/stackupdate`): no floating tags. A tag scheme the generic
  rules do not read (a partial `18.6-alpine`, a build number, a date stamp) is
  declared by the app itself in `x-catena.images.<service>.rule`, written
  with no `^` or `$` (the whole tag is matched). Each app pins and moves its
  own database; there is no catalog-wide pin. The engine moves the pins, one
  app per pull request.
- An app service's `vps.auto-update` label is `${UPDATE_LEVEL:-major}` and the
  block declares `UPDATE_LEVEL` as a setting with the default `major`; a
  database, a cache and a stateful service no data upgrade covers keep a literal
  label, and a literal label on a service that is not a database or a cache has
  a comment beside it naming why. `make lint` refuses a literal label without
  one and a compose that reads `UPDATE_LEVEL` without declaring it.
- An app's main address reads who may open it from the setting `AUTH_MODE`: its
  `vps.auth.mode` label is `${AUTH_MODE:-<mode>}` and the block declares
  `AUTH_MODE` as a setting with the same default. Another address whose protocol
  needs one access keeps a literal label with a comment beside it naming why.
  `make lint` refuses a literal without that comment, a variable with no mode
  default, and a default the setting does not share.
- No emojis or em-dashes in any artifact. Plain hyphens + straight
  quotes only. `npm run check:unicode` enforces, and also scans for the
  system names it lists.
- Code comments describe the template pipeline as it stands.
  `npm run check:prose` enforces, over the Python and the
  YAML comments alike. Both gates live in catenahq/contracts and run
  from the sibling checkout; this repo holds no copy. A debt entry in
  `.github/prose-debt.txt` that has become clean FAILS the gate and must
  be deleted.
- Bilingual prose (the `en` / `fr` objects of `app.json`): both required, no
  EN-only or FR-only templates. Each carries a `summary`, the card's one line.
- An entry `build/import_registry.py` writes carries
  `status: imported` and publishes marked untested, with stubs
  for its bench pack, sizing and prose. `origin` records where its compose
  file was copied from, with that source's licence and notice. Import only
  an app whose own licence is on the importer's `APP_LICENCES`
  (`lib/importers/pipeline.py`), and never one whose own licence the
  importer reports unknown. An imported
  entry merges only once `make` passes and the app checker reports no
  error on it (catena-admin CI runs `catena-applint --catalog` over every
  blueprint): every error the importer records in `pending.findings` is
  fixed and its line deleted, or the entry is dropped. `make lint` refuses
  an error line and the line recording that the checker did not run.
  Never import an entry whose title or id holds a word on the contracts
  banned-word list; the importer skips it. One catalog app per upstream
  project: an entry the importer records as an alias (`origin.aliases`,
  `catalog-aliases.json`) is not imported again. Imports reach `main` one
  app per pull request on `import/<id>` (`--wave`), never ahead of an open
  update or vulnerability fix; keep the daily and open limits
  conservative. Finishing an imported entry
  means filling every stub, settling `pending`, then deleting `pending`
  and `status`; `origin` stays.
- No secrets, ever. This repo is public and one file serves every client, so
  it cannot hold a per-host value: a host mints each generated value when it
  deploys the app.
- An app has at most one own image and stock pinned images for the rest; an own
  image is a public package; a database image's name is one `appdb.Family`
  (catena-admin `payload/engines/appdb/appdb.go`) recognises, so it is backed up.
- An own-built image's pin names what its build published.
- A domain-dependent setting is a managed key (`env_managed_keys`), so it follows
  a domain change; an app that bakes its hostname sets `hostname_baked: true` and
  the client moves it.
- Keep the `HPB` and `JITSI` BEGIN/END delimiter lines intact: ops
  `automation/tests/unit/test_templates_contract.py` reads them.
- An HTTP app publishes no host port; a non-HTTP port is declared in
  `vps.expose.tcp` or `vps.expose.udp`, never a firewall rule.
- A block declares no MX, SPF, DKIM or DMARC record in its DNS needs: those are
  the domain owner's.
- Keep ERPNext's own CRM module; ask before adding Frappe CRM or widening the
  ERPNext app set.
- Every major move of an app is one main commit, gated by app-ci and the
  app-tests upgrade and rollback.
- Only `app-tag` writes a tag `<app>/<upstream version>-<revision>`; no signed
  archive, no registry publishing.

## The render contract

`make render` (thin entrypoint: `build/render.py`, logic in `lib/`)
transforms each app's files into:

- `blueprints/<id>/docker-compose.yml` -- packed from `services.yml`,
  `x-catena.yml` and the script files (`lib/pack.py`), committed beside them;
  a host fetches it from this public repo at a tag. Jinja stays in place; the env
  it reads is settled on the host, and routing is reconciled post-deploy by
  dashboard-sync.
- `blueprints/<id>/logo.svg` -- a deterministic placeholder, unless a
  hand-placed `logo.png` sits beside it.

The render deletes a blueprint directory only when it holds no `app.json`.

- `catalog.json` -- the browsable catalog, in one
  fetch: the flat per-template fields, `sizing`, the EN/FR prose, and the
  compose block lifted under the keys `env`, `lifecycle`, `quiesce`,
  `object_store` and `env_managed_keys`. `env_defaults` is the
  env as `KEY=value` lines: a setting's default, the `lookup('password', ...)`
  expression of a generated key, a managed key's expression.
- `index.html` -- a static preview of the catalog, browsable beside `catalog.json`.

The render must be idempotent: running it twice produces byte-identical
outputs. CI verifies with `git diff --exit-code` over the generated files.

## Every compose here is a SWARM stack file

The client host runs a single-node swarm carrying the catena services, and
every template deploys onto it. `docker stack
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
  `any`. A service that finishes stays below its replica count, and its
  finished task never reads running to the host (catena-admin
  `payload/engines/appdecl` OneShot). A setup step runs in a long-running
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
the compose that a host does not have: it adds the compose byte for byte.
`docker stack config` cannot see the problem, because it resolves the path
against this repository, where the file does sit next to the compose. Config
files a template needs are written by the service's own entrypoint, and the
lint offers every inline script to the interpreter it names.

## Naming and identity

`app_name` in `app.json` starts with `catena-`. That string is the stack name, so under
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
one whose service also carries `vps.route.main=true`. The host reads the app-wide labels (the sign-in labels
`vps.auth.oidc*`, `vps.auth.protected`, `vps.display-name`) from the main address's service
alone.

`catena-network` holds every app's service keys, and Docker names a task only once
it is healthy, so a service on it that asks for a sibling's key can reach another
app. Such a service reaches each sibling by an alias of the sibling on the stack
network named `<id>-<role>`, which `tests/test_catalog_entries.py` holds to one
app; an nginx asks for it per request (`resolver 127.0.0.11` and the name in a
variable of `fastcgi_pass` or `proxy_pass`).

The host runs a quiesce or lifecycle command in the container of the service
the block names, and `make lint` checks that a quiesce block's `service` is a
service the compose actually defines -- because a service no container runs
under fails the quiesce on every host, and the backup is then taken without
it.

## Validation layers

1. `app.schema.json` -- field shapes, enums, required keys, and the
   imported tier (an imported entry names its origin and its pending
   list; only a curated one has to carry a bench pack and a measured
   peak). `block.schema.json` -- the compose's `x-catena` block: its
   `schema`, the env shapes, the env key deny list (`DOCKER_*`, `PATH`,
   `HOME`, `LD_*`, `*_PROXY`), the quiesce timeout cap, argv shapes. Both run
   on every load, not just in CI.
2. Cross-file invariants in `lib/model.py` -- the compose file exists and its
   block declares an env, the logo and its `logo` field agree, no
   `vps.homepage.*` label.
3. `make lint` -- the block holds no `$` (outside a hooks body) and no
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
  data the seed itself wrote is never enough. A seed is safe to run twice: it
  gets or creates each record by the run id, and an assert counts exactly one.
  No step reads or writes a stand-in (a `catena_canary` table, a database
  server's own database). An app listed in `tests/app-tests-debt.txt` waits
  for its rewrite; its line goes in the commit whose test meets these rules.
- An app whose first run needs a person (a first administrator, a setup
  code, an installer) does it in `setup`, never in a seed: get-or-create,
  safe to run twice, no `{{ run_id }}`. Its only credential is
  `CATENA_TEST_PASSWORD`, given to the app in `setup` and signed in with in an
  assert; never make a password, never write one to the app's data, never put
  it on a command line. A shell-less app is reached through
  `{{ helper_<service> }}`, a one-time code through `stdin: service_log`, never
  through a change to the stack.
- An app that keeps live state a file snapshot can tear declares `quiesce`,
  one that migrates a schema declares `lifecycle` with `ready`, and one that
  keeps its files in a bucket the host can repoint declares `object_store`.
  `post` must succeed when `pre` never ran.
- Never weaken or remove a check, an assert or a leg to make an app pass:
  fix the app or its declaration.
- One app or none per branch, pull request and commit.
- A `blueprints/<id>/test.yml` change goes up as its own pull request and merges
  once app-tests passes; any other change of an app is one commit on main. Each
  main commit is judged by app-ci, and a failed commit gets no tag.

## Changing catalog.json

`app.schema.json` and `block.schema.json` are internal to this repo; the
CONTRACT with every reader of the catalog is `catalog.json`. A card or block
change that leaves `catalog.json` identical needs nothing else. A change to
`catalog.json` lands in the same merge window as its readers, since they
fetch `main`: ops `automation/helpers/templates_catalog.py` and
`automation/operator-tools/generate-sizing-doc.py`, and any other reader a
search of the workspace finds.

## What does NOT live here

- The on-box settling of an app's env and the minting of per-deploy app
  passwords. catenahq/catena-admin.
- On-box config key names: catenahq/catena-ce. The Keycloak client an
  app signs in with: catenahq/catena-admin's settings sync, from the
  app's sign-in labels.
- Per-VPS runtime state. All under `/var/lib/catena/` on each VPS.
- The client docs site. catenahq/docs is hand-written and reads nothing
  from this repo; a template's description and setup steps are in its `app.json`.

## Security invariants (machine-enforced -- do not weaken silently)

- No secrets, ever (gitleaks on every change; the catalog is public and
  fetched raw by every deployment).
- Generated artifacts are BUILD OUTPUTS; hand-edits fail the
  idempotent-render CI gate.
- Every catalog image ref is CVE-scanned (security.yml, through
  scanctl.yml's image pins);
  quiesce and lifecycle argv each pass their own allowlist in
  `lib/quiesce_lint.py`, with no shell operator.
- SPEC.md gate pointers must resolve (ops audit --check-public-specs).
