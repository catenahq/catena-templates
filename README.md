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
  importers/             # the registry importer: pipeline.py, plus one
                         # adapter per registry format

build/                   # thin entrypoints
  render.py  validate.py  lint_quiesce.py  lint_postgres_pins.py
  lint_swarm.py  import_registry.py  app_check.py  versions.py

sources.schema.json      # schema for a sources/<id>.json
block.schema.json        # schema for the x-catena block of a compose
Schema.json              # schema for the generated templates.json

templates.json           # generated; the Portainer App Templates index (v3)
catalog.json             # generated; the machine view catena-admin, ops/ and the bench read
index.html               # generated; static catalog preview

Makefile                 # make render / validate / lint / test / verify
Dockerfile               # nginx image serving the generated catalog
```

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
TURN_STATIC_AUTH_SECRET=__CATENA_OPERATOR_WIRED__
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
   field and the reason on failure.
2. `blueprints/<id>/docker-compose.yml` -- found by the directory name,
   so there is no path to keep in step with it. Its `x-catena` block
   starts with `schema: 2` and lists the app's `env`: each key is one of
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
3. `blueprints/<id>/logo.png` -- optional, square, 512x512. Without it
   the render emits a deterministic `logo.svg` placeholder.
4. `x-catena.sizing.peak_ram_mb` is required: the bench scheduler
   multiplies it by 1.15 to gate parallel slot acquisition. The other
   numbers stay null until a measured run.
5. If the application keeps live state that a file-level snapshot can
   tear and the backup's own Postgres and MariaDB dumps do not cover,
   add `quiesce` to the `x-catena` block at the top of the compose, with `service`, `pre`, `timeout_seconds` and
   optionally `user` and `post`. Each command is an argv array the
   nightly maintenance runs inside that service with `docker exec`, no
   shell; `post` must succeed when `pre` never ran. Real examples:
   Nextcloud (`occ maintenance:mode` on/off), Rocket.Chat (MongoDB
   fsyncLock/unlock), and Actual Budget and the webmail (`pkill -STOP` /
   `-CONT` on the server process). `make lint` holds each command to an
   allowlist of the application's own admin clients, plus `pkill` for an
   application with no admin client that can hold its writes.
6. If the entry works only on a Catena release newer than some servers
   run, `x-catena.requires_catena` names the oldest one it works on
   (`0.11.0`). The render opens the entry's Portainer description and its
   README with that release.
7. `make` -- renders, lints, and runs the tests. Commit the regenerated
   `blueprints/<id>/README.md`, `templates.json`, `catalog.json` and
   `index.html` with the source change.
8. Open a PR. CI runs `build-and-verify.yml`, `app-ci.yml` (see "Checks on
   every pull request"), `check:unicode` and `check:prose`.

A merge to `main` is live for every new deploy as soon as the consumers
next fetch the catalog; there are no releases to cut. An app already
deployed keeps the block it was deployed with.

## Checks on every pull request

`.github/workflows/app-ci.yml` runs on GitHub-hosted runners for every pull
request, forks included (no secret, a read-only token). The bench lifecycle
runs later and elsewhere. One job covers the catalog:

- `make` (render, lint, tests) and `catena-applint --catalog --nodes 2` over
  `blueprints/`, run from the public catena-admin image.

Then one job per app the change touches (`blueprints/<id>/**` or
`sources/<id>.json`), each running:

- `docker stack config` on the app's compose file.
- The licence rule: a new app names a licence from the importer's list
  (`x-catena.origin.licence`, the `sources.schema.json` enum); an app already
  in the catalog may name none, and one it names must be on the list.
- scanctl on the app's images: any vulnerability at high or above that has a
  fixed version fails, introduced by the change or not.
- Provenance (`lib/provenance.py`): each image's `org.opencontainers.image.source`
  repository on GitHub is at least a year old and has three releases (the
  GitHub API, with the run's `GITHUB_TOKEN`). An image with no GitHub source
  that lives on Docker Hub passes on its Docker Hub repository: registered at
  least a year ago, three pushed tags. Any other image fails naming itself,
  because GHCR and the other registries expose no dates.
- ClamAV over each image's exported files (`docker create`, `docker export`),
  with the databases the pinned `clamav/clamav` feature tag ships.

App ids come from changed file names, so the workflow hands them to scripts
through `env:` only and refuses an id that is not a lowercase slug.
`uv run build/app_check.py` runs the per-app checks locally (`images ID`,
`licence ID [--new]`, `provenance ID` after pulling the images).

## Versions

No file holds a version string: versions are git tags. A tested app is tagged
`<app>/<version>-<revision>` (`nextcloud-s3-oidc/31.0.8-1`).

- **Version.** The numeric part of the image tag of the app's main address
  service: the service `x-catena.domain.service` names in the source, or the
  compose block's `version_from` service when it has one (`34.0.5-apache` is
  `34.0.5`).
- **Revision.** 1 plus the number of tags the app already has at that version,
  so a change that leaves the image alone still gets a new tag. Tags order by
  version, then revision, as numbers.
- **When.** `app-tag.yml` runs on the `app-tests` commit status the bench
  lifecycle posts. A success on a commit `main` contains tags each app that
  commit changed, through the API with the CI GitHub App's token (secrets
  `RENOVATE_APP_CLIENT_ID` and `RENOVATE_APP_PRIVATE_KEY`). A tag ruleset
  (catenahq/renovate `scripts/apply-rulesets.sh`) lets only that App create,
  move or delete a `*/*` tag.
- **Scope.** The `scope` check fails a pull request that changes more than one
  app (distinct ids across `blueprints/<id>/**` and `sources/<id>.json`), so
  one merge is one tag. `app-checks`, `scope` and `app-tests` are required on
  `main`.

`uv run build/versions.py next <app>` prints the next tag from the existing tags
on stdin; `uv run build/versions.py scope` checks changed paths on stdin.

## Import from a registry

`build/import_registry.py` writes entries from a third-party template
registry, one adapter per format (`lib/importers/`; `--format` picks it,
`--help` lists them): Portainer template lists, versions 2 and 3, and a
registry holding one directory per app with a compose file, a
`template.toml` and a `meta.json`, read at one commit through GitHub's
file listing:

```
uv run build/import_registry.py --applint /path/to/catena-applint [--dry-run] \
    https://raw.githubusercontent.com/<owner>/<repo>/<ref>/templates.json
uv run build/import_registry.py --format <format> [--dry-run] \
    'https://api.github.com/repos/<owner>/<repo>/git/trees/<commit>?recursive=1'
```

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
  `GITHUB_TOKEN` lifts GitHub's rate limit.
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
  host cannot mint are recorded as choices.
- **Settings.** Every variable the registry or the compose names is an
  env default; one named like a password, secret or key gets a minted
  value. A `template.toml` helper (`${password:32}` and the others) gets
  a minted value of the same length and alphabet, and env lines that
  share one generated value read it from one setting.
- **What it writes.** `sources/<id>.json` with `x-catena.status:
  imported`, the `origin` (registry, source, licence, attribution) and
  `pending` (the findings left on the compose, and what a person still
  chooses: version, access, host name, EN/FR text, sizing, bench pack,
  SSO, quiesce hooks, bind mounts, refused features), plus
  `blueprints/<id>/docker-compose.yml`, and the id at the end of the
  `_meta.json` order. An id already in the catalog is left alone.

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
