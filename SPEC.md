# catena-templates -- Specification

This file states what this repository promises. Every claim in the
invariants table points at a machine-checked gate; the maintainers' CI
resolves each pointer on every change. The generated companion sheet
[VALIDATION.md](VALIDATION.md) lists the CI gates in force.

## Intent

The canonical application catalog for Catena: one hand-edited source of
truth (`sources/<id>.json` plus its `sources/compose/<id>.compose.yml`)
from which everything else is rendered -- the per-app blueprints, the
Portainer App Templates v3 `templates.json`, the machine catalog
`catalog.json`, and the client-facing per-app documentation pages.

## Boundaries

- **Hand-edited vs rendered.** Only `sources/` is hand-edited.
  `blueprints/`, `templates.json`, `catalog.json` and `index.html` are
  build outputs, committed and drift-gated; editing them directly fails
  CI.
- **Two output formats, one input.** `templates.json` is the Portainer
  App Templates index and carries only what that format defines;
  `catalog.json` carries the rest (SSO mode, backup quiesce hooks, bench
  membership, sizing, bilingual prose) for the Catena side.
- **Secrets.** Compose files carry sentinel placeholders only; real
  values are injected at deploy time on the target server.
- **Public by necessity.** Portainer fetches `templates.json` from this
  repository's raw URL, so the whole catalog is public.

## Invariants

| Invariant | Enforced by |
| --- | --- |
| Rendered outputs are exactly what `sources/` renders to (idempotent build) | `workflow:build-and-verify.yml` |
| Every source file matches `sources.schema.json`, and `templates.json` matches the published Portainer format in `Schema.json` | `workflow:build-and-verify.yml` |
| Every catalog image reference is CVE-scanned | `workflow:trivy-images.yml` |
| Source is scanned on every change (secrets, vulnerable deps, static analysis, rendered config) | `workflow:security.yml`, `scanctl:gitleaks`, `scanctl:osv-scanner`, `scanctl:semgrep`, `scanctl:trivy` |
| Templates deploy and run end-to-end on real servers | `bench:ce_install_suite` |

Gate pointer grammar: `workflow:<file>` = a CI workflow in this
repository; `scanctl:<tool>` = a scanner run by the bundled security
workflow; `bench:<scenario>` = a rehearsal scenario of the Catena
suite that provisions disposable virtual machines and deploys from
this catalog.
