# catena-templates -- Specification

This file states what this repository promises. Every claim in the
invariants table points at a machine-checked gate; the maintainers' CI
resolves each pointer on every change.

## Intent

The canonical application catalog for Catena. A template is one folder,
`blueprints/<id>/`, holding its hand-edited `services.yml`, `x-catena.yml`, hook
scripts under `hooks/`, card (`app.json`), optional `test.yml` and optional
`logo.png`; everything else is rendered from them: the packed
`docker-compose.yml` a host deploys, the machine catalog `catalog.json` and the
static preview `index.html`.

## Boundaries

- **Hand-edited vs rendered.** The hand-edited files are
  `blueprints/<id>/services.yml`, `blueprints/<id>/x-catena.yml`, the script
  files under `blueprints/<id>/hooks/`, `blueprints/<id>/app.json`, an
  optional `blueprints/<id>/test.yml`, an optional
  `blueprints/<id>/logo.png` and the shared `snippets/`. Everything else is a
  build output, committed and drift-gated: each app's packed
  `docker-compose.yml` and placeholder logo, plus `catalog.json` and
  `index.html`. Editing one of those directly fails CI.
- **The app carries its own declarations.** What the server needs to back
  up, update and restore an app is the `x-catena` block at the root of the
  app's compose, its env included. A host adds the compose byte for byte, so a deployed
  app keeps the block it was deployed with and the server does not read
  this catalog to manage it. A commit on `main` reaches every new deploy.
- **One directory per template.** `blueprints/<id>/` is the unit
  a host fetches at a tag, so it is also where that template's
  compose, card and logo live. Nothing about a template is duplicated
  elsewhere in the repository.
- **One catalog file.** `catalog.json` carries the SSO mode, the compose
  block's backup quiesce hooks and update commands, bench membership,
  sizing and the bilingual prose for the Catena side.
- **Secrets.** The repository holds no secret; the server mints each
  generated value when it deploys the app.
- **Public by necessity.** A host fetches an app from this repository by
  sparse clone, so the whole catalog is public.

## Invariants

| Invariant | Enforced by |
| --- | --- |
| Rendered outputs are exactly what the `app.json` files and composes render to (idempotent build) | `workflow:build-and-verify.yml` |
| Every `app.json` matches `app.schema.json`, and every compose block and every `x-catena.yml` matches `block.schema.json` | `workflow:build-and-verify.yml` |
| A hand-placed logo is a square PNG the control panel keeps, and its source names where it comes from | `workflow:build-and-verify.yml` |
| An entry imported from a third-party registry names its source, that source's licence and notice, and the published catalog marks it untested | `workflow:build-and-verify.yml` |
| Every catalog image reference is CVE-scanned | `workflow:security.yml`, `scanctl:trivy` |
| Source is scanned on every change (secrets, vulnerable deps, static analysis, rendered config) | `workflow:security.yml`, `scanctl:gitleaks`, `scanctl:osv-scanner`, `scanctl:semgrep`, `scanctl:trivy` |
| A push to `main` sends only the commits that passed the app checks to the bench, and an app is tagged only when they passed at its last change | `workflow:app-main.yml`, `workflow:app-tag.yml` |
| A change of an app's presentation alone keeps its last app-tests verdict and is tagged without a bench run; a change of what runs or of its tests runs the bench | `workflow:app-dispatch.yml`, `workflow:app-tag.yml` |
| The compose a host deploys for an app held as neat files is the one the render packs from them, and a change whose compose differs from that render fails | `workflow:app-ci.yml` |
| A catalog app that opts in exports to a folder plain Docker starts, and it answers on its published port on a machine without Catena | `workflow:app-ci.yml` |
| Templates deploy and run end-to-end on real servers | `bench:ce_install_suite` |

Gate pointer grammar: `workflow:<file>` = a CI workflow in this
repository; `scanctl:<tool>` = a scanner run by the bundled security
workflow; `bench:<scenario>` = a rehearsal scenario of the Catena
suite that provisions disposable virtual machines and deploys from
this catalog.
