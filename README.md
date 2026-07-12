# catenahq/catena-templates

Single source of truth for the Catena template catalog: the compose files
+ per-template metadata + build pipeline that emits Portainer App Templates
(templates.json v3). Consumed by catenahq/ops (via a vendored tarball,
contracts-pattern) and by Portainer itself (its App Templates URL field
pointing at this repo's raw templates.json).

**What this repo promises and how that is enforced:** [SPEC.md](SPEC.md)
(hand-written intent + machine-checked invariants) and
[VALIDATION.md](VALIDATION.md) (generated CI-gate sheet; drift fails the
maintainers' CI).

## Layout

```
source/                  # canonical input, Jinja-templated
  catalog.yml            # per-template metadata (slugs, domains, env vars, prose)
  sizing-data.yml        # per-template idle/peak RAM, CPU, disk + bilingual notes;
                         # canonical input for sales/docs sizing pages AND for the
                         # catena-ops bench parallel scheduler (ram_budget reads
                         # peak_ram_mb + applies a fixed 1.15x safety margin)
  compose/<id>.compose.yml
  assets/<id>/logo.png

blueprints/              # generated; the type-3 stackfiles Portainer clones
  <id>/
    docker-compose.yml   # the compose Portainer deploys as an App Template stack
    logo.svg             # placeholder or copied source asset
    quiesce.yml          # if the entry declares backup-quiesce hooks

templates.json           # generated; the Portainer App Templates index (v3)

build/
  render.py              # source/ -> blueprints/ + templates.json
  serve.py               # local preview server

.github/workflows/
  build-and-verify.yml   # CI: run render.py, fail if outputs drift from source/
```

`source/` is human-edited. `blueprints/` and `templates.json` are committed
build artifacts; CI verifies they stay in sync with `source/` on every
PR.

## Two consumer paths

### 1. Portainer App Templates (URL)

In any Portainer instance, point the App Templates URL at this repo's
raw templates.json:

```
https://raw.githubusercontent.com/catenahq/catena-templates/main/templates.json
```

Pin a release tag to freeze the catalog:

```
https://raw.githubusercontent.com/catenahq/catena-templates/tags/v0.1.0/templates.json
```

Each entry is a type-3 (compose git-repo) stack: Portainer clones this repo
and deploys `blueprints/<id>/docker-compose.yml`.

### 2. ops/ Ansible reconciliation (vendored)

catenahq/ops consumes a tagged tarball of `source/` and reconciles each
managed VPS at converge time (env + routing). The converge owns
`env_managed_keys` drift-healing (operator state re-injected on every
converge); the marketplace UI path does not.

## The env_managed_keys sentinel convention

Operator-controlled env vars (OIDC client id/secret, TURN auth secret,
discovery URL, ...) plus every secret (Portainer has no per-deploy
generator) default in templates.json to the sentinel placeholder:

```
OIDC_CLIENT_SECRET=__CATENA_OPERATOR_WIRED__
```

ops/ converge overwrites these post-deploy with real vault values.
Marketplace-deployed templates need an ops/ converge pass before they
are functional.

## How to add a template

1. Add an entry to `source/catalog.yml` (schema documented inline).
2. Add a matching entry to `source/sizing-data.yml` (id MUST match
   catalog id; peak_ram_mb is required, other fields nullable until a
   measurement run lands). `build/render.py` validates parity and fails
   the build if an id is in catalog but missing from sizing-data (or
   vice versa).
3. Add `source/compose/<id>.compose.yml`.
4. Add `source/assets/<id>/logo.png` (square, 512x512 PNG).
5. If the template has write traffic during backup (DB writes,
   append-only filesystem state, queue consumption), add
   `quiesce_pre` + `quiesce_post` + `quiesce_timeout_seconds` to
   the catalog entry. Real examples: Nextcloud at
   `source/catalog.yml:142-144` (occ maintenance:mode on/off) and
   Rocket.Chat at `:505-507` (mongo fsyncLock/unlock). Stateless or
   read-only templates can either omit these fields or set them to
   `"true"` placeholders with a justifying comment (see Synapse at
   `:614-616` for the placeholder pattern). `uv run
   build/lint_quiesce.py` enforces snippet safety (no curl/wget,
   no rm outside the app's data path).
6. Run `uv run build/render.py`. Commit the regenerated
   `blueprints/<id>/` + updated `meta.json`.
7. Open a PR. CI runs `build-and-verify.yml`.
8. After merge, tag a `vX.Y.Z` release.

## How to bump

Patch: env-default change, prose tweak. Minor: new template, new
env_managed_key. Major: catalog schema change (breaks the ops/ loader).

Tag a release: `git tag -a vX.Y.Z -m "..." && git push --tags`.
catenahq/ops's `Bump @catenahq/catena-templates to latest` workflow opens
a vendored-tarball-bump PR on its next daily run.

## What does NOT live here

- Vault values, OIDC client secrets, any actual secret. Sentinels only.
- Operator-side wiring (which env_managed_keys overwrite which vault
  refs, how OIDC clients get minted). Lives in catenahq/ops.
- Client-facing documentation. Lives in catenahq/docs (generated from
  `source/catalog.yml` by ops/automation/operator-tools/generate-template-docs.py).
- Per-VPS state (installed templates, CVE queue, SBOM). Lives at
  `/var/lib/catena/` on each managed VPS.

## Repo split status

Seventh repo in the catenahq split, lifted out of catenahq/ops on
2026-05-16. Predecessor location: the in-tree catalog under
`ops/automation/ansible/roles/infrastructure/vars/`
+ `ops/internal_docs/operator/client-app-templates/`. Lift driven by
the need to expose the catalog through the App Templates BASE URL field
and to ship the CVE-watcher template (catenahq/ops `BACKLOG_TECHNICAL.md` R2).
