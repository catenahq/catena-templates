# catena-templates -- Specification

This file states what this repository promises. Every claim in the
invariants table points at a machine-checked gate; the maintainers' CI
resolves each pointer on every change. The generated companion sheet
[VALIDATION.md](VALIDATION.md) lists the CI gates in force.

## Intent

The canonical application catalog for Catena: one hand-edited source of
truth (`source/catalog.yml`, `source/sizing-data.yml`,
`source/compose/*.compose.yml`) from which everything else is rendered
-- the per-app blueprints, the Portainer App Templates v3 `templates.json`,
and the client-facing per-app documentation pages.

## Boundaries

- **Hand-edited vs rendered.** Only `source/` is hand-edited.
  `blueprints/` and `templates.json` are build outputs, committed and
  drift-gated; editing them directly fails CI.
- **Secrets.** Compose files carry sentinel placeholders only; real
  values are injected at deploy time on the target server.
- **Public by necessity.** Portainer fetches `templates.json` from this
  repository's raw URL, so the whole catalog is public.

## Invariants

| Invariant | Enforced by |
| --- | --- |
| Rendered outputs are exactly what `source/` renders to (idempotent build) | `workflow:build-and-verify.yml` |
| Every catalog image reference is CVE-scanned | `workflow:trivy-images.yml` |
| Source is scanned on every change (secrets, vulnerable deps, static analysis, rendered config) | `workflow:security.yml`, `scanctl:gitleaks`, `scanctl:osv-scanner`, `scanctl:semgrep`, `scanctl:trivy` |
| Templates deploy and run end-to-end on real servers | `bench:ce_install_suite` |

Gate pointer grammar: `workflow:<file>` = a CI workflow in this
repository; `scanctl:<tool>` = a scanner run by the bundled security
workflow; `bench:<scenario>` = a rehearsal scenario of the Catena
suite that provisions disposable virtual machines and deploys from
this catalog.
