# catenahq/catena-templates

Single source of truth for the Catena template catalog: one JSON file per
template plus its compose file, and the build pipeline that renders them
into Portainer App Templates (`templates.json`, format v3) and the
machine catalog (`catalog.json`). Consumed by catenahq/ops (via a
vendored tarball, contracts-pattern) and by Portainer itself (its App
Templates URL field pointing at this repo's raw `templates.json`).

**What this repo promises and how that is enforced:** [SPEC.md](SPEC.md)
(hand-written intent + machine-checked invariants) and
[VALIDATION.md](VALIDATION.md) (generated CI-gate sheet; drift fails the
maintainers' CI).

## Layout

```
sources/                 # canonical input, hand-edited
  _meta.json             # postgres_default_image + sizing measurement header
  <id>.json              # ONE file per template: the Portainer fields a human
                         # owns, plus x-catena for everything that format
                         # cannot carry (SSO mode, quiesce hooks,
                         # post-restore migrations, bench membership,
                         # sizing, EN/FR prose)
  compose/<id>.compose.yml
  assets/<id>/logo.png

lib/                     # the build library
  model.py               # load + validate sources/
  render.py              # sources/ -> every generated artifact
  quiesce_lint.py        # security lint over the hooks a host runs
                         # unattended: quiesce + post-restore migrations
  postgres_pins.py       # central Postgres image enforcement

build/                   # thin entrypoints
  render.py  validate.py  lint_quiesce.py  lint_postgres_pins.py

sources.schema.json      # schema for a sources/<id>.json
Schema.json              # schema for the generated templates.json

blueprints/              # generated; the type-2 stackfiles Portainer clones
  <id>/
    docker-compose.yml
    logo.svg             # placeholder or copied source asset
    quiesce.yml          # if the entry declares backup-quiesce hooks

templates.json           # generated; the Portainer App Templates index (v3)
catalog.json             # generated; the machine view ops/ + the bench read
index.html               # generated; static catalog preview

Makefile                 # make render / validate / lint / test / verify
Dockerfile               # nginx image serving the generated catalog
```

`sources/` is human-edited. Everything else listed as generated is a
committed build artifact; CI re-renders on every PR and fails if the
committed copies drift.

## Two consumer paths

### 1. Portainer App Templates (URL)

In any Portainer instance, point the App Templates URL at this repo's
raw templates.json:

```
https://raw.githubusercontent.com/catenahq/catena-templates/main/templates.json
```

Pin a release tag to freeze the catalog:

```
https://raw.githubusercontent.com/catenahq/catena-templates/tags/v0.2.0/templates.json
```

Each entry is a type-2 (swarm git-repo) stack: Portainer clones this
repo and deploys `blueprints/<id>/docker-compose.yml` onto the host's
swarm.

### 2. ops/ Ansible reconciliation (catalog.json)

catenahq/ops reads `catalog.json` -- one fetch, every template, with the
fields Portainer's format has no slot for. The converge reconciles each
managed VPS (env + routing) and owns `env_managed_keys` drift-healing;
the marketplace UI path does not.

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
any value still carrying Jinja after render. The converge overwrites
them post-deploy. Marketplace-deployed templates need a converge pass
before they are functional.

## How to add a template

1. `sources/<id>.json` -- copy the closest existing file and edit. The
   shape is enforced by `sources.schema.json`; the validator names the
   field and the reason on failure.
2. `sources/compose/<id>.compose.yml` -- referenced by
   `x-catena.compose_file`.
3. `sources/assets/<id>/logo.png` -- optional, square, 512x512. Without
   it the render emits a deterministic placeholder.
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
   `blueprints/<id>/`, `templates.json`, `catalog.json` and `index.html`
   with the source change.
7. Open a PR. CI runs `build-and-verify.yml` and `check:unicode`.
8. After merge, tag a `vX.Y.Z` release.

## How to bump

Patch: env-default change, prose tweak. Minor: new template, new
`env_managed_keys` entry. Major: a change to the source schema or to the
generated `catalog.json` shape (breaks the ops loader).

Tag a release: `git tag -a vX.Y.Z -m "..." && git push --tags`.
catenahq/ops's `Bump @catenahq/catena-templates to latest` workflow opens
a vendored-tarball-bump PR on its next daily run.

## What does NOT live here

- Vault values, OIDC client secrets, any actual secret. Sentinels only.
- Operator-side wiring (which `env_managed_keys` overwrite which values,
  how OIDC clients get minted). Lives in catenahq/ops.
- Client-facing documentation. Lives in catenahq/docs (generated from
  `catalog.json` by ops/automation/operator-tools/generate-template-docs.py).
- Per-VPS state (installed templates, CVE queue, SBOM). Lives at
  `/var/lib/catena/` on each managed VPS.

## Repo split status

Seventh repo in the catenahq split, lifted out of catenahq/ops on
2026-05-16. Lift driven by the need to expose the catalog through the
App Templates BASE URL field and to ship the CVE-watcher template
(catenahq/ops `BACKLOG_TECHNICAL.md` R2). The YAML catalog
(`source/catalog.yml` + `source/sizing-data.yml`) was split into
per-template JSON on 2026-08-03, aligning the repo with the Portainer
App Templates format documentation and the community layout used by
Lissy93/portainer-templates.
