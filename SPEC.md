# catena-templates -- Specification

This file states what this repository promises. Every claim in the
invariants table points at a machine-checked gate; the maintainers' CI
resolves each pointer on every change.

## Intent

The canonical application catalog for Catena. A template is two
hand-edited files -- its metadata at `sources/<id>.json` and its compose
at `blueprints/<id>/docker-compose.yml` -- from which everything else is
rendered: the per-app README, the Portainer App Templates v3
`templates.json`, and the machine catalog `catalog.json`.

## Boundaries

- **Hand-edited vs rendered.** The hand-edited files are
  `sources/<id>.json`, `blueprints/<id>/docker-compose.yml` and an
  optional `blueprints/<id>/logo.png`. Everything else is a build
  output, committed and drift-gated: the per-app `README.md` and
  placeholder logo, plus `templates.json`, `catalog.json` and
  `index.html`. Editing one of those directly fails CI.
- **The app carries its own declarations.** What the server needs to back
  up, update and restore an app is the `x-catena` block at the root of the
  app's compose, its env included. A host adds the compose byte for byte, so a deployed
  app keeps the block it was deployed with and the server does not read
  this catalog to manage it. A commit on `main` reaches every new deploy.
- **One directory per template.** `blueprints/<id>/` is the unit
  a host fetches at a tag, so it is also where that template's
  compose, README and logo live. Nothing about a template is duplicated
  elsewhere in the repository.
- **Two output formats, one input.** `templates.json` is the Portainer
  App Templates index and carries only what that format defines;
  `catalog.json` carries the rest (SSO mode, the compose block's backup
  quiesce hooks and update commands, bench membership, sizing, bilingual
  prose) for the Catena side.
- **Secrets.** Compose files carry sentinel placeholders only; real
  values are injected at deploy time on the target server.
- **Public by necessity.** A host fetches an app from this repository by
  sparse clone, so the whole catalog is public.

## Invariants

| Invariant | Enforced by |
| --- | --- |
| Rendered outputs are exactly what `sources/` renders to (idempotent build) | `workflow:build-and-verify.yml` |
| Every source file matches `sources.schema.json`, and `templates.json` matches the published Portainer format in `Schema.json` | `workflow:build-and-verify.yml` |
| A hand-placed logo is a square PNG the control panel keeps, and its source names where it comes from | `workflow:build-and-verify.yml` |
| An entry imported from a third-party registry names its source, that source's licence and notice, and the published catalog marks it untested | `workflow:build-and-verify.yml` |
| Every catalog image reference is CVE-scanned | `workflow:security.yml`, `scanctl:trivy` |
| Source is scanned on every change (secrets, vulnerable deps, static analysis, rendered config) | `workflow:security.yml`, `scanctl:gitleaks`, `scanctl:osv-scanner`, `scanctl:semgrep`, `scanctl:trivy` |
| A push to `main` sends only the commits that passed the app checks to the bench, and an app is tagged only when they passed at its last change | `workflow:app-main.yml`, `workflow:app-tag.yml` |
| A change of an app's presentation alone keeps its last app-tests verdict and is tagged without a bench run; a change of what runs or of its tests runs the bench | `workflow:app-dispatch.yml`, `workflow:app-tag.yml` |
| Templates deploy and run end-to-end on real servers | `bench:ce_install_suite` |

Gate pointer grammar: `workflow:<file>` = a CI workflow in this
repository; `scanctl:<tool>` = a scanner run by the bundled security
workflow; `bench:<scenario>` = a rehearsal scenario of the Catena
suite that provisions disposable virtual machines and deploys from
this catalog.
