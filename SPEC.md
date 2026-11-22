# catena-templates -- Specification

This file states what this repository promises. Every claim in the
invariants table points at a machine-checked gate; the maintainers' CI
resolves each pointer on every change.

## Intent

The canonical application catalog for Catena. A template is one folder,
`blueprints/<id>/`, holding its hand-edited `services.yml`, `x-catena.yml`, hook
scripts under `hooks/`, card (`app.json`), optional `test.yml` and optional
`logo.png`; the packed `docker-compose.yml` a host deploys is rendered from them, and
the machine catalog is what `build/catalog.py` prints from them.

## Boundaries

- **Hand-edited vs rendered.** The hand-edited files are
  `blueprints/<id>/services.yml`, `blueprints/<id>/x-catena.yml`, the script
  files under `blueprints/<id>/hooks/`, `blueprints/<id>/app.json`, an
  optional `blueprints/<id>/test.yml`, an optional
  `blueprints/<id>/logo.png` and the shared `snippets/`. Everything else is a
  build output, committed and drift-gated: each app's packed
  `docker-compose.yml` and placeholder logo. Editing one of those directly fails CI.
- **The app carries its own declarations.** What the server needs to back
  up, update and restore an app is the `x-catena` block at the root of the
  app's compose, its env included. A host adds the compose byte for byte, so a deployed
  app keeps the block it was deployed with and the server does not read
  this catalog to manage it. A commit on `main` reaches every new deploy.
- **One directory per template.** `blueprints/<id>/` is the unit
  a host fetches at a tag, so it is also where that template's
  compose, card and logo live. Nothing about a template is duplicated
  elsewhere in the repository.
- **One catalog document.** `build/catalog.py` prints the SSO mode, the compose
  block's backup quiesce hooks and update commands, bench membership,
  sizing and the bilingual prose for the Catena side.
- **Secrets.** The repository holds no secret; the server mints each
  generated value when it deploys the app.
- **Public by necessity.** A host fetches an app from this repository by
  sparse clone, so the whole catalog is public.

## Invariants

| Invariant | Enforced by |
| --- | --- |
| Rendered outputs are exactly what the `app.json` files and composes render to (idempotent build) | `checks:render` |
| Every `app.json` matches `app.schema.json`, and every compose block and every `x-catena.yml` matches `block.schema.json` | `checks:schema` |
| A hand-placed logo is a square PNG the control panel keeps, and its source names where it comes from | `checks:python` |
| An entry imported from a third-party registry names its source, that source's licence and notice, and the published catalog marks it untested | `checks:python` |
| Access to each catalog app's main URL is set by the app's Access setting (`AUTH_MODE`), which starts at the catalog's choice and can be changed by an administrator | `checks:python` |
| When an app has another URL that only works with one fixed access, such as the mail policy page that every sending mail server reads, that URL keeps its fixed access, and the reason is written next to it | `checks:python` |
| Every catalog image reference is CVE-scanned | `checks:apps`, `checks:scan`, `scanctl:trivy` |
| Source is scanned on every change (secrets, vulnerable deps, static analysis, rendered config) | `checks:scan`, `scanctl:gitleaks`, `scanctl:osv-scanner`, `scanctl:semgrep`, `scanctl:trivy` |
| A pull request that changes an app sends that app alone to the app tests once its ci run completes, and a merged app is tagged only when the app tests passed its change | `checks:apps`, `workflow:ci-control.yml#control` |
| A change of an app's presentation alone keeps its last app-tests verdict and is tagged without an app-tests run; a change of what runs or of its tests runs the app tests | `workflow:ci-control.yml#control` |
| The compose a host deploys for an app held as neat files is the one the render packs from them, and a change whose compose differs from that render fails | `checks:render` |
| A catalog app that opts in exports to a folder plain Docker starts, and it answers on its published port on a machine without Catena | `checks:apps` |
| Templates deploy and run end-to-end on real servers | `bench:ce_install_suite` |

Gate pointer grammar: `workflow:<file>#<job>` = a job of a CI workflow in
this repository; `checks:<name>` = a check of `.github/checks.yml` (`apps`
the app checks, `scan` the security scan) that the shared PR workflow runs;
`scanctl:<tool>` = a scanner the scan runs; `bench:<scenario>` = a
rehearsal scenario of the Catena suite that provisions disposable virtual
machines and deploys from this catalog.
