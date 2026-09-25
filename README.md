# catenahq/catena-templates

Single source of truth for the Catena template catalog: one directory
per application holding its compose file and its README, one JSON file
per application holding its metadata and prose, and the build pipeline
that renders them into Portainer App Templates (`templates.json`, format
v3) and the machine catalog (`catalog.json`). Every consumer reads `main`,
fetched raw: catena-admin, catena-ce and the ops tooling read
`catalog.json`, and Portainer itself can point its App Templates URL at
`templates.json`.

**What this repo promises and how that is enforced:** [SPEC.md](SPEC.md)
-- hand-written intent plus machine-checked invariants, each citing the
gate that holds it. The maintainers' CI fails on a pointer that does not
resolve.

## Layout

```
blueprints/              # ONE directory per template -- what Portainer clones
  <id>/
    docker-compose.yml   # HAND-EDITED; the type-2 stackfile
    logo.png             # HAND-PLACED; optional
    README.md            # generated from the source prose, EN then FR
    logo.svg             # generated placeholder, absent a logo.png
    quiesce.yml          # generated, if the entry declares backup hooks

sources/                 # hand-edited metadata
  _meta.json             # postgres_default_image + sizing measurement header
  <id>.json              # ONE file per template: the Portainer fields a human
                         # owns, plus x-catena for everything that format
                         # cannot carry (SSO mode, quiesce hooks,
                         # post-restore migrations, bench membership,
                         # sizing, EN/FR prose)

lib/                     # the build library
  model.py               # load + validate the hand-edited inputs
  render.py              # inputs -> every generated artifact
  readme.py              # one template's prose -> its README
  swarm_lint.py          # the swarm-stack-file compatibility gate
  quiesce_lint.py        # security lint over the hooks a host runs
                         # unattended: quiesce + post-restore migrations
  postgres_pins.py       # central Postgres image enforcement
  images.py              # the pinned images, and the ones a change moves

build/                   # thin entrypoints
  render.py  validate.py  lint_quiesce.py  lint_postgres_pins.py
  lint_swarm.py  list_images.py  changed_images.py

sources.schema.json      # schema for a sources/<id>.json
Schema.json              # schema for the generated templates.json

templates.json           # generated; the Portainer App Templates index (v3)
catalog.json             # generated; the machine view ops/ + the bench read
index.html               # generated; static catalog preview

Makefile                 # make render / validate / lint / test / verify
Dockerfile               # nginx image serving the generated catalog
```

Three things are human-edited: `sources/<id>.json`, the compose in
`blueprints/<id>/`, and an optional logo beside it. Everything else
listed as generated is a committed build artifact; CI re-renders on
every PR and fails if the committed copies drift.

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

catena-admin (`shell/marketplace`, `payload/engines/catalog`), the
catena-ce converge and the ops tooling read `catalog.json` -- one fetch,
every template, with the fields Portainer's format has no slot for. A
managed VPS reads it to render its OWN copy of `templates.json`, which is
what its Portainer actually serves (see the sentinel section below).

Why two artifacts rather than one: Portainer's format defines what it
defines. Squeezing `sso_mode`, quiesce hooks, bench packs, sizing and
bilingual prose into it would either be ignored by Portainer or break its
parser. `catalog.json` carries them beside the same ids.

## The env sentinel convention

Operator-controlled env vars (OIDC client id/secret, TURN auth secret,
discovery URL, ...) plus every secret default in `templates.json` to a
sentinel placeholder:

```
OIDC_CLIENT_SECRET=__CATENA_OPERATOR_WIRED__
```

Portainer has no per-deploy secret generator and no template engine, so
three classes of value collapse to the sentinel: declared
`env_managed_keys`, Ansible `lookup('password', ...)` expressions, and
any value still carrying Jinja after render.

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
   so there is no path to keep in step with it.
3. `blueprints/<id>/logo.png` -- optional, square, 512x512. Without it
   the render emits a deterministic `logo.svg` placeholder.
4. `x-catena.sizing.peak_ram_mb` is required: the bench scheduler
   multiplies it by 1.15 to gate parallel slot acquisition. The other
   numbers stay null until a measured run.
5. If the template takes write traffic during backup (DB writes,
   append-only filesystem state, queue consumption), add
   `x-catena.quiesce` with `pre`, `post` and `timeout_seconds`. Real
   examples: Nextcloud (`occ maintenance:mode` on/off) and Rocket.Chat
   (mongo fsyncLock/unlock). `make lint` enforces snippet safety: no
   curl/wget, no rm outside the app's data path.
6. `make` -- renders, lints, and runs the tests. Commit the regenerated
   `blueprints/<id>/README.md`, `templates.json`, `catalog.json` and
   `index.html` with the source change.
7. Open a PR. CI runs `build-and-verify.yml`, `check:unicode` and `check:prose`.

A merge to `main` is live for every new deploy as soon as the consumers
next fetch the catalog; there are no releases to cut.

## What does NOT live here

- Vault values, OIDC client secrets, any actual secret. Sentinels only.
- The per-host render that resolves the sentinels, and the on-box minting
  of per-deploy app passwords. Lives in catenahq/catena-admin
  (`shell/marketplace`), served to that host's Portainer.
- Operator-side wiring (how OIDC clients get minted). Lives in
  catenahq/ops and catenahq/catena-ce.
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
