# catenahq/templates -- Portainer App Template catalog

This repo holds the Catena Portainer App Template catalog. See README.md
for layout, consumer model, and BASE URL setup.

## Edit rules

- `source/` is canonical. `blueprints/` and `templates.json` are generated.
  Never hand-edit a file under `blueprints/` or `templates.json` -- the
  change is overwritten on the next `render.py` run, and CI rejects the PR.
- Every change is a deliberate version bump. Tag a `vX.Y.Z` release
  on every merge to main; catenahq/ops pulls via that tag.
- No emojis or em-dashes in any artifact. Plain hyphens + straight
  quotes only. `npm run check:unicode` enforces.
- Bilingual prose (the `en` / `fr` blocks per catalog entry): both
  required, no EN-only or FR-only templates.
- No secrets, ever. Sentinel placeholders (`__CATENA_OPERATOR_WIRED__`)
  in templates.json for env vars that ops/ converge owns. Portainer App
  Templates have no per-deploy secret generator, so ALL secrets default
  to the sentinel and ops/ converge re-injects them.

## The render contract

`build/render.py` transforms `source/catalog.yml` + `source/sizing-data.yml`
+ `source/compose/` to `blueprints/` + `templates.json` (Portainer App
Templates v3). `sizing-data.yml` is validated for catalog parity (every
catalog id must have a matching sizing entry with a positive int
`peak_ram_mb`; orphan sizing entries fail the build too) but is NOT emitted
to `blueprints/` -- it is consumed downstream by ops/ (bench scheduler +
generate-sizing-doc.py), not by Portainer itself.

- The compose file is copied verbatim into `blueprints/<id>/docker-compose.yml`
  (the Portainer type-3 stackfile). Jinja stays in place; ops/ converge
  reconciles env + routing post-deploy (catena is operator-managed).
- `templates.json` holds one Portainer App Template per catalog entry:
  type 3, title/description/note/categories/logo from catalog metadata,
  `repository{url, stackfile}` -> `blueprints/<id>/`, and `env`
  `[{name,label,default}]` from env_defaults. Portainer has NO per-deploy
  secret generator, so secret + `env_managed_keys` default to the sentinel
  (`__CATENA_OPERATOR_WIRED__`) that ops/ re-injects.

The render must be idempotent: running `render.py` twice produces
byte-identical outputs. CI verifies this with `git diff --exit-code`
after running render.

## Add a new template

Checklist:

1. `source/catalog.yml` entry with all required fields (id, app_name,
   upstream_url, sso_mode, domain_host, domain_port, env_defaults,
   en, fr).
2. Matching `source/sizing-data.yml` entry (same id). `peak_ram_mb`
   is required (positive int); other RAM/CPU/disk fields nullable
   until a real measurement run lands. Render.py validates parity
   and fails the build on a missing or orphan id.
3. `source/compose/<id>.compose.yml` (existing Jinja-templated form
   is fine -- render strips it).
4. `source/assets/<id>/logo.png` (512x512 PNG, max 100KB).
5. If the template has write traffic during backup (DB writes,
   append-only filesystem state, queue consumption), add
   `quiesce_pre` + `quiesce_post` + `quiesce_timeout_seconds` to
   the catalog entry. Real examples: Nextcloud at
   `source/catalog.yml:142-144` and Rocket.Chat at `:505-507`.
   Stateless / read-only templates can omit OR use `"true"`
   placeholders with a justifying comment (Synapse at `:614-616`
   is the placeholder pattern). `uv run build/lint_quiesce.py`
   enforces snippet safety (no curl/wget, no rm outside the app's
   data path).
6. `uv run build/render.py` locally. Commit the regenerated
   `blueprints/<id>/` and the updated `templates.json`.
7. Open a PR. CI must pass `build-and-verify.yml` (idempotent render)
   and `check:unicode`.
8. After merge, `git tag -a vX.Y.Z -m "..." && git push --tags`.

## When to bump the catalog schema

The schema (catalog.yml entry shape) is a contract with the ops/
loader and with `generate-template-docs.py`. Breaking changes need
coordinated PRs:

1. Land the new shape in templates/ behind a version bump (major).
2. Update ops/'s catalog loader in the same merge window.
3. Update `generate-template-docs.py` in catenahq/ops.
4. Bump the vendored tarball in catenahq/ops via the bump workflow.

## What does NOT live here

- Operator-side wiring (vault key names, OIDC client minting flow,
  env_managed_keys re-injection logic). All in catenahq/ops.
- Per-VPS runtime state. All under `/var/lib/catena/` on each VPS.
- Docs site copy. catenahq/docs generates the per-template pages
  from `source/catalog.yml` via a sibling-write generator.
