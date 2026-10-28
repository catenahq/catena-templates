# catenahq/catena-templates

Single source of truth for the Catena template catalog: one directory
per application holding its compose file and its README, one JSON file
per application holding its metadata and prose, and the build pipeline
that renders them into Portainer App Templates (`templates.json`, format
v3) and the machine catalog (`catalog.json`). The catalog seeds a new
install: catena-admin and the ops tooling read `catalog.json` from `main`,
fetched raw, and Portainer itself can point its App Templates URL at
`templates.json`. A merge reaches every new deploy; a deployed app keeps the
compose, and the `x-catena` block in it, that it was deployed with.

**What this repo promises and how that is enforced:** [SPEC.md](SPEC.md)
-- hand-written intent plus machine-checked invariants, each citing the
gate that holds it. The maintainers' CI fails on a pointer that does not
resolve.

## Layout

```
blueprints/              # ONE directory per template -- what Portainer clones
  <id>/
    docker-compose.yml   # HAND-EDITED; the type-2 stackfile, whose root
                         # x-catena block declares the app's backup mode,
                         # update and restore commands, object-store check
                         # and managed env expressions
    test.yml             # HAND-EDITED; optional; the state the CI harness
                         # seeds after a deploy and checks after a restore
    logo.png             # HAND-PLACED; optional
    README.md            # generated from the source prose, EN then FR
    logo.svg             # generated placeholder, absent a logo.png

sources/                 # hand-edited metadata
  _meta.json             # template order + sizing measurement header
  <id>.json              # ONE file per template: the Portainer fields a human
                         # owns, plus x-catena for everything that format
                         # cannot carry (SSO mode, bench membership,
                         # sizing, EN/FR prose)

lib/                     # the build library
  model.py               # load + validate the hand-edited inputs
  render.py              # inputs -> every generated artifact
  readme.py              # one template's prose -> its README
  swarm_lint.py          # the swarm-stack-file compatibility gate
  quiesce_lint.py        # security lint over what a host acts on
                         # unattended: quiesce + lifecycle commands,
                         # sign-in labels
  postgres_pins.py       # each Postgres pin is one the update engine can order
  changes.py             # changed paths -> the app ids they touch
  provenance.py          # an image's project: age, releases (pull-request check)
  app_checks.py          # the per-app pull-request checks' commands
  versions.py            # app version tags and the one-app scope check
  adopt.py               # a fork's one-app pull request -> branch adopt/<pr>
  importers/             # the registry importer: pipeline.py, dedup.py,
                         # publish.py (waves), plus one adapter per registry
                         # format

build/                   # thin entrypoints
  render.py  validate.py  lint_quiesce.py  lint_postgres_pins.py
  lint_swarm.py  import_registry.py  app_check.py  versions.py  adopt.py

sources.schema.json      # schema for a sources/<id>.json
block.schema.json        # schema for the x-catena block of a compose
Schema.json              # schema for the generated templates.json

templates.json           # generated; the Portainer App Templates index (v3)
catalog.json             # generated; the machine view catena-admin, ops/ and the bench read
index.html               # generated; static catalog preview

Makefile                 # make render / validate / lint / test / verify
Dockerfile               # nginx image serving the generated catalog
```

The tools (`build/`, `lib/`) also run on another repository's tree of the same
layout: the environment variable `TEMPLATES_ROOT` names the tree holding its
`sources/`, `blueprints/` and generated files (default: this checkout), while the
schemas are always read from this checkout. The render writes into that tree and
`build/versions.py` reads its git tags.

Three things are human-edited: `sources/<id>.json`, the compose in
`blueprints/<id>/` with its `x-catena` block, and an optional logo beside
it. Everything else listed as generated is a committed build artifact; CI
re-renders on every PR and fails if the committed copies drift.

## Two consumer paths

### 1. Portainer App Templates (URL)

In any Portainer instance, point the App Templates URL at this repo's
raw templates.json:

```
https://raw.githubusercontent.com/catenahq/catena-templates/main/templates.json
```

Each entry is a type-2 (swarm git-repo) stack: Portainer clones this
repo and deploys `blueprints/<id>/docker-compose.yml` onto the host's
swarm.

### 2. The machine catalog (catalog.json)

catena-admin (`shell/marketplace`, `payload/engines/catalog`) and the
ops tooling read `catalog.json` -- one fetch,
every template, with the fields Portainer's format has no slot for (the
compose block's quiesce, lifecycle, object-store check and object-store switch included, lifted
from each compose). A
managed VPS reads it to render its OWN copy of `templates.json`, which is
what its Portainer actually serves (see the sentinel section below).

Why two artifacts rather than one: Portainer's format defines what it
defines. Squeezing `sso_mode`, quiesce hooks, bench packs, sizing and
bilingual prose into it would either be ignored by Portainer or break its
parser. `catalog.json` carries them beside the same ids.

## The env sentinel convention

Host-controlled env vars (TURN auth secret, admin password, ...) plus
every secret default in `templates.json` to a sentinel placeholder:

```
TURN_SECRET=__CATENA_OPERATOR_WIRED__
```

An app's own sign-in values (`OIDC_CLIENT_ID`, `OIDC_CLIENT_SECRET`,
`OIDC_ISSUER_URL`) are in neither file: the host's settings sync writes
them into the deployed stack's env from the Keycloak client it makes for
the sign-in labels on the service of the app's main address (its only
address, or the one marked `vps.route.main=true`), and the compose reads
them as `${OIDC_CLIENT_ID:-}`.

Portainer has no per-deploy secret generator and no template engine, so
three classes of value collapse to the sentinel: the keys of the compose
block's `managed` env keys, Ansible `lookup('password', ...)` expressions
(the block's `generated` env keys), and any value still carrying Jinja after
render.

**Nothing resolves the sentinel in THIS file, and nothing is meant to.**
This repository is public: one `templates.json` serves every client, so
it cannot hold a value that differs per host. A client's Portainer does
not read this copy. It reads its own host's, which catena-admin renders
from `catalog.json` with every expression resolved against that host --
its zone, its Keycloak, its store, and per-deploy passwords minted on the
box (`catena-admin shell/marketplace`). That render is why the values
have to be correct BEFORE the client clicks Deploy: an app that boots
against the wrong database password has already initialised its database
with it.

So deploying this file directly -- pointing a Portainer at the raw URL --
gives an app the literal sentinel as its secret. It is the render's
input, not a catalog to deploy from.

## How to add a template

1. `sources/<id>.json` -- copy the closest existing file and edit. The
   shape is enforced by `sources.schema.json`; the validator names the
   field and the reason on failure. Required: `id` (equal to the file
   name without `.json` and to the blueprint directory name), `type` (2),
   `title`, `name`, `categories`, `platform`, and the `x-catena` block
   (`app_name`, `upstream_url`, `sso_mode`, `domain` (absent for an app
   with no web address), `bench.pack`, `sizing.peak_ram_mb`, `en`, `fr`).
2. `blueprints/<id>/docker-compose.yml` -- found by the directory name,
   so there is no path to keep in step with it. Jinja stays in place: the
   render never writes into this file. Its root `x-catena` block, written by
   hand, starts with `schema: 2` and lists the app's `env`: each key is one of
   a `setting` (`default`, and a `label` in `en` and `fr`), a `generated`
   secret (`length`, `alphabet` of `alnum` or `hex`) or a `managed`
   expression (`expr`). The deploy form shows the keys in that order.
   An application that other applications use adds `provides`, naming per
   capability the env keys that hold what it gives (`provides: {antivirus:
   {host: CLAMD_HOST, port: CLAMD_PORT}}`); a user reads them in a managed env
   expression as `{{ provided.antivirus.host }}`. The host settles the user's
   `.env` from the one running application that provides the capability and
   redeploys the user when the provider is deployed, stopped or removed.
   An application with no web address leaves `domain` out of its source and
   names `version_from` in its block.
   An application that offers access levels to invited people adds `access`
   (`access: {user: {label: {en: "Nextcloud", fr: "Nextcloud"}}}`): the panel
   shows each label, and Catena admits the level's members at the application's
   sign-in.
3. `blueprints/<id>/logo.png` -- optional, square, 512x512, at most 100 KB.
   Without it the render emits a deterministic `logo.svg` placeholder.
4. `x-catena.sizing.peak_ram_mb` is required: the bench scheduler
   multiplies it by 1.15 to gate parallel slot acquisition. The other
   numbers stay null until a measured run.
5. If the application keeps live state that a file-level snapshot can
   tear and the backup's own Postgres and MariaDB dumps do not cover,
   add `quiesce` to the `x-catena` block at the top of the compose, with `service`, `pre`, `timeout_seconds` and
   optionally `user` and `post`. Each command is an argv array the
   nightly maintenance runs inside that service with `docker exec`, no
   shell. `post` also runs after a failed or resumed backup and after a
   restore, so it must succeed when `pre` never ran. Real examples:
   Nextcloud (`occ maintenance:mode` on/off), Rocket.Chat (MongoDB
   fsyncLock/unlock), and Actual Budget and the webmail (`pkill -STOP` /
   `-CONT` on the server process). `make lint` holds each command to an
   allowlist of the application's own admin clients, plus `pkill` for an
   application with no admin client that can hold its writes.
6. If the application keeps a schema its new code expects migrated, add
   `lifecycle` to the block. `migrate` runs after every update the host
   applies to the image of the service `lifecycle` names, and after a forward
   restore (a start-time migration runs against the pre-replay database, which
   the replay then overwrites). `before_update` and `after_update` wrap an
   update, for an application with a maintenance mode. `ready` is one command
   the update lane runs before each phase, and a forward restore before its
   migration, until it exits 0; every lifecycle declares it (the
   application's own status command, or a probe of the port its server opens
   last), and `make lint` fails one without it. Commands are argv arrays:
   `docker exec` gives them no shell. `versioned_volumes` names the volumes
   the new version of that service upgrades in place and the old one refuses
   to start on or misreads (code tree, configuration, add-ons, or a SQLite
   database; never a volume of the users' files the old one still reads): the
   lane copies them before an update, in the backup mode when the entry
   declares one, and puts the copies back when it rolls back, losing what was
   written since, as a database dump's replay does. An application that
   upgrades such a volume as it starts declares `service`,
   `versioned_volumes` and `timeout_seconds` alone. Real examples: the
   compose blocks of `blueprints/outline`, `blueprints/nextcloud-s3-oidc`,
   `blueprints/erpnext` and `blueprints/actualbudget`.
   An application that keeps its files in an object-store bucket the host can
   point at another bucket adds `object_store`: `env` names the stack env keys
   that hold the bucket, its access key and secret (and host, port, ssl and
   region when the template exposes them), and `switch` (`service`, `user`,
   argv `commands`, which may carry the placeholders `{old_bucket}` and
   `{new_bucket}`) runs once the app runs on the new bucket. `make lint`
   checks that the compose reads each key and that `switch` runs in one of its
   services. Real example: `blueprints/nextcloud-s3-oidc`.
7. If the entry works only on a Catena release newer than some servers
   run, `x-catena.requires_catena` names the oldest one it works on
   (`0.11.0`). The render opens the entry's Portainer description and its
   README with that release.
8. Declare the verify hook, `x-catena.hooks.verify`, which asks the running
   app whether it works: it makes the app's own server answer on the path users
   reach, checks each feature the app claims and its server provides, writes
   nothing, prints one line, exits 0 only when every check holds, and answers ok
   while a maintenance mode that a backup or an update holds is on. Its fields,
   when the server runs it and what a good check asks are in
   [Declare a verify check](https://docs.catena.run/en/configure-apps/#declare-a-verify-check-optional).
   Real examples: the hooks of `blueprints/erpnext` and
   `blueprints/nextcloud-s3-oidc`.
9. `blueprints/<id>/test.yml` -- the data the bench seeds and reads back. It
   holds `id` (the template id), `description`, and the lists `seed` and
   `assert`. Each step has a unique `name` and a `type`: `docker_exec` runs the
   argv `command` in `container` (`expect_exit_code`, default 0, and
   `expect_stdout_contains`, a substring of the output, `timeout_s`, default
   30), and `volume_file` writes (`content`) or reads (`expect_stdout_contains`)
   one `path` in the named `volume`, for an image with no shell. `{{ run_id }}`,
   `{{ app_name }}`, `{{ primary_container }}` and `{{ container_<service> }}`
   are filled in. The seed writes its data through the app itself (its API or
   command line) wherever the app allows it, so the restore proves data the
   app wrote. The asserts read that data back through the app and exercise it;
   an assert that reads only a file the seed wrote proves the volume, not the
   app. Real example: `blueprints/erpnext`.
10. `make` -- renders, lints, and runs the tests. Commit the regenerated
    `blueprints/<id>/README.md`, `templates.json`, `catalog.json` and
    `index.html` with the source change.
11. Open a PR from a branch that changes this one app and nothing else. CI
    runs `build-and-verify.yml`, `app-ci.yml` (see "Checks on every pull
    request"), `check:unicode` and `check:prose`; the bench then runs the legs
    below.

One app per branch, pull request and commit: the `scope` check fails a pull
request that touches two, and the bench judges a branch only when it changes one
app or none.

### What each app-tests leg proves

The bench runs these legs in this order in a test VM (see
"The bench lifecycle"); a leg that cannot run is a failed leg, not a skip.

1. `install` -- the app is added at the commit's tag from a repository holding
   its files.
2. `volumes` -- no container keeps a path on an anonymous volume, which a new
   container replaces.
3. `checks` -- the `seed` steps of `test.yml` run and the `assert` steps pass.
4. `verify` -- the verify hook answers ok. An app with no verify hook gets a
   skipped `verify` leg, and the run's summary names it.
5. `backup` -- a backup holds the seeded data.
6. `restore` -- the app is removed with its data, then restored from that backup
   alone: the asserts pass with no seed step, the app runs the commit's tag and
   the verify hook answers ok.
7. The update legs -- `update_newest` from the newest tag to the commit,
   `update_oldest` from the oldest tag of the last 12 months through the
   versions the block declares as stops, or `update_previous` for an app with no
   tag, from the image's previous upstream release. Each ends with the asserts
   and the verify hook.
8. `data_upgrade:<service>` -- for each service the block declares under
   `data`, its volume and the seeded data survive the update from the oldest
   version.
9. `rollback` -- a next revision that cannot start is deployed by the update
   engine and reverted: the service was redeployed, its image is back, the tag is
   quarantined, the data is as before and the verify hook answers ok.
10. `egress` -- the clone still drops a connection the app's declared `egress`
    does not allow.

A failed `install` skips every leg but `egress`; a failed `checks` skips `verify`, `backup`,
`restore` and `rollback`.

### Where to read a failed leg

The `app-tests` status on the commit links to the ops run; the job of the app
there logs one line per leg with its reason when it failed, and, when the bench
held any, the last lines of the evidence about it. On `main`, the failure also opens or rewrites
the issue `app-tests failing: <id>` of this repository, labelled `app:<id>`,
with the failing leg's last 200 lines; a branch's failures show on its pull
request only. See "The bench lifecycle".

A merge to `main` is live for every new deploy as soon as the consumers
next fetch the catalog; there are no releases to cut. An app already
deployed keeps the block it was deployed with.

## Checks on every pull request

`.github/workflows/app-ci.yml` runs on GitHub-hosted runners (the `runner`
input of its `workflow_call` sets another label) for every pull
request, forks included (no secret, a read-only token). The bench lifecycle
runs later and elsewhere. One job covers the catalog:

- `make` (render, lint, tests) and `catena-applint --catalog --nodes 2` over
  `blueprints/`, run from the public catena-admin image.

Then one job per app the change touches (`blueprints/<id>/**` or
`sources/<id>.json`), each running:

- `docker stack config` on the app's compose file.
- The licence rule: a new imported entry (`x-catena.status: imported`) names a
  licence from the importer's list (`x-catena.origin.licence`, the
  `sources.schema.json` enum), the licence of the registry it was copied from; a
  hand-written app may name none, and any licence an entry names must be on the
  list.
- scanctl on the app's images: any vulnerability at high or above that has a
  fixed version fails, introduced by the change or not.
- Provenance (`lib/provenance.py`): each image's `org.opencontainers.image.source`
  repository on GitHub is at least a year old and has three releases (the
  GitHub API, with the run's `GITHUB_TOKEN`). An image on Docker Hub passes on
  its Docker Hub repository when it has no GitHub source, or when that
  repository is under a year old or has too few releases: registered at least
  a year ago, three pushed tags. A GitHub repository that does not exist
  fails, and any other image fails naming itself, because GHCR and the other
  registries expose no dates. An image listed in `PROVENANCE_OWN_IMAGES`
  (comma-separated repositories, no tag) is the repository's own build and
  passes.
- ClamAV over each image's exported files (`docker create`, `docker export`),
  with the databases the pinned `clamav/clamav` feature tag ships.

`app-ci.yml`, `build-and-verify.yml`, `app-dispatch.yml` and `app-tag.yml` are
also reusable workflows (`workflow_call`) for another repository with this
layout. Their inputs are `catalog` (default true), `tools-ref` (default `main`)
and `own-images` (default empty); `app-dispatch.yml` and `app-tag.yml` take the
secrets `RENOVATE_APP_CLIENT_ID` and `RENOVATE_APP_PRIVATE_KEY`. With `catalog`
false a called workflow checks out catena-templates at `tools-ref` into
`.tools` and sets `TEMPLATES_ROOT` to the caller's tree. It runs the swarm lint
and `catena-applint --nodes 2 blueprints` (no `--catalog`) instead of `make`,
the caller's `tests/`, render drift over `blueprints` alone, and no jscpd;
`own-images` reaches the provenance step as `PROVENANCE_OWN_IMAGES`. With
`catalog` true every step runs as written above, under the names `app-checks`
and `scope`.

App ids come from changed file names, so the workflow hands them to scripts
through `env:` only and refuses an id that is not a lowercase slug.
`uv run build/app_check.py` runs the per-app checks locally (`images ID`,
`licence ID [--new]`, `provenance ID` after pulling the images).

## The bench lifecycle

After the cheap checks, the lifecycle of each app runs on the Catena bench (a
private runner this repository cannot reach):

- `app-dispatch.yml` runs on a push to any branch, `adopt/*` included. It sends
  the private ops repository the event `app-test` with the commit, the commit
  the change is judged from, the apps the change touches, the branch and the
  name of the repository the event comes from (`catena-templates` here; another
  repository calls this workflow too). On `main`, each commit of the push is sent on its own, judged from its parent. A
  branch other than `main` is judged as a whole, from where it left `main`, and
  is sent only when it changes one app or none. The repository variable
  `APP_CI_PAUSED=true` stops it.
- A commit of one app whose change already has an `app-tests` verdict carries
  it: the event holds `carry` (the address of the ops run) and `carry_state`,
  and ops posts that verdict on the commit without a run. The dispatch looks
  at the commit itself, then, on `main`, at the head of the merged pull
  request, on another branch at the head the branch had before the push, and
  then at the run that made the app's newest tag. A candidate counts when the
  CI GitHub App posted its verdict, and the app's files, and the files its
  change is judged from, are the same (`build/versions.py same`). A failure is
  carried only on a branch other than `main`. A carried pass on `main` is
  tagged with the address of the run that tested the change.
- ops runs install, checks, backup, restore, rollback, updates and the outbound
  check, then posts the commit status `app-tests` on the commit (success,
  failure, or error when the bench gave no verdict). A commit its branch has
  moved past by the time a runner takes it is skipped. For a commit of `main`,
  ops sends back the event `app-passed` with the apps that passed, and
  `app-tag.yml` tags them (see "Versions").
- `adopt-fork.yml` serves pull requests from forks, which the bench never sees
  directly. When `app-ci` has passed a fork's pull request, it copies the fork's
  files for exactly one app (`blueprints/<id>/**` and `sources/<id>.json`) onto
  the branch `adopt/<pr>` of this repository through the API, as data: nothing
  of the fork is checked out or run with a secret. A change that touches more
  than one app, a file outside an app, a link or a file over 1 MB is refused.
  The generated catalog files are not copied; a maintainer renders them on the
  branch. `adopt/<pr>` is dispatched like any branch, and its `app-tests` status
  is what a maintainer reads before merging the pull request.

Both new workflows use the CI GitHub App's secrets (`RENOVATE_APP_CLIENT_ID`,
`RENOVATE_APP_PRIVATE_KEY`). The path end to end, the secrets and how to pause it
are in ops `internal_docs/tools/app-ci.md`.

## Versions

No file holds a version string: versions are git tags. A tested app is tagged
`<app>/<version>-<revision>` (`nextcloud-s3-oidc/31.0.8-1`).

- **Version.** The numeric part of the image tag of the app's main address
  service: the service `x-catena.domain.service` names in the source, or the
  compose block's `version_from` service when it has one (`34.0.5-apache` is
  `34.0.5`).
- **Revision.** 1 plus the highest revision the app already has at that
  version, so a change that leaves the image alone still gets a new tag. Tags order by
  version, then revision, as numbers.
- **When.** `app-tag.yml` runs on the event `app-passed`, which the bench sends
  with the apps that passed on a commit of `main`. An app whose files
  (`blueprints/<id>/`, `sources/<id>.json`) differ from its newest tag, or that
  has none, gets a new tag on the commit, but only while `main` still holds the
  commit's files of the app: a pass of a commit whose app `main` has since
  changed tags nothing. An app whose newest tag was made by
  hand and holds the same files has that tag annotated where it stands. The
  tags are annotated `app-tests passed at <commit>: <run>` and written through
  the API with the CI GitHub App's token (secrets `RENOVATE_APP_CLIENT_ID` and
  `RENOVATE_APP_PRIVATE_KEY`). A tag ruleset (catenahq/renovate
  `scripts/apply-rulesets.sh`) lets only that App create, move or delete a
  `*/*` tag.
- **Tested.** An app is `tested` when a passing run made its newest tag and
  that tag holds the files `main` holds, `changed` when the files differ from
  its newest tag, `untested` when it has no tag or its newest was made by hand.
  The nightly bench run takes the apps that are not `tested` first.
- **Scope.** The `scope` check fails a pull request that changes more than one
  app (distinct ids across `blueprints/<id>/**` and `sources/<id>.json`), so
  one merge is one tag; the `one-app` pre-commit hook does the same for a
  commit. `app-checks`, `scope` and `app-tests` are required on `main`.

`uv run build/versions.py tested` lists each app's state and newest tag
(`--needs-run`: the ids of the apps not `tested`); `uv run build/versions.py
plan <app>` prints what a pass does to the app's tags;
`uv run build/versions.py scope` checks changed paths on stdin;
`uv run build/versions.py same <app> --ref <sha> --base <sha> --from <sha>`
exits 0 when the `--from` commit holds the change `--ref` holds, judged from
`--base`: the app's files are the same at both commits, and the same at the
merge base of `--from` and `--base` and at `--base`;
`uv run build/versions.py tested-run <app> [--ref <rev>]` prints the address of
the run that made the app's newest tag when the app is `tested` there. With
`TEMPLATES_ROOT` set, the tags and files read are those of that tree's git
repository.

## Import from a registry

`build/import_registry.py` writes entries from a third-party template
registry, one adapter per format (`lib/importers/`; `--format` picks it,
`--help` lists them):

| `--format` | Source | Read at |
|---|---|---|
| `portainer` | Portainer template lists, versions 2 and 3: Lissy93/portainer-templates (`templates.json`), Portainer's own (`templates-2.0.json`) | the list's address; each stack entry's repository at the commit its default branch is at |
| `arcane` | Arcane's `registry.json`; each template's compose file and `.env.example` come from getarcaneapp/templates | that repository's default-branch commit |
| the per-app directory format (`--help` names it) | a registry with one directory per app holding a compose file, a `template.toml` and a `meta.json` | the commit in GitHub's file listing address |
| `awesome-selfhosted` | awesome-selfhosted-data: one `software/<name>.yml` per project | the commit in GitHub's file listing address |

```
uv run build/import_registry.py --applint /path/to/catena-applint [--dry-run] [--entry TITLE ...] \
    https://raw.githubusercontent.com/<owner>/<repo>/<commit>/templates.json
uv run build/import_registry.py --format <format> [--dry-run] \
    'https://api.github.com/repos/<owner>/<repo>/git/trees/<commit>?recursive=1'
uv run build/import_registry.py --wave 5 --also arcane=<registry.json address> <address>
```

- **Entries.** `--entry TITLE`, repeatable, reads only the entries of that
  title (case ignored) from every registry of the run; titles of one app
  named together give one app and its aliases. A title no registry holds
  stops the run before anything is written.
- **Names.** An entry whose title or id holds a word on the banned-word
  list `check:unicode` reads (`contracts/banned-words.json`, in the
  sibling checkout) is skipped, and the import stops when that list
  cannot be read.
- **Licence.** Read from GitHub's licence API for the repository the
  compose file is copied from (the registry's own repository for a
  single-container entry), or, for a per-app directory, the app's own
  repository its `meta.json` links; that registry's own licence must be
  permitted too, and its notice joins the attribution. The app's own
  repository is read on its forge: GitHub (a `/tree/<ref>/<path>` link
  reads as its repository), GitLab through its project API, or Gitea
  through its repository API. Forgejo, Codeberg's forge, shares Gitea's
  API, and its answer names no licence, so an app there reads as licence
  unknown. Any host other than github.com, gitlab.com and codeberg.org
  is asked through Gitea's API, then GitLab's. A link
  to an owner's page on one of those three reads as the owner's
  repository named as the entry's id, which the entry asks a person to
  confirm; with no such repository the licence is unknown. Only MIT,
  Apache-2.0 and BSD-2/3-Clause sources are imported; any other licence,
  or one the forge cannot name, skips the entry and the report lists it.
  `GITHUB_TOKEN` lifts GitHub's rate limit. When the registry or a listing
  names the app's upstream repository (a per-app `meta.json` link, or an
  awesome-selfhosted project of the entry's name, whose list is read for the
  repository only), that repository is the upstream project and its address
  is recorded as `origin.project`. Otherwise the importer looks for it from
  the main image: the image's `org.opencontainers.image.source` label, read
  from its registry without pulling it, then the source linked on its Docker
  Hub repository (`lib/importers/upstream.py`). An entry whose upstream
  repository is found nowhere is skipped as licence unknown. An upstream
  project other than the repository the compose file is copied from may
  carry those licences or GPL, LGPL or AGPL: its licence covers the app the
  image runs, and the catalog copies none of its files. An app whose only
  compose file sits in a GPL or AGPL repository is written by hand instead
  ("How to add a template").
- **Compose.** A stack entry's file is fetched from its repository; a
  single-container entry gets a one-service file from its image, ports,
  volumes, env and labels. The address the registry routes (the first
  `[[config.domains]]`), or else the first published TCP port, becomes
  `vps.route.host`/`vps.route.port`, Traefik labels go, and the app
  checker's `--fix` (catena-admin `payload/cmd/catena-applint`, named by
  `--applint` or `CATENA_APPLINT`) corrects the rest. Without the
  checker the import warns and records that it did not run. `make lint`
  fails on that record and on each error the checker leaves, until a
  person settles it. A further
  address, a file the registry writes beside the stack and a value the
  host cannot mint are recorded as choices. A file fetched from a
  repository is read at the commit its default branch is at, recorded in
  `origin.commit` with its `origin.path`; an entry whose repository cannot be
  pinned is skipped.
- **Duplicates.** One catalog app per upstream project. An entry is keyed by
  the repository of its main image (tag, digest and Docker Hub's spelling
  removed) and by its upstream repository when known. Entries that share a
  key are one app, and so is an entry that shares a key with an app already
  in the catalog. The richest of a group (most settings, plus a health check,
  plus sign-in labels) seeds the new app and the others are aliases:
  recorded in its `origin.aliases`, not imported. An alias of an app already
  in the catalog is recorded in `sources/_aliases.json`, which a later run
  reads so it weighs the entry no more. `--also FORMAT=ADDRESS` reads a
  further registry in the same run.
- **Waves.** `--wave N` imports up to N new apps, each on its own branch
  `import/<id>` with its own pull request (one app per pull request, so the
  `scope` check holds), rendering the catalog files into the commit; apps
  the checker or the licence rule refuses are skipped with the reason, and the
  rest wait for a later wave. Run it in a clean checkout of the base branch
  with `git` and an authenticated `gh`. The aliases a wave records go in
  one pull request of their own, `import/aliases`. A wave imports nothing
  while an update or vulnerability fix pull request is open (branches
  `renovate/`, `engine-bump/`, `scanctl/`), and otherwise at most N, the
  day's remainder of `--daily-limit` (5) and the room under `--open-limit`
  (10 open import pull requests). `--dry-run` with `--wave` reads the pull
  request counts, prints the plan and writes nothing.
- **Settings.** Every variable the registry or the compose names is an
  env default; one named like a password, secret or key gets a minted
  value. A `template.toml` helper (`${password:32}` and the others) gets
  a minted value of the same length and alphabet, and env lines that
  share one generated value read it from one setting.
- **What it writes.** `sources/<id>.json` with `x-catena.status:
  imported`, the `origin` (registry, entry, source, licence, attribution,
  and when known the commit, path, upstream project and aliases) and
  `pending` (the findings left on the compose, and what a person still
  chooses: version, access, host name, EN/FR text, sizing, bench pack,
  SSO, quiesce hooks, bind mounts, refused features), plus
  `blueprints/<id>/docker-compose.yml`. An imported entry the `_meta.json`
  order does not list follows the listed ones, by id; finishing it means
  placing it in the order. An id already in the catalog is left alone.

An imported entry renders with stubs (no bench pack, no measured peak,
placeholder prose) and the published catalog marks it untested:
`status` and `origin` in `catalog.json`, "Imported, untested" in the
Portainer description and note, the README and `index.html`. Finishing
it means filling the stubs, settling `pending`, and deleting both
`pending` and `status`; `origin` stays.

## What does NOT live here

- Vault values, OIDC client secrets, any actual secret. Sentinels only.
- The per-host render that resolves the sentinels, and the on-box minting
  of per-deploy app passwords. Lives in catenahq/catena-admin
  (`shell/marketplace`), served to that host's Portainer.
- The Keycloak clients apps sign in with: an app's own client comes from
  its sign-in labels through catenahq/catena-admin's settings sync
  (`payload/lib/oidc_clients.py`), the mail server's from
  catenahq/catena-ce's realm templates.
- The client docs site, catenahq/docs. It is hand-written and reads
  nothing from here; a template's own documentation is the README in its
  blueprint directory.
- Per-VPS state (installed templates, CVE queue, SBOM). Lives at
  `/var/lib/catena/` on each managed VPS.

## Why this is its own repo

Portainer's App Templates BASE URL field takes a public URL and clones
the repository behind it, so the catalog has to be a public repository
of its own. The per-template JSON layout matches the Portainer App
Templates format documentation and the community layout used by
Lissy93/portainer-templates.
